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

## 7. Rollout

Steps 6.1 and 6.2 are independent roots; 6.3 follows 6.2; 6.4 follows 6.3. After all merge, flip
`rotation.enabled=true` and watch `rotation-events.jsonl` + `anthropic-quota-events.jsonl` for one
heavy day before trusting it. Master kill at any time: `rotation.enabled=false` (scheduler forces
tier1 on next tick).
