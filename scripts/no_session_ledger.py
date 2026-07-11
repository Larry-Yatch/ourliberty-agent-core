#!/usr/bin/env python3
"""no_session_ledger.py — durable obligation ledger for no-session revisions.

Spec: agents/beacon/specs/forge-cold-start-revision.md (S1 / M3).

THE GAP THIS CLOSES: a Mirror REVISION on a session-less PR (a `claude/`
PR Forge never built, or a heal-rebuilt envelope) used to route a notify to
Beacon and stop. Nothing mechanically guaranteed the revision was applied,
and the only backstop scraped a log line that had drifted out of match
(#645 / #653). This ledger is the enforcement substrate: every cold-start
revision dispatch OPENS an obligation here; a Mirror PASS / merge / close
RESOLVES it; and the heal-pipeline-stall backstop reads OPEN-and-stale rows
to verify the loop actually closed — instead of trusting an LLM turn or a
log regex.

State file: ``~/agents/state/no-session-revision-ledger.json`` — one JSON
object keyed by ``task_id``. Per-row schema::

    {
      "task_id": "<str>",
      "pr_url": "<str>",
      "branch": "<str>",
      "target_repo": "<str>",          # short repo name (ourliberty-agent-core)
      "head_sha": "<str>|null",        # PR head the dispatch targeted
      "round": <int>,                  # revision round dispatched
      "status": "open"|"resolved",
      "opened_at": <iso8601>,          # first dispatch for this task
      "last_dispatch_at": <iso8601>,   # most recent cold-start dispatch
      "resolved_at": <iso8601>|null,
      "resolution": "<str>|null"       # review-pass | merged | closed | manual
    }

Lifecycle: ``open_obligation`` (idempotent create/update) → ``resolve_obligation``
(review-pass / merge / close). Re-dispatching a later round on the same task
updates ``round`` + ``head_sha`` + ``last_dispatch_at`` and re-opens the row if
it had been resolved. Resolved rows are retained ``_RESOLVED_RETENTION_DAYS``
for audit, then pruned; the whole store is bounded to ``_MAX_ROWS``.

Atomic writes via ``atomic_io.atomic_write_json``. Stdlib only otherwise.
Every reader/writer is fail-safe: a corrupt or unreadable file degrades to an
empty ledger (logged by the caller), never an exception into a daemon/timer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import atomic_io  # noqa: E402  # locked_update: serialize load→mutate→write
import ledger_base  # noqa: E402
# Time helpers are shared verbatim across the sibling ledgers — see ledger_base.
from ledger_base import iso as _iso, now as _now, parse_iso as _parse_iso  # noqa: E402,F401

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
LEDGER_FILE = AGENTS_ROOT / 'state' / 'no-session-revision-ledger.json'

# Retain resolved rows this long for audit/inspection, then prune on the next
# write. Open rows are never pruned by age — a stuck obligation must stay
# visible to the backstop until it actually resolves.
_RESOLVED_RETENTION_DAYS = 7
# Hard cap on total rows so a pathological loop can't grow the file unbounded.
# Oldest-by-last_dispatch_at resolved rows are dropped first; open rows are
# preserved preferentially.
_MAX_ROWS = 500

OPEN = 'open'
RESOLVED = 'resolved'


# -------------------- load / save --------------------
# _now / _iso / _parse_iso are imported from ledger_base (shared verbatim).

def _load() -> dict[str, dict[str, Any]]:
    """Return the ledger object. Fail-safe: any read/parse error → empty dict
    (the obligation is re-opened on the next dispatch anyway; crashing would
    take down whatever daemon called us)."""
    return ledger_base.load(LEDGER_FILE)


def _prune(state: dict[str, dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    """Drop resolved rows older than the retention window, then enforce the
    row cap (dropping oldest resolved rows first; open rows are kept)."""
    cutoff = now - timedelta(days=_RESOLVED_RETENTION_DAYS)
    kept: dict[str, dict[str, Any]] = {}
    for task_id, row in state.items():
        if row.get('status') == RESOLVED:
            resolved_at = _parse_iso(row.get('resolved_at'))
            if resolved_at is not None and resolved_at < cutoff:
                continue  # aged-out resolved row
        kept[task_id] = row

    if len(kept) <= _MAX_ROWS:
        return kept
    # Over cap: NEVER evict an OPEN obligation — losing one would blind the
    # backstop to a stuck PR (the exact silent-loss this ledger exists to
    # prevent). Prune only RESOLVED rows, oldest first, down to the cap. If OPEN
    # rows alone exceed the cap, keep them ALL (a real stuck-PR backlog the
    # backstop must still surface); the cap only bounds RESOLVED-row growth.
    open_rows = {k: v for k, v in kept.items() if v.get('status') == OPEN}
    resolved = [(k, v) for k, v in kept.items() if v.get('status') != OPEN]
    slots = _MAX_ROWS - len(open_rows)
    if slots <= 0:
        return open_rows
    resolved.sort(
        key=lambda kv: kv[1].get('resolved_at')
        or kv[1].get('last_dispatch_at') or ''
    )
    return {**open_rows, **dict(resolved[len(resolved) - slots:])}


def _save(state: dict[str, dict[str, Any]], now: datetime) -> None:
    ledger_base.save(LEDGER_FILE, state, lambda s: _prune(s, now))


# -------------------- lifecycle --------------------

def open_obligation(
    task_id: str,
    *,
    pr_url: str,
    branch: Optional[str] = None,
    target_repo: Optional[str] = None,
    head_sha: Optional[str] = None,
    round_num: int = 1,
    now: Optional[datetime] = None,
) -> None:
    """Record (or refresh) an OPEN no-session-revision obligation for ``task_id``.

    Idempotent: re-opening an existing task preserves ``opened_at`` and bumps
    ``round`` / ``head_sha`` / ``last_dispatch_at``. Re-opening a previously
    resolved task flips it back to OPEN (a fresh REVISION after a PASS is a new
    obligation). Never raises — a ledger write failure must not block the
    dispatch it accompanies."""
    if not task_id:
        return
    n = _now(now)
    try:
        # Serialize the whole load→mutate→write so an overlapping writer (e.g. a
        # notifier restart-rescan) can't clobber this open with a stale copy.
        with atomic_io.locked_update(LEDGER_FILE):
            state = _load()
            existing = state.get(task_id)
            opened_at = (existing or {}).get('opened_at') if isinstance(existing, dict) else None
            if not isinstance(opened_at, str) or not opened_at:
                opened_at = _iso(n)
            state[task_id] = {
                'task_id': task_id,
                'pr_url': pr_url,
                'branch': branch,
                'target_repo': target_repo,
                'head_sha': head_sha,
                'round': int(round_num) if isinstance(round_num, int) else 1,
                'status': OPEN,
                'opened_at': opened_at,
                'last_dispatch_at': _iso(n),
                'resolved_at': None,
                'resolution': None,
            }
            _save(state, n)
    except OSError:
        # Best-effort durability; the dispatch proceeds regardless.
        pass


def resolve_obligation(
    task_id: str,
    *,
    resolution: str = 'review-pass',
    now: Optional[datetime] = None,
) -> bool:
    """Mark ``task_id``'s obligation RESOLVED. Returns True if an OPEN row was
    cleared, False if there was nothing open to clear (unknown task, or already
    resolved). Never raises."""
    if not task_id:
        return False
    n = _now(now)
    try:
        # Serialize load→mutate→write: a dropped resolve would leave a stale OPEN
        # flag suppressing a legit review until its TTL.
        with atomic_io.locked_update(LEDGER_FILE):
            state = _load()
            row = state.get(task_id)
            if not isinstance(row, dict) or row.get('status') != OPEN:
                return False
            row['status'] = RESOLVED
            row['resolved_at'] = _iso(n)
            row['resolution'] = resolution
            state[task_id] = row
            _save(state, n)
            return True
    except OSError:
        return False


def get_obligation(task_id: str) -> Optional[dict[str, Any]]:
    """Return the row for ``task_id`` (open or resolved), or None."""
    if not task_id:
        return None
    return _load().get(task_id)


def list_open(now: Optional[datetime] = None,
              older_than_minutes: Optional[int] = None) -> list[dict[str, Any]]:
    """Return OPEN obligation rows. If ``older_than_minutes`` is given, only
    rows whose ``last_dispatch_at`` is at least that old (the backstop's
    "stuck past grace" filter)."""
    n = _now(now)
    rows = [r for r in _load().values() if r.get('status') == OPEN]
    if older_than_minutes is None:
        return rows
    cutoff = n - timedelta(minutes=older_than_minutes)
    out: list[dict[str, Any]] = []
    for r in rows:
        last = _parse_iso(r.get('last_dispatch_at')) or _parse_iso(r.get('opened_at'))
        if last is not None and last <= cutoff:
            out.append(r)
    return out


# -------------------- CLI (inspection / manual ops) --------------------

def _main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description='no-session-revision obligation ledger')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='list OPEN obligations')
    sub.add_parser('all', help='dump the whole ledger')
    g = sub.add_parser('get', help='show one task'); g.add_argument('task_id')
    r = sub.add_parser('resolve', help='manually resolve a task')
    r.add_argument('task_id')
    r.add_argument('--resolution', default='manual')
    args = p.parse_args(argv)

    if args.cmd == 'list':
        print(json.dumps(list_open(), indent=2, sort_keys=True))
    elif args.cmd == 'all':
        print(json.dumps(_load(), indent=2, sort_keys=True))
    elif args.cmd == 'get':
        print(json.dumps(get_obligation(args.task_id), indent=2, sort_keys=True))
    elif args.cmd == 'resolve':
        ok = resolve_obligation(args.task_id, resolution=args.resolution)
        print(f'resolved={ok}')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
