# Spec: Phase E4 — Unified PM Dashboard + Supabase + Chain Discipline (Overview)

**Status:** Draft v1 (awaiting Larry approval — mark up in place)
**Author:** Claude-as-Beacon (drafted 2026-05-24)
**Approver:** Larry (pending)
**Phase:** E4 of the Phase E plan (`docs/phase-e-plan.md`)
**Predecessor:** E3 (read-only dashboard) complete 2026-05-21 — PR #62 + ourliberty-dashboard PR #1 + Caddy/Cloudflare/Vercel ops
**Successors:** None planned — Phase E closes with this work. Phase F territory after.

This is the OVERVIEW doc only. Sub-phase specs (E4.0 Supabase activation, E4.1 schema, E4.2 migration script, E4.3 backend, E4.4 UI rebuild, E4.5 agent integration) get drafted AFTER Larry signs off on this. Same approach as E3 (overview-then-subspecs).

---

## 1. Problem statement (what triggered this)

After ~6 hours of real E3 dashboard usage on 2026-05-21 → 2026-05-24, Larry surfaced the actual gap E4's "1 week of usage" trigger was waiting for:

> *Telegram is doing two incompatible jobs at once. State management (what's running, where each build is in its lifecycle, what's stuck) and async comms (clarifications, approvals, escalations). When more than one build is in flight, the comms channel drowns the state channel. The result is that the chat-as-database antipattern Telegram has always been gets visible.*

He also surfaced a second insight: **his personal project management** (currently in Marvin Mission Control, a Node.js + JSON-files app on his Mac Mini, accessed via Tailscale) shares fundamental primitives with what an agent-OS build management surface would need — Programs, Projects, Tasks, status, blockers, owners, deadlines, history. **Two PM surfaces don't make sense; one unified PM surface does.**

Therefore E4 is not "interactive dashboard with kick/halt controls" (the original sketch). It's:

1. **A unified PM dashboard** at `https://dashboard.ourliberty.dev` that hosts Larry's personal projects + the agent OS's build initiatives in the same Programs > Projects > Tasks model.
2. **Backed by Supabase** as a system primitive — first real activation of the Postgres + auth layer that all future prototypes will share.
3. **Narrows Telegram** to actionable comms only (clarifications, approvals, escalations, watchdog alerts), demoting informational chatter (build started / merged / deployed) to the dashboard.
4. **Adopts four Sage workflow patterns** from Joe's `gm-agent-core` upstream that strengthen chain discipline (Plan-First Protocol, manifest+polling formalization, adversarial fix auditor, Promise-Before-Work + Unified Verifier + Sweep ledger).

This is the largest single phase in the E plan by scope. It's also the highest-leverage — once it's done, the agent OS is a real shippable platform (spec → build → preview → dashboard → backed by Postgres + auth out of the box) and Larry's PM is unified across personal + automation work.

---

## 2. Decisions locked (2026-05-24)

