#!/usr/bin/env python3
"""Tests for pulse_check_viii (Check VIII PR-2b).

Covers each proposal-firing rule with synthetic fixture corpora, the
fixture-pattern filter on quota events, the idempotency artifact write,
and the digest format.

larry-alerts and config IO are never touched live — every test exercises
the pure-function core (`run_check`) with injected inputs.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_viii
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_viii as p8  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)  # Monday


def _dm(hours_ago: float, spend: float = 55.0, threshold: float = 60.0):
    return p8.BurnRateDM(
        ts=NOW - timedelta(hours=hours_ago),
        spend_usd=spend, threshold_usd=threshold,
    )


def _event(hours_ago: float, task_id: str = 'real-task-001', agent: str = 'beacon'):
    return p8.QuotaEvent(
        ts=NOW - timedelta(hours=hours_ago),
        task_id=task_id, agent=agent,
    )


def _cost(hours_ago: float, cost: float):
    return p8.CostEntry(
        ts=NOW - timedelta(hours=hours_ago),
        cost_usd=cost,
    )


class TestParseDmBody(unittest.TestCase):

    def test_extracts_spend_and_threshold(self):
        body = (
            'Trailing 5h LLM pace at 92% of dollar gate '
            '($55.20 of $60.00). Pace indicator only ...'
        )
        out = p8.parse_dm_body(body)
        self.assertEqual(out, (55.20, 60.00))

    def test_no_match_returns_none(self):
        self.assertIsNone(p8.parse_dm_body('random text without dollars'))

    def test_non_string_returns_none(self):
        self.assertIsNone(p8.parse_dm_body(None))  # type: ignore[arg-type]


class TestClassifyDms(unittest.TestCase):

    def test_dm_with_event_within_2h_after_is_tp(self):
        dm = _dm(hours_ago=10)
        ev = _event(hours_ago=8.5)  # 1.5h after DM
        tps, fps = p8.classify_dms([dm], [ev])
        self.assertEqual(len(tps), 1)
        self.assertEqual(len(fps), 0)

    def test_dm_with_no_event_in_window_is_fp(self):
        dm = _dm(hours_ago=10)
        ev = _event(hours_ago=20)  # 10h BEFORE DM — outside window
        tps, fps = p8.classify_dms([dm], [ev])
        self.assertEqual(len(tps), 0)
        self.assertEqual(len(fps), 1)

    def test_event_before_dm_is_not_tp(self):
        dm = _dm(hours_ago=5)
        ev = _event(hours_ago=6)  # 1h BEFORE DM — must not count as TP
        tps, fps = p8.classify_dms([dm], [ev])
        self.assertEqual(len(tps), 0)
        self.assertEqual(len(fps), 1)


class TestClassifyEvents(unittest.TestCase):

    def test_event_with_dm_within_2h_before_is_not_fn(self):
        dm = _dm(hours_ago=11)
        ev = _event(hours_ago=10)  # 1h after DM
        fns = p8.classify_events([dm], [ev])
        self.assertEqual(len(fns), 0)

    def test_event_with_no_dm_in_window_is_fn(self):
        ev = _event(hours_ago=10)
        fns = p8.classify_events([], [ev])
        self.assertEqual(len(fns), 1)

    def test_dm_after_event_is_not_counted(self):
        dm = _dm(hours_ago=5)
        ev = _event(hours_ago=10)  # DM is AFTER event
        fns = p8.classify_events([dm], [ev])
        self.assertEqual(len(fns), 1)


class TestComputePercentile(unittest.TestCase):

    def test_p75_known(self):
        # [10, 20, 30, 40] — p75 should be 32.5 with linear interpolation.
        self.assertAlmostEqual(
            p8.compute_percentile([10.0, 20.0, 30.0, 40.0], 0.75),
            32.5, places=4,
        )

    def test_empty(self):
        self.assertEqual(p8.compute_percentile([], 0.75), 0.0)

    def test_single(self):
        self.assertEqual(p8.compute_percentile([42.0], 0.75), 42.0)


class TestRollingSpend(unittest.TestCase):

    def test_sums_costs_within_5h_window_before_target(self):
        target = NOW
        costs = [
            _cost(hours_ago=1, cost=2.0),   # in window
            _cost(hours_ago=4, cost=3.0),   # in window
            _cost(hours_ago=6, cost=10.0),  # outside
        ]
        out = p8.rolling_5h_spend_at(target, costs)
        self.assertAlmostEqual(out, 5.0)


class TestRunCheckRules(unittest.TestCase):

    CURRENT_THRESHOLD = 60.0

    def _run(self, *, dms, events, costs=None):
        return p8.run_check(
            dms=dms, events=events,
            costs=costs or [],
            current_threshold_usd=self.CURRENT_THRESHOLD,
            now=NOW,
        )

    # ---- insufficient_signal ----

    def test_insufficient_signal_when_too_few_dms(self):
        # 4 DMs (< floor 5) + 3 events should still trip insufficient_signal.
        dms = [_dm(hours_ago=10 + i * 24) for i in range(4)]
        events = [_event(hours_ago=8 + i * 24) for i in range(3)]
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'insufficient_signal')
        self.assertIsNone(result.proposed_threshold_usd)

    def test_insufficient_signal_when_too_few_events(self):
        # 5 DMs + 2 events (< floor 3).
        dms = [_dm(hours_ago=10 + i * 24) for i in range(5)]
        events = [_event(hours_ago=8 + i * 24) for i in range(2)]
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'insufficient_signal')

    # ---- raise ----

    def test_raise_fires_when_precision_low(self):
        # 10 DMs: 3 TPs paired 1:1 with 3 events, 7 FPs far from any event.
        # precision = 3/10 = 0.3 (< 0.4 → raise). recall = 3/3 = 1.0
        # (avoids defer). Proposed = p75 of TP spends [40, 50, 60] = 55.0.
        dms = []
        events = []
        for i, sp in enumerate([40.0, 50.0, 60.0]):
            base = 24 + i * 12
            dms.append(_dm(hours_ago=base, spend=sp))
            events.append(_event(hours_ago=base - 0.5))
        for j in range(7):
            dms.append(_dm(hours_ago=200 + j * 24, spend=10.0))
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'raise')
        self.assertAlmostEqual(result.proposed_threshold_usd, 55.0, places=2)

    def test_raise_proposed_is_p75_of_tp_spends(self):
        # 8 DMs: 5 FPs at low spend + 3 TPs paired with 3 events at high
        # spend. precision = 3/8 = 0.375 (< 0.4 → raise). recall = 3/3 = 1.0.
        # Proposed = 75th pct of TP spends [99, 99, 99] = 99.0.
        dms = [_dm(hours_ago=10 + i * 24, spend=10.0) for i in range(5)]
        events = []
        for i in range(3):
            evhrs = 100 + i * 24
            events.append(_event(hours_ago=evhrs))
            dms.append(_dm(hours_ago=evhrs + 0.5, spend=99.0))
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'raise')
        self.assertAlmostEqual(result.proposed_threshold_usd, 99.0, places=2)

    # ---- lower ----

    def test_lower_fires_when_recall_low(self):
        # We need recall < 0.6 (>= 3 events), precision NOT < 0.4 (else
        # defer). precision = TP/(TP+FP). With 5 DMs all TP → precision = 1.0.
        # recall = 5/(5+5) = 0.5 < 0.6 → lower fires.
        dms = []
        events = []
        # 5 paired DM->event TPs.
        for i in range(5):
            base = 24 + i * 6
            dms.append(_dm(hours_ago=base, spend=40.0))
            events.append(_event(hours_ago=base - 0.5))
        # 5 unpaired events (FNs) at distant times.
        fn_event_hours = [200 + i * 12 for i in range(5)]
        for h in fn_event_hours:
            events.append(_event(hours_ago=h))
        # Costs at FN-event moments (rolling-5h spend each = 30.0).
        costs = []
        for h in fn_event_hours:
            costs.append(_cost(hours_ago=h, cost=15.0))
            costs.append(_cost(hours_ago=h + 1, cost=15.0))  # within 5h
        result = self._run(dms=dms, events=events, costs=costs)
        self.assertEqual(result.rule_fired, 'lower',
                         f'rule={result.rule_fired}, precision={result.precision}, '
                         f'recall={result.recall}, TP={result.tp_count}, '
                         f'FP={result.fp_count}, FN={result.fn_count}')
        # 75th-pct of [30.0]*5 = 30.0
        self.assertAlmostEqual(result.proposed_threshold_usd, 30.0, places=2)

    def test_lower_fallback_when_no_fn_spend_data(self):
        # Recall low, FNs exist, but no costs in window → fallback to -20%.
        dms = []
        events = []
        for i in range(5):
            base = 24 + i * 6
            dms.append(_dm(hours_ago=base, spend=40.0))
            events.append(_event(hours_ago=base - 0.5))
        for h in [200, 224, 248, 272, 296]:
            events.append(_event(hours_ago=h))
        result = self._run(dms=dms, events=events, costs=[])
        self.assertEqual(result.rule_fired, 'lower')
        # 60.0 * 0.80 = 48.0
        self.assertAlmostEqual(result.proposed_threshold_usd, 48.0, places=2)

    # ---- defer ----

    def test_defer_when_both_raise_and_lower_fire(self):
        # 10 DMs, 1 of which is TP (paired with 1 event). 9 FPs.
        # 4 events, 1 paired (TP), 3 unpaired (FN).
        # precision = 1/10 = 0.1 < 0.4 (raise).
        # recall = 1/(1+3) = 0.25 < 0.6 (lower).
        # Both fire → defer.
        dms = []
        events = []
        dms.append(_dm(hours_ago=24, spend=55.0))
        events.append(_event(hours_ago=23.5))
        for j in range(9):
            dms.append(_dm(hours_ago=100 + j * 12, spend=10.0))
        for k in range(3):
            events.append(_event(hours_ago=400 + k * 6))
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'defer')
        self.assertIsNone(result.proposed_threshold_usd)

    # ---- deprecate ----

    def test_deprecate_when_zero_tp_8w_and_many_events(self):
        # 8w window: TP = 0 (no DM has an event within 2h after).
        # 5+ events scattered in the 8w window.
        dms = [_dm(hours_ago=24 + i * 24, spend=20.0) for i in range(5)]
        # Place events 10h BEFORE each DM — outside the 2h-after window.
        events = [_event(hours_ago=24 + i * 24 + 10) for i in range(6)]
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'deprecate')
        self.assertIsNone(result.proposed_threshold_usd)
        self.assertEqual(result.tp_count_8w, 0)
        self.assertGreaterEqual(result.event_count_8w, 5)

    def test_deprecate_takes_priority_over_lower(self):
        # Even if recall would be < 0.6, deprecate (TP_8w==0, ≥5 events_8w)
        # fires first.
        dms = [_dm(hours_ago=24 + i * 24, spend=20.0) for i in range(5)]
        events = [_event(hours_ago=24 + i * 24 + 10) for i in range(6)]
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'deprecate')

    # ---- none ----

    def test_both_healthy_returns_none(self):
        # precision = 5/5 = 1.0, recall = 5/(5+1) ≈ 0.83.
        dms = []
        events = []
        for i in range(5):
            base = 24 + i * 6
            dms.append(_dm(hours_ago=base, spend=50.0))
            events.append(_event(hours_ago=base - 0.5))
        events.append(_event(hours_ago=400))  # 1 lonely FN
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'none')
        self.assertIsNone(result.proposed_threshold_usd)


class TestFixturePatternFilter(unittest.TestCase):

    def test_fixture_task_ids_dropped_from_load_quota_events(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'q.jsonl'
            lines = [
                {'ts': (NOW - timedelta(hours=5)).isoformat(),
                 'task_id': 't-fixture-001', 'agent': 'beacon'},
                {'ts': (NOW - timedelta(hours=4)).isoformat(),
                 'task_id': 'real-task-001', 'agent': 'beacon'},
                {'ts': (NOW - timedelta(hours=3)).isoformat(),
                 'task_id': 'task-001', 'agent': 'forge'},  # exact-match
            ]
            path.write_text('\n'.join(json.dumps(r) for r in lines))
            events = p8.load_quota_events(path, now=NOW)
            # Only the real-task-001 entry survives.
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].task_id, 'real-task-001')


class TestIdempotency(unittest.TestCase):
    """Re-running for the same week should not overwrite the artifact via
    the CLI; the file's presence is the gate."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig_agents_root = p8.AGENTS_ROOT
        self.orig_proposals_dir = p8.PROPOSALS_DIR
        p8.AGENTS_ROOT = self.tmp
        p8.PROPOSALS_DIR = self.tmp / 'blackboard' / 'pulse-check-viii-proposals'

    def tearDown(self):
        p8.AGENTS_ROOT = self.orig_agents_root
        p8.PROPOSALS_DIR = self.orig_proposals_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_path_per_week_is_stable(self):
        week = p8.iso_week_monday(NOW.date())
        path = p8.artifact_path_for_week(week)
        self.assertTrue(str(path).endswith(f'check-viii-{week}.json'))

    def test_write_artifact_lands_at_week_path(self):
        artifact = {
            'as_of': NOW.isoformat(),
            'week_anchor': p8.iso_week_monday(NOW.date()),
            'rule_fired': 'none',
            'current_threshold': 60.0,
            'proposed_threshold': None,
            'sample_sizes': {'TP': 0, 'FP': 0, 'FN': 0,
                             'DM_count': 0, 'event_count': 0,
                             'TP_8w': 0, 'event_count_8w': 0},
            'precision': None, 'recall': None,
            'rationale': 'test',
            'applied': False,
        }
        path = p8.write_artifact(artifact)
        self.assertTrue(path.exists())
        # Second write overwrites the same path (deterministic).
        path2 = p8.write_artifact(artifact)
        self.assertEqual(path, path2)


