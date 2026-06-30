#!/usr/bin/env python3
"""rotate_active_tier.py — proactive account-rotation scheduler.

Spec: ``agents/beacon/specs/account-rotation.md`` §§ 6.3 + 6.4.

One tick (driven by ``ourliberty-rotate-active-tier.timer``, ~2 min cadence)
does the following, in order:

1. Read the ``rotation`` block from ``config/agent-models.json``. If the
   block is missing or ``enabled=false``, FORCE ``tier1`` + clear any
   partial drain state and exit. This is the master kill switch — Larry
   can disable the rotation at any time by flipping ``enabled`` to false;
   the next tick reverts state to today's behavior.

2. Decide "engaged" via § 6.4 load gating with hysteresis. Read the
   rolling-5h quota-consuming token volume from
   ``heal_claude_max_burn_rate.rolling_5h_token_volume()`` and the
   pulse-tuned tier1 token threshold from ``load_threshold()``. Compute
   ``engage_thr = engage_at_fraction * threshold`` and ``disengage_thr =
   disengage_at_fraction * threshold``. ENGAGE when usage >= engage_thr;
   DISENGAGE (force tier1 + clear drain) when usage < disengage_thr; HOLD
   the current engaged state when usage is between the two — this is what
   prevents the gate from flapping near the boundary. The fractions track
   the pulse-tuned threshold so when Check VIII raises or lowers the
   ceiling, the gate moves with it.

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

Each state-changing action (engage / disengage / switch / drain-defer)
appends one JSON line to ``blackboard/rotation-events.jsonl`` for a future
Pulse Check to tune the ratio/thresholds against ground-truth rate-limit
events. Schema: ``{ts, action, from_tier, to_tier, trigger,
rolling_5h_tokens, threshold, drained_after_sec}``.

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
import heal_claude_max_burn_rate as hcmbr  # noqa: E402

HOME = Path.home()
AGENT_CORE = HOME / 'agent-core'
DEFAULT_MODELS_FILE = AGENT_CORE / 'config' / 'agent-models.json'

LOG_FILE_NAME = 'rotate-active-tier.log'
EVENTS_FILE_NAME = 'rotation-events.jsonl'

# Runtime override file. Presence forces the scheduler off (tier1, no drain)
# exactly like config rotation.enabled=false, but unlike the config default it
# is a live, dashboard-toggleable switch that mutates no tracked file — same
# idiom as the fleet-wide ~/agents/healers.disabled kill switch.
OVERRIDE_DISABLE_FILE_NAME = 'rotation.disabled'

# Manual-pin support (spec § 6.5). The override file's CONTENTS carry the tier
# the operator has pinned: 'tier1' or 'tier2'. An empty file (the historical
# `touch` the pre-pin dashboard Off control wrote) or unrecognized contents map
# to tier1 — identical to the original force-tier1 Off semantics, so a file
# written by an older dashboard build still pins tier1.
_OVERRIDE_VALID_TIERS = ('tier1', 'tier2', 'tier3')
_OVERRIDE_DEFAULT_TIER = 'tier1'

# Spec §§ 6.3 + 6.4 default config values; only used when the rotation block
# is missing OR partial. The shipped block in config/agent-models.json
# carries the same numbers; this duplication is a defensive
# default-of-defaults. engage_at_fraction / disengage_at_fraction are
# fractions of tier1_quota.max_5h_token_threshold (see § 6.4 / config
# _note); using fractions keeps the gate tracking the pulse-tuned token
# threshold rather than a second hand-tuned constant.
_DEFAULTS = {
    'enabled': False,
    'tier1_window_minutes': 120,
    'tier2_window_minutes': 60,
    'max_drain_minutes': 45,
    'engage_at_fraction': 0.70,
    'disengage_at_fraction': 0.50,
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


def _override_disabled():
    """True when the runtime override file ``~/agents/rotation.disabled``
    exists. Mirrors the shared ``~/agents/healers.disabled`` idiom: presence
    means off. Resolved at call time so a touch/rm between ticks takes effect
    on the next tick."""
    return (_agents_root() / OVERRIDE_DISABLE_FILE_NAME).exists()


def _override_pinned_tier():
    """Return the tier the operator has pinned via the override file's
    contents, or ``None`` when the file is absent (Auto).

    Contents map: ``'tier1'``/``'tier2'`` → that tier; empty or unrecognized
    → ``tier1`` (the historical Off behavior — a file written by an older
    dashboard build, which just touched it, still pins tier1). A present-but-
    unreadable file also maps to tier1 rather than None, so a transient read
    error cannot silently re-engage load-gated rotation against the operator's
    intent. Resolved at call time so an edit between ticks takes effect on the
    next tick."""
    path = _agents_root() / OVERRIDE_DISABLE_FILE_NAME
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return _OVERRIDE_DEFAULT_TIER
    tier = raw.strip().lower()
    return tier if tier in _OVERRIDE_VALID_TIERS else _OVERRIDE_DEFAULT_TIER


def _events_file():
    """Resolve ``blackboard/rotation-events.jsonl``. Append-only; one JSON
    line per state-changing tick. Consumed by a future Pulse Check to tune
    the rotation ratio and load-gate fractions against the ground-truth
    rate-limit ledger."""
    return _agents_root() / 'blackboard' / EVENTS_FILE_NAME


def _emit_event(action, from_tier, to_tier, trigger, rolling_5h_tokens,
                threshold, drained_after_sec=None, now=None):
    """Append one event line to ``blackboard/rotation-events.jsonl``.

    Schema (per spec § 6.4):

        {ts, action, from_tier, to_tier, trigger, rolling_5h_tokens,
         threshold, drained_after_sec}

    Write failures are swallowed (best-effort observability — a missing
    event line must not wedge the tick). ``drained_after_sec`` is ``None``
    for engage/disengage actions where no drain timer applies; the field
    is always present in the line so consumers can rely on a stable
    schema.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rec = {
        'ts': now.astimezone(timezone.utc).isoformat(),
        'action': action,
        'from_tier': from_tier,
        'to_tier': to_tier,
        'trigger': trigger,
        'rolling_5h_tokens': int(rolling_5h_tokens),
        'threshold': int(threshold),
        'drained_after_sec': (None if drained_after_sec is None
                              else int(drained_after_sec)),
    }
    try:
        path = _events_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as f:
            f.write(json.dumps(rec) + '\n')
    except OSError:
        pass


