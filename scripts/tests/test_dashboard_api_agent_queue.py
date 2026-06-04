#!/usr/bin/env python3
"""Tests for GET /api/system/agent-queue (Forge Queue panel, Phase 1).

Path-isolation pattern matches test_dashboard_api_system.py: a synthetic
AGENTS_ROOT + WORKTREES_ROOT tree under a tmpdir, monkeypatched directly
onto da._agents_root / da._worktrees_root. The supabase client is injected
via da._get_larry_action_supabase_client (a recording stub that answers the
chain_events select(...).eq('agent', ...) chain), or left as the None path
for the degradation test.

Covers docs/forge-queue-brief.md § Tests:
  - queued: count, oldest-first order, waited_seconds from mtime
  - building: filtered to agent + is_in_flight
  - in_review: review_request with no terminal event appears; one WITH a
    later auto_merge does not
  - done_today: merged vs failed classification; an event from yesterday is
    excluded (UTC boundary)
  - supabase-None degradation: in_review / done_today empty, no 500
  - auth: 401 without the token

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_agent_queue
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Set the token BEFORE importing dashboard_api so the auth dependency can
# resolve it at request time. _TokenSetMixin re-sets it in setUp so the fix
# survives sibling modules' teardown ordering under `unittest discover`.
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


class _ChainEventsClient:
    """Answers the dashboard's chain_events read chain:
    table('chain_events').select(cols).eq('agent', agent).execute().

    Pre-seeded with a flat list of rows; execute() returns the rows whose
    'agent' matches the eq filter.
    """

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None):
        self.rows = rows or []
        self._table: Optional[str] = None
        self._filters: dict[str, Any] = {}

    def table(self, name: str):
        self._table = name
        self._filters = {}
        return self

    def select(self, cols: str = '*'):
        return self

    def eq(self, col: str, val: Any):
        self._filters[col] = val
        return self

    def execute(self):
        agent = self._filters.get('agent')
        data = [r for r in self.rows if r.get('agent') == agent]
        return _Resp(data)


# ---------- fixtures ----------

def _build_agents_root(tmp: Path) -> Path:
    (tmp / 'state' / 'in-flight').mkdir(parents=True, exist_ok=True)
    for a in ('beacon', 'forge', 'mirror', 'pulse'):
        (tmp / 'inboxes' / a).mkdir(parents=True, exist_ok=True)
    return tmp


def _build_worktrees_root(tmp: Path) -> Path:
    wt = tmp / 'worktrees'
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _write_inbox_file(agents_root: Path, agent: str, name: str,
                      *, mtime: Optional[float] = None) -> Path:
    p = agents_root / 'inboxes' / agent / name
    p.write_text('{}')
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _write_in_flight(agents_root: Path, *, task_stem: str, agent_id: str,
                     pid: int = 4242,
                     started_at: str = '2026-06-03T07:00:00+00:00') -> None:
    import json
    p = agents_root / 'state' / 'in-flight' / f'{task_stem}.json'
    p.write_text(json.dumps({
        'task_stem': task_stem, 'agent_id': agent_id,
        'pid': pid, 'started_at': started_at,
    }))


def _make_worktree(worktrees_root: Path, name: str) -> None:
    (worktrees_root / name).mkdir(parents=True, exist_ok=True)


def _client(agents_root: Path, worktrees_root: Path,
            supabase_client: Any = None) -> TestClient:
    da._agents_root = lambda: agents_root  # type: ignore[assignment]
    da._worktrees_root = lambda: worktrees_root  # type: ignore[assignment]
    da._get_larry_action_supabase_client = lambda: supabase_client  # type: ignore[assignment]
    return TestClient(da.app)


class _Base(_TokenSetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._orig_agents_root = da._agents_root
        self._orig_worktrees_root = da._worktrees_root
        self._orig_get_client = da._get_larry_action_supabase_client
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-agent-queue-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')
        self.worktrees_root = _build_worktrees_root(self.tmp)

    def tearDown(self):
        da._agents_root = self._orig_agents_root  # type: ignore[assignment]
        da._worktrees_root = self._orig_worktrees_root  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]


# ==================== auth ====================

class AuthTest(_Base):
    def test_requires_token(self):
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue')
        self.assertEqual(r.status_code, 401)
        r = c.get('/api/system/agent-queue',
                  headers={'X-Dashboard-Token': 'wrong'})
        self.assertEqual(r.status_code, 401)


# ==================== agent param validation ====================

class AgentParamTest(_Base):
    def test_default_agent_is_forge(self):
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['agent'], 'forge')

    def test_unknown_agent_rejected(self):
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue',
                  headers=AUTH, params={'agent': 'bogus'})
        self.assertEqual(r.status_code, 400)


# ==================== queued lane ====================

class QueuedLaneTest(_Base):
    def test_oldest_first_order_and_waited_seconds(self):
        now = time.time()
        # Three real dispatches WITHOUT a `task-` prefix (the gotcha that
        # _agent_inbox_pending would miss). Distinct mtimes, out of order.
        _write_inbox_file(self.agents_root, 'forge', 'aaa.json', mtime=now - 30)
        _write_inbox_file(self.agents_root, 'forge', 'bbb.json', mtime=now - 300)
        _write_inbox_file(self.agents_root, 'forge', 'ccc.json', mtime=now - 90)
        # Noise the matcher must skip: dotfile + non-json.
        _write_inbox_file(self.agents_root, 'forge', '.hidden.json', mtime=now - 999)
        (self.agents_root / 'inboxes' / 'forge' / 'note.txt').write_text('x')

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        queued = r.json()['queued']
        self.assertEqual([q['task_id'] for q in queued], ['bbb', 'ccc', 'aaa'])
        # waited_seconds derived from mtime; oldest has the largest wait.
        self.assertGreater(queued[0]['waited_seconds'], queued[1]['waited_seconds'])
        self.assertGreater(queued[0]['waited_seconds'], 250)

    def test_other_agents_inbox_not_leaked(self):
        _write_inbox_file(self.agents_root, 'mirror', 'mmm.json')
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.json()['queued'], [])


# ==================== building lane ====================

class BuildingLaneTest(_Base):
    def test_filtered_to_agent_and_in_flight(self):
        # Worktree names follow `wt-<agent>-<task_id>` with the lowercase
        # task_id the reader's regex accepts.
        # forge in-flight worktree -> appears.
        _make_worktree(self.worktrees_root, 'wt-forge-build-thing-001')
        _write_in_flight(self.agents_root,
                         task_stem='build-thing-001', agent_id='forge')
        # forge worktree NOT in-flight -> excluded.
        _make_worktree(self.worktrees_root, 'wt-forge-stale-002')
        # mirror in-flight worktree -> excluded (wrong agent).
        _make_worktree(self.worktrees_root, 'wt-mirror-review-003')
        _write_in_flight(self.agents_root,
                         task_stem='review-003', agent_id='mirror')

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        building = r.json()['building']
        self.assertEqual(len(building), 1)
        self.assertEqual(building[0]['task_id'], 'build-thing-001')
        self.assertIn('branch', building[0])
        self.assertIn('age_seconds', building[0])


# ==================== in_review lane ====================

class InReviewLaneTest(_Base):
    def _rows(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return [
            # task A: review_request, no terminal -> in_review.
            {'agent': 'forge', 'task_id': 'taskA',
             'event_type': 'review_request', 'pr_url': 'https://pr/A',
             'ts': (now - timedelta(minutes=10)).isoformat()},
            # task B: review_request THEN auto_merge -> NOT in_review.
            {'agent': 'forge', 'task_id': 'taskB',
             'event_type': 'review_request', 'pr_url': 'https://pr/B',
             'ts': (now - timedelta(minutes=20)).isoformat()},
            {'agent': 'forge', 'task_id': 'taskB',
             'event_type': 'auto_merge', 'pr_url': 'https://pr/B',
             'ts': (now - timedelta(minutes=5)).isoformat()},
        ]

    def test_review_request_without_terminal_appears(self):
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(self._rows()))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        in_review = r.json()['in_review']
        ids = [x['task_id'] for x in in_review]
        self.assertEqual(ids, ['taskA'])
        self.assertEqual(in_review[0]['pr_url'], 'https://pr/A')
        self.assertIsNotNone(in_review[0]['since'])

    def test_only_requested_agent_rows(self):
        rows = self._rows() + [
            {'agent': 'mirror', 'task_id': 'taskZ',
             'event_type': 'review_request', 'pr_url': 'https://pr/Z',
             'ts': datetime.now(timezone.utc).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        ids = [x['task_id'] for x in r.json()['in_review']]
        self.assertEqual(ids, ['taskA'])


# ==================== done_today lane ====================

class DoneTodayLaneTest(_Base):
    def test_merged_failed_classification_and_utc_boundary(self):
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        rows = [
            {'agent': 'forge', 'task_id': 'merged1',
             'event_type': 'auto_merge', 'pr_url': 'https://pr/1',
             'ts': now.isoformat()},
            {'agent': 'forge', 'task_id': 'failed1',
             'event_type': 'marker_error', 'pr_url': 'https://pr/2',
             'ts': now.isoformat()},
            {'agent': 'forge', 'task_id': 'failed2',
             'event_type': 'cost_budget', 'pr_url': None,
             'ts': now.isoformat()},
            # yesterday's terminal event -> excluded by UTC boundary.
            {'agent': 'forge', 'task_id': 'old1',
             'event_type': 'auto_merge', 'pr_url': 'https://pr/old',
             'ts': yesterday.isoformat()},
            # non-terminal event today -> excluded.
            {'agent': 'forge', 'task_id': 'noise',
             'event_type': 'review_request', 'pr_url': 'https://pr/n',
             'ts': now.isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        done = r.json()['done_today']
        by_task = {d['task_id']: d for d in done}
        self.assertEqual(set(by_task), {'merged1', 'failed1', 'failed2'})
        self.assertEqual(by_task['merged1']['outcome'], 'merged')
        self.assertIsNone(by_task['merged1']['reason'])
        self.assertEqual(by_task['failed1']['outcome'], 'failed')
        self.assertEqual(by_task['failed1']['reason'], 'marker_error')
        self.assertEqual(by_task['failed2']['outcome'], 'failed')
        self.assertEqual(by_task['failed2']['reason'], 'cost_budget')


# ==================== supabase-None degradation ====================

class SupabaseNoneDegradationTest(_Base):
    def test_review_and_done_empty_no_500(self):
        # queued + building still populated; supabase lanes degrade to [].
        _write_inbox_file(self.agents_root, 'forge', 'qqq.json')
        c = _client(self.agents_root, self.worktrees_root, supabase_client=None)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body['queued']), 1)
        self.assertEqual(body['in_review'], [])
        self.assertEqual(body['done_today'], [])
        self.assertIn('captured_at', body)


if __name__ == '__main__':
    unittest.main()
