#!/usr/bin/env python3
"""Tests for the PR-A path-traversal sanitizer (2026-06-05 audit #10/#11/#39).

Covers:
  - safe_write_inbox.sanitize_component — the shared single-component sanitizer.
  - safe_write_inbox end-to-end: a traversal task_id/filename cannot escape the
    target inbox directory (closes #10 beacon + #11 + every dispatch caller).
  - inbox_watcher outbox writes route through the same sanitizer (#39).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_path_traversal_sanitizer
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher as iw          # noqa: E402
import routing_validator as rv      # noqa: E402
import safe_write_inbox as swi      # noqa: E402


class SanitizeComponentTest(unittest.TestCase):
    def test_legit_task_id_passes_through_unchanged(self):
        # The common case: real task ids are already [A-Za-z0-9._-].
        for name in ("forge-supply-chain-3-20260605T090000Z.json",
                     "test-task-001.json", "build-abc_def.json"):
            self.assertEqual(swi.sanitize_component(name), name)

    def test_path_separators_are_neutralized(self):
        for hostile in ("../../../../etc/passwd.json",
                        "/etc/shadow", "a/b/c.json", "x\\y\\z.json"):
            out = swi.sanitize_component(hostile)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)

    def test_dot_only_components_fall_back(self):
        # '.'/'..' name a directory entry; they must not survive as the filename.
        for hostile in (".", "..", "...", "...."):
            self.assertEqual(swi.sanitize_component(hostile), "task")

    def test_separator_only_is_safe_but_not_traversal(self):
        # '/' -> '-' is acceptable: it can't traverse, it just isn't dot-only.
        self.assertEqual(swi.sanitize_component("/"), "-")
        self.assertNotIn("/", swi.sanitize_component("//"))

    def test_empty_or_non_str_falls_back(self):
        self.assertEqual(swi.sanitize_component(""), "task")
        self.assertEqual(swi.sanitize_component(None), "task")  # type: ignore[arg-type]
        self.assertEqual(swi.sanitize_component("x", fallback="z"), "x")
        self.assertEqual(swi.sanitize_component("", fallback="z"), "z")

    def test_nul_and_control_bytes_neutralized(self):
        out = swi.sanitize_component("a\x00b\nc.json")
        self.assertNotIn("\x00", out)
        self.assertNotIn("\n", out)

    def test_filesystem_safe_punctuation_is_preserved(self):
        # Critical for avoiding writer/reader divergence: real task ids carry
        # ':' / '@' (e.g. medic-silence-cpu:high@web-01). Those are NOT path-
        # structural, so the on-disk name must still equal f'{task_id}.json' or
        # the idempotency readers (outbox_notifier / heal_pipeline_stall) that
        # rebuild the raw name would miss it and re-dispatch.
        for name in ("medic-silence-cpu:high@web-01.json",
                     "alert#294.json", "a task with spaces.json"):
            self.assertEqual(swi.sanitize_component(name), name)


class SafeWriteInboxTraversalTest(unittest.TestCase):
    """End-to-end: a hostile filename must land inside the target inbox."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._orig = (swi.AGENTS_ROOT, swi.INBOXES_ROOT, swi.ROUTING_EVENTS_LOG)
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / "inboxes"
        swi.ROUTING_EVENTS_LOG = self._root / "logs" / "routing-events.jsonl"
        self._orig_rv = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / "repo"
        rv.invalidate_cache()

    def tearDown(self):
        swi.AGENTS_ROOT, swi.INBOXES_ROOT, swi.ROUTING_EVENTS_LOG = self._orig
        rv.REPO_ROOT = self._orig_rv
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _task(self):
        return {
            "task_id": "t",
            "prompt": "x" * 150,  # clears MIN_PROMPT_LEN
            "source": "beacon",
        }

    def test_traversal_filename_stays_in_target_inbox(self):
        dest = swi.safe_write_inbox(
            "forge", self._task(), "beacon", "../../../../etc/passwd.json"
        )
        target_dir = (swi.INBOXES_ROOT / "forge").resolve()
        # Written inside inboxes/forge, not escaped up the tree.
        self.assertEqual(dest.parent.resolve(), target_dir)
        self.assertTrue(str(dest.resolve()).startswith(str(target_dir)))
        self.assertNotIn("/etc/", str(dest.resolve()))
        self.assertTrue(dest.exists())
        # Nothing got written outside the sandbox root.
        self.assertFalse((self._root.parent / "etc" / "passwd.json").exists())

    def test_legit_filename_unchanged(self):
        dest = swi.safe_write_inbox("forge", self._task(), "beacon", "task-001.json")
        self.assertEqual(dest.name, "task-001.json")


class InboxWatcherOutboxContainmentTest(unittest.TestCase):
    """#39/#6 — drive the real process_task so the outbox write actually goes
    through the sanitizer, with a traversal task_id in the envelope."""

    AGENT = "beacon"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig = {k: getattr(iw, k) for k in
                      ("INBOXES_ROOT", "OUTBOXES_ROOT", "BLACKBOARD", "COSTS_FILE")}
        iw.INBOXES_ROOT = self.root / "inboxes"
        iw.OUTBOXES_ROOT = self.root / "outboxes"
        iw.BLACKBOARD = self.root / "blackboard"
        iw.COSTS_FILE = iw.BLACKBOARD / "costs.jsonl"
        (iw.INBOXES_ROOT / self.AGENT / ".archive").mkdir(parents=True, exist_ok=True)
        (iw.OUTBOXES_ROOT / self.AGENT).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(iw, k, v)
        self.tmp.cleanup()

    def test_traversal_task_id_outbox_stays_in_outbox_tree(self):
        # On-disk inbox filename is safe; the ENVELOPE task_id is hostile.
        hostile = "../../../../etc/pwned"
        task_file = iw.INBOXES_ROOT / self.AGENT / "incoming.json"
        task_file.write_text(json.dumps(
            {"task_id": hostile, "prompt": "do the work", "source": self.AGENT}))
        models_config = {"agents": {self.AGENT: {"inbox_model": "claude-sonnet-4-6"}}}

        def fake_run_claude(**kwargs):
            kwargs["out_meta"]["cost_usd"] = 0.0
            kwargs["out_meta"]["completed_at"] = "2026-06-05T00:00:00Z"
            return (True, "agent output", "sess-1")

        with mock.patch.object(iw.agent_runner, "run_claude", side_effect=fake_run_claude), \
             mock.patch.object(iw, "dispatch_validator") as dv, \
             mock.patch.object(iw, "routing_validator") as rvm, \
             mock.patch.object(iw, "_rotation_gate_block_reason", return_value=None), \
             mock.patch.object(iw, "bump_requeue"):
            dv.validate_task.return_value = (True, "")
            rvm.check_hard_topology.return_value = (True, "")
            rvm.check_target_repo.return_value = (True, "")
            iw.process_task(self.AGENT, task_file, models_config)

        outbox_dir = (iw.OUTBOXES_ROOT / self.AGENT).resolve()
        written = list(outbox_dir.glob("*.json"))
        self.assertEqual(len(written), 1, f"expected one outbox file, got {written}")
        # Contained in the outbox dir, did not traverse up to /etc.
        self.assertEqual(written[0].parent.resolve(), outbox_dir)
        self.assertNotIn("/etc/", str(written[0].resolve()))
        self.assertFalse((self.root.parent / "etc" / "pwned.json").exists())


if __name__ == "__main__":
    unittest.main()
