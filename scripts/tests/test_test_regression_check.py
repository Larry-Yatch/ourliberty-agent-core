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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import contextlib
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import test_regression_check as trc  # noqa: E402
import regression_baseline_cache as baseline_cache  # noqa: E402


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
        # The regression-baseline cache deliberately does NOT use
        # OURLIBERTY_AGENTS_ROOT (that's the gate's per-run sandbox redirect for
        # the suite subprocess) — it writes to the REAL tree via
        # OL_REGRESSION_BASELINE_DIR / $HOME. Isolate it per-test too, else
        # main()'s cache writes would pollute the real home AND leak a baseline
        # across tests that share a parent SHA.
        self._baseline_env_orig = os.environ.get('OL_REGRESSION_BASELINE_DIR')
        os.environ['OL_REGRESSION_BASELINE_DIR'] = os.path.join(
            self._isolated_tmp, 'regression-baselines',
        )
        importlib.reload(trc)

    def tearDown(self):
        if self._isolated_env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_env_orig
        if self._baseline_env_orig is None:
            os.environ.pop('OL_REGRESSION_BASELINE_DIR', None)
        else:
            os.environ['OL_REGRESSION_BASELINE_DIR'] = self._baseline_env_orig
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


class DefaultPerShaTimeoutTest(_IsolatedAgentsRoot):
    def test_default_per_sha_timeout_exceeds_a_measured_run(self):
        # regression-gate-cant-conclude: a full suite pass MEASURES ~537s wall.
        # The default per-SHA cap must sit comfortably ABOVE that or it kills a
        # healthy run mid-suite (the old 300s default → EXIT_ANALYSIS_FAIL →
        # false "inconclusive" ESCALATE on clean code). Guard against a revert
        # to a sub-run value.
        self.assertGreaterEqual(trc.DEFAULT_TIMEOUT_PER_SHA_S, 700)


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


class AbsoluteInvariantVerdictTest(_IsolatedAgentsRoot):
    """The absolute-invariant set bypasses dial 3's pre-existing tolerance: an
    invariant test failing at HEAD blocks even when it was ALSO failing at the
    parent (the silent-accumulation hole that let two non-compliant test files
    merge red — bootstrap-import-gate-enforce-001). NON-invariant pre-existing
    failures keep the original tolerance and still PASS."""

    def setUp(self):
        super().setUp()
        # Use the real invariant id from the module so this test tracks the set
        # rather than hardcoding a copy that could silently drift out of parity.
        self._invariant_id = next(iter(trc.ABSOLUTE_INVARIANT_TESTS))

    def test_invariant_failing_at_both_shas_still_blocks(self):
        parent = {self._invariant_id}
        head = {self._invariant_id}
        v = trc.compute_verdict(parent, head)
        self.assertEqual(v['verdict'], 'BLOCK')
        # It is NOT a regression (present at parent too) — it blocks via the
        # invariant path, and is surfaced distinctly.
        self.assertEqual(v['regressions'], [])
        self.assertEqual(v['invariant_failures'], [self._invariant_id])
        self.assertEqual(v['pre_existing_unaffected'], [self._invariant_id])

    def test_non_invariant_pre_existing_still_passes(self):
        """Tolerance preserved for everything outside the invariant set."""
        parent = {'a.B.test_old'}
        head = {'a.B.test_old'}
        v = trc.compute_verdict(parent, head)
        self.assertEqual(v['verdict'], 'PASS')
        self.assertEqual(v['invariant_failures'], [])
        self.assertEqual(v['pre_existing_unaffected'], ['a.B.test_old'])

    def test_invariant_plus_non_invariant_pre_existing_blocks_on_invariant_only(self):
        parent = {self._invariant_id, 'a.B.test_old'}
        head = {self._invariant_id, 'a.B.test_old'}
        v = trc.compute_verdict(parent, head)
        self.assertEqual(v['verdict'], 'BLOCK')
        self.assertEqual(v['regressions'], [])
        self.assertEqual(v['invariant_failures'], [self._invariant_id])
        self.assertIn('a.B.test_old', v['pre_existing_unaffected'])

    def test_invariant_absent_at_head_does_not_block(self):
        """A PR that FIXES the invariant (failing at parent, passing at head)
        must not be blocked by the invariant path — only HEAD presence blocks."""
        parent = {self._invariant_id}
        head: set[str] = set()
        v = trc.compute_verdict(parent, head)
        self.assertEqual(v['verdict'], 'PASS')
        self.assertEqual(v['invariant_failures'], [])
        self.assertEqual(v['fixed'], [self._invariant_id])


