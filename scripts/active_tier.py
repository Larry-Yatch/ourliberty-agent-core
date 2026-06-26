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

# Long-lived setup-token env-var mapping (single source of truth shared with
# agent_runner._apply_tier_auth). Each tier has a NON-refreshing
# `claude setup-token` (valid ~1 yr) stored in the process env; presence of a
# non-empty value means dispatches will authenticate via the token and bypass
# the credentials.json refresh path. tier_auth_ok mirrors that precedence so
# the rotation pre-engage gate verifies the SAME auth source a dispatch would
# actually use.
_SETUP_TOKEN_ENV_BY_TIER = {
    'tier1': 'CLAUDE_CODE_OAUTH_TOKEN_TIER1',
    'tier2': 'CLAUDE_CODE_OAUTH_TOKEN_TIER2',
}

# Canonical on-disk home of the durable setup-tokens: the systemd
# ``EnvironmentFile`` that exports CLAUDE_CODE_OAUTH_TOKEN_TIER{1,2}.
# ``_setup_token_for_tier`` falls back to parsing this file when the token is
# absent from ``os.environ`` — so an auth check is correct even from a process
# that did NOT inherit the EnvironmentFile (manual SSH probes, ``run_*.sh``
# debug runs, ad-hoc healer invocations, a Claude session on the droplet).
#
# Without this fallback, such a process found no token, fell through to the
# intentionally-frozen ``.credentials.json`` under TIER2_HOME, and emitted a
# FALSE "Tier 2 down" — the recurring false-negative where the system reports
# Tier 2 unavailable while it is actually up on the durable token. The env var
# remains the primary (hot-path) source; the file is read only on a miss, so
# systemd-launched daemons never touch disk here. Override via
# ``OURLIBERTY_CREDENTIALS_ENV_FILE`` (tests point it at a nonexistent path to
# isolate the legacy credentials.json cases). Spec: account-rotation.md § 6.1.
_CREDENTIALS_ENV_FILE = '/home/larry/credentials/.env.larry'


def _credentials_env_file():
    """Path to the credentials EnvironmentFile. Honors
    ``OURLIBERTY_CREDENTIALS_ENV_FILE`` (tests / non-standard hosts); resolved
    at call time so an override set after import takes effect."""
    return Path(os.environ.get('OURLIBERTY_CREDENTIALS_ENV_FILE',
                               _CREDENTIALS_ENV_FILE))


def _setup_token_from_env_file(env_name):
    """Best-effort read of ``env_name``'s value from the credentials
    EnvironmentFile. Returns the token, or None on any miss / read error.

    Parses systemd ``EnvironmentFile`` / shell ``KEY=value`` lines, tolerating
    a leading ``export`` and single/double-quoted values. The token value is
    NEVER logged."""
    path = _credentials_env_file()
    try:
        # Pin UTF-8 so parsing is deterministic across launch contexts (a
        # C/POSIX-locale systemd process vs a UTF-8 interactive shell) and
        # catch UnicodeError (NOT an OSError) so a binary / partial-write /
        # non-UTF-8 file degrades to a clean miss instead of crashing the
        # rotation auth gate — honoring this function's "None on any read
        # error" contract.
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None
    prefix = env_name + '='
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        # Strip a single matching pair of surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        return value or None
    return None


def _setup_token_for_tier(tier):
    """Return the long-lived setup-token for ``tier`` or None if unconfigured.

    Source precedence:
      1. ``os.environ`` — the systemd ``EnvironmentFile`` path (hot path; no
         disk I/O for daemons that inherited the env).
      2. The credentials ``EnvironmentFile`` on disk (fallback) — so the token
         is readable from a process that did NOT inherit the env var (manual
         SSH checks, ``run_*.sh`` debug runs, ad-hoc healer runs). This removes
         the "token must be present in every process env" fragility behind the
         recurring false "Tier 2 down".

    Empty string counts as unset so a presence check equals a usability check.
    Token values must never be logged."""
    env_name = _SETUP_TOKEN_ENV_BY_TIER.get(tier)
    if not env_name:
        return None
    return (os.environ.get(env_name)
            or _setup_token_from_env_file(env_name)
            or None)


def active_setup_token():
    """Setup-token for the *currently active* tier, or None if unconfigured.

    Resolves ``read()['tier']`` (blackboard/active-tier.json) then the per-tier
    token, so a caller authenticates against the SAME account a dispatch would.
    This lets the /cycle heartbeat follow the team's tier switch / rotation
    instead of pinning to HOME's auto-refreshing ``~/.claude/.credentials.json``
    (which rots silently on an OAuth refresh failure and then 401s every
    iteration — the recurring false "Tier N down"). Never log the return value."""
    return _setup_token_for_tier(read()['tier'])

# Max backoff when the "resets <time>" message is unparseable. Spec § 6.3.
_COOLDOWN_BACKOFF_CAP = timedelta(minutes=30)
# Base unit for capped exponential backoff: 1 min, doubling each attempt.
_COOLDOWN_BACKOFF_BASE = timedelta(minutes=1)

# Fixed cooldown applied when a tier hits auth_401 (Step A rotation fix).
# Auth-401 messages don't carry a reset time, so there's nothing to parse;
# the cooldown just has to be long enough that a single bad token cannot
# storm the dispatcher. 30 min matches the rate-limit backoff cap.
_AUTH_COOLDOWN = timedelta(minutes=30)

