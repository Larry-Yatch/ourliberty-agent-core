"""test_atomic_io_guard.py — the shared durable-write helper (atomic_io) is the
one chokepoint 39 modules route state writes through, so guarding it there
extends the destination-aware test-jail seal (PR #815) from the four tier-state
writers to every atomic_io caller (missions/projects/inbox/dashboard/...).

Same safety discipline as test_tier_state_write_guard: the live-tree cases
monkeypatch `_real_agents_roots` to a throwaway dir, so the real tree is never
at risk and the guard raises before any mkdir/write anyway.
"""
import _bootstrap  # noqa: F401  # arm sandbox + run sentinel first
import os
import unittest
from pathlib import Path

import atomic_io
import test_isolation_guard as tig
from test_isolation_guard import TestIsolationBreach


class AtomicIoGuardTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.environ.get('OURLIBERTY_TEST_RUN_SENTINEL'),
                        'run sentinel must be armed for this suite')
        self.tmp = Path(self.enterContext(_tmp()))
        self.fake_real = self.tmp / 'agents'
        _patch(self, tig, '_real_agents_roots', lambda: [self.fake_real.resolve()])

    def test_json_refuses_live(self):
        with self.assertRaises(TestIsolationBreach):
            atomic_io.atomic_write_json(
                self.fake_real / 'blackboard' / 'missions.json', {'x': 1})
        self.assertFalse((self.fake_real / 'blackboard' / 'missions.json').exists())

    def test_text_refuses_live(self):
        with self.assertRaises(TestIsolationBreach):
            atomic_io.atomic_write_text(self.fake_real / 'state' / 'x.txt', 'hi')

    def test_bytes_refuses_live(self):
        with self.assertRaises(TestIsolationBreach):
            atomic_io.atomic_write_bytes(self.fake_real / 'state' / 'x.bin', b'hi')

    def test_allows_sandbox_write(self):
        sandbox = self.tmp / 'sandbox-agents'  # NOT under fake_real
        out = atomic_io.atomic_write_json(
            sandbox / 'blackboard' / 'missions.json', {'ok': True})
        self.assertTrue(out.exists())
        self.assertIn('ok', out.read_text())

    def test_allows_non_agents_path(self):
        # A write entirely outside any agents tree (e.g. a repo/config file) is
        # always allowed under test.
        out = atomic_io.atomic_write_text(self.tmp / 'plain' / 'f.txt', 'data')
        self.assertTrue(out.exists())

    def test_production_passthrough(self):
        # Sentinel cleared == production: a real-tree path writes normally.
        with tig.allow('unit'):
            out = atomic_io.atomic_write_text(self.fake_real / 'logs' / 'p.txt', 'p')
            self.assertTrue(out.exists())


def _tmp():
    import tempfile
    return tempfile.TemporaryDirectory()


def _patch(tc, obj, attr, value):
    orig = getattr(obj, attr)
    setattr(obj, attr, value)
    tc.addCleanup(setattr, obj, attr, orig)


if __name__ == '__main__':
    unittest.main()
