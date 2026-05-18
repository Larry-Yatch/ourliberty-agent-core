#!/usr/bin/env python3
"""Pulse Check I — weekly optimization digest.

Spec: `agents/beacon/specs/pulse-check-i.md`.

Runs after Ledger writes the weekly sidecar (Monday morning). Reads
`~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json`, joins Pulse's own
engineering signals (retry overhead, recurring-task repeats from outbox
archives, σ-flagged anomalies), synthesizes 0–3 proposed optimizations
tagged with effort + impact, and emits:

  - A digest DM via `larry_alerts.append_alert` (heartbeat shape when
    nothing is actionable; full digest shape when proposals exist).
  - A `**Check I:**` block appended to `runbooks/cycle-journal.md`.
  - A structured JSON sidecar at `~/agents/blackboard/pulse-check-i/
    check-i-YYYY-MM-DD.json` for audit and test verification.

Determinism: no LLM in the loop. Pulse-the-LLM running /cycle may invoke
this script as part of its Monday cycle and extend the digest with prose;
the deterministic baseline ensures the acceptance criteria in spec § 6
hold regardless.

Triggers:
  - Scheduled: `/cycle` on Monday morning, after Ledger's sentinel exists.
    cycle-prompt.md § Check I gates this.
  - Manual: `/optimize` on Telegram. If the sidecar is >24h old, the bot
    refreshes Ledger first (out of scope for this module; the bot handles
    the orchestration). This script accepts `--force` to skip the Monday
    weekday gate.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_type_inference import infer_task_type
from typing import Any, Optional

# --- constants ---

SCHEMA_VERSION = "v1"
SIDECAR_MAX_AGE_DAYS = 7
SIDECAR_FRESH_MAX_AGE_HOURS = 24  # /optimize threshold

# Heuristic thresholds — tune after week 2 per spec § 8.
RETRY_OVERHEAD_PCT_THRESHOLD = 15.0
HIGH_REPEAT_COUNT_THRESHOLD = 3  # >= N retry suffixes for same task_id
SIGMA_ANOMALY_ESCALATE_THRESHOLD = 3.0
MAX_PROPOSALS_PER_DIGEST = 3

HOME = Path(os.environ.get("HOME", "/home/larry"))
DEFAULT_SIDECAR_DIR = HOME / "agents" / "blackboard" / "ledger"
DEFAULT_OUTBOX_ROOT = HOME / "agents" / "outboxes"
DEFAULT_OUTPUT_DIR = HOME / "agents" / "blackboard" / "pulse-check-i"
DEFAULT_HALT_FLAG = HOME / "agents" / "blackboard" / "EMERGENCY_HALT"
DEFAULT_JOURNAL = (
    Path(__file__).resolve().parents[1] / "runbooks" / "cycle-journal.md"
)


# --- IO helpers ---


def _atomic_write(path: Path, content: str) -> None:
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


def _default_week_ending(now: Optional[datetime] = None) -> datetime:
    """Most recent Monday at 00:00 UTC."""
    now = now or datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return today - timedelta(days=today.weekday())  # Monday=0


def _load_sidecar(sidecar_dir: Path, week_ending: str) -> Optional[dict[str, Any]]:
    path = sidecar_dir / f"weekly-{week_ending}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _sidecar_age_hours(sidecar_dir: Path, week_ending: str,
                      now: Optional[datetime] = None) -> Optional[float]:
    path = sidecar_dir / f"weekly-{week_ending}.json"
    if not path.exists():
        return None
    now = now or datetime.now(timezone.utc)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (now - mtime).total_seconds() / 3600.0


# --- engineering signals ---


def gather_retry_repeats(outbox_root: Path) -> list[dict[str, Any]]:
    """Scan outbox archives for tasks with multiple retry suffixes.

    A `task_id.json` written by the inbox-watcher is the canonical archive.
    Retries land as `task_id.1.json`, `task_id.2.json`, etc. (rotation by
    safe_write_inbox when a result with the same task_id appears). A task
    with ≥ HIGH_REPEAT_COUNT_THRESHOLD suffixes is a candidate for templating
    / fast-pathing — these are the patterns Check I surfaces.

    Excludes `notify-*` task_ids (the inter-agent workflow channel).
    A task that goes Forge -> Mirror -> auto-merge produces three notify-<id>
    files in Beacon's outbox archive (forge-result, mirror-result, auto-merge),
    all with the same notify-<id> task_id, which safe_write_inbox rotates as
    `.1.json`, `.2.json`. Counting these as retries is the same v1 measurement
    bug that PR #33 fixed in Ledger's compute_retry_overhead one level up.

    Returns a list of {task_id, agent, retry_count} sorted desc by retries.
    """
    counts: dict[tuple[str, str], int] = {}
    if not outbox_root.exists():
        return []
    for agent_dir in outbox_root.iterdir():
        archive = agent_dir / ".archive"
        if not archive.is_dir():
            continue
        for f in archive.iterdir():
            name = f.name
            if not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            # task_id may have a numeric retry suffix: stem.split(".")[-1] is
            # digit-only iff there's a retry suffix. base task_id is the prefix.
            parts = stem.rsplit(".", 1)
            if len(parts) == 2 and parts[1].isdigit():
                base = parts[0]
            else:
                base = stem
            # Exclude workflow notify-* rotations. These are not retries.
            if infer_task_type(base) == "notification":
                continue
            key = (agent_dir.name, base)
            counts[key] = counts.get(key, 0) + 1
    repeats = [
        {"agent": agent, "task_id": tid, "retry_count": n}
        for (agent, tid), n in counts.items()
        if n >= HIGH_REPEAT_COUNT_THRESHOLD
    ]
    repeats.sort(key=lambda r: (-r["retry_count"], r["task_id"]))
    return repeats


# --- proposal synthesis ---


def synthesize_proposals(
    sidecar: dict[str, Any],
    repeats: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build up to MAX_PROPOSALS_PER_DIGEST proposals from sidecar + signals.

    Each proposal is {title, effort, impact, rationale}. The heuristics are
    deliberately simple — v1; tune after 2 weeks of real data per spec § 8.
    """
    proposals: list[dict[str, Any]] = []

    retry_overhead = sidecar.get("retry_overhead", {}) or {}
    overhead_pct = float(retry_overhead.get("percent_of_total", 0.0) or 0.0)
    overhead_usd = float(retry_overhead.get("total_retry_cost_usd", 0.0) or 0.0)
    if overhead_pct >= RETRY_OVERHEAD_PCT_THRESHOLD:
        proposals.append({
            "title": "Investigate retry / clarification cost sources",
            "effort": "medium",
            "impact": (
                f"~${overhead_usd:.2f}/wk reclaimable ({overhead_pct:.1f}% "
                f"of total spend is retries/clarifications)"
            ),
            "rationale": (
                "Retry overhead is above the 15% threshold. Audit the "
                "outbox-notifier log for the dominant retry shapes (revision, "
                "clarification, cycle-fix) and tighten the upstream "
                "preflight / spec template that caused them."
            ),
        })

    # σ anomalies above the escalate threshold get their own proposal slot.
    sigma_hits = [
        a for a in (sidecar.get("anomalies") or [])
        if isinstance(a, dict)
        and a.get("task_id") != "_ramp_up_notice"
        and float(a.get("sigma_above", 0.0) or 0.0)
        >= SIGMA_ANOMALY_ESCALATE_THRESHOLD
    ]
    if sigma_hits and len(proposals) < MAX_PROPOSALS_PER_DIGEST:
        top = sigma_hits[0]
        proposals.append({
            "title": f"Review high-σ anomaly task `{top.get('task_id')}`",
            "effort": "small",
            "impact": (
                f"${float(top.get('cost_usd', 0.0)):.2f} task vs "
                f"${float(top.get('baseline_usd', 0.0)):.2f} baseline "
                f"({float(top.get('sigma_above', 0.0)):.1f}σ above)"
            ),
            "rationale": (
                f"Ledger flagged this task at "
                f"{float(top.get('sigma_above', 0.0)):.1f}σ above baseline. "
                f"Read the chain archive and propose either: a fast-path "
                f"for the shape, a prompt-discipline fix, or a model "
                f"downgrade if the depth wasn't warranted."
            ),
        })

    if repeats and len(proposals) < MAX_PROPOSALS_PER_DIGEST:
        top_repeat = repeats[0]
        proposals.append({
            "title": (
                f"Template / fast-path repeating shape "
                f"`{top_repeat['task_id']}`"
            ),
            "effort": "medium",
            "impact": (
                f"{top_repeat['retry_count']} repeats observed this week; "
                f"templating would collapse most retry cycles"
            ),
            "rationale": (
                f"Outbox archives show this task_id retried "
                f"{top_repeat['retry_count']} times on agent "
                f"`{top_repeat['agent']}`. Recurring shapes are the "
                f"prime candidate for the teach-to-fish discipline — "
                f"propose a templated dispatch or an upstream fix to "
                f"Beacon."
            ),
        })

    return proposals[:MAX_PROPOSALS_PER_DIGEST]