def _dm_auth_blocked(held_tier, blocked_tier, logger=None):
    """DM Larry that the scheduler refused to flip into ``blocked_tier``
    because its OAuth credentials failed the pre-engage auth gate. Points
    at the Tier 2 restore runbook (auth_401 with both tiers in scope, the
    Tier 2 runbook covers either direction). Best-effort: a failed import
    or a cooldown-suppressed alert never wedges the tick.

    Gated on the ``OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE=true`` sentinel
    set only by ``systemd/ourliberty-rotate-active-tier.service`` — manual
    / agent CLI invocations probing the auth gate must not page Larry. The
    in-process auth-blocked event + held-window logic still runs; only the
    DM is suppressed.
    """
    if os.environ.get('OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE') != 'true':
        if logger is not None:
            logger.info(
                'rotation auth-blocked DM suppressed: '
                'OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE sentinel not set '
                '(non-systemd invocation)'
            )
        return
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import larry_alerts as la  # noqa: E402
    except Exception:
        return
    try:
        la.append_alert(
            source='rotate-active-tier',
            severity='warning',
            message=(
                f'Rotation auth gate blocked the flip into {blocked_tier}: '
                f'{blocked_tier} OAuth credentials are missing, expired, or '
                f'unverifiable. Held on {held_tier}; the scheduler will '
                f'retry the flip on the next window after the operator '
                f're-auths.'
            ),
            subject=f'rotation_auth_gate_blocked:{blocked_tier}',
            suggested_action=(
                'Re-auth the blocked tier: '
                'docs/runbooks/restore-larry-personal-claude-oauth-tier2.md.'
            ),
        )
    except Exception:
        if logger is not None:
            logger.warning('rotation auth-blocked DM failed (suppressed)')


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
    # Audit #22: the master kill switch must fail safe. `enabled` is consumed
    # via bool(cfg['enabled']), and bool() of any non-empty string is True — so
    # a wrongly-typed JSON value (e.g. the string "false", a common quoted-bool
    # typo) would silently ENABLE rotation, the exact opposite of intent.
    # Normalize at the boundary: rotation is enabled only when the value is the
    # JSON boolean `true`; every other type/value collapses to the safe default
    # (disabled), consistent with this loader's stated posture.
    cfg['enabled'] = cfg.get('enabled') is True
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


