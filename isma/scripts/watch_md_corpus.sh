#!/bin/bash
# watch_md_corpus.sh - auto-add watcher for markdown -> ISMA.
#
# Periodic-scan strategy. Every INTERVAL seconds it re-runs
# backfill_md_corpus.py --apply, which:
#   - content-hash dedups (unchanged file = no-op skip)
#   - ingests new files
#   - multi-path identical bodies ingest once
# Additive-only by default (NO deletes) so it can never destroy enriched/legacy
# tiles. Deterministic tile UUIDs (uuid5 of doc_hash/scale/index) make even a
# race with a manual backfill idempotent (same IDs overwrite, not duplicate).
#
# Run:  tmux new-session -d -s md-corpus-watch "bash ./isma/scripts/watch_md_corpus.sh"
# Log:  /tmp/md_corpus_watch.log
# Stop: tmux kill-session -t md-corpus-watch

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
: "${ISMA_MD_ROOTS_FILE:?set ISMA_MD_ROOTS_FILE to a newline-delimited markdown roots file}"
PYBIN="${PYBIN:-python3}"
DRIVER="$SCRIPT_DIR/backfill_md_corpus.py"
INTERVAL="${INTERVAL:-900}"   # 15 min
LOG="${LOG:-/tmp/md_corpus_watch.log}"

export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf "[%s] %s\n" "$(ts)" "$*" | tee -a "$LOG"; }

log "=== md-corpus-watch starting; interval=${INTERVAL}s additive-only ==="
while true; do
    log "--- scan pass begin ---"
    # --pace gentle so we never starve query/HMM traffic on the embedding server
    "$PYBIN" "$DRIVER" --apply --roots-file "$ISMA_MD_ROOTS_FILE" --pace 0.05 2>&1 \
        | grep -E "SUMMARY|ingested|present-skip|dup body|failed" \
        | sed 's/^/  /' | tee -a "$LOG" >/dev/null
    log "--- scan pass end; sleeping ${INTERVAL}s ---"
    sleep "$INTERVAL"
done
