# Spec: Ledger — CFO agent

**Status:** Approved
**Author:** Beacon (drafted 2026-05-15)
**Approver:** Larry (2026-05-15, in conversation)

## 1. Problem statement

Cost data for the agent system is captured in scattered raw artifacts (`~/agents/blackboard/cost-capture.jsonl`, outbox-notifier logs, dispatch archives) but nothing surfaces it. Larry has no weekly visibility into total spend, per-agent cost, anomalies, or trends. D3.5 5d shipped a per-task `cost_per_task_usd` budget gate, but that is enforcement, not reporting. No agent currently owns cost intelligence.

## 2. Success criteria

- Larry receives a weekly Telegram DM every Monday morning with headline cost numbers and a pointer to the full report.
- A structured weekly report exists at a known path on the persistent mount, readable by Pulse Check I and any future tool.
- Anomalies (>2σ above task_type baseline; week-over-week drift >20%) surface in the report without manual querying.
- `/optimize` on Telegram re-runs Ledger when his last report is >24h old.
- Empty weeks (nothing notable) still produce a heartbeat DM ("Week of X: $N total, all within baseline").

## 3. Users / consumers

- **Primary:** Larry. Reads the DM headline; reads the full report when something flags.
- **Secondary:** Pulse Check I. Reads the JSON sidecar Monday morning to layer engineering interpretation on top.
- **Tertiary:** Future agents/tools needing cost data (Beacon for spec cost estimates, etc.).

Downstream consumer category: Larry-internal infrastructure. Not customer-facing.

## 4. Scope (what's in)

- Weekly run, Monday morning, unattended (systemd timer).
- Reads the last 7 days of: `~/agents/blackboard/cost-capture.jsonl`, dispatch archives in `~/agents/outboxes/*/.archive/`, `runbooks/cycle-actions.jsonl`, outbox-notifier cost-budget logs.
- Computes:
  - Total weekly spend (USD).
  - Spend by agent (Beacon, Forge, Mirror, Pulse — and Ledger himself once running).
  - Spend by task_type (`doc-only`, `feature-development`, `code-review`, etc. — drawn from dispatch envelope `task_type` fields).
  - Anomaly tags: tasks costing >2σ above their task_type rolling-average baseline.
  - Week-over-week deltas (this week vs prior week, percentage and absolute).
  - Retry/clarification overhead: percentage of total cost paid on retries (marker-error retry cascade, Forge clarification rounds, Mirror re-reviews) vs first-try completion.
  - Top 5 most expensive tasks of the week with task_id + cost + agent.
- Emits two outputs:
  - Markdown report at `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.md` (human-readable).
  - JSON sidecar at `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json` (machine-readable; consumed by Pulse Check I).
- Sends a Telegram DM to Larry with the headline + report path pointer.
- Manual trigger: when Pulse runs `/optimize` and Ledger's last report is >24h old, Ledger runs fresh.

## 5. Out of scope (what's deliberately not in)

- Real-time cost monitoring or alerting (Pulse handles latency-style health checks).
- Per-task cost budget enforcement (already shipped in D3.5 5d, `scripts/outbox_notifier.py`).
- Billing reconciliation or payment processing (this is internal cost reporting, not financial accounting).
- Cost prediction or budgeting forecasts (v1 is descriptive, not predictive).
- Per-user attribution (Larry is the only user).
- Token-level breakdown (v1 reports dollars, not raw token counts — token cost varies by model).

## 6. Acceptance criteria

- [ ] When the Monday systemd timer fires, Ledger runs unattended, completes, and writes both files within 5 minutes.
- [ ] The Telegram DM lands in Larry's chat thread within 2 minutes of completion.
- [ ] The JSON sidecar conforms to the schema documented in § 7.
- [ ] When `/optimize` fires and Ledger's report is >24h old, Pulse triggers Ledger's fresh-run before consuming.
- [ ] When a week has zero anomalies and no week-over-week drift >20%, the DM is the heartbeat shape ("Week of X: $N total, all within baseline").
- [ ] When a week has anomalies, the DM headline names them ("Week of X: $N total, +N% from last week, top anomaly: <task_id> at $Y").
- [ ] The full markdown report includes the five computed sections (total, by-agent, by-task_type, anomalies, top-5).
- [ ] An EMERGENCY_HALT trip pauses Ledger like the other agents.

## 7. Architecture sketch

Ledger runs as a cron-driven agent, analogous to Pulse's existing 4h `/cycle`. Components:

