#!/usr/bin/env python3
"""spec_review_silent_failure_gauge.py — "is the spec gauntlet quietly stuck?" gauge.

PURPOSE
-------
The spec-gauntlet gate (agents/beacon/specs/spec-gauntlet-gate.md) fails OPEN: a
gauntlet that ends ``errored`` or ``incomplete`` lets the underlying approval
through un-reviewed rather than blocking it. That is the correct safety
direction — a broken reviewer must never wedge Larry's approvals — but it
creates a blind spot: if the gate breaks *persistently* (bad config, a runner
crash-loop, a dependency outage), every gauntlet silently ends errored/incomplete
and the gate degrades into an invisible permanent no-op. Nobody is told; the
approvals keep flowing as if the gate were reviewing them.

This is the trailing gauge §3.5 calls for. It reads the durable conclusion
ledger and surfaces "N consecutive gauntlets ended errored/incomplete" so a
stuck gate becomes visible.

INFO SURFACE ONLY — NO DMs
--------------------------
Per the alert default-deny north star (§3.5), this gauge does NOT DM Larry and
does NOT write a for_larry / needs_attention escalation. Persistent gate
breakage is an operational-health signal, not a must-interrupt-the-CEO event.
The surface is a single **chain_event** (``spec_review_silent_failure``,
registered in chain_event_shipper.KNOWN_EVENT_TYPES) — the same zero-DM,
dashboard-visible, "queryable in chain_events" substrate the gauntlet rounds
themselves use (§3.5). It never imports larry_alerts.

SIGNAL SOURCE
-------------
The durable per-gauntlet conclusion files the runner writes (one per gauntlet,
never deleted — a separate ``stamped/`` marker guards re-stamping):
    ~/agents/state/spec-review/concluded/<task_id>.json
Each carries ``{task_id, terminal_state, concluded_at, ...}`` where
terminal_state ∈ {passed, contested, incomplete, errored} (spec_review_conclusion
.TERMINAL_STATES). This gauge treats ``errored`` and ``incomplete`` as the
silent-failure states and ``passed`` / ``contested`` as healthy terminal results
(the gate produced a verdict).

THE FIRING RULE
---------------
FIRE when the *trailing contiguous* run of conclusions (ordered by concluded_at)
that are errored/incomplete is >= MIN_STREAK. Trailing-and-contiguous is the
whole point: one flake does not fire, and a single healthy ``passed`` /
``contested`` breaks the streak and self-clears the gauge — we only care that
the gate is *currently, persistently* failing, not that it ever failed. There
is deliberately no separate freshness/time-window gate in v1: trailing semantics
already self-correct (a live gate producing verdicts breaks the tail), and a
stuck gate that has gone completely quiet is exactly the case we must still
surface, so a "must be recent" clause would defeat the gauge.

Idempotency: the state file records ``surfaced_tail`` (the tail task_id last
surfaced). While the same tail persists we do not re-emit; a genuinely new
failure at the tail (streak grew) surfaces once more with the updated count.
Below threshold the gauge self-clears ``surfaced_tail`` so a future streak fires
fresh. State only advances on a *successful* emit.

This gauge only READS the conclusion ledger + its own state file; the one file
it writes is its state. No dispatch, no gate change — a visibility SIGNAL only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# -------------------- paths (resolved at call time, honor sandbox) --------------------

def agents_root() -> Path:
    """Agents runtime root. Honors OURLIBERTY_AGENTS_ROOT (test bootstrap /
    sandbox); defaults to ~/agents. Resolved at call time so a test redirect
    lands rather than being frozen at import."""
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def concluded_dir() -> Path:
    """Where the runner writes one durable conclusion file per gauntlet."""
    return agents_root() / 'state' / 'spec-review' / 'concluded'


def state_file() -> Path:
    return agents_root() / 'state' / 'spec-review-silent-failure-gauge.json'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def log_file() -> Path:
    return agents_root() / 'logs' / 'spec-review-silent-failure-gauge.log'


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


# Trailing contiguous errored/incomplete conclusions at/above which the gate
# looks persistently stuck. 3 default: one flake does not fire, but three in a
# row is a pattern worth a human eye. Env: OURLIBERTY_SPEC_REVIEW_GAUGE_MIN_STREAK.
MIN_STREAK = _env_int('OURLIBERTY_SPEC_REVIEW_GAUGE_MIN_STREAK', 3)

# The terminal states that mean the gate produced NO verdict (fail-open). A
# `passed` or `contested` conclusion is a healthy verdict and breaks the streak.
FAILURE_STATES: frozenset[str] = frozenset({'errored', 'incomplete'})

GAUGE_EVENT_TYPE = 'spec_review_silent_failure'
EMIT_AGENT = 'spec-review'


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
    """State dict: {'surfaced_tail': task_id|None, 'last_fired_at': iso|None}.
    Missing/corrupt => fresh empty (safe direction: re-baseline rather than
    crash — worst case a re-surface, never a missed surface)."""
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


# -------------------- conclusion ledger --------------------

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def load_conclusions() -> list[dict[str, Any]]:
    """Return the conclusion records, ordered oldest → newest by concluded_at so
    the trailing streak is the tail of the list.

    Each record is normalized to {task_id, terminal_state, concluded_at}. Files
    that are unreadable / not-JSON / not-a-dict / missing terminal_state are
    skipped — a single corrupt artifact must never crash the gauge. A record
    with an unparseable concluded_at sorts as oldest (it cannot corrupt the
    recent tail) with task_id as a deterministic tiebreaker."""
    cdir = concluded_dir()
    out: list[dict[str, Any]] = []
    try:
        paths = sorted(cdir.glob('*.json'))
    except OSError:
        return out
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        terminal_state = data.get('terminal_state')
        if not isinstance(terminal_state, str):
            continue
        task_id = data.get('task_id')
        if not isinstance(task_id, str) or not task_id:
            task_id = p.stem
        out.append({
            'task_id': task_id,
            'terminal_state': terminal_state,
            'concluded_at': data.get('concluded_at'),
        })
    out.sort(key=lambda r: (_parse_iso(r.get('concluded_at')) or _EPOCH,
                            r['task_id']))
    return out


def compute_streak(conclusions: list[dict[str, Any]]) -> dict[str, Any]:
    """Trailing contiguous run of FAILURE_STATES at the newest end.

    Returns {streak, tail_task_id, streak_task_ids, total}. Empty ledger =>
    streak 0, tail None."""
    streak_ids: list[str] = []
    for rec in reversed(conclusions):
        if rec['terminal_state'] in FAILURE_STATES:
            streak_ids.append(rec['task_id'])
        else:
            break
    streak_ids.reverse()  # oldest → newest within the streak
    tail = conclusions[-1]['task_id'] if conclusions else None
    return {
        'streak': len(streak_ids),
        'tail_task_id': tail,
        'streak_task_ids': streak_ids,
        'total': len(conclusions),
    }


# -------------------- evaluation --------------------

def evaluate(conclusions: list[dict[str, Any]],
             state: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Decide whether to surface. Returns (should_fire, reason, info)."""
    info = compute_streak(conclusions)
    streak = info['streak']

    if info['total'] == 0:
        return (False, 'no concluded gauntlets yet — nothing to measure', info)

    if streak < MIN_STREAK:
        return (False,
                f'trailing errored/incomplete streak {streak} < {MIN_STREAK} — '
                f'gate is producing verdicts',
                info)

    if state.get('surfaced_tail') == info['tail_task_id']:
        return (False,
                f'streak {streak} >= {MIN_STREAK} but tail '
                f'{info["tail_task_id"]!r} already surfaced — holding',
                info)

    return (True,
            f'FIRE: {streak} consecutive gauntlet(s) ended errored/incomplete '
            f'(>= {MIN_STREAK}) — spec gauntlet may be silently stuck',
            info)


