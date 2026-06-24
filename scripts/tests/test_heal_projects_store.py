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
from datetime import datetime, timedelta, timezone
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

    def _write_missions(self, missions):
        mp = self.repo / h.MISSIONS_REL
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({'schema_version': 1, 'missions': missions}) + '\n')

    def test_mirrors_active_missions_into_pipeline(self):
        # retire-missions-kanban: an ACTIVE in_flight mission with no project yet
        # is mirrored into projects.json as a single-phase building project; a
        # shipped mission is NOT mirrored (spec § 4.2). missions.json is untouched.
        self.proj.write_text(json.dumps({'schema_version': 1, 'projects': []}, indent=2) + '\n')
        self._git('add', '.'); self._git('commit', '-q', '-m', 'seed empty projects')
        self._write_missions([
            {'id': 'flight-x', 'name': 'Flight X', 'phase': 'in_flight',
             'brief': 'ship it', 'task_ids': ['seq-flight-x-001-step-a'],
             'repo': 'ourliberty-agent-core', 'spec_docs': ['s.md']},
            {'id': 'shipped-x', 'name': 'Shipped X', 'phase': 'shipped'},
        ])
        missions_before = (self.repo / h.MISSIONS_REL).read_text()
        rc = h.run_once(dry_run=False, now=NOW)
        self.assertIn(rc, (0, 2))
        data = json.loads(self.proj.read_text())
        self.assertEqual([p['id'] for p in data['projects']], ['flight-x'])  # shipped skipped
        ph = data['projects'][0]['phases'][0]
        self.assertEqual(ph['lifecycle_state'], 'building')      # in_flight → building
        self.assertEqual(ph['sequence_ref'], 'flight-x-001')      # from seq-step task
        self.assertEqual(data['projects'][0]['promoted_from'],
                         {'kind': 'mission', 'mission_id': 'flight-x'})
        # missions.json is READ-ONLY here — never written by this healer.
        self.assertEqual((self.repo / h.MISSIONS_REL).read_text(), missions_before)

    def test_mirror_is_idempotent_second_tick_noop(self):
        self.proj.write_text(json.dumps({'schema_version': 1, 'projects': []}, indent=2) + '\n')
        self._git('add', '.'); self._git('commit', '-q', '-m', 'seed')
        self._write_missions([{'id': 'flight-x', 'name': 'Flight X',
                               'phase': 'in_flight', 'brief': 'x', 'task_ids': [],
                               'repo': 'ourliberty-agent-core'}])
        h.run_once(dry_run=False, now=NOW)            # first tick mirrors + commits
        before = self.proj.read_text()
        rc = h.run_once(dry_run=False, now=NOW)       # second tick: project exists → no-op
        self.assertEqual(rc, 0)
        self.assertEqual(self.proj.read_text(), before)

    def test_no_missions_file_mirrors_nothing(self):
        # The common path before deploy: no missions.json under the repo → fail-safe
        # read returns [] → nothing mirrored, the normalize+commit proceeds.
        self.proj.write_text(json.dumps({'schema_version': 1, 'projects': []}, indent=2) + '\n')
        self._git('add', '.'); self._git('commit', '-q', '-m', 'seed')
        before = self.proj.read_text()
        rc = h.run_once(dry_run=False, now=NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(self.proj.read_text(), before)

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
        # Capture the healer's log so we can assert the report itself, not just
        # the side effects — the old 'no delta' wording is what hid this state.
        lines: list[str] = []
        orig_log = h.log
        h.log = lambda msg: lines.append(msg)
        try:
            rc = h.run_once(dry_run=True, now=NOW)
        finally:
            h.log = orig_log
        self.assertEqual(rc, 0)
        self.assertEqual(self.proj.read_text(), before)  # dry-run writes nothing
        report = ' '.join(lines)
        self.assertIn('git delta', report)       # reports the real (git) delta
        self.assertNotIn('no delta', report)      # not the masking message
        # And the tree is still dirty (dry-run did not commit).
        dirty = subprocess.run(
            ['git', 'status', '--porcelain', h.PROJECTS_REL],
            cwd=str(self.repo), capture_output=True, text=True)
        self.assertTrue(dirty.stdout.strip())


    # ---- auto-retire visibility window (projects-v3) -------------------------
    def _seed_done_project(self, done_at):
        """Commit an already-normalized all-phases-done project whose phase
        finished at ``done_at`` (so the retire pass sees that as the Done moment)."""
        import projects_store
        normalized, _ = projects_store.normalize_registry(
            {'projects': [{'id': 'proj-done',
                           'phases': [{'id': 'ph-1', 'lifecycle_state': 'done'}]}]},
            now=NOW)
        normalized['projects'][0]['phases'][0]['updated_at'] = done_at.isoformat()
        self.proj.write_text(json.dumps(normalized, indent=2) + '\n')
        self._git('add', '.'); self._git('commit', '-q', '-m', 'seed done project')

    def _capture_log(self, *, dry_run):
        lines: list[str] = []
        orig = h.log
        h.log = lambda msg: lines.append(msg)
        try:
            rc = h.run_once(dry_run=dry_run, now=NOW)
        finally:
            h.log = orig
        return rc, ' '.join(lines)

    def test_done_project_past_window_auto_retires(self):
        self._seed_done_project(NOW - timedelta(hours=49))  # past the default 48h
        rc, report = self._capture_log(dry_run=False)
        self.assertIn(rc, (0, 2))
        data = json.loads(self.proj.read_text())
        self.assertEqual(data['projects'][0]['state'], 'retired')
        # the log explains why/when: window + the done_at/age detail.
        self.assertIn('visibility window', report)
        self.assertIn('done_at=', report)
        self.assertIn('age=', report)

    def test_env_override_widens_window_keeps_done_project(self):
        # 49h-old project would retire under the default 48h window; a 100h env
        # override must keep it active — proving the env is read AND threaded.
        self._seed_done_project(NOW - timedelta(hours=49))
        os.environ['OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC'] = str(100 * 3600)
        try:
            h.run_once(dry_run=False, now=NOW)
        finally:
            os.environ.pop('OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC', None)
        data = json.loads(self.proj.read_text())
        self.assertEqual(data['projects'][0]['state'], 'active')  # still lingering

    def test_dry_run_logs_window_and_age(self):
        self._seed_done_project(NOW - timedelta(hours=49))
        rc, report = self._capture_log(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn('would retire', report)
        self.assertIn('visibility window', report)
        self.assertIn('age=', report)
        # dry-run mutates nothing on disk.
        self.assertEqual(json.loads(self.proj.read_text())['projects'][0]['state'], 'active')


class RetireWindowEnvTest(unittest.TestCase):
    """The env read that keeps projects_store.retire_completed_projects pure."""

    def tearDown(self):
        os.environ.pop('OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC', None)

    def test_default_is_store_constant(self):
        import projects_store
        os.environ.pop('OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC', None)
        self.assertEqual(h._retire_window_sec(),
                         projects_store.DONE_RETIRE_VISIBILITY_SEC)

    def test_override_is_read(self):
        os.environ['OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC'] = '0'
        self.assertEqual(h._retire_window_sec(), 0)

    def test_bad_int_falls_back_to_default(self):
        import projects_store
        os.environ['OURLIBERTY_PROJECTS_RETIRE_WINDOW_SEC'] = 'not-an-int'
        self.assertEqual(h._retire_window_sec(),
                         projects_store.DONE_RETIRE_VISIBILITY_SEC)


if __name__ == '__main__':
    unittest.main()
