# Spec: Operator "Needs-You" Feed — complete the Waiting-on-You surface

**Status:** Draft (awaiting Larry's design pass)
**Author:** Claude (Larry-session), 2026-06-24
**Approver:** Larry (pending)
**Supersedes:** `agents/beacon/specs/e4-4g-operator-action-queue.md` (archived `legacy-drafting-never-advanced` 2026-06-17). That spec proposed a *new* left-rail panel on `/missions`. Since it was written, `/where-we-are`'s "Waiting on You" panel + the doorbell DM became the real front door, and the approvals class shipped with live buttons. This spec finishes that surface instead of rebuilding it.

---

## 1. Purpose

The `/where-we-are` "Waiting on You" panel + the doorbell DM answer *"what does the chain need from me right now?"* — but today they only itemize **approvals** and **parked captures**. Two classes that most need Larry never reach the board; they only scroll past in Telegram:

- **Critical alerts the healers couldn't auto-fix** (the alert-toil bar).
- **Forge stuck after exhausting its clarification budget** (CLARIFY-exhausted builds).

And **paused / stuck build sequences** are visible only on the separate `/operations/build-sequences` ladder, with steering available only via Telegram shortcuts.

This spec makes the board + doorbell the complete answer: every genuinely-needs-Larry item lands there, and paused/stuck sequences carry **live steering buttons** (resume / skip / cancel) inline.

The key discovery that shapes this spec: **the "needs-you" gate already exists.** `scripts/promote_alerts.py` already classifies which escalations cross the "needs CEO attention" bar (critical AND not self-resolved within a cycle, OR explicitly for-Larry) and emits a `needs_attention` chain_event consumed by `/live`. We do not build a new classifier — we feed the board + doorbell from the same gate, and add the two missing producers.

---

## 2. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| a | The front door is `/where-we-are`'s "Waiting on You" panel + the doorbell — NOT a new panel on `/missions`. | That surface already exists and is where Larry looks; a second surface splits attention. Supersedes e4-4g decision (a). |
| b | Reuse the existing `promote_alerts` gate; do NOT invent a second classifier. | The alert-toil rule (critical + not-self-resolved, or explicit-for-Larry) is already written and running. One gate → three surfaces (`/live`, `/where-we-are`, doorbell). No double-pinging risk. |
| c | The for-Larry escalation signal is a **durable local file** read by `system_state_log`, written by the gate. | The doorbell counts escalations off the local `system-state-log` snapshot, NOT off Supabase (`system_state_log` is deliberately local-files-only — "No HTTP; no Supabase"). A durable local signal keeps board + doorbell on ONE substrate. This is exactly the "canonical for-Larry signal" `load_for_larry_escalations`'s docstring already anticipates. |
| d | The signal is **self-clearing**, not append-only. | Each item retracts when its trigger clears (alert self-resolves; build re-dispatched). Append-only would leave stale "needs you" rows that never retract — the failure mode Larry has flagged on `larry_alerts`. |
| e | Sequence rows carry **live buttons** (resume / skip / cancel), wired through a new audited droplet endpoint that calls the existing `sequence_shortcut_helpers`. | Larry chose buttons-from-start. The button→action pattern (`/api/larry/action`) and the `apply_*` helpers both already exist and are battle-tested; this is wiring, not new mechanism. Reverses e4-4g decision (b)'s copy-paste-first posture on the strength of that reuse. |
| f | Destructive actions (`cancel`) require a confirm step in the UI before firing. | An accidental click on `cancel sequence` is destructive. Reuse the approvals-button confirm UX. |

---

## 3. Why now

- The board + doorbell are live and trusted, but blind to two needs-you classes — so Larry still has to watch Telegram for the things that most need him. That defeats the doorbell's purpose.
- The gate (`promote_alerts`) and the sequence verbs (`apply_*`) and the button pattern (`/api/larry/action`) are all already built. The remaining work is connective, not foundational.
- Closing this directly serves the alert-toil principle (only un-auto-handled criticals reach Larry) and the doorbell investment.

---

## 4. Acceptance

- A critical alert the healers could NOT auto-resolve within a cycle appears on `/where-we-are` as an `escalation` row AND increments the doorbell count.
- A benign / auto-handled / self-resolved alert does NOT appear and does NOT ring the doorbell. (Alert-toil bar preserved.)
- A CLARIFY-exhausted build appears as an `escalation` row with a plain-English "Forge is stuck — needs a scope decision" why, and a deep-link to the task.
- When the underlying trigger clears (alert self-resolves; build re-dispatched), the row disappears within one tick — no stale rows.
- A paused sequence and a stuck-dispatched step each appear as a `sequence` row with live **Resume** / **Skip** / **Cancel** buttons.
- Clicking Resume on a paused sequence resumes it (the existing `apply_resume` runs on the droplet); the row clears on the next tick. Clicking Cancel prompts a confirm first. Every click writes a `larry_action` audit row.
- The `/operations/build-sequences/[seq_id]` ladder detail page exposes the same buttons.

---

## 5. Per-item spec

### 5.1 Escalation — critical alert that failed auto-reconcile (Part A)

- **Producer:** `scripts/promote_alerts.py`. When an escalation crosses the existing bar (and the cross-cycle probation confirms it did not self-resolve), in addition to emitting the `needs_attention` chain_event it ALSO writes a durable for-Larry record to the canonical local signal file (decision c).
- **Reader:** `system_state_log.load_for_larry_escalations()` — already reads `for_larry: true` entries; point it at (or fold in) the canonical signal file.
- **Row:** `source='escalation'`, severity badge, headline as title, the escalation's `suggested_action`/`context` as why, deep-link to the underlying artifact.
- **Clearing:** the record carries the escalation's dedup identity; when `promote_alerts` next observes that identity has left the snapshot (self-resolved) it marks the record `resolved: true` (decision d).
- **Enforcement:** the `promote_alerts` probation state machine already owns "did this self-resolve?"; the durable-write + resolve-clear is a same-function addition guarded by its existing idempotency. Tests assert: crosses-bar→record written; self-resolves→record cleared; below-bar→no record.

### 5.2 Escalation — CLARIFY-exhausted build (Part A)

- **Producer:** `scripts/outbox_notifier.py` `clarification-exhausted` terminal-intent handler (the path that already auto-DMs Larry). It additionally writes a durable for-Larry record (task_id, last clarification question, repo).
- **Row:** `source='escalation'`, title "Forge is stuck on `<task>`", why = "Exhausted its questions — needs a scope decision", deep-link to the task / PR branch.
- **Clearing:** the record clears when a fresh dispatch for that task_id is observed (Beacon re-dispatched, or Larry dropped it). Backstop: `heal_pipeline_stall` already tracks stuck tasks; reuse its observation rather than a timer.
- **Enforcement:** the write rides the existing terminal-intent handler (no new poll). Test asserts: clarification-exhausted→record written with the task_id; re-dispatch→record cleared.

> **Sibling producer (cross-ref).** A third escalation producer feeds this same `promote_alerts` "needs-you" gate → Waiting-on-You panel + doorbell: `mirror-review-visibility.md` Contract C routes the *action-needed* session-less-PR escalation (a red `mirror-review` PR gone quiet with no self-heal) as a durable, self-clearing for-Larry record through this gate — reusing the §5.1/§5.2 producer pattern and decisions c/d, so it lands on this surface with no rework here. See that spec's Contract C and its §9 soft-dependency on this feed.

### 5.3 Sequence — paused sequence (Part B)

- **Source:** `system_state_log.load_active_sequences()` already returns `status=='paused'` sequences. Itemize them into `waiting_on_larry.items` as `source='sequence'`.
- **Row:** title = sequence id/name, why = "Paused — waiting on you to resume or cancel", age, **Resume** / **Cancel** buttons.
- **Enforcement:** new `load_waiting_sequences()` (or extension of the waiting builder); test asserts a paused sequence yields exactly one `source='sequence'` item with the resume/cancel actions and an active sequence yields none.

### 5.4 Sequence — stuck-dispatched step (Part B)

- **Source:** sequence files where `step.status=='dispatched'` AND `(now - step.dispatched_at) > 15 min` (the threshold the e4-4g spec named; hardcoded v1).
- **Row:** title = `<seq_id> / <step_id>`, why = "Step dispatched `<age>` ago with no PR — may be stuck", **Skip** / **Cancel** buttons.
- **Enforcement:** same builder + the 15-min threshold as a module constant; test asserts a fresh dispatched step yields nothing and a 16-min-old one yields a `sequence` item with skip/cancel.

### 5.5 The steering endpoint (Part B, the one new backend piece)

- **New:** `POST /api/system/build-sequences/{seq_id}/action` in `scripts/dashboard_api.py`, body `{action: resume|skip|cancel|retry, step_id?: str, reason?: str}`.
- Validates the action against an allowlist, dispatches to the matching `sequence_shortcut_helpers.apply_*`, and writes a `larry_action` audit row keyed on `(seq_id, action, ts)` — mirroring `/api/larry/action` exactly.
- Idempotency + atomic-write + audit_log shape are already guaranteed by the `apply_*` helpers; the endpoint is a thin authed wrapper.
- **Enforcement:** the action allowlist + the helper delegation; test asserts an unknown action 400s, a valid action runs the helper + writes the audit row, and a missing sequence 404s.

---

## 6. Out of scope

- A new top-level route or panel (superseded — we extend `/where-we-are`).
- Promoting `larry_alerts.jsonl` / `sentinel-alerts.jsonl` directly (they already DM Larry; promoting again double-surfaces — `promote_alerts`'s docstring is explicit about this). We feed off the *escalation snapshot* gate only.
- Tuning the 15-min stuck threshold (hardcoded v1; a Pulse Check sibling can propose tuning later).
- Real-time push (the panel's existing poll cadence is fine).

---

## 7. File list

**agent-core (backend):**
- `scripts/promote_alerts.py` — durable for-Larry write + resolve-clear on the existing gate (§5.1).
- `scripts/outbox_notifier.py` — for-Larry write on the clarification-exhausted handler (§5.2).
- `scripts/system_state_log.py` — reader fold-in (§5.1) + sequence itemization (§5.3, §5.4).
- `scripts/dashboard_api.py` — `POST /api/system/build-sequences/{seq_id}/action` (§5.5).
- (Canonical signal file path: a durable file under `~/agents/blackboard/`, single-writer per decision c — exact name Beacon's call.)

**ourliberty-dashboard (render):**
- `app/where-we-are/page.tsx` + `app/where-we-are/waiting-helpers.ts` + `lib/types.ts` — add `'sequence'` to `WaitingItemSource`, badge, `linkFor`, and the buttons.
- `app/operations/build-sequences/[seq_id]/client.tsx` — same buttons on the ladder detail page.
- A small action client that POSTs to the new endpoint with a confirm step on `cancel`.

**doorbell:** no change — it already counts `waiting_on_larry.escalations`; once the local signal is fed (decision c) the doorbell rings automatically.

---

## 8. Build sequence (proposed DAG)

- **Step 1 — agent-core, Part A (escalation feed):** §5.1 + §5.2 + the §5.1 reader fold-in + self-clearing. Lights up the board + doorbell for critical-unhandled + CLARIFY-exhausted. No `depends_on`.
- **Step 2 — agent-core, Part B (sequence itemization + endpoint):** §5.3 + §5.4 + §5.5. No `depends_on` (independent of Step 1 — different files/concerns).
- **Step 3 — dashboard render:** escalation rows (needs Step 1's shape) + sequence rows & buttons (needs Step 2's endpoint). `depends_on: [step-1, step-2]`.

Steps 1 and 2 run in parallel; Step 3 follows both.

---

## 9. Test matrix

Per producer/reader: populated→row, empty→nothing, trigger-cleared→row retracts. Endpoint: valid action runs helper + audit row; unknown action 400; missing sequence 404; cancel path exercised. Dashboard (vitest): each `source` renders its row + correct buttons; cancel shows a confirm; the all-empty state renders the calm "all caught up" copy.

---

## 10. Cost & migration

- Cost: ~2 agent-core PRs + 1 dashboard PR. Modest — the gate, the verbs, and the button pattern are all reused.
- Migration: none. Additive — reads existing state, adds one durable local signal file, one new endpoint. No schema change, no new credential.

---

## 11. Enforcement (doctrine-of-doctrine)

Every rule-bearing paragraph above pairs with a mechanism (the producer write, the reader, the allowlist, the `apply_*` helpers, the Mirror test-matrix check). The cross-cutting guarantee: the self-clearing requirement (decision d) is enforced by the resolve-clear assertions in §5.1/§5.2 tests — a record that doesn't retract when its trigger clears fails the suite, so a stale "needs you" row cannot ship.
