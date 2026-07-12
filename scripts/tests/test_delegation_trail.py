#!/usr/bin/env python3
"""Delegate-tracking Slice 2b — the build/review trail on a delegated card.

The build spawned by a delegation runs under a FRESH task_id; its REVIEW
chain_events carry `payload.origin_task_id == delegate-<cid>` (Slice 2a).
`_fetch_delegation_build_events` joins them by origin; `_delegation_build_phase`
derives a verdict-aware phase (in_review | review_passed); `_delegation_trail_field`
surfaces it on the parked card. `derive_phase_for_task` is deliberately NOT reused
(it reads session_start / marker_emit / auto_merge — none origin-tagged here).

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_delegation_trail
"""

from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api as da  # noqa: E402

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
DELEGATE_ID = 'delegate-cap-fix-the-thing-ab12'


def _ev(event_type, *, pr_url=None, ts='2026-07-11T12:00:00Z'):
    return {'event_type': event_type, 'pr_url': pr_url, 'ts': ts,
            'payload': {'origin_task_id': DELEGATE_ID}}


# ---------- deriver ----------


class DelegationBuildPhaseTest(unittest.TestCase):
    def test_review_pass_wins(self):
        events = [_ev('review_pass'), _ev('review_request')]
        self.assertEqual(da._delegation_build_phase(events), 'review_passed')

    def test_review_request_only_is_in_review(self):
        self.assertEqual(
            da._delegation_build_phase([_ev('review_request')]), 'in_review')

    def test_revision_and_escalate_are_in_review(self):
        self.assertEqual(
            da._delegation_build_phase([_ev('review_revision')]), 'in_review')
        self.assertEqual(
            da._delegation_build_phase([_ev('review_escalate')]), 'in_review')

    def test_no_events_is_none(self):
        self.assertIsNone(da._delegation_build_phase([]))

    def test_unrelated_events_is_none(self):
        # session_start / auto_merge are NOT part of the origin-joined trail
        self.assertIsNone(da._delegation_build_phase([_ev('session_start')]))


class DelegationBuildPrUrlTest(unittest.TestCase):
    def test_first_nonempty_wins(self):
        events = [_ev('review_pass', pr_url='https://github.com/o/r/pull/7'),
                  _ev('review_request', pr_url='https://github.com/o/r/pull/9')]
        self.assertEqual(da._delegation_build_pr_url(events),
                         'https://github.com/o/r/pull/7')

    def test_none_when_absent(self):
        self.assertIsNone(da._delegation_build_pr_url([_ev('review_request')]))


# ---------- field + integration ----------


def _delegate_cap():
    return {'id': 'cap-fix-the-thing-ab12', 'state': 'parked',
            'title': 'Fix the thing',
            'spawned': {'kind': 'delegate', 'task_id': DELEGATE_ID}}


class DelegationTrailFieldTest(unittest.TestCase):
    def test_delegate_card_with_trail(self):
        m = {DELEGATE_ID: [_ev('review_pass',
                               pr_url='https://github.com/o/r/pull/7')]}
        got = da._delegation_trail_field(_delegate_cap(), m)
        self.assertEqual(got['delegation_build_phase'], 'review_passed')
        self.assertEqual(got['delegation_pr_url'],
                         'https://github.com/o/r/pull/7')

    def test_non_delegate_card_is_neutral(self):
        cap = {'id': 'c', 'state': 'parked',
               'spawned': {'kind': 'orphan', 'task_id': 't-1'}}
        got = da._delegation_trail_field(cap, {'t-1': [_ev('review_pass')]})
        self.assertIsNone(got['delegation_build_phase'])
        self.assertIsNone(got['delegation_pr_url'])

    def test_no_join_is_neutral(self):
        got = da._delegation_trail_field(_delegate_cap(), {})
        self.assertIsNone(got['delegation_build_phase'])
        self.assertIsNone(got['delegation_pr_url'])

    def test_merged_pr_flips_review_passed_to_merged(self):
        pr = 'https://github.com/o/r/pull/7'
        m = {DELEGATE_ID: [_ev('review_pass', pr_url=pr)]}
        got = da._delegation_trail_field(_delegate_cap(), m, {pr: 'MERGED'})
        self.assertEqual(got['delegation_build_phase'], 'merged')

    def test_open_pr_stays_review_passed(self):
        pr = 'https://github.com/o/r/pull/7'
        m = {DELEGATE_ID: [_ev('review_pass', pr_url=pr)]}
        got = da._delegation_trail_field(_delegate_cap(), m, {pr: 'OPEN'})
        self.assertEqual(got['delegation_build_phase'], 'review_passed')

    def test_parked_integration_surfaces_trail(self):
        m = {DELEGATE_ID: [_ev('review_request')]}
        parked = da._parked_from_captures(
            [_delegate_cap()], NOW, {}, None, build_events_by_origin=m)
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]['delegation_build_phase'], 'in_review')

    def test_parked_neutral_without_map(self):
        parked = da._parked_from_captures([_delegate_cap()], NOW, {})
        self.assertIsNone(parked[0]['delegation_build_phase'])
        self.assertIsNone(parked[0]['delegation_pr_url'])


# ---------- fetch join ----------


class _StubTable:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    def select(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        self._calls.append((col, list(vals)))
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        return type('R', (), {'data': self._rows})()


class _StubClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list = []

    def table(self, _name):
        return _StubTable(self._rows, self.calls)


class FetchDelegationBuildEventsTest(unittest.TestCase):
    def test_keys_by_origin_and_filters_on_payload(self):
        rows = [
            {'event_type': 'review_pass', 'task_id': 'fix-thing-001',
             'pr_url': 'https://github.com/o/r/pull/7', 'ts': '2026-07-11T12:00:00Z',
             'payload': {'origin_task_id': DELEGATE_ID}},
            {'event_type': 'review_request', 'task_id': 'fix-thing-001',
             'ts': '2026-07-11T11:00:00Z',
             'payload': {'origin_task_id': DELEGATE_ID}},
        ]
        client = _StubClient(rows)
        out = da._fetch_delegation_build_events(client, [DELEGATE_ID])
        # keyed by origin_task_id, not the fresh task_id
        self.assertIn(DELEGATE_ID, out)
        self.assertEqual(len(out[DELEGATE_ID]), 2)
        # queried on the JSON payload field, newest-first ordering applied
        self.assertEqual(client.calls, [('payload->>origin_task_id',
                                         [DELEGATE_ID])])
        self.assertEqual(out[DELEGATE_ID][0]['event_type'], 'review_pass')

    def test_row_without_origin_is_skipped(self):
        rows = [{'event_type': 'review_pass', 'task_id': 't', 'ts': 'x',
                 'payload': {}}]
        out = da._fetch_delegation_build_events(_StubClient(rows), [DELEGATE_ID])
        self.assertEqual(out, {})

    def test_no_client_or_ids_degrades_empty(self):
        self.assertEqual(da._fetch_delegation_build_events(None, [DELEGATE_ID]), {})
        self.assertEqual(da._fetch_delegation_build_events(_StubClient([]), []), {})


if __name__ == '__main__':
    unittest.main()
