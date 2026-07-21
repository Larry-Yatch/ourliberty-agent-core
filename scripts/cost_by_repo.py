#!/usr/bin/env python3
"""Per-repo cost attribution report (approach A — no historical backfill).

Reads `~/agents/blackboard/costs.jsonl` over a window (default 30 days) and
buckets each row's `cost_usd` with STRICT precedence:

  (i)   INFRA        — rows whose `source` is recurring infra/chat spend
                       (classified from config/recurring-infra.json). These
                       are not attributable to a single build repo.
  (ii)  PER-REPO     — non-infra rows carrying a `target_repo`, grouped by repo
                       (stamped at write-time by inbox_watcher._record_outbox_cost).
  (iii) UNATTRIBUTED — everything else: non-infra rows with no target_repo
                       (e.g. pre-change history, chat rows without a repo).

Precedence is strict: an infra-source row lands in INFRA and is NEVER also
counted in per-repo or unattributed. `cost_usd` of None coalesces to 0, so the
reconciliation identity holds exactly:

    sum(per-repo) + infra + unattributed == sum(cost_usd, None->0) over the window

Separately, the static recurring-dollar infra tally from
config/recurring-infra.json `monthly_usd` (SaaS subscriptions that never appear
in costs.jsonl) is reported as a fixed-cost line.

Report-only; no dashboard view, no budget alerts, no per-mission cards.

    python3 scripts/cost_by_repo.py                 # 30d, text
    python3 scripts/cost_by_repo.py --window-days 7 # 7d
    python3 scripts/cost_by_repo.py --format json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# --- path resolution (mirrors ledger_weekly / active_tier) ---

_SCRIPTS_DIR = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HOME", "/home/larry"))
_AGENTS = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT") or HOME / "agents")
DEFAULT_COSTS_FILE = _AGENTS / "blackboard" / "costs.jsonl"
DEFAULT_CONFIG_FILE = _SCRIPTS_DIR.parent / "config" / "recurring-infra.json"
DEFAULT_WINDOW_DAYS = 30


def _parse_ts(raw: str) -> datetime:
    """Parse a costs.jsonl `ts` — tolerates a trailing `Z` and naive (no-TZ)
    forms (both shapes exist in the wild). Naive is treated as UTC."""
    s = raw
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_config(config_file: Path = DEFAULT_CONFIG_FILE) -> dict[str, Any]:
    """Read recurring-infra.json. Fail-safe: a missing/broken config yields
    empty classifiers + an empty dollar tally so the report still runs (every
    row simply falls through to per-repo/unattributed)."""
    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sources": [], "source_suffixes": [], "monthly_usd": {}}
    return {
        "sources": list(cfg.get("sources") or []),
        "source_suffixes": list(cfg.get("source_suffixes") or []),
        "monthly_usd": dict(cfg.get("monthly_usd") or {}),
    }


def is_infra_source(source: Optional[str], infra_sources: list[str],
                    infra_suffixes: list[str]) -> bool:
    """True when `source` is recurring infra/chat spend — an exact match in
    `infra_sources`, or an endswith-match of any `infra_suffixes` entry (covers
    the `{agent}-telegram-bot` family without enumerating every agent slug)."""
    if not source:
        return False
    if source in infra_sources:
        return True
    return any(source.endswith(sfx) for sfx in infra_suffixes)


def _coalesce_cost(raw: Any) -> float:
    """None-safe cost. None -> 0.0; anything unparseable also -> 0.0 so a bad
    row never breaks the reconciliation identity."""
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def bucket_costs(rows: list[dict], infra_sources: list[str],
                 infra_suffixes: list[str]) -> dict[str, Any]:
    """Bucket rows into infra / per-repo / unattributed with strict precedence.

    Returns {"per_repo": {repo: total}, "infra": total, "unattributed": total,
    "total": total}. The reconciliation identity holds by construction:
    sum(per_repo) + infra + unattributed == total == sum(coalesced cost_usd)."""
    infra_total = 0.0
    unattributed_total = 0.0
    per_repo: dict[str, float] = {}
    grand_total = 0.0
    for row in rows:
        cost = _coalesce_cost(row.get("cost_usd"))
        grand_total += cost
        if is_infra_source(row.get("source"), infra_sources, infra_suffixes):
            infra_total += cost
            continue
        repo = row.get("target_repo")
        if repo:
            per_repo[repo] = per_repo.get(repo, 0.0) + cost
        else:
            unattributed_total += cost
    return {
        "per_repo": per_repo,
        "infra": infra_total,
        "unattributed": unattributed_total,
        "total": grand_total,
    }


def load_rows(costs_file: Path, window_start: datetime,
              window_end: datetime) -> list[dict]:
    """Load raw costs.jsonl rows whose ts is in [window_start, window_end).
    Rows with an unparseable ts are skipped (they can't be windowed); a missing
    file yields no rows."""
    rows: list[dict] = []
    if not costs_file.exists():
        return rows
    with open(costs_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts = _parse_ts(obj["ts"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
            if window_start <= ts < window_end:
                rows.append(obj)
    return rows


def build_report(costs_file: Path, config_file: Path, window_days: int,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """Assemble the full report dict: windowed LLM buckets + the static
    monthly_usd infra tally."""
    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    cfg = load_config(config_file)
    rows = load_rows(costs_file, window_start, now)
    buckets = bucket_costs(rows, cfg["sources"], cfg["source_suffixes"])

    monthly = cfg["monthly_usd"]
    monthly_total = sum(_coalesce_cost(v) for v in monthly.values())

    return {
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "row_count": len(rows),
        "llm_costs": {
            "per_repo": buckets["per_repo"],
            "infra": buckets["infra"],
            "unattributed": buckets["unattributed"],
            "total": buckets["total"],
        },
        "recurring_infra_monthly_usd": {
            "line_items": monthly,
            "total": monthly_total,
        },
    }


def render_text(report: dict[str, Any]) -> str:
    llm = report["llm_costs"]
    lines: list[str] = []
    lines.append(
        f"Per-repo cost attribution — last {report['window_days']}d "
        f"({report['row_count']} rows)"
    )
    lines.append(f"  window: {report['window_start']} .. {report['window_end']}")
    lines.append("")
    lines.append("LLM spend (costs.jsonl):")
    per_repo = llm["per_repo"]
    if per_repo:
        for repo in sorted(per_repo, key=lambda r: -per_repo[r]):
            lines.append(f"  {repo:<32} ${per_repo[repo]:>10.4f}")
    else:
        lines.append("  (no per-repo rows in window)")
    lines.append(f"  {'INFRA (chat/recurring)':<32} ${llm['infra']:>10.4f}")
    lines.append(f"  {'UNATTRIBUTED':<32} ${llm['unattributed']:>10.4f}")
    lines.append(f"  {'TOTAL':<32} ${llm['total']:>10.4f}")
    lines.append("")
    infra = report["recurring_infra_monthly_usd"]
    lines.append("Recurring infra (static, $/month):")
    if infra["line_items"]:
        for name in sorted(infra["line_items"]):
            lines.append(f"  {name:<32} ${_coalesce_cost(infra['line_items'][name]):>10.2f}")
    else:
        lines.append("  (none configured)")
    lines.append(f"  {'TOTAL / month':<32} ${infra['total']:>10.2f}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Per-repo cost attribution report.")
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument("--costs-file", default=str(DEFAULT_COSTS_FILE))
    p.add_argument("--config-file", default=str(DEFAULT_CONFIG_FILE))
    p.add_argument("--format", choices=["text", "json"], default="text")
    args = p.parse_args(argv)

    report = build_report(
        Path(args.costs_file), Path(args.config_file), args.window_days
    )
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
