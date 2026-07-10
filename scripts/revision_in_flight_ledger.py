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
dispatch while a revision is in flight. The flag is cleared by
``_dispatch_mirror_review_rerun`` (the legitimate post-revision re-review) and on
terminal PR verdicts (merged/closed/pass/escalate). A staleness TTL ensures a
Forge that crashes mid-revision does not strand the PR forever: once the TTL
lapses, reviews flow again so the reconcile net can recover the PR.

SUPPRESSION KEYS ON THE FLAG, NOT ON THE PR HEAD. An earlier design suppressed
only while the recorded head still matched the PR's current head, on the theory
that "a landed revision produces a new head, so let the new-head re-review
through." That theory is false in this system: a revision advances the PR head
REPEATEDLY while it is still in flight — Forge pushes a ``[WIP][session-start]``
checkpoint (``worktree_manager``) and incremental work commits before the
revision is done. Head equality is therefore not a proxy for "in flight", and
keying suppression on it reopened the exact duplicate-dispatch window this
ledger exists to close (a mid-revision push looked like a landed revision and
let a concurrent reconcile scan through). The landed-revision re-review is owned
by the rerun clear + terminal clears; the TTL bounds the crash/dead-letter case.
``head_sha`` is retained on the row for inspection/debugging only.

State file: ``~/agents/state/revision-in-flight-ledger.json`` — one JSON object
keyed by ``task_id``. Per-row schema::

    {
      "task_id": "<str>",
      "head_sha": "<str>|null",   # head the revision was dispatched against (diagnostic)
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

# Placeholder task_ids that are NOT real identities. Callers derive the key as
# ``data.get('task_id') or 'unknown'``, so a task-id-less envelope keys on the
# literal 'unknown'. If we honoured that key, every task-id-less PR would share
# ONE flag and one PR's in-flight revision would suppress an UNRELATED PR's
# review until the TTL lapsed. Treat these as "no identity": mark/clear/guard
# all no-op, so a task-id-less PR simply falls back to the round-0 head_sha
# dedup instead of a cross-PR-colliding flag.
_SENTINEL_TASK_IDS = frozenset({'', 'unknown'})


def _is_real_task_id(task_id: Any) -> bool:
    """True when ``task_id`` is a usable per-PR identity (not empty / a shared
    placeholder). Guards every public entry point so the sentinel never becomes
    a cross-PR collision key."""
    return isinstance(task_id, str) and task_id not in _SENTINEL_TASK_IDS


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
    if not _is_real_task_id(task_id):
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
    if not _is_real_task_id(task_id):
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
    now: Optional[datetime] = None,
    ttl_seconds: Optional[int] = None,
) -> bool:
    """Guard predicate: is a Forge revision in flight for ``task_id`` such that a
    NEW ``_dispatch_mirror_review`` (first-review / reconcile path) should be
    suppressed?

    Returns True (suppress) iff a flag exists and is within the TTL:

      - No row / unparseable ``set_at`` / row older than the TTL → False (let
        reviews flow; a crashed- or dead-lettered-mid-revision Forge un-strands
        once the TTL lapses so the reconcile net can recover the PR).
      - Otherwise → True (suppress).

    Deliberately does NOT compare PR head SHAs. A revision advances the PR head
    repeatedly WHILE it is still in flight (Forge's ``[WIP][session-start]``
    checkpoint + incremental work commits), so head equality is not a proxy for
    "in flight" — see the module docstring. The legitimate re-review after a
    revision genuinely lands is dispatched by ``_dispatch_mirror_review_rerun``,
    which clears the flag first; terminal verdicts also clear it. This guard
    only has to answer "is a revision still in flight", and the flag's presence
    (bounded by the TTL) is that answer.

    Never raises."""
    if not _is_real_task_id(task_id):
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
    return True


def get(task_id: str) -> Optional[dict[str, Any]]:
    """Return the row for ``task_id`` (raw, no TTL filtering), or None."""
    if not _is_real_task_id(task_id):
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
