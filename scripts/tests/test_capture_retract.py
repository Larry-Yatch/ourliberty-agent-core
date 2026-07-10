#!/usr/bin/env python3
"""Tests for the agent-callable capture-retract handler (slice 9).

Mirror focus:
  - SAFETY WALL: the ingest-token retract drops ONLY a machine-owned
    (CAPTURE_MACHINE_RETRACTABLE_LABELS) card — a human capture (label=None /
    desktop-chat) is NEVER dropped even when the id matches.
  - only a still-`parked` card is dropped; a promoted/dropped card is left alone.
  - idempotent + fail-soft: a missing / already-dropped card returns
    retracted=False with a reason, never raises (a reconciler loop must not crash).
  - the drop rides the single captures.json writer (state='dropped' + drop_reason).

captures.json is a tmp file passed directly to the handler; the real registry is
never touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_capture_retract
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api  # noqa: E402


def _write_captures(path: Path, captures: list) -> None:
    path.write_text(json.dumps({'schema_version': 1, 'captures': captures}))


def _read_captures(path: Path) -> dict:
    return {c['id']: c for c in json.loads(path.read_text())['captures']}


class CaptureRetractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'captures.json'
        _write_captures(self.path, [
            {'id': 'medic-1', 'state': 'parked', 'label': 'medic-proposal',
             'title': 'Recurring: Medic wants to restart-daemon foo'},
            {'id': 'human-1', 'state': 'parked', 'label': None,
             'title': "Larry's idea", 'origin': {'source': 'desktop-chat'}},
            {'id': 'medic-done', 'state': 'dropped', 'label': 'medic-proposal',
             'title': 'already dropped'},
            {'id': 'pulse-1', 'state': 'parked', 'label': 'pulse-check-i',
             'title': 'a pulse parked proposal'},
        ])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _retract(self, cid, reason=None):
        return dashboard_api._handle_capture_retract(
            capture_id=cid, captures_path=self.path, reason=reason,
        )

    def test_retracts_a_parked_machine_capture(self):
        r = self._retract('medic-1', reason='condition-cleared')
        self.assertTrue(r['retracted'])
        self.assertEqual(r['state'], 'dropped')
        row = _read_captures(self.path)['medic-1']
        self.assertEqual(row['state'], 'dropped')
        self.assertIn('condition-cleared', row['drop_reason'])

    def test_never_retracts_a_human_capture(self):
        r = self._retract('human-1')
        self.assertFalse(r['retracted'])
        self.assertEqual(r['reason'], 'not-machine-retractable')
        # the human card is UNTOUCHED — still parked, no drop_reason.
        row = _read_captures(self.path)['human-1']
        self.assertEqual(row['state'], 'parked')
        self.assertNotIn('drop_reason', row)

    def test_not_parked_is_left_alone(self):
        r = self._retract('medic-done')
        self.assertFalse(r['retracted'])
        self.assertEqual(r['reason'], 'not-parked')

    def test_missing_capture_is_idempotent_noop(self):
        r = self._retract('does-not-exist')
        self.assertFalse(r['retracted'])
        self.assertEqual(r['reason'], 'not-found')

    def test_double_retract_is_terminal_second_time(self):
        self.assertTrue(self._retract('medic-1')['retracted'])
        second = self._retract('medic-1')
        self.assertFalse(second['retracted'])
        self.assertEqual(second['reason'], 'not-parked')

    def test_pulse_label_not_machine_retractable_this_slice(self):
        # Only 'medic-proposal' is machine-retractable in slice 9; a pulse card
        # is not (pulse_check_i does not self-retract yet).
        r = self._retract('pulse-1')
        self.assertFalse(r['retracted'])
        self.assertEqual(r['reason'], 'not-machine-retractable')


if __name__ == '__main__':
    unittest.main()
