#!/usr/bin/env python3
"""Tests for inbox_dispatch_order — the queued-lane / dispatch ordering rule
shared by inbox_watcher.scan_inbox and dashboard_api._reader_agent_queue_queued
(forge-queue-fast-track).

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_inbox_dispatch_order
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_dispatch_order as ido  # noqa: E402


class OrderPendingTest(unittest.TestCase):
    def _ids(self, entries):
        return [payload for _, _, payload in ido.order_pending(entries)]

    def test_normal_only_is_oldest_mtime_first(self):
        # (mtime, fast_tracked_at, payload). Out of order on input.
        entries = [
            (30.0, None, 'a'),
            (10.0, None, 'b'),
            (20.0, None, 'c'),
        ]
        self.assertEqual(self._ids(entries), ['b', 'c', 'a'])

    def test_fast_tracked_floats_ahead_of_all_normal(self):
        entries = [
            (10.0, None, 'old'),          # oldest normal
            (20.0, None, 'mid'),
            (99.0, '2026-06-23T10:00:00+00:00', 'ft'),  # newest mtime, but ft
        ]
        # ft jumps the whole FIFO line despite being the newest file.
        self.assertEqual(self._ids(entries), ['ft', 'old', 'mid'])

    def test_multiple_fast_tracked_are_newest_first_lifo(self):
        # Click order: A then B then C. C is newest stamp -> head; A oldest
        # stamp -> last of the fast-tracked group; all still ahead of normal.
        entries = [
            (5.0, '2026-06-23T10:00:00+00:00', 'A'),
            (5.0, '2026-06-23T10:00:05+00:00', 'B'),
            (5.0, '2026-06-23T10:00:09+00:00', 'C'),
            (1.0, None, 'normal-old'),
        ]
        self.assertEqual(self._ids(entries), ['C', 'B', 'A', 'normal-old'])

    def test_empty(self):
        self.assertEqual(ido.order_pending([]), [])

    def test_stable_on_equal_keys(self):
        # Identical mtimes keep input order (stable sort); same for identical
        # fast_tracked_at stamps.
        entries = [
            (5.0, None, 'n1'),
            (5.0, None, 'n2'),
            (5.0, 'T', 'f1'),
            (5.0, 'T', 'f2'),
        ]
        self.assertEqual(self._ids(entries), ['f1', 'f2', 'n1', 'n2'])


class ReadFastTrackedAtTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ido-'))

    def _write(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.write_text(text, encoding='utf-8')
        return p

    def test_present(self):
        p = self._write('t.json',
                        '{"task_id": "t", "fast_tracked_at": "2026-06-23T00:00:00+00:00"}')
        self.assertEqual(ido.read_fast_tracked_at(p), '2026-06-23T00:00:00+00:00')

    def test_absent_field(self):
        p = self._write('t.json', '{"task_id": "t"}')
        self.assertIsNone(ido.read_fast_tracked_at(p))

    def test_blank_or_non_string(self):
        self.assertIsNone(ido.read_fast_tracked_at(
            self._write('a.json', '{"fast_tracked_at": ""}')))
        self.assertIsNone(ido.read_fast_tracked_at(
            self._write('b.json', '{"fast_tracked_at": 123}')))
        self.assertIsNone(ido.read_fast_tracked_at(
            self._write('c.json', '{"fast_tracked_at": null}')))

    def test_corrupt_json_falls_back_to_none(self):
        # A half-written / non-JSON file must never raise — it's treated as
        # not-fast-tracked so a bad file can't wedge dispatch.
        p = self._write('half.json', '{"task_id": "t", "fast_trac')
        self.assertIsNone(ido.read_fast_tracked_at(p))

    def test_non_object_payload(self):
        self.assertIsNone(ido.read_fast_tracked_at(
            self._write('list.json', '["not", "an", "object"]')))

    def test_missing_file(self):
        self.assertIsNone(ido.read_fast_tracked_at(self.tmp / 'nope.json'))


if __name__ == '__main__':
    unittest.main()
