#!/usr/bin/env python3
"""
Regression fixtures for the harness identity-landmine scrub.

Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core
(2026-05-11, Phase D2.5 — pulled as part of Gap 9 hardening before the watcher
migration to agent_runner.run_claude).

Context: 2026-04-16 (upstream). Four consecutive Luma dispatches were mis-routed
into Prism's identity container because Claude Code's project-memory walk-up
picked up a stale root-owned /tmp/CLAUDE.md (placed weeks earlier by a manual
`sudo cp`). The harness now scrubs /tmp top-level CLAUDE.md / AGENTS.md /
IDENTITY.md on every subprocess spawn, and optionally prepends an
IDENTITY_ASSERTION_MARKER preamble when the task envelope declares an
``expected_agent``.

These fixtures cover the three scenarios upstream Atlas flagged:
    - evicts a landmine planted in the scrub root
    - never removes a CLAUDE.md inside the legitimate worktree cwd
    - is a no-op when the scrub root is already clean (idempotent)

Plus coverage for the assertion-preamble builder so future refactors
don't accidentally break its shape, and ordering coverage that the scrub
fires before subprocess.run on every spawn.

Run:
    python3 /home/larry/agent-core/scripts/tests/test_identity_landmine_scrub.py
    # or
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_identity_landmine_scrub
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Larry's fork has no separate runtime-scripts dir (no atomic-swap to a
# different path); _REPO_SCRIPTS is the only resolution path.
_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if _REPO_SCRIPTS.exists() and str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


class ScrubEvictsLandmines(unittest.TestCase):
    """Fixture 1: every identity-autoload name in the scrub root is removed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='landmine-scrub-'))
        self.captured = []

        def capture(agent_id, msg, level='INFO'):
            self.captured.append((agent_id, msg, level))

        self._log_fn = capture

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_evicts_all_three_landmine_names(self):
        import agent_runner
        # Plant all three landmines that Claude Code auto-loads.
        for name in agent_runner._IDENTITY_LANDMINE_NAMES:
            (self.tmp / name).write_text('# stale identity from sudo cp\n')

        reports = agent_runner.scrub_tmp_identity_landmines(
            tmp_root=self.tmp, log_fn=self._log_fn)

        for name in agent_runner._IDENTITY_LANDMINE_NAMES:
            self.assertFalse(
                (self.tmp / name).exists(),
                'expected ' + name + ' to be evicted from scrub root')

        removed = [r for r in reports if r[1] == 'REMOVED']
        self.assertEqual(len(removed), 3,
                         'expected 3 REMOVED report rows, got ' + str(reports))

        # Every eviction must emit a WARN-level structured log line.
        warn_lines = [m for _, m, lvl in self.captured if lvl == 'WARN']
        self.assertEqual(len(warn_lines), 3)
        for line in warn_lines:
            self.assertIn('IDENTITY_LANDMINE_EVICTED', line)

    def test_evicts_only_the_landmine_names(self):
        """A file named something else in /tmp must be left alone."""
        import agent_runner
        (self.tmp / 'CLAUDE.md').write_text('landmine')
        (self.tmp / 'README.md').write_text('not a landmine')
        (self.tmp / 'wt-main-abc').mkdir()  # pretend a real worktree

        agent_runner.scrub_tmp_identity_landmines(
            tmp_root=self.tmp, log_fn=self._log_fn)

        self.assertFalse((self.tmp / 'CLAUDE.md').exists())
        self.assertTrue((self.tmp / 'README.md').exists(),
                        'non-landmine README.md must survive')
        self.assertTrue((self.tmp / 'wt-main-abc').is_dir(),
                        'existing worktree dir must survive')


