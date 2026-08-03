#!/usr/bin/env python3
"""Tests for pulse_check_vi (PR-β PRIME DIRECTIVE posture analyzer).

Covers each of the three proposal-firing rules, atomic-write semantics,
no-LLM discipline, OQ1 path correctness.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_vi as p6  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def _row(*, kind: str, days_ago: float, intervention_id: str = '',
         verified_at: str = None) -> dict:
    return {
        'ts': (NOW - timedelta(days=days_ago)).isoformat(),
        'kind': kind, 'tier': 1,
        'intervention_id': intervention_id,
        'verified_at': verified_at,
    }


class TestComputeMetrics(unittest.TestCase):

    def test_pending_rate_basic(self):
        rows = [
            _row(kind='intervention', days_ago=1),
            _row(kind='systemic_fix', days_ago=2),
            _row(kind='verification_pending', days_ago=3, intervention_id='a'),
        ]
        m = p6.compute_metrics(rows, now=NOW)
        self.assertEqual(m.pending_count, 1)
        self.assertEqual(m.intervention_count, 1)
        self.assertEqual(m.systemic_fix_count, 1)
        self.assertEqual(m.total_count, 3)
        self.assertAlmostEqual(m.pending_rate, 1 / 3, places=4)

    def test_outside_30d_filtered(self):
        rows = [
            _row(kind='intervention', days_ago=60),
            _row(kind='systemic_fix', days_ago=1),
        ]
        m = p6.compute_metrics(rows, now=NOW)
        self.assertEqual(m.intervention_count, 0)
        self.assertEqual(m.systemic_fix_count, 1)

    def test_auto_promote_and_stuck_forever_rates(self):
        # 'p1' is a pending row older than 7d that was promoted.
        # 'p2' is a pending row older than 7d that was NOT promoted (stuck).
        # 'p3' is a pending row younger than 7d (not yet aged).
        rows = [
            _row(kind='verification_pending', days_ago=10,
                 intervention_id='p1'),
            _row(kind='systemic_fix', days_ago=4, intervention_id='p1',
                 verified_at=(NOW - timedelta(days=4)).isoformat()),
            _row(kind='verification_pending', days_ago=12,
                 intervention_id='p2'),
            _row(kind='verification_pending', days_ago=2,
                 intervention_id='p3'),
        ]
        m = p6.compute_metrics(rows, now=NOW)
        # Aged pending = p1 + p2 = 2; promoted = 1; stuck = 1.
        self.assertEqual(m.auto_promote_count, 1)
        self.assertEqual(m.stuck_forever_count, 1)
        self.assertAlmostEqual(m.auto_promote_rate, 0.5, places=2)
        self.assertAlmostEqual(m.stuck_forever_rate, 0.5, places=2)


class TestRules(unittest.TestCase):

    def _result_with_metrics(self, metrics: p6.PostureMetrics):
        """Bypass compute_metrics; inject the metrics directly so we can
        target rule firing without contriving long row sequences."""
        # We can't easily inject metrics through run_check, so we test
        # via crafted rows that produce desired metrics.
        # Easier: target each rule with focused row sets.
        raise NotImplementedError

    def test_tighten_lenient_fires(self):
        # Need: pending_rate > 0.40 AND auto_promote_rate > 0.80.
        rows = []
        # 3 pending rows older than 7d, all promoted -> auto_promote=1.0,
        # stuck=0.
        for i, iid in enumerate(['p1', 'p2', 'p3']):
            rows.append(_row(kind='verification_pending', days_ago=10,
                             intervention_id=iid))
            rows.append(_row(kind='systemic_fix', days_ago=4,
                             intervention_id=iid,
                             verified_at=(NOW - timedelta(days=4)).isoformat()))
        # 1 plain intervention so total = 3 pending + 3 fix + 1 = 7,
        # pending_rate = 3/7 ≈ 0.43 > 0.40.
        rows.append(_row(kind='intervention', days_ago=2))
        result = p6.run_check(rows=rows, now=NOW)
        rules = {p.rule for p in result.proposals}
        self.assertIn('tighten_lenient', rules)

    def test_tighten_masking_fires_when_pending_low_and_trend_not_improving(self):
        # pending_rate < 0.05, trend != improving. Build rows where
        # interventions = systemic_fixes in both halves -> flat trend, plus ONE
        # live pending row so the rate is low-but-nonzero (1/41 ≈ 0.02).
        # The row is required: with pending=0 the retired-category
        # short-circuit precedes this rule and no proposal is emitted (that is
        # the whole point of the retirement — see
        # TestRetiredCategoryShortCircuit). It is dated inside the 7d promote
        # window so it is not "aged", keeping rule 3 out of the result.
        rows = [_row(kind='verification_pending', days_ago=2,
                     intervention_id='live')]
        for h in (20 * 24, 1):  # older + recent halves
            for _ in range(10):
                rows.append({
                    'ts': (NOW - timedelta(hours=h)).isoformat(),
                    'kind': 'intervention', 'tier': 1,
                    'intervention_id': '', 'verified_at': None,
                })
                rows.append({
                    'ts': (NOW - timedelta(hours=h)).isoformat(),
                    'kind': 'systemic_fix', 'tier': 1,
                    'intervention_id': '', 'verified_at': None,
                })
        metrics = p6.compute_metrics(rows, now=NOW)
        self.assertLess(metrics.pending_rate, p6.PENDING_LOW_RATE)
        result = p6.run_check(rows=rows, now=NOW)
        rules = {p.rule for p in result.proposals}
        self.assertIn('tighten_masking', rules)

    def test_stricter_unverifiable_fires(self):
        # stuck_forever_rate > 0.30 — need most aged pending to be unpromoted.
        rows = []
        for i in range(5):
            rows.append(_row(kind='verification_pending', days_ago=10,
                             intervention_id=f'p{i}'))
        result = p6.run_check(rows=rows, now=NOW)
        rules = {p.rule for p in result.proposals}
        self.assertIn('stricter_unverifiable', rules)

    def test_calm_state_yields_no_proposals(self):
        # Calm: pending_rate at ~0.10 (above masking floor 0.05, below
        # lenient floor 0.40), improving trend, stuck_forever 0.
        # 1 verification_pending (recent, ages NOT past 7d window) +
        # interventions/fixes balanced -> stays below all firing rules.
        rows = []
        # Older half (15-30d ago): worse ratio (5 interventions / 1 fix).
        for _ in range(5):
            rows.append(_row(kind='intervention', days_ago=20))
        rows.append(_row(kind='systemic_fix', days_ago=20))
        # Recent half (0-15d): better ratio (1 intervention / 5 fixes)
        # -> trend = improving.
        rows.append(_row(kind='intervention', days_ago=2))
        for _ in range(5):
            rows.append(_row(kind='systemic_fix', days_ago=2))
        # 1 pending row within the promote window (no stuck-forever).
        rows.append(_row(kind='verification_pending', days_ago=2,
                         intervention_id='fresh'))
        result = p6.run_check(rows=rows, now=NOW)
        self.assertEqual(
            result.proposals, [],
            f'unexpected proposals: {[p.rule for p in result.proposals]}',
        )


class TestArtifactAtomicity(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = p6.PROPOSALS_DIR
        p6.PROPOSALS_DIR = self.tmp / 'proposals'

    def tearDown(self):
        p6.PROPOSALS_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_tmp_after_write(self):
        result = p6.run_check(rows=[], now=NOW)
        artifact = p6.build_artifact(result)
        path = p6.write_artifact(artifact)
        leftovers = list(path.parent.glob('*.tmp'))
        self.assertEqual(leftovers, [])


class TestRetiredCategoryShortCircuit(unittest.TestCase):
    """verification_pending retired 2026-08-03 (cycle-prompt.md § 6.2).

    With no NEW pending rows in the window the check must short-circuit BEFORE
    every proposal rule and emit zero proposals.
    """

    def test_zero_pending_rows_short_circuits_to_retired(self):
        rows = [
            _row(kind='intervention', days_ago=d) for d in range(1, 20)
        ] + [_row(kind='systemic_fix', days_ago=5)]
        res = p6.run_check(rows=rows, now=NOW)
        self.assertEqual(res.rule_fired, 'retired')
        self.assertEqual(res.proposals, [])
        self.assertIn('retired', res.rationale)

    def test_does_not_fire_tighten_masking_off_zero_pending_rate(self):
        """The load-bearing guard: a retired category drives pending_rate to
        0.0, which satisfies rule 2's `< 0.05` forever. Without the
        short-circuit Check VI proposes 'tighten' on an empty category every
        month. These rows are built to satisfy rule 2 in full."""
        rows = [
            _row(kind='intervention', days_ago=d) for d in range(1, 15)
        ] + [_row(kind='systemic_fix', days_ago=20)]
        metrics = p6.compute_metrics(rows, now=NOW)
        self.assertEqual(metrics.pending_rate, 0.0)
        self.assertLess(metrics.pending_rate, p6.PENDING_LOW_RATE)
        self.assertNotEqual(metrics.ratio_trend, 'improving')

        res = p6.run_check(rows=rows, now=NOW)
        self.assertEqual([p.rule for p in res.proposals], [])
        self.assertEqual(res.rule_fired, 'retired')

    def test_historical_rows_only_still_zero_proposals(self):
        """Historical pending rows sit outside the 30d window; they must not
        revive the category."""
        rows = [
            _row(kind='verification_pending', days_ago=90,
                 intervention_id='old-a'),
            _row(kind='verification_pending', days_ago=200,
                 intervention_id='old-b'),
            _row(kind='intervention', days_ago=3),
        ]
        res = p6.run_check(rows=rows, now=NOW)
        self.assertEqual(res.rule_fired, 'retired')
        self.assertEqual(res.proposals, [])

    def test_retired_artifact_emits_no_dm(self):
        rows = [_row(kind='intervention', days_ago=2)]
        artifact = p6.build_artifact(p6.run_check(rows=rows, now=NOW))
        self.assertEqual(artifact['rule_fired'], 'retired')
        self.assertEqual(artifact['proposals'], [])
        self.assertFalse(p6.dm_digest(artifact))
        self.assertIn('retired', p6.format_digest(artifact))

    def test_short_circuit_precedes_every_proposal_rule(self):
        """Structural guard: the retired check must sit ahead of rules 1-3 in
        run_check's source, not merely filter their output."""
        src = (_REPO_SCRIPTS / 'pulse_check_vi.py').read_text()
        body = src.split('def run_check(', 1)[1]
        self.assertLess(body.index("rule_fired='retired'"),
                        body.index("rule='tighten_lenient'"))
        self.assertLess(body.index("rule_fired='retired'"),
                        body.index("rule='tighten_masking'"))
        self.assertLess(body.index("rule_fired='retired'"),
                        body.index("rule='stricter_unverifiable'"))


class TestNoLLMCalls(unittest.TestCase):

    def test_module_does_not_import_llm_clients(self):
        src = (_REPO_SCRIPTS / 'pulse_check_vi.py').read_text()
        for forbidden in ('import anthropic', 'from anthropic',
                          'import openai', 'from openai'):
            self.assertNotIn(forbidden, src)

    def test_oq1_path_consistency(self):
        src = (_REPO_SCRIPTS / 'pulse_check_vi.py').read_text()
        self.assertNotIn('cycle-actions.jsonl', src)
        self.assertIn('cycle-prime-ledger.jsonl', src)


if __name__ == '__main__':
    unittest.main()
