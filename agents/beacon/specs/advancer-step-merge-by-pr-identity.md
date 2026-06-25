# Build spec — Credit a step-merge by PR identity (fix the rebase gate-mismatch wedge)

**Status:** Draft v1 for build — 2026-06-25.
**Repo:** ourliberty-agent-core.
**Author:** Chain (dispatched spec-first doc, task `spec-advancer-step-merge-by-pr-identity-001`). **Approver:** Larry / Mirror review.

> Grounded against live code (`scripts/build_sequence_advancer.py`, `scripts/tests/test_build_sequence_advancer.py`, read 2026-06-25). Reuse-first: the gate, the mismatch clock, and the PR-identity reconcile notion already exist — this is a targeted asymmetric branch in the existing single-signal mismatch path, not a new subsystem.

## 0. Goal

Stop the build-sequence advancer from **falsely wedging a step whose PR genuinely merged**. When the belt-and-suspenders gate sees `gh_merged=True` (the step's own PR is MERGED on GitHub) but `chain_merged=False` (no matching `auto_merge` chain_event under the step's task_id), the advancer should — **after a short grace** — credit the step as merged **by PR identity**, because the step's own `pr_url` reporting MERGED is authoritative proof that step's work shipped. The opposite direction (`chain_merged=True` / `gh_merged≠True`) stays guarded exactly as today.

## 1. Why (the evidence)

The advancer's gate computes two signals in `_check_in_flight_step` (`scripts/build_sequence_advancer.py` ~L745):

- `chain_merged = chain_event_says_merged(client, task_id, dispatched_at)` where `task_id = f'seq-{seq_id}-step-{step_id}'` — i.e. it looks for an `auto_merge` chain_event **keyed to the step's own task_id**.
- `gh_merged = gh_pr_says_merged(step['pr_url'])` — asks GitHub whether **the step's own PR** is MERGED.

**The bug:** when a step's PR is merged via a *different* dispatch than the step's original build — a `phase=rebase` re-dispatch (`forge-post-open-mergeable-rebase-001`) or any re-dispatch task that owns the merge — the `auto_merge` chain_event is emitted under **that task's** id, not `seq-{seq_id}-step-{step_id}`. So `chain_event_says_merged` finds no row → `chain_merged=False`, while `gh pr view <step.pr_url>` returns MERGED → `gh_merged=True`.

