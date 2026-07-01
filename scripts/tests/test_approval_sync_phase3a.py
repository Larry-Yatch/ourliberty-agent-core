#!/usr/bin/env python3
"""Tests for approval-sync Phase 3a — the "Needs You" classification core.

Covers the three §3a deliverables that land in the deep-review critical
fileset (larry_alerts / for_larry_signal / chain_event_emit / the shipper's
type registry):

  * §3a.2  producer-side ``needs_larry`` flag on append_alert + for_larry_signal
  * §3a.2  the retraction gap: resolve_alert must clear the already-shipped
           ``larry_alert`` chain_event row (else an auto-fixed alert renders
           live on the dashboard forever)
  * §3a.1  registration of the two projected event types the emit sites (PR-2)
           will push

Run:
    python3 -m unittest scripts.tests.test_approval_sync_phase3a
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import chain_event_emit as cee  # noqa: E402
import chain_event_shipper as ces  # noqa: E402
import for_larry_signal  # noqa: E402
import larry_alerts  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # Drives the Layer-B-guarded larry_alerts chokepoint against already-isolated
    # tempfile state, so the guard would breach before the test's own mocks. Opt
    # out for the module (pass-through); mirrors test_larry_alerts.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


# --------------------------------------------------------------------------
# §3a.1 — the two projected event types are registered so emit_event admits
# them and the weekly chain-event-type audit does not flag them as unknown.
# --------------------------------------------------------------------------
class KnownEventTypesTest(unittest.TestCase):
    def test_new_needs_you_types_registered(self):
        self.assertIn('parked_capture', ces.KNOWN_EVENT_TYPES)
        self.assertIn('sequence_needs_you', ces.KNOWN_EVENT_TYPES)


# --------------------------------------------------------------------------
# §3a.2 — producer-side needs_larry flag (default False; stamped only when True
# on the lean alert record; explicit bool on every for_larry record).
# --------------------------------------------------------------------------
class _IsolatedQueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        td = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', td),
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              td / 'blackboard' / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              td / 'state' / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'SILENCE_ROOT',
                              td / 'state' / 'alert-silenced'),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              td / 'state' / 'beacon-alerts-offset.txt'),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _only_record(self) -> dict:
        return json.loads(larry_alerts.ALERTS_FILE.read_text().strip())


class NeedsLarryAppendAlertTest(_IsolatedQueueTest):
    def test_default_append_omits_needs_larry(self):
        # Default False → absent from the record (lean queue; absent == False
        # downstream). This is the ~150-healer-alert case that must NOT surface.
        larry_alerts.append_alert(
            source='heal-x', severity='warning', message='drift', subject='k')
        self.assertNotIn('needs_larry', self._only_record())

    def test_needs_larry_true_is_stamped(self):
        larry_alerts.append_alert(
            source='beacon', severity='critical', message='decide',
            subject='pr-42', needs_larry=True)
        self.assertIs(self._only_record().get('needs_larry'), True)


class NeedsLarryForLarrySignalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / 'for-larry.json'

    def test_default_normalized_to_false(self):
        for_larry_signal.upsert_record('k1', {'headline': 'h'}, path=self.path)
        rec = for_larry_signal.load_records(self.path)['k1']
        self.assertIn('needs_larry', rec)
        self.assertIs(rec['needs_larry'], False)

    def test_true_preserved(self):
        for_larry_signal.upsert_record(
            'k2', {'headline': 'h', 'needs_larry': True}, path=self.path)
        rec = for_larry_signal.load_records(self.path)['k2']
        self.assertIs(rec['needs_larry'], True)

    def test_truthy_coerced_to_explicit_bool(self):
        for_larry_signal.upsert_record(
            'k3', {'needs_larry': 1}, path=self.path)
        rec = for_larry_signal.load_records(self.path)['k3']
        self.assertIs(rec['needs_larry'], True)


# --------------------------------------------------------------------------
# §3a.2 — the retraction gap. _retract_shipped_alert_events correlates each
# removed line to the exact task_id the shipper stamped and clears ONLY the
# larry_alert row.
# --------------------------------------------------------------------------
class RetractShippedEventsUnitTest(unittest.TestCase):
    def _recorder(self):
        calls = []

        def clear_fn(key, *, event_types):
            calls.append((key, tuple(event_types)))
            return 1
        return calls, clear_fn

    def test_empty_is_noop(self):
        calls, clear_fn = self._recorder()
        self.assertEqual(
            larry_alerts._retract_shipped_alert_events([], clear_fn=clear_fn), 0)
        self.assertEqual(calls, [])

    def test_task_id_wins_over_subject(self):
        # The shipper keys task_id = rec['task_id'] or subject; a record with an
        # explicit task_id must clear THAT key, not the subject.
        calls, clear_fn = self._recorder()
        larry_alerts._retract_shipped_alert_events(
            [{'task_id': 'tid-1', 'subject': 'subj-1'}], clear_fn=clear_fn)
        self.assertEqual(calls, [('tid-1', ('larry_alert',))])

    def test_subject_used_when_no_task_id(self):
        calls, clear_fn = self._recorder()
        larry_alerts._retract_shipped_alert_events(
            [{'subject': 'install-drift:x.timer'}], clear_fn=clear_fn)
        self.assertEqual(calls, [('install-drift:x.timer', ('larry_alert',))])

    def test_decision_key_fallback_when_shipper_had_no_task_id(self):
        # No task_id/subject/intent → the shipper stamped task_id=None and only
        # payload.decision_key survives; clear by that so the row is still hit.
        calls, clear_fn = self._recorder()
        larry_alerts._retract_shipped_alert_events(
            [{'decision_key': 'pr-ourliberty-9'}], clear_fn=clear_fn)
        self.assertEqual(calls, [('pr-ourliberty-9', ('larry_alert',))])

    def test_dedup_collapses_shared_subject(self):
        calls, clear_fn = self._recorder()
        larry_alerts._retract_shipped_alert_events(
            [{'subject': 'same'}, {'subject': 'same'}], clear_fn=clear_fn)
        self.assertEqual(calls, [('same', ('larry_alert',))])

    def test_clear_exception_is_swallowed(self):
        def boom(key, *, event_types):
            raise RuntimeError('supabase down')
        # Never propagates into the resolve caller.
        self.assertEqual(
            larry_alerts._retract_shipped_alert_events(
                [{'subject': 'x'}], clear_fn=boom), 0)


class ResolveAlertRetractionIntegrationTest(unittest.TestCase):
    """resolve_alert removes the stale line AND clears its shipped row — the
    end-to-end wiring that closes the gap (§3a.4 test 1: an auto-resolved alert
    must not linger with read_at=NULL)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        td = Path(self._tmp.name)
        self.alerts = td / 'larry-alerts.jsonl'
        self.beacon = td / 'beacon-alerts-offset.txt'
        self.medic = td / 'medic-alerts-offset.txt'

    def _line(self, source, subject, **extra):
        rec = {'source': source, 'subject': subject, 'route': 'escalate',
               'severity': 'critical', 'message': 'm'}
        rec.update(extra)
        return json.dumps(rec) + '\n'

    def test_resolve_clears_shipped_larry_alert_row(self):
        self.alerts.write_text(''.join([
            self._line('s', 'a'),
            self._line('heal-systemd-install-drift', 'install-drift:x.timer',
                       suggested_action='sudo systemctl restart x'),
        ]))
        calls = []

        def rec_clear(key, *, event_types=('larry_alert',), **kw):
            calls.append((key, tuple(event_types)))
            return 1
        with mock.patch.object(cee, 'clear_decision', rec_clear):
            removed = larry_alerts.resolve_alert(
                'heal-systemd-install-drift:install-drift:x.timer',
                consumer_offset_files=[self.beacon, self.medic],
                alerts_file=self.alerts)
        self.assertEqual(removed, 1)
        # The stale line is gone from the queue...
        self.assertNotIn('x.timer', self.alerts.read_text())
        # ...and its shipped chain_event row was cleared by the shipper's key
        # (subject, since the record carried no explicit task_id), larry_alert-
        # scoped so a same-keyed pending approval/escalation is never touched.
        self.assertEqual(calls, [('install-drift:x.timer', ('larry_alert',))])

    def test_no_match_issues_no_clear(self):
        self.alerts.write_text(self._line('s', 'a'))
        calls = []
        with mock.patch.object(
                cee, 'clear_decision',
                lambda *a, **k: calls.append(a) or 0):
            larry_alerts.resolve_alert(
                'nope:nothing',
                consumer_offset_files=[self.beacon, self.medic],
                alerts_file=self.alerts)
        self.assertEqual(calls, [])


