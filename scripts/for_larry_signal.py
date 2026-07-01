"""scripts/for_larry_signal.py — the canonical durable for-Larry escalation signal.

operator-needs-you-feed spec §5.1 + §5.2 (Part A). ONE local file under
``~/agents/blackboard/`` that two producers feed and ``system_state_log`` reads,
so the `/where-we-are` "Waiting on You" panel + the doorbell surface the two
"needs-you" classes that previously only scrolled past in Telegram:

  * promote_alerts.run_cycle — a critical alert that crossed the existing
    promotion bar and did NOT self-resolve within a cycle (§5.1).
  * outbox_notifier — a Forge build that exhausted its clarification budget
    (§5.2).

Decisions this module encodes (spec § 2):

  * (c) ONE canonical local file keeps the board + doorbell on a single
    substrate (the doorbell counts off the local system-state-log snapshot,
    never Supabase).
  * (d) SELF-CLEARING, not append-only: each record retracts (``resolved:
    true``) when its trigger clears (alert leaves the snapshot; build
    re-dispatched), so a stale "needs you" row can never linger.

Two producers write the one file — a Pulse-cycle timer (promote_alerts) and the
notifier loop (outbox_notifier) — so every read-modify-write runs under the
shared ``file_lock`` and publishes via ``atomic_io``. That is what makes "one
canonical file, two producers" safe against a lost update; there is no second
classifier and no double-surfacing of ``larry_alerts`` (spec § 6).

stdlib + the two shared write helpers only (no HTTP, no Supabase) so it imports
clean under the test jail and inside system_state_log's no-Supabase contract.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Sibling scripts/ on path so imports resolve under systemd, the notifier, or tests.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import file_lock  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402

__all__ = [
    'signal_path',
    'load_records',
    'active_entries',
    'upsert_record',
    'resolve_record',
    'sync_prefix',
    'ESCALATION_KEY_PREFIX',
    'CLARIFY_EXHAUSTED_KEY_PREFIX',
]

# Per-producer key namespaces so the two writers never collide on a key and so a
# producer can reconcile *only* its own records (sync_prefix) without touching
# the other's.
ESCALATION_KEY_PREFIX = 'escalation:'
CLARIFY_EXHAUSTED_KEY_PREFIX = 'clarify-exhausted:'

_SCHEMA_VERSION = 1


def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def signal_path() -> Path:
    """The canonical for-Larry signal file. ``OURLIBERTY_FOR_LARRY_SIGNAL_FILE``
    overrides for tests; otherwise the blackboard file (decision c)."""
    override = os.environ.get('OURLIBERTY_FOR_LARRY_SIGNAL_FILE')
    if override:
        return Path(override)
    return _agents_root() / 'blackboard' / 'for-larry-escalations.json'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_doc() -> dict[str, Any]:
    return {'version': _SCHEMA_VERSION, 'updated_at': _now_iso(), 'records': {}}


def _read_doc(path: Path) -> dict[str, Any]:
    """Read the signal doc, fail-safe to an empty doc (never raises)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return _empty_doc()
    if not isinstance(data, dict):
        return _empty_doc()
    records = data.get('records')
    if not isinstance(records, dict):
        data['records'] = {}
    data.setdefault('version', _SCHEMA_VERSION)
    return data


def _normalize_record(key: str, record: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Stamp the invariants every for-Larry record carries.

    A producer writes an ACTIVE record, so ``for_larry`` is always True and
    ``resolved`` defaults False — re-upserting a previously-resolved key (its
    trigger re-appeared) flips it back to active.

    ``needs_larry`` (approval-sync Phase 3a §3a.2) is the producer-side
    "only Larry can act on this" classification, normalized to an explicit bool
    (default False) so every record carries it uniformly and the "Needs You"
    read model can gate the DECIDE lane on it without re-deriving from severity.
    """
    rec = dict(record) if isinstance(record, dict) else {}
    rec['for_larry'] = True
    rec['resolved'] = bool(rec.get('resolved', False))
    rec['needs_larry'] = bool(rec.get('needs_larry', False))
    rec.setdefault('key', key)
    rec.setdefault('ts', _now_iso())
    return rec


# -------------------- reads (fail-safe to empty) --------------------

def load_records(path: Optional[Path] = None) -> dict[str, Any]:
    """All records (resolved + unresolved) keyed by record key, fail-safe {}."""
    path = path or signal_path()
    records = _read_doc(path).get('records')
    return records if isinstance(records, dict) else {}


def active_entries(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Unresolved, explicitly-for-Larry records as a flat list — the read
    surface ``system_state_log.load_for_larry_escalations`` folds in (§5.1)."""
    out: list[dict[str, Any]] = []
    for rec in load_records(path).values():
        if not isinstance(rec, dict):
            continue
        if rec.get('for_larry') is not True:
            continue
        if rec.get('resolved') is True:
            continue
        out.append(rec)
    return out


# -------------------- writes (locked read-merge-write) --------------------

def upsert_record(
    key: str, record: dict[str, Any], *, path: Optional[Path] = None,
) -> None:
    """Write (or overwrite) one active for-Larry record under ``key``.

    Locked + atomic so a concurrent producer's write is never lost.
    """
    path = path or signal_path()
    rec = _normalize_record(key, record)
    with file_lock.exclusive_lock(file_lock.sidecar_lock_path(path)):
        doc = _read_doc(path)
        doc['records'][key] = rec
        doc['updated_at'] = _now_iso()
        atomic_write_json(path, doc)


def resolve_record(key: str, *, path: Optional[Path] = None) -> bool:
    """Mark the record under ``key`` resolved (self-clear, decision d).

    Returns True iff it existed and was newly resolved; a missing or
    already-resolved key is a no-op (no write, no churn).
    """
    path = path or signal_path()
    with file_lock.exclusive_lock(file_lock.sidecar_lock_path(path)):
        doc = _read_doc(path)
        rec = doc['records'].get(key)
        if not isinstance(rec, dict) or rec.get('resolved') is True:
            return False
        rec['resolved'] = True
        rec['resolved_at'] = _now_iso()
        doc['updated_at'] = _now_iso()
        atomic_write_json(path, doc)
        return True


def sync_prefix(
    active: dict[str, dict[str, Any]], prefix: str, *,
    path: Optional[Path] = None,
) -> dict[str, list[str]]:
    """Reconcile one producer's whole namespace in a single locked transaction.

    Upserts every record in ``active`` (keyed) and marks resolved any existing
    *unresolved* record whose key starts with ``prefix`` but is absent from
    ``active`` — i.e. its trigger left the snapshot (self-clear, decision d).
    Only touches keys under ``prefix``, so the other producer's records are
    untouched. Returns ``{'written': [...], 'resolved': [...]}``. Skips the
    write entirely when nothing changed and the file already exists (no churn).
    """
    path = path or signal_path()
    with file_lock.exclusive_lock(file_lock.sidecar_lock_path(path)):
        doc = _read_doc(path)
        records = doc['records']
        written: list[str] = []
        resolved: list[str] = []
        for key, rec in active.items():
            records[key] = _normalize_record(key, rec)
            written.append(key)
        for key, rec in list(records.items()):
            if not key.startswith(prefix) or key in active:
                continue
            if isinstance(rec, dict) and rec.get('resolved') is not True:
                rec['resolved'] = True
                rec['resolved_at'] = _now_iso()
                resolved.append(key)
        if not written and not resolved and path.exists():
            return {'written': written, 'resolved': resolved}
        doc['updated_at'] = _now_iso()
        atomic_write_json(path, doc)
        return {'written': written, 'resolved': resolved}
