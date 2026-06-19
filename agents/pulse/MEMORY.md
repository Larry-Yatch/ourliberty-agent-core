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

## Status snapshot — updated 2026-06-19 02:13Z UTC (Iter ~2240, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2240 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 routing gap confirmed (no envelope found anywhere in /agents/). mergeable=MERGEABLE (recovered from UNKNOWN iter ~2239 — GitHub re-computation resolved). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=931=file_length). All 5 daemons alive — beacon 3734671 (~47.8h), chain-event 3734305 (~47.8h), inbox-watcher 3434697 (~68.96h), outbox_notifier 217608 (~2.61h), dashboard_api 218007 (~2.60h). Repo HEAD=ac61030c=origin/main (clean, untracked trim_memory.py). Last sync 01:33:04Z (~40 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 02:06:15Z (~7 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11.4h remaining, fix holding). Stale bash orphan PID 1834248 (~21.29d+). G-rule ledger/check-i at 2/3. PRIME ratio≈19.96 (1038/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 02:09Z UTC (Iter ~2239, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2239 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 routing gap confirmed (no envelope found anywhere in /agents/). mergeable=UNKNOWN (was MERGEABLE — likely transient GitHub re-computation). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=931=file_length). All 5 daemons alive — beacon 3734671 (~47.7h), chain-event 3734305 (~47.7h), inbox-watcher 3434697 (~68.9h), outbox_notifier 217608 (~2.54h), dashboard_api 218007 (~2.53h). Repo HEAD=7a7e010d=origin/main (clean, untracked trim_memory.py). Last sync 01:33:04Z (~36 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 02:06:15Z (~3 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11.4h remaining, fix holding). Stale bash orphan PID 1834248 (~21.29d+). G-rule ledger/check-i at 2/3. PRIME ratio≈19.94 (1037/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 02:03Z UTC (Iter ~2238, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2238 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 APPROVAL_REQUEST still not in Forge inbox (Forge inbox=0). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=931=file_length). All 5 daemons alive — beacon 3734671 (~47.6h), chain-event 3734305 (~47.6h), inbox-watcher 3434697 (~68.8h), outbox_notifier 217608 (~2.43h), dashboard_api 218007 (~2.42h). Repo HEAD=b0b67dd3=origin/main (clean, untracked trim_memory.py). Last sync 01:33:04Z (~30 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 01:36:02Z (~27 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present from iter ~2230). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11.5h remaining, fix holding). Stale bash orphan PID 1834248 (~21.28d+). G-rule ledger/check-i at 2/3. PRIME ratio≈19.90 (1035/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 01:53Z UTC (Iter ~2237, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2237 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 APPROVAL_REQUEST still not in Forge inbox (Forge inbox=0). mergeable=MERGEABLE (confirmed). [yellow] DM active from iter ~2228; Larry no response. 1 new alert (L931=dispatch-branch-cleanup, Tier-3 silence, watermark 930→931). All 5 daemons alive — beacon 3734671 (~47.4h), chain-event 3734305 (~47.4h), inbox-watcher 3434697 (~68.6h), outbox_notifier 217608 (~2.26h), dashboard_api 218007 (~2.26h). Repo HEAD=208aaa4a=origin/main (clean, untracked trim_memory.py). Last sync 01:33:04Z (~20 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 01:36:02Z (~15 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present from iter ~2230). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11.7h remaining, fix holding). Stale bash orphan PID 1834248 (~21.27d+). G-rule ledger/check-i at 2/3. PRIME ratio≈19.88 (1034/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 01:42Z UTC (Iter ~2236, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2236 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 APPROVAL_REQUEST still not in Forge inbox (Forge inbox=0 confirmed). New: PR #584 now MERGEABLE (was UNKNOWN — no code conflicts). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=930=file_length). All 5 daemons alive — beacon 3734671 (~47.3h), chain-event 3734305 (~47.3h), inbox-watcher 3434697 (~68.4h), outbox_notifier 217608 (~2.09h), dashboard_api 218007 (~2.08h). Repo HEAD=a17eb851=origin/main (clean, untracked trim_memory.py). Last sync 01:33:04Z (~9 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 01:36:02Z (~5 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present from iter ~2230). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11.89h remaining, fix holding). Stale bash orphan PID 1834248 (~21.27d+). G-rule ledger/check-i at 2/3. PRIME ratio≈19.87 (1033/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 01:37Z UTC (Iter ~2235, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2235 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing — fix-p4-closeout-outputs-revisions-001 APPROVAL_REQUEST still not in Forge inbox (Forge inbox=0 confirmed). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=930=file_length). All 5 daemons alive — beacon 3734671 (~47.2h), chain-event 3734305 (~47.2h), inbox-watcher 3434697 (~68.4h), outbox_notifier 217608 (~2.01h), dashboard_api 218007 (~2.00h). Repo HEAD=a6df8c92=origin/main (clean, untracked trim_memory.py). Last sync 01:33:04Z (~4 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 01:36:02Z (~1 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present from iter ~2230). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11.97h remaining, fix holding). Stale bash orphan PID 1834248 (~21.27d+). G-rule ledger/check-i at 2/3. PRIME ratio≈19.87 (1033/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 00:53Z UTC (Iter ~2228, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall)

