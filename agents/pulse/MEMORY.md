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

## Status snapshot — updated 2026-06-17 09:26Z UTC (Iter ~2116, Tier 3, consecutive_clean=1→2, NOMINAL)

**Iter ~2116 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=b911208f=origin/main (clean). Last sync 08:47:39Z (~39 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅.** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 3, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-17 08:52Z UTC (Iter ~2115, Tier 3, consecutive_clean=0→1, NOMINAL)

**Iter ~2115 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=73863848=origin/main (clean). Last sync 08:47:39Z (~4 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅.** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 3, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-17 08:18Z UTC (Iter ~2114, Tier 2→3, consecutive_clean=2→3→de-escalate, NOMINAL)

**Iter ~2114 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=f589e1e5=origin/main (clean). Last sync 07:47:20Z (~29 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅.** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 2→3, consecutive_clean=2→3→de-escalate.**

## Status snapshot — updated 2026-06-17 07:56Z UTC (Iter ~2113, Tier 2, consecutive_clean=1→2, NOMINAL)

**Iter ~2113 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=b1055e20=origin/main (clean). Last sync 07:47:20Z (~9 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅.** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 2, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-17 07:42Z UTC (Iter ~2112, Tier 2, consecutive_clean=0→1, NOMINAL)

**Iter ~2112 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=800163dc=origin/main (clean). Last sync 06:47:19Z (~54 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅.** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 2, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-17 07:22Z UTC (Iter ~2111, Tier 1→2, consecutive_clean=2→3→de-escalate, NOMINAL)

**Iter ~2111 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=7aa976f0=origin/main (clean). Last sync 06:47:19Z (~35 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅ (all 4 steps: PR #558+#559+#560+PR #59-dashboard).** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 1→2, consecutive_clean=2→3→de-escalate.**

## Status snapshot — updated 2026-06-17 07:16Z UTC (Iter ~2110, Tier 1, consecutive_clean=1→2, NOMINAL)

**Iter ~2110 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=7c57d9a2=origin/main (clean). Last sync 06:47:19Z (~29 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅ (all 4 steps: PR #558+#559+#560+PR #59-dashboard).** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-17 07:07Z UTC (Iter ~2109, Tier 1, consecutive_clean=0→1, NOMINAL)

**Iter ~2109 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=3dbd9f56=origin/main (clean). Last sync 06:47:19Z (~20 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅ (all 4 steps: PR #558+#559+#560+PR #59-dashboard).** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.53 (1006 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-17 07:04Z UTC (Iter ~2108, Tier 2→1, consecutive_clean=0, SIGNAL/NO-ACTION)

**Iter ~2108 summary:** ⚠️ Signal (tier-reset). 1 new Tier-4 alert (L1076: outbox-notifier/sequence-complete:projects-v3-p2, 06:50Z; bot delivered idx=1075 at 06:54Z via route=escalate; Pulse no-DM). All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=04a3264b=origin/main (clean). Last sync 06:47:19Z (~17 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs across all repos.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 SEQUENCE COMPLETE ✅ (all 4 steps: PR #558+#559+#560+PR #59-dashboard).** Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.51 (1005 interventions, 49 systemic fixes). **Tier 2→1, consecutive_clean=0.** G-rule sequence-complete-tier4 2/3.

## Status snapshot — updated 2026-06-17 06:47Z UTC (Iter ~2107, Tier 1→2, consecutive_clean=2→3→de-escalate, NOMINAL)

**Iter ~2107 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=59a4753c=origin/main (clean). Last sync 05:47:08Z (~60 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs in ourliberty-agent-core.** PR #560 (Contract C: multi-source suggestion intake) MERGED ✅. PR #59 in ourliberty-dashboard (p2-funnel-card-ui, created 06:45:06Z, 2 min old, Vercel SUCCESS, Mirror review pending). projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 ACTIVE:** PR #558 (A) ✅ + PR #559 (B) ✅ + PR #560 (C) ✅ MERGED; PR #59 ourliberty-dashboard (p2-funnel-card-ui) OPEN. Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.51 (1005 interventions, 49 systemic fixes). **Tier 1→2 DE-ESCALATED, consecutive_clean=2→3.**

## Status snapshot — updated 2026-06-17 06:38Z UTC (Iter ~2106, Tier 1, consecutive_clean=1→2, NOMINAL)

**Iter ~2106 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3809960). Repo HEAD=3fa4df2c=origin/main (clean). Last sync 05:47:08Z (~51 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **1 open PR: #560** (P2 Contract C multi-source suggestion intake, created 06:29:46Z, ~7 min old, normal pipeline, Mirror review dispatch expected). projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 ACTIVE:** PR #558 (A) ✅ + PR #559 (B) ✅ MERGED; PR #560 (C) OPEN. Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.51 (1005 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-17 06:32Z UTC (Iter ~2105, Tier 1, consecutive_clean=0→1, NOMINAL)

**Iter ~2105 summary:** ✅ Nominal. 1 Tier-3 alert (L1075 heal-stale-daemon-code/auto-restarted:ourliberty-dashboard-api.service at 06:25:24Z, silenced — PR #559 code deploy). All 5 daemons alive: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api NEW PID 3809960 (restarted to deploy PR #559 code). Repo HEAD=735153f1=origin/main (clean). Last sync 05:47:08Z (no-change, ~45 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **1 open PR: #560** (P2 Contract C: multi-source suggestion intake, created 06:29:46Z, ~2 min old, Mirror review pending dispatch, normal pipeline). projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 ACTIVE:** PR #558 (A) ✅ + PR #559 (B) ✅ MERGED; PR #560 (C) OPEN. Check I skipped (artifact exists 2026-06-17). PRIME ratio=20.51 (1005 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-17 06:22Z UTC (Iter ~2104, Tier 1, consecutive_clean=2→0, DRIFT/FIXED)

**Iter ~2104 summary:** ⚠️ Drift (auto-fixed). 0 new alerts. Check A: repo behind 1 commit (PR #559 `feat(dashboard-api): universal action card for mission-backed funnel cards — P2 Contract B`) → fast-forwarded 4df56cea→37316266. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3734769). Repo HEAD=37316266=origin/main (clean post-ff). Last sync 05:47:08Z (~35 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2:** PR #558 (P2 Contract A) MERGED ✅; PR #559 (P2 Contract B universal action card) MERGED ✅. Both contracts shipped. Check I skipped (artifact exists for 2026-06-17). PRIME ratio=20.51 (1005 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=2→0.**

## Status snapshot — updated 2026-06-17 06:14Z UTC (Iter ~2103, Tier 1, consecutive_clean=1→2, NOMINAL)

**Iter ~2103 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3734769). Repo HEAD=a0f4a8f0=origin/main (clean; wrapper committed be3b1eb6 Pulse cycle 20260617T061117Z + GC healer auto-committed a0f4a8f0 since iter ~2102). Last sync 05:47:08Z (~27 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **1 open PR: #559** (P2 Contract B universal action card, created 06:02Z, Mirror review IN PROGRESS since 06:07Z, normal pipeline). projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 ACTIVE:** PR #558 (P2 Contract A) MERGED 05:43Z ✅; PR #559 (P2 Contract B) Mirror review in progress. Check I skipped (artifact exists for 2026-06-17). PRIME ratio=20.49 (1004 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-17 06:09Z UTC (Iter ~2102, Tier 1, consecutive_clean=0→1, NOMINAL)

**Iter ~2102 summary:** ✅ Nominal. 1 Tier-3 alert (L1074 heal-systemd-install-drift/stuck-timer-healed:ourliberty-cycle.timer at 06:00:05Z, silenced — cycle timer stuck, auto-recovered by healer). All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3734769). Repo HEAD=d82220c3=origin/main (clean). Last sync 05:47:08Z (~22 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **1 open PR: #559** (P2 Contract B universal action card for mission-backed funnel cards, created 06:02Z, Mirror review dispatched 06:07Z, normal pipeline). projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 ACTIVE:** PR #558 (P2 Contract A meaning-layer) MERGED 05:43Z ✅; PR #559 (P2 Contract B actions) Mirror review IN PROGRESS. Check I skipped (artifact exists for 2026-06-17). PRIME ratio=20.49 (1004 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-17 05:59Z UTC (Iter ~2101, Tier 3→1, consecutive_clean=1→0, DRIFT/FIXED)

**Iter ~2101 summary:** ⚠️ Drift (auto-fixed). 1 Tier-3 alert (L1073 heal-wedged-review-sessions/wedged-review-reaped:wt-forge-p2-meaning-layer, silenced — forge review session reaped post-merge). Check A: repo behind 1 commit (PR #558 `feat(narrator): meaning layer for orphan + suggested funnel missions (P2 Contract A)`) → fast-forwarded 5f98b7e0→65561e8b. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3734769). Repo HEAD=65561e8b=origin/main (clean post-ff). Last sync 05:47:08Z (no-change, ~9 min). No stalls. pending=0. Credential rotation: OK. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅. **projects-v3-p2 ACTIVE:** PR #558 (P2 Contract A meaning-layer, 4 files, 814 lines) MERGED 05:43Z. Step 1 COMPLETE. Next steps pending from sequence advancer. Check I skipped (artifact exists for 2026-06-17). PRIME ratio=20.49 (1004 interventions, 49 systemic fixes). **Tier 3→1, consecutive_clean=1→0.**

## Status snapshot — updated 2026-06-17 05:24Z UTC (Iter ~2100, Tier 3, consecutive_clean=0→1, NOMINAL)

**Iter ~2100 summary:** ✅ Nominal. 1 Tier-3 alert (L1072 outbox-notifier/mirror-dag-pass:projects-v3-p2, silenced — known pattern). All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3734769). Repo HEAD=00b85223=origin/main (clean; 3 new commits since iter ~2099: PR #557 spec/projects-v3-p2, Pulse cycle wrapper, GC healer autoregister). Last sync 04:47:32Z (~37 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅ (all 3/3 PRs merged). **projects-v3-p2 ACTIVE** (Larry approved dag-preflight 04:54Z; Mirror PASSED 04:56Z; Beacon new session started 05:21Z — first step building). Check I skipped (artifact exists for 2026-06-17). PRIME ratio=20.47 (1003 interventions, 49 systemic fixes). **Tier 3, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-17 04:47Z UTC (Iter ~2099, Tier 2→3, consecutive_clean=2→3→de-escalate, NOMINAL)

**Iter ~2099 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 3769291, dashboard_api 3734769). Repo HEAD=935eb29b=origin/main (clean; newest commit = GC healer auto-committed missions.json delta). Last sync 04:40:50Z (no-change, ~6 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. projects-v3-p4 COMPLETE ✅ (all 3/3 PRs merged). Check I skipped (artifact exists for 2026-06-17). PRIME ratio=20.47 (1003 interventions, 49 systemic fixes). **Tier 2→3, consecutive_clean=2→3→de-escalate.**


## Key standing items (as of iter ~1982)

| Item | Status | Action needed |
|---|---|---|
| PR #525 MERGED ✅ | feat(missions): re-brief narrator cards on mission state change. Merged 2026-06-15T20:21:50Z (`1bebe776`). missions-v2-phase4.1 sequence advancing. | DONE. Watch for next sequence step. |
| PR #522 MERGED ✅ | fix(missions-card-gc): stop emitting routine success summary as Pulse-claimed digest alert. Merged 2026-06-15 after iter ~1986. **G-rule missions-card-gc-warn-vs-info COMPLETE ✅**. | DONE. |
| PR #529 MERGED ✅ | `cred-drift-ignore-feature-flags-001`: adds `OURLIBERTY_NEWMISSION_INGEST_ENABLED` to ignored_keys allowlist in detect_drift. Merged 2026-06-15T23:48Z. | DONE. Credential drift false-positive resolved. |
| PR #532 delegate-endpoint MERGED ✅ | [blue] MERGED 2026-06-16T05:46:10Z. missions-v2-delegate-fix sequence COMPLETE. | DONE. |
| PR #55 chat-label-fix MERGED ✅ | missions-v2-delegate-fix step 2. Merged 02:27Z 2026-06-16. | DONE |
| PR #497 CLOSED ✅ | [blue] Larry closed manually at 2026-06-16T11:54:52Z (05:54 MDT). **RESOLVED.** | DONE. |
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
| G-rule sequence-complete-tier4 | [blue] **2/3** (iter ~2108: 2nd occurrence) — outbox-notifier sequence-complete:* alerts classify Tier-4 (novel); bot delivers via route=escalate; Pulse no-DM. | Watch; dispatch to Beacon at 3/3 for Tier-3 translation |
| G-rule catalog-accuracy-drift-tier4 | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule dirty-tree-beacon-data-files-tier4 | [blue] **1/3** (NEW iter ~2085) — pulse-self-escalates dirty tree (Beacon runtime files); GC healer auto-resolves; Tier-4 (novel). | Watch; propose Tier-3 translation at 3/3 |
| G-rule ledger/check-i Tier-4 | [blue] **2/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule health-notify-script-missing | [blue] **1/3** | Watch; dispatch at 3/3 |
| catalog-accuracy-drift | [blue] 9/34 ourliberty-graph shelf cards drifted | route=digest; journal-note only |
| G-rule Forge-timeout-worktree-missing-retry-loop | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule F24-empty-prompt-envelope-rejected | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-preflight-CLARIFY_REQUEST | [blue] **2/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule revision-phase-preamble-missing | [blue] **2/3** | Forge outbox missing "Revision N applied:" preamble → retry chain. Watch; dispatch to Beacon at 3/3 |
| G-rule mirror-malformed-verdict-marker | [blue] **1/3** (NEW) | Mirror produces output without canonical verdict marker (=== REVIEW_PASS === etc.) → MalformedMirrorMarker × 2/3 for s-3-failure-cost-pause. Distinct from mirror-no-session-revision-loop. Watch; dispatch at 3/3 |
| G-rule mirror-no-session-revision-loop | [blue] **2/3** | Mirror review NO_SESSION × 2+ for PR #497; Beacon re-dispatches Mirror instead of Forge. Watch; dispatch at 3/3 |
| G-rule auto-dispatch-APPROVAL_REQUEST-task-id-mismatch | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 (warn-vs-info) |
| G-rule telegram-409-burst | [yellow] **2/3** | Watch; dispatch at 3/3 |
| G-rule dual-bot-instance-409-external | [blue] **1/3** (NEW iter ~2102) — 409 burst 05:21–05:25Z 2026-06-17, NOT Pulse-caused. Two competing getUpdates loops. Possible trigger: Beacon P2 session start. Distinct from self-inflicted 409. | Watch; dispatch to Beacon at 3/3 |
| **G-rule watermark-rotation-gap** | [blue] **2/3** (NEW iter ~2035) — larry-alerts-retention.py compacts file, watermark left > file length; new alerts missed until gap detected + repaired. Prior: iter ~1936. | Watch; dispatch to Beacon at 3/3 for auto-detect/repair fix in cycle startup |
| Phase S ALL 6/6 MERGED ✅ | [blue] s-1 PR#541 ✅, s-2 PR#542 ✅, s-3 PR#543 ✅, s-4 PR#544 ✅, s-5 PR#58 ourliberty-dashboard ✅, s-6-drain PR#545 ✅ (10:52:38Z). missions-v2-phase-s SEQUENCE COMPLETE. | DONE. |
| PR #497 CLOSED ✅ | [blue] Larry closed manually 2026-06-16T11:54:52Z. RESOLVED. | DONE. |
| projects-v3-p4 COMPLETE ✅ | [blue] PR#554 (p4-complete-signal) + PR#555 (p4-cleanup-committer) + **PR#556 (p4-postmerge-exec, 03:17:42Z)** ALL MERGED. GC healer Contract D live (ab8353aa). outbox-notifier sent sequence-complete DM 03:17:44Z. | DONE. All 3/3. |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap. outbox-notifier also drops regular approval_request markers from Beacon inter-agent sessions (observed iter ~2020: both routing-signal and approval_request dropped in same 5-min window). | DAG markers and scope-decision markers still fall through; recover manually |
| projects-v3-p2 COMPLETE ✅ | [blue] All 4 steps merged: PR #558 (meaning-layer) + PR #559 (actions) + PR #560 (suggest-intake) in ourliberty-agent-core; PR #59 (funnel-card-ui) in ourliberty-dashboard. Sequence-complete DM delivered 06:54Z 2026-06-17. | DONE. |
| Stale bash orphans | [blue] PIDs 1834248 (17d+) + 2605007 (1d+). Ss, low CPU. | Carry |
