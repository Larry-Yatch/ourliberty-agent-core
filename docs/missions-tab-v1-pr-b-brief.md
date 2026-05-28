# Brief: Missions Tab v1 — PR-B (Dashboard UI)

**Status:** Ready for Forge preflight
**Author:** Claude (drafted with Larry 2026-05-28)
**Target repo:** `ourliberty-dashboard`
**Parent spec:** `agents/beacon/specs/e4-4f-missions-tab-v1.md` (in `ourliberty-agent-core`, merged 2026-05-28 as PR #156)
**Predecessor:** PR-A `e4-4f-missions-tab-v1-pr-a` — agent-core PR #159, **MERGED 2026-05-28**. Provides `agents/beacon/missions.json` + `GET/POST /api/system/missions` on `dashboard-api.service`.

This brief is the canonical reference for PR-B. Forge: read in full during preflight. Spec § 5/8/9 stay authoritative for design intent; this brief carries verbatim implementation artifacts (function signatures, JSON shapes, code snippets, file inserts) so no synthesis from spec is required at build time.

---

## Scope summary

PR-B ships the full Missions tab UI in `ourliberty-dashboard`:
- 1 page route + 6 components
- 2 route handlers (read + write)
- 1 new client-helper module + types extension
- Nav.tsx insert
- Vitest tests + regression guard

**No droplet repo changes.** All droplet-side work shipped in PR-A.

Estimated chain spend: $7-9. Mirror revision rounds expected: 0-1.

---

## Reuse points (from current `ourliberty-dashboard` main)

| Artifact | Path | Use |
|---|---|---|
| `extractHeadline(event)` | `lib/approval-queries.ts:151` | MissionCard title — import + reuse, do NOT re-implement |
| `ApprovalQueryError` pattern | `lib/approval-queries.ts:15-28` | Copy as `MissionQueryError` in `lib/mission-queries.ts` |
| `fail(op, error)` helper shape | `lib/approval-queries.ts:25-28` | Copy as `fail` in `lib/mission-queries.ts` |
| `getSupabaseServer()` | `lib/supabase-server.ts` | Chain-events query in `mission-queries.ts` |
| `getUser()` + `isAllowed(email)` | `lib/auth-server.ts` + `lib/auth.ts` | Auth gate on `POST /api/missions/new` |
| Droplet proxy (GET-only) | `app/api/proxy/[...path]/route.ts` | Already allowlists `api/system/*` — `GET /api/proxy/api/system/missions` works as-is from client. **But:** server-side route handler will do its own fetch to droplet for the join (see Architecture below). |
| `readServerEnv()` + token | `lib/env.ts` | Direct droplet fetch in route handlers |
| `useDashboardData<T>(path)` | `lib/api.ts` | SWR client hook for proxy-backed reads |
| Approvals route auth pattern | `app/api/approvals/list/route.ts:51-58` | Copy `getUser() + isAllowed()` gate for POST |
| Chain-events query helpers | `lib/system-queries.ts` — `getRecentChainEvents`, `getLatestSessionStartByTaskId`, `getLatestEventTsByTaskId` | Reuse for phase derivation |

---

## Architecture: read vs write path

### Read path (`/api/missions/list`)

Server-side join, **not** client-side. Route handler:

1. Fetches `missions.json` from droplet via `fetch(\`${env.apiUrl}/api/system/missions\`, { headers: { "X-Dashboard-Token": env.apiToken } })` — direct, not via the catch-all proxy, because we also need to query Supabase in the same handler.
2. Queries Supabase `chain_events` for all task_ids referenced across missions (single query with `IN` clause).
3. Joins in TypeScript using `lib/mission-queries.ts` helpers.
4. Returns `MissionListResponse` to client.

Client uses `useSWR<MissionListResponse>("/api/missions/list", ...)` with 10s/30s/60s cadence per spec § 5.7. **Direct fetch path**, not `useDashboardData` (that hook targets `/api/proxy/*`).

### Write path (`/api/missions/new`)

Route handler:

1. Auth gate: `const user = await getUser(); if (!user || !isAllowed(user.email)) return jsonResponse(401, { error: "unauthenticated" });`
2. Parse + validate body: `{ name: string, brief: string, repo: string, spec_docs?: string[] }`.
3. Direct fetch POST to droplet `${env.apiUrl}/api/system/missions/new` with token header.
4. Pass-through droplet response (PR URL on success, 409 on id collision, 502 on push/gh failure).

**The catch-all proxy stays GET-only.** Do NOT extend it to support POST — keep the trust boundary tight per the proxy's existing comment block.

---

## Files in scope

### NEW files

```
app/missions/page.tsx
app/missions/components/PhaseColumn.tsx
app/missions/components/MissionRow.tsx
app/missions/components/MissionCard.tsx
app/missions/components/MissionDetailPanel.tsx
app/missions/components/OrphansLane.tsx
app/missions/components/NewMissionModal.tsx
app/api/missions/list/route.ts
app/api/missions/new/route.ts
lib/mission-queries.ts
app/missions/__tests__/page.test.tsx
app/missions/components/__tests__/MissionRow.test.tsx
app/missions/components/__tests__/MissionCard.test.tsx
app/missions/components/__tests__/NewMissionModal.test.tsx
lib/__tests__/mission-queries.test.ts
app/api/missions/list/__tests__/route.test.ts
app/api/missions/new/__tests__/route.test.ts
```

### MODIFIED files

- `components/Nav.tsx` — insert Missions in slot 2 (verbatim diff below)
- `lib/types.ts` — append Mission + MissionPhase + MissionListResponse types (verbatim below)

No other modifications. Existing list/action/proxy tests must keep passing.

---

## Verbatim: lib/types.ts append

Append after the existing types (do not modify existing types). Place near the chain_events related types:

```typescript
// E4.4f Missions tab — registry entry shape mirrors agents/beacon/missions.json
// (see agent-core PR #159). Schema version 1.

export type MissionPhase =
  | "drafting"
  | "ready"
  | "in_flight"
  | "awaiting_merge"
  | "shipped"
  | "deferred";

export interface MissionEntry {
  id: string;
  name: string;
  phase: MissionPhase;          // hint only; live phase derived from data per spec § 5.2
  brief: string;
  spec_docs: string[];
  task_ids: string[];
  repo: string;
  created: string;              // ISO date
  deferred_reason: string | null;
}

export interface TaskPhaseState {
  task_id: string;
  derived_phase: MissionPhase;
  last_event_ts: string | null;
  pr_url: string | null;
  pr_state: "OPEN" | "CLOSED" | "MERGED" | null;
  agent: AgentName | null;
}

export interface Mission extends MissionEntry {
  tasks: TaskPhaseState[];
  aggregate_phase: MissionPhase;   // most-advanced non-shipped phase among tasks, else "shipped" if all shipped, else hint
}

export interface OrphanTask {
  task_id: string;
  last_event_ts: string;
  agent: AgentName | null;
  pr_url: string | null;
}

export interface MissionListResponse {
  missions: Mission[];
  orphans: OrphanTask[];
  last_synced_at: string;          // from droplet missions.json mtime
  as_of: string;                    // server response timestamp
}

export interface NewMissionRequest {
  name: string;
  brief: string;
  repo: string;
  spec_docs?: string[];
}

export interface NewMissionResponse {
  pr_url: string;
  mission_id: string;
}
```

---

## Verbatim: lib/mission-queries.ts skeleton

```typescript
import { getSupabaseServer } from "./supabase-server";
import { extractHeadline } from "./approval-queries";
import type {
  ChainEvent,
  Mission,
  MissionEntry,
  MissionPhase,
  OrphanTask,
  TaskPhaseState,
} from "./types";

export class MissionQueryError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = "MissionQueryError";
  }
}

function fail(op: string, error: unknown): never {
  throw new MissionQueryError(`mission-queries: ${op} failed`, error);
}

// Phase derivation rules per spec § 5.2 — must match exactly.
// Inputs: chain_events for one task_id (newest first) + optional PR state.
// Output: the derived MissionPhase.
export function derivePhaseForTask(args: {
  task_id: string;
  events: ChainEvent[];            // ordered newest-first
  pr_state: "OPEN" | "CLOSED" | "MERGED" | null;
}): MissionPhase {
  const { events, pr_state } = args;
  // Rule order from spec § 5.2 (most-specific first):
  // 1. Shipped: any auto_merge event OR pr_state === "MERGED"
  // 2. Awaiting merge: mirror_review_pass marker OR escalation-type event AND no auto_merge AND pr_state === "OPEN"
  // 3. In flight: session_start event (forge or mirror) AND no auto_merge
  // 4. Ready: (caller-side decision — task_id exists but no session_start yet)
  // Implementation must follow this order; spec § 5.2 is authoritative.
  // TODO(forge): implement per spec § 5.2 verbatim.
  throw new Error("not implemented");
}

// Mission-aggregate phase: the most-advanced non-shipped phase among tasks;
// "shipped" only if all tasks shipped. If mission has no tasks, falls back
// to mission.phase hint.
export function aggregateMissionPhase(
  entry: MissionEntry,
  tasks: TaskPhaseState[],
): MissionPhase {
  if (entry.phase === "deferred") return "deferred";
  if (tasks.length === 0) return entry.phase;
  // TODO(forge): implement per spec § 5.2 closing paragraph.
  throw new Error("not implemented");
}

// Orphan detection: task_ids in chain_events but not in any mission.task_ids.
// 30-day cutoff per spec § 5.3. Sorted most-recent first.
export function detectOrphans(args: {
  events: ChainEvent[];            // 30-day window, any order
  registeredTaskIds: Set<string>;
}): OrphanTask[] {
  // TODO(forge): implement per spec § 5.3.
  throw new Error("not implemented");
}

// Fetches chain_events for a set of task_ids in a single query.
// Used by /api/missions/list to batch-load events for all mission tasks
// plus the 30-day orphan window in one round-trip per query group.
export async function fetchEventsForTaskIds(taskIds: string[]): Promise<Map<string, ChainEvent[]>> {
  if (taskIds.length === 0) return new Map();
  const { data, error } = await getSupabaseServer()
    .from("chain_events")
    .select("*")
    .in("task_id", taskIds)
    .order("ts", { ascending: false });
  if (error) fail("fetchEventsForTaskIds", error);
  const rows = (data ?? []) as ChainEvent[];
  const out = new Map<string, ChainEvent[]>();
  for (const ev of rows) {
    const list = out.get(ev.task_id) ?? [];
    list.push(ev);
    out.set(ev.task_id, list);
  }
  return out;
}

// Fetches all chain_events in the trailing 30 days for orphan detection.
export async function fetchRecentChainEvents(days: number = 30): Promise<ChainEvent[]> {
  const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
  const { data, error } = await getSupabaseServer()
    .from("chain_events")
    .select("*")
    .gte("ts", since)
    .order("ts", { ascending: false });
  if (error) fail("fetchRecentChainEvents", error);
  return (data ?? []) as ChainEvent[];
}

// Re-export for component use (cards display extracted headlines).
export { extractHeadline };
```

Forge implements the `TODO(forge)` bodies per the spec § 5.2/5.3 rules. The structure and signatures above are locked.

---

## Verbatim: components/Nav.tsx diff

In `components/Nav.tsx`, change the `LINKS` array to insert Missions in slot 2 (after Programs, before Approvals). Update the slot-2 comment on Approvals to reflect the new ordering.

**OLD (verbatim):**

```typescript
const LINKS: { href: string; label: string }[] = [
  { href: "/", label: "Programs" },
  // E4.4e PR-D — Approvals sits in slot 2 (right after Programs) per spec
  // § 8.1 so Larry's eye lands on it first. Page is auth-gated by proxy.ts;
  // anon visitors get bounced to /login before render.
  { href: "/approvals", label: "Approvals" },
```

**NEW (verbatim):**

```typescript
const LINKS: { href: string; label: string }[] = [
  { href: "/", label: "Programs" },
  // E4.4f — Missions in slot 2 (technical coordination surface; spec
  // agent-core/agents/beacon/specs/e4-4f-missions-tab-v1.md). Read-only,
  // anon-accessible.
  { href: "/missions", label: "Missions" },
  // E4.4e PR-D — Approvals shifted from slot 2 to slot 3 by E4.4f. Page
  // is auth-gated by proxy.ts; anon visitors get bounced to /login before
  // render.
  { href: "/approvals", label: "Approvals" },
```

Active-link logic: no changes needed — `/missions` will match exact and prefix per the existing fallthrough rule.

---

## Verbatim: phase derivation table (spec § 5.2 — authoritative)

| Phase | Derivation rule |
|---|---|
| `drafting` | Mission has no `spec_docs` AND no `task_ids` (concept only) |
| `ready` | Mission has `spec_docs` but `task_ids[]` is empty OR none have a `session_start` event yet |
| `in_flight` | Task has `session_start` event (forge or mirror) AND no `auto_merge` event |
| `awaiting_merge` | Task has `mirror_review_pass` marker OR `escalation`-type chain_event AND no `auto_merge` event yet AND PR.state is OPEN |
| `shipped` | Task has `auto_merge` event OR PR.state is MERGED |
| `deferred` | Mission `phase` hint is `"deferred"` (overrides all task-level derivation) |

Mission-aggregate phase: most-advanced non-shipped phase among the mission's tasks; `"shipped"` only if all tasks shipped. If no tasks, falls back to mission `phase` hint.

---

## Verbatim: SWR cadences (spec § 5.7)

```typescript
const ACTIVE_KANBAN_REFRESH_MS = 10_000;   // 10s
const SHIPPED_TODAY_REFRESH_MS = 30_000;   // 30s
const DEFERRED_REFRESH_MS = 60_000;        // 60s
```

Each bucket gets its own SWR key with its own `refreshInterval`. Bucket data is fetched from the same `/api/missions/list` route but with `?bucket=active|shipped_today|deferred` query param; the route handler filters server-side.

Browser-local midnight for "Shipped today" boundary: pass `since=<iso>` query param computed in the client (same pattern as Approvals tab; see `app/approvals/page.tsx` for browser-midnight ISO generation).

---

## Component signatures (props locked)

### `app/missions/page.tsx`

Server component shell with client-side bucket children (each does its own SWR poll):

```tsx
export default function MissionsPage() {
  return (
    <div className="space-y-6">
      <MissionsTopStrip />
      <ActiveKanban />           // useSWR refresh 10s
      <ShippedTodaySection />    // useSWR refresh 30s, collapsed by default
      <DeferredSection />        // useSWR refresh 60s, collapsed by default
    </div>
  );
}
```

### `PhaseColumn.tsx`

```tsx
interface PhaseColumnProps {
  phase: MissionPhase;
  label: string;
  cards: React.ReactNode[];
}
```

### `MissionRow.tsx`

```tsx
interface MissionRowProps {
  mission: Mission;
  onCardClick: (task: TaskPhaseState, mission: Mission) => void;
}
```

Renders one row across all 5 phase columns. Cards positioned in the column matching `task.derived_phase`. Accent stripe color hashed from `mission.id` (use a stable hash → HSL; if no existing color-hash helper, implement a small `hashToHsl(id: string): string` in `lib/mission-queries.ts`).

### `MissionCard.tsx`

```tsx
interface MissionCardProps {
  task: TaskPhaseState;
  mission: Mission;
  compact: boolean;
  onClick?: () => void;
}
```

Compact card target height ≤72px. Layout per spec § 5.4. Title via `extractHeadline()` (imported from `lib/mission-queries.ts` re-export).

### `MissionDetailPanel.tsx`

```tsx
interface MissionDetailPanelProps {
  selected: { mission: Mission; task: TaskPhaseState } | null;
  onClose: () => void;
}
```

Slides in from right via Tailwind `transform translate-x-0/full` transition. Shows: mission name+brief, task title+id, chain timeline (vertical newest-first), PR link, spec doc link(s), worktree path (from latest session_start event payload), action affordances per spec § 5.4.

### `OrphansLane.tsx`

```tsx
interface OrphansLaneProps {
  orphans: OrphanTask[];
}
```

Bottom of kanban, demoted styling. Sort newest-first; 30-day window enforced server-side in route handler.

### `NewMissionModal.tsx`

```tsx
interface NewMissionModalProps {
  open: boolean;
  onClose: () => void;
  onSuccess: (response: NewMissionResponse) => void;
}
```

Form fields: `name` (text), `brief` (textarea), `repo` (text), `spec_docs` (comma-separated text → string[]). On submit: POST to `/api/missions/new`; handle 401 (bounce to login) / 409 (show "ID collision") / 502 (show "Push failed") / 200 (call `onSuccess`).

---

## Verbatim: app/api/missions/list/route.ts skeleton

```typescript
import type { NextRequest } from "next/server";
import { MissingEnvError, readServerEnv } from "@/lib/env";
import {
  MissionQueryError,
  derivePhaseForTask,
  aggregateMissionPhase,
  detectOrphans,
  fetchEventsForTaskIds,
  fetchRecentChainEvents,
} from "@/lib/mission-queries";
import type {
  Mission,
  MissionEntry,
  MissionListResponse,
  TaskPhaseState,
} from "@/lib/types";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

export async function GET(_request: NextRequest): Promise<Response> {
  let env;
  try {
    env = readServerEnv();
  } catch (err) {
    if (err instanceof MissingEnvError) {
      return jsonResponse(500, { error: "server misconfigured" });
    }
    throw err;
  }

  // 1. Fetch missions.json from droplet (direct, not via [...path] proxy).
  let dropletJson: { missions: MissionEntry[]; last_synced_at: string };
  try {
    const upstream = await fetch(`${env.apiUrl}/api/system/missions`, {
      headers: { "X-Dashboard-Token": env.apiToken, Accept: "application/json" },
      cache: "no-store",
    });
    if (!upstream.ok) {
      return jsonResponse(502, { error: "droplet missions endpoint failed", status: upstream.status });
    }
    dropletJson = await upstream.json();
  } catch (err) {
    return jsonResponse(502, { error: "droplet unreachable" });
  }

  // 2. Collect all task_ids across all missions.
  const registeredTaskIds = new Set<string>();
  for (const m of dropletJson.missions) {
    for (const tid of m.task_ids) registeredTaskIds.add(tid);
  }

  // 3. Batch-fetch chain_events for registered task_ids.
  const eventsByTaskId = await fetchEventsForTaskIds([...registeredTaskIds]);

  // 4. TODO(forge): per-task PR-state lookup. v1 simplification — pass null
  //    pr_state and let derivePhaseForTask handle null gracefully (spec § 5.2
  //    rules degrade cleanly without PR state).

  // 5. Build Mission objects with derived phases.
  const missions: Mission[] = dropletJson.missions.map((entry) => {
    const tasks: TaskPhaseState[] = entry.task_ids.map((tid) => {
      const events = eventsByTaskId.get(tid) ?? [];
      const derived_phase = derivePhaseForTask({ task_id: tid, events, pr_state: null });
      // TODO(forge): extract last_event_ts, pr_url (from event payload), agent
      return { task_id: tid, derived_phase, last_event_ts: null, pr_url: null, pr_state: null, agent: null };
    });
    return { ...entry, tasks, aggregate_phase: aggregateMissionPhase(entry, tasks) };
  });

  // 6. Orphans — chain_events from last 30d whose task_id is NOT in registeredTaskIds.
  const recentEvents = await fetchRecentChainEvents(30);
  const orphans = detectOrphans({ events: recentEvents, registeredTaskIds });

  const response: MissionListResponse = {
    missions,
    orphans,
    last_synced_at: dropletJson.last_synced_at,
    as_of: new Date().toISOString(),
  };
  return jsonResponse(200, response);
}
```

---

## Verbatim: app/api/missions/new/route.ts skeleton

```typescript
import type { NextRequest } from "next/server";
import { MissingEnvError, readServerEnv } from "@/lib/env";
import { getUser } from "@/lib/auth-server";
import { isAllowed } from "@/lib/auth";
import type { NewMissionRequest, NewMissionResponse } from "@/lib/types";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

export async function POST(request: NextRequest): Promise<Response> {
  // Auth gate — Supabase Auth + Google login + allowlist (PR-C pattern).
  const user = await getUser();
  if (!user || !isAllowed(user.email)) {
    return jsonResponse(401, { error: "unauthenticated" });
  }

  let env;
  try {
    env = readServerEnv();
  } catch (err) {
    if (err instanceof MissingEnvError) {
      return jsonResponse(500, { error: "server misconfigured" });
    }
    throw err;
  }

  let body: NewMissionRequest;
  try {
    body = await request.json();
  } catch {
    return jsonResponse(400, { error: "invalid JSON body" });
  }
  if (!body.name || !body.brief || !body.repo) {
    return jsonResponse(400, { error: "missing required fields: name, brief, repo" });
  }

  try {
    const upstream = await fetch(`${env.apiUrl}/api/system/missions/new`, {
      method: "POST",
      headers: {
        "X-Dashboard-Token": env.apiToken,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const bodyText = await upstream.text();
    return new Response(bodyText, {
      status: upstream.status,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return jsonResponse(502, { error: "droplet unreachable" });
  }
}
```

---

## Test plan

### Vitest — unit / component

| File | Coverage |
|---|---|
| `lib/__tests__/mission-queries.test.ts` | `derivePhaseForTask` — one fixture per phase rule in spec § 5.2; `aggregateMissionPhase` — empty tasks + mixed phases + all-shipped; `detectOrphans` — registered + unregistered task_ids; `fetchEventsForTaskIds` — empty input + grouping; `fetchRecentChainEvents` — 30-day cutoff |
| `app/missions/__tests__/page.test.tsx` | Renders 3 buckets; collapsed sections collapsed by default |
| `app/missions/components/__tests__/MissionRow.test.tsx` | Cards positioned in correct columns per fixture; accent stripe deterministic from mission id |
| `app/missions/components/__tests__/MissionCard.test.tsx` | Compact vs expanded layout; uses `extractHeadline` (mock + assert call) |
| `app/missions/components/__tests__/NewMissionModal.test.tsx` | Form validation; POST happy path; 401 / 409 / 502 error rendering |
| `app/api/missions/list/__tests__/route.test.ts` | Happy path (mock droplet fetch + mock Supabase); droplet 502 propagates; droplet unreachable returns 502; orphans included |
| `app/api/missions/new/__tests__/route.test.ts` | 401 anon; 401 non-allowlisted; 400 missing fields; happy path proxies droplet response; droplet 409 / 502 pass-through |

### Regression guard

Existing dashboard test suite must keep passing — `app/api/approvals/list/__tests__/route.test.ts`, `app/api/proxy/[...path]/__tests__/route.test.ts`, `app/api/operations/**/__tests__/`, `lib/__tests__/approval-queries.test.ts`, `lib/__tests__/system-queries.test.ts`. The Nav.tsx slot reorder is the only change touching shared UI.

---

## Acceptance criteria

Per spec § 6, all must hold after merge + Vercel deploy:

1. Visit `dashboard.ourliberty.dev/missions` — page renders with no console errors.
2. Active kanban shows the seeded `missions-tab-v1` mission with at least one task card.
3. Deferred section (collapsed) header shows count `1` (the seeded `e4-4b-projects-kanban` entry).
4. Card click → side panel opens within 1s with chain timeline + spec link.
5. Nav order: Programs / Missions / Approvals / Live System / Operations / Build Sequences / Tasks / Costs / Healers.
6. Anon visitor can read `/missions` (no auth bounce).
7. Anon visitor clicking "+ New mission" → modal opens; submit → 401 with login prompt (or modal CTA to log in).
8. Logged-in allowlisted user can submit "+ New mission" → PR opens on agent-core.

---

## Out of scope (deferred to v2)

Per spec § 7 — explicitly NOT in PR-B:

- Drag-drop between phase columns
- Inline mission editing (name / brief / deferred_reason)
- Mission dependency tracking
- Cost rollups / time estimates
- Mobile responsive layout
- Auto-archival after 90 days
- Mission templates

Do not implement these. Surface as TODOs only if a stub is unavoidable for type completeness.

---

## End of brief