| Decision | Locked value | Rationale |
|---|---|---|
| Marvin Mission Control fate | **Decommission — migrate everything** | Single source of truth long-term beats sync complexity. Migration is a ½-day script + Larry's validation. We commit to the new system once Larry has used it 1 week without going back. |
| Marvin (Openclaw agent) integration | **No integration yet — design for it later** | Build the dashboard standalone. Marvin stays on his current JSON files until we know what he actually needs to read/write. Saves scope. |
| Sage patterns adopted | **All four bundles**: Plan-First, manifest+polling formalization, adversarial fix auditor, Promise-Before-Work + Unified Verifier + Sweep ledger | Full discipline package. Each is small individually; together they meaningfully upgrade the chain's reliability + Larry's signal-to-noise. |
| Supabase project ownership | **Larry-Yatch GitHub** (overrode 2026-05-24 during setup walk) | Originally locked as `agent.beacon.ourliberty@gmail.com` for tenant separation. Overridden during the activation walk: Supabase doesn't offer Google SSO (only GitHub/SSO/email), and Vercel is already under Larry-Yatch GitHub so this matches that posture. Tenant-separation argument was weak for Supabase on free tier (no billing visible yet). When per-product Supabases land in Phase F (TruPath, AI Co), each gets its own identity tied to that product's billing structure regardless. |
| Backend architecture | **Hybrid: droplet FastAPI for runtime, Next.js+supabase-js for PM** | Each backend does what it's best at. Dashboard works even if droplet is down (PM stays up; only 'live' runtime cards go stale). Mirrors Joe's split. Confirmed 2026-05-24 round 1. |
| State backend timing | **Supabase day-1** (no JSON-first interim) | Schema is well-understood from weeks of operating the agent OS. JSON-first would just be extra migration work. Supabase migrations cheap. |
| Project entity model | **Defined, not inferred** | 30-sec friction at project start is rounding error vs. spec-writing time; benefit is a real data model. Confirmed 2026-05-24 round 1. |
| Default Program for agent-OS work | **New Program: "Agent OS Development"** | Clean separation. Marvin System stays specifically about Marvin-the-agent. Future products built ON the agent OS get their own Programs (TruPath, AI Co, etc.). Locked 2026-05-24 round 1. |
| Project type discriminator | **Single enum: `personal \| agent_os_build \| client \| research`** | Simple, every project picks one type. Easy filters in UI. Enum can be extended via migration if needed. Locked 2026-05-24 round 1. |
| Plan-First sections | **7 sections**: Goal+scope · Files in scope · Acceptance criteria · Schema/state impact · Prior-PR archaeology · Risks+rollback · Approval gate | Adapted from Joe's 12. Dropped: live-customer-impact (no customers yet), detailed test plan (redundant with acceptance criteria), Stitch wireframes (no such tool), plan-gist URL (we use specs/), council review notes (no 7-agent council). Locked 2026-05-24 round 2. |
| Plan-First skip rule | **Skip for trivial Claude-as-Forge edits only** | Same boundary as existing Claude-as-Forge pattern: 1-line config/docs edits, line-level fixes, typos. Anything bigger gets a plan. ~80/20 split estimated. Locked 2026-05-24 round 2. |
| Deploy preview URL delivery | **Telegram DM (primary) + dashboard comms-inbox (record)** | Phone-friendly click-through preserved. Locked 2026-05-24 round 2. |
| Active-project pinning | **Sticky per-chat-id** (no time-decay; explicit `/switch-project` to change) | State file: `~/agents/state/active_projects.json`. Lowest friction for sustained work in one project. Locked 2026-05-24 round 2. |
| Dashboard landing | **Programs grid + horizontal tabs per Program** | Cross-Program awareness up front; tabs let you swap context without grid bounce. Locked 2026-05-24 round 3. |
| Sub-phase ordering | **E4.0 → E4.1 → E4.1b (0003) → E4.2 → E4.4 → E4.3 → E4.5** | E4.4 reordered before E4.3 because round-4's UI CRUD lock removed E4.4's dependency on `pm_writer`. UI can stand alone via Next.js Route Handlers + supabase-js. Visual feedback earliest. Locked 2026-05-24 round 5. |
| E4.4 dispatch shape | **3 sub-sub-phases (E4.4a / E4.4b / E4.4c)** | Original ~2500 LOC monolithic estimate exceeds Forge dispatch reliability zone (~1000 LOC). Splitting also gets visual feedback after E4.4a (~½ day) instead of after the whole rebuild ships. Locked 2026-05-24 round 5. |
| Project detail default view | **Kanban by task status** (list view available as alternate) | Matches Mission Control muscle memory. List available but not the default. Locked 2026-05-24 round 3. |
| First-of-day digest format | **5-7 bullets ~7am MDT**: yesterday's merges, today's in-flight tasks, blockers needing decision, costs vs. budget | Default; configurable later. Locked 2026-05-24 round 3. |
| Mission Control parallel-run length | **≥1 week; Larry calls cutover** | Decommission once Larry confirms he hasn't gone back to MC. No fixed deadline. Locked 2026-05-24 round 3. |
| Direct UI CRUD in dashboard | **REQUIRED in E4.4 scope**: + New Program/Project/Task buttons, inline-edit on all fields, delete-via-overflow with confirm. Also `+ New Decision` in Project sidebar. | Without UI CRUD, daily PM work would require opening Telegram for routine adds/edits — that's the comms-channel-overload pattern E4 was built to fix. Locked 2026-05-24 round 4 after gap surfaced during E4.2 dispatch planning. |

---

## 3. Success criteria

- `https://dashboard.ourliberty.dev` shows a unified Programs > Projects > Tasks view that includes both Larry's personal projects (migrated from Marvin Mission Control) AND every agent-OS build initiative as first-class entities.
- A new project can be created in <30 sec via the dashboard OR via a Beacon Telegram command (`/new-project <name>`).
- Every Forge dispatch automatically writes a Task row in Supabase, with timeline events (preflight / build / review / merge / deploy) populated as the chain progresses. Larry can drill into any task and see the full lifecycle without opening Telegram.
- Telegram traffic drops by ≥60% measured by DM count over a week of comparable activity. The chat surface is only used for actionable items (clarifications, approvals, escalations, watchdog alerts, EMERGENCY_HALT, cost-budget warnings, first-of-day digest).
- Plan-First Protocol is in effect: every non-trivial Forge dispatch gets a Beacon `/plan` step that produces a structured plan with the required sections. Plan-First is gated by task type (trivial Claude-as-Forge edits skip it; real builds always run it).
- Supabase has full E1.5 4-artifact credential discipline (service-role key + anon key in registry, runbook for both rotations, calendar event for annual scope audit, drift healer aware of both).
- Mission Control runs in parallel for ~1 week. Once Larry confirms he doesn't need to go back to it, the launchd plists are removed + the JSON files archived. Migration is complete.
- All 8 Sage patterns adopted are live and Larry has seen each one operate ≥3 times in real work (not just smoke tests).

---

## 4. Out of scope (explicit deferrals)

- **Per-project Supabase databases.** Each Larry product (TruPath, AI services co, client work) gets ITS OWN Supabase project in Phase F when the prototype actually needs persistent data. E4's Supabase is **only** for the PM dashboard's own data. Don't conflate.
- **Auth for multi-user dashboard access.** Dashboard auth = same `X-Dashboard-Token` model as E3.1 but extended for write paths. Real user management (multiple humans, role-based access) waits until a second human needs in.
- **Marvin / Openclaw integration.** Per locked decision above. Future phase.
- **Mobile native app.** Dashboard is responsive web; that's enough. Native app only if a year of usage shows web isn't cutting it.
- **Real-time SSE / WebSocket updates.** SWR polling at 30 s is enough. Real-time waits until a user complains.
- **AI-generated project briefs.** Beacon can draft a project's `reportingBrief` field if asked, but no auto-generation pipeline.
- **Time tracking / Pomodoro / focus mode.** Out of scope. The dashboard is for state, not for managing your attention.

---

## 5. Architecture

### 5.1 Data model — Programs > Projects > Tasks > Events

Three-tier hierarchy preserved from Marvin Mission Control, extended with agent-OS primitives. Each level:

**Program** — top-level grouping. Yours today: TruPath, "The Thing", Marvin System, AI Company, Personal. We'll likely add: Agent OS Development, Holding Business, Rocket Station.

