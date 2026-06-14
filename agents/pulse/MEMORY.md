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

**Rule:** Beacon inbox dispatch envelopes MUST use root field `task_id` (not `envelope_id`). Required fields: `task_id`, `source`, `dedup_identity`, `prompt`, `timeout`. `envelope_id` is silently ignored and fails the validator.

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

## Status snapshot — updated 2026-06-14 01:22Z UTC (Iter ~1693, interactive /cycle, Tier 3, consecutive_clean 4→5)

**Iter ~1693 summary:** Alert watermark: 975 (0 new). Tier 3, consecutive_clean 4→5 (steady-state). PRIME DIRECTIVE (script-authoritative): interventions=836, systemic_fixes=37, verification_pending=11, ratio=22.59, trend=flat. All checks nominal. 5/5 daemons same PIDs (beacon_telegram_bot:2517973, chain_event_shipper:1849505, outbox_notifier:2552416, dashboard_api:2322792, inbox_watcher:2530123). 0 open PRs. git: clean, HEAD=633a526=origin/main. Sync: last_sync=00:59:26Z (~23 min, nominal). Pipeline skip list 5 items (stable). **Conditional:** Check I SKIP (check-i-2026-06-14.json exists from ~1691). Check III SKIP (3d artifact, < 14d). **CARRY:** unreviewed-merge:489 [yellow], Tier-2 weekly probe auth_401, Check III `approve threshold-update-2026-06-11`, beacon-pending-approvals 3 stale entries (2x Jun-12 + catalog-drift Jun-13), Check I proposal medic-operator-scaffold-001 [blue], G-rule timer-cycle-no-journal-entry 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 2/3, G-rule droplet-uncommitted:main 1/3, G-rule F24-empty-prompt-envelope-rejected 1/3, G-rule alert-translations-no-patterns-delivery-confirmation-tier4 0/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED. **Tier:** Tier 3, consecutive_clean=5. (Steady-state.)

---

## Status snapshot — updated 2026-06-14 00:51Z UTC (Iter ~1692, interactive /cycle, Tier 3, consecutive_clean 3→4)

