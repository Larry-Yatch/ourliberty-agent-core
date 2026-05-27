# Spec: E4.4e — Live System + Operations Polish v1 (Post-Approvals Cleanup)

**Status:** Draft (awaiting Larry approval — sibling polish sub-spec of E4.4e)
**Author:** Claude-as-Forge (written 2026-05-27 morning, after UX inspection session)
**Approver:** Larry (pending)
**Phase:** E4.4e polish, after Approvals tab live
**Parent specs:** [agents/beacon/specs/e4-overview.md](e4-overview.md), [agents/beacon/specs/e4-4e-approvals-tab.md](e4-4e-approvals-tab.md)
**Sibling:** [agents/beacon/specs/e4-4e-approvals-tab-polish-v1.md](e4-4e-approvals-tab-polish-v1.md) (in flight)

---

## 1. Problem statement

The Approvals tab now owns the "what needs my decision" surface. That makes two other dashboard surfaces partially redundant or stale:

**Live System tab** (`/live`):
- "in-flight" count is always 0 across all four agent cards + the global card, even when Forge is actively building a PR. Data source is reading from outbox archive rather than the active-sessions endpoint that the rest of the dashboard uses.
- Agent cards label themselves "bot active" or "no bot (inbox-watcher)" — exposes the deployment-implementation split (systemd-bot vs inbox-watcher subprocess) that Larry doesn't need. Semantic he needs is *idle / running / stuck / errored*.
- "Recent Cycle-Journal Entries" shows five timestamp-only rows ("Iteration 92 — date — (interactive)") with no content. Doesn't surface what Pulse actually found.

**Operations / System tab** (`/operations/system`):
- "Active Sessions" includes a ghost card (PID 1288519, the inbox-watcher master process, with "started: never, model: —, duration: 0s") — supervisor process leaking into the per-task display.
- "Escalations + Alerts" panel is parallel-displaying the same chain_events that Approvals now owns, with worse affordances. Redundant.
- System Health panel is buried below the long Escalations list — "is the system healthy?" should not be lower-priority than the alert history.
- PR Pipeline at very bottom shows "no PR found" when empty, taking up vertical space for nothing.

The three tabs need clearer purpose separation post-Approvals.

---

## 2. Four locked decisions

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | Tab purposes (post-Approvals) | **Approvals = action surface; Live System = heartbeat surface; Operations = diagnostic surface** | Each tab gets a distinct mode. Live System for glances ("is the chain alive?"); Operations for drill-down ("what's running / stuck / pressured?"); Approvals for action ("what needs me?"). |
| B | Active Sessions filtering | **Filter out the inbox-watcher supervisor process from "Active Sessions" by requiring a task_id from the in-flight registry; do NOT show cgroup PIDs without a registry entry** | The supervisor isn't a task. Showing it as a ghost row pollutes the diagnostic surface. The active-sessions reader already has `entry = in_flight_by_pid.get(pid)` — V1 polish requires non-null `entry` (or `entry.task_stem`) to render. |
| C | Operations tab right column | **Remove Escalations + Alerts panel; replace with a single "N pending → Approvals" link row at the top of the right column; promote System Health to top of right column** | Approvals owns escalations. Operations only needs to know an unresolved count exists; the link affordance routes the user to the right surface. |
| D | PR Pipeline visibility | **Hide entire PR Pipeline section when there are 0 open PRs; show compact section with chain-state badges when 1+ open** | Empty-state real estate is anti-pattern. The section adds value only when there's something to see. |

---

## 3. Design intent — how Larry uses each tab

| Tab | Workflow state | Time budget | What polish must support |
|---|---|---|---|
| Live System | **Glance** "is everything green?" | 3 sec | Agent dots reflect real status (not just systemd state); spend visible; needs-attention count |
| Live System | **Pulse check** "what did Pulse last find?" | 15 sec | One row with the latest cycle's *headline finding* + click → Approvals filtered to that finding |
| Live System | **Cost check** "today high or normal?" | 5 sec | Today's spend with delta vs yesterday same-time-of-day |
| Operations | **Triage stuck** "what's stuck and how do I unstick?" | 30 sec | Filtered Active Sessions (no supervisor noise); kill recipe + worktree path on each session card |
| Operations | **Pressure check** "memory / cpu OK?" | 10 sec | System Health top-right with gauges |
| Operations | **PR-in-chain status** "is PR #X still mid-Mirror?" | 15 sec | PR Pipeline visible when 1+ open with chain-state badge per PR |
| Operations | **Worktree leak check** "anything stale?" | 20 sec | Worktrees list with ages (V1 read-only; cleanup affordances deferred to v2) |

---

## 4. Success criteria