```
Program
  id (uuid)
  name (text, unique)
  description (text)
  color (hex, for UI grouping)
  status (enum: active | archived)
  created_at, updated_at (timestamptz)
  position (int, for UI ordering)
```

**Project** — a coherent initiative under a Program. Today's E4 itself would be a Project under "Agent OS Development." TruPath's compliance review would be a Project under "TruPath."

```
Project
  id (uuid)
  program_id (uuid, FK → Program)
  name (text)
  description (text)
  reporting_brief (text)             -- one-liner for briefing others (high-value field from Mission Control)
  owner (text)                       -- person/people responsible (free-text, not user FK yet)
  status (enum: notstarted | inprogress | done | blocked | dropped)
  priority (enum: high | medium | low)
  blocker_type (text, nullable)      -- "waiting-on-larry", "waiting-on-vendor", etc.
  blocker_note (text, nullable)      -- prose context about the block
  next_action (text)                 -- prose description of next concrete step
  why_it_matters (text, nullable)    -- preserved from Mission Control but optional (field was usually empty)
  links (jsonb)                      -- array of {label, url} (typically Google Docs)
  project_type (enum: personal | agent_os_build | client | research)  -- discriminator
  started_at (timestamptz, nullable)
  last_updated (timestamptz)
  created_at (timestamptz)
  position (int, for UI ordering within program)
```

**Task** — a single unit of work. For personal projects, a human to-do. For agent-OS-build projects, ONE Forge dispatch (which has its own rich timeline of Events).

```
Task
  id (uuid)
  project_id (uuid, FK → Project)
  external_id (text, nullable)       -- e.g., "task-30-canonical-repo-paths-to-config" for agent-OS dispatches
  agent (text, nullable)             -- "forge" | "mirror" | "beacon" | "pulse" | null for human tasks
  name (text)                        -- short title
  description (text)                 -- longer; for agent tasks, the spec summary
  task_type (enum: human | agent_dispatch)
  status (enum: pending | in_progress | blocked | completed | cancelled)
  assignee (text, nullable)          -- free-text for human tasks ("Larry", "Marvin", "Robert"), agent name for agent tasks
  due_date (date, nullable)          -- human tasks only typically
  pr_url (text, nullable)            -- agent_dispatch only
  spec_path (text, nullable)         -- agents/beacon/specs/<spec>.md
  cost_usd (numeric, nullable)       -- total LLM spend on this task, agent_dispatch only
  duration_seconds (int, nullable)   -- wall-clock from start → completion
  started_at (timestamptz, nullable)
  completed_at (timestamptz, nullable)
  created_at (timestamptz)
  position (int)
```

**Event** — append-only log of state transitions on a Task. Agent_dispatch tasks get rich timelines automatically.

```
Event
  id (uuid)
  task_id (uuid, FK → Task)
  event_type (text)                  -- "preflight_proceed" | "build_started" | "pr_opened" | "review_pass" | "review_revision" | "auto_merged" | "deployed" | "marker_error" | "human_status_change" | ...
  payload (jsonb)                    -- event-specific data (pr_url, cost_delta, marker payload, etc.)
  emitted_at (timestamptz)
  emitted_by (text)                  -- "forge" | "mirror" | "beacon" | "outbox_notifier" | "larry" | "system"
```

**Decision** — append-only log of project-level decisions worth preserving (replaces the "decisions locked" markdown sections that scatter across spec docs today).

```
Decision
  id (uuid)
  project_id (uuid, FK → Project)
  title (text)
  context (text)                     -- what was being decided
  options_considered (jsonb)         -- array of {label, pros, cons}
  decision (text)                    -- the chosen option + 1-2 line rationale
  decided_at (timestamptz)
  decided_by (text)                  -- "larry" | "beacon" | "larry+claude"
  reversibility (enum: easy | medium | hard)
```

Notes on the model:

- **No `Cost` or `Build` separate table.** Cost is a field on Task (aggregated from Events on read if needed); Build is the agent_dispatch Task itself. The Sage pattern of separate ship-tracking files becomes unnecessary when Task IS the build.
- **`position` columns everywhere.** Drag-drop reordering is a key Mission Control feature; positions need to be persisted to survive page reload.
- **Soft-delete via `status = archived` on Programs only.** Projects + Tasks can be deleted hard (with cascade) — they're scoped to projects, low risk. Programs are bigger commitments; archiving preserves history.
- **No project hierarchy.** Projects don't have sub-projects. Tasks are the only level under a Project. Matches Mission Control today; resist the urge to add deeper nesting.

### 5.2 Supabase schema v1

Above maps to roughly 5 tables + 2-3 join tables for tags/labels if we add them later. Migration file: `supabase/migrations/<timestamp>_initial_pm_schema.sql`.

RLS policies (Row-Level Security): all tables readable by anyone with valid Supabase JWT issued to the dashboard, mutable only with the service-role key (used by Beacon + outbox-notifier on the droplet, AND by the Next.js route handlers behind dashboard auth). This is the standard 2-key pattern:

- **Anon key**: read-only, exposed to browser. Wrapped behind the dashboard-token check at the Next.js route layer so we don't even let unauthenticated browsers see the schema.
- **Service-role key**: read+write, lives on droplet in `.env.larry` + on Vercel as project env var. Never exposed to browser.

The dashboard's Next.js route handlers use the service-role key (server-side) to do mutations. The droplet's Beacon + outbox-notifier use it directly via Python supabase-py.

