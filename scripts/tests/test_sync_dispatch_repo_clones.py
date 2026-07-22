#!/usr/bin/env python3
"""Tests for sync_dispatch_repo_clones — the dispatch-repo checkout freshener.

Builds REAL throwaway git repos in a tmpdir (origin + clone) rather than mocking
subprocess, so the guards are exercised against git's actual behaviour: what
`rev-parse --abbrev-ref HEAD` reports on a detached HEAD, what `merge --ff-only`
does to a diverged branch, what `status --porcelain` counts.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_sync_dispatch_repo_clones
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import sync_dispatch_repo_clones as sdrc  # noqa: E402


def _git(path, *args):
    return subprocess.run(
        ['git', '-C', str(path), *args],
        capture_output=True, text=True, check=True,
    )


def _commit(path, name, body='x'):
    (Path(path) / name).write_text(body)
    _git(path, 'add', name)
    _git(path, 'commit', '-q', '-m', f'add {name}')


class _RepoPair:
    """An origin repo plus a clone of it, both on main."""

    def __init__(self, root: Path):
        self.origin = root / 'origin'
        self.clone = root / 'clone'
        self.origin.mkdir()
        _git(self.origin, 'init', '-q', '-b', 'main')
        _git(self.origin, 'config', 'user.email', 't@t.test')
        _git(self.origin, 'config', 'user.name', 'T')
        _commit(self.origin, 'seed.txt')
        subprocess.run(
            ['git', 'clone', '-q', str(self.origin), str(self.clone)],
            capture_output=True, check=True)
        _git(self.clone, 'config', 'user.email', 't@t.test')
        _git(self.clone, 'config', 'user.name', 'T')

    def advance_origin(self, n=1):
        for i in range(n):
            _commit(self.origin, f'upstream-{i}.txt')


class SyncOneTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pair = _RepoPair(self.root)
        self.path = str(self.pair.clone)

    def tearDown(self):
        self._tmp.cleanup()

    def test_advances_a_behind_clone(self):
        self.pair.advance_origin(3)
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'advanced')
        self.assertEqual(out.commits, 3)
        # the file from origin is really on disk now
        self.assertTrue((self.pair.clone / 'upstream-2.txt').exists())

    def test_dry_run_does_not_move_the_clone(self):
        self.pair.advance_origin(2)
        before = _git(self.path, 'rev-parse', 'HEAD').stdout.strip()
        out = sdrc.sync_one('demo', self.path, apply=False)
        self.assertEqual(out.action, 'advanced')
        self.assertEqual(out.commits, 2)
        self.assertIn('DRY-RUN', out.reason)
        after = _git(self.path, 'rev-parse', 'HEAD').stdout.strip()
        self.assertEqual(before, after, 'dry-run must not move HEAD')

    def test_current_clone_reports_current(self):
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'current')
        self.assertEqual(out.commits, 0)

    def test_tracked_modification_is_skipped_and_never_clobbered(self):
        """Somebody is mid-edit on a tracked file — leave the tree alone."""
        self.pair.advance_origin(1)
        tracked = self.pair.clone / 'seed.txt'      # committed in _RepoPair
        tracked.write_text('half-finished edit')
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'skipped')
        self.assertIn('uncommitted', out.reason)
        self.assertEqual(tracked.read_text(), 'half-finished edit')

    def test_untracked_build_litter_does_not_block_the_sync(self):
        """The ourliberty-graph case: a checkout carrying only __pycache__/ must
        still advance, or it parks forever on litter nobody will ever commit."""
        self.pair.advance_origin(1)
        litter = self.pair.clone / '__pycache__'
        litter.mkdir()
        (litter / 'mod.cpython-312.pyc').write_text('bytecode')
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'advanced')
        self.assertEqual(out.commits, 1)
        self.assertTrue((litter / 'mod.cpython-312.pyc').exists(),
                        'litter must survive the fast-forward')

    def test_untracked_collision_errors_rather_than_clobbering(self):
        """git refuses a merge that would overwrite an untracked file; we must
        surface that as an error, never silently destroy the local copy."""
        self.pair.advance_origin(0)
        _commit(self.pair.origin, 'collide.txt', 'from upstream')
        local = self.pair.clone / 'collide.txt'
        local.write_text('precious local copy')
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'error')
        self.assertEqual(local.read_text(), 'precious local copy')

    def test_local_commits_ahead_are_skipped(self):
        _commit(self.path, 'local-only.txt')
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'skipped')
        self.assertIn('ahead', out.reason)

    def test_diverged_clone_is_skipped_not_merged(self):
        # both sides move — ff-only would refuse; the ahead-guard catches it first
        self.pair.advance_origin(1)
        _commit(self.path, 'local-only.txt')
        head_before = _git(self.path, 'rev-parse', 'HEAD').stdout.strip()
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'skipped')
        self.assertEqual(
            _git(self.path, 'rev-parse', 'HEAD').stdout.strip(), head_before)

    def test_non_main_branch_is_skipped(self):
        _git(self.path, 'checkout', '-q', '-b', 'feature/wip')
        self.pair.advance_origin(1)
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'skipped')
        self.assertIn('not main', out.reason)

    def test_detached_head_is_skipped(self):
        sha = _git(self.path, 'rev-parse', 'HEAD').stdout.strip()
        _git(self.path, 'checkout', '-q', '--detach', sha)
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'skipped')

    def test_agent_core_is_never_touched(self):
        self.pair.advance_origin(5)
        out = sdrc.sync_one('ourliberty-agent-core', self.path, apply=True)
        self.assertEqual(out.action, 'skipped')
        self.assertIn('sync_agent_core.sh', out.reason)
        self.assertFalse((self.pair.clone / 'upstream-0.txt').exists())

    def test_missing_path_is_skipped_not_an_error(self):
        out = sdrc.sync_one('gone', str(self.root / 'nope'), apply=True)
        self.assertEqual(out.action, 'skipped')

    def test_non_git_directory_is_skipped(self):
        plain = self.root / 'plain'
        plain.mkdir()
        out = sdrc.sync_one('plain', str(plain), apply=True)
        self.assertEqual(out.action, 'skipped')
        self.assertIn('not a git repository', out.reason)

    def test_unreachable_origin_is_an_error_not_a_crash(self):
        _git(self.path, 'remote', 'set-url', 'origin',
             str(self.root / 'does-not-exist'))
        out = sdrc.sync_one('demo', self.path, apply=True)
        self.assertEqual(out.action, 'error')
        self.assertIn('fetch failed', out.reason)


class LoadRepoPathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / 'agent-models.json'

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_repo_paths(self):
        self.cfg.write_text(json.dumps({'repo_paths': {'a': '/tmp/a'}}))
        self.assertEqual(sdrc.load_repo_paths(self.cfg), {'a': '/tmp/a'})

    def test_missing_file_is_empty_not_a_crash(self):
        self.assertEqual(
            sdrc.load_repo_paths(Path('/nope/agent-models.json')), {})

    def test_malformed_json_is_empty_not_a_crash(self):
        self.cfg.write_text('{not json')
        self.assertEqual(sdrc.load_repo_paths(self.cfg), {})

    def test_non_dict_repo_paths_is_empty(self):
        self.cfg.write_text(json.dumps({'repo_paths': ['a', 'b']}))
        self.assertEqual(sdrc.load_repo_paths(self.cfg), {})

    def test_drops_blank_and_non_string_paths(self):
        self.cfg.write_text(json.dumps(
            {'repo_paths': {'a': '/tmp/a', 'b': '', 'c': None, 'd': 7}}))
        self.assertEqual(sdrc.load_repo_paths(self.cfg), {'a': '/tmp/a'})


class LiveConfigTest(unittest.TestCase):
    """The shipped config must stay consistent with the unit's write allowlist."""

    def test_every_registered_repo_is_a_string_path(self):
        paths = sdrc.load_repo_paths()
        self.assertTrue(paths, 'repo_paths should be readable from the repo')
        for repo, path in paths.items():
            self.assertTrue(path.startswith('/'), f'{repo} path not absolute')

    def test_unit_grants_write_to_every_syncable_repo(self):
        """A repo in repo_paths that the unit can't write to fails every tick.

        This is the trap the unit comments warn about: adding a dispatch repo
        without extending ReadWritePaths yields a silent per-tick error."""
        unit = (Path(sdrc.__file__).resolve().parent.parent
                / 'systemd' / 'ourliberty-sync-dispatch-repos.service')
        text = unit.read_text()
        rw_line = next(l for l in text.splitlines()
                       if l.startswith('ReadWritePaths='))
        granted = rw_line.split('=', 1)[1].split()
        for repo, path in sdrc.load_repo_paths().items():
            if repo in sdrc.SELF_SYNCED_REPOS:
                continue
            self.assertIn(
                path, granted,
                f'{repo} ({path}) is in repo_paths but the unit cannot write it')


class MainTest(unittest.TestCase):
    def test_main_dry_run_returns_zero(self):
        self.assertEqual(sdrc.main([]), 0)

    def test_main_json_mode_returns_zero(self):
        self.assertEqual(sdrc.main(['--json']), 0)


if __name__ == '__main__':
    unittest.main()
