#!/usr/bin/env python3
"""chain_events_retention.py — retention on chain_events bookkeeping rows (DAG node N2).

Spec: agents/beacon/specs/approvals-queue-rework.md § 5 (N2 — Retention on
chain_events).

The chain_events table accumulates ~3,200 session_start / session_done
bookkeeping rows that never get `read_at` set, so they grow the table
unbounded (invisible to the Approvals tab, but real). This job, on a daily
timer, ARCHIVES (full-row dump to a timestamped file) then DELETES bookkeeping
rows older than a configurable retention window — genuine data retention, not
a `read_at` clear.

RETENTION SCOPE
  - Only event types in config/chain-events-retention.json
    `bookkeeping_event_types` (default: session_start, session_done) are
    candidates. The list is config-tunable, intended to become Pulse-Check
    self-optimizing per the Check III/VIII pattern.
  - Only rows OLDER than `retention_days` (default 14) are candidates.

SAFETY MODEL (mirrors approvals_cleanup.py / heal_stale_approvals.py):
  - DECISION rows are NEVER touched. approval_request / clarify_request are
    hard-excluded from deletion regardless of what the config lists — a
    misconfigured `bookkeeping_event_types` cannot delete a pending decision.
    This is the load-bearing guard for "NEVER archives a decision row that is
    still pending."
  - Archive FIRST: every row about to be deleted is dumped (ALL columns,
    including payload) to a timestamped JSON file before any delete, so each
    retention pass is reversible by re-inserting from the archive.
  - Window from config, NOT hardcoded: a missing / malformed config falls back
    to the conservative defaults (14 days, session_start/session_done) and
    never crashes.
  - Idempotent: each tick re-selects only rows matching (bookkeeping type AND
    ts < cutoff); once deleted they are gone, so re-running is a no-op.

Applies by default (this is a scheduled job). Pass --dry-run to inspect
without writing.
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
from atomic_io import atomic_write_text  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'chain-events-retention.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'chain-events-retention.heartbeat'
ARCHIVE_DIR = AGENTS_ROOT / 'blackboard' / 'retention-archive'

REPO_ROOT = Path(__file__).resolve().parent.parent
RETENTION_CONFIG = REPO_ROOT / 'config' / 'chain-events-retention.json'

DEFAULT_RETENTION_DAYS = 14
DEFAULT_BOOKKEEPING_TYPES = ('session_start', 'session_done')

# Decision rows ALWAYS need Larry; they are never retention-eligible no matter
# what the config says. This is the hard guard, not just a default.
DECISION_TYPES = frozenset({'approval_request', 'clarify_request'})

DELETE_BATCH = 200

# Full row (incl. payload) so the archive is restorable by re-insert.
_SELECT_COLS = '*'


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


# -------------------- supabase + config --------------------

def _connect_supabase():
    """Return a Supabase client or raise with a clear message."""
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — '
            'cannot run retention.'
        )
    from supabase import create_client  # type: ignore
    return create_client(url, key)


def load_retention_config(
    path: Path = RETENTION_CONFIG,
) -> tuple[int, list[str]]:
    """Return (retention_days, bookkeeping_types) from config.

    Window and type list come from config, never hardcoded at the call site.
    A missing / malformed file, or any individually invalid field, falls back
    to the conservative defaults for that field and never raises. Decision
    types are stripped from the list here too (belt; the run_once guard is the
    suspenders).
    """
    days = DEFAULT_RETENTION_DAYS
    types: list[str] = list(DEFAULT_BOOKKEEPING_TYPES)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f'retention config unreadable ({type(e).__name__}); using defaults '
            f'(days={days}, types={types})', 'WARN')
        return days, types

    raw_days = data.get('retention_days')
    if isinstance(raw_days, int) and not isinstance(raw_days, bool) and raw_days > 0:
        days = raw_days
    else:
        log(f'retention_days invalid ({raw_days!r}); using default {days}', 'WARN')

    raw_types = data.get('bookkeeping_event_types')
    if isinstance(raw_types, list) and raw_types and all(
        isinstance(t, str) and t for t in raw_types
    ):
        types = [t for t in raw_types if t not in DECISION_TYPES]
        dropped = [t for t in raw_types if t in DECISION_TYPES]
        if dropped:
            log(f'ignoring decision types in bookkeeping_event_types: {dropped} '
                f'— decision rows are never retention-eligible', 'WARN')
        if not types:
            log('bookkeeping_event_types resolved empty after dropping decision '
                f'types; using defaults {list(DEFAULT_BOOKKEEPING_TYPES)}', 'WARN')
            types = list(DEFAULT_BOOKKEEPING_TYPES)
    else:
        log(f'bookkeeping_event_types invalid ({raw_types!r}); using default '
            f'{types}', 'WARN')

    return days, types


def fetch_expired_bookkeeping(
    client, event_type: str, cutoff_iso: str,
) -> list[dict[str, Any]]:
    """Page through every bookkeeping row of one event_type older than cutoff."""
    rows: list[dict[str, Any]] = []
    page, size = 0, 1000
    while True:
        resp = (
            client.table('chain_events')
            .select(_SELECT_COLS)
            .eq('event_type', event_type)
            .lt('ts', cutoff_iso)
            .range(page * size, page * size + size - 1)
            .execute()
        )
        batch = getattr(resp, 'data', None) or []
        rows.extend(batch)
        if len(batch) < size:
            break
        page += 1
    return rows


# -------------------- apply --------------------

def _archive(rows: list[dict[str, Any]], now: datetime, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime('%Y%m%dT%H%M%SZ')
    path = archive_dir / f'chain-events-retention-{stamp}.json'
    # default=str: rows may carry datetime/Decimal from the DB client, which
    # atomic_write_json can't serialize — render here, then write atomically so
    # a mid-archive crash can't leave a torn file (preserves the "archive
    # FIRST → reversible" guarantee before _apply_deletes runs).
    atomic_write_text(path, json.dumps(rows, indent=2, default=str))
    return path


def _apply_deletes(client, rows: list[dict[str, Any]]) -> int:
    ids = [r['event_id'] for r in rows]
    deleted = 0
    for i in range(0, len(ids), DELETE_BATCH):
        chunk = ids[i:i + DELETE_BATCH]
        client.table('chain_events').delete().in_('event_id', chunk).execute()
        deleted += len(chunk)
    return deleted


# -------------------- orchestration --------------------

def run_once(
    client,
    *,
    config: Optional[tuple[int, list[str]]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    archive_dir: Path = ARCHIVE_DIR,
) -> dict[str, Any]:
    """Single retention tick. Returns a counts dict. `client` and `config` are
    injectable for tests; in production both come from the environment."""
    now = now or datetime.now(timezone.utc)
    retention_days, bookkeeping_types = (
        config if config is not None else load_retention_config()
    )
    # Hard guard (suspenders): never let a decision type into the delete set,
    # even if an injected config slipped one through.
    bookkeeping_types = [t for t in bookkeeping_types if t not in DECISION_TYPES]

    cutoff = now - timedelta(days=retention_days)
    cutoff_iso = cutoff.isoformat()

    counts: dict[str, Any] = {
        'retention_days': retention_days,
        'cutoff': cutoff_iso,
        'candidates': 0,
        'protected_skipped': 0,
        'archived': 0,
        'deleted': 0,
    }

    candidates: list[dict[str, Any]] = []
    for et in bookkeeping_types:
        candidates.extend(fetch_expired_bookkeeping(client, et, cutoff_iso))
    counts['candidates'] = len(candidates)

    # Final defensive filter: drop anything that is a decision type. With the
    # type-scoped fetch this should always be empty; if it is not, something is
    # badly wrong and we log loudly and refuse to delete those rows.
    to_delete = [r for r in candidates if r.get('event_type') not in DECISION_TYPES]
    protected = len(candidates) - len(to_delete)
    if protected:
        counts['protected_skipped'] = protected
        log(f'REFUSED to delete {protected} decision-type row(s) that appeared '
            f'in the candidate set — guard tripped', 'ERROR')

    if to_delete and not dry_run:
        archive_path = _archive(to_delete, now, archive_dir)
        counts['archived'] = len(to_delete)
        counts['deleted'] = _apply_deletes(client, to_delete)
        log(f'archived + deleted {counts["deleted"]} bookkeeping rows older than '
            f'{retention_days}d; archive -> {archive_path}')

    log('tick: ' + ('DRY-RUN ' if dry_run else '')
        + ' '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='select + report but write nothing')
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
