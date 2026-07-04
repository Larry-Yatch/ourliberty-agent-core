#!/usr/bin/env python3
"""for_larry_escalations.py — durable, self-clearing "needs you" record store.

Spec: agents/beacon/specs/mirror-review-visibility.md (Contracts C + D, the
action-needed bucket) — and forward-compatible with operator-needs-you-feed.md
§5 (the `/where-we-are` "Waiting on You" panel + doorbell).

THE GAP THIS CLOSES: when a session-less PR's Mirror revision cannot
self-heal mechanically (no chain envelope to re-dispatch, or recovery already
failed), the only thing that reaches Larry must land on a surface he actually
watches — not a PR comment. operator-needs-you-feed names a single
"needs-you" gate fed by a durable for-Larry record on a canonical local signal
file; this module is the *producer* for that record, written at the routing
site, with the self-clearing semantics decision (d) requires.

CANONICAL FILE: ``~/agents/blackboard/for-larry-escalations.json`` (override
with ``OURLIBERTY_FOR_LARRY_FEED_FILE``). Schema is deliberately the SHAPE
``system_state_log.load_for_larry_escalations`` already consumes — a dict with
an ``escalations`` list whose rows carry ``for_larry``/``resolved``/
``headline``/``id``/``context``/``severity``/``ts`` — so the
operator-needs-you-feed §5.1 reader fold-in reads it with no schema change::

    {
      "escalations": [
        {
          "id": "<stable record id, keyed on PR+task>",
          "source": "mirror-review",
          "headline": "<title Larry sees>",
          "context": "<copy-paste next step>",
          "severity": "warning",
          "pr_url": "<deep-link>",
          "head_sha": "<PR head the record was written for>",
          "dedup_identity": "<PR+head SHA — Contract D idempotency>",
          "for_larry": true,
          "resolved": false,
          "ts": "<iso8601 created>",
          "updated_at": "<iso8601 last write>"
        }
      ]
    }

Self-clearing (decision d): ``clear`` flips ``resolved: true`` when the
trigger clears (the no-session obligation resolves, or a fresh dispatch is
observed). A resolved row is retained for audit, pruned after
``_RESOLVED_RETENTION_DAYS``.

Atomic writes via ``atomic_io``. Every reader/writer is fail-safe: a corrupt
or unreadable file degrades to an empty feed, never an exception into the
notifier daemon it runs inside.
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
import file_lock  # noqa: E402
import prod_write_guard  # noqa: E402  # refuse live-tree writes under pytest


def _decision_key_for(record_id: Optional[str], pr_url: Optional[str]) -> Optional[str]:
    """Compute the canonical cross-store join key for this record (Phase 2
    Change A). Imported lazily + fail-safe: decision_identity is pure, but a
    stamp must never turn a routing-site upsert into an exception."""
    try:
        from decision_identity import canonical_decision_key
        return canonical_decision_key(record_id, pr_url)
    except Exception:  # noqa: BLE001 — stamping is best-effort, never fatal
        return None


AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))

# Retain resolved rows this long for audit, then prune on the next write.
_RESOLVED_RETENTION_DAYS = 7
# Hard cap so a pathological loop can't grow the file unbounded.
_MAX_ROWS = 500


# -------------------- path --------------------

def feed_path() -> Path:
    """Canonical for-Larry signal file. ``OURLIBERTY_FOR_LARRY_FEED_FILE``
    overrides for tests; otherwise the blackboard file the panel reads."""
    override = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
    if override:
        return Path(override)
    return AGENTS_ROOT / 'blackboard' / 'for-larry-escalations.json'


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

def _load() -> list[dict[str, Any]]:
    """Return the escalations rows. Fail-safe: any read/parse error → [] (the
    record is re-written on the next routing pass anyway; crashing would take
    down the notifier that called us)."""
    try:
        data = json.loads(feed_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    rows = data.get('escalations') if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _prune(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Drop resolved rows older than the retention window, then enforce the row
    cap (oldest resolved first; open rows are kept preferentially)."""
    cutoff = now - timedelta(days=_RESOLVED_RETENTION_DAYS)
    kept: list[dict[str, Any]] = []
    for row in rows:
        if row.get('resolved') is True:
            resolved_at = _parse_iso(row.get('resolved_at'))
            if resolved_at is not None and resolved_at < cutoff:
                continue
        kept.append(row)
    if len(kept) <= _MAX_ROWS:
        return kept
    open_rows = [r for r in kept if r.get('resolved') is not True]
    resolved = [r for r in kept if r.get('resolved') is True]
    slots = _MAX_ROWS - len(open_rows)
    if slots <= 0:
        return open_rows
    resolved.sort(key=lambda r: r.get('resolved_at') or r.get('updated_at') or '')
    return open_rows + resolved[len(resolved) - slots:]


def _feed_lock():
    """The SAME sidecar lock `for_larry_signal` takes on this file, so the two
    producers of `for-larry-escalations.json` mutually exclude (Phase 2.1 FIX 2).
    Every load-modify-save below holds it across the whole envelope."""
    return file_lock.exclusive_lock(file_lock.sidecar_lock_path(feed_path()))


