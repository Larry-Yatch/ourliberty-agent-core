# Spec: Pulse Check I — optimization mode

**Status:** Approved
**Author:** Beacon (drafted 2026-05-15)
**Approver:** Larry (2026-05-15, in conversation)

## 1. Problem statement

Pulse's existing `/cycle` (every 4h) catches drift and health issues via Checks A-H but does not surface optimization opportunities. Cost anomalies, slow chains, repeated retries, recurring clarifications, and pattern-shaped improvements (templating, fast-paths) go unaddressed. D3.5 has shipped a complete chain that has now run end-to-end for ~1 week; optimization-shaped intelligence is becoming valuable. But Pulse's existing cycle is the wrong cadence for it (4h is too noisy for trend analysis) and her checks are health-shaped, not improvement-shaped.

## 2. Success criteria

- Larry receives a weekly Telegram DM every Monday morning, after Ledger's, with engineering interpretation of Ledger's findings + Pulse's own engineering signals + 1-3 concrete proposed optimizations (each tagged with effort/impact estimate).
- The DM is silent only when there is truly nothing — empty weeks produce a heartbeat ("Week of X: chain shapes nominal").
- `/optimize` on Telegram triggers an on-demand Check I run, refreshing Ledger's report first if older than 24h.
- Pulse's existing 4h `/cycle` (Checks A-H) continues unchanged; Check I is additive, not a replacement.

## 3. Users / consumers

- **Primary:** Larry. Reads the digest DM Monday morning.
- **Secondary:** Beacon, Forge, Mirror — when Pulse's proposed optimizations are concrete enough to be a dispatch (e.g. "tighten Forge's CLAUDE.md section X"), Pulse may surface them as draft proposals for Beacon to dispatch.

Downstream consumer category: Larry-internal infrastructure.

## 4. Scope (what's in)

- New Check I added to Pulse's prompt, gated to run weekly Monday morning (not every 4h cycle).
- Check I runs **after** Ledger writes his weekly report (uses a sentinel file or timestamp check — see § 7).
- **Reads:**
  - Ledger's JSON sidecar (`~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json`).
  - Pulse's own engineering signals: last 10 cycle-journal entries, dispatch retry rates (from outbox-notifier logs), chain-shape archives (`~/agents/outboxes/*/.archive/`), recurring clarification topics (Forge preflight clarifications), PR queue depths.
- **Computes:**
  - For each anomaly Ledger flagged: an engineering interpretation (why this cost more / took longer / required retries — citing specific evidence from chain archives).
  - Engineering signals Ledger doesn't see: chains with above-baseline duration, PRs stalled in review, repeated clarification topics across dispatches, pattern of dispatches that could be templated/automated.
  - 1-3 proposed optimizations per week, ranked by impact (projected $/time saved) and tagged with effort estimate (small / medium / large).
- **Emits:**
  - A digest DM to Larry containing: (1) Ledger's headline numbers repeated for context, (2) Pulse's interpretation layer, (3) the 1-3 proposed optimizations.
  - An entry appended to `runbooks/cycle-journal.md` (Check I block) for the audit trail.
- Manual trigger: `/optimize` on Telegram. If Ledger's report is >24h old, Pulse signals Ledger to refresh first, then proceeds.

## 5. Out of scope (what's deliberately not in)

