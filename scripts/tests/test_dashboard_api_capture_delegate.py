#!/usr/bin/env python3
"""Tests for POST /api/missions/captures/{id}/delegate (delegate-fix spec § 2).

The droplet "Delegate to team" endpoint: mirrors the capture-action route's auth
(X-Dashboard-Token + an allowlisted X-Actor) and guards (_find_capture → 404;
_require_parked → 409), but instead of mutating the capture it emits a
human-approval-gate APPROVAL_REQUEST proposal into Beacon's inbox via
safe_write_inbox. The capture stays parked — NO captures.json mutation. A re-POST
that finds an already-open proposal collapses onto it (dedup).

Path-isolation mirrors test_dashboard_api_capture_actions.py: each test owns a
fresh tmpdir; `da._captures_json_path` is rebound onto it, and
`safe_write_inbox.safe_write_inbox` + `INBOXES_ROOT` are rebound so the real
agent inboxes are never touched and the test jail's inbox-write guard never trips.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_capture_delegate
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


def _endpoint(capture_id: str) -> str:
    return f'/api/missions/captures/{capture_id}/delegate'


def _cap(cid='cap-1', *, state='parked', title='Aging idea',
         note='do the thing', last_touched='2026-06-01T00:00:00+00:00', **extra):
    cap = {
        'id': cid,
        'title': title,
        'state': state,
        'note': note,
        'last_touched': last_touched,
        'origin': {'repo': 'ourliberty-agent-core'},
    }
    cap.update(extra)
    return cap


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


class _DelegateTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-delegate-'))
        self.captures_path = self.tmp / 'agents' / 'beacon' / 'captures.json'
        self.inboxes_root = self.tmp / 'agents' / 'inboxes'

        self._orig_captures_path = da._captures_json_path
        da._captures_json_path = lambda: self.captures_path  # type: ignore[assignment]

        # Rebind safe_write_inbox so the real inboxes/jail are never engaged.
        self._orig_inboxes_root = swi.INBOXES_ROOT
        self._orig_swi = swi.safe_write_inbox
        swi.INBOXES_ROOT = self.inboxes_root  # type: ignore[assignment]
        self.inbox = _InboxRecorder(self.inboxes_root)
        swi.safe_write_inbox = self.inbox  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._captures_json_path = self._orig_captures_path  # type: ignore[assignment]
        swi.INBOXES_ROOT = self._orig_inboxes_root  # type: ignore[assignment]
        swi.safe_write_inbox = self._orig_swi  # type: ignore[assignment]

    def _seed(self, *caps) -> None:
        self.captures_path.parent.mkdir(parents=True, exist_ok=True)
        self.captures_path.write_text(
            json.dumps({'schema_version': 2, 'captures': list(caps)}, indent=2) + '\n')

    def _read_local(self) -> dict:
        return json.loads(self.captures_path.read_text())


# ==================== auth (401) ====================


class AuthTest(_DelegateTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(_endpoint('cap-1'), headers={'X-Actor': ACTOR}, json={})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.inbox.calls, [])

    def test_missing_actor_returns_401(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'),
                             headers={'X-Dashboard-Token': TOKEN}, json={})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.inbox.calls, [])

    def test_unlisted_actor_returns_401(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(
            _endpoint('cap-1'),
            headers={'X-Dashboard-Token': TOKEN, 'X-Actor': 'mallory@evil.test'},
            json={})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['detail'], 'unauthorized')
        self.assertEqual(self.inbox.calls, [])


# ==================== happy path + required AR fields ====================


class DelegateProposalTest(_DelegateTestBase):
    def test_emits_proposal_with_required_ar_fields(self):
        self._seed(_cap('cap-1', title='Ship the thing',
                        briefing={'what': 'a thing', 'why': 'it matters',
                                  'suggest': 'do the thing first'},
                        recommended_action='delegate'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['dispatched'])
        # response_model_exclude_none drops deduped on a fresh proposal.
        self.assertNotIn('deduped', body)

        self.assertEqual(len(self.inbox.calls), 1)
        call = self.inbox.calls[0]
        self.assertEqual(call['target_agent'], 'beacon')
        self.assertEqual(call['source_agent'], 'dashboard')
        self.assertEqual(call['filename'], 'delegate-cap-1.json')

        env = call['task_dict']
        # Every APPROVAL_REQUEST required field is present + non-empty.
        for field in _AR_REQUIRED:
            self.assertIn(field, env)
            self.assertTrue(env[field], f'{field} must be non-empty')
        self.assertEqual(env['target_agent'], 'beacon')
        self.assertEqual(env['task_id'], 'delegate-cap-1')
        # The capture identity, actor, dedup identity, and timeout ride along.
        self.assertEqual(env['capture_id'], 'cap-1')
        self.assertEqual(env['actor'], ACTOR)
        self.assertEqual(env['dedup_identity'], 'delegate:cap-1')
        self.assertEqual(env['timeout'], 600)
        self.assertEqual(env['source'], 'dashboard')
        # summary derives from briefing.suggest; prompt carries the run-down.
        self.assertEqual(env['summary'], 'do the thing first')
        self.assertIn('Delegate to team', env['prompt'])
        self.assertIn('cap-1', env['prompt'])

    def test_action_defaults_to_recommended_then_delegate(self):
        # No briefing/recommendation, empty body → action defaults to 'delegate'.
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.inbox.calls[0]['task_dict']['action'], 'delegate')

    def test_recommended_action_used_when_body_omits_it(self):
        self._seed(_cap('cap-1', recommended_action='promote'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.inbox.calls[0]['task_dict']['action'], 'promote')

    def test_body_action_overrides_recommendation(self):
        self._seed(_cap('cap-1', recommended_action='promote'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'delegate'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.inbox.calls[0]['task_dict']['action'], 'delegate')

    def test_bodiless_post_succeeds(self):
        # Body is optional (§ 2) — a POST with no JSON body still delegates.
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.inbox.calls), 1)

    def test_invalid_action_returns_400(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.inbox.calls, [])

    def test_capture_stays_parked_and_gets_spawned_ref(self):
        # Phase S (S1): delegating stamps a `spawned` ref back onto the capture
        # (the join key to the work it created) but does NOT change `state` —
        # the card stays parked; the spawned ref is additive.
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertEqual(cap['state'], 'parked')
        self.assertEqual(cap['spawned']['kind'], 'delegate')
        self.assertEqual(cap['spawned']['task_id'], 'delegate-cap-1')
        self.assertIn('stamped_at', cap['spawned'])

    def test_spawned_ref_stamp_is_idempotent_across_dedup_repost(self):
        # A re-POST collapses onto the open proposal (deduped) and re-asserts the
        # spawned ref idempotently — same identity, so stamped_at does not churn.
        self._seed(_cap('cap-1'))
        self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        first_ref = next(
            c for c in self._read_local()['captures'] if c['id'] == 'cap-1'
        )['spawned']
        second = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertTrue(second.json()['deduped'])
        second_ref = next(
            c for c in self._read_local()['captures'] if c['id'] == 'cap-1'
        )['spawned']
        self.assertEqual(first_ref, second_ref)  # no churn on the idempotent re-stamp


# ==================== guards (404 / 409) ====================


class GuardTest(_DelegateTestBase):
    def test_missing_capture_returns_404(self):
        self._seed(_cap('cap-1'))
        r = self.client.post(_endpoint('nope'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()['detail']['error'], 'capture not found')
        self.assertEqual(self.inbox.calls, [])

    def test_non_parked_returns_409(self):
        self._seed(_cap('cap-1', state='promoted'))
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'capture not actionable')
        self.assertEqual(self.inbox.calls, [])


# ==================== idempotency (re-POST dedups) ====================


class DedupTest(_DelegateTestBase):
    def test_repost_collapses_onto_open_proposal(self):
        self._seed(_cap('cap-1'))
        first = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()['dispatched'])
        self.assertNotIn('deduped', first.json())

        # Second POST sees the open proposal in Beacon's inbox → collapse.
        second = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()['dispatched'])
        self.assertTrue(second.json()['deduped'])

        # Only ONE proposal was ever written — no double-propose.
        self.assertEqual(len(self.inbox.calls), 1)

    def test_distinct_captures_each_get_a_proposal(self):
        self._seed(_cap('cap-1'), _cap('cap-2'))
        self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.client.post(_endpoint('cap-2'), headers=AUTH, json={})
        self.assertEqual(len(self.inbox.calls), 2)
        filenames = {c['filename'] for c in self.inbox.calls}
        self.assertEqual(filenames, {'delegate-cap-1.json', 'delegate-cap-2.json'})

    _STANDING_REF = {
        'kind': 'delegate',
        'task_id': 'delegate-cap-1',
        'stamped_at': '2026-07-09T20:05:00+00:00',
    }

    def _with_evidence(self, evidence):
        """Patch the outcome-evidence lookup for one request (the sandbox has
        no pending-approvals store, so the real lookup always returns None)."""
        orig = da._delegation_outcome_evidence
        da._delegation_outcome_evidence = lambda tid, agents_root=None: evidence
        self.addCleanup(
            lambda: setattr(da, '_delegation_outcome_evidence', orig))

    def test_consumed_proposal_with_evidence_short_circuits(self):
        # Durable idempotency (2026-07-11 revert class): the live-inbox dedup
        # window is only minutes — once Beacon consumed the proposal AND the
        # run left durable evidence (open/approved approval), a re-POST returns
        # the prior outcome instead of a fresh paid run. dispatched is honestly
        # False — nothing new was handed to the team.
        self._seed(_cap('cap-1', spawned=dict(self._STANDING_REF)))
        self._with_evidence({'status': 'open', 'approval_id': 'scoped-001',
                             'created_at': '2026-07-09T20:10:00+00:00'})
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body['dispatched'])
        self.assertTrue(body['already_delegated'])
        self.assertEqual(body['delegated_at'], '2026-07-09T20:05:00+00:00')
        self.assertEqual(body['approval_id'], 'scoped-001')
        self.assertEqual(self.inbox.calls, [])  # NO new proposal, no LLM spend

    def test_no_outcome_evidence_redelegates(self):
        # A prior run that left NO durable trace must not wedge the card —
        # the re-click re-delegates (matching the no-outcome alert's advice).
        self._seed(_cap('cap-1', spawned=dict(self._STANDING_REF)))
        self._with_evidence(None)
        r = self.client.post(_endpoint('cap-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['dispatched'])
        self.assertEqual(len(self.inbox.calls), 1)  # fresh proposal written

    def test_force_redelegates_and_refreshes_stamp(self):
        self._seed(_cap('cap-1', spawned=dict(self._STANDING_REF)))
        self._with_evidence({'status': 'open', 'approval_id': 'scoped-001',
                             'created_at': '2026-07-09T20:10:00+00:00'})
        r = self.client.post(_endpoint('cap-1'), headers=AUTH,
                             json={'force': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['dispatched'])
        self.assertNotIn('already_delegated', r.json())
        self.assertEqual(len(self.inbox.calls), 1)  # fresh proposal written
        # delegated_at now reports the FORCED dispatch, not the first click.
        cap = next(c for c in self._read_local()['captures'] if c['id'] == 'cap-1')
        self.assertNotEqual(cap['spawned']['stamped_at'],
                            self._STANDING_REF['stamped_at'])


if __name__ == '__main__':
    unittest.main()
