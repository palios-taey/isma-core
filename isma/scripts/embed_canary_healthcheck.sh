#!/usr/bin/env bash
# Fail-loud canary for the isma-embedding server (:8089). /health does NOT exercise the model,
# so a wedged forward path (e.g. corrupted torch.compile cudagraph) returns 200 on /health while
# every /v1/embeddings returns 500 — silent staleness. This canary embeds a real string and
# restarts the --user unit if the EMBED PATH (not just /health) is broken.
#
# 2026-09-08 — THIS CANARY WAS THE OUTAGE. Root-caused by infra-codex from the service journal:
# it restarted isma-embedding ~3x/hour for hours. The loop is self-sustaining and every piece of
# it was in these 21 lines:
#
#   * The 20s probe is SHORTER than a legitimate ~25s model load, and stopping the unit can block
#     ~90s. So a probe that lands during startup is GUARANTEED to fail — the canary then kills a
#     server that was merely still loading, and the next 5-minute tick lands in the fresh load and
#     kills it again. A health check whose timeout is shorter than the startup it must tolerate
#     cannot distinguish "wedged" from "starting", and answers "wedged" every time.
#   * It restarted on the FIRST failed probe with no cooldown, so nothing damped the cycle.
#   * It reused one fixed /tmp response file. On a failed curl the file kept its PREVIOUS
#     SUCCESSFUL body, so the failure line reported "http=000 dim=4096" — a diagnostic that
#     contradicts itself and lies in the direction of looking healthy.
#   * It had no alert path at all: it cycled a GPU-resident service every five minutes and told
#     nobody, exactly the class of silence this repo has spent a week removing.
#
# The collateral was not confined to embeddings: md-corpus ingest passes that landed in a restart
# window failed, and the disk canary's write probe timed out and paged a human with
# "ISMA WRITE ENDPOINT UNREACHABLE".
set -u

URL="${ISMA_EMBED_CANARY_URL:-http://localhost:8089/v1/embeddings}"
MODEL="Qwen/Qwen3-Embedding-8B"
UNIT="${ISMA_EMBED_UNIT:-isma-embedding.service}"
LOG="${ISMA_CANARY_LOG:-/tmp/embed_canary.log}"
STATE="${ISMA_EMBED_CANARY_STATE:-/tmp/embed_canary_restart.state}"
# Must exceed stop-time + model-load. Observed: ~25s load, stop can block ~90s.
GRACE="${ISMA_EMBED_CANARY_STARTUP_GRACE:-180}"
# A restart that did not help must not be retried every 5 minutes forever.
COOLDOWN="${ISMA_EMBED_CANARY_COOLDOWN:-1800}"
ALERT_CMD="${ISMA_EMBED_ALERT_CMD:-}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(ts) $*" >> "$LOG"; }
raise() { log "$*"; [ -n "$ALERT_CMD" ] && $ALERT_CMD "$*" >> "$LOG" 2>&1 || true; }

# Unique per run, always removed: a stale body must never be reported as this run's result.
BODY="$(mktemp -t embed_canary_resp.XXXXXX)"
trap 'rm -f "$BODY"' EXIT

resp=$(curl -s -m20 -o "$BODY" -w "%{http_code}" -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -d "{\"input\":[\"embed canary healthcheck\"],\"model\":\"$MODEL\"}" 2>/dev/null)
dim=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))['data'][0]['embedding']))" "$BODY" 2>/dev/null)

if [ "$resp" = "200" ] && [ "${dim:-0}" -ge 1024 ]; then
  if [ -f "$STATE" ]; then
    raise "RECOVERED: isma-embedding embed path is answering again (http=200, dim=${dim})."
    rm -f "$STATE" 2>/dev/null || true
  fi
  exit 0
fi

NOW=$(date +%s)

# Is it merely STARTING? Loading the model legitimately outlasts the probe timeout.
SUB=$(systemctl --user show "$UNIT" -p SubState --value 2>/dev/null)
START=$(systemctl --user show "$UNIT" -p ExecMainStartTimestampMonotonic --value 2>/dev/null)
UPTIME_S=$(awk '{print int($1)}' /proc/uptime 2>/dev/null)
AGE=$(( UPTIME_S - ${START:-0}/1000000 ))
if [ "$SUB" = "start" ] || [ "$SUB" = "activating" ] || { [ -n "${START:-}" ] && [ "$AGE" -lt "$GRACE" ]; }; then
  log "probe failed (http=$resp dim=${dim:-none}) but $UNIT started ${AGE}s ago (grace ${GRACE}s) — still loading, NOT restarting"
  exit 0
fi

# Restarted recently and it did not stick: escalate instead of looping.
LAST=0; [ -r "$STATE" ] && read -r LAST _ < "$STATE" 2>/dev/null || LAST=0
if [ "${LAST:-0}" -gt 0 ] && [ $((NOW - LAST)) -lt "$COOLDOWN" ]; then
  raise "ISMA EMBED CANARY: embed path still failing (http=$resp dim=${dim:-none}) $((NOW - LAST))s after a restart. NOT restarting again — cooldown ${COOLDOWN}s. A restart is not fixing this; it needs a human."
  exit 1
fi

raise "ISMA EMBED CANARY: embed path dead (http=$resp dim=${dim:-none}) — restarting $UNIT."
systemctl --user restart "$UNIT" >> "$LOG" 2>&1
printf '%s restart\n' "$NOW" > "$STATE" 2>/dev/null || true
log "restart issued"
exit 1
