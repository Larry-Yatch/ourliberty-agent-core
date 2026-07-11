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
    check-i-YYYY-MM-DD.json` (firing date) for audit and test verification.

Determinism: no LLM in the loop. Pulse-the-LLM running /cycle may invoke
this script as part of its Mon/Wed/Fri/Sun cycles and extend the digest
with prose; the deterministic baseline ensures the acceptance criteria
in spec § 6 hold regardless.

Triggers:
  - Scheduled: `/cycle` on Mon/Wed/Fri/Sun, after Ledger's sentinel
    exists. cycle-prompt.md § Check I gates this. Ledger itself remains
    weekly Monday; Check I re-reads the same sidecar each firing.
  - Manual: `/optimize` on Telegram. If the sidecar is missing or >24h old,
    this script auto-invokes `scripts/run_ledger.sh` to refresh it before
    proceeding. `--force` skips the Mon/Wed/Fri/Sun weekday gate.

Stdlib only.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# scripts/ on sys.path so sibling-module imports work whether the script is
# invoked directly or via `python3 -m`. safe_write_inbox itself adjusts
# sys.path; doing it here too keeps imports order-independent.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from task_type_inference import infer_task_type
from typing import Any, Optional

import safe_write_inbox  # noqa: E402 — sys.path adjusted above
from fixture_patterns import is_fixture_task_id  # noqa: E402
from emit_capture_impl import emit_capture  # noqa: E402 — Contract B §5.1 helper

# --- constants ---

SCHEMA_VERSION = "v1"
SIDECAR_MAX_AGE_DAYS = 7
# Trigger an auto-refresh (run_ledger.sh) when the sidecar is missing or
# older than this many hours. Above SIDECAR_MAX_AGE_DAYS the >7d stale-skip
# takes over instead.
SIDECAR_REFRESH_AGE_HOURS = 24.0
SIDECAR_REFRESH_TIMEOUT_SEC = 120
SCRIPT_DIR = Path(__file__).resolve().parent

# Weekdays on which Check I fires (Monday=0 ... Sunday=6).
# Mon/Wed/Fri/Sun cadence — Ledger remains weekly Monday; Check I re-reads
# the same sidecar on each firing.
CHECK_I_FIRING_WEEKDAYS = frozenset({0, 2, 4, 6})

# Heuristic thresholds — tune after week 2 per spec § 8.
RETRY_OVERHEAD_PCT_THRESHOLD = 15.0
HIGH_REPEAT_COUNT_THRESHOLD = 3  # >= N retry suffixes for same task_id
SIGMA_ANOMALY_ESCALATE_THRESHOLD = 3.0
MAX_PROPOSALS_PER_DIGEST = 3

# preflight-marker-discipline signal (task forge-marker-error-retry-fillin-001).
# Self-optimizing per the repo pattern: the alert condition is NOT a hand-picked
# magic miss count — it's derived from a trailing-window baseline, exactly like
# Ledger's σ-anomaly detector (ledger_weekly.py: SIGMA_THRESHOLD / BASELINE_WEEKS
# / RAMP_UP_WEEKS). We mirror those values so the two self-optimizing signals
# share one statistical convention.
MARKER_DISCIPLINE_AGENT = "forge"
MARKER_DISCIPLINE_BASELINE_WEEKS = 4
MARKER_DISCIPLINE_RAMP_UP_WEEKS = 4  # alerting suspended until ≥ this many prior weeks
MARKER_DISCIPLINE_SIGMA = 2.0  # current misses ≥ mean + σ·stdev ⇒ statistically elevated
MARKER_DISCIPLINE_MAX_DEPTH = 3  # MAX_MARKER_ERROR_RETRIES — cascade caps here

HOME = Path(os.environ.get("HOME", "/home/larry"))
_AGENTS = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT") or HOME / "agents")
DEFAULT_SIDECAR_DIR = _AGENTS / "blackboard" / "ledger"
DEFAULT_OUTBOX_ROOT = _AGENTS / "outboxes"
DEFAULT_OUTPUT_DIR = _AGENTS / "blackboard" / "pulse-check-i"
DEFAULT_HALT_FLAG = _AGENTS / "blackboard" / "EMERGENCY_HALT"
DEFAULT_JOURNAL = (
    Path(__file__).resolve().parents[1] / "runbooks" / "cycle-journal.md"
)

# Closed-loop step 5 (2026-05-24) — auto-dispatch tunables.
# Eligible-proposal heuristic (widened 2026-06-22): effort in {small, medium}
# AND a non-empty impact. Large-effort + impact-less proposals still surface in
# the digest / park to the funnel for Larry to triage. See
# _is_auto_dispatch_eligible.
# Same proposal recurring across Check I runs should only dispatch once
# per window. 7 days lines up with the weekly Ledger cadence — a recurring
# σ-anomaly that survives a week is genuinely new evidence.
AUTO_DISPATCH_DEDUP_WINDOW_DAYS = 7
DEFAULT_DISPATCH_STATE_FILE = _AGENTS / "state" / "pulse-check-i-dispatched.json"
# Contract B (park-the-nudge §5.2) — emitter-side dedup for parked proposals,
# mirroring DEFAULT_DISPATCH_STATE_FILE. Keys on _proposal_dedup_key; records
# the returned capture_id so DM suppression is earned by durable capture.
DEFAULT_PARKED_STATE_FILE = _AGENTS / "state" / "pulse-check-i-parked.json"


# --- IO helpers ---


def _atomic_write(path: Path, content: str) -> None:
    # Delegates to the shared guarded atomic_io writer. Byte-identical to the
    # prior inline text writer.
    import atomic_io
    atomic_io.atomic_write_text(path, content)


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


def _refresh_sidecar() -> None:
    """Invoke run_ledger.sh as a best-effort subprocess.

    Any failure (non-zero exit, timeout, missing script) is logged but does
    not raise — Check I falls back to whatever sidecar state exists on disk.
    """
    script = SCRIPT_DIR / "run_ledger.sh"
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["bash", str(script)],
            timeout=SIDECAR_REFRESH_TIMEOUT_SEC,
            check=False,
            capture_output=True,
        )
        took = time.monotonic() - started
        print(
            f"[pulse-check-i] sidecar refresh: invoked run_ledger.sh, "
            f"exit={result.returncode}, took {took:.1f}s"
        )
        if result.returncode != 0:
            stderr_tail = (result.stderr or b"").decode(
                "utf-8", errors="replace"
            ).strip().splitlines()[-3:]
            print(
                f"[pulse-check-i] WARN: run_ledger.sh non-zero; "
                f"stderr tail: {stderr_tail}"
            )
    except subprocess.TimeoutExpired:
        took = time.monotonic() - started
        print(
            f"[pulse-check-i] WARN: run_ledger.sh timed out after "
            f"{took:.1f}s (limit {SIDECAR_REFRESH_TIMEOUT_SEC}s); continuing"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[pulse-check-i] WARN: run_ledger.sh invocation failed: "
            f"{type(exc).__name__}: {exc}; continuing"
        )


# --- engineering signals ---


_MARKER_ERROR_PREFIX = "marker-error-"
_DEAD_LETTER_MARKER_PREFIX = "dead-letter-marker-"


def _unwrap_marker_error_base(stem: str) -> str | None:
    """Recover the underlying base task_id from a marker-error wrapped stem.

    Marker-error retries chain: each failed marker emission wraps the prior
    stem with another `marker-error-` prefix and appends a `-<N>` numeric
    suffix. e.g. `opmanual-d35-5b-shipped-note-001` → first retry as
    `marker-error-opmanual-d35-5b-shipped-note-001-1` → second retry as
    `marker-error-marker-error-opmanual-d35-5b-shipped-note-001-1-2` → ...

    Returns the un-wrapped base (here `opmanual-d35-5b-shipped-note-001`)
    or None if the stem is not a marker-error file.
    """
    if not stem.startswith(_MARKER_ERROR_PREFIX):
        return None
    depth = 0
    body = stem
    while body.startswith(_MARKER_ERROR_PREFIX):
        body = body[len(_MARKER_ERROR_PREFIX):]
        depth += 1
    # Strip the trailing `-<digit>` cascade-depth markers (one per wrap).
    for _ in range(depth):
        idx = body.rfind("-")
        if idx == -1 or not body[idx + 1:].isdigit():
            break
        body = body[:idx]
    return body


def _marker_error_retry_depth(stem: str) -> Optional[int]:
    """Retry depth (1-indexed) encoded in a marker-error archive stem.

    The notifier names each retry `marker-error-<base>-<N>` where N is the
    cumulative `marker_error_count` (1 on the first failed-marker round, 2 on
    the second, 3 on the third — the cascade caps at MAX_MARKER_ERROR_RETRIES).
    The trailing `-<N>` token is therefore the retry depth. This holds for the
    current flat form AND the legacy nested wrapping
    (`marker-error-marker-error-<base>-<N>-<M>`), where the final integer is
    still the cumulative count. Returns None when no trailing integer is
    present (caller treats that as depth 1 — at least a first attempt).
    """
    idx = stem.rfind("-")
    if idx == -1:
        return None
    tail = stem[idx + 1:]
    return int(tail) if tail.isdigit() else None


# One marker-error retry event recovered from an outbox archive. `depth` is the
# retry round (1/2/3); `mtime` is when the retry artifact was written (used to
# window the preflight-marker-discipline signal to the Check-I week).
_MarkerErrorEvent = collections.namedtuple(
    "_MarkerErrorEvent", ["agent", "base", "depth", "mtime"]
)


def _iter_marker_error_events(outbox_root: Path):
    """Yield one `_MarkerErrorEvent` per marker-error archive file.

    Single source of truth for marker-error retry parsing. Both
    `gather_retry_repeats` (aggregate repeat counts per base) and
    `compute_marker_discipline` (retry-depth distribution + trend) consume this
    generator, so there is exactly ONE parser with ONE set of exclusions — no
    divergent second scan. Exclusions mirror the prior `gather_retry_repeats`
    body verbatim:

      - `dead-letter-marker-*` — terminal recovery artifact, not a retry.
      - non-marker-error files (plain `<task>.json` / `<task>.N.json`) — skipped
        via `_unwrap_marker_error_base` returning None.
      - `notify-*` underlying base — inter-agent workflow noise.
      - fixture-pattern task_ids — test artifacts must not contaminate signal.
    """
    if not outbox_root.exists():
        return
    for agent_dir in outbox_root.iterdir():
        archive = agent_dir / ".archive"
        if not archive.is_dir():
            continue
        for f in archive.iterdir():
            name = f.name
            if not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            if stem.startswith(_DEAD_LETTER_MARKER_PREFIX):
                continue
            base = _unwrap_marker_error_base(stem)
            if base is None:
                continue
            if infer_task_type(base) == "notification":
                continue
            if is_fixture_task_id(base):
                continue
            try:
                mtime = datetime.fromtimestamp(
                    f.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                mtime = None
            yield _MarkerErrorEvent(
                agent_dir.name, base, _marker_error_retry_depth(stem), mtime
            )


def gather_retry_repeats(outbox_root: Path) -> list[dict[str, Any]]:
    """Scan outbox archives for tasks with real retry signal.

    `retry_count` reflects the number of *actual retry events* observed for
    a base task_id — not the count of archive entries. The inbox-watcher's
    `safe_write_inbox` rotates any second result with the same task_id to
    `<task>.1.json`, `<task>.2.json`, etc. Those rotations are ambiguous:
    they may be marker-error driven retries, beacon-clarification revision
    rounds, or just chain phases (Forge result + Mirror result both landing
    under the same canonical id). Counting them all as "retries" produced
    false-positive templating proposals (see Pulse audits 2026-05-24/25 on
    `opmanual-d35-5b-shipped-note-001` and `task-34-e4-2-mission-control-
    migration`), so v2 instead counts only artifacts with unambiguous retry
    semantics.

    Counted as retry events:
      - `marker-error-<base>-<N>.json` (and arbitrarily-nested
        `marker-error-marker-error-...-<base>-<N>-<M>-....json` wrappings)
        — each standalone file in the archive is one failed-marker retry of
        the underlying base task. The base is recovered by unwrapping the
        nested `marker-error-` prefixes and stripping the same number of
        trailing `-<digit>` depth markers.

    Skipped (NOT retries):
      - `notify-*` — inter-agent workflow channel; same v1 bug PR #33 fixed
        in Ledger's compute_retry_overhead one level up.
      - `dead-letter-marker-*` — terminal infra-failure marker, written
        once after a marker-error cascade exceeds retry budget. The cascade
        itself already contributes retry events; counting the dead-letter
        too would double-count.
      - Canonical `<task>.json` and `<task>.N.json` plain rotations — these
        are normal chain phases or revision rounds via the rotation-on-
        collision behavior, not retries by the originating dispatch.

    Returns a list of {task_id, agent, retry_count} for entries meeting
    HIGH_REPEAT_COUNT_THRESHOLD, sorted desc by retry_count then task_id.

    Parsing + exclusions live in the shared `_iter_marker_error_events` source
    (also used by `compute_marker_discipline`) so the two signals never drift.
    """
    counts: dict[tuple[str, str], int] = {}
    for ev in _iter_marker_error_events(outbox_root):
        key = (ev.agent, ev.base)
        counts[key] = counts.get(key, 0) + 1
    repeats = [
        {"agent": agent, "task_id": tid, "retry_count": n}
        for (agent, tid), n in counts.items()
        if n >= HIGH_REPEAT_COUNT_THRESHOLD
    ]
    repeats.sort(key=lambda r: (-r["retry_count"], r["task_id"]))
    return repeats


# --- preflight-marker-discipline signal ---


def _marker_discipline_window_dist(
    events: list[_MarkerErrorEvent],
    start: datetime,
    end: datetime,
    agent: str,
) -> dict[int, int]:
    """Retry-depth histogram for `agent` marker-error events in [start, end).

    Keys are retry depth (1..MARKER_DISCIPLINE_MAX_DEPTH); values are event
    counts. Depth is clamped into [1, MAX_DEPTH] defensively — the cascade caps
    at MAX_MARKER_ERROR_RETRIES so depths above that shouldn't occur.
    """
    dist: dict[int, int] = {}
    for ev in events:
        if ev.agent != agent or ev.mtime is None:
            continue
        if not (start <= ev.mtime < end):
            continue
        depth = ev.depth if isinstance(ev.depth, int) and ev.depth >= 1 else 1
        depth = min(depth, MARKER_DISCIPLINE_MAX_DEPTH)
        dist[depth] = dist.get(depth, 0) + 1
    return dist


def _load_prior_marker_discipline_misses(
    output_dir: Path, week_ending: datetime, baseline_weeks: int,
) -> dict[str, int]:
    """{prior_week_iso: misses} read from persisted Check I audit sidecars.

    The trailing-week baseline is built from what *prior* Check I runs recorded
    (this is the "trend vs the prior week's sidecar" source). Reading persisted
    audits — rather than recomputing prior windows from the archive — is what
    lets a clean week (0 misses) count as a genuine observation: the audit's
    existence proves Check I ran that week and saw zero, distinct from an empty
    pre-history window that simply has no archive files yet.

    Audits are named by firing date (4 firings/week) but every firing in a week
    reads the same window, so the persisted `misses` is identical within a week;
    last-write-wins per `week_ending` is therefore safe.
    """
    if not output_dir.is_dir():
        return {}
    by_week: dict[str, int] = {}
    for p in output_dir.glob("check-i-*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        wk = data.get("week_ending")
        sigs = data.get("engineering_signals") or {}
        md = sigs.get("marker_discipline") if isinstance(sigs, dict) else None
        if not isinstance(wk, str) or not isinstance(md, dict):
            continue
        misses = md.get("misses")
        if isinstance(misses, bool) or not isinstance(misses, int):
            continue
        by_week[wk] = misses
    out: dict[str, int] = {}
    for i in range(1, baseline_weeks + 1):
        prior_wk = (week_ending - timedelta(days=7 * i)).date().isoformat()
        if prior_wk in by_week:
            out[prior_wk] = by_week[prior_wk]
    return out


def compute_marker_discipline(
    outbox_root: Path,
    week_ending: datetime,
    output_dir: Path,
    agent: str = MARKER_DISCIPLINE_AGENT,
    baseline_weeks: int = MARKER_DISCIPLINE_BASELINE_WEEKS,
) -> dict[str, Any]:
    """Trend the Forge preflight MalformedForgeMarker rate over the Check-I week.

    Reuses the shared `_iter_marker_error_events` marker-error parser (same
    source as `gather_retry_repeats`) and windows it by artifact mtime to the
    Check-I week `[week_ending - 7d, week_ending)`. Reports:

      - `misses` — depth-1 marker-error events in the window. Each dispatch that
        trips the marker-error cascade writes exactly one depth-1 artifact, so
        this is the count of distinct preflight marker-discipline failures.
      - `retry_depth_distribution` — {1, 2, 3} event counts. A dispatch that
        escalates to retry-2 writes a depth-2 artifact too (and depth-3 if it
        nearly forfeits), so depth-2 count = dispatches that needed a 2nd
        attempt, depth-3 = those that hit the cap (near-forfeit).
      - `escalation_rate` — depth-2 / depth-1 (the retry-2+ escalation share).
      - `trend` — delta vs the immediately prior week's persisted misses.
      - `alert` — True iff misses are statistically elevated vs the trailing
        baseline. Self-optimizing: no magic count. Mirrors Ledger's σ detector —
        suspended until ≥ RAMP_UP_WEEKS prior weeks observed, then fires when
        misses ≥ mean + σ·stdev. When the baseline is flat (stdev 0 — e.g. a run
        of clean weeks after the retry-prompt fix lands) any strict increase
        above the mean is elevated, so a regression off a zero baseline is still
        caught.
    """
    events = list(_iter_marker_error_events(outbox_root))
    end = week_ending
    start = end - timedelta(days=7)

    dist = _marker_discipline_window_dist(events, start, end, agent)
    misses = dist.get(1, 0)
    escalated = dist.get(2, 0)  # dispatches that reached retry-2 (⊇ those at 3)
    near_forfeit = dist.get(3, 0)
    total_events = sum(dist.values())
    escalation_rate = (escalated / misses) if misses else 0.0
    near_forfeit_rate = (near_forfeit / misses) if misses else 0.0

    prior = _load_prior_marker_discipline_misses(output_dir, end, baseline_weeks)
    prior_values = list(prior.values())
    weeks_observed = len(prior_values)
    ramp_active = weeks_observed >= MARKER_DISCIPLINE_RAMP_UP_WEEKS
    mean = statistics.fmean(prior_values) if prior_values else 0.0
    stdev = statistics.stdev(prior_values) if len(prior_values) >= 2 else 0.0

    alert = False
    alert_reason: Optional[str] = None
    if ramp_active:
        if stdev > 0.0:
            threshold = mean + MARKER_DISCIPLINE_SIGMA * stdev
            if misses >= threshold:
                alert = True
                alert_reason = (
                    f"{misses} misses ≥ baseline mean {mean:.1f} + "
                    f"{MARKER_DISCIPLINE_SIGMA:.0f}σ ({threshold:.1f}) over "
                    f"{weeks_observed} trailing weeks"
                )
        elif misses > mean:
            # Flat (zero-variance) baseline — typically a run of clean weeks.
            # Any strict increase is a regression worth flagging.
            alert = True
            alert_reason = (
                f"{misses} misses above flat baseline {mean:.1f} "
                f"(zero-variance over {weeks_observed} trailing weeks)"
            )

    prior_week_iso = (end - timedelta(days=7)).date().isoformat()
    trend: Optional[dict[str, Any]] = None
    if prior_week_iso in prior:
        prior_misses = prior[prior_week_iso]
        delta = misses - prior_misses
        trend = {
            "prior_week": prior_week_iso,
            "prior_misses": prior_misses,
            "misses_delta": delta,
            "direction": (
                "up" if delta > 0 else "down" if delta < 0 else "flat"
            ),
        }

    return {
        "agent": agent,
        "window_start": start.date().isoformat(),
        "window_end": end.date().isoformat(),
        "misses": misses,
        "retry_depth_distribution": {
            "1": misses,
            "2": escalated,
            "3": near_forfeit,
        },
        "total_events": total_events,
        "retry_2_plus": escalated,
        "escalation_rate": round(escalation_rate, 4),
        "near_forfeit": near_forfeit,
        "near_forfeit_rate": round(near_forfeit_rate, 4),
        "baseline": {
            "weeks_observed": weeks_observed,
            "baseline_weeks": baseline_weeks,
            "ramp_up_weeks": MARKER_DISCIPLINE_RAMP_UP_WEEKS,
            "sigma": MARKER_DISCIPLINE_SIGMA,
            "mean_misses": round(mean, 2),
            "stdev_misses": round(stdev, 2),
            "active": ramp_active,
        },
        "trend": trend,
        "alert": alert,
        "alert_reason": alert_reason,
    }


# --- proposal synthesis ---


def synthesize_proposals(
    sidecar: dict[str, Any],
    repeats: list[dict[str, Any]],
    marker_discipline: Optional[dict[str, Any]] = None,
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
    # Fixture-pattern allowlist (2026-05-27): drop test-artifact task_ids so a
    # leaked fixture σ outlier can't trigger a hallucinated proposal.
    sigma_hits = [
        a for a in (sidecar.get("anomalies") or [])
        if isinstance(a, dict)
        and a.get("task_id") != "_ramp_up_notice"
        and not is_fixture_task_id(a.get("task_id"))
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

    # preflight-marker-discipline regression — surfaced only when the trailing-
    # window baseline flags misses as statistically elevated. Intentionally NOT
    # auto-dispatch eligible: effort=small but the impact line carries no
    # `$<digit>` token, so it surfaces in the digest for Larry to triage rather
    # than auto-opening a Beacon spec (the retry-prompt fix already owns this
    # area). Per-retry cost is written as a bare USD figure to keep that so.
    if (
        marker_discipline
        and marker_discipline.get("alert")
        and len(proposals) < MAX_PROPOSALS_PER_DIGEST
    ):
        md_dist = marker_discipline.get("retry_depth_distribution", {}) or {}
        md_base = marker_discipline.get("baseline", {}) or {}
        proposals.append({
            "title": "Forge preflight marker-discipline regression",
            "effort": "small",
            "impact": (
                f"{marker_discipline.get('misses', 0)} preflight marker-error "
                f"misses this window (retry-depth "
                f"{md_dist.get('1', 0)}/{md_dist.get('2', 0)}/"
                f"{md_dist.get('3', 0)}); "
                f"{marker_discipline.get('escalation_rate', 0.0) * 100:.0f}% "
                f"escalated to retry-2+ (~0.60 USD per retry re-run + queue "
                f"congestion)"
            ),
            "rationale": (
                f"Forge preflight MalformedForgeMarker miss count is "
                f"statistically elevated vs the trailing "
                f"{md_base.get('weeks_observed', 0)}-week baseline (mean "
                f"{md_base.get('mean_misses', 0.0)}, σ="
                f"{md_base.get('stdev_misses', 0.0)}). "
                f"{marker_discipline.get('alert_reason') or ''}. The retry-"
                f"prompt fix (forge-marker-error-retry-fillin-001) should have "
                f"driven this down — an upward regression means the fix needs a "
                f"re-look or a new failure shape appeared. Audit recent "
                f"`marker-error-*` outbox archives for the dominant parse error."
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


# --- auto-dispatch (closed-loop step 5) ---


def _is_auto_dispatch_eligible(proposal: dict[str, Any]) -> bool:
    """A proposal auto-dispatches iff effort is 'small' or 'medium' AND it states
    a clear (non-empty) impact.

    Widened 2026-06-22 (Larry-approved "open the Pulse dial — more confidence in
    the system + Mirror"). The v1 filter (effort=small AND $-quantified impact)
    deliberately held back medium-effort and non-cost-framed wins; both now flow.
    Still excluded: large-effort changes (Larry's call) and impact-less proposals
    (nothing concrete to act on) — these continue to surface in the digest / park
    to the funnel for triage. The downstream build is unchanged: Beacon drafts a
    spec, and the trust-policy carve-outs + Mirror REVIEW_PASS + classify_careful
    still gate it.
    """
    if proposal.get("effort") not in ("small", "medium"):
        return False
    impact = proposal.get("impact")
    return isinstance(impact, str) and bool(impact.strip())


def _proposal_dedup_key(proposal: dict[str, Any]) -> str:
    """SHA-1 of stable proposal fields. The same recurring proposal across
    Check I runs maps to the same key — different proposals collide only on
    a real content collision (which would still semantically mean "same fix"
    so a single dispatch is correct).
    """
    blob = "␟".join([
        str(proposal.get("title") or ""),
        str(proposal.get("impact") or ""),
        str(proposal.get("rationale") or ""),
    ])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _short_proposal_slug(proposal: dict[str, Any]) -> str:
    """Stable filesystem-safe slug from proposal content (first 10 hex of
    the dedup key). Avoids using the title directly so renames in
    synthesize_proposals don't change the slug shape.
    """
    return _proposal_dedup_key(proposal)[:10]


def _primary_chat_id() -> Optional[int]:
    """Larry's primary Telegram chat — the lowest id in TELEGRAM_ALLOWED_CHAT_IDS
    (mirrors heal_unregistered_approval / outbox_notifier / pulse_check_v
    _primary_chat_id). None only when the allow-list is unset/empty."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    ids = []
    for tok in raw.replace(",", " ").split():
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return min(ids) if ids else None


def _build_dispatch_envelope(
    proposal: dict[str, Any],
    fired_at: datetime,
    sidecar: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Construct the inbox envelope Beacon will receive.

    Beacon's job on arrival is to draft a spec following the standard
    template and emit an APPROVAL_REQUEST marker (step 4 wired the
    extraction). The envelope carries enough context that Beacon doesn't
    need to re-derive the optimization candidate from sidecar files.
    """
    slug = _short_proposal_slug(proposal)
    task_id = f"pulse-auto-{slug}-{fired_at.strftime('%Y%m%d')}"
    title = str(proposal.get("title") or "(untitled proposal)")
    rationale = str(proposal.get("rationale") or "")
    impact = str(proposal.get("impact") or "")
    evidence_lines: list[str] = []
    # Surface a compact slice of the sidecar so Beacon can quote concrete
    # numbers in the spec without round-tripping back to disk.
    if sidecar:
        head_total = sidecar.get("total_usd")
        if head_total is not None:
            evidence_lines.append(f"- Ledger weekly total: ${float(head_total):.2f}")
        anomalies = sidecar.get("anomalies") or []
        if anomalies and isinstance(anomalies[0], dict):
            a = anomalies[0]
            if a.get("task_id") != "_ramp_up_notice":
                evidence_lines.append(
                    f"- Top anomaly: task=`{a.get('task_id')}` "
                    f"cost=${float(a.get('cost_usd', 0.0)):.2f} "
                    f"baseline=${float(a.get('baseline_usd', 0.0)):.2f} "
                    f"σ={float(a.get('sigma_above', 0.0)):.1f}"
                )
    evidence_block = "\n".join(evidence_lines) if evidence_lines else "(no sidecar evidence threaded)"
    prompt = (
        f"{title}\n\n"
        f"Rationale: {rationale}\n\n"
        f"Impact: {impact}\n\n"
        f"Evidence from Ledger sidecar:\n{evidence_block}\n\n"
        f"This is an auto-dispatched optimization candidate from Pulse "
        f"Check I (closed-loop step 5). Read the relevant sidecar / outbox "
        f"archives, then draft a spec following the standard template and "
        f"emit an APPROVAL_REQUEST marker — the trust policy gates whether the "
        f"build auto-starts or asks Larry, and Mirror reviews before any merge."
    )
    envelope: dict[str, Any] = {
        "task_id": task_id,
        "source": "pulse-auto-dispatch",
        "target_agent": "beacon",
        "target_repo": "ourliberty-agent-core",
        "task_type": "feature-development",
        "phase": "preflight",
        "prompt": prompt,
    }
    # Stamp the recipient at creation so the downstream APPROVAL_REQUEST marker
    # carries a real reply_chat_id (#812 null-chat pattern) — omit the key when
    # unresolvable so the notifier's fallback stays intact (never stamp null).
    reply_chat_id = _primary_chat_id()
    if reply_chat_id is not None:
        envelope["reply_chat_id"] = reply_chat_id
    return envelope


def _load_dispatch_state(path: Path) -> dict[str, dict[str, Any]]:
    """Read dedup state. Fail-open on any error — better to over-dispatch
    once than to silently never fire. Returns {dedup_key: {task_id,
    dispatched_at_iso}}.
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[pulse-check-i] WARN: dispatch state read failed ({e}); "
              f"treating as empty")
        return {}


def _save_dispatch_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    """Persist dedup state. Best-effort — failure logs a WARN but does not
    propagate (the dispatch already succeeded; a missed state-write means
    we may re-dispatch next run, which is recoverable).
    """
    try:
        _atomic_write(path, json.dumps(state, indent=2) + "\n")
    except OSError as e:
        print(f"[pulse-check-i] WARN: dispatch state write failed ({e})")


def _is_within_dedup_window(
    entry: dict[str, Any], now: datetime, window_days: int,
) -> bool:
    """True iff entry['dispatched_at'] is within `window_days` of `now`."""
    iso = entry.get("dispatched_at")
    if not isinstance(iso, str):
        return False
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts) < timedelta(days=window_days)


def auto_dispatch_proposals(
    check_i: dict[str, Any],
    fired_at: datetime,
    state_path: Path,
    sidecar: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Dispatch eligible proposals to Beacon's inbox, honoring dedup.

    Returns the list of dispatch records actually written (each
    `{task_id, dedup_key, dispatched_at}`). Skipped proposals (ineligible
    or in dedup window) and write failures (DispatchRejected / RoutingDenied)
    are logged but do not propagate.
    """
    proposals = check_i.get("proposals") or []
    if not proposals:
        return []
    state = _load_dispatch_state(state_path)
    dispatched_now: list[dict[str, Any]] = []
    for p in proposals:
        if not _is_auto_dispatch_eligible(p):
            continue
        key = _proposal_dedup_key(p)
        prior = state.get(key)
        if prior and _is_within_dedup_window(
            prior, fired_at, AUTO_DISPATCH_DEDUP_WINDOW_DAYS,
        ):
            print(f"[pulse-check-i] auto-dispatch dedup skip: "
                  f"key={key[:10]} prior_task={prior.get('task_id')} "
                  f"dispatched_at={prior.get('dispatched_at')}")
            continue
        envelope = _build_dispatch_envelope(p, fired_at, sidecar=sidecar)
        task_id = envelope["task_id"]
        try:
            dest = safe_write_inbox.safe_write_inbox(
                target_agent="beacon",
                task_dict=envelope,
                source_agent="pulse-auto-dispatch",
                filename=f"{task_id}.json",
            )
        except safe_write_inbox.DispatchRejected as e:
            print(f"[pulse-check-i] WARN: auto-dispatch rejected for "
                  f"task={task_id}: {e}")
            continue
        except safe_write_inbox.RoutingDenied as e:
            print(f"[pulse-check-i] WARN: auto-dispatch routing denied for "
                  f"task={task_id}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 — never crash Check I on dispatch
            print(f"[pulse-check-i] WARN: auto-dispatch unexpected error for "
                  f"task={task_id}: {type(e).__name__}: {e}")
            continue
        record = {
            "task_id": task_id,
            "dedup_key": key,
            "dispatched_at": fired_at.isoformat(),
            "dest": str(dest),
        }
        dispatched_now.append(record)
        state[key] = {
            "task_id": task_id,
            "dispatched_at": fired_at.isoformat(),
        }
        print(f"[pulse-check-i] auto-dispatched: task={task_id} "
              f"dest={dest} key={key[:10]}")
    if dispatched_now:
        _save_dispatch_state(state_path, state)
    return dispatched_now


# --- park non-auto-dispatched proposals (Contract B §5.2 / §5.3) ---


def _load_parked_state(path: Path) -> dict[str, dict[str, Any]]:
    """Read parked-proposal dedup state. Fail-open on any error — a missing or
    unreadable state file means "nothing parked yet", which is recoverable (we
    may re-park, never silently suppress). Returns {dedup_key: {capture_id,
    parked_at}}.
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[pulse-check-i] WARN: parked state read failed ({e}); "
              f"treating as empty")
        return {}


def _save_parked_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    """Persist parked-proposal dedup state. Best-effort + atomic, mirroring
    _save_dispatch_state — a failed write logs a WARN but does not propagate
    (the capture already succeeded; a missed state-write means we may re-park
    next cycle, which is recoverable and strictly safer than over-suppressing).
    """
    try:
        _atomic_write(path, json.dumps(state, indent=2) + "\n")
    except OSError as e:
        print(f"[pulse-check-i] WARN: parked state write failed ({e})")


def park_proposals(
    proposals: list[dict[str, Any]],
    fired_at: datetime,
    state_path: Path,
) -> dict[str, dict[str, Any]]:
    """Park each non-auto-dispatched proposal as a durable capture card.

    For every proposal that is NOT auto-dispatch-eligible (the judgment /
    medium-effort ones that re-pitch every cycle), emit a durable capture via
    `emit_capture(label='pulse-check-i')` unless its dedup key already carries a
    recorded capture_id in the parked-state file. On success record
    `{key: {capture_id, parked_at}}`. Best-effort + atomic, mirroring
    `auto_dispatch_proposals`.

    Returns the mapping of keys parked in THIS run (so the DM layer can show
    them once as `[parked]`). NEVER raises and NEVER suppresses on failure: a
    proposal whose park fails records no capture_id, so `apply_park_suppression`
    keeps it in the digest. (Mirror focus: emit failure can never crash Check I;
    suppression strictly requires a recorded capture_id.)
    """
    if not proposals:
        return {}
    state = _load_parked_state(state_path)
    parked_now: dict[str, dict[str, Any]] = {}
    for p in proposals:
        if _is_auto_dispatch_eligible(p):
            continue  # eligible proposals auto-dispatch — never parked
        key = _proposal_dedup_key(p)
        prior = state.get(key)
        if prior and prior.get("capture_id"):
            continue  # already parked in a prior cycle — silence earned
        title = str(p.get("title") or "(untitled proposal)")
        note = f"{p.get('impact') or ''}\n\n{p.get('rationale') or ''}".strip()
        try:
            capture_id = emit_capture(
                title=title,
                note=note,
                source="agent",
                label="pulse-check-i",
            )
        except Exception as e:  # noqa: BLE001 — park must never crash Check I
            print(f"[pulse-check-i] WARN: park raised for key={key[:10]} "
                  f"({type(e).__name__}: {e}); leaving proposal in DM")
            continue
        if not capture_id:
            print(f"[pulse-check-i] park failed (no capture_id) key={key[:10]} "
                  f"title={title!r}; leaving proposal in DM")
            continue
        record = {"capture_id": capture_id, "parked_at": fired_at.isoformat()}
        state[key] = record
        parked_now[key] = record
        print(f"[pulse-check-i] parked: key={key[:10]} "
              f"capture_id={capture_id} title={title!r}")
    if parked_now:
        _save_parked_state(state_path, state)
    return parked_now


def apply_park_suppression(
    check_i: dict[str, Any],
    parked_now: dict[str, dict[str, Any]],
    parked_state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Suppress parked proposals' DM lines (Contract B §5.3), mutating check_i.

    Each non-auto-dispatch proposal is reclassified by its dedup key:
      - parked in THIS run (key in `parked_now` with a capture_id) → annotated
        `parked=True` and shown ONCE as `[parked] … — see dashboard Parked lane`.
      - parked in a PRIOR run (capture_id in `parked_state`, not `parked_now`) →
        omitted from the digest entirely (silence earned by durable capture).
      - not parked (park failed → no capture_id) → left untouched, still DM'd in
        full. Suppression STRICTLY requires a recorded capture_id.

    Auto-dispatch-eligible proposals and the Ledger headline are unaffected.
    `mode`/`has_signal` are recomputed so an all-parked digest with no fresh
    signal collapses toward heartbeat/no-signal instead of re-pitching.
    """
    parked_now = parked_now or {}
    parked_state = parked_state or {}
    if check_i.get("mode") == "skipped":
        return check_i
    if not parked_now and not parked_state:
        return check_i  # nothing ever parked — preserve today's behavior

    proposals = check_i.get("proposals") or []
    kept: list[dict[str, Any]] = []
    for p in proposals:
        if _is_auto_dispatch_eligible(p):
            kept.append(p)
            continue
        key = _proposal_dedup_key(p)
        now_rec = parked_now.get(key)
        if now_rec and now_rec.get("capture_id"):
            annotated = dict(p)
            annotated["parked"] = True
            annotated["capture_id"] = now_rec["capture_id"]
            kept.append(annotated)
            continue
        prior = parked_state.get(key)
        if prior and prior.get("capture_id"):
            continue  # parked in a prior cycle → omit from this digest
        kept.append(p)  # not parked (e.g. park failed) → keep it in the DM
    check_i["proposals"] = kept

    sigs = check_i.get("engineering_signals") or {}
    real_anoms = sigs.get("sigma_anomalies") or []
    repeats = sigs.get("high_repeat_tasks") or []
    overhead_pct = float(sigs.get("retry_overhead_pct", 0.0) or 0.0)
    md = sigs.get("marker_discipline")
    md_alert = bool(isinstance(md, dict) and md.get("alert"))
    has_signal = bool(
        kept or real_anoms or repeats
        or overhead_pct >= RETRY_OVERHEAD_PCT_THRESHOLD
        or md_alert
    )
    check_i["has_signal"] = has_signal
    if kept:
        check_i["mode"] = "digest"
    elif has_signal:
        check_i["mode"] = "heartbeat"
    else:
        check_i["mode"] = "no-signal"
    return check_i


# --- manual dispatch (Larry-driven /dispatch <N>) ---


def _find_latest_audit(output_dir: Path) -> Optional[Path]:
    """Return the most-recently-modified `check-i-*.json` in output_dir, or
    None if the directory is empty / missing. Manual dispatch defaults to
    this when --audit isn't supplied.
    """
    if not output_dir.is_dir():
        return None
    candidates = sorted(
        output_dir.glob("check-i-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def manual_dispatch_proposal(
    audit_path: Path,
    n: int,
    state_path: Path,
    fired_at: datetime,
    sidecar: Optional[dict[str, Any]] = None,
) -> int:
    """Dispatch proposal #N (1-indexed) from the audit JSON at audit_path.

    Bypasses _is_auto_dispatch_eligible — Larry's explicit /dispatch is the
    gate. Dedup hits log a WARN and proceed (manual override). Returns 0 on
    successful dispatch, 1 on any error (missing file, out-of-range N,
    safe_write_inbox failure).
    """
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            audit = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[pulse-check-i] manual-dispatch ERROR: cannot read audit "
              f"{audit_path}: {e}")
        return 1

    proposals = audit.get("proposals") or []
    if n < 1 or n > len(proposals):
        print(f"[pulse-check-i] manual-dispatch ERROR: proposal N={n} out "
              f"of range. Audit {audit_path} has {len(proposals)} "
              f"proposal(s); valid N is 1..{len(proposals) or 0}.")
        return 1

    proposal = proposals[n - 1]
    envelope = _build_dispatch_envelope(proposal, fired_at, sidecar=sidecar)
    task_id = envelope["task_id"]
    key = _proposal_dedup_key(proposal)

    state = _load_dispatch_state(state_path)
    prior = state.get(key)
    if prior and _is_within_dedup_window(
        prior, fired_at, AUTO_DISPATCH_DEDUP_WINDOW_DAYS,
    ):
        print(f"[pulse-check-i] manual-dispatch WARN: proposal previously "
              f"dispatched at {prior.get('dispatched_at')} as task_id "
              f"{prior.get('task_id')}; manual override proceeding.")

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent="beacon",
            task_dict=envelope,
            source_agent="pulse-auto-dispatch",
            filename=f"{task_id}.json",
        )
    except safe_write_inbox.DispatchRejected as e:
        print(f"[pulse-check-i] manual-dispatch ERROR: rejected for "
              f"task={task_id}: {e}")
        return 1
    except safe_write_inbox.RoutingDenied as e:
        print(f"[pulse-check-i] manual-dispatch ERROR: routing denied for "
              f"task={task_id}: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[pulse-check-i] manual-dispatch ERROR: unexpected for "
              f"task={task_id}: {type(e).__name__}: {e}")
        return 1

    state[key] = {
        "task_id": task_id,
        "dispatched_at": fired_at.isoformat(),
    }
    _save_dispatch_state(state_path, state)
    title = str(proposal.get("title") or "(untitled proposal)")
    print(f"[pulse-check-i] manual-dispatch: proposal N={n} [{title}] "
          f"→ beacon inbox {dest}")
    return 0


# --- digest assembly ---


def _round2(x: float) -> float:
    return round(x, 2)


def assemble_check_i(
    sidecar: Optional[dict[str, Any]],
    repeats: list[dict[str, Any]],
    week_ending: str,
    sidecar_filename: Optional[str],
    fired_at: datetime,
    marker_discipline: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return the structured Check I result.

    Four modes:
      - `skipped` — sidecar unavailable; no DM, just a journal note.
      - `no-signal` — sidecar present, no proposals, no anomalies, no
        repeats, retry overhead below threshold. Scheduled runs suppress
        the DM (closed-loop spec § 4); `--force` (/optimize) still DMs.
      - `heartbeat` — sidecar present, no proposals, but some signal
        present (anomalies / repeats / elevated retry overhead). DM
        still fires so Larry sees the underlying signal.
      - `digest` — sidecar + proposals; full DM.

    `has_signal` is included in the returned dict so the DM gate at the
    call site can decide whether to suppress without re-deriving it.
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
            "has_signal": False,
        }

    total_usd = float(sidecar.get("total_usd", 0.0) or 0.0)
    delta = sidecar.get("delta_vs_prior_week")
    raw_anoms = sidecar.get("anomalies") or []
    # Fixture-pattern allowlist (2026-05-27): the headline count + the
    # sigma_anomalies list both feed has_signal and the journal block; a
    # fixture leak in either would falsely flag the cycle as actionable.
    real_anoms = [
        a for a in raw_anoms
        if isinstance(a, dict)
        and a.get("task_id") != "_ramp_up_notice"
        and not is_fixture_task_id(a.get("task_id"))
    ]
    retry_overhead = sidecar.get("retry_overhead", {}) or {}
    overhead_pct = float(retry_overhead.get("percent_of_total", 0.0) or 0.0)
    overhead_usd = float(retry_overhead.get("total_retry_cost_usd", 0.0) or 0.0)

    proposals = synthesize_proposals(sidecar, repeats, marker_discipline)
    md_alert = bool(marker_discipline and marker_discipline.get("alert"))
    has_signal = bool(
        proposals or real_anoms or repeats
        or overhead_pct >= RETRY_OVERHEAD_PCT_THRESHOLD
        or md_alert
    )
    if proposals:
        mode = "digest"
    elif has_signal:
        mode = "heartbeat"
    else:
        mode = "no-signal"

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
            "marker_discipline": marker_discipline,
        },
        "proposals": proposals,
        "has_signal": has_signal,
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

    if mode == "no-signal":
        return (
            f"🩺 Pulse Check I (week of {week}): no signal — "
            f"no proposals, no anomalies, no high-repeat tasks, "
            f"retry overhead within bounds. "
            f"Ledger total ${_round2(total_usd):.2f}{delta_phrase}."
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
    md = sigs.get("marker_discipline")
    if isinstance(md, dict) and md.get("alert"):
        base = md.get("baseline", {}) or {}
        lines.append(
            f"Forge marker-discipline ELEVATED: {md.get('misses', 0)} misses, "
            f"{md.get('escalation_rate', 0.0) * 100:.0f}% retry-2+ "
            f"(baseline mean {base.get('mean_misses', 0.0)})."
        )
    lines.append("")
    lines.append(f"Proposed optimizations ({len(check_i['proposals'])}):")
    for i, p in enumerate(check_i["proposals"], 1):
        if p.get("parked"):
            lines.append(
                f"  {i}. [parked] {p['title']} — see dashboard Parked lane"
            )
        else:
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

    md = sigs.get("marker_discipline")
    if isinstance(md, dict):
        dist = md.get("retry_depth_distribution", {}) or {}
        flag = " [ELEVATED]" if md.get("alert") else ""
        trend = md.get("trend") or {}
        trend_phrase = (
            f", trend {trend.get('direction')} "
            f"({trend.get('misses_delta'):+d} vs prior wk)"
            if trend else ""
        )
        lines.append(
            f"- Forge marker-discipline: {md.get('misses', 0)} misses "
            f"(retry-depth {dist.get('1', 0)}/{dist.get('2', 0)}/"
            f"{dist.get('3', 0)}, "
            f"{md.get('escalation_rate', 0.0) * 100:.0f}% retry-2+)"
            f"{trend_phrase}{flag}"
        )

    if mode == "no-signal":
        lines.append("- Mode: no-signal — DM suppressed (scheduled run)")
        return "\n".join(lines)

    if mode == "heartbeat":
        lines.append("- Mode: heartbeat (no proposed optimizations)")
        return "\n".join(lines)

    lines.append(f"- Mode: digest — {len(check_i['proposals'])} proposal(s):")
    for i, p in enumerate(check_i["proposals"], 1):
        if p.get("parked"):
            lines.append(
                f"  {i}. [parked] {p['title']} — see dashboard Parked lane "
                f"(capture {p.get('capture_id')})"
            )
        else:
            lines.append(
                f"  {i}. [{p['effort']}] {p['title']} — {p['impact']}"
            )
            lines.append(f"     Rationale: {p['rationale']}")
    return "\n".join(lines)


# Identifies a `**Check I (YYYY-MM-DD):**` header line so append_journal can
# skip re-writing a block whose week is already in the journal (a single week
# is hit by the scheduled /cycle, on-demand /optimize, and manual re-runs).
_CHECK_I_HEADER_RE = re.compile(
    r"^\*\*Check I \((\d{4}-\d{2}-\d{2})\):\*\*", re.MULTILINE
)


def append_journal(journal_path: Path, block: str) -> None:
    """Append a journal block, idempotent per Check I week marker.

    If ``block`` carries a ``**Check I (YYYY-MM-DD):**`` header and the
    journal already contains that week's header, skip the write so the same
    week never stacks duplicate blocks. Blocks without a parseable Check I
    header always append (other callers are unaffected).
    """
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    m = _CHECK_I_HEADER_RE.search(block)
    if m and journal_path.exists():
        week = m.group(1)
        existing = journal_path.read_text(encoding="utf-8")
        if week in _CHECK_I_HEADER_RE.findall(existing):
            print(
                f"[pulse-check-i] journal: skipped \u2014 block for {week} "
                "already present",
                file=sys.stderr,
            )
            return
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
        help="Skip the Mon/Wed/Fri/Sun weekday gate. Used by /optimize.",
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
    p.add_argument(
        "--no-auto-dispatch",
        action="store_true",
        help="Skip the auto-dispatch step (test / dry-run).",
    )
    p.add_argument(
        "--dispatch-state-file",
        default=str(DEFAULT_DISPATCH_STATE_FILE),
        help="Dedup state file for auto-dispatch (closed-loop step 5).",
    )
    p.add_argument(
        "--no-park",
        action="store_true",
        help="Skip parking non-auto-dispatched proposals (Contract B). "
             "Test / dry-run.",
    )
    p.add_argument(
        "--parked-state-file",
        default=str(DEFAULT_PARKED_STATE_FILE),
        help="Dedup state file for parked proposals (Contract B §5.2).",
    )
    p.add_argument(
        "--dispatch",
        type=int,
        metavar="N",
        help="Manual dispatch: send proposal N (1-indexed) from the most "
             "recent audit JSON to Beacon's inbox. Bypasses the small-effort "
             "eligibility gate and dedup window — Larry's /dispatch is the "
             "gate.",
    )
    p.add_argument(
        "--audit",
        help="Override the audit JSON path for --dispatch. Defaults to the "
             "most-recently-modified `check-i-*.json` in --output-dir.",
    )
    args = p.parse_args(argv)

    if args.dispatch is not None:
        output_dir = Path(args.output_dir)
        if args.audit:
            audit_path = Path(args.audit)
        else:
            latest = _find_latest_audit(output_dir)
            if latest is None:
                print(f"[pulse-check-i] manual-dispatch ERROR: no audit JSON "
                      f"found in {output_dir}. Run /optimize first to "
                      f"produce one.")
                return 1
            audit_path = latest
        now = datetime.now(timezone.utc)
        return manual_dispatch_proposal(
            audit_path=audit_path,
            n=args.dispatch,
            state_path=Path(args.dispatch_state_file),
            fired_at=now,
        )

    halt_flag = Path(args.halt_flag)
    if halt_flag.exists():
        print(f"[pulse-check-i] EMERGENCY_HALT present at {halt_flag}; "
              f"skipping run.")
        return 0

    now = datetime.now(timezone.utc)
    if args.week_ending:
        try:
            week_ending_dt = datetime.fromisoformat(args.week_ending).replace(
                tzinfo=timezone.utc
            )
        except ValueError as e:
            # A malformed --week-ending is operator input error, not a check
            # failure: exit cleanly with a usage message instead of letting the
            # ValueError escape main() and mis-fire a pulse-check-failed:i alert
            # (audit #29).
            print(f"invalid --week-ending {args.week_ending!r}: {e}; "
                  f"expected ISO date YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        week_ending_dt = _default_week_ending(now)

    # Spec § 6: fires on Mon/Wed/Fri/Sun cycles unless forced (`/optimize`).
    if (
        not args.force
        and not args.week_ending
        and now.weekday() not in CHECK_I_FIRING_WEEKDAYS
    ):
        print(f"[pulse-check-i] today is not in (Mon/Wed/Fri/Sun) "
              f"(weekday={now.weekday()}); skipping. "
              f"Use --force or /optimize for ad-hoc runs.")
        return 0

    week_ending = week_ending_dt.date().isoformat()
    sidecar_dir = Path(args.sidecar_dir)
    outbox_root = Path(args.outbox_root)
    output_dir = Path(args.output_dir)
    journal_path = Path(args.journal)

    sidecar = _load_sidecar(sidecar_dir, week_ending)
    age_hours = _sidecar_age_hours(sidecar_dir, week_ending, now=now)

    # Auto-refresh: missing or >24h old → invoke run_ledger.sh, then re-read.
    # The 7d stale-skip below still applies against the (possibly refreshed)
    # sidecar, so a refresh that fails leaves the existing logic intact.
    if sidecar is None or (age_hours is not None
                           and age_hours > SIDECAR_REFRESH_AGE_HOURS):
        _refresh_sidecar()
        sidecar = _load_sidecar(sidecar_dir, week_ending)
        age_hours = _sidecar_age_hours(sidecar_dir, week_ending, now=now)

    sidecar_filename = (
        f"weekly-{week_ending}.json" if sidecar is not None else None
    )

    # Stale check: spec § 6, sidecar > 7 days old → skip.
    if sidecar is not None:
        if age_hours is not None and age_hours > SIDECAR_MAX_AGE_DAYS * 24:
            sidecar = None
            sidecar_filename = None

    repeats = gather_retry_repeats(outbox_root)
    marker_discipline = compute_marker_discipline(
        outbox_root=outbox_root,
        week_ending=week_ending_dt,
        output_dir=output_dir,
    )
    check_i = assemble_check_i(
        sidecar=sidecar,
        repeats=repeats,
        week_ending=week_ending,
        sidecar_filename=sidecar_filename,
        fired_at=now,
        marker_discipline=marker_discipline,
    )

    # Contract B (park-the-nudge §5.2/§5.3): park each non-auto-dispatched
    # proposal as a durable capture, then suppress its DM line once a
    # capture_id is recorded. Park BEFORE the audit/DM render so a freshly
    # recorded capture_id is honored this cycle. Wrapped defensively — a park
    # failure must never crash Check I (the proposal simply stays in the DM).
    parked_state_path = Path(args.parked_state_file)
    parked_now: dict[str, dict[str, Any]] = {}
    if not args.no_park:
        try:
            parked_now = park_proposals(
                check_i.get("proposals") or [], now, parked_state_path,
            )
        except Exception as e:  # noqa: BLE001 — park must never crash Check I
            print(f"[pulse-check-i] WARN: park step crashed "
                  f"({type(e).__name__}: {e}); continuing")
    parked_state = _load_parked_state(parked_state_path)
    apply_park_suppression(check_i, parked_now, parked_state)

    # Audit filename uses firing date so the 4 weekly firings each get
    # their own record. Sidecar lookup above still uses week_ending —
    # Ledger remains weekly Monday.
    firing_date = now.date().isoformat()
    out_path = output_dir / f"check-i-{firing_date}.json"
    _atomic_write(out_path, json.dumps(check_i, indent=2) + "\n")

    dm_body = render_dm(check_i)
    dm_result = "skipped (--no-dm)"
    # Closed-loop spec § 4: on scheduled runs with no signal, skip the DM
    # but still write audit JSON + journal entry. /optimize (--force)
    # bypasses suppression so on-demand callers always get a reply.
    suppress_dm = (
        check_i.get("mode") != "skipped"
        and not check_i.get("has_signal", True)
        and not args.force
    )
    if args.no_dm:
        pass
    elif suppress_dm:
        dm_result = "suppressed (no signal, scheduled run)"
    else:
        # Route the DM so the same weekly digest isn't DM'd 4-5×/week. The
        # predicate mirrors append_journal's dedup exactly: is this week's
        # `**Check I (YYYY-MM-DD):**` header already in the journal? The
        # journal is written AFTER this DM, so the first scheduled run of a
        # week sees the week absent → escalate (DM now), and every later
        # same-week scheduled run sees it present → digest (no DM; the daily
        # CEO digest still surfaces it).
        #   - --force (on-demand /optimize): always escalate — Larry expects
        #     a reply regardless of journal state.
        #   - --no-journal / missing journal / read failure: escalate
        #     (fail-loud — a routing slip must over-notify, never drop).
        dm_route = "escalate"
        if not args.force and not args.no_journal:
            try:
                if journal_path.exists():
                    journal_text = journal_path.read_text(encoding="utf-8")
                    if week_ending in _CHECK_I_HEADER_RE.findall(journal_text):
                        dm_route = "digest"
            except Exception as e:  # noqa: BLE001 — never silently suppress
                print(f"[pulse-check-i] WARN: journal read for DM route "
                      f"crashed ({type(e).__name__}: {e}); routing escalate")
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import larry_alerts  # type: ignore
            ok = larry_alerts.append_alert(
                source="pulse",
                severity="warning",
                message=dm_body,
                subject=f"check-i-{week_ending}",
                route=dm_route,
            )
            dm_result = f"queued (route={dm_route})" if ok else (
                "cooldown-suppressed or write failed"
            )
        except Exception as e:  # noqa: BLE001
            dm_result = f"larry_alerts unavailable ({e})"

    if not args.no_journal:
        block = render_journal_block(check_i)
        append_journal(journal_path, block)

    # Closed-loop step 5 — auto-dispatch eligible proposals to Beacon's
    # inbox. Wrapped in a try/except defensively: even an unhandled error
    # here must not regress the digest path. Eligibility is narrow (small
    # effort + $-quantified); ineligible proposals continue to surface
    # via the digest DM for Larry to triage.
    dispatched: list[dict[str, Any]] = []
    if not args.no_auto_dispatch:
        try:
            dispatched = auto_dispatch_proposals(
                check_i=check_i,
                fired_at=now,
                state_path=Path(args.dispatch_state_file),
                sidecar=sidecar,
            )
        except Exception as e:  # noqa: BLE001 — Check I must never crash
            print(f"[pulse-check-i] WARN: auto-dispatch step crashed "
                  f"({type(e).__name__}: {e}); continuing")

    print(f"[pulse-check-i] mode={check_i['mode']}")
    print(f"[pulse-check-i] parked: {len(parked_now)}")
    print(f"[pulse-check-i] wrote {out_path}")
    print(f"[pulse-check-i] DM: {dm_result}")
    if not args.no_journal:
        print(f"[pulse-check-i] journal: appended to {journal_path}")
    print(f"[pulse-check-i] auto-dispatched: {len(dispatched)}")
    return 0


if __name__ == "__main__":
    from pulse_check_heartbeat import run_check
    raise SystemExit(run_check("i", main))
