#!/usr/bin/env python3
"""suite_guardian_ledger.py — outcome ledger for the Main-Suite Green Guardian.

Spec: agents/beacon/specs/main-suite-green-guardian.md (D2.6, PR-2).

WHAT THIS IS
------------
The obligation ledger that measures every guardian proposal's FULL outcome
window — the substrate D3's staged-autonomy graduation reads. It clones the
``no_session_ledger`` obligation pattern (open / resolve / list_open, fail-safe
atomic IO) with the richer D2.6 per-row schema and two guardian-specific
lifecycle rules:

  * **Observable-based resolution.** A fix obligation resolves off the OBSERVABLE
    — the victim test green >=2 consecutive guardian runs AND the named
    poison-injection test present + passing at main — regardless of merge
    provenance (out-of-band merges are a documented class). ``record_observation``
    is the single place that rule lives.
  * **Abandoned age-out.** An approved-but-dead obligation (no fix PR activity for
    ``_ABANDON_AFTER_DAYS`` days — the verdictless-Forge-death class) terminates
    as ``abandoned`` and is re-eligible ONCE (``reeligible`` flag), digest-noted.

State file: ``~/agents/state/suite-guardian-ledger.json`` — one JSON object keyed
by ``test_id`` (one live obligation per victim test at a time). Per-row schema::

    {
      "test_id": "<str>",              # the victim test the fix targets
      "run_task_id": "<str>",          # the batched approval entry that proposed it
      "poison_test_name": "<str>",     # the named poison-injection test (D2.4)
      "proposed_at": <iso8601>,
      "decision": "proposed"|"approved"|"parked",
      "fix_task_id": "<str>|null",     # set when the guardian dispatches the fix
      "dispatched_at": <iso8601>|null,
      "fix_pr": "<str>|null",
      "merged_at": <iso8601>|null,
      "green_streak_after_fix": <int>, # victim consecutive green runs observed
      "poison_present": <bool>,        # named poison test present + passing
      "window_closed": <bool>,         # 14 clean runs since resolution (D3 graduation)
      "resolved_run": <int>|null,      # guardian run seq at resolution (D3 window clock)
      "filed_stage": <int>|null,       # effective stage when the fix was dispatched (D3: "auto-filed" = >=2)
      "scope_violation": <bool>,       # fix PR touched outside scripts/tests/** (L4 diff gate)
      "regressed": <bool>,
      "regressed_attribution": "same-leak"|"new-polluter"|null,
      "status": "open"|"resolved"|"abandoned",
      "resolved_at": <iso8601>|null,
      "resolution": "<str>|null",
      "reeligible": <bool>,            # an abandoned row got one more shot
      "opened_at": <iso8601>,
      "last_activity_at": <iso8601>
    }

Lifecycle::

    open_proposal -> set_decision(approved|parked) -> mark_dispatched
      -> record_observation (auto-resolves on the observable) | resolve
      -> age_out_abandoned (the approved-but-dead terminal)

Atomic writes via ``atomic_io.atomic_write_json``. Stdlib only otherwise. Every
reader/writer is fail-safe: a corrupt/unreadable file degrades to an empty
ledger, never an exception into the nightly guardian timer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import atomic_io  # noqa: E402


def _agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path('/home/larry/agents')


def default_ledger_path() -> Path:
    return _agents_root() / 'state' / 'suite-guardian-ledger.json'


# An approved obligation with no fix-PR activity for this long is a verdictless
# Forge death (the abandoned class); it terminates and is re-eligible once.
_ABANDON_AFTER_DAYS = 7
# Resolved/abandoned rows retained this long for audit, then pruned.
_RETENTION_DAYS = 30
_MAX_ROWS = 1000

# Decisions.
DEC_PROPOSED = 'proposed'
DEC_APPROVED = 'approved'
DEC_PARKED = 'parked'

# Statuses.
OPEN = 'open'
RESOLVED = 'resolved'
ABANDONED = 'abandoned'

# Observable-resolution threshold: the victim must be green this many consecutive
# guardian runs (with the poison test present + passing) to resolve (spec D2.6 /
# success bar 2).
_GREEN_STREAK_TO_RESOLVE = 2

# Serial-drain cap: at most this many open, dispatched fix obligations at once
# (L1 / D2.1 — the guardian dispatches the next as one resolves).
OPEN_FIX_CAP = 3

# A resolved fix's window "closes" after this many clean guardian runs since
# resolution (spec D3 — "only CLOSED 14-run windows count" toward graduation).
WINDOW_RUNS = 14


# -------------------- time --------------------

def _now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# -------------------- load / save --------------------

def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _prune(state: dict[str, dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    """Drop terminal rows past the retention window; NEVER evict an OPEN row (a
    live obligation must stay visible to the D3 grad reader + the abandoned
    age-out). Bounds RESOLVED/ABANDONED growth only."""
    cutoff = now - timedelta(days=_RETENTION_DAYS)
    kept: dict[str, dict[str, Any]] = {}
    for tid, row in state.items():
        if row.get('status') in (RESOLVED, ABANDONED):
            resolved_at = _parse_iso(row.get('resolved_at'))
            if resolved_at is not None and resolved_at < cutoff:
                continue
        kept[tid] = row
    if len(kept) <= _MAX_ROWS:
        return kept
    open_rows = {k: v for k, v in kept.items() if v.get('status') == OPEN}
    terminal = [(k, v) for k, v in kept.items() if v.get('status') != OPEN]
    slots = _MAX_ROWS - len(open_rows)
    if slots <= 0:
        return open_rows
    terminal.sort(key=lambda kv: kv[1].get('resolved_at')
                  or kv[1].get('last_activity_at') or '')
    return {**open_rows, **dict(terminal[len(terminal) - slots:])}


def _save(path: Path, state: dict[str, dict[str, Any]], now: datetime) -> None:
    pruned = _prune(state, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.atomic_write_json(path, pruned, indent=2, sort_keys=True,
                                trailing_newline=True)


def _touch(row: dict[str, Any], now: datetime) -> None:
    row['last_activity_at'] = _iso(now)


# -------------------- lifecycle --------------------

def open_proposal(
    test_id: str,
    *,
    run_task_id: str,
    poison_test_name: str,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Open (or refresh) a PROPOSED obligation for ``test_id``. Idempotent:
    re-proposing an existing OPEN row preserves ``opened_at``/``proposed_at`` and
    refreshes ``run_task_id``/``poison_test_name`` (a later run re-batching the
    same still-unresolved test). Does NOT reopen a PARKED row (L9 — parked items
    are never re-proposed). Never raises."""
    if not test_id:
        return None
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        existing = state.get(test_id)
        if isinstance(existing, dict) and existing.get('decision') == DEC_PARKED:
            return existing  # parked: never re-proposed
        if isinstance(existing, dict) and existing.get('status') == OPEN:
            existing['run_task_id'] = run_task_id
            existing['poison_test_name'] = poison_test_name
            _touch(existing, n)
            state[test_id] = existing
            _save(p, state, n)
            return existing
        row = {
            'test_id': test_id,
            'run_task_id': run_task_id,
            'poison_test_name': poison_test_name,
            'proposed_at': _iso(n),
            'decision': DEC_PROPOSED,
            'fix_task_id': None,
            'dispatched_at': None,
            'fix_pr': None,
            'merged_at': None,
            'green_streak_after_fix': 0,
            'poison_present': False,
            'window_closed': False,
            'resolved_run': None,
            'filed_stage': None,
            'scope_violation': False,
            'regressed': False,
            'regressed_attribution': None,
            'status': OPEN,
            'resolved_at': None,
            'resolution': None,
            'reeligible': bool(isinstance(existing, dict)
                               and existing.get('status') == ABANDONED),
            'opened_at': _iso(n),
            'last_activity_at': _iso(n),
        }
        state[test_id] = row
        _save(p, state, n)
        return row
    except OSError:
        return None


