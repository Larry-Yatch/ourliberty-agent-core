#!/usr/bin/env python3
"""test_main_suite_guardian.py — PR-1 tests for the Main-Suite Green Guardian.

Covers (spec § 8, PR-1):
  * four-way classification incl. env-signature screen + canary short-circuit;
  * registry transitions (new red, flip -> unstable/park, green -> recovered,
    previously-green derivation);
  * run_guardian orchestration with a FAKE invoker — SHA-skip, canary-failed,
    infra-flake retry, step-change branch, normal classify/record, green
    baseline hygiene;
  * the NEW run_single_test_in_dir isolation runner (prefix-strip + fail-closed);
  * the warmer single-flight lock relocation to the absolute path (L7).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_main_suite_guardian
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import main_suite_guardian as g  # noqa: E402
import regression_baseline_cache as baseline_cache  # noqa: E402
import test_regression_check as trc  # noqa: E402


# --- fake invoker ------------------------------------------------------------

class FakeInvoker:
    """Injectable stand-in for DefaultInvoker — no worktree, no lock, no I/O."""

    def __init__(
        self, *, sha='a' * 40, canary_ok=True, canary_detail='',
        red_sets=None, single_results=None, collect_errors=0,
    ):
        self.sha = sha
        self._canary_ok = canary_ok
        self._canary_detail = canary_detail
        self._red_sets = [set(s) for s in (red_sets if red_sets is not None else [set()])]
        self._collect_idx = 0
        self._single = dict(single_results or {})
        self._collect_errors = collect_errors
        self.setup_called = False
        self.teardown_called = False
        self.store_calls = []

    def resolve_sha(self):
        return self.sha

    def setup(self, sha):
        self.setup_called = True

    def run_canary(self):
        return self._canary_ok, self._canary_detail

    def collect_failures(self):
        if self._collect_errors > 0:
            self._collect_errors -= 1
            raise trc.AnalysisError('simulated run-level failure')
        idx = min(self._collect_idx, len(self._red_sets) - 1)
        self._collect_idx += 1
        return set(self._red_sets[idx])

    def run_single(self, test_id):
        return self._single.get(test_id, (False, ''))

    def store_green_baseline(self, sha):
        self.store_calls.append(sha)

    def teardown(self):
        self.teardown_called = True


def _run(invoker, registry_path, **kw):
    return g.run_guardian(
        Path('/nonexistent-repo'), invoker=invoker,
        registry_path=registry_path, now=None, **kw,
    )


class _TmpRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.registry_path = self.tmp / 'main-suite-guardian.json'

    def _load(self):
        return json.loads(self.registry_path.read_text('utf-8'))


# --- pure classification -----------------------------------------------------

class ClassificationTest(unittest.TestCase):

    def test_env_signature_detects_import_error(self):
        self.assertTrue(g.has_env_signature('ModuleNotFoundError: no foo'))
        self.assertTrue(g.has_env_signature('boom ImportError happened'))
        self.assertTrue(g.has_env_signature('missing SUPABASE service-role key'))

    def test_env_signature_absent_on_plain_assertion(self):
        self.assertFalse(g.has_env_signature(
            'AssertionError: 1 != 2\nRan 1 test in 0.0s\nFAILED (failures=1)'
        ))

    def test_passes_alone_is_order_flake(self):
        self.assertEqual(
            g.classify_red(passed_alone=True, output='',
                           previously_green=True), g.CLS_ORDER_FLAKE,
        )

    def test_fails_alone_with_env_signature_is_env_fail(self):
        self.assertEqual(
            g.classify_red(passed_alone=False,
                           output='ImportError: no module named x',
                           previously_green=True), g.CLS_ENV_FAIL,
        )

    def test_fails_alone_previously_green_is_genuine_break(self):
        self.assertEqual(
            g.classify_red(passed_alone=False, output='AssertionError',
                           previously_green=True), g.CLS_GENUINE_BREAK,
        )

    def test_fails_alone_not_previously_green_is_backlog(self):
        self.assertEqual(
            g.classify_red(passed_alone=False, output='AssertionError',
                           previously_green=False), g.CLS_BACKLOG,
        )

    def test_canary_failure_forces_env_fail(self):
        # Even a passes-alone red is env-fail if the canary preflight failed.
        self.assertEqual(
            g.classify_red(passed_alone=True, output='',
                           previously_green=True, canary_ok=False),
            g.CLS_ENV_FAIL,
        )


# --- registry helper functions ----------------------------------------------

class RegistryHelperTest(unittest.TestCase):

    def test_should_skip_same_sha_after_conclusive_green(self):
        reg = g.new_registry()
        reg['_meta'].update(last_sha='x' * 40, last_run_result=g.RUN_GREEN)
        self.assertTrue(g.should_skip_sha('x' * 40, reg))

    def test_no_skip_on_new_sha(self):
        reg = g.new_registry()
        reg['_meta'].update(last_sha='x' * 40, last_run_result=g.RUN_GREEN)
        self.assertFalse(g.should_skip_sha('y' * 40, reg))

    def test_no_skip_after_inconclusive_result(self):
        reg = g.new_registry()
        reg['_meta'].update(last_sha='x' * 40, last_run_result=g.RUN_INFRA_FLAKE)
        self.assertFalse(g.should_skip_sha('x' * 40, reg))
        reg['_meta']['last_run_result'] = g.RUN_CANARY_FAILED
        self.assertFalse(g.should_skip_sha('x' * 40, reg))

    def test_no_skip_when_never_run(self):
        self.assertFalse(g.should_skip_sha('x' * 40, g.new_registry()))

    def test_previously_green_from_ever_green_entry(self):
        reg = g.new_registry()
        reg['tests']['t.C.m'] = {'ever_green': True}
        self.assertTrue(g.is_previously_green('t.C.m', reg, 5))

    def test_previously_green_new_test_after_prior_run(self):
        # No entry + guardian has completed >=1 prior full run => was green then.
        self.assertTrue(g.is_previously_green('t.C.m', g.new_registry(), 1))

    def test_not_previously_green_on_first_ever_run(self):
        self.assertFalse(g.is_previously_green('t.C.m', g.new_registry(), 0))

    def test_not_previously_green_for_never_green_entry(self):
        # Legacy entry predating `origin_run` — conservative False, unchanged.
        reg = g.new_registry()
        reg['tests']['t.C.m'] = {'ever_green': False}
        self.assertFalse(g.is_previously_green('t.C.m', reg, 5))

    def test_previously_green_survives_its_own_entry(self):
        # THE DEMOTION BUG. A test first seen red on run 11 was green on runs
        # 1-10, so it is a genuine break. Re-deriving from `ever_green` (False —
        # a broken test never goes green on its own) demoted it to backlog on
        # the SECOND night, one night before escalation is allowed to fire.
        reg = g.new_registry()
        reg['tests']['t.C.m'] = {'ever_green': False, 'origin_run': 11}
        self.assertTrue(g.is_previously_green('t.C.m', reg, 12))

    def test_bootstrap_red_stays_debt_forever(self):
        # The day-one flood protection this must NOT weaken: a test already red
        # on the guardian's first-ever run is inherited debt, not a break.
        reg = g.new_registry()
        reg['tests']['t.C.m'] = {'ever_green': False, 'origin_run': 0}
        self.assertFalse(g.is_previously_green('t.C.m', reg, 17))

    def test_origin_run_true_is_not_read_as_one(self):
        # bool is an int subclass; a stray True must not read as origin_run >= 1.
        reg = g.new_registry()
        reg['tests']['t.C.m'] = {'ever_green': False, 'origin_run': True}
        self.assertFalse(g.is_previously_green('t.C.m', reg, 5))

    def test_break_survives_two_nights_and_reaches_escalation(self):
        # End-to-end on the exact sequence that was impossible before: the same
        # test red on two consecutive runs must still be a genuine break on the
        # second, because that is when `should_escalate_break` is allowed to fire.
        # Nothing tested two runs against one test, which is why this survived
        # review — the pieces all pass in isolation.
        reg = g.new_registry()
        reg['_meta']['completed_runs'] = 11
        tid = 't.C.m'
        for run in (11, 12):
            cls = g.classify_red(
                passed_alone=False, output='AssertionError: boom',
                previously_green=g.is_previously_green(tid, reg, run),
            )
            g.record_red(reg, tid, cls, sha='a' * 40,
                         now_iso='2026-07-28T00:00:00+00:00',
                         completed_runs_before=run)
        entry = reg['tests'][tid]
        self.assertEqual(entry['classification'], g.CLS_GENUINE_BREAK)
        self.assertEqual(entry['consecutive_red_runs'], 2)
        self.assertTrue(g.should_escalate_break(entry))
        # ...and it never flip-flopped into `unstable` on the way (flip_count
        # bumps on a classification CHANGE, which is what the demotion was).
        self.assertEqual(entry.get('flip_count', 0), 0)

    def test_live_demoted_entry_recovers_to_break_without_parking(self):
        # THE MIGRATION PATH — the only path that exists on the real system, and
        # the one every other test here misses by starting from an empty
        # registry. Seeded from the VERBATIM live shape: an entry already sitting
        # in `backlog` with `flip_count: 1` banked from the demotion this fix
        # undoes. Counting the correction as a second flip parked all four live
        # tests as `unstable` instead of escalating them.
        reg = g.new_registry()
        reg['_meta']['completed_runs'] = 17
        tid = 't.C.m'
        reg['tests'][tid] = {
            'classification': g.CLS_BACKLOG, 'flip_count': 1, 'parked': False,
            'ever_green': False, 'origin_run': 11,
            'consecutive_red_runs': 17, 'consecutive_green_runs': 0,
            'history': [],
        }
        cls = g.classify_red(
            passed_alone=False, output='AssertionError: boom',
            previously_green=g.is_previously_green(tid, reg, 17),
        )
        self.assertEqual(cls, g.CLS_GENUINE_BREAK)
        entry = g.record_red(reg, tid, cls, sha='a' * 40,
                             now_iso='2026-07-29T00:00:00+00:00',
                             completed_runs_before=17)
        # It must SURVIVE record_red as a break — not be parked as unstable.
        self.assertEqual(entry['classification'], g.CLS_GENUINE_BREAK)
        self.assertFalse(entry['parked'])
        self.assertEqual(entry['flip_count'], 1)  # not bumped by the correction
        self.assertTrue(g.should_escalate_break(entry))
        # ...and it must not poison the graduation gate, which requires zero
        # unstable tests to produce a stage-0 -> 1 candidate.
        self.assertEqual(g._unstable_count(reg), 0)

    def test_genuine_instability_still_parks(self):
        # The guard this must NOT weaken: a test that really does flip between
        # ACTIONABLE categories still trips at 2 and parks.
        reg = g.new_registry()
        tid = 't.C.m'
        for cls in (g.CLS_GENUINE_BREAK, g.CLS_ORDER_FLAKE,
                    g.CLS_GENUINE_BREAK):
            entry = g.record_red(reg, tid, cls, sha='a' * 40,
                                 now_iso='2026-07-29T00:00:00+00:00',
                                 completed_runs_before=5)
        self.assertEqual(entry['classification'], g.CLS_UNSTABLE)
        self.assertTrue(entry['parked'])

    def test_bootstrap_debt_never_escalates_across_runs(self):
        # Same loop, origin run 0: stays backlog, never becomes escalatable.
        reg = g.new_registry()
        tid = 't.C.m'
        for run in (0, 1, 2):
            cls = g.classify_red(
                passed_alone=False, output='AssertionError: boom',
                previously_green=g.is_previously_green(tid, reg, run),
            )
            g.record_red(reg, tid, cls, sha='a' * 40,
                         now_iso='2026-07-28T00:00:00+00:00',
                         completed_runs_before=run)
        entry = reg['tests'][tid]
        self.assertEqual(entry['classification'], g.CLS_BACKLOG)
        self.assertFalse(g.should_escalate_break(entry))

    def test_select_new_reds_and_step_change(self):
        reg = g.new_registry()
        reg['tests']['old.C.m'] = {'consecutive_red_runs': 3}
        reds = {'old.C.m', 'new1.C.m', 'new2.C.m'}
        self.assertEqual(g.select_new_reds(reds, reg), {'new1.C.m', 'new2.C.m'})
        self.assertFalse(g.is_step_change({'a', 'b', 'c', 'd', 'e'}))
        self.assertTrue(g.is_step_change({'a', 'b', 'c', 'd', 'e', 'f'}))


# --- registry transitions ----------------------------------------------------

class RegistryTransitionTest(unittest.TestCase):

    def test_new_red_entry_shape(self):
        reg = g.new_registry()
        e = g.record_red(reg, 't.C.m', g.CLS_BACKLOG, sha='s' * 40,
                         now_iso='2026-07-08T00:00:00+00:00',
                         completed_runs_before=2)
        self.assertEqual(e['classification'], g.CLS_BACKLOG)
        self.assertEqual(e['consecutive_red_runs'], 1)
        self.assertEqual(e['origin_run'], 2)
        self.assertFalse(e['ever_green'])
        self.assertEqual(len(e['history']), 1)

    def test_flip_count_promotes_to_unstable_and_parks(self):
        # Seeded from an ACTIONABLE class, not `backlog`. This originally opened
        # on `backlog` purely as convenient flip-fodder, but leaving a holding
        # lane is no longer counted as instability (see `record_red`) — so that
        # seed would now measure one real flip, not two. The assertions, which
        # are what this test is actually about, are unchanged.
        reg = g.new_registry()
        iso = '2026-07-08T00:00:00+00:00'
        g.record_red(reg, 't.C.m', g.CLS_ORDER_FLAKE, sha='s' * 40, now_iso=iso,
                     completed_runs_before=1)
        g.record_red(reg, 't.C.m', g.CLS_GENUINE_BREAK, sha='s' * 40,
                     now_iso=iso, completed_runs_before=1)  # flip 1
        e = g.record_red(reg, 't.C.m', g.CLS_ORDER_FLAKE, sha='s' * 40,
                         now_iso=iso, completed_runs_before=1)  # flip 2
        self.assertEqual(e['flip_count'], 2)
        self.assertEqual(e['classification'], g.CLS_UNSTABLE)
        self.assertTrue(e['parked'])

    def test_leaving_backlog_is_not_a_flip(self):
        # The narrow behaviour change, pinned directly: backlog -> actionable
        # banks no flip, so a demoted test can be corrected without being parked.
        reg = g.new_registry()
        iso = '2026-07-08T00:00:00+00:00'
        g.record_red(reg, 't.C.m', g.CLS_BACKLOG, sha='s' * 40, now_iso=iso,
                     completed_runs_before=1)
        e = g.record_red(reg, 't.C.m', g.CLS_GENUINE_BREAK, sha='s' * 40,
                         now_iso=iso, completed_runs_before=1)
        self.assertEqual(e['flip_count'], 0)
        self.assertEqual(e['classification'], g.CLS_GENUINE_BREAK)
        self.assertFalse(e['parked'])

    def test_green_twice_recovers(self):
        reg = g.new_registry()
        iso = '2026-07-08T00:00:00+00:00'
        g.record_red(reg, 't.C.m', g.CLS_ORDER_FLAKE, sha='s' * 40, now_iso=iso,
                     completed_runs_before=1)
        g.record_green(reg, 't.C.m', sha='s' * 40, now_iso=iso)
        self.assertNotEqual(reg['tests']['t.C.m']['classification'],
                            g.CLS_RECOVERED)
        g.record_green(reg, 't.C.m', sha='s' * 40, now_iso=iso)
        self.assertEqual(reg['tests']['t.C.m']['classification'],
                         g.CLS_RECOVERED)
        self.assertTrue(reg['tests']['t.C.m']['ever_green'])

    def test_history_capped_at_max(self):
        reg = g.new_registry()
        iso = '2026-07-08T00:00:00+00:00'
        for _ in range(g.HISTORY_MAX + 5):
            g.record_red(reg, 't.C.m', g.CLS_BACKLOG, sha='s' * 40, now_iso=iso,
                         completed_runs_before=1)
        self.assertEqual(len(reg['tests']['t.C.m']['history']), g.HISTORY_MAX)


# --- run_guardian orchestration (fake invoker) -------------------------------

class RunGuardianTest(_TmpRegistryTest):

    def test_sha_skip_leaves_registry_untouched(self):
        g.save_registry(self.registry_path, {
            '_meta': {'last_sha': 'a' * 40, 'last_run_result': g.RUN_GREEN,
                      'completed_runs': 3, 'last_run_at': 'x'},
            'tests': {},
        })
        inv = FakeInvoker(sha='a' * 40)
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_SKIPPED)
        self.assertFalse(inv.setup_called)  # never provisioned a worktree
        self.assertEqual(self._load()['_meta']['completed_runs'], 3)

    def test_canary_failure_records_no_per_test_state(self):
        inv = FakeInvoker(canary_ok=False, canary_detail='canary.C.m: FAILED',
                          red_sets=[{'x.C.m'}])
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_CANARY_FAILED)
        reg = self._load()
        self.assertEqual(reg['tests'], {})
        self.assertEqual(reg['_meta']['last_run_result'], g.RUN_CANARY_FAILED)
        self.assertTrue(inv.teardown_called)

    def test_infra_flake_after_retry(self):
        inv = FakeInvoker(collect_errors=2)  # both attempts raise
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_INFRA_FLAKE)
        self.assertEqual(self._load()['_meta']['last_run_result'],
                         g.RUN_INFRA_FLAKE)

    def test_run_level_failure_recovers_on_retry(self):
        inv = FakeInvoker(collect_errors=1, red_sets=[set()])  # 1 fail then green
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_GREEN)

    def test_step_change_skips_isolation(self):
        # Seed a prior completed run so completed_runs_before >= 1; the
        # step-change branch is suppressed on the first-ever run (a standing
        # backlog is debt, not a mass break — see run_guardian).
        _run(FakeInvoker(sha='a' * 40, red_sets=[set()]), self.registry_path)
        reds = {f't{n}.C.m' for n in range(6)}  # 6 new reds > threshold 5
        inv = FakeInvoker(sha='b' * 40, red_sets=[reds])
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_STEP_CHANGE)
        self.assertEqual(len(res['new_reds']), 6)
        # per-test bookkeeping suspended during a step-change episode
        self.assertEqual(self._load()['tests'], {})

    def test_first_run_backlog_catalogs_not_step_change(self):
        # First-ever run with a big standing backlog (> step-change threshold):
        # must catalog every red as backlog and advance completed_runs, NOT
        # trip step-change and stall at completed_runs=0 forever.
        reds = {f't{n}.C.m' for n in range(8)}  # 8 reds > threshold 5
        inv = FakeInvoker(
            sha='a' * 40, red_sets=[reds],
            single_results={t: (False, 'AssertionError') for t in reds},
        )
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_RED)
        reg = self._load()
        self.assertEqual(reg['_meta']['completed_runs'], 1)
        self.assertEqual(len(reg['tests']), 8)
        self.assertTrue(
            all(e['classification'] == g.CLS_BACKLOG
                for e in reg['tests'].values())
        )

    def test_normal_run_classifies_and_records(self):
        reds = {'flake.C.m', 'break.C.m'}
        inv = FakeInvoker(red_sets=[reds], single_results={
            'flake.C.m': (True, ''),          # passes alone -> order-flake
            'break.C.m': (False, 'AssertionError'),  # fails alone
        })
        # completed_runs_before is 0 => break is backlog (first-ever run).
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_RED)
        self.assertEqual(res['classifications']['flake.C.m'], g.CLS_ORDER_FLAKE)
        self.assertEqual(res['classifications']['break.C.m'], g.CLS_BACKLOG)
        reg = self._load()
        self.assertEqual(reg['_meta']['completed_runs'], 1)

    def test_genuine_break_after_prior_green_run(self):
        # Run 1: all green (establishes completed_runs=1 so the test was green).
        inv1 = FakeInvoker(sha='a' * 40, red_sets=[set()])
        _run(inv1, self.registry_path)
        # Run 2: a brand-new red that fails alone with no env sig => genuine-break.
        inv2 = FakeInvoker(sha='b' * 40, red_sets=[{'break.C.m'}],
                           single_results={'break.C.m': (False, 'AssertionError')})
        res = _run(inv2, self.registry_path)
        self.assertEqual(res['classifications']['break.C.m'], g.CLS_GENUINE_BREAK)

    def test_green_run_stores_baseline_hygiene(self):
        inv = FakeInvoker(sha='a' * 40, red_sets=[set()])
        res = _run(inv, self.registry_path)
        self.assertEqual(res['status'], g.RUN_GREEN)
        self.assertEqual(inv.store_calls, ['a' * 40])

    def test_red_run_does_not_store_baseline(self):
        inv = FakeInvoker(red_sets=[{'x.C.m'}],
                          single_results={'x.C.m': (True, '')})
        _run(inv, self.registry_path)
        self.assertEqual(inv.store_calls, [])

    def test_recovered_across_two_green_runs(self):
        # red -> green -> green should end as recovered.
        inv1 = FakeInvoker(sha='a' * 40, red_sets=[{'x.C.m'}],
                           single_results={'x.C.m': (True, '')})
        _run(inv1, self.registry_path)
        inv2 = FakeInvoker(sha='b' * 40, red_sets=[set()])
        _run(inv2, self.registry_path)
        inv3 = FakeInvoker(sha='c' * 40, red_sets=[set()])
        _run(inv3, self.registry_path)
        self.assertEqual(self._load()['tests']['x.C.m']['classification'],
                         g.CLS_RECOVERED)


# --- run_single_test_in_dir (the NEW L6 isolation runner) --------------------

class RunSingleTestInDirTest(unittest.TestCase):

    def _fake_proc(self, returncode, stdout='', stderr=''):
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr,
        )

    def test_strips_discovery_prefix_to_bare_module(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured['cmd'] = cmd
            return self._fake_proc(0, 'Ran 1 test in 0.0s\n\nOK')

        with mock.patch.object(trc, '_discover_wall_prefix', return_value=[]), \
                mock.patch.object(trc.subprocess, 'run', side_effect=fake_run):
            passed, _ = trc.run_single_test_in_dir(
                Path('/wt'), 'scripts.tests.test_foo.TestBar.test_x', {}, 120,
            )
        self.assertTrue(passed)
        # 'scripts.tests.' prefix stripped; bare id is the unittest target.
        self.assertEqual(captured['cmd'][-1], 'test_foo.TestBar.test_x')

    def test_failure_line_yields_not_passed(self):
        out = 'FAIL: test_x (test_foo.TestBar)\nRan 1 test in 0.0s\n\nFAILED'
        with mock.patch.object(trc, '_discover_wall_prefix', return_value=[]), \
                mock.patch.object(trc.subprocess, 'run',
                                  return_value=self._fake_proc(1, out)):
            passed, combined = trc.run_single_test_in_dir(
                Path('/wt'), 'test_foo.TestBar.test_x', {}, 120,
            )
        self.assertFalse(passed)
        self.assertIn('FAIL', combined)

    def test_timeout_is_fail_closed(self):
        import subprocess as _sp
        with mock.patch.object(trc, '_discover_wall_prefix', return_value=[]), \
                mock.patch.object(trc.subprocess, 'run',
                                  side_effect=_sp.TimeoutExpired('cmd', 120)):
            with self.assertRaises(trc.AnalysisError):
                trc.run_single_test_in_dir(Path('/wt'), 'test_foo.T.m', {}, 120)

    def test_malformed_output_is_fail_closed(self):
        # No FAIL/ERROR lines and no "Ran N tests" summary => aborted run.
        with mock.patch.object(trc, '_discover_wall_prefix', return_value=[]), \
                mock.patch.object(trc.subprocess, 'run',
                                  return_value=self._fake_proc(0, 'garbage')):
            with self.assertRaises(trc.AnalysisError):
                trc.run_single_test_in_dir(Path('/wt'), 'test_foo.T.m', {}, 120)


# --- warmer single-flight lock relocation (L7) -------------------------------

class RegBaselineLockPathTest(unittest.TestCase):

    def test_default_is_absolute_and_home_swap_stable(self):
        import os
        with mock.patch.dict('os.environ', {}, clear=False):
            os.environ.pop('OL_REGBASELINE_LOCK_PATH', None)
            p1 = baseline_cache.regbaseline_lock_path()
            # A HOME swap must NOT relocate the lock (the #755 class): the
            # default is a fixed absolute constant, not Path.home()-derived.
            with mock.patch.dict('os.environ', {'HOME': '/tmp/fake-home'}):
                p2 = baseline_cache.regbaseline_lock_path()
        # References the production constant rather than a raw path literal
        # (test-isolation leak guard). The constant itself lives in the
        # module under test.
        self.assertEqual(p1, baseline_cache.REGBASELINE_LOCK_PATH)
        self.assertTrue(p1.is_absolute())
        self.assertEqual(p1, p2)
        self.assertEqual(p1.parent.name, 'state')
        self.assertEqual(p1.name, 'ol-regbaseline-warm.lock')

    def test_env_override_wins(self):
        import os
        with mock.patch.dict(
            'os.environ', {'OL_REGBASELINE_LOCK_PATH': '/tmp/scratch.lock'},
        ):
            self.assertEqual(baseline_cache.regbaseline_lock_path(),
                             Path('/tmp/scratch.lock'))


class StageMaintenanceGateTest(unittest.TestCase):
    """Stage bookkeeping — which files the GRADUATION evidence card — must run at
    every stage, including 0.

    It used to be nested inside `if eff_stage >= 1`, making Stage 0 a dead end:
    the only exit from shadow is a graduation card, and the code that files it
    could only run once already out of shadow. The guardian met the Stage-1 bar
    around run 7 and sat in shadow through run 17 with no way to say so.

    `main()` had NO test coverage at all, which is why 17 runs went by without
    this surfacing — every piece passes in isolation; only the wiring is wrong.
    """

    def _run(self, eff_stage, status=g.RUN_RED):
        calls = {'maintenance': 0, 'proposal': 0, 'escalation': 0}

        def _maint(*_a, **_k):
            calls['maintenance'] += 1

        def _prop(*_a, **_k):
            calls['proposal'] += 1
            return {}

        def _esc(*_a, **_k):
            calls['escalation'] += 1
            return {'escalated': []}

        with mock.patch.object(g, 'run_guardian', return_value={'status': status}), \
             mock.patch.object(g.stage, 'effective_stage', return_value=eff_stage), \
             mock.patch.object(g, '_run_stage_maintenance', _maint), \
             mock.patch.object(g, 'run_proposal_cycle', _prop), \
             mock.patch.object(g, 'run_escalation_only', _esc), \
             mock.patch.object(g, 'load_registry', return_value=g.new_registry()):
            rc = g.main(['--repo-root', '/tmp/nonexistent', '--mode', 'shadow'])
        return rc, calls

    def test_stage_zero_still_files_graduation_evidence(self):
        rc, calls = self._run(0)
        self.assertEqual(rc, 0)
        # The escape hatch runs...
        self.assertEqual(calls['maintenance'], 1)
        # ...but shadow is still shadow: no proposing, no dispatching.
        self.assertEqual(calls['proposal'], 0)
        # ...and it CAN speak: the notify-only half runs.
        self.assertEqual(calls['escalation'], 1)

    def test_stage_one_does_not_double_escalate(self):
        # At Stage 1+ `run_proposal_cycle` already does the episode-reset and
        # escalation halves. Running the shadow path too would mean two
        # independent registry read/write passes racing in one cycle.
        _rc, calls = self._run(1)
        self.assertEqual(calls['proposal'], 1)
        self.assertEqual(calls['escalation'], 0)

    def test_stage_one_runs_both(self):
        _rc, calls = self._run(1)
        self.assertEqual(calls['maintenance'], 1)
        self.assertEqual(calls['proposal'], 1)

    def test_inconclusive_run_does_neither(self):
        # No per-test verdict this cycle → nothing to propose and no evidence to
        # count. Unchanged by the fix.
        _rc, calls = self._run(1, status=g.RUN_INFRA_FLAKE)
        self.assertEqual(calls['maintenance'], 0)
        self.assertEqual(calls['proposal'], 0)
        self.assertEqual(calls['escalation'], 0)


class ShadowEscalationTest(unittest.TestCase):
    """Stage 0 may SAY a test is broken; it still may not ACT on one.

    Escalation proposes nothing, dispatches nothing, merges nothing and touches no
    config — so gating it behind Stage 1 only meant the guardian could not report a
    break until a human had already extended trust to a component they had never
    seen do the job.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg_path = Path(self._tmp.name) / 'registry.json'
        self.paged = []

    def _deps(self, boom=False):
        def _esc(*, test_id, entry):
            if boom:
                raise RuntimeError('DM down')
            self.paged.append(test_id)
        return types.SimpleNamespace(escalate=_esc)

    def _seed(self, **over):
        reg = g.new_registry()
        entry = {'classification': g.CLS_GENUINE_BREAK, 'consecutive_red_runs': 2,
                 'consecutive_green_runs': 0, 'break_escalated': False}
        entry.update(over)
        reg['tests']['t.C.m'] = entry
        g.save_registry(self.reg_path, reg)
        return reg

    def _run(self, **kw):
        return g.run_escalation_only('/tmp/nope', registry_path=self.reg_path,
                                     deps=self._deps(**kw))

    def test_genuine_break_pages_in_shadow(self):
        self._seed()
        out = self._run()
        self.assertEqual(self.paged, ['t.C.m'])
        self.assertEqual(out['escalated'], ['t.C.m'])
        # latched, so it pages exactly once per episode
        self.assertTrue(g.load_registry(self.reg_path)['tests']['t.C.m']
                        ['break_escalated'])

    def test_second_run_does_not_re_page(self):
        self._seed()
        self._run()
        self._run()
        self.assertEqual(self.paged, ['t.C.m'])

    def test_green_victim_rearms_for_a_new_episode(self):
        self._seed(break_escalated=True, consecutive_red_runs=0)
        self._run()
        self.assertFalse(g.load_registry(self.reg_path)['tests']['t.C.m']
                         ['break_escalated'])

    def test_backlog_and_one_night_breaks_do_not_page(self):
        self._seed(classification=g.CLS_BACKLOG)
        self.assertEqual(self._run()['escalated'], [])
        self._seed(consecutive_red_runs=1)   # one night only
        self.assertEqual(self._run()['escalated'], [])
        self.assertEqual(self.paged, [])

    def test_a_failed_page_is_retried_not_swallowed(self):
        # If the DM/alert throws, the latch must stay UNSET so the next run
        # tries again — a lost page is the failure this whole PR is about.
        self._seed()
        out = g.run_escalation_only('/tmp/nope', registry_path=self.reg_path,
                                    deps=self._deps(boom=True))
        self.assertEqual(out['escalated'], [])
        self.assertFalse(g.load_registry(self.reg_path)['tests']['t.C.m']
                         .get('break_escalated'))
        self.assertEqual(self._run()['escalated'], ['t.C.m'])


if __name__ == '__main__':
    unittest.main()
