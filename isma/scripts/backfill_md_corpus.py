#!/usr/bin/env python3
"""
Backfill driver for markdown ingestion into ISMA. Walks an explicit roots file,
content-hash dedups against existing ISMA tiles, and ingests new or changed
files via ingest_md_file.ingest_file().

Design (matches dispatch spec):
  - DEDUP on sha256 of file BODY (doc_hash), NOT path:
      * same body at multiple paths   -> ingested ONCE (later paths skip)
      * unchanged file re-run          -> no-op skip
      * changed file                   -> stale path-tiles purged, then re-ingest
  - EXCLUDES: .venv / site-packages / node_modules / .git / .ipynb_checkpoints
    and other local workspace/cache directories that create duplicate trees
  - Writes a manifest path->doc_hash so the watcher can do hash-diff updates.

Usage:
  backfill_md_corpus.py [--apply] [--roots-file FILE] [--manifest FILE] [--limit N]
  (default is DRY-RUN: classifies ingest/skip without writing. --apply to write.)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

if __package__:
    from . import ingest_md_file as ing  # noqa: E402
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    import ingest_md_file as ing  # noqa: E402
# Use the SAME hash the ingester stores as doc_hash (canonicalized sha256[:16]),
# NOT raw sha256 — otherwise dedup never matches existing tiles and we duplicate.
from isma.src.hmm.ids import content_hash  # noqa: E402

ROOTS_FILE_ENV = "ISMA_MD_ROOTS_FILE"
DEFAULT_MANIFEST = Path(__file__).resolve().parent.parent / "reports" / "md_corpus_manifest.json"

EXCLUDE_SUBPATHS = (
    "/.venv/", "/site-packages/", "/node_modules/", "/.git/",
    "/.ipynb_checkpoints/", "/.peer-worktrees/", "/__pycache__/",
)


def excluded(p: str) -> bool:
    return any(x in p for x in EXCLUDE_SUBPATHS)


def load_roots(roots_file: str | None) -> list[str]:
    path_value = roots_file or os.environ.get(ROOTS_FILE_ENV)
    if not path_value:
        raise RuntimeError(
            f"set {ROOTS_FILE_ENV} or pass --roots-file with a newline-delimited list of markdown roots"
        )
    path = Path(path_value)
    if not path.is_file():
        raise RuntimeError(f"roots file not found: {path}")
    roots = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not roots:
        raise RuntimeError(f"roots file is empty: {path}")
    return roots


def iter_md(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not excluded(os.path.join(dirpath, d) + "/")]
            for fn in filenames:
                if fn.endswith(".md"):
                    full = os.path.join(dirpath, fn)
                    if not excluded(full):
                        yield Path(full)


def purge_stale_path_tiles(source_file: str, current_doc_hash: str) -> int:
    """Delete ONLY superseded versions of a changed file that THIS pipeline
    created and that are NOT HMM-enriched. Hard safety scoping so we never
    touch legacy/other-pipeline tiles or destroy enrichment work:
      source_file == path  AND  ingest_pipeline == 'watch_md_v1'
      AND hmm_enriched == false  AND  doc_hash != current
    (NULL doc_hash legacy tiles are excluded because they fail the
     ingest_pipeline==watch_md_v1 predicate.)"""
    q = (f'{{ Get {{ {ing.WEAVIATE_CLASS}('
         f'where: {{ operator: And, operands: ['
         f'{{ path:["source_file"], operator: Equal, valueText: {json.dumps(source_file)} }},'
         f'{{ path:["ingest_pipeline"], operator: Equal, valueText: "watch_md_v1" }},'
         f'{{ path:["hmm_enriched"], operator: Equal, valueBoolean: false }},'
         f'{{ path:["doc_hash"], operator: NotEqual, valueText: {json.dumps(current_doc_hash)} }}'
         f'] }}, limit: 500) {{ _additional {{ id }} doc_hash }} }} }}')
    try:
        r = ing.session.post(f"{ing.WEAVIATE_URL}/v1/graphql", json={"query": q}, timeout=20)
        r.raise_for_status()
        items = (r.json().get("data", {}).get("Get", {}).get(ing.WEAVIATE_CLASS) or [])
    except Exception as e:
        ing.log.warning(f"purge query failed for {source_file}: {e}")
        return 0
    deleted = 0
    for it in items:
        oid = it.get("_additional", {}).get("id")
        if not oid:
            continue
        try:
            d = ing.session.delete(f"{ing.WEAVIATE_URL}/v1/objects/{ing.WEAVIATE_CLASS}/{oid}", timeout=20)
            if d.status_code in (200, 204):
                deleted += 1
        except Exception as e:
            ing.log.warning(f"delete {oid} failed: {e}")
    if deleted:
        ing.log.info(f"  purged {deleted} stale-hash tiles for changed file {Path(source_file).name}")
    return deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default dry-run)")
    ap.add_argument("--roots-file", help="newline-delimited roots override")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--limit", type=int, default=0, help="cap files (0=all)")
    ap.add_argument("--pace", type=float, default=0.0, help="sleep seconds between ingests")
    ap.add_argument("--purge-on-change", action="store_true",
                    help="delete superseded watch_md_v1 unenriched tiles for changed files "
                         "(OFF by default — backfill is additive-only; never deletes legacy/enriched tiles)")
    args = ap.parse_args()

    roots = load_roots(args.roots_file)

    seen_hashes = {}      # doc_hash -> first path (multi-path dedup)
    manifest = {}         # path -> doc_hash
    n_ingest = n_skip_present = n_skip_dupbody = n_skip_short = n_fail = n_purged = 0
    files = list(iter_md(roots))
    if args.limit:
        files = files[:args.limit]
    total = len(files)
    print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] {total} candidate .md files across {len(roots)} roots")

    for i, path in enumerate(files, 1):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  READ-FAIL {path}: {e}"); n_fail += 1; continue
        if len(text.strip()) < 50:
            n_skip_short += 1; continue
        dh = content_hash(text)   # canonicalized sha256[:16] — matches ingester's doc_hash
        manifest[str(path)] = dh

        if dh in seen_hashes:
            n_skip_dupbody += 1
            continue
        seen_hashes[dh] = str(path)

        present = ing.check_exists_doc(dh)
        if present:
            n_skip_present += 1
            continue

        # New body. In apply mode: optionally purge OUR stale unenriched tiles, then ingest.
        if args.apply:
            if args.purge_on_change:
                n_purged += purge_stale_path_tiles(str(path), dh)
            ok = ing.ingest_file(path)
            if ok:
                n_ingest += 1
            else:
                n_fail += 1
            if args.pace:
                time.sleep(args.pace)
        else:
            n_ingest += 1  # would-ingest
        if i % 100 == 0 or i == total:
            print(f"  [{i}/{total}] ingest={n_ingest} present-skip={n_skip_present} "
                  f"dupbody-skip={n_skip_dupbody} short={n_skip_short} fail={n_fail}")

    # write manifest (apply mode only, to avoid clobbering on dry runs)
    if args.apply:
        os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
        json.dump({"generated": time.time(), "count": len(manifest), "files": manifest},
                  open(args.manifest, "w"), indent=0)
        print(f"manifest -> {args.manifest} ({len(manifest)} paths)")

    print("\n=== SUMMARY ===")
    print(f"  candidate files     : {total}")
    print(f"  {'ingested' if args.apply else 'WOULD ingest'}        : {n_ingest}")
    print(f"  skipped (in ISMA)   : {n_skip_present}")
    print(f"  skipped (dup body)  : {n_skip_dupbody}")
    print(f"  skipped (too short) : {n_skip_short}")
    print(f"  stale tiles purged  : {n_purged}")
    print(f"  failed              : {n_fail}")
    print(f"  unique bodies       : {len(seen_hashes)}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
