#!/usr/bin/env python3
"""Fixtures for `cleanup_stale_worktrees.sweep_orphan_dirs` — orphan-dir GC gap.

`sweep_canonical` only sees worktrees git reports via `git worktree list`. A dir
de-registered by `git worktree remove`/`prune` whose physical directory persists
is invisible to that sweep and accumulates forever. `sweep_orphan_dirs` closes
that gap, guarded by three independent checks (registered-set membership, the
in-flight-stem guard, and a 24h age floor).

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_cleanup_stale_worktrees_orphan_sweep
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import cleanup_stale_worktrees as csw  # noqa: E402


class SweepOrphanDirsTest(unittest.TestCase):
    """`sweep_orphan_dirs` reaps de-registered wt-* dirs past the 24h floor."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cleanup-orphan-sweep-test-')
        self.base = Path(self.tmpdir) / 'agent-worktrees'
        self.base.mkdir()
        self._patch = mock.patch.object(csw, 'WORKTREE_BASE', self.base)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_dir(self, name: str, age_seconds: float) -> Path:
        d = self.base / name
        d.mkdir()
        (d / 'marker').write_text('x')
        past = time.time() - age_seconds
        os.utime(d, (past, past))
        return d

    def test_orphan_older_than_24h_is_removed(self):
        d = self._make_dir('wt-forge-old', csw.ORPHAN_MAX_AGE_SECONDS + 3600)
        removed, kept = csw.sweep_orphan_dirs(set(), set())
        self.assertEqual((removed, kept), (1, 0))
        self.assertFalse(d.exists())

    def test_orphan_younger_than_24h_is_kept(self):
        d = self._make_dir('wt-forge-fresh', csw.ORPHAN_MAX_AGE_SECONDS - 3600)
        removed, kept = csw.sweep_orphan_dirs(set(), set())
        self.assertEqual((removed, kept), (0, 1))
        self.assertTrue(d.exists())

    def test_git_registered_path_is_untouched(self):
        # Old enough to be reaped on age alone, but git still tracks it.
        d = self._make_dir('wt-forge-registered', csw.ORPHAN_MAX_AGE_SECONDS + 3600)
        registered = {str(d.resolve())}
        removed, kept = csw.sweep_orphan_dirs(registered, set())
        # sweep_canonical owns registered dirs; orphan sweep skips silently.
        self.assertEqual((removed, kept), (0, 0))
        self.assertTrue(d.exists())

    def test_orphan_matching_active_stem_is_kept(self):
        d = self._make_dir(
            'wt-forge-some-active-task', csw.ORPHAN_MAX_AGE_SECONDS + 3600
        )
        removed, kept = csw.sweep_orphan_dirs(set(), {'some-active-task'})
        self.assertEqual((removed, kept), (0, 1))
        self.assertTrue(d.exists())

    def test_non_wt_dirs_and_files_ignored(self):
        # A non-wt directory and a stray file should never be candidates.
        other = self.base / 'not-a-worktree'
        other.mkdir()
        past = time.time() - (csw.ORPHAN_MAX_AGE_SECONDS + 3600)
        os.utime(other, (past, past))
        (self.base / 'wt-stray-file').write_text('x')
        removed, kept = csw.sweep_orphan_dirs(set(), set())
        self.assertEqual((removed, kept), (0, 0))
        self.assertTrue(other.exists())


if __name__ == '__main__':
    unittest.main()
