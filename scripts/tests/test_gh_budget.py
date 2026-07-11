#!/usr/bin/env python3
"""Tests for gh_budget (gh-api-burn phase 1, Part B).

Covers:
  - _parse_rate_limit reduces the gh api rate_limit JSON (core -> 'rest').
  - budget_ok: False below threshold, True above, True (fail-open) on gh error.
  - remaining: the ~60s cache serves a second caller without re-spawning gh.
  - should_skip: logs the skip line + returns True when low; False when healthy /
    unknown / on a guard error (fail-open).
  - kernel guard: task_terminal_state returns UNKNOWN without firing its gh query
    when the budget is low, and proceeds (fires gh) when healthy.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_gh_budget
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import gh_budget  # noqa: E402


def _rate_limit_json(graphql_remaining, graphql_limit=5000, reset=1_700_000_000,
                     core_remaining=4000, core_limit=5000):
    return json.dumps({
        'resources': {
            'graphql': {'remaining': graphql_remaining, 'limit': graphql_limit,
                        'reset': reset},
            'core': {'remaining': core_remaining, 'limit': core_limit,
                     'reset': reset},
        },
    })


def _fake_proc(stdout='', returncode=0, stderr=''):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode,
                                 stderr=stderr)


class ParseRateLimitTests(unittest.TestCase):
    def test_maps_graphql_and_core_to_rest(self):
        data = gh_budget._parse_rate_limit(
            json.loads(_rate_limit_json(1234)))
        self.assertEqual(data['graphql']['remaining'], 1234)
        self.assertEqual(data['graphql']['limit'], 5000)
        # core is surfaced under the friendlier 'rest' name.
        self.assertEqual(data['rest']['remaining'], 4000)

    def test_missing_resources_returns_empty(self):
        self.assertEqual(gh_budget._parse_rate_limit({'nope': 1}), {})
        self.assertEqual(gh_budget._parse_rate_limit('not a dict'), {})


class BudgetOkTests(unittest.TestCase):
    def setUp(self):
        gh_budget._reset_cache()
        self.addCleanup(gh_budget._reset_cache)

    def test_false_below_threshold(self):
        with mock.patch('gh_budget.subprocess.run',
                        return_value=_fake_proc(_rate_limit_json(100))):
            self.assertFalse(gh_budget.budget_ok(500))

    def test_true_above_threshold(self):
        with mock.patch('gh_budget.subprocess.run',
                        return_value=_fake_proc(_rate_limit_json(1500))):
            self.assertTrue(gh_budget.budget_ok(500))

    def test_fail_open_when_gh_errors(self):
        with mock.patch('gh_budget.subprocess.run',
                        side_effect=FileNotFoundError('no gh')):
            self.assertTrue(gh_budget.budget_ok(500))

    def test_fail_open_on_nonzero_exit(self):
        with mock.patch('gh_budget.subprocess.run',
                        return_value=_fake_proc('', returncode=1,
                                                stderr='rate limit exceeded')):
            self.assertTrue(gh_budget.budget_ok(500))

    def test_fail_open_on_unparseable_json(self):
        with mock.patch('gh_budget.subprocess.run',
                        return_value=_fake_proc('not json{')):
            self.assertTrue(gh_budget.budget_ok(500))


class CacheTests(unittest.TestCase):
    def setUp(self):
        gh_budget._reset_cache()
        self.addCleanup(gh_budget._reset_cache)

    def test_cache_avoids_duplicate_gh_calls_within_a_tick(self):
        run = mock.Mock(return_value=_fake_proc(_rate_limit_json(2000)))
        with mock.patch('gh_budget.subprocess.run', run):
            first = gh_budget.remaining()
            second = gh_budget.remaining()
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)  # second call served from cache

    def test_failed_read_is_not_cached(self):
        run = mock.Mock(side_effect=FileNotFoundError('no gh'))
        with mock.patch('gh_budget.subprocess.run', run):
            self.assertEqual(gh_budget.remaining(), {})
            self.assertEqual(gh_budget.remaining(), {})
        # An empty/failed read must NOT be cached — both calls re-query.
        self.assertEqual(run.call_count, 2)


class ShouldSkipTests(unittest.TestCase):
    def setUp(self):
        gh_budget._reset_cache()
        self.addCleanup(gh_budget._reset_cache)

    def test_skips_and_logs_when_low(self):
        logs = []
        with mock.patch('gh_budget.remaining',
                        return_value={'graphql': {'remaining': 100,
                                                  'limit': 5000, 'reset': None}}):
            skip = gh_budget.should_skip('heal_x', min_graphql=500,
                                         log=logs.append)
        self.assertTrue(skip)
        self.assertEqual(len(logs), 1)
        self.assertIn('skipping this run', logs[0])
        self.assertIn('budget low', logs[0])

    def test_proceeds_silently_when_healthy(self):
        logs = []
        with mock.patch('gh_budget.remaining',
                        return_value={'graphql': {'remaining': 4000,
                                                  'limit': 5000, 'reset': None}}):
            skip = gh_budget.should_skip('heal_x', min_graphql=500,
                                         log=logs.append)
        self.assertFalse(skip)
        self.assertEqual(logs, [])

    def test_proceeds_when_budget_unknown(self):
        with mock.patch('gh_budget.remaining', return_value={}):
            self.assertFalse(gh_budget.should_skip('heal_x'))

    def test_fail_open_when_guard_raises(self):
        with mock.patch('gh_budget.budget_ok',
                        side_effect=RuntimeError('boom')):
            self.assertFalse(gh_budget.should_skip('heal_x'))


class KernelGuardTests(unittest.TestCase):
    """The shared task_terminal_state probe now sources PR state from the
    phase-2 shared snapshot (gh_pr_snapshot.all_prs) instead of firing its own
    gh query. The gh_budget backoff moved into the reader's live-fallback path;
    the kernel itself no longer calls gh directly. This is the highest-leverage
    consumer (covers the ~10 healers that share it)."""

    def setUp(self):
        gh_budget._reset_cache()
        self.addCleanup(gh_budget._reset_cache)
        import task_terminal_state as tts
        self.tts = tts

    def test_kernel_does_not_fire_gh_directly(self):
        gh = mock.Mock(return_value=[])
        snap = mock.Mock(return_value=[])
        with mock.patch.object(self.tts, 'gh_json', gh), \
                mock.patch.object(self.tts, '_snapshot_all_prs', snap):
            self.tts.task_terminal_state('some-task-id', repos=['owner/repo'])
        gh.assert_not_called()  # phase-2: no direct GraphQL burn from the kernel

    def test_consults_shared_snapshot_source(self):
        snap = mock.Mock(return_value=[])
        with mock.patch.object(self.tts, '_snapshot_all_prs', snap):
            self.tts.task_terminal_state('some-task-id', repos=['owner/repo'])
        snap.assert_called()  # kernel reads the shared cached snapshot


if __name__ == '__main__':
    unittest.main()
