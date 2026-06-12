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

## Status snapshot — updated 2026-06-12 20:39Z UTC (Iter ~1606, interactive /cycle)

**Iter ~1606 summary:** Alert watermark: L1137 (1 new: dispatch-branch-cleanup digest, Tier-3 silence). Tier 1, consecutive_clean=2. PRIME DIRECTIVE (script-authoritative): interventions=819, systemic_fixes=33, verification_pending=11, ratio=24.82, trend=flat. **KEY STATE:** 9/9 services active. 0 open PRs. All inboxes empty. Sync: last_sync=19:55:19Z, no-change, commit=159984a. beacon-pending-approvals.json (3 entries: 2 stale PRs #482+#484 healer will GC; 1 active wire-pulse-check-iv-cadence-001 pending Larry 'Go'). **CARRY:** wire-pulse-check-iv-cadence-001 approval DM delivered 20:05:38Z; pending Larry 'Go'. Check IV heartbeat stale 18:26Z. G-rule timer-cycle-no-journal-entry 1/3. **STANDING:** Tier-2 weekly probe auth_401, Check III proposals `approve threshold-update-2026-06-11` (2 high-attention), Check 5 MISSING (G-rule dispatched ~iter 1416), sync-push-rebase-loop-001 UNREGISTERED AR, dag-preflight-revision gap, ccd-s1-envelope-builder PAUSED, catalog-accuracy-drift G-rule 2/3. **Tier:** Tier 1, consecutive_clean=2.

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
| wire-pulse-check-iv-cadence-001 | Pending Larry 'Go' | Larry approve → Forge builds timer |
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
| sync-blocked:uncommitted-changes | G-rule 1/3 | Watch |
| bughunt-gate-soak Phase 2 | Pending Larry | Yellow carry |
| health-check-notify-script-missing | G-rule 3/3 | No Forge PR found; stale |
| Check IX GITHUB_TOKEN missing | Pending | Monday check |
| G-rule unreviewed-merge sprint batch | Dispatched ~iter 1378 | Watch Beacon |
| sentinel-inflight-marker-fix | Beacon consumed | Watch Forge brief |
| sentinel-inbox-stall 3/3 | Beacon consumed | Watch Forge brief |
| sync-push-rebase-fallback-001 | Beacon consumed | Watch Forge brief |
