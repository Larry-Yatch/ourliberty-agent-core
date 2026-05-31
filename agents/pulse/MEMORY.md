# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

---

## Status snapshot — updated 2026-05-31 ~15:07Z UTC (Iter 195 — interactive, full cycle)

**System: ⚠️ Degraded — Tier 1, consecutive_clean=0 (carry-forward).** Active pipeline stalls (forge + beacon-bot, root cause: Tier 2 OAuth expired). Sync.json error is a **stale artifact** — repo is actually in sync (local HEAD = remote HEAD = be7ddde). 7/7 services active. 0 open PRs. All inboxes empty. Alert watermark: 1083 (unchanged). APPROVAL_REQUEST queue: 7 (unchanged, 2 items elevated).

**Watch items:**
- **TIER 1 ACTIVE.** 5-min cadence. consecutive_clean=0 (active stalls).
- **TIER 2 OAUTH EXPIRED (ELEVATED — ACTIVE STALLS).** forge + beacon-bot tasks paused_on_tier1 since 13:59Z UTC. Fix: `docs/runbooks/restore-larry-personal-claude-oauth-tier2.md`.
- **SYNC-PUSH-REBASE-FALLBACK-001 CONFIRMED.** Materialized at 13:50:29Z UTC. Approve the defensive hardening fix. NOTE: sync.json still shows error from that failure, but repo is actually in sync — confirmed by stale-sync auto-fix at iter 195 (sync_agent_core.sh: no changes needed, local = remote).
- **Check VIII/IX FIRST FIRING TOMORROW (2026-06-01 UTC).** Both analyzers first-ever run. Monitor for unexpected output or errors.
- **Monday [yellow] DM: 2026-06-01 UTC (TOMORROW).** Elevated scope: Tier 2 OAuth active stalls + sync-push-rebase-fallback-001 + Check VIII/IX first-firing note.
- **Healer heartbeat static.** 14:37:20Z UTC across iters 193–195 (3 consecutive). 29 min old at iter 195 check; within 90-min threshold. Escalation trigger: if still static AND >60 min old at next check (~15:37:20Z UTC).
- **APPROVAL_REQUEST queue (7):** pulse-grule-check-c-canonical-names-001, alert-triage-persistence-invocation-001, **sync-push-rebase-fallback-001 (ELEVATED — confirmed failure)**, pulse_telegram_bot.sh launcher, stuck-cycle timeout guard, **Tier 2 OAuth restore (ELEVATED — active stalls)**, forge-claude-md-preflight-self-check-bullet-001. + heal-resume-paused-on-tier1 install (ask-then-do carry-forward iter 158).

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

- **Stale imported-module gap (1st observation, iter 120).** heal-stale-daemon-code tracks main-script mtime but not imported Python module mtimes. inbox-watcher loaded stale dispatch_validator.py for ~40 min post-update. Watch threshold=3.

- **Dispatch source discipline.** Pulse G-rule dispatches must use `source="pulse"` (canonical). `"pulse-g-rule"` is NOT in ALLOWED_SOURCES — validator silently rejects it. Observed iter 91 false dispatch. All future G-rule dispatches: use `source="pulse"`.

---

## Open pending watch items (APPROVAL_REQUEST + G-rule)

**G-rule items (active, not yet at 3/3):**
- `heal-pr-auto-merge blind to CONFLICTING`: G-rule 2/3 (iters 127, 128). Next occurrence → dispatch to Beacon.
- `heal-pipeline-stall "369 min" duration bug`: G-rule 1/3 (iter 128). Mirror PASS timestamp vs PR-creation timestamp mismatch.
- `inbox-watcher rc=-1`: G-rule 2/3 (iters 117, 123). healer's 30s timeout shorter than service startup time. Next occurrence → dispatch to Beacon.
- `systemd install-drift`: G-rule 1/3 (iter 158 — PR #219 shipped unit files without install dance). Next 2 → dispatch to Beacon for Forge PR template checklist update.
- `cycle.timer stuck pattern`: G-rule 1/3 (iter 158 — both cycle.timer + heal-systemd-install-drift.timer infinity-trap simultaneously). Watch 14d.
- `MalformedForgeMarker G-rule`: DISPATCHED (iter 150). Post-dispatch counter: 4 self-resolved. Doc-fix APPROVAL_REQUEST `forge-claude-md-preflight-self-check-bullet-001` pending Larry. G-rule stays open until doc-PR merges.

**APPROVAL_REQUEST items (Larry-gate):**
- `pulse-grule-check-c-canonical-names-001` — Beacon APPROVAL_REQUEST for doc-fix to cycle-prompt.md § 4.3 (canonical service names). Trust-policy → Forge pending.
- `alert-triage-persistence-invocation-001` — alert-triage.json invocation gap fix. Beacon diagnosed: neither run_cycle.sh nor cycle-prompt.md § 3.0 invokes alert_triage_state.py. Pending Larry approval.
- `sync-push-rebase-fallback-001` — sync_agent_core.sh:161 bare-push has no rebase fallback. Confirmed 21 consecutive clean Check B cycles; acute risk resolved. Pending as defensive hardening.
- `pulse_telegram_bot.sh launcher` — pulse-bot restart path broken (pulse_telegram_bot.sh doesn't exist; systemctl restart requires interactive auth). Option A: create launcher script. Pending Larry.
- `stuck-cycle timeout guard` — CYCLE_TIMEOUT_SEC=1800 + timeout wrapper in run_cycle.sh. Multiple occurrences across cycle history. Pending Larry (iter 43).
- `Tier 2 OAuth restore` — tier2-verifier-probe-001 REJECTED (iter 149); Forge confirmed expired OAuth ~10:39Z May 30. Runbook: docs/runbooks/restore-larry-personal-claude-oauth-tier2.md.
- `forge-claude-md-preflight-self-check-bullet-001` — doc-only: pre-emit self-check bullet in Forge CLAUDE.md Preflight discipline. Beacon dispatched after G-rule 3/3.

**Carry-forward ask-then-do:**
- `heal-resume-paused-on-tier1 NOT INSTALLED` (iter 158). PR #219 shipped ourliberty-heal-resume-paused-on-tier1.service + .timer but install dance not performed. Without install, paused-on-tier1 tasks won't auto-resume. Action: SSH + sudo cp + daemon-reload + enable --now.

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
- **2026-05-30 (iter 115) — Check I journal idempotency fixed.** Commit 64fdcfb. Idempotency guard + auto-commit on actual write.

---

## System-state assumptions still relevant

- **Bash read-only commands require manual approval in interactive sessions.** git status, git branch, systemctl is-active, gh pr list require user approval each invocation in interactive sessions (no pre-approved allowlist in pulse .claude/settings.json for these). The sync.json and gitStatus session-start context are the reliable sources for source repo checks when Bash is blocked.

- **`relaunch-missing-bot` broken for pulse-bot.** `pulse_telegram_bot.sh` does not exist. `systemctl restart` requires interactive auth. Both fail. APPROVAL_REQUEST pending (pulse_telegram_bot.sh launcher).

- **Stuck automated cycle failure mode.** Automated cycles can hang silently. CYCLE_TIMEOUT_SEC guard pending Larry authorization (APPROVAL_REQUEST stuck-cycle timeout guard, iter 43+).
