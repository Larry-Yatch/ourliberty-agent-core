#!/usr/bin/env python3
"""Tests for scripts/task_resolution.py — the shared out-of-band resolution
check used by healers before re-dispatching / recovering a stuck task.

All optional deps (supabase, gh) are mocked; no network, no real gh.

Run:
    python3 -m unittest scripts.tests.test_task_resolution
"""
from __future__ import annotations

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
        self.assertEqual(
            tr.pr_matches_task(
                {'headRefName': 'forge/other', 'title': 'fix Abc-123 bug'},
                'abc-123'),
            'title')

    def test_no_match(self):
        self.assertIsNone(
            tr.pr_matches_task({'headRefName': 'forge/zzz', 'title': 'no'},
                               'abc-123'))


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
