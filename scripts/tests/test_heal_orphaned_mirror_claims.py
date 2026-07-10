"""Tests for scripts/heal_orphaned_mirror_claims.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- conclusion signals: outbox-archive verdict (incl. collision suffix + forfeit
  exclusion + prefix-collision rejection), inbox same-name archive, notifier-log
  grep, gh PR-terminal (merged/closed/open/error)
- liveness guards: live worktree process (exact / (deleted) / subdir), live vs
  dead in-flight entry
- scan_agent decision matrix: concluded+stranded → archive; the LOAD-BEARING
  live-process / live-in-flight / fresh / not-concluded → spare
- kill-switch honored by main()
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

if not os.environ.get("OURLIBERTY_AGENTS_ROOT"):
    _SANDBOX_ROOT = tempfile.mkdtemp(prefix="ol-test-agents-root-")
    os.makedirs(os.path.join(_SANDBOX_ROOT, "logs"), exist_ok=True)
    os.environ["OURLIBERTY_AGENTS_ROOT"] = _SANDBOX_ROOT

import heal_orphaned_mirror_claims as h  # noqa: E402
import mirror_review_conclusion as mrc  # noqa: E402
import worktree_manager  # noqa: E402


def _old_ts() -> float:
    return datetime.now(timezone.utc).timestamp() - 10_000


class ScanFixture(unittest.TestCase):
    """Per-test sandbox: fresh tmp tree with the module path globals repointed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ol-orphan-claims-"))
        self._saved = {
            k: getattr(h, k) for k in (
                "AGENTS_ROOT", "INBOXES_ROOT", "OUTBOXES_ROOT", "IN_FLIGHT_DIR",
                "NOTIFIER_LOG", "KILL_SWITCH", "LOG_FILE", "HEARTBEAT_FILE",
                "ORPHAN_CLAIM_GRACE_SEC",
            )
        }
        h.AGENTS_ROOT = self.root
        h.INBOXES_ROOT = self.root / "inboxes"
        h.OUTBOXES_ROOT = self.root / "outboxes"
        h.IN_FLIGHT_DIR = self.root / "state" / "in-flight"
        h.NOTIFIER_LOG = self.root / "logs" / "outbox-notifier.log"
        h.KILL_SWITCH = self.root / "healers.disabled"
        h.LOG_FILE = self.root / "logs" / "heal.log"
        h.HEARTBEAT_FILE = self.root / "blackboard" / "hb"
        h.ORPHAN_CLAIM_GRACE_SEC = 3600
        for d in (h.INBOXES_ROOT / "mirror" / ".claimed" / "0",
                  h.INBOXES_ROOT / "mirror" / ".archive",
                  h.OUTBOXES_ROOT / "mirror" / ".archive",
                  h.IN_FLIGHT_DIR, h.NOTIFIER_LOG.parent):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(h, k, v)

    # -- fixture helpers --
    def _claim(self, task_id, *, slot=0, pr_url=None, old=True, name=None):
        name = name or f"review-{task_id}.json"
        d = h.INBOXES_ROOT / "mirror" / ".claimed" / str(slot)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        body = {"task_id": task_id}
        if pr_url:
            body["pr_url"] = pr_url
        p.write_text(json.dumps(body))
        if old:
            os.utime(p, (_old_ts(), _old_ts()))
        return p

    def _outbox_verdict(self, task_id, suffix=".json"):
        (h.OUTBOXES_ROOT / "mirror" / ".archive" / f"{task_id}{suffix}").write_text("{}")


