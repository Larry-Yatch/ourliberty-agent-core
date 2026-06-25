#!/usr/bin/env python3
"""Execution-time half of the merged/closed-PR Mirror-review guard.

Background: on task `forge-wip-only-auto-redispatch-001` (PR #693) a Mirror
review was dispatched ~6s BEFORE the PR auto-merged, then the expensive Opus
session ran ~12 min entirely AFTER the merge — $0.918 reviewing a PR that gates
nothing. A guard checked only at dispatch time can't catch that race, so
`inbox_watcher.process_task` re-checks the PR's state immediately before the
review session launches and no-ops when the PR has already left OPEN.

Covers (success criteria 1, 2, 4):
  1. A `phase=review` task for a MERGED PR is archived as a $0 no-op and
     run_claude is NEVER called.
  2. Same for a CLOSED PR.
  3. An OPEN PR proceeds — run_claude IS called (the guard never blocks a
     live review; criterion 3's spirit at the execution layer).
  4. Fail-OPEN: an UNKNOWN state, a gh hiccup (gh_json -> None), a missing
     pr_url, and an unparseable pr_url all proceed with the review.
  5. The guard is scoped: a non-review mirror task and a non-mirror review
     task are never short-circuited.

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_inbox_watcher_mirror_review_terminal_guard
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

_PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/693'


class MirrorReviewTerminalGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="review-terminal-guard-"))
        self.inboxes = self.tmp / "inboxes"
        self.outboxes = self.tmp / "outboxes"
        self.blackboard = self.tmp / "blackboard"
        self.costs = self.blackboard / "costs.jsonl"
        for agent in ("beacon", "forge", "mirror"):
            (self.inboxes / agent / ".archive").mkdir(parents=True, exist_ok=True)
            (self.inboxes / agent / ".invalid").mkdir(parents=True, exist_ok=True)
            (self.outboxes / agent).mkdir(parents=True, exist_ok=True)
        self.blackboard.mkdir(parents=True, exist_ok=True)

        # run_claude is the expensive session — a MagicMock so we can both stub
        # the OPEN "proceeds" path and assert call/no-call.
        self.run_claude = mock.MagicMock(return_value=(True, "ok", "sess-1"))

        self._patches = [
            mock.patch.object(inbox_watcher, "INBOXES_ROOT", self.inboxes),
            mock.patch.object(inbox_watcher, "OUTBOXES_ROOT", self.outboxes),
            mock.patch.object(inbox_watcher, "BLACKBOARD", self.blackboard),
            mock.patch.object(inbox_watcher, "COSTS_FILE", self.costs),
            mock.patch.object(inbox_watcher, "LOG_FILE", self.tmp / "watcher.log"),
            mock.patch.object(inbox_watcher.agent_runner, "run_claude",
                              self.run_claude),
            # Let the task reach the guard: pass every upstream gate.
            mock.patch.object(inbox_watcher.dispatch_validator, "validate_task",
                              return_value=(True, "")),
            mock.patch.object(inbox_watcher.routing_validator,
                              "check_hard_topology", return_value=(True, "")),
            mock.patch.object(inbox_watcher.routing_validator,
                              "check_target_repo", return_value=(True, "")),
            mock.patch.object(inbox_watcher, "_rotation_gate_block_reason",
                              return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------------

    def _write_review(self, name="review-wip-redispatch-001",
                      pr_url=_PR_URL, phase="review", agent="mirror",
                      task_id="wip-redispatch-001"):
        task = {
            "task_id": task_id,
            "prompt": "Review phase. ...",
            "source": "beacon",
            "phase": phase,
            "target_repo": "ourliberty-agent-core",
            "branch": "forge/wip-redispatch-001",
            "pr_url": pr_url,
        }
        f = self.inboxes / agent / f"{name}.json"
        f.write_text(json.dumps(task))
        return f

    def _run(self, task_file, agent="mirror", gh_state=None, gh_raw=mock.sentinel.unset):
        """gh_raw overrides the whole gh_json return when set; otherwise a
        {'state': gh_state} dict (or None when gh_state is None)."""
        if gh_raw is not mock.sentinel.unset:
            ret = gh_raw
        else:
            ret = {"state": gh_state} if gh_state is not None else None
        with mock.patch.object(inbox_watcher.task_terminal_state, "gh_json",
                               return_value=ret) as gh:
            inbox_watcher.process_task(agent, task_file, models_config={})
        return gh

    def _archived(self, agent="mirror"):
        return list((self.inboxes / agent / ".archive").glob("*.json"))

    def _last_cost_row(self):
        lines = [l for l in self.costs.read_text().splitlines() if l.strip()]
        self.assertTrue(lines, "expected a cost row")
        return json.loads(lines[-1])

    # -- terminal: skip -------------------------------------------------------

    def test_merged_pr_review_is_skipped(self):
        tf = self._write_review()
        self._run(tf, gh_state="MERGED")
        self.run_claude.assert_not_called()
        self.assertFalse(tf.exists())
        self.assertEqual(len(self._archived()), 1)
        row = self._last_cost_row()
        self.assertEqual(row["task_type"], "review-skipped-terminal")
        self.assertEqual(row["cost_usd"], 0.0)
        # No outbox written for a no-op skip.
        self.assertEqual(list((self.outboxes / "mirror").glob("*.json")), [])

    def test_closed_pr_review_is_skipped(self):
        tf = self._write_review()
        self._run(tf, gh_state="CLOSED")
        self.run_claude.assert_not_called()
        self.assertEqual(self._last_cost_row()["task_type"],
                         "review-skipped-terminal")

    # -- non-terminal / uncertain: proceed (fail open) ------------------------

    def test_open_pr_review_proceeds(self):
        tf = self._write_review()
        self._run(tf, gh_state="OPEN")
        self.run_claude.assert_called_once()

    def test_unknown_state_proceeds(self):
        tf = self._write_review()
        self._run(tf, gh_state="SOMETHING-WEIRD")
        self.run_claude.assert_called_once()

    def test_gh_hiccup_returns_none_proceeds(self):
        # gh_json -> None is the UNKNOWN-on-error kernel signal; fail OPEN.
        tf = self._write_review()
        self._run(tf, gh_raw=None)
        self.run_claude.assert_called_once()

    def test_missing_pr_url_proceeds(self):
        tf = self._write_review(pr_url=None)
        # gh must never even be consulted without a url.
        gh = self._run(tf, gh_state="MERGED")
        gh.assert_not_called()
        self.run_claude.assert_called_once()

    def test_unparseable_pr_url_proceeds(self):
        tf = self._write_review(pr_url="not-a-github-url")
        gh = self._run(tf, gh_state="MERGED")
        gh.assert_not_called()
        self.run_claude.assert_called_once()

    # -- scope ----------------------------------------------------------------

    def test_non_review_mirror_task_not_guarded(self):
        # A mirror task that isn't phase=review must not be probed/short-circuited.
        tf = self._write_review(phase="preflight")
        gh = self._run(tf, gh_state="MERGED")
        gh.assert_not_called()
        self.run_claude.assert_called_once()

    def test_non_mirror_review_task_not_guarded(self):
        # Only Mirror reviews are guarded; a forge review-phase task (shouldn't
        # exist, but proves scoping) is never probed.
        tf = self._write_review(agent="forge")
        gh = self._run(tf, agent="forge", gh_state="MERGED")
        gh.assert_not_called()
        self.run_claude.assert_called_once()


if __name__ == "__main__":
    unittest.main()
