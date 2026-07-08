#!/usr/bin/env python3
"""Mirror reviews must not push checkpoints to the PR branch under review.

Background (PR #841, 2026-07-08): a Mirror review dispatch ran
`setup_branch_checkpoint` on the PR branch (`work/operator-timers`) and
pushed an empty `[WIP][session-start]` commit — moving the PR head SHA.
The outbox-notifier's reconcile sweep then saw an "unreviewed" head and
dispatched a duplicate round-0 review; head-SHA review dedup can't catch
it because the SHA genuinely differs. The WIP commit also lands in the
PR's merge history and receives the mirror-review commit status.

Fix: `inbox_watcher.process_task` passes `readonly=True` to
`worktree_manager.ensure_worktree_for_task` for mirror dispatches that
name someone else's branch in the envelope and look review-shaped
(phase=review OR a pr_url present — healers can rewrite phase on
re-dispatch, e.g. heal_resume_paused_on_tier1 forces 'preflight').
Builder dispatches and mirror tasks on their own derived branch keep the
checkpoint push (crash-recoverable branches). A failed readonly checkout
(branch gone from origin / fetch failure) requeues the task instead of
reviewing the wrong tree.

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

    def _write_task(self, agent: str, phase, task_id: str,
                    branch, pr_url=_PR_URL) -> Path:
        task = {
            "task_id": task_id,
            "prompt": "do the thing",
            "source": "beacon",
            "target_repo": "ourliberty-agent-core",
        }
        if phase is not None:
            task["phase"] = phase
        if branch is not None:
            task["branch"] = branch
        if pr_url is not None:
            task["pr_url"] = pr_url
        f = self.inboxes / agent / f"{phase or 'task'}-{task_id}.json"
        f.write_text(json.dumps(task))
        return f

    def _models_config(self, agent: str) -> dict:
        return {"agents": {agent: {"worktree_enabled": True}}}

    def _readonly_arg(self):
        self.ensure_worktree.assert_called_once()
        return self.ensure_worktree.call_args.kwargs.get("readonly")

    def test_mirror_review_uses_readonly_checkout(self):
        tf = self._write_task("mirror", "review", "pr-841",
                              "work/operator-timers")
        inbox_watcher.process_task("mirror", tf,
                                   models_config=self._models_config("mirror"))
        self.assertIs(self._readonly_arg(), True,
                      "mirror review must NOT push to the PR branch "
                      "under review")
        self.run_claude.assert_called_once()

    def test_mirror_phaseless_pr_envelope_is_readonly(self):
        # Healers can rewrite phase on re-dispatch (heal_resume_paused_on_
        # tier1 forces 'preflight') and ad-hoc review envelopes may omit
        # it; a pr_url + someone else's branch is review-shaped enough.
        tf = self._write_task("mirror", "preflight", "pr-841",
                              "work/operator-timers")
        inbox_watcher.process_task("mirror", tf,
                                   models_config=self._models_config("mirror"))
        self.assertIs(self._readonly_arg(), True)

    def test_forge_build_keeps_checkpoint_push(self):
        tf = self._write_task("forge", "build", "task-b1", "forge/task-b1")
        inbox_watcher.process_task("forge", tf,
                                   models_config=self._models_config("forge"))
        self.assertIs(self._readonly_arg(), False,
                      "forge builds rely on the crash-recovery checkpoint")

    def test_mirror_own_branch_keeps_checkpoint_push(self):
        # No envelope branch: the derived mirror/<task_id> branch is
        # mirror-owned scratch space — heal_forge_wip_only_redispatch's
        # WIP-on-origin signal relies on the checkpoint existing there.
        tf = self._write_task("mirror", "review", "audit-1", branch=None)
        inbox_watcher.process_task("mirror", tf,
                                   models_config=self._models_config("mirror"))
        self.assertIs(self._readonly_arg(), False)

    def test_mirror_build_task_keeps_checkpoint_push(self):
        # Mirror build-style task on an explicit feature branch with no
        # pr_url and a non-review phase: not review-shaped, stays writable.
        tf = self._write_task("mirror", "build", "task-m1",
                              "mirror/task-m1", pr_url=None)
        inbox_watcher.process_task("mirror", tf,
                                   models_config=self._models_config("mirror"))
        self.assertIs(self._readonly_arg(), False)

    def test_mirror_review_failed_checkout_requeues(self):
        # Readonly checkout failure (branch gone from origin, fetch error):
        # the review must NOT run against whatever tree the worktree holds.
        # The task stays in the inbox with requeue_count bumped.
        self.ensure_worktree.return_value = (self.worktree, None)
        tf = self._write_task("mirror", "review", "pr-841",
                              "work/operator-timers")
        inbox_watcher.process_task("mirror", tf,
                                   models_config=self._models_config("mirror"))
        self.run_claude.assert_not_called()
        self.assertTrue(tf.exists(), "task must stay queued for retry")
        self.assertEqual(json.loads(tf.read_text()).get("requeue_count"), 1)

    def test_forge_none_branch_still_proceeds(self):
        # Push-failure tolerance for builders is unchanged: Forge retries
        # the push during her build phase.
        self.ensure_worktree.return_value = (self.worktree, None)
        tf = self._write_task("forge", "build", "task-b2", "forge/task-b2")
        inbox_watcher.process_task("forge", tf,
                                   models_config=self._models_config("forge"))
        self.run_claude.assert_called_once()


if __name__ == "__main__":
    unittest.main()
