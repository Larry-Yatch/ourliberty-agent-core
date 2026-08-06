# Approvals Tab — Informational Cards for Non-Binary Awaiting-Larry Items

## Status

Adopted 2026-08-05, by Larry's decision on direction-ask `approvals-tab-nonbinary-contract-001` (dashboard reject = Option B).

## Problem

The Approvals tab carries a stated invariant: every item awaiting Larry appears on the decide tab and clears when resolved. Two mechanisms enforce it — `scripts/heal_unregistered_approval.py` (the promoter, which mints cards) and `scripts/heal_approvals_surface_drift.py` (the sentinel, which independently derives set A, the items awaiting Larry, and compares it against set B, the open `approval_request` chain_events, in both directions).

The invariant is currently false for one class. `evaluate()` refuses to promote an alert-derived ask whose `suggested_action` does not parse into two options (the `SKIP_NEEDS_TRIAGE` gate, ~line 1415), and `retire_needs_triage_cards()` (~line 2621) sweeps any already-promoted ones back off the tab. The sentinel's set A is deliberately wider and independent — `is_actionable_alert()` is `route == 'escalate'` AND `needs_larry is True`, with no needs-triage exclusion. That independence is the property that makes the sentinel a real checker rather than a mirror of the promoter, so it is not the thing to change. The consequence is that a non-binary actionable alert sits permanently in A, permanently absent from B, and is correctly reported as `missing_card` drift forever.

Live example: `pipeline-stall:unrouted-pr:PR#1096`, whose `suggested_action` is an imperative runbook string rather than a binary choice.

A compounding trap sits underneath: the skip is persisted to `state/heal-unregistered-approval-promoted.json` as a `skipped: needs_triage` record, which short-circuits the dedup check (~line 1284) on every later tick. Even a re-fire carrying better, parseable text can never be re-evaluated.

## Decision

Widen the tab rather than narrow the sentinel. A non-binary awaiting-Larry item becomes an informational card: it renders the `suggested_action` runbook text and carries a single non-dispatching exit verb instead of Approve/Reject. Set A and set B converge, and the stated invariant becomes literally true.

The rejected alternative (Option A) was to teach the sentinel to exclude items the promoter deliberately skipped, narrowing the invariant to every BINARY decision awaiting Larry is on the tab. It was rejected because it leaves genuinely actionable items visible only in the Telegram alert stream — the `pipeline-stall:unrouted-pr:PR#1084` failure class the sentinel was built to catch.

Note that this partially reverses `promoted-needs-triage-cards-off-approvals-tab-001` (merged ~2026-07-31), which introduced both the prevention gate and the retire sweep. That reversal is intended and is the substance of Option B.

## Design

### Card shape

An informational card is a real `approval_request` chain_event. That is load-bearing rather than incidental: set B is defined as every OPEN `approval_request` card, and convergence of A and B is the entire point of the change.

It is distinguished by an explicit marker on its dispatch payload (`card_kind: "informational"`), NOT by summary-string matching. The existing `_is_needs_triage_card` predicate matches on an exact summary string; that coupling is fragile and the explicit marker supersedes it.

The card body carries the source alert's `suggested_action` text verbatim. It is a runbook Larry acts on, so paraphrasing or truncating removes its only value.

### Exit verb

A new `acknowledge` verb in `LARRY_ACTION_VALID_ACTIONS`. Three constraints, each traceable to a prior incident:

- Valid ONLY on an `approval_request` whose source event payload carries the informational marker, server-side re-verified before the atomic claim (the same two-place posture the merge verb uses). A binary direction-ask already has three real exits and must not gain a dismiss button.
- It builds NO dispatch envelope. It clears the card and appends a `decision_outcome_ledger` row with outcome `acknowledged`, explicitly NOT `approved`. That ledger feeds the govern loop's autonomy-widening learning; recording a dismissal as an approval is the agent-core #1058 shape and would teach the loop the wrong lesson.
- The existing ban on `mark_done` against an `approval_request` stays exactly as written. `acknowledge` is a distinct verb with its own ledger semantics, not a reopening of that door.

### Retire path and set-A convergence

Acknowledging must also drop the item out of set A. If it does not, the sentinel immediately re-reports it as `missing_card` (present in A, absent from B), trading one permanent drift for another. The acknowledged decision therefore registers as resolved through the same `resolution_check` the promoter already injects, so the source alert stops counting as awaiting.

`retire_needs_triage_cards()` is removed. Left in place it sweeps off the tab precisely what the promoter now mints, producing a promote/retire churn loop on every tick.

### Skip re-evaluability

Skip-ledger entries record a fingerprint of the `suggested_action` they were skipped on, so a re-fire carrying materially different text is re-evaluated while an identical re-fire stays deduped. The drift-sentinel skip (`SKIP_REASON_DRIFT_SENTINEL`) stays terminal — that class is non-promotable by identity, not by phrasing.

### Frontend

`ourliberty-dashboard` renders the informational card as runbook text plus a single Acknowledge button, with no Approve/Reject affordance.

## Build sequence

Ordering is load-bearing. If the promoter ships first, informational cards land on a tab with no working exit, and their Approve/Reject fall through to a generic Beacon envelope that spends a paid session on a no-op — the exact failure this work removes.

- `step-verb` (`ourliberty-agent-core`) — the `acknowledge` verb, its guards, and its ledger semantics. Inert until cards exist. No dependencies.
- `step-render` (`ourliberty-dashboard`) — informational card render plus Acknowledge button. Inert until cards exist. No dependencies.
- `step-promote` (`ourliberty-agent-core`) — promoter mints informational cards, `retire_needs_triage_cards` removed, skip fingerprinting, resolution-on-acknowledge. Depends on `step-verb` AND `step-render`.

`step-verb` and `step-render` touch different repos and share no files, so they are genuinely parallel.

## Success criteria

- A non-binary `route=escalate` + `needs_larry` alert appears on the Approvals tab as an informational card carrying its `suggested_action` verbatim.
- Acknowledging clears the card, appends outcome `acknowledged` (never `approved`), and drops the item from set A.
- `heal_approvals_surface_drift` reports zero `missing_card` drift for `pipeline-stall:unrouted-pr:PR#1096` after a full tick.
- `acknowledge` against a binary `approval_request` is a 400.
- `mark_done` against an `approval_request` remains a 400.
- A minted informational card survives consecutive promoter ticks with no promote/retire churn.

## Out of scope

- Any change to `is_actionable_alert` or another set-A predicate. The sentinel deriving A independently is what makes it a checker rather than a mirror.
- The cooldown-retire and Tier-3 alert-translations changes Pulse proposed. Both were verified false-premise and neither is included.
- Whether unrouted-PR alerts should fire at all.
