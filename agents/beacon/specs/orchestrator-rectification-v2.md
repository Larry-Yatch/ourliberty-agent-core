# Spec: Orchestrator Rectification v2 — close the 6 bootstrap-002 gaps

**Status:** Draft (awaiting Larry approval)
**Author:** Claude-as-Forge (written 2026-05-28 morning, post-bootstrap-002 verification)
**Approver:** Larry (pending)
**Parent:** [agents/beacon/specs/orchestrator-bootstrap-002.md](orchestrator-bootstrap-002.md) (verification thread)
**Predecessor:** [docs/pr-s4-rectification-v1-brief.md](../../docs/pr-s4-rectification-v1-brief.md) (PR #145, shipped 2026-05-27 19:07 UTC)
**Successor:** `orchestrator-bootstrap-003` (verifier, separate spec)

---

## 1. Problem statement

PR #145 (PR-S4 rectification v1) shipped fixes for 14 wiring gaps identified by a deep audit + empirical run of `orchestrator-bootstrap-001`. The brief explicitly called for a follow-up synthetic verifier (`orchestrator-bootstrap-002`) to confirm all gaps closed.

`bootstrap-002` ran 2026-05-27 17:11-17:36 MDT and caught **6 real gaps** in PR #145's H1-H5 fixes — exactly what the verification was designed to surface. Step-verify-write (the first step) completed end-to-end in 2 min, but the chain then wedged on V6 (the largest gap) and has been stuck ~9h until manual cancel via the M1 helper.

This spec defines the v2 rectification PR that closes all 6.

---

## 2. The 6 gaps + verification context

| Gap | Severity | What broke | Verified during |
|---|---|---|---|
| V1 | medium | Beacon's DAG-preflight APPROVAL_REQUEST emitted without `phase: routing-signal` despite CLAUDE.md Discipline 3 mandating it | Larry's `approve` reply twice rejected with F24 "prompt too short" |
| V2 | low | `agents/build_sequence_advancer/` has only `IDENTITY.md`; sync_agent_core validator expects every `agents/*/` to have `SOUL.md` + `CLAUDE.md` | Sync.service fails every tick with 2 validation errors |
| V3 | medium | `scripts/marker.py render` template for APPROVAL_REQUEST has no slot for `phase` — Beacon physically cannot include `phase: routing-signal` via standard emission | Hotfixed via PR #149 prompt-prefix exemption; root cause still open |
| V4 | medium | Changes to `scripts/dispatch_validator.py` (and other shared-lib code) require restart of THREE daemons: beacon-bot, outbox-notifier, inbox-watcher. Discipline not documented; missed on first restart attempt | Verified empirically — first restart cycle missed beacon-bot; same rejection re-emitted until beacon-bot also restarted |
| V5 | low (behavioral) | H1's `_handle_mirror_dag_preflight_result` auto-transitions sequence status pending→active instead of DM-ing Larry the kickoff cue per spec § 4 ("Sequence ready for kickoff: `approve sequence X`") | Sequence transitioned to active without the spec'd DM; no harm, but spec and implementation now diverge |
| **V6** | **critical** | **build_sequence_advancer doesn't detect PR merge → step.status stays "dispatched" forever; advancer ticks 100+ times doing nothing while sequence is wedged** | **Step-verify-write merged at 17:36 MDT; sequence file still showed status=dispatched, merged_at=null at 02:30+ UTC the next day until manual cancel** |

---

## 3. Five locked decisions

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | V1+V3 root vs symptom | **Fix the root (V3) — extend `marker.py render` to accept a `phase` parameter; PR #149's prompt-prefix exemption stays as defense-in-depth** | V1 is downstream of V3; fixing V3 makes V1 self-correcting. The hotfix exemption from PR #149 stays as belt-and-suspenders. |
| B | V2 stub vs validator change | **Add minimal `SOUL.md` + `CLAUDE.md` stubs identifying build_sequence_advancer as a service-agent (Python daemon, not LLM)** | Cheaper than adding validator service-agent detection logic. Stubs explicitly document "this is a daemon, not LLM-driven" which is its own documentation value. |
| C | V4 discipline + automation | **(1) Document the multi-daemon restart discipline in a new runbook; (2) extend `heal_stale_daemon_code` (PR #142) with a shared-lib watchlist that auto-restarts dependent daemons** | Documentation alone repeats the bug. Automation alone is opaque. Both together: runbook is the spec, healer is the enforcement. |
| D | V5 spec vs implementation | **Implementation wins — update spec § 4 to match H1's auto-transition behavior; remove the "Beacon DMs the kickoff cue" prose** | Auto-transition is less friction. Larry explicitly approved the sequence at author-time; requiring a second approval after Mirror PASSes is friction without safety value. Update spec, keep handler. |
| E | V6 merge-detection mechanism | **Notifier-emitted signal — outbox-notifier calls a NEW `sequence_shortcut_helpers.apply_step_merged()` when it detects AUTO_MERGE on a task whose task_id matches a step in any active sequence file** | Notifier already detects AUTO_MERGE for the task; cheapest place to add the signal. Beats GitHub API polling (slow, rate-limited) and chain_events polling (extra read path). |

---

## 4. Detailed requirements per gap

### V3 — marker.py phase parameter

**File:** `scripts/marker.py`
- Extend the `render` command's APPROVAL_REQUEST type to accept `--phase <value>` flag.
- When provided, include `"phase": "<value>"` in the emitted JSON envelope.
- When omitted, no phase field is emitted (backward-compatible — existing callers continue working).
- Tests: `scripts/tests/test_marker.py` (NEW or extend) — assert phase field landed in JSON when flag set.

### V1 — Beacon CLAUDE.md update

**File:** `agents/beacon/CLAUDE.md` Discipline 3 section
- Update the prose to instruct Beacon to invoke `marker.py render approval_request --phase routing-signal ...` for DAG-preflight markers.
- Add a worked example showing the full command.
- No code change here — pure adherence guidance.

### V2 — build_sequence_advancer agent stubs

**Files (NEW):**
- `agents/build_sequence_advancer/SOUL.md` — ~30 lines. Title: "build_sequence_advancer (service-agent, no LLM)". Body explains: this is a Python daemon timer (every 5 min) that polls sequence files and dispatches steps. No LLM invocation. The IDENTITY.md (already present) carries the routing identity; this SOUL.md exists to satisfy the sync validator's per-agent-directory convention and to document "no LLM here, look at the daemon code instead."
- `agents/build_sequence_advancer/CLAUDE.md` — ~10 lines. Title: "Not applicable — service-agent." Body: "This agent is a Python daemon. No CLAUDE.md instructions are loaded by any LLM invocation. See SOUL.md and `scripts/build_sequence_advancer.py` for the operational logic."

### V4 — daemon-restart discipline + automation

**Files (NEW + extend):**
- `runbooks/post-merge-daemon-restart-discipline.md` (NEW) — documents the canonical mapping: any PR touching `scripts/dispatch_validator.py`, `scripts/safe_write_inbox.py`, `scripts/marker.py`, `scripts/routing_validator.py` (the shared-lib set imported by multiple long-running daemons) must restart `{beacon-bot, outbox-notifier, inbox-watcher}` after sync. Includes a table mapping shared-lib file → list of dependent services.
- `scripts/heal_stale_daemon_code.py` extend — add a `SHARED_LIB_WATCHLIST` constant mapping shared-lib file paths to sets of dependent service names. On each healer tick, if any watched file's mtime > a dependent service's ActiveEnterTimestamp + RACE_AVOIDANCE_SEC, auto-restart that service (same logic as the existing direct-script detection from PR #120). Tests: fixture sets file mtime > service start; assert restart attempt; existing tests pass (regression guard).

### V5 — spec § 4 update

**File:** `agents/beacon/specs/build-sequence-orchestrator.md` § 4
- Remove the prose "If Mirror returns PASS, Beacon DMs Larry: 'Sequence ready for kickoff: approve sequence X'."
- Replace with: "If Mirror returns PASS, the H1 handler (`_handle_mirror_dag_preflight_result` in outbox-notifier) auto-transitions the sequence file from `status: pending` → `status: active` and appends an audit_log entry. The build_sequence_advancer's next tick (≤5 min) dispatches the root step. No additional approval required — Larry already approved the sequence at author-time."
- Also update the section in `agents/beacon/CLAUDE.md` that describes the kickoff handoff if present.

### V6 — notifier-emitted step-merged signal (critical)

**Files:**
- `scripts/sequence_shortcut_helpers.py` extend — add `apply_step_merged(seq_id, step_id, pr_url, merged_at, actor) -> Result` function. Same shape as existing helpers (idempotent, atomic-write, audit_log entry `event: step-merged`). Updates step.status from "dispatched" → "merged", sets merged_at + pr_url, removes step_id from sequence.current_steps. Idempotent: if step.status already "merged", returns Result(applied=False, reason="already merged"). Tests: scripts/tests/test_sequence_shortcut_helpers.py extend.
- `scripts/outbox_notifier.py` extend — when AUTO_MERGE fires for a task, before logging the outcome, scan active sequence files in `~/agents/blackboard/build-sequences/*.json` and check if any active sequence has a step with `step_id == task_id`. If yes, call `apply_step_merged(seq_id, task_id, pr_url, datetime.now(timezone.utc).isoformat(), actor='notifier')`. Log the signal: `SEQUENCE_STEP_MERGED seq=<id> step=<id> pr=<url>`. Tests: extend `scripts/tests/test_outbox_notifier_sequence_handlers.py` with the AUTO_MERGE → step-merged trigger.

---

## 5. Success criteria

After v2 ships + bootstrap-003 runs, ALL of:

- bootstrap-003 (2 sequential steps, doc-only, identical shape to bootstrap-002) completes end-to-end with `status: complete`, both steps `status: merged`, both with correct `merged_at` + `pr_url`.
- Total wall-clock from `approve sequence bootstrap-003` (or auto-kickoff per V5) to `status: complete`: ≤30 min.
- No manual unstick required; no `cancel sequence` invocation needed.
- `heal_stale_daemon_code` correctly restarts dependent daemons within 30 min of a shared-lib file change on disk.
- Sync.service stops emitting V2's SOUL.md/CLAUDE.md missing errors on each tick.
- Spec § 4 prose matches H1's auto-transition behavior.

---

## 6. Out of scope (v3 or later)

- bootstrap-003 spec itself (separate doc, drafted after v2 ships)
- Cross-sequence dependencies (e.g., "sequence B depends on sequence A completing")
- Sequence pause/resume UX in the dashboard
- Parallel-step dispatch (the empirical bootstrap-001 had parallel siblings; bootstrap-002 was linear; v2 fix preserves parallel-step path but doesn't add new exercise)
- Mission Control UI surface for sequences (lives in the Missions tab spec when that drafts)

---

## 7. Files in scope (summary)

**ourliberty-agent-core (no dashboard changes):**

- `scripts/marker.py` — add `--phase` flag for approval_request type (V3)
- `agents/beacon/CLAUDE.md` — update Discipline 3 prose (V1)
- `agents/build_sequence_advancer/SOUL.md` (NEW) — service-agent stub (V2)
- `agents/build_sequence_advancer/CLAUDE.md` (NEW) — service-agent stub (V2)
- `runbooks/post-merge-daemon-restart-discipline.md` (NEW) — V4 documentation
- `scripts/heal_stale_daemon_code.py` — SHARED_LIB_WATCHLIST + auto-restart (V4)
- `agents/beacon/specs/build-sequence-orchestrator.md` § 4 — spec ↔ implementation alignment (V5)
- `scripts/sequence_shortcut_helpers.py` — `apply_step_merged()` (V6)
- `scripts/outbox_notifier.py` — AUTO_MERGE → `apply_step_merged()` trigger (V6)
- `scripts/tests/test_marker.py` (NEW or extend)
- `scripts/tests/test_heal_stale_daemon_code.py` (extend)
- `scripts/tests/test_sequence_shortcut_helpers.py` (extend)
- `scripts/tests/test_outbox_notifier_sequence_handlers.py` (extend)

No droplet endpoint changes; no Supabase migrations; no new env vars.

---

## 8. Test plan summary

- Unit tests per § 4 cover each gap fix.
- Integration: post-merge, run `heal_stale_daemon_code` dry-run with a fixture mtime > service-start; assert restart attempt for the 3 dependent services.
- Manual acceptance via bootstrap-003 verifier (separate dispatch after v2 merges).

---

## 9. Cost estimate

Best-guess Forge + Mirror chain spend: **$10-15**. Larger than today's average ($3-5) because the PR touches 8+ files across config, docs, scripts, tests + introduces a non-trivial new code path (V6 notifier → helper hook).

Mirror revision rounds expected: **0-1**. Each fix is well-scoped; no architectural ambiguity beyond decisions A-E already locked.

---

## End of spec
