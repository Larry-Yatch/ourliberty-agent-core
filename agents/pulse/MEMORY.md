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

## §5.0 script paths — ground-truth (confirmed iter ~2183)

**Rule:** `audit_due_nudge.py` and `distill_detector.py` live in `scripts/`, NOT `review/distill/`. Only `audit_cadence_signal.py` is in `review/distill/`. Always invoke: `python3 scripts/audit_due_nudge.py`, `python3 scripts/distill_detector.py`, `python3 review/distill/audit_cadence_signal.py`.

---

## Status snapshot — updated 2026-06-18 18:16Z UTC (Iter ~2216, Tier 3, consecutive_clean=11→12, NOMINAL)

**Iter ~2216 summary:** ✅ Nominal. 0 new alerts (watermark=921=file_length). All 5 daemons alive — beacon 3734671 (Ss), chain-event 3734305 (SNs), inbox-watcher 3434697 (Ssl), dashboard_api 130620 (Ssl, ~1h41m), outbox_notifier 130853 (Ss, ~1h41m). Repo HEAD=773ad12d=origin/main (clean). Last sync 17:31:50Z (~44 min). 0 open PRs. 0 stalls. 0 pending approvals. Credential rotation: OK. Heartbeat 18:04:32Z (~12 min, fresh). Thursday: Check I/III/VIII/IX/X skip. §5.0 all no-ops. PR #576 verification window open (closes 2026-06-19T13:35Z; repair-watermark no-op confirms fix holding). Stale bash orphan PID 1834248: confirmed alive (~20d 23h). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=11→12 — steady-state continues.**

## Status snapshot — updated 2026-06-18 17:49Z UTC (Iter ~2215, Tier 3, consecutive_clean=10→11, NOMINAL)

**Iter ~2215 summary:** ✅ Nominal. 0 new alerts (watermark=921=file_length). All 5 daemons alive — beacon 3734671 (Ss), chain-event 3734305 (SNs), inbox-watcher 3434697 (Ssl), dashboard_api 130620 (Ssl, ~71m), outbox_notifier 130853 (Ss, ~71m). Repo HEAD=cfc3fdf9=origin/main (clean). Last sync 17:31:50Z (~17 min). 0 open PRs. 0 stalls (heal-pipeline-stall-state.json 6h stale but own verification confirms 0 real stalls). 0 pending approvals. Heartbeat 17:34:27Z (~15 min, fresh). Thursday: Check I/III/VIII/IX/X skip. §5.0 all no-ops. PR #576 verification window open (closes 2026-06-19T13:35Z; repair-watermark no-op confirms fix holding). Stale bash orphan PID 1834248: confirmed alive (~20d 22h). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=10→11 — steady-state continues.**

## Status snapshot — updated 2026-06-18 17:12Z UTC (Iter ~2214, Tier 3, consecutive_clean=9→10, NOMINAL)

**Iter ~2214 summary:** ✅ Nominal. 0 new alerts (watermark=921=file_length). All 5 daemons alive — beacon 3734671 (Ss), chain-event 3734305 (SNs), inbox-watcher 3434697 (Ssl), dashboard_api 130620 (Ssl, ~37 min), outbox_notifier 130853 (Ss, ~37 min). Repo HEAD=6693c39e=origin/main (2 new commits since iter ~2213: wrapper auto-commit + missions healer GC). Last sync 16:31:38Z (~41 min, no-change). 0 open PRs. 0 stalls. pending=0. Credential rotation: OK. Heartbeat 17:04:19Z (~8 min, fresh). Thursday: Check I/III/VIII/IX/X skip. §5.0 all no-ops. PR #576 verification window open (closes 2026-06-19T13:35Z; repair-watermark no-op confirms fix holding). Stale bash orphan PID 1834248: CONFIRMED alive (20d 21h 54m, sleep-loop awaiting build-check-viii-pr-2b-analyzer-001.json). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=9→10 — steady-state continues.**

## Status snapshot — updated 2026-06-18 16:39Z UTC (Iter ~2213, Tier 3, consecutive_clean=8→9, NOMINAL)

