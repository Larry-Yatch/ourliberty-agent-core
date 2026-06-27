#!/usr/bin/env python3
"""Tests for the review-timeout -> REVIEW_ESCALATE synthesis (2026-06-26).

When the harness kills a phase=review session at the wall-clock ceiling
(agent_runner.REVIEW_SESSION_CEILING_SECONDS), the outbox carries no canonical
verdict marker. Without recovery that falls into the marker-error net (3 retries
on a session that no longer exists, then dead-letter) or, in the worst observed
case, gets force-merged with NO review (#713, 2026-06-26). `outbox_notifier`
synthesizes a clean REVIEW_ESCALATE from the envelope so the PR routes to Beacon
as inconclusive — the safe, recoverable, auto-routed action.

These tests pin: (1) the synthesis helper, and (2) that `_classify_mirror_marker`
wires it in FRONT of the prose-pass fallback / marker-error raise, while leaving
the non-timeout no-marker path raising as before.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_outbox_notifier_review_timeout_escalate
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import outbox_notifier as on  # noqa: E402


class SynthesizeTimeoutEscalateHelperTest(unittest.TestCase):
    """Direct coverage of _maybe_synthesize_timeout_escalate."""

    def test_returns_escalate_when_timed_out(self):
        data = {
            'timed_out': True,
            'timeout_seconds': 2100,
            'task_id': 'pr-ourliberty-agent-core-999',
            'pr_url': 'https://github.com/x/y/pull/999',
        }
        result = on._maybe_synthesize_timeout_escalate(data)
        self.assertIsNotNone(result)
        marker_type, payload = result
        self.assertEqual(marker_type, 'review_escalate')
        self.assertEqual(payload['task_id'], 'pr-ourliberty-agent-core-999')
        self.assertEqual(payload['pr_url'], 'https://github.com/x/y/pull/999')
        # The escalate handler reads severity/confidence/reason with defaults;
        # we supply explicit inconclusive values + a reason that names the cause.
        self.assertEqual(payload['severity'], 'inconclusive')
        self.assertIn('review_session_timeout', payload['reason'])
        self.assertIn('2100s', payload['reason'])

    def test_none_when_not_timed_out(self):
        self.assertIsNone(on._maybe_synthesize_timeout_escalate({}))
        self.assertIsNone(
            on._maybe_synthesize_timeout_escalate({'timed_out': False})
        )

    def test_reason_omits_seconds_when_absent(self):
        data = {'timed_out': True, 'task_id': 't', 'pr_url': 'u'}
        _marker, payload = on._maybe_synthesize_timeout_escalate(data)
        self.assertIn('review_session_timeout', payload['reason'])
        # No "(<n>s)" fragment when timeout_seconds is missing.
        self.assertNotIn('(None', payload['reason'])


class ClassifyMirrorMarkerWiringTest(unittest.TestCase):
    """The classifier must route a timed-out review to escalate, and leave the
    ordinary no-marker path raising a marker-error (unchanged)."""

    def _no_session_recovery(self):
        # A timed-out session has no usable session log; isolate our branch by
        # making the log-scan return nothing.
        return mock.patch.object(
            on, '_recover_marker_text_from_session_log',
            return_value=('', {}),
        )

    def test_timed_out_review_classifies_as_escalate(self):
        data = {
            'phase': 'review',
            'timed_out': True,
            'timeout_seconds': 2100,
            'task_id': 'pr-ourliberty-agent-core-999',
            'pr_url': 'https://github.com/x/y/pull/999',
            'agent': 'mirror',
            'result': 'partial review output, killed mid-step, no verdict marker',
        }
        with self._no_session_recovery():
            decision = on._classify_mirror_marker(data)
        self.assertIsNotNone(decision)
        self.assertEqual(decision['marker_type'], 'review_escalate')
        self.assertIn('review_session_timeout',
                      decision['intent_kwargs']['reason'])

    def test_non_timed_out_no_marker_still_raises(self):
        # Guard: a genuine missing-marker review (not a timeout) must keep its
        # existing marker-error behavior, not be masked as an escalate.
        data = {
            'phase': 'review',
            'task_id': 'pr-ourliberty-agent-core-998',
            'pr_url': 'https://github.com/x/y/pull/998',
            'agent': 'mirror',
            'result': 'a chatty review with no canonical verdict marker at all',
        }
        with self._no_session_recovery():
            with self.assertRaises(on.mrh.MalformedMirrorMarker):
                on._classify_mirror_marker(data)


if __name__ == '__main__':
    unittest.main()
