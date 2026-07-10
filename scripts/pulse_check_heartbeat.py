#!/usr/bin/env python3
"""pulse_check_heartbeat.py — liveness signals for the Pulse checks (I–X).

A Pulse check is the one fleet component with no heartbeat of its own: when its
invoker (the Pulse agent's /cycle) stops calling it, it goes dark silently. That
is exactly the failure that hid the IX/X stall for ~2 days (cost-signal audit,
2026-06-03). This module gives every check a freshness-of-success signal so the
companion watcher (scripts/heal_pulse_check_staleness.py) can alert before a
dark check goes unnoticed again.

Each pulse_check_*.py wraps its main() with run_check(<id>, main):

  - clean exit (rc == 0)  -> touch blackboard/pulse-check-<id>.heartbeat
  - nonzero exit / raise  -> emit a pulse-check-failed:<id> larry-alert

The heartbeat is invocation-agnostic on purpose — it fires no matter WHAT
scheduled the check (agent /cycle today, anything tomorrow). Freshness-of-success
(not a bare try/except) is what catches all three failure modes: errored,
can't-run/missing-env, and fully-silent timer death.

Heartbeat shape (docs/pulse-check-liveness-brief.md):
    blackboard/pulse-check-<id>.heartbeat -> {"ts": <iso8601>, "check": "<id>"}

Stdlib only. Never raises out of emit_* — a liveness signal must not be able to
crash the check it is observing.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def agents_root() -> Path:
    """Resolve ~/agents, honoring OURLIBERTY_AGENTS_ROOT (test isolation)."""
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def heartbeat_path(check_id: str) -> Path:
    return agents_root() / 'blackboard' / f'pulse-check-{check_id}.heartbeat'


def deferral_path(check_id: str) -> Path:
    return agents_root() / 'blackboard' / f'pulse-check-{check_id}.deferred'


def emit_heartbeat(check_id: str, *, now: Optional[datetime] = None) -> bool:
    """Write the success heartbeat for a check. Atomic; never raises.

    A completed run also clears any deferral streak: a success proves the runner
    is not starved, so the consecutive-deferral counter must not persist past it.
    """
    ts = (now or datetime.now(timezone.utc)).isoformat()
    payload = json.dumps({'ts': ts, 'check': check_id}, ensure_ascii=False)
    path = heartbeat_path(check_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(payload + '\n', encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        return False
    try:
        deferral_path(check_id).unlink()
    except OSError:
        pass
    return True


def _read_consecutive_deferrals(path: Path) -> int:
    """Prior consecutive-deferral count from the deferral file; 0 if absent."""
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    count = obj.get('consecutive') if isinstance(obj, dict) else None
    return count if isinstance(count, int) and count >= 0 else 0


def emit_deferral(check_id: str, *, now: Optional[datetime] = None) -> bool:
    """Record a legitimate deferral: the runner fired on schedule but could not
    acquire its single-flight lock within the bounded wait, so it skipped WITHOUT
    running. Atomic; never raises.

    This is a distinct liveness class from the success heartbeat: it tells the
    staleness watcher the runner IS firing (so a single contended night is not a
    gone-dark alarm), while a monotonically incrementing consecutive counter lets
    the watcher escalate a genuinely starved runner (N nights deferred, zero
    completed runs). A completed run clears the streak (see emit_heartbeat).
    """
    ts = (now or datetime.now(timezone.utc)).isoformat()
    path = deferral_path(check_id)
    consecutive = _read_consecutive_deferrals(path) + 1
    payload = json.dumps(
        {'ts': ts, 'check': check_id, 'consecutive': consecutive},
        ensure_ascii=False,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(payload + '\n', encoding='utf-8')
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def emit_failure(check_id: str, detail: str) -> bool:
    """Emit a pulse-check-failed:<id> larry-alert. Never raises.

    A failed check is the cheap complement to the heartbeat: the watcher would
    catch the missing heartbeat anyway, but a same-cycle failure DM names the
    error immediately rather than waiting out the staleness window.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402

        return la.append_alert(
            source='pulse-check',
            severity='warning',
            message=(
                f'Pulse check {check_id} did not complete cleanly: {detail}. '
                'The check ran but errored, so its liveness heartbeat was not '
                'refreshed — investigate before its next scheduled firing.'
            ),
            subject=f'pulse-check-failed:{check_id}',
            suggested_action=(
                f'Run python3 ~/agent-core/scripts/pulse_check_{check_id}.py '
                'by hand and read the traceback; logs are under '
                '~/agents/logs/pulse-check-*.log.'
            ),
        )
    except Exception:
        return False


def run_check(
    check_id: str,
    main_fn: Callable[..., int],
    *,
    argv: Optional[list] = None,
    log_fn: Optional[Callable[..., None]] = None,
) -> int:
    """Run a check's main() and emit the liveness signal around it.

    Returns the exit code to hand to sys.exit(). SystemExit (e.g. argparse
    --help / arg errors) propagates untouched so CLI behavior is unchanged.
    """
    try:
        rc = main_fn() if argv is None else main_fn(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — top-level guard for one check
        if log_fn is not None:
            try:
                log_fn(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
            except Exception:
                pass
        emit_failure(check_id, f'{type(exc).__name__}: {exc}')
        return 1
    if rc == 0:
        emit_heartbeat(check_id)
    else:
        emit_failure(check_id, f'exited with code {rc}')
    return rc
