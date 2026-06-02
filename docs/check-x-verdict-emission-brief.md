# Check X follow-up — Forge/Mirror verdict + preflight emission (brief)

## Why
Check X v1 (PR #238) shipped with two ACTIVE thresholds (clarify-rounds,
revision-rounds) and two DEFERRED ones (`escalate_rate_abs_increase`,
`pass_rate_abs_drop`) because the data they need isn't emitted. This PR closes
that emission gap and reactivates the deferred thresholds. It also fills a
genuine observability hole — Forge preflight outcomes and Mirror verdicts SHOULD
be in `chain_events` for the dashboard and future checks, not only in logs.

## Confirmed current state (from the Check X preflight investigation, 2026-06-01)
- Event types for Forge preflight (`preflight_proceed/clarify/reject`) are in
  `chain_event_shipper.KNOWN_EVENT_TYPES` but NO writer emits them.
- Mirror verdicts are written by `outbox_notifier.py` only as lowercase log lines
  (`marker-notified beacon <- mirror ... intent=review-pass/revision/escalate`),
  which `chain_event_shipper.parse_log_line` (uppercase-only) ignores. There is no
  dedicated `review_pass/review_revision/review_escalate` event type emitted today.
- `clarify_request` / `clarify_response` ARE push-emitted via
  `chain_event_emit.emit_event(...)` at `outbox_notifier.py` sites — USE THIS AS
  THE TEMPLATE (best-effort, try/except, WARN on failure, never blocks the flow).
- `auto_merge outcome=merged` is emitted at ~`outbox_notifier.py:3614`.

## Goal (outcome, not prescription)
After this PR, Check X must be able to compute, per window, from `chain_events`:
- Forge preflight outcome counts (proceed / clarify / reject), and
- Mirror verdict mix counts (PASS / REVISION / ESCALATE).

PREFLIGHT MUST RESOLVE (decide in your plan, verify exact sites/line numbers —
do NOT trust the numbers above blindly):
- The emission REPRESENTATION for verdicts. Prefer dedicated event types
  (`review_pass` / `review_revision` / `review_escalate`) for clarity. If you add
  new event types you MUST add them to `chain_event_shipper.KNOWN_EVENT_TYPES` AND
  to `heal_chain_event_type_audit.py`'s allowlist so the audit healer doesn't flag
  them. If you instead reuse existing types (`escalation` for ESCALATE, `auto_merge`
  for PASS, a new one for REVISION), justify it and make sure PASS is counted at the
  verdict moment, not only at actual merge (a PASS can be held in the auto-merge
  queue behind a blocker).
- The exact emission sites in `outbox_notifier.py` (the verdict-classification path
  that writes the lowercase `intent=review-*` lines, and the Forge preflight-marker
  classification path).

## Scope
1. Push-emit Forge preflight outcomes (proceed/clarify/reject) to `chain_events`
   at the preflight-marker classification site, via `chain_event_emit.emit_event`.
2. Push-emit Mirror verdicts (PASS/REVISION/ESCALATE) to `chain_events` at the
   verdict-classification site, via `chain_event_emit.emit_event`.
3. BOTH emissions MUST be additive + best-effort — wrapped exactly like the
   existing `clarify_request/response` emits: a Supabase outage or emit failure
   logs WARN and returns; it must NEVER block, delay, or alter the existing
   marker-notify / auto-merge / escalation behavior. No behavior change on the
   happy path other than the extra best-effort row.
4. Reactivate Check X's two deferred thresholds in `scripts/pulse_check_x.py`:
   compute the Mirror verdict mix from the new events, un-defer
   `escalate_rate_abs_increase` + `pass_rate_abs_drop`, and have them fire per the
   existing pattern. They stay DATA-GATED (min_tasks_per_window + insufficient_signal),
   so they stay silent until verdict history accrues. Update the
   `config/agent-models.json:check_x_regression` `_note` and
   `docs/check-x-chain-quality-regression-brief.md` to reflect activation.

## HONEST LIMITATION (document, do NOT try to fix)
Emission starts at merge of this PR. The Check X baseline window for the CURRENT
4.8 cutover (before 2026-06-01) has NO verdict events, so the reactivated
verdict-mix thresholds CANNOT retroactively read the 4.8 transition — they activate
for FUTURE cutovers once verdict history exists on both sides of `cutover_date`.
State this plainly in the brief + the Check X artifact/_note. The active
clarify/revision metrics remain the 4.8 read (they have pre-cutover history).

## Tests
- Emission: assert `emit_event` is called with the right event_type + payload on
  each preflight outcome and each verdict path; assert best-effort (emit raising /
  returning False does NOT break the notify/merge path — patch emit to raise and
  confirm the existing flow still completes).
- Check X: synthetic verdict events → escalate_rate / pass_rate computed correctly;
  breach fires, clean = none, thin = insufficient_signal. Reuse the `--from-json`
  harness. Test-isolation discipline (OURLIBERTY_AGENTS_ROOT override, `zz-fixture-`
  task_ids, no prod paths). Keep the existing 15 Check X tests green.

## Constraints
- All `chain_events` writes go through `chain_event_emit.emit_event` only.
- Emission additive + best-effort; zero behavior change to notify/merge/escalate on
  the happy path or on Supabase outage. Text-only (no emoji). Dial-3: observability.
- Standard Forge flow: preflight -> build -> Mirror review -> PR.
