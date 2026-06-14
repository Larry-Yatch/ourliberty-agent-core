#!/usr/bin/env python3
"""Tests for heal_droplet_git_drift (droplet-drift-discipline-v2, 2026-05-27).

Covers the three threshold trips (ahead/behind/uncommitted) at the boundary
on both sides, the no-false-positive case, the git-fetch failsafe, and the
local 6h subject cooldown.

Test isolation per PR #137: every fixture lives in tmp_path; all git
shellouts are mocked; larry_alerts is patched at the module surface so no
DM ever leaves the test process; OURLIBERTY_AGENTS_ROOT redirects every
state/log/heartbeat file to the per-test tmp dir.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_droplet_git_drift
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
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

import heal_droplet_git_drift as h  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test."""

    def setUp(self):
        super().setUp()
        self._isolated_tmp = tempfile.mkdtemp(prefix='git-drift-test-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self._isolated_tmp, sub), exist_ok=True)
        self._env_orig_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        self._env_orig_repo = os.environ.get('OURLIBERTY_REPO_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_tmp
        # REPO_ROOT also pointed inside the tmp so newest_mtime_of() can
        # stat files relative to it during the uncommitted tests.
        self._isolated_repo = os.path.join(self._isolated_tmp, 'repo')
        os.makedirs(self._isolated_repo, exist_ok=True)
        os.environ['OURLIBERTY_REPO_ROOT'] = self._isolated_repo
        importlib.reload(h)

    def tearDown(self):
        for key, orig in (('OURLIBERTY_AGENTS_ROOT', self._env_orig_root),
                          ('OURLIBERTY_REPO_ROOT', self._env_orig_repo)):
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig
        importlib.reload(h)
        shutil.rmtree(self._isolated_tmp, ignore_errors=True)
        super().tearDown()

    def _stub_alerts(self):
        """Patch _import_larry_alerts so append_alert is a recordable Mock."""
        fake_la = mock.MagicMock()
        fake_la.append_alert = mock.MagicMock(return_value=True)
        return mock.patch.object(h, '_import_larry_alerts', return_value=fake_la), fake_la


# -------------------- AHEAD trips --------------------

class AheadTripTests(_IsolatedAgentsRoot):
    def test_ahead_one_commit_older_than_2h_trips(self):
        now = 1_700_000_000.0
        oldest = now - (3 * 60 * 60)  # 3h ago
        state = {'subjects': {}}
        with mock.patch.object(h, 'ahead_count', return_value=1), \
                mock.patch.object(h, 'oldest_unpushed_commit_ts',
                                  return_value=oldest):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_ahead(
                    'origin/main', 'feature-x', state, now=now,
                )
        self.assertTrue(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 1)
        kwargs = fake_la.append_alert.call_args.kwargs
        self.assertEqual(kwargs['subject'], 'droplet-ahead:feature-x')
        self.assertEqual(kwargs['severity'], 'warning')

    def test_ahead_one_commit_within_2h_does_not_trip(self):
        now = 1_700_000_000.0
        oldest = now - (30 * 60)  # 30 min ago — well inside 2h
        state = {'subjects': {}}
        with mock.patch.object(h, 'ahead_count', return_value=1), \
                mock.patch.object(h, 'oldest_unpushed_commit_ts',
                                  return_value=oldest):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_ahead(
                    'origin/main', 'feature-x', state, now=now,
                )
        self.assertFalse(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 0)

    def test_ahead_zero_commits_does_not_trip(self):
        now = 1_700_000_000.0
        state = {'subjects': {}}
        with mock.patch.object(h, 'ahead_count', return_value=0), \
                mock.patch.object(h, 'oldest_unpushed_commit_ts',
                                  return_value=None):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_ahead(
                    'origin/main', 'main', state, now=now,
                )
        self.assertFalse(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 0)


# -------------------- BEHIND trips --------------------

class BehindTripTests(_IsolatedAgentsRoot):
    def test_behind_three_commits_trips(self):
        now = 1_700_000_000.0
        state = {'subjects': {}}
        with mock.patch.object(h, 'behind_count', return_value=3):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_behind(
                    'origin/main', 'main', state, now=now,
                )
        self.assertTrue(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 1)
        self.assertEqual(
            fake_la.append_alert.call_args.kwargs['subject'],
            'droplet-behind:main',
        )

    def test_behind_two_commits_does_not_trip(self):
        # Boundary case: threshold is strict `>`, so exactly 2 does NOT trip.
        now = 1_700_000_000.0
        state = {'subjects': {}}
        with mock.patch.object(h, 'behind_count', return_value=2):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_behind(
                    'origin/main', 'main', state, now=now,
                )
        self.assertFalse(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 0)

    def test_behind_zero_does_not_trip(self):
        now = 1_700_000_000.0
        state = {'subjects': {}}
        with mock.patch.object(h, 'behind_count', return_value=0):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_behind(
                    'origin/main', 'main', state, now=now,
                )
        self.assertFalse(tripped)


# -------------------- UNCOMMITTED trips --------------------

class UncommittedTripTests(_IsolatedAgentsRoot):
    def _write_uncommitted_file(self, mtime: float) -> str:
        # The REPO_ROOT (= self._isolated_repo) holds the tracked files.
        # newest_mtime_of() stats `root / rel`; pass a relative name.
        p = Path(self._isolated_repo) / 'changed.py'
        p.write_text('# touched')
        os.utime(p, (mtime, mtime))
        return 'changed.py'

    def test_uncommitted_older_than_6h_trips(self):
        now = time.time()
        old_mtime = now - (7 * 60 * 60)  # 7h ago
        rel = self._write_uncommitted_file(old_mtime)
        state = {'subjects': {}}
        with mock.patch.object(h, 'uncommitted_files', return_value=[rel]):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_uncommitted(
                    'main', state, now=now,
                )
        self.assertTrue(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 1)
        self.assertEqual(
            fake_la.append_alert.call_args.kwargs['subject'],
            'droplet-uncommitted:main',
        )

    def test_uncommitted_within_6h_does_not_trip(self):
        now = time.time()
        recent_mtime = now - (60 * 60)  # 1h ago
        rel = self._write_uncommitted_file(recent_mtime)
        state = {'subjects': {}}
        with mock.patch.object(h, 'uncommitted_files', return_value=[rel]):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_uncommitted(
                    'main', state, now=now,
                )
        self.assertFalse(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 0)

    def test_no_uncommitted_files_does_not_trip(self):
        now = time.time()
        state = {'subjects': {}}
        with mock.patch.object(h, 'uncommitted_files', return_value=[]):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_uncommitted(
                    'main', state, now=now,
                )
        self.assertFalse(tripped)


# -------------------- failsafes --------------------

class FailsafeTests(_IsolatedAgentsRoot):
    def test_fetch_failure_skips_tick_without_alert(self):
        # rc != 0 from `git fetch` → no alert, no crash, run_tick returns
        # with fetched=False and no signals evaluated.
        with mock.patch.object(h, '_git', return_value=(1, '', 'auth fail')):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                result = h.run_tick(now=time.time())
        self.assertFalse(result['fetched'])
        self.assertFalse(result['ahead'])
        self.assertFalse(result['behind'])
        self.assertFalse(result['uncommitted'])
        self.assertEqual(fake_la.append_alert.call_count, 0)

    def test_no_upstream_skips_ahead_behind_but_still_checks_uncommitted(self):
        # When `@{upstream}` resolution fails, ahead/behind get skipped but
        # the uncommitted check still runs (it doesn't depend on upstream).
        now = time.time()
        with mock.patch.object(h, 'fetch_origin', return_value=True), \
                mock.patch.object(h, 'remote_tracking_ref', return_value=None), \
                mock.patch.object(h, 'current_branch', return_value='main'), \
                mock.patch.object(h, 'uncommitted_files', return_value=[]):
            patcher, _ = self._stub_alerts()
            with patcher:
                result = h.run_tick(now=now)
        self.assertTrue(result['fetched'])
        self.assertFalse(result['ahead'])
        self.assertFalse(result['behind'])
        self.assertFalse(result['uncommitted'])


# -------------------- cooldown --------------------

class CooldownTests(_IsolatedAgentsRoot):
    def test_same_subject_within_6h_suppresses_second_alert(self):
        now = 1_700_000_000.0
        oldest = now - (3 * 60 * 60)
        state = {'subjects': {}}
        with mock.patch.object(h, 'ahead_count', return_value=1), \
                mock.patch.object(h, 'oldest_unpushed_commit_ts',
                                  return_value=oldest):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                h.evaluate_ahead('origin/main', 'main', state, now=now)
                # 5h later — still inside the 6h subject cooldown.
                h.evaluate_ahead(
                    'origin/main', 'main', state, now=now + (5 * 60 * 60),
                )
        self.assertEqual(fake_la.append_alert.call_count, 1)

    def test_same_subject_after_6h_re_alerts(self):
        now = 1_700_000_000.0
        oldest = now - (3 * 60 * 60)
        state = {'subjects': {}}
        with mock.patch.object(h, 'ahead_count', return_value=1), \
                mock.patch.object(h, 'oldest_unpushed_commit_ts',
                                  return_value=oldest):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                h.evaluate_ahead('origin/main', 'main', state, now=now)
                # 7h later — outside the cooldown; second alert fires.
                h.evaluate_ahead(
                    'origin/main', 'main', state, now=now + (7 * 60 * 60),
                )
        self.assertEqual(fake_la.append_alert.call_count, 2)


# -------------------- clean-tree no-false-positive --------------------

class CleanTreeTests(_IsolatedAgentsRoot):
    def test_clean_tree_no_alerts(self):
        # Droplet at origin/main, clean tree → no alerts on any signal.
        now = time.time()
        with mock.patch.object(h, 'fetch_origin', return_value=True), \
                mock.patch.object(h, 'remote_tracking_ref',
                                  return_value='origin/main'), \
                mock.patch.object(h, 'current_branch', return_value='main'), \
                mock.patch.object(h, 'ahead_count', return_value=0), \
                mock.patch.object(h, 'behind_count', return_value=0), \
                mock.patch.object(h, 'uncommitted_files', return_value=[]):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                result = h.run_tick(now=now)
        self.assertTrue(result['fetched'])
        self.assertFalse(result['ahead'])
        self.assertFalse(result['behind'])
        self.assertFalse(result['uncommitted'])
        self.assertEqual(fake_la.append_alert.call_count, 0)


# -------------------- kill switch --------------------

class KillSwitchTests(_IsolatedAgentsRoot):
    def test_kill_switch_short_circuits(self):
        h.KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        h.KILL_SWITCH.touch()
        with mock.patch.object(h, 'fetch_origin') as m_fetch:
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(m_fetch.call_count, 0)


# -------------------- multi-signal simultaneous --------------------

class MultiSignalTests(_IsolatedAgentsRoot):
    def test_ahead_and_behind_trip_independent_subjects(self):
        # Both signals trip in the same tick → one alert per subject.
        now = time.time()
        oldest = now - (3 * 60 * 60)
        with mock.patch.object(h, 'fetch_origin', return_value=True), \
                mock.patch.object(h, 'remote_tracking_ref',
                                  return_value='origin/main'), \
                mock.patch.object(h, 'current_branch', return_value='hot'), \
                mock.patch.object(h, 'ahead_count', return_value=1), \
                mock.patch.object(h, 'oldest_unpushed_commit_ts',
                                  return_value=oldest), \
                mock.patch.object(h, 'behind_count', return_value=5), \
                mock.patch.object(h, 'uncommitted_files', return_value=[]):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                result = h.run_tick(now=now)
        self.assertTrue(result['ahead'])
        self.assertTrue(result['behind'])
        subjects = sorted(
            call.kwargs['subject']
            for call in fake_la.append_alert.call_args_list
        )
        self.assertEqual(subjects, [
            'droplet-ahead:hot', 'droplet-behind:hot',
        ])


# -------------------- uncommitted-files parser --------------------

class UncommittedParserTests(_IsolatedAgentsRoot):
    def test_porcelain_parses_modified_and_added_lines(self):
        # `XY <path>` lines should each produce one entry.
        with mock.patch.object(
            h, '_git', return_value=(0, ' M scripts/a.py\nA  scripts/b.py\n', ''),
        ):
            paths = h.uncommitted_files()
        self.assertEqual(paths, ['scripts/a.py', 'scripts/b.py'])

    def test_porcelain_handles_rename(self):
        with mock.patch.object(
            h, '_git', return_value=(0, 'R  old.py -> new.py\n', ''),
        ):
            paths = h.uncommitted_files()
        self.assertEqual(paths, ['new.py'])

    def test_porcelain_empty_returns_empty(self):
        with mock.patch.object(h, '_git', return_value=(0, '', '')):
            paths = h.uncommitted_files()
        self.assertEqual(paths, [])

    def test_porcelain_error_returns_empty(self):
        with mock.patch.object(h, '_git', return_value=(1, '', 'fatal: nope')):
            paths = h.uncommitted_files()
        self.assertEqual(paths, [])


# -------------------- healer-managed runtime-path carve-out --------------------

class HealerManagedDirtCarveOutTests(_IsolatedAgentsRoot):
    """captures.json (and any config/healer-managed-runtime-paths.json entry) is
    nominal-by-design dirt — it must never page, but non-managed dirt still must.
    """

    def _write_stale(self, rel: str, age_h: float, now: float) -> None:
        p = Path(self._isolated_repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('# touched')
        mtime = now - (age_h * 60 * 60)
        os.utime(p, (mtime, mtime))

    def test_captures_only_dirt_stale_does_not_trip(self):
        # Only dirt is the healer-managed captures.json, stale at 9h → NO alert.
        now = 1_700_000_000.0
        managed = h.healer_managed_paths()
        self.assertIn('agents/beacon/captures.json', managed)
        self._write_stale('agents/beacon/captures.json', 9, now)
        state = {'subjects': {}}
        with mock.patch.object(h, 'uncommitted_files',
                               return_value=['agents/beacon/captures.json']):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_uncommitted('main', state, now=now)
        self.assertFalse(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 0)

    def test_captures_plus_non_managed_stale_trips_and_names_non_managed(self):
        # captures.json (managed) + a stale non-managed file → alert STILL fires,
        # named on the non-managed file only; captures.json is not the trigger.
        now = 1_700_000_000.0
        self._write_stale('agents/beacon/captures.json', 9, now)
        self._write_stale('scripts/stale_edit.py', 7, now)
        state = {'subjects': {}}
        with mock.patch.object(
            h, 'uncommitted_files',
            return_value=['agents/beacon/captures.json', 'scripts/stale_edit.py'],
        ):
            patcher, fake_la = self._stub_alerts()
            with patcher:
                tripped = h.evaluate_uncommitted('main', state, now=now)
        self.assertTrue(tripped)
        self.assertEqual(fake_la.append_alert.call_count, 1)
        kwargs = fake_la.append_alert.call_args.kwargs
        self.assertEqual(kwargs['subject'], 'droplet-uncommitted:main')
        # The alert body counts + names ONLY the non-managed dirt.
        self.assertIn('scripts/stale_edit.py', kwargs['message'])
        self.assertNotIn('captures.json', kwargs['message'])
        self.assertIn('1 uncommitted file', kwargs['message'])

    def test_canonical_json_matches_sync_extra_runtime_paths(self):
        # Drift guard: config/healer-managed-runtime-paths.json must equal the
        # bash SYNC_EXTRA_RUNTIME_PATHS array (sole source of truth for sync).
        repo_root = _REPO_SCRIPTS.parent
        config_file = repo_root / 'config' / 'healer-managed-runtime-paths.json'
        import json as _json
        json_paths = _json.loads(config_file.read_text())['paths']

        lib = _REPO_SCRIPTS / '_lib_pulse_runtime.sh'
        import subprocess as _sp
        out = _sp.run(
            ['bash', '-c',
             f'source "{lib}"; printf "%s\\n" "${{SYNC_EXTRA_RUNTIME_PATHS[@]}}"'],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        bash_paths = [ln for ln in out.stdout.splitlines() if ln]
        self.assertEqual(sorted(json_paths), sorted(bash_paths))
        # And the healer's hardcoded fallback must agree too.
        self.assertEqual(sorted(json_paths),
                         sorted(h._HEALER_MANAGED_PATHS_FALLBACK))


if __name__ == '__main__':
    unittest.main()
