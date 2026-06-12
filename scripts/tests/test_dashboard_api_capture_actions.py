#!/usr/bin/env python3
"""Tests for POST /api/missions/captures/{id}/action (Missions v2 Phase 3 § 4).

The capture write-back endpoint: promote / drop are PR-backed (reuse the
new-mission GitHub-REST mechanism — promote edits BOTH missions.json and
captures.json in ONE PR); snooze writes DIRECT through the single captures.json
committer (no second writer). Snoozed captures are suppressed from the derive +
the digest (covered for the digest in test_parked_aging_digest_generator).

Path-isolation + GitHub-recorder pattern mirrors test_dashboard_api_missions.py:
each test owns a fresh tmpdir; `da._captures_json_path` / `da._missions_json_path`
are rebound onto it so the live registries are never touched; all GitHub REST is
intercepted by a recording stub on `da._github_api_request` (no live HTTPS).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_capture_actions
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import base64
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


# ---------- recorder (mirrors test_dashboard_api_missions) ----------


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _GithubRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.scripted: list[tuple[str, str, _FakeResponse]] = []

    def script(self, method: str, url_suffix: str, response: _FakeResponse) -> None:
        self.scripted.append((method, url_suffix, response))

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({
            'method': method, 'url': url, 'headers': headers,
            'json_body': json_body,
        })
        for idx, (m, suffix, resp) in enumerate(self.scripted):
            if m == method and url.endswith(suffix):
                self.scripted.pop(idx)
                return resp
        raise AssertionError(
            f'unscripted github call: {method} {url}; '
            f'remaining scripts: {[(m, s) for m, s, _ in self.scripted]}',
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

        self._orig_captures_path = da._captures_json_path
        self._orig_missions_path = da._missions_json_path
        self._orig_github = da._github_api_request
        self._orig_gh_token = da._github_token
        da._captures_json_path = lambda: self.captures_path  # type: ignore[assignment]
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._captures_json_path = self._orig_captures_path  # type: ignore[assignment]
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
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

    def _read_local(self) -> dict:
        return json.loads(self.captures_path.read_text())

    def _decode_put(self, call: dict) -> dict:
        return json.loads(base64.b64decode(call['json_body']['content']).decode('utf-8'))


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


# ==================== promote (PR-backed, two files, one PR) ====================


def _promote_github_script(gh: _GithubRecorder, *, branch: str,
                           pr_url: str = 'https://github.com/test-owner/test-repo/pull/42') -> None:
    """Script the 7 GitHub calls for a two-file promote PR (GET main ref →
    create branch → GET+PUT missions.json → GET+PUT captures.json → open PR)."""
    gh.script('GET', '/git/refs/heads/main', _FakeResponse(200, {'object': {'sha': 'main-sha'}}))
    gh.script('POST', '/git/refs', _FakeResponse(201, {'ref': f'refs/heads/{branch}'}))
    gh.script('GET', f'/contents/agents/beacon/missions.json?ref={branch}',
              _FakeResponse(200, {'sha': 'missions-blob'}))
    gh.script('PUT', '/contents/agents/beacon/missions.json',
              _FakeResponse(200, {'content': {'sha': 'new-missions-blob'}}))
    gh.script('GET', f'/contents/agents/beacon/captures.json?ref={branch}',
              _FakeResponse(200, {'sha': 'captures-blob'}))
    gh.script('PUT', '/contents/agents/beacon/captures.json',
              _FakeResponse(200, {'content': {'sha': 'new-captures-blob'}}))
    gh.script('POST', '/pulls', _FakeResponse(201, {'html_url': pr_url, 'number': 42}))


class PromoteTest(_ActionsTestBase):
    def test_opens_one_pr_editing_both_registries(self):
        self._seed(_cap('cap-1', title='Aging idea'))
        branch = 'feat/promote-capture-cap-1'
        _promote_github_script(self.gh, branch=branch)
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'promote'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['mission_id'], 'aging-idea')
        self.assertEqual(body['branch'], branch)
        self.assertEqual(body['pr_url'],
                         'https://github.com/test-owner/test-repo/pull/42')

        # Exactly one branch created and one PR opened — atomic.
        self.assertEqual(len(self.gh.calls), 7)
        branch_posts = [c for c in self.gh.calls if c['url'].endswith('/git/refs')]
        pr_posts = [c for c in self.gh.calls if c['url'].endswith('/pulls')]
        self.assertEqual(len(branch_posts), 1)
        self.assertEqual(len(pr_posts), 1)

        # Both files were PUT onto the SAME branch (land together in one PR).
        puts = [c for c in self.gh.calls if c['method'] == 'PUT']
        self.assertEqual(len(puts), 2)
        for put in puts:
            self.assertEqual(put['json_body']['branch'], branch)

        missions_put = next(p for p in puts if p['url'].endswith('/contents/agents/beacon/missions.json'))
        captures_put = next(p for p in puts if p['url'].endswith('/contents/agents/beacon/captures.json'))
        # missions.json gains the new drafting mission.
        new_missions = self._decode_put(missions_put)
        entry = next(m for m in new_missions['missions'] if m['id'] == 'aging-idea')
        self.assertEqual(entry['phase'], 'drafting')
        self.assertEqual(entry['repo'], 'ourliberty-agent-core')
        # captures.json marks the capture promoted + links the mission.
        new_caps = self._decode_put(captures_put)
        cap = next(c for c in new_caps['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'promoted')
        self.assertEqual(cap['promoted_to'], 'aging-idea')

    def test_local_captures_not_mutated(self):
        self._seed(_cap('cap-1'))
        before = self.captures_path.read_text()
        _promote_github_script(self.gh, branch='feat/promote-capture-cap-1')
        self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(self.captures_path.read_text(), before)

    def test_overrides_name_and_repo(self):
        self._seed(_cap('cap-1', title='Aging idea'))
        branch = 'feat/promote-capture-cap-1'
        _promote_github_script(self.gh, branch=branch)
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={
            'action': 'promote', 'name': 'Shiny Mission', 'repo': 'ourliberty-dashboard',
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['mission_id'], 'shiny-mission')

    def test_missing_capture_returns_404_no_github(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_non_parked_returns_409_no_github(self):
        self._seed(_cap('cap-1', state='dropped'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_token_missing_returns_500_no_github(self):
        self._seed(_cap('cap-1'))
        da._github_token = lambda: None  # type: ignore[assignment]
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(r.json()['detail']['error'], 'github token missing')
        self.assertEqual(self.gh.calls, [])


# ==================== drop (PR-backed, captures.json only) ====================


def _drop_github_script(gh: _GithubRecorder, *, branch: str,
                        pr_url: str = 'https://github.com/test-owner/test-repo/pull/43') -> None:
    gh.script('GET', '/git/refs/heads/main', _FakeResponse(200, {'object': {'sha': 'main-sha'}}))
    gh.script('POST', '/git/refs', _FakeResponse(201, {'ref': f'refs/heads/{branch}'}))
    gh.script('GET', f'/contents/agents/beacon/captures.json?ref={branch}',
              _FakeResponse(200, {'sha': 'captures-blob'}))
    gh.script('PUT', '/contents/agents/beacon/captures.json',
              _FakeResponse(200, {'content': {'sha': 'new-captures-blob'}}))
    gh.script('POST', '/pulls', _FakeResponse(201, {'html_url': pr_url, 'number': 43}))


class DropTest(_ActionsTestBase):
    def test_opens_pr_marking_dropped(self):
        self._seed(_cap('cap-1'))
        branch = 'chore/drop-capture-cap-1'
        _drop_github_script(self.gh, branch=branch)
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'drop', 'reason': 'no longer relevant'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['branch'], branch)
        self.assertEqual(body['pr_url'],
                         'https://github.com/test-owner/test-repo/pull/43')
        self.assertNotIn('mission_id', body)  # drop opens no mission

        put = next(c for c in self.gh.calls if c['method'] == 'PUT')
        new_caps = self._decode_put(put)
        cap = next(c for c in new_caps['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'dropped')
        self.assertEqual(cap['drop_reason'], 'no longer relevant')

    def test_drop_without_reason_omits_field(self):
        self._seed(_cap('cap-1'))
        _drop_github_script(self.gh, branch='chore/drop-capture-cap-1')
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(r.status_code, 200, r.text)
        put = next(c for c in self.gh.calls if c['method'] == 'PUT')
        cap = next(c for c in self._decode_put(put)['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'dropped')
        self.assertNotIn('drop_reason', cap)

    def test_local_captures_not_mutated(self):
        self._seed(_cap('cap-1'))
        before = self.captures_path.read_text()
        _drop_github_script(self.gh, branch='chore/drop-capture-cap-1')
        self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(self.captures_path.read_text(), before)

    def test_non_parked_returns_409_no_github(self):
        self._seed(_cap('cap-1', state='promoted'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.gh.calls, [])


# ==================== dispatch ====================


class InvalidActionTest(_ActionsTestBase):
    def test_unknown_action_returns_400_no_github(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.gh.calls, [])


if __name__ == '__main__':
    unittest.main()
