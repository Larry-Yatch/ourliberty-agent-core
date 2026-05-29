#!/usr/bin/env python3
"""rotate_active_tier.py — proactive account-rotation scheduler.

Spec: ``agents/beacon/specs/account-rotation.md`` § 6.3.

One tick (driven by ``ourliberty-rotate-active-tier.timer``, ~2 min cadence)
does the following, in order:

1. Read the ``rotation`` block from ``config/agent-models.json``. If the
   block is missing or ``enabled=false``, FORCE ``tier1`` + clear any
   partial drain state and exit. This is the master kill switch — Larry
   can disable the rotation at any time by flipping ``enabled`` to false;
   the next tick reverts state to today's behavior.

2. Decide "engaged" — for THIS PR, ``engaged = enabled``. The load-gated
   engage/disengage hysteresis lands in § 6.4 (a separate PR) and reads
   ``engage_5h_spend_usd`` / ``disengage_5h_spend_usd`` from the same
   block. For now, when enabled, we always run the rotation cadence.

3. If engaged and not draining: initialize ``next_switch_due`` if missing,
   otherwise compare against ``now``. When the window has elapsed, set
   ``draining=true``. The drain phase is when in-flight work gets a chance
   to finish on its original account before the switch.

4. If draining: check if ``IN_FLIGHT_DIR`` is empty AND no open build
   sequence is running. If so, FLIP the tier, reset ``since`` and
   ``next_switch_due`` for the new tier's window, and clear ``draining``.
   If ``max_drain_minutes`` has been exceeded, DEFER the flip (log + leave
   ``draining=true``) — NEVER force-kill in-flight work. The next tick
   will re-evaluate.

Idempotency: every tick reads state fresh, recomputes, writes only the
minimum necessary state. Multiple concurrent ticks would race on the
state file write but ``active_tier._write`` uses tmp-then-rename. The
systemd timer is OneShot so concurrency is bounded to at most one extra
overlapping run on a slow tick — acceptable for a 2-min cadence.

Cooldown management is handled by ``active_tier.cooldown_until()`` (read
side, watcher gate) and ``active_tier.set_cooldown()`` (write side,
agent_runner rate-limit branches). This scheduler does NOT clear or set
cooldowns; expiry is filtered at read time.

stdlib only. CLI:

    python3 scripts/rotate_active_tier.py --once
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import active_tier  # noqa: E402

HOME = Path.home()
AGENT_CORE = HOME / 'agent-core'
DEFAULT_MODELS_FILE = AGENT_CORE / 'config' / 'agent-models.json'

LOG_FILE_NAME = 'rotate-active-tier.log'

# Spec § 6.3 default config values; only used when the rotation block is
# missing OR partial. The shipped block in config/agent-models.json carries
# the same numbers; this duplication is a defensive default-of-defaults.
_DEFAULTS = {
    'enabled': False,
    'tier1_window_minutes': 120,
    'tier2_window_minutes': 60,
    'max_drain_minutes': 45,
}


def _agents_root():
    """Resolve ~/agents (honors OURLIBERTY_AGENTS_ROOT for tests). Resolved
    at call time so an env override set after import takes effect."""
    root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(root) if root else (HOME / 'agents')


def _in_flight_dir():
    return _agents_root() / 'state' / 'in-flight'


def _build_sequences_dir():
    return _agents_root() / 'blackboard' / 'build-sequences'


def _log_file():
    log_dir = os.environ.get('OURLIBERTY_LOG_DIR')
    base = Path(log_dir) if log_dir else (_agents_root() / 'logs')
    base.mkdir(parents=True, exist_ok=True)
    return base / LOG_FILE_NAME


def _logger():
    logger = logging.getLogger('rotate_active_tier')
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(_log_file())
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s',
    ))
    logger.addHandler(handler)
    # Also echo to stdout so the systemd journal carries it.
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    logger.addHandler(stream)
    logger.propagate = False
    return logger


def _load_rotation_config(models_file=None):
    """Return the rotation block merged on top of defaults. Missing file,
    unreadable file, malformed JSON, or missing block all collapse to the
    safe default (enabled=false) — same defense-in-depth posture as
    ``active_tier.read``."""
    path = Path(models_file) if models_file else DEFAULT_MODELS_FILE
    cfg = dict(_DEFAULTS)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    block = data.get('rotation')
    if not isinstance(block, dict):
        return cfg
    cfg.update({k: v for k, v in block.items() if not k.startswith('_')})
    return cfg


def _in_flight_empty():
    """True if ``state/in-flight/`` has no JSON registry entries. Other file
    shapes (e.g., a stray README) are ignored. A missing directory is
    treated as empty — the watcher creates it on first dispatch."""
    p = _in_flight_dir()
    if not p.exists():
        return True
    return not any(p.glob('*.json'))


_OPEN_SEQUENCE_STATUSES = ('pending', 'active', 'paused')


def _any_open_build_sequence():
    """True if ``blackboard/build-sequences/*.json`` contains any sequence
    whose ``status`` is one of pending/active/paused (the build_sequence_
    advancer treats these as "could still produce work"). complete / failed
    / archived are terminal and do not block a tier flip."""
    p = _build_sequences_dir()
    if not p.exists():
        return False
    for entry in p.glob('*.json'):
        try:
            seq = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            # A malformed sequence file is build_sequence_advancer's problem
            # (it auto-pauses on schema-invalid). Treat as NOT blocking the
            # flip — the operator will see the advancer's DM and recover.
            continue
        if not isinstance(seq, dict):
            continue
        if seq.get('status') in _OPEN_SEQUENCE_STATUSES:
            return True
    return False


def _window_for(tier, cfg):
    """Window length for the given tier, in minutes, from config."""
    return int(cfg.get(
        'tier1_window_minutes' if tier == 'tier1' else 'tier2_window_minutes',
        _DEFAULTS['tier1_window_minutes' if tier == 'tier1' else 'tier2_window_minutes'],
    ))


def _other_tier(tier):
    return 'tier2' if tier == 'tier1' else 'tier1'


def _parse_iso(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def tick(now=None, models_file=None, logger=None):
    """Run one scheduler tick. Returns a short dict summarizing the action
    taken — useful for tests + structured logging. Side effects: at most one
    sequence of writes to ``blackboard/active-tier.json`` via the
    ``active_tier`` helpers."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    logger = logger or _logger()
    cfg = _load_rotation_config(models_file)
    state = active_tier.read()
    tier = state['tier']
    enabled = bool(cfg.get('enabled'))

    # Master kill switch — when disabled, force tier1 + clear drain state.
    # Idempotent: a no-op when already tier1 + not draining.
    if not enabled:
        actions = []
        if tier != 'tier1':
            active_tier.set_tier('tier1')
            actions.append('forced-tier1')
        if state['draining']:
            active_tier.set_draining(False)
            actions.append('cleared-draining')
        if state['next_switch_due']:
            active_tier.set_next_switch_due(None)
            actions.append('cleared-next-switch-due')
        result = {'action': 'disabled', 'changes': actions, 'tier': 'tier1'}
        logger.info('rotation disabled tick: ' + json.dumps(result))
        return result

    # § 6.3 LOCKED DECISION: engaged = always-true when enabled. Load-gating
    # lands in § 6.4 as a follow-up PR.
    engaged = True
    if not engaged:
        result = {'action': 'not-engaged', 'tier': tier}
        logger.info('rotation tick: ' + json.dumps(result))
        return result

    if state['draining']:
        # Drain phase. Check whether the world has settled enough to flip,
        # OR whether we've waited too long and should defer until next tick.
        in_flight_clear = _in_flight_empty()
        no_open_seq = not _any_open_build_sequence()
        if in_flight_clear and no_open_seq:
            # FLIP — set new tier, reset window, clear draining. The order
            # matters: set_tier() stamps `since`; set_next_switch_due() pins
            # the next deadline; set_draining(False) opens the gate. A
            # crash between writes leaves state in an intermediate-but-safe
            # shape (the gate is still closed until set_draining lands).
            next_tier = _other_tier(tier)
            active_tier.set_tier(next_tier)
            window_min = _window_for(next_tier, cfg)
            active_tier.set_next_switch_due(now + timedelta(minutes=window_min))
            active_tier.set_draining(False)
            result = {
                'action': 'flipped',
                'from_tier': tier,
                'to_tier': next_tier,
                'next_switch_due_minutes': window_min,
            }
            logger.info('rotation flip: ' + json.dumps(result))
            return result

        # Drain still pending. Has the timeout elapsed?
        max_drain = int(cfg.get('max_drain_minutes',
                                _DEFAULTS['max_drain_minutes']))
        since = _parse_iso(state.get('since')) or now
        elapsed = now - since
        if elapsed > timedelta(minutes=max_drain):
            # DEFER — log + leave draining=true. Never force-kill. Next tick
            # will re-evaluate; eventually the in-flight work finishes (or
            # Larry's operator action clears it) and the flip lands.
            result = {
                'action': 'drain-deferred',
                'tier': tier,
                'elapsed_minutes': int(elapsed.total_seconds() // 60),
                'max_drain_minutes': max_drain,
                'in_flight_clear': in_flight_clear,
                'open_build_sequence': not no_open_seq,
            }
            logger.warning('rotation drain deferred: ' + json.dumps(result))
            return result

        result = {
            'action': 'drain-waiting',
            'tier': tier,
            'elapsed_minutes': int(elapsed.total_seconds() // 60),
            'in_flight_clear': in_flight_clear,
            'open_build_sequence': not no_open_seq,
        }
        logger.info('rotation drain waiting: ' + json.dumps(result))
        return result

    # Not draining. Check the window deadline.
    deadline = _parse_iso(state.get('next_switch_due'))
    if deadline is None:
        # First tick after enable (or after a force-tier1 disable+reenable
        # cycle). Pin a fresh deadline for the current tier and exit; the
        # next tick will start counting down.
        window_min = _window_for(tier, cfg)
        active_tier.set_next_switch_due(now + timedelta(minutes=window_min))
        result = {
            'action': 'initialized-window',
            'tier': tier,
            'next_switch_due_minutes': window_min,
        }
        logger.info('rotation init window: ' + json.dumps(result))
        return result

    if deadline <= now:
        # Window elapsed — open the drain gate. The watcher will start
        # blocking new top-level dispatches on the next 5s poll.
        active_tier.set_draining(True)
        result = {
            'action': 'drain-started',
            'tier': tier,
            'overdue_minutes': int((now - deadline).total_seconds() // 60),
        }
        logger.info('rotation drain started: ' + json.dumps(result))
        return result

    result = {
        'action': 'idle',
        'tier': tier,
        'remaining_minutes': int((deadline - now).total_seconds() // 60),
    }
    return result


def main():
    parser = argparse.ArgumentParser(
        description='Account-rotation scheduler (spec § 6.3).',
    )
    parser.add_argument(
        '--once', action='store_true',
        help='Run exactly one tick and exit. systemd timer drives the loop; '
             'this is the only supported mode.',
    )
    parser.add_argument(
        '--models-file', default=None,
        help='Path to agent-models.json. Defaults to ~/agent-core/config/.',
    )
    args = parser.parse_args()
    if not args.once:
        # The script is OneShot under the systemd timer; running without
        # --once is almost certainly a manual mistake.
        print('error: --once is required (systemd timer drives the cadence)',
              file=sys.stderr)
        return 2
    result = tick(models_file=args.models_file)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
