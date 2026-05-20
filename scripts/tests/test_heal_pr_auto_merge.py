#!/usr/bin/env python3
"""Tests for heal_pr_auto_merge (E1.3).

Covers the pure-logic surface (log parsing, mergeability predicate, state
file CRUD, retry-budget gate) plus the orchestration loop with gh shell-out
mocked. Subprocess invocations are NEVER live in tests — every gh call is
either monkey-patched or skipped via the dry-run path.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_pr_auto_merge
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_pr_auto_merge as h  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test.

    Why: heal_pr_auto_merge's LOG_FILE / STATE_FILE / HEARTBEAT_FILE /
    KILL_SWITCH derive from AGENTS_ROOT at import time. Without this
    redirection, running these tests in a worktree pollutes prod
    `/home/larry/agents/...` state. Reload the module so its module-level
    constants pick up the override.
    """

    def setUp(self):
        super().setUp()
        self._isolated_tmp = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
            os.makedirs(os.path.join(self._isolated_tmp, sub), exist_ok=True)
        self._isolated_env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_tmp
        importlib.reload(h)

    def tearDown(self):
        if self._isolated_env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_env_orig
        importlib.reload(h)
        shutil.rmtree(self._isolated_tmp, ignore_errors=True)
        super().tearDown()


# -------------------- log scanning --------------------

