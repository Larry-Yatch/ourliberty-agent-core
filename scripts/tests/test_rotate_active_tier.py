#!/usr/bin/env python3
"""Tests for scripts/rotate_active_tier.py — the proactive account-rotation
scheduler (spec §§ 6.3 + 6.4).

Covers the acceptance criteria verbatim from the spec + the dispatch:

  - enabled=false → forced tier1, draining cleared (the master kill switch).
  - Window elapsed + not draining → drain gate opens.
  - Draining + in-flight present → no flip (drain-waiting).
  - Draining + open build sequence → no flip.
  - Draining + clear → flip + windows reset for new tier.
  - Drain timeout → defer (NEVER force-kill).
  - § 6.4 load gating: usage >= engage_thr → engaged; usage between the
    two fractions → HOLD current state (hysteresis, no flap); usage <
    disengage_thr → disengaged + forced tier1.
  - § 6.4 rotation-events.jsonl: every state-changing action emits one
    well-formed JSON line with the locked schema.

Each test wires a tmp ``OURLIBERTY_AGENTS_ROOT`` so the state file + the
in-flight directory + the build-sequences directory live in tmp. The
shared fixture also pre-seeds ``costs.jsonl`` with a high-volume entry so
the load gate engages by default — tests that exercise the gate itself
override the seed via ``_set_load_tokens``.

Run::

    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_rotate_active_tier
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import active_tier  # noqa: E402
import heal_claude_max_burn_rate  # noqa: E402
import rotate_active_tier  # noqa: E402


def _write_models_file(path: Path, rotation: dict) -> None:
    """Write a minimal config/agent-models.json carrying just the rotation
    block we want to exercise. The script tolerates everything else missing."""
    payload = {'rotation': rotation}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


_BASE_ROTATION = {
    'enabled': True,
    'tier1_window_minutes': 120,
    'tier2_window_minutes': 60,
    'max_drain_minutes': 45,
    'engage_at_fraction': 0.70,
    'disengage_at_fraction': 0.50,
}

# Production tier1_quota.max_5h_token_threshold is 10M; engage at 0.70 = 7M.
# Pre-seed costs.jsonl with 8M tokens so the load gate engages by default
# in tests that don't explicitly exercise the gate. Tests that DO exercise
# the gate override via _set_load_tokens.
_DEFAULT_LOAD_TOKENS = 8_000_000


class _AuthOkPatcher:
    """Swap `active_tier.tier_auth_ok` for a dict-backed stub during a test.

    The real implementation reads ``.credentials.json`` from the on-disk
    tier home dirs; tests must not depend on the dev box's actual
    credential state. The stub keeps a single shared dict so the test can
    flip a tier's auth result mid-fixture (e.g., the auth-gate path that
    fires on the SECOND tick after the operator re-auths)."""

    def __init__(self, mapping):
        self._mapping = mapping
        self._prev = None

    def start(self):
        self._prev = active_tier.tier_auth_ok

        def _stub(tier, now=None):
            return bool(self._mapping.get(tier, True))

        active_tier.tier_auth_ok = _stub

    def stop(self):
        if self._prev is not None:
            active_tier.tier_auth_ok = self._prev


class RotateTickBaseTest(unittest.TestCase):
    """Shared fixture: redirect ~/agents, pre-build a models file, and seed
    costs.jsonl so the § 6.4 load gate engages by default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        # Reload hcmbr so its module-level COSTS_FILE / RATE_LIMIT_LEDGER_FILE
        # pick up the new OURLIBERTY_AGENTS_ROOT. rotate_active_tier's
        # `import heal_claude_max_burn_rate as hcmbr` reference stays bound
        # to the same module object — in-place reload propagates.
        importlib.reload(heal_claude_max_burn_rate)
        self.models_file = self.root / 'config' / 'agent-models.json'
        _write_models_file(self.models_file, dict(_BASE_ROTATION))
        self._set_load_tokens(_DEFAULT_LOAD_TOKENS)
        # Step A auth gate: pin the default to "auth-ok" so the legacy flip
        # tests don't accidentally exercise the gate's hold-and-DM branch
        # against the dev box's real credential files. Tests that exercise
        # the gate itself override via `self._set_auth_ok(...)`.
        self._auth_ok_map = {'tier1': True, 'tier2': True}
        self._auth_patcher = _AuthOkPatcher(self._auth_ok_map)
        self._auth_patcher.start()

    def tearDown(self):
        self._auth_patcher.stop()
        if self._prev is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev
        # Restore hcmbr to production paths so subsequent tests in other
        # modules don't see our tmp-rooted COSTS_FILE.
        importlib.reload(heal_claude_max_burn_rate)
        self.tmp.cleanup()

    def _set_auth_ok(self, *, tier1=True, tier2=True):
        """Override the auth-gate result for the legacy `tier_auth_ok` shim.
        Tests that exercise the auth-blocked branch flip one side False."""
        self._auth_ok_map['tier1'] = tier1
        self._auth_ok_map['tier2'] = tier2

    def _set_load_tokens(self, total_tokens):
        """Write a single costs.jsonl record with `total_tokens` quota-
        consuming tokens (input+output+cache_creation, per the healer's
        token-volume signal) stamped recently enough to land inside the
        rolling 5h window. Overwrites any prior seed."""
        d = self.root / 'blackboard'
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'agent': 'test-seed',
            'input_tokens': int(total_tokens),
            'output_tokens': 0,
            'cache_creation': 0,
            'cache_read': 0,
        }
        (d / 'costs.jsonl').write_text(json.dumps(rec) + '\n')

    def _set_state(self, **fields):
        state = active_tier.read()
        state.update(fields)
        (self.root / 'blackboard').mkdir(parents=True, exist_ok=True)
        (self.root / 'blackboard' / 'active-tier.json').write_text(
            json.dumps(state),
        )

    def _add_in_flight(self, task_id):
        d = self.root / 'state' / 'in-flight'
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{task_id}.json').write_text(
            json.dumps({'agent': 'forge', 'pid': 1234}),
        )

    def _add_sequence(self, seq_id, status):
        d = self.root / 'blackboard' / 'build-sequences'
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{seq_id}.json').write_text(
            json.dumps({'sequence_id': seq_id, 'status': status}),
        )

    def _set_rotation(self, **fields):
        rot = dict(_BASE_ROTATION)
        rot.update(fields)
        _write_models_file(self.models_file, rot)


