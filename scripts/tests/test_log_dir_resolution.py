#!/usr/bin/env python3
"""Test family D — production-default verification for resolve_log_dir().

Locks the invariant that when OURLIBERTY_LOG_DIR is UNSET, both production
modules' resolve_log_dir() returns the canonical ~/agents/logs/ path. The
whole test-isolation discipline (PR #137 + this PR) depends on the env-var
override being a no-op in production — if the default ever silently drifts
to a different path (e.g., /home/larry/agent-core/logs/), the live
beacon_telegram_bot.log relocates and operators lose their trail.

Why this test is critical: the brief that authored this PR proposed the
WRONG default path. Without this regression test, a future refactor that
re-introduces a hardcoded default could quietly move the production log
file without anyone noticing.

These tests must explicitly delete OURLIBERTY_LOG_DIR via monkeypatch.delenv
to undo the conftest autouse fixture — otherwise they'd test the test-time
override instead of the production default.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_log_dir_resolution
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


class AgentRunnerResolveLogDirTest(unittest.TestCase):
    """agent_runner.resolve_log_dir() — env var unset → ~/agents/logs/."""

    def setUp(self):
        self.ar = importlib.import_module('agent_runner')

    def test_default_is_home_agents_logs_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
            self.assertEqual(
                self.ar.resolve_log_dir(),
                Path.home() / 'agents' / 'logs',
            )

    def test_override_is_used_when_env_set(self):
        with mock.patch.dict(os.environ, {'OURLIBERTY_LOG_DIR': '/tmp/x/y'}):
            self.assertEqual(self.ar.resolve_log_dir(), Path('/tmp/x/y'))

    def test_override_with_empty_string_falls_back_to_default(self):
        # Defensive: an explicitly empty OURLIBERTY_LOG_DIR is treated the
        # same as unset. This guards against a sloppy `OURLIBERTY_LOG_DIR=`
        # in a systemd EnvironmentFile silently relocating logs to '.'.
        with mock.patch.dict(os.environ, {'OURLIBERTY_LOG_DIR': ''}):
            self.assertEqual(
                self.ar.resolve_log_dir(),
                Path.home() / 'agents' / 'logs',
            )


class BeaconBotResolveLogDirTest(unittest.TestCase):
    """beacon_telegram_bot.resolve_log_dir() — same invariant.

    beacon_telegram_bot validates TOKEN/ALLOWED at RUN (in main()), not import, so it
    imports cleanly now; we still supply placeholders so the module's env-derived
    constants resolve to test values. Force a clean re-import so the module-level LOG_DIR
    is re-evaluated against the env state we want for the specific test.
    """

    def _import_bot_with_env(self, override_value=None):
        os.environ.setdefault('TELEGRAM_BOT_TOKEN_BEACON', 'TEST_TOKEN')
        os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_IDS', '12345')
        if override_value is None:
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
        else:
            os.environ['OURLIBERTY_LOG_DIR'] = override_value
        if 'beacon_telegram_bot' in sys.modules:
            del sys.modules['beacon_telegram_bot']
        return importlib.import_module('beacon_telegram_bot')

    def test_default_is_home_agents_logs_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            bot = self._import_bot_with_env(override_value=None)
            self.assertEqual(
                bot.resolve_log_dir(),
                Path.home() / 'agents' / 'logs',
            )

    def test_override_is_used_when_env_set(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            bot = self._import_bot_with_env(override_value='/tmp/p/q')
            self.assertEqual(bot.resolve_log_dir(), Path('/tmp/p/q'))


class LogWriteHitsOverrideDirTest(unittest.TestCase):
    """End-to-end-ish: calling agent_runner.log() with OURLIBERTY_LOG_DIR
    set must write into that override dir, NOT the production
    ~/agents/logs/. This is the regression that proves the 2026-05-27
    sentinel-leak class is closed.

    Hermetic by construction: setUp points OURLIBERTY_LOG_DIR at its own
    fresh tmp dir (and tearDown restores the prior value). It must NOT
    depend on the pytest autouse conftest fixture or on the package-import
    isolation in scripts/tests/__init__.py — the latter does not run under
    `python3 -m unittest discover -s scripts/tests` (the regression gate),
    which loads test modules as top-level names and bypasses the package
    __init__. Owning the env var here makes the test pass identically under
    both the dotted-path and discover invocations, and order-independent.
    """

    def setUp(self):
        self.ar = importlib.import_module('agent_runner')
        self._prev_log_dir = os.environ.get('OURLIBERTY_LOG_DIR')
        self._tmp_log_dir = tempfile.mkdtemp(prefix='ourliberty-logwrite-test-')
        self.addCleanup(shutil.rmtree, self._tmp_log_dir, ignore_errors=True)
        os.environ['OURLIBERTY_LOG_DIR'] = self._tmp_log_dir

    def tearDown(self):
        if self._prev_log_dir is None:
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
        else:
            os.environ['OURLIBERTY_LOG_DIR'] = self._prev_log_dir

    def test_log_call_writes_to_overridden_dir(self):
        override = os.environ.get('OURLIBERTY_LOG_DIR')
        self.assertIsNotNone(
            override,
            'setUp should have set OURLIBERTY_LOG_DIR to a tmp dir',
        )
        self.ar.log('test-isolation-v3-canary', 'sentinel-write-from-test')
        expected = Path(override) / 'test-isolation-v3-canary.log'
        self.assertTrue(expected.exists(),
                        f'log file should be at {expected}')
        self.assertIn('sentinel-write-from-test', expected.read_text())
        # And the production path MUST NOT have been written this test run.
        prod_path = Path.home() / 'agents' / 'logs' / 'test-isolation-v3-canary.log'
        self.assertFalse(
            prod_path.exists(),
            f'production log path {prod_path} should not have been touched',
        )


if __name__ == '__main__':
    unittest.main()
