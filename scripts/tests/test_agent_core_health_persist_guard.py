#!/usr/bin/env python3
"""Tests for the persist-2-runs actionable-only guard in agent_core_health_check.

The guard suppresses the transient mid-cycle dirty-tree window (Pulse committing
mid-cycle) by alerting only about issues that were ALSO flagged on the previous
run. Covers the pure gate (persisted_issues) and the state round-trip.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_core_health_check as health  # noqa: E402


class TestPersistedIssuesGate(unittest.TestCase):
    def test_first_occurrence_not_persisted(self):
        issues = [('clean_tree', {'detail': '1 untracked'})]
        # Nothing flagged on the prior run -> nothing persisted -> no alert.
        self.assertEqual(health.persisted_issues(issues, set()), [])

    def test_same_issue_two_runs_persists(self):
        issues = [('clean_tree', {'detail': '1 untracked'})]
        out = health.persisted_issues(issues, {'clean_tree'})
        self.assertEqual([name for name, _ in out], ['clean_tree'])

    def test_only_overlapping_issues_persist(self):
        # clean_tree carried over from last run; origin_sync is brand new.
        issues = [('clean_tree', {'detail': 'x'}), ('origin_sync', {'detail': 'y'})]
        out = health.persisted_issues(issues, {'clean_tree'})
        self.assertEqual([name for name, _ in out], ['clean_tree'])

    def test_transient_clears_between_runs(self):
        # Prior run had a (now-resolved) blip; this run flags a different issue.
        issues = [('branch', {'detail': 'on feature'})]
        self.assertEqual(health.persisted_issues(issues, {'clean_tree'}), [])


class TestStateRoundTrip(unittest.TestCase):
    def setUp(self):
        self._saved = health.HEALTH_STATE_FILE
        self._tmp = tempfile.TemporaryDirectory()
        health.HEALTH_STATE_FILE = pathlib.Path(self._tmp.name) / 'state.json'

    def tearDown(self):
        health.HEALTH_STATE_FILE = self._saved
        self._tmp.cleanup()

    def test_write_then_read(self):
        health.write_issue_names({'clean_tree', 'branch'})
        self.assertEqual(health.read_prior_issue_names(), {'branch', 'clean_tree'})

    def test_read_missing_file_is_empty(self):
        self.assertEqual(health.read_prior_issue_names(), set())

    def test_clearing_resets(self):
        health.write_issue_names({'clean_tree'})
        health.write_issue_names(set())
        self.assertEqual(health.read_prior_issue_names(), set())


if __name__ == '__main__':
    unittest.main()
