#!/usr/bin/env python3
"""Tests for the MANDATORY Mirror review wall-clock ceiling (2026-06-26).

`build_review_bounded_step_system_prompt` is advisory — it asks Mirror to wrap
long steps in `run_review_step.sh`, but the model can ignore it, and has, three
times (PR #101 71m, #334 102m, #717/#720 85-100m), each wedging the single-holder
`inbox:mirror` lease until a human killed it. The laptop-PR jam (#713, 8.5h,
force-merged-without-review) was the same class on the session-less review path.

`agent_runner.review_session_effective_timeout` is the HARNESS-enforced backstop:
the dispatcher caps every Mirror review session at REVIEW_SESSION_CEILING_SECONDS
regardless of model compliance, on EVERY path into review (Forge-dispatched and
the session-less human/laptop-PR review both spawn with phase='review'). This
test pins that gate so a future refactor can't silently widen it back to the 4h
session default.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_review_session_ceiling
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

import agent_runner as ar  # noqa: E402


class ReviewSessionEffectiveTimeoutTest(unittest.TestCase):
    """Direct coverage of the pure ceiling helper."""

    def test_mirror_review_capped_at_ceiling(self):
        # The 4h session default must be clamped to the ceiling for a review.
        self.assertEqual(
            ar.review_session_effective_timeout(14400, 'review', 'mirror'),
            ar.REVIEW_SESSION_CEILING_SECONDS,
        )

    def test_default_timeout_zero_also_capped_for_review(self):
        # timeout<=0 resolves to the 14400 default, then clamps for a review.
        self.assertEqual(
            ar.review_session_effective_timeout(0, 'review', 'mirror'),
            ar.REVIEW_SESSION_CEILING_SECONDS,
        )

    def test_caller_timeout_below_ceiling_is_kept(self):
        # The ceiling is a max, never a floor: a smaller explicit timeout wins.
        self.assertEqual(
            ar.review_session_effective_timeout(600, 'review', 'mirror'),
            600,
        )

    def test_non_review_phase_unaffected(self):
        # A Forge build keeps the full session budget.
        self.assertEqual(
            ar.review_session_effective_timeout(14400, 'build', 'forge'),
            14400,
        )

    def test_review_phase_non_mirror_agent_unaffected(self):
        # Gate requires BOTH phase=review AND agent=mirror.
        self.assertEqual(
            ar.review_session_effective_timeout(14400, 'review', 'forge'),
            14400,
        )

    def test_mirror_non_review_phase_unaffected(self):
        self.assertEqual(
            ar.review_session_effective_timeout(14400, 'preflight', 'mirror'),
            14400,
        )

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(
            ar.review_session_effective_timeout(14400, ' Review ', ' MIRROR '),
            ar.REVIEW_SESSION_CEILING_SECONDS,
        )

    def test_none_phase_or_agent_unaffected(self):
        self.assertEqual(
            ar.review_session_effective_timeout(14400, None, None),
            14400,
        )

    def test_ceiling_disabled_disables_cap(self):
        # <=0 ceiling (env opt-out for incident response) means no clamp.
        with mock.patch.object(ar, 'REVIEW_SESSION_CEILING_SECONDS', 0):
            self.assertEqual(
                ar.review_session_effective_timeout(14400, 'review', 'mirror'),
                14400,
            )

    def test_env_override_respected(self):
        with mock.patch.object(ar, 'REVIEW_SESSION_CEILING_SECONDS', 1200):
            self.assertEqual(
                ar.review_session_effective_timeout(14400, 'review', 'mirror'),
                1200,
            )

    def test_default_ceiling_is_sane(self):
        # 35 min: generous over a healthy review, far under the 4h backstop and
        # the old 60-min reaper grace it now fires ahead of.
        self.assertEqual(ar.REVIEW_SESSION_CEILING_SECONDS, 2100)


if __name__ == '__main__':
    unittest.main()
