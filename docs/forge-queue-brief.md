# Brief: agent-queue lifecycle endpoint (Forge Queue panel, Phase 1)

Audience: Forge. Target repo: `ourliberty-agent-core`. One new read-only
endpoint + Pydantic models + tests in `scripts/dashboard_api.py`. Mirror
reviews after. Regression dial 3 — do not break existing dashboard tests.

This brief covers **Phase 1 (API only)**. Phase 2 (the Next.js UI panel in
the separate `ourliberty-dashboard` repo) is a follow-up dispatch and is out
of scope here.

## Goal

Add `GET /api/system/agent-queue?agent=forge` — a read-only endpoint that
returns one agent's dispatch lifecycle as four lanes:

- `queued` — dispatches sitting in the agent's inbox, not yet picked up
- `building` — dispatches actively in-flight (worktree open)
- `in_review` — built, PR opened, awaiting Mirror verdict
- `done_today` — terminal outcomes (merged / failed) from today only (UTC)

The endpoint is **generic** (agent is a query param, defaulting to `forge`)
so the same endpoint serves all agents later. We only wire/verify Forge now.

## Why now

The dashboard surfaces outcomes (costs, recent tasks, healer status) but not
work-in-progress — the waiting line of dispatches and what is mid-build.
Today that is only visible by SSHing into the droplet and reading inbox
files. Queue depth is a real operational signal (a monotonically growing
queue means wrong-audience / no-closure, not "behind"). This turns it into a
glance.

## Key facts (already true on `main` — reuse, do not reinvent)

- `scripts/dashboard_api.py` is FastAPI on `127.0.0.1:8000`. Auth is the
  `X-Dashboard-Token` header enforced by the existing `_require_token`
  dependency. Every response model carries an `as_of` (or `captured_at`) ISO
  timestamp. Reader functions take an `agents_root: Path` and are unit-tested
  in isolation via the `OURLIBERTY_AGENTS_ROOT` tmpdir convention.

- **QUEUED is already implemented generically.** `_agent_inbox_pending(
  agents_root, agent)` returns `(count, sorted_task_ids)` of pending inbox
  JSON files for any agent, scanning `inboxes/<agent>/`. Reuse it. For
  wait-time, the inbox file **mtime** is the arrival timestamp (this is how
  `inbox_watcher.scan_inbox()` orders the queue — oldest first).

- **BUILDING is already exposed.** The `/api/system/worktrees` reader returns
  `SystemWorktree { name, agent, task_id, branch, age_seconds, is_in_flight }`.
  Reuse that reader; filter to `agent == <agent>` and `is_in_flight == true`.

- **chain_events read machinery already exists in this file.** Use the
  existing supabase client accessor (the one behind the larry-action /
  stuck-sessions endpoints — `_get_larry_action_supabase_client()`, which
  returns `None` when env is unset or supabase-py is absent) and
  `supabase_client.table('chain_events').select(...)`. There is also a
  `triage_decisions._fetch(client, event_type=...)` helper used nearby.
  Do NOT add a new client or new dependency.

- **chain_events event_type vocabulary (verified in use on `main`):**
  `session_start`, `session_done`, `review_request`,
  `review_pass`, `review_revision`, `review_escalate`, `auto_merge`,
  `marker_error`, `preflight_reject`, `cost_budget`. Rows carry `agent`,
  `task_id`, `pr_url`, `ts` (timestamptz), `payload` (jsonb).

- **Day-boundary convention is UTC.** `_reader_costs_today` uses
  `now = datetime.now(timezone.utc); today = now.date()` and filters
  `dt.astimezone(timezone.utc).date() != today`. `done_today` MUST reuse this
  exact convention so the lane self-clears at UTC midnight.

## Lane definitions (the only new logic)

For the requested `agent` (default `forge`):

1. **queued** — from `_agent_inbox_pending(agents_root, agent)`. For each
   pending task_id emit `{ task_id, waited_seconds }` where
   `waited_seconds = now - inbox_file_mtime`. Oldest first.

2. **building** — from the worktrees reader, filtered to `agent == agent`
   and `is_in_flight`. Emit `{ task_id, branch, age_seconds }`.

3. **in_review** — from `chain_events` for this `agent`: a task whose most
   recent relevant event is `review_request` with **no later terminal event**
   (`auto_merge` / `marker_error` / `preflight_reject` / `cost_budget` /
   `review_escalate`) for the same `task_id`. Emit
   `{ task_id, pr_url, since }` where `since` is the `review_request` `ts`.
   (For agents with no review phase this lane is naturally empty — fine.)

4. **done_today** — from `chain_events` for this `agent`, `ts` UTC-date ==
   today only:
   - `auto_merge` → `outcome: "merged"`
   - `marker_error` / `preflight_reject` / `cost_budget` / `review_escalate`
     → `outcome: "failed"` (carry the event_type as `reason`)
   Emit `{ task_id, pr_url, outcome, reason, at }` (`at` = event `ts`).
   This is a rolling daily window — NO storage, NO accumulation surface.

## Endpoint contract

```
GET /api/system/agent-queue?agent=forge
-> AgentQueueResponse {
     agent: str,
     queued:     list[QueuedItem   { task_id, waited_seconds }],
     building:   list[BuildingItem { task_id, branch, age_seconds }],
     in_review:  list[ReviewItem   { task_id, pr_url, since }],
     done_today: list[DoneItem     { task_id, pr_url, outcome, reason, at }],
     captured_at: str,  # ISO, UTC
   }
```

- Auth: the existing `_require_token` dependency. Read-only — no writes.
- `agent` query param: default `forge`; validate against the known agent
  names (mirror the set already used elsewhere in the file) and 422/400 on an
  unknown agent rather than scanning an arbitrary path.
- **Graceful degradation:** if the supabase client is `None` (test env / no
  creds), `in_review` and `done_today` return `[]` and the endpoint still
  serves `queued` + `building`. Match how other readers degrade — never 500
  on missing supabase.
- No GitHub API calls. Derive everything from inbox + worktrees + chain_events.

## Tests (`scripts/tests/test_dashboard_api_agent_queue.py`)

Use the `OURLIBERTY_AGENTS_ROOT` tmpdir convention; stub/inject the supabase
client (or assert the `None` path). Cover:

- queued: count, oldest-first order, `waited_seconds` from mtime
- building: filtered to agent + `is_in_flight`
- in_review: `review_request` with no terminal event appears; one WITH a later
  `auto_merge` does NOT
- done_today: merged vs failed classification; an event from yesterday is
  excluded (UTC boundary)
- supabase-`None` degradation: `in_review`/`done_today` empty, no 500
- auth: 401/403 without the token

Do not break the existing `scripts/tests/test_dashboard_api*.py` suite.

## PREFLIGHT MUST VERIFY (resolve before building; CLARIFY if wrong)

1. The exact name/signature of the supabase client accessor to reuse
   (`_get_larry_action_supabase_client` vs a read-only sibling) and the
   `triage_decisions._fetch` query shape — read the larry-action /
   stuck-sessions code and reuse the established pattern verbatim.
2. The canonical set of known agent names already defined in `dashboard_api.py`
   to validate the `agent` param against.
3. That the worktrees reader is callable as a plain function (not only via the
   route) so the new reader can reuse it without an HTTP round-trip.

## Out of scope

- The UI panel (Phase 2, `ourliberty-dashboard` repo — separate dispatch).
- Any write path, any GitHub API call, any new dependency, any new supabase
  client, any persisted/accumulating store for terminal events.
