#!/usr/bin/env python3
"""Tests for heal_empty_inbox_files (audit 2026-06-05 PR-L / finding #55).

The healer archives <50-byte / empty / trivial inbox *.json files. Finding #55:
with no age gate it raced non-atomic producers (inbox_watcher.bump_requeue's
in-place rewrite; cron signal-*.json writers) and could archive a real task
caught mid-write. The fix adds a MIN_FILE_AGE_SECONDS grace window: a file is
only judged once its mtime is at least that old.

These tests assert:
  - a just-written <50-byte invalid file inside the grace window is NOT archived
  - a just-written 0-byte file inside the grace window is NOT archived
  - the same files, aged past the grace window, ARE archived
  - the grace gate does not rescue genuinely-aged empty files (no behavior loss)

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_heal_empty_inbox_files
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_empty_inbox_files as heal  # noqa: E402


class GraceWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inboxes = self.root / "inboxes"
        self.agent_inbox = self.inboxes / "observer"
        self.agent_inbox.mkdir(parents=True, exist_ok=True)

        # Repoint the module's hardcoded /home/larry paths at the scratch root.
        self._saved = {
            "INBOXES_ROOT": heal.INBOXES_ROOT,
            "LOG_FILE": heal.LOG_FILE,
            "KILL_SWITCH": heal.KILL_SWITCH,
        }
        heal.INBOXES_ROOT = self.inboxes
        heal.LOG_FILE = self.root / "logs" / "heal.log"
        heal.KILL_SWITCH = self.root / "healers.disabled"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(heal, k, v)
        self.tmp.cleanup()

    def _write(self, name, content):
        p = self.agent_inbox / name
        p.write_text(content)
        return p

    def _age(self, path, seconds):
        """Backdate a file's mtime by ``seconds`` (push it past the grace)."""
        old = time.time() - seconds
        os.utime(path, (old, old))

    def _archive_dir(self):
        return self.agent_inbox / "blocked" / "empty-files"

    # ---- classify(): the grace gate -----------------------------------

    def test_fresh_invalid_small_file_is_not_archivable(self):
        p = self._write("signal-1.json", "{partial")  # <50B, invalid JSON
        is_empty, reason, _size = heal.classify(p)
        self.assertFalse(is_empty, f"fresh file should be skipped, got: {reason}")
        self.assertIn("too-fresh", reason)

    def test_fresh_zero_byte_file_is_not_archivable(self):
        # An in-progress write is exactly the 0-byte case; the grace must gate
        # BEFORE the zero-byte check.
        p = self._write("signal-2.json", "")
        is_empty, reason, _size = heal.classify(p)
        self.assertFalse(is_empty, f"fresh 0-byte file should be skipped, got: {reason}")
        self.assertIn("too-fresh", reason)

    def test_aged_invalid_small_file_is_archivable(self):
        p = self._write("signal-3.json", "{partial")
        self._age(p, heal.MIN_FILE_AGE_SECONDS + 5)
        is_empty, reason, _size = heal.classify(p)
        self.assertTrue(is_empty, f"aged invalid file should be archived, got: {reason}")
        self.assertEqual(reason, "invalid-json-small-file")

    def test_aged_zero_byte_file_is_archivable(self):
        p = self._write("signal-4.json", "")
        self._age(p, heal.MIN_FILE_AGE_SECONDS + 5)
        is_empty, reason, _size = heal.classify(p)
        self.assertTrue(is_empty)
        self.assertEqual(reason, "zero-byte-file")

    # ---- scan_agent_inbox(): end-to-end archive behavior --------------

    def test_scan_does_not_archive_file_in_grace_window(self):
        self._write("signal-5.json", "{partial")  # fresh
        scanned, healed = heal.scan_agent_inbox("observer")
        self.assertEqual(scanned, 1)
        self.assertEqual(healed, 0)
        # File stays in the inbox, untouched, for the writer to finish.
        self.assertTrue((self.agent_inbox / "signal-5.json").exists())
        self.assertFalse(self._archive_dir().exists())

    def test_scan_archives_file_older_than_grace_window(self):
        p = self._write("signal-6.json", "{partial")
        self._age(p, heal.MIN_FILE_AGE_SECONDS + 5)
        scanned, healed = heal.scan_agent_inbox("observer")
        self.assertEqual(scanned, 1)
        self.assertEqual(healed, 1)
        self.assertFalse((self.agent_inbox / "signal-6.json").exists())
        bucket = time.strftime("%Y%m%d", time.gmtime())
        self.assertTrue((self._archive_dir() / bucket / "signal-6.json").exists())

    def test_substantive_file_never_archived_regardless_of_age(self):
        # A real, valid payload must pass through even once aged.
        p = self._write("task-real.json", '{"task_id": "abc", "type": "build"}')
        self._age(p, heal.MIN_FILE_AGE_SECONDS + 5)
        scanned, healed = heal.scan_agent_inbox("observer")
        self.assertEqual((scanned, healed), (1, 0))
        self.assertTrue((self.agent_inbox / "task-real.json").exists())


if __name__ == "__main__":
    unittest.main()
