# Spec: XIV-b — Tier-4 Alert Write-Back Loop

**Status:** Draft
**Author:** Beacon (drafted 2026-07-07)
**Approver:** Larry (date TBD)
**Parent registry entry:** `agents/beacon/missions.json#xiv-b-tier-4-alert-write-back-loop`
**Predecessor:** `agents/beacon/specs/operator-ux-alert-taxonomy.md` (the NOW/SOON/FYI tiering this loop instruments)

> **Timing note — build deferred (per Larry's briefing, 2026-07-07):** the *build* is deliberately deferred ~1 month (target kickoff ~2026-08-07). Rationale: the alert taxonomy this loop measures is still stabilizing (its predecessor spec is itself "awaiting design pass"); instrumenting a moving target would design the lapse window and outcome buckets against the wrong distribution. Let ~1 month of real alert traffic accumulate first, then build. This spec lands ahead of the build so the August dispatch is friction-free. Only the SPEC lands now — no feature code.
> **Enforcement:** mission entry `phase: "deferred"` + `deferred_reason` holds the build; no build APPROVAL_REQUEST is emitted before ~2026-08-07.

## 1. Problem statement
We send Larry alerts but never record what he did with them — acted on, ignored, or let lapse. With no outcome signal we cannot measure whether a given alert source/subject is actually useful, and we cannot safely let the system tune its own alert thresholds later. This is the feedback channel that makes alert quality measurable.

## 2. Success criteria
- For every Larry-facing (tier-4) alert delivered, the system records an outcome: `acted`, `ignored`, `lapsed`, or `auto_resolved`.
- Larry (or a stranger analyst) can read an aggregate: "for source X / subject Y, what fraction of alerts get acted on vs ignored vs lapsed over the trailing N weeks."
- The outcome data is structured so a future auto-tuning loop can consume it without re-instrumentation.

## 3. Users / consumers
- **Larry** — the operator whose actions are the signal. He supplies the explicit acted/ignored click; the burden must stay near-zero.
- **A future tuning loop** (out of scope here) — consumes the aggregate to propose threshold changes (the Check-VIII-style pattern).
- **Beacon / Pulse** — read the aggregate to reason about alert usefulness in cycle checks.

## 4. Scope (what's in)
- A stable `alert_id` stamped at emission so outcomes can be keyed back to the originating alert.
- A new append-only outcome ledger `~/agents/blackboard/alert-outcomes.jsonl`, one row per outcome, keyed by `alert_id` (with `source`, `subject`, `tier` denormalized for analytics).
- Four outcome values: `acted`, `ignored` (Larry's explicit deliberate no-action), `lapsed` (no signal past a threshold), `auto_resolved` (system healed it via `resolve_alert()` before Larry engaged).
- Explicit capture: inline buttons (Acted / Ignore / Snooze) under each delivered NOW- and SOON-tier alert, via the Telegram `callback_query` path, reusing the existing approval callback infrastructure.
- Passive capture: a lapse sweep marks alerts with no explicit outcome and no auto-resolve after a configurable window as `lapsed`; `auto_resolved` is derived from `resolve_alert()`.
- An aggregate read-model: outcome rate by `source × subject × tier × trailing-window`, exposed as a JSON artifact and/or dashboard-readable view.
- Optional: ship each outcome to `chain_events` as an `alert_outcome` event for the dashboard read-model.

## 5. Out of scope (what's deliberately not in)
- **Auto-tuning of alert thresholds.** v1 only *records and exposes* outcomes. Consuming them to change thresholds is a deliberate follow-on (XIV-c).
- **FYI-tier interaction buttons** — FYI alerts get passive/derived outcomes only, to avoid button fatigue on informational noise.
- **Retroactive backfill** of outcomes for alerts delivered before this ships — forward-looking only.
- **Verified action detection** — "acted" is Larry's self-reported click, a coarse proxy, not a verified downstream action.

## 6. Acceptance criteria
- [ ] Every emitted tier-4 alert carries a stable `alert_id`.
- [ ] A NOW/SOON alert delivered to Telegram renders Acted / Ignore / Snooze inline buttons; clicking one appends exactly one outcome row to `alert-outcomes.jsonl` keyed by that `alert_id`.
- [ ] Clicking Snooze re-queues the alert for later re-delivery and records no terminal outcome.
- [ ] An alert with no explicit outcome and no auto-resolve after the configured lapse window is recorded as `lapsed` exactly once (idempotent sweep).
- [ ] An alert retracted by `resolve_alert()` before an explicit outcome is recorded as `auto_resolved`, not `lapsed`.
- [ ] The aggregate read-model reports outcome counts/rates by `source × subject × tier` for a trailing window.
- [ ] Re-processing the same click, or re-running the lapse sweep, never produces a duplicate outcome row (idempotent on `alert_id`).

## 7. Architecture sketch
- **Identity:** `append_alert()` stamps a short stable `alert_id` (must fit Telegram's 64-byte `callback_data` limit) alongside the existing `subject` / `decision_key`.
- **Emission → delivery:** unchanged offset-cursor delivery. The bot's DM formatter attaches an `InlineKeyboardMarkup` (Acted / Ignore / Snooze) for NOW/SOON, carrying `alert_id` in `callback_data`.
- **Explicit outcome:** a `callback_query` handler in the bot appends `{alert_id, outcome, actor:"larry", ts, source, subject, tier}` to `alert-outcomes.jsonl` (tmp+rename atomic; idempotent on `alert_id`).
- **Derived outcomes:** `resolve_alert()` also appends an `auto_resolved` outcome row. A lapse sweep (cron/healer pattern) appends `lapsed` for stale un-outcomed alerts.
- **Read-model:** a small aggregator reduces the ledger to per-`(source,subject,tier,window)` rates; optionally mirrored into `chain_events` as `alert_outcome` events for the dashboard.
- **Boundary (deliberate):** the outcome ledger is a *sibling* of `larry-alerts.jsonl`, never a mutation of it — the emission ledger stays write-once so the delivery cursor is unaffected.

## 8. Open questions / risks
- Telegram `callback_data` 64-byte cap → `alert_id` must be short (hash/counter, not the full subject). *To resolve: Forge at build.*
- Interaction burden: buttons on every NOW/SOON alert could annoy Larry. Mitigation: NOW/SOON only; Snooze avoids forcing a choice. *To resolve: ~1 week of live use after build; tune down if noisy.*
- "ignored" (deliberate) vs "lapsed" (never engaged) rests on whether Larry clicks; some deliberate-ignores will present as lapses. Acceptable for a coarse usefulness signal. *To resolve: revisit once the real distribution is visible.*
- Lapse-window length is a guess until we have a month of traffic — this is the reason the build is deferred. *To resolve: set from observed data at build time.*
- "acted" is self-reported, not verified downstream. Documented limitation, not a defect. *To resolve: N/A (accepted).*

## 9. Handoff package requirements
- README paragraph: what the write-back loop is and the four outcomes.
- Decisions log: sibling-ledger-not-mutation choice; NOW/SOON-only buttons; `auto_resolved` vs `lapsed` split; tuning-loop-deferred boundary.
- Runbook: how to read the aggregate; how to tune the lapse window.
- Done/stub matrix; test coverage map (idempotency of clicks + lapse sweep); known issues (self-reported "acted"); deploy notes (bot callback handler, cron for lapse sweep).

## 10. References
- Predecessor spec: `agents/beacon/specs/operator-ux-alert-taxonomy.md` (NOW/SOON/FYI tiering).
- Emission: `scripts/larry_alerts.py` (`append_alert`, `resolve_alert`, `read_pending`).
- Delivery: `scripts/beacon_telegram_bot.py` (5-min sweep, DM formatter).
- Callback infra to reuse: `scripts/beacon_approval_handler.py` (existing `callback_query` path).
- Analogous tuning-loop pattern (for the deferred follow-on): Pulse Check VIII (`scripts/pulse_check_viii.py`).

## Changelog
- 2026-07-07 — Beacon drafted; build deferred ~1 month per Larry's briefing (target kickoff ~2026-08-07).
