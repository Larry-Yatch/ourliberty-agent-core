# Brief: generalize the Agent Queue panel to all agents (per-agent lifecycle)

Audience: Forge. Two phases, two repos. Phase A = backend
(`ourliberty-agent-core`). Phase B = UI (`ourliberty-dashboard`). Mirror
reviews each. Regression dial 3 — do not break existing dashboard tests, and
do NOT change forge's current lane behavior.

## Goal

Generalize `GET /api/system/agent-queue?agent=<name>` and the System-tab
panel from forge-only to all four tracked agents (`AGENT_NAMES = beacon,
forge, mirror, pulse`), with lanes that fit each agent's real lifecycle.

## Archetype model (DECIDED — do not re-litigate)

- **BUILDER = forge** (worktree_enabled, opens PRs). Lanes:
  `queued / building / in_review / done_today(merged|changes_requested|failed)`.
  **UNCHANGED from today.**
- **WORKER = mirror, beacon, pulse.** Lanes:
  `queued / active / done_today(succeeded|failed)`.
  (Mirror uses worktrees but reviews rather than ships — its outcome is its
  session result, not a merge, so it is a worker.)

## Data sources (all verified live on the droplet)

- **queued (ALL agents):** existing `_reader_agent_queue_queued` (inbox scan,
  `scan_inbox` rule). No change.
- **building (BUILDER only):** existing `_reader_agent_queue_building`
  (worktree + `is_in_flight`, branch + age). No change.
- **active (WORKER only) — NEW:** read the in-flight registry
  `<agents_root>/state/in-flight/*.json` (env-overridable via
  `OURLIBERTY_AGENTS_ROOT`). Each entry is JSON
  `{ "task_stem": str, "agent_id": str, "pid": int, "started_at": ISO }`.
  Filter `agent_id == <agent>`; emit `{ task_id: task_stem,
  age_seconds: now(UTC) - started_at }`, newest-first. Degrade to `[]` if the
  dir is missing. (Optional: drop entries whose pid is dead via
  `os.kill(pid, 0)` — keep it safe, never raise.)
- **in_review (BUILDER only):** existing logic. No change.
- **done_today (BUILDER = forge):** existing review-verdict attribution
  (`merged|changes_requested|failed`). No change.
- **done_today (WORKER) — NEW:** today's `session_done` chain_events for
  `agent == <agent>` (UTC day boundary, reuse `_reader_costs_today`'s
  convention), classified by `payload.success` (bool, confirmed present on
  beacon/mirror/pulse): `True -> 'succeeded'`, `False -> 'failed'`. Item:
  `{ task_id, outcome: succeeded|failed, at: ts, message: payload.message
  (optional, truncated) }`. Dedup by task_id keeping latest ts; sort by `at`
  desc. NOTE: `_fetch_chain_events_for_agent` currently selects
  `'agent,event_type,task_id,pr_url,ts'` — **add `payload`** to that
  projection so the worker done lane can read `payload.success`.

## Response shape

Add `archetype: "builder" | "worker"` to `AgentQueueResponse`. Keep ALL lane
keys present (empty list when N/A) so the response is uniform; `archetype`
tells the UI which lanes to render:

- builder: `queued`, `building`, `in_review`, `done_today`
  (done item: `{task_id, pr_url, outcome ∈ merged|changes_requested|failed,
  reason, at}`)
- worker: `queued`, `active` (item `{task_id, age_seconds}`), `done_today`
  (done item: `{task_id, outcome ∈ succeeded|failed, at, message}`)

Validate `agent` against `AGENT_NAMES` (400 on unknown). Auth via
`_require_token`. Degrade to empty review/done/active lanes when the supabase
client is None or the in-flight dir is absent — never 500.

## Phase A — backend (`scripts/dashboard_api.py`)

Implement archetype routing + the new `active` and worker-`done_today`
readers + the `archetype` response field + the `payload` projection add.

TEST DISCIPLINE (learned the hard way on PR #303): tests MUST exercise the
real fetch/parse path, not mock it away. The `_ChainEventsClient` stub MUST
already honor column projection (it does after #312) — keep it honest so a
missing `payload` column would be caught. Add a real in-flight-registry
tmpdir fixture (write JSON files under `state/in-flight/` and assert the
`active` lane parses them); do NOT stub the parse. Cover: worker
queued/active/done(succeeded AND failed), builder lanes UNCHANGED
(regression), the `archetype` field per agent, unknown-agent 400,
supabase-None + missing-in-flight-dir degradation. Full dashboard suite
green; dial 3. Open a PR; Mirror reviews.

## Phase B — UI (`ourliberty-dashboard`)

Generalize `app/operations/system/components/ForgeQueuePanel.tsx` (rename to
`AgentQueuePanel`) + `lib/types.ts`.

- Add an agent tab/selector (`beacon, forge, mirror, pulse`) at the panel
  top; default `forge`. Fetch the selected agent via
  `useDashboardData('/api/system/agent-queue?agent=<sel>', { refreshInterval: 30_000 })`.
- Render lanes per `response.archetype`: `builder` -> the existing 4 lanes
  (unchanged); `worker` -> `Queued / Active (task_id + age) / Done today`
  (succeeded = green badge, failed = red badge).
- Panel title becomes `Agent Queue` (was `Forge Queue`); keep its position
  directly under Active Sessions.
- Update `lib/types.ts`: add `archetype`, worker lane item types, and extend
  the done outcome union to `merged|changes_requested|failed|succeeded`.
  Tests (vitest+RTL): render a builder (forge) lane set AND a worker (e.g.
  pulse) lane set; tab switch; succeeded/failed badges. npm test + lint +
  typecheck green; dial 3. Open a PR; Mirror reviews.

## BUILD MUST VERIFY

- In-flight entry schema field names (`task_stem`, `agent_id`, `pid`,
  `started_at`) and that `OURLIBERTY_AGENTS_ROOT` redirects `state/in-flight`
  for tests.
- `session_done.payload.success` is the success bool (confirmed); `message`
  also present.
- The existing `AgentQueueResponse` shape — forge's lanes must stay
  byte-for-byte compatible so the deployed forge panel is unaffected until
  Phase B ships.

## Out of scope

`build_sequence_advancer` (not in `AGENT_NAMES`). No new deps. No change to
forge's queued/building/in_review/done behavior beyond adding `payload` to
the shared chain_events select.
