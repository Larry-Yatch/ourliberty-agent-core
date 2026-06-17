# projects-v3-p3-followup — make the pipeline actually flow

**Type:** Follow-up that completes P3. Born from a browser + 2-subagent audit of shipped P3 (2026-06-17).
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P3.
**Depends:** P3 (shipped #567/#568/#569/#570/#571 + dashboard #62).

---

## 0. Desired End State *(the destination)*

**A promoted item can travel the whole pipeline from the dashboard — Brainstorm → Spec → Building → Done — without touching Telegram or a terminal, and a mis-promote can be undone.**

Concretely, opening the board:
- A project at **Brainstorm** has a **"Ready to spec" button** (the checkpoint "refine → go") that advances it to **Spec**.
- A project at **Spec** has a way to **attach its spec** (point the phase at its spec doc) → the **"Launch build" button** appears.
- Clicking **Launch** dispatches the build (already works) and the phase flips to **Building**; when the build completes, it flips to **Done** — automatically, no manual status edits.
- Every pipeline project has a **Drop / Archive** control, so a mis-promote returns to the funnel (or is archived) — not a dead end.
- **Accept** on a Proposed card does the *same* thing as Promote (lands it in the pipeline), and **raw orphan** items can be promoted too.

## 1. Why now — this is the foundation, not a patch

The P3 audit found P3 shipped **the two ends of the pipeline but not the middle**: Promote drops a phase at Brainstorm, Launch demands a spec-ready phase, and **nothing advances a phase or writes its status back** — so nothing can actually flow. The store, the single-committer plumbing, and the launch→Mirror→build bridge are solid; the **connective tissue (transitions + status writeback + reversibility) was never built**, and review passed it because the done-gate wasn't tested.

This is load-bearing for the rest of the roadmap: **P4 (closeout) fires on a phase going Done; P5 (DAG status) extends the coarse status; P6 (auto-fill) sits on the Brainstorm→Spec checkpoint.** All three silently assume the machinery this follow-up builds. Shipping them on the current foundation would just hit the same wall. So this completes P3's foundation before P4+ build on it.

## 2. Scope & non-goals

**In:**
- **Phase transitions:** a checkpoint advance (Brainstorm→Spec) + spec-attach (set `spec_ref` → spec-ready), as dashboard buttons + backend endpoints.
- **Status writeback:** phase → `building` when its launch dispatches; phase → `done` on its build-sequence completing (wire to the existing SEQUENCE_COMPLETE signal + `sequence_ref`).
- **Reversibility:** a Drop/Archive endpoint (set project `archived`, item leaves "Actively working") + the dashboard control for it.
- **Promote completeness:** Proposed-lane **Accept** routes to `/api/funnel/promote`; the promote endpoint resolves **raw-orphan task_id refs** (not just capture/mission kinds).
- **Ops hardening:** the launch-queue-drain service gets a `WorkingDirectory`; the new timers/services are registered with the install-drift inventory + verified active.

**Out:**
- In-dashboard brainstorm/spec *authoring* (stays chat + spec files per North Star §0; spec-attach just points the phase at an existing spec doc — it does not edit specs).
- P4 closeout synthesis, P5 N-of-M DAG detail, P6 auto-fill, P7 generalize — still their own phases.
- Auto-advance with no human click (the checkpoint + Launch stay human gates).

## 3. Constraints & reuse *(consulted shelf/graph during P3; reuse holds)*

- **Reuse the lifecycle helpers that already exist** — `projects_store.can_transition` / `next_lifecycle_state` (`scripts/projects_store.py`) are built but unused by any route; the advance endpoint consumes them.
- **Reuse the SEQUENCE_COMPLETE signal** (P4 engine, live) + the phase's `sequence_ref` for status writeback — do not invent a new completion poller.
- **Reuse the `archived` PROJECT_STATE** — the data model + derive already treat `archived` as "returned to funnel" (`_promoted_mission_ids`, `_find_active_project_by_promoted_from`); only the route that *sets* it is missing.
- **Reuse `/api/funnel/promote`** for the Proposed-Accept fix and the orphan-source extension (one endpoint, more sources).

**Must not break / preserve:**
- **Single-committer invariant** — all store writes (advance, attach, status, archive) land on disk and are committed ONLY by `heal_projects_store.py`; endpoints + notifier are non-committers (the pattern promote already uses, hardened by #571). The dashboard writes via droplet endpoints only.
- **Launch idempotency** (per phase id) — preserved; status writeback must not re-dispatch.
- **Don't auto-drop unmerged work** — status→done fires only on a real SEQUENCE_COMPLETE for the phase's own sequence_ref, never a clock.

## 4. Risks & guardrails

- **Status writeback racing the single committer** — the notifier/healer stamps the phase on disk; only `heal_projects_store` commits. A double SEQUENCE_COMPLETE must be idempotent (done→done is a no-op).
- **Archive reversibility** — archiving a project must cleanly un-suppress the original funnel item (or clearly archive it); verify it actually reappears/clears, not just flips a flag.
- **Spec-attach validation** — attaching a non-existent spec path must fail loudly, not create an un-launchable "spec-ready" phase.
- **Ops install (the [[install-drift-timer-false-negative]] class)** — confirm `systemctl is-active` on the healer + drain timers; don't trust "merged" = "installed."

## 5. Done-gate *(BROWSER-CHECKABLE — each item is a test someone runs in the live UI; this is the fix for how P3's gate was skipped)*

Run these on the live board, in order, on a freshly-promoted throwaway project:
- [ ] **Advance:** a Brainstorm project shows a "Ready to spec / go" button; clicking it moves the phase to **Spec** (badge changes; persists on reload).
- [ ] **Attach spec:** a Spec project has an affordance to attach a spec doc; after attaching, the **"Launch build" button appears** (and did NOT appear before).
- [ ] **Launch → Building:** clicking Launch dispatches exactly one build AND the phase badge flips to **Building** (verified in the browser network tab + badge).
- [ ] **Build complete → Done:** when the dispatched build's sequence completes, the phase badge flips to **Done** on its own (no manual edit).
- [ ] **Reversibility:** a pipeline project shows a **Drop/Archive** control; using it removes it from "Actively working" (and the source item returns to the funnel or is archived). *(This also clears the leftover "Tier-alert cycle" test item — its validation case.)*
- [ ] **Proposed Accept:** clicking **Accept** on a Proposed card lands it in "Actively working" at Brainstorm (NOT the old drafting flip) and removes it from the Proposed lane.
- [ ] **Orphan promote:** Promote on a raw orphan card succeeds (no 404) and lands in the pipeline.
- [ ] **Ops:** `systemctl is-active` is green for the projects-store healer timer AND the launch-queue-drain timer/service; a live Launch end-to-end actually reaches the build team.
- [ ] **Invariant:** none of the above dirties the git tree in a way that jams sync; the dashboard creates no commits.

## 6. Breakdown (steps → DAG)

1. **p3f-phase-transitions** *(agent-core)* — **End state:** *a phase can move forward.* Add the checkpoint-advance endpoint (Brainstorm→Spec via `can_transition`/`next_lifecycle_state`) and the spec-attach endpoint (validate the spec doc exists, set `spec_ref` → spec-ready). Store writes on disk; healer commits (non-committer discipline). Unit tests for each transition + invalid-attach. *(no deps)*

2. **p3f-status-writeback** *(agent-core)* — **End state:** *the board reflects reality.* On a phase's launch, stamp `building` + its `sequence_ref`; on SEQUENCE_COMPLETE for that `sequence_ref`, stamp `done`. Wire into `outbox_notifier` / `build_sequence_advancer` (or a thin healer) via the store; idempotent. *(no deps)*

3. **p3f-reversibility-and-orphan** *(agent-core)* — **End state:** *no dead ends; every funnel source promotable.* Add the archive/drop-back endpoint (set project `archived`; un-suppress or archive the source item). Extend `/api/funnel/promote` to resolve **raw-orphan task_id** refs. Add `WorkingDirectory` to the launch-drain service unit + register the new timers/services with the install-drift inventory. Tests + an explicit install-active check. *(no deps)*

4. **p3f-pipeline-controls** *(ourliberty-dashboard)* — **End state:** *Larry drives it all from the dashboard.* Add the buttons wired to the new endpoints: checkpoint "Ready to spec", attach-spec, **Drop/Archive** on pipeline cards; fix the Spec-badge-vs-"refine" copy mismatch; render the project-level rollup status; and **route ProposedLane Accept → `/api/funnel/promote`** (replace the legacy `proposed→drafting` action). Data via the same-origin proxy client. *(dep: p3f-phase-transitions, p3f-reversibility-and-orphan)*

**DAG:** the three agent-core steps run in parallel; the dashboard step follows (it calls their endpoints). Writers serialized per §4. **Closeout MUST confirm every §5 done-gate item was checked in the live browser** — not in the data, not at merge. That confirmation is the deliverable, not a nicety.
