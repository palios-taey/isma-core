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
def live_tiles_for(doc_hash_value: str, source_file: str) -> tuple:
    """Count LIVE tiles carrying this doc_hash, split by path.

    Returns (n_at_this_path, n_at_other_paths).

    Two corrections to the old `check_exists_doc`, which asked only "does ANY tile
    carry this hash?" and returned a bare bool:

    1. SUPERSEDED-AWARE. A superseded tile is a historical record, not a reason to
       skip an ingest. The old check counted it, so a superseded copy blocked its
       own replacement — which is what made BOTH repair orderings destructive:
       ingest-then-supersede left zero live tiles because the ingest was a no-op,
       and supersede-then-ingest skipped because the superseded tiles still
       matched. See docs/MEMORY_GOVERNANCE_REVIEW_2026-08-01.md §4.

    2. PATH-AWARE. The dedup key is (doc_hash, source_file), not doc_hash alone.
       Identical content at a DIFFERENT path is not "already ingested" — it is the
       same document indexed under a stale path. Skipping it left the canonical
       path permanently empty while the stale path stayed live and retrievable.

    Cross-path thrash is not possible here: the batch driver dedups identical
    bodies within a cycle (`seen_hashes`, first path wins, deterministic), so only
    one path per body is ever offered to this function in a given pass.
    """
    q = ('{ Get { %s(limit: 500, where: {operator: And, operands: ['
         '{path: ["doc_hash"], operator: Equal, valueText: "%s"},'
         '{path: ["is_superseded"], operator: NotEqual, valueBoolean: true}'
         ']}) { source_file } } }' % (WEAVIATE_CLASS, doc_hash_value))
    try:
        r = session.post(f"{WEAVIATE_URL}/v1/graphql",
                         json={"query": q}, timeout=30)
        r.raise_for_status()
        items = (r.json().get("data", {}).get("Get", {})
                 .get(WEAVIATE_CLASS) or [])
    except Exception as e:
        # Fail-loud-ish: report "nothing live" so the caller ingests rather than
        # silently skipping. A duplicate is recoverable; a silent skip is not.
        log.warning(f"live_tiles_for failed ({e}) — treating as no live tiles")
        return (0, 0)
    here = sum(1 for t in items if t.get("source_file") == source_file)
    return (here, len(items) - here)


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
        # Count ONLY explicit SUCCESS, and surface the error for anything else.
        #
        # The old form was `(res.get("status") or "SUCCESS") == "SUCCESS"`. Be
        # precise about what that did, because an earlier version of this comment
        # was wrong and asserted more than was known:
        #   status="FAILED"        -> "FAILED" != "SUCCESS"  -> correctly NOT counted
        #   status missing/None/"" -> defaults to "SUCCESS"  -> COUNTED. False success.
        # So the defect is the MISSING-status path, not the FAILED path. Verified
        # live 2026-08-01: a rejected object comes back status="FAILED" with errors
        # populated, which the old form already handled correctly.
        #
        # OBSERVED: during the DISKGATE incident this script reported inserted
        # counts for writes that did not persist. UNKNOWN: whether a read-only
        # store omits the per-object status (which would make the old default the
        # cause) or fails some other way — reproducing it needs a full disk, which
        # is not worth doing. This form is correct under BOTH readings: it counts
        # only an explicit SUCCESS, so a missing status can never inflate the count.
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

    TWO kinds of stale tile are retired, and only these two:

      1. VERSION CHANGE — same source_file, different doc_hash. An older snapshot
         of this same file.
      2. PATH MOVE — same doc_hash, different source_file. Byte-identical content
         indexed under a stale path. This is what left the canonical path empty
         while a private-tree path stayed live and retrievable; the old dedup saw
         the stale-path tiles, called it "already ingested", and skipped forever.

    Both are supersession by definition, not opinion-correction. Nothing is
    deleted — `is_superseded=true` is the only mutation, so every step here is
    reversible by flipping the flag back.

    Fail-loud: PATCH errors are logged per-object, never swallowed silently.
    Returns the number of tiles marked.
    """
    safe_path = source_file.replace('"', '')
    # (1) same path, older content
    q_version = '''{ Get { ISMA_Quantum(limit: 500, where: {operator: And, operands: [
            {path: ["source_file"], operator: Equal, valueText: "%s"},
            {path: ["is_superseded"], operator: NotEqual, valueBoolean: true}
        ]}) { doc_hash source_file _additional { id } } } }''' % safe_path
    # (2) same content, other path
    q_moved = '''{ Get { ISMA_Quantum(limit: 500, where: {operator: And, operands: [
            {path: ["doc_hash"], operator: Equal, valueText: "%s"},
            {path: ["is_superseded"], operator: NotEqual, valueBoolean: true}
        ]}) { doc_hash source_file _additional { id } } } }''' % new_doc_hash

    def fetch(q, label):
        try:
            r = session.post(f"{WEAVIATE_URL}/v1/graphql", json={"query": q}, timeout=30)
            r.raise_for_status()
            return (r.json().get("data", {}).get("Get", {})
                    .get("ISMA_Quantum", []) or [])
        except Exception as e:
            log.error(f"supersede {label} query failed for {source_file}: {e}")
            return []

    stale, seen_ids = [], set()
    for t in fetch(q_version, "version"):
        if t.get("doc_hash") and t["doc_hash"] != new_doc_hash:
            stale.append(t); seen_ids.add(t["_additional"]["id"])
    for t in fetch(q_moved, "path-move"):
        oid = t["_additional"]["id"]
        if t.get("source_file") != source_file and oid not in seen_ids:
            stale.append(t); seen_ids.add(oid)
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


# ── Outcomes ──────────────────────────────────────────────────────────────
# ingest_file used to return a bare bool, and returned True for "I did nothing".
# A caller could not tell an ingest from a no-op, so batch counters reported
# skips as successes. Every outcome below is distinct and truthful; the only one
# that means tiles were written is INGESTED.
INGESTED = "ingested"          # new tiles written for this (doc_hash, path)
ALREADY_LIVE = "already-live"  # this exact content is already live at this path
SKIPPED = "skipped"            # not markdown / too short — nothing to do, not an error
FAILED = "failed"              # a real failure; the caller must surface it


def ingest_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        log.error(f"not a file: {path}")
        return FAILED
    if path.suffix.lower() != ".md":
        log.info(f"skip (not .md): {path}")
        return SKIPPED

    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 50:
        log.info(f"skip (too short): {path}")
        return SKIPPED

    doc_hash = content_hash(text)
    meta = classify(path)
    log.info(f"file={path.name} hash={doc_hash[:12]} source_type={meta['source_type']} repo={meta['source_repo']}")

    # Idempotency, keyed on (doc_hash, source_file) over LIVE tiles only.
    n_here, n_elsewhere = live_tiles_for(doc_hash, str(path))
    if n_here:
        # This exact content is already live at this exact path, so there is
        # nothing to WRITE. But "nothing to write" is not "nothing to do":
        # OLDER versions of this same path may still be live alongside it, which
        # is the retrieval-integrity defect itself — several live answers to the
        # same question with no currency signal.
        #
        # The invariant is per-PATH, not per-ingest: after any call, at most one
        # live version of a source_file may remain. Enforcing it only on the
        # writing path left every already-current document permanently
        # un-repairable — a migration over unchanged files would report
        # already-live for each and supersede nothing. Found while executing
        # exactly that migration.
        marked = supersede_prior_versions(str(path), doc_hash)
        if marked:
            log.info(f"already live at this path: {doc_hash[:12]} ({n_here} tiles); "
                     f"retired {marked} stale tile(s) still live alongside it")
        else:
            log.info(f"already live at this path: {doc_hash[:12]} ({n_here} tiles)")
        return ALREADY_LIVE
    if n_elsewhere:
        # Same content, different path — the document moved. Ingest it under the
        # canonical path; supersede_prior_versions retires the stale-path copies.
        log.info(f"same content live at {n_elsewhere} tile(s) under another path "
                 f"— re-homing to {path}")

    tiles = multi_scale_tile(text, source_file=str(path), layer=meta["source_type"])
    if not tiles:
        log.warning(f"no tiles produced from {path}")
        return FAILED

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
        return FAILED

    if len(vectors) != len(tiles):
        log.error(f"embedding count mismatch: tiles={len(tiles)} vectors={len(vectors)}")
        return FAILED

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
        return INGESTED
    return FAILED


def main():
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <path-to-md-file>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1]).resolve()
    outcome = ingest_file(target)
    print(f"outcome: {outcome}")
    # Exit 0 only for outcomes that are not failures. ALREADY_LIVE is a truthful
    # success — nothing needed doing — but the printed outcome distinguishes it
    # from an actual ingest, so a caller can never read "exit 0" as "it wrote".
    return 0 if outcome in (INGESTED, ALREADY_LIVE, SKIPPED) else 1


if __name__ == "__main__":
    sys.exit(main())
