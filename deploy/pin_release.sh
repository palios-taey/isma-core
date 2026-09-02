#!/usr/bin/env bash
#
# Cut an immutable ISMA release at a git ref and repoint release-pinned systemd
# units to it.
#
# WHY THIS EXISTS. Two ISMA units (isma-disk-canary, isma-canary-watchdog) run
# from an immutable copy of the repo at a fixed SHA, NOT from the working tree.
# That is deliberate -- a detector that changes under you is not a detector --
# but it means `git pull` does not deploy them. Before this script the repoint
# was done by hand, once, and nothing recorded how. That is the situation this
# file removes.
#
# THE TRAP THIS SCRIPT EXISTS TO AVOID. The pin is a systemd drop-in that resets
# ExecStart and repoints it. That drop-in ALSO carries Environment settings that
# are not in the unit fragment -- on isma-canary-watchdog, ISMA_DISK_ALERT_CMD
# lives ONLY in the drop-in. Writing a fresh ExecStart-only drop-in therefore
# silently disables that unit's alerting: it keeps running, keeps exiting 0, and
# tells nobody anything. A dead-man's switch that has gone mute is worse than no
# dead-man's switch, because it still looks present.
#
# So this script never authors a drop-in from a template. It carries every
# non-ExecStart line of the existing drop-in forward verbatim, derives the new
# ExecStart from the old one by path substitution (which preserves flags such as
# --watchdog automatically), and then MECHANICALLY VERIFIES that the effective
# environment is byte-identical before and after. If it moved, the run fails.
#
# Usage:
#   deploy/pin_release.sh --check              # read-only; print what would change
#   deploy/pin_release.sh                      # cut + repoint + verify
#   deploy/pin_release.sh --ref <git-ref>      # default: origin/main
#
# Env overrides (all fail loud rather than guessing):
#   ISMA_RELEASES_ROOT  default $HOME/releases/isma-core
#   ISMA_PINNED_UNITS   default "isma-disk-canary isma-canary-watchdog"
#
set -euo pipefail

REF="origin/main"
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check)   CHECK=1 ;;
    --ref)     shift; REF="${1:-}" ;;
    --ref=*)   REF="${arg#*=}" ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
  esac
done

RELEASES_ROOT="${ISMA_RELEASES_ROOT:-$HOME/releases/isma-core}"
UNITS="${ISMA_PINNED_UNITS:-isma-disk-canary isma-canary-watchdog}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

[ -n "$REF" ] || die "--ref given with no value"

step "1. resolve the ref"
git -C "$REPO_ROOT" fetch --quiet origin 2>/dev/null || say "   (fetch skipped/failed; using local refs)"
SHA="$(git -C "$REPO_ROOT" rev-parse "$REF^{commit}")" || die "cannot resolve ref '$REF'"
SHORT="${SHA:0:8}"
say "   $REF -> $SHA"

RELEASE_DIR="$RELEASES_ROOT/$SHA"
say "   release dir: $RELEASE_DIR"

step "2. cut the immutable release (idempotent)"
if [ -d "$RELEASE_DIR" ]; then
  say "   already exists -- reusing, not rewriting"
  [ -n "$(ls -A "$RELEASE_DIR" 2>/dev/null)" ] || die "release dir exists but is EMPTY: $RELEASE_DIR"
elif [ "$CHECK" = 1 ]; then
  say "   [check] would create $RELEASE_DIR from git archive $SHA"
else
  TMP="$(mktemp -d "${RELEASE_DIR}.tmp.XXXXXX")"
  # git archive takes the TREE AT THE SHA -- never the working copy. Copying the
  # working tree instead would carry uncommitted state into an "immutable" release.
  git -C "$REPO_ROOT" archive --format=tar "$SHA" | tar -x -C "$TMP" \
    || die "git archive/extract failed"
  [ -n "$(ls -A "$TMP")" ] || die "extracted release is empty"
  mv "$TMP" "$RELEASE_DIR"
  # read-only AFTER extraction is verified, never before
  chmod -R a-w "$RELEASE_DIR"
  chmod 500 "$RELEASE_DIR"
  say "   created and set read-only"
fi

