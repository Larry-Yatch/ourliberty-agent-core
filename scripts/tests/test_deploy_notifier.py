#!/usr/bin/env python3
"""Tests for deploy_notifier (E2.2).

Covers state-machine surface (empty registry, READY/ERROR DMs, BUILDING
skipped, branch_filter glob, PR resolution paths) and orchestration
(kill-switch, dry-run + activation DM, 401 infra alert + 24h throttle,
5xx no-DM, pagination, LRU prune, CLI flags). Vercel + gh CLI calls are
stubbed; no network I/O.

Path isolation: the autouse setUpModule fixture sets
OURLIBERTY_AGENTS_ROOT to a tmpdir and reloads the module BEFORE any test
exercises log/state/heartbeat. Otherwise, AGENTS_ROOT would resolve to
/home/larry/agents and tests would pollute the production log file.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_deploy_notifier
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Path-isolation: monkeypatch the env var BEFORE importing the module so
# AGENTS_ROOT (and every path derived from it) resolves into a tmpdir
# instead of /home/larry/agents.
_TMP_AGENTS_ROOT = tempfile.TemporaryDirectory(prefix='deploy-notifier-test-')
os.environ['OURLIBERTY_AGENTS_ROOT'] = _TMP_AGENTS_ROOT.name

import deploy_notifier as dn  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest API
    # This module drives a Layer B-guarded chokepoint against the tmpdir tree
    # above; opt out so the guard passes through to the test's mocks (the #428
    # real-tree leak scanner still runs).
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest API
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)
    _TMP_AGENTS_ROOT.cleanup()


# ---------- fixtures ----------

def _registry(entries=None):
    return {
        '$schema_version': 1,
        '_doc': 'test',
        'deploy_targets': entries or [],
        'known_frameworks': ['nextjs', 'sveltekit', 'vite', 'astro', 'remix', 'other'],
    }


def _target(name, vercel_project_id, **overrides):
    base = {
        'name': name,
        'github_repo': f'Larry-Yatch/{name}',
        'vercel_project_id': vercel_project_id,
        'vercel_org_id': None,
        'framework': 'nextjs',
        'env_var_keys': [],
        'branch_filter': None,
        'preview_enabled': True,
        'production_enabled': False,
        'created_at': '2026-05-20',
        'notes': '',
    }
    base.update(overrides)
    return base


def _deployment(
    uid='dpl_abc', project_id='prj_x', state='READY',
    branch='forge/feature-x', url='ourliberty-dashboard-pr-7.vercel.app',
    pr_id=None, commit_message='Add login flow',
):
    meta = {
        'githubCommitRef': branch,
        'githubCommitMessage': commit_message,
        'githubRepo': 'ourliberty-dashboard',
        'githubOrg': 'Larry-Yatch',
    }
    if pr_id is not None:
        meta['githubPrId'] = pr_id
    return {
        'uid': uid,
        'name': 'ourliberty-dashboard',
        'projectId': project_id,
        'url': url,
        'state': state,
        'meta': meta,
        'createdAt': 1716000000000,
    }


def _state():
    return {
        '$schema_version': 1,
        'notified': {},
        'notified_order': [],
        '_meta': {'infra_alert_last_at': {}},
    }


# ---------- pure-logic units ----------

class LoadVercelTokenTest(unittest.TestCase):
    def test_extracts_value(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('FOO=bar\nVERCEL_TOKEN=abc123\n')
            self.assertEqual(dn.load_vercel_token(p), 'abc123')

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('OTHER=value\n')
            self.assertIsNone(dn.load_vercel_token(p))


class BranchMatchesTest(unittest.TestCase):
    def test_null_filter_matches_any_branch(self):
        self.assertTrue(dn._branch_matches('main', None))
        self.assertTrue(dn._branch_matches('forge/x', None))

    def test_exact_match(self):
        self.assertTrue(dn._branch_matches('main', 'main'))
        self.assertFalse(dn._branch_matches('forge/x', 'main'))

    def test_glob_match(self):
        self.assertTrue(dn._branch_matches('forge/login-flow', 'forge/*'))
        self.assertFalse(dn._branch_matches('main', 'forge/*'))

    def test_missing_branch_with_filter_fails_closed(self):
        self.assertFalse(dn._branch_matches(None, 'main'))


class PrResolutionTest(unittest.TestCase):
    def test_meta_prid_int(self):
        d = _deployment(pr_id=7)
        n, src = dn._resolve_pr_number(d, _target('p', 'prj_x'))
        self.assertEqual((n, src), (7, 'meta'))

    def test_meta_prid_string(self):
        d = _deployment(pr_id='42')
        n, src = dn._resolve_pr_number(d, _target('p', 'prj_x'))
        self.assertEqual((n, src), (42, 'meta'))

    def test_gh_cli_fallback_succeeds(self):
        d = _deployment(pr_id=None)
        runner = mock.Mock(return_value=11)
        n, src = dn._resolve_pr_number(
            d, _target('p', 'prj_x'), gh_runner=runner,
        )
        self.assertEqual((n, src), (11, 'gh'))
        runner.assert_called_once_with('forge/feature-x', 'Larry-Yatch/p')

    def test_both_paths_fail_unknown(self):
        d = _deployment(pr_id=None)
        runner = mock.Mock(return_value=None)
        n, src = dn._resolve_pr_number(
            d, _target('p', 'prj_x'), gh_runner=runner,
        )
        self.assertEqual((n, src), (None, 'unknown'))


class TargetMatchTest(unittest.TestCase):
    def test_matches_on_project_id(self):
        d = _deployment(project_id='prj_match')
        t = _target('p', 'prj_match')
        self.assertIs(dn._target_for_deployment(d, [t]), t)

    def test_no_match_returns_none(self):
        d = _deployment(project_id='prj_other')
        t = _target('p', 'prj_match')
        self.assertIsNone(dn._target_for_deployment(d, [t]))


# ---------- orchestration ----------

class OrchestrationBase(unittest.TestCase):
    def setUp(self):
        self._dm_calls = []

        def fake_dm(message, subject, suggested_action='', severity='warning'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
            })
            return True

        self._dm_patch = mock.patch.object(dn, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

        self._log_lines = []

        def fake_log(msg, level='INFO'):
            self._log_lines.append(msg)

        self._log_patch = mock.patch.object(dn, 'log', fake_log)
        self._log_patch.start()
        self.addCleanup(self._log_patch.stop)


class EmptyRegistryTest(OrchestrationBase):
    def test_empty_targets_no_dm_no_api_call(self):
        fetch = mock.Mock()
        counts = dn.run_once(
            registry=_registry([]), state=_state(),
            vercel_token='fake-token', fetch_fn=fetch,
        )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])
        fetch.assert_not_called()


class NoMatchingDeploymentsTest(OrchestrationBase):
    def test_zero_deployments_no_dm(self):
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_dash')]),
            state=_state(), vercel_deployments=[], dry_run_override=False,
        )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])


class ReadyLogOnlyTest(OrchestrationBase):
    def test_ready_logs_preview_url_no_dm(self):
        d = _deployment(uid='dpl_r1', project_id='prj_dash',
                        state='READY', pr_id=7)
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_dash')]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False,
        )
        self.assertEqual(counts['ready'], 1)
        # READY is log-only: no DM appended.
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])
        # Preview URL is preserved in the log.
        log_blob = '\n'.join(self._log_lines)
        self.assertIn('log-only', log_blob)
        self.assertIn('https://ourliberty-dashboard-pr-7.vercel.app', log_blob)
        self.assertIn('PR #7', log_blob)

    def test_ready_with_gh_fallback_logs_pr(self):
        d = _deployment(uid='dpl_r2', project_id='prj_dash',
                        state='READY', pr_id=None)
        runner = mock.Mock(return_value=11)
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_dash')]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False, gh_runner=runner,
        )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])
        self.assertIn('PR #11', '\n'.join(self._log_lines))

    def test_ready_log_only_dedups_across_ticks(self):
        reg = _registry([_target('dash', 'prj_dash')])
        state = _state()
        d = _deployment(uid='dpl_r3', project_id='prj_dash',
                        state='READY', pr_id=7)
        dn.run_once(registry=reg, state=state, vercel_deployments=[d],
                    dry_run_override=False)
        # Second tick on the same READY uid: deduped, still no DM.
        self._log_lines.clear()
        counts = dn.run_once(registry=reg, state=state, vercel_deployments=[d],
                             dry_run_override=False)
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(counts['skipped_already_notified'], 1)
        self.assertEqual(self._dm_calls, [])


class ErrorDmTest(OrchestrationBase):
    def test_error_fires_critical_dm(self):
        d = _deployment(uid='dpl_e1', project_id='prj_dash',
                        state='ERROR', pr_id=9)
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_dash')]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False,
        )
        self.assertEqual(counts['error'], 1)
        self.assertEqual(counts['dm_sent'], 1)
        call = self._dm_calls[0]
        self.assertEqual(call['subject'], 'deploy-notifier:ERROR:dpl_e1')
        self.assertEqual(call['severity'], 'critical')
        self.assertIn('FAILED', call['message'])
        self.assertIn('PR #9', call['message'])


class SkippedStateTest(OrchestrationBase):
    def test_building_state_skipped(self):
        d = _deployment(state='BUILDING')
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_x')]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False,
        )
        self.assertEqual(counts['ready'], 0)
        self.assertEqual(counts['error'], 0)
        self.assertEqual(counts['skipped_state'], 1)
        self.assertEqual(self._dm_calls, [])

    def test_canceled_state_skipped(self):
        d = _deployment(state='CANCELED')
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_x')]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False,
        )
        self.assertEqual(counts['skipped_state'], 1)
        self.assertEqual(self._dm_calls, [])


class BranchFilterTest(OrchestrationBase):
    def test_filter_main_skips_forge_branch(self):
        d = _deployment(branch='forge/feature-x', pr_id=1)
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_x', branch_filter='main')]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False,
        )
        self.assertEqual(counts['skipped_branch_filter'], 1)
        self.assertEqual(self._dm_calls, [])

    def test_filter_forge_glob_matches(self):
        d = _deployment(branch='forge/feature-x', state='ERROR', pr_id=1)
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_x', branch_filter='forge/*')]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False,
        )
        self.assertEqual(counts['dm_sent'], 1)

    def test_filter_null_matches_any(self):
        d = _deployment(branch='main', state='ERROR', pr_id=1)
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_x', branch_filter=None)]),
            state=_state(), vercel_deployments=[d],
            dry_run_override=False,
        )
        self.assertEqual(counts['dm_sent'], 1)


class DedupTest(OrchestrationBase):
    def test_same_uid_same_state_no_redm(self):
        reg = _registry([_target('dash', 'prj_x')])
        state = _state()
        d = _deployment(uid='dpl_d1', project_id='prj_x', state='ERROR', pr_id=1)
        dn.run_once(registry=reg, state=state, vercel_deployments=[d],
                    dry_run_override=False)
        self.assertEqual(len(self._dm_calls), 1)
        self._dm_calls.clear()
        counts = dn.run_once(registry=reg, state=state, vercel_deployments=[d],
                             dry_run_override=False)
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(counts['skipped_already_notified'], 1)
        self.assertEqual(self._dm_calls, [])

    def test_state_transition_ready_to_error_redms(self):
        reg = _registry([_target('dash', 'prj_x')])
        state = _state()
        d_ready = _deployment(uid='dpl_t1', project_id='prj_x',
                              state='READY', pr_id=1)
        dn.run_once(registry=reg, state=state, vercel_deployments=[d_ready],
                    dry_run_override=False)
        self._dm_calls.clear()
        d_error = _deployment(uid='dpl_t1', project_id='prj_x',
                              state='ERROR', pr_id=1)
        counts = dn.run_once(registry=reg, state=state,
                             vercel_deployments=[d_error],
                             dry_run_override=False)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(self._dm_calls[0]['severity'], 'critical')


class DryRunActivationTest(OrchestrationBase):
    def test_dry_run_fires_activation_dm_once_then_suppresses(self):
        reg = _registry([_target('dash', 'prj_x')])
        state = _state()
        d = _deployment(uid='dpl_a1', project_id='prj_x',
                        state='READY', pr_id=1)
        counts = dn.run_once(
            registry=reg, state=state, vercel_deployments=[d],
            dry_run_override=True,
        )
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(
            self._dm_calls[0]['subject'],
            'deploy-notifier: activate to receive preview-URL DMs',
        )
        # Pending deployment was NOT actually DMed (just logged + counted).
        self.assertEqual(counts['ready'], 1)
        # Second dry-run tick with a NEW deployment: no second activation DM.
        self._dm_calls.clear()
        d2 = _deployment(uid='dpl_a2', project_id='prj_x',
                         state='READY', pr_id=2)
        counts2 = dn.run_once(
            registry=reg, state=state, vercel_deployments=[d2],
            dry_run_override=True,
        )
        self.assertEqual(counts2['dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])


class InfraAlertTest(OrchestrationBase):
    def test_missing_vercel_token_emits_infra_alert(self):
        # No token → infra alert. Mock load_vercel_token directly because
        # the default ENV_FILE arg is captured at function-def time and
        # patching dn.ENV_FILE alone wouldn't affect a no-arg call.
        with mock.patch.object(dn, 'load_vercel_token', return_value=None):
            state = _state()
            with self.assertRaises(RuntimeError):
                dn.run_once(
                    registry=_registry([_target('dash', 'prj_x')]),
                    state=state, dry_run_override=False,
                )
        self.assertEqual(len(self._dm_calls), 1)
        self.assertEqual(self._dm_calls[0]['severity'], 'critical')
        self.assertIn('VERCEL_TOKEN', self._dm_calls[0]['message'])

    def test_vercel_auth_error_emits_critical_and_raises(self):
        def boom(token):
            raise dn.VercelAuthError('401 from Vercel on /v6/deployments')

        state = _state()
        with self.assertRaises(dn.VercelAuthError):
            dn.run_once(
                registry=_registry([_target('dash', 'prj_x')]),
                state=state, vercel_token='fake', fetch_fn=boom,
                dry_run_override=False,
            )
        self.assertEqual(len(self._dm_calls), 1)
        self.assertEqual(self._dm_calls[0]['severity'], 'critical')
        self.assertEqual(
            self._dm_calls[0]['subject'],
            'deploy-notifier:INFRASTRUCTURE_ALERT:vercel-auth',
        )

    def test_auth_alert_throttled_within_24h(self):
        def boom(token):
            raise dn.VercelAuthError('401')

        state = _state()
        now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        with self.assertRaises(dn.VercelAuthError):
            dn.run_once(
                registry=_registry([_target('dash', 'prj_x')]),
                state=state, vercel_token='fake', fetch_fn=boom,
                dry_run_override=False, now=now,
            )
        self.assertEqual(len(self._dm_calls), 1)
        self._dm_calls.clear()
        # Next tick within 24h: same auth error, but DM is throttled.
        with self.assertRaises(dn.VercelAuthError):
            dn.run_once(
                registry=_registry([_target('dash', 'prj_x')]),
                state=state, vercel_token='fake', fetch_fn=boom,
                dry_run_override=False,
                now=now + timedelta(hours=12),
            )
        self.assertEqual(self._dm_calls, [])

    def test_5xx_network_error_no_dm(self):
        # _vercel_get returns (502, None) for non-auth HTTP errors;
        # fetch_vercel_deployments breaks out of pagination on status != 200
        # and returns []. No deployments → no DM. Exit cleanly.
        def fake_fetch(token):
            return []  # mimics the "aborted pagination" return shape

        state = _state()
        counts = dn.run_once(
            registry=_registry([_target('dash', 'prj_x')]),
            state=state, vercel_token='fake', fetch_fn=fake_fetch,
            dry_run_override=False,
        )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])


class PaginationTest(unittest.TestCase):
    def test_two_pages_aggregated(self):
        page1 = {
            'deployments': [_deployment(uid='dpl_1')],
            'pagination': {'next': 1716000000000},
        }
        page2 = {
            'deployments': [_deployment(uid='dpl_2')],
            'pagination': {'next': None},
        }
        responses = iter([(200, page1), (200, page2)])

        def fake_get(path, token, params=None, timeout=20):
            return next(responses)

        with mock.patch.object(dn, '_vercel_get', side_effect=fake_get):
            deployments = dn.fetch_vercel_deployments('fake-token')
        self.assertEqual([d['uid'] for d in deployments], ['dpl_1', 'dpl_2'])

    def test_page_cap_stops_traversal(self):
        # Always returns "next" to force unbounded pagination if not capped.
        call_count = {'n': 0}

        def fake_get(path, token, params=None, timeout=20):
            call_count['n'] += 1
            return (200, {
                'deployments': [_deployment(uid=f'dpl_{call_count["n"]}')],
                'pagination': {'next': call_count['n']},
            })

        with mock.patch.object(dn, '_vercel_get', side_effect=fake_get):
            deployments = dn.fetch_vercel_deployments('fake-token')
        self.assertEqual(call_count['n'], dn.VERCEL_PAGE_CAP)
        self.assertEqual(len(deployments), dn.VERCEL_PAGE_CAP)


class LruPruneTest(unittest.TestCase):
    def test_notified_history_capped(self):
        state = _state()
        # Fill past the cap.
        for i in range(dn.NOTIFIED_HISTORY_CAP + 25):
            dn._record_notified(state, f'dpl_{i}', 'READY')
        self.assertEqual(len(state['notified_order']), dn.NOTIFIED_HISTORY_CAP)
        self.assertEqual(len(state['notified']), dn.NOTIFIED_HISTORY_CAP)
        # Oldest 25 evicted; newest must still be present.
        self.assertNotIn('dpl_0:READY', state['notified'])
        self.assertIn(f'dpl_{dn.NOTIFIED_HISTORY_CAP + 24}:READY',
                      state['notified'])


class KillSwitchTest(OrchestrationBase):
    def test_kill_switch_exits_clean(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(dn, 'KILL_SWITCH', Path(td) / 'on'):
                (Path(td) / 'on').write_text('1')
                counts = dn.run_once(
                    registry=_registry([_target('dash', 'prj_x')]),
                    state=_state(),
                    vercel_deployments=[_deployment()],
                    dry_run_override=False,
                )
                self.assertEqual(counts['dm_sent'], 0)


class CliFlagsTest(unittest.TestCase):
    def test_dry_run_flag(self):
        parsed = dn._parse_args(['deploy_notifier.py', '--once', '--dry-run'])
        self.assertTrue(parsed.dry_run)
        self.assertTrue(parsed.once)

    def test_once_alone(self):
        parsed = dn._parse_args(['deploy_notifier.py', '--once'])
        self.assertFalse(parsed.dry_run)
        self.assertTrue(parsed.once)


class PathIsolationTest(unittest.TestCase):
    """Guard: AGENTS_ROOT must derive from OURLIBERTY_AGENTS_ROOT env var.

    Without this, healer scripts hardcode '/home/larry/agents/...' and the
    test suite pollutes the production log file.
    """

    def test_agents_root_inside_tmpdir(self):
        # The module-level fixture set OURLIBERTY_AGENTS_ROOT to _TMP_AGENTS_ROOT BEFORE
        # import; dn.AGENTS_ROOT is FROZEN to that value. Compare against the fixture value
        # we control — NOT live os.environ, which a sibling test module can mutate after our
        # import under `discover` (a leaked write flipped this test by discovery order). See
        # [[test-isolation-hygiene-debt]].
        expected = _TMP_AGENTS_ROOT.name
        self.assertEqual(str(dn.AGENTS_ROOT), expected)
        self.assertTrue(str(dn.LOG_FILE).startswith(expected))
        self.assertTrue(str(dn.STATE_FILE).startswith(expected))


if __name__ == '__main__':
    unittest.main()
