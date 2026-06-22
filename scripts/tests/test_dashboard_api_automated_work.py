#!/usr/bin/env python3
"""Tests for GET /api/system/automated-work (the "Automated Work" feed — system
self-awareness, the autonomy dial-in surface).

The endpoint reads chain_events `autonomy_decision` rows via the shared
`_get_larry_action_supabase_client`, mirroring the agent-queue lane reader. A
recording stub answers the
`table('chain_events').select(cols).eq('event_type', …).gte('ts', …)
.order('ts', desc=True).limit(N).execute()` chain; the None path exercises the
fail-safe degradation (present=False, 200 not 500).

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_automated_work
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}


class _TokenSetMixin:
    def setUp(self):  # noqa: D401 — unittest hook
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        super().setUp()


# ---------- supabase stub ----------

class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _AutonomyClient:
    """Answers the automated-work read chain:
    table('chain_events').select(cols).eq('event_type', 'autonomy_decision')
    .gte('ts', cutoff).order('ts', desc=True).limit(N).execute().

    execute() applies the eq + gte predicates against full rows, sorts by the
    `order` column (honoring desc), truncates to `limit`, then projects to the
    selected columns (so a query that forgets to select a column the reader
    reads fails here the same way it would in production).
    """

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None):
        self.rows = rows or []
        self._reset()

    def _reset(self):
        self._table: Optional[str] = None
        self._filters: dict[str, Any] = {}
        self._gte: dict[str, Any] = {}
        self._cols = '*'
        self._order: Optional[tuple[str, bool]] = None
        self._limit: Optional[int] = None

    def table(self, name: str):
        self._reset()
        self._table = name
        return self

    def select(self, cols: str = '*'):
        self._cols = cols
        return self

    def eq(self, col: str, val: Any):
        self._filters[col] = val
        return self

    def gte(self, col: str, val: Any):
        self._gte[col] = val
        return self

    def order(self, col: str, desc: bool = False):
        self._order = (col, desc)
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def execute(self):
        data = list(self.rows)
        for col, val in self._filters.items():
            data = [r for r in data if r.get(col) == val]
        for col, val in self._gte.items():
            data = [r for r in data
                    if r.get(col) is not None and r.get(col) >= val]
        if self._order is not None:
            col, desc = self._order
            data = sorted(data, key=lambda r: r.get(col) or '', reverse=desc)
        if self._limit is not None:
            data = data[: self._limit]
        if self._cols != '*':
            keep = [c.strip() for c in self._cols.split(',')]
            data = [{k: r.get(k) for k in keep} for r in data]
        return _Resp(data)


class _RaisingClient(_AutonomyClient):
    def execute(self):
        raise RuntimeError('transient supabase error')


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


_UNSET = object()  # distinguish "not passed" (auto-fill) from explicit None


def _row(task_id: str, *, decision: str, minutes_ago: int,
         dispatched: Optional[bool] = None, source: str = 'beacon',
         target_repo: str = 'ourliberty-agent-core',
         task_type: Optional[str] = 'feature-development',
         summary: str = 'Do the thing in plain English.',
         matched_rule: Any = _UNSET,
         pr_url: Optional[str] = None) -> dict[str, Any]:
    ts = _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    if dispatched is None:
        dispatched = decision == 'auto_approve'
    if matched_rule is _UNSET:
        # default: a representative matched rule (None must be passed explicitly
        # to exercise the default-policy label)
        matched_rule = None if decision == 'reject' else {
            'source': 'beacon', 'target': 'forge',
            'repos': ['ourliberty-agent-core'], 'action': decision,
        }
    return {
        'task_id': task_id,
        'ts': ts,
        'pr_url': pr_url,
        'event_type': 'autonomy_decision',
        'payload': {
            'decision': decision,
            'dispatched': dispatched,
            'source': source,
            'target_agent': 'forge',
            'target_repo': target_repo,
            'task_type': task_type,
            'summary': summary,
            'matched_rule': matched_rule,
        },
    }


def _client(supabase_client: Any = None) -> TestClient:
    da._get_larry_action_supabase_client = lambda: supabase_client  # type: ignore[assignment]
    return TestClient(da.app)


class _Base(_TokenSetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._orig_get_client = da._get_larry_action_supabase_client

    def tearDown(self):
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]


# ==================== auth ====================

class AuthTest(_Base):
    def test_requires_token(self):
        c = _client(_AutonomyClient([]))
        self.assertEqual(c.get('/api/system/automated-work').status_code, 401)
        self.assertEqual(
            c.get('/api/system/automated-work',
                  headers={'X-Dashboard-Token': 'wrong'}).status_code, 401)


# ==================== happy path ====================

class FeedTest(_Base):
    def test_mixed_window_items_are_auto_approve_only_recent_first(self):
        rows = [
            _row('aa-1', decision='auto_approve', minutes_ago=30),
            _row('aa-2', decision='auto_approve', minutes_ago=5),   # newest
            _row('ask-1', decision='force_ask', minutes_ago=20),
            _row('rej-1', decision='reject', minutes_ago=10),
            _row('aa-3', decision='auto_approve', minutes_ago=60),  # oldest
        ]
        c = _client(_AutonomyClient(rows))
        r = c.get('/api/system/automated-work', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['present'])
        # counts cover all three decisions over the window
        self.assertEqual(body['counts'],
                         {'auto_approved': 3, 'asked': 1, 'rejected': 1})
        # items are auto_approve only, most-recent first
        self.assertEqual([i['task_id'] for i in body['items']],
                         ['aa-2', 'aa-1', 'aa-3'])
        self.assertTrue(all(i['decision'] == 'auto_approve'
                            for i in body['items']))
        self.assertFalse(body['truncated'])
        # plain-language rule label + dispatched + age present
        top = body['items'][0]
        self.assertEqual(top['rule_label'],
                         'beacon → forge · ourliberty-agent-core')
        self.assertEqual(top['rule_action'], 'auto_approve')
        self.assertTrue(top['dispatched'])
        self.assertIsInstance(top['age_seconds'], int)
        self.assertEqual(top['target_repo'], 'ourliberty-agent-core')

    def test_default_policy_label_when_no_matched_rule(self):
        rows = [_row('aa-x', decision='auto_approve', minutes_ago=3,
                     matched_rule=None)]
        c = _client(_AutonomyClient(rows))
        body = c.get('/api/system/automated-work', headers=AUTH).json()
        item = body['items'][0]
        self.assertEqual(item['rule_label'], 'default policy')
        self.assertIsNone(item['rule_action'])

    def test_limit_truncates_items_not_counts(self):
        rows = [_row(f'aa-{i}', decision='auto_approve', minutes_ago=i)
                for i in range(1, 6)]  # 5 auto_approve
        c = _client(_AutonomyClient(rows))
        body = c.get('/api/system/automated-work',
                     headers=AUTH, params={'limit': 2}).json()
        self.assertEqual(len(body['items']), 2)
        self.assertTrue(body['truncated'])
        self.assertEqual(body['counts']['auto_approved'], 5)  # count is full

    def test_window_days_param_passthrough(self):
        body = _client(_AutonomyClient([])).get(
            '/api/system/automated-work',
            headers=AUTH, params={'window_days': 30}).json()
        self.assertEqual(body['window_days'], 30)


# ==================== fail-safe (never 500) ====================

class DegradationTest(_Base):
    def test_none_client_degrades_to_present_false(self):
        r = _client(None).get('/api/system/automated-work', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body['present'])
        self.assertEqual(body['items'], [])
        self.assertEqual(body['counts'],
                         {'auto_approved': 0, 'asked': 0, 'rejected': 0})

    def test_query_error_degrades_no_500(self):
        r = _client(_RaisingClient([])).get(
            '/api/system/automated-work', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['present'])


if __name__ == '__main__':
    unittest.main()
