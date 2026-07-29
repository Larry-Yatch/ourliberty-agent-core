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
from unittest import mock
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

    def test_every_decline_path_still_refreshes_the_tracking_ref(self):
        """The property, over EVERY reason we decline — not just the one that
        was tested.

        `check_spec_doc_presence` classifies a spec against this checkout's
        local `refs/remotes/origin/main` and never fetches, so a stale tracking
        ref makes a merged spec read as NOT_AUTHORED and the caller is told to
        author a spec that already exists (incident 2026-06-10). A fetch cannot
        disturb anyone — it moves no HEAD and no file — so there is no decline
        reason that justifies leaving the ref stale. Before this, the fetch sat
        below the branch check: a dirty tree got a fresh ref and a checkout
        parked on a branch did not.
        """
        for label, arrange in (
            ('dirty tree',
             lambda: (self.pair.clone / 'seed.txt').write_text('mid-edit\n')),
            ('on a feature branch',
             lambda: _git(self.path, 'checkout', '-q', '-b', 'feature/wip')),
            ('detached HEAD',
             lambda: _git(self.path, 'checkout', '-q', '--detach',
                          _git(self.path, 'rev-parse', 'HEAD').stdout.strip())),
            ('local commits ahead',
             lambda: (_git(self.path, 'commit', '-q', '--allow-empty',
                           '-m', 'local work'))),
        ):
            with self.subTest(decline=label):
                self.setUp()          # fresh clone+origin pair per case
                arrange()
                self.pair.advance_origin(1)
                before = _git(self.path, 'rev-parse',
                              'refs/remotes/origin/main').stdout.strip()
                out = sdrc.sync_one('demo', self.path, apply=True)
                after = _git(self.path, 'rev-parse',
                             'refs/remotes/origin/main').stdout.strip()
                # It still refuses to move anything...
                self.assertNotEqual(
                    out.action, 'advanced',
                    f'{label}: the tree must NOT be moved')
                # ...but the classification input is now current.
                self.assertNotEqual(
                    before, after,
                    f'{label}: declined without fetching, so origin/main is '
                    f'stale and a merged spec will read as never-authored')

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

    def _granted_paths(self, unit_name: str) -> list[str]:
        """Every path the unit grants write to.

        systemd ACCUMULATES repeated `ReadWritePaths=` directives, so this
        reads all of them, not just the first. Taking only the first would
        mis-report a grant that someone added on a fresh line — the natural
        way to extend an allowlist that is already a dozen entries long — and
        fail a config that is in fact correct."""
        unit = (Path(sdrc.__file__).resolve().parent.parent
                / 'systemd' / unit_name)
        self.assertTrue(
            unit.is_file(),
            f'{unit_name} is listed in FF_CAPABLE_UNITS/FF_EXCLUDED_UNITS but '
            f'has no unit file at {unit} — its allowlist cannot be checked')
        rw_lines = [l for l in unit.read_text().splitlines()
                    if l.startswith('ReadWritePaths=')]
        self.assertTrue(
            rw_lines,
            f'{unit_name} has no ReadWritePaths= line; if its sandbox changed '
            f'shape, this invariant needs rechecking, not skipping')
        granted: list[str] = []
        for line in rw_lines:
            granted.extend(line.split('=', 1)[1].split())
        return granted

    def _syncable(self) -> dict:
        syncable = {repo: path
                    for repo, path in sdrc.load_repo_paths().items()
                    if repo not in sdrc.SELF_SYNCED_REPOS}
        self.assertTrue(syncable, 'expected at least one syncable repo')
        return syncable

    def test_multi_line_read_write_paths_are_all_collected(self):
        """systemd accumulates repeated ReadWritePaths=; so must we.

        Reading only the first line would fail a correct config the moment
        someone extends an allowlist by adding a line instead of editing a
        twelve-entry one — a false alarm pointing at a non-problem."""
        unit = (Path(sdrc.__file__).resolve().parent.parent / 'systemd'
                / 'ourliberty-sync-dispatch-repos.service')
        text = unit.read_text().replace(
            'ReadWritePaths=/home/larry/ourliberty-dashboard',
            'ReadWritePaths=/home/larry/ourliberty-dashboard\n'
            'ReadWritePaths=/home/larry/SPLIT-ACROSS-LINES', 1)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        tmp = Path(td.name)
        (tmp / 'systemd').mkdir()
        (tmp / 'systemd' / 'probe.service').write_text(text)
        with mock.patch.object(
                sdrc, '__file__', str(tmp / 'scripts' / 'x.py')):
            granted = self._granted_paths('probe.service')
        # Both the original line's entries AND the appended line's are seen.
        self.assertIn('/home/larry/RSDPM', granted)
        self.assertIn('/home/larry/SPLIT-ACROSS-LINES', granted)

    def test_excluded_units_are_not_granted_write(self):
        """The deliberate exclusions must stay exclusions.

        ourliberty-beacon-bot reaches sync_one via the chat-approve kickoff but
        is intentionally denied write on the product checkouts (see
        FF_EXCLUDED_UNITS for the measurement behind that call). Pinning it here
        means a later grant fails this test, forcing whoever makes it to move
        the unit into FF_CAPABLE_UNITS as a conscious decision rather than
        quietly widening the outward-facing bot's reach."""
        # Non-empty guard, mirroring the capable-units test. Without it this
        # whole test passes hardest when there is nothing left to check: empty
        # the tuple and the loop below runs zero times, so a cleanup that drops
        # the beacon-bot entry would delete the protection AND the signal that
        # it was deleted. Verified by emptying it — the suite stayed green.
        self.assertTrue(
            sdrc.FF_EXCLUDED_UNITS,
            'FF_EXCLUDED_UNITS is empty, so this test guards nothing. A unit '
            'was removed from it: either it is no longer reachable from '
            'refresh_checkout (delete this test too, deliberately) or the '
            'no-grant decision was dropped by accident')
        for unit_name in sdrc.FF_EXCLUDED_UNITS:
            granted = self._granted_paths(unit_name)
            for repo, path in self._syncable().items():
                self.assertNotIn(
                    path, granted,
                    f'{unit_name} now grants write to {repo} ({path}). If that '
                    f'is intended, MOVE it to FF_CAPABLE_UNITS — an ff-capable '
                    f'unit must be covered by the grants-write invariant, not '
                    f'sitting in the excluded list claiming it cannot write')

    def test_capable_and_excluded_unit_lists_are_disjoint(self):
        """A unit cannot be both, and every reachable unit needs a verdict."""
        self.assertFalse(
            set(sdrc.FF_CAPABLE_UNITS) & set(sdrc.FF_EXCLUDED_UNITS),
            'a unit listed as both ff-capable and ff-excluded has no defined '
            'expectation — the two invariants would contradict each other')

    def test_unit_grants_write_to_every_syncable_repo(self):
        """A repo in repo_paths that a unit can't write to fails there alone.

        This is the trap the unit comments warn about, and the on-demand
        self-heal widened it: `sync_one` is now also called inline from the
        DAG-preflight (under ourliberty-inbox-watcher) and the kickoff (under
        ourliberty-outbox-notifier), each with its own ProtectHome=read-only
        sandbox. Checking only the timer's unit would leave the newer callers
        free to EROFS on the next repo onboarded — and because the timer would
        still be succeeding, the checkout would look maintained while the
        on-demand path was silently dead. Assert across ALL of them."""
        syncable = {repo: path
                    for repo, path in sdrc.load_repo_paths().items()
                    if repo not in sdrc.SELF_SYNCED_REPOS}
        self.assertTrue(syncable, 'expected at least one syncable repo')
        self.assertTrue(sdrc.FF_CAPABLE_UNITS, 'the unit list must not be empty')
        for unit_name in sdrc.FF_CAPABLE_UNITS:
            granted = self._granted_paths(unit_name)
            for repo, path in syncable.items():
                self.assertIn(
                    path, granted,
                    f'{repo} ({path}) is in repo_paths but {unit_name} cannot '
                    f'write it — its fast-forward will EROFS there while the '
                    f'other units keep succeeding')


class MainTest(unittest.TestCase):
    def test_main_dry_run_returns_zero(self):
        self.assertEqual(sdrc.main([]), 0)

    def test_main_json_mode_returns_zero(self):
        self.assertEqual(sdrc.main(['--json']), 0)


if __name__ == '__main__':
    unittest.main()
