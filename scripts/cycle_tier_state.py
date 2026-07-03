#!/usr/bin/env python3
"""cycle_tier_state.py — Pulse cycle tier state machine.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 2.2 + α₁ brief § 2.2.

State file: ``~/agents/state/cycle-tier.json``. Schema::

    {
      "tier": 1|2|3,
      "consecutive_clean": int,
      "last_signal_at": <iso8601 utc>|null,
      "last_updated": <iso8601 utc>
    }

Semantics (per § 2.2):

  - Any non-empty check finding resets to Tier 1 (``reset_to_tier_1``).
  - 3 consecutive clean iters at the current tier promote to next tier
    (Tier 1 → 2, 2 → 3, capped at 3).
  - On read corruption or missing file, restore the Tier 1 default and
    DM Larry once via larry_alerts (warning).

All writes are atomic (tmp + replace). Stdlib only; no subprocess; no
LLM. Safe to call from run_cycle.sh and from /cycle prompt context.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)
STATE_REL = 'state/cycle-tier.json'
LOG_REL = 'logs/cycle-tier-state.log'

MAX_TIER = 3
MIN_TIER = 1
CLEAN_ITERS_TO_PROMOTE = 3

_DEFAULT_STATE: dict[str, Any] = {
    'tier': 1,
    'consecutive_clean': 0,
    'last_signal_at': None,
    'last_updated': None,
}


def _state_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / STATE_REL


def _log_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / LOG_REL


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _log(msg: str, level: str = 'INFO') -> None:
    line = f'[{_now_iso()}] [{level}] {msg}'
    print(line, flush=True)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def _default_state() -> dict[str, Any]:
    state = dict(_DEFAULT_STATE)
    state['last_updated'] = _now_iso()
    return state


def _validate(data: Any) -> Optional[dict[str, Any]]:
    """Schema-validate a parsed JSON payload. Returns the canonicalized
    dict on success, None on any structural problem (caller falls back to
    defaults)."""
    if not isinstance(data, dict):
        return None
    tier = data.get('tier')
    if not isinstance(tier, int) or tier < MIN_TIER or tier > MAX_TIER:
        return None
    cc = data.get('consecutive_clean')
    if not isinstance(cc, int) or cc < 0:
        return None
    last_signal = data.get('last_signal_at')
    if last_signal is not None and not isinstance(last_signal, str):
        return None
    last_updated = data.get('last_updated')
    if last_updated is not None and not isinstance(last_updated, str):
        return None
    return {
        'tier': tier,
        'consecutive_clean': cc,
        'last_signal_at': last_signal,
        'last_updated': last_updated,
    }


def _atomic_write(state: dict[str, Any]) -> None:
    """Atomic tmp + replace. Never partial; never overwrites with garbage."""
    from test_isolation_guard import refuse_live_state_write  # sibling; test-jail guard
    path = _state_path()
    refuse_live_state_write(path, 'cycle-tier-state-write')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _dm_corruption_once() -> None:
    """Best-effort warning DM on schema-corruption recovery. Never raises."""
    try:
        import larry_alerts as la  # local import: tests can monkeypatch
        la.append_alert(
            source='cycle-tier-state',
            severity='warning',
            subject='cycle-tier-corrupted',
            message=(
                'cycle-tier.json was missing or schema-invalid; restored '
                'Tier 1 defaults. If this recurs, inspect '
                '~/agents/state/cycle-tier.json by hand.'
            ),
            suggested_action=(
                "echo '{\"tier\":1,\"consecutive_clean\":0,"
                "\"last_signal_at\":null}' > ~/agents/state/cycle-tier.json"
            ),
        )
    except Exception:
        pass


def read_tier_state() -> dict[str, Any]:
    """Atomic read. On missing or corrupt, writes the default state back
    and DMs Larry (warning) about the corruption."""
    path = _state_path()
    if not path.exists():
        state = _default_state()
        _atomic_write(state)
        return state
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _log(f'cycle-tier.json unreadable; resetting to Tier 1', 'WARN')
        _dm_corruption_once()
        state = _default_state()
        _atomic_write(state)
        return state
    validated = _validate(raw)
    if validated is None:
        _log(f'cycle-tier.json schema-invalid; resetting to Tier 1', 'WARN')
        _dm_corruption_once()
        state = _default_state()
        _atomic_write(state)
        return state
    return validated


def _write_state(state: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    state['last_updated'] = _now_iso()
    _atomic_write(state)
    return state


def advance_tier() -> int:
    """Promote one tier up to MAX_TIER. Resets consecutive_clean=0.
    Returns the new tier. No-op if already at MAX_TIER."""
    state = read_tier_state()
    old = int(state['tier'])
    new = min(old + 1, MAX_TIER)
    state['tier'] = new
    state['consecutive_clean'] = 0
    _write_state(state)
    if new != old:
        _log(f'tier promoted {old} -> {new}')
    return new


def reset_to_tier_1() -> dict[str, Any]:
    """Force Tier 1 + zero clean counter + stamp last_signal_at=now.
    Called on any non-empty Check finding per § 2.3 tier-reset rule."""
    state = read_tier_state()
    old = int(state['tier'])
    state['tier'] = 1
    state['consecutive_clean'] = 0
    state['last_signal_at'] = _now_iso()
    state = _write_state(state)
    if old != 1:
        _log(f'tier reset {old} -> 1 (signal observed)')
    return state


def record_iter_result(checks_clean: bool) -> dict[str, Any]:
    """Append one cycle-iter outcome to the state machine.

    - ``checks_clean=False`` -> reset_to_tier_1.
    - ``checks_clean=True`` -> increment consecutive_clean; if it crosses
      CLEAN_ITERS_TO_PROMOTE at current tier, promote.

    Returns the post-mutation state.
    """
    if not checks_clean:
        return reset_to_tier_1()
    state = read_tier_state()
    cc = int(state['consecutive_clean']) + 1
    if cc >= CLEAN_ITERS_TO_PROMOTE and int(state['tier']) < MAX_TIER:
        # Promote; reset counter (advance_tier zeroes it).
        advance_tier()
        return read_tier_state()
    state['consecutive_clean'] = cc
    return _write_state(state)


# -------------------- CLI --------------------


def _cli_read(_args) -> int:
    print(json.dumps(read_tier_state()))
    return 0


def _cli_record(args) -> int:
    state = record_iter_result(args.checks_clean == 'true')
    print(json.dumps(state))
    return 0


def _cli_advance(_args) -> int:
    new = advance_tier()
    print(json.dumps({'tier': new}))
    return 0


def _cli_reset(_args) -> int:
    state = reset_to_tier_1()
    print(json.dumps(state))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='cycle_tier_state.py',
                                     description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('read', help='Print current state JSON.')
    p_record = sub.add_parser('record',
                              help='Record one iter outcome and print state.')
    p_record.add_argument('--checks-clean', required=True,
                          choices=['true', 'false'])
    sub.add_parser('advance', help='Force-advance one tier.')
    sub.add_parser('reset', help='Force-reset to Tier 1.')
    args = parser.parse_args(argv)
    if args.cmd == 'read':
        return _cli_read(args)
    if args.cmd == 'record':
        return _cli_record(args)
    if args.cmd == 'advance':
        return _cli_advance(args)
    if args.cmd == 'reset':
        return _cli_reset(args)
    return 2


if __name__ == '__main__':
    sys.exit(main())
