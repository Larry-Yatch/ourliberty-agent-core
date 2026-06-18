#!/usr/bin/env python3
"""Tests for the phase-transition write endpoints (projects-v3 P3 follow-up,
p3f-phase-transitions, spec § 0 / § 6 step 1):

  * POST /api/projects/advance      — the Brainstorm→Spec checkpoint ("Ready to
    spec"), advancing a phase one forward lifecycle step via
    `projects_store.next_lifecycle_state` + `can_transition`.
  * POST /api/projects/attach-spec  — point a Spec-stage phase at its authored
    spec doc (set `spec_ref` → spec-ready). A non-existent spec path is rejected
    loudly (spec § 4 guardrail), never written.

Both endpoints are NON-committers: they rewrite projects.json ON DISK and rely on
`heal_projects_store.py` (the SOLE committer) to commit the delta. A correct run
therefore opens NO PR / makes NO github call — the recorder raises on any attempt
— while the on-disk file DOES change (the lifecycle bump / spec_ref). An invalid
request leaves projects.json byte-identical.

Path-isolation mirrors test_dashboard_api_launch_build.py: each test owns a fresh
tmpdir with the projects path AND the repo root rebound onto it, so spec-doc
existence checks resolve inside the tmp tree.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_phase_transitions
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

ADVANCE = '/api/projects/advance'
ATTACH = '/api/projects/attach-spec'
ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}

SPEC_REL = 'agents/beacon/specs/aging-idea.md'


def _phase(phid='aging-idea', *, title='Aging Idea',
           lifecycle_state='brainstorm', spec_ref=None,
           desired_end_state='ship the thing', sequence_ref=None, **extra):
    p = {
        'id': phid,
        'title': title,
        'desired_end_state': desired_end_state,
        'lifecycle_state': lifecycle_state,
        'order': 0,
        'spec_ref': spec_ref,
        'sequence_ref': sequence_ref,
    }
    p.update(extra)
    return p


def _project(pid='aging-idea', *, state='active', repo='ourliberty-agent-core',
             phases=None, **extra):
    proj = {
        'id': pid,
        'title': pid,
        'repo': repo,
        'state': state,
        'phases': phases if phases is not None else [_phase()],
    }
    proj.update(extra)
    return proj


class _GithubRecorder:
    """A phase transition opens no PR — any github call is a regression."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({'method': method, 'url': url})
        raise AssertionError(
            f'unexpected github call: {method} {url} — transitions open no PR')


class _PhaseTransitionTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-phase-transitions-'))
        self.projects_path = self.tmp / 'agents' / 'beacon' / 'projects.json'

        self._orig_projects_path = da._projects_json_path
        self._orig_repo_root = da._repo_root
        self._orig_github = da._github_api_request
        self._orig_la_client = da._get_larry_action_supabase_client
        da._projects_json_path = lambda: self.projects_path
        # Rebind the repo root onto the tmp tree so spec-doc existence checks
        # resolve a file we control, not the real checkout.
        da._repo_root = lambda: self.tmp
        da._get_larry_action_supabase_client = lambda: None
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh

        self.client = TestClient(da.app)

    def tearDown(self):
        da._projects_json_path = self._orig_projects_path
        da._repo_root = self._orig_repo_root
        da._github_api_request = self._orig_github
        da._get_larry_action_supabase_client = self._orig_la_client

    def _seed_projects(self, *projects) -> None:
        self.projects_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_path.write_text(
            json.dumps({'schema_version': 1, 'projects': list(projects)},
                       indent=2) + '\n')

    def _seed_spec_doc(self, rel=SPEC_REL, body='# spec\n') -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def _phase_on_disk(self, project_id='aging-idea', phase_id='aging-idea') -> dict:
        data = json.loads(self.projects_path.read_text())
        for proj in data['projects']:
            if proj['id'] == project_id:
                for ph in proj['phases']:
                    if ph['id'] == phase_id:
                        return ph
        raise AssertionError(f'phase {project_id}/{phase_id} not on disk')


# ==================== auth ====================


class AuthTest(_PhaseTransitionTestBase):
    def test_advance_missing_token_returns_401(self):
        r = self.client.post(ADVANCE, headers={'X-Actor': ACTOR},
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_advance_missing_actor_returns_401(self):
        self._seed_projects(_project())
        r = self.client.post(ADVANCE, headers={'X-Dashboard-Token': TOKEN},
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 401)

    def test_attach_missing_actor_returns_401(self):
        self._seed_projects(_project())
        r = self.client.post(ATTACH, headers={'X-Dashboard-Token': TOKEN},
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea',
                                   'spec_ref': SPEC_REL})
        self.assertEqual(r.status_code, 401)


# ==================== request validation ====================


class ValidationTest(_PhaseTransitionTestBase):
    def test_advance_blank_phase_id_rejected_422(self):
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea', 'phase_id': ''})
        self.assertEqual(r.status_code, 422)

    def test_attach_blank_spec_ref_rejected_422(self):
        r = self.client.post(ATTACH, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea', 'spec_ref': ''})
        self.assertEqual(r.status_code, 422)

    def test_advance_unknown_phase_returns_404(self):
        self._seed_projects(_project())
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea', 'phase_id': 'nope'})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.gh.calls, [])

    def test_advance_archived_project_returns_404(self):
        self._seed_projects(_project(state='archived'))
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 404)