# Lead margin for tier_auth_ok: the target tier's OAuth credentials must
# remain valid at least this far past `now`. The Claude CLI's auto-refresh
# typically lands well before expiry, but a few minutes of headroom keeps a
# tier flip from racing the refresh path. Tunable; not in config because
# this is a structural invariant of the gate, not an operator dial.
_AUTH_EXPIRY_MIN_LEAD = timedelta(minutes=10)

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


def set_cooldown(tier, raw_excerpt='', now=None, kind='rate_limit'):
    """Set a per-tier cooldown. Returns the persisted ``until`` ISO string.

    ``kind='rate_limit'`` (default): tries to parse ``"resets <time>"`` from
    ``raw_excerpt``; falls back to capped exponential backoff when the
    message is unparseable (spec § 6.3). The exponential backoff grows
    1m → 2m → 4m → 8m → 16m → 30m (capped) for each consecutive unparseable
    rate_limit on the same tier. A successful parse resets the backoff
    counter for that tier — fresh parseable events are not penalized by an
    earlier unparseable streak.

    ``kind='auth_401'``: fixed ``_AUTH_COOLDOWN`` window (Step A rotation
    fix). Auth-401 messages don't carry a parseable reset time and have a
    different recovery model (operator re-auths the bad tier), so a single
    fixed window is the right shape. The rate_limit backoff counter is left
    untouched — auth and rate-limit streaks are independent signals.
    """
    if tier not in _VALID_TIERS:
        raise ValueError('invalid tier: ' + repr(tier))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    state = read()
    cooldowns = dict(state.get('cooldowns') or {})
    backoff = dict(state.get('cooldown_backoff') or {})

    if kind == 'auth_401':
        until = now + _AUTH_COOLDOWN
        cooldowns[tier] = until.astimezone(timezone.utc).isoformat()
        state['cooldowns'] = cooldowns
        _write(state)
        return cooldowns[tier]

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


def _credentials_path(tier):
    """Path to the OAuth credentials file for a tier. Reads the home dirs
    at call time so tests can monkey-patch ``TIER1_HOME`` / ``TIER2_HOME``."""
    home = TIER1_HOME if tier == 'tier1' else TIER2_HOME
    return Path(home, '.claude', '.credentials.json')


def tier_auth_ok(tier, now=None):
    """Return True iff the target tier has usable dispatch auth.

    Precedence mirrors ``agent_runner._apply_tier_auth`` (the dispatch
    path): if the tier's long-lived setup-token env var
    (``CLAUDE_CODE_OAUTH_TOKEN_TIER1`` / ``..._TIER2``) is set and
    non-empty, the gate returns True — that token is the live auth source
    a dispatch would actually use, so presence alone is the signal.
    (A per-tick live ``claude -p`` probe would be too expensive; the
    auth_401 circuit-breaker in scripts/rotate_active_tier.py backstops
    a bad token after a single failed flip.)

    Only when no setup-token is configured for the tier does the gate
    fall back to the legacy credentials.json ``claudeAiOauth.expiresAt``
    check (>= ``_AUTH_EXPIRY_MIN_LEAD`` in the future), preserving the
    historical Step-A semantics byte-for-byte for credentials.json-only
    deployments.

    Used by the rotation scheduler's pre-engage gate (Step A rotation
    fix) to refuse switching into a tier with a stale token. The
    2026-05-29 storm root cause was Tier 2 going silently expired and
    the scheduler flipping into it anyway; this gate is the structural
    fix. The setup-token short-circuit added 2026-05-30 prevents a
    follow-on false-block where credentials.json lapses (because the
    setup-token path no longer exercises/refreshes it) while the
    setup-token itself remains valid for dispatches.

    For the credentials.json fallback: any I/O error, parse error,
    missing field, or wrong-shape payload returns False. The defensive
    posture is intentional: a tier we can't verify is treated as not
    auth-ok, so the scheduler holds the current tier rather than
    committing to an unverifiable target.
    """
    if tier not in _VALID_TIERS:
        return False
    if _setup_token_for_tier(tier):
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = _credentials_path(tier)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    oauth = data.get('claudeAiOauth')
    if not isinstance(oauth, dict):
        return False
    expires_at_ms = oauth.get('expiresAt')
    if not isinstance(expires_at_ms, (int, float)):
        return False
    try:
        expires = datetime.fromtimestamp(
            float(expires_at_ms) / 1000.0, tz=timezone.utc,
        )
    except (OverflowError, OSError, ValueError):
        return False
    return expires > now + _AUTH_EXPIRY_MIN_LEAD


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


if __name__ == '__main__':  # pragma: no cover
    # Thin single-purpose CLI so run_cycle.sh can resolve the active tier's
    # setup-token without re-implementing the precedence rules. The token goes
    # to stdout for capture only — empty when unconfigured (caller falls back to
    # the credentials.json path). NEVER log the printed value.
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == 'active-setup-token':
        sys.stdout.write(active_setup_token() or '')
    else:
        sys.stderr.write('usage: active_tier.py active-setup-token\n')
        sys.exit(2)
