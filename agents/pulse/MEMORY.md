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

## Dispatch envelope schema (learned 2026-06-11, confirmed 2026-06-14, burned again 2026-06-26)

**Rule:** Beacon inbox dispatch envelopes MUST use root field `task_id` (not `envelope_id`). Required fields: `task_id`, `source`, `dedup_identity`, `prompt`, `timeout`. `timeout` MUST be an integer (seconds), in range [60, 14400] — string durations like `"48h"` are rejected. **86400 (24h) is out of bounds — use 14400 max.** This burned again 2026-06-26 (direction-ask-forge-no-pr-pr-task-id-closed-fp-fix-001 dead-lettered). Hard cap: always use `14400` for long-running tasks.

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

## G-rule watchdog-watcher-log-stale-post-pr694 → COMPLETE ✅ (iter ~2973)

PR #717 (fix: suppress spurious watchdog stale-log WARN during active Mirror reviews) MERGED 2026-06-26T16:51:20Z. Fix verified live. Moving to Completed G-rules.

---

## G-rule review-duplicate-dispatch-wip-redispatch → DISPATCHED ✅ (iter ~2671, 3/3), vp

**Rule:** After Mirror completes a review, Beacon's notification-handler re-dispatches a new `review-<task>.json` to Mirror's inbox without checking if one is already queued. Fix: add inbox-existence check in Beacon notify-handler before dispatching. `skip-mirror-review-on-merged-or-closed-pr-001` approval pending Larry. verification_pending. **Mitigation (iter ~2919):** When duplicate original review (revision_count=0) appears while rev1 is running, archive it via `archive-duplicate-inbox-task` allow-list action. Prevents Mirror from restarting review from scratch after finishing rev1.

---

## triage-alert call discipline — pass ACTUAL alert JSON, never reconstruct (learned iter ~2503)

**Rule:** When calling `alert_triage_state.py triage-alert --alert '<json>'`, always pass the VERBATIM JSON from larry-alerts.jsonl. Never reconstruct with inferred fields. Adding a non-null `subject` field not in the original overrides the `intent` fallback and fails the translation lookup (returns Tier-4 instead of Tier-3).

---

## Completed G-rules — condensed for space (COMPLETE ✅)

`outbox-notifier url-shape-invalid` → PR #493 (2026-06-13). `medic-diagnosis-tier4` → PR #515 (2026-06-15). `heal-pipeline-stall:unrouted-pr` → PR #516 (2026-06-15). `check-i-repeat-dm-fix-001` → PR #674 (2026-06-24). `heal-droplet-git-drift` → PR #586 (2026-06-19). `silence-routine-weekly-alerts` → PR #604 (2026-06-20). `forge-preflight-no-marker` → PR #600 (2026-06-19). `projects-json-healer-path` → PR #603 (2026-06-20). `outbox-notifier-review-pass` → PR #604 scope. `seq-advancer-sequence-stranded` → PR #661 (2026-06-24). `catalog-accuracy-drift` → PR #6 ourliberty-graph (2026-06-22). `doorbell-tier4-pattern` → PR #648 (2026-06-23). `heal-stale-daemon-code-script-service-mismatch` → PR #647 (2026-06-23). `mirror-marker-parse-error` → PR #650 (2026-06-23). `watchdog-watcher-log-stale` → PR #649 (2026-06-23). `watchdog-watcher-log-stale-post-fix` → PR #694 (2026-06-25). `ourliberty-health-notify-script-missing` → PR #696 (2026-06-25). `heal-pipeline-stall-mirror-pass-unmerged-tier4` → PR #695 (2026-06-25). `stale-proposed-mission-pipeline-fp-001` → PR #697 (2026-06-25, sibling_pr_title_shipped suppression). `outbox-notifier-auto-merge-loop-merged-pr-001` → PR #700 (2026-06-25, verified iter ~2713). `forge-built-no-pr-retry1-fp-001` → PR #701 (pattern1) + PR #702 (pattern2, rebase_target_shipped disambiguation, both 2026-06-25, verified iter ~2772). `mirror-marker-severity-blocking-pr711-001` → PR #714 (2026-06-26T06:03:41Z, Mirror REVIEW_PASS + auto-merged). `unrouted-open-pr-active-mirror-session-fp-001` → PR #716 (2026-06-26T14:38:28Z, MIRROR_ACTIVE_SKIP suppression). `forge-built-no-pr-closed-pr-fp-001` → PR #715 (2026-06-26T14:38:35Z, CLOSED-not-merged PR skip in check_forge_built_no_pr). `watchdog-watcher-log-stale-post-pr694` → PR #717 (2026-06-26T16:51:20Z, MIRROR_ACTIVE_SKIP suppression in watchdog stale-log warning path, verified iter ~2973). `medic-dispatcher-delivery-failure-tier4-001` → PR #718 (2026-06-26T16:55Z, Tier-3 translation for medic-dispatcher relay-failure, verified iter ~2974). `beacon-erofs-concurrent-claude-sessions-001` → PR #720 (2026-06-26T19:42:49Z, auto-rebind dangled ~/.claude.json mount, verified iter ~2990). `forge-built-no-pr-pr-task-id-closed-fp-001` → PR #725 (2026-06-26T19:33:58Z, skip forge_built_no_pr for pr-<repo>-<num> tasks with CLOSED/MERGED PR, stall fix verified iter ~2990). `ourliberty-health-sync-push-failed-tier4-001` → PR #728 (2026-06-26T21:50:20Z, Tier-3 silence for ourliberty-health sync_agent_core push-fail alerts, verified iter ~2998). `pulse-source-alert-delivery-confirm-tier4-001` → COMPLETE (iter ~2999; translation already present in alert-translations.json; 3 consecutive Tier-3 returns confirmed). `mirror-malformed-verdict-post-restart-001` → PR #732 (2026-06-27T00:53:24Z, in-process verdict-marker self-validation gate, verified iter ~3012).

