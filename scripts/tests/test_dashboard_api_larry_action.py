#!/usr/bin/env python3
"""Tests for /api/larry/action + /api/larry/allowlist (E4.4e PR-B2).

Covers spec § 7.2's test matrix: 401 (bad token / bad actor), 400
(bad target_agent — frozenset + path-resolve guards both), 409
(already-acted source event), 200 happy paths for approve / reject /
comment / mark_done, mark_done idempotent on already-read source.

The supabase client is stubbed via a `_RecordingClient` that captures
every `.table().select()/update()/upsert()` chain. Tests monkeypatch
`da._get_larry_action_supabase_client` to inject the stub plus
`da._agents_root` to redirect inbox writes into a tmpdir.

Run:
    cd /home/larry/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_larry_action
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Set DASHBOARD_API_TOKEN at import time via setdefault so the dashboard_api
# auth dependency resolves it at request time. Sibling test modules
# (e.g. test_dashboard_api.py) may pop this env var in their own
# tearDownModule(); _TokenSetMixin re-sets it in setUp() so the fix
# survives any cross-module teardown ordering under `unittest discover`.
TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


ALLOWED_ACTOR = 'larry@sealteamleaders.com'
AUTH = {
    'X-Dashboard-Token': TOKEN,
    'X-Actor': ALLOWED_ACTOR,
    'Content-Type': 'application/json',
}


class _TokenSetMixin:
    """Re-set DASHBOARD_API_TOKEN in every setUp.

    test_dashboard_api.py's tearDownModule pops the env var unconditionally
    if its own _ORIGINAL_TOKEN snapshot was None at its import time —
    which it is under `unittest discover` since this module isn't loaded
    yet at that point. Re-setting in setUp is the most robust shape: it
    survives any teardown ordering and any other test module's env
    mutations. Mirrors test_dashboard_api_system.py's fix verbatim.
    """

    def setUp(self):  # noqa: D401 — unittest hook
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        super().setUp()


# ---------- supabase stub ----------

class _RecordingClient:
    """Captures every supabase-py table() call chain for assertion.

    The stub speaks the subset of supabase-py the dashboard endpoint
    uses: select+eq+limit+execute (fetch), update+eq+execute (read_at
    flip), upsert+execute (audit insert). The owning test pre-seeds a
    `rows_by_event_id` dict; select(...) returns that row.
    """

    def __init__(self, rows_by_event_id: Optional[dict[str, dict[str, Any]]] = None):
        self.rows_by_event_id = rows_by_event_id or {}
        self.calls: list[dict[str, Any]] = []
        self._table: Optional[str] = None
        self._op: Optional[str] = None
        self._filters: dict[str, Any] = {}
        self._select_cols: Optional[str] = None
        self._update_values: Optional[dict[str, Any]] = None
        self._upsert_rows: Optional[list[dict[str, Any]]] = None
        self._upsert_kwargs: Optional[dict[str, Any]] = None
        self._limit: Optional[int] = None

    def table(self, name: str):
        self._table = name
        self._op = None
        self._filters = {}
        self._select_cols = None
        self._update_values = None
        self._upsert_rows = None
        self._upsert_kwargs = None
        self._limit = None
        return self

    def select(self, cols: str = '*'):
        self._op = 'select'
        self._select_cols = cols
        return self

    def update(self, values: dict[str, Any]):
        self._op = 'update'
        self._update_values = values
        return self

    def upsert(self, rows: list[dict[str, Any]], **kwargs):
        self._op = 'upsert'
        self._upsert_rows = rows
        self._upsert_kwargs = kwargs
        return self

    def eq(self, col: str, val: Any):
        self._filters[col] = val
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def execute(self):
        call = {
            'table': self._table,
            'op': self._op,
            'filters': dict(self._filters),
            'limit': self._limit,
            'select_cols': self._select_cols,
            'update_values': self._update_values,
            'upsert_rows': self._upsert_rows,
            'upsert_kwargs': self._upsert_kwargs,
        }
        self.calls.append(call)
        if self._op == 'select':
            event_id = self._filters.get('event_id')
            row = self.rows_by_event_id.get(event_id)
            return _Resp([row] if row else [])
        return _Resp([])


class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


# ---------- shared test base ----------

class _LarryActionBase(_TokenSetMixin, unittest.TestCase):
    """Sets up an isolated agents-root + injected supabase stub per test."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-larry-'))
        for sub in (
            'inboxes/beacon', 'inboxes/forge',
            'inboxes/mirror', 'inboxes/pulse',
        ):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self._orig_agents_root = da._agents_root
        self._orig_get_client = da._get_larry_action_supabase_client
        da._agents_root = lambda: self.tmp  # type: ignore[assignment]
        self.client_stub = _RecordingClient()
        da._get_larry_action_supabase_client = lambda: self.client_stub  # type: ignore[assignment]
        self.c = TestClient(da.app)

    def tearDown(self):
        da._agents_root = self._orig_agents_root  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]

    def _seed(self, **row) -> dict[str, Any]:
        """Insert a chain_events row into the stub keyed by event_id."""
        self.client_stub.rows_by_event_id[row['event_id']] = row
        return row


