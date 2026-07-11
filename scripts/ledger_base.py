#!/usr/bin/env python3
"""ledger_base.py — shared scaffolding for the hand-rolled JSON ledgers.

Three sibling ledgers — ``no_session_ledger``, ``rebase_obligation_ledger``,
and ``revision_in_flight_ledger`` — independently grew a byte-identical block
of time helpers plus an identical fail-safe load and the same
``prune → mkdir → atomic_write_json`` save sequence. A single fix to that
skeleton (broadening ``parse_iso`` beyond its ``Z``-suffix handling, changing
the row-cap policy shape, hardening the corrupt-file degrade) previously had
to be applied and tested in three files, and missing one silently diverged the
ledgers. This module owns the shared mechanics ONCE:

  - UTC-normalized :func:`now` / :func:`iso` / :func:`parse_iso`
  - fail-safe :func:`load` (missing / corrupt / non-dict → ``{}``)
  - atomic :func:`save` that runs a caller-supplied prune before writing

Each ledger keeps its own ``LEDGER_FILE``, per-row schema, public API, and —
crucially — its own prune POLICY (TTL-seconds expiry vs resolved-retention-days
with open-row preservation). Those policies are genuinely different and stay in
their modules; :func:`save` takes the prune as a callable so the write
skeleton is shared without collapsing the policies together.

Stdlib only (``atomic_io`` is itself stdlib-only). Nothing here raises into a
daemon/timer: :func:`load` degrades to ``{}`` on any read/parse error; callers
wrap :func:`save` in their own best-effort ``try/except OSError``.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import atomic_io  # noqa: E402


# -------------------- time --------------------

def now(value: datetime | None = None) -> datetime:
    """Return ``value`` (or the current time) normalized to UTC."""
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """Serialize ``dt`` to an ISO-8601 string in UTC."""
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(ts: Any) -> datetime | None:
    """Parse an ISO-8601 string to a UTC datetime, or None if unparseable.

    Accepts a trailing ``Z`` and naive timestamps (assumed UTC)."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# -------------------- load / save --------------------

def load(ledger_file: Path) -> dict[str, dict[str, Any]]:
    """Return the ledger object keyed by id. Fail-safe: any read/parse error →
    empty dict, and non-dict rows are dropped so callers can assume the shape.
    Crashing here would take down whatever daemon/timer called us."""
    try:
        data = json.loads(Path(ledger_file).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save(
    ledger_file: Path,
    state: dict[str, dict[str, Any]],
    prune: Callable[[dict[str, dict[str, Any]]], dict[str, dict[str, Any]]],
) -> None:
    """Prune ``state`` with the caller's policy, then atomically persist it.

    ``prune`` is a ``state -> pruned_state`` callable so each ledger keeps its
    own expiry/cap policy while sharing this write skeleton. Not wrapped in a
    try/except: callers hold the best-effort ``OSError`` guard so a write
    failure never blocks the dispatch it accompanies."""
    pruned = prune(state)
    path = Path(ledger_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.atomic_write_json(path, pruned, sort_keys=True)
