# systemd units

Unit files for the ISMA background jobs, committed so the running schedule is a
reproducible artifact rather than undocumented local state.

```bash
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now isma-disk-canary.timer
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
