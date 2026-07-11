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
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

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


class TestSyncFreshnessPushFailDedup(unittest.TestCase):
    """sync_freshness must NOT independently DM for an auto-commit-push ERROR:
    sync_agent_core.sh owns that (persistence-gated) DM. The consecutive_push_
    failures counter — written ONLY by sync's push-fail path — is the signal:
    any errored sync carrying a positive counter is deferred (treated healthy);
    every OTHER errored sync (counter 0) still flags for human attention."""

    def setUp(self):
        self._saved = health.SYNC_STATUS_FILE
        self._tmp = tempfile.TemporaryDirectory()
        health.SYNC_STATUS_FILE = pathlib.Path(self._tmp.name) / 'sync.json'

    def tearDown(self):
        health.SYNC_STATUS_FILE = self._saved
        self._tmp.cleanup()

    def _write_status(self, **fields) -> None:
        base = {
            'last_sync': datetime.now(timezone.utc).isoformat(),
            'status': 'error',
        }
        base.update(fields)
        health.SYNC_STATUS_FILE.write_text(json.dumps(base))

    def test_below_threshold_push_fail_is_healthy(self):
        # A single self-healing race: sync stays silent, so must the health check.
        self._write_status(
            message='Auto-commit push failed; rolled back',
            consecutive_push_failures=1,
        )
        result = health.check_sync_freshness()
        self.assertTrue(result['ok'])
        self.assertTrue(result.get('deferred_to_sync'))

    def test_at_threshold_push_fail_still_deferred_no_duplicate(self):
        # Persistent: sync emits its single DM, so the health check still defers
        # (never a second DM for the same condition).
        self._write_status(
            message='Auto-commit push failed; rolled back',
            consecutive_push_failures=3,
        )
        result = health.check_sync_freshness()
        self.assertTrue(result['ok'])
        self.assertTrue(result.get('deferred_to_sync'))

    def test_non_push_fail_error_still_flags(self):
        # A different errored sync (counter 0, e.g. validation-failed) is NOT the
        # push-fail class and must still surface for human attention.
        self._write_status(
            message='Validation failed for commit abc123',
            consecutive_push_failures=0,
        )
        result = health.check_sync_freshness()
        self.assertFalse(result['ok'])
        self.assertIn('ERRORED', result['detail'])

    def test_error_without_counter_field_still_flags(self):
        # Back-compat: a status written before the counter existed (no field)
        # defaults to 0 and is treated as a genuine, flaggable error.
        self._write_status(message='Fast-forward merge failed')
        result = health.check_sync_freshness()
        self.assertFalse(result['ok'])

    def test_malformed_counter_treated_as_zero_and_flags(self):
        self._write_status(
            message='Auto-commit push failed; rolled back',
            consecutive_push_failures='not-an-int',
        )
        result = health.check_sync_freshness()
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
