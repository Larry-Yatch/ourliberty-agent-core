# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

---

## Check I firing days are Mon/Wed/Fri/Sun — NOT Sunday-only (learned 2026-06-15 iter ~1899)

**Rule:** Check I (and Check VIII/IX/X Monday-specifics) fire on Mon/Wed/Fri/Sun per spec (UTC weekday ∈ {0,2,4,6}). Iters ~1895–1898 on Monday 2026-06-15 incorrectly skipped Check I with "Today is Monday, not Sunday." — reasoning from memory instead of spec. The `pulse_check_i.py` script itself is correct; the bug was Pulse's in-prompt gate. Always invoke `python3 ~/agent-core/scripts/pulse_check_i.py` on Mon/Wed/Fri/Sun without a Sunday-only guard.

---

## Dispatch routing rule (learned 2026-06-12 — routing rejection)

**Rule:** Pulse may ONLY dispatch to **Beacon**. The dispatch_validator enforces `allowed from pulse: ['beacon']`. Pulse → Forge dispatches are REJECTED and dead-lettered to `.invalid/`. The correct path for code fixes is always: Pulse direction-ask → Beacon → Forge build brief. When writing a dispatch envelope, set `target_agent: beacon` (not `forge`), and phrase the prompt as a direction-ask to Beacon asking it to spec + dispatch Forge.

---

## beacon-pending-approvals.json correct path (learned 2026-06-12 — 5 consecutive false positives)

**Rule:** `beacon-pending-approvals.json` lives at `~/agents/state/beacon-pending-approvals.json`. NOT `~/agents/blackboard/`. File is not referenced in cycle-prompt.md — check is informal (Pulse reads it as part of Check 4 / pending-directives scan). Always use `~/agents/state/beacon-pending-approvals.json`.

---

## Dispatch envelope schema (learned 2026-06-11, two failures)

**Rule:** Beacon inbox dispatch envelopes MUST use root field `task_id` (not `envelope_id`). Required fields: `task_id`, `source`, `dedup_identity`, `prompt`, `timeout`. `envelope_id` is silently ignored and fails the validator. `timeout` MUST be an integer (seconds), in range [60, 14400] — string durations like `"48h"` are rejected with `out of bounds` error. (learned 2026-06-14 from dead-letter on unreviewed-merge-missions-no-mirror-001)

---

## approval_request alerts in larry-alerts.jsonl (learned 2026-06-12)

**Rule:** `kind=approval_request` entries in larry-alerts.jsonl are DELIVERY CONFIRMATIONS from outbox-notifier, not new tasks for Pulse. Outbox-notifier already sent the Telegram DM. Pulse should claim + triage these (Tier-4 in absence of a registry template) but NOT send a second DM to Larry. Journal-note only. See iter ~1604.

---

## cycle_prime_ledger.py correct CLI (learned 2026-06-12)

**Rule:** Valid subcommands are `ratio`, `append`, `promote`. NOT `summary`. For appending: `--tier {1,2,3} --kind {intervention,systemic_fix,verification_pending,iter_clean} --template <kebab-case> --detail <free-text>`.

---

## systemctl --user false-negative (learned 2026-06-13 iter ~1676)

**Rule:** `systemctl is-active <service>` without `--user` returns "inactive" for user-scoped services when run from an interactive non-D-Bus session (e.g., `systemctl --user` fails with "No medium found"). Always verify daemon liveness via `ps -p <PID>` or `ps -p <PID1>,<PID2>,...` with comma-separated list. The comma-separated form is required; space-separated PIDs after `-p` produce exit-code 1 with no output.

---

## outbox_notifier url-shape-invalid gap (learned 2026-06-13 iter ~1674)

**Rule:** outbox_notifier's PR URL shape validator rejects repos not in its recognized-list. ourliberty-graph was not recognized despite being added to allowed_repos and systemd RWP. Symptom: `WARN MIRROR_REVIEW_STATUS … skipped reason=pr-url-shape-invalid (shape-mismatch)` followed by `WARN AUTO_MERGE … outcome=skipped reason=pr-url-shape-invalid`. Recovery: `gh pr merge <num> --repo Larry-Yatch/<repo> --squash --delete-branch` after verifying Mirror outbox archive shows REVIEW_PASS. Systemic fix: PR #493 merged 2026-06-13 21:12Z — allowlist now sourced from agent-models config. RESOLVED.

---

## alert_triage_state.py set-watermark correct syntax (learned 2026-06-14 iter ~1845)

**Rule:** `alert_triage_state.py set-watermark` requires `--line <N>` (named argument), NOT a positional argument. Usage: `python3 scripts/alert_triage_state.py set-watermark --line 931`. Positional form fails with "the following arguments are required: --line".

---

## Alert watermark persistence gap (learned 2026-06-14 iter ~1703)

