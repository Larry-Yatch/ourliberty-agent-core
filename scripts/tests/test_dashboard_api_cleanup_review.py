#!/usr/bin/env python3
"""Tests for POST /api/larry/cleanup-review (approvals-queue-rework N1 / L8).

The cleanup-review engine reuses scripts/triage_decisions.py's
classification verbatim, auto-clears confirmed-stale rows (backup-first),
and surfaces still-live items with a reason. Low-confidence (UNCERTAIN)
rows go to a verification subagent and are cleared ONLY when it confirms
resolution. These tests assert:

  - token + actor gating (mirrors /api/larry/action)
  - STALE (beacon-history) + MOCK (real-*) rows auto-clear
  - LIVE rows (still pending in beacon, or no clarify_response) are kept
  - UNCERTAIN rows route through the subagent: verdict=resolved -> cleared,
    anything else (incl. subagent failure -> {}) -> kept (never clear what
    we cannot confirm)
  - backup file is written BEFORE the read_at flip, and is reversible
  - the subagent is NOT invoked when there are no uncertain rows

The supabase client is a `_FakeSupabase` that speaks the query subset
triage_decisions + the engine use: select+is_+eq+range (fetch), select+eq
(clarify_response), update+in_ (clear).

Run:
    cd /home/larry/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_cleanup_review
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


# ---------- supabase stub ----------

class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _FakeSupabase:
    """Minimal supabase-py query-builder stub over an in-memory row list.

    Supports the chains triage_decisions + the engine call:
      select(cols).is_(col,val).eq(col,val).range(a,b).execute()  -> rows
      select(cols).eq(col,val).execute()                          -> rows
      update(values).in_(col, ids).execute()                      -> mutate
    """

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.update_calls: list[tuple[list[Any], dict[str, Any]]] = []
        self._reset()

    def _reset(self):
        self._table: Optional[str] = None
        self._op: Optional[str] = None
        self._eq: dict[str, Any] = {}
        self._is: dict[str, Any] = {}
        self._in: Optional[tuple[str, list[Any]]] = None
        self._update: Optional[dict[str, Any]] = None
        self._range: Optional[tuple[int, int]] = None

    def table(self, name: str):
        self._reset()
        self._table = name
        return self

    def select(self, cols: str = '*'):
        self._op = 'select'
        return self

    def update(self, values: dict[str, Any]):
        self._op = 'update'
        self._update = values
        return self

    def is_(self, col: str, val: Any):
        self._is[col] = val
        return self

    def eq(self, col: str, val: Any):
        self._eq[col] = val
        return self

    def in_(self, col: str, vals: list[Any]):
        self._in = (col, list(vals))
        return self

    def range(self, a: int, b: int):
        self._range = (a, b)
        return self

    def _match(self, r: dict[str, Any]) -> bool:
        for col, val in self._eq.items():
            if r.get(col) != val:
                return False
        for col, val in self._is.items():
            if val == 'null' and r.get(col) is not None:
                return False
        return True

    def execute(self):
        if self._op == 'update':
            col, ids = self._in  # type: ignore[misc]
            self.update_calls.append((list(ids), dict(self._update or {})))
            for r in self.rows:
                if r.get(col) in ids:
                    r.update(self._update or {})
            return _Resp([])
        data = [r for r in self.rows if self._match(r)]
        if self._range is not None:
            a, b = self._range
            data = data[a:b + 1]
        return _Resp(data)


# ---------- base ----------

class _CleanupReviewBase(unittest.TestCase):

    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-cleanup-'))
        (self.tmp / 'state').mkdir(parents=True, exist_ok=True)
        (self.tmp / 'blackboard' / 'backups').mkdir(parents=True, exist_ok=True)

        self._orig_agents_root = da._agents_root
        self._orig_get_client = da._get_larry_action_supabase_client
        self._orig_verifier = da._cleanup_review_verify_uncertain

        da._agents_root = lambda: self.tmp  # type: ignore[assignment]

        # Default verifier records its calls and returns {} (keep all) unless
        # a test overrides self.verdicts.
        self.verifier_calls: list[list[dict[str, Any]]] = []
        self.verdicts: dict[str, dict[str, str]] = {}

        def _fake_verifier(items, **_kw):
            self.verifier_calls.append(items)
            return self.verdicts

        da._cleanup_review_verify_uncertain = _fake_verifier  # type: ignore[assignment]
        self.c = TestClient(da.app)

    def tearDown(self):
        da._agents_root = self._orig_agents_root  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]
        da._cleanup_review_verify_uncertain = self._orig_verifier  # type: ignore[assignment]

    def _install_client(self, rows: list[dict[str, Any]]):
        self.client = _FakeSupabase(rows)
        da._get_larry_action_supabase_client = lambda: self.client  # type: ignore[assignment]

    def _write_beacon(self, *, pending=None, history=None):
        body = {'pending': pending or [], 'history': history or []}
        (self.tmp / 'state' / 'beacon-pending-approvals.json').write_text(
            json.dumps(body))


# ---------- auth ----------

class AuthTest(_CleanupReviewBase):

    def setUp(self):
        super().setUp()
        self._install_client([])
        self._write_beacon()

    def test_missing_token_401(self):
        r = self.c.post('/api/larry/cleanup-review',
                        headers={'X-Actor': ALLOWED_ACTOR})
        self.assertEqual(r.status_code, 401)

    def test_missing_actor_401(self):
        r = self.c.post('/api/larry/cleanup-review',
                        headers={'X-Dashboard-Token': TOKEN})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {'detail': 'unauthorized'})

    def test_bad_actor_401(self):
        r = self.c.post('/api/larry/cleanup-review', headers={
            'X-Dashboard-Token': TOKEN, 'X-Actor': 'nope@gmail.com'})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('nope', r.text)

    def test_supabase_unavailable_503(self):
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        r = self.c.post('/api/larry/cleanup-review', headers=AUTH)
        self.assertEqual(r.status_code, 503)


# ---------- triage + clear behavior ----------

class TriageTest(_CleanupReviewBase):

    def test_empty_pending_noop_no_subagent(self):
        self._install_client([])
        self._write_beacon()
        r = self.c.post('/api/larry/cleanup-review', headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['cleared'], [])
        self.assertEqual(body['kept'], [])
        self.assertIsNone(body['backup_path'])
        self.assertEqual(body['uncertain_reviewed'], 0)
        # No uncertain rows -> subagent never invoked (cost guard).
        self.assertEqual(self.verifier_calls, [])
        # Nothing cleared.
        self.assertEqual(self.client.update_calls, [])

    def test_stale_and_mock_cleared_live_kept(self):
        rows = [
            # STALE approval: resolved in beacon history.
            {'event_id': 'ev-stale', 'event_type': 'approval_request',
             'task_id': 'task-stale', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
            # LIVE approval: still pending in beacon.
            {'event_id': 'ev-live', 'event_type': 'approval_request',
             'task_id': 'task-live', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
            # MOCK: real-* task_id, cleared across types.
            {'event_id': 'ev-mock', 'event_type': 'approval_request',
             'task_id': 'real-pf-1', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
            # LIVE clarify: no clarify_response, task not resolved.
            {'event_id': 'ev-cl-live', 'event_type': 'clarify_request',
             'task_id': 'task-cl-live', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
            # STALE clarify: a later clarify_response exists.
            {'event_id': 'ev-cl-stale', 'event_type': 'clarify_request',
             'task_id': 'task-cl-stale', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
            {'event_id': 'ev-cresp', 'event_type': 'clarify_response',
             'task_id': 'task-cl-stale', 'ts': '2026-06-02T00:00:00+00:00',
             'read_at': '2026-06-02T00:00:00+00:00'},
        ]
        self._install_client(rows)
        self._write_beacon(
            pending=[{'id': 'task-live'}],
            history=[{'id': 'task-stale'}],
        )
        r = self.c.post('/api/larry/cleanup-review', headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()

        self.assertCountEqual(
            body['cleared'], ['task-stale', 'real-pf-1', 'task-cl-stale'])
        kept_ids = {k['task_id'] for k in body['kept']}
        self.assertEqual(kept_ids, {'task-live', 'task-cl-live'})
        # No uncertain rows here.
        self.assertEqual(body['uncertain_reviewed'], 0)
        self.assertEqual(self.verifier_calls, [])

        # Backup written, then read_at flipped on exactly the cleared ids.
        self.assertIsNotNone(body['backup_path'])
        backup = json.loads(Path(body['backup_path']).read_text())
        self.assertEqual(backup['triggered_by'], ALLOWED_ACTOR)
        backed_ids = {row['event_id'] for row in backup['rows']}
        self.assertEqual(backed_ids, {'ev-stale', 'ev-mock', 'ev-cl-stale'})
        self.assertEqual(len(self.client.update_calls), 1)
        cleared_ids, vals = self.client.update_calls[0]
        self.assertCountEqual(cleared_ids, ['ev-stale', 'ev-mock', 'ev-cl-stale'])
        self.assertIn('read_at', vals)
        # The LIVE rows were NOT flipped.
        for r2 in self.client.rows:
            if r2['event_id'] in ('ev-live', 'ev-cl-live'):
                self.assertIsNone(r2['read_at'])

    def test_uncertain_resolved_cleared_unknown_kept(self):
        """UNCERTAIN rows route to the subagent; only verdict=resolved clears."""
        rows = [
            # UNCERTAIN approval: not in beacon pending or history.
            {'event_id': 'ev-u1', 'event_type': 'approval_request',
             'task_id': 'task-u-resolved', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
            {'event_id': 'ev-u2', 'event_type': 'approval_request',
             'task_id': 'task-u-unknown', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
        ]
        self._install_client(rows)
        self._write_beacon()  # neither task in pending/history -> UNCERTAIN
        self.verdicts = {
            'task-u-resolved': {'verdict': 'resolved', 'reason': 'PR #9 merged'},
            'task-u-unknown': {'verdict': 'unknown', 'reason': 'no evidence'},
        }
        r = self.c.post('/api/larry/cleanup-review', headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()

        self.assertEqual(body['uncertain_reviewed'], 2)
        self.assertEqual(len(self.verifier_calls), 1)
        # Only the subagent-confirmed-resolved row was cleared.
        self.assertEqual(body['cleared'], ['task-u-resolved'])
        kept = {k['task_id']: k['reason'] for k in body['kept']}
        self.assertIn('task-u-unknown', kept)
        self.assertIn('unconfirmed', kept['task-u-unknown'])
        # read_at flipped only on the resolved row.
        cleared_ids, _vals = self.client.update_calls[0]
        self.assertEqual(cleared_ids, ['ev-u1'])

    def test_subagent_failure_keeps_all_uncertain(self):
        """A subagent that returns {} (failure) must clear NOTHING uncertain."""
        rows = [
            {'event_id': 'ev-u', 'event_type': 'approval_request',
             'task_id': 'task-u', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
        ]
        self._install_client(rows)
        self._write_beacon()
        self.verdicts = {}  # simulate subagent failure / no verdicts
        r = self.c.post('/api/larry/cleanup-review', headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['cleared'], [])
        self.assertEqual([k['task_id'] for k in body['kept']], ['task-u'])
        self.assertIsNone(body['backup_path'])
        self.assertEqual(self.client.update_calls, [])

    def test_missing_beacon_file_treats_approvals_as_uncertain(self):
        """No beacon state file -> approvals are UNCERTAIN, not blindly cleared."""
        rows = [
            {'event_id': 'ev-a', 'event_type': 'approval_request',
             'task_id': 'task-a', 'ts': '2026-06-01T00:00:00+00:00',
             'read_at': None},
        ]
        self._install_client(rows)
        # Intentionally do NOT write the beacon file.
        r = self.c.post('/api/larry/cleanup-review', headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['uncertain_reviewed'], 1)
        # Subagent returned {} (default) -> kept, not cleared.
        self.assertEqual(body['cleared'], [])


if __name__ == '__main__':
    unittest.main()
