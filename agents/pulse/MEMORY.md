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

## Status snapshot — updated 2026-06-17 00:26Z UTC (Iter ~2082, Tier 1, consecutive_clean=1→2, NOMINAL)

**Iter ~2082 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3676902, dashboard_api 3688711). Repo HEAD=16d7dc77=origin/main (clean). Last sync 23:29:16Z (~57 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. Check I skipped (already fired iter ~2080, artifact confirmed). PRIME ratio=20.39 (999 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-17 00:18Z UTC (Iter ~2081, Tier 1, consecutive_clean=0→1, NOMINAL)

**Iter ~2081 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3676902, dashboard_api 3688711). Repo HEAD=329295b5=origin/main (clean). Last sync 23:29:16Z (~49 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. Check I skipped (already fired iter ~2080, artifact confirmed). PRIME ratio=20.39 (999 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-17 00:14Z UTC (Iter ~2080, Tier 1, consecutive_clean=2→0, TIER-RESET)

**Iter ~2080 summary:** ⚠️ Tier-reset. 2 Tier-4 alerts (L1059 ledger-weekly-2026-06-15 + L1060 check-i-2026-06-15; routine Check I outputs, bot-delivered via route=escalate; no Pulse double-DM). All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3676902, dashboard_api 3688711). Repo HEAD=17aafd32=origin/main (clean). Last sync 23:29:16Z (~41 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. Check I fired (Wednesday, mode=digest, no new dispatch, dedup-skip on 04807c018d). iter ~2079 incorrectly labeled Wed as "Tuesday" — corrected this iter. PRIME ratio=20.39 (999 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=2→0.**

## Status snapshot — updated 2026-06-17 00:02Z UTC (Iter ~2079, Tier 1, consecutive_clean=1→2, NOMINAL)

**Iter ~2079 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3676902, dashboard_api 3688711). Repo HEAD=2ead9913=origin/main (clean). Last sync 23:29:16Z (~32 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. PRIME ratio=20.39 (999 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-16 23:57Z UTC (Iter ~2078, Tier 1, consecutive_clean=0→1, NOMINAL)

**Iter ~2078 summary:** ✅ Nominal. 1 Tier-3 alert (L1058 heal-stale-daemon-code auto-restarted dashboard_api; known pattern, digest-only). All 5 daemons alive (dashboard_api new PID 3688711 post-restart, PR #551 code live). Repo HEAD=18b08f31=origin/main (clean). Last sync 23:29:16Z (~28 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1 SEQUENCE COMPLETE ✅. PRIME ratio=20.39 (999 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-16 23:52Z UTC (Iter ~2077, Tier 1, consecutive_clean=2→0, DRIFT/FIXED)

**Iter ~2077 summary:** ⚠️ Drift (auto-fixed). 0 new alerts. All 5 daemons alive (beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3676902, dashboard_api 3593488). Repo HEAD=bb6ab1b3→822c24d2=origin/main (fast-forwarded, clean). Last sync 23:29:16Z (~23 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** **projects-v3-p1 SEQUENCE COMPLETE ✅** (PR #551 p1-funnel-derive C4 MERGED 23:44:39Z). PRIME ratio=20.37 (999 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=2→0.**

## Status snapshot — updated 2026-06-16 23:33Z UTC (Iter ~2075, Tier 1, consecutive_clean=0→1, NOMINAL)

**Iter ~2075 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive (beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3676902, dashboard_api 3593488). Repo HEAD=1e9d45c7=origin/main (clean). Last sync 23:29:16Z (~4 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **PR #551 OPEN** (p1-funnel-derive C4, Mirror review started 23:30:09Z). projects-v3-p1 sequence: C1+C2/C3 merged; C4 Mirror review IN PROGRESS. PRIME ratio=20.37 (998 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-16 23:29Z UTC (Iter ~2074, Tier 2→1, consecutive_clean=0, DRIFT/FIXED)

**Iter ~2074 summary:** ⚠️ Drift (auto-fixed). 1 new alert (L1057 Tier-3: heal-stale-daemon-code auto-restarted outbox_notifier). Check A: repo behind 1 commit (PR #550 merge) → fast-forwarded c1842eaf→e41da8ea. All 5 daemons alive (beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier NEW PIDs 3676902+3677736, dashboard_api 3593488). Repo HEAD=e41da8ea=origin/main (clean post-ff). Last sync 22:29:31Z (~60 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **0 open PRs.** projects-v3-p1: C1 PR#549 MERGED 23:09Z, C2/C3 PR#550 MERGED 23:19Z, **p1-funnel-derive Forge IN PROGRESS since 23:20:38Z** (outbox_notifier restarted with new chain_envelope code at 23:23:48Z). PRIME ratio=20.37 (998 interventions, 49 systemic fixes). **Tier 2→1, consecutive_clean=0.**

## Status snapshot — updated 2026-06-16 23:08Z UTC (Iter ~2073, Tier 2, consecutive_clean=1→2)

**Iter ~2073 summary:** ✅ Nominal. 0 new alerts. All mandatory checks clean. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo HEAD=0690ef87=origin/main (clean). Last sync 22:29:31Z (success). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **PRs #549 (Mirror reviewing) + #550 (Forge build-phase active) OPEN** — projects-v3-p1 C1/C2-C3 in progress. MalformedForgeMarker on p1-drain-archive preflight (retry 1/3) — covered by completed G-rule PR #524. PRIME ratio=20.35 (997 interventions, 49 systemic fixes). **Tier 2, consecutive_clean=1→2.**

## Status snapshot — updated 2026-06-16 22:54Z UTC (Iter ~2072, Tier 2, consecutive_clean=0→1)

**Iter ~2072 summary:** ✅ Nominal. 0 new alerts. All mandatory checks clean. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo HEAD=e23ed4db=origin/main (clean). Last sync 22:29:31Z (success). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. **PR #549 OPEN** (p1-target-repo C1, created 22:46:25Z, Mirror not yet dispatched, 7min old, within 30min threshold; preflight marker-error retry 1/3 in progress per completed G-rule PR #524). Forge building p1-drain-archive (started 22:46:42Z). projects-v3-p1 sequence ACTIVE. PRIME ratio=20.35 (997 interventions, 49 systemic fixes). **Tier 2, consecutive_clean=0→1.**

## Status snapshot — updated 2026-06-16 22:33Z UTC (Iter ~2071, Tier 1→2, consecutive_clean=2→3→de-escalate)

**Iter ~2071 summary:** ✅ Nominal. 0 new alerts. All mandatory checks clean. Tier de-escalated 1→2. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo HEAD=87ba188e=origin/main (clean). Last sync 22:29:31Z (success; synced PR #548 `fix(merge): nudge droplet to sync main right after a desktop merge`). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. 0 open PRs. **projects-v3-p1 sequence ACTIVE: Forge building p1-target-repo (started 22:30:55Z), p1-drain-archive queued.** PRIME ratio=20.35 (998 interventions, 49 systemic fixes). **Tier 1→2, consecutive_clean=0.**

## Status snapshot — updated 2026-06-16 22:28Z UTC (Iter ~2070, Tier 1, consecutive_clean=2)

**Iter ~2070 summary:** ✅ Nominal. 1 new alert (L1056 mirror-dag-pass:projects-v3-p1 Tier-3 resolved, no DM). All mandatory checks clean. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo HEAD=87d147aa=origin/main (GC healer auto-committed captures.json delta after iter ~2069). Last sync 22:20:03Z (~8 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. **projects-v3-p1 sequence: DAG preflight PASSED 22:26Z, transitioned pending→active, step 1 dispatch expected from advancer next tick.** PR #497 CLOSED ✅. 0 open PRs. PRIME ratio=20.35 (998 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=2.**

## Status snapshot — updated 2026-06-16 22:18Z UTC (Iter ~2069, Tier 1, consecutive_clean=1)

**Iter ~2069 summary:** ✅ Nominal. 0 new alerts. All mandatory checks clean. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo 33593240=origin/main (clean). Last successful sync 20:30:31Z (~107 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. 0 open PRs. PRIME ratio=20.35 (998 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=1.** Note: Beacon responded to Larry's projects-v3-p1 dispatch request (16:05 MDT) — timing edge noted (PR #547 was ahead of sync); repo now includes PR #547 post iter ~2068 fast-forward.

## Status snapshot — updated 2026-06-16 22:12Z UTC (Iter ~2068, Tier 2→1, consecutive_clean=0, DRIFT/FIXED)

**Iter ~2068 summary:** ⚠️ Drift (auto-fixed). 0 new alerts. Check A: repo behind by 1 commit (PR #547 `spec(projects-v3): P1 — funnel + Missions retirement (data layer)` merged); fast-forwarded b513f4fb→86f15ab5. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo 86f15ab5=origin/main (clean, post-ff). Last successful sync 20:30:31Z (~101 min). No stalls. pending=0. Credential rotation: all clear (21 creds, none due within 60d). Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. 0 open PRs. PRIME ratio=20.35 (998 interventions, 49 systemic fixes). **Tier 2→1, consecutive_clean=0.**

## Status snapshot — updated 2026-06-16 21:52Z UTC (Iter ~2067, Tier 1→2, consecutive_clean=2→3→de-escalate)

**Iter ~2067 summary:** ✅ Nominal. 0 new alerts. All mandatory checks clean. Tier de-escalated 1→2. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo 5a68a283=origin/main (clean). Last successful sync 20:30:31Z (~81 min). No stalls. pending=0. Credential rotation: all clear. Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. 0 open PRs. PRIME ratio=20.33 (997 interventions, 49 systemic fixes). **Tier 1→2, consecutive_clean=0.**

## Status snapshot — updated 2026-06-16 21:42Z UTC (Iter ~2066, Tier 1, consecutive_clean=2, STEADY-STATE)

**Iter ~2066 summary:** ✅ Nominal. 0 new alerts. All mandatory checks clean. All 5 daemons alive (same PIDs: beacon 3556778, chain-event 2744551, inbox-watcher 3434697, outbox_notifier 3556624, dashboard_api 3593488). Repo 4b8cc61c=origin/main (clean). Last successful sync 20:30:31Z (~71 min). No stalls. pending=0. Credential rotation: all clear (21 creds, none due within 60d). Phase S ALL 6/6 MERGED ✅. PR #497 CLOSED ✅. 0 open PRs. PRIME ratio=20.33 (997 interventions, 49 systemic fixes). **Tier 1, consecutive_clean=2.**

---

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
| G-rule catalog-accuracy-drift-tier4 | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 |
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
| **G-rule watermark-rotation-gap** | [blue] **2/3** (NEW iter ~2035) — larry-alerts-retention.py compacts file, watermark left > file length; new alerts missed until gap detected + repaired. Prior: iter ~1936. | Watch; dispatch to Beacon at 3/3 for auto-detect/repair fix in cycle startup |
| Phase S ALL 6/6 MERGED ✅ | [blue] s-1 PR#541 ✅, s-2 PR#542 ✅, s-3 PR#543 ✅, s-4 PR#544 ✅, s-5 PR#58 ourliberty-dashboard ✅, s-6-drain PR#545 ✅ (10:52:38Z). missions-v2-phase-s SEQUENCE COMPLETE. | DONE. |
| PR #497 CLOSED ✅ | [blue] Larry closed manually 2026-06-16T11:54:52Z. RESOLVED. | DONE. |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap. outbox-notifier also drops regular approval_request markers from Beacon inter-agent sessions (observed iter ~2020: both routing-signal and approval_request dropped in same 5-min window). | DAG markers and scope-decision markers still fall through; recover manually |
| Stale bash orphans | [blue] PIDs 1834248 (17d+) + 2605007 (1d+). Ss, low CPU. | Carry |
