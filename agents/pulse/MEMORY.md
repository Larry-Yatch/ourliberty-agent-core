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

## Status snapshot — updated 2026-06-14 03:13Z UTC (Iter ~1703, Tier 2, consecutive_clean=0)

**Iter ~1703 summary:** Alert watermark: 984 (advanced from 982; 2 Tier-3/nominal lines re-claimed). PRIME DIRECTIVE: interventions=839, systemic_fixes=39, verification_pending=11, ratio=21.51, trend=flat. All 5 daemons alive (same PIDs). HEAD=1c01ccf=origin/main. Sync: sync.json stale-error (02:59Z), self-healed; actual repo synced. Inboxes: all empty. 0 open PRs. **Tier:** DE-ESCALATED to Tier 2, consecutive_clean=0 (3 consecutive clean iters at Tier 1).

---

## Key standing items (as of iter ~1701)

| Item | Status | Action needed |
|---|---|---|
| unreviewed-merge:494 | [yellow] DM sent iter ~1694 (01:54Z) | Reply 'go: retroactive-review-494' or 'silence: missions-promotions-no-mirror-needed' |
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if Mirror review wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| PR #496 MERGED | RESOLVED — merged 02:57:16Z Jun-14 (Mirror REVIEW_PASS + auto-merge) | CLOSED |
| timer-cycle-no-journal-entry-001 | FALSE POSITIVE — Beacon investigated, journals present, cosmetic label only; TASK CLOSED | Larry binary: `approve timer-label-fix` or `reject timer-label-fix` (Beacon holding; [blue] carry) |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| beacon-pending-approvals | [blue] 2 stale entries (2x Jun-12) | Carry; no actionable pending |
| G-rule dispatch-branch-cleanup-warning | [blue] 2/3 (iter ~1700) | Watch |
| G-rule heal-stale-daemon-code-auto-restart-needs-template | [blue] 2/3 | Dispatch at 3/3 |
| G-rule droplet-uncommitted:main | [blue] 1/3 | Watch |
| G-rule F24-empty-prompt-envelope-rejected | [blue] 1/3 | Watch |
| G-rule alert-translations-no-patterns-delivery-confirmation-tier4 | [blue] 0/3 | Watch |
| G-rule timer-cycle-no-journal-entry | RESET to 0/3 — false positive (cosmetic label; Beacon confirmed journals present) | Recalibrate: only count iters where journal entry is genuinely absent from the file |
| Check 5 MISSING | [blue] heal-stale-daemon-code-state.json absent | G-rule dispatched ~iter 1416 |
| sync-push-rebase-loop-001 | [blue] UNREGISTERED AR; new occurrence 02:59:39Z Jun-14 (self-healed) | Carry |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| unreviewed-merge:495 | RESOLVED — implicitly resolved by PR #496 exemption merge; DM delivered, no Larry reply needed | CLOSED iter ~1702 |
| unreviewed-merge-missions-exemption-001 | RESOLVED — Larry approved 02:45Z Jun-14; Forge built PR #496; Mirror PASS + auto-merged 02:57Z Jun-14 | CLOSED iter ~1702 |
| G-rule unreviewed-merge | RESOLVED — 3/3 dispatched iter ~1700; PR #496 systemic fix MERGED iter ~1702 | CLOSED |
| catalog-drift-facts-sync-001 | RESOLVED — Forge rejected (work done on main PR#1) | CLOSED iter ~1694 |
| source=pulse-cycle-self-report | RESOLVED — PR #490 merged Jun-13 | CLOSED |
| approval_request-delivery-confirmation | RESOLVED — PR #491 merged Jun-13 | CLOSED |
| notifier-autopr-allowlist-from-config-001 | RESOLVED — PR #493 merged 21:12Z Jun-13 | CLOSED |
| agent-models-allowlist-not-on-main | RESOLVED — commit e427631 | CLOSED |
| wire-pulse-check-iv-cadence-001 | RESOLVED — PR #488 merged; timer active | CLOSED |
| PR #495 MERGED | RESOLVED — merged iter ~1699 (no Mirror review → G-rule 3/3) | CLOSED |
