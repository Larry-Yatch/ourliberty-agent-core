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


# ---------- discuss trail (approval-card-build-trail Phase 2) ----------


def _discuss_ev(event_type, cid, *, pr_url=None, ts='2026-07-11T12:00:00Z'):
    """A build event spawned by 'Talk with the team' on card <cid>: origin-tagged
    card-message-<cid>, with NO delegate ref on the card."""
    return {'event_type': event_type, 'pr_url': pr_url, 'ts': ts,
            'payload': {'origin_task_id': f'card-message-{cid}'}}


class DiscussTrailFieldTest(unittest.TestCase):
    def test_discussed_card_with_no_spawned_ref_shows_trail(self):
        cid = 'cap-talked-about-ab12'
        cap = {'id': cid, 'state': 'parked', 'title': 'Talked about'}  # no spawned
        m = {f'card-message-{cid}': [
            _discuss_ev('review_pass', cid, pr_url='https://github.com/o/r/pull/9')]}
        got = da._delegation_trail_field(cap, m)
        self.assertEqual(got['delegation_build_phase'], 'review_passed')
        self.assertEqual(got['delegation_pr_url'], 'https://github.com/o/r/pull/9')

    def test_discussed_card_in_review(self):
        cid = 'cap-x'
        cap = {'id': cid, 'state': 'parked'}
        got = da._delegation_trail_field(
            cap, {f'card-message-{cid}': [_discuss_ev('review_request', cid)]})
        self.assertEqual(got['delegation_build_phase'], 'in_review')

    def test_discussed_merged_flip(self):
        cid = 'cap-x'
        pr = 'https://github.com/o/r/pull/9'
        cap = {'id': cid, 'state': 'parked'}
        got = da._delegation_trail_field(
            cap, {f'card-message-{cid}': [_discuss_ev('review_pass', cid, pr_url=pr)]},
            {pr: 'MERGED'})
        self.assertEqual(got['delegation_build_phase'], 'merged')

    def test_delegated_and_discussed_pick_most_advanced_COHERENTLY(self):
        # Same card delegated (in_review, its OWN PR-A) AND discussed
        # (review_pass, PR-B). The most-advanced wins — AND phase+pr_url must
        # come from the SAME build: review_passed → PR-B (not the newer PR-A).
        cid = 'cap-fix-the-thing-ab12'
        pr_a = 'https://github.com/o/r/pull/1'   # delegate build, in review
        pr_b = 'https://github.com/o/r/pull/9'   # discuss build, passed
        cap = _delegate_cap()  # id == cid, spawned delegate ref
        m = {
            # delegate build's review_request is NEWER than discuss's review_pass
            DELEGATE_ID: [_ev('review_request', pr_url=pr_a,
                              ts='2026-07-11T13:00:00Z')],
            f'card-message-{cid}': [
                _discuss_ev('review_pass', cid, pr_url=pr_b,
                            ts='2026-07-11T12:00:00Z')],
        }
        got = da._delegation_trail_field(cap, m)
        self.assertEqual(got['delegation_build_phase'], 'review_passed')
        self.assertEqual(got['delegation_pr_url'], pr_b)  # coherent, not pr_a

    def test_both_doors_merged_flip_checks_the_passing_build_pr(self):
        cid = 'cap-fix-the-thing-ab12'
        pr_a = 'https://github.com/o/r/pull/1'
        pr_b = 'https://github.com/o/r/pull/9'
        cap = _delegate_cap()
        m = {
            DELEGATE_ID: [_ev('review_request', pr_url=pr_a,
                              ts='2026-07-11T13:00:00Z')],
            f'card-message-{cid}': [
                _discuss_ev('review_pass', cid, pr_url=pr_b,
                            ts='2026-07-11T12:00:00Z')],
        }
        # PR-B (the passing build) merged → merged; PR-A merging must NOT flip.
        self.assertEqual(
            da._delegation_trail_field(cap, m, {pr_b: 'MERGED'})['delegation_build_phase'],
            'merged')
        self.assertEqual(
            da._delegation_trail_field(cap, m, {pr_a: 'MERGED'})['delegation_build_phase'],
            'review_passed')

    def test_both_doors_same_phase_newest_wins(self):
        # Both in_review → tie broken by newest event's origin. Uses explicit
        # +00:00 offsets (not `Z`) so the recency compare is version-independent
        # (datetime.fromisoformat only parses a `Z` suffix on Python ≥3.11; the
        # droplet runs 3.12 but the local/CI runner may be older).
        cid = 'cap-fix-the-thing-ab12'
        cap = _delegate_cap()
        m = {
            DELEGATE_ID: [_ev('review_request', pr_url='https://github.com/o/r/pull/1',
                              ts='2026-07-11T11:00:00+00:00')],
            f'card-message-{cid}': [
                _discuss_ev('review_request', cid,
                            pr_url='https://github.com/o/r/pull/9',
                            ts='2026-07-11T13:00:00+00:00')],
        }
        got = da._delegation_trail_field(cap, m)
        self.assertEqual(got['delegation_build_phase'], 'in_review')
        self.assertEqual(got['delegation_pr_url'], 'https://github.com/o/r/pull/9')

    def test_no_id_is_neutral(self):
        got = da._delegation_trail_field(
            {'state': 'parked'}, {'card-message-x': [_discuss_ev('review_pass', 'x')]})
        self.assertIsNone(got['delegation_build_phase'])

    def test_discussed_integration_via_parked(self):
        cid = 'cap-talked-ab12'
        cap = {'id': cid, 'state': 'parked', 'title': 'T'}
        m = {f'card-message-{cid}': [_discuss_ev('review_request', cid)]}
        parked = da._parked_from_captures(
            [cap], NOW, {}, None, build_events_by_origin=m)
        self.assertEqual(parked[0]['delegation_build_phase'], 'in_review')


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

    def test_large_origin_list_is_chunked_not_one_oversized_query(self):
        # Finding 1: a big parked backlog must NOT build one over-length URL.
        # 250 origins → 3 bounded `.in_` queries (100, 100, 50), all resolving.
        n = 250
        ids = [f'card-message-cap-{i}' for i in range(n)]
        client = _StubClient([])
        da._fetch_delegation_build_events(client, ids)
        chunk = da._DELEGATION_ORIGIN_CHUNK
        self.assertEqual(len(client.calls), 3)
        self.assertEqual([len(c[1]) for c in client.calls], [chunk, chunk, n - 2 * chunk])
        # union of the chunks covers every id exactly once
        seen = [v for c in client.calls for v in c[1]]
        self.assertEqual(sorted(seen), sorted(ids))

    def test_one_failing_chunk_does_not_drop_the_others(self):
        # A chunk that raises drops only its own ids; the rest still resolve.
        class _FlakyTable:
            def __init__(self, rows, calls):
                self._rows, self._calls = rows, calls

            def select(self, *_a, **_k):
                return self

            def in_(self, _col, vals):
                self._calls.append(list(vals))
                return self

            def order(self, *_a, **_k):
                return self

            def execute(self):
                # fail the FIRST chunk only
                if len(self._calls) == 1:
                    raise RuntimeError('supabase hiccup')
                return type('R', (), {'data': self._rows})()

        class _FlakyClient:
            def __init__(self, rows):
                self._rows, self.calls = rows, []

            def table(self, _n):
                return _FlakyTable(self._rows, self.calls)

        ids = [f'card-message-cap-{i}' for i in range(da._DELEGATION_ORIGIN_CHUNK + 5)]
        good = {'event_type': 'review_pass', 'task_id': 't', 'ts': 'x',
                'pr_url': None, 'payload': {'origin_task_id': ids[-1]}}
        out = da._fetch_delegation_build_events(_FlakyClient([good]), ids)
        # first chunk raised (its ids dropped); second chunk's event survives
        self.assertIn(ids[-1], out)


if __name__ == '__main__':
    unittest.main()
