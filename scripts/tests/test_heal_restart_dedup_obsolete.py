#!/usr/bin/env python3
"""Tests for heal_restart_dedup_obsolete (audit #54 no-clobber archive)."""

import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_restart_dedup_obsolete as mod  # noqa: E402

# Unit tests for the no-clobber helper itself live in test_atomic_io
# (NoClobberDestTest); this suite exercises the healer's end-to-end use of it.


class ArchiveCollisionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.main_inbox = self.tmp / "inboxes" / "main"
        self.archive = self.main_inbox / "archive"
        self.archive.mkdir(parents=True, exist_ok=True)
        self._orig = {
            "MAIN_INBOX": mod.MAIN_INBOX,
            "ARCHIVE_DIR": mod.ARCHIVE_DIR,
            "KILL_SWITCH": mod.KILL_SWITCH,
            "LOG_FILE": mod.LOG_FILE,
        }
        mod.MAIN_INBOX = self.main_inbox
        mod.ARCHIVE_DIR = self.archive
        mod.KILL_SWITCH = self.tmp / "healers.disabled"
        mod.LOG_FILE = self.tmp / "log.log"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(mod, k, v)

    def test_archive_collision_preserves_prior(self):
        name = "20260101000000-foo.json"
        # A prior archived copy already exists.
        (self.archive / name).write_text("OLD ARCHIVED CONTENT")
        # A new stale file with the identical name lands in main inbox.
        new_file = self.main_inbox / name
        new_file.write_text("NEW CONTENT")
        old = time.time() - (mod.STALE_HOURS * 3600) - 60
        os.utime(new_file, (old, old))

        rc = mod.main()
        self.assertEqual(rc, 0)
        # Prior archive is intact; new file archived under a suffixed name.
        self.assertEqual((self.archive / name).read_text(), "OLD ARCHIVED CONTENT")
        self.assertEqual(
            (self.archive / "20260101000000-foo.1.json").read_text(), "NEW CONTENT"
        )
        self.assertFalse(new_file.exists())


if __name__ == "__main__":
    unittest.main()
