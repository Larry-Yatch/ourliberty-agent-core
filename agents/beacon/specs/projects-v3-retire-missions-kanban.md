# Spec: Projects Tab v3 — Retire the Missions kanban (migrate active missions → Pipeline)

**Status:** Draft for Larry's approval (forks in §4 are *recommended* — confirm)
**Owner / approver:** Larry
**Author:** Claude Code (desktop design session, 2026-06-19)
**Parent North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §0 + §7 P3 (the pipeline *replaces* the kanban; "Missions retired as an abstraction").
**Builds on (shipped):** P3 pipeline (project store + promote + launch + status writeback) · P1 retirement (funnel + proposed-drain) · P4 closeout.
**Build path:** build-sequence orchestrator — agent-core first, then `ourliberty-dashboard`.

> This is the **capstone** of P1's headline ("the board should be Funnel + Pipeline only"). P1's *spec* was data-only and explicitly deferred the UI/pipeline; the board still renders the legacy active-missions **kanban**, the last surface of the retired Missions abstraction.

---

## 0. Desired End State
Larry opens the tab and sees **Funnel + Pipeline ONLY** — no separate Missions kanban. The active multi-PR missions that still live in the kanban (`pulse-cycle-upgrade`, `rate-limit-resilience`) show as **Projects** in "Actively working" with a real Building status; already-**shipped** missions are retired (not shown as active); **nothing in-flight is lost**; `missions.json` is untouched (the migration is reversible).

## 1. Why now
P1 cleared the funnel + drained the proposed backlog; P3 built the Pipeline; P4 closeout works. The **only** legacy surface between today's board and "Funnel + Pipeline only" is the active-missions kanban (6 cards). It double-shows work already promoted (`pipeline-empty-state-hint` renders as *both* a drafting mission and an active project) and shows stale shipped missions. Retiring it finishes the §0 end-state and removes the last piece of the inert "Missions" model from the UI.

## 2. Scope & non-goals
**In:**
- A **one-time, idempotent migration** that represents each **non-shipped active mission** (`phase ∈ {drafting, ready, in_flight}`, not `archived`, not `proposed`) as a **single-phase Project** (`promoted_from {kind: "mission", mission_id}`), lifecycle mapped from the mission phase, `sequence_ref` pinned to the mission's existing build-sequence when one exists.
- **Retire the kanban UI** in the dashboard (`app/missions/page.tsx`): remove the active-missions kanban section + its now-dead plumbing (`activeMissions`, `MissionRow` import, `counts`, `isShippedToday`, `filterMatches` if unused).
- **Handle shipped missions** per §4.2.

**Out (own phases / fast-follows):** closeout authoring (P4 owns done/closeout) · live DAG N-of-M detail (P5) · brainstorm auto-fill (P6) · Programs↔Projects unification. Mapping a mission's `task_ids` to multiple lifecycle phases is **out** — a migrated mission is ONE single-phase project (its task list is a build-sequence, not Brainstorm/Spec/Building/Done phases).

## 3. Constraints & reuse *(assembly, not greenfield)*
**Reuse:**
- **`projects_store.new_single_phase_project`** + the atomic projects.json write under `_PROJECTS_INGEST_LOCK`, committed by **`heal_projects_store.py`** (the SINGLE committer) — the migration mints the **same project shape** Promote does and lets the existing committer land the delta (the `heal_missions_board_drain` precedent: the pass writes, the owning healer commits).
- **`_create_project_from_funnel` / the mission-accept path** (`dashboard_api.py` ~2923 / ~5977) — the migration reuses this so every project carries `promoted_from {kind:"mission"}`; the existing **funnel suppression** (`_promoted_mission_ids`, ~4099) and **pipeline derive** (`projects_store.build_pipeline`, ~523) then show it with **no derive change**.
- **The mission's build-sequence** (`blackboard/build-sequences/<seq>.json`, e.g. `pulse-upgrade-001`) → the phase's `sequence_ref`, so the existing **coarse Building/Done reflection** (`projects_status_writeback.py`) lights up for free.

