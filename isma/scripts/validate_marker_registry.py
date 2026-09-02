#!/usr/bin/env python3
"""Validate the ephemeral-marker registry's schema and writer boundary.

check_marker_registry.sh proves `found ⊆ registered`. That is one direction only:
a STALE entry passes forever after its writer stops using it, and the registry's
`writer` column was consumed by nothing at all. This closes the other direction --
every registered marker must actually occur in the tracked file that declares it.

KNOWN LIMIT, stated rather than papered over: literal discovery cannot see a marker
that is COMPUTED at runtime (f-strings, concatenation, a value from config). No grep
closes that. The durable fix is that ephemeral writers consume this registry instead
of spelling their own markers, so the registry is the source rather than a mirror.
Until then this file bounds the claim to declared, literal markers.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ephemeral_markers  # noqa: E402


def main() -> int:
    rc = 0
    markers = getattr(ephemeral_markers, "MARKERS", None)
    if not isinstance(markers, list) or not markers:
        print("FAIL: MARKERS missing or empty", file=sys.stderr)
        return 1

    tracked = set(subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split())

    seen = set()
    for entry in markers:
        if not (isinstance(entry, tuple) and len(entry) == 4):
            print(f"FAIL: entry is not a 4-tuple: {entry!r}", file=sys.stderr)
            rc = 1
            continue
        prop, exact, prefilter, writer = entry
        if prop not in ("source_file", "authority"):
            print(f"FAIL: {exact}: unknown property {prop!r}", file=sys.stderr)
            rc = 1
        if exact in seen:
            print(f"FAIL: duplicate marker {exact!r}", file=sys.stderr)
            rc = 1
        seen.add(exact)
        if not (prefilter.startswith("*") and prefilter.endswith("*")):
            print(f"FAIL: {exact}: prefilter {prefilter!r} is not a *token* form", file=sys.stderr)
            rc = 1
        if writer not in tracked:
            print(f"FAIL: {exact}: declared writer {writer!r} is not a tracked file", file=sys.stderr)
            rc = 1
            continue
        # the other direction: a registered marker must occur in its declared writer
        if exact not in Path(writer).read_text(errors="ignore"):
            print(f"FAIL: {exact}: not found in its declared writer {writer} "
                  f"(stale entry, or the writer changed its marker)", file=sys.stderr)
            rc = 1

    if rc == 0:
        print(f"OK: {len(markers)} registered marker(s); schema valid, "
              f"each present in its declared tracked writer")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
