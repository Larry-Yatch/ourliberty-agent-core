#!/usr/bin/env python3
"""Tests for stale-worktree-teardown-001 — event-driven worktree teardown.

Spec: when a PR auto-merges, outbox_notifier reaps BOTH the forge and mirror
worktrees that produced it (the branch is deleted, the task is terminal). A
'failed' outcome reaps neither; an in-flight task_id is never touched.

Covers:
  - _teardown_worktrees_for_task removes both forge + mirror worktrees.
  - in-flight task_id (present in AGENTS_ROOT/state/in-flight) is skipped.
  - unconfigured repo coords → no removal (logged + skipped).
  - absent worktrees → no removal call.
  - call-site: merged outcome via _attempt_auto_merge_with_gates triggers
    teardown; failed outcome triggers neither (uses the _AUTO_MERGE_FN_OVERRIDE
    + _AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST seam).

Run:
    cd /home/larry/agent-core &&
    python3 -m unittest scripts.tests.test_outbox_worktree_teardown
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


_AGENTS_ROOT_BACKUP = None
_AGENTS_ROOT_TMPDIR = None

REPO = 'Larry-Yatch/ourliberty-agent-core'


def setUpModule():  # noqa: N802 — unittest hook
    global _AGENTS_ROOT_BACKUP, _AGENTS_ROOT_TMPDIR
    _AGENTS_ROOT_BACKUP = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    _AGENTS_ROOT_TMPDIR = tempfile.mkdtemp(prefix='worktree-teardown-test-')
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_TMPDIR
    for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
        Path(_AGENTS_ROOT_TMPDIR, sub).mkdir(exist_ok=True)
    import outbox_notifier
    importlib.reload(outbox_notifier)


def tearDownModule():  # noqa: N802
    if _AGENTS_ROOT_BACKUP is None:
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
    else:
        os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_BACKUP
    if _AGENTS_ROOT_TMPDIR:
        shutil.rmtree(_AGENTS_ROOT_TMPDIR, ignore_errors=True)
    import outbox_notifier
    importlib.reload(outbox_notifier)


class TeardownTestBase(unittest.TestCase):
    """Fresh tmp AGENTS_ROOT + worktree base; record (don't perform) removals."""

    def setUp(self):
        import outbox_notifier as on
        import worktree_manager as wm
        self.on = on
        self.wm = wm
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {
            'AGENTS_ROOT': on.AGENTS_ROOT,
            'LOG_FILE': on.LOG_FILE,
        }
        on.AGENTS_ROOT = self._root
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        (self._root / 'logs').mkdir(parents=True, exist_ok=True)
        (self._root / 'state' / 'in-flight').mkdir(parents=True, exist_ok=True)

        # Reroute the worktree base so worktree_path_for resolves under tmp.
        self._wt_base = self._root / 'agent-worktrees'
        self._wt_base.mkdir(parents=True, exist_ok=True)
        self._orig_wt_base = wm.WORKTREE_BASE
        wm.WORKTREE_BASE = self._wt_base

        # Record removal requests instead of touching git/filesystem.
        self.removed: list[Path] = []
        self._rm_patch = mock.patch.object(
            wm, '_remove_worktree',
            side_effect=lambda repo, path, log_fn=None: self.removed.append(
                Path(path)
            ),
        )
        self._rm_patch.start()
        # Hermetic: never shell out to `git worktree list` for registration.
        self._reg_patch = mock.patch.object(
            wm, '_is_worktree_registered', return_value=False,
        )
        self._reg_patch.start()

    def tearDown(self):
        self._reg_patch.stop()
        self._rm_patch.stop()
        for k, v in self._originals.items():
            setattr(self.on, k, v)
        self.wm.WORKTREE_BASE = self._orig_wt_base
        self._tmp.cleanup()

    def _make_worktrees(self, task_id: str) -> list[Path]:
        paths = []
        for agent in ('forge', 'mirror'):
            p = self.wm.worktree_path_for(agent, task_id)
            p.mkdir(parents=True, exist_ok=True)
            paths.append(p)
        return paths


class TeardownDirectTest(TeardownTestBase):

    def test_merged_removes_both_worktrees(self):
        task = 'teardown-merged-1'
        forge_wt, mirror_wt = self._make_worktrees(task)
        self.on._teardown_worktrees_for_task(task_id=task, repo_coords=REPO)
        self.assertEqual(sorted(self.removed), sorted([forge_wt, mirror_wt]))

    def test_in_flight_task_is_skipped(self):
        task = 'teardown-inflight-1'
        self._make_worktrees(task)
        (self._root / 'state' / 'in-flight' / f'{task}.json').write_text(
            json.dumps({'task_stem': task})
        )
        self.on._teardown_worktrees_for_task(task_id=task, repo_coords=REPO)
        self.assertEqual(self.removed, [])

    def test_unconfigured_repo_skips(self):
        task = 'teardown-norepo-1'
        self._make_worktrees(task)
        self.on._teardown_worktrees_for_task(
            task_id=task, repo_coords='Larry-Yatch/some-unknown-repo',
        )
        self.assertEqual(self.removed, [])

    def test_absent_worktrees_is_noop(self):
        # No directories created and registration mocked False → nothing to do.
        self.on._teardown_worktrees_for_task(
            task_id='teardown-absent-1', repo_coords=REPO,
        )
        self.assertEqual(self.removed, [])

    def test_empty_task_id_is_noop(self):
        self.on._teardown_worktrees_for_task(task_id='', repo_coords=REPO)
        self.assertEqual(self.removed, [])


class TeardownCallSiteTest(TeardownTestBase):
    """Teardown fires from _attempt_auto_merge_with_gates on merge success."""

    def setUp(self):
        super().setUp()
        on = self.on
        self._orig_skip = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        self._orig_fn = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = True

    def tearDown(self):
        self.on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._orig_skip
        self.on._AUTO_MERGE_FN_OVERRIDE = self._orig_fn
        super().tearDown()

    def _attempt(self, task: str, outcome: str) -> dict:
        self.on._AUTO_MERGE_FN_OVERRIDE = lambda pr_url, task_id: {
            'merge_outcome': outcome,
            'merge_reason': 'test',
            'pr_number': 1,
            'repo_coords': REPO,
        }
        return self.on._attempt_auto_merge_with_gates(
            pr_url=f'https://github.com/{REPO}/pull/1',
            repo_coords=REPO,
            pr_number=1,
            task_id=task,
            summary='s',
            chat_id=None,
            changed_files=[],
        )

    def test_merged_outcome_triggers_teardown(self):
        task = 'teardown-cs-merged'
        forge_wt, mirror_wt = self._make_worktrees(task)
        result = self._attempt(task, 'merged')
        self.assertEqual(result['merge_outcome'], 'merged')
        self.assertEqual(sorted(self.removed), sorted([forge_wt, mirror_wt]))

    def test_failed_outcome_triggers_no_teardown(self):
        task = 'teardown-cs-failed'
        self._make_worktrees(task)
        result = self._attempt(task, 'failed')
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertEqual(self.removed, [])


if __name__ == '__main__':
    unittest.main()
