#!/usr/bin/env python3
"""Tests for the /api/system/* endpoints (E4.4d PR-C).

Path-isolation pattern matches test_dashboard_api.py: synthetic
AGENTS_ROOT + CGROUP_BASE + WORKTREES_ROOT trees under a tmpdir, no
mutation of process-wide env (sibling test modules capture
OURLIBERTY_AGENTS_ROOT at their import time and assert stability).

Each TestClient instance monkeypatches `da._agents_root` /
`da._cgroup_base` / `da._worktrees_root` directly onto closures over the
test's tmpdir.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_dashboard_api_system
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
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Set the token BEFORE importing dashboard_api so the FastAPI app's
# auth dependency can resolve it at request time. Sibling test modules
# (e.g. test_dashboard_api.py) may pop this env var in their own
# tearDownModule(); each TestCase below re-sets it in setUp() so the
# fix survives any cross-module teardown ordering under
# `unittest discover`.
TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}


class _TokenSetMixin:
    """Re-set DASHBOARD_API_TOKEN in every setUp.

    test_dashboard_api.py's tearDownModule pops the env var unconditionally
    if its own _ORIGINAL_TOKEN snapshot was None at its import time —
    which it is under `unittest discover` since this module isn't loaded
    yet at that point. Re-setting in setUp is the most robust shape: it
    survives any teardown ordering and any other test module's env
    mutations.
    """

    def setUp(self):  # noqa: D401 — unittest hook
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        super().setUp()


# ---------- fixture helpers ----------


def _build_agents_root(tmp: Path) -> Path:
    (tmp / 'state' / 'in-flight').mkdir(parents=True, exist_ok=True)
    return tmp


def _build_cgroup(tmp: Path, *, pids: list[int],
                  memory_current: int = 1024 * 1024 * 100,
                  memory_peak: int = 1024 * 1024 * 200,
                  memory_max: str = '8589934592',
                  memory_high: str = '3221225472',
                  memory_events_max: int = 0,
                  memory_events_high: int = 7,
                  cpu_user_usec: int = 1234567,
                  cpu_system_usec: int = 89012) -> Path:
    cg = tmp / 'cgroup'
    cg.mkdir(parents=True, exist_ok=True)
    (cg / 'cgroup.procs').write_text('\n'.join(str(p) for p in pids) + '\n')
    (cg / 'memory.current').write_text(f'{memory_current}\n')
    (cg / 'memory.peak').write_text(f'{memory_peak}\n')
    (cg / 'memory.max').write_text(f'{memory_max}\n')
    (cg / 'memory.high').write_text(f'{memory_high}\n')
    (cg / 'memory.events').write_text(
        f'low 0\nhigh {memory_events_high}\nmax {memory_events_max}\noom 0\noom_kill 0\n'
    )
    (cg / 'cpu.stat').write_text(
        f'usage_usec {cpu_user_usec + cpu_system_usec}\n'
        f'user_usec {cpu_user_usec}\n'
        f'system_usec {cpu_system_usec}\n'
    )
    return cg


def _build_worktrees_root(tmp: Path, names: list[str]) -> Path:
    wt = tmp / 'worktrees'
    wt.mkdir(parents=True, exist_ok=True)
    for n in names:
        (wt / n).mkdir(parents=True, exist_ok=True)
    return wt


def _write_in_flight(agents_root: Path, *, task_stem: str, agent_id: str,
                      pid: int, started_at: str, task_type: str | None = None) -> None:
    entry = {
        'task_stem': task_stem,
        'agent_id': agent_id,
        'pid': pid,
        'started_at': started_at,
    }
    if task_type is not None:
        entry['task_type'] = task_type
    p = agents_root / 'state' / 'in-flight' / f'{task_stem}.json'
    p.write_text(json.dumps(entry))


def _client(testcase: unittest.TestCase, agents_root: Path, cgroup_base: Path,
            worktrees_root: Path) -> TestClient:
    """Build a TestClient with da's path resolvers pointed at the test's
    synthetic trees.

    The resolvers are patched via ``mock.patch.object`` and restored via
    ``testcase.addCleanup`` so the originals are put back at the end of
    each test. A bare ``da._agents_root = lambda: ...`` with no restore
    leaks the (soon-deleted) tmpdir into every later-discovered module
    under ``unittest discover`` (e.g. heal_orphan_autoregister's queue-dir
    parity check would read the stale path instead of its own env).
    """
    for attr, val in (
        ('_agents_root', agents_root),
        ('_cgroup_base', cgroup_base),
        ('_worktrees_root', worktrees_root),
    ):
        p = mock.patch.object(da, attr, lambda v=val: v)
        p.start()
        testcase.addCleanup(p.stop)
    return TestClient(da.app)


# ==================== auth ====================


class SystemAuthTest(_TokenSetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-sys-auth-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')
        self.cg = _build_cgroup(self.tmp, pids=[])
        self.wt = _build_worktrees_root(self.tmp, [])
        self.c = _client(self, self.agents_root, self.cg, self.wt)

    def test_active_sessions_requires_token(self):
        r = self.c.get('/api/system/active-sessions')
        self.assertEqual(r.status_code, 401)
        r = self.c.get('/api/system/active-sessions', headers={'X-Dashboard-Token': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_cgroup_stats_requires_token(self):
        r = self.c.get('/api/system/cgroup-stats')
        self.assertEqual(r.status_code, 401)
        r = self.c.get('/api/system/cgroup-stats', headers={'X-Dashboard-Token': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_worktrees_requires_token(self):
        r = self.c.get('/api/system/worktrees')
        self.assertEqual(r.status_code, 401)
        r = self.c.get('/api/system/worktrees', headers={'X-Dashboard-Token': 'wrong'})
        self.assertEqual(r.status_code, 401)


# ==================== /api/system/active-sessions ====================


class ActiveSessionsTest(_TokenSetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-sys-as-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')

    def test_empty_slice(self):
        cg = _build_cgroup(self.tmp, pids=[])
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r = c.get('/api/system/active-sessions', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['sessions'], [])
        self.assertIn('captured_at', body)

    def test_session_cross_referenced_with_in_flight(self):
        # Use our own PID — guaranteed to exist for /proc/<pid>/cmdline.
        my_pid = os.getpid()
        _write_in_flight(
            self.agents_root,
            task_stem='build-e4-4d-pr-c-droplet-api',
            agent_id='forge',
            pid=my_pid,
            started_at='2026-05-26T07:00:00+00:00',
            task_type='feature-development',
        )
        cg = _build_cgroup(self.tmp, pids=[my_pid])
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r = c.get('/api/system/active-sessions', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body['sessions']), 1)
        s = body['sessions'][0]
        self.assertEqual(s['pid'], my_pid)
        self.assertEqual(s['agent'], 'forge')
        self.assertEqual(s['task_id'], 'build-e4-4d-pr-c-droplet-api')
        self.assertEqual(s['task_type'], 'feature-development')
        self.assertEqual(s['started_at'], '2026-05-26T07:00:00+00:00')
        # duration computed live, must be a non-negative float.
        self.assertIsInstance(s['duration_sec'], (int, float))
        self.assertGreater(s['duration_sec'], 0)
        # Raw signals only — droplet must NOT emit stuck/stuck_reason.
        self.assertNotIn('stuck', s)
        self.assertNotIn('stuck_reason', s)

    def test_dead_pid_omitted_not_500(self):
        # A PID guaranteed to not exist on this system.
        dead_pid = 999999999
        cg = _build_cgroup(self.tmp, pids=[dead_pid])
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r = c.get('/api/system/active-sessions', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        # PID has no in-flight entry AND cmdline read fails → omitted.
        self.assertEqual(r.json()['sessions'], [])

    def test_slice_missing_returns_503_structured_body(self):
        # Point cgroup_base at a path that doesn't exist.
        missing = self.tmp / 'no-such-cgroup'
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, missing, wt)
        r = c.get('/api/system/active-sessions', headers=AUTH)
        self.assertEqual(r.status_code, 503)
        detail = r.json()['detail']
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail['error'], 'service-unavailable')
        self.assertIn('message', detail)


# ==================== /api/system/cgroup-stats ====================


class CgroupStatsTest(_TokenSetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-sys-cg-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')

    def test_happy_path_shape(self):
        cg = _build_cgroup(
            self.tmp, pids=[],
            memory_current=3124076544,
            memory_peak=4489773056,
            memory_max='8589934592',
            memory_high='3221225472',
            memory_events_max=868,
            memory_events_high=1247,
            cpu_user_usec=6678234567,
            cpu_system_usec=891234567,
        )
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r = c.get('/api/system/cgroup-stats', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['memory_current_bytes'], 3124076544)
        self.assertEqual(body['memory_peak_bytes'], 4489773056)
        self.assertEqual(body['memory_max_bytes'], 8589934592)
        self.assertEqual(body['memory_high_bytes'], 3221225472)
        self.assertEqual(body['memory_events_max'], 868)
        self.assertEqual(body['memory_events_high'], 1247)
        self.assertEqual(body['cpu_user_usec'], 6678234567)
        self.assertEqual(body['cpu_system_usec'], 891234567)
        self.assertIn('captured_at', body)

    def test_memory_max_sentinel_surfaces_as_null(self):
        cg = _build_cgroup(self.tmp, pids=[], memory_max='max', memory_high='max')
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r = c.get('/api/system/cgroup-stats', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body['memory_max_bytes'])
        self.assertIsNone(body['memory_high_bytes'])

    def test_slice_stopped_returns_503_structured_body(self):
        missing = self.tmp / 'no-such-cgroup'
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, missing, wt)
        r = c.get('/api/system/cgroup-stats', headers=AUTH)
        self.assertEqual(r.status_code, 503)
        detail = r.json()['detail']
        self.assertEqual(detail['error'], 'service-unavailable')
        self.assertIn('message', detail)


# ==================== /api/system/worktrees ====================


class WorktreesTest(_TokenSetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-sys-wt-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')

    def test_empty_worktrees_root(self):
        cg = _build_cgroup(self.tmp, pids=[])
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r = c.get('/api/system/worktrees', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['worktrees'], [])
        self.assertIn('captured_at', body)

    def test_worktrees_parsed_and_in_flight_marked(self):
        cg = _build_cgroup(self.tmp, pids=[])
        wt = _build_worktrees_root(self.tmp, [
            'wt-forge-build-e4-4d-pr-c-droplet-api',
            'wt-mirror-review-pr-104-e4-4d-system-tab-spec',
            'some-other-dir',  # ignored (no wt- prefix)
        ])
        _write_in_flight(
            self.agents_root,
            task_stem='build-e4-4d-pr-c-droplet-api',
            agent_id='forge',
            pid=999999,
            started_at='2026-05-26T07:00:00+00:00',
        )
        c = _client(self, self.agents_root, cg, wt)
        r = c.get('/api/system/worktrees', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        names = [w['name'] for w in body['worktrees']]
        self.assertIn('wt-forge-build-e4-4d-pr-c-droplet-api', names)
        self.assertIn('wt-mirror-review-pr-104-e4-4d-system-tab-spec', names)
        self.assertNotIn('some-other-dir', names)
        forge_row = next(w for w in body['worktrees'] if w['agent'] == 'forge')
        self.assertEqual(forge_row['task_id'], 'build-e4-4d-pr-c-droplet-api')
        self.assertEqual(forge_row['branch'], 'forge/build-e4-4d-pr-c-droplet-api')
        self.assertTrue(forge_row['is_in_flight'])
        self.assertIsInstance(forge_row['age_seconds'], (int, float))
        mirror_row = next(w for w in body['worktrees'] if w['agent'] == 'mirror')
        self.assertFalse(mirror_row['is_in_flight'])

    def test_worktrees_root_missing_returns_empty_not_500(self):
        cg = _build_cgroup(self.tmp, pids=[])
        missing = self.tmp / 'no-such-worktrees-root'
        c = _client(self, self.agents_root, cg, missing)
        r = c.get('/api/system/worktrees', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['worktrees'], [])


# ==================== uncached behavior ====================


class UncachedTest(_TokenSetMixin, unittest.TestCase):
    """Each request must re-read filesystem. No in-process caching."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-sys-cache-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')

    def test_cgroup_stats_re_reads_each_request(self):
        cg = _build_cgroup(self.tmp, pids=[], memory_current=100)
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r1 = c.get('/api/system/cgroup-stats', headers=AUTH)
        self.assertEqual(r1.json()['memory_current_bytes'], 100)
        # Mutate the file mid-flight; second request must reflect it.
        (cg / 'memory.current').write_text('999\n')
        r2 = c.get('/api/system/cgroup-stats', headers=AUTH)
        self.assertEqual(r2.json()['memory_current_bytes'], 999)

    def test_worktrees_re_reads_each_request(self):
        cg = _build_cgroup(self.tmp, pids=[])
        wt = _build_worktrees_root(self.tmp, [])
        c = _client(self, self.agents_root, cg, wt)
        r1 = c.get('/api/system/worktrees', headers=AUTH)
        self.assertEqual(r1.json()['worktrees'], [])
        (wt / 'wt-forge-new-task-001').mkdir()
        r2 = c.get('/api/system/worktrees', headers=AUTH)
        self.assertEqual(len(r2.json()['worktrees']), 1)


if __name__ == '__main__':
    unittest.main()
