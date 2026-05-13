"""Tests for worktree_manager.py — keyed-reuse worktree creation."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Allow `import worktree_manager` from scripts/.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import worktree_manager  # noqa: E402


def _git(*args, cwd=None, check=True):
    """Run git with subprocess, return CompletedProcess."""
    return subprocess.run(
        ['git', *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, check=check, timeout=60,
    )


def _make_origin_repo(parent: Path) -> Path:
    """Create a bare-bones origin repo with one initial commit on main."""
    origin = parent / 'origin'
    origin.mkdir()
    _git('init', '-q', '--initial-branch=main', cwd=origin)
    _git('config', 'user.email', 'test@example.com', cwd=origin)
    _git('config', 'user.name', 'Test', cwd=origin)
    (origin / 'README.md').write_text('initial\n')
    _git('add', 'README.md', cwd=origin)
    _git('commit', '-q', '-m', 'initial commit', cwd=origin)
    return origin


def _make_canonical_clone(parent: Path, origin: Path) -> Path:
    """Clone origin to act as the canonical repo worktrees spawn from."""
    canonical = parent / 'canonical'
    _git('clone', '-q', str(origin), str(canonical))
    _git('config', 'user.email', 'test@example.com', cwd=canonical)
    _git('config', 'user.name', 'Test', cwd=canonical)
    return canonical


class PathHelperTest(unittest.TestCase):
    def test_sanitize_strips_unsafe_chars(self):
        self.assertEqual(
            worktree_manager._sanitize_task_id('feat/cool.thing'),
            'feat-cool-thing',
        )

    def test_sanitize_keeps_alnum_dash_underscore(self):
        self.assertEqual(
            worktree_manager._sanitize_task_id('abc_123-XYZ'),
            'abc_123-XYZ',
        )

    def test_sanitize_caps_length(self):
        long_id = 'a' * 200
        result = worktree_manager._sanitize_task_id(long_id)
        self.assertEqual(len(result), worktree_manager.MAX_TASK_ID_LEN)

    def test_sanitize_empty_falls_back(self):
        self.assertEqual(worktree_manager._sanitize_task_id(''), 'task')
        self.assertEqual(worktree_manager._sanitize_task_id(None), 'task')

    def test_sanitize_all_special_falls_back(self):
        # All chars get replaced with '-'; then the function returns the
        # truncated 'task' fallback only when sanitized is empty after
        # truncation. '!@#$%' becomes '-----'.
        self.assertEqual(worktree_manager._sanitize_task_id('!@#$%'), '-----')

    def test_worktree_path_deterministic(self):
        p1 = worktree_manager.worktree_path_for('forge', 'task-001')
        p2 = worktree_manager.worktree_path_for('forge', 'task-001')
        self.assertEqual(p1, p2)
        self.assertEqual(p1.name, 'wt-forge-task-001')

    def test_worktree_path_uses_base(self):
        p = worktree_manager.worktree_path_for('forge', 'x')
        self.assertEqual(p.parent, worktree_manager.WORKTREE_BASE)

    def test_derive_branch_name(self):
        self.assertEqual(
            worktree_manager.derive_branch_name('forge', 'watchdog-fix-001'),
            'forge/watchdog-fix-001',
        )

    def test_derive_branch_name_sanitizes(self):
        # Slashes in task_id are NOT allowed (treated as unsafe).
        self.assertEqual(
            worktree_manager.derive_branch_name('forge', 'feat/cool'),
            'forge/feat-cool',
        )


class WorktreeLifecycleTest(unittest.TestCase):
    """End-to-end tests using a real ephemeral git origin + canonical."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.origin = _make_origin_repo(self.tmpdir)
        self.canonical = _make_canonical_clone(self.tmpdir, self.origin)
        self._wt_base = self.tmpdir / 'wt'
        self._wt_base.mkdir()
        self._original_base = worktree_manager.WORKTREE_BASE
        worktree_manager.WORKTREE_BASE = self._wt_base
        self.logs: list[str] = []

    def tearDown(self):
        worktree_manager.WORKTREE_BASE = self._original_base
        self._tmp.cleanup()

    def _log_fn(self, msg: str) -> None:
        self.logs.append(msg)

    def test_create_fresh_worktree(self):
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-001', self.canonical, log_fn=self._log_fn,
        )
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.name, 'wt-forge-task-001')
        # README from origin/main should be in the worktree.
        self.assertTrue((path / 'README.md').exists())

    def test_reuse_same_task_id_returns_same_path(self):
        path1 = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-001', self.canonical, log_fn=self._log_fn,
        )
        path2 = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-001', self.canonical, log_fn=self._log_fn,
        )
        self.assertEqual(path1, path2)
        # The reuse path should log a reuse message.
        self.assertTrue(any('reusing worktree' in m for m in self.logs))

    def test_missing_canonical_returns_none(self):
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-001',
            self.tmpdir / 'does-not-exist',
            log_fn=self._log_fn,
        )
        self.assertIsNone(path)
        self.assertTrue(any('canonical_repo missing' in m for m in self.logs))

    def test_stale_dir_not_registered_is_removed(self):
        # Manually create a directory at the would-be worktree path that
        # isn't registered with git. ensure_worktree_for_task should clean
        # it up and create fresh.
        stale = worktree_manager.worktree_path_for('forge', 'task-stale')
        stale.mkdir()
        (stale / 'junk').write_text('not-a-worktree')

        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-stale', self.canonical, log_fn=self._log_fn,
        )
        self.assertIsNotNone(path)
        # The junk file should be gone (worktree was recreated fresh).
        self.assertFalse((path / 'junk').exists())
        self.assertTrue((path / 'README.md').exists())
        self.assertTrue(any('stale dir' in m for m in self.logs))

    def test_orphan_registry_entry_is_pruned(self):
        # Create worktree, then rm -rf its directory directly (simulating
        # external cleanup). ensure should prune + recreate.
        path1 = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-orphan', self.canonical, log_fn=self._log_fn,
        )
        self.assertIsNotNone(path1)
        # Remove the worktree directory but leave the git registry entry.
        import shutil
        shutil.rmtree(path1)
        self.logs.clear()

        path2 = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-orphan', self.canonical, log_fn=self._log_fn,
        )
        self.assertIsNotNone(path2)
        self.assertEqual(path1, path2)
        self.assertTrue(path2.exists())
        self.assertTrue(any('orphan worktree' in m for m in self.logs))

    def test_setup_branch_checkpoint_pushes(self):
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-branch', self.canonical, log_fn=self._log_fn,
        )
        # Need to allow push to origin (origin is bare-style; clone gives
        # it a non-bare working tree which rejects pushes by default).
        # Configure origin to accept pushes to currently-checked-out branch.
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        branch = worktree_manager.setup_branch_checkpoint(
            path, 'forge/task-branch', 'task-branch', log_fn=self._log_fn,
        )
        self.assertEqual(branch, 'forge/task-branch')

        # Branch should now exist on origin.
        r = _git('branch', '--list', 'forge/task-branch', cwd=self.origin)
        self.assertIn('forge/task-branch', r.stdout)

    def test_setup_branch_checkpoint_bad_branch_returns_none(self):
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-bad-branch', self.canonical, log_fn=self._log_fn,
        )
        # Empty branch name.
        result = worktree_manager.setup_branch_checkpoint(
            path, '', 'task-bad-branch', log_fn=self._log_fn,
        )
        self.assertIsNone(result)
        # Overlong branch name.
        result = worktree_manager.setup_branch_checkpoint(
            path, 'x' * 250, 'task-bad-branch', log_fn=self._log_fn,
        )
        self.assertIsNone(result)

    def test_setup_branch_checkpoint_missing_worktree_returns_none(self):
        result = worktree_manager.setup_branch_checkpoint(
            self.tmpdir / 'nope', 'forge/x', 'task', log_fn=self._log_fn,
        )
        self.assertIsNone(result)

    def test_ensure_worktree_with_branch(self):
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        path, branch = worktree_manager.ensure_worktree_for_task(
            'forge', 'task-ensure', self.canonical,
            branch='forge/task-ensure', log_fn=self._log_fn,
        )
        self.assertIsNotNone(path)
        self.assertEqual(branch, 'forge/task-ensure')

    def test_ensure_worktree_without_branch(self):
        path, branch = worktree_manager.ensure_worktree_for_task(
            'forge', 'task-nobranch', self.canonical, log_fn=self._log_fn,
        )
        self.assertIsNotNone(path)
        self.assertIsNone(branch)

    def test_ensure_worktree_idempotent_reuse(self):
        # Calling twice with same task_id reuses the worktree.
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        p1, b1 = worktree_manager.ensure_worktree_for_task(
            'forge', 'task-idem', self.canonical,
            branch='forge/task-idem', log_fn=self._log_fn,
        )
        p2, b2 = worktree_manager.ensure_worktree_for_task(
            'forge', 'task-idem', self.canonical,
            branch='forge/task-idem', log_fn=self._log_fn,
        )
        self.assertEqual(p1, p2)
        self.assertEqual(b1, b2)

    def test_list_worktrees_parses_porcelain(self):
        worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-list', self.canonical, log_fn=self._log_fn,
        )
        entries = worktree_manager._list_worktrees(self.canonical)
        paths = [e.get('path') for e in entries]
        # Should include the canonical itself + the new worktree.
        self.assertIn(str(self.canonical), paths)
        self.assertTrue(
            any('wt-forge-task-list' in p for p in paths if p),
        )

    def test_is_worktree_registered_after_create(self):
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-reg', self.canonical, log_fn=self._log_fn,
        )
        self.assertTrue(
            worktree_manager._is_worktree_registered(self.canonical, path),
        )

    def test_is_worktree_registered_for_unknown_path(self):
        self.assertFalse(
            worktree_manager._is_worktree_registered(
                self.canonical, self.tmpdir / 'not-a-worktree',
            )
        )


if __name__ == '__main__':
    unittest.main()
