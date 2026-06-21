# projects-v3 P6 — Brainstorm template + auto-fill

**Type:** Phase 6 of the Projects-tab v3 redesign.
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P6 + §4.3/§4.4.
**Depends:** P3 (pipeline + Brainstorm checkpoint) + P4 (closeout that already pre-loads into the next phase's Brainstorm). All shipped/proven.

---

## 0. Desired End State *(the destination)*

**Starting a phase drops Larry into a brainstorm the team already pre-filled — he answers only the handful of decisions that are genuinely his, then hands off to Claude in one paste.**

Concretely, on a **Brainstorm-state** phase card:
- A **pre-filled draft of the 8-section brainstorm template** (Narrator-authored) — clearly marked an AI draft to edit, not a finished doc.
- A short **"Your decisions" list (≤5)** — the genuine choices the team won't assume for Larry (scope trade-offs, risk tolerance, priorities, key approach forks).
- A **"Copy handoff for Claude" button** that copies a **paste-ready handoff prompt** to the clipboard, so Larry goes board → Claude desktop session in one paste and Claude is instantly oriented to finalize the brainstorm + author the spec.

Net: the brainstorm is never a blank page, and the handoff to Claude is one click + one paste.

## 1. Why now

P3–P5 made the pipeline real, honest, and glanceable. The one remaining manual, blank-page step is the brainstorm at the front of every phase. P4 already pre-loads the prior phase's **closeout** into the next phase's Brainstorm checkpoint — P6 builds directly on that hook to author a full pre-filled draft, and removes the last friction (getting it into Claude) with the handoff button.

## 2. Scope & non-goals

**In:**
- **(agent-core)** Extend the **Narrator** to author, on a phase entering Brainstorm, a **draft of the 8-section brainstorm template** + a capped **"Your decisions" list (≤5)** + the **handoff-prompt payload fields** — stored on the phase (same pattern/place as the P4 closeout). Aggressive auto-fill: draft everything it reasonably can, **including a proposed phase breakdown (§7)**, from the funnel item's briefing + the North Star + (for a later phase) the prior closeout P4 pre-loads.
- **(dashboard)** Render the pre-filled draft (expandable, AI-draft styling) + the "Your decisions" list on the Brainstorm card, and a **"Copy handoff for Claude" button** that **deterministically assembles** a paste-ready handoff prompt from the stored fields and copies it to the clipboard.

**Out (v1 boundary — preserve the locked brainstorm model):**
- **No auto-spec / no auto-launch.** The pre-fill stops at *draft + decisions + handoff*. Larry (with Claude) still refines and authors the final spec, then marks Ready-to-spec. (Auto-authoring is the autonomy-dial future.)
- **No second model call on button-click** — the model already ran (the pre-fill); the handoff is deterministic string assembly.
- A deep-link that *opens* Claude with the handoff pre-loaded (zero-paste) — future nicety, client-dependent; clipboard is the robust v1.
- P7 (generalize/shelf).

## 3. Constraints & reuse *(assembly, not greenfield)*

- **Reuse the Narrator** (Beacon-owned authoring) — P6 is the Narrator extended to the brainstorm, exactly as P4 extended it to the closeout. Same store location + write discipline (single-committer; the projects-store healer commits) as the closeout.
- **Reuse P4's prior-closeout pre-load** as a primary input to the draft (feed-forward already wired).
- **Reuse the brainstorm template** ([docs/templates/project-brainstorm-template.md](../../../docs/templates/project-brainstorm-template.md)) as the 8-section shape to fill.
- **Handoff = deterministic assembly.** The "Copy handoff for Claude" payload is a fixed template wrapping the stored draft + Larry's decisions + the spec target (`agents/beacon/specs/<phase_id>.md`, repo) + a standard directive ("work with Larry to finalize this brainstorm and author the spec at <path> in <repo>; he'll mark it Ready to spec"). Assemble from the card's current state so it reflects any edits — no model call.
- **Plain-language + glanceable** (North Star §4.10): the draft and decisions read in Larry's terms; the AI-draft framing is explicit so he edits freely.

## 4. Risks & guardrails

- **Draft must not read as final.** Style + label it an AI draft; the "Your decisions" list makes clear the brainstorm isn't done. Never present the pre-fill as the spec.
- **Cap the decisions list (≤5)** — the value is *fewer* decisions, not a new checklist. If the Narrator can't reduce to ≤5, it surfaces the top ones + notes more will surface in the Claude session.
- **Thin context** (first phase, no prior closeout, sparse briefing) — degrade gracefully to a lighter draft (§0 + decisions) rather than hallucinating detail; never block the Brainstorm checkpoint.
- **Handoff completeness** — the copied prompt must be self-contained (a fresh Claude session has none of this context): include the draft, decisions, North Star ref, prior closeout (if any), and the exact spec target path + repo.
- **Don't burden the build team / don't double-call** — pre-fill rides the Narrator's existing sweep (the GC tick), like briefings/closeouts; the button does zero model work.

## 5. Done-gate *(BROWSER-CHECKABLE — run live on the board)*

- [ ] **Pre-filled draft:** a phase that has just entered Brainstorm shows a Narrator-authored 8-section draft (incl. a proposed breakdown), marked as an AI draft.
- [ ] **Decisions surfaced:** a capped **"Your decisions" list (≤5)** of the genuine choices appears with it.
- [ ] **Feed-forward:** for a phase that follows a completed one, the draft visibly draws on the prior **closeout** (P4 pre-load).
- [ ] **Handoff button:** "Copy handoff for Claude" copies a **paste-ready** prompt containing the draft + decisions + North Star/closeout refs + the exact spec target (`agents/beacon/specs/<phase_id>.md` + repo) + the standard directive; pasting it into a fresh Claude session orients it to finalize + author the spec with **no extra framing**.
- [ ] **v1 boundary held:** no spec is auto-authored and nothing auto-launches; Larry still marks Ready-to-spec.
- [ ] **Graceful thin-context:** a first phase with sparse inputs still gets a usable lighter draft + decisions, no error, checkpoint not blocked.

## 6. Breakdown (steps → DAG)

1. **p6-brainstorm-autofill-author** *(ourliberty-agent-core)* — **End state:** *every Brainstorm phase arrives pre-filled.* Extend the Narrator to author, on a phase entering Brainstorm, the 8-section brainstorm draft + capped "Your decisions" list + the handoff payload fields, stored on the phase (mirror the P4 closeout author: `scripts/projects_closeout_author.py` pattern + the Narrator + the projects-store single-committer write). Inputs: funnel briefing + North Star + prior closeout. Deterministic fallback for thin context. Tests for full + thin-context authoring. *(no deps)*
2. **p6-brainstorm-card-ui** *(ourliberty-dashboard)* — **End state:** *the brainstorm reads as a pre-filled draft you hand off in one paste.* On the Brainstorm-state phase card, render the draft (expandable, AI-draft styling) + the "Your decisions" list, and add the **"Copy handoff for Claude"** button that deterministically assembles + copies the paste-ready handoff prompt from the stored fields + spec target. Graceful when the pre-fill is absent/thin. Tests for the render + the assembled-handoff content. *(dep: p6-brainstorm-autofill-author)*

**DAG:** author (agent-core) → card UI (dashboard). **Closeout MUST confirm every §5 item live in the browser — including pasting the handoff into a real Claude session and confirming it orients with no extra framing.**
