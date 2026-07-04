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

try:  # dotted/pytest path: relative import within the scripts.tests package
    from ._runtime_script_test_support import (
        copy_larry_alerts_cli,
        install_timeout_shim,
        scrub_run_sentinel,
        redirect_agents_root,
    )
except ImportError:  # discover loads this module top-level (no package parent)
    from _runtime_script_test_support import (
        copy_larry_alerts_cli,
        install_timeout_shim,
        scrub_run_sentinel,
        redirect_agents_root,
    )


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SYNC_SCRIPT = _REPO_ROOT / 'scripts' / 'sync_agent_core.sh'
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
        # larry_alerts.py + its sibling-module deps (atomic_io, file_lock,
        # test_isolation_guard) via the single source of truth, so the envelope
        # CLI imports cleanly when sync shells out to it.
        copy_larry_alerts_cli(self.scripts_dir)
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

        # sync_agent_core.sh wraps its alert CLI calls in `timeout 10 …`; shim it
        # on PATH so the alert-asserting subtests run where GNU `timeout` is
        # absent (macOS). See _runtime_script_test_support.
        self.shim_bin = self.tmp_path / 'bin'
        self.shim_bin.mkdir()
        install_timeout_shim(self.shim_bin)

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
        env['PATH'] = f"{self.shim_bin}{os.pathsep}{env.get('PATH', '')}"
        # HOME-sandboxed tree → let the larry_alerts subprocess emit for real
        # (subprocess equivalent of test_isolation_guard.allow()); the inherited
        # run sentinel would otherwise make the choke guard drop the alert.
        scrub_run_sentinel(env)
        redirect_agents_root(env)
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
    def test_dirty_tree_two_consecutive_ticks_emit_envelope_and_exit_nonzero(self):
        # One-tick grace: the FIRST dirty tick refuses (non-zero) but defers the
        # alert — the dirt almost always clears by the next tick when Pulse's
        # auto-commit / the GC healer lands. Stay on main, dirty the tree.
        (self.repo_dir / 'README').write_text('dirty\n')

        first = self._run_sync()
        # Safety refusal is unconditional: sync still exits non-zero so it never
        # pulls onto a dirty tree.
        self.assertNotEqual(first.returncode, 0)
        # ...but no alert fired on the first occurrence.
        self.assertEqual(
            self._read_alerts(), [],
            'first dirty tick must defer the alert (one-tick grace)',
        )

        # Second consecutive dirty tick: still dirty, so now it pages.
        second = self._run_sync()
        self.assertNotEqual(second.returncode, 0)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        record = alerts[0]
        self.assertEqual(record['source'], 'sync.service')
        self.assertEqual(record['severity'], 'warning')
        self.assertEqual(record['subject'], 'sync-blocked:uncommitted-changes')
        self.assertIn('Recovery', record['message'])

    def test_early_exit_clears_stale_grace_marker(self):
        # A prior dirty tick may have set the one-tick grace marker. A later tick
        # that exits EARLY for a different reason (here: wrong branch, which
        # returns before the uncommitted-changes block) must still clear the
        # marker via the EXIT trap — otherwise a stale marker would spuriously
        # page on a future genuine FIRST dirty tick. The trap covers every
        # non-dirty-refuse exit, so wrong-branch is representative of the class
        # (auto-commit-push-failed, validation/quiescence abort, etc.).
        marker = (
            self.live_root / 'blackboard' / 'agent-core-sync-uncommitted-grace'
        )
        # Tick 1: dirty on main → first dirty tick sets the grace marker.
        (self.repo_dir / 'README').write_text('dirty\n')
        first = self._run_sync()
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(
            marker.exists(), 'first dirty tick should set the grace marker',
        )

        # Tick 2: now on a non-main branch (tree still dirty) → early wrong-branch
        # exit, which never reaches the uncommitted block.
        self._git('checkout', '-q', '-b', 'chore/foo')
        second = self._run_sync()
        self.assertNotEqual(second.returncode, 0)
        self.assertFalse(
            marker.exists(),
            'a wrong-branch (early-exit) tick must clear a stale grace marker',
        )


if __name__ == '__main__':
    unittest.main()
