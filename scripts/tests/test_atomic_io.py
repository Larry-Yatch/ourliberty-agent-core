#!/usr/bin/env python3
"""Tests for scripts/atomic_io.py (audit PR-E shared primitives)."""

import json
import multiprocessing
import os
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

    def test_overwrite_preserves_old_on_error(self):
        # Pre-existing good file.
        atomic_io.atomic_write_text(self.path, "ORIGINAL")
        # Non-serializable object raises inside json.dumps, before any rename.
        with self.assertRaises(TypeError):
            atomic_io.atomic_write_json(self.path, {"bad": object()})
        # Original is untouched.
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
        # Two interleaved writers using the same destination must not share a
        # scratch file. We can't easily force true concurrency here, but we can
        # assert that two mkstemp-based writes in the same dir each produce a
        # distinct temp (the property that prevents the clobber).
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


def _locked_increment(path_str, lock_str, n, barrier):
    """Worker: increment a shared counter under file_lock, n times."""
    path = Path(path_str)
    barrier.wait()
    for _ in range(n):
        with atomic_io.file_lock(lock_str):
            try:
                cur = json.loads(path.read_text())["v"]
            except (OSError, ValueError):
                cur = 0
            atomic_io.atomic_write_json(path, {"v": cur + 1})


class FileLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "counter.json"
        self.lock = Path(self.tmp) / "counter.lock"

    def test_lock_held_flag(self):
        with atomic_io.file_lock(self.lock) as held:
            self.assertTrue(held)

    def test_concurrent_increments_no_lost_update(self):
        atomic_io.atomic_write_json(self.path, {"v": 0})
        procs = 4
        per = 25
        ctx = multiprocessing.get_context("fork") if sys.platform != "win32" else multiprocessing.get_context()
        barrier = ctx.Barrier(procs)
        workers = [
            ctx.Process(
                target=_locked_increment,
                args=(str(self.path), str(self.lock), per, barrier),
            )
            for _ in range(procs)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(30)
        for w in workers:
            self.assertFalse(w.is_alive())
        # Without the lock, racing read-modify-write would lose updates and
        # land well under procs*per. With it, every increment survives.
        self.assertEqual(json.loads(self.path.read_text())["v"], procs * per)


if __name__ == "__main__":
    unittest.main()
