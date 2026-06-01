# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

---

## Status snapshot — updated 2026-06-01 12:22Z UTC (Iter 380 — interactive)

**System: ⚠️ Degraded — Tier 1, consecutive_clean=0.** Active pipeline stalls (Tier 2 OAuth expired). **PRs #226 + #227 MERGED (iter 305):** stuck-timer fixes. **PR #228 MERGED (iter 306):** "feat(medic): scaffold alert-operator (PR1 escalate-only)". **PR #229 MERGED (iter 306):** "fix/alert-translations-6-missing". **PR #231 MERGED (iter 375):** "feat(medic): wire real rate-window gauge and per-session timeout". **PR #232 MERGED (iter 376):** "rotation-gate-dm-isolation-001" — `_dm_auth_blocked` short-circuits when `OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE!=true`; auto-merged ~11:56:33Z UTC. `rotation-gate-dm-isolation-001` CLOSED. Sync.json: **status=no-change** at 12:06:36Z UTC — SYNC-PUSH-REBASE-FALLBACK-001 **SELF-CLEARED** (last error: 11:55:54Z, iter 376; no 7th occurrence through iter 378). [yellow] DM sent to Larry at iter 376 (idx=1117+idx=1118). Healer heartbeat **11:40:05Z UTC** (31 min old at iter 378 check; next expected ~12:10Z UTC). 7/7 services active. Alert watermark: **1119** (stable — 0 new alerts in iter 377 or 378). Cooldown/warning: **318** files (stable). Pipeline-stall prefix: **37** (stable). APPROVAL_REQUEST queue: 8 (unchanged). **Check IX FATAL on first firing (port 8001 connection refused, iter 266).** Ledger: **$1,611.38/week (+540.7% vs May 25's $251.49).** **alert-triage.json missing on disk** (INFO; no functional impact). Forge inbox: **empty**. Beacon inbox: **empty**. Beacon rate-limited at 11:55:54Z UTC (resets ~17-18Z UTC; no new Beacon dispatches until reset). **Medic 20th run CONFIRMED COMPLETE (~18 min, iter 361).** Check III 2026-06-14 will assess all 20 runs. **Medic formal approval_request pending: medic-tier2auth401-beaconbot-20260529T045737Z. DM sent to Larry (iter 346).** `medic` source NOT in alert-translations.json — all Medic alerts need manual Check 0 judgment until allowlist built.

