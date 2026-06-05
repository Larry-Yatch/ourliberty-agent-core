#!/usr/bin/env python3
"""Tests for scripts/wait_for_pid.sh — the safe "wait on a backgrounded PID"
primitive.

Regression coverage for the two poll-loop wedges that each held a fleet slot +
a live Opus session and blocked inbox-watcher:
  - PR #101 (2026-05-25): self-matching `pgrep -f`.
  - PR #334 (2026-06-05): empty `pgrep` → `/proc/$()` collapses to `/proc/`
    (always a directory) → `until` loop never exits.

The helper closes both by (1) gating liveness solely on `kill -0` of a PID
captured once, and (2) a hard wall-clock timeout that fails loudly (exit 124)
instead of wedging. The empty-pid guard (exit 2) is the direct regression for
the empty-substitution footgun: a bad `$(pgrep …)` now fails fast rather than
spinning forever.

Run:
    python3 -m unittest scripts.tests.test_wait_for_pid
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WAIT_FOR_PID = _REPO_ROOT / 'scripts' / 'wait_for_pid.sh'

# Exit-code contract (mirrors the script header).
EXIT_OK = 0
EXIT_TIMEOUT = 124
EXIT_USAGE = 2


def _run(*args: str, hard_timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Invoke wait_for_pid.sh with args. `hard_timeout` is a test-harness
    safety net (NOT the script's --timeout): if the script itself ever wedged,
    this raises rather than hanging the suite."""
    return subprocess.run(
        ['bash', str(_WAIT_FOR_PID), *args],
        capture_output=True, text=True, timeout=hard_timeout,
    )


class WaitForPidTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_WAIT_FOR_PID.exists(), f'missing helper: {_WAIT_FOR_PID}')
        self._spawned: list[int] = []

    def tearDown(self):
        for pid in self._spawned:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _spawn(self, seconds: float) -> int:
        """Background a `sleep` DETACHED (parent bash exits immediately, so the
        sleep reparents to init and is reaped on exit). This mirrors how a real
        harness backgrounds a job with `&` — and, unlike a Popen the test never
        wait()s, leaves no zombie that `kill -0` would falsely see as alive."""
        out = subprocess.run(
            ['bash', '-c', f'sleep {seconds} >/dev/null 2>&1 & echo $!'],
            capture_output=True, text=True, timeout=5,
        )
        pid = int(out.stdout.strip())
        self._spawned.append(pid)
        return pid

    def test_returns_zero_when_process_exits(self):
        """A live PID that exits while we wait → exit 0 promptly."""
        pid = self._spawn(1)
        start = time.monotonic()
        result = _run(str(pid), '--interval', '1', '--timeout', '15')
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        # Must observe the exit, not ride the timeout out.
        self.assertLess(elapsed, 10, 'should return shortly after the process exits')

    def test_timeout_fails_loudly_when_process_stays_alive(self):
        """A PID that outlives the wall-clock ceiling → exit 124 + loud stderr,
        NOT an indefinite wedge. This is the backstop that PR #101 / #334
        lacked."""
        pid = self._spawn(30)
        result = _run(str(pid), '--timeout', '1', '--interval', '1')
        self.assertEqual(result.returncode, EXIT_TIMEOUT, result.stderr)
        self.assertIn('TIMEOUT', result.stderr)

    def test_already_dead_pid_returns_zero_immediately(self):
        """A PID that is not alive (already reaped / never existed) is 'done' →
        exit 0 without polling. Gated on `kill -0`, so no /proc path can keep
        it falsely 'alive'."""
        # A very high PID that is essentially never live on a test host.
        result = _run('2147483640', '--timeout', '5', '--interval', '1')
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)

    def test_empty_pid_fails_fast(self):
        """Empty pid arg — the empty-`$(pgrep …)`-substitution footgun — must
        fail loudly (exit 2), never spin. This is the PR #334 regression."""
        result = _run('')
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
        self.assertIn('pid must be a positive integer', result.stderr)

    def test_missing_pid_fails_fast(self):
        """No args at all (what `wait_for_pid.sh $(pgrep …)` becomes when pgrep
        matches nothing) → exit 2, not a hang."""
        result = _run()
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)

    def test_non_numeric_pid_fails_fast(self):
        result = _run('not-a-pid')
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)

    def test_bad_timeout_value_fails_fast(self):
        result = _run('123', '--timeout', 'xyz')
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)

    def test_unknown_option_fails_fast(self):
        result = _run('123', '--bogus')
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)


if __name__ == '__main__':
    unittest.main()
