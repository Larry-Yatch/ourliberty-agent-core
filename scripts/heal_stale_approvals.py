#!/usr/bin/env python3
"""heal_stale_approvals.py — auto-clear resolved decision rows (DAG node N1).

Spec: agents/beacon/specs/approvals-queue-rework.md § 5 (N1 — Auto-clear on
resolution).

The dashboard Approvals tab is append-only: a chain_events row leaves the
pending list ONLY when `read_at` is set, which today happens ONLY when Larry
clicks Approve/Reject/Mark-done. Nothing clears a decision row when the
underlying work resolves on its own, so resolved approvals/clarifications
sit "pending" forever. This job, on a 10-minute timer, sets `read_at` on
pending decision rows whose work has DEMONSTRABLY resolved — never on age,
never on a task_id pattern, ONLY on a real resolution signal.

Signals (grounded in agent-core code, proven in the 2026-06-02 triage):

  approval_request — STALE iff its task_id is in beacon-pending-approvals.json
    `history` (resolved) and NOT in `pending`. The CRITICAL guard: a task_id
    still present in `pending` is NEVER cleared. A task_id in neither bucket
    is UNCERTAIN and left untouched.

  clarify_request — STALE iff EITHER
    (a) a `clarify_response` chain_event exists for the same task_id, OR
    (b) the task "progressed past" the question: the same task_id has an
        approval_request resolved in beacon history at/after the clarify's ts.
    Signal (a) ALONE under-clears — `clarify_response` is a newer event type
    and was not emitted for older rounds (in the 2026-06-02 backlog, (a)-only
    left 19 rows that had all actually shipped). Signal (b) catches those.
    A clarify whose task is in beacon history but resolved BEFORE the clarify
    ts is a fresh question posted after that resolution -> UNCERTAIN, kept.

SAFETY MODEL (mirrors approvals_cleanup.py):
  - Backup FIRST: every row about to be cleared is dumped to a timestamped
    JSON file before any write, so each clear is reversible (read_at -> NULL).
  - Signal-only: a row is cleared ONLY on a confirmed resolution signal.
    Test/mock task_ids (real-*, prod-*) are NOT pattern-matched — they clear
    only if they carry a genuine signal, same as any other row.
  - Idempotent: only `read_at IS NULL` rows are fetched, so an already-cleared
    row is never reconsidered; re-running the tick is a no-op.
  - Conservative on missing state: if beacon-pending-approvals.json is absent
    or malformed, approvals clear nothing and clarifies clear only on a
    `clarify_response`. Never crash, never over-clear.

Applies by default (this is a scheduled healer). Pass --dry-run to inspect
without writing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-stale-approvals.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-stale-approvals.heartbeat'
PENDING_APPROVALS = AGENTS_ROOT / 'state' / 'beacon-pending-approvals.json'
BACKUP_DIR = AGENTS_ROOT / 'blackboard' / 'backups'

UPDATE_BATCH = 200

# Columns fetched per pending decision row. event_id is required for the
# update + backup; the rest give the backup row enough context to audit.
_SELECT_COLS = 'event_id,event_type,agent,task_id,ts,read_at'


# -------------------- logging + heartbeat --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as fh:
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


# -------------------- supabase + beacon state --------------------

def _connect_supabase():
    """Return a Supabase client or raise with a clear message."""
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — '
            'cannot run auto-clear.'
        )
    from supabase import create_client  # type: ignore
    return create_client(url, key)


def fetch_pending(client, event_type: str) -> list[dict[str, Any]]:
    """Page through every pending (read_at IS NULL) row of one event_type."""
    rows: list[dict[str, Any]] = []
    page, size = 0, 1000
    while True:
        resp = (
            client.table('chain_events')
            .select(_SELECT_COLS)
            .is_('read_at', 'null')
            .eq('event_type', event_type)
            .range(page * size, page * size + size - 1)
            .execute()
        )
        batch = getattr(resp, 'data', None) or []
        rows.extend(batch)
        if len(batch) < size:
            break
        page += 1
    return rows


def fetch_clarify_response_ts(client) -> dict[str, list[str]]:
    """task_id -> list of clarify_response timestamps (resolution signal)."""
    resp = (
        client.table('chain_events')
        .select('task_id,ts')
        .eq('event_type', 'clarify_response')
        .execute()
    )
    out: dict[str, list[str]] = defaultdict(list)
    for r in (getattr(resp, 'data', None) or []):
        tid = r.get('task_id')
        if tid:
            out[tid].append(r.get('ts') or '')
    return out


def load_beacon_approvals(
    path: Path = PENDING_APPROVALS,
) -> tuple[set[str], set[str], dict[str, str]]:
    """Return (pending_ids, history_ids, hist_resolved_at).

    hist_resolved_at maps task_id -> latest resolved_at across history entries
    (the progressed-past signal for clarifies). Missing / malformed file is
    treated as empty state (conservative): never raises.
    """
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f'beacon-pending-approvals unreadable ({type(e).__name__}); '
            f'treating approval state as empty — approvals clear nothing this '
            f'tick', 'WARN')
        return set(), set(), {}

    def _tid(entry: dict[str, Any]) -> Optional[str]:
        return entry.get('id') or entry.get('task_id')

    pending = {t for e in data.get('pending', []) if (t := _tid(e))}
    history = {t for e in data.get('history', []) if (t := _tid(e))}
    hist_resolved_at: dict[str, str] = {}
    for e in data.get('history', []):
        tid = _tid(e)
        ra = e.get('resolved_at') or ''
        if tid and ra > hist_resolved_at.get(tid, ''):
            hist_resolved_at[tid] = ra
    return pending, history, hist_resolved_at


# -------------------- classification (pure) --------------------

def classify_approval(
    row: dict[str, Any], pending_ids: set[str], history_ids: set[str],
) -> tuple[bool, str]:
    """(clear?, reason). NEVER clears a task_id still pending in beacon."""
    tid = row.get('task_id')
    if tid in pending_ids:
        return False, 'still pending in beacon-pending-approvals (live)'
    if tid in history_ids:
        return True, 'resolved in beacon history'
    return False, 'no entry in beacon pending or history (uncertain)'


def classify_clarify(
    row: dict[str, Any],
    cresp_ts: dict[str, list[str]],
    hist_resolved_at: dict[str, str],
    history_ids: set[str],
) -> tuple[bool, str]:
    """(clear?, reason). Clears on clarify_response OR progressed-past."""
    tid = row.get('task_id')
    ts = row.get('ts') or ''
    responses = cresp_ts.get(tid, [])
    later = [t for t in responses if t > ts]
    if later:
        return True, f'clarify_response exists after clarify ts ({len(later)})'
    if responses:
        return True, 'clarify_response exists (same/earlier ts)'
    # Progressed-past: the task's approval resolved at/after this clarify's ts.
    ra = hist_resolved_at.get(tid)
    if ra and ra >= ts:
        return True, 'task approval resolved at/after clarify ts (progressed past)'
    if tid in history_ids:
        return False, 'task in beacon history but resolved before clarify ts (uncertain)'
    return False, 'no clarify_response and task not resolved (live)'


# -------------------- apply --------------------

def _backup(rows: list[dict[str, Any]], now: datetime, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime('%Y%m%dT%H%M%SZ')
    path = backup_dir / f'heal-stale-approvals-{stamp}.json'
    with open(path, 'w') as fh:
        json.dump(rows, fh, indent=2, default=str)
    return path


def _apply_clears(client, rows: list[dict[str, Any]], now: datetime) -> int:
    ids = [r['event_id'] for r in rows]
    iso = now.isoformat()
    cleared = 0
    for i in range(0, len(ids), UPDATE_BATCH):
        chunk = ids[i:i + UPDATE_BATCH]
        client.table('chain_events').update({'read_at': iso}) \
            .in_('event_id', chunk).execute()
        cleared += len(chunk)
    return cleared


# -------------------- orchestration --------------------

def run_once(
    client,
    *,
    beacon_state: Optional[tuple[set[str], set[str], dict[str, str]]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    backup_dir: Path = BACKUP_DIR,
) -> dict[str, int]:
    """Single tick. Returns a counts dict. `client` and `beacon_state` are
    injectable for tests; in production both come from the environment."""
    now = now or datetime.now(timezone.utc)
    counts = {
        'pending_approval': 0, 'pending_clarify': 0,
        'clear_approval': 0, 'clear_clarify': 0,
        'kept_live': 0, 'kept_uncertain': 0, 'cleared': 0,
    }

    pending_ids, history_ids, hist_resolved_at = (
        beacon_state if beacon_state is not None else load_beacon_approvals()
    )

    approvals = fetch_pending(client, 'approval_request')
    clarifies = fetch_pending(client, 'clarify_request')
    cresp_ts = fetch_clarify_response_ts(client)
    counts['pending_approval'] = len(approvals)
    counts['pending_clarify'] = len(clarifies)

    to_clear: list[dict[str, Any]] = []
    for r in approvals:
        clear, reason = classify_approval(r, pending_ids, history_ids)
        if clear:
            counts['clear_approval'] += 1
            to_clear.append(r)
        elif 'uncertain' in reason:
            counts['kept_uncertain'] += 1
        else:
            counts['kept_live'] += 1
    for r in clarifies:
        clear, reason = classify_clarify(r, cresp_ts, hist_resolved_at, history_ids)
        if clear:
            counts['clear_clarify'] += 1
            to_clear.append(r)
        elif 'uncertain' in reason:
            counts['kept_uncertain'] += 1
        else:
            counts['kept_live'] += 1

    if to_clear and not dry_run:
        backup_path = _backup(to_clear, now, backup_dir)
        counts['cleared'] = _apply_clears(client, to_clear, now)
        log(f'cleared {counts["cleared"]} resolved decision rows; '
            f'backup -> {backup_path}')

    log('tick: ' + ('DRY-RUN ' if dry_run else '')
        + ' '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


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
        client = _connect_supabase()
    except Exception as e:  # noqa: BLE001
        log(f'cannot connect to Supabase: {e}', 'WARN')
        return 0

    try:
        run_once(client, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
