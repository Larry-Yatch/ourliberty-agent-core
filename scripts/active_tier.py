"""Active-tier state for OAuth account rotation.

Reads/writes ``blackboard/active-tier.json`` to determine which HOME the
Claude CLI subprocess should authenticate from. Missing or corrupt state
defaults to ``tier1`` — today's behavior — so this module is safe to wire
up before the rotation scheduler (spec § 6.3) lands.

Spec anchor: ``agents/beacon/specs/account-rotation.md`` § 6.2.

Schema::

    {"tier": "tier1"|"tier2",
     "since": <iso8601 utc>|null,
     "next_switch_due": <iso8601 utc>|null,
     "draining": bool}

The helpers are intentionally synchronous and dependency-free; the
rotation scheduler that will drive ``set_tier()`` / ``set_draining()``
lands in a separate PR.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENTS_ROOT = Path.home() / 'agents'

# Tier homes — the two OAuth credential roots. Tier 1 is Larry's system
# account home (today's default); Tier 2 is the personal account's
# isolated home dir.
TIER1_HOME = '/home/larry'
TIER2_HOME = '/home/larry/.claude-larry-personal'

STATE_REL = 'blackboard/active-tier.json'

_DEFAULT_STATE = {
    'tier': 'tier1',
    'since': None,
    'next_switch_due': None,
    'draining': False,
    # Cooldown bookkeeping (spec § 6.3 rate-limit fold-in). Keyed by tier;
    # value is an ISO8601 UTC `until` timestamp. Missing tier or expired
    # `until` means no cooldown. `cooldown_backoff` counts consecutive
    # unparseable rate_limit events so capped exponential backoff can grow
    # the cooldown without unbounded thrash.
    'cooldowns': {},
    'cooldown_backoff': {},
}

_VALID_TIERS = ('tier1', 'tier2')

# Max backoff when the "resets <time>" message is unparseable. Spec § 6.3.
_COOLDOWN_BACKOFF_CAP = timedelta(minutes=30)
# Base unit for capped exponential backoff: 1 min, doubling each attempt.
_COOLDOWN_BACKOFF_BASE = timedelta(minutes=1)

# "resets <time>" parsers — the Claude CLI rate-limit message uses
# account-local time without a tz hint. The 2026-05-28 incident shows
# variants like "resets 3:30pm", "resets 14:00", "resets 3pm". The fallback
# tz for naive times is America/Denver (Larry's home tz, the OAuth account
# locale). Unparseable inputs return None and trigger the capped-backoff
# branch in set_cooldown().
_RESETS_HHMM_AMPM_RE = re.compile(
    r'resets\s+(\d{1,2}):(\d{2})\s*(am|pm)\b', re.IGNORECASE,
)
_RESETS_H_AMPM_RE = re.compile(
    r'resets\s+(\d{1,2})\s*(am|pm)\b', re.IGNORECASE,
)
_RESETS_HHMM_24_RE = re.compile(
    r'resets\s+(\d{1,2}):(\d{2})\b', re.IGNORECASE,
)

# Sanity bounds for a parsed "resets <time>" — Claude's CLI doesn't carry
# tz info, so the wall-clock we parse can land wildly wrong. Cap to [5 min,
# 5 hours] which brackets the legitimate quota window. Out-of-bounds parses
# fall through to the capped exponential backoff (treated as unparseable).
_COOLDOWN_PARSED_MIN = timedelta(minutes=5)
_COOLDOWN_PARSED_MAX = timedelta(hours=5)


def _state_path():
    """Resolve the state file path. Honors ``OURLIBERTY_AGENTS_ROOT`` so
    tests can redirect to a tmpdir; production resolves to
    ``~/agents/blackboard/active-tier.json``. Resolved at call time so env
    tweaks (e.g. by an autouse fixture) take effect."""
    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    base = Path(root) if root else AGENTS_ROOT
    return base / STATE_REL


def read():
    """Return the current state dict. Missing file, unreadable file,
    malformed JSON, non-dict payload, or unknown ``tier`` value all fall
    back to the tier1 default — the rotation plumbing must never wedge
    the runner on a parse error."""
    path = _state_path()
    if not path.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_STATE)
    if not isinstance(data, dict):
        return dict(_DEFAULT_STATE)
    if data.get('tier') not in _VALID_TIERS:
        return dict(_DEFAULT_STATE)
    merged = dict(_DEFAULT_STATE)
    merged.update(data)
    # Coerce structurally-broken cooldown sub-fields back to empty dicts so
    # downstream callers can `.get(tier)` without isinstance churn.
    if not isinstance(merged.get('cooldowns'), dict):
        merged['cooldowns'] = {}
    if not isinstance(merged.get('cooldown_backoff'), dict):
        merged['cooldown_backoff'] = {}
    return merged


def current_home():
    """Return the HOME directory for the currently active tier."""
    return TIER1_HOME if read()['tier'] == 'tier1' else TIER2_HOME


def other_home():
    """Return the HOME of the OPPOSITE tier — the failure-fallback target.

    With state=tier1 (today's default), this returns the Tier 2 personal
    home, preserving the existing Tier 1 → Tier 2 fallback behavior.
    """
    return TIER2_HOME if read()['tier'] == 'tier1' else TIER1_HOME


def set_tier(tier):
    """Persist a new active tier. Stamps ``since`` with the current UTC
    time; leaves ``next_switch_due`` and ``draining`` alone so the
    scheduler that owns those fields (PR 6.3) can write them
    independently."""
    if tier not in _VALID_TIERS:
        raise ValueError('invalid tier: ' + repr(tier))
    state = read()
    state['tier'] = tier
    state['since'] = datetime.now(timezone.utc).isoformat()
    _write(state)


def set_draining(draining):
    """Update the ``draining`` flag without touching tier/since."""
    state = read()
    state['draining'] = bool(draining)
    _write(state)


def set_next_switch_due(when):
    """Persist the next switch deadline. Accepts a datetime (will be
    serialized to ISO8601 UTC) or None (clears the deadline)."""
    state = read()
    if when is None:
        state['next_switch_due'] = None
    else:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        state['next_switch_due'] = when.astimezone(timezone.utc).isoformat()
    _write(state)


def parse_reset_time(raw, now=None):
    """Parse a ``"resets <time>"`` substring from a Claude CLI rate-limit
    message. Returns a timezone-aware UTC datetime for the next reset, or
    ``None`` if unparseable.

    No timezone is carried by the CLI message, so the parsed wall-clock is
    treated as UTC. Out-of-band parses (already past, or more than 5h out)
    return ``None`` so the caller falls back to capped backoff — better to
    cool down on a bounded interval than commit to a wildly-wrong deadline.
    """
    if not isinstance(raw, str) or not raw:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    hour = minute = ampm = None
    m = _RESETS_HHMM_AMPM_RE.search(raw)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    else:
        m = _RESETS_H_AMPM_RE.search(raw)
        if m:
            hour, minute, ampm = int(m.group(1)), 0, m.group(2).lower()
        else:
            m = _RESETS_HHMM_24_RE.search(raw)
            if m:
                hour, minute, ampm = int(m.group(1)), int(m.group(2)), None

    if hour is None:
        return None
    if ampm == 'pm' and hour < 12:
        hour += 12
    elif ampm == 'am' and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Roll forward to the next future occurrence.
    if candidate <= now:
        candidate = candidate + timedelta(days=1)

    delta = candidate - now
    if delta < _COOLDOWN_PARSED_MIN or delta > _COOLDOWN_PARSED_MAX:
        return None
    return candidate


def set_cooldown(tier, raw_excerpt='', now=None):
    """Set a per-tier rate-limit cooldown. Tries to parse ``"resets <time>"``
    from ``raw_excerpt``; falls back to capped exponential backoff when the
    message is unparseable (per spec § 6.3). Returns the persisted ``until``
    ISO string.

    The exponential backoff grows 1m → 2m → 4m → 8m → 16m → 30m (capped) for
    each consecutive unparseable rate_limit on the same tier. A successful
    parse resets the backoff counter for that tier — fresh parseable events
    are not penalized by an earlier unparseable streak.
    """
    if tier not in _VALID_TIERS:
        raise ValueError('invalid tier: ' + repr(tier))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    state = read()
    cooldowns = dict(state.get('cooldowns') or {})
    backoff = dict(state.get('cooldown_backoff') or {})

    parsed = parse_reset_time(raw_excerpt, now=now)
    if parsed is not None:
        until = parsed
        backoff.pop(tier, None)
    else:
        attempts = int(backoff.get(tier, 0)) + 1
        delay = min(
            _COOLDOWN_BACKOFF_BASE * (2 ** (attempts - 1)),
            _COOLDOWN_BACKOFF_CAP,
        )
        until = now + delay
        backoff[tier] = attempts

    cooldowns[tier] = until.astimezone(timezone.utc).isoformat()
    state['cooldowns'] = cooldowns
    state['cooldown_backoff'] = backoff
    _write(state)
    return cooldowns[tier]


def cooldown_until(tier, now=None):
    """Return the ISO ``until`` string for an active per-tier cooldown, or
    ``None`` if no cooldown is recorded OR the recorded one has already
    expired. The watcher uses this as the dispatch gate; ``rotate_active_tier``
    does not need to clear expired entries because expiry is filtered at read
    time (housekeeping happens lazily on the next ``set_cooldown`` for the
    tier)."""
    if tier not in _VALID_TIERS:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    state = read()
    until_iso = (state.get('cooldowns') or {}).get(tier)
    if not until_iso:
        return None
    try:
        until_dt = datetime.fromisoformat(until_iso)
    except (TypeError, ValueError):
        return None
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)
    if until_dt <= now:
        return None
    return until_iso


def clear_cooldown(tier):
    """Drop a tier's cooldown + backoff counter. Intended for tests and for
    operator recovery (e.g., after a manual quota top-up); the watcher does
    NOT call this — it relies on ``cooldown_until`` expiry filtering."""
    if tier not in _VALID_TIERS:
        raise ValueError('invalid tier: ' + repr(tier))
    state = read()
    cooldowns = dict(state.get('cooldowns') or {})
    backoff = dict(state.get('cooldown_backoff') or {})
    cooldowns.pop(tier, None)
    backoff.pop(tier, None)
    state['cooldowns'] = cooldowns
    state['cooldown_backoff'] = backoff
    _write(state)


def _write(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