- Live System tab "in-flight" count reflects actual active sessions within 5 seconds of a Forge build starting. Forge running PR-D-polish-v1 at 3m 19s on Operations shows as "in-flight: 1, running: e4-4e-approvals-tab-polish-v1" on Live System Forge card — not "0, last activity 1h ago".
- Larry can derive each agent's true state (idle / running / stuck / errored) from the dot color + status text without needing to understand systemd-vs-inbox-watcher deployment shape.
- Operations Active Sessions shows only real task sessions (no supervisor ghost row).
- Operations right column lead is System Health, not the alert history.
- Operations PR Pipeline section disappears when 0 open PRs; appears compact with status badges when ≥1.
- Latest Pulse Cycle on Live System shows a one-sentence headline finding (not just a timestamp).

---

## 5. Detailed requirements

### 5.1 Live System tab (`app/live/page.tsx`)

**Agent cards (4):**

- Replace `"bot active"` / `"no bot (inbox-watcher)"` labels with status semantic:
  - `Idle` — agent has no in-flight task and last completed task was clean
  - `Running task X` — agent has an in-flight task (read task_id from active-sessions)
  - `Stuck` — agent's in-flight task exceeds the stuck threshold (use existing dashboard-side stuck-detection from spec § 5.5 of e4-4d)
  - `Errored` — most recent session_done has `success=false` AND no subsequent session_start
- Status dot color: green (idle), blue (running), amber (stuck), red (errored).
- `in-flight` count: read from the SAME droplet endpoint that Operations uses (`/api/proxy/api/system/active-sessions`); count sessions where `agent === <this card's agent>`.
- `last activity`: read from `chain_events` latest `session_done` for this agent.
- Today's task count: count `session_done` events from this agent with `ts >= browser-local midnight`.
- Today's spend per agent: sum `cost_usd` from session_done events for this agent today (already in chain_events.cost_usd column).

**Today's spend card:**

- Keep the current layout (total + per-agent breakdown).
- Add a delta row: "vs yesterday same time: +$23.42 (12%)" — compute by summing today's chain_events.cost_usd up to NOW vs yesterday's chain_events.cost_usd up to the same wall-clock time minus 24h.
- Delta color: red if >+20%, amber if +5..+20%, green if <+5%, gray if comparable.

**In-flight card (global):**

- Replace the stuck-zero with actual count from active-sessions endpoint.
- When count >0: show list of (agent, task_id, age) — one row per session, clickable to Operations / Active Sessions card.
- When count =0: show "no tasks in flight" (current text) with subtle styling.

**NEW: Needs your attention card:**

- Position: right of Today's Spend (or below if narrow viewport).
- Content: integer count of unresolved Pending items from the Approvals query (`read_at IS NULL` and `event_type IN (approval_request, clarify_request, larry_alert, sentinel_alert, escalation)`).
- Click: navigates to `/approvals`.
- When count =0: subtle "all clear" with green check.

**NEW: Latest Pulse Cycle card (replaces "Recent Cycle-Journal Entries"):**

- Position: where "Recent Cycle-Journal Entries" currently sits.
- Content: ONE primary row showing the latest Pulse iteration's:
  - Iteration number + timestamp (e.g. "Iteration 92 — 5:00 UTC")
  - Headline finding: read from chain_events where `agent='pulse' AND event_type IN ('escalation', 'larry_alert')` filtered to events with `payload.source_finding = 'pulse'` AND `ts >= cycle_start`, ordered by ts ASC, take first 1 — its `payload.headline` (escalation shape) or `payload.subject` (alert shape).
  - If no findings: "No findings this cycle"
- Below the primary row: collapsed accordion "Previous 4 cycles" with the same shape (timestamp + finding headline). Default collapsed.

### 5.2 Operations / System tab (`app/operations/system/page.tsx`)

**Active Sessions panel:**

- Filter: only render session cards where the droplet's active-sessions response includes a non-null `agent` AND `task_id` (i.e., a registry-matched task). Drop cards for cgroup-only PIDs (the inbox-watcher supervisor + transient subprocesses).
- The droplet endpoint itself is unchanged; the filter is dashboard-side in `app/operations/system/page.tsx` where `sessions` is derived.
- Empty state: "No active sessions" remains.

**Stuck envelopes panel (existing):**

- Unchanged.

**Right column (re-ordered):**

1. **Approvals link row** (NEW, top): "X items pending → Approvals" pill linking to `/approvals`. Shows count from same query as the Live System "needs attention" card. Hide entirely when count =0.
2. **System Health panel** (PROMOTED to top, was buried below Escalations).
3. **NEW: Worktrees panel** (V1 read-only). Per-worktree row: `wt-<agent>-<task>` name, age (using `mtime` from the droplet `/api/system/worktrees` response), size if cheap. Sort by age DESC. Truncate to top 10 with "show all (N)" expand.
4. ~~Escalations + Alerts panel~~ — **REMOVE.** Approvals owns this surface now.

