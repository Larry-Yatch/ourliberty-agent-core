# Spec: E4.4d — Operations Tab + System View (Chain Telemetry MVP-2)

**Status:** Draft (awaiting Larry approval — sub-spec of E4)
**Author:** Claude-as-Forge (written 2026-05-25, Beacon spec round same day, decisions A–F locked via Telegram)
**Approver:** Larry (pending)
**Phase:** E4.4d of `docs/phase-e-plan.md` Phase E4
**Parent spec:** [agents/beacon/specs/e4-overview.md](e4-overview.md)
**Predecessors:** E4.4a shipped 2026-05-24 (read-only MVP); E4.4b/c pending (kanban + CRUD)
**Successor:** Future MVP-3 (auto-remediation actions, escalations panel, cost/memory time-series, historical replay)
**Related:** PR #102 (page-cache fix) + PR #103 (marker-parser + regression-check discipline) shipped 2026-05-25; this spec builds on the lessons

---

## 1. Problem statement (what triggered this)

The 2026-05-25 incident chain was the trigger. Across ~3 hours of debugging:

- Mirror hung 71 min on PR #101 review running a self-matching `pgrep -f` poll loop. Larry had no signal that anything was stuck. He had to ask.
- Pulse investigation surfaced page-cache attribution misread as a memory leak; manual systemctl tuning fixed it. Larry had no signal that memory was climbing — only the watchdog DM at 2G→4G→8G.
- PR #101 was approved by Mirror but invisible to the chain (marker-parser session-lifecycle bug). Larry had no signal that approval had landed.

Every status answer required SSH forensics — journalctl, inbox/outbox dirs, session JSONLs, outbox-notifier logs, cgroup stats, marker files. The existing dashboard at `dashboard.ourliberty.dev` surfaces zero of this. Larry's words: *"I have no clue if they're stuck, if they're working. I have to ask status questions, which sometimes they can give me, or I have to ask you to look. Currently, the live system dashboard seems to be useless."*

E4.4d closes the visibility gap. The substrate exists; the surface doesn't.

---

## 2. Six locked decisions (Telegram spec round, 2026-05-25)

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | Event ingestion path | **Hybrid: poll-based daemon initially, push-compatible schema from day 1** | Poll path doesn't touch the inbox-watcher or outbox-notifier (no production risk during instrumentation). Schema admits either source so a future push-instrumentation PR is purely additive (~1 day Forge work). Confirmed 2026-05-25 round A. |
| B | Supabase schema shape | **`chain_events` table + `agent_sessions` Postgres VIEW + PR pipeline as on-read API route** (Beacon called all three sub-resolutions) | Table is canonical source-of-truth for events; sessions derived from start/end event pairs avoid double-write race; PR pipeline cross-references GitHub state at read time (no stale stored state). B1/B2/B3 sub-resolutions in § 5.1. Locked 2026-05-25 round B. |
| C | Live system data via droplet API | **Extend existing `dashboard-api.service` with three new endpoints: `/api/system/active-sessions`, `/api/system/cgroup-stats`, `/api/system/worktrees`** | One service, one credential surface, one process; matches E3 pattern. Endpoints in § 5.3. Locked 2026-05-25 round C (C1 confirmed by Larry; C2 Beacon decided per his "you decide"). |
| D | Stuck-detection thresholds | **Tighter defaults than gut-instinct, task_type-based gating per agent+phase (not flat per-agent)** | Mirror with regression check legitimately runs 20 min; Forge preflight should rarely exceed 5 min. Flat per-agent thresholds either under-fire on Mirror or over-fire on Forge. Specific thresholds in § 5.4; all live in a config file so tweaks are config-only not code-PRs. Locked 2026-05-25 round D. |
| E | Dashboard tab placement | **Nested `Operations` parent tab; System is first child view** | Larry overrode my YAGNI bias — he named concrete future siblings (Cost, Memory, Worktrees) so the parent tab is justified now. Routing pattern matches Next.js app router conventions used in E4.4a. Locked 2026-05-25 round E. |
| F | Auth posture | **Public for V1; "build it then protect it in a separate project"** | Exposed data is operational metadata only (active task IDs, model names, cost-per-task, cgroup memory) — no PII, no credentials. Cost-per-task patterns are mildly commercially sensitive but Larry explicitly chose to defer. V2 auth will be its own dedicated project, not retrofitted into this surface. Locked 2026-05-25 round F. |

---

## 3. Success criteria

A working System view delivers when ALL of the following are true:

- Larry can open `dashboard.ourliberty.dev/operations/system` and within 5 seconds see whether each agent (Forge / Mirror / Pulse / Beacon) is idle or running; if running, what task and for how long.
- Today's 71-min Mirror hang would have surfaced as a red "stuck" indicator no later than minute 25 of the hang, with the exact `kill <pid>` command shown on the card for copy-paste.
- Today's 2G→4G→8G memory cadence would have been visible in the cgroup-stats view in real time (last update ≤30s old), not first surfaced as a watchdog DM.
- Larry can browse the last 50 chain events (dispatches, completions, marker emits, AUTO_MERGEs, marker-errors) in a scrollable feed, with timestamps in his local zone.
- Pulse escalations (from `pulse-escalations.json`), Beacon larry-alerts (from `larry-alerts.jsonl`), and dispatch sentinel alerts (from `sentinel-alerts.jsonl`) surface in a dedicated Escalations + Alerts panel with `needs_response=true` entries pinned at top. Today's `inbox-watcher-memleak-root-cause` finding (high severity) would have appeared there immediately when Pulse wrote it, with "Mark as read" available to clear it after Larry acts.
- Open PRs (across both `ourliberty-agent-core` and `ourliberty-dashboard` repos) appear in a single table with chain-state cross-referenced: `dispatched-to-forge` / `forge-building` / `pr-opened` / `mirror-reviewing` / `mirror-passed-pending-merge` / `in-revision-loop` / `escalated` / `merged`.
- When a system component fails (Supabase down, ingestion daemon crashed, droplet API unreachable), the dashboard shows a clear warning banner with "last good update" timestamp instead of silently lying with stale data.
- Larry can answer "what's going on right now" without asking me or SSHing.