---

## G-rule forge-wip-redispatch-digest-tier4-001 → DISPATCHED ✅ (iter ~2797), Beacon fix designed (iter ~2798), Forge dispatch pending

**Rule:** `forge-wip-redispatch` healer fires alerts with `route=digest` (auto-redispatched retry1 notifications). Triage helper classifies Tier-4 (novel, no translation). But these are auto-remediated informational digests — per actionable-only discipline, no DM to Larry. **Beacon result (iter ~2798):** Naive `route=digest` catch-all would also silence critical `route=escalate` exhausted alerts. Two-part fix: (1) healer changes escalate subject `base` → `exhausted:{base}` for distinguishability; (2) `alert-translations.json` gets `forge-wip-redispatch` `"*"` catch-all PLUS `"exhausted": {never_silence: true}`. Forge dispatch pending trust-policy approval from Larry. verification_pending.

---

## G-rule heal-stale-daemon-code-auto-restart-failed-self-recovered → DISPATCHED ✅ (iter ~3000), vp

**Rule:** `source=heal-stale-daemon-code, subject^=auto-restart-failed:*` alerts (route=escalate) fire when heal-stale-daemon-code's `sudo systemctl restart` times out (3s post-restart check window shorter than ~75s startup latency). Systemd self-heals within ~90s in all 3 cases. Triage helper: Tier-4 novel (no translation match). Occurrences: iter ~2704 (1/3); iter ~2947 (2/3, ourliberty-outbox-notifier.service, PR #713 mass-restart); iter ~3000 (3/3, ourliberty-outbox-notifier.service, PR #730 mass-restart). Dispatched `direction-ask-auto-restart-failed-tier3-translation-001` to Beacon inbox at iter ~3000. Fix: add source=heal-stale-daemon-code subject^=auto-restart-failed: Tier-3 translation entry. Do NOT silence subject^=still-stale-after-restart: (different, genuine ask-then-do). verification_pending.

---

## G-rule forge-wip-redispatch-exhausted-pr-exists-fp-001 → 3/3 HOLD-DEFERRED (updated iter ~3124)

