#!/usr/bin/env python3
"""Tests for POST /api/system/agent-queue/{agent}/fast-track and the
_handle_fast_track / queued-lane ordering it drives (forge-queue-fast-track).

Path-isolation pattern matches test_dashboard_api_agent_queue.py.
Auth pattern (X-Dashboard-Token + allowlisted X-Actor) matches
test_dashboard_api_capture_actions.py.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_fast_track
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}


def _endpoint(agent: str = 'forge') -> str:
    return f'/api/system/agent-queue/{agent}/fast-track'


def _write_inbox_file(agents_root: Path, agent: str, name: str,
                      *, body: Optional[dict] = None,
                      mtime: Optional[float] = None) -> Path:
    p = agents_root / 'inboxes' / agent / name
    p.write_text(json.dumps(body if body is not None else {'task_id': name[:-5]}))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class _Base(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self._orig_agents_root = da._agents_root
        self._orig_worktrees_root = da._worktrees_root
        self._orig_get_client = da._get_larry_action_supabase_client
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-fast-track-'))
        self.agents_root = self.tmp / 'agents'
        for a in ('beacon', 'forge', 'mirror', 'pulse'):
            (self.agents_root / 'inboxes' / a).mkdir(parents=True, exist_ok=True)
        (self.agents_root / 'state' / 'in-flight').mkdir(parents=True, exist_ok=True)
        self.worktrees_root = self.tmp / 'worktrees'
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        da._agents_root = lambda: self.agents_root  # type: ignore[assignment]
        da._worktrees_root = lambda: self.worktrees_root  # type: ignore[assignment]
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        self.client = TestClient(da.app)

    def tearDown(self):
        da._agents_root = self._orig_agents_root  # type: ignore[assignment]
        da._worktrees_root = self._orig_worktrees_root  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]


# ==================== auth ====================

class AuthTest(_Base):
    def test_requires_token(self):
        _write_inbox_file(self.agents_root, 'forge', 'aaa.json')
        r = self.client.post(_endpoint(), json={'task_id': 'aaa'},
                             headers={'X-Actor': ACTOR})
        self.assertEqual(r.status_code, 401)
        r = self.client.post(_endpoint(), json={'task_id': 'aaa'},
                             headers={'X-Dashboard-Token': 'wrong', 'X-Actor': ACTOR})
        self.assertEqual(r.status_code, 401)

    def test_requires_allowlisted_actor(self):
        _write_inbox_file(self.agents_root, 'forge', 'aaa.json')
        r = self.client.post(_endpoint(), json={'task_id': 'aaa'},
                             headers={'X-Dashboard-Token': TOKEN})
        self.assertEqual(r.status_code, 401)
        r = self.client.post(_endpoint(), json={'task_id': 'aaa'},
                             headers={'X-Dashboard-Token': TOKEN,
                                      'X-Actor': 'mallory@evil.test'})
        self.assertEqual(r.status_code, 401)


# ==================== validation ====================

class ValidationTest(_Base):
    def test_unknown_agent_rejected(self):
        r = self.client.post(_endpoint('bogus'), json={'task_id': 'aaa'},
                             headers=AUTH)
        self.assertEqual(r.status_code, 400)

    def test_path_traversal_and_bad_task_ids_rejected(self):
        for bad in ('../../../../etc/pwned', 'a/b', '..', '.hidden', '', 'a\\b'):
            r = self.client.post(_endpoint(), json={'task_id': bad}, headers=AUTH)
            self.assertEqual(r.status_code, 400, f'task_id={bad!r}')

    def test_not_queued_is_409(self):
        # No such file in the inbox (already building / never existed).
        r = self.client.post(_endpoint(), json={'task_id': 'ghost'}, headers=AUTH)
        self.assertEqual(r.status_code, 409)


# ==================== happy path + side effects ====================

class FastTrackEffectTest(_Base):
    def test_sets_stamp_preserves_mtime_and_floats_to_front(self):
        now = time.time()
        # q1 older, q2 newer -> normal FIFO order is [q1, q2].
        _write_inbox_file(self.agents_root, 'forge', 'q1.json', mtime=now - 300)
        _write_inbox_file(self.agents_root, 'forge', 'q2.json', mtime=now - 100)

        r = self.client.post(_endpoint(), json={'task_id': 'q2'}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['task_id'], 'q2')
        self.assertTrue(body['fast_tracked_at'])

        # File now carries the stamp...
        q2 = self.agents_root / 'inboxes' / 'forge' / 'q2.json'
        payload = json.loads(q2.read_text())
        self.assertEqual(payload['fast_tracked_at'], body['fast_tracked_at'])
        # ...and the original mtime is preserved (waited_seconds stays honest).
        self.assertAlmostEqual(q2.stat().st_mtime, now - 100, delta=2.0)

        # The queued lane now floats q2 to the head despite being the newer file.
        gr = self.client.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(gr.status_code, 200)
        queued = gr.json()['queued']
        self.assertEqual([q['task_id'] for q in queued], ['q2', 'q1'])
        self.assertEqual(queued[0]['fast_tracked_at'], body['fast_tracked_at'])
        self.assertIsNone(queued[1]['fast_tracked_at'])

    def test_lifo_newest_click_wins(self):
        # Drive ordering deterministically at the handler level with explicit
        # `now` stamps (HTTP sub-microsecond ties would be flaky).
        now = time.time()
        _write_inbox_file(self.agents_root, 'forge', 'a.json', mtime=now - 30)
        _write_inbox_file(self.agents_root, 'forge', 'b.json', mtime=now - 20)
        _write_inbox_file(self.agents_root, 'forge', 'c.json', mtime=now - 10)

        t0 = datetime(2026, 6, 23, 10, 0, 0, tzinfo=timezone.utc)
        da._handle_fast_track(self.agents_root, 'forge', 'a', now=t0)
        da._handle_fast_track(self.agents_root, 'forge', 'b',
                              now=t0 + timedelta(seconds=5))

        ordered = da._reader_agent_queue_queued(self.agents_root, 'forge')
        # b clicked last -> head; a clicked first -> second; c never -> last.
        self.assertEqual([x['task_id'] for x in ordered], ['b', 'a', 'c'])

    def test_refast_track_restamps_and_returns_to_head(self):
        now = time.time()
        _write_inbox_file(self.agents_root, 'forge', 'a.json', mtime=now - 30)
        _write_inbox_file(self.agents_root, 'forge', 'b.json', mtime=now - 20)

        t0 = datetime(2026, 6, 23, 10, 0, 0, tzinfo=timezone.utc)
        da._handle_fast_track(self.agents_root, 'forge', 'a', now=t0)
        da._handle_fast_track(self.agents_root, 'forge', 'b',
                              now=t0 + timedelta(seconds=5))
        # b at head. Now re-fast-track a with a newer stamp -> a back to head,
        # b drops to second (NOT un-fast-tracked).
        da._handle_fast_track(self.agents_root, 'forge', 'a',
                              now=t0 + timedelta(seconds=10))
        ordered = da._reader_agent_queue_queued(self.agents_root, 'forge')
        self.assertEqual([x['task_id'] for x in ordered], ['a', 'b'])
        self.assertTrue(all(x['fast_tracked_at'] for x in ordered))


if __name__ == '__main__':
    unittest.main()
