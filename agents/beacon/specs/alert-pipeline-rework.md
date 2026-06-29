# Alert Pipeline Rework — Build Spec (draft for Larry's review)

**Date:** 2026-06-28
**Author:** Claude (audit + design)
**Status:** APPROVED — direction + all four decisions locked by Larry 2026-06-28. Build HELD until 10pm MT (04:00 UTC 2026-06-29) for the Claude capacity reset; then Phase 1 launches as a build-sequence hands-free on the team.

**Locked decisions:** D1 = Hybrid DM gate (critical/approvals/escalations/Medic-unfixable DM instantly; routine warnings wait for Pulse promotion). D2 = retrospective pre-drafts the concrete fix on the proposed card; nothing activates without Larry's accept. D3 = ship P1 now, build P3 next, P2 alongside, P4 folds into P3's first run. D4 = spec committed to agent-core/agents/beacon/specs/; built via build-sequence.

## One-sentence intent
Nothing texts Larry until it has been *judged*; Medic *fixes* the reversible problems and *logs* them instead of narrating; and once a week the system mines everything that did reach Larry to turn recurring interruptions into permanent fixes or new automation — on the board for his accept.

## Principle (the invariant every phase serves)
One funnel → Pulse judges → Medic fixes what's fixable → only genuine needs-a-human reaches the phone → a thing that's stuck and not self-healing becomes ONE "needs you", never N pings → recurring elevations get permanently killed, not re-handled.

---

## Grounding facts (verified against current code)

