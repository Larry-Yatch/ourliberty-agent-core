#!/usr/bin/env python3
"""Mirror reviews must not push checkpoints to the PR branch under review.

Background (PR #841, 2026-07-08): a Mirror review dispatch ran
`setup_branch_checkpoint` on the PR branch (`work/operator-timers`) and
pushed an empty `[WIP][session-start]` commit — moving the PR head SHA.
The outbox-notifier's reconcile sweep then saw an "unreviewed" head and
dispatched a duplicate round-0 review; head-SHA review dedup can't catch
it because the SHA genuinely differs. The WIP commit also lands in the
PR's merge history and receives the mirror-review commit status.

Fix: `inbox_watcher.process_task` passes `push_checkpoint=False` to
`worktree_manager.ensure_worktree_for_task` for `agent=mirror` +
`phase=review` dispatches (read-only checkout), and `True` for everything
else (Forge builds rely on the checkpoint for crash-recoverable branches).

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_inbox_watcher_mirror_readonly_checkout
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher  # noqa: E402

_PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/841'

_CLEAN_REVIEW_MARKER = (
    "Reviewed the PR — looks good.\n\n"
    "=== REVIEW_PASS ===\n"
    '{\n'
    f'  "pr_url": "{_PR_URL}",\n'
    '  "summary": "ok",\n'
    '  "task_id": "pr-841"\n'
    '}\n'
    "=== END_REVIEW_PASS ===\n"
)


class MirrorReadonlyCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mirror-ro-checkout-"))
        self.inboxes = self.tmp / "inboxes"
        self.outboxes = self.tmp / "outboxes"
        self.blackboard = self.tmp / "blackboard"
        self.costs = self.blackboard / "costs.jsonl"
        for agent in ("beacon", "forge", "mirror"):
            (self.inboxes / agent / ".archive").mkdir(parents=True, exist_ok=True)
            (self.inboxes / agent / ".invalid").mkdir(parents=True, exist_ok=True)
            (self.outboxes / agent).mkdir(parents=True, exist_ok=True)
        self.blackboard.mkdir(parents=True, exist_ok=True)
        self.canonical = self.tmp / "canonical"
        self.canonical.mkdir()
        self.worktree = self.tmp / "wt"
        self.worktree.mkdir()

        self.run_claude = mock.MagicMock(
            return_value=(True, _CLEAN_REVIEW_MARKER, "sess-1"))
        self.ensure_worktree = mock.MagicMock(
            return_value=(self.worktree, "some-branch"))

        self._patches = [
            mock.patch.object(inbox_watcher, "INBOXES_ROOT", self.inboxes),
            mock.patch.object(inbox_watcher, "OUTBOXES_ROOT", self.outboxes),
            mock.patch.object(inbox_watcher, "BLACKBOARD", self.blackboard),
            mock.patch.object(inbox_watcher, "COSTS_FILE", self.costs),
            mock.patch.object(inbox_watcher, "LOG_FILE", self.tmp / "watcher.log"),
            mock.patch.object(inbox_watcher.agent_runner, "run_claude",
                              self.run_claude),
            mock.patch.object(inbox_watcher.worktree_manager,
                              "ensure_worktree_for_task",
                              self.ensure_worktree),
            mock.patch.object(inbox_watcher, "_load_repo_paths",
                              return_value={
                                  "ourliberty-agent-core": self.canonical}),
            # Let the task reach the worktree block: pass every upstream gate.
            mock.patch.object(inbox_watcher.dispatch_validator, "validate_task",
                              return_value=(True, "")),
            mock.patch.object(inbox_watcher.routing_validator,
                              "check_hard_topology", return_value=(True, "")),
            mock.patch.object(inbox_watcher.routing_validator,
                              "check_target_repo", return_value=(True, "")),
            mock.patch.object(inbox_watcher, "_rotation_gate_block_reason",
                              return_value=None),
            # Merged/closed-PR guard: PR is OPEN, review proceeds.
            mock.patch.object(inbox_watcher.task_terminal_state, "gh_json",
                              return_value={"state": "OPEN"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_task(self, agent: str, phase: str, task_id: str,
                    branch: str) -> Path:
        task = {
            "task_id": task_id,
            "prompt": "do the thing",
            "source": "beacon",
            "phase": phase,
            "target_repo": "ourliberty-agent-core",
            "branch": branch,
            "pr_url": _PR_URL,
        }
        f = self.inboxes / agent / f"{phase}-{task_id}.json"
        f.write_text(json.dumps(task))
        return f

    def _models_config(self, agent: str) -> dict:
        return {"agents": {agent: {"worktree_enabled": True}}}

    def test_mirror_review_uses_readonly_checkout(self):
        tf = self._write_task("mirror", "review", "pr-841",
                              "work/operator-timers")
        inbox_watcher.process_task("mirror", tf,
                                   models_config=self._models_config("mirror"))
        self.ensure_worktree.assert_called_once()
        kwargs = self.ensure_worktree.call_args.kwargs
        self.assertEqual(kwargs.get("push_checkpoint"), False,
                         "mirror review must NOT push a checkpoint to the "
                         "PR branch under review")
        self.run_claude.assert_called_once()

    def test_forge_build_keeps_checkpoint_push(self):
        tf = self._write_task("forge", "build", "task-b1", "forge/task-b1")
        inbox_watcher.process_task("forge", tf,
                                   models_config=self._models_config("forge"))
        self.ensure_worktree.assert_called_once()
        kwargs = self.ensure_worktree.call_args.kwargs
        self.assertEqual(kwargs.get("push_checkpoint"), True,
                         "forge builds rely on the crash-recovery checkpoint")

    def test_mirror_non_review_keeps_checkpoint_push(self):
        # Mirror occasionally runs build-style tasks on its own branch;
        # only phase=review is read-only.
        tf = self._write_task("mirror", "build", "task-m1", "mirror/task-m1")
        inbox_watcher.process_task("mirror", tf,
                                   models_config=self._models_config("mirror"))
        self.ensure_worktree.assert_called_once()
        kwargs = self.ensure_worktree.call_args.kwargs
        self.assertEqual(kwargs.get("push_checkpoint"), True)


if __name__ == "__main__":
    unittest.main()
