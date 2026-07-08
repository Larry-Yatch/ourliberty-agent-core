#!/usr/bin/env python3
"""Tests for pulse_check_v (PR-β action-template trust analyzer).

Covers: graduate rule (>=10 dispatches, 0 Larry mods, guarded),
add_to_guard rule (Larry-correction on non-guarded template), sentinel
idempotency, no-LLM discipline, atomic-write.
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
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_v as p5  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def _row(*, kind: str, template: str, days_ago: float,
         larry_modified: bool = False,
         larry_correction: bool = False) -> dict:
    intervention_id = f'{template}:detail'
    row = {
        'ts': (NOW - timedelta(days=days_ago)).isoformat(),
        'kind': kind,
        'tier': 1,
        'intervention_id': intervention_id,
        'payload': {},
    }
    if larry_modified:
        row['payload']['larry_modified'] = True
    if larry_correction:
        row['payload']['kind'] = 'larry_correction'
    return row


class TestRunCheckGraduate(unittest.TestCase):

    def test_graduates_when_threshold_met_and_guarded(self):
        rows = [
            _row(kind='systemic_fix', template='restart-daemon', days_ago=i)
            for i in range(10)
        ]
        result = p5.run_check(rows=rows,
                              guard_list={'restart-daemon'}, now=NOW)
        self.assertEqual(len(result.proposals), 1)
        p = result.proposals[0]
        self.assertEqual(p.kind, 'graduate')
        self.assertEqual(p.template, 'restart-daemon')

    def test_no_graduate_when_below_threshold(self):
        rows = [
            _row(kind='systemic_fix', template='restart-daemon', days_ago=i)
            for i in range(9)
        ]
        result = p5.run_check(rows=rows,
                              guard_list={'restart-daemon'}, now=NOW)
        graduates = [p for p in result.proposals if p.kind == 'graduate']
        self.assertEqual(graduates, [])

    def test_no_graduate_when_larry_modified(self):
        rows = [
            _row(kind='systemic_fix', template='restart-daemon', days_ago=i,
                 larry_modified=(i == 0))
            for i in range(10)
        ]
        result = p5.run_check(rows=rows,
                              guard_list={'restart-daemon'}, now=NOW)
        graduates = [p for p in result.proposals if p.kind == 'graduate']
        self.assertEqual(graduates, [])

    def test_no_graduate_when_not_guarded(self):
        rows = [
            _row(kind='systemic_fix', template='restart-daemon', days_ago=i)
            for i in range(10)
        ]
        result = p5.run_check(rows=rows, guard_list=set(), now=NOW)
        graduates = [p for p in result.proposals if p.kind == 'graduate']
        self.assertEqual(graduates, [])

    def test_outside_90d_window_dropped(self):
        rows = [
            _row(kind='systemic_fix', template='restart-daemon', days_ago=100)
            for _ in range(15)
        ]
        result = p5.run_check(rows=rows,
                              guard_list={'restart-daemon'}, now=NOW)
        graduates = [p for p in result.proposals if p.kind == 'graduate']
        self.assertEqual(graduates, [])


class TestUncategorizedBucket(unittest.TestCase):
    """The reserved 'uncategorized' template (the ledger's classify-me bucket
    for untagged interventions) must aggregate into ONE stable bucket and
    must NEVER graduate, regardless of track record."""

    def _uncat_row(self, *, kind: str, detail: str, days_ago: float) -> dict:
        return {
            'ts': (NOW - timedelta(days=days_ago)).isoformat(),
            'kind': kind,
            'tier': 1,
            'intervention_id': f'uncategorized:{detail}',
            'payload': {},
        }

    def test_uncategorized_rows_bucket_to_single_template(self):
        rows = [
            self._uncat_row(kind='intervention', detail=f'iter-{i}', days_ago=i)
            for i in range(5)
        ]
        stats = p5.compute_template_stats(rows, now=NOW)
        self.assertEqual(set(stats.keys()), {'uncategorized'})
        self.assertEqual(stats['uncategorized'].dispatch_count_90d, 5)

    def test_uncategorized_never_graduates_even_if_guarded(self):
        """Even with >=10 dispatches, 0 Larry-mods, AND present in the guard
        list, uncategorized produces no graduate proposal — it is reserved
        non-graduating."""
        rows = [
            self._uncat_row(kind='systemic_fix', detail=f'iter-{i}', days_ago=i)
            for i in range(12)
        ]
        result = p5.run_check(rows=rows,
                              guard_list={'uncategorized'}, now=NOW)
        graduates = [p for p in result.proposals if p.kind == 'graduate']
        self.assertEqual(graduates, [])

    def test_real_template_still_graduates_alongside(self):
        """The guard is scoped to 'uncategorized' only — a real template in
        the same run still graduates."""
        rows = [
            _row(kind='systemic_fix', template='restart-daemon', days_ago=i)
            for i in range(10)
        ]
        rows += [
            self._uncat_row(kind='systemic_fix', detail=f'iter-{i}', days_ago=i)
            for i in range(12)
        ]
        result = p5.run_check(
            rows=rows, guard_list={'restart-daemon', 'uncategorized'}, now=NOW)
        graduated = {p.template for p in result.proposals
                     if p.kind == 'graduate'}
        self.assertEqual(graduated, {'restart-daemon'})


class TestRunCheckAddToGuard(unittest.TestCase):

    def test_add_to_guard_on_recent_correction(self):
        rows = [
            _row(kind='intervention', template='auto-bump', days_ago=5,
                 larry_correction=True),
        ]
        result = p5.run_check(rows=rows, guard_list=set(), now=NOW)
        adds = [p for p in result.proposals if p.kind == 'add_to_guard']
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0].template, 'auto-bump')

    def test_no_add_when_already_guarded(self):
        rows = [
            _row(kind='intervention', template='auto-bump', days_ago=5,
                 larry_correction=True),
        ]
        result = p5.run_check(rows=rows,
                              guard_list={'auto-bump'}, now=NOW)
        adds = [p for p in result.proposals if p.kind == 'add_to_guard']
        self.assertEqual(adds, [])

    def test_correction_outside_30d_does_not_fire(self):
        rows = [
            _row(kind='intervention', template='auto-bump', days_ago=45,
                 larry_correction=True),
        ]
        result = p5.run_check(rows=rows, guard_list=set(), now=NOW)
        adds = [p for p in result.proposals if p.kind == 'add_to_guard']
        self.assertEqual(adds, [])


class TestSentinelAndPath(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = p5.PROPOSALS_DIR
        p5.PROPOSALS_DIR = self.tmp / 'proposals'

    def tearDown(self):
        p5.PROPOSALS_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_path_is_month_keyed(self):
        anchor = p5.month_anchor(NOW.date())
        self.assertEqual(anchor, '2026-05')
        path = p5.artifact_path_for_month(anchor)
        self.assertTrue(str(path).endswith('check-v-2026-05.json'))

    def test_write_artifact_atomic_no_tmp_left(self):
        result = p5.run_check(rows=[], guard_list=set(), now=NOW)
        artifact = p5.build_artifact(result)
        path = p5.write_artifact(artifact)
        leftovers = list(path.parent.glob('*.tmp'))
        self.assertEqual(leftovers, [])


class TestNoLLMCalls(unittest.TestCase):

    def test_module_does_not_import_anthropic_or_openai(self):
        src = (_REPO_SCRIPTS / 'pulse_check_v.py').read_text()
        for forbidden in ('import anthropic', 'from anthropic',
                          'import openai', 'from openai'):
            self.assertNotIn(forbidden, src)

    def test_uses_cycle_prime_ledger_not_cycle_actions(self):
        """OQ1 path check (Mirror focus #6). The LEDGER_FILE constant
        must point at cycle-prime-ledger.jsonl. The docstring is allowed
        to mention cycle-actions.jsonl by name (explaining the OQ1
        resolution), but no code path may target it."""
        import pulse_check_v as p5
        self.assertIn('cycle-prime-ledger.jsonl', str(p5.LEDGER_FILE))
        self.assertNotIn('cycle-actions.jsonl', str(p5.LEDGER_FILE))


class TestGuardListLoading(unittest.TestCase):

    def test_missing_config_returns_empty(self):
        bogus = Path(tempfile.mkdtemp()) / 'nope.json'
        self.assertEqual(p5.load_guard_list(bogus), set())

    def test_list_shape_loaded(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                         delete=False) as fh:
            json.dump(['a', 'b', 'c'], fh)
            path = fh.name
        try:
            self.assertEqual(p5.load_guard_list(Path(path)),
                             {'a', 'b', 'c'})
        finally:
            Path(path).unlink()

    def test_object_shape_loaded(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                         delete=False) as fh:
            json.dump({'guarded_templates': ['x', 'y']}, fh)
            path = fh.name
        try:
            self.assertEqual(p5.load_guard_list(Path(path)), {'x', 'y'})
        finally:
            Path(path).unlink()


class TestConsecutiveCleanStreak(unittest.TestCase):
    """Item (d): the triage default flipped to 'unverified'. Check V's streak
    reader must treat 'unverified' as NEUTRAL — it neither increments the streak
    nor breaks it (the verifier just hasn't confirmed it yet); only 'success'
    counts and only 'failure'/a Larry-correction breaks."""

    @staticmethod
    def _ex(outcome, correction=False):
        return {'outcome': outcome, 'larry_correction_signal': correction}

    def test_all_success_counts_all(self):
        exs = [self._ex('success')] * 4
        self.assertEqual(p5.consecutive_clean_streak(exs), 4)

    def test_failure_at_tail_breaks_streak(self):
        exs = [self._ex('success'), self._ex('success'), self._ex('failure')]
        self.assertEqual(p5.consecutive_clean_streak(exs), 0)

    def test_correction_at_tail_breaks_streak(self):
        exs = [self._ex('success'), self._ex('success', correction=True)]
        self.assertEqual(p5.consecutive_clean_streak(exs), 0)

    def test_unverified_is_neutral_skipped(self):
        # tail 'unverified' is skipped; the two older successes still count.
        exs = [self._ex('success'), self._ex('success'), self._ex('unverified')]
        self.assertEqual(p5.consecutive_clean_streak(exs), 2)

    def test_unverified_between_successes_does_not_break(self):
        exs = [self._ex('success'), self._ex('unverified'), self._ex('success')]
        self.assertEqual(p5.consecutive_clean_streak(exs), 2)

    def test_failure_behind_unverified_still_breaks(self):
        # walking from the tail: unverified (skip) -> failure (break); no success
        # after the failure, so the streak is 0.
        exs = [self._ex('success'), self._ex('failure'), self._ex('unverified')]
        self.assertEqual(p5.consecutive_clean_streak(exs), 0)

    def test_empty_is_zero(self):
        self.assertEqual(p5.consecutive_clean_streak([]), 0)
        self.assertEqual(p5.consecutive_clean_streak(None), 0)


if __name__ == '__main__':
    unittest.main()