class ScrubPreservesWorktreeIdentity(unittest.TestCase):
    """Fixture 2: the legitimate CLAUDE.md INSIDE a worktree is never touched.

    This is the single most important scope guarantee — if the scrub ever
    walked into worktree subdirs it would delete the legitimate project-
    context CLAUDE.md from a real repo and break every task.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='landmine-scrub-preserve-'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_worktree_claude_md_is_preserved(self):
        import agent_runner
        # Simulate a /tmp/wt-<agent>-<task>/ worktree with a checked-in CLAUDE.md
        worktree = self.tmp / 'wt-forge-my-task'
        worktree.mkdir()
        legitimate = worktree / 'CLAUDE.md'
        legitimate.write_text('# Project memory\n\nYou are Forge.\n')

        # Plant a landmine at the scrub root alongside the worktree.
        (self.tmp / 'CLAUDE.md').write_text('# Stale Beacon identity\n')

        agent_runner.scrub_tmp_identity_landmines(
            tmp_root=self.tmp, log_fn=lambda *a, **k: None)

        self.assertFalse(
            (self.tmp / 'CLAUDE.md').exists(),
            'landmine at scrub root must be removed')
        self.assertTrue(
            legitimate.exists(),
            'CLAUDE.md inside the worktree must be preserved')
        self.assertIn('Forge', legitimate.read_text(),
                      'worktree CLAUDE.md contents must be untouched')


class ScrubIdempotentNoOp(unittest.TestCase):
    """Fixture 3: scrub on a clean root does nothing and logs nothing.

    Important because the scrub runs on EVERY subprocess spawn. If it were
    noisy on the clean path it would flood WARN-level logs and train
    watchdogs to ignore the signal.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='landmine-scrub-idempotent-'))
        self.captured = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self, agent_id, msg, level='INFO'):
        self.captured.append((agent_id, msg, level))

    def test_clean_root_is_silent(self):
        import agent_runner
        reports = agent_runner.scrub_tmp_identity_landmines(
            tmp_root=self.tmp, log_fn=self._log)
        self.assertEqual(reports, [],
                         'expected empty report on clean root, got ' + str(reports))
        self.assertEqual(self.captured, [],
                         'expected zero log lines on clean root, got ' + str(self.captured))

    def test_second_call_after_evict_is_silent(self):
        """First call evicts; second call must be a no-op."""
        import agent_runner
        (self.tmp / 'CLAUDE.md').write_text('landmine')

        first = agent_runner.scrub_tmp_identity_landmines(
            tmp_root=self.tmp, log_fn=self._log)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0][1], 'REMOVED')

        # Reset the capture buffer between calls so we can assert the
        # second call is silent.
        self.captured.clear()

        second = agent_runner.scrub_tmp_identity_landmines(
            tmp_root=self.tmp, log_fn=self._log)
        self.assertEqual(second, [],
                         'second call must be a no-op, got ' + str(second))
        self.assertEqual(self.captured, [],
                         'second call must not log, got ' + str(self.captured))


class ExpectedAgentAssertionBuilder(unittest.TestCase):
    """Fixture 4: the opt-in identity-assertion preamble has a stable shape.

    The watcher prepends this block to any task whose envelope carries
    `expected_agent: '<id>'` (in Larry's fork: inferred from inbox path).
    The subprocess is instructed to self-catch any identity mismatch and
    respond with a single ``IDENTITY_MISMATCH`` line. Downstream watchdogs
    grep for both the marker and the mismatch line, so breaking either
    string breaks production triage.
    """

    def test_marker_present(self):
        import agent_runner
        block = agent_runner.build_expected_agent_assertion('pulse')
        self.assertIn(agent_runner.IDENTITY_ASSERTION_MARKER, block)

    def test_agent_id_is_lowercased_and_interpolated(self):
        import agent_runner
        block = agent_runner.build_expected_agent_assertion('  PULSE  ')
        # Whitespace must be stripped and casing normalized so the
        # subprocess comparison is deterministic.
        self.assertIn('`pulse`', block)
        self.assertNotIn('PULSE', block)

    def test_refusal_line_is_stable(self):
        import agent_runner
        block = agent_runner.build_expected_agent_assertion('pulse')
        # Watchdogs grep for this literal shape. Do not reformat without
        # updating the downstream grep patterns.
        self.assertIn('IDENTITY_MISMATCH: expected=pulse', block)