# ==================== advance: Brainstorm→Spec ====================


class AdvanceTest(_PhaseTransitionTestBase):
    def test_brainstorm_advances_to_spec_and_does_not_commit(self):
        self._seed_projects(_project())
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'advanced')
        self.assertEqual(body['from_state'], 'brainstorm')
        self.assertEqual(body['to_state'], 'spec')

        # The bump landed on disk (non-committer: no github call).
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self._phase_on_disk()['lifecycle_state'], 'spec')

    def test_advance_stamps_updated_at(self):
        self._seed_projects(_project())
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('updated_at', self._phase_on_disk())

    def test_advance_from_spec_rejected_409_no_write(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='spec')]))
        before = self.projects_path.read_text()
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['detail']['error'],
                         'phase not at brainstorm checkpoint')
        # Not this endpoint's transition — projects.json untouched.
        self.assertEqual(self.projects_path.read_text(), before)

    def test_advance_from_building_rejected_409(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='building')]))
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 409)

    def test_advance_from_done_rejected_409(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='done')]))
        r = self.client.post(ADVANCE, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 409)

    def test_advance_twice_second_is_409(self):
        self._seed_projects(_project())
        first = self.client.post(ADVANCE, headers=AUTH,
                                 json={'project_id': 'aging-idea',
                                       'phase_id': 'aging-idea'})
        self.assertEqual(first.status_code, 200)
        second = self.client.post(ADVANCE, headers=AUTH,
                                  json={'project_id': 'aging-idea',
                                        'phase_id': 'aging-idea'})
        self.assertEqual(second.status_code, 409)
        # Still at spec — no further advance.
        self.assertEqual(self._phase_on_disk()['lifecycle_state'], 'spec')


# ==================== attach-spec ====================


class AttachSpecTest(_PhaseTransitionTestBase):
    def test_attach_existing_spec_sets_ref_and_does_not_commit(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='spec')]))
        self._seed_spec_doc()
        r = self.client.post(ATTACH, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea',
                                   'spec_ref': SPEC_REL})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'spec-attached')
        self.assertEqual(body['spec_ref'], SPEC_REL)
        self.assertEqual(body['lifecycle_state'], 'spec')

        # spec_ref landed on disk; non-committer (no github call).
        self.assertEqual(self.gh.calls, [])
        ph = self._phase_on_disk()
        self.assertEqual(ph['spec_ref'], SPEC_REL)
        self.assertIn('updated_at', ph)

    def test_attach_nonexistent_spec_rejected_400_no_write(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='spec')]))
        # No spec doc seeded → the path doesn't exist.
        before = self.projects_path.read_text()
        r = self.client.post(ATTACH, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea',
                                   'spec_ref': SPEC_REL})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['detail']['error'], 'spec doc not found')
        # Fail loudly, never create an un-launchable spec-ready phase.
        self.assertEqual(self.projects_path.read_text(), before)
        self.assertIsNone(self._phase_on_disk()['spec_ref'])

    def test_attach_path_traversal_rejected_400(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='spec')]))
        r = self.client.post(ATTACH, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea',
                                   'spec_ref': '../../../etc/passwd'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['detail']['error'], 'spec doc not found')

    def test_attach_directory_not_file_rejected_400(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='spec')]))
        (self.tmp / 'agents' / 'beacon' / 'specs').mkdir(parents=True, exist_ok=True)
        r = self.client.post(ATTACH, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea',
                                   'spec_ref': 'agents/beacon/specs'})
        self.assertEqual(r.status_code, 400)

    def test_attach_to_brainstorm_phase_rejected_409(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='brainstorm')]))
        self._seed_spec_doc()
        before = self.projects_path.read_text()
        r = self.client.post(ATTACH, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea',
                                   'spec_ref': SPEC_REL})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['detail']['error'], 'phase not at spec stage')
        self.assertEqual(self.projects_path.read_text(), before)

    def test_attach_unknown_phase_returns_404(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='spec')]))
        self._seed_spec_doc()
        r = self.client.post(ATTACH, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'nope', 'spec_ref': SPEC_REL})
        self.assertEqual(r.status_code, 404)

    def test_attach_then_phase_is_launch_ready(self):
        # After advance + attach, the phase has spec_ref at lifecycle 'spec' —
        # exactly the spec-ready shape the Launch endpoint requires.
        self._seed_projects(_project())
        self._seed_spec_doc()
        adv = self.client.post(ADVANCE, headers=AUTH,
                               json={'project_id': 'aging-idea',
                                     'phase_id': 'aging-idea'})
        self.assertEqual(adv.status_code, 200)
        att = self.client.post(ATTACH, headers=AUTH,
                               json={'project_id': 'aging-idea',
                                     'phase_id': 'aging-idea',
                                     'spec_ref': SPEC_REL})
        self.assertEqual(att.status_code, 200)
        ph = self._phase_on_disk()
        self.assertEqual(ph['lifecycle_state'], 'spec')
        self.assertEqual(ph['spec_ref'], SPEC_REL)


if __name__ == '__main__':
    unittest.main()
