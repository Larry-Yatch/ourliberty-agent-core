# Spec: E4.4f — Missions Tab v1 (Technical Coordination Surface)

**Status:** Draft (awaiting Larry approval)
**Author:** Claude-as-Forge (written 2026-05-28 morning, after Approvals tab + UX inspection session)
**Approver:** Larry (pending)
**Phase:** E4.4f of `docs/phase-e-plan.md` Phase E4
**Parent spec:** [agents/beacon/specs/e4-overview.md](e4-overview.md)
**Predecessors:** E4.4a (read-only MVP) · E4.4d (Operations / System view) · E4.4e (Approvals tab) — all shipped 2026-05-27
**Sibling:** E4.4b (Programs kanban with drag-drop) — specced 2026-05-24, deferred in favor of this spec per 2026-05-28 design pass

---

## 1. Problem statement

The dashboard now has four operational surfaces (Programs, Approvals, Live System, Operations) — but **none** of them answer the question *"What are we building right now?"* at the initiative level.

Concrete example from 2026-05-27: across one day Larry had 5+ active technical missions in various states — Approvals tab arc, Live + Ops polish, healer reconciliation, sync.service fix, Pulse Check III threshold tuning, PR-S4 rectification, the bootstrap-002 verification. Their states lived in:

- **Chat history** (drafts of dispatch text, half-formed concepts)
- **GitHub** (only open PRs visible; merged + planned invisible)
- **Spec docs in `agents/beacon/specs/`** (only after a spec exists)
- **Larry's memory** (the rest)

No single "5 missions, here's their state" surface existed. Larry's words from the inspection: *"It would be useful on the operations system page to have a list of 'projects' they're working on, what state that project is in, who's working on it, what's next, etc., so that there's one place to look at and see all the 'missions' we got them on."*

E4.4f introduces a **coordination surface** — kanban-shaped, sibling to the planned (deferred) E4.4b Programs kanban — but operating at the technical-implementation layer rather than the business-portfolio layer.

---

## 2. Layer separation — Programs vs Missions

The two kanbans look identical visually but serve different audience modes:

| | Programs kanban (E4.4b, deferred) | **Missions kanban (this spec)** |
|---|---|---|
| Audience mode | Larry-as-CEO | Larry-as-engineer |
| Cards represent | Business projects (from Mission Control) | Technical multi-PR initiatives |
| Who moves cards | **Larry (interactive drag-drop)** | **The chain (auto-derived from chain_events)** |
| Data source | `projects` / `tasks` Supabase tables | `missions.json` registry + `chain_events` + open PRs |
| Mutations | PATCH on status change | None — read-only v1 |
| Drag-drop library | needed | not needed |
| Implementation cost | $5 / ½ day (per E4.4b spec) | $8-12 / this spec |

Missions ships first per the 2026-05-28 design call (real Larry pain; E4.4b is nice-to-have, Mission Control on Marvin still works).

---

