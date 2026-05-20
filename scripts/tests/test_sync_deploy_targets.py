#!/usr/bin/env python3
"""Tests for sync_deploy_targets (E2.1).

Covers pure-logic surface (token parser, drift detection, DM rendering,
state-file dedup) plus orchestration with kill-switch + dry-run + activation
+ Vercel auth error. Vercel API calls are stubbed; no network I/O.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_sync_deploy_targets
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import sync_deploy_targets as s  # noqa: E402


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


def _vercel_project(pid, name):
    return {'id': pid, 'name': name}


class LoadVercelTokenTest(unittest.TestCase):
    def test_extracts_unquoted_value(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('FOO=bar\nVERCEL_TOKEN=abc123\nBAZ=qux\n')
            self.assertEqual(s.load_vercel_token(p), 'abc123')

    def test_extracts_quoted_value(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('VERCEL_TOKEN="abc 123"\n')
            self.assertEqual(s.load_vercel_token(p), 'abc 123')

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('OTHER=value\n')
            self.assertIsNone(s.load_vercel_token(p))

    def test_empty_value_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('VERCEL_TOKEN=\n')
            self.assertIsNone(s.load_vercel_token(p))

    def test_missing_file_returns_none(self):
        self.assertIsNone(s.load_vercel_token(Path('/tmp/no-such-env-xyzzy')))


class DetectDriftTest(unittest.TestCase):
    def test_empty_registry_empty_vercel_no_drift(self):
        drifts, live = s.detect_drift(_registry([]), [])
        self.assertEqual(drifts, [])
        self.assertEqual(live, set())

    def test_missing_from_vercel(self):
        reg = _registry([_target('ghost', 'prj_ghost123')])
        drifts, _ = s.detect_drift(reg, [])
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertEqual(kinds, [('ghost', 'MISSING_FROM_VERCEL')])

    def test_missing_from_registry(self):
        reg = _registry([])
        drifts, _ = s.detect_drift(reg, [_vercel_project('prj_orphan9', 'orphan')])
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertEqual(kinds, [('prj_orphan9', 'MISSING_FROM_REGISTRY')])

    def test_name_mismatch(self):
        reg = _registry([_target('dashboard', 'prj_match42')])
        drifts, _ = s.detect_drift(reg, [_vercel_project('prj_match42', 'dashboard-renamed')])
        self.assertEqual(len(drifts), 1)
        name, kind, payload = drifts[0]
        self.assertEqual((name, kind), ('dashboard', 'NAME_MISMATCH'))
        self.assertEqual(payload['vercel_name'], 'dashboard-renamed')

    def test_no_drift_when_both_sides_aligned(self):
        reg = _registry([_target('aligned', 'prj_aligned1')])
        drifts, _ = s.detect_drift(reg, [_vercel_project('prj_aligned1', 'aligned')])
        self.assertEqual(drifts, [])


class DedupTest(unittest.TestCase):
    def test_first_drift_is_re_dm_eligible(self):
        state = {'drifts': {}, '_meta': {}}
        self.assertTrue(s._should_re_dm(state, 'ghost', 'MISSING_FROM_VERCEL'))

    def test_just_dmed_blocks_re_dm(self):
        state = {'drifts': {}, '_meta': {}}
        now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        s._record_dm(state, 'ghost', 'MISSING_FROM_VERCEL', now=now)
        self.assertFalse(s._should_re_dm(
            state, 'ghost', 'MISSING_FROM_VERCEL',
            now=now + timedelta(hours=23),
        ))

    def test_after_window_re_dm_eligible(self):
        state = {'drifts': {}, '_meta': {}}
        old = datetime(2026, 5, 20, 6, 0, tzinfo=timezone.utc)
        s._record_dm(state, 'ghost', 'MISSING_FROM_VERCEL', now=old)
        self.assertTrue(s._should_re_dm(
            state, 'ghost', 'MISSING_FROM_VERCEL',
            now=old + timedelta(hours=25),
        ))


class OrchestrationTest(unittest.TestCase):
    def setUp(self):
        self._dm_calls = []

        def fake_dm(message, subject, suggested_action, severity='warning'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
            })
            return True

        self._dm_patch = mock.patch.object(s, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

    def test_empty_registry_and_empty_vercel_zero_drift(self):
        counts = s.run_once(
            registry=_registry([]), state={'drifts': {}, '_meta': {}},
            vercel_projects=[], dry_run_override=True,
        )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(counts['missing_from_registry'], 0)
        self.assertEqual(counts['missing_from_vercel'], 0)
        self.assertEqual(counts['name_mismatch'], 0)
        self.assertEqual(self._dm_calls, [])

    def test_missing_from_vercel_emits_dm_in_live_mode(self):
        counts = s.run_once(
            registry=_registry([_target('ghost', 'prj_ghost')]),
            state={'drifts': {}, '_meta': {}},
            vercel_projects=[], dry_run_override=False,
        )
        self.assertEqual(counts['missing_from_vercel'], 1)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(self._dm_calls[0]['subject'],
                         'deploy-targets-sync:MISSING_FROM_VERCEL:ghost')

    def test_missing_from_registry_emits_dm_in_live_mode(self):
        counts = s.run_once(
            registry=_registry([]),
            state={'drifts': {}, '_meta': {}},
            vercel_projects=[_vercel_project('prj_orphan9', 'orphan')],
            dry_run_override=False,
        )
        self.assertEqual(counts['missing_from_registry'], 1)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(self._dm_calls[0]['subject'],
                         'deploy-targets-sync:MISSING_FROM_REGISTRY:prj_orphan9')

    def test_name_mismatch_emits_dm(self):
        counts = s.run_once(
            registry=_registry([_target('dashboard', 'prj_aaa')]),
            state={'drifts': {}, '_meta': {}},
            vercel_projects=[_vercel_project('prj_aaa', 'renamed-dash')],
            dry_run_override=False,
        )
        self.assertEqual(counts['name_mismatch'], 1)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(self._dm_calls[0]['subject'],
                         'deploy-targets-sync:NAME_MISMATCH:dashboard')

    def test_paginated_fetch_aggregates_all_pages(self):
        page1 = {
            'projects': [_vercel_project('prj_1', 'one')],
            'pagination': {'next': 'cursor-2'},
        }
        page2 = {
            'projects': [_vercel_project('prj_2', 'two')],
            'pagination': {'next': None},
        }
        responses = iter([(200, page1), (200, page2)])

        def fake_get(path, token, params=None, timeout=20):
            return next(responses)

        with mock.patch.object(s, '_vercel_get', side_effect=fake_get):
            projects = s.fetch_vercel_projects('fake-token')
        self.assertEqual([p['id'] for p in projects], ['prj_1', 'prj_2'])

    def test_dry_run_drift_fires_activation_dm_once(self):
        counts = s.run_once(
            registry=_registry([_target('ghost', 'prj_ghost')]),
            state={'drifts': {}, '_meta': {}},
            vercel_projects=[],
            dry_run_override=True,
        )
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(
            self._dm_calls[0]['subject'],
            'deploy-targets-sync: activate to receive drift alerts',
        )

    def test_dedup_suppresses_second_tick_within_window(self):
        reg = _registry([_target('ghost', 'prj_ghost')])
        state = {'drifts': {}, '_meta': {}}
        now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        s.run_once(registry=reg, state=state, vercel_projects=[],
                   dry_run_override=False, now=now)
        self._dm_calls.clear()
        counts = s.run_once(
            registry=reg, state=state, vercel_projects=[],
            dry_run_override=False, now=now + timedelta(hours=12),
        )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(counts['dm_suppressed_dedup'], 1)
        self.assertEqual(self._dm_calls, [])

    def test_drift_set_changes_only_new_items_dmed(self):
        state = {'drifts': {}, '_meta': {}}
        now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        # First tick: 'ghost' missing.
        s.run_once(
            registry=_registry([_target('ghost', 'prj_ghost'),
                                _target('other', 'prj_other')]),
            state=state,
            vercel_projects=[_vercel_project('prj_other', 'other')],
            dry_run_override=False, now=now,
        )
        self._dm_calls.clear()
        # Second tick (within 24h window): 'ghost' still missing AND
        # 'phantom' newly missing. Only 'phantom' should DM.
        counts = s.run_once(
            registry=_registry([_target('ghost', 'prj_ghost'),
                                _target('phantom', 'prj_phantom'),
                                _target('other', 'prj_other')]),
            state=state,
            vercel_projects=[_vercel_project('prj_other', 'other')],
            dry_run_override=False, now=now + timedelta(hours=12),
        )
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(counts['dm_suppressed_dedup'], 1)
        self.assertEqual(self._dm_calls[0]['subject'],
                         'deploy-targets-sync:MISSING_FROM_VERCEL:phantom')

    def test_vercel_auth_error_emits_infra_alert_and_raises(self):
        def boom(token):
            raise s.VercelAuthError('401 from Vercel on /v9/projects')

        state = {'drifts': {}, '_meta': {}}
        with self.assertRaises(s.VercelAuthError):
            s.run_once(
                registry=_registry([_target('ghost', 'prj_ghost')]),
                state=state,
                vercel_token='fake-token',
                fetch_fn=boom,
                dry_run_override=False,
            )
        # One critical infra DM.
        self.assertEqual(len(self._dm_calls), 1)
        self.assertEqual(self._dm_calls[0]['severity'], 'critical')
        self.assertEqual(
            self._dm_calls[0]['subject'],
            'deploy-targets-sync:INFRASTRUCTURE_ALERT:vercel-auth',
        )

    def test_reconciled_drift_garbage_collected(self):
        reg = _registry([_target('ghost', 'prj_ghost'),
                         _target('other', 'prj_other')])
        state = {'drifts': {}, '_meta': {}}
        s.run_once(
            registry=reg, state=state,
            vercel_projects=[_vercel_project('prj_other', 'other')],
            dry_run_override=False,
        )
        self.assertIn('ghost:MISSING_FROM_VERCEL', state['drifts'])
        # Operator restores the Vercel project — drift goes away.
        counts = s.run_once(
            registry=reg, state=state,
            vercel_projects=[
                _vercel_project('prj_other', 'other'),
                _vercel_project('prj_ghost', 'ghost'),
            ],
            dry_run_override=False,
        )
        self.assertNotIn('ghost:MISSING_FROM_VERCEL', state['drifts'])
        self.assertEqual(counts['reconciled_gc'], 1)


class KillSwitchTest(unittest.TestCase):
    def test_kill_switch_exits_clean(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(s, 'KILL_SWITCH', Path(td) / 'on'):
                (Path(td) / 'on').write_text('1')
                counts = s.run_once(
                    registry=_registry(),
                    state={'drifts': {}, '_meta': {}},
                    vercel_projects=[],
                )
                self.assertEqual(counts['dm_sent'], 0)


class CliFlagsTest(unittest.TestCase):
    def test_dry_run_flag_forces_dry_run(self):
        parsed = s._parse_args(['sync_deploy_targets.py', '--dry-run', '--once'])
        self.assertTrue(parsed.dry_run)
        self.assertTrue(parsed.once)

    def test_once_alone_keeps_dry_run_off(self):
        parsed = s._parse_args(['sync_deploy_targets.py', '--once'])
        self.assertFalse(parsed.dry_run)
        self.assertTrue(parsed.once)


class RenderingTest(unittest.TestCase):
    def test_missing_from_vercel_message_mentions_404(self):
        msg, subj, sug = s._render_missing_from_vercel(
            'ghost', {'vercel_project_id': 'prj_ghost', 'github_repo': 'foo/bar'},
        )
        self.assertIn('404', msg)
        self.assertIn('ghost', msg)
        self.assertIn('foo/bar', msg)

    def test_missing_from_registry_message_mentions_unregistered(self):
        msg, _, sug = s._render_missing_from_registry(
            'prj_orphan', {'vercel_project_id': 'prj_orphan', 'vercel_name': 'orphan'},
        )
        self.assertIn('orphan', msg)
        self.assertIn('deploy_targets', sug)

    def test_name_mismatch_message_includes_both_names(self):
        msg, _, _ = s._render_name_mismatch(
            'dash', {'vercel_project_id': 'prj_x',
                     'registry_name': 'dash', 'vercel_name': 'dash-new'},
        )
        self.assertIn('dash', msg)
        self.assertIn('dash-new', msg)


if __name__ == '__main__':
    unittest.main()
