#!/usr/bin/env python3
"""Tests for the GitHub API rate-limit backoff gate (outbox-notifier-gh-
ratelimit-backoff-001).

Covers the spec's success criteria:
  (1) a rate-limit result short-circuits the next gh call (no shell-out) until
      the window expires;
  (2) the window grows exponentially from a 60s floor to a ~300s ceiling with
      jitter across consecutive rate-limit hits;
  (3) a single successful gh call resets the backoff to zero;
  (4) non-rate-limit failures (timeout / 404 / auth) do NOT arm the backoff;
  (5) log volume during a window is bounded (one skip line per window);
  (6) signature detection covers both REST and GraphQL wording.

All gh interaction is faked (monkeypatched subprocess + fake monotonic clock);
no real GitHub API is touched. stdlib unittest only — pytest is not installed
in the droplet environment.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import outbox_notifier as on  # noqa: E402


class _FakeClock:
    """Deterministic monotonic clock — no real time passes in these tests."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _fake_proc(returncode: int, stdout: str = '', stderr: str = ''):
    return types.SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr,
    )


# GitHub rate-limit stderr wordings seen in the wild (per spec).
_REST_STDERR = (
    'HTTP 403: API rate limit already exceeded for user ID 12345. '
    '(https://docs.github.com/rest/overview/resources-in-the-rest-api)'
)
_GRAPHQL_STDERR = (
    'GraphQL: API rate limit already exceeded for user ID 12345 (type: RATE_LIMITED)'
)


