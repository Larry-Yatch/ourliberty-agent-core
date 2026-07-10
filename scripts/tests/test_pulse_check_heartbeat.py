#!/usr/bin/env python3
"""Tests for pulse_check_heartbeat (per-check liveness signals).

Covers:
  - emit_heartbeat writes the {ts, check} shape atomically
  - run_check emits a heartbeat on rc==0 and not on failure
  - run_check emits a pulse-check-failed envelope on nonzero rc and on raise
  - run_check returns 1 (not a crash) on an uncaught exception, calling log_fn
  - SystemExit (argparse) propagates untouched

larry_alerts is stubbed — no real alert queue is ever touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_heartbeat
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_heartbeat as hb  # noqa: E402


class HeartbeatEmitTest(unittest.TestCase):
    def setUp(self):
        self._calls = []
        # Redirect agents_root at the module level for the duration.
        self._orig_agents_root = hb.agents_root

    def tearDown(self):
        hb.agents_root = self._orig_agents_root

    def _point_root(self, tmp):
        hb.agents_root = lambda: Path(tmp)

    def test_emit_heartbeat_writes_expected_shape(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self._point_root(td)
            self.assertTrue(hb.emit_heartbeat('ix'))
            path = hb.heartbeat_path('ix')
            self.assertTrue(path.exists())
            obj = json.loads(path.read_text())
            self.assertEqual(obj['check'], 'ix')
            self.assertIn('ts', obj)
            # No leftover temp file.
            self.assertEqual(
                list(path.parent.glob('*.tmp')), [],
                'atomic write left a .tmp file behind',
            )

    def test_emit_heartbeat_never_raises_on_bad_root(self):
        # Point at a path that cannot be created (under an existing file).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / 'afile'
            blocker.write_text('x')
            hb.agents_root = lambda: blocker  # blackboard dir mkdir will fail
            self.assertFalse(hb.emit_heartbeat('ix'))

    def test_emit_deferral_increments_consecutive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self._point_root(td)
            self.assertTrue(hb.emit_deferral('main-suite-guardian'))
            path = hb.deferral_path('main-suite-guardian')
            obj = json.loads(path.read_text())
            self.assertEqual(obj['check'], 'main-suite-guardian')
            self.assertEqual(obj['consecutive'], 1)
            self.assertIn('ts', obj)
            # A second deferral without an intervening success bumps the streak.
            self.assertTrue(hb.emit_deferral('main-suite-guardian'))
            self.assertEqual(
                json.loads(path.read_text())['consecutive'], 2)
            self.assertEqual(
                list(path.parent.glob('*.tmp')), [],
                'atomic write left a .tmp file behind',
            )

    def test_emit_heartbeat_clears_deferral_streak(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self._point_root(td)
            hb.emit_deferral('main-suite-guardian')
            hb.emit_deferral('main-suite-guardian')  # consecutive == 2
            self.assertTrue(hb.deferral_path('main-suite-guardian').exists())
            # A completed run resets the streak by removing the deferral file.
            self.assertTrue(hb.emit_heartbeat('main-suite-guardian'))
            self.assertFalse(hb.deferral_path('main-suite-guardian').exists())
            # A subsequent deferral therefore starts counting from 1 again.
            hb.emit_deferral('main-suite-guardian')
            self.assertEqual(
                json.loads(
                    hb.deferral_path('main-suite-guardian').read_text()
                )['consecutive'],
                1,
            )

    def test_emit_deferral_never_raises_on_bad_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / 'afile'
            blocker.write_text('x')
            hb.agents_root = lambda: blocker
            self.assertFalse(hb.emit_deferral('main-suite-guardian'))


class _FakeAlerts:
    def __init__(self):
        self.calls = []

    def append_alert(self, **kw):
        self.calls.append(kw)
        return True


class RunCheckTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self._orig_agents_root = hb.agents_root
        hb.agents_root = lambda: Path(self._td.name)
        self._fake = _FakeAlerts()
        sys.modules['larry_alerts'] = self._fake  # emit_failure imports this

    def tearDown(self):
        hb.agents_root = self._orig_agents_root
        sys.modules.pop('larry_alerts', None)
        self._td.cleanup()

    def test_clean_exit_emits_heartbeat_only(self):
        rc = hb.run_check('ix', lambda: 0)
        self.assertEqual(rc, 0)
        self.assertTrue(hb.heartbeat_path('ix').exists())
        self.assertEqual(self._fake.calls, [])

    def test_nonzero_exit_emits_failure_not_heartbeat(self):
        rc = hb.run_check('x', lambda: 2)
        self.assertEqual(rc, 2)
        self.assertFalse(hb.heartbeat_path('x').exists())
        self.assertEqual(len(self._fake.calls), 1)
        call = self._fake.calls[0]
        self.assertEqual(call['source'], 'pulse-check')
        self.assertEqual(call['subject'], 'pulse-check-failed:x')

    def test_exception_emits_failure_and_returns_1(self):
        logged = []

        def boom():
            raise RuntimeError('kaboom')

        rc = hb.run_check(
            'iv', boom, log_fn=lambda m, lvl='INFO': logged.append((lvl, m)),
        )
        self.assertEqual(rc, 1)
        self.assertFalse(hb.heartbeat_path('iv').exists())
        self.assertEqual(len(self._fake.calls), 1)
        self.assertEqual(self._fake.calls[0]['subject'], 'pulse-check-failed:iv')
        self.assertIn('kaboom', self._fake.calls[0]['message'])
        # log_fn was called with the FATAL line at ERROR level.
        self.assertTrue(any(lvl == 'ERROR' for lvl, _ in logged))

    def test_systemexit_propagates(self):
        def argparse_like():
            raise SystemExit(2)

        with self.assertRaises(SystemExit):
            hb.run_check('i', argparse_like)
        # No heartbeat, no failure envelope for an arg-parse style exit.
        self.assertFalse(hb.heartbeat_path('i').exists())
        self.assertEqual(self._fake.calls, [])

    def test_argv_is_forwarded(self):
        seen = {}

        def main_with_argv(argv):
            seen['argv'] = argv
            return 0

        rc = hb.run_check('iii', main_with_argv, argv=['--force'])
        self.assertEqual(rc, 0)
        self.assertEqual(seen['argv'], ['--force'])


if __name__ == '__main__':
    unittest.main()
