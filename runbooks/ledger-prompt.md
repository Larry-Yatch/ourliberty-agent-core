# /ledger — Ledger's Weekly Run Spec

**Read every run. This is the operational spec for the weekly cost report.**

The weekly run is fully deterministic — implemented in `scripts/ledger_weekly.py`, invoked by `scripts/run_ledger.sh`, which is fired by the `ourliberty-ledger.timer` systemd unit every Monday morning. **No LLM is in the loop for the weekly run.** This document describes what the Python module does, so that anyone — Larry, Pulse Check I, a future engineer — can audit the algorithm and reproduce the outputs.

---

## Mission filter

Make weekly cost legible. Five sections in the markdown report (total, by-agent, by-task_type, anomalies, top-5) + a JSON sidecar in a stable schema + a one-line Telegram DM headline + a sentinel file Pulse Check I polls before reading.

---

## When the run fires

- **Cron:** `ourliberty-ledger.timer` — `OnCalendar=Mon *-*-* 07:00:00 UTC` (00:00 MDT in summer, 01:00 MDT in winter). `Persistent=true` so a missed run fires on next boot.
- **Manual trigger:** `bash ~/agent-core/scripts/run_ledger.sh` OR `systemctl start ourliberty-ledger.service`.
- **`/optimize` on-demand path (Pulse Check I):** when Pulse finds the current week's sidecar is missing or >24h old, an operator runs `systemctl start ourliberty-ledger.service` manually for v1. Full auto-trigger is a future iteration.

---

## What the run reads

| Source | Used for |
|---|---|
| `~/agents/blackboard/costs.jsonl` | All cost rows. Schema: `{ts, agent, task_id, model, cost_usd, input_tokens, output_tokens, cache_read, cache_creation, duration_sec, source[, attempts]}`. Filtered to `[week_ending - 7 days, week_ending)`. |
| `~/agents/outboxes/<agent>/.archive/<task_id>.json` | `task_type` lookup per row. Rows with no matching archive get `task_type = "unknown"`. |
| `~/agents/blackboard/ledger/weekly-<prior_date>.json` (prior weeks) | Prior 4 sidecars used to compute σ baselines; the most-recent prior used for week-over-week delta. |
| `~/agents/blackboard/EMERGENCY_HALT` | Presence-only flag. If present, skip the run + journal-note + exit 0. |

---

## Week-window convention

`week_ending` is always a **Monday at 00:00 UTC** (the same Monday on which the cron fires). The covered window is `[week_ending - 7 days, week_ending)` — i.e., the seven days strictly before `week_ending`. The DM names the week as "Week of YYYY-MM-DD" using `week_ending` as the date.

Examples:
- Cron fires Monday 2026-05-18 07:00 UTC → `week_ending = 2026-05-18 00:00 UTC` → window = `[2026-05-11 00:00 UTC, 2026-05-18 00:00 UTC)`.
- Manual run with `--week-ending 2026-05-25` → window = `[2026-05-18 00:00 UTC, 2026-05-25 00:00 UTC)`.

---

## Computation steps

The Python module executes these in order:

1. **Halt check.** If `~/agents/blackboard/EMERGENCY_HALT` exists, print a notice and exit 0. (`run_ledger.sh` also checks; the Python check is for manual invocation.)
2. **Determine `week_ending`.** From `--week-ending` argv, or default to the current Monday at 00:00 UTC.
3. **Load rows.** Read `costs.jsonl`. Filter to the window. Tolerant of both `+00:00` and naive ISO `ts` (naive treated as UTC). Skip lines that are unparseable or missing `cost_usd`; count the skip total.
4. **Attribute `task_type`.** For each row, attempt to open `~/agents/outboxes/<row.agent>/.archive/<row.task_id>.json` and copy `task_type` from it. On miss / parse-fail, leave as `"unknown"`.
5. **Compute totals.** `total_usd = sum(row.cost_usd)`. `by_agent` and `by_task_type` are `{<key>: {usd, task_count}}` aggregations.
6. **Load prior sidecars.** Try to load up to `BASELINE_WEEKS = 4` prior sidecars. If `len(prior_sidecars) >= RAMP_UP_WEEKS = 4`, σ-flagging is active for the week; else it's suspended.
7. **Compute σ baselines** (only when active). For each `task_type` present in prior sidecars, baseline = `(mean, stdev)` of `(usd/task_count)` across the prior weeks. `stdev=0.0` when only one prior week of data exists for that type — σ-flagging is skipped for that type.
8. **Compute anomalies.**
   - When σ-flagging is suspended: emit one synthetic informational entry — `{task_id: "_ramp_up_notice", context: "baseline ramp-up: N prior weekly window(s) observed; σ-flagging suspended until ≥4 weeks of data"}`. This is NOT a real anomaly; the DM heartbeat shape still fires.
   - When σ-flagging is active: for each row, `sigma_above = (cost - mean) / stdev`. Emit a row if `sigma_above ≥ SIGMA_THRESHOLD = 2.0`. Sort descending by `sigma_above`.
