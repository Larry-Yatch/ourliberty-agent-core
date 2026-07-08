"""Tests for scripts/mission_rank.py (Operator Feed Loop slice 4 — the
portfolio rank brain).

Coverage:
- load_portfolio / resolve_project: repo→project join (bare + owner/repo forms),
  default-project catch-all, missing/empty config -> None.
- score_card: LLM-scored path (rank math = (benefit−cost)×weight×10 + benefit
  tiebreak), fallback path on LLM None/raise (neutral 5/5, scored:false,
  deterministic brief), partial-JSON handling, risk badge passthrough.
- rank(): excludes slice-3 stale candidates, sorts desc, counts unmapped when
  config has no default, enforces + reports the LLM cap, aborts without a
  portfolio config, never mutates missions.json, writes RANK_FILE atomically,
  never raises on missing/malformed inputs.

All LLM + risk calls are injected seams — no claude spawn, no trust_policy.
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
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import mission_rank as mr  # noqa: E402

_PORTFOLIO = {
    'projects': [
        {'key': 'rsdpm', 'name': 'RSDPM', 'repos': ['rsdpm-app'],
         'stage': 'active-build', 'weight': 0.7, 'north_star': 'ship it'},
        {'key': 'factory', 'name': 'Factory', 'repos': ['ourliberty-agent-core'],
         'stage': 'steady-maintenance', 'weight': 0.2, 'north_star': 'keep it running'},
        {'key': 'exploration', 'name': 'Exploration', 'repos': [],
         'stage': 'exploration', 'weight': 0.1, 'default': True,
         'north_star': 'find the next thing'},
    ],
}


def _mission(**kw) -> dict:
    base = {'id': kw.get('id', 'm1'), 'name': 'A Real Card', 'phase': 'proposed',
            'brief': 'a deliberate proposal', 'repo': 'ourliberty-agent-core',
            'archived': False}
    base.update(kw)
    return base


def _llm_stub(benefit=8, cost=3):
    def llm(prompt: str):
        return {'benefit': benefit, 'cost': cost, 'what': 'a thing',
                'why': 'it matters', 'suggest': 'build it'}
    return llm


_SAFE = lambda shaped: 'safe'  # noqa: E731


class ResolveProjectTest(unittest.TestCase):
    def test_bare_and_qualified_repo_match(self) -> None:
        pj = _PORTFOLIO['projects']
        self.assertEqual(mr.resolve_project('rsdpm-app', pj)['key'], 'rsdpm')
        self.assertEqual(
            mr.resolve_project('Larry-Yatch/ourliberty-agent-core', pj)['key'],
            'factory')

    def test_unmapped_and_none_fall_to_default(self) -> None:
        pj = _PORTFOLIO['projects']
        self.assertEqual(mr.resolve_project('mystery-repo', pj)['key'], 'exploration')
        self.assertEqual(mr.resolve_project(None, pj)['key'], 'exploration')

    def test_no_default_returns_none(self) -> None:
        pj = [p for p in _PORTFOLIO['projects'] if not p.get('default')]
        self.assertIsNone(mr.resolve_project('mystery-repo', pj))


class CoerceScoreTest(unittest.TestCase):
    def test_coercions(self) -> None:
        self.assertEqual(mr._coerce_score(7), 7)
        self.assertEqual(mr._coerce_score('7'), 7)
        self.assertEqual(mr._coerce_score(15), 10)   # clamp
        self.assertEqual(mr._coerce_score(-2), 0)    # clamp
        self.assertIsNone(mr._coerce_score('lots'))
        self.assertIsNone(mr._coerce_score(None))


class ScoreCardTest(unittest.TestCase):
    def _project(self, key='rsdpm'):
        return next(p for p in _PORTFOLIO['projects'] if p['key'] == key)

    def test_llm_scored_rank_math(self) -> None:
        e = mr.score_card(_mission(repo='rsdpm-app'), self._project('rsdpm'),
                          llm=_llm_stub(benefit=8, cost=3), risk_fn=_SAFE)
        self.assertTrue(e['scored'])
        # (8-3) * 0.7 * 10 = 35.0 (no epsilon — ties break at sort time)
        self.assertAlmostEqual(e['rank_score'], 35.0)
        self.assertEqual(e['risk'], 'safe')
        self.assertEqual(e['brief']['suggest'], 'build it')

    def test_weight_arbitrates_between_projects(self) -> None:
        # identical card quality: heavier project must rank higher
        rs = mr.score_card(_mission(repo='rsdpm-app'), self._project('rsdpm'),
                           llm=_llm_stub(8, 3), risk_fn=_SAFE)
        fa = mr.score_card(_mission(), self._project('factory'),
                           llm=_llm_stub(8, 3), risk_fn=_SAFE)
        self.assertGreater(rs['rank_score'], fa['rank_score'])

    def test_high_leverage_internal_beats_minor_product(self) -> None:
        # the doctrine: factory card with big net value CAN outrank a marginal
        # product card despite the weight gap
        fa = mr.score_card(_mission(), self._project('factory'),
                           llm=_llm_stub(9, 1), risk_fn=_SAFE)   # net 8 × .2 = 16
        rs = mr.score_card(_mission(repo='rsdpm-app'), self._project('rsdpm'),
                           llm=_llm_stub(4, 3), risk_fn=_SAFE)   # net 1 × .7 = 7
        self.assertGreater(fa['rank_score'], rs['rank_score'])

    def test_llm_none_falls_back_neutral_unscored(self) -> None:
        e = mr.score_card(_mission(), self._project('factory'),
                          llm=lambda p: None, risk_fn=_SAFE)
        self.assertFalse(e['scored'])
        self.assertEqual((e['benefit'], e['cost']), (5, 5))
        self.assertAlmostEqual(e['rank_score'], 0.0)  # neutral net-value
        self.assertIn('unavailable', e['brief']['suggest'])

    def test_raising_risk_fn_fails_toward_careful_not_crash(self) -> None:
        def boom_risk(shaped): raise RuntimeError('policy exploded')
        e = mr.score_card(_mission(), self._project('factory'),
                          llm=_llm_stub(), risk_fn=boom_risk)
        self.assertEqual(e['risk'], 'careful')  # B3: seam never-raises

    def test_default_risk_fn_uses_derive_risk_contract(self) -> None:
        # B1 regression: _default_risk_fn must consume missions_narrator.
        # derive_risk's TUPLE return (risk, careful) — not treat it as a dict.
        import missions_narrator as mn
        orig = mn.derive_risk
        mn.derive_risk = lambda shaped, policy=None: ('medium', False)
        try:
            fn = mr._default_risk_fn()
            self.assertEqual(fn({'title': 'x', 'note': '', 'origin': {}}),
                             'medium')  # passthrough, not swallowed to careful
        finally:
            mn.derive_risk = orig

    def test_llm_raising_falls_back(self) -> None:
        def boom(p): raise RuntimeError('tier down')
        e = mr.score_card(_mission(), self._project('factory'),
                          llm=boom, risk_fn=_SAFE)
        self.assertFalse(e['scored'])

    def test_partial_llm_json_keeps_scores_falls_back_brief(self) -> None:
        e = mr.score_card(
            _mission(), self._project('factory'),
            llm=lambda p: {'benefit': 6, 'cost': 2, 'what': 'x', 'why': ''},
            risk_fn=_SAFE)
        self.assertTrue(e['scored'])           # numbers were valid
        self.assertNotEqual(e['brief']['what'], 'x')  # brief fell back whole

    def test_risk_fn_failure_direction(self) -> None:
        e = mr.score_card(_mission(), self._project('factory'),
                          llm=None, risk_fn=lambda s: 'careful')
        self.assertEqual(e['risk'], 'careful')


class _IsolatedRank(unittest.TestCase):
    """Fixture board + staleness + portfolio under a tmp OURLIBERTY_AGENTS_ROOT."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='mission-rank-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        importlib.reload(mr)
        mr.MISSIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
        self._write(mr.MISSIONS_JSON, {'missions': [
            _mission(id='a', name='Product Feature', repo='rsdpm-app'),
            _mission(id='b', name='Factory Fix'),
            _mission(id='stale1', name='Old Junk'),
            _mission(id='shipped1', phase='shipped'),
        ]})
        mr.STALENESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._write(mr.STALENESS_FILE, {'candidates': [{'id': 'stale1'}]})
        self._pf = Path(self._tmpdir) / 'operator-portfolio.json'
        self._write(self._pf, _PORTFOLIO)

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(mr)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, path: Path, obj) -> None:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def _rank(self, **kw):
        kw.setdefault('portfolio_path', self._pf)
        kw.setdefault('llm', _llm_stub())
        kw.setdefault('risk_fn', _SAFE)
        return mr.rank(**kw)


