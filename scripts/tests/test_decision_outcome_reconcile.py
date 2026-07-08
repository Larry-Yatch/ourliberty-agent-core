"""Tests for scripts/decision_outcome_reconcile.py.

Coverage (Operator Feed Loop slice 2 — the build-outcome join):
- parse_pr_coord: `pr-<repo>-<n>` -> (repo, n), incl. dashed repo names; a bare
  task_id key and malformed keys -> None.
- classify: merged / merged+regression-label / closed / open / missing-blob map
  to the right verdict (or None), never a guessed terminal state.
- reconcile: joins GitHub truth (via an injected runner stub — no real gh) onto
  unresolved PR-coordinate decisions; leaves open PRs pending; skips non-PR
  (task-keyed) decisions; is idempotent (a recorded terminal outcome is not
  re-recorded); a runner error defers rather than crashing.

Uses the same tmp-root isolation as test_decision_outcome_ledger so the shared
ledger file is a scratch file.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import decision_outcome_ledger as dol  # noqa: E402
import decision_outcome_reconcile as dor  # noqa: E402


class ParsePrCoordTest(unittest.TestCase):
    def test_simple(self) -> None:
        self.assertEqual(dor.parse_pr_coord('pr-dashboard-42'),
                         ('dashboard', 42))

    def test_dashed_repo_name_round_trips(self) -> None:
        # number is the trailing digit-run, so a dashed repo survives
        self.assertEqual(dor.parse_pr_coord('pr-ourliberty-agent-core-830'),
                         ('ourliberty-agent-core', 830))

    def test_repo_name_ending_in_digits_round_trips(self) -> None:
        # the ambiguity the reviewer probed: repo name whose last segment looks
        # numeric. The PR number is the FINAL digit-run, so this is unambiguous.
        self.assertEqual(dor.parse_pr_coord('pr-my-app-2024-7'),
                         ('my-app-2024', 7))

    def test_non_pr_key_is_none(self) -> None:
        self.assertIsNone(dor.parse_pr_coord('some-task-id-abc'))
        self.assertIsNone(dor.parse_pr_coord('mission_1234'))

    def test_malformed_is_none(self) -> None:
        self.assertIsNone(dor.parse_pr_coord('pr-repo-'))     # no number
        self.assertIsNone(dor.parse_pr_coord('pr-42'))        # no repo segment
        self.assertIsNone(dor.parse_pr_coord(None))           # type: ignore[arg-type]


class ClassifyTest(unittest.TestCase):
    def test_merged(self) -> None:
        self.assertEqual(dor.classify({'state': 'MERGED', 'labels': []}),
                         'merged')

    def test_merged_with_regression_label(self) -> None:
        self.assertEqual(
            dor.classify({'state': 'MERGED',
                          'labels': [{'name': 'regression'}]}),
            'merged_regressed')

    def test_closed_unmerged(self) -> None:
        self.assertEqual(dor.classify({'state': 'CLOSED'}), 'closed_unmerged')

    def test_open_is_pending(self) -> None:
        self.assertEqual(dor.classify({'state': 'OPEN'}), 'pending')

    def test_missing_or_bad_blob_is_none(self) -> None:
        self.assertIsNone(dor.classify(None))
        self.assertIsNone(dor.classify({}))
        self.assertIsNone(dor.classify({'state': 123}))
        self.assertIsNone(dor.classify({'state': 'WEIRD'}))

    def test_lowercase_state_tolerated(self) -> None:
        self.assertEqual(dor.classify({'state': 'merged', 'labels': []}),
                         'merged')


# Real registered repo + its resolved slug, so resolve_repo_slug succeeds.
_AC = 'ourliberty-agent-core'
_AC_SLUG = 'Larry-Yatch/ourliberty-agent-core'


class ResolveRepoSlugTest(unittest.TestCase):
    def test_known_repo_resolves(self) -> None:
        self.assertEqual(dor.resolve_repo_slug('ourliberty-agent-core'),
                         'Larry-Yatch/ourliberty-agent-core')
        self.assertEqual(dor.resolve_repo_slug('ourliberty-dashboard'),
                         'Larry-Yatch/ourliberty-dashboard')

    def test_unknown_repo_is_none(self) -> None:
        self.assertIsNone(dor.resolve_repo_slug('some-other-repo'))
        self.assertIsNone(dor.resolve_repo_slug(''))


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='decision-reconcile-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        importlib.reload(dol)
        importlib.reload(dor)  # picks up the reloaded dol + tmp LOG_FILE

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(dol)
        importlib.reload(dor)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _stub(self, mapping: dict):
        """runner stub: (slug, num) -> the gh-json blob to return."""
        def runner(slug, num):
            return mapping.get((slug, num))
        return runner

    def test_records_terminal_and_keeps_pending(self) -> None:
        # three decisions: a merged PR, an open PR, and a task-keyed dispatch
        # whose bare key the kernel can't resolve (UNKNOWN => KEEP).
        dol.record_decision(f'pr-{_AC}-1', 'approved', actor='dashboard', cleared=1)
        dol.record_decision(f'pr-{_AC}-2', 'approved', actor='dashboard', cleared=1)
        dol.record_decision('dispatch-task-xyz', 'approved', actor='telegram',
                            cleared=1)
        runner = self._stub({
            (_AC_SLUG, 1): {'state': 'MERGED', 'labels': []},
            (_AC_SLUG, 2): {'state': 'OPEN'},
        })
        # bare-key seam injected so no real gh shell-out; UNKNOWN keeps pending.
        summary = dor.reconcile(runner=runner,
                                terminal_state_fn=lambda k: 'UNKNOWN')
        self.assertEqual(summary['recorded'], 1)
        # open PR (pending) + bare-key UNKNOWN (KEEP) both stay pending.
        self.assertEqual(summary['pending'], 2)
        self.assertTrue(dol.has_build_outcome(f'pr-{_AC}-1'))
        self.assertFalse(dol.has_build_outcome(f'pr-{_AC}-2'))
        self.assertFalse(dol.has_build_outcome('dispatch-task-xyz'))

    def test_unknown_repo_is_skipped_not_queried(self) -> None:
        dol.record_decision('pr-not-a-real-repo-5', 'approved', actor='x',
                            cleared=1)
        called = []

        def runner(slug, num):
            called.append((slug, num))
            return {'state': 'MERGED', 'labels': []}

        summary = dor.reconcile(runner=runner)
        self.assertEqual(summary['skipped_unknown_repo'], 1)
        self.assertEqual(summary['recorded'], 0)
        self.assertEqual(called, [])  # never queried under a guessed owner
        self.assertFalse(dol.has_build_outcome('pr-not-a-real-repo-5'))

    def test_idempotent_second_pass_records_nothing(self) -> None:
        dol.record_decision(f'pr-{_AC}-5', 'approved', actor='x', cleared=1)
        runner = self._stub({(_AC_SLUG, 5): {'state': 'MERGED', 'labels': []}})
        first = dor.reconcile(runner=runner)
        self.assertEqual(first['recorded'], 1)
        second = dor.reconcile(runner=runner)
        self.assertEqual(second['recorded'], 0)
        self.assertEqual(second['checked'], 0)  # already resolved -> not rechecked
        rows = [r for r in dol.read_recent(50)
                if r.get('kind') == 'build_outcome']
        self.assertEqual(len(rows), 1)

    def test_regression_label_recorded_as_merged_regressed(self) -> None:
        dol.record_decision(f'pr-{_AC}-9', 'approved', actor='x', cleared=1)
        runner = self._stub({
            (_AC_SLUG, 9): {'state': 'MERGED', 'labels': [{'name': 'regression'}]},
        })
        dor.reconcile(runner=runner)
        outcome_rows = [r for r in dol.records_for_key(f'pr-{_AC}-9')
                        if r.get('kind') == 'build_outcome']
        self.assertEqual(len(outcome_rows), 1)
        self.assertEqual(outcome_rows[0]['build_outcome'], 'merged_regressed')

    def test_runner_error_defers_without_crashing(self) -> None:
        dol.record_decision(f'pr-{_AC}-3', 'approved', actor='x', cleared=1)

        def boom(slug, num):
            raise RuntimeError('gh exploded')

        summary = dor.reconcile(runner=boom)
        self.assertEqual(summary['errors'], 1)
        self.assertEqual(summary['recorded'], 0)
        self.assertFalse(dol.has_build_outcome(f'pr-{_AC}-3'))  # retried next pass

    def test_runner_returning_none_is_error_not_terminal(self) -> None:
        dol.record_decision(f'pr-{_AC}-4', 'approved', actor='x', cleared=1)
        runner = self._stub({})  # (slug,4) missing -> None
        summary = dor.reconcile(runner=runner)
        self.assertEqual(summary['errors'], 1)
        self.assertEqual(summary['recorded'], 0)
        self.assertFalse(dol.has_build_outcome(f'pr-{_AC}-4'))

    # ---- item (c): bare task_id keys join via the terminal-state kernel ----

    def test_bare_key_merged_is_recorded(self) -> None:
        dol.record_decision('dispatch-task-abc', 'approved', actor='telegram',
                            cleared=1)
        summary = dor.reconcile(terminal_state_fn=lambda k: 'MERGED')
        self.assertEqual(summary['recorded'], 1)
        self.assertTrue(dol.has_build_outcome('dispatch-task-abc'))
        self.assertEqual(dol.latest_build_outcome('dispatch-task-abc'), 'merged')

    def test_bare_key_closed_is_recorded_closed_unmerged(self) -> None:
        dol.record_decision('dispatch-task-def', 'approved', actor='telegram',
                            cleared=1)
        summary = dor.reconcile(terminal_state_fn=lambda k: 'CLOSED')
        self.assertEqual(summary['recorded'], 1)
        self.assertEqual(dol.latest_build_outcome('dispatch-task-def'),
                         'closed_unmerged')

    def test_bare_key_unknown_keeps_pending_records_nothing(self) -> None:
        dol.record_decision('dispatch-task-ghi', 'approved', actor='telegram',
                            cleared=1)
        for state in ('UNKNOWN', 'OPEN'):
            summary = dor.reconcile(terminal_state_fn=lambda k, s=state: s)
            self.assertEqual(summary['recorded'], 0)
            self.assertEqual(summary['pending'], 1)
            self.assertFalse(dol.has_build_outcome('dispatch-task-ghi'))

    def test_bare_key_kernel_raises_is_error_not_terminal(self) -> None:
        dol.record_decision('dispatch-task-jkl', 'approved', actor='telegram',
                            cleared=1)

        def boom(k):
            raise RuntimeError('gh exploded')

        summary = dor.reconcile(terminal_state_fn=boom)
        self.assertEqual(summary['errors'], 1)
        self.assertEqual(summary['recorded'], 0)
        self.assertFalse(dol.has_build_outcome('dispatch-task-jkl'))

    # ---- item (b): closed_unmerged re-check + merged supersede ----

    def test_closed_unmerged_supersedes_to_merged_on_recheck(self) -> None:
        # First pass sees CLOSED -> records closed_unmerged. A later merge (a PR
        # reopened + merged) is picked up on the re-check inside the settle
        # window and supersedes.
        dol.record_decision(f'pr-{_AC}-7', 'approved', actor='x', cleared=1)
        first = dor.reconcile(
            runner=self._stub({(_AC_SLUG, 7): {'state': 'CLOSED'}}))
        self.assertEqual(first['recorded'], 1)
        self.assertEqual(dol.latest_build_outcome(f'pr-{_AC}-7'), 'closed_unmerged')
        second = dor.reconcile(
            runner=self._stub({(_AC_SLUG, 7): {'state': 'MERGED', 'labels': []}}))
        self.assertEqual(second['superseded'], 1)
        self.assertEqual(dol.latest_build_outcome(f'pr-{_AC}-7'), 'merged')

    def test_closed_unmerged_recheck_no_change_records_nothing(self) -> None:
        dol.record_decision(f'pr-{_AC}-8', 'approved', actor='x', cleared=1)
        dor.reconcile(runner=self._stub({(_AC_SLUG, 8): {'state': 'CLOSED'}}))
        second = dor.reconcile(
            runner=self._stub({(_AC_SLUG, 8): {'state': 'CLOSED'}}))
        self.assertEqual(second['recorded'], 0)
        self.assertEqual(second['superseded'], 0)
        self.assertEqual(second['rechecked_no_change'], 1)
        rows = [r for r in dol.records_for_key(f'pr-{_AC}-8')
                if r.get('kind') == 'build_outcome']
        self.assertEqual(len(rows), 1)  # no churn — still one row

    # ---- item (a): the single-writer lock ----

    def test_run_once_skips_when_lock_held(self) -> None:
        import decision_outcome_reconcile as _dor
        with _dor._reconcile_lock() as acquired:
            self.assertTrue(acquired)
            # A second acquisition while the first is held must be refused.
            summary = _dor.run_once()
            self.assertEqual(summary, {'skipped_locked': True})

    def test_run_once_runs_when_lock_free(self) -> None:
        dol.record_decision(f'pr-{_AC}-11', 'approved', actor='x', cleared=1)
        summary = dor.run_once(
            runner=self._stub({(_AC_SLUG, 11): {'state': 'MERGED', 'labels': []}}))
        self.assertNotIn('skipped_locked', summary)
        self.assertEqual(summary['recorded'], 1)


if __name__ == '__main__':
    unittest.main()