# ---------- auth: token + actor ----------

class AuthTest(_LarryActionBase):

    def test_missing_token_401(self):
        r = self.c.post(
            '/api/larry/action',
            headers={'X-Actor': ALLOWED_ACTOR, 'Content-Type': 'application/json'},
            json={'source_event_id': 'e1', 'action': 'mark_done'},
        )
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {'detail': 'missing X-Dashboard-Token'})

    def test_bad_token_401(self):
        r = self.c.post(
            '/api/larry/action',
            headers={
                'X-Dashboard-Token': 'wrong',
                'X-Actor': ALLOWED_ACTOR,
                'Content-Type': 'application/json',
            },
            json={'source_event_id': 'e1', 'action': 'mark_done'},
        )
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {'detail': 'invalid X-Dashboard-Token'})

    def test_missing_actor_401(self):
        r = self.c.post(
            '/api/larry/action',
            headers={'X-Dashboard-Token': TOKEN, 'Content-Type': 'application/json'},
            json={'source_event_id': 'e1', 'action': 'mark_done'},
        )
        self.assertEqual(r.status_code, 401)
        # Generic 'unauthorized' — never echo the (missing) actor value.
        self.assertEqual(r.json(), {'detail': 'unauthorized'})

    def test_bad_actor_401(self):
        r = self.c.post(
            '/api/larry/action',
            headers={
                'X-Dashboard-Token': TOKEN,
                'X-Actor': 'someone-else@gmail.com',
                'Content-Type': 'application/json',
            },
            json={'source_event_id': 'e1', 'action': 'mark_done'},
        )
        self.assertEqual(r.status_code, 401)
        # PII rule — the rejected actor value MUST NOT leak into the body.
        self.assertEqual(r.json(), {'detail': 'unauthorized'})
        self.assertNotIn('someone-else', r.text)


# ---------- 404 / 409 / 400 error paths ----------