class DisabledKillSwitchTest(RotateTickBaseTest):
    """Spec acceptance: enabled=false ships as default so there is no
    runtime behavior change on merge."""

    def test_disabled_forces_tier1_when_state_is_tier2(self):
        self._set_state(tier='tier2', since='2026-05-28T10:00:00+00:00')
        self._set_rotation(enabled=False)
        result = rotate_active_tier.tick(models_file=self.models_file)
        self.assertEqual(result['action'], 'disabled')
        self.assertIn('forced-tier1', result['changes'])
        self.assertEqual(active_tier.read()['tier'], 'tier1')

    def test_disabled_clears_partial_drain_state(self):
        self._set_state(tier='tier1', draining=True,
                        next_switch_due='2026-05-28T14:00:00+00:00')
        self._set_rotation(enabled=False)
        result = rotate_active_tier.tick(models_file=self.models_file)
        state = active_tier.read()
        self.assertFalse(state['draining'])
        self.assertIsNone(state['next_switch_due'])
        self.assertEqual(result['action'], 'disabled')

    def test_disabled_and_already_tier1_is_idempotent_noop(self):
        # Default state: tier1, not draining, no deadline. Disable just exits
        # with action=disabled, no changes recorded.
        self._set_rotation(enabled=False)
        result = rotate_active_tier.tick(models_file=self.models_file)
        self.assertEqual(result['action'], 'disabled')
        self.assertEqual(result['changes'], [])

    def test_missing_rotation_block_treated_as_disabled(self):
        # An agent-models.json without a rotation block must collapse to
        # disabled — defense-in-depth so a config typo can never accidentally
        # activate the rotation cadence.
        self.models_file.write_text(json.dumps({'other': 'block'}))
        result = rotate_active_tier.tick(models_file=self.models_file)
        self.assertEqual(result['action'], 'disabled')


