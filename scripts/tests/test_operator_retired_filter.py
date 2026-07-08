"""Tests for the retired-proposal exclusion in the operator pipeline
(mission_staleness + mission_rank), added 2026-07-08.

The bug: both modules filtered only `archived`, so the 176 rows
heal_orphan_autoregister had already retired-with-audit (additive
`acknowledged` flag) were re-scanned and re-counted every pass — the
Approvals queue said "247 proposed" when the live shelf was ~20.

Coverage: proposed_is_retired semantics (the single shared definition);
staleness excludes retired rows from scoring and reports retired_excluded;
rank does the same and never LLM-scores a retired row.
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

import mission_rank as mr  # noqa: E402
import mission_staleness as ms  # noqa: E402

TODAY = date(2026, 7, 8)


def _mission(mid: str, **over) -> dict:
    m = {'id': mid, 'name': f'Mission {mid}', 'phase': 'proposed',
         'brief': 'a deliberate proposal', 'task_ids': [],
         'created': '2026-07-01', 'archived': False}
    m.update(over)
    return m


class ProposedIsRetiredTest(unittest.TestCase):
    def test_semantics(self):
        self.assertFalse(ms.proposed_is_retired(_mission('live')))
        self.assertTrue(ms.proposed_is_retired(_mission('a', acknowledged=True)))
        self.assertTrue(ms.proposed_is_retired(
            _mission('b', retired_at='2026-07-01T00:00:00Z')))
        self.assertTrue(ms.proposed_is_retired(_mission('c', phase='retired')))
        # `is True` is deliberate: a truthy non-bool must NOT read as retired
        # (guards against a future writer stamping 1 / 'true' and silently
        # hiding a live card).
        self.assertFalse(ms.proposed_is_retired(_mission('d', acknowledged='true')))
        self.assertFalse(ms.proposed_is_retired(_mission('e2', acknowledged=1)))
        self.assertFalse(ms.proposed_is_retired(_mission('e', acknowledged=False)))


class SandboxedTest(unittest.TestCase):
    module = None  # set by subclass

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(ms)
        importlib.reload(mr)
        ms.MISSIONS_JSON.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(ms)
        importlib.reload(mr)

    def _write_board(self, missions: list) -> None:
        ms.MISSIONS_JSON.write_text(json.dumps({'missions': missions}))


class StalenessExcludesRetiredTest(SandboxedTest):
    def test_retired_rows_not_scored_and_counted_separately(self):
        self._write_board([
            _mission('live-1'),
            _mission('retired-1', acknowledged=True,
                     brief='Auto-proposed from orphan task x'),
            _mission('retired-2', retired_at='2026-07-01T00:00:00Z'),
        ])
        summary = ms.reconcile(today=TODAY, terminal_fn=lambda tid: 'UNKNOWN')
        self.assertEqual(summary['proposed'], 1)
        self.assertEqual(summary['retired_excluded'], 2)
        out = json.loads(ms.CANDIDATES_FILE.read_text())
        ids = {c['id'] for c in out.get('candidates', [])}
        self.assertNotIn('retired-1', ids)  # junk signals, but already handled
        self.assertNotIn('retired-2', ids)


class RankExcludesRetiredTest(SandboxedTest):
    def _portfolio(self) -> Path:
        p = self.tmp / 'portfolio.json'
        p.write_text(json.dumps({'projects': [
            {'key': 'factory', 'name': 'factory', 'weight': 1.0, 'repos': [],
             'stage': 'steady', 'north_star': 'less toil', 'default': True},
        ]}))
        return p

    def test_retired_rows_never_reach_scoring(self):
        self._write_board([
            _mission('live-1'),
            _mission('retired-1', acknowledged=True),
        ])
        scored_names: list = []

        def fake_llm(prompt: str):
            scored_names.append(prompt)
            return {'benefit': 5, 'cost': 5,
                    'brief': {'what': 'w', 'why': 'y', 'suggest': 's'}}

        summary = mr.rank(portfolio_path=self._portfolio(), llm=fake_llm,
                          risk_fn=lambda shaped: 'safe')
        self.assertEqual(summary['proposed'], 1)
        self.assertEqual(summary['retired_excluded'], 1)
        self.assertEqual(summary['ranked'], 1)
        joined = ' '.join(scored_names)
        self.assertNotIn('retired-1', joined)


class DigestExcludesRetiredTest(unittest.TestCase):
    """The parked-aging monthly digest's actionable_count = proposed − candidates.
    Since candidates already drops retired rows, proposed must too, or the
    Larry-facing count inflates."""

    def test_proposed_missions_excludes_retired(self):
        import parked_aging_digest_generator as pd
        importlib.reload(pd)
        data = {'missions': [
            _mission('live-1'),
            _mission('a', acknowledged=True),
            _mission('b', retired_at='2026-07-01T00:00:00Z'),
            _mission('c', archived=True),
        ]}
        live = pd._proposed_missions(data)
        self.assertEqual([m['id'] for m in live], ['live-1'])


if __name__ == '__main__':
    unittest.main()
