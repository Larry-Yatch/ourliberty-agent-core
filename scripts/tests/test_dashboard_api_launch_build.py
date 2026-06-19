#!/usr/bin/env python3
"""Tests for POST /api/projects/launch — the dashboard "Launch build" gate
(projects-v3 P3, p3-launch-queue-drain, spec § 7 step 3).

The endpoint is a NON-committer: it reads projects.json (read-only), and on a
spec-ready phase drops a single launch-request file under
`<queue_dir>/<phase_id>.json` for the Beacon-side drain to author a build from.
It opens NO PR, makes NO github call, and NEVER writes projects.json — so a
correct run leaves `da._github_api_request` uncalled and projects.json
byte-identical. The drain's deterministic `launch-<phase_id>` sequence-file
existence is the durable idempotency backstop; the endpoint's own 409 only
catches a rapid double-click whose first request hasn't drained yet.

Path-isolation mirrors test_dashboard_api_funnel_promote.py.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_launch_build
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

ENDPOINT = '/api/projects/launch'
ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}


def _phase(phid='aging-idea', *, title='Aging Idea',
           spec_ref='agents/beacon/specs/aging-idea.md',
           desired_end_state='ship the thing', lifecycle_state='spec_ready',
           sequence_ref=None, **extra):
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
    """Launching a build opens no PR — any github call is a regression."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({'method': method, 'url': url})
        raise AssertionError(
            f'unexpected github call: {method} {url} — launch opens no PR')


class _LaunchBuildTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-launch-build-'))
        self.projects_path = self.tmp / 'agents' / 'beacon' / 'projects.json'
        self.queue_dir = self.tmp / 'agents' / 'blackboard' / 'build-launch-queue'

        self._orig_projects_path = da._projects_json_path
        self._orig_queue_dir = da._build_launch_queue_dir
        self._orig_github = da._github_api_request
        self._orig_la_client = da._get_larry_action_supabase_client
        da._projects_json_path = lambda: self.projects_path
        da._build_launch_queue_dir = lambda: self.queue_dir
        da._get_larry_action_supabase_client = lambda: None
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh

        self.client = TestClient(da.app)

    def tearDown(self):
        da._projects_json_path = self._orig_projects_path
        da._build_launch_queue_dir = self._orig_queue_dir
        da._github_api_request = self._orig_github
        da._get_larry_action_supabase_client = self._orig_la_client

    def _seed_projects(self, *projects) -> None:
        self.projects_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_path.write_text(
            json.dumps({'schema_version': 1, 'projects': list(projects)},
                       indent=2) + '\n')

    def _queue_files(self) -> list[Path]:
        if not self.queue_dir.is_dir():
            return []
        return [p for p in self.queue_dir.iterdir()
                if p.is_file() and p.suffix == '.json']

    def _read_queue(self, phase_id='aging-idea') -> dict:
        return json.loads((self.queue_dir / f'{phase_id}.json').read_text())


# ==================== auth ====================


class AuthTest(_LaunchBuildTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(ENDPOINT, headers={'X-Actor': ACTOR},
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self._queue_files(), [])

    def test_missing_actor_returns_401(self):
        self._seed_projects(_project())
        r = self.client.post(ENDPOINT, headers={'X-Dashboard-Token': TOKEN},
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self._queue_files(), [])


# ==================== validation ====================


class ValidationTest(_LaunchBuildTestBase):
    def test_blank_phase_id_rejected_422(self):
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea', 'phase_id': ''})
        self.assertEqual(r.status_code, 422)

    def test_unknown_phase_returns_404_no_queue(self):
        self._seed_projects(_project())
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'nope'})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._queue_files(), [])
        self.assertEqual(self.gh.calls, [])

    def test_archived_project_not_launchable_404(self):
        self._seed_projects(_project(state='archived'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._queue_files(), [])

    def test_phase_without_spec_ref_returns_409(self):
        self._seed_projects(_project(phases=[_phase(spec_ref=None)]))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['detail']['error'], 'phase not spec-ready')
        self.assertEqual(self._queue_files(), [])

    def test_phase_with_sequence_ref_returns_409(self):
        self._seed_projects(_project(phases=[_phase(sequence_ref='launch-aging-idea')]))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['detail']['error'], 'phase already launched')
        self.assertEqual(self._queue_files(), [])

    def test_phase_already_building_returns_409(self):
        self._seed_projects(_project(phases=[_phase(lifecycle_state='building')]))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._queue_files(), [])


# ==================== happy path / non-committer ====================