def set_decision(
    test_id: str,
    decision: str,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Record Larry's decision on an OPEN obligation. ``approved`` keeps it open
    for the serial drain; ``parked`` terminates it (L9 — never re-proposed).
    Returns True iff an OPEN row was updated. Never raises."""
    if decision not in (DEC_APPROVED, DEC_PARKED):
        raise ValueError(f'invalid decision: {decision}')
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        row = state.get(test_id)
        if not isinstance(row, dict) or row.get('status') != OPEN:
            return False
        row['decision'] = decision
        if decision == DEC_PARKED:
            row['status'] = RESOLVED
            row['resolved_at'] = _iso(n)
            row['resolution'] = 'parked'
        _touch(row, n)
        state[test_id] = row
        _save(p, state, n)
        return True
    except OSError:
        return False


def mark_dispatched(
    test_id: str,
    fix_task_id: str,
    *,
    filed_stage: Optional[int] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Record that the guardian dispatched the fix task for ``test_id``.
    ``filed_stage`` stamps the EFFECTIVE stage in force at dispatch time — a fix
    dispatched at stage >=2 is "auto-filed" (no prior consent), the denominator
    for the Stage 2->3 graduation evidence (spec D3). Never raises."""
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        row = state.get(test_id)
        if not isinstance(row, dict) or row.get('status') != OPEN:
            return False
        row['fix_task_id'] = fix_task_id
        row['dispatched_at'] = _iso(n)
        if filed_stage is not None:
            row['filed_stage'] = int(filed_stage)
        _touch(row, n)
        state[test_id] = row
        _save(p, state, n)
        return True
    except OSError:
        return False


def record_fix_pr(
    test_id: str,
    *,
    fix_pr: Optional[str] = None,
    merged_at: Optional[str] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Attach PR/merge activity to an OPEN obligation (resets the abandoned
    clock via last_activity_at). Never raises."""
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        row = state.get(test_id)
        if not isinstance(row, dict) or row.get('status') != OPEN:
            return False
        if fix_pr is not None:
            row['fix_pr'] = fix_pr
        if merged_at is not None:
            row['merged_at'] = merged_at
        _touch(row, n)
        state[test_id] = row
        _save(p, state, n)
        return True
    except OSError:
        return False


def record_observation(
    test_id: str,
    *,
    green_streak: int,
    poison_present: bool,
    current_run: Optional[int] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Fold one guardian run's observation into an OPEN obligation and AUTO-RESOLVE
    on the observable (D2.6): victim green >=2 consecutive runs AND the named
    poison test present + passing. Returns True iff this call RESOLVED the row.
    ``current_run`` (the guardian's completed-run sequence) stamps ``resolved_run``
    on resolution — the clock :func:`close_windows` uses to close the 14-run
    graduation window (spec D3). Records the observation regardless. Never raises."""
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        row = state.get(test_id)
        if not isinstance(row, dict) or row.get('status') != OPEN:
            return False
        row['green_streak_after_fix'] = int(green_streak)
        row['poison_present'] = bool(poison_present)
        _touch(row, n)
        resolved = (int(green_streak) >= _GREEN_STREAK_TO_RESOLVE
                    and bool(poison_present))
        if resolved:
            row['status'] = RESOLVED
            row['resolved_at'] = _iso(n)
            row['resolution'] = 'observed-green'
            if current_run is not None:
                row['resolved_run'] = int(current_run)
        state[test_id] = row
        _save(p, state, n)
        return resolved
    except OSError:
        return False


def resolve(
    test_id: str,
    *,
    resolution: str = 'manual',
    current_run: Optional[int] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Force an OPEN obligation to RESOLVED. ``current_run`` stamps ``resolved_run``
    (the D3 window clock). Never raises."""
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        row = state.get(test_id)
        if not isinstance(row, dict) or row.get('status') != OPEN:
            return False
        row['status'] = RESOLVED
        row['resolved_at'] = _iso(n)
        row['resolution'] = resolution
        if current_run is not None:
            row['resolved_run'] = int(current_run)
        _touch(row, n)
        state[test_id] = row
        _save(p, state, n)
        return True
    except OSError:
        return False


def mark_scope_violation(
    test_id: str,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Flag that a guardian-lane fix PR touched a path outside ``scripts/tests/**``
    (L4 diff-gate violation). A scope violation is a hard graduation disqualifier
    (spec D3 "0 scope violations") and drives a Stage-0 downgrade (L10). Idempotent;
    never raises. Returns True iff a row existed and was flagged."""
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        row = state.get(test_id)
        if not isinstance(row, dict):
            return False
        row['scope_violation'] = True
        _touch(row, n)
        state[test_id] = row
        _save(p, state, n)
        return True
    except OSError:
        return False


def close_windows(
    current_run: int,
    *,
    window: int = WINDOW_RUNS,
    path: Optional[Path] = None,
) -> list[str]:
    """Mark every RESOLVED, non-regressed obligation whose ``resolved_run`` is at
    least ``window`` guardian runs in the past as ``window_closed`` (spec D3 — only
    CLOSED windows count toward graduation). A regression re-opens the accounting,
    so a regressed row never closes. Returns the test_ids newly closed this call.
    Never raises."""
    p = path or default_ledger_path()
    n = _now()
    closed: list[str] = []
    try:
        state = _load(p)
        changed = False
        for tid, row in state.items():
            if row.get('status') != RESOLVED or row.get('window_closed'):
                continue
            if row.get('regressed'):
                continue
            resolved_run = row.get('resolved_run')
            if not isinstance(resolved_run, int):
                continue
            if current_run - resolved_run >= window:
                row['window_closed'] = True
                _touch(row, n)
                closed.append(tid)
                changed = True
        if changed:
            _save(p, state, n)
    except OSError:
        return closed
    return closed


def mark_regressed(
    test_id: str,
    *,
    attribution: Optional[str] = None,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Flag that a previously-resolved fix regressed. ``attribution`` is
    ``same-leak`` (counts against the fix) or ``new-polluter`` (does not) per
    L10. Never raises."""
    if attribution not in (None, 'same-leak', 'new-polluter'):
        raise ValueError(f'invalid attribution: {attribution}')
    p = path or default_ledger_path()
    n = _now(now)
    try:
        state = _load(p)
        row = state.get(test_id)
        if not isinstance(row, dict):
            return False
        row['regressed'] = True
        row['regressed_attribution'] = attribution
        _touch(row, n)
        state[test_id] = row
        _save(p, state, n)
        return True
    except OSError:
        return False


def age_out_abandoned(
    now: Optional[datetime] = None,
    *,
    days: int = _ABANDON_AFTER_DAYS,
    path: Optional[Path] = None,
) -> list[str]:
    """Terminate verdictless-dead fix obligations as ``abandoned`` (the
    verdictless-Forge-death class, D2.6). The dead class is a fix that WAS
    dispatched to Forge (``fix_task_id`` set) but produced no PR activity
    (``fix_pr`` None) for ``days`` after dispatch — Forge died without a verdict.

    The clock is ``dispatched_at``, NOT ``last_activity_at``: the nightly
    observable poll (:func:`record_observation`) touches every open dispatched
    obligation each cycle, so keying abandonment off activity would keep a dead
    dispatch alive forever and permanently hold one of the serial-drain slots.
    An approved obligation still QUEUED behind the drain cap (consented but never
    dispatched) is waiting for a slot, not dead, so it never ages out — only a
    dispatched-then-silent fix does. A still-``proposed`` row waits on Larry.
    Returns the list of aged-out test_ids. Never raises."""
    p = path or default_ledger_path()
    n = _now(now)
    cutoff = n - timedelta(days=days)
    aged: list[str] = []
    try:
        state = _load(p)
        changed = False
        for tid, row in state.items():
            if row.get('status') != OPEN:
                continue
            if row.get('decision') != DEC_APPROVED:
                continue
            if not row.get('fix_task_id'):
                continue  # never dispatched — queued behind the cap, not dead
            if row.get('fix_pr'):
                continue  # PR activity exists — not dead
            dispatched = _parse_iso(row.get('dispatched_at'))
            if dispatched is None or dispatched > cutoff:
                continue
            row['status'] = ABANDONED
            row['resolved_at'] = _iso(n)
            row['resolution'] = 'abandoned'
            _touch(row, n)
            aged.append(tid)
            changed = True
        if changed:
            _save(p, state, n)
    except OSError:
        return aged
    return aged


# -------------------- queries --------------------

def get(test_id: str, *, path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    return _load(path or default_ledger_path()).get(test_id)


def list_open(*, path: Optional[Path] = None) -> list[dict[str, Any]]:
    return [r for r in _load(path or default_ledger_path()).values()
            if r.get('status') == OPEN]


def list_by_status(status: str, *, path: Optional[Path] = None) -> list[dict[str, Any]]:
    return [r for r in _load(path or default_ledger_path()).values()
            if r.get('status') == status]


def count_closed_windows(
    *,
    since_run: Optional[int] = None,
    auto_filed: bool = False,
    path: Optional[Path] = None,
) -> int:
    """Count CLOSED graduation windows (spec D3). A closed window is a resolved
    obligation with ``window_closed`` true, no regression, and no scope violation.
    ``since_run`` (the L10 evidence-reset floor) excludes windows resolved on or
    before that run — after a downgrade, only evidence accrued AFTER counts.
    ``auto_filed`` restricts to fixes dispatched at stage >=2 (the Stage 2->3
    denominator). Never raises."""
    n = 0
    for row in _load(path or default_ledger_path()).values():
        if not row.get('window_closed'):
            continue
        if row.get('regressed') or row.get('scope_violation'):
            continue
        if since_run is not None:
            resolved_run = row.get('resolved_run')
            if not isinstance(resolved_run, int) or resolved_run <= since_run:
                continue
        if auto_filed:
            fs = row.get('filed_stage')
            if not isinstance(fs, int) or fs < 2:
                continue
        n += 1
    return n


def count_same_leak_regressions(
    *,
    since_run: Optional[int] = None,
    path: Optional[Path] = None,
) -> int:
    """Count ``same-leak`` regressions (spec D3/L10 — the disqualifier that must
    be 0 to graduate). ``since_run`` scopes to regressions of fixes resolved after
    the evidence-reset floor. Never raises."""
    n = 0
    for row in _load(path or default_ledger_path()).values():
        if not row.get('regressed'):
            continue
        if row.get('regressed_attribution') != 'same-leak':
            continue
        if since_run is not None:
            resolved_run = row.get('resolved_run')
            if not isinstance(resolved_run, int) or resolved_run <= since_run:
                continue
        n += 1
    return n


def count_scope_violations(
    *,
    since_run: Optional[int] = None,
    path: Optional[Path] = None,
) -> int:
    """Count L4 scope violations (spec D3 — must be 0 to graduate past Stage 1).
    ``since_run`` scopes to violations recorded after the evidence-reset floor
    (keyed off the fix's ``resolved_run`` where present; unresolved-then-violated
    fixes always count since they carry no reset anchor). Never raises."""
    n = 0
    for row in _load(path or default_ledger_path()).values():
        if not row.get('scope_violation'):
            continue
        if since_run is not None:
            resolved_run = row.get('resolved_run')
            if isinstance(resolved_run, int) and resolved_run <= since_run:
                continue
        n += 1
    return n


def count_open_dispatched_fixes(*, path: Optional[Path] = None) -> int:
    """Open obligations whose fix was dispatched and is in flight — the serial
    drain's cap denominator (L1)."""
    return sum(
        1 for r in _load(path or default_ledger_path()).values()
        if r.get('status') == OPEN
        and r.get('decision') == DEC_APPROVED
        and r.get('fix_task_id')
    )


def dispatchable_fixes(
    *, cap: int = OPEN_FIX_CAP, path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Approved, not-yet-dispatched obligations, capped so no more than ``cap``
    fix obligations are ever in flight at once (serial drain, L1/D2.1). Oldest
    proposal first so the backlog drains FIFO."""
    p = path or default_ledger_path()
    open_dispatched = count_open_dispatched_fixes(path=p)
    slots = max(0, cap - open_dispatched)
    if slots == 0:
        return []
    candidates = [
        r for r in _load(p).values()
        if r.get('status') == OPEN
        and r.get('decision') == DEC_APPROVED
        and not r.get('fix_task_id')
    ]
    candidates.sort(key=lambda r: r.get('proposed_at') or '')
    return candidates[:slots]


# -------------------- CLI (inspection / manual ops) --------------------

def _main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description='suite-guardian outcome ledger')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('open', help='list OPEN obligations')
    sub.add_parser('all', help='dump the whole ledger')
    g = sub.add_parser('get'); g.add_argument('test_id')
    r = sub.add_parser('resolve'); r.add_argument('test_id')
    r.add_argument('--resolution', default='manual')
    args = p.parse_args(argv)
    if args.cmd == 'open':
        print(json.dumps(list_open(), indent=2, sort_keys=True))
    elif args.cmd == 'all':
        print(json.dumps(_load(default_ledger_path()), indent=2, sort_keys=True))
    elif args.cmd == 'get':
        print(json.dumps(get(args.test_id), indent=2, sort_keys=True))
    elif args.cmd == 'resolve':
        print(f'resolved={resolve(args.test_id, resolution=args.resolution)}')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
