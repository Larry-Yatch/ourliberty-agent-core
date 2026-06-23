#!/usr/bin/env python3
"""Tests for the deterministic review marker reminder (2026-06-23).

A phase=review Mirror dispatch must end its turn with EXACTLY ONE canonical
verdict marker (REVIEW_PASS / REVIEW_REVISION / REVIEW_ESCALATE /
REVIEW_EMERGENCY_HALT), but the review prompt is authored upstream and a model
that narrates its verdict in prose ("Verdict: PASS") with no marker block
produces the recurring outbox-notifier `MalformedMirrorMarker: phase=review
requires ONE canonical verdict marker ... none found`. The chain self-heals via
a marker-error retry, but each miss burns a ~$1/~7min round.
`agent_runner.review_marker_reminder_args` appends an authoritative,
last-in-context marker-discipline reminder to the worker's system prompt on
every Mirror review dispatch — the symmetric analogue of the Forge preflight
reminder — so the requirement no longer depends on the task author remembering
it. Because the review-request envelope carries phase='review' and the
marker-error retry envelope preserves phase, the reminder fires on both the
first review attempt and every marker-error retry.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_review_marker_reminder
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


class BuildReviewMarkerReminderSystemPromptTest(unittest.TestCase):
    """Direct coverage of the reminder text builder."""

    def test_carries_marker_and_phase_review(self):
        text = ar.build_review_marker_reminder_system_prompt()
        self.assertIn(ar.REVIEW_MARKER_REMINDER_MARKER, text)
        self.assertIn('phase=review', text)

    def test_names_all_four_marker_types(self):
        text = ar.build_review_marker_reminder_system_prompt()
        self.assertIn('=== REVIEW_PASS ===', text)
        self.assertIn('=== REVIEW_REVISION ===', text)
        self.assertIn('=== REVIEW_ESCALATE ===', text)
        self.assertIn('=== REVIEW_EMERGENCY_HALT ===', text)

    def test_points_at_marker_py_mirror_render(self):
        text = ar.build_review_marker_reminder_system_prompt()
        self.assertIn('marker.py render mirror', text)

    def test_warns_prose_ending_dead_letters(self):
        # The exact failure mode being neutralized: a turn that ends on prose.
        text = ar.build_review_marker_reminder_system_prompt()
        self.assertIn('dead-letter', text.lower())

    def test_deterministic(self):
        self.assertEqual(
            ar.build_review_marker_reminder_system_prompt(),
            ar.build_review_marker_reminder_system_prompt(),
        )


class ReviewMarkerReminderArgsTest(unittest.TestCase):
    """The CLI-arg wrapper consumed by run_claude's spawn path."""

    def test_appends_for_mirror_review(self):
        args = ar.review_marker_reminder_args('review', 'mirror')
        self.assertEqual(args[0], '--append-system-prompt')
        self.assertEqual(len(args), 2)
        self.assertEqual(
            args[1], ar.build_review_marker_reminder_system_prompt()
        )

    def test_normalizes_case_and_whitespace(self):
        # The dispatch fields may arrive with stray case/whitespace; the gate
        # must still fire for a Mirror review.
        self.assertEqual(
            ar.review_marker_reminder_args('  Review  ', '  MIRROR  '),
            ar.review_marker_reminder_args('review', 'mirror'),
        )
        self.assertEqual(len(ar.review_marker_reminder_args('REVIEW', 'Mirror')), 2)

    def test_empty_for_non_review_phases(self):
        # The reminder is review-only; build/preflight/revision must NOT carry it.
        self.assertEqual(ar.review_marker_reminder_args('build', 'mirror'), [])
        self.assertEqual(ar.review_marker_reminder_args('preflight', 'mirror'), [])
        self.assertEqual(ar.review_marker_reminder_args('revision', 'mirror'), [])

    def test_empty_when_phase_missing(self):
        self.assertEqual(ar.review_marker_reminder_args(None, 'mirror'), [])
        self.assertEqual(ar.review_marker_reminder_args('', 'mirror'), [])

    def test_empty_for_non_mirror_agent(self):
        # Other agents have their own marker grammars; this reminder is Mirror's.
        self.assertEqual(ar.review_marker_reminder_args('review', 'forge'), [])
        self.assertEqual(ar.review_marker_reminder_args('review', 'beacon'), [])
        self.assertEqual(ar.review_marker_reminder_args('review', None), [])


if __name__ == '__main__':
    unittest.main()