Migrations approach (copy Joe's pattern): `supabase/` dir at repo root of `ourliberty-dashboard` repo. `supabase db push` from local dev pushes migrations to the production Supabase project. A GitHub workflow (`supabase-migration-check.yml`) validates SQL syntax on PR, but human approval (Larry) is required to actually apply to prod.

### 5.3 Backend split — droplet FastAPI stays, PM goes to Next.js

This is the architectural call that needs your gut check:

**Droplet FastAPI (E3.1, `scripts/dashboard_api.py`) keeps serving live agent runtime state:**
- `/agents/status`, `/healers/status`, `/cycle-journal/recent` — these read filesystem state that ONLY the droplet has access to.
- `/health` — droplet liveness.
- These don't migrate to Supabase because they're queries against the running process state, not persisted PM data.

**New: Next.js Route Handlers in `ourliberty-dashboard` repo handle all PM:**
- `/api/programs`, `/api/programs/:id` (GET/POST/PATCH/DELETE)
- `/api/projects`, `/api/projects/:id` (GET/POST/PATCH/DELETE)
- `/api/tasks`, `/api/tasks/:id` (GET/POST/PATCH/DELETE)
- `/api/events` (GET only — append happens server-side from droplet, not from UI)
- `/api/decisions` (GET/POST/PATCH/DELETE)
- All use `@supabase/supabase-js` server-side with the service-role key.
- Same auth pattern as today's proxy routes (the `X-Dashboard-Token` header from the browser → server-side checks → server-side calls Supabase).

**Droplet → Supabase write path (NEW):**
- Beacon's CLAUDE.md teaches her to call a tiny Python helper `scripts/pm_writer.py` whenever she dispatches a task, marks a project status change, or logs a decision. The helper uses `supabase-py` against the same database.
- Outbox-notifier's existing marker handlers ALSO call `pm_writer` to append Events to Tasks as they fire (preflight_proceed, build_started, pr_opened, review_pass, auto_merged).
- This is the "polling vs. webhook" decision: we PUSH from droplet → Supabase on every state change, rather than poll. Push is fine because droplet → Supabase is a single direction with no race; the marker handlers already do single-writer state changes.

**Why this split:**
- Droplet uniquely owns runtime state; Supabase uniquely owns persistent state. Each backend does what it's best at.
- Avoids running supabase-py inside the FastAPI app (we'd need to manage another credential there).
- Lets the dashboard scale independent of the droplet (if droplet goes down, PM still works; only the "live runtime" cards go stale).
- Matches Joe's pattern (his Sage uses Python supabase clients on the agent side; his customer dashboard uses supabase-js on the Next.js side).

### 5.4 UI surface — what the dashboard becomes

The current E3 dashboard (`ourliberty-dashboard` repo) is 4 read-only pages (Overview, Tasks, Costs, Healers). E4 rewrites it as a PM-first surface:

**Primary nav: Programs grid landing → horizontal tabs per Program → kanban Projects (locked 2026-05-24 round 3)**
- **Landing page**: grid of all Programs as cards. Each card shows program name, project count, recent-activity indicator, color accent. Click a card → enter that Program.
- **Inside a Program**: horizontal tabs across the top let you switch between Programs without going back to the grid. Active Program highlighted. Stays on the Projects view as you switch.
- **Projects view (default kanban)**: columns = status (notstarted | inprogress | blocked | done). Cards = Projects with reporting_brief preview, blocker badge, priority indicator, owner. Drag-drop between columns to change status. Drag-drop between Programs (via the top tabs as drop targets) preserved from Mission Control.

**Direct UI CRUD — locked 2026-05-24 round 4 (added after gap surfaced)**

The PM dashboard MUST support direct UI creation + edit + delete of projects and tasks, matching Mission Control's daily-use ergonomics. Without this, you'd be forced to open Telegram for routine "add a task" work — the exact comms-channel-overload pattern E4 was supposed to fix.

- **`+ New Program`** action in the Programs grid landing — modal form (name, description, color picker, project_type? — probably no — and Program-level status). Auto-positions at end.
- **`+ New Project`** action in each Program's kanban view (top-right of column header OR floating button) → modal or right-side drawer with fields: name, description, reporting_brief, owner, priority, project_type (`personal | agent_os_build | client | research`), links. Status defaults to `notstarted`.
- **`+ New Task`** action inside Project detail view → inline row at bottom of kanban column OR modal. Fields: name, description, assignee, due_date. Defaults: `task_type='human'`, `agent=null`, `status='pending'`.
- **Inline edit on Project cards** (click any visible field → editable input → save on blur/Enter; escape cancels). Covers: name, reporting_brief, owner, priority, blocker_type, blocker_note, next_action.
- **Click a Project card → opens detail drawer** with full field set editable (Mission Control's side-panel pattern). All fields editable inline including `links` (add/remove pairs).
- **Inline edit on Task rows** similar pattern. Covers: name, description, assignee, due_date.
- **Delete via overflow menu** (`⋯` icon on each Project/Task card) → confirm modal. Hard delete (CASCADE on FK handles cleanup).
- **`+ New Decision`** action in Project detail sidebar → inline form (title, context, options_considered as JSON-shaped repeater, decision, reversibility, decided_by defaults to "larry").

**Two creation paths converge on Supabase:**

- **UI path (this spec):** browser → Next.js Route Handler → `getSupabaseServer().table(...)` → insert/update.
- **Telegram path (`/new-project`, `/new-task`):** Telegram → Beacon → `pm_writer.create_project(...)` → insert.

Both write to the same Supabase, both use the service-role key server-side. The UI path is the everyday surface; the Telegram path is for hands-busy moments (driving, on phone, mid-meeting) and for agent OS dispatches that auto-create Task rows.

**Project detail view (default = kanban by task status, locked 2026-05-24 round 3)**
- Header: name, status, priority, owner, dates, links to Google Docs.
- Body: tasks kanban — columns by task status (pending | in_progress | blocked | completed | cancelled). Cards show task name, agent (if any), assignee, due_date, cost. Both human and agent_dispatch tasks shown together, distinguished by an icon.
- Sidebar: blockers, decisions log (read-only here, edit elsewhere), recent events across all tasks.
- For agent_os_build projects: a "build timeline" view that shows the chain phases across all tasks in the project (great for visualizing a multi-task initiative like E3 was).
- Note: list view available as an alternate (no per-Project preference memory in v1; can add later if you actively use both).

**Task detail view (the multi-task triage answer)**
- Header: name, status, agent, assignee, dates, cost, PR link.
- Body: timeline of Events (preflight → build → review → merge → deploy) with timestamps, costs at each phase, marker payloads, PR diff link, logs link.
- For agent_dispatch tasks: a "what's it doing right now" card that pulls live data from the droplet FastAPI (the only place runtime state lives).

**Agent runtime cards (preserved from E3)**
- Still shown on Overview as a "live system" section.
- Now sourced from droplet FastAPI as before; not from Supabase (runtime state, not persisted).

**Costs view**
- Same shape as today (today's spend by agent, 7-day breakdown).
- NEW: per-project cost rollup. "TruPath has cost $X this month." "Agent OS Development phase has cost $Y total."

**Healers view**
- Same as today (no change needed for E4).

**New: Comms inbox (collapsed sidebar by default)**
- Shows demoted-from-Telegram informational pings: build_started, pr_opened, review_pass, auto_merged, deployed, cycle_journal_updates.
- Mark-as-read pattern. Lightweight.
- Distinct from the Tasks/Events view (which is structured); this is the chronological log.

### 5.5 Agent integration model

Programs/Projects/Tasks are first-class entities the agent OS reads + writes:

- **Project creation from Telegram** — `/new-project <name> [program]` Beacon command. Creates a Project row in Supabase. Returns the project URL.
- **Default-active-project pinning (locked 2026-05-24 round 2):** Sticky per-chat. Beacon remembers Larry's last-active project (stored in `~/agents/state/active_projects.json` keyed by `chat_id`); every new task auto-tags `project_id` to that. `/switch-project <name>` to change context; `/no-project` to clear. No time-decay reset; sticky until explicit switch.
- **Forge dispatch envelope adds `project_id` field** — required for agent_os_build tasks. Beacon populates it automatically based on the active project at dispatch time.
- **Outbox-notifier writes Events** — every marker handler appends an Event row to the Task's timeline. Adds ~50 ms per dispatch for the supabase-py call. Negligible.
- **Status transitions** — Task status flips happen at well-defined points: `pending → in_progress` when Forge worktree is created; `in_progress → completed` on auto_merge; `in_progress → blocked` on REVIEW_ESCALATE or marker-error retry budget exhausted; `blocked → in_progress` on revision dispatch.
- **Project-level status rollup** — denormalized field on Project, recomputed when any Task changes. Cheap.

---

## 6. Phasing — six sub-phases of E4 + four parallel small dispatches

Sub-phases ship in order (E4.0 → E4.5). Parallel dispatches can ship anytime they're convenient — they're independent of E4 dashboard work.

### E4.0 — Supabase activation (Larry-actions: ~1h; Claude-actions: ~½ day)

- Larry creates Supabase project under `agent.beacon.ourliberty@gmail.com` (Chrome MCP-assisted, region us-east-1 or wherever lowest-latency to droplet).
- Service-role + anon keys captured into `.env.larry` on droplet, Vercel env vars on dashboard, and full E1.5 4-artifact credential discipline (registry entry, two runbooks for two key rotations, annual scope-audit calendar event).
- `pip install supabase` on droplet (first Python supabase-py install).
- `@supabase/supabase-js` added to dashboard repo.
- Smoke test: `psql` from droplet against Supabase reaches it; trivial `select 1` works.
- Drift healer (`scripts/healers/heal_credential_drift.py`) updated to know about the two new keys.

### E4.1 — Schema v1 + migrations harness (Forge dispatch, ~½ day)

- New `supabase/` dir at root of `ourliberty-dashboard` repo with `migrations/0001_initial_pm_schema.sql`.
- Tables per § 5.1 / 5.2.
- RLS policies enabled.
- `supabase-migration-check.yml` GitHub Action that runs `supabase db lint` on PR and posts results as a comment.
- Manual approval required for `supabase db push` to prod (Larry runs it after merge).
- Schema doc at `ourliberty-dashboard/supabase/SCHEMA.md` (auto-generated from migrations, but with hand-written rationale block at top).

### E4.2 — Migration script (Marvin Mission Control JSON → Supabase, Forge dispatch ~½ day)

- New `scripts/migrate_mission_control.py` in `ourliberty-agent-core` repo.
- Reads from Marvin Mission Control's JSON files (`/Users/marvinrogers/.openclaw/workspace/{programs,projects,assignees}.json` — accessed via Tailscale or one-off copy by Larry).
- Maps 5 programs → Programs rows, 28 projects → Projects rows, all nested tasks → Tasks rows.
- Field mapping doc inside the script's docstring.
- Idempotent (running twice is safe; uses `external_id` for dedup).
- Dry-run mode (default) prints what would be inserted; `--apply` actually writes.
- Larry runs it once with `--dry-run`, reviews output, then `--apply`.

### E4.4 — Dashboard UI rebuild (REORDERED before E4.3, locked 2026-05-24 round 5; SPLIT into 3 sub-sub-phases)

**Why reordered before E4.3:** Round-4 lock put UI CRUD in E4.4's scope (UI writes directly to Supabase via Next.js Route Handlers). That removed the dependency on `pm_writer` being wired first — dashboard can stand alone. Larry's stated need (visual feedback before further design choices) is satisfied earliest by shipping E4.4 first.

**Why split into 3 sub-sub-phases:** Original monolithic estimate ~2-3 days, 1500-2500 LOC. That's at the edge of Forge dispatch reliability (Joe's Sage caps ~1000 LOC). Splitting also gets visual feedback faster.

Full sub-spec: [agents/beacon/specs/e4-4-dashboard-ui-rebuild.md](specs/e4-4-dashboard-ui-rebuild.md).

- **E4.4a — MVP read-only** (~½ day, ~500 LOC, ~$6 LLM): Programs grid → Projects list → Project detail → Tasks list → Task detail with Events timeline. All RENDER, no MUTATION. Larry sees his real Mission Control data in `dashboard.ourliberty.dev` for the first time.
- **E4.4b — Kanban + drag-drop** (~½ day, ~400 LOC, ~$5 LLM): Switch list views to kanban-by-status; @dnd-kit drag-drop. First MUTATION endpoint (PATCH only for status changes).
- **E4.4c — CRUD + forms** (~1 day, ~800 LOC, ~$10 LLM): + New buttons, inline edit, delete via overflow. Full CRUD per overview § 5.4 "Direct UI CRUD" subsection.

Each sub-sub-phase ships as an independent Forge dispatch with Larry review between. Total estimate: ~$25-35 LLM, ~2-2.5h wall clock, ~50-60 min Larry-time spread across the 3 dispatches.

### E4.3 — Backend extension: droplet `pm_writer.py` + Beacon CLAUDE.md updates (Forge dispatch ~1 day; FOLLOWS E4.4 per round 5)

- New `scripts/pm_writer.py` — small library: `pm_writer.create_project(...)`, `pm_writer.create_task(...)`, `pm_writer.append_event(...)`, `pm_writer.set_status(...)`, etc.
- Outbox-notifier marker handlers updated to call `pm_writer.append_event` at each handler.
- Beacon CLAUDE.md teaches her to call `pm_writer.create_project` on `/new-project` commands and to pin active project per-chat in a `~/agents/state/active_projects.json` file.
- Forge dispatch envelope validator adds optional `project_id` field; Beacon populates it on construction.
- Unit tests on the pm_writer library; integration tests behind a `PM_WRITER_TEST_SUPABASE_URL` env (skipped in normal CI).

### E4.5 — Mission Control decommission + Marvin cleanup (Larry-actions: ~½ hour, Claude assists)

- After Larry has used the new dashboard for ≥1 week without going back to Mission Control:
- Stop launchd services on Marvin's Mac (`launchctl unload` both plists).
- Archive the JSON files to a dated tarball in `Shared with Larry/` Google Drive.
- Delete the plists.
- Update Marvin's CLAUDE.md to point at the new dashboard URL when asked about projects.
- Mark Mission Control as decommissioned in `docs/operating-manual.md` Part II.

### Parallel small dispatches (independent of E4 phasing; ship when convenient)

These four were locked as part of E4's scope but can ship in any order, independent of dashboard build.

**P-1: Comms narrowing** (~1 day Forge dispatch)
- Envelope adds `notification_class` field: `actionable | informational`.
- Outbox-notifier respects the field — `informational` skips Telegram, only writes to Supabase Events.
- Beacon CLAUDE.md updated with the boundary list (stays in Telegram vs. demote).
- Default boundary per spec § 5.6 below; Larry can override individual notification types via a config block in `agent-models.json`.

**P-2: Plan-First Protocol** (~1-2 days, biggest of the four)
- New `/plan <task-summary>` Beacon command.
- Plan template at `agents/beacon/plan-template.md` (adapted from Joe's 12-section template, narrowed to 7 for our fleet — locked 2026-05-24 round 2).
- **Required plan sections (7):** Goal+scope · Files in scope · Acceptance criteria · Schema/state impact · Prior-PR archaeology · Risks+rollback · Approval gate. (Joe's excluded 5: live-customer-impact, detailed test plan, Stitch wireframes, plan-gist URL, council review notes — not applicable to our 4-agent fleet.)
- **Skip rule (locked 2026-05-24 round 2):** Plan-First applies to all dispatches EXCEPT trivial Claude-as-Forge edits (same boundary as today's pattern: 1-line config/docs edits, line fixes, typo corrections). Estimated ~80% of dispatches require plans; ~20% skip.
- Beacon's CLAUDE.md teaches her: every non-trivial dispatch goes through `/plan` first. Plan lives at `agents/beacon/plans/<task-id>.md` (committed to repo). Forge dispatches reference the plan via `plan_path` field on the envelope.
- Mirror's CLAUDE.md teaches her to check the plan exists + the diff matches the plan's acceptance criteria.

**P-3: Adversarial fix auditor** (~½ day Forge dispatch)
- New `scripts/adversarial_auditor.py` — spawns a fresh Claude Opus subprocess with NO prior context to re-audit a Mirror PASS.
- Triggered after Mirror PASS, before auto-merge. Max 3 rounds (escalate to Larry if rounds 1-3 all flag concerns).
- Adds ~$0.30 + ~2 min per PR. Kill-switch available via `OURLIBERTY_ADVERSARIAL_AUDITOR_ENABLED=false`.
- Pattern: read the PR diff, the spec/plan, the Mirror PASS rationale, and ask "would I have passed this?" Audit findings appended to PR as a comment; if findings rise to REVISION-level, block auto-merge.

**P-4: Promise-Before-Work + Unified Verifier + Sweep ledger** (3 small concerns bundled, ~1 day total)
- **Promise-Before-Work:** Beacon CLAUDE.md teaches her: when Larry asks for something, immediately log the promise (Supabase `Task` row with status=pending) and DM ack BEFORE starting work. Closes the gap where Larry asks for 3 things and doesn't see anything happen for 5 min.
- **Unified Verifier:** Mirror's review prompt updated to do ONE walk with TWO lenses (correctness + cleanliness). Today she's primarily a correctness checker; the cleanliness lens catches code smell + maintainability the way Joe's Sage does. Single REVIEW_PASS marker, two-lens narrative.
- **Sweep ledger:** New `runbooks/fix-ledger.json` — append-only log of every fix shipped, with the issue/symptom it addressed. Mirror checks against the ledger before flagging "this looks like a bug" to avoid re-filing already-fixed things.

### Comms boundary (§ 5.6, referenced by P-1)

**Stays in Telegram (actionable — needs your eyes):**
- Clarifications from Forge or Mirror
- APPROVAL_REQUEST (Beacon → Larry)
- REVIEW_ESCALATE, REVIEW_EMERGENCY_HALT
- Watchdog alerts (memory pressure, healer death, cgroup limits)
- Kill-switch trips
- Marker-error retries past budget (dead-letter)
- Cost-budget cap hit OR warning at 80%
- First-of-day digest (skim-actionable, sent 7am MDT)
- New project / task creations requested by Larry (Beacon ack)
- Mention of Larry's name in a dispatch (escalation surface)

**Demoted to dashboard only (informational):**
- Build started / running / progress
- PR opened
- Review passed (just-passed, no action needed)
- Auto-merged
- Deployed
- Cost-per-task increments (running tally)
- Healer "all clear" sweeps
- Cycle journal updates
- Forge preflight PROCEED (today this DMs Larry, will demote)

**Special case — deploy preview URLs (locked 2026-05-24 round 2):** keep in Telegram. You're often on phone away from a laptop; the URL click-through belongs where the phone is. Dashboard's comms-inbox shows them too as a record, but Telegram remains the primary delivery channel for previews. Revisit if usage shows otherwise.

---

## 7. Migration plan — Marvin Mission Control → dashboard.ourliberty.dev

Done as E4.2 (script) + E4.5 (decommission). Tactical sequence:

1. Larry copies the three JSON files (`programs.json`, `projects.json`, `assignees.json`) from Marvin's `/Users/marvinrogers/.openclaw/workspace/` to a tmp location on his laptop OR runs the migration script from Marvin's Mac directly (Tailscale-accessible).
2. Run `python3 scripts/migrate_mission_control.py --dry-run` — outputs JSON-shaped "I would insert these N programs, M projects, P tasks."
3. Larry reads the output, asks Beacon for any cleanup (e.g., "rename 'The Thing' to 'Next Holding Initiative'") — clarify-and-edit pass.
4. `python3 scripts/migrate_mission_control.py --apply` — actually writes to Supabase. Idempotent so safe to retry.
5. Larry opens `dashboard.ourliberty.dev`, navigates to Programs, verifies all 28 projects + 5 programs are there.
6. For ≥1 week, Larry uses the NEW dashboard but Mission Control stays running as fallback. Any project edits go to the new dashboard.
7. After 1 week without falling back: `launchctl unload` both plists, archive JSON to Google Drive, delete plists.

Risks:
- **Field mismatches.** Some Mission Control fields (e.g., `whyItMatters`) are usually empty but might have edge-case data. Migration script preserves all fields verbatim into Supabase columns even if our new model doesn't surface them. Optional columns get NULL.
- **Drift during the parallel-run week.** If Larry edits in BOTH systems, the new dashboard wins (it's the new source of truth). Migration script can be re-run with `--update-existing` to pull any late-stage edits from Mission Control, but really the convention is "edit only in the new system once you've validated migration."
- **Task IDs.** Mission Control tasks are nested inside project JSON; they get extracted into separate Task rows with their original IDs preserved as `external_id` (lets us match up later if needed).

---

## 8. Effort + cost estimate

| Sub-phase | LLM cost | Wall clock | Larry actions |
|---|---|---|---|
| E4.0 Supabase activation | ~$1 (Chrome MCP + small validators) | ½ day | ~1h (Supabase project create, key capture, Vercel env vars) |
| E4.1 Schema v1 | ~$5 (Forge dispatch) | ½ day | 15 min review + approve `supabase db push` |
| E4.2 Migration script | ~$5 (Forge dispatch) | ½ day | ~30 min copy files + run script + verify in UI |
| E4.3 pm_writer + Beacon updates | ~$8 (Forge dispatch, biggest of the API layer) | 1 day | ~15 min smoke test |
| E4.4 Dashboard UI rebuild (incl. CRUD per round 4) | ~$30-50 (multi-dispatch likely; biggest unknown) | 2-3 days | Spot-check Vercel previews + final approval |
| E4.5 Decommission | $0 | ½ hour | ~½ hour ops |
| P-1 Comms narrowing | ~$5 | 1 day | None |
| P-2 Plan-First Protocol | ~$10 | 1-2 days | Read first auto-generated plan + provide feedback |
| P-3 Adversarial auditor | ~$5 | ½ day | None |
| P-4 Promise + Verifier + Sweep | ~$8 | 1 day | None |
| **Total** | **~$72-100** | **~10 working days** spread over ~3 weeks (parallel small dispatches overlap with main phasing) | **~3-4 hours total** across all sub-phases |

This is a real budget. If anything stretches it'd be E4.4 (UI rebuild is iterative by nature). If anything compresses it'd be the parallel small dispatches (likely under-budget).

---

## 9. Risks + rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| Supabase project misconfigured (wrong region, wrong tier, exposed creds) | E4.0 validator checks region + RLS-on + service-role-only-server-side before declaring activation done. | Delete Supabase project; redo. Anon key rotation isolates exposure. |
| Schema v1 has design flaws | Expected — plan on 3-5 migrations in first month. Supabase migrations are non-disruptive. | `supabase db rollback` (Joe's setup includes a rollback runbook; we adopt). |
| Migration script corrupts Mission Control data | Mission Control files are NEVER written to by the script (read-only). Source of truth preserved. | Re-clone from Marvin's backup. |
| Dashboard rebuild breaks E3's read-only features Larry currently relies on | All E3 features (agent status, costs, healers) must work in v1 of the new UI; we don't replace until parity is proven. Vercel preview deploys mean Larry can compare side-by-side before promoting. | Vercel rollback to last E3 deployment is one click. |
| Telegram comms-narrowing demotes something Larry actually wanted | Boundary is configurable in `agent-models.json`; Larry can flip an individual notification type back to Telegram in ~30 sec. | Edit config, restart outbox-notifier. |
| Plan-First Protocol slows trivial dispatches | Trivial Claude-as-Forge edits explicitly skip it (existing pattern). | Disable via `OURLIBERTY_PLAN_FIRST_ENABLED=false`. |
| Adversarial auditor over-flags + blocks auto-merge | Kill-switch via env var; round budget of 3 means it can't infinite-loop. | Set `OURLIBERTY_ADVERSARIAL_AUDITOR_ENABLED=false`. |
| Drift healer not aware of new Supabase keys | E4.0 includes the healer update as a hard requirement. | Manual update + restart healer; <5 min. |

---

## 10. Open questions for Larry before sub-spec drafting

These are the things this overview deliberately punts on; would benefit from your read before sub-specs lock the answers:

1. ~~Default Program for agent-OS work~~ — **RESOLVED 2026-05-24 round 1**: new Program "Agent OS Development." Moved to § 2 decisions table.

2. **Multi-account future for Supabase.** Long-term, do you want one Supabase project per Larry-product (TruPath has its own, AI services co has its own, dashboard has its own), or shared infrastructure with logical separation? My read: per-product is cleaner long-term but per-product means per-product credential discipline. **Deferred until first product graduates from prototype.**

3. ~~Project type discriminator usefulness~~ — **RESOLVED 2026-05-24 round 1**: single enum `personal | agent_os_build | client | research`. Moved to § 2 decisions table.

4. ~~Drag-drop or list view default?~~ — **RESOLVED 2026-05-24 round 3**: kanban-by-task-status default within Projects; list available as alternate. Moved to § 2.

5. ~~First-of-day digest format.~~ — **RESOLVED 2026-05-24 round 3**: 5-7 bullets ~7am MDT covering yesterday's merges, today's in-flight, blockers needing decision, costs vs. budget. Configurable later. Moved to § 2.

6. ~~Marvin-on-Tailscale access during parallel-run week.~~ — **RESOLVED 2026-05-24 round 3**: fine to context-switch between the two URLs; no design work. Moved to § 2.

**All Round 1-3 decisions locked.** Overview spec is approved-pending-Larry-final-read. Next step: draft E4.0 sub-spec (Supabase activation), the gating dependency for all other sub-phases.

---

## 11. Trigger to start

This spec sits in draft state until Larry signs off. On approval:

1. Sub-specs for E4.0, E4.1, E4.2, E4.3, E4.4, E4.5 get drafted (Beacon, sequentially or in parallel as appropriate).
2. The 4 parallel small dispatches get their own short specs.
3. E4.0 dispatches first (it's a prerequisite for all sub-phases since Supabase needs to exist).
4. E4.1 + P-1 can ship in parallel (different surfaces).
5. E4.2 + E4.3 + P-2 in parallel (different surfaces).
6. E4.4 is the long pole; E4.5 + remaining parallel dispatches finish concurrently.

Expected total wall clock: ~3 weeks from approval to "Mission Control decommissioned, all four Sage patterns adopted."

---

## 12. Source notes (where this design came from)

- Marvin Mission Control structure: research pass on `Larry-Yatch/marvin-workspace` — Programs/Projects/Tasks 3-tier, 5 programs + 28 projects, kanban UI, Node.js+Express+vanilla-JS stack, JSON-files-on-disk persistence.
- Joe's Supabase pattern: research pass on `GrowthMastery-ai/gm-agent-core` (Sage agent dir) + `growth-mastery` (live customer dashboard). Sage uses Supabase ONLY for live customer data, not PM. PM in Joe's world is GitHub issues + gists + JSON files. We're greenfielding the PM schema; copying his integration mechanics (supabase-js/supabase-py clients, migration harness, env-var-based creds).
- Sage workflow patterns (Plan-First, manifest+polling, adversarial auditor, Promise-Before-Work, Unified Verifier, Sweep ledger): all from `agents/sage/` CLAUDE.md + workspace docs in `gm-agent-core`. Adapted from his 7-agent topology to our 4-agent (Beacon orchestrates the patterns Sage does; Forge is Luma's analog; Mirror absorbs the Unified Verifier role).
- Larry's 4 locked decisions (Mission Control decommission, no Marvin integration, all 4 Sage patterns, Supabase under agent.beacon.ourliberty@gmail.com): explicit in 2026-05-24 chat session.
- Telegram comms boundary: drafted from observed pain (~6h of multi-build E3 session) and the Plan-First / Unified Verifier discipline patterns Joe runs.
