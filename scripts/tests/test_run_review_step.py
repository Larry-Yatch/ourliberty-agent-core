#!/usr/bin/env python3
"""Tests for scripts/run_review_step.sh — the bounded, foreground review-step
runner that removes any reason to background-and-poll during a Mirror review.

Regression coverage for the inbox:mirror-lease starvation class: a Mirror
review that runs a long step (the test regression check, a subagent task, any
slow command) and then hand-rolls an UNBOUNDED poll waiting for it. Three
shapes have fired in production and each wedged the WHOLE review queue for
71-102 min until a human killed the process:

  - PR #101 (2026-05-25): self-matching `pgrep -f`.
  - PR #334 (2026-06-05): empty `pgrep` -> `/proc/$()` collapses to `/proc/`.
  - PR #717 / #720 (2026-06-26): a Bash-tool *background-mode* command polled by
    `until [ -s <task>.output ] && grep -qE 'verdict|timed out|Traceback' ...;
    do sleep 15; done` — the content sentinel never appeared (the command
    emitted only warnings, exited 0), so the poll spun forever.

This helper closes the class by running the step in the FOREGROUND under a hard
wall-clock ceiling: it owns the child, bounds it on the clock, kills the whole
process group on timeout, and reports ONE unambiguous result — the command's
own exit code on completion, or exit 124 + a clear `REVIEW_STEP_TIMED_OUT`
banner on timeout (which Mirror maps to REVIEW_ESCALATE, never a hang).

Run:
    python3 -m unittest scripts.tests.test_run_review_step
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import signal
import subprocess
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RUN_REVIEW_STEP = _REPO_ROOT / 'scripts' / 'run_review_step.sh'

# Exit-code contract (mirrors the script header).
EXIT_OK = 0
EXIT_TIMEOUT = 124
EXIT_USAGE = 2


def _run(*args: str, hard_timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Invoke run_review_step.sh with args. `hard_timeout` is a test-harness
    safety net (NOT the script's --timeout): if the script itself ever wedged —
    the exact failure this helper exists to prevent — this raises rather than
    hanging the suite."""
    return subprocess.run(
        ['bash', str(_RUN_REVIEW_STEP), *args],
        capture_output=True, text=True, timeout=hard_timeout,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunReviewStepPassthroughTest(unittest.TestCase):
    """A step that finishes within budget passes its own exit code through."""

    def setUp(self):
        self.assertTrue(_RUN_REVIEW_STEP.exists(), f'missing helper: {_RUN_REVIEW_STEP}')

    def test_success_exit_code_passthrough(self):
        r = _run('--timeout', '10', '--', 'bash', '-c', 'echo ok; exit 0')
        self.assertEqual(r.returncode, EXIT_OK)
        self.assertIn('ok', r.stdout)
        # A clean run must NOT print the timeout banner.
        self.assertNotIn('REVIEW_STEP_TIMED_OUT', r.stdout)

    def test_nonzero_exit_code_passthrough(self):
        r = _run('--timeout', '10', '--', 'bash', '-c', 'exit 7')
        self.assertEqual(r.returncode, 7)
        self.assertNotIn('REVIEW_STEP_TIMED_OUT', r.stdout)

    def test_command_flags_forwarded_without_double_dash(self):
        # Mirror's real command carries its own --flags (e.g.
        # test_regression_check.py --parent-sha X --output json). Option parsing
        # must stop at the first non-option token so they reach the command.
        r = _run(
            '--timeout', '10',
            'python3', '-c',
            'import sys; print("ARGV", sys.argv[1:])',
            '--parent-sha', 'abc', '--output', 'json',
        )
        self.assertEqual(r.returncode, EXIT_OK)
        self.assertIn("ARGV ['--parent-sha', 'abc', '--output', 'json']", r.stdout)

    def test_timeout_eq_form_accepted(self):
        r = _run('--timeout=10', '--', 'bash', '-c', 'exit 0')
        self.assertEqual(r.returncode, EXIT_OK)


class RunReviewStepTimeoutTest(unittest.TestCase):
    """The load-bearing path: a step that overruns the ceiling is killed and
    yields a clear, ESCALATE-shaped result instead of an infinite poll."""

    def test_timeout_yields_124_and_banner(self):
        # Ceiling 2s, command sleeps far longer. Must return 124 well within the
        # harness safety net, with the human-readable banner + ESCALATE steer.
        start = time.monotonic()
        r = _run('--timeout', '2', '--interval', '1', '--label', 'regression check',
                 '--', 'bash', '-c', 'sleep 30', hard_timeout=20.0)
        elapsed = time.monotonic() - start
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        self.assertIn('=== REVIEW_STEP_TIMED_OUT ===', r.stdout)
        self.assertIn('REVIEW_ESCALATE', r.stdout)
        self.assertIn('regression check', r.stdout)  # the --label echoed
        # Bounded: nowhere near the command's own 30s runtime.
        self.assertLess(elapsed, 15.0)

    def test_timeout_no_terminated_job_noise_on_stdout(self):
        # The banner (stdout) is what Mirror keys off; it must be clean — no
        # bash job-control "Terminated" line bleeding into the verdict surface.
        r = _run('--timeout', '2', '--interval', '1',
                 '--', 'bash', '-c', 'sleep 30', hard_timeout=20.0)
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        self.assertNotIn('Terminated', r.stdout)

    def test_timeout_kills_the_child(self):
        # On timeout the step process itself must be dead, not orphaned.
        r = _run('--timeout', '2', '--interval', '1',
                 '--', 'bash', '-c', 'echo CHILD=$$; sleep 30', hard_timeout=20.0)
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        child_pid = None
        for line in r.stdout.splitlines():
            if line.startswith('CHILD='):
                child_pid = int(line.split('=', 1)[1])
                break
        self.assertIsNotNone(child_pid, 'did not capture child pid from output')
        time.sleep(1.0)  # allow the SIGTERM/SIGKILL to take effect
        self.assertFalse(_pid_alive(child_pid), f'child {child_pid} lingered after timeout')

    def test_timeout_kills_the_process_group(self):
        # A timed-out step's descendants (e.g. pytest workers) must die too —
        # the group kill, not just the direct child. The command spawns a
        # detached-looking sleeper and prints its pid.
        r = _run('--timeout', '2', '--interval', '1',
                 '--', 'bash', '-c', 'sleep 40 & echo GRANDCHILD=$!; wait',
                 hard_timeout=20.0)
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        gc_pid = None
        for line in r.stdout.splitlines():
            if line.startswith('GRANDCHILD='):
                gc_pid = int(line.split('=', 1)[1])
                break
        self.assertIsNotNone(gc_pid, 'did not capture grandchild pid from output')
        time.sleep(1.0)
        alive = _pid_alive(gc_pid)
        if alive:  # don't leak a 40s sleeper if the assertion is about to fail
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertFalse(alive, f'grandchild {gc_pid} survived the process-group kill')

    def test_term_ignoring_step_still_killed_within_grace(self):
        # A step that traps SIGTERM must still die at the SIGKILL escalation —
        # the wait stays bounded by ceiling + grace, never the command runtime.
        start = time.monotonic()
        r = _run('--timeout', '2', '--interval', '1',
                 '--', 'bash', '-c', 'trap "" TERM; sleep 60', hard_timeout=25.0)
        elapsed = time.monotonic() - start
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        # 2s ceiling + 5s grace + slack; nowhere near the 60s the step wanted.
        self.assertLess(elapsed, 20.0)


class RunReviewStepUsageTest(unittest.TestCase):
    """Misuse fails fast (exit 2) — it can never degrade into a hang."""

    def test_no_command_is_usage_error(self):
        r = _run('--timeout', '10')
        self.assertEqual(r.returncode, EXIT_USAGE)
        self.assertIn('no command', r.stderr.lower())

    def test_bad_timeout_value_is_usage_error(self):
        r = _run('--timeout', 'abc', '--', 'bash', '-c', 'exit 0')
        self.assertEqual(r.returncode, EXIT_USAGE)

    def test_zero_timeout_rejected(self):
        # A 0 ceiling would instant-kill a healthy step; reject it.
        r = _run('--timeout', '0', '--', 'bash', '-c', 'exit 0')
        self.assertEqual(r.returncode, EXIT_USAGE)

    def test_bare_timeout_as_last_arg_does_not_hang(self):
        # The shift-2 footgun: a trailing bare `--timeout` with no value must
        # fail fast (exit 2), not spin forever re-processing the flag. The
        # harness safety net would catch a hang as a failure.
        r = _run('--timeout', hard_timeout=10.0)
        self.assertEqual(r.returncode, EXIT_USAGE)
        self.assertIn('requires a value', r.stderr.lower())

    def test_unknown_option_is_usage_error(self):
        r = _run('--bogus', '--', 'bash', '-c', 'exit 0')
        self.assertEqual(r.returncode, EXIT_USAGE)


if __name__ == '__main__':
    unittest.main()
