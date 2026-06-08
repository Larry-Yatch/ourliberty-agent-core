# Account Rotation + Tier Distinctness — Build Spec

Canonical spec for the `account-rotation-001` build sequence. Self-contained:
a reader who has not seen the originating conversation can build from this.
Author: Larry (via external Claude Code), 2026-05-28.

## 1. Why

Two problems surfaced 2026-05-28:

1. **The Tier 1/Tier 2 fallback was silently a no-op.** Tier 1 (`agent.beacon.ourliberty`)
   hit a real 429 at 15:06 MDT. The fallback fired correctly (~43 TIER1_FAILURE →
   TIER2_FALLBACK_ATTEMPT events 15:06-15:12) but every Tier 2 retry returned Tier 1's
   *identical* "resets 3:30pm" message — the Tier 2 credentials file was authenticating as the
   SAME account. Re-authed to `larry@sealteamleaders.com` at 15:18; both tiers now distinct
   (Tier 1 org `43441a1c…`, Tier 2 org `848cafcc…`). Nothing in the system would have caught
   the duplication — it was found by eyeballing logs.

2. **No proactive load-spreading.** During heavy work Tier 1 burns toward its rolling 5h quota
   wall with no relief except the reactive failure-fallback. We want a proactive 2:1 rotation
   (2h system account / 1h personal account) that engages during heavy load.

The HOME-swap fallback mechanism itself is sound (verified: this CLI authenticates from
`$HOME/.claude/.credentials.json` and ignores even a bogus `CLAUDE_CODE_OAUTH_TOKEN`). No fix
needed there.

## 2. Decisions (locked)

- **Trigger: load-gated.** Rotation engages only during sustained heavy load; quiet periods
  stay 100% on the system account (Tier 1) so the personal account is untouched unless needed.
- **Tier 2 guard: schedule-only.** When engaged, follow the 2:1 schedule regardless of whether
  Larry is using his personal account. No auto-yield, no drain-on-demand. A feature-level master
  enable flag IS included (basic off-switch).
- **2:1 is a starting guess, not final** — tuned later by Pulse against logged events (§ 6.4).

## 3. Hard constraints

1. **Accounts cannot switch mid-task.** `--resume` session IDs are account-bound; a multi-phase
   Forge build started on one account CANNOT continue on the other (`session not found`). The
   existing fallback already refuses to fall back on a `--resume` call for this reason. Therefore
   the switch is **drain-then-switch**: stop dispatching NEW top-level work, let in-flight builds
   finish on their original account, then flip. Windows are "at least 2h / at least 1h, switch at
   the next safe boundary" — a long build can stretch a window.

2. **Drain must not deadlock active builds.** A drain that blocks ALL dispatch would stall a build
   whose next phase is queued as a fresh inbox task. So the drain gate blocks NEW top-level tasks
   but PERMITS continuation/`--resume` dispatches. The switch fires only when no build sequence is
   open AND `IN_FLIGHT_DIR` is empty.

3. **Rolling 5h window, not a reset.** A 1h break does not fully reset Tier 1's rolling window; it
   lowers Tier 1's duty cycle to ~2/3. Hence load-gating + event logging for later tuning, not a
   claim that 2:1 guarantees zero rate limits.

## 4. Verified integration points (audited 2026-05-28)

- `scripts/agent_runner.py`: `get_token()` (~L23) returns `CLAUDE_CODE_OAUTH_TOKEN` (empty in
  prod) + `'oauth'`; primary env built ~L748-750 (HOME inherited = `/home/larry`);
  `TIER2_HOME='/home/larry/.claude-larry-personal'` (~L93); `classify_tier1_failure` (~L104);
  `tier2_available` (~L199); `_build_cmd_for_tier` (~L281); Tier-2 fallback block ~L890-1000
  (`t2_env = dict(env); t2_env['HOME']=TIER2_HOME`); `IN_FLIGHT_DIR=AGENTS_ROOT/'state'/'in-flight'`
  (~L624); `_register_in_flight`/`_unregister_in_flight` (~L627/643).
- `scripts/inbox_watcher.py`: `EMERGENCY_HALT_FILE=BLACKBOARD/'EMERGENCY_HALT'` (L54),
  `emergency_halt_active()` (~L625) — the drain-gate pattern to copy; per-agent 5s poll;
  `process_task()` (L398); session_id/continuation logic ~L466-490.
- `scripts/heal_claude_max_burn_rate.py`: `rolling_5h_spend()` (L151),
  `recent_rate_limit_event_count()` (L189), `load_threshold()` reads
  `config/agent-models.json:tier1_quota.max_5h_spend_threshold_usd` (default 60).
