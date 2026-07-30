#!/usr/bin/env python3
"""Tests for action=merge on /api/larry/action — the Approvals tab's FOURTH
exit ("the work is fine, merge it"), merge-verb-backend-001.

The verb publishes a reviewed-but-stuck PR with one click. Larry's strict,
non-negotiable rule: it is available ONLY on review-passed items and enforced
in TWO places — the `merge_target` stamp condition AND a server-side re-verify
that NEVER trusts the client. It reuses the auto-merger's gated machinery and
NEVER bypasses a safety gate: a held/failed outcome leaves the card actionable.

Run:
    cd /home/larry/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_merge_action
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

os.environ.setdefault('DASHBOARD_API_TOKEN', 'test-token-value')

import dashboard_api as da  # noqa: E402
import outbox_notifier as on  # noqa: E402
import decision_resolve as dr  # noqa: E402
from fastapi import HTTPException  # noqa: E402

PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1058'
REPO_COORDS = 'Larry-Yatch/ourliberty-agent-core'
PR_NUMBER = 1058
TASK_ID = 'pr-ourliberty-agent-core-1058'
HEAD_SHA = 'd4f17fc2a285439efecdb2f41efdd758527cc38c'

FULL_MERGE_TARGET = {
    'task_id': TASK_ID,
    'pr_url': PR_URL,
    'target_repo': 'ourliberty-agent-core',
    'head_sha': HEAD_SHA,
}


# ---------- supabase stub tailored to the merge flow ----------

class _Resp:
    def __init__(self, data: list[Any], count: Optional[int] = None):
        self.data = data
        self.count = count


class _FakeMergeClient:
    """Minimal supabase-py stub covering the merge handler's DB touches:
    the source fetch (select by event_id), the REVIEW_PASS lookup (select on
    event_type='review_pass'), the atomic read_at CAS claim / release (update),
    and the audit upsert."""

    def __init__(self, source_row: dict[str, Any], review_pass: bool = True):
        self.source_row = source_row
        self.review_pass = review_pass
        self.updates: list[dict[str, Any]] = []
        self.upserts: list[Any] = []
        self._reset()

    def _reset(self):
        self._op: Optional[str] = None
        self._filters: dict[str, Any] = {}
        self._is: dict[str, Any] = {}
        self._values: Optional[dict[str, Any]] = None
        self._count: Any = None
        self._rows: Any = None

    def table(self, name: str):
        self._reset()
        return self

    def select(self, cols: str = '*'):
        self._op = 'select'
        return self

    def update(self, values, count=None, **kw):
        self._op = 'update'
        self._values = values
        self._count = count
        return self

    def upsert(self, rows, **kw):
        self._op = 'upsert'
        self._rows = rows
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def is_(self, col, val):
        self._is[col] = val
        return self

    def limit(self, n):
        return self

    def execute(self):
        op, filters, is_, values = (
            self._op, dict(self._filters), dict(self._is), self._values)
        if op == 'select':
            if filters.get('event_type') == 'review_pass':
                return _Resp([{'event_id': 'rp1'}] if self.review_pass else [])
            if filters.get('event_id') == self.source_row.get('event_id'):
                return _Resp([dict(self.source_row)])
            return _Resp([])
        if op == 'update':
            self.updates.append({'values': values, 'is': is_})
            if is_.get('read_at') == 'null':  # the atomic CAS claim
                if self.source_row.get('read_at') is None:
                    self.source_row['read_at'] = values.get('read_at')
                    return _Resp([dict(self.source_row)], count=1)
                return _Resp([], count=0)
            self.source_row['read_at'] = values.get('read_at')  # release
            return _Resp([dict(self.source_row)])
        if op == 'upsert':
            self.upserts.append(self._rows)
            return _Resp([])
        return _Resp([])


def _source(event_type='approval_request', merge_target=FULL_MERGE_TARGET,
            read_at=None, task_id=TASK_ID):
    payload: dict[str, Any] = {'summary': 'held PR', 'prompt': 'held PR'}
    if merge_target is not None:
        payload['merge_target'] = merge_target
    return {
        'event_id': 'ev-merge-1',
        'event_type': event_type,
        'task_id': task_id,
        'read_at': read_at,
        'payload': payload,
    }


# ---------- pure-helper tests: the server-side re-verify ----------

class VerifyMergeReleaseTest(unittest.TestCase):
    def _verify(self, source, review_pass=True, state='OPEN'):
        client = _FakeMergeClient(dict(source), review_pass=review_pass)
        with mock.patch.object(on, '_gh_pr_state', return_value=state):
            return da._verify_merge_release(source, client)

    def test_happy_path_returns_the_resolved_coordinate(self):
        coord = self._verify(_source())
        self.assertEqual(coord['pr_url'], PR_URL)
        self.assertEqual(coord['repo_coords'], REPO_COORDS)
        self.assertEqual(coord['pr_number'], PR_NUMBER)
        self.assertEqual(coord['task_id'], TASK_ID)

    def test_wrong_event_type_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(event_type='larry_alert'))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_escalation_event_type_is_allowed(self):
        coord = self._verify(_source(event_type='escalation'))
        self.assertEqual(coord['pr_number'], PR_NUMBER)

    def test_missing_merge_target_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(merge_target=None))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_incomplete_merge_target_is_400(self):
        # No pr_url on the stamp (task_id falls back to the source row) ⇒ the
        # coordinate is unaddressable ⇒ 400 before any merge.
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(merge_target={'task_id': TASK_ID}))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_no_review_pass_on_record_is_409_and_names_no_merge_fired(self):
        # The load-bearing half of the two-place gate: even with a stamp, a PR
        # with no REVIEW_PASS on record is refused.
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(), review_pass=False)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('no merge fired', ctx.exception.detail)

    def test_already_merged_pr_is_409(self):
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(), state='MERGED')
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('already merged', ctx.exception.detail)

    def test_closed_pr_is_409(self):
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(), state='CLOSED')
        self.assertEqual(ctx.exception.status_code, 409)

    def test_unverifiable_state_is_502_and_never_merges(self):
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(), state=None)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn('card is untouched', ctx.exception.detail)

    def test_malformed_pr_url_is_400(self):
        bad = dict(FULL_MERGE_TARGET, pr_url='https://evil.example/x/pull/1')
        with self.assertRaises(HTTPException) as ctx:
            self._verify(_source(merge_target=bad))
        self.assertEqual(ctx.exception.status_code, 400)


class ReviewPassOnRecordTest(unittest.TestCase):
    def test_true_when_a_review_pass_row_exists(self):
        client = _FakeMergeClient(_source(), review_pass=True)
        self.assertTrue(da._review_pass_on_record(
            client, task_id=TASK_ID, pr_url=PR_URL))

    def test_false_when_absent(self):
        client = _FakeMergeClient(_source(), review_pass=False)
        self.assertFalse(da._review_pass_on_record(
            client, task_id=TASK_ID, pr_url=PR_URL))

    def test_fail_closed_on_query_error(self):
        client = mock.Mock()
        client.table.side_effect = RuntimeError('supabase down')
        self.assertFalse(da._review_pass_on_record(
            client, task_id=TASK_ID, pr_url=PR_URL))


# ---------- end-to-end handler tests ----------

class MergeHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-merge-'))
        (self.tmp / 'inboxes' / 'beacon').mkdir(parents=True, exist_ok=True)

    def _handle(self, source, merge_result, review_pass=True, state='OPEN'):
        client = _FakeMergeClient(source, review_pass=review_pass)
        with mock.patch.object(on, '_gh_pr_state', return_value=state), \
                mock.patch.object(
                    on, '_attempt_auto_merge_with_gates',
                    return_value=merge_result) as merge_fn, \
                mock.patch.object(dr, 'resolve_decision') as resolve_fn:
            result = da._handle_larry_action(
                source_event_id=source['event_id'], action='merge',
                comment=None, actor='larry@sealteamleaders.com',
                agents_root=self.tmp, supabase_client=client,
            )
        return result, client, merge_fn, resolve_fn

    def test_merged_resolves_the_card_and_fans_out_approved(self):
        src = _source()
        result, client, merge_fn, resolve_fn = self._handle(
            src, {'merge_outcome': 'merged', 'merge_reason': 'squashed'})
        self.assertEqual(result['merge_outcome'], 'merged')
        self.assertEqual(result['merge_reason'], 'squashed')
        merge_fn.assert_called_once()
        # Card cleared (claim kept) — the read_at was set and never released.
        self.assertIsNotNone(client.source_row['read_at'])
        # A published merge fans out as an 'approved' resolution.
        resolve_fn.assert_called_once()
        self.assertEqual(resolve_fn.call_args.args[1], 'approved')

    def test_already_merged_is_also_a_success(self):
        result, client, _, resolve_fn = self._handle(
            _source(), {'merge_outcome': 'already_merged', 'merge_reason': 'x'})
        self.assertEqual(result['merge_outcome'], 'already_merged')
        self.assertIsNotNone(client.source_row['read_at'])
        resolve_fn.assert_called_once()

    def test_held_conflict_releases_the_claim_and_does_not_fan_out(self):
        result, client, _, resolve_fn = self._handle(
            _source(),
            {'merge_outcome': 'held_conflict', 'merge_reason': 'blocker #5'})
        self.assertEqual(result['merge_outcome'], 'held_conflict')
        self.assertEqual(result['merge_reason'], 'blocker #5')
        # Card must stay actionable: the claim was released back to NULL.
        self.assertIsNone(client.source_row['read_at'])
        resolve_fn.assert_not_called()

    def test_failed_merge_releases_the_claim(self):
        _, client, _, resolve_fn = self._handle(
            _source(), {'merge_outcome': 'failed', 'merge_reason': 'network'})
        self.assertIsNone(client.source_row['read_at'])
        resolve_fn.assert_not_called()

    def test_refusal_before_claim_leaves_card_untouched(self):
        # No REVIEW_PASS on record → 409 raised inside _verify_merge_release,
        # which runs BEFORE the atomic claim, so read_at is never touched and
        # the merge machinery is never invoked.
        src = _source()
        client = _FakeMergeClient(src, review_pass=False)
        with mock.patch.object(on, '_gh_pr_state', return_value='OPEN'), \
                mock.patch.object(
                    on, '_attempt_auto_merge_with_gates') as merge_fn:
            with self.assertRaises(HTTPException) as ctx:
                da._handle_larry_action(
                    source_event_id=src['event_id'], action='merge',
                    comment=None, actor='larry@sealteamleaders.com',
                    agents_root=self.tmp, supabase_client=client,
                )
        self.assertEqual(ctx.exception.status_code, 409)
        merge_fn.assert_not_called()
        self.assertIsNone(client.source_row['read_at'])

    def test_already_acted_card_409s_before_any_gh_call(self):
        src = _source(read_at='2026-07-30T00:00:00+00:00')
        client = _FakeMergeClient(src)
        with mock.patch.object(on, '_gh_pr_state') as state_fn:
            with self.assertRaises(HTTPException) as ctx:
                da._handle_larry_action(
                    source_event_id=src['event_id'], action='merge',
                    comment=None, actor='larry@sealteamleaders.com',
                    agents_root=self.tmp, supabase_client=client,
                )
        self.assertEqual(ctx.exception.status_code, 409)
        state_fn.assert_not_called()

    def test_merge_writes_no_agent_envelope(self):
        # merge is a direct-execute verb: it must bypass the envelope builder
        # entirely (no inbox file written).
        self._handle(_source(), {'merge_outcome': 'merged', 'merge_reason': ''})
        written = list((self.tmp / 'inboxes' / 'beacon').iterdir())
        self.assertEqual(written, [])


# ---------- wiring guards ----------

class MergeWiringTest(unittest.TestCase):
    def test_merge_is_a_valid_action(self):
        self.assertIn('merge', da.LARRY_ACTION_VALID_ACTIONS)

    def test_merge_is_not_an_alert_rating(self):
        self.assertNotIn('merge', da.ALERT_RATING_ACTIONS)

    def test_merge_event_types_are_approval_request_and_escalation(self):
        self.assertEqual(
            da.MERGE_VALID_EVENT_TYPES,
            frozenset({'approval_request', 'escalation'}))

    def test_success_outcomes_are_merged_and_already_merged_only(self):
        self.assertEqual(
            da._MERGE_SUCCESS_OUTCOMES,
            frozenset({'merged', 'already_merged'}))

    def test_response_model_exposes_merge_fields(self):
        fields = da.LarryActionResponse.model_fields
        self.assertIn('merge_outcome', fields)
        self.assertIn('merge_reason', fields)

    def test_merge_is_not_a_static_member_of_the_fanout_map(self):
        # A published merge fans out 'approved' ONLY on a real merge; a static
        # map entry would fan out unconditionally even on a held/failed result.
        src = Path(da.__file__).read_text()
        self.assertNotIn("'merge': 'approved'", src)


if __name__ == '__main__':
    unittest.main()
