# North Star: Projects Tab v3 (the "Missions tab" reborn)

**Status:** Draft v1 for review — design pass, 2026-06-16. *Skeleton — tear apart before we spec the phases.*
**Owner / approver:** Larry
**Author:** Claude Code (desktop design session)
**Born from:** the Missions-is-inert verdict (2026-06-16) + the Projects-tab brainstorm.
**Supersedes / absorbs:** [meaning-layer-roadmap.md](meaning-layer-roadmap.md) (its phases fold in below) and the Missions abstraction.
**Builds on (shipped):** Phase 4 meaning layer, 4.1 Narrator schedule, **Phase S two-way sync**, the delegate-endpoint fix.
**Reuse mandate:** build durable + **shelf-able** — this whole process is a reusable capability (see §8).

> Living doc — keep current; tick the tracker (§9) as work lands. Start here when we pick the work back up.

---

## 0. Desired End State  *(the destination — everything builds from here)*

**Larry opens one tab and sees every piece of work in his world, each as a plain-English card he can act on without decoding anything.**

- **One side is the funnel:** everything not yet committed — things he parked, things the team suggests (Beacon/Medic/Pulse), and (quietly) leftover orphans. Each card says, in his terms, *what it is · why it matters · how careful to be · what I suggest*, with one-click **Delegate / Promote / Snooze / Drop / Talk-to-team** — and those clicks **dispatch real work**, never mint a dead record.
- **The other side is the pipeline:** committed work as **Projects**, each with a living **North Star doc** and 3–8 **phase cards** flowing **Brainstorm → Spec → Building → Done.** A one-off is just a single-phase project — no ceremony.
- **The board is never stale.** When a build completes, a dedicated pass writes a plain-language **closeout** back onto the card — what shipped, what changed, what we learned, what's next — so the following phase always opens with context. Running builds show their live status (running / stuck / 3-of-6) pulled from the DAG orchestrator.
- **"Missions" as a dead, inert container is gone.** The tab mirrors how the factory actually works: *idea → brainstorm → spec → DAG build → closeout*, made first-class and visible.

## 1. Why now

The Missions board is **inert** — a 2026-06-16 audit confirmed nothing reads a mission's phase to drive work; missions are labels stamped on work that happened elsewhere, and "Accept/Promote" mint records that nobody builds. The board sits at 151 missions (89% auto-churn, mostly dismissed; 9 stale legacy drafts that never moved). Larry stopped using it. Meanwhile we keep running the real loop — brainstorm → spec → DAG build — scattered across chat, loose spec files, and the orchestrator, with no single place that captures a multi-phase project and tracks it.

## 2. Scope & non-goals

**In:** the funnel (parked/suggested/orphaned), the pipeline (projects → phases → lifecycle), the universal action card, the closeout pass, DAG-status mirroring, the brainstorm template, and the Missions→Projects migration.
**Out (for now):** the dashboard-wide rollout to other tabs (Approvals/Operations/Alerts) and the full autonomy dial — those are the *payoff* phases once the capability is generic + shelved (§7 P7). Programs↔Projects unification and drag-drop remain out.

## 3. Constraints & reuse  *(assembly, not greenfield)*

**Reuse:**
- **Phase S two-way sync** (shipped) — the card↔work link + verified-merge detection the closeout pass and status mirror ride on.
- **The Narrator** (Beacon-owned authoring, 4.1) — the closeout pass is the Narrator *extended to closeout*; the funnel briefings are the Narrator spread to new lanes.
- **`human-approval-gate` + `trust_policy`** — one-click delegate + the risk dial (= the autonomy dial).
- **The build-sequence orchestrator + the DAG tab** — the "push to work" target; the tab mirrors its status.
- **The CLARIFY rails / conversation card** — Talk-to-team.
- **The brainstorm template** ([templates/project-brainstorm-template.md](templates/project-brainstorm-template.md)).