step "3. plan the repoint (carrying non-ExecStart drop-in lines forward)"
declare -A NEW_EXEC PLAN_FILE OLD_DROPIN
for u in $UNITS; do
  unit="$u.service"
  systemctl --user cat "$unit" >/dev/null 2>&1 || die "unknown unit: $unit"

  old_exec="$(systemctl --user show "$unit" -p ExecStart --value \
              | sed -n 's/.*argv\[\]=\([^;]*\);.*/\1/p')"
  [ -n "$old_exec" ] || die "$unit: could not read current ExecStart"

  # Substitute ANY releases-root/<sha> prefix, or the working tree, with the new
  # release. Deriving from the old command preserves trailing flags (--watchdog).
  new_exec="$(printf '%s' "$old_exec" \
    | sed -E "s#$RELEASES_ROOT/[0-9a-f]{40}#$RELEASE_DIR#g" \
    | sed -E "s#$REPO_ROOT#$RELEASE_DIR#g")"

  case "$new_exec" in
    *"$RELEASE_DIR"*) : ;;
    *) die "$unit: could not rewrite ExecStart to the release. old was: $old_exec" ;;
  esac

  # The script the unit will actually run must exist. In check mode the release
  # has not been cut yet, so validate against the git tree at the SHA instead of
  # against a directory this run deliberately did not create -- otherwise --check
  # fails on its own read-only-ness, which would train people to skip it.
  target="$(printf '%s' "$new_exec" | tr ' ' '\n' | grep -E '\.(sh|py)$' | head -1)"
  [ -n "$target" ] || die "$unit: no .sh/.py target found in ExecStart: $new_exec"
  if [ -d "$RELEASE_DIR" ]; then
    [ -f "$target" ] || die "$unit: target missing in release: $target"
  else
    rel="${target#$RELEASE_DIR/}"
    git -C "$REPO_ROOT" cat-file -e "$SHA:$rel" 2>/dev/null \
      || die "$unit: target '$rel' does not exist in tree $SHORT -- the release would not contain it"
    say "     target verified in tree $SHORT: $rel"
  fi

  NEW_EXEC[$u]="$new_exec"
  OLD_DROPIN[$u]="$(systemctl --user show "$unit" -p DropInPaths --value \
                    | tr ' ' '\n' | grep -E '/[0-9]+-release-[^/]*\.conf$' | head -1 || true)"
  PLAN_FILE[$u]="$UNIT_DIR/$unit.d/20-release-$SHORT.conf"

  say "   $unit"
  say "     old ExecStart: $old_exec"
  say "     new ExecStart: $new_exec"
  say "     carrying fwd : ${OLD_DROPIN[$u]:-<none>}"
done

step "4. capture the environment invariant BEFORE"
declare -A ENV_BEFORE
for u in $UNITS; do
  # raw string on purpose: values contain spaces (ISMA_DISK_ALERT_CMD), so any
  # space-splitting comparison silently drops them
  ENV_BEFORE[$u]="$(systemctl --user show "$u.service" -p Environment --value)"
done

if [ "$CHECK" = 1 ]; then
  step "CHECK MODE -- nothing was written"
  for u in $UNITS; do say "   would write ${PLAN_FILE[$u]}"; done
  exit 0
fi

step "5. write the new drop-ins"
for u in $UNITS; do
  unit="$u.service"
  mkdir -p "$UNIT_DIR/$unit.d"
  {
    printf '# Generated by deploy/pin_release.sh -- do not hand-edit.\n'
    printf '# Pins %s to the immutable release at %s\n' "$unit" "$SHA"
    printf '[Service]\n'
    printf 'ExecStart=\n'
    printf 'ExecStart=%s\n' "${NEW_EXEC[$u]}"
    if [ -n "${OLD_DROPIN[$u]}" ] && [ -r "${OLD_DROPIN[$u]}" ]; then
      printf '# carried forward verbatim from %s:\n' "$(basename "${OLD_DROPIN[$u]}")"
      grep -vE '^\s*(ExecStart=|\[Service\]|#)' "${OLD_DROPIN[$u]}" | sed '/^\s*$/d'
    fi
  } > "${PLAN_FILE[$u]}"
  say "   wrote ${PLAN_FILE[$u]}"

  # retire older release drop-ins so exactly one pin is in effect
  for old in "$UNIT_DIR/$unit.d"/*-release-*.conf; do
    [ -e "$old" ] || continue
    [ "$old" = "${PLAN_FILE[$u]}" ] && continue
    mv "$old" "$old.superseded-$SHORT"
    say "   retired $(basename "$old")"
  done
done

step "6. reload"
systemctl --user daemon-reload

step "7. VERIFY -- receipt"
rc=0
for u in $UNITS; do
  unit="$u.service"
  got="$(systemctl --user show "$unit" -p ExecStart --value \
         | sed -n 's/.*argv\[\]=\([^;]*\);.*/\1/p')"
  env_after="$(systemctl --user show "$unit" -p Environment --value)"

  say "   $unit"
  say "     ExecStart -> $got"
  case "$got" in
    *"$RELEASE_DIR"*) say "     [OK] points into release $SHORT" ;;
    *) say "     [FAIL] does NOT point into the new release"; rc=1 ;;
  esac
  if [ "$env_after" = "${ENV_BEFORE[$u]}" ]; then
    say "     [OK] environment byte-identical to before"
  else
    say "     [FAIL] ENVIRONMENT CHANGED -- alerting may be silently disabled"
    say "       before: ${ENV_BEFORE[$u]}"
    say "       after : $env_after"
    rc=1
  fi
done

[ "$rc" = 0 ] || die "verification failed -- see [FAIL] above. Drop-ins are on disk; the previous ones were renamed .superseded-$SHORT and can be restored."

step "DONE"
say "   release: $SHA"
say "   units repointed and verified: $UNITS"
say "   Next: run each unit once and confirm its output, e.g."
say "     systemctl --user start isma-disk-canary.service && tail -5 /tmp/disk_headroom_canary.log"