**Iter ~2213 summary:** ✅ Nominal. 1 Tier-3 alert: L921 (sequence-complete:projects-v3-p3-followup2 — all 3 steps merged PRs #579/#582/dashboard#64, bot DM'd 16:29:44Z, silenced). **Pipeline projects-v3-p3-followup2 COMPLETE.** All 5 daemons alive — beacon 3734671 (Ss), chain-event 3734305 (SNs), inbox-watcher 3434697 (Ssl), dashboard_api **130620** (Ssl, restarted 16:34Z — PR #582 mtime drift), outbox_notifier **130853** (Ss, same restart). Repo HEAD=87149b91=origin/main (missions healer auto-commits × 2). Last sync 16:31:38Z (~8 min, no-change). 0 open PRs (0 agent-core, 0 dashboard). 0 stalls. pending=0. Credential rotation: OK. Heartbeat 16:34:14Z (~5 min, fresh). Thursday: Check I/III/VIII/IX/X skip. §5.0 all no-ops. PR #576 verification window open (closes 2026-06-19T13:35Z; repair-watermark no-op confirms fix holding). Stale bash orphan PID 1834248: carrying (last confirmed ~2212). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=8→9 — steady-state continues.**

## Status snapshot — updated 2026-06-18 16:07Z UTC (Iter ~2212, Tier 3, consecutive_clean=7→8, NOMINAL)

**Iter ~2212 summary:** ✅ Nominal. 2 Tier-3 alerts: L919 (auto-restarted:ourliberty-dashboard-api.service — healer restart after PR #579 code deploy, known pattern, silenced) + L920 (auto-restarted:ourliberty-outbox-notifier.service — same code delta, silenced). All 5 daemons alive — beacon 3734671 (Ss), chain-event 3734305 (SNs), inbox-watcher 3434697 (Ssl), dashboard_api **107261** (Ssl, restarted), outbox_notifier **107946** (Ss, restarted). Repo HEAD=9dae7d9a=origin/main (chore(missions): GC healer — 2 missions-healer auto-commits post iter ~2211). Last sync 15:31:57Z (~35 min). 0 stalls. pending=0. Credential rotation: OK. Heartbeat 16:04:02Z (~3 min, fresh). Thursday: Check I/III/VIII/IX/X skip. §5.0 all no-ops. **Pipeline: projects-v3-p3-followup2** — PR #579 merged 15:37:40Z, PR #582 (fix: p3f2 — make Drop/Archive honest + durable) opened 15:51:13Z, Mirror review in progress (2 inbox tasks). PR #576 verification window open (closes 2026-06-19T13:35Z; repair-watermark no-op confirms fix holding). Stale bash orphan PID 1834248: alive (20d 20h 48m+). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=7→8 — steady-state continues.**

## Status snapshot — updated 2026-06-18 15:39Z UTC (Iter ~2211, Tier 3, consecutive_clean=6→7, NOMINAL)

**Iter ~2211 summary:** ✅ Nominal. 1 new alert L918 → Tier-3 silence (mirror-dag-pass:projects-v3-p3-followup2; bot already delivered, watermark 917→918). All 5 daemons alive — beacon 3734671 (Ss), chain-event 3734305 (SNs), inbox-watcher 3434697 (Ssl), dashboard_api 4159159 (Ssl), outbox_notifier 4159430 (Ss). Repo HEAD=61f958d6=origin/main (spec(projects-v3): P4 — closeout pass, PR #580 — new since ~2210). Last sync 15:31:57Z (~8 min). 0 stalls. pending=0. Credential rotation: OK. Heartbeat 15:34:01Z (~5 min, fresh). Thursday: Check I/III/VIII/IX/X skip. §5.0 all no-ops. **Pipeline: projects-v3-p3-followup2 active** — DAG preflight PASSED (L918, 15:10Z), PR #579 (fix: promote creates+persists project) opened 15:25Z, Mirror reviewing (inbox .archive = picked up). PR #576 verification window open (closes 2026-06-19T13:35Z; repair-watermark no-op confirms fix holding). Stale bash orphan PID 1834248: alive (20d 20h+). Orphan worktree `wt-mirror-dag-preflight-projects-v3-p3-followup` dir persists (Jun 17, git reg pruned). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=6→7 — steady-state continues.**

