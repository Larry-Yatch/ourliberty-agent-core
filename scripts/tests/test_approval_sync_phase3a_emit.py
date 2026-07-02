#!/usr/bin/env python3
"""Tests for approval-sync Phase 3a PR-2 — the projected emit sites.

Covers §3a.1's `sequence_needs_you` projection + the shared delta primitive:
  * chain_event_emit.reconcile_open_events (the shared delta primitive) +
    list_open_event_task_ids + clear_event_by_task_id
  * build_sequence_advancer._reconcile_sequence_needs_you / _needs_you_task_id

The sibling `parked_capture` projection was RETIRED 2026-07-01 (no consumer;
see scripts/retire_parked_capture_rows.py + its test) — its
heal_missions_card_gc.project_parked_captures test lived here and was removed.

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
from _fake_reconcile_client import (  # noqa: E402
    FakeReconcileClient as _FakeReconcileClient,
)


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
            'sequence_needs_you', self._desired('seq-1'), agent='x', client=None)
        self.assertTrue(summary['skipped'])

    def test_select_failure_skips_no_blind_emit(self):
        # If the open set can't be read, DO NOT emit (would duplicate rows).
        client = _FakeReconcileClient(select_raises=True)
        summary = cee.reconcile_open_events(
            'sequence_needs_you', self._desired('seq-1'), agent='x',
            client=client)
        self.assertTrue(summary['skipped'])
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.cleared, [])

    def test_empty_desired_clears_all_open(self):
        # The retirement path: an EMPTY desired set clears every open row and
        # emits nothing (scripts/retire_parked_capture_rows.py drives this).
        client = _FakeReconcileClient(open_task_ids=['seq-A', 'seq-B'])
        summary = cee.reconcile_open_events(
            'sequence_needs_you', {}, agent='retire', client=client)
        self.assertEqual(summary, {'emitted': 0, 'cleared': 2, 'skipped': False})
        self.assertEqual(client.upserts, [])
        self.assertEqual(sorted(client.cleared),
                         [('sequence_needs_you', 'seq-A'),
                          ('sequence_needs_you', 'seq-B')])

    def test_stable_ts_flows_to_emitted_row(self):
        client = _FakeReconcileClient(open_task_ids=[])
        cee.reconcile_open_events(
            'sequence_needs_you',
            {'seq-9': {'ts': '2026-06-30T12:00:00+00:00',
                       'payload': {'seq_id': '9'}}},
            agent='build_sequence_advancer', client=client)
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
            cee.list_open_event_task_ids('sequence_needs_you', client=client),
            {'a', 'b'})

    def test_list_open_none_on_error(self):
        client = _FakeReconcileClient(select_raises=True)
        self.assertIsNone(
            cee.list_open_event_task_ids('sequence_needs_you', client=client))

    def test_list_open_paginates_across_pages(self):
        # More rows than one page → the reader pages until a short page and
        # returns the UNION, never truncates at the cap.
        ids = [f'seq-{i}' for i in range(2500)]
        client = _FakeReconcileClient(open_task_ids=ids)
        with mock.patch.object(cee, '_OPEN_EVENT_PAGE', 1000), \
             mock.patch.object(cee, '_OPEN_EVENT_SCAN_CAP', 10000):
            got = cee.list_open_event_task_ids('sequence_needs_you',
                                               client=client)
        self.assertEqual(got, set(ids))

    def test_list_open_refuses_when_over_cap(self):
        # Open set at/over the cap → None (skip), never a silently truncated set
        # that would wrongly clear the rows it couldn't see.
        ids = [f'seq-{i}' for i in range(30)]
        client = _FakeReconcileClient(open_task_ids=ids)
        with mock.patch.object(cee, '_OPEN_EVENT_PAGE', 10), \
             mock.patch.object(cee, '_OPEN_EVENT_SCAN_CAP', 20):
            self.assertIsNone(
                cee.list_open_event_task_ids('sequence_needs_you', client=client))


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

    def test_read_failure_does_not_blind_clear_open_rows(self):
        # A transient read hiccup must NOT reach reconcile with an empty desired
        # set — that would clear every still-open sequence_needs_you row, and the
        # stable event_id makes the re-emit a no-op, permanently suppressing the
        # item. The advancer requests strict=True so the seam RAISES instead of
        # degrading to a soft-[] the reconcile would treat as "clear everything".
        logger = mock.Mock()
        with mock.patch('system_state_log.load_waiting_sequences',
                        side_effect=OSError('transient read')) as load, \
             mock.patch('chain_event_emit.reconcile_open_events') as rec:
            self.bsa._reconcile_sequence_needs_you(
                datetime(2026, 7, 1, tzinfo=timezone.utc), logger)
        # Wired strict so an unconfirmed-empty read signals rather than hides.
        self.assertTrue(load.call_args.kwargs.get('strict'))
        rec.assert_not_called()  # no reconcile → no blind clear of open rows


if __name__ == '__main__':
    unittest.main()
