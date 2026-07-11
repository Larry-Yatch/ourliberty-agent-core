#!/usr/bin/env python3
"""Delegate-tracking Slice 2a — carry origin_task_id onto the build's review
chain_events so the delegated card can track building → review.

The propagation chain (dispatch_approved → Forge envelope → Forge outbox via
inbox_watcher._build_outbox allow-list → Mirror review-request via the CARRY
sentinel → Mirror outbox → these emit sites) ends here: the `review_request`
and `review_<verdict>` chain_events carry `origin_task_id` in their payload, so
the dashboard (Slice 2b) can join them to the delegated card by that key. The
`auto_merge`/shipped event is a separate follow-up (it rides the async
auto-merge queue).

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_delegate_origin_carry
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher as iw       # noqa: E402
import outbox_notifier as on     # noqa: E402

ORIGIN = 'delegate-cap-fix-thing-ab12'


def _capture_emit():
    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return True

    return captured, _fake


class ReviewRequestOriginTest(unittest.TestCase):
    def test_origin_rides_the_payload_when_passed(self):
        captured, fake = _capture_emit()
        with mock.patch.object(on.chain_event_emit, 'emit_event', fake):
            on._emit_review_request_chain_event(
                'fix-thing-001', 'https://github.com/o/r/pull/7',
                revision_count=0, replan_count=0, origin_task_id=ORIGIN,
            )
        self.assertEqual(captured['event_type'], 'review_request')
        self.assertEqual(captured['task_id'], 'fix-thing-001')  # marker id
        self.assertEqual(captured['payload'].get('origin_task_id'), ORIGIN)

    def test_no_origin_leaves_payload_clean(self):
        captured, fake = _capture_emit()
        with mock.patch.object(on.chain_event_emit, 'emit_event', fake):
            on._emit_review_request_chain_event(
                'fix-thing-001', 'https://github.com/o/r/pull/7',
                revision_count=0, replan_count=0,
            )
        self.assertNotIn('origin_task_id', captured['payload'])


class MirrorVerdictOriginTest(unittest.TestCase):
    def _emit(self, data):
        captured, fake = _capture_emit()
        with mock.patch.object(on.chain_event_emit, 'emit_event', fake):
            on._emit_mirror_verdict_chain_event(
                data,
                {'intent': 'review-pass', 'marker_type': 'review_pass',
                 'payload': {'pr_url': 'https://github.com/o/r/pull/7'}},
                agent='mirror',
            )
        return captured

    def test_origin_from_mirror_outbox_rides_the_payload(self):
        captured = self._emit(
            {'task_id': 'fix-thing-001', 'origin_task_id': ORIGIN})
        self.assertEqual(captured['payload'].get('verdict'), 'pass')
        self.assertEqual(captured['task_id'], 'fix-thing-001')  # marker id
        self.assertEqual(captured['payload'].get('origin_task_id'), ORIGIN)

    def test_no_origin_on_a_non_delegate_verdict(self):
        captured = self._emit({'task_id': 'fix-thing-001'})
        self.assertNotIn('origin_task_id', captured['payload'])


class BuildOutboxCarryTest(unittest.TestCase):
    """inbox_watcher._build_outbox propagates origin_task_id from the inbound
    task envelope onto the outbox (Forge AND Mirror share this function), so it
    survives to outbox_notifier."""

    def _build(self, task):
        return iw._build_outbox(
            agent='forge', task_id=task['task_id'], task=task,
            task_file=Path('/tmp/x.json'), success=True, output_text='done',
            session_id='s1', meta={},
        )

    def test_origin_propagates_onto_outbox(self):
        outbox = self._build(
            {'task_id': 'fix-thing-001', 'origin_task_id': ORIGIN,
             'target_repo': 'ourliberty-agent-core'})
        self.assertEqual(outbox.get('origin_task_id'), ORIGIN)

    def test_absent_origin_not_added(self):
        outbox = self._build(
            {'task_id': 'plain-001', 'target_repo': 'ourliberty-agent-core'})
        self.assertNotIn('origin_task_id', outbox)


if __name__ == '__main__':
    unittest.main()
