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
from unittest import mock

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
        # projects-v3 P3: promote now MOVES the capture into a new single-phase
        # project on projects.json (heal_projects_store is the SOLE committer) —
        # bind the projects path onto the tmpdir so the live store is never
        # touched. The queue dir is bound too: an absent file there is the
        # assertion that promote no longer queues a mission.
        self.projects_path = self.tmp / 'agents' / 'beacon' / 'projects.json'
        self.queue_dir = self.tmp / 'agents' / 'blackboard' / 'new-mission-queue'

        self._orig_captures_path = da._captures_json_path
        self._orig_missions_path = da._missions_json_path
        self._orig_projects_path = da._projects_json_path
        self._orig_queue_dir = da._new_mission_queue_dir
        self._orig_github = da._github_api_request
        self._orig_gh_token = da._github_token
        self._orig_la_client = da._get_larry_action_supabase_client
        da._captures_json_path = lambda: self.captures_path  # type: ignore[assignment]
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        da._projects_json_path = lambda: self.projects_path  # type: ignore[assignment]
        da._new_mission_queue_dir = lambda: self.queue_dir  # type: ignore[assignment]
        # Phase S (S7): the route resolves the in-flight gate from the live
        # chain_events client. Pin it to None so the resolver is skipped (actions
        # apply immediately, as before) — the defer/apply branch is covered by
        # InFlightDeferTest with an injected resolver, and a real SUPABASE_URL in
        # the env would otherwise trip the test-isolation guard.
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._captures_json_path = self._orig_captures_path  # type: ignore[assignment]
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        da._projects_json_path = self._orig_projects_path  # type: ignore[assignment]
        da._new_mission_queue_dir = self._orig_queue_dir  # type: ignore[assignment]
        da._github_api_request = self._orig_github  # type: ignore[assignment]
        da._github_token = self._orig_gh_token  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_la_client  # type: ignore[assignment]
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

    def _read_projects(self) -> list:
        return json.loads(self.projects_path.read_text())['projects']


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


# ====== promote (projects-v3 P3: MOVE the capture into a new project, no PR) ======
#
# Promote is a move, not a record (spec § 0 / § 4 decision 2): it creates a new
# single-phase project at Brainstorm on projects.json (heal_projects_store is the
# SOLE committer — the dashboard is a non-committer) AND flips the capture out of
# the parked/funnel lane. No mission is minted, no queue file, no GitHub PR.