9. **Compute retry overhead.** v1 heuristic: a row counts as a retry if `task_id` starts with `notify-` OR contains `-revision-` OR contains `-cycle-fix-`. Sum their `cost_usd`; compute `percent_of_total`. Tune after week 2 once we have real data.
10. **Compute top-5.** Sort all rows by `cost_usd` desc, take first 5, emit `{task_id, agent, cost_usd}`.
11. **Compute week-over-week delta.** If a prior-week sidecar exists, emit `{absolute_usd: total - prior_total, percent: (absolute / prior_total) * 100}`. Else emit `null`.
12. **Write outputs atomically.** Temp-file + `os.fsync` + `os.replace` for both `weekly-YYYY-MM-DD.md` and `weekly-YYYY-MM-DD.json`.
13. **Touch sentinel.** `~/agents/blackboard/ledger/ledger-ready-YYYY-MM-DD`. Pulse Check I polls for this before reading.
14. **Queue DM.** `larry_alerts.append_alert(source='ledger', severity='warning', subject='weekly-YYYY-MM-DD', message=<headline>)`. The cooldown bucket is keyed by `(source, subject)`, so re-runs for the same week collapse — no duplicate DMs.
15. **Append journal.** Append to `runbooks/ledger-journal.md` with iteration number, week-ending, health (`✅ Nominal` / `🟡 Anomalies` / `🟡 Drift` / `🔴 Failed`), total, delta vs prior, anomaly count, skip count, sentinel path, DM result.

---

## DM headline grammar

Two shapes:

**Heartbeat (no real anomaly, drift ≤ DRIFT_PERCENT_THRESHOLD = 20%):**
```
📒 Week of YYYY-MM-DD: $N.NN total, all within baseline. Report: ~/agents/blackboard/ledger/weekly-YYYY-MM-DD.md
```

**Anomaly-bearing (at least one σ-flag OR drift > 20%):**
```
📒 Week of YYYY-MM-DD: $N.NN total, ±M.M% vs prior week. Top anomaly: `<task_id>` at $X.XX. Report: ~/agents/blackboard/ledger/weekly-YYYY-MM-DD.md
```

Drift-only (no σ anomalies but >20% WoW):
```
📒 Week of YYYY-MM-DD: $N.NN total, ±M.M% vs prior week. No σ anomalies; week-over-week drift > threshold. Report: ...
```

The `📒` emoji is the source tag the beacon-bot's `format_dm` uses for Ledger.

---

## JSON sidecar schema

This MUST match the schema in `agents/beacon/specs/ledger.md` § 7 byte-for-byte for field names + types. Schema version is `"v1"`.

```json
{
  "schema_version": "v1",
  "week_ending": "2026-05-18",
  "total_usd": 14.27,
  "delta_vs_prior_week": {
    "absolute_usd": 1.52,
    "percent": 11.9
  },
  "by_agent": {
    "beacon": { "usd": 3.21, "task_count": 7 },
    "forge":  { "usd": 8.45, "task_count": 12 },
    "mirror": { "usd": 2.10, "task_count": 11 },
    "pulse":  { "usd": 0.51, "task_count": 42 }
  },
  "by_task_type": {
    "doc-only": { "usd": 4.20, "task_count": 8 },
    "feature-development": { "usd": 7.34, "task_count": 4 },
    "code-review": { "usd": 2.73, "task_count": 5 },
    "unknown": { "usd": 0.00, "task_count": 0 }
  },
  "anomalies": [
    {
      "task_id": "build-ledger-001",
      "agent": "forge",
      "task_type": "feature-development",
      "cost_usd": 2.91,
      "baseline_usd": 0.85,
      "sigma_above": 2.4,
      "context": "task cost $2.91 vs $0.85 baseline (n=4wk)"
    }
  ],
  "retry_overhead": {
    "total_retry_cost_usd": 1.27,
    "percent_of_total": 8.9
  },
  "top_5_tasks": [
    { "task_id": "build-ledger-001", "agent": "forge", "cost_usd": 2.91 }
  ]
}
```

