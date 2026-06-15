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

## beacon_telegram_bot.py get-messages MUST NOT run in background (learned iter ~1876)

**Rule:** Never call `beacon_telegram_bot.py get-messages` (or any Telegram long-poll command) with `run_in_background=true`. It spawns a competing getUpdates loop that causes HTTP 409 conflicts with the production bot, disrupting message receipt. For Telegram sweeps (Check 2), use a one-shot method that doesn't open a competing poll — or read from the bot's in-memory message cache / log directly. Calling get-messages in foreground mode from an interactive cycle also risks the same conflict; prefer log-based or state-file-based Telegram checks.

---

## beacon-pending-approvals.json correct structure (corrected iter ~1878)

**Rule:** `beacon-pending-approvals.json` structure is `{"version": 1, "pending": [...], "history": [...]}` — NOT a dict keyed by approval ID. Check for pending items via `d["pending"]` list. Prior parsing (looking for `.items()` with a `status` field) was wrong and returned 0 pending incorrectly. Correct check: `len(d.get("pending", []))`.

---

## Ledger/Check-I Tier-4 pattern (observed 2026-06-15 iter ~1900)

**Rule:** `source=ledger` weekly reports (subject=weekly-YYYY-MM-DD) and `source=pulse` Check I digests (subject=check-i-YYYY-MM-DD) consistently classify as Tier-4 (novel, no template) in the triage helper. These are routine Monday outputs already delivered by the bot via route=escalate. G-rule candidate: add Tier-3 translations for both. **Count: 1/3** — dispatch to Beacon at 3/3.

---

## auto-dispatch-APPROVAL_REQUEST-task-id-mismatch (observed iter ~1910)

**Rule:** When Pulse sends a Check I auto-dispatch envelope, outbox-notifier WARNs `beacon pulse-auto-dispatch APPROVAL_REQUEST task_id mismatch (envelope=pulse-auto-XXXX, marker='<proposal-task-id>'); falling through to default routing`. Dispatch STILL SUCCEEDS via fallback. 6 total occurrences since May 28 (firing at Check I dispatch cadence). Prior iters missed it because Check 1 used `tail -20` (too small). G-rule: **auto-dispatch-APPROVAL_REQUEST-task-id-mismatch-warn-vs-info 1/3**. Dispatch to Beacon at 3/3 for warn-vs-info fix.

---

## Status snapshot — updated 2026-06-15 09:27Z UTC (Iter ~1918, Tier 1)

**Iter ~1918 summary:** ✅ Nominal. **Tier 1, consecutive_clean=0**. All checks clean except Check 4 carry (pending=1). 0 new interventions. ratio≈20.80 (957/46). All agent PIDs healthy. Watermark=989 (no change). Sync fresh (last_sync=09:24:17Z). Bot log last entry idx=988 at 03:12:32Z (no new directives from Larry). 5 open PRs: #497 (REVIEW_ESCALATE carry, 29.4h, under 72h), #509/#510/#512/#513 (cooldowns active).

**heal_pipeline_stall.py --dry-run note:** `--dry-run` does NOT suppress writes to larry-alerts.jsonl for this script. When cooldown expires, the alert fires even in dry-run mode. Be aware: calling --dry-run in a cycle will generate real alerts if the cooldown has passed.

**medic-diagnosis alerts (learned iter ~1905):** The medic module sends `kind=notification, intent=medic-diagnosis` alerts with a chat_id when it performs detailed PR diagnoses. These carry a chat_id meaning the DM was already delivered directly. Triage helper classifies as Tier-4 (no registry template). No second DM from Pulse warranted.

---

## Key standing items (as of iter ~1916)

| Item | Status | Action needed |
|---|---|---|
| PR #497 REVIEW_ESCALATE | [yellow] MERGEABLE (re-verified 09:13Z); reviewDecision=""; Mirror REVIEW_ESCALATE 04:05:31Z Jun-14; age ~30.2h; 72h expires ~Jun-17T04:05Z. | Carry; escalate if still open at Jun-17T04:05Z |
| PR #509 | [yellow] UNKNOWN/no-review; medic DM attempt 10 at 08:58Z Jun-15; cooldown active | Awaiting Larry; merge/close or dispatch Mirror review |
| PRs #510 + #512 + #513 | [yellow] Medic follow-up DMs idx=986/987/988 delivered 03:12Z MDT Jun-15; cooldowns active iter ~1916 | Larry to route to Mirror or merge/close |
| G-rule stall-detector Forge build | [yellow] Beacon spec complete. Forge build pending Larry's dashboard approval. | Approve Forge build via dashboard |
| Check VIII rule=lower | [yellow] FN=3027, TP=5, FP=2 — threshold too high. DM queued iter ~1899. | `approve check-viii-update-2026-06-15` when shortcut lands |
| unreviewed-merge:511 | [yellow] PR #511 merged by Larry at 23:58Z Jun-14 without Mirror routing | Reply 'go: retroactive-review-511' or 'silence: local-review-marker-counts' |
| unreviewed-merge:499 | [yellow] PR #499 merged by Larry without Mirror | Reply 'go: retroactive-review-499' or 'silence: missions-spec-no-mirror-needed' |
| unreviewed-merge:494 | [yellow] DM sent iter ~1694 (01:54Z Jun-14) | Reply 'go: retroactive-review-494' |
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| Check I 2026-06-15 | [blue] 1 proposal dispatched iter ~1899, Beacon processed | Beacon spec in progress |
| Check IX missions | [blue] PR #512 (catch-me-up-gap) + PR #513 (alert-ignored) open, no review yet | Larry review on kanban |
| G-rule ledger/check-i Tier-4 | [blue] **1/3** | Watch; dispatch to Beacon at 3/3 |
| G-rule health-notify-script-missing | [blue] **1/3** | Watch; dispatch at 3/3 |
| catalog-accuracy-drift | [blue] 8/34 ourliberty-graph shelf cards drifted | route=digest; journal-note only |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| G-rule missions-autoregister-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-timeout-worktree-missing-retry-loop | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule missions-card-gc-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule F24-empty-prompt-envelope-rejected | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule stuck-cycle-timer | [blue] **0/3** (healed Jun-15 06:00Z, counter reset) | Watch; dispatch at 3/3 |
| G-rule timer-cycle-no-journal-entry | [blue] **0/3** | Watch |
| G-rule heal-stale-daemon-script_path-cosmetic | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule Forge-preflight-marker-error-retry | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule auto-dispatch-APPROVAL_REQUEST-task-id-mismatch | [blue] **1/3** (new iter ~1910) | Watch; dispatch to Beacon at 3/3 (warn-vs-info) |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| dashboard_api PID 2868353 | [blue] Ssl stable | Note; watch for recurrence |
| Stale bash orphans | [blue] PIDs 1834248 (17d 13h+) + 2605007 (1d 4h+). Ss, low CPU. | Carry |
