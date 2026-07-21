#!/usr/bin/env python3
"""Delegate-thread-narrator — the shared narrative-phase resolver.

`dashboard_api.resolve_delegation_narrative_phase` collapses the read-side
`_delegation_trail_field` phase + the GC healer's persisted terminal stamps
(`spawned.outcome`, `shipped_note`/`shipped_pr_url`, `failure_signaled`) +
`has_open_approval` into ONE narrative phase over {handed_off, waiting_approval,
building, in_review, review_passed, merged, closed_failed, None} — the SAME
resolver the sweep and the dashboard both read. These tests pin the
most-terminal-wins precedence and the honest-merge rule.

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_delegation_narrative_phase
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api as da  # noqa: E402

DELEGATE_ID = 'delegate-cap-fix-the-thing-ab12'
PR = 'https://github.com/o/r/pull/7'


def _ev(event_type, *, pr_url=None, ts='2026-07-11T12:00:00Z'):
    return {'event_type': event_type, 'pr_url': pr_url, 'ts': ts,
            'payload': {'origin_task_id': DELEGATE_ID}}


def _cap(**over):
    cap = {'id': 'cap-fix-the-thing-ab12', 'state': 'parked',
           'title': 'Fix the thing',
           'spawned': {'kind': 'delegate', 'task_id': DELEGATE_ID}}
    cap.update(over)
    return cap


def _resolve(cap, m=None, **kw):
    return da.resolve_delegation_narrative_phase(cap, m, **kw)


class PhasePrecedenceTest(unittest.TestCase):
    def test_no_signal_is_none(self):
        # A non-delegate card / nothing observed → nothing to narrate.
        cap = {'id': 'c', 'state': 'parked',
               'spawned': {'kind': 'orphan', 'task_id': 't-1'}}
        got = _resolve(cap, {})
        self.assertIsNone(got['narrative_phase'])
        self.assertIsNone(got['narrative_pr_url'])

    def test_handed_off(self):
        got = _resolve(_cap(), {})
        self.assertEqual(got['narrative_phase'], 'handed_off')
        self.assertIsNone(got['narrative_pr_url'])

    def test_waiting_approval_wins_over_handed_off(self):
        got = _resolve(_cap(), {}, has_open_approval=True)
        self.assertEqual(got['narrative_phase'], 'waiting_approval')
        self.assertIsNone(got['narrative_pr_url'])

    def test_building_from_native_session_start(self):
        native = {DELEGATE_ID: [{'event_type': 'session_start',
                                 'agent': 'forge'}]}
        got = _resolve(_cap(), {}, native_build_events=native)
        self.assertEqual(got['narrative_phase'], 'building')

    def test_in_review(self):
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_request')]})
        self.assertEqual(got['narrative_phase'], 'in_review')

    def test_review_passed_carries_pr_url(self):
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_pass', pr_url=PR)]})
        self.assertEqual(got['narrative_phase'], 'review_passed')
        self.assertEqual(got['narrative_pr_url'], PR)


class MergeStampTest(unittest.TestCase):
    def test_outcome_merged_wins(self):
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'merged'})
        got = _resolve(cap, {DELEGATE_ID: [_ev('review_pass', pr_url=PR)]})
        self.assertEqual(got['narrative_phase'], 'merged')
        # PR url overlays from the trail when no shipped stamp carried one.
        self.assertEqual(got['narrative_pr_url'], PR)

    def test_shipped_note_alone_is_merged(self):
        # reconcile_completed_cards (S3) sets shipped_note but NOT spawned.outcome.
        cap = _cap(shipped_note='shipped in PR #7')
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'merged')

    def test_shipped_pr_url_supplies_merged_url(self):
        cap = _cap(shipped_pr_url=PR)
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'merged')
        self.assertEqual(got['narrative_pr_url'], PR)

    def test_merged_no_pr_url_is_none_url(self):
        # A no-PR delegation that still merged linked work: merged, but no url.
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'merged'})
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'merged')
        self.assertIsNone(got['narrative_pr_url'])

    def test_review_passed_never_inferred_as_merged(self):
        # Honest: a bare review_passed trail with NO merge stamp stays
        # review_passed — never silently promoted to merged.
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_pass', pr_url=PR)]})
        self.assertEqual(got['narrative_phase'], 'review_passed')


class ClosedFailedTest(unittest.TestCase):
    def test_closed_plus_failure_signaled(self):
        cap = _cap(
            spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                     'outcome': 'closed'},
            failure_signaled={'reason': 'closed without merge', 'at': 'x'})
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'closed_failed')

    def test_closed_without_failure_signal_is_not_closed_failed(self):
        # outcome closed but no failure_signaled → falls through (no phantom
        # closed_failed); with no trail it degrades to handed_off.
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'closed'})
        got = _resolve(cap, {})
        self.assertNotEqual(got['narrative_phase'], 'closed_failed')


if __name__ == '__main__':
    unittest.main()