class RuntimeOverrideTest(RotateTickBaseTest):
    """dashboard-rotation-switch-001: the ~/agents/rotation.disabled runtime
    override forces the scheduler off exactly like config enabled=false, even
    while the config default stays enabled=true. Distinct from config-disabled
    in one way only: an override-driven force-tier1 emits a manual_override
    rotation-events line. Config-disabled stays silent (backward compat)."""

    def _touch_override(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'rotation.disabled').touch()

    def _events_lines(self):
        path = self.root / 'blackboard' / 'rotation-events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    def test_override_forces_tier1_even_when_config_enabled(self):
        # Config default ON, but the override file is present → off.
        self._set_state(tier='tier2', since='2026-05-28T10:00:00+00:00')
        self._set_rotation(enabled=True)
        self._touch_override()
        result = rotate_active_tier.tick(models_file=self.models_file)
        self.assertEqual(result['action'], 'disabled')
        self.assertEqual(result['reason'], 'override')
        self.assertIn('forced-tier1', result['changes'])
        self.assertEqual(active_tier.read()['tier'], 'tier1')

    def test_override_abandons_partial_drain(self):
        self._set_state(tier='tier1', draining=True,
                        next_switch_due='2026-05-28T14:00:00+00:00')
        self._set_rotation(enabled=True)
        self._touch_override()
        result = rotate_active_tier.tick(models_file=self.models_file)
        state = active_tier.read()
        self.assertFalse(state['draining'])
        self.assertIsNone(state['next_switch_due'])
        self.assertEqual(result['reason'], 'override')

    def test_override_emits_manual_override_event_with_schema(self):
        self._set_state(tier='tier2', since='2026-05-28T10:00:00+00:00')
        self._set_rotation(enabled=True)
        self._touch_override()
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        rotate_active_tier.tick(now=now, models_file=self.models_file)
        lines = self._events_lines()
        self.assertEqual(len(lines), 1)
        ev = lines[0]
        self.assertEqual(ev['trigger'], 'manual_override')
        self.assertEqual(ev['action'], 'disengage')
        self.assertEqual(ev['from_tier'], 'tier2')
        self.assertEqual(ev['to_tier'], 'tier1')
        # Locked schema: every field present on every line.
        for key in ('ts', 'action', 'from_tier', 'to_tier', 'trigger',
                    'rolling_5h_tokens', 'threshold', 'drained_after_sec'):
            self.assertIn(key, ev)

    def test_override_idempotent_when_already_parked_no_event(self):
        # Clean tier1 + no drain + override present → no change, no event line
        # (matches the emit-on-transition discipline of the load-gate events).
        self._set_rotation(enabled=True)
        self._touch_override()
        result = rotate_active_tier.tick(models_file=self.models_file)
        self.assertEqual(result['action'], 'disabled')
        self.assertEqual(result['reason'], 'override')
        self.assertEqual(result['changes'], [])
        self.assertEqual(self._events_lines(), [])

    def test_config_disabled_stays_silent_no_event(self):
        # The override adds an event; plain config-disabled must NOT — its
        # historical silent behavior is preserved.
        self._set_state(tier='tier2', since='2026-05-28T10:00:00+00:00')
        self._set_rotation(enabled=False)
        result = rotate_active_tier.tick(models_file=self.models_file)
        self.assertEqual(result['reason'], 'disabled')
        self.assertIn('forced-tier1', result['changes'])
        self.assertEqual(self._events_lines(), [])

    def test_no_override_with_config_enabled_runs_normally(self):
        # Sanity: absent the override file, enabled=true proceeds past the
        # kill-switch block into normal scheduling (not 'disabled').
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        self._set_rotation(enabled=True)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertNotEqual(result['action'], 'disabled')


class WindowElapseTest(RotateTickBaseTest):
    """Spec acceptance: window elapse → drain gate opens."""

    def test_first_tick_after_enable_initializes_window(self):
        # State has no next_switch_due — first tick pins one and exits.
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'initialized-window')
        self.assertEqual(result['next_switch_due_minutes'], 120)
        state = active_tier.read()
        self.assertEqual(state['next_switch_due'],
                         '2026-05-28T14:00:00+00:00')

    def test_window_not_yet_elapsed_is_idle(self):
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        self._set_state(next_switch_due='2026-05-28T14:00:00+00:00')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'idle')
        self.assertFalse(active_tier.read()['draining'])

    def test_window_elapsed_sets_draining(self):
        now = datetime(2026, 5, 28, 14, 5, 0, tzinfo=timezone.utc)
        self._set_state(next_switch_due='2026-05-28T14:00:00+00:00')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'drain-started')
        self.assertTrue(active_tier.read()['draining'])


