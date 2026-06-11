#!/usr/bin/env python3
"""Tests for the rotation drain + cooldown gate wired into
``inbox_watcher.process_task`` (spec § 6.3).

The gate blocks NEW top-level dispatches but ALLOWS continuations
(``--resume`` / ``phase=build|revision``) through so in-flight work
finishes on its original account. Same blocking semantic as
``_emergency_halt_active()``: task stays in the inbox; the next poll
re-evaluates.

These tests exercise the gate at the helper-function level rather than
spinning up the full agent_loop, because:

  - The helper IS the gate's single decision point; mocking the whole
    agent_loop pulls in dispatch_lease, models config loading, etc.,
    none of which exercise the gate logic.
  - The acceptance criteria are "blocks fresh top-level / passes
    continuation / passes cooldown" — all expressible as
    ``_rotation_gate_block_reason(envelope) == ?`` assertions.

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

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import active_tier  # noqa: E402
import inbox_watcher  # noqa: E402


class RotationGateBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev
        self.tmp.cleanup()


class DrainGateBlockTest(RotationGateBaseTest):
    """Spec acceptance: watcher blocks a fresh top-level task during drain
    but passes a --resume continuation."""

    def test_no_rotation_state_is_open_gate(self):
        # Default state: tier1, not draining, no cooldowns. New top-level
        # tasks pass through.
        envelope = {'task_id': 'fresh-001', 'source': 'beacon',
                    'phase': 'preflight'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(envelope))

    def test_draining_blocks_fresh_top_level(self):
        active_tier.set_draining(True)
        envelope = {'task_id': 'fresh-001', 'source': 'beacon',
                    'phase': 'preflight'}
        self.assertEqual(
            inbox_watcher._rotation_gate_block_reason(envelope),
            'draining',
        )

    def test_draining_passes_phase_build_continuation(self):
        active_tier.set_draining(True)
        envelope = {'task_id': 'continuation-001', 'source': 'beacon',
                    'phase': 'build', 'session_id': 'abc123'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(envelope))

    def test_draining_passes_phase_revision_continuation(self):
        active_tier.set_draining(True)
        envelope = {'task_id': 'rev-001', 'source': 'beacon',
                    'phase': 'revision', 'session_id': 'abc123'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(envelope))

    def test_draining_passes_resume_session_id_continuation(self):
        # Clarification-response shape: phase=preflight but with explicit
        # resume_session_id — the watcher must let it through (otherwise the
        # clarification answer would re-queue forever).
        active_tier.set_draining(True)
        envelope = {'task_id': 'clarify-001', 'source': 'beacon',
                    'phase': 'preflight',
                    'resume_session_id': 'session-xyz'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(envelope))


class CooldownGateBlockTest(RotationGateBaseTest):
    """Spec acceptance: watcher skips that account until expiry; cooldown
    clears at expiry."""

    def _set_cooldown_future(self, tier, hours_out=2):
        """Write a future-dated cooldown straight into state. We don't use
        ``set_cooldown(raw_excerpt='resets 3pm', now=...)`` because
        ``_rotation_gate_block_reason`` calls into ``cooldown_until`` which
        re-resolves wall-clock now — and the parsed "3pm" goes stale once
        wall-clock time passes 3pm UTC. Direct state injection keeps the
        test deterministic across clock drift."""
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        until_iso = (datetime.now(timezone.utc)
                     + timedelta(hours=hours_out)).isoformat()
        existing = active_tier.read()
        existing['cooldowns'] = {tier: until_iso}
        path.write_text(json.dumps(existing))
        return until_iso

    def test_active_tier_cooldown_blocks_fresh_top_level(self):
        self._set_cooldown_future('tier1')
        envelope = {'task_id': 'fresh-001', 'source': 'beacon',
                    'phase': 'preflight'}
        self.assertEqual(
            inbox_watcher._rotation_gate_block_reason(envelope),
            'cooldown',
        )

    def test_active_tier_cooldown_passes_continuation(self):
        # Continuation must always pass, even during cooldown.
        self._set_cooldown_future('tier1')
        envelope = {'task_id': 'rev-001', 'source': 'beacon',
                    'phase': 'build', 'session_id': 'abc123'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(envelope))

    def test_cooldown_only_blocks_when_active_tier_matches(self):
        # tier1 is the active tier; setting a cooldown on tier2 must NOT
        # block dispatch (the active account is unaffected).
        self._set_cooldown_future('tier2')
        envelope = {'task_id': 'fresh-001', 'source': 'beacon',
                    'phase': 'preflight'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(envelope))

    def test_expired_cooldown_does_not_block(self):
        # Write an expired cooldown directly into state. The gate reads via
        # active_tier.cooldown_until which filters expired entries.
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        # 1 hour in the past
        expired_iso = (datetime.now(timezone.utc) - timedelta(hours=1)) \
            .isoformat()
        path.write_text(json.dumps({
            'tier': 'tier1',
            'cooldowns': {'tier1': expired_iso},
        }))
        envelope = {'task_id': 'fresh-001', 'source': 'beacon',
                    'phase': 'preflight'}
        self.assertIsNone(inbox_watcher._rotation_gate_block_reason(envelope))


class IsContinuationTest(RotationGateBaseTest):
    """Unit-test the continuation discriminator directly. The watcher's
    resume-gate in process_task uses the same combined-OR logic; keeping
    them aligned is a structural invariant."""

    def test_preflight_with_no_session_is_not_continuation(self):
        envelope = {'phase': 'preflight'}
        self.assertFalse(inbox_watcher._is_continuation_task(envelope))

    def test_phase_build_is_continuation(self):
        envelope = {'phase': 'build', 'session_id': 'abc'}
        self.assertTrue(inbox_watcher._is_continuation_task(envelope))

    def test_phase_revision_is_continuation(self):
        envelope = {'phase': 'revision', 'session_id': 'abc'}
        self.assertTrue(inbox_watcher._is_continuation_task(envelope))

    def test_resume_session_id_under_preflight_is_continuation(self):
        envelope = {'phase': 'preflight', 'resume_session_id': 'xyz'}
        self.assertTrue(inbox_watcher._is_continuation_task(envelope))

    def test_non_dict_input_is_not_continuation(self):
        self.assertFalse(inbox_watcher._is_continuation_task(None))
        self.assertFalse(inbox_watcher._is_continuation_task('hello'))


if __name__ == '__main__':
    unittest.main()
