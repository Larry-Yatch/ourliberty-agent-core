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

import json
import sys
import tempfile
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


# ═══════════════════════════════════════════════════════════════════════════
# outbox-notifier-pending-auto-merge-queue-001 — durable retry of auto-merges
# that were skipped because a rate-limit backoff window was open when the
# REVIEW_PASS was processed. Without this, the PR orphans forever (the marker
# doesn't re-arrive, the healer only retries `failed`, the serializer queue is
# never entered). The enqueue is scoped TIGHTLY to the backoff reason: a genuine
# 404 / timeout keeps today's skip-without-enqueue behavior.
# ═══════════════════════════════════════════════════════════════════════════

_AGENT_CORE_PR = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
_AGENT_CORE_REPO = 'Larry-Yatch/ourliberty-agent-core'


class _PendingQueueTestBase(unittest.TestCase):
    """Shared setup: deterministic backoff clock + a tmpdir-rerouted pending
    queue file so nothing touches ~/agents or hits real gh."""

    def setUp(self):
        self.clock = _FakeClock()
        self._saved_clock = on._gh_backoff_clock
        self._saved_jitter = on._gh_backoff_jitter
        on._gh_backoff_clock = self.clock.now
        on._gh_backoff_jitter = lambda lo, hi: 0.0
        on._gh_backoff_until = 0.0
        on._gh_consecutive_rate_limit = 0
        on._gh_backoff_skip_logged = False
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved_queue_file = on.PENDING_AUTO_MERGE_QUEUE_FILE
        on.PENDING_AUTO_MERGE_QUEUE_FILE = (
            Path(self._tmp.name) / 'state' / 'pending-auto-merges.json'
        )
        self._saved_corrupt_dmed = on._PENDING_AUTO_MERGE_CORRUPT_DMED
        on._PENDING_AUTO_MERGE_CORRUPT_DMED = False

    def tearDown(self):
        on._gh_backoff_clock = self._saved_clock
        on._gh_backoff_jitter = self._saved_jitter
        on._gh_backoff_until = 0.0
        on._gh_consecutive_rate_limit = 0
        on._gh_backoff_skip_logged = False
        on.PENDING_AUTO_MERGE_QUEUE_FILE = self._saved_queue_file
        on._PENDING_AUTO_MERGE_CORRUPT_DMED = self._saved_corrupt_dmed


class PendingAutoMergeEnqueueTest(_PendingQueueTestBase):
    """The enqueue predicate at the `pr_state is None` skip branch — driven
    through the real `_run_review_pass_auto_merge` with a mocked existence
    check (so no gh shell-out) and no `_AUTO_MERGE_FN_OVERRIDE` (production
    default), so the existence-check bypass is not taken."""

    def _run_pass(self, exist_return, *, changed_files=('scripts/x.py',),
                  reply_chat_id=555, task_id='orphan-1'):
        data = {
            'task_id': task_id,
            'reply_chat_id': reply_chat_id,
            'changed_files': (
                list(changed_files) if changed_files is not None else None
            ),
            'pr_url': _AGENT_CORE_PR,
        }
        marker_decision = {
            'payload': {'pr_url': _AGENT_CORE_PR, 'summary': 'did the thing'},
            'intent_kwargs': {},
        }
        with mock.patch.object(on, '_pr_url_existence_state',
                               return_value=exist_return), \
             mock.patch.object(on, '_sequence_cancelled', return_value=False):
            return on._run_review_pass_auto_merge(
                data, marker_decision, Path(self._tmp.name) / 'ob.json',
            )

    def test_enqueue_on_ratelimit_skip(self):
        ret = self._run_pass((None, on._GH_BACKOFF_EXIST_REASON))
        self.assertEqual(ret, 'auto-merge-skipped')
        entries = on._load_pending_auto_merge_queue()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e['repo'], _AGENT_CORE_REPO)
        self.assertEqual(e['pr_number'], 42)
        self.assertEqual(e['pr_url'], _AGENT_CORE_PR)
        self.assertEqual(e['task_id'], 'orphan-1')
        self.assertEqual(e['summary'], 'did the thing')
        self.assertEqual(e['reply_chat_id'], 555)
        self.assertEqual(e['changed_files'], ['scripts/x.py'])
        self.assertEqual(e['retry_attempts'], 0)
        self.assertIsNone(e['last_attempt_at'])
        self.assertIn('queued_at', e)

    def test_no_enqueue_on_genuine_404(self):
        ret = self._run_pass(
            (None, 'gh exit=1: HTTP 404: This PR could not be found'),
        )
        self.assertEqual(ret, 'auto-merge-skipped')
        self.assertEqual(on._load_pending_auto_merge_queue(), [])

    def test_enqueue_dedup(self):
        self._run_pass((None, on._GH_BACKOFF_EXIST_REASON))
        self._run_pass((None, on._GH_BACKOFF_EXIST_REASON))
        self.assertEqual(len(on._load_pending_auto_merge_queue()), 1)