That is exactly one signal, so the gate falls into the single-signal **mismatch** branch (~L773). The mismatch persists tick after tick (the chain leg will *never* catch up — the event is permanently keyed elsewhere), and at 30 min (`GATE_MISMATCH_TIMEOUT_SEC`) the advancer emits `gate-mismatch-timeout`, **pauses the sequence, and DMs Larry** (operator-needs-you / escalation-feed, #685). A step that in fact merged cleanly strands the whole sequence and pages a human to reconcile by hand.

This is a *false* gate-mismatch: the two signals disagree only because they are keyed differently, not because the merge is in doubt. GitHub already confirmed the merge.

## 2. What already exists (reuse, don't rebuild)

- **The dual-gate + single-signal mismatch branch:** `_check_in_flight_step` (`build_sequence_advancer.py` ~L713–811). The both-gates-pass path (~L750) → `step-merged`; the neither-gate path (~L760) → wait; the single-signal else-branch (~L773) → `gate-mismatch` → (30 min) → `mismatch_timeout`.
- **The mismatch clock:** `_first_gate_mismatch_ts(audit_log, step_id)` (~L453) already tracks the age of an open mismatch and is reset by `gate-clear` / `step-merged` / `step-dispatched`. The new grace check **reuses this same clock** — no new timestamp bookkeeping.
- **`gh_pr_says_merged(pr_url)`** (~L434) — returns `True` (MERGED) / `False` (OPEN/CLOSED-not-merged) / `None` (gh timeout/auth/network → soft-fail). The asymmetry below keys on `is True` precisely so a `None` soft-fail never triggers a credit.
- **PR-identity reconcile precedent:** the stall-backstop reconcile pass (`_match_step_to_merged_pr`, ~L1079) already attributes a merged PR to a step by `pr_url` → branch → title-substring identity. This spec applies the *same* "the PR's identity is the step's identity" principle inside the in-flight gate, for the in-review case the reconcile pass doesn't cover (it only RETIRES already-merged stranded work; it does not credit an in-flight step on the merge gate).
- **The 30-minute tolerance constant** `GATE_MISMATCH_TIMEOUT_SEC = 30*60` (~L93) — unchanged; the new grace is a *separate, shorter* constant.

**THE GAP:** the single-signal mismatch branch is symmetric — it treats `chain=False/gh=True` and `chain=True/gh=False` identically (both head toward the 30-min pause). One of those directions is a false wedge; the other is a real safety hold. They must be handled asymmetrically.

## 3. Deliverables

### D1 — CRITICAL: asymmetric credit in the single-signal mismatch branch

In `_check_in_flight_step` (`scripts/build_sequence_advancer.py`), split the existing single-signal mismatch path on the **direction** of the disagreement:

**Direction A — `gh_merged is True and not chain_merged` (the false-wedge / rebase case):**
- This is "GitHub confirms the step's own PR merged, but no `auto_merge` event landed under the step's task_id." Treat the step's `pr_url` MERGED state as authoritative **after a short grace**:
  - Compute the open-mismatch age from `_first_gate_mismatch_ts(audit_log, step_id)` (same clock the 30-min check uses).
  - **First observation** (no open mismatch yet) → log `gate-mismatch` and `wait`, exactly as today. (Do **not** credit on the very first tick — see § 4 belt-and-suspenders rationale.)
  - **Open-mismatch age ≥ `MERGE_BY_PR_IDENTITY_GRACE_SEC`** → transition the step to **`merged`**, appending a **distinct** audit event `step-merged-by-pr-identity` (carry `step_id`, `pr_url`, `task_id`, `reason='gh MERGED on step pr_url; chain_event keyed to a different (re-dispatch) task'`). No pause, no Larry DM — this is a success transition, identical downstream to `step-merged`.
  - **Open-mismatch age < grace** → keep `wait` (mismatch clock keeps running; the happy-path chain leg still has time to catch up and produce the normal both-gates `step-merged`).

**Direction B — `chain_merged and gh_merged is not True` (the guarded / dangerous case):** **unchanged.** chain_events claims the merge but GitHub does not confirm it (PR still OPEN/CLOSED, or `gh` unreachable → `None`). This MUST NOT auto-credit: a stale/mis-keyed chain event, a reverted merge, or a transient `gh` outage must never advance the sequence onto a step whose PR did not actually land. Keep the existing `gate-mismatch` → 30-min → `mismatch_timeout` → pause + DM flow verbatim. GitHub is the source of truth for "did this PR actually merge"; absent its confirmation, the safe action is to hold for a human.

### D2 — The grace constant

Add a module-level tunable near `GATE_MISMATCH_TIMEOUT_SEC`:

```python
# Grace before crediting a step as merged by PR identity (Direction A: gh MERGED
# on the step's own pr_url, but no auto_merge chain_event under the step's
# task_id — the merge landed via a re-dispatch/rebase task keyed elsewhere).
# Must be < GATE_MISMATCH_TIMEOUT_SEC so the credit fires well before the pause.
# Long enough that a NORMAL merge's auto_merge chain_event has time to ingest
# (preserving belt-and-suspenders on the happy path); short enough to beat the
# 30-min false pause by a wide margin.
MERGE_BY_PR_IDENTITY_GRACE_SEC = 10 * 60
```

Recommended value 10 min (implementer may tune within the stated constraint `0 < grace < GATE_MISMATCH_TIMEOUT_SEC`). Rationale for "not zero" is in § 4.

### D3 — Observability (no new alert)

`step-merged-by-pr-identity` is its own audit event (distinct from `step-merged`) so the audit trail records when the fallback path fired and how often the rebase case occurs. Emit one INFO log line on the transition. **No Larry DM** — a successful merge is not actionable; per the actionable-only alert discipline, only the *guarded* direction's pause DMs Larry.

## 4. Why a grace (not an immediate credit) — the belt-and-suspenders rationale

On the **normal** (no-rebase) merge, `gh pr view` flips to MERGED a tick or two *before* `chain_events` ingests the `auto_merge` row. If we credited immediately on `gh=True/chain=False`, we would bypass the chain-event leg on **every** normal merge — belt-and-suspenders would degrade to a one-leg GitHub-only gate. The grace preserves the second leg: on the happy path the chain row lands within the grace and the step transitions via the normal both-gates `step-merged`. Only when the chain leg **fails to catch up within the grace** — which is precisely the rebase case, where it will never catch up — does the gate fall back to PR-identity credit. The grace is therefore the mechanism that keeps the happy-path gate two-legged while still healing the false wedge.

**Enforcement:** the asymmetric branch + the grace are pinned by the regression tests in § 5 (the `step-merged-by-pr-identity` transition, the within-grace wait, and the unchanged Direction-B pause). Mirror's review checklist flags any change to the single-signal branch that removes the direction split or the grace.

## 5. Acceptance criteria (each a test in `scripts/tests/test_build_sequence_advancer.py`)

- [ ] **Direction A, past grace → credit.** `gh_merged=True`, `chain_merged=False`, with an open `gate-mismatch` for the step older than `MERGE_BY_PR_IDENTITY_GRACE_SEC` → step transitions to `merged`; audit log contains `step-merged-by-pr-identity`; **no** `gate-mismatch-timeout`; sequence **not** paused.
- [ ] **Direction A, within grace → wait.** Same signals, mismatch younger than the grace → step stays in-flight, **no** credit, mismatch clock still running. (This is the updated half of today's `test_only_gh_does_not_advance` ~L316.)
- [ ] **Direction A, first observation → wait + log.** `gh_merged=True`, `chain_merged=False`, no prior mismatch → `gate-mismatch` logged, no credit this tick.
- [ ] **Happy path preserved.** Within the grace, if `chain_merged` becomes `True` (chain leg catches up), the step transitions via the normal `step-merged` (both gates), **not** `step-merged-by-pr-identity`.
- [ ] **Direction B guarded (within 30 min).** `chain_merged=True`, `gh_merged=False`, mismatch older than the grace but younger than `GATE_MISMATCH_TIMEOUT_SEC` → still `mismatch`, **no** credit, **no** advance.
- [ ] **Direction B guarded (past 30 min) → pause unchanged.** `chain_merged=True`, `gh_merged=False`, mismatch older than `GATE_MISMATCH_TIMEOUT_SEC` → `gate-mismatch-timeout`, sequence paused + DM (regression of existing `test_old_mismatch_triggers_pause` ~L343).
- [ ] **`gh` soft-fail never credits.** `chain_merged=True`, `gh_merged=None` (gh unreachable) → guarded path, no credit (asymmetry keys on `gh_merged is True`, so `None` cannot trigger Direction A).
- [ ] **`gate-clear` still resets the clock** for both directions (existing `test_gate_clear_resets_mismatch_clock` ~L401 still passes).
- [ ] Full `python3 -m unittest discover -s scripts/tests` green; `/code-review` clean.

## 6. Out of scope

- **Implementing the change.** This spec is a doc-only PR; the code change is a separate dispatched build.
- **Re-keying the `auto_merge` chain_event to the step's task_id** at the rebase/re-dispatch source. That would also close the gap, but it touches the rebase-dispatch and chain-event-shipper paths and is a larger, riskier change; PR-identity credit at the gate is the smaller, localized fix that needs no upstream coordination. Note it as a possible future hardening, not this work.
- **The stall-backstop reconcile pass** (`_match_step_to_merged_pr`) — untouched; it covers the already-stranded/RETIRE case, this covers the in-flight gate case.

## 7. Safety notes

- **The asymmetry is the whole safety property.** Credit-by-identity fires ONLY when GitHub confirms the step's own PR is MERGED (`gh_merged is True`). The direction where chain_events claims a merge GitHub won't confirm is never auto-credited — it keeps the 30-min pause-and-page behavior. Do not collapse the two directions back into one branch.
- **`gh_merged is None` is not `True`.** A `gh` timeout/auth/network failure must never be read as a merge. The branch condition is `gh_merged is True`, never a truthy check.
- **Grace bound is load-bearing:** `0 < MERGE_BY_PR_IDENTITY_GRACE_SEC < GATE_MISMATCH_TIMEOUT_SEC`. A grace of 0 degrades belt-and-suspenders to GitHub-only on every merge; a grace ≥ the 30-min timeout would let the false pause fire first and defeat the fix.