## 3. Five locked decisions

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | Tab placement | **New top-level tab "Missions" in slot 2 (between Programs and Approvals); Approvals shifts to slot 3** | Larry-flow: Programs (CEO) → Missions (engineering coordination) → Approvals (action) → Live/Ops (status/diag). Adjacent to Programs as Larry requested ("next to Programs"). |
| B | Mission registry | **JSON file `agents/beacon/missions.json` in agent-core repo, version-controlled, manually curated** | Simpler than Supabase table for v1. Every mission edit is a git commit (audit trail free). Future migration to Supabase trivial if missions get dynamic fields (assignees, deadlines, etc.). |
| C | Phase columns (kanban x-axis) | **5 columns + 1 collapsed special: Drafting / Ready to fire / In flight / Awaiting merge / Shipped + (Deferred-Paused collapsed)** | Each phase is derivable from data — no manual maintenance per card. 5 + 1 fits comfortably; 7+ would crowd. |
| D | Layout (kanban y-axis) | **Mission-as-a-row; cards (PRs / tasks) flow horizontally through phases. Orphans lane at bottom for ungrouped PRs.** | Larry can scan one row to see one mission's progress; orphans lane keeps hotfixes from disappearing. |
| E | Interaction depth (v1) | **Read-only — card click expands side panel with detail; NO drag-drop, NO inline editing.** | Cards move themselves based on chain state. Drag-drop adds nothing useful (you'd be lying about state). Inline editing of mission name/brief deferred to v2. |

---

## 4. Design intent — Larry's workflows

| State | Question | Time budget | Polish needs |
|---|---|---|---|
| **Triage** | "Which missions are blocked vs flowing?" | <10 sec | Phase-column distribution per mission row scannable at a glance |
| **Plan** | "What's queued to fire next?" | 30 sec | Drafting + Ready-to-fire columns surface the backlog Larry hasn't dispatched yet |
| **Inspect** | "Why is mission X stuck in Awaiting merge?" | 30 sec | Card click → side panel shows chain timeline + AUTO_MERGE state + blocker if any |
| **Catch up** | "What shipped while I was away?" | 30 sec | Shipped lane (collapsed) shows recent completions with deploy confirmation |
| **Audit** | "Did mission Y ever land?" | 1 min | Search across all missions + Shipped/Deferred history |

Success metric: Larry opens Missions, in <60 seconds can answer "what needs my attention, what's flowing, what's done, what's deferred" — without consulting chat history or GitHub.

---

## 5. Detailed requirements

### 5.1 missions.json schema

Path: `agents/beacon/missions.json` (in `ourliberty-agent-core` repo).

```jsonc
{
  "schema_version": 1,
  "missions": [
    {
      "id": "missions-tab-v1",
      "name": "Missions Tab v1",
      "phase": "drafting",          // hint only; live phase derived from data
      "brief": "Kanban surface for technical multi-PR initiatives.",
      "spec_docs": [
        "agents/beacon/specs/e4-4f-missions-tab-v1.md"
      ],
      "task_ids": [
        "e4-4f-missions-tab-v1"      // populated when PRs dispatch
      ],
      "repo": "ourliberty-dashboard", // primary repo (UI work)
      "created": "2026-05-28",
      "deferred_reason": null        // null unless phase=deferred
    },
    {
      "id": "e4-4b-projects-kanban",
      "name": "Programs Kanban (drag-drop projects)",
      "phase": "deferred",
      "brief": "Switch Programs view to kanban + drag-drop per E4.4b spec.",
      "spec_docs": ["agents/beacon/specs/e4-4-dashboard-ui-rebuild.md#e44b"],
      "task_ids": [],
      "repo": "ourliberty-dashboard",
      "created": "2026-05-24",
      "deferred_reason": "Prioritized Missions tab first per 2026-05-28 design pass"
    }
  ]
}
```

`phase` is a *hint* for cards that aren't yet derivable from data (e.g., a pure-draft mission with no task_ids and no spec_docs yet). For missions with task_ids, the dashboard ignores this field and computes phase from chain_events. For deferred missions (`phase: "deferred"`), the dashboard respects the hint (no chain_events check) and renders in the Deferred lane.

### 5.2 Phase derivation rules (dashboard-side)

For each task_id in a mission's `task_ids[]`, determine its phase by checking the most-recent chain_events for that task_id + GitHub PR state:

| Phase | Derivation rule |
|---|---|
| **Drafting** | Mission has no `spec_docs` AND no `task_ids` (concept only) |
| **Ready to fire** | Mission has `spec_docs` but `task_ids[]` is empty OR none have a `session_start` event yet |
| **In flight** | Task has `session_start` event (Forge or Mirror) AND no `auto_merge` event |
| **Awaiting merge** | Task has `mirror_review_pass` marker OR `escalation`-type chain_event AND no `auto_merge` event yet AND PR.state is OPEN |
| **Shipped** | Task has `auto_merge` event OR PR.state is MERGED |

Mission-aggregate state (badge on the row): derived from the distribution of its tasks across phases.

### 5.3 Orphans lane

Bottom row of the kanban (always rendered, visually demoted with subtle styling):

- Cards = task_ids that appear in `chain_events` but DON'T appear in any registered mission's `task_ids[]`
- One-off hotfixes (today's PR #142, #143, #144, etc. would all be here)
- Sort: most-recent first
- Limit: last 30 days (older orphans hidden — can be searched)

### 5.4 Card content

**Compact card (in kanban cell — collapsed by default, target ≤72px):**

```
[mission accent stripe] [task title (truncated)]    [age]   [▸]
                        [agent · phase-substate]            [actions]
```

- `mission accent stripe`: 4px color stripe on left edge, color hash from mission id
- `task title`: extracted via the same `extractHeadline()` helper used in Approvals polish v1 (first H1, then first sentence, then humanized task_id)
- `agent · phase-substate`: "forge · building" or "mirror · reviewing" or "auto_merge · queued" etc.
- `age`: relative time since last event for this task

**Expanded card (side panel, slides in from right when card clicked):**

- Mission name + brief (top context)
- Task title + task_id
- Chain timeline (vertical list of chain_events for this task_id, newest top)
- PR link (if any) + chain-state badge
- Spec doc link (if any) + line reference
- Worktree path + session JSONL path (if running)
- Action affordances:
  - For Drafting cards: "Edit brief" (v2) / "Promote to spec'd"
  - For Ready-to-fire cards: "View dispatch text" (if drafted in a chat) — placeholder; v2 wires up
  - For In-flight cards: "View in Operations / System" (deep-link to active-sessions card filtered)
  - For Awaiting merge cards: "View PR on GitHub"
  - For Shipped cards: "View PR" + "View deploy" (Vercel for dashboard, daemon-restart info for droplet)

### 5.5 Top strip

Above the kanban, sticky:

```
┌────────────────────────────────────────────────────────────────────────┐
│ Missions                                       [Search] [+ New mission] │
│ X active · Y in flight · Z awaiting · W shipped today · V deferred     │
└────────────────────────────────────────────────────────────────────────┘
```

- **Search** — client-side filter on mission name + brief + task_ids. Debounce 200ms.
- **+ New mission** — opens a small modal that POSTs to a new Route Handler `app/api/missions/new/route.ts` which writes a new mission entry to `missions.json` and opens a PR on `ourliberty-agent-core` with the addition (via the droplet's existing GitHub-write surface). v1 simplification: modal just generates the JSON + opens a PR for Larry to review/merge. Auto-merge deferred to v2.

### 5.6 Bucket UI (vertical sections)

1. **Active missions kanban** (visible at all times) — all missions with `phase != "deferred"` and at least one task in a non-shipped phase
2. **Shipped today** (collapsed, expandable, header shows count) — missions where all tasks shipped within last 24h browser-local
3. **Deferred** (collapsed, expandable, header shows count) — missions with `phase: "deferred"`

### 5.7 Real-time + polling

- SWR poll cadence: 10 sec for active kanban, 30 sec for Shipped today, 60 sec for Deferred (matches Approvals tab cadence pattern from polish-v1 spec).
- Optimistic UI on "+ New mission" submit: modal closes immediately; mission appears in Drafting column with subtle "submitting…" indicator; on PR creation success, indicator clears.

### 5.8 Droplet endpoint

NEW: `GET /api/system/missions` on the existing `ourliberty-dashboard-api.service` (FastAPI app at `scripts/dashboard_api.py`):

- Reads `agents/beacon/missions.json` from the droplet's `~/agent-core/` checkout
- Returns the raw missions list + a `last_synced_at` timestamp
- Token-gated via `_require_token` (existing pattern)

Dashboard's Route Handler `app/api/missions/list/route.ts` calls this droplet endpoint, joins with chain_events query for each task_id, returns the unified mission+phase model to the client.

---

## 6. Success criteria

- Visit `dashboard.ourliberty.dev/missions` and within 10 seconds answer: "how many missions are in flight right now?" without looking at chat or GitHub.
- For any mission in flight, click any card → side panel shows full chain timeline + PR link + spec link within 1 second.
- Orphans lane surfaces all one-off PRs from the last 30 days that don't belong to a registered mission.
- "+ New mission" modal works: enter name + brief, click Create → PR opens on agent-core with the addition to `missions.json` → after Larry merges, the new mission appears on next poll.
- Adding a task_id to a mission's `task_ids[]` (via PR edit) → card appears in correct phase on next poll based on its chain_events.
- Deferred lane lists all explicitly-paused missions with their `deferred_reason` visible on card hover.

---

## 7. Out of scope (deferred to v2+)

- Drag-drop between phase columns (cards are auto-derived; Larry shouldn't move them manually)
- Inline mission editing (name, brief, deferred_reason changes — v1 requires a PR; v2 could add direct UI editing → PR auto-creation)
- Mission dependency tracking ("Mission B depends on Mission A shipping")
- Mission-level cost rollup (sum of chain spend across all tasks in a mission)
- Mission-level time estimates / actuals (PR estimates vs actual chain spend)
- Mobile responsive layout
- Cross-mission filter chips (e.g., "show only dashboard-repo missions")
- Auto-archival of long-shipped missions (v2 could archive missions after 90 days)
- Mission templates (e.g., "new feature mission template" with pre-filled PR-A/B/C/D structure)

---

## 8. Files in scope

**ourliberty-agent-core:**
- `agents/beacon/missions.json` (NEW) — seed registry with 1-2 entries (this mission itself + e4-4b-deferred entry)
- `scripts/dashboard_api.py` — add `GET /api/system/missions` endpoint
- `agents/beacon/CLAUDE.md` — add a "registering a new mission" subsection (instruct Beacon to update missions.json when a mission concept is formalized)
- `scripts/tests/test_dashboard_api_missions.py` (NEW) — endpoint tests

**ourliberty-dashboard:**
- `app/missions/page.tsx` (NEW) — kanban layout
- `app/missions/components/PhaseColumn.tsx` (NEW)
- `app/missions/components/MissionRow.tsx` (NEW)
- `app/missions/components/MissionCard.tsx` (NEW) — compact + expanded
- `app/missions/components/MissionDetailPanel.tsx` (NEW) — side panel
- `app/missions/components/OrphansLane.tsx` (NEW)
- `app/missions/components/NewMissionModal.tsx` (NEW)
- `app/api/missions/list/route.ts` (NEW) — GET; joins missions + chain_events
- `app/api/missions/new/route.ts` (NEW) — POST; opens GitHub PR via droplet endpoint
- `lib/mission-queries.ts` (NEW) — phase derivation, orphan detection, mission state helpers
- `lib/types.ts` — extend with Mission, MissionPhase, etc.
- `app/layout.tsx` — add Missions tab to nav (slot 2; Approvals shifts to slot 3)

**Supabase:** no migrations.
**Auth:** read endpoints stay anon-friendly (no PII); `+ New mission` write path uses the existing Supabase Auth + Google login from PR-C (Approvals).

---

## 9. Test plan

Vitest:

- `extractHeadline` reused from Approvals polish-v1
- `derivePhaseForTask(taskId, chainEvents, prState)` — fixture each phase derivation case; assert correct phase returned
- `detectOrphans(chainEvents, missions)` — fixture mixed task_ids; assert only unregistered ones land in orphans
- `MissionRow` component renders correct phase distribution from fixture data
- `NewMissionModal` form validation + POST handler
- `list/route.ts` joins missions.json + chain_events correctly; handles missing fields gracefully
- Existing list/action route tests for other tabs must keep passing

Python (agent-core):
- `test_dashboard_api_missions.py` — endpoint returns 200 with token, 401 without; payload shape matches contract; missing missions.json returns empty list

Manual acceptance per § 6.

---

## 10. Cost estimate

Best-guess Forge + Mirror chain spend: **$10-14**. Larger than today's polish PRs because:
- 7 new components + 2 new API routes + 1 new droplet endpoint + 1 registry JSON
- Phase derivation logic has real test surface
- Mission detail panel non-trivial

Mirror revision rounds expected: **0-1**.

---

## 11. PR breakdown (if Forge wants to split)

Forge can optionally split into 2 PRs for review-able size:

- **PR-A** (agent-core, ~$3-5): `missions.json` seed + droplet `GET /api/system/missions` endpoint + CLAUDE.md update + Python tests. Enables the dashboard side without requiring it.
- **PR-B** (dashboard, ~$7-9): the full UI per § 8 — components, routes, helpers, tests, nav update.

Single PR also fine if Forge prefers; the split is just for review ergonomics.

---

## End of spec
