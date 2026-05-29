# Pulse Cycle Upgrade — PR-β Brief

**Purpose:** Forge reads this brief during PR-β preflight + build. Dispatch text is short; canonical scope lives here. Sister doc to [docs/pulse-alpha1-brief.md](pulse-alpha1-brief.md), [docs/pulse-alpha2-brief.md](pulse-alpha2-brief.md), and [docs/pulse-cycle-upgrade-design-pass-2026-05-26.md](pulse-cycle-upgrade-design-pass-2026-05-26.md).

**Spec base:** [`agents/beacon/specs/pulse-cycle-upgrade.md`](../agents/beacon/specs/pulse-cycle-upgrade.md) § 6 PR-β + § 5.1 implementation paragraph + § 12.3 Check family (analyzers IV/V/VI/VII).

**Gates on:** PR-α₂ merged.

**What this brief is NOT:** scope for further cycle-prompt.md edits (α₁/α₂'s job), CLAUDE.md additions (γ's job), or any of the operational Check I/VIII/IX analyzers (already shipped, not touched by β).

---

## Scope summary

β ships the support infrastructure α₁/α₂ depend on. Pure code + systemd + tests; no prompt edits. Implements:

1. **Tier state machine library** — `scripts/cycle_tier_state.py` reads/writes `~/agents/state/cycle-tier.json` per α₁'s § 2.2 documented schema.
2. **PRIME DIRECTIVE ledger** — `scripts/cycle_prime_ledger.py` writes intervention/systemic-fix/verification_pending rows to `~/agents/blackboard/cycle-prime-ledger.jsonl` (NOT `cycle-actions.jsonl` — see OQ1 resolution below) and computes the trailing-30d ratio metric per α₁'s § 6.
3. **Alert-triage state library** — `scripts/alert_triage_state.py` reads/writes `~/agents/state/alert-triage.json` per α₂'s § 3.0 + § 6.10 lifecycle.
4. **Check IV/V/VI/VII analyzers** — four new deterministic analyzers following the pulse_check_iii.py / pulse_check_viii.py / pulse_check_ix.py pattern (stdlib + idempotent + sentinel-cum-artifact).
5. **run_cycle.sh tier-routing logic** — read tier state at startup, sleep-until-next-tier-window if appropriate, invoke `/cycle` with the right prompt context per α₁'s § 2.
6. **Systemd cadence change** — `ourliberty-cycle.timer` `OnUnitActiveSec=4h` → `OnUnitActiveSec=5min` (Tier 1 cadence; the script self-throttles to Tier 2/3 windows).
7. **Test coverage** — unittest for tier state machine, ledger ratio computation, alert-triage lifecycle transitions, each of the 4 new analyzers.

Total file count: 7 new (3 state libs + 4 analyzers) + 5 test modules + 3 modified.

---

## OQ1 resolution (locked) — cycle-actions.jsonl naming collision

Per Larry's 2026-05-29 decision: **rename the PRIME DIRECTIVE ledger to `cycle-prime-ledger.jsonl`** to avoid collision with the existing auto-fix action log.

**Concrete impact:**

