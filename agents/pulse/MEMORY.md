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

## Alert watermark persistence gap (learned 2026-06-14 iter ~1703)

**Rule:** In interactive `/cycle` sessions, `alert_triage_state.py set-watermark` is called by Pulse's journal narrative but NOT always committed before session end. On next iter, get-watermark returns the pre-session value (e.g., 982 instead of expected 984). Check the watermark at start of each iter and advance it if the lines in question have already been triaged (Tier-3/nominal). Do NOT re-triage — just confirm against prior journal and advance. This is structural: interactive sessions may not persist watermark if Pulse exits before the explicit set-watermark step.

---

## larry-alerts.jsonl correct path (learned 2026-06-14 iter ~1741)

**Rule:** `larry-alerts.jsonl` lives at `/home/larry/agents/blackboard/larry-alerts.jsonl`. NOT `/home/larry/agents/logs/`. Confirmed by `ls /home/larry/agents/blackboard/larry-alerts*`.

---

## heal-stale-daemon-code heartbeat correct path (corrected iter ~1768)

**Rule:** The heal-stale-daemon-code heartbeat lives at `/home/larry/agents/blackboard/heal-stale-daemon-code.heartbeat` (NOT `state/`). This is `HEARTBEAT_FILE` in `scripts/heal_stale_daemon_code.py`. Previous Check 5 invocations used `state/` path and would have gotten "no file" — always use `blackboard/` path.

---

## Check 0 must call helper before manual classification (learned 2026-06-14 iter ~1812)

**Rule:** Before manually classifying an alert as Tier-4, Pulse MUST call `python3 scripts/alert_triage_state.py triage-alert --alert-id "<id>" --alert '<json>' --iter <N>` and act on the returned tier. If the helper returns Tier-3 (silence/known-pattern match), that result is authoritative — do NOT override it with in-prompt manual classification. The helper handles `kind`-only alerts (no `subject` field) via fallback logic in `_translation_match` that Pulse's in-prompt subject-keyed lookup misses. PR #491 (merged 2026-06-13) already added `outbox-notifier → approval_request` Tier-3 silence to `config/alert-translations.json`; multiple Tier-4 mis-classifications before and after that PR were Pulse bypassing the helper.

---

## Status snapshot — updated 2026-06-14 19:11Z UTC (Iter ~1817, Tier 1, nominal)

**Iter ~1817 summary:** All checks 0–5 + A–E nominal. 0 new alerts (watermark=930). No new findings. PR #497 carry (93rd iter). §5.0 one-shots all no-op. Rotations nominal (closest: SUPABASE_SERVICE_ROLE_KEY 69d). PRIME DIRECTIVE: ratio≈20.44, systemic_fixes=45, iter_clean. **Tier: Tier 1** (consecutive_clean=0).

---

## Key standing items (as of iter ~1816)

| Item | Status | Action needed |
|---|---|---|
| PR #497 REVIEW_ESCALATE | [yellow] Carry — statusCheckRollup=FAILURE, no reviewDecision (93rd iter: ~1724→~1817) | Close PR: `gh pr close 497 --repo Larry-Yatch/ourliberty-agent-core` |
| unreviewed-merge:499 | [yellow] PR #499 merged by Larry without Mirror | Reply 'go: retroactive-review-499' or 'silence: missions-spec-no-mirror-needed' |
| unreviewed-merge:494 | [yellow] DM sent iter ~1694 (01:54Z Jun-14) | Reply 'go: retroactive-review-494' or 'silence: missions-promotions-no-mirror-needed' |
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if Mirror review wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| TSR DAG sequence | [blue] **COMPLETE ✅** — PR #504 ✅, PR #505 ✅, PR #506 ✅ (18:13:12Z) | Done |
| captures-dirty-tree-allowlist-001 | [blue] **RESOLVED ✅** — PR #507 merged 18:50:13Z, commit 8cf83835; fix live | Done |
| ourliberty-dashboard PR #54 | [blue] **RESOLVED ✅** — merged 10:31:11Z (p4-parked-card: meaning-layer Parked card) | Done |
| check-0-helper-authority-enforcement-001 | [blue] **PENDING LARRY APPROVAL** (beacon-pending-approvals.json, created 18:38:15Z Jun-14) | Reply 'approve'/'go'/'ok' in Telegram |
| G-rule health-notify-script-missing | [blue] **1/3** — ourliberty-health notify path misconfigured; alerts drop to journalctl only | Watch; dispatch at 3/3 |
| catalog-accuracy-drift | [blue] 8/34 ourliberty-graph shelf cards drifted (attention rate 24%, gate 10%) | route=digest; journal-note only |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| G-rule missions-autoregister-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-timeout-worktree-missing-retry-loop | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule missions-card-gc-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule droplet-uncommitted:main | [blue] **RESOLVED** (PR #507 merged 18:50Z) | Monitor for recurrence |
| G-rule F24-empty-prompt-envelope-rejected | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule timer-cycle-no-journal-entry | [blue] **0/3** | Watch |
| G-rule heal-stale-daemon-script_path-cosmetic | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule Forge-preflight-marker-error-retry | [blue] **1/3** | Watch; dispatch at 3/3 |
| sync-push-rebase-loop-001 | [blue] UNREGISTERED AR; last occurrence 13:22:39Z Jun-14 (self-healed) | Carry |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| dashboard_api PID 2868353 | [blue] Ssl stable since 17:59Z; prior restart cause still unknown | Note; watch for recurrence |
| Stale bash orphans | [blue] PIDs 1834248 (16d 23h+) + 2605007 (14h 41m+). Ss, 0% CPU. | Carry; cleanup when convenient |
