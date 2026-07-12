#!/usr/bin/env python3
"""Retraction tests for heal_chain_event_type_audit.py (slice 2 adopter).

Spec: agents/beacon/specs/notifier-auto-retraction.md. Proves the two guardrails
for this single-subject adopter:
  - a positively-clear tick (audit ran successfully AND found zero unknown
    event_types) retracts a seeded stale alert and emits a closure stand-down;
  - every degraded read (connect failure, query failure) and a tick that finds
    unknown types do NOT retract. Gate is the positive `not unknown` branch,
    reached only after a successful connect + query + classify.

Run:
    python3 -m unittest scripts.tests.test_heal_chain_event_type_audit_retraction
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

import heal_chain_event_type_audit as h  # noqa: E402
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
            message='unknown event_types found', subject=h.ALERT_SUBJECT,
            route='escalate',
        )
        self.assertTrue(ok)


class CleanAuditRetractsTest(_IsolatedQueue):
    def test_zero_unknown_retracts_seeded_stale_alert(self):
        self._seed_stale_escalate()
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, '_connect_supabase', return_value=object()), \
             mock.patch.object(h, 'fetch_event_type_counts',
                               return_value={'known': 5}), \
             mock.patch.object(h, 'find_unknown_types', return_value={}):
            rc = h.main()
        self.assertEqual(rc, 0)
        recs = self._records()
        self.assertFalse(
            any(r.get('route') == 'escalate' for r in recs),
            f'stale escalate should be retracted, got {recs}',
        )
        closures = [r for r in recs if r.get('route') == 'closure']
        self.assertEqual(len(closures), 1)
        self.assertEqual(closures[0]['severity'], 'info')


class DegradedReadDoesNotRetractTest(unittest.TestCase):
    """REQUIRED criterion: connect/query failures and an unknown-types tick must
    NOT reach the retract call. Gate is the positive `not unknown` branch."""

    def test_connect_failure_does_not_retract(self):
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, '_connect_supabase',
                               side_effect=RuntimeError('no creds')), \
             mock.patch.object(h, '_retract_standdown') as retract:
            rc = h.main()
        self.assertEqual(rc, 0)
        retract.assert_not_called()

    def test_query_failure_does_not_retract(self):
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, '_connect_supabase', return_value=object()), \
             mock.patch.object(h, 'fetch_event_type_counts',
                               side_effect=RuntimeError('query boom')), \
             mock.patch.object(h, '_retract_standdown') as retract:
            rc = h.main()
        self.assertEqual(rc, 0)
        retract.assert_not_called()

    def test_unknown_types_present_does_not_retract(self):
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, '_connect_supabase', return_value=object()), \
             mock.patch.object(h, 'fetch_event_type_counts',
                               return_value={'weird': 2}), \
             mock.patch.object(h, 'find_unknown_types',
                               return_value={'weird': 2}), \
             mock.patch.object(h, '_dm_larry', return_value=True), \
             mock.patch.object(h, '_retract_standdown') as retract:
            rc = h.main()
        self.assertEqual(rc, 0)
        retract.assert_not_called()

    def test_zero_unknown_calls_retract(self):
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, '_connect_supabase', return_value=object()), \
             mock.patch.object(h, 'fetch_event_type_counts',
                               return_value={'known': 5}), \
             mock.patch.object(h, 'find_unknown_types', return_value={}), \
             mock.patch.object(h, '_retract_standdown') as retract:
            rc = h.main()
        self.assertEqual(rc, 0)
        retract.assert_called_once()


if __name__ == '__main__':
    unittest.main()
