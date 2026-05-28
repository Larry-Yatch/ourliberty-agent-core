#!/usr/bin/env python3
"""Tests for the /api/system/missions endpoints (E4.4f PR-A).

Spec: `agents/beacon/specs/e4-4f-missions-tab-v1.md` § 5.1 (schema),
§ 5.5 (+ New mission flow), § 5.8 (droplet endpoint).

Path-isolation pattern mirrors `test_dashboard_api_build_sequences.py`:
each test owns a fresh tmpdir; `da._missions_json_path` is rebound onto
that tmpdir's missions.json so live `agents/beacon/missions.json` is
never touched. All GitHub REST calls are intercepted via a recording
stub installed onto `da._github_api_request`; no live HTTPS is made.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_missions
"""
from __future__ import annotations

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

AUTH = {'X-Dashboard-Token': TOKEN}
GET_ENDPOINT = '/api/system/missions'
POST_ENDPOINT = '/api/system/missions/new'


# ---------- fixture helpers ----------


def _seed_registry(missions_path: Path, entries: list[dict]) -> None:
    missions_path.parent.mkdir(parents=True, exist_ok=True)
    missions_path.write_text(
        json.dumps({'schema_version': 1, 'missions': entries}, indent=2) + '\n',
    )


def _seed_two_missions(missions_path: Path) -> None:
    _seed_registry(missions_path, [
        {
            'id': 'missions-tab-v1',
            'name': 'Missions Tab v1',
            'phase': 'drafting',
            'brief': 'Kanban surface for technical multi-PR initiatives.',
            'spec_docs': ['agents/beacon/specs/e4-4f-missions-tab-v1.md'],
            'task_ids': ['e4-4f-missions-tab-v1'],
            'repo': 'ourliberty-dashboard',
            'created': '2026-05-28',
            'deferred_reason': None,
        },
        {
            'id': 'e4-4b-projects-kanban',
            'name': 'Programs Kanban (drag-drop projects)',
            'phase': 'deferred',
            'brief': 'Switch Programs view to kanban + drag-drop per E4.4b spec.',
            'spec_docs': ['agents/beacon/specs/e4-4-dashboard-ui-rebuild.md#e44b'],
            'task_ids': [],
            'repo': 'ourliberty-dashboard',
            'created': '2026-05-24',
            'deferred_reason': 'Prioritized Missions tab first per 2026-05-28 design pass',
        },
    ])


class _FakeResponse:
    """Minimal httpx-Response-shaped recorder."""

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _GithubRecorder:
    """Records every _github_api_request call; returns scripted responses
    from the per-(method, url-suffix) queue. URL-suffix match is endswith
    so test setup can ignore the `https://api.github.com/repos/owner/repo`
    prefix."""

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


class _MissionsTestBase(unittest.TestCase):
    """Shared scaffold for GET + POST tests."""

    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        # GITHUB_TOKEN required only for POST tests, but harmless to set
        # globally. Save+restore so we don't pollute other test modules.
        self._prev_gh_token = os.environ.get('GITHUB_TOKEN')
        os.environ['GITHUB_TOKEN'] = 'test-gh-token'
        self._prev_repo = os.environ.get('OURLIBERTY_MISSIONS_REPO')
        os.environ['OURLIBERTY_MISSIONS_REPO'] = 'test-owner/test-repo'

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-missions-'))
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'

        # Rebind module-level helpers onto our tmpdir + GH recorder.
        self._orig_missions_path = da._missions_json_path
        self._orig_github = da._github_api_request
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        da._github_api_request = self._orig_github  # type: ignore[assignment]
        if self._prev_gh_token is None:
            os.environ.pop('GITHUB_TOKEN', None)
        else:
            os.environ['GITHUB_TOKEN'] = self._prev_gh_token
        if self._prev_repo is None:
            os.environ.pop('OURLIBERTY_MISSIONS_REPO', None)
        else:
            os.environ['OURLIBERTY_MISSIONS_REPO'] = self._prev_repo


# ==================== GET /api/system/missions ====================


class GetMissionsAuthTest(_MissionsTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.get(GET_ENDPOINT)
        self.assertEqual(r.status_code, 401)

    def test_bad_token_returns_401(self):
        r = self.client.get(GET_ENDPOINT, headers={'X-Dashboard-Token': 'nope'})
        self.assertEqual(r.status_code, 401)


class GetMissionsHappyPathTest(_MissionsTestBase):
    def test_returns_seeded_entries_with_timestamp(self):
        _seed_two_missions(self.missions_path)
        r = self.client.get(GET_ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body['missions']), 2)
        ids = {m['id'] for m in body['missions']}
        self.assertEqual(ids, {'missions-tab-v1', 'e4-4b-projects-kanban'})
        # last_synced_at reflects mtime, not server time.
        self.assertIsNotNone(body['last_synced_at'])
        self.assertEqual(body['schema_version'], 1)

    def test_passes_through_full_entry_shape(self):
        _seed_two_missions(self.missions_path)
        body = self.client.get(GET_ENDPOINT, headers=AUTH).json()
        entry = next(m for m in body['missions'] if m['id'] == 'missions-tab-v1')
        for field in (
            'id', 'name', 'phase', 'brief', 'spec_docs', 'task_ids',
            'repo', 'created', 'deferred_reason',
        ):
            self.assertIn(field, entry)


