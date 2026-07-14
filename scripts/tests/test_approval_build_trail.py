#!/usr/bin/env python3
"""Talk-with-the-team origin trail on Approvals-tab decision cards (Phase 1).

When Larry discusses an `approval_request` card ("Talk with the team") and that
conversation makes Beacon dispatch a build, the card-message door stamps the
build's approval + REVIEW chain_events with
`payload.origin_task_id == card-message-<event_id>`. `_approval_build_trails`
joins on that origin — reusing the delegate trail's `_fetch_delegation_build_events`
+ `_delegation_build_phase`/`_delegation_build_pr_url` verbatim — and re-keys the
result by the card's event_id so the Approvals tab can track the spawned build
through completion.

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_approval_build_trail
"""

from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

_TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', _TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EVENT_ID = 'evt-approve-the-thing-abc123'
ORIGIN = f'card-message-{EVENT_ID}'
PR = 'https://github.com/o/r/pull/7'


def _ev(event_type, *, pr_url=None, ts='2026-07-13T12:00:00Z', origin=ORIGIN):
    return {'event_type': event_type, 'pr_url': pr_url, 'ts': ts,
            'payload': {'origin_task_id': origin}}


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


class ApprovalBuildTrailsTest(unittest.TestCase):
    def test_joins_on_card_message_origin_and_rekeys_by_event_id(self):
        rows = [_ev('review_pass', pr_url=PR),
                _ev('review_request', ts='2026-07-13T11:00:00Z')]
        client = _StubClient(rows)
        out = da._approval_build_trails([EVENT_ID], client)
        # queried on the card-message origin, keyed back by the card event_id
        self.assertEqual(client.calls,
                         [('payload->>origin_task_id', [ORIGIN])])
        self.assertIn(EVENT_ID, out)
        self.assertEqual(out[EVENT_ID],
                         {'build_phase': 'review_passed', 'pr_url': PR})

    def test_in_review_when_only_review_activity(self):
        out = da._approval_build_trails(
            [EVENT_ID], _StubClient([_ev('review_request')]))
        self.assertEqual(out[EVENT_ID]['build_phase'], 'in_review')
        self.assertIsNone(out[EVENT_ID]['pr_url'])

    def test_card_with_no_build_is_omitted(self):
        # A card whose discussion spawned nothing has no origin-joined events.
        out = da._approval_build_trails([EVENT_ID], _StubClient([]))
        self.assertEqual(out, {})

    def test_unrelated_origin_does_not_leak_onto_card(self):
        rows = [_ev('review_pass', pr_url=PR, origin='card-message-someone-else')]
        out = da._approval_build_trails([EVENT_ID], _StubClient(rows))
        self.assertEqual(out, {})

    def test_merged_flip_via_pr_state(self):
        out = da._approval_build_trails(
            [EVENT_ID], _StubClient([_ev('review_pass', pr_url=PR)]),
            pr_state_resolver=lambda urls: {PR: 'MERGED'})
        self.assertEqual(out[EVENT_ID]['build_phase'], 'merged')

    def test_open_pr_stays_review_passed(self):
        out = da._approval_build_trails(
            [EVENT_ID], _StubClient([_ev('review_pass', pr_url=PR)]),
            pr_state_resolver=lambda urls: {PR: 'OPEN'})
        self.assertEqual(out[EVENT_ID]['build_phase'], 'review_passed')

    def test_pr_resolver_only_asked_about_review_passed(self):
        # in_review card (no PR) must not trigger a PR-state lookup.
        asked: list = []

        def resolver(urls):
            asked.append(list(urls))
            return {}

        da._approval_build_trails(
            [EVENT_ID], _StubClient([_ev('review_request')]),
            pr_state_resolver=resolver)
        self.assertEqual(asked, [])  # nothing review_passed → no lookup

    def test_pr_resolver_error_degrades_to_unflipped(self):
        def boom(urls):
            raise RuntimeError('gh down')

        out = da._approval_build_trails(
            [EVENT_ID], _StubClient([_ev('review_pass', pr_url=PR)]),
            pr_state_resolver=boom)
        self.assertEqual(out[EVENT_ID]['build_phase'], 'review_passed')

    def test_multiple_cards_partition_correctly(self):
        e2 = 'evt-second-card-def456'
        rows = [
            _ev('review_pass', pr_url=PR),
            _ev('review_request', origin=f'card-message-{e2}'),
        ]
        out = da._approval_build_trails([EVENT_ID, e2], _StubClient(rows))
        self.assertEqual(out[EVENT_ID]['build_phase'], 'review_passed')
        self.assertEqual(out[e2]['build_phase'], 'in_review')

    def test_empty_or_no_client_degrades_empty(self):
        self.assertEqual(da._approval_build_trails([], _StubClient([])), {})
        self.assertEqual(da._approval_build_trails([EVENT_ID], None), {})

    def test_non_string_ids_skipped(self):
        out = da._approval_build_trails(
            [None, '', 123, EVENT_ID], _StubClient([_ev('review_pass', pr_url=PR)]))  # type: ignore[list-item]
        self.assertEqual(list(out), [EVENT_ID])

    def test_id_cap_bounds_the_query(self):
        many = [f'evt-{i}' for i in range(da._MAX_APPROVAL_TRAIL_EVENT_IDS + 50)]
        client = _StubClient([])
        da._approval_build_trails(many, client)
        # only the cap's worth of origins reach the IN filter
        self.assertEqual(len(client.calls[0][1]),
                         da._MAX_APPROVAL_TRAIL_EVENT_IDS)


