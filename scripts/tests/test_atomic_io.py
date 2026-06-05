#!/usr/bin/env python3
"""Tests for scripts/atomic_io.py (audit PR-E shared primitives)."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import atomic_io  # noqa: E402


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "state.json"

    def test_writes_content(self):
        atomic_io.atomic_write_json(self.path, {"a": 1})
        self.assertEqual(json.loads(self.path.read_text()), {"a": 1})

    def test_trailing_newline(self):
        atomic_io.atomic_write_json(self.path, {"a": 1})
        self.assertTrue(self.path.read_text().endswith("\n"))

    def test_creates_parent_dirs(self):
        nested = Path(self.tmp) / "x" / "y" / "z.json"
        atomic_io.atomic_write_json(nested, [1, 2, 3])
        self.assertEqual(json.loads(nested.read_text()), [1, 2, 3])

    def test_default_mode_is_0644(self):
        # mkstemp creates 0600; the helper must widen to the umask-default the
        # replaced write_text calls produced, so perms are not silently narrowed.
        atomic_io.atomic_write_text(self.path, "x")
        perm = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(perm, 0o644)

    def test_explicit_mode_respected(self):
        atomic_io.atomic_write_text(self.path, "x", mode=0o600)
        perm = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(perm, 0o600)

    def test_overwrite_preserves_old_on_error(self):
        atomic_io.atomic_write_text(self.path, "ORIGINAL")
        with self.assertRaises(TypeError):
            atomic_io.atomic_write_json(self.path, {"bad": object()})
        self.assertEqual(self.path.read_text(), "ORIGINAL")

    def test_no_tmp_files_left_on_success(self):
        atomic_io.atomic_write_json(self.path, {"a": 1})
        leftovers = [p for p in Path(self.tmp).iterdir() if p.name != "state.json"]
        self.assertEqual(leftovers, [])

    def test_no_tmp_files_left_on_error(self):
        with self.assertRaises(TypeError):
            atomic_io.atomic_write_json(self.path, {"bad": object()})
        self.assertEqual(list(Path(self.tmp).iterdir()), [])

    def test_unique_tmp_names_do_not_collide(self):
        names = set()
        real_mkstemp = tempfile.mkstemp

        def spy(*a, **k):
            fd, name = real_mkstemp(*a, **k)
            names.add(name)
            return fd, name

        tempfile.mkstemp = spy
        try:
            atomic_io.atomic_write_text(self.path, "one")
            atomic_io.atomic_write_text(self.path, "two")
        finally:
            tempfile.mkstemp = real_mkstemp
        self.assertEqual(len(names), 2)
        self.assertEqual(self.path.read_text(), "two")


class NoClobberDestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_free_name_unchanged(self):
        t = self.tmp / "20260101000000-foo.json"
        self.assertEqual(atomic_io.noclobber_dest(t), t)

    def test_collision_appends_suffix(self):
        t = self.tmp / "20260101000000-foo.json"
        t.write_text("first")
        self.assertEqual(
            atomic_io.noclobber_dest(t), self.tmp / "20260101000000-foo.1.json"
        )

    def test_multiple_collisions_increment(self):
        base = "20260101000000-foo"
        (self.tmp / f"{base}.json").write_text("0")
        (self.tmp / f"{base}.1.json").write_text("1")
        (self.tmp / f"{base}.2.json").write_text("2")
        self.assertEqual(
            atomic_io.noclobber_dest(self.tmp / f"{base}.json"),
            self.tmp / f"{base}.3.json",
        )


if __name__ == "__main__":
    unittest.main()
