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
from datetime import datetime, timezone
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
}

_VALID_TIERS = ('tier1', 'tier2')


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


def _write(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