class PendingAutoMergeSweepTest(_PendingQueueTestBase):
    """The per-scan retry sweep."""

    def _seed(self, **over):
        entry = {
            'task_id': 'orphan-1',
            'pr_url': _AGENT_CORE_PR,
            'repo': _AGENT_CORE_REPO,
            'pr_number': 42,
            'summary': 'did the thing',
            'reply_chat_id': 555,
            'changed_files': ['scripts/x.py'],
            'queued_at': '2026-07-09T00:00:00+00:00',
            'retry_attempts': 0,
            'last_attempt_at': None,
        }
        entry.update(over)
        on._save_pending_auto_merge_queue([entry])

    def test_sweep_noop_while_backoff_active(self):
        self._seed()
        on._gh_backoff_until = self.clock.now() + 100  # window still open
        with mock.patch.object(on, '_attempt_auto_merge_with_gates') as merge, \
             mock.patch.object(on, '_pr_url_existence_state') as exist:
            on._pending_auto_merge_sweep()
        merge.assert_not_called()
        exist.assert_not_called()
        self.assertEqual(len(on._load_pending_auto_merge_queue()), 1)

    def test_sweep_merges_after_backoff_clears(self):
        self._seed()
        on._gh_backoff_until = 0.0  # window cleared
        merged = {
            'merge_outcome': 'merged', 'merge_reason': 'squash-merged',
            'pr_number': 42, 'repo_coords': _AGENT_CORE_REPO,
        }
        with mock.patch.object(on, '_pr_url_existence_state',
                               return_value=('OPEN', 'ok')), \
             mock.patch.object(on, '_attempt_auto_merge_with_gates',
                               return_value=merged) as merge, \
             mock.patch.object(on, '_fire_review_pass_outcome_dm') as dm:
            on._pending_auto_merge_sweep()
        merge.assert_called_once()
        dm.assert_called_once()
        self.assertEqual(on._load_pending_auto_merge_queue(), [])

    def test_sweep_dequeues_terminal(self):
        self._seed()
        on._gh_backoff_until = 0.0
        with mock.patch.object(on, '_pr_url_existence_state',
                               return_value=('MERGED', 'ok')), \
             mock.patch.object(on, '_attempt_auto_merge_with_gates') as merge, \
             mock.patch.object(on, '_fire_review_pass_outcome_dm') as dm:
            on._pending_auto_merge_sweep()
        merge.assert_not_called()
        dm.assert_not_called()  # no duplicate DM on clean out-of-band merge
        self.assertEqual(on._load_pending_auto_merge_queue(), [])

    def test_sweep_attempt_cap_dms_and_dequeues(self):
        self._seed(retry_attempts=on._PENDING_AUTO_MERGE_MAX_ATTEMPTS + 1)
        on._gh_backoff_until = 0.0
        with mock.patch.object(
                on, '_dm_larry_pending_auto_merge_exhausted') as dm, \
             mock.patch.object(on, '_attempt_auto_merge_with_gates') as merge, \
             mock.patch.object(on, '_pr_url_existence_state') as exist:
            on._pending_auto_merge_sweep()
        dm.assert_called_once()
        merge.assert_not_called()
        exist.assert_not_called()  # cap check short-circuits before ground-truth
        self.assertEqual(on._load_pending_auto_merge_queue(), [])

    def test_sweep_transient_non_backoff_bumps_and_keeps(self):
        self._seed()
        on._gh_backoff_until = 0.0
        with mock.patch.object(on, '_pr_url_existence_state',
                               return_value=(None, 'timeout after 10s')), \
             mock.patch.object(on, '_attempt_auto_merge_with_gates') as merge:
            on._pending_auto_merge_sweep()
        merge.assert_not_called()
        entries = on._load_pending_auto_merge_queue()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['retry_attempts'], 1)
        self.assertIsNotNone(entries[0]['last_attempt_at'])

    def test_sweep_reraised_backoff_leaves_entry(self):
        self._seed()
        on._gh_backoff_until = 0.0  # clears the top-of-sweep guard
        with mock.patch.object(
                on, '_pr_url_existence_state',
                return_value=(None, on._GH_BACKOFF_EXIST_REASON)), \
             mock.patch.object(on, '_attempt_auto_merge_with_gates') as merge:
            on._pending_auto_merge_sweep()
        merge.assert_not_called()
        entries = on._load_pending_auto_merge_queue()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['retry_attempts'], 0)  # untouched

    def test_corrupt_queue_fails_soft(self):
        on.PENDING_AUTO_MERGE_QUEUE_FILE.parent.mkdir(
            parents=True, exist_ok=True,
        )
        on.PENDING_AUTO_MERGE_QUEUE_FILE.write_text(
            '{ this is not valid json', encoding='utf-8',
        )
        on._gh_backoff_until = 0.0
        with mock.patch.object(on, '_attempt_auto_merge_with_gates') as merge, \
             mock.patch.object(on, 'log') as log_fn, \
             mock.patch.object(on.larry_alerts, 'append_alert') as alert:
            on._pending_auto_merge_sweep()  # must not raise
        merge.assert_not_called()
        warned = [
            c for c in log_fn.call_args_list
            if 'PENDING_AUTO_MERGE_QUEUE_CORRUPT' in c.args[0]
        ]
        self.assertTrue(warned)
        alert.assert_called_once()

    def test_empty_queue_cheap_noop(self):
        self.assertFalse(on.PENDING_AUTO_MERGE_QUEUE_FILE.exists())
        on._gh_backoff_until = 0.0
        with mock.patch.object(on, '_pr_url_existence_state') as exist, \
             mock.patch.object(on, '_attempt_auto_merge_with_gates') as merge:
            on._pending_auto_merge_sweep()
        exist.assert_not_called()
        merge.assert_not_called()


