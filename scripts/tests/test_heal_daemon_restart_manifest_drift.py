#!/usr/bin/env python3
"""Tests for heal_daemon_restart_manifest_drift (manifest-self-heal-001).

Covers:
  1. compute_drift — the pure manifest diff (added/removed paths, added/dropped
     units, entrypoint change, _meta-only difference is ignored).
  2. commit_and_push_manifest — tmp git repo: clean / off-main / pathspec
     isolation (never co-commits another dirty file) / idempotency.
  3. run_once — orchestration with mocks: no-drift no-op, drift → write+commit+
     alert, wrong-branch (no write), write-failed (escalate), build error.
  4. emit_alert — routing: success → digest/closure, failure → escalate w/ cmd.
  5. main — kill switch short-circuits.

Run:
    python3 -m unittest scripts.tests.test_heal_daemon_restart_manifest_drift
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_daemon_restart_manifest_drift as h  # noqa: E402


def _manifest(units: dict, meta: dict | None = None) -> dict:
    out = {'units': units}
    if meta is not None:
        out['_meta'] = meta
    return out


def _unit(entrypoint: str, watch: list[str]) -> dict:
    return {'entrypoint': entrypoint, 'watch_paths': sorted(watch)}


class ComputeDriftTest(unittest.TestCase):
    def test_identical_no_drift(self):
        u = {'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py', 'scripts/a.py'])}
        d = h.compute_drift(_manifest(u), _manifest(dict(u)))
        self.assertFalse(d.has_drift)
        self.assertEqual(d.one_line(), '(none)')

    def test_meta_only_difference_ignored(self):
        u = {'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py'])}
        committed = _manifest(dict(u), meta={'note': 'old'})
        fresh = _manifest(dict(u), meta={'note': 'new — self-heal'})
        self.assertFalse(h.compute_drift(committed, fresh).has_drift)

    def test_added_path(self):
        committed = _manifest({'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py'])})
        fresh = _manifest({'ourliberty-x.service':
                           _unit('scripts/x.py', ['scripts/x.py', 'scripts/new.py'])})
        d = h.compute_drift(committed, fresh)
        self.assertTrue(d.has_drift)
        self.assertEqual(d.units_changed['ourliberty-x.service'].added, ['scripts/new.py'])
        self.assertEqual(d.units_changed['ourliberty-x.service'].removed, [])
        self.assertIn('new.py', d.alert_body())

    def test_removed_path(self):
        committed = _manifest({'ourliberty-x.service':
                              _unit('scripts/x.py', ['scripts/x.py', 'scripts/gone.py'])})
        fresh = _manifest({'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py'])})
        d = h.compute_drift(committed, fresh)
        self.assertTrue(d.has_drift)
        self.assertEqual(d.units_changed['ourliberty-x.service'].removed, ['scripts/gone.py'])

    def test_added_unit(self):
        committed = _manifest({})
        fresh = _manifest({'ourliberty-new.service': _unit('scripts/new.py', ['scripts/new.py'])})
        d = h.compute_drift(committed, fresh)
        self.assertTrue(d.has_drift)
        self.assertEqual(d.units_added, ['ourliberty-new.service'])
        self.assertIn('NEW daemons', d.alert_body())

    def test_removed_unit(self):
        committed = _manifest({'ourliberty-old.service': _unit('scripts/old.py', ['scripts/old.py'])})
        fresh = _manifest({})
        d = h.compute_drift(committed, fresh)
        self.assertTrue(d.has_drift)
        self.assertEqual(d.units_removed, ['ourliberty-old.service'])

    def test_entrypoint_change(self):
        committed = _manifest({'ourliberty-x.service': _unit('scripts/old.py', ['scripts/old.py'])})
        fresh = _manifest({'ourliberty-x.service': _unit('scripts/new.py', ['scripts/old.py'])})
        d = h.compute_drift(committed, fresh)
        self.assertTrue(d.has_drift)
        self.assertEqual(d.units_changed['ourliberty-x.service'].entrypoint_changed,
                         ('scripts/old.py', 'scripts/new.py'))

    def test_path_list_truncates_with_count_not_silently(self):
        many = [f'scripts/m{i}.py' for i in range(30)]
        committed = _manifest({'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py'])})
        fresh = _manifest({'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py'] + many)})
        d = h.compute_drift(committed, fresh)
        body = d.alert_body()
        self.assertIn(f'+{len(many)})', body)            # exact total present
        self.assertIn('more)', body)                      # tail collapsed, not dropped


class CommitAndPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='manifest-drift-git-')
        self.base = Path(self.tmp)
        self.origin = self.base / 'origin.git'
        # Pin the bare origin's default branch to main. Without this the origin
        # HEAD defaults to the host's init.defaultBranch (often `master` on the
        # droplet, which has no global override), so a later `git clone` checks
        # out that empty branch instead of the seeded `main` — the clone then has
        # no working tree and writing config/<manifest> hits FileNotFoundError.
        # --initial-branch needs git >= 2.28 (present on the droplet).
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', '-q',
                        str(self.origin)], check=True,
                       capture_output=True, text=True)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self._git('init', '-q', '-b', 'main')
        self._git('config', 'user.email', 'test@test')
        self._git('config', 'user.name', 'Test')
        self._git('remote', 'add', 'origin', str(self.origin))
        (self.repo / 'config').mkdir()
        self.manifest = self.repo / h.MANIFEST_REL
        self.manifest.write_text(json.dumps({'units': {}}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed')
        self._git('push', '-q', '-u', 'origin', 'main')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=str(self.repo), check=True,
                       capture_output=True, text=True)

    @staticmethod
    def _run(cwd, *args):
        subprocess.run(['git', *args], cwd=str(cwd), check=True,
                       capture_output=True, text=True)

    def test_nothing_when_clean(self):
        self.assertEqual(h.commit_and_push_manifest(self.repo, h.MANIFEST_REL, 'audit'),
                         'nothing')

    def test_refuses_off_main(self):
        self._git('checkout', '-q', '-b', 'forge/feature')
        self.manifest.write_text(json.dumps({'units': {'a': 1}}) + '\n')
        self.assertEqual(h.commit_and_push_manifest(self.repo, h.MANIFEST_REL, 'audit'),
                         'wrong-branch')

    def test_commits_pushes_only_manifest_path(self):
        # A second machine-owned file is ALSO dirty; the pathspec-limited commit
        # must NOT sweep it in (single-committer invariant).
        other = self.repo / 'config' / 'other-machine-owned.json'
        other.write_text('{"dirty": true}\n')
        self._git('add', 'config/other-machine-owned.json')
        self._git('commit', '-q', '-m', 'seed other')
        self._git('push', '-q', 'origin', 'main')
        other.write_text('{"dirty": "CHANGED"}\n')          # leave it dirty

        self.manifest.write_text(json.dumps({'units': {'x': 1}}) + '\n')
        status = h.commit_and_push_manifest(self.repo, h.MANIFEST_REL, 'audit-line')
        self.assertEqual(status, 'committed')
        # The HEAD commit carries the audit line and touches ONLY the manifest.
        files = subprocess.run(['git', 'show', '--name-only', '--pretty=', 'HEAD'],
                               cwd=str(self.repo), capture_output=True, text=True).stdout.split()
        self.assertEqual(files, [h.MANIFEST_REL])
        msg = subprocess.run(['git', 'log', '-1', '--pretty=%B'],
                             cwd=str(self.repo), capture_output=True, text=True).stdout
        self.assertIn('manifest-drift healer', msg)
        self.assertIn('audit-line', msg)
        # The other file is still dirty (untouched by our commit).
        self.assertNotEqual(subprocess.run(['git', 'diff', '--quiet', '--',
                                            'config/other-machine-owned.json'],
                                           cwd=str(self.repo)).returncode, 0)
        # Idempotent: nothing left to commit for the manifest.
        self.assertEqual(h.commit_and_push_manifest(self.repo, h.MANIFEST_REL, 'audit'),
                         'nothing')

    def test_rebase_conflict_does_not_diverge_origin(self):
        # Regression for the divergence-page risk: a CONCURRENT origin-side
        # manifest change (a human `regenerate`) makes our push non-FF and the
        # rebase conflict; the healer must drop its local commit so the droplet's
        # HEAD stays an ancestor of origin (sync's `merge --ff-only` must succeed).
        clone = self.base / 'clone'
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(clone)], check=True,
                       capture_output=True, text=True)
        self._run(clone, 'config', 'user.email', 't2@test')
        self._run(clone, 'config', 'user.name', 'T2')
        (clone / h.MANIFEST_REL).write_text(json.dumps({'units': {'origin_side': 1}}) + '\n')
        self._run(clone, 'commit', '-q', '-am', 'origin-side manifest change')
        self._run(clone, 'push', '-q', 'origin', 'main')

        # Our side makes a DIFFERENT manifest change and tries to commit+push.
        self.manifest.write_text(json.dumps({'units': {'our_side': 2}}) + '\n')
        status = h.commit_and_push_manifest(self.repo, h.MANIFEST_REL, 'audit')
        self.assertEqual(status, 'push-failed')

        # Critical invariant: local HEAD is NOT diverged from origin — i.e. it is
        # an ancestor of origin/main, so a downstream `git merge --ff-only` works
        # and never pages. (Without the reset --hard HEAD~1 fix, HEAD would carry
        # our orphan commit and this is-ancestor check would fail.)
        self._git('fetch', '-q', 'origin', 'main')
        anc = subprocess.run(['git', 'merge-base', '--is-ancestor', 'HEAD', 'origin/main'],
                             cwd=str(self.repo)).returncode
        self.assertEqual(anc, 0, 'local HEAD diverged from origin — sync ff-only would page')


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.committed = _manifest({'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py'])})
        self.drifted = _manifest({'ourliberty-x.service':
                                 _unit('scripts/x.py', ['scripts/x.py', 'scripts/new.py'])})

    def test_no_drift_is_noop(self):
        with mock.patch.object(h.drm, 'load_manifest', return_value=self.committed), \
                mock.patch.object(h.drm, 'build_manifest', return_value=self.committed), \
                mock.patch.object(h, 'atomic_write_json') as write, \
                mock.patch.object(h, 'commit_and_push_manifest') as commit, \
                mock.patch.object(h, 'emit_alert') as alert:
            self.assertEqual(h.run_once(), 'fresh')
            write.assert_not_called()
            commit.assert_not_called()
            alert.assert_not_called()

    def test_drift_writes_commits_alerts(self):
        with mock.patch.object(h.drm, 'load_manifest', return_value=self.committed), \
                mock.patch.object(h.drm, 'build_manifest', return_value=self.drifted), \
                mock.patch.object(h, 'current_branch', return_value='main'), \
                mock.patch.object(h, 'atomic_write_json') as write, \
                mock.patch.object(h, 'commit_and_push_manifest', return_value='committed') as commit, \
                mock.patch.object(h, 'emit_alert') as alert:
            self.assertEqual(h.run_once(), 'committed')
            write.assert_called_once()
            commit.assert_called_once()
            alert.assert_called_once()
            self.assertEqual(alert.call_args.args[1], 'committed')

    def test_wrong_branch_does_not_write(self):
        with mock.patch.object(h.drm, 'load_manifest', return_value=self.committed), \
                mock.patch.object(h.drm, 'build_manifest', return_value=self.drifted), \
                mock.patch.object(h, 'current_branch', return_value='forge/x'), \
                mock.patch.object(h, 'atomic_write_json') as write, \
                mock.patch.object(h, 'commit_and_push_manifest') as commit, \
                mock.patch.object(h, 'emit_alert') as alert:
            self.assertEqual(h.run_once(), 'wrong-branch')
            write.assert_not_called()
            commit.assert_not_called()
            alert.assert_called_once_with(mock.ANY, 'wrong-branch')

    def test_write_failure_escalates(self):
        with mock.patch.object(h.drm, 'load_manifest', return_value=self.committed), \
                mock.patch.object(h.drm, 'build_manifest', return_value=self.drifted), \
                mock.patch.object(h, 'current_branch', return_value='main'), \
                mock.patch.object(h, 'atomic_write_json', side_effect=OSError('read-only fs')), \
                mock.patch.object(h, 'commit_and_push_manifest') as commit, \
                mock.patch.object(h, 'emit_alert') as alert:
            self.assertEqual(h.run_once(), 'write-failed')
            commit.assert_not_called()
            alert.assert_called_once_with(mock.ANY, 'write-failed')

    def test_build_error_is_swallowed(self):
        with mock.patch.object(h.drm, 'load_manifest', return_value=self.committed), \
                mock.patch.object(h.drm, 'build_manifest', side_effect=RuntimeError('bad ast')), \
                mock.patch.object(h, 'emit_alert') as alert:
            self.assertEqual(h.run_once(), 'error')
            alert.assert_not_called()


class FakeLarryAlerts:
    """Minimal larry_alerts stand-in capturing append_alert kwargs."""
    def __init__(self, route='digest'):
        self._route = route
        self.calls = []

    def classify_route(self, source, subject, healed):
        return self._route

    def append_alert(self, **kwargs):
        self.calls.append(kwargs)
        return True


class EmitAlertTest(unittest.TestCase):
    def _drift(self):
        committed = _manifest({'ourliberty-x.service': _unit('scripts/x.py', ['scripts/x.py'])})
        fresh = _manifest({'ourliberty-x.service':
                          _unit('scripts/x.py', ['scripts/x.py', 'scripts/new.py'])})
        return h.compute_drift(committed, fresh)

    def test_success_routes_per_classify(self):
        fake = FakeLarryAlerts(route='digest')
        with mock.patch.object(h, '_import_larry_alerts', return_value=fake):
            self.assertTrue(h.emit_alert(self._drift(), 'committed'))
        self.assertEqual(fake.calls[0]['route'], 'digest')
        self.assertEqual(fake.calls[0]['subject'], 'regenerated')
        self.assertIn('new.py', fake.calls[0]['message'])

    def test_failure_escalates_with_command(self):
        for status in ('push-failed', 'commit-failed', 'wrong-branch', 'write-failed'):
            fake = FakeLarryAlerts()
            with mock.patch.object(h, '_import_larry_alerts', return_value=fake):
                self.assertTrue(h.emit_alert(self._drift(), status))
            self.assertEqual(fake.calls[0]['route'], 'escalate', status)
            self.assertEqual(fake.calls[0]['subject'], status)
            self.assertIn('regenerate', fake.calls[0]['suggested_action'])

    def test_never_raises_on_alert_failure(self):
        with mock.patch.object(h, '_import_larry_alerts', side_effect=RuntimeError('no module')):
            self.assertFalse(h.emit_alert(self._drift(), 'committed'))


class MainKillSwitchTest(unittest.TestCase):
    def test_kill_switch_short_circuits(self):
        tmp = tempfile.mkdtemp(prefix='manifest-drift-kill-')
        try:
            (Path(tmp) / 'healers.disabled').write_text('')
            with mock.patch.dict(os.environ, {'OURLIBERTY_AGENTS_ROOT': tmp}), \
                    mock.patch.object(h, 'run_once') as run:
                self.assertEqual(h.main(), 0)
                run.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_runs_when_no_kill_switch(self):
        tmp = tempfile.mkdtemp(prefix='manifest-drift-nokill-')
        try:
            with mock.patch.dict(os.environ, {'OURLIBERTY_AGENTS_ROOT': tmp}), \
                    mock.patch.object(h, 'run_once', return_value='fresh') as run:
                self.assertEqual(h.main(), 0)
                run.assert_called_once()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
