#!/usr/bin/env python3
"""Tests for pulse_check_v (PR-β action-template trust analyzer).

Covers: graduate rule (>=10 dispatches, 0 Larry mods, guarded),
add_to_guard rule (Larry-correction on non-guarded template), sentinel
idempotency, no-LLM discipline, atomic-write.
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


if __name__ == '__main__':
    unittest.main()