- `scripts/heal_credential_registry_drift.py`: `scan_claude_cli` (L421), `run_once` (L649),
  `main` (L776), `dm_larry` (L190), `_should_re_dm`/`_record_dm` dedup; scheduled ~6h.
- `config/agent-models.json` top-level keys include `tier1_quota`; add a sibling `rotation` block.
- Tier homes: Tier 1 = `/home/larry`; Tier 2 = `/home/larry/.claude-larry-personal`.

## 5. Architecture

- **Active-tier state** `blackboard/active-tier.json`:
  `{tier, since, next_switch_due, draining}`. Helper `scripts/active_tier.py`: `read()`,
  `current_home()`, `other_home()`, `set_tier()`, `set_draining()`. Missing/corrupt → default
  `tier1` (today's behavior).
- **Rotation scheduler** `scripts/rotate_active_tier.py` + `ourliberty-rotate-active-tier.timer`
  (~2 min), default-OFF behind `rotation.enabled`. Drives engage/disengage + drain-then-switch.
- **Drain gate** in `inbox_watcher.py`: when `draining`, skip NEW top-level dispatch (mirror
  `emergency_halt_active()`) but allow continuation/`--resume` tasks.
- **Load signal**: reuse `heal_claude_max_burn_rate.rolling_5h_spend()` with engage/disengage
  hysteresis thresholds.

## 6. PR scope (the sequence steps reference these anchors)

### 6.1 Tier account-distinctness healthcheck (independent root)

Add a check to `heal_credential_registry_drift.py`: run `claude auth status` under
`HOME=/home/larry` and `HOME=/home/larry/.claude-larry-personal`, parse `orgId` (fallback
`email`) from each; if they MATCH (or either is logged-out/unparseable) DM Larry via existing
`dm_larry` + `_should_re_dm` cooldown. Run inside `run_once()`; add a `--check-tiers` flag.
Drift key `tier-distinctness:claude-oauth`. ~20s subprocess timeout; timeout = "unknown, do not
alarm." Tests: distinct orgs → no DM; identical orgs → DM; logged-out → DM; parse failure → no
false alarm. No dependency on the rotation work.

### 6.2 Active-tier plumbing (independent root; pure refactor, behavior unchanged)

Add `blackboard/active-tier.json` + `scripts/active_tier.py` (per § 5). In
`agent_runner.run_claude`, set the PRIMARY `env['HOME']` to `active_tier.current_home()` instead
of inheriting; the failure-fallback targets `active_tier.other_home()` instead of hardcoded
`TIER2_HOME`. Keep the `--resume` no-fallback refusal intact. Default state ships `tier1`, so
behavior is identical to today. Tests: tier1 → HOME=/home/larry; tier2 → HOME=personal; fallback
always targets the other home; missing state → tier1.

### 6.3 Rotation scheduler + drain gate (depends on 6.2; feature, default OFF)

Add the `rotation` config block:
`{enabled:false, tier1_window_minutes:120, tier2_window_minutes:60, max_drain_minutes:45,
engage_5h_spend_usd:42.0, disengage_5h_spend_usd:30.0, tier1_home, tier2_home}`. Add
`scripts/rotate_active_tier.py` + timer (~2 min): if `enabled` false → force tier1, clear
draining, exit; if engaged and window elapsed → set draining; while draining, when `IN_FLIGHT_DIR`
empty AND no open build sequence → flip tier + reset windows + clear draining; `max_drain_minutes`
exceeded → defer (never force-kill). Wire the drain gate into `inbox_watcher.py` per § 5
(block new top-level, allow continuations). For PR-3, engage = always-true when enabled (so it is
testable before load-gating lands in 6.4). Resolve "open build sequence" against the
build-sequence-advancer state (the orchestrator already tracks active sequences) — pick the
reliable existing source, do not invent a parallel tracker.

Rate-limit cooldown (folds in the retry-storm fix). The 2026-05-28 incident was not just retries —
the watcher kept throwing every queued task at Tier 1 after it was already rate-limited (~43
failing dispatches in ~6 min). Fix, reusing the same dispatch gate as draining: when the active
tier returns a `rate_limit` AND no tier switch happens (resume-session skip, other tier
unavailable, or both tiers limited), set a per-account cooldown until the reset time parsed from
the "resets <time>" message; when unparseable, fall back to a capped exponential backoff (cap
30 min). The `inbox_watcher` dispatch path must skip dispatching to a cooled-down account until
the cooldown expires. Cooldown state lives in `active-tier.json` (e.g. a `cooldowns: {<tier>:
<until_iso>}` field). Keep this bounded — do NOT expand it into a general retry-policy rework.

Tests: window elapse → drain; in-flight present → no flip; clears → flip + reset; enabled=false →
forced tier1; drain timeout → defer; watcher blocks fresh task during drain but passes a
continuation; rate_limit with no tier switch → account cooldown set until parsed reset and watcher
skips that account until expiry; unparseable reset → capped backoff; cooldown clears at expiry.

### 6.4 Load-gating + observability (depends on 6.3)

Implement engage/disengage in `rotate_active_tier.py` via
`heal_claude_max_burn_rate.rolling_5h_spend()`: engage when trailing-5h spend ≥
`engage_5h_spend_usd`, disengage (hysteresis) when < `disengage_5h_spend_usd`. Emit each rotation
action (engage/switch/switch-back/disengage/drain-defer) to `blackboard/rotation-events.jsonl`
`{ts, action, from_tier, to_tier, trigger, rolling_5h_spend, drained_after_sec}` for a future
Pulse Check to tune the ratio/thresholds. Tests: spend ≥ engage → engaged; spend between
thresholds → state held (hysteresis); ledger lines well-formed.

### 6.5 Manual tier pin via dashboard Off control (depends on 6.3 + the dashboard Auto/Off switch)

**Why.** The load gate (§ 6.4) decides tiers from the rolling-5h token signal — but Larry can see
the Anthropic-side usage walls (per-account session/weekly limits) earlier and more accurately than
the system can infer them from `costs.jsonl`. When Tier 1 is exhausted, he needs to *hold* the
agents on Tier 2 himself. Before this, the dashboard Off control force-pinned Tier 1, so there was
no way to do that.

**Decision (locked): manual pin fully wins.** While Off+pinned, the load gate is completely
bypassed — the scheduler never flips off the pinned tier until the operator returns to Auto. No
"pin is a floor, auto can escalate" coupling.

**Mechanism — reuse the override file's CONTENTS, no new state file.** The runtime override file
`~/agents/rotation.disabled` (the Auto/Off switch from `dashboard-rotation-switch-001`) now carries
the pinned tier as its body:

- absent → Auto (load-gated rotation runs; § 6.3/6.4 unchanged)
- contains `tier1` / `tier2` → that tier is force-pinned; the scheduler's disabled branch re-pins it
  every tick via `active_tier.set_tier(<pinned>)`
- empty (the historical `touch`) or unrecognized → `tier1` — identical to the original Off behavior,
  so a file written by a pre-pin dashboard build keeps pinning Tier 1 (backward compatible)

`rotate_active_tier._override_pinned_tier()` reads the contents at call time. The disabled branch
emits a direction-aware `manual_override` event (`engage`→tier2, `disengage`→tier1) so the existing
tier1 event shape is preserved and a tier2 pin is observable in `rotation-events.jsonl`.

**Dashboard API.** `GET /api/system/rotation` returns `pinned_tier` (`tier1`|`tier2` while off, `null`
in auto). `POST` accepts an optional `pinned_tier`; `mode=off` writes it as the file contents (default
`tier1`), `mode=auto` removes the file. An invalid `pinned_tier` is a 400 before any filesystem
mutation. The larry_action audit row records the pinned tier.

**Dashboard UI.** A "Pinned tier" Tier 1 / Tier 2 selector under the Auto/Off row, enabled only while
Off (grayed in Auto). Copy notes that a rate-limited tier still auto-falls to the other per request
(the § 6.2 dispatch-path fallback + cooldown is the safety net under any pin).

**Known interaction (documented, not defeated).** Pinning a tier that then hits a 429 does NOT move
the pin: agent_runner cools down the failing tier and falls *that* dispatch to the other home, while
the drain gate blocks *new* top-level dispatches to the cooling tier until the cooldown clears
(continuations still flow). The scheduler keeps re-pinning the operator's choice. The pin is the
*preference*; the per-dispatch 429 fallback remains the *safety net*.

**Enforcement:** scheduler behavior is locked by `test_rotate_active_tier.py` (tier2 pin sticks /
idempotent / survives-low-load, direction-aware event, unknown→tier1); the API contract by
`test_dashboard_api_rotation.py` (pinned_tier across modes, write/validate/round-trip); the UI +
proxy by `RotationToggle.test.tsx` + the route test. Backward-compat (empty file → tier1) is asserted
in both the scheduler and API suites.

## 7. Rollout

Steps 6.1 and 6.2 are independent roots; 6.3 follows 6.2; 6.4 follows 6.3; 6.5 follows 6.3 + the
dashboard switch. After all merge, flip `rotation.enabled=true` and watch `rotation-events.jsonl` +
`anthropic-quota-events.jsonl` for one heavy day before trusting it. Master kill at any time:
`rotation.enabled=false` (scheduler forces tier1 on next tick). Manual override at any time: the
dashboard Off control with a pinned tier (or write `tier1`/`tier2` into `~/agents/rotation.disabled`
directly).
