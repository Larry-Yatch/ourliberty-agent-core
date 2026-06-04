# Approvals tab coverage gap — direction-asks never reach the tab

Status: Larry-approved scope (emission + reconciliation), 2026-06-03. One cohesive Forge build.

## Why

The Approvals tab is fed exclusively by `approval_request` chain_events in Supabase, which are
created ONLY when Beacon emits a canonical `=== APPROVAL_REQUEST === {json} === END ===` marker (bot
→ `chain_event_emit` → Supabase + `state/beacon-pending-approvals.json`). On 2026-06-03 a real
decision — "ship deploy-notifier config-only now" vs "also scope the engine fix" — was surfaced as
two `pulse/beacon-result` **alerts** ("Beacon is holding the APPROVAL_REQUEST waiting for your
direction"). That wording is misleading: no marker was emitted, so no event was registered (confirmed
absent from `beacon-pending-approvals.json`), so it never reached the tab. It lived only in the
Telegram/alert stream and nearly got lost — the exact failure the tab exists to prevent.

Root cause is a COVERAGE gap (not retention, not a malformed/dead-lettered marker): the approval
system captures dispatch-shaped approvals ("approve this Forge task") but has no path for a
**direction question between options that precedes a dispatch**. That class bypasses the tab.

Key plumbing fact (verified): the dashboard approve/reject handler already hardcodes
`target_agent='beacon'` and routes the click back to Beacon carrying the source event's
`suggested_envelope_for_approve` / `_reject`. So a BINARY direction-ask fits `approval_request`
natively — approve = option A's dispatch, reject = option B's dispatch — with NO new event type.

## Goal

Guarantee that every decision needing Larry's direction lands on the Approvals tab — by first-class
emission AND a reconciliation net so a missed marker can never strand a decision in chat again.

Read first: `scripts/beacon_approval_handler.py` (`build_approval_request_chain_event`,
`REQUIRED_FIELDS`, `add_pending`), `scripts/chain_event_emit.py` (`emit_event`),
`scripts/dashboard_api.py` (the `/api/larry/action` handler ~L2277-2340 + the approvals fetch
~L2747), `state/beacon-pending-approvals.json` (pending/history shape), `agents/beacon/CLAUDE.md`
(APPROVAL_REQUEST marker discipline §218+), and `scripts/heal_pulse_check_staleness.py` as the
healer pattern.

## Locked decisions (Larry approved — do not re-open)

1. REUSE `approval_request` — NO new event type. A binary direction-ask is an `approval_request`
   with `target_agent: "beacon"` where approve = option A and reject = option B, each carried by
   `suggested_envelope_for_approve` / `_reject`. The `summary`/`prompt` MUST state both options in
   plain language so the approve/reject buttons are self-explanatory. Binary only this round.

2. EMISSION (first-class). Update `agents/beacon/CLAUDE.md`: a decision that needs Larry's DIRECTION
   (choosing between options before a dispatch is shaped) is a binary `approval_request` emitted via
   `marker.py` — approve = option-A envelope, reject = option-B envelope — NOT a `pulse/beacon-result`
   larry-alert. This explicitly covers the Pulse-cycle / notify context where the deploy-notifier ask
   was wrongly written as an alert. (Guidance is descriptive; the GUARANTEE is decision 3.)

3. RECONCILIATION (the enforcement net). New deterministic healer
   `scripts/heal_unregistered_approval.py` + systemd `.service`/`.timer` on an OnCalendar cadence,
   following the `heal_pulse_check_staleness.py` pattern (heartbeat, `healers.disabled` kill-switch,
   stdlib + the existing `chain_event_emit`/`add_pending` for the write; `EnvironmentFile=.env.larry`
   so it has Supabase creds — load env the same way prod does, not a bare shell). Each run:
   - SCAN `larry-alerts.jsonl` over a trailing window (default 24h) for APPROVAL-CLASS escalations:
     `route == "escalate"` AND a decision signal — `suggested_action` matching
     `^(Reply|Tell Beacon|Choose|Pick)\b` (case-insensitive) OR `message`/`subject` containing
     "holding APPROVAL_REQUEST" / "needs your call" / "your direction". Keep the heuristic
     CONSERVATIVE and in a small editable config list (favor catching a real decision over silence;
     false positives are a dismissible tab card, false negatives are the bug we are killing).
   - MATCH each against registered approvals (`beacon-pending-approvals.json` pending+history, by a
     stable dedup_identity derived from the alert subject; a Supabase lookup is acceptable if cheap).
   - If UNMATCHED: register an `approval_request` chain_event (`target_agent: "beacon"`) so it lands
     on the tab — reconstruct the binary options from the alert's `suggested_action` where parseable;
     if not parseable into a binary, register a single "needs-triage" `approval_request` carrying the
     alert `message` + `suggested_action` verbatim whose approve/reject BOTH route back to Beacon to
     formalize. Write through the existing `add_pending` helper so dashboard resolution stays
     consistent.
   - DEDUP: persist promoted-alert keys in a state file; register each source alert at most once;
     idempotent across ticks.
   - Emit a heartbeat each run; on its own failure emit a larry-alert. (It is itself now covered by
     the pulse-check/daemon liveness watchers.)

4. NO DOUBLE-REGISTRATION. If Beacon already emitted a proper `approval_request` for an ask
   (decision 2 path), reconciliation MUST NOT duplicate it — match on dedup_identity / subject so the
   first-class path and the net never collide.

## Acceptance

- A fixture replaying the exact deploy-notifier `pulse/beacon-result` alert, with no registered
  approval, → reconciliation registers an `approval_request` that appears on the tab within one tick.
- An ask Beacon already registered via marker → reconciliation does NOT duplicate it.
- A routine (non-decision) escalation alert → NOT promoted (conservative-heuristic test).
- Running the healer twice → each alert promoted exactly once (idempotent).
- A registered direction-ask approved/rejected on the dashboard → routes Larry's choice back to
  Beacon's inbox (assert envelope shape against the existing action handler).
- Healer uses the standard pattern (heartbeat, kill-switch, install-drift coverage), stdlib + the
  existing chain_event/supabase deps only. Forge flow preflight → build → Mirror → PR; conventional
  commits.

## Out of scope

- >2-option direction asks (binary only; Beacon narrows to binary or uses chat — document the limit).
- The append-only / retention rework of the tab (separate, already tracked).
- Frontend rendering changes beyond what `approval_request` already supports. If the approve/reject
  UI needs explicit option labels, CLARIFY — verify whether the frontend reads `payload.summary`/
  `prompt` for button context before changing it.
