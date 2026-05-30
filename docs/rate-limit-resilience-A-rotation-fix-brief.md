# PR A — Rotation auth hardening + Tier 2 keep-alive

Step `step-a-rotation` of sequence `rate-limit-resilience-001`. Depends on
`step-c-ledger`. Read `docs/rate-limit-resilience-project.md` for shared constraints.

## Problem (precise root cause, diagnosed 2026-05-30)

When rotation is engaged, `scripts/rotate_active_tier.py:tick()` flips the active tier to
Tier 2 on a load-gated schedule via `active_tier.set_tier(next_tier)` (~line 416) with NO
check that the target tier can authenticate. In `agent_runner.run_claude`, the primary
call uses `current_home()`; if the rotated-to tier's OAuth token is expired it returns
`auth_401 'Invalid authentication credentials'`. The fallback correctly uses
`other_home()` and succeeds — so the HOME computation is NOT the bug.

It STORMS because there is no cooldown/circuit-breaker on `auth_401`:
`active_tier.set_cooldown()` is only ever called for `rate_limit`, and only on the
fallback leg. So the bad tier stays active for its whole window and every dispatch
repeats auth_401 -> fallback (~every 90s).

Separately, nothing refreshes Tier 2's token before it expires (~14h lifetime). The only
health mechanism is `heal_tier2_weekly_health_probe.py` — a WEEKLY liveness probe that
only detects rot and DMs Larry; at weekly cadence it cannot keep the token warm.

## Scope (four parts)

1. **Pre-engage auth gate.** Before `set_tier(next_tier)` in `tick()`, verify the target
   tier can authenticate — reuse the spec-6.1 auth probe (`claude auth status` under the
   target HOME; PR #180 added a `--check-tiers` path) and/or check the target's
   `.credentials.json` `expiresAt` is in the future. If the target fails, do NOT switch:
   emit a rotation event + DM Larry (pointer to the Tier 2 restore runbook) and hold the
   current tier.
2. **auth_401 circuit-breaker.** In `agent_runner.run_claude`, on a primary-tier
   `auth_401`, park that tier via `active_tier.set_cooldown(<active tier>, ...)` (extend
   set_cooldown to handle an auth-failure cooldown, not just rate_limit reset-parsing) so
   the scheduler/watcher stop routing to it until cleared. One bad token must not storm.
3. **De-tier-1 the naming.** `classify_tier1_failure` / the `TIER1_FAILURE_DETECTED` log
   line hardcode "tier1" but fire for whichever tier ran. Make the log report the actual
   failing account (tier1/tier2) under rotation so incidents are readable.
4. **Tier 2 keep-alive.** Reduce the Tier 2 probe cadence from weekly to every ~6-8h
   (edit the `OnCalendar` in the systemd timer template for
   `heal_tier2_weekly_health_probe`; rename if the "weekly" name becomes misleading) so
   the probe exercises Tier 2 inside the token lifetime, triggering the CLI's auto-refresh
   and keeping the refresh token warm.

## Acceptance

- The scheduler refuses to engage a tier that fails the auth/expiry check (event + DM;
  current tier held).
- A primary-tier `auth_401` parks that tier (cooldown set) instead of looping every
  dispatch.
- Logs name the real failing tier under rotation.
- The Tier 2 probe runs at <= 8h cadence.
- `rotation.enabled` stays `false`. Regression gate passes.