**Watch items:**
- **TIER 1 ACTIVE.** consecutive_clean=0 (active stalls: Tier 2 OAuth). Sync SELF-CLEARED (no 7th occurrence; 12:06:36Z no-change). Larry DM'd at iter 376 (idx=1117+1118). APPROVAL_REQUEST `sync-push-rebase-fallback-001` remains open as defensive hardening (urgency reduced — no recurrence since 11:55:54Z). **Medic formal approval_request pending: medic-tier2auth401-beaconbot-20260529T045737Z — DM sent to Larry (iter 346).**
- **Forge IDLE (iter 377).** Both tasks COMPLETE: `medic-hardening-ratewindow-timeout-001` (PR #231) + `rotation-gate-dm-isolation-001` (PR #232). No queued tasks. Monitor for new dispatches.
- ~~**PR #231 OPEN**~~ — **PR #231 MERGED** (~11:46:36Z UTC). `medic-hardening-ratewindow-timeout-001` systemic fix live.
- ~~**PR #232 OPEN**~~ — **PR #232 MERGED** (~11:56:33Z UTC). `rotation-gate-dm-isolation-001` systemic fix live. 4 pre-existing test failures unaffected (tracked separately).
- **Medic 20th run COMPLETE (~18 min).** 16th: ~17.6 min; 17th: ~6.7 min; 18th: ~8.7 min; 19th: ~9.1 min; 20th: ~18 min. Sub-10-min cluster (runs 17–19) confirmed transient. Check III 2026-06-14 will assess distribution of all 20 runs.
- **NEW G-rule (iter 361): `stale-daemon-healer missing daemon-reload guard before restart` — 1/3.** Medic 20th run alerts 1108+1109: stale-daemon healer called `systemctl restart ourliberty-inbox-watcher` without preceding `daemon-reload`, got rc=5. PR #212 added this guard for timer recovery; stale-daemon healer lacks it. PR #232 body noted daemon-reload followup — no new G-rule occurrence yet. At 3/3: dispatch to Beacon.
- **daemon-reload triggers cycle.timer stuck (G-rule 1/3).** At 06:00:20Z UTC (iter 317). If 2 more: dispatch to Beacon.
- ~~**CYCLE.TIMER STUCK**~~ — **RESOLVED. PR #225 merged.** Closed.
- **TIER 2 OAUTH EXPIRED (ELEVATED — ACTIVE STALLS).** forge + beacon-bot tasks paused_on_tier1 since 13:59Z UTC May 30. Fix: `docs/runbooks/restore-larry-personal-claude-oauth-tier2.md`.
- **SYNC-PUSH-REBASE-FALLBACK-001 — SELF-CLEARED (iter 378, 12:06:36Z no-change).** Last error: 11:55:54Z (6th occurrence, iter 376). No 7th occurrence through iter 378. Hypothesis: run_cycle.sh wins the push race before sync_agent_core.sh runs; sync finds "Already up to date." APPROVAL_REQUEST `sync-push-rebase-fallback-001` stays open as defensive hardening. Monitor: if error recurs, re-escalate to Larry.
- **Check VIII FIRST FIRING DONE (iter 266).** rule=insufficient_signal. precision=0.5, recall=0.004 (247 FN, 1 TP). No DM. Very low recall — burn-rate alarm severely under-alerting relative to quota events. Will improve as 4w data accumulates. Next Monday: reassess.
- **Check IX FIRST FIRING FATAL (iter 266).** RuntimeError: Connection refused on port 8001 (`http://127.0.0.1:8001/api/system/missions`). Dashboard API not running. Escalation written to pulse-escalations.json. Sentinel NOT written (script fataled). Next Monday will retry and fail again unless port 8001 is restored.
- **Ledger spend: $1,611.38/week (iter 266, June 1 sidecar).** +540.7% vs May 25's $251.49. No σ anomalies per Ledger. Likely reflects interactive-cycle volume (265+ iters at Opus). Ledger DM (idx=1088) and Check I DM (idx=1089) queued.
- **Check I (Monday 2026-06-01).** 1 proposal: [medium] Template smoke-5a-pf-no-marker (3 Forge retries this week). smoke-5a-pf-no-marker NOT in fixture-pattern allowlist. Auto-dispatch: 0 (medium effort above threshold). Larry can `/dispatch 1` to send to Beacon.
- **APPROVAL_REQUEST queue (8):** pulse-grule-check-c-canonical-names-001, alert-triage-persistence-invocation-001, **sync-push-rebase-fallback-001 (ELEVATED — confirmed failure)**, pulse_telegram_bot.sh launcher, stuck-cycle timeout guard, **Tier 2 OAuth restore (ELEVATED — active stalls)**, forge-claude-md-preflight-self-check-bullet-001, **pulse-grule-prompt-template-001 (doc-only, cycle-prompt.md § 17 G-rule subsection, fixes F24 empty-prompt recurrence)**.
- **deploy-notifier cooldown GC gap — BEACON CORRECTED DIAGNOSIS (2nd investigation).** 106 files stable (unchanged iters 211-246). Root cause: `deploy_notifier.py:490` embeds per-Vercel-uid in subject; no GC after cooldown expires. APPROVAL_REQUEST `cycle-finding-deploy-notifier-gc-20260531T170000Z` — Forge to add `_gc_stale_cooldown_files(max_age_days=7)` to `deploy_notifier.py`. Awaiting Larry approval.
- **F24 EMPTY-PROMPT BUG — BEACON INVESTIGATION COMPLETE.** Two root causes confirmed: (1) missing `prompt` field; (2) wrong source key `pulse-g-rule`. APPROVAL_REQUEST `pulse-grule-prompt-template-001` produced. **Workaround in place: always include `"prompt"` (≥100 chars) + always use `source: "pulse"` in every dispatch envelope.**

---

## ROUTING CONSTRAINT (discovered iter 36)

Pulse can only dispatch to Beacon — HARD_TOPOLOGY in `routing_validator.py` line 54 restricts `'pulse': {'beacon'}`. Pulse→Forge is explicitly blocked at the validator layer. Any cycle-fix permanent-fix dispatch MUST go to Beacon (who then relays to Forge). cycle-prompt.md routing rules (Section G, "code shape → Forge") are accurate in spirit but Pulse must send to Beacon, not Forge directly. Do not write dispatch files to `~/agents/inboxes/forge/` from Pulse sessions.

---

## Known calibration issues

- **All-bot log-silence false positive (confirmed iter 2).** Check C threshold (>30m log silence → ask-then-do) fires on idle Telegram polling for ALL bots. None log anything when no user messages arrive. Do not escalate for log silence unless the systemd unit is also non-active or there's error-spam in the last visible log lines.

- **heal-pipeline-stall-state.json GONE (iter 116, 2026-05-30). CONFIRMED.** Healer uses `~/agents/state/alert-cooldown/` (individual files per cooldown key). Check 3 future scans: `ls ~/agents/state/alert-cooldown/warning/heal-pipeline-stall*`. Rate_limit cooldown files still in alert-cooldown/warning/ — known snoozed state.

- **heal-droplet-git-drift (1st observation, iter 117 2026-05-30).** Fires when droplet main is N+ commits behind origin/main. Calibration issue: healer fires during the post-journal/pre-push window of every cycle. Watch threshold=3 for G-rule.

- **heal-pipeline-stall-state.json format oscillation (iters 182→183→184, 2026-05-31). RESOLVED WATCH.** Format settled: iter 186 confirms cooldown-dict format is stable. Healer uses cooldown-dict format; the earlier MISSING observations were transient (race condition during healer restarts). Pattern did not recur at 3/3 threshold; G-rule not dispatched.

- **Check C service-name suffix false-positive (iter 162). G-rule DISPATCHED iter 165.** Canonical names: `ourliberty-beacon-bot`, `ourliberty-forge-bot`, `ourliberty-mirror-bot`, `ourliberty-pulse-bot`, `ourliberty-inbox-watcher`, `ourliberty-cycle.timer`, `ourliberty-outbox-notifier`. Doc-fix APPROVAL_REQUEST `pulse-grule-check-c-canonical-names-001` in pipeline. Close when Forge PR merges cycle-prompt.md § 4.3 update.

- **inbox-watcher.log file MISSING on disk (iter 165).** Service is active. Inbox-watcher writes to journald only. Use `journalctl -u ourliberty-inbox-watcher` to read its logs, not a file path.

- **larry-alerts.jsonl path (confirmed iter 342).** File is at `~/agents/blackboard/larry-alerts.jsonl`, NOT `~/agents/larry-alerts.jsonl`. Check 0 commands must use the blackboard path. Using the wrong path silently fails (exit code 1, no output).

- **Stale imported-module gap (1st observation, iter 120).** heal-stale-daemon-code tracks main-script mtime but not imported Python module mtimes. inbox-watcher loaded stale dispatch_validator.py for ~40 min post-update. Watch threshold=3.

- **Dispatch source discipline.** Pulse G-rule dispatches must use `source="pulse"` (canonical). `"pulse-g-rule"` is NOT in ALLOWED_SOURCES — validator silently rejects it. Observed iter 91 false dispatch. All future G-rule dispatches: use `source="pulse"`.

- **F24 empty-prompt bug (RECURRING — iters 117 and 209).** `dispatch_validator` requires a top-level `prompt` string field (≥100 chars plain text) in every Beacon dispatch envelope. Pulse twice omitted this field when constructing structured-field-only envelopes (task_id, source, target_agent, summary, finding, etc.) — resulting in dead-letter notification post-cycle. Fix pattern: always include `"prompt": "<≥100 char plain text describing the task for Beacon>"` as a top-level field in every dispatch. The `prompt` field is what the Beacon agent reads as its instruction; the structured fields are metadata only.

---

## Open pending watch items (APPROVAL_REQUEST + G-rule)

**G-rule items (active, not yet at 3/3):**
- `heal-pr-auto-merge blind to CONFLICTING`: G-rule 2/3 (iters 127, 128). Next occurrence → dispatch to Beacon.
- `heal-pipeline-stall "369 min" duration bug`: G-rule 1/3 (iter 128). Mirror PASS timestamp vs PR-creation timestamp mismatch.
- `inbox-watcher rc=-1`: G-rule 2/3 (iters 117, 123). healer's 30s timeout shorter than service startup time. Next occurrence → dispatch to Beacon.
- `daemon-reload triggers cycle.timer stuck (post-PR#225)`: G-rule 1/3 (iter 317, 06:00:20Z UTC). Healer auto-healed. Likely install-time daemon-reload side effect. 2 more occurrences → dispatch to Beacon: "install-drift healer should restart ourliberty-cycle.timer after daemon-reload."
- `stale-daemon-healer missing daemon-reload guard before restart`: G-rule 1/3 (iter 361, 2026-06-01T10:08Z). Medic 20th run alerts 1108+1109: stale-daemon healer called `systemctl restart` without `daemon-reload` → rc=5 (unit not found). PR #212 added this guard for timer recovery; stale-daemon healer lacks it. 2 more occurrences → dispatch to Beacon: "add daemon-reload before restart in stale-daemon healer, per PR #212 pattern."
- ~~`systemd install-drift`~~: G-rule **CLOSED iter 213** — PR #223 "feat(healer): auto-remediate install-drift missing-install case" merged 2026-05-31T17:18:50Z. Systemic fix live: healer now auto-remediates missing-install via `_remediate_missing_install` with post-verify gate. Future occurrences handled automatically.
- ~~`cycle.timer stuck pattern`~~: G-rule **CLOSED iter 304**. PR #225 "Fix ourliberty-cycle.timer NextElapse=infinity wedge: oneshot→simple" merged. Timer switched from OnUnitActiveSec (infinity-trap susceptible) to `OnCalendar=*:0/5, Persistent=true` (wall-clock anchor, guaranteed next-fire). First automated cycle post-fix confirmed running 04:25:32Z UTC. Systemic fix recorded in permanent-fixes section.
- `MalformedForgeMarker G-rule`: DISPATCHED (iter 150). Post-dispatch counter: 4 self-resolved. Doc-fix APPROVAL_REQUEST `forge-claude-md-preflight-self-check-bullet-001` pending Larry. G-rule stays open until doc-PR merges.
- `deploy-notifier cooldown GC gap`: CORRECTED INVESTIGATION COMPLETE (iter 210 re-dispatch → Beacon 2nd result). 309+ files. Root cause: `deploy_notifier.py:490` per-uid subject embedding + no GC. Old APPROVAL_REQUEST `larry-alerts-cooldown-gc-001` SUPERSEDED. New APPROVAL_REQUEST `cycle-finding-deploy-notifier-gc-20260531T170000Z` — fix in `deploy_notifier.py` piggyback tick. Watch: close when Forge PR merges and file count stabilizes.
- `F24 empty-prompt bug (Pulse dispatch)`: DISPATCHED iter 210 (3/3 threshold). Three Pulse G-rule envelopes rejected with "prompt too short (0 chars, min 100)". Dispatch: `cycle-finding-pulse-dispatch-empty-prompt-20260531T165531Z.json` → beacon inbox. Fix: add prompt field template to cycle-prompt.md § G + CLAUDE.md. Workaround: hand-write prompt field ≥100 chars in all dispatches until fix lands.

**APPROVAL_REQUEST items (Larry-gate):**
- `pulse-grule-check-c-canonical-names-001` — Beacon APPROVAL_REQUEST for doc-fix to cycle-prompt.md § 4.3 (canonical service names). Trust-policy → Forge pending.
- `alert-triage-persistence-invocation-001` — alert-triage.json invocation gap fix. Beacon diagnosed: neither run_cycle.sh nor cycle-prompt.md § 3.0 invokes alert_triage_state.py. Pending Larry approval.
- `sync-push-rebase-fallback-001` — sync_agent_core.sh:161 bare-push has no rebase fallback. Confirmed 21 consecutive clean Check B cycles; acute risk resolved. Pending as defensive hardening.
- `pulse_telegram_bot.sh launcher` — pulse-bot restart path broken (pulse_telegram_bot.sh doesn't exist; systemctl restart requires interactive auth). Option A: create launcher script. Pending Larry.
- `stuck-cycle timeout guard` — CYCLE_TIMEOUT_SEC=1800 + timeout wrapper in run_cycle.sh. Multiple occurrences across cycle history. Pending Larry (iter 43).
- `Tier 2 OAuth restore` — tier2-verifier-probe-001 REJECTED (iter 149); Forge confirmed expired OAuth ~10:39Z May 30. Runbook: docs/runbooks/restore-larry-personal-claude-oauth-tier2.md.
- `forge-claude-md-preflight-self-check-bullet-001` — doc-only: pre-emit self-check bullet in Forge CLAUDE.md Preflight discipline. Beacon dispatched after G-rule 3/3.
- ~~`larry-alerts-cooldown-gc-001`~~ — SUPERSEDED. Beacon 2nd investigation corrected target to `deploy_notifier.py`. See new APPROVAL_REQUEST `cycle-finding-deploy-notifier-gc-20260531T170000Z` below.
- `cycle-finding-deploy-notifier-gc-20260531T170000Z` — fix(deploy-notifier): add `_gc_stale_cooldown_files(max_age_days=7)` to `deploy_notifier.py`, invoked per tick, scoped to `deploy-notifier:` prefix only. Target: Forge preflight phase. Awaiting Larry approval.

**Carry-forward ask-then-do:**
- ~~`heal-resume-paused-on-tier1 NOT INSTALLED`~~ — **CLOSED iter 211.** PR #221 "fix(healer): auto-resume timer uses OnCalendar" merged 2026-05-31T16:52Z; body confirms "Re-installed live." Timer verified active, NextElapse 17:10Z UTC, enabled+waiting.

**Verification pending:**
- `Check 5 healer substrate fix` (iters 142-143). Beacon: use `~/agents/blackboard/heal-stale-daemon-code.heartbeat` (90-min threshold). Forge task pending trust-policy dispatch. Verification: 2026-06-07.
- `Healer state file >60m trust-policy dispatch to Forge pending` (iters 143+). Heartbeat confirms healer alive. Verification: 2026-06-07.

**Still-open monitoring:**
- `task-29 E3.2 dashboard-ui build` (iter 60). requeue_count >= 3. E3.2 spec shipped. Frontend build may need re-dispatch. Close when new E3.2 build task dispatched or Larry confirms E3.2 deferred.
- `34 larry-reject-*.json in beacon/.invalid/` (iters 120-124). Need Larry to re-deposit (re-issue those 4 approvals/rejects via Telegram) or explicitly defer.
- `heal-claude-max-burn-rate watch` (iter 158). rate-limit-resilience-001 COMPLETE (all 4 PRs merged). Watch for alert frequency drop Monday 2026-06-01.
- `Telegram getUpdates "Network is unreachable"` G-rule dispatched iter 57 (archived). No recurrence since. Continue monitoring.
- `SUPABASE_SERVICE_ROLE_KEY rotation due 2026-08-22` — 90d cadence. Enters 60d notification window 2026-06-23. No action now.

---

## Recurring patterns promoted to permanent fixes (closed)

- **2026-05-11 — Dirty tree (Pulse operational writes). CLOSED iter 16.** `run_cycle.sh` auto-commit added (commit 6b6284a). Clean since iter 16.
- **2026-05-15 — gh pr merge allowlist. CLOSED iter 41.** `agents/pulse/.claude/settings.json` allows `Bash(gh pr merge:*)` + `Bash(git branch:*)` (PR #21).
- **2026-05-15 — D3.5 infra decommission. CLOSED iter 35.** 4 decommissioned services codified in cycle-prompt.md Check C. Do not escalate as "down."
- **2026-05-31 (iter 135) — PR #211 step-a-rotation MERGED.** Rotation hardening (auth gate, auth_401 circuit-breaker, tier-aware logs, Tier 2 probe 6h cadence, 88 unit tests) live.
- **2026-05-31 (iter 134) — PR #213 extend-thresholds MERGED.** Check III threshold implementation: beacon _default→2147s, pulse _default→262s. forge/mirror deferred.
- **2026-05-30 (iter 109) — Sync commit guard false positive on MEMORY.md FIXED.** afe9d07 merged. Guard scoped correctly. Dirty tree pattern (iters 99-108) ended.
- **2026-05-26 — Check III analyzer shipped (PR #112).** First run iter 126. Next run 2026-06-07.
- **2026-05-29 — Check IX analyzer shipped (PR #179).** First firing 2026-06-01 (Monday-only). Scans 4 operator-friction signals.
- **2026-05-30 (iter 124) — PR #210 fix(auth): wire dispatches MERGED.** long-lived setup-tokens path live.
- **2026-05-31 (iter 213) — PR #223 feat(healer): auto-remediate install-drift missing-install case MERGED.** `_remediate_missing_install` added with post-verify gate (ActiveState=active AND NextElapse non-empty). `config/auto-remediation-allowlist.json` created. 43 tests pass. heal-stale-daemon-code will reload healer on next sweep. G-rule (systemd install-drift, 1/3 iter 158) CLOSED.
- **2026-05-31 (iter 211) — PR #221 fix(healer): auto-resume timer OnCalendar MERGED.** heal-resume-paused-on-tier1.timer switched from OnUnitActiveSec (silent-death risk) to OnCalendar=*:0/10 + Persistent=true; re-installed live. Timer active+waiting, fires every 10 min.
- **2026-06-01 (iter 304) — PR #225 cycle.timer infinity-trap fix MERGED.** Timer switched from OnUnitActiveSec (infinity-trap on stop+start while idle) to `OnCalendar=*:0/5, Persistent=true`. Wall-clock anchor guarantees next-fire; immune to the trap. Automated cycles resumed after iter 259 stuck-timer onset. G-rule 2/3 closed.
- **2026-06-01 (iter 305) — PR #226 MERGED.** "Decouple stuck-timer heal from DM cooldown in install-drift healer" — DM cooldown no longer gates stuck-timer auto-heal; healer can fire without being blocked by a prior DM cooldown.
- **2026-06-01 (iter 305) — PR #227 MERGED.** "Fix stuck-timer alert leak + immunize 3 timers from monotonic infinity trap" — fixed alert leak and hardened 3 additional timers against the OnCalendar/OnUnitActiveSec infinity trap.
- **2026-06-01 (iter 306) — PR #228 MERGED.** "feat(medic): scaffold alert-operator (PR1 escalate-only)" — Medic alert-operator scaffold PR1 complete. Mirror reviewed and approved between iters 305 and 306. Build cost ~$0.41.
- **2026-06-01 (iter 306) — PR #229 MERGED.** "fix/alert-translations-6-missing" — 6 pre-existing healer alert translations added to config/alert-translations.json. Expands Tier 3 known-pattern allowlist for Check 0.
- **2026-06-01 (iter 375) — PR #231 MERGED.** "feat(medic): wire real rate-window gauge and per-session timeout" — `_rate_window_ok()` now reads real `heal_claude_max_burn_rate.recent_rate_limit_event_count()` + `active_tier.cooldown_until()` (fail-open); threshold configurable via `OURLIBERTY_MEDIC_RATE_WINDOW_MAX_EVENTS` (default 2). `run_medic.sh` wraps claude --print in `timeout $CLAUDE_TIMEOUT` (default 10m, `MEDIC_CLAUDE_TIMEOUT` override); exit 124 propagates + EXIT trap releases lock. 49 medic + 5 timeout tests pass. `medic-hardening-ratewindow-timeout-001` CLOSED.
- **2026-06-01 (iter 376) — PR #232 MERGED.** "rotation-gate-dm-isolation-001" — `_dm_auth_blocked` short-circuits when `OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE!=true` (sentinel in [Service] block, not .env.larry); `DmAuthBlockedServiceGateTest` added (3 cases: sentinel fires / no-sentinel suppresses / non-true suppresses). Pre-existing 4 test failures unaffected. Operator daemon-reload note in PR body. `rotation-gate-dm-isolation-001` CLOSED.
- **2026-05-30 (iter 115) — Check I journal idempotency fixed.** Commit 64fdcfb. Idempotency guard + auto-commit on actual write.

---

## System-state assumptions still relevant

- **Bash read-only commands require manual approval in interactive sessions.** git status, git branch, systemctl is-active, gh pr list require user approval each invocation in interactive sessions (no pre-approved allowlist in pulse .claude/settings.json for these). The sync.json and gitStatus session-start context are the reliable sources for source repo checks when Bash is blocked.

- **`relaunch-missing-bot` broken for pulse-bot.** `pulse_telegram_bot.sh` does not exist. `systemctl restart` requires interactive auth. Both fail. APPROVAL_REQUEST pending (pulse_telegram_bot.sh launcher).

- **Stuck automated cycle failure mode.** Automated cycles can hang silently. CYCLE_TIMEOUT_SEC guard pending Larry authorization (APPROVAL_REQUEST stuck-cycle timeout guard, iter 43+).