**Rule:** In interactive `/cycle` sessions, `alert_triage_state.py set-watermark` is called by Pulse's journal narrative but NOT always committed before session end. On next iter, get-watermark returns the pre-session value (e.g., 982 instead of expected 984). Check the watermark at start of each iter and advance it if the lines in question have already been triaged (Tier-3/nominal). Do NOT re-triage — just confirm against prior journal and advance. This is structural: interactive sessions may not persist watermark if Pulse exits before the explicit set-watermark step.

---

## larry-alerts.jsonl correct path (learned 2026-06-14 iter ~1741)

**Rule:** `larry-alerts.jsonl` lives at `/home/larry/agents/blackboard/larry-alerts.jsonl`. NOT `/home/larry/agents/logs/`. Confirmed by `ls /home/larry/agents/blackboard/larry-alerts*`.

---

## heal-stale-daemon-code heartbeat correct path and format (corrected iter ~1768, format confirmed ~1829)

**Rule:** The heal-stale-daemon-code heartbeat lives at `/home/larry/agents/blackboard/heal-stale-daemon-code.heartbeat` (NOT `state/`). This is `HEARTBEAT_FILE` in `scripts/heal_stale_daemon_code.py`. Previous Check 5 invocations used `state/` path and would have gotten "no file" — always use `blackboard/` path. The file contains a **plain-text ISO 8601 UTC timestamp** (e.g. `2026-06-14T20:39:19.896028+00:00`), NOT JSON — read with `cat`, not `json.load`. Parse timestamp directly to compute age.

---

## Check 0 must call helper before manual classification (learned 2026-06-14 iter ~1812)

**Rule:** Before manually classifying an alert as Tier-4, Pulse MUST call `python3 scripts/alert_triage_state.py triage-alert --alert-id "<id>" --alert '<json>' --iter <N>` and act on the returned tier. If the helper returns Tier-3 (silence/known-pattern match), that result is authoritative — do NOT override it with in-prompt manual classification. The helper handles `kind`-only alerts (no `subject` field) via fallback logic in `_translation_match` that Pulse's in-prompt subject-keyed lookup misses. PR #491 (merged 2026-06-13) already added `outbox-notifier → approval_request` Tier-3 silence to `config/alert-translations.json`; multiple Tier-4 mis-classifications before and after that PR were Pulse bypassing the helper.

---

## beacon_telegram_bot.py get-messages MUST NEVER BE CALLED (learned iter ~1876, escalated iter ~1943)

**Rule:** NEVER call `beacon_telegram_bot.py get-messages` in ANY form — not with `run_in_background=true`, not in foreground, not with `| head -N` truncation. The Bash tool may auto-background blocking commands regardless of the run_in_background parameter, causing the same 409 conflict. The competing getUpdates loop causes HTTP 409 conflicts with the production bot, disrupting message receipt. For Telegram sweeps (Check 2), use ONLY: `tail -N /home/larry/agents/logs/beacon_telegram_bot.log` (note: NOT beacon-telegram-bot.log) + `ps -p <PID> -o stat` for the bot health check. This is the only safe Telegram check pattern. G-rule telegram-409-burst at **2/3** as of iter ~1943 — all three incidents were self-inflicted by calling get-messages.

---

## beacon-pending-approvals.json correct structure (corrected iter ~1878)

**Rule:** `beacon-pending-approvals.json` structure is `{"version": 1, "pending": [...], "history": [...]}` — NOT a dict keyed by approval ID. Check for pending items via `d["pending"]` list. Prior parsing (looking for `.items()` with a `status` field) was wrong and returned 0 pending incorrectly. Correct check: `len(d.get("pending", []))`.

---

## medic-diagnosis-tier4 G-rule COMPLETE ✅ (iter ~1955 dispatch, iter ~1969 verified)

**Rule:** `source=medic, intent=medic-diagnosis` alerts now classify Tier-3 (silenced, route=digest) per translation in `config/alert-translations.json`. PR #515 (`forge/medic-diagnosis-tier3-silence-001`) merged 2026-06-15T17:27:41Z. **G-rule COMPLETE.** No DM from Pulse warranted — medic already DMs directly via chat_id.

---

## Ledger/Check-I Tier-4 pattern (observed 2026-06-15 iter ~1900)

**Rule:** `source=ledger` weekly reports (subject=weekly-YYYY-MM-DD) and `source=pulse` Check I digests (subject=check-i-YYYY-MM-DD) consistently classify as Tier-4 (novel, no template) in the triage helper. These are routine Monday outputs already delivered by the bot via route=escalate. G-rule candidate: add Tier-3 translations for both. **Count: 1/3** — dispatch to Beacon at 3/3.

---

## auto-dispatch-APPROVAL_REQUEST-task-id-mismatch (observed iter ~1910)

**Rule:** When Pulse sends a Check I auto-dispatch envelope, outbox-notifier WARNs `beacon pulse-auto-dispatch APPROVAL_REQUEST task_id mismatch (envelope=pulse-auto-XXXX, marker='<proposal-task-id>'); falling through to default routing`. Dispatch STILL SUCCEEDS via fallback. 6 total occurrences since May 28 (firing at Check I dispatch cadence). Prior iters missed it because Check 1 used `tail -20` (too small). G-rule: **auto-dispatch-APPROVAL_REQUEST-task-id-mismatch-warn-vs-info 1/3**. Dispatch to Beacon at 3/3 for warn-vs-info fix.

