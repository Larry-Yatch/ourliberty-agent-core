#!/usr/bin/env python3
"""test_sandbox_env_restored_after_teardown.py — the sandbox redirect must
survive every test's tearDown.

THE LEAK (found 2026-07-29 while verifying PR #1052; present on clean main)
--------------------------------------------------------------------------
`_bootstrap` sets ``OURLIBERTY_AGENTS_ROOT`` ONCE per process to a sandbox
mkdtemp. A test class that overrides it in setUp and then ``os.environ.pop``s it
in tearDown — rather than restoring the value it was handed — does not return to
the sandbox, it returns to NOTHING. Every production module that resolves the
root at CALL time (rather than binding it at import) then falls through to its
hardcoded ``/home/larry/agents`` default, i.e. THE LIVE DROPLET TREE, for the
entire remainder of the run.

Observed symptom: on the droplet, from a clean origin/main worktree,

    python3 -m unittest scripts.tests.test_heal_pipeline_stall \
                        scripts.tests.test_system_state_log_escalation_count

failed all three `EscalationCountRegressionTest` cases (5 != 3, 4 != 2, and a
non-empty list where [] was expected) — while the count module ALONE passed.
The two surplus rows were not leftovers from the heal tests: they were
production's own open for-Larry records, read live by Source 1b of
`system_state_log.load_for_larry_escalations`
(`for_larry_signal.active_entries()`, which resolves its path at call time).
It could not reproduce on a Mac (no ``/home/larry/agents``) and only reproduced
on the droplet while production actually held open records — so in full-suite
runs it surfaced as an intermittent phantom regression.

WHAT THIS PINS
--------------
1. The two classes that popped instead of restoring now restore
   (`test_heal_pipeline_stall._TempAgentsRootMixin`,
   `test_task_cancel.AgentRunnerReaderParityTests`) — asserted by actually
   RUNNING one of their tests and checking the env afterwards, not by reading
   their source.
2. `_bootstrap`'s process-level backstop re-asserts a DELETED sandbox key after
   every test, so the next class to pop cannot reopen the class.
3. The backstop heals only deletion — a value a test or module deliberately set
   is left alone (three modules pin their own root at import scope).

Run:
    python3 -m unittest scripts.tests.test_sandbox_env_restored_after_teardown
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

_SANDBOX_KEYS = ('OURLIBERTY_AGENTS_ROOT', 'OURLIBERTY_WORKTREES_ROOT',
                 'OURLIBERTY_LOG_DIR')


def _run_case(case: unittest.TestCase) -> unittest.TestResult:
    """Run one already-constructed TestCase and return its result."""
    result = unittest.TestResult()
    case.run(result)
    return result


class SandboxKeySurvivesTeardownTest(unittest.TestCase):
    """The offenders, run for real — source inspection would not catch a
    tearDown that pops through a helper."""

    def _assert_survives(self, case: unittest.TestCase, label: str) -> None:
        """Drive setUp/tearDown DIRECTLY rather than through ``case.run()``.

        `_bootstrap`'s backstop hooks ``TestCase.run``, so going through run()
        would heal a popped key before we could observe it and this test could
        never fail — a false-clean. Calling the pair directly pins the class's
        OWN contract, independent of the backstop, so the two layers are
        genuinely separate evidence."""
        before = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        self.assertIsNotNone(
            before, 'precondition: _bootstrap must have set the sandbox root')
        try:
            case.setUp()
            try:
                self.assertEqual(
                    os.environ.get('OURLIBERTY_AGENTS_ROOT'),
                    str(getattr(case, 'agents_root', None) or case.root),
                    f'{label}: setUp did not install its own root')
            finally:
                case.tearDown()
            after = os.environ.get('OURLIBERTY_AGENTS_ROOT')
            self.assertIsNotNone(
                after,
                f'{label} left OURLIBERTY_AGENTS_ROOT UNSET — every later test '
                f'that resolves the root at call time now reads '
                f'/home/larry/agents (live production). Restore the value setUp '
                f'was handed; do not pop it.')
            self.assertEqual(
                after, before,
                f'{label} left OURLIBERTY_AGENTS_ROOT pointing at {after!r} '
                f'instead of the sandbox root {before!r}.')
        finally:
            # We bypass TestCase.run here, so the _bootstrap backstop does not
            # cover us. Put the sandbox root back ourselves — a failure in this
            # test must not become the very leak it asserts against.
            if os.environ.get('OURLIBERTY_AGENTS_ROOT') != before:
                os.environ['OURLIBERTY_AGENTS_ROOT'] = before

    def test_heal_pipeline_stall_mixin_restores_the_root(self) -> None:
        try:  # same dual-identity idiom every module here uses (see _bootstrap)
            from . import test_heal_pipeline_stall as mod
        except ImportError:
            import test_heal_pipeline_stall as mod
        case = mod.TestRegexPatterns(
            'test_forge_done_regex_matches_real_inbox_watcher_log_shape')
        self._assert_survives(case, 'test_heal_pipeline_stall._TempAgentsRootMixin')

    def test_task_cancel_parity_class_restores_the_root(self) -> None:
        try:
            from . import test_task_cancel as mod
        except ImportError:
            import test_task_cancel as mod
        case = mod.AgentRunnerReaderParityTests(
            'test_agent_runner_reads_what_this_module_writes')
        self._assert_survives(
            case, 'AgentRunnerReaderParityTests.tearDown')


class SandboxKeyReassertBackstopTest(unittest.TestCase):
    """`_bootstrap._arm_sandbox_key_reassert` — the process-level guard that
    keeps the NEXT class that pops from reopening the leak."""

    def test_deleted_key_is_reasserted_after_the_test_that_dropped_it(self) -> None:
        sandbox = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        self.assertIsNotNone(sandbox)

        class Dropper(unittest.TestCase):
            def runTest(self):  # noqa: N802 - unittest naming
                os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
                assert 'OURLIBERTY_AGENTS_ROOT' not in os.environ

        try:
            result = _run_case(Dropper())
            self.assertEqual(result.errors, [])
            self.assertEqual(
                os.environ.get('OURLIBERTY_AGENTS_ROOT'), sandbox,
                'the backstop did not re-assert the sandbox root after a test '
                'deleted it — _arm_sandbox_key_reassert is not armed')
        finally:
            # Restore unconditionally: if the backstop IS broken, this test must
            # not itself become the leak it is asserting against and take the
            # rest of the run down with it.
            os.environ['OURLIBERTY_AGENTS_ROOT'] = sandbox

    def test_backstop_does_not_clobber_a_value_a_test_set(self) -> None:
        """Three modules pin their own root at import scope and must keep it;
        the backstop heals DELETION only."""
        sandbox = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        pinned = '/tmp/zz-fixture-sandbox-reassert-root'

        class Pinner(unittest.TestCase):
            def runTest(self):  # noqa: N802 - unittest naming
                os.environ['OURLIBERTY_AGENTS_ROOT'] = pinned

        try:
            result = _run_case(Pinner())
            self.assertEqual(result.errors, [])
            self.assertEqual(
                os.environ.get('OURLIBERTY_AGENTS_ROOT'), pinned,
                'the backstop overwrote a root a test deliberately set')
        finally:
            if sandbox is not None:
                os.environ['OURLIBERTY_AGENTS_ROOT'] = sandbox

    def test_every_sandbox_key_is_covered(self) -> None:
        """Not just the root — the log dir and worktrees root fall through to
        production defaults the same way."""
        snapshot = {k: os.environ.get(k) for k in _SANDBOX_KEYS}

        class Dropper(unittest.TestCase):
            def runTest(self):  # noqa: N802 - unittest naming
                for key in _SANDBOX_KEYS:
                    os.environ.pop(key, None)

        try:
            result = _run_case(Dropper())
            self.assertEqual(result.errors, [])
            for key, value in snapshot.items():
                if value is None:
                    continue
                self.assertEqual(
                    os.environ.get(key), value,
                    f'{key} was not re-asserted after a test deleted it')
        finally:
            for key, value in snapshot.items():  # see the note above
                if value is not None:
                    os.environ[key] = value


class NoUnguardedSandboxPopTest(unittest.TestCase):
    """Static backstop to the two runtime ones: a tearDown that pops a sandbox
    key without having snapshotted it is the defect, and it is cheap to spot
    before it costs another bisect."""

    _ALLOWED_UNGUARDED = {
        # These pop INSIDE a test body to exercise the module's unset-fallback,
        # and their own _Base.tearDown restores the value setUp snapshotted.
        ('test_heal_stale_alert_triage.py', 'test_unset_override_falls_back_to_home'),
        ('test_heal_stale_approvals.py', 'test_unset_override_falls_back_to_home'),
    }

    def test_no_teardown_pops_a_sandbox_key_without_snapshotting_it(self) -> None:
        import re

        tests_dir = Path(__file__).resolve().parent
        offenders = []
        pop_re = re.compile(
            r"environ\.pop\(\s*['\"](OURLIBERTY_(?:AGENTS_ROOT|WORKTREES_ROOT|LOG_DIR))['\"]")
        for path in sorted(tests_dir.glob('test_*.py')):
            if path.name == Path(__file__).name:
                continue  # this module drops keys on purpose, to prove the backstop
            lines = path.read_text().splitlines()
            for i, line in enumerate(lines):
                m = pop_re.search(line)
                if not m:
                    continue
                # Guarded restore idiom: `if <snapshot> is None: pop else: set`.
                if i and 'is None' in lines[i - 1]:
                    continue
                if i and lines[i - 1].strip() == 'else:':
                    continue
                # Walk back to the enclosing `def` — both to name the offender
                # and to see whether the pop sits under a context manager that
                # restores the whole environ on exit.
                span_start = 0
                for j in range(i - 1, -1, -1):
                    if lines[j].strip().startswith('def '):
                        span_start = j
                        break
                span = lines[span_start:i]
                if any('patch.dict(os.environ' in ln for ln in span):
                    continue  # mock.patch.dict restores the environ on exit
                enclosing = lines[span_start].strip() if span else ''
                fname = enclosing[4:].split('(')[0] if enclosing.startswith('def ') else ''
                if (path.name, fname) in self._ALLOWED_UNGUARDED:
                    continue
                offenders.append(f'{path.name}:{i + 1} (in {fname or "?"}): {line.strip()}')

        self.assertEqual(
            offenders, [],
            'These pop a sandbox key without restoring the value they were '
            'handed. _bootstrap sets it once per process; popping it drops the '
            'redirect for the whole run and later tests read /home/larry/agents '
            '(live production). Snapshot in setUp and restore in tearDown:\n  '
            + '\n  '.join(offenders))


if __name__ == '__main__':
    unittest.main()
