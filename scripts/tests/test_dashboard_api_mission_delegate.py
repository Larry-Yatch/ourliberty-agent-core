#!/usr/bin/env python3
"""Tests for POST /api/system/missions/{id}/delegate (Contract B + the
2026-07-11 delegate-revert fixes).

The mission "Delegate to team" endpoint mirrors the parked-capture Delegate:
auth (X-Dashboard-Token + allowlisted X-Actor), guards (_find_mission → 404;
phase != proposed → 409), and a human-approval-gate APPROVAL_REQUEST proposal
into Beacon's inbox via safe_write_inbox. New in the revert fix:

  * a `spawned` ref (`kind='delegate'`, `task_id='delegate-<mission_id>'`) is
    stamped onto the mission entry — the join key the delegate-tracking read
    side needs; before this, a fully-successful mission delegation left the
    registry untouched and the card rendered as if never delegated;
  * durable idempotency — once the live-inbox proposal has been consumed, a
    re-POST short-circuits on the standing `spawned` ref to `already_delegated`
    instead of dispatching a fresh paid Beacon run; `force=true` overrides.

Path-isolation mirrors test_dashboard_api_capture_delegate.py: each test owns a
fresh tmpdir; `da._missions_json_path` is rebound onto it, and
`safe_write_inbox.safe_write_inbox` + `INBOXES_ROOT` are rebound so the real
agent inboxes are never touched.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_mission_delegate
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
import safe_write_inbox as swi  # noqa: E402
from beacon_approval_handler import REQUIRED_FIELDS  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}

_AR_REQUIRED = REQUIRED_FIELDS['approval_request']  # {task_id, summary, target_agent, prompt}


def _endpoint(mission_id: str) -> str:
    return f'/api/system/missions/{mission_id}/delegate'


def _mission(mid='m-1', *, phase='proposed', name='Fix the thing',
             brief='the thing is broken', **extra):
    m = {'id': mid, 'phase': phase, 'name': name, 'brief': brief}
    m.update(extra)
    return m


class _InboxRecorder:
    """Stand-in for safe_write_inbox.safe_write_inbox. Records every call AND
    writes the envelope to the (rebound) tmp inbox so the handler's dedup
    existence-check sees an already-open proposal on a re-POST."""

    def __init__(self, inboxes_root: Path) -> None:
        self.inboxes_root = inboxes_root
        self.calls: list[dict] = []

    def __call__(self, target_agent, task_dict, source_agent, filename):
        self.calls.append({
            'target_agent': target_agent,
            'task_dict': task_dict,
            'source_agent': source_agent,
            'filename': filename,
        })
        safe_name = swi.canonical_inbox_name(filename)
        dest = self.inboxes_root / swi.sanitize_component(target_agent) / safe_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(task_dict, indent=2) + '\n')
        return dest


class _MissionDelegateTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-mission-delegate-'))
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'
        self.inboxes_root = self.tmp / 'agents' / 'inboxes'

        self._orig_missions_path = da._missions_json_path
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]

        # Rebind safe_write_inbox so the real inboxes/jail are never engaged.
        self._orig_inboxes_root = swi.INBOXES_ROOT
        self._orig_swi = swi.safe_write_inbox
        swi.INBOXES_ROOT = self.inboxes_root  # type: ignore[assignment]
        self.inbox = _InboxRecorder(self.inboxes_root)
        swi.safe_write_inbox = self.inbox  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        swi.INBOXES_ROOT = self._orig_inboxes_root  # type: ignore[assignment]
        swi.safe_write_inbox = self._orig_swi  # type: ignore[assignment]

    def _seed(self, *missions) -> None:
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)},
                       indent=2) + '\n')

    def _read_local(self) -> dict:
        return json.loads(self.missions_path.read_text())

    def _mission_row(self, mid='m-1') -> dict:
        return next(m for m in self._read_local()['missions'] if m['id'] == mid)


# ==================== happy path: proposal + spawned stamp ====================


class MissionDelegateProposalTest(_MissionDelegateTestBase):
    def test_emits_proposal_with_required_ar_fields(self):
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['dispatched'])
        self.assertNotIn('deduped', body)
        self.assertNotIn('already_delegated', body)

        self.assertEqual(len(self.inbox.calls), 1)
        call = self.inbox.calls[0]
        self.assertEqual(call['target_agent'], 'beacon')
        self.assertEqual(call['source_agent'], 'dashboard')
        self.assertEqual(call['filename'], 'delegate-m-1.json')

        env = call['task_dict']
        for field in _AR_REQUIRED:
            self.assertIn(field, env)
            self.assertTrue(env[field], f'{field} must be non-empty')
        self.assertEqual(env['task_id'], 'delegate-m-1')
        self.assertEqual(env['mission_id'], 'm-1')
        self.assertEqual(env['dedup_identity'], 'delegate:m-1')
        self.assertEqual(env['timeout'], 600)
        self.assertEqual(env['source'], 'dashboard')

    def test_mission_stays_proposed_and_gets_spawned_ref(self):
        # The revert fix: delegating stamps the join key onto the mission entry
        # but does NOT change `phase` — the stamp is additive.
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._mission_row('m-1')
        self.assertEqual(row['phase'], 'proposed')
        self.assertEqual(row['spawned']['kind'], 'delegate')
        self.assertEqual(row['spawned']['task_id'], 'delegate-m-1')
        self.assertIn('stamped_at', row['spawned'])

    def test_other_missions_untouched_by_stamp(self):
        self._seed(_mission('m-1'), _mission('m-2', name='Other'))
        self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertNotIn('spawned', self._mission_row('m-2'))

    def test_invalid_action_returns_400(self):
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.inbox.calls, [])
        self.assertNotIn('spawned', self._mission_row('m-1'))


# ==================== guards (404 / 409) ====================


class MissionDelegateGuardTest(_MissionDelegateTestBase):
    def test_missing_mission_returns_404(self):
        self._seed(_mission('m-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.inbox.calls, [])

    def test_non_proposed_returns_409(self):
        self._seed(_mission('m-1', phase='in_flight'))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not proposed')
        self.assertEqual(self.inbox.calls, [])


# ==================== idempotency: live-inbox dedup + durable ====================


class MissionDelegateIdempotencyTest(_MissionDelegateTestBase):
    def test_repost_collapses_onto_open_proposal_and_reasserts_stamp(self):
        self._seed(_mission('m-1'))
        first = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(first.status_code, 200, first.text)
        first_ref = self._mission_row('m-1')['spawned']

        second = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()['deduped'])
        self.assertEqual(len(self.inbox.calls), 1)  # no double-propose
        # Idempotent re-stamp: same identity, no stamped_at churn.
        self.assertEqual(self._mission_row('m-1')['spawned'], first_ref)

    def test_consumed_proposal_short_circuits_to_already_delegated(self):
        # The 2026-07-11 revert class: the proposal was consumed (no live inbox
        # file) but the mission carries the standing spawned ref → re-POST
        # returns the prior outcome instead of a fresh paid Beacon run.
        self._seed(_mission('m-1', spawned={
            'kind': 'delegate',
            'task_id': 'delegate-m-1',
            'stamped_at': '2026-07-09T20:05:00+00:00',
        }))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['dispatched'])
        self.assertTrue(body['already_delegated'])
        self.assertEqual(body['delegated_at'], '2026-07-09T20:05:00+00:00')
        self.assertEqual(self.inbox.calls, [])  # NO new proposal, no LLM spend

    def test_already_delegated_joins_open_approval(self):
        # When the delegation's pending approval is still open, the short-
        # circuit response carries its id (same origin_task_id join as the
        # delegation_needs_you chip).
        self._seed(_mission('m-1', spawned={
            'kind': 'delegate',
            'task_id': 'delegate-m-1',
            'stamped_at': '2026-07-09T20:05:00+00:00',
        }))
        orig = da._open_delegate_approvals
        da._open_delegate_approvals = lambda ids, agents_root=None: {
            'delegate-m-1': {'approval_id': 'scoped-task-001',
                             'created_at': '2026-07-09T20:10:00+00:00'},
        }
        try:
            r = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        finally:
            da._open_delegate_approvals = orig
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['approval_id'], 'scoped-task-001')

    def test_force_redelegates_past_standing_ref(self):
        self._seed(_mission('m-1', spawned={
            'kind': 'delegate',
            'task_id': 'delegate-m-1',
            'stamped_at': '2026-07-09T20:05:00+00:00',
        }))
        r = self.client.post(_endpoint('m-1'), headers=AUTH,
                             json={'force': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['dispatched'])
        self.assertNotIn('already_delegated', r.json())
        self.assertEqual(len(self.inbox.calls), 1)  # fresh proposal written

    def test_foreign_spawned_kind_does_not_short_circuit(self):
        # A mission whose spawned ref is some other kind (e.g. a future
        # 'project' ref) still delegates normally.
        self._seed(_mission('m-1', spawned={
            'kind': 'project', 'task_id': 'proj-1'}))
        r = self.client.post(_endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.inbox.calls), 1)


if __name__ == '__main__':
    unittest.main()
