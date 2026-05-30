# PR B — Auto-resume paused-on-tier1 tasks

Step `step-b-resume` of sequence `rate-limit-resilience-001`. Depends on
`step-a-rotation`. Read `docs/rate-limit-resilience-project.md` for shared constraints.

## Problem

When Tier 1 hits a rate-limit or auth_401 on a `--resume` session, Tier 2 fallback is
structurally unavailable (session IDs are account-bound). `agent_runner.run_claude`
correctly refuses the fallback and calls `_mark_paused_on_tier1(task_stem, failure_type)`
(~`agent_runner.py:953`), then returns terminally. The task then sits stalled until Larry
notices the DM. On 2026-05-29 this happened 6+ times to `forge` and `beacon-bot`.

`scripts/heal_pipeline_stall.py` already DETECTS the `paused_on_tier1` marker and DMs, but
nothing RESUMES the work when the window clears.

## Scope

1. **Auto-resume healer.** Add a healer (e.g. `scripts/heal_resume_paused_on_tier1.py`)
   plus a systemd timer template, that:
   - Scans for tasks marked `paused_on_tier1` (the marker `_mark_paused_on_tier1` writes).
   - Checks whether the relevant tier's cooldown / 5h window has cleared, using
     `active_tier.cooldown_until()` and the `retry_after_sec` now captured by PR C.
   - Re-dispatches the work as a FRESH task on the recovered tier — drop the stale
     `--resume` session_id (it is account-bound and cannot be reused), matching the
     guidance already in the `_dm_tier2_unavailable` text.
   - Clears the `paused_on_tier1` marker on successful re-dispatch.
   - Use a bounded systemd timer; do NOT hand-roll detached poll loops (repo convention).
2. **Reduce resume exposure (scope conservatively).** For heavy multi-phase Forge builds,
   prefer fresh-session dispatch where feasible so Tier 2 fallback stays available. If
   this is larger than a small change, surface it in preflight as a follow-up rather than
   forcing it into this PR.

## Acceptance

- A task marked `paused_on_tier1` is automatically re-dispatched (fresh session, recovered
  tier) once the tier's cooldown/window clears, without Larry intervening.
- No orphaned/detached poll loops; the healer runs on a bounded timer and is covered by
  the `~/agents/healers.disabled` kill switch like other healers.
- Regression gate passes; rotation remains disabled.
