#!/usr/bin/env python3
"""Tests for sort_once_tier4_cleanup — the one-time grouped tier-4 cleanup pass.

Exercises the pure classification core (is_target_row / classify_row /
build_plan / build_artifact) with synthetic triage rows, the digest format, and
the apply path against injected fake ats/ledger modules — so no live triage
state, ledger, or larry-alerts is ever touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_sort_once_tier4_cleanup
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import sort_once_tier4_cleanup as soc  # noqa: E402


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _row(alert_id: str, *, tier: int = 4, status: str = 'triaged-tier-4',
         decision: str = 'ask', rationale: str = 'novel: no registry template',
         days_old: float = 40.0, resolved: bool = False) -> dict:
    triaged_at = (NOW - timedelta(days=days_old)).isoformat()
    return {
        'alert_id': alert_id,
        'tier': tier,
        'status': 'resolved' if resolved else status,
        'decision': decision,
        'rationale': rationale,
        'route': 'escalate',
        'template': None,
        'triaged_at': triaged_at,
        'resolved_at': triaged_at if resolved else None,
        'resolution': 'x' if resolved else None,
        'last_updated': triaged_at,
    }


class TestIsTargetRow(unittest.TestCase):
    def test_open_tier4_ask_is_target(self):
        self.assertTrue(soc.is_target_row(_row('1')))

    def test_resolved_row_excluded(self):
        self.assertFalse(soc.is_target_row(_row('1', resolved=True)))

    def test_row_with_resolved_at_excluded(self):
        row = _row('1')
        row['resolved_at'] = NOW.isoformat()
        self.assertFalse(soc.is_target_row(row))

    def test_ask_decision_alone_is_target(self):
        # tier/status not the tier-4 shape but decision=='ask' still counts
        row = _row('1', tier=3, status='triaged-tier-3', decision='ask')
        self.assertTrue(soc.is_target_row(row))

    def test_unrelated_tier3_excluded(self):
        row = _row('1', tier=3, status='triaged-tier-3', decision='dispatch')
        self.assertFalse(soc.is_target_row(row))

    def test_non_dict_excluded(self):
        self.assertFalse(soc.is_target_row(None))
        self.assertFalse(soc.is_target_row('nope'))


class TestClassifyRow(unittest.TestCase):
    def test_recurring_signature_kept_even_when_old(self):
        row = _row('1', rationale='known never-silence pattern in translations',
                   days_old=999)
        self.assertEqual(soc.classify_row(row, NOW, 30), 'keep')

    def test_novel_old_one_off_retires(self):
        row = _row('1', days_old=40)
        self.assertEqual(soc.classify_row(row, NOW, 30), 'retire')

    def test_novel_young_one_off_parks(self):
        row = _row('1', days_old=5)
        self.assertEqual(soc.classify_row(row, NOW, 30), 'park')

    def test_boundary_at_threshold_retires(self):
        row = _row('1', days_old=30)
        self.assertEqual(soc.classify_row(row, NOW, 30), 'retire')

    def test_undatable_row_parks_never_retires(self):
        row = _row('1', days_old=40)
        row['triaged_at'] = 'not-a-timestamp'
        self.assertEqual(soc.classify_row(row, NOW, 30), 'park')


class TestBuildPlanAndArtifact(unittest.TestCase):
    def _rows(self):
        return {
            'r1': _row('r1', days_old=40),                       # retire
            'r2': _row('r2', days_old=45),                       # retire
            'p1': _row('p1', days_old=3),                        # park
            'k1': _row('k1', rationale='known never-silence pattern', days_old=90),  # keep
            'done': _row('done', days_old=99, resolved=True),    # excluded
            'noise': _row('noise', tier=3, status='triaged-tier-3', decision='dispatch'),  # excluded
        }

    def test_build_plan_groups(self):
        groups = soc.build_plan(self._rows(), NOW, 30)
        self.assertEqual(groups['retire'], ['r1', 'r2'])
        self.assertEqual(groups['park'], ['p1'])
        self.assertEqual(groups['keep'], ['k1'])

    def test_groups_sorted(self):
        rows = {'b': _row('b', days_old=40), 'a': _row('a', days_old=40)}
        self.assertEqual(soc.build_plan(rows, NOW, 30)['retire'], ['a', 'b'])

    def test_build_artifact_shape(self):
        groups = soc.build_plan(self._rows(), NOW, 30)
        art = soc.build_artifact(groups, NOW, 30)
        self.assertFalse(art['applied'])
        self.assertIsNone(art['applied_at'])
        self.assertEqual(art['slug'], soc.APPROVE_SLUG)
        self.assertEqual(art['retire_age_days'], 30)
        self.assertEqual(art['counts'], {'keep': 1, 'retire': 2, 'park': 1})
        self.assertEqual(art['total_targets'], 4)
        self.assertEqual(art['retire'], ['r1', 'r2'])


class TestDigest(unittest.TestCase):
    def test_digest_carries_grouped_approve_slug(self):
        groups = {'keep': ['k'], 'retire': ['a', 'b'], 'park': ['p']}
        art = soc.build_artifact(groups, NOW, 30)
        text = soc.format_digest(art)
        self.assertIn(f'approve {soc.APPROVE_SLUG}-2026-07-21', text)
        self.assertIn('retire', text)
        self.assertIn('keep', text)
        self.assertIn('park', text)


class _FakeAts:
    def __init__(self, existing):
        self.existing = set(existing)
        self.resolved = []

    def mark_resolved(self, alert_id, ts, resolution):
        if alert_id not in self.existing:
            return False
        self.resolved.append((alert_id, ts, resolution))
        return True


class _FakeLedger:
    def __init__(self):
        self.records = []

    def record_decision(self, decision_key, outcome, *, actor=None,
                        cleared=0, notes=None):
        self.records.append((decision_key, outcome, actor, cleared))
        return True


class TestApply(unittest.TestCase):
    def _artifact(self):
        groups = {'keep': ['k1'], 'retire': ['r1', 'r2'], 'park': ['p1']}
        return soc.build_artifact(groups, NOW, 30)

    def test_apply_retires_and_records(self):
        art = self._artifact()
        ats = _FakeAts(existing=['r1', 'r2'])
        ledger = _FakeLedger()
        summary = soc.apply_cleanup(art, NOW, ats_mod=ats, ledger_mod=ledger)
        self.assertEqual(summary['retired'], 2)
        self.assertEqual(summary['ledger_recorded'], 2)
        self.assertEqual(summary['missing'], [])
        self.assertTrue(art['applied'])
        self.assertIsNotNone(art['applied_at'])
        # every resolution used the 'expired' vocabulary
        self.assertTrue(all(r[2] == 'expired' for r in ats.resolved))
        self.assertTrue(all(rec[1] == 'expired' for rec in ledger.records))
        self.assertEqual({r[0] for r in ats.resolved}, {'r1', 'r2'})

    def test_apply_is_idempotent(self):
        art = self._artifact()
        ats = _FakeAts(existing=['r1', 'r2'])
        ledger = _FakeLedger()
        soc.apply_cleanup(art, NOW, ats_mod=ats, ledger_mod=ledger)
        again = soc.apply_cleanup(art, NOW, ats_mod=ats, ledger_mod=ledger)
        self.assertTrue(again['already_applied'])
        self.assertEqual(again['retired'], 0)
        # no second round of mutations
        self.assertEqual(len(ats.resolved), 2)
        self.assertEqual(len(ledger.records), 2)

    def test_missing_row_not_ledger_recorded(self):
        art = self._artifact()
        ats = _FakeAts(existing=['r1'])  # r2 absent from the store
        ledger = _FakeLedger()
        summary = soc.apply_cleanup(art, NOW, ats_mod=ats, ledger_mod=ledger)
        self.assertEqual(summary['retired'], 1)
        self.assertEqual(summary['ledger_recorded'], 1)
        self.assertEqual(summary['missing'], ['r2'])
        self.assertEqual([rec[0] for rec in ledger.records], ['tier4-ask-r1'])


if __name__ == '__main__':
    unittest.main()
