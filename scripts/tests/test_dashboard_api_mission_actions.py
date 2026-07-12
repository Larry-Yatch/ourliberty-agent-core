#!/usr/bin/env python3
"""Tests for POST /api/system/missions/{id}/action (Missions v2 Phase 3 § 5).

The mission write-back endpoint: defer / resume / reprioritize are ALL PR-backed
(missions.json is the curated registry — every change auditable) and reuse the
new-mission GitHub-REST mechanism (GET main ref → create branch → GET+PUT
missions.json → open PR). The LOCAL missions.json is never mutated — it updates
via `git pull` on merge.

  defer        — phase: deferred + deferred_reason. No derive change: the derive
                 already treats deferred as a mission-level override.
  resume       — clear the override (phase → drafting, deferred_reason → null).
  reprioritize — set the additive optional `priority` int (null clears it).

Path-isolation + GitHub-recorder pattern mirrors
test_dashboard_api_capture_actions.py: each test owns a fresh tmpdir;
`da._missions_json_path` is rebound onto it so the live registry is never
touched; all GitHub REST is intercepted by a recording stub on
`da._github_api_request` (no live HTTPS).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_mission_actions
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


def _endpoint(mission_id: str) -> str:
    return f'/api/system/missions/{mission_id}/action'


def _mission(mid='m-1', *, phase='drafting', name='Mission One',
             brief='do the thing', repo='ourliberty-agent-core',
             task_ids=None, **extra):
    mission = {
        'id': mid,
        'name': name,
        'phase': phase,
        'brief': brief,
        'spec_docs': [],
        'task_ids': list(task_ids or []),
        'repo': repo,
        'created': '2026-06-01',
        'deferred_reason': None,
    }
    mission.update(extra)
    return mission


# ---------- recorder (mirrors test_dashboard_api_capture_actions) ----------


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


def _mission_edit_github_script(gh: _GithubRecorder, *, branch: str,
                                pr_url: str = 'https://github.com/test-owner/test-repo/pull/77') -> None:
    """Script the 5 GitHub calls for a single-file missions.json edit PR (GET
    main ref → create branch → GET+PUT missions.json → open PR)."""
    gh.script('GET', '/git/refs/heads/main', _FakeResponse(200, {'object': {'sha': 'main-sha'}}))
    gh.script('POST', '/git/refs', _FakeResponse(201, {'ref': f'refs/heads/{branch}'}))
    gh.script('GET', f'/contents/agents/beacon/missions.json?ref={branch}',
              _FakeResponse(200, {'sha': 'missions-blob'}))
    gh.script('PUT', '/contents/agents/beacon/missions.json',
              _FakeResponse(200, {'content': {'sha': 'new-missions-blob'}}))
    gh.script('POST', '/pulls', _FakeResponse(201, {'html_url': pr_url, 'number': 77}))


class _MissionActionsTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self._prev_gh_token = os.environ.get('GITHUB_TOKEN')
        os.environ['GITHUB_TOKEN'] = 'test-gh-token'
        self._prev_repo = os.environ.get('OURLIBERTY_MISSIONS_REPO')
        os.environ['OURLIBERTY_MISSIONS_REPO'] = 'test-owner/test-repo'

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-mission-actions-'))
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'
        # projects-v3 P3: accept now MOVES the proposed mission into a new
        # single-phase project on projects.json (heal_projects_store is the SOLE
        # committer) — bind the projects path onto the tmpdir so the live store
        # is never touched.
        self.projects_path = self.tmp / 'agents' / 'beacon' / 'projects.json'

        self._orig_missions_path = da._missions_json_path
        self._orig_projects_path = da._projects_json_path
        self._orig_github = da._github_api_request
        self._orig_gh_token = da._github_token
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        da._projects_json_path = lambda: self.projects_path  # type: ignore[assignment]
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        da._projects_json_path = self._orig_projects_path  # type: ignore[assignment]
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
    def _seed(self, *missions) -> None:
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)}, indent=2) + '\n')

    def _decode_put(self, call: dict) -> dict:
        return json.loads(base64.b64decode(call['json_body']['content']).decode('utf-8'))

    def _put_entry(self, mission_id: str) -> dict:
        put = next(c for c in self.gh.calls if c['method'] == 'PUT')
        new_registry = self._decode_put(put)
        return next(m for m in new_registry['missions'] if m['id'] == mission_id)

    def _read_projects(self) -> list:
        return json.loads(self.projects_path.read_text())['projects']


# ==================== auth ====================


class AuthTest(_MissionActionsTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(_endpoint('m-1'),
                             headers={'X-Actor': ACTOR},
                             json={'action': 'resume'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_missing_actor_returns_401(self):
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('m-1'),
                             headers={'X-Dashboard-Token': TOKEN},
                             json={'action': 'resume'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_unlisted_actor_returns_401(self):
        self._seed(_mission('m-1'))
        r = self.client.post(
            _endpoint('m-1'),
            headers={'X-Dashboard-Token': TOKEN, 'X-Actor': 'mallory@evil.test'},
            json={'action': 'resume'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['detail'], 'unauthorized')


# ==================== defer ====================


class DeferTest(_MissionActionsTestBase):
    def test_opens_pr_setting_deferred_phase_and_reason(self):
        self._seed(_mission('m-1', phase='in_flight'))
        branch = 'chore/defer-mission-m-1'
        _mission_edit_github_script(self.gh, branch=branch)
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'action': 'defer', 'reason': 'waiting on upstream'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['branch'], branch)
        self.assertEqual(body['pr_url'],
                         'https://github.com/test-owner/test-repo/pull/77')

        # Exactly one branch + one PR — single-field auditable edit.
        self.assertEqual(len(self.gh.calls), 5)
        self.assertEqual(len([c for c in self.gh.calls if c['url'].endswith('/git/refs')]), 1)
        self.assertEqual(len([c for c in self.gh.calls if c['url'].endswith('/pulls')]), 1)

        entry = self._put_entry('m-1')
        self.assertEqual(entry['phase'], 'deferred')
        self.assertEqual(entry['deferred_reason'], 'waiting on upstream')

    def test_defer_without_reason_sets_null(self):
        self._seed(_mission('m-1'))
        _mission_edit_github_script(self.gh, branch='chore/defer-mission-m-1')
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'defer'})
        self.assertEqual(r.status_code, 200, r.text)
        entry = self._put_entry('m-1')
        self.assertEqual(entry['phase'], 'deferred')
        self.assertIsNone(entry['deferred_reason'])

    def test_local_missions_not_mutated(self):
        self._seed(_mission('m-1'))
        before = self.missions_path.read_text()
        _mission_edit_github_script(self.gh, branch='chore/defer-mission-m-1')
        self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'defer'})
        self.assertEqual(self.missions_path.read_text(), before)

    def test_already_deferred_returns_409_no_github(self):
        self._seed(_mission('m-1', phase='deferred'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'defer'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission already deferred')
        self.assertEqual(self.gh.calls, [])

    def test_missing_mission_returns_404_no_github(self):
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'defer'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not found')
        self.assertEqual(self.gh.calls, [])

    def test_non_string_reason_rejected_at_model_no_github(self):
        # `reason: Optional[str]` — pydantic rejects an int before the handler.
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'action': 'defer', 'reason': 123})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.gh.calls, [])

    def test_token_missing_returns_500_no_github(self):
        self._seed(_mission('m-1'))
        da._github_token = lambda: None  # type: ignore[assignment]
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'defer'})
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(r.json()['detail']['error'], 'github token missing')
        self.assertEqual(self.gh.calls, [])


# ==================== resume ====================


class ResumeTest(_MissionActionsTestBase):
    def test_opens_pr_clearing_deferred(self):
        self._seed(_mission('m-1', phase='deferred', deferred_reason='blocked'))
        branch = 'chore/resume-mission-m-1'
        _mission_edit_github_script(self.gh, branch=branch)
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'resume'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['branch'], branch)

        entry = self._put_entry('m-1')
        self.assertEqual(entry['phase'], 'drafting')
        self.assertIsNone(entry['deferred_reason'])

    def test_resume_non_deferred_returns_409_no_github(self):
        self._seed(_mission('m-1', phase='in_flight'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'resume'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not deferred')
        self.assertEqual(self.gh.calls, [])

    def test_missing_mission_returns_404_no_github(self):
        self._seed(_mission('m-1', phase='deferred'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'resume'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_local_missions_not_mutated(self):
        self._seed(_mission('m-1', phase='deferred', deferred_reason='blocked'))
        before = self.missions_path.read_text()
        _mission_edit_github_script(self.gh, branch='chore/resume-mission-m-1')
        self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'resume'})
        self.assertEqual(self.missions_path.read_text(), before)


# ==================== reprioritize ====================


class ReprioritizeTest(_MissionActionsTestBase):
    def test_opens_pr_setting_priority(self):
        self._seed(_mission('m-1'))
        branch = 'chore/reprioritize-mission-m-1'
        _mission_edit_github_script(self.gh, branch=branch)
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'action': 'reprioritize', 'priority': 5})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['branch'], branch)

        entry = self._put_entry('m-1')
        self.assertEqual(entry['priority'], 5)
        # additive — other fields untouched.
        self.assertEqual(entry['phase'], 'drafting')

    def test_priority_null_clears_field(self):
        self._seed(_mission('m-1', priority=9))
        _mission_edit_github_script(self.gh, branch='chore/reprioritize-mission-m-1')
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'action': 'reprioritize', 'priority': None})
        self.assertEqual(r.status_code, 200, r.text)
        entry = self._put_entry('m-1')
        self.assertNotIn('priority', entry)

    def test_zero_priority_is_valid(self):
        self._seed(_mission('m-1'))
        _mission_edit_github_script(self.gh, branch='chore/reprioritize-mission-m-1')
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'action': 'reprioritize', 'priority': 0})
        self.assertEqual(r.status_code, 200, r.text)
        entry = self._put_entry('m-1')
        self.assertEqual(entry['priority'], 0)

    def test_non_int_priority_rejected_at_model_no_github(self):
        # `priority: Optional[int]` — pydantic rejects a non-numeric string
        # before the handler (the handler's own int/bool guard is defense for
        # direct, non-HTTP callers).
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'action': 'reprioritize', 'priority': 'high'})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_missing_mission_returns_404_no_github(self):
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH,
                             json={'action': 'reprioritize', 'priority': 1})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_local_missions_not_mutated(self):
        self._seed(_mission('m-1'))
        before = self.missions_path.read_text()
        _mission_edit_github_script(self.gh, branch='chore/reprioritize-mission-m-1')
        self.client.post(_endpoint('m-1'), headers=AUTH,
                         json={'action': 'reprioritize', 'priority': 3})
        self.assertEqual(self.missions_path.read_text(), before)


# ==================== accept (projects-v3 P3: unified onto Promote, no PR) ====================
#
# Accept is now the SAME gesture as Promote (spec § 4 decision 2): it MOVES the
# proposed mission into a new single-phase project at Brainstorm carrying
# `promoted_from: {kind: mission, mission_id}`. The mission is NOT mutated (no
# missions.json PR, no GitHub call); the funnel derive suppresses it via the
# project's cross-ref. Reversible with no data loss: archiving the project
# un-suppresses the mission (the mission record was never touched).


class AcceptTest(_MissionActionsTestBase):
    def test_creates_project_no_pr_no_mission_mutation(self):
        self._seed(_mission('proposed-orphan-x', phase='proposed',
                            name='Orphan X', brief='close the gap',
                            task_ids=['orphan-x']))
        r = self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                             json={'action': 'accept'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['project_id'], 'orphan-x')
        self.assertEqual(body['phase_id'], 'orphan-x')
        self.assertEqual(body['status'], 'promoted')
        self.assertTrue(body['applied'])
        # No PR — unified onto Promote: never a branch/pr_url, never a GitHub call.
        self.assertNotIn('pr_url', body)
        self.assertNotIn('branch', body)
        self.assertEqual(self.gh.calls, [])

        # A new single-phase project landed on projects.json at Brainstorm,
        # active, carrying the mission provenance (the suppression cross-ref).
        projects = self._read_projects()
        self.assertEqual(len(projects), 1)
        proj = projects[0]
        self.assertEqual(proj['id'], 'orphan-x')
        self.assertEqual(proj['title'], 'Orphan X')           # from mission name
        self.assertEqual(proj['state'], 'active')
        self.assertTrue(proj['one_off'])
        self.assertEqual(proj['repo'], 'ourliberty-agent-core')
        self.assertEqual(proj['promoted_from'],
                         {'kind': 'mission', 'mission_id': 'proposed-orphan-x'})
        self.assertEqual(proj['phases'][0]['lifecycle_state'], 'brainstorm')
        self.assertEqual(proj['phases'][0]['desired_end_state'], 'close the gap')

    def test_mission_not_mutated(self):
        # The mission stays exactly as-is (proposed) — the cross-ref, not a phase
        # flip, is the 'accepted' signal, so re-accept stays idempotent.
        self._seed(_mission('proposed-orphan-x', phase='proposed',
                            task_ids=['orphan-x']))
        before = self.missions_path.read_text()
        self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                         json={'action': 'accept'})
        self.assertEqual(self.missions_path.read_text(), before)

    def test_re_accept_is_idempotent_one_project(self):
        # A second accept finds the existing active project by promoted_from and
        # returns it (applied=False) instead of minting a duplicate.
        self._seed(_mission('proposed-orphan-x', phase='proposed',
                            task_ids=['orphan-x']))
        r1 = self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                              json={'action': 'accept'})
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertTrue(r1.json()['applied'])
        r2 = self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                              json={'action': 'accept'})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertFalse(r2.json()['applied'])
        self.assertEqual(r2.json()['project_id'], r1.json()['project_id'])
        self.assertEqual(len(self._read_projects()), 1)

    def test_archived_project_lets_re_accept_recreate(self):
        # Reversibility: archiving the project (the escape hatch) un-suppresses
        # the mission, so accepting again creates a fresh project (no data loss).
        self._seed(_mission('proposed-orphan-x', phase='proposed',
                            name='Orphan X', task_ids=['orphan-x']))
        self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                         json={'action': 'accept'})
        # Archive the project (what the reversibility path does).
        registry = json.loads(self.projects_path.read_text())
        registry['projects'][0]['state'] = 'archived'
        self.projects_path.write_text(json.dumps(registry, indent=2) + '\n')
        r = self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                             json={'action': 'accept'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['applied'])
        # The archived project persists; a fresh active one is created alongside.
        states = sorted(p['state'] for p in self._read_projects())
        self.assertEqual(states, ['active', 'archived'])

    def test_accept_non_proposed_returns_409_no_write(self):
        self._seed(_mission('m-1', phase='drafting'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'accept'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not proposed')
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(self.projects_path.exists())

    def test_missing_mission_returns_404_no_write(self):
        self._seed(_mission('proposed-orphan-x', phase='proposed'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'accept'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(self.projects_path.exists())


# ==================== dismiss ====================


class DismissTest(_MissionActionsTestBase):
    def test_opens_pr_setting_acknowledged_phase_unchanged(self):
        self._seed(_mission('proposed-orphan-x', phase='proposed',
                            task_ids=['orphan-x']))
        branch = 'chore/dismiss-mission-proposed-orphan-x'
        _mission_edit_github_script(self.gh, branch=branch)
        r = self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                             json={'action': 'dismiss'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['branch'], branch)

        self.assertEqual(len(self.gh.calls), 5)

        entry = self._put_entry('proposed-orphan-x')
        # Additive flag set; phase STAYS proposed; task_id still registered so the
        # healer never re-proposes it.
        self.assertIs(entry['acknowledged'], True)
        self.assertEqual(entry['phase'], 'proposed')
        self.assertEqual(entry['task_ids'], ['orphan-x'])

    def test_dismiss_non_proposed_returns_409_no_github(self):
        self._seed(_mission('m-1', phase='drafting'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'dismiss'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not proposed')
        self.assertEqual(self.gh.calls, [])

    def test_already_acknowledged_returns_409_no_github(self):
        # A re-dismiss (double-click, or a mission the healer already
        # acknowledged) must NOT open an empty-diff PR (PR #940 regression) —
        # the no-op is surfaced as a 409 with zero GitHub calls.
        self._seed(_mission('proposed-orphan-x', phase='proposed',
                            acknowledged=True))
        r = self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                             json={'action': 'dismiss'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission already dismissed')
        self.assertEqual(self.gh.calls, [])

    def test_missing_mission_returns_404_no_github(self):
        self._seed(_mission('proposed-orphan-x', phase='proposed'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'dismiss'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_local_missions_not_mutated(self):
        self._seed(_mission('proposed-orphan-x', phase='proposed',
                            task_ids=['orphan-x']))
        before = self.missions_path.read_text()
        _mission_edit_github_script(
            self.gh, branch='chore/dismiss-mission-proposed-orphan-x')
        self.client.post(_endpoint('proposed-orphan-x'), headers=AUTH,
                         json={'action': 'dismiss'})
        self.assertEqual(self.missions_path.read_text(), before)


# ==================== dispatch ====================


class InvalidActionTest(_MissionActionsTestBase):
    def test_unknown_action_returns_400_no_github(self):
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.gh.calls, [])


if __name__ == '__main__':
    unittest.main()
