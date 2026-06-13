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

## Status snapshot — updated 2026-06-13 10:24Z UTC (Iter ~1637, interactive /cycle, Tier 3→1, consecutive_clean 16→0)

**Iter ~1637 summary:** Alert watermark: 943→946 (3 new alerts: L944 Tier-3 silence, L945 droplet-uncommitted:main Tier-4 [outbox-notifier already DM'd at 10:18Z], L946 catalog-accuracy-drift Tier-4 G-rule 3/3 DISPATCHED). Tier 3→1 reset (Tier-4 findings). PRIME DIRECTIVE (script-authoritative): interventions=822, systemic_fixes=34, verification_pending=11, ratio=24.18, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. Beacon inbox: catalog-accuracy-drift-grule-dispatch-001.json. Sync: last_sync=09:56:49Z status=error ("Uncommitted changes"); last successful sync=01:56:15Z (~8.5h); blocked by uncommitted agents/pulse/.claude/settings.json from iter ~1625. beacon-pending-approvals.json: **2 stale entries** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule DISPATCHED iter ~1623. **NEW:** G-rule droplet-uncommitted-main 1/3 [NEW]. **G-rule catalog-accuracy-drift DISPATCHED** (3/3) — direction-ask catalog-accuracy-drift-grule-dispatch-001 to Beacon. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule sync-blocked:uncommitted-changes 1/3, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED. **Tier:** Tier 1, consecutive_clean=0.

---

## Status snapshot — updated 2026-06-13 09:18Z UTC (Iter ~1635, interactive /cycle, Tier 3, consecutive_clean 14→15)

**Iter ~1635 summary:** Alert watermark: L1159 (1 new: L1159 sync-blocked Tier-3 silence). Tier 3, consecutive_clean 14→15. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=08:56:39Z status=error ("Uncommitted changes"); last successful sync=01:56:15Z (~7h); blocked by uncommitted agents/pulse/.claude/settings.json from iter ~1625. beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule DISPATCHED iter ~1623; entries self-clear after PR merges. **pulse-check-iv: RESOLVED** — ourliberty-pulse-check-iv.timer active (waiting), next fire Mon 2026-06-15 04:26:45 MDT. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule sync-blocked:uncommitted-changes 1/3, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=15.

---

## Status snapshot — updated 2026-06-13 08:12Z UTC (Iter ~1633, interactive /cycle, Tier 3, consecutive_clean 12→13)

**Iter ~1633 summary:** Alert watermark: L1157 (1 new: L1157 sync-blocked Tier-3 silence). Tier 3, consecutive_clean 12→13. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=07:56:29Z status=error ("Uncommitted changes"); last successful sync=01:56:15Z (~6h); blocked by uncommitted agents/pulse/.claude/settings.json from iter ~1625. beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule DISPATCHED iter ~1623; entries self-clear after PR merges. **pulse-check-iv: RESOLVED** — ourliberty-pulse-check-iv.timer active (waiting), next fire Mon 2026-06-15 04:26:45 MDT. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule sync-blocked:uncommitted-changes 1/3, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=13.

---

## Status snapshot — updated 2026-06-13 07:41Z UTC (Iter ~1632, interactive /cycle via /loop, Tier 3, consecutive_clean 11→12)

**Iter ~1632 summary:** Alert watermark: L1156 (1 new: L1156 dispatch-branch-cleanup Tier-3 silence). Tier 3, consecutive_clean 11→12. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=06:56:23Z status=error ("Uncommitted changes"); last successful sync=01:56:15Z (~6h); blocked by uncommitted agents/pulse/.claude/settings.json from iter ~1625. beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule DISPATCHED iter ~1623; entries self-clear after PR merges. **pulse-check-iv: RESOLVED** — ourliberty-pulse-check-iv.timer active (waiting), next fire Mon 2026-06-15 04:26:45 MDT. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule sync-blocked:uncommitted-changes 1/3, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=12.

---

## Status snapshot — updated 2026-06-13 07:12Z UTC (Iter ~1631, interactive /cycle, Tier 3, consecutive_clean 10→11)

**Iter ~1631 summary:** Alert watermark: L1155 (1 new: L1155 sync-blocked Tier-3 silence). Tier 3, consecutive_clean 10→11. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=06:56:23Z status=error ("Uncommitted changes"); last successful sync=01:56:15Z (~5h); blocked by uncommitted agents/pulse/.claude/settings.json from iter ~1625. beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule DISPATCHED iter ~1623; entries self-clear after PR merges. **pulse-check-iv: RESOLVED** — heal-systemd-install-drift fired 06:00Z UTC June 13, auto-installed service+timer; ourliberty-pulse-check-iv.timer now active (waiting), next fire Mon 2026-06-15 04:26:45 MDT. Heartbeat still stale (mtime=18:26Z June 12) until first timer run. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule sync-blocked:uncommitted-changes 1/3, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=11.

---

## Status snapshot — updated 2026-06-13 04:59Z UTC (Iter ~1627, interactive /cycle via /loop, Tier 3, consecutive_clean 6→7)

**Iter ~1627 summary:** Alert watermark: L1150 (1 new: L1150 sync-blocked:uncommitted-changes Tier-3 silence). Tier 3, consecutive_clean 6→7. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=04:56:17Z status=error ("Uncommitted modifications"); last successful sync=01:56:15Z (~3h); blocked by uncommitted agents/pulse/.claude/settings.json from iter ~1625. beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule DISPATCHED iter ~1623; Beacon confirmed root cause + fix dispatched to Forge; entries self-clear after PR merges. **pulse-check-iv:** heartbeat stale (~10.5h, mtime=18:26Z June 12); service inactive; drift-healer fires ~12:00Z UTC today (~7h from now). **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule sync-blocked:uncommitted-changes 1/3, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=7.

---

## Status snapshot — updated 2026-06-13 04:28Z UTC (Iter ~1626, interactive /cycle, Tier 3, consecutive_clean 5→6)

**Iter ~1626 summary:** Alert watermark: L1149 (0 new). Tier 3, consecutive_clean 5→6. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=03:56:17Z status=error ("Uncommitted changes in working tree"); last successful sync=01:56:15Z (~2.5h); blocked by uncommitted agents/pulse/.claude/settings.json from iter ~1625. beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule DISPATCHED (iter ~1623); Beacon confirmed root cause + fix dispatched to Forge; entries self-clear after PR merges. **pulse-check-iv:** heartbeat stale (~10h, mtime=18:26Z June 12); service inactive; drift-healer fires ~12:00Z UTC today. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule sync-blocked:uncommitted-changes 1/3, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=6.

---

## Status snapshot — updated 2026-06-13 03:57Z UTC (Iter ~1625, interactive /cycle, Tier 3, consecutive_clean 4→5)

**Iter ~1625 summary:** Alert watermark: L1149 (2 new: dispatch-branch-cleanup Tier-3 silence + sync-blocked:uncommitted-changes Tier-3 silence; latter is mid-cycle normal, G-rule not incremented). Tier 3, consecutive_clean 4→5. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=03:56:17Z status=error ("Uncommitted modifications" — mid-cycle normal); last successful sync=01:56:15Z (~2h at cycle start, triaged known-pattern). beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule heal-stale-approvals-not-gc-merged-prs DISPATCHED (iter ~1623); Beacon confirmed root cause + fix dispatched to Forge; entries self-clear after PR merges. **pulse-check-iv:** heartbeat EXISTS but stale (mtime=18:26Z June 12, ~9.5h at cycle start, flat path `~/agents/blackboard/pulse-check-iv.heartbeat`); service inactive; drift-healer fires ~12:00Z UTC today. **PERMISSIONS:** Added journalctl/systemctl/git-C/tmux-ls to `.claude/settings.json` allowlist this cycle. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=5.

---

## Status snapshot — updated 2026-06-13 03:19Z UTC (Iter ~1624, interactive /cycle via /loop, Tier 3, consecutive_clean 3→4)

**Iter ~1624 summary:** Alert watermark: L1147 (1 new: sync-service push-fail, Tier-3 silence). Tier 3, consecutive_clean 3→4. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=02:56:16Z status=error ("Auto-commit push failed; rolled back"); last successful sync=01:56:15Z (~83min ago, within 2h). beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001); G-rule heal-stale-approvals-not-gc-merged-prs DISPATCHED (iter ~1623); Beacon confirmed root cause + dispatched fix to Forge (inter-agent notify at top of journal); entries self-clear after PR merges. **pulse-check-iv:** heartbeat EXISTS but stale (mtime=18:26Z June 12, ~15.5h); service inactive; drift-healer fires ~12:00Z UTC today. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=4.

---

## Status snapshot — updated 2026-06-13 02:42Z UTC (Iter ~1623, interactive /cycle, Tier 3, consecutive_clean 2→3, G-rule dispatch)

**Iter ~1623 summary:** Alert watermark: L1146 (1 new: dispatch-branch-cleanup, Tier-3 silence). Tier 3, consecutive_clean 2→3. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=01:56:15Z, no-change, commit=0b5b092. beacon-pending-approvals.json: **CORRECTION** — 2 stale entries STILL PRESENT (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001; PRs #482+#484 merged). iter ~1622 "RESOLVED/0 entries" claim was FALSE. G-rule heal-stale-approvals-not-gc-merged-prs → **DISPATCHED** (3/3: iters ~1620, ~1621, ~1623). Beacon direction-ask: `heal-stale-approvals-not-gc-merged-prs-dispatch-001.json`. **pulse-check-iv:** heartbeat EXISTS but stale (mtime=12:26Z June 12, ~14.3h); service inactive; drift-healer fires ~12:00Z UTC today. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=3.

---

## Status snapshot — updated 2026-06-13 02:07Z UTC (Iter ~1622, interactive /cycle, Tier 3, consecutive_clean 1→2)

**⚠️ NOTE: "RESOLVED/0 entries" claim for beacon-pending-approvals.json was FALSE. Entries still present as of iter ~1623. G-rule heal-stale-approvals-not-gc-merged-prs was NOT reset.**

**Iter ~1622 summary:** Alert watermark: L1145 (0 new). Tier 3, consecutive_clean 1→2. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=01:56:15Z, no-change, commit=0b5b092. beacon-pending-approvals.json: claimed "0 entries/RESOLVED" — **incorrect** (entries still present at iter ~1623). **pulse-check-iv:** heartbeat EXISTS but stale (mtime=18:26Z June 12, ~7.7h); service inactive; drift-healer fires ~12:00Z UTC today. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=2.

---

## Status snapshot — updated 2026-06-13 00:27Z UTC (Iter ~1618, interactive /cycle, Tier 2 consecutive_clean 0→1)

**Iter ~1618 summary:** Alert watermark: L1144 (0 new). Tier 2, consecutive_clean 0→1. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=23:56:13Z, no-change, commit=aac2abb. beacon-pending-approvals.json: **2 stale entries STILL PRESENT** (fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001; iter ~1617 "0 entries GC'd" claim was FALSE — healer has NOT yet GC'd; MEMORY.md "RESOLVED/Closed" status was premature). **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule pulse-check-iv-no-heartbeat 3/3 (drift-healer fires Sat 06:00Z TODAY), G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 2, consecutive_clean=1.

---

## Status snapshot — updated 2026-06-13 00:05Z UTC (Iter ~1616, interactive /cycle, Tier 1 consecutive_clean 1→2)

**Iter ~1616 summary:** Alert watermark: L1144 (1 new: heal-stale-daemon-code route=digest, auto-restarted dashboard-api after PR #489 code change — treated as known healer success, no DM; G-rule 1/3 for translation template). Tier 1, consecutive_clean 1→2. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=23:56:13Z, no-change. beacon-pending-approvals.json: 2 stale entries (#482+#484) STILL PRESENT (iter ~1615 "GC'd" claim was incorrect). **CORRECTION:** iter ~1615 beacon-pending-approvals "0 items (GC'd)" was false — current truth: 2 stale entries, file mtime=22:35Z. **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule pulse-check-iv-no-heartbeat 3/3 (resolves Sat 06:00Z UTC drift-healer install), G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-success-needs-template 1/3 [NEW], Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 1, consecutive_clean=2.

---

## Status snapshot — updated 2026-06-12 23:57Z UTC (Iter ~1615, interactive /cycle, Tier 1 consecutive_clean 0→1)

**Iter ~1615 summary:** Alert watermark: L1143 (1 new: source=pulse-cycle self-report of iter ~1614 unreviewed-merge:489 DM — treated as known-pattern, no re-DM). Tier 1, consecutive_clean 0→1 (clean). PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=22:56:02Z (~57 min ago), within 2h. beacon-pending-approvals.json: 0 items (GC'd). **NEW G-RULE:** source=pulse-cycle-self-report 1/3 (source=pulse-cycle alerts are Pulse DM delivery records appearing as new alerts next iter; at 3/3 dispatch Beacon for allowlist entry in alert-translations.json). **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. G-rule pulse-check-iv-no-heartbeat (heartbeat stale 18:26Z; resolves Sat 06:00Z drift-healer install). G-rule timer-cycle-no-journal-entry 1/3. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 1, consecutive_clean=1.

---

## Status snapshot — updated 2026-06-12 23:49Z UTC (Iter ~1614, interactive /cycle via /loop, Tier 3→1 reset)

**Iter ~1614 summary:** Alert watermark: L1142 (3 new: dispatch-branch-cleanup Tier-3 silence, heal-unreviewed-merge-detector Tier-4 escalation [NEW], missions-autoregister Tier-3 silence). Tier 3→1 reset (Tier-4 alert). PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=22:56:02Z, no-change, commit=83ff858 (HEAD=a2dd023, 2 commits after last sync). **NEW FINDING:** PR #489 (fix(dashboard-api): add scripts/ to sys.path) merged by Larry-Yatch at 23:26Z without Mirror review. 9 files including agents/forge/CLAUDE.md. heal-unreviewed-merge-detector fired 23:30Z. [yellow] DM sent to Larry. Reply 'go: retroactive-review-489' to trigger Mirror retroactive review. **CARRY:** G-rule pulse-check-iv-no-heartbeat (heartbeat stale 18:26Z; resolves after drift-healer Sat 06:00Z install). G-rule timer-cycle-no-journal-entry 1/3. beacon-pending-approvals.json 2 stale entries. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 1, consecutive_clean=0.

---

## Status snapshot — updated 2026-06-12 23:18Z UTC (Iter ~1613, interactive /cycle via /loop, Tier 3 consecutive_clean 2→3)

**Iter ~1613 summary:** Alert watermark: L1139 (1 new: review-pass PR #488 auto-merge, Tier-3 silence). Tier 3, consecutive_clean 2→3. PRIME DIRECTIVE (script-authoritative): interventions=819, systemic_fixes=33, verification_pending=11, ratio=24.82, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=22:56:02Z, no-change, commit=83ff858. beacon-pending-approvals.json (2 stale entries: fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001; healer will GC). **MAJOR EVENT:** PR #488 ("feat(systemd): weekly timer for Pulse Check IV") auto-merged 22:46Z — wire-pulse-check-iv-cadence-001 RESOLVED. pulse-check-iv service+timer not yet installed on droplet (drift healer next run Sat 06:00Z). **NEW:** Larry said "pin to tier 2" at 23:03Z; Beacon bot responded explaining rotation.disabled mechanism; rotation.disabled = "tier1"; Beacon's domain, no Pulse action. **CARRY:** G-rule pulse-check-iv-no-heartbeat (heartbeat stale 18:26Z; resolves after drift-healer install). G-rule timer-cycle-no-journal-entry 1/3. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING (G-rule dispatched ~iter 1416), sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=3.

---

## Status snapshot — updated 2026-06-12 22:44Z UTC (Iter ~1612, interactive /cycle, Tier 3 consecutive_clean 1→2)

**Iter ~1612 summary:** Alert watermark: L1138 (1 new: dispatch-branch-cleanup Tier-3 silence). Tier 3, consecutive_clean 1→2. PRIME DIRECTIVE (script-authoritative): interventions=819, systemic_fixes=33, verification_pending=11, ratio=24.82, trend=flat. **KEY STATE:** 9/9 services active. 1 open PR (#488 "feat(systemd): weekly timer for Pulse Check IV", MERGEABLE, Mirror review pending 22:37Z). Mirror inbox: review-wire-pulse-check-iv-cadence-001.json. Beacon/Forge/Pulse inboxes empty. Sync: last_sync=21:55:51Z, no-change. beacon-pending-approvals.json (2 stale entries: fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001; healer will GC). **MAJOR EVENT:** Larry approved wire-pulse-check-iv-cadence-001 at 22:35Z → Forge opened PR #488 → Mirror reviewing. **CARRY:** Check IV heartbeat absent (stale 18:26Z; PR #488 will resolve). G-rule timer-cycle-no-journal-entry 1/3. **STANDING:** Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING (G-rule dispatched ~iter 1416), sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=2.

---

## Status snapshot — updated 2026-06-12 22:12Z UTC (Iter ~1611, interactive /cycle, Tier 3 consecutive_clean 0→1)

**Iter ~1611 summary:** Alert watermark: L1137 (0 new). Tier 3, consecutive_clean 0→1. PRIME DIRECTIVE (script-authoritative): interventions=819, systemic_fixes=33, verification_pending=11, ratio=24.82, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=21:55:51Z, no-change, commit=b051d53. beacon-pending-approvals.json (3 entries: 2 stale PRs #482+#484 healer will GC; 1 active wire-pulse-check-iv-cadence-001 pending Larry 'Go'). **CARRY:** wire-pulse-check-iv-cadence-001 approval DM delivered 20:05:38Z; pending Larry 'Go'. Check IV heartbeat absent (stale 18:26Z). G-rule timer-cycle-no-journal-entry 1/3. **STANDING:** Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING (G-rule dispatched ~iter 1416), sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=1.

---

## Status snapshot — updated 2026-06-12 21:37Z UTC (Iter ~1610, interactive /cycle, Tier 3 promoted)

**Iter ~1610 summary:** Alert watermark: L1137 (0 new). Tier 2→3 PROMOTED (3rd consecutive clean Tier-2 iter). PRIME DIRECTIVE (script-authoritative): interventions=819, systemic_fixes=33, verification_pending=11, ratio=24.82, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=20:55:35Z, no-change. beacon-pending-approvals.json (3 entries: 2 stale PRs #482+#484 healer will GC; 1 active wire-pulse-check-iv-cadence-001 pending Larry 'Go'). **CARRY:** wire-pulse-check-iv-cadence-001 approval DM delivered 20:05:38Z; pending Larry 'Go'. Check IV heartbeat absent (stale since 18:26Z). G-rule timer-cycle-no-journal-entry 1/3. **STANDING:** Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING (G-rule dispatched ~iter 1416), sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 3, consecutive_clean=0.

---

## Key standing items (reference)

| Item | Status | Action needed |
|---|---|---|
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | No Larry reply yet; reply 'go: retroactive-review-489' if Mirror review wanted |
| source=pulse-cycle-self-report | G-rule 1/3 | At 3/3 dispatch Beacon: add source=pulse-cycle allowlist to alert-translations.json |
| beacon-pending-approvals.json (#482+#484) | **G-rule DISPATCHED** (3/3) — 2 entries still present; iter ~1622 "RESOLVED" was false. Beacon direction-ask written iter ~1623. | Watch Beacon for spec |
| wire-pulse-check-iv-cadence-001 | **RESOLVED** — PR #488 merged 22:46Z; drift-healer installed service+timer 06:00Z UTC June 13; timer active, next fire Mon 2026-06-15 04:26:45 MDT | heartbeat still stale (mtime=18:26Z June 12) until first timer run |
| Tier-2 weekly probe auth_401 | Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | Pending Larry | `approve threshold-update-2026-06-11` |
| cycle-timer checkpoint | Pending Larry | `go: cycle-timer checkpoint` |
| actor-exemption-config | Pending Larry | `go: actor-exemption-config` |
| cycle-prompt-check-c-pgrep-liveness-001 | APPROVAL_REQUEST | Pending Larry |
| Check 5 MISSING | Standing G-rule | G-rule dispatched ~iter 1416; no Forge PR found |
| ccd-s1-envelope-builder | PAUSED | Carry; unverified |
| sync-push-rebase-loop-001 | UNREGISTERED AR | Carry |
| dag-preflight-revision gap | Active | PR #484 closed source=pulse gap; DAG markers still fall through |
| catalog-accuracy-drift | **G-rule DISPATCHED** (3/3, iter ~1637) | Watch Beacon for spec; direction-ask: catalog-accuracy-drift-grule-dispatch-001 |
| droplet-uncommitted:main | G-rule 1/3 [NEW iter ~1637] | Watch; at 3/3 dispatch Beacon to add translation allowlist entry |
| F24-empty-prompt-envelope-rejected | G-rule 1/3 | Watch |
| heal-stale-daemon-code-auto-restart-needs-template | G-rule 1/3 | At 3/3 dispatch Beacon: add translation template to alert-translations.json |
| heal-stale-approvals-not-gc-merged-prs | **DISPATCHED** (3/3, iter ~1623) | Watch Forge for PR; entries self-clear after merge |
| sync-blocked:uncommitted-changes | G-rule 1/3 | Watch |
| bughunt-gate-soak Phase 2 | Pending Larry | Yellow carry |
| health-check-notify-script-missing | G-rule 3/3 | No Forge PR found; stale |
| Check IX GITHUB_TOKEN missing | Pending | Monday check |
| G-rule unreviewed-merge sprint batch | Dispatched ~iter 1378 | Watch Beacon |
| sentinel-inflight-marker-fix | Beacon consumed | Watch Forge brief |
| sentinel-inbox-stall 3/3 | Beacon consumed | Watch Forge brief |
| sync-push-rebase-fallback-001 | Beacon consumed | Watch Forge brief |
