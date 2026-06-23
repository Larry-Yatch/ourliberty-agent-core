#!/usr/bin/env python3
"""Tests for the reversibility + raw-orphan-promote surface (projects-v3 P3
follow-up, p3f-reversibility-and-orphan, spec § 5 / § 6 step 3):

  * POST /api/projects/archive    — the Drop/Archive gesture: flip a project's
    `state` to `archived` so it leaves "Actively working" and its original funnel
    source item (capture / mission / orphan) returns to the funnel. Reversible,
    not a dead end.
  * POST /api/funnel/promote with kind='orphan' — a raw orphan card (a task_id in
    chain_events with no registered mission) is promotable too; the new project
    records `promoted_from={'kind':'orphan','task_id':ref}`.
  * The funnel-derive suppression of a promoted orphan (`_promoted_orphan_task_ids`
    + `_build_funnel`), tested as pure functions (network-free).

Both endpoints are NON-committers: they rewrite projects.json ON DISK and rely on
`heal_projects_store.py` (the SOLE committer) to commit the delta. A correct run
opens NO PR / makes NO github call — the recorder raises on any attempt.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_archive_and_orphan
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
import projects_store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ARCHIVE = '/api/projects/archive'
PROMOTE = '/api/funnel/promote'
ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}


def _phase(phid='aging-idea', *, lifecycle_state='brainstorm', **extra):
    p = {
        'id': phid,
        'title': phid,
        'desired_end_state': 'ship the thing',
        'lifecycle_state': lifecycle_state,
        'order': 0,
        'spec_ref': None,
        'sequence_ref': None,
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
        'phases': phases if phases is not None else [_phase(pid)],
    }
    proj.update(extra)
    return proj


def _mission(mid='proposed-orphan-x', *, name='Orphan X', phase='proposed',
             brief='close the gap', repo='ourliberty-agent-core', **extra):
    m = {
        'id': mid, 'name': name, 'phase': phase, 'brief': brief,
        'repo': repo, 'task_ids': [],
    }
    m.update(extra)
    return m


class _GithubRecorder:
    """Archive / promote open no PR — any github call is a regression."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({'method': method, 'url': url})
        raise AssertionError(
            f'unexpected github call: {method} {url} — this path opens no PR')


class _Base(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-archive-orphan-'))
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
        da._get_larry_action_supabase_client = lambda: None
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh

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

    def _seed_projects(self, *projects) -> None:
        self.projects_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_path.write_text(
            json.dumps({'schema_version': 1, 'projects': list(projects)},
                       indent=2) + '\n')

    def _read_projects(self) -> list:
        return json.loads(self.projects_path.read_text())['projects']

    def _project_on_disk(self, project_id) -> dict:
        for proj in self._read_projects():
            if proj['id'] == project_id:
                return proj
        raise AssertionError(f'project {project_id} not on disk')


# ==================== archive: auth ====================


class ArchiveAuthTest(_Base):
    def test_missing_token_returns_401(self):
        r = self.client.post(ARCHIVE, headers={'X-Actor': ACTOR},
                             json={'project_id': 'aging-idea'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.gh.calls, [])

    def test_missing_actor_returns_401(self):
        self._seed_projects(_project())
        r = self.client.post(ARCHIVE, headers={'X-Dashboard-Token': TOKEN},
                             json={'project_id': 'aging-idea'})
        self.assertEqual(r.status_code, 401)

    def test_unlisted_actor_returns_401(self):
        self._seed_projects(_project())
        r = self.client.post(
            ARCHIVE,
            headers={'X-Dashboard-Token': TOKEN, 'X-Actor': 'mallory@evil.test'},
            json={'project_id': 'aging-idea'})
        self.assertEqual(r.status_code, 401)


# ==================== archive: validation ====================


class ArchiveValidationTest(_Base):
    def test_blank_project_id_rejected_422(self):
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': ''})
        self.assertEqual(r.status_code, 422)

    def test_unknown_project_returns_404_no_write(self):
        self._seed_projects(_project())
        before = self.projects_path.read_text()
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'nope'})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()['detail']['error'], 'project not found')
        self.assertEqual(self.projects_path.read_text(), before)
        self.assertEqual(self.gh.calls, [])


# ==================== archive: the gesture ====================