**Rule:** `source=forge-wip-redispatch, route=escalate` exhaustion alerts ("WIP-only auto-recovery EXHAUSTED") fire for tasks whose original PRs already exist. FP class: wip-redispatch retried a task whose output already shipped; retry dying WIP-only is expected. Fix: extend wip-redispatch to check PR existence before declaring exhaustion, OR add `source=forge-wip-redispatch, route=escalate, <pr_exists signal>` → Tier-3 entry. **3/3 reached iter ~3124 (L1110, land-pr731-restore-fix-head-001, PR #731 MERGED; outbox-notifier logged BUILD_ALREADY_MERGED + reconciled sequence step). Dispatch to Beacon deferred under HOLD.** Occurrences: iter ~2702 (L1130/1131); iter ~2705 (L1146); iter ~3124 (L1110).

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

## G-rule unrouted-open-pr-active-mirror-session-fp-001 → COMPLETE ✅ (iter ~2955)

PR #716 (fix(heal-stall): suppress unrouted_open_pr alert while Mirror is actively reviewing) MERGED 2026-06-26T14:38:28Z. Fix verified live. Moving to Completed G-rules.

---

## G-rule ourliberty-health-sync-push-failed-tier4-001 → COMPLETE ✅ (iter ~2998)

PR #728 (chore(alerts): Tier-3 silence ourliberty-health-sync-push-failed duplicate) MERGED 2026-06-26T21:50:20Z as d1c8dce9. Fix verified live. Moving to Completed G-rules.

---

---

## G-rule forge-revision-preamble-missing-pr711-001 → DISPATCHED ✅ (iter ~2992), vp

**Rule:** outbox-notifier fires `forge revision-phase outbox without "Revision N applied:" preamble: <task>.json; treating as marker-error` when Forge submits a revision outbox file lacking the expected "Revision N applied:" preamble. Treated as marker-error; retry fires; review proceeds and PR still merges. Fix: Forge build-sequence discipline or outbox-notifier tolerance. Dispatched `forge-revision-preamble-missing-direction-ask-001.json` to Beacon inbox at iter ~2992 (3/3). Occurrences: iter ~2851 (PR #711, 1/3); iter ~2990 (PR #720 rev2, 2/3); iter ~2992 (PR #726, 3/3 × 2 retries at 14:26:10 + 14:26:31 MDT). verification_pending.

---

## G-rule no-session-revision-active-mirror-session-fp-001 → DISPATCHED ✅ (iter ~2906), vp

**Rule:** `heal_pipeline_stall.py` fires `no_session_revision:<task_id>` when cooldown expires even though Mirror IS actively reviewing. Three occurrences: iter ~2885 (Mirror 26-min in), iter ~2905 (Mirror 2h31m+), iter ~2906 (L1062, Mirror 2h39m+ live healer fired). `heal-stall-mirror-active-suppression-001` (pending approval) scoped this OUT; dispatched `direction-ask-no-session-revision-active-mirror-fix-001.json` to Beacon at iter ~2906. Fix: add `NO_SESSION_REVISION_MIRROR_ACTIVE_SKIP` suppression in `check_revision_dispatched_with_no_session` using same `_mirror_session_active_for_pr` helper. verification_pending.
**Alert triage note (corrected iter ~2935):** translations.json HAS a `pipeline-stall:no-session-revision` entry (WARNING/SOON tier) — Pulse's triage ALREADY correctly Tier-3 silences no-session-revision alerts. Prior MEMORY "NO entry" claim (iter ~2933) was wrong — that search used underscore ("no_session") instead of hyphen ("no-session"). This G-rule is about preventing the stall CHECKER from firing FPs when Mirror is active, not about alert triage.

---

## G-rule sentinel-inflight-stall-mirror-tier4 — 2/3 (updated iter ~2964)

**Rule:** `source=sentinel, subject^=in-flight-stall:` alerts classify Tier-4 (novel, no translation match). Fired when Mirror session exceeds 60-min in-flight threshold. Sentinel message says heal_wedged_review_sessions auto-recovers; kill PID unblocks sooner. outbox-notifier delivers route=escalate DM to Larry; Pulse suppresses duplicate DM (journal-note only). Fix: add `source=sentinel, subject^=in-flight-stall:` → Tier-3 (if healer reliably self-recovers) OR Tier-2 (kill-PID action needed). Dispatch to Beacon at 3/3. Occurrences: iter ~2892 (1/3); iter ~2964 (2/3, Mirror PR #717 watchdog-mirror-active-stale-suppression-001.json, 1.13h in-flight).

---

## G-rule forge-built-no-pr-closed-pr-fp-001 → COMPLETE ✅ (iter ~2955)

PR #715 (fix(healer): skip forge_built_no_pr stall when task PR is CLOSED) MERGED 2026-06-26T14:38:35Z. Fix verified live. Moving to Completed G-rules.

---

## G-rule pulse-source-alert-delivery-confirm-tier4-001 → COMPLETE ✅ (iter ~2999)

Triage helper returned Tier-3 for source=pulse alerts on 3 consecutive iters (~2997, ~2998, ~2999). Translation already present in alert-translations.json. Moving to Completed G-rules.

---

## G-rule beacon-erofs-concurrent-claude-sessions-001 → COMPLETE ✅ (iter ~2990)

PR #720 (heal: auto-rebind dangled ~/.claude.json mount) MERGED 2026-06-26T19:42:49Z. Fix verified: worktrees torn down cleanly by AUTO_MERGE_WORKTREE_TEARDOWN. Moving to Completed G-rules.

---

## G-rule forge-built-no-pr-pr-task-id-closed-fp-001 → COMPLETE ✅ (iter ~2990)

PR #725 (fix(healer): skip forge_built_no_pr for pr-<repo>-<num> tasks whose named PR is closed/merged) MERGED 2026-06-26T19:33:58Z. Fix verified: stall dry-run shows `pr-ourliberty-agent-core-712` now FORGE_NO_PR_SKIP reason=pr_task_id_closed_or_merged. "no stalls detected." Moving to Completed G-rules.

---

## G-rule mirror-runner-missing-worktree-retry-001 — 1/3 (new, iter ~2980)

**Rule:** Mirror runner tears down the worktree after a SIGTERM kill (exit 143) of the claude subprocess. Retry attempts (2-5) then fail immediately with `Exception: [Errno 2] No such file or directory: wt-mirror-pr-<task>`. Mirror review never completes; outbox empty; Beacon doesn't auto-re-dispatch. Root cause: runner's retry logic doesn't recreate the worktree when the prior attempt was killed. Fix: Mirror runner should either (a) recreate the worktree at retry start if missing, or (b) detect missing worktree and request a fresh review dispatch from outbox-notifier. First occurrence iter ~2980 (PR #720, review killed ~33 min in, worktree wt-mirror-pr-ourliberty-agent-core-720). Dispatch to Beacon at 3/3.

---

## G-rule mirror-malformed-verdict-post-restart-001 → COMPLETE ✅ (iter ~3012)

PR #732 (fix(mirror): in-process verdict-marker self-validation gate to kill restart FP round-trip) MERGED 2026-06-27T00:53:24Z as 80bc08b0. Fix: in-process self-validation gate in Mirror runner prevents malformed markers from reaching the outbox. Mirror REVIEW_PASS on revision-1 confirmed gate works. Moving to Completed G-rules.

---

## G-rule ourliberty-health-clean-tree-dirty-tier4-001 → DISPATCHED ✅ (iter ~3012), vp

**Rule:** `source=ourliberty-health` alerts with dirty-tree subjects classify Tier-4 (novel, no translation match). Two variants: (1) `subject=sync_agent_core: uncommitted changes block sync`, (2) `subject^=ourliberty-agent-core health: N issue(s) need attention` with clean_tree issue. Dispatched `direction-ask-ourliberty-health-dirty-tree-tier3-001.json` to Beacon inbox at iter ~3012 (3/3). Fix: add Tier-3 translations for both variants to config/alert-translations.json. Occurrences: iter ~3005 (1/3), iter ~3011 line 1082 (2/3), iter ~3012 line 1084 (3/3). verification_pending.

---

## G-rule decision-needed-approval-forge-dispatch-no-target-repo-001 — 3/3 HOLD-DEFERRED (updated iter ~3048)

**Rule:** When outbox-notifier emits an `approval_request` via the "no-session decision-needed" (REVIEW_ESCALATE) path and Larry approves via Telegram, Beacon dispatches a task to Forge inbox — but the envelope has `target_repo=None`. Forge inbox_watcher dead-letters with: `worktree: no canonical path for target_repo=None`. Additionally: the DM drops earlier — `beacon replan APPROVAL_REQUEST cannot route approval DM (reply_chat_id=None), falling through` — so Larry never gets to approve. Both failures stem from the no-session REVIEW_ESCALATE path losing task metadata. Fix: Beacon's REVIEW_ESCALATE replan dispatch path must carry `target_repo` AND `reply_chat_id` from original task metadata. Occurrences: iter ~3006 (1/3, PR #731); iter ~3043 (2/3, PR #733, 04:43:26Z); iter ~3048 (3/3, PR #733, 05:14:28Z). Dispatch to Beacon deferred under HOLD until strategy decided. **Alternate unblock path:** direction-ask-pr731-nextcount-guard-fix-001 dispatched iter ~3011 — Beacon/Forge apply medic's concrete fix directly to PR #731 branch, bypassing the broken approval flow.

---

## G-rule sync-service-deploy-restart-storm-tier4-001 — 1/3 (new, iter ~3044)

**Rule:** `source=sync.service, subject=deploy-restart-storm` alerts classify Tier-4 (novel, no translation match). Fire when a PR deploy causes a widely-imported module change and sync.service restarts all 6 daemons. Alert's own `route=digest`; beacon bot correctly silences (no DM). All daemons restart cleanly. Should be Tier-3. Dispatch to Beacon at 3/3 (post-HOLD). First occurrence iter ~3044 (PR #734 deploy, 04:47:02Z).

---

## G-rule medic-diagnosis-tier4 → STALE/CLOSED ✅ (re-verified iter ~3084)

**Rule (historical):** Was re-opened at iter ~3068 with claim "0 medic entries in alert-translations.json." Re-verified iter ~3084: `config/alert-translations.json` HAS `medic.medic-diagnosis` entry (severity=INFO, tier=FYI). Triage helper returned Tier-3 for attempt-10 medic-diagnosis alert. Translation IS present and working. G-rule re-open claim was STALE. Closing. No dispatch needed.

---

## G-rule check-iii-invoke-gap-sunday-001 — 1/3 (new, iter ~3125)

**Rule:** Check III missed its Sunday 2026-06-22 gate. heal-pulse-check-staleness fired Tier-4 escalation (387.9h since last run, past 336h+48h=384h grace). Root cause: the Pulse /cycle apparently didn't invoke `pulse_check_iii.py` on Sunday 2026-06-22. Pulse ran Check III off-schedule (Saturday 2026-06-27) to clear staleness. Fix: ensure Sunday cycles reliably invoke Check III when conditions hold; may need explicit gate logging or a staleness pre-check in the cycle invoker. Dispatch to Beacon at 3/3 (post-HOLD). First occurrence iter ~3125. Cannot dispatch under HOLD.

---

## G-rule watchdog-log-growth-idle-overnight-001 — 1/3 (new, iter ~3148)

**Rule:** Watchdog `log_growth: idle >12h` fires when outbox-notifier.log is quiet overnight with no pipeline activity. Root: outbox-notifier IS running but writes nothing when the pipeline is genuinely idle. Prior fixes (PR #649, PR #694, PR #717) addressed stale-log during Mirror reviews; pure overnight-idle path is not suppressed. Per WARN-vs-INFO calibration this is an idle-state INFO observation — system not worse off. Dispatch to Beacon at 3/3 (post-HOLD) to add Tier-3 translation or raise watchdog log_growth threshold for extended idle state. First occurrence iter ~3148 (seconds_since_write=43316, outbox-notifier last wrote 17:01:33 UTC 2026-06-27).

---

## Status snapshot — updated 2026-06-28T13:32Z UTC (Iter ~3164, Tier 3, consecutive_clean=32)

**Iter ~3164 summary:** ✅ Nominal (iter_clean). 1 new alert (doorbell L1062, Tier-3 silence, watermark=1062). consecutive_clean=31→32. Check 1: watchdog overall=warning (log_growth idle, seconds_since_write=74266 ~20.6h, same continuous overnight-idle window from iters ~3148+, INFO-level, G-rule 1/3). Check I: same-week dedup skip (artifact from iter ~3141). Check III next due 2026-07-11. PRIME: 0 interventions, ratio≈17.44, trend=improving. HOLD in effect. Larry /pause active. Daemons alive (PID 3961026/3961281/17832). All 4 bots alive (beacon/forge/mirror/pulse). Repo 8f35efa8, clean. Sync 13:24:15Z. Heal-daemon 13:30:54Z. 5 beacon-pending (unchanged). 0 open PRs. Awaiting `approve threshold-update-2026-06-27`. Cadence 30-min (Tier 3).


