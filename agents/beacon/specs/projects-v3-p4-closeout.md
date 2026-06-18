# projects-v3-p4 — closeout pass (feed-forward)

**Type:** Phase P4 of Projects-tab-v3. Layer-2 phase brainstorm → spec (brainstormed with Larry 2026-06-18).
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §4.5 + §7 P4.
**Depends:** P3 + P3-followup + **P3-followup-2 (must be verified live first)**. Builds on the completion engine ("P4 slice") + status-writeback (#574) already shipped.

---

## 0. Desired End State *(the destination)*

**When a phase's build lands, a plain-English closeout appears on its card on its own — what shipped, what changed from the plan, what we learned, what's next — so the next phase opens already knowing the story, and Larry never reconstructs it by hand.** Loose ends it finds become funnel suggestions so nothing's lost; and Larry gets a completion DM framed as *"done — and here's your next move."*

## 1. Why now

P3 made the pipeline *flow*; P4 makes a finished phase **tell its story and feed the next one** — closing the loop the whole tab was built around. It stands on two things already shipped: the completion signal ("P4 slice") and the status-writeback (#574, phase→Done). **Precondition:** P3-followup-2 verified live (the Done transition actually fires) and the multi-phase project flow confirmed — P4 fires on Done and feeds the *next* phase, so both must be real first.

## 2. Scope & non-goals

**In:**
- **Closeout synthesis** (Narrator-extension): on a phase reaching Done, read the merged diff + PR bodies/commits + spec + North Star and author the closeout — a plain-language summary + the structured schema (shipped · changed-vs-spec · learnings · actual cost · done-gate met?).
- **Completion DM** on every closeout: *"Phase X done: [summary]. Next up: Phase Y — ready for you to brainstorm."* The DM flags when it **needs you** (done-gate not met / build diverged from spec / a careful-or-risky follow-up).
- **Follow-ups → funnel:** loose ends the closeout finds auto-drop into the funnel's **Suggested lane** (source = "closeout"), durable, no interruption.
- **Feed-forward:** for a multi-phase project, the next phase's Brainstorm checkpoint opens **pre-loaded with the prior closeout**.
- Closeout writes the phase **card** and **ticks the North Star status tracker** (§9) for that phase.

**Out:**
- **Auto-advancing / auto-launching the next phase** — v1 keeps the human gates (brainstorm-with-Claude + the Launch button); a phase never fires the next one on its own. *That* (and the "next phase kicked off cleanly" DM) is the **autonomy-dial** increment, deliberately later (per Larry: real-work dispatch stays gated until trusted).
- Auto-rewriting the North Star doc **prose** (only the tracker status ticks; the card is the live surface).
- P5 DAG detail · P6 auto-fill · P7 generalize.

## 3. Constraints & reuse *(consulted shelf/graph 2026-06-18, graph fresh)*

**Reuse:**
- **The Narrator** (`missions_narrator`) — the closeout author is the Narrator *extended to closeout*; same authoring pattern as `ceo-digest-briefing` (LLM reads a source stream → structured, plain-language output with a deterministic fallback).
- **The completion-signal DM engine** (`outbox_notifier`, the shipped "P4 slice") — extend its SEQUENCE_COMPLETE DM with the closeout summary + next-phase handoff. Do not build a new completion poller.
- **The funnel Suggested lane + multi-source suggest** (P2 Contract C) — follow-ups land here as a new source alongside Beacon/Medic/Pulse.
- **Phase S verified-merge detection** + **status-writeback #574** — the Done trigger.

**Must not break / preserve:**
- **Single-committer** — closeout writes the card/store on disk; only `heal_projects_store` commits; the closeout pass is a non-committer to the store (the established pattern).
- **Deterministic fallback** (North Star §5) — robust JSON parse; a card **never** shows raw metadata if the LLM output is malformed.
- **Don't burden the build team** — closeout is a board-side pull pass, never Forge/Mirror's duty.
- **DM accuracy** — the DM must never claim the next phase auto-launched (it doesn't, in v1).

## 4. Decisions *(locked with Larry 2026-06-18)*

1. **Card content:** a tight **plain-English summary** (what shipped + what's next, written for the next phase) with the full structured schema **on expand**. *(Plain first, detail on demand.)*
2. **Attention:** a **completion DM on every closeout** (not silent — Larry may be waiting to launch something else; the DM is the "system is humming + here's your next move" signal), framed *"done: [summary]. Next up: [Y] ready for you to brainstorm,"* and it **flags when it needs a decision** (done-gate missed / diverged / risky follow-up). **No auto-kick of the next phase in v1** — that's the autonomy dial.
3. **Follow-ups:** **auto-drop into the funnel Suggested lane** (durable, tagged "closeout," no interruption — Promote/Dismiss whenever).

## 5. Risks & guardrails

- **LLM fragility** → deterministic fallback; never render raw metadata; the structured schema parse is fail-safe.
- **Follow-up spam** → dedup before dropping into the funnel (don't re-suggest the same loose end on a re-run).
- **DM over-claiming** → the DM states only what happened (phase done + next phase *ready*), never "auto-launched."
- **Multi-phase precondition** → if the multi-phase flow isn't solid, "feed the next phase" has nothing to feed; confirm it before P4 leans on it (verify alongside P3-followup-2).

## 6. Done-gate *(BROWSER-CHECKABLE — run live)*

- [ ] A phase reaching **Done** shows a plain-English closeout on its card (summary visible; the schema — shipped/changed/learnings/cost/done-gate-met — on expand).
- [ ] A **completion DM** arrives: *"Phase X done: [summary]. Next up: Phase Y — ready for you to brainstorm,"* and flags the case where the done-gate wasn't met / build diverged / a risky follow-up exists.
- [ ] **Follow-ups** the closeout names appear in the funnel's **Suggested lane**, tagged "closeout."
- [ ] **Multi-phase feed-forward:** the next phase's Brainstorm checkpoint shows the prior closeout pre-loaded.
- [ ] **Fallback:** a malformed-LLM closeout renders a neutral deterministic summary, never raw metadata.
- [ ] **No false auto-kick:** nothing claims the next phase launched on its own; the next phase sits at Brainstorm awaiting Larry.

## 7. Breakdown (steps → DAG)

1. **p4-closeout-author** *(agent-core)* — **End state:** *a finished phase writes its own story.* On a phase reaching Done (SEQUENCE_COMPLETE + status=done), read the merged diff + PR bodies/commits + spec + North Star and author the closeout (plain summary + structured schema) onto the phase card; tick the North Star tracker. Extend the Narrator; deterministic fallback. *(no deps)*

2. **p4-closeout-outputs** *(agent-core)* — **End state:** *Larry hears about it; loose ends are captured.* (a) Extend the SEQUENCE_COMPLETE DM into the closeout DM (summary + next-phase handoff + needs-you flags). (b) Auto-drop the closeout's follow-ups into the funnel Suggested lane (source="closeout", deduped). *(dep: p4-closeout-author)*

3. **p4-closeout-ui** *(ourliberty-dashboard)* — **End state:** *the closeout reads like English and feeds the next phase.* Render the closeout on the phase card — plain summary + detail-on-expand; pre-load the next phase's Brainstorm checkpoint with the prior closeout. *(dep: p4-closeout-author)*

**DAG:** author first → outputs + ui in parallel. Writers serialized. **Closeout MUST confirm every §6 item live in the browser** — including a real phase reaching Done that produces a closeout card + DM + a funnel suggestion.
