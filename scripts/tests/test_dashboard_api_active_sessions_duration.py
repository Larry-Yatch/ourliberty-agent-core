#!/usr/bin/env python3
"""Regression tests: dashboard /api/system/active-sessions duration math.

Consumer-side guard for the 2026-05-26 timezone bug. The producer fix
(agent_runner writes tz-aware UTC) is necessary; this test confirms the
consumer math then yields a duration matching wall-clock — and catches
the +6h drift class that the bug produced when agent_runner wrote
MDT-naive `started_at`.

Calls `_reader_system_active_sessions` directly (no FastAPI client) with
synthetic agents_root + cgroup tree, mirroring fixtures in
test_dashboard_api_system.py. Uses `os.getpid()` so the cgroup.procs PID
has a readable /proc/<pid>/cmdline.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_active_sessions_duration
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api as da  # noqa: E402


def _build_agents_root(tmp: Path) -> Path:
    (tmp / 'state' / 'in-flight').mkdir(parents=True, exist_ok=True)
    return tmp


def _build_cgroup(tmp: Path, pids: list[int]) -> Path:
    cg = tmp / 'cgroup'
    cg.mkdir(parents=True, exist_ok=True)
    (cg / 'cgroup.procs').write_text('\n'.join(str(p) for p in pids) + '\n')
    return cg


def _write_in_flight(agents_root: Path, *, task_stem: str, pid: int,
                      started_at: str, agent_id: str = 'forge') -> None:
    entry = {
        'task_stem': task_stem,
        'agent_id': agent_id,
        'pid': pid,
        'started_at': started_at,
    }
    (agents_root / 'state' / 'in-flight' / f'{task_stem}.json').write_text(
        json.dumps(entry)
    )


SIX_HOURS_SEC = 6 * 60 * 60


class ActiveSessionsDurationTest(unittest.TestCase):
    """End-to-end: in-flight 'started_at' → reader → 'duration_sec'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-active-dur-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')
        self.my_pid = os.getpid()
        self.cgroup = _build_cgroup(self.tmp, pids=[self.my_pid])
        self.captured_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.started_at = self.captured_at - timedelta(seconds=60)

    def test_utc_aware_started_at_yields_wall_clock_duration(self):
        """Producer wrote tz-aware UTC (post-fix shape) — duration ≈ 60s."""
        _write_in_flight(
            self.agents_root,
            task_stem='post-fix-task',
            pid=self.my_pid,
            started_at=self.started_at.isoformat(),
        )
        body = da._reader_system_active_sessions(
            self.agents_root, self.cgroup, now=self.captured_at,
        )
        self.assertEqual(len(body['sessions']), 1)
        duration = body['sessions'][0]['duration_sec']
        self.assertIsNotNone(duration)
        # ≈ 60s (small epsilon for any rounding inside the reader).
        self.assertAlmostEqual(duration, 60.0, delta=2.0)
        # Crucially: NOT the +6h shape the original bug produced. Guards
        # against any future change that reintroduces the drift class.
        self.assertNotAlmostEqual(
            duration, 60.0 + SIX_HOURS_SEC, delta=60.0,
            msg=f'duration_sec={duration} matches the +6h drift class — '
                f'check producer timezone discipline',
        )

    def test_naive_started_at_treated_as_utc_documents_consumer_contract(self):
        """Consumer contract sanity: _ts_to_dt assumes UTC for naive
        datetimes (line 540 of dashboard_api.py). If a producer ever
        regresses to naive, the dashboard math is STILL correct as long
        as the producer is also on UTC (droplet is on MDT, so this is
        belt-and-braces — the fix is on the producer; this test guards
        the consumer side of the contract).
        """
        _write_in_flight(
            self.agents_root,
            task_stem='naive-task',
            pid=self.my_pid,
            started_at=self.started_at.replace(tzinfo=None).isoformat(),
        )
        body = da._reader_system_active_sessions(
            self.agents_root, self.cgroup, now=self.captured_at,
        )
        self.assertEqual(len(body['sessions']), 1)
        duration = body['sessions'][0]['duration_sec']
        self.assertIsNotNone(duration)
        # _ts_to_dt assumes UTC for naive — duration matches wall-clock.
        self.assertAlmostEqual(duration, 60.0, delta=2.0)


if __name__ == '__main__':
    unittest.main()
