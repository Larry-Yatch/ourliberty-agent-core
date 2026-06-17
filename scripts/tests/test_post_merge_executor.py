#!/usr/bin/env python3
"""Unit tests for the projects-v3 P4 Contract B post_merge executor + the
Contract B schema check in build_sequence_validator.

The executor (`sequence_shortcut_helpers.execute_post_merge`) is pure: it takes
a sequence dict, an injectable command runner, and an injectable propose
callback, and returns a PostMergeReport. It classifies each finish-step:

  - `verify` (read-only)                 → auto-run.
  - `run` marked `safe: true`            → auto-run.
  - plain-string `run` + every `restart` → gated (proposed, NEVER executed).

Mirror focus: never auto-exec risky/irreversible (gated); fail-safe-only
auto-run; build never blocked on a failure. These tests prove all three at the
unit level without touching the filesystem or shelling out.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_post_merge_executor
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import build_sequence_validator as bsv     # noqa: E402
import sequence_shortcut_helpers as ssh     # noqa: E402


def _ok_runner(cmd):
    return True, f'ran: {cmd}'


class ClassificationTest(unittest.TestCase):
    def test_verify_is_auto(self):
        self.assertEqual(ssh._classify_post_merge_step('verify', False), 'auto')

    def test_safe_run_is_auto(self):
        self.assertEqual(ssh._classify_post_merge_step('run', True), 'auto')

    def test_plain_run_is_gated(self):
        self.assertEqual(ssh._classify_post_merge_step('run', False), 'gated')

    def test_restart_is_always_gated(self):
        # Even if some future caller passed is_safe=True, restart is gated.
        self.assertEqual(ssh._classify_post_merge_step('restart', True), 'gated')


class ExecutorTest(unittest.TestCase):
    def test_auto_runs_verify_and_safe_run(self):
        seq = {'seq_id': 's1', 'post_merge': {
            'verify': ['probe-a'],
            'run': [{'cmd': 'drain --apply', 'safe': True}],
        }}
        report = ssh.execute_post_merge(seq, runner=_ok_runner)
        self.assertEqual(len(report.results), 2)
        self.assertTrue(all(r.executed for r in report.results))
        self.assertTrue(all(r.ok for r in report.results))
        self.assertEqual({r.command for r in report.auto_results},
                         {'probe-a', 'drain --apply'})

    def test_gated_steps_are_not_executed(self):
        seq = {'seq_id': 's2', 'post_merge': {
            'restart': ['svc.service'],
            'run': ['risky.py'],
        }}

        def boom(cmd):
            raise AssertionError(f'gated step executed: {cmd}')

        proposed = []
        report = ssh.execute_post_merge(
            seq, runner=boom,
            propose_gated=lambda s, k, c: proposed.append((k, c)) or f'tap:{c}',
        )
        self.assertEqual(len(report.gated_results), 2)
        self.assertTrue(all(not r.executed for r in report.gated_results))
        self.assertTrue(all(r.ok is None for r in report.gated_results))
        self.assertEqual(set(proposed),
                         {('restart', 'svc.service'), ('run', 'risky.py')})
        # The propose-callback return rides into the result detail.
        self.assertIn('tap:svc.service',
                      [r.detail for r in report.gated_results])

    def test_runner_failure_is_captured_not_raised(self):
        seq = {'seq_id': 's3', 'post_merge': {'verify': ['probe']}}
        report = ssh.execute_post_merge(
            seq, runner=lambda c: (False, 'exit 1'),
        )
        self.assertEqual(len(report.results), 1)
        self.assertFalse(report.results[0].ok)
        self.assertEqual(len(report.verify_failures), 1)

    def test_runner_exception_never_propagates(self):
        seq = {'seq_id': 's4', 'post_merge': {'verify': ['probe']}}

        def raiser(cmd):
            raise RuntimeError('kaboom')

        report = ssh.execute_post_merge(seq, runner=raiser)  # must not raise
        self.assertFalse(report.results[0].ok)
        self.assertIn('kaboom', report.results[0].detail)

    def test_propose_exception_never_propagates(self):
        seq = {'seq_id': 's5', 'post_merge': {'restart': ['svc']}}

        def bad_propose(s, k, c):
            raise RuntimeError('propose-fail')

        report = ssh.execute_post_merge(
            seq, runner=_ok_runner, propose_gated=bad_propose,
        )
        self.assertEqual(len(report.gated_results), 1)
        self.assertIn('propose failed', report.gated_results[0].detail)

    def test_no_post_merge_block_is_empty_report(self):
        report = ssh.execute_post_merge({'seq_id': 's6'}, runner=_ok_runner)
        self.assertFalse(report.has_steps)
        self.assertEqual(report.results, [])

    def test_report_order_verify_then_run_then_restart(self):
        seq = {'seq_id': 's7', 'post_merge': {
            'restart': ['svc'],
            'run': [{'cmd': 'r', 'safe': True}],
            'verify': ['v'],
        }}
        report = ssh.execute_post_merge(
            seq, runner=_ok_runner, propose_gated=lambda *a: 'tap',
        )
        self.assertEqual([r.kind for r in report.results],
                         ['verify', 'run', 'restart'])

    def test_real_runner_runs_safe_command(self):
        # End-to-end through the real subprocess runner: a trivially-true
        # command auto-runs and reports ok. Proves shell=False execution.
        seq = {'seq_id': 's8', 'post_merge': {
            'verify': [f'{sys.executable} -c "print(123)"'],
        }}
        report = ssh.execute_post_merge(seq)
        self.assertTrue(report.results[0].ok)
        self.assertIn('123', report.results[0].detail)

    def test_real_runner_captures_nonzero_exit(self):
        seq = {'seq_id': 's9', 'post_merge': {
            'verify': [f'{sys.executable} -c "import sys; sys.exit(3)"'],
        }}
        report = ssh.execute_post_merge(seq)
        self.assertFalse(report.results[0].ok)
        self.assertIn('exit 3', report.results[0].detail)


class ValidatorPostMergeTest(unittest.TestCase):
    def _base(self):
        return {
            'seq_id': 'v', 'label': 'L', 'spec_doc': 'd',
            'created_at': 't', 'created_by': 'beacon', 'status': 'active',
            'current_steps': [], 'audit_log': [],
            'steps': [{
                'step_id': 'a', 'label': 'A', 'depends_on': [],
                'dispatch_text': 'x', 'target_repo': 'ourliberty-agent-core',
                'task_type': 'feature-development', 'status': 'pending',
                'dispatched_at': None, 'merged_at': None, 'pr_url': None,
                'current_actor': None, 'failure_reason': None,
            }],
        }

    def test_absent_post_merge_is_valid(self):
        self.assertTrue(bsv.validate_dag(self._base()).valid)

    def test_well_formed_post_merge_is_valid(self):
        seq = self._base()
        seq['post_merge'] = {
            'restart': ['svc.service'],
            'run': ['plain.py', {'cmd': 'safe.py', 'safe': True}],
            'verify': ['probe.py'],
        }
        res = bsv.validate_dag(seq)
        self.assertTrue(res.valid, res.errors)

    def test_post_merge_must_be_dict(self):
        seq = self._base()
        seq['post_merge'] = ['nope']
        res = bsv.validate_dag(seq)
        self.assertFalse(res.valid)
        self.assertTrue(any('post_merge must be a dict' in e
                            for e in res.errors))

    def test_run_list_must_be_list(self):
        seq = self._base()
        seq['post_merge'] = {'run': 'not-a-list'}
        res = bsv.validate_dag(seq)
        self.assertFalse(res.valid)
        self.assertTrue(any('post_merge.run must be a list' in e
                            for e in res.errors))

    def test_run_object_requires_cmd(self):
        seq = self._base()
        seq['post_merge'] = {'run': [{'safe': True}]}
        res = bsv.validate_dag(seq)
        self.assertFalse(res.valid)
        self.assertTrue(any('post_merge.run[0].cmd' in e for e in res.errors))

    def test_run_object_safe_must_be_bool(self):
        seq = self._base()
        seq['post_merge'] = {'run': [{'cmd': 'x', 'safe': 'yes'}]}
        res = bsv.validate_dag(seq)
        self.assertFalse(res.valid)
        self.assertTrue(any('post_merge.run[0].safe' in e for e in res.errors))

    def test_restart_entry_must_be_nonempty_string(self):
        seq = self._base()
        seq['post_merge'] = {'restart': ['']}
        res = bsv.validate_dag(seq)
        self.assertFalse(res.valid)
        self.assertTrue(any('post_merge.restart[0]' in e for e in res.errors))


if __name__ == '__main__':
    unittest.main()
