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
from unittest import mock
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RUN_REVIEW_STEP = _REPO_ROOT / 'scripts' / 'run_review_step.sh'

# Exit-code contract (mirrors the script header).
EXIT_OK = 0
EXIT_TIMEOUT = 124
EXIT_USAGE = 2


def _run(
    *args: str, hard_timeout: float = 30.0, env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Invoke run_review_step.sh with args. `hard_timeout` is a test-harness
    safety net (NOT the script's --timeout): if the script itself ever wedged —
    the exact failure this helper exists to prevent — this raises rather than
    hanging the suite. `env`, when given, is merged over the current environment
    (used to set OL_REVIEW_STEP_KILL_GRACE_SECONDS so the TERM-ignoring test
    escalates on a short grace instead of the 5s production default)."""
    return subprocess.run(
        ['bash', str(_RUN_REVIEW_STEP), *args],
        capture_output=True, text=True, timeout=hard_timeout,
        env={**os.environ, **env} if env else None,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_cmdline(pid: int) -> str:
    """Best-effort command line for `pid`, '' if it cannot be read.

    Linux first (`/proc`, no subprocess — this runs in a poll loop), then `ps`
    for macOS. A failure to read is '' , which the identity check below treats
    as "not our process" — the safe direction: we would rather call a dead
    sleeper dead than SIGKILL a stranger."""
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as fh:
            return fh.read().replace(b'\0', b' ').decode('utf-8', 'replace')
    except OSError:
        pass
    try:
        out = subprocess.run(
            ['ps', '-o', 'args=', '-p', str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ''


def _wait_until_reaped(pid: int, needle: str, timeout_s: float = 10.0) -> bool:
    """Poll until `pid` is gone (or is no longer OUR process). True == reaped.

    Replaces a fixed `time.sleep(0.3)` + single `_pid_alive` check. This test was
    the main-suite guardian's only `genuine-break` (runs 17-18) while passing in
    isolation, with `run_review_step.sh` untouched for weeks — a harness flake,
    not a regression in what it tests.

      1. TIMING — the actual cause. 300ms is plenty on an idle box and not always
         enough on a loaded one, where signal delivery and reaping lag behind a
         4-core droplet running 9000 tests. Measured directly: a process that
         exits at 1.0s reads as ALIVE at the old 0.3s checkpoint, so the old
         assertion fails on a kill that worked perfectly. Polling to a deadline
         is both faster in the common case (usually gone in ~1ms) and immune to
         load; the deadline is only ever paid by a genuine regression.

      2. PID REUSE — defensive, NOT what was failing here. If the sleeper's pid
         were recycled before the check, a bare `os.kill(pid, 0)` would report
         our long-dead sleeper as alive AND the caller's cleanup
         `os.kill(pid, SIGKILL)` would shoot whatever innocent process now holds
         it — plausibly another test's subprocess, turning one flake into
         someone else's mystery failure. On this droplet that is currently
         unreachable (`pid_max` 4194304 against ~2.8M in use, so pids do not wrap
         within a run), so it is hardening against a smaller `pid_max` or a
         longer-lived host rather than a live bug. It costs one `/proc` read per
         poll and removes the only path by which this test could damage another.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if not _pid_alive(pid):
            return True
        if needle not in _proc_cmdline(pid):
            return True  # pid recycled — ours is gone, and this one is NOT ours
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _spawn_sleeper(case: unittest.TestCase, seconds: int) -> subprocess.Popen:
    """Spawn `sleep <seconds>` and return it only once it IS that process.

    Between `Popen` returning and the child completing `execve`, Linux reports
    an EMPTY `/proc/<pid>/cmdline` for a live pid. `_wait_until_reaped` reads an
    empty command line as "pid recycled, this one is NOT ours" and returns True
    on its FIRST poll — so a test that spawns a sleeper and immediately asserts
    NOT-reaped fails for a reason that has nothing to do with the helper.

    Measured on the droplet, otherwise idle: time-to-exec is p50 0.10ms / p95
    1.24ms / max 3.16ms, and the first read lands inside that window in 1 of 60
    spawns from a fresh interpreter (this test's own shape) and 66 of 200 when
    spawns run back-to-back — the shape a full suite puts on the spawn path.

    EVIDENCE, stated honestly. This is the only mechanism found that produces
    the 2026-07-31 droplet failure (this `assertFalse`, during a full-suite run
    at PR #1079's head, passing 3/3 in isolation at that head and at base main),
    and it is real on this box — but it was NOT reproduced end-to-end: 100
    loaded runs of the test and 40 loaded 4-way-concurrent runs of this whole
    file, on the unfixed code, all passed.
    So it is a proven-possible cause at a rate below what those runs could
    catch, not a proven-actual one. Two things follow: the gate is worth having
    because it removes the mechanism for free, and the assertion below carries
    a diagnostic message so a recurrence names its own cause instead of costing
    another investigation.

    Note that raising `timeout_s` cannot fix this class — the wrong answer is
    returned before the deadline is ever consulted — and that the same race
    makes the recycled-pid test pass VACUOUSLY, True for the empty-cmdline
    reason instead of the mismatch it means to prove.

    Gating on exec having completed makes the assertion that follows about the
    helper's logic and nothing else."""
    p = subprocess.Popen(['sleep', str(seconds)])
    case.addCleanup(p.wait)
    case.addCleanup(p.kill)
    needle = f'sleep {seconds}'
    deadline = time.monotonic() + 10.0
    while needle not in _proc_cmdline(p.pid):
        if not _pid_alive(p.pid):
            raise AssertionError(f'sleeper {p.pid} died before it exec\'d {needle!r}')
        if time.monotonic() >= deadline:
            raise AssertionError(
                f'sleeper {p.pid} never exec\'d {needle!r} '
                f'(command line: {_proc_cmdline(p.pid)!r})')
        time.sleep(0.005)
    return p


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
        r = _run('--timeout', '1', '--interval', '1', '--label', 'regression check',
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
        r = _run('--timeout', '1', '--interval', '1',
                 '--', 'bash', '-c', 'sleep 30', hard_timeout=20.0)
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        self.assertNotIn('Terminated', r.stdout)

    def test_timeout_kills_the_child(self):
        # On timeout the step process itself must be dead, not orphaned.
        r = _run('--timeout', '1', '--interval', '1',
                 '--', 'bash', '-c', 'echo CHILD=$$; sleep 30', hard_timeout=20.0)
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        child_pid = None
        for line in r.stdout.splitlines():
            if line.startswith('CHILD='):
                child_pid = int(line.split('=', 1)[1])
                break
        self.assertIsNotNone(child_pid, 'did not capture child pid from output')
        self.assertTrue(
            _wait_until_reaped(child_pid, 'sleep 30'),
            f'child {child_pid} lingered after timeout')

    def test_timeout_kills_the_process_group(self):
        # A timed-out step's descendants (e.g. pytest workers) must die too —
        # the group kill, not just the direct child. The command spawns a
        # detached-looking sleeper and prints its pid.
        r = _run('--timeout', '1', '--interval', '1',
                 '--', 'bash', '-c', 'sleep 40 & echo GRANDCHILD=$!; wait',
                 hard_timeout=20.0)
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        gc_pid = None
        for line in r.stdout.splitlines():
            if line.startswith('GRANDCHILD='):
                gc_pid = int(line.split('=', 1)[1])
                break
        self.assertIsNotNone(gc_pid, 'did not capture grandchild pid from output')
        reaped = _wait_until_reaped(gc_pid, 'sleep 40')
        if not reaped:  # don't leak a 40s sleeper if the assertion is about to fail
            # Safe to signal: _wait_until_reaped only reports NOT-reaped while the
            # pid still matches our sleeper's command line, so this can never hit
            # a recycled pid belonging to another test.
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertTrue(
            reaped, f'grandchild {gc_pid} survived the process-group kill')

    def test_term_ignoring_step_still_killed_within_grace(self):
        # A step that traps SIGTERM must still die at the SIGKILL escalation —
        # the wait stays bounded by ceiling + grace, never the command runtime.
        # Use a short grace (via env) so the escalation still runs but the test
        # doesn't pay the full 5s production grace on every suite run.
        start = time.monotonic()
        r = _run('--timeout', '1', '--interval', '1',
                 '--', 'bash', '-c', 'trap "" TERM; sleep 60', hard_timeout=25.0,
                 env={'OL_REVIEW_STEP_KILL_GRACE_SECONDS': '2'})
        elapsed = time.monotonic() - start
        self.assertEqual(r.returncode, EXIT_TIMEOUT)
        # 1s ceiling + 2s grace + slack; nowhere near the 60s the step wanted.
        # (Still exercises the full SIGTERM-ignored → SIGKILL escalation path.)
        self.assertLess(elapsed, 20.0)


class WaitUntilReapedTest(unittest.TestCase):
    """Coverage for the liveness helper itself.

    The timeout tests above were the guardian's only `genuine-break` on main
    (runs 17-18) while passing in isolation and with `run_review_step.sh`
    untouched for weeks — i.e. a harness flake, not a regression. Fixing a flake
    without covering the fix just moves the blind spot, so the two properties
    that make it robust are pinned here."""

    def test_dead_pid_is_reaped_immediately(self):
        p = subprocess.Popen(['sleep', '0'])
        p.wait()
        start = time.monotonic()
        self.assertTrue(_wait_until_reaped(p.pid, 'sleep 0', timeout_s=5.0))
        # Returns on the first poll — it must not burn the whole deadline.
        self.assertLess(time.monotonic() - start, 1.0)

    def test_recycled_pid_is_not_mistaken_for_ours(self):
        # THE dangerous case. A live pid whose command line is not ours means our
        # process died and the pid was reused. Reporting it alive would be a false
        # failure AND would send the caller's cleanup SIGKILL at a stranger.
        # Gated on exec: an un-exec'd sleeper reads as an empty command line and
        # would pass this for the wrong reason (see `_spawn_sleeper`).
        p = _spawn_sleeper(self, 30)
        # Same live pid, but we are looking for a different command.
        self.assertTrue(_wait_until_reaped(p.pid, 'sleep 40', timeout_s=5.0))
        # ...and it is genuinely still running: we reported "reaped" without
        # touching it, which is the whole point.
        self.assertTrue(_pid_alive(p.pid))

    def test_live_matching_process_is_not_reaped(self):
        # The assertion the timeout tests actually rely on must still be able to
        # FAIL — a helper that always returns True would make them vacuous.
        p = _spawn_sleeper(self, 30)
        # 0.5s stays deliberately short: once exec is confirmed, a live
        # `sleep 30` cannot stop being alive or stop matching, so the helper is
        # forced to burn the whole deadline and answer False. Load changes how
        # long that costs, never the answer — so a longer deadline would buy no
        # robustness, only a slower suite.
        reaped = _wait_until_reaped(p.pid, 'sleep 30', timeout_s=0.5)
        # The message is the point if this ever fires again: `alive=True` with an
        # empty command line means the exec race got past the gate, while
        # `alive=False` means something outside this test killed our sleeper —
        # a different bug entirely, and one no timeout tuning would touch.
        self.assertFalse(
            reaped,
            f'live sleeper {p.pid} reported reaped: '
            f'alive={_pid_alive(p.pid)} cmdline={_proc_cmdline(p.pid)!r}')

    def test_spawn_sleeper_absorbs_the_exec_window(self):
        # Pins the fix rather than the symptom: the exec gate must SWALLOW empty
        # command lines, not merely get lucky about them. The window is forced
        # here instead of waited for — the first three reads come back empty,
        # exactly as a mid-`execve` child does — because a race that shows up in
        # ~2% of real spawns cannot be pinned by running the real thing once.
        # Without the gate, `_wait_until_reaped` answers True on poll one and
        # the assertion below inverts; that is the flake, on demand.
        real = _proc_cmdline
        calls = []

        def racy(pid):
            calls.append(pid)
            return '' if len(calls) <= 3 else real(pid)

        with mock.patch.dict(globals(), {'_proc_cmdline': racy}):
            p = _spawn_sleeper(self, 30)
            self.assertGreater(
                len(calls), 3, 'gate returned while the command line still read empty')
            self.assertFalse(_wait_until_reaped(p.pid, 'sleep 30', timeout_s=0.2))

    def test_cmdline_of_dead_pid_is_empty_not_an_exception(self):
        p = subprocess.Popen(['sleep', '0'])
        p.wait()
        self.assertEqual(_proc_cmdline(p.pid).strip(), '')


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
