# Spec: Claude-as-Forge Rescue Runbook + Dashboard Trigger

**Status:** Draft awaiting design pass
**Author:** Forge (stub from operator-UX backlog, 2026-05-28)
**Parent registry entry:** `agents/beacon/missions.json#operator-ux-rescue-runbook`

---

## 1. Purpose

Document the out-of-chain manual rescue path operator uses when chain CLARIFY-exhausts (test-isolation-v3, Missions PR-A, fixture-gate #169 patterns). Add Missions-tab card affordance to trigger rescue. Closes tribal-knowledge gap.

Today the rescue procedure lives only in Larry's head: when Forge exhausts her clarification budget on a dispatch, Larry opens a fresh chat with Claude in Forge-mode, hands over the dispatch + the clarification trail, and Claude finishes the work as a manual session. The path works but is undocumented, undiscoverable to a new operator, and produces no audit trail.

---

## 2. Sketch

- New runbook at `runbooks/claude-as-forge-rescue.md` covering: when to invoke (CLARIFY-exhausted in last 30 min, no PR opened, severity-NOW), the exact chat-bootstrap prompt template, the file checklist (clarification trail, original APPROVAL_REQUEST, target_repo state), and the closing handoff (commit message convention referencing the rescue).
- New Missions-tab card affordance: when a card's underlying task is CLARIFY-exhausted, render a *"Rescue via Claude-as-Forge"* button that opens a modal pre-populating the bootstrap prompt.
- The modal's "copy prompt" action writes a structured rescue artifact to `~/agents/blackboard/rescues/<task_id>-<ts>.json` so the rescue becomes audit-trail-visible.
- The Action Queue's CLARIFY-exhausted row (§ 5.4 of the e4-4g spec) links to this runbook directly.

---

## 3. Open questions

- Does the bootstrap prompt template include the entire clarification trail verbatim, or a Beacon-summarized version? (Verbatim is safer; longer.)
- Should the rescue artifact be writable from the dashboard's API surface, or only from the CLI? Dashboard-write needs an auth pattern that does not exist yet.
- Is there a Mirror-review hook for rescue-produced PRs (they bypass Forge's normal preflight), or does Mirror's standard review-on-PR-open suffice?
- Should rescue invocations decrement a budget so they do not become the default escape valve?

---

## 4. Acceptance (rough)

- The runbook is discoverable from the Missions tab without prior knowledge.
- A new operator following the runbook end-to-end can complete a rescue without asking Larry.
- Every rescue produces an audit-trail entry under `~/agents/blackboard/rescues/`.

---

## 5. Estimated cost + sizing

Runbook (~1 page) + dashboard affordance (modal + audit-trail write): ~$8–10. One PR or two (split if the runbook is doc-only and the affordance is UI-only). Mirror revisions expected 1. Sizing: medium; the design work is the bootstrap-prompt template.