def _is_engaged_state(state):
    """Derive the current "engaged" bit from existing active-tier state —
    no new field needed. We're engaged whenever rotation machinery is
    visibly running: a non-tier1 tier, an in-progress drain, or a scheduled
    next-switch deadline. A clean tier1 state with no draining and no
    deadline is disengaged (parked).
    """
    if state.get('tier') != 'tier1':
        return True
    if state.get('draining'):
        return True
    if state.get('next_switch_due'):
        return True
    return False


def _decide_engaged(currently_engaged, usage, threshold, cfg):
    """Apply § 6.4 hysteresis. Returns (new_engaged, engage_thr,
    disengage_thr).

    Engage when usage >= engage_thr; disengage when usage < disengage_thr;
    hold the current state in the middle band. A zero or negative
    threshold collapses to "always disengaged" — defensive against a
    misconfigured tier1_quota block, never wedge the runner.
    """
    engage_frac = float(cfg.get('engage_at_fraction',
                                _DEFAULTS['engage_at_fraction']))
    disengage_frac = float(cfg.get('disengage_at_fraction',
                                   _DEFAULTS['disengage_at_fraction']))
    engage_thr = engage_frac * threshold if threshold > 0 else 0
    disengage_thr = disengage_frac * threshold if threshold > 0 else 0
    if threshold <= 0:
        return False, engage_thr, disengage_thr
    if usage >= engage_thr:
        return True, engage_thr, disengage_thr
    if usage < disengage_thr:
        return False, engage_thr, disengage_thr
    return currently_engaged, engage_thr, disengage_thr


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
    override_off = _override_disabled()

    # Master kill switch — force tier1 + clear drain state when rotation is
    # off, via either the permanent config default (enabled=false) or the
    # live runtime override file (~/agents/rotation.disabled). Idempotent: a
    # no-op when already tier1 + not draining. The override mirrors
    # enabled=false exactly; its only added side effect is a manual_override
    # event line (below) so a future Pulse Check can correlate the manual
    # disable against the rate-limit ledger.
    if not enabled or override_off:
        # Config-disabled (enabled=false) keeps the historical force-tier1.
        # Override-off honors the operator's pinned tier from the file
        # contents (default tier1), so a manual pin to tier2 sticks: every
        # tick re-pins it, fully bypassing load-gated rotation (spec § 6.5
        # manual-pin — "manual pin fully wins"; the load gate never overrides
        # an operator pin).
        target = _OVERRIDE_DEFAULT_TIER
        if override_off:
            target = _override_pinned_tier() or _OVERRIDE_DEFAULT_TIER
        actions = []
        if tier != target:
            active_tier.set_tier(target)
            actions.append('forced-' + target)
        if state['draining']:
            active_tier.set_draining(False)
            actions.append('cleared-draining')
        if state['next_switch_due']:
            active_tier.set_next_switch_due(None)
            actions.append('cleared-next-switch-due')
        # Emit one manual_override event only when the override file drove an
        # actual state change (re-pin / drain abandon) — matches the
        # emit-on-transition discipline of the load-gate events and avoids a
        # per-tick event line while parked. Load is not consulted on this
        # fast path, so the token/threshold fields carry 0 sentinels; the
        # distinct trigger marks the row as a manual action, not a load
        # event. action is direction-aware: a pin onto tier2 is an 'engage',
        # a pin onto tier1 a 'disengage' (the historical Off direction).
        # Config-disabled keeps its historical silent behavior.
        if override_off and actions:
            _emit_event(
                action='engage' if target == 'tier2' else 'disengage',
                from_tier=tier,
                to_tier=target,
                trigger='manual_override',
                rolling_5h_tokens=0,
                threshold=0,
                now=now,
            )
        reason = 'override' if override_off else 'disabled'
        result = {'action': 'disabled', 'reason': reason,
                  'changes': actions, 'tier': target}
        logger.info('rotation disabled tick: ' + json.dumps(result))
        return result

    # § 6.4 load gating with hysteresis. The token signal lives in
    # heal_claude_max_burn_rate; we do not reimplement the window math.
    usage = hcmbr.rolling_5h_token_volume(now=now)
    threshold = hcmbr.load_threshold()
    currently_engaged = _is_engaged_state(state)
    engaged, engage_thr, disengage_thr = _decide_engaged(
        currently_engaged, usage, threshold, cfg,
    )

    if not engaged:
        # Disengaged. Force tier1 + clear any drain state. Idempotent when
        # already parked. On the engaged → disengaged transition, emit a
        # disengage event with the threshold-cross trigger so the future
        # Pulse Check can correlate against rate-limit ground truth.
        if currently_engaged:
            _emit_event(
                action='disengage',
                from_tier=tier,
                to_tier='tier1',
                trigger='load_gate_below_disengage',
                rolling_5h_tokens=usage,
                threshold=threshold,
                now=now,
            )
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
        result = {
            'action': 'disengaged',
            'changes': actions,
            'tier': 'tier1',
            'rolling_5h_tokens': usage,
            'threshold': threshold,
            'disengage_threshold': int(disengage_thr),
            'transitioned': currently_engaged,
        }
        logger.info('rotation disengaged tick: ' + json.dumps(result))
        return result

    # Engaged. Emit an engage event on the disengaged → engaged transition
    # before the existing rotation logic runs.
    if not currently_engaged:
        _emit_event(
            action='engage',
            from_tier=tier,
            to_tier=tier,
            trigger='load_gate_above_engage',
            rolling_5h_tokens=usage,
            threshold=threshold,
            now=now,
        )

    if state['draining']:
        # Drain phase. Check whether the world has settled enough to flip,
        # OR whether we've waited too long and should defer until next tick.
        in_flight_clear = _in_flight_empty()
        no_open_seq = not _any_open_build_sequence()
        # Drain-start anchor for drained_after_sec: prefer next_switch_due
        # (the deadline at which set_draining(True) fired in normal flow);
        # fall back to since when the test/initial state pre-set draining
        # without a deadline.
        drain_start = (_parse_iso(state.get('next_switch_due'))
                       or _parse_iso(state.get('since'))
                       or now)
        # Has the drain run past its cap? Computed up-front because the cap now
        # gates the flip itself, not just the defer path below.
        max_drain = int(cfg.get('max_drain_minutes',
                                _DEFAULTS['max_drain_minutes']))
        since = _parse_iso(state.get('since')) or now
        elapsed = now - since
        max_drain_exceeded = elapsed > timedelta(minutes=max_drain)
        # Force-complete the flip when the ONLY thing still blocking it is an
        # open build sequence (no in-flight work) and we've waited past
        # max_drain. The "never force-kill in-flight work" rule protects
        # IN_FLIGHT_DIR leases only; an open sequence with no in-flight lease
        # is merely PAUSED between steps and resumes safely on the post-flip
        # tier (sequences are tier-agnostic). Blocking the flip on it forever
        # was the 2026-06-02 deadlock: drain opened, in_flight was clear, but
        # the `approvals-queue-rework` sequence stayed open — and an open
        # sequence can only advance by dispatching new top-level tasks, which
        # the drain gate blocks. A closed loop that deferred for 1228 min.
        # Below max_drain we still PREFER to wait (preserve the
        # don't-flip-mid-sequence intent); past it we force so this can NEVER
        # deadlock again.
        forced_open_seq = (in_flight_clear and not no_open_seq
                           and max_drain_exceeded)
        if in_flight_clear and (no_open_seq or max_drain_exceeded):
            # FLIP — set new tier, reset window, clear draining. The order
            # matters: set_tier() stamps `since`; set_next_switch_due() pins
            # the next deadline; set_draining(False) opens the gate. A
            # crash between writes leaves state in an intermediate-but-safe
            # shape (the gate is still closed until set_draining lands).
            next_tier = _other_tier(tier)
            # Pre-engage auth gate (Step A rotation fix): refuse to flip into
            # a tier whose OAuth credentials are expired or unverifiable.
            # The 2026-05-29 storm root cause was the scheduler flipping to
            # Tier 2 with a silently-expired token; every dispatch then
            # auth_401-stormed for the full window. Hold the current tier
            # instead — re-pin a fresh window so we don't immediately re-enter
            # drain on the next tick — and DM Larry pointing at the Tier 2
            # restore runbook.
            if not active_tier.tier_auth_ok(next_tier, now=now):
                cur_window_min = _window_for(tier, cfg)
                active_tier.set_draining(False)
                active_tier.set_next_switch_due(
                    now + timedelta(minutes=cur_window_min),
                )
                _emit_event(
                    action='auth-blocked',
                    from_tier=tier,
                    to_tier=next_tier,
                    trigger='target_auth_check_failed',
                    rolling_5h_tokens=usage,
                    threshold=threshold,
                    drained_after_sec=(now - drain_start).total_seconds(),
                    now=now,
                )
                _dm_auth_blocked(tier, next_tier, logger=logger)
                result = {
                    'action': 'auth-blocked',
                    'from_tier': tier,
                    'to_tier': next_tier,
                    'held_tier': tier,
                    'next_switch_due_minutes': cur_window_min,
                }
                logger.warning('rotation auth-blocked: ' + json.dumps(result))
                return result
            active_tier.set_tier(next_tier)
            window_min = _window_for(next_tier, cfg)
            active_tier.set_next_switch_due(now + timedelta(minutes=window_min))
            active_tier.set_draining(False)
            _emit_event(
                action='switch',
                from_tier=tier,
                to_tier=next_tier,
                trigger=('max_drain_forced_open_sequence' if forced_open_seq
                         else 'window_elapsed_drain_complete'),
                rolling_5h_tokens=usage,
                threshold=threshold,
                drained_after_sec=(now - drain_start).total_seconds(),
                now=now,
            )
            result = {
                'action': 'flipped',
                'from_tier': tier,
                'to_tier': next_tier,
                'next_switch_due_minutes': window_min,
                'forced_open_sequence': forced_open_seq,
            }
            logger.info('rotation flip: ' + json.dumps(result))
            return result

        # Drain still pending. If we're here with max_drain exceeded, the flip
        # gate above did NOT fire — which now means in-flight work is present
        # (an open sequence alone would have force-flipped). DEFER: never
        # force-kill in-flight leases.
        if max_drain_exceeded:
            # DEFER — log + leave draining=true. Never force-kill. Next tick
            # will re-evaluate; eventually the in-flight work finishes (or
            # Larry's operator action clears it) and the flip lands.
            _emit_event(
                action='drain-defer',
                from_tier=tier,
                to_tier=tier,
                trigger='max_drain_minutes_exceeded_in_flight_present',
                rolling_5h_tokens=usage,
                threshold=threshold,
                drained_after_sec=(now - drain_start).total_seconds(),
                now=now,
            )
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
