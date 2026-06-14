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

## Status snapshot — updated 2026-06-14 04:34Z UTC (Iter ~1710, Tier 1, consecutive_clean=0)

**Iter ~1710 summary:** Alert watermark: 988 (no change, 0 new alerts). PRIME DIRECTIVE: interventions=842, systemic_fixes=40, verification_pending=11, ratio=21.05, trend=flat. All 5 daemons alive (same PIDs). HEAD=d27ec1c=origin/main. Sync: 03:59:51Z no-change (healthy). missions-proposed Forge second run in-flight (PID 2602672). PR #497 Mirror REVIEW_ESCALATE — spec invalid, fix already in translations.json, close PR. **Tier:** Tier 1 (signal: PR #497 escalation).

---

## Key standing items (as of iter ~1710)

| Item | Status | Action needed |
|---|---|---|
| unreviewed-merge:494 | [yellow] DM sent iter ~1694 (01:54Z Jun-14) | Reply 'go: retroactive-review-494' or 'silence: missions-promotions-no-mirror-needed' |
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if Mirror review wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| PR #497 REVIEW_ESCALATE | [yellow] NEW iter ~1710 — Mirror found spec invalid; Beacon: fix already in alert-translations.json | Close PR: `gh pr close 497 --repo Larry-Yatch/ourliberty-agent-core` |
| missions-proposed-lane-signal-hardening-001 | [blue] Forge second run in-flight (PID 2602672, 04:08Z) | Watch for PR |
| fix-alert-triage-watermark-durability-001 | [blue] 1 stale entry in beacon-pending-approvals (Jun-12) | Carry |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| G-rule dispatch-branch-cleanup-warning | RESOLVED — PR #497 Mirror ESCALATED; fix already in translations.json | No further dispatch |
| G-rule heal-stale-daemon-code-auto-restart-needs-template | [blue] 2/3 | Dispatch at 3/3 |
| G-rule alert-translations-no-patterns-delivery-confirmation-tier4 | [blue] 2/3 | Watch; dispatch at 3/3 |
| G-rule missions-card-gc-warn-vs-info | [blue] 1/3 | Watch; dispatch at 3/3 — missions-card-gc severity=warning on GC runs should be severity=info |
| G-rule droplet-uncommitted:main | [blue] 1/3 | Watch |
| G-rule F24-empty-prompt-envelope-rejected | [blue] 1/3 | Watch |
| G-rule timer-cycle-no-journal-entry | RESET 0/3 — false positive | Recalibrate |
| Check 5 MISSING | [blue] heal-stale-daemon-code-state.json absent | G-rule dispatched ~iter 1416 |
| sync-push-rebase-loop-001 | [blue] UNREGISTERED AR; self-healed | Carry |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| fix-depth1-pulse-approval-extraction-001 | CLOSED — Forge REJECTED (PR #484 already implements it) | No action |
| timer-label-fix binary | RESOLVED — not in pending; task closed/false-positive | No action |
| PR #496 MERGED | RESOLVED — merged 02:57:16Z Jun-14 | CLOSED |
| notifier-autopr-allowlist-from-config-001 | RESOLVED — PR #493 merged 21:12Z Jun-13 | CLOSED |
