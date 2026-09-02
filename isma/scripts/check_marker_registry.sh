#!/usr/bin/env bash
# CI gate: every ephemeral-marker literal in production source must be registered.
#
# Without this, a new probe writer silently disappears from the residue detectors
# exactly as /__canary__/ did -- the detector list stays green while its coverage
# quietly shrinks. Registration is enforced here rather than remembered.
set -euo pipefail
cd "$(dirname "$0")/../.."
REG="isma/scripts/ephemeral_markers.tsv"
[ -f "$REG" ] || { echo "FAIL: registry missing at $REG" >&2; exit 1; }

registered=$(grep -v '^#' "$REG" | awk -F'\t' 'NF>=2 {print $2}' | sort -u)
# production source only: demo/ and benchmarks/ use their own topology
found=$(git ls-files 'isma/**/*.py' 'isma/**/*.sh' \
  | xargs grep -ohE '/__[a-z_]+__/|__[a-z_]+_fixture__' 2>/dev/null \
  | grep -v '__pycache__' | sort -u || true)

rc=0
while IFS= read -r m; do
  [ -n "$m" ] || continue
  if ! printf '%s\n' "$registered" | grep -qxF "$m"; then
    echo "FAIL: marker '$m' appears in production source but is not in $REG" >&2
    rc=1
  fi
done <<< "$found"

if [ $rc -eq 0 ]; then
  echo "OK: $(printf '%s\n' "$found" | grep -c .) marker literal(s) in production source, all registered"
fi
exit $rc