# --- digest assembly ---


def _round2(x: float) -> float:
    return round(x, 2)


def assemble_check_i(
    sidecar: Optional[dict[str, Any]],
    repeats: list[dict[str, Any]],
    week_ending: str,
    sidecar_filename: Optional[str],
    fired_at: datetime,
) -> dict[str, Any]:
    """Return the structured Check I result.

    Three modes:
      - `skipped` — sidecar unavailable; no DM, just a journal note.
      - `heartbeat` — sidecar present but no proposals; minimal DM.
      - `digest` — sidecar + proposals; full DM.
    """
    if sidecar is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "week_ending": week_ending,
            "ledger_sidecar": None,
            "fired_at": fired_at.isoformat(),
            "mode": "skipped",
            "skip_reason": "Ledger sidecar unavailable",
            "ledger_headline": None,
            "engineering_signals": None,
            "proposals": [],
        }

    total_usd = float(sidecar.get("total_usd", 0.0) or 0.0)
    delta = sidecar.get("delta_vs_prior_week")
    raw_anoms = sidecar.get("anomalies") or []
    real_anoms = [
        a for a in raw_anoms
        if isinstance(a, dict) and a.get("task_id") != "_ramp_up_notice"
    ]
    retry_overhead = sidecar.get("retry_overhead", {}) or {}
    overhead_pct = float(retry_overhead.get("percent_of_total", 0.0) or 0.0)
    overhead_usd = float(retry_overhead.get("total_retry_cost_usd", 0.0) or 0.0)

    proposals = synthesize_proposals(sidecar, repeats)
    mode = "digest" if proposals else "heartbeat"

    return {
        "schema_version": SCHEMA_VERSION,
        "week_ending": week_ending,
        "ledger_sidecar": sidecar_filename,
        "fired_at": fired_at.isoformat(),
        "mode": mode,
        "skip_reason": None,
        "ledger_headline": {
            "total_usd": total_usd,
            "delta_vs_prior_week": delta,
            "anomaly_count": len(real_anoms),
        },
        "engineering_signals": {
            "retry_overhead_usd": overhead_usd,
            "retry_overhead_pct": overhead_pct,
            "sigma_anomalies": real_anoms,
            "high_repeat_tasks": repeats,
        },
        "proposals": proposals,
    }