- Cost capture, computation, anomaly detection (all Ledger's job).
- Production code reviews (Mirror's job).
- Per-task cost budget enforcement (already in D3.5 5d).
- Direct dispatch of optimizations to other agents — Pulse proposes; Beacon dispatches once Larry approves.
- Modifications to existing 4h `/cycle` Checks A-H — Check I is additive only.
- Recommendations that touch off-limits repos (T1 + Marvin/Pocket-Agent territory).

## 6. Acceptance criteria

- [ ] Check I fires only on Monday cycles (not every 4h) — gated by weekday + hour check at the top of the check block.
- [ ] When Ledger's JSON sidecar for the current week exists, Pulse reads it and proceeds.
- [ ] When the sidecar is missing or >7 days old, Pulse skips Check I with a journal note ("Check I skipped: Ledger report unavailable").
- [ ] When Check I produces zero findings (no anomalies, no engineering issues, no proposed optimizations), the heartbeat DM fires.
- [ ] When `/optimize` fires AND Ledger's sidecar is >24h old, Pulse triggers Ledger fresh-run before reading.
- [ ] Pulse's existing 4h Checks A-H output is unchanged on Monday cycles — Check I appends to the journal, doesn't replace.
- [ ] Optimization proposals are tagged with effort (small/medium/large) and impact (dollar or percent estimate).
- [ ] An EMERGENCY_HALT trip pauses Check I like the rest of Pulse.

## 7. Architecture sketch

Check I is a new section in `runbooks/cycle-prompt.md`, gated by weekday/hour, that runs as part of the standard cycle but only fires content on Mondays. Components:

- **Prompt update:** new `#### I. Optimization mode (weekly, Monday)` section in `cycle-prompt.md`, placed between Check H and Check G (pattern detection). Specifies: when to run, what to read, what to compute, output format.
- **Ledger handoff:** Pulse reads `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json` for the current week. If file missing AND today is Monday, Pulse waits up to 30 minutes (polling) for Ledger to finish; if still missing, journal-notes and skips.
- **On-demand path:** `/optimize` triggers a Check-I-only run. Pulse first checks Ledger's last-modified timestamp; if >24h old, writes a `ledger-refresh-request` file to a known location that Ledger watches (or triggers Ledger's systemd timer manually — exact mechanism in build).
- **Output:**
  - Telegram DM via the existing `larry_alerts.append_notification` pipeline.
  - Cycle-journal entry under a new `**Check I:**` block.
- **No new bot, no new timer.** Reuses Pulse's existing 4h cycle infrastructure.

## 8. Open questions / risks

- **First-run baseline.** Week 1 of Pulse Check I will have minimal pattern data; engineering signals computed against "last N cycles" will be sparse. Acceptable for v1; flag in the first digest. To resolve: revisit after week 2.
- **Coordination with Ledger's run.** Monday 00:00 MDT — Ledger fires first. Pulse's existing 4h timer fires at staggered times (e.g. 02:38). Pulse's first Monday cycle after Ledger's run is when Check I should activate. Mechanism: Pulse checks for sidecar presence + sentinel file. To resolve: lock in v1 build.
- **Empty-digest heartbeat shape.** Should the heartbeat reference Ledger's empty-week DM ("Ledger flagged $X total nominal — chain shapes nominal, no proposed optimizations this week") or be Pulse-pure ("chain shapes nominal — see Ledger for $ details")? v1 default: reference Ledger so Larry sees the full picture in one DM. To resolve: in build.
- **Proposed-optimization fidelity.** If Pulse proposes "tighten Forge's CLAUDE.md section X" — does the digest include a draft of the actual proposed change, or just the recommendation that Beacon dispatch the work? v1 default: recommendation only; Beacon drafts the dispatch when Larry approves. To resolve: in build.
- **Effort/impact scoring.** v1 uses small/medium/large effort + free-text impact estimate. May tighten to structured scoring after 2 weeks. To resolve: iterate after data accumulates.

## 9. Handoff package requirements

- `runbooks/cycle-prompt.md` updated with the Check I section.
- Updated `agents/pulse/MEMORY.md` (calibration note for Check I behavior, if Pulse learns anything in the first runs).
- Tests: at minimum, a unit test that exercises Check I given a synthetic Ledger sidecar and validates the digest shape.
- Runbook section in `docs/operating-manual.md` Part I describing Check I — when it fires, what triggers it, how to interpret an empty heartbeat vs a digest.
- Deploy notes: how to confirm Check I is firing on Mondays, how to manually trigger via `/optimize`.

## 10. References

- Pulse's existing infrastructure: `runbooks/cycle-prompt.md` (the Checks A-H model Check I extends), `runbooks/cycle-journal.md`.
- Ledger spec (companion): `agents/beacon/specs/ledger.md`.
- D3.5 5d cost-budget gate (Larry's existing cost enforcement primitive): `scripts/outbox_notifier.py`.
- Roadmap entry: `docs/roadmap.md`.