class FlipTest(RotateTickBaseTest):
    """Spec acceptance: in-flight clears + no open sequence → flip + windows
    reset for new tier."""

    def test_clean_drain_flips_and_resets_window(self):
        now = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
        # Currently tier1, draining, no in-flight, no sequences
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'flipped')
        self.assertEqual(result['to_tier'], 'tier2')
        # tier2 window is 60 min by config
        self.assertEqual(result['next_switch_due_minutes'], 60)
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier2')
        self.assertFalse(state['draining'])
        # since stamped to "now" (close enough — the set_tier helper uses
        # wallclock now, not our injected now, so we just verify it's not the
        # pre-drain timestamp).
        self.assertNotEqual(state['since'], '2026-05-28T14:00:00+00:00')

    def test_tier2_to_tier1_flip_uses_tier1_window(self):
        now = datetime(2026, 5, 28, 15, 30, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier2', draining=True,
                        since='2026-05-28T15:00:00+00:00')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['to_tier'], 'tier1')
        self.assertEqual(result['next_switch_due_minutes'], 120)


class DrainBlockingTest(RotateTickBaseTest):
    """Spec acceptance: in-flight present → no flip; open build sequence → no
    flip. Both are drain-waiting (still draining, no state mutation yet)."""

    def test_in_flight_present_blocks_flip(self):
        now = datetime(2026, 5, 28, 14, 20, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_in_flight('forge-task-001')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'drain-waiting')
        # No flip; still on tier1
        self.assertEqual(active_tier.read()['tier'], 'tier1')

    def test_open_build_sequence_blocks_flip(self):
        now = datetime(2026, 5, 28, 14, 20, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_sequence('build-seq-001', status='active')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'drain-waiting')
        self.assertEqual(active_tier.read()['tier'], 'tier1')

    def test_completed_sequence_does_not_block_flip(self):
        # Closed/complete sequences must not block; the flip should proceed.
        now = datetime(2026, 5, 28, 14, 20, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_sequence('build-seq-old', status='complete')
        self._add_sequence('build-seq-archived', status='archived')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'flipped')

    def test_in_flight_clearing_after_block_unblocks(self):
        # Two ticks: first blocks; second unblocks after the in-flight entry
        # is removed.
        now = datetime(2026, 5, 28, 14, 20, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_in_flight('forge-task-001')
        first = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(first['action'], 'drain-waiting')
        # Clear the in-flight entry; next tick flips
        (self.root / 'state' / 'in-flight' / 'forge-task-001.json').unlink()
        second = rotate_active_tier.tick(now=now + timedelta(minutes=2),
                                         models_file=self.models_file)
        self.assertEqual(second['action'], 'flipped')


class DrainTimeoutTest(RotateTickBaseTest):
    """Spec acceptance: max_drain_minutes exceeded → defer; NEVER force-kill."""

    def test_drain_timeout_defers_without_flipping(self):
        # since=14:00, max_drain=45, now=14:46 → exceeded.
        now = datetime(2026, 5, 28, 14, 46, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_in_flight('stuck-task-001')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'drain-deferred')
        # State unchanged: still tier1, still draining, in-flight not removed
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertTrue(state['draining'])
        self.assertTrue((self.root / 'state' / 'in-flight'
                        / 'stuck-task-001.json').exists())

    def test_drain_timeout_keeps_deferring_until_clear(self):
        # Subsequent ticks keep deferring; once the in-flight clears, even
        # past the timeout, the flip proceeds normally.
        now = datetime(2026, 5, 28, 14, 46, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_in_flight('stuck-task-001')
        first = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(first['action'], 'drain-deferred')
        # Even further past the timeout
        later = now + timedelta(minutes=30)
        second = rotate_active_tier.tick(now=later,
                                         models_file=self.models_file)
        self.assertEqual(second['action'], 'drain-deferred')
        # In-flight finally clears
        (self.root / 'state' / 'in-flight' / 'stuck-task-001.json').unlink()
        third = rotate_active_tier.tick(now=later + timedelta(minutes=2),
                                        models_file=self.models_file)
        self.assertEqual(third['action'], 'flipped')


class DrainOpenSequenceDeadlockTest(RotateTickBaseTest):
    """Regression guard for the 2026-06-02 deadlock: an open build sequence
    with NO in-flight work must not block the flip forever. Below max_drain we
    still prefer to wait; once max_drain is exceeded we force the flip so the
    drain can never close-loop on a paused sequence again."""

    def _events_lines(self):
        path = self.root / 'blackboard' / 'rotation-events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    def test_open_seq_within_max_drain_waits(self):
        # in_flight_clear + open sequence + elapsed <= max_drain → prefer-wait.
        # since=14:00, max_drain=45, now=14:20 → 20 min elapsed (<= 45).
        now = datetime(2026, 5, 28, 14, 20, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_sequence('approvals-queue-rework', status='active')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'drain-waiting')
        # No flip: still tier1, still draining.
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertTrue(state['draining'])

    def test_open_seq_past_max_drain_force_flips(self):
        # THE deadlock case (previously deferred forever):
        # in_flight_clear + open sequence + elapsed > max_drain → FORCE FLIP.
        # since=14:00, max_drain=45, now=14:46 → 46 min elapsed (> 45).
        now = datetime(2026, 5, 28, 14, 46, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_sequence('approvals-queue-rework', status='active')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'flipped')
        self.assertTrue(result['forced_open_sequence'])
        # Flip landed: now tier2, drain cleared.
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier2')
        self.assertFalse(state['draining'])
        # The switch event carries the distinguishing forced trigger.
        events = [e for e in self._events_lines()
                  if e['action'] == 'switch']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['trigger'],
                         'max_drain_forced_open_sequence')

    def test_in_flight_past_max_drain_still_defers(self):
        # Unchanged guarantee: a live in-flight lease past max_drain must NEVER
        # force-flip, even when there is also an open sequence. The force path
        # only applies when in-flight is clear.
        now = datetime(2026, 5, 28, 14, 46, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_in_flight('stuck-task-001')
        self._add_sequence('approvals-queue-rework', status='active')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'drain-deferred')
        # State unchanged: still tier1, still draining, lease not removed.
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertTrue(state['draining'])
        self.assertTrue((self.root / 'state' / 'in-flight'
                        / 'stuck-task-001.json').exists())

    def test_forced_flip_still_gated_by_target_auth(self):
        # The pre-engage auth guard MUST still gate the forced flip. A forced
        # flip into a tier whose auth is bad holds the current tier + re-pins a
        # fresh window — it does NOT flip (prevents the 2026-05-29 storm).
        now = datetime(2026, 5, 28, 14, 46, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00')
        self._add_sequence('approvals-queue-rework', status='active')
        self._set_auth_ok(tier1=True, tier2=False)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'auth-blocked')
        self.assertEqual(result['held_tier'], 'tier1')
        # Held on tier1, drain cleared, a fresh tier1 window pinned.
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertFalse(state['draining'])
        self.assertEqual(result['next_switch_due_minutes'], 120)


class LoadGatingTest(RotateTickBaseTest):
    """Spec § 6.4 acceptance: load-gated engage/disengage with hysteresis.

    Production tier1_quota.max_5h_token_threshold = 10M;
    engage_at_fraction=0.70 → engage_thr=7M; disengage_at_fraction=0.50 →
    disengage_thr=5M. So:

      - usage >= 7M  → engaged
      - 5M <= usage < 7M → HOLD current engaged state
      - usage <  5M  → disengaged + forced tier1

    The hysteresis band is the whole point: a flapping signal near the
    boundary must NEVER produce engage/disengage thrash.
    """

    def _events_lines(self):
        path = self.root / 'blackboard' / 'rotation-events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    def test_usage_at_engage_threshold_engages_when_parked(self):
        # Initial state: tier1, no drain, no deadline → currently_engaged
        # is False. Usage 8M >= 7M engage_thr → engages, initializes the
        # tier1 window, emits an engage event.
        self._set_load_tokens(8_000_000)
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'initialized-window')
        events = self._events_lines()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['action'], 'engage')
        self.assertEqual(events[0]['trigger'], 'load_gate_above_engage')
        self.assertEqual(events[0]['rolling_5h_tokens'], 8_000_000)
        self.assertEqual(events[0]['threshold'], 10_000_000)

    def test_usage_between_thresholds_holds_engaged_state(self):
        # State is engaged (deadline pinned), usage drops into the hold
        # band (5M-7M). Expect: state held (still engaged, idle), NO
        # disengage event emitted, NO state mutation beyond the normal
        # idle/init path.
        self._set_load_tokens(6_000_000)  # between 5M and 7M
        self._set_state(tier='tier1',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'idle')
        state = active_tier.read()
        # Still engaged: deadline preserved, not forced to tier1.
        self.assertEqual(state['tier'], 'tier1')
        self.assertEqual(state['next_switch_due'],
                         '2026-05-28T14:00:00+00:00')
        # No engage/disengage event — the hold band must be silent.
        events = self._events_lines()
        action_set = {e['action'] for e in events}
        self.assertNotIn('engage', action_set)
        self.assertNotIn('disengage', action_set)

    def test_usage_between_thresholds_holds_disengaged_state(self):
        # State is disengaged (parked: tier1, no draining, no deadline),
        # usage rises into the hold band (5M-7M). Expect: state held
        # (still disengaged), NO engage event emitted, no window init.
        self._set_load_tokens(6_000_000)
        # Initial state is already the parked default.
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'disengaged')
        self.assertFalse(result['transitioned'])
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertFalse(state['draining'])
        self.assertIsNone(state['next_switch_due'])
        # Hold-disengaged must NOT emit a disengage event — it would
        # double-count on every tick. Engage is also silent. Total events: 0.
        self.assertEqual(self._events_lines(), [])

    def test_usage_below_disengage_disengages_and_forces_tier1(self):
        # State is tier2, engaged. Usage drops below 5M → disengage path:
        # force tier1, clear draining, clear deadline, emit disengage
        # event with from_tier=tier2.
        self._set_load_tokens(4_000_000)
        self._set_state(tier='tier2', draining=True,
                        since='2026-05-28T11:00:00+00:00',
                        next_switch_due='2026-05-28T12:00:00+00:00')
        now = datetime(2026, 5, 28, 12, 30, 0, tzinfo=timezone.utc)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'disengaged')
        self.assertTrue(result['transitioned'])
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertFalse(state['draining'])
        self.assertIsNone(state['next_switch_due'])
        events = self._events_lines()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['action'], 'disengage')
        self.assertEqual(events[0]['from_tier'], 'tier2')
        self.assertEqual(events[0]['to_tier'], 'tier1')
        self.assertEqual(events[0]['trigger'], 'load_gate_below_disengage')
        self.assertEqual(events[0]['rolling_5h_tokens'], 4_000_000)

    def test_disengaged_when_already_parked_is_idempotent(self):
        # Already parked + usage low → still parked, no event emitted,
        # `transitioned` is False. Critical: a sustained low-load period
        # must NOT spam disengage events on every 2-min tick.
        self._set_load_tokens(1_000_000)
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'disengaged')
        self.assertFalse(result['transitioned'])
        self.assertEqual(self._events_lines(), [])

    def test_load_gating_disengage_does_not_fire_when_kill_switch_off(self):
        # enabled=false short-circuits before load gating. Even if usage
        # is high, the master kill switch wins and no engage/disengage
        # event is emitted (the kill switch path is its own concern,
        # surfaced as action=disabled).
        self._set_load_tokens(8_000_000)
        self._set_rotation(enabled=False)
        self._set_state(tier='tier2', draining=True)
        result = rotate_active_tier.tick(models_file=self.models_file)
        self.assertEqual(result['action'], 'disabled')
        self.assertEqual(self._events_lines(), [])


