#!/usr/bin/env python3
"""Ledger weekly cost report — deterministic compute, no LLM in the loop.

Reads `~/agents/blackboard/costs.jsonl` for the last 7 days, joins each row
against the matching outbox archive at
`~/agents/outboxes/<agent>/.archive/<task_id>.json` to attribute `task_type`,
computes the five report sections, writes `weekly-YYYY-MM-DD.md` +
`weekly-YYYY-MM-DD.json` atomically, touches the sentinel, appends a journal
entry, and queues a DM to Larry via `larry_alerts.append_alert`.

Invoked by `scripts/run_ledger.sh` (which is invoked by
`ourliberty-ledger.service` on the Monday timer). Manual invocation:

    python3 scripts/ledger_weekly.py --week-ending 2026-05-18

The week covered is `[week_ending - 7 days, week_ending)` — i.e., the seven
days strictly before `week_ending` (Monday). `week_ending` itself is the
Monday on which the report runs and names the week in the DM ("Week of X").

JSON sidecar schema: see `runbooks/ledger-prompt.md` § "JSON sidecar schema"
(matches spec `agents/beacon/specs/ledger.md` § 7 byte-for-byte).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from task_type_inference import infer_task_type
import safe_write_inbox  # sanitize_component: match on-disk outbox archive names

# --- constants ---

SCHEMA_VERSION = "v1"
SIGMA_THRESHOLD = 2.0
DRIFT_PERCENT_THRESHOLD = 20.0
BASELINE_WEEKS = 4
RAMP_UP_WEEKS = 4  # σ-flagging suspended until ≥ this many prior sidecars exist

HOME = Path(os.environ.get("HOME", "/home/larry"))
_AGENTS = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT") or HOME / "agents")
DEFAULT_COSTS_FILE = _AGENTS / "blackboard" / "costs.jsonl"
DEFAULT_OUTBOX_ROOT = _AGENTS / "outboxes"
DEFAULT_OUTPUT_DIR = _AGENTS / "blackboard" / "ledger"
DEFAULT_HALT_FLAG = _AGENTS / "blackboard" / "EMERGENCY_HALT"


# --- data types ---


@dataclass
class CostRow:
    ts: datetime
    agent: str
    task_id: str
    model: str
    cost_usd: float
    duration_sec: Optional[float]
    source: str
    task_type: str = "unknown"  # filled by archive join


@dataclass
class WeeklyReport:
    week_ending: str
    total_usd: float
    by_agent: dict[str, dict[str, float | int]]
    by_task_type: dict[str, dict[str, float | int]]
    anomalies: list[dict[str, Any]]
    retry_overhead: dict[str, float]
    top_5_tasks: list[dict[str, Any]]
    delta_vs_prior_week: Optional[dict[str, Any]]
    sigma_flagging_active: bool
    rows: list[CostRow] = field(default_factory=list)


# --- IO helpers ---


def _parse_ts(raw: str) -> datetime:
    """Parse the ts field — handles `+00:00` suffix and naive forms.

    `costs.jsonl` has two shapes in the wild: ISO with `+00:00` (early rows
    written by inbox-watcher) and naive ISO without TZ (later rows). Treat
    naive as UTC.
    """
    s = raw
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_cost_rows(
    costs_file: Path, window_start: datetime, window_end: datetime
) -> tuple[list[CostRow], int]:
    """Load cost rows in [window_start, window_end). Returns (rows, skipped_count)."""
    rows: list[CostRow] = []
    skipped = 0
    if not costs_file.exists():
        raise FileNotFoundError(f"costs file not found: {costs_file}")
    with open(costs_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts = _parse_ts(obj["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                skipped += 1
                continue
            if not (window_start <= ts < window_end):
                continue
            cost_usd = obj.get("cost_usd")
            if cost_usd is None:
                skipped += 1
                continue
            rows.append(
                CostRow(
                    ts=ts,
                    agent=obj.get("agent", "unknown"),
                    task_id=obj.get("task_id", "unknown"),
                    model=obj.get("model", "unknown"),
                    cost_usd=float(cost_usd),
                    duration_sec=obj.get("duration_sec"),
                    source=obj.get("source", "unknown"),
                )
            )
    return rows, skipped


def attribute_task_types(rows: list[CostRow], outbox_root: Path) -> None:
    """For each row, look up task_type from the matching outbox archive.

    Mutates rows in place. Lookup order:
      1. outbox archive `task_type` field — authoritative when the dispatch
         envelope set one explicitly.
      2. `infer_task_type(task_id)` fallback (task_type_inference module) —
         maps known task_id prefixes to labels (notify-* -> notification,
         cycle-* -> cycle, marker-error-* -> retry, etc.). Recovers ~90%+
         of historical rows that pre-dated dispatch envelopes carrying
         task_type.

    Rows whose task_id matches no known prefix end up as "unclassified",
    distinct from the legacy "unknown" default (so new bug-finding can tell
    "writer set nothing" apart from "inference saw the id and gave up").
    """
    for row in rows:
        # Outbox archive names are sanitize-only (inbox_watcher writes the
        # outbox via _unique_dest without truncation), so match that — a raw
        # task_id misses for a path-structural id.
        archive = outbox_root / row.agent / ".archive" / safe_write_inbox.sanitize_component(f"{row.task_id}.json")
        if archive.exists():
            try:
                with open(archive, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                tt = obj.get("task_type")
                if tt:
                    row.task_type = tt
                    continue
            except (json.JSONDecodeError, OSError):
                pass
        # Archive missing, unreadable, or had no task_type — infer from prefix.
        if row.task_type in (None, "", "unknown"):
            row.task_type = infer_task_type(row.task_id)


# --- computation ---


def _round2(x: float) -> float:
    """Round to 2 decimals for human display in the markdown.

    JSON sidecar preserves full precision; this is only for the .md surfaces.
    """
    return round(x, 2)


def compute_by_agent(rows: list[CostRow]) -> dict[str, dict[str, float | int]]:
    out: dict[str, dict[str, float | int]] = {}
    for row in rows:
        bucket = out.setdefault(row.agent, {"usd": 0.0, "task_count": 0})
        bucket["usd"] += row.cost_usd
        bucket["task_count"] += 1
    return out


def compute_by_task_type(rows: list[CostRow]) -> dict[str, dict[str, float | int]]:
    out: dict[str, dict[str, float | int]] = {}
    for row in rows:
        bucket = out.setdefault(row.task_type, {"usd": 0.0, "task_count": 0})
        bucket["usd"] += row.cost_usd
        bucket["task_count"] += 1
    return out


def _load_prior_sidecars(
    output_dir: Path, week_ending: datetime, n_weeks: int
) -> list[dict[str, Any]]:
    """Return up to n_weeks prior sidecars, most-recent-first."""
    found: list[dict[str, Any]] = []
    for i in range(1, n_weeks + 1):
        prior_date = (week_ending - timedelta(days=7 * i)).date().isoformat()
        prior = output_dir / f"weekly-{prior_date}.json"
        if not prior.exists():
            continue
        try:
            with open(prior, "r", encoding="utf-8") as f:
                found.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return found


def compute_baselines(
    prior_sidecars: list[dict[str, Any]],
) -> dict[str, tuple[float, float]]:
    """Build per-task_type baselines (mean, stdev) of per-task cost.

    Uses the by_task_type summaries from prior sidecars: mean cost per task
    in each bucket. stdev is computed across weeks if ≥2 weeks of data exist
    for that task_type; else stdev=0.0 which disables σ-flagging for it.
    """
    per_type_means: dict[str, list[float]] = {}
    for sidecar in prior_sidecars:
        bt = sidecar.get("by_task_type", {})
        for task_type, bucket in bt.items():
            count = bucket.get("task_count", 0)
            usd = bucket.get("usd", 0.0)
            if count > 0:
                per_type_means.setdefault(task_type, []).append(usd / count)
    out: dict[str, tuple[float, float]] = {}
    for task_type, means in per_type_means.items():
        if len(means) < 2:
            out[task_type] = (statistics.fmean(means), 0.0)
        else:
            out[task_type] = (statistics.fmean(means), statistics.stdev(means))
    return out


def compute_anomalies(
    rows: list[CostRow],
    baselines: dict[str, tuple[float, float]],
    sigma_flagging_active: bool,
    ramp_up_weeks_observed: int,
) -> list[dict[str, Any]]:
    """Return anomaly list.

    When σ-flagging is suspended (insufficient baseline), returns a single
    informational entry rather than emitting potentially false-positive σ rows.
    """
    if not sigma_flagging_active:
        return [
            {
                "task_id": "_ramp_up_notice",
                "agent": "_ledger",
                "task_type": "_ramp_up",
                "cost_usd": 0.0,
                "baseline_usd": 0.0,
                "sigma_above": 0.0,
                "context": (
                    f"baseline ramp-up: {ramp_up_weeks_observed} prior weekly "
                    f"window(s) observed; σ-flagging suspended until "
                    f"≥{RAMP_UP_WEEKS} weeks of data"
                ),
            }
        ]
    out: list[dict[str, Any]] = []
    for row in rows:
        baseline = baselines.get(row.task_type)
        if baseline is None:
            continue
        mean, stdev = baseline
        if stdev <= 0.0:
            continue
        sigma_above = (row.cost_usd - mean) / stdev
        if sigma_above >= SIGMA_THRESHOLD:
            out.append(
                {
                    "task_id": row.task_id,
                    "agent": row.agent,
                    "task_type": row.task_type,
                    "cost_usd": row.cost_usd,
                    "baseline_usd": mean,
                    "sigma_above": sigma_above,
                    "context": (
                        f"task cost ${row.cost_usd:.2f} vs ${mean:.2f} "
                        f"baseline (n={BASELINE_WEEKS}wk)"
                    ),
                }
            )
    out.sort(key=lambda a: a["sigma_above"], reverse=True)
    return out


def _is_retry_task_id(tid: str) -> bool:
    """Return True iff `tid` names a row that represents work paid for a retry.

    Discriminator (tuned 2026-05-18 after week-1 measurement showed v1 over-
    counted by ~3-4x):

    * `notify-*` rows are EXCLUDED. They are the inter-agent workflow channel
      (a task that goes Forge -> Mirror -> auto-merge produces 3 notify-<id>
      rows; that is the legitimate workflow shape, not a retry). The v1
      heuristic counted all notify-* as retries, which is what inflated the
      reported overhead.
    * `marker-error-*` rows (and the cascade form `marker-error-marker-error-*`)
      ARE retries — the marker-error notify cascade is, by definition, work
      Forge or Mirror is redoing after emitting a malformed marker.
    * `cycle-fix-*` rows ARE retries — Pulse-driven re-dispatch when the cycle
      surfaced a fault that needs Forge or Beacon to redo a piece of work.
    * Rows containing `-revision-` ARE retries — Forge revision rounds where
      Mirror sent her back to fix findings on an open PR.
    """
    if tid.startswith("notify-"):
        return False
    if tid.startswith("marker-error-"):
        return True
    if tid.startswith("cycle-fix-"):
        return True
    if "-revision-" in tid:
        return True
    return False


def compute_retry_overhead(
    rows: list[CostRow], total_usd: float
) -> dict[str, float]:
    """Sum cost of retry/clarification rows.

    See `_is_retry_task_id` for the discriminator and the rationale behind
    the v2 tuning (2026-05-18 week-1 follow-up).
    """
    retry_usd = sum(row.cost_usd for row in rows if _is_retry_task_id(row.task_id))
    percent = (retry_usd / total_usd * 100.0) if total_usd > 0 else 0.0
    return {"total_retry_cost_usd": retry_usd, "percent_of_total": percent}


def compute_top_5(rows: list[CostRow]) -> list[dict[str, Any]]:
    sorted_rows = sorted(rows, key=lambda r: r.cost_usd, reverse=True)[:5]
    return [
        {"task_id": r.task_id, "agent": r.agent, "cost_usd": r.cost_usd}
        for r in sorted_rows
    ]


def _weeks_back_of(
    sidecar: dict[str, Any], week_ending: datetime
) -> Optional[int]:
    """How many whole weeks before ``week_ending`` this sidecar covers.

    Reads the sidecar's own ``week_ending`` field. Returns None when it is
    absent, unparseable, or not an exact multiple of 7 days back (audit #38).
    """
    we = sidecar.get("week_ending")
    if not we:
        return None
    try:
        prior_we = datetime.fromisoformat(we)
    except (TypeError, ValueError):
        return None
    days = (week_ending.date() - prior_we.date()).days
    if days <= 0 or days % 7 != 0:
        return None
    return days // 7


def compute_delta(
    total_usd: float, prior_sidecar: Optional[dict[str, Any]],
    weeks_back: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    if prior_sidecar is None:
        return None
    prior_total = prior_sidecar.get("total_usd")
    if prior_total is None:
        return None
    absolute = total_usd - prior_total
    percent = (absolute / prior_total * 100.0) if prior_total > 0 else 0.0
    # weeks_back lets callers distinguish a genuine "vs prior week" (1) from a
    # gapped comparison against an older baseline (≥2 / None) so neither the
    # label nor the drift alert misrepresents the window (audit #38).
    return {"absolute_usd": absolute, "percent": percent,
            "weeks_back": weeks_back}


def _delta_window_label(delta: dict[str, Any]) -> str:
    """Human label for the delta's baseline window (audit #38).

    'prior week' only when the baseline is genuinely last week; otherwise name
    the real gap so the report never claims 'vs prior week' against an older
    baseline."""
    wb = delta.get("weeks_back")
    if wb == 1:
        return "prior week"
    if isinstance(wb, int) and wb >= 2:
        return f"{wb} weeks ago"
    return "an earlier week"


def _drift_is_real(delta: Optional[dict[str, Any]]) -> bool:
    """True when the week-over-week drift gate should fire.

    Only a genuine last-week baseline (weeks_back == 1) can be called drift; a
    gapped comparison against an older week must not trip the anomaly DM/health
    against the wrong window (audit #38)."""
    return (
        delta is not None
        and delta.get("weeks_back") == 1
        and abs(delta["percent"]) > DRIFT_PERCENT_THRESHOLD
    )


# --- main report assembly ---


def compute_weekly_report(
    week_ending: datetime,
    costs_file: Path,
    outbox_root: Path,
    output_dir: Path,
) -> tuple[WeeklyReport, dict[str, Any], int]:
    """Build the WeeklyReport + JSON-serializable dict + skipped-row count.

    `week_ending` is a Monday (timezone-aware). The covered window is
    [week_ending - 7 days, week_ending).
    """
    window_start = week_ending - timedelta(days=7)
    rows, skipped = load_cost_rows(costs_file, window_start, week_ending)
    attribute_task_types(rows, outbox_root)

    total_usd = sum(r.cost_usd for r in rows)
    by_agent = compute_by_agent(rows)
    by_task_type = compute_by_task_type(rows)

    prior_sidecars = _load_prior_sidecars(output_dir, week_ending, BASELINE_WEEKS)
    sigma_active = len(prior_sidecars) >= RAMP_UP_WEEKS
    baselines = compute_baselines(prior_sidecars) if sigma_active else {}

    anomalies = compute_anomalies(rows, baselines, sigma_active, len(prior_sidecars))
    retry_overhead = compute_retry_overhead(rows, total_usd)
    top_5 = compute_top_5(rows)
    # _load_prior_sidecars skips missing/corrupt weeks, so prior_sidecars[0] is
    # the most-recent EXISTING sidecar — not necessarily last week. Tag the
    # delta with how many weeks back it actually is (audit #38).
    prior_for_delta = prior_sidecars[0] if prior_sidecars else None
    weeks_back = (
        _weeks_back_of(prior_for_delta, week_ending)
        if prior_for_delta is not None else None
    )
    delta = compute_delta(total_usd, prior_for_delta, weeks_back)

    report = WeeklyReport(
        week_ending=week_ending.date().isoformat(),
        total_usd=total_usd,
        by_agent=by_agent,
        by_task_type=by_task_type,
        anomalies=anomalies,
        retry_overhead=retry_overhead,
        top_5_tasks=top_5,
        delta_vs_prior_week=delta,
        sigma_flagging_active=sigma_active,
        rows=rows,
    )

    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "week_ending": report.week_ending,
        "total_usd": report.total_usd,
        "delta_vs_prior_week": report.delta_vs_prior_week,
        "by_agent": report.by_agent,
        "by_task_type": report.by_task_type,
        "anomalies": report.anomalies,
        "retry_overhead": report.retry_overhead,
        "top_5_tasks": report.top_5_tasks,
    }
    return report, sidecar, skipped


# --- markdown rendering ---


def render_markdown(report: WeeklyReport, skipped_rows: int) -> str:
    lines: list[str] = []
    lines.append(f"# Ledger — Weekly Report ({report.week_ending})")
    lines.append("")
    lines.append(f"**Total spend:** ${_round2(report.total_usd):.2f}")
    if report.delta_vs_prior_week is not None:
        d = report.delta_vs_prior_week
        sign = "+" if d["absolute_usd"] >= 0 else "−"
        lines.append(
            f"**Vs {_delta_window_label(d)}:** "
            f"{sign}${abs(_round2(d['absolute_usd'])):.2f} "
            f"({sign}{abs(_round2(d['percent'])):.1f}%)"
        )
    else:
        lines.append("**Vs prior week:** _no prior week to compare_")
    if not report.sigma_flagging_active:
        lines.append(
            f"**Baseline:** ramp-up — σ-flagging suspended "
            f"(need ≥{RAMP_UP_WEEKS} prior weekly windows)"
        )
    if skipped_rows:
        lines.append(f"**Skipped rows:** {skipped_rows} (unparseable or missing cost_usd)")
    lines.append("")

    lines.append("## By agent")
    lines.append("")
    if report.by_agent:
        lines.append("| Agent | USD | Tasks |")
        lines.append("|---|---:|---:|")
        for agent in sorted(report.by_agent.keys()):
            b = report.by_agent[agent]
            lines.append(f"| {agent} | ${_round2(b['usd']):.2f} | {b['task_count']} |")
    else:
        lines.append("_no rows_")
    lines.append("")

    lines.append("## By task_type")
    lines.append("")
    if report.by_task_type:
        lines.append("| task_type | USD | Tasks |")
        lines.append("|---|---:|---:|")
        for tt in sorted(report.by_task_type.keys()):
            b = report.by_task_type[tt]
            lines.append(f"| {tt} | ${_round2(b['usd']):.2f} | {b['task_count']} |")
    else:
        lines.append("_no rows_")
    lines.append("")

    lines.append("## Anomalies")
    lines.append("")
    if report.anomalies:
        for a in report.anomalies:
            if a["task_id"] == "_ramp_up_notice":
                lines.append(f"- _{a['context']}_")
            else:
                lines.append(
                    f"- `{a['task_id']}` ({a['agent']}, {a['task_type']}) — "
                    f"${_round2(a['cost_usd']):.2f} "
                    f"(baseline ${_round2(a['baseline_usd']):.2f}, "
                    f"{a['sigma_above']:.1f}σ above). {a['context']}"
                )
    else:
        lines.append("_none flagged_")
    lines.append("")

    lines.append("## Retry / clarification overhead")
    lines.append("")
    r = report.retry_overhead
    lines.append(
        f"${_round2(r['total_retry_cost_usd']):.2f} "
        f"({_round2(r['percent_of_total']):.1f}% of total)"
    )
    lines.append("")

    lines.append("## Top 5 most expensive tasks")
    lines.append("")
    if report.top_5_tasks:
        lines.append("| task_id | agent | USD |")
        lines.append("|---|---|---:|")
        for t in report.top_5_tasks:
            lines.append(f"| `{t['task_id']}` | {t['agent']} | ${_round2(t['cost_usd']):.2f} |")
    else:
        lines.append("_no rows_")
    lines.append("")

    return "\n".join(lines)


# --- DM headline ---


def render_dm_headline(report: WeeklyReport, report_path: Path) -> tuple[str, bool]:
    """Return (message, is_anomaly_shape).

    Anomaly shape fires when at least one real σ anomaly is present OR drift
    > DRIFT_PERCENT_THRESHOLD. The ramp-up notice is NOT a real anomaly.
    """
    real_anoms = [a for a in report.anomalies if a["task_id"] != "_ramp_up_notice"]
    drift_high = _drift_is_real(report.delta_vs_prior_week)
    is_anomaly = bool(real_anoms) or drift_high

    if is_anomaly:
        if real_anoms:
            top = real_anoms[0]
            anom_phrase = f"top anomaly: `{top['task_id']}` at ${_round2(top['cost_usd']):.2f}"
        else:
            anom_phrase = "no σ anomalies; week-over-week drift > threshold"
        drift_phrase = ""
        if report.delta_vs_prior_week is not None:
            d = report.delta_vs_prior_week
            sign = "+" if d["absolute_usd"] >= 0 else "−"
            drift_phrase = (
                f", {sign}{abs(_round2(d['percent'])):.1f}% "
                f"vs {_delta_window_label(d)}"
            )
        msg = (
            f"📒 Week of {report.week_ending}: ${_round2(report.total_usd):.2f} total"
            f"{drift_phrase}. {anom_phrase}. Report: {report_path}"
        )
    else:
        msg = (
            f"📒 Week of {report.week_ending}: ${_round2(report.total_usd):.2f} total, "
            f"all within baseline. Report: {report_path}"
        )
    return msg, is_anomaly


# --- atomic write ---


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def touch_sentinel(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


# --- journal append ---


def append_journal(
    journal_path: Path,
    iteration: int,
    week_ending: str,
    report: WeeklyReport,
    sentinel_path: Path,
    dm_result: str,
    skipped_rows: int,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    real_anoms = [a for a in report.anomalies if a["task_id"] != "_ramp_up_notice"]
    health = "✅ Nominal"
    if real_anoms:
        health = "🟡 Anomalies"
    elif _drift_is_real(report.delta_vs_prior_week):
        health = "🟡 Drift"
    entry = []
    entry.append("")
    entry.append(f"## Iteration {iteration} — {now}")
    entry.append("")
    entry.append(f"**Week ending:** {week_ending}")
    entry.append(f"**Health:** {health}")
    entry.append(f"**Total:** ${_round2(report.total_usd):.2f}")
    if report.delta_vs_prior_week is not None:
        d = report.delta_vs_prior_week
        sign = "+" if d["absolute_usd"] >= 0 else "−"
        entry.append(
            f"**Vs {_delta_window_label(d)}:** "
            f"{sign}${abs(_round2(d['absolute_usd'])):.2f} "
            f"({sign}{abs(_round2(d['percent'])):.1f}%)"
        )
    else:
        entry.append("**Vs prior:** n/a (no prior week)")
    entry.append(f"**Anomalies:** {len(real_anoms)} σ-flagged" +
                 ("" if report.sigma_flagging_active else " (ramp-up: σ-flagging suspended)"))
    entry.append(f"**Skipped rows:** {skipped_rows}")
    entry.append(f"**Sentinel:** {sentinel_path}")
    entry.append(f"**DM:** {dm_result}")
    entry.append("")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write("\n".join(entry))


def _next_iteration(journal_path: Path) -> int:
    """Read the journal and return the next monotonic iteration number."""
    if not journal_path.exists():
        return 1
    max_iter = 0
    with open(journal_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("## Iteration "):
                try:
                    n = int(line.split()[2])
                    if n > max_iter:
                        max_iter = n
                except (IndexError, ValueError):
                    continue
    return max_iter + 1


# --- main ---


def _default_week_ending() -> datetime:
    """Return the most recent Monday at 00:00 UTC.

    If today is Monday, today is the week-ending (covers prior 7 days).
    If today is any other day, the most recent past Monday is the week-ending.
    """
    now = datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    days_since_monday = today.weekday()  # Monday=0
    return today - timedelta(days=days_since_monday)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Ledger weekly cost report.")
    p.add_argument(
        "--week-ending",
        help="ISO date (YYYY-MM-DD) — Monday of the week to report; "
             "defaults to the current Monday.",
    )
    p.add_argument("--costs-file", default=str(DEFAULT_COSTS_FILE))
    p.add_argument("--outbox-root", default=str(DEFAULT_OUTBOX_ROOT))
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--halt-flag", default=str(DEFAULT_HALT_FLAG))
    p.add_argument(
        "--journal",
        default=str(Path(__file__).resolve().parents[1] / "runbooks" / "ledger-journal.md"),
        help="Path to ledger-journal.md (default: repo runbooks/ledger-journal.md)",
    )
    p.add_argument(
        "--no-dm",
        action="store_true",
        help="Skip the larry_alerts.append_alert call (test / dry-run).",
    )
    args = p.parse_args(argv)

    halt_flag = Path(args.halt_flag)
    if halt_flag.exists():
        print(f"[ledger] EMERGENCY_HALT present at {halt_flag}; skipping run.")
        return 0

    if args.week_ending:
        week_ending = datetime.fromisoformat(args.week_ending).replace(tzinfo=timezone.utc)
    else:
        week_ending = _default_week_ending()
    week_str = week_ending.date().isoformat()

    costs_file = Path(args.costs_file)
    outbox_root = Path(args.outbox_root)
    output_dir = Path(args.output_dir)
    journal_path = Path(args.journal)

    try:
        report, sidecar, skipped = compute_weekly_report(
            week_ending, costs_file, outbox_root, output_dir
        )
    except FileNotFoundError as e:
        print(f"[ledger] {e}", file=sys.stderr)
        if journal_path.parent.exists() or journal_path.exists():
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            iteration = _next_iteration(journal_path)
            with open(journal_path, "a", encoding="utf-8") as f:
                f.write(
                    f"\n## Iteration {iteration} — {now}\n\n"
                    f"**Week ending:** {week_str}\n"
                    f"**Health:** 🔴 Failed\n"
                    f"**Error:** cost-capture unavailable ({e}); no report emitted.\n\n"
                )
        return 1

    md_path = output_dir / f"weekly-{week_str}.md"
    json_path = output_dir / f"weekly-{week_str}.json"
    sentinel_path = output_dir / f"ledger-ready-{week_str}"

    atomic_write(md_path, render_markdown(report, skipped))
    atomic_write(json_path, json.dumps(sidecar, indent=2) + "\n")
    touch_sentinel(sentinel_path)

    dm_message, is_anomaly = render_dm_headline(report, md_path)
    dm_result = "skipped (--no-dm)"
    if not args.no_dm:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import larry_alerts  # type: ignore
            # Routine weeks (no real anomaly, drift within threshold) emit at
            # `info`, which append_alert's severity->route default maps to the
            # digest lane (dashboard-only, no DM). A genuine cost anomaly emits
            # at `warning`, which defaults to escalate and DMs Larry.
            severity = "warning" if is_anomaly else "info"
            ok = larry_alerts.append_alert(
                source="ledger",
                severity=severity,
                message=dm_message,
                subject=f"weekly-{week_str}",
            )
            dm_result = "queued" if ok else "cooldown-suppressed or write failed"
        except Exception as e:  # noqa: BLE001
            dm_result = f"larry_alerts unavailable ({e})"

    iteration = _next_iteration(journal_path)
    append_journal(
        journal_path,
        iteration,
        week_str,
        report,
        sentinel_path,
        dm_result,
        skipped,
    )

    print(f"[ledger] wrote {md_path}")
    print(f"[ledger] wrote {json_path}")
    print(f"[ledger] sentinel {sentinel_path}")
    print(f"[ledger] DM: {dm_result}")
    print(f"[ledger] journal iteration {iteration} appended")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
