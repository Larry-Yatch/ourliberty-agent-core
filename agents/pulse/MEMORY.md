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

## Status snapshot — updated 2026-06-19 10:32Z UTC (Iter ~2269, Tier 3, consecutive_clean=5, NOMINAL ✅)

**Iter ~2269 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 8h 6m+), chain-event 3734305 (2d 8h 6m+), inbox-watcher 3434697 (3d 5h 16m+), outbox_notifier 305068 (~6h 54m), dashboard_api 304948 (~6h 54m). Repo HEAD=51734dac=origin/main (clean, untracked trim_memory.py). Last sync 09:38:11Z (~54 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 10:08:11Z (~24 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~3h 3m remaining, fix holding). Stale bash orphan PID 1834248 (21d 15h 13m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=5 (ceiling). Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 10:03Z UTC (Iter ~2268, Tier 3, consecutive_clean=4, NOMINAL ✅)

**Iter ~2268 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 7h 36m+), chain-event 3734305 (2d 7h 36m+), inbox-watcher 3434697 (3d 4h 46m+), outbox_notifier 305068 (~6h 24m), dashboard_api 304948 (~6h 24m). Repo HEAD=9ef4a3a4=origin/main (clean, untracked trim_memory.py). Last sync 09:38:11Z (~25 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 09:38:10Z (~25 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~3h 32m remaining, fix holding). Watermark=873=file_length (prior iters reported 940 — persistence gap + possible compaction; confirmed 0 missed alerts). Stale bash orphan PID 1834248 (21d 14h 44m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=4. Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 09:32Z UTC (Iter ~2267, Tier 3, consecutive_clean=3, NOMINAL ✅)

**Iter ~2267 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 7h 6m+), chain-event 3734305 (2d 7h 6m+), inbox-watcher 3434697 (3d 4h 16m+), outbox_notifier 305068 (~5h 54m), dashboard_api 304948 (~5h 54m). Repo HEAD=11b68597=origin/main (clean, untracked trim_memory.py). Last sync 08:38:10Z (~54 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 09:08:10Z (~24 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~4h 3m remaining, fix holding). Stale bash orphan PID 1834248 (21d 14h 13m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=3. Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 08:58Z UTC (Iter ~2266, Tier 3, consecutive_clean=2, NOMINAL ✅)

**Iter ~2266 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 6h 32m+), chain-event 3734305 (2d 6h 32m+), inbox-watcher 3434697 (3d 3h 42m+), outbox_notifier 305068 (~5h 20m), dashboard_api 304948 (~5h 20m). Repo HEAD=781aff99=origin/main (clean, untracked trim_memory.py). Last sync 08:38:10Z (~20 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 08:38:10Z (~20 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~4h 37m remaining, fix holding). PR #581 merged 2026-06-18T15:39Z (one-tick grace for uncommitted-changes — may reduce heal-droplet-git-drift). Stale bash orphan PID 1834248 (21d 13h 39m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=2. Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 08:22Z UTC (Iter ~2265, Tier 3, consecutive_clean=1, NOMINAL ✅)

**Iter ~2265 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 5h 57m+), chain-event 3734305 (2d 5h 57m+), inbox-watcher 3434697 (3d 3h 7m+), outbox_notifier 305068 (~4h 45m), dashboard_api 304948 (~4h 45m). Repo HEAD=e3bf6155=origin/main (clean, untracked trim_memory.py). Last sync 07:37:59Z (~44 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 08:08:09Z (~13 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~5h 13m remaining, fix holding). Stale bash orphan PID 1834248 (21d 13h 3m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=1. Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 07:46Z UTC (Iter ~2264, Tier 2→3 de-escalation, consecutive_clean=0, NOMINAL ✅)

**Iter ~2264 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 5h 24m+), chain-event 3734305 (2d 5h 24m+), inbox-watcher 3434697 (3d 2h 32m+), outbox_notifier 305068 (~4h 10m), dashboard_api 304948 (~4h 10m). Repo HEAD=290c6a79=origin/main (clean, untracked trim_memory.py). Last sync 07:37:59Z (~8 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 07:37:59Z (~8 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~5h 49m remaining, fix holding). Stale bash orphan PID 1834248 (21d 12h 28m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 2→3 de-escalation (consecutive_clean=3 hit threshold; reset to 0). Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 07:27Z UTC (Iter ~2263, Tier 2, consecutive_clean=2, NOMINAL ✅)

**Iter ~2263 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 5h+), chain-event 3734305 (2d 5h+), inbox-watcher 3434697 (3d 2h+), outbox_notifier 305068 (~3h 50m), dashboard_api 304948 (~3h 50m). Repo HEAD=e260680e=origin/main (clean, untracked trim_memory.py). Last sync 06:37:49Z (~49 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 07:07:51Z (~19 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~6h 8m remaining, fix holding). Stale bash orphan PID 1834248 (21d 12h 8m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 2, consecutive_clean=2 — de-escalation ladder: 1 more clean iter to Tier 3. Next cadence: 15-min.**

## Status snapshot — updated 2026-06-19 07:11Z UTC (Iter ~2262, Tier 2, consecutive_clean=1, NOMINAL ✅)

**Iter ~2262 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 4h+), chain-event 3734305 (2d 4h+), inbox-watcher 3434697 (3d 1h+), outbox_notifier 305068 (~3h 34m), dashboard_api 304948 (~3h 34m). Repo HEAD=42847e2c=origin/main (clean, untracked trim_memory.py). Last sync 06:37:49Z (~32.9 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 07:07:51Z (~3.5 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~6h 24m remaining, fix holding). Stale bash orphan PID 1834248 (21d 11h 52m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 2, consecutive_clean=1 — de-escalation ladder: 2 more clean iters to Tier 3. Next cadence: 15-min.**

## Status snapshot — updated 2026-06-19 06:57Z UTC (Iter ~2261, Tier 1→2 de-escalation, consecutive_clean=3→0, NOMINAL ✅)

**Iter ~2261 summary:** ✅ Nominal. 1 new alert (L940: dispatch-branch-cleanup/summary — Tier-3 silenced). All 5 daemons alive — beacon 3734671 (2d 4h+), chain-event 3734305 (2d 4h+), inbox-watcher 3434697 (3d 1h+), outbox_notifier 305068 (~3h 19m), dashboard_api 304948 (~3h 19m). Repo HEAD=51baef61=origin/main (clean, untracked trim_memory.py). Last sync 06:37:49Z (~19 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 06:37:19Z (~19 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~6h 38m remaining, fix holding). Stale bash orphan PID 1834248 (21d 11h 38m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 1→2 de-escalation (consecutive_clean=3 hit threshold). Next cadence: 15-min.**

## Status snapshot — updated 2026-06-19 06:47Z UTC (Iter ~2260, Tier 1, consecutive_clean=2, NOMINAL ✅)

**Iter ~2260 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 4h+), chain-event 3734305 (2d 4h+), inbox-watcher 3434697 (3d 1h+), outbox_notifier 305068 (~3h 10m), dashboard_api 304948 (~3h 10m). Repo HEAD=79aae545=origin/main (clean, untracked trim_memory.py). Last sync 06:37:49Z (~8 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 06:37:19Z (~8 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~6h 49m remaining, fix holding). Stale bash orphan PID 1834248 (21d 11h 28m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 1, consecutive_clean=2 — de-escalation ladder: 1 more clean iter to Tier 2.**

