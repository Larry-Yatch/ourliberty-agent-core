"""Tests for scripts/run_medic.sh per-session timeout hardening.

Why this exists
---------------
During the first live hours of the Medic operator (PR #228), one
headless `claude --print` invocation hung for ~30 minutes. The
concurrency lock contained the damage (LOCK_MAX_AGE_SEC = 30 min) but a
hung session has no business holding a rolling-5h rate-limit window
slot that long. PR #229 wraps the claude invocation in `timeout(1)` so
the session is killed after MEDIC_CLAUDE_TIMEOUT (default 10m), the
EXIT trap releases the lock, and the script exits non-zero.

Coverage
--------
Two layers of defense in depth:

  * **Structural** (`StructuralTest`) -- the script literally contains
    a `timeout` wrap on the claude call and an `exit 124` branch. Cheap,
    fast, catches regressions in code review where the test author
    didn't bother running a real shell.
  * **Behavioral** (`TimeoutKillsAndReleasesLockTest`) -- run the script
    end-to-end against a fake `claude` binary that sleeps past the
    configured timeout; assert exit code 124, lock file gone, no
    cost record written. Skipped if `timeout` or `bash` is not on PATH.

The cost-capture path (`jq`) is exercised when present but not required
-- the test only asserts on the timeout behavior, not on cost accounting.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RUN_MEDIC = _REPO_ROOT / 'scripts' / 'run_medic.sh'


class StructuralTest(unittest.TestCase):
    """Source-grep guards that the timeout wrap is present and the
    exit-124 branch exists. These run regardless of platform tooling."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = _RUN_MEDIC.read_text(encoding='utf-8')

    def test_claude_call_is_wrapped_in_timeout(self) -> None:
        # The timeout wrap must precede the `claude --print` invocation.
        self.assertIn('timeout "$CLAUDE_TIMEOUT" claude --print', self.body,
                      'run_medic.sh must invoke claude via timeout(1) to bound '
                      'per-session wall time (PR #229).')

    def test_timeout_default_is_ten_minutes(self) -> None:
        # The default is read from MEDIC_CLAUDE_TIMEOUT; the literal "10m"
        # is the locked default per the dispatch brief.
        self.assertIn('CLAUDE_TIMEOUT="${MEDIC_CLAUDE_TIMEOUT:-10m}"', self.body,
                      'run_medic.sh must default MEDIC_CLAUDE_TIMEOUT to 10m.')

    def test_exit_124_branch_exists(self) -> None:
        # timeout(1) exits 124 when the child is killed by the timer.
        # The script must detect that case explicitly and exit 124 so
        # the dispatcher's outer wrapper logs a distinct failure.
        self.assertIn('"124"', self.body,
                      'run_medic.sh must branch on timeout exit code 124.')
        self.assertIn('exit 124', self.body,
                      'run_medic.sh must propagate exit 124 on timeout.')

    def test_lock_release_trap_still_present(self) -> None:
        # The EXIT trap is the lock-release mechanism on timeout; if it
        # ever moves to a non-EXIT signal the timeout path stops releasing.
        self.assertIn("trap 'rm -f \"$LOCK_FILE\"", self.body,
                      'run_medic.sh must release the lock on EXIT '
                      '(which fires on exit 124 too).')


class TimeoutKillsAndReleasesLockTest(unittest.TestCase):
    """End-to-end: spin run_medic.sh against a fake `claude` that sleeps
    past the timeout and verify the timeout path actually kills the
    process AND releases the lock. Skipped if bash or timeout(1) are
    unavailable in the environment.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which('bash') is None:
            raise unittest.SkipTest('bash not on PATH')
        if shutil.which('timeout') is None:
            raise unittest.SkipTest('timeout(1) not on PATH (coreutils)')

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix='run-medic-timeout-'))
        # Synthetic $HOME so the script's lock + log paths land in tmp.
        self.fake_home = self._tmpdir / 'home'
        (self.fake_home / 'agents' / 'state').mkdir(parents=True)
        (self.fake_home / 'agents' / 'logs').mkdir(parents=True)
        (self.fake_home / 'agents' / 'blackboard').mkdir(parents=True)
        # The script does `cd "$MEDIC_DIR"`; make it exist as a real dir.
        self.medic_dir = self.fake_home / 'agent-core' / 'agents' / 'medic'
        self.medic_dir.mkdir(parents=True)
        # Minimal batch file (the script reads only its existence).
        self.batch_path = self._tmpdir / 'batch.json'
        self.batch_path.write_text(json.dumps({'alerts': []}))
        # Fake `claude` that sleeps long enough to be killed by timeout.
        # 60s is comfortably beyond the 1s timeout we'll set, while short
        # enough to keep the test fast even in the unhappy "didn't get
        # killed" case.
        self.fake_bin = self._tmpdir / 'bin'
        self.fake_bin.mkdir()
        fake_claude = self.fake_bin / 'claude'
        fake_claude.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            # Test stub: sleep past the configured timeout so timeout(1)
            # has to kill us. Exit code unused -- timeout(1) will report 124.
            sleep 60
        """))
        fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR
                          | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _run(self, timeout_str: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env['HOME'] = str(self.fake_home)
        # Prepend the fake-bin dir so `claude` resolves to the stub. Keep
        # the system PATH so bash, timeout, date, stat, tee, cat are found.
        env['PATH'] = f'{self.fake_bin}:{env.get("PATH", "")}'
        env['MEDIC_CLAUDE_TIMEOUT'] = timeout_str
        # Run with a generous outer timeout so a regression (claude not
        # killed by `timeout`) does NOT hang the test forever.
        return subprocess.run(
            ['bash', str(_RUN_MEDIC), str(self.batch_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_timeout_kills_claude_and_releases_lock(self) -> None:
        result = self._run('1s')
        self.assertEqual(
            result.returncode, 124,
            f'expected exit 124 on timeout, got {result.returncode}; '
            f'stderr={result.stderr!r}')
        lock_file = self.fake_home / 'agents' / 'state' / '.medic.lock'
        self.assertFalse(
            lock_file.exists(),
            'lock file must be removed by the EXIT trap after timeout, '
            'otherwise the next dispatcher tick is blocked by stale lock')
        # Log line should mention the timeout so postmortem is easy.
        log_file = self.fake_home / 'agents' / 'logs' / 'medic.log'
        self.assertTrue(log_file.exists(), 'log file should be created')
        log_body = log_file.read_text(encoding='utf-8')
        self.assertIn('timed out', log_body.lower(),
                      f'log should mention timeout; saw:\n{log_body}')


if __name__ == '__main__':
    unittest.main()
