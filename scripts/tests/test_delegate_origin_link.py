#!/usr/bin/env python3
"""Delegate-tracking Slice 1 — link a parked delegated card to its OPEN approval.

Write side (`beacon_approval_handler`): the origin envelope task_id
(`delegate-<cid>`) is stamped ADDITIVELY onto the pending-approval entry, only
when distinct from the marker id — the Forge dispatch id (the marker task_id) is
never touched. (The approval_request chain_event join is deferred to Slice 2,
when a reader for it exists.)

Read side (`dashboard_api`): `_open_delegate_approvals` reads the authoritative
pending store and `_delegation_needs_you_field` / `_parked_from_captures` surface
`delegation_needs_you` on a delegated card iff a still-open approval joins.

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_delegate_origin_link
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import beacon_approval_handler as ah  # noqa: E402
import dashboard_api as da            # noqa: E402

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
DELEGATE_ID = 'delegate-cap-fix-the-thing-ab12'
MARKER_ID = 'fix-the-thing-001'  # Beacon's fresh LLM-authored id


def _payload(task_id=MARKER_ID):
    return {
        'task_id': task_id,
        'summary': 'do the thing',
        'target_agent': 'forge',
        'prompt': 'build it',
    }


# ---------- write side: add_pending ----------


class AddPendingOriginTest(unittest.TestCase):
    def _add(self, **kw):
        # own=False path (explicit state) → no disk, no lock.
        state = {'pending': []}
        return ah.add_pending(_payload(), chat_id=1, state=state, **kw), state

    def test_distinct_origin_is_stamped(self):
        entry, state = self._add(origin_task_id=DELEGATE_ID)
        self.assertEqual(entry['origin_task_id'], DELEGATE_ID)
        # the marker id (Forge envelope id) is untouched
        self.assertEqual(entry['id'], MARKER_ID)
        self.assertEqual(state['pending'][0]['origin_task_id'], DELEGATE_ID)

    def test_origin_equal_to_marker_is_not_stamped(self):
        # a plain pulse dispatch where envelope==marker gets no self-reference
        entry, _ = self._add(origin_task_id=MARKER_ID)
        self.assertNotIn('origin_task_id', entry)

    def test_no_origin_is_backward_compatible(self):
        entry, _ = self._add()
        self.assertNotIn('origin_task_id', entry)


# ---------- write side: build_approval_request_chain_event ----------
# The approval_request chain_event carries origin_task_id so the needs-you card
# (PendingCard) can say "from your delegation of <card>". Re-added once PendingCard
# became a real reader (it was dropped in #935 as dead surface).


class ChainEventOriginTest(unittest.TestCase):
    def test_distinct_origin_rides_the_payload(self):
        ev = ah.build_approval_request_chain_event(
            _payload(), ts='2026-07-12T00:00:00Z', origin_task_id=DELEGATE_ID)
        self.assertEqual(ev['payload']['origin_task_id'], DELEGATE_ID)
        # event key + dispatch identity remain the marker id — dispatch unchanged
        self.assertEqual(ev['task_id'], MARKER_ID)
        self.assertEqual(ev['payload']['dedup_identity'], MARKER_ID)

    def test_no_origin_leaves_payload_clean(self):
        ev = ah.build_approval_request_chain_event(
            _payload(), ts='2026-07-12T00:00:00Z')
        self.assertNotIn('origin_task_id', ev['payload'])

    def test_origin_equal_to_marker_not_stamped(self):
        ev = ah.build_approval_request_chain_event(
            _payload(), ts='2026-07-12T00:00:00Z', origin_task_id=MARKER_ID)
        self.assertNotIn('origin_task_id', ev['payload'])


# ---------- read side: _open_delegate_approvals ----------


class OpenDelegateApprovalsTest(unittest.TestCase):
    def setUp(self):
        self._root = Path(tempfile.mkdtemp(prefix='delegate_link_'))
        (self._root / 'state').mkdir(parents=True)
        self._path = self._root / 'state' / 'beacon-pending-approvals.json'

    def _write(self, pending):
        self._path.write_text(json.dumps({'pending': pending}))

    def test_matches_open_pending_by_origin(self):
        self._write([
            {'id': MARKER_ID, 'status': 'pending',
             'origin_task_id': DELEGATE_ID, 'created_at': NOW.isoformat()},
        ])
        got = da._open_delegate_approvals([DELEGATE_ID], agents_root=self._root)
        self.assertEqual(got[DELEGATE_ID]['approval_id'], MARKER_ID)
        self.assertEqual(got[DELEGATE_ID]['created_at'], NOW.isoformat())

    def test_ignores_non_pending_status(self):
        self._write([
            {'id': MARKER_ID, 'status': 'approved',
             'origin_task_id': DELEGATE_ID},
        ])
        self.assertEqual(
            da._open_delegate_approvals([DELEGATE_ID], agents_root=self._root), {})

    def test_ignores_unrelated_origin(self):
        self._write([
            {'id': MARKER_ID, 'status': 'pending',
             'origin_task_id': 'delegate-something-else'},
        ])
        self.assertEqual(
            da._open_delegate_approvals([DELEGATE_ID], agents_root=self._root), {})

    def test_missing_store_degrades_to_empty(self):
        self.assertEqual(
            da._open_delegate_approvals([DELEGATE_ID], agents_root=self._root), {})

    def test_empty_input_is_empty(self):
        self._write([{'id': MARKER_ID, 'status': 'pending',
                      'origin_task_id': DELEGATE_ID}])
        self.assertEqual(da._open_delegate_approvals([], agents_root=self._root), {})


# ---------- read side: field + parked integration ----------


def _delegate_cap():
    return {
        'id': 'cap-fix-the-thing-ab12', 'state': 'parked',
        'title': 'Fix the thing',
        'spawned': {'kind': 'delegate', 'task_id': DELEGATE_ID},
    }


class DelegationNeedsYouFieldTest(unittest.TestCase):
    def test_delegate_card_with_open_approval(self):
        m = {DELEGATE_ID: {'approval_id': MARKER_ID, 'created_at': NOW.isoformat()}}
        got = da._delegation_needs_you_field(_delegate_cap(), m)
        self.assertEqual(got['delegation_needs_you']['approval_id'], MARKER_ID)

    def test_delegate_card_without_open_approval_is_none(self):
        got = da._delegation_needs_you_field(_delegate_cap(), {})
        self.assertIsNone(got['delegation_needs_you'])

    def test_non_delegate_card_is_none(self):
        cap = {'id': 'c', 'state': 'parked',
               'spawned': {'kind': 'orphan', 'task_id': 't-1'}}
        m = {'t-1': {'approval_id': 'x'}}
        self.assertIsNone(
            da._delegation_needs_you_field(cap, m)['delegation_needs_you'])

    def test_no_spawned_ref_is_none(self):
        self.assertIsNone(
            da._delegation_needs_you_field(
                {'id': 'c', 'state': 'parked'}, None)['delegation_needs_you'])


class ParkedIntegrationTest(unittest.TestCase):
    def test_parked_surfaces_needs_you(self):
        m = {DELEGATE_ID: {'approval_id': MARKER_ID, 'created_at': NOW.isoformat()}}
        parked = da._parked_from_captures(
            [_delegate_cap()], NOW, {}, open_delegate_approvals=m)
        self.assertEqual(len(parked), 1)
        self.assertEqual(
            parked[0]['delegation_needs_you']['approval_id'], MARKER_ID)

    def test_parked_neutral_without_map(self):
        parked = da._parked_from_captures([_delegate_cap()], NOW, {})
        self.assertIsNone(parked[0]['delegation_needs_you'])


if __name__ == '__main__':
    unittest.main()
