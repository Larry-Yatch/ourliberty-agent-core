# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

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

## Status snapshot — updated 2026-06-15 06:57Z UTC (Iter ~1898, Tier 1)

**Iter ~1898 summary:** ⚠️ Signal. **Tier 1 (unchanged)**. Check 0: 1 new Tier-4 alert (line 967 — medic notification, source=medic, kind=notification, intent=medic-diagnosis, PR#509 attempt 8). Bot already delivered (idx=966, 06:51:09 UTC). No additional Pulse DM. Watermark=967. All daemons alive (PIDs 2530123/2744551/2744840/2744914/2868353). PRs #497/#509/#510 carry [yellow]. ratio≈20.5 (944 interventions / 46 systemic_fixes). Pattern note: medic notifications now flowing into larry-alerts.jsonl as Tier-4 — propose translation rule after PRs #509+#510 resolve.

**heal_pipeline_stall.py --dry-run note:** `--dry-run` does NOT suppress writes to larry-alerts.jsonl for this script. When cooldown expires, the alert fires even in dry-run mode. Be aware: calling --dry-run in a cycle will generate real alerts if the cooldown has passed.

---

## Key standing items (as of iter ~1893)

| Item | Status | Action needed |
|---|---|---|
| PR #497 REVIEW_ESCALATE | [yellow] Carry — **MERGEABLE** (confirmed iter ~1897/~1898); reviewDecision=""; Mirror REVIEW_ESCALATE at 04:05Z Jun-14 (~35h old). Under 72h. | Carry; at 72h escalate or close |
| PR #509 + #510 | [yellow] UNKNOWN/no-review; medic DM'd Larry attempt 8 (Jun-15 06:49Z); pending approval `unreg-approval-482eb78951ee` (dashboard; chat_id=None) | Larry replies: go:merge-509-510-direct OR go:mirror-review-509-510 |
| G-rule stall-detector Forge build | [yellow] Beacon spec CONFIRMED COMPLETE (branch-prefix gate; notification archived 01:51Z Jun-15). Forge build pending Larry's dashboard approval. | Approve Forge build via dashboard |
| G-rule stuck-cycle-timer | [blue] **1/3** (started iter ~1893). `ourliberty-cycle.timer` had NextElapseUSecRealtime empty + NextElapseUSecMonotonic=infinity; heal-systemd-install-drift auto-healed at 06:00Z Jun-15. | Watch; dispatch at 3/3 |
| unreviewed-merge:511 | [yellow] PR #511 (`feat/local-review-pass-marker`) merged by Larry at 23:58Z Jun-14 without Mirror routing | Reply 'go: retroactive-review-511' or 'silence: local-review-marker-counts' |
| unreviewed-merge:499 | [yellow] PR #499 merged by Larry without Mirror | Reply 'go: retroactive-review-499' or 'silence: missions-spec-no-mirror-needed' |
| unreviewed-merge:494 | [yellow] DM sent iter ~1694 (01:54Z Jun-14) | Reply 'go: retroactive-review-494' or 'silence: missions-promotions-no-mirror-needed' |
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if Mirror review wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| cleanup-branch-success-alert-info-translation-001 | [blue] **CLOSED** — Forge REJECTED (already satisfied by PR #485). 5 residual baseline-red sources tracked under G-rules below. | No action; G-rules handle at 3/3 |
| G-rule health-notify-script-missing | [blue] **1/3** | Watch; dispatch at 3/3 |
| catalog-accuracy-drift | [blue] 8/34 ourliberty-graph shelf cards drifted (attention rate 24%, gate 10%) | route=digest; journal-note only |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| G-rule missions-autoregister-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-timeout-worktree-missing-retry-loop | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule missions-card-gc-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule F24-empty-prompt-envelope-rejected | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule timer-cycle-no-journal-entry | [blue] **0/3** | Watch |
| G-rule heal-stale-daemon-script_path-cosmetic | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule Forge-preflight-marker-error-retry | [blue] **1/3** | Watch; dispatch at 3/3 |
| sync-push-rebase-loop-001 | [blue] UNREGISTERED AR; last occurrence 13:22:39Z Jun-14 (self-healed) | Carry |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| dashboard_api PID 2868353 | [blue] Ssl stable; prior restart cause still unknown | Note; watch for recurrence |
| Stale bash orphans | [blue] PIDs 1834248 (17d 8h) + 2605007 (23h+). Ss, low CPU. | Carry; cleanup when convenient |