class ArchiveTest(_Base):
    def test_archive_active_project_flips_state_no_commit(self):
        self._seed_projects(_project())
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['state'], 'archived')
        self.assertEqual(body['status'], 'archived')
        self.assertTrue(body['applied'])

        # State flipped on disk; non-committer (no github call).
        self.assertEqual(self.gh.calls, [])
        proj = self._project_on_disk('aging-idea')
        self.assertEqual(proj['state'], 'archived')
        self.assertIn('updated_at', proj)

    def test_re_archive_is_idempotent_no_second_write(self):
        self._seed_projects(_project())
        first = self.client.post(ARCHIVE, headers=AUTH,
                                 json={'project_id': 'aging-idea'})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['applied'])
        after_first = self.projects_path.read_text()

        second = self.client.post(ARCHIVE, headers=AUTH,
                                  json={'project_id': 'aging-idea'})
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()['applied'])
        # No spurious delta — byte-identical (no heal-commit churn).
        self.assertEqual(self.projects_path.read_text(), after_first)

    def test_archive_leaves_pipeline_view(self):
        # build_pipeline excludes archived projects → it leaves "Actively working".
        self._seed_projects(_project())
        self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'aging-idea'})
        from projects_store import build_pipeline
        self.assertEqual(build_pipeline(self._read_projects()), [])


# ============ archive: honest message + durable return-to-funnel (p3f2) ======


def _capture(cid='cap-1', *, state='promoted', title='Cap One', **extra):
    cap = {'id': cid, 'state': state, 'title': title}
    if state == 'promoted':
        cap['promoted_to'] = extra.pop('promoted_to', f'{cid}-proj')
        cap['spawned'] = extra.pop(
            'spawned', {'kind': 'project', 'project_id': f'{cid}-proj'})
    cap.update(extra)
    return cap


