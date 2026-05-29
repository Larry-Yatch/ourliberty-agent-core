#!/usr/bin/env python3
"""Tests for pulse_check_vii (PR-β cost-ceiling escalation analyzer).

Covers: remove rule (20 consecutive same decision), raise rule (10
consecutive keep_going on low band), lower rule (10 consecutive
throttle on low band), atomic-write, no-LLM discipline.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_vii as p7  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def _row(*, decision: str, hours_ago: float, band: str = p7.LOW_BAND,
         threshold: float = 50.0) -> p7.EscalationRow:
    return p7.EscalationRow(
        ts=NOW - timedelta(hours=hours_ago),
        band=band, threshold_usd=threshold,
        spend_usd_at_dm=threshold + 1.0,
        decision=decision,
    )


class TestRemoveRule(unittest.TestCase):

    def test_remove_fires_at_20_consecutive_keep_going(self):
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=20 - i)
                for i in range(20)]
        result = p7.run_check(rows=rows, now=NOW)
        removes = [p for p in result.proposals if p.rule == 'remove']
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0].band, p7.LOW_BAND)

    def test_remove_fires_at_20_consecutive_throttle(self):
        rows = [_row(decision=p7.DECISION_THROTTLE, hours_ago=20 - i,
                     band=p7.HIGH_BAND, threshold=100.0)
                for i in range(20)]
        result = p7.run_check(rows=rows, now=NOW)
        removes = [p for p in result.proposals if p.rule == 'remove']
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0].band, p7.HIGH_BAND)

    def test_remove_does_not_fire_below_20(self):
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=20 - i)
                for i in range(19)]
        result = p7.run_check(rows=rows, now=NOW)
        removes = [p for p in result.proposals if p.rule == 'remove']
        self.assertEqual(removes, [])

    def test_remove_does_not_fire_on_mixed_decisions(self):
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=21 - i)
                for i in range(19)]
        rows.append(_row(decision=p7.DECISION_THROTTLE, hours_ago=1))
        result = p7.run_check(rows=rows, now=NOW)
        removes = [p for p in result.proposals if p.rule == 'remove']
        self.assertEqual(removes, [])


class TestRaiseRule(unittest.TestCase):

    def test_raise_fires_at_10_consecutive_keep_going_low(self):
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=10 - i)
                for i in range(10)]
        # Need high-band reference too so the proposal target is the
        # midpoint between low ($50) and high ($100) = $75.
        rows.insert(0, _row(decision=p7.DECISION_KEEP, hours_ago=200,
                            band=p7.HIGH_BAND, threshold=100.0))
        result = p7.run_check(rows=rows, now=NOW)
        raises = [p for p in result.proposals if p.rule == 'raise']
        self.assertEqual(len(raises), 1)
        self.assertEqual(raises[0].proposed_threshold_usd, 75.0)

    def test_raise_only_applies_to_low_band(self):
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=10 - i,
                     band=p7.HIGH_BAND, threshold=100.0)
                for i in range(10)]
        result = p7.run_check(rows=rows, now=NOW)
        raises = [p for p in result.proposals if p.rule == 'raise']
        self.assertEqual(raises, [])

    def test_no_raise_when_mixed_decisions(self):
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=10 - i)
                for i in range(9)]
        rows.append(_row(decision=p7.DECISION_SILENT, hours_ago=1))
        result = p7.run_check(rows=rows, now=NOW)
        raises = [p for p in result.proposals if p.rule == 'raise']
        self.assertEqual(raises, [])


class TestLowerRule(unittest.TestCase):

    def test_lower_fires_at_10_consecutive_throttle_low(self):
        rows = [_row(decision=p7.DECISION_THROTTLE, hours_ago=10 - i)
                for i in range(10)]
        result = p7.run_check(rows=rows, now=NOW)
        lowers = [p for p in result.proposals if p.rule == 'lower']
        self.assertEqual(len(lowers), 1)
        # $50 * 0.80 = $40.
        self.assertEqual(lowers[0].proposed_threshold_usd, 40.0)

    def test_lower_only_applies_to_low_band(self):
        rows = [_row(decision=p7.DECISION_THROTTLE, hours_ago=10 - i,
                     band=p7.HIGH_BAND, threshold=100.0)
                for i in range(10)]
        result = p7.run_check(rows=rows, now=NOW)
        lowers = [p for p in result.proposals if p.rule == 'lower']
        self.assertEqual(lowers, [])


class TestRulePriority(unittest.TestCase):

    def test_remove_takes_priority_over_raise(self):
        # 20 keep_going on low band: remove fires (no raise/lower).
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=20 - i)
                for i in range(20)]
        result = p7.run_check(rows=rows, now=NOW)
        kinds = {p.rule for p in result.proposals}
        self.assertIn('remove', kinds)
        self.assertNotIn('raise', kinds)
        self.assertNotIn('lower', kinds)


class TestArtifactAtomicity(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = p7.PROPOSALS_DIR
        p7.PROPOSALS_DIR = self.tmp / 'proposals'

    def tearDown(self):
        p7.PROPOSALS_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_tmp_after_write(self):
        rows = [_row(decision=p7.DECISION_KEEP, hours_ago=10 - i)
                for i in range(10)]
        result = p7.run_check(rows=rows, now=NOW)
        artifact = p7.build_artifact(result)
        path = p7.write_artifact(artifact)
        leftovers = list(path.parent.glob('*.tmp'))
        self.assertEqual(leftovers, [])


class TestNoLLMCalls(unittest.TestCase):

    def test_module_does_not_import_llm_clients(self):
        src = (_REPO_SCRIPTS / 'pulse_check_vii.py').read_text()
        for forbidden in ('import anthropic', 'from anthropic',
                          'import openai', 'from openai'):
            self.assertNotIn(forbidden, src)


class TestReadLog(unittest.TestCase):

    def test_log_parser_drops_invalid_rows(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl',
                                         delete=False) as fh:
            fh.write(json.dumps({
                'ts': NOW.isoformat(), 'band': 'low',
                'threshold_usd': 50.0, 'spend_usd_at_dm': 51.0,
                'larry_decision': 'keep_going',
            }) + '\n')
            fh.write('{garbage line}\n')
            fh.write(json.dumps({
                'ts': NOW.isoformat(), 'band': 'bogus',
                'threshold_usd': 50.0, 'spend_usd_at_dm': 51.0,
                'larry_decision': 'keep_going',
            }) + '\n')
            path = fh.name
        try:
            rows = p7._read_log(Path(path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].band, 'low')
        finally:
            Path(path).unlink()


if __name__ == '__main__':
    unittest.main()