class RunClaudeInvokesScrub(unittest.TestCase):
    """Fixture 5: the scrub fires on every run_claude() attempt.

    Regression lock: the scrub must be called BEFORE the subprocess that
    spawns claude, otherwise the landmine poisons the very spawn that is
    supposed to be protected by it.

    Note on the mock target: Larry's fork uses subprocess.Popen (with poll-
    based cancel-marker support) rather than upstream's subprocess.run.
    This test patches Popen accordingly.
    """

    def test_scrub_called_before_subprocess(self):
        import agent_runner
        import larry_alerts
        from unittest import mock

        order = []

        def fake_scrub(tmp_root=None, log_fn=None):
            order.append('scrub')
            return []

        class _FakeStdin:
            def write(self, _data):
                pass
            def close(self):
                pass

        class _FakeStream:
            def __init__(self, text):
                self._text = text
            def read(self):
                return self._text

        class _FakeProc:
            def __init__(self):
                self.pid = 99999
                self.stdin = _FakeStdin()
                self.stdout = _FakeStream(
                    '{"result": "ok", "session_id": "s1", "total_cost_usd": 0.0}')
                self.stderr = _FakeStream('')
                self.returncode = 0
                self._polled = False
            def poll(self):
                # Return None once (so the poll loop body runs at most once),
                # then 0 on subsequent calls. This exercises the cancel-check
                # branch but exits immediately.
                if not self._polled:
                    self._polled = True
                    return 0  # done immediately
                return 0
            def terminate(self):
                pass
            def kill(self):
                pass
            def wait(self, timeout=None):
                return 0

        def fake_popen(*args, **kwargs):
            order.append('subprocess')
            return _FakeProc()

        fake_tm = mock.Mock()
        fake_tm.get_token.return_value = ('tok', 'acct')
        fake_tm.check_for_rate_limit.return_value = False
        fake_tm.detect_cap_in_output.return_value = False
        fake_tm.report_success = mock.Mock()

        fake_guard = mock.Mock()
        fake_guard.wait_for_slot.return_value = True
        fake_guard.active_count.return_value = 0

        with mock.patch.object(agent_runner, 'scrub_tmp_identity_landmines',
                               side_effect=fake_scrub), \
             mock.patch.object(agent_runner.subprocess, 'Popen',
                               side_effect=fake_popen), \
             mock.patch.object(agent_runner.active_tier, 'select_dispatch_tier',
                               return_value='tier1'), \
             mock.patch.object(agent_runner, 'get_manager',
                               return_value=fake_tm, create=True), \
             mock.patch.object(agent_runner, 'get_guard',
                               return_value=fake_guard, create=True), \
             mock.patch.object(agent_runner, 'get_agent_model',
                               return_value=('sonnet', 'sonnet'), create=True), \
             mock.patch.object(agent_runner, 'quarantine_parent_claude_md_poison',
                               return_value=[]), \
             mock.patch.object(larry_alerts, 'append_alert'), \
             mock.patch.object(agent_runner, 'log', create=True):
            success, output, sid = agent_runner.run_claude(
                'pulse', 'hello world this is a test prompt', timeout=5)

        self.assertTrue(success,
                        'run_claude should report success when subprocess returncode is 0 + valid JSON')
        self.assertEqual(output, 'ok')
        self.assertEqual(sid, 's1')
        self.assertEqual(order[:2], ['scrub', 'subprocess'],
                         'scrub must run BEFORE the subprocess spawn, got ' + str(order))


def _main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ScrubEvictsLandmines,
                ScrubPreservesWorktreeIdentity,
                ScrubIdempotentNoOp,
                ExpectedAgentAssertionBuilder,
                RunClaudeInvokesScrub):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    sys.exit(_main())
