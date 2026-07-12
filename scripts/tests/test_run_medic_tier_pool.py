#!/usr/bin/env python3
"""Tests for run_medic.sh's tier-pool dispatch wiring (mirror of
test_run_cycle_tier_pool — see that module's docstring for the why).

Each Medic tick now dispatches through active_tier.py's ``select-dispatch-env``
verb ({tier1,tier3} round-robin) instead of riding the single active tier,
scopes the env delta to the claude subshell, stamps the cost row with the
selected tier, benches a walled tier via ``report-dispatch-failure``, and
falls back to the legacy active-setup-token path on any selection failure.

Harness: tmpdir HOME + stub `claude`/`active_tier.py`, modeled on
test_run_medic_timeout.TimeoutKillsAndReleasesLockTest (the script runs from
the real repo path; only $HOME and PATH are faked).

Run:
    python3 -m unittest scripts.tests.test_run_medic_tier_pool
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:  # dotted/pytest path: relative import within the scripts.tests package
    from ._runtime_script_test_support import install_timeout_shim
except ImportError:  # discover loads this module top-level (no package parent)
    from _runtime_script_test_support import install_timeout_shim

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RUN_MEDIC = _REPO_ROOT / 'scripts' / 'run_medic.sh'

_HAS_JQ = shutil.which('jq') is not None

_CLAUDE_ENV_PROBE_STUB = '''#!/usr/bin/env bash
{{
  echo "TOKEN=${{CLAUDE_CODE_OAUTH_TOKEN:-}}"
  echo "HOME=${{HOME:-}}"
}} > "{probe}"
echo '{{"total_cost_usd": 0.1, "duration_ms": 500, "usage": {{"input_tokens": 5, "output_tokens": 7}}}}'
exit {exit_code}
'''

_ACTIVE_TIER_STUB = '''#!/usr/bin/env python3
import sys
with open({calls!r}, 'a') as f:
    f.write(' '.join(sys.argv[1:]) + '\\n')
verb = sys.argv[1] if len(sys.argv) > 1 else ''
if verb == 'select-dispatch-env':
    sys.stdout.write({select_out!r})
    sys.exit({select_code})
if verb == 'active-setup-token':
    sys.stdout.write({legacy_token!r})
    sys.exit(0)
if verb == 'report-dispatch-failure':
    sys.stdout.write({report_out!r})
    sys.exit(0)
sys.exit(2)
'''


class _MedicTierPoolBase(unittest.TestCase):
    def setUp(self):
        if shutil.which('bash') is None:
            raise unittest.SkipTest('bash not on PATH')
        self._tmpdir = Path(tempfile.mkdtemp(prefix='run-medic-tier-pool-'))
        self.addCleanup(shutil.rmtree, self._tmpdir, ignore_errors=True)
        self.fake_home = self._tmpdir / 'home'
        (self.fake_home / 'agents' / 'state').mkdir(parents=True)
        (self.fake_home / 'agents' / 'logs').mkdir(parents=True)
        (self.fake_home / 'agents' / 'blackboard').mkdir(parents=True)
        self.medic_dir = self.fake_home / 'agent-core' / 'agents' / 'medic'
        self.medic_dir.mkdir(parents=True)
        self.scripts_dir = self.fake_home / 'agent-core' / 'scripts'
        self.scripts_dir.mkdir(parents=True)
        self.batch_path = self._tmpdir / 'batch.json'
        self.batch_path.write_text(json.dumps({'alerts': []}))
        self.probe = self._tmpdir / 'claude-env-probe'
        self.calls = self._tmpdir / 'active-tier-calls.log'
        self.fake_bin = self._tmpdir / 'bin'
        self.fake_bin.mkdir()
        install_timeout_shim(self.fake_bin)

    def _install_active_tier_stub(self, select_out='', select_code=3,
                                  legacy_token='', report_out=''):
        stub = self.scripts_dir / 'active_tier.py'
        stub.write_text(_ACTIVE_TIER_STUB.format(
            calls=str(self.calls), select_out=select_out,
            select_code=select_code, legacy_token=legacy_token,
            report_out=report_out))
        os.chmod(stub, 0o755)

    def _install_claude_stub(self, exit_code=0):
        stub = self.fake_bin / 'claude'
        stub.write_text(_CLAUDE_ENV_PROBE_STUB.format(
            probe=str(self.probe), exit_code=exit_code))
        os.chmod(stub, 0o755)

    def _run(self):
        env = os.environ.copy()
        env['HOME'] = str(self.fake_home)
        env['PATH'] = f'{self.fake_bin}:{env.get("PATH", "")}'
        return subprocess.run(
            ['bash', str(_RUN_MEDIC), str(self.batch_path)],
            env=env, capture_output=True, text=True, timeout=30)

    def _read_probe(self):
        pairs = {}
        for line in self.probe.read_text().splitlines():
            key, _, value = line.partition('=')
            pairs[key] = value
        return pairs

    def _read_calls(self):
        if not self.calls.exists():
            return []
        return [ln for ln in self.calls.read_text().splitlines() if ln]

    def _read_cost_rows(self):
        costs = self.fake_home / 'agents' / 'blackboard' / 'costs.jsonl'
        if not costs.exists():
            return []
        return [json.loads(ln)
                for ln in costs.read_text().splitlines() if ln.strip()]


class MedicPoolDispatchTest(_MedicTierPoolBase):
    def test_pool_selected_tier_env_and_cost_stamp(self):
        self._install_active_tier_stub(
            select_out='TIER=tier3\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-t3\n',
            select_code=0)
        self._install_claude_stub(exit_code=0)

        result = self._run()

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr[:500]}')
        probe = self._read_probe()
        self.assertEqual(probe['TOKEN'], 'sk-ant-oat01-t3')
        self.assertEqual(probe['HOME'], str(self.fake_home))
        self.assertFalse(any('active-setup-token' in c
                             for c in self._read_calls()))
        if _HAS_JQ:
            rows = self._read_cost_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['account'], 'tier3')
            self.assertEqual(rows[0]['agent'], 'medic')

    def test_failed_tick_reports_dispatch_failure(self):
        self._install_active_tier_stub(
            select_out='TIER=tier1\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-t1\n',
            select_code=0, report_out='rate_limit')
        self._install_claude_stub(exit_code=1)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        report_calls = [c for c in self._read_calls()
                        if c.startswith('report-dispatch-failure')]
        self.assertEqual(len(report_calls), 1)
        parts = report_calls[0].split()
        self.assertEqual(parts[1:3], ['tier1', 'medic'])
        self.assertTrue(parts[3].endswith('medic.last-output.json'))

    def test_selection_unavailable_falls_back_to_legacy_token(self):
        self._install_active_tier_stub(select_code=3,
                                       legacy_token='sk-ant-oat01-legacy')
        self._install_claude_stub(exit_code=0)

        result = self._run()

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr[:500]}')
        self.assertEqual(self._read_probe()['TOKEN'], 'sk-ant-oat01-legacy')
        self.assertFalse(any(c.startswith('report-dispatch-failure')
                             for c in self._read_calls()))


if __name__ == '__main__':
    unittest.main()
