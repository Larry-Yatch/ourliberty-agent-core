#!/usr/bin/env python3
"""Retraction tests for heal_daemon_restart_manifest_drift.py (slice 2 adopter).

Spec: agents/beacon/specs/notifier-auto-retraction.md. Proves the two guardrails
for this multi-subject-single-detector adopter:
  - a positively-fresh tick (no drift: committed manifest == live closure)
    retracts every seeded stale escalate subject and emits a closure stand-down
    for each;
  - a degraded read (build/load error → 'error') and a still-drifted tick do
    NOT retract. Gate is the positive no-drift observation.

Run:
    python3 -m unittest scripts.tests.test_heal_daemon_restart_manifest_drift_retraction
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_daemon_restart_manifest_drift as h  # noqa: E402
import larry_alerts  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


def _drift(has_drift):
    d = types.SimpleNamespace(has_drift=has_drift)
    d.one_line = lambda: 'drift one-line'
    d.alert_body = lambda: 'drift body'
    return d


class _IsolatedQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tp = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', tp),
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              tp / 'blackboard' / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              tp / 'state' / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'SILENCE_ROOT',
                              tp / 'state' / 'alert-silenced'),
            mock.patch.object(larry_alerts, 'SILENCE_COUNTER_ROOT',
                              tp / 'state' / 'alert-silenced-counts'),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              tp / 'state' / 'beacon-alerts-offset.txt'),
            mock.patch.object(larry_alerts, 'MEDIC_OFFSET_FILE',
                              tp / 'state' / 'medic-alerts-offset.txt'),
            mock.patch.object(larry_alerts, '_retract_shipped_alert_events',
                              return_value=0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _records(self):
        af = larry_alerts.ALERTS_FILE
        if not af.exists():
            return []
        return [json.loads(ln) for ln in af.read_text().splitlines() if ln.strip()]

    def _seed_stale_escalate(self, subject):
        ok = larry_alerts.append_alert(
            source=h.SOURCE, severity='warning',
            message=f'{subject} seeded red', subject=subject, route='escalate',
        )
        self.assertTrue(ok)


class FreshTickRetractsAllTest(_IsolatedQueue):
    def test_no_drift_retracts_all_seeded_stale_alerts(self):
        for subject in h._ESCALATE_SUBJECTS:
            self._seed_stale_escalate(subject)
        with mock.patch.object(h.drm, 'load_manifest', return_value={}), \
             mock.patch.object(h.drm, 'build_manifest', return_value={}), \
             mock.patch.object(h, 'compute_drift', return_value=_drift(False)):
            outcome = h.run_once()
        self.assertEqual(outcome, 'fresh')
        recs = self._records()
        self.assertFalse(
            any(r.get('route') == 'escalate' for r in recs),
            f'all stale escalates should be retracted, got {recs}',
        )
        closures = [r for r in recs if r.get('route') == 'closure']
        self.assertEqual(len(closures), len(h._ESCALATE_SUBJECTS))
        self.assertTrue(all(c['severity'] == 'info' for c in closures))


class DegradedReadDoesNotRetractTest(unittest.TestCase):
    """REQUIRED criterion: an error/degraded read and a still-drifted tick must
    NOT reach the retract call. Gate is the positive no-drift observation."""

    def test_build_error_does_not_retract(self):
        with mock.patch.object(h, '_retract_standdown') as retract, \
             mock.patch.object(h.drm, 'load_manifest', return_value={}), \
             mock.patch.object(h.drm, 'build_manifest',
                               side_effect=RuntimeError('parse boom')):
            outcome = h.run_once()
        self.assertEqual(outcome, 'error')
        retract.assert_not_called()

    def test_still_drifted_dry_run_does_not_retract(self):
        with mock.patch.object(h, '_retract_standdown') as retract, \
             mock.patch.object(h.drm, 'load_manifest', return_value={}), \
             mock.patch.object(h.drm, 'build_manifest', return_value={'x': 1}), \
             mock.patch.object(h, 'compute_drift', return_value=_drift(True)):
            outcome = h.run_once(dry_run=True)
        self.assertEqual(outcome, 'drift-dry-run')
        retract.assert_not_called()

    def test_no_drift_calls_retract(self):
        with mock.patch.object(h, '_retract_standdown') as retract, \
             mock.patch.object(h.drm, 'load_manifest', return_value={}), \
             mock.patch.object(h.drm, 'build_manifest', return_value={}), \
             mock.patch.object(h, 'compute_drift', return_value=_drift(False)):
            outcome = h.run_once()
        self.assertEqual(outcome, 'fresh')
        retract.assert_called_once()


if __name__ == '__main__':
    unittest.main()
