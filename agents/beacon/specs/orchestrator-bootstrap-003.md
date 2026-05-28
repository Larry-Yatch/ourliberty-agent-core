# Spec: orchestrator-bootstrap-003 — 6-gap rectification v2 verifier + first M1 helper exercise

**Status:** Draft (awaiting Larry approval — synthetic verifier, not a feature)
**Author:** Claude-as-Forge (written 2026-05-28)
**Approver:** Larry (pending)
**Parent:** [agents/beacon/specs/orchestrator-rectification-v2.md](orchestrator-rectification-v2.md) (PR #154, merged 2026-05-27)
**Predecessor:** [agents/beacon/specs/orchestrator-bootstrap-002.md](orchestrator-bootstrap-002.md) — surfaced the 6 V1-V6 gaps closed by rectification v2.

---

## 1. Purpose

PR #154 closed the 6 wiring gaps (V1-V6) surfaced by `orchestrator-bootstrap-002`. The single highest-value gap was V6 (advancer never detected PR merge → sequence wedged indefinitely). Bootstrap-003 is the first real end-to-end test of V6's notifier-emitted step-merged signal under a hands-free linear sequence, and the first real exercise of two M1 sequence shortcut helpers (`skip`, `retry`).

This is a **synthetic fixture**, not a feature delivery. The output (a small verify-log file + a deliberately-skipped synthetic step) is irrelevant; what matters is the path exercised.

---

## 2. What bootstrap-002 didn't reach that this covers

| Coverage gap in v002 | How v003 closes it |
|---|---|
| V6 — notifier `apply_step_merged()` trigger never ran (v002 wedged at first AUTO_MERGE) | v003 runs at least two AUTO_MERGE events through the notifier signal path; both must transition `step.status: dispatched → merged` within one advancer tick |
| V1+V3 — Beacon's DAG-preflight phase routing-signal | v003 author-pass must emit APPROVAL_REQUEST with `phase: routing-signal` via the new `marker.py render --phase` plumbing |
| V5 — H1 auto-transition (pending → active) on Mirror DAG PASS | v003 explicitly asserts sequence file flips to `status: active` without a second Larry approval |
| M1 skip helper — never exercised substantively | v003 declares a synthetic third step intended to be skipped; the operator runs `skip sequence` while that step is `pending`, exercising the substantive mutation path |
| M1 retry helper — never exercised | v003 runs `retry sequence` against a step in `merged` status, exercising the documented immutable-step WARN-no-op idempotency contract (substantive failure-path retry is deferred to bootstrap-004 once a failure-injection harness exists) |

V2 (build_sequence_advancer agent stubs) and V4 (shared-lib daemon-restart automation) are infrastructure-state gaps verified by pre-kickoff inspection, not by the run itself (see § 5 pre-flight).

---

## 3. The 3 steps

Three steps in a linear DAG. Two execute as PRs; the third is the planned-skip target. All target `ourliberty-agent-core`. All doc-only.

### Step 1: `step-v003-write`

- **dispatch_text:** `Create docs/orchestrator-bootstrap-003-verify.log (new file) with exactly one line: "step-v003-write completed at <UTC iso timestamp>". No other files; no other changes. Synthetic verifier per orchestrator-bootstrap-003 spec — keep it absolutely minimal.`
- **target_repo:** `ourliberty-agent-core`
- **task_type:** `doc-only`
- **depends_on:** `[]`

### Step 2: `step-v003-append`

- **dispatch_text:** `Append a second line to docs/orchestrator-bootstrap-003-verify.log: "step-v003-append completed at <UTC iso timestamp>". File already exists (created by step-v003-write). No other files; no other changes. Synthetic verifier per orchestrator-bootstrap-003 spec.`
- **target_repo:** `ourliberty-agent-core`
- **task_type:** `doc-only`
- **depends_on:** `["step-v003-write"]`

### Step 3: `step-v003-skip-target` (planned-skip)

- **dispatch_text:** `Append a third line "step-v003-skip-target completed at <UTC iso timestamp>" — but per spec § 4 this step is intentionally skipped at runtime to exercise the M1 skip helper; dispatch_text is here only so the validator accepts a non-empty step.`
- **target_repo:** `ourliberty-agent-core`
- **task_type:** `doc-only`
- **depends_on:** `["step-v003-append"]`

---

## 4. Operational flow

Three Beacon messages from Larry. Steps 1+2 run hands-free; step 3 is skipped.

**Message 1 — author + DAG-preflight (same shape as bootstrap-002 § 4 Message 1, pointing at v003 spec).** Beacon writes the sequence file, emits `marker.py render approval_request --phase routing-signal ...` for the DAG-preflight to Mirror. Mirror PASSes; H1 handler auto-transitions `status: pending → active` (V5 assertion point). No second Larry message required to kick off.

**Hands-free interval 1.** Advancer dispatches step-v003-write; Forge builds; Mirror reviews; AUTO_MERGE. Notifier's `apply_step_merged()` fires (V6 assertion point #1); advancer's next tick dispatches step-v003-append.

**Hands-free interval 2.** step-v003-append builds, reviews, AUTO_MERGEs. `apply_step_merged()` fires (V6 assertion point #2). Sequence still active; current_steps now empty; step-v003-skip-target is dispatchable.

**Message 2 — skip exercise (BEFORE the next advancer tick, ≤5 min window).** Larry: `skip sequence orchestrator-bootstrap-003 step step-v003-skip-target, planned-skip per spec § 4`. M1 `apply_skip()` runs substantively: step.status `pending → merged`, merged_at populated, audit_log `step-skipped` entry. Advancer's next tick sees all steps merged → sequence `status: complete`, completion DM.

**Message 3 — retry idempotency probe.** Larry: `retry sequence orchestrator-bootstrap-003 step step-v003-write`. M1 `apply_retry()` returns `applied=False, reason=…immutable per spec § 5.3`. Beacon DMs the WARN no-op. No sequence-file mutation.

---

## 5. Pre-flight checks (V2 + V4 infrastructure state)

Before kickoff, verify on the droplet:

- `ls ~/agent-core/agents/build_sequence_advancer/{SOUL.md,CLAUDE.md}` — both exist (V2).
- `systemctl status ourliberty-sync-agent-core` — last invocation shows zero validator errors for `agents/build_sequence_advancer/` (V2).
- `grep SHARED_LIB_WATCHLIST ~/agent-core/scripts/heal_stale_daemon_code.py` — constant present with the four shared-lib paths + their dependent service sets (V4).
- `systemctl is-active ourliberty-{beacon-bot,outbox-notifier,inbox-watcher,build-sequence-advancer}` — all `active` (V4 + advancer liveness).

If any pre-flight fails, do not kick off — file as a rectification-v3 candidate first.

---

## 6. Success criteria

ALL of:

- `~/agents/blackboard/build-sequences/orchestrator-bootstrap-003.json` ends with `status: "complete"`.
- step-v003-write + step-v003-append both end with `status: "merged"`, populated `merged_at` + `pr_url`.
- step-v003-skip-target ends with `status: "merged"`, populated `merged_at`, `pr_url: null`; audit_log carries one `step-skipped` event with `actor: "larry"`.
- audit_log carries: `sequence-created`, `dag-preflight-passed`, `kickoff-acknowledged` (or equivalent H1 auto-transition event), `step-merged` ×2 (one per real PR), `step-skipped` ×1, `sequence-complete`. No `gate-mismatch` events. No `step-failed` events.
- 2 PRs land cleanly on `ourliberty-agent-core` main, each adding one line to `docs/orchestrator-bootstrap-003-verify.log`.
- No `pipeline-stall:*` alerts; no manual cancel / unstick; no Tier 2 fallback triggered.
- `retry sequence … step step-v003-write` DMs the immutable-step WARN no-op without mutating the sequence file (check `git diff` on the sequence file vs. mtime).
- Wall-clock from H1 auto-transition to `status: complete`: ≤45 min (allows for skip-window operator pause).

---

## 7. Out of scope

- Substantive failure-path retry (requires a deliberate failure-injection mechanism — bootstrap-004).
- pause + resume helpers (separately verifiable; not the critical path V6 needed).
- cancel helper (already exercised at the close of bootstrap-002).
- Multi-branch parallel DAG — bootstrap-002 + 003 both linear; parallel verifier is its own bootstrap.
- Cross-repo sequences (V1 single-repo constraint per orchestrator spec § 4).

---

## 8. Gap log discipline

During run, every surfacing finding is captured immediately in a `bootstrap-003 gap log` table in the operator's chat: timestamp + severity (critical/medium/low) + finding + where surfaced. If any gap surfaces, the sequence is cancelled with reason and the findings roll forward to a rectification-v3 spec — do NOT attempt to unstick mid-verification.

---

## End of spec
