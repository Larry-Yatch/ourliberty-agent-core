# Brief: Close the deploy-side stale-daemon restart gap

**Type:** Forge dispatch (real code, live deploy path — NOT Claude-as-Forge)
**Origin:** Cost-signal audit 2026-06-03. `heal-stale-daemon-code` fired 34× auto-restarting
daemons; root-caused to the sync/deploy step not restarting long-running daemons after a pull.

## Problem (two layered holes)

1. **Deploy side.** `scripts/sync_agent_core.sh` Step 7 restarts **only** `ourliberty-orchestrator`,
   and only when `orchestrator.py` itself changed (comment F55 deliberately narrowed it on the
   assumption that "everything else runs as oneshot timers and re-reads code on next fire").
   That assumption is false for the **long-running daemons** (`Type=simple`): when a sync pulls a
   change to a *shared module they import*, they keep running the old code in memory until restarted.
2. **Healer side.** `scripts/heal_stale_daemon_code.py` only compares the **entrypoint script's**
   mtime, so it misses daemons whose entrypoint is unchanged but whose **imported module** changed —
   the safety net has a hole exactly where the deploy side leaks.

## Confirmed long-running daemon set (Type=simple, EnvironmentFile=.env.larry)

`beacon-bot`, `forge-bot`, `mirror-bot`, `inbox-watcher`, `chain-event-shipper`,
`dashboard-api`, `outbox-notifier`, `pulse-bot`. (`orchestrator` already handled; verify list live.)

## Fix

1. **Restart-on-deploy in `sync_agent_core.sh`.** After the pull, capture `OLD_HEAD..NEW_HEAD`,
   compute `git diff --name-only`, and restart exactly the daemons whose **watched paths** changed.
   - Keep the existing orchestrator restart.
   - Only restart units that are currently `is-active`.
   - Restart each unit at most once per sync; log every restart (unit + triggering paths).
   - Guard against restart storms: if >N daemons would restart (e.g. a shared base module changed),
     still restart them, but emit a single summary log line, not one alert per unit.
2. **Explicit manifest** `config/daemon-restart-manifest.json`: maps each long-running unit to the
   set of paths (entrypoint + imported module files/dirs) whose change triggers its restart. Single
   source of truth, handoff-readable, enforceable. Prefer this over auto-deriving the import graph
   (clever but fragile).
3. **Backstop:** extend `heal_stale_daemon_code.py` to also consider the unit's manifest watch-paths'
   mtimes (not just the entrypoint), so the healer catches anything the sync path misses.

## Tests

- Changed-file → unit mapping: unrelated change restarts nothing; entrypoint change restarts the unit;
  imported-module change restarts the unit; shared-base change restarts all dependents once each.
- Manifest completeness: every `Type=simple` unit has a manifest entry (fail the test if a daemon is
  unmapped — enforcement).

## PREFLIGHT MUST VERIFY (do not trust this brief's data claims)

- The 8-daemon list against `systemctl list-units 'ourliberty-*.service'` on the droplet.
- `sync_agent_core.sh` Step 7 exact lines (~284–355) and where OLD/NEW head can be captured.
- That each daemon's actual imported-module set is reflected in the manifest (spot-check 2–3).
- CLARIFY if the import surface is too broad to enumerate by hand — may need a lightweight import scan.
