# Spec: Per-task tier dispatch (capacity pool) — HARDENED

**Status:** Reviewed (3 research agents + 5 adversarial reviewers, 2026-06-30). Ready to build.
**Owner:** Larry. **Builds on:** #760 (auth↔HOME decouple, MERGED), #765 (tier3 recognition + fallback_tier, OPEN — **must land first**), #763 (registry+spec, OPEN).
**Supersedes:** the time-sliced rotation model.

## 1. Goal

Stop being capacity-limited in heavy dev. Run **two dedicated primary accounts {tier1, tier3} concurrently**, choosing a tier **per task at dispatch time**; **tier2 (laptop/personal) is emergency-only**. A tier = a credential (setup-token); HOME stays at the real home (#760), so concurrent tasks just inject different tokens.

## 2. Non-goals (v1)

- Precise real-time burn-weighted selection — burn is stale/racy (§7). **v1 = round-robin among healthy primaries**; burn is only a coarse proactive skip (`near_cap`). Burn-weighted = v2.
- Cross-tier session migration (forbidden — I2).
- Seeing the laptop's live quota (no Anthropic API). T2 reserve bounds **our own** consumption (§8).
- Hard per-tier concurrency caps.

## 3. Invariants (MUST hold — from the failure-history audit; each tied to a past incident)

I1 dispatch-time auth gate · I2 sessions account-bound, no cross-tier `--resume` · I3 verify transcript persisted after dispatch · I4 failure order rate_limit→session_lost→auth_401 · I5 auth_401=fixed 30min cooldown · I6 setup-token primary, creds.json fallback · I7 never force-kill in-flight · I8 setup-token file-fallback · I9 reuse `parse_reset_time`/`set_cooldown` · I10 conditional HOME + `OURLIBERTY_AGENTS_ROOT` pin · I11 pin `GH_CONFIG_DIR`/`GIT_CONFIG_GLOBAL` to real home · I12 canonical cwd→slug · I13 per-tier, per-kind cooldown · I14 fresh state read + re-verify at dispatch · I16 atomic state writes · I17 lazy cooldown expiry · I18 never log tokens · I19 cooldown persists to disk.

## 4. Selection algorithm (`active_tier.select_dispatch_tier`)

```
def select_dispatch_tier(session_tier=None):
    # (0) OPERATOR PIN FIRST — makes rollback (§16) actually work.
    pin = read_operator_pin()              # contents of ~/agents/rotation.disabled, if a valid tier
    if pin:
        return pin                         # forced; ignore pool logic (operator override)
    # (1) SESSION BINDING (I2) — a resumed task NEVER changes tier.
    if session_tier:
        return session_tier if usable(session_tier) else None   # None => caller PAUSES (no migration)
    # (2) NEW task: round-robin among healthy primaries under the proactive cap.
    primary = cfg.primary    # [tier1, tier3]
    healthy = [t for t in primary if usable(t) and not near_cap(t)]
    if healthy: return round_robin(healthy)
    # (3) all primaries loaded-but-not-benched: use them anyway (don't spill to laptop for mere load).
    usable_primary = [t for t in primary if usable(t)]
    if usable_primary: return round_robin(usable_primary)
    # (4) EMERGENCY: both primaries benched -> fallback with reserve guard (§8).
    for t in cfg.fallback:   # [tier2]
        if usable(t) and fallback_reserve_ok(t): return t
    return None              # nothing available -> caller HOLDS + escalates (§9)

def usable(t):               # I1, I14
    return cooldown_until(t) is None and tier_auth_ok(t)
```

**Caller contract (all 4 paths, §10):** call the selector; if it returns **None**, do NOT dispatch — HOLD/re-poll (and the inbox gate escalates per §9). Re-verify `usable(selected)` immediately before spawn (TOCTOU mitigation, I14); if it flipped, re-select.

## 5. Session→tier binding (I2 — the highest-risk item)

A **durable map** `~/agents/state/session-tier-map.json` (atomic writes, I16): `{session_id: {tier, ts}}`.
- On a NEW dispatch that creates a session: after `run_claude` returns `new_session_id`, record `session_id -> selected_tier`.
- On a `--resume` dispatch (agent_runner AND both telegram bots' per-chat resume): look up `session_tier` from the map and pass it to the selector. The selector returns it iff usable; **if benched, the caller follows the existing resume-discipline: pause + DM, never cross-tier** (I2).
- If the map entry is missing (e.g. pre-existing session): log WARN, treat as `session_tier=None` but the existing resume-discipline still refuses cross-tier fallback (defense-in-depth).
- **Auto-resume:** a task paused because its session_tier is benched is resumed by `heal_resume_paused_on_tier1` when the tier un-benches — the pause marker MUST carry `agent_id` + `session_id` so the healer can reconstruct the dispatch. (Avoids the historical no-session-revision strand.)

## 6. Effective-tier discipline (completeness-pass catch — critical)

The selected tier ("effective_tier") MUST be threaded through agent_runner so that **on failure the tier that actually ran is benched/recorded**, not the global active tier:
- `set_cooldown(effective_tier, ...)` on rate_limit/auth_401 (not `active_tier.read()['tier']`).
- `out_meta['account_tier'] = effective_tier` (so the cost row attributes burn to the right tier).
- The transcript-persistence check (I3) targets effective_tier's home.
- The fallback path picks the next tier via `fallback_tier(effective_tier)` (pool-aware, not binary).

## 7. Cooldown & concurrency

- **Monotonic (extend-only):** `set_cooldown` must do `until = max(existing_until, new_until)` (never shorten an active bench). Current code overwrites — REQUIRED FIX.
- **Lock shared-state RMW:** `set_cooldown`/`_write` do read-modify-write; wrap the critical section in `fcntl.flock` (LOCK_EX) so concurrent sets across tiers can't clobber each other. Pre-existing race; per-task amplifies it.
- **Round-robin counter:** `~/agents/state/tier-rr-counter` = single-line JSON `{"counter": int}`. Increment under `flock`; pick `pool[counter % len(pool)]`; cold-start = create at 0; on lock-unavailable, fall back to process-local random (log WARN). Counter race at worst skews distribution by one (acceptable, self-correcting).
- **Burn staleness is tolerated by design** (cost rows write post-dispatch; in-flight invisible). Round-robin does the distribution; `near_cap` only needs coarse "near the ceiling," where seconds-to-minutes of staleness don't matter. Documented limitation; `conservative_cap_fraction` lever available for burst-heavy periods.

## 8. Burn reader & Tier2 reserve

- `rolling_5h_token_volume(account=<t>)` — add an `account` filter to the existing helper (today account-agnostic). Sum `input+output+cache_creation` over 5h for rows where `account==t`. **Ignore** rows with `account` in {None, 'fixture', 'skipped'}. **Fail-open** on unreadable/corrupt costs.jsonl (treat as 0 / not-near-cap; never crash the dispatch).
- `near_cap(t)` = `rolling_5h_token_volume(account=t) >= cfg.proactive_cap_fraction * cfg.max_5h_budget_tokens`, with hysteresis (`proactive_release_fraction` to re-include).
- `fallback_reserve_ok(tier2)` = the **system's own** tier2 5h burn (`account==tier2` rows — the droplet only logs its own dispatches; the laptop writes nothing here) is `< cfg.t2_reserve_fraction * cfg.max_5h_budget_tokens`. If exhausted, the selector returns None → **hold** (better to wait than lock Larry out of his laptop). Best-effort but fail-safe.

## 9. Visibility when held (silent-lockout fix — adversary standout)

When the selector returns None (all tiers unavailable) for a NEW dispatch, the inbox gate holds the task AND, if the hold persists **> `hold_alert_minutes` (default 10)**, emits ONE deduped `larry_alert` (`tier-pool-all-unavailable`, severity warning) naming each tier's reason (cooldown until / auth / reserve). Re-arms per cooldown window; never spams. This converts a silent indefinite stall into a visible, actionable state.

## 10. Wiring points — IMPLEMENTATION CHECKLIST (build cannot skip any)

Each replaces `active_tier.read()['tier']` (the dispatch-tier source) with `select_dispatch_tier(session_tier=...)`, threads effective_tier (§6), and handles None (hold):

- [ ] **W1 `agent_runner.run_claude`** — accept/derive `session_tier`; select; thread effective_tier into auth, cooldown-on-failure, `account_tier`, transcript check, fallback. Record session→tier on new-session success (§5).
- [ ] **W2 `agent_telegram_bot`** (forge/mirror/pulse bots) — select; per-chat session→tier lookup on `--resume`; stamp `account` on its cost rows.
- [ ] **W3 `beacon_telegram_bot.call_beacon`** — select; per-chat session→tier on `--resume`; **add the `GH_CONFIG_DIR`/`GIT_CONFIG_GLOBAL` pins (I11)**; stamp `account`.
- [ ] **W4 `active_tier.durable_claude_env`** (3 generators) — select; return None-safe (caller skips run if None); stamp `account`.
- [ ] **G `inbox_watcher._rotation_gate_block_reason`** — block a NEW dispatch iff `select_dispatch_tier(None)` is None; drop the global-draining logic. Continuations (resume) bypass as today.
- [ ] **costs `account` stamped by ALL of W1–W4** (today only W1) — required for per-tier burn.

## 11. Tier3 registration (from #765 — confirm landed before build)

`_VALID_TIERS += 'tier3'`; `_SETUP_TOKEN_ENV_BY_TIER['tier3']='CLAUDE_CODE_OAUTH_TOKEN_TIER3'`; **`TIER3_HOME = '/home/larry'` (SHARED with tier1 — tier3 is setup-token-only, shared-config)** → therefore **no new systemd ReadWritePaths needed** (tier3 writes under the already-carved real home). `current_home`/`_credentials_path` via `home_for_tier` (call-time). `dashboard_api.ROTATION_VALID_TIERS += 'tier3'`.

## 12. Config (`agent-models.json`)

```json
"tier_pool": {
  "primary": ["tier1", "tier3"], "fallback": ["tier2"],
  "proactive_cap_fraction": 0.85, "proactive_release_fraction": 0.70,
  "t2_reserve_fraction": 0.25, "max_5h_budget_tokens": <calibrated>,
  "hold_alert_minutes": 10
}
```
Selector reads with **fail-safe defaults** if missing/malformed (default to `primary=[tier1]`, no near_cap, so a config error degrades to "use tier1," never crashes).

## 13. Deploy

1. Land #765 (+#763), then the selector PR (this spec).
2. **Regenerate the daemon-restart manifest** (`daemon_restart_manifest.py regenerate`) so a change to active_tier.py / agent_runner.py / agent_telegram_bot.py / beacon_telegram_bot.py / dashboard_api.py / outbox_notifier.py restarts **its** daemon — else a split-brain window (some daemons new selector, some old single-tier). Verify each wiring-point file is in the right unit's watch_paths.
3. Pull main; restart inbox-watcher, beacon-bot, forge/mirror/pulse-bot, dashboard-api, outbox-notifier (manual, for immediacy; hourly `heal_stale_daemon_code` is the backstop). Generators pick up code on next timer tick.
4. `rotation.enabled=false`; remove the `rotation.disabled=tier2` pin.
5. Verify (§14).

## 14. Verification (observable — depends on §15 logging)

0. **Pre-flight:** one dispatch on each of W1–W4 → costs.jsonl has rows with distinct `account` values. (If a path's row lacks `account`, costs-stamping is broken — fix first.)
1. Daemons active on new code.
2. Two concurrent NEW dispatches → logs show `dispatch_tier=tier1` and `=tier3` (round-robin, concurrent); rr-counter incremented.
3. Both primaries healthy → no `account==tier2` cost rows (tier2 idle).
4. Bench tier1 → new dispatches all go tier3; none held.
5. Bench tier1 AND tier3 → spill to tier2 only if reserve_ok, else hold; tier2 burn stays < reserve.
6. Multi-phase task: every phase on the SAME tier (I2); benched mid-task → pauses, then auto-resumes when un-benched.
7. tier1 task transcript persists under the real home (I3).
8. **Rollback test:** `echo tier3 > rotation.disabled` with all healthy → ALL dispatches go tier3 (pin honored, §4 step 0); remove → round-robin resumes.

## 15. Observability (first-class, not later)

- **Per-dispatch tier logged at dispatch time:** `dispatch_tier=<t>` on the start line of all W1–W4.
- **Per-tier burn:** `rolling_5h_token_volume(account=<t>)` surfaced via `GET /api/system/rotation` (extend to return per-tier {burn, cooldown_until, benched}) + dashboard.
- **All-held alert** (§9).
- **Dashboard rotation UI** (follow-on, criteria: show pool primary/fallback, per-tier burn+cooldown, operator pin; the component doesn't exist yet — backend ready).

## 16. Rollback

`echo tier1 > ~/agents/rotation.disabled` → selector step (0) forces tier1 for ALL paths (verified by §14.8). Or revert the selector PR. Fully reversible; no state migration.

## 17. Hardening from adversarial review (traceability)

- Silent lock-out → §9 escalation alert.
- Non-monotonic cooldown / RMW clobber → §7 extend-only + flock.
- Session→tier not persisted (I2 risk on both bots) → §5 durable map covering all resume paths.
- Bench/stamp wrong tier → §6 effective-tier discipline.
- Operator pin not honored → §4 step (0) + §14.8 test.
- Account-agnostic burn / dark bot+generator burn → §8 account filter + §10 all-paths stamp `account`.
- Daemon split-brain on deploy → §13.2 manifest regenerate.
- Unobservable verification → §15 dispatch-time logging + §14.0 pre-flight.
- tier3 home/ReadWritePaths confusion → §11 (shared home, no new RWP).
- Cold-start tie / counter init / corrupt burn → §7 (cold-start=0) + §8 (fail-open).
- TOCTOU select→spawn → §4 re-verify before spawn.

## 18. Open parameters for Larry (defaults chosen; not blockers)

- **v1 = round-robin** among healthy primaries (robust); burn-weighted = v2. (Confirm acceptable vs wanting burn-weighted now.)
- `t2_reserve_fraction` = 0.25 (system uses ≤25% of the laptop account before holding).
- `proactive_cap_fraction`/`release` = 0.85/0.70; `max_5h_budget_tokens` = calibrate from observed wall-hits (start conservative).
- `hold_alert_minutes` = 10.
