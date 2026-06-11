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
        # sweep_orphan_dirs logs every keep/removal; csw.LOG_FILE is a
        # hardcoded /home/larry path no env var can redirect — patch it or
        # every droplet test run appends to the real worktree-cleanup.log.
        self._log_patch = mock.patch.object(
            csw, 'LOG_FILE', Path(self.tmpdir) / 'worktree-cleanup.log')
        self._log_patch.start()

    def tearDown(self):
        self._log_patch.stop()
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

    def test_short_stem_does_not_keep_unrelated_worktree(self):
        # audit #12 regression: a short active stem 'b' must NOT keep an
        # unrelated worktree via a bare substring match. The old guard
        # (`stem in name`) matched the 'b' infix of 'rebuild' and wrongly
        # preserved a stale worktree forever. Boundary matching (id_matches,
        # min_len=1) sees 'b' is not a delimited token in 'wt-forge-rebuild-001'
        # and lets the age sweep reap it.
        d = self._make_dir(
            'wt-forge-rebuild-001', csw.ORPHAN_MAX_AGE_SECONDS + 3600
        )
        removed, kept = csw.sweep_orphan_dirs(set(), {'b'})
        self.assertEqual((removed, kept), (1, 0))
        self.assertFalse(d.exists())

    def test_short_stem_keeps_its_own_worktree(self):
        # Safe direction: the same short stem 'b' MUST still keep its own
        # worktree, where it appears as a boundary-delimited token (-b-).
        d = self._make_dir('wt-forge-b-001', csw.ORPHAN_MAX_AGE_SECONDS + 3600)
        removed, kept = csw.sweep_orphan_dirs(set(), {'b'})
        self.assertEqual((removed, kept), (0, 1))
        self.assertTrue(d.exists())


class SweepCanonicalStemGuardTest(unittest.TestCase):
    """`sweep_canonical`'s in-flight-stem guard uses the same boundary match.

    This is the PRIMARY guard the audit (#12) cites — the worktree git still
    tracks. We patch `list_worktrees` (the git call) and `remove_worktree` (the
    git mutation) so the test exercises only the keep/reap decision logic.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cleanup-canonical-stem-test-')
        self.base = Path(self.tmpdir) / 'agent-worktrees'
        self.base.mkdir()
        # Route MANAGED_WORKTREE_PREFIX at the tmp base so our fake worktree
        # paths are recognized as "ours".
        self._prefix_patch = mock.patch.object(
            csw, 'MANAGED_WORKTREE_PREFIX', str(self.base / 'wt-')
        )
        self._prefix_patch.start()
        # The keep/reap flow logs decisions; see SweepOrphanDirsTest.setUp.
        self._log_patch = mock.patch.object(
            csw, 'LOG_FILE', Path(self.tmpdir) / 'worktree-cleanup.log')
        self._log_patch.start()

    def tearDown(self):
        self._log_patch.stop()
        self._prefix_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_worktree(self, name: str, age_seconds: float) -> Path:
        d = self.base / name
        d.mkdir()
        (d / 'marker').write_text('x')
        past = time.time() - age_seconds
        os.utime(d, (past, past))
        return d

    def _run(self, name, active_stems):
        d = self._make_worktree(name, csw.MAX_AGE_SECONDS + 3600)
        canonical = Path(self.tmpdir)  # exists; not under the wt- prefix
        with mock.patch.object(
            csw, 'list_worktrees', return_value=[{'path': str(d)}]
        ), mock.patch.object(
            csw, 'remove_worktree', return_value=True
        ) as rm:
            removed, kept, _ = csw.sweep_canonical(canonical, active_stems)
        return removed, kept, rm, d

    def test_short_stem_does_not_keep_unrelated_worktree(self):
        # 'b' must not match the 'b' infix of 'rebuild' — the stale, git-tracked
        # worktree is reaped via remove_worktree (age past MAX_AGE_SECONDS).
        removed, kept, rm, d = self._run('wt-forge-rebuild-001', {'b'})
        self.assertEqual(removed, 1)
        rm.assert_called_once_with(Path(self.tmpdir), str(d))

    def test_short_stem_keeps_its_own_worktree(self):
        # Safe direction: 'b' is boundary-delimited in 'wt-forge-b-001', so the
        # guard keeps it and remove_worktree is never called.
        removed, kept, rm, d = self._run('wt-forge-b-001', {'b'})
        self.assertEqual((removed, kept), (0, 1))
        rm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
