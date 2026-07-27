#!/usr/bin/env python3
"""Tests for the transient-GitHub-5xx retry on the merge-state recheck
(notifier-gh-502-transient-retry-001).

A transient GitHub server error (HTTP 502/503/504) surfaces as a non-zero `gh`
exit whose stderr carries the 5xx code. It is NOT the rate-limit signature, so
the backoff gate never arms on it — and before this change the `gh pr view`
recheck in `_gh_pr_state` got no retry at all, losing the merged/failed
disambiguation on a momentary blip. These tests cover the spec's success
criteria for the narrow retry that closes that gap:

  (1) a transient 502 then a clean success returns the real state (no WARN);
  (2) a transient 5xx that persists across all 3 attempts preserves today's
      behavior exactly — one WARN + return None;
  (3) a non-transient non-zero exit (404 / auth) does NOT retry;
  (4) the rate-limit signature path is unchanged — still arms the backoff;
  (5) sleeps go through the injectable seam; no real time passes;
  (6) the transient-server-error detector matches 502/503/504 + the CLI's
      "We had issues producing the response" wording, case-insensitively.

All gh interaction is faked (monkeypatched subprocess + injected sleep/clock);
no real GitHub API is touched and no real time passes. stdlib unittest only —
pytest is not installed in the droplet environment.
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


def _fake_proc(returncode: int, stdout: str = '', stderr: str = ''):
    return types.SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr,
    )


_REPO = 'Larry-Yatch/ourliberty-agent-core'

# Transient GitHub server-error stderr wordings (per spec).
_HTTP_502 = 'HTTP 502: Bad Gateway (https://api.github.com/graphql)'
_HTTP_503 = 'HTTP 503: Service Unavailable'
_HTTP_504 = 'HTTP 504: Gateway Timeout'
_GRAPHQL_ISSUES = 'GraphQL: We had issues producing the response'

# Rate-limit + non-transient wordings for the negative paths.
_RATE_LIMIT = (
    'HTTP 403: API rate limit already exceeded for user ID 12345.'
)
_HTTP_404 = 'HTTP 404: Not Found'
_HTTP_401 = 'HTTP 401: Bad credentials'


class TransientServerErrorDetectorTest(unittest.TestCase):
    """(6) `_gh_is_transient_server_error` signature coverage."""

    def test_detects_502_503_504(self):
        for stderr in (_HTTP_502, _HTTP_503, _HTTP_504):
            self.assertTrue(on._gh_is_transient_server_error(1, stderr), stderr)

    def test_detects_graphql_issues_wording(self):
        self.assertTrue(on._gh_is_transient_server_error(1, _GRAPHQL_ISSUES))

    def test_is_case_insensitive(self):
        self.assertTrue(
            on._gh_is_transient_server_error(1, 'http 502: bad gateway'),
        )
        self.assertTrue(
            on._gh_is_transient_server_error(
                1, 'WE HAD ISSUES PRODUCING THE RESPONSE',
            ),
        )

    def test_ignores_zero_exit(self):
        # A 2xx result is never transient-error even if stderr mentions 502.
        self.assertFalse(on._gh_is_transient_server_error(0, _HTTP_502))

    def test_ignores_non_transient_stderr(self):
        self.assertFalse(on._gh_is_transient_server_error(1, _HTTP_404))
        self.assertFalse(on._gh_is_transient_server_error(1, _HTTP_401))
        self.assertFalse(on._gh_is_transient_server_error(1, _RATE_LIMIT))
        # 500/501 are outside the enumerated transient set.
        self.assertFalse(
            on._gh_is_transient_server_error(1, 'HTTP 500: Internal'),
        )
        self.assertFalse(on._gh_is_transient_server_error(1, ''))
        self.assertFalse(on._gh_is_transient_server_error(1, None))


class GhPrStateTransientRetryTest(unittest.TestCase):
    def setUp(self):
        # Injected sleep seam — no real time passes; records requested delays.
        self.sleeps: list[float] = []
        self._saved_sleep = on._gh_transient_retry_sleep
        on._gh_transient_retry_sleep = self.sleeps.append
        # Clean backoff state so an armed window from another test can't skip.
        self._saved_clock = on._gh_backoff_clock
        self._saved_jitter = on._gh_backoff_jitter
        on._gh_backoff_clock = lambda: 1000.0
        on._gh_backoff_jitter = lambda lo, hi: 0.0
        on._gh_backoff_until = 0.0
        on._gh_consecutive_rate_limit = 0
        on._gh_backoff_skip_logged = False

    def tearDown(self):
        on._gh_transient_retry_sleep = self._saved_sleep
        on._gh_backoff_clock = self._saved_clock
        on._gh_backoff_jitter = self._saved_jitter
        on._gh_backoff_until = 0.0
        on._gh_consecutive_rate_limit = 0
        on._gh_backoff_skip_logged = False

    # ── (1) transient then success ──────────────────────────────────────────

    def test_transient_then_success_returns_state_no_warn(self):
        procs = [
            _fake_proc(returncode=1, stderr=_HTTP_502),
            _fake_proc(returncode=0, stdout='{"state": "MERGED"}'),
        ]
        with mock.patch.object(on.subprocess, 'run', side_effect=procs) as run, \
                mock.patch.object(on, 'log') as log:
            result = on._gh_pr_state(_REPO, 5)
        self.assertEqual(result, 'MERGED')
        self.assertEqual(run.call_count, 2)
        # One retry delay: 5s before attempt #2, via the injected seam.
        self.assertEqual(self.sleeps, [5.0])
        # No WARN on the success path.
        warns = [c for c in log.call_args_list if 'WARN' in c.args]
        self.assertEqual(warns, [])
        # A clean success clears any backoff (routes through _gh_note_result).
        self.assertFalse(on._gh_backoff_active())

    # ── (2) transient persists across all attempts ──────────────────────────

    def test_transient_exhausted_warns_once_returns_none(self):
        procs = [
            _fake_proc(returncode=1, stderr=_HTTP_502),
            _fake_proc(returncode=1, stderr=_HTTP_503),
            _fake_proc(returncode=1, stderr=_HTTP_504),
        ]
        with mock.patch.object(on.subprocess, 'run', side_effect=procs) as run, \
                mock.patch.object(on, 'log') as log:
            result = on._gh_pr_state(_REPO, 5)
        self.assertIsNone(result)
        # 3 attempts total (1 initial + 2 retries).
        self.assertEqual(run.call_count, 3)
        # Two fixed delays: 5s then 15s.
        self.assertEqual(self.sleeps, [5.0, 15.0])
        # Exactly one WARN after retries are exhausted.
        warns = [c for c in log.call_args_list if 'WARN' in c.args]
        self.assertEqual(len(warns), 1)
        self.assertIn('merge-state recheck', warns[0].args[0])
        # A transient 5xx never arms the rate-limit backoff window.
        self.assertFalse(on._gh_backoff_active())

    # ── (3) non-transient error does not retry ──────────────────────────────

    def test_non_transient_404_does_not_retry(self):
        proc = _fake_proc(returncode=1, stderr=_HTTP_404)
        with mock.patch.object(on.subprocess, 'run', return_value=proc) as run, \
                mock.patch.object(on, 'log') as log:
            result = on._gh_pr_state(_REPO, 5)
        self.assertIsNone(result)
        self.assertEqual(run.call_count, 1)   # no retry
        self.assertEqual(self.sleeps, [])     # no sleep
        warns = [c for c in log.call_args_list if 'WARN' in c.args]
        self.assertEqual(len(warns), 1)

    def test_non_transient_auth_error_does_not_retry(self):
        proc = _fake_proc(returncode=1, stderr=_HTTP_401)
        with mock.patch.object(on.subprocess, 'run', return_value=proc) as run:
            result = on._gh_pr_state(_REPO, 5)
        self.assertIsNone(result)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.sleeps, [])

    # ── (4) rate-limit signature path unchanged ─────────────────────────────

    def test_rate_limit_still_arms_and_does_not_retry(self):
        proc = _fake_proc(returncode=1, stderr=_RATE_LIMIT)
        with mock.patch.object(on.subprocess, 'run', return_value=proc) as run:
            result = on._gh_pr_state(_REPO, 5)
        self.assertIsNone(result)
        # Rate-limit is not a transient-server-error → no retry.
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.sleeps, [])
        # But it still arms the backoff window (criterion 4 unchanged).
        self.assertTrue(on._gh_backoff_active())
        self.assertEqual(on._gh_consecutive_rate_limit, 1)

    def test_open_backoff_window_short_circuits_without_shelling_out(self):
        # A pre-armed window still skips the recheck entirely at entry.
        on._gh_backoff_until = on._gh_backoff_clock() + 60.0
        with mock.patch.object(on.subprocess, 'run') as run:
            result = on._gh_pr_state(_REPO, 5)
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertEqual(self.sleeps, [])

    # ── extra: transient-then-non-transient stops retrying immediately ──────

    def test_transient_then_non_transient_stops_and_warns_once(self):
        procs = [
            _fake_proc(returncode=1, stderr=_HTTP_502),
            _fake_proc(returncode=1, stderr=_HTTP_404),  # terminal on retry
        ]
        with mock.patch.object(on.subprocess, 'run', side_effect=procs) as run, \
                mock.patch.object(on, 'log') as log:
            result = on._gh_pr_state(_REPO, 5)
        self.assertIsNone(result)
        self.assertEqual(run.call_count, 2)   # retried once, then gave up
        self.assertEqual(self.sleeps, [5.0])
        warns = [c for c in log.call_args_list if 'WARN' in c.args]
        self.assertEqual(len(warns), 1)


if __name__ == '__main__':
    unittest.main()
