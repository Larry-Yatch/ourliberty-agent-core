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

## medic-diagnosis-tier4 G-rule DISPATCHED (iter ~1955)

**Rule:** `source=medic, intent=medic-diagnosis` alerts classify Tier-4 consistently (no registry template). Medic DMs directly via `chat_id` — no second DM from Pulse warranted. **G-rule 3/3 → DISPATCHED** iter ~1955: `g-rule-medic-diagnosis-tier3-translation-001` sent to Beacon inbox — direction-ask to add Tier-3 silence in `config/alert-translations.json`. Watch for Beacon spec + Forge config-only PR.

---

## Ledger/Check-I Tier-4 pattern (observed 2026-06-15 iter ~1900)

**Rule:** `source=ledger` weekly reports (subject=weekly-YYYY-MM-DD) and `source=pulse` Check I digests (subject=check-i-YYYY-MM-DD) consistently classify as Tier-4 (novel, no template) in the triage helper. These are routine Monday outputs already delivered by the bot via route=escalate. G-rule candidate: add Tier-3 translations for both. **Count: 1/3** — dispatch to Beacon at 3/3.

---

## auto-dispatch-APPROVAL_REQUEST-task-id-mismatch (observed iter ~1910)

**Rule:** When Pulse sends a Check I auto-dispatch envelope, outbox-notifier WARNs `beacon pulse-auto-dispatch APPROVAL_REQUEST task_id mismatch (envelope=pulse-auto-XXXX, marker='<proposal-task-id>'); falling through to default routing`. Dispatch STILL SUCCEEDS via fallback. 6 total occurrences since May 28 (firing at Check I dispatch cadence). Prior iters missed it because Check 1 used `tail -20` (too small). G-rule: **auto-dispatch-APPROVAL_REQUEST-task-id-mismatch-warn-vs-info 1/3**. Dispatch to Beacon at 3/3 for warn-vs-info fix.

---

## heal-pipeline-stall:unrouted-pr Tier-4 repeat pattern (observed iter ~1922, DISPATCHED iter ~1930)

**Rule:** `source=heal-pipeline-stall, subject=pipeline-stall:unrouted-pr:PR#N` alerts consistently classify as Tier-4 (no registry template, no translation match) in the triage helper. The healer cycles through cooldowns and fires repeatedly for the same PR. Bot fallback delivery already DMs Larry; a second DM from Pulse is noise. Do NOT send repeat DM if the bot already delivered the prior iteration of the same alert. **G-rule count: 3/3 → DISPATCHED** iter ~1930: `g-rule-healer-unrouted-pr-tier3-translation-001` sent to Beacon inbox — direction-ask to add Tier-3 translation for `source=heal-pipeline-stall, subject=pipeline-stall:unrouted-pr:*` in `config/alert-translations.json`. Watch for Beacon spec + Forge config-only PR.

---

## catalog-accuracy-drift Tier-4 pattern (observed iter ~1926)

**Rule:** `source=pulse-check, subject=catalog-accuracy-drift` alerts classify as Tier-4 (novel, no registry template) in triage helper. Alert carries `route=digest` — bot delivers as digest, no DM. Do NOT send second DM from Pulse. Journal-note only. Current count: 9/34 ourliberty-graph shelf cards drifted. **G-rule count: 1/3** — dispatch to Beacon at 3/3 for alert-translations.json Tier-3 template.

---

## Status snapshot — updated 2026-06-15 17:23Z UTC (Iter ~1967, Tier 1, consecutive_clean=2)

