#!/usr/bin/env python3
"""Tests for heal_recovery_already_merged.

Covers the nervous-system-audit (2026-06-05) fixes:
  - AGENTS_ROOT honors OURLIBERTY_AGENTS_ROOT (path isolation / test safety)
  - multi-repo PR search via OURLIBERTY_VERIFY_REPOS / OURLIBERTY_VERIFY_REPO
  - query_merged_pr tolerates a per-repo error and keeps searching
plus a main()-level archive of an already-merged recovery task.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_recovery_already_merged
"""
from __future__ import annotations

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


def _reload_with_root(root: Path):
    """(Re)import the module with OURLIBERTY_AGENTS_ROOT pointed at `root` so
    its module-level path constants resolve under the tmp tree."""
    os.environ['OURLIBERTY_AGENTS_ROOT'] = str(root)
    import heal_recovery_already_merged as h  # noqa: E402
    return importlib.reload(h)


class _CompletedProc:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


class VerifyReposTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Clear repo env so each test sets its own.
        for k in ('OURLIBERTY_VERIFY_REPOS', 'OURLIBERTY_VERIFY_REPO'):
            os.environ.pop(k, None)
        self.h = _reload_with_root(self.root)

    def tearDown(self) -> None:
        for k in ('OURLIBERTY_VERIFY_REPOS', 'OURLIBERTY_VERIFY_REPO',
                  'OURLIBERTY_AGENTS_ROOT'):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_default_single_repo(self) -> None:
        self.assertEqual(self.h.verify_repos(), [self.h._DEFAULT_VERIFY_REPO])

    def test_single_env_override(self) -> None:
        os.environ['OURLIBERTY_VERIFY_REPO'] = 'owner/other'
        self.assertEqual(self.h.verify_repos(), ['owner/other'])

    def test_multi_env_override_wins_and_splits(self) -> None:
        os.environ['OURLIBERTY_VERIFY_REPOS'] = 'a/one, b/two ,c/three'
        os.environ['OURLIBERTY_VERIFY_REPO'] = 'ignored/single'
        self.assertEqual(self.h.verify_repos(), ['a/one', 'b/two', 'c/three'])

    def test_agents_root_honors_env(self) -> None:
        self.assertEqual(self.h.AGENTS_ROOT, self.root)
        self.assertEqual(self.h.KILL_SWITCH, self.root / 'healers.disabled')


class QueryMergedPrTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for k in ('OURLIBERTY_VERIFY_REPOS', 'OURLIBERTY_VERIFY_REPO'):
            os.environ.pop(k, None)
        self.h = _reload_with_root(self.root)

    def tearDown(self) -> None:
        for k in ('OURLIBERTY_VERIFY_REPOS', 'OURLIBERTY_VERIFY_REPO',
                  'OURLIBERTY_AGENTS_ROOT'):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_finds_in_first_repo(self) -> None:
        os.environ['OURLIBERTY_VERIFY_REPOS'] = 'a/one,b/two'
        hit = [{'number': 7, 'title': '[PR-1f] x', 'mergedAt': '2026-06-01T00:00:00Z'}]
        with mock.patch.object(self.h.subprocess, 'run',
                               return_value=_CompletedProc(0, json.dumps(hit))) as run:
            pr = self.h.query_merged_pr('PR-1f')
        self.assertIsNotNone(pr)
        self.assertEqual(pr['number'], 7)
        self.assertEqual(pr['repo'], 'a/one')
        run.assert_called_once()  # short-circuits on first hit

    def test_falls_through_to_second_repo(self) -> None:
        os.environ['OURLIBERTY_VERIFY_REPOS'] = 'a/one,b/two'
        miss = _CompletedProc(0, json.dumps([]))
        hit = _CompletedProc(0, json.dumps(
            [{'number': 9, 'title': '[PR-2a] y', 'mergedAt': '2026-06-02T00:00:00Z'}]))
        with mock.patch.object(self.h.subprocess, 'run', side_effect=[miss, hit]):
            pr = self.h.query_merged_pr('PR-2a')
        self.assertEqual(pr['repo'], 'b/two')

    def test_per_repo_error_does_not_abort_search(self) -> None:
        os.environ['OURLIBERTY_VERIFY_REPOS'] = 'a/one,b/two'
        boom = self.h.subprocess.TimeoutExpired(cmd='gh', timeout=15)
        hit = _CompletedProc(0, json.dumps(
            [{'number': 11, 'title': '[PR-3c] z', 'mergedAt': '2026-06-03T00:00:00Z'}]))
        with mock.patch.object(self.h.subprocess, 'run', side_effect=[boom, hit]):
            pr = self.h.query_merged_pr('PR-3c')
        self.assertEqual(pr['number'], 11)
        self.assertEqual(pr['repo'], 'b/two')

    def test_no_match_returns_none(self) -> None:
        with mock.patch.object(self.h.subprocess, 'run',
                               return_value=_CompletedProc(0, json.dumps([]))):
            self.assertIsNone(self.h.query_merged_pr('PR-none'))


class MainArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for k in ('OURLIBERTY_VERIFY_REPOS', 'OURLIBERTY_VERIFY_REPO'):
            os.environ.pop(k, None)
        self.h = _reload_with_root(self.root)

    def tearDown(self) -> None:
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        self._tmp.cleanup()

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
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / 'healers.disabled').write_text('')
        with mock.patch.object(self.h, 'query_merged_pr') as q:
            rc = self.h.main()
        self.assertEqual(rc, 0)
        q.assert_not_called()


if __name__ == '__main__':
    unittest.main()