**Iter ~2228 summary:** ⚠️ Stall. PR #584 (p4-closeout-outputs) Mirror REVISION at 23:45Z — 2 blocking test failures (chokepoint census + daemon manifest). Beacon authored APPROVAL_REQUEST `fix-p4-closeout-outputs-revisions-001` at 23:47Z but dispatch never reached Forge (outbox_notifier routing gap on no-session-revision path; APPROVAL_REQUEST task_id `fix-p4-closeout-outputs-revisions-001` ≠ inbox task_id `notify-no-session-revision-p4-closeout-outputs`, fallback silent-drop). `[yellow]` escalation sent. p4-closeout-ui (PR #65 / ourliberty-dashboard) MERGED 23:57Z. 3 new alerts (L927-929, all Tier-4, all bot-DM'd via route=escalate). L929 = new alert source heal-droplet-git-drift (trim_memory.py untracked 7h+). All 5 daemons alive — beacon 3734671, chain-event 3734305, inbox-watcher 3434697, outbox_notifier 217608, dashboard_api 218007. Last sync 00:32Z. 0 stalls (heal_pipeline_stall). PR #576 fix holding. Stale bash orphan PID 1834248 (~21.22d+). G-rule ledger/check-i **2/3** (advanced). PRIME ratio=19.73 (1026/52, trend=improving). **Tier 1, consecutive_clean=0 — reset.**

## Status snapshot — updated 2026-06-19 00:09Z UTC (Iter ~2227, Tier 3, consecutive_clean=22→23, NOMINAL)

**Iter ~2227 summary:** ✅ Nominal. 3 new alerts (924-926, all Tier-3 silence: dispatch-branch-cleanup + heal-stale-daemon-code auto-restart ×2). heal_stale_daemon_code auto-restarted outbox_notifier (→PID 217608) and dashboard_api (→PID 218007) at 23:35Z triggered by commit 3bb474c9. All 5 daemons alive — beacon 3734671 (Ss, ~45.7h), chain-event 3734305 (SNs, ~45.7h), inbox-watcher 3434697 (Ssl, ~66.9h), outbox_notifier 217608 (Ss, ~25min), dashboard_api 218007 (Ssl, ~25min). Repo HEAD=218a31ec=origin/main (clean, new commit: GC healer). Last sync 23:32:39Z (~37 min). PR #584 open (forge/p4-closeout-outputs, awaiting Mirror, build tasks archived 23:38-23:39Z). 0 stalls. pending=0. Credential rotation: OK. Heartbeat 00:05:31Z (~3.5 min, fresh). Check I fired (Fri UTC gate): week-ending 2026-06-15, $1,135.74 (+9.03%), 1 proposal dedup-skipped. PR #576 verification window closes 2026-06-19T13:35Z; fix holding. Stale bash orphan PID 1834248: CONFIRMED alive (~21.19d+). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=22→23 — steady-state continues.**

## Status snapshot — updated 2026-06-18 23:34Z UTC (Iter ~2226, Tier 3, consecutive_clean=21→22, NOMINAL)

**Iter ~2226 summary:** ✅ Nominal. 0 new alerts (watermark=923=file_length). All 5 daemons alive — beacon 3734671 (Ss, ~45.1h), chain-event 3734305 (SNs, ~45.1h), inbox-watcher 3434697 (Ssl, ~66.3h), dashboard_api 130620 (Ssl, ~6.95h), outbox_notifier 130853 (Ss, ~6.95h). Repo HEAD=3bb474c9=origin/main (clean). PR #583 CONFIRMED MERGED (p4-closeout-author). Last sync 22:32:19Z (~62 min). p4-closeout sequence active: PR #584 open (23:23Z, ~11 min), 2 build tasks in Forge inbox (build-p4-closeout-outputs + build-p4-closeout-ui, dispatched 23:28Z). 0 stalls. pending=0. Credential rotation: OK. Heartbeat 23:05:22Z (~29 min, fresh). Thursday: Check I/III/VIII/IX/X skip. §5.0 all no-ops. PR #576 verification window open (closes 2026-06-19T13:35Z; fix holding). Stale bash orphan PID 1834248: CONFIRMED alive (~21.17d+). PRIME ratio=19.71 (1025 interventions, 52 systemic_fixes, trend=improving). **Tier 3, consecutive_clean=21→22 — steady-state continues.**



