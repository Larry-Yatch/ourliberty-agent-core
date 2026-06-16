#!/usr/bin/env python3
"""Tests for POST /api/missions/captures/{id}/action (Missions v2 Phase 3 § 4).

The capture write-back endpoint — all three actions are now ONE-CLICK (no PR):
  * promote QUEUES the new mission for the missions writer (heal_orphan_autoregister
    drains <queue_dir>/<mission_id>.json into missions.json) AND flips the capture
    (promoted_to + state:promoted) on the LOCAL captures.json;
  * drop and snooze write DIRECT through the single captures.json committer (no
    second writer) — drop sets state:dropped, snooze sets snoozed_until.
heal_missions_card_gc commits the captures.json delta on its tick. Snoozed/dropped
captures are suppressed from the derive + the digest (digest coverage in
test_parked_aging_digest_generator). None of the three opens a GitHub PR, so a
correct run leaves `da._github_api_request` entirely uncalled.

Path-isolation pattern mirrors test_dashboard_api_missions.py: each test owns a
fresh tmpdir; `da._captures_json_path` / `da._missions_json_path` /
`da._new_mission_queue_dir` are rebound onto it so the live registries + queue are
never touched; a recording stub on `da._github_api_request` ASSERTS no live HTTPS
is attempted (any call is an unscripted-call failure).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_capture_actions
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}


def _endpoint(capture_id: str) -> str:
    return f'/api/missions/captures/{capture_id}/action'


def _cap(cid='cap-1', *, state='parked', title='Aging idea',
         repo='ourliberty-agent-core', note='do the thing',
         last_touched='2026-06-01T00:00:00+00:00', **extra):
    cap = {
        'id': cid,
        'title': title,
        'state': state,
        'note': note,
        'last_touched': last_touched,
        'origin': {'repo': repo},
    }
    cap.update(extra)
    return cap


# ---------- recorder ----------
#
# The one-click write-back never opens a PR, so the GitHub seam must stay
# untouched. This stub records every call and raises on the first one — any
# attempted REST call is a regression (a handler reaching for the old PR path).
# Tests additionally assert `self.gh.calls == []` for an explicit, readable check.


class _GithubRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({
            'method': method, 'url': url, 'headers': headers,
            'json_body': json_body,
        })
        raise AssertionError(
            f'unexpected github call: {method} {url} — capture write-back is '
            f'one-click and must open no PR',
        )


class _ActionsTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self._prev_gh_token = os.environ.get('GITHUB_TOKEN')
        os.environ['GITHUB_TOKEN'] = 'test-gh-token'
        self._prev_repo = os.environ.get('OURLIBERTY_MISSIONS_REPO')
        os.environ['OURLIBERTY_MISSIONS_REPO'] = 'test-owner/test-repo'

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-actions-'))
        self.captures_path = self.tmp / 'agents' / 'beacon' / 'captures.json'
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'
        # promote queues the mission for the missions writer — bind the queue
        # dir onto the tmpdir so the live blackboard is never touched (mirrors
        # test_dashboard_api_missions).
        self.queue_dir = self.tmp / 'agents' / 'blackboard' / 'new-mission-queue'

        self._orig_captures_path = da._captures_json_path
        self._orig_missions_path = da._missions_json_path
        self._orig_queue_dir = da._new_mission_queue_dir
        self._orig_github = da._github_api_request
        self._orig_gh_token = da._github_token
        da._captures_json_path = lambda: self.captures_path  # type: ignore[assignment]
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        da._new_mission_queue_dir = lambda: self.queue_dir  # type: ignore[assignment]
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._captures_json_path = self._orig_captures_path  # type: ignore[assignment]
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        da._new_mission_queue_dir = self._orig_queue_dir  # type: ignore[assignment]
        da._github_api_request = self._orig_github  # type: ignore[assignment]
        da._github_token = self._orig_gh_token  # type: ignore[assignment]
        if self._prev_gh_token is None:
            os.environ.pop('GITHUB_TOKEN', None)
        else:
            os.environ['GITHUB_TOKEN'] = self._prev_gh_token
        if self._prev_repo is None:
            os.environ.pop('OURLIBERTY_MISSIONS_REPO', None)
        else:
            os.environ['OURLIBERTY_MISSIONS_REPO'] = self._prev_repo

    # -- fixtures --
    def _seed(self, *caps) -> None:
        self.captures_path.parent.mkdir(parents=True, exist_ok=True)
        self.captures_path.write_text(
            json.dumps({'schema_version': 2, 'captures': list(caps)}, indent=2) + '\n')

    def _seed_missions(self, *missions) -> None:
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)}, indent=2) + '\n')

    def _read_local(self) -> dict:
        return json.loads(self.captures_path.read_text())

    def _read_queued(self, mission_id: str) -> dict:
        return json.loads((self.queue_dir / f'{mission_id}.json').read_text())


# ==================== auth ====================


class AuthTest(_ActionsTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(_endpoint('cap-1'),
                             headers={'X-Actor': ACTOR},
                             json={'action': 'snooze', 'snoozed_until': None})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_missing_actor_returns_401(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'),
                             headers={'X-Dashboard-Token': TOKEN},
                             json={'action': 'snooze', 'snoozed_until': None})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_unlisted_actor_returns_401(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(
            _endpoint('cap-1'),
            headers={'X-Dashboard-Token': TOKEN, 'X-Actor': 'mallory@evil.test'},
            json={'action': 'snooze', 'snoozed_until': None})
        self.assertEqual(r.status_code, 401)
        # Generic body — never echoes the rejected actor (no email oracle).
        self.assertEqual(r.json()['detail'], 'unauthorized')


# ==================== snooze (direct committer write) ====================


class SnoozeTest(_ActionsTestBase):
    FUTURE = '2099-01-01T00:00:00+00:00'

    def test_sets_snoozed_until_via_local_write_no_github(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': self.FUTURE})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['applied'])
        self.assertEqual(body['snoozed_until'], '2099-01-01T00:00:00+00:00')
        # snooze is a DIRECT committer write — never a PR.
        self.assertEqual(self.gh.calls, [])
        # Local captures.json mutated in place by the single committer.
        local = self._read_local()
        cap = next(c for c in local['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['snoozed_until'], '2099-01-01T00:00:00+00:00')
        self.assertEqual(cap['state'], 'parked')  # snooze never changes state

    def test_null_clears_snooze(self):
        self._seed(_cap('cap-1', snoozed_until=self.FUTURE))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['applied'])
        # response_model_exclude_none drops the null — a cleared snooze just
        # reports applied; the local file holds the authoritative null.
        self.assertNotIn('snoozed_until', r.json())
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertIsNone(cap['snoozed_until'])

    def test_missing_capture_returns_404(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': self.FUTURE})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()['detail']['error'], 'capture not found')

    def test_non_parked_returns_409(self):
        self._seed(_cap('cap-1', state='promoted'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': self.FUTURE})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'capture not actionable')

    def test_past_date_returns_400(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'snooze',
                                   'snoozed_until': '2000-01-01T00:00:00+00:00'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('future', r.json()['detail'])

    def test_malformed_date_returns_400(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': 'not-a-date'})
        self.assertEqual(r.status_code, 400, r.text)


# ============ promote (one-click: queue the mission + flip the capture) ============


class PromoteTest(_ActionsTestBase):
    def test_queues_mission_and_flips_capture_no_github(self):
        self._seed(_cap('cap-1', title='Aging idea'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'promote'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['mission_id'], 'aging-idea')
        self.assertEqual(body['status'], 'queued')
        self.assertTrue(body['applied'])
        # No PR — never a branch/pr_url, never a GitHub call.
        self.assertNotIn('pr_url', body)
        self.assertNotIn('branch', body)
        self.assertEqual(self.gh.calls, [])

        # The mission entry was queued for the missions writer (drafting).
        entry = self._read_queued('aging-idea')
        self.assertEqual(entry['phase'], 'drafting')
        self.assertEqual(entry['repo'], 'ourliberty-agent-core')

        # The LOCAL captures.json is flipped in place (the GC healer commits it).
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'promoted')
        self.assertEqual(cap['promoted_to'], 'aging-idea')

    def test_brief_defaults_to_capture_note(self):
        self._seed(_cap('cap-1', title='Aging idea', note='do the thing'))
        self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(self._read_queued('aging-idea')['brief'], 'do the thing')

    def test_overrides_name_and_repo(self):
        self._seed(_cap('cap-1', title='Aging idea'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={
            'action': 'promote', 'name': 'Shiny Mission', 'repo': 'ourliberty-dashboard',
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['mission_id'], 'shiny-mission')
        self.assertEqual(self._read_queued('shiny-mission')['repo'], 'ourliberty-dashboard')

    def test_missing_capture_returns_404_no_write(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(self.queue_dir.exists())

    def test_non_parked_returns_409_no_write(self):
        self._seed(_cap('cap-1', state='dropped'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(self.queue_dir.exists())

    def test_mission_id_collision_returns_409_no_write(self):
        # A mission already registered under the derived id blocks promote — and
        # leaves the capture parked (no queue file, capture unchanged).
        self._seed(_cap('cap-1', title='Aging idea'))
        self._seed_missions({'id': 'aging-idea', 'name': 'Aging idea', 'brief': 'x'})
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission_id collision')
        self.assertFalse((self.queue_dir / 'aging-idea.json').exists())
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'parked')

    def test_already_queued_returns_409(self):
        self._seed(_cap('cap-1', title='Aging idea'))
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        (self.queue_dir / 'aging-idea.json').write_text('{"id": "aging-idea"}')
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission_id queued')


# ============ drop (one-click: direct captures.json committer write) ============


class DropTest(_ActionsTestBase):
    def test_flips_dropped_in_place_no_github(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'drop', 'reason': 'no longer relevant'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['applied'])
        self.assertEqual(body['state'], 'dropped')
        self.assertNotIn('pr_url', body)   # no PR
        self.assertNotIn('branch', body)
        self.assertNotIn('mission_id', body)  # drop opens no mission
        self.assertEqual(self.gh.calls, [])

        # The LOCAL captures.json is flipped in place (the GC healer commits it).
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'dropped')
        self.assertEqual(cap['drop_reason'], 'no longer relevant')

    def test_drop_without_reason_omits_field(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(r.status_code, 200, r.text)
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'dropped')
        self.assertNotIn('drop_reason', cap)

    def test_non_string_reason_rejected_capture_untouched(self):
        # A non-string reason is rejected by request validation (pydantic → 422)
        # before the handler runs, so the capture is never flipped.
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'drop', 'reason': 123})
        self.assertEqual(r.status_code, 422, r.text)
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'parked')

    def test_missing_capture_returns_404(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(r.status_code, 404, r.text)

    def test_non_parked_returns_409_no_write(self):
        self._seed(_cap('cap-1', state='promoted'))
        before = self.captures_path.read_text()
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.captures_path.read_text(), before)


# ==================== dispatch ====================


class InvalidActionTest(_ActionsTestBase):
    def test_unknown_action_returns_400_no_github(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.gh.calls, [])


if __name__ == '__main__':
    unittest.main()