class GhRateLimitBackoffTest(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self._saved_clock = on._gh_backoff_clock
        self._saved_jitter = on._gh_backoff_jitter
        on._gh_backoff_clock = self.clock.now
        on._gh_backoff_jitter = lambda lo, hi: 0.0  # no jitter by default
        # Reset module state to a clean window.
        on._gh_backoff_until = 0.0
        on._gh_consecutive_rate_limit = 0
        on._gh_backoff_skip_logged = False

    def tearDown(self):
        on._gh_backoff_clock = self._saved_clock
        on._gh_backoff_jitter = self._saved_jitter
        on._gh_backoff_until = 0.0
        on._gh_consecutive_rate_limit = 0
        on._gh_backoff_skip_logged = False

    def _window(self) -> float:
        """Seconds of backoff currently armed relative to the fake clock."""
        return on._gh_backoff_until - self.clock.now()

    # ── (6) signature detection — REST + GraphQL wording ────────────────────

    def test_signature_detects_rest_wording(self):
        self.assertTrue(on._gh_is_rate_limit_error(1, _REST_STDERR))

    def test_signature_detects_graphql_wording(self):
        self.assertTrue(on._gh_is_rate_limit_error(1, _GRAPHQL_STDERR))

    def test_signature_is_case_insensitive(self):
        self.assertTrue(
            on._gh_is_rate_limit_error(1, 'RATE LIMIT ALREADY EXCEEDED'),
        )

    def test_signature_ignores_zero_exit(self):
        # A 2xx result is never a rate-limit error even if stderr mentions it.
        self.assertFalse(on._gh_is_rate_limit_error(0, _REST_STDERR))

    def test_signature_ignores_non_rate_limit_stderr(self):
        self.assertFalse(
            on._gh_is_rate_limit_error(1, 'HTTP 404: Not Found'),
        )
        self.assertFalse(
            on._gh_is_rate_limit_error(1, 'HTTP 401: Bad credentials'),
        )
        self.assertFalse(on._gh_is_rate_limit_error(1, ''))
        self.assertFalse(on._gh_is_rate_limit_error(1, None))

    # ── (2) exponential growth + ceiling clamp ──────────────────────────────

    def test_exponential_growth_and_ceiling_clamp(self):
        expected = [60.0, 120.0, 240.0, 300.0, 300.0]
        for want in expected:
            # Re-anchor: clear the elapsed portion so each _window() reads the
            # freshly-armed window, not the remainder of the previous one.
            on._gh_note_rate_limit(_REST_STDERR)
            self.assertAlmostEqual(self._window(), want, places=6)
        # Consecutive count tracked one-per-hit.
        self.assertEqual(on._gh_consecutive_rate_limit, len(expected))

    # ── (2) jitter presence ─────────────────────────────────────────────────

    def test_jitter_is_applied(self):
        on._gh_backoff_jitter = lambda lo, hi: 10.0
        on._gh_note_rate_limit(_REST_STDERR)
        self.assertAlmostEqual(self._window(), 70.0, places=6)  # 60 + 10

    def test_negative_jitter_never_goes_below_zero(self):
        on._gh_backoff_jitter = lambda lo, hi: -1000.0
        on._gh_note_rate_limit(_REST_STDERR)
        self.assertGreaterEqual(self._window(), 0.0)

    def test_positive_jitter_never_exceeds_ceiling(self):
        # Drive the count high so the base is already at the ceiling, then add
        # positive jitter — the result must still clamp to the ceiling.
        on._gh_consecutive_rate_limit = 10
        on._gh_backoff_jitter = lambda lo, hi: 1000.0
        on._gh_note_rate_limit(_REST_STDERR)
        self.assertLessEqual(self._window(), on._GH_RATE_LIMIT_CEILING_S)

    def test_jitter_is_requested_within_configured_bounds(self):
        captured = {}

        def _spy(lo, hi):
            captured['lo'], captured['hi'] = lo, hi
            return 0.0

        on._gh_backoff_jitter = _spy
        on._gh_note_rate_limit(_REST_STDERR)
        self.assertEqual(captured['lo'], -on._GH_RATE_LIMIT_JITTER_S)
        self.assertEqual(captured['hi'], on._GH_RATE_LIMIT_JITTER_S)

    # ── (1) active window + short-circuit skip ──────────────────────────────

    def test_backoff_active_within_window_then_expires(self):
        on._gh_note_rate_limit(_REST_STDERR)  # arms ~60s
        self.assertTrue(on._gh_backoff_active())
        self.clock.advance(59.0)
        self.assertTrue(on._gh_backoff_active())
        self.clock.advance(2.0)  # past the 60s window
        self.assertFalse(on._gh_backoff_active())

    def test_wrapper_short_circuits_without_shelling_out(self):
        on._gh_note_rate_limit(_REST_STDERR)  # window open
        with mock.patch.object(on.subprocess, 'run') as run:
            result = on._gh_pr_state('Larry-Yatch/repo', 5)
        self.assertIsNone(result)
        run.assert_not_called()

    def test_poll_mergeable_does_not_burn_budget_during_backoff(self):
        on._gh_note_rate_limit(_REST_STDERR)  # window open
        sleep = mock.Mock()
        with mock.patch.object(on, '_gh_pr_mergeable_status') as status:
            out = on._poll_pr_mergeable('Larry-Yatch/repo', 5, sleep=sleep)
        self.assertEqual(out, 'unknown')
        status.assert_not_called()
        sleep.assert_not_called()

    # ── (5) bounded logging — one skip line per window ──────────────────────

    def test_skip_log_is_throttled_to_once_per_window(self):
        on._gh_note_rate_limit(_REST_STDERR)  # window open, skip flag reset
        with mock.patch.object(on, 'log') as log:
            for _ in range(5):
                self.assertTrue(on._gh_backoff_skip('ctx'))
            skip_lines = [
                c for c in log.call_args_list
                if 'skipping gh call' in c.args[0]
            ]
        self.assertEqual(len(skip_lines), 1)

    def test_skip_returns_false_when_no_window(self):
        self.assertFalse(on._gh_backoff_skip('ctx'))

    # ── (3) success reset ───────────────────────────────────────────────────

    def test_success_resets_backoff(self):
        on._gh_note_rate_limit(_REST_STDERR)
        on._gh_note_rate_limit(_REST_STDERR)
        self.assertTrue(on._gh_backoff_active())
        on._gh_note_success()
        self.assertFalse(on._gh_backoff_active())
        self.assertEqual(on._gh_consecutive_rate_limit, 0)
        self.assertEqual(on._gh_backoff_until, 0.0)

    def test_note_result_success_clears_window(self):
        on._gh_note_rate_limit(_REST_STDERR)
        on._gh_note_result(0, '')
        self.assertFalse(on._gh_backoff_active())
        self.assertEqual(on._gh_consecutive_rate_limit, 0)

    # ── (4) non-rate-limit errors do NOT arm ────────────────────────────────

    def test_note_result_404_does_not_arm(self):
        on._gh_note_result(1, 'HTTP 404: Not Found')
        self.assertFalse(on._gh_backoff_active())
        self.assertEqual(on._gh_consecutive_rate_limit, 0)

    def test_note_result_auth_error_does_not_arm(self):
        on._gh_note_result(1, 'HTTP 401: Bad credentials')
        self.assertFalse(on._gh_backoff_active())

    def test_note_result_preserves_open_window_on_unrelated_error(self):
        # A rate-limit hit arms the window; a subsequent plain-404 result must
        # neither clear nor extend it — the window persists untouched.
        on._gh_note_rate_limit(_REST_STDERR)
        armed_until = on._gh_backoff_until
        on._gh_note_result(1, 'HTTP 404: Not Found')
        self.assertEqual(on._gh_backoff_until, armed_until)
        self.assertEqual(on._gh_consecutive_rate_limit, 1)

    # ── wrapper end-to-end routing (rate-limit result arms the window) ───────

    def test_rate_limit_result_through_wrapper_arms_window(self):
        proc = _fake_proc(returncode=1, stdout='', stderr=_GRAPHQL_STDERR)
        with mock.patch.object(on.subprocess, 'run', return_value=proc):
            result = on._gh_pr_state('Larry-Yatch/repo', 5)
        self.assertIsNone(result)
        self.assertTrue(on._gh_backoff_active())
        self.assertEqual(on._gh_consecutive_rate_limit, 1)

    def test_successful_wrapper_call_clears_window(self):
        on._gh_note_rate_limit(_REST_STDERR)  # pre-arm
        proc = _fake_proc(returncode=0, stdout='{"state": "OPEN"}', stderr='')
        # Clock past the window so backoff is inactive and the call proceeds.
        self.clock.advance(1000.0)
        with mock.patch.object(on.subprocess, 'run', return_value=proc):
            result = on._gh_pr_state('Larry-Yatch/repo', 5)
        self.assertEqual(result, 'OPEN')
        self.assertFalse(on._gh_backoff_active())
        self.assertEqual(on._gh_consecutive_rate_limit, 0)

    def test_auto_merge_defers_during_backoff_without_shelling_out(self):
        on._gh_note_rate_limit(_REST_STDERR)  # window open
        pr_url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/9'
        with mock.patch.object(on, 'gh_write') as gh_write:
            result = on._auto_merge_pr(pr_url, task_id='t-1')
        gh_write.assert_not_called()
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertIn('backoff', result['merge_reason'])
        self.assertEqual(result['pr_number'], 9)


if __name__ == '__main__':
    unittest.main()
