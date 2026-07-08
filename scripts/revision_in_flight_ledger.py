#!/usr/bin/env python3
"""revision_in_flight_ledger.py — durable "Forge revision in flight" flag.

Spec: notifier-concurrent-scan-dup-review-dispatch-001.

THE GAP THIS CLOSES: when Mirror emits REVIEW_REVISION,
``_dispatch_revision_to_forge`` writes a revision task to Forge and the PR
stays OPEN at the SAME head while Forge works. During that window the
concurrent reconcile sweep (``_reconcile_missed_mirror_reviews``) can observe
the PR still open with its round-0 review file archived and re-invoke
``_dispatch_mirror_review`` — spawning a DUPLICATE Mirror review of a PR that
is mid-revision. The legitimate re-review AFTER a revision lands is dispatched
by ``_dispatch_mirror_review_rerun``, never by ``_dispatch_mirror_review``.

This ledger is a durable, ``task_id``-keyed flag set AFTER a revision task is
successfully written to Forge's inbox. A guard at the top of
``_dispatch_mirror_review`` consults it and suppresses a first/reconcile review
dispatch while a revision is in flight AT THE SAME HEAD. The flag is cleared by
``_dispatch_mirror_review_rerun`` (the legitimate post-revision re-review) and on
terminal PR verdicts (merged/closed/pass/escalate). A staleness TTL ensures a
Forge that crashes mid-revision does not strand the PR forever: once the TTL
lapses, reviews flow again so the reconcile net can recover the PR.

State file: ``~/agents/state/revision-in-flight-ledger.json`` — one JSON object
keyed by ``task_id``. Per-row schema::

    {
      "task_id": "<str>",
      "head_sha": "<str>|null",   # PR head the revision was dispatched against
      "round": <int>,             # revision round dispatched
      "pr_url": "<str>|null",
      "set_at": <iso8601>         # when the flag was (re)set — drives the TTL
    }

Rows are transient: ``clear`` DELETES the row (no audit-retention need — the
no-session obligation ledger already provides durable audit for the loop). TTL
lapse prunes stale rows on the next read/write. The store is bounded to
``_MAX_ROWS`` as a pathological-loop backstop.

Atomic writes via ``atomic_io.atomic_write_json``. Stdlib only otherwise. Every
reader/writer is fail-safe: a corrupt or unreadable file degrades to an empty
ledger, never an exception into a daemon/timer.
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

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
LEDGER_FILE = AGENTS_ROOT / 'state' / 'revision-in-flight-ledger.json'

# Staleness TTL. Long enough to cover a real Forge revision, short enough that a
# crashed-mid-revision Forge does not strand the PR: the wedged-session reaper's
# hard_silent_grace is 3600s (60 min; config/review-reaper-rules.json) and its
# no-forward-progress signal frees a wedged build slot at ~25 min — so a genuine
# revision resolves (rerun clears the flag) or the session is reaped well within
# this window. Set modestly ABOVE the reaper's hard grace so the normal clear
# wins the common case, and a stranded flag lapses shortly after the reaper
# would have freed the slot. Overridable via env for ops tuning.
_DEFAULT_TTL_SECONDS = int(
    os.environ.get('REVISION_IN_FLIGHT_TTL_SECONDS', str(90 * 60))
)
# Hard cap on total rows so a pathological loop can't grow the file unbounded.
# Oldest-by-set_at rows are dropped first.
_MAX_ROWS = 500


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

def _load() -> dict[str, dict[str, Any]]:
    """Return the ledger object. Fail-safe: any read/parse error → empty dict
    (a missing flag means "no revision in flight" — the safe default is to let
    reviews flow; crashing would take down whatever daemon called us)."""
    try:
        data = json.loads(LEDGER_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _prune(state: dict[str, dict[str, Any]], now: datetime,
           ttl_seconds: int) -> dict[str, dict[str, Any]]:
    """Drop rows whose flag has lapsed the TTL, then enforce the row cap
    (dropping oldest-by-set_at first)."""
    cutoff = now - timedelta(seconds=ttl_seconds)
    kept: dict[str, dict[str, Any]] = {}
    for task_id, row in state.items():
        set_at = _parse_iso(row.get('set_at'))
        # A row with no parseable set_at is treated as stale (can't age it) and
        # dropped — a flag we can't TTL is worse than no flag.
        if set_at is None or set_at < cutoff:
            continue
        kept[task_id] = row

    if len(kept) <= _MAX_ROWS:
        return kept
    ordered = sorted(kept.items(), key=lambda kv: kv[1].get('set_at') or '')
    return dict(ordered[len(ordered) - _MAX_ROWS:])


def _save(state: dict[str, dict[str, Any]], now: datetime,
          ttl_seconds: int) -> None:
    pruned = _prune(state, now, ttl_seconds)
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.atomic_write_json(LEDGER_FILE, pruned, sort_keys=True)


# -------------------- lifecycle --------------------

def mark_in_flight(
    task_id: str,
    *,
    head_sha: Optional[str] = None,
    pr_url: Optional[str] = None,
    round_num: int = 1,
    now: Optional[datetime] = None,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Record (or refresh) an in-flight revision flag for ``task_id``.

    Called AFTER a revision task is successfully written to Forge's inbox.
    Idempotent: re-marking bumps ``head_sha`` / ``round`` / ``set_at`` (a later
    revision round is a fresh in-flight window). Never raises — a ledger write
    failure must not block the dispatch it accompanies."""
    if not task_id:
        return
    n = _now(now)
    ttl = ttl_seconds if isinstance(ttl_seconds, int) else _DEFAULT_TTL_SECONDS
    try:
        state = _load()
        state[task_id] = {
            'task_id': task_id,
            'head_sha': head_sha if isinstance(head_sha, str) and head_sha else None,
            'round': int(round_num) if isinstance(round_num, int) else 1,
            'pr_url': pr_url if isinstance(pr_url, str) and pr_url else None,
            'set_at': _iso(n),
        }
        _save(state, n, ttl)
    except OSError:
        # Best-effort durability; the dispatch proceeds regardless.
        pass


