# Spec: CLARIFY round visibility on Missions cards

**Status:** Draft awaiting design pass
**Author:** Forge (stub from operator-UX backlog, 2026-05-28)
**Parent registry entry:** `agents/beacon/missions.json#operator-ux-clarify-visibility`

---

## 1. Purpose

Surface the Forge↔Beacon CLARIFY round-trip Q&A on Missions-tab card side-panel. Today the operator only learns CLARIFY-exhausted post-mortem from logs. Read-only v1; later optional 'intervene' affordance.

The clarification budget can exhaust silently from the operator's perspective: Beacon answers each clarification in-scope, Forge accepts, the cycle repeats until budget is gone, and only then does Larry hear about it via a closing DM. Surfacing the Q&A in real-time would let an operator intervene before the budget is spent.

---

## 2. Sketch

- New side-panel section on each Missions-tab card titled *"Clarifications"* — populated when the underlying task has ≥1 clarification round-trip.
- Source: the outbox notifier log, parsed for `intent=clarify` and `intent=clarification-response` envelopes referencing the card's task_id.
- Each round renders as Q (Forge's question) / A (Beacon's answer), timestamped, with the running budget shown (`2 of 3 used`).
- Live update on each new round (polling, same 30s cadence as the Action Queue panel).
- v2 (deliberately out of scope): an *"intervene"* affordance that lets the operator answer a pending clarification directly, pre-empting Beacon's response. Requires routing + auth design that does not exist yet.

---

## 3. Open questions

- Where does the side-panel persist beyond task completion? (Probably a snapshot on the merged-PR card; needs a small archive surface.)
- Should the panel hide for tasks that completed with 0 clarifications, or always render an empty state?
- For Pulse-promoted ESCALATE flows (where the clarification budget exhausted), does the side-panel link to the resulting replan APPROVAL_REQUEST?
- Privacy: any clarification content (e.g. file paths, credential names) that should be redacted before display?

---

## 4. Acceptance (rough)

- A task with 2 clarification rounds renders both Q/A pairs on the corresponding Missions card.
- The budget counter accurately reflects `clarification_count / max_clarifications` from the envelope.
- The panel updates live as new clarification rounds happen, without requiring a page refresh.

---

## 5. Estimated cost + sizing

Log-parser query helper + side-panel component + live-update polling: ~$8–10. One PR. Mirror revisions expected 0–1. Sizing: small-to-medium; the design work is the log-parsing contract and the v2 intervene-button deferral boundary.
