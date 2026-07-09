#!/usr/bin/env python3
"""Tests for GET /api/approvals/operator-queue (Operator Feed Loop slice 5).

The endpoint serves state/mission-rank.json (the rank brain's shadow output) to
the Approvals tab. Contract under test:
  - token-gated (401/403 without the dashboard token)
  - absent / malformed / non-dict / ranked-not-a-list file → available=False +
    empty ranked, HTTP 200 (never a 500)
  - a valid file round-trips: order preserved, scored flag + risk + brief intact
  - a single bad row (non-dict, missing name, wrong-typed fields) is skipped or
    degraded to defaults — it never breaks the rest of the queue

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_operator_queue
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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

TOKEN = 'test-token-value'
# Plain assignment, not setdefault: a real token already in the process env
# would otherwise make every authed request below send the WRONG token (401s).
os.environ['DASHBOARD_API_TOKEN'] = TOKEN

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}
URL = '/api/approvals/operator-queue'


def _entry(i: int, **kw) -> dict:
    base = {'id': f'm{i}', 'name': f'Card {i}', 'repo': 'ourliberty-agent-core',
            'project': 'factory', 'stage': 'steady-maintenance', 'weight': 0.2,
            'benefit': 8, 'cost': 3, 'rank_score': 10.0 - i, 'scored': True,
            'risk': 'safe', 'brief': {'what': 'w', 'why': 'y', 'suggest': 's'}}
    base.update(kw)
    return base


class _TmpAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT at a tmpdir for the rank-file read."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='operator-queue-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        self.client = TestClient(da.app)
        self.rank_file = Path(self._tmpdir) / 'state' / 'mission-rank.json'
        self.rank_file.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, obj) -> None:
        self.rank_file.write_text(
            obj if isinstance(obj, str) else json.dumps(obj), encoding='utf-8')


class AuthTest(_TmpAgentsRoot):
    def test_requires_token(self) -> None:
        r = self.client.get(URL)
        self.assertIn(r.status_code, (401, 403))


class EmptyStateTest(_TmpAgentsRoot):
    def test_absent_file_is_unavailable_not_500(self) -> None:
        r = self.client.get(URL, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body['available'])
        self.assertEqual(body['ranked'], [])
        self.assertEqual(body['mode'], 'shadow')

    def test_malformed_json_is_unavailable(self) -> None:
        self._write('{broken')
        r = self.client.get(URL, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['available'])

    def test_non_dict_and_bad_ranked_container(self) -> None:
        for payload in ('[1,2,3]', json.dumps({'ranked': None}),
                        json.dumps({'ranked': 'nope'})):
            self._write(payload)
            r = self.client.get(URL, headers=AUTH)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()['available'])


class RoundTripTest(_TmpAgentsRoot):
    def test_valid_file_round_trips_ordered(self) -> None:
        self._write({
            'generated_at': '2026-07-07T20:00:00+00:00', 'mode': 'shadow',
            'summary': {'ranked': 2},
            'ranked': [_entry(1), _entry(2, scored=False, risk='careful')],
        })
        r = self.client.get(URL, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['available'])
        self.assertEqual(body['generated_at'], '2026-07-07T20:00:00+00:00')
        self.assertEqual([e['id'] for e in body['ranked']], ['m1', 'm2'])
        self.assertTrue(body['ranked'][0]['scored'])
        self.assertFalse(body['ranked'][1]['scored'])   # fallback badge intact
        self.assertEqual(body['ranked'][1]['risk'], 'careful')
        self.assertEqual(body['ranked'][0]['brief']['suggest'], 's')

    def test_source_passes_through_and_defaults_unknown(self) -> None:
        # The source-badge contract: a stamped source survives the
        # endpoint verbatim; a row missing it degrades to 'unknown'
        # (never dropped, never null) so the badge always has a value.
        e = _entry(1); e['source'] = 'you'
        no_src = _entry(2)  # legacy row, no source field
        no_src.pop('source', None)
        self._write({'generated_at': None, 'mode': 'shadow',
                     'summary': {}, 'ranked': [e, no_src]})
        body = self.client.get(URL, headers=AUTH).json()
        self.assertEqual(body['ranked'][0]['source'], 'you')
        self.assertEqual(body['ranked'][1]['source'], 'unknown')

    def test_bad_rows_skipped_good_rows_survive(self) -> None:
        self._write({'ranked': [
            'not-a-dict',
            {'benefit': 9},                      # no name → skipped
            _entry(3),
            _entry(4, weight='heavy', benefit='big', risk=7),  # degrade fields
        ]})
        r = self.client.get(URL, headers=AUTH)
        body = r.json()
        self.assertTrue(body['available'])
        ids = [e['id'] for e in body['ranked']]
        self.assertEqual(ids, ['m3', 'm4'])
        degraded = body['ranked'][1]
        self.assertEqual(degraded['weight'], 0.0)     # 'heavy' → default
        self.assertEqual(degraded['benefit'], 5)      # 'big' → default
        self.assertEqual(degraded['risk'], 'careful')  # 7 → fail toward caution


class Never500Test(_TmpAgentsRoot):
    """Regression tests for the reviewed 500 paths — every one of these was a
    reproduced HTTP 500 before the finiteness/RecursionError guards."""

    def test_nan_and_infinity_in_int_fields_degrade_not_500(self) -> None:
        # json.loads accepts the non-standard NaN/Infinity literals; 1e400→inf
        self._write('{"ranked": [{"name": "x", "benefit": NaN, "cost": Infinity},'
                    ' {"name": "y", "benefit": 1e400}]}')
        r = self.client.get(URL, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['available'])
        self.assertEqual(body['ranked'][0]['benefit'], 5)   # NaN → default
        self.assertEqual(body['ranked'][0]['cost'], 5)      # inf → default
        self.assertEqual(body['ranked'][1]['benefit'], 5)   # 1e400 → default

    def test_nonfinite_float_fields_default_not_null(self) -> None:
        # weight/rank_score NaN previously serialized to null in a non-Optional
        # field — the exact dashboard↔droplet type-drift crash class.
        self._write('{"ranked": [{"name": "x", "weight": NaN, "rank_score": '
                    'Infinity}]}')
        r = self.client.get(URL, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        e = r.json()['ranked'][0]
        self.assertEqual(e['weight'], 0.0)
        self.assertEqual(e['rank_score'], 0.0)

    def test_pathological_nesting_is_unavailable_not_500(self) -> None:
        self._write('[' * 200000 + ']' * 200000)  # RecursionError in json.load
        r = self.client.get(URL, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['available'])

    def test_brief_non_dict_degrades_to_empty(self) -> None:
        self._write({'ranked': [_entry(1, brief='not a dict')]})
        r = self.client.get(URL, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['ranked'][0]['brief'], {})


if __name__ == '__main__':
    unittest.main()
