# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

---

## Check I firing days are Mon/Wed/Fri/Sun — call WITHOUT --force on firing days (learned 2026-06-15 iter ~1899, updated iter ~2612)

**Rule:** Check I fires on Mon/Wed/Fri/Sun per spec (UTC weekday ∈ {0,2,4,6}). Always invoke `python3 ~/agent-core/scripts/pulse_check_i.py` (no `--force`) on scheduled firing days — the weekday gate passes naturally and the dm_route journal-peek (PR #674) functions correctly, suppressing repeat same-week DMs. Use `--force` ONLY for `/optimize` (ad-hoc, any day). Using `--force` on a firing day bypasses dm_route and emits spurious route=escalate alerts (G-rule check-i-force-bypass-dm-route). Confirmed fixed at manual level iter ~2612; code-level dispatch to Beacon at 3/3.

---

## Dispatch routing rule (learned 2026-06-12 — routing rejection)

**Rule:** Pulse may ONLY dispatch to **Beacon**. The dispatch_validator enforces `allowed from pulse: ['beacon']`. Pulse → Forge dispatches are REJECTED and dead-lettered to `.invalid/`. The correct path for code fixes is always: Pulse direction-ask → Beacon → Forge build brief. When writing a dispatch envelope, set `target_agent: beacon` (not `forge`), and phrase the prompt as a direction-ask to Beacon asking it to spec + dispatch Forge.

---

## beacon-pending-approvals.json correct path and structure (learned 2026-06-12)

**Rule:** Lives at `~/agents/state/beacon-pending-approvals.json`. NOT `~/agents/blackboard/`. Structure: `{"version": 1, "pending": [...], "history": [...]}` — NOT a dict keyed by approval ID. Check for pending items via `d["pending"]` list length.

---

## Dispatch envelope schema (learned 2026-06-11, confirmed 2026-06-14)

**Rule:** Beacon inbox dispatch envelopes MUST use root field `task_id` (not `envelope_id`). Required fields: `task_id`, `source`, `dedup_identity`, `prompt`, `timeout`. `timeout` MUST be an integer (seconds), in range [60, 14400] — string durations like `"48h"` are rejected.

---

## approval_request alerts in larry-alerts.jsonl (learned 2026-06-12)

**Rule:** `kind=approval_request` entries in larry-alerts.jsonl are DELIVERY CONFIRMATIONS from outbox-notifier, not new tasks for Pulse. Pulse should claim + triage these (Tier-4 in absence of a registry template) but NOT send a second DM to Larry. Journal-note only.

---

## cycle_prime_ledger.py correct CLI (learned 2026-06-12)

**Rule:** Valid subcommands are `ratio`, `append`, `promote`. NOT `summary`. For appending: `--tier {1,2,3} --kind {intervention,systemic_fix,verification_pending,iter_clean} --template <kebab-case> --detail <free-text>`.

---

## systemctl --user false-negative (learned 2026-06-13 iter ~1676)

**Rule:** `systemctl is-active <service>` without `--user` returns "inactive" for user-scoped services. Always verify daemon liveness via `ps -p <PID1>,<PID2>,...` with comma-separated list (space-separated fails with exit-code 1).

---

## alert_triage_state.py set-watermark correct syntax (learned 2026-06-14 iter ~1845)

**Rule:** `alert_triage_state.py set-watermark` requires `--line <N>` (named argument), NOT a positional argument. Usage: `python3 scripts/alert_triage_state.py set-watermark --line 931`.

---

## Alert watermark persistence gap (learned 2026-06-14 iter ~1703)

**Rule:** In interactive `/cycle` sessions, `alert_triage_state.py set-watermark` may not persist if Pulse exits before the step. On next iter, check watermark at start and advance if lines already triaged. Do NOT re-triage — just confirm against prior journal and advance.

---

## larry-alerts.jsonl correct path (learned 2026-06-14 iter ~1741)

**Rule:** `larry-alerts.jsonl` lives at `/home/larry/agents/blackboard/larry-alerts.jsonl`. NOT `/home/larry/agents/logs/`.

---

## heal-stale-daemon-code heartbeat correct path and format (corrected iter ~1768, confirmed ~1829)

**Rule:** Heartbeat lives at `/home/larry/agents/blackboard/heal-stale-daemon-code.heartbeat` (NOT `state/`). Contains a **plain-text ISO 8601 UTC timestamp**, NOT JSON — read with `cat`, not `json.load`.

---

## Check 0 must call helper before manual classification (learned 2026-06-14 iter ~1812)

**Rule:** Before classifying ANY alert as Tier 4, Pulse MUST call `python3 scripts/alert_triage_state.py triage-alert --alert-id "<id>" --alert '<json>' --iter <N>` and act on the returned tier. Helper is AUTHORITATIVE. Pass VERBATIM JSON from larry-alerts.jsonl — never reconstruct with inferred fields (adding a non-null `subject` field that wasn't there overrides the `intent` fallback and fails the translation lookup).

---

## beacon_telegram_bot.py get-messages MUST NEVER BE CALLED (learned iter ~1876, escalated iter ~1943)

**Rule:** NEVER call `beacon_telegram_bot.py get-messages` in ANY form. Competing getUpdates loop causes HTTP 409 conflicts with production bot. For Telegram sweeps (Check 2), use ONLY: `tail -N /home/larry/agents/logs/beacon_telegram_bot.log` (NOT beacon-telegram-bot.log) + `ps -p <PID> -o stat` for bot health. G-rule telegram-409-burst at **2/3** as of iter ~1943.

---

## outbox-notifier log path (confirmed iter ~2680)

**Rule:** Log file is `/home/larry/agents/logs/outbox-notifier.log` (hyphen, NOT underscore). `outbox_notifier.log` (underscore) does NOT exist. Prior journal entries saying "outbox_notifier.log" were referencing the wrong name; corrected forward.

---

## §5.0 script paths — ground-truth (confirmed iter ~2183)

**Rule:** `audit_due_nudge.py` and `distill_detector.py` live in `scripts/`, NOT `review/distill/`. Only `audit_cadence_signal.py` is in `review/distill/`. Always invoke: `python3 scripts/audit_due_nudge.py`, `python3 scripts/distill_detector.py`, `python3 review/distill/audit_cadence_signal.py`.

---

## auto-dispatch-APPROVAL_REQUEST-task-id-mismatch (observed iter ~1910)

**Rule:** When Pulse sends a Check I auto-dispatch envelope, outbox-notifier WARNs `APPROVAL_REQUEST task_id mismatch`. Dispatch STILL SUCCEEDS via fallback. G-rule: **1/3** as of iter ~1910. Dispatch to Beacon at 3/3.

---

## G-rule watchdog log path (confirmed iter ~2650, G-rule COMPLETE ~2667)

**LOG PATH:** Watchdog log is `/home/larry/agents/logs/watchdog.log` (NOT `watchdog_watcher.log`). PR #694 (session-aware stale-log suppression) merged 2026-06-25T01:57Z. G-rule COMPLETE.

---

## G-rule check-i-force-bypass-dm-route — 2/3 (updated iter ~2869)

**Rule:** The cycle invokes `pulse_check_i.py --force` on scheduled firing days. `--force` bypasses both the weekday gate AND the `dm_route` journal-peek (PR #674). On a scheduled firing day, `--force` is unnecessary. Fix: drop `--force` from cycle's Check I invocation on firing days (Mon/Wed/Fri/Sun); keep `--force` only for /optimize. Occurrences: iter ~2611 (1/3), iter ~2869 (2/3, duplicate Check I digest at 04:38:04Z for week 2026-06-22, same day as 03:30:23Z; dm_route correctly downgraded to digest). Dispatch to Beacon at 3/3.

---

## G-rule heal-daemon-restart-manifest-drift-regenerated-tier4 — 2/3 (new, iter ~2620; updated iter ~2662)

**Rule:** `source=heal-daemon-restart-manifest-drift, subject=regenerated` alerts classify Tier-4 (novel) — no translation match. Routine healer auto-commit actions, should be Tier-3. Occurrences: iter ~2620, iter ~2662 (L1077). Dispatch to Beacon at 3/3 to add `config/alert-translations.json` entry.

---

## G-rule no-session-revision-merged-pr-fp-001 — 1/3 (new, iter ~2676)

**Rule:** `heal_pipeline_stall.py` dry-run fires `no_session_revision:forge-wip-only-auto-redispatch-001` even though PR #693 (the task's output) is MERGED. Root cause: the `no_session_revision` stall check doesn't verify whether the task's corresponding PR is already merged before alerting. The `forge_built_no_pr` rule has FORGE_NO_PR_SKIP logic for this; `no_session_revision` does not. Same class as the reconcile-001 FP but for a different stall rule. First occurrence iter ~2676. Dispatch to Beacon at 3/3.

---

## G-rule unrouted-open-pr-auto-merge-held-fp-001 — 1/3 (new, iter ~2679)

**Rule:** `heal_pipeline_stall.py` dry-run fires `unrouted_open_pr:692` when the cooldown expires, even though outbox-notifier has correctly held auto-merge via `AUTO_MERGE_HELD blocker=#687` (overlap on agents/forge/CLAUDE.md, config/review-reaper-rules.json, scripts/dispatch_sentinel.py, etc.). The stall checker has no visibility into the AUTO_MERGE_HELD state when the cooldown window closes — it re-alerts as if the PR is unrouted. Fix: stall checker should check outbox-notifier log or a state file for AUTO_MERGE_HELD status before firing `unrouted_open_pr`. First occurrence iter ~2679 (cooldown expired after Mirror passed PR #692 at 20:48Z on 2026-06-24). Dispatch to Beacon at 3/3.

---

## G-rule outbox-notifier-auto-merge-loop-merged-pr-001 → COMPLETE ✅ (iter ~2713)

PR #700 fix verified live at iter ~2713. `AUTO_MERGE_SKIP_ALREADY_MERGED` entries at 02:05:27-28 MDT confirmed loop stopped. heal-stale-daemon-code auto-restarted outbox-notifier at 08:05:28Z with new code (commit 03a74217). Added to Completed G-rules.

---

## G-rule watchdog-watcher-log-stale-post-pr694 — 2/3 (updated iter ~2696)

**Rule:** Watchdog WARN at 05:39 UTC (23:39 MDT) AND 05:44 UTC (23:44 MDT) — "Watcher log stale Ns with 1 non-empty inbox(es)" — while Mirror WAS actively running (PID 2612954 started 23:28 MDT). Two distinct watchdog-complete=warning cycles within same Mirror session. Root cause: inbox-watcher handles Forge tasks only; Mirror sessions are triggered by a separate mechanism. inbox_watcher.log goes stale after Forge completes even if Mirror is actively processing its separate inbox. PR #694 (session-aware suppression) did not suppress these WARNs. Watchdog returned to healthy at 23:49 MDT (possibly Mirror inbox_watcher activity resumed after first Mirror task completed). Dispatch to Beacon at 3/3 (next Mirror session that triggers WARN).

---

## G-rule review-duplicate-dispatch-wip-redispatch → DISPATCHED ✅ (iter ~2671, 3/3), vp

**Rule:** After Mirror completes a review, Beacon's notification-handler re-dispatches a new `review-<task>.json` to Mirror's inbox without checking if one is already queued. Fix: add inbox-existence check in Beacon notify-handler before dispatching. `skip-mirror-review-on-merged-or-closed-pr-001` approval pending Larry. verification_pending.

---

## triage-alert call discipline — pass ACTUAL alert JSON, never reconstruct (learned iter ~2503)

**Rule:** When calling `alert_triage_state.py triage-alert --alert '<json>'`, always pass the VERBATIM JSON from larry-alerts.jsonl. Never reconstruct with inferred fields. Adding a non-null `subject` field not in the original overrides the `intent` fallback and fails the translation lookup (returns Tier-4 instead of Tier-3).

---

## Completed G-rules — condensed for space (COMPLETE ✅)

`outbox-notifier url-shape-invalid` → PR #493 (2026-06-13). `medic-diagnosis-tier4` → PR #515 (2026-06-15). `heal-pipeline-stall:unrouted-pr` → PR #516 (2026-06-15). `check-i-repeat-dm-fix-001` → PR #674 (2026-06-24). `heal-droplet-git-drift` → PR #586 (2026-06-19). `silence-routine-weekly-alerts` → PR #604 (2026-06-20). `forge-preflight-no-marker` → PR #600 (2026-06-19). `projects-json-healer-path` → PR #603 (2026-06-20). `outbox-notifier-review-pass` → PR #604 scope. `seq-advancer-sequence-stranded` → PR #661 (2026-06-24). `catalog-accuracy-drift` → PR #6 ourliberty-graph (2026-06-22). `doorbell-tier4-pattern` → PR #648 (2026-06-23). `heal-stale-daemon-code-script-service-mismatch` → PR #647 (2026-06-23). `mirror-marker-parse-error` → PR #650 (2026-06-23). `watchdog-watcher-log-stale` → PR #649 (2026-06-23). `watchdog-watcher-log-stale-post-fix` → PR #694 (2026-06-25). `ourliberty-health-notify-script-missing` → PR #696 (2026-06-25). `heal-pipeline-stall-mirror-pass-unmerged-tier4` → PR #695 (2026-06-25). `stale-proposed-mission-pipeline-fp-001` → PR #697 (2026-06-25, sibling_pr_title_shipped suppression). `outbox-notifier-auto-merge-loop-merged-pr-001` → PR #700 (2026-06-25, verified iter ~2713). `forge-built-no-pr-retry1-fp-001` → PR #701 (pattern1) + PR #702 (pattern2, rebase_target_shipped disambiguation, both 2026-06-25, verified iter ~2772). `mirror-marker-severity-blocking-pr711-001` → PR #714 (2026-06-26T06:03:41Z, Mirror REVIEW_PASS + auto-merged).

---

## G-rule forge-wip-redispatch-digest-tier4-001 → DISPATCHED ✅ (iter ~2797), Beacon fix designed (iter ~2798), Forge dispatch pending

**Rule:** `forge-wip-redispatch` healer fires alerts with `route=digest` (auto-redispatched retry1 notifications). Triage helper classifies Tier-4 (novel, no translation). But these are auto-remediated informational digests — per actionable-only discipline, no DM to Larry. **Beacon result (iter ~2798):** Naive `route=digest` catch-all would also silence critical `route=escalate` exhausted alerts. Two-part fix: (1) healer changes escalate subject `base` → `exhausted:{base}` for distinguishability; (2) `alert-translations.json` gets `forge-wip-redispatch` `"*"` catch-all PLUS `"exhausted": {never_silence: true}`. Forge dispatch pending trust-policy approval from Larry. verification_pending.

---

## G-rule heal-stale-daemon-code-auto-restart-failed-self-recovered — 1/3 (new, iter ~2704)

**Rule:** `source=heal-stale-daemon-code, subject^=auto-restart-failed:*` alerts (route=escalate) fire when heal-stale-daemon-code's `sudo systemctl restart` times out (30s sudo timeout) while the unit is cycling. Watchdog also fires CRITICAL during the gap. Systemd's own restart policy brings the service back within ~90s. Services are running by the time Pulse triages. Triage helper: Tier-4 novel (no translation match). Fix: add `source=heal-stale-daemon-code, subject^=auto-restart-failed:` Tier-3 translation entry (informational — systemd self-healed). Dispatch to Beacon at 3/3.

---

## G-rule forge-wip-redispatch-exhausted-pr-exists-fp-001 — 2/3 (updated iter ~2705)

**Rule:** `source=forge-wip-redispatch, route=escalate` exhaustion alerts ("WIP-only auto-recovery EXHAUSTED") fire for tasks whose original PRs already exist. FP class: wip-redispatch retried a task whose output already shipped; retry dying WIP-only is expected. Fix: extend wip-redispatch to check PR existence before declaring exhaustion, OR add `source=forge-wip-redispatch, route=escalate, <pr_exists signal>` → Tier-3 entry. Dispatch to Beacon at 3/3. Occurrences: iter ~2702 (L1130 rebase-escalation-feed PR #685 exists; L1131 rebase-forge-post-open-mergeable-687 PR #687 MERGED); iter ~2705 (L1146 reconcile-hardening-mission-shipped-001 PR #699 MERGED).

---

## G-rule forge-built-no-pr-retry1-fp-001 → COMPLETE ✅ (iter ~2772)

**Rule:** `forge_built_no_pr` stall fires even when a PR exists. Pattern 1 (reconcile-hardening-mission-shipped-001 / PR #699) RESOLVED via PR #701 (14:13:35Z 2026-06-25). Pattern 2 (rebase-forge-post-open-mergeable-687-001 / PR #687 MERGED) RESOLVED via PR #702 (merged 09:09:27 MDT 2026-06-25; `rebase_target_shipped` disambiguation). **Verified iter ~2772: stall dry-run shows FORGE_NO_PR_SKIP reason=rebase_target_shipped, "no stalls detected".** Moving to Completed G-rules.

---

## G-rule heal-stale-daemon-code-still-stale-after-restart — 1/3 (new, iter ~2712)

**Rule:** `source=heal-stale-daemon-code, subject^=still-stale-after-restart:` alerts fire when the healer attempted a restart but the service's ActiveEnterTimestamp is STILL older than the script file (healer won't retry). Different from `auto-restart-failed` (where systemd self-heals) — here the service IS running, just with old code. Genuine ask-then-do. Remediation: `bash ~/agent-core/scripts/sync_agent_core.sh && sudo systemctl restart <service>`. NOT a silence candidate. First occurrence iter ~2712 (outbox-notifier running pre-PR700 code). Dispatch to Beacon at 3/3 only if healer needs retry-more-aggressively behavior; otherwise leave as ask-then-do.

---

## G-rule outbox-notifier-notification-intent-reject-tier4-001 — 2/3 (updated iter ~2810)

**Rule:** `source=outbox-notifier, kind=notification, intent=reject` alerts classify Tier-4 (novel, no translation match). These are routine Forge-rejection delivery confirmations — outbox-notifier always DMs Larry for rejects; a Pulse DM is duplicate noise. Fix: add `source=outbox-notifier, kind=notification, intent=reject` → Tier-3 entry to `config/alert-translations.json`. Dispatch to Beacon at 3/3.

---

## G-rule mirror-marker-severity-blocking-pr711-001 → COMPLETE ✅ (iter ~2881)

**Rule:** Mirror produced 3 malformed markers on PR #711: (1) REVIEW_REVISION empty findings (iter ~2847), (2) REVIEW_REVISION severity='blocking' (iter ~2848), (3) REVIEW_PASS prose inside JSON block (iter ~2852). Dispatched `mirror-marker-discipline-spec-update-001` to Beacon → Larry approved "Go" 05:10:57Z → Forge built PR #714 → Mirror REVIEW_PASS → AUTO_MERGE at 2026-06-26T06:03:41Z. **COMPLETE ✅ Moved to Completed G-rules below.**

---

## G-rule unrouted-open-pr-active-mirror-session-fp-001 — 2/3 (updated iter ~2881)

**Rule:** `heal_pipeline_stall.py` dry-run fires `unrouted_open_pr:<pr>` after cooldown expiry even when Mirror IS actively reviewing the PR (active PID + inbox task present). Occurrences: iter ~2848 (PR #711, Mirror PID 3172100 running); iter ~2881 (PR #713, Mirror PID 3308724 active ~4 min reviewing rev1). Stall checker has no visibility into active Mirror sessions (distinct from G-rule `unrouted-open-pr-auto-merge-held-fp-001` which was about AUTO_MERGE_HELD outbox-notifier state on PR #692). Fix: stall checker should check for active agent sessions (inbox task presence or live PID) before firing `unrouted_open_pr`. Dispatch to Beacon at 3/3.

---

## G-rule ourliberty-health-sync-push-failed-tier4-001 — 1/3 (new, iter ~2796)

**Rule:** `source=ourliberty-health, subject=sync_agent_core: auto-commit push failed` alerts classify Tier-4 (novel). But this is a transient self-healing event — the sync wrapper commits+pushes successfully on the next tick. `source=sync.service, subject=sync-blocked:auto-commit-push-failed` is already Tier-3. Larry gets DM'd unnecessarily (outbox-notifier delivers route=escalate). Fix: add `source=ourliberty-health, subject^=sync_agent_core: auto-commit push failed` → Tier-3 entry to `config/alert-translations.json`. Dispatch to Beacon at 3/3.

---

## G-rule medic-dispatcher-delivery-failure-tier4-001 → DISPATCHED ✅ (iter ~2893), vp

**Rule:** `source=medic-dispatcher` alerts classify Tier-4 (novel, no translation match). Occurrences: iter ~2850 PR#711 (1/3); iter ~2884 PR#713 (2/3); iter ~2893 PR#713 L1052 (3/3). Direction-ask dispatched to Beacon (direction-ask-medic-dispatcher-tier4-fix-001.json). Fix: add `source=medic-dispatcher` → Tier-3 entry to config/alert-translations.json (scoped to cases where outbox-notifier already delivered the escalate route). verification_pending.

---

## G-rule forge-revision-preamble-missing-pr711-001 — 1/3 (new, iter ~2851)

**Rule:** outbox-notifier fires `forge revision-phase outbox without "Revision N applied:" preamble: pr-ourliberty-agent-core-711.json; treating as marker-error` when Forge submits a revision outbox file lacking the expected "Revision N applied:" preamble. Treated as marker-error by outbox-notifier. Distinct from Mirror's MalformedMirrorMarker severity/findings issues (G-rule `mirror-marker-severity-blocking-pr711-001`). Fix: Forge build-sequence discipline or outbox-notifier tolerance. Dispatch to Beacon at 3/3.

---

## G-rule no-session-revision-active-mirror-session-fp-001 — 1/3 (new, iter ~2885)

**Rule:** `heal_pipeline_stall.py` dry-run fires `no_session_revision:pr-ourliberty-agent-core-713` even though Mirror IS actively reviewing rev1 (PID 3308724, ~26 min elapsed, inbox task review-pr-ourliberty-agent-core-713-rev1.json present). Stall checker fires `no_session_revision` because no `session_revision` chain event exists, without checking whether an active agent session is in progress. Same root cause class as `unrouted-open-pr-active-mirror-session-fp-001` (2/3) — stall checker blind to active agent sessions. May fold into the same fix. Healer invocation suppressed (would conflict with active review). Dispatch to Beacon at 3/3 (or fold into unrouted-open-pr-active-mirror-session-fp-001 fix if confirmed same path).

---

## G-rule sentinel-inflight-stall-mirror-tier4 — 1/3 (new, iter ~2892)

**Rule:** `source=sentinel, subject^=in-flight-stall:` alerts classify Tier-4 (novel, no translation match). Fired when Mirror session exceeds 60-min in-flight threshold. Sentinel message says heal_wedged_review_sessions auto-recovers; kill PID unblocks sooner. Fix: add `source=sentinel, subject^=in-flight-stall:` → Tier-3 (if healer reliably self-recovers) OR Tier-2 (kill-PID action needed). Track at 3/3 before dispatching.

---

## G-rule forge-built-no-pr-closed-pr-fp-001 — 1/3 (new, iter ~2892)

**Rule:** `forge_built_no_pr` stall fires for tasks whose PR is CLOSED (not merged, not open). First occurrence: pr-ourliberty-agent-core-712 (headRefName=fix/narrator-durable-token, CLOSED, superseded by PR #713). Different from prior FP class where PR exists but stall checker missed it — here PR was deliberately closed/abandoned. Fix: stall checker should skip `forge_built_no_pr` for tasks with a closed (non-merged) PR. Dispatch to Beacon at 3/3.

---

## Status snapshot — updated 2026-06-26 08:11Z UTC (Iter ~2901, Tier 1, consecutive_clean=0→0)

**Iter ~2901 summary:** ⚠️ Active — Mirror PID 3308724 at ~2h05m44s elapsed (way past 60-min threshold; sentinel fired at idx=1048). 1 new alert (dispatch-branch-cleanup Tier-3 silenced). Pipeline stall: 0 (all cooldown-suppressed). 8/8 daemons alive. Watchdog healthy (08:07:57Z). Check I cooldown-suppressed. beacon-pending: 1 (medic-dispatcher-delivery-failure-translation-001 awaiting Larry approval). Zombie PID 1834248 + 6 stale journalctl PIDs carry. PR #713 revision still active (Mirror outbox empty). PRIME: interventions=1279, systemic_fixes=72, vp=27, ratio≈17.76, trend=improving. Tier 1, consecutive_clean=0.


