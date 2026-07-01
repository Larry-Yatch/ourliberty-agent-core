#!/usr/bin/env python3
"""Tests for the per-task tier-dispatch inbox gate (spec § 10-G, § 9) wired
into ``inbox_watcher._rotation_gate_block_reason``.

The gate blocks a NEW top-level dispatch iff the tier pool has NOTHING to
dispatch on (``active_tier.select_dispatch_tier(None) is None``) and lets
continuations (``--resume`` / ``phase=build|revision``) through so in-flight
work finishes on its bound account. A PERSISTENT all-held state escalates once
via a deduped ``larry_alert`` (§ 9).

These exercise the gate at the helper-function level (its single decision
point) rather than the full agent_loop.

Run::

    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_inbox_watcher_rotation_gate
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import active_tier  # noqa: E402
import inbox_watcher  # noqa: E402
import larry_alerts  # noqa: E402


class RotationGateBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._env = {}
        for k, v in (
            ('OURLIBERTY_AGENTS_ROOT', str(self.root)),
            ('OURLIBERTY_CREDENTIALS_ENV_FILE', str(self.root / 'no.env')),
            # tier1/tier3 usable via setup-tokens; tier2 unset.
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER1', 'sk-ant-oat01-t1'),
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER3', 'sk-ant-oat01-t3'),
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None),
        ):
            self._env[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        (self.root / 'blackboard').mkdir(parents=True, exist_ok=True)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, prev in self._env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        self.tmp.cleanup()

    def _bench(self, tier):
        active_tier.set_cooldown(tier, raw_excerpt='resets 3pm')


class PoolGateBlockTest(RotationGateBaseTest):
    """§ 10-G: block a NEW top-level task iff no tier is available; pass
    continuations always."""

    def test_healthy_pool_is_open_gate(self):
        env = {'task_id': 'fresh-001', 'source': 'beacon', 'phase': 'preflight'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(env))

    def test_one_primary_benched_still_open(self):
        # tier1 benched but tier3 usable -> pool still serves -> open gate.
        self._bench('tier1')
        env = {'task_id': 'fresh-001', 'source': 'beacon', 'phase': 'preflight'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(env))

    def test_all_tiers_unavailable_blocks_fresh(self):
        # Both primaries benched; tier2 has no token -> nothing available.
        self._bench('tier1')
        self._bench('tier3')
        env = {'task_id': 'fresh-001', 'source': 'beacon', 'phase': 'preflight'}
        with mock.patch.object(larry_alerts, 'append_alert'):
            self.assertEqual(
                inbox_watcher._rotation_gate_block_reason(env),
                'tier-pool-unavailable')

    def test_continuation_passes_even_when_all_held(self):
        self._bench('tier1')
        self._bench('tier3')
        for env in (
            {'task_id': 'c1', 'phase': 'build', 'session_id': 'abc'},
            {'task_id': 'c2', 'phase': 'revision', 'session_id': 'abc'},
            {'task_id': 'c3', 'phase': 'preflight', 'resume_session_id': 'xyz'},
        ):
            self.assertIsNone(
                inbox_watcher._rotation_gate_block_reason(env), env)

    def test_operator_pin_forces_open_gate(self):
        # An operator pin makes the selector return that tier unconditionally,
        # so the gate is pass-through (the pre-cutover state).
        self._bench('tier1')
        self._bench('tier3')
        (self.root / 'rotation.disabled').write_text('tier1')
        env = {'task_id': 'fresh-001', 'source': 'beacon', 'phase': 'preflight'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(env))

    def test_error_reading_pool_is_open_gate(self):
        env = {'task_id': 'fresh-001', 'source': 'beacon', 'phase': 'preflight'}
        with mock.patch.object(active_tier, 'has_usable_dispatch_tier',
                               side_effect=RuntimeError('boom')):
            self.assertIsNone(inbox_watcher._rotation_gate_block_reason(env))


class AllHeldEscalationTest(RotationGateBaseTest):
    """§ 9: a PERSISTENT all-held state fires one deduped alert; a transient
    blip does not; recovery clears the clock."""

    def test_first_block_starts_clock_no_alert(self):
        self._bench('tier1')
        self._bench('tier3')
        env = {'task_id': 'f', 'source': 'beacon', 'phase': 'preflight'}
        with mock.patch.object(larry_alerts, 'append_alert') as alert:
            inbox_watcher._rotation_gate_block_reason(env)
        alert.assert_not_called()
        self.assertTrue((self.root / 'state' / 'tier-pool-hold.json').exists())

    def test_persistent_hold_escalates_once(self):
        # Backdate the hold marker beyond the 10-min threshold.
        hold = self.root / 'state' / 'tier-pool-hold.json'
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        hold.write_text(json.dumps({'held_since': old}))
        self._bench('tier1')
        self._bench('tier3')
        with mock.patch.object(larry_alerts, 'append_alert') as alert:
            inbox_watcher._escalate_tier_pool_held_if_persistent()
        alert.assert_called_once()
        kwargs = alert.call_args.kwargs
        self.assertEqual(kwargs['subject'], 'tier-pool-all-unavailable')
        self.assertEqual(kwargs['route'], 'escalate')

    def test_recovery_clears_hold_marker(self):
        hold = self.root / 'state' / 'tier-pool-hold.json'
        hold.write_text(json.dumps({'held_since':
                                    datetime.now(timezone.utc).isoformat()}))
        # tier1/tier3 usable (no bench) -> gate open -> marker cleared.
        env = {'task_id': 'f', 'source': 'beacon', 'phase': 'preflight'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(env))
        self.assertFalse(hold.exists())

    def test_continuation_clears_stale_hold_on_recovery(self):
        # Finding-1 fix: recovery seen via a CONTINUATION-only poll must still
        # clear the debounce clock — otherwise the next unrelated all-held blip
        # reads the stale held_since and pages instantly (defeats the debounce).
        hold = self.root / 'state' / 'tier-pool-hold.json'
        hold.write_text(json.dumps({'held_since':
                                    datetime.now(timezone.utc).isoformat()}))
        cont = {'task_id': 'c1', 'phase': 'build', 'session_id': 'abc'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(cont))
        self.assertFalse(hold.exists())  # cleared despite a continuation


class IsContinuationTest(RotationGateBaseTest):
    """The continuation discriminator (unchanged by § 10-G)."""

    def test_preflight_with_no_session_is_not_continuation(self):
        self.assertFalse(inbox_watcher._is_continuation_task({'phase': 'preflight'}))

    def test_phase_build_is_continuation(self):
        self.assertTrue(inbox_watcher._is_continuation_task(
            {'phase': 'build', 'session_id': 'abc'}))

    def test_resume_session_id_under_preflight_is_continuation(self):
        self.assertTrue(inbox_watcher._is_continuation_task(
            {'phase': 'preflight', 'resume_session_id': 'xyz'}))

    def test_non_dict_input_is_not_continuation(self):
        self.assertFalse(inbox_watcher._is_continuation_task(None))
        self.assertFalse(inbox_watcher._is_continuation_task('hello'))


if __name__ == '__main__':
    unittest.main()
