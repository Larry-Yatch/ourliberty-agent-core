#!/usr/bin/env python3
"""Tests for scripts/rotate_active_tier.py — the proactive account-rotation
scheduler (spec § 6.3).

Covers the acceptance criteria verbatim from the spec + the dispatch:

  - enabled=false → forced tier1, draining cleared (the master kill switch).
  - Window elapsed + not draining → drain gate opens.
  - Draining + in-flight present → no flip (drain-waiting).
  - Draining + open build sequence → no flip.
  - Draining + clear → flip + windows reset for new tier.
  - Drain timeout → defer (NEVER force-kill).

Each test wires a tmp ``OURLIBERTY_AGENTS_ROOT`` so the state file + the
in-flight directory + the build-sequences directory live in tmp.

Run::

    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_rotate_active_tier
"""
from __future__ import annotations

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
}


class RotateTickBaseTest(unittest.TestCase):
    """Shared fixture: redirect ~/agents and pre-build a models file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        self.models_file = self.root / 'config' / 'agent-models.json'
        _write_models_file(self.models_file, dict(_BASE_ROTATION))

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev
        self.tmp.cleanup()

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


if __name__ == '__main__':
    unittest.main()
