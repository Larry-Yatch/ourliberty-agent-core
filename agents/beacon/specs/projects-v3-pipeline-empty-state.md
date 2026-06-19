# projects-v3 — "Actively working" empty-state hint

**Type:** Small standalone UX polish. **Also the first live dogfood of the P4 closeout** — this phase is deliberately launched *through the board* (Promote → Ready-to-spec → Attach-spec → Launch) so that, on completion, it exercises the never-yet-run Building→Done status-writeback and the P4 closeout end-to-end.
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 (P3 pipeline UX).
**Depends:** P3 + P4 (shipped). One-off (single-phase) project.

---

## 0. Desired End State *(the destination)*

**When the "Actively working" pipeline is empty, the board shows a quiet one-line hint instead of nothing** — so a user who hasn't promoted anything yet still learns the pipeline exists and what to do, without it stealing focus from the funnel.

Concretely: with zero active projects, `PipelineSection` renders a small, low-emphasis placeholder — heading "Actively working" plus a single muted line, e.g. *"Nothing here yet — Promote an item from the funnel to start a project."* No buttons, no cards, no visual weight that competes with the funnel.

## 1. Why now

Today `PipelineSection` self-hides entirely when empty (a deliberate "keep the funnel the focus" choice). The trade-off: a first-time / empty-board user gets no signal the pipeline half of the board exists. A *subtle* hint keeps the funnel the focus while making the feature discoverable. Low-stakes and trivially reversible — which is exactly why it's the chosen first specimen to prove the P4 closeout fires on a real, board-launched phase before we bet a real feature (P5) on it.

## 2. Scope & non-goals

**In:**
- `PipelineSection` renders a quiet empty-state (heading + one muted line) when `projects.length === 0`, instead of returning nothing.

**Out:**
- Any change to the populated pipeline, phase cards, or controls.
- Any funnel change. Any backend change. Any new lifecycle state.
- Animation, illustration, or anything beyond a single muted line.

## 3. Constraints & reuse

- **Layout-only component** — `PipelineSection` "owns layout only; the parent owns network calls + toasts." Keep it that way: no new props, no fetches.
- **Match existing typography/spacing** — reuse the section's current heading treatment and the muted/secondary text token already used elsewhere on the board (e.g. the funnel's helper text). Do not introduce new color values.
- **Don't regress the populated path** — the non-empty render must be byte-for-byte unchanged.

## 4. Risks & guardrails

- **It reverses a deliberate self-hide.** Keep it genuinely subtle (muted, single line, no card chrome) so it honors the original "funnel stays the focus" intent. If anything, err quieter.
- The existing `PipelineSection.test.tsx` asserts the populated render — keep those green and add one case for the empty render.

## 5. Done-gate *(BROWSER-CHECKABLE — run live on the board)*

- [ ] **Empty state shows:** with no active projects, the board shows the "Actively working" heading + the one-line muted hint (not a blank gap).
- [ ] **Populated state unchanged:** with ≥1 active project, the section renders exactly as before (no hint line, cards as today).
- [ ] **Closeout dogfood (the real reason this phase exists):** because this phase is launched through the board, on its build's completion the phase badge flips **Building → Done**, a **closeout** is authored onto the card (plain summary + structured detail, never raw metadata), the **completion DM** lands, and any follow-ups appear in the funnel's **Suggested** lane. Confirm each live.

## 6. Breakdown (steps → DAG)

1. **pipeline-empty-state** *(ourliberty-dashboard)* — **End state:** *the empty pipeline teaches instead of hiding.* In `app/missions/components/PipelineSection.tsx`, when `projects.length === 0` render the quiet empty-state (heading + one muted line) instead of returning null. Reuse existing heading + muted-text styling; no new props/colors. Add a `PipelineSection.test.tsx` case for the empty render; keep the populated cases green. *(no deps — single step / one-off)*

**DAG:** one step. **Closeout MUST confirm every §5 item live in the browser — including the Building→Done flip and the closeout itself, since proving that machinery is this phase's whole purpose.**