class RotationEventsTest(RotateTickBaseTest):
    """Spec § 6.4 acceptance: rotation-events.jsonl carries one
    well-formed JSON line per state-changing action with the locked
    schema {ts, action, from_tier, to_tier, trigger, rolling_5h_tokens,
    threshold, drained_after_sec}."""

    _SCHEMA_KEYS = {
        'ts', 'action', 'from_tier', 'to_tier', 'trigger',
        'rolling_5h_tokens', 'threshold', 'drained_after_sec',
    }

    def _events_lines(self):
        path = self.root / 'blackboard' / 'rotation-events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    def _assert_schema(self, rec):
        self.assertEqual(set(rec.keys()), self._SCHEMA_KEYS,
                         f'rotation event missing fields: {rec}')
        # ts parseable as ISO 8601 UTC
        ts = datetime.fromisoformat(rec['ts'])
        self.assertIsNotNone(ts.tzinfo)
        self.assertIsInstance(rec['rolling_5h_tokens'], int)
        self.assertIsInstance(rec['threshold'], int)
        if rec['drained_after_sec'] is not None:
            self.assertIsInstance(rec['drained_after_sec'], int)

    def test_switch_event_emitted_on_flip(self):
        # Set up a state that flips this tick: draining=True, no
        # in-flight, no open sequence. Expect: action=switch, from→to,
        # drained_after_sec reflects time since drain started.
        self._set_load_tokens(8_000_000)
        now = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'flipped')
        events = self._events_lines()
        switch_events = [e for e in events if e['action'] == 'switch']
        self.assertEqual(len(switch_events), 1)
        sw = switch_events[0]
        self._assert_schema(sw)
        self.assertEqual(sw['from_tier'], 'tier1')
        self.assertEqual(sw['to_tier'], 'tier2')
        self.assertEqual(sw['trigger'], 'window_elapsed_drain_complete')
        self.assertEqual(sw['rolling_5h_tokens'], 8_000_000)
        self.assertEqual(sw['threshold'], 10_000_000)
        # drain started at next_switch_due=14:00; now=14:30 → 1800 sec.
        self.assertEqual(sw['drained_after_sec'], 1800)

    def test_drain_defer_event_emitted_on_timeout(self):
        # Drain stuck past max_drain WITH in-flight work present → drain-defer
        # event with the in-flight-blocked trigger (an open sequence alone
        # would force-flip; only a live lease defers now).
        self._set_load_tokens(8_000_000)
        now = datetime(2026, 5, 28, 14, 46, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        self._add_in_flight('stuck-task-001')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'drain-deferred')
        events = [e for e in self._events_lines()
                  if e['action'] == 'drain-defer']
        self.assertEqual(len(events), 1)
        ev = events[0]
        self._assert_schema(ev)
        self.assertEqual(ev['from_tier'], 'tier1')
        self.assertEqual(ev['to_tier'], 'tier1')
        self.assertEqual(ev['trigger'],
                         'max_drain_minutes_exceeded_in_flight_present')
        self.assertEqual(ev['drained_after_sec'], 46 * 60)

    def test_no_events_emitted_on_silent_paths(self):
        # Idle / drain-waiting / hold-band ticks must NOT emit events.
        # We exercise idle here: engaged, deadline in future, no flip.
        self._set_load_tokens(8_000_000)
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'idle')
        self.assertEqual(self._events_lines(), [])

    def test_engage_event_schema_well_formed(self):
        # Initial parked state + high load → engage event lands with the
        # complete schema. Covers the first-engage path's event shape.
        self._set_load_tokens(8_500_000)
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        rotate_active_tier.tick(now=now, models_file=self.models_file)
        events = self._events_lines()
        engage_events = [e for e in events if e['action'] == 'engage']
        self.assertEqual(len(engage_events), 1)
        ev = engage_events[0]
        self._assert_schema(ev)
        self.assertEqual(ev['from_tier'], 'tier1')
        self.assertEqual(ev['to_tier'], 'tier1')
        self.assertEqual(ev['trigger'], 'load_gate_above_engage')
        # No drain on an engage event.
        self.assertIsNone(ev['drained_after_sec'])


