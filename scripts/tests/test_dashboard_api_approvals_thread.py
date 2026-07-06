#!/usr/bin/env python3
"""Tests for the approval conversation thread (approval-sync Phase 4):

  - GET  /api/approvals/{event_id}/thread  — read the card_message thread
  - POST /api/approvals/{event_id}/message — Larry asks the team about an approval

Reuses the SAME store-agnostic card_message core the capture/mission threads use
(_post_card_message + _card_thread_messages), keyed on the approval's chain_event
event_id. A POST emits one `card_message` event (direction larry_to_team), drops
a resume envelope in Beacon's inbox, and 404s when the approval event is gone;
the GET reads the card_message rows back oldest-first. Supabase is stubbed.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_approvals_thread
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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
APPR = 'appr-evt-1'


def _thread_url(eid: str) -> str:
    return f'/api/approvals/{eid}/thread'


def _message_url(eid: str) -> str:
    return f'/api/approvals/{eid}/message'


class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _Client:
    """Supabase stub handling the two read shapes the approval handlers use —
    `_select_source_event` (select.eq('event_id').limit) and the thread read
    (select.in_('task_id').order) — plus the card_message upsert."""

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None):
        self.rows = list(rows or [])
        self.upserts: list[dict[str, Any]] = []
        self._op: Optional[str] = None
        self._eq: dict[str, Any] = {}
        self._in_vals: list[str] = []
        self._upsert_rows: list[dict[str, Any]] = []
        self._upsert_kwargs: dict[str, Any] = {}

    def table(self, name: str):
        self._op = None
        self._eq = {}
        self._in_vals = []
        self._upsert_rows = []
        self._upsert_kwargs = {}
        return self

    def select(self, cols: str = '*'):
        self._op = 'select'
        return self

    def eq(self, col: str, val: Any):
        self._eq[col] = val
        return self

    def in_(self, col: str, vals: list[str]):
        self._in_vals = list(vals)
        return self

    def order(self, col: str, desc: bool = False):
        return self

    def limit(self, n: int):
        return self

    def upsert(self, rows: list[dict[str, Any]], **kwargs):
        self._op = 'upsert'
        self._upsert_rows = rows
        self._upsert_kwargs = kwargs
        return self

    def execute(self):
        if self._op == 'select':
            if 'event_id' in self._eq:
                data = [r for r in self.rows if r.get('event_id') == self._eq['event_id']]
            elif self._in_vals:
                data = [r for r in self.rows if r.get('task_id') in self._in_vals]
            else:
                data = list(self.rows)
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


def _approval_row(event_id=APPR, *, event_type='approval_request', headline='Deploy to prod?'):
    return {
        'event_id': event_id, 'ts': '2026-07-06T01:00:00+00:00', 'agent': 'forge',
        'event_type': event_type, 'task_id': event_id,
        'payload': {'headline': headline},
    }


def _msg_row(eid, event_id, ts, *, direction='larry_to_team', text='hi'):
    return {
        'event_id': event_id, 'ts': ts, 'agent': ACTOR,
        'event_type': 'card_message', 'task_id': eid, 'actor': ACTOR,
        'payload': {'approval_event_id': eid, 'direction': direction,
                    'text': text, 'actor': ACTOR, 'needs_reply': True},
    }


class _Base(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-appr-thread-'))
        (self.tmp / 'inboxes' / 'beacon').mkdir(parents=True, exist_ok=True)
        self._orig_agents = da._agents_root
        self._orig_client = da._get_larry_action_supabase_client
        da._agents_root = lambda: self.tmp  # type: ignore[assignment]
        self.client = _Client()
        da._get_larry_action_supabase_client = lambda: self.client  # type: ignore[assignment]
        self.c = TestClient(da.app)

    def tearDown(self):
        da._agents_root = self._orig_agents  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_client  # type: ignore[assignment]

    def _beacon_inbox(self) -> Path:
        return self.tmp / 'inboxes' / 'beacon'


class CardKindMetaTest(unittest.TestCase):
    def test_approval_kind_registered(self):
        meta = da._CARD_KIND_META[da._CARD_KIND_APPROVAL]
        self.assertEqual(meta['thread_url'], '/api/approvals/{id}/thread')
        self.assertEqual(meta['id_key'], 'approval_event_id')


class AuthTest(_Base):
    def test_thread_missing_token_401(self):
        r = self.c.get(_thread_url(APPR))
        self.assertEqual(r.status_code, 401)

    def test_message_missing_actor_401(self):
        self.client.rows = [_approval_row()]
        r = self.c.post(_message_url(APPR),
                        headers={'X-Dashboard-Token': TOKEN},
                        json={'text': 'question?'})
        self.assertEqual(r.status_code, 401)


class ThreadGetTest(_Base):
    def test_missing_approval_404(self):
        self.client.rows = []
        r = self.c.get(_thread_url('nope'), headers=AUTH)
        self.assertEqual(r.status_code, 404)

    def test_empty_thread_for_known_approval(self):
        self.client.rows = [_approval_row()]
        r = self.c.get(_thread_url(APPR), headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['approval_event_id'], APPR)
        self.assertEqual(body['messages'], [])
        self.assertIn('last_synced_at', body)

    def test_returns_card_messages_oldest_first(self):
        self.client.rows = [
            _approval_row(),
            _msg_row(APPR, 'e2', '2026-07-06T02:00:00+00:00',
                     direction='team_to_larry', text='second'),
            _msg_row(APPR, 'e1', '2026-07-06T01:30:00+00:00',
                     direction='larry_to_team', text='first'),
        ]
        r = self.c.get(_thread_url(APPR), headers=AUTH)
        self.assertEqual(r.status_code, 200)
        msgs = r.json()['messages']
        self.assertEqual([m['text'] for m in msgs], ['first', 'second'])
        self.assertEqual([m['direction'] for m in msgs],
                         ['larry_to_team', 'team_to_larry'])


class MessagePostTest(_Base):
    def test_missing_approval_404(self):
        self.client.rows = []
        r = self.c.post(_message_url('nope'), headers=AUTH, json={'text': 'q?'})
        self.assertEqual(r.status_code, 404)

    def test_empty_text_422_or_400(self):
        self.client.rows = [_approval_row()]
        r = self.c.post(_message_url(APPR), headers=AUTH, json={'text': ''})
        # min_length=1 on the pydantic model → 422 (FastAPI validation).
        self.assertIn(r.status_code, (400, 422))

    def test_posts_card_message_and_drops_envelope(self):
        self.client.rows = [_approval_row(headline='Deploy to prod?')]
        # The doorbell resolve is capture-centric + routes through larry_alerts
        # (refuse_under_test); mock it so the best-effort call is a clean no-op
        # rather than a swallowed-but-noisy traceback.
        with mock.patch('missions_doorbell.resolve_doorbell',
                        return_value={'resolved': False}):
            r = self.c.post(_message_url(APPR), headers=AUTH,
                            json={'text': 'why this repo?'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['posted'])
        self.assertEqual(body['direction'], 'larry_to_team')
        # A card_message row was upserted, keyed on the approval event_id.
        cm = [u for u in self.client.upserts if u.get('event_type') == 'card_message']
        self.assertEqual(len(cm), 1)
        self.assertEqual(cm[0]['task_id'], APPR)
        self.assertEqual(cm[0]['payload']['approval_event_id'], APPR)
        self.assertEqual(cm[0]['payload']['text'], 'why this repo?')
        # A resume envelope landed in Beacon's inbox.
        envelopes = list(self._beacon_inbox().glob('card-message-*.json'))
        self.assertEqual(len(envelopes), 1)


if __name__ == '__main__':
    unittest.main()
