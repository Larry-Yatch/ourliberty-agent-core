#!/usr/bin/env python3
"""Tests for pulse_check_viii (Check VIII PR-2b).

Covers each proposal-firing rule with synthetic fixture corpora, the
fixture-pattern filter on quota events, the idempotency artifact write,
and the digest format. Re-based 2026-05-28 onto the token-volume signal.

larry-alerts and config IO are never touched live — every test exercises
the pure-function core (`run_check`) with injected inputs.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_viii
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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


def _dm(hours_ago: float, usage: int = 9_000_000,
        threshold: int = 10_000_000):
    return p8.BurnRateDM(
        ts=NOW - timedelta(hours=hours_ago),
        usage_tokens=usage, threshold_tokens=threshold,
    )


def _event(hours_ago: float, task_id: str = 'real-task-001', agent: str = 'beacon'):
    return p8.QuotaEvent(
        ts=NOW - timedelta(hours=hours_ago),
        task_id=task_id, agent=agent,
    )


def _cost(hours_ago: float, tokens: int):
    return p8.CostEntry(
        ts=NOW - timedelta(hours=hours_ago),
        usage_tokens=tokens,
    )


class TestParseDmBody(unittest.TestCase):

    def test_extracts_usage_and_threshold(self):
        body = (
            'Trailing 5h quota pace at 92% of token gate '
            '(9,200,000 of 10,000,000 tokens; input+output+cache_creation). '
            'Pace indicator only ...'
        )
        out = p8.parse_dm_body(body)
        self.assertEqual(out, (9_200_000, 10_000_000))

    def test_handles_uncommatized_numbers(self):
        body = 'token gate (5000 of 10000 tokens; ...).'
        self.assertEqual(p8.parse_dm_body(body), (5000, 10000))

    def test_no_match_returns_none(self):
        self.assertIsNone(p8.parse_dm_body('random text without tokens'))

    def test_pre_re_base_dollar_body_returns_none(self):
        # Old DM bodies (pre-2026-05-28) used "$X of $Y" — the new parser
        # should NOT match those; they're filtered out of the corpus.
        old_body = (
            'Trailing 5h LLM pace at 92% of dollar gate '
            '($55.20 of $60.00). ...'
        )
        self.assertIsNone(p8.parse_dm_body(old_body))

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


class TestRollingTokens(unittest.TestCase):

    def test_sums_tokens_within_5h_window_before_target(self):
        target = NOW
        costs = [
            _cost(hours_ago=1, tokens=2_000_000),   # in window
            _cost(hours_ago=4, tokens=3_000_000),   # in window
            _cost(hours_ago=6, tokens=10_000_000),  # outside
        ]
        out = p8.rolling_5h_tokens_at(target, costs)
        self.assertEqual(out, 5_000_000)


class TestRunCheckRules(unittest.TestCase):

    CURRENT_THRESHOLD = 10_000_000

    def _run(self, *, dms, events, costs=None, gate_enabled=True):
        return p8.run_check(
            dms=dms, events=events,
            costs=costs or [],
            current_threshold_tokens=self.CURRENT_THRESHOLD,
            gate_enabled=gate_enabled,
            now=NOW,
        )

    # ---- insufficient_signal ----

    def test_insufficient_signal_when_too_few_dms(self):
        # 4 DMs (< floor 5) + 3 events should still trip insufficient_signal.
        dms = [_dm(hours_ago=10 + i * 24) for i in range(4)]
        events = [_event(hours_ago=8 + i * 24) for i in range(3)]
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'insufficient_signal')
        self.assertIsNone(result.proposed_threshold_tokens)

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
        # (avoids defer). Proposed = p75 of TP usages [7M, 8M, 9M] = 8.5M.
        dms = []
        events = []
        for i, usage in enumerate([7_000_000, 8_000_000, 9_000_000]):
            base = 24 + i * 12
            dms.append(_dm(hours_ago=base, usage=usage))
            events.append(_event(hours_ago=base - 0.5))
        for j in range(7):
            dms.append(_dm(hours_ago=200 + j * 24, usage=2_000_000))
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'raise')
        self.assertEqual(result.proposed_threshold_tokens, 8_500_000)

    def test_raise_proposed_is_p75_of_tp_usages(self):
        # 8 DMs: 5 FPs at low usage + 3 TPs paired with 3 events at high
        # usage. precision = 3/8 = 0.375 (< 0.4 → raise). recall = 1.0.
        # Proposed = 75th pct of TP usages [12M, 12M, 12M] = 12M.
        dms = [_dm(hours_ago=10 + i * 24, usage=2_000_000) for i in range(5)]
        events = []
        for i in range(3):
            evhrs = 100 + i * 24
            events.append(_event(hours_ago=evhrs))
            dms.append(_dm(hours_ago=evhrs + 0.5, usage=12_000_000))
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'raise')
        self.assertEqual(result.proposed_threshold_tokens, 12_000_000)

    # ---- lower ----

    def test_lower_fires_when_recall_low(self):
        # We need recall < 0.6 (>= 3 events), precision NOT < 0.4 (else
        # defer). 5 DMs all TP → precision = 1.0. recall = 5/10 = 0.5
        # < 0.6 → lower fires.
        dms = []
        events = []
        for i in range(5):
            base = 24 + i * 6
            dms.append(_dm(hours_ago=base, usage=6_000_000))
            events.append(_event(hours_ago=base - 0.5))
        # 5 unpaired events (FNs) at distant times.
        fn_event_hours = [200 + i * 12 for i in range(5)]
        for h in fn_event_hours:
            events.append(_event(hours_ago=h))
        # Tokens at FN-event moments (rolling-5h tokens each = 4M).
        costs = []
        for h in fn_event_hours:
            costs.append(_cost(hours_ago=h, tokens=2_000_000))
            costs.append(_cost(hours_ago=h + 1, tokens=2_000_000))  # within 5h
        result = self._run(dms=dms, events=events, costs=costs)
        self.assertEqual(result.rule_fired, 'lower',
                         f'rule={result.rule_fired}, precision={result.precision}, '
                         f'recall={result.recall}, TP={result.tp_count}, '
                         f'FP={result.fp_count}, FN={result.fn_count}')
        # 75th-pct of [4_000_000]*5 = 4_000_000.
        self.assertEqual(result.proposed_threshold_tokens, 4_000_000)

    def test_lower_fallback_when_no_fn_token_data(self):
        # Recall low, FNs exist, but no costs in window → fallback to -20%.
        dms = []
        events = []
        for i in range(5):
            base = 24 + i * 6
            dms.append(_dm(hours_ago=base, usage=6_000_000))
            events.append(_event(hours_ago=base - 0.5))
        for h in [200, 224, 248, 272, 296]:
            events.append(_event(hours_ago=h))
        result = self._run(dms=dms, events=events, costs=[])
        self.assertEqual(result.rule_fired, 'lower')
        # 10_000_000 * 0.80 = 8_000_000
        self.assertEqual(result.proposed_threshold_tokens, 8_000_000)

    # ---- defer ----

    def test_defer_when_both_raise_and_lower_fire(self):
        # 10 DMs, 1 of which is TP. 9 FPs.
        # 4 events, 1 paired (TP), 3 unpaired (FN).
        # precision = 1/10 = 0.1 < 0.4 (raise).
        # recall = 1/(1+3) = 0.25 < 0.6 (lower).
        # Both fire → defer.
        dms = []
        events = []
        dms.append(_dm(hours_ago=24, usage=9_000_000))
        events.append(_event(hours_ago=23.5))
        for j in range(9):
            dms.append(_dm(hours_ago=100 + j * 12, usage=1_000_000))
        for k in range(3):
            events.append(_event(hours_ago=400 + k * 6))
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'defer')
        self.assertIsNone(result.proposed_threshold_tokens)

    # ---- deprecate ----

    def test_deprecate_when_zero_tp_8w_and_many_events(self):
        # 8w window: TP = 0 (no DM has an event within 2h after).
        # 5+ events scattered in the 8w window.
        dms = [_dm(hours_ago=24 + i * 24, usage=3_000_000) for i in range(5)]
        # Place events 10h BEFORE each DM — outside the 2h-after window.
        events = [_event(hours_ago=24 + i * 24 + 10) for i in range(6)]
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'deprecate')
        self.assertIsNone(result.proposed_threshold_tokens)
        self.assertEqual(result.tp_count_8w, 0)
        self.assertGreaterEqual(result.event_count_8w, 5)

    def test_deprecate_rationale_uses_token_framing(self):
        dms = [_dm(hours_ago=24 + i * 24, usage=3_000_000) for i in range(5)]
        events = [_event(hours_ago=24 + i * 24 + 10) for i in range(6)]
        result = self._run(dms=dms, events=events)
        self.assertIn('token gate', result.rationale)
        # No dollar denomination anywhere in the rationale.
        self.assertNotIn('$', result.rationale)
        self.assertNotIn('dollar gate', result.rationale)

    def test_deprecate_takes_priority_over_lower(self):
        # Even if recall would be < 0.6, deprecate (TP_8w==0, ≥5 events_8w)
        # fires first.
        dms = [_dm(hours_ago=24 + i * 24, usage=3_000_000) for i in range(5)]
        events = [_event(hours_ago=24 + i * 24 + 10) for i in range(6)]
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'deprecate')

    # ---- already_deprecated (gate disabled) ----

    def test_disabled_gate_short_circuits_deprecate(self):
        # Same signal that would otherwise fire deprecate (TP_8w==0, ≥5
        # events_8w), but with the gate disabled: the deprecate re-proposal is
        # suppressed and no DM is emitted.
        dms = [_dm(hours_ago=24 + i * 24, usage=3_000_000) for i in range(5)]
        events = [_event(hours_ago=24 + i * 24 + 10) for i in range(6)]
        result = self._run(dms=dms, events=events, gate_enabled=False)
        self.assertEqual(result.rule_fired, 'already_deprecated')
        self.assertIsNone(result.proposed_threshold_tokens)
        # No DM for the no-op rule.
        self.assertFalse(p8.dm_digest(p8.build_artifact(result)))

    def test_enabled_gate_still_fires_deprecate_same_signal(self):
        # Regression guard: the short-circuit is gated ONLY on the flag — the
        # identical inputs still yield deprecate when the gate is enabled.
        dms = [_dm(hours_ago=24 + i * 24, usage=3_000_000) for i in range(5)]
        events = [_event(hours_ago=24 + i * 24 + 10) for i in range(6)]
        result = self._run(dms=dms, events=events, gate_enabled=True)
        self.assertEqual(result.rule_fired, 'deprecate')

    # ---- none ----

    def test_both_healthy_returns_none(self):
        # precision = 5/5 = 1.0, recall = 5/(5+1) ≈ 0.83.
        dms = []
        events = []
        for i in range(5):
            base = 24 + i * 6
            dms.append(_dm(hours_ago=base, usage=8_000_000))
            events.append(_event(hours_ago=base - 0.5))
        events.append(_event(hours_ago=400))  # 1 lonely FN
        result = self._run(dms=dms, events=events)
        self.assertEqual(result.rule_fired, 'none')
        self.assertIsNone(result.proposed_threshold_tokens)


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


class TestLoadCostEntries(unittest.TestCase):

    def test_sums_quota_consuming_tokens_and_excludes_cache_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'costs.jsonl'
            recs = [
                {'ts': (NOW - timedelta(hours=2)).isoformat(),
                 'input_tokens': 100, 'output_tokens': 200,
                 'cache_creation': 1000, 'cache_read': 99999,
                 'cost_usd': 1.0},
                {'ts': (NOW - timedelta(hours=3)).isoformat(),
                 'input_tokens': 50, 'output_tokens': 50,
                 'cache_creation': 0, 'cache_read': 1000000,
                 'cost_usd': 0.5},
            ]
            path.write_text('\n'.join(json.dumps(r) for r in recs))
            costs = p8.load_cost_entries(path, now=NOW)
            self.assertEqual(len(costs), 2)
            # Tokens = io + cache_creation; cache_read excluded.
            self.assertEqual(costs[0].usage_tokens, 1300)
            self.assertEqual(costs[1].usage_tokens, 100)


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
            'unit': 'tokens',
            'current_threshold': 10_000_000,
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


class TestBuildArtifact(unittest.TestCase):
    """The artifact schema is the contract Beacon's approve flow reads.
    Pin the field shapes so a refactor can't break the downstream flow."""

    def test_artifact_carries_token_unit_and_int_thresholds(self):
        result = p8.run_check(
            dms=[], events=[],
            costs=[],
            current_threshold_tokens=10_000_000,
            now=NOW,
        )
        art = p8.build_artifact(result)
        self.assertEqual(art['unit'], 'tokens')
        self.assertEqual(art['current_threshold'], 10_000_000)
        self.assertIsNone(art['proposed_threshold'])


