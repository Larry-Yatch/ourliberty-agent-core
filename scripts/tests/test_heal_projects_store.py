"""Tests for heal_projects_store — the SOLE committer for the projects-v3 P3
pipeline store agents/beacon/projects.json (projects-v3 P3, step
p3-project-store). Mirrors the GC healer's CommitAndPushTest shape: a tmp git
repo, a real on-disk projects.json, and the normalize→atomic-write→commit tick."""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import heal_projects_store as h  # noqa: E402

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


class ReadRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='proj-read-test-')
        self.path = Path(self.tmp) / 'projects.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty_registry(self):
        # The first tick seeds the store, so a missing file is not an error.
        self.assertEqual(h.read_registry(self.path),
                         {'schema_version': 1, 'projects': []})

    def test_empty_file_returns_empty_registry(self):
        self.path.write_text('')
        self.assertEqual(h.read_registry(self.path),
                         {'schema_version': 1, 'projects': []})

    def test_malformed_file_returns_none(self):
        self.path.write_text('{not valid json')
        self.assertIsNone(h.read_registry(self.path))

    def test_wrong_shape_returns_none(self):
        self.path.write_text(json.dumps(['not', 'a', 'dict']))
        self.assertIsNone(h.read_registry(self.path))

    def test_valid_registry_round_trips(self):
        reg = {'schema_version': 1, 'projects': [{'id': 'p'}]}
        self.path.write_text(json.dumps(reg))
        self.assertEqual(h.read_registry(self.path), reg)


class CommitAndPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='proj-git-test-')
        self.repo = Path(self.tmp) / 'repo'
        self.repo.mkdir()
        self._git('init', '-q', '-b', 'main')
        self._git('config', 'user.email', 'test@test')
        self._git('config', 'user.name', 'Test')
        (self.repo / 'agents' / 'beacon').mkdir(parents=True)
        self.proj = self.repo / h.PROJECTS_REL
        self.proj.write_text(json.dumps({'schema_version': 1, 'projects': []}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=str(self.repo), check=True,
                       capture_output=True, text=True)

    def test_nothing_when_clean(self):
        self.assertEqual(h.commit_and_push_projects(self.repo, 'audit'), 'nothing')

    def test_refuses_off_main(self):
        self._git('checkout', '-q', '-b', 'forge/feature')
        self.proj.write_text(json.dumps({'schema_version': 1,
                                         'projects': [{'id': 'x'}]}) + '\n')
        self.assertEqual(h.commit_and_push_projects(self.repo, 'audit'),
                         'wrong-branch')

    def test_commits_delta_then_clean(self):
        # No remote configured: commit succeeds, push fails -> 'push-failed',
        # but the delta IS committed locally (durability half is the commit).
        self.proj.write_text(json.dumps({'schema_version': 1,
                                         'projects': [{'id': 'x'}]}) + '\n')
        status = h.commit_and_push_projects(self.repo, 'audit-line')
        self.assertEqual(status, 'push-failed')  # no origin remote in the test
        log = subprocess.run(['git', 'log', '-1', '--pretty=%B'],
                             cwd=str(self.repo), capture_output=True, text=True)
        self.assertIn('projects-store healer', log.stdout)
        self.assertIn('audit-line', log.stdout)
        # A second call finds nothing to commit (idempotent).
        self.assertEqual(h.commit_and_push_projects(self.repo, 'audit'), 'nothing')


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='proj-runonce-test-')
        self.repo = Path(self.tmp) / 'repo'
        self.repo.mkdir()
        self._git('init', '-q', '-b', 'main')
        self._git('config', 'user.email', 'test@test')
        self._git('config', 'user.name', 'Test')
        (self.repo / 'agents' / 'beacon').mkdir(parents=True)
        self.proj = self.repo / h.PROJECTS_REL
        # Point the healer's store path at our tmp repo's projects.json, and
        # the repo_paths block at our tmp repo, so the commit lands here.
        os.environ['OURLIBERTY_PROJECTS_JSON'] = str(self.proj)
        self._orig_load = h.load_repo_paths
        h.load_repo_paths = lambda: {'ourliberty-agent-core': self.repo}

    def tearDown(self):
        import shutil
        os.environ.pop('OURLIBERTY_PROJECTS_JSON', None)
        h.load_repo_paths = self._orig_load
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=str(self.repo), check=True,
                       capture_output=True, text=True)

    def test_missing_store_seeds_empty_registry(self):
        # No projects.json yet → the tick seeds it and commits.
        rc = h.run_once(dry_run=False, now=NOW)
        self.assertIn(rc, (0, 2))  # 2 == push-failed (no remote), still wrote
        data = json.loads(self.proj.read_text())
        self.assertEqual(data, {'schema_version': 1, 'projects': []})

    def test_dry_run_writes_nothing(self):
        self.proj.write_text(json.dumps({'projects': [{'id': 'raw'}]}) + '\n')
        before = self.proj.read_text()
        rc = h.run_once(dry_run=True, now=NOW)
        self.assertEqual(rc, 0)
        # Dry-run mutates nothing on disk.
        self.assertEqual(self.proj.read_text(), before)

    def test_normalizes_and_writes_delta(self):
        # A raw, un-normalized entry on disk gets backfilled + committed.
        self.proj.write_text(json.dumps(
            {'projects': [{'id': 'proj-a', 'phases': [{'id': 'ph-1'}]}]}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed raw')
        rc = h.run_once(dry_run=False, now=NOW)
        self.assertIn(rc, (0, 2))
        data = json.loads(self.proj.read_text())
        proj = data['projects'][0]
        self.assertEqual(proj['title'], 'proj-a')       # backfilled
        self.assertEqual(proj['state'], 'active')        # default
        self.assertTrue(proj['one_off'])                 # single phase
        self.assertEqual(
            proj['phases'][0]['lifecycle_state'], 'brainstorm')  # default

    def test_clean_store_is_noop(self):
        # An already-normalized store produces no delta → no write, exit 0.
        import projects_store
        normalized, _ = projects_store.normalize_registry(
            {'projects': [{'id': 'proj-a', 'phases': [{'id': 'ph-1'}]}]}, now=NOW)
        self.proj.write_text(json.dumps(normalized, indent=2) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed normalized')
        before = self.proj.read_text()
        rc = h.run_once(dry_run=False, now=NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(self.proj.read_text(), before)

    def test_malformed_store_skips_tick(self):
        self.proj.write_text('{not json')
        rc = h.run_once(dry_run=False, now=NOW)
        self.assertEqual(rc, 1)

    def test_uncommitted_already_normalized_store_is_committed(self):
        # Regression for the commit being gated on the *normalization* delta
        # instead of the *git* delta. The dashboard (a non-committer) writes a
        # well-formed, ALREADY-normalized projects.json to disk but does not
        # commit it. normalize_registry is then a no-op (changed=False), yet the
        # healer MUST still drain the git delta — otherwise the file sits
        # uncommitted forever and blocks ourliberty-sync (the live incident).
        import projects_store
        # HEAD holds the empty registry (production's committed state).
        self.proj.write_text(
            json.dumps({'schema_version': 1, 'projects': []}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed empty')
        # The dashboard writes an already-normalized registry to disk (uncommitted).
        normalized, _ = projects_store.normalize_registry(
            {'projects': [{'id': 'proj-a', 'phases': [{'id': 'ph-1'}]}]}, now=NOW)
        self.proj.write_text(json.dumps(normalized, indent=2) + '\n')
        # Precondition: normalize is a no-op on this content (the changed=False
        # path), yet the working tree is git-dirty.
        renorm, _ = projects_store.normalize_registry(normalized, now=NOW)
        self.assertEqual(renorm, normalized, 'fixture must be already-normalized')
        dirty = subprocess.run(
            ['git', 'status', '--porcelain', h.PROJECTS_REL],
            cwd=str(self.repo), capture_output=True, text=True)
        self.assertTrue(dirty.stdout.strip(), 'precondition: store must be git-dirty')
        # The tick must commit the delta (push-failed only because no remote).
        rc = h.run_once(dry_run=False, now=NOW)
        self.assertIn(rc, (0, 2))
        after = subprocess.run(
            ['git', 'status', '--porcelain', h.PROJECTS_REL],
            cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(
            after.stdout.strip(), '',
            'healer must drain the uncommitted (already-normalized) store')
        log = subprocess.run(
            ['git', 'log', '-1', '--pretty=%B'],
            cwd=str(self.repo), capture_output=True, text=True)
        self.assertIn('projects-store healer', log.stdout)

    def test_dry_run_reports_git_delta_for_already_normalized_store(self):
        # The dry-run report must distinguish a git delta (uncommitted but
        # already-normalized) from a truly clean tree — the "no delta" message
        # previously masked exactly the state that blocked sync.
        import projects_store
        self.proj.write_text(
            json.dumps({'schema_version': 1, 'projects': []}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed empty')
        normalized, _ = projects_store.normalize_registry(
            {'projects': [{'id': 'proj-a', 'phases': [{'id': 'ph-1'}]}]}, now=NOW)
        self.proj.write_text(json.dumps(normalized, indent=2) + '\n')
        before = self.proj.read_text()
        rc = h.run_once(dry_run=True, now=NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(self.proj.read_text(), before)  # dry-run writes nothing
        # And the tree is still dirty (dry-run did not commit).
        dirty = subprocess.run(
            ['git', 'status', '--porcelain', h.PROJECTS_REL],
            cwd=str(self.repo), capture_output=True, text=True)
        self.assertTrue(dirty.stdout.strip())


if __name__ == '__main__':
    unittest.main()
