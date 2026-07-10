#!/usr/bin/env python3
"""Tests for the shared "Talk to the team" collapsed-card badge projection
(_team_reply_fields / _project_team_reply_fields) and its use on the operator
queue.

The dashboard reads two FLAT fields per item to badge a collapsed thread:
  * latest_team_message_id — the newest team_to_larry reply's stable event_id
  * blocked_on_you         — that reply's needs_reply (loud vs quiet)
derived from the SAME newest-reply rule the funnel doorbell uses. Contract:
  - no team reply → None / False (never unread)
  - a trailing larry turn never masks the earlier team reply
  - fail-safe: a None client / read error degrades to the neutral fields, the
    feed never 500s
  - the operator-queue endpoint round-trips the fields through OperatorQueueEntry

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_team_reply_fields
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ['DASHBOARD_API_TOKEN'] = TOKEN

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}


def _cm(event_id: str, direction: str, needs_reply=None, ts: str = 't') -> dict:
    """A card_message chain_event as the fetch returns it (newest-first)."""
    return {
        'event_id': event_id,
        'event_type': 'card_message',
        'ts': ts,
        'payload': {'direction': direction, 'needs_reply': needs_reply,
                    'text': 'hi', 'actor': 'beacon'},
    }


class TeamReplyFieldsTest(unittest.TestCase):
    def test_no_events_is_never_unread(self) -> None:
        self.assertEqual(
            da._team_reply_fields([]),
            {'latest_team_message_id': None, 'blocked_on_you': False})

    def test_newest_team_reply_blocked(self) -> None:
        out = da._team_reply_fields([_cm('m2', 'team_to_larry', True)])
        self.assertEqual(out['latest_team_message_id'], 'm2')
        self.assertTrue(out['blocked_on_you'])

    def test_team_reply_not_blocked_when_needs_reply_absent(self) -> None:
        # An FYI reply (needs_reply None/absent) is a quiet dot, not a doorbell.
        out = da._team_reply_fields([_cm('m2', 'team_to_larry', None)])
        self.assertEqual(out['latest_team_message_id'], 'm2')
        self.assertFalse(out['blocked_on_you'])

    def test_trailing_larry_turn_does_not_mask_the_team_reply(self) -> None:
        out = da._team_reply_fields([
            _cm('m3', 'larry_to_team', None),   # newest
            _cm('m2', 'team_to_larry', True),   # the reply we want
            _cm('m1', 'larry_to_team', None),
        ])
        self.assertEqual(out['latest_team_message_id'], 'm2')
        self.assertTrue(out['blocked_on_you'])

    def test_only_larry_turns_is_never_unread(self) -> None:
        out = da._team_reply_fields([_cm('m1', 'larry_to_team', None)])
        self.assertIsNone(out['latest_team_message_id'])
        self.assertFalse(out['blocked_on_you'])


class ProjectTeamReplyFieldsTest(unittest.TestCase):
    def test_projects_per_item_keyed_by_id_of(self) -> None:
        items = [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]
        canned = {
            'a': [_cm('a1', 'team_to_larry', True)],
            'b': [_cm('b1', 'team_to_larry', None)],
            # 'c' has no team reply
        }
        with mock.patch.object(da, '_fetch_events_for_task_ids',
                               return_value=canned):
            da._project_team_reply_fields(items, lambda it: it.get('id'),
                                          object())
        self.assertEqual(items[0]['latest_team_message_id'], 'a1')
        self.assertTrue(items[0]['blocked_on_you'])
        self.assertEqual(items[1]['latest_team_message_id'], 'b1')
        self.assertFalse(items[1]['blocked_on_you'])
        self.assertIsNone(items[2]['latest_team_message_id'])
        self.assertFalse(items[2]['blocked_on_you'])

    def test_read_error_degrades_to_neutral_fields_not_raise(self) -> None:
        items = [{'id': 'a'}]
        with mock.patch.object(da, '_fetch_events_for_task_ids',
                               side_effect=RuntimeError('boom')):
            da._project_team_reply_fields(items, lambda it: it.get('id'),
                                          object())
        self.assertIsNone(items[0]['latest_team_message_id'])
        self.assertFalse(items[0]['blocked_on_you'])

    def test_no_usable_ids_stamps_neutral_without_a_read(self) -> None:
        items = [{'id': None}, {'id': ''}]
        with mock.patch.object(da, '_fetch_events_for_task_ids') as fetch:
            da._project_team_reply_fields(items, lambda it: it.get('id'),
                                          object())
            fetch.assert_not_called()
        for it in items:
            self.assertIsNone(it['latest_team_message_id'])
            self.assertFalse(it['blocked_on_you'])


class OperatorQueueDoorbellTest(unittest.TestCase):
    """The endpoint round-trips the flat fields through OperatorQueueEntry."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='operator-doorbell-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        self.client = TestClient(da.app)
        self.rank_file = Path(self._tmpdir) / 'state' / 'mission-rank.json'
        self.rank_file.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _entry(self, i: int) -> dict:
        return {'id': f'm{i}', 'name': f'Card {i}', 'weight': 0.2, 'benefit': 8,
                'cost': 3, 'rank_score': 10.0 - i, 'scored': True,
                'risk': 'safe', 'kind': 'mission', 'brief': {}}

    def test_ranked_cards_carry_the_team_reply_fields(self) -> None:
        self.rank_file.write_text(json.dumps(
            {'mode': 'shadow', 'ranked': [self._entry(1), self._entry(2)]}),
            encoding='utf-8')
        canned = {'m1': [_cm('r1', 'team_to_larry', True)]}  # m2 has no reply
        with mock.patch.object(da, '_get_larry_action_supabase_client',
                               return_value=object()), \
             mock.patch.object(da, '_fetch_events_for_task_ids',
                               return_value=canned):
            r = self.client.get('/api/approvals/operator-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        ranked = r.json()['ranked']
        by_id = {e['id']: e for e in ranked}
        self.assertEqual(by_id['m1']['latest_team_message_id'], 'r1')
        self.assertTrue(by_id['m1']['blocked_on_you'])
        # A card with no team reply serializes the neutral defaults, not absent.
        self.assertIsNone(by_id['m2']['latest_team_message_id'])
        self.assertFalse(by_id['m2']['blocked_on_you'])


class PipelinePhaseDoorbellTest(unittest.TestCase):
    """End-to-end through `_handle_missions_derived`: a pipeline phase card gets
    the team-reply fields keyed by its composite thread ref project_id::phase_id,
    and the internal _project_id join key is stripped back off."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix='pipeline-doorbell-'))
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._missions = self._tmp / 'missions.json'
        self._captures = self._tmp / 'captures.json'
        self._projects = self._tmp / 'projects.json'
        self._seq_root = self._tmp / 'build-sequences'
        self._seq_root.mkdir()
        self._missions.write_text(json.dumps(
            {'schema_version': 1, 'missions': [], 'last_synced_at': None}))
        self._captures.write_text(json.dumps(
            {'schema_version': 1, 'captures': []}))
        self._projects.write_text(json.dumps({'projects': [{
            'id': 'proj-x', 'title': 'X', 'state': 'active',
            'phases': [{'id': 'phase-1', 'title': 'P1',
                        'lifecycle_state': 'brainstorm',
                        'desired_end_state': 'ship it'}],
        }]}))

    def _derive(self, canned: dict) -> dict:
        with mock.patch.object(da, '_fetch_events_for_task_ids',
                               return_value=canned), \
             mock.patch.object(da, '_fetch_recent_chain_events',
                               return_value=[]):
            return da._handle_missions_derived(
                missions_path=self._missions, captures_path=self._captures,
                supabase_client=object(), repo=None, task_id=None,
                projects_path=self._projects,
                build_sequences_root=self._seq_root)

    def test_phase_gets_fields_keyed_by_phase_ref(self) -> None:
        out = self._derive(
            {'proj-x::phase-1': [_cm('p1', 'team_to_larry', True)]})
        phase = out['pipeline'][0]['phases'][0]
        self.assertEqual(phase['latest_team_message_id'], 'p1')
        self.assertTrue(phase['blocked_on_you'])
        # the internal join key never leaks onto the wire
        self.assertNotIn('_project_id', phase)

    def test_phase_without_a_reply_is_neutral_and_clean(self) -> None:
        out = self._derive({})
        phase = out['pipeline'][0]['phases'][0]
        self.assertIsNone(phase['latest_team_message_id'])
        self.assertFalse(phase['blocked_on_you'])
        self.assertNotIn('_project_id', phase)


class FunnelLaneDoorbellTest(unittest.TestCase):
    """The funnel card reads parked/suggested/orphan off /api/missions/derived,
    so the badge fields are projected there (keyed by capture_id / id / task_id).
    Covers the parked lane end-to-end through `_handle_missions_derived`."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix='funnel-doorbell-'))
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._missions = self._tmp / 'missions.json'
        self._captures = self._tmp / 'captures.json'
        self._seq_root = self._tmp / 'build-sequences'
        self._seq_root.mkdir()
        self._missions.write_text(json.dumps(
            {'schema_version': 1, 'missions': [], 'last_synced_at': None}))
        self._captures.write_text(json.dumps({'schema_version': 1, 'captures': [{
            'id': 'cap-1', 'title': 'Idea', 'state': 'parked',
            'last_touched': '2026-06-01T00:00:00+00:00',
            'origin': {'repo': 'ourliberty-agent-core'},
        }]}))

    def _derive(self, canned: dict) -> dict:
        with mock.patch.object(da, '_fetch_events_for_task_ids',
                               return_value=canned), \
             mock.patch.object(da, '_fetch_recent_chain_events',
                               return_value=[]):
            return da._handle_missions_derived(
                missions_path=self._missions, captures_path=self._captures,
                supabase_client=object(), repo=None, task_id=None,
                build_sequences_root=self._seq_root)

    def test_parked_funnel_item_gets_team_reply_fields(self) -> None:
        out = self._derive({'cap-1': [_cm('c1', 'team_to_larry', True)]})
        parked = out['parked']
        self.assertTrue(parked, 'expected the parked capture in the funnel')
        self.assertEqual(parked[0]['capture_id'], 'cap-1')
        self.assertEqual(parked[0]['latest_team_message_id'], 'c1')
        self.assertTrue(parked[0]['blocked_on_you'])

    def test_parked_without_reply_is_neutral(self) -> None:
        out = self._derive({})
        parked = out['parked']
        self.assertTrue(parked)
        self.assertIsNone(parked[0]['latest_team_message_id'])
        self.assertFalse(parked[0]['blocked_on_you'])


if __name__ == '__main__':
    unittest.main()
