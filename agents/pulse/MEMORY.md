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

## Status snapshot — updated 2026-06-13 00:47Z UTC (Iter ~1619, interactive /cycle, Tier 2 consecutive_clean 1→2)

**Iter ~1619 summary:** Alert watermark: L1145 (1 new: dispatch-branch-cleanup:summary route=digest, Tier-3 silence). Tier 2, consecutive_clean 1→2. PRIME DIRECTIVE (script-authoritative): interventions=820, systemic_fixes=33, verification_pending=11, ratio=24.85, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=23:56:13Z, no-change, commit=aac2abb. beacon-pending-approvals.json: **0 entries (RESOLVED)**—healer GC'd fix-alert-triage-watermark-durability-001 + fix-depth1-pulse-approval-extraction-001 since iter ~1618. pulse-check-iv.service+timer inactive; heartbeat MISSING (expected; drift-healer fires Sat 06:00Z UTC ~5h away). **CARRY:** unreviewed-merge:489 [yellow] DM sent iter ~1614, no Larry reply. Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, G-rule pulse-check-iv-no-heartbeat 3/3 (drift-healer fires Sat 06:00Z TODAY), G-rule timer-cycle-no-journal-entry 1/3, G-rule source=pulse-cycle-self-report 1/3, G-rule heal-stale-daemon-code-auto-restart-needs-template 1/3, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 2, consecutive_clean=2.

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

## Status snapshot — updated 2026-06-12 21:18Z UTC (Iter ~1609, interactive /cycle via /loop)

**Iter ~1609 summary:** Alert watermark: L1137 (0 new). Tier 2, consecutive_clean 1→2. PRIME DIRECTIVE (script-authoritative): interventions=819, systemic_fixes=33, verification_pending=11, ratio=24.82, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=20:55:35Z, no-change, commit=b5d304c. beacon-pending-approvals.json (3 entries: 2 stale PRs #482+#484 healer will GC; 1 active wire-pulse-check-iv-cadence-001 pending Larry 'Go'). **CARRY:** wire-pulse-check-iv-cadence-001 approval DM delivered 20:05:38Z; pending Larry 'Go'. Check IV heartbeat absent (stale since 18:26Z). G-rule timer-cycle-no-journal-entry 1/3. **STANDING:** Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING (G-rule dispatched ~iter 1416), sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 2, consecutive_clean=2.

---

## Status snapshot — updated 2026-06-12 19:39Z UTC (Iter ~1603, /cycle)

**Iter ~1603 summary:** Alert watermark: L1135 (2 new: dispatch-branch-cleanup:summary + missions-autoregister:summary, both digest). Tier 2, consecutive_clean=1→2. PRIME DIRECTIVE (script-authoritative): interventions=818, systemic_fixes=33, verification_pending=10, ratio=24.79, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=18:55:16Z, no-change, commit=105fb0c. beacon-pending-approvals.json present (2 stale entries for merged PRs #482+#484; healer will GC; v1 schema). pulse-check-iv.heartbeat present (mtime=18:26Z). **VERIFIED:** PR #485 MERGED 16:56:37Z; PR #487 (fix(alert-triage): intent fallback) MERGED 18:23:12Z. **STANDING:** Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, Check 5 MISSING, sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift 2/3, pulse-check-iv-no-heartbeat 1/3. **Tier:** Tier 2, consecutive_clean=2.

---

## Status snapshot — updated 2026-06-12 18:30Z UTC (Iter ~1576, /cycle)

**Iter ~1576 summary:** Alert watermark: L1131 (no new). Tier 1, consecutive_clean=0. PRIME DIRECTIVE: interventions=819, systemic_fixes=33, ratio=24.79, trend=flat. **KEY STATE:** 9/9 active. 0 open PRs. All inboxes empty. Sync no-change. **MAJOR EVENT:** PR #487 (fix(alert-triage): intent fallback when subject=None) merged 18:23:12Z — resolves recurring outbox-notifier:review-pass Tier-4 misclassification arc. **NEW G-RULE:** pulse-check-iv-no-heartbeat 1/3. **STANDING:** Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11`, Check 5 MISSING (G-rule dispatched ~iter 1416), dag-preflight-revision gap. **Tier:** Tier 1, consecutive_clean=0.

---

## Key standing items (reference)

| Item | Status | Action needed |
|---|---|---|
| unreviewed-merge:489 | [yellow] DM sent iter ~1614 | No Larry reply yet; reply 'go: retroactive-review-489' if Mirror review wanted |
| source=pulse-cycle-self-report | G-rule 1/3 | At 3/3 dispatch Beacon: add source=pulse-cycle allowlist to alert-translations.json |
| beacon-pending-approvals.json (#482+#484) | **RESOLVED** — 0 entries (GC'd by healer after iter ~1618) ✅ | Closed |
| wire-pulse-check-iv-cadence-001 | **RESOLVED** — PR #488 merged 22:46Z | drift-healer will install timer Sat 06:00Z |
| Tier-2 weekly probe auth_401 | Pending Larry | docs/runbooks/rotate-claude-setup-tokens.md |
| Check III threshold proposals | Pending Larry | `approve threshold-update-2026-06-11` |
| cycle-timer checkpoint | Pending Larry | `go: cycle-timer checkpoint` |
| actor-exemption-config | Pending Larry | `go: actor-exemption-config` |
| cycle-prompt-check-c-pgrep-liveness-001 | APPROVAL_REQUEST | Pending Larry |
| Check 5 MISSING | Standing G-rule | G-rule dispatched ~iter 1416; no Forge PR found |
| ccd-s1-envelope-builder | PAUSED | Carry; unverified |
| sync-push-rebase-loop-001 | UNREGISTERED AR | Carry |
| dag-preflight-revision gap | Active | PR #484 closed source=pulse gap; DAG markers still fall through |
| catalog-accuracy-drift | G-rule 2/3 | Watch; at 3/3 dispatch Beacon |
| F24-empty-prompt-envelope-rejected | G-rule 1/3 | Watch |
| heal-stale-daemon-code-auto-restart-needs-template | G-rule 1/3 | At 3/3 dispatch Beacon: add translation template to alert-translations.json |
| sync-blocked:uncommitted-changes | G-rule 1/3 | Watch |
| bughunt-gate-soak Phase 2 | Pending Larry | Yellow carry |
| health-check-notify-script-missing | G-rule 3/3 | No Forge PR found; stale |
| Check IX GITHUB_TOKEN missing | Pending | Monday check |
| G-rule unreviewed-merge sprint batch | Dispatched ~iter 1378 | Watch Beacon |
| sentinel-inflight-marker-fix | Beacon consumed | Watch Forge brief |
| sentinel-inbox-stall 3/3 | Beacon consumed | Watch Forge brief |
| sync-push-rebase-fallback-001 | Beacon consumed | Watch Forge brief |
