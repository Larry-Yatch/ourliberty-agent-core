#!/usr/bin/env python3
"""scripts/log_ts.py — shared log-timestamp parser (stdlib only).

Single source of truth for one recurring bug class (5+ independently-fixed
instances). Several parsers read ``~/agents/logs/outbox-notifier.log``, whose
lines begin with a NAIVE ``[YYYY-MM-DD HH:MM:SS]`` stamp written by
``outbox_notifier.log()`` via ``datetime.now()`` — no timezone, and the
production droplet runs America/Denver. Each parser independently extracted
that stamp and then incorrectly pinned it as UTC with
``dt.replace(tzinfo=timezone.utc)``, shifting every event ~6h into the past
(far enough that ``build_sequence_advancer.chain_event_says_merged`` would
discard a freshly-merged step, ``heal_pr_auto_merge`` refused every merge, and
``heal_pipeline_stall`` mis-bounded its scan window).

The correct read is to interpret a naive value in the SYSTEM-LOCAL zone — the
same host that wrote the log — which ``datetime.astimezone()`` does. Aware
values (beacon-bot ``-0600`` offsets, inbox-watcher / alert / chain_event
``+00:00`` isoformat, gh ``...Z``) pass through to the same instant in UTC.

``parse_log_ts`` owns the format tolerance so callers keep only their own
line-extraction regex: space-or-``T`` separator, optional microseconds,
trailing ``Z``, and ``+HHMM`` (no-colon) offsets — the last two unsupported by
py3.9's ``datetime.fromisoformat``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

# A ``+HHMM`` / ``-HHMM`` offset (no colon) at end-of-string. py3.9's
# fromisoformat only accepts ``+HH:MM``, so we splice the colon in.
_NUMERIC_OFFSET_RE = re.compile(r'[+-]\d{4}$')


def parse_log_ts(s: str) -> Optional[datetime]:
    """Parse a log/ISO timestamp token to an aware UTC ``datetime``.

    Returns ``None`` for non-strings, blanks, and unparseable input. A naive
    value is interpreted as system-local (the log writer's zone) and converted
    to UTC; an aware value is normalized to the same instant in UTC. Pass only
    the timestamp token (callers do their own line/bracket extraction first).
    """
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    s = s.replace(' ', 'T')
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    if _NUMERIC_OFFSET_RE.search(s):
        s = s[:-2] + ':' + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # astimezone() reads a naive dt in the system-local zone (= the writer's,
    # since reader and notifier run on the same host) before converting; on an
    # aware dt it just re-expresses the same instant in UTC.
    return dt.astimezone(timezone.utc)
