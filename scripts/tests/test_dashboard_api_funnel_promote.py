#!/usr/bin/env python3
"""Tests for POST /api/funnel/promote — the ONE unified Promote gesture
(projects-v3 P3, p3-promote-endpoint, spec § 0 / § 4 decision 2).

This endpoint MOVES any funnel item — a parked capture OR a proposed mission —
into a new single-phase project at Brainstorm and removes it from its funnel
lane. The two lanes route through the SAME project-create core (no divergent
path): a capture delegates to the capture-promote handler (flip the capture), a
mission to the mission-accept handler (the project's `promoted_from` cross-ref
suppresses it from the funnel; the mission record is never mutated). `kind` is
optional — when omitted the ref is auto-resolved (captures first, then missions).

The dashboard stays a non-committer: the project is written to projects.json on
disk (heal_projects_store commits), the capture flip rides the captures committer,
and NO missions PR is opened. A correct run therefore leaves `da._github_api_request`
entirely uncalled — the recorder raises on any attempt.

Path-isolation mirrors test_dashboard_api_capture_actions.py: each test owns a
fresh tmpdir with captures/missions/projects paths rebound onto it.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_funnel_promote
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

ENDPOINT = '/api/funnel/promote'
ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}


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


def _mission(mid='proposed-orphan-x', *, name='Orphan X', phase='proposed',
             brief='close the gap', repo='ourliberty-agent-core', **extra):
    m = {
        'id': mid,
        'name': name,
        'phase': phase,
        'brief': brief,
        'repo': repo,
        'task_ids': [],
    }
    m.update(extra)
    return m


class _GithubRecorder:
    """The unified promote opens no PR — any github call is a regression."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({'method': method, 'url': url})
        raise AssertionError(
            f'unexpected github call: {method} {url} — funnel promote opens no PR',
        )


class _FunnelPromoteTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-funnel-promote-'))
        beacon = self.tmp / 'agents' / 'beacon'
        self.captures_path = beacon / 'captures.json'
        self.missions_path = beacon / 'missions.json'
        self.projects_path = beacon / 'projects.json'

        self._orig_captures_path = da._captures_json_path
        self._orig_missions_path = da._missions_json_path
        self._orig_projects_path = da._projects_json_path
        self._orig_github = da._github_api_request
        self._orig_la_client = da._get_larry_action_supabase_client
        da._captures_json_path = lambda: self.captures_path
        da._missions_json_path = lambda: self.missions_path
        da._projects_json_path = lambda: self.projects_path
        # Pin the in-flight resolver to None (no live chain_events read).
        da._get_larry_action_supabase_client = lambda: None
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh

        # Default empty registries; tests override as needed.
        self._seed_captures()
        self._seed_missions()

        self.client = TestClient(da.app)

    def tearDown(self):
        da._captures_json_path = self._orig_captures_path
        da._missions_json_path = self._orig_missions_path
        da._projects_json_path = self._orig_projects_path
        da._github_api_request = self._orig_github
        da._get_larry_action_supabase_client = self._orig_la_client

    def _seed_captures(self, *caps) -> None:
        self.captures_path.parent.mkdir(parents=True, exist_ok=True)
        self.captures_path.write_text(
            json.dumps({'schema_version': 2, 'captures': list(caps)}, indent=2) + '\n')

    def _seed_missions(self, *missions) -> None:
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)}, indent=2) + '\n')

    def _read_captures(self) -> list:
        return json.loads(self.captures_path.read_text())['captures']

    def _read_missions(self) -> list:
        return json.loads(self.missions_path.read_text())['missions']

    def _read_projects(self) -> list:
        return json.loads(self.projects_path.read_text())['projects']


# ==================== auth ====================


