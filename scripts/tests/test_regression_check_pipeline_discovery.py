#!/usr/bin/env python3
"""Tests for the worktree-driven suite-layout discovery in
scripts/test_regression_check.py (graph-gate-pipeline-discovery-001).

The gate historically hard-coded `unittest discover -s scripts/tests`, so a
repo whose tests live in `pipeline/test_*.py` (the ourliberty-graph shape — no
scripts/tests dir, no package.json/CI) discovered nothing and exited 2
(analysis-fail) at both SHAs, producing no verdict. `discover_start_dir` now
picks the start dir from what exists in the worktree, and both the full-suite
runner and the single-test isolation runner honor it.

Covers:
  - discover_start_dir precedence (scripts/tests wins; pipeline only when it
    holds test_*.py; fallback to scripts/tests when neither shape is present).
  - run_tests_in_dir builds `-s pipeline` for the pipeline layout and the
    unchanged `-s scripts/tests` for agent-core's layout.
  - run_single_test_in_dir cds into the detected dir and strips the matching
    dotted prefix (`pipeline.` / `scripts.tests.`) before invoking unittest.

Subprocess invocations are mocked at the subprocess.run boundary; the wall
prefix is disabled (return []) so these exercise the discovery path directly.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_regression_check_pipeline_discovery
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import test_regression_check as trc  # noqa: E402


def _completed(stdout: str = '', stderr: str = '', returncode: int = 0,
               args=('python3', '-m', 'unittest')):
    return subprocess.CompletedProcess(args=list(args), returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _ok_output(n_tests: int = 5) -> str:
    """A clean unittest -v blob with the mandatory 'Ran N tests' sentinel."""
    return f'\nRan {n_tests} tests in 0.123s\nOK\n'


def _make_layout(root: Path, *, scripts_tests: bool = False,
                 pipeline_tests: bool = False,
                 pipeline_no_tests: bool = False) -> None:
    """Materialize the requested suite layout under ``root``."""
    if scripts_tests:
        (root / 'scripts' / 'tests').mkdir(parents=True, exist_ok=True)
        (root / 'scripts' / 'tests' / 'test_a.py').write_text('# test\n')
    if pipeline_tests:
        (root / 'pipeline').mkdir(parents=True, exist_ok=True)
        (root / 'pipeline' / 'test_thing.py').write_text('# test\n')
    if pipeline_no_tests:
        (root / 'pipeline').mkdir(parents=True, exist_ok=True)
        (root / 'pipeline' / 'librarian.py').write_text('# not a test\n')


class DiscoverStartDirTest(unittest.TestCase):
    """Layout detection is driven by worktree contents, not a repo allowlist."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='discover-start-dir-')
        self.root = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_scripts_tests_present_returns_scripts_tests(self):
        _make_layout(self.root, scripts_tests=True)
        self.assertEqual(trc.discover_start_dir(self.root), 'scripts/tests')

    def test_pipeline_with_tests_and_no_scripts_tests_returns_pipeline(self):
        _make_layout(self.root, pipeline_tests=True)
        self.assertEqual(trc.discover_start_dir(self.root), 'pipeline')

    def test_scripts_tests_wins_when_both_present(self):
        _make_layout(self.root, scripts_tests=True, pipeline_tests=True)
        self.assertEqual(trc.discover_start_dir(self.root), 'scripts/tests')

    def test_pipeline_without_test_files_falls_back_to_scripts_tests(self):
        """A pipeline/ package that ships no test_*.py is not a test layout —
        detection must not route to it (would discover 0 tests and mask a real
        suite elsewhere)."""
        _make_layout(self.root, pipeline_no_tests=True)
        self.assertEqual(trc.discover_start_dir(self.root), 'scripts/tests')

    def test_neither_layout_falls_back_to_scripts_tests(self):
        """No supported layout → keep the historical default so any repo that is
        neither shape behaves exactly as before this change."""
        self.assertEqual(trc.discover_start_dir(self.root), 'scripts/tests')


class RunTestsInDirDiscoveryTargetTest(unittest.TestCase):
    """run_tests_in_dir points `discover -s` at the detected layout."""

    def setUp(self):
        p = patch.object(trc, '_discover_wall_prefix', return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self._tmp = tempfile.mkdtemp(prefix='run-tests-discovery-')
        self.root = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_and_capture_argv(self) -> list[str]:
        completed = _completed(stdout=_ok_output(), returncode=0)
        with patch.object(trc.subprocess, 'run', return_value=completed) as m:
            trc.run_tests_in_dir(self.root, 60, self.root / 'iso')
        # Wall prefix is [], so the discover argv is the first positional arg.
        return list(m.call_args.args[0])

    def test_pipeline_layout_discovers_pipeline(self):
        _make_layout(self.root, pipeline_tests=True)
        argv = self._run_and_capture_argv()
        self.assertIn('discover', argv)
        self.assertEqual(argv[argv.index('-s') + 1], 'pipeline')

    def test_scripts_tests_layout_discovers_scripts_tests_unchanged(self):
        _make_layout(self.root, scripts_tests=True)
        argv = self._run_and_capture_argv()
        self.assertEqual(
            argv,
            ['python3', '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-v'],
        )


class RunSingleTestDiscoveryTargetTest(unittest.TestCase):
    """The single-test isolation runner honors the detected layout too — it cds
    into <workdir>/<start_dir> and strips the matching dotted prefix."""

    def setUp(self):
        p = patch.object(trc, '_discover_wall_prefix', return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self._tmp = tempfile.mkdtemp(prefix='run-single-discovery-')
        self.root = Path(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_pipeline_layout_cwd_and_bare_id(self):
        _make_layout(self.root, pipeline_tests=True)
        completed = _completed(stdout=_ok_output(), returncode=0)
        with patch.object(trc.subprocess, 'run', return_value=completed) as m:
            passed, _ = trc.run_single_test_in_dir(
                self.root, 'pipeline.test_thing.TCase.test_x', {}, 60,
            )
        self.assertTrue(passed)
        self.assertEqual(m.call_args.kwargs['cwd'], str(self.root / 'pipeline'))
        # The 'pipeline.' prefix is stripped to a bare, cwd-relative module id.
        self.assertEqual(m.call_args.args[0][-1], 'test_thing.TCase.test_x')

    def test_scripts_tests_layout_cwd_and_bare_id_unchanged(self):
        _make_layout(self.root, scripts_tests=True)
        completed = _completed(stdout=_ok_output(), returncode=0)
        with patch.object(trc.subprocess, 'run', return_value=completed) as m:
            passed, _ = trc.run_single_test_in_dir(
                self.root, 'scripts.tests.test_a.TCase.test_x', {}, 60,
            )
        self.assertTrue(passed)
        self.assertEqual(
            m.call_args.kwargs['cwd'], str(self.root / 'scripts' / 'tests'),
        )
        self.assertEqual(m.call_args.args[0][-1], 'test_a.TCase.test_x')


if __name__ == '__main__':
    unittest.main()
