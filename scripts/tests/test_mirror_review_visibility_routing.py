"""Tests for mirror-review-visibility step 2 — Contracts B+C+D.

Covers:
  - for_larry_escalations.py: durable, self-clearing for-Larry record store
    (upsert / clear / idempotency on PR+head SHA) and forward-compat with
    system_state_log.load_for_larry_escalations (the Waiting-on-You reader).
  - outbox_notifier._classify_no_session_review: bucket choice on wire signals
    (marker_type + session/ledger state), never finding prose.
  - outbox_notifier._route_no_session_review: one artifact per escalation —
    self-healing → silent; action-needed → durable record (no standalone
    larry_alert), retracts when the trigger clears; decision-needed → binary
    approval_request. Re-reviewed-still-self-healing emits nothing new.

stdlib unittest only — pytest is NOT installed in the droplet test env.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import beacon_approval_handler as approval  # noqa: E402
import for_larry_escalations as fle  # noqa: E402
import outbox_notifier as on  # noqa: E402


# ----------------------------- the record store -----------------------------

class ForLarryEscalationsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._feed = Path(self._tmp.name) / 'for-larry-escalations.json'
        self._orig_env = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(self._feed)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._orig_env is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_FEED_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = self._orig_env

    def test_upsert_writes_open_record(self):
        row = fle.upsert(
            'mirror-review:t1', headline='Needs you', context='go unstick',
            pr_url='https://gh/o/r/pull/9', head_sha='abc', dedup_identity='pr9@abc',
        )
        self.assertIsNotNone(row)
        got = fle.get('mirror-review:t1')
        self.assertEqual(got['for_larry'], True)
        self.assertEqual(got['resolved'], False)
        self.assertEqual(got['pr_url'], 'https://gh/o/r/pull/9')
        self.assertEqual(got['dedup_identity'], 'pr9@abc')
        self.assertEqual([r['id'] for r in fle.list_open()], ['mirror-review:t1'])

    def test_upsert_idempotent_same_dedup_identity(self):
        fle.upsert('mirror-review:t1', headline='a', context='b',
                   dedup_identity='pr9@abc')
        second = fle.upsert('mirror-review:t1', headline='a2', context='b2',
                            dedup_identity='pr9@abc')
        self.assertIsNone(second)  # same PR+head SHA → no new artifact
        # The original headline is preserved (no churn on re-review).
        self.assertEqual(fle.get('mirror-review:t1')['headline'], 'a')

    def test_upsert_refreshes_on_new_dedup_identity(self):
        fle.upsert('mirror-review:t1', headline='a', context='b',
                   dedup_identity='pr9@abc')
        refreshed = fle.upsert('mirror-review:t1', headline='a2', context='b2',
                               dedup_identity='pr9@def')  # fresh push
        self.assertIsNotNone(refreshed)
        self.assertEqual(fle.get('mirror-review:t1')['headline'], 'a2')
        self.assertEqual(len(fle.list_open()), 1)  # still one row, in place

    def test_clear_retracts_record(self):
        fle.upsert('mirror-review:t1', headline='a', context='b',
                   dedup_identity='pr9@abc')
        self.assertTrue(fle.clear('mirror-review:t1'))
        self.assertEqual(fle.list_open(), [])
        self.assertEqual(fle.get('mirror-review:t1')['resolved'], True)
        # Idempotent: clearing an already-resolved row reports no change.
        self.assertFalse(fle.clear('mirror-review:t1'))

    def test_reopen_after_clear(self):
        fle.upsert('mirror-review:t1', headline='a', context='b',
                   dedup_identity='pr9@abc')
        fle.clear('mirror-review:t1')
        reopened = fle.upsert('mirror-review:t1', headline='a2', context='b2',
                              dedup_identity='pr9@def')
        self.assertIsNotNone(reopened)
        self.assertEqual(fle.get('mirror-review:t1')['resolved'], False)

    def test_corrupt_file_degrades_to_empty(self):
        self._feed.parent.mkdir(parents=True, exist_ok=True)
        self._feed.write_text('{ not json')
        self.assertEqual(fle.list_open(), [])
        # ...and a write still succeeds (overwrites the corruption).
        self.assertIsNotNone(
            fle.upsert('mirror-review:t1', headline='a', context='b',
                       dedup_identity='x')
        )

    def test_forward_compat_with_waiting_on_you_reader(self):
        # The record schema is exactly what system_state_log's Waiting-on-You
        # reader consumes. Since Phase 2 Change C, the reader's Source 1 reads
        # this feed natively (for_larry_escalations.list_open, honoring
        # OURLIBERTY_FOR_LARRY_FEED_FILE — set to self._feed in setUp), so the
        # record surfaces with no schema change (operator-needs-you-feed §5.1).
        # The legacy Source 2 (OURLIBERTY_ESCALATIONS_FILE) is isolated to a
        # nonexistent path so it contributes none and the single seeded record
        # yields exactly one item.
        import system_state_log as ssl
        fle.upsert('mirror-review:t1', headline='Session-less PR needs you',
                   context='go unstick', severity='warning',
                   pr_url='https://gh/o/r/pull/9', dedup_identity='pr9@abc')
        orig = os.environ.get('OURLIBERTY_ESCALATIONS_FILE')
        os.environ['OURLIBERTY_ESCALATIONS_FILE'] = '/no/such/legacy-esc.json'
        try:
            items = ssl.load_for_larry_escalations()
        finally:
            if orig is None:
                os.environ.pop('OURLIBERTY_ESCALATIONS_FILE', None)
            else:
                os.environ['OURLIBERTY_ESCALATIONS_FILE'] = orig
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'Session-less PR needs you')
        self.assertEqual(items[0]['source'], 'escalation')
        # A resolved record drops out of the feed.
        fle.clear('mirror-review:t1')
        os.environ['OURLIBERTY_ESCALATIONS_FILE'] = '/no/such/legacy-esc.json'
        try:
            self.assertEqual(ssl.load_for_larry_escalations(), [])
        finally:
            if orig is None:
                os.environ.pop('OURLIBERTY_ESCALATIONS_FILE', None)
            else:
                os.environ['OURLIBERTY_ESCALATIONS_FILE'] = orig


# ------------------------------- the classifier -------------------------------

class ClassifyNoSessionReviewTest(unittest.TestCase):
    """`_classify_no_session_review` buckets on wire signals only."""

    def setUp(self):
        self._orig_backfill = on.backfill_target_repo
        self.addCleanup(setattr, on, 'backfill_target_repo', self._orig_backfill)

    def _data(self, **kw):
        base = {'task_id': 't1', 'pr_url': 'https://gh/o/r/pull/9',
                'head_sha': 'abc'}
        base.update(kw)
        return base

    def _decision(self, marker_type='review_revision', **kw):
        d = {'marker_type': marker_type, 'payload': {}, 'intent_kwargs': {}}
        d.update(kw)
        return d

    def test_session_present_out_of_scope(self):
        data = self._data(forge_build_session_id='sess', target_repo='r')
        self.assertIsNone(
            on._classify_no_session_review(data, self._decision())
        )

    def test_escalate_is_decision(self):
        data = self._data()
        self.assertEqual(
            on._classify_no_session_review(data, self._decision('review_escalate')),
            on.NO_SESSION_DECISION_NEEDED,
        )

    def test_downgraded_revision_is_decision(self):
        data = self._data()
        self.assertEqual(
            on._classify_no_session_review(
                data, self._decision(auto_promoted=True)),
            on.NO_SESSION_DECISION_NEEDED,
        )
        self.assertEqual(
            on._classify_no_session_review(
                data, self._decision(budget_exhausted=True)),
            on.NO_SESSION_DECISION_NEEDED,
        )

    def test_revision_with_target_repo_is_self_healing(self):
        data = self._data(target_repo='ourliberty-agent-core')
        self.assertEqual(
            on._classify_no_session_review(data, self._decision()),
            on.NO_SESSION_SELF_HEALING,
        )

    def test_offchain_revision_no_target_repo_is_action(self):
        on.backfill_target_repo = lambda task_id: ''  # nothing derivable
        data = self._data()  # no target_repo on the envelope
        self.assertEqual(
            on._classify_no_session_review(data, self._decision()),
            on.NO_SESSION_ACTION_NEEDED,
        )

    def test_larry_interactive_revision_out_of_scope(self):
        data = self._data(source='larry', reply_chat_id=123)
        self.assertIsNone(
            on._classify_no_session_review(data, self._decision())
        )

    def test_pass_marker_out_of_scope(self):
        data = self._data(target_repo='r')
        self.assertIsNone(
            on._classify_no_session_review(data, self._decision('review_pass'))
        )


# --------------------------------- the router ---------------------------------

class RouteNoSessionReviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        # Sandbox the for-Larry feed.
        self._feed = root / 'for-larry-escalations.json'
        self._orig_feed_env = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(self._feed)
        self.addCleanup(self._restore_feed_env)
        # Sandbox the pending-approvals store.
        self._orig_pending = approval.PENDING_APPROVALS_PATH
        approval.PENDING_APPROVALS_PATH = root / 'beacon-pending-approvals.json'
        self.addCleanup(
            setattr, approval, 'PENDING_APPROVALS_PATH', self._orig_pending)
        # No Supabase in tests: keep the tab-feed emit a deterministic no-op.
        self._orig_emit = on.chain_event_emit.emit_event
        on.chain_event_emit.emit_event = lambda **kw: True
        self.addCleanup(
            setattr, on.chain_event_emit, 'emit_event', self._orig_emit)
        self._orig_backfill = on.backfill_target_repo
        self.addCleanup(setattr, on, 'backfill_target_repo', self._orig_backfill)

    def _restore_feed_env(self):
        if self._orig_feed_env is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_FEED_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = self._orig_feed_env

    def _data(self, **kw):
        base = {'task_id': 't1', 'pr_url': 'https://gh/o/r/pull/9',
                'head_sha': 'abc'}
        base.update(kw)
        return base

    def _decision(self, marker_type='review_revision', **kw):
        d = {'marker_type': marker_type, 'payload': {}, 'intent_kwargs': {}}
        d.update(kw)
        return d

    def _pending(self):
        return approval.load_state().get('pending', [])

    def test_self_healing_emits_nothing(self):
        data = self._data(target_repo='ourliberty-agent-core')
        bucket = on._route_no_session_review(data, self._decision())
        self.assertEqual(bucket, on.NO_SESSION_SELF_HEALING)
        self.assertEqual(fle.list_open(), [])
        self.assertEqual(self._pending(), [])

    def test_action_needed_writes_durable_record_no_larry_alert(self):
        on.backfill_target_repo = lambda task_id: ''
        data = self._data()  # off-chain, no target_repo
        bucket = on._route_no_session_review(data, self._decision())
        self.assertEqual(bucket, on.NO_SESSION_ACTION_NEEDED)
        rows = fle.list_open()
        self.assertEqual(len(rows), 1)
        self.assertIn('t1', rows[0]['headline'])
        self.assertEqual(rows[0]['dedup_identity'], 'https://gh/o/r/pull/9@abc')
        # No standalone approval artifact for the action bucket.
        self.assertEqual(self._pending(), [])

    def test_action_needed_idempotent_same_head_sha(self):
        on.backfill_target_repo = lambda task_id: ''
        data = self._data()
        on._route_no_session_review(data, self._decision())
        on._route_no_session_review(data, self._decision())  # re-review, same head
        self.assertEqual(len(fle.list_open()), 1)

    def test_action_record_clears_on_self_heal(self):
        on.backfill_target_repo = lambda task_id: ''
        on._route_no_session_review(self._data(), self._decision())
        self.assertEqual(len(fle.list_open()), 1)
        # A later round acquires a chain envelope → self-healing → retract.
        healed = self._data(target_repo='ourliberty-agent-core')
        on._route_no_session_review(healed, self._decision())
        self.assertEqual(fle.list_open(), [])

    def test_decision_needed_emits_binary_approval(self):
        data = self._data()
        bucket = on._route_no_session_review(
            data, self._decision('review_escalate',
                                 intent_kwargs={'reason': 'Scope call.'}))
        self.assertEqual(bucket, on.NO_SESSION_DECISION_NEEDED)
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        entry = pending[0]
        self.assertEqual(entry['id'], 'mirror-review-t1-abc')
        self.assertIn('Approve', entry['dispatch_payload']['summary'])
        self.assertIn('Reject', entry['dispatch_payload']['summary'])
        # Decision bucket writes no for-Larry action record.
        self.assertEqual(fle.list_open(), [])

    def test_decision_idempotent_same_head_sha(self):
        data = self._data()
        on._route_no_session_review(data, self._decision('review_escalate'))
        on._route_no_session_review(data, self._decision('review_escalate'))
        self.assertEqual(len(self._pending()), 1)

    def test_decision_new_head_sha_new_approval(self):
        on._route_no_session_review(
            self._data(head_sha='abc'), self._decision('review_escalate'))
        on._route_no_session_review(
            self._data(head_sha='def'), self._decision('review_escalate'))
        ids = sorted(e['id'] for e in self._pending())
        self.assertEqual(ids, ['mirror-review-t1-abc', 'mirror-review-t1-def'])


if __name__ == '__main__':
    unittest.main()