class TestFormatDigest(unittest.TestCase):

    def _base_artifact(self, rule: str, **kwargs):
        return {
            'as_of': '2026-05-25T12:00:00+00:00',
            'week_anchor': '2026-05-25',
            'rule_fired': rule,
            'unit': 'tokens',
            'current_threshold': 10_000_000,
            'proposed_threshold': kwargs.get('proposed', 12_500_000),
            'sample_sizes': {
                'TP': 1, 'FP': 9, 'FN': 0,
                'DM_count': 10, 'event_count': 1,
                'TP_8w': 1, 'event_count_8w': 1,
            },
            'precision': 0.1, 'recall': 1.0,
            'rationale': 'r',
            'applied': False,
        }

    def test_raise_digest_includes_approve_command_and_token_units(self):
        digest = p8.format_digest(self._base_artifact('raise'))
        self.assertIn('RAISE', digest)
        self.assertIn('approve check-viii-update-2026-05-25', digest)
        self.assertIn('10,000,000 tokens', digest)
        self.assertIn('12,500,000 tokens', digest)
        self.assertNotIn('$', digest)

    def test_none_digest_has_no_approve_command(self):
        a = self._base_artifact('none', proposed=None)
        digest = p8.format_digest(a)
        self.assertNotIn('approve check-viii-update-', digest)

    def test_deprecate_digest(self):
        a = self._base_artifact('deprecate', proposed=None)
        digest = p8.format_digest(a)
        self.assertIn('DEPRECATE', digest)
        self.assertIn('token gate', digest)
        self.assertIn('approve check-viii-update-2026-05-25', digest)
        self.assertNotIn('dollar gate', digest)

    def test_defer_digest_no_approve(self):
        a = self._base_artifact('defer', proposed=None)
        digest = p8.format_digest(a)
        self.assertIn('tension', digest.lower())
        self.assertNotIn('approve check-viii-update-', digest)

    def test_already_deprecated_digest_no_approve(self):
        a = self._base_artifact('already_deprecated', proposed=None)
        digest = p8.format_digest(a)
        self.assertIn('already deprecated', digest.lower())
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
