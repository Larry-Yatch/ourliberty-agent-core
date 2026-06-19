# projects-v3 P5 — DAG status on cards

**Type:** Phase 5 of the Projects-tab v3 redesign. **Launched through the board** (Promote → Ready-to-spec → Attach-spec → Launch) — so its completion also serves as the first clean real-time confirmation of the P4 enriched completion DM.
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P5 + §4.9.
**Depends:** P3 (pipeline + `sequence_ref` on phases, persisted by #592) + P4 (closeout) — all shipped/proven.

---

## 0. Desired End State *(the destination)*

**Larry glances at the pipeline and instantly sees which phases are running, stuck, or done — without leaving the tab.**

Concretely, on the "Actively working" pipeline:
- A **building** phase card shows a compact live line — e.g. **`Building · step 2/3 · forge`** plus the last event — instead of just the static "Building" badge.
- A phase whose build has made **no progress** shows an early, glanceable **⚠ "looks stuck"** badge on the card (card-only — see §3; the *DM* safety net already exists in the advancer #589/#590).
- The **project header** shows an N-of-M rollup (e.g. `2/4 phases done`).
- Each card **click-throughs** to the existing Build Sequences tab (`/operations/build-sequences`) for the full DAG detail.

## 1. Why now

P3 made phases real and P4 made the board honest about *done*; P5 makes it honest about *in-flight*. With `sequence_ref` now persisted on each phase (#592), the card knows its build and can mirror the orchestrator's live status. This closes the §0 "running builds show their live status (running / stuck / N-of-6)" promise — without Larry leaving the Projects tab for the Operations tab.

## 2. Scope & non-goals

**In (ourliberty-dashboard only):**
- Render compact live build status on a building phase card: step N-of-M + current actor + last event.
- An early **⚠ card badge** when a building phase's linked sequence has been dispatched with no progress past a short, glanceable threshold (card-only signal).
- A project-header **N-of-M phases done** rollup.
- **Click-through** from a phase card / project to `/operations/build-sequences` focused on that sequence.

**Out:**
- The **real-stuck DM** — already shipped in the advancer (#589/#590): immediate alert on a bad/unbuildable target_repo, plus a 4h wall-clock backstop pause+DM for any other no-progress stall. P5 does NOT re-implement alerting; the card's ⚠ is a *glance* layer on top of that existing safety net.
- Any in-card DAG **visualization** (graph/tree) — defer to the click-through (Build Sequences tab already renders the DAG).
- Backend/derive changes, new lifecycle states, new endpoints (see §3 — client-side join).
- P6 (brainstorm auto-fill), P7 (generalize/shelf).

## 3. Constraints & reuse *(assembly, not greenfield)*

- **Client-side join — no backend change.** The Build Sequences tab (`/operations/build-sequences`) already fetches live sequence status. Reuse that same feed: the pipeline UI joins each phase's `sequence_ref` to its sequence status **on the client**, so P5 is dashboard-only and needs no `dashboard_api`/derive change. (If that feed isn't reusable as-is, prefer extending it minimally over a new endpoint.)
- **Card-only ⚠, no new pings.** The early "looks stuck" badge is a visual on the card. The *DM* path is the advancer's existing escalation (#589/#590) — do not add a second alerting path (Larry's alert-toil doctrine: one signal, no false pings).
- **Plain language + glanceable.** Match the existing card typography; the live line is one compact row, the rollup one short phrase. Technical DAG detail lives behind the click-through.
- **Don't regress P3/P4 cards.** Done/spec/brainstorm phase rendering is unchanged; this only enriches the *building* state + adds the rollup + click-through.

## 4. Risks & guardrails

- **Polling load.** Reuse the board's existing refresh/poll cadence (and the sequence feed's own) — do not add a new fast poll per card. A building phase can refresh on the existing board tick.
- **Stale/missing sequence.** A phase whose `sequence_ref` has no live sequence (already complete / not yet dispatched) must degrade gracefully to the plain badge — never error or show a broken row.
- **⚠ threshold tuning.** The card ⚠ must be short enough to be useful but not cry-wolf on a legitimately slow early build; pick a conservative-but-glanceable default (e.g. dispatched + no PR/no step progress for ~30–45 min) and keep it a single tunable constant. It is advisory only — it never pauses or pings.

## 5. Done-gate *(BROWSER-CHECKABLE — run live on the board)*

- [ ] **Live status:** while a phase is building, its card shows step **N-of-M + actor + last event** (not just "Building"), and it advances as the build progresses.
- [ ] **Rollup:** a multi-phase project header shows **N-of-M phases done**; a one-off shows none (no ceremony).
- [ ] **Stuck badge:** a building phase with no progress past the threshold shows the ⚠ "looks stuck" badge; a healthy in-progress build does **not**.
- [ ] **Click-through:** clicking a phase/project opens the Build Sequences tab focused on that sequence.
- [ ] **Graceful degrade:** a done/spec phase, or a phase whose sequence is gone, renders the normal card with no errors.
- [ ] **No new ping:** confirm P5 added no new DM path (the existing #589/#590 escalation remains the only stuck-alert source).
- [ ] **(rides on this phase) Enriched completion DM:** because P5 is launched through the board, on its build's completion confirm the **enriched closeout DM** fires live (`Phase done: [summary]. Next up…`) — the one P4 done-gate item not yet confirmed on a clean run.

## 6. Breakdown (steps → DAG)

1. **p5-dag-status-on-cards** *(ourliberty-dashboard)* — **End state:** *the pipeline shows live build status at a glance.* In the pipeline UI (`app/missions/components/PipelineSection.tsx` / `PipelinePhaseCard.tsx`), client-side-join each phase's `sequence_ref` to the existing Build Sequences status feed and render: building → `step N/M · actor · last event`; early ⚠ "looks stuck" badge (single tunable threshold, card-only); project N-of-M rollup; click-through to `/operations/build-sequences` for that sequence. Graceful degrade when no live sequence. No backend/derive change; no new alert path. Tests for the building/stuck/done/rollup/degrade render states. *(no deps — single one-off phase)*

**DAG:** one step / one-off. **Closeout MUST confirm every §5 item live in the browser — including the enriched completion DM on this phase's own completion.**