**Must not break / preserve:**
- **Single-committer invariant** on `captures.json` (the GC healer) — every writer here honors it.
- **`chain_envelope.backfill_target_repo`** uses `missions.json` as a task_id→repo lookup — the one genuine non-display dependency. **Migrate it to a chain-events-derived source before retiring `missions.json`.**
- The orphan **decision-queue** function (stop re-proposing un-built PRs) survives as the funnel's orphaned view.

## 4. The model & key decisions

1. **Two sides: Funnel + Pipeline,** bridged by **Promote = "admit into the pipeline"** (opens a phase brainstorm / dispatches a build) — the action that finally *does* something.
2. **Projects, not Missions.** Missions retired as an abstraction.
3. **North Star doc = the canonical spine** of each project; phase **cards are structured views** onto it. (Layer-1 brainstorm produces it; each phase boundary updates it.)
4. **The phase loop:** kickoff **checkpoint** ("refine or go" — shows prior closeout + North Star + proposed phase) → **spec** → **build (DAG)** → **closeout-that-feeds-forward.**
5. **Closeout = a dedicated board-side pull pass, Beacon-authored** (not the build team's job). Triggered by verified-merge / sequence-complete; reads the merged diff + PR bodies/commits + spec + North Star. Two granularities: **step = cheap auto status; phase = the LLM closeout synthesis.** Schema: *shipped · changed-vs-spec · learnings · new follow-ups (→ funnel) · impact on remaining phases · actual cost · done-gate met?* It can **spawn follow-ups back into the funnel** — closing the cycle.
6. **Gates, two levels.** Project: start · North-Star-approve · archive. Phase: **checkpoint** · **"spec good → build"** · then build/merge/closeout flow auto (risky closeouts pause for review). ~2 human touches per phase; the autonomy dial later lets safe phases auto-pass.
7. **One-off = single-phase project;** the phase layer is collapsible, so small fixes get no ceremony.
8. **Funnel arrangement:** parked + suggested **primary** (high-signal, deliberate); orphaned **secondary, auto-filtered** (low-signal machine exhaust — auto-drop the verifiably-dead). Suggestions are **multi-source** (Beacon/Medic/Pulse) through one propose interface.
9. **DAG status mirrored on cards** — orchestrator owns it; the card shows a compact running/stuck/N-of-M + last event, click-through for detail.
10. **Plain language always; technical view on demand.**

## 5. Risks & guardrails

- **Don't burden the build team** — closeout is a separate pass, not Forge/Mirror's duty (§4.5).
- **Don't auto-drop unmerged work** — the orphan auto-drop fires only on a *real terminal signal* (PR merged/closed), never a clock; anything unresolved stays and gets briefed.
- **Parallel file overlap** — these phases mostly touch `dashboard_api.py` + the GC/closeout healers + `captures.json` (single committer). The build DAG must **serialize** the writers (Phase S's preflight taught us this — expect serial, not parallel).
- **Migration safety** — retire `missions.json` only *after* the target_repo lookup has an alternate source; archive (don't delete) the legacy drafts.
- **LLM closeout fragility** — robust JSON parse + deterministic fallback; a card never shows raw metadata.

## 6. Done-gate  *(the checkable form of §0)*

- Larry can open the tab and, for any item, decide and act in plain language without decoding metadata.
- Promote/Delegate dispatch real work; no inert records are created.
- A completed phase shows a plain-language closeout within one short cycle of merge, and the next phase's checkpoint opens pre-loaded with it.
- `missions.json` is retired (or demoted to a pure lookup) with the target_repo dependency migrated; the orphan backlog is drained to the verifiably-live set.
- The capability is cataloged on the shelf with a descriptor (§8).

## 7. Phase breakdown (candidate — to confirm/spec)

Each phase leads with its **Desired End State** (plain language — *why this phase is here*); the scope under it is the technical detail.

**P1 — Funnel + Missions retirement** · *depends: — (Phase S shipped)*
- **End state:** the tab's intake shows your parked items and the team's suggestions front and center, the dead orphan clutter is cleared, and the old inert "Missions" plumbing no longer drives anything — and nothing breaks when we stop creating missions.
- *Scope:* reframe the data model (parked/suggested primary, orphaned secondary); auto-drop verifiably-dead orphans; migrate `target_repo` lookup off `missions.json`; archive legacy drafts.

**P2 — Universal action card on the funnel** · *depends: P1*
- **End state:** every item in the funnel is a plain-English card with the full one-click action set, and clicking Promote or Delegate actually sets real work in motion instead of creating a record that just sits there.
- *Scope:* spread the meaning layer + full action set (Delegate/Promote/Snooze/Drop/Talk) to orphans/suggested; Promote/Delegate **dispatch real work**; multi-source suggest interface (Beacon/Medic/Pulse).

**P3 — The pipeline (Projects → Phases)** · *depends: P1*
- **End state:** you can see committed work as Projects, each with its phases moving Brainstorm→Spec→Building→Done, and a one-off shows as a single simple card with no ceremony.
- *Scope:* project entity + North Star binding; phase cards (each with its own Desired End State); lifecycle states + the two-level gates; checkpoint UI; one-off collapse.

**P4 — Closeout pass (feed-forward)** · *depends: P3*
- **End state:** the moment a phase's build lands, a plain-language closeout appears on the card — what shipped, what changed, what's next — so the next phase opens with full context and you never reconstruct what happened by hand.
- *Scope:* dedicated board-side Narrator-extension; pull on verified-merge/sequence-complete; the §4.5 schema; spawn follow-ups; risky→review.

**P5 — DAG status on cards** · *depends: P3*
- **End state:** you can glance at a project and instantly see which phases are running, stuck, or done — without leaving the tab.
- *Scope:* mirror running/stuck/N-of-M from chain data; click-through to the orchestrator.

**P6 — Brainstorm template + auto-fill** · *depends: P3*
- **End state:** starting a phase or project drops you into a brainstorm the team already pre-filled, so you only answer the handful of decisions that are genuinely yours.
- *Scope:* bake in the template; team pre-fills North Star + brainstorm, asks Larry only refining questions.

**P7 — Generalize + shelf + dashboard-wide (later)** · *depends: P2–P6*
- **End state:** this whole card+pipeline+closeout capability is a reusable shelf component, and the other dashboard tabs (Approvals, Operations, Alerts) speak the same plain-language card language — Telegram is just a doorbell.
- *Scope:* lift to a generic shelf component w/ descriptor; adopt across Approvals/Operations/Alerts; autonomy dial.

## 8. Reuse / shelf descriptor (the durability mandate)

This entire process — **funnel → pipeline → brainstorm → spec → build → closeout** — is the reusable capability, not a Projects-tab feature. Catalog a descriptor on the [ourliberty-graph] shelf (capability statement, contracts, reuse_mode, seams, invariants) so future products (e.g. RSDPM) and the other dashboard tabs (P7) adopt it instead of rebuilding. Reuse boundary: **generic** = the card/Narrator/risk/chat/doorbell + the project/phase lifecycle + the closeout pass + the brainstorm template; **surface-specific** = which registry the items live in, the available actions, and the context the Narrator reads.

## 9. Status tracker (living)

| Phase | What | Status |
|---|---|---|
| (foundation) | Phase 4 meaning layer · 4.1 Narrator schedule · Phase S sync · delegate fix | ✅ shipped |
| P1 | Funnel + Missions retirement | ✅ shipped |
| P2 | Universal action card on funnel | ✅ shipped (+ follow-up: meaning layer on Proposed lane + reliable refresh) |
| P3 | Pipeline (Projects → Phases) | 🚧 specced — `projects-v3-p3-pipeline` |
| P4 | Closeout pass | ▫️ planned |
| P5 | DAG status on cards | ▫️ planned |
| P6 | Brainstorm template + auto-fill | ▫️ planned |
| P7 | Generalize + shelf + dashboard-wide | ▫️ planned |

_Legend: ✅ done · 🚧 in flight · ▫️ planned_
