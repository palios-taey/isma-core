#!/usr/bin/env bash
# Early-warning canary for the DISKGATE cliff.
#
# Weaviate flips the store to READ-ONLY at DISK_USE_READONLY_PERCENTAGE.
# THE 90% FIGURE IS UNVERIFIED — it is the documented default, inherited and never
# observed on this deployment. Measured 2026-08-01: the store was at 92% and STILL
# ACCEPTING WRITES. Treat the warn threshold as the actionable number and the
# read-only point as unknown until someone measures it.
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
#   * WORSENING while in the band — compared against the LAST OBSERVED reading,
#     never against an all-time high-water mark. That distinction is the whole
#     defect this rule was rewritten to fix: on 2026-08-01 a stale mark of 89 from
#     an earlier episode silenced a real climb at 87% and 88%, and the alarm only
#     woke at 92% — past the cliff it exists to warn about. An anti-silence alarm
#     that silences the approach is worse than no alarm.
#   * a WRITE-PROBE FAILURE, always, every run — the store being read-only is the
#     emergency itself and must never be suppressed
#   * one REMINDER per ISMA_DISK_REMIND_SECS (default 24h) while steady
# Recovery below the threshold clears the state, so the next crossing alerts again.
#
# THE DEAD-MAN'S SWITCH. Everything above assumes the canary RUNS. If the timer
# stops, nothing runs, nothing alerts, and the silence is indistinguishable from
# all-clear — an instrument that has stopped looking reports the same thing as one
# that looked and found nothing wrong. So the canary records a HEARTBEAT on every
# completed observation, and `--watchdog` (a separate timer) escalates when that
# heartbeat goes stale. Misconfiguration escalates immediately by the same logic:
# a canary watching nothing is down, whatever its exit code says.
set -u

THRESHOLD="${ISMA_DISK_WARN_PERCENT:-85}"
# The path to the Weaviate store. NOTE: this is a PATH, not necessarily a mount
# point — df follows it to whatever filesystem actually backs it. On Mira
# /var/spark is a directory on the root filesystem, not a separate device, so a
# warning here is a warning about ROOT, and freeing space anywhere on that
# filesystem helps equally. Do not read a warning from this canary as an
# ISMA-local problem.
# No default. /var/lib/weaviate is the path INSIDE the container, not on the host,
# so defaulting to it hands every host a path that cannot exist and an error that
# reads like a broken install instead of a missing setting. Require it explicitly.
DATA_PATH="${ISMA_WEAVIATE_DATA_PATH:-}"
WEAVIATE_URL="${WEAVIATE_URL:-http://localhost:8088}"
LOG="${ISMA_CANARY_LOG:-/tmp/disk_headroom_canary.log}"
STATE="${ISMA_CANARY_STATE:-/tmp/disk_headroom_canary.state}"
REMIND_SECS="${ISMA_DISK_REMIND_SECS:-86400}"
# How to raise an alert. Fleet deployments set this to their notifier; if unset the
# canary still logs and exits non-zero, so a supervisor or timer can act on it.
ALERT_CMD="${ISMA_DISK_ALERT_CMD:-}"

# HEARTBEAT — liveness, recorded UNCONDITIONALLY on every completed observation.
# It is deliberately NOT the alert state file: that file is deleted on RECOVERED,
# so it disappears exactly when the disk is healthy — which is precisely when a
# silent canary is indistinguishable from a quiet one. Liveness has to be recorded
# on the healthy path or it does not answer the question it exists for.
# Not /tmp by default: /tmp is cleared on reboot, and a heartbeat that vanishes on
# every boot manufactures the alarm it exists to raise.
HEARTBEAT="${ISMA_CANARY_HEARTBEAT:-${XDG_STATE_HOME:-$HOME/.local/state}/isma/disk_canary.heartbeat}"
# Watchdog tolerance. The shipped timer fires every 15 min; the default allows two
# missed runs before escalating, so a single slow or skipped cycle is not an alarm.
MAX_AGE="${ISMA_CANARY_MAX_AGE_SECS:-2400}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# ---------------------------------------------------------------------------
# WATCHDOG MODE — the dead-man's switch.
#
# A canary cannot report its own death: if the timer stops, the script does not
# run, and nothing inside it can alert. Silence from a detector then reads exactly
# like all-clear. This mode is the external observer — it never touches the disk or
# the store, it only asks "did the canary actually look, recently?"
#
# It is deliberately trivial (read a file, compare an integer) because it is the
# last link in the chain and its own failure is unwatched. THAT LIMIT IS REAL AND
# STATED: nothing watches the watchdog. The turtles stop here, on purpose, and the
# honest mitigation is that this path has no network, no store, and no parsing.
if [ "${1:-}" = "--watchdog" ]; then
  if [ ! -r "$HEARTBEAT" ]; then
    MSG="ISMA CANARY DEAD: no heartbeat at ${HEARTBEAT}. The disk canary has not completed a run — the DISKGATE detector is not looking. Read-only would now arrive silently: search stays green while every write fails."
  else
    read -r HB_TS _ < "$HEARTBEAT" 2>/dev/null || HB_TS=0
    AGE=$(( $(date +%s) - ${HB_TS:-0} ))
    if [ "$AGE" -gt "$MAX_AGE" ]; then
      MSG="ISMA CANARY STALE: last completed run ${AGE}s ago (limit ${MAX_AGE}s). The disk canary has stopped reporting — treat the DISKGATE detector as DOWN, not as all-clear."
    else
      exit 0                                   # canary is alive; say nothing
    fi
  fi
  echo "$(ts) $MSG" >> "$LOG"
  [ -n "$ALERT_CMD" ] && $ALERT_CMD "$MSG" >> "$LOG" 2>&1
  exit 1
