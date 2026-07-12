#!/usr/bin/env python3
"""Tests for run_cycle.sh's tier-pool dispatch wiring.

Pre-pool, run_cycle.sh rode the single ACTIVE tier (active-setup-token), which
concentrated the cycle's burn — the dominant burn source on the droplet — on
tier1 while tier3 idled. The wiring under test dispatches each /cycle run
through active_tier.py's ``select-dispatch-env`` verb (the {tier1,tier3}
round-robin pool), scopes the returned env delta to the claude subshell only,
stamps the cost row with the SELECTED tier, benches a walled tier via
``report-dispatch-failure``, and falls back to the legacy active-setup-token
path on any selection failure.

Reuses the subprocess harness from test_run_cycle_wrong_branch_guard
(_RunCycleTestBase): a tmpdir-rooted fake agent-core + bare origin + stub
`claude` on PATH. Each test additionally installs a stub
scripts/active_tier.py whose behavior drives the path under test; the stub
records its argv invocations to a call log for assertions.

Run:
    python3 -m unittest scripts.tests.test_run_cycle_tier_pool
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import shutil
import unittest

try:  # dotted/pytest path: relative import within the scripts.tests package
    from .test_run_cycle_wrong_branch_guard import _RunCycleTestBase
except ImportError:  # discover loads this module top-level (no package parent)
    from test_run_cycle_wrong_branch_guard import _RunCycleTestBase

_HAS_JQ = shutil.which('jq') is not None

# Stub claude: probe the env the wrapper hands the child (token + HOME), emit
# a cost-parseable JSON body. The probe path is absolute (baked in per-test)
# so a HOME override in the child can't move it.
_CLAUDE_ENV_PROBE_STUB = '''#!/usr/bin/env bash
{{
  echo "TOKEN=${{CLAUDE_CODE_OAUTH_TOKEN:-}}"
  echo "HOME=${{HOME:-}}"
}} > "{probe}"
echo '{{"total_cost_usd": 0.5, "duration_ms": 1000, "usage": {{"input_tokens": 10, "output_tokens": 20}}}}'
exit {exit_code}
'''

# Stub active_tier.py CLI. Behavior per verb is baked in per-test via format
# fields; every invocation appends its argv to the call log.
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


class _TierPoolBase(_RunCycleTestBase):
    def setUp(self):
        super().setUp()
        self.probe = self.tmp_path / 'claude-env-probe'
        self.calls = self.tmp_path / 'active-tier-calls.log'

    def _install_active_tier_stub(self, select_out='', select_code=3,
                                  legacy_token='', report_out=''):
        stub = self.scripts_dir / 'active_tier.py'
        stub.write_text(_ACTIVE_TIER_STUB.format(
            calls=str(self.calls), select_out=select_out,
            select_code=select_code, legacy_token=legacy_token,
            report_out=report_out))
        os.chmod(stub, 0o755)

    def _install_claude_stub(self, exit_code=0):
        stub = self.stub_bin / 'claude'
        stub.write_text(_CLAUDE_ENV_PROBE_STUB.format(
            probe=str(self.probe), exit_code=exit_code))
        os.chmod(stub, 0o755)

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
        costs = self.home / 'agents' / 'blackboard' / 'costs.jsonl'
        if not costs.exists():
            return []
        return [json.loads(ln)
                for ln in costs.read_text().splitlines() if ln.strip()]


class PoolDispatchTest(_TierPoolBase):
    """Pool selects tier3 → claude runs with tier3's token, the wrapper's own
    HOME is untouched, and the cost row is stamped account=tier3."""

    def test_pool_selected_tier_env_and_cost_stamp(self):
        self._install_active_tier_stub(
            select_out='TIER=tier3\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-t3\n',
            select_code=0)
        self._install_claude_stub(exit_code=0)

        result = self._run_cycle()

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr.decode()[:500]}')
        probe = self._read_probe()
        self.assertEqual(probe['TOKEN'], 'sk-ant-oat01-t3')
        # No HOME line in the delta → the child keeps the wrapper's HOME.
        self.assertEqual(probe['HOME'], str(self.home))
        # Selection was consulted; the legacy verb was NOT (pool path).
        calls = self._read_calls()
        self.assertIn('select-dispatch-env', calls[0])
        self.assertFalse(any('active-setup-token' in c for c in calls))
        if _HAS_JQ:
            rows = self._read_cost_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['account'], 'tier3')
            self.assertTrue(rows[0]['success'])

    def test_home_line_is_never_exported_token_only_whitelist(self):
        # Defense in depth: even if a stray HOME= line reached the wrapper, the
        # token-only export whitelist must ignore it — a wrapper must never
        # swap HOME (the units' ProtectHome mount can't satisfy it; the child
        # would EROFS on ~/.claude.json). The child keeps the wrapper's HOME.
        alt_home = self.tmp_path / 'alt-home'
        alt_home.mkdir()
        self._install_active_tier_stub(
            select_out=(f'TIER=tier1\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-t1\n'
                        f'HOME={alt_home}\n'),
            select_code=0)
        self._install_claude_stub(exit_code=0)

        result = self._run_cycle()

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr.decode()[:500]}')
        probe = self._read_probe()
        self.assertEqual(probe['TOKEN'], 'sk-ant-oat01-t1')
        # HOME line ignored — child ran under the wrapper's own home.
        self.assertEqual(probe['HOME'], str(self.home))
        self.assertTrue((self.home / 'agents' / 'logs' / 'cycle.log').exists())
        if _HAS_JQ:
            rows = self._read_cost_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['account'], 'tier1')


class WallBenchingTest(_TierPoolBase):
    """A failed pool-dispatched run reports the output for classification so
    a walled tier benches before the next fire."""

    def test_failed_run_reports_dispatch_failure(self):
        self._install_active_tier_stub(
            select_out='TIER=tier1\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-t1\n',
            select_code=0, report_out='rate_limit')
        self._install_claude_stub(exit_code=1)

        result = self._run_cycle()

        self.assertEqual(result.returncode, 1)  # failed cycle still exits 1
        report_calls = [c for c in self._read_calls()
                        if c.startswith('report-dispatch-failure')]
        self.assertEqual(len(report_calls), 1)
        parts = report_calls[0].split()
        self.assertEqual(parts[1:3], ['tier1', 'pulse'])
        self.assertTrue(parts[3].endswith('cycle.last-output.json'))
        if _HAS_JQ:
            rows = self._read_cost_rows()
            self.assertEqual(rows[0]['account'], 'tier1')
            self.assertFalse(rows[0]['success'])

    def test_legacy_fallback_run_does_not_report(self):
        # No DISPATCH_TIER (selection unavailable) → nothing to bench.
        self._install_active_tier_stub(select_code=3, legacy_token='sk-legacy')
        self._install_claude_stub(exit_code=1)

        self._run_cycle()

        self.assertFalse(any(c.startswith('report-dispatch-failure')
                             for c in self._read_calls()))


class LegacyFallbackTest(_TierPoolBase):
    """Selection unavailable (exit 3 / missing CLI) → the legacy
    active-setup-token path keeps the heartbeat alive, byte-for-byte."""

    def test_falls_back_to_active_setup_token(self):
        self._install_active_tier_stub(select_code=3,
                                       legacy_token='sk-ant-oat01-legacy')
        self._install_claude_stub(exit_code=0)

        result = self._run_cycle()

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr.decode()[:500]}')
        self.assertEqual(self._read_probe()['TOKEN'], 'sk-ant-oat01-legacy')
        calls = self._read_calls()
        self.assertTrue(any('active-setup-token' in c for c in calls))
        if _HAS_JQ:
            # Legacy attribution path: active-tier.json absent → tier1.
            self.assertEqual(self._read_cost_rows()[0]['account'], 'tier1')

    def test_missing_cli_falls_back_to_credentials_json(self):
        # No active_tier.py at all (this harness's default): both verbs fail →
        # claude runs on the inherited env (credentials.json path), exactly
        # the pre-pool degraded behavior.
        self._install_claude_stub(exit_code=0)

        result = self._run_cycle()

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr.decode()[:500]}')
        self.assertEqual(self._read_probe()['TOKEN'], '')


if __name__ == '__main__':
    unittest.main()
