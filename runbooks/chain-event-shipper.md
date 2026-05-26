# Runbook: chain_event_shipper

Operational guide for `scripts/chain_event_shipper.py`, the poll-based
ingestion daemon that populates the Supabase `chain_events` table.
Reference spec: `agents/beacon/specs/e4-4d-system-tab.md` § 5.1, § 5.2.

## Quick reference

| What | Where |
|---|---|
| Source code | `scripts/chain_event_shipper.py` |
| Systemd service | `ourliberty-chain-event-shipper.service` |
| Heartbeat | `~/agents/blackboard/chain-event-shipper.heartbeat` (touched every 30s) |
| Daemon log | `~/agents/logs/chain-event-shipper.log` + `journalctl -u ourliberty-chain-event-shipper.service` |
| Spill buffer | `~/agents/state/chain-event-buffer.jsonl` (only present when Supabase unreachable) |
| Journal cursor | `~/agents/state/chain-event-cursor.journal` |
| File cursors | `~/agents/state/chain-event-cursors.json` |
| Pulse cursor | `~/agents/state/chain-event-cursor-pulse.json` |
| Activation gate | `Environment="OURLIBERTY_CHAIN_SHIPPER_ENABLED=true"` in service override |
| Blanket kill-switch | `~/agents/healers.disabled` (touch to halt; daemon exits cleanly) |

## Start / stop / restart

```bash
# Start (first time / after install)
sudo systemctl enable ourliberty-chain-event-shipper.service
sudo systemctl edit ourliberty-chain-event-shipper.service
# add under [Service]: Environment="OURLIBERTY_CHAIN_SHIPPER_ENABLED=true"
sudo systemctl start ourliberty-chain-event-shipper.service

# Stop
sudo systemctl stop ourliberty-chain-event-shipper.service

# Restart after a code change (pair with stale-daemon-code healer's signal)
sudo systemctl restart ourliberty-chain-event-shipper.service

# Verify
systemctl status ourliberty-chain-event-shipper.service
journalctl -u ourliberty-chain-event-shipper.service --since "10 min ago" | tail -50
```

## Verify ingestion is healthy

```bash
# 1. Daemon is up and the heartbeat is recent
stat -c '%y %n' ~/agents/blackboard/chain-event-shipper.heartbeat

# 2. Drain logs show non-zero counts (after some activity has happened)
grep "drain:" ~/agents/logs/chain-event-shipper.log | tail -10

# 3. Buffer is absent or small (chronic outage would grow this)
ls -lh ~/agents/state/chain-event-buffer.jsonl 2>/dev/null || echo "buffer is empty (good)"

# 4. From the dashboard side: chain_events row count is moving
source ~/credentials/.env.larry
curl -s "${SUPABASE_URL}/rest/v1/chain_events?select=count" \
     -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" \
     -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}" \
     -H "Prefer: count=exact"
```

The healer `heal_chain_event_shipper_heartbeat.py` runs every 5 min and DMs
Larry if the heartbeat is >10 min stale. If you didn't get a DM, the
daemon is alive (or the healer itself is broken — check
`journalctl -u ourliberty-heal-chain-event-shipper-heartbeat.service`).

## Debugging a stuck cursor

Symptom: drain logs show `journal=0 log=0 ...` for a long time but you
know events were generated.

```bash
# Each source has its own cursor — show them all
cat ~/agents/state/chain-event-cursor.journal
cat ~/agents/state/chain-event-cursors.json
cat ~/agents/state/chain-event-cursor-pulse.json

# Find the source by running a single drain pass with verbose logging
sudo systemctl stop ourliberty-chain-event-shipper.service
sudo -u larry -E python3 ~/agent-core/scripts/chain_event_shipper.py --once --log-level=DEBUG
sudo systemctl start ourliberty-chain-event-shipper.service

# If a single source is stuck, reset just that cursor:
# - journalctl cursor: rm ~/agents/state/chain-event-cursor.journal (restart from oldest)
# - file cursors: edit ~/agents/state/chain-event-cursors.json and reset offset to 0
#   for the affected key
# Then restart the daemon.
```

## Handle Supabase downtime

The daemon spills to `~/agents/state/chain-event-buffer.jsonl` (capped at
10,000 lines / ~5 MB) when Supabase writes fail. On next successful tick
it flushes the buffer FIFO before fresh events. **Behavior to expect:**

