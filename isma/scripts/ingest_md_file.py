#!/usr/bin/env python3
"""
Single-file .md ingester for the build-in-public watch pipeline.

Reads one markdown file, phi-tiles into search_512/context_2048/full_4096,
embeds via the local embedding server, and writes ISMA_Quantum objects to
Weaviate. Idempotent: dedups by content_hash.

Source-type derivation from path:
  */<repo>/recaps/YYYY-MM-DD_{session}.md   -> source_type=recap
  */foundations/*.md                        -> source_type=foundation
  */audits/*.md                             -> source_type=audit_packet
  fallback                                  -> source_type=document

Usage: ingest_md_file.py <path/to/file.md>
"""

import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from isma.config import EMBEDDING_URL, WEAVIATE_URL
from isma.src.phi_tiling import multi_scale_tile
from isma.src.hmm.ids import content_hash

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingest_md")

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
WEAVIATE_CLASS = "ISMA_Quantum"

session = requests.Session()


# ── Source-type classifier ────────────────────────────────────────────────
def classify(path: Path) -> dict:
    """Infer source_type + session/date metadata from the file path."""
    parts = path.parts
    name = path.name

    # Per-repo recaps: */<repo>/recaps/YYYY-MM-DD_{session}.md
    if "recaps" in parts:
        try:
            repo_idx = parts.index("recaps") - 1
            repo = parts[repo_idx] if repo_idx >= 0 else "unknown"
        except ValueError:
            repo = "unknown"
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)\.md$", name)
        date = m.group(1) if m else ""
        sess = m.group(2) if m else name[:-3] if name.endswith(".md") else name
        return {
            "source_type": "recap",
            "source_session": sess,
            "source_repo": repo,
            "source_date": date,
        }

    # Foundations
    if "foundations" in parts:
        sess = name[:-3] if name.endswith(".md") else name
        return {"source_type": "foundation",
                "source_session": sess, "source_repo": "treasurer",
                "source_date": ""}

    # Dispatch-log audits
    if "audits" in parts:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)\.md$", name)
        date = m.group(1) if m else ""
        sess = m.group(2) if m else name[:-3] if name.endswith(".md") else name
        return {"source_type": "audit_packet",
                "source_session": sess, "source_repo": "dispatch_log",
                "source_date": date}

    # Fallback
    return {"source_type": "document",
            "source_session": name[:-3] if name.endswith(".md") else name,
            "source_repo": "", "source_date": ""}


# ── Embedding ─────────────────────────────────────────────────────────────
def get_embeddings(texts: list) -> list:
    """Get embeddings with retry/backoff. Small batches to avoid embedding-server OOM
    under concurrent load from HMM workers + this ingester."""
    all_vectors = []
    # Single-tile per request - 4096-token tiles peak ~1 GiB; Semaphore(4) on the server already handles cross-client parallelism, no need to stack from one client.
    BATCH = 1
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        for attempt in range(5):
            try:
                r = session.post(EMBEDDING_URL,
                                 json={"input": chunk, "model": EMBEDDING_MODEL},
                                 timeout=180)
                r.raise_for_status()
                data = r.json()["data"]
                data.sort(key=lambda x: x["index"])
                all_vectors.extend(d["embedding"] for d in data)
                break
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (500, 502, 503):
                    backoff = (2 ** attempt) * 5
                    log.warning(f"  embedding {e.response.status_code} (attempt {attempt+1}/5) — backoff {backoff}s")
                    time.sleep(backoff)
                else:
                    raise
        else:
            raise RuntimeError(f"embedding failed after 5 attempts on batch {i}")
    return all_vectors


