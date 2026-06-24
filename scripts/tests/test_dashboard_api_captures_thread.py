#!/usr/bin/env python3
"""Tests for the capture conversation thread (Missions v2 Phase 4 § 8):

  - GET  /api/missions/captures/{id}/thread  — read the card_message thread
  - POST /api/missions/captures/{id}/message — Larry posts on a card

The thread reuses the chain_events store (no bespoke thread store): a POST emits
one `card_message` event (direction larry_to_team), drops a resume envelope in
Beacon's inbox, and clears any blocked-on-you doorbell; the GET reads the
card_message rows back oldest-first. Supabase is stubbed; the doorbell resolve is
mocked (it routes through larry_alerts/alert_triage_state which refuse_under_test).

Also covers the detect_orphans card_message skip-guard (card_message rows are
keyed by capture_id and must never surface in the Orphans lane).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_captures_thread
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}


def _thread_url(cid: str) -> str:
    return f'/api/missions/captures/{cid}/thread'


def _message_url(cid: str) -> str:
    return f'/api/missions/captures/{cid}/message'


def _cap(cid='cap-1', *, title='Aging idea', risk=None):
    cap = {'id': cid, 'title': title, 'state': 'parked',
           'origin': {'repo': 'ourliberty-agent-core'}}
    if risk is not None:
        cap['risk'] = risk
    return cap


class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _ThreadClient:
    """Minimal supabase stub: thread fetch (select.in_.order) + message upsert.

    `rows` is the chain_events table; select returns rows matching the in_ filter,
    upsert appends (honoring on_conflict/ignore_duplicates on event_id)."""

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None):
        self.rows = list(rows or [])
        self.upserts: list[dict[str, Any]] = []
        self._op: Optional[str] = None
        self._in_vals: list[str] = []
        self._upsert_rows: list[dict[str, Any]] = []
        self._upsert_kwargs: dict[str, Any] = {}

    def table(self, name: str):
        self._op = None
        self._in_vals = []
        self._upsert_rows = []
        self._upsert_kwargs = {}
        return self

    def select(self, cols: str = '*'):
        self._op = 'select'
        return self

    def in_(self, col: str, vals: list[str]):
        self._in_vals = list(vals)
        return self

    def order(self, col: str, desc: bool = False):
        return self

    def upsert(self, rows: list[dict[str, Any]], **kwargs):
        self._op = 'upsert'
        self._upsert_rows = rows
        self._upsert_kwargs = kwargs
        return self

    def execute(self):
        if self._op == 'select':
            data = [r for r in self.rows if r.get('task_id') in self._in_vals]
            return _Resp(data)
        if self._op == 'upsert':
            existing = {r.get('event_id') for r in self.rows}
            for row in self._upsert_rows:
                self.upserts.append(row)
                if (self._upsert_kwargs.get('ignore_duplicates')
                        and row.get('event_id') in existing):
                    continue
                self.rows.append(row)
            return _Resp([])
        return _Resp([])


class _ThreadBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-thread-'))
        (self.tmp / 'inboxes' / 'beacon').mkdir(parents=True, exist_ok=True)
        self.captures_path = self.tmp / 'agents' / 'beacon' / 'captures.json'

        self._orig_captures = da._captures_json_path
        self._orig_agents = da._agents_root
        self._orig_client = da._get_larry_action_supabase_client
        da._captures_json_path = lambda: self.captures_path  # type: ignore[assignment]
        da._agents_root = lambda: self.tmp  # type: ignore[assignment]
        self.client = _ThreadClient()
        da._get_larry_action_supabase_client = lambda: self.client  # type: ignore[assignment]
        self.c = TestClient(da.app)

    def tearDown(self):
        da._captures_json_path = self._orig_captures  # type: ignore[assignment]
        da._agents_root = self._orig_agents  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_client  # type: ignore[assignment]

    def _seed(self, *caps):
        self.captures_path.parent.mkdir(parents=True, exist_ok=True)
        self.captures_path.write_text(
            json.dumps({'schema_version': 2, 'captures': list(caps)}) + '\n')

    def _msg_row(self, cid, event_id, ts, *, direction='larry_to_team',
                 text='hi', actor=ACTOR, needs_reply=True):
        return {
            'event_id': event_id, 'ts': ts, 'agent': actor,
            'event_type': 'card_message', 'task_id': cid, 'actor': actor,
            'payload': {'capture_id': cid, 'direction': direction,
                        'text': text, 'actor': actor, 'needs_reply': needs_reply},
        }

    def _beacon_inbox(self) -> Path:
        return self.tmp / 'inboxes' / 'beacon'


# ==================== auth ====================

class AuthTest(_ThreadBase):
    def test_thread_missing_token_401(self):
        r = self.c.get(_thread_url('cap-1'))
        self.assertEqual(r.status_code, 401)

    def test_message_missing_actor_401(self):
        self._seed(_cap('cap-1'))
        r = self.c.post(_message_url('cap-1'),
                        headers={'X-Dashboard-Token': TOKEN},
                        json={'text': 'hello'})
        self.assertEqual(r.status_code, 401)


# ==================== GET thread ====================

class ThreadGetTest(_ThreadBase):
    def test_missing_capture_404(self):
        self._seed(_cap('cap-1'))
        r = self.c.get(_thread_url('nope'), headers=AUTH)
        self.assertEqual(r.status_code, 404)

    def test_empty_thread_for_known_capture(self):
        self._seed(_cap('cap-1'))
        r = self.c.get(_thread_url('cap-1'), headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['capture_id'], 'cap-1')
        self.assertEqual(body['messages'], [])
        self.assertIn('last_synced_at', body)

    def test_returns_card_messages_oldest_first(self):
        self._seed(_cap('cap-1'))
        # store newest-first (as supabase would, before the handler reverses)
        self.client.rows = [
            self._msg_row('cap-1', 'e2', '2026-06-14T02:00:00+00:00',
                          direction='team_to_larry', text='second'),
            self._msg_row('cap-1', 'e1', '2026-06-14T01:00:00+00:00',
                          direction='larry_to_team', text='first'),
        ]
        r = self.c.get(_thread_url('cap-1'), headers=AUTH)
        self.assertEqual(r.status_code, 200)
        msgs = r.json()['messages']
        self.assertEqual([m['text'] for m in msgs], ['first', 'second'])
        self.assertEqual([m['direction'] for m in msgs],
                         ['larry_to_team', 'team_to_larry'])

    def test_get_pins_per_message_contract_shape(self):
        # Contract C read-shape lock (Phase 4b polling). Per message the GET
        # response must surface a stable per-turn `id` (the projected
        # chain_events.event_id), plus ts, needs_reply, and direction — the
        # exact fields the dashboard's dedupe / unread detection rides on. The
        # live-thread poll replaces the rendered list by `id`, so the id IS the
        # per-message stable identity (§ 7 option a).
        self._seed(_cap('cap-1'))
        self.client.rows = [
            self._msg_row('cap-1', 'e2', '2026-06-14T02:00:00+00:00',
                          direction='team_to_larry', text='second',
                          needs_reply=False),
            self._msg_row('cap-1', 'e1', '2026-06-14T01:00:00+00:00',
                          direction='larry_to_team', text='first',
                          needs_reply=True),
        ]
        r = self.c.get(_thread_url('cap-1'), headers=AUTH)
        self.assertEqual(r.status_code, 200)
        msgs = r.json()['messages']
        self.assertEqual(len(msgs), 2)

        # oldest-first: 'first' (01:00) precedes 'second' (02:00)
        first, second = msgs
        # id — the per-message stable key, projected verbatim from the row's
        # chain_events.event_id (what dedupe / mark-as-seen rides on).
        self.assertEqual(first['id'], 'e1')
        self.assertEqual(second['id'], 'e2')
        # ts — the per-turn timestamp, surfaced verbatim per message.
        self.assertEqual(first['ts'], '2026-06-14T01:00:00+00:00')
        self.assertEqual(second['ts'], '2026-06-14T02:00:00+00:00')
        # needs_reply — surfaced per message as a bool (not just on the POST
        # upsert row), and carries the seeded value, not a constant.
        self.assertIs(first['needs_reply'], True)
        self.assertIs(second['needs_reply'], False)
        # direction — already covered elsewhere; re-pinned here as part of the
        # locked contract shape.
        self.assertEqual(first['direction'], 'larry_to_team')
        self.assertEqual(second['direction'], 'team_to_larry')

        # The per-message shape is exactly the six contract fields — no more,
        # no less. Pinning the field set locks the contract so accidental
        # schema drift (a field added/dropped later) trips this test and forces
        # an intentional decision.
        for m in msgs:
            self.assertEqual(set(m), {'id', 'ts', 'direction', 'text', 'actor',
                                      'needs_reply'})

    def test_filters_non_card_message_rows(self):
        self._seed(_cap('cap-1'))
        self.client.rows = [
            self._msg_row('cap-1', 'e1', '2026-06-14T01:00:00+00:00'),
            {'event_id': 'x', 'ts': '2026-06-14T01:30:00+00:00',
             'event_type': 'task_done', 'task_id': 'cap-1', 'payload': {}},
        ]
        r = self.c.get(_thread_url('cap-1'), headers=AUTH)
        msgs = r.json()['messages']
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]['text'], 'hi')


# ==================== POST message ====================

class MessagePostTest(_ThreadBase):
    def test_empty_text_400(self):
        self._seed(_cap('cap-1'))
        r = self.c.post(_message_url('cap-1'), headers=AUTH, json={'text': '   '})
        # pydantic min_length=1 lets '   ' through; the handler strips → 400.
        self.assertEqual(r.status_code, 400)

    def test_missing_text_422(self):
        self._seed(_cap('cap-1'))
        r = self.c.post(_message_url('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 422)

    def test_unknown_capture_404(self):
        self._seed(_cap('cap-1'))
        r = self.c.post(_message_url('nope'), headers=AUTH, json={'text': 'hi'})
        self.assertEqual(r.status_code, 404)

    def test_post_emits_event_envelope_and_resolves_doorbell(self):
        self._seed(_cap('cap-1', title='My card'))
        with mock.patch('missions_doorbell.resolve_doorbell',
                        return_value={'resolved': True}) as resolve:
            r = self.c.post(_message_url('cap-1'), headers=AUTH,
                            json={'text': 'what about X?'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['posted'])
        self.assertEqual(body['direction'], 'larry_to_team')
        self.assertTrue(body['doorbell_resolved'])
        self.assertTrue(body['event_id'])

        # the card_message event was upserted with the right shape
        self.assertEqual(len(self.client.upserts), 1)
        row = self.client.upserts[0]
        self.assertEqual(row['event_type'], 'card_message')
        self.assertEqual(row['task_id'], 'cap-1')
        self.assertEqual(row['actor'], ACTOR)
        self.assertEqual(row['payload']['direction'], 'larry_to_team')
        self.assertEqual(row['payload']['text'], 'what about X?')
        self.assertTrue(row['payload']['needs_reply'])

        # a resume envelope landed in Beacon's inbox
        envelopes = list(self._beacon_inbox().glob('card-message-*.json'))
        self.assertEqual(len(envelopes), 1)
        env = json.loads(envelopes[0].read_text())
        self.assertEqual(env['source'], 'dashboard')
        self.assertEqual(env['capture_id'], 'cap-1')
        self.assertIn('what about X?', env['prompt'])
        self.assertEqual(env['dedup_identity'], f"card-message:{row['event_id']}")

        resolve.assert_called_once()

    def test_supabase_unavailable_503(self):
        self._seed(_cap('cap-1'))
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        r = self.c.post(_message_url('cap-1'), headers=AUTH, json={'text': 'hi'})
        self.assertEqual(r.status_code, 503)

    def test_doorbell_resolve_failure_does_not_500(self):
        self._seed(_cap('cap-1'))
        with mock.patch('missions_doorbell.resolve_doorbell',
                        side_effect=RuntimeError('boom')):
            r = self.c.post(_message_url('cap-1'), headers=AUTH,
                            json={'text': 'hi'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['doorbell_resolved'])


# ==================== detect_orphans card_message skip ====================

class DetectOrphansCardMessageTest(unittest.TestCase):
    def test_card_message_events_never_orphan(self):
        events = [
            {'task_id': 'cap-1', 'event_type': 'card_message', 'agent': ACTOR,
             'ts': '2026-06-14T01:00:00+00:00'},
            {'task_id': 'genuine-orphan-work', 'event_type': 'task_done',
             'agent': 'forge', 'ts': '2026-06-14T01:00:00+00:00'},
        ]
        orphans = da.detect_orphans(events, registered_task_ids=set())
        tids = {o['task_id'] for o in orphans}
        self.assertNotIn('cap-1', tids)
        self.assertIn('genuine-orphan-work', tids)


# ============ captures-list doorbell projection (Phase 4b.2 Contract E) ======

class CapturesDoorbellTest(_ThreadBase):
    """Pin Contract E: GET /api/missions/captures projects a per-card `doorbell`
    (newest team_to_larry id/ts + blocked), batched from ONE chain_events read,
    fail-safe to null. The closed-card badge (Contract F) keys on this shape."""

    def _captures_by_id(self):
        r = self.c.get('/api/missions/captures', headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        return body, {c['id']: c for c in body['captures']}

    def test_projection_shape_newest_team_reply(self):
        # cap-1: larry ask then a team reply with needs_reply → blocked badge.
        # cap-2: only a larry_to_team message (no team reply) → doorbell null.
        self._seed(_cap('cap-1'), _cap('cap-2'))
        self.client.rows = [
            self._msg_row('cap-1', 'e2', '2026-06-14T02:00:00+00:00',
                          direction='team_to_larry', text='reply',
                          needs_reply=True),
            self._msg_row('cap-1', 'e1', '2026-06-14T01:00:00+00:00',
                          direction='larry_to_team', text='ask'),
            self._msg_row('cap-2', 'e3', '2026-06-14T01:30:00+00:00',
                          direction='larry_to_team', text='ask2'),
        ]
        _, caps = self._captures_by_id()
        db = caps['cap-1']['doorbell']
        self.assertEqual(db['latest_team_id'], 'e2')
        self.assertEqual(db['latest_team_ts'], '2026-06-14T02:00:00+00:00')
        self.assertIs(db['blocked'], True)
        self.assertEqual(set(db), {'latest_team_id', 'latest_team_ts', 'blocked'})
        # no team reply → null (a card with no Beacon reply shows no badge)
        self.assertIsNone(caps['cap-2']['doorbell'])

    def test_newest_team_reply_wins_over_older(self):
        # Two team replies; only the NEWEST one's id/ts/blocked is projected.
        self._seed(_cap('cap-1'))
        self.client.rows = [
            self._msg_row('cap-1', 'new', '2026-06-14T03:00:00+00:00',
                          direction='team_to_larry', needs_reply=False),
            self._msg_row('cap-1', 'old', '2026-06-14T01:00:00+00:00',
                          direction='team_to_larry', needs_reply=True),
        ]
        _, caps = self._captures_by_id()
        db = caps['cap-1']['doorbell']
        self.assertEqual(db['latest_team_id'], 'new')
        self.assertIs(db['blocked'], False)

    def test_blocked_false_when_needs_reply_falsey(self):
        # Pins the loud→quiet distinction Contract F renders: an FYI team reply
        # (needs_reply false) projects blocked=False, not a louder badge.
        self._seed(_cap('cap-1'))
        self.client.rows = [
            self._msg_row('cap-1', 'e1', '2026-06-14T02:00:00+00:00',
                          direction='team_to_larry', needs_reply=False),
        ]
        _, caps = self._captures_by_id()
        db = caps['cap-1']['doorbell']
        self.assertEqual(db['latest_team_id'], 'e1')
        self.assertIs(db['blocked'], False)

    def test_failsafe_chain_events_error_degrades_to_null(self):
        # With the chain_events read stubbed to raise, the captures payload
        # still serves its file-read contract with every card → doorbell null,
        # no 500 (mirrors _reader_captures' missing-file degradation).
        self._seed(_cap('cap-1'), _cap('cap-2'))
        orig = da._fetch_events_for_task_ids

        def _boom(*a, **k):
            raise RuntimeError('chain_events down')

        da._fetch_events_for_task_ids = _boom  # type: ignore[assignment]
        try:
            body, caps = self._captures_by_id()
        finally:
            da._fetch_events_for_task_ids = orig  # type: ignore[assignment]
        self.assertIn('last_synced_at', body)
        self.assertEqual(set(caps), {'cap-1', 'cap-2'})
        for c in caps.values():
            self.assertIsNone(c['doorbell'])

    def test_single_batch_query_no_per_card_roundtrip(self):
        # The projection consults chain_events exactly ONCE for the whole list,
        # carrying every parked card id — no per-card round-trip.
        self._seed(_cap('cap-1'), _cap('cap-2'), _cap('cap-3'))
        orig = da._fetch_events_for_task_ids
        calls: list[list[str]] = []

        def _spy(client, task_ids):
            calls.append(list(task_ids))
            return orig(client, task_ids)

        da._fetch_events_for_task_ids = _spy  # type: ignore[assignment]
        try:
            self._captures_by_id()
        finally:
            da._fetch_events_for_task_ids = orig  # type: ignore[assignment]
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {'cap-1', 'cap-2', 'cap-3'})


if __name__ == '__main__':
    unittest.main()