---

## 4. Out of MVP-2 scope (explicit deferrals to MVP-3 or later)

- **Auto-remediation actions.** Stuck-detector EMITS surface signals only. It never auto-kills a process, restarts a service, or merges a PR. Action remains a human decision. The copy-paste `kill <pid>` recipe (§ 5.6) is the only "action surface" we ship.
- **Cost time-series charts.** Per-task cost shown on Active Sessions card; aggregate cost charts (daily / weekly / per-project) deferred.
- **Memory / CPU time-series charts.** Current memory + peak shown as scalars in cgroup-stats; time-series with sparklines deferred.
- **Historical replay / archeology view.** chain_events stored unbounded; UI shows last 50 in MVP-2. Filtering, search, full-history pagination deferred.
- **Push-instrumented ingestion.** Hybrid schema is push-compatible; push instrumentation in inbox_watcher.py + outbox_notifier.py is a separate PR after MVP-2 validates the schema in production.
- **Auth.** Public for V1 per decision F. V2 auth is its own project, not a retrofit.
- **Operations parent tab additional children.** System is the first and only child in MVP-2. Cost, Memory, Worktrees as sibling children land later when they have content beyond what System already shows.
- **WebSocket / SSE.** Polling at 5-30 second cadence is sufficient; real-time push deferred.
- **Mobile-specific layout.** Responsive web only.

---

## 5. Architecture

### 5.1 Supabase schema (decision B sub-resolutions B1/B2/B3 verbatim)

**B1 — `chain_events` table (canonical event log):**

```sql
CREATE TABLE chain_events (
  event_id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
  emitted_at        timestamptz   NOT NULL,
  ingested_at       timestamptz   NOT NULL DEFAULT now(),
  source            text          NOT NULL CHECK (source IN ('poll', 'push')),
  agent             text          NOT NULL CHECK (agent IN ('forge', 'mirror', 'pulse', 'beacon', 'watcher', 'outbox-notifier', 'healer')),
  event_type        text          NOT NULL,  -- validated application-side; see "event_type discipline" below
  task_id           text          NULL,      -- nullable for events not tied to a task
  pr_url            text          NULL,
  model             text          NULL,      -- 'claude-opus-4-7' etc.
  cost_usd          numeric(10,4) NULL,
  duration_sec      numeric(8,2)  NULL,
  marker_verdict    text          NULL,      -- 'review_pass' | 'review_revision' | 'review_escalate' | 'review_emergency_halt' | 'proceed' | 'clarify_request' | 'reject'
  payload           jsonb         NULL,      -- event-specific extras (claude_session_id, attempts, errors, etc.)
  dedup_hash        text          NOT NULL UNIQUE  -- sha256(emitted_at || agent || event_type || task_id || marker_verdict)
);

CREATE INDEX chain_events_emitted_at_idx ON chain_events (emitted_at DESC);
CREATE INDEX chain_events_task_id_idx ON chain_events (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX chain_events_agent_event_type_idx ON chain_events (agent, event_type);
```

`dedup_hash` is the load-bearing detail: the poll daemon may re-parse the same journal lines after restart. Insert uses `ON CONFLICT (dedup_hash) DO NOTHING` to make ingestion idempotent.

**event_type discipline (application-side validation, not DB-level):**

The DB column is plain `text` with no CHECK constraint and no Postgres ENUM. Type safety happens in code:

- `scripts/chain_event_shipper.py` defines a module-level constant: `KNOWN_EVENT_TYPES = frozenset({...})` listing every accepted value (initial set: `session_start`, `session_done`, `marker_emit`, `auto_merge`, `marker_error`, `cost_budget`, `review_request`, `build_dispatched`, `preflight_proceed`, `preflight_clarify`, `preflight_reject`, `escalation`, `larry_alert`, `sentinel_alert`, `healer_fire`).
- Shipper rejects any event with `event_type not in KNOWN_EVENT_TYPES`, logs a WARN line with the offender, drops the event. Never inserts unknown types.
- Adding a new event type = a single-line PR to the constant, reviewed by Mirror (typos caught at code review).
- A new weekly healer (`scripts/heal_chain_event_type_audit.py`, runs Sundays) executes `SELECT DISTINCT event_type FROM chain_events WHERE emitted_at > now() - interval '7 days'` and DMs Larry if anything landed that's not in the allowlist (belt-and-suspenders against hot-patched code or schema drift).

Standard event-log pattern (Segment / Snowplow / similar). Trades DB-level safety for write-path flexibility; adding new event types ships as code review not migration.

**B2 — `agent_sessions` Postgres VIEW (computed from chain_events):**

