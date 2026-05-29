# Spec: E4.4g — Operator Action Queue

**Status:** Draft (awaiting design pass)
**Author:** Forge (drafted 2026-05-28 from operator-UX backlog dispatch)
**Approver:** Larry (pending)
**Phase:** E4.4g of `docs/phase-e-plan.md` Phase E4
**Predecessor:** E4.4f (Missions Tab v1) — the panel lands inside the Missions route shipped by E4.4f.

---

## 1. Purpose

The dashboard's Missions tab (E4.4f) answers *"what are we building?"* but does not answer the more urgent question: *"what is the chain waiting on me for, right now?"*

The bootstrap-003 verifier surfaced the canonical instance on 2026-05-28: a skip-window message (Message 2 in § 4 of the orchestrator-bootstrap-003 spec) was queued waiting for operator action with no surface that called it out. Larry caught it manually only after a refetch round-trip. The same pattern repeats across at least five other operator-action shapes (paused sequences, stuck-dispatched steps, CLARIFY-exhausted PRs, NOW-severity alerts, drafting missions) — each lives in its own state file, none of them currently surface on the dashboard, and the only way an operator notices is by polling Telegram + GitHub + memory.

The Operator Action Queue is a left-rail sticky panel on the `/missions` route that **collects every chain state requiring operator action**, renders each as a row with type-badge + identifier + age + a copy-paste command, and gives a single answer to *"what does the chain need from me right now?"* in <30 seconds without leaving the dashboard.

---

## 2. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| a | Panel lives on `/missions` as a left-rail sticky panel — not a new top-level route. | E4.4f's Missions tab is the natural home; a new top-level route would split the operator's attention surface. |
| b | Read-only v1: copy-paste commands, NOT direct helper invocation buttons. | An audit trail of operator-driven shortcut invocations needs to settle before buttons are safe to wire — accidental click on `cancel sequence` is destructive. v2 lands buttons after one full chain-week of copy-paste usage with no incident. |
| c | Six action types in scope (see § 5). | These are the six pattern classes surfaced by the bootstrap-003 audit; later additions extend the panel via the same row contract. |

---

## 3. Why now

- **bootstrap-003 § 4 Message 2** was unreachable without a manual notification — surfaced as a doctrine gap on 2026-05-28.
- The broader audit (same day) flagged 5 sibling patterns where the operator depends on memory + polling to discover required action.
- E4.4f shipped the Missions surface; mounting the queue inside the existing route is a small additive change rather than a new tab.

---

## 4. Acceptance

- Visit `/missions` → see Action Queue panel pinned to the left rail.
- In <30s, an operator can answer *"what does the chain need from me right now?"* without consulting chat or GitHub.
- Each row displays: type-badge + identifier (task_id / seq_id / alert subject) + age (e.g. `12m`, `3h`) + copy-button-wrapped command + a details-expander that links to the underlying artifact.
- Empty state ("Nothing waiting on you") renders cleanly when all six queries return zero.
- The panel polls every 30s; manual refresh button forces immediate refetch.

---

## 5. Per-item-type spec

### 5.1 Pending Approvals

- **Source:** `~/agents/state/beacon-pending-approvals.json`.
- **Row identifier:** approval's `task_id` + one-line `summary`.
- **Command:** `approve <task_id>` (copy-paste into Telegram).
- **Details-expander:** full plan text + reply_chat_id.

### 5.2 Paused sequences

- **Source:** `~/agents/blackboard/build-sequences/*.json` where `status == "paused"`.
- **Row identifier:** sequence `id` + name field if present.
- **Command:** `resume sequence <seq_id>` OR `cancel sequence <seq_id>: <reason>`.
- **Details-expander:** last 5 audit_log entries + current_steps array.

### 5.3 Stuck-dispatched steps

- **Source:** sequence files where `step.status == "dispatched"` AND `(now - step.dispatched_at) > 15 min`.
- **Row identifier:** `<seq_id>/<step_id>` + dispatched-age.
- **Command:** the per-step skip-helper invocation (looks up `skip sequence <seq_id> step <step_id>: <reason>`) OR `cancel sequence <seq_id>: <reason>`.
- **Details-expander:** step.dispatch_text + the linked spec_doc anchor.

### 5.4 CLARIFY-exhausted PRs

