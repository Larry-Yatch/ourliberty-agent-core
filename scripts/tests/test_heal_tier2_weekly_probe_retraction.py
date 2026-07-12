#!/usr/bin/env python3
"""Retraction tests for heal_tier2_weekly_health_probe.py (slice 2 adopter).

Spec: agents/beacon/specs/notifier-auto-retraction.md. Proves the two guardrails
for this single-subject adopter:
  - a positively-successful probe (PROBE_OK, exit 0) retracts a seeded stale
    `tier2_weekly_probe_failed` alert and emits a closure stand-down;
  - a degraded probe (ok=False: timeout / OOM / 401 / missing binary) does NOT
    retract. Gate is the positive `ok` observation.

Run:
    python3 -m unittest scripts.tests.test_heal_tier2_weekly_probe_retraction
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_tier2_weekly_health_probe as h  # noqa: E402
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

    def _seed_stale_escalate(self):
        ok = larry_alerts.append_alert(
            source=h.PROBE_ALERT_SOURCE, severity='warning',
            message='tier2 weekly probe failed', subject=h.PROBE_ALERT_SUBJECT,
            route='escalate',
        )
        self.assertTrue(ok)


class HealthyProbeRetractsTest(_IsolatedQueue):
    def test_healthy_probe_retracts_seeded_stale_alert(self):
        self._seed_stale_escalate()
        with mock.patch.object(h, 'KILL_SWITCH', Path('/nonexistent-killswitch-xyz')), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'check_provisioning_parity', return_value=[]), \
             mock.patch.object(h, 'load_haiku_model_id', return_value='m'), \
             mock.patch.object(h, 'run_probe',
                               return_value=(True, 'PROBE_OK', '', 0)):
            rc = h.run()
        self.assertEqual(rc, 0)
        recs = self._records()
        self.assertFalse(
            any(r.get('route') == 'escalate' for r in recs),
            f'stale escalate should be retracted, got {recs}',
        )
        closures = [r for r in recs if r.get('route') == 'closure']
        self.assertEqual(len(closures), 1)
        self.assertEqual(closures[0]['severity'], 'info')


class DegradedProbeDoesNotRetractTest(unittest.TestCase):
    """REQUIRED criterion: a degraded probe (ok=False) must NOT reach the
    retract call. Gate is the positive `ok` observation."""

    def _run(self, probe_result):
        with mock.patch.object(h, 'KILL_SWITCH', Path('/nonexistent-killswitch-xyz')), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'check_provisioning_parity', return_value=[]), \
             mock.patch.object(h, 'load_haiku_model_id', return_value='m'), \
             mock.patch.object(h, 'run_probe', return_value=probe_result), \
             mock.patch.object(h, 'load_state', return_value={}), \
             mock.patch.object(h, 'save_state'), \
             mock.patch.object(h, '_in_dm_cooldown', return_value=False), \
             mock.patch.object(larry_alerts, 'append_alert', return_value=True), \
             mock.patch.object(h, '_retract_probe_standdown') as retract:
            rc = h.run()
        return rc, retract

    def test_timeout_failure_does_not_retract(self):
        rc, retract = self._run((False, '', 'TimeoutExpired', -1))
        self.assertEqual(rc, 0)
        retract.assert_not_called()

    def test_auth_401_failure_does_not_retract(self):
        rc, retract = self._run((False, '', 'HTTP 401 Unauthorized', 1))
        self.assertEqual(rc, 0)
        retract.assert_not_called()

    def test_healthy_probe_calls_retract(self):
        rc, retract = self._run((True, 'PROBE_OK', '', 0))
        self.assertEqual(rc, 0)
        retract.assert_called_once()


if __name__ == '__main__':
    unittest.main()
