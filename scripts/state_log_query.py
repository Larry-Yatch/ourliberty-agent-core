#!/usr/bin/env python3
"""state_log_query.py — Beacon's read side of the work-in-flight State Log.

System self-awareness Slice 1 § D4. The narrator (`system_state_log.py`) is the
SOLE writer of `~/agents/blackboard/system-state-log.json`; this module is a
pure READER + recognizer + formatter that the Telegram bot consults so Larry can
ask "where are we on work in flight?" and get the standing picture WITHOUT a
Beacon/Claude round-trip.

Three pieces, all stdlib-only (the bot is stdlib-only):

  1. `is_state_log_query(text)` — recognizes the work-in-flight ask. Phrases are
     chosen NOT to collide with `catch_me_up`'s shortcut set ("status",
     "where are we", "summary", …): catch_me_up is a delta-since-last-checkpoint
     synthesizer (live gh queries), this is the always-fresh standing snapshot.
     We match on substrings like "work in flight" so "where are we on work in
     flight" lands here while a bare "where are we" still flows to catch_me_up.

  2. `read_state_log()` — mirrors the dashboard reader (`_reader_system_state_log`)
     but stdlib-only: returns the doc plus `present`/`stale` freshness signals.
     Missing OR malformed log degrades to present=False (never raises) — a broken
     log must never break the bot's message loop.

  3. `format_reply(doc)` — renders the operator-facing answer: the narrator's
     prose, a freshness line, and an honest degrade when the log is stale or
     absent (so Larry is never shown a confidently-wrong stale picture).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

# A State Log older than this is reported stale (matches the dashboard reader's
# STATE_LOG_STALE_AFTER_SEC). Sized to the narrator's GC-tick cadence (~15 min)
# plus headroom for one skipped/slow tick.
STALE_AFTER_SEC = 25 * 60

# Substring triggers (matched against normalized text). Deliberately distinct
# from catch_me_up.SHORTCUT_PHRASES so the two operator shortcuts don't fight.
_QUERY_SUBSTRINGS = (
    'work in flight',
    'in flight',
    'state of work',
    'state of the work',
    'standing brain',
)

# Exact normalized tokens (whole message equals one of these). `/state` is the
# slash-command form; the leading slash is stripped by `_normalize`.
_QUERY_EXACT = frozenset({'state', 'statelog', 'state log'})


def _normalize(text: str) -> str:
    """Lowercase; strip a leading slash, surrounding whitespace, and trailing
    sentence punctuation. Mirrors `catch_me_up._normalize` so recognition is
    consistent across the two shortcuts."""
    text = (text or '').strip()
    if text.startswith('/'):
        text = text[1:]
    text = text.lower().strip()
    text = re.sub(r'[.?!,;:]+$', '', text).strip()
    return text


def is_state_log_query(text: str) -> bool:
    """True if `text` asks for the work-in-flight standing picture."""
    norm = _normalize(text)
    if not norm:
        return False
    if norm in _QUERY_EXACT:
        return True
    return any(phrase in norm for phrase in _QUERY_SUBSTRINGS)


def _state_log_path() -> Path:
    """Resolve the State Log path. Reuses the narrator's own resolver so the
    read path can never drift from the write path; falls back to the documented
    default if that import is somehow unavailable (keeps the bot resilient)."""
    try:
        from system_state_log import state_log_path  # noqa: PLC0415
        return state_log_path()
    except Exception:  # noqa: BLE001 — never let a reader import break the bot
        import os  # noqa: PLC0415
        override = os.environ.get('OURLIBERTY_SYSTEM_STATE_LOG')
        if override:
            return Path(override)
        root = os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents')
        return Path(root) / 'blackboard' / 'system-state-log.json'


def read_state_log(path: Optional[Path] = None) -> dict[str, Any]:
    """Return the State Log doc + freshness signals, fail-safe. Missing OR
    malformed → present=False (never raises). Mirrors the dashboard reader."""
    p = path if path is not None else _state_log_path()
    absent = {
        'present': False,
        'stale': True,
        'age_sec': None,
        'narrative_prose': None,
        'structured_snapshot': None,
        'provenance': None,
        'as_of': None,
    }
    try:
        if not p.exists():
            return absent
        raw = p.read_text()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return dict(absent)
    if not isinstance(data, dict):
        return dict(absent)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = None
    age = (time.time() - mtime) if mtime is not None else None
    stale = age is None or age > STALE_AFTER_SEC
    narrative = data.get('narrative_prose')
    snapshot = data.get('structured_snapshot')
    provenance = data.get('provenance')
    as_of = data.get('as_of')
    return {
        'present': True,
        'stale': stale,
        'age_sec': age,
        'narrative_prose': narrative if isinstance(narrative, str) else None,
        'structured_snapshot': snapshot if isinstance(snapshot, dict) else None,
        'provenance': provenance if isinstance(provenance, dict) else None,
        'as_of': as_of if isinstance(as_of, str) else None,
    }


def _humanize_age(age_sec: Optional[float]) -> str:
    if age_sec is None:
        return 'unknown age'
    mins = int(age_sec // 60)
    if mins <= 0:
        return 'just now'
    if mins == 1:
        return '1 minute ago'
    if mins < 60:
        return f'{mins} minutes ago'
    hours = mins // 60
    rem = mins % 60
    if hours == 1 and rem == 0:
        return '1 hour ago'
    if rem == 0:
        return f'{hours} hours ago'
    return f'{hours}h {rem}m ago'


def format_reply(doc: dict[str, Any]) -> str:
    """Render the operator-facing answer from a `read_state_log` result.

    Honest degrade is the whole point: when the log is absent or stale we SAY so
    rather than present a stale snapshot as the current truth. A present, fresh
    log leads with the narrator's prose and ends with a freshness line."""
    if not doc.get('present'):
        return (
            "I don't have a work-in-flight picture yet — the standing State Log "
            "hasn't been written (the narrator may not have run a cycle yet). "
            "Ask me again shortly, or say \"catch me up\" for a live synthesis."
        )

    narrative = (doc.get('narrative_prose') or '').strip()
    freshness = _humanize_age(doc.get('age_sec'))

    if not narrative:
        narrative = (
            "The State Log is present but carries no narrative this cycle."
        )

    lines = [narrative, ""]
    if doc.get('stale'):
        lines.append(
            f"⚠ Heads up: this picture is {freshness} and may be out of date "
            f"(the narrator hasn't refreshed it recently). For the live state, "
            f"say \"catch me up\"."
        )
    else:
        lines.append(f"_State of work in flight as of {freshness}._")
    return "\n".join(lines).strip()