class TestFormatDigest(unittest.TestCase):

    def _base_artifact(self, rule: str, **kwargs):
        return {
            'as_of': '2026-05-25T12:00:00+00:00',
            'week_anchor': '2026-05-25',
            'rule_fired': rule,
            'current_threshold': 60.0,
            'proposed_threshold': kwargs.get('proposed', 75.0),
            'sample_sizes': {
                'TP': 1, 'FP': 9, 'FN': 0,
                'DM_count': 10, 'event_count': 1,
                'TP_8w': 1, 'event_count_8w': 1,
            },
            'precision': 0.1, 'recall': 1.0,
            'rationale': 'r',
            'applied': False,
        }

    def test_raise_digest_includes_approve_command(self):
        digest = p8.format_digest(self._base_artifact('raise'))
        self.assertIn('RAISE', digest)
        self.assertIn('approve check-viii-update-2026-05-25', digest)

    def test_none_digest_has_no_approve_command(self):
        a = self._base_artifact('none', proposed=None)
        digest = p8.format_digest(a)
        self.assertNotIn('approve check-viii-update-', digest)

    def test_deprecate_digest(self):
        a = self._base_artifact('deprecate', proposed=None)
        digest = p8.format_digest(a)
        self.assertIn('DEPRECATE', digest)
        self.assertIn('approve check-viii-update-2026-05-25', digest)

    def test_defer_digest_no_approve(self):
        a = self._base_artifact('defer', proposed=None)
        digest = p8.format_digest(a)
        self.assertIn('tension', digest.lower())
        self.assertNotIn('approve check-viii-update-', digest)


class TestIsoWeekMonday(unittest.TestCase):

    def test_monday_returns_self(self):
        # 2026-05-25 is a Monday.
        self.assertEqual(p8.iso_week_monday(datetime(2026, 5, 25).date()),
                         '2026-05-25')

    def test_sunday_returns_preceding_monday(self):
        # 2026-05-31 is a Sunday; its ISO-week Monday is 2026-05-25.
        self.assertEqual(p8.iso_week_monday(datetime(2026, 5, 31).date()),
                         '2026-05-25')


if __name__ == '__main__':
    unittest.main()