class AuthTest(_FunnelPromoteTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(ENDPOINT, headers={'X-Actor': ACTOR},
                             json={'ref': 'cap-1'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_missing_actor_returns_401(self):
        self._seed_captures(_cap('cap-1'))
        r = self.client.post(ENDPOINT, headers={'X-Dashboard-Token': TOKEN},
                             json={'ref': 'cap-1'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_unlisted_actor_returns_401(self):
        self._seed_captures(_cap('cap-1'))
        r = self.client.post(
            ENDPOINT,
            headers={'X-Dashboard-Token': TOKEN, 'X-Actor': 'mallory@evil.test'},
            json={'ref': 'cap-1'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['detail'], 'unauthorized')


# ==================== validation ====================


class ValidationTest(_FunnelPromoteTestBase):
    def test_blank_ref_is_rejected_422(self):
        r = self.client.post(ENDPOINT, headers=AUTH, json={'ref': ''})
        self.assertEqual(r.status_code, 422)

    def test_invalid_kind_returns_400(self):
        self._seed_captures(_cap('cap-1'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'ref': 'cap-1', 'kind': 'banana'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.gh.calls, [])
        # Nothing was promoted.
        self.assertFalse(self.projects_path.exists())

    def test_unknown_ref_returns_404_no_write(self):
        r = self.client.post(ENDPOINT, headers=AUTH, json={'ref': 'nope'})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(self.projects_path.exists())
        self.assertEqual(self.gh.calls, [])


# ==================== capture lane (auto-resolved) ====================


class CapturePromoteTest(_FunnelPromoteTestBase):
    def test_auto_resolves_capture_creates_project_and_flips(self):
        self._seed_captures(_cap('cap-1', title='Aging idea'))
        r = self.client.post(ENDPOINT, headers=AUTH, json={'ref': 'cap-1'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['source_kind'], 'capture')
        self.assertEqual(body['status'], 'promoted')
        self.assertTrue(body['applied'])
        self.assertEqual(body['project_id'], 'aging-idea')
        self.assertEqual(body['phase_id'], 'aging-idea')
        # No mission/PR artifacts on the unified capture path.
        self.assertNotIn('mission_id', body)
        self.assertNotIn('pr_url', body)
        self.assertEqual(self.gh.calls, [])

        projects = self._read_projects()
        self.assertEqual(len(projects), 1)
        proj = projects[0]
        self.assertEqual(proj['state'], 'active')
        self.assertEqual(proj['phases'][0]['lifecycle_state'], 'brainstorm')
        self.assertEqual(
            proj['promoted_from'], {'kind': 'capture', 'capture_id': 'cap-1'})

        cap = self._read_captures()[0]
        self.assertEqual(cap['state'], 'promoted')
        self.assertEqual(cap['promoted_to'], 'aging-idea')
        self.assertEqual(cap['spawned']['kind'], 'project')

    def test_explicit_kind_capture_honored(self):
        self._seed_captures(_cap('cap-1'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'ref': 'cap-1', 'kind': 'capture'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['source_kind'], 'capture')

    def test_overrides_threaded_through(self):
        self._seed_captures(_cap('cap-1', title='Aging idea'))
        r = self.client.post(ENDPOINT, headers=AUTH, json={
            'ref': 'cap-1', 'name': 'Renamed', 'brief': 'sharper brief',
            'repo': 'ourliberty-dashboard', 'north_star_ref': 'ns-1',
        })
        self.assertEqual(r.status_code, 200)
        proj = self._read_projects()[0]
        self.assertEqual(proj['title'], 'Renamed')
        self.assertEqual(proj['repo'], 'ourliberty-dashboard')
        self.assertEqual(proj['north_star_ref'], 'ns-1')

    def test_double_promote_returns_409(self):
        self._seed_captures(_cap('cap-1'))
        first = self.client.post(ENDPOINT, headers=AUTH, json={'ref': 'cap-1'})
        self.assertEqual(first.status_code, 200)
        second = self.client.post(ENDPOINT, headers=AUTH, json={'ref': 'cap-1'})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(self._read_projects()), 1)


# ==================== mission lane (auto-resolved) ====================


class MissionAcceptTest(_FunnelPromoteTestBase):
    def test_auto_resolves_mission_creates_project_no_mutation(self):
        self._seed_missions(_mission('proposed-orphan-x', name='Orphan X'))
        before = self._read_missions()
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'ref': 'proposed-orphan-x'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['source_kind'], 'mission')
        self.assertEqual(body['status'], 'promoted')
        self.assertTrue(body['applied'])
        self.assertEqual(body['project_id'], 'orphan-x')
        self.assertNotIn('pr_url', body)
        self.assertEqual(self.gh.calls, [])

        proj = self._read_projects()[0]
        self.assertEqual(
            proj['promoted_from'],
            {'kind': 'mission', 'mission_id': 'proposed-orphan-x'})
        self.assertEqual(proj['phases'][0]['desired_end_state'], 'close the gap')
        # The mission record is byte-identical — Accept never mutates missions.json.
        self.assertEqual(self._read_missions(), before)

    def test_explicit_kind_mission_honored(self):
        self._seed_missions(_mission('proposed-orphan-x'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'ref': 'proposed-orphan-x', 'kind': 'mission'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['source_kind'], 'mission')

    def test_non_proposed_mission_returns_409_no_write(self):
        self._seed_missions(_mission('m-flight', phase='in_flight'))
        r = self.client.post(ENDPOINT, headers=AUTH, json={'ref': 'm-flight'})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(self.projects_path.exists())

    def test_re_accept_is_idempotent_one_project(self):
        self._seed_missions(_mission('proposed-orphan-x'))
        r1 = self.client.post(ENDPOINT, headers=AUTH,
                              json={'ref': 'proposed-orphan-x'})
        r2 = self.client.post(ENDPOINT, headers=AUTH,
                              json={'ref': 'proposed-orphan-x'})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()['project_id'], r1.json()['project_id'])
        self.assertEqual(len(self._read_projects()), 1)


# ==================== auto-resolution order ====================


class AutoResolutionOrderTest(_FunnelPromoteTestBase):
    def test_captures_resolve_before_missions_on_id_collision(self):
        # Same ref id exists as BOTH a capture and a proposed mission: the
        # auto-resolver tries captures first, so the capture lane wins.
        self._seed_captures(_cap('dup-id', title='Capture Wins'))
        self._seed_missions(_mission('dup-id', name='Mission Loses'))
        r = self.client.post(ENDPOINT, headers=AUTH, json={'ref': 'dup-id'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['source_kind'], 'capture')
        # The mission was untouched — capture lane handled it.
        self.assertEqual(self._read_captures()[0]['state'], 'promoted')


if __name__ == '__main__':
    unittest.main()
