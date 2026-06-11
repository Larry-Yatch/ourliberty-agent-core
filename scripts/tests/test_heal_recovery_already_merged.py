#!/usr/bin/env python3
"""Tests for heal_recovery_already_merged.

Covers the nervous-system-audit (2026-06-05) fixes:
  - AGENTS_ROOT honors OURLIBERTY_AGENTS_ROOT (path isolation / test safety)
  - multi-repo PR search across tracked REPOS (single-repo override still works)
  - query_merged_pr tolerates a per-repo error and keeps searching
plus a main()-level archive of an already-merged recovery task.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_recovery_already_merged
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

_ENV_KEYS = ('OURLIBERTY_AGENTS_ROOT', 'OURLIBERTY_VERIFY_REPO')


class _CompletedProc:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


class _ReloadBase(unittest.TestCase):
    """Reload the module under a tmp OURLIBERTY_AGENTS_ROOT, then restore the
    prior environment AND reload the module back to its default state so no
    stale tmp-rooted module is left in sys.modules for sibling test modules."""

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        import heal_recovery_already_merged as h  # noqa: E402
        self.h = importlib.reload(h)

    def tearDown(self) -> None:
        for k in _ENV_KEYS:
            if self._saved_env[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = self._saved_env[k]
        self._tmp.cleanup()
        # Rebind the cached module to the restored (non-tmp) environment.
        importlib.reload(self.h)


class VerifyReposTest(_ReloadBase):
    def test_default_scans_all_tracked_repos(self) -> None:
        self.assertEqual(self.h.verify_repos(), self.h.REPOS)
        self.assertIn('Larry-Yatch/ourliberty-agent-core', self.h.REPOS)
        self.assertIn('Larry-Yatch/ourliberty-dashboard', self.h.REPOS)

    def test_single_env_override(self) -> None:
        os.environ['OURLIBERTY_VERIFY_REPO'] = 'owner/other'
        self.assertEqual(self.h.verify_repos(), ['owner/other'])

    def test_returns_copy_not_module_list(self) -> None:
        # Mutating the result must not corrupt the shared REPOS constant.
        repos = self.h.verify_repos()
        repos.append('owner/extra')
        self.assertNotIn('owner/extra', self.h.REPOS)

    def test_agents_root_honors_env(self) -> None:
        self.assertEqual(self.h.AGENTS_ROOT, self.root)
        self.assertEqual(self.h.KILL_SWITCH, self.root / 'healers.disabled')


class QueryMergedPrTest(_ReloadBase):
    def test_finds_in_first_repo(self) -> None:
        self.h.REPOS = ['a/one', 'b/two']
        hit = [{'number': 7, 'title': '[PR-1f] x', 'mergedAt': '2026-06-01T00:00:00Z'}]
        with mock.patch.object(self.h.subprocess, 'run',
                               return_value=_CompletedProc(0, json.dumps(hit))) as run:
            pr = self.h.query_merged_pr('PR-1f')
        self.assertIsNotNone(pr)
        self.assertEqual(pr['number'], 7)
        self.assertEqual(pr['repo'], 'a/one')
        run.assert_called_once()  # short-circuits on first hit

    def test_falls_through_to_second_repo(self) -> None:
        self.h.REPOS = ['a/one', 'b/two']
        miss = _CompletedProc(0, json.dumps([]))
        hit = _CompletedProc(0, json.dumps(
            [{'number': 9, 'title': '[PR-2a] y', 'mergedAt': '2026-06-02T00:00:00Z'}]))
        with mock.patch.object(self.h.subprocess, 'run', side_effect=[miss, hit]):
            pr = self.h.query_merged_pr('PR-2a')
        self.assertEqual(pr['repo'], 'b/two')

    def test_per_repo_error_does_not_abort_search(self) -> None:
        self.h.REPOS = ['a/one', 'b/two']
        boom = self.h.subprocess.TimeoutExpired(cmd='gh', timeout=15)
        hit = _CompletedProc(0, json.dumps(
            [{'number': 11, 'title': '[PR-3c] z', 'mergedAt': '2026-06-03T00:00:00Z'}]))
        with mock.patch.object(self.h.subprocess, 'run', side_effect=[boom, hit]):
            pr = self.h.query_merged_pr('PR-3c')
        self.assertEqual(pr['number'], 11)
        self.assertEqual(pr['repo'], 'b/two')

    def test_no_match_returns_none(self) -> None:
        self.h.REPOS = ['a/one', 'b/two']
        with mock.patch.object(self.h.subprocess, 'run',
                               return_value=_CompletedProc(0, json.dumps([]))) as run:
            self.assertIsNone(self.h.query_merged_pr('PR-none'))
        self.assertEqual(run.call_count, 2)  # both repos searched on a miss


class MainArchiveTest(_ReloadBase):
    def test_archives_recovery_task_whose_pr_is_merged(self) -> None:
        inbox = self.root / 'inboxes' / 'forge'
        inbox.mkdir(parents=True)
        task = inbox / '00-RECOVER-PR-1f.json'
        task.write_text(json.dumps({'sub_task_id': 'PR-1f'}))
        pr = {'number': 2108, 'title': '[PR-1f] ship it',
              'mergedAt': '2026-06-01T00:00:00Z', 'repo': 'a/one'}
        with mock.patch.object(self.h, 'query_merged_pr', return_value=pr):
            rc = self.h.main()
        self.assertEqual(rc, 0)
        self.assertFalse(task.exists())
        archived = inbox / 'archive' / '00-RECOVER-PR-1f.json'
        self.assertTrue(archived.exists())
        sidecar = inbox / 'archive' / '00-RECOVER-PR-1f.archive-reason.txt'
        self.assertIn('repo=a/one', sidecar.read_text())
        self.assertIn('pr=#2108', sidecar.read_text())

    def test_kill_switch_exits_clean(self) -> None:
        (self.root / 'healers.disabled').write_text('')
        with mock.patch.object(self.h, 'query_merged_pr') as q:
            rc = self.h.main()
        self.assertEqual(rc, 0)
        q.assert_not_called()


if __name__ == '__main__':
    unittest.main()
