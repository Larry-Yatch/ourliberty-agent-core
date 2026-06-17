# projects-v3-p3 — the pipeline (Projects → Phases)

**Type:** Phase P3 of the Projects-tab-v3 project. Layer-2 phase brainstorm → spec.
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P3.
**Depends:** P1 (shipped). Independent of the P2 follow-up.
**Brainstormed with Larry:** 2026-06-17 (decisions in §4).

---

## 0. Desired End State *(the destination — everything builds from here)*

**Larry opens the tab and sees his committed work as Projects on a "pipeline" side, each a few phase cards flowing Brainstorm → Spec → Building → Done — and he moves work forward with buttons on the dashboard, never by copy-pasting into Telegram.**

- **Promote is a move, not a record.** Clicking Promote (or Accept, on a Proposed card) **relocates** a funnel item out of its lane (parked / suggested / orphaned) and into an **"Actively working"** section, where it enters the cycle at **Brainstorm**. The funnel lane no longer shows it — it's now *in* the pipeline. No inert duplicate is minted.
- **One model for everything.** A big multi-phase effort and a tiny one-off are the same entity — a Project with one or more phases. A one-off renders as a single simple collapsed card; no ceremony.
- **Every phase says why it exists.** Each phase card carries its own plain-language **Desired End State** (the "why is this here" line), distinct from its spec.
- **You drive it from the dashboard.** The gates are **dashboard buttons** — the kickoff *checkpoint* ("refine or go"), and **"Launch build"** at the Spec→Building boundary. Clicking Launch dispatches the build to the team; **Telegram is never in the loop.** The same Launch path works for any build, not just pipeline phases.

> **Out of scope for P3** (their own phases): the feed-forward **closeout** (P4), the **live DAG detail / N-of-M** on cards (P5 — P3 shows only a coarse Building/Done status), and the **team auto-fill** of brainstorms (P6 — in P3 the brainstorm/spec are authored the way they are today, in chat + spec files; P3 makes the *states and transitions* real, not the authoring surfaces).

## 1. Why now

P1 + P2 made the funnel real and plain-English, but the **pipeline side does not exist yet** — promoted work has nowhere to go, and every build is still launched by hand-pasting a message into Telegram (we just did exactly that for the P2 follow-up). P3 is the structural heart of the whole tab: it turns "a board of labels" into "the loop the factory actually runs." It also retires the last manual-Telegram-launch step. Building it now, before the closeout/status/auto-fill phases, gives those phases a real pipeline to attach to.

## 2. Scope & non-goals

**In:**
- A Project + Phase entity and a **clean single-committer store** (not the retired `missions.json`, and deliberately **separate from the Programs `pm-data-model`** — unification stays out per North Star §2).
- The derive/API surface that serves the "Actively working" pipeline to the dashboard.
- **Promote = relocate** a funnel item into the pipeline as a new single-phase project at Brainstorm; remove it from the funnel lane. Unify the Proposed-lane **Accept** onto the same gesture.
- The **"Actively working" pipeline UI**: project cards, phase cards with lifecycle (Brainstorm → Spec → Building → Done) + each phase's Desired End State, one-off collapse, and the **dashboard gate buttons**.
- The **dashboard→team dispatch bridge**: the **Launch build** button enqueues a launch request to the droplet; Beacon authors the build-sequence from the phase's spec and kicks Mirror preflight → build. Dashboard stays a **non-committer** (queues a request file like `+New mission`; never commits).

**Out:**
- Closeout pass (P4), live DAG N-of-M detail (P5), brainstorm auto-fill (P6), generalize/shelf + dashboard-wide (P7).
- In-dashboard brainstorm/spec *authoring* surfaces (those are how-we-work-today + P6).
- "Add as a phase to an existing project" — fast-follow; P3's Promote always creates a new single-phase project (a picker + merge is a later increment).
- Auto-dispatch with no human click — the Launch button is the gate; full auto is an autonomy-dial increment later.
- Programs↔Projects unification, drag-drop.

## 3. Constraints & reuse *(assembly, not greenfield — consulted shelf+graph 2026-06-17, graph fresh)*