**Chain Event Feed (left column, full):**

- Improvement set deferred to v2 (collapse session_start/done pairs, humanize slugs, filter chips). V1 keeps it as-is.

**PR Pipeline section (full-width, bottom):**

- Hide entire section when `prs.length === 0`.
- When 1+ open PRs: render compact rows (one PR per row). Per-row columns: repo, PR # + title (truncated), head branch, chain-state badge (forge-building / pr-opened / mirror-reviewing / mirror-passed-pending-merge / in-revision-loop / escalated / merged), age.
- Chain state derived from chain_events for the PR's task_id (already implemented by the existing PR pipeline Route Handler — confirm it surfaces all states).

### 5.3 New / extended queries

In `lib/system-queries.ts` and / or `lib/approval-queries.ts`:

- `getActiveSessionCountByAgent(): Promise<Record<string, number>>` — call active-sessions, group by agent, return counts. Used by Live System agent cards.
- `getTodaysSpendByAgent(): Promise<Record<string, number>>` — sum chain_events.cost_usd today grouped by agent.
- `getSpendDeltaVsYesterday(): Promise<{ today_total: number, yesterday_at_same_time: number, delta_usd: number, delta_pct: number }>` — wall-clock comparison helper.
- `getPendingAttentionCount(): Promise<number>` — count chain_events where `read_at IS NULL AND event_type IN (...)`. Used by Live System "needs attention" card AND Operations "approvals link row".
- `getLatestPulseCycleFinding(): Promise<{ iteration: string, ts: string, headline: string | null, source_finding: string | null } | null>` — query latest Pulse cycle's first finding event.
- `getRecentPulseCycles(limit: number = 4): Promise<Array<...>>` — for the collapsed accordion.

### 5.4 Deletion / cleanup

- Delete `components/EscalationsAlertsPanel.tsx` import + usage from `app/operations/system/page.tsx`. Component file itself can stay in the repo (some tests reference it; deleting tests is out of scope) but is no longer mounted.
- Remove obsolete cycle-journal entries query from `app/live/page.tsx` (was reading from `cycle-journal/recent` droplet endpoint). The endpoint itself stays; just the dashboard consumer is removed.

---

## 6. Out of scope (explicit — deferred to v2)

- Worktree cleanup affordances (write path to droplet `/api/system/worktrees/cleanup` — needs auth + POST endpoint design)
- Memory time-series chart (currently System Health shows point-in-time gauge; time-series chart would need a new chain_events emitter for cgroup samples)
- Chain Event Feed humanization (collapse session pairs, slug translation, filter chips)
- Mobile responsive layout
- Click-through from Live System agent cards to Operations filtered to that agent
- Per-PR chain-state timeline view (drill into PR Pipeline row)

---

## 7. Files in scope (dashboard repo)

- `app/live/page.tsx` — major rework (agent cards new semantics, spend delta, needs-attention, Pulse cycle replacement)
- `app/live/components/AgentCard.tsx` — NEW or rework (status semantic, dot color matrix)
- `app/live/components/SpendDeltaRow.tsx` — NEW
- `app/live/components/NeedsAttentionCard.tsx` — NEW
- `app/live/components/LatestPulseCycleCard.tsx` — NEW
- `app/operations/system/page.tsx` — filter Active Sessions, remove EscalationsAlertsPanel mount, re-order right column, hide-when-empty for PR Pipeline
- `app/operations/system/components/ApprovalsLinkRow.tsx` — NEW
- `app/operations/system/components/WorktreesPanel.tsx` — NEW
- `lib/system-queries.ts` — extend with the 6 query helpers in § 5.3
- No droplet changes; no Supabase migrations; no new env vars.

---

## 8. Test plan

Vitest:

- `getActiveSessionCountByAgent`: fixture active-sessions response with mix of agents; assert correct counts.
- `getSpendDeltaVsYesterday`: fixture two days of chain_events with known costs; assert delta computation matches both currency + percentage.
- `getPendingAttentionCount`: fixture chain_events with mix of read/unread + event types; assert filter matches spec.
- `getLatestPulseCycleFinding`: fixture chain_events with multiple Pulse cycles; assert latest is returned with first finding (not most recent finding).
- AgentCard status derivation: fixture inputs for each (idle / running / stuck / errored); assert correct dot color + label.
- Active Sessions filter: fixture droplet response with supervisor PID + real task PIDs; assert supervisor is dropped.
- PR Pipeline empty: assert section unmounted when `prs.length === 0`.
- Existing list / action / proxy tests must continue to pass (regression guard).

Manual acceptance per § 4 success criteria.

---

## 9. Cost estimate

Best-guess Forge + Mirror chain spend: $7-11. Smaller than today's polish PR because no markdown rendering / virtualization, but substantial query surface added + two pages reworked. Mirror revision rounds: 0-1.

---

## End of spec
