#!/usr/bin/env python3
"""Retraction tests for heal_dashboard_api_sha_drift.py (slice 2 adopter).

Spec: agents/beacon/specs/notifier-auto-retraction.md. Proves the two guardrails
for this multi-subject-single-detector adopter:
  - a positively-fresh tick (PROBE_OK + running SHA == on-disk HEAD) retracts
    BOTH seeded stale escalate subjects (`-stuck`, `-restart-failed`) and emits
    a closure stand-down for each;
  - every degraded / non-fresh probe (unreachable / auth / no-token /
    field-missing / route-missing, and the confirmed-stale path) does NOT
    retract. Gate is the positive 'fresh' outcome.

Run:
    python3 -m unittest scripts.tests.test_heal_dashboard_api_sha_drift_retraction
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

import heal_dashboard_api_sha_drift as h  # noqa: E402
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

    def _seed_stale_escalate(self, subject):
        ok = larry_alerts.append_alert(
            source=h.ALERT_SOURCE, severity='critical',
            message=f'{subject} seeded red', subject=subject, route='escalate',
        )
        self.assertTrue(ok)


class FreshTickRetractsBothTest(_IsolatedQueue):
    def test_fresh_tick_retracts_both_seeded_stale_alerts(self):
        self._seed_stale_escalate(h.STUCK_SUBJECT)
        self._seed_stale_escalate(h.RESTART_FAILED_SUBJECT)
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'read_disk_head', return_value='a' * 40), \
             mock.patch.object(h, 'probe_running_sha',
                               return_value=(h.PROBE_OK, 'a' * 40)):
            outcome = h.run_once()
        self.assertEqual(outcome, 'fresh')
        recs = self._records()
        self.assertFalse(
            any(r.get('route') == 'escalate' for r in recs),
            f'both stale escalates should be retracted, got {recs}',
        )
        closures = [r for r in recs if r.get('route') == 'closure']
        self.assertEqual(len(closures), 2)
        self.assertTrue(all(c['severity'] == 'info' for c in closures))


class DegradedReadDoesNotRetractTest(unittest.TestCase):
    """REQUIRED criterion: every non-fresh probe must NOT reach the retract
    call. Gate is the positive 'fresh' outcome."""

    def _run(self, disk_head, probe_return, extra_patches=()):
        with mock.patch.object(h, '_retract_standdown') as retract, \
             mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'read_disk_head', return_value=disk_head), \
             mock.patch.object(h, 'probe_running_sha', return_value=probe_return):
            for p in extra_patches:
                p.start()
            try:
                outcome = h.run_once()
            finally:
                for p in extra_patches:
                    p.stop()
        return outcome, retract

    def test_unreachable_does_not_retract(self):
        outcome, retract = self._run('a' * 40, (h.PROBE_UNREACHABLE, None))
        self.assertEqual(outcome, 'unreachable')
        retract.assert_not_called()

    def test_auth_does_not_retract(self):
        outcome, retract = self._run('a' * 40, (h.PROBE_AUTH, None))
        self.assertEqual(outcome, 'auth')
        retract.assert_not_called()

    def test_no_token_does_not_retract(self):
        outcome, retract = self._run('a' * 40, (h.PROBE_NO_TOKEN, None))
        self.assertEqual(outcome, 'no-token')
        retract.assert_not_called()

    def test_no_disk_head_does_not_retract(self):
        outcome, retract = self._run('', (h.PROBE_OK, 'a' * 40))
        self.assertEqual(outcome, 'no-disk-head')
        retract.assert_not_called()

    def test_confirmed_stale_does_not_retract(self):
        # PROBE_OK but running SHA != on-disk HEAD → stale branch (restart),
        # never the fresh/retract branch. Mock the restart machinery so no
        # real systemctl / state write happens.
        extra = (
            mock.patch.object(h, 'load_state', return_value={}),
            mock.patch.object(h, 'save_state'),
            mock.patch.object(h, 'restart_unit', return_value=(0, '')),
            mock.patch.object(h, '_dm', return_value=True),
        )
        outcome, retract = self._run('a' * 40, (h.PROBE_OK, 'b' * 40), extra)
        self.assertEqual(outcome, 'restarted')
        retract.assert_not_called()

    def test_fresh_calls_retract(self):
        outcome, retract = self._run('a' * 40, (h.PROBE_OK, 'a' * 40))
        self.assertEqual(outcome, 'fresh')
        retract.assert_called_once()


if __name__ == '__main__':
    unittest.main()
