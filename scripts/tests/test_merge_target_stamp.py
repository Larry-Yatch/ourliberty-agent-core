#!/usr/bin/env python3
"""Tests for the `merge_target` coordinate stamp (merge-verb-backend-001).

`merge_target` is the dashboard's capability gate for the FOURTH operator verb
("merge it"). It is stamped ONLY on a review-passed, open, held PR's approval
card — `outbox_notifier._surface_deep_review_hold_approval`, the card that
exists precisely because a PR PASSED Mirror but is held for a human deep review.
The builder fails closed (no coordinate ⇒ no stamp ⇒ no button), and the
chain-event forwarder (`beacon_approval_handler.build_approval_request_chain_event`)
carries the stamp onto the Approvals-tab feed the way it already does for
`recheck_target`.

NOTE: stdlib unittest only — pytest is NOT installed in the droplet
test/regression environment.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import outbox_notifier as notifier  # noqa: E402
import beacon_approval_handler as bah  # noqa: E402

PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1058'
TASK_ID = 'pr-ourliberty-agent-core-1058'
HEAD_SHA = 'd4f17fc2a285439efecdb2f41efdd758527cc38c'


class BuildMergeTargetTest(unittest.TestCase):
    def test_full_coordinate_returns_the_four_fields(self):
        mt = notifier._build_merge_target(
            task_id=TASK_ID, pr_url=PR_URL,
            target_repo='ourliberty-agent-core', head_sha=HEAD_SHA)
        self.assertEqual(mt, {
            'task_id': TASK_ID,
            'pr_url': PR_URL,
            'target_repo': 'ourliberty-agent-core',
            'head_sha': HEAD_SHA,
        })

    def test_fails_closed_on_any_missing_field(self):
        for missing in ('pr_url', 'target_repo', 'head_sha'):
            kwargs = dict(
                task_id=TASK_ID, pr_url=PR_URL,
                target_repo='ourliberty-agent-core', head_sha=HEAD_SHA)
            kwargs[missing] = None
            with self.subTest(missing=missing):
                self.assertIsNone(notifier._build_merge_target(**kwargs))


class ChainEventForwardingTest(unittest.TestCase):
    """The stamp must reach the Approvals-tab chain event, mirroring how
    `recheck_target` is forwarded."""

    MERGE_TARGET = {
        'task_id': TASK_ID, 'pr_url': PR_URL,
        'target_repo': 'ourliberty-agent-core', 'head_sha': HEAD_SHA,
    }

    def test_merge_target_is_forwarded_to_the_payload(self):
        kwargs = bah.build_approval_request_chain_event({
            'task_id': 'mirror-review-' + TASK_ID,
            'prompt': 'held PR',
            'merge_target': self.MERGE_TARGET,
        })
        self.assertEqual(kwargs['payload']['merge_target'], self.MERGE_TARGET)

    def test_absent_merge_target_leaves_payload_without_the_key(self):
        kwargs = bah.build_approval_request_chain_event({
            'task_id': 'mirror-review-' + TASK_ID, 'prompt': 'p',
        })
        self.assertNotIn('merge_target', kwargs['payload'])

    def test_non_dict_merge_target_is_ignored(self):
        for bogus in ('x', [], 0, True):
            with self.subTest(bogus=bogus):
                kwargs = bah.build_approval_request_chain_event({
                    'task_id': 'mirror-review-' + TASK_ID,
                    'prompt': 'p', 'merge_target': bogus,
                })
                self.assertNotIn('merge_target', kwargs['payload'])


class DeepReviewHoldStampTest(unittest.TestCase):
    """The deep-review-hold card (a review-passed, open, held PR) is where the
    merge verb lives — it must carry a `merge_target` on both the durable store
    payload and the tab-feed chain event."""

    def _surface(self, head_sha=HEAD_SHA):
        captured = {}

        def _fake_add_pending(payload, chat_id=None):
            captured['pending'] = payload

        def _fake_emit(**kwargs):
            captured['emit'] = kwargs

        with mock.patch.object(notifier, '_primary_chat_id', return_value=42), \
                mock.patch.object(
                    notifier, '_deep_review_hold_trigger_files',
                    return_value=['scripts/x.py']), \
                mock.patch.object(
                    notifier.approval, 'add_pending',
                    side_effect=_fake_add_pending), \
                mock.patch.object(
                    notifier.chain_event_emit, 'emit_event',
                    side_effect=_fake_emit):
            notifier._surface_deep_review_hold_approval(
                'Larry-Yatch/ourliberty-agent-core', 1058, PR_URL,
                TASK_ID, head_sha, 'mirror-review-' + TASK_ID)
        return captured

    def test_stamp_is_present_and_keyed_on_the_raw_task_id(self):
        captured = self._surface()
        mt = captured['pending']['merge_target']
        # Keyed on the RAW build task_id (so the REVIEW_PASS-on-record re-verify
        # matches the Mirror verdict event), NOT the head-suffixed approval id.
        self.assertEqual(mt['task_id'], TASK_ID)
        self.assertEqual(mt['pr_url'], PR_URL)
        self.assertEqual(mt['head_sha'], HEAD_SHA)
        # ...and it survives onto the tab-feed chain event.
        self.assertEqual(captured['emit']['payload']['merge_target'], mt)

    def test_no_head_sha_means_no_stamp_no_button(self):
        captured = self._surface(head_sha=None)
        self.assertNotIn('merge_target', captured['pending'])
        self.assertNotIn('merge_target', captured['emit']['payload'])


if __name__ == '__main__':
    unittest.main()