```sql
CREATE VIEW agent_sessions AS
SELECT
  s.task_id,
  s.agent,
  s.emitted_at        AS started_at,
  d.emitted_at        AS completed_at,
  s.model,
  s.payload->>'task_type'           AS task_type,
  s.payload->>'claude_session_id'   AS claude_session_id,
  CASE
    WHEN d.event_id IS NULL THEN 'running'
    WHEN d.payload->>'success' = 'true' THEN 'completed'
    ELSE 'failed'
  END                 AS state,
  COALESCE(d.duration_sec,
           EXTRACT(EPOCH FROM (now() - s.emitted_at)))  AS duration_sec_live,
  d.cost_usd          AS cost_usd
FROM chain_events s
LEFT JOIN chain_events d
  ON d.task_id = s.task_id
  AND d.agent = s.agent
  AND d.event_type = 'session_done'
  AND d.emitted_at > s.emitted_at
WHERE s.event_type = 'session_start';
```

Sessions with no matching `session_done` and `started_at > now() - interval '4 hours'` are "running." Beyond 4h they're considered orphaned (matches watcher's `timeout=14400s`) and a downstream healer-monitor surfaces them.

**B3 — PR pipeline state as on-read API route (NOT a Supabase view):**

PR pipeline state is computed live in a Next.js Route Handler at `app/api/operations/pr-pipeline/route.ts`. The handler:

1. Calls `gh pr list --state all --limit 50 --json number,title,state,mergedAt,headRefName --repo Larry-Yatch/ourliberty-agent-core` (and same for `ourliberty-dashboard`).
2. Queries Supabase `chain_events` for events matching each PR's branch name → derives chain state from latest event.
3. Returns merged JSON with derived `chain_state` field per PR.

Reason for live computation: GitHub state changes (merge, close, label add) shouldn't require a Supabase write to reflect. Computed-on-read keeps the truth boundary clean: GitHub owns PR state, Supabase owns chain state, the route merges them.

Cache: 30-second TTL in-process (Next.js fetch cache) to bound `gh` rate-limit consumption.

**RLS + GRANTs (migration discipline checklist):**

Per `project_phase_e4_in_progress` memory entries 30+31 — "Automatically expose new tables UNCHECKED" toggle blocks service_role auto-grants too. Migration `0004_chain_events.sql` MUST include explicit:

```sql
GRANT SELECT, INSERT ON chain_events TO service_role;
GRANT SELECT ON chain_events TO anon;          -- public-read per decision F
GRANT SELECT ON agent_sessions TO service_role, anon;
ALTER TABLE chain_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon-read-only" ON chain_events FOR SELECT TO anon USING (true);
CREATE POLICY "service-role-full" ON chain_events FOR ALL TO service_role USING (true) WITH CHECK (true);
```

This is the same pattern PR #4 (migration 0002) had to hotfix after 0001 shipped without GRANTs. Forge ships these in 0004 from day 1.

### 5.2 Ingestion daemon (decision A — poll initially)

**New service:** `ourliberty-chain-event-shipper.service` (systemd, `Type=simple`, `Restart=on-failure`, `MemoryMax=512M`, `MemoryHigh=256M`).

**Source code:** `scripts/chain_event_shipper.py` in `ourliberty-agent-core`.

**Loop logic — five input sources:**

1. Tail `journalctl -fu ourliberty-inbox-watcher.service --output=json` (session start/done events).
2. Tail `/home/larry/agents/logs/outbox-notifier.log` (marker emits, AUTO_MERGE, marker_error, cost_budget, review_request, build_dispatched events).
3. Poll `~/agents/blackboard/pulse-escalations.json` every 30s (Pulse findings → `event_type='escalation'` rows with `payload.severity`, `payload.headline`, `payload.needs_response`).
4. Tail `~/agents/blackboard/larry-alerts.jsonl` (Beacon DM audit log → `event_type='larry_alert'` rows with `payload.source`, `payload.subject`, `payload.delivered`).
5. Tail `~/agents/blackboard/sentinel-alerts.jsonl` (dispatch sentinel watchdog → `event_type='sentinel_alert'`).
6. Parse each line/entry into chain_events shape (extract `task_id`, `agent`, `pr_url`, `model`, `cost_usd`, `duration_sec`, `marker_verdict`; full original payload as jsonb).
7. Compute `dedup_hash`; `INSERT ... ON CONFLICT (dedup_hash) DO NOTHING` into `chain_events`.
8. Persist resumable cursors per source: `journalctl --cursor-file=/home/larry/agents/state/chain-event-cursor.journal` for journal; (inode, byte_offset) tuple for each log/jsonl file; (file_mtime, content_sha256) for `pulse-escalations.json` (overwritten on each Pulse update, not append-only).

**No backfill of historical data on first run.** Shipper starts from the current end-of-file for each source. Historical events stay in the JSON/log files but don't get backfilled into chain_events. (Future PR can add a one-shot backfill script if needed; not in MVP-2.)

**Graceful degradation:**

- **Supabase unreachable:** buffer up to 10,000 events in `~/agents/state/chain-event-buffer.jsonl` (~5 MB cap). On reconnect, flush buffer in FIFO order. If buffer fills (chronic outage), drop oldest with a `WARN` log entry; healer DMs Larry. NEVER block journal/log reading on Supabase availability.
- **Daemon crash:** systemd `Restart=on-failure` brings it back. Cursor file ensures resume-from-last-event semantics. Worst case: 1-2 sec of events double-inserted, dedup_hash absorbs them.
- **Log rotation mid-read:** detect inode mismatch on next read; close old fd, open new file, re-anchor cursor at byte 0.

**Health signaling:** writes a heartbeat to `/home/larry/agents/blackboard/chain-event-shipper.heartbeat` every 30 seconds. A new healer timer (§ 5.5) reads it and DMs Larry if stale ≥5 min.

### 5.3 Droplet API endpoints (decision C verbatim)

Extends existing `scripts/dashboard_api.py` (Python stdlib HTTP server bound to `127.0.0.1:8000`). Three new endpoints, all GET, no auth (per decision F), JSON responses.