Notes:
- `delta_vs_prior_week` is `null` when no prior-week sidecar exists.
- `anomalies` is `[]` (empty) when σ-flagging is active and nothing flags; it's a single-element array with `task_id: "_ramp_up_notice"` during ramp-up (weeks 1–4).
- `by_task_type` contains `"unknown"` bucket whenever any row failed the outbox-archive join.
- Floats are full-precision in JSON; markdown surfaces round to 2 decimals.

---

## Sentinel contract with Pulse Check I

- Path: `~/agents/blackboard/ledger/ledger-ready-YYYY-MM-DD`.
- Written via `Path.touch()` AFTER both `.md` and `.json` are fsync'd via `atomic_write`.
- Existence means: "both report files are durably written, fully consistent with the JSON schema above, and immutable for this week."
- Pulse Check I polls this path (existence check) for up to 30 minutes on Monday before reading the JSON sidecar. If the sentinel is missing after 30 min, Pulse skips Check I with a journal note (per `pulse-check-i.md` § 7).

---

## Concurrency

- `run_ledger.sh` holds `~/agents/state/.ledger.lock` for the run duration (30-min stale threshold). Subsequent invocations within the window abort silently.
- `larry_alerts.append_alert` has its own cooldown keyed `(source, subject)`. Re-running for the same `week-YYYY-MM-DD` does not double-DM Larry.
- Sidecar writes are atomic (temp + rename), so a partial write can never be observed by Pulse.

---

## EMERGENCY_HALT discipline

If `~/agents/blackboard/EMERGENCY_HALT` exists:

1. `run_ledger.sh` logs `EMERGENCY_HALT present; aborting` and exits 0.
2. If somehow reached without the shell wrapper (e.g., manual `python3 ledger_weekly.py`), the Python `main()` performs the same check and exits 0.
3. No outputs written. No DM queued. No journal entry (the halt is logged in `~/agents/logs/ledger.log` and visible via `journalctl -u ourliberty-ledger.service`).

---

## Failure-mode summary

| Failure | Behavior | Exit |
|---|---|---|
| `costs.jsonl` missing | Journal-note "cost-capture unavailable"; no outputs | 1 |
| `costs.jsonl` malformed lines | Skip bad lines; report skip count in markdown + journal | 0 |
| Outbox archive missing for a row | Bucket as `task_type=unknown`; continue | 0 |
| Prior-week sidecar missing | `delta_vs_prior_week = null`; markdown says "no prior week" | 0 |
| `larry_alerts.append_alert` returns False | Journal-note `cooldown-suppressed`; outputs + sentinel still written | 0 |
| `EMERGENCY_HALT` present | Log + exit; no outputs | 0 |
| Python crashes after sidecar write but before sentinel | Sentinel missing → Pulse skips Check I; operator re-runs manually | 1 |

---

## Deploy / verification checklist

After landing this PR:

1. `sudo cp systemd/ourliberty-ledger.{service,timer} /etc/systemd/system/`
2. `sudo systemctl daemon-reload`
3. `sudo systemctl enable --now ourliberty-ledger.timer`
4. `systemctl list-timers ourliberty-ledger.timer` — confirm next firing is a Monday at 07:00 UTC.
5. `systemd-analyze calendar 'Mon *-*-* 07:00:00 UTC'` — confirm the calendar spec parses correctly.
6. Manual smoke: `sudo systemctl start ourliberty-ledger.service` — confirm exits 0, files land at `~/agents/blackboard/ledger/`, sentinel touched, DM queued in `~/agents/blackboard/larry-alerts.jsonl`.
7. `journalctl -u ourliberty-ledger.service -n 50` — confirm no errors.
