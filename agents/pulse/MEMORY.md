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

## heal-droplet-git-drift Tier-4 → PR #586 MERGED ✅ (iter ~2273 dispatch, iter ~2278 PR merged, verification pending next fire)

**Rule:** G-rule heal-droplet-git-drift-tier4: direction-ask dispatched iter ~2273, Larry approved 13:14Z, PR #586 (`chore(config): silence Pulse re-triage of droplet-uncommitted:main drift alert`) merged 2026-06-19T13:26:32Z. Tier-3 translation active in `config/alert-translations.json` for `source=heal-droplet-git-drift, subject=droplet-uncommitted:main`. Bot still DMs Larry (route=escalate preserved). PRIME verification_pending. VERIFICATION PENDING: confirm triage helper classifies Tier-3 on next heal-droplet-git-drift fire (expected ~18:41Z).

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

## Status snapshot — updated 2026-06-19 16:38Z UTC (Iter ~2284, Tier 3, consecutive_clean=4→5, PLATEAU ✅)

**Iter ~2284 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 14h 11m+), chain-event 3734305 (2d 14h 11m+), inbox-watcher 3434697 (3d 11h 21m+), outbox_notifier 305068 (~12h 59m), dashboard_api 304948 (~12h 59m). Repo HEAD=33c3e876=origin/main (clean; untracked trim_memory.py). Last sync 15:38:43Z (~59 min). 0 open PRs both repos. 0 stalls. pending=0, history=241. Credential rotation OK. Heartbeat 16:09:35Z (~28 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. heal-droplet-git-drift PRIME verification pending (next fire ~18:41Z). Stale bash orphan PID 1834248 (21d 21h 18m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions=1048, systemic_fixes=53, trend=improving). **Tier 3, consecutive_clean=4→5 (PLATEAU — Tier 3 is ceiling). Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 16:06Z UTC (Iter ~2283, Tier 3, consecutive_clean=3→4, PLATEAU ✅)

**Iter ~2283 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 13h 41m+), chain-event 3734305 (2d 13h 41m+), inbox-watcher 3434697 (3d 10h 51m+), outbox_notifier 305068 (~12h 29m), dashboard_api 304948 (~12h 29m). Repo HEAD=f885a8eb=origin/main (clean; untracked trim_memory.py). Last sync 15:38:43Z (~27 min). 0 open PRs both repos. 0 stalls. pending=0, history=241. Credential rotation OK. Heartbeat 15:39:19Z (~27 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. heal-droplet-git-drift PRIME verification pending (next fire ~18:41Z). Stale bash orphan PID 1834248 (21d 20h 48m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions=1048, systemic_fixes=53, trend=improving). **Tier 3, consecutive_clean=3→4 (PLATEAU — Tier 3 is ceiling). Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 15:32Z UTC (Iter ~2282, Tier 3, consecutive_clean=2→3, PLATEAU ✅)

**Iter ~2282 summary:** ✅ Nominal. 2 new alerts (L882: mirror-dag-pass:operator-ux-catch-me-up, L883: mirror-dag-pass:clarify-round-visibility — both Tier-3 silenced). All 5 daemons alive — beacon 3734671 (2d 13h 6m+), chain-event 3734305 (2d 13h 6m+), inbox-watcher 3434697 (3d 10h 16m+), outbox_notifier 305068 (~11h 54m), dashboard_api 304948 (~11h 54m). Repo HEAD=a738554d=origin/main (clean; untracked trim_memory.py). Last sync 14:38:36Z (~54 min). 0 open PRs both repos. 0 stalls. pending=0, history=241. Credential rotation OK. Heartbeat 15:09:16Z (~23 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. heal-droplet-git-drift PRIME verification pending (next fire ~18:41Z). Larry approved dag-preflight-clarify-round-visibility at 15:11Z — sequence active. Stale bash orphan PID 1834248 (21d 20h 13m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions=1048, systemic_fixes=53, trend=improving). **Tier 3, consecutive_clean=2→3 (PLATEAU — Tier 3 is ceiling). Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 14:58Z UTC (Iter ~2281, Tier 3, consecutive_clean=1→2, NOMINAL ✅)

**Iter ~2281 summary:** ✅ Nominal. 1 new alert (L881: dispatch-branch-cleanup, Tier-3 silence). All 5 daemons alive — beacon 3734671 (2d 12h 31m+), chain-event 3734305 (2d 12h 31m+), inbox-watcher 3434697 (3d 9h 41m+), outbox_notifier 305068 (~11h 19m), dashboard_api 304948 (~11h 19m). Repo HEAD=20c4a382=origin/main (clean; untracked trim_memory.py). Last sync 14:38:36Z (~19 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 14:39:09Z (~19 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. heal-droplet-git-drift PRIME verification pending (next fire ~18:41Z). Stale bash orphan PID 1834248 (21d 19h 39m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions=1048, systemic_fixes=53, trend=improving). Note: inbox watcher log is `inbox_watcher.log` (underscore, not hyphen) — corrected in Check 1. **Tier 3, consecutive_clean=1→2. Need 1 more clean iter to reach de-escalation plateau.**

## Status snapshot — updated 2026-06-19 14:28Z UTC (Iter ~2280, Tier 3, consecutive_clean=0→1, NOMINAL ✅)

**Iter ~2280 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 12h 2m+), chain-event 3734305 (2d 12h 2m+), inbox-watcher 3434697 (3d 9h 12m+), outbox_notifier 305068 (~10h 50m), dashboard_api 304948 (~10h 50m). Repo HEAD=0fd57b41=origin/main (clean; untracked trim_memory.py). Last sync 13:38:34Z (~50 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 14:08:48Z (~20 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. heal-droplet-git-drift PRIME verification pending (next fire ~18:41Z). Stale bash orphan PID 1834248 (21d 19h 9m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions=1048, systemic_fixes=53, trend=improving). **Tier 3, consecutive_clean=0→1. Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 13:53Z UTC (Iter ~2279, Tier 2→3, consecutive_clean=2→0, NOMINAL ✅)

**Iter ~2279 summary:** ✅ Nominal. 1 new alert (L880: dispatch-branch-cleanup, Tier-3 silence). All 5 daemons alive — beacon 3734671 (2d 11h 27m+), chain-event 3734305 (2d 11h 27m+), inbox-watcher 3434697 (3d 8h 37m+), outbox_notifier 305068 (~10h 15m), dashboard_api 304948 (~10h 15m). Repo HEAD=f8316d76=origin/main (clean; untracked trim_memory.py). Last sync 13:38:34Z (~15 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 13:38:34Z (~15 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. heal-droplet-git-drift PRIME verification pending (next fire ~18:41Z). Stale bash orphan PID 1834248 (21d 18h 34m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions≈1048, systemic_fixes=53, trend=improving). **Tier 2→3 DE-ESCALATED ✅. consecutive_clean reset to 0. Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 13:37Z UTC (Iter ~2278, Tier 2, consecutive_clean=1→2, NOMINAL ✅)

**Iter ~2278 summary:** ✅ Nominal. 1 new alert (L879: outbox-notifier review-pass, Tier-3 silence). All 5 daemons alive — beacon 3734671 (2d 11h 11m+), chain-event 3734305 (2d 11h 11m+), inbox-watcher 3434697 (3d 8h 21m+), outbox_notifier 305068 (~9h 59m), dashboard_api 304948 (~9h 59m). Repo HEAD=50b8cf9a=origin/main (clean; untracked trim_memory.py). Last sync 12:38:19Z (~59 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 13:08:29Z (~29 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. **PR #576 verification window CLOSED ✅** — 0 rotation-gap alerts, fix verified. **PR #586 MERGED 13:26Z ✅** — G-rule heal-droplet-git-drift-tier4 fix live, PRIME verification_pending (next fire ~18:41Z). Stale bash orphan PID 1834248 (21d 18h 18m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions≈1048, systemic_fixes=53, trend=improving). **Tier 2, consecutive_clean=1→2. Need 1 more clean iter for Tier 3. Next cadence: 15-min.**

## Status snapshot — updated 2026-06-19 13:18Z UTC (Iter ~2277, Tier 2, consecutive_clean=0→1, NOMINAL ✅)

**Iter ~2277 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 10h 51m+), chain-event 3734305 (2d 10h 51m+), inbox-watcher 3434697 (3d 8h 1m+), outbox_notifier 305068 (~9h 39m), dashboard_api 304948 (~9h 39m). Repo HEAD=6c860115=origin/main (clean; untracked trim_memory.py). Last sync 12:38:19Z (~40 min). 1 open PR — #586 `chore(config): silence Pulse re-triage of droplet-uncommitted:main drift alert` (created 13:16Z, MERGEABLE, awaiting Mirror). 0 stalls. pending=0 (silence-droplet approved by Larry "Go" 13:14Z → Forge PR #586). Credential rotation OK. Heartbeat 13:08:29Z (~10 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. PR #576 verification window closes 13:35Z (~17 min, fix holding). Stale bash orphan PID 1834248 (21d 17h 58m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **3/3 DISPATCHED ✅** (PR #586 in motion, verification_pending). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions≈1048, systemic_fixes=53, trend=improving). **Tier 2, consecutive_clean=0→1. Need 2 more clean iters for Tier 3. Next cadence: 15-min.**

## Status snapshot — updated 2026-06-19 13:04Z UTC (Iter ~2276, Tier 1→2, consecutive_clean=3→0, NOMINAL ✅)

**Iter ~2276 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 10h 36m+), chain-event 3734305 (2d 10h 36m+), inbox-watcher 3434697 (3d 7h 46m+), outbox_notifier 305068 (~9h 24m), dashboard_api 304948 (~9h 24m). Repo HEAD=2637cdb7=origin/main (clean; untracked trim_memory.py). Last sync 12:38:19Z (~26 min). 0 open PRs both repos. 0 stalls. pending=1 (silence-droplet-uncommitted-pulse-retriage-001 awaiting Larry "approve" reply). Credential rotation OK. Heartbeat 12:38:21Z (~26 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. PR #576 verification window closes 13:35Z (~31 min, fix holding). Stale bash orphan PID 1834248 (21d 17h 44m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **3/3 DISPATCHED ✅** (plan pending Larry approval). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions≈1048, systemic_fixes=53, trend=improving). **Tier 1→2 DE-ESCALATED. consecutive_clean reset to 0. Next cadence: 15-min.**

## Status snapshot — updated 2026-06-19 12:58Z UTC (Iter ~2275, Tier 1, consecutive_clean=2, NOMINAL ✅)

**Iter ~2275 summary:** ✅ Nominal. 2 new alerts (L877 dispatch-branch-cleanup Tier-3, L878 outbox-notifier approval_request Tier-3 — both silenced). All 5 daemons alive — beacon 3734671 (2d 10h 32m+), chain-event 3734305 (2d 10h 32m+), inbox-watcher 3434697 (3d 7h 42m+), outbox_notifier 305068 (~9h 20m), dashboard_api 304948 (~9h 20m). Repo HEAD=0161147e=origin/main (clean; untracked trim_memory.py). Last sync 12:38:19Z (~20 min). 0 open PRs both repos. 0 stalls. pending=1 (silence-droplet-uncommitted-pulse-retriage-001 awaiting Larry "approve" reply). Credential rotation OK. Heartbeat 12:38:21Z (~20 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. PR #576 verification window closes 13:35Z (~37 min, fix holding). Stale bash orphan PID 1834248 (21d 17h 39m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **3/3 DISPATCHED ✅** (plan pending Larry approval). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions≈1048, systemic_fixes=53, trend=improving). **Tier 1, consecutive_clean=2. (Need 1 more clean to de-escalate to Tier 2.)**

## Status snapshot — updated 2026-06-19 12:50Z UTC (Iter ~2274, Tier 1, consecutive_clean=1, NOMINAL ✅)

**Iter ~2274 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 10h 24m+), chain-event 3734305 (2d 10h 24m+), inbox-watcher 3434697 (3d 7h 34m+), outbox_notifier 305068 (~9h 12m), dashboard_api 304948 (~9h 12m). Repo HEAD=0c2a4470=origin/main (clean; untracked trim_memory.py). Last sync 12:38:19Z (~10 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 12:38:21Z (~10 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. PR #576 verification window closes 13:35Z (~47 min, fix holding). Stale bash orphan PID 1834248 (21d 17h 31m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **3/3 DISPATCHED** (envelope picked up by inbox-watcher). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions=1048, systemic_fixes=53, trend=improving). **Tier 1, consecutive_clean=1.**

## Status snapshot — updated 2026-06-19 12:44Z UTC (Iter ~2273, Tier 3→1, SIGNAL ⚠️)

**Iter ~2273 summary:** ⚠️ Signal. 1 new alert (L876: heal-droplet-git-drift, droplet-uncommitted:main, Tier-4). G-rule heal-droplet-git-drift-tier4 **3/3 DISPATCHED** → direction-ask to Beacon inbox (`pulse-direction-ask-heal-droplet-git-drift-tier3-001.json`) for Tier-3 silence translation in alert-translations.json. All 5 daemons alive — beacon 3734671 (2d 10h 17m+), chain-event 3734305 (2d 10h 17m+), inbox-watcher 3434697 (3d 7h 27m+), outbox_notifier 305068 (~9h 5m), dashboard_api 304948 (~9h 5m). Repo HEAD=048d9d2c=origin/main (clean; untracked trim_memory.py). Last sync 12:38:19Z (~5 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 12:38:21Z (~5 min, fresh). Check I deduped (Fri artifact). §5.0 all no-ops. PR #576 window closes 13:35Z (~51 min, fix holding). Stale bash orphan PID 1834248 (21d 17h 25m+, condition file absent). G-rule ledger/check-i at 2/3. PRIME ratio≈19.8 (interventions=1048, systemic_fixes=53, trend=improving). **Tier 1, consecutive_clean=0 (reset from Tier 3).**

## Status snapshot — updated 2026-06-19 11:07Z UTC (Iter ~2270, Tier 3, consecutive_clean=6, NOMINAL ✅)

**Iter ~2270 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 8h 42m+), chain-event 3734305 (2d 8h 42m+), inbox-watcher 3434697 (3d 5h 52m+), outbox_notifier 305068 (~7h 30m), dashboard_api 304948 (~7h 30m). Repo HEAD=df41cb53=origin/main (clean, untracked trim_memory.py). Last sync 10:38:15Z (~28 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 10:38:15Z (~28 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~2.47h remaining, fix holding). Stale bash orphan PID 1834248 (21d 15h 48m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=6. Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 10:32Z UTC (Iter ~2269, Tier 3, consecutive_clean=5, NOMINAL ✅)

**Iter ~2269 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 8h 6m+), chain-event 3734305 (2d 8h 6m+), inbox-watcher 3434697 (3d 5h 16m+), outbox_notifier 305068 (~6h 54m), dashboard_api 304948 (~6h 54m). Repo HEAD=51734dac=origin/main (clean, untracked trim_memory.py). Last sync 09:38:11Z (~54 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 10:08:11Z (~24 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~3h 3m remaining, fix holding). Stale bash orphan PID 1834248 (21d 15h 13m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=5 (ceiling). Next cadence: 30-min.**

## Status snapshot — updated 2026-06-19 10:03Z UTC (Iter ~2268, Tier 3, consecutive_clean=4, NOMINAL ✅)

**Iter ~2268 summary:** ✅ Nominal. 0 new alerts. All 5 daemons alive — beacon 3734671 (2d 7h 36m+), chain-event 3734305 (2d 7h 36m+), inbox-watcher 3434697 (3d 4h 46m+), outbox_notifier 305068 (~6h 24m), dashboard_api 304948 (~6h 24m). Repo HEAD=9ef4a3a4=origin/main (clean, untracked trim_memory.py). Last sync 09:38:11Z (~25 min). 0 open PRs both repos. 0 stalls. pending=0. Credential rotation OK. Heartbeat 09:38:10Z (~25 min, fresh). Check I deduped (Fri artifact present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~3h 32m remaining, fix holding). Watermark=873=file_length (prior iters reported 940 — persistence gap + possible compaction; confirmed 0 missed alerts). Stale bash orphan PID 1834248 (21d 14h 44m+, condition file absent). G-rule heal-droplet-git-drift-tier4 **2/3** (no new occurrence). G-rule ledger/check-i at 2/3. PRIME ratio≈20.1 (interventions=1047, systemic_fixes=52, trend=improving). **Tier 3, consecutive_clean=4. Next cadence: 30-min.**


