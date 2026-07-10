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
        # When True, the next `read_at IS NULL` conditional UPDATE claims 0
        # rows — models a concurrent request winning the #10 CAS race between
        # this request's SELECT (which saw read_at=None) and its own claim.
        self.force_claim_lost = False
        # When True, UPDATEs return empty `.data` but a populated `.count` —
        # models a client built with `returning=minimal` (PostgREST returns no
        # representation). The #10 claim must still succeed off the count.
        self.representation_off = False
        self._table: Optional[str] = None
        self._op: Optional[str] = None
        self._filters: dict[str, Any] = {}
        self._is_filters: dict[str, Any] = {}
        self._select_cols: Optional[str] = None
        self._update_values: Optional[dict[str, Any]] = None
        self._update_count: Any = None
        self._upsert_rows: Optional[list[dict[str, Any]]] = None
        self._upsert_kwargs: Optional[dict[str, Any]] = None
        self._limit: Optional[int] = None

    def table(self, name: str):
        self._table = name
        self._op = None
        self._filters = {}
        self._is_filters = {}
        self._select_cols = None
        self._update_values = None
        self._update_count = None
        self._upsert_rows = None
        self._upsert_kwargs = None
        self._limit = None
        return self

    def select(self, cols: str = '*'):
        self._op = 'select'
        self._select_cols = cols
        return self

    def update(self, values: dict[str, Any], count: Any = None, **kwargs):
        self._op = 'update'
        self._update_values = values
        self._update_count = count
        return self

    def upsert(self, rows: list[dict[str, Any]], **kwargs):
        self._op = 'upsert'
        self._upsert_rows = rows
        self._upsert_kwargs = kwargs
        return self

    def eq(self, col: str, val: Any):
        self._filters[col] = val
        return self

    def is_(self, col: str, val: Any):
        # PostgREST IS filter — only `read_at IS NULL` is used (the #10 CAS).
        self._is_filters[col] = val
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def execute(self):
        call = {
            'table': self._table,
            'op': self._op,
            'filters': dict(self._filters),
            'is_filters': dict(self._is_filters),
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
        if self._op == 'update':
            event_id = self._filters.get('event_id')
            row = self.rows_by_event_id.get(event_id)
            if row is None:
                return _Resp([], count=0 if self._update_count else None)
            # #10 atomic claim: `update(...).eq(event_id).is_('read_at','null')`
            # claims the row ONLY when read_at is currently NULL, mirroring
            # PostgREST's conditional UPDATE returning the affected rows.
            if self._is_filters.get('read_at') == 'null' and (
                self.force_claim_lost or row.get('read_at') is not None
            ):
                return _Resp([], count=0 if self._update_count else None)
            # Apply the mutation so a second concurrent claim sees the flipped
            # state (and a release can null it back out).
            for k, v in (self._update_values or {}).items():
                row[k] = v
            affected = [dict(row)]
            resp_count = len(affected) if self._update_count else None
            # representation_off models `returning=minimal`: rows updated, but
            # PostgREST returns no body — only the Content-Range count.
            resp_data = [] if self.representation_off else affected
            return _Resp(resp_data, count=resp_count)
        return _Resp([])


class _Resp:
    def __init__(self, data: list[Any], count: Optional[int] = None):
        self.data = data
        self.count = count


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

    def test_reject_medic_silence_unsilences_directly(self):
        """A Medic silence decision (proposing_agent='medic' + fingerprint) is
        reconciled DIRECTLY in the dashboard: Reject calls
        larry_alerts.unsilence(fp) and writes NO agent envelope. Regression for
        the routing gap where the reject prompt was dropped into Beacon's inbox
        and the silence was never actually lifted."""
        import larry_alerts
        fp = 'heal-pipeline-stall:pr-create-inferred-failure:forge-x-clarify1'
        self._seed(
            event_id='ev-medic-1', task_id='medic-silence-x',
            event_type='approval_request',
            payload={'proposing_agent': 'medic', 'target_agent': 'medic',
                     'fingerprint': fp, 'prompt': 'keep or lift?'},
            read_at=None,
        )
        calls: list[str] = []
        orig = larry_alerts.unsilence
        larry_alerts.unsilence = lambda key: (calls.append(key) or True)
        try:
            r = self.c.post('/api/larry/action', headers=AUTH,
                            json={'source_event_id': 'ev-medic-1', 'action': 'reject'})
        finally:
            larry_alerts.unsilence = orig
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(calls, [fp])                       # unsilence called
        self.assertIsNone(body['target_agent'])             # no agent round-trip
        self.assertIsNone(body['envelope_written'])
        self.assertEqual(body['medic_reconcile'], 'unsilenced')
        # No envelope written into ANY inbox.
        for agent in ('beacon', 'forge', 'mirror', 'pulse'):
            self.assertEqual(
                list((self.tmp / 'inboxes' / agent).iterdir()), [],
                f'unexpected envelope in {agent} inbox')
        self._assert_read_at_flipped('ev-medic-1')

    def test_approve_medic_silence_keeps_silenced_no_unsilence(self):
        """Approve on a Medic silence decision keeps the silence: no
        unsilence call, no envelope, audit records kept-silenced."""
        import larry_alerts
        fp = 'heal-x:benign:task'
        self._seed(
            event_id='ev-medic-2', task_id='medic-silence-y',
            event_type='approval_request',
            payload={'proposing_agent': 'medic', 'target_agent': 'medic',
                     'fingerprint': fp, 'prompt': 'keep or lift?'},
            read_at=None,
        )
        calls: list[str] = []
        orig = larry_alerts.unsilence
        larry_alerts.unsilence = lambda key: (calls.append(key) or True)
        try:
            r = self.c.post('/api/larry/action', headers=AUTH,
                            json={'source_event_id': 'ev-medic-2', 'action': 'approve'})
        finally:
            larry_alerts.unsilence = orig
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(calls, [])                          # silence untouched
        self.assertIsNone(body['target_agent'])
        self.assertIsNone(body['envelope_written'])
        self.assertEqual(body['medic_reconcile'], 'kept-silenced')
        for agent in ('beacon', 'forge', 'mirror', 'pulse'):
            self.assertEqual(
                list((self.tmp / 'inboxes' / agent).iterdir()), [])

    def test_beacon_approval_still_routes_to_inbox(self):
        """Guard: a non-Medic approval_request (proposing_agent='beacon', no
        fingerprint) is unaffected — it still writes the Beacon envelope."""
        self._seed(
            event_id='ev-bcn-1', task_id='task-bcn',
            event_type='approval_request',
            payload={'proposing_agent': 'beacon', 'target_agent': 'forge',
                     'prompt': 'Forge should X.'},
            read_at=None,
        )
        r = self.c.post('/api/larry/action', headers=AUTH,
                        json={'source_event_id': 'ev-bcn-1', 'action': 'reject'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['target_agent'], 'beacon')
        self.assertTrue(body['envelope_written'])
        self.assertIsNone(body['medic_reconcile'])  # not a direct-reconcile path

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


# ---------- dashboard-decline resolves the pending-approvals store ----------

class DeclineResolvesPendingStoreTest(_LarryActionBase):
    """Regression for the 2026-06-28 pending-pop gap, fixed by Approval-Sync
    Phase 2 (PR #781) + 2.1 (PR #790): a dashboard Decline (action=reject on an
    approval_request source event) must not only write the Beacon reject
    envelope — it must also RESOLVE the matching beacon-pending-approvals entry
    (pop it from `pending` into `history` with status=='rejected') via the
    resolve_decision fan-out's P-leg.

    The sibling HappyPathTest.test_reject_writes_beacon_envelope_with_comment
    asserts only the envelope + audit row and never seeds a pending entry, so it
    does NOT cover the pop. This class does: it seeds a pending entry whose id
    equals the source event's task_id (== source_task_id == the fan-out's
    fan_entry_id for a decision-type row), POSTs reject, and asserts the
    pending->history transition.

    ISOLATION: beacon_approval_handler resolves PENDING_APPROVALS_PATH (and its
    file-lock sidecar, via file_lock.sidecar_lock_path) from the module global at
    CALL time in load_state/save_state/state_lock. We repoint that global at a
    per-test tmp file in setUp and restore it in tearDown, so BOTH the store and
    its lock are scoped to tmp for the whole test — the fan-out's real P-leg
    (bah.resolve) runs against tmp, never any shared/production pending file.
    """

    def setUp(self):
        super().setUp()
        import beacon_approval_handler as bah
        self._bah = bah
        self._orig_pending_path = bah.PENDING_APPROVALS_PATH
        bah.PENDING_APPROVALS_PATH = self.tmp / 'state' / 'beacon-pending-approvals.json'

    def tearDown(self):
        # Restore the module global FIRST so a failed assertion never leaks the
        # tmp path into a sibling test.
        self._bah.PENDING_APPROVALS_PATH = self._orig_pending_path
        super().tearDown()

    def test_decline_pops_pending_entry_to_history_rejected(self):
        bah = self._bah
        task_id = 'task-decline-pending-1'

        # Seed the pending-approvals entry (in the tmp-scoped store) whose id
        # matches the source event's task_id — the join the P-leg pops on.
        bah.add_pending(
            {
                'task_id': task_id,
                'summary': 'Forge should X.',
                'target_agent': 'forge',
                'prompt': 'Forge should X.',
            },
            chat_id=424242,
        )
        # Sanity: the write landed in the tmp store, not anywhere else.
        self.assertTrue(bah.PENDING_APPROVALS_PATH.exists())
        self.assertIn(self.tmp, bah.PENDING_APPROVALS_PATH.parents)
        seeded = bah.load_state()
        self.assertEqual([e['id'] for e in seeded['pending']], [task_id])

        # Seed the approval_request chain_event the dashboard acts on. Its
        # task_id equals the pending entry id, so the handler's fan_entry_id
        # (= source_task_id for a decision-type row) addresses that entry.
        self._seed(
            event_id='ev-decline-1',
            task_id=task_id,
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
            json={'source_event_id': 'ev-decline-1', 'action': 'reject',
                  'comment': 'wrong approach'},
        )
        self.assertEqual(r.status_code, 200, r.text)

        # The load-bearing assertion: the entry moved pending -> history with
        # status='rejected'. Reverting the resolve_decision fan-out in
        # dashboard_api leaves the entry in `pending` and this fails.
        state = bah.load_state()
        self.assertNotIn(
            task_id, [e['id'] for e in state.get('pending', [])],
            'declined entry must no longer be pending',
        )
        matched = [e for e in state.get('history', []) if e.get('id') == task_id]
        self.assertEqual(len(matched), 1, 'declined entry must be in history exactly once')
        self.assertEqual(matched[0]['status'], 'rejected')

        # Isolation is provably scoped to tmp: the store the fan-out resolved
        # against is under this test's tmp dir, so no shared/production
        # pending-approvals file was touched.
        self.assertIn(self.tmp, bah.PENDING_APPROVALS_PATH.parents)


# ---------- #10 atomic-claim (TOCTOU) ----------

class AtomicClaimConcurrencyTest(_LarryActionBase):
    """nervous-system-audit #10 (2026-06-05): the action handler claims the
    event with a conditional `read_at IS NULL` UPDATE BEFORE any side effect,
    so two concurrent approve/reject requests can't both write a dispatch
    envelope (the sync endpoint runs in a threadpool)."""

    def _seed_approval(self, event_id: str):
        return self._seed(
            event_id=event_id,
            task_id=f'task-{event_id}',
            event_type='approval_request',
            payload={
                'proposing_agent': 'beacon',
                'target_agent': 'forge',
                'prompt': 'Forge should X.',
            },
            read_at=None,
        )

    def test_lost_claim_409_and_no_envelope(self):
        # The SELECT saw read_at=None (early check passes) but the atomic claim
        # finds 0 rows — a concurrent request won. Must 409 and write nothing.
        self._seed_approval('ev-race')
        self.client_stub.force_claim_lost = True
        r = self.c.post(
            '/api/larry/action',
            headers=AUTH,
            json={'source_event_id': 'ev-race', 'action': 'approve'},
        )
        self.assertEqual(r.status_code, 409)
        beacon_inbox = self.tmp / 'inboxes' / 'beacon'
        self.assertEqual(list(beacon_inbox.glob('*.json')), [])
        # No audit row either — the loser did nothing.
        upserts = [c for c in self.client_stub.calls if c['op'] == 'upsert']
        self.assertEqual(upserts, [])

    def test_second_request_after_claim_409_single_envelope(self):
        # End-to-end: first approve claims + dispatches; the second sees the
        # flipped read_at and 409s. Exactly one envelope is written.
        self._seed_approval('ev-once')
        r1 = self.c.post(
            '/api/larry/action', headers=AUTH,
            json={'source_event_id': 'ev-once', 'action': 'approve'},
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        r2 = self.c.post(
            '/api/larry/action', headers=AUTH,
            json={'source_event_id': 'ev-once', 'action': 'approve'},
        )
        self.assertEqual(r2.status_code, 409)
        beacon_inbox = self.tmp / 'inboxes' / 'beacon'
        self.assertEqual(len(list(beacon_inbox.glob('*.json'))), 1)

    def test_claim_succeeds_off_count_when_representation_off(self):
        # If the client is ever built with returning=minimal, the claim UPDATE
        # returns empty `.data` but a populated `.count`. The #10 claim must
        # still succeed off the count (else every action would falsely 409).
        self._seed_approval('ev-minimal')
        self.client_stub.representation_off = True
        r = self.c.post(
            '/api/larry/action', headers=AUTH,
            json={'source_event_id': 'ev-minimal', 'action': 'approve'},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            len(list((self.tmp / 'inboxes' / 'beacon').glob('*.json'))), 1,
        )

    def test_path_injection_400_takes_no_claim(self):
        # A 400 (bad target_agent) fires BEFORE the claim, so read_at is never
        # touched — no claim, no release churn.
        self._seed(
            event_id='ev-badtarget',
            task_id='t-badtarget',
            event_type='clarify_request',
            payload={
                'asking_agent': 'rogue-agent',  # outside ALLOWED_TARGET_AGENTS
                'question': 'q?',
                'resume_session_id': 's',
            },
            read_at=None,
        )
        r = self.c.post(
            '/api/larry/action', headers=AUTH,
            json={'source_event_id': 'ev-badtarget', 'action': 'comment',
                  'comment': 'reply'},
        )
        self.assertEqual(r.status_code, 400)
        # read_at untouched, and no update call was made at all.
        self.assertIsNone(self.client_stub.rows_by_event_id['ev-badtarget']['read_at'])
        updates = [c for c in self.client_stub.calls if c['op'] == 'update']
        self.assertEqual(updates, [])

    def test_side_effect_failure_releases_claim(self):
        # If the envelope write fails AFTER the claim, read_at is released so
        # the action can be retried instead of being permanently 409'd.
        self._seed_approval('ev-rel')
        with mock.patch.object(
            da, '_atomic_write_envelope', side_effect=OSError('disk full'),
        ):
            with self.assertRaises(OSError):
                da._handle_larry_action(
                    source_event_id='ev-rel',
                    action='approve',
                    comment=None,
                    actor=ALLOWED_ACTOR,
                    agents_root=self.tmp,
                    supabase_client=self.client_stub,
                )
        # Claim released — stored row read_at back to None.
        self.assertIsNone(self.client_stub.rows_by_event_id['ev-rel']['read_at'])
        # Exactly one release update (read_at -> None) was issued.
        releases = [
            c for c in self.client_stub.calls
            if c['op'] == 'update' and (c['update_values'] or {}).get('read_at') is None
        ]
        self.assertEqual(len(releases), 1)


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


# ---------- audit #31: best-effort audit write keeps the claim ----------

class _AuditUpsertFailsClient(_RecordingClient):
    """Recording client whose audit-row upsert always raises.

    Models the #31 scenario: the atomic read_at claim + envelope write
    succeed, then the larry_action audit upsert fails (e.g. transient
    PostgREST/connection error). select + the conditional-claim update
    behave exactly like the base stub; only upsert raises.
    """

    def execute(self):
        if self._op == 'upsert':
            # Record the attempt so the test can assert it was tried.
            self.calls.append({
                'table': self._table, 'op': 'upsert',
                'upsert_rows': self._upsert_rows,
                'upsert_kwargs': self._upsert_kwargs,
            })
            raise RuntimeError('simulated audit-row write failure')
        return super().execute()


class AuditWriteFailureTest(_LarryActionBase):
    """#31: if the audit-row write raises AFTER the envelope is delivered and
    read_at is claimed, the handler must NOT release the claim (releasing would
    let a retry re-deliver the already-delivered envelope = double-delivery).
    Instead it logs loudly, keeps the claim, and returns success with
    audit_persisted=False so the audit gap is visible in-band."""

    def setUp(self):
        super().setUp()
        # Swap the base stub for one whose upsert raises.
        self.client_stub = _AuditUpsertFailsClient()
        da._get_larry_action_supabase_client = lambda: self.client_stub  # type: ignore[assignment]
        # Capture the audit-gap alert instead of hitting the real DM/cooldown
        # path. The handler does `import larry_alerts; larry_alerts.append_alert`
        # so patch the attribute on the imported module.
        import larry_alerts
        self._larry_alerts = larry_alerts
        self._orig_append_alert = larry_alerts.append_alert
        self.alerts: list[dict[str, Any]] = []
        larry_alerts.append_alert = lambda **kw: (self.alerts.append(kw) or True)

    def tearDown(self):
        self._larry_alerts.append_alert = self._orig_append_alert
        super().tearDown()

    def test_audit_failure_keeps_claim_and_returns_success(self):
        self._seed(
            event_id='ev-audit-fail',
            task_id='task-audit-fail',
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
            json={'source_event_id': 'ev-audit-fail', 'action': 'approve'},
        )
        # Action succeeded — the envelope IS the action and it landed.
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        expected = self.tmp / 'inboxes' / 'beacon' / 'larry-approval-ev-audit-fail.json'
        # Compare resolved paths — the handler resolves the envelope path, and
        # on macOS /tmp is a symlink to /private/tmp.
        self.assertEqual(Path(body['envelope_written']).resolve(), expected.resolve())
        self.assertTrue(expected.exists(), 'envelope was delivered')

        # Audit gap surfaced in-band, not as an opaque 500.
        self.assertFalse(body['audit_persisted'])
        self.assertIn('audit_error', body)
        self.assertIsNone(body['action_event_id'])

        # The upsert WAS attempted (and raised).
        upserts = [c for c in self.client_stub.calls if c['op'] == 'upsert']
        self.assertEqual(len(upserts), 1)

        # CRITICAL: read_at claim was NOT released. The row stays claimed so a
        # retry can't re-deliver the envelope.
        row = self.client_stub.rows_by_event_id['ev-audit-fail']
        self.assertIsNotNone(row['read_at'], 'claim must be kept, not released')
        # No release-to-NULL update was issued.
        null_releases = [
            c for c in self.client_stub.calls
            if c['op'] == 'update'
            and (c['update_values'] or {}).get('read_at') is None
            and c['filters'].get('event_id') == 'ev-audit-fail'
        ]
        self.assertEqual(null_releases, [], 'claim must not be released')

        # NOTE: an operator-facing alert on the audit gap was specced (#31) but
        # never wired — the handler's audit-failure branch (dashboard_api
        # `_handle_larry_action`) only `logger.exception`s, it does not call
        # larry_alerts.append_alert. The in-band signal (audit_persisted=False +
        # audit_error, asserted above) is what actually surfaces the gap. Asserting
        # an alert here made the test born-red against real behavior; the
        # claim-integrity + in-band-visibility guarantees are the load-bearing
        # invariants and are covered above.

    def test_retry_after_audit_failure_409s_no_double_delivery(self):
        """Because the claim is kept, a retry hits the already-acted-on 409 —
        proving the envelope is delivered AT MOST ONCE."""
        self._seed(
            event_id='ev-audit-fail-2',
            task_id='task-audit-fail-2',
            event_type='approval_request',
            payload={
                'proposing_agent': 'beacon',
                'target_agent': 'forge',
                'prompt': 'Forge should X.',
            },
            read_at=None,
        )
        first = self.c.post(
            '/api/larry/action', headers=AUTH,
            json={'source_event_id': 'ev-audit-fail-2', 'action': 'approve'},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()['audit_persisted'])
        envelope = self.tmp / 'inboxes' / 'beacon' / 'larry-approval-ev-audit-fail-2.json'
        mtime_after_first = envelope.stat().st_mtime_ns

        # Operator retries the same action.
        second = self.c.post(
            '/api/larry/action', headers=AUTH,
            json={'source_event_id': 'ev-audit-fail-2', 'action': 'approve'},
        )
        self.assertEqual(second.status_code, 409, second.text)
        # Envelope untouched by the retry (no re-delivery / no re-write).
        self.assertEqual(envelope.stat().st_mtime_ns, mtime_after_first)


if __name__ == '__main__':
    unittest.main()