def clear(task_id: str, now: Optional[datetime] = None,
          ttl_seconds: Optional[int] = None) -> bool:
    """Clear ``task_id``'s in-flight flag (DELETE the row). Returns True if a row
    was removed, False if there was nothing to clear. Never raises.

    Called by the legitimate re-review path (``_dispatch_mirror_review_rerun``)
    and on terminal PR verdicts (merged/closed/pass/escalate)."""
    if not task_id:
        return False
    n = _now(now)
    ttl = ttl_seconds if isinstance(ttl_seconds, int) else _DEFAULT_TTL_SECONDS
    try:
        state = _load()
        if task_id not in state:
            return False
        del state[task_id]
        _save(state, n, ttl)
        return True
    except OSError:
        return False


def is_in_flight(
    task_id: str,
    *,
    current_head_sha: Optional[str] = None,
    now: Optional[datetime] = None,
    ttl_seconds: Optional[int] = None,
) -> bool:
    """Guard predicate: is a Forge revision in flight for ``task_id`` such that a
    NEW ``_dispatch_mirror_review`` should be suppressed?

    Returns True (suppress) iff a flag exists, is within the TTL, AND the PR has
    NOT advanced past the recorded in-flight head:

      - No row / unparseable ``set_at`` / row older than the TTL → False (let
        reviews flow; a crashed-mid-revision Forge un-strands once the TTL
        lapses so the reconcile net can recover the PR).
      - Recorded head and ``current_head_sha`` are both known and DIFFER → the
        revision has landed (new head) → False, so the legitimate new-head
        re-review is never suppressed. This is the guard's answer to design
        question (1): the flag keys on ``task_id`` but the head comparison lets
        a new-head re-review through even before the rerun clear fires.
      - Heads match, or either head is unknown → True (suppress). The
        either-unknown case is conservative on purpose: a gh hiccup that hides
        the current head should not open the duplicate-dispatch window the flag
        exists to close; the reconcile sweep retries and the TTL bounds it.

    Never raises."""
    if not task_id:
        return False
    n = _now(now)
    ttl = ttl_seconds if isinstance(ttl_seconds, int) else _DEFAULT_TTL_SECONDS
    row = _load().get(task_id)
    if not isinstance(row, dict):
        return False
    set_at = _parse_iso(row.get('set_at'))
    if set_at is None:
        return False
    if set_at < n - timedelta(seconds=ttl):
        return False  # TTL lapsed
    recorded_head = row.get('head_sha')
    if (
        isinstance(recorded_head, str) and recorded_head
        and isinstance(current_head_sha, str) and current_head_sha
        and recorded_head != current_head_sha
    ):
        return False  # PR advanced past the in-flight head — revision landed
    return True


def get(task_id: str) -> Optional[dict[str, Any]]:
    """Return the row for ``task_id`` (raw, no TTL filtering), or None."""
    if not task_id:
        return None
    return _load().get(task_id)


# -------------------- CLI (inspection / manual ops) --------------------

def _main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description='revision-in-flight flag ledger')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('all', help='dump the whole ledger')
    g = sub.add_parser('get', help='show one task'); g.add_argument('task_id')
    c = sub.add_parser('clear', help='manually clear a task flag')
    c.add_argument('task_id')
    args = p.parse_args(argv)

    if args.cmd == 'all':
        print(json.dumps(_load(), indent=2, sort_keys=True))
    elif args.cmd == 'get':
        print(json.dumps(get(args.task_id), indent=2, sort_keys=True))
    elif args.cmd == 'clear':
        print(f'cleared={clear(args.task_id)}')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