- `scripts/cycle_prime_ledger.py` writes to `~/agents/blackboard/cycle-prime-ledger.jsonl` (NEW path). Schema per α₁'s § 6.4 row shape.
- Spec § 5.3 says `~/agents/blackboard/cycle-actions.jsonl` — α₂ already updates the spec language to match the rename (verify in α₂'s output before dispatching β). If α₂ missed it, β raises a CLARIFY in preflight.
- Spec § 5.7 data-sources table — same update.
- Existing `runbooks/cycle-actions.jsonl` (auto-fix log) is UNTOUCHED. Continues to be written by α₁'s § 14 logic. No migration needed; the two files are semantically distinct (auto-fix actions vs intervention/systemic-fix ratio rows) and live at different paths.
- Check V's data substrate per § 12.3 ("`cycle-actions.jsonl`") MEANS the new ledger `cycle-prime-ledger.jsonl` — Check V analyzes intervention/systemic-fix history, not the auto-fix log.
- Check VI's data substrate ("`cycle-actions.jsonl` `verification_pending` rates") — same: means `cycle-prime-ledger.jsonl`.

---

## Concrete deliverables — file by file

### NEW files

#### 1. `scripts/cycle_tier_state.py`

Small helper library. Functions:

- `read_tier_state() -> dict` — atomic read; on missing/corrupt, returns `{"tier": 1, "consecutive_clean": 0, "last_signal_at": None}` and writes that back (recovers gracefully).
- `record_iter_result(checks_clean: bool) -> dict` — append result; updates `consecutive_clean` counter; calls `advance_tier()` if 3-consecutive-clean threshold met at current tier; calls `reset_to_tier_1()` if `checks_clean=False`.
- `advance_tier() -> int` — promote to next tier (1→2→3); resets `consecutive_clean=0`; logs to `~/agents/logs/cycle-tier-state.log`.
- `reset_to_tier_1()` — used on Check non-empty findings (per α₁ § 2.3 tier-reset rule). Sets `last_signal_at = now_utc()`.

Schema (matches α₁ § 2.2 documentation):

```json
{
  "tier": 1,
  "consecutive_clean": 0,
  "last_signal_at": "2026-05-29T15:42:12Z",
  "last_updated": "2026-05-29T15:43:00Z"
}
```

Atomic writes via tmp + rename. Schema validation on read; on corruption write defaults + DM Larry via `larry_alerts` (severity=warning).

Stdlib only. No subprocess. No LLM.

#### 2. `scripts/cycle_prime_ledger.py`

Small helper library. Functions:

- `append_action(tier: int, kind: str, payload: dict)` — append a single row. `kind` is one of `intervention`, `systemic_fix`, `verification_pending`. Payload carries fix-specific metadata (commit_sha, chain_event_id, dispatched_at, verifies_at).
- `compute_ratio_30d() -> dict` — read trailing-30d rows; return `{interventions: N, systemic_fixes: M, verification_pending: K, ratio: N/M or "N/A" if M==0, trend: "improving"|"flat"|"worsening"}`.
- `promote_verification_pending(row_id: str)` — when a `verification_pending` row's verifying signal appears within 7d, promote it to `systemic_fix` (per Decision II semantics).

Schema row:

```json
{
  "ts": "2026-05-29T15:42:12Z",
  "iter": 1234,
  "tier": 1,
  "kind": "systemic_fix",
  "intervention_id": "abc123",
  "fix_commit_sha": "f80ebb1",
  "chain_event_id": 9876,
  "verifies_at": "2026-05-30T15:42:12Z",
  "verified_at": null,
  "verification_anchor": "chain_events_dispatch_ts"
}
```

Stdlib only.

#### 3. `scripts/alert_triage_state.py`

Functions:

- `read_state() -> dict` — atomic read; returns `{}` if missing.
- `record_triage(alert_id: str, tier: int, decision: str, rationale: str)` — write a per-alert lifecycle row.
- `mark_dispatched(alert_id: str, dispatch_ts: str, target_agent: str, task_id: str)`.
- `mark_resolved(alert_id: str, resolved_ts: str, resolution: str)`.

State file path: `~/agents/state/alert-triage.json`. Per α₂ § 3.0 lifecycle: `pending → triaged-tier-N → action-dispatched → resolved`.

#### 4. `scripts/pulse_check_iv.py`

Marker-drift enforcement-strictness analyzer. Cadence: weekly (Monday). Data substrate: `chain_events` table query for `event_type LIKE 'mirror_marker_invisible:%'` over trailing 4 weeks.

Proposal-firing rule: rate > 2/week over trailing 4 weeks → propose enforcement tightening (Mirror prompt re-emphasis OR hard validator gate on marker shape). Per spec § 12.3.

Artifact: `~/agents/blackboard/pulse-check-iv-proposals/check-iv-<week-Monday>.json`. DM via `larry_alerts.append_alert` if proposal fires.

Follow pulse_check_viii.py shape (deterministic, stdlib, sentinel-cum-artifact, idempotent on same-week sentinel).

#### 5. `scripts/pulse_check_v.py`

Tier-1 action-template trust analyzer. Cadence: monthly. Data substrate: `cycle-prime-ledger.jsonl` (the NEW ledger from deliverable 2 above).

Two proposal-firing rules per spec § 12.3:
- For each action-template with ≥10 dispatches in trailing 90d AND zero Larry modifications → propose removing from guard list.
- For each non-guarded template that caused a Larry-correction within 30d → propose moving INTO guard list.

Artifact: `~/agents/blackboard/pulse-check-v-proposals/check-v-<month>.json`.

#### 6. `scripts/pulse_check_vi.py`

PRIME DIRECTIVE posture analyzer. Cadence: monthly. Data substrate: `cycle-prime-ledger.jsonl` `verification_pending` rate + auto-promote rate + ratio trend.

Three proposal-firing rules per spec § 12.3:
1. `verification_pending` rate > 40% AND auto-promote rate > 80% → propose tightening posture or shrinking verification window.
2. `verification_pending` rate < 5% AND ratio NOT trending toward zero → propose tightening.
3. `verification_pending` stuck-forever rate > 30% → propose stricter posture + re-examine fix-categories.

Artifact: `~/agents/blackboard/pulse-check-vi-proposals/check-vi-<month>.json`.

#### 7. `scripts/pulse_check_vii.py`

Cost-ceiling escalation-threshold analyzer. Cadence: after every escalation DM logged (not periodic — event-driven). Data substrate: Pulse's escalation-response log at `~/agents/state/pulse-cost-escalations.jsonl` (NEW file written by Decision III's escalation logic; α₂ documents the file's role, β creates it).

Three proposal-firing rules per spec § 12.3 with the ≥10/≥20 consecutive thresholds.

Artifact: `~/agents/blackboard/pulse-check-vii-proposals/check-vii-<date>.json`. Fires immediately after each new escalation-response row arrives.

### Test files (~5 unittest modules)

- `scripts/tests/test_cycle_tier_state.py` — tier transitions, atomic-write semantics, corruption-recovery.
- `scripts/tests/test_cycle_prime_ledger.py` — append + ratio computation + promote logic.
- `scripts/tests/test_alert_triage_state.py` — lifecycle transitions.
- `scripts/tests/test_pulse_check_iv.py` through `_vii.py` — each analyzer's proposal-firing rules + sentinel idempotency.

Follow existing `test_pulse_check_viii.py` pattern for sentinel + DM-suppression coverage.

### MODIFIED files

#### 1. `scripts/run_cycle.sh`

Add tier-routing logic at startup per α₁'s § 2:

```bash
# Pseudocode (Forge translates to bash):
TIER_STATE=$(python3 ~/agent-core/scripts/cycle_tier_state.py read)
CURRENT_TIER=$(echo "$TIER_STATE" | jq -r .tier)
TIER_WINDOW_S=$(case $CURRENT_TIER in 1) echo 300 ;; 2) echo 900 ;; 3) echo 1800 ;; esac)
LAST_RUN=$(stat -c %Y ~/agents/state/cycle-last-run.flag 2>/dev/null || echo 0)
NOW=$(date +%s)
ELAPSED=$((NOW - LAST_RUN))
if [ $ELAPSED -lt $TIER_WINDOW_S ]; then
  echo "tier $CURRENT_TIER window not elapsed ($ELAPSED < $TIER_WINDOW_S); skipping"
  exit 0
fi
touch ~/agents/state/cycle-last-run.flag
# ... existing cycle invocation
```

This lets `OnUnitActiveSec=5min` (Tier 1) timer fire every 5 min while Tier 2/3 sessions self-throttle.

#### 2. `systemd/ourliberty-cycle.timer`

Change `OnUnitActiveSec=4h` → `OnUnitActiveSec=5min`. Post-merge: Larry `sudo cp` + `daemon-reload` + `systemctl restart ourliberty-cycle.timer`. heal-systemd-install-drift will alert until done.

#### 3. `agents/beacon/specs/pulse-cycle-upgrade.md`

Update § 5.3 + § 5.7 references from `cycle-actions.jsonl` → `cycle-prime-ledger.jsonl` per OQ1 resolution. Should already be done by α₂ — if not, β does it.

---

## task_type + Mirror review focus

- **task_type:** `feature-development` (multi-file code + tests + systemd; substantive infrastructure).
- **Cost ceiling:** ~$5 LLM per memory project-pulse-cycle-upgrade-pending.

**Mirror review focus (Dial 3 regression-only):**

1. **Atomic-write semantics** — every state file write uses tmp + rename. Mirror should grep for `os.rename` or `pathlib.Path.replace` in each write path. No partial-file-write windows.
2. **Idempotency on sentinels** — each new analyzer checks for its sentinel-cum-artifact before doing work. Mirror should verify by grepping for the artifact path in each analyzer's main() before the analysis runs.
3. **No LLM in analyzers** — Checks IV/V/VI/VII are deterministic per the spec § 12.3 five-step pattern. Mirror should grep each analyzer file for `claude`, `subprocess.*claude`, `anthropic`, `openai` — none should appear.
4. **Self-protection** — the analyzers must not consume quota (same as heal_claude_max_burn_rate). Verify import of subprocess only for systemd-status checks / file ops, never for LLM calls.
5. **Tier 1 5-min cadence safety** — verify run_cycle.sh tier-window gate prevents Tier 1 from firing more than once per 5 min even if systemd fires faster (clock-skew protection).
6. **OQ1 path consistency** — every reference to the PRIME DIRECTIVE ledger uses `~/agents/blackboard/cycle-prime-ledger.jsonl`. Mirror should grep for `cycle-actions.jsonl` in β's diff and verify only the existing auto-fix path (in `runbooks/`) appears, NOT the blackboard path.
7. **Test coverage of the verification_pending → systemic_fix promotion** — Decision II auto-promote rule must have a test. Mirror should grep test_cycle_prime_ledger.py for `promote_verification_pending`.
8. **systemd timer change doesn't break existing cycles** — `OnUnitActiveSec=5min` is a behavior change. Verify the run_cycle.sh tier-window gate is shipped IN THE SAME PR so Tier 1's 5-min cadence is the only firing-rate change, NOT the actual cycle-invocation rate.

---

## Acceptance criteria

- [ ] All 7 new files present (3 state libraries + 4 analyzers)
- [ ] All 5 test files present with passing tests
- [ ] `scripts/run_cycle.sh` updated with tier-window gate
- [ ] `systemd/ourliberty-cycle.timer` updated to `OnUnitActiveSec=5min`
- [ ] No analyzer makes LLM subprocess calls
- [ ] OQ1 path resolution applied throughout (no PRIME DIRECTIVE ledger writes to `cycle-actions.jsonl`)
- [ ] Mirror PASS
- [ ] Post-merge sudo cp + daemon-reload: cycle.timer fires every 5 min; first Tier 1 cycle completes within 1200s budget (cycle-timeout-bump PR #165 already shipped that headroom)
- [ ] Post-merge: 3 clean Tier 1 cycles in a row promote Pulse to Tier 2 per α₁'s tier-reset rule. Observable via `cat ~/agents/state/cycle-tier.json` showing `tier: 2`.

---

## Dependencies + sequencing

- **Blocks:** PR-γ (CLAUDE.md additions). γ's Pulse persona doc cross-references β's state-file paths + analyzer names.
- **Blocked by:** PR-α₂ merged. β implements the mechanisms α₂ documents the BEHAVIOR for.
- **Larry action post-merge:** sudo cp the new timer + daemon-reload + restart cycle.timer (~5 min). heal-systemd-install-drift surfaces if not done.

---

End of brief.
