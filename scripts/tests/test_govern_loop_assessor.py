"""Tests for scripts/govern_loop_assessor.py.

Coverage (Operator Feed Loop slice 3 — read-only, shadow-first payoff scorer):
- a fixture ledger (decision + build_outcome rows) maps to an EXPECTED ranking:
  buckets ordered best-payoff to worst, with counts + payoff scores + reasons.
- latest-row-wins supersede: an earlier `closed_unmerged` then a later `merged`
  for the SAME decision_key scores as merged (reopen+merge beats abandonment),
  resolved through the ledger's own read API.
- empty-key ('') decision rows are excluded from payoff scoring but do NOT crash
  the pass (counted in totals as empty_key_excluded).
- a missing / malformed ledger yields an EMPTY assessment (empty ranking), not a
  raise (mirrors the ledger module's never-raise discipline).
- filesystem contract: the assessor writes ONLY its own state file — the ledger
  file and a sentinel missions.json are byte-for-byte untouched.
- --once / the pure builder surface the ranking as JSON.

Isolation mirrors test_decision_outcome_ledger / test_mission_staleness: point
OURLIBERTY_AGENTS_ROOT at a tmp dir and reload BOTH the ledger module and the
assessor so their path constants pick up the override.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import decision_outcome_ledger as dol  # noqa: E402
import govern_loop_assessor as gla  # noqa: E402


def _decision(key, outcome='approved', actor='dashboard', notes=None) -> dict:
    row = {'ts': '2026-07-07T00:00:00+00:00', 'kind': 'decision',
           'decision_key': key, 'outcome': outcome, 'actor': actor, 'cleared': 1}
    if notes is not None:
        row['notes'] = notes
    return row


def _build(key, build_outcome, ts='2026-07-07T01:00:00+00:00') -> dict:
    return {'ts': ts, 'kind': 'build_outcome', 'decision_key': key,
            'build_outcome': build_outcome, 'actor': 'reconcile'}


class _IsolatedAssessor(unittest.TestCase):
    """tmp OURLIBERTY_AGENTS_ROOT + reload the ledger AND the assessor so
    LEDGER_FILE / ASSESSMENT_FILE / LOG_FILE all resolve under the tmp tree."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='govern-assessor-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        importlib.reload(dol)
        importlib.reload(gla)
        self.root = Path(self._tmpdir)
        self.state = self.root / 'state'
        self.state.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(dol)
        importlib.reload(gla)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_ledger(self, rows) -> Path:
        path = dol.LEDGER_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        return path


class RankingTest(_IsolatedAssessor):
    def test_fixture_ledger_maps_to_expected_ranking(self) -> None:
        # Two buckets by actor: a dashboard/human bucket that ships clean, and a
        # telegram/human bucket that fizzles/regresses.
        rows = [
            _decision('pr-alpha-1', actor='dashboard'),
            _build('pr-alpha-1', 'merged'),
            _decision('pr-alpha-2', actor='dashboard'),
            _build('pr-alpha-2', 'merged'),
            _decision('pr-beta-3', actor='telegram'),
            _build('pr-beta-3', 'closed_unmerged'),
            _decision('pr-beta-4', actor='telegram'),
            _build('pr-beta-4', 'merged_regressed'),
        ]
        self._write_ledger(rows)
        payload = gla.assess()

        self.assertEqual(payload['totals']['scored_decisions'], 4)
        self.assertEqual(payload['totals']['buckets'], 2)
        ranking = payload['ranking']
        self.assertEqual(len(ranking), 2)

        # Best bucket first: dashboard/human, both merged -> mean payoff +1.0.
        best = ranking[0]
        self.assertEqual((best['actor'], best['approval_mode']),
                         ('dashboard', 'human'))
        self.assertEqual(best['payoff_score'], 2.0)
        self.assertEqual(best['payoff_per_decision'], 1.0)
        self.assertEqual(best['outcome_counts'], {'merged': 2})
        self.assertEqual(best['repos'], {'alpha': 2})

        # Worst bucket: telegram/human, one abandoned (-0.5) + one regressed
        # (-1.0) -> total -1.5, mean -0.75.
        worst = ranking[1]
        self.assertEqual((worst['actor'], worst['approval_mode']),
                         ('telegram', 'human'))
        self.assertEqual(worst['payoff_score'], -1.5)
        self.assertEqual(worst['payoff_per_decision'], -0.75)
        self.assertEqual(worst['outcome_counts'],
                         {'closed_unmerged': 1, 'merged_regressed': 1})
        # Each bucket carries a plain-language reason.
        self.assertIn('reliably ship clean', best['reason'])
        self.assertIn('NOT paying off', worst['reason'])

    def test_auto_vs_human_split_from_notes(self) -> None:
        rows = [
            _decision('pr-alpha-1', actor='dashboard',
                      notes='auto-approved by trust-policy carve-out'),
            _build('pr-alpha-1', 'merged'),
            _decision('pr-alpha-2', actor='dashboard', notes='Larry clicked'),
            _build('pr-alpha-2', 'merged'),
        ]
        self._write_ledger(rows)
        payload = gla.assess()
        modes = {(b['actor'], b['approval_mode']): b for b in payload['ranking']}
        self.assertIn(('dashboard', 'auto'), modes)
        self.assertIn(('dashboard', 'human'), modes)
        self.assertEqual(modes[('dashboard', 'auto')]['scored'], 1)
        self.assertEqual(modes[('dashboard', 'human')]['scored'], 1)


