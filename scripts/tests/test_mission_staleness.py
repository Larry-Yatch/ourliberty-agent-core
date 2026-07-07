"""Tests for scripts/mission_staleness.py.

Coverage (Operator Feed Loop slice 3 — read-only staleness scorer):
- signal detection: orphan-autoproposed brief, degenerate name (numeric /
  name==task_id / blank), aging (>AGING_DAYS), duplicate (proposed collision +
  shipped-name match), already_terminal via an injected probe (MERGED/CLOSED
  fire + stamp terminal_state; OPEN/UNKNOWN do not).
- score_mission returns None when no signal fires; terminal outranks aging.
- reconcile: ranks candidates desc, writes the candidates file, and NEVER
  mutates missions.json (read-only contract).
- terminal probe is gated to real-looking cards (orphan/degenerate task ids are
  never probed) and capped (terminal_capped reported when real-looking >
  cap); a probe that raises is swallowed.
- fail safe: missing / malformed missions.json -> empty summary, no raise.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import mission_staleness as ms  # noqa: E402

_TODAY = date(2026, 7, 7)


def _mission(**kw) -> dict:
    base = {'id': kw.get('id', 'm'), 'name': '', 'phase': 'proposed',
            'brief': '', 'task_ids': [], 'created': '2026-07-06', 'archived': False}
    base.update(kw)
    return base


class SignalUnitTest(unittest.TestCase):
    def test_norm_deslugifies_name_to_task_id(self) -> None:
        self.assertEqual(ms._norm('P3 Mission Proposed Actions API'),
                         ms._norm('p3-mission-proposed-actions-api'))

    def test_orphan_autoproposed(self) -> None:
        self.assertTrue(ms._is_orphan_autoproposed(
            _mission(brief='Auto-proposed from orphan task `summary`. Last...')))
        self.assertFalse(ms._is_orphan_autoproposed(
            _mission(brief='A real deliberate proposal.')))

    def test_degenerate_name(self) -> None:
        self.assertTrue(ms._is_degenerate_name(
            _mission(name='20', task_ids=['20'])))                 # numeric
        self.assertTrue(ms._is_degenerate_name(_mission(name='x1')))  # tiny non-word
        self.assertTrue(ms._is_degenerate_name(_mission(name='')))  # blank
        # short ACRONYM proposals are real, not junk — must not be flagged
        self.assertFalse(ms._is_degenerate_name(_mission(name='KYC')))
        self.assertFalse(ms._is_degenerate_name(_mission(name='Ops')))
        # a single real word is NOT degenerate (orphan-brief catches junk like
        # this); and a legit multi-word name whose task id slugifies from it must
        # never be flagged.
        self.assertFalse(ms._is_degenerate_name(
            _mission(name='Summary', task_ids=['summary'])))
        self.assertFalse(ms._is_degenerate_name(
            _mission(name='Wire Health Notify', task_ids=['wire-health-notify'])))

    def test_age_days(self) -> None:
        self.assertEqual(ms._age_days(_mission(created='2026-06-07'), _TODAY), 30)
        self.assertIsNone(ms._age_days(_mission(created='garbage'), _TODAY))


class ScoreMissionTest(unittest.TestCase):
    def _score(self, mission, terminal_fn=None, name_counts=None, shipped=None):
        return ms.score_mission(
            mission, name_counts=name_counts or {}, shipped_names=shipped or set(),
            today=_TODAY, terminal_fn=terminal_fn)

    def test_no_signal_returns_none(self) -> None:
        # recent, real name, unique, no terminal probe -> nothing stale
        self.assertIsNone(self._score(
            _mission(name='Fresh Real Idea', task_ids=['fresh-real-idea'],
                     brief='A deliberate proposal.', created='2026-07-06')))

    def test_orphan_and_degenerate_stack(self) -> None:
        # '20' is both orphan-briefed AND a degenerate (numeric) name
        c = self._score(_mission(
            name='20', task_ids=['20'],
            brief='Auto-proposed from orphan task `20`.', created='2026-07-06'))
        self.assertIsNotNone(c)
        self.assertEqual(c['score'], ms._W_ORPHAN + ms._W_DEGENERATE)

    def test_aging_fires_only_past_threshold(self) -> None:
        recent = self._score(_mission(name='Real Idea One', task_ids=['rio'],
                                       brief='real', created='2026-07-01'))  # 6d
        self.assertIsNone(recent)
        old = self._score(_mission(name='Real Idea Two', task_ids=['rit'],
                                    brief='real', created='2026-06-01'))  # 36d
        self.assertIsNotNone(old)
        self.assertIn('aging', old['reasons'][0])

    def test_duplicate_proposed_and_shipped(self) -> None:
        dup = self._score(
            _mission(name='Dup Thing', task_ids=['dt'], brief='real',
                     created='2026-07-06'),
            name_counts={ms._norm('Dup Thing'): 2})
        self.assertIsNotNone(dup)
        self.assertTrue(any('duplicate' in r for r in dup['reasons']))
        shipped_match = self._score(
            _mission(name='Shipped Twin', task_ids=['st'], brief='real',
                     created='2026-07-06'),
            shipped={ms._norm('Shipped Twin')})
        self.assertTrue(any('shipped mission' in r for r in shipped_match['reasons']))

    def test_terminal_merged_fires_and_outranks_aging(self) -> None:
        merged = self._score(
            _mission(name='Real Merged Work', task_ids=['rmw'], brief='real',
                     created='2026-07-05'),  # 2d, no aging
            terminal_fn=lambda tid: 'MERGED')
        self.assertIsNotNone(merged)
        self.assertEqual(merged['terminal_state'], 'MERGED')
        self.assertGreaterEqual(merged['score'], ms._W_TERMINAL)
        # terminal (definitive) scores higher than a purely-aging card
        aging = self._score(_mission(name='Old Real', task_ids=['or1'],
                                     brief='real', created='2026-05-01'))  # ~67d
        self.assertGreater(merged['score'], aging['score'])

    def test_terminal_open_or_unknown_does_not_fire(self) -> None:
        for state in ('OPEN', 'UNKNOWN'):
            c = self._score(
                _mission(name='Real Live Work', task_ids=['rlw'], brief='real',
                         created='2026-07-06'),
                terminal_fn=lambda tid, s=state: s)
            self.assertIsNone(c, f'{state} should not flag stale')


class _IsolatedBoard(unittest.TestCase):
    """Write a fixture missions.json under a tmp OURLIBERTY_AGENTS_ROOT and reload
    the module so its paths point at the scratch board."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='mission-staleness-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        importlib.reload(ms)
        self._board = ms.MISSIONS_JSON
        self._board.parent.mkdir(parents=True, exist_ok=True)
        self._write_board(self._default_missions())

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(ms)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_board(self, missions: list) -> None:
        with open(self._board, 'w', encoding='utf-8') as f:
            json.dump({'schema_version': 1, 'missions': missions}, f)

    def _default_missions(self) -> list:
        return [
            # orphan+degenerate junk (deterministic, no probe)
            _mission(id='j1', name='Summary', task_ids=['summary'],
                     brief='Auto-proposed from orphan task `summary`.',
                     created='2026-06-12'),
            _mission(id='j2', name='20', task_ids=['20'],
                     brief='Auto-proposed from orphan task `20`.',
                     created='2026-06-12'),
            # real-looking, work already merged
            _mission(id='r1', name='Rebase PR 687', task_ids=['rebase-pr-687'],
                     brief='A real proposal.', created='2026-06-27'),
            # real-looking, aging, still live
            _mission(id='r2', name='Wire Health Notify',
                     task_ids=['wire-health-notify'], brief='A real proposal.',
                     created='2026-06-01'),
            # real-looking, fresh + open -> NOT a candidate
            _mission(id='r3', name='Fresh Live Idea', task_ids=['fresh-live-idea'],
                     brief='A real proposal.', created='2026-07-06'),
            # a shipped mission (context for duplicate + shipped-name)
            _mission(id='s1', name='Wire Health Notify', phase='shipped',
                     task_ids=['whn-shipped'], brief='shipped', created='2026-06-15'),
        ]

    def _probe(self, mapping: dict, calls: list):
        def fn(tid: str) -> str:
            calls.append(tid)
            return mapping.get(tid, 'UNKNOWN')
        return fn