# --- DM rendering ---


def render_dm(check_i: dict[str, Any]) -> str:
    """Produce the Telegram-bound DM body.

    Spec § 6: heartbeat shape on empty weeks ("Week of X: chain shapes
    nominal"); digest shape with Ledger's headline + Pulse's
    interpretation layer + proposals when actionable.
    """
    week = check_i["week_ending"]
    mode = check_i["mode"]
    if mode == "skipped":
        return (
            f"🩺 Pulse Check I (week of {week}): skipped — "
            f"{check_i.get('skip_reason', 'no reason recorded')}."
        )

    head = check_i["ledger_headline"] or {}
    total_usd = float(head.get("total_usd", 0.0) or 0.0)
    delta = head.get("delta_vs_prior_week")
    delta_phrase = ""
    if isinstance(delta, dict):
        absolute = float(delta.get("absolute_usd", 0.0) or 0.0)
        percent = float(delta.get("percent", 0.0) or 0.0)
        sign = "+" if absolute >= 0 else "−"
        delta_phrase = (
            f" ({sign}${abs(_round2(absolute)):.2f}, "
            f"{sign}{abs(_round2(percent)):.1f}% vs prior)"
        )

    if mode == "heartbeat":
        return (
            f"🩺 Pulse Check I (week of {week}): chain shapes nominal — "
            f"no proposed optimizations this week. "
            f"Ledger total ${_round2(total_usd):.2f}{delta_phrase}."
        )

    lines = [
        f"🩺 Pulse Check I (week of {week}):",
        f"Ledger total ${_round2(total_usd):.2f}{delta_phrase}; "
        f"{head.get('anomaly_count', 0)} σ-flagged anomaly(ies).",
    ]
    sigs = check_i["engineering_signals"] or {}
    overhead_pct = float(sigs.get("retry_overhead_pct", 0.0) or 0.0)
    if overhead_pct > 0:
        lines.append(f"Retry overhead: {_round2(overhead_pct):.1f}% of spend.")
    lines.append("")
    lines.append(f"Proposed optimizations ({len(check_i['proposals'])}):")
    for i, p in enumerate(check_i["proposals"], 1):
        lines.append(
            f"  {i}. [{p['effort']}] {p['title']} — {p['impact']}"
        )
    return "\n".join(lines)


