# Spec: Missions v2 — Delegate endpoint + chat-label fix (Phase 4 completion)

**Status:** Draft — ready to sequence
**Author:** Claude Code (desktop session, 2026-06-15)
**Approver:** Larry (found in the 2026-06-15 live button test; approved the fix)
**Parent:** [Phase 4 — operator meaning layer](missions-v2-phase4-meaning-layer.md) §7 (one-click delegate)
**Build path:** build-sequence orchestrator, two-repo (agent-core + dashboard), independent steps

---

## 1. Purpose

The live button test (2026-06-15) found the **primary** Parked-card action — **"Delegate to team"** — is broken: it POSTs `/api/missions/captures/{id}/delegate` and gets a **404**. The dashboard UI + proxy route shipped (#54), but the **droplet endpoint was never implemented** in `dashboard_api.py`. Without it, the centerpiece "one click hands the work to the team" promise doesn't work. This spec builds that endpoint, and fixes a small chat-label wart found in the same test.

**Done-gate:** clicking **Delegate to team** on a parked card hands the work to Beacon (a `human-approval-gate` proposal lands; the dashboard shows "Delegated to the team." with no 404), and the card chat labels the operator's own message **"You"** (not "Forge asks").

---

## 2. Contract A — droplet `POST /api/missions/captures/{id}/delegate`

Mirror the existing capture-action route (`POST .../action`, `dashboard_api.py` ~L6227) — same auth (`_require_token` for `X-Dashboard-Token` + `_require_actor` for `X-Actor`), same `_find_capture` + `_require_parked` guards.

- **Body:** `{ action?: "delegate"|"promote"|"drop"|"snooze" }` — optional; defaults to the capture's `recommended_action`, else `"delegate"`.
- **Action:** emit a **`human-approval-gate` proposal** for Beacon — reuse the `_handle_capture_message` envelope pattern (#502) + `safe_write_inbox.safe_write_inbox(target_agent='beacon', …)`. The proposal envelope carries the `APPROVAL_REQUEST` required fields (`beacon_approval_handler.REQUIRED_FIELDS`): `task_id` (derived from capture id), `target_agent='beacon'`, `summary` (one-line from the capture's `briefing.suggest`/title), `prompt` (run-down instructions: the title + briefing + `recommended_action`, "scope and propose/run this down"). Carry `capture_id`, `actor`, a dedup identity, and a timeout, like the message envelope.
- **Recommend-first:** Larry's click **is** the go. Delegate hands to Beacon; whether the resulting *dispatch* auto-fires or asks again is governed by `trust_policy` (the existing gate) — this endpoint just creates the proposal.
- **No captures.json mutation** — the capture stays `parked`; the delegation lives as a Beacon proposal (inbox + `beacon-pending-approvals.json`), not a capture state. (No schema change.)
- **Response:** `CaptureDelegateResponse` — `{ dispatched: true }` on success (or `{ pr_url }` only if a future action routes PR-backed). `404` if no such capture; `409` if not parked; `401` bad token/actor.
- **Idempotency:** a re-POST for a capture that already has an open delegate proposal collapses onto it (don't double-propose) — mirror the message handler's dedup.

## 3. Contract B — capture-card chat labels

The card chat reuses `ClarifyRoundDrawer`, which hard-codes **"Forge asks" / "Beacon answers"** (`ClarifyRoundDrawer.tsx` L61/L71). On a capture-card thread the asker is the operator, not Forge.

- Add optional props `questionLabel?: string` / `answerLabel?: string` to `ClarifyRoundDrawer` (defaults `"Forge asks"` / `"Beacon answers"` — real CLARIFY rounds unchanged).
- The Parked card mounts the drawer with `questionLabel="You"` `answerLabel="Team"`.
- Pure presentational change; no data/contract change (capture-thread rounds already carry `session_id: null`, distinguishing them from real CLARIFY).

---

## 4. Build plan — 2 steps (independent, parallel)

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1 — delegate endpoint** | agent-core | `POST /api/missions/captures/{id}/delegate` per Contract A (reuse action-route auth/guards + the message-handler `safe_write_inbox` proposal path); response model; tests | — |
| **2 — chat-label fix** | dashboard | `questionLabel`/`answerLabel` props on `ClarifyRoundDrawer`; Parked card passes "You"/"Team"; tests | — |

Both depend only on existing code (no cross-dependency) → dispatch in parallel.

## 5. Test / proof plan

- **1:** POST delegate on a parked capture → an `APPROVAL_REQUEST` envelope lands in Beacon's inbox with the required fields; returns `{dispatched: true}`; 404 on unknown capture; 409 on non-parked; re-POST dedups (no second proposal).
- **2:** capture-card thread renders the operator's message as "You" and the reply slot as "Team"; a real Forge CLARIFY round still renders "Forge asks"/"Beacon answers".
- **End-to-end (post-merge, droplet restart):** click Delegate on a real parked card → "Delegated to the team." toast, no 404, proposal visible to Beacon.

## 6. Out of scope

- Auto-closing/closing the card on delegate, or showing delegated-status on the card → **Phase S** (two-way sync; that's where a delegated card tracks its spawned work).
- The deploy step: `dashboard_api.py` change needs a **`dashboard-api` restart** on the droplet after merge (the long-lived process serves stale code) — a known deploy action, not a build step.
