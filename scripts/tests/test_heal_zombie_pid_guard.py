#!/usr/bin/env python3
"""Tests for the PID-reuse guard in heal_zombie_main_workers.kill_zombie
(2026-06-05 audit #14): the SIGKILL must be skipped when the PID is no longer
the same sysprompt-main worker it was when is_zombie() observed it.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_heal_zombie_pid_guard
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_zombie_main_workers as hz  # noqa: E402


class KillZombiePidGuardTest(unittest.TestCase):
    def setUp(self):
        # log() writes under /home/larry/agents — silence it for the test.
        self._log = mock.patch.object(hz, "log")
        self._log.start()
        self.addCleanup(self._log.stop)

    def test_skips_kill_when_identity_changed(self):
        with mock.patch.object(hz.pid_identity, "still_same_process", return_value=False) as ssp, \
             mock.patch.object(hz.os, "kill") as kill:
            killed = hz.kill_zombie(4242, "pattern-A deleted-cwd", expected_starttime=111)
        self.assertFalse(killed)
        kill.assert_not_called()  # the irreversible SIGKILL must NOT fire
        ssp.assert_called_once()

    def test_kills_when_identity_matches(self):
        with mock.patch.object(hz.pid_identity, "still_same_process", return_value=True), \
             mock.patch.object(hz.os, "kill") as kill:
            killed = hz.kill_zombie(4242, "pattern-B completed-PR", expected_starttime=111)
        self.assertTrue(killed)
        kill.assert_called_once_with(4242, signal.SIGKILL)

    def test_skips_kill_when_starttime_baseline_missing(self):
        # No baseline start time -> we can't verify identity -> fail closed, even
        # if still_same_process would have matched on cmdline alone.
        with mock.patch.object(hz.pid_identity, "still_same_process", return_value=True) as ssp, \
             mock.patch.object(hz.os, "kill") as kill:
            killed = hz.kill_zombie(4242, "pattern-A", expected_starttime=None)
        self.assertFalse(killed)
        kill.assert_not_called()
        ssp.assert_not_called()  # short-circuits before even calling the guard

    def test_guard_passes_worker_signature_and_starttime(self):
        with mock.patch.object(hz.pid_identity, "still_same_process", return_value=True) as ssp, \
             mock.patch.object(hz.os, "kill"):
            hz.kill_zombie(4242, "r", expected_starttime=777)
        _, kwargs = ssp.call_args
        self.assertEqual(ssp.call_args.args[0], 4242)
        self.assertEqual(ssp.call_args.args[1], 777)
        self.assertEqual(kwargs.get("require_cmdline_substr"), hz.SYSPROMPT_FILTER)


if __name__ == "__main__":
    unittest.main()