# --- journal block ---


def render_journal_block(check_i: dict[str, Any]) -> str:
    """Render the `**Check I:**` block appended to cycle-journal.md.

    The block is additive — it does not replace the standard A-H cycle
    entry (spec § 6). Pulse-the-LLM running /cycle still writes its
    normal journal section; this block is appended right after.
    """
    week = check_i["week_ending"]
    mode = check_i["mode"]
    lines = ["", f"**Check I ({week}):**", ""]
    if mode == "skipped":
        lines.append(
            f"- Skipped: {check_i.get('skip_reason', 'no reason recorded')}"
        )
        return "\n".join(lines)

    head = check_i["ledger_headline"] or {}
    sigs = check_i["engineering_signals"] or {}
    lines.append(
        f"- Ledger total: ${_round2(float(head.get('total_usd', 0.0))):.2f}; "
        f"{head.get('anomaly_count', 0)} anomaly(ies)"
    )
    overhead_pct = float(sigs.get("retry_overhead_pct", 0.0) or 0.0)
    overhead_usd = float(sigs.get("retry_overhead_usd", 0.0) or 0.0)
    lines.append(
        f"- Retry overhead: ${_round2(overhead_usd):.2f} "
        f"({_round2(overhead_pct):.1f}%)"
    )
    repeats = sigs.get("high_repeat_tasks") or []
    if repeats:
        names = ", ".join(
            f"`{r['task_id']}`×{r['retry_count']}" for r in repeats[:5]
        )
        lines.append(f"- High-repeat tasks: {names}")

    if mode == "heartbeat":
        lines.append("- Mode: heartbeat (no proposed optimizations)")
        return "\n".join(lines)

    lines.append(f"- Mode: digest — {len(check_i['proposals'])} proposal(s):")
    for i, p in enumerate(check_i["proposals"], 1):
        lines.append(
            f"  {i}. [{p['effort']}] {p['title']} — {p['impact']}"
        )
        lines.append(f"     Rationale: {p['rationale']}")
    return "\n".join(lines)


