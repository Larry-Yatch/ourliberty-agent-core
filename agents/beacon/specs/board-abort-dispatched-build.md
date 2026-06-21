# Build spec — Abort a dispatched build (guarantee: nothing from it auto-merges)

**Status:** Draft v1 for build — 2026-06-20.
**Repo:** ourliberty-agent-core (core change) + ourliberty-dashboard (the button, separate/coordinated).
**Author:** Claude Code (desktop). **Approver:** Larry.
**⚠️ BUILD CONTROLLED, HUMAN-GATED MERGE — do NOT auto-merge this PR.** It modifies the auto-merge gate itself; it must not ship through the very path it's changing. `/code-review high` → Larry-gated merge.

> Grounded against live code (Explore pass 2026-06-20, re-verified 2026-06-21). Reuse-first: the cancel action already exists; the gap is a single safety check.

## 0. Goal

Give us a reliable way to **abort a dispatched build**, with one hard guarantee: **once aborted, nothing from that build can auto-merge to main.** Today there is no such guarantee — a dispatched build runs to completion and auto-merges on Mirror pass with no way to stop it.

## 1. Why (the evidence)

2026-06-20 dogfood: a build we tried to stop (`#602`) **auto-merged to main at 01:10** with no gate, and a duplicate build dispatched before we could cancel it. Larry: *"I don't have an option to not merge it if it is the wrong one."* Correct — and this closes that hole.

## 2. What already exists (reuse, don't rebuild)

- **Cancel action exists:** `scripts/sequence_shortcut_helpers.py::apply_cancel(seq_id, actor, reason)` — sets sequence `status: failed` + appends `audit_log` entry `{event:'cancelled', actor, reason, ts}`. Idempotent. (No `cancelled` enum — cancellation = `failed` + the audit event.)
- **Advancer already stops** on `status != 'active'` (`build_sequence_advancer.py::_process_active_sequence`) — no change needed there.
- **Sequence files** = SSOT at `~/agents/blackboard/build-sequences/<seq_id>.json` (atomic writes).
- **Task-id is parseable:** dispatched steps use `task_id = seq-{seq_id}-step-{step_id}` → regex `^seq-(.+)-step-(.+)$`.

**THE GAP:** the auto-merge gate does NOT check the sequence's cancelled state before merging.

## 3. Deliverables

### D1 — CRITICAL: cancelled-check in the auto-merge gate
`scripts/outbox_notifier.py::_run_review_pass_auto_merge()` (~line 8959), BEFORE it calls `_attempt_auto_merge_with_gates()` (~7024) / `_auto_merge_pr()` (the `gh pr merge` at ~line 5242):
- Parse `seq_id` from `data['task_id']` (`^seq-(.+)-step-(.+)$`). If it's not a sequence task, proceed normally (unchanged).
- Read `~/agents/blackboard/build-sequences/<seq_id>.json`. If `status == 'failed'` AND any `audit_log` entry has `event == 'cancelled'` → **do NOT merge.** Return the skip outcome (mirror the existing `_skip(...)` pattern) with reason `sequence-cancelled`; log `AUTO_MERGE task=... outcome=skipped reason=sequence-cancelled agent=forge`.
- **FAIL-OPEN:** any read/parse error → proceed with the normal merge (never block a legitimate merge because we couldn't read a file). This is the key safety posture — the check only ever *adds* a skip for a *confirmed* cancellation.

### D2 — Abort endpoint
`scripts/dashboard_api.py`: add `POST /api/sequences/{seq_id}/abort` (same `Depends(_require_token)` auth as the other system routes) → calls `sequence_shortcut_helpers.apply_cancel(seq_id, actor='larry', reason=<from body or default>)` → returns the updated status. This is the controllable "stop it" lever.

### D3 — Board button (dashboard repo — coordinate with Projects-tab-v3 stream)
A "Cancel / Abort build" control on an active project/sequence card → calls D2. Separate PR in `ourliberty-dashboard`; route through the board stream (boundary). Not required for the safety guarantee (D1+D2 give that); it's the human-facing trigger.

### D4 — OPTIONAL followup: close orphaned PRs of cancelled builds
A small healer that, for a `failed`+`cancelled` sequence, finds any still-OPEN step PRs and closes them (cleanup, so a cancelled build leaves nothing dangling). Safe to defer.

### (Secondary, OPTIONAL) wire board "drop mission" → `apply_cancel` its underlying sequence, so dropping a card also stops its build. Today drop archives the card but leaves the sequence live (this is how the 2026-06-20 duplicate slipped through).

## 4. Acceptance criteria

- [ ] A cancelled sequence's step PR is **NOT auto-merged** — the gate logs `outcome=skipped reason=sequence-cancelled` and the PR stays open. (Test with a fixture sequence in `failed`+cancelled state.)
- [ ] A normal (active) sequence's PR **still auto-merges** — no regression. (Test.)
- [ ] A missing/unreadable sequence file → **merge proceeds** (fail-open). (Test.)
- [ ] A non-sequence task_id (not `seq-*-step-*`) is unaffected. (Test.)
- [ ] `POST /api/sequences/{seq_id}/abort` cancels the sequence (status `failed` + audit `cancelled`) and is auth-gated. (Test.)
- [ ] `/code-review high` clean; **Larry-gated merge (NOT auto-merged).**

## 5. Out of scope (separate, lower urgency — already carded)

- **Launch spec-confirm** (show + confirm repo/spec_docs before dispatch; editable mission spec_docs). Separate fix.
- The other board cosmetic bugs (queue latency, card-not-updating-after-launch).

## 6. Safety notes

- **Fail-open in D1 is non-negotiable** — the check must never block a legitimate merge; it only adds a skip for a confirmed cancellation.
- This PR changes the irreversible merge path; build it controlled, review hard, and merge by hand.