# -------------------- surfacing (chain_event, no DM) --------------------

def surface(info: dict[str, Any]) -> bool:
    """Emit the info-only ``spec_review_silent_failure`` chain_event. NO DM, NO
    for_larry, NO needs_attention. Returns True on a confirmed emit, False on
    drop/error. Never raises — a failed emit is event-loss, never a crash."""
    payload = {
        'streak': info['streak'],
        'min_streak': MIN_STREAK,
        'failure_states': sorted(FAILURE_STATES),
        'tail_task_id': info['tail_task_id'],
        'streak_task_ids': info['streak_task_ids'],
        'summary': (
            f'{info["streak"]} consecutive spec gauntlet(s) ended '
            f'errored/incomplete — the gate may be silently failing open.'
        ),
    }
    try:
        import chain_event_emit  # local: keep the Supabase dep off the hot path
        return bool(chain_event_emit.emit_event(
            event_type=GAUGE_EVENT_TYPE,
            agent=EMIT_AGENT,
            task_id=info['tail_task_id'],
            payload=payload,
            id_extra=info['tail_task_id'],
        ))
    except Exception as e:  # noqa: BLE001
        log(f'surface: emit_event failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- tick orchestration --------------------

def run_once(now: Optional[datetime] = None, dry_run: bool = False,
             persist: bool = True) -> dict[str, Any]:
    """One tick: load conclusions → evaluate → (maybe) surface.

    Args:
        now: time anchor (tests pass a fixed time; used only for last_fired_at).
        dry_run: compute + decide but NEVER emit and NEVER mutate/write state.
        persist: write the state file at the end (tests may suppress).

    Returns {info, should_fire, reason, fired, dry_run}.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        'info': None, 'should_fire': False, 'reason': '', 'fired': False,
        'dry_run': dry_run,
    }

    if kill_switch_active():
        result['reason'] = f'kill-switch active at {kill_switch()}; no-op'
        log(result['reason'])
        return result

    state = load_state()
    conclusions = load_conclusions()
    should_fire, reason, info = evaluate(conclusions, state)
    result.update(info=info, should_fire=should_fire, reason=reason)

    if dry_run:
        log(f'tick: dry_run=True should_fire={should_fire} :: {reason}')
        return result

    dirty = False
    if should_fire:
        if surface(info):
            state['surfaced_tail'] = info['tail_task_id']
            state['last_fired_at'] = now.isoformat()
            result['fired'] = True
            dirty = True
            log(f'SURFACED silent-failure gauge: {reason}')
        else:
            log('silent-failure gauge: emit dropped/failed; surfaced_tail NOT '
                'advanced', 'WARN')
    else:
        # Self-clear: once the gate is healthy again (streak below threshold),
        # forget the surfaced tail so the next streak surfaces fresh.
        if info['streak'] < MIN_STREAK and state.get('surfaced_tail') is not None:
            state.pop('surfaced_tail', None)
            dirty = True
            log('silent-failure gauge: streak cleared; surfaced_tail reset')
        log(f'tick: should_fire=False :: {reason}')

    if persist and dirty:
        save_state(state)
    return result


def _status_text() -> str:
    state = load_state()
    conclusions = load_conclusions()
    should_fire, reason, info = evaluate(conclusions, state)
    return '\n'.join([
        'spec-review-silent-failure-gauge status',
        f'  min_streak      : {MIN_STREAK}',
        f'  failure_states  : {sorted(FAILURE_STATES)}',
        f'  surfaced_tail   : {state.get("surfaced_tail", "(none)")}',
        f'  last_fired_at   : {state.get("last_fired_at", "(never)")}',
        '',
        'current ledger:',
        f'  total_concluded : {info["total"]}',
        f'  trailing_streak : {info["streak"]}  (need >= {MIN_STREAK})',
        f'  tail_task_id    : {info["tail_task_id"]}',
        '',
        f'would_fire: {should_fire}',
        f'reason    : {reason}',
    ])


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Spec-gauntlet silent-failure gauge — surface N consecutive '
                    'errored/incomplete gauntlets (info-only, no DMs).')
    parser.add_argument('--once', action='store_true',
                        help='run one tick (default action).')
    parser.add_argument('--dry-run', action='store_true',
                        help='decide but never emit or mutate state.')
    parser.add_argument('--status', action='store_true',
                        help='print current ledger + would-fire, no side effects.')
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