class PendingAutoMergeQueueHelpersTest(_PendingQueueTestBase):
    """Direct coverage of the queue file schema + helper round-trips."""

    def test_enqueue_writes_versioned_dict_wrapper(self):
        on._enqueue_pending_auto_merge(
            task_id='t', pr_url=_AGENT_CORE_PR, repo=_AGENT_CORE_REPO,
            pr_number=7, summary='s', reply_chat_id=None, changed_files=None,
        )
        raw = json.loads(
            on.PENDING_AUTO_MERGE_QUEUE_FILE.read_text(encoding='utf-8'),
        )
        self.assertEqual(raw['version'], on.PENDING_AUTO_MERGE_QUEUE_VERSION)
        self.assertEqual(len(raw['entries']), 1)

    def test_dequeue_removes_matching_entry(self):
        on._enqueue_pending_auto_merge(
            task_id='t', pr_url=_AGENT_CORE_PR, repo=_AGENT_CORE_REPO,
            pr_number=7, summary='s', reply_chat_id=None, changed_files=None,
        )
        removed = on._dequeue_pending_auto_merge(_AGENT_CORE_REPO, 7)
        self.assertIsNotNone(removed)
        self.assertEqual(on._load_pending_auto_merge_queue(), [])
        # Second dequeue of the same key is a clean no-op.
        self.assertIsNone(on._dequeue_pending_auto_merge(_AGENT_CORE_REPO, 7))

    def test_absent_file_reads_empty(self):
        self.assertEqual(on._load_pending_auto_merge_queue(), [])


if __name__ == '__main__':
    unittest.main()
