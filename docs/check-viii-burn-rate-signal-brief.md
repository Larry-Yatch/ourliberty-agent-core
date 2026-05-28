# Check VIII — burn-rate signal validity (brief)

**Status:** brief shipped 2026-05-28. Implementation in two PRs (2a: rate-limit-event ledger + DM reframe; 2b: Check VIII analyzer).

**Purpose:** First real workload for the self-optimizing Pulse Check pattern (doctrine #48, `feedback_self_optimizing_config_via_pulse_check_pattern`). Operationalizes the doctrine before PR-α/β/γ of the broader Pulse cycle upgrade.

## 1. Problem

`scripts/heal_claude_max_burn_rate.py` warns Larry when `costs.jsonl` trailing-5h spend crosses 80% of a hand-picked $60 threshold. On Claude Max OAuth (flat-fee), the dollar gate is a *pace indicator*, not a *quota proxy*. The actual rate-limit wall is on Anthropic's session / weekly / Sonnet caps (token-based), not dollars.

Current DM body claims "consider pausing dispatches to avoid hitting the quota wall" — overstating the correlation. The 2026-05-26/27 incident (Tier 1 wall hit overnight) did correlate with a sustained high burn, but in general $-trailing-5h is a noisy predictor of token-cap exhaustion.

Two evidence gaps:
- We don't capture *actual* rate-limit events (`classify_failure_type → 'rate_limit'` in `agent_runner.py` lines 105-163 exists but only feeds retry logic, not an observation ledger).
- We can't measure the dollar-gate's accuracy without a ground-truth signal.

## 2. Approach — two-phase ship

### PR-2a — rate-limit event ledger + DM reframe (Forge, full chain, ~$4)

1. **`scripts/agent_runner.py`** — when `classify_failure_type → 'rate_limit'`, before retrying, append a JSONL line to `~/agents/blackboard/anthropic-quota-events.jsonl`:
   ```json
   {"ts": "<ISO 8601>", "agent": "<agent>", "task_id": "<id>",
    "model": "<resolved-model>", "account": "tier1|tier2",
    "retry_after_sec": <int|null>, "raw_excerpt": "<first 300 chars of stderr>"}
   ```
   Idempotent append; no DM, no behavior change beyond the file write. Pure observation.

2. **`scripts/heal_claude_max_burn_rate.py`** — DM body reframe only:
   - Drop "quota wall" phrasing.
   - Replace with: "Trailing 5h LLM pace at <pct>% of dollar gate ($<spend> / $<threshold>). Pace indicator only — for actual quota state, check console.anthropic.com/settings/usage. Recent rate-limit events: <count from anthropic-quota-events.jsonl in last 2h>."
   - Threshold unchanged ($60 / 80%). Cooldown unchanged (5h).

3. **Tests:** unit test for the ledger writer (idempotency + schema). Integration test that a simulated rate-limit response produces a ledger entry. Existing `test_heal_claude_max_burn_rate.py` updated for new DM body.

4. **Acceptance:** ledger receives entries from real rate-limit events; existing burn-rate alert still fires at 80%; DM body no longer claims a wall correlation we haven't measured.

### PR-2b — Check VIII analyzer (Claude-as-Forge, ~$3)

1. **`scripts/pulse_check_viii.py`** — weekly cadence, fires from `/cycle` on Mondays alongside Check I:
   - Read `larry-alerts.jsonl` entries where `source == 'heal-claude-max-burn-rate'` for trailing 4w.
   - Read `anthropic-quota-events.jsonl` for trailing 4w.
   - For each burn-rate DM, classify:
     - **TP:** quota-event recorded within 2h *after* DM.
     - **FP:** no quota-event within 2h after DM.
   - For each quota-event, classify:
     - **FN:** no burn-rate DM within 2h *before* the event.
   - Compute weekly counts. Compute `precision = TP / (TP + FP)` and `recall = TP / (TP + FN)`.

2. **Proposal-firing rules** (per doctrine #48):
   - **Precision < 0.4 with ≥ 5 DMs in window** → propose raising dollar threshold (specifically: raise to the 75th-percentile spend at the *moment of TP events*, or +20% if no TPs).
   - **Recall < 0.6 with ≥ 3 quota-events in window** → propose lowering threshold (to 75th-percentile spend at the *moment of FN events*, or -20% if no FNs).
   - **Both precision and recall above 0.6 for 4 consecutive weeks** → propose nothing; healer is well-calibrated.
   - **TP count == 0 across trailing 8w with ≥ 5 quota-events** → propose deprecating the dollar gate entirely (signal has no observable predictive value).

3. **Standard 5-step pattern:**
   - Write artifact: `~/agents/blackboard/pulse-check-viii-proposals/check-viii-<week>.json` (current vs proposed value, sample sizes, rationale, `applied: false`, `as_of`).
   - DM via `larry_alerts.append_alert` (digest format matching Check III).
   - Beacon shortcut `approve check-viii-update-<date>` reads artifact, dispatches Claude-as-Forge config PR (modifies `config/agent-models.json:tier1_quota.max_5h_spend_threshold_usd`).
   - Mirror auto-merges.
   - On merge, Beacon flips `applied: true` on the archived artifact.

4. **Tests:** offline analyzer test using a fixture corpus of synthetic burn-rate DMs + quota events. Verify TP/FP/FN counts; verify each proposal-firing rule. Beacon shortcut helper covered separately.

5. **systemd:** no new timer — Check VIII fires inside the existing `/cycle` invocation on Mondays (gated on weekday == Monday + sentinel artifact missing OR > 7d). No additional cadence infra.

## 3. Boundaries + risks

- **First-data-week limitation:** Check VIII's first proposal-firing is realistically ~4 weeks out (need data accumulation). PR-2b ships the analyzer but expect quiet output for the first month — that's expected, not a regression.
- **Cooldown alignment:** existing 5h DM cooldown means at most 1 DM per 5h-burn period. With 4 weeks of data and Larry's typical workload pattern, expect ~5-15 DMs over the trailing 4w analysis window.
- **Rate-limit event coverage:** `agent_runner.classify_failure_type` only fires on completed agent_runner-routed dispatches. Direct `claude -p` invocations outside agent_runner (e.g. `run_cycle.sh`'s wrapper) won't surface in the ledger. Acceptable for V1; broader coverage is follow-up.
- **Rollback:** PR-2a is purely additive (new file + DM body string). Reverting PR-2a leaves the existing healer behavior intact. PR-2b can be disabled by removing the Monday gate or stopping `/cycle` from invoking it.

## 4. Cross-references

- Doctrine: `feedback_self_optimizing_config_via_pulse_check_pattern` (memory)
- Spec context: `agents/beacon/specs/pulse-cycle-upgrade.md` § 12.3 (Check VII is cost-ceiling escalation; Check VIII is the burn-rate-signal cousin — separate concerns).
- Existing healer: `scripts/heal_claude_max_burn_rate.py` (PR #139, claude-quota-fixes-v2 bundle).
- Rate-limit detection: `scripts/agent_runner.py:105-163` (`classify_failure_type`).
- PR-2a target_repo: `ourliberty-agent-core` (T0 sandbox).
- Beacon shortcut path: parallel to `approve threshold-update-<date>` (Check III) per spec § 12.3.

## 5. Sequencing

1. This brief lands first (small docs PR).
2. PR-2a dispatched via Beacon APPROVAL_REQUEST → Forge → Mirror → merge. Adds the ledger + DM reframe.
3. After PR-2a merges, PR-2b dispatched via Claude-as-Forge → Mirror → merge. Adds the analyzer + Beacon shortcut.
4. Wait ~4 weeks for Check VIII data accumulation; first real proposal fires.

Total: ~$6-8 LLM, ~1 day wall clock for PR-2a + PR-2b. First proposal value-delivery ~4 weeks out.
