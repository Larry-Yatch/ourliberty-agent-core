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

## §5.0 script paths — ground-truth (confirmed iter ~2183)

**Rule:** `audit_due_nudge.py` and `distill_detector.py` live in `scripts/`, NOT `review/distill/`. Only `audit_cadence_signal.py` is in `review/distill/`. Always invoke: `python3 scripts/audit_due_nudge.py`, `python3 scripts/distill_detector.py`, `python3 review/distill/audit_cadence_signal.py`.

---

## auto-dispatch-APPROVAL_REQUEST-task-id-mismatch (observed iter ~1910)

**Rule:** When Pulse sends a Check I auto-dispatch envelope, outbox-notifier WARNs `APPROVAL_REQUEST task_id mismatch`. Dispatch STILL SUCCEEDS via fallback. G-rule: **1/3** as of iter ~1910. Dispatch to Beacon at 3/3.

---

## G-rule watchdog log path (confirmed iter ~2650, G-rule COMPLETE ~2667)

**LOG PATH:** Watchdog log is `/home/larry/agents/logs/watchdog.log` (NOT `watchdog_watcher.log`). PR #694 (session-aware stale-log suppression) merged 2026-06-25T01:57Z. G-rule COMPLETE.

---

## G-rule check-i-force-bypass-dm-route — 1/3 (new, iter ~2611)