class ReconcileTest(_IsolatedBoard):
    def test_ranks_and_writes_candidates_without_touching_board(self) -> None:
        before = self._board.read_bytes()
        calls: list = []
        probe = self._probe({'rebase-pr-687': 'MERGED'}, calls)
        summary = ms.reconcile(today=_TODAY, terminal_fn=probe)

        self.assertEqual(summary['proposed'], 5)  # s1 is shipped, excluded
        # candidates: j1, j2 (junk), r1 (merged), r2 (aging+dup-shipped). r3 fresh+open -> not.
        cand = json.loads(ms.CANDIDATES_FILE.read_text())['candidates']
        ids = [c['id'] for c in cand]
        self.assertIn('r1', ids)
        self.assertNotIn('r3', ids)
        # merged work ranks first (terminal is the heaviest signal)
        self.assertEqual(cand[0]['id'], 'r1')
        self.assertEqual(cand[0]['terminal_state'], 'MERGED')
        # READ-ONLY: missions.json is byte-identical after the pass
        self.assertEqual(self._board.read_bytes(), before)

    def test_terminal_probe_gated_to_real_looking_cards(self) -> None:
        calls: list = []
        probe = self._probe({}, calls)
        ms.reconcile(today=_TODAY, terminal_fn=probe)
        # junk task ids ('summary', '20') must never be probed; real ones are
        self.assertNotIn('summary', calls)
        self.assertNotIn('20', calls)
        self.assertIn('rebase-pr-687', calls)
        self.assertIn('wire-health-notify', calls)

    def test_terminal_cap_bounds_probes_and_reports(self) -> None:
        calls: list = []
        probe = self._probe({}, calls)
        summary = ms.reconcile(today=_TODAY, terminal_fn=probe, terminal_cap=1)
        self.assertEqual(summary['terminal_checks'], 1)   # only 1 of 3 real-looking
        self.assertTrue(summary['terminal_capped'])
        self.assertEqual(len(calls), 1)

    def test_probe_that_raises_is_swallowed(self) -> None:
        def boom(tid: str) -> str:
            raise RuntimeError('gh exploded')
        summary = ms.reconcile(today=_TODAY, terminal_fn=boom)
        # r1 no longer flagged terminal, but r2 still aging etc.; no crash
        self.assertGreaterEqual(summary['candidates'], 1)

    def test_missing_board_returns_empty_summary(self) -> None:
        self._board.unlink()
        summary = ms.reconcile(today=_TODAY, terminal_fn=lambda t: 'UNKNOWN')
        self.assertEqual(summary['proposed'], 0)
        self.assertEqual(summary['candidates'], 0)

    def test_malformed_board_returns_empty_summary(self) -> None:
        self._board.write_text('{not valid json')
        summary = ms.reconcile(today=_TODAY, terminal_fn=lambda t: 'UNKNOWN')
        self.assertEqual(summary['proposed'], 0)


if __name__ == '__main__':
    unittest.main()
