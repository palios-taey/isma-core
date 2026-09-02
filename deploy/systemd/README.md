# systemd units

Unit files for the ISMA background jobs, committed so the running schedule is a
reproducible artifact rather than undocumented local state.

```bash
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now isma-disk-canary.timer
systemctl --user enable --now isma-canary-watchdog.timer   # enable BOTH — see below
systemctl --user list-timers | grep isma      # verify it is scheduled
```

## isma-disk-canary

Warns at 85% disk on the volume holding the Weaviate store, five points before
Weaviate flips to READ-ONLY at a disk threshold whose real value is UNVERIFIED — see the note below. Past that line **reads keep working perfectly
while every write silently fails** — search looks healthy while the corpus stops
growing, and nothing on the read path reveals it.

The canary reports; it never clears anything. What to reclaim is a judgment call —
blind clearing of a shared model cache can break a service restart.

It also **probes writability** with a real object write+delete rather than inferring
it from the percentage. Usage predicts the cliff; only a write proves which side of
it you are on. Exit codes: `0` healthy · `1` warning or store read-only · `2`
misconfigured (e.g. watching a path that does not exist).

Set `ISMA_DISK_ALERT_CMD` to route alerts to your notifier; without it the canary
still logs and exits non-zero.

**Quote the whole assignment if your command takes arguments.** systemd splits an
unquoted `Environment=` line on whitespace, so this silently drops everything after
the binary:

```ini
Environment=ISMA_DISK_ALERT_CMD=/usr/local/bin/mynotify --from me ops-channel    # BROKEN
Environment="ISMA_DISK_ALERT_CMD=/usr/local/bin/mynotify --from me ops-channel"  # correct
```

The broken form fails in the worst way: the canary detects correctly and invokes the
notifier, the notifier rejects the call for missing arguments, and the alert never
arrives. Verify yours by running the unit once and confirming the alert actually
landed — not merely that the unit ran.

## isma-canary-watchdog — the dead-man's switch

**The canary alone cannot tell you it has died.** If its timer stops, nothing runs,
so nothing alerts — and an instrument that has stopped looking produces exactly the
same silence as one that looked and found nothing wrong. Read-only would then arrive
completely unannounced, which is the failure the canary exists to prevent.

So the canary writes a **heartbeat on every completed observation** — including the
healthy path, which is the whole point: the alert state file is deleted on recovery,
so it vanishes precisely when silence becomes ambiguous. This watchdog reads that
heartbeat hourly and escalates when it goes stale:

| condition | verdict |
|---|---|
| heartbeat fresh | silent, exit `0` |
| heartbeat older than `ISMA_CANARY_MAX_AGE_SECS` (default 2400s) | `ISMA CANARY STALE`, exit `1` |
| no heartbeat file at all | `ISMA CANARY DEAD`, exit `1` |

`ISMA_CANARY_MAX_AGE_SECS` must **exceed the canary's timer interval** or the
watchdog alarms on an ordinary gap between runs. The default allows two missed
15-minute runs plus slack.

Misconfiguration escalates through the same reasoning: a canary pointed at a path
that does not exist is not watching anything, so exit `2` now raises through
`ISMA_DISK_ALERT_CMD` rather than only writing to a log nobody opens.

**Two stated limits, so neither reads as an oversight:**

- **Nothing watches the watchdog.** The regress stops here deliberately. Its
  mitigation is that it touches no disk, no store and no network — it reads one
  file and compares one integer, so it is small enough to be obviously correct.
- **It does not de-duplicate.** A dead detector alerts every hour until it is fixed.
  That is intentional and is *not* the alert-fatigue defect fixed in the canary's own
  dedup logic: a steady 85% is a known, accepted condition, whereas a down detector
  is an unresolved outage. Do not "fix" this by adding suppression.

Both units fall back to the **same in-script defaults** for the heartbeat and log
paths, so they cannot drift apart. If you override either path, override it in
**both units** — otherwise the watchdog reads a file the canary never writes and
reports a perfectly healthy canary as dead, forever.

## Release pinning — why `git pull` does not deploy these two units

`isma-disk-canary` and `isma-canary-watchdog` do **not** run from the working tree. They run from an
immutable copy of the repo at a fixed SHA under `~/releases/isma-core/<sha>/`, selected by a systemd
drop-in that resets `ExecStart` and repoints it. That is deliberate: a detector that changes
underneath you is not a detector. The cost is that **merging a fix to these scripts deploys nothing**
until the release is re-cut and the units repointed.

This bit once: a probe-attribution fix sat merged and inert for a week while the old detector kept
emitting the very message the fix removed. "PR merged, gates green" is not a production observation.

Use `deploy/pin_release.sh`. Do not hand-edit the drop-ins.

```bash
deploy/pin_release.sh --check     # read-only: resolve the ref, show the exact ExecStart rewrite
deploy/pin_release.sh             # cut the release, repoint, reload, verify
```

**Two things it does that a hand-written drop-in gets wrong.**

1. **It carries the existing drop-in's non-`ExecStart` lines forward verbatim.** Those lines are not
   decoration — on `isma-canary-watchdog`, `ISMA_DISK_ALERT_CMD` exists **only** in the drop-in and
   not in the unit fragment. Replacing the drop-in with a fresh `ExecStart`-only one silently
   disables that unit's alerting: it keeps running, keeps exiting `0`, and tells nobody anything.
   A dead-man's switch that has gone mute is worse than none, because it still looks present.
2. **It derives the new `ExecStart` from the old one by path substitution**, so trailing flags such as
   `--watchdog` survive rather than depending on someone remembering them.

It then **mechanically verifies** that `systemctl show -p Environment` is byte-identical before and
after, and fails the run if it moved. Compare the raw string, never a space-split list — several of
these values contain spaces, so splitting silently drops them.

Verify the deploy by reading the **resolved** command, never `systemctl cat`:

```bash
systemctl --user show isma-disk-canary.service -p ExecStart --value \
  | sed -n 's/.*argv\[\]=\([^;]*\);.*/\1/p'
```

`systemctl cat` prints dead lines: an empty `ExecStart=` **resets the whole list**, so everything
above it still looks authoritative while systemd ignores it.