**Iter ~1967 summary:** ✅ Nominal. L1028 Tier-3 silenced (review-pass PR #515). PR #515 Mirror PASS received — auto-merge HELD behind PR #516 (config/alert-translations.json overlap; system retries automatically when #516 merges). 0 new directives. Watermark: 1027→1028. pending=0. Tier 1, consecutive_clean=2. Daemons all alive. No new G-rule advances. PR #497 REVIEW_ESCALATE age=37.3h (deadline Jun-17T04:05Z).

**Iter ~1966 summary:** ✅ Nominal. All checks clean. 0 new alerts. No new directives. Watermark: 1027 (unchanged). pending=0. Tier 1, consecutive_clean=1. Daemons all alive. No new G-rule advances. PR #516 (alert-translation-unrouted-pr-001) in Mirror review; PR #515 (medic-diagnosis) in Mirror review; PR #497 REVIEW_ESCALATE age=37.1h (deadline Jun-17T04:05Z).

**Iter ~1965 summary:** ⚠️ Check 0: L1026-1027 new (unreviewed-merge:510+509, Tier-4, bot-delivered, no Pulse DM). Check E: PR #516 new (alert-translation-unrouted-pr-001, awaiting Mirror). All other checks nominal. Watermark: 1025→1027. pending=0. Tier 1, consecutive_clean=0. Daemons: beacon:2744840, chain-event-shipper:2744551, outbox-notifier:2744914, dashboard-api:2868353, inbox-watcher:2530123. PRIME ratio=20.77 (improving).

**Iter ~1964 summary (prior):** ⚠️ Check A always-fix: repo behind origin/main → fast-forward to 9e0182ea (PRs #514 #509 #510 merged). L1025: review-pass for PR #514 Tier-3 silenced. Watermark: 1024→1025. pending=0 (all 4 cleared). PRs #509+#510 auto-merged after Larry's "Route to mirror" directive. PR #515 new (medic-diagnosis-tier3-silence-001, awaiting Mirror review). Tier 1, consecutive_clean=0.

**Iter ~1963 summary (prior-2):** ⚠️ 7 new alerts (L1018-L1024). L1018-1020: pipeline-stall PRs #510/512/513 Tier-4, bot delivered. L1021: missions-autoregister Tier-3 silenced. L1022-1024: medic-diagnosis Tier-4, chat_id delivered. Watermark: 1017→1024. Check 2: Larry "Go" at 10:41MDT → dispatch FAILED for autoregister-warn-demote-001 (self-dispatch denied beacon→beacon). G-rule telegram-approval-self-dispatch-denied **1/3 NEW**. Tier 1, consecutive_clean=0.

**Iter ~1960 summary (prior-2):** ✅ Nominal. All checks clean. 0 new alerts. Tier 1 → Tier 2 de-escalation (consecutive_clean 2→3).

**heal_pipeline_stall.py --dry-run note:** `--dry-run` does NOT suppress writes to larry-alerts.jsonl. When cooldown expires, the alert fires in dry-run mode. Be aware: calling --dry-run in a cycle will write real alerts if the cooldown has passed. Always check wc -l of the file before and after.

**medic-diagnosis alerts (learned iter ~1905):** The medic module sends `kind=notification, intent=medic-diagnosis` alerts with a chat_id when it performs detailed PR diagnoses. These carry a chat_id meaning the DM was already delivered directly. Triage helper classifies as Tier-4 (no registry template). No second DM from Pulse warranted.

**Watermark gap (closed iter ~1936):** was watermark=989 >> file=978; advanced to 986 = file length. Gap closed. Standard get-watermark path works again. If gap re-forms: check `wc -l` vs watermark each iter; manually read new tail lines and triage if file < watermark. Do NOT set watermark backward.

---

## telegram-approval-self-dispatch-denied G-rule (observed iter ~1963)

**Rule:** When Larry replies "Go" (or similar approval shortcut) in Telegram for a Beacon-authored APPROVAL_REQUEST plan, the bot attempts to dispatch the plan back to Beacon (its own source), resulting in "self-dispatch denied (beacon → beacon)". The approval is NOT processed. The plan stays pending in beacon-pending-approvals.json. Recovery: Larry must re-approve explicitly via Telegram or dashboard. **G-rule count: 1/3** — dispatch to Beacon at 3/3 for a routing fix in the bot's approval handler.

---

## Key standing items (as of iter ~1965)

| Item | Status | Action needed |
|---|---|---|
| PR #516 | [yellow] `forge/alert-translation-unrouted-pr-001` — config PR for unrouted-pr Tier-3 translation. Awaiting Mirror review (0.3h old iter ~1967). | Watch; should auto-merge on Mirror PASS → unblocks #515 auto-merge |
| PR #515 | [blue] `forge/medic-diagnosis-tier3-silence-001` — **Mirror PASS received** (L1028, iter ~1967). MERGEABLE. Auto-merge HELD behind #516 (overlap: config/alert-translations.json). Will retry when #516 merges. | Watch for merge outcome; may need Forge rebase if conflict |
| PR #497 REVIEW_ESCALATE | [yellow] UNKNOWN mergeable; reviewDecision=""; Mirror REVIEW_ESCALATE 04:05:31Z Jun-14; age ~38h; 72h expires ~Jun-17T04:05Z (~34h remaining). | Escalate if still open at Jun-17T04:05Z |
| PRs #512/#513 pipeline-stall | [yellow] Cooldowns suppressed iters ~1964-1965. | Carry |
| unreviewed-merge:511/499/494/489/510/509 | [yellow] PRs merged by Larry without Mirror; bot-delivered; Larry's judgment call. | Reply appropriate shortcut or silence |
| G-rule stall-detector Forge build | [yellow] Beacon spec complete. Forge build pending Larry's dashboard approval. | Approve Forge build via dashboard |
| Check VIII rule=lower | [yellow] FN=3027, TP=5, FP=2 — threshold too high. | `approve check-viii-update-2026-06-15` when shortcut lands |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| Telegram 409 burst | [yellow] G-rule **2/3**. All self-inflicted by calling get-messages. | Watch; dispatch at 3/3 |
| G-rule telegram-approval-self-dispatch-denied | [yellow] **1/3** — Larry "Go" → dispatch failed (self-dispatch denied beacon→beacon). | Watch; dispatch to Beacon at 3/3 for bot routing fix |
| G-rule missions-autoregister-warn-vs-info | [blue] **COMPLETE ✅** iter ~1964. PR #514 merged 16:54:35Z Jun-15. | Done. |
| Check I 2026-06-15 | [blue] 1 proposal dispatched iter ~1899, Beacon processed | Beacon spec in progress |
| Check IX missions | [blue] PR #512 (catch-me-up-gap) + PR #513 (alert-ignored) open | Larry review on kanban |
| G-rule medic-diagnosis-tier4 | [blue] **DISPATCHED** iter ~1955; PR #515 built, Mirror review in progress. | Watch for Mirror PASS → auto-merge |
| G-rule catalog-accuracy-drift-tier4 | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule ledger/check-i Tier-4 | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule health-notify-script-missing | [blue] **1/3** | Watch; dispatch at 3/3 |
| catalog-accuracy-drift | [blue] 9/34 ourliberty-graph shelf cards drifted | route=digest; journal-note only |
| G-rule Forge-timeout-worktree-missing-retry-loop | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule missions-card-gc-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule F24-empty-prompt-envelope-rejected | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-preflight-marker-error-retry | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-preflight-CLARIFY_REQUEST | [blue] **1/3** | Watch; dispatch at 3/3 |
| G-rule auto-dispatch-APPROVAL_REQUEST-task-id-mismatch | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 (warn-vs-info) |
| G-rule telegram-409-burst | [yellow] **2/3** | Watch; dispatch at 3/3 |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| Stale bash orphans | [blue] PIDs 1834248 (17d+) + 2605007 (1d+). Ss, low CPU. | Carry |