**Rule:** The cycle invokes `pulse_check_i.py --force` on scheduled firing days. `--force` bypasses both the weekday gate AND the `dm_route` journal-peek (PR #674). On a scheduled firing day, `--force` is unnecessary. Fix: drop `--force` from cycle's Check I invocation on firing days (Mon/Wed/Fri/Sun); keep `--force` only for /optimize. Dispatch to Beacon at 3/3.

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

## G-rule stale-proposed-mission-pipeline-fp-001 → vp advancing (iter ~2673 dispatch, Beacon processed iter ~2674)

**Rule:** Pipeline stall checker fires `DRY-RUN would alert: forge_built_no_pr:reconcile-hardening-mission-shipped-001` every cycle. Root cause (corrected by Beacon iter ~2674): NOT missions.json — real fix is PR-title sibling supersession (reconcile-001 superseded by -002/PR#688 but no supersession check catches same-family different-token PRs). Beacon produced `forge-no-pr-sibling-pr-title-supersession-001` approval (03:04Z 2026-06-25, DM'd Larry). verification_pending: needs Larry approval → Forge build → Mirror review → merge.

---

## G-rule review-duplicate-dispatch-wip-redispatch → DISPATCHED ✅ (iter ~2671, 3/3), vp

**Rule:** After Mirror completes a review, Beacon's notification-handler re-dispatches a new `review-<task>.json` to Mirror's inbox without checking if one is already queued. Fix: add inbox-existence check in Beacon notify-handler before dispatching. `skip-mirror-review-on-merged-or-closed-pr-001` approval pending Larry. verification_pending.

---

## triage-alert call discipline — pass ACTUAL alert JSON, never reconstruct (learned iter ~2503)

**Rule:** When calling `alert_triage_state.py triage-alert --alert '<json>'`, always pass the VERBATIM JSON from larry-alerts.jsonl. Never reconstruct with inferred fields. Adding a non-null `subject` field not in the original overrides the `intent` fallback and fails the translation lookup (returns Tier-4 instead of Tier-3).

---

## Completed G-rules — condensed for space (COMPLETE ✅)

`outbox-notifier url-shape-invalid` → PR #493 (2026-06-13). `medic-diagnosis-tier4` → PR #515 (2026-06-15). `heal-pipeline-stall:unrouted-pr` → PR #516 (2026-06-15). `check-i-repeat-dm-fix-001` → PR #674 (2026-06-24). `heal-droplet-git-drift` → PR #586 (2026-06-19). `silence-routine-weekly-alerts` → PR #604 (2026-06-20). `forge-preflight-no-marker` → PR #600 (2026-06-19). `projects-json-healer-path` → PR #603 (2026-06-20). `outbox-notifier-review-pass` → PR #604 scope. `seq-advancer-sequence-stranded` → PR #661 (2026-06-24). `catalog-accuracy-drift` → PR #6 ourliberty-graph (2026-06-22). `doorbell-tier4-pattern` → PR #648 (2026-06-23). `heal-stale-daemon-code-script-service-mismatch` → PR #647 (2026-06-23). `mirror-marker-parse-error` → PR #650 (2026-06-23). `watchdog-watcher-log-stale` → PR #649 (2026-06-23). `watchdog-watcher-log-stale-post-fix` → PR #694 (2026-06-25). `ourliberty-health-notify-script-missing` → PR #696 (2026-06-25). `heal-pipeline-stall-mirror-pass-unmerged-tier4` → PR #695 (2026-06-25).

---

## Status snapshot — updated 2026-06-25 03:46Z UTC (Iter ~2679, Tier 1, consecutive_clean=0)

**Iter ~2679 summary:** ⚠️ Watch — PR #687 CONFLICTING (rebase-pr-687-post-open-mergeable-001 pending Larry). **KEY EVENTS: All agents IDLE. 0 new alerts (watermark holds at 1100). 4 pending approvals (carry). Pipeline stall: reconcile-001 FP persists (fix pending). NEW G-rule: unrouted-open-pr-auto-merge-held-fp-001 (1/3) — stall checker fires unrouted_open_pr:692 when cooldown expires despite AUTO_MERGE_HELD blocker=#687. No dispatches.** PRIME: interventions=1151, systemic_fixes=70, vp=25, ratio=16.44, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-25 03:42Z UTC (Iter ~2678, Tier 1, consecutive_clean=0)

**Iter ~2678 summary:** ⚠️ Watch — PR #687 CONFLICTING (rebase-pr-687-post-open-mergeable-001 pending Larry). **KEY EVENTS: All agents IDLE. 0 new alerts (watermark holds at 1100). 4 pending approvals (carry). Pipeline stall: reconcile-001 FP persists (fix pending). New stall output item: rebase-escalation-feed-685-001 FORGE_NO_PR_SKIP preflight_non_proceed (PR #685 MERGED, nominal). No dispatches.** PRIME: interventions=1150, systemic_fixes=70, vp=25, ratio=16.43, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-25 03:35Z UTC (Iter ~2677, Tier 1, consecutive_clean=0)

**Iter ~2677 summary:** ⚠️ Watch — PR #687 CONFLICTING (rebase-pr-687-post-open-mergeable-001 pending Larry). **KEY EVENTS: All agents IDLE. 0 new alerts (watermark holds at 1100). 4 pending approvals (carry). Pipeline stall: reconcile-001 FP persists (fix pending). G-rule no-session-revision-merged-pr-fp-001 did NOT recur (FORGE_NO_PR_SKIP handled it via pr_exists). No dispatches.** PRIME: interventions=1149, systemic_fixes=70, vp=25, ratio=16.41, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-25 03:28Z UTC (Iter ~2676, Tier 1, consecutive_clean=0)

**Iter ~2676 summary:** ⚠️ Watch — PR #687 CONFLICTING (rebase-pr-687-post-open-mergeable-001 pending Larry). **KEY EVENTS: All agents IDLE. 1 new alert (L1100 doorbell Tier-3 silenced). Watermark 1099→1100. New G-rule candidate: no-session-revision-merged-pr-fp-001 (1/3) — stall checker fires no_session_revision for forge-wip-only-auto-redispatch-001 despite PR #693 MERGED. 4 pending approvals (carry). No dispatches.** Pipeline stall: reconcile-001 FP (fix approval pending). Check I: Thursday, skip. PRIME: interventions=1149, systemic_fixes=70, vp=25, ratio=16.41, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-25 03:18Z UTC (Iter ~2675, Tier 1, consecutive_clean=0)

**Iter ~2675 summary:** ⚠️ Watch — PR #687 CONFLICTING (rebase-pr-687-post-open-mergeable-001 pending Larry). **KEY EVENTS: All agents IDLE. 1 new alert (L1099 Tier-3 approval_request delivery confirmation, silenced). Watermark 1098→1099. 4 pending approvals (carry). No dispatches.** Pipeline stall: reconcile-001 FP (fix approval pending). Check I: Thursday, skip. PRIME: interventions=1148, systemic_fixes=70, vp=25, ratio=16.4, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-25 03:07Z UTC (Iter ~2674, Tier 1, consecutive_clean=0)

**Iter ~2674 summary:** ⚠️ Watch — PR #687 CONFLICTING (rebase-pr-687-post-open-mergeable-001 pending Larry). **KEY EVENTS: Beacon processed stale-proposed-mission-pipeline-fp-001 (270s, $0.87) → NEW approval `forge-no-pr-sibling-pr-title-supersession-001` (03:04Z, DM'd Larry; root cause was PR-title sibling supersession, not missions.json). All agents IDLE.** 2 alerts (L1097-L1098, both Tier-3). Watermark 1096→1098. 4 pending approvals (unreg-approval-6009fbf6bfa2 stale; rebase-pr-687 active; skip-mirror-review; forge-no-pr-sibling-pr-title-supersession-001 NEW). Pipeline stall: reconcile-001 FP (fix pending approval). Check I: Thursday, skip. PRIME: interventions=1147, systemic_fixes=70, vp=25, ratio≈16.39, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-25 03:00Z UTC (Iter ~2673, Tier 1, consecutive_clean=0)

**Iter ~2673 summary:** ⚠️ Watch — PR #687 CONFLICTING (rebase approval pending Larry). **KEY EVENTS: PR #696 (wire-agent-core-health-notify-001) MERGED 02:49:38Z ✅ — G-rule ourliberty-health-notify COMPLETE. G-rule stale-proposed-mission-pipeline-fp-001 3/3 DISPATCHED → Beacon. All agents IDLE.** 2 alerts (L1095-L1096, both Tier-3). Watermark 1094→1096. PR #692 MERGEABLE/CLEAN, AUTO_MERGE_HELD blocker=#687. PRIME: interventions=1146, systemic_fixes=70, vp=25, ratio≈16.4, trend=improving. Tier 1, consecutive_clean=0.

## Status snapshot — updated 2026-06-25 02:47Z UTC (Iter ~2672, Tier 1, consecutive_clean=0)

**Iter ~2672 summary:** ⚠️ Watch — PR #687 CONFLICTING. **KEY EVENTS: Beacon processed review-duplicate-dispatch-notify-handler-fix-001 (325s, $0.84) → NEW approval `skip-mirror-review-on-merged-or-closed-pr-001`. Mirror ACTIVE reviewing PR #692. PR #696 rev1 waiting.** 3 alerts (L1092-L1094, all Tier-3). Watermark 1091→1094. PRIME: interventions=1145, systemic_fixes=69, vp=24, ratio≈16.6, trend=improving. Tier 1, consecutive_clean=0.