class ArchiveHonestMessageTest(_Base):
    """p3f2-archive-honest: the toast must match behavior. A project promoted from
    a funnel source returns to the funnel ('returned to the funnel'); a project
    with no funnel provenance is archive-only ('Archived'). Reload-stable."""

    def _capture_on_disk(self, cid) -> dict:
        registry = json.loads(self.captures_path.read_text())
        for cap in registry['captures']:
            if cap['id'] == cid:
                return cap
        raise AssertionError(f'capture {cid} not on disk')

    # ---- no provenance → honest "Archived" ----
    def test_archive_without_provenance_says_archived(self):
        self._seed_projects(_project())
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'archived')
        self.assertEqual(body['message'], 'Archived.')
        self.assertTrue(body['applied'])
        self.assertEqual(self.gh.calls, [])

    # ---- mission provenance → honest "returned to the funnel", no capture write ----
    def test_archive_mission_says_returned_to_funnel(self):
        self._seed_projects(_project(
            promoted_from={'kind': 'mission', 'mission_id': 'm-1'}))
        before_caps = self.captures_path.read_text()
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'returned-to-funnel')
        self.assertEqual(body['message'], 'Dropped — returned to the funnel.')
        # mission/orphan return structurally — captures.json is untouched.
        self.assertEqual(self.captures_path.read_text(), before_caps)
        self.assertEqual(self.gh.calls, [])

    # ---- orphan provenance → honest "returned to the funnel" ----
    def test_archive_orphan_says_returned_to_funnel(self):
        self._seed_projects(_project(
            promoted_from={'kind': 'orphan', 'task_id': 'orphan-task-1'}))
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'aging-idea'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['message'], 'Dropped — returned to the funnel.')

    # ---- capture provenance → re-park the capture so the return is REAL ----
    def test_archive_capture_re_parks_and_says_returned(self):
        self._seed_captures(_capture('cap-1', promoted_to='cap-1-proj'))
        self._seed_projects(_project(
            'cap-1-proj',
            promoted_from={'kind': 'capture', 'capture_id': 'cap-1'}))

        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'cap-1-proj'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'returned-to-funnel')
        self.assertEqual(body['message'], 'Dropped — returned to the funnel.')
        self.assertEqual(self.gh.calls, [])

        # The capture is actually back in the parked lane — state flipped, the
        # promote-stamped fields cleared (so it's a clean re-park, not a ghost).
        cap = self._capture_on_disk('cap-1')
        self.assertEqual(cap['state'], 'parked')
        self.assertNotIn('promoted_to', cap)
        self.assertNotIn('spawned', cap)

        # Reload-stable: re-derive the funnel from disk → the capture is back in
        # the parked lane, and the project is gone from the pipeline.
        from datetime import datetime, timezone
        caps = json.loads(self.captures_path.read_text())['captures']
        parked = da._parked_from_captures(caps, datetime.now(timezone.utc))
        self.assertIn('cap-1', {p['capture_id'] for p in parked})

        from projects_store import build_pipeline
        self.assertEqual(build_pipeline(self._read_projects()), [])
        self.assertEqual(self._project_on_disk('cap-1-proj')['state'], 'archived')

    def test_archive_capture_missing_capture_is_fail_safe(self):
        # The project claims capture provenance but the capture row is gone — the
        # un-flip is a no-op (nothing to return) and the archive still succeeds.
        self._seed_captures()  # no captures
        self._seed_projects(_project(
            'cap-1-proj',
            promoted_from={'kind': 'capture', 'capture_id': 'cap-1'}))
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'cap-1-proj'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Nothing was re-parked → the toast must NOT claim a return.
        self.assertEqual(body['message'], 'Archived.')
        self.assertEqual(body['status'], 'archived')
        self.assertEqual(self._project_on_disk('cap-1-proj')['state'], 'archived')

        # Re-archive (idempotent path): the honesty contract must hold here too —
        # the capture row is still gone, so the no-op return must stay 'Archived'
        # and not regress to a kind-based "returned to the funnel" claim.
        again = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'cap-1-proj'})
        self.assertEqual(again.status_code, 200)
        again_body = again.json()
        self.assertFalse(again_body['applied'])
        self.assertEqual(again_body['message'], 'Archived.')
        self.assertEqual(again_body['status'], 'archived')

    # ---- idempotent re-archive still reports the honest outcome, no 2nd write ----
    def test_re_archive_capture_idempotent_reports_outcome(self):
        self._seed_captures(_capture('cap-1', promoted_to='cap-1-proj'))
        self._seed_projects(_project(
            'cap-1-proj',
            promoted_from={'kind': 'capture', 'capture_id': 'cap-1'}))
        first = self.client.post(ARCHIVE, headers=AUTH,
                                 json={'project_id': 'cap-1-proj'})
        self.assertTrue(first.json()['applied'])
        caps_after_first = self.captures_path.read_text()
        projs_after_first = self.projects_path.read_text()

        second = self.client.post(ARCHIVE, headers=AUTH,
                                  json={'project_id': 'cap-1-proj'})
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertFalse(body['applied'])
        self.assertEqual(body['message'], 'Dropped — returned to the funnel.')
        # No second write to either store (no spurious heal-commit churn).
        self.assertEqual(self.captures_path.read_text(), caps_after_first)
        self.assertEqual(self.projects_path.read_text(), projs_after_first)


# ============ Complete & retire: a Done project leaves the board, source kept ===


def _done_project(pid='done-idea', *, promoted_from=None, **extra):
    """A project whose every phase is done → coarse rollup 'done' → the terminal
    Drop RETIRES it (state 'retired', source NOT returned) rather than dropping it
    back to the funnel."""
    proj = _project(pid, phases=[_phase(pid, lifecycle_state='done')], **extra)
    if promoted_from is not None:
        proj['promoted_from'] = promoted_from
    return proj