class FindMirrorPassedFailuresTest(_IsolatedAgentsRoot):
    """The healer only acts on PRs that already had a Mirror-PASS attempt.
    That signal comes from outbox-notifier.log AUTO_MERGE entries."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / 'outbox-notifier.log'

    def _write_log(self, lines: list[str]) -> None:
        self.log_path.write_text('\n'.join(lines) + '\n')

    def test_empty_log_returns_empty_dict(self):
        self.log_path.write_text('')
        self.assertEqual(h.find_mirror_passed_failures(self.log_path), {})

    def test_missing_log_returns_empty_dict(self):
        missing = Path(self.tmp.name) / 'does-not-exist.log'
        self.assertEqual(h.find_mirror_passed_failures(missing), {})

    def test_extracts_failed_entry_raw_url(self):
        self._write_log([
            '[2026-05-19T16:00:00+00:00] [WARN] AUTO_MERGE task=abc-001 '
            'pr=https://github.com/x/y/pull/1 outcome=failed reason=timeout '
            'after 30s',
        ])
        out = h.find_mirror_passed_failures(self.log_path)
        self.assertIn('https://github.com/x/y/pull/1', out)
        entry = out['https://github.com/x/y/pull/1']
        self.assertEqual(entry['task_id'], 'abc-001')
        self.assertIn('timeout', entry['reason'])

    def test_extracts_failed_entry_quoted_url(self):
        # The malformed-URL path in outbox_notifier uses {pr_url!r} which
        # adds surrounding quotes.
        self._write_log([
            "[2026-05-19T16:00:00+00:00] [WARN] AUTO_MERGE task=abc-002 "
            "pr='https://github.com/x/y/pull/2' outcome=failed "
            "reason=malformed-pr-url",
        ])
        out = h.find_mirror_passed_failures(self.log_path)
        self.assertIn('https://github.com/x/y/pull/2', out)

    def test_keeps_most_recent_when_same_pr_fails_multiple_times(self):
        self._write_log([
            '[2026-05-19T10:00:00+00:00] [WARN] AUTO_MERGE task=t1 '
            'pr=https://github.com/x/y/pull/1 outcome=failed reason=first',
            '[2026-05-19T11:00:00+00:00] [WARN] AUTO_MERGE task=t1 '
            'pr=https://github.com/x/y/pull/1 outcome=failed reason=second',
        ])
        out = h.find_mirror_passed_failures(self.log_path)
        self.assertEqual(out['https://github.com/x/y/pull/1']['reason'],
                         'second')

    def test_ignores_succeeded_entries(self):
        self._write_log([
            '[2026-05-19T10:00:00+00:00] [INFO] AUTO_MERGE task=t1 '
            'pr=https://github.com/x/y/pull/1 outcome=merged',
        ])
        self.assertEqual(h.find_mirror_passed_failures(self.log_path), {})

    def test_lookback_window_excludes_old_failures(self):
        from datetime import datetime, timezone
        self._write_log([
            '[2025-01-01T00:00:00+00:00] [WARN] AUTO_MERGE task=old '
            'pr=https://github.com/x/y/pull/1 outcome=failed reason=ancient',
        ])
        # `now` is far in the future of the log entry; 24h lookback should
        # skip it.
        out = h.find_mirror_passed_failures(
            self.log_path, lookback_hours=24,
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(out, {})


# -------------------- mergeability predicate --------------------

class IsMergeableTest(_IsolatedAgentsRoot):
    """Per-PR pre-merge gate. Pulled verbatim from upstream + stalled-label."""

    def _pr(self, **overrides) -> dict:
        base = {
            'isDraft': False, 'title': 'feat: x',
            'mergeStateStatus': 'CLEAN', 'reviewDecision': 'APPROVED',
            'statusCheckRollup': [
                {'status': 'COMPLETED', 'conclusion': 'SUCCESS',
                 'name': 'ci'},
            ],
            'labels': [],
        }
        base.update(overrides)
        return base

    def test_clean_passes(self):
        ok, reason = h.is_mergeable(self._pr())
        self.assertTrue(ok, reason)

    def test_draft_rejected(self):
        ok, reason = h.is_mergeable(self._pr(isDraft=True))
        self.assertFalse(ok)
        self.assertIn('draft', reason)

    def test_hold_prefix_rejected(self):
        ok, reason = h.is_mergeable(self._pr(title='WIP: not done'))
        self.assertFalse(ok)
        self.assertIn('hold-prefix', reason)

    def test_changes_requested_rejected(self):
        ok, reason = h.is_mergeable(self._pr(reviewDecision='CHANGES_REQUESTED'))
        self.assertFalse(ok)

    def test_non_clean_merge_state_rejected(self):
        ok, reason = h.is_mergeable(self._pr(mergeStateStatus='BLOCKED'))
        self.assertFalse(ok)
        self.assertIn('merge-state=BLOCKED', reason)

    def test_failed_check_rejected(self):
        ok, reason = h.is_mergeable(self._pr(statusCheckRollup=[
            {'status': 'COMPLETED', 'conclusion': 'FAILURE', 'name': 'lint'},
        ]))
        self.assertFalse(ok)
        self.assertIn('check-failed', reason)

    def test_in_progress_check_rejected(self):
        ok, reason = h.is_mergeable(self._pr(statusCheckRollup=[
            {'status': 'IN_PROGRESS', 'name': 'tests'},
        ]))
        self.assertFalse(ok)
        self.assertIn('in-progress', reason)

    def test_hold_label_rejected(self):
        ok, reason = h.is_mergeable(self._pr(labels=[{'name': 'do-not-merge'}]))
        self.assertFalse(ok)
        self.assertIn('label: do-not-merge', reason)

    def test_human_blocked_label_rejected(self):
        ok, reason = h.is_mergeable(self._pr(labels=[{'name': 'human-blocked'}]))
        self.assertFalse(ok)
        self.assertIn('label: human-blocked', reason)

    def test_stalled_label_rejected(self):
        # Once the healer has labeled a PR stalled, it backs off.
        ok, reason = h.is_mergeable(self._pr(labels=[{'name': h.STALLED_LABEL}]))
        self.assertFalse(ok)
        self.assertIn(h.STALLED_LABEL, reason)


# -------------------- CANCELLED-workflow rerun helper --------------------

class FindCancelledChecksTest(_IsolatedAgentsRoot):

    def test_returns_cancelled_when_no_other_problem(self):
        pr = {'statusCheckRollup': [
            {'status': 'COMPLETED', 'conclusion': 'SUCCESS', 'name': 'a'},
            {'status': 'COMPLETED', 'conclusion': 'CANCELLED', 'name': 'b',
             'detailsUrl': 'https://github.com/x/y/actions/runs/12345'},
        ]}
        out = h.find_cancelled_checks_to_rerun(pr)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['name'], 'b')

    def test_empty_when_in_progress_check(self):
        pr = {'statusCheckRollup': [
            {'status': 'IN_PROGRESS', 'name': 'tests'},
            {'status': 'COMPLETED', 'conclusion': 'CANCELLED', 'name': 'b'},
        ]}
        self.assertEqual(h.find_cancelled_checks_to_rerun(pr), [])

    def test_empty_when_real_failure_present(self):
        pr = {'statusCheckRollup': [
            {'status': 'COMPLETED', 'conclusion': 'FAILURE', 'name': 'lint'},
            {'status': 'COMPLETED', 'conclusion': 'CANCELLED', 'name': 'b'},
        ]}
        self.assertEqual(h.find_cancelled_checks_to_rerun(pr), [])


# -------------------- state file CRUD --------------------

class StateFileTest(_IsolatedAgentsRoot):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = h.STATE_FILE
        h.STATE_FILE = Path(self.tmp.name) / 'state.json'

    def tearDown(self):
        h.STATE_FILE = self._orig
        super().tearDown()

    def test_load_missing_returns_empty(self):
        self.assertEqual(h.load_state(), {'prs': {}})

    def test_save_and_reload(self):
        s = {'prs': {'https://x/pr/1': {'attempts': 2,
                                        'stalled_alerted': False,
                                        'activation_alerted': True,
                                        'last_attempt_iso': '2026-05-19T16:00:00'}}}
        h.save_state(s)
        reloaded = h.load_state()
        self.assertEqual(reloaded['prs']['https://x/pr/1']['attempts'], 2)

    def test_load_malformed_returns_empty(self):
        h.STATE_FILE.write_text('{not json')
        self.assertEqual(h.load_state(), {'prs': {}})

    def test_get_pr_state_initializes_defaults(self):
        s = {'prs': {}}
        entry = h.get_pr_state(s, 'https://x/pr/2')
        self.assertEqual(entry['attempts'], 0)
        self.assertFalse(entry['stalled_alerted'])
        self.assertFalse(entry['activation_alerted'])


# -------------------- per-PR orchestration (mocked gh) --------------------

class ProcessPrTest(_IsolatedAgentsRoot):
    """The most important test surface: dry-run vs live, budget, stalled,
    no-mirror-pass, not-mergeable branches. Subprocess invocations are
    mocked so the test never hits gh."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_state = h.STATE_FILE
        h.STATE_FILE = Path(self.tmp.name) / 'state.json'
        # Stub dm_larry / add_stalled_label so tests don't hit real channels.
        self._dm_calls: list[dict] = []
        self._label_calls: list[tuple[str, int]] = []
        self._orig_dm = h.dm_larry
        self._orig_label = h.add_stalled_label
        h.dm_larry = lambda **kw: (self._dm_calls.append(kw) or True)
        h.add_stalled_label = lambda r, n: (
            self._label_calls.append((r, n)) or True
        )

    def tearDown(self):
        h.STATE_FILE = self._orig_state
        h.dm_larry = self._orig_dm
        h.add_stalled_label = self._orig_label
        super().tearDown()

    def _pr(self, **overrides) -> dict:
        base = {
            '_repo': 'Larry-Yatch/ourliberty-agent-core', 'number': 42,
            'url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42',
            'title': 'feat: x', 'isDraft': False,
            'mergeStateStatus': 'CLEAN', 'reviewDecision': 'APPROVED',
            'statusCheckRollup': [
                {'status': 'COMPLETED', 'conclusion': 'SUCCESS', 'name': 'ci'},
            ],
            'labels': [],
        }
        base.update(overrides)
        return base

    def test_no_mirror_pass_returns_no_mirror_pass(self):
        pr = self._pr()
        out = h.process_pr(pr, mirror_passed={}, state=h.load_state(),
                           dry_run=True)
        self.assertEqual(out, 'no-mirror-pass')

    def test_not_mergeable_returns_not_mergeable(self):
        pr = self._pr(mergeStateStatus='DIRTY')
        out = h.process_pr(
            pr,
            mirror_passed={pr['url']: {'task_id': 't', 'reason': 'r',
                                       'ts': ''}},
            state=h.load_state(), dry_run=True,
        )
        self.assertEqual(out, 'not-mergeable')

    def test_dry_run_candidate_dms_once_then_silent(self):
        pr = self._pr()
        mp = {pr['url']: {'task_id': 't1', 'reason': 'timeout', 'ts': ''}}
        state = h.load_state()
        out1 = h.process_pr(pr, mp, state, dry_run=True)
        self.assertEqual(out1, 'dry-run-candidate')
        self.assertEqual(len(self._dm_calls), 1)
        self.assertIn('activate', self._dm_calls[0]['subject'].lower())
        # Second tick: still a candidate, but no second DM.
        state = h.load_state()  # reload (process_pr saved it)
        out2 = h.process_pr(pr, mp, state, dry_run=True)
        self.assertEqual(out2, 'dry-run-candidate')
        self.assertEqual(len(self._dm_calls), 1)

    def test_live_merge_success_clears_state(self):
        pr = self._pr()
        mp = {pr['url']: {'task_id': 't1', 'reason': 'first-fail', 'ts': ''}}
        # Mock merge_pr to return success.
        with patch.object(h, 'merge_pr',
                          return_value=('merged', 'merged Larry/x#42')):
            out = h.process_pr(pr, mp, h.load_state(), dry_run=False)
        self.assertEqual(out, 'merged')
        state = h.load_state()
        # Successful merge clears the PR's state.
        self.assertNotIn(pr['url'], state['prs'])
        # Success DM was queued.
        self.assertEqual(len(self._dm_calls), 1)
        self.assertIn('healed', self._dm_calls[0]['subject'].lower())

    def test_live_merge_failure_increments_attempts(self):
        pr = self._pr()
        mp = {pr['url']: {'task_id': 't1', 'reason': 'first-fail', 'ts': ''}}
        with patch.object(h, 'merge_pr',
                          return_value=('failed', 'still failing')):
            out = h.process_pr(pr, mp, h.load_state(), dry_run=False)
        self.assertEqual(out, 'skipped')
        state = h.load_state()
        self.assertEqual(state['prs'][pr['url']]['attempts'], 1)
        # No DM yet on a single failure (we still have budget).
        self.assertEqual(len(self._dm_calls), 0)

    def test_budget_exhausted_labels_and_dms_once(self):
        pr = self._pr()
        mp = {pr['url']: {'task_id': 't1', 'reason': 'persistent',
                          'ts': '2026-05-19'}}
        # Pre-set attempts to the cap.
        state = h.load_state()
        entry = h.get_pr_state(state, pr['url'])
        entry['attempts'] = h.MAX_RETRIES_PER_PR
        h.save_state(state)
        out = h.process_pr(pr, mp, h.load_state(), dry_run=False)
        self.assertEqual(out, 'budget-exhausted')
        self.assertEqual(len(self._label_calls), 1)
        self.assertEqual(len(self._dm_calls), 1)
        self.assertIn('stalled', self._dm_calls[0]['subject'].lower())
        # Second tick — stalled_alerted now True, no second DM/label.
        out2 = h.process_pr(pr, mp, h.load_state(), dry_run=False)
        self.assertEqual(out2, 'budget-exhausted')
        self.assertEqual(len(self._label_calls), 1)
        self.assertEqual(len(self._dm_calls), 1)

    def test_already_merged_treated_as_success(self):
        pr = self._pr()
        mp = {pr['url']: {'task_id': 't1', 'reason': 'race', 'ts': ''}}
        with patch.object(h, 'merge_pr',
                          return_value=('already_merged', 'race condition')):
            out = h.process_pr(pr, mp, h.load_state(), dry_run=False)
        self.assertEqual(out, 'merged')