**Must not break / preserve:**
- **Single-committer** on `projects.json` (`heal_projects_store`) — the migration is a non-committer writer; **`missions.json` is not mutated** (the mission stays as-is, suppressed by `promoted_from` exactly like an accepted proposal). See [[machine-owned-file-single-committer]].
- **Reversibility** — a migrated project is droppable/archivable back like any promoted one; since missions.json is untouched, rollback is just dropping the projects.
- **Idempotency** — keyed on `promoted_from.mission_id`, so a re-run is a no-op and an already-promoted mission (`pipeline-empty-state-hint`) is skipped (no double project).

## 4. Options & the decision *(recommended — confirm before build)*
1. **Granularity — RECOMMEND: 1 single-phase project per mission.** A mission's `task_ids` are build STEPS (PRs), not lifecycle phases; P3's model already says one-off = single-phase. Lifecycle map: `drafting→brainstorm`, `ready→spec`, `in_flight→building` (pin `sequence_ref` so status reflects truth). *(Alt — map tasks→phases — rejected: tasks aren't Brainstorm/Spec/Building/Done.)*
2. **Shipped missions — RECOMMEND: do NOT migrate as active; retire them.** The 3 shipped missions (`clarify-round-visibility`, `pm-dashboard-project-due-date`, `missions-proposed-lane-signal-hardening-001`) are done work. Recommend **(a)** leave them in `missions.json` unrendered (kanban gone → invisible, zero migration work) over **(b)** minting `done`-state projects for a "recently shipped" trace. The pipeline is for active+upcoming work; recently-shipped belongs to P4 closeout, not a permanent done-pile. **⟵ the one outcome to confirm: does Larry want recently-shipped projects visible, or invisible-once-done?**
3. **Trigger — RECOMMEND: a one-shot idempotent reconcile** (a small `migrate_missions_to_projects.py` reusing `new_single_phase_project`, committed by `heal_projects_store` on its next tick) — *not* auto-on-render, keeping the store the single source.
4. **Sequencing — RECOMMEND: migrate FIRST, verify in the browser, THEN remove the kanban** — so no in-flight work blinks out.

## 5. Risks & guardrails
- **No lost in-flight work:** verify the 2 `in_flight` missions appear in "Actively working" with a Building status **before** the kanban is removed.
- **Single-committer:** the migration writes `projects.json` via the `heal_projects_store` path only; `missions.json` is not written.
- **Reversible + idempotent:** keyed on `promoted_from.mission_id`; re-run is a no-op; rollback = drop the projects (missions.json intact).
- **Status accuracy:** pin `sequence_ref` to the real build-sequence; an in_flight mission with no resolvable sequence shows a coarse `building` without a sequence (acceptable — P5 adds detail).
- **Double-show during rollout:** `pipeline-empty-state-hint` is already a project AND a drafting mission; idempotency skips re-creating it, and removing the kanban resolves the double-show.

## 6. Done-gate *(checkable form of §0)*
- [ ] Each non-shipped active mission appears **once** in "Actively working" as a project at the mapped lifecycle state; the 2 `in_flight` ones show **Building** tied to their real build-sequence.
- [ ] Shipped missions no longer render as active (per §4.2).
- [ ] The legacy kanban section is **gone**; the board renders **Funnel + Pipeline ONLY** — verified **in the browser** (not just the data).
- [ ] `missions.json` is untouched by the migration; a re-run is a no-op; a migrated project is droppable back.

## 7. Build sequence *(finalize via DAG-preflight)*
| Step | repo | end state | depends_on |
|---|---|---|---|
| **migrate-missions-to-projects** | agent-core | one-shot idempotent reconcile mints a single-phase project per non-shipped active mission (`promoted_from.mission_id`, lifecycle mapped, `sequence_ref` pinned); shipped handled per §4.2; `missions.json` untouched; committed by `heal_projects_store` | — |
| **retire-kanban-ui** | ourliberty-dashboard | remove the active-missions kanban section + dead plumbing; board = Pipeline + Funnel(Parked·Suggested·Orphaned); verify in browser | migrate-missions-to-projects (browser-verified) |
| **(opt) derive-stop-missions** | agent-core | once nothing reads `missions[]` for the kanban, drop it from the derive response (or keep transitional) | retire-kanban-ui |

Writers serialized per §5 (migration → projects.json; UI is dashboard-only). The closeout note must confirm the Done-gate **in the browser** (the P2/P3 lesson).