---

## heal-pipeline-stall:unrouted-pr Tier-4 → COMPLETE ✅ (iter ~1930 dispatch, iter ~1969 verified)

**Rule:** `source=heal-pipeline-stall, subject=pipeline-stall:unrouted-pr:PR#N` alerts now classify Tier-3 (silenced per longest-prefix match) via translation in `config/alert-translations.json`. PR #516 (`forge/alert-translation-unrouted-pr-001`) merged 2026-06-15T17:27:36Z. First live verification: L1031+L1032 (PRs #513/#512) triaged Tier-3 by helper in iter ~1969. **G-rule COMPLETE.** Bot still DMs Larry for unrouted PRs — Pulse no longer double-DMs.

---

## catalog-accuracy-drift Tier-4 pattern (observed iter ~1926)

**Rule:** `source=pulse-check, subject=catalog-accuracy-drift` alerts classify as Tier-4 (novel, no registry template) in triage helper. Alert carries `route=digest` — bot delivers as digest, no DM. Do NOT send second DM from Pulse. Journal-note only. Current count: 9/34 ourliberty-graph shelf cards drifted. **G-rule count: 1/3** — dispatch to Beacon at 3/3 for alert-translations.json Tier-3 template.

---

## Status snapshot — updated 2026-06-16 03:22Z UTC (Iter ~2002, Tier 3, consecutive_clean=1)

