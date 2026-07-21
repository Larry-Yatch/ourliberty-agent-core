#!/usr/bin/env python3
"""Delegate-thread-narrator — the folded GC-tick sweep.

`heal_missions_card_gc.author_delegate_thread_narration` narrates each DELEGATED
capture's current phase as ONE FYI `team_to_larry` card_message, keyed by a
deterministic (capture_id + phase) event_id so a re-tick is a no-op. These tests
pin: per-phase single-post idempotency, skipped-phase-no-phantom, no-PR never
merged, the closed_failed line, FYI (needs_reply False), and that the sweep NEVER
mutates the captures registry (single-writer invariant).

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_delegate_thread_narration
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api as da  # noqa: E402
import heal_missions_card_gc as h  # noqa: E402

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
DELEGATE_ID = 'delegate-cap-fix-the-thing-ab12'
PR = 'https://github.com/o/r/pull/7'


def _ev(event_type, *, pr_url=None):
    return {'event_type': event_type, 'pr_url': pr_url,
            'ts': '2026-07-21T11:00:00Z',
            'payload': {'origin_task_id': DELEGATE_ID}}


def _cap(cid='cap-fix-the-thing-ab12', **over):
    cap = {'id': cid, 'state': 'parked', 'title': 'Fix the thing',
           'spawned': {'kind': 'delegate', 'task_id': DELEGATE_ID}}
    cap.update(over)
    return cap


class _Resp:
    def __init__(self, data):
        self.data = data


class _EmitClient:
    """Minimal supabase stub recording chain_events upserts, deduping on
    event_id when ignore_duplicates is set (mirrors the real upsert)."""

    def __init__(self):
        self.rows: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []
        self.upsert_kwargs: list[dict[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self._kwargs: dict[str, Any] = {}

    def table(self, name):
        self._rows = []
        self._kwargs = {}
        return self

    def upsert(self, rows, **kwargs):
        self._rows = rows
        self._kwargs = kwargs
        return self

    def execute(self):
        existing = {r.get('event_id') for r in self.rows}
        self.upsert_kwargs.append(self._kwargs)
        for row in self._rows:
            self.upserts.append(row)
            if (self._kwargs.get('ignore_duplicates')
                    and row.get('event_id') in existing):
                continue
            self.rows.append(row)
        return _Resp([])


# ---------- pure planner ----------


class PlanTest(unittest.TestCase):
    def _plan(self, caps, m=None, **kw):
        kw.setdefault('open_delegate_approvals', {})
        kw.setdefault('native_build_events', None)
        return h.plan_delegate_narrations(
            caps, build_events_by_origin=m, **kw)

    def test_in_review_one_post(self):
        posts = self._plan([_cap()], {DELEGATE_ID: [_ev('review_request')]})
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['phase'], 'in_review')
        self.assertEqual(posts[0]['capture_id'], 'cap-fix-the-thing-ab12')
        self.assertIn('review', posts[0]['text'].lower())

    def test_non_delegate_card_skipped(self):
        orphan = {'id': 'c', 'state': 'parked',
                  'spawned': {'kind': 'orphan', 'task_id': 't-1'}}
        self.assertEqual(self._plan([orphan], {'t-1': [_ev('review_request')]}),
                         [])

    def test_none_phase_no_post(self):
        # An orphan-only board → nothing to narrate.
        self.assertEqual(self._plan([_cap(spawned={'kind': 'delegate'})], {}),
                         [])

    def test_handed_off_one_post(self):
        posts = self._plan([_cap()], {})
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['phase'], 'handed_off')

    def test_skipped_phase_only_current_narrated(self):
        # Card jumped straight to in_review: exactly ONE post (in_review), no
        # phantom handed_off/building backfill for the phases it skipped.
        posts = self._plan([_cap()], {DELEGATE_ID: [_ev('review_request')]})
        self.assertEqual([p['phase'] for p in posts], ['in_review'])

    def test_review_passed_carries_pr_link(self):
        posts = self._plan([_cap()],
                           {DELEGATE_ID: [_ev('review_pass', pr_url=PR)]})
        self.assertEqual(posts[0]['phase'], 'review_passed')
        self.assertIn(PR, posts[0]['text'])

    def test_merged_no_pr_never_claims_link(self):
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'merged'})
        posts = self._plan([cap], {})
        self.assertEqual(posts[0]['phase'], 'merged')
        self.assertNotIn('http', posts[0]['text'])
        self.assertIn('merged', posts[0]['text'].lower())

    def test_merged_with_shipped_url_carries_link(self):
        cap = _cap(shipped_pr_url=PR)
        posts = self._plan([cap], {})
        self.assertEqual(posts[0]['phase'], 'merged')
        self.assertIn(PR, posts[0]['text'])

    def test_closed_failed_line(self):
        cap = _cap(
            spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                     'outcome': 'closed'},
            failure_signaled={'reason': 'closed without merge'})
        posts = self._plan([cap], {})
        self.assertEqual(posts[0]['phase'], 'closed_failed')
        self.assertEqual(
            posts[0]['text'],
            'The linked PR was closed without merging — needs your eyes.')

    def test_two_cards_two_posts(self):
        c1 = _cap('cap-a', spawned={'kind': 'delegate', 'task_id': 'delegate-a'})
        c2 = _cap('cap-b', spawned={'kind': 'delegate', 'task_id': 'delegate-b'})
        m = {'delegate-a': [{'event_type': 'review_request', 'pr_url': None,
                             'ts': '2026-07-21T11:00:00Z',
                             'payload': {'origin_task_id': 'delegate-a'}}]}
        posts = self._plan([c1, c2], m)
        by_id = {p['capture_id']: p['phase'] for p in posts}
        self.assertEqual(by_id['cap-a'], 'in_review')
        self.assertEqual(by_id['cap-b'], 'handed_off')


# ---------- deterministic event_id ----------


class EventIdTest(unittest.TestCase):
    def test_same_id_and_phase_is_stable(self):
        a = h._narration_event_id('cap-1', 'in_review')
        b = h._narration_event_id('cap-1', 'in_review')
        self.assertEqual(a, b)

    def test_different_phase_differs(self):
        self.assertNotEqual(
            h._narration_event_id('cap-1', 'in_review'),
            h._narration_event_id('cap-1', 'merged'))

    def test_different_card_differs(self):
        self.assertNotEqual(
            h._narration_event_id('cap-1', 'in_review'),
            h._narration_event_id('cap-2', 'in_review'))


# ---------- emit row shape ----------


class EmitTest(unittest.TestCase):
    def test_row_shape_is_fyi_team_message(self):
        client = _EmitClient()
        post = {'capture_id': 'cap-1', 'phase': 'in_review',
                'event_id': 'deadbeef', 'text': 'under review'}
        h._emit_narration_post(client, post, NOW)
        self.assertEqual(len(client.upserts), 1)
        row = client.upserts[0]
        self.assertEqual(row['event_id'], 'deadbeef')
        self.assertEqual(row['task_id'], 'cap-1')
        self.assertEqual(row['event_type'], 'card_message')
        self.assertEqual(row['ts'], NOW.isoformat())
        self.assertEqual(row['payload']['direction'], 'team_to_larry')
        self.assertEqual(row['payload']['actor'], 'beacon')
        # FYI: never rings the blocked-on-you doorbell.
        self.assertIs(row['payload']['needs_reply'], False)
        self.assertEqual(client.upsert_kwargs[0].get('on_conflict'), 'event_id')
        self.assertIs(client.upsert_kwargs[0].get('ignore_duplicates'), True)


# ---------- the sweep (idempotency + single-writer) ----------


class SweepTest(unittest.TestCase):
    def setUp(self):
        # Isolate the trail/approval reads so the sweep logic is under test, not
        # Supabase query shapes. resolve_delegation_narrative_phase stays REAL.
        self._patches = [
            mock.patch.object(da, '_open_delegate_approvals',
                              lambda ids, root=None: {}),
            mock.patch.object(da, '_delegate_dispatched_task_ids',
                              lambda ids, root=None: {}),
            mock.patch.object(da, '_fetch_delegation_build_events',
                              lambda client, ids: {
                                  DELEGATE_ID: [_ev('review_request')]}),
            mock.patch.object(da, '_fetch_events_for_task_ids',
                              lambda client, ids: {}),
            mock.patch.object(da, '_agents_root', lambda: Path('/tmp')),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_emits_one_post_then_idempotent(self):
        client = _EmitClient()
        registry = {'captures': [_cap()]}
        n1 = h.author_delegate_thread_narration(
            registry, now=NOW, dry_run=False, supabase_client=client)
        self.assertEqual(n1, 1)
        self.assertEqual(len(client.rows), 1)
        self.assertEqual(client.rows[0]['payload']['direction'], 'team_to_larry')
        # Re-tick: same (id, phase) → same event_id → ignore_duplicates no-ops it.
        n2 = h.author_delegate_thread_narration(
            registry, now=NOW, dry_run=False, supabase_client=client)
        self.assertEqual(n2, 1)  # planned again
        self.assertEqual(len(client.rows), 1)  # but zero NEW rows landed

    def test_sweep_never_mutates_registry(self):
        client = _EmitClient()
        registry = {'captures': [_cap()]}
        snapshot = copy.deepcopy(registry)
        h.author_delegate_thread_narration(
            registry, now=NOW, dry_run=False, supabase_client=client)
        self.assertEqual(registry, snapshot)

    def test_dry_run_emits_nothing(self):
        client = _EmitClient()
        registry = {'captures': [_cap()]}
        n = h.author_delegate_thread_narration(
            registry, now=NOW, dry_run=True, supabase_client=client)
        self.assertEqual(n, 1)  # would-post count
        self.assertEqual(client.upserts, [])  # but nothing emitted

    def test_no_delegated_cards_is_noop(self):
        client = _EmitClient()
        registry = {'captures': [
            {'id': 'c', 'state': 'parked',
             'spawned': {'kind': 'orphan', 'task_id': 't-1'}}]}
        n = h.author_delegate_thread_narration(
            registry, now=NOW, dry_run=False, supabase_client=client)
        self.assertEqual(n, 0)
        self.assertEqual(client.upserts, [])


if __name__ == '__main__':
    unittest.main()
