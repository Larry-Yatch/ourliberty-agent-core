#!/usr/bin/env python3
"""alert_triage_state.py — per-alert triage lifecycle state.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 3.0 (Check 0 alert
triage) + α₂ brief § 6.10 (lifecycle).

State file: ``~/agents/state/alert-triage.json``. One JSON object whose
keys are alert IDs and whose values are per-alert lifecycle rows.

Per-alert row schema::

    {
      "alert_id": "<str>",
      "tier": 1|2|3,
      "decision": "<str>",                # e.g. "dispatch", "snooze", "noop"
      "rationale": "<str>",
      "status": "pending"|"triaged-tier-N"|"action-dispatched"|"resolved",
      "triaged_at": <iso8601>|null,
      "dispatched_at": <iso8601>|null,
      "dispatch_target_agent": "<str>"|null,
      "dispatch_task_id": "<str>"|null,
      "resolved_at": <iso8601>|null,
      "resolution": "<str>"|null,
      "last_updated": <iso8601>
    }

Lifecycle: ``pending → triaged-tier-N → action-dispatched → resolved``
(per α₂ § 3.0). The functions below advance one transition each and are
intentionally additive: an unknown alert_id is created on the first
``record_triage`` call; ``mark_dispatched`` / ``mark_resolved`` no-op (
return False) if the prior state isn't present.

Atomic writes via tmp + replace. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STATE_REL = 'state/alert-triage.json'
LOG_REL = 'logs/alert-triage-state.log'

VALID_TIERS = (1, 2, 3)


def _state_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / STATE_REL


def _log_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / LOG_REL


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def read_state() -> dict[str, dict[str, Any]]:
    """Atomic read. Returns {} on missing or corrupt — callers can then
    add the first row without losing prior data only when prior data was
    unreadable (the alternative — refusing to write — would leave the
    triage state permanently stuck after one bad write)."""
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _log(f'{path.name} unreadable; treating as empty', 'WARN')
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = v
    return out


def _write_state(state: dict[str, dict[str, Any]]) -> None:
    """Atomic tmp + replace. Never partial-file-write."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def record_triage(alert_id: str, tier: int, decision: str,
                  rationale: str) -> dict[str, Any]:
    """Move (or initialize) ``alert_id`` to ``triaged-tier-N``.
    Overwrites prior triage decisions (re-triage is expected when an
    alert resurfaces). Returns the post-mutation row."""
    if not isinstance(alert_id, str) or not alert_id:
        raise ValueError('alert_id must be a non-empty string')
    if tier not in VALID_TIERS:
        raise ValueError(f'invalid tier={tier!r}')
    state = read_state()
    now = _now_iso()
    existing = state.get(alert_id, {})
    row: dict[str, Any] = {
        'alert_id': alert_id,
        'tier': int(tier),
        'decision': str(decision),
        'rationale': str(rationale),
        'status': f'triaged-tier-{tier}',
        'triaged_at': now,
        'dispatched_at': existing.get('dispatched_at'),
        'dispatch_target_agent': existing.get('dispatch_target_agent'),
        'dispatch_task_id': existing.get('dispatch_task_id'),
        'resolved_at': existing.get('resolved_at'),
        'resolution': existing.get('resolution'),
        'last_updated': now,
    }
    state[alert_id] = row
    _write_state(state)
    return row


def mark_dispatched(alert_id: str, dispatch_ts: str,
                    target_agent: str, task_id: str) -> bool:
    """Transition ``triaged-tier-N → action-dispatched``. Returns False
    (no-op) if the alert hasn't been triaged yet."""
    state = read_state()
    row = state.get(alert_id)
    if not row:
        _log(f'mark_dispatched: unknown alert_id={alert_id!r}', 'WARN')
        return False
    row['status'] = 'action-dispatched'
    row['dispatched_at'] = dispatch_ts
    row['dispatch_target_agent'] = target_agent
    row['dispatch_task_id'] = task_id
    row['last_updated'] = _now_iso()
    _write_state(state)
    return True


def mark_resolved(alert_id: str, resolved_ts: str,
                  resolution: str) -> bool:
    """Transition to ``resolved``. Returns False (no-op) if the alert
    isn't present at all. We do NOT enforce a strict
    triaged→dispatched→resolved ordering — Larry may resolve an alert
    directly (e.g., manual fix) without a dispatch step."""
    state = read_state()
    row = state.get(alert_id)
    if not row:
        _log(f'mark_resolved: unknown alert_id={alert_id!r}', 'WARN')
        return False
    row['status'] = 'resolved'
    row['resolved_at'] = resolved_ts
    row['resolution'] = resolution
    row['last_updated'] = _now_iso()
    _write_state(state)
    return True


# -------------------- CLI --------------------


def _cli_read(_args) -> int:
    print(json.dumps(read_state(), indent=2))
    return 0


def _cli_triage(args) -> int:
    row = record_triage(args.alert_id, args.tier, args.decision, args.rationale)
    print(json.dumps(row))
    return 0


def _cli_dispatch(args) -> int:
    ok = mark_dispatched(args.alert_id, args.dispatch_ts,
                         args.target_agent, args.task_id)
    return 0 if ok else 1


def _cli_resolve(args) -> int:
    ok = mark_resolved(args.alert_id, args.resolved_ts, args.resolution)
    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='alert_triage_state.py',
                                     description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('read', help='Print current state JSON.')
    p_t = sub.add_parser('triage', help='Record a triage decision.')
    p_t.add_argument('--alert-id', required=True)
    p_t.add_argument('--tier', required=True, type=int, choices=list(VALID_TIERS))
    p_t.add_argument('--decision', required=True)
    p_t.add_argument('--rationale', required=True)
    p_d = sub.add_parser('dispatched', help='Mark an alert as dispatched.')
    p_d.add_argument('--alert-id', required=True)
    p_d.add_argument('--dispatch-ts', required=True)
    p_d.add_argument('--target-agent', required=True)
    p_d.add_argument('--task-id', required=True)
    p_r = sub.add_parser('resolved', help='Mark an alert as resolved.')
    p_r.add_argument('--alert-id', required=True)
    p_r.add_argument('--resolved-ts', required=True)
    p_r.add_argument('--resolution', required=True)
    args = parser.parse_args(argv)
    if args.cmd == 'read':
        return _cli_read(args)
    if args.cmd == 'triage':
        return _cli_triage(args)
    if args.cmd == 'dispatched':
        return _cli_dispatch(args)
    if args.cmd == 'resolved':
        return _cli_resolve(args)
    return 2


if __name__ == '__main__':
    sys.exit(main())
