#!/usr/bin/env python3
"""Retraction tests for heal_chain_event_shipper_heartbeat.py (slice 1 pilot).

Spec: agents/beacon/specs/notifier-auto-retraction.md. Proves the two guardrails
for this pilot:
  - a positively-fresh tick retracts a seeded stale alert for the healer's key
    and emits a closure stand-down;
  - a degraded / unreadable probe (not-stale-but-not-fresh, or the real stale
    path) does NOT retract.

Run:
    python3 -m unittest scripts.tests.test_heal_chain_event_shipper_retraction
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

import heal_chain_event_shipper_heartbeat as h  # noqa: E402
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
            source=h.ALERT_SOURCE, severity='warning',
            message='shipper heartbeat stale', subject=h.ALERT_SUBJECT,
            route='escalate',
        )
        self.assertTrue(ok)


class FreshTickRetractsTest(_IsolatedQueue):
    def test_fresh_tick_retracts_seeded_stale_alert(self):
        self._seed_stale_escalate()
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'check_staleness',
                               return_value=(False, 12.0, 'fresh')):
            rc = h.main()
        self.assertEqual(rc, 0)
        recs = self._records()
        # Seeded escalate is gone; exactly one closure stand-down remains.
        self.assertFalse(
            any(r.get('route') == 'escalate' for r in recs),
            f'stale escalate should be retracted, got {recs}',
        )
        closures = [r for r in recs if r.get('route') == 'closure']
        self.assertEqual(len(closures), 1)
        self.assertEqual(closures[0]['severity'], 'info')


class DegradedReadDoesNotRetractTest(unittest.TestCase):
    """REQUIRED criterion: a not-stale-but-not-fresh (degraded/unreadable) probe
    must NOT reach the retract call. Gate is `reason == 'fresh'`."""

    def _run_main(self, staleness):
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'check_staleness', return_value=staleness), \
             mock.patch.object(h, 'intentionally_off', return_value=(True, 'off')), \
             mock.patch.object(h, '_dm_larry', return_value=True), \
             mock.patch.object(h, '_retract_standdown') as retract:
            rc = h.main()
        return rc, retract

    def test_degraded_not_stale_not_fresh_does_not_retract(self):
        # not-stale (False) but reason is a probe-error sentinel, not 'fresh'.
        rc, retract = self._run_main(
            (False, -1.0, 'heartbeat stat failed: [Errno 5] I/O error'))
        self.assertEqual(rc, 0)
        retract.assert_not_called()

    def test_stale_path_does_not_retract(self):
        # The real degraded/crashed path returns is_stale=True → alert branch,
        # never the retract branch.
        rc, retract = self._run_main((True, 999.0, 'heartbeat mtime 999s old'))
        self.assertEqual(rc, 0)
        retract.assert_not_called()

    def test_fresh_tick_calls_retract(self):
        rc, retract = self._run_main((False, 5.0, 'fresh'))
        self.assertEqual(rc, 0)
        retract.assert_called_once()


if __name__ == '__main__':
    unittest.main()