class ReverifyWithFreshParentTest(_IsolatedAgentsRoot):
    """A would-be BLOCK against a CACHED parent baseline is recomputed against a
    freshly re-run parent. reverify_verdict_with_fresh_parent is the pure core:
    given head, the (stale) cached parent set, and the fresh parent set, it must
    tolerate a flake that fails in BOTH fresh runs and KEEP a genuine regression."""

    def test_deterministic_flake_cleared(self):
        # The flake failed at head but the cache missed it at parent; a fresh
        # parent run shows it fails there too → not this PR's regression.
        head = {'a.B.flake'}
        cached_parent: set = set()          # cache missed the flake
        fresh_parent = {'a.B.flake'}        # fresh run reproduces it at parent
        vb, flakes = trc.reverify_verdict_with_fresh_parent(
            head, cached_parent, fresh_parent)
        self.assertEqual(vb['verdict'], 'PASS')
        self.assertEqual(vb['regressions'], [])
        self.assertEqual(flakes, ['a.B.flake'])

    def test_genuine_regression_still_blocks(self):
        # Fails at head, absent from BOTH the cached and the fresh parent run.
        head = {'a.B.real'}
        vb, flakes = trc.reverify_verdict_with_fresh_parent(head, set(), set())
        self.assertEqual(vb['verdict'], 'BLOCK')
        self.assertEqual(vb['regressions'], ['a.B.real'])
        self.assertEqual(flakes, [])

    def test_newly_added_broken_test_still_blocks(self):
        # Regression-guard for the isolation-approach false-PASS this design
        # replaced: a brand-new failing test does not exist at parent, so it is
        # absent from the fresh parent failure-set and stays a regression.
        head = {'new_mod.T.test_broken'}
        vb, flakes = trc.reverify_verdict_with_fresh_parent(head, set(), set())
        self.assertEqual(vb['verdict'], 'BLOCK')
        self.assertEqual(vb['regressions'], ['new_mod.T.test_broken'])
        self.assertEqual(flakes, [])

    def test_mixed_flake_and_real_regression(self):
        head = {'a.B.flake', 'a.B.real'}
        cached_parent: set = set()
        fresh_parent = {'a.B.flake'}   # flake reproduces at fresh parent, real does not
        vb, flakes = trc.reverify_verdict_with_fresh_parent(
            head, cached_parent, fresh_parent)
        self.assertEqual(vb['verdict'], 'BLOCK')
        self.assertEqual(vb['regressions'], ['a.B.real'])
        self.assertEqual(flakes, ['a.B.flake'])

    def test_reverified_flakes_only_counts_vanished_candidates(self):
        # A pre-existing failure present in the cached parent is NOT a candidate,
        # so it is never reported as reverified even though it is in fresh_parent.
        head = {'a.B.flake', 'a.B.old'}
        cached_parent = {'a.B.old'}
        fresh_parent = {'a.B.flake', 'a.B.old'}
        vb, flakes = trc.reverify_verdict_with_fresh_parent(
            head, cached_parent, fresh_parent)
        self.assertEqual(vb['verdict'], 'PASS')
        self.assertEqual(flakes, ['a.B.flake'])  # not 'a.B.old'


# -------------------- collect_failures_at_sha (test-runner branch) --------------------

class RunTestsInDirTest(_IsolatedAgentsRoot):
    def setUp(self):
        super().setUp()
        # The droplet hard wall (item 3) is exercised in its own test class; here
        # we test the suite-run path in isolation, so disable the wall (return []).
        p = patch.object(trc, '_discover_wall_prefix', return_value=[])
        p.start()
        self.addCleanup(p.stop)

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