**`GET /api/system/active-sessions`**

Reads cgroup process tree at `/sys/fs/cgroup/system.slice/ourliberty-inbox-watcher.service/cgroup.procs`, walks each PID, parses `/proc/<pid>/cmdline` to detect agent + task_id + model, cross-references with `/home/larry/agents/state/in-flight/*.json` for task metadata.

Response:
```json
{
  "captured_at": "2026-05-25T19:42:13Z",
  "sessions": [
    {
      "pid": 1080351,
      "agent": "mirror",
      "task_id": "chain-discipline-marker-parser-and-regression-check-001",
      "task_type": "review",
      "model": "claude-opus-4-7",
      "started_at": "2026-05-25T19:08:48Z",
      "duration_sec": 1985,
      "stuck": false,
      "stuck_reason": null
    }
  ]
}
```

Cache: 5-second TTL in-process.

**`GET /api/system/cgroup-stats`**

Reads `/sys/fs/cgroup/system.slice/ourliberty-inbox-watcher.service/memory.{current,peak,events}` and `cpu.stat`.

Response:
```json
{
  "captured_at": "2026-05-25T19:42:13Z",
  "memory_current_bytes": 3124076544,
  "memory_peak_bytes": 4489773056,
  "memory_max_bytes": 8589934592,
  "memory_high_bytes": 3221225472,
  "memory_events_max": 868,
  "memory_events_high": 1247,
  "cpu_user_usec": 6678234567,
  "cpu_system_usec": 891234567
}
```

Cache: 5-second TTL.

**`GET /api/system/worktrees`**

Reads `/home/larry/agent-worktrees/` directory listing, parses each worktree's branch + agent + task_id from name, stat's mtime for age, cross-references with in-flight registry.

Response:
```json
{
  "captured_at": "2026-05-25T19:42:13Z",
  "worktrees": [
    {
      "name": "wt-forge-chain-discipline-marker-parser-and-regression-chec",
      "agent": "forge",
      "task_id": "chain-discipline-marker-parser-and-regression-check-001",
      "branch": "forge/chain-discipline-marker-parser-and-regression-check-001",
      "age_seconds": 2310,
      "size_mb": 412,
      "is_in_flight": false,
      "expires_at": "2026-05-25T23:08:46Z"
    }
  ]
}
```

Cache: 30-second TTL (filesystem stat is cheap but `du -s` per worktree adds up).

**Caddy routing:** existing reverse proxy already terminates `dashboard.ourliberty.dev` and proxies to Vercel. New endpoints reachable via the existing Next.js `app/api/proxy/[...path]/route.ts` route handler (extend the allowlist to include `/api/system/*` paths). No Caddy config change required.

**Error contracts:** every endpoint returns `{"error": "<machine-readable-code>", "message": "<human>"}` on failure with appropriate HTTP status. Specific cases:
- `ENOENT` on cgroup path (service stopped): `503` `service-unavailable`.
- Process died mid-read (PID gone): omit that session, don't fail the whole response.
- Cursor file corrupted: `500` `cursor-corrupted` — operator must reset.

### 5.4 Stuck-detection thresholds (decision D verbatim)

Thresholds live in `config/system_tab_thresholds.json`:

```json
{
  "_meta": {
    "owner": "stuck-detector",
    "tweak_via": "edit this file + systemctl reload ourliberty-chain-event-shipper",
    "rationale": "Tighter defaults per decision D1; loosen with production data over first 2 weeks"
  },
  "session_duration_seconds": {
    "forge": {
      "preflight":  { "warn": 300,  "alert": 600  },
      "build":      { "warn": 900,  "alert": 1800 },
      "_default":   { "warn": 600,  "alert": 1200 }
    },
    "mirror": {
      "review":            { "warn": 600,  "alert": 1200 },
      "review-regression": { "warn": 1200, "alert": 1800 },
      "_default":          { "warn": 900,  "alert": 1500 }
    },
    "pulse": {
      "cycle":         { "warn": 900,  "alert": 1800 },
      "investigation": { "warn": 1800, "alert": 3600 },
      "_default":      { "warn": 1200, "alert": 2400 }
    },
    "beacon": {
      "notify":   { "warn": 180, "alert": 300 },
      "approval": { "warn": 300, "alert": 600 },
      "_default": { "warn": 240, "alert": 480 }
    }
  },
  "no_journal_output_seconds": {
    "_default": { "warn": 300, "alert": 600 }
  },
  "inbox_envelope_not_picked_up_seconds": {
    "_default": { "warn": 300, "alert": 600 }
  }
}
```

**Reading the table:** session running longer than `warn` shows yellow stuck indicator in the dashboard; longer than `alert` shows red. Today's 71-min Mirror hang would have hit `alert=1200s` (20 min) at minute 20 of the hang and shown red ever since.

**Task-type detection:** the chain_event_shipper writes the dispatched `task_type` and `phase` from the envelope into `chain_events.payload.task_type`. Stuck-detector keys lookup off `(agent, task_type)`. Unknown task_types fall back to `_default`.

**Mirror "review-regression" detection:** Mirror runs `python3 scripts/test_regression_check.py` when reviewing. The shipper detects this from the journal "Running" line OR (more reliably) by reading Mirror's session JSONL for the test_regression_check.py bash call. If detected within 60s of session start, the threshold elevates to `review-regression` for that session.

**Tightening with data:** Pulse runs Check III (§ 5.10) every 14 days against `chain_events`, computes p90/p99 per `(agent, task_type)` bucket, and proposes refined thresholds via Telegram DM. Larry approves with a one-line shortcut; Beacon applies via a small Claude-as-Forge PR. Self-optimizing loop, no manual analysis cadence required.

