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
        kw.setdefault('dispatched_by_origin', {})
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
        # Needs the LEDGER RECEIPT — #974 gated `handed_off` on the team actually
        # picking the work up, not on Larry's click. This assertion predates that
        # gate and was merged red in #975; the narrator never passed a receipt
        # map through at all, so every live delegation resolved to None and went
        # unnarrated for two weeks.
        posts = self._plan([_cap()], {},
                           dispatched_by_origin={DELEGATE_ID: 'f-tid'})
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['phase'], 'handed_off')

    def test_grace_handed_off_is_withheld_from_the_thread(self):
        # A just-clicked delegation resolves `handed_off` off the GRACE window
        # alone (no receipt). The dashboard card should show that; a POST must
        # not — it claims the team picked up work nobody has touched. It is also
        # unrecoverable: the sweep runs every 10min inside a 15min grace, so the
        # post would be spent before the real pickup, and the deterministic
        # event_id would then block the genuine `handed_off` forever.
        from datetime import datetime, timezone
        fresh = _cap(spawned={
            'kind': 'delegate', 'task_id': DELEGATE_ID,
            'stamped_at': datetime.now(timezone.utc).isoformat()})
        self.assertEqual(self._plan([fresh], {}), [])
        # ...and once the receipt lands, the SAME card posts it for real.
        posts = self._plan([fresh], {},
                           dispatched_by_origin={DELEGATE_ID: 'f-tid'})
        self.assertEqual([p['phase'] for p in posts], ['handed_off'])

    def test_declined_origin_is_not_narrated(self):
        # Larry REJECTED this delegation. It is declined, not neglected — telling
        # him it "may need a nudge" invites him to re-push work he stopped.
        # The card now resolves its own `declined` phase rather than `stalled`
        # (one resolver, one answer for both read paths); the thread stays silent
        # because `_narration_text` has no honest wording for a decision Larry
        # just made, NOT because the resolver hides the phase from it. The BOARD
        # renders that same phase — see test_delegation_trail.
        self.assertEqual(
            self._plan([_cap()], {}, declined_origins={DELEGATE_ID}), [])
        # A DIFFERENT origin being declined must not suppress this card.
        posts = self._plan([_cap()], {}, declined_origins={'delegate-other'})
        self.assertEqual([p['phase'] for p in posts], ['stalled'])

    def test_declined_phase_resolves_but_yields_no_post(self):
        # Pins the split explicitly: the shared resolver reports `declined` for
        # this exact card while the planner emits nothing for it. If a future
        # change makes the resolver return None instead, this fails — that would
        # be the dashboard going blank again, silently.
        import dashboard_api as _dash  # noqa: PLC0415
        self.assertEqual(
            _dash.resolve_delegation_narrative_phase(
                _cap(), {}, declined_origins={DELEGATE_ID},
            )['narrative_phase'],
            'declined')
        self.assertEqual(
            self._plan([_cap()], {}, declined_origins={DELEGATE_ID}), [])

    def test_declined_origin_still_narrates_real_progress(self):
        # Suppression is scoped to the declined phase only. If a declined origin
        # carries real build signal, that is observed truth and must still post.
        posts = self._plan([_cap()], {DELEGATE_ID: [_ev('review_request')]},
                           declined_origins={DELEGATE_ID})
        self.assertEqual([p['phase'] for p in posts], ['in_review'])

    def test_receipt_map_is_required(self):
        # Omitting it must fail at the CALL, not silently post "no sign of
        # progress" for every card — the #975 failure mode, made loud.
        with self.assertRaises(TypeError):
            h.plan_delegate_narrations(
                [_cap()], build_events_by_origin={},
                open_delegate_approvals={}, native_build_events=None)

    def test_stalled_posts_and_never_claims_it_never_started(self):
        # No receipt, past grace, no build signal ⇒ one honest post. The wording
        # is load-bearing: a missing receipt does NOT prove nothing ran (pre-
        # 2026-07-11 delegations predate the stamp), so "no sign of progress" is
        # the ceiling of what we know and "never started" is forbidden.
        posts = self._plan([_cap()], {})
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['phase'], 'stalled')
        self.assertIn('no sign of progress', posts[0]['text'].lower())
        self.assertNotIn('never started', posts[0]['text'].lower())

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
        posts = self._plan([c1, c2], m,
                           dispatched_by_origin={'delegate-b': 'f-tid'})
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


