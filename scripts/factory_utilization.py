#!/usr/bin/env python3
"""factory_utilization.py — the operator layer's headline KPI: how busy is the
factory, and what is the current constraint (Operator Feed Loop slice 6b).

WHY THIS EXISTS
---------------
The goal is a factory building 24/7 within tier capacity. The one number that
tells Larry whether the system is living up to that — and WHICH lever to pull
when it is not — is utilization over the trailing window, joined with whether
the shelf has ranked work waiting:

  idle + shelf has ranked work  -> SUPPLY constraint (the feed loop / autonomy
                                   width is the bottleneck, not capacity)
  saturated + backlog sustained -> CAPACITY constraint (the readiness trip-wire
                                   territory: consider adding a tier)
  idle + shelf empty            -> no constraint; the pipeline is honestly drained

readiness_trip_wire.py already samples backlog/active/cap every 15 minutes but
keeps only a 50-tick rolling history (~12.5h) — enough for its sustained-fire
rule, useless for a weekly utilization read. This pass merges each trip-wire
tick into its OWN durable ledger (bounded at ~31 days) and computes busy%,
mean concurrency, and the constraint diagnosis over 24h/7d/30d windows.

READ-ONLY over its inputs (readiness-trip-wire.json, mission-rank.json); writes
only its own state file + log. No LLM calls, no gh calls, no network. The
system-effectiveness pulse (slice 7) is the intended consumer; the dashboard
can read the same file later.

Discipline (mirrors mission_staleness): stdlib only, never raises — a missing
or malformed input degrades to an "unknown" diagnosis, WARN-logged. Atomic
state swap so readers never see a half-written file.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HOME = Path(os.environ.get('HOME', '/home/larry'))
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
TRIP_WIRE_FILE = AGENTS_ROOT / 'state' / 'readiness-trip-wire.json'
RANK_FILE = AGENTS_ROOT / 'state' / 'mission-rank.json'
STATE_FILE = AGENTS_ROOT / 'state' / 'factory-utilization.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'factory-utilization.log'

# ~31 days of 15-minute ticks. Bounds the ledger so the state file never grows
# unbounded (same discipline as the trip-wire's own HISTORY_MAX).
TICKS_MAX = 3000

WINDOWS_SEC = {'24h': 24 * 3600, '7d': 7 * 86400, '30d': 30 * 86400}

# Diagnosis thresholds (24h window). MIN_TICKS guards a cold ledger — never
# diagnose from a handful of samples. busy = any active dispatch that tick;
# saturated = at cap AND work queued behind it (mirrors the trip-wire's
# saturation arm, minus the quota arm it owns).
MIN_TICKS = 8
BUSY_PCT_LOW = 0.5
SATURATED_PCT_HIGH = 0.25


def _log(level: str, msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {level} {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        print(line, file=sys.stderr)


def _read_json(path: Path) -> Optional[Any]:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _num(v: Any, default: float = 0.0) -> float:
    # NaN/Infinity survive json.load and would poison means AND re-serialize
    # as non-strict JSON the dashboard's JSON.parse would choke on.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    if not math.isfinite(v):
        return default
    return float(v)


def _parse_ts(v: Any) -> Optional[float]:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v).timestamp()
    except (ValueError, OverflowError, OSError):
        # ValueError covers everything fromisoformat rejects on this box;
        # OverflowError/OSError make never-raises platform-independent for
        # naive extreme dates (.timestamp() goes through the local tz).
        return None


def sanitize_tick(raw: Any) -> Optional[dict[str, Any]]:
    """One trip-wire history row -> the minimal ledger tick, or None to skip."""
    if not isinstance(raw, dict):
        return None
    if _parse_ts(raw.get('ts')) is None:
        return None
    return {
        'ts': raw['ts'],
        'active': _num(raw.get('concurrency_active')),
        'cap': _num(raw.get('cap')),
        'backlog': _num(raw.get('backlog_depth')),
    }


def _valid_ledger_tick(t: Any) -> Optional[dict[str, Any]]:
    """One row already in ledger form -> validated, or None to drop. Guards a
    hand-edited / corrupt state file: every kept row has a parseable ts and
    numeric fields, so downstream never KeyErrors."""
    if not isinstance(t, dict) or _parse_ts(t.get('ts')) is None:
        return None
    return {
        'ts': t['ts'],
        'active': _num(t.get('active')),
        'cap': _num(t.get('cap')),
        'backlog': _num(t.get('backlog')),
    }


# A tick stamped further than this into the future is corrupt (hand-edit /
# clock skew). Without the guard it would sort newest forever — immortal in
# the TICKS_MAX bound and counted in every window.
FUTURE_SLACK_SEC = 3600


def merge_ticks(ledger: list[Any], incoming: list[dict],
                now_ts: Optional[float] = None) -> list[dict]:
    """Union by ts (incoming wins), chronological, bounded at TICKS_MAX
    (oldest dropped). `incoming` rows are already sanitized trip-wire ticks;
    `ledger` rows are re-validated on every pass; future-dated rows dropped."""
    horizon = (now_ts + FUTURE_SLACK_SEC) if now_ts is not None else None
    by_ts: dict[str, dict] = {}
    for t in ledger:
        clean = _valid_ledger_tick(t)
        if clean is not None:
            by_ts[clean['ts']] = clean
    for t in incoming:
        by_ts[t['ts']] = t
    kept = (t for t in by_ts.values()
            if horizon is None or (_parse_ts(t['ts']) or 0.0) <= horizon)
    ordered = sorted(kept, key=lambda t: _parse_ts(t['ts']) or 0.0)
    return ordered[-TICKS_MAX:]


def window_stats(ticks: list[dict], now_ts: float, span_sec: int) -> dict[str, Any]:
    inside = [t for t in ticks
              if (_parse_ts(t.get('ts')) or 0.0) >= now_ts - span_sec]
    n = len(inside)
    if n == 0:
        return {'ticks': 0, 'busy_pct': None, 'saturated_pct': None,
                'mean_active': None, 'mean_cap': None}
    busy = sum(1 for t in inside if _num(t.get('active')) > 0)
    saturated = sum(
        1 for t in inside
        if _num(t.get('cap')) > 0
        and _num(t.get('active')) >= _num(t.get('cap'))
        and _num(t.get('backlog')) > 0)
    return {
        'ticks': n,
        'busy_pct': round(busy / n, 3),
        'saturated_pct': round(saturated / n, 3),
        'mean_active': round(sum(_num(t.get('active')) for t in inside) / n, 2),
        'mean_cap': round(sum(_num(t.get('cap')) for t in inside) / n, 2),
    }


def shelf_ranked_count() -> Optional[int]:
    """How much ranked, actionable work sits on the shelf (mission-rank.json).
    None = unknown (file absent/malformed) — diagnosis degrades, never guesses."""
    data = _read_json(RANK_FILE)
    if not isinstance(data, dict):
        return None
    ranked = data.get('ranked')
    return len(ranked) if isinstance(ranked, list) else None


def diagnose(day: dict[str, Any], shelf: Optional[int]) -> tuple[str, str]:
    """(constraint, plain reason) from the 24h window + shelf depth."""
    n = day.get('ticks') or 0
    if n < MIN_TICKS:
        return ('unknown',
                f'only {n} tick(s) in the last 24h — not enough history to judge')
    busy = day['busy_pct'] or 0.0
    saturated = day['saturated_pct'] or 0.0
    if saturated >= SATURATED_PCT_HIGH:
        return ('capacity',
                f'at cap with a queue behind it {saturated:.0%} of the last 24h '
                f'— capacity is the constraint (trip-wire territory)')
    if busy < BUSY_PCT_LOW and shelf is None:
        return ('unknown',
                f'factory busy only {busy:.0%} of the last 24h but the ranked '
                f'shelf is unreadable — cannot tell supply from drained')
    if busy < BUSY_PCT_LOW and shelf and shelf > 0:
        return ('supply',
                f'factory busy only {busy:.0%} of the last 24h while {shelf} '
                f'ranked card(s) wait on the shelf — feeding work is the '
                f'constraint, not capacity')
    if busy < BUSY_PCT_LOW:
        return ('drained',
                f'factory busy {busy:.0%} of the last 24h and the ranked shelf '
                f'is empty — nothing queued, honestly idle')
    return ('balanced',
            f'factory busy {busy:.0%} of the last 24h without a sustained '
            f'queue behind the cap — no constraint binding')


def run_once(now: Optional[datetime] = None) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()

    prior = _read_json(STATE_FILE)
    ledger = prior.get('ticks') if isinstance(prior, dict) else None
    ledger = ledger if isinstance(ledger, list) else []

    trip = _read_json(TRIP_WIRE_FILE)
    history = trip.get('history') if isinstance(trip, dict) else None
    incoming = [t for t in (sanitize_tick(r) for r in history or [])
                if t is not None]
    if not incoming and not ledger:
        _log('WARN', 'no trip-wire history and empty ledger; writing unknown state')

    ticks = merge_ticks(ledger, incoming, now_ts=now_ts)
    windows = {name: window_stats(ticks, now_ts, span)
               for name, span in WINDOWS_SEC.items()}
    shelf = shelf_ranked_count()
    constraint, reason = diagnose(windows['24h'], shelf)

    state = {
        'as_of': now_dt.isoformat(),
        'ticks': ticks,
        'computed': {
            'windows': windows,
            'shelf_ranked': shelf,
            'constraint': constraint,
            'reason': reason,
        },
    }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=1)
        tmp.replace(STATE_FILE)  # atomic swap, never a half-written file
    except OSError as e:
        _log('WARN', f'could not write state file: {e}')
    _log('INFO',
         f"pass: ticks={len(ticks)} 24h={windows['24h']} "
         f"shelf={shelf} constraint={constraint}")
    return state


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--once':
        run_once()
        sys.exit(0)
    print('usage: factory_utilization.py --once', file=sys.stderr)
    sys.exit(2)