- **Source:** Forge sessions where the notifier log shows `clarification-exhausted` within the last 30 min AND no PR has opened on the task's expected branch.
- **Row identifier:** task_id + last clarification question.
- **Command:** link to the rescue runbook (sibling mission `operator-ux-rescue-runbook`).
- **Details-expander:** all clarification round-trips for the task (Q/A pairs).

### 5.5 NOW-severity alerts

- **Source:** `larry_alerts` ledger entries with `tier == "NOW"` (depends on sibling `operator-ux-alert-taxonomy` landing first; fallback during interim: all alerts with `severity in {critical, warning}`).
- **Row identifier:** alert subject + age since first fire.
- **Command:** the alert's `recommended_action` string verbatim.
- **Details-expander:** alert body + linked artifact (journalctl, healer log, etc.).

### 5.6 Drafting missions

- **Source:** `missions.json` entries where `phase == "drafting"`.
- **Row identifier:** mission id + name.
- **Command:** *"review and promote to ready"* (no automation; link to `spec_docs[0]`).
- **Details-expander:** mission brief + spec_docs list.

---

## 6. Out of scope

- Direct helper invocation buttons (deferred to v2 per § 2 decision b).
- Mobile-optimized layout (desktop-first; mobile lands when the dashboard's mobile pass happens).
- Threshold tuning for the "stuck-dispatched" 15-min window (hardcoded in v1; Pulse can propose tuning later via a Check III sibling).
- Real-time WebSocket updates (v1 polls every 30s; WebSocket migration is a cross-dashboard concern, not Action-Queue-specific).
- Cross-operator routing (single-operator system today; multi-operator triage UI is far-future).

---

## 7. File list

**New components (ourliberty-dashboard):**

- `app/(dashboard)/missions/_components/OperatorActionQueuePanel.tsx` — the panel container; orchestrates the six row queries; renders empty-state.
- `app/(dashboard)/missions/_components/ActionRowCard.tsx` — single-row presentation: type-badge, identifier, age, command-with-copy, details-expander.
- `app/(dashboard)/missions/_components/ActionCommandCopyButton.tsx` — wraps the copy-to-clipboard interaction with a toast confirmation.

**New query helpers:**

- `lib/operator-queries.ts` exports:
  - `queryPendingApprovals(): Promise<PendingApproval[]>`
  - `queryPausedSequences(): Promise<PausedSequence[]>`
  - `queryStuckDispatchedSteps(): Promise<StuckStep[]>`
  - `queryClarifyExhaustedPRs(): Promise<ClarifyExhaustion[]>`
  - `queryNOWAlerts(): Promise<NOWAlert[]>`
  - `queryDraftingMissions(): Promise<DraftingMission[]>`

Each helper hits the dashboard's existing droplet-state API (`GET /api/system/...`) and returns the shape the row card expects.

**Page integration:**

- Minimal edit to `app/(dashboard)/missions/page.tsx` to mount `<OperatorActionQueuePanel />` in the left rail.

---

## 8. Test matrix

Vitest fixtures cover, for each of the six row types:

- A populated state renders the row correctly (badge text, age formatting, command string, expander payload).
- An empty state contributes nothing to the panel.
- The all-empty state renders the panel's empty-state copy.
- Clicking the copy button writes the expected command to the clipboard mock.
- The details-expander toggle reveals the expected payload and links to the right downstream artifact.

Snapshot tests are avoided in favor of per-field assertions (snapshots churn on copy tweaks).

---

## 9. Cost estimate

Single PR: $10–12 (Forge build) + 0–1 Mirror revision rounds (estimated $1–2). Total ceiling ~$14.

---

## 10. Migration

None. The panel is additive — it reads existing state files and existing droplet-state API endpoints. No schema changes, no data migrations, no config additions.

---

## 11. Followups (out of scope here, captured for the registry)

- v2: direct helper invocation buttons (after one chain-week of clean copy-paste audit trail).
- v2: mobile layout.
- Future: Pulse Check IX (sibling mission `pulse-check-ix-operator-friction`) may surface tuning proposals for the 15-min stuck-dispatched threshold.

**Enforcement:** Mirror review checklist item — confirm the panel's six queries each have a corresponding fixture-backed Vitest case and the empty-state path is exercised, before approving the PR.