class ErrorPathsTest(_LarryActionBase):

    def test_source_event_not_found_404(self):
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'missing', 'action': 'approve'},
        )
        self.assertEqual(r.status_code, 404)

    def test_already_acted_on_409(self):
        """read_at IS NOT NULL blocks every action except mark_done."""
        self._seed(
            event_id='ev-approval-1',
            task_id='task-001',
            event_type='approval_request',
            payload={'proposing_agent': 'forge', 'target_agent': 'forge', 'prompt': '...'},
            read_at='2026-05-26T16:00:00+00:00',
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-approval-1', 'action': 'approve'},
        )
        self.assertEqual(r.status_code, 409)

    def test_mark_done_idempotent_on_already_read(self):
        """mark_done MUST still succeed on already-read source events."""
        self._seed(
            event_id='ev-alert-1',
            task_id='task-002',
            event_type='larry_alert',
            payload={'message': 'memory bumped to 4G'},
            read_at='2026-05-26T16:00:00+00:00',
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-alert-1', 'action': 'mark_done'},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body['envelope_written'])
        self.assertIsNone(body['target_agent'])
        # Audit row still got written.
        upserts = [c for c in self.client_stub.calls if c['op'] == 'upsert']
        self.assertEqual(len(upserts), 1)

    def test_bad_action_400(self):
        self._seed(
            event_id='ev-a-bad',
            task_id='t',
            event_type='approval_request',
            payload={},
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-a-bad', 'action': 'nope'},
        )
        self.assertEqual(r.status_code, 400)

    def test_event_type_alert_rejects_non_mark_done(self):
        """larry_alert / escalation / sentinel_alert support only mark_done."""
        self._seed(
            event_id='ev-alert-2',
            task_id='t',
            event_type='larry_alert',
            payload={},
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-alert-2', 'action': 'approve'},
        )
        self.assertEqual(r.status_code, 400)


# ---------- path-injection guards (§ 7.3) ----------

class PathInjectionTest(_LarryActionBase):

    def test_unknown_asking_agent_400(self):
        """clarify_request with target_agent outside the frozenset → 400.

        Drives the FIRST half of the path-injection guard: target_agent
        must be in ALLOWED_TARGET_AGENTS.
        """
        self._seed(
            event_id='ev-clarify-bad',
            task_id='task-x',
            event_type='clarify_request',
            payload={
                'asking_agent': 'rogue-agent',
                'question': 'q?',
                'resume_session_id': 's',
            },
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-clarify-bad', 'action': 'comment',
                  'comment': 'reply'},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('not allowed', r.json()['detail'])

    def test_path_traversal_in_task_id_400(self):
        """clarify_request with `..` in the task_id (which becomes part of the
        envelope filename) → 400 via the .resolve() containment check.

        Drives the SECOND half of the guard: frozenset alone isn't enough
        because target_agent='forge' is legitimate; the filename must
        also not escape the agent inbox dir.
        """
        self._seed(
            event_id='ev-clarify-traverse',
            task_id='../../etc/passwd',  # filename = resume-../../etc/passwd-r1.json
            event_type='clarify_request',
            payload={
                'asking_agent': 'forge',  # allowed
                'question': 'q?',
                'resume_session_id': 's',
            },
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-clarify-traverse',
                  'action': 'comment', 'comment': 'reply'},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('invalid envelope filename', r.json()['detail'])
        # Nothing should have been written under any inbox.
        for agent in ('beacon', 'forge', 'mirror', 'pulse'):
            entries = list((self.tmp / 'inboxes' / agent).iterdir())
            self.assertEqual(entries, [], f'unexpected file in {agent} inbox')


# ---------- happy paths: approve / reject / comment / mark_done ----------

class HappyPathTest(_LarryActionBase):

    def _assert_audit_row(self, *, source_event_id: str, action: str,
                          target_agent: Optional[str],
                          envelope_written: Optional[str]) -> None:
        upserts = [c for c in self.client_stub.calls if c['op'] == 'upsert']
        self.assertEqual(len(upserts), 1, 'expected exactly one larry_action insert')
        row = upserts[0]['upsert_rows'][0]
        self.assertEqual(row['event_type'], 'larry_action')
        self.assertEqual(row['agent'], 'dashboard')
        self.assertEqual(row['actor'], ALLOWED_ACTOR)
        self.assertEqual(row['payload']['source_event_id'], source_event_id)
        self.assertEqual(row['payload']['action'], action)
        self.assertEqual(row['payload']['target_agent'], target_agent)
        self.assertEqual(row['payload']['envelope_written'], envelope_written)
        self.assertEqual(upserts[0]['upsert_kwargs'].get('on_conflict'), 'event_id')
        self.assertTrue(upserts[0]['upsert_kwargs'].get('ignore_duplicates'))

    def _assert_read_at_flipped(self, source_event_id: str) -> None:
        updates = [c for c in self.client_stub.calls if c['op'] == 'update']
        matches = [u for u in updates
                   if u['filters'].get('event_id') == source_event_id
                   and 'read_at' in (u['update_values'] or {})]
        self.assertEqual(len(matches), 1, 'expected one read_at update')

    def test_approve_writes_beacon_envelope(self):
        self._seed(
            event_id='ev-ap-1',
            task_id='task-approve-1',
            event_type='approval_request',
            payload={
                'proposing_agent': 'beacon',
                'target_agent': 'forge',
                'prompt': 'Forge should X.',
            },
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-ap-1', 'action': 'approve'},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['target_agent'], 'beacon')
        expected = self.tmp / 'inboxes' / 'beacon' / 'larry-approval-ev-ap-1.json'
        self.assertEqual(Path(body['envelope_written']), expected)
        self.assertTrue(expected.exists())
        envelope = json.loads(expected.read_text())
        self.assertEqual(envelope['task_id'], 'larry-approval-ev-ap-1')
        self.assertEqual(envelope['source'], 'dashboard')
        self.assertEqual(envelope['actor'], ALLOWED_ACTOR)
        self.assertEqual(envelope['dedup_identity'], 'larry-approval:ev-ap-1')
        self.assertIn('Larry approved', envelope['prompt'])
        # No leftover .tmp file.
        leftovers = [p for p in expected.parent.iterdir() if p.name.endswith('.tmp')]
        self.assertEqual(leftovers, [])
        self._assert_read_at_flipped('ev-ap-1')
        self._assert_audit_row(
            source_event_id='ev-ap-1', action='approve',
            target_agent='beacon', envelope_written=str(expected),
        )

    def test_reject_writes_beacon_envelope_with_comment(self):
        self._seed(
            event_id='ev-ap-2',
            task_id='task-reject-1',
            event_type='approval_request',
            payload={
                'proposing_agent': 'beacon',
                'target_agent': 'forge',
                'prompt': 'Forge should X.',
            },
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-ap-2', 'action': 'reject',
                  'comment': 'wrong approach, try Y'},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        expected = self.tmp / 'inboxes' / 'beacon' / 'larry-reject-ev-ap-2.json'
        self.assertEqual(Path(body['envelope_written']), expected)
        envelope = json.loads(expected.read_text())
        self.assertEqual(envelope['task_id'], 'larry-reject-ev-ap-2')
        self.assertEqual(envelope['dedup_identity'], 'larry-reject:ev-ap-2')
        self.assertEqual(envelope['comment'], 'wrong approach, try Y')
        self.assertIn('Larry rejected', envelope['prompt'])
        self._assert_audit_row(
            source_event_id='ev-ap-2', action='reject',
            target_agent='beacon', envelope_written=str(expected),
        )

    def test_comment_writes_clarify_resume_envelope(self):
        self._seed(
            event_id='ev-cl-1',
            task_id='task-clarify-1',
            event_type='clarify_request',
            payload={
                'asking_agent': 'forge',
                'question': 'should I use option A or B?',
                'resume_session_id': 'sess-abc-123',
            },
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-cl-1', 'action': 'comment',
                  'comment': 'go with B'},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['target_agent'], 'forge')
        expected = self.tmp / 'inboxes' / 'forge' / 'resume-task-clarify-1-r1.json'
        self.assertEqual(Path(body['envelope_written']), expected)
        envelope = json.loads(expected.read_text())
        self.assertEqual(envelope['task_id'], 'task-clarify-1')
        self.assertEqual(envelope['source'], 'dashboard')
        self.assertEqual(envelope['actor'], ALLOWED_ACTOR)
        self.assertEqual(envelope['resume_session_id'], 'sess-abc-123')
        self.assertEqual(envelope['round'], 1)
        self.assertEqual(envelope['prompt'], 'go with B')
        self._assert_audit_row(
            source_event_id='ev-cl-1', action='comment',
            target_agent='forge', envelope_written=str(expected),
        )

    def test_mark_done_no_envelope(self):
        self._seed(
            event_id='ev-alert-fresh',
            task_id='task-alert-1',
            event_type='larry_alert',
            payload={'message': 'pipeline blocked'},
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-alert-fresh', 'action': 'mark_done'},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body['envelope_written'])
        self.assertIsNone(body['target_agent'])
        # No files in any inbox.
        for agent in ('beacon', 'forge', 'mirror', 'pulse'):
            entries = list((self.tmp / 'inboxes' / agent).iterdir())
            self.assertEqual(entries, [])
        self._assert_read_at_flipped('ev-alert-fresh')
        self._assert_audit_row(
            source_event_id='ev-alert-fresh', action='mark_done',
            target_agent=None, envelope_written=None,
        )


# ---------- GET /api/larry/allowlist ----------

class AllowlistEndpointTest(_LarryActionBase):

    def test_returns_hardcoded_list(self):
        r = self.c.get(
            '/api/larry/allowlist',
            headers={'X-Dashboard-Token': TOKEN},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'allowed_emails': [ALLOWED_ACTOR]})

    def test_token_required(self):
        r = self.c.get('/api/larry/allowlist')
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()
