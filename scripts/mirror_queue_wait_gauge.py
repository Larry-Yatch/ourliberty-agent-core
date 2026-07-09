#!/usr/bin/env python3
"""mirror_queue_wait_gauge.py — "do we need a THIRD Mirror review slot?" gauge.

PURPOSE
-------
A deterministic, self-firing oneshot (systemd timer, ~hourly) that DMs Larry
when Mirror's *burst* review queue-wait stays high EVEN WITH two slots active —
the signal that a third review slot (or a per-review service-time cut) is now
worth its cost. Sibling to readiness_trip_wire.py (which asks the different
question "do we need a second BUILD team?"). They are intentionally SEPARATE:
the readiness trip-wire is an all-must-hold AND over forge-backlog +
concurrency-saturation + rate-limit pressure, scoped to build capacity. Grafting
a Mirror-review queue-wait term into that conjunction would corrupt its
semantics (a review backlog is not a build backlog, and neither is a subset of
the other). Per spec mirror-two-slot-review §4 PR3 this is the "or a sibling
gauge" branch.

SIGNAL SOURCE
-------------
The local jsonl ledger written by inbox_watcher.emit_review_queue_wait at every
Mirror review-start:
    ~/agents/blackboard/mirror-queue-wait.jsonl
Each row: {ts, task_id, pr_url, review_slot, queue_wait_sec}. The chain_events
copy of the same sample is the dashboard/analytics view; this gauge reads the
local ledger so it never depends on Supabase reachability.

THE FIRING RULE (and why each clause is necessary)
--------------------------------------------------
FIRE when, over the recent window:
    sample_count >= MIN_SAMPLES  AND  p95(queue_wait_sec) >= P95_THRESHOLD_SEC

  - MIN_SAMPLES: a single slow PR must not fire — we need a real burst of
    reviews to have queued for the p95 to mean anything. Below this the window
    is too thin to trust.
  - p95 (not mean/max): the success metric (§8) is burst tail-latency, and p95
    is robust to one outlier while still catching a sustained wall. With two
    slots the §8 target is a 5-PR burst clearing < 2h end-to-end; a p95
    review-start wait creeping toward ~90m means the two slots are themselves
    saturating during bursts — the point of the gauge.

Plus a self-managed multi-day FIRE_COOLDOWN_SEC (on top of larry_alerts' own
warning cooldown) so a persistently-hot window re-DMs at most every few days,
never every tick. A kill switch (~/agents/healers.disabled) hard-disables it.

This gauge only READS the ledger + its own state file; the one file it writes is
its state (last_fired_at). No dispatch, no automatic slot change — it is a
readiness SIGNAL, exactly like readiness_trip_wire.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# -------------------- paths (resolved at call time, honor sandbox) --------------------

def agents_root() -> Path:
    """Agents runtime root. Honors OURLIBERTY_AGENTS_ROOT (test bootstrap /
    sandbox); defaults to ~/agents. Resolved at call time so a test redirect
    lands rather than being frozen at import."""
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def ledger_path() -> Path:
    return agents_root() / 'blackboard' / 'mirror-queue-wait.jsonl'


def state_file() -> Path:
    return agents_root() / 'state' / 'mirror-queue-wait-gauge.json'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def log_file() -> Path:
    return agents_root() / 'logs' / 'mirror-queue-wait-gauge.log'


# -------------------- thresholds (module constants, env-overridable) --------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log(f'{name}={raw!r} is not an integer; using default {default}', 'WARN')
        return default


# Lookback window (hours) over which samples are gathered. 24h captures a full
# day's burst pattern. Env: OURLIBERTY_MIRROR_QW_WINDOW_HOURS.
WINDOW_HOURS = _env_int('OURLIBERTY_MIRROR_QW_WINDOW_HOURS', 24)

# Need at least this many review samples in the window before the p95 means
# anything (a burst, not one slow PR). Env: OURLIBERTY_MIRROR_QW_MIN_SAMPLES.
MIN_SAMPLES = _env_int('OURLIBERTY_MIRROR_QW_MIN_SAMPLES', 5)

# p95 queue-wait (seconds) at/above which two slots look saturated and a third
# is worth considering. 90 min default — with two slots the §8 target is a 5-PR
# burst clearing < 2h end-to-end, so a p95 *start*-wait near 90m is the warning
# band. Env: OURLIBERTY_MIRROR_QW_P95_SEC.
P95_THRESHOLD_SEC = _env_int('OURLIBERTY_MIRROR_QW_P95_SEC', 90 * 60)

# Self-managed cooldown (seconds) between fires. 3 days default. Env:
# OURLIBERTY_MIRROR_QW_COOLDOWN_SEC.
FIRE_COOLDOWN_SEC = _env_int('OURLIBERTY_MIRROR_QW_COOLDOWN_SEC', 3 * 24 * 3600)

ALERT_SOURCE = 'mirror-queue-wait-gauge'
ALERT_SUBJECT = 'third-review-slot-readiness'


# -------------------- logging --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, file=sys.stderr, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


# -------------------- kill switch + state --------------------

def kill_switch_active() -> bool:
    return kill_switch().exists()


def load_state() -> dict[str, Any]:
    """State dict: {'last_fired_at': iso|None}. Missing/corrupt => fresh empty
    (safe direction: re-baseline rather than crash)."""
    try:
        data = json.loads(state_file().read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        sf = state_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(state, indent=2), encoding='utf-8')
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# -------------------- sampling --------------------

def load_samples(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Return queue-wait samples from the ledger whose ts is within the recent
    WINDOW_HOURS. Malformed lines and rows lacking a numeric queue_wait_sec are
    skipped (never crash the gauge on a partial write)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    out: list[dict[str, Any]] = []
    lp = ledger_path()
    try:
        raw = lp.read_text(encoding='utf-8')
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        ts = _parse_iso(row.get('ts'))
        if ts is None or ts < cutoff:
            continue
        qw = row.get('queue_wait_sec')
        if not isinstance(qw, (int, float)):
            continue
        out.append(row)
    return out


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in [0,100]) of a list. Deterministic, no
    interpolation — matches how we reason about "the p95 sample" and is robust
    to a single outlier. Empty list => 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    # Nearest-rank: rank = ceil(pct/100 * N), clamped to [1, N].
    rank = max(1, min(len(ordered),
                      math.ceil(pct / 100.0 * len(ordered))))
    return float(ordered[rank - 1])


def compute_stats(samples: list[dict[str, Any]]) -> dict[str, Any]:
    waits = [float(s['queue_wait_sec']) for s in samples]
    return {
        'sample_count': len(waits),
        'p95_sec': round(_percentile(waits, 95), 1),
        'max_sec': round(max(waits), 1) if waits else 0.0,
        'window_hours': WINDOW_HOURS,
    }


# -------------------- evaluation --------------------

def evaluate(samples: list[dict[str, Any]], state: dict[str, Any],
             now: Optional[datetime] = None) -> tuple[bool, str, dict[str, Any]]:
    """Decide whether to fire. Returns (should_fire, reason, stats)."""
    now = now or datetime.now(timezone.utc)
    stats = compute_stats(samples)

    if stats['sample_count'] < MIN_SAMPLES:
        return (False,
                f'thin window: {stats["sample_count"]} sample(s) < '
                f'{MIN_SAMPLES} min — not a burst, holding',
                stats)

    if stats['p95_sec'] < P95_THRESHOLD_SEC:
        return (False,
                f'p95 queue-wait {stats["p95_sec"]/60.0:.1f}m < '
                f'{P95_THRESHOLD_SEC/60.0:.0f}m threshold over '
                f'{stats["sample_count"]} sample(s) — two slots keeping up',
                stats)

    last_fired = _parse_iso(state.get('last_fired_at'))
    if last_fired is not None:
        elapsed = (now - last_fired).total_seconds()
        if elapsed < FIRE_COOLDOWN_SEC:
            remaining_h = (FIRE_COOLDOWN_SEC - elapsed) / 3600.0
            return (False,
                    f'would fire (p95 {stats["p95_sec"]/60.0:.1f}m over '
                    f'{stats["sample_count"]} samples) but in cooldown '
                    f'({remaining_h:.1f}h left)',
                    stats)

    return (True,
            f'FIRE: p95 queue-wait {stats["p95_sec"]/60.0:.1f}m >= '
            f'{P95_THRESHOLD_SEC/60.0:.0f}m over {stats["sample_count"]} '
            f'review(s) in {WINDOW_HOURS}h — two slots saturating on bursts',
            stats)


def _render_alert(stats: dict[str, Any]) -> tuple[str, str]:
    p95_m = stats['p95_sec'] / 60.0
    max_m = stats['max_sec'] / 60.0
    message = (
        f'Mirror review queue-wait is still high WITH two slots running — a '
        f'third review slot (or a per-review service-time cut) may now be '
        f'worth it.\n'
        f'\n'
        f'Over the last {stats["window_hours"]}h across {stats["sample_count"]} '
        f'review(s):\n'
        f'  - p95 PR-open → review-start wait: {p95_m:.1f}m '
        f'(threshold {P95_THRESHOLD_SEC/60.0:.0f}m).\n'
        f'  - worst wait in window: {max_m:.1f}m.\n'
        f'\n'
        f'Two slots were meant to hold burst queue-wait under ~2h end-to-end '
        f'(mirror-two-slot-review §8). A p95 start-wait this high means the two '
        f'slots are themselves saturating during bursts. This gauge stays '
        f'silent below the threshold and will not re-fire for '
        f'{FIRE_COOLDOWN_SEC / (24 * 3600.0):.0f} days.'
    )
    suggested = (
        'Decide whether to raise mirror review_slots to 3 (config/agent-models.json '
        '+ ConcurrencyGuard RAM re-check per mirror-two-slot-review §5) or invest '
        'in cutting per-review service time (regression-gate speedup). No '
        'automatic action is taken; this is a readiness signal only.'
    )
    return message, suggested


def fire(stats: dict[str, Any]) -> bool:
    """Emit the readiness DM via larry_alerts. Returns True on append, False on
    cooldown/error. Never raises."""
    message, suggested = _render_alert(stats)
    try:
        import larry_alerts
        return larry_alerts.append_alert(
            source=ALERT_SOURCE,
            severity='warning',
            message=message,
            subject=ALERT_SUBJECT,
            suggested_action=suggested,
        )
    except Exception as e:  # noqa: BLE001
        log(f'fire: append_alert failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- tick orchestration --------------------

def run_once(now: Optional[datetime] = None, dry_run: bool = False,
             persist: bool = True) -> dict[str, Any]:
    """One tick: load samples → evaluate → (maybe) fire.

    Args:
        now: time anchor (tests pass a fixed time).
        dry_run: compute + decide but NEVER emit and NEVER write last_fired_at.
        persist: write the state file at the end (tests may suppress).

    Returns {stats, should_fire, reason, fired, dry_run}.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        'stats': None, 'should_fire': False, 'reason': '', 'fired': False,
        'dry_run': dry_run,
    }

    if kill_switch_active():
        result['reason'] = f'kill-switch active at {kill_switch()}; no-op'
        log(result['reason'])
        return result

    state = load_state()
    samples = load_samples(now=now)
    should_fire, reason, stats = evaluate(samples, state, now=now)
    result.update(stats=stats, should_fire=should_fire, reason=reason)

    if should_fire and not dry_run:
        if fire(stats):
            state['last_fired_at'] = now.isoformat()
            result['fired'] = True
            log(f'FIRED mirror-queue-wait gauge: {reason}')
        else:
            log('mirror-queue-wait gauge: append_alert suppressed (cooldown or '
                'write error); last_fired_at NOT advanced', 'WARN')
    else:
        log(f'tick: dry_run={dry_run} should_fire={should_fire} :: {reason}')

    if persist and not dry_run:
        save_state(state)
    return result