class RankPassTest(_IsolatedRank):
    def test_excludes_stale_ranks_survivors_sorted(self) -> None:
        before = mr.MISSIONS_JSON.read_bytes()
        summary = self._rank()
        self.assertEqual(summary['proposed'], 3)        # shipped excluded
        self.assertEqual(summary['stale_excluded'], 1)  # stale1
        self.assertEqual(summary['ranked'], 2)
        out = json.loads(mr.RANK_FILE.read_text())
        ids = [e['id'] for e in out['ranked']]
        self.assertEqual(ids, ['a', 'b'])  # rsdpm weight 0.7 > factory 0.2
        self.assertEqual(out['mode'], 'shadow')
        self.assertEqual(mr.MISSIONS_JSON.read_bytes(), before)  # READ-ONLY

    def test_missing_staleness_file_excludes_nothing(self) -> None:
        mr.STALENESS_FILE.unlink()
        summary = self._rank()
        self.assertEqual(summary['stale_excluded'], 0)
        self.assertEqual(summary['ranked'], 3)

    def test_no_portfolio_config_aborts(self) -> None:
        summary = self._rank(portfolio_path=Path(self._tmpdir) / 'missing.json')
        self.assertEqual(summary['ranked'], 0)
        self.assertFalse(mr.RANK_FILE.exists())

    def test_unmapped_counted_when_no_default(self) -> None:
        no_default = {'projects': [
            {'key': 'rsdpm', 'repos': ['rsdpm-app'], 'weight': 0.7,
             'north_star': 'ship'},
        ]}
        self._write(self._pf, no_default)
        summary = self._rank()
        self.assertEqual(summary['ranked'], 1)     # only the rsdpm card
        self.assertEqual(summary['unmapped'], 1)   # the factory card

    def test_llm_cap_enforced_and_reported(self) -> None:
        calls = []

        def counting_llm(prompt):
            calls.append(1)
            return _llm_stub()(prompt)

        summary = self._rank(llm=counting_llm, llm_cap=1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(summary['llm_capped'])
        self.assertEqual(summary['llm_scored'], 1)
        self.assertEqual(summary['ranked'], 2)  # capped card still ranked (fallback)

    def test_malformed_board_never_raises(self) -> None:
        mr.MISSIONS_JSON.write_text('{broken')
        summary = self._rank()
        self.assertEqual(summary['ranked'], 0)

    def test_malformed_staleness_container_never_raises(self) -> None:
        # B2 regression: candidates=null (a partial slice-3 write) must not crash
        self._write(mr.STALENESS_FILE, {'candidates': None})
        summary = self._rank()
        self.assertEqual(summary['stale_excluded'], 0)
        self.assertEqual(summary['ranked'], 3)  # nothing excluded, pass survives

    def test_llm_capped_false_when_under_cap_or_no_llm(self) -> None:
        # exactly at cap → not capped; llm never attempted → not capped
        summary = self._rank(llm_cap=2)          # 2 survivors mapped, cap 2
        self.assertFalse(summary['llm_capped'])
        summary2 = self._rank(llm=lambda p: None, llm_cap=0)
        # cap 0: every mapped card WANTED a call and was refused → capped
        self.assertTrue(summary2['llm_capped'])


if __name__ == '__main__':
    unittest.main()
