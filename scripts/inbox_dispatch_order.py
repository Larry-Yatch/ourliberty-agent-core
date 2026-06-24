#!/usr/bin/env python3
"""Shared dispatch-ordering for agent inboxes (forge-queue-fast-track).

Two places must order an agent's pending inbox tasks IDENTICALLY, or the
Forge Queue panel would show a different order than the one the forge will
actually build:

  - the dispatcher: ``inbox_watcher.scan_inbox`` (decides what runs next)
  - the dashboard reader: ``dashboard_api._reader_agent_queue_queued``
    (the queued lane the operator sees)

The rule (ascending dispatch order, head = next to build):

  1. FAST-TRACKED tasks first — a task whose JSON carries a non-empty
     ``fast_tracked_at`` ISO-8601 timestamp (set by the dashboard's
     "Build next" action). Among fast-tracked tasks, NEWEST timestamp wins
     (LIFO): the most recent "Build next" click jumps to the head, the
     previous one drops to second — they stack, none is un-fast-tracked.
  2. Everything else after, OLDEST file mtime first (FIFO) — unchanged from
     the original behavior.

ISO-8601 UTC strings sort lexicographically in chronological order, so the
fast-tracked group sorts by the raw string descending; no datetime parsing
(and no parse-failure surface) on the hot dispatch path.

A task file that can't be read or parsed (mid-write, corrupt, not a JSON
object) is treated as NOT fast-tracked and falls back to its mtime — a bad
file can never wedge or reorder the dispatch loop.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, TypeVar

T = TypeVar('T')


def read_fast_tracked_at(path: Path) -> Optional[str]:
    """Return the task's ``fast_tracked_at`` string, or None.

    Best-effort: any OS/JSON error, a non-object payload, or a missing/blank
    field all yield None (treat as not fast-tracked).
    """
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get('fast_tracked_at')
    return value if isinstance(value, str) and value else None


def order_pending(
    entries: list[tuple[float, Optional[str], T]],
) -> list[tuple[float, Optional[str], T]]:
    """Order ``(mtime, fast_tracked_at, payload)`` triples for dispatch.

    Returns the same triples, fast-tracked-first (newest fast_tracked_at
    first / LIFO), then the rest oldest-mtime-first (FIFO). Python's sort is
    stable, so ties (identical timestamps or mtimes) keep their input order.
    The payload is opaque — callers pass whatever they need to emit (a Path
    for the dispatcher, a filename for the reader).
    """
    fast = [e for e in entries if e[1]]
    normal = [e for e in entries if not e[1]]
    fast.sort(key=lambda e: e[1], reverse=True)  # newest fast_tracked_at first
    normal.sort(key=lambda e: e[0])              # oldest mtime first
    return fast + normal
