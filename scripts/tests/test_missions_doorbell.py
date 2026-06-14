#!/usr/bin/env python3
"""Tests for missions_doorbell.py — the Parked-card conversation doorbell
(Missions v2 Phase 4 § 9).

`decide_doorbell` is a pure function (no mocks needed). `ring_doorbell` /
`resolve_doorbell` perform side effects through `larry_alerts` /
`alert_triage_state`, which `refuse_under_test` — so every ring/resolve test
MUST mock those two sinks (the sandbox env-redirection does NOT reach
larry_alerts' Path.home()-frozen AGENTS_ROOT, and the real tree must never be
written from a test). A test that forgets to mock fails loud rather than
silently writing prod state.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_missions_doorbell
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from unittest import mock
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import missions_doorbell as md  # noqa: E402


# ---------- decide_doorbell (pure) ----------

class DecideDoorbellTest(unittest.TestCase):

    def test_blocked_careful_is_critical_escalate(self):
        plan = md.decide_doorbell(blocked=True, risk='careful')
        self.assertEqual(plan, {
            'loudness': 'blocked-on-you',
            'route': 'escalate',
            'severity': 'critical',
        })

    def test_blocked_non_careful_is_warning_escalate(self):
        for risk in ('safe', 'medium', None, 'unknown'):
            with self.subTest(risk=risk):
                plan = md.decide_doorbell(blocked=True, risk=risk)
                self.assertEqual(plan['loudness'], 'blocked-on-you')
                self.assertEqual(plan['route'], 'escalate')
                self.assertEqual(plan['severity'], 'warning')

    def test_briefed_not_blocked_is_fyi_digest(self):
        for risk in ('safe', 'medium', 'careful'):
            with self.subTest(risk=risk):
                plan = md.decide_doorbell(blocked=False, risk=risk)
                self.assertEqual(plan, {
                    'loudness': 'fyi',
                    'route': 'digest',
                    'severity': 'warning',
                })

    def test_unbriefed_not_blocked_is_none(self):
        for risk in (None, '', 'bogus'):
            with self.subTest(risk=risk):
                self.assertIsNone(md.decide_doorbell(blocked=False, risk=risk))


# ---------- doorbell_key ----------

class DoorbellKeyTest(unittest.TestCase):

    def test_key_is_source_scoped(self):
        self.assertEqual(md.doorbell_key('cap-9'), 'missions-doorbell:cap-9')


# ---------- ring_doorbell (mocked sinks) ----------

class RingDoorbellTest(unittest.TestCase):

    def test_no_ring_is_clean_noop(self):
        # un-briefed + not blocked → no alert, no triage, no sink calls at all.
        with mock.patch.object(md, 'larry_alerts') as alerts, \
                mock.patch.object(md, 'alert_triage_state') as triage:
            res = md.ring_doorbell(capture_id='cap-1', blocked=False, risk=None)
        self.assertEqual(res, {'rung': False, 'capture_id': 'cap-1',
                               'loudness': None})
        alerts.append_alert.assert_not_called()
        triage.record_triage.assert_not_called()

    def test_blocked_rings_loud_escalate_tier3(self):
        with mock.patch.object(md, 'larry_alerts') as alerts, \
                mock.patch.object(md, 'alert_triage_state') as triage:
            alerts.append_alert.return_value = True
            res = md.ring_doorbell(
                capture_id='cap-2', blocked=True, risk='careful',
                title='Ship it?', deep_link='https://dash/cap-2',
            )
        self.assertTrue(res['rung'])
        self.assertTrue(res['appended'])
        self.assertEqual(res['loudness'], 'blocked-on-you')
        self.assertEqual(res['route'], 'escalate')
        self.assertEqual(res['severity'], 'critical')
        self.assertEqual(res['alert_id'], 'missions-doorbell:cap-2')

        _, akw = alerts.append_alert.call_args
        self.assertEqual(akw['source'], md.SOURCE)
        self.assertEqual(akw['severity'], 'critical')
        self.assertEqual(akw['route'], 'escalate')
        self.assertEqual(akw['subject'], 'cap-2')
        self.assertEqual(akw['suggested_action'], 'https://dash/cap-2')
        self.assertIn('Ship it?', akw['message'])
        self.assertIn('https://dash/cap-2', akw['message'])

        _, tkw = triage.record_triage.call_args
        self.assertEqual(tkw['alert_id'], 'missions-doorbell:cap-2')
        self.assertEqual(tkw['tier'], md._TIER_BLOCKED)
        self.assertEqual(tkw['decision'], 'ring-blocked-on-you')
        self.assertEqual(tkw['route'], 'escalate')

    def test_fyi_rings_quiet_digest_tier4(self):
        with mock.patch.object(md, 'larry_alerts') as alerts, \
                mock.patch.object(md, 'alert_triage_state') as triage:
            alerts.append_alert.return_value = True
            res = md.ring_doorbell(capture_id='cap-3', blocked=False, risk='safe')
        self.assertEqual(res['loudness'], 'fyi')
        self.assertEqual(res['route'], 'digest')
        _, tkw = triage.record_triage.call_args
        self.assertEqual(tkw['tier'], md._TIER_FYI)
        self.assertEqual(tkw['decision'], 'ring-fyi')

    def test_records_triage_even_when_alert_suppressed(self):
        # larry_alerts cooldown/silence → append_alert False, but the triage row
        # is still written so a later resolve has something to clear.
        with mock.patch.object(md, 'larry_alerts') as alerts, \
                mock.patch.object(md, 'alert_triage_state') as triage:
            alerts.append_alert.return_value = False
            res = md.ring_doorbell(capture_id='cap-4', blocked=True, risk='safe')
        self.assertTrue(res['rung'])
        self.assertFalse(res['appended'])
        triage.record_triage.assert_called_once()


# ---------- resolve_doorbell (mocked sinks) ----------

class ResolveDoorbellTest(unittest.TestCase):

    def test_resolve_retracts_alert_and_marks_triage(self):
        with mock.patch.object(md, 'larry_alerts') as alerts, \
                mock.patch.object(md, 'alert_triage_state') as triage:
            alerts.resolve_alert.return_value = 1
            triage.mark_resolved.return_value = True
            res = md.resolve_doorbell(capture_id='cap-5')
        self.assertEqual(res['capture_id'], 'cap-5')
        self.assertEqual(res['removed'], 1)
        self.assertTrue(res['resolved'])
        alerts.resolve_alert.assert_called_once_with('missions-doorbell:cap-5')
        margs, _ = triage.mark_resolved.call_args
        self.assertEqual(margs[0], 'missions-doorbell:cap-5')
        self.assertEqual(margs[2], 'larry-replied')

    def test_resolve_noop_when_nothing_pending(self):
        with mock.patch.object(md, 'larry_alerts') as alerts, \
                mock.patch.object(md, 'alert_triage_state') as triage:
            alerts.resolve_alert.return_value = 0
            triage.mark_resolved.return_value = False
            res = md.resolve_doorbell(capture_id='cap-6')
        self.assertEqual(res['removed'], 0)
        self.assertFalse(res['resolved'])


if __name__ == '__main__':
    unittest.main()