class _ClearResp:
    def __init__(self, count):
        self.data = []
        self.count = count


class _FakeClearClient:
    """Captures clear_decision's fluent read_at UPDATE, incl. the .in_ filter
    (which the module-level _ClearClient in test_chain_event_emit omits)."""

    def __init__(self, task_id_count=1, decision_key_count=0):
        self.executed = []
        self._counts = {'task_id': task_id_count,
                        'decision_key': decision_key_count}
        self._cur = {}

    def table(self, name):
        self._cur = {'table': name, 'eq': {}, 'in': {}, 'is': []}
        return self

    def update(self, values, **kw):
        self._cur['update'] = values
        self._cur['count_kw'] = kw.get('count')
        return self

    def in_(self, col, vals):
        self._cur['in'][col] = list(vals)
        return self

    def is_(self, col, val):
        self._cur['is'].append((col, val))
        return self

    def eq(self, col, val):  # noqa: A003 — mirrors supabase-py builder
        self._cur['eq'][col] = val
        return self

    def execute(self):
        leg = 'decision_key' if 'payload->>decision_key' in self._cur['eq'] \
            else 'task_id'
        snap = dict(self._cur)
        snap['_leg'] = leg
        self.executed.append(snap)
        return _ClearResp(self._counts.get(leg, 0))


class ClearDecisionLarryAlertScopeTest(unittest.TestCase):
    """Proves the retraction actually sets read_at, scoped to larry_alert +
    read_at IS NULL — i.e. the shipped row stops reading as live."""

    def setUp(self):
        cee.reset_client_for_testing()
        self.addCleanup(cee.reset_client_for_testing)
        self._log = logging.getLogger('test-clear-scope')
        self._log.setLevel(logging.CRITICAL)

    def test_sets_read_at_scoped_to_larry_alert(self):
        client = _FakeClearClient(task_id_count=1)
        total = cee.clear_decision(
            'install-drift:x.timer', event_types=('larry_alert',),
            ts='2026-07-01T00:00:00+00:00', client=client, logger=self._log)
        self.assertEqual(total, 1)
        # Two legs run (decision_key column + task_id column); both set read_at,
        # scope to larry_alert, and guard on read_at IS NULL (idempotent).
        self.assertTrue(client.executed)
        for snap in client.executed:
            self.assertEqual(snap['update'], {'read_at': '2026-07-01T00:00:00+00:00'})
            self.assertEqual(snap['in'].get('event_type'), ['larry_alert'])
            self.assertIn(('read_at', 'null'), snap['is'])
        # The task_id leg matched on the shipper's key.
        task_leg = [s for s in client.executed if s['_leg'] == 'task_id'][0]
        self.assertEqual(task_leg['eq'].get('task_id'), 'install-drift:x.timer')


if __name__ == '__main__':
    unittest.main()