class RetireDoneProjectTest(_Base):
    """p3f3-complete-and-retire: a DONE project's Drop is the terminal "Complete &
    retire" gesture — state→'retired', it leaves the board, and its funnel source
    is NOT returned (done is done, not a mis-promote). A not-done project keeps the
    reversible return-to-funnel behavior unchanged."""

    def _capture_on_disk(self, cid) -> dict:
        for cap in json.loads(self.captures_path.read_text())['captures']:
            if cap['id'] == cid:
                return cap
        raise AssertionError(f'capture {cid} not on disk')

    def test_done_project_retires_with_completed_message(self):
        self._seed_projects(_done_project(
            promoted_from={'kind': 'mission', 'mission_id': 'm-1'}))
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'done-idea'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'retired')
        self.assertEqual(body['message'], 'Completed — cleared from the board.')
        self.assertEqual(body['state'], 'retired')
        self.assertTrue(body['applied'])
        self.assertEqual(self._project_on_disk('done-idea')['state'], 'retired')
        self.assertEqual(self.gh.calls, [])
        # A retired project leaves the pipeline like any non-active project.
        from projects_store import build_pipeline
        self.assertEqual(build_pipeline(self._read_projects()), [])

    def test_retire_done_mission_stays_suppressed(self):
        # The whole point: a retired Done mission must NOT bounce back into the
        # funnel. The suppression set still counts it (active OR retired).
        self._seed_projects(_done_project(
            promoted_from={'kind': 'mission', 'mission_id': 'm-1'}))
        self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'done-idea'})
        self.assertIn('m-1', da._promoted_mission_ids(self._read_projects()))

    def test_retire_done_orphan_stays_suppressed(self):
        self._seed_projects(_done_project(
            promoted_from={'kind': 'orphan', 'task_id': 'orphan-task-1'}))
        self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'done-idea'})
        self.assertIn(
            'orphan-task-1', da._promoted_orphan_task_ids(self._read_projects()))

    def test_retire_done_capture_not_re_parked(self):
        # A capture-sourced Done project: retire leaves the capture CONSUMED
        # ('promoted'), NOT re-parked — the work shipped, it's not parked intake.
        self._seed_captures(_capture('cap-1', promoted_to='cap-1-proj'))
        self._seed_projects(_done_project(
            'cap-1-proj', promoted_from={'kind': 'capture', 'capture_id': 'cap-1'}))
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'cap-1-proj'})
        self.assertEqual(r.json()['status'], 'retired')
        self.assertEqual(self._capture_on_disk('cap-1')['state'], 'promoted')

    def test_re_retire_is_idempotent_no_second_write(self):
        self._seed_projects(_done_project(
            promoted_from={'kind': 'mission', 'mission_id': 'm-1'}))
        first = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'done-idea'})
        self.assertTrue(first.json()['applied'])
        projs_after = self.projects_path.read_text()
        second = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'done-idea'})
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertFalse(body['applied'])
        self.assertEqual(body['status'], 'retired')
        self.assertEqual(body['message'], 'Completed — cleared from the board.')
        self.assertEqual(self.projects_path.read_text(), projs_after)

    def test_idempotent_outcome_tracks_recorded_state_not_phases(self):
        # Guard the honesty contract on the already-terminal path: a project
        # DROPPED while not-done (→ 'archived', returned to funnel) whose phases
        # later complete OFFLINE must, on a re-archive, report its RECORDED state
        # ('archived' / returned-to-funnel) — NOT a phase-recomputed 'retired'.
        # state and status can never disagree.
        self._seed_projects(_project(
            'drift-idea', promoted_from={'kind': 'mission', 'mission_id': 'm-3'}))
        first = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'drift-idea'})
        self.assertEqual(first.json()['state'], 'archived')
        # Simulate the project's phases completing after it was dropped.
        self._seed_projects(_project(
            'drift-idea', state='archived',
            phases=[_phase('drift-idea', lifecycle_state='done')],
            promoted_from={'kind': 'mission', 'mission_id': 'm-3'}))
        again = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'drift-idea'})
        body = again.json()
        self.assertFalse(body['applied'])
        self.assertEqual(body['state'], 'archived')
        self.assertEqual(body['status'], 'returned-to-funnel')
        self.assertNotEqual(body['status'], 'retired')

    def test_partially_done_project_still_returns_to_funnel(self):
        # Not every phase done → NOT a retire → the reversible drop still returns
        # the mission to the funnel (the in-flight escape hatch is unchanged).
        self._seed_projects(_project(
            'half-idea',
            phases=[_phase('p1', lifecycle_state='done', order=0),
                    _phase('p2', lifecycle_state='building', order=1)],
            promoted_from={'kind': 'mission', 'mission_id': 'm-2'}))
        r = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': 'half-idea'})
        self.assertEqual(r.json()['status'], 'returned-to-funnel')
        self.assertEqual(self._project_on_disk('half-idea')['state'], 'archived')
        self.assertNotIn('m-2', da._promoted_mission_ids(self._read_projects()))


