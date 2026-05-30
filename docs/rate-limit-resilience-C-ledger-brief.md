# PR C — Rate-limit ledger completeness

Step `step-c-ledger` of sequence `rate-limit-resilience-001`. Root step (no deps).
Read `docs/rate-limit-resilience-project.md` for shared constraints (notably: do NOT
enable rotation).

## Problem

The ground-truth ledger `~/agents/blackboard/anthropic-quota-events.jsonl` (Check VIII
PR-2a) is the signal a pre-dispatch gate and the weekly Check VIII analyzer rely on, but
it is incomplete:

1. `retry_after_sec` is `null` on every event to date. `append_rate_limit_event`
   (`scripts/agent_runner.py`) already accepts a `retry_after_sec` param, but all callers
   pass `None`. The cooldown duration is available in the CLI rate-limit text ("resets
   <time>") and is never parsed into the ledger.
2. The dominant operational class — `auth_401` and `--resume`-session rate-limits, plus
   failures surfaced through the bot wrappers — did NOT land in the ledger at all (zero
   events logged 2026-05-29 despite 6+ stall alerts). Only `rate_limit` on the primary
   `run_claude` path appends today.
3. `costs.jsonl` has no per-account field, so rolling-5h sums conflate Tier 1 + Tier 2
   (documented V1 limitation in `config/agent-models.json:tier1_quota._note`).

## Scope

- Parse the cooldown from CLI output and populate `retry_after_sec`. Reuse
  `active_tier.parse_reset_time()` (it already parses "resets <time>" variants) to derive
  seconds-until-reset; pass it into `append_rate_limit_event` at the call site
  (~`agent_runner.py:916`).
- Also record `auth_401` events to the ledger (today only `rate_limit` is appended), with
  the failure class captured, so Check VIII recall covers the auth-expiry class.
- Ensure rate-limit/auth failures surfaced through the bot wrappers
  (`scripts/beacon_telegram_bot.py` and the forge/mirror bot paths) append to the same
  ledger rather than only DMing.
- Add a per-account field to `costs.jsonl` writes (tier1/tier2) so rolling-5h math can be
  account-scoped later. Keep backward-compatible reads (absent field = unknown/tier1).

## Acceptance

- New rate-limit events carry a non-null `retry_after_sec` whenever the CLI output
  contains a parseable reset time; otherwise `null` (no crash).
- `auth_401` and resume-class / bot-wrapper failures appear as ledger events.
- `costs.jsonl` entries carry an `account` field; existing readers still work.
- Regression gate passes; rotation remains disabled.