class PromoteTest(_ActionsTestBase):
    def test_creates_project_and_flips_capture_no_github(self):
        self._seed(_cap('cap-1', title='Aging idea'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'promote'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['project_id'], 'aging-idea')
        self.assertEqual(body['phase_id'], 'aging-idea')
        self.assertEqual(body['status'], 'promoted')
        self.assertTrue(body['applied'])
        # No mission, no PR — never a mission_id/branch/pr_url, never a GitHub call.
        self.assertNotIn('mission_id', body)
        self.assertNotIn('pr_url', body)
        self.assertNotIn('branch', body)
        self.assertEqual(self.gh.calls, [])
        # Promote does NOT queue a mission anymore.
        self.assertFalse((self.queue_dir / 'aging-idea.json').exists())

        # A new single-phase project landed on projects.json at Brainstorm,
        # active, carrying the capture provenance (the reversibility cross-ref).
        projects = self._read_projects()
        self.assertEqual(len(projects), 1)
        proj = projects[0]
        self.assertEqual(proj['id'], 'aging-idea')
        self.assertEqual(proj['title'], 'Aging idea')
        self.assertEqual(proj['state'], 'active')
        self.assertTrue(proj['one_off'])
        self.assertEqual(proj['promoted_from'], {'kind': 'capture', 'capture_id': 'cap-1'})
        self.assertEqual(proj['phases'][0]['lifecycle_state'], 'brainstorm')

        # The LOCAL captures.json is flipped in place (the GC healer commits it) —
        # state:promoted removes it from the parked/funnel lane.
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'promoted')
        self.assertEqual(cap['promoted_to'], 'aging-idea')
        # Phase S (S1): the spawned ref links the card back to the PROJECT it
        # created — join key = project_id.
        self.assertEqual(cap['spawned']['kind'], 'project')
        self.assertEqual(cap['spawned']['project_id'], 'aging-idea')
        self.assertIn('stamped_at', cap['spawned'])

    def test_brief_override_seeds_phase_desired_end_state(self):
        self._seed(_cap('cap-1', title='Aging idea', note='do the thing'))
        self.client.post(_endpoint('cap-1'), headers=AUTH,
                         json={'action': 'promote', 'brief': 'so the loop closes'})
        proj = self._read_projects()[0]
        self.assertEqual(proj['phases'][0]['desired_end_state'], 'so the loop closes')

    def test_brief_defaults_to_capture_note(self):
        self._seed(_cap('cap-1', title='Aging idea', note='do the thing'))
        self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        proj = self._read_projects()[0]
        self.assertEqual(proj['phases'][0]['desired_end_state'], 'do the thing')

    def test_overrides_name_repo_and_north_star(self):
        self._seed(_cap('cap-1', title='Aging idea'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={
            'action': 'promote', 'name': 'Shiny Project',
            'repo': 'ourliberty-dashboard', 'north_star_ref': 'docs/ns.md#p3',
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['project_id'], 'shiny-project')
        proj = self._read_projects()[0]
        self.assertEqual(proj['title'], 'Shiny Project')
        self.assertEqual(proj['repo'], 'ourliberty-dashboard')
        self.assertEqual(proj['north_star_ref'], 'docs/ns.md#p3')

    def test_repo_defaults_to_capture_origin(self):
        self._seed(_cap('cap-1', title='Aging idea', repo='ourliberty-agent-core'))
        self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(self._read_projects()[0]['repo'], 'ourliberty-agent-core')

    def test_bogus_origin_repo_dropped_to_none(self):
        # Bug 1: a capture emitted from a local working dir inherits the dir name
        # as origin.repo (e.g. `ol-work`), which is not a buildable repo. Promote
        # must NOT carry it into projects.json — store None so the Launch
        # endpoint re-derives the real repo from the spec at build time.
        self._seed(_cap('cap-1', title='Aging idea', repo='ol-work'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'promote'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._read_projects()[0]['repo'])

    def test_missing_capture_returns_404_no_write(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(self.projects_path.exists())

    def test_non_parked_returns_409_no_write(self):
        self._seed(_cap('cap-1', state='dropped'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(self.projects_path.exists())

    def test_double_promote_returns_409_and_one_project(self):
        # The first promote flips the capture to state:promoted; the second
        # hits the _require_parked guard (409). No duplicate project is minted.
        self._seed(_cap('cap-1', title='Aging idea'))
        r1 = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r1.status_code, 200, r1.text)
        r2 = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r2.status_code, 409, r2.text)
        self.assertEqual(len(self._read_projects()), 1)

    def test_id_collision_disambiguates_not_overwrites(self):
        # A project already using the slug forces a unique id (no overwrite).
        self.projects_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_path.write_text(json.dumps({
            'schema_version': 1,
            'projects': [{'id': 'aging-idea', 'title': 'Pre-existing',
                          'state': 'active', 'phases': [{'id': 'aging-idea'}],
                          'promoted_from': {'kind': 'capture', 'capture_id': 'other'}}],
        }, indent=2) + '\n')
        self._seed(_cap('cap-1', title='Aging idea'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'promote'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['project_id'], 'aging-idea-2')
        ids = sorted(p['id'] for p in self._read_projects())
        self.assertEqual(ids, ['aging-idea', 'aging-idea-2'])


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


# ====== S7: in-flight overrules a late pause (drop/snooze defer, never interrupt) ======


class InFlightDeferTest(_ActionsTestBase):
    """A pause(=snooze)/drop on a card whose linked work is in-flight is RECORDED
    as a pending_action (not applied) so the run is never interrupted; the same
    action on a card whose work has reached a safe stop applies immediately. The
    handlers are called directly with an injected resolver — the route resolves
    the resolver from the live chain_events client (covered by _spawned_work_in_flight
    unit coverage), so injecting here keeps the defer/apply branch deterministic."""

    FUTURE = '2099-01-01T00:00:00+00:00'

    def _drop(self, cap, *, in_flight, reason='stale'):
        self._seed(cap)
        return da._handle_capture_drop(
            capture_id=cap['id'], reason=reason,
            captures_path=self.captures_path,
            in_flight_resolver=lambda _c: in_flight)

    def _snooze(self, cap, *, in_flight):
        self._seed(cap)
        return da._handle_capture_snooze(
            capture_id=cap['id'], snoozed_until=self.FUTURE,
            captures_path=self.captures_path,
            in_flight_resolver=lambda _c: in_flight)

    def test_drop_defers_when_in_flight(self):
        out = self._drop(_cap('cap-1'), in_flight=True, reason='obsolete')
        self.assertFalse(out['applied'])
        self.assertTrue(out['deferred'])
        self.assertEqual(out['pending_action']['action'], 'drop')
        self.assertEqual(out['pending_action']['args'], {'reason': 'obsolete'})
        self.assertIn('requested_at', out['pending_action'])
        # the card is NOT dropped — the run continues; the intent is recorded.
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'parked')
        self.assertEqual(cap['pending_action']['action'], 'drop')

    def test_drop_applies_when_not_in_flight(self):
        out = self._drop(_cap('cap-1'), in_flight=False)
        self.assertTrue(out['applied'])
        self.assertEqual(out['state'], 'dropped')
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'dropped')
        self.assertNotIn('pending_action', cap)

    def test_snooze_defers_when_in_flight(self):
        out = self._snooze(_cap('cap-1'), in_flight=True)
        self.assertFalse(out['applied'])
        self.assertTrue(out['deferred'])
        self.assertEqual(out['pending_action']['action'], 'snooze')
        self.assertEqual(out['pending_action']['args']['snoozed_until'], self.FUTURE)
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'parked')
        self.assertNotIn('snoozed_until', cap)  # not applied yet
        self.assertEqual(cap['pending_action']['action'], 'snooze')

    def test_snooze_applies_when_not_in_flight(self):
        out = self._snooze(_cap('cap-1'), in_flight=False)
        self.assertTrue(out['applied'])
        self.assertEqual(out['snoozed_until'], self.FUTURE)
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['snoozed_until'], self.FUTURE)
        self.assertNotIn('pending_action', cap)

    def test_no_resolver_applies_directly(self):
        # Backward-compatible: with no resolver the action applies as before.
        self._seed(_cap('cap-1'))
        out = da._handle_capture_drop(
            capture_id='cap-1', reason=None, captures_path=self.captures_path)
        self.assertTrue(out['applied'])
        self.assertEqual(out['state'], 'dropped')


class SpawnedWorkInFlightTest(unittest.TestCase):
    """S4<->S7: the in-flight gate (`_spawned_work_in_flight`) must treat a detected
    terminal failure as a safe stop. Failed work keeps its `session_start` but never
    merges, so `derive_phase_for_task` would report it in-flight forever — without the
    failure short-circuit a deferred pause/drop on a failed card would never apply."""

    @staticmethod
    def _cap(task_id='delegate-x'):
        return {'spawned': {'kind': 'delegate', 'task_id': task_id}}

    def test_no_spawned_ref_is_not_in_flight(self):
        self.assertFalse(da._spawned_work_in_flight({}, object()))

    def test_no_task_id_is_not_in_flight(self):
        self.assertFalse(da._spawned_work_in_flight({'spawned': {}}, object()))

    def test_failure_is_safe_stop_short_circuits_before_fetch(self):
        import build_sequence_advancer as bsa
        with mock.patch.object(bsa, 'chain_event_says_failed',
                               return_value='forge_reject: tests broke'), \
                mock.patch.object(da, '_fetch_events_for_task_ids') as fetch:
            self.assertFalse(da._spawned_work_in_flight(self._cap(), object()))
            fetch.assert_not_called()

    def test_in_flight_when_not_failed(self):
        import build_sequence_advancer as bsa
        events = [{'task_id': 'delegate-x', 'event_type': 'session_start',
                   'agent': 'forge', 'pr_url': None,
                   'ts': '2026-06-15T10:00:00+00:00', 'payload': {}}]
        with mock.patch.object(bsa, 'chain_event_says_failed', return_value=None), \
                mock.patch.object(da, '_fetch_events_for_task_ids',
                                  return_value={'delegate-x': events}):
            self.assertTrue(da._spawned_work_in_flight(self._cap(), object()))

    def test_no_events_and_not_failed_is_not_in_flight(self):
        import build_sequence_advancer as bsa
        with mock.patch.object(bsa, 'chain_event_says_failed', return_value=None), \
                mock.patch.object(da, '_fetch_events_for_task_ids',
                                  return_value={}):
            self.assertFalse(da._spawned_work_in_flight(self._cap(), object()))


# ==================== dispatch ====================


class InvalidActionTest(_ActionsTestBase):
    def test_unknown_action_returns_400_no_github(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.gh.calls, [])


if __name__ == '__main__':
    unittest.main()
