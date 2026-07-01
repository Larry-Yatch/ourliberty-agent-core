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
import random
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Shared advisory-lock helper (the repo's audit-blessed flock wrapper). Used to
# serialize the cooldown / round-robin-counter read-modify-write critical
# sections (spec § 7). active_tier.py lives in scripts/ alongside file_lock.py;
# add scripts/ to sys.path so a bare `import file_lock` resolves regardless of
# how active_tier was imported. Degrades to a no-op lock where fcntl is absent.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import file_lock  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or Path.home() / 'agents')

# Tier homes — the two OAuth credential roots. Tier 1 is Larry's system
# account home (today's default); Tier 2 is the personal account's
# isolated home dir.
TIER1_HOME = '/home/larry'
TIER2_HOME = '/home/larry/.claude-larry-personal'
# Tier 3: a dedicated Max account. Under the shared-config model it is a
# setup-token-only tier with NO own home — it runs with the real account
# home (TIER1_HOME), inheriting Tier 1's MCP/settings/projects.
TIER3_HOME = '/home/larry'

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

_VALID_TIERS = ('tier1', 'tier2', 'tier3')
# tier -> account home (credentials.json root). tier3 shares the real home.
# Resolved at CALL time so tests that monkeypatch TIER*_HOME take effect
# (a module-level dict would freeze the values at import).
def home_for_tier(tier):
    return {'tier1': TIER1_HOME, 'tier2': TIER2_HOME,
            'tier3': TIER3_HOME}.get(tier, TIER1_HOME)
# Per-tier fallback priority for the LEGACY rotation scheduler (retiring at
# cutover). Preserves the historical binary behavior (tier1<->tier2).
# fallback_tier() walks this, skipping any benched tier. The NEW per-task
# dispatch paths use fallback_tier_pool_aware() instead, which derives a
# primary-first, reserve-guarded order from the pool config (never drains the
# laptop before trying the sibling primary).
_FALLBACK_ORDER = {
    'tier1': ('tier2', 'tier3'),
    'tier2': ('tier1', 'tier3'),
    'tier3': ('tier2', 'tier1'),
}

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
    'tier3': 'CLAUDE_CODE_OAUTH_TOKEN_TIER3',
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


def durable_claude_env(env=None):
    """THE canonical process-env for a direct ``claude`` *inference* spawn.

    Returns an env dict with the bare ``CLAUDE_CODE_OAUTH_TOKEN`` (the var the
    ``claude`` CLI reads) bridged from the ACTIVE tier's long-lived setup-token
    (``CLAUDE_CODE_OAUTH_TOKEN_TIER{1,2}``). Any direct spawn MUST authenticate
    through this (or the equivalent ``agent_runner._apply_tier_auth`` /
    ``run_cycle.sh`` export) — otherwise the child inherits HOME's
    ``~/.claude/.credentials.json``, which rots headlessly (~14h token, no
    refresh on the fleet) and then 401s every call (the "(basic summary — AI
    write unavailable)" / Medic-silent / digest-raw class of failure).

    Pass the result as ``subprocess.run(..., env=durable_claude_env())``. Base
    defaults to ``os.environ`` (a copy); pass ``env=`` to extend a custom base.

    Fail-safe + no-op: when no setup-token is configured (or resolution raises),
    the env is returned unchanged and ``claude`` uses the default credential
    exactly as before — so this is harmless where setup-tokens aren't provisioned
    (e.g. desktop). The token value is NEVER logged.

    Back-compat wrapper over ``select_durable_claude_env`` (spec §10-W4): it
    now round-robins a per-task tier instead of pinning to the single active
    tier. When no tier is available it returns the base env UNCHANGED (the
    historical no-op), so a caller that hasn't adopted the ``(env, tier)`` API
    still gets a usable env. W4 callers should use ``select_durable_claude_env``
    to skip the run on None and to get the selected tier for cost stamping."""
    out, _tier = select_durable_claude_env(env=env)
    if out is not None:
        return out
    return dict(env) if env is not None else os.environ.copy()


def select_durable_claude_env(env=None, now=None):
    """Per-task tier-selected process env for a DIRECT ``claude`` inference
    spawn (spec §10-W4 — the digest/narrator/retrospective generators).

    Returns ``(env, tier)``:
      - ``env``: a copy of the base env with ``CLAUDE_CODE_OAUTH_TOKEN`` (and
        ``HOME``, plus the I10/I11 agents-root/git pins on a creds.json
        fallback) set for the round-robin-selected tier.
      - ``tier``: the selected tier name — the caller stamps its costs.jsonl
        row with this (``append_cost_row(account=tier)``) so the generator's
        burn is attributed to the tier that actually ran.

    Returns ``(None, None)`` when no tier is available — the caller MUST SKIP
    the run (do not spawn; there is nothing usable to spawn on). Generators
    start a FRESH session each run, so selection is session-less round-robin.
    The token value is NEVER logged.

    **NEVER RAISES:** any error resolving the tier/token (e.g. a malformed
    tier-pool config surfacing through ``select_dispatch_tier``) degrades to
    ``(None, None)`` so the caller takes its graceful fallback (raw digest /
    raw briefing / skip / keep-rows) — NOT a crash and NOT a spawn on the
    401-prone default credential. This preserves the callers' own "never
    raises" contracts even though they invoke selection unguarded."""
    base = dict(env) if env is not None else os.environ.copy()
    try:
        tier = select_dispatch_tier(now=now)
        if tier is None:
            return None, None
        out = dict(base)
        token = _setup_token_for_tier(tier)
        if token:
            out['CLAUDE_CODE_OAUTH_TOKEN'] = token
            out['HOME'] = TIER1_HOME              # setup-token = HOME-independent
        else:
            out['HOME'] = home_for_tier(tier)     # creds.json fallback: swap HOME
            if out['HOME'] != TIER1_HOME:
                out.setdefault('OURLIBERTY_AGENTS_ROOT',
                               str(Path(TIER1_HOME) / 'agents'))
                out.setdefault('GH_CONFIG_DIR',
                               os.path.join(TIER1_HOME, '.config', 'gh'))
                out.setdefault('GIT_CONFIG_GLOBAL',
                               os.path.join(TIER1_HOME, '.gitconfig'))
        return out, tier
    except Exception:  # noqa: BLE001 — never crash a generator; skip instead
        return None, None

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
    return home_for_tier(read()['tier'])


def fallback_tier(active=None):
    """Return the highest-priority fallback tier for ``active`` that is NOT
    currently benched (no active cooldown), or None if every candidate is
    benched. Generalizes the old binary 'the other tier' to the N-tier pool
    via _FALLBACK_ORDER."""
    if active is None:
        active = read()['tier']
    for t in _FALLBACK_ORDER.get(active, ()):
        if cooldown_until(t) is None:
            return t
    return None


def fallback_tier_pool_aware(from_tier, now=None):
    """Pool-aware, reserve-guarded fallback for a DISPATCHER's failure retry
    (spec §6 fallback + §8 reserve). Derives its order from the pool config:
    first a usable OTHER PRIMARY (tier1<->tier3), then a usable fallback tier
    (tier2, the laptop) — and the laptop ONLY when ``fallback_reserve_ok`` still
    holds. So a primary wall spills to the sibling primary first and reaches the
    laptop last, never past its 25% reserve. ``usable`` = no cooldown AND auth
    ok. Returns None when no eligible fallback exists (caller holds / DMs).

    Candidates must be not-benched (cooldown) AND have a real per-account auth
    SOURCE (``_has_dispatch_auth_source`` — excludes a shared-home tokenless
    tier like tier3, which would otherwise 'auth' by borrowing tier1's
    creds.json and run cross-account while the ledger stamps the wrong tier,
    the V6 gap). The caller still verifies the actual credential downstream
    (run_claude's ``_fallback_available`` / beacon's setup-token+creds check).
    What's new vs ``fallback_tier``: primary-first ORDER and the reserve GUARD
    on the laptop tier. run_claude and the beacon bot's failure-fallback use
    THIS one so their retry can never drain Larry's laptop past the reserve nor
    misattribute a tokenless shared-home tier."""
    cfg = _tier_pool_config()
    for t in cfg['primary']:
        if (t != from_tier and cooldown_until(t, now=now) is None
                and _has_dispatch_auth_source(t)):
            return t
    for t in cfg['fallback']:
        if (t != from_tier and cooldown_until(t, now=now) is None
                and _has_dispatch_auth_source(t)
                and fallback_reserve_ok(t, now=now)):
            return t
    return None


def _has_dispatch_auth_source(tier):
    """True unless ``tier`` could only authenticate by BORROWING another
    account's credentials.json — i.e. a shared-home tier (home == tier1's real
    home, e.g. tier3) with NO setup token (the V6 cross-account case). tier1
    (the home owner) and own-home tiers (tier2) pass; a real credential is
    still verified downstream. This is ``tier_auth_ok`` minus the creds.json
    *validity* read, so fallback selection excludes the misattribution case
    without requiring the callers' creds check to move here."""
    if _setup_token_for_tier(tier):
        return True
    return not (tier != 'tier1' and home_for_tier(tier) == TIER1_HOME)


def other_home():
    """Return the HOME of the OPPOSITE tier — the failure-fallback target.

    With state=tier1 (today's default), this returns the Tier 2 personal
    home, preserving the existing Tier 1 → Tier 2 fallback behavior.
    """
    t = fallback_tier()
    return home_for_tier(t) if t else TIER1_HOME


def _warn_lock_degrade(op):
    """Emit ONE stderr line when a state-lock write degrades to an unlocked
    read-modify-write on lock timeout (F5). active_tier stays logger-free by
    design (imported everywhere), so this is a best-effort stderr write —
    systemd journals it — giving an operator a signal that the serialization
    guarantee momentarily degraded under contention instead of failing
    silently. Rare (only on > _STATE_LOCK_TIMEOUT contention)."""
    try:
        sys.stderr.write(
            f'[active_tier] WARN lock-degrade: {op} took the unlocked RMW path '
            f'after a {_STATE_LOCK_TIMEOUT}s lock timeout (contention)\n')
    except Exception:  # noqa: BLE001 — a warning must never break the write
        pass


def _locked_state_write(mutate):
    """Run ``mutate(state)`` then ``_write`` under the SAME state-file lock
    ``set_cooldown`` uses, so the rotation scheduler's tier/draining writes
    can't clobber a concurrent per-task cooldown write (both RMW the one
    active-tier.json). Degrades to an unlocked RMW on lock timeout (best-effort;
    never fails the caller) — but logs the degrade (F5)."""
    def _do():
        state = read()
        mutate(state)
        _write(state)
    try:
        with file_lock.exclusive_lock(_state_lock_path(),
                                      timeout=_STATE_LOCK_TIMEOUT):
            _do()
    except file_lock.LockTimeout:
        _warn_lock_degrade('set_tier/draining')
        _do()


def set_tier(tier):
    """Persist a new active tier. Stamps ``since`` with the current UTC
    time; leaves ``next_switch_due`` and ``draining`` alone so the
    scheduler that owns those fields (PR 6.3) can write them
    independently."""
    if tier not in _VALID_TIERS:
        raise ValueError('invalid tier: ' + repr(tier))
    def _m(state):
        state['tier'] = tier
        state['since'] = datetime.now(timezone.utc).isoformat()
    _locked_state_write(_m)


def set_draining(draining):
    """Update the ``draining`` flag without touching tier/since."""
    _locked_state_write(lambda state: state.__setitem__('draining', bool(draining)))


def set_next_switch_due(when):
    """Persist the next switch deadline. Accepts a datetime (will be
    serialized to ISO8601 UTC) or None (clears the deadline)."""
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    iso = None if when is None else when.astimezone(timezone.utc).isoformat()
    _locked_state_write(lambda state: state.__setitem__('next_switch_due', iso))


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

    **Monotonic (extend-only, spec § 7):** the persisted ``until`` is
    ``max(existing_until, new_until)`` — a fresh event NEVER shortens an
    already-active bench. Without this, a short backoff event landing on top
    of a long parsed reset would prematurely un-bench a still-walled tier and
    storm it. **Flocked (spec § 7):** the read-modify-write below is serialized
    on the state file's sidecar lock so concurrent cross-tier cooldown writes
    (which per-task dispatch multiplies) cannot clobber each other (lost
    update). A lock timeout degrades to an unlocked RMW — strictly no worse
    than the pre-lock behavior, never a failed dispatch.
    """
    if tier not in _VALID_TIERS:
        raise ValueError('invalid tier: ' + repr(tier))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        with file_lock.exclusive_lock(_state_lock_path(),
                                      timeout=_STATE_LOCK_TIMEOUT):
            return _set_cooldown_locked(tier, raw_excerpt, now, kind)
    except file_lock.LockTimeout:
        _warn_lock_degrade('set_cooldown')
        return _set_cooldown_locked(tier, raw_excerpt, now, kind)


def _set_cooldown_locked(tier, raw_excerpt, now, kind):
    """The read-modify-write half of ``set_cooldown`` — call only with the
    state lock held (or accepting an unlocked degrade). ``now`` is already a
    tz-aware UTC datetime and ``tier`` already validated."""
    state = read()
    cooldowns = dict(state.get('cooldowns') or {})
    backoff = dict(state.get('cooldown_backoff') or {})

    if kind == 'auth_401':
        candidate = now + _AUTH_COOLDOWN
    else:
        parsed = parse_reset_time(raw_excerpt, now=now)
        if parsed is not None:
            candidate = parsed
            backoff.pop(tier, None)
        else:
            attempts = int(backoff.get(tier, 0)) + 1
            delay = min(
                _COOLDOWN_BACKOFF_BASE * (2 ** (attempts - 1)),
                _COOLDOWN_BACKOFF_CAP,
            )
            candidate = now + delay
            backoff[tier] = attempts

    until = _monotonic_until(cooldowns.get(tier), candidate)
    cooldowns[tier] = until.astimezone(timezone.utc).isoformat()
    state['cooldowns'] = cooldowns
    if kind != 'auth_401':
        state['cooldown_backoff'] = backoff
    _write(state)
    return cooldowns[tier]


def _monotonic_until(existing_iso, candidate):
    """Return the LATER of an existing cooldown ``until`` (ISO string, maybe
    None / unparseable) and a freshly-computed ``candidate`` datetime. Encodes
    the extend-only invariant (spec § 7): a bench is never shortened. An
    unparseable / missing existing value yields the candidate."""
    if existing_iso:
        try:
            existing = datetime.fromisoformat(existing_iso)
        except (TypeError, ValueError):
            existing = None
        if existing is not None:
            if existing.tzinfo is None:
                existing = existing.replace(tzinfo=timezone.utc)
            if existing > candidate:
                return existing
    return candidate


def _credentials_path(tier):
    """Path to the OAuth credentials file for a tier. Reads the home dirs
    at call time so tests can monkey-patch ``TIER1_HOME`` / ``TIER2_HOME``."""
    home = home_for_tier(tier)
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
    # SETUP-TOKEN-ONLY shared-home tier (e.g. tier3, whose home IS tier1's real
    # home, spec §11): with NO setup token there is NO own credentials.json —
    # falling through to _credentials_path(tier) would read tier1's creds and
    # wrongly report this tier auth-ok, so a dispatch would run under tier1's
    # OAuth while the ledger stamps this tier (silent cross-account
    # misattribution that poisons per-tier burn + calibration). Treat a missing
    # token as NOT auth-ok for such tiers. tier1 itself (the real home owner)
    # still validates against its own creds.json below.
    if tier != 'tier1' and home_for_tier(tier) == TIER1_HOME:
        return False
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
    try:
        with file_lock.exclusive_lock(_state_lock_path(),
                                      timeout=_STATE_LOCK_TIMEOUT):
            _clear_cooldown_locked(tier)
    except file_lock.LockTimeout:
        _warn_lock_degrade('clear_cooldown')
        _clear_cooldown_locked(tier)


def _clear_cooldown_locked(tier):
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
    # Per-writer-unique temp (pid+thread) so a locked writer and an unlocked-
    # degrade writer never share a temp path -> no torn file (see
    # _atomic_write_json).
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_native_id()}.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# ===========================================================================
# Per-task tier-dispatch capacity pool (spec: docs/specs/tier-dispatch-spec.md)
# ---------------------------------------------------------------------------
# select_dispatch_tier() chooses a tier PER TASK at dispatch time: round-robin
# among healthy primaries {tier1, tier3}, with tier2 (laptop) as emergency
# fallback under a reserve guard. The four dispatch wiring points (run_claude,
# the two telegram bots, durable_claude_env) and the inbox gate call this in
# place of the old single `read()['tier']`. This block is the engine only — it
# adds no dispatch behavior until those wiring points call it.
# ===========================================================================

# Advisory-lock timeouts for the state / counter read-modify-write sections.
# A timeout degrades to an unlocked RMW (best-effort, never a failed dispatch).
_STATE_LOCK_TIMEOUT = 5.0
_RR_LOCK_TIMEOUT = 2.0


def _agents_root():
    """Resolve the agents root at call time (honors OURLIBERTY_AGENTS_ROOT so
    tests redirect to a tmpdir). Mirrors ``_state_path``'s resolution."""
    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(root) if root else AGENTS_ROOT


def _state_lock_path():
    return file_lock.sidecar_lock_path(_state_path())


def _atomic_write_json(path, obj, indent=None):
    """Atomic JSON write via a PER-WRITER-UNIQUE temp file + os.replace. The
    suffix carries BOTH pid and thread-id, so two concurrent writers — even two
    THREADS in the same process on a lock-degraded path (inbox_watcher runs one
    agent_loop thread per agent) — never write the same temp file and produce a
    torn/garbled file after replace. Worst case is a benign last-writer-wins on
    the final replace, never corruption."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_native_id()}.tmp')
    tmp.write_text(json.dumps(obj, indent=indent))
    tmp.replace(path)


# ---- config (config/agent-models.json:tier_pool) --------------------------

# Conservative bootstrap 5h token budget written into the config file's
# tier_pool block: ~the documented 2026-05-27 observed peak rolling-5h volume
# (9.7M); 10M sits just above it. § 12b calibration tightens this per tier at
# runtime via the override file (no deploy). Referenced here so the constant
# documenting the seed lives next to the reader.
_BOOTSTRAP_MAX_5H_BUDGET = 10_000_000

# Fail-safe defaults used when the config file / tier_pool block is missing or
# malformed: degrade to tier1-only with NO proactive near_cap (so a config
# error becomes "use tier1," never a crash) — spec § 12.
_TIER_POOL_DEFAULTS = {
    'primary': ['tier1'],
    'fallback': ['tier2'],
    'proactive_cap_fraction': 0.85,
    'proactive_release_fraction': 0.70,
    't2_reserve_fraction': 0.25,
    'max_5h_budget_tokens': None,
    'hold_alert_minutes': 10,
}


def _config_file():
    """Path to config/agent-models.json (repo-root relative), matching the
    convention in inbox_watcher / heal_claude_max_burn_rate."""
    return Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'


def _tier_pool_config():
    """Read the ``tier_pool`` block from config/agent-models.json with
    fail-safe defaults (spec § 12). Any read/parse error or missing block
    yields ``_TIER_POOL_DEFAULTS`` (tier1-only, no near_cap). Each field is
    individually sanity-coerced so a partially-malformed block can't crash the
    selector. ``primary``/``fallback`` are filtered to recognized tiers HERE
    (not at each call site) so every consumer — the selector, tier_pool_status,
    and the § 12b calibration job — sees clean lists and never probes a
    non-tier."""
    out = dict(_TIER_POOL_DEFAULTS)
    block = None
    try:
        data = json.loads(_config_file().read_text())
        if isinstance(data, dict):
            block = data.get('tier_pool')
    except (OSError, json.JSONDecodeError):
        block = None
    if isinstance(block, dict):
        for key in _TIER_POOL_DEFAULTS:
            if key in block and block[key] is not None:
                out[key] = block[key]
    # Validity-filter the tier lists, then fail-safe to the defaults if a list
    # is missing/non-list/empties-out (e.g. primary=['bogus'] -> [] -> tier1).
    out['primary'] = [t for t in out['primary']
                      if t in _VALID_TIERS] if isinstance(
                          out['primary'], list) else []
    if not out['primary']:
        out['primary'] = list(_TIER_POOL_DEFAULTS['primary'])
    out['fallback'] = [t for t in out['fallback']
                       if t in _VALID_TIERS] if isinstance(
                           out['fallback'], list) else []
    for frac_key in ('proactive_cap_fraction', 'proactive_release_fraction',
                     't2_reserve_fraction'):
        if not isinstance(out[frac_key], (int, float)):
            out[frac_key] = _TIER_POOL_DEFAULTS[frac_key]
    budget = out['max_5h_budget_tokens']
    if budget is not None and not isinstance(budget, (int, float)):
        out['max_5h_budget_tokens'] = None
    return out


# ---- operator pin (rollback lever, selector step 0) -----------------------

def _rotation_disabled_path():
    return _agents_root() / 'rotation.disabled'


def read_operator_pin():
    """Return the tier the operator pinned via ``~/agents/rotation.disabled``,
    or None when absent / not a recognized tier. Selector step (0), spec § 4,
    and the rollback lever, spec § 16: ``echo tier1 > ~/agents/rotation.disabled``
    forces ALL dispatches onto tier1.

    Unlike ``rotate_active_tier._override_pinned_tier`` (which maps an
    empty/unreadable file to tier1 for the legacy scheduler kill-switch), the
    dispatch selector treats only an EXPLICIT valid tier as a pin — an empty or
    unparseable file yields None (fall through to pool logic). The documented
    rollback always writes an explicit tier, so this stays unambiguous; an
    accidental empty ``touch`` must not silently freeze the pool on one tier."""
    try:
        raw = _rotation_disabled_path().read_text()
    except OSError:
        return None
    tier = raw.strip().lower()
    return tier if tier in _VALID_TIERS else None


# ---- per-tier burn (account-filtered, spec § 8) ---------------------------

_BURN_WINDOW_HOURS = 5
# Sentinel/blank account values that never represent real per-tier quota burn.
_IGNORED_BURN_ACCOUNTS = frozenset({None, '', 'fixture', 'skipped'})


def _costs_path():
    return _agents_root() / 'blackboard' / 'costs.jsonl'


def _parse_cost_ts(ts_str):
    """Parse a costs.jsonl ``ts`` (ISO 8601, optional trailing Z). None on any
    parse failure — the row is then skipped, not counted."""
    if not isinstance(ts_str, str) or not ts_str:
        return None
    s = ts_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _record_quota_tokens(rec):
    """Quota-consuming token total for one costs.jsonl record: input + output +
    cache_creation (cache_read excluded — largely-free re-read, not the quota
    signal). Non-numeric fields count as 0 so a schema drift can't crash the
    sum. Mirrors heal_claude_max_burn_rate._record_tokens."""
    total = 0
    for key in ('input_tokens', 'output_tokens', 'cache_creation'):
        v = rec.get(key, 0)
        if isinstance(v, (int, float)) and v > 0:
            total += int(v)
    return total


def rolling_5h_token_volume(account=None, now=None):
    """Sum quota-consuming tokens over the trailing 5h from
    ``blackboard/costs.jsonl``. When ``account`` is given, only rows whose
    ``account`` equals it are counted (per-tier burn, spec § 8); rows with a
    sentinel/blank account ({None,'','fixture','skipped'}) are always ignored.

    The window is the CLOSED interval [now-5h, now]: rows AFTER ``now`` are
    excluded, so reconstructing a PAST burn (``now=<wall_ts>`` for § 12b
    calibration) measures only what had been spent BY that moment — not
    activity that happened afterwards. (For a live near_cap read now=present,
    so the upper bound is a no-op.) Without it, calibration folded post-wall
    rows into the wall-burn and inflated the inferred 5h ceiling.

    Fail-open: a missing or unreadable costs file, or a malformed line, yields
    0 / skips the line — burn reading must NEVER crash a dispatch (spec § 8)."""
    path = _costs_path()
    if not path.exists():
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=_BURN_WINDOW_HOURS)
    total = 0
    try:
        with open(path, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                acct = rec.get('account')
                if acct in _IGNORED_BURN_ACCOUNTS:
                    continue
                if account is not None and acct != account:
                    continue
                ts = _parse_cost_ts(rec.get('ts', ''))
                if ts is None or ts < cutoff or ts > now:
                    continue
                total += _record_quota_tokens(rec)
    except OSError:
        return 0
    return total


def append_cost_row(account, *, model='', cost_usd=None, usage=None,
                    agent='', task_id='', task_type='', source='',
                    duration_sec=None, now=None):
    """Append one account-stamped row to blackboard/costs.jsonl (spec § 8/§10).

    Used by the dispatch paths that do NOT route through
    inbox_watcher.process_task (the telegram bots W2/W3 and the
    durable_claude_env generators W4) so their burn is visible to the per-tier
    rolling-5h reader. Matches the canonical row shape inbox_watcher writes:
    the ``account`` field is what ``rolling_5h_token_volume(account=...)``
    filters on. ``usage`` is the Claude CLI ``usage`` dict (input_tokens /
    output_tokens / cache_read_input_tokens / cache_creation_input_tokens).

    Best-effort: never raises (a cost-ledger write must not break a reply or a
    generator run). A blank/None account is written through as-is — callers
    pass the effective dispatch tier."""
    usage = usage or {}
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    row = {
        'ts': now.isoformat(),
        'agent': agent,
        'task_id': task_id,
        'task_type': task_type,
        'model': model,
        'account': account,
        'cost_usd': cost_usd,
        'input_tokens': usage.get('input_tokens'),
        'output_tokens': usage.get('output_tokens'),
        'cache_read': usage.get('cache_read_input_tokens'),
        'cache_creation': usage.get('cache_creation_input_tokens'),
        'duration_sec': duration_sec,
        'source': source,
    }
    try:
        path = _costs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as f:
            f.write(json.dumps(row) + '\n')
    except OSError:
        pass


# ---- self-tuning budget calibration override (spec § 12b) -----------------

_CALIBRATION_REL = ('state', 'tier-budget-calibration.json')


def _calibration_path():
    return _agents_root().joinpath(*_CALIBRATION_REL)


def _effective_budget(tier, cfg):
    """Per-tier 5h token budget: the § 12b runtime calibration override
    (``~/agents/state/tier-budget-calibration.json``, self-tuned from real
    wall-hits, no deploy) if present and positive, else the config default.
    None => proactive gating (near_cap / reserve) disabled for this tier."""
    try:
        data = json.loads(_calibration_path().read_text())
        if isinstance(data, dict):
            val = data.get(tier)
            if isinstance(val, (int, float)) and val > 0:
                return val
    except (OSError, json.JSONDecodeError):
        pass
    budget = cfg.get('max_5h_budget_tokens')
    return budget if isinstance(budget, (int, float)) and budget > 0 else None


# ---- proactive cap (near_cap) with hysteresis -----------------------------

_NEAR_CAP_LATCH_REL = ('state', 'tier-near-cap-latch.json')


def _near_cap_latch_path():
    return _agents_root().joinpath(*_NEAR_CAP_LATCH_REL)


def _read_near_cap_latch(tier):
    try:
        data = json.loads(_near_cap_latch_path().read_text())
        return bool(data.get(tier)) if isinstance(data, dict) else False
    except (OSError, json.JSONDecodeError):
        return False


def _write_near_cap_latch(tier, value):
    """Persist one tier's near_cap latch — ONLY under the lock. On lock
    contention we SKIP the write (the transition is simply re-evaluated and
    re-persisted on the next call) rather than degrading to an unlocked
    read-modify-write of the shared multi-tier dict, which could lose another
    tier's concurrent latch flip (and so fail to skip a tier that just crossed
    the cap — defeating the proactive guard exactly under the concurrent load
    this feature introduces). A missed latch update is self-correcting; a lost
    update is not."""
    path = _near_cap_latch_path()
    try:
        with file_lock.exclusive_lock(file_lock.sidecar_lock_path(path),
                                      timeout=_STATE_LOCK_TIMEOUT):
            _write_near_cap_latch_locked(path, tier, value)
    except (file_lock.LockTimeout, OSError):
        pass


def _write_near_cap_latch_locked(path, tier, value):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[tier] = bool(value)
    _atomic_write_json(path, data)


def near_cap(tier, now=None, update_latch=True):
    """True iff ``tier``'s rolling-5h burn is at/above the proactive cap
    (spec § 8), with hysteresis: once tripped it stays True until burn falls
    below the lower release line, so a tier oscillating around the cap doesn't
    flap. Returns False (gating disabled) when no positive budget is known —
    the common pre-calibration / config-degraded case. Burn read is fail-open
    (0 on a corrupt costs file) so this never blocks a dispatch on an I/O
    error.

    ``update_latch`` (default True) persists a hysteresis-state TRANSITION
    (rare — only when burn crosses a threshold). Pass ``False`` for a pure
    read (observability: ``tier_pool_status`` / the all-held alert) so an
    observability poll never mutates routing state. The persist is lock-only
    (see ``_write_near_cap_latch``); a lock miss just defers the transition."""
    cfg = _tier_pool_config()
    budget = _effective_budget(tier, cfg)
    if not budget or budget <= 0:
        return False
    burn = rolling_5h_token_volume(account=tier, now=now)
    cap = cfg['proactive_cap_fraction'] * budget
    release = cfg['proactive_release_fraction'] * budget
    latched = _read_near_cap_latch(tier)
    new_state = (burn >= release) if latched else (burn >= cap)
    if update_latch and new_state != latched:
        _write_near_cap_latch(tier, new_state)
    return new_state


def fallback_reserve_ok(tier, now=None):
    """True iff spilling an emergency dispatch onto ``tier`` (the laptop's
    tier2) keeps the SYSTEM's own tier2 5h burn below the reserve fraction
    (spec § 8) — so a spill never drains Larry's laptop account out from under
    him. We can only see our own dispatches' burn (the laptop logs nothing
    here); best-effort but bounded.

    FAIL-SAFE (spec § 8 'fail-safe'): when no positive budget is known (total
    config loss AND no calibration), we CANNOT bound the spill, so we return
    False → the selector HOLDS rather than spilling unbounded onto Larry's
    personal account. Holding is recoverable (the task waits + the § 9 all-held
    alert fires); draining the laptop is not. In normal operation the config
    seeds a budget, so the guard is always active."""
    cfg = _tier_pool_config()
    budget = _effective_budget(tier, cfg)
    if not budget or budget <= 0:
        return False
    burn = rolling_5h_token_volume(account=tier, now=now)
    return burn < cfg['t2_reserve_fraction'] * budget


# ---- round-robin counter (flocked, spec § 7) ------------------------------

def _rr_counter_path():
    return _agents_root() / 'state' / 'tier-rr-counter'


def _read_rr_counter(path):
    try:
        data = json.loads(path.read_text())
        return int(data.get('counter', 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return 0


def _write_rr_counter(path, counter):
    _atomic_write_json(path, {'counter': int(counter)})


def round_robin(pool):
    """Pick the next tier from ``pool`` (order-preserving) via a flocked
    monotonic counter at ``~/agents/state/tier-rr-counter`` (spec § 7).
    Cold-start counter = 0. On lock-unavailable OR any filesystem fault
    (unwritable/EROFS/full disk — this system has a history of HOME-swap EROFS
    write failures), degrade to a process-local random pick rather than
    crashing the dispatch (the counter race at worst skews distribution by one,
    self-correcting). Empty pool => None."""
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    path = _rr_counter_path()
    try:
        with file_lock.exclusive_lock(file_lock.sidecar_lock_path(path),
                                      timeout=_RR_LOCK_TIMEOUT):
            counter = _read_rr_counter(path)
            pick = pool[counter % len(pool)]
            _write_rr_counter(path, counter + 1)
            return pick
    except (file_lock.LockTimeout, OSError):
        return random.choice(pool)


# ---- durable session -> tier map (I2, spec § 5) ---------------------------

_SESSION_MAP_REL = ('state', 'session-tier-map.json')
# Prune entries older than this on write to bound the map's growth.
_SESSION_MAP_MAX_AGE = timedelta(days=14)


def _session_tier_map_path():
    return _agents_root().joinpath(*_SESSION_MAP_REL)


def lookup_session_tier(session_id):
    """Return the tier a session was originally dispatched on (spec § 5), or
    None when the session is unknown / the map is unreadable. Callers pass the
    result to ``select_dispatch_tier(session_tier=...)`` so a ``--resume`` never
    crosses tiers (I2).

    Spec § 5 requires a WARN when a resume finds NO binding (a pre-existing or
    pruned session) — that log is the WIRING POINT's responsibility (it has the
    agent_id/task context for a useful line and the resume-discipline that
    refuses cross-tier fallback). This engine helper stays logger-free by
    design; a None return on a known resume is the wiring point's cue to WARN."""
    if not session_id:
        return None
    try:
        data = json.loads(_session_tier_map_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(session_id)
    if isinstance(entry, dict):
        tier = entry.get('tier')
        return tier if tier in _VALID_TIERS else None
    return None


def record_session_tier(session_id, tier, now=None):
    """Persist ``session_id -> tier`` (atomic + flocked, spec § 5/I16) after a
    NEW dispatch creates a session. No-op for a blank session_id or invalid
    tier. Stale entries (> 14d) are pruned on write to bound growth."""
    if not session_id or tier not in _VALID_TIERS:
        return
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = _session_tier_map_path()
    try:
        with file_lock.exclusive_lock(file_lock.sidecar_lock_path(path),
                                      timeout=_STATE_LOCK_TIMEOUT):
            _record_session_tier_locked(path, session_id, tier, now)
    except file_lock.LockTimeout:
        _warn_lock_degrade('record_session_tier')
        _record_session_tier_locked(path, session_id, tier, now)


def _record_session_tier_locked(path, session_id, tier, now):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[session_id] = {'tier': tier, 'ts': now.isoformat()}
    cutoff = now - _SESSION_MAP_MAX_AGE
    pruned = {}
    for sid, ent in data.items():
        if sid == session_id:
            pruned[sid] = ent
            continue
        if not isinstance(ent, dict):
            continue
        ts_dt = _parse_cost_ts(ent.get('ts', ''))
        # Keep entries that are fresh OR whose ts we can't date (conservative).
        if ts_dt is None or ts_dt >= cutoff:
            pruned[sid] = ent
    _atomic_write_json(path, pruned, indent=2)


# ---- usable() + the selector (spec § 4) -----------------------------------

def usable(tier, now=None):
    """A tier is usable iff it has no active cooldown AND its dispatch auth is
    OK (I1, I14). Mirrors the spec § 4 ``usable``."""
    return cooldown_until(tier, now=now) is None and tier_auth_ok(tier, now=now)


def select_dispatch_tier(session_tier=None, now=None):
    """Choose the tier for ONE dispatch (spec § 4). Returns a tier name, or
    None — and **None means the caller MUST NOT dispatch**: it holds / re-polls
    (the inbox gate escalates per § 9). The caller re-verifies ``usable(picked)``
    immediately before spawn (TOCTOU mitigation, I14) and re-selects if it
    flipped.

    Order:
      0. Operator pin (``rotation.disabled``) — forced; makes rollback work.
      1. Session binding (I2) — a resumed task keeps its tier iff still usable,
         else None (caller PAUSES; never cross-tier migrate).
      2. Round-robin among healthy primaries under the proactive cap.
      3. Primaries usable-but-near-cap: use them anyway (don't spill to the
         laptop for mere load).
      4. Emergency: both primaries benched -> fallback (tier2) under the
         reserve guard. Else None (hold + escalate)."""
    pin = read_operator_pin()
    if pin:
        return pin
    cfg = _tier_pool_config()  # primary/fallback already validity-filtered
    if session_tier:
        if session_tier in _VALID_TIERS and usable(session_tier, now=now):
            return session_tier
        return None
    primary = cfg['primary']
    healthy = [t for t in primary
               if usable(t, now=now) and not near_cap(t, now=now)]
    if healthy:
        return round_robin(healthy)
    usable_primary = [t for t in primary if usable(t, now=now)]
    if usable_primary:
        return round_robin(usable_primary)
    for t in cfg['fallback']:
        if usable(t, now=now) and fallback_reserve_ok(t, now=now):
            return t
    return None


def has_usable_dispatch_tier(now=None):
    """True iff ``select_dispatch_tier()`` would return a tier — but WITHOUT
    the round-robin counter side effect. For the inbox gate's hold-clock probe
    (§ 9): the gate must decide "is the pool serving?" on every poll (incl.
    continuation-only and idle polls) to clear a stale all-held debounce clock,
    and it must NOT bump the round-robin counter while doing so (that would skew
    the tier1/tier3 alternation). Equivalent to select returning non-None: an
    operator pin, OR any usable primary (near_cap primaries still dispatch via
    path 3), OR a usable fallback under its reserve."""
    if read_operator_pin():
        return True
    cfg = _tier_pool_config()
    if any(usable(t, now=now) for t in cfg['primary']):
        return True
    return any(usable(t, now=now) and fallback_reserve_ok(t, now=now)
               for t in cfg['fallback'])


def tier_pool_status(now=None):
    """Snapshot of the pool for observability (spec § 15): per-tier
    {usable, cooldown_until, near_cap, burn_5h} plus the resolved pool config
    and the active operator pin. Pure read; used by the dashboard rotation
    endpoint and the all-held alert. Never raises — each tier is probed
    defensively."""
    cfg = _tier_pool_config()
    tiers = {}
    for t in _VALID_TIERS:
        try:
            tiers[t] = {
                'usable': usable(t, now=now),
                'cooldown_until': cooldown_until(t, now=now),
                'auth_ok': tier_auth_ok(t, now=now),
                # update_latch=False: an observability poll must never mutate
                # routing (hysteresis-latch) state — see near_cap().
                'near_cap': near_cap(t, now=now, update_latch=False),
                'burn_5h': rolling_5h_token_volume(account=t, now=now),
                'budget_5h': _effective_budget(t, cfg),
            }
        except Exception:  # noqa: BLE001 — observability must never raise
            tiers[t] = {'usable': False, 'error': True}
    return {
        'primary': cfg['primary'],   # already validity-filtered
        'fallback': cfg['fallback'],
        'operator_pin': read_operator_pin(),
        'tiers': tiers,
    }


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