class RetireHelperUnitTest(unittest.TestCase):
    """The pure projects_store helpers the retire branch hinges on."""

    def test_suppresses_funnel_source(self):
        self.assertTrue(projects_store.suppresses_funnel_source('active'))
        self.assertTrue(projects_store.suppresses_funnel_source('retired'))
        self.assertFalse(projects_store.suppresses_funnel_source('archived'))
        self.assertFalse(projects_store.suppresses_funnel_source('nonsense'))

    def test_project_is_done(self):
        self.assertTrue(projects_store.project_is_done(
            {'phases': [{'lifecycle_state': 'done'},
                        {'lifecycle_state': 'done'}]}))
        self.assertFalse(projects_store.project_is_done(
            {'phases': [{'lifecycle_state': 'done'},
                        {'lifecycle_state': 'building'}]}))
        # No phases / junk → not done (fail-safe).
        self.assertFalse(projects_store.project_is_done({'phases': []}))
        self.assertFalse(projects_store.project_is_done({}))
        self.assertFalse(projects_store.project_is_done(None))

    def test_promoted_mission_ids_counts_active_and_retired_not_archived(self):
        projects = [
            {'state': 'active',
             'promoted_from': {'kind': 'mission', 'mission_id': 'a'}},
            {'state': 'retired',
             'promoted_from': {'kind': 'mission', 'mission_id': 'r'}},
            {'state': 'archived',
             'promoted_from': {'kind': 'mission', 'mission_id': 'x'}},
        ]
        self.assertEqual(da._promoted_mission_ids(projects), {'a', 'r'})


# ==================== orphan promote ====================


