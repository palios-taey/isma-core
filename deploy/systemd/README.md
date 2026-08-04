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
