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

## Status snapshot — updated 2026-06-19 02:58Z UTC (Iter ~2246, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 fix IN-PROGRESS)

**Iter ~2246 summary:** ⚠️ PR #584 (p4-closeout-outputs) fix now IN-PROGRESS. Larry said "go" at 02:50Z → Beacon dispatched → `build-fix-p4-closeout-outputs-revisions-001.json` in Forge inbox. PR now `mergeable=MERGEABLE` (recovered from UNKNOWN). Mirror-review FAILURE expected until fix lands. Routing gap from iter ~2228 RESOLVED. 1 new alert (L932=dispatch-branch-cleanup/summary, Tier-3 silence, watermark 931→932). All 5 daemons alive — beacon 3734671 (~48.5h), chain-event 3734305 (~48.5h), inbox-watcher 3434697 (~69.7h), outbox_notifier 217608 (~3.36h), dashboard_api 218007 (~3.35h). Repo HEAD=0213c11e=origin/main (clean, untracked trim_memory.py). Last sync 02:33:15Z (~24 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 02:36:18Z (~21 min, fresh). Check I deduped (Fri gate). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~10.6h remaining, fix holding). Stale bash orphan PID 1834248 (~21.3d+). G-rule ledger/check-i at 2/3. PRIME ratio≈20.08 (1044/52, trend=improving). **Tier 1, consecutive_clean=0 — fix in-progress, watch.**

## Status snapshot — updated 2026-06-19 02:47Z UTC (Iter ~2245, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2245 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 routing gap confirmed (no envelope found anywhere in /agents/). mergeable=UNKNOWN (transient GitHub re-computation oscillation; no code conflict). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=931=file_length). All 5 daemons alive — beacon 3734671 (~48.4h), chain-event 3734305 (~48.4h), inbox-watcher 3434697 (~69.5h), outbox_notifier 217608 (~3.18h), dashboard_api 218007 (~3.18h). Repo HEAD=f1ab58f4=origin/main (clean, untracked trim_memory.py). Last sync 02:33:15Z (~14 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 02:36:18Z (~10 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~10.8h remaining, fix holding). Stale bash orphan PID 1834248 (~21.31d+). G-rule ledger/check-i at 2/3. PRIME ratio≈20.06 (1043/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 02:43Z UTC (Iter ~2244, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2244 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 routing gap confirmed (no envelope found anywhere in /agents/). mergeable=UNKNOWN (transient GitHub re-computation oscillation; no new conflict). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=931=file_length). All 5 daemons alive — beacon 3734671 (~48.3h), chain-event 3734305 (~48.3h), inbox-watcher 3434697 (~69.5h), outbox_notifier 217608 (~3.10h), dashboard_api 218007 (~3.10h). Repo HEAD=ae974c16=origin/main (clean, untracked trim_memory.py). Last sync 02:33:15Z (~10 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 02:36:18Z (~7 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~10.9h remaining, fix holding). Stale bash orphan PID 1834248 (~21.30d+). G-rule ledger/check-i at 2/3. PRIME ratio≈20.04 (1042/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 02:37Z UTC (Iter ~2243, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2243 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 routing gap confirmed (no envelope anywhere in /agents/). mergeable=UNKNOWN (GitHub oscillation, no code conflict). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=931=file_length). All 5 daemons alive — beacon 3734671 (~48.2h), chain-event 3734305 (~48.2h), inbox-watcher 3434697 (~69.4h), outbox_notifier 217608 (~3.01h), dashboard_api 218007 (~3.01h). Repo HEAD=fa0a8f65=origin/main (clean, untracked trim_memory.py). Last sync 02:33:15Z (~4 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 02:36:18Z (<1 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11h remaining, fix holding). Stale bash orphan PID 1834248 (~21.30d+). G-rule ledger/check-i at 2/3. PRIME ratio=20.0 (1040/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

## Status snapshot — updated 2026-06-19 02:28Z UTC (Iter ~2242, Tier 1, consecutive_clean=0, NON-NOMINAL — PR #584 stall continues)

**Iter ~2242 summary:** ⚠️ Stall continues. PR #584 (p4-closeout-outputs) mirror-review FAILURE ongoing; fix-p4-closeout-outputs-revisions-001 routing gap confirmed (no envelope found anywhere in /agents/). mergeable=MERGEABLE (recovered from UNKNOWN oscillation; no code conflict). [yellow] DM active from iter ~2228; Larry no response. 0 new alerts (watermark=931=file_length). All 5 daemons alive — beacon 3734671 (~48.1h), chain-event 3734305 (~48.1h), inbox-watcher 3434697 (~69.3h), outbox_notifier 217608 (~2.92h), dashboard_api 218007 (~2.91h). Repo HEAD=b3e7e779=origin/main (clean, untracked trim_memory.py). Last sync 01:33:04Z (~55 min). 0 stalls. pending=0. Credential rotation OK. Heartbeat 02:06:15Z (~21 min, fresh). Check I deduped (Fri gate, check-i-2026-06-19.json present). §5.0 all no-ops. PR #576 verification window closes 2026-06-19T13:35Z (~11.1h remaining, fix holding). Stale bash orphan PID 1834248 (~21.30d+). G-rule ledger/check-i at 2/3. PRIME ratio=20.0 (1040/52, trend=improving). **Tier 1, consecutive_clean=0 — stall ongoing.**