class ConclusionSignalTests(ScanFixture):
    """The conclusion predicate lives in the shared mirror_review_conclusion
    module (single source of truth with inbox_watcher.sweep_claimed_orphans)."""

    def _has_verdict(self, task_id):
        return mrc.outbox_archive_has_verdict(h.OUTBOXES_ROOT, "mirror", task_id)

    def test_outbox_archive_verdict_plain(self):
        self._outbox_verdict("t1")
        self.assertTrue(self._has_verdict("t1"))

    def test_outbox_archive_verdict_collision_suffix(self):
        self._outbox_verdict("t1", ".2.json")
        self.assertTrue(self._has_verdict("t1"))

    def test_outbox_forfeit_is_not_a_verdict(self):
        self._outbox_verdict("t1", ".forfeit.json")
        self.assertFalse(self._has_verdict("t1"))

    def test_outbox_prefix_collision_not_matched(self):
        # A DIFFERENT task whose id has t1 as a prefix must not read as t1's verdict.
        self._outbox_verdict("t1bar")
        self.assertFalse(self._has_verdict("t1"))

    def test_glob_metachar_task_id_matches_literally(self):
        # A task_id carrying a glob metachar must match by literal string ops,
        # not be interpreted as a glob pattern (old glob(f"{task_id}.*.json")
        # treated `[1]` as a character class and missed the real file).
        self._outbox_verdict("batch[1]", ".3.json")
        self.assertTrue(self._has_verdict("batch[1]"))
        # And a metachar id must not spuriously match an unrelated file.
        self.assertFalse(self._has_verdict("batch[2]"))

    def test_inbox_same_name_archive(self):
        (h.INBOXES_ROOT / "mirror" / ".archive" / "review-t1.json").write_text("{}")
        self.assertTrue(mrc.inbox_archive_has_same_review(
            h.INBOXES_ROOT, "mirror", "review-t1.json"))
        self.assertFalse(mrc.inbox_archive_has_same_review(
            h.INBOXES_ROOT, "mirror", "review-t2.json"))

    def test_notifier_log_boundary_match(self):
        h.NOTIFIER_LOG.write_text(
            "[x] noise\n"
            "[y] marker-notified beacon <- mirror (mirror-result, "
            "file=notify-task-alpha.json)\n")
        self.assertTrue(mrc.notifier_log_marked_delivered(h.NOTIFIER_LOG, "task-alpha"))
        self.assertFalse(mrc.notifier_log_marked_delivered(h.NOTIFIER_LOG, "task-beta"))

    def test_notifier_log_substring_id_not_matched(self):
        # 'task-alpha' must NOT match a delivery line for 'task-alpha-2' (the
        # substring-ID anti-pattern id_match.id_matches defeats).
        h.NOTIFIER_LOG.write_text(
            "[y] marker-notified beacon <- mirror (file=notify-task-alpha-2.json)\n")
        self.assertFalse(mrc.notifier_log_marked_delivered(h.NOTIFIER_LOG, "task-alpha"))
        self.assertTrue(mrc.notifier_log_marked_delivered(h.NOTIFIER_LOG, "task-alpha-2"))

    def test_pr_terminal_merged(self):
        with mock.patch.object(h.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"state":"MERGED"}', stderr="")
            self.assertTrue(h.pr_terminal("https://x/pull/1"))

    def test_pr_terminal_closed(self):
        with mock.patch.object(h.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"state":"CLOSED"}', stderr="")
            self.assertTrue(h.pr_terminal("https://x/pull/1"))

    def test_pr_open_is_not_terminal(self):
        with mock.patch.object(h.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"state":"OPEN"}', stderr="")
            self.assertFalse(h.pr_terminal("https://x/pull/1"))

    def test_pr_terminal_gh_error_fails_safe(self):
        with mock.patch.object(h.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
            with mock.patch.object(h, "log"):
                self.assertFalse(h.pr_terminal("https://x/pull/1"))

    def test_empty_pr_url_not_terminal(self):
        self.assertFalse(h.pr_terminal(""))


class LivenessGuardTests(ScanFixture):
    def test_live_worktree_process_exact(self):
        wt = str(worktree_manager.worktree_path_for("mirror", "t1"))
        self.assertTrue(h.live_worktree_process("mirror", "t1", {wt}))

    def test_live_worktree_process_subdir(self):
        wt = str(worktree_manager.worktree_path_for("mirror", "t1"))
        self.assertTrue(h.live_worktree_process("mirror", "t1", {wt + "/scripts"}))

    def test_no_live_process_for_other_task(self):
        wt = str(worktree_manager.worktree_path_for("mirror", "other"))
        self.assertFalse(h.live_worktree_process("mirror", "t1", {wt}))

    def test_deleted_suffix_normalized_in_scan_collector(self):
        # get_active_claude_cwds strips the kernel's " (deleted)" marker so a
        # torn-down-but-alive worktree still matches its path.
        wt = str(worktree_manager.worktree_path_for("mirror", "t1"))
        with mock.patch.object(h.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="123\n")
            with mock.patch.object(h.os, "readlink", return_value=wt + " (deleted)"):
                self.assertIn(wt, h.get_active_claude_cwds())

    def test_in_flight_live_pid_protects(self):
        (h.IN_FLIGHT_DIR / "t1.json").write_text(json.dumps({"pid": os.getpid()}))
        self.assertTrue(h.has_live_in_flight("t1"))

    def test_in_flight_bad_pid_does_not_protect(self):
        (h.IN_FLIGHT_DIR / "t1.json").write_text(json.dumps({"pid": -1}))
        self.assertFalse(h.has_live_in_flight("t1"))

    def test_in_flight_missing_does_not_protect(self):
        self.assertFalse(h.has_live_in_flight("nope"))


class ScanDecisionTests(ScanFixture):
    def test_concluded_stranded_orphan_is_archived(self):
        claim = self._claim("t1")
        self._outbox_verdict("t1")  # verdict delivered
        with mock.patch.object(h, "log"):
            scanned, cleared, spared = h.scan_agent("mirror", set())
        self.assertEqual((scanned, cleared, spared), (1, 1, 0))
        self.assertFalse(claim.exists())
        archived = list((h.INBOXES_ROOT / "mirror" / ".archive").glob(
            "review-t1.orphan-cleared-*.json"))
        self.assertEqual(len(archived), 1)

    def test_pr_terminal_orphan_is_archived(self):
        claim = self._claim("t1", pr_url="https://x/pull/854")
        with mock.patch.object(h, "pr_terminal", return_value=True), \
                mock.patch.object(h, "log"):
            _, cleared, _ = h.scan_agent("mirror", set())
        self.assertEqual(cleared, 1)
        self.assertFalse(claim.exists())

    def test_live_worktree_process_spares_even_when_concluded(self):
        # LOAD-BEARING: a NEW review round can run while a prior round's verdict
        # is already delivered — the live process must win.
        claim = self._claim("t1")
        self._outbox_verdict("t1")
        wt = str(worktree_manager.worktree_path_for("mirror", "t1"))
        with mock.patch.object(h, "log"):
            _, cleared, spared = h.scan_agent("mirror", {wt})
        self.assertEqual((cleared, spared), (0, 1))
        self.assertTrue(claim.exists())

    def test_live_in_flight_spares_even_when_concluded(self):
        claim = self._claim("t1")
        self._outbox_verdict("t1")
        (h.IN_FLIGHT_DIR / "t1.json").write_text(json.dumps({"pid": os.getpid()}))
        with mock.patch.object(h, "log"):
            _, cleared, spared = h.scan_agent("mirror", set())
        self.assertEqual((cleared, spared), (0, 1))
        self.assertTrue(claim.exists())

    def test_fresh_claim_is_spared(self):
        claim = self._claim("t1", old=False)
        self._outbox_verdict("t1")
        with mock.patch.object(h, "log"):
            _, cleared, spared = h.scan_agent("mirror", set())
        self.assertEqual((cleared, spared), (0, 1))
        self.assertTrue(claim.exists())

    def test_not_concluded_is_spared_for_requeue(self):
        # No verdict, PR still open (gh not terminal): the review may never have
        # run — leave it for sweep_claimed_orphans to re-queue, never archive.
        claim = self._claim("t1", pr_url="https://x/pull/854")
        with mock.patch.object(h, "pr_terminal", return_value=False), \
                mock.patch.object(h, "log"):
            _, cleared, spared = h.scan_agent("mirror", set())
        self.assertEqual((cleared, spared), (0, 1))
        self.assertTrue(claim.exists())

    def test_slot_dispatch_lease_held_spares_whole_slot(self):
        # The watcher is mid-dispatch on this slot (holds its lease): even a
        # concluded-looking, old, process-less claim must be spared — the claim
        # may be spawning (mtime predates the claim; no process/in-flight yet).
        claim = self._claim("t1", slot=1)
        self._outbox_verdict("t1")
        with mock.patch.object(h, "slot_dispatch_active", return_value=True), \
                mock.patch.object(h, "log"):
            scanned, cleared, spared = h.scan_agent("mirror", set())
        self.assertEqual((cleared, spared), (0, 1))
        self.assertTrue(claim.exists())

    def test_non_review_claim_ignored(self):
        # Only review-*.json is in scope.
        d = h.INBOXES_ROOT / "mirror" / ".claimed" / "0"
        p = d / "build-t1.json"
        p.write_text(json.dumps({"task_id": "t1"}))
        os.utime(p, (_old_ts(), _old_ts()))
        with mock.patch.object(h, "log"):
            scanned, cleared, _ = h.scan_agent("mirror", set())
        self.assertEqual((scanned, cleared), (0, 0))
        self.assertTrue(p.exists())


class MainTests(ScanFixture):
    def test_kill_switch_blocks_main(self):
        h.KILL_SWITCH.write_text("halt")
        self._claim("t1")
        self._outbox_verdict("t1")
        with mock.patch.object(h, "scan_agent") as scan, mock.patch.object(h, "log"):
            rc = h.main()
        self.assertEqual(rc, 0)
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
