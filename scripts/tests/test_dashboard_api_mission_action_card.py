#!/usr/bin/env python3
"""Tests for the mission-backed universal action card (Projects v3 P2 Contract B).

A proposed mission-backed funnel card exposes the SAME actions as a parked
capture — Delegate, Snooze, Drop (supersedes bare dismiss), Talk-to-team —
regardless of the backing store. Promote is intentionally ABSENT (deferred to P3).

  Delegate  POST /api/system/missions/{id}/delegate — emits a human-approval-gate
            APPROVAL_REQUEST proposal into Beacon's inbox via safe_write_inbox
            (no missions.json mutation — single-committer / no-dirty-tree).
  Snooze    POST /api/system/missions/{id}/action {action:'snooze'} — PR-backed
            additive `snoozed_until`; the funnel hides it until the snooze passes.
  Drop      POST /api/system/missions/{id}/action {action:'drop'} — the dismiss
            semantics (acknowledged=true, phase stays proposed → stop re-proposing).
  Talk      GET/POST /api/system/missions/{id}/thread + /message — the card_message
            conversation, reusing the same chain_events store keyed by mission_id.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_mission_action_card
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path
from typing import Any, Optional

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


def _mission(mid='m-1', *, phase='proposed', name='Mission One',
             brief='do the thing', repo='ourliberty-agent-core',
             task_ids=None, **extra):
    mission = {
        'id': mid,
        'name': name,
        'phase': phase,
        'brief': brief,
        'spec_docs': [],
        'task_ids': list(task_ids or []),
        'repo': repo,
        'created': '2026-06-01',
        'deferred_reason': None,
    }
    mission.update(extra)
    return mission


# ============================================================================
# Delegate — inbox proposal (+ the additive spawned-ref stamp)
# ============================================================================


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
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-mission-delegate-'))
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'
        self.inboxes_root = self.tmp / 'agents' / 'inboxes'

        self._orig_missions_path = da._missions_json_path
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]

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
            json.dumps({'schema_version': 1, 'missions': list(missions)}, indent=2) + '\n')

    def _endpoint(self, mid: str) -> str:
        return f'/api/system/missions/{mid}/delegate'


class MissionDelegateAuthTest(_DelegateTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.post(self._endpoint('m-1'), headers={'X-Actor': ACTOR}, json={})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.inbox.calls, [])

    def test_missing_actor_returns_401(self):
        self._seed(_mission('m-1'))
        r = self.client.post(self._endpoint('m-1'),
                             headers={'X-Dashboard-Token': TOKEN}, json={})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.inbox.calls, [])


class MissionDelegateProposalTest(_DelegateTestBase):
    def test_emits_proposal_with_required_ar_fields(self):
        self._seed(_mission('m-1', name='Ship the thing', brief='it matters'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['dispatched'])
        self.assertNotIn('deduped', body)  # response_model_exclude_none

        self.assertEqual(len(self.inbox.calls), 1)
        call = self.inbox.calls[0]
        self.assertEqual(call['target_agent'], 'beacon')
        self.assertEqual(call['source_agent'], 'dashboard')
        self.assertEqual(call['filename'], 'delegate-m-1.json')

        env = call['task_dict']
        for field in _AR_REQUIRED:
            self.assertIn(field, env)
            self.assertTrue(env[field], f'{field} must be non-empty')
        self.assertEqual(env['target_agent'], 'beacon')
        self.assertEqual(env['task_id'], 'delegate-m-1')
        self.assertEqual(env['mission_id'], 'm-1')
        self.assertEqual(env['actor'], ACTOR)
        self.assertEqual(env['dedup_identity'], 'delegate:m-1')
        self.assertEqual(env['timeout'], 600)
        self.assertEqual(env['source'], 'dashboard')
        self.assertEqual(env['summary'], 'it matters')
        self.assertIn('Delegate to team', env['prompt'])
        self.assertIn('m-1', env['prompt'])

    def test_action_defaults_to_delegate(self):
        self._seed(_mission('m-1'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.inbox.calls[0]['task_dict']['action'], 'delegate')

    def test_body_action_override(self):
        self._seed(_mission('m-1'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'promote'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.inbox.calls[0]['task_dict']['action'], 'promote')

    def test_invalid_action_returns_400(self):
        self._seed(_mission('m-1'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.inbox.calls, [])

    def test_bodiless_post_succeeds(self):
        self._seed(_mission('m-1'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.inbox.calls), 1)

    def test_delegate_mutates_only_the_spawned_ref(self):
        # Delegate-tracking (2026-07-11): the handler now stamps the additive
        # `spawned` join-key ref onto the mission — the ONLY permitted mutation.
        # Phase and every other field stay untouched (the old "never mutated"
        # invariant, narrowed to everything-but-spawned).
        self._seed(_mission('m-1'))
        before = json.loads(self.missions_path.read_text())
        r = self.client.post(self._endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 200, r.text)
        after = json.loads(self.missions_path.read_text())
        m_after = next(m for m in after['missions'] if m['id'] == 'm-1')
        spawned = m_after.pop('spawned')
        self.assertEqual(spawned['kind'], 'delegate')
        self.assertEqual(spawned['task_id'], 'delegate-m-1')
        m_before = next(m for m in before['missions'] if m['id'] == 'm-1')
        self.assertEqual(m_after, m_before)  # nothing else changed


class MissionDelegateGuardTest(_DelegateTestBase):
    def test_missing_mission_returns_404(self):
        self._seed(_mission('m-1'))
        r = self.client.post(self._endpoint('nope'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not found')
        self.assertEqual(self.inbox.calls, [])

    def test_non_proposed_returns_409(self):
        self._seed(_mission('m-1', phase='drafting'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH, json={})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not proposed')
        self.assertEqual(self.inbox.calls, [])


class MissionDelegateDedupTest(_DelegateTestBase):
    def test_repost_collapses_onto_open_proposal(self):
        self._seed(_mission('m-1'))
        first = self.client.post(self._endpoint('m-1'), headers=AUTH, json={})
        self.assertTrue(first.json()['dispatched'])
        self.assertNotIn('deduped', first.json())

        second = self.client.post(self._endpoint('m-1'), headers=AUTH, json={})
        self.assertTrue(second.json()['dispatched'])
        self.assertTrue(second.json()['deduped'])
        self.assertEqual(len(self.inbox.calls), 1)


# ============================================================================
# Snooze + Drop — PR-backed action verbs
# ============================================================================


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _GithubRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.scripted: list[tuple[str, str, _FakeResponse]] = []

    def script(self, method: str, url_suffix: str, response: _FakeResponse) -> None:
        self.scripted.append((method, url_suffix, response))

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=10.0):
        self.calls.append({'method': method, 'url': url, 'json_body': json_body})
        for idx, (m, suffix, resp) in enumerate(self.scripted):
            if m == method and url.endswith(suffix):
                self.scripted.pop(idx)
                return resp
        raise AssertionError(f'unscripted github call: {method} {url}')


def _mission_edit_github_script(gh: _GithubRecorder, *, branch: str,
                                pr_url: str = 'https://github.com/test-owner/test-repo/pull/77') -> None:
    gh.script('GET', '/git/refs/heads/main', _FakeResponse(200, {'object': {'sha': 'main-sha'}}))
    gh.script('POST', '/git/refs', _FakeResponse(201, {'ref': f'refs/heads/{branch}'}))
    gh.script('GET', f'/contents/agents/beacon/missions.json?ref={branch}',
              _FakeResponse(200, {'sha': 'missions-blob'}))
    gh.script('PUT', '/contents/agents/beacon/missions.json',
              _FakeResponse(200, {'content': {'sha': 'new-missions-blob'}}))
    gh.script('POST', '/pulls', _FakeResponse(201, {'html_url': pr_url, 'number': 77}))


class _MissionPRTestBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self._prev_gh_token = os.environ.get('GITHUB_TOKEN')
        os.environ['GITHUB_TOKEN'] = 'test-gh-token'
        self._prev_repo = os.environ.get('OURLIBERTY_MISSIONS_REPO')
        os.environ['OURLIBERTY_MISSIONS_REPO'] = 'test-owner/test-repo'

        self.tmp = Path(tempfile.mkdtemp(prefix='dash-mission-card-'))
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'

        self._orig_missions_path = da._missions_json_path
        self._orig_github = da._github_api_request
        self._orig_gh_token = da._github_token
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        self.gh = _GithubRecorder()
        da._github_api_request = self.gh  # type: ignore[assignment]

        self.client = TestClient(da.app)

    def tearDown(self):
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        da._github_api_request = self._orig_github  # type: ignore[assignment]
        da._github_token = self._orig_gh_token  # type: ignore[assignment]
        if self._prev_gh_token is None:
            os.environ.pop('GITHUB_TOKEN', None)
        else:
            os.environ['GITHUB_TOKEN'] = self._prev_gh_token
        if self._prev_repo is None:
            os.environ.pop('OURLIBERTY_MISSIONS_REPO', None)
        else:
            os.environ['OURLIBERTY_MISSIONS_REPO'] = self._prev_repo

    def _seed(self, *missions) -> None:
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)}, indent=2) + '\n')

    def _endpoint(self, mid: str) -> str:
        return f'/api/system/missions/{mid}/action'

    def _put_entry(self, mission_id: str) -> dict:
        put = next(c for c in self.gh.calls if c['method'] == 'PUT')
        registry = json.loads(base64.b64decode(put['json_body']['content']).decode('utf-8'))
        return next(m for m in registry['missions'] if m['id'] == mission_id)


class MissionSnoozeTest(_MissionPRTestBase):
    def _future(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    def test_opens_pr_setting_snoozed_until(self):
        self._seed(_mission('m-1', phase='proposed'))
        branch = 'chore/snooze-mission-m-1'
        _mission_edit_github_script(self.gh, branch=branch)
        until = self._future()
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': until})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['branch'], branch)
        self.assertEqual(len(self.gh.calls), 5)

        entry = self._put_entry('m-1')
        self.assertEqual(entry['snoozed_until'], until)
        self.assertEqual(entry['phase'], 'proposed')  # additive; phase unchanged

    def test_null_clears_snooze(self):
        self._seed(_mission('m-1', phase='proposed', snoozed_until=self._future()))
        _mission_edit_github_script(self.gh, branch='chore/snooze-mission-m-1')
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': None})
        self.assertEqual(r.status_code, 200, r.text)
        entry = self._put_entry('m-1')
        self.assertIsNone(entry['snoozed_until'])

    def test_past_date_returns_400_no_github(self):
        self._seed(_mission('m-1', phase='proposed'))
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': past})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_malformed_date_returns_400_no_github(self):
        self._seed(_mission('m-1', phase='proposed'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': 'not-a-date'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_non_proposed_returns_409_no_github(self):
        self._seed(_mission('m-1', phase='drafting'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': self._future()})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not proposed')
        self.assertEqual(self.gh.calls, [])

    def test_missing_mission_returns_404_no_github(self):
        self._seed(_mission('m-1', phase='proposed'))
        r = self.client.post(self._endpoint('nope'), headers=AUTH,
                             json={'action': 'snooze', 'snoozed_until': self._future()})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])

    def test_local_missions_not_mutated(self):
        self._seed(_mission('m-1', phase='proposed'))
        before = self.missions_path.read_text()
        _mission_edit_github_script(self.gh, branch='chore/snooze-mission-m-1')
        self.client.post(self._endpoint('m-1'), headers=AUTH,
                         json={'action': 'snooze', 'snoozed_until': self._future()})
        self.assertEqual(self.missions_path.read_text(), before)


class MissionDropTest(_MissionPRTestBase):
    """Drop supersedes bare dismiss: it MUST preserve dismiss semantics exactly —
    acknowledged=true, phase stays proposed (so the autoregister healer never
    re-proposes), on the SAME dismiss branch."""

    def test_drop_is_dismiss_semantics(self):
        self._seed(_mission('proposed-orphan-x', phase='proposed', task_ids=['orphan-x']))
        branch = 'chore/dismiss-mission-proposed-orphan-x'
        _mission_edit_github_script(self.gh, branch=branch)
        r = self.client.post(self._endpoint('proposed-orphan-x'), headers=AUTH,
                             json={'action': 'drop'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['branch'], branch)

        entry = self._put_entry('proposed-orphan-x')
        self.assertIs(entry['acknowledged'], True)
        self.assertEqual(entry['phase'], 'proposed')
        self.assertEqual(entry['task_ids'], ['orphan-x'])  # stays registered

    def test_drop_non_proposed_returns_409_no_github(self):
        self._seed(_mission('m-1', phase='drafting'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['error'], 'mission not proposed')
        self.assertEqual(self.gh.calls, [])

    def test_drop_missing_mission_returns_404_no_github(self):
        self._seed(_mission('m-1', phase='proposed'))
        r = self.client.post(self._endpoint('nope'), headers=AUTH, json={'action': 'drop'})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.gh.calls, [])


class MissionNoPromoteTest(_MissionPRTestBase):
    """Promote is intentionally ABSENT on the mission card (deferred to P3)."""

    def test_promote_action_returns_400(self):
        self._seed(_mission('m-1', phase='proposed'))
        r = self.client.post(self._endpoint('m-1'), headers=AUTH,
                             json={'action': 'promote'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.gh.calls, [])


# ============================================================================
# Talk-to-team — thread + message keyed by mission_id
# ============================================================================


class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _ThreadClient:
    def __init__(self, rows: Optional[list[dict[str, Any]]] = None):
        self.rows = list(rows or [])
        self.upserts: list[dict[str, Any]] = []
        self._op: Optional[str] = None
        self._in_vals: list[str] = []
        self._upsert_rows: list[dict[str, Any]] = []
        self._upsert_kwargs: dict[str, Any] = {}

    def table(self, name: str):
        self._op = None
        self._in_vals = []
        self._upsert_rows = []
        self._upsert_kwargs = {}
        return self

    def select(self, cols: str = '*'):
        self._op = 'select'
        return self

    def in_(self, col: str, vals: list[str]):
        self._in_vals = list(vals)
        return self

    def order(self, col: str, desc: bool = False):
        return self

    def upsert(self, rows: list[dict[str, Any]], **kwargs):
        self._op = 'upsert'
        self._upsert_rows = rows
        self._upsert_kwargs = kwargs
        return self

    def execute(self):
        if self._op == 'select':
            return _Resp([r for r in self.rows if r.get('task_id') in self._in_vals])
        if self._op == 'upsert':
            existing = {r.get('event_id') for r in self.rows}
            for row in self._upsert_rows:
                self.upserts.append(row)
                if (self._upsert_kwargs.get('ignore_duplicates')
                        and row.get('event_id') in existing):
                    continue
                self.rows.append(row)
            return _Resp([])
        return _Resp([])


class _MissionThreadBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-mission-thread-'))
        (self.tmp / 'inboxes' / 'beacon').mkdir(parents=True, exist_ok=True)
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'

        self._orig_missions = da._missions_json_path
        self._orig_agents = da._agents_root
        self._orig_client = da._get_larry_action_supabase_client
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        da._agents_root = lambda: self.tmp  # type: ignore[assignment]
        self.client_stub = _ThreadClient()
        da._get_larry_action_supabase_client = lambda: self.client_stub  # type: ignore[assignment]
        self.c = TestClient(da.app)

    def tearDown(self):
        da._missions_json_path = self._orig_missions  # type: ignore[assignment]
        da._agents_root = self._orig_agents  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_client  # type: ignore[assignment]

    def _seed(self, *missions):
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)}) + '\n')

    def _msg_row(self, mid, event_id, ts, *, direction='larry_to_team', text='hi'):
        return {
            'event_id': event_id, 'ts': ts, 'agent': ACTOR,
            'event_type': 'card_message', 'task_id': mid, 'actor': ACTOR,
            'payload': {'mission_id': mid, 'direction': direction,
                        'text': text, 'actor': ACTOR, 'needs_reply': True},
        }

    def _thread_url(self, mid):
        return f'/api/system/missions/{mid}/thread'

    def _message_url(self, mid):
        return f'/api/system/missions/{mid}/message'

    def _beacon_inbox(self) -> Path:
        return self.tmp / 'inboxes' / 'beacon'


class MissionThreadGetTest(_MissionThreadBase):
    def test_missing_token_401(self):
        r = self.c.get(self._thread_url('m-1'))
        self.assertEqual(r.status_code, 401)

    def test_missing_mission_404(self):
        self._seed(_mission('m-1'))
        r = self.c.get(self._thread_url('nope'), headers=AUTH)
        self.assertEqual(r.status_code, 404)

    def test_empty_thread_for_known_mission(self):
        self._seed(_mission('m-1'))
        r = self.c.get(self._thread_url('m-1'), headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['mission_id'], 'm-1')
        self.assertEqual(body['messages'], [])
        self.assertIn('last_synced_at', body)

    def test_returns_card_messages_oldest_first(self):
        self._seed(_mission('m-1'))
        self.client_stub.rows = [
            self._msg_row('m-1', 'e2', '2026-06-14T02:00:00+00:00',
                          direction='team_to_larry', text='second'),
            self._msg_row('m-1', 'e1', '2026-06-14T01:00:00+00:00',
                          direction='larry_to_team', text='first'),
        ]
        r = self.c.get(self._thread_url('m-1'), headers=AUTH)
        msgs = r.json()['messages']
        self.assertEqual([m['text'] for m in msgs], ['first', 'second'])


class MissionMessagePostTest(_MissionThreadBase):
    def test_missing_actor_401(self):
        self._seed(_mission('m-1'))
        r = self.c.post(self._message_url('m-1'),
                        headers={'X-Dashboard-Token': TOKEN}, json={'text': 'hi'})
        self.assertEqual(r.status_code, 401)

    def test_unknown_mission_404(self):
        self._seed(_mission('m-1'))
        r = self.c.post(self._message_url('nope'), headers=AUTH, json={'text': 'hi'})
        self.assertEqual(r.status_code, 404)

    def test_empty_text_400(self):
        self._seed(_mission('m-1'))
        r = self.c.post(self._message_url('m-1'), headers=AUTH, json={'text': '   '})
        self.assertEqual(r.status_code, 400)

    def test_post_emits_event_envelope_and_resolves_doorbell(self):
        self._seed(_mission('m-1', name='My mission'))
        with mock.patch('missions_doorbell.resolve_doorbell',
                        return_value={'resolved': True}) as resolve:
            r = self.c.post(self._message_url('m-1'), headers=AUTH,
                            json={'text': 'what about X?'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['posted'])
        self.assertEqual(body['direction'], 'larry_to_team')
        self.assertTrue(body['doorbell_resolved'])

        self.assertEqual(len(self.client_stub.upserts), 1)
        row = self.client_stub.upserts[0]
        self.assertEqual(row['event_type'], 'card_message')
        self.assertEqual(row['task_id'], 'm-1')
        self.assertEqual(row['actor'], ACTOR)
        self.assertEqual(row['payload']['direction'], 'larry_to_team')
        self.assertEqual(row['payload']['mission_id'], 'm-1')
        self.assertEqual(row['payload']['text'], 'what about X?')

        envelopes = list(self._beacon_inbox().glob('card-message-*.json'))
        self.assertEqual(len(envelopes), 1)
        env = json.loads(envelopes[0].read_text())
        self.assertEqual(env['source'], 'dashboard')
        self.assertEqual(env['mission_id'], 'm-1')
        self.assertNotIn('capture_id', env)
        self.assertIn('what about X?', env['prompt'])
        self.assertIn('/api/system/missions/m-1/thread', env['prompt'])
        self.assertEqual(env['dedup_identity'], f"card-message:{row['event_id']}")
        resolve.assert_called_once()

    def test_supabase_unavailable_503(self):
        self._seed(_mission('m-1'))
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        r = self.c.post(self._message_url('m-1'), headers=AUTH, json={'text': 'hi'})
        self.assertEqual(r.status_code, 503)

    def test_doorbell_resolve_failure_does_not_500(self):
        self._seed(_mission('m-1'))
        with mock.patch('missions_doorbell.resolve_doorbell',
                        side_effect=RuntimeError('boom')):
            r = self.c.post(self._message_url('m-1'), headers=AUTH, json={'text': 'hi'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['doorbell_resolved'])


# ============================================================================
# Funnel snooze filter (unit) — a snoozed proposed mission hides
# ============================================================================


class FunnelSnoozeFilterTest(unittest.TestCase):
    def _now(self) -> datetime:
        return datetime(2026, 6, 16, tzinfo=timezone.utc)

    def test_snoozed_proposed_mission_hidden(self):
        future = (self._now() + timedelta(days=2)).isoformat()
        m = _mission('m-1', phase='proposed', proposed_by='beacon', snoozed_until=future)
        funnel = da._build_funnel([m], [], [], self._now())
        refs = {i['ref'] for i in funnel['primary'] + funnel['secondary']}
        self.assertNotIn('m-1', refs)

    def test_unsnoozed_proposed_mission_visible(self):
        m = _mission('m-1', phase='proposed', proposed_by='beacon')
        funnel = da._build_funnel([m], [], [], self._now())
        refs = {i['ref'] for i in funnel['primary'] + funnel['secondary']}
        self.assertIn('m-1', refs)

    def test_past_snooze_resurfaces(self):
        past = (self._now() - timedelta(days=2)).isoformat()
        m = _mission('m-1', phase='proposed', proposed_by='beacon', snoozed_until=past)
        funnel = da._build_funnel([m], [], [], self._now())
        refs = {i['ref'] for i in funnel['primary'] + funnel['secondary']}
        self.assertIn('m-1', refs)

    def test_snoozed_orphan_derived_proposed_hidden_from_secondary(self):
        future = (self._now() + timedelta(days=2)).isoformat()
        m = _mission('orphan-x', phase='proposed', snoozed_until=future)  # no proposed_by
        funnel = da._build_funnel([m], [], [], self._now())
        refs = {i['ref'] for i in funnel['secondary']}
        self.assertNotIn('orphan-x', refs)


if __name__ == '__main__':
    unittest.main()