**Iter ~1692 summary:** Alert watermark: 975 (2 new delivery-confirmation alerts from iter ~1691's Check I + ledger run; both Tier-4 per helper, silenced per memory discipline, no DM). Tier 3, consecutive_clean 3→4 (steady-state). PRIME DIRECTIVE (script-authoritative): interventions=836, systemic_fixes=37, verification_pending=11, ratio=22.59, trend=flat. All checks nominal. 5/5 daemons same PIDs (beacon_telegram_bot:2517973, chain_event_shipper:1849505, outbox_notifier:2552416, dashboard_api:2322792, inbox_watcher:2530123). 0 open PRs. git: clean, HEAD=b67ad72=origin/main. Sync: last_sync=23:59:19Z (~52 min, nominal). Pipeline skip list 5 items (stable). **Conditional:** Check I SKIP (check-i-2026-06-14.json exists from ~1691). Check III SKIP (3d artifact, < 14d). **New note:** alert-translations.json has 0 patterns → G-rule 0/3 alert-translations-no-patterns-delivery-confirmation-tier4. **CARRY:** unreviewed-merge:489 [yellow], Tier-2 weekly probe auth_401, Check III `approve threshold-update-2026-06-11`, beacon-pending-approvals 3 stale entries (2x Jun-12 + catalog-drift Jun-13), Check I proposal medic-operator-scaffold-001 [blue], G-rule timer-cycle-no-journal-entry 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 2/3, G-rule droplet-uncommitted:main 1/3, G-rule F24-empty-prompt-envelope-rejected 1/3, G-rule alert-translations-no-patterns-delivery-confirmation-tier4 0/3 (new), Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED. **Tier:** Tier 3, consecutive_clean=4. (Steady-state.)

---

## Status snapshot — updated 2026-06-14 00:20Z UTC (Iter ~1691, interactive /cycle, Tier 3, consecutive_clean 2→3, Sunday Check I)

**Iter ~1691 summary:** Alert watermark: 973 (0 new at check-time; Check I DM queued → expect 974 next iter). Tier 3, consecutive_clean 2→3 (steady-state). PRIME DIRECTIVE (script-authoritative): interventions=836, systemic_fixes=37, verification_pending=11, ratio=22.59, trend=flat. All checks nominal. 5/5 daemons same PIDs (beacon_telegram_bot:2517973, chain_event_shipper:1849505, outbox_notifier:2552416, dashboard_api:2322792, inbox_watcher:2530123). 0 open PRs. git: clean, HEAD=92bf506=origin/main. Sync: last_sync=23:59:19Z (~21 min, nominal). Pipeline skip list 5 items (stable). **Check I (Sunday):** mode=digest, 1 proposal — medic-operator-scaffold-001 24.4σ ($6.72 vs $1.68 baseline), effort=small, dedup-skip (prior dispatch 2026-06-10). **CARRY:** unreviewed-merge:489 [yellow], Tier-2 weekly probe auth_401, Check III `approve threshold-update-2026-06-11`, beacon-pending-approvals 3 stale entries (2x Jun-12 + catalog-drift Jun-13), Check I proposal medic-operator-scaffold-001 [blue], G-rule timer-cycle-no-journal-entry 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 2/3, G-rule droplet-uncommitted:main 1/3, G-rule F24-empty-prompt-envelope-rejected 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED. **Tier:** Tier 3, consecutive_clean=3. (Steady-state.)

---

## Status snapshot — updated 2026-06-13 23:46Z UTC (Iter ~1690, interactive /cycle, Tier 3, consecutive_clean 1→2)

**Iter ~1690 summary:** Alert watermark: 973 (0 new). Tier 3, consecutive_clean 1→2. PRIME DIRECTIVE (script-authoritative): interventions=836, systemic_fixes=37, verification_pending=11, ratio=22.59, trend=flat. All checks nominal. 5/5 daemons same PIDs (beacon_telegram_bot:2517973, chain_event_shipper:1849505, outbox_notifier:2552416, dashboard_api:2322792, inbox_watcher:2530123). 0 open PRs. git: clean, HEAD=dc8bb39=origin/main. Sync: last_sync=22:58:59Z (~48 min, nominal). Pipeline skip list 5 items (stable). **CARRY:** unreviewed-merge:489 [yellow], Tier-2 weekly probe auth_401, Check III `approve threshold-update-2026-06-11`, beacon-pending-approvals 3 stale entries (2x Jun-12 + catalog-drift Jun-13), G-rule timer-cycle-no-journal-entry 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 2/3, G-rule droplet-uncommitted:main 1/3, G-rule F24-empty-prompt-envelope-rejected 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED. **Tier:** Tier 3, consecutive_clean=2.

---

## Status snapshot — updated 2026-06-13 22:42Z UTC (Iter ~1688, interactive /cycle, Tier 2→3 de-escalation, consecutive_clean 2→3)

**Iter ~1688 summary:** Alert watermark: 973 (0 new). Tier 2→3 DE-ESCALATION (3rd consecutive clean Tier-2 iter). PRIME DIRECTIVE (script-authoritative): interventions=836, systemic_fixes=37, verification_pending=11, ratio=22.59, trend=flat. All checks nominal. 5/5 daemons same PIDs. 0 open PRs. git: clean, HEAD=41205ad=origin/main. Sync: last_sync=21:58:42Z (~44 min, nominal). forge/.invalid: empty. Pipeline skip list 4 items (PR #488 task cleaned). **Tier:** Tier 3, consecutive_clean=0.

---

## Key standing items (as of iter ~1692)

| Item | Status | Action needed |
|---|---|---|
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | Reply 'go: retroactive-review-489' if Mirror review wanted |
| Tier-2 weekly probe auth_401 | [yellow] Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | [yellow] Pending Larry | `approve threshold-update-2026-06-11` |
| Check I medic-operator-scaffold-001 | [blue] 24.4σ; prior dispatch 2026-06-10 | `/dispatch 1` if re-run needed |
| beacon-pending-approvals | [blue] 3 stale entries (2x Jun-12 + catalog-drift Jun-13) | Carry; no actionable pending |
| G-rule timer-cycle-no-journal-entry | [blue] 1/3 | Watch |
| G-rule heal-stale-daemon-code-auto-restart-needs-template | [blue] 2/3 | Dispatch at 3/3 |
| G-rule droplet-uncommitted:main | [blue] 1/3 | Watch |
| G-rule F24-empty-prompt-envelope-rejected | [blue] 1/3 | Watch |
| G-rule alert-translations-no-patterns-delivery-confirmation-tier4 | [blue] 0/3 | Watch; first iter ~1692 |
| Check 5 MISSING | [blue] heal-stale-daemon-code-state.json absent | G-rule dispatched ~iter 1416 |
| sync-push-rebase-loop-001 | [blue] UNREGISTERED AR | Carry |
| dag-preflight-revision gap | [blue] PR #484 closed source=pulse gap | DAG markers still fall through |
| ccd-s1-envelope-builder | [blue] PAUSED | Carry; unverified |
| source=pulse-cycle-self-report | RESOLVED — PR #490 merged Jun-13 | CLOSED |
| approval_request-delivery-confirmation | RESOLVED — PR #491 merged Jun-13 | CLOSED |
| notifier-autopr-allowlist-from-config-001 | RESOLVED — PR #493 merged 21:12Z Jun-13 | CLOSED |
| catalog-drift-facts-sync-001 | RESOLVED — ourliberty-graph PR #1 merged ~1674 | CLOSED |
| agent-models-allowlist-not-on-main | RESOLVED — commit e427631 | CLOSED |
| wire-pulse-check-iv-cadence-001 | RESOLVED — PR #488 merged; timer active | CLOSED |
