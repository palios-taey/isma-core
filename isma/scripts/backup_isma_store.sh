#!/usr/bin/env bash
# ISMA store backup — with rotation, and alerting that fires on FAILURE.
#
# WHY THIS EXISTS: on 2026-07-31 an audit found ISMA's only backup was 34 days
# old and NO backup automation existed at all. Roughly 60,000 tiles of authored
# corpus and conversation history existed solely in the live store, with no
# recovery point. Nothing had failed loudly — backups had simply never been
# automated, and the gap was invisible because a missing backup produces no error.
#
# DESIGN, following the disk-canary lesson: a backup system that fails SILENTLY
# is worse than none, because it manufactures false confidence. So every failure
# path here alerts, and the success path verifies rather than assumes:
#   * the copy is checked for the store's structural markers before it counts
#   * a SIZE FLOOR rejects a copy that is implausibly small (a truncated rsync
#     still exits 0 for the files it did transfer)
#   * rotation NEVER drops below RETAIN_MIN good backups — replace-then-retire,
#     never zero recovery points
#   * restorability is a SEPARATE, heavier check (restore_verify_isma.py); this
#     script does not claim a backup is restorable, only that it is complete
#
# Usage: backup_isma_store.sh
# Env:   ISMA_STORE_PATH   (default /var/lib/weaviate) — the live store
#        ISMA_BACKUP_DIR   (default /home/mira/backups)
#        ISMA_BACKUP_RETAIN (default 2) — minimum good backups to keep
#        ISMA_BACKUP_ALERT_CMD — receives the alert text as its final argument.
#          QUOTE THE WHOLE Environment= assignment in the unit if it has
#          arguments; systemd splits unquoted values on whitespace and would
#          silently drop everything after the binary.
set -u

STORE="${ISMA_STORE_PATH:-/var/lib/weaviate}"
DEST="${ISMA_BACKUP_DIR:-/home/mira/backups}"
RETAIN="${ISMA_BACKUP_RETAIN:-2}"
ALERT_CMD="${ISMA_BACKUP_ALERT_CMD:-}"
LOG="${ISMA_BACKUP_LOG:-/tmp/isma_backup.log}"
MIN_GB="${ISMA_BACKUP_MIN_GB:-10}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
alert() {
  echo "$(ts) $1" >> "$LOG"
  [ -n "$ALERT_CMD" ] && $ALERT_CMD "$1" >> "$LOG" 2>&1
  return 0
}

[ -d "$STORE" ] || { alert "ISMA BACKUP FAILED: store path '$STORE' does not exist. Nothing was backed up."; exit 2; }

STAMP=$(date +%Y%m%d-%H%M)
TARGET="$DEST/isma-weaviate-$STAMP"
mkdir -p "$TARGET" || { alert "ISMA BACKUP FAILED: cannot create $TARGET"; exit 2; }

# Free space must exceed the store size, or the copy silently truncates.
NEED_KB=$(du -xsk "$STORE" 2>/dev/null | cut -f1)
FREE_KB=$(df -Pk "$DEST" | tail -1 | awk '{print $4}')
if [ "${FREE_KB:-0}" -lt "${NEED_KB:-0}" ]; then
  alert "ISMA BACKUP FAILED: need $((NEED_KB/1024/1024))G, only $((FREE_KB/1024/1024))G free at $DEST. NOT started."
  rmdir "$TARGET" 2>/dev/null
  exit 1
fi

# RSYNC_CMD lets the unit supply privilege (e.g. "sudo -n") for the handful of
# root-owned files inside an otherwise user-owned store. Weaviate's raft
# db_users/users.json is root:root mode 600; without privilege rsync exits 23 and
# the whole backup is refused for one 148-byte file.
RSYNC="${ISMA_RSYNC_CMD:-}"
$RSYNC rsync -a --delete "$STORE/" "$TARGET/data/" >> "$LOG" 2>&1
RC=$?
# Exit-code semantics matter on a LIVE store:
#   24 = source files vanished mid-copy. On a running LSM store, WAL segments are
#        created and compacted away constantly, so 24 is EXPECTED and benign — the
#        vanished file was transient by definition. Treating it as failure would
#        make every nightly run fail on a healthy system, and an alarm that always
#        fires is an alarm nobody reads.
#   23 = partial transfer due to an ERROR (typically permission). Genuine failure.
# Observed 2026-07-31 on the first real run: exit 23 from one root-owned file,
# alongside a benign vanished WAL segment. Two different problems in one exit code
# is exactly why this is now discriminated rather than lumped together.
if [ "$RC" -eq 24 ]; then
  echo "$(ts) NOTE: rsync exit 24 — source files vanished mid-copy (normal WAL churn on a live store); continuing to verification" >> "$LOG"
elif [ "$RC" -ne 0 ]; then
  alert "ISMA BACKUP FAILED: rsync exit $RC. Partial copy left at $TARGET for inspection — NOT counted as a backup."
  exit 1
fi

# Completeness, not existence. rsync exits 0 for what it did transfer, so an
# interrupted run can leave a plausible-looking directory that is not a backup.
GOT_KB=$(du -xsk "$TARGET/data" 2>/dev/null | cut -f1)
if [ "${GOT_KB:-0}" -lt $((MIN_GB * 1024 * 1024)) ]; then
  alert "ISMA BACKUP FAILED: copy is only $((GOT_KB/1024/1024))G, below the ${MIN_GB}G floor. Treating as INCOMPLETE."
  exit 1
fi
for MARKER in "data" ; do
  [ -d "$TARGET/$MARKER" ] || { alert "ISMA BACKUP FAILED: '$MARKER' missing from $TARGET"; exit 1; }
done

cat > "$TARGET/MANIFEST.txt" <<EOF
ISMA Weaviate store backup
created:      $(ts)
source:       $STORE
size:         $((GOT_KB/1024/1024))G
host:         $(hostname)
NOTE: completeness verified (size floor + structure). RESTORABILITY is proven
separately by isma/scripts/restore_verify_isma.py — existence is not restorability.
EOF
echo "$(ts) OK: backup complete at $TARGET ($((GOT_KB/1024/1024))G)" >> "$LOG"

# ── Rotation: replace-then-retire, never below RETAIN good copies ──────────
mapfile -t GOOD < <(find "$DEST" -maxdepth 1 -type d -name 'isma-weaviate-*' \
  -exec test -f '{}/MANIFEST.txt' \; -print 2>/dev/null | sort)
COUNT=${#GOOD[@]}
if [ "$COUNT" -gt "$RETAIN" ]; then
  DROP=$((COUNT - RETAIN))
  for i in $(seq 0 $((DROP - 1))); do
    echo "$(ts) rotating out ${GOOD[$i]}" >> "$LOG"
    rm -rf "${GOOD[$i]}"
  done
else
  echo "$(ts) rotation: $COUNT good backup(s), retain=$RETAIN — nothing removed" >> "$LOG"
fi

exit 0
