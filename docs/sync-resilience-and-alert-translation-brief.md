# Brief: sync resilience to Pulse runtime files + sync.service alert translations

## Context

`scripts/sync_agent_core.sh` refuses to pull (fast-forward from origin/main) whenever the working tree has ANY uncommitted tracked changes, emitting `sync-blocked:uncommitted-changes` (source `sync.service`). This is a deliberate safety posture — sync must not pull onto a dirty/divergent tree.

`scripts/run_cycle.sh` auto-commits the Pulse-owned runtime files after a successful cycle (lines ~141-195): `runbooks/cycle-journal.md`, `runbooks/cycle-actions.jsonl`, `agents/pulse/MEMORY.md`, `agents/pulse/memory/`. But **interactive** Pulse `/cycle` runs do NOT go through `run_cycle.sh`, so they leave those same files dirty. The next sync tick then refuses to pull and pages Larry.

Incident 2026-05-28 (iter 98, interactive): three Pulse runtime files left uncommitted blocked sync for hours until manually committed (commit 22126bd). Separately, the `sync-blocked:*` alerts have ZERO entries in `config/alert-translations.json`, so they reach Larry as raw jargon instead of plain-language triage.

## Goal

Two related fixes, one PR:

1. **Make sync resilient to the known Pulse-owned runtime allowlist** so interactive cycles stop blocking sync — WITHOUT weakening the refuse-on-arbitrary-dirt safety posture.
2. **Add `sync.service` entries to `config/alert-translations.json`** for every `sync-blocked:*` subject.

## Fix 1 — sync resilience (DESIGN FORK — Forge/Mirror to choose)

The Pulse-owned runtime allowlist (exactly the set run_cycle.sh already commits):
- `runbooks/cycle-journal.md`
- `runbooks/cycle-actions.jsonl`
- `agents/pulse/MEMORY.md`
- `agents/pulse/memory/` (any file under)

CRITICAL CONSTRAINT: `sync_agent_core.sh` currently only PULLS (`git merge --ff-only origin/main`); it never pushes. A local-only auto-commit on `main` would diverge local from origin and break the next fast-forward. So any auto-commit MUST also push, or the gap must be closed at the cycle entry point.

Two acceptable approaches — implementer picks, Mirror reviews the tradeoff:

**Option A — sync auto-commits + pushes the allowlist.**
Before sync refuses on uncommitted changes, check whether the dirty set is a SUBSET of the allowlist. If so, commit those files (message e.g. `pulse: auto-commit runtime files (sync resilience)`) AND `git push origin main`, then proceed with the pull. If ANY non-allowlist file is dirty, refuse exactly as today.
- Pro: catches the dirt regardless of which entry point produced it.
- Con/tradeoff to flag: sync gains push capability (currently pull-only) — a posture change, bounded to the hardcoded allowlist. Must handle push failure gracefully (alert, do not leave a local-only commit that breaks future ff).

**Option B — interactive cycle path commits + pushes its own runtime files.**
Mirror `run_cycle.sh`'s auto-commit+push block into the interactive `/cycle` path (or a shared helper both call) so Pulse commits its runtime files itself. Sync stays pull-only.
- Pro: keeps sync's safer pull-only posture; fixes the root cause where it occurs.
- Con: only covers the cycle path; unrelated stray dirt still (correctly) blocks sync.

Either way, preserve: arbitrary non-allowlist uncommitted changes STILL block sync and still emit `sync-blocked:uncommitted-changes`. Reuse `run_cycle.sh`'s existing fixture-pattern commit guard so auto-committed runtime files never carry fixture-leak task_ids.

## Fix 2 — alert-translations.json entries

Add a top-level source block keyed `"sync.service"` (matches `--source sync.service` in the emitter). The renderer's lookup strips trailing `:`-segments, so a `sync-blocked` catch-all covers the `:<branch>` / `:<hash>` variants. Provide:

- `sync-blocked` (catch-all): WARNING / SOON — sync refused to pull from origin/main; working tree won't receive PR merges until resolved.
- `sync-blocked:uncommitted-changes`: WARNING / SOON — working tree has uncommitted modifications (commonly Pulse runtime files from an interactive cycle). Recovery: ssh, `cd ~/agent-core && git status`, commit or stash.
- `sync-blocked:wrong-branch`: WARNING / SOON — repo checked out on a non-main branch; all work commits direct to main. Recovery: checkout main, merge/discard the feature branch.
- `sync-blocked:fast-forward-failed`: URGENT / NOW — repo and origin diverged, ff merge impossible. Recovery: inspect `git log --oneline origin/main..HEAD`, rebase or reset to origin once safe.
- `sync-blocked:validation-failed`: URGENT / NOW — validate_agent_core.py rejected the pulled commit; sync rolled back. Recovery: run the validator to see the failure, fix on origin/main.
- `sync-blocked:quiescence-timeout`: WARNING / SOON — strict-quiescence wait exceeded; sync rolled back. Recovery: run await_quiescence.py to find the busy agent, resolve before next tick.

Match the existing schema exactly (`severity`, `tier`, `plain_language_summary`, `recommended_action`). Mirror the recovery text from the emit calls in `sync_agent_core.sh` so the plain-language action stays accurate.

## Addendum 2026-06-10 — captures.json (same incident class, second file)

Option A shipped. The hourly sync tick then began refusing on a *different*
machine-owned runtime file: `agents/beacon/captures.json`, written by the
missions ingest endpoint (`dashboard_api.py`) and committed every ~10 min by
`heal_missions_card_gc.py`. When sync lands in the gap between an ingest write
and the GC healer's next commit tick, it refuses-and-pages on a purely
automation-owned file — identical in spirit to the Pulse iter-98 case.

Fix: the auto-commit allowlist that was implicitly "the Pulse runtime set" is
now an explicit, named superset — `SYNC_AUTOCOMMIT_PATHS` in
`scripts/_lib_pulse_runtime.sh` = `PULSE_RUNTIME_PATHS` + `SYNC_EXTRA_RUNTIME_PATHS`
(currently just `captures.json`). Sync gates on
`all_modified_in_sync_autocommit_allowlist`; `run_cycle.sh` is unaffected (it
hardcodes the narrow Pulse set inline and is not a consumer of this lib).

Criteria for adding the next path to `SYNC_EXTRA_RUNTIME_PATHS` (documented at
the array): (1) written exclusively by automation, never hand-edited; (2) writes
are atomic (tmp+rename) so a git snapshot is never torn; (3) some other
automation already commits it on its own cadence — sync only absorbs the race
window. Conflict with the other committer is benign: both push to main with a
rebase/autostash fallback, and once either commits the delta the other sees a
no-op (idempotent). Non-allowlist dirt still blocks sync unchanged.

## Acceptance

- Interactive-cycle runtime dirt no longer blocks sync (per chosen option); a clean end-to-end demonstration.
- Arbitrary non-allowlist uncommitted file STILL blocks sync and emits the unchanged alert.
- No local-only commit can be left that would break a future fast-forward (push handled, or failure alerts and rolls back).
- All six `sync.service` keys present in `alert-translations.json`; the CI gate that enforces a translation per healer subject passes.
- Tests cover: allowlist-only dirt -> resolved; mixed dirt -> still blocked; push-failure path -> safe.
- Standard Forge flow: preflight -> build -> Mirror review -> PR. Conventional-commit style. No emoji in any artifact.
