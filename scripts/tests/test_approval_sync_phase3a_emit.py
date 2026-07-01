#!/usr/bin/env python3
"""Tests for approval-sync Phase 3a PR-2 — the two projected emit sites.

Covers §3a.1's `parked_capture` + `sequence_needs_you` projection:
  * chain_event_emit.reconcile_open_events (the shared delta primitive) +
    list_open_event_task_ids + clear_event_by_task_id
  * build_sequence_advancer._reconcile_sequence_needs_you / _needs_you_task_id
  * heal_missions_card_gc.project_parked_captures

Run:
    python3 -m unittest scripts.tests.test_approval_sync_phase3a_emit
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import chain_event_emit as cee  # noqa: E402


class _Resp:
    def __init__(self, data, count):
        self.data = data
        self.count = count


class _FakeReconcileClient:
    """Fake supabase client covering the three verbs reconcile_open_events uses:
    select (list open), upsert (emit), update (clear)."""

    def __init__(self, *, open_task_ids=(), select_raises=False):
        self.open = list(open_task_ids)
        self.select_raises = select_raises
        self.upserts = []          # list of row-lists emitted
        self.cleared = []          # (event_type, task_id) pairs
        self._mode = None
        self._cur = {}

    def table(self, name):
        self._cur = {'table': name, 'eq': {}, 'is': []}
        self._mode = None
        return self

    def select(self, cols):
        self._mode = 'select'
        return self

    def update(self, values, **kw):
        self._mode = 'update'
        self._cur['update'] = values
        return self

    def upsert(self, rows, **kw):
        self._mode = 'upsert'
        self._cur['rows'] = rows
        return self

    def eq(self, col, val):  # noqa: A003 — mirrors supabase-py
        self._cur['eq'][col] = val
        return self

    def is_(self, col, val):
        self._cur['is'].append((col, val))
        return self

    def execute(self):
        if self._mode == 'select':
            if self.select_raises:
                raise RuntimeError('supabase select down')
            data = [{'task_id': t} for t in self.open]
            return _Resp(data, len(data))
        if self._mode == 'upsert':
            self.upserts.append(self._cur['rows'])
            return _Resp([], 0)
        if self._mode == 'update':
            self.cleared.append((self._cur['eq'].get('event_type'),
                                 self._cur['eq'].get('task_id')))
            return _Resp([{'event_id': 'x'}], 1)
        return _Resp([], 0)


class ReconcileOpenEventsTest(unittest.TestCase):
    def _desired(self, *tids):
        return {t: {'ts': '2026-07-01T00:00:00+00:00',
                    'payload': {'k': t}} for t in tids}

    def test_emits_new_clears_gone_skips_still_open(self):
        # open={A,B}; desired={B,C} → emit C, clear A, leave B.
        client = _FakeReconcileClient(open_task_ids=['seq-A', 'seq-B'])
        summary = cee.reconcile_open_events(
            'sequence_needs_you', self._desired('seq-B', 'seq-C'),
            agent='build_sequence_advancer', client=client)
        self.assertEqual(summary, {'emitted': 1, 'cleared': 1, 'skipped': False})
        # Emitted exactly seq-C...
        emitted_task_ids = [r['task_id'] for rows in client.upserts for r in rows]
        self.assertEqual(emitted_task_ids, ['seq-C'])
        self.assertEqual([r['event_type'] for rows in client.upserts for r in rows],
                         ['sequence_needs_you'])
        # ...and cleared exactly seq-A, scoped to the event_type.
        self.assertEqual(client.cleared, [('sequence_needs_you', 'seq-A')])

    def test_steady_state_no_writes(self):
        # desired == open → zero emits, zero clears (idempotent).
        client = _FakeReconcileClient(open_task_ids=['seq-A'])
        summary = cee.reconcile_open_events(
            'sequence_needs_you', self._desired('seq-A'),
            agent='x', client=client)
        self.assertEqual(summary, {'emitted': 0, 'cleared': 0, 'skipped': False})
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.cleared, [])

    def test_no_client_skips(self):
        summary = cee.reconcile_open_events(
            'parked_capture', self._desired('capture-1'), agent='x', client=None)
        self.assertTrue(summary['skipped'])

    def test_select_failure_skips_no_blind_emit(self):
        # If the open set can't be read, DO NOT emit (would duplicate rows).
        client = _FakeReconcileClient(select_raises=True)
        summary = cee.reconcile_open_events(
            'parked_capture', self._desired('capture-1'), agent='x',
            client=client)
        self.assertTrue(summary['skipped'])
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.cleared, [])

    def test_stable_ts_flows_to_emitted_row(self):
        client = _FakeReconcileClient(open_task_ids=[])
        cee.reconcile_open_events(
            'parked_capture',
            {'capture-9': {'ts': '2026-06-30T12:00:00+00:00',
                           'payload': {'capture_id': '9'}}},
            agent='heal_missions_card_gc', client=client)
        row = client.upserts[0][0]
        self.assertEqual(row['ts'], '2026-06-30T12:00:00+00:00')


class ClearAndListPrimitivesTest(unittest.TestCase):
    def test_clear_event_by_task_id_single_scoped_update(self):
        client = _FakeReconcileClient()
        n = cee.clear_event_by_task_id(
            'seq-7', event_type='sequence_needs_you',
            ts='2026-07-01T00:00:00+00:00', client=client)
        self.assertEqual(n, 1)
        self.assertEqual(client.cleared, [('sequence_needs_you', 'seq-7')])
        self.assertEqual(client._cur['update'], {'read_at': '2026-07-01T00:00:00+00:00'})
        self.assertIn(('read_at', 'null'), client._cur['is'])

    def test_clear_empty_args_noop(self):
        client = _FakeReconcileClient()
        self.assertEqual(cee.clear_event_by_task_id('', event_type='x',
                                                    client=client), 0)
        self.assertEqual(cee.clear_event_by_task_id('t', event_type='',
                                                    client=client), 0)
        self.assertEqual(client.cleared, [])

    def test_list_open_returns_set(self):
        client = _FakeReconcileClient(open_task_ids=['a', 'b'])
        self.assertEqual(
            cee.list_open_event_task_ids('parked_capture', client=client),
            {'a', 'b'})

    def test_list_open_none_on_error(self):
        client = _FakeReconcileClient(select_raises=True)
        self.assertIsNone(
            cee.list_open_event_task_ids('parked_capture', client=client))


class SequenceNeedsYouProjectionTest(unittest.TestCase):
    def setUp(self):
        import build_sequence_advancer as bsa
        self.bsa = bsa

    def test_task_id_paused_vs_stuck_step(self):
        self.assertEqual(
            self.bsa._needs_you_task_id({'seq_id': 'launch-x', 'step_id': None}),
            'seq-launch-x')
        self.assertEqual(
            self.bsa._needs_you_task_id({'seq_id': 'launch-x', 'step_id': 's2'}),
            'seq-launch-x-step-s2')
        self.assertIsNone(self.bsa._needs_you_task_id({'seq_id': None}))

    def test_builds_desired_and_reconciles_as_steer(self):
        waiting = [
            {'seq_id': 'sq1', 'step_id': None, 'why': 'paused',
             'actions': ['resume', 'cancel'], '_ts': '2026-06-30T01:00:00+00:00'},
            {'seq_id': 'sq2', 'step_id': 'build', 'why': 'stuck',
             'actions': ['skip', 'cancel'], '_ts': '2026-06-30T02:00:00+00:00'},
        ]
        captured = {}

        def fake_reconcile(event_type, desired, *, agent, **kw):
            captured['event_type'] = event_type
            captured['desired'] = desired
            captured['agent'] = agent
            return {'emitted': 2, 'cleared': 0, 'skipped': False}

        logger = mock.Mock()
        with mock.patch('system_state_log.load_waiting_sequences',
                        return_value=waiting), \
             mock.patch('chain_event_emit.reconcile_open_events', fake_reconcile):
            self.bsa._reconcile_sequence_needs_you(
                datetime(2026, 7, 1, tzinfo=timezone.utc), logger)
        self.assertEqual(captured['event_type'], 'sequence_needs_you')
        self.assertEqual(captured['agent'], 'build_sequence_advancer')
        d = captured['desired']
        self.assertEqual(set(d), {'seq-sq1', 'seq-sq2-step-build'})
        # Stable ts carried from the waiting item; classified as reversible steer.
        self.assertEqual(d['seq-sq1']['ts'], '2026-06-30T01:00:00+00:00')
        self.assertEqual(d['seq-sq1']['payload']['lane'], 'steer')
        self.assertIs(d['seq-sq1']['payload']['needs_larry'], False)
        self.assertEqual(d['seq-sq2-step-build']['payload']['step_id'], 'build')

    def test_load_failure_is_swallowed(self):
        logger = mock.Mock()
        with mock.patch('system_state_log.load_waiting_sequences',
                        side_effect=RuntimeError('read boom')), \
             mock.patch('chain_event_emit.reconcile_open_events') as rec:
            self.bsa._reconcile_sequence_needs_you(
                datetime(2026, 7, 1, tzinfo=timezone.utc), logger)
        rec.assert_not_called()  # never reconcile against an unknown set


class ParkedCaptureProjectionTest(unittest.TestCase):
    def setUp(self):
        import heal_missions_card_gc as gc
        self.gc = gc

    def _registry(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def test_builds_desired_from_parked_only(self):
        reg = self._registry(
            {'id': 'cap-a', 'state': 'parked', 'title': 'A',
             'last_touched': '2026-06-30T00:00:00+00:00'},
            {'id': 'cap-b', 'state': 'promoted', 'title': 'B'},   # not parked
            {'id': '', 'state': 'parked'},                        # no id
            {'state': 'parked'},                                  # no id key
        )
        captured = {}

        def fake_reconcile(event_type, desired, *, agent, **kw):
            captured['event_type'] = event_type
            captured['desired'] = desired
            captured['agent'] = agent
            return {'emitted': 1, 'cleared': 0, 'skipped': False}

        self.gc.project_parked_captures(
            reg, datetime(2026, 7, 1, tzinfo=timezone.utc),
            reconcile_fn=fake_reconcile)
        self.assertEqual(captured['event_type'], 'parked_capture')
        self.assertEqual(captured['agent'], 'heal_missions_card_gc')
        d = captured['desired']
        self.assertEqual(set(d), {'capture-cap-a'})   # only the valid parked one
        self.assertEqual(d['capture-cap-a']['ts'], '2026-06-30T00:00:00+00:00')
        self.assertEqual(d['capture-cap-a']['payload']['lane'], 'parked')
        self.assertIs(d['capture-cap-a']['payload']['needs_larry'], False)

    def test_ts_falls_back_to_origin_captured_at(self):
        reg = self._registry(
            {'id': 'cap-c', 'state': 'parked',
             'origin': {'captured_at': '2026-06-29T09:00:00+00:00'}})
        captured = {}
        self.gc.project_parked_captures(
            reg, datetime(2026, 7, 1, tzinfo=timezone.utc),
            reconcile_fn=lambda et, d, *, agent, **kw: captured.update(d) or
            {'emitted': 1, 'cleared': 0, 'skipped': False})
        self.assertEqual(captured['capture-cap-c']['ts'],
                         '2026-06-29T09:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