fi
# ---------------------------------------------------------------------------

# Fail loud means loud TO A HUMAN, not just to a log nobody opens. A canary that
# is misconfigured is a canary that is not watching, which is the same condition
# the watchdog exists to escalate — so it escalates here too, immediately, rather
# than waiting for the heartbeat to age out.
fatal() {
  echo "$(ts) $1" >> "$LOG"
  [ -n "$ALERT_CMD" ] && $ALERT_CMD "$1" >> "$LOG" 2>&1
  exit 2
}

if [ -z "$DATA_PATH" ]; then
  echo "$(ts)   Set ISMA_WEAVIATE_DATA_PATH to the HOST path holding the Weaviate data. To find it:" >> "$LOG"
  echo "$(ts)     docker inspect <weaviate-container> --format '{{range .Mounts}}{{.Source}}{{end}}'" >> "$LOG"
  echo "$(ts)   (that is the host side of the container's /var/lib/weaviate mount)" >> "$LOG"
  fatal "ISMA CANARY MISCONFIGURED: ISMA_WEAVIATE_DATA_PATH is not set — the DISKGATE detector is watching nothing. See $LOG for the setting."
fi

if [ ! -e "$DATA_PATH" ]; then
  fatal "ISMA CANARY MISCONFIGURED: ISMA_WEAVIATE_DATA_PATH '$DATA_PATH' does not exist — the DISKGATE detector is watching nothing."
fi

USED=$(df --output=pcent "$DATA_PATH" 2>/dev/null | tail -1 | tr -dc '0-9')
# REPLAY SEAM. Overrides only the OBSERVED PERCENTAGE so a reviewer can replay a
# real incident sequence and confirm the alerting decision, rather than taking my
# word that the logic is fixed. It is announced in the log on every use, it cannot
# suppress an alert (a simulated value still runs the full decision path, and the
# write-probe branch is untouched), and it is never set in the shipped unit.
if [ -n "${ISMA_SIMULATE_USED:-}" ]; then
  echo "$(ts) [REPLAY] simulated reading ${ISMA_SIMULATE_USED}% (real disk is ${USED}%)" >> "$LOG"
  USED="$ISMA_SIMULATE_USED"
fi
AVAIL=$(df -h --output=avail "$DATA_PATH" 2>/dev/null | tail -1 | tr -d ' ')
if [ -z "${USED:-}" ]; then
  fatal "ISMA CANARY BLIND: could not read disk usage for $DATA_PATH — the DISKGATE detector is not measuring anything."
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

# Heartbeat: we read the disk AND ran the probe, so this run did its job. Written
# on EVERY outcome from here down — healthy, warning, or read-only — because the
# watchdog is asking "is the detector looking?", not "is the news good?". It is
# NOT written above this line: a config error means the canary never observed
# anything, and a heartbeat then would certify a canary that is watching nothing.
mkdir -p "$(dirname "$HEARTBEAT")" 2>/dev/null || true
echo "$(date +%s) ${USED} write_ok=${WRITE_OK}" > "$HEARTBEAT" 2>/dev/null || true

# State: "<last_observed_pct> <epoch_of_last_OBSERVATION> <epoch_of_last_ALERT> <last_alerted_pct>".
# LAST_SEEN and LAST_ALERTED are deliberately SEPARATE. Comparing a new reading
# against the worst-ever value makes any re-climb below that peak invisible;
# comparing against what we saw on the previous run makes every step upward
# visible, which is what "worsening" has to mean for an approach to a cliff.
#
# OBS_TS and ALERT_TS are also deliberately separate, and conflating them was a
# real defect (fixed 2026-08-04). They answer different questions:
#   OBS_TS   — "when did we last LOOK at the disk?"  -> the staleness guard
#   ALERT_TS — "when did we last TELL anyone?"       -> the reminder interval
# A single field cannot mean both: the reminder needs it frozen while steady, the
# staleness guard needs it advanced on every run. Frozen won it, so the staleness
# guard saw a healthy 15-minute cadence as an hour-old observation, re-armed every
# hour, and emitted a "crossing into the band" alert at a completely unchanged
# reading. Because STALE_SECS (1h) < REMIND_SECS (24h), the reminder branch below
# was also UNREACHABLE — the guard blanked LAST_SEEN before it could ever be
# tested. Observed in production: 116 warnings, 46 alerts sent, 0 REMINDER.
LAST_SEEN=""; OBS_TS=0; ALERT_TS=0; LAST_ALERTED=""
if [ -r "$STATE" ]; then
  read -r LAST_SEEN OBS_TS ALERT_TS LAST_ALERTED < "$STATE" 2>/dev/null || true
  LAST_SEEN="${LAST_SEEN:-}"; OBS_TS="${OBS_TS:-0}"
  if [ -z "$LAST_ALERTED" ]; then
    # Legacy 3-field state "<pct> <ts> <alerted_pct>": its single timestamp was
    # the last ALERT, so seed both from it. Self-heals to 4 fields on next write.
    LAST_ALERTED="${ALERT_TS:-$LAST_SEEN}"; ALERT_TS="$OBS_TS"
  fi
  ALERT_TS="${ALERT_TS:-0}"; LAST_ALERTED="${LAST_ALERTED:-$LAST_SEEN}"
