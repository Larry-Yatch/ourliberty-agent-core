# projects-v3-p3-followup2 — close the pipeline edges

**Type:** Second follow-up that finishes P3. Born from a live browser verification (2026-06-18).
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P3.
**Depends:** P3 + P3-followup (shipped #573/#574/#575 + dashboard #63).

---

## 0. Desired End State *(the destination)*

**The funnel→pipeline flow works for *every* lane and *every* action, with the board reflecting the truth after each click.**

Specifically — fixing what the verification caught:
- **Accept on a Proposed card actually lands the item in the pipeline.** Today it routes to the promote endpoint and returns 200 — but **creates no project** (silent no-op). The item must appear in "Actively working" as a single-phase project at Brainstorm, like a parked Promote does.
- **Drop/Archive does what it says.** Today it removes the project from the pipeline view but the item does **not** return to the funnel as the toast ("returned to the funnel") claims, and its persisted state is murky. Drop must be honest and durable: the item returns to the funnel **or** the toast says "Archived" — and the outcome is reload-stable.
- **The board updates after every action without a manual reload.** Today advance reflects, but Accept and Drop need a page reload to show their effect.

## 1. Why now

The P3-followup fixed the big gap — the pipeline's middle now flows (advance → attach-spec → Launch-ready, all verified live). But a click-through verification found the *edges* still broken: the **Proposed lane (the 49-item, biggest intake) still can't actually reach the pipeline** — Accept routes correctly now but the promote is a no-op, so the original "proposed items go nowhere" problem is only half-closed. P4 (closeout) builds on items flowing through the pipeline, so the proposed→pipeline path must actually work first.

## 2. Scope & non-goals

**In:**
- Make the promote endpoint **actually create the project** for a proposed-mission / raw-orphan source (not just 200).
- Make **Drop/Archive** persist durably and match its message (return-to-funnel OR honest "Archived"; reload-stable).
- **Refetch the board after every pipeline/funnel action** (promote, accept, advance, attach-spec, drop) so state shows without a manual reload.
- Quiet the **/api/missions/list polling flood** observed on the board (~35 calls in one view) if it's a runaway refetch.

**Out:**
- Closeout (P4), DAG detail (P5), auto-fill (P6).
- Status-writeback CODE (shipped #574) — but its live Building→Done behavior is **verified** in this phase's done-gate (it was never exercised live).
- Any new lifecycle states or actions.

## 3. Constraints & reuse

- **Capture-promote already works end-to-end** (`_create_project_from_funnel` for `kind=capture` creates + persists the project, verified live). The proposed/orphan path must reach the same create-and-persist outcome — diff the working capture path against the proposed/orphan branch (`scripts/dashboard_api.py` promote handler ~line 2792+ and the orphan `kind=='orphan'`/`promoted_from.task_id` logic ~line 3955).
- **Single-committer invariant** — store writes land on disk; `heal_projects_store.py` is the only committer (#571/#3405003c). Any archive/persist fix keeps this; the dashboard stays a non-committer.
- **Reuse the existing same-origin proxy SWR client** for the refetch (the P2-followup pattern) — invalidate/refetch the derived query after an action mutation; don't hand-roll polling.

## 4. Risks & guardrails

- **The promote no-op is a 200** — so it fails silently. The fix must make a failed/empty promote return a real error the UI surfaces, not a green 200 that did nothing.
- **Refetch storm** — the missions/list flood suggests a refetch may already be mis-tuned; the fix must *reduce* calls (one refetch per action), not add to the storm.
- **Archive reversibility** — if return-to-funnel is chosen, un-suppressing the source (capture→parked / orphan→proposed) must not resurrect a verifiably-dead orphan; if archive-only, the toast must not claim a return.

## 5. Done-gate *(BROWSER-CHECKABLE — run live on the board)*

- [ ] **Proposed Accept lands:** click Accept on a Proposed card → it leaves the Proposed lane AND appears in "Actively working" as a Brainstorm project (verify in the pipeline payload, not just a 200).
- [ ] **Orphan promote lands:** same for a raw-orphan card.
- [ ] **Drop is honest + durable:** Drop a pipeline project → it leaves "Actively working" AND the stated outcome is true (item back in the funnel, or toast says "Archived") AND it stays gone after a reload (reload-stable; does not reappear).
- [ ] **No manual reload:** after Accept / Promote / Advance / Attach-spec / Drop, the board reflects the change without a page reload.
- [ ] **Status writeback (verify the shipped #574 live):** launch one genuine spec-ready phase → its badge flips to **Building**, and on that build's SEQUENCE_COMPLETE flips to **Done** — observed on the board.
- [ ] **Polling sane:** loading the board does not fire a flood of duplicate `/api/missions/list` calls.

## 6. Breakdown (steps → DAG)

1. **p3f2-promote-lands-proposed** *(agent-core)* — **End state:** *every funnel lane can actually enter the pipeline.* Fix the promote handler so a proposed-mission / raw-orphan source creates AND persists the single-phase project (match the working `kind=capture` path). A promote that can't resolve/create must return a real error, not a 200 no-op. Tests: promote each source kind → assert a project exists in the store + derive. *(no deps)*

2. **p3f2-archive-honest** *(agent-core)* — **End state:** *Drop does what it says, durably.* Make archive/drop-back persist (reload-stable; project stays out of the pipeline) and make the outcome match the message — implement return-to-funnel (un-suppress the source) and only then say "returned to the funnel"; otherwise say "Archived". Single-committer preserved. Tests for persistence + the chosen semantics. *(no deps)*

3. **p3f2-ui-refetch** *(ourliberty-dashboard)* — **End state:** *the board never lies about current state.* Refetch/invalidate the derived board query after every pipeline + funnel action (promote, accept, advance, attach-spec, drop) so the UI updates without a manual reload; surface a real error if a mutation returns one. Investigate + quiet the duplicate `/api/missions/list` flood. **Also reword the Brainstorm-state pipeline card guidance** — replace the vague "refine in chat, then mark it ready to spec" with copy that reflects the locked v1 model: brainstorm happens with Claude in the desktop session + author the spec, then mark ready to spec (e.g. *"Brainstorm with Claude and author the spec, then mark it ready to spec."*). *(dep: p3f2-promote-lands-proposed, p3f2-archive-honest — so the buttons hit fixed endpoints)*

**DAG:** the two agent-core steps run in parallel; the dashboard step follows. **Closeout MUST confirm every §5 item live in the browser** — including a real launch exercising Building→Done.
