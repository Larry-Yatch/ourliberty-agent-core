#!/usr/bin/env python3
"""test_test_isolation_guard.py — focused meta-test for the Layer B guard
primitive (scripts/test_isolation_guard.py).

Proves the guard works BOTH ways:
  * RAISES ``TestIsolationBreach`` when the run sentinel is set (i.e. inside a
    test process — which is exactly the leak we want to fail loud on), and
  * is a pure PASS-THROUGH when the sentinel is unset (production) or inside the
    ``allow()`` escape hatch.

If either direction regresses, every other chokepoint guard built on this
primitive silently stops protecting — so this test is the keystone.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import unittest

import test_isolation_guard as guard

_SENTINEL = 'OURLIBERTY_TEST_RUN_SENTINEL'


class RefuseUnderTestTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(_SENTINEL)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(_SENTINEL, None)
        else:
            os.environ[_SENTINEL] = self._saved

    def test_raises_when_sentinel_set(self):
        os.environ[_SENTINEL] = '1'
        with self.assertRaises(guard.TestIsolationBreach):
            guard.refuse_under_test('larry-alerts')

    def test_breach_message_names_channel(self):
        os.environ[_SENTINEL] = '1'
        try:
            guard.refuse_under_test('telegram-send')
        except guard.TestIsolationBreach as e:
            self.assertIn('telegram-send', str(e))
        else:  # pragma: no cover - guard must raise
            self.fail('refuse_under_test did not raise with sentinel set')

    def test_pass_through_when_sentinel_unset(self):
        os.environ.pop(_SENTINEL, None)
        # No raise == pass-through (production behavior).
        self.assertIsNone(guard.refuse_under_test('supabase'))

    def test_breach_is_a_runtimeerror(self):
        # Documents the inheritance contract: a broad ``except Exception`` in a
        # producer still catches it (accepted for the few fire-and-forget
        # healers) while ``except (OSError, ...)`` clauses do NOT swallow it.
        self.assertTrue(issubclass(guard.TestIsolationBreach, RuntimeError))


class AllowEscapeHatchTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(_SENTINEL)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(_SENTINEL, None)
        else:
            os.environ[_SENTINEL] = self._saved

    def test_allow_clears_then_restores_sentinel(self):
        os.environ[_SENTINEL] = 'token-123'
        with guard.allow('supabase'):
            # Inside the hatch the guard is a pass-through.
            self.assertNotIn(_SENTINEL, os.environ)
            self.assertIsNone(guard.refuse_under_test('supabase'))
        # Restored afterward with the exact prior value.
        self.assertEqual(os.environ.get(_SENTINEL), 'token-123')

    def test_allow_restores_even_on_exception(self):
        os.environ[_SENTINEL] = 'token-xyz'
        with self.assertRaises(ValueError):
            with guard.allow('supabase'):
                raise ValueError('boom')
        self.assertEqual(os.environ.get(_SENTINEL), 'token-xyz')

    def test_allow_no_op_when_sentinel_already_unset(self):
        os.environ.pop(_SENTINEL, None)
        with guard.allow('supabase'):
            self.assertNotIn(_SENTINEL, os.environ)
        # Still unset; nothing spuriously restored.
        self.assertNotIn(_SENTINEL, os.environ)


class GhWriteTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(_SENTINEL)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(_SENTINEL, None)
        else:
            os.environ[_SENTINEL] = self._saved

    def test_gh_write_refuses_under_test(self):
        os.environ[_SENTINEL] = '1'
        with self.assertRaises(guard.TestIsolationBreach):
            # Must raise BEFORE shelling out — the command never runs.
            guard.gh_write(['gh', 'pr', 'merge', '999'])

    def test_gh_write_runs_subprocess_when_allowed(self):
        os.environ[_SENTINEL] = '1'
        with guard.allow('gh-write'):
            # A harmless real subprocess proves the pass-through path shells out
            # and returns the CompletedProcess (kwargs forwarded).
            result = guard.gh_write(
                ['true'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