fi
NOW=$(date +%s)

# STATE EXPIRY. A "last observed" reading is only meaningful if it was observed
# RECENTLY. In the 2026-08-01 incident the stored value was 89 from an episode
# hours earlier; the disk had since dropped to 80 and climbed back, but the timer
# had not run during the low period so RECOVERED never fired and the stale 89
# silenced a genuine climb at 87%. A stale entry must not be allowed to suppress
# a current reading — if we have not seen the disk recently, this reading is a
# fresh crossing, not a continuation.
# This reads OBS_TS — "when did we last look" — NOT the last-alert time. A steady
# condition that is being observed every 15 minutes is not stale, however long ago
# we last said something about it.
STALE_SECS="${ISMA_CANARY_STALE_SECS:-3600}"
if [ -n "$LAST_SEEN" ] && [ $((NOW - OBS_TS)) -gt "$STALE_SECS" ]; then
  echo "$(ts) state is stale (${LAST_SEEN}% observed $((NOW - OBS_TS))s ago, limit ${STALE_SECS}s) — treating this reading as a fresh crossing" >> "$LOG"
  LAST_SEEN=""; LAST_ALERTED=""
fi

# Emit the alert and record that we did, so the next run can dedup against it.
# Both clocks advance: we looked, and we spoke.
raise() {
  echo "$(ts) $1" >> "$LOG"
  [ -n "$ALERT_CMD" ] && $ALERT_CMD "$1" >> "$LOG" 2>&1
  echo "$USED $NOW $NOW $USED" > "$STATE" 2>/dev/null || true
  return 0
}
# Condition persists unchanged. Record what we SAW so the next run compares
# against reality rather than against the last thing that happened to alert.
# OBS_TS advances (we looked); ALERT_TS is preserved (we stayed quiet), so the
# reminder interval keeps counting from the last thing conductor actually saw.
note() {
  echo "$(ts) [suppressed, no state change] $1" >> "$LOG"
  echo "$USED $NOW $ALERT_TS ${LAST_ALERTED:-$USED}" > "$STATE" 2>/dev/null || true
}

# The store being read-only is the emergency, whatever the percentage says, and it
# is NEVER suppressed — a dedup rule that can silence an active outage is a bug.
if [ "$WRITE_OK" -ne 0 ]; then
  raise "ISMA DISK CRITICAL: Weaviate store is NOT ACCEPTING WRITES (probe failed, http=${code:-none}). Disk ${USED}% used, ${AVAIL} free on the filesystem backing ${DATA_PATH}. Ingestion is failing SILENTLY while reads keep working."
  exit 1
fi

if [ "$USED" -ge "$THRESHOLD" ]; then
  MSG="ISMA DISK WARNING: ${USED}% used (warn at ${THRESHOLD}%), ${AVAIL} free on the filesystem backing ${DATA_PATH}. Writes still succeed — this alert is a HEADROOM warning, not a cliff prediction. The read-only threshold on this deployment is UNKNOWN: DISK_USE_READONLY_PERCENTAGE is NOT SET on the container (verified 2026-08-02, Weaviate 1.36.2), and the store was still accepting writes at 92% on 2026-08-01, which does not match the documented default of 90%. Do not plan against any specific number. The write probe in this canary is what actually detects read-only, every run, whatever the threshold turns out to be. Past whatever it is: every write fails SILENTLY while search stays green."
  if [ -z "$LAST_SEEN" ]; then
    raise "$MSG"                                              # crossing into the band
  elif [ "$USED" -gt "$LAST_SEEN" ]; then
    # Compared against the LAST OBSERVED reading, not the worst-ever. Every step
    # upward alerts, including a re-climb that is still below a previous peak.
    raise "WORSENING — $MSG (was ${LAST_SEEN}% on the previous check)"
  elif [ $((NOW - ALERT_TS)) -ge "$REMIND_SECS" ]; then
    raise "REMINDER — $MSG"                                   # still unresolved
  else
    note "$MSG"                                               # steady: log only
  fi
  exit 1
fi

# Recovered below the threshold: clear state so the next crossing alerts again.
if [ -n "$LAST_SEEN" ]; then
  echo "$(ts) RECOVERED: ${USED}% used, below the ${THRESHOLD}% threshold (was ${LAST_SEEN}%)" >> "$LOG"
  rm -f "$STATE" 2>/dev/null || true
fi
exit 0