class LaunchTest(_LaunchBuildTestBase):
    def test_spec_ready_phase_queues_and_does_not_commit(self):
        self._seed_projects(_project())
        before = self.projects_path.read_text()

        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'queued')
        self.assertEqual(body['phase_id'], 'aging-idea')
        self.assertEqual(body['project_id'], 'aging-idea')
        self.assertEqual(body['seq_id'], 'launch-aging-idea')

        # NO PR, NO projects.json mutation.
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.projects_path.read_text(), before)

        # A single queue file the drain authors the build from.
        self.assertEqual(len(self._queue_files()), 1)
        entry = self._read_queue('aging-idea')
        self.assertEqual(entry['phase_id'], 'aging-idea')
        self.assertEqual(entry['seq_id'], 'launch-aging-idea')
        self.assertEqual(entry['spec_ref'], 'agents/beacon/specs/aging-idea.md')
        self.assertEqual(entry['repo'], 'ourliberty-agent-core')
        self.assertEqual(entry['requested_by'], ACTOR)

    def test_phase_inherits_project_repo_when_phase_has_none(self):
        self._seed_projects(_project(repo='ourliberty-dashboard'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._read_queue('aging-idea')['repo'],
                         'ourliberty-dashboard')

    def test_double_click_before_drain_returns_409(self):
        self._seed_projects(_project())
        first = self.client.post(ENDPOINT, headers=AUTH,
                                 json={'project_id': 'aging-idea',
                                       'phase_id': 'aging-idea'})
        self.assertEqual(first.status_code, 200)
        second = self.client.post(ENDPOINT, headers=AUTH,
                                  json={'project_id': 'aging-idea',
                                        'phase_id': 'aging-idea'})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()['detail']['error'], 'phase launch queued')
        # Still exactly one queue file.
        self.assertEqual(len(self._queue_files()), 1)
        self.assertEqual(self.gh.calls, [])


# ==================== repo resolution (Bug 1) ====================


class RepoResolutionTest(_LaunchBuildTestBase):
    """A bad/missing build repo (e.g. `ol-work` inherited from a capture origin,
    dropped to None at promote) must never ride to an unbuildable dispatch: a
    valid repo passes through, anything else is rejected LOUDLY (422). We never
    derive the repo from the spec — every spec lives in agent-core regardless of
    the build target, so spec location carries no target-repo signal."""

    def setUp(self):
        super().setUp()
        # Stand-in config so validation is deterministic, not dependent on the
        # live droplet config.
        self.models_path = self.tmp / 'config' / 'agent-models.json'
        self.models_path.parent.mkdir(parents=True, exist_ok=True)
        self.models_path.write_text(json.dumps({'repo_paths': {
            'ourliberty-agent-core': '/home/larry/agent-core',
            'ourliberty-dashboard': '/home/larry/ourliberty-dashboard',
        }}))
        self._orig_models = da._agent_models_json_path
        da._agent_models_json_path = lambda: self.models_path

    def tearDown(self):
        da._agent_models_json_path = self._orig_models
        super().tearDown()

    def test_bogus_repo_rejected_422_no_queue(self):
        # Project repo is a working-dir name (`ol-work`) → loud rejection, no
        # queue file, no silent bad dispatch.
        self._seed_projects(_project(repo='ol-work'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 422)
        detail = r.json()['detail']
        self.assertEqual(detail['error'], 'unbuildable target repo')
        self.assertEqual(detail['target_repo'], 'ol-work')
        self.assertIn('ourliberty-agent-core', detail['valid_repos'])
        self.assertEqual(self._queue_files(), [])
        self.assertEqual(self.gh.calls, [])

    def test_missing_repo_rejected_422(self):
        # Promote drops a bogus repo to None; with no buildable repo the launch
        # rejects (the user must set the real target repo) — never guesses.
        self._seed_projects(_project(repo=None, phases=[_phase()]))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()['detail']['error'], 'unbuildable target repo')
        self.assertEqual(self._queue_files(), [])

    def test_valid_repo_passes_through(self):
        self._seed_projects(_project(repo='ourliberty-dashboard'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._read_queue('aging-idea')['repo'],
                         'ourliberty-dashboard')

    def test_unreadable_config_fails_open(self):
        # Config missing → can't validate → fail open: the candidate is used
        # as-is (never block a launch over a transient config read miss).
        da._agent_models_json_path = lambda: self.tmp / 'config' / 'does-not-exist.json'
        self._seed_projects(_project(repo='ol-work'))
        r = self.client.post(ENDPOINT, headers=AUTH,
                             json={'project_id': 'aging-idea',
                                   'phase_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._read_queue('aging-idea')['repo'], 'ol-work')


if __name__ == '__main__':
    unittest.main()