class ApprovalScanTest(unittest.TestCase):
    """The receipt map counts `approved` ONLY, and origins whose NEWEST terminal
    entry is a rejection are reported separately as declined. Before the filter
    existed, a REJECTED approval still handed back a receipt, and the narrator
    announced "Picked this up" for work Larry had turned down (live instance on
    the 2026-07-28 board).

    Two properties this class pins, both found by review of the consuming PR:
      - `expired` is NOT declined — it is an auto-retirement, not a decision.
      - The netting is NEWEST-WINS, not a set difference, so a rejection that
        follows an approval is honoured instead of being cancelled by it."""

    def _store(self, entries, bucket='history'):
        import json
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / 'state').mkdir(parents=True)
        (root / 'state' / 'beacon-pending-approvals.json').write_text(
            json.dumps({'version': 1, 'pending': [], 'history': [],
                        bucket: entries}))
        return root

    def test_approved_is_a_receipt(self):
        root = self._store([{'origin_task_id': 'delegate-a', 'id': 'fresh-1',
                             'status': 'approved'}])
        self.assertEqual(
            da._delegate_dispatched_task_ids(['delegate-a'], root),
            {'delegate-a': 'fresh-1'})
        self.assertEqual(da._delegate_declined_origins(['delegate-a'], root),
                         set())

    def test_rejected_is_not_a_receipt_and_is_declined(self):
        root = self._store([{'origin_task_id': 'delegate-a', 'id': 'fresh-1',
                             'status': 'rejected'}])
        self.assertEqual(
            da._delegate_dispatched_task_ids(['delegate-a'], root), {})
        self.assertEqual(da._delegate_declined_origins(['delegate-a'], root),
                         {'delegate-a'})

    def test_expired_is_not_a_receipt_and_is_NOT_declined(self):
        # `expired` is an AUTO-RETIREMENT, not a decision — its three writers all
        # say so (`outbox_notifier.py:6320` "Larry never acted";
        # `heal_stale_approvals.py:528`; `heal_unregistered_approval.py:1303`).
        # It was originally lumped in with `rejected`, which rendered "You turned
        # this down" over a delegation Larry never answered AND withheld the
        # thread's stalled post for it. Not a receipt (nothing dispatched) and
        # not declined (he decided nothing) — it falls through to the honest
        # `stalled` floor, which is exactly what an unanswered delegation is.
        root = self._store([{'origin_task_id': 'delegate-a', 'id': 'fresh-1',
                             'status': 'expired'}])
        self.assertEqual(
            da._delegate_dispatched_task_ids(['delegate-a'], root), {})
        self.assertEqual(da._delegate_declined_origins(['delegate-a'], root),
                         set())

    def test_newest_terminal_wins_approved_then_rejected_is_declined(self):
        # ORDER, not set membership. The old netting was
        # `{o for o in declined if o not in approved}`, so ANY approval anywhere
        # in history cancelled a rejection — even one that came AFTER it.
        # delegate → approve → build fails → re-delegate → REJECT left the origin
        # non-declined with a stale receipt, so the card read `handed_off` and
        # the narrator posted a permanent "the team picked this up" about work
        # Larry had just turned down.
        root = self._store([
            {'origin_task_id': 'delegate-a', 'id': 'fresh-1',
             'status': 'approved', 'resolved_at': '2026-07-01T00:00:00Z'},
            {'origin_task_id': 'delegate-a', 'id': 'appr-2',
             'status': 'rejected', 'resolved_at': '2026-07-02T00:00:00Z'},
        ])
        approved, declined = da._delegate_approval_scan(['delegate-a'], root)
        self.assertEqual(declined, {'delegate-a'})
        # And the superseded receipt is GONE — the rejection is the origin's
        # current terminal state, so there is no "the team has it" to out-rank.
        # This is what makes the two halves disjoint by construction.
        self.assertEqual(approved, {})

    def test_the_two_halves_are_disjoint_by_construction(self):
        # The property the scan's contract promises, and that
        # `_delegation_trail_field` is allowed to rely on. It was briefly FALSE
        # under a positional netting fix, which is what forced the ledger-bridge
        # `building` override to be gated on `not _declined`.
        root = self._store([
            {'origin_task_id': 'delegate-a', 'id': 'r1', 'status': 'rejected',
             'resolved_at': '2026-07-01T00:00:00Z'},
            {'origin_task_id': 'delegate-a', 'id': 'a1', 'status': 'approved',
             'resolved_at': '2026-07-02T00:00:00Z'},
            {'origin_task_id': 'delegate-b', 'id': 'a2', 'status': 'approved',
             'resolved_at': '2026-07-01T00:00:00Z'},
            {'origin_task_id': 'delegate-b', 'id': 'r2', 'status': 'rejected',
             'resolved_at': '2026-07-02T00:00:00Z'},
        ])
        approved, declined = da._delegate_approval_scan(
            ['delegate-a', 'delegate-b'], root)
        self.assertEqual(approved, {'delegate-a': 'a1'})
        self.assertEqual(declined, {'delegate-b'})
        self.assertFalse(set(approved) & declined)

    def test_recency_is_resolved_at_not_scan_position(self):
        # A terminal entry stranded in the `pending` bucket (a `resolve()` dying
        # between the history append and the pending removal — the case
        # `_open_delegate_approvals` defensively guards) is scanned FIRST and so
        # has the LOWER position. Ordering on position would let the older
        # approval win and silently un-decline the card.
        root = self._store(
            [{'origin_task_id': 'delegate-a', 'id': 'r1', 'status': 'rejected',
              'resolved_at': '2026-07-09T00:00:00Z'}],
            bucket='pending')
        import json
        p = root / 'state' / 'beacon-pending-approvals.json'
        data = json.loads(p.read_text())
        data['history'] = [{'origin_task_id': 'delegate-a', 'id': 'a1',
                            'status': 'approved',
                            'resolved_at': '2026-07-01T00:00:00Z'}]
        p.write_text(json.dumps(data))
        approved, declined = da._delegate_approval_scan(['delegate-a'], root)
        self.assertEqual(declined, {'delegate-a'})
        self.assertEqual(approved, {})

    def test_approval_with_an_unusable_id_still_supersedes_a_rejection(self):
        # The entry is an approval whether or not its id is usable, so it must
        # still win the recency comparison; only the RECEIPT needs a real id.
        root = self._store([
            {'origin_task_id': 'delegate-a', 'id': 'appr-1',
             'status': 'rejected'},
            {'origin_task_id': 'delegate-a', 'id': '', 'status': 'approved'},
        ])
        approved, declined = da._delegate_approval_scan(['delegate-a'], root)
        self.assertEqual(declined, set())
        self.assertEqual(approved, {})

    def test_pending_is_not_a_receipt(self):
        # Still awaiting Larry — nothing has been dispatched under it.
        root = self._store([{'origin_task_id': 'delegate-a', 'id': 'fresh-1',
                             'status': 'pending'}], bucket='pending')
        self.assertEqual(
            da._delegate_dispatched_task_ids(['delegate-a'], root), {})
        self.assertEqual(da._delegate_declined_origins(['delegate-a'], root),
                         set())

    def test_rejected_then_re_delegated_and_approved_is_not_declined(self):
        # A card turned down once and later re-delegated + approved is LIVE work.
        # Declined requires the absence of ANY approved entry.
        root = self._store([
            {'origin_task_id': 'delegate-a', 'id': 'old', 'status': 'rejected'},
            {'origin_task_id': 'delegate-a', 'id': 'new', 'status': 'approved'},
        ])
        self.assertEqual(
            da._delegate_dispatched_task_ids(['delegate-a'], root),
            {'delegate-a': 'new'})
        self.assertEqual(da._delegate_declined_origins(['delegate-a'], root),
                         set())

    def test_newest_approval_wins(self):
        # A card delegated, built, then re-delegated has TWO approved entries.
        # Taking the FIRST would bridge to the PREVIOUS build, whose completed
        # session_start then reads as `building` for work that has not started.
        root = self._store([
            {'origin_task_id': 'delegate-a', 'id': 'run-1', 'status': 'approved'},
            {'origin_task_id': 'delegate-a', 'id': 'run-2', 'status': 'approved'},
        ])
        self.assertEqual(
            da._delegate_dispatched_task_ids(['delegate-a'], root),
            {'delegate-a': 'run-2'})

    def test_scan_returns_disjoint_halves_in_one_read(self):
        # Both halves from ONE pass, declined already net of approved — callers
        # needing both must not read the store twice (an approval resolving
        # between two reads yields no receipt AND no declined marking, which
        # posts a permanent "no sign of progress" about just-approved work).
        root = self._store([
            {'origin_task_id': 'delegate-a', 'id': 'a1', 'status': 'approved'},
            {'origin_task_id': 'delegate-b', 'id': 'r1', 'status': 'rejected'},
            {'origin_task_id': 'delegate-c', 'id': 'c1', 'status': 'rejected'},
            {'origin_task_id': 'delegate-c', 'id': 'c2', 'status': 'approved'},
        ])
        approved, declined = da._delegate_approval_scan(
            ['delegate-a', 'delegate-b', 'delegate-c'], root)
        self.assertEqual(approved, {'delegate-a': 'a1', 'delegate-c': 'c2'})
        self.assertEqual(declined, {'delegate-b'})
        self.assertFalse(set(approved) & declined)

    def test_missing_store_is_fail_safe(self):
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        self.assertEqual(
            da._delegate_dispatched_task_ids(['delegate-a'], root), {})
        self.assertEqual(da._delegate_declined_origins(['delegate-a'], root),
                         set())


class SweepTest(unittest.TestCase):
    def setUp(self):
        # Isolate the trail/approval reads so the sweep logic is under test, not
        # Supabase query shapes. resolve_delegation_narrative_phase stays REAL.
        self._patches = [
            mock.patch.object(da, '_open_delegate_approvals',
                              lambda ids, root=None: {}),
            mock.patch.object(da, '_delegate_dispatched_task_ids',
                              lambda ids, root=None: {DELEGATE_ID: 'f-tid'}),
            mock.patch.object(da, '_delegate_declined_origins',
                              lambda ids, root=None: set()),
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
