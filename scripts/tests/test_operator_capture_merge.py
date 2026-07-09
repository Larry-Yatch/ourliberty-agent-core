"""Tests for the parked-capture merge into the operator rank pool (slice 8).

Covers the functionally-load-bearing bits an antagonistic reviewer would poke:
eligibility (parked-only, spawned/promoted excluded, snooze is TIME-based not
presence-based), normalization (title/origin/briefing → card shape, kind +
source), briefing REUSE (authored what/why/suggest wins over the LLM), dedup
vs proposed ids, and the merged rank pass counting + kind stamping.
"""

from __future__ import annotations

try:
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import mission_rank as mr  # noqa: E402

NOW = datetime(2026, 7, 8, 18, 0, 0, tzinfo=timezone.utc)
BRIEF = {'what': 'w', 'why': 'y', 'suggest': 's'}


def _cap(cid: str, **over) -> dict:
    c = {'id': cid, 'title': f'Idea {cid}', 'state': 'parked',
         'origin': {'source': 'desktop-chat', 'repo': 'ourliberty-agent-core'},
         'briefing': dict(BRIEF)}
    c.update(over)
    return c


class EligibilityTest(unittest.TestCase):
    def test_only_parked_uncommitted_unsnoozed(self):
        caps = [
            _cap('keep'),
            _cap('active', state='active'),          # not parked
            _cap('spawned', spawned={'phase': 'x'}),  # already work
            _cap('promoted', promoted_to='proj-1'),   # already work
            _cap('snoozed', snoozed_until=(NOW + timedelta(days=3)).isoformat()),
            'garbage',
        ]
        got = mr.load_parked_captures(
            NOW, path=self._write(caps))
        self.assertEqual([c['id'] for c in got], ['keep'])

    def test_expired_snooze_resurfaces(self):
        caps = [_cap('past', snoozed_until=(NOW - timedelta(days=1)).isoformat())]
        self.assertEqual(len(mr.load_parked_captures(NOW, path=self._write(caps))), 1)

    def test_unparseable_snooze_surfaces_not_hides(self):
        caps = [_cap('bad', snoozed_until='not-a-date')]
        self.assertEqual(len(mr.load_parked_captures(NOW, path=self._write(caps))), 1)

    def test_missing_or_malformed_file_is_empty(self):
        self.assertEqual(mr.load_parked_captures(NOW, path=Path('/no/such')), [])

    def _write(self, caps) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / 'captures.json'
        p.write_text(json.dumps({'captures': caps}))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return p


class NormalizeTest(unittest.TestCase):
    def test_capture_to_card_shape(self):
        card = mr.capture_to_card(_cap('c1'))
        self.assertEqual(card['id'], 'c1')
        self.assertEqual(card['name'], 'Idea c1')
        self.assertEqual(card['repo'], 'ourliberty-agent-core')
        self.assertEqual(card['kind'], 'capture')
        self.assertEqual(card['prebrief'], BRIEF)
        self.assertIn('w', card['brief'])  # briefing flattened for prompt context

    def test_source_of_a_capture_card(self):
        self.assertEqual(mr.derive_source(mr.capture_to_card(_cap('c1'))), 'you')
        agent = _cap('c2', origin={'source': 'agent', 'repo': None})
        self.assertEqual(mr.derive_source(mr.capture_to_card(agent)), 'beacon')

    def test_invalid_briefing_yields_no_prebrief(self):
        # A partial briefing can't be REUSED as the card brief (prebrief=None),
        # but its present text still seeds the LLM prompt context.
        card = mr.capture_to_card(_cap('c3', briefing={'what': 'only'}))
        self.assertIsNone(card['prebrief'])
        self.assertEqual(card['brief'], 'only')

    def test_no_briefing_at_all_falls_back_to_title(self):
        card = mr.capture_to_card(_cap('c4', briefing=None))
        self.assertIsNone(card['prebrief'])
        self.assertEqual(card['brief'], card['name'])


