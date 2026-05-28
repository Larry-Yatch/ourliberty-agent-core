# Runbook — post-merge daemon-restart discipline

When a PR merges code in `scripts/` that is **imported by multiple long-running daemons**, every dependent daemon must be restarted after `sync.service` lands the change on disk. Otherwise dependents keep executing the pre-merge module bytes for the entire interval until their next natural restart — typically hours to days.

Background — orchestrator-rectification-v2 V4 (2026-05-28). Bootstrap-002 verified that PR #145's H4 (the `scripts/dispatch_validator.py` `validate_task` validator changes) didn't take effect until **three** daemons restarted, not the one that intuitively "owns" the validator. The first restart cycle missed `beacon-bot`, so the same dispatch-validator rejection re-emitted until `beacon-bot` also restarted.

## Shared-lib → dependent-daemon mapping

| Shared library (script) | Restart these units after merge |
| --- | --- |
| `scripts/dispatch_validator.py` | `ourliberty-beacon-bot.service`, `ourliberty-outbox-notifier.service`, `ourliberty-inbox-watcher.service` |
| `scripts/safe_write_inbox.py` | `ourliberty-beacon-bot.service`, `ourliberty-outbox-notifier.service`, `ourliberty-inbox-watcher.service` |
| `scripts/routing_validator.py` | `ourliberty-beacon-bot.service`, `ourliberty-outbox-notifier.service`, `ourliberty-inbox-watcher.service` |
| `scripts/marker.py` *(library surface — not the CLI)* | `ourliberty-beacon-bot.service`, `ourliberty-outbox-notifier.service`, `ourliberty-inbox-watcher.service` |

The table is the canonical specification; `scripts/heal_stale_daemon_code.py::SHARED_LIB_WATCHLIST` is the executable enforcement. They must stay in sync — when a new shared lib appears, add it to both places.

## Manual recovery command

For each unit in the right column:

```bash
sudo systemctl restart <unit-name>
```

The systemd sudoers contract on the droplet permits this without a password. Verify with `systemctl status <unit-name>` and inspect `ActiveEnterTimestamp` to confirm the unit picked up the new code.

## Automated enforcement

`scripts/heal_stale_daemon_code.py` ticks every 30 minutes via `ourliberty-heal-stale-daemon-code.timer`. The healer's `SHARED_LIB_WATCHLIST` mirrors the table above. On each tick, for every (shared-lib, dependent-daemon) pair, the healer compares the shared lib's mtime against the dependent daemon's `ActiveEnterTimestamp`. When `lib_mtime > service_start + RACE_AVOIDANCE_SEC` (5 min), the healer treats the dependent daemon as stale and invokes `sudo -n systemctl restart <unit>`, then DMs Larry with the outcome.

The existing direct-script staleness path (a unit's own ExecStart script being newer than its service start) continues to function untouched; the watchlist path is purely additive — it catches the multi-daemon-restart shape that the direct-script path can't see.

## Why this discipline, why automation

**Documentation alone repeats the bug.** Bootstrap-002 was preceded by PR-S4 (PR #145) — the original H4 work itself only worked once Larry manually restarted three daemons mid-incident. Without a runbook the next H4-shape PR will repeat the same first-restart-misses pattern.

**Automation alone is opaque.** The healer's restart can fire faster than a human can correlate "PR merged 14 minutes ago" with "ouliberty-beacon-bot just restarted." The runbook gives operators (Larry, future maintainers) the mental model for what they're seeing.

**Both together: runbook is the spec; healer is the enforcement.** Read both when investigating a "daemon restarted unexpectedly" DM or when planning a PR that touches the left column.

## Trade-offs / known limits

- The mapping is **hand-maintained**. A new shared lib that grows a dependent set silently is invisible until someone catches it. Mitigation: code review on PRs that introduce a new import from `scripts/` into a long-running daemon should include "is this lib in the SHARED_LIB_WATCHLIST?"
- The healer's per-unit 30-min restart cooldown is shared between direct-script and watchlist paths. A watchlist-driven restart followed within 30 min by a direct-script change (or vice versa) will suppress the second restart and escalate to a manual-investigation DM. This is intentional — the cooldown's job is to prevent loops; an actual rapid second-change scenario should land on a human's eyeballs, not on auto-recovery.
- `scripts/marker.py` is listed because its `MARKER_TYPES` / `REQUIRED_FIELDS` registries are imported by `beacon_approval_handler.py`, which is imported by both `beacon-bot` and `outbox-notifier`. The CLI binary itself runs per-invocation and doesn't need a restart — the library surface does.

## Composition with the existing heal-stale-daemon-code runbook

`runbooks/heal-stale-daemon-code.md` documents the direct-script detection path (a unit's own ExecStart script newer than its active-since). The watchlist path described here is a sibling mechanism inside the same healer process — same logging, same DM machinery, same 30-min cooldown, same `healers.disabled` kill switch. Reading both runbooks together gives the full picture.