class PreEngageAuthGateTest(RotateTickBaseTest):
    """Step A rotation fix: refuse to flip into a tier whose OAuth
    credentials are missing, expired, or unverifiable.

    The 2026-05-29 storm root cause was the scheduler flipping into Tier 2
    with a silently-expired token; every dispatch in the active-tier window
    then auth_401-stormed for the full window. The gate is the structural
    fix: it never trusts a target it can't verify.
    """

    def _events_lines(self):
        path = self.root / 'blackboard' / 'rotation-events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    def test_flip_blocked_when_target_tier_auth_fails(self):
        # Drain-complete state heading into tier2, but tier2 fails the auth
        # gate → no tier flip, draining cleared, fresh tier1 window pinned.
        now = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        self._set_auth_ok(tier1=True, tier2=False)
        result = rotate_active_tier.tick(now=now, models_file=self.models_file)
        self.assertEqual(result['action'], 'auth-blocked')
        self.assertEqual(result['from_tier'], 'tier1')
        self.assertEqual(result['to_tier'], 'tier2')
        self.assertEqual(result['held_tier'], 'tier1')
        # Held tier1 window (120m by default), not the tier2 window.
        self.assertEqual(result['next_switch_due_minutes'], 120)
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertFalse(state['draining'])
        # Fresh deadline pinned for the current tier.
        self.assertEqual(state['next_switch_due'],
                         '2026-05-28T16:30:00+00:00')

    def test_flip_blocked_emits_auth_blocked_event(self):
        # The rotation-events.jsonl gains one auth-blocked record with the
        # locked schema. Future Pulse Check needs this to correlate
        # auth-gate blocks against ground-truth auth-401 events.
        now = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        self._set_auth_ok(tier1=True, tier2=False)
        rotate_active_tier.tick(now=now, models_file=self.models_file)
        events = [e for e in self._events_lines()
                  if e['action'] == 'auth-blocked']
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev['from_tier'], 'tier1')
        self.assertEqual(ev['to_tier'], 'tier2')
        self.assertEqual(ev['trigger'], 'target_auth_check_failed')
        self.assertEqual(ev['drained_after_sec'], 1800)

    def test_flip_proceeds_after_operator_reauth(self):
        # Two ticks. First: tier2 auth fails → blocked + held on tier1 with
        # a fresh window. Second: operator re-auths (auth_ok flips True),
        # the held window elapses, drain re-opens, and the flip lands.
        first_now = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
        self._set_state(tier='tier1', draining=True,
                        since='2026-05-28T14:00:00+00:00',
                        next_switch_due='2026-05-28T14:00:00+00:00')
        self._set_auth_ok(tier1=True, tier2=False)
        first = rotate_active_tier.tick(now=first_now,
                                        models_file=self.models_file)
        self.assertEqual(first['action'], 'auth-blocked')
        # Operator re-auths Tier 2 + the held tier1 window elapses.
        self._set_auth_ok(tier1=True, tier2=True)
        # Trigger drain again: window elapses → set draining=True.
        elapsed_now = first_now + timedelta(minutes=121)
        drain_again = rotate_active_tier.tick(now=elapsed_now,
                                              models_file=self.models_file)
        self.assertEqual(drain_again['action'], 'drain-started')
        # Final tick: now drain-complete, auth_ok=True → flip lands.
        third_now = elapsed_now + timedelta(minutes=1)
        third = rotate_active_tier.tick(now=third_now,
                                        models_file=self.models_file)
        self.assertEqual(third['action'], 'flipped')
        self.assertEqual(third['to_tier'], 'tier2')