class GetMissionsMissingFileTest(_MissionsTestBase):
    def test_missing_file_returns_empty_defensive_default(self):
        # Don't seed; missions_path doesn't exist.
        r = self.client.get(GET_ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['missions'], [])
        self.assertIsNone(body['last_synced_at'])
        self.assertIsNone(body['schema_version'])


class GetMissionsMalformedTest(_MissionsTestBase):
    def test_malformed_json_returns_500_with_clean_body(self):
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text('{not valid json')
        r = self.client.get(GET_ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 500)
        body = r.json()
        # Structured body, NOT a Flask/FastAPI stack trace.
        self.assertIn('detail', body)
        self.assertEqual(body['detail']['error'], 'missions.json malformed')
        self.assertIn('detail', body['detail'])
        # Single line, not a multi-line traceback.
        self.assertNotIn('\n', body['detail']['detail'])

    def test_top_level_not_object_returns_500(self):
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text('[]')
        r = self.client.get(GET_ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(
            r.json()['detail']['error'], 'missions.json malformed',
        )


# ==================== POST /api/system/missions/new ====================


def _happy_path_github_script(
    gh: _GithubRecorder,
    *,
    branch: str = 'feat/new-mission-shiny-thing',
    pr_url: str = 'https://github.com/test-owner/test-repo/pull/9999',
) -> None:
    """Script the four expected GitHub calls for a successful POST."""
    gh.script('GET', '/git/refs/heads/main', _FakeResponse(
        200, {'object': {'sha': 'abc123main'}},
    ))
    gh.script('POST', '/git/refs', _FakeResponse(
        201, {'ref': f'refs/heads/{branch}'},
    ))
    gh.script(
        'GET',
        f'/contents/agents/beacon/missions.json?ref={branch}',
        _FakeResponse(200, {'sha': 'blob-sha'}),
    )
    gh.script(
        'PUT', '/contents/agents/beacon/missions.json',
        _FakeResponse(200, {'content': {'sha': 'new-blob-sha'}}),
    )
    gh.script('POST', '/pulls', _FakeResponse(
        201, {'html_url': pr_url, 'number': 9999},
    ))


class PostMissionsAuthTest(_MissionsTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(POST_ENDPOINT, json={
            'name': 'Shiny Thing', 'brief': 'A new shiny mission.',
            'repo': 'ourliberty-dashboard',
        })
        self.assertEqual(r.status_code, 401)
        # No GitHub calls should have been made on auth failure.
        self.assertEqual(self.gh.calls, [])


class PostMissionsHappyPathTest(_MissionsTestBase):
    def test_creates_pr_and_returns_url(self):
        _seed_two_missions(self.missions_path)
        _happy_path_github_script(self.gh)
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Shiny Thing',
            'brief': 'A new shiny mission.',
            'repo': 'ourliberty-dashboard',
            'spec_docs': ['agents/beacon/specs/shiny.md'],
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['mission_id'], 'shiny-thing')
        self.assertEqual(body['branch'], 'feat/new-mission-shiny-thing')
        self.assertEqual(
            body['pr_url'],
            'https://github.com/test-owner/test-repo/pull/9999',
        )
        # All five expected GH calls fired in order.
        self.assertEqual(len(self.gh.calls), 5)
        # The PUT body should include the new entry in the registry.
        put_call = self.gh.calls[3]
        self.assertEqual(put_call['method'], 'PUT')
        self.assertEqual(
            put_call['json_body']['branch'],
            'feat/new-mission-shiny-thing',
        )
        import base64
        new_text = base64.b64decode(
            put_call['json_body']['content'],
        ).decode('utf-8')
        updated = json.loads(new_text)
        self.assertEqual(updated['schema_version'], 1)
        ids = {m['id'] for m in updated['missions']}
        self.assertEqual(
            ids,
            {'missions-tab-v1', 'e4-4b-projects-kanban', 'shiny-thing'},
        )

    def test_authorization_header_is_bearer_github_token(self):
        _seed_two_missions(self.missions_path)
        _happy_path_github_script(self.gh)
        self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Shiny Thing', 'brief': 'b', 'repo': 'r',
        })
        # Every GH call carries the Bearer token from GITHUB_TOKEN env.
        for call in self.gh.calls:
            self.assertEqual(
                call['headers']['Authorization'], 'Bearer test-gh-token',
            )

    def test_local_missions_json_not_mutated(self):
        # The POST flow opens a PR via GitHub REST; the local file is
        # NOT touched (drift-vs-main avoidance).
        _seed_two_missions(self.missions_path)
        before = self.missions_path.read_text()
        _happy_path_github_script(self.gh)
        self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Shiny Thing', 'brief': 'b', 'repo': 'r',
        })
        after = self.missions_path.read_text()
        self.assertEqual(before, after)


