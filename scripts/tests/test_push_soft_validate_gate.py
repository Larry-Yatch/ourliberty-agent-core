#!/usr/bin/env python3
"""Tests for the soft (alert-only) deploy gate in _lib_push_with_rebase.sh.

Context: the sync-time validate gate (validate_agent_core.py via
sync_agent_core.sh) only fires when local HEAD is BEHIND origin/main. The
routine commit path is Pulse / Ledger auto-committing locally and pushing
through push_with_rebase, which bypasses that gate entirely. soft_validate_
main_before_push closes the observability gap by running the SAME validator
at push time and alerting Larry on failure — WITHOUT ever blocking the push
(a validator false-positive must never freeze the self-healing cycle).

These tests stand up an isolated repo + bare origin and stub two scripts the
gate shells out to:
  - validate_agent_core.py : configurable pass/fail via PUSH_GATE_STUB_RC.
  - larry_alerts.py        : records each append_alert invocation to a marker
                             file so the test can assert on alert behavior.

Covered contract:
  - validator FAILS  -> exactly one alert recorded AND the push still lands
    on origin (non-blocking is the core safety property).
  - same failure again -> no second alert (fingerprint dedup).
  - validator PASSES -> no alert, fingerprint state cleared, push lands.
  - PUSH_SOFT_VALIDATE=0 -> validator never invoked, push lands.

Test isolation: HOME / OURLIBERTY_AGENTS_ROOT are tmp_path-rooted; no writes
to ~/agents/. The real production _lib_push_with_rebase.sh is copied in.

Run:
    python3 -m unittest scripts.tests.test_push_soft_validate_gate
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LIB_PUSH = _REPO_ROOT / 'scripts' / '_lib_push_with_rebase.sh'


# Stub validator: exits with $PUSH_GATE_STUB_RC (default 0). On nonzero it
# prints validate_agent_core.py-shaped "  ERROR:" lines to stderr so the
# gate's fingerprint parser has something real to chew on.
_VALIDATE_STUB = '''#!/usr/bin/env python3
import os, sys
rc = int(os.environ.get("PUSH_GATE_STUB_RC", "0"))
if rc != 0:
    sys.stderr.write("  ERROR: agents/medic/SOUL.md is missing\\n")
    sys.stderr.write("  ERROR: stub-induced failure\\n")
sys.exit(rc)
'''

# Stub larry_alerts.py: append the subject of each append_alert call to the
# marker file named by PUSH_GATE_ALERT_MARKER. Ignores all other subcommands.
_ALERTS_STUB = '''#!/usr/bin/env python3
import os, sys
if len(sys.argv) >= 2 and sys.argv[1] == "append_alert":
    subj = ""
    for i, a in enumerate(sys.argv):
        if a == "--subject" and i + 1 < len(sys.argv):
            subj = sys.argv[i + 1]
    marker = os.environ.get("PUSH_GATE_ALERT_MARKER")
    if marker:
        with open(marker, "a") as f:
            f.write(subj + "\\n")
sys.exit(0)
'''


class SoftValidateGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / 'home'
        self.agents_root = self.home / 'agents'
        (self.agents_root / 'state').mkdir(parents=True)

        # Work repo with scripts/ holding the real lib + the two stubs.
        self.repo = self.home / 'agent-core'
        self.scripts = self.repo / 'scripts'
        self.scripts.mkdir(parents=True)
        shutil.copy2(_LIB_PUSH, self.scripts / _LIB_PUSH.name)
        (self.scripts / 'validate_agent_core.py').write_text(_VALIDATE_STUB)
        (self.scripts / 'larry_alerts.py').write_text(_ALERTS_STUB)

        self.log_file = self.tmp / 'push.log'
        self.alert_marker = self.tmp / 'alerts.txt'

        self._git(self.repo, 'init', '-q', '-b', 'main')
        self._git(self.repo, 'config', 'user.email', 'test@example.com')
        self._git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'README.md').write_text('seed\n')
        self._git(self.repo, 'add', '-A')
        self._git(self.repo, 'commit', '-q', '-m', 'seed')

        # Bare origin, seeded from the repo, tracked as origin/main.
        self.origin = self.tmp / 'origin.git'
        self._git(self.tmp, 'clone', '-q', '--bare', str(self.repo), str(self.origin))
        self._git(self.repo, 'remote', 'add', 'origin', str(self.origin))
        self._git(self.repo, 'push', '-q', '-u', 'origin', 'main')

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, cwd, *args):
        subprocess.run(
            ['git', *args], cwd=str(cwd), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _env(self, stub_rc, soft='1'):
        env = dict(os.environ)
        env['HOME'] = str(self.home)
        env['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        env['PUSH_GATE_STUB_RC'] = str(stub_rc)
        env['PUSH_GATE_ALERT_MARKER'] = str(self.alert_marker)
        env['PUSH_SOFT_VALIDATE'] = soft
        return env

    def _commit(self, name):
        (self.repo / name).write_text('x\n')
        self._git(self.repo, 'add', '-A')
        self._git(self.repo, 'commit', '-q', '-m', name)

    def _run_push(self, stub_rc, soft='1'):
        """Make a commit, drive push_with_rebase, return (rc, origin_head)."""
        lib = self.scripts / '_lib_push_with_rebase.sh'
        driver = (
            f'source "{lib}"; '
            f'push_with_rebase origin main "{self.log_file}"'
        )
        proc = subprocess.run(
            ['bash', '-c', driver], cwd=str(self.repo), env=self._env(stub_rc, soft),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        origin_head = subprocess.run(
            ['git', 'rev-parse', 'main'], cwd=str(self.origin),
            stdout=subprocess.PIPE, text=True, check=True,
        ).stdout.strip()
        return proc.returncode, origin_head

    def _local_head(self):
        return subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=str(self.repo),
            stdout=subprocess.PIPE, text=True, check=True,
        ).stdout.strip()

    def _alerts(self):
        if not self.alert_marker.exists():
            return []
        return [l for l in self.alert_marker.read_text().splitlines() if l]

    def test_failure_alerts_but_push_still_lands(self):
        self._commit('a.txt')
        rc, origin_head = self._run_push(stub_rc=2)
        # Non-blocking: push succeeded and origin advanced to local HEAD.
        self.assertEqual(rc, 0)
        self.assertEqual(origin_head, self._local_head())
        # Exactly one alert recorded.
        self.assertEqual(len(self._alerts()), 1, self._alerts())

    def test_same_failure_deduped(self):
        self._commit('a.txt')
        self._run_push(stub_rc=2)
        self._commit('b.txt')
        self._run_push(stub_rc=2)  # identical ERROR set -> same fingerprint
        self.assertEqual(len(self._alerts()), 1, self._alerts())

    def test_pass_no_alert_and_clears_state(self):
        # First fail to write a fingerprint, then pass to clear it.
        self._commit('a.txt')
        self._run_push(stub_rc=2)
        fp_file = self.agents_root / 'state' / 'push-validate-soft.fp'
        self.assertTrue(fp_file.exists())
        self._commit('b.txt')
        rc, origin_head = self._run_push(stub_rc=0)
        self.assertEqual(rc, 0)
        self.assertEqual(origin_head, self._local_head())
        self.assertFalse(fp_file.exists())
        self.assertEqual(len(self._alerts()), 1)  # no new alert on the pass

    def test_disabled_skips_validator(self):
        self._commit('a.txt')
        rc, origin_head = self._run_push(stub_rc=2, soft='0')
        self.assertEqual(rc, 0)
        self.assertEqual(origin_head, self._local_head())
        self.assertEqual(self._alerts(), [])  # gate off -> no validator, no alert


if __name__ == '__main__':
    unittest.main()
