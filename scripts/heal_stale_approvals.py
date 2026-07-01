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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supabase_chunk import chunked_clear, ChunkedClearError  # noqa: E402
import beacon_approval_handler as approval  # noqa: E402
import task_terminal_state as tts  # noqa: E402 — shared terminal-state probe
from decision_identity import canonical_decision_key  # noqa: E402

# Resolve the approval-state root identically to the other two modules in the
# pending-approvals trio (beacon_approval_handler, heal_unregistered_approval):
# `or`-fallback so an EMPTY OURLIBERTY_AGENTS_ROOT falls through to ~/agents
# instead of being taken literally as Path('') = cwd. The bare `get(.., default)`
# form treats '' as set; that was the lone trio outlier left after audit L1.
# On the droplet Path.home() is /home/larry, so prod resolution is unchanged.
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or Path.home() / 'agents')
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-stale-approvals.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-stale-approvals.heartbeat'
PENDING_APPROVALS = AGENTS_ROOT / 'state' / 'beacon-pending-approvals.json'
BACKUP_DIR = AGENTS_ROOT / 'blackboard' / 'backups'

UPDATE_BATCH = 200

# Terminal-state reconciliation (spec terminal-state-reconciliation.md § 3.1).
# A pending approval is only probed against its work's terminal ground truth
# once it has aged past this grace window — fresh approvals are awaiting Larry,
# not phantoms. 2h matches the spec's suggested grace.
TERMINAL_APPROVAL_GRACE_HOURS = 2.0

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
    from supabase_factory import get_supabase_client  # type: ignore
    return get_supabase_client(url, key)


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


# Larry resolved an approval on the DASHBOARD when a `larry_action` audit row
# (written by _handle_larry_action) exists for its task_id. Only the binary
# decision actions count — comment/mark_done are not approval resolutions.
_DECISION_ACTIONS = {'approve', 'reject'}
_ACTION_TO_STATUS = {'approve': 'approved', 'reject': 'rejected'}


def fetch_larry_actions(client, task_ids: set[str]) -> dict[str, str]:
    """task_id -> latest dashboard decision action ('approve'|'reject'), scoped
    to `task_ids`. Reads `larry_action` audit rows (written by the dashboard
    approve/reject handler with task_id = the source approval_request's task_id).
    Chunks the `.in_()` filter so a large pending set never builds an oversized
    query. Rows with a non-decision action or missing task_id are ignored."""
    wanted = sorted({t for t in (task_ids or set()) if t})
    if not wanted:
        return {}
    # Phase 2 re-key: the returned dict is keyed by the CANONICAL decision key
    # (stamped `payload.decision_key`, else derived from task_id+pr_url), not the
    # raw task_id — so a dashboard larry_action stamped with a PR coordinate joins
    # a pending entry whose id is the wrapped `mirror-review:…` form. `wanted` may
    # carry both raw task_ids and canonical keys (the caller adds both) so the
    # task_id-column filter still catches the row whichever shape it was written
    # under.
    latest: dict[str, tuple[str, str]] = {}  # decision_key -> (ts, action)
    chunk = 200
    for i in range(0, len(wanted), chunk):
        resp = (
            client.table('chain_events')
            .select('task_id,ts,payload,pr_url')
            .eq('event_type', 'larry_action')
            .in_('task_id', wanted[i:i + chunk])
            .execute()
        )
        for r in (getattr(resp, 'data', None) or []):
            tid = r.get('task_id')
            payload = r.get('payload')
            action = payload.get('action') if isinstance(payload, dict) else None
            if not tid or action not in _DECISION_ACTIONS:
                continue
            stamped = payload.get('decision_key') if isinstance(payload, dict) else None
            key = stamped or canonical_decision_key(tid, r.get('pr_url'))
            if not key:
                continue
            ts = r.get('ts') or ''
            prev = latest.get(key)
            if prev is None or ts >= prev[0]:
                latest[key] = (ts, action)
    return {key: act for key, (_ts, act) in latest.items()}


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
    """Set read_at on every row, batched. Returns the count cleared.

    Delegates to the shared `chunked_clear`, which on a mid-loop chunk failure
    raises ChunkedClearError carrying the cleared-so-far list. Callers back the
    rows up FIRST, so that partial state is reversible from the backup."""
    ids = [r['event_id'] for r in rows]
    cleared = chunked_clear(
        client, 'chain_events', ids, {'read_at': now.isoformat()},
        chunk_size=UPDATE_BATCH,
    )
    return len(cleared)