class LatestRowWinsTest(_IsolatedAssessor):
    def test_later_merged_supersedes_earlier_closed_unmerged(self) -> None:
        # Same key: abandoned first, then reopened+merged. Latest row wins, so it
        # scores as merged (+1.0), NOT closed_unmerged (-0.5).
        rows = [
            _decision('pr-alpha-9', actor='dashboard'),
            _build('pr-alpha-9', 'closed_unmerged', ts='2026-07-07T01:00:00+00:00'),
            _build('pr-alpha-9', 'merged', ts='2026-07-09T01:00:00+00:00'),
        ]
        self._write_ledger(rows)
        payload = gla.assess()
        self.assertEqual(len(payload['ranking']), 1)
        b = payload['ranking'][0]
        self.assertEqual(b['payoff_score'], 1.0)
        self.assertEqual(b['outcome_counts'], {'merged': 1})
        self.assertNotIn('closed_unmerged', b['outcome_counts'])


class EmptyKeyTest(_IsolatedAssessor):
    def test_empty_key_counted_but_not_scored(self) -> None:
        rows = [
            _decision('', actor='heal_stale_approvals'),   # tally-only, no join
            _decision('pr-alpha-1', actor='dashboard'),
            _build('pr-alpha-1', 'merged'),
        ]
        self._write_ledger(rows)
        payload = gla.assess()  # must not raise
        self.assertEqual(payload['totals']['empty_key_excluded'], 1)
        self.assertEqual(payload['totals']['scored_decisions'], 1)
        # The empty-key row contributes no bucket.
        self.assertEqual(len(payload['ranking']), 1)
        self.assertEqual(payload['ranking'][0]['actor'], 'dashboard')


class FailSafeTest(_IsolatedAssessor):
    def test_missing_ledger_yields_empty_assessment(self) -> None:
        # No ledger file written at all.
        self.assertFalse(dol.LEDGER_FILE.exists())
        payload = gla.assess()
        self.assertEqual(payload['ranking'], [])
        self.assertEqual(payload['totals']['scored_decisions'], 0)
        self.assertEqual(payload['totals']['decision_rows'], 0)
        # Still writes its own (empty) state file.
        self.assertTrue(gla.ASSESSMENT_FILE.exists())

    def test_malformed_ledger_yields_empty_assessment(self) -> None:
        dol.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(dol.LEDGER_FILE, 'w', encoding='utf-8') as f:
            f.write('{not json at all\n')
            f.write('also not json }{\n')
        payload = gla.assess()  # must not raise
        self.assertEqual(payload['ranking'], [])
        self.assertEqual(payload['totals']['scored_decisions'], 0)


class ReadOnlyContractTest(_IsolatedAssessor):
    def test_writes_only_its_own_state_file(self) -> None:
        rows = [
            _decision('pr-alpha-1', actor='dashboard'),
            _build('pr-alpha-1', 'merged'),
        ]
        ledger = self._write_ledger(rows)
        ledger_bytes_before = ledger.read_bytes()

        # A sentinel missions.json the assessor must never touch.
        missions = self.state / 'missions.json'
        missions_bytes = b'{"missions": ["SENTINEL"]}\n'
        missions.write_bytes(missions_bytes)

        gla.assess()

        # Ledger byte-for-byte unchanged (read-only over the ledger).
        self.assertEqual(ledger.read_bytes(), ledger_bytes_before)
        # missions.json untouched.
        self.assertEqual(missions.read_bytes(), missions_bytes)
        # Its own state file was written.
        self.assertTrue(gla.ASSESSMENT_FILE.exists())
        # No leftover tmp file.
        self.assertFalse(
            gla.ASSESSMENT_FILE.with_suffix('.json.tmp').exists())


class PureBuilderTest(unittest.TestCase):
    """build_assessment is a pure function (no IO) — exercised without a tmp
    ledger so the ranking math is covered independently of the read path."""

    def test_ranks_and_scores_without_io(self) -> None:
        records = [
            {'kind': 'decision', 'decision_key': 'pr-alpha-1',
             'actor': 'dashboard'},
            {'kind': 'decision', 'decision_key': 'task-xyz',
             'actor': 'telegram'},
        ]
        latest_build = {'pr-alpha-1': 'merged', 'task-xyz': 'closed_unmerged'}
        payload = gla.build_assessment(records, latest_build)
        self.assertEqual(payload['ranking'][0]['payoff_per_decision'], 1.0)
        self.assertEqual(payload['ranking'][-1]['payoff_per_decision'], -0.5)
        # Bare task_id key -> repo underivable.
        tele = next(b for b in payload['ranking'] if b['actor'] == 'telegram')
        self.assertEqual(tele['repos'], {gla._REPO_UNDERIVABLE: 1})

    def test_pre_kind_row_read_as_decision(self) -> None:
        # A row with no `kind` is a decision (ledger forward-compat contract).
        records = [{'decision_key': 'pr-alpha-1', 'actor': 'dashboard'}]
        latest_build = {'pr-alpha-1': 'merged'}
        payload = gla.build_assessment(records, latest_build)
        self.assertEqual(payload['totals']['decision_rows'], 1)
        self.assertEqual(payload['ranking'][0]['actor'], 'dashboard')


if __name__ == '__main__':
    unittest.main()