- 1-hour outage: buffer grows, no events lost.
- 24-hour outage on a quiet day (<400 events/h): buffer grows but stays
  under cap.
- Chronic outage filling the cap: the *oldest* events get dropped with a
  `BUFFER_OVERFLOW` log line. The audit healer DMs Larry within a week
  if any of the dropped events were unique types.

**Manual buffer inspection:**

```bash
wc -l ~/agents/state/chain-event-buffer.jsonl
head -3 ~/agents/state/chain-event-buffer.jsonl | python3 -m json.tool
tail -3 ~/agents/state/chain-event-buffer.jsonl | python3 -m json.tool
```

If you suspect the buffer is the wrong shape for the schema (rare —
would mean the daemon shipped + a migration changed the schema), stop
the daemon, copy the buffer aside, and `rm ~/agents/state/chain-event-buffer.jsonl`
to start fresh. The events are still in the source files so on next
drain they get re-emitted.

## Recover from a daemon crash

systemd `Restart=on-failure` brings it back within `RestartSec=15s`. The
cursor files mean it resumes from the last successfully-shipped event.
Worst case: 1-2 seconds of events get re-shipped — the deterministic
`event_id` PK absorbs them via the upsert(ignore_duplicates=True) path.

If the daemon is in a crash-loop (systemd's `Restart=on-failure` budget
exhausts within a configured window):

```bash
# Inspect the crash trace
journalctl -u ourliberty-chain-event-shipper.service --since "10 min ago"

# Common causes:
# - SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing (check .env.larry)
# - Schema mismatch (chain_events table doesn't exist yet → PR-A not applied)
# - One source file is unreadable (permission flip)

# Pause via blanket kill-switch while you fix the root cause
touch ~/agents/healers.disabled
# ...fix the issue...
rm ~/agents/healers.disabled
sudo systemctl restart ourliberty-chain-event-shipper.service
```

## Add a new event_type

The KNOWN_EVENT_TYPES allowlist in `scripts/chain_event_shipper.py` is the
source of truth. To add one:

1. Add the string to the `frozenset({...})` block (top of `chain_event_shipper.py`).
2. Add a matching parser branch — extend `_LOG_EVENT_KEYWORDS` or one of
   the source parsers (`parse_journal_record`, `parse_jsonl_line`, etc.).
3. Add a unit test in `scripts/tests/test_chain_event_shipper.py`.
4. Open a single-line Forge PR (Mirror reviews + auto-merges).
5. After merge, `heal_stale_daemon_code.py` will alert if the live daemon
   isn't running the new code; restart with `sudo systemctl restart
   ourliberty-chain-event-shipper.service`.

The weekly audit healer (`heal_chain_event_type_audit.py`) will stop
DMing Larry about the new type once the allowlist includes it.

## Accepting / rejecting Check III proposals

Pulse writes `~/agents/blackboard/pulse-threshold-proposals.json` every
14 days. Larry approves via Telegram:

```
approve threshold-update-<YYYY-MM-DD>
reject  threshold-update-<YYYY-MM-DD>  <reason>
```

Beacon's CLAUDE.md (Pulse Check III approvals section) handles dispatch
to Forge → config-only PR → Mirror auto-merge. Idempotency is enforced
by the `applied: true` flag in the archived artifact at
`~/agents/blackboard/pulse-check-iii/check-iii-<date>.json`.

If a previously-applied proposal needs reverting, the next Check III
cycle will automatically propose a rollback if it detects >3
false-positive stuck alerts within 7 days of apply. The rollback
proposal goes through the same approve/reject shortcut.

## Known deviations from spec

- **Heartbeat-healer DM cooldown.** Spec § 5.2 mentions "6h cooldown per
  incident"; `larry_alerts.append_alert` only exposes warning (60 min) /
  critical (10 min) windows. We use `warning` (60 min). In practice the
  heartbeat staleness window is sticky once tripped (the daemon stays
  down until restarted), so the DM will land once per 60-min cycle until
  the operator acts.
- **Memory limits.** `MemoryMax=512M` / `MemoryHigh=256M` are spec-locked;
  if 24h journalctl-tail load reveals tightness, edit the service file
  override and document the new values here.
