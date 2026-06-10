#!/usr/bin/env python3
"""Tests for run_cycle.sh's wrong-branch guard.

Closes 2026-05-26 'merged but not deployed' outage class (Pulse cycle auto-
commit risk on the wrong branch). run_cycle.sh auto-commits cycle-journal /
cycle-actions / Pulse MEMORY on every successful cycle. If the tree is on
a non-main branch when this fires, those commits land on the feature branch
— silent corruption of branch history.

The new guard runs after lock acquisition, before claude --print:
  - main + clean  → pass through unchanged
  - non-main + clean → auto-restore (checkout main + ff-pull) + audit log
  - non-main + dirty → larry_alert + exit 1; do NOT touch the tree

These tests subprocess-invoke run_cycle.sh in a tmpdir-rooted fake agent-
core tree with a stub `claude` on PATH (so the cycle invocation exits
fast without trying to call the real claude CLI) and stub auto-commit by
running on a tree that has no Pulse-owned changes.

Test isolation: HOME / REPO_DIR are tmp_path-rooted; LOG_DIR / LOCK_DIR
follow from HOME inside the script. No writes to ~/agents/.

Run:
    python3 -m unittest scripts.tests.test_run_cycle_wrong_branch_guard
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ._runtime_script_test_support import (
    copy_larry_alerts_cli,
    install_timeout_shim,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RUN_CYCLE = _REPO_ROOT / 'scripts' / 'run_cycle.sh'
_LIB_PUSH = _REPO_ROOT / 'scripts' / '_lib_push_with_rebase.sh'
# run_cycle.sh sources _lib_pulse_runtime.sh for PULSE_RUNTIME_PATHS (the
# auto-commit path set, shared with sync_agent_core.sh); the fake scripts dir
# must carry it or the script dies at the `source` line under `set -e`.
_LIB_PULSE = _REPO_ROOT / 'scripts' / '_lib_pulse_runtime.sh'


_CLAUDE_STUB = '''#!/usr/bin/env bash
# Stub `claude` for run_cycle.sh tests. The cycle body is not under test;
# only the wrong-branch guard is. Emit a minimal JSON shape that satisfies
# the script's downstream cost-capture jq parsing, then exit 0.
echo '{"total_cost_usd": 0.0, "duration_ms": 1}'
exit 0
'''


class _RunCycleTestBase(unittest.TestCase):
    """Builds an isolated agent-core + origin + claude stub for run_cycle.sh."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.home = self.tmp_path / 'home'
        self.home.mkdir()
        # The cycle script expects ${HOME}/agent-core to exist.
        self.repo_dir = self.home / 'agent-core'
        self.repo_dir.mkdir()
        self.scripts_dir = self.repo_dir / 'scripts'
        self.scripts_dir.mkdir()
        # Pulse dir + cycle-prompt the script chdirs into.
        pulse_dir = self.repo_dir / 'agents' / 'pulse'
        pulse_dir.mkdir(parents=True)
        # Runbooks dir for cycle-actions.jsonl + cycle-prompt.
        runbooks_dir = self.repo_dir / 'runbooks'
        runbooks_dir.mkdir()
        (runbooks_dir / 'cycle-prompt.md').write_text('# stub\n')
        (runbooks_dir / 'cycle-journal.md').write_text('')
        (runbooks_dir / 'cycle-actions.jsonl').write_text('')
        # Copy real production scripts under test + their deps.
        for src in (_RUN_CYCLE, _LIB_PUSH, _LIB_PULSE):
            shutil.copy2(src, self.scripts_dir / src.name)
        # larry_alerts.py + its sibling-module deps (single source of truth).
        copy_larry_alerts_cli(self.scripts_dir)
        os.chmod(self.scripts_dir / 'run_cycle.sh', 0o755)
        # Set up a bare origin + clone for the auto-restore path (git pull
        # --ff-only needs an upstream).
        self.origin = self.tmp_path / 'origin.git'
        subprocess.run(
            ['git', 'init', '-q', '--bare', '--initial-branch=main', str(self.origin)],
            check=True,
        )
        # Init the repo, make a commit on main, push to origin.
        self._git('init', '-q', '--initial-branch=main')
        self._git('config', 'user.email', 'test@example.com')
        self._git('config', 'user.name', 'Test')
        (self.repo_dir / 'README').write_text('seed\n')
        self._git('add', 'README')
        self._git('commit', '-q', '-m', 'seed')
        self._git('remote', 'add', 'origin', str(self.origin))
        self._git('push', '-q', '-u', 'origin', 'main')
        # Stub claude on PATH so run_cycle.sh doesn't invoke the real one.
        self.stub_bin = self.tmp_path / 'stub-bin'
        self.stub_bin.mkdir()
        (self.stub_bin / 'claude').write_text(_CLAUDE_STUB)
        os.chmod(self.stub_bin / 'claude', 0o755)
        # run_cycle.sh wraps its alert CLI calls in `timeout 10 …`; shim it on
        # PATH (here, the same stub-bin as the claude stub) so the alert-asserting
        # subtests run where GNU `timeout` is absent (macOS).
        install_timeout_shim(self.stub_bin)

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        subprocess.run(
            ['git', *args],
            cwd=self.repo_dir, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _run_cycle(self):
        env = os.environ.copy()
        env['HOME'] = str(self.home)
        env['PATH'] = f'{self.stub_bin}:{env["PATH"]}'
        return subprocess.run(
            ['bash', str(self.scripts_dir / 'run_cycle.sh')],
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

    def _read_cycle_actions(self):
        actions = self.repo_dir / 'runbooks' / 'cycle-actions.jsonl'
        if not actions.exists():
            return []
        return [line for line in actions.read_text().splitlines() if line.strip()]

    def _current_branch(self):
        result = subprocess.run(
            ['git', '-C', str(self.repo_dir), 'rev-parse', '--abbrev-ref', 'HEAD'],
            check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()


class NonMainCleanTreeTest(_RunCycleTestBase):
    """non-main + clean tree → auto-restore to main, log, no alert."""

    def test_auto_restore_to_main(self):
        self._git('checkout', '-q', '-b', 'chore/foo')

        result = self._run_cycle()

        # Tree must be restored back to main.
        self.assertEqual(self._current_branch(), 'main')
        # Audit-log line written.
        actions = self._read_cycle_actions()
        restore_lines = [
            json.loads(ln) for ln in actions
            if 'auto-restored-main-from-chore/foo' in ln
        ]
        self.assertEqual(len(restore_lines), 1, f'expected one restore line, got {actions}')
        self.assertEqual(restore_lines[0]['event'], 'auto-restored-main-from-chore/foo')
        self.assertEqual(restore_lines[0]['actor'], 'run_cycle')
        # No larry_alert (auto-restore is quiet).
        self.assertEqual(self._read_alerts(), [])
        # Cycle proceeded past the guard (exit 0 from our claude stub).
        self.assertEqual(result.returncode, 0, f'stderr={result.stderr.decode()[:500]}')


class NonMainDirtyTreeTest(_RunCycleTestBase):
    """non-main + dirty tree → larry_alert + exit 1, never touch tree."""

    def test_alert_and_refuse_without_touching_tree(self):
        self._git('checkout', '-q', '-b', 'chore/bar')
        # Dirty the tree.
        (self.repo_dir / 'README').write_text('local-edit\n')

        result = self._run_cycle()

        # Must exit non-zero (cycle refuses to run).
        self.assertNotEqual(result.returncode, 0)
        # Tree must remain untouched: still on chore/bar, still dirty.
        self.assertEqual(self._current_branch(), 'chore/bar')
        dirty_check = subprocess.run(
            ['git', '-C', str(self.repo_dir), 'diff', '--quiet'],
            check=False,
        )
        self.assertNotEqual(dirty_check.returncode, 0, 'tree must still be dirty')
        # Alert emitted.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1, f'expected 1 alert, got {alerts}')
        record = alerts[0]
        self.assertEqual(record['source'], 'pulse-cycle')
        self.assertEqual(record['severity'], 'warning')
        self.assertEqual(record['subject'], 'cycle-blocked:dirty-tree-on-chore/bar')
        self.assertIn('chore/bar', record['message'])


class MainCleanTreePassThroughTest(_RunCycleTestBase):
    """main + clean tree → guard is a no-op, cycle runs unchanged."""

    def test_guard_is_no_op_on_main(self):
        # Already on main from setUp; tree is clean.
        result = self._run_cycle()

        # Cycle completed (the stub claude exits 0).
        self.assertEqual(result.returncode, 0, f'stderr={result.stderr.decode()[:500]}')
        # No alert, no auto-restore audit line.
        self.assertEqual(self._read_alerts(), [])
        self.assertEqual(self._read_cycle_actions(), [])
        # Tree still on main.
        self.assertEqual(self._current_branch(), 'main')


if __name__ == '__main__':
    unittest.main()
