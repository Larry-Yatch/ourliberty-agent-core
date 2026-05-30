# Multi-Builder Coordinator — Design

Status: DRAFT for Larry review (2026-05-29). Not yet building. Anchor doc for the
multi-builder scaling effort (parallel Forges/Mirrors + concurrent missions).
Author: external Claude Code, from the 2026-05-29 scaling discussion.

## 1. Problem

Forge is the throughput bottleneck and the heaviest token consumer. Scaling building
means (a) more builders (a second Forge on its own account), and (b) running 2–3
multi-step missions concurrently. Both are unsafe without coordination: two builders on a
shared codebase will grab tasks touching the same files and collide — merge conflicts,
clobbered work, wasted build spend (we saw a mild version manually with #183/#184/#185).

The coordinator is the keystone that makes parallel building safe. Without it, "two
builders" is a liability, not a speedup. With it, lifting the one-active-sequence limit and
adding Forge-2 become safe.

## 2. What already exists (build on, don't reinvent)

The coordinator generalizes pieces we already run:
- **build_sequence_advancer** — dependency sequencing within ONE mission (dispatch a step
  when its deps merge). Coordinator generalizes this to MANY missions across MANY builders.
- **auto-merge serializer** — already prevents merge-time conflicts by HOLDing a PR behind
  another that overlaps its files (held #185 behind #184). Stays as the merge-time backstop.
- **Mirror DAG-preflight** — already does static "do these parallel steps touch overlapping
  files" checks on a sequence. The coordinator does the same at dispatch time, continuously.
- **inbox-watcher + dispatch_lease** — 1-task-per-agent assignment. Coordinator replaces the
  naive "1 per agent" with footprint-aware assignment across a builder pool.
- **healers** (heal-pipeline-stall, heal-pr-auto-merge, heal-recovery-already-merged,
  worktree-cleanup) — already do PR-tree hygiene. They fold under / report to the coordinator.

So this is largely **unification + multi-builder awareness**, not greenfield.

## 3. Coordinator responsibilities

1. **Footprint-aware work assignment** — never let two *concurrent* builds touch overlapping
   files. (The heart — see § 5.)
2. **Builder routing / load-balance** — hand each dispatchable task to a free, non-conflicting
   builder (Forge-1 / Forge-2 …).
3. **Reviewer routing** — hand each finished build to a free Mirror (Mirror-1 / Mirror-2 …).
4. **PR-tree hygiene** — continuously track every open PR's state (open / mergeable /
   conflicting / stalled / orphaned); rebase DIRTY PRs; sequence merges; keep main green.
5. **Mess detection + recovery** — conflict appeared → rebase/re-dispatch; build went DIRTY;
   review drifted (the Mirror marker-drift we hit on #184) → re-classify/nudge; orphan PR
   (#174) → flag.

## 4. Architecture: deterministic core, escalate the ambiguous

Mirror the pattern already in use (deterministic healers + LLM Pulse on top):
- **Deterministic coordinator daemon** does the mechanical 90% — assignment, mutual exclusion,
  merge ordering, tree monitoring. Cheap, predictable, unit-testable, no token cost.
- **Escalate** only genuinely ambiguous cases — an auto-unresolvable merge conflict, a
  priority call between two missions competing for a builder — to Beacon (judgment) or Larry
  (values/cost). Never burn an LLM on mechanical scheduling.

State the coordinator holds (single source of truth, file-backed like the sequence files):
- **Builders**: {id, account, status (idle/building), current_task, current_footprint}.
- **Mirrors**: {id, account, status, current_review}.
- **Work queue**: dispatchable tasks + their declared footprints + mission/priority.
- **PR tree**: open PRs + state, keyed to task_id.

## 5. The claim / mutual-exclusion model (the heart)

Treat each in-flight build as holding a **claim** (a lock) on a set of paths. Assignment rule:
a task is dispatched to a free builder ONLY if its footprint does not intersect any active
claim. Otherwise it waits.

**Footprint sources, coarse → precise:**
- **Up front**: the dispatch's declared `changed_files` + spec area + target_repo. This is a
  HINT and sometimes wrong — so up-front avoidance is *coarse* (block obvious overlaps:
  same files, same repo+area).
- **After preflight**: Forge's preflight reveals the real files. The coordinator refines the
  claim then; if the refined footprint now collides with another active build, it pauses/
  serializes the later one before the build phase.
- **At merge**: the existing auto-merge serializer is the final backstop for anything the
  coarse model missed.

So the coordinator and the serializer are **complementary, not redundant**: coordinator
dodges obvious collisions early (saves wasted builds), serializer catches the residue at merge.
Honest limitation: perfect up-front conflict avoidance is impossible because footprints aren't
fully known pre-preflight — we accept coarse-up-front + precise-after-preflight + serialize-at-merge.

## 6. Builder & Mirror pools; account model

- **Builder pool**: Forge-1 (account A = Tier 1 agent), Forge-2 (account B). Each builder is
  **pinned to its own Max account** — this is what spreads token load across two separate rate
  limits (the real win, and what prevents the morning-wall pattern). Coordinator assigns to
  whichever builder is idle + non-conflicting.
- **Mirror pool**: same shape; add Mirror-2 when Mirror becomes the bottleneck (it will — see § 11).
- **Account decision (for Larry, § 9)**: a dedicated 3rd Max for Forge-2 is cleanest
  (~$200/mo). Reusing Tier 2 / personal means contention with Larry + with rotation. API for
  the token-hog defeats Max economics.

## 7. Interaction with existing components

- **Orchestrator / advancer**: evolve into (or be wrapped by) the coordinator. Missions =
  today's sequences; the coordinator schedules many of them. **This also unlocks lifting the
  one-active-sequence limit safely** — the limit exists today partly because one Forge
  serialized everything anyway; with footprint-aware multi-builder scheduling, concurrent
  missions become safe.
- **Auto-merge serializer**: unchanged; merge-time backstop.
- **Rotation (just shipped)**: for builders pinned to dedicated accounts, rotation's
  load-spreading role is **superseded** — each builder already lives on its own account. Be
  deliberate: rotation likely demotes to a *fallback* (if a builder's account walls, route its
  next task to an idle builder on a healthy account) rather than a time-share. Don't run two
  overlapping load-spreading mechanisms.
- **Healers**: tree-hygiene healers report to / are invoked by the coordinator instead of
  acting independently (avoids the late/stale alerts we saw, e.g. heal-pipeline-stall surfacing
  a 6h-old event).

## 8. Deterministic vs escalate

| Function | Owner |
|---|---|
| Assign task to free non-conflicting builder | Deterministic |
| Footprint claim / mutual exclusion | Deterministic |
| Route build → free Mirror | Deterministic |
| Sequence merges, rebase DIRTY, keep main green | Deterministic |
| Detect stalls / drift / orphans | Deterministic |
| Auto-unresolvable merge conflict | Escalate → Beacon, then Larry |
| Priority call between competing missions | Escalate → Larry (values) |
| Mission scope / what to build | Beacon (technical) / Larry (scope) — per #189 doctrine |

## 9. Open decisions for Larry

1. **Account model for Forge-2** — dedicated 3rd Max vs reuse Tier 2. (Cost vs contention.)
2. **Evolve the advancer in place vs build a new coordinator component** that subsumes it.
3. **When to scale Mirror** — at 2 Forges, do we add Mirror-2 immediately or wait until it
   demonstrably bottlenecks?
4. **Rotation's fate** — demote to builder-fallback, or keep for non-builder agents (beacon-bot,
   pulse)? (It still helps the single-account agents.)

## 10. Phased build path (each phase shippable, low-risk)

- **Phase 1 — Coordinator core, single builder, no new behavior.** Formalize claims + the
  PR-tree state model + tree-hygiene, wrapping today's advancer + serializer + healers. Ships
  with one Forge → zero behavior change, but the scheduling/monitoring is now centralized and
  multi-builder-ready. (De-risks everything downstream.)
- **Phase 2 — Add Forge-2 + builder pool + account pinning.** The capacity win. Requires the
  account decision (§ 9.1).
- **Phase 3 — Lift the one-active-sequence limit.** Now safe — run 2–3 missions concurrently;
  coordinator's mutual exclusion keeps them from colliding.
- **Phase 4 — Add Mirror-2** when review demonstrably bottlenecks.

## 11. Bottleneck progression (be honest about where it moves)

Single Forge (today) → add Forge-2 → **Mirror becomes the bottleneck** (every build still
needs one review) → add Mirror-2 → **merge/CI throughput + conflict rate** become the limit
(more parallel builds = more conflicts the serializer must resolve). The coordinator is what
keeps each of these stages safe; scaling is iterative, not one-shot.
