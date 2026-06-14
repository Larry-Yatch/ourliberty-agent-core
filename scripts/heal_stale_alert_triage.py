#!/usr/bin/env python3
"""heal_stale_alert_triage.py — terminal-state reconciler for stuck alert-triage rows.

Spec: agents/beacon/specs/terminal-state-reconciliation.md § 3.2.

The per-alert triage lifecycle (alert_triage_state.py) advances
``pending → triaged-tier-N → action-dispatched → resolved``. The
``action-dispatched → resolved`` transition fires only on a happy-path linking
event; when that event is missed, a row sits in ``action-dispatched`` forever —
five rows are stuck 10+ days as of 2026-06-14. This healer is the ADDITIVE
backstop: for each ``action-dispatched`` row past a grace window, it probes the
dispatched work's terminal ground truth via the shared ``task_terminal_state``
and, when the work is MERGED/CLOSED, calls ``mark_resolved``.

**Conservative posture (spec § 1, non-negotiable):** an OPEN or UNKNOWN/
indeterminate probe ⇒ KEEP. A row is resolved ONLY when its dispatched work is
positively terminal AND the row has aged past the grace window. Erring toward
UNKNOWN can only leave a phantom for another cycle — it can never falsely retire
a live row. (Tier-1 auto-fix rows carry a ``canonical_intervention_id`` as their
dispatch_task_id, which matches no PR ⇒ UNKNOWN ⇒ kept — correct by design.)

Applies by default (scheduled healer). Pass --dry-run to classify without
writing. Stdlib + ``gh`` + the existing alert_triage_state helper only; no
Supabase dependency.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alert_triage_state as ats  # noqa: E402
import task_terminal_state as tts  # noqa: E402 — shared terminal-state probe

# Resolve the runtime root identically to alert_triage_state (and the rest of the
# healer family): honor OURLIBERTY_AGENTS_ROOT, else ~/agents. `or`-fallback so an
# EMPTY override degrades to ~/agents rather than Path('') = cwd.
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or Path.home() / 'agents')
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-stale-alert-triage.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-stale-alert-triage.heartbeat'

# A dispatched row is only probed once it has aged past this grace window — a
# freshly-dispatched row's work may legitimately still be in flight. The spec
# (§ 3.2) notes rows stuck 10+ days; a 2h grace mirrors § 3.1's pending-approval
# window so both reconcilers share one posture.
DISPATCHED_GRACE_HOURS = 2.0

DISPATCHED_STATUS = 'action-dispatched'


# -------------------- logging + heartbeat --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# -------------------- classification (pure) --------------------

def _row_age_hours(row: dict[str, Any], now: datetime) -> Optional[float]:
    """Hours since the row was dispatched, or None if dispatched_at is missing
    or unparseable (caller treats as KEEP — never retire on a bad ts)."""
    raw = row.get('dispatched_at')
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dispatched = ats_parse_iso(raw)
    except (ValueError, TypeError):
        return None
    return (now - dispatched).total_seconds() / 3600.0


def ats_parse_iso(value: str) -> datetime:
    """Parse ISO-8601 robustly; accept 'Z' or offset, return aware UTC.
    (alert_triage_state writes _now_iso() timestamps, which are offset-aware, but
    accept 'Z' / naive too so a hand-edited row never crashes the probe.)"""
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_stuck_row(
    row: dict[str, Any],
    now: datetime,
    grace_hours: float,
    probe: Any,
) -> tuple[bool, str]:
    """(resolve?, reason) for one alert-triage row against its dispatched work's
    terminal ground truth. Conservative (spec § 1): resolve ONLY when the row is
    ``action-dispatched``, aged past the grace window, AND the work is positively
    terminal (MERGED/CLOSED). EVERY other path — wrong status, missing/unparseable
    dispatched_at, within grace, missing dispatch_task_id, OPEN, or UNKNOWN — is
    KEEP."""
    if row.get('status') != DISPATCHED_STATUS:
        return False, f"status={row.get('status')!r} not {DISPATCHED_STATUS} (keep)"
    age = _row_age_hours(row, now)
    if age is None:
        return False, 'no/unparseable dispatched_at (keep)'
    if age < grace_hours:
        return False, f'within grace ({age:.1f}h < {grace_hours}h) (keep)'
    task_id = row.get('dispatch_task_id')
    if not isinstance(task_id, str) or not task_id:
        return False, 'no dispatch_task_id to probe (keep)'
    state = probe(task_id)
    if state in tts.TERMINAL_STATES:
        return True, f'work terminal ({state}) past {grace_hours}h grace'
    return False, f'work {state} (not terminal) (keep)'


# -------------------- reconcile --------------------

def reconcile_stuck_alert_triage(
    *,
    state: Optional[dict[str, dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    grace_hours: float = DISPATCHED_GRACE_HOURS,
    probe: Any = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Spec § 3.2: resolve alert-triage rows stuck in ``action-dispatched`` whose
    dispatched work has reached a terminal state. For each such row past the
    grace window, probe ``task_terminal_state(dispatch_task_id)``; on
    MERGED/CLOSED call ``mark_resolved`` — OPEN/UNKNOWN keep.

    ADDITIVE backstop to the happy-path ``mark_resolved`` linking event; only
    catches rows whose linking event was missed.

    `state` / `now` / `probe` are injectable for tests; in production they come
    from `ats.read_state()`, the wall clock, and `task_terminal_state`. Returns a
    counts dict."""
    now = now or datetime.now(timezone.utc)
    probe = probe or (lambda tid: tts.task_terminal_state(tid))
    counts = {'dispatched': 0, 'resolved': 0, 'kept': 0}

    rows = state if state is not None else ats.read_state()

    to_resolve: list[tuple[str, str]] = []
    for alert_id, row in rows.items():
        if not isinstance(row, dict):
            continue
        if row.get('status') == DISPATCHED_STATUS:
            counts['dispatched'] += 1
        resolve, reason = classify_stuck_row(row, now, grace_hours, probe)
        if resolve:
            to_resolve.append((alert_id, reason))
        elif row.get('status') == DISPATCHED_STATUS:
            counts['kept'] += 1

    for alert_id, reason in to_resolve:
        if dry_run:
            counts['resolved'] += 1
            log(f'DRY-RUN would resolve alert-triage row {alert_id} ({reason})')
            continue
        try:
            ok = ats.mark_resolved(
                alert_id, resolved_ts=ats._now_iso(),
                resolution=f'auto-resolved by heal-stale-alert-triage '
                           f'(terminal-state): {reason}')
        except Exception as e:  # noqa: BLE001
            log(f'mark_resolved failed for {alert_id}: '
                f'{type(e).__name__}: {e}', 'ERROR')
            continue
        if not ok:
            # Row vanished between read and write (concurrent resolve). Not an
            # error — the phantom is gone either way.
            log(f'resolve: {alert_id} no longer present ({reason})')
            continue
        counts['resolved'] += 1
        log(f'terminal-resolved alert-triage row {alert_id} ({reason})')

    log('alert-triage reconcile: ' + ('DRY-RUN ' if dry_run else '')
        + ' '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


# -------------------- main --------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='classify + report but write nothing')
    args = ap.parse_args()

    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return 0
    heartbeat()

    try:
        reconcile_stuck_alert_triage(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
