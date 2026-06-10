#!/usr/bin/env python3
"""Tests for scripts/test_regression_check.py (task-26).

Covers:
  - Pure logic: failure-line parser, verdict computation, text renderer.
  - CLI / orchestration: main() exit codes for PASS / BLOCK / ANALYSIS_FAIL,
    timeout handling, malformed-output detection, current-HEAD optimization,
    JSON vs text rendering.

Subprocess invocations (git, python3 -m unittest) are mocked at the
subprocess.run boundary. No git or test runner is invoked live in this
suite.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_test_regression_check
"""
from __future__ import annotations

import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import test_regression_check as trc  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test.

    Mirrors the pattern from test_heal_pr_auto_merge.py — keeps the module
    under test from reading or writing prod /home/larry/agents/ state on
    import if it ever grows such behavior.
    """

    def setUp(self):
        super().setUp()
        self._isolated_tmp = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
            os.makedirs(os.path.join(self._isolated_tmp, sub), exist_ok=True)
        self._isolated_env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_tmp
        importlib.reload(trc)

    def tearDown(self):
        if self._isolated_env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_env_orig
        importlib.reload(trc)
        shutil.rmtree(self._isolated_tmp, ignore_errors=True)
        super().tearDown()


def _completed(stdout: str = '', stderr: str = '', returncode: int = 0,
               args=('python3', '-m', 'unittest')):
    return subprocess.CompletedProcess(args=list(args), returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _unittest_output(failures: list[tuple[str, str]] = None,
                     n_tests: int = 5) -> str:
    """Build a plausible unittest -v output blob.

    ``failures`` is a list of (test_method, fqclass) tuples. The "Ran N
    tests in X.Xs" sentinel is always included so the runner-aborted
    heuristic in run_tests_in_dir treats the output as well-formed.
    """
    lines = []
    failures = failures or []
    for method, fqclass in failures:
        lines.append(f'FAIL: {method} ({fqclass})')
    lines.append('')
    lines.append(f'Ran {n_tests} tests in 0.123s')
    if failures:
        lines.append('FAILED (failures=%d)' % len(failures))
    else:
        lines.append('OK')
    return '\n'.join(lines) + '\n'


# -------------------- pure-logic units --------------------

class ParseUnittestFailuresTest(_IsolatedAgentsRoot):
    def test_no_failures(self):
        output = _unittest_output()
        self.assertEqual(trc.parse_unittest_failures(output), set())

    def test_single_fail(self):
        output = _unittest_output([('test_a', 'scripts.tests.test_x.TestY')])
        self.assertEqual(
            trc.parse_unittest_failures(output),
            {'scripts.tests.test_x.TestY.test_a'},
        )

    def test_error_lines_also_count(self):
        output = (
            'ERROR: test_boom (scripts.tests.test_x.TestY)\n'
            'FAIL: test_oops (scripts.tests.test_x.TestY)\n'
            'Ran 2 tests in 0.01s\n'
        )
        self.assertEqual(
            trc.parse_unittest_failures(output),
            {
                'scripts.tests.test_x.TestY.test_boom',
                'scripts.tests.test_x.TestY.test_oops',
            },
        )

    def test_id_preserves_verbatim(self):
        """The dotted-id format is what `python3 -m unittest <id>` accepts.
        We must not normalize or rewrite case/separators."""
        output = (
            'FAIL: test_Mixed_Case '
            '(scripts.tests.test_camel.TestCamel_Mixed)\n'
            'Ran 1 tests in 0.01s\n'
        )
        self.assertEqual(
            trc.parse_unittest_failures(output),
            {'scripts.tests.test_camel.TestCamel_Mixed.test_Mixed_Case'},
        )


class ComputeVerdictTest(_IsolatedAgentsRoot):
    def test_both_empty_is_pass(self):
        v = trc.compute_verdict(set(), set())
        self.assertEqual(v['verdict'], 'PASS')
        self.assertEqual(v['regressions'], [])
        self.assertEqual(v['fixed'], [])
        self.assertEqual(v['pre_existing_unaffected'], [])

    def test_pre_existing_only_is_pass(self):
        parent = {'a.B.test_c'}
        head = {'a.B.test_c'}
        v = trc.compute_verdict(parent, head)
        self.assertEqual(v['verdict'], 'PASS')
        self.assertEqual(v['regressions'], [])
        self.assertEqual(v['pre_existing_unaffected'], ['a.B.test_c'])

    def test_new_failure_is_block(self):
        v = trc.compute_verdict(set(), {'a.B.test_c'})
        self.assertEqual(v['verdict'], 'BLOCK')
        self.assertEqual(v['regressions'], ['a.B.test_c'])

    def test_fixed_only_is_pass(self):
        v = trc.compute_verdict({'a.B.test_c'}, set())
        self.assertEqual(v['verdict'], 'PASS')
        self.assertEqual(v['fixed'], ['a.B.test_c'])

    def test_mixed_shared_plus_new_blocks_on_new_only(self):
        parent = {'a.B.test_old'}
        head = {'a.B.test_old', 'a.B.test_new'}
        v = trc.compute_verdict(parent, head)
        self.assertEqual(v['verdict'], 'BLOCK')
        self.assertEqual(v['regressions'], ['a.B.test_new'])
        self.assertEqual(v['pre_existing_unaffected'], ['a.B.test_old'])
        self.assertEqual(v['fixed'], [])


# -------------------- collect_failures_at_sha (test-runner branch) --------------------

class RunTestsInDirTest(_IsolatedAgentsRoot):
    def test_returns_failure_set_on_normal_run(self):
        completed = _completed(
            stdout=_unittest_output([('test_a', 'scripts.tests.test_x.T')]),
            returncode=1,
        )
        with patch.object(trc.subprocess, 'run', return_value=completed) as m:
            result = trc.run_tests_in_dir(Path('/tmp/x'), 60, Path('/tmp/iso'))
        self.assertEqual(result, {'scripts.tests.test_x.T.test_a'})
        # env override is applied
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs['env']['OURLIBERTY_AGENTS_ROOT'], '/tmp/iso')

    def test_timeout_raises_analysis_error(self):
        with patch.object(
            trc.subprocess, 'run',
            side_effect=subprocess.TimeoutExpired(cmd='x', timeout=1),
        ):
            with self.assertRaises(trc.AnalysisError):
                trc.run_tests_in_dir(Path('/tmp/x'), 1, None)

    def test_malformed_output_raises_analysis_error(self):
        """No "Ran N tests" sentinel AND no FAIL/ERROR lines → runner aborted."""
        completed = _completed(stdout='garbage\n', returncode=0)
        with patch.object(trc.subprocess, 'run', return_value=completed):
            with self.assertRaises(trc.AnalysisError):
                trc.run_tests_in_dir(Path('/tmp/x'), 60, None)

    def test_negative_return_code_raises(self):
        completed = _completed(stdout=_unittest_output(), returncode=-9)
        with patch.object(trc.subprocess, 'run', return_value=completed):
            with self.assertRaises(trc.AnalysisError):
                trc.run_tests_in_dir(Path('/tmp/x'), 60, None)

    def test_nonzero_exit_without_fail_lines_surfaces_synthetic_failure(self):
        """A clean "Ran N tests" + OK summary but a non-zero exit (a session
        guard like the production-write tripwire's atexit os._exit(1)) must NOT
        be swallowed: run_tests_in_dir surfaces the stable synthetic id so the
        gate can block on it. Regression for the gap where the tripwire's exit
        code was invisible to the FAIL/ERROR-line parser."""
        completed = _completed(stdout=_unittest_output(), returncode=1)
        with patch.object(trc.subprocess, 'run', return_value=completed):
            result = trc.run_tests_in_dir(Path('/tmp/x'), 60, None)
        self.assertEqual(result, {trc.SUITE_EXITED_NONZERO_ID})

    def test_clean_zero_exit_adds_no_synthetic_failure(self):
        """The common path — all tests pass, exit 0 — must stay empty; the
        synthetic id only appears on a non-zero exit (no false gate blocks)."""
        completed = _completed(stdout=_unittest_output(), returncode=0)
        with patch.object(trc.subprocess, 'run', return_value=completed):
            result = trc.run_tests_in_dir(Path('/tmp/x'), 60, None)
        self.assertEqual(result, set())

    def test_real_fail_lines_take_precedence_over_synthetic(self):
        """When the suite reports real FAIL/ERROR lines AND exits non-zero, the
        parsed ids are returned as-is — the synthetic id is only a fallback for a
        non-zero exit with NO parsed failures."""
        out = _unittest_output(failures=[('test_x', 'scripts.tests.test_a.TA')])
        completed = _completed(stdout=out, returncode=1)
        with patch.object(trc.subprocess, 'run', return_value=completed):
            result = trc.run_tests_in_dir(Path('/tmp/x'), 60, None)
        self.assertEqual(result, {'scripts.tests.test_a.TA.test_x'})
        self.assertNotIn(trc.SUITE_EXITED_NONZERO_ID, result)


# -------------------- main() / CLI surface --------------------

class _MainHarness(_IsolatedAgentsRoot):
    """Shared scaffolding for main() tests.

    We patch the inner helpers (resolve_sha, collect_failures_at_sha) rather
    than going all the way down to subprocess so each test states its
    intent: "parent had set X, head had set Y, what's the exit code?"
    """

    def _invoke_main(self, parent_failures: set[str], head_failures: set[str],
                     extra_argv: list[str] = None) -> tuple[int, str, str]:
        argv = [
            '--parent-sha', 'aaaabbbb',
            '--head-sha', 'ccccdddd',
            '--repo-root', '/tmp/fake-repo',
        ]
        if extra_argv:
            argv = argv + extra_argv

        sha_map = {'aaaabbbb': 'aaaabbbb' * 5, 'ccccdddd': 'ccccdddd' * 5}

        def fake_resolve(sha, root):
            return sha_map[sha]

        canonical_to_failures = {
            sha_map['aaaabbbb']: parent_failures,
            sha_map['ccccdddd']: head_failures,
        }

        def fake_collect(sha, repo_root, timeout, tmp_parent):
            return canonical_to_failures[sha]

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(trc, 'resolve_sha', side_effect=fake_resolve), \
             patch.object(trc, 'collect_failures_at_sha', side_effect=fake_collect):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = trc.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()


class MainCliVerdictTest(_MainHarness):
    def test_clean_both_sides_exits_pass(self):
        code, out, _ = self._invoke_main(set(), set())
        self.assertEqual(code, trc.EXIT_PASS)
        report = json.loads(out)
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['regressions'], [])

    def test_new_failure_exits_block(self):
        code, out, _ = self._invoke_main(set(), {'a.B.test_new'})
        self.assertEqual(code, trc.EXIT_BLOCK)
        report = json.loads(out)
        self.assertEqual(report['verdict'], 'BLOCK')
        self.assertEqual(report['regressions'], ['a.B.test_new'])

    def test_pre_existing_only_exits_pass(self):
        code, out, _ = self._invoke_main({'a.B.test_old'}, {'a.B.test_old'})
        self.assertEqual(code, trc.EXIT_PASS)
        report = json.loads(out)
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['pre_existing_unaffected'], ['a.B.test_old'])

    def test_text_output_mode(self):
        code, out, _ = self._invoke_main(
            set(), {'a.B.test_new'}, extra_argv=['--output', 'text'],
        )
        self.assertEqual(code, trc.EXIT_BLOCK)
        self.assertIn('verdict:    BLOCK', out)
        self.assertIn('a.B.test_new', out)
        # text mode should NOT be valid JSON
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


class MainCliAnalysisFailTest(_IsolatedAgentsRoot):
    def test_unresolvable_parent_sha_exits_analysis_fail(self):
        def fake_resolve(sha, root):
            raise trc.AnalysisError(f'cannot resolve SHA {sha!r}')

        stderr = io.StringIO()
        with patch.object(trc, 'resolve_sha', side_effect=fake_resolve):
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = trc.main([
                    '--parent-sha', 'deadbeef', '--head-sha', 'cafebabe',
                    '--repo-root', '/tmp/fake-repo',
                ])
        self.assertEqual(code, trc.EXIT_ANALYSIS_FAIL)
        self.assertIn('cannot resolve', stderr.getvalue())

    def test_collect_failure_at_parent_exits_analysis_fail(self):
        def fake_resolve(sha, root):
            return sha * 5

        def fake_collect(sha, repo_root, timeout, tmp_parent):
            raise trc.AnalysisError('test suite timed out after 300s')

        stderr = io.StringIO()
        with patch.object(trc, 'resolve_sha', side_effect=fake_resolve), \
             patch.object(trc, 'collect_failures_at_sha', side_effect=fake_collect):
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = trc.main([
                    '--parent-sha', 'aaaabbbb', '--head-sha', 'ccccdddd',
                    '--repo-root', '/tmp/fake-repo',
                ])
        self.assertEqual(code, trc.EXIT_ANALYSIS_FAIL)
        self.assertIn('timed out', stderr.getvalue())


# -------------------- HEAD optimization --------------------

class CurrentHeadOptimizationTest(_IsolatedAgentsRoot):
    def test_skips_worktree_when_sha_matches_head(self):
        """When the requested SHA equals the worktree's current HEAD,
        the helper must NOT create a tmp worktree (perf + safety)."""
        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as tmp:
            tmp_parent = Path(tmp)
            with patch.object(trc, 'current_head_sha', return_value=sha), \
                 patch.object(trc, 'add_worktree') as add_mock, \
                 patch.object(trc, 'remove_worktree') as rm_mock, \
                 patch.object(trc, 'run_tests_in_dir', return_value=set()) as run_mock:
                result = trc.collect_failures_at_sha(
                    sha, Path('/tmp/repo'), 60, tmp_parent,
                )
        self.assertEqual(result, set())
        add_mock.assert_not_called()
        rm_mock.assert_not_called()
        run_mock.assert_called_once()
        # the workdir passed to run_tests_in_dir is the repo root itself
        self.assertEqual(run_mock.call_args.args[0], Path('/tmp/repo'))

    def test_creates_worktree_when_sha_differs_from_head(self):
        sha = 'b' * 40
        with tempfile.TemporaryDirectory() as tmp:
            tmp_parent = Path(tmp)
            with patch.object(trc, 'current_head_sha', return_value='c' * 40), \
                 patch.object(trc, 'add_worktree') as add_mock, \
                 patch.object(trc, 'remove_worktree') as rm_mock, \
                 patch.object(trc, 'run_tests_in_dir', return_value=set()) as run_mock:
                trc.collect_failures_at_sha(
                    sha, Path('/tmp/repo'), 60, tmp_parent,
                )
        add_mock.assert_called_once()
        rm_mock.assert_called_once()
        run_mock.assert_called_once()
        # the workdir is the tmp worktree path, not the repo root
        self.assertNotEqual(run_mock.call_args.args[0], Path('/tmp/repo'))


# -------------------- resolve_sha --------------------

class ResolveShaTest(_IsolatedAgentsRoot):
    def test_resolves_canonical_sha(self):
        completed = _completed(stdout='deadbeef' * 5 + '\n', returncode=0)
        with patch.object(trc.subprocess, 'run', return_value=completed):
            result = trc.resolve_sha('deadbeef', Path('/tmp/repo'))
        self.assertEqual(result, 'deadbeef' * 5)

    def test_unresolvable_raises_analysis_error(self):
        completed = _completed(stderr='fatal: bad revision\n', returncode=128)
        with patch.object(trc.subprocess, 'run', return_value=completed):
            with self.assertRaises(trc.AnalysisError):
                trc.resolve_sha('nope', Path('/tmp/repo'))


if __name__ == '__main__':
    unittest.main()