# ── Weaviate ──────────────────────────────────────────────────────────────
def check_exists_doc(doc_hash_value: str) -> bool:
    """True if any tile already exists for this doc_hash."""
    q = (f'{{ Get {{ {WEAVIATE_CLASS}('
         f'where: {{ path: ["doc_hash"], operator: Equal, '
         f'valueText: "{doc_hash_value}" }}, limit: 1) {{ doc_hash }} }} }}')
    try:
        r = session.post(f"{WEAVIATE_URL}/v1/graphql",
                         json={"query": q}, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", {}).get("Get", {}).get(WEAVIATE_CLASS) or []
        return len(items) > 0
    except Exception as e:
        log.warning(f"check_exists_doc failed: {e}")
        return False


def insert_objects(objs: list) -> int:
    """Batch-insert objects into Weaviate. Returns count successfully inserted."""
    if not objs:
        return 0
    payload = {"objects": objs}
    try:
        r = session.post(f"{WEAVIATE_URL}/v1/batch/objects",
                         json=payload, timeout=60)
        r.raise_for_status()
        results = r.json()
        # Count ONLY explicit SUCCESS. A missing/failed per-object status is a
        # FAILURE, and its error is surfaced — a read-only store (e.g. Weaviate
        # DISKGATE at 90% disk) returns HTTP 200 with per-object
        # status=FAILED; the old `or "SUCCESS"` default miscounted that as
        # inserted, reporting success while zero tiles persisted.
        ok = 0
        for x in results:
            res = x.get("result", {}) or {}
            if res.get("status") == "SUCCESS":
                ok += 1
            else:
                log.error(f"  object insert FAILED: status={res.get('status')!r} "
                          f"errors={res.get('errors')}")
        return ok
    except Exception as e:
        log.error(f"batch insert failed: {e}")
        return 0


def supersede_prior_versions(source_file: str, new_doc_hash: str) -> int:
    """Mark tiles of PRIOR versions of this source_file as superseded.

    Re-ingesting a changed file previously left the old version's tiles
    co-current with the new ones — the stale text could outrank the
    correction (observed live 2026-07-23: a corrected identity doc's old
    version stayed the top hit). The file on disk is canonical for
    document-type ingests, so an older snapshot of the same source_file is
    superseded by definition (version-supersession, not opinion-correction —
    mirrors the memory-governance supersede-on-write semantics).

    Scoped strictly: only tiles whose source_file matches exactly AND whose
    doc_hash differs from the new ingest AND not already superseded.
    Fail-loud: PATCH errors are logged per-object, never swallowed silently.
    Returns the number of tiles marked.
    """
    gql = {
        "query": '''{ Get { ISMA_Quantum(limit: 500, where: {operator: And, operands: [
            {path: ["source_file"], operator: Equal, valueText: "%s"},
            {path: ["is_superseded"], operator: NotEqual, valueBoolean: true}
        ]}) { doc_hash _additional { id } } } }''' % source_file.replace('"', '')
    }
    try:
        r = session.post(f"{WEAVIATE_URL}/v1/graphql", json=gql, timeout=30)
        r.raise_for_status()
        tiles = (r.json().get("data", {}).get("Get", {})
                 .get("ISMA_Quantum", []) or [])
    except Exception as e:
        log.error(f"supersede query failed for {source_file}: {e}")
        return 0
    stale = [t for t in tiles
             if t.get("doc_hash") and t["doc_hash"] != new_doc_hash]
    marked = 0
    for t in stale:
        oid = t["_additional"]["id"]
        try:
            pr = session.patch(
                f"{WEAVIATE_URL}/v1/objects/ISMA_Quantum/{oid}",
                json={"class": "ISMA_Quantum", "properties": {
                    "is_superseded": True,
                    "superseded_by": new_doc_hash[:12],
                }}, timeout=15)
            pr.raise_for_status()
            marked += 1
        except Exception as e:
            log.error(f"  supersede PATCH failed for {oid}: {e}")
    if stale:
        log.info(f"  superseded {marked}/{len(stale)} prior-version tiles "
                 f"(old doc versions of {source_file})")
    return marked


# ── Main ──────────────────────────────────────────────────────────────────
def ingest_file(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        log.error(f"not a file: {path}")
        return False
    if path.suffix.lower() != ".md":
        log.info(f"skip (not .md): {path}")
        return True

    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 50:
        log.info(f"skip (too short): {path}")
        return True

    doc_hash = content_hash(text)
    meta = classify(path)
    log.info(f"file={path.name} hash={doc_hash[:12]} source_type={meta['source_type']} repo={meta['source_repo']}")

    # Idempotency: dedup against any existing tile carrying this doc_hash.
    if check_exists_doc(doc_hash):
        log.info(f"already ingested: {doc_hash[:12]}")
        return True

    tiles = multi_scale_tile(text, source_file=str(path), layer=meta["source_type"])
    if not tiles:
        log.warning(f"no tiles produced from {path}")
        return False

    log.info(f"  phi-tiled into {len(tiles)} tiles "
             f"(search_512={sum(1 for t in tiles if t.scale=='search_512')} "
             f"context_2048={sum(1 for t in tiles if t.scale=='context_2048')} "
             f"full_4096={sum(1 for t in tiles if t.scale=='full_4096')})")

    # Embed each tile's content
    contents = [t.text for t in tiles]
    try:
        vectors = get_embeddings(contents)
    except Exception as e:
        log.error(f"embedding failed: {e}")
        return False

    if len(vectors) != len(tiles):
        log.error(f"embedding count mismatch: tiles={len(tiles)} vectors={len(vectors)}")
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    src_basename = path.name
    src_file = str(path)

    objs = []
    for tile, vec in zip(tiles, vectors):
        c = tile.text
        # Tile-level content_hash includes scale + index to disambiguate
        tile_hash = content_hash(f"{c}::{tile.scale}::{tile.index}")
        objs.append({
            "class": WEAVIATE_CLASS,
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"{doc_hash}/{tile.scale}/{tile.index}")),
            "vector": vec,
            "properties": {
                "content": c,
                "content_hash": tile_hash,
                "content_preview": c[:200],
                "doc_hash": doc_hash,
                "scale": tile.scale,
                "tile_index": tile.index,
                "start_char": tile.start_char,
                "end_char": tile.end_char,
                "token_count": tile.estimated_tokens,
                "source_type": meta["source_type"],
                "source_basename": src_basename,
                "source_file": src_file,
                "source_session": meta["source_session"],
                "source_repo": meta["source_repo"],
                "source_date": meta["source_date"],
                "hmm_enriched": False,
                "created_at": now_iso,
                "ingested_at": now_iso,
                "ingest_pipeline": "watch_md_v1",
            },
        })

    # Add a placeholder object at full_4096 hash to mark idempotency
    log.info(f"  inserting {len(objs)} objects to Weaviate")
    ok = insert_objects(objs)
    log.info(f"  inserted {ok}/{len(objs)}")
    if ok == len(objs):
        # New version fully persisted -> retire any prior versions of this
        # file so the stale snapshot cannot outrank the current one.
        # Only after a COMPLETE insert: a partial insert must not orphan
        # the old version (better co-current than amnesia).
        supersede_prior_versions(str(path), doc_hash)
    return ok == len(objs)


def main():
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <path-to-md-file>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1]).resolve()
    ok = ingest_file(target)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