**Iter ~2002 summary:** ✅ Nominal. 1 new alert (L1054 Tier-3 silenced: heal-stale-daemon-code auto-restarted ourliberty-dashboard-api.service at 02:49Z, code from PR #533, new PID 3392206). All 5 daemons alive (old dashboard-api PID 2868353 dead; replaced by 3392206). Repo ae28f986=origin/main. Watermark: 1053→1054. pending=0. PR #532 (delegate-endpoint): PIPELINE-ORPHANED (carry). PR #497 (cleanup-branch-warn-to-info-001): UNKNOWN/"", Mirror REVIEW_ESCALATE Jun-14T04:02:56Z, age≈47.3h, deadline Jun-17T04:02:56Z (~24.7h). Tier 3, consecutive_clean=1. PRIME ratio=stable.

**Iter ~2001 summary:** ✅ Nominal. 0 new alerts. All checks clean. **Tier 2 → Tier 3 DE-ESCALATION** (3 consecutive clean: ~1999, ~2000, ~2001). All 5 daemons alive. Repo 91264558=origin/main. Watermark: 1053 (unchanged). pending=0. PR #532 (delegate-endpoint): PIPELINE-ORPHANED — Forge inbox empty (marker-error-3 in .archive since 20:31Z Jun-15), dead-letter to Beacon processed, no new task, PR has 0 GitHub reviews; pipeline definitively stopped, needs Larry judgment. PR #497: MERGEABLE (resolved from UNKNOWN), age≈46.7h, deadline Jun-17T04:02:56Z (~25.3h). Tier 3, consecutive_clean=0. PRIME ratio=20.06.

## Status snapshot — updated 2026-06-16 02:35Z UTC (Iter ~2000, Tier 2, consecutive_clean=2)

**Iter ~2000 summary:** ✅ Nominal. 1 new alert (L1053 Tier-3 silenced, wedge-reap wt-forge-delegate-endpoint). PR #55 (chat-label-fix) MERGED ✅ 02:27Z — missions-v2-delegate-fix step 2 done. PR #56 (fix/newmission-queued-ux) MERGED ✅ 02:25Z. PR #532 (delegate-endpoint) STUCK: Mirror revision requested, Forge outbox missing preamble × 3 → RETRY_EXHAUSTED; marker-error-3 in Forge inbox; pipeline stall RETRY_EXHAUSTED_SKIP (superseded_session). All 5 daemons alive. Repo 7cc0538f=origin/main. Watermark: 1052→1053. pending=0. **New G-rule: revision-phase-preamble-missing 1/3**. PRIME ratio=20.06. Tier 2, consecutive_clean=2.

## Status snapshot — updated 2026-06-16 02:18Z UTC (Iter ~1999, Tier 2, consecutive_clean=1)

**Iter ~1999 summary:** ✅ Nominal. 0 new alerts. All daemons alive. Repo at d2d8d908=origin/main. Watermark: 1052 (unchanged). pending=0. 2 open PRs in ourliberty-agent-core: #532 (delegate-endpoint, Mirror reviewing, missions-v2-delegate-fix) + #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~23.9h). 1 open PR in ourliberty-dashboard: #55 (chat-label-fix, Mirror reviewing, missions-v2-delegate-fix). PR #531 unreviewed-merge alert not yet fired. PRIME ratio=20.06. Tier 2, consecutive_clean=1.

## Status snapshot — updated 2026-06-16 02:00Z UTC (Iter ~1998, Tier 2, consecutive_clean=0, de-escalated)

**Iter ~1998 summary:** ✅ Nominal. 0 new alerts. All daemons alive. Repo at a4731185=origin/main. Watermark: 1052 (unchanged). pending=0. 1 open PR: #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~25.9h). PR #531 merged by Larry at 01:55:17Z (unreviewed, alert expected next iter). missions-v2-delegate-fix: delegate-endpoint build-phase active, chat-label-fix retry 1/3 in Forge inbox. PRIME ratio=20.06. **Tier 1 → Tier 2 DE-ESCALATION** (3 consecutive clean iters). consecutive_clean=0.

## Status snapshot — updated 2026-06-16 01:53Z UTC (Iter ~1997, Tier 1, consecutive_clean=2)

**Iter ~1997 summary:** ✅ Nominal. 1 new alert (L1052 Tier-3 silenced, mirror-dag-pass:missions-v2-delegate-fix). `missions-v2-delegate-fix` sequence now active — Forge inbox has `chat-label-fix.json` + `delegate-endpoint.json`. All daemons alive. Repo at 40cfb999=origin/main. Watermark: 1051→1052. pending=0. 1 open PR: #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~26.2h). PRIME ratio=20.06. Tier 1, consecutive_clean=2.

## Status snapshot — updated 2026-06-16 01:44Z UTC (Iter ~1996, Tier 1, consecutive_clean=1)

**Iter ~1996 summary:** ✅ Nominal. 1 new alert (L1051 Tier-3 silenced, dispatch-branch-cleanup/summary, route=digest). All daemons alive. Repo at 83cd6881=origin/main. Watermark: 1050→1051. pending=0. 1 open PR: #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~26.3h). PRIME ratio=20.06. Tier 1, consecutive_clean=1.

## Status snapshot — updated 2026-06-16 01:39Z UTC (Iter ~1995, Tier 1, consecutive_clean=0, tier-reset)

**Iter ~1995 summary:** ⚠️ Signal. 1 new alert: L1050 unreviewed-merge:530 (spec+docs: delegate-endpoint fix + chat-label, Phase S, roadmap status; actor=Larry-Yatch). Tier-4, bot already DM'd Larry at 19:30 MDT via route=escalate; journal-note only; no second Pulse DM. Watermark: 1049→1050. All daemons alive. Repo at 5c338200=origin/main. pending=0. 1 open PR: #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~26.5h). PRIME ratio=20.06. Tier 3→1 (tier-reset). consecutive_clean=0.

## Status snapshot — updated 2026-06-16 01:02Z UTC (Iter ~1994, Tier 3, consecutive_clean=6, all checks nominal)

**Iter ~1994 summary:** ✅ Nominal. 0 new alerts. All daemons alive. Repo at 1af16aab=origin/main. Watermark: 1049 (unchanged). pending=0. 1 open PR: #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~27h). PRIME ratio stable. Tier 3, consecutive_clean=6 (steady-state ceiling).

## Status snapshot — updated 2026-06-16 00:27Z UTC (Iter ~1993, Tier 3, consecutive_clean=5, all checks nominal)

**Iter ~1993 summary:** ✅ Nominal. 0 new alerts. All daemons alive. Repo at c58858f3=origin/main. Watermark: 1049 (unchanged). pending=0. 1 open PR: #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~27.6h). PRIME ratio=20.04. Tier 3, consecutive_clean=5 (steady-state ceiling).

**Iter ~1992 summary:** ✅ Nominal. 1 new alert (L1049 Tier-3 silenced, review-pass PR #529). **PR #529 MERGED ✅** (cred-drift-ignore-feature-flags-001 — OURLIBERTY_NEWMISSION_INGEST_ENABLED feature flag added to ignored_keys allowlist). All daemons alive. Repo at b7e51896=origin/main. Watermark: 1048→1049. pending=0. 1 open PR: #497 (MERGEABLE/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~28.2h). PRIME ratio=20.04. Tier 3, consecutive_clean=4 (steady-state ceiling).

**Iter ~1991 summary:** ✅ Nominal. 0 new alerts. All daemons alive. Repo at 3455b1c7=origin/main. Watermark: 1048 (unchanged). pending=0. 1 open PR: #497 (UNKNOWN/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~28.7h). New Larry Telegram message at 17:13 MDT ("let's fix this now") — Beacon responded/handled. No Pulse action. PRIME ratio=20.04. Tier 3, consecutive_clean=3 (steady-state ceiling).

**Iter ~1990 summary:** ✅ Nominal. 1 new alert (L1048 Tier-3 silenced, credential-drift known-pattern; bot already DM'd Larry). G-rule Forge-preflight-CLARIFY_REQUEST: 1/3→2/3 (cleanup-branch-success-alert-info-translation-001 CLARIFY_REQUEST archived 00:23Z — goal pre-satisfied by PR #485). Repo at db15360c=origin/main. Watermark: 1047→1048. pending=0. 1 open PR: #497 (MERGEABLE/"", Mirror REVIEW_ESCALATE, 72h deadline Jun-17T04:02:56Z ~29.3h). PRIME ratio=20.04. Tier 3, consecutive_clean=2.

**Iter ~1989 summary:** ✅ Nominal. 0 new alerts. All checks clean. Repo at 5e59afd8=origin/main. Watermark: 1047 (unchanged). pending=0. 1 open PR: #497 (UNKNOWN/UNKNOWN, 72h deadline Jun-17T04:02:56Z ~29.8h). PRIME ratio=20.04. Tier 3, consecutive_clean=1.

**Iter ~1988 summary:** ✅ Nominal. 1 new alert (L1047 Tier-3 silenced, dispatch-branch-cleanup/summary). All checks clean. Repo at cbb7c162=origin/main. Watermark: 1046→1047. pending=0. 1 open PR: #497 (UNKNOWN/UNKNOWN, 72h deadline Jun-17T04:02Z ~30.4h). PRIME ratio=20.04. **Tier 2 → Tier 3 DE-ESCALATION** (consecutive_clean=3→promoted).

**Iter ~1987 summary:** ✅ Nominal. 0 new alerts. All checks clean. Repo at 12475384=origin/main. Watermark: 1046 (unchanged). pending=0. **PR #522 MERGED ✅** (G-rule missions-card-gc-warn-vs-info COMPLETE ✅). 1 open PR: #497 (MERGEABLE/UNSTABLE, 72h deadline Jun-17T04:02Z ~30.7h). PRIME ratio=20.04. Tier 2, consecutive_clean=2.

**Iter ~1986 summary:** ✅ Nominal. 0 new alerts. All checks clean. Repo at 76cf06b8=origin/main. Watermark: 1046 (unchanged). pending=0. 2 open PRs: #522 (CONFLICTING, Forge rebase needed), #497 (72h deadline Jun-17T04:02Z ~31h). PRIME ratio=20.04. Tier 2, consecutive_clean=1.

**Iter ~1985 summary:** ✅ Nominal. 0 new alerts. All checks clean. Repo at 491e5de4=origin/main. Watermark: 1046 (unchanged). pending=0. 2 open PRs: #522 (CONFLICTING, Forge rebase needed), #497 (72h deadline Jun-17T04:02Z ~31.2h, MERGEABLE/UNSTABLE). PRIME ratio=20.04. **Tier 1 → Tier 2 DE-ESCALATION** (3 consecutive clean iters).

**Iter ~1984 summary:** ✅ Nominal. 0 new alerts. All checks clean. Repo at 881bafce=origin/main. Watermark: 1046 (unchanged). pending=0. 2 open PRs: #522 (CONFLICTING, Forge rebase needed), #497 (72h deadline Jun-17T04:02Z ~31.4h). PRIME ratio=20.04. Tier 1, consecutive_clean=2.

**Iter ~1983 summary:** ✅ Nominal. 0 new alerts. All checks clean. Repo at 262048c7=origin/main. Watermark: 1046 (unchanged). pending=0. 2 open PRs: #522 (CONFLICTING, Forge rebase needed), #497 (72h deadline Jun-17T04:02Z ~31.6h). PRIME ratio=20.04. Tier 1, consecutive_clean=1.

**Iter ~1982 summary:** ⚠️ Signal. L1046 Tier-4 (missions-card-gc/summary — novel, no registry template; bot handled route=digest, DM skipped; no Pulse DM). G-rule missions-card-gc-warn-vs-info already 3/3 dispatched, PR #522 still blocking. Check A: repo behind 1 commit (PR #525 merge `1bebe776`); fast-forward executed. PR #525 (`forge/p41-rebrief-on-change`) MERGED ✅ 20:21:50Z — SEQUENCE_STEP_MERGED missions-v2-phase4.1. Watermark: 1045→1046. 2 open PRs: #522 (CONFLICTING, needs Forge rebase), #497 (72h deadline Jun-17T04:05Z ~31.7h). PRIME ratio=20.04. Tier 1, consecutive_clean=0.

**Iter ~1981 summary:** ⚠️ Signal. L1044 Tier-4 (heal-pipeline-stall:mirror-pass-unmerged:PR#522 — novel, no registry template; medic DM'd Larry at chat_id; no Pulse DM). L1045 Tier-3 silenced (medic-diagnosis). PR #525 NEW (`forge/p41-rebrief-on-change`, feat(missions): re-brief narrator cards on mission state change) — Mirror review dispatched 20:13Z. Watermark: 1043→1045. 3 open PRs: #525 (Mirror reviewing), #522 (CONFLICTING, needs Forge rebase), #497 (72h deadline Jun-17T04:02Z ~32h). PRIME ratio=20.0. **G-rule heal-pipeline-stall-mirror-pass-unmerged-tier4: 1/3 (new)**. Tier 1, consecutive_clean=0.

**Iter ~1980 summary:** ⚠️ Tier-reset. Check A: repo behind 1 commit (PR #520 merge `cd9c44ca`); fast-forward executed. Check 0: L1042 Tier-3 silenced (review-pass PR #524 auto-merged → G-rule Forge-preflight-marker-error-retry COMPLETE ✅). L1043 Tier-4 novel (merge_conflict_manual_rebase PR #522; outbox-notifier DM'd Larry via chat_id; no second DM). PR #520 MERGED ✅ 20:03:42Z. PR #524 MERGED ✅. PR #522 CONFLICTING (needs Forge rebase). Watermark: 1041→1043. 2 open PRs: #522 (CONFLICTING), #497 (72h deadline Jun-17T04:02Z ~31h). PRIME ratio=20.0. **Tier 2 → Tier 1 RESET**. G-rule merge_conflict_manual_rebase-tier4: **1/3**.

**Iter ~1979 summary:** ✅ Nominal. 0 new alerts. pending=0 (Larry approved forge-preflight-marker-selfcheck-001 → PR #524 built). PR #520 Mirror REVISION (data-loss: captures.json RMW window) → Forge revised → Mirror re-reviewing round 1. PR #524 NEW (Mirror reviewing). PR #522 Mirror PASS, held behind #520. Watermark: 1041 unchanged. 4 open PRs: #497 (72h deadline Jun-17T04:02Z ~32h), #520 (revision-1 Mirror review), #522 (held behind #520), #524 (new, Mirror review). PRIME ratio≈19.98. **Tier 1 → Tier 2 DE-ESCALATED**.

**Iter ~1977 summary:** ✅ Nominal. 2 new alerts, both Tier-3 silenced (L1039 review-pass PR #522, L1040 approval_request forge-preflight-marker-selfcheck-001). PR #521 CLOSED by heal_orphan_autoregister ✅. PR #522 Mirror PASS (19:35Z), auto-merge held behind PR #520 (file overlap). pending=1: forge-preflight-marker-selfcheck-001 (Beacon-DM'd Larry 13:35 MDT, awaiting "go"). G-rule Forge-preflight-marker-error-retry: DISPATCHED → Beacon spec → pending approval. Watermark: 1038→1040. 3 open PRs: #497 (MERGEABLE, 72h deadline Jun-17T04:02Z ~32.4h), #520 (Mirror review in progress), #522 (Mirror PASS, held). PRIME ratio≈19.98. Tier 1, consecutive_clean=1.

**Iter ~1976 summary:** ⚠️ Check 1: Forge-preflight-marker-error-retry WARN (p41-schedule-harden preflight, 13:23:24 MDT, auto-recovered 13:25:40). G-rule **2/3 → 3/3 → DISPATCHED** (Beacon: pulse-forge-preflight-marker-error-retry-001). 3 new PRs: #520 (p41-schedule-harden, Mirror review), #521 (ingest-selftest, self-managed), #522 (missions-card-gc-warn-demote-001, Mirror review). 0 new alerts. Watermark: 1038 unchanged. 4 open PRs: #497 (MERGEABLE, 72h deadline Jun-17T04:02Z ~32.5h), #520, #521, #522. PRIME ratio≈19.98. Tier 1, consecutive_clean=0.

**Iter ~1975 summary:** ✅ Nominal. 0 new alerts. 0 stalls. 0 pending directives. All daemons alive. Forge building p41-schedule-harden (~19 min elapsed, no PR yet). missions-card-gc-warn-demote-001 in Forge inbox, queued behind p41-schedule-harden. Watermark: 1038 unchanged. 1 open PR: #497 (MERGEABLE, age≈39.3h, deadline Jun-17T04:02Z ~32.7h). PRIME ratio≈20.40. Tier 1, consecutive_clean=1.

**Iter ~1974 summary:** ⚠️ Check A: behind origin/main by 1 commit (PR #519 fix-missions-auto-reconcile merged 19:13:44Z). Fast-forward executed. Check 4: pending=0 (missions-card-gc-warn-demote-001 APPROVED, Forge build dispatched). p41-schedule-harden: Forge actively building (~11 min). PR #519 merged (unreviewed-merge, alert TBD). Watermark: 1038 unchanged. 1 open PR: #497 (now MERGEABLE, age≈39.2h, deadline Jun-17T04:02Z ~32.8h). PRIME ratio≈20.375. Tier-reset. Tier 1, consecutive_clean=0.

**Iter ~1973 summary:** ✅ Near-nominal. 2 new alerts (L1037-L1038, both Tier-3 silenced). Check 4: pending=1 (missions-card-gc-warn-demote-001, bot-DM already delivered 13:05 MDT, awaiting Larry "go"). No G-rule advances. No dispatches. Watermark: 1036→1038. 1 open PR: #497 (age≈39.1h, deadline Jun-17T04:02Z ~32.9h). PRIME ratio≈20.38. Tier 1, consecutive_clean=0.

**Iter ~1972 summary:** ⚠️ Tier-reset 3→1. 2 new alerts. L1035 missions-card-gc/summary → Tier-4; G-rule 3/3 → dispatched Beacon `pulse-missions-card-gc-warn-vs-info-001`. L1036 unreviewed-merge:518 → Tier-4, bot-delivered, Larry judgment, carry. PRs #512+#513 now CLOSED. 1 open PR: #497 (age≈38.9h, deadline Jun-17T04:02Z ~33h). Watermark: 1034→1036. PRIME ratio≈20.79.

**Iter ~1971 summary:** ✅ Nominal. 0 new alerts. Watermark: 1034 (unchanged). pending=0. **Tier 2 → Tier 3 PROMOTED** (consecutive_clean=3). Daemons all alive. No G-rule advances. 3 open PRs: #497 (age≈38.3h, deadline Jun-17T04:05Z ~33.7h), #512 CONFLICTING, #513 CONFLICTING.

**Iter ~1970 summary:** ✅ Nominal. 2 new alerts, both Tier-3 silenced (L1033-L1034, medic-diagnosis). Watermark: 1032→1034. pending=0. Tier 2, consecutive_clean=2. Daemons all alive. No G-rule advances. 3 open PRs: #497 (age≈38h, deadline Jun-17T04:05Z ~34h), #512, #513.

**Iter ~1969 summary:** ✅ Nominal. 4 new alerts, all Tier-3 silenced (L1029-1032). Watermark: 1028→1032. pending=0. Tier 2, consecutive_clean=1. Daemons all alive. **G-rule medic-diagnosis-tier4: COMPLETE ✅** (PR #515 merged). **G-rule healer-unrouted-pr-tier3-translation: COMPLETE ✅** (PR #516 merged, L1031+L1032 verified Tier-3). 3 open PRs: #497 (age≈39.3h, deadline Jun-17T04:05Z ~30.3h), #512, #513.

**Iter ~1968 summary:** ✅ Nominal. 0 new alerts. 0 directives. Watermark: 1028 (unchanged). pending=0. **Tier 1 → Tier 2 PROMOTED** (consecutive_clean=3). Daemons all alive. No new G-rule advances. PR #516 awaiting Mirror review. PR #515 Mirror PASS, auto-merge held behind #516. PR #497 REVIEW_ESCALATE age≈39h (deadline Jun-17T04:05Z, ~34.6h remaining).

**Iter ~1965 summary:** ⚠️ Check 0: L1026-1027 (unreviewed-merge:510+509, Tier-4, bot-delivered). Check E: PR #516 new. Watermark: 1025→1027. Tier 1, consecutive_clean=0. Daemons: beacon:2744840, chain-event-shipper:2744551, outbox-notifier:2744914, dashboard-api:2868353, inbox-watcher:2530123. PRIME ratio=20.77.

**heal_pipeline_stall.py --dry-run note:** `--dry-run` does NOT suppress writes to larry-alerts.jsonl. When cooldown expires, the alert fires in dry-run mode. Be aware: calling --dry-run in a cycle will write real alerts if the cooldown has passed. Always check wc -l of the file before and after.

**medic-diagnosis alerts (learned iter ~1905):** The medic module sends `kind=notification, intent=medic-diagnosis` alerts with a chat_id when it performs detailed PR diagnoses. These carry a chat_id meaning the DM was already delivered directly. Triage helper classifies as Tier-4 (no registry template). No second DM from Pulse warranted.

**Watermark gap (closed iter ~1936):** was watermark=989 >> file=978; advanced to 986 = file length. Gap closed. Standard get-watermark path works again. If gap re-forms: check `wc -l` vs watermark each iter; manually read new tail lines and triage if file < watermark. Do NOT set watermark backward.

---

## telegram-approval-self-dispatch-denied G-rule (observed iter ~1963)

**Rule:** When Larry replies "Go" (or similar approval shortcut) in Telegram for a Beacon-authored APPROVAL_REQUEST plan, the bot attempts to dispatch the plan back to Beacon (its own source), resulting in "self-dispatch denied (beacon → beacon)". The approval is NOT processed. The plan stays pending in beacon-pending-approvals.json. Recovery: Larry must re-approve explicitly via Telegram or dashboard. **G-rule count: 1/3** — dispatch to Beacon at 3/3 for a routing fix in the bot's approval handler.

---

## heal-pipeline-stall-mirror-pass-unmerged-tier4 G-rule (observed iter ~1981)

**Rule:** When `heal-pipeline-stall` fires `pipeline-stall:mirror-pass-unmerged:PR#N` into larry-alerts.jsonl, the triage helper returns Tier-4 (novel, no registry template). The medic module almost always diagnoses the same event and DMs Larry directly via chat_id. Do NOT send a second Pulse DM — journal-note only. G-rule count: **1/3** — dispatch to Beacon at 3/3 for Tier-3 translation in `config/alert-translations.json`.

---

## Key standing items (as of iter ~1982)

| Item | Status | Action needed |
|---|---|---|
| PR #525 MERGED ✅ | feat(missions): re-brief narrator cards on mission state change. Merged 2026-06-15T20:21:50Z (`1bebe776`). missions-v2-phase4.1 sequence advancing. | DONE. Watch for next sequence step. |
| PR #522 MERGED ✅ | fix(missions-card-gc): stop emitting routine success summary as Pulse-claimed digest alert. Merged 2026-06-15 after iter ~1986. **G-rule missions-card-gc-warn-vs-info COMPLETE ✅**. | DONE. |
| PR #529 MERGED ✅ | `cred-drift-ignore-feature-flags-001`: adds `OURLIBERTY_NEWMISSION_INGEST_ENABLED` to ignored_keys allowlist in detect_drift. Merged 2026-06-15T23:48Z. | DONE. Credential drift false-positive resolved. |
| PR #532 delegate-endpoint PIPELINE-ORPHANED | [yellow] OPEN, MERGEABLE, 0 GitHub reviews. Forge preamble failure × 4 → RETRY_EXHAUSTED → dead-letter to Beacon (processed, Beacon inbox now empty) → no new Forge task. PR has no active pipeline driver. missions-v2-delegate-fix step 1. Needs Larry judgment. | Re-trigger Forge revision manually, or close PR and restart? Escalate if still stuck. |
| PR #55 chat-label-fix MERGED ✅ | missions-v2-delegate-fix step 2. Merged 02:27Z 2026-06-16. | DONE |
| PR #497 REVIEW_ESCALATE | [yellow] mergeable=UNKNOWN; Mirror REVIEW_ESCALATE Jun-14T04:02:56Z; age≈49.5h; 72h expires Jun-17T04:02:56Z (~25.5h remaining). | Escalate if still open at Jun-17T04:02:56Z |
| unreviewed-merge:511/499/494/489/510/509/518/519/530 | [yellow] PRs merged by Larry without Mirror; bot-delivered for others. Larry's judgment call. | Reply appropriate shortcut or silence |
| G-rule stall-detector Forge build | [yellow] Beacon spec complete. Forge build pending Larry's dashboard approval. | Approve Forge build via dashboard |
| Check VIII rule=lower | [yellow] FN=3027, TP=5, FP=2 — threshold too high. | `approve check-viii-update-2026-06-15` when shortcut lands |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| Telegram 409 burst | [yellow] G-rule **2/3**. All self-inflicted by calling get-messages. | Watch; dispatch at 3/3 |
| G-rule telegram-approval-self-dispatch-denied | [yellow] **1/3** — Larry "Go" → dispatch failed (self-dispatch denied beacon→beacon). | Watch; dispatch to Beacon at 3/3 for bot routing fix |
| G-rule missions-card-gc-warn-vs-info | [blue] **COMPLETE ✅** PR #522 merged 2026-06-15 (iter ~1987 observation). | DONE. |
| G-rule Forge-preflight-marker-error-retry | [blue] **COMPLETE ✅** PR #524 merged `97300fc1` Jun-15. | Done. |
| G-rule missions-autoregister-warn-vs-info | [blue] **COMPLETE ✅** PR #514 merged Jun-15. | Done. |
| G-rule medic-diagnosis-tier4 | [blue] **COMPLETE ✅** PR #515 merged Jun-15T17:27:41Z. | Done. |
| G-rule healer-unrouted-pr-tier3-translation | [blue] **COMPLETE ✅** PR #516 merged Jun-15T17:27:36Z. | Done. |
| G-rule merge_conflict_manual_rebase-tier4 | [blue] **1/3** — outbox-notifier DMs Larry directly via chat_id; Pulse should NOT double-DM. | Watch; dispatch to Beacon at 3/3 for Tier-3 translation |
| G-rule heal-pipeline-stall-mirror-pass-unmerged-tier4 | [blue] **1/3** — heal-pipeline-stall fires Tier-4; medic DMs directly; Pulse no-DM. | Watch; dispatch to Beacon at 3/3 for Tier-3 translation |
| Check I 2026-06-15 | [blue] 1 proposal dispatched iter ~1899, Beacon processed | Beacon spec in progress |
| G-rule catalog-accuracy-drift-tier4 | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule ledger/check-i Tier-4 | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule health-notify-script-missing | [blue] **1/3** | Watch; dispatch at 3/3 |
| catalog-accuracy-drift | [blue] 9/34 ourliberty-graph shelf cards drifted | route=digest; journal-note only |
| G-rule Forge-timeout-worktree-missing-retry-loop | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule F24-empty-prompt-envelope-rejected | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-preflight-CLARIFY_REQUEST | [blue] **2/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule revision-phase-preamble-missing | [blue] **1/3 (new)** | Forge outbox missing "Revision N applied:" preamble → RETRY_EXHAUSTED. Watch; dispatch to Beacon at 3/3 |
| G-rule auto-dispatch-APPROVAL_REQUEST-task-id-mismatch | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 (warn-vs-info) |
| G-rule telegram-409-burst | [yellow] **2/3** | Watch; dispatch at 3/3 |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| Stale bash orphans | [blue] PIDs 1834248 (17d+) + 2605007 (1d+). Ss, low CPU. | Carry |
