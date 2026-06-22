# Spec: self-healing daemon-restart manifest (kill the stale-dashboard window)

**Date:** 2026-06-21
**Status:** proposed → building
**Owner:** healer subsystem
**Touches:** `scripts/heal_daemon_restart_manifest_drift.py` (new),
`config/daemon-restart-manifest.json` (regen),
`systemd/ourliberty-heal-daemon-restart-manifest-drift.{service,timer}` (new),
`systemd/ourliberty-heal-stale-daemon-code.timer` (cadence),
`runbooks/post-merge-daemon-restart-discipline.md`.

## Problem

`ourliberty-dashboard-api.service` runs `python3 -m uvicorn scripts.dashboard_api:app`
(no `--reload`). Python caches imported modules, so after `ourliberty-sync`
pulls main the process keeps serving the *old* bytes until restarted.

Two mechanisms already restart it automatically, so the service is **not**
gated behind a manual approval (contrary to older notes — see memory
`dashboard-api-restart-on-deploy`, now corrected):

1. **Deploy-restart** — `sync_agent_core.sh` step 7 restarts every active
   `Type=simple` daemon whose `git diff OLD..NEW` intersects its
   `watch_paths` in `config/daemon-restart-manifest.json` (~0 latency).
2. **Healer backstop** — `heal_stale_daemon_code.py` (timer) restarts a daemon
   whose entrypoint *or* any manifest-listed import is newer than the
   process's `ActiveEnterTimestamp`.

**Both consume the same committed manifest, and the manifest silently drifts.**
The `watch_paths` are a transitive first-party import closure that is computed
once (`daemon_restart_manifest.py regenerate`) and committed — there is nothing
that keeps it fresh. When a PR adds a new first-party import to a daemon's
closure but does not regenerate the manifest, that dependency is invisible to
**both** restart paths.

That is exactly what hid **#617** (P6 brainstorm) and **#621** (P6.1 PR A):
`dashboard_api.py` reaches `projects_brainstorm_author.py` transitively (via
`heal_projects_store.py`), but the committed manifest's dashboard-api entry was
missing `projects_brainstorm_author.py` and 8 other modules. So the deploy-restart
computed "no daemon affected", the backstop's watchlist didn't know dashboard-api
depended on the changed file, and the fix sat stale until Larry restarted by hand.

Measured drift at spec time — `build_manifest()` vs the committed file adds:
- `ourliberty-dashboard-api.service` **+9**: `projects_brainstorm_author.py`,
  `projects_closeout_author.py`, `missions_narrator.py`, `launch_dedup_guard.py`,
  `heal_missions_card_gc.py`, `heal_orphan_autoregister.py`, `suggest_funnel_card.py`,
  `system_state_log.py`, `task_resolution.py`.
- `ourliberty-beacon-bot.service` **+26**, `ourliberty-outbox-notifier.service` **+4**.

## Decision (Larry, 2026-06-21)

Keep the auto-restart ("let the robot keep tapping") — a dashboard-api restart
is reversible and low-risk — and **make the stale window short** by removing
the drift that blinds it. Do **not** convert dashboard-api to alert-only.

## Design

### 1. One-time fix — regenerate the manifest
Ship the freshly-regenerated `config/daemon-restart-manifest.json` in this PR so
the existing drift (incl. the #617 module) is closed on merge.

### 2. Durable fix — `heal_daemon_restart_manifest_drift.py`
A healer that makes the manifest self-maintaining so it can never silently
drift again:

- Each tick: `fresh = build_manifest()` (truth from current code) vs
  `committed = load_manifest()` (on disk).
- **No drift → no-op** (idempotent; this is the steady state).
- **Drift →** write the fresh manifest, then commit + push *only that path* to
  `origin/main` (single-committer, pathspec-limited commit, FF-or-rebase-retry,
  never force; refuse if not on `main`). One **digest** alert summarizing the
  per-unit added/removed paths (auditable, low-noise). A commit/push failure or
  off-`main` refusal **escalates** with the manual `regenerate` command.
- Self-heal propagation: the committed manifest lands on the droplet's live
  checkout immediately (local write) and on `origin` (push). The backstop's next
  tick reads the fresh manifest and restarts any now-tracked, currently-stale
  daemon. Decoupled: this healer keeps the manifest fresh; `heal_stale_daemon_code`
  does the restart.
- Fail-safe (never raises; degrades to no-op + log), kill-switch aware
  (`~/agents/healers.disabled`), heartbeat, journal logging — mirrors the
  existing `heal_*` family.

### 3. Shrink the backstop window
Drop `ourliberty-heal-stale-daemon-code.timer` `OnUnitActiveSec` 30min → 10min.
The per-unit 30-min restart cooldown still caps each unit to one restart per
30 min, so this only makes *detection* faster (no restart storms); the scan is a
cheap `systemctl show` + `stat` sweep. Net worst-case window for a newly-imported
dependency: ≤ ~10 min (drift heal) + ≤ ~10 min (backstop) instead of hours/manual.

## Why not a new alert-only detector (the original ask)
dashboard-api already auto-restarts; a parallel alert-only detector would
duplicate ~1200 lines of `heal_stale_daemon_code` logic, fight the existing
auto-restart (alert, then it restarts anyway), and not address the *root cause*
(manifest drift). Fixing the manifest fixes the gap for **every** manifested
daemon, not just dashboard-api.

## Test plan (`unittest`)
1. `compute_drift`: identical → no drift; added/removed paths per unit; added
   unit; removed unit; `_meta`-only difference is ignored.
2. Commit path (tmp git repo, mirrors `test_heal_missions_card_gc`): clean →
   `nothing`; off-main → `wrong-branch`; delta → commits only the manifest path
   (no co-commit of other dirty files), idempotent on re-run.
3. `run_once` orchestration (mock `build_manifest`/`load_manifest`/commit/alert):
   drift → writes + commits + **digest** alert; no drift → no write/commit/alert;
   commit-failed/wrong-branch → **escalate** alert with the manual command.
4. Fail-safe: `build_manifest` raises → `main()` returns 0, no alert, logged.
5. Kill-switch present → early return, no work.
