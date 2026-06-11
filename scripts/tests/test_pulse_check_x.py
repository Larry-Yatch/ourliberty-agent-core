#!/usr/bin/env python3
"""Tests for pulse_check_x (Check X chain-quality regression watch).

Covers the pure analysis core via synthetic clarify_events + costs:
clean (no breach), each active-threshold breach (clarify-rounds,
revision-rounds), insufficient_signal gating, fixture-id exclusion,
deferred-threshold rendering, DM-only-on-regression discipline, atomic
write, sentinel naming, and the no-LLM-import self-check.

Run::

    cd ~/agent-core && python3 -m pytest scripts/tests/test_pulse_check_x.py
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_x as px  # noqa: E402


# NOW is a Monday; cutover default 2026-06-01 (also a Monday).
NOW = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
BASELINE_TS = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)  # in baseline
TRAILING_TS = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)  # in trailing


def _config(**over):
    cfg = {
        'cutover_date': px.DEFAULT_CUTOVER_DATE,
        'window_days': px.DEFAULT_WINDOW_DAYS,
        'min_tasks_per_window': px.DEFAULT_MIN_TASKS_PER_WINDOW,
        'clarify_rate_rel_increase': px.DEFAULT_CLARIFY_RATE_REL_INCREASE,
        'revision_rounds_rel_increase': px.DEFAULT_REVISION_ROUNDS_REL_INCREASE,
        'escalate_rate_abs_increase': px.DEFAULT_ESCALATE_RATE_ABS_INCREASE,
        'pass_rate_abs_drop': px.DEFAULT_PASS_RATE_ABS_DROP,
    }
    cfg.update(over)
    return cfg


def _cost_rows(prefix, ts, n_tasks, *, rows_each=1, agent='forge',
               task_type='build'):
    """n_tasks distinct task_ids, each with rows_each dispatch rows."""
    rows = []
    for i in range(n_tasks):
        tid = f'{prefix}-{i}'
        for _ in range(rows_each):
            rows.append(px.CostRow(ts=ts, task_id=tid, agent=agent,
                                   task_type=task_type))
    return rows


def _clarifies(prefix, ts, count, n_tasks, *, agent='forge'):
    """count clarify events spread across n_tasks task_ids."""
    rows = []
    for j in range(count):
        tid = f'{prefix}-{j % max(n_tasks, 1)}'
        rows.append(px.ClarifyEvent(ts=ts, task_id=tid, agent=agent))
    return rows


def _verdicts(prefix, ts, *, n_pass=0, n_revision=0, n_escalate=0):
    """Synthetic Mirror VerdictEvents: n_pass/n_revision/n_escalate of each."""
    rows = []
    i = 0
    for verdict, n in (('pass', n_pass), ('revision', n_revision),
                       ('escalate', n_escalate)):
        for _ in range(n):
            rows.append(px.VerdictEvent(ts=ts, task_id=f'{prefix}-v{i}',
                                        verdict=verdict))
            i += 1
    return rows


class TestCleanRun(unittest.TestCase):

    def test_equal_metrics_no_breach(self):
        # Both windows: 10 tasks, 1 dispatch each, 10 clarifies => identical
        # clarify_rpt=1.0, revision_rpt=0.0 => no relative increase.
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 10))
        clarifies = (_clarifies('base', BASELINE_TS, 10, 10)
                     + _clarifies('trail', TRAILING_TS, 10, 10))
        result = px.run_check(clarify_events=clarifies, cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'none')
        self.assertEqual(result.breaches, [])
        self.assertEqual(result.baseline.distinct_tasks, 10)
        self.assertEqual(result.trailing.distinct_tasks, 10)


class TestClarifyBreach(unittest.TestCase):

    def test_clarify_rounds_rel_increase_fires(self):
        # Baseline clarify_rpt 1.0 (10/10), trailing 1.6 (16/10) => +0.6 >= 0.5.
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 10))
        clarifies = (_clarifies('base', BASELINE_TS, 10, 10)
                     + _clarifies('trail', TRAILING_TS, 16, 10))
        result = px.run_check(clarify_events=clarifies, cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'regression_suspected')
        names = [b['threshold'] for b in result.breaches]
        self.assertIn('clarify_rate_rel_increase', names)
        self.assertNotIn('revision_rounds_rel_increase', names)

    def test_clarify_increase_below_threshold_no_fire(self):
        # Baseline 1.0, trailing 1.3 (13/10) => +0.3 < 0.5.
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 10))
        clarifies = (_clarifies('base', BASELINE_TS, 10, 10)
                     + _clarifies('trail', TRAILING_TS, 13, 10))
        result = px.run_check(clarify_events=clarifies, cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'none')


class TestRevisionBreach(unittest.TestCase):

    def test_revision_rounds_rel_increase_fires(self):
        # Baseline 2 dispatches/task => rpt 1.0; trailing 3/task => rpt 2.0.
        costs = (_cost_rows('base', BASELINE_TS, 10, rows_each=2)
                 + _cost_rows('trail', TRAILING_TS, 10, rows_each=3))
        result = px.run_check(clarify_events=[], cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'regression_suspected')
        names = [b['threshold'] for b in result.breaches]
        self.assertIn('revision_rounds_rel_increase', names)


class TestVerdictMix(unittest.TestCase):
    """check-x-verdict-emission: reactivated verdict-mix thresholds.

    Both windows carry enough Forge cost-row volume (10 tasks each) to clear
    the overall insufficient-signal gate; clarify/revision are held flat so any
    fired breach is attributable to the verdict mix alone.
    """

    def _flat_costs(self):
        return (_cost_rows('base', BASELINE_TS, 10)
                + _cost_rows('trail', TRAILING_TS, 10))

    def test_escalate_rate_abs_increase_fires(self):
        # baseline esc 0.0 (0/10), trailing esc 0.2 (2/10) => +0.2 >= 0.10.
        # pass held flat at 0.6 so only escalate fires.
        verdicts = (
            _verdicts('base', BASELINE_TS, n_pass=6, n_revision=4)
            + _verdicts('trail', TRAILING_TS, n_pass=6, n_revision=2,
                        n_escalate=2))
        result = px.run_check(clarify_events=[], cost_rows=self._flat_costs(),
                              verdict_events=verdicts, config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'regression_suspected')
        names = [b['threshold'] for b in result.breaches]
        self.assertIn('escalate_rate_abs_increase', names)
        self.assertNotIn('pass_rate_abs_drop', names)
        # Breach dicts use the absolute-change key, not rel_increase.
        esc = next(b for b in result.breaches
                   if b['threshold'] == 'escalate_rate_abs_increase')
        self.assertIn('abs_change', esc)
        self.assertAlmostEqual(esc['abs_change'], 0.2)

    def test_pass_rate_abs_drop_fires(self):
        # baseline pass 1.0 (10/10), trailing pass 0.8 (8/10) => drop 0.2 >= 0.15.
        # escalate held flat at 0.0 so only pass-drop fires.
        verdicts = (
            _verdicts('base', BASELINE_TS, n_pass=10)
            + _verdicts('trail', TRAILING_TS, n_pass=8, n_revision=2))
        result = px.run_check(clarify_events=[], cost_rows=self._flat_costs(),
                              verdict_events=verdicts, config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'regression_suspected')
        names = [b['threshold'] for b in result.breaches]
        self.assertIn('pass_rate_abs_drop', names)
        self.assertNotIn('escalate_rate_abs_increase', names)

    def test_stable_verdict_mix_no_breach(self):
        verdicts = (
            _verdicts('base', BASELINE_TS, n_pass=8, n_revision=1, n_escalate=1)
            + _verdicts('trail', TRAILING_TS, n_pass=8, n_revision=1,
                        n_escalate=1))
        result = px.run_check(clarify_events=[], cost_rows=self._flat_costs(),
                              verdict_events=verdicts, config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'none')
        self.assertEqual(result.breaches, [])
        statuses = {s['threshold']: s for s in result.verdict_thresholds}
        self.assertEqual(statuses['escalate_rate_abs_increase']['status'],
                         'clean')
        self.assertEqual(statuses['pass_rate_abs_drop']['status'], 'clean')

    def test_thin_verdicts_report_insufficient_signal(self):
        # Forge volume is fine (10 tasks/window), but only 3 verdicts/window
        # (< min 8) => verdict-mix thresholds gate to insufficient_signal and
        # never fire, even with a big escalate swing.
        verdicts = (
            _verdicts('base', BASELINE_TS, n_pass=3)
            + _verdicts('trail', TRAILING_TS, n_escalate=3))
        result = px.run_check(clarify_events=[], cost_rows=self._flat_costs(),
                              verdict_events=verdicts, config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'none')
        statuses = {s['threshold']: s for s in result.verdict_thresholds}
        self.assertEqual(statuses['escalate_rate_abs_increase']['status'],
                         'insufficient_signal')
        self.assertEqual(statuses['pass_rate_abs_drop']['status'],
                         'insufficient_signal')

    def test_no_verdict_events_is_insufficient_signal(self):
        # The 4.8-baseline reality: zero verdict events => both thresholds
        # report insufficient_signal (the HONEST LIMITATION mechanism).
        result = px.run_check(clarify_events=[], cost_rows=self._flat_costs(),
                              verdict_events=[], config=_config(), now=NOW)
        statuses = {s['threshold']: s for s in result.verdict_thresholds}
        self.assertEqual(statuses['escalate_rate_abs_increase']['status'],
                         'insufficient_signal')
        self.assertEqual(statuses['pass_rate_abs_drop']['status'],
                         'insufficient_signal')


class TestInsufficientSignal(unittest.TestCase):

    def test_thin_trailing_window_is_insufficient(self):
        # Trailing only 3 distinct tasks (< min 8) => insufficient_signal,
        # even with a clarify spike that would otherwise breach.
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 3))
        clarifies = (_clarifies('base', BASELINE_TS, 10, 10)
                     + _clarifies('trail', TRAILING_TS, 30, 3))
        result = px.run_check(clarify_events=clarifies, cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'insufficient_signal')
        self.assertEqual(result.breaches, [])

    def test_thin_baseline_window_is_insufficient(self):
        costs = (_cost_rows('base', BASELINE_TS, 2)
                 + _cost_rows('trail', TRAILING_TS, 10))
        result = px.run_check(clarify_events=[], cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.outcome, 'insufficient_signal')


class TestFixtureExclusion(unittest.TestCase):

    def test_fixture_task_ids_excluded_from_metrics(self):
        # 10 real trailing tasks (clean) + a flood of zz-fixture- dispatches
        # and clarifies that would breach if counted. Must not.
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 10)
                 + _cost_rows('zz-fixture-x', TRAILING_TS, 50, rows_each=5))
        clarifies = (_clarifies('base', BASELINE_TS, 10, 10)
                     + _clarifies('trail', TRAILING_TS, 10, 10)
                     + _clarifies('zz-fixture-x', TRAILING_TS, 100, 50))
        result = px.run_check(clarify_events=clarifies, cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.trailing.distinct_tasks, 10)
        self.assertEqual(result.trailing.clarify_count, 10)
        self.assertEqual(result.outcome, 'none')

    def test_non_forge_and_excluded_task_types_dropped(self):
        # Mirror rows + notification/auto-merge task_types must not count.
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 10)
                 + _cost_rows('mir', TRAILING_TS, 20, agent='mirror')
                 + _cost_rows('noti', TRAILING_TS, 20, task_type='notification'))
        result = px.run_check(clarify_events=[], cost_rows=costs,
                              config=_config(), now=NOW)
        self.assertEqual(result.trailing.distinct_tasks, 10)


class TestArtifactShape(unittest.TestCase):

    def test_artifact_has_required_keys_and_deferred(self):
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 10))
        result = px.run_check(clarify_events=[], cost_rows=costs,
                              config=_config(), now=NOW)
        art = px.build_artifact(result)
        for key in ('as_of', 'week_anchor', 'check', 'outcome',
                    'cutover_date', 'window_days', 'min_tasks_per_window',
                    'windows', 'active_thresholds_breached',
                    'deferred_thresholds', 'rationale', 'applied'):
            self.assertIn(key, art)
        self.assertEqual(art['check'], 'X')
        self.assertFalse(art['applied'])
        self.assertEqual(art['week_anchor'], '2026-06-29')
        deferred_names = {d['threshold'] for d in art['deferred_thresholds']}
        self.assertEqual(deferred_names,
                         set(px.DEFERRED_THRESHOLDS))
        self.assertTrue(all(d['deferred'] for d in art['deferred_thresholds']))


class TestDmDiscipline(unittest.TestCase):
    """DM fires ONLY on regression_suspected, via larry_alerts.append_alert."""

    def setUp(self):
        self._orig = sys.modules.get('larry_alerts')
        self.calls = []

        fake = types.ModuleType('larry_alerts')

        def append_alert(*, source, severity, message, subject=None,
                         suggested_action=None):
            self.calls.append({
                'source': source, 'severity': severity, 'message': message,
                'subject': subject, 'suggested_action': suggested_action,
            })
            return True

        fake.append_alert = append_alert
        sys.modules['larry_alerts'] = fake

    def tearDown(self):
        if self._orig is not None:
            sys.modules['larry_alerts'] = self._orig
        else:
            sys.modules.pop('larry_alerts', None)

    def test_no_dm_when_outcome_none(self):
        art = {'outcome': 'none'}
        self.assertFalse(px.dm_digest(art))
        self.assertEqual(self.calls, [])

    def test_dm_when_regression_suspected(self):
        costs = (_cost_rows('base', BASELINE_TS, 10, rows_each=2)
                 + _cost_rows('trail', TRAILING_TS, 10, rows_each=3))
        result = px.run_check(clarify_events=[], cost_rows=costs,
                              config=_config(), now=NOW)
        art = px.build_artifact(result)
        self.assertEqual(art['outcome'], 'regression_suspected')
        self.assertTrue(px.dm_digest(art))
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call['source'], 'pulse-check-x')
        self.assertEqual(call['severity'], 'warning')
        self.assertTrue(call['subject'].startswith('check-x-regression:'))
        self.assertIn('claude-opus-4-7', call['suggested_action'])


class TestSentinelAndAtomicWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = px.PROPOSALS_DIR
        px.PROPOSALS_DIR = self.tmp / 'blackboard' / 'pulse-check-x-proposals'

    def tearDown(self):
        px.PROPOSALS_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_path_uses_iso_monday(self):
        week = px.iso_week_monday(NOW.date())
        self.assertEqual(week, '2026-06-29')
        path = px.artifact_path_for_week(week)
        self.assertTrue(str(path).endswith(f'check-x-{week}.json'))

    def test_write_artifact_atomic_no_tmp_leftover(self):
        costs = (_cost_rows('base', BASELINE_TS, 10)
                 + _cost_rows('trail', TRAILING_TS, 10))
        result = px.run_check(clarify_events=[], cost_rows=costs,
                              config=_config(), now=NOW)
        path = px.write_artifact(px.build_artifact(result))
        self.assertEqual(list(path.parent.glob('*.tmp')), [])
        self.assertTrue(path.exists())


class TestNoLLMCalls(unittest.TestCase):
    """Mirror review focus: the analyzer must not import an LLM client."""

    def test_module_does_not_import_llm_client(self):
        src = (_REPO_SCRIPTS / 'pulse_check_x.py').read_text()
        for forbidden in ('import anthropic', 'from anthropic',
                          'import openai', 'from openai'):
            self.assertNotIn(forbidden, src,
                             f'{forbidden!r} must not appear in '
                             'pulse_check_x.py')


class TestFromJsonRoundTrip(unittest.TestCase):

    def test_fixture_parse_via_cli_helper(self):
        raw = {
            'now': NOW.isoformat(),
            'clarify_events': [
                {'ts': TRAILING_TS.isoformat(), 'task_id': 'real-1',
                 'agent': 'forge'},
            ],
            'costs': [
                {'ts': TRAILING_TS.isoformat(), 'task_id': 'real-1',
                 'agent': 'forge', 'task_type': 'build'},
            ],
        }
        clarify_events, cost_rows = px._events_from_fixture(raw, NOW)
        self.assertEqual(len(clarify_events), 1)
        self.assertEqual(len(cost_rows), 1)
        self.assertEqual(clarify_events[0].agent, 'forge')
        self.assertEqual(cost_rows[0].task_type, 'build')


if __name__ == '__main__':
    unittest.main()