- **Identity files** (mirror existing agents): `agents/ledger/{IDENTITY,SOUL,USER,TOOLS,CLAUDE,MEMORY}.md` — establish persona, mandate, tools, Larry's preferences as principal.
- **Prompt file:** `runbooks/ledger-prompt.md` — analog to `cycle-prompt.md`, instructing Ledger what to read, compute, and emit each run. Includes the JSON sidecar schema doc.
- **Cron:** systemd `.service` + `.timer` pair following the existing healer pattern (`Nice=10`, lean hardening). Weekly trigger Monday 00:00 MDT.
- **Telegram bot adapter:** routes DM through Pulse's existing `larry_alerts.append_notification` pipe in v1 (no dedicated Ledger bot). Revisit if v1 reveals it's needed.
- **Inputs:**
  - `~/agents/blackboard/cost-capture.jsonl` (rolling cost log, populated by outbox notifier)
  - `~/agents/outboxes/*/.archive/*.json` (per-dispatch cost field)
  - `~/agents/logs/outbox-notifier.log` (AUTO_MERGE / cost-budget audit lines)
  - `runbooks/cycle-actions.jsonl` (Pulse's action log; may contain cost rows)
- **Outputs:**
  - `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.md`
  - `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json`
  - Telegram DM (headline + path pointer)

**JSON sidecar schema (v1):**

```json
{
  "schema_version": "v1",
  "week_ending": "2026-05-15",
  "total_usd": 12.34,
  "delta_vs_prior_week": {
    "absolute_usd": 3.21,
    "percent": 35.2
  },
  "by_agent": {
    "beacon": { "usd": 1.23, "task_count": 5 },
    "forge": { "usd": 8.45, "task_count": 12 },
    "mirror": { "usd": 2.10, "task_count": 11 },
    "pulse": { "usd": 0.56, "task_count": 42 }
  },
  "by_task_type": {
    "doc-only": { "usd": 4.20, "task_count": 8 },
    "feature-development": { "usd": 7.34, "task_count": 4 },
    "code-review": { "usd": 0.80, "task_count": 5 }
  },
  "anomalies": [
    {
      "task_id": "auto-merge-gap-pr16-001",
      "agent": "forge",
      "task_type": "bug-investigation",
      "cost_usd": 2.91,
      "baseline_usd": 0.85,
      "sigma_above": 2.4,
      "context": "Phase 2 surfaced discipline-gate notify-prefix bug; cost includes recovery"
    }
  ],
  "retry_overhead": {
    "total_retry_cost_usd": 1.27,
    "percent_of_total": 10.3
  },
  "top_5_tasks": [
    { "task_id": "...", "agent": "forge", "cost_usd": 2.91 }
  ]
}
```

What is TBD in v1 build: exact baseline computation (rolling average over N weeks; first 2-4 weeks are ramp-up where anomaly detection may be quiet); exact "drift threshold" (start at 20%, tune after 2 weeks); USD rounding (2 decimals for human display; full precision in JSON).

## 8. Open questions / risks

- **Cost-capture JSONL schema verification.** v1 assumes outbox-notifier's cost-capture pipeline writes a clean per-task JSONL; needs verification against actual file contents. To resolve: Forge confirms during preflight by reading a sample of the live file.
- **Baseline ramp-up.** First 2-4 weeks will not have meaningful task_type baselines (need history to compute σ). Acceptable for v1; flag in the first reports that "anomaly detection ramping up — N tasks in baseline, may produce false negatives." To resolve: revisit after week 4.
- **Telegram bot decision.** Routing through Pulse's adapter (D3.5 pattern) vs spinning up a dedicated Ledger bot. v1 default: route through existing larry-alerts pipe. To resolve: in build if Forge sees a cleaner path.
- **Cost rounding.** USD numbers round to 2 decimals for human display; the JSON sidecar preserves full precision. To resolve: lock in v1 implementation.
- **Concurrent run with Pulse.** Ledger Monday 00:00 + Pulse Monday "after Ledger" — define "after." Simplest: a sentinel file at `~/agents/blackboard/ledger/ledger-ready-YYYY-MM-DD` that Ledger writes when done; Pulse polls for it before running her Check I. To resolve: lock in v1 build.

## 9. Handoff package requirements

- `agents/ledger/CLAUDE.md` analog to existing agents.
- `runbooks/ledger-prompt.md` with computation steps + JSON schema doc.
- `runbooks/ledger-journal.md` (analog to `cycle-journal.md`) — append-only run log.
- systemd `.service` + `.timer` files in `systemd/` following existing patterns.
- Tests: at minimum, a unit test that computes a weekly report from synthetic input and validates the JSON sidecar schema.
- A runbook section in `docs/operating-manual.md` Part I describing how Ledger works, his cron, and recovery from a missed run.
- Deploy notes: how to enable the timer, where to find the JSON sidecar, how to manually trigger a fresh run.

## 10. References

- Pulse's analogous infrastructure: `runbooks/cycle-prompt.md`, `runbooks/cycle-journal.md`, `scripts/concurrency_guard.py`, identity files in `agents/pulse/`.
- D3.5 5d cost-budget gate: `scripts/outbox_notifier.py` (the `cost_per_task_usd` enforcement primitive Ledger builds on, not replace).
- Pulse Check I spec (companion): `agents/beacon/specs/pulse-check-i.md`.
- Roadmap entry: `docs/roadmap.md`.