### 5.5 Stuck-detector deployment shape (MVP-2 surface-only)

**Stuck detection runs in TWO places, each for a different surface:**

1. **Computed-on-read in the droplet API** (`/api/system/active-sessions` response includes `stuck` + `stuck_reason` per session). Reads thresholds, compares against live duration. This drives the dashboard's red/yellow indicator. Cheap.

2. **Healer-based for alerting** (NEW `ourliberty-heal-stuck-session.timer`, every 5 min): reads same thresholds, walks active sessions, identifies new stuck-state crossings (transitioning warn→alert that haven't been DMed). DMs Larry via `larry_alerts.append_alert(source='heal-stuck-session', subject=<task_id>, severity=high)`. Cooldown: same alert key not re-DMed within 30 min.

**The non-negotiable constraint:** the stuck-detector NEVER acts on the stuck session. No `kill`, no `systemctl restart`, no PR-close. Surface only. Both the API endpoint and the healer are read-only against the running session state. The healer enforces this in code — any attempt to add an action path requires explicitly removing the `# MVP-2: surface-only, no auto-action` comment block AND adding an `OURLIBERTY_STUCK_AUTOACTION_ENABLED=false` kill-switch in the same diff.

### 5.6 Unstick action surface (read-only copy-paste recipes)

On a stuck-session card in the dashboard, the UI displays:

- The stuck PID (from `active-sessions` response).
- A copy button next to a code block: `kill <pid>` — exactly the command Larry would run.
- A second copy button for the SSH wrapper: `ssh larry@134.209.44.80 'kill <pid>'`.
- A link to the agent's worktree path so Larry can SSH and `cd` to inspect.
- A link to the session JSONL path (`~/.claude/projects/-home-larry-agent-worktrees-wt-<agent>-<task>/<session-id>.jsonl`) so Larry can replay what the agent did.

This is the bridge from "I see a problem" to "I act on it" without leaving the dashboard. Still no auto-action; just less SSH friction.

### 5.7 UI surface — Operations parent, System first child

**Routing (decision E):**

- `app/operations/page.tsx` — Operations parent route, renders tab strip + currently-selected child
- `app/operations/system/page.tsx` — System view (only child in MVP-2)

**Top-level nav update:** `Operations` becomes a top-level nav entry alongside the existing Programs grid. Clicking it lands on `/operations/system` (the default child).

**System view layout (single page, 5 sections, responsive grid):**

1. **Active Sessions** (top, most prominent) — one card per running agent. Empty state: "No active sessions" with subtle styling. Each card shows agent badge, task_id, task_type, model, duration (live-updating), cumulative cost, stuck indicator (green/yellow/red), and the copy-paste unstick recipes (only when yellow or red).

2. **Escalations + Alerts** (right column on desktop, below Sessions on mobile — high signal, deserves prominence) — filtered view of `chain_events` where `event_type IN ('escalation', 'larry_alert', 'sentinel_alert')`. Pinned-at-top: any entries where `payload.needs_response = true` (unactioned Pulse findings). Each row: timestamp, source badge (Pulse / Beacon / Sentinel), severity (yellow/red), headline, "Mark as read" button (writes a Supabase update flipping a `read` boolean per-event, scoped to dashboard sessions). Auto-refresh: poll every 10s. This is the "what should I look at next" panel.

3. **System Health** (compact card, right column below Escalations) — cgroup memory.current as a number + bar showing distance to MemoryHigh (3G) and MemoryMax (8G). memory_events_max counter. Worktree count + total size. Last-good-update timestamp with red banner if stale.

4. **Chain Event Feed** (left column on desktop, full-width below on mobile) — scrollable list of last 50 events from `chain_events` (newest first), with timestamps in Larry's local TZ (MDT). EXCLUDES the escalation/larry_alert/sentinel_alert types (those go in panel 2 to avoid duplication). Each event row: timestamp, agent badge, event_type icon, task_id (clickable, deep-links to Task detail in PM view if Task exists), short summary. Auto-refresh: poll every 10s.

5. **PR Pipeline** (full-width, bottom) — table of open PRs across both repos. Columns: PR number (clickable to GitHub), title, repo, head branch, chain_state, age. Auto-refresh: poll every 30s (longer cadence — `gh` rate-limit-aware).

**Refresh + caching cadence:**

- Active Sessions: 5s poll (driven by droplet API's 5s cache).
- Escalations + Alerts: 10s poll (Supabase query).
- System Health: 5s poll.
- Chain Event Feed: 10s poll (Supabase query).
- PR Pipeline: 30s poll (Next.js route + 30s in-process cache on the `gh pr list` call).

These cadences chosen to balance freshness against droplet/API/`gh` request volume. Production tuning expected.

### 5.8 Graceful degradation defaults

**Supabase reachable, droplet API unreachable:**
- Active Sessions + System Health sections show last-good data with a red "Live data stale — droplet API unreachable" banner and the "last good update" timestamp.
- Chain Event Feed + PR Pipeline continue to work (they don't depend on droplet API).

**Supabase unreachable, droplet API reachable:**
- Chain Event Feed shows red "Event history unavailable" banner.
- PR Pipeline shows partial data (GitHub-side only, no chain_state cross-reference).
- Active Sessions + System Health unaffected.

**Both unreachable:**
- Page shows global error banner: "System tab data unavailable; check `dashboard-api.service` and Supabase status."
- No skeleton spinners-forever; explicit error state.

**Ingestion daemon crashed (Supabase reachable but stale):**
- Chain Event Feed banner: "Event ingestion stale (last event Xm ago)." Threshold for stale: 5 min since most-recent event.
- Healer DMs Larry per § 5.2 heartbeat logic.

**GitHub API rate-limited:**
- PR Pipeline shows partial data with banner: "GitHub rate limit hit; data is Xm old. Retries automatically."
- Cached data continues to render; no broken layout.

### 5.9 Threshold configurability

The config file at `config/system_tab_thresholds.json` is read on every healer tick AND on every API request (cheap — small JSON file). No daemon restart required to apply tweaks. Future PRs may add a UI editor; MVP-2 is text-file edit + git commit (the file ships in the repo).

The file's `_meta.tweak_via` field documents the edit-then-reload pattern for future readers.

### 5.10 Self-optimizing thresholds via Pulse Check III (every 14 days)

Manual threshold tuning eventually drifts away from reality. Pulse already runs an autonomous `/cycle` every 4h and a weekly Check I cost audit — she's the right agent to also handle threshold review. New "Check III" added to her cadence:

**When it runs:**
- Every 14 days (lighter cadence than weekly Check I; thresholds shouldn't churn faster than this)
- Anchored to Sunday cycles so it doesn't compete with Check I on the same run

**What it computes (per `(agent, task_type)`):**
- Median, p90, p99 duration from `chain_events` in the last 30 days
- Sample size — skip the bucket if <10 data points (insufficient signal)
- Proposed `warn` = p90, proposed `alert` = p99 (with floor adjustments to keep alert ≥ warn × 1.25)

**What it writes:**
- `~/agents/blackboard/pulse-threshold-proposals.json` — current vs proposed values per bucket, sample sizes, one-line rationale, `applied: false` flag
- A `larry_alerts.append_alert(source='pulse', subject='threshold-proposal-<date>', severity='blue')` entry → DM to Larry with the diff

**How Larry approves:**
- Telegram to Beacon: `approve threshold-update-<date>` (or `reject threshold-update-<date>` with optional reason)
- Beacon edits `config/system_tab_thresholds.json` via the Claude-as-Forge pattern (small doc/config edit, opens a one-line PR, Mirror reviews + auto-merges)
- On merge, Beacon flips `applied: true` in `pulse-threshold-proposals.json` for that proposal

**Guardrails:**
- **No auto-apply.** Pulse proposes, Larry approves. Same posture as the stuck-detector itself.
- **Bounded delta.** Changes >50% from current are flagged as `high-attention: regime-change-suspected` in the DM. Probably worth understanding the underlying cause before tightening or loosening that much.
- **No-change OK.** "No proposed changes this cycle" is a valid Check III output if everything's within ±10% of current.
- **Rollback signal.** If a tightened threshold produces >3 false-positive alerts within 7 days of applying, the next Check III run automatically proposes un-tightening with `rollback: true` in the rationale.

**Why this fits Pulse's existing pattern:**

Pulse's `pulse-high-repeat-heuristic-tighten-001` (PR #101 today) was Pulse tuning her OWN observation heuristics based on production data. Check III is the same shape: read events, compute stats, propose self-tightening. New `scripts/pulse_check_iii.py` + a `Check III` section in `agents/pulse/CLAUDE.md` + a Beacon CLAUDE.md update for the `approve threshold-update-<date>` shortcut. Mirrors Check I's structure exactly.

---

## 6. Implementation staging (4 PRs)

Each PR is independently mergeable + reviewable. Mirror reviews each. Larry validates between PRs.

### PR-A: Supabase migration + drift-healer awareness (~½ day, ~$3 LLM)

**Files:**
- `ourliberty-dashboard/supabase/migrations/0004_chain_events.sql` — table + view + grants per § 5.1
- `scripts/heal_credential_registry_drift.py` — already aware of Supabase keys; no change unless new env vars introduced (none expected)
- `scripts/SCHEMA.md` — append `chain_events` documentation

**Acceptance:** `supabase db push` from local dev applies 0004 cleanly. `select * from chain_events limit 1` returns empty result (no rows yet). `select * from agent_sessions` returns empty. Anon and service_role both can SELECT.

**Reviewer focus (Mirror):** migration forward-only (per memory entry 31); GRANTs explicit per memory entry 30; no editing of 0001/0002/0003.

### PR-B: Ingestion daemon + audit healer + Pulse Check III + systemd units (~2 days, ~$9 LLM)

**Files:**
- `scripts/chain_event_shipper.py` (NEW) — daemon loop per § 5.2, FIVE input sources (journalctl + outbox-notifier.log + pulse-escalations.json + larry-alerts.jsonl + sentinel-alerts.jsonl)
- `scripts/tests/test_chain_event_shipper.py` (NEW) — unit tests for each parser (journal, log, jsonl, JSON-snapshot), dedup_hash, cursor resume, buffer overflow, KNOWN_EVENT_TYPES rejection
- `systemd/ourliberty-chain-event-shipper.service` (NEW) — unit definition
- `scripts/heal_chain_event_shipper_heartbeat.py` (NEW) — heartbeat-staleness healer
- `systemd/ourliberty-heal-chain-event-shipper-heartbeat.timer` (NEW) — every 5 min
- `scripts/heal_chain_event_type_audit.py` (NEW) — weekly audit healer per § 5.1 (catches unknown event_types that landed)
- `systemd/ourliberty-heal-chain-event-type-audit.timer` (NEW) — weekly Sundays
- `scripts/pulse_check_iii.py` (NEW) — Check III threshold-review analyzer per § 5.10 (queries chain_events, computes p90/p99 per bucket, writes pulse-threshold-proposals.json, queues larry_alert)
- `scripts/tests/test_pulse_check_iii.py` (NEW) — unit tests for the analyzer + sample-size floor + rollback-signal detection
- `agents/pulse/CLAUDE.md` — add "Check III — stuck-threshold review" section after Check I, on a 14-day cadence anchored to Sunday cycles
- `agents/beacon/CLAUDE.md` — teach Beacon the `approve threshold-update-<date>` and `reject threshold-update-<date>` shortcuts; both edit `config/system_tab_thresholds.json` via Claude-as-Forge pattern (small PR, Mirror review, auto-merge)
- `runbooks/chain-event-shipper.md` (NEW) — operational runbook (start/stop, cursor reset, buffer flush, adding a new event_type, accepting/rejecting Check III proposals)
- `docs/operating-manual.md` Part II — append a new numbered entry codifying the "self-optimizing operational config via Pulse Check pattern" as reusable agent-OS doctrine. Lists the 5-step loop (Pulse Check → proposal artifact → Telegram DM → Beacon approval shortcut → Claude-as-Forge PR), the discipline boundary (no auto-apply, bounded delta, sample-size floor), and the backlog of 10 candidate surfaces (cost-budget cap, memory thresholds, cleanup retention, healer retry budgets, cycle cadences, etc.). Makes the pattern discoverable to Pulse/Beacon/Forge/Mirror agents, not just to Larry's Claude Code session.
- `config/token-rotation-schedule.json` — add `chain_event_shipper.supabase_service_role_key` if new credential (likely reuse existing)

**Acceptance:** daemon ingests a 24-hour journalctl tail without dropping or duplicating events (verify via `select count(distinct dedup_hash) = count(*) from chain_events`). Buffer fills + flushes correctly under simulated Supabase outage. All five input sources surface their event types in chain_events (verified by `select distinct event_type, source from chain_events`). Heartbeat healer DMs Larry within 5 min of stopping the daemon. Audit healer DMs Larry within one week if an unknown event_type slips into the table. Check III can be smoke-tested on simulated chain_events data (fixture with 30 days of synthetic events) and produces a plausible proposals.json — Larry's `approve` shortcut then routes through Beacon → Forge → PR → merge cleanly.

**Reviewer focus (Mirror):** dedup correctness; cursor resume after restart; buffer overflow behavior; KNOWN_EVENT_TYPES discipline (rejection logged, never inserted); no PII or credentials in log payloads; pulse-escalations.json snapshot-overwrite handling (file is replaced atomically per Pulse cycle, not appended); Check III analyzer correctness (sample-size floor enforced; bounded-delta flagging; rollback-signal detection); Beacon shortcut idempotency (re-running `approve threshold-update-<date>` doesn't double-apply).

### PR-C: Droplet API endpoints (~½ day, ~$3 LLM)

**Files:**
- `scripts/dashboard_api.py` — extend with 3 endpoints per § 5.3
- `scripts/tests/test_dashboard_api_system.py` (NEW) — unit tests
- `config/system_tab_thresholds.json` (NEW) — initial values per § 5.4
- `runbooks/system-tab-thresholds.md` (NEW) — how to edit + reload

**Acceptance:** `curl http://127.0.0.1:8000/api/system/active-sessions` returns valid JSON. Test scenarios: live Mirror session running (returns it); no sessions (empty array); cgroup service stopped (503). Same for cgroup-stats and worktrees.

**Reviewer focus (Mirror):** error contracts; cache TTL behavior; `/proc/<pid>` race conditions (process dies mid-read); no shell injection via task_id or worktree names.

### PR-D: Operations tab + System view UI (~1.5 days, ~$10 LLM)

**Files:**
- `ourliberty-dashboard/app/operations/page.tsx` (NEW)
- `ourliberty-dashboard/app/operations/system/page.tsx` (NEW)
- `ourliberty-dashboard/app/api/operations/pr-pipeline/route.ts` (NEW)
- `ourliberty-dashboard/app/api/operations/mark-event-read/route.ts` (NEW) — POST handler for the "Mark as read" button on escalation/alert entries
- `ourliberty-dashboard/app/api/proxy/[...path]/route.ts` — extend allowlist for `/api/system/*`
- `ourliberty-dashboard/supabase/migrations/0005_chain_events_read_flag.sql` (NEW) — adds `read_at timestamptz NULL` to chain_events for per-event read-state tracking
- `ourliberty-dashboard/components/<ActiveSessionCard>`, `<EscalationsAlertsPanel>`, `<SystemHealthPanel>`, `<ChainEventFeed>`, `<PRPipelineTable>`, `<UnstickRecipe>`, `<StaleDataBanner>` (all NEW — 7 components)
- `ourliberty-dashboard/lib/system-queries.ts` (NEW) — Supabase + droplet API client helpers
- Layout / nav update — add Operations to top-level nav
- Vitest tests for all 7 components + the 2 new route handlers
- `ourliberty-dashboard/README.md` — add System tab section

**Acceptance:** Larry opens `dashboard.ourliberty.dev/operations/system`, sees all 5 sections render with real data. The Escalations + Alerts panel shows pinned-at-top pulse-escalations.json entries with `needs_response=true`. "Mark as read" toggles persist across page reloads. Simulates each degradation mode by stopping each upstream — banners surface correctly. Chain event feed shows the same events forensically dug out via SSH today.

**Reviewer focus (Mirror):** graceful degradation paths all reachable; copy-paste recipes are EXACT strings (no smart quotes, no autocorrect); read-flag persistence (write race with concurrent dashboard sessions); pinned-at-top sort stability for escalations; accessibility (keyboard nav on cards); responsive layout.

### Sequencing

PR-A ships first (no dependencies). PR-B + PR-C can ship in parallel (both depend only on PR-A). PR-D ships last (depends on B + C). Total wall clock estimate: ~3-4 days if sequential, ~2-3 days with parallelism on B/C.

---

## 7. Effort + cost estimate

| PR | LLM cost | Wall clock | Larry actions |
|---|---|---|---|
| PR-A migration | ~$3 | ½ day | 5 min apply `supabase db push` |
| PR-B ingestion daemon + audit healer + Pulse Check III | ~$9 | 2 days | 15 min validate ingestion on real data |
| PR-C droplet API | ~$3 | ½ day | 10 min curl + validate |
| PR-D UI (5 panels incl. Escalations + Alerts) | ~$10 | 1.5 days | 20 min spot-check + Vercel preview |
| **Total MVP-2** | **~$25** | **~4.5 days** | **~50 min** |

Comparable to E4.4a's actual spend (~$15 across the build + review). Within E4 phase budget.

---

## 8. Risks + rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| Chain_events grows unbounded over time | Default ingestion is unbounded; PR-A includes a comment noting need for retention policy (likely 90 days) before E4.4d-followup | Migration 0005 adds TTL + cron pruning |
| Ingestion daemon misclassifies event types and pollutes chain_events | Dedup_hash catches double-inserts; unit tests cover all parser branches; if production shows misclassification, daemon stop + targeted `DELETE FROM chain_events WHERE ...` + bug fix re-deploy | `systemctl stop ourliberty-chain-event-shipper`; selective DELETE; redeploy fix |
| Public read access exposes commercially-sensitive data | Decision F accepted this risk explicitly; mitigation is "V2 protects in separate project"; in MVP-2 the exposed surface is operational metadata only | Set anon GRANT to none via emergency migration; UI breaks but data is private |
| Droplet API endpoint perf degrades with high session count | Sessions are typically ≤2 active; cache TTL bounds re-reads; if Larry's workload grows, cache TTL can lengthen | Adjust cache TTL in `dashboard_api.py`; restart service |
| `gh` rate-limit hits during PR-heavy days | 30s cache + 50-PR limit per call keeps usage well under 5000/hr. Banner explains stale state if hit | Increase cache TTL to 5 min; degraded freshness, no broken behavior |
| Stuck-detector false positives DM Larry too often | 30 min cooldown per alert key; thresholds editable in config file without redeploy | Tweak thresholds; restart healer |
| Healer false-restarts something it shouldn't | Constraint § 5.5 forbids auto-action; healer code reviewer (Mirror) blocks any PR adding action paths | Constraint is enforced by code review; no rollback needed |
| Stuck-detector misses an actually-stuck session because task_type detection fails | Falls back to `_default` thresholds per agent; tighter than no-detection | Add task_type to envelope manually; bug-fix detection logic |
| Supabase free-tier limits hit (DB size, request count) | At ~100 events/day × 365 = 36,500 rows/year, ~10 MB/year, trivial; request count at ~10s polling = ~8,640 requests/day per visitor, within Supabase free tier (50,000/day) for low-traffic dashboard | Upgrade Supabase tier (Larry's call); $25/mo Pro tier |
| Operations tab nav placement confuses users | Single child in MVP-2; clicking Operations lands directly on System (no empty parent screen) | Nav restructure is one-PR easy if pattern doesn't work |

---

## 9. Acceptance criteria (for the implementation phase that follows)

Each PR has its own acceptance per § 6. MVP-2 as a whole is accepted when:

- [ ] All 4 PRs merged, all Mirror reviews passed, all Vercel deployments green.
- [ ] `dashboard.ourliberty.dev/operations/system` renders the 4 sections with real data.
- [ ] A simulated stuck session (manually `sleep 9999` injected as a Forge subprocess) surfaces in Active Sessions with red indicator within 25 min of injection.
- [ ] Killing the simulated stuck session via copy-paste recipe removes it from Active Sessions within one poll cycle.
- [ ] All 4 graceful-degradation modes (Supabase down, droplet API down, both down, ingestion daemon stale) display the expected banners when triggered.
- [ ] Larry uses the System tab to answer "what's going on right now" without SSHing for a full work day.
- [ ] Operating manual entry appended to `docs/operating-manual.md` Part II documenting the incident-driven origin + the four PRs.

---

## 10. Source notes (where this design came from)

- 2026-05-25 Mirror hang on PR #101 (self-matching `pgrep -f` poll loop, 71 min wall, marker emitted invisibly) + Pulse page-cache investigation + manual PR #101 merge after session JSONL forensics. All documented in `feedback_agent_shell_discipline_poll_loops_and_marker_lifecycle.md` + `project_phase_e4_in_progress.md`.
- Larry's "the live system dashboard seems to be useless" framing 2026-05-25 chat; my 5-view enumeration (Active Sessions / Stuck Detector / Chain Event Feed / PR Pipeline / Escalations) of which MVP-2 takes 4.
- Beacon spec-round chat 2026-05-25 14:29 → 15:12 MDT covering decisions A–F with rationale.
- Existing patterns reused: dashboard-api.service shape (E3); Supabase migration discipline (0002+0003 hotfix lessons from memory entries 30+31); healer + heartbeat pattern (existing healers); copy-paste unstick recipe pattern ([feedback_pbpaste_ssh_credential_dance] + [feedback_systemctl_set_property_live_bump]).
- Decision F's "public for V1, protect in separate project later" matches the E3 dashboard precedent.
