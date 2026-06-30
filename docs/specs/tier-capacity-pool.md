# Tier capacity pool — design spec

**Status:** Phase 0a landed (this doc + Tier-3 registration). Phase 0b (router) stacks on #760.
**Owner:** Larry. **Date:** 2026-06-29.

## Problem

The constraint during heavy dev is **total Claude capacity**, not tier-switch reliability. A 2h-Tier1 / 1h-Tier2 rotation plus concurrent load (laptop on Tier 2, droplet system on both) exhausted **both** the 5-hour AND the weekly caps on **both** accounts over 7 days. Rocket Station build week needs more headroom. Root contention found: the droplet was pinned to `tier2` = Larry's *personal* account = the same one his laptop uses, so they fought over one weekly quota.

## Decided architecture

- **A tier is a durable token + shared config.** One fully-provisioned home; a "tier" is just a credential (an OAuth setup-token). Builds on **#760** (auth decoupled from HOME — HOME stays at the real home, the tier is selected by token). Consequence: a new tier needs **no** home and **no** provisioning — it inherits the shared config automatically. Adding/replacing a tier (e.g. swap personal-Tier2 for a dedicated Tier3) = one token + one registry line.
- **Routing = 3-tier priority pool:**
  - **Primary pool = {tier1, tier3}** — droplet system work load-balanced across both.
  - **Emergency fallback = tier2** — the system spills here *only* when both primaries are benched, and only past a **reserve threshold** so a spill never fully drains the laptop's account mid-session.
  - **Laptop = tier2, native** — logged into the personal Max directly; the droplet never touches it except the guarded emergency spill.

## Quota signals (no Anthropic quota API exists)

Only the web usage bar is exposed; remaining quota cannot be queried. So:
- **Reactive (reliable, already built):** the rate-limit error carries its reset time. `active_tier.parse_reset_time` + `set_cooldown` bench the account until reset; `cooldown_until` gates dispatch. The `cooldowns{}` / `cooldown_backoff{}` state already keys per-tier — N-tier ready, no schema change.
- **Predictive (soft):** per-account burn over rolling 5h/7d windows. The write side already exists — `agent_runner` stamps `account_tier` onto each dispatch's cost record (`costs.jsonl`). Phase 0b adds only a `rolling_token_volume(account=...)` reader. Estimate vs an undocumented cap; calibrate from observed wall-hits; never the sole gate.

## Invariant: balance at the task level, never the session level

We do **not** hot-migrate an in-flight session (account-bound). A task that walls mid-session is **re-dispatched fresh** on a healthy tier — already current behavior; bounded cost since tasks are short and hard-ceilinged. The router picks a tier at *dispatch* time. The fallback loop becomes "iterate the pool in priority order" instead of the binary `other_home()` flip.

## Generalization surface (from the 2026-06-29 three-part audit)

Most of the scary "binary-flip" sites are HOME logic that #760 makes moot for the token path. What genuinely changes:

- **Token/registry recognition** (`active_tier._VALID_TIERS`, `_SETUP_TOKEN_ENV_BY_TIER`, `TIER*_HOME`): generalize from hardcoded 2-tuples to a config-driven registry. Add tier3.
- **Pool selection** (NEW `scripts/tier_pool_selector.py`): candidates = primaries not benched → least-burned/round-robin → emergency (with reserve) → else defer to soonest reset.
- **Fallback routing** (`agent_runner.py` ~1700-1815, `beacon_telegram_bot.py` ~487): replace `other_home()` / `'tier2' if x=='tier1' else 'tier1'` with pool iteration. **Overlaps #760's diff — must stack on #760.**
- **Rotation** (`rotate_active_tier.py` `_other_tier()`): replace the binary "next tier" with an explicit pool/sequence; or retire load-gate rotation in favor of the pool router.
- **Secondary (rename/parametrize, non-blocking):** `heal_tier2_weekly_health_probe`, `heal_credential_registry_drift` tier loops, `dashboard_api` rotation enum, `heal_pipeline_stall` tier2-fallback scan.

Already N-tier-ready (no change): the cooldown state dicts, the rate-limit ledger (`account` field), the per-dispatch `account_tier` cost stamp.

## Phased rollout

- **Phase 0a (this PR):** register Tier 3 in `token-rotation-schedule.json` + this spec. Conflict-free; behavior unchanged.
- **Phase 0b (stacks on #760, after it merges):** active_tier registry generalization + `tier_pool_selector` + pool-aware fallback in agent_runner/beacon + `rolling_token_volume(account)` reader. Default config keeps today's behavior (tier1 primary, tier2 fallback) so it's inert until the cutover.
- **Phase 1 (when tier1 resets, with the dedicated T3 live):** flip config to primary `{tier1, tier3}` + fallback `tier2`; remove the `rotation.disabled=tier2` pin; free T2 for the laptop. Verify independent quota (if tier3 and tier2 ever wall together, they're the same account — they are not, confirmed at mint).
- **Phase 2 (hardening):** enable predictive burn-balancing once enough per-account history; tune the T2 reserve; fold in the beacon-bot-path decouple and the no-session stranded-task auto-archive healer.

## Dependency / risk

The clean shape rides on **#760** landing (it makes a tier need no home/provisioning, and the router edits sit on its diff). Until #760 merges, Phase 0b can't cleanly build. Highest-leverage action: get #760 merged. If it reverts, fall back to the heavier per-tier-home generalization.

## What Larry provides

The dedicated Tier-3 Max account's setup-token (DONE 2026-06-29 — installed + auth-verified). Nothing else needed until the Phase 1 cutover.