def _save(rows: list[dict[str, Any]], now: datetime) -> None:
    """Write the `escalations` list back WITHOUT dropping any sibling top-level
    key (Phase 2.1 FIX 2). The Phase 2 version wrote `{'escalations': ...}` from
    scratch, which clobbered `for_larry_signal`'s `records` key on the shared
    file. Read the current full doc under the caller's lock, replace only our
    key, and rewrite — so the two schemas coexist on one file."""
    path = feed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text())
        doc = existing if isinstance(existing, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        doc = {}
    doc['escalations'] = _prune(rows, now)
    prod_write_guard.guard_no_prod_write_under_test(path)
    atomic_io.atomic_write_json(path, doc, sort_keys=True)


# -------------------- lifecycle --------------------

def upsert(
    record_id: str,
    *,
    headline: str,
    context: str,
    severity: str = 'warning',
    source: str = 'mirror-review',
    pr_url: Optional[str] = None,
    head_sha: Optional[str] = None,
    dedup_identity: Optional[str] = None,
    decision_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Create or refresh an OPEN for-Larry record keyed by ``record_id``.

    ``decision_key`` (Phase 2 Change A) is the canonical cross-store join key
    stamped at rest so the resolve fan-out (decision_resolve.resolve_decision)
    can match this record against the P/C/A stores without recomputing on every
    read. When the caller omits it, it is derived from ``record_id``/``pr_url``
    via the same pure helper readers fall back to — so the field is always
    present and consistent whether stamped by the producer or defaulted here.

    Contract D idempotency: if an OPEN row already exists for this id with the
    SAME ``dedup_identity`` (PR + head SHA), this is a no-op and returns None —
    a re-review of the same head emits no new artifact. A different
    ``dedup_identity`` (a fresh push) refreshes the row in place. Re-opening a
    previously resolved row flips it back to OPEN (a new escalation after a
    clear). Never raises — a write failure must not block the routing pass.
    """
    if not record_id:
        return None
    n = _now(now)
    try:
        with _feed_lock():  # FIX 2: serialize the load→save envelope
            rows = _load()
            existing = next((r for r in rows if r.get('id') == record_id), None)
            if (
                isinstance(existing, dict)
                and existing.get('resolved') is not True
                and existing.get('dedup_identity') == dedup_identity
            ):
                return None  # already surfaced for this exact PR+head SHA
            created = (existing or {}).get('ts') if isinstance(existing, dict) else None
            if not isinstance(created, str) or not created:
                created = _iso(n)
            row = {
                'id': record_id,
                'source': source,
                'headline': headline,
                'context': context,
                'severity': severity,
                'pr_url': pr_url,
                'head_sha': head_sha,
                'dedup_identity': dedup_identity,
                'decision_key': decision_key or _decision_key_for(record_id, pr_url),
                'for_larry': True,
                'resolved': False,
                'resolved_at': None,
                'ts': created,
                'updated_at': _iso(n),
            }
            rows = [r for r in rows if r.get('id') != record_id]
            rows.append(row)
            _save(rows, n)
            return row
    except OSError:
        return None


def clear(record_id: str, *, now: Optional[datetime] = None) -> bool:
    """Mark ``record_id`` RESOLVED (self-clearing per decision d). Returns True
    if an OPEN row was cleared, False if there was nothing open to clear.
    Never raises."""
    if not record_id:
        return False
    return clear_many([record_id], now=now) > 0


def clear_many(record_ids, *, now: Optional[datetime] = None) -> int:
    """Mark several ids RESOLVED in ONE locked load-modify-save (Phase 2.1 FIX 2).
    The resolve fan-out's E-leg used to call `clear` per matching row — N unlocked
    read-modify-writes of the same file, each racing a concurrent producer upsert.
    This does one locked pass. Returns the count newly cleared. Never raises."""
    ids = {r for r in record_ids if r}
    if not ids:
        return 0
    n = _now(now)
    try:
        with _feed_lock():
            rows = _load()
            cleared = 0
            for row in rows:
                if row.get('id') in ids and row.get('resolved') is not True:
                    row['resolved'] = True
                    row['resolved_at'] = _iso(n)
                    row['updated_at'] = _iso(n)
                    cleared += 1
            if cleared:
                _save(rows, n)
            return cleared
    except OSError:
        return 0


def get(record_id: str) -> Optional[dict[str, Any]]:
    """Return the row for ``record_id`` (open or resolved), or None."""
    if not record_id:
        return None
    return next((r for r in _load() if r.get('id') == record_id), None)


def list_open() -> list[dict[str, Any]]:
    """Return OPEN (unresolved) for-Larry rows."""
    return [r for r in _load() if r.get('resolved') is not True]


# -------------------- CLI (inspection / manual ops) --------------------

def _main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description='for-Larry needs-you record feed')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='list OPEN records')
    sub.add_parser('all', help='dump the whole feed')
    g = sub.add_parser('get', help='show one record'); g.add_argument('record_id')
    c = sub.add_parser('clear', help='resolve one record'); c.add_argument('record_id')
    args = p.parse_args(argv)

    if args.cmd == 'list':
        print(json.dumps(list_open(), indent=2, sort_keys=True))
    elif args.cmd == 'all':
        print(json.dumps(_load(), indent=2, sort_keys=True))
    elif args.cmd == 'get':
        print(json.dumps(get(args.record_id), indent=2, sort_keys=True))
    elif args.cmd == 'clear':
        print(f'cleared={clear(args.record_id)}')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