# -------------------- kill-switch + activation gate --------------------

class KillSwitchTest(_IsolatedAgentsRoot):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = h.KILL_SWITCH
        h.KILL_SWITCH = Path(self.tmp.name) / 'healers.disabled'

    def tearDown(self):
        h.KILL_SWITCH = self._orig
        super().tearDown()

    def test_absent_means_inactive(self):
        self.assertFalse(h.kill_switch_active())

    def test_present_means_active(self):
        h.KILL_SWITCH.write_text('')
        self.assertTrue(h.kill_switch_active())


class AutomergeEnabledTest(_IsolatedAgentsRoot):

    def test_unset_means_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.ENV_AUTOMERGE_ENABLED, None)
            self.assertFalse(h.automerge_enabled())

    def test_lowercase_true_enables(self):
        with patch.dict(os.environ, {h.ENV_AUTOMERGE_ENABLED: 'true'}):
            self.assertTrue(h.automerge_enabled())

    def test_uppercase_true_enables(self):
        with patch.dict(os.environ, {h.ENV_AUTOMERGE_ENABLED: 'TRUE'}):
            self.assertTrue(h.automerge_enabled())

    def test_anything_else_disabled(self):
        for v in ('1', 'yes', 'on', 'false', '0', ''):
            with patch.dict(os.environ, {h.ENV_AUTOMERGE_ENABLED: v}):
                self.assertFalse(h.automerge_enabled(),
                                 f'value {v!r} should be disabled')


if __name__ == '__main__':
    unittest.main()
