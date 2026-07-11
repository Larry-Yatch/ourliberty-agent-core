#!/usr/bin/env python3
"""spec_review_conclusion.py — the single "did this gauntlet conclude?" predicate.

Runner-engine slice of agents/beacon/specs/spec-gauntlet-gate.md (§3.4). The
gauntlet runner writes exactly one conclusion file per gauntlet at
``~/agents/state/spec-review/concluded/<task_id>.json`` carrying a terminal
state (``passed | contested | incomplete | errored``) and the ``payload_hash``
of the payload it reviewed. This module is the ONE predicate that answers
"has (task_id, payload_hash) concluded?" — with two named consumers (§3.4, the
v1 "no named consumer" cargo-cult is closed):

  1. **The runner's resume-on-start scan** — before re-running a pending
     gauntlet after a restart, ask whether it already concluded so a completed
     gauntlet is never re-run (the notifier-restart dup class).
  2. **The stale-spool sweep** (folded into the runner) — a pending entry past
     the wall-clock ceiling with a dead runner is concluded ``incomplete`` and
     handed to the host for stamping; this predicate tells the sweep which
     pending entries already have a conclusion and must be left alone.

The predicate is deliberately payload-hash-aware: a conclusion only counts for
the exact payload it reviewed. If the same task_id is re-spooled with a
*different* body (a genuinely new spec), a stale conclusion for the old body
does not mask it.

Fail-safe reads: a missing/unreadable/malformed conclusion file reads as
"not concluded" so a corrupt artifact never wedges the pending entry — the
runner simply (re-)runs the gauntlet rather than silently treating a broken
file as a terminal state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or HOME / 'agents')

STATE_DIR = AGENTS_ROOT / 'state' / 'spec-review'
PENDING_DIR = STATE_DIR / 'pending'
CONCLUDED_DIR = STATE_DIR / 'concluded'
ARCHIVE_DIR = STATE_DIR / 'archive'

# The four terminal states, one per card while the gate is enabled (§3.5). A
# conclusion file whose ``terminal_state`` is not one of these is treated as
# not-concluded (fail-safe) — an unrecognized state is a bug, not a stamp gate.
TERMINAL_STATES: frozenset[str] = frozenset(
    {'passed', 'contested', 'incomplete', 'errored'})


def conclusion_path(task_id: str) -> Path:
    """Path of the conclusion artifact for ``task_id`` (may not exist)."""
    return CONCLUDED_DIR / f'{task_id}.json'


def read_conclusion(task_id: str) -> Optional[dict[str, Any]]:
    """Return the parsed conclusion dict for ``task_id``, or None if absent /
    unreadable / malformed. Never raises."""
    path = conclusion_path(task_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def is_concluded(task_id: str, payload_hash: Optional[str] = None) -> bool:
    """True iff a valid conclusion exists for ``task_id``.

    When ``payload_hash`` is given, the conclusion must match it — a conclusion
    for a *different* body does not count (a re-spooled, genuinely-changed spec
    is not masked by a stale terminal state). A conclusion whose
    ``terminal_state`` is not in ``TERMINAL_STATES`` reads as not-concluded.
    """
    data = read_conclusion(task_id)
    if data is None:
        return False
    if data.get('terminal_state') not in TERMINAL_STATES:
        return False
    if payload_hash is not None and data.get('payload_hash') != payload_hash:
        return False
    return True
