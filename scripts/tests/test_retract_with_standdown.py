#!/usr/bin/env python3
"""Tests for larry_alerts.retract_with_standdown — auditable alert retraction.

Slice 1 of notifier-auto-retraction (agents/beacon/specs/notifier-auto-retraction.md).
The helper wraps resolve_alert so a retraction of a red Larry actually SAW emits
a visible closure stand-down (never silent), and is a pure no-op on no-match.

Run:
    python3 -m unittest scripts.tests.test_retract_with_standdown
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
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

import larry_alerts  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # Drives the larry_alerts Layer-B chokepoint against already-isolated state,
    # so opt the guard out for the module (mirrors test_larry_alerts).
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


class RetractWithStanddownTest(unittest.TestCase):
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
            # The shipped-row clear is a best-effort network write; stub it so the
            # unit test never touches Supabase.
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

    def _seed_escalate(self, source, subject):
        ok = larry_alerts.append_alert(
            source=source, severity='warning', message='stale!',
            subject=subject, route='escalate',
        )
        self.assertTrue(ok)

    def test_no_match_returns_zero_and_appends_nothing(self):
        removed = larry_alerts.retract_with_standdown(
            'nobody:nothing', 'stand down',
        )
        self.assertEqual(removed, 0)
        self.assertEqual(self._records(), [])

    def test_no_match_leaves_unrelated_lines_untouched(self):
        self._seed_escalate('some-healer', 'some-subject')
        before = self._records()
        removed = larry_alerts.retract_with_standdown(
            'other-healer:other-subject', 'stand down',
        )
        self.assertEqual(removed, 0)
        # No retraction, no closure line — the queue is exactly as seeded.
        self.assertEqual(self._records(), before)

    def test_match_removes_line_and_appends_one_closure(self):
        self._seed_escalate('heal-x', 'subj-x')
        removed = larry_alerts.retract_with_standdown(
            'heal-x:subj-x', 'heartbeat fresh again — standing down',
        )
        self.assertEqual(removed, 1)
        recs = self._records()
        # The escalate line is gone; exactly one closure line remains.
        self.assertEqual(len(recs), 1)
        closure = recs[0]
        self.assertEqual(closure['route'], 'closure')
        self.assertEqual(closure['severity'], 'info')
        self.assertEqual(closure['message'], 'heartbeat fresh again — standing down')
        # No surviving escalate line for the key.
        self.assertFalse(
            any(r.get('route') == 'escalate' and r.get('source') == 'heal-x'
                for r in recs)
        )

    def test_subject_defaults_to_key(self):
        self._seed_escalate('heal-y', 'subj-y')
        larry_alerts.retract_with_standdown('heal-y:subj-y', 'sd')
        closure = self._records()[0]
        self.assertEqual(closure['subject'], 'heal-y:subj-y')

    def test_explicit_subject_used_for_closure(self):
        self._seed_escalate('heal-z', 'subj-z')
        larry_alerts.retract_with_standdown(
            'heal-z:subj-z', 'sd', subject='custom-closure-subject',
        )
        closure = self._records()[0]
        self.assertEqual(closure['subject'], 'custom-closure-subject')

    def test_never_raises_on_resolve_error(self):
        # resolve_alert is contractually no-raise, but the wrapper must swallow
        # even a hard failure and report 0 rather than propagate.
        with mock.patch.object(larry_alerts, 'resolve_alert',
                               side_effect=RuntimeError('boom')):
            try:
                removed = larry_alerts.retract_with_standdown('k', 'sd')
            except Exception as exc:  # noqa: BLE001
                self.fail(f'retract_with_standdown raised: {exc!r}')
        self.assertEqual(removed, 0)


if __name__ == '__main__':
    unittest.main()