class OrphanPromoteTest(_Base):
    def test_orphan_promote_creates_project_with_provenance(self):
        r = self.client.post(PROMOTE, headers=AUTH,
                             json={'ref': 'orphan-task-1', 'kind': 'orphan'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['source_kind'], 'orphan')
        self.assertEqual(body['status'], 'promoted')
        self.assertTrue(body['applied'])
        self.assertEqual(self.gh.calls, [])

        proj = self._read_projects()[0]
        self.assertEqual(proj['state'], 'active')
        self.assertEqual(proj['phases'][0]['lifecycle_state'], 'brainstorm')
        self.assertEqual(
            proj['promoted_from'], {'kind': 'orphan', 'task_id': 'orphan-task-1'})
        # Default title is the humanized task_id.
        self.assertEqual(proj['title'], 'Orphan Task 1')

    def test_orphan_promote_threads_overrides(self):
        r = self.client.post(PROMOTE, headers=AUTH, json={
            'ref': 'orphan-task-1', 'kind': 'orphan',
            'name': 'Renamed', 'brief': 'sharper brief',
            'repo': 'ourliberty-dashboard', 'north_star_ref': 'ns-1',
        })
        self.assertEqual(r.status_code, 200)
        proj = self._read_projects()[0]
        self.assertEqual(proj['title'], 'Renamed')
        self.assertEqual(proj['repo'], 'ourliberty-dashboard')
        self.assertEqual(proj['north_star_ref'], 'ns-1')
        self.assertEqual(proj['phases'][0]['desired_end_state'], 'sharper brief')

    def test_re_promote_orphan_is_idempotent(self):
        # A re-promote finds the existing active project via promoted_from and
        # returns it (applied=False) instead of minting a duplicate — same
        # idempotency contract as capture/mission promote (200, not 409).
        first = self.client.post(PROMOTE, headers=AUTH,
                                 json={'ref': 'orphan-task-1', 'kind': 'orphan'})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['applied'])
        second = self.client.post(PROMOTE, headers=AUTH,
                                  json={'ref': 'orphan-task-1', 'kind': 'orphan'})
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()['applied'])
        self.assertEqual(second.json()['project_id'], first.json()['project_id'])
        self.assertEqual(len(self._read_projects()), 1)

    def test_orphan_kind_does_not_collide_with_capture_or_mission(self):
        # kind='orphan' takes the orphan path even if the same id exists as a
        # capture/mission — it is an explicit disambiguator.
        self._seed_missions(_mission('shared-id', name='Mission'))
        r = self.client.post(PROMOTE, headers=AUTH,
                             json={'ref': 'shared-id', 'kind': 'orphan'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['source_kind'], 'orphan')
        proj = self._read_projects()[0]
        self.assertEqual(proj['promoted_from']['kind'], 'orphan')

    def test_auto_resolve_does_not_create_orphan_for_unknown_ref(self):
        # Without kind='orphan', an unknown ref stays a 404 (the orphan path is
        # opt-in; auto-resolution never mints a project from an arbitrary ref).
        r = self.client.post(PROMOTE, headers=AUTH, json={'ref': 'orphan-task-1'})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(self.projects_path.exists())


# ==================== reversibility (archive un-suppresses the source) ====================


class ReversibilityTest(_Base):
    def test_archived_orphan_is_re_promotable(self):
        # Promote a raw orphan → active project. Archive it. The orphan is no
        # longer suppressed (only active projects count), so a re-promote
        # succeeds (200, fresh project) instead of the idempotent 409.
        first = self.client.post(PROMOTE, headers=AUTH,
                                 json={'ref': 'orphan-task-1', 'kind': 'orphan'})
        self.assertEqual(first.status_code, 200)
        pid = first.json()['project_id']

        arch = self.client.post(ARCHIVE, headers=AUTH, json={'project_id': pid})
        self.assertEqual(arch.status_code, 200)

        again = self.client.post(PROMOTE, headers=AUTH,
                                 json={'ref': 'orphan-task-1', 'kind': 'orphan'})
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()['applied'])
        self.assertNotEqual(again.json()['project_id'], pid)
        # The archived original is preserved (no overwrite); the new one is active.
        states = {p['id']: p['state'] for p in self._read_projects()}
        self.assertEqual(states[pid], 'archived')
        self.assertEqual(states[again.json()['project_id']], 'active')

    def test_archived_mission_returns_to_funnel_and_is_re_promotable(self):
        self._seed_missions(_mission('proposed-orphan-x'))
        first = self.client.post(PROMOTE, headers=AUTH,
                                 json={'ref': 'proposed-orphan-x'})
        self.assertEqual(first.status_code, 200)
        pid = first.json()['project_id']
        # While active, the mission is suppressed: a re-promote is the idempotent
        # no-op (200, applied=False, same project) rather than a duplicate.
        dup = self.client.post(PROMOTE, headers=AUTH,
                               json={'ref': 'proposed-orphan-x'})
        self.assertEqual(dup.status_code, 200)
        self.assertFalse(dup.json()['applied'])
        self.assertEqual(dup.json()['project_id'], pid)

        self.assertEqual(
            self.client.post(ARCHIVE, headers=AUTH,
                             json={'project_id': pid}).status_code, 200)

        # Archived → un-suppressed → re-promote mints a fresh active project.
        again = self.client.post(PROMOTE, headers=AUTH,
                                 json={'ref': 'proposed-orphan-x'})
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()['applied'])
        self.assertNotEqual(again.json()['project_id'], pid)


# ==================== funnel suppression (pure helpers) ====================


class OrphanSuppressionUnitTest(unittest.TestCase):
    def test_promoted_orphan_task_ids_counts_only_active(self):
        projects = [
            {'state': 'active',
             'promoted_from': {'kind': 'orphan', 'task_id': 'live-task'}},
            {'state': 'archived',
             'promoted_from': {'kind': 'orphan', 'task_id': 'archived-task'}},
            {'state': 'active',
             'promoted_from': {'kind': 'mission', 'mission_id': 'm-1'}},
        ]
        self.assertEqual(da._promoted_orphan_task_ids(projects), {'live-task'})

    def test_build_funnel_suppresses_promoted_orphan(self):
        orphans = [
            {'task_id': 'live-task', 'repo': 'r', 'terminal': False},
            {'task_id': 'free-task', 'repo': 'r', 'terminal': False},
        ]
        funnel = da._build_funnel(
            [], orphans, [], None,
            promoted_orphan_task_ids={'live-task'})
        refs = {item['ref'] for item in funnel['secondary']}
        self.assertNotIn('live-task', refs)
        self.assertIn('free-task', refs)

    def test_build_funnel_without_suppression_keeps_all_orphans(self):
        orphans = [{'task_id': 'live-task', 'repo': 'r', 'terminal': False}]
        funnel = da._build_funnel([], orphans, [], None)
        refs = {item['ref'] for item in funnel['secondary']}
        self.assertIn('live-task', refs)