_AUTH = {'X-Dashboard-Token': _TOKEN}


class ApprovalBuildTrailsEndpointTest(unittest.TestCase):
    def setUp(self):
        self._orig = da._get_larry_action_supabase_client
        self.c = TestClient(da.app)

    def tearDown(self):
        da._get_larry_action_supabase_client = self._orig  # type: ignore[assignment]

    def test_requires_token(self):
        r = self.c.get('/api/approvals/build-trails',
                       params={'event_ids': EVENT_ID})
        self.assertIn(r.status_code, (401, 403))

    def test_returns_trails_for_talked_card(self):
        da._get_larry_action_supabase_client = lambda: _StubClient(  # type: ignore[assignment]
            [_ev('review_pass', pr_url=PR)])
        r = self.c.get('/api/approvals/build-trails',
                       headers=_AUTH, params={'event_ids': EVENT_ID})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['trails'][EVENT_ID]['build_phase'], 'review_passed')
        self.assertEqual(body['trails'][EVENT_ID]['pr_url'], PR)
        self.assertFalse(body.get('truncated', False))

    def test_comma_separated_ids_parsed(self):
        e2 = 'evt-second-card-def456'
        rows = [_ev('review_pass', pr_url=PR),
                _ev('review_request', origin=f'card-message-{e2}')]
        da._get_larry_action_supabase_client = lambda: _StubClient(rows)  # type: ignore[assignment]
        r = self.c.get('/api/approvals/build-trails', headers=_AUTH,
                       params={'event_ids': f'{EVENT_ID},{e2}'})
        self.assertEqual(r.status_code, 200)
        trails = r.json()['trails']
        self.assertEqual(trails[EVENT_ID]['build_phase'], 'review_passed')
        self.assertEqual(trails[e2]['build_phase'], 'in_review')

    def test_always_200_when_client_unavailable(self):
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        r = self.c.get('/api/approvals/build-trails',
                       headers=_AUTH, params={'event_ids': EVENT_ID})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['trails'], {})

    def test_empty_param_ok(self):
        da._get_larry_action_supabase_client = lambda: _StubClient([])  # type: ignore[assignment]
        r = self.c.get('/api/approvals/build-trails', headers=_AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['trails'], {})

    def test_truncated_flag_computed_on_deduped_set(self):
        da._get_larry_action_supabase_client = lambda: _StubClient([])  # type: ignore[assignment]
        # cap unique ids → NOT truncated even with duplicates padding the string.
        exactly_cap = [f'evt-{i}' for i in range(da._MAX_APPROVAL_TRAIL_EVENT_IDS)]
        padded = exactly_cap + [exactly_cap[0]] * 5  # duplicates, still cap unique
        r = self.c.get('/api/approvals/build-trails', headers=_AUTH,
                       params={'event_ids': ','.join(padded)})
        self.assertFalse(r.json()['truncated'])
        # one MORE unique id → genuinely truncated.
        over = exactly_cap + ['evt-one-too-many']
        r2 = self.c.get('/api/approvals/build-trails', headers=_AUTH,
                        params={'event_ids': ','.join(over)})
        self.assertTrue(r2.json()['truncated'])


if __name__ == '__main__':
    unittest.main()