class ScoreReusesBriefingTest(unittest.TestCase):
    def _project(self):
        return {'key': 'factory', 'name': 'Factory', 'stage': 'steady',
                'weight': 0.5, 'north_star': 'less toil'}

    def test_authored_briefing_wins_over_llm(self):
        # LLM returns its OWN brief; the capture's authored one must win.
        def llm(_p):
            return {'benefit': 8, 'cost': 2, 'what': 'LLM-what',
                    'why': 'LLM-why', 'suggest': 'LLM-suggest'}
        card = mr.capture_to_card(_cap('c1'))
        entry = mr.score_card(card, self._project(), llm=llm,
                              risk_fn=lambda s: 'safe')
        self.assertEqual(entry['brief'], BRIEF)       # authored wins
        self.assertEqual(entry['benefit'], 8)          # but LLM score kept
        self.assertEqual(entry['kind'], 'capture')

    def test_mission_kind_default(self):
        entry = mr.score_card({'id': 'm', 'name': 'M', 'repo': None,
                               'proposed_by': 'closeout'}, self._project(),
                              llm=None, risk_fn=lambda s: 'safe')
        self.assertEqual(entry['kind'], 'mission')


class MergedRankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(mr)
        (self.tmp / 'agents' / 'beacon' / 'workspace').mkdir(parents=True)
        (self.tmp / 'state').mkdir(parents=True)

    def tearDown(self):
        if self._env is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(mr)

    def _portfolio(self) -> Path:
        p = self.tmp / 'portfolio.json'
        p.write_text(json.dumps({'projects': [
            {'key': 'factory', 'name': 'Factory', 'stage': 'steady',
             'weight': 0.5, 'north_star': 'less toil', 'default': True}]}))
        return p

    def _board(self, missions, captures):
        (self.tmp / 'agents' / 'beacon' / 'workspace' / 'missions.json'
         ).write_text(json.dumps({'missions': missions}))
        (self.tmp / 'agents' / 'beacon' / 'workspace' / 'captures.json'
         ).write_text(json.dumps({'captures': captures}))

    def _run(self):
        return mr.rank(portfolio_path=self._portfolio(),
                      llm=lambda p: {'benefit': 6, 'cost': 3, 'what': 'a',
                                     'why': 'b', 'suggest': 'c'},
                      risk_fn=lambda s: 'safe')

    def _ranked_by_id(self):
        return {e['id']: e
                for e in json.loads(
                    (self.tmp / 'state' / 'mission-rank.json').read_text())['ranked']}

    def test_exact_id_twin_dedup_keeps_capture(self):
        self._board(
            missions=[{'id': 'cap-x', 'name': 'Shared', 'phase': 'proposed',
                       'repo': 'ourliberty-agent-core', 'proposed_by': 'beacon'}],
            captures=[_cap('cap-x'), _cap('cap-unique')])
        out = self._run()
        self.assertEqual(out['captures'], 2)
        self.assertEqual(out['mission_twins_deduped'], 1)  # the exact twin dropped
        by_id = self._ranked_by_id()
        self.assertEqual(by_id['cap-x']['kind'], 'capture')   # capture wins
        self.assertEqual(by_id['cap-unique']['kind'], 'capture')

    def test_PREFIXED_id_twin_deduped(self):
        # The blocker the adversarial review caught: heal_orphan_autoregister
        # mirrors a capture into a proposed mission under proposed-<kind>-<capId>
        # with task_id <kind>-<capId>. Exact-id matching missed it; the suffix
        # match must drop the machine twin and keep the authored capture.
        self._board(
            missions=[{
                'id': 'proposed-card-message-cap-build-58c8',
                'task_ids': ['card-message-cap-build-58c8'],
                'name': 'Auto twin', 'phase': 'proposed',
                'repo': 'ourliberty-agent-core',
                'proposed_by': 'heal_orphan_autoregister'}],
            captures=[_cap('cap-build-58c8')])
        out = self._run()
        self.assertEqual(out['mission_twins_deduped'], 1)
        self.assertEqual(out['ranked'], 1)  # one item, not two
        by_id = self._ranked_by_id()
        self.assertIn('cap-build-58c8', by_id)                # the capture
        self.assertEqual(by_id['cap-build-58c8']['kind'], 'capture')
        self.assertNotIn('proposed-card-message-cap-build-58c8', by_id)  # twin gone

    def test_partial_token_is_not_a_false_dedup(self):
        # A mission that merely CONTAINS the capture id as a non-boundary
        # substring must NOT be deduped (both survive).
        self._board(
            missions=[{'id': 'proposed-capxthing', 'name': 'Unrelated',
                       'phase': 'proposed', 'repo': 'ourliberty-agent-core',
                       'proposed_by': 'beacon'}],
            captures=[_cap('capx')])
        out = self._run()
        self.assertEqual(out['mission_twins_deduped'], 0)
        self.assertEqual(out['ranked'], 2)

    def test_short_capture_id_only_dedups_on_exact_match(self):
        # A short/generic capture id must NOT fuzzy-suffix-match an unrelated
        # mission (the guard); it still dedups its OWN exact-id twin.
        self._board(
            missions=[
                {'id': 'proposed-prefix-fix', 'name': 'Unrelated',
                 'phase': 'proposed', 'repo': 'ourliberty-agent-core',
                 'proposed_by': 'beacon'},
                {'id': 'fix', 'name': 'Exact', 'phase': 'proposed',
                 'repo': 'ourliberty-agent-core', 'proposed_by': 'beacon'}],
            captures=[_cap('fix')])
        out = self._run()
        # only the EXACT-id 'fix' mission is deduped; the unrelated
        # 'proposed-prefix-fix' is NOT fuzzy-matched by the short id 'fix'.
        self.assertEqual(out['mission_twins_deduped'], 1)
        by_id = self._ranked_by_id()
        self.assertIn('proposed-prefix-fix', by_id)         # unrelated survives
        self.assertEqual(by_id['proposed-prefix-fix']['kind'], 'mission')
        self.assertEqual(by_id['fix']['kind'], 'capture')   # capture won the id
        self.assertEqual(out['ranked'], 2)  # capture 'fix' + unrelated mission

    def test_non_list_task_ids_never_crashes(self):
        self._board(
            missions=[{'id': 'm', 'task_ids': 'not-a-list', 'name': 'M',
                       'phase': 'proposed', 'repo': 'ourliberty-agent-core',
                       'proposed_by': 'beacon'}],
            captures=[_cap('cap-something-9999')])
        out = self._run()  # must not raise
        self.assertEqual(out['ranked'], 2)

    def test_idless_capture_dropped(self):
        self._board(missions=[], captures=[_cap('good'), _cap(None)])
        out = self._run()
        self.assertEqual(out['captures'], 1)  # the null-id capture dropped

    def test_captures_scored_before_missions_under_cap(self):
        # Cap of 1: the single LLM call must go to the CAPTURE (higher trust);
        # the mission degrades to fallback.
        self._board(
            missions=[{'id': 'm', 'name': 'Mission', 'phase': 'proposed',
                       'repo': 'ourliberty-agent-core', 'proposed_by': 'beacon'}],
            captures=[_cap('cap-a')])
        calls = []
        mr.rank(portfolio_path=self._portfolio(),
                llm=lambda p: (calls.append(p) or {'benefit': 7, 'cost': 2,
                               'what': 'a', 'why': 'b', 'suggest': 'c'}),
                risk_fn=lambda s: 'safe', llm_cap=1)
        by_id = self._ranked_by_id()
        self.assertTrue(by_id['cap-a']['scored'])   # capture got the LLM
        self.assertFalse(by_id['m']['scored'])       # mission fell to fallback


if __name__ == '__main__':
    unittest.main()
