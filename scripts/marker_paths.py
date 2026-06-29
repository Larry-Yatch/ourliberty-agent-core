#!/usr/bin/env python3
"""marker_paths.py -- single source of truth for the watchdog/Medic
restart-coordination marker paths.

scripts/watchdog.py and scripts/medic_actions.py both reason about the same
on-disk coordination markers under <agents_root>/state/. watchdog WRITES them
(flap streak, mem-restart cooldown, reconcile cooldown); Medic READS them in
_recent_peer_restart to defer to a peer already restarting a unit. The path
conventions were previously REPLICATED in both files -- a silent-drift risk: a
marker watchdog moves but Medic keeps reading at the old path would let Medic
reset systemd backoff on a flapping unit, undercutting the watchdog's
defer-on-flap discipline. This module is the one place the conventions live.

Why agents_root is a parameter rather than a module constant: watchdog
hardcodes AGENTS_ROOT = ~/agents, while medic_actions honors an
OURLIBERTY_AGENTS_ROOT override that its env contract and test isolation depend
on. A shared module that imported either caller's constant would break the
other; passing agents_root in keeps both callers authoritative over their own
root while sharing the path *shape*.

Stdlib only. Pure functions -- no I/O, never raises.
"""

from __future__ import annotations

from pathlib import Path

_STATE_SUBDIR = 'state'
_FLAP_STREAK_SUBDIR = 'auto-restart-flap'


def flap_marker_name(unit: str) -> str:
    """The flap-streak marker is keyed by the service name WITHOUT the
    '.service' suffix but WITH the 'ourliberty-' prefix -- the form used in
    watchdog.AUTO_RESTART_SERVICES (e.g.
    'ourliberty-inbox-watcher.service' -> 'ourliberty-inbox-watcher')."""
    return unit[:-len('.service')] if unit.endswith('.service') else unit


def unit_short_name(unit: str) -> str:
    """The cooldown markers are keyed by the SHORT name: strip BOTH the
    'ourliberty-' prefix AND the '.service' suffix (e.g.
    'ourliberty-inbox-watcher.service' -> 'inbox-watcher'). Mirrors the literal
    'inbox-watcher-mem-restart-cooldown' marker watchdog writes and the policy
    short keys used for '<short>-reconcile-cooldown'."""
    name = flap_marker_name(unit)
    if name.startswith('ourliberty-'):
        name = name[len('ourliberty-'):]
    return name


def state_dir(agents_root: Path) -> Path:
    """<agents_root>/state -- the directory every coordination marker lives in."""
    return Path(agents_root) / _STATE_SUBDIR


def flap_streak_dir(agents_root: Path) -> Path:
    """<agents_root>/state/auto-restart-flap -- the per-service flap-streak
    counter directory (watchdog keeps this as the _FLAP_STREAK_DIR constant it
    patches in tests, so it's exposed as a directory builder, not only a
    full-path builder)."""
    return state_dir(agents_root) / _FLAP_STREAK_SUBDIR


def flap_streak_path(agents_root: Path, service_name: str) -> Path:
    """<agents_root>/state/auto-restart-flap/<service_name>. service_name is the
    flap_marker_name() form (prefix kept, '.service' stripped) -- the same value
    watchdog.AUTO_RESTART_SERVICES carries."""
    return flap_streak_dir(agents_root) / service_name


def mem_restart_cooldown_path(agents_root: Path, short: str) -> Path:
    """<agents_root>/state/<short>-mem-restart-cooldown. short is the
    unit_short_name() form."""
    return state_dir(agents_root) / f'{short}-mem-restart-cooldown'


def reconcile_marker_path(agents_root: Path, short: str) -> Path:
    """<agents_root>/state/<short>-reconcile-cooldown. short is the
    unit_short_name() form."""
    return state_dir(agents_root) / f'{short}-reconcile-cooldown'