class PostMissionsDupIdTest(_MissionsTestBase):
    def test_local_dup_id_returns_409_no_github_calls(self):
        _seed_two_missions(self.missions_path)
        # 'Missions Tab V1' kebabs to 'missions-tab-v1' — matches seed.
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Missions Tab V1',
            'brief': 'dup',
            'repo': 'ourliberty-dashboard',
        })
        self.assertEqual(r.status_code, 409, r.text)
        body = r.json()
        self.assertEqual(body['detail']['error'], 'mission_id collision')
        self.assertEqual(body['detail']['id'], 'missions-tab-v1')
        self.assertIn('existing_entry_brief', body['detail'])
        # CRITICAL: no GitHub calls on local dup-id detection.
        self.assertEqual(self.gh.calls, [])


class PostMissionsRemoteBranchExistsTest(_MissionsTestBase):
    def test_422_on_branch_creation_returns_409(self):
        # No local seed → local dup check passes. GitHub reports the
        # branch already exists (422) — we must map to 409.
        self.gh.script('GET', '/git/refs/heads/main', _FakeResponse(
            200, {'object': {'sha': 'abc123main'}},
        ))
        self.gh.script('POST', '/git/refs', _FakeResponse(
            422, {'message': 'Reference already exists'},
        ))
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Shiny Thing', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 409, r.text)
        body = r.json()
        self.assertEqual(body['detail']['error'], 'branch_exists')
        self.assertEqual(
            body['detail']['branch'], 'feat/new-mission-shiny-thing',
        )


class PostMissionsBodyValidationTest(_MissionsTestBase):
    def test_missing_name_returns_422(self):
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'brief': 'b', 'repo': 'r',
        })
        # FastAPI's Pydantic validation returns 422 for missing required.
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.gh.calls, [])

    def test_empty_brief_returns_422(self):
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'X', 'brief': '', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.gh.calls, [])

    def test_name_kebabs_to_empty_returns_400(self):
        # min_length=1 prevents the empty literal; this case is a name
        # of only non-alphanumerics, which Pydantic accepts but our
        # handler rejects after kebab.
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': '!!!', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(
            r.json()['detail']['error'], 'invalid mission name',
        )
        self.assertEqual(self.gh.calls, [])


class PostMissionsGithubFailureTest(_MissionsTestBase):
    def test_get_main_ref_fails_returns_502(self):
        self.gh.script('GET', '/git/refs/heads/main', _FakeResponse(
            500, {'message': 'internal'},
        ))
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'X', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(
            r.json()['detail']['error'], 'github get main ref failed',
        )

    def test_pr_create_fails_returns_502(self):
        self.gh.script('GET', '/git/refs/heads/main', _FakeResponse(
            200, {'object': {'sha': 'abc'}},
        ))
        self.gh.script('POST', '/git/refs', _FakeResponse(201, {}))
        self.gh.script(
            'GET', '/contents/agents/beacon/missions.json?ref=feat/new-mission-x',
            _FakeResponse(404, {}),
        )
        self.gh.script(
            'PUT', '/contents/agents/beacon/missions.json',
            _FakeResponse(201, {}),
        )
        self.gh.script('POST', '/pulls', _FakeResponse(
            500, {'message': 'gateway'},
        ))
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'X', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(
            r.json()['detail']['error'], 'github create pr failed',
        )


class PostMissionsTokenMissingTest(_MissionsTestBase):
    def test_missing_github_token_returns_500(self):
        # The fixture sets GITHUB_TOKEN; remove it for this test only.
        os.environ.pop('GITHUB_TOKEN', None)
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'X', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(
            r.json()['detail']['error'], 'github token missing',
        )
        self.assertEqual(self.gh.calls, [])


# ==================== kebab helper ====================


class KebabCaseTest(unittest.TestCase):
    def test_canonical_forms(self):
        self.assertEqual(da._kebab_case('Missions Tab V1'), 'missions-tab-v1')
        self.assertEqual(da._kebab_case('  Shiny  Thing  '), 'shiny-thing')
        self.assertEqual(da._kebab_case('FOO_bar-Baz'), 'foo-bar-baz')
        self.assertEqual(da._kebab_case('!!!'), '')
        self.assertEqual(da._kebab_case('e4.4f Missions'), 'e4-4f-missions')


if __name__ == '__main__':
    unittest.main()
