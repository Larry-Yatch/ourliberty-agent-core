#!/usr/bin/env python3
"""Tests for concurrency_guard — the file-based global semaphore.

Focus (nervous-system-audit 2026-06-05): PID-reuse robustness. A slot whose
owning process died and whose PID was recycled by an unrelated process must be
dropped (not squat a capacity slot), while a verified-same long-running process
keeps its slot even past STALE_TIMEOUT.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_concurrency_guard
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import concurrency_guard as cg  # noqa: E402


class _IsolatedGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / 'config').mkdir(parents=True, exist_ok=True)
        self._patch = mock.patch.object(
            cg, 'GUARD_FILE', root / 'config' / '.concurrency-guard.json')
        self._patch.start()
        cg._guard = None  # reset the module singleton

    def tearDown(self) -> None:
        self._patch.stop()
        cg._guard = None
        self._tmp.cleanup()

    def _write_slots(self, slots):
        cg.GUARD_FILE.write_text(json.dumps({'slots': slots, 'max': cg.MAX_CONCURRENT}))


class AcquireReleaseTest(_IsolatedGuard):
    def test_acquire_records_start_token(self):
        g = cg.ConcurrencyGuard()
        with mock.patch.object(cg, '_proc_start_token', return_value='TOK'):
            self.assertTrue(g.acquire('agent-a', 'task-1'))
        data = json.loads(cg.GUARD_FILE.read_text())
        self.assertEqual(len(data['slots']), 1)
        self.assertEqual(data['slots'][0]['start'], 'TOK')

    def test_capacity_enforced(self):
        g = cg.ConcurrencyGuard()
        slots = [{'pid': 10_000 + i, 'agent': f'a{i}', 'ts': time.time(),
                  'start': f'tok{i}'} for i in range(cg.MAX_CONCURRENT)]
        self._write_slots(slots)
        with mock.patch.object(cg.os, 'kill', return_value=None):
            with mock.patch.object(cg, '_proc_start_token',
                                   side_effect=lambda pid: f'tok{pid - 10_000}'):
                self.assertFalse(g.acquire('overflow'))

    def test_release_removes_this_pid(self):
        g = cg.ConcurrencyGuard()
        with mock.patch.object(cg, '_proc_start_token', return_value='TOK'):
            g.acquire('agent-a')
        g.release('agent-a')
        data = json.loads(cg.GUARD_FILE.read_text())
        self.assertEqual(data['slots'], [])


class CleanStaleTest(_IsolatedGuard):
    def setUp(self) -> None:
        super().setUp()
        self.g = cg.ConcurrencyGuard()

    def test_dead_pid_dropped(self):
        self._write_slots([{'pid': 4242, 'agent': 'a', 'ts': time.time(),
                            'start': 'tok'}])
        with mock.patch.object(cg.os, 'kill', side_effect=ProcessLookupError):
            self.assertEqual(self.g.active_count(), 0)

    def test_pid_reuse_dropped_on_token_mismatch(self):
        # PID alive, but the start token differs → the PID was recycled.
        self._write_slots([{'pid': 4242, 'agent': 'a', 'ts': time.time(),
                            'start': 'OLD-TOKEN'}])
        with mock.patch.object(cg.os, 'kill', return_value=None):
            with mock.patch.object(cg, '_proc_start_token', return_value='NEW-TOKEN'):
                self.assertEqual(self.g.active_count(), 0)

    def test_same_process_kept_even_past_stale_timeout(self):
        # Token matches → it IS the original process; keep it regardless of age
        # (a long build legitimately outlives STALE_TIMEOUT).
        old_ts = time.time() - (cg.STALE_TIMEOUT + 600)
        self._write_slots([{'pid': 4242, 'agent': 'a', 'ts': old_ts,
                            'start': 'SAME'}])
        with mock.patch.object(cg.os, 'kill', return_value=None):
            with mock.patch.object(cg, '_proc_start_token', return_value='SAME'):
                self.assertEqual(self.g.active_count(), 1)

    def test_unverifiable_token_falls_back_to_time_reap(self):
        # No /proc (token None): fall back to STALE_TIMEOUT. Fresh slot kept...
        fresh = [{'pid': 4242, 'agent': 'a', 'ts': time.time(), 'start': None}]
        self._write_slots(fresh)
        with mock.patch.object(cg.os, 'kill', return_value=None):
            with mock.patch.object(cg, '_proc_start_token', return_value=None):
                self.assertEqual(self.g.active_count(), 1)
        # ...stale slot reaped.
        stale = [{'pid': 4242, 'agent': 'a',
                  'ts': time.time() - (cg.STALE_TIMEOUT + 1), 'start': None}]
        self._write_slots(stale)
        with mock.patch.object(cg.os, 'kill', return_value=None):
            with mock.patch.object(cg, '_proc_start_token', return_value=None):
                self.assertEqual(self.g.active_count(), 0)

    def test_legacy_slot_without_start_uses_time_reap(self):
        # A slot written before this change has no 'start' key → time-based.
        stale = [{'pid': 4242, 'agent': 'a',
                  'ts': time.time() - (cg.STALE_TIMEOUT + 1)}]
        self._write_slots(stale)
        with mock.patch.object(cg.os, 'kill', return_value=None):
            with mock.patch.object(cg, '_proc_start_token', return_value='whatever'):
                # stored is None (missing key) → fall back to time reap → reaped.
                self.assertEqual(self.g.active_count(), 0)


class MaxConcurrentResolutionTest(unittest.TestCase):
    """The documented safe ceiling is 6 (RAM math in the module docstring). A
    prior bootstrap left MAX_CONCURRENT hard-coded at 10, contradicting the
    comment and risking OOM. These tests pin the value, not just the mechanics.
    """

    def test_default_is_six(self):
        with mock.patch.dict('os.environ', {}, clear=False):
            os_environ = cg.os.environ
            if 'OURLIBERTY_MAX_CONCURRENT' in os_environ:
                del os_environ['OURLIBERTY_MAX_CONCURRENT']
            self.assertEqual(cg._resolve_max_concurrent(), 6)

    def test_module_constant_within_safe_band(self):
        self.assertGreaterEqual(cg.MAX_CONCURRENT, 1)
        self.assertLessEqual(cg.MAX_CONCURRENT, 6)

    def test_env_override_within_band(self):
        with mock.patch.dict('os.environ',
                             {'OURLIBERTY_MAX_CONCURRENT': '3'}):
            self.assertEqual(cg._resolve_max_concurrent(), 3)

    def test_env_override_above_ceiling_rejected(self):
        with mock.patch.dict('os.environ',
                             {'OURLIBERTY_MAX_CONCURRENT': '10'}):
            with self.assertRaises(ValueError):
                cg._resolve_max_concurrent()

    def test_env_override_zero_rejected(self):
        with mock.patch.dict('os.environ',
                             {'OURLIBERTY_MAX_CONCURRENT': '0'}):
            with self.assertRaises(ValueError):
                cg._resolve_max_concurrent()

    def test_env_override_non_integer_rejected(self):
        with mock.patch.dict('os.environ',
                             {'OURLIBERTY_MAX_CONCURRENT': 'lots'}):
            with self.assertRaises(ValueError):
                cg._resolve_max_concurrent()


class ProcStartTokenTest(unittest.TestCase):
    def test_returns_none_for_missing_pid(self):
        # PID 999999 almost certainly doesn't exist; on macOS /proc is absent.
        self.assertIsNone(cg._proc_start_token(999999))

    def test_parses_starttime_field(self):
        # comm contains a space + parens to exercise the rfind(')') handling.
        # starttime is field 22 → index 19 of the post-comm fields (state is
        # field 3 = index 0). Build exactly 20 post-comm tokens ending in it.
        post_comm = 'S ' + ' '.join(['0'] * 18) + ' 998877'
        fake = (f'4242 (my proc) {post_comm}\n').encode()
        m = mock.mock_open(read_data=fake)
        with mock.patch('builtins.open', m):
            self.assertEqual(cg._proc_start_token(4242), '998877')


class FailClosedTest(_IsolatedGuard):
    """audit #6: a corrupt guard file must fail CLOSED (refuse / read as full),
    never reset to empty and over-commit past MAX_CONCURRENT."""

    def test_corrupt_file_acquire_refuses(self):
        cg.GUARD_FILE.write_text('{ truncated json')  # SIGKILL-mid-write shape
        g = cg.ConcurrencyGuard()
        with mock.patch.object(cg, '_proc_start_token', return_value='TOK'):
            self.assertFalse(g.acquire('agent-a', 'task-1'))

    def test_corrupt_file_active_count_reads_full(self):
        cg.GUARD_FILE.write_text('not json at all')
        self.assertEqual(cg.ConcurrencyGuard().active_count(), cg.MAX_CONCURRENT)

    def test_corrupt_file_release_is_noop(self):
        cg.GUARD_FILE.write_text('{ broken')
        before = cg.GUARD_FILE.read_text()
        cg.ConcurrencyGuard().release('agent-a')  # must not clobber/empty it
        self.assertEqual(cg.GUARD_FILE.read_text(), before)

    def test_missing_file_reads_empty_and_acquires(self):
        # missing (vs corrupt) is the legitimate first-run case → empty, grants.
        if cg.GUARD_FILE.exists():
            cg.GUARD_FILE.unlink()
        g = cg.ConcurrencyGuard()  # __init__ recreates empty
        self.assertEqual(g.active_count(), 0)
        with mock.patch.object(cg, '_proc_start_token', return_value='TOK'):
            self.assertTrue(g.acquire('agent-a', 'task-1'))

    def test_write_leaves_no_tmp_and_valid_json(self):
        g = cg.ConcurrencyGuard()
        with mock.patch.object(cg, '_proc_start_token', return_value='TOK'):
            g.acquire('agent-a', 'task-1')
        json.loads(cg.GUARD_FILE.read_text())  # valid
        leftovers = list(cg.GUARD_FILE.parent.glob('.concurrency-guard.*.tmp'))
        self.assertEqual(leftovers, [])


if __name__ == '__main__':
    unittest.main()
