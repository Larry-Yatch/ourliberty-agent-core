#!/usr/bin/env python3
"""Tests for scripts/atomic_io.py — the shared durable atomic-write helper.

PR-E (2026-06-05 full-codebase audit; findings #7, #54, #58, #62).

Run::

    cd ~/agent-core && python3 -m unittest scripts.tests.test_atomic_io
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
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import atomic_io  # noqa: E402


class TestAtomicWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _siblings(self, path: Path) -> list[str]:
        return sorted(p.name for p in path.parent.iterdir())

    def test_write_bytes_roundtrip(self):
        p = self.tmp / 'a.bin'
        atomic_io.atomic_write_bytes(p, b'\x00\x01hello')
        self.assertEqual(p.read_bytes(), b'\x00\x01hello')

    def test_write_text_roundtrip(self):
        p = self.tmp / 'a.txt'
        atomic_io.atomic_write_text(p, 'héllo')
        self.assertEqual(p.read_text(encoding='utf-8'), 'héllo')

    def test_write_json_roundtrip(self):
        p = self.tmp / 'a.json'
        atomic_io.atomic_write_json(p, {'k': [1, 2]}, indent=2)
        self.assertEqual(json.loads(p.read_text()), {'k': [1, 2]})

    def test_json_trailing_newline(self):
        p = self.tmp / 'nl.json'
        atomic_io.atomic_write_json(p, {'k': 1}, trailing_newline=True)
        self.assertTrue(p.read_text().endswith('}\n'))
        p2 = self.tmp / 'no-nl.json'
        atomic_io.atomic_write_json(p2, {'k': 1}, trailing_newline=False)
        self.assertTrue(p2.read_text().endswith('}'))
        self.assertFalse(p2.read_text().endswith('\n'))

    def test_creates_parent_dirs(self):
        p = self.tmp / 'nested' / 'deep' / 'a.json'
        atomic_io.atomic_write_json(p, {'ok': True})
        self.assertTrue(p.exists())

    def test_overwrites_existing(self):
        p = self.tmp / 'a.json'
        atomic_io.atomic_write_json(p, {'v': 1})
        atomic_io.atomic_write_json(p, {'v': 2})
        self.assertEqual(json.loads(p.read_text()), {'v': 2})

    def test_no_leftover_tmp_on_success(self):
        p = self.tmp / 'a.json'
        atomic_io.atomic_write_json(p, {'v': 1})
        # The destination is the ONLY file left; no stray .tmp sibling.
        self.assertEqual(self._siblings(p), ['a.json'])

    def test_unique_tmp_not_fixed_name(self):
        # Audit #62: a fixed '<path>.tmp' is the bug. Capture the tmp name the
        # helper picks and assert it is NOT the predictable fixed name.
        p = self.tmp / 'seq.json'
        real_mkstemp = tempfile.mkstemp
        captured = {}

        def _spy(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            captured['name'] = name
            return fd, name

        with mock.patch('atomic_io.tempfile.mkstemp', side_effect=_spy):
            atomic_io.atomic_write_json(p, {'v': 1})
        self.assertIn('name', captured)
        self.assertNotEqual(Path(captured['name']).name, 'seq.json.tmp')
        self.assertTrue(Path(captured['name']).name.startswith('.seq.json.'))

    def test_failure_cleans_tmp_and_preserves_original(self):
        p = self.tmp / 'a.json'
        atomic_io.atomic_write_json(p, {'v': 'original'})
        # Force the write to blow up after the tmp is created (during write).
        with mock.patch('atomic_io.os.replace',
                        side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                atomic_io.atomic_write_json(p, {'v': 'new'})
        # Original intact, no stray tmp left behind.
        self.assertEqual(json.loads(p.read_text()), {'v': 'original'})
        self.assertEqual(self._siblings(p), ['a.json'])

    def test_mode_applied(self):
        p = self.tmp / 'a.json'
        atomic_io.atomic_write_json(p, {'v': 1}, mode=0o600)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        atomic_io.atomic_write_json(p, {'v': 2})  # default 0o644
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o644)


if __name__ == '__main__':
    unittest.main()
