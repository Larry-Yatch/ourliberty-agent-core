"""Tests for worktree_manager.py — keyed-reuse worktree creation."""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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
        # Resolve symlinks (e.g. macOS /var/folders -> /private/var/folders)
        # so path comparisons against `git worktree list --porcelain` output
        # — which always returns the real path — pass on both Linux and macOS.
        self.tmpdir = Path(self._tmp.name).resolve()
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

    def test_setup_branch_checkpoint_preserves_existing_origin_commits(self):
        """D3.5 5c-followup-3 (Miss #1): when origin/<branch> already has
        commits (e.g., Larry directly authored work on this branch and
        pushed it), setup_branch_checkpoint must NOT overwrite those
        commits with the WIP-checkpoint-on-main-base. The WIP commit
        must land ON TOP of origin's existing HEAD.

        Surfaced on PR #7 2026-05-14: Mirror's worktree force-pushed a
        [WIP][session-start] commit over Larry's 1594-LOC `a9035b0`,
        wiping the branch on origin. Audit Miss #1.

        Pre-fix behavior: `git checkout -B <branch>` would create the
        local branch at the worktree's HEAD (= origin/main, since
        create_or_reuse_worktree_for_task uses --detach origin/main),
        then push --force-with-lease would succeed because we never
        fetched origin/<branch> (no remote-tracking ref → empty lease
        accepts the force).

        Post-fix behavior: fetch origin/<branch> explicitly; if it
        exists, checkout from origin/<branch>; the push is then a
        fast-forward and origin's prior commit is preserved as the
        WIP commit's parent.
        """
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        # Larry creates a branch on origin with real work
        _git('checkout', '-b', 'larry/real-work', cwd=self.origin)
        (self.origin / 'real_file.py').write_text(
            '# Larry\'s real implementation\nprint("important code")\n'
        )
        _git('add', 'real_file.py', cwd=self.origin)
        _git('commit', '-q', '-m', 'feat: real implementation', cwd=self.origin)
        # Capture the real commit hash before Mirror's worktree fires
        larry_commit = _git('rev-parse', 'HEAD', cwd=self.origin).stdout.strip()
        # Switch origin back to main so it's not on the protected branch
        _git('checkout', 'main', cwd=self.origin)

        # Mirror's worktree fires: creates from origin/main, then runs
        # setup_branch_checkpoint targeting larry/real-work
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'mirror', 'review-larry-work', self.canonical, log_fn=self._log_fn,
        )
        self.assertIsNotNone(path)

        # The fix: this should NOT wipe larry's commit on origin
        branch = worktree_manager.setup_branch_checkpoint(
            path, 'larry/real-work', 'review-larry-work',
            log_fn=self._log_fn,
        )
        self.assertEqual(branch, 'larry/real-work')

        # Verify origin still has larry's commit reachable from
        # larry/real-work HEAD — i.e., the WIP checkpoint landed ON TOP,
        # NOT as a replacement.
        log = _git(
            'log', '--format=%H %s', 'larry/real-work',
            cwd=self.origin, check=False,
        )
        self.assertIn(
            larry_commit, log.stdout,
            f"Larry's commit {larry_commit[:12]} was wiped from origin/larry/real-work. "
            f"Branch log:\n{log.stdout}"
        )
        # And the WIP checkpoint should be the new HEAD (above larry's)
        head_subject = _git(
            'log', '-1', '--pretty=%s', 'larry/real-work', cwd=self.origin,
        ).stdout.strip()
        self.assertIn('[WIP][session-start]', head_subject)

    def test_setup_branch_checkpoint_empty_despite_stale_reused_worktree(self):
        """Regression (#731 revision-loop class): a REUSED worktree can carry a
        STALE working tree — an old pre-fix copy of a file, staged — into the
        next round. `git commit --allow-empty` commits a DIRTY index (the flag
        only suppresses the empty-tree error), so without the pre-commit
        hard-reset the [WIP][session-start] snapshot captures that stale content
        and REVERTS the branch tip, clobbering mid-review work and forcing Forge
        to redo the fix every round (the ~18h non-converging loop). The
        checkpoint must be EMPTY and the fix on origin/<branch> must survive.
        """
        _git('config', 'receive.denyCurrentBranch', 'updateInstead', cwd=self.origin)
        # origin/forge/task-x: round-1 baseline (target.py="OLD") then the fix
        # (target.py="FIXED"). HEAD of the branch on origin = the fix.
        _git('checkout', '-b', 'forge/task-x', cwd=self.origin)
        (self.origin / 'target.py').write_text('OLD\n')
        _git('add', 'target.py', cwd=self.origin)
        _git('commit', '-q', '-m', 'round-1 baseline', cwd=self.origin)
        (self.origin / 'target.py').write_text('FIXED\n')
        _git('add', 'target.py', cwd=self.origin)
        _git('commit', '-q', '-m', 'fix: the gating', cwd=self.origin)
        fix_commit = _git('rev-parse', 'HEAD', cwd=self.origin).stdout.strip()
        _git('checkout', 'main', cwd=self.origin)  # leave the protected branch

        # Make the worktree look like a REUSED one already on the branch with
        # stale staged pre-fix content (the dirtying condition the checkpoint
        # must absorb without clobbering).
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-x', self.canonical, log_fn=self._log_fn,
        )
        self.assertIsNotNone(path)
        _git('fetch', 'origin', 'forge/task-x', cwd=path)
        _git('checkout', '-B', 'forge/task-x', 'origin/forge/task-x', cwd=path)
        self.assertEqual((path / 'target.py').read_text(), 'FIXED\n')
        (path / 'target.py').write_text('OLD\n')      # revert the fix...
        _git('add', 'target.py', cwd=path)            # ...and stage the stale copy

        branch = worktree_manager.setup_branch_checkpoint(
            path, 'forge/task-x', 'task-x', log_fn=self._log_fn,
        )
        self.assertEqual(branch, 'forge/task-x')

        # The fix must survive on origin — the checkpoint did NOT revert it.
        show = _git('show', 'forge/task-x:target.py', cwd=self.origin).stdout
        self.assertEqual(
            show, 'FIXED\n',
            'session-start checkpoint reverted the fix (the #731 clobber)',
        )
        # Fix commit still reachable; the WIP checkpoint is the new HEAD and is
        # EMPTY (its tree equals its parent, the fix commit).
        log = _git('log', '--format=%H %s', 'forge/task-x', cwd=self.origin).stdout
        self.assertIn(fix_commit, log)
        head_subject = _git(
            'log', '-1', '--pretty=%s', 'forge/task-x', cwd=self.origin,
        ).stdout.strip()
        self.assertIn('[WIP][session-start]', head_subject)
        diff = _git(
            'diff', 'forge/task-x~1', 'forge/task-x', cwd=self.origin, check=False,
        ).stdout
        self.assertEqual(diff.strip(), '', 'checkpoint commit must carry no content')

    def test_setup_branch_checkpoint_fresh_branch_still_works(self):
        """Backward compat: when origin/<branch> doesn't exist, fall
        through to the legacy 'create from current HEAD' behavior."""
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-fresh', self.canonical, log_fn=self._log_fn,
        )
        # forge/task-fresh does NOT exist on origin yet
        branch = worktree_manager.setup_branch_checkpoint(
            path, 'forge/task-fresh', 'task-fresh', log_fn=self._log_fn,
        )
        self.assertEqual(branch, 'forge/task-fresh')
        # Verify origin now has the new branch with the WIP commit
        r = _git('branch', '--list', 'forge/task-fresh', cwd=self.origin)
        self.assertIn('forge/task-fresh', r.stdout)

    def test_setup_branch_checkpoint_fetch_failure_refuses_checkpoint(self):
        """Defense-in-depth (PR #11 reviewer §2): when the fetch can't
        succeed for non-absent-branch reasons (network failure; origin
        unreachable), refuse to checkpoint. The risk is that a stale
        remote-tracking ref combined with a fetch failure could base the
        checkout on stale state and force-with-lease could push over
        origin's current commits."""
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-fetch-fail', self.canonical,
            log_fn=self._log_fn,
        )
        # Break origin URL so the fetch genuinely fails (simulates
        # network/auth failure, not absent-branch). The reused worktree
        # inherits the broken remote URL from the canonical clone.
        _git(
            'remote', 'set-url', 'origin',
            str(self.tmpdir / 'does-not-exist'),
            cwd=path,
        )
        branch = worktree_manager.setup_branch_checkpoint(
            path, 'forge/task-fetch-fail', 'task-fetch-fail',
            log_fn=self._log_fn,
        )
        # Refuse — don't risk a wipe via uncertain state
        self.assertIsNone(branch)
        # Surface the refusal in logs for watchdog visibility
        self.assertTrue(
            any('refusing to checkpoint' in m for m in self.logs),
            f'expected refusal log; got: {self.logs}',
        )

    def test_setup_branch_checkpoint_absent_remote_branch_is_not_a_failure(self):
        """When `git fetch origin <branch>` returns non-zero because the
        branch doesn't exist on origin (stderr: "couldn't find remote
        ref..."), that's NOT a fetch failure — it's a fresh-branch
        signal. Fall through to legacy create-from-HEAD behavior."""
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'task-absent-remote', self.canonical,
            log_fn=self._log_fn,
        )
        # forge/totally-new-branch absolutely does NOT exist on origin.
        # The fetch will return 128 with "couldn't find remote ref" — our
        # logic must recognize this as "fresh branch" not "fetch failed".
        branch = worktree_manager.setup_branch_checkpoint(
            path, 'forge/totally-new-branch', 'task-absent-remote',
            log_fn=self._log_fn,
        )
        self.assertEqual(branch, 'forge/totally-new-branch')
        # Origin should now have the new branch
        r = _git('branch', '--list', 'forge/totally-new-branch',
                 cwd=self.origin)
        self.assertIn('forge/totally-new-branch', r.stdout)

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

    def _make_origin_pr_branch(self, branch: str) -> str:
        """Create `branch` on origin with one real commit; return its sha."""
        _git(
            'config', 'receive.denyCurrentBranch', 'updateInstead',
            cwd=self.origin,
        )
        _git('checkout', '-b', branch, cwd=self.origin)
        (self.origin / 'feature.py').write_text('print("the PR content")\n')
        _git('add', 'feature.py', cwd=self.origin)
        _git('commit', '-q', '-m', 'feat: the PR content', cwd=self.origin)
        tip = _git('rev-parse', 'HEAD', cwd=self.origin).stdout.strip()
        _git('checkout', 'main', cwd=self.origin)
        return tip

    def test_setup_branch_checkpoint_readonly_detached_no_push(self):
        """Mirror-review mode (readonly=True): forced DETACHED checkout at
        the origin tip — no [WIP] commit, no push, no local branch ref. A
        push here moves the PR head SHA, which retriggers review dispatch
        and defeats head-SHA review dedup (PR #841 dup-review, 2026-07-08);
        a local branch ref would collide with a concurrent Forge worktree
        on the same PR branch (shared .git)."""
        tip = self._make_origin_pr_branch('work/pr-under-review')
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'mirror', 'pr-review-ro', self.canonical, log_fn=self._log_fn,
        )
        branch = worktree_manager.setup_branch_checkpoint(
            path, 'work/pr-under-review', 'pr-review-ro',
            log_fn=self._log_fn, readonly=True,
        )
        self.assertEqual(branch, 'work/pr-under-review')
        # Worktree sits exactly at the origin tip, DETACHED (no branch ref).
        wt_head = _git('rev-parse', 'HEAD', cwd=path).stdout.strip()
        self.assertEqual(wt_head, tip)
        self.assertTrue((path / 'feature.py').exists())
        sym = _git('symbolic-ref', '-q', 'HEAD', cwd=path, check=False)
        self.assertNotEqual(sym.returncode, 0, 'expected detached HEAD')
        # Origin tip unchanged — the reviewer pushed NOTHING.
        origin_tip = _git(
            'rev-parse', 'work/pr-under-review', cwd=self.origin,
        ).stdout.strip()
        self.assertEqual(origin_tip, tip)
        self.assertTrue(
            any('read-only detached checkout' in m for m in self.logs))

    def test_setup_branch_checkpoint_readonly_absorbs_dirty_reuse(self):
        # A REUSED worktree can carry stale local modifications into the
        # next round; --force must discard them so the review sees the
        # exact origin tip.
        tip = self._make_origin_pr_branch('work/pr-under-review-2')
        path, branch = worktree_manager.ensure_worktree_for_task(
            'mirror', 'pr-review-ro-2', self.canonical,
            branch='work/pr-under-review-2', log_fn=self._log_fn,
            readonly=True,
        )
        self.assertEqual(branch, 'work/pr-under-review-2')
        (path / 'feature.py').write_text('STALE LOCAL EDIT\n')
        path2, branch2 = worktree_manager.ensure_worktree_for_task(
            'mirror', 'pr-review-ro-2', self.canonical,
            branch='work/pr-under-review-2', log_fn=self._log_fn,
            readonly=True,
        )
        self.assertEqual(path2, path)
        self.assertEqual(branch2, 'work/pr-under-review-2')
        self.assertEqual(
            (path / 'feature.py').read_text(), 'print("the PR content")\n')
        origin_tip = _git(
            'rev-parse', 'work/pr-under-review-2', cwd=self.origin,
        ).stdout.strip()
        self.assertEqual(origin_tip, tip, 'read-only checkout must not push')

    def test_setup_branch_checkpoint_readonly_absent_branch_refuses(self):
        """readonly=True with no origin/<branch>: refuse (None). The PR
        branch being gone means there is nothing to review — proceeding
        would silently review whatever tree the worktree holds."""
        path = worktree_manager.create_or_reuse_worktree_for_task(
            'mirror', 'pr-review-ro-3', self.canonical, log_fn=self._log_fn,
        )
        branch = worktree_manager.setup_branch_checkpoint(
            path, 'work/vanished-branch', 'pr-review-ro-3',
            log_fn=self._log_fn, readonly=True,
        )
        self.assertIsNone(branch)
        r = _git(
            'branch', '--list', 'work/vanished-branch',
            cwd=self.origin, check=False,
        )
        self.assertNotIn('work/vanished-branch', r.stdout)
        self.assertTrue(any('nothing to review' in m for m in self.logs))

    def test_readonly_coexists_with_branch_checked_out_elsewhere(self):
        # The PR branch is checked out (via checkout -B) in a Forge-style
        # worktree; the reviewer's DETACHED checkout of the same branch in
        # a second worktree must still succeed — the dup-review scenario
        # runs both concurrently against one shared .git.
        self._make_origin_pr_branch('work/pr-shared')
        forge_wt = worktree_manager.create_or_reuse_worktree_for_task(
            'forge', 'pr-shared-build', self.canonical, log_fn=self._log_fn,
        )
        self.assertEqual(
            worktree_manager.setup_branch_checkpoint(
                forge_wt, 'work/pr-shared', 'pr-shared-build',
                log_fn=self._log_fn,
            ),
            'work/pr-shared',
        )
        mirror_wt = worktree_manager.create_or_reuse_worktree_for_task(
            'mirror', 'pr-shared-review', self.canonical, log_fn=self._log_fn,
        )
        branch = worktree_manager.setup_branch_checkpoint(
            mirror_wt, 'work/pr-shared', 'pr-shared-review',
            log_fn=self._log_fn, readonly=True,
        )
        self.assertEqual(branch, 'work/pr-shared')

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
