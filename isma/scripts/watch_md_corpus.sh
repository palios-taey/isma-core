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

# ALERTING. Until 2026-09-08 this watcher had NO alert path: a pass could report
# "failed: N" and the number went only to $LOG, which nobody reads. On 2026-09-08 a
# transient stall timed out both Weaviate and the embedding server and two files
# failed to ingest — correctly logged, and silent to every consumer of ISMA. A
# failure that only exists in an unread log is indistinguishable from success.
#
# Deduped by state transition, like the disk canary: alert on entering the failing
# state, remind at most once per REMIND window while it persists, and say so on
# recovery. A per-pass alert on a chronic condition trains people to ignore it.
ALERT_CMD="${ISMA_MD_WATCH_ALERT_CMD:-}"
STATE="${ISMA_MD_WATCH_STATE:-/tmp/md_corpus_watch.state}"
REMIND_SECS="${ISMA_MD_WATCH_REMIND_SECS:-86400}"

raise() { log "$1"; [ -n "$ALERT_CMD" ] && $ALERT_CMD "$1" >>"$LOG" 2>&1 || true; }

log "=== md-corpus-watch starting; interval=${INTERVAL}s additive-only ==="
while true; do
    log "--- scan pass begin ---"
    # The driver's exit code used to be SWALLOWED BY THE PIPE: `driver | grep | tee`
    # reports tee's status, so a driver that died outright looked identical to a clean
    # pass. Capture to a file, read the real status, then filter.
    OUT="$(mktemp)"
    # --pace gentle so we never starve query/HMM traffic on the embedding server
    "$PYBIN" "$DRIVER" --apply --roots-file "$ISMA_MD_ROOTS_FILE" --pace 0.05 >"$OUT" 2>&1
    rc=$?
    grep -E "SUMMARY|ingested|present-skip|dup body|failed" "$OUT" \
        | sed 's/^/  /' | tee -a "$LOG" >/dev/null
    failed="$(grep -oE 'failed[[:space:]]*:[[:space:]]*[0-9]+' "$OUT" | grep -oE '[0-9]+$' | tail -1)"
    failed="${failed:-0}"
    rm -f "$OUT"

    NOW="$(date +%s)"
    LAST_TS=0; [ -r "$STATE" ] && read -r LAST_TS _ < "$STATE" 2>/dev/null || LAST_TS=0
    if [ "$rc" -ne 0 ]; then
        raise "ISMA MD-WATCH DRIVER FAILED: backfill_md_corpus.py exited ${rc}. No files were ingested this pass; prose writes are NOT landing."
        printf '%s driver_rc=%s\n' "$NOW" "$rc" > "$STATE"
    elif [ "$failed" -gt 0 ]; then
        if [ "$LAST_TS" -eq 0 ] || [ $((NOW - LAST_TS)) -ge "$REMIND_SECS" ]; then
            raise "ISMA MD-WATCH INGEST FAILURES: ${failed} file(s) failed this pass. Additive-only, so they are retried next pass — but a write a consumer expected may not have landed. Log: ${LOG}"
            printf '%s failed=%s\n' "$NOW" "$failed" > "$STATE"
        fi
    elif [ -f "$STATE" ]; then
        raise "RECOVERED: ISMA md-corpus watch pass completed with 0 failures."
        rm -f "$STATE"
    fi

    log "--- scan pass end; sleeping ${INTERVAL}s ---"
    sleep "$INTERVAL"
done