class MainBaselineCacheTest(_MainHarness):
    """Parent-SHA baseline cache: a hit skips the parent suite run, a miss runs
    it and warms the cache, and --no-baseline-cache forces the original
    always-two-runs behavior. Verdict math is unchanged either way."""

    PARENT = 'aaaabbbb' * 5
    HEAD = 'ccccdddd' * 5

    def _invoke_counting(self, parent_failures, head_failures, extra_argv=None):
        """Like _invoke_main but records which SHAs collect_failures ran for,
        so we can prove the parent run was (or wasn't) skipped."""
        argv = ['--parent-sha', 'aaaabbbb', '--head-sha', 'ccccdddd',
                '--repo-root', '/tmp/fake-repo'] + (extra_argv or [])
        sha_map = {'aaaabbbb': self.PARENT, 'ccccdddd': self.HEAD}
        cf = {self.PARENT: parent_failures, self.HEAD: head_failures}
        ran = []

        def fake_collect(sha, repo_root, timeout, tmp_parent):
            ran.append(sha)
            return cf[sha]

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(trc, 'resolve_sha',
                          side_effect=lambda s, r: sha_map[s]), \
             patch.object(trc, 'collect_failures_at_sha',
                          side_effect=fake_collect):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = trc.main(argv)
        return code, json.loads(stdout.getvalue()), ran

    def test_cache_miss_runs_both_and_warms(self):
        code, report, ran = self._invoke_counting(
            {'a.B.test_old'}, {'a.B.test_old'},
        )
        self.assertEqual(code, trc.EXIT_PASS)
        self.assertEqual(ran, [self.PARENT, self.HEAD])  # both ran (cold)
        self.assertFalse(report['used_cached_baseline'])
        # the parent baseline is now warmed for next time
        self.assertEqual(baseline_cache.load(self.PARENT), {'a.B.test_old'})

    def test_cache_hit_skips_parent_run(self):
        baseline_cache.store(self.PARENT, {'a.B.test_old'})
        # The parent_failures wired here is deliberately WRONG: if the gate ran
        # the parent instead of using the cache, the verdict would differ.
        code, report, ran = self._invoke_counting(
            {'WRONG.should.not.appear'}, {'a.B.test_old'},
        )
        self.assertEqual(ran, [self.HEAD])  # parent run SKIPPED
        self.assertTrue(report['used_cached_baseline'])
        self.assertEqual(report['parent_failures'], ['a.B.test_old'])  # from cache
        self.assertEqual(report['verdict'], 'PASS')  # head == cached parent

    def test_cache_hit_real_regression_reverified_still_blocks(self):
        # A cached baseline that would BLOCK re-runs the parent fresh to verify.
        # The fresh parent (wired here as the accurate parent set) confirms
        # test_new is genuinely new → still BLOCK. This is the safety property:
        # the fresh-parent re-run cannot clear a real regression.
        baseline_cache.store(self.PARENT, {'a.B.test_old'})
        code, report, ran = self._invoke_counting(
            {'a.B.test_old'}, {'a.B.test_old', 'a.B.test_new'},
        )
        self.assertEqual(ran, [self.HEAD, self.PARENT])  # head, then fresh parent
        self.assertEqual(code, trc.EXIT_BLOCK)
        self.assertEqual(report['regressions'], ['a.B.test_new'])
        self.assertTrue(report['parent_reverified'])
        self.assertEqual(report['reverified_flakes'], [])

    def test_cache_hit_flake_cleared_by_fresh_parent(self):
        # The stale cache MISSED a deterministic full-suite flake; head has it,
        # so it looks like a new regression. The fresh parent re-run reproduces
        # the flake → it is present at BOTH SHAs → not this PR's regression → the
        # would-be BLOCK is corrected to PASS. This is the false-BLOCK class fix.
        baseline_cache.store(self.PARENT, set())          # cache missed the flake
        code, report, ran = self._invoke_counting(
            {'a.B.flake'}, {'a.B.flake'},                 # fresh parent has it too
        )
        self.assertEqual(ran, [self.HEAD, self.PARENT])
        self.assertEqual(code, trc.EXIT_PASS)
        self.assertEqual(report['regressions'], [])
        self.assertEqual(report['reverified_flakes'], ['a.B.flake'])
        self.assertTrue(report['parent_reverified'])

    def test_cache_hit_no_regression_does_not_rerun_parent(self):
        # The green path: a cache hit with no regression never pays for a re-run.
        baseline_cache.store(self.PARENT, {'a.B.test_old'})
        code, report, ran = self._invoke_counting(
            {'a.B.test_old'}, {'a.B.test_old'},
        )
        self.assertEqual(ran, [self.HEAD])  # parent NOT re-run
        self.assertEqual(code, trc.EXIT_PASS)
        self.assertFalse(report['parent_reverified'])

    def test_no_flake_reverify_flag_keeps_cached_baseline_block(self):
        # Escape hatch: with --no-flake-reverify the cached-baseline BLOCK stands
        # (no fresh parent re-run), preserving the pre-fix behavior on demand.
        baseline_cache.store(self.PARENT, set())
        code, report, ran = self._invoke_counting(
            {'a.B.flake'}, {'a.B.flake'}, extra_argv=['--no-flake-reverify'],
        )
        self.assertEqual(ran, [self.HEAD])  # parent NOT re-run
        self.assertEqual(code, trc.EXIT_BLOCK)
        self.assertFalse(report['parent_reverified'])
        self.assertEqual(report['regressions'], ['a.B.flake'])

    def test_no_cache_flag_forces_both_runs(self):
        baseline_cache.store(self.PARENT, {'a.B.test_old'})
        code, report, ran = self._invoke_counting(
            {'a.B.test_old'}, {'a.B.test_old'},
            extra_argv=['--no-baseline-cache'],
        )
        self.assertEqual(ran, [self.PARENT, self.HEAD])  # cache ignored
        self.assertFalse(report['used_cached_baseline'])

    def test_invariant_pre_existing_exits_block_end_to_end(self):
        """A PR whose HEAD still fails an absolute-invariant test exits BLOCK
        even though that same id was already failing at parent — the JSON
        surfaces it under invariant_failures, not regressions."""
        invariant_id = next(iter(trc.ABSOLUTE_INVARIANT_TESTS))
        code, out, _ = self._invoke_main({invariant_id}, {invariant_id})
        self.assertEqual(code, trc.EXIT_BLOCK)
        report = json.loads(out)
        self.assertEqual(report['verdict'], 'BLOCK')
        self.assertEqual(report['regressions'], [])
        self.assertEqual(report['invariant_failures'], [invariant_id])


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


# -------------------- gate never runs in-place (item 1 — M2/M3) --------------------

class GateAlwaysMaterializesWorktreeTest(_IsolatedAgentsRoot):
    """The gate must NEVER run the suite in the live checkout — always a
    disposable worktree, for BOTH parent and head SHAs, even when the SHA
    equals the current HEAD (item 1 closes M2; prevents M3's auto-commit of
    cwd-relative test residue onto origin/main and mid-run code-swap)."""

    def _collect_with_mocks(self, sha: str, repo_root: Path):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_parent = Path(tmp)
            with patch.object(trc, 'add_worktree') as add_mock, \
                 patch.object(trc, 'remove_worktree') as rm_mock, \
                 patch.object(trc, 'run_tests_in_dir', return_value=set()) as run_mock, \
                 patch.object(trc, 'scan_real_tree_for_sentinel') as scan_mock:
                trc.collect_failures_at_sha(sha, repo_root, 60, tmp_parent)
        return add_mock, rm_mock, run_mock, scan_mock

    def test_materializes_worktree_even_when_sha_is_head(self):
        # Resolve a SHA and point repo_root at THIS repo so the SHA genuinely is
        # the live HEAD — the old fast path would have run in place here.
        repo_root = Path(trc.__file__).resolve().parents[1]
        head = subprocess.run(
            ['git', '-C', str(repo_root), 'rev-parse', 'HEAD'],
            capture_output=True, text=True,
        ).stdout.strip()
        add_mock, rm_mock, run_mock, _ = self._collect_with_mocks(head, repo_root)
        add_mock.assert_called_once()
        rm_mock.assert_called_once()
        run_mock.assert_called_once()
        # run_tests_in_dir is NEVER handed the live checkout as its workdir.
        self.assertNotEqual(run_mock.call_args.args[0], repo_root)

    def test_worktree_path_is_under_tmp_parent(self):
        sha = 'b' * 40
        add_mock, _, run_mock, _ = self._collect_with_mocks(sha, Path('/tmp/repo'))
        # The dest passed to add_worktree and the workdir handed to the runner
        # are the same gate worktree, nested under the mkdtemp parent.
        worktree_dest = add_mock.call_args.args[2]
        self.assertEqual(run_mock.call_args.args[0], worktree_dest)

    def test_worktree_naming_cannot_match_healer_kill_pattern(self):
        """The healers SIGKILL processes whose cwd starts with /tmp/wt-main-
        (M9). The gate worktree path must never match that prefix and must stay
        under the test-regression-check- mkdtemp parent."""
        captured = {}

        def fake_add(repo_root, sha, dest):
            captured['dest'] = dest

        with tempfile.TemporaryDirectory(prefix='test-regression-check-') as tmp:
            tmp_parent = Path(tmp)
            with patch.object(trc, 'add_worktree', side_effect=fake_add), \
                 patch.object(trc, 'remove_worktree'), \
                 patch.object(trc, 'run_tests_in_dir', return_value=set()), \
                 patch.object(trc, 'scan_real_tree_for_sentinel'):
                trc.collect_failures_at_sha('a' * 40, Path('/tmp/repo'), 60, tmp_parent)

        dest = str(captured['dest'])
        self.assertFalse(
            dest.startswith('/tmp/wt-main-'),
            f'gate worktree {dest} matches the healers kill prefix',
        )
        self.assertIn('test-regression-check-', dest)
        self.assertIn('gate-wt-', dest)


