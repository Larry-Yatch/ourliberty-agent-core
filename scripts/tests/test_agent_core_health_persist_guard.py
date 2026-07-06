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


class TestPushFailOnlyDedup(unittest.TestCase):
    """The sync push-fail (origin_sync ahead/unpushed) is already DM'd via the
    sync.service sync-blocked digest; when it's the SOLE persisted issue, the
    summary escalate DM is a duplicate and must be suppressed. Every other case
    still DMs.
    """

    _UNPUSHED = {
        'signal': 'unpushed-commits',
        'detail': 'local ahead of origin/main (unpushed commits)',
    }

    def test_only_push_fail_is_suppressed(self):
        persisted = [('origin_sync', dict(self._UNPUSHED))]
        self.assertTrue(health._is_push_fail_only(persisted))

    def test_push_fail_plus_other_issue_still_dms(self):
        # Mixed set: the digest doesn't cover clean_tree, so the summary must fire.
        persisted = [
            ('origin_sync', dict(self._UNPUSHED)),
            ('clean_tree', {'detail': '1 untracked'}),
        ]
        self.assertFalse(health._is_push_fail_only(persisted))

    def test_origin_sync_diverged_shape_still_dms(self):
        # Divergence is NOT the digest-covered push-fail shape (no signal key).
        diverged = {'detail': 'local and origin/main have DIVERGED'}
        self.assertFalse(health._is_push_fail_only([('origin_sync', diverged)]))

    def test_non_push_fail_issue_still_dms(self):
        self.assertFalse(
            health._is_push_fail_only([('branch', {'detail': 'on feature'})])
        )

    def test_empty_persisted_is_not_push_fail(self):
        self.assertFalse(health._is_push_fail_only([]))

    def test_check_origin_sync_stamps_signal_on_ahead(self):
        # Guard against a rename of the 'ahead/unpushed' return shape that the
        # dedup keys on: the branch must carry signal='unpushed-commits'.
        import inspect
        src = inspect.getsource(health.check_origin_sync)
        self.assertIn("'signal': 'unpushed-commits'", src)


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