**Reuse (verdicts from `build_check`):**
- **Build-sequence authoring/kick — verdict STRONG.** `build_sequence_validator` (schema + DAG validator), `sequence_shortcut_helpers` (the single library of legal sequence mutations), `outbox_notifier` (advances steps + auto-merge), `mirror_review_handler` (preflight/review). The Launch bridge mostly *wires these* — Beacon already authors + kicks sequences; we replace the human Telegram tap with a queued request it drains.
- **Dashboard→droplet queue (non-committer) — net-new glue, proven precedent.** The `+New mission` flow (`blackboard/new-mission-queue/<id>.json` drained by `heal_orphan_autoregister`, agent-core #533) is the pattern: dashboard POSTs → droplet writes a queue file → a healer drains it. The Launch request reuses this exact shape (a `build-launch-queue/`), so the dashboard never commits and the single-committer rule holds.
- **Dashboard surface:** `dashboard_api` (the HTTP surface that materializes the board) + `dashboard-api-client` (same-origin `/api/proxy/*` SWR hooks — the funnel UI must use this, per the P2-followup fix).
- **Phase S two-way sync** (shipped) — the card↔work link the Building-status reflection rides on.
- **The funnel** (P1/P2, live) — Promote lifts items out of it.

**Must not break / preserve:**
- **Single-committer invariant** — the new projects store has exactly ONE committer (a GC-style healer); the dashboard and Beacon are non-committers to it. (See [[machine-owned-file-single-committer]].)
- **Serialize the writers** — these steps touch `dashboard_api.py`, the new store, and the funnel derive; the build DAG must serialize, not parallelize, writers (Phase S's preflight lesson).
- The funnel's orphan **decision-queue** behavior — Promote removing an item must not resurrect it as an un-built orphan re-proposal.

## 4. Options & the decision *(the forks Larry settled, 2026-06-17)*

1. **P3 altitude — DECIDED: skeleton + dispatch bridge only.** Build the project/phase structure, lifecycle, gates, one-off collapse, and the Launch bridge; reflect build status coarsely. Defer closeout/DAG-detail/auto-fill to P4–P6. *(Bundling them makes P3 a monster; each is independently valuable.)*
2. **Promote behavior — DECIDED (Larry's framing): Promote is a MOVE.** It relocates the funnel item into "Actively working," entering the cycle at Brainstorm, and removes it from the funnel. Always creates a new single-phase project; "add to existing" is a fast-follow. Proposed-lane **Accept** is the same gesture. *(No duplication, no inert records — the item is physically in the pipeline.)*
3. **One model vs two — DECIDED: one model.** Everything is a Project; a one-off is a 1-phase project rendered simply. *(Singular, shelf-able, no second code path.)*
4. **Launch — DECIDED: a dashboard button, never Telegram.** The human gate stays, but it's a **"Launch build"** button on the phase card that dispatches via the queue→Beacon bridge. All pipeline gates are dashboard buttons; Telegram is a doorbell only. *(Kills the copy-paste for every build; keeps real-work dispatch gated behind a human click until the autonomy dial is trusted.)*

## 5. Risks & guardrails

- **Dispatch bridge correctness** — a Launch click must dispatch *exactly one* build for the phase (idempotent: a launch request carries the phase id; the drain de-dups so a double-click or a re-drain can't double-dispatch). Reuse `sequence_shortcut_helpers` legal-mutation guarantees.
- **Non-committer discipline** — the dashboard writes only a queue request via the droplet endpoint; it never touches the repo or the projects store directly (the `+New mission` precedent). Avoids the sync-jam class.
- **Store ownership** — exactly one committer for the projects store; Beacon/dashboard tolerate-but-don't-co-commit. (The #409→#413 dual-committer data-loss class.)
- **Promote irreversibility** — moving an item out of the funnel should be reversible (a project at Brainstorm can be dropped back / archived), so a mis-Promote isn't a dead end.
- **Coarse status only** — P3's Building/Done reflection reads a cheap signal (is there an active/complete build-sequence for the phase?); it must not try to render full DAG detail (that's P5) or it will sprawl.

## 6. Done-gate *(the checkable form of §0)*

- [ ] Promote on a parked/suggested/orphaned card (and Accept on a Proposed card) removes it from the funnel and shows it in "Actively working" as a new project at Brainstorm.
- [ ] A project shows its phase card(s) with lifecycle state and each phase's plain-language Desired End State; a one-off shows as a single simple collapsed card.
- [ ] Gates are dashboard buttons (checkpoint "refine or go"; "spec good → Launch"); none require Telegram.
- [ ] Clicking **Launch build** on a spec-ready phase dispatches the build to the team (Beacon authors the sequence, Mirror preflight runs, the build proceeds) — verified end-to-end **in the browser**, with no Telegram step and exactly one build dispatched.
- [ ] A phase in Building shows a coarse Building status; on sequence-complete it shows Done.
- [ ] Single-committer + non-committer discipline intact (no dirty-tree sync jam; dashboard creates no commits).

## 7. Breakdown (steps → the DAG build-sequence)

1. **p3-project-store** *(agent-core)* — **End state:** *the pipeline has a place to live.* A Project + Phase data model and a clean single-committer store (project = North Star ref + ordered phases; phase = own Desired End State + lifecycle state + optional spec-doc ref + optional build-sequence ref). Extend the derive/API to expose the "Actively working" pipeline. Single committer (GC-style healer); dashboard + Beacon are non-committers. *(no deps)*

2. **p3-promote-endpoint** *(agent-core)* — **End state:** *Promote moves an item into the pipeline.* Endpoint that relocates a funnel item (parked/suggested/orphaned/proposed) into a new single-phase project at Brainstorm and removes it from the funnel lane; reversible (droppable back). Unify Proposed **Accept** onto this. *(dep: p3-project-store)*

3. **p3-launch-queue-drain** *(agent-core)* — **End state:** *a dashboard click can launch a real build.* A `build-launch-queue/` request shape + a drain (Beacon-side) that authors the build-sequence from the phase's spec via `build_sequence_validator` + `sequence_shortcut_helpers`, runs Mirror preflight, and kicks the build. Idempotent on phase id (no double-dispatch). Dashboard writes the request via a droplet endpoint only (non-committer, `+New mission` precedent). *(dep: p3-project-store)*

4. **p3-pipeline-ui** *(ourliberty-dashboard)* — **End state:** *Larry sees and drives the pipeline.* The "Actively working" section: project + phase cards with lifecycle (Brainstorm→Spec→Building→Done) + each phase's Desired End State + one-off collapse, and the **dashboard gate buttons** (checkpoint; **Launch build**) wired to the agent-core endpoints. Promote/Accept buttons call p3-promote-endpoint; Launch calls the queue endpoint. Data via the same-origin proxy SWR client. *(dep: p3-project-store, p3-promote-endpoint, p3-launch-queue-drain)*

**DAG shape:** store first → promote-endpoint + launch-queue-drain in parallel → pipeline-ui last. Writers serialized per §5. Closeout note must confirm the Done-gate was checked **in the browser**, not just in the data (the P2 lesson).
