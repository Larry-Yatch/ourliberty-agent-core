# Ledger — Tools, Inputs, Outputs

## Where I run

- **Host:** `ourliberty-agents-01.ourliberty.dev`
- **Working directory for chat:** `~/agent-core/agents/ledger/`
- **Working directory during weekly run:** invoked via `scripts/run_ledger.sh` (no specific cwd; the Python module uses absolute paths)
- **Memory:** `agents/ledger/MEMORY.md` (in repo; small, calibration-shaped — no separate `~/agents/memory/ledger/` directory in v1)
- **Runtime model (interactive chat):** Opus 4.7. Weekly cron run is pure-Python, no LLM in the loop.

## Repos I touch

| Repo | Authority |
|---|---|
| `Larry-Yatch/ourliberty-agent-core` | Read; append to `runbooks/ledger-journal.md`; commit + push via `scripts/run_ledger.sh` |
| All other repos | Forbidden — out of scope |

## CLI tools (interactive chat only — weekly run is pure-Python)

- `jq`, `rg`, `find` — for ad-hoc queries on `costs.jsonl` and dispatch archives
- `python3` — for invoking `scripts/ledger_weekly.py` ad-hoc
- `git` — for journal commits (handled by `run_ledger.sh` in the weekly path)

## Inputs (canonical sources, read every weekly run)

| File / glob | Role |
|---|---|
| `~/agents/blackboard/costs.jsonl` | The cost source. One JSON object per line. Schema: `{ts, agent, task_id, model, cost_usd, input_tokens, output_tokens, cache_read, cache_creation, duration_sec, source[, attempts]}`. `source ∈ {inbox-watcher, run_cycle.sh}`. **No `task_type` field — joined from outbox archives.** |
| `~/agents/outboxes/<agent>/.archive/<task_id>.json` | Per-dispatch outbox archive. Carries `task_type`, `cost_usd`, `task_id`, `agent`, `source`, `phase`, plus the dispatch envelope fields. Used to attribute `task_type` to each `costs.jsonl` row. |
| `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json` (prior week) | Prior-week sidecar — used to compute week-over-week delta and to build the rolling-4-week baseline once enough history accumulates. |
| `~/agents/blackboard/EMERGENCY_HALT` (flag file; presence-only) | If present, skip the run + journal-note + exit 0. Same convention as `inbox_watcher.py`. |

## Outputs (written every weekly run — atomic via temp-file + rename)

| File | Role |
|---|---|
| `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.md` | Human-readable weekly report. Five sections: total, by-agent, by-task_type, anomalies, top-5. |
| `~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json` | Machine-readable sidecar. Schema documented in `runbooks/ledger-prompt.md` § "JSON sidecar schema" — matches spec § 7 byte-for-byte for field names + types. |
| `~/agents/blackboard/ledger/ledger-ready-YYYY-MM-DD` | Sentinel file. Atomically touched after both `.md` and `.json` are fsync'd. Pulse Check I polls for this before reading. |
| `runbooks/ledger-journal.md` | Append-only journal. One entry per weekly run. Committed + pushed by `scripts/run_ledger.sh`. |
| `~/agents/blackboard/larry-alerts.jsonl` | One row appended via `larry_alerts.append_alert(source='ledger', severity='warning', subject='weekly-YYYY-MM-DD', message=<headline>)`. The beacon-bot's 5-min alert sweep delivers it to Telegram. |

## Outputs I do NOT write

- I do not touch `~/agents/blackboard/costs.jsonl` (read-only — the outbox watcher owns it).
- I do not touch `~/agents/outboxes/<agent>/.archive/` (read-only — the inbox watcher owns it).
- I do not touch `~/credentials/` or `~/agents/memory/`.
- I do not open issues, PRs, or dispatch tasks to other agents in v1.

## Weekly run anatomy

The systemd timer `ourliberty-ledger.timer` fires `ourliberty-ledger.service`, which runs `scripts/run_ledger.sh`. The shell wrapper:

1. Acquires a flight-time lock at `~/agents/state/.ledger.lock` (30-min stale threshold; same convention as `run_cycle.sh`).
2. Checks `~/agents/blackboard/EMERGENCY_HALT` — if present, journal-note + exit 0.
3. Invokes `python3 scripts/ledger_weekly.py --week-ending YYYY-MM-DD` (auto-computed from current Monday).
4. The Python module:
   - Reads `costs.jsonl` (last 7 days based on `ts`).
   - For each row, looks up `task_type` from the matching outbox archive (best-effort; rows without a matching archive get `task_type = "unknown"`).
   - Computes the five report sections + JSON sidecar.
   - Computes σ baselines from prior 4 weeks of sidecars if available; else suspends σ flagging with a ramp-up notice.
   - Computes week-over-week delta from prior week's sidecar if present.
   - Writes `.md` + `.json` atomically (temp + rename).
   - Touches sentinel.
   - Appends one line to the journal.
   - Queues the DM via `larry_alerts.append_alert`.
   - Exits 0 on success.
5. The shell wrapper auto-commits + pushes the journal append.

## On-demand path (Pulse Check I `/optimize`)

When Pulse Check I receives `/optimize` and finds the current week's sidecar is missing or >24h old, she writes a refresh-request file at `~/agents/blackboard/ledger/ledger-refresh-request` that the Ledger systemd unit can re-trigger via `systemctl start ourliberty-ledger.service` (operator runs this manually for v1; full auto-wire is a future iteration).

## Concurrency

- Only one `ledger_weekly.py` instance can write the same `weekly-YYYY-MM-DD.{md,json}` at a time — guarded by the lock file. Subsequent invocations within 30 minutes silently abort (same convention as `run_cycle.sh`).
- The sentinel file is the contract with Pulse: it only exists if both outputs are durably written. Pulse never reads `.json` without first checking the sentinel.

## Failure modes

| Failure | Behavior |
|---|---|
| `costs.jsonl` missing | Journal `cost-capture unavailable; no report emitted`; exit 1. |
| `costs.jsonl` malformed (lines unparseable) | Skip bad lines; log count to journal; emit report from valid lines. |
| Outbox archive lookup fails for a row | Attribute `task_type = "unknown"`; continue. |
| Prior-week sidecar missing | Skip week-over-week delta (set both `absolute_usd` and `percent` to `null` in JSON); markdown report notes "no prior week to compare". |
| `larry_alerts.append_alert` returns False | Journal-note `DM cooldown-suppressed` or `DM write failed`; exit 0 (report is still written, sentinel is still touched). |
| `EMERGENCY_HALT` present | Journal `halt-respected`; exit 0; no outputs. |