- **chain_events ≠ phone DMs.** `chain_event_shipper.py:806-807` ships every `larry-alerts.jsonl` line as a `larry_alert` event regardless of route; `route` ∈ {escalate, closure, digest} is preserved in the event payload. Only `route=='escalate'` (and targeted notifications) actually DM via `beacon_telegram_bot.py:996`. ~705 of 1063 DM'd; 366 were silent digest.
- **The two alert lanes don't converge.** Healer alert → `larry-alerts.jsonl` → Beacon bot DM (gated only by the static `route` field, set by the emitting healer). Pulse escalation → `pulse-escalations.json` → `promote_alerts.py` gate → silent dashboard. Pulse's Check 0 triage reads the same `larry-alerts.jsonl` but on a 5-min poll, AFTER the bot already sent the DM — so triage can't gate the ping today.
- **Medic engine exists, parked Stage-1.** `medic_actions.py` `_act_restart` + the Stage-2 watchdog-coordination guard `_recent_peer_restart` are written and tested. Brake = `config/medic-reversible-targets.json` `restart_daemon_units`/`retrigger_inbox_targets` are empty arrays → every restart classification REFUSED → diagnose-only.
- **Pipeline-stall root cause already shipped Jun 26-27** (#716/#719 `pipeline_live_state.py` real-state probe, #715/#725 closed-PR skips, #739 noise reduction). heal-pipeline-stall and medic move in lockstep in the data (medic echoes the detector) — fixing the detector kills the echoes.

---

## PHASE 1 — Clean what you see (near-zero risk, ships standalone)

### 1a. Segregate digest-route events out of the alerts feed
- **Change:** `ourliberty-dashboard/lib/system-queries.ts` `getEscalationEvents()` (lines 81-90) — add a route filter that keeps escalation/sentinel rows (which have no route) and drops digest larry_alerts:
  `.or("payload->>route.is.null,payload->>route.neq.digest")`
  (PostgREST `payload->>field` filtering is already used in this codebase at `app/api/operations/pr-pipeline/route.ts:144`, so this is precedented.)
- Apply the same filter to `getRecentPulseCycles()` (lines 296-318) so a digest row is never chosen as a cycle's headline finding.
- **No component changes required** (`EscalationsAlertsPanel.tsx` consumes the query; `ChainEventFeed.tsx` already excludes these types).
- **CORRECTION to the audit:** the "permanently maxed unread" badge is NOT caused by digest alerts. `getPendingAttentionCount()` (system-queries.ts:343-351) counts only `approval_request` + `clarify_request` with `read_at` null — it never sees larry_alerts. So this phase cleans the **Escalations + Alerts panel** (the clutter Larry scrolls), not the needs-attention count. The stuck count, if real, is unresolved approvals whose `read_at` is never set — tracked separately as a follow-up to confirm.

### 1b. Add an "info"/FYI severity below "warning"
- **Change:** `agent-core/scripts/larry_alerts.py:77` `VALID_SEVERITIES = ('info', 'warning', 'critical')`.
- Add cooldown branch at `_cooldown_window` (line ~127): `info` → longest window (new `INFO_COOLDOWN_SEC`).
- Add an info glyph in `_render_raw_alert_body` (line ~903).
- **Make info never-DM by default:** in `append_alert` (around line 325) default `route='digest'` when `severity=='info'` (the durable self-firing rule, rather than relying on every caller).
- **Already forward-compatible (no change):** `event_briefing.py:80-82` maps `info→safe`; dashboard `approval-queries.ts:36-45` `NormalizedSeverity` already includes `"info"`; `chain_event_shipper.py` copies severity verbatim (no validation). The ONLY hard gate is `VALID_SEVERITIES`.
- **Then reclassify the housekeeping emitters** to `severity='info'` (route digest): `dispatch-branch-cleanup` "pruned N branches", `heal-stale-daemon-code` successful "auto-restarted:X", `forge-wip-redispatch` routine, `missions-autoregister` summaries, `install-healed` successes. These keep their dashboard/log trail but structurally stop being "warnings."

### 1c. Fix the cost-panel double-count (already a spawned task)
- `getSpendDeltaVsYesterday()` (system-queries.ts) counts `cost_usd` over 48h with no event_type filter, so `ceo_digest` rollup figures (one was $1,155) count as new spend. Fix = exclude ceo_digest, or stop writing cost_usd on digest events. (task_8b156836)

**Phase 1 acceptance:** Escalations+Alerts panel shows zero digest rows; a test `info` alert never DMs and lands digest; spend panel matches `sum(session_done.cost_usd)` not the digest-inflated total.

---

## PHASE 2 — Turn the fixer on + converge the lanes (moderate, reversible)

### 2a. Populate Medic's restart allowlist (activate the existing engine)
- **Change:** `config/medic-reversible-targets.json` — populate `restart_daemon_units` and `retrigger_inbox_targets` to mirror `watchdog.py AUTO_RESTART_SERVICES` (the services that already auto-restart benignly: outbox-notifier, inbox-watcher, beacon-bot, dashboard-api, etc.).
- The act path (`medic_actions.py _act_restart`) and Stage-2 guard (`_recent_peer_restart`) already exist — this is config-only activation of tested code.
- **Behavior change:** a benign service-stale alert → Medic restarts + verifies + writes ONE `medic-action-taken` ledger entry (route digest/log), instead of a diagnose-only DM. Failed restart (`still-stale-after-restart`) still escalates — that's the real signal.

### 2b. Converge the lanes — route the DM gate through judgment
**This is the architecturally heaviest change. See DECISION D1 below — recommended design:**
- Introduce a triage lane: healer-emitted `warning`/`info` alerts default to a non-DM route ("pending-triage") rather than `escalate`.
- The Beacon bot DMs immediately ONLY for: `severity=='critical'`, `approval_request`/`clarify_request`, Pulse `escalation`, and Medic "I-tried-and-couldn't" results.
- Everything else waits for Pulse Check 0 (≤5 min), which promotes to DM only if it judges it needs a human. This preserves urgency (critical/approvals/escalations are instant) while routine warnings flow through judgment.
- Add the **persistence→escalate-once rule**: if a fingerprint persists past N triage cycles without clearing, Pulse emits ONE "auto-heal stuck, needs you" escalation (not N pings). Pulse already has flood-collapse for bursts; this adds the slow-repeat case.

**Phase 2 acceptance:** a benign daemon-stale event is fixed-and-logged by Medic with no DM; a synthetic critical still DMs instantly; a warning-level healer alert does not DM until Pulse promotes it; a fingerprint repeating across cycles produces exactly one escalation.

---

## PHASE 3 — Weekly elevation retrospective (the self-improving keystone)

**Build by cloning `scripts/pulse_check_ix.py`** (operator-friction check) — it already does ~80% of the plumbing. New work is the root-cause bucketing, the 3-way classification with a confidence gate, and the resolution-join.

### Inputs (read-only, all existing)
- `chain_events` over trailing 7d (+ prior weeks for recurrence): elevations = `escalation`, `larry_alert` (route=escalate), `sentinel_alert`, `approval_request`, `needs_attention`; resolutions = `larry_action`, `clarify_response`. (Event types confirmed in `chain_event_shipper.py:98-148`.)
- `larry-alerts.jsonl` (subject-keyed) for recurrence — Check IX's `load_alerts` already reads this.
- `ceo_digest_generator.py:81-83` already pairs elevations↔resolutions (ATTENTION vs AUTO_CLEARED) — reuse as the reducer model.

### Logic
1. **Bucket** elevations by *root signature* — normalize subject (strip `:`-segments and `-YYYY-MM-DD`, like the alert-translations lookup) + source. NEW work: Check IX keys on the raw subject only; root-cause normalization is genuinely new.
2. For each bucket compute: frequency, weeks-recurring, and **how Larry resolved it** (joined from `larry_action`) — the resolution pattern is the signal for "safe to automate."
3. **Classify into 3 plans:**
   - **Automate-now** — recurring + benign + Larry consistently resolved the same way + reversible → draft the specific change (add to Medic allowlist / a Pulse auto-fix-pattern / a healer-side reconciliation) as part of the proposed mission.
   - **Fix-permanently** — recurring from a real defect → propose a build mission to kill the root cause.
   - **Keep-elevating** — irreducibly needs-human (e.g. deploy-gate posture) → leave; just confirm it's well-formed.
4. **Surface, don't act:** write each bucket+plan to the board via `POST /api/system/missions/new` → `new-mission-queue/` → drained by `heal_orphan_autoregister` (single committer). Use **`phase: 'proposed'`** so Larry gets accept/decline (accept → drafting/initiate; dismiss → `acknowledged: true`, never re-proposed). Mission schema: `{id, name, phase, brief (plain-language), spec_docs, task_ids, repo, created, proposed_by, proposed_at}`.

### Cadence & safety
- **Weekly**, self-gated by an atomic per-ISO-week artifact sentinel (Check IX pattern at `pulse_check_ix.py:708-762`) — validate the sentinel, don't just `.exists()`. Either `/cycle`-invoked weekly or a dedicated `OnCalendar=Mon` systemd timer (heavier checks use timers; see Check IV/XI units).
- Wrap `main` in `pulse_check_heartbeat.run_check('<id>', ...)` so staleness is detected.
- **Dedup/probation:** reuse `promote_alerts.py` probation pattern (`promotion-probation.json`, dedup_identity, PROMOTE/HOLD/SKIP, promoted-stays) keyed on the normalized root signature, PLUS registry-id-prefix skip (a live proposed/drafting card for the bucket = skip). A declined bucket is suppressed until it re-crosses threshold by a margin (Check III regime-change re-surface guard).

### Human-in-loop boundary
The retrospective PROPOSES and may PRE-DRAFT the fix, but **nothing activates without Larry's accept** — matches "designed and initiated for me to review and accept" and the autonomy ladder / auto_approve gate.

**Phase 3 acceptance:** a seeded recurring elevation produces exactly one proposed board card with a plain briefing and a concrete plan in the right bucket; declining it prevents re-proposal; the same bucket is not proposed twice while a card is live.

---

## PHASE 4 — Pipeline-stall root cause (mostly shipped; verify + finish)
- **Verify** the Jun 26-27 fixes (#716/#719/#725/#739) actually drove heal-pipeline-stall + medic-echo DMs toward zero — **this is literally Phase 3's first weekly run** over post-fix data, so Phase 3 absorbs the verification.
- **Finish** the one open edge: confirm the "persisted N cycles → single needs-you" rule is in place (overlaps Phase 2b's persistence rule) rather than just a quieter backstop.
- Do NOT rebuild — the root-cause primitive (`pipeline_live_state.py`) is in.

---

## DECISIONS — LOCKED (2026-06-28)

- **D1 (DM gate):** ✅ Hybrid. Critical/approvals/escalations/Medic-unfixable DM instantly; routine warning/info healer alerts wait for Pulse promotion (≤5 min).
- **D2 (automate-now scope):** ✅ Pre-draft. The retrospective drafts the concrete change and attaches it to the proposed card; merge/activation waits on Larry's accept.
- **D3 (sequencing):** ✅ Ship P1 now; build P3 next; P2 alongside/after; P4 folds into P3's first run.
- **D4 (delivery):** ✅ Spec committed to repo; built via build-sequence. **Build held until 10pm MT tonight (capacity reset).**