def append_journal(journal_path: Path, block: str) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write(block + "\n")


# --- main ---


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Pulse Check I — weekly digest.")
    p.add_argument(
        "--week-ending",
        help="ISO date (YYYY-MM-DD) — Monday of the week to digest; "
             "defaults to the current Monday.",
    )
    p.add_argument("--sidecar-dir", default=str(DEFAULT_SIDECAR_DIR))
    p.add_argument("--outbox-root", default=str(DEFAULT_OUTBOX_ROOT))
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--halt-flag", default=str(DEFAULT_HALT_FLAG))
    p.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the Monday weekday gate. Used by /optimize.",
    )
    p.add_argument(
        "--no-dm",
        action="store_true",
        help="Skip larry_alerts.append_alert (test / dry-run).",
    )
    p.add_argument(
        "--no-journal",
        action="store_true",
        help="Skip appending to cycle-journal.md (test / dry-run).",
    )
    args = p.parse_args(argv)

    halt_flag = Path(args.halt_flag)
    if halt_flag.exists():
        print(f"[pulse-check-i] EMERGENCY_HALT present at {halt_flag}; "
              f"skipping run.")
        return 0

    now = datetime.now(timezone.utc)
    if args.week_ending:
        week_ending_dt = datetime.fromisoformat(args.week_ending).replace(
            tzinfo=timezone.utc
        )
    else:
        week_ending_dt = _default_week_ending(now)

    # Spec § 6: fires only on Monday cycles unless forced (`/optimize`).
    if not args.force and not args.week_ending and now.weekday() != 0:
        print(f"[pulse-check-i] today is not Monday (weekday={now.weekday()});"
              f" skipping. Use --force or /optimize for ad-hoc runs.")
        return 0

    week_ending = week_ending_dt.date().isoformat()
    sidecar_dir = Path(args.sidecar_dir)
    outbox_root = Path(args.outbox_root)
    output_dir = Path(args.output_dir)
    journal_path = Path(args.journal)

    sidecar = _load_sidecar(sidecar_dir, week_ending)
    sidecar_filename = (
        f"weekly-{week_ending}.json" if sidecar is not None else None
    )

    # Stale check: spec § 6, sidecar > 7 days old → skip.
    if sidecar is not None:
        age_hours = _sidecar_age_hours(sidecar_dir, week_ending, now=now)
        if age_hours is not None and age_hours > SIDECAR_MAX_AGE_DAYS * 24:
            sidecar = None
            sidecar_filename = None

    repeats = gather_retry_repeats(outbox_root)
    check_i = assemble_check_i(
        sidecar=sidecar,
        repeats=repeats,
        week_ending=week_ending,
        sidecar_filename=sidecar_filename,
        fired_at=now,
    )

    out_path = output_dir / f"check-i-{week_ending}.json"
    _atomic_write(out_path, json.dumps(check_i, indent=2) + "\n")

    dm_body = render_dm(check_i)
    dm_result = "skipped (--no-dm)"
    if not args.no_dm:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import larry_alerts  # type: ignore
            ok = larry_alerts.append_alert(
                source="pulse",
                severity="warning",
                message=dm_body,
                subject=f"check-i-{week_ending}",
            )
            dm_result = "queued" if ok else (
                "cooldown-suppressed or write failed"
            )
        except Exception as e:  # noqa: BLE001
            dm_result = f"larry_alerts unavailable ({e})"

    if not args.no_journal:
        block = render_journal_block(check_i)
        append_journal(journal_path, block)

    print(f"[pulse-check-i] mode={check_i['mode']}")
    print(f"[pulse-check-i] wrote {out_path}")
    print(f"[pulse-check-i] DM: {dm_result}")
    if not args.no_journal:
        print(f"[pulse-check-i] journal: appended to {journal_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
