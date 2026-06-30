#!/usr/bin/env python3
"""Tests for `cleanup_stale_worktrees.sweep_orphan_locked_worktrees`.

The regression gate / baseline warmer create detached worktrees under /tmp via
`git worktree add`, which writes a `locked` file (reason 'initializing') for the
duration of the add. A SIGKILL mid-init leaves that lock in place; `git worktree
prune` SKIPS locked entries and the in-process teardown never runs, so the
`.git/worktrees/<name>` metadata leaks forever. An entry killed early enough to
leave an all-zero HEAD corrupts `git fetch`, blocking every push to origin/main.

These tests prove the reaper:
  - removes a kill-mid-init orphan (locked + working dir absent),
  - removes the zero-HEAD variant AND unblocks `git fetch`,
  - NEVER touches an entry whose working dir still exists (the live-run
    safety invariant — the load-bearing test for the scope guarantee).

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_cleanup_orphan_locked_worktrees
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cleanup_stale_worktrees as csw  # noqa: E402

_ZERO_HEAD = '0' * 40


def _git(*args, cwd=None, check=True):
    return subprocess.run(
        ['git', *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, check=check, timeout=60,
    )


def _make_origin_repo(parent: Path) -> Path:
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
    canonical = parent / 'canonical'
    _git('clone', '-q', str(origin), str(canonical))
    _git('config', 'user.email', 'test@example.com', cwd=canonical)
    _git('config', 'user.name', 'Test', cwd=canonical)
    return canonical


class OrphanLockedWorktreeReaperTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix='orphan-locked-reaper-test-'))
        self.origin = _make_origin_repo(self.tmpdir)
        self.canonical = _make_canonical_clone(self.tmpdir, self.origin)
        self.workdirs = self.tmpdir / 'workdirs'
        self.workdirs.mkdir()
        # sweep_orphan_locked_worktrees logs via csw.LOG_FILE, a hardcoded
        # /home/larry path; redirect it so the test never appends to the real
        # droplet worktree-cleanup.log.
        _log_patch = mock.patch.object(
            csw, 'LOG_FILE', self.tmpdir / 'worktree-cleanup.log')
        _log_patch.start()
        self.addCleanup(_log_patch.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _meta_dir(self, name: str) -> Path:
        return self.canonical / '.git' / 'worktrees' / name

    def _provision(
        self, name: str, *, locked: bool = True,
        zero_head: bool = False, remove_workdir: bool = True,
    ) -> tuple[Path, Path]:
        """Create a real detached worktree, then simulate a kill-mid-init orphan.

        Returns (metadata_dir, working_dir).
        """
        workdir = self.workdirs / name
        _git('worktree', 'add', '--detach', str(workdir), 'HEAD',
             cwd=self.canonical)
        meta = self._meta_dir(name)
        self.assertTrue(meta.is_dir(), f'expected metadata dir for {name}')
        if locked:
            # git removes this on a successful add; we re-create it to simulate
            # the add being SIGKILLed mid-init with the lock still held.
            (meta / 'locked').write_text('initializing\n')
        if zero_head:
            (meta / 'HEAD').write_text(_ZERO_HEAD + '\n')
        if remove_workdir:
            # Simulate the /tmp cleaner (systemd-tmpfiles) wiping the working dir.
            shutil.rmtree(workdir)
        return meta, workdir

    def test_removes_kill_mid_init_orphan(self):
        meta, workdir = self._provision('gate-wt-killmidinit')
        self.assertTrue(meta.exists())
        self.assertFalse(workdir.exists())

        reaped, kept = csw.sweep_orphan_locked_worktrees(self.canonical)

        self.assertEqual(reaped, 1)
        self.assertFalse(
            meta.exists(),
            'kill-mid-init orphan metadata should be removed by the reaper',
        )

    def test_removes_zero_head_orphan_and_unblocks_fetch(self):
        meta, _ = self._provision('gate-wt-zerohead', zero_head=True)
        self.assertTrue(meta.exists())

        # Precondition: the corrupt zero-HEAD entry blocks `git fetch`.
        before = _git('fetch', 'origin', cwd=self.canonical, check=False)
        self.assertNotEqual(
            before.returncode, 0,
            'a leaked zero-HEAD worktree entry should block git fetch '
            f'(stderr={before.stderr!r})',
        )

        reaped, _ = csw.sweep_orphan_locked_worktrees(self.canonical)
        self.assertEqual(reaped, 1)
        self.assertFalse(meta.exists())

        # Postcondition: fetch is no longer blocked by the leaked entry.
        after = _git('fetch', 'origin', cwd=self.canonical, check=False)
        self.assertEqual(
            after.returncode, 0,
            f'git fetch should succeed after reaping the orphan '
            f'(stderr={after.stderr!r})',
        )

    def test_preserves_live_worktree_with_initializing_lock(self):
        # A live gate run legitimately holds an 'initializing' lock while its
        # working dir still exists; the reaper MUST NOT touch it.
        meta, workdir = self._provision(
            'gate-wt-live', locked=True, remove_workdir=False)
        self.assertTrue(meta.exists())
        self.assertTrue(workdir.exists())
        self.assertTrue((meta / 'locked').exists())

        reaped, kept = csw.sweep_orphan_locked_worktrees(self.canonical)

        self.assertEqual(reaped, 0, 'a live worktree must never be reaped')
        self.assertGreaterEqual(kept, 1)
        self.assertTrue(
            meta.exists(), 'live worktree metadata must survive the reaper')
        self.assertTrue(
            (meta / 'locked').exists(),
            'the reaper must NOT clear the lock of a live (present-workdir) run',
        )

    def test_mixed_live_and_orphan_only_orphan_reaped(self):
        live_meta, live_workdir = self._provision(
            'gate-wt-livemixed', remove_workdir=False)
        orphan_meta, _ = self._provision('gate-wt-orphanmixed')

        reaped, _ = csw.sweep_orphan_locked_worktrees(self.canonical)

        self.assertEqual(reaped, 1)
        self.assertFalse(orphan_meta.exists())
        self.assertTrue(live_meta.exists())
        self.assertTrue((live_meta / 'locked').exists())

    def test_no_worktrees_is_noop(self):
        reaped, kept = csw.sweep_orphan_locked_worktrees(self.canonical)
        self.assertEqual((reaped, kept), (0, 0))


if __name__ == '__main__':
    unittest.main()
