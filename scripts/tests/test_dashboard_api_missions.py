#!/usr/bin/env python3
"""Tests for the /api/system/missions endpoints (E4.4f PR-A).

Spec: `agents/beacon/specs/e4-4f-missions-tab-v1.md` § 5.1 (schema),
§ 5.5 (+ New mission flow), § 5.8 (droplet endpoint).

Path-isolation pattern mirrors `test_dashboard_api_build_sequences.py`:
each test owns a fresh tmpdir; `da._missions_json_path` and
`da._new_mission_queue_dir` are rebound onto that tmpdir so the live
`agents/beacon/missions.json` and the real queue dir are never touched.
The +New mission flow (§ 5.5) no longer opens a PR — it queues a file for
the missions writer (heal_orphan_autoregister) to drain — so POST makes no
GitHub calls; the recording stub on `da._github_api_request` doubles as a
guard that proves zero GitHub I/O. No live HTTPS is made.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_missions
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
        self.queue_dir = self.tmp / 'agents' / 'blackboard' / 'new-mission-queue'

        # Rebind module-level helpers onto our tmpdir + GH recorder. The +New
        # mission flow makes NO GitHub calls now (it queues a file), so the
        # recorder doubles as a guard: any GH call raises, proving zero I/O.
        self._orig_missions_path = da._missions_json_path
        self._orig_queue_dir = da._new_mission_queue_dir
        self._orig_github = da._github_api_request
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        da._new_mission_queue_dir = lambda: self.queue_dir  # type: ignore[assignment]
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def _queued_ids(self) -> set:
        """Mission ids currently sitting in the queue dir (by filename)."""
        if not self.queue_dir.is_dir():
            return set()
        return {p.stem for p in self.queue_dir.iterdir() if p.suffix == '.json'}

    def tearDown(self):
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        da._new_mission_queue_dir = self._orig_queue_dir  # type: ignore[assignment]
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


class PostMissionsAuthTest(_MissionsTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(POST_ENDPOINT, json={
            'name': 'Shiny Thing', 'brief': 'A new shiny mission.',
            'repo': 'ourliberty-dashboard',
        })
        self.assertEqual(r.status_code, 401)
        # No queue file + no GitHub calls on auth failure.
        self.assertEqual(self._queued_ids(), set())
        self.assertEqual(self.gh.calls, [])


class PostMissionsHappyPathTest(_MissionsTestBase):
    def test_queues_mission_and_returns_status(self):
        _seed_two_missions(self.missions_path)
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Shiny Thing',
            'brief': 'A new shiny mission.',
            'repo': 'ourliberty-dashboard',
            'spec_docs': ['agents/beacon/specs/shiny.md'],
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['mission_id'], 'shiny-thing')
        self.assertEqual(body['status'], 'queued')
        # No PR is created → no pr_url/branch (optional, absent → null).
        self.assertIsNone(body.get('pr_url'))
        self.assertIsNone(body.get('branch'))
        # CRITICAL: the +New mission flow makes ZERO GitHub calls now.
        self.assertEqual(self.gh.calls, [])
        # The full mission entry is written to the queue, shaped like a
        # registry entry, for the healer to drain.
        queue_file = self.queue_dir / 'shiny-thing.json'
        self.assertTrue(queue_file.is_file())
        entry = json.loads(queue_file.read_text())
        self.assertEqual(entry['id'], 'shiny-thing')
        self.assertEqual(entry['name'], 'Shiny Thing')
        self.assertEqual(entry['phase'], 'drafting')
        self.assertEqual(entry['brief'], 'A new shiny mission.')
        self.assertEqual(entry['spec_docs'], ['agents/beacon/specs/shiny.md'])
        self.assertEqual(entry['task_ids'], [])
        self.assertEqual(entry['repo'], 'ourliberty-dashboard')
        self.assertIsNone(entry['deferred_reason'])
        self.assertIn('created', entry)

    def test_no_github_token_still_queues(self):
        # The queue flow needs no GitHub token — removing it must NOT block
        # registration (proves the GitHub dependency is fully gone).
        os.environ.pop('GITHUB_TOKEN', None)
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'No Token Mission', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['status'], 'queued')
        self.assertIn('no-token-mission', self._queued_ids())
        self.assertEqual(self.gh.calls, [])

    def test_local_missions_json_not_mutated(self):
        # The dashboard is NOT a git committer: queueing must leave the local
        # missions.json byte-identical (the healer is the sole writer).
        _seed_two_missions(self.missions_path)
        before = self.missions_path.read_text()
        self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Shiny Thing', 'brief': 'b', 'repo': 'r',
        })
        after = self.missions_path.read_text()
        self.assertEqual(before, after)


class PostMissionsProposedTest(_MissionsTestBase):
    def test_proposed_card_stamps_provenance_and_keeps_predraft(self):
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Retro Bucket', 'brief': 'b', 'repo': 'r',
            'phase': 'proposed', 'proposed_by': 'retrospective-author',
            'predraft': {'template': 'route-to-hold', 'root_signature': 'medic::x'},
        })
        self.assertEqual(r.status_code, 200, r.text)
        entry = json.loads((self.queue_dir / 'retro-bucket.json').read_text())
        self.assertEqual(entry['phase'], 'proposed')
        self.assertEqual(entry['proposed_by'], 'retrospective-author')
        self.assertEqual(entry['predraft']['template'], 'route-to-hold')

    def test_oversized_predraft_rejected_400(self):
        # predraft lands verbatim in the auto-committed registry → bound it so a
        # buggy/compromised caller can't bloat missions.json (review #749 finding 5).
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Big Predraft', 'brief': 'b', 'repo': 'r',
            'phase': 'proposed', 'proposed_by': 'retrospective-author',
            'predraft': {'blob': 'x' * 9000},
        })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse((self.queue_dir / 'big-predraft.json').exists())


class NormalizeSuggestedSourceTest(unittest.TestCase):
    def test_retrospective_author_maps_to_pulse_lane(self):
        # Without this mapping the retrospective's curated cards normalize to None
        # and fall into the secondary orphan-clutter lane (review #749 finding 1).
        self.assertEqual(
            da._normalize_suggested_source('retrospective-author'), 'pulse')


class PostMissionsDupIdTest(_MissionsTestBase):
    def test_local_dup_id_returns_409_no_queue_write(self):
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
        # No queue file written + no GitHub calls on dup-id detection.
        self.assertEqual(self._queued_ids(), set())
        self.assertEqual(self.gh.calls, [])


class PostMissionsQueuedDupTest(_MissionsTestBase):
    def test_already_queued_id_returns_409(self):
        # A mission already sitting in the queue (not yet drained) → reject the
        # second submit as an in-flight dup rather than enqueue it twice.
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        (self.queue_dir / 'shiny-thing.json').write_text(
            json.dumps({'id': 'shiny-thing', 'name': 'Shiny Thing',
                        'phase': 'drafting', 'task_ids': []}) + '\n')
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Shiny Thing', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission_id queued')
        self.assertEqual(r.json()['detail']['id'], 'shiny-thing')
        self.assertEqual(self.gh.calls, [])


class PostMissionsBodyValidationTest(_MissionsTestBase):
    def test_missing_name_returns_422(self):
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'brief': 'b', 'repo': 'r',
        })
        # FastAPI's Pydantic validation returns 422 for missing required.
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self._queued_ids(), set())
        self.assertEqual(self.gh.calls, [])

    def test_empty_brief_returns_422(self):
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'X', 'brief': '', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self._queued_ids(), set())
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
        self.assertEqual(self._queued_ids(), set())
        self.assertEqual(self.gh.calls, [])


class PostMissionsQueueWriteFailureTest(_MissionsTestBase):
    def test_queue_write_oserror_returns_500(self):
        # An OSError from the atomic queue write surfaces as a clean 500 with a
        # single-line detail (not a traceback) — never a partial file.
        import unittest.mock as mock
        with mock.patch.object(
                da, '_atomic_write_json', side_effect=OSError('disk full')):
            r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
                'name': 'X', 'brief': 'b', 'repo': 'r',
            })
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(r.json()['detail']['error'], 'queue write failed')
        self.assertNotIn('\n', r.json()['detail']['detail'])


# ==================== proposed-phase extension (alert-pipeline-rework P3b) ====================


class PostMissionsProposedPhaseTest(_MissionsTestBase):
    """The optional phase/proposed_by/predraft fields (Stage B retrospective
    author posts `phase:'proposed'` cards). Backward-compatible: an unset phase
    keeps the legacy `drafting` shape byte-for-byte."""

    def test_proposed_phase_queues_with_provenance(self):
        predraft = {
            'classification': 'automate-now',
            'root_signature': 'medic::build-failed',
            'template_id': '2',
            'count_at_proposal': 4,
        }
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'retrospective medic build-failed 2026-06-29',
            'brief': 'recurring benign medic line',
            'repo': 'ourliberty-agent-core',
            'phase': 'proposed',
            'proposed_by': 'retrospective-author',
            'predraft': predraft,
        })
        self.assertEqual(r.status_code, 200, r.text)
        mid = r.json()['mission_id']
        entry = json.loads((self.queue_dir / f'{mid}.json').read_text())
        self.assertEqual(entry['phase'], 'proposed')
        self.assertEqual(entry['proposed_by'], 'retrospective-author')
        self.assertIn('proposed_at', entry)
        self.assertEqual(entry['predraft'], predraft)
        self.assertEqual(self.gh.calls, [])

    def test_unset_phase_is_drafting_without_proposed_keys(self):
        # Backward compatibility: the legacy +New flow shape is unchanged — no
        # proposed_by/proposed_at/predraft keys leak onto a drafting entry.
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Legacy Mission', 'brief': 'b', 'repo': 'r',
        })
        self.assertEqual(r.status_code, 200, r.text)
        entry = json.loads((self.queue_dir / 'legacy-mission.json').read_text())
        self.assertEqual(entry['phase'], 'drafting')
        self.assertNotIn('proposed_by', entry)
        self.assertNotIn('proposed_at', entry)
        self.assertNotIn('predraft', entry)

    def test_invalid_phase_returns_400_no_queue_write(self):
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Bad Phase', 'brief': 'b', 'repo': 'r',
            'phase': 'shipped',
        })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['detail']['error'], 'invalid phase')
        self.assertEqual(self._queued_ids(), set())

    def test_proposed_without_proposed_by_defaults_unknown(self):
        r = self.client.post(POST_ENDPOINT, headers=AUTH, json={
            'name': 'Anon Proposed', 'brief': 'b', 'repo': 'r',
            'phase': 'proposed',
        })
        self.assertEqual(r.status_code, 200, r.text)
        entry = json.loads((self.queue_dir / 'anon-proposed.json').read_text())
        self.assertEqual(entry['proposed_by'], 'unknown')
        self.assertNotIn('predraft', entry)  # predraft omitted when not sent


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
