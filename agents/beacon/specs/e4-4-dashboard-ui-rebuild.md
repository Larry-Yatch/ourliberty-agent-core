# Spec: E4.4 — Dashboard UI Rebuild (split into 3 sub-sub-phases)

**Status:** Draft (awaiting Larry approval — sub-spec of E4)
**Author:** Claude-as-Beacon (drafted 2026-05-24)
**Approver:** Larry (pending)
**Phase:** E4.4 of `docs/phase-e-plan.md` Phase E4
**Parent spec:** [agents/beacon/specs/e4-overview.md](e4-overview.md)
**Predecessors:** E4.0 + E4.1 + E4.1b + E4.2 + E4 calibration bundle (all shipped 2026-05-24)
**Successor:** E4.3 (`pm_writer` + Beacon CLAUDE.md updates) follows; reordered after E4.4 per round 5 decision (UI CRUD lets dashboard stand alone without agent OS writes).

---

## 1. Problem statement

E4.0 → E4.2 stood up Supabase + populated it with Larry's real Mission Control data (6 Programs, 28 Projects, 13 Tasks). But none of that is visible in the dashboard yet — `dashboard.ourliberty.dev` still shows the E3 read-only droplet runtime view, not PM data. Larry has explicitly said he can't give meaningful feedback on the data model OR the UX until he sees it rendered.

