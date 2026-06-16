# Spec: Build-Sequence Step Stall — detect + recover, never sit silently

**Status:** ready for build
**Author:** Beacon
**Date:** 2026-06-16
**Origin:** PR #532 (sequence `missions-v2-delegate-fix`, step `delegate-endpoint`) sat stalled for hours. Mirror returned a REVISION; the auto-dispatched revision died on a marker-format error (missing `Revision N applied:` preamble) and dead-lettered to Beacon, which journaled it without recovering or escalating. No healer caught it. Larry found it by chance on the dashboard.

## 1. Invariant (locked) — and why it cannot live in memory

No build-sequence step may sit in a non-terminal in-flight state (`dispatched`) without, within a bounded time, EITHER making progress, self-recovering, OR surfacing to Larry as an actionable alert. **The enforcement must be a standing, timer-driven healer** — never a human (or agent) remembering that a specific incident happened. "Would we notice next time?" must be answered by infrastructure that checks every active sequence on every tick, not by recall.

**Enforcement:** §6.

## 2. The two gaps (worked example: PR #532, 2026-06-16)

- **Gap A — no detection of a stalled `active`-sequence step.** `scripts/heal_pipeline_stall.py` `check_stalled_pending_sequence` only fires for sequences with `status == 'pending'` stuck on a *DAG-preflight* revision (line ~1527 short-circuits on any non-`pending` status). An `active` sequence with a *step* stuck in `status: dispatched` (its build/revision dead-lettered) has ZERO coverage. So a step-level stall is invisible to every monitor.
- **Gap B — no recover-or-route on a step dead-letter.** When the step's revision dead-lettered to Beacon, it was journaled-and-archived with no re-dispatch and no escalation. The recover-or-route doctrine (Shape 10 / M2 / M4 — a stuck auto-loop step routes to the owning agent for re-dispatch, never dead-ends to a human) was never applied to the dead-lettered-step-revision path.

(Proximate trigger — Forge's revision outbox lacked the strict `Revision N applied:` preamble at `outbox_notifier.py:248` — is explicitly OUT OF SCOPE per §5; this spec makes the STALL self-healing regardless of what triggered it.)

## 3. Fix A — detect a stalled active-sequence step (standing healer)

Extend `scripts/heal_pipeline_stall.py` with a new check that scans `~/agents/blackboard/build-sequences/*.json` for: `status == 'active'` AND a step in `status: dispatched` whose `dispatched_at` is older than a threshold (`STALLED_ACTIVE_STEP_MIN`, default ~30m) AND the step shows no forward progress (no `merged_at`/`pr_url` advance; and/or a dead-letter or `.invalid` task file exists for it). Reuse the existing alert + `ALERT_DEDUP_HOURS` cooldown machinery; dedup keyed on `stalled_active_step:<seq_id>:<step_id>:<dispatched_at>` so a fresh dispatch re-arms. The alert is Larry-actionable (names the seq/step/PR and the recommended recovery).

## 4. Fix B — recover-or-route a dead-lettered step revision (self-heal first)

When a sequence-step revision dead-letters (the §2 Gap-B path), route it for AUTONOMOUS recovery instead of passive journaling, mirroring the existing recover-or-route handlers (`_route_no_session_revision_to_beacon`, `_handle_mirror_dag_preflight_result`):
- Reconstruct the recovery from durable truth: Mirror's findings live in the `review-revision` notify / `chain_events`; the existing PR branch + url are on the envelope.
- Auto re-dispatch a FRESH build task that applies Mirror's findings to the EXISTING PR branch (checkout branch → apply → push so the PR auto-updates → Mirror re-reviews) — exactly the manual recovery performed for #532.
- Escalate to Larry as an actionable alert ONLY if recovery cannot fire (no reconstructable findings/branch) or repeatedly fails. Recover-then-alert, never alert-only-and-sit.

Whether Fix B lives in the notifier's dead-letter path or as a remediation in the Fix-A healer is the author's call at preflight — pick the one place that makes recovery deterministic and idempotent.

## 5. Out of scope

- **Loosening the revision-preamble gate** (`outbox_notifier.py:248`, "strict per Larry's signoff"). The strictness deliberately prevents Forge from claiming a revision it didn't apply; relaxing it is a separate, explicit decision — NOT bundled here. This spec makes the stall self-healing whatever the trigger.
- The DAG-preflight-pending stall shape — already covered by `check_stalled_pending_sequence`.
- Changing the happy-path revision loop (`_dispatch_revision_to_forge`).

## 6. Success criteria + enforcement

- A standing healer tick detects an `active` sequence step stuck `dispatched` past the threshold and surfaces it (replaying the #532 shape → an alert/recovery fires within one cycle, not hours).
- A recoverable dead-lettered step-revision auto re-dispatches to the existing PR branch with no human action; an unrecoverable one fires ONE actionable Larry alert (never silent, never a loop).
- A healthy in-flight step (recently dispatched, progressing) is left alone — no false stall alerts.
- **Enforcement:** the new check is a standing `heal_pipeline_stall` timer pass (the system's memory, not anyone's recall) + the recover-or-route handler; tests assert: stuck-step→detected; recoverable→auto-re-dispatch; unrecoverable→one actionable alert; healthy→untouched. A Mirror review-checklist note flags any future sequence-step state added without stall coverage.

## 7. Build plan (sequence after this spec merges)

- **Step 1 — detection.** Add the stalled-active-step check to `heal_pipeline_stall.py` + tests. (agent-core; standalone, ships value immediately — the alert alone ends the silence.)

- **Step 2 — recover-or-route.** Add the dead-lettered-step-revision auto-re-dispatch (recover-then-alert) + tests. (agent-core) depends_on Step 1.

Linear (Step 2 builds on Step 1's detection). Single-repo (agent-core).