def clear_resolved_by_task_id(
    client,
    task_ids,
    now: Optional[datetime] = None,
    backup_dir: Path = BACKUP_DIR,
) -> int:
    """Set read_at on the pending approval_request rows for these task_ids.

    The shared, signal-driven clear path other reconcilers reuse instead of
    hand-writing a Supabase update: it backs the rows up FIRST (reversible),
    then clears in batches via `_apply_clears`. Caller is responsible for the
    resolution decision; this only mechanizes the read_at write. Returns the
    number of rows cleared (0 if none match)."""
    wanted = {t for t in (task_ids or []) if t}
    if not wanted:
        return 0
    now = now or datetime.now(timezone.utc)
    rows = [
        r for r in fetch_pending(client, 'approval_request')
        if r.get('task_id') in wanted
    ]
    if not rows:
        return 0
    backup_path = _backup(rows, now, backup_dir)
    try:
        cleared = _apply_clears(client, rows, now)
    except ChunkedClearError as e:
        log(f'PARTIAL clear: {e.cleared_count}/{e.total} retired decision '
            f'row(s) cleared before failure ({e.cause}); reversible from '
            f'backup -> {backup_path}', 'ERROR')
        raise
    log(f'cleared {cleared} retired decision row(s) by task_id; '
        f'backup -> {backup_path}')
    return cleared


# -------------------- terminal-state reconciliation (spec § 3.1) --------------------

def _entry_task_id(entry: dict[str, Any]) -> Optional[str]:
    """The work's stable task_id for a pending entry: the dispatch_payload's
    task_id (what the PR branch/title carry), falling back to the entry id (which
    add_pending sets to payload['task_id'], so they normally agree)."""
    payload = entry.get('dispatch_payload')
    if isinstance(payload, dict):
        tid = payload.get('task_id')
        if isinstance(tid, str) and tid:
            return tid
    eid = entry.get('id')
    return eid if isinstance(eid, str) and eid else None


def _entry_decision_key(entry: dict[str, Any]) -> Optional[str]:
    """The canonical decision key for a pending entry: the stamped
    `decision_key` if present (Phase 2 emit-time stamp), else derived from the
    entry's task_id + dispatch_payload pr_url. This is the join key against the
    re-keyed `fetch_larry_actions` map."""
    stamped = entry.get('decision_key')
    if isinstance(stamped, str) and stamped:
        return stamped
    payload = entry.get('dispatch_payload')
    pr_url = payload.get('pr_url') if isinstance(payload, dict) else None
    return canonical_decision_key(_entry_task_id(entry), pr_url)


def _approval_age_hours(entry: dict[str, Any], now: datetime) -> Optional[float]:
    """Hours since the entry was created, or None if created_at is missing or
    unparseable (which the caller treats as KEEP — never retire on a bad ts)."""
    created_raw = entry.get('created_at')
    if not isinstance(created_raw, str) or not created_raw:
        return None
    try:
        created = approval._parse_iso(created_raw)
    except (ValueError, TypeError):
        return None
    return (now - created).total_seconds() / 3600.0


def classify_terminal_approval(
    entry: dict[str, Any],
    now: datetime,
    grace_hours: float,
    probe: Any,
) -> tuple[bool, str]:
    """(retire?, reason) for one pending approval against its work's terminal
    ground truth. Conservative posture (spec § 1): retire ONLY when the work is
    positively terminal (MERGED/CLOSED) AND the entry has aged past the grace
    window. EVERY other path — missing/unparseable created_at, within grace,
    missing task_id, OPEN, or UNKNOWN/indeterminate — is KEEP. An indeterminate
    probe can only ever leave a phantom for another cycle, never falsely retire
    live work."""
    age = _approval_age_hours(entry, now)
    if age is None:
        return False, 'no/unparseable created_at (keep)'
    if age < grace_hours:
        return False, f'within grace ({age:.1f}h < {grace_hours}h) (keep)'
    task_id = _entry_task_id(entry)
    if not task_id:
        return False, 'no task_id to probe (keep)'
    state = probe(task_id)
    if state in tts.TERMINAL_STATES:
        return True, f'work terminal ({state}) past {grace_hours}h grace'
    return False, f'work {state} (not terminal) (keep)'