E4.4 rewrites the dashboard into the full PM surface defined in [e4-overview.md § 5.4](e4-overview.md#54-ui-surface): Programs grid → kanban Projects → Project detail → CRUD on everything, plus the existing E3 features (agent runtime cards, costs, healers) preserved as the "live system" section.

It's the largest single sub-phase of E4 by code volume (estimated 1500-2500 LOC across 15+ components, 5+ routes, 4+ Route Handlers, 20+ tests). Splitting into 3 sub-sub-phases keeps each Forge dispatch within the ~1000-LOC zone where chain reliability is highest AND gets Larry visual feedback faster.

**Trigger:** E4.2 fully shipped (28 projects + 13 tasks live in Supabase + manually-cleaned-up "The Thing" duplicate done) + PR #97 calibration merged.

---

## 2. Reorder: E4.4 before E4.3 (locked 2026-05-24 round 5)

The original spec sequence was E4.3 (`pm_writer` + Beacon CLAUDE.md) → E4.4 (UI). That assumed the UI would be a READER of Supabase data populated by the agent OS via `pm_writer`. With the round-4 lock that UI CRUD is in E4.4's scope (UI can write directly to Supabase via Next.js Route Handlers), the dependency flips:

- **E4.4 first** = Larry sees his data + edits it via UI. Functional standalone PM tool.
- **E4.3 second** = adds the agent OS write path. Layers cleanly on the working UI.

This also matches Larry's explicit need: visual feedback before further design choices.

---

## 3. Three sub-sub-phases — independent dispatches, sequential ship

Each sub-sub-phase is a Forge dispatch with its own preflight + build + Mirror review + auto-merge. Larry validates after each ships before the next dispatches.

### E4.4a — MVP read-only (~½ day, ~500 LOC, ~$6 LLM)

**What ships:** Programs grid landing → click into Program → Projects list view → click into Project → Tasks list view → click into Task → Task detail with Events timeline. All RENDER, no MUTATION.

**Files:**
- `app/page.tsx` rewritten — Programs grid landing replacing the E3 overview
- `app/programs/[id]/page.tsx` (NEW) — Projects list under one Program
- `app/projects/[id]/page.tsx` (NEW) — Project detail + Tasks list
- `app/tasks/[id]/page.tsx` (NEW) — Task detail with Events timeline
- `app/api/proxy/[...path]/route.ts` — preserved (still serves E3 runtime endpoints)
- NEW: `app/api/pm/programs/route.ts` + `app/api/pm/projects/route.ts` + `app/api/pm/tasks/route.ts` + `app/api/pm/events/route.ts` (all GET only in E4.4a; POST/PATCH/DELETE comes in E4.4c)
- NEW: `components/<ProgramCard>`, `<ProjectListRow>`, `<TaskListRow>`, `<EventLogRow>`, `<TaskTimeline>` (basic, not styled-to-perfection)
- NEW: `lib/types.ts` extended with `Program`, `Project`, `Task`, `Event`, `Decision` TypeScript types matching schema
- NEW: `lib/pm-queries.ts` — server-side query helpers using `getSupabaseServer()` from E4.0b
- NEW: tests for the 4 GET route handlers + the 5 components

**Preserved from E3:** `/api/proxy/*` still serves agent status / costs / healers. Those views stay accessible (will be incorporated into the new UI shell in E4.4b or made a separate "Live System" page).

**Acceptance:** Larry opens `dashboard.ourliberty.dev`, sees a grid of 6 Programs (Agent OS Development, TruPath, The Thing, AI Company, Marvin System, Personal) with correct project counts. Clicks into TruPath → sees 7 project cards. Clicks into "Voice Chat Portal" → sees project details + (empty) tasks list. Clicks into a Project with tasks → sees task list.

**Why ship this first:** This gives Larry a usable read-only PM surface within ~½ day. He can browse his data, validate the schema decisions hold up under real eyes, and surface UX concerns BEFORE E4.4b/c lock in interactions.

### E4.4b — Kanban + drag-drop (~½ day, ~400 LOC, ~$5 LLM)

**What ships:** Switch the Projects list view (inside a Program) to a kanban-by-status column layout. Tasks list (inside a Project) also kanban-by-status. Drag-drop:
- Project cards between kanban columns → status change
- Project cards onto the horizontal Program tabs at top → move to another Program
- Task cards between kanban columns → status change

**Files:**
- `app/programs/[id]/page.tsx` — replace list with kanban using `@dnd-kit/core`
- `app/projects/[id]/page.tsx` — same for tasks
- NEW: `components/<KanbanColumn>`, `<DraggableProjectCard>`, `<DraggableTaskCard>`, `<ProgramTabsAsDropTargets>`
- NEW: `app/api/pm/projects/[id]/route.ts` PATCH-only (for drag-drop status updates) — first MUTATION endpoint
- NEW: `app/api/pm/tasks/[id]/route.ts` PATCH-only
- NEW: Vitest tests for drag-drop reducer logic + PATCH handlers

**Acceptance:** Larry drags "Voice Chat Portal" from "blocked" to "inprogress" → status updates in Supabase + UI reflects optimistically. Drags "TruPath Tool 1" from TruPath tab to AI Company tab → project moves; refresh confirms persisted.

**Why second:** Once the read-only UI proves the data is right, drag-drop is the most-used interaction beyond reading. Mission Control's #1 daily use is dragging cards. Match that ergonomics fast.

### E4.4c — CRUD + forms (~1 day, ~800 LOC, ~$10 LLM)

**What ships:** All CREATE / EDIT (beyond drag-drop status) / DELETE per the spec § 5.4 "Direct UI CRUD" subsection (locked round 4):

- `+ New Program` action on grid landing → modal form
- `+ New Project` action in each Program's kanban → drawer/modal form
- `+ New Task` action in Project detail → inline row or modal
- `+ New Decision` action in Project detail sidebar → inline form
- Inline-edit on all visible Project + Task fields (click → edit → save on blur/Enter)
- Delete via overflow menu (`⋯`) with confirm modal
- Optimistic updates on all CRUD via SWR mutate pattern

**Files:**
- `app/api/pm/programs/route.ts` extended with POST/PATCH/DELETE
- `app/api/pm/projects/route.ts` extended with POST + DELETE (PATCH already in E4.4b)
- `app/api/pm/tasks/route.ts` extended with POST + DELETE
- `app/api/pm/decisions/route.ts` (NEW) GET/POST/PATCH/DELETE
- NEW: `components/<ProgramFormModal>`, `<ProjectFormDrawer>`, `<TaskFormInline>`, `<DecisionFormInline>`, `<InlineEditableField>`, `<ConfirmDeleteModal>`
- NEW: Vitest tests for each form + each route handler (mocked Supabase)

**Acceptance:** Larry clicks "+ New Project" in TruPath → fills name + reporting_brief + owner + priority → saves → card appears in kanban with optimistic update (no spinner). Clicks the project's `reporting_brief` field → it becomes editable → types → blurs → updates persist to Supabase. Clicks overflow → Delete → confirms → card disappears.

**Why third:** CRUD is the most code-heavy + most UX-decision-heavy piece. Larry's feedback from E4.4a/b informs form fields, copy, validation rules, modal-vs-drawer choices. Doing this last lets earlier feedback land before lockdown.

---

## 4. Out of scope (explicit deferrals)

- **`pm_writer` + Beacon CLAUDE.md updates** — that's E4.3, follows E4.4. UI gets ahead of agent OS write path; layered on later.
- **Comms inbox** (collapsed sidebar showing demoted-from-Telegram informational pings) — out of E4.4 scope; ships with P-1 (comms narrowing) as a separate parallel dispatch.
- **Build timeline visualization** for agent_os_build projects — out of E4.4 scope; needs Events table populated which is E4.3's job. Add as E4.4d if Larry wants it later.
- **Mobile-native app** — web is responsive; native app is Phase F at earliest.
- **Real-time updates via Supabase Realtime channels** — SWR 30 s polling is sufficient; revisit if usage shows it isn't.
- **Multi-user auth / role-based access** — single-user dashboard for now; auth still gated by the existing `X-Dashboard-Token` from E3.
- **Search across Projects/Tasks** — out of E4.4 scope; add when Larry's data grows past easy-scroll size.
- **Bulk operations** (select multiple, batch delete/move) — out of scope; not needed at current data volume.
- **Project archive/restore** — `status='dropped'` covers archive semantics; full archive lifecycle deferred.
- **Decision options_considered structured editor** — JSON-text-area in v1; structured repeater form deferred to a polish pass.

---

## 5. Architecture decisions (all inherit from E4 overview unless noted)

| Decision | Value | Reference |
|---|---|---|
| Frontend framework | Next.js 16 (already in repo) | E4.0b |
| Styling | Tailwind 4 with `@theme inline` (already in repo) | E3.2 + E4.0b |
| Server-side Supabase client | `getSupabaseServer()` helper from E4.0b | E4.0b |
| Backend split | Hybrid: droplet FastAPI for runtime, Next.js Route Handlers for PM | overview § 5.3 |
| Drag-drop library | `@dnd-kit/core` | overview § 5.4 |
| State management | SWR with `keepPreviousData` + optimistic updates via `mutate()` | overview § 5.4 (round 4) |
| Browser-side Supabase | NONE — all DB access through server-side Route Handlers | E4.0b |
| Auth | existing `X-Dashboard-Token` from E3 (server-side gated) | E3.1 |
| Test runner | Vitest (already in repo) | E3.2 |
| Test scope | unit tests for components + route handlers; mocked Supabase client | E4.0b |
| Default landing | Programs grid → tabs per Program → kanban Projects | overview § 5.4 (round 3) |
| Default project view | Kanban by task status; list as alternate | overview § 5.4 (round 3) |
| CRUD posture | + buttons, inline edit on fields, delete via overflow menu | overview § 5.4 (round 4) |
| Mutation route handlers | POST/PATCH/DELETE on `/api/pm/{programs,projects,tasks,decisions}` | this spec § 3 |

---

## 6. Larry-actions (per sub-sub-phase)

Each sub-sub-phase has the same shape: review Vercel preview deploy, validate against your real data, give thumbs-up / spec-correction notes, then I dispatch the next sub-sub-phase.

**Per ship (~10 min Larry-time):**
1. Beacon DMs you the merge.
2. Click the Vercel preview URL in the DM (you're on phone or laptop, either works).
3. Walk through the new capability:
   - **E4.4a:** browse Programs → Projects → Tasks, confirm data fidelity.
   - **E4.4b:** drag a project between status columns, drag a project between Programs, confirm persistence.
   - **E4.4c:** create a test project, edit it, delete it, do the same with a task, decision.
4. Surface anything that looks wrong (visual, layout, copy, behavior) — I draft a calibration commit.
5. Ship-or-no-ship the next sub-sub-phase.

**Total Larry-time across all 3:** ~30 min of structured review + whatever calibration noise surfaces.

---

## 7. Risks + rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| One sub-sub-phase's UI breaks existing E3 functionality | Each sub-sub-phase preserves existing routes; only adds. Vercel preview deploys let Larry compare side-by-side before promotion. | Vercel rollback to last green deployment = one click. |
| Forge dispatch fails on a sub-sub-phase | Sub-sub-phases are independent; failure doesn't roll back the others. | Re-dispatch with corrections; prior sub-sub-phases stay live. |
| Supabase RLS denies a query unexpectedly | Server-side Route Handlers use service-role (bypasses RLS); RLS only applies if/when client-side queries are added (out of scope). | Investigate specific RLS posture; no app-side change needed. |
| Drag-drop UX feels janky | Larry feedback after E4.4b before E4.4c; iterate on @dnd-kit configuration. | Disable drag-drop, fall back to right-click menu for status changes. |
| Optimistic update shows wrong state on error | SWR `mutate(undefined, { revalidate: true })` on error rollback. | Hard refresh; investigate which API call failed. |
| CRUD form fields miss something | Iterative — calibration commits add fields. | Backward-compatible schema means no migration needed for new optional fields. |
| Test suite gets too slow | Mock Supabase responses; no real network calls in CI. | Currently OK; revisit if CI runtime exceeds 60s. |
| Forge generates wrong Tailwind classes (Next.js + Tailwind 4 unfamiliarity) | E4.0b worked through this; Forge has the patterns. Vercel build catches class errors. | Calibration commit fixes. |

---

## 8. Effort + cost

| Sub-sub-phase | LLM cost | Wall clock | Larry-time |
|---|---|---|---|
| E4.4 spec PR (this doc) + Mirror review + auto-merge | ~$0.50 | ~5 min | 2 min approval |
| E4.4a — MVP read-only (Forge + Mirror) | ~$6 | ~25 min | 10 min review |
| E4.4a calibration (if needed) | ~$1 | ~10 min | 5 min |
| E4.4b — Kanban + drag-drop (Forge + Mirror) | ~$5 | ~25 min | 10 min review |
| E4.4b calibration | ~$1 | ~10 min | 5 min |
| E4.4c — CRUD + forms (Forge + Mirror; may multi-dispatch) | ~$10 (could be $15-20 if multi-dispatch) | ~45 min | 10 min review |
| E4.4c calibration | ~$1-3 | ~15 min | 5 min |
| **Total E4.4** | **~$25-35 LLM** | **~2-2.5 hours wall clock** (spread over a few sessions) | **~50-60 min Larry-time** |

---

## 9. Validation (post-each-sub-sub-phase)

### After E4.4a
- [ ] `https://dashboard.ourliberty.dev` shows Programs grid with 6 Program cards (project counts visible).
- [ ] Click into TruPath → 7 project cards visible with `reporting_brief` preview.
- [ ] Click into "Voice Chat Portal" → full project details + (empty) tasks list visible.
- [ ] Click into a project with tasks (e.g. one of the agent-OS-related ones) → tasks visible.
- [ ] `/api/pm/programs` returns 6 rows when called server-side with the dashboard token.
- [ ] E3 features (agent status, costs, healers) still accessible (route preserved or moved to new path).

### After E4.4b
- [ ] Larry drags "Voice Chat Portal" from blocked → inprogress; Supabase row reflects status change within ~1s.
- [ ] Larry drags a project to a different Program tab; persists; refresh shows it in new Program.
- [ ] Drag-drop has no visual jank on hover/drop; cursor feedback matches Mission Control's feel.
- [ ] Task drag-drop inside a project works the same as project drag-drop.

### After E4.4c
- [ ] Create a test project via "+ New Project" — appears in kanban immediately (optimistic), persists after refresh.
- [ ] Inline-edit `reporting_brief` on the test project — save on blur, persists.
- [ ] Delete the test project via overflow → confirm; row gone.
- [ ] Same flow for tasks (+ New Task, inline edit, delete).
- [ ] Create a Decision in the test project's sidebar; persists.
- [ ] Mission Control parallel-run can begin: Larry uses the new dashboard for ≥1 week without going back to MC.

---

## 10. Trigger sequence

1. **Approve this spec** → I open spec PR → Mirror reviews → auto-merge.
2. **Approve E4.4a dispatch** → I draft task envelope → drop into Forge inbox → Forge builds → Mirror reviews → auto-merge.
3. **Larry reviews E4.4a Vercel preview** → thumbs-up / calibration → go to E4.4b.
4. Repeat for E4.4b + E4.4c.
5. After E4.4c lands + your usage validates: **E4.3 dispatches** (pm_writer + Beacon updates).
6. After ≥1 week of dashboard usage: **E4.5 decommissions Mission Control**.

---

## 11. Open questions

None expected. If Forge surfaces concerns during any sub-sub-phase preflight, CLARIFY_REQUEST routes through the chain as designed.

---

*This sub-spec lives at `agents/beacon/specs/e4-4-dashboard-ui-rebuild.md`. Parent: [e4-overview.md](e4-overview.md). Predecessors: [e4-0-supabase-activation.md](e4-0-supabase-activation.md), [e4-1-schema-v1.md](e4-1-schema-v1.md), [e4-2-mission-control-migration.md](e4-2-mission-control-migration.md). Update parent doc's § 6 when each sub-sub-phase ships.*
