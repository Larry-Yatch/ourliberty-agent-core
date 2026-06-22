#!/usr/bin/env python3
"""Tests for the projects-v3 P6.1 Brainstorm-card workspace endpoints:

  * POST /api/projects/brainstorm            — edit + persist the draft/decisions.
  * GET  /api/projects/phases/{id}/thread    — the card's team conversation.
  * POST /api/projects/phases/{id}/message   — Larry posts on the card.

The edit endpoint is a NON-committer: it rewrites projects.json ON DISK and relies
on heal_projects_store.py (the SOLE committer) to commit the delta — so a correct
run opens NO PR (the github recorder raises on any attempt) while the on-disk file
DOES change. Path-isolation mirrors test_dashboard_api_phase_transitions.py.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_brainstorm_edit_chat
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

EDIT = '/api/projects/brainstorm'
ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}
READ = {'X-Dashboard-Token': TOKEN}


def _phase(phid='where-are-we', *, lifecycle_state='brainstorm', **extra):
    p = {
        'id': phid, 'title': 'Where are we', 'desired_end_state': 'know',
        'lifecycle_state': lifecycle_state, 'order': 0,
        'spec_ref': None, 'sequence_ref': None,
        'brainstorm': {
            'is_ai_draft': True,
            'draft': {'end_state': 'authored end state', 'why_now': 'because'},
            'decisions': ['Pick a scope.'],
            'decisions_overflow': False,
            'handoff': {'spec_target_path': 'agents/beacon/specs/where-are-we.md'},
        },
        'brainstorm_provenance': {'by': 'beacon', 'from_state': 'brainstorm'},
    }
    p.update(extra)
    return p


def _project(pid='where-are-we', *, state='active', phases=None, **extra):
    proj = {'id': pid, 'title': pid, 'repo': 'ourliberty-dashboard',
            'state': state, 'phases': phases if phases is not None else [_phase()]}
    proj.update(extra)
    return proj


class _GithubRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({'method': method, 'url': url})
        raise AssertionError(f'unexpected github call: {method} {url} — edit opens no PR')


class _Base(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-p6_1-'))
        self.projects_path = self.tmp / 'agents' / 'beacon' / 'projects.json'
        self._orig_pp = da._projects_json_path
        self._orig_ar = da._agents_root
        self._orig_gh = da._github_api_request
        self._orig_la = da._get_larry_action_supabase_client
        da._projects_json_path = lambda: self.projects_path
        da._agents_root = lambda: self.tmp
        da._get_larry_action_supabase_client = lambda: None
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh
        self.client = TestClient(da.app)

    def tearDown(self):
        da._projects_json_path = self._orig_pp
        da._agents_root = self._orig_ar
        da._github_api_request = self._orig_gh
        da._get_larry_action_supabase_client = self._orig_la

    def _seed(self, *projects):
        self.projects_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_path.write_text(
            json.dumps({'schema_version': 1, 'projects': list(projects)}, indent=2) + '\n')

    def _disk(self):
        return json.loads(self.projects_path.read_text())


class EditBrainstormTest(_Base):
    def test_edit_draft_persists_and_returns_flat_card(self):
        self._seed(_project())
        r = self.client.post(EDIT, headers=AUTH, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we',
            'draft': 'My own words now.'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['applied'])
        self.assertEqual(body['draft'], 'My own words now.')
        self.assertEqual(body['spec_target_path'], 'agents/beacon/specs/where-are-we.md')
        # persisted on disk as a string draft + marked Larry-authored
        ph = self._disk()['projects'][0]['phases'][0]
        self.assertEqual(ph['brainstorm']['draft'], 'My own words now.')
        self.assertIs(ph['brainstorm']['is_ai_draft'], False)
        self.assertEqual(ph['brainstorm_provenance']['by'], 'larry')

    def test_edit_decisions_returns_card_objects(self):
        self._seed(_project())
        r = self.client.post(EDIT, headers=AUTH, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we',
            'decisions': ['Keep it small?', 'Ship today?']})
        self.assertEqual(r.status_code, 200, r.text)
        decisions = r.json()['decisions']
        self.assertEqual([d['title'] for d in decisions],
                         ['Keep it small?', 'Ship today?'])
        self.assertTrue(all(d['decision'] == '' and d['id'] for d in decisions))

    def test_no_fields_400(self):
        self._seed(_project())
        r = self.client.post(EDIT, headers=AUTH, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we'})
        self.assertEqual(r.status_code, 400, r.text)

    def test_missing_phase_404_no_write(self):
        self._seed(_project())
        before = self.projects_path.read_text()
        r = self.client.post(EDIT, headers=AUTH, json={
            'project_id': 'where-are-we', 'phase_id': 'nope', 'draft': 'x'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.projects_path.read_text(), before)

    def test_non_brainstorm_409(self):
        self._seed(_project(phases=[_phase(lifecycle_state='building')]))
        r = self.client.post(EDIT, headers=AUTH, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we', 'draft': 'x'})
        self.assertEqual(r.status_code, 409, r.text)

    def test_idempotent_resave_no_write(self):
        self._seed(_project())
        first = self.client.post(EDIT, headers=AUTH, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we', 'draft': 'same'})
        self.assertTrue(first.json()['applied'])
        snapshot = self.projects_path.read_text()
        second = self.client.post(EDIT, headers=AUTH, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we', 'draft': 'same'})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()['applied'])
        self.assertEqual(self.projects_path.read_text(), snapshot)

    def test_auth_required(self):
        self._seed(_project())
        self.assertEqual(self.client.post(EDIT, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we',
            'draft': 'x'}).status_code, 401)
        self.assertEqual(self.client.post(EDIT, headers=READ, json={
            'project_id': 'where-are-we', 'phase_id': 'where-are-we',
            'draft': 'x'}).status_code, 401)  # missing X-Actor


class PhaseThreadTest(_Base):
    REF = 'where-are-we::where-are-we'  # composite project_id::phase_id

    def test_thread_404_missing_phase(self):
        self._seed(_project())
        r = self.client.get('/api/projects/phases/where-are-we::nope/thread', headers=READ)
        self.assertEqual(r.status_code, 404, r.text)

    def test_thread_empty_when_supabase_unavailable(self):
        self._seed(_project())
        r = self.client.get(f'/api/projects/phases/{self.REF}/thread', headers=READ)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['phase_ref'], self.REF)
        self.assertEqual(r.json()['messages'], [])

    def test_message_404_missing_phase(self):
        self._seed(_project())
        r = self.client.post('/api/projects/phases/where-are-we::nope/message',
                             headers=AUTH, json={'text': 'hi'})
        self.assertEqual(r.status_code, 404, r.text)

    def test_message_503_when_supabase_unavailable(self):
        # The phase resolves; the durable-message core refuses (503) with no
        # supabase — proving the phase-specific wiring reached the shared core.
        self._seed(_project())
        r = self.client.post(f'/api/projects/phases/{self.REF}/message',
                             headers=AUTH, json={'text': 'a question for the team'})
        self.assertEqual(r.status_code, 503, r.text)

    def test_composite_ref_resolves_precisely_across_slug_collision(self):
        # Two active projects whose phases share the slug 'dup'. The composite ref
        # must resolve to the named project's phase, not the first match — so chat
        # never mis-routes (the #review finding).
        self._seed(
            _project('proj-a', phases=[_phase('dup', title='A copy')]),
            _project('proj-b', phases=[_phase('dup', title='B copy')]),
        )
        # proj-b's ref resolves (200 thread); a present-but-wrong project still 404s
        # only when the pair doesn't exist.
        ok = self.client.get('/api/projects/phases/proj-b::dup/thread', headers=READ)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()['phase_ref'], 'proj-b::dup')
        missing = self.client.get('/api/projects/phases/proj-c::dup/thread', headers=READ)
        self.assertEqual(missing.status_code, 404, missing.text)


if __name__ == '__main__':
    unittest.main()