class DmAuthBlockedServiceGateTest(unittest.TestCase):
    """The rotation_auth_gate_blocked DM must only fire when invoked from
    the systemd unit (which sets OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE=true).

    Manual / agent CLI runs of `rotate_active_tier.py --once` probe the
    auth gate as a side effect — without this gate they DM Larry on every
    such probe, drowning out the genuine "operator must re-auth" signal.
    The in-process auth-blocked event + held-window logic is unaffected;
    only the la.append_alert call is suppressed.
    """

    def setUp(self):
        self._prev_env = os.environ.pop(
            'OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE', None,
        )
        self._calls = []
        sys.path.insert(0, str(_REPO_SCRIPTS))
        import larry_alerts as la
        self._la = la
        self._prev_append = la.append_alert
        la.append_alert = lambda **kw: self._calls.append(kw)

    def tearDown(self):
        self._la.append_alert = self._prev_append
        if self._prev_env is None:
            os.environ.pop('OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE', None)
        else:
            os.environ['OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE'] = (
                self._prev_env
            )

    def test_no_sentinel_suppresses_dm(self):
        rotate_active_tier._dm_auth_blocked('tier1', 'tier2')
        self.assertEqual(self._calls, [])

    def test_sentinel_set_fires_dm(self):
        os.environ['OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE'] = 'true'
        rotate_active_tier._dm_auth_blocked('tier1', 'tier2')
        self.assertEqual(len(self._calls), 1)
        kw = self._calls[0]
        self.assertEqual(kw['subject'], 'rotation_auth_gate_blocked:tier2')
        self.assertEqual(kw['severity'], 'warning')
        self.assertEqual(kw['source'], 'rotate-active-tier')

    def test_sentinel_non_true_value_suppresses_dm(self):
        # Only the exact string "true" enables the DM. A leftover "1" or
        # "yes" from a misconfigured EnvironmentFile must not accidentally
        # re-open the gate — the systemd unit is the only blessed source.
        os.environ['OURLIBERTY_ROTATE_ACTIVE_TIER_SERVICE'] = '1'
        rotate_active_tier._dm_auth_blocked('tier1', 'tier2')
        self.assertEqual(self._calls, [])


class ConfigSchemaTest(unittest.TestCase):
    """The stale dollar keys engage_5h_spend_usd / disengage_5h_spend_usd
    must be gone from the shipped rotation block — PR #184 retired
    rolling_5h_spend() and the gate now reads token volume + fractions."""

    def test_dollar_keys_absent_from_shipped_config(self):
        config_path = (Path(__file__).resolve().parent.parent.parent
                       / 'config' / 'agent-models.json')
        data = json.loads(config_path.read_text())
        rotation = data.get('rotation') or {}
        self.assertNotIn('engage_5h_spend_usd', rotation)
        self.assertNotIn('disengage_5h_spend_usd', rotation)
        self.assertIn('engage_at_fraction', rotation)
        self.assertIn('disengage_at_fraction', rotation)
        # Fractions must satisfy disengage < engage (hysteresis ordering).
        self.assertLess(rotation['disengage_at_fraction'],
                        rotation['engage_at_fraction'])


if __name__ == '__main__':
    unittest.main()
