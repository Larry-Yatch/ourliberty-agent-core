#!/usr/bin/env python3
"""Tests for the deterministic review bounded-step reminder (2026-06-26).

A phase=review Mirror dispatch must run long steps (the test regression check,
a subagent task, any slow command) in the FOREGROUND under a wall-clock ceiling
and ESCALATE on timeout — never backgrounded-and-polled. The unbounded poll has
wedged the WHOLE Mirror review queue for 71-102 min three times (PR #101, #334,
#717/#720), each holding the `inbox:mirror` lease until a human killed it.
`agent_runner.review_bounded_step_reminder_args` appends an authoritative,
last-in-context reminder to the worker's system prompt on every Mirror review
dispatch — the symmetric analogue of the review marker reminder — so the
requirement no longer depends on the task author or the model recalling the
manual. The gate matches the marker reminder (phase='review' + Mirror), so it
fires on the first review attempt and every retry.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_review_bounded_step_reminder
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import agent_runner as ar  # noqa: E402


class BuildReviewBoundedStepSystemPromptTest(unittest.TestCase):
    """Direct coverage of the reminder text builder."""

    def test_carries_marker(self):
        text = ar.build_review_bounded_step_system_prompt()
        self.assertIn(ar.REVIEW_BOUNDED_STEP_REMINDER_MARKER, text)

    def test_points_at_the_bounded_runner(self):
        text = ar.build_review_bounded_step_system_prompt()
        self.assertIn('run_review_step.sh', text)
        self.assertIn('--timeout', text)

    def test_forbids_the_background_poll_shapes(self):
        # The exact wedge signatures this reminder neutralizes.
        text = ar.build_review_bounded_step_system_prompt()
        self.assertIn('NEVER background', text)
        self.assertIn('grep -qE', text)          # the #717/#720 content-sentinel poll
        self.assertIn('pgrep', text)             # the #101/#334 liveness re-derivation
        self.assertIn('inbox:mirror', text)      # names the starvation it prevents

    def test_maps_timeout_to_escalate(self):
        text = ar.build_review_bounded_step_system_prompt()
        self.assertIn('REVIEW_STEP_TIMED_OUT', text)
        self.assertIn('REVIEW_ESCALATE', text)
        # ... and explicitly forbids a PASS on an incomplete step.
        self.assertIn('REVIEW_PASS', text)

    def test_deterministic(self):
        self.assertEqual(
            ar.build_review_bounded_step_system_prompt(),
            ar.build_review_bounded_step_system_prompt(),
        )


class ReviewBoundedStepReminderArgsTest(unittest.TestCase):
    """The CLI-arg wrapper consumed by run_claude's spawn path."""

    def test_appends_for_mirror_review(self):
        args = ar.review_bounded_step_reminder_args('review', 'mirror')
        self.assertEqual(args[0], '--append-system-prompt')
        self.assertEqual(len(args), 2)
        self.assertEqual(args[1], ar.build_review_bounded_step_system_prompt())

    def test_normalizes_case_and_whitespace(self):
        self.assertEqual(
            ar.review_bounded_step_reminder_args('  Review  ', '  MIRROR  '),
            ar.review_bounded_step_reminder_args('review', 'mirror'),
        )
        self.assertEqual(
            len(ar.review_bounded_step_reminder_args('REVIEW', 'Mirror')), 2
        )

    def test_empty_for_non_review_phases(self):
        self.assertEqual(ar.review_bounded_step_reminder_args('build', 'mirror'), [])
        self.assertEqual(ar.review_bounded_step_reminder_args('preflight', 'mirror'), [])
        self.assertEqual(ar.review_bounded_step_reminder_args('revision', 'mirror'), [])

    def test_empty_when_phase_missing(self):
        self.assertEqual(ar.review_bounded_step_reminder_args(None, 'mirror'), [])
        self.assertEqual(ar.review_bounded_step_reminder_args('', 'mirror'), [])

    def test_empty_for_non_mirror_agent(self):
        self.assertEqual(ar.review_bounded_step_reminder_args('review', 'forge'), [])
        self.assertEqual(ar.review_bounded_step_reminder_args('review', 'beacon'), [])
        self.assertEqual(ar.review_bounded_step_reminder_args('review', None), [])


if __name__ == '__main__':
    unittest.main()