class SequenceRollupOrphanCollapseTest(unittest.TestCase):
    """projects-v3 sequence-rollup-done-flip: the orphan-collapse half. A step
    of a build sequence LINKED to a pipeline phase must not float loose as a
    standalone orphan card — it is attributed to its parent phase. Bare
    (non-phase-linked) sequences are out of scope and untouched."""

    def _build_sequences(self):
        # mirrors the `_reader_build_sequences` {active, archived} response:
        # a complete sequence lands in `archived`; an active one in `active`.
        return {
            'active': [
                {'seq_id': 'launch-ph-building', 'status': 'active',
                 'steps': [{'step_id': 'ph-building'}]},
            ],
            'archived': [
                {'seq_id': 'launch-ph-done', 'status': 'complete',
                 'steps': [{'step_id': 'p4-cleanup-committer'},
                           {'step_id': 'p4-postmerge-exec'}]},
                {'seq_id': 'bare-meta-seq', 'status': 'complete',
                 'steps': [{'step_id': 'bare-step'}]},
            ],
        }

    def _projects(self):
        # two phases carry sequence_ref (phase-linked); `bare-meta-seq` has no
        # phase pointing at it.
        return [{
            'id': 'proj', 'state': 'active', 'phases': [
                {'id': 'ph-done', 'sequence_ref': 'launch-ph-done'},
                {'id': 'ph-building', 'sequence_ref': 'launch-ph-building'},
            ],
        }]

    def test_status_map_spans_active_and_archived(self):
        self.assertEqual(
            da._sequence_status_by_id(self._build_sequences()),
            {'launch-ph-building': 'active',
             'launch-ph-done': 'complete',
             'bare-meta-seq': 'complete'},
        )

    def test_phase_linked_ids_only(self):
        self.assertEqual(
            da._phase_linked_sequence_ids(self._projects()),
            {'launch-ph-done', 'launch-ph-building'},
        )

    def test_collapsed_step_ids_exclude_bare_sequence(self):
        collapsed = da._collapsed_step_task_ids(
            self._projects(), self._build_sequences())
        self.assertEqual(
            collapsed, {'p4-cleanup-committer', 'p4-postmerge-exec', 'ph-building'})
        self.assertNotIn('bare-step', collapsed)  # not phase-linked → untouched

    def test_collapsed_empty_when_no_phase_carries_a_ref(self):
        projects = [{'id': 'p', 'state': 'active',
                     'phases': [{'id': 'ph'}]}]  # no sequence_ref
        self.assertEqual(
            da._collapsed_step_task_ids(projects, self._build_sequences()), set())

    def test_detect_orphans_collapses_phase_linked_steps(self):
        events = [
            {'task_id': 'p4-cleanup-committer', 'event_type': 'task_done',
             'agent': 'forge', 'ts': '2026-06-22T01:00:00+00:00'},
            {'task_id': 'free-orphan', 'event_type': 'task_done',
             'agent': 'forge', 'ts': '2026-06-22T01:00:00+00:00'},
            {'task_id': 'bare-step', 'event_type': 'task_done',
             'agent': 'forge', 'ts': '2026-06-22T01:00:00+00:00'},
        ]
        collapsed = da._collapsed_step_task_ids(
            self._projects(), self._build_sequences())
        tids = {o['task_id']
                for o in da.detect_orphans(events, set(), collapsed)}
        self.assertNotIn('p4-cleanup-committer', tids)  # collapsed under parent
        self.assertIn('free-orphan', tids)              # genuine orphan stays
        self.assertIn('bare-step', tids)                # bare seq step untouched

    def test_detect_orphans_without_collapse_arg_unchanged(self):
        events = [{'task_id': 'p4-cleanup-committer', 'event_type': 'task_done',
                   'agent': 'forge', 'ts': '2026-06-22T01:00:00+00:00'}]
        tids = {o['task_id'] for o in da.detect_orphans(events, set())}
        self.assertIn('p4-cleanup-committer', tids)


if __name__ == '__main__':
    unittest.main()
