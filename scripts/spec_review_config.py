#!/usr/bin/env python3
"""spec_review_config.py — config + override resolution for the spec gauntlet.

Foundations slice of agents/beacon/specs/spec-gauntlet-gate.md (§3.6). The gate
has repo defaults in `config/spec-review.json` and a LIVE override in
`~/agents/spec-review.override.json`, resolved exactly like the trust policy
(scripts/trust_policy.py): override wins, then the synced runtime snapshot, then
the git-tracked repo copy.

Two properties the spec calls out explicitly (§3.6, AC-3):

1. **Read FRESH on every call — no module-level cache.** `intercept()` calls
   `is_enabled()` on every spec; flipping `enabled` in the override must take
   effect on the very next spec with zero daemon restarts. So there is no cached
   dict at import time — every `load_config()` re-reads from disk.

2. **Fail SAFE to disabled.** A missing/unreadable/malformed config degrades to
   the DEFAULTS with `enabled: False` — i.e. the gate is OFF and the pipeline
   falls back to the byte-identical legacy stamp path. A broken config can never
   block the approval pipeline (it also never silently turns the gauntlet ON).

The override lives OUTSIDE the synced `config/` tree (a sibling of
`~/agents/trust-policy.override.json`) so ourliberty-sync — which rsyncs
`config/` from the repo on every sync — never clobbers it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or HOME / 'agents')
REPO_ROOT = Path(__file__).resolve().parent.parent

# Same three-tier shape as trust_policy: the override (dial) wins, then the
# synced runtime snapshot the agent OS reads, then the repo copy (first-run
# bootstrap). The override is deliberately OUTSIDE the synced config/ tree so a
# sync never clobbers a live edit.
OVERRIDE_CONFIG_PATH = AGENTS_ROOT / 'spec-review.override.json'
RUNTIME_CONFIG_PATH = AGENTS_ROOT / 'config' / 'spec-review.json'
REPO_CONFIG_PATH = REPO_ROOT / 'config' / 'spec-review.json'

# Fail-safe defaults: gate OFF (legacy stamp path), so a missing/broken config
# never blocks the pipeline and never silently enables the gauntlet.
DEFAULTS: dict[str, Any] = {
    'version': 1,
    'enabled': False,
    'max_rounds': 2,
    'per_step_ceiling_s': 900,
    'wall_clock_ceiling_s': 4500,
    'gated_sites': ['bot_chat', 'replan', 'pulse_auto_dispatch'],
}


def _resolve_config_path() -> Path:
    # Override (dial) wins, then the synced runtime snapshot, then the repo copy
    # (first-run bootstrap). A malformed file at any tier degrades to DEFAULTS in
    # load_config, so this never crashes.
    if OVERRIDE_CONFIG_PATH.exists():
        return OVERRIDE_CONFIG_PATH
    if RUNTIME_CONFIG_PATH.exists():
        return RUNTIME_CONFIG_PATH
    return REPO_CONFIG_PATH


def load_config() -> dict[str, Any]:
    """Return the effective spec-review config, read FRESH from disk on every
    call (no module-level cache — AC-3). Missing/unreadable/malformed files
    degrade to DEFAULTS (gate disabled), never raise."""
    path = _resolve_config_path()
    if not path.exists():
        return dict(DEFAULTS)
    try:
        with open(path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    # Overlay onto DEFAULTS so a partial override (e.g. just `{"enabled": true}`)
    # still yields a complete, well-typed config.
    merged = dict(DEFAULTS)
    merged.update(raw)
    return merged


def is_enabled() -> bool:
    """True iff the gate is enabled in the effective (freshly-read) config. Any
    non-boolean `enabled` value is treated as disabled — no truthy-string
    fail-open (the `enabled: "false"` trap Lens E flags)."""
    return load_config().get('enabled') is True


def gated_sites() -> list[str]:
    """The stamp sites the gauntlet intercepts, from the freshly-read config."""
    sites = load_config().get('gated_sites')
    return list(sites) if isinstance(sites, list) else list(DEFAULTS['gated_sites'])