def _status_text(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    state = load_state()
    samples = load_samples(now=now)
    should_fire, reason, stats = evaluate(samples, state, now=now)
    return '\n'.join([
        'mirror-queue-wait-gauge status',
        f'  window           : last {WINDOW_HOURS}h',
        f'  last_fired_at    : {state.get("last_fired_at", "(never)")}',
        '',
        'current window:',
        f'  sample_count : {stats["sample_count"]}  (need >= {MIN_SAMPLES})',
        f'  p95_sec      : {stats["p95_sec"]}  '
        f'(threshold >= {P95_THRESHOLD_SEC})',
        f'  max_sec      : {stats["max_sec"]}',
        '',
        f'would_fire: {should_fire}',
        f'reason    : {reason}',
    ])


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Mirror review queue-wait gauge — "need a third slot?" '
                    'readiness signal.')
    parser.add_argument('--once', action='store_true',
                        help='run one tick (default action).')
    parser.add_argument('--dry-run', action='store_true',
                        help='decide but never DM or advance state.')
    parser.add_argument('--status', action='store_true',
                        help='print current window + would-fire, no side effects.')
    args = parser.parse_args(argv)

    if args.status:
        print(_status_text())
        return 0

    result = run_once(dry_run=args.dry_run)
    log(f'result: should_fire={result["should_fire"]} fired={result["fired"]} '
        f':: {result["reason"]}')
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
