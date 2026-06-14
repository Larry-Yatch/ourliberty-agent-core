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

## Status snapshot — updated 2026-06-14 16:00Z UTC (Iter ~1791, Tier 1, signal: adopt-terminal-002 pending approval)

**Iter ~1791 summary:** Check 0 found alert line 920 — adopt-terminal-state-reconciliation-spec-002 approval_request from outbox-notifier (15:56:11Z UTC). Beacon independently diagnosed push-auth gap as stall root cause; created 002 re-dispatch; DM sent to Larry. Pending Larry's "go". All mandatory checks 1-5 + A-E nominal except PR #497 (67th carry). Watermark advanced 919→920. No new dispatches. PRIME DIRECTIVE: interventions≈915, systemic_fixes=41, ratio≈22.32, trend=flat. **Tier: Tier 1** (pending approval in flight; consecutive_clean=0).

---

## Key standing items (as of iter ~1791)

| Item | Status | Action needed |
|---|---|---|
| **adopt-terminal-state-reconciliation-spec-002** | [yellow] Pending Larry's "go" — DM sent 09:56 MDT Jun-14; Forge inbox empty until approved | Reply "go" / "approve" to Telegram DM |
| PR #497 REVIEW_ESCALATE | [yellow] Carry — statusCheckRollup=FAILURE, no reviewDecision (67th iter: ~1724→~1791) | Close PR: `gh pr close 497 --repo Larry-Yatch/ourliberty-agent-core` |
| unreviewed-merge:499 | [yellow] PR #499 merged by Larry without Mirror | Reply 'go: retroactive-review-499' or 'silence: missions-spec-no-mirror-needed' |
| unreviewed-merge:494 | [yellow] DM sent iter ~1694 (01:54Z Jun-14) | Reply 'go: retroactive-review-494' or 'silence: missions-promotions-no-mirror-needed' |
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if Mirror review wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| pulse-investigate-forge-stall-adopt-terminal-001 in Beacon inbox | [blue] ~9 min old (15:51Z); Beacon will process; dedup should prevent 3rd dispatch | Watch — Beacon handles |
| catalog-accuracy-drift | [blue] 8/34 ourliberty-graph shelf cards drifted (attention rate 24%, gate 10%) | route=digest; journal-note only |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| G-rule adopt-terminal-state-forge-stall | [yellow] **3/3 DISPATCHED** (iter ~1790) | Beacon investigating; 002 re-dispatch in flight |
| G-rule missions-autoregister-warn-vs-info | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule Forge-timeout-worktree-missing-retry-loop | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule alert-translations-no-patterns-delivery-confirmation-tier4 | [blue] **2/3** | Watch; dispatch at 3/3 |
| G-rule missions-card-gc-warn-vs-info | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule droplet-uncommitted:main | [blue] **2/3** (last occurrence 13:22:39Z Jun-14, self-healed; no new occurrence through iter ~1791) | Watch; dispatch at 3/3 |
| G-rule F24-empty-prompt-envelope-rejected | [blue] 1/3 | Watch |
| G-rule timer-cycle-no-journal-entry | [blue] **0/3** (RESET; false positive confirmed by Beacon) | Forge PR pending: approve/reject timer-label-fix |
| G-rule heal-stale-daemon-script_path-cosmetic | [blue] 1/3 | Watch; dispatch at 3/3 |
| sync-push-rebase-loop-001 | [blue] UNREGISTERED AR; last occurrence 13:22:39Z Jun-14 (self-healed); sync now no-change | Carry |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| Stale bash orphan loops | [blue] PIDs 1834248 (May 28) + 2605007 (Jun 13). 0% CPU. | Carry; cleanup when convenient |
