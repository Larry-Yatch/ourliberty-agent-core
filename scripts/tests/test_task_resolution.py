#!/usr/bin/env python3
"""Tests for scripts/task_resolution.py — the shared out-of-band resolution
check used by healers before re-dispatching / recovering a stuck task.

All optional deps (supabase, gh) are mocked; no network, no real gh.

Run:
    python3 -m unittest scripts.tests.test_task_resolution
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import task_resolution as tr  # noqa: E402

NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(hours=2)


class PrMatchesTaskTest(unittest.TestCase):
    def test_exact_branch(self):
        self.assertEqual(
            tr.pr_matches_task({'headRefName': 'forge/abc-123'}, 'abc-123'),
            'branch')

    def test_larry_branch(self):
        self.assertEqual(
            tr.pr_matches_task({'headRefName': 'larry/abc-123'}, 'abc-123'),
            'branch')

    def test_truncated_branch_above_floor(self):
        task = 'chain-discipline-gap-beacon-plan-synthesis-stale-state'
        branch = 'forge/chain-discipline-gap-beacon-plan-synthesis-stale-s'
        self.assertEqual(
            tr.pr_matches_task({'headRefName': branch}, task), 'branch_truncated')

    def test_short_shared_prefix_below_floor_no_match(self):
        self.assertIsNone(
            tr.pr_matches_task({'headRefName': 'forge/build'}, 'build-x-001'))

    def test_title_substring(self):
        # A realistic-length task_id carried verbatim in a human PR title still
        # matches via the title fallback (case-insensitively).
        self.assertEqual(
            tr.pr_matches_task(
                {'headRefName': 'forge/other',
                 'title': 'fix Chain-Discipline-Gap-Synthesis-001 bug'},
                'chain-discipline-gap-synthesis-001'),
            'title')

    def test_title_short_id_below_floor_no_match(self):
        # audit #19: a short/common task_id must NOT match by title substring,
        # which previously suppressed legitimate recovery. Below id_match's
        # length floor the title path returns None so recovery proceeds.
        self.assertIsNone(
            tr.pr_matches_task(
                {'headRefName': 'forge/other', 'title': 'fix Abc-123 bug'},
                'abc-123'))

    def test_title_match_requires_token_boundary(self):
        # a long id that is only an infix of a larger title token must not match
        self.assertIsNone(
            tr.pr_matches_task(
                {'headRefName': 'forge/other',
                 'title': 'chain-discipline-gap-synthesis-001x follow-up'},
                'chain-discipline-gap-synthesis-001'))

    def test_no_match(self):
        self.assertIsNone(
            tr.pr_matches_task({'headRefName': 'forge/zzz', 'title': 'no'},
                               'abc-123'))

    def test_forge_iteration_suffix_matches(self):
        # Forge's re-attempt branch `forge/<task_id>-<NN>` carries an extra numeric
        # suffix the bare task_id lacks. It must match the task (the canonical case
        # the lane never drained on): forge/p3-dashboard-proposed-lane-002 -> task
        # p3-dashboard-proposed-lane.
        self.assertEqual(
            tr.pr_matches_task(
                {'headRefName': 'forge/p3-dashboard-proposed-lane-002'},
                'p3-dashboard-proposed-lane'),
            'branch_iter')

    def test_forge_iteration_suffix_wrong_core_no_match(self):
        # The iteration strip must require EXACT core equality — a different core
        # with an iteration suffix is not this task's branch.
        self.assertIsNone(
            tr.pr_matches_task(
                {'headRefName': 'forge/some-other-long-feature-002'},
                'p3-dashboard-proposed-lane'))

    def test_iteration_core_below_floor_no_match(self):
        # A short core (below the length floor) must NOT iter-match, so a tiny task
        # id can't collide with an unrelated `<short>-NN` branch.
        self.assertIsNone(
            tr.pr_matches_task({'headRefName': 'forge/abc-2'}, 'abc'))

    def test_exact_numeric_task_prefers_branch_over_iter(self):
        # When the numeric tail is PART of the task id, the exact-branch rule fires
        # first (not iter) — forge/abc-123 == task abc-123.
        self.assertEqual(
            tr.pr_matches_task({'headRefName': 'forge/abc-123'}, 'abc-123'),
            'branch')


class ResolvedOutOfBandTest(unittest.TestCase):
    def test_larry_action_wins(self):
        with mock.patch.object(tr, 'get_chain_events_for_task',
                               return_value=[{'event_type': 'larry_action'}]):
            self.assertEqual(tr.resolved_out_of_band('t', PAST),
                             (True, 'larry_action'))

    def test_session_start_supersedes(self):
        with mock.patch.object(tr, 'get_chain_events_for_task',
                               return_value=[{'event_type': 'session_start'}]):
            self.assertEqual(tr.resolved_out_of_band('t', PAST),
                             (True, 'superseded_session'))

    def test_no_signal_no_pr(self):
        with mock.patch.object(tr, 'get_chain_events_for_task',
                               return_value=[]), \
             mock.patch.object(tr, 'shipped_pr', return_value=None):
            self.assertEqual(tr.resolved_out_of_band('t', PAST), (False, None))

    def test_chain_events_infra_fail_falls_through_to_pr(self):
        # None == infra error -> still checks PR; a merged PR resolves it.
        with mock.patch.object(tr, 'get_chain_events_for_task',
                               return_value=None), \
             mock.patch.object(tr, 'shipped_pr',
                               return_value={'number': 294}):
            self.assertEqual(tr.resolved_out_of_band('t', PAST),
                             (True, 'pr_merged:#294'))

    def test_check_pr_false_ignores_merged_pr(self):
        with mock.patch.object(tr, 'get_chain_events_for_task',
                               return_value=None), \
             mock.patch.object(tr, 'shipped_pr',
                               return_value={'number': 1}) as pr_m:
            self.assertEqual(
                tr.resolved_out_of_band('t', PAST, check_pr=False), (False, None))
            pr_m.assert_not_called()

    def test_unknown_task_short_circuits(self):
        self.assertEqual(tr.resolved_out_of_band('unknown', PAST), (False, None))
        self.assertEqual(tr.resolved_out_of_band('', PAST), (False, None))


class ShippedPrTest(unittest.TestCase):
    def _gh(self, rc, stdout):
        m = mock.Mock()
        m.returncode = rc
        m.stdout = stdout
        m.stderr = ''
        return m

    def test_returns_matching_merged_pr(self):
        payload = '[{"number":294,"title":"feat: queue api","headRefName":"forge/abc-123","mergedAt":"2026-06-04T11:00:00Z"}]'
        with mock.patch.object(tr.subprocess, 'run',
                               return_value=self._gh(0, payload)):
            pr = tr.shipped_pr('abc-123', repos=('r/one',))
        self.assertIsNotNone(pr)
        self.assertEqual(pr['number'], 294)
        self.assertEqual(pr['_repo'], 'r/one')

    def test_since_ts_excludes_merge_before_pause(self):
        # PR merged 09:00, task paused (since_ts) 10:00 -> stale, no match.
        payload = '[{"number":1,"title":"x","headRefName":"forge/abc-123","mergedAt":"2026-06-04T09:00:00Z"}]'
        since = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
        with mock.patch.object(tr.subprocess, 'run',
                               return_value=self._gh(0, payload)):
            self.assertIsNone(tr.shipped_pr('abc-123', since_ts=since,
                                            repos=('r/one',)))

    def test_since_ts_includes_merge_after_pause(self):
        payload = '[{"number":1,"title":"x","headRefName":"forge/abc-123","mergedAt":"2026-06-04T11:00:00Z"}]'
        since = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
        with mock.patch.object(tr.subprocess, 'run',
                               return_value=self._gh(0, payload)):
            pr = tr.shipped_pr('abc-123', since_ts=since, repos=('r/one',))
        self.assertIsNotNone(pr)

    def test_gh_failure_returns_none(self):
        with mock.patch.object(tr.subprocess, 'run',
                               return_value=self._gh(1, '')):
            self.assertIsNone(tr.shipped_pr('abc-123', repos=('r/one',)))


if __name__ == '__main__':
    unittest.main()
