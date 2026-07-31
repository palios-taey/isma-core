#!/usr/bin/env bash
# Early-warning canary for the DISKGATE cliff.
#
# Weaviate flips the store to READ-ONLY at DISK_USE_READONLY_PERCENTAGE (default 90%).
# Past that line reads keep working PERFECTLY while every write silently fails — so
# search looks healthy, dashboards stay green, and the corpus quietly stops growing.
# That failure is invisible from the read path, which is exactly why it needs a
# mechanical warning ahead of the cliff rather than a human noticing afterwards.
#
# This canary warns at 85% — five points of headroom — so the problem is handled while
# it is still routine. It does NOT clear anything: what to reclaim is a judgment call
# (shared model caches can break a service restart if cleared blind), so this reports
# and lets a human decide.
#
# It also PROBES WRITABILITY rather than inferring it from the percentage. Disk usage
# predicts the cliff; only a real write proves which side of it you are on.
#
# ALERTS ARE DEDUPED BY STATE TRANSITION, not emitted every run. A steady 89% would
# otherwise alert on every timer cycle until the cleanup lands, the fleet would learn
# to ignore it, and the next real cliff would arrive into a muted channel. Alert
# fatigue is how genuine failures get missed. So it alerts on:
#   * CROSSING into the warn band (healthy -> warning)
#   * WORSENING while in the band (a higher percentage than last alerted)
#   * a WRITE-PROBE FAILURE, always, every run — the store being read-only is the
#     emergency itself and must never be suppressed
#   * one REMINDER per ISMA_DISK_REMIND_SECS (default 24h) while steady
# Recovery below the threshold clears the state, so the next crossing alerts again.
set -u

THRESHOLD="${ISMA_DISK_WARN_PERCENT:-85}"
DATA_PATH="${ISMA_WEAVIATE_DATA_PATH:-/var/lib/weaviate}"
WEAVIATE_URL="${WEAVIATE_URL:-http://localhost:8088}"
LOG="${ISMA_CANARY_LOG:-/tmp/disk_headroom_canary.log}"
STATE="${ISMA_CANARY_STATE:-/tmp/disk_headroom_canary.state}"
REMIND_SECS="${ISMA_DISK_REMIND_SECS:-86400}"
# How to raise an alert. Fleet deployments set this to their notifier; if unset the
# canary still logs and exits non-zero, so a supervisor or timer can act on it.
ALERT_CMD="${ISMA_DISK_ALERT_CMD:-}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [ ! -e "$DATA_PATH" ]; then
  echo "$(ts) CANARY CONFIG ERROR: ISMA_WEAVIATE_DATA_PATH '$DATA_PATH' does not exist" >> "$LOG"
  exit 2   # fail loud: a canary watching the wrong filesystem is worse than none
fi

USED=$(df --output=pcent "$DATA_PATH" 2>/dev/null | tail -1 | tr -dc '0-9')
AVAIL=$(df -h --output=avail "$DATA_PATH" 2>/dev/null | tail -1 | tr -d ' ')
if [ -z "${USED:-}" ]; then
  echo "$(ts) CANARY ERROR: could not read disk usage for $DATA_PATH" >> "$LOG"
  exit 2
fi

# Writability probe: create then delete a real object. A percentage predicts the
# cliff; only a write tells you whether the store is already over it.
PROBE_ID="00000000-0000-4000-8000-$(printf '%012d' $((RANDOM * RANDOM % 999999999999)))"
WRITE_OK=1
if command -v curl >/dev/null 2>&1; then
  code=$(curl -s -m15 -o /dev/null -w "%{http_code}" -X POST "$WEAVIATE_URL/v1/objects" \
    -H 'Content-Type: application/json' \
    -d "{\"class\":\"ISMA_Quantum\",\"id\":\"$PROBE_ID\",\"properties\":{\"content\":\"disk headroom canary probe\",\"source_file\":\"/__canary__/disk_headroom.md\",\"scale\":\"search_512\",\"is_superseded\":false}}" 2>/dev/null)
  case "$code" in
    200|201) WRITE_OK=0
             curl -s -m15 -o /dev/null -X DELETE "$WEAVIATE_URL/v1/objects/ISMA_Quantum/$PROBE_ID" 2>/dev/null ;;
    *)       WRITE_OK=1 ;;
  esac
fi

# Previous state: "<last_alerted_percent> <epoch_of_last_alert>", absent when healthy.
PREV_PCT=""; PREV_TS=0
if [ -r "$STATE" ]; then
  read -r PREV_PCT PREV_TS < "$STATE" 2>/dev/null || true
  PREV_PCT="${PREV_PCT:-}"; PREV_TS="${PREV_TS:-0}"
fi
NOW=$(date +%s)

# Emit the alert and record that we did, so the next run can dedup against it.
raise() {
  echo "$(ts) $1" >> "$LOG"
  [ -n "$ALERT_CMD" ] && $ALERT_CMD "$1" >> "$LOG" 2>&1
  echo "$USED $NOW" > "$STATE" 2>/dev/null || true
  return 0
}
# Condition persists but does not warrant waking anyone again.
note() { echo "$(ts) [suppressed, no state change] $1" >> "$LOG"; }

# The store being read-only is the emergency, whatever the percentage says, and it
# is NEVER suppressed — a dedup rule that can silence an active outage is a bug.
if [ "$WRITE_OK" -ne 0 ]; then
  raise "ISMA DISK CRITICAL: Weaviate store is NOT ACCEPTING WRITES (probe failed, http=${code:-none}). Disk ${USED}% used, ${AVAIL} free on ${DATA_PATH}. Ingestion is failing SILENTLY while reads keep working."
  exit 1
fi

if [ "$USED" -ge "$THRESHOLD" ]; then
  MSG="ISMA DISK WARNING: ${USED}% used (warn at ${THRESHOLD}%, Weaviate goes READ-ONLY at 90%), ${AVAIL} free on ${DATA_PATH}. Writes still succeed (probe passed). Restore headroom before the cliff — past 90% every write fails silently while search stays green."
  if [ -z "$PREV_PCT" ]; then
    raise "$MSG"                                     # crossing into the band
  elif [ "$USED" -gt "$PREV_PCT" ]; then
    raise "WORSENING — $MSG (was ${PREV_PCT}%)"      # deteriorating
  elif [ $((NOW - PREV_TS)) -ge "$REMIND_SECS" ]; then
    raise "REMINDER — $MSG"                          # still unresolved
  else
    note "$MSG"                                      # steady: log only
  fi
  exit 1
fi

# Recovered below the threshold: clear state so the next crossing alerts again.
if [ -n "$PREV_PCT" ]; then
  echo "$(ts) RECOVERED: ${USED}% used, below the ${THRESHOLD}% threshold (was ${PREV_PCT}%)" >> "$LOG"
  rm -f "$STATE" 2>/dev/null || true
fi
exit 0
