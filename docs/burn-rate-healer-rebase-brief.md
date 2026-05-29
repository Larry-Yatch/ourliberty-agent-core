# Brief: re-base burn-rate healer off imputed dollars onto real quota signal

## Context

We run **OAuth Max** (verified 2026-05-29: no `ANTHROPIC_API_KEY`; OAuth credentials at `~/.claude/.credentials.json`). There is **no per-token billing** — the `cost_usd` values in `~/agents/blackboard/costs.jsonl` are *imputed* (tokens x API list price), not money. The only real constraint is the **rolling-5h Max rate-limit window**, which Anthropic meters in usage/quota, not dollars.

`scripts/heal_claude_max_burn_rate.py` currently DMs Larry when summed `cost_usd` over the trailing 5h crosses 80% of a `$60` threshold (`config/agent-models.json:tier1_quota.max_5h_spend_threshold_usd`). This denominates a quota-proximity warning in dollars, which (a) is misleading (looks like money we're spending when we aren't) and (b) false-alarmed every ~15 min on 2026-05-27 while real account usage sat at 31% session / 59% weekly. Larry's decision: re-base the healer onto the real signal and reframe the DM in usage terms, not dollars.

Already available: **`~/agents/blackboard/anthropic-quota-events.jsonl`** — a ground-truth ledger of *actual* rate-limit events written by `agent_runner` (Check VIII PR-2a, #160). `scripts/pulse_check_viii.py` (#164) already measures the precision/recall of the current dollar DM against this ledger and can propose threshold changes / deprecation (doctrine #48, self-optimizing config). This re-base should cooperate with that loop, not fight it.

## Goal

Make `heal_claude_max_burn_rate.py` warn off a signal that reflects the real rolling-5h quota constraint, and reframe its DM body and logs in usage terms rather than dollars — while preserving its self-protection property (no LLM subprocess calls; pure read + arithmetic) and its once-per-window cooldown.

## The core design problem (DESIGN FORK — Forge/Mirror to resolve, flag for Mirror)

The dollar sum was a *leading* indicator (tried to predict the wall before we hit it). `anthropic-quota-events.jsonl` is *lagging* (it records walls after they happen). A naive swap onto the ledger turns an early-warning into an after-the-fact notification. The re-base must preserve a leading-warning capability. Acceptable approaches, in preference order:

1. **Programmatic usage-%, if obtainable headlessly.** Investigate whether `claude auth status` (already invoked by the tier-distinctness check in `scripts/heal_credential_registry_drift.py`, PR #180) or any non-interactive Anthropic call exposes session/weekly usage percentage. If a real usage-% is available without burning quota, warn off that (e.g. DM at 80% of the session window). This is the ideal leading signal. Do NOT add a paid API dependency.
2. **Token-volume proxy (not dollars).** If no usage-% feed exists, keep a trailing-5h *token* sum (input+output, from `costs.jsonl` usage fields) as the leading proxy — strictly better than dollars because it drops the misleading money framing and the arbitrary list-price multiplier. Calibrate the threshold against the ledger (see #3).
3. **Calibrate threshold empirically, defer to Check VIII.** Whatever the leading signal, do NOT hand-pick a magic threshold. Seed a reasonable default and let `pulse_check_viii.py` tune/deprecate it via its existing precision/recall loop. If the chosen approach makes the dollar threshold obsolete, update or remove `tier1_quota.max_5h_spend_threshold_usd` and any reader, and update Check VIII's inputs accordingly so the two stay consistent.

The DM body and log lines must stop implying money. Replace "$X of $60 spent" with usage framing (e.g. "Tier 1 at ~N% of the rolling-5h window" or "K rate-limit events in the last 5h"), and keep the `https://console.anthropic.com/settings/usage` pointer for manual verification.

## Acceptance

- Healer no longer denominates its warning in dollars; DM body + logs are in usage/quota terms.
- A leading-warning capability is preserved (per chosen approach #1 or #2), not reduced to lagging-only notification.
- No LLM subprocess calls in the healer (self-protection invariant intact); verify by grep for `claude`/`subprocess`.
- Once-per-window cooldown intact; a sustained high-usage period yields one DM, not many.
- If the dollar threshold is removed/changed, `pulse_check_viii.py` and `config/agent-models.json` are updated consistently so Check VIII still runs.
- Tests updated: `scripts/tests/test_heal_claude_max_burn_rate.py` covers the new signal (warn fires near the wall; quiet when usage is low; no false alarm at the old 31%/59% real-usage condition).
- Standard Forge flow: preflight -> build -> Mirror review -> PR. Conventional-commit style. No emoji in any artifact.

## Note for the implementer

If approach #1 turns out to require interactive auth or a paid call, fall back to #2 and say so in the PR description — do not block the build on an unobtainable usage feed. Larry has confirmed (2026-05-29) the dollar framing is the thing to remove; the leading-vs-lagging tradeoff is yours to resolve with Mirror.
