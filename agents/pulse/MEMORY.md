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

## Status snapshot — updated 2026-06-14 05:16Z UTC (Iter ~1715, Tier 1, consecutive_clean=0)

**Iter ~1715 summary:** Alert watermark: 992 (2 new: unreviewed-merge:499 [yellow] DM already sent by Beacon; dashboard_api auto-restart [Tier-4/nominal]). G-rule heal-stale-daemon-code-auto-restart-needs-template hit 3/3 → dispatched to Beacon (g-rule-heal-stale-daemon-warn-to-info-001). PRIME DIRECTIVE: interventions=847, systemic_fixes=41, verification_pending=11, ratio=20.66, trend=flat. Dashboard_api PID: 2322792→2627542 (restarted by healer). HEAD=ea9a055=origin/main. Sync: 04:59:59Z (~16 min ago). Larry directive active: Phase 4 build sequence request to Beacon at 05:05:59Z (Beacon replied, in-flight). PR #497 REVIEW_ESCALATE carry. PR #499 unreviewed merge (new). **Tier:** Tier 1.

---

## Key standing items (as of iter ~1715)

| Item | Status | Action needed |
|---|---|---|
| PR #497 REVIEW_ESCALATE | [yellow] Carry since iter ~1710 — Mirror found spec invalid; Beacon: fix already in alert-translations.json | Close PR: `gh pr close 497 --repo Larry-Yatch/ourliberty-agent-core` |
| unreviewed-merge:499 | [yellow] NEW — PR #499 spec(missions-v2) Phase 4 merged by Larry at 05:02:56Z without Mirror; DM sent by Beacon at 23:09 MDT | Reply 'go: retroactive-review-499' or 'silence: missions-spec-no-mirror-needed' |
| unreviewed-merge:494 | [yellow] DM sent iter ~1694 (01:54Z Jun-14) | Reply 'go: retroactive-review-494' or 'silence: missions-promotions-no-mirror-needed' |
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if Mirror review wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| Forge missions-proposed reconciliation | [blue/watch] PID 2602672 running (1h+ elapsed; timeout ~08:08Z Jun-14); inbox task active | Watch: next cycle expect forge.log "Completed" + inbox archived |
| Phase 4 build sequence directive | [blue] Larry asked Beacon at 05:05Z to dispatch build sequence for missions-v2 Phase 4 spec | Beacon handling; watch for approval_request in next cycle |
| fix-alert-triage-watermark-durability-001 | [blue] 1 stale entry in beacon-pending-approvals (Jun-12) | Carry |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| G-rule heal-stale-daemon-code-auto-restart-needs-template | DISPATCHED to Beacon iter ~1715 (g-rule-heal-stale-daemon-warn-to-info-001). Reset 0/3. | Watch: Beacon spec → Forge PR |
| G-rule alert-translations-no-patterns-delivery-confirmation-tier4 | [blue] 2/3 | Watch; dispatch at 3/3 |
| G-rule missions-card-gc-warn-vs-info | [blue] 1/3 | Watch; dispatch at 3/3 |
| G-rule missions-autoregister-warn-vs-info | [blue] 1/3 (new iter ~1714) | Watch; dispatch at 3/3 |
| G-rule droplet-uncommitted:main | [blue] 1/3 | Watch |
| G-rule F24-empty-prompt-envelope-rejected | [blue] 1/3 | Watch |
| G-rule timer-cycle-no-journal-entry | RESET 0/3 | Watch |
| Check 5 MISSING | [blue] heal-stale-daemon-code-state.json absent despite healer running at 05:05Z | Carry; possible path bug in healer |
| sync-push-rebase-loop-001 | [blue] UNREGISTERED AR; self-healed | Carry |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| fix-depth1-pulse-approval-extraction-001 | CLOSED — Forge REJECTED (PR #484 already implements it) | No action |
| PR #498 MERGED | RESOLVED — merged 22:49:46Z Jun-13 | CLOSED |
| notifier-autopr-allowlist-from-config-001 | RESOLVED — PR #493 merged 21:12Z Jun-13 | CLOSED |
