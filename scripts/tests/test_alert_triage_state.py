#!/usr/bin/env python3
"""Tests for alert_triage_state (PR-β alert-triage lifecycle).

Covers: lifecycle transitions, atomic-write semantics, idempotency on
re-triage.

Run::

    cd ~/agent-core && python3 -m pytest scripts/tests/test_alert_triage_state.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import alert_triage_state as ats  # noqa: E402


class _ATSTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prior_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(ats)

    def tearDown(self):
        if self._prior_root is not None:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prior_root
        else:
            del os.environ['OURLIBERTY_AGENTS_ROOT']
        importlib.reload(ats)

    def _state_path(self) -> Path:
        return self.tmp / ats.STATE_REL


class TestReadState(_ATSTestBase):

    def test_missing_file_returns_empty(self):
        self.assertEqual(ats.read_state(), {})

    def test_corrupt_json_returns_empty(self):
        self._state_path().parent.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text('{garbage')
        self.assertEqual(ats.read_state(), {})


class TestRecordTriage(_ATSTestBase):

    def test_initial_triage_creates_row(self):
        row = ats.record_triage('alert-1', tier=2, decision='dispatch',
                                rationale='matches healer pattern')
        self.assertEqual(row['status'], 'triaged-tier-2')
        self.assertEqual(row['tier'], 2)
        self.assertEqual(row['decision'], 'dispatch')
        self.assertIsNotNone(row['triaged_at'])
        self.assertIsNone(row['dispatched_at'])

    def test_re_triage_preserves_dispatched_at(self):
        ats.record_triage('alert-1', tier=2, decision='dispatch',
                          rationale='initial')
        ats.mark_dispatched('alert-1', dispatch_ts='2026-05-25T12:00:00Z',
                            target_agent='beacon', task_id='t-99')
        # Re-triage moves status back to triaged-tier-N but keeps
        # dispatch metadata for audit.
        row = ats.record_triage('alert-1', tier=3, decision='snooze',
                                rationale='auto-resolved upstream')
        self.assertEqual(row['status'], 'triaged-tier-3')
        self.assertEqual(row['dispatched_at'], '2026-05-25T12:00:00Z')
        self.assertEqual(row['dispatch_target_agent'], 'beacon')

    def test_invalid_tier_raises(self):
        with self.assertRaises(ValueError):
            ats.record_triage('alert-1', tier=9, decision='x', rationale='y')

    def test_empty_alert_id_raises(self):
        with self.assertRaises(ValueError):
            ats.record_triage('', tier=1, decision='x', rationale='y')


class TestMarkDispatched(_ATSTestBase):

    def test_dispatch_after_triage(self):
        ats.record_triage('a1', tier=1, decision='dispatch', rationale='ok')
        ok = ats.mark_dispatched('a1', '2026-05-25T12:30:00Z', 'forge', 'tid')
        self.assertTrue(ok)
        row = ats.read_state()['a1']
        self.assertEqual(row['status'], 'action-dispatched')
        self.assertEqual(row['dispatch_target_agent'], 'forge')
        self.assertEqual(row['dispatch_task_id'], 'tid')

    def test_dispatch_without_triage_is_noop(self):
        ok = ats.mark_dispatched('unknown', 'ts', 'agent', 'task')
        self.assertFalse(ok)
        self.assertEqual(ats.read_state(), {})


class TestMarkResolved(_ATSTestBase):

    def test_resolve_after_triage(self):
        ats.record_triage('a1', tier=1, decision='dispatch', rationale='ok')
        ats.mark_dispatched('a1', '2026-05-25T12:30:00Z', 'forge', 'tid')
        ok = ats.mark_resolved('a1', '2026-05-25T13:00:00Z', 'PR merged')
        self.assertTrue(ok)
        row = ats.read_state()['a1']
        self.assertEqual(row['status'], 'resolved')
        self.assertEqual(row['resolution'], 'PR merged')

    def test_resolve_without_dispatch_still_allowed(self):
        # Larry may resolve directly without a Pulse-dispatched action.
        ats.record_triage('a1', tier=1, decision='noop', rationale='manual')
        ok = ats.mark_resolved('a1', '2026-05-25T13:00:00Z', 'manual fix')
        self.assertTrue(ok)
        self.assertEqual(ats.read_state()['a1']['status'], 'resolved')

    def test_resolve_unknown_is_noop(self):
        ok = ats.mark_resolved('missing', 'ts', 'no action')
        self.assertFalse(ok)


class TestAtomicWrite(_ATSTestBase):

    def test_no_tmp_left_after_write(self):
        ats.record_triage('a1', tier=1, decision='x', rationale='y')
        leftovers = list(self._state_path().parent.glob('*.tmp'))
        self.assertEqual(leftovers, [])

    def test_state_file_is_valid_json_after_multiple_writes(self):
        ats.record_triage('a1', tier=1, decision='x', rationale='y')
        ats.record_triage('a2', tier=2, decision='z', rationale='w')
        ats.mark_dispatched('a1', 'ts', 'agent', 'task')
        data = json.loads(self._state_path().read_text())
        self.assertIn('a1', data)
        self.assertIn('a2', data)


if __name__ == '__main__':
    unittest.main()
