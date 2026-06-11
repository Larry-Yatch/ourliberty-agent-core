#!/usr/bin/env python3
"""Tests for sync_agent_core.sh's new larry_alert envelope emission paths.

Closes 2026-05-26 'merged but not deployed' outage class (silent sync
failure). sync_agent_core.sh's existing alert_larry()->notify_larry.py call
is left in place; this PR adds a parallel larry_alerts.append_alert call at
each non-zero exit so Beacon's Telegram bot polls and DMs Larry on any
sync-gating condition.

These tests subprocess-invoke sync_agent_core.sh in a tmpdir-rooted fake
agent-core tree and assert: (a) the larry-alerts.jsonl envelope shape and
subject for the dedup key, (b) the script still exits non-zero so systemd
marks the unit failed.

Test isolation: HOME / REPO_DIR / LIVE_ROOT / STAGING_ROOT / BACKUP_ROOT are
all redirected at tmp_path via env vars. No writes to ~/agents/. Per PR
#137 test-isolation discipline.

Run:
    python3 -m unittest scripts.tests.test_sync_agent_core_branch_alert
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SYNC_SCRIPT = _REPO_ROOT / 'scripts' / 'sync_agent_core.sh'
_LARRY_ALERTS = _REPO_ROOT / 'scripts' / 'larry_alerts.py'
_LIB_PULSE = _REPO_ROOT / 'scripts' / '_lib_pulse_runtime.sh'
# sync_agent_core.sh sources this for the push rebase-fallback (c0e238e); the
# fake scripts dir must carry it or every run dies at the `source` line.
_LIB_PUSH = _REPO_ROOT / 'scripts' / '_lib_push_with_rebase.sh'


class _SyncTestBase(unittest.TestCase):
    """Builds an isolated agent-core git repo + larry_alerts queue location."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        # Fake HOME so larry_alerts writes into tmp_path/agents/blackboard/.
        self.home = self.tmp_path / 'home'
        self.home.mkdir()
        # Fake agent-core repo for REPO_DIR.
        self.repo_dir = self.tmp_path / 'agent-core'
        self.repo_dir.mkdir()
        # Scripts dir inside the fake repo — sync_agent_core.sh computes
        # SCRIPTS_DIR via $(dirname "$0"), so we copy the actual scripts
        # there and invoke that copy.
        self.scripts_dir = self.repo_dir / 'scripts'
        self.scripts_dir.mkdir()
        # Copy the production scripts the test exercises.
        (self.scripts_dir / 'sync_agent_core.sh').write_bytes(_SYNC_SCRIPT.read_bytes())
        (self.scripts_dir / 'larry_alerts.py').write_bytes(_LARRY_ALERTS.read_bytes())
        (self.scripts_dir / '_lib_pulse_runtime.sh').write_bytes(_LIB_PULSE.read_bytes())
        (self.scripts_dir / '_lib_push_with_rebase.sh').write_bytes(_LIB_PUSH.read_bytes())
        os.chmod(self.scripts_dir / 'sync_agent_core.sh', 0o755)
        # Init git, make an initial commit on main, then optionally switch
        # branches per the specific subtest.
        self._git('init', '-q', '--initial-branch=main')
        self._git('config', 'user.email', 'test@example.com')
        self._git('config', 'user.name', 'Test')
        (self.repo_dir / 'README').write_text('seed\n')
        self._git('add', 'README')
        self._git('commit', '-q', '-m', 'seed')
        # Other paths the script touches — point at tmp so we never write
        # to the real /home/larry/agents.
        self.live_root = self.tmp_path / 'agents'
        self.staging_root = self.live_root / '.sync-staging'
        self.backup_root = self.live_root / '.sync-backup'
        self.live_root.mkdir()
        # Blackboard dir for the sync status file.
        (self.live_root / 'blackboard').mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        subprocess.run(
            ['git', *args],
            cwd=self.repo_dir, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _run_sync(self):
        env = os.environ.copy()
        env['HOME'] = str(self.home)
        env['REPO_DIR'] = str(self.repo_dir)
        env['LIVE_ROOT'] = str(self.live_root)
        env['STAGING_ROOT'] = str(self.staging_root)
        env['BACKUP_ROOT'] = str(self.backup_root)
        return subprocess.run(
            ['bash', str(self.scripts_dir / 'sync_agent_core.sh')],
            env=env, cwd=self.repo_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )

    def _read_alerts(self):
        alerts_file = self.home / 'agents' / 'blackboard' / 'larry-alerts.jsonl'
        if not alerts_file.exists():
            return []
        out = []
        for line in alerts_file.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


class WrongBranchAlertTest(_SyncTestBase):
    def test_non_main_branch_emits_envelope_and_exits_nonzero(self):
        # Put the repo on a non-main branch.
        self._git('checkout', '-q', '-b', 'chore/foo')

        result = self._run_sync()

        # Existing semantics preserved: systemd-visible non-zero exit.
        self.assertNotEqual(result.returncode, 0)
        # New behavior: a larry_alerts envelope on the queue.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        record = alerts[0]
        self.assertEqual(record['source'], 'sync.service')
        self.assertEqual(record['severity'], 'warning')
        self.assertEqual(record['subject'], 'sync-blocked:wrong-branch:chore/foo')
        # Body should name the branch + give a recovery hint.
        self.assertIn('chore/foo', record['message'])
        self.assertIn('Recovery', record['message'])


class UncommittedChangesAlertTest(_SyncTestBase):
    def test_dirty_tree_on_main_emits_envelope_and_exits_nonzero(self):
        # Stay on main, but make the tree dirty.
        (self.repo_dir / 'README').write_text('dirty\n')

        result = self._run_sync()

        self.assertNotEqual(result.returncode, 0)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        record = alerts[0]
        self.assertEqual(record['source'], 'sync.service')
        self.assertEqual(record['severity'], 'warning')
        self.assertEqual(record['subject'], 'sync-blocked:uncommitted-changes')
        self.assertIn('Recovery', record['message'])


if __name__ == '__main__':
    unittest.main()