# -------------------- subtractive env (item 2 — M4/H12) --------------------

class SubtractiveSandboxEnvTest(_IsolatedAgentsRoot):
    """build_sandbox_env must STRIP live credential families and pin external
    API URLs to a dead port, while still SETTING the OURLIBERTY_* sandbox keys
    + the run sentinel."""

    def _base_with_creds(self) -> dict:
        return {
            'PATH': '/usr/bin',
            'HOME': '/home/larry',
            'LANG': 'C.UTF-8',
            'SUPABASE_URL': 'https://live.supabase.co',
            'SUPABASE_SERVICE_ROLE_KEY': 'live-service-role',
            'TELEGRAM_BOT_TOKEN': 'live-bot-token',
            'TELEGRAM_BOT_TOKEN_TIER2': 'live-bot-token-2',
            'GH_TOKEN': 'gho_live',
            'GITHUB_TOKEN': 'ghp_live',
            'ANTHROPIC_API_KEY': 'sk-ant-live',
            'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-live',
            'OL_DASHBOARD_API_URL': 'https://api.ourliberty.dev',
            'DASHBOARD_API_URL': 'https://api.ourliberty.dev',
        }

    def test_strips_every_credential_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = trc.build_sandbox_env(Path(tmp), base_env=self._base_with_creds())
        for cred in (
            'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY',
            'TELEGRAM_BOT_TOKEN', 'TELEGRAM_BOT_TOKEN_TIER2',
            'GH_TOKEN', 'GITHUB_TOKEN',
            'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN',
        ):
            self.assertNotIn(cred, env, f'{cred} was NOT stripped from the gate env')

    def test_preserves_toolchain_vars(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = trc.build_sandbox_env(Path(tmp), base_env=self._base_with_creds())
        for keep in ('PATH', 'HOME', 'LANG'):
            self.assertIn(keep, env, f'{keep} must be preserved for the toolchain')

    def test_pins_api_urls_to_dead_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = trc.build_sandbox_env(Path(tmp), base_env=self._base_with_creds())
        for var in ('OL_DASHBOARD_API_URL', 'DASHBOARD_API_URL'):
            self.assertEqual(env[var], trc._DEAD_API_URL)

    def test_still_sets_sandbox_keys_and_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = trc.build_sandbox_env(Path(tmp), base_env=self._base_with_creds())
        self.assertEqual(env['OURLIBERTY_AGENTS_ROOT'], tmp)
        self.assertEqual(env['OURLIBERTY_DISABLE_LIVE_EMIT'], '1')
        self.assertTrue(
            env['OURLIBERTY_TEST_RUN_SENTINEL'].startswith(
                trc._TEST_RUN_SENTINEL_PREFIX,
            ),
        )


# -------------------- outside-jail tripwire (item 4 — H10) --------------------

class OutsideJailTripwireTest(_IsolatedAgentsRoot):
    """The parent-process sentinel scan of the REAL tree blocks (exit 2) on a
    confirmed leak, fails-open on scan-infra errors, and covers outboxes/."""

    def test_outboxes_is_scanned(self):
        self.assertIn('outboxes', trc._TRIPWIRE_SUBDIRS)

    def test_clean_tree_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_agents = Path(tmp) / 'agents'
            for sub in trc._TRIPWIRE_SUBDIRS:
                (fake_agents / sub).mkdir(parents=True, exist_ok=True)
            (fake_agents / 'logs' / 'benign.log').write_text('nothing to see\n')
            with patch.object(trc, 'REAL_AGENTS', fake_agents):
                trc.scan_real_tree_for_sentinel('OL-TEST-RUN-SENTINEL-deadbeef')

    def test_sentinel_hit_raises_analysis_error(self):
        sentinel = 'OL-TEST-RUN-SENTINEL-' + 'cafe' * 8
        with tempfile.TemporaryDirectory() as tmp:
            fake_agents = Path(tmp) / 'agents'
            (fake_agents / 'outboxes').mkdir(parents=True, exist_ok=True)
            leaked = fake_agents / 'outboxes' / 'leaked.json'
            leaked.write_text(f'{{"sentinel": "{sentinel}"}}\n')
            with patch.object(trc, 'REAL_AGENTS', fake_agents):
                with self.assertRaises(trc.AnalysisError):
                    trc.scan_real_tree_for_sentinel(sentinel)

    def test_old_residue_is_skipped_by_mtime_filter(self):
        """A pre-existing real-tree file that (synthetically) holds the sentinel
        but predates the run is SKIPPED when since_mtime is set — it can't be
        from this run, so reading it is the ~140MB-per-run waste we avoid. With
        the default since_mtime=0.0 the SAME file is read (proving the skip is
        the mtime filter, not a missing needle)."""
        sentinel = 'OL-TEST-RUN-SENTINEL-' + 'feed' * 8
        with tempfile.TemporaryDirectory() as tmp:
            fake_agents = Path(tmp) / 'agents'
            (fake_agents / 'logs').mkdir(parents=True, exist_ok=True)
            residue = fake_agents / 'logs' / 'old-residue.log'
            residue.write_text(f'stale {sentinel}\n')
            old = time.time() - 3600
            os.utime(residue, (old, old))
            with patch.object(trc, 'REAL_AGENTS', fake_agents):
                # since_mtime in the recent past → the hour-old file is skipped.
                trc.scan_real_tree_for_sentinel(
                    sentinel, since_mtime=time.time() - 60,
                )
                # default (read everything) → the same file IS flagged.
                with self.assertRaises(trc.AnalysisError):
                    trc.scan_real_tree_for_sentinel(sentinel)

    def test_collect_failures_raises_on_real_tree_leak(self):
        """End-to-end through collect_failures_at_sha: a sentinel-bearing file in
        the (monkeypatched) real tree yields AnalysisError, which main() maps to
        exit 2."""
        sentinel = 'OL-TEST-RUN-SENTINEL-' + 'beef' * 8
        fixed_env = {'OURLIBERTY_TEST_RUN_SENTINEL': sentinel}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_parent = Path(tmp) / 'gate'
            tmp_parent.mkdir()
            fake_agents = Path(tmp) / 'agents'
            (fake_agents / 'blackboard').mkdir(parents=True, exist_ok=True)
            (fake_agents / 'blackboard' / 'larry-alerts.jsonl').write_text(
                f'leak {sentinel}\n',
            )
            with patch.object(trc, 'build_sandbox_env', return_value=fixed_env), \
                 patch.object(trc, 'add_worktree'), \
                 patch.object(trc, 'remove_worktree'), \
                 patch.object(trc, 'run_tests_in_dir', return_value=set()), \
                 patch.object(trc, 'REAL_AGENTS', fake_agents):
                with self.assertRaises(trc.AnalysisError):
                    trc.collect_failures_at_sha(
                        'a' * 40, Path('/tmp/repo'), 60, tmp_parent,
                    )

    def test_scan_infra_error_fails_open(self):
        """A scan that can't read a dir logs a warning and does NOT block."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_agents = Path(tmp) / 'agents'
            (fake_agents / 'state').mkdir(parents=True, exist_ok=True)
            with patch.object(trc, 'REAL_AGENTS', fake_agents), \
                 patch.object(Path, 'rglob', side_effect=OSError('boom')):
                # Must not raise — fail-open on infra error.
                trc.scan_real_tree_for_sentinel('OL-TEST-RUN-SENTINEL-x')


# -------------------- hard-wall fall-through (item 3) --------------------

class HardWallFallThroughTest(_IsolatedAgentsRoot):
    """When no namespace primitive is available, the wall logs a warning and
    returns [] (run unwalled) — wall-absence must NEVER block a PR."""

    def test_falls_through_to_unwalled_with_warning(self):
        stderr = io.StringIO()
        # Simulate the PRODUCTION top-level gate invocation, which runs UNWALLED
        # (OURLIBERTY_TEST_WALL_ACTIVE unset). The wall-unavailable warning is
        # deliberately suppressed when _discover_wall_prefix is called from
        # INSIDE an active wall (e.g. this very suite, which re-execs under
        # bwrap) — there bwrap isn't absent, it just can't nest. Clear the flag
        # so we exercise the genuine "no namespace primitive" path.
        env_no_wall = {k: v for k, v in trc.os.environ.items()
                       if k != trc._WALL_ACTIVE_ENV}
        with patch.dict(trc.os.environ, env_no_wall, clear=True), \
             patch.object(trc.shutil, 'which', return_value=None), \
             patch.object(trc, '_probe', return_value=False):
            with redirect_stderr(stderr):
                prefix = trc._discover_wall_prefix(Path('/tmp/wt'))
        self.assertEqual(prefix, [])
        self.assertIn('hard wall unavailable', stderr.getvalue())

    def test_bwrap_prefix_when_probe_passes(self):
        with patch.object(trc.shutil, 'which', return_value='/usr/bin/bwrap'), \
             patch.object(trc, '_probe', return_value=True):
            prefix = trc._discover_wall_prefix(Path('/tmp/wt'))
        self.assertEqual(prefix[0], '/usr/bin/bwrap')
        self.assertIn('--ro-bind', prefix)
        self.assertEqual(prefix[-1], '--')

    def test_run_tests_completes_when_wall_unavailable(self):
        """run_tests_in_dir must complete normally (no exit 2) when the wall is
        unavailable — the discover command runs unwrapped."""
        completed = _completed(stdout=_unittest_output(), returncode=0)
        with patch.object(trc, '_discover_wall_prefix', return_value=[]) as wall_mock, \
             patch.object(trc.subprocess, 'run', return_value=completed) as run_mock:
            result = trc.run_tests_in_dir(Path('/tmp/x'), 60, Path('/tmp/iso'))
        self.assertEqual(result, set())
        wall_mock.assert_called_once()
        # No wall prefix => the discover command is the first positional arg.
        self.assertEqual(run_mock.call_args.args[0][0], 'python3')


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


# -------------------- remove_worktree (double-force) --------------------


def _git_live(*args, cwd=None, check=True):
    return subprocess.run(
        ['git', *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, check=check, timeout=60,
    )


class RemoveWorktreeDoubleForceTest(unittest.TestCase):
    """remove_worktree() must override git's 'initializing' lock.

    A single `git worktree remove --force` REFUSES a locked worktree; only
    `--force --force` (double force) overrides it. This is a live-git test —
    the mocked-subprocess suite above can't catch the single-vs-double-force
    behavior because it never runs real git.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix='remove-wt-doubleforce-'))
        self.repo = self.tmpdir / 'repo'
        self.repo.mkdir()
        _git_live('init', '-q', '--initial-branch=main', cwd=self.repo)
        _git_live('config', 'user.email', 'test@example.com', cwd=self.repo)
        _git_live('config', 'user.name', 'Test', cwd=self.repo)
        (self.repo / 'README.md').write_text('initial\n')
        _git_live('add', 'README.md', cwd=self.repo)
        _git_live('commit', '-q', '-m', 'initial commit', cwd=self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_removes_worktree_carrying_initializing_lock(self):
        workdir = self.tmpdir / 'gate-wt-locked'
        _git_live('worktree', 'add', '--detach', str(workdir), 'HEAD',
                  cwd=self.repo)
        meta = self.repo / '.git' / 'worktrees' / 'gate-wt-locked'
        self.assertTrue(meta.is_dir())
        # Simulate the add being SIGKILLed mid-init with the lock still held.
        (meta / 'locked').write_text('initializing\n')

        trc.remove_worktree(self.repo, workdir)

        self.assertFalse(
            workdir.exists(),
            'double-force remove should delete the locked worktree dir',
        )
        self.assertFalse(
            meta.exists(),
            'double-force remove should drop the locked .git/worktrees entry',
        )


class DiffInertSkipTest(_IsolatedAgentsRoot):
    """Diff-scoped skip: a pure-docs PR cannot regress a test, so the gate
    short-circuits to PASS without running the suite (the #770-#773 thrash)."""

    # ---- pure path classification ----

    def test_is_test_inert_path(self):
        for p in ('docs/spec.md', 'docs/specs/tier.md',
                  'docs/approval-sync-north-star.md'):
            self.assertTrue(trc._is_test_inert_path(p), p)
        # Non-inert: docs/runbooks (existence-validated via config), code,
        # config, agents, top-level runbooks (read by the pulse-fixture test),
        # and repo-root files outside docs/.
        for p in ('docs/runbooks/rotate.md', 'scripts/foo.py',
                  'scripts/tests/test_foo.py', 'config/agent-models.json',
                  'agents/pulse/CLAUDE.md', 'runbooks/cycle-prompt.md',
                  'README.md'):
            self.assertFalse(trc._is_test_inert_path(p), p)

    def test_diff_is_test_inert(self):
        self.assertTrue(trc.diff_is_test_inert(['docs/a.md', 'docs/specs/b.md']))
        self.assertFalse(trc.diff_is_test_inert([]))               # empty → run gate
        self.assertFalse(trc.diff_is_test_inert(['docs/a.md', 'scripts/x.py']))
        self.assertFalse(trc.diff_is_test_inert(['docs/runbooks/r.md']))

    # ---- diff_changed_files against a real tmp git repo ----

    def test_diff_changed_files_real_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def g(*a):
                return subprocess.run(
                    ['git', '-C', str(repo), *a],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            g('init', '-q'); g('config', 'user.email', 't@t')
            g('config', 'user.name', 't')
            (repo / 'scripts').mkdir(); (repo / 'scripts' / 'foo.py').write_text('x=1\n')
            (repo / 'docs').mkdir(); (repo / 'docs' / 'spec.md').write_text('v1\n')
            g('add', '-A'); g('commit', '-q', '-m', 'base')
            parent = g('rev-parse', 'HEAD')
            (repo / 'docs' / 'spec.md').write_text('v2 — changed\n')  # docs-only
            g('add', '-A'); g('commit', '-q', '-m', 'docs')
            head = g('rev-parse', 'HEAD')

            changed = trc.diff_changed_files(parent, head, repo)
            self.assertEqual(changed, ['docs/spec.md'])
            self.assertTrue(trc.diff_is_test_inert(changed))
            # Unknown SHAs → git error → None (fail-safe: caller runs the gate).
            self.assertIsNone(
                trc.diff_changed_files('dead' * 10, 'beef' * 10, repo))

    # ---- main() short-circuit ----

    def _run_main_with_diff(self, changed, extra=()):
        argv = ['--parent-sha', 'aaaa', '--head-sha', 'bbbb',
                '--repo-root', '/tmp/fake-repo', *extra]
        sha_map = {'aaaa': 'a' * 40, 'bbbb': 'b' * 40}
        out = io.StringIO()
        with patch.object(trc, 'resolve_sha', side_effect=lambda s, r: sha_map[s]), \
             patch.object(trc, 'diff_changed_files', return_value=changed), \
             patch.object(trc, 'collect_failures_at_sha',
                          return_value=set()) as collect:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = trc.main(argv)
        return code, json.loads(out.getvalue()), collect

    def test_inert_diff_skips_suite(self):
        code, report, collect = self._run_main_with_diff(['docs/x.md'])
        self.assertEqual(code, trc.EXIT_PASS)
        self.assertTrue(report.get('gate_skipped'))
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['regressions'], [])
        collect.assert_not_called()  # the whole point: no suite run

    def test_code_diff_runs_suite(self):
        code, report, collect = self._run_main_with_diff(['scripts/x.py'])
        self.assertEqual(code, trc.EXIT_PASS)
        self.assertNotIn('gate_skipped', report)
        self.assertTrue(collect.called)  # suite ran (non-inert change)

    def test_git_failure_runs_suite_failsafe(self):
        # diff_changed_files → None (git error) must NOT skip — run the gate.
        code, report, collect = self._run_main_with_diff(None)
        self.assertNotIn('gate_skipped', report)
        self.assertTrue(collect.called)

    def test_no_skip_inert_flag_forces_suite(self):
        code, report, collect = self._run_main_with_diff(
            ['docs/x.md'], extra=('--no-skip-inert',))
        self.assertNotIn('gate_skipped', report)
        self.assertTrue(collect.called)


# -------------------- CI-delegated gate (JS/TS repos) --------------------

class IsCiDelegatedRepoTest(_IsolatedAgentsRoot):
    def _mk(self, pkg=None, with_scripts_tests=False):
        d = Path(tempfile.mkdtemp(prefix='ci-detect-'))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        if pkg is not None:
            (d / 'package.json').write_text(pkg, encoding='utf-8')
        if with_scripts_tests:
            (d / 'scripts' / 'tests').mkdir(parents=True)
        return d

    def test_js_repo_with_test_script_is_delegated(self):
        d = self._mk('{"scripts": {"test": "vitest run"}}')
        self.assertTrue(trc.is_ci_delegated_repo(d))

    def test_no_package_json_is_not_delegated(self):
        self.assertFalse(trc.is_ci_delegated_repo(self._mk(None)))

    def test_package_json_without_test_script_is_not_delegated(self):
        d = self._mk('{"scripts": {"build": "next build"}}')
        self.assertFalse(trc.is_ci_delegated_repo(d))

    def test_non_vitest_test_script_is_not_delegated(self):
        # a JS repo whose runner is not vitest must not be routed to a check
        # named 'vitest' that will never exist
        d = self._mk('{"scripts": {"test": "jest --ci"}}')
        self.assertFalse(trc.is_ci_delegated_repo(d))

    def test_scripts_tests_dir_forces_unittest_path(self):
        # a python repo that also carries a package.json stays on unittest
        d = self._mk('{"scripts": {"test": "vitest run"}}', with_scripts_tests=True)
        self.assertFalse(trc.is_ci_delegated_repo(d))

    def test_malformed_package_json_is_not_delegated(self):
        self.assertFalse(trc.is_ci_delegated_repo(self._mk('{not valid json')))


class RepoSlugTest(_IsolatedAgentsRoot):
    def _slug_for(self, url):
        completed = subprocess.CompletedProcess([], 0, stdout=url + '\n', stderr='')
        with patch.object(trc.subprocess, 'run', return_value=completed):
            return trc._repo_slug(Path('/tmp/x'))

    def test_ssh_url(self):
        self.assertEqual(
            self._slug_for('git@github.com:Larry-Yatch/ourliberty-dashboard.git'),
            'Larry-Yatch/ourliberty-dashboard')

    def test_https_url_with_suffix(self):
        self.assertEqual(
            self._slug_for('https://github.com/Larry-Yatch/ourliberty-dashboard.git'),
            'Larry-Yatch/ourliberty-dashboard')

    def test_https_url_without_suffix(self):
        self.assertEqual(
            self._slug_for('https://github.com/Larry-Yatch/ourliberty-dashboard'),
            'Larry-Yatch/ourliberty-dashboard')

    def test_git_failure_returns_none(self):
        with patch.object(trc.subprocess, 'run', side_effect=OSError):
            self.assertIsNone(trc._repo_slug(Path('/tmp/x')))


class FetchCiCheckTest(_IsolatedAgentsRoot):
    def _fetch(self, ndjson):
        completed = subprocess.CompletedProcess([], 0, stdout=ndjson, stderr='')
        with patch.object(trc.subprocess, 'run', return_value=completed):
            return trc._fetch_ci_check('o/r', 'sha')

    def test_picks_the_named_check(self):
        nd = '\n'.join([
            json.dumps({'name': 'Vercel', 'status': 'completed',
                        'conclusion': 'success', 'started_at': '2026-01-01T00:00:00Z'}),
            json.dumps({'name': 'vitest', 'status': 'completed',
                        'conclusion': 'success', 'started_at': '2026-01-01T00:00:00Z'}),
        ])
        self.assertEqual(self._fetch(nd)['name'], 'vitest')

    def test_newest_rerun_wins_by_id(self):
        # the re-run has a HIGHER id but an EARLIER started_at — id must win, so
        # started_at ordering can't let a stale run mask the re-run
        nd = '\n'.join([
            json.dumps({'name': 'vitest', 'id': 10, 'status': 'completed',
                        'conclusion': 'failure', 'started_at': '2026-01-02T00:00:00Z'}),
            json.dumps({'name': 'vitest', 'id': 20, 'status': 'completed',
                        'conclusion': 'success', 'started_at': '2026-01-01T00:00:00Z'}),
        ])
        self.assertEqual(self._fetch(nd)['conclusion'], 'success')

    def test_null_started_at_does_not_mask_newer_by_id(self):
        # a newer RED run with a missing started_at must not be masked by an
        # older green run — id ordering (not started_at) decides
        nd = '\n'.join([
            json.dumps({'name': 'vitest', 'id': 5, 'status': 'completed',
                        'conclusion': 'success', 'started_at': '2026-01-01T00:00:00Z'}),
            json.dumps({'name': 'vitest', 'id': 9, 'status': 'completed',
                        'conclusion': 'failure'}),  # no started_at
        ])
        self.assertEqual(self._fetch(nd)['conclusion'], 'failure')

    def test_ignores_non_github_actions_app(self):
        # a 'vitest' check posted by a foreign app is not trusted
        nd = '\n'.join([
            json.dumps({'name': 'vitest', 'id': 30, 'status': 'completed',
                        'conclusion': 'success',
                        'app': {'slug': 'some-third-party-bot'}}),
        ])
        self.assertIsNone(self._fetch(nd))

    def test_trusts_github_actions_app(self):
        nd = json.dumps({'name': 'vitest', 'id': 30, 'status': 'completed',
                         'conclusion': 'success', 'app': {'slug': 'github-actions'}})
        self.assertEqual(self._fetch(nd)['conclusion'], 'success')

    def test_absent_check_returns_none(self):
        nd = json.dumps({'name': 'Vercel', 'status': 'completed', 'conclusion': 'success'})
        self.assertIsNone(self._fetch(nd))

    def test_gh_failure_returns_none(self):
        with patch.object(trc.subprocess, 'run',
                          side_effect=subprocess.SubprocessError):
            self.assertIsNone(trc._fetch_ci_check('o/r', 'sha'))


class RunCiDelegatedGateTest(_IsolatedAgentsRoot):
    def _run(self, fetch, output='json', timeout=None):
        slug = patch.object(trc, '_repo_slug', return_value='o/r')
        sleep = patch.object(trc.time, 'sleep', return_value=None)
        if isinstance(fetch, list):
            fpatch = patch.object(trc, '_fetch_ci_check', side_effect=fetch)
        else:
            fpatch = patch.object(trc, '_fetch_ci_check', return_value=fetch)
        stdout, stderr = io.StringIO(), io.StringIO()
        ctxs = [slug, sleep, fpatch]
        if timeout is not None:
            ctxs.append(patch.object(trc, 'CI_CHECK_POLL_TIMEOUT_S', timeout))
        with contextlib.ExitStack() as stack:
            for c in ctxs:
                stack.enter_context(c)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = trc.run_ci_delegated_gate('p', 'h', Path('/tmp/x'), output)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_success_is_pass(self):
        code, out, _ = self._run(
            {'status': 'completed', 'conclusion': 'success', 'html_url': 'u'})
        self.assertEqual(code, trc.EXIT_PASS)
        rpt = json.loads(out)
        self.assertEqual(rpt['verdict'], 'PASS')
        self.assertEqual(rpt['gate_mode'], 'ci-delegated')

    def test_failure_is_block(self):
        code, out, _ = self._run(
            {'status': 'completed', 'conclusion': 'failure', 'html_url': 'u'})
        self.assertEqual(code, trc.EXIT_BLOCK)
        rpt = json.loads(out)
        self.assertEqual(rpt['verdict'], 'BLOCK')
        self.assertEqual(rpt['regressions'], ['ci:vitest=failure'])

    def test_skipped_is_analysis_fail_not_pass(self):
        # 'skipped' means tests did not run — never a silent PASS
        code, out, _ = self._run(
            {'status': 'completed', 'conclusion': 'skipped', 'html_url': 'u'})
        self.assertEqual(code, trc.EXIT_ANALYSIS_FAIL)
        self.assertEqual(out, '')  # no PASS/BLOCK report emitted

    def test_neutral_is_analysis_fail_not_pass(self):
        code, out, _ = self._run(
            {'status': 'completed', 'conclusion': 'neutral', 'html_url': 'u'})
        self.assertEqual(code, trc.EXIT_ANALYSIS_FAIL)
        self.assertEqual(out, '')

    def test_cancelled_is_block(self):
        code, _, _ = self._run(
            {'status': 'completed', 'conclusion': 'cancelled', 'html_url': 'u'})
        self.assertEqual(code, trc.EXIT_BLOCK)

    def test_missing_check_after_timeout_is_analysis_fail(self):
        code, _, err = self._run(None, timeout=0)
        self.assertEqual(code, trc.EXIT_ANALYSIS_FAIL)
        self.assertIn('no', err.lower())

    def test_still_pending_after_timeout_is_analysis_fail(self):
        code, _, _ = self._run(
            {'status': 'in_progress', 'conclusion': None}, timeout=0)
        self.assertEqual(code, trc.EXIT_ANALYSIS_FAIL)

    def test_pending_then_completes_passes(self):
        code, out, _ = self._run([
            {'status': 'in_progress', 'conclusion': None},
            {'status': 'completed', 'conclusion': 'success', 'html_url': 'u'},
        ])
        self.assertEqual(code, trc.EXIT_PASS)
        self.assertEqual(json.loads(out)['verdict'], 'PASS')

    def test_no_slug_is_analysis_fail(self):
        with patch.object(trc, '_repo_slug', return_value=None):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = trc.run_ci_delegated_gate('p', 'h', Path('/tmp/x'))
        self.assertEqual(code, trc.EXIT_ANALYSIS_FAIL)

    def test_text_output_renders(self):
        code, out, _ = self._run(
            {'status': 'completed', 'conclusion': 'failure', 'html_url': 'u'},
            output='text')
        self.assertEqual(code, trc.EXIT_BLOCK)
        self.assertIn('verdict:    BLOCK', out)


class MainRoutesJsRepoTest(_IsolatedAgentsRoot):
    def test_js_repo_routes_to_ci_gate_not_unittest(self):
        d = Path(tempfile.mkdtemp(prefix='ci-main-'))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / 'package.json').write_text('{"scripts":{"test":"vitest run"}}')
        argv = ['--parent-sha', 'p', '--head-sha', 'h', '--repo-root', str(d)]
        with patch.object(trc, 'resolve_sha', side_effect=lambda s, r: s * 8), \
             patch.object(trc, 'run_ci_delegated_gate',
                          return_value=trc.EXIT_PASS) as gate, \
             patch.object(trc, 'collect_failures_at_sha') as collect:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = trc.main(argv)
        self.assertEqual(code, trc.EXIT_PASS)
        gate.assert_called_once()
        # head canonical ('h'*8) is forwarded; the unittest path is never taken
        self.assertEqual(gate.call_args[0][1], 'h' * 8)
        self.assertFalse(collect.called)


if __name__ == '__main__':
    unittest.main()
