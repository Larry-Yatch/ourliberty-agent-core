# Template: Project / Phase Brainstorm

**Type:** Reusable process template (shelf-able capability)
**Owner / approver:** Larry
**Status:** Draft v1 — born from the Projects-tab-v3 design conversation, 2026-06-16
**Used by:** the Projects pipeline (see [projects-tab-v3-north-star.md](../projects-tab-v3-north-star.md)); intended to generalize to any future build.

> **Why this exists.** We run the same loop constantly — *brainstorm → spec → build → closeout* — and right now each brainstorm starts from a blank page. This template makes the brainstorm a **fixed, fast, repeatable structure**, so we drop straight into deciding. The end-state goal: the **team auto-fills** every section below from what it already knows (the code graph, the shelf, prior phases, the project's North Star), and only asks Larry the **refining questions** that are genuinely his to answer.

---

## How it's used (two layers)

- **Project brainstorm (Layer 1, once per project).** Run the full template. Output = the project's **North Star doc** (the living source of truth) + the **phase breakdown** (§7). Rare and high-value.
- **Phase brainstorm (Layer 2, per phase).** Usually a *light* pass: revisit the North Star, fold in what the prior phase's closeout taught us, and confirm the spec. A full re-run only happens when a phase genuinely needs rethinking. The system should **check at each phase boundary** whether a brainstorm is needed or we can go straight to spec → build.

**Every layer carries its own Desired End State.** The project has one; each phase has one. Read top to bottom, the project end state plus all the phase end states are the **plain-language map of the whole project** — they tell you what it's for and why each phase exists without opening a single spec.

**The bookend principle:** §0 (Desired End State) and §6 (Done-gate) are the same statement seen from two ends — start from the destination, finish by checking you reached it. Everything in between is detail that flows from §0.

---

## The template

### 0. Desired End State  *(the lead — write this first; one per project AND one per phase)*
Plain language. *When this is done, here is what is true / what the world looks like.* Describe the destination, not the work. No jargon — the sentence you'd say to someone who doesn't know the system.

This is the **plain-language layer you navigate the system by** — it answers *"why is this here / what does it do?"*, and it's deliberately distinct from the technical spec (the *how*). It's **persistent**: the project's end state and each phase's end state live on the cards / in the North Star as the top-level map, with the spec detail available on demand. Everything else in this template is detail flowing from §0.

### 1. Why now
What's forcing this — the pain, the trigger, the cost of not doing it.

### 2. Scope & non-goals
What's explicitly **in**, and just as important, what's explicitly **out** (so the build doesn't sprawl).

### 3. Constraints & reuse
What already exists that we **reuse** (shelf components, graph seams, prior-phase output) — *check the shelf/graph first, this is assembly not greenfield*. What we **must not break** (invariants, single-committer files, live contracts).

### 4. Options & the decision
The real forks. For each: the trade-off in one line. End with a **recommendation**, not a menu.

### 5. Risks & guardrails
What could go wrong, and the guardrail that contains each. Call out anything irreversible or outward-facing.

### 6. Done-gate
The **checkable form of §0**. How we'll know we're actually done — the operator-decidability bar (a human can act on the result unaided), plus tests. If you can't check it, §0 is too vague.

### 7. Breakdown
- **Project level:** the **phases** (3–8), each a coherent brainstorm→spec→build unit, with dependencies. A one-off collapses to a single phase.
- **Phase level:** the **contracts / steps** — these become the spec and the DAG build-sequence.

---

## Notes for the auto-fill end state
When the team pre-fills this:
- §0–§2 come from the candidate card + the project North Star.
- §3 comes from a shelf/graph lookup (reuse candidates + blast radius).
- §4–§5 are the team's draft reasoning; **these are where Larry's refining input matters most.**
- §6 is derived from §0.
- §7 is the proposed DAG.
Larry sees a filled draft and answers only the open forks — plain language, technical detail on demand.