def reconcile_terminal_approvals(
    *,
    beacon_state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    grace_hours: float = TERMINAL_APPROVAL_GRACE_HOURS,
    probe: Any = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Spec § 3.1: retire pending approvals whose work has reached a terminal
    state. For each `pending[]` entry past the grace window, probe
    `task_terminal_state(task_id)`; on MERGED/CLOSED resolve it `expired` (which
    self-clears the dashboard row) — OPEN/UNKNOWN keep.

    This is the missing ground-truth check on the one store
    (beacon-pending-approvals.json `pending[]`) the rest of this healer is
    forbidden to touch. It is ADDITIVE to the happy-path `resolve()` — it only
    catches approvals whose linking event was missed.

    `beacon_state` / `now` / `probe` are injectable for tests; in production they
    come from `approval.load_state()`, the wall clock, and
    `task_terminal_state`. Returns a counts dict."""
    now = now or datetime.now(timezone.utc)
    probe = probe or (lambda tid: tts.task_terminal_state(tid))
    counts = {'pending': 0, 'retired': 0, 'kept': 0}

    state = beacon_state if beacon_state is not None else approval.load_state()
    pending = state.get('pending', []) if isinstance(state, dict) else []
    counts['pending'] = len(pending)

    to_retire: list[tuple[str, str]] = []
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        retire, reason = classify_terminal_approval(entry, now, grace_hours, probe)
        approval_id = entry.get('id')
        if retire and isinstance(approval_id, str) and approval_id:
            to_retire.append((approval_id, reason))
        else:
            counts['kept'] += 1

    for approval_id, reason in to_retire:
        if dry_run:
            counts['retired'] += 1
            log(f'DRY-RUN would retire pending approval {approval_id} ({reason})')
            continue
        try:
            # state=None: resolve self-locks, self-saves, AND best-effort clears
            # the dashboard's pending approval_request row (spec § 3.1 "clear the
            # dashboard row"). Re-loads fresh state under the lock internally, so
            # passing no state here keeps the slow gh probes above out of the lock.
            resolved = approval.resolve(
                approval_id, 'expired',
                note=f'auto-retired by heal-stale-approvals (terminal-state): {reason}',
            )
        except Exception as e:  # noqa: BLE001
            log(f'terminal-retire failed for {approval_id}: '
                f'{type(e).__name__}: {e}', 'ERROR')
            continue
        if resolved is None:
            # Already gone from pending (a concurrent resolve / prior tick). Not
            # an error — the phantom is cleared either way.
            log(f'terminal-retire: {approval_id} no longer pending ({reason})')
        counts['retired'] += 1
        log(f'terminal-retired pending approval {approval_id} ({reason})')

    log('terminal-approval reconcile: ' + ('DRY-RUN ' if dry_run else '')
        + ' '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


# -------------------- resolved-in-supabase reconciliation (approval-sync §3.2) --

def reconcile_resolved_in_supabase(
    client,
    *,
    beacon_state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Pop `pending[]` entries Larry already resolved on the DASHBOARD.

    The dashboard approve/reject path (_handle_larry_action) sets read_at on the
    approval_request row, writes a dispatch envelope, and emits a `larry_action`
    audit row — but it never pops beacon-pending-approvals.json `pending[]`. That
    pop relied on a best-effort Beacon LLM session, so a missed session left the
    entry pending forever; the State Log / doorbell then re-nagged Larry about a
    decision he'd already made (the 2026-06-30 "4 items need your call" phantoms).

    This closes that gap deterministically: for each pending entry whose task_id
    carries a terminal `larry_action` (approve/reject) in chain_events, resolve()
    it to history with the matching status. It is ADDITIVE to the Telegram
    self-pop (which never writes a larry_action) and the terminal-state retire,
    and idempotent — resolve() returns None if the entry is already gone.

    `client` / `beacon_state` / `now` are injectable for tests. Returns counts."""
    now = now or datetime.now(timezone.utc)
    counts = {'pending': 0, 'resolved': 0, 'kept': 0}

    state = beacon_state if beacon_state is not None else approval.load_state()
    pending = state.get('pending', []) if isinstance(state, dict) else []
    counts['pending'] = len(pending)

    # Map each pending entry's id -> its CANONICAL decision key (the re-keyed
    # larry_action join key). `query_tids` carries both the raw task_id AND the
    # canonical key for every entry, because the chain_events `task_id` column is
    # filtered server-side — the row may have been written under either shape, so
    # we must query for both to be sure the larry_action is fetched.
    id_to_key: dict[str, str] = {}
    query_tids: set[str] = set()
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        approval_id = entry.get('id')
        if not (isinstance(approval_id, str) and approval_id):
            continue
        key = _entry_decision_key(entry)
        if not key:
            continue
        id_to_key[approval_id] = key
        query_tids.add(key)
        tid = _entry_task_id(entry)
        if tid:
            query_tids.add(tid)
    if not id_to_key:
        log('resolved-in-supabase reconcile: no pending entries to check')
        return counts

    decided = fetch_larry_actions(client, query_tids)

    to_resolve: list[tuple[str, str]] = []  # (approval_id, new_status)
    for approval_id, key in id_to_key.items():
        action = decided.get(key)
        if action in _ACTION_TO_STATUS:
            to_resolve.append((approval_id, _ACTION_TO_STATUS[action]))
        else:
            counts['kept'] += 1

    for approval_id, new_status in to_resolve:
        if dry_run:
            counts['resolved'] += 1
            log(f'DRY-RUN would pop pending approval {approval_id} '
                f'(dashboard larry_action={new_status} in chain_events)')
            continue
        try:
            # state=None: resolve self-locks + self-saves + best-effort clears the
            # dashboard row. Re-loads fresh under the lock, so a concurrent Beacon
            # write can't be clobbered by a stale snapshot read above.
            resolved = approval.resolve(
                approval_id, new_status,
                note=('auto-popped by heal-stale-approvals (resolved-in-supabase): '
                      'dashboard larry_action found in chain_events; the '
                      'dashboard-approve path never popped pending[]'),
            )
        except Exception as e:  # noqa: BLE001
            log(f'resolved-in-supabase pop failed for {approval_id}: '
                f'{type(e).__name__}: {e}', 'ERROR')
            continue
        if resolved is None:
            # Already gone (Beacon session or a prior tick popped it). Not an
            # error — the phantom is cleared either way.
            log(f'resolved-in-supabase: {approval_id} no longer pending')
        counts['resolved'] += 1
        log(f'popped pending approval {approval_id} -> {new_status} '
            f'(dashboard larry_action in chain_events)')

    log('resolved-in-supabase reconcile: ' + ('DRY-RUN ' if dry_run else '')
        + ' '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


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
        try:
            counts['cleared'] = _apply_clears(client, to_clear, now)
        except ChunkedClearError as e:
            log(f'PARTIAL clear: {e.cleared_count}/{e.total} resolved decision '
                f'row(s) cleared before failure ({e.cause}); reversible from '
                f'backup -> {backup_path}', 'ERROR')
            raise
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

    rc = 0

    # § 3.1 terminal-state reconcile runs FIRST and independent of Supabase: it
    # needs only gh + the local beacon-pending-approvals.json + a best-effort
    # resolve (whose dashboard clear self-suppresses when Supabase is down). So a
    # Supabase outage must not skip the phantom-approval cleanup.
    try:
        reconcile_terminal_approvals(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f'terminal-approval reconcile FAILED: {type(e).__name__}: {e}', 'ERROR')
        rc = 1

    # The original Supabase-backed auto-clear (resolved decision rows on the
    # dashboard). Degrades to a clean skip when Supabase is unavailable.
    try:
        client = _connect_supabase()
    except Exception as e:  # noqa: BLE001
        log(f'cannot connect to Supabase: {e}', 'WARN')
        return rc

    try:
        run_once(client, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f'run_once FAILED: {type(e).__name__}: {e}', 'ERROR')
        rc = 1

    # § 3.2: pop pending[] entries Larry already resolved on the dashboard
    # (larry_action in chain_events but no pending pop). Independent of run_once
    # so a failure there still lets already-approved phantoms clear.
    try:
        reconcile_resolved_in_supabase(client, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f'resolved-in-supabase reconcile FAILED: {type(e).__name__}: {e}',
            'ERROR')
        rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
