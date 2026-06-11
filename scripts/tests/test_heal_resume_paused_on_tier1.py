#!/usr/bin/env python3
"""Tests for heal_resume_paused_on_tier1 (rate-limit-resilience step B).

Covers the pure-logic surface (envelope rewriting, marker scanning,
cooldown gating, archive lookup) and the orchestration loop with
safe_write_inbox + larry_alerts mocked. No real subprocess work; no
prod state mutation.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_resume_paused_on_tier1
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_resume_paused_on_tier1 as h  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT so paths derive into a tmp dir.

    h's module-level constants (KILL_SWITCH, IN_FLIGHT_DIR, etc) bind to
    AGENTS_ROOT at import time, so we reload after swapping the env var.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='heal-resume-')
        for sub in ('logs', 'state', 'blackboard', 'inboxes',
                    Path('state', 'in-flight'), Path('inboxes', 'forge', '.archive')):
            os.makedirs(os.path.join(self._tmp, str(sub)), exist_ok=True)
        self._orig_env = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp
        # Default disable the activation gate; per-test can override.
        self._orig_autoresume = os.environ.get(h.ENV_AUTORESUME_ENABLED)
        os.environ.pop(h.ENV_AUTORESUME_ENABLED, None)
        importlib.reload(h)

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._orig_env
        if self._orig_autoresume is None:
            os.environ.pop(h.ENV_AUTORESUME_ENABLED, None)
        else:
            os.environ[h.ENV_AUTORESUME_ENABLED] = self._orig_autoresume
        importlib.reload(h)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    # -- fixtures ---------------------------------------------------------

    def _write_marker(self, task_stem: str, *,
                      failure_type: str = 'rate_limit',
                      agent_id: str | None = 'forge',
                      tier: str | None = 'tier1',
                      extra: dict | None = None) -> Path:
        path = h.IN_FLIGHT_DIR / f'{task_stem}.json'
        marker = {'failure_type': failure_type,
                  'at': datetime.now(timezone.utc).isoformat()}
        if agent_id is not None:
            marker['agent_id'] = agent_id
        if tier is not None:
            marker['tier'] = tier
        data: dict = {'paused_on_tier1': marker}
        if extra:
            data.update(extra)
        path.write_text(json.dumps(data, indent=2))
        return path

    def _write_archive(self, task_stem: str, agent: str = 'forge',
                       envelope: dict | None = None) -> Path:
        archive_dir = h.INBOXES_ROOT / agent / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / f'{task_stem}.json'
        env = envelope or {
            'task_id': task_stem,
            'prompt': ('Test prompt — ' + 'x' * 200),
            'source': 'beacon',
            'target_repo': 'ourliberty-agent-core',
            'phase': 'build',
            'session_id': 'cafef00d-cafe-f00d-cafe-f00dcafef00d',
            'resume_session_id': 'cafef00d-cafe-f00d-cafe-f00dcafef00d',
        }
        path.write_text(json.dumps(env, indent=2))
        return path


# -------------------- kill switch + activation gate --------------------


class KillSwitchTest(_IsolatedAgentsRoot):
    def test_main_exits_early_when_kill_switch_present(self):
        h.KILL_SWITCH.write_text('off')
        self._write_marker('paused-task-001')
        with mock.patch.object(h, 'redispatch') as m_redispatch:
            rc = h.main()
        self.assertEqual(rc, 0)
        m_redispatch.assert_not_called()
        # In-flight marker untouched.
        self.assertTrue((h.IN_FLIGHT_DIR / 'paused-task-001.json').exists())


class AutoresumeEnabledTest(_IsolatedAgentsRoot):
    def test_default_off(self):
        self.assertFalse(h.autoresume_enabled())

    def test_explicit_true(self):
        os.environ[h.ENV_AUTORESUME_ENABLED] = 'true'
        self.assertTrue(h.autoresume_enabled())
        os.environ[h.ENV_AUTORESUME_ENABLED] = 'TRUE'
        self.assertTrue(h.autoresume_enabled())

    def test_other_values_false(self):
        for v in ('false', '1', 'yes', ''):
            os.environ[h.ENV_AUTORESUME_ENABLED] = v
            self.assertFalse(h.autoresume_enabled(),
                             f'value={v!r} should disable')


# -------------------- pure envelope rewriting --------------------


class BuildResumedEnvelopeTest(unittest.TestCase):
    """Pure function — no fixture rig needed."""

    def test_strips_resume_keys(self):
        env = {
            'task_id': 'original-task',
            'prompt': 'x' * 150,
            'source': 'beacon',
            'session_id': 'abc',
            'resume_session_id': 'abc',
        }
        new, new_id = h.build_resumed_envelope(env)
        self.assertNotIn('session_id', new)
        self.assertNotIn('resume_session_id', new)
        # Original dict untouched.
        self.assertIn('session_id', env)
        self.assertNotEqual(new_id, 'original-task')

    def test_task_id_suffix_matches_format(self):
        env = {'task_id': 'foo', 'prompt': 'x' * 150, 'source': 'beacon'}
        now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        new, new_id = h.build_resumed_envelope(env, now=now)
        self.assertEqual(new_id, 'foo-resume-20260102T030405Z')
        self.assertEqual(new['task_id'], new_id)

    def test_source_overridden_to_auto_retry(self):
        env = {'task_id': 'foo', 'prompt': 'x' * 150, 'source': 'beacon'}
        new, _ = h.build_resumed_envelope(env)
        self.assertEqual(new['source'], 'auto-retry')

    def test_phase_resets_to_preflight(self):
        env = {'task_id': 'foo', 'prompt': 'x' * 150, 'source': 'beacon',
               'phase': 'build'}
        new, _ = h.build_resumed_envelope(env)
        self.assertEqual(new['phase'], 'preflight')

    def test_resumed_from_recorded(self):
        env = {'task_id': 'original-id', 'prompt': 'x' * 150,
               'source': 'beacon'}
        new, _ = h.build_resumed_envelope(env)
        self.assertEqual(new['_resumed_from'], 'original-id')

    def test_drops_transient_notify_fields(self):
        env = {
            'task_id': 'foo', 'prompt': 'x' * 150, 'source': 'beacon',
            'dispatched_by': 'outbox-notifier',
            '_notify_depth': 2,
            'marker_error_count': 1,
        }
        new, _ = h.build_resumed_envelope(env)
        self.assertNotIn('dispatched_by', new)
        self.assertNotIn('_notify_depth', new)
        self.assertNotIn('marker_error_count', new)


# -------------------- scanner --------------------


class ScanPausedMarkersTest(_IsolatedAgentsRoot):
    def test_empty_dir_returns_empty(self):
        self.assertEqual(h.scan_paused_markers(), [])

    def test_returns_only_files_with_marker(self):
        self._write_marker('paused-001')
        # A bare in-flight registration (no paused marker) must be skipped.
        bare = h.IN_FLIGHT_DIR / 'fresh-task.json'
        bare.write_text(json.dumps({
            'task_stem': 'fresh-task', 'agent_id': 'forge', 'pid': 9999,
        }))
        found = h.scan_paused_markers()
        self.assertEqual(len(found), 1)
        _, stem, _ = found[0]
        self.assertEqual(stem, 'paused-001')

    def test_malformed_json_skipped(self):
        bad = h.IN_FLIGHT_DIR / 'bad.json'
        bad.write_text('{not-json')
        self._write_marker('ok')
        found = h.scan_paused_markers()
        stems = [s for (_, s, _) in found]
        self.assertEqual(stems, ['ok'])


# -------------------- archive lookup --------------------


class FindArchivedEnvelopeTest(_IsolatedAgentsRoot):
    def test_with_hint_short_circuits(self):
        self._write_archive('hinted-task', agent='forge')
        result = h.find_archived_envelope('hinted-task', 'forge')
        self.assertIsNotNone(result)
        agent, _, env = result
        self.assertEqual(agent, 'forge')
        self.assertEqual(env['task_id'], 'hinted-task')

    def test_without_hint_scans_all(self):
        # Place envelope under beacon's inbox even though no hint.
        os.makedirs(h.INBOXES_ROOT / 'beacon' / '.archive', exist_ok=True)
        self._write_archive('legacy-task', agent='beacon')
        result = h.find_archived_envelope('legacy-task', None)
        self.assertIsNotNone(result)
        agent, _, _ = result
        self.assertEqual(agent, 'beacon')

    def test_missing_returns_none(self):
        self.assertIsNone(h.find_archived_envelope('nope', 'forge'))


# -------------------- cooldown gate --------------------


class CooldownClearedTest(_IsolatedAgentsRoot):
    def test_no_cooldown_returns_cleared(self):
        with mock.patch('active_tier.cooldown_until', return_value=None):
            cleared, until = h.cooldown_cleared('tier1')
        self.assertTrue(cleared)
        self.assertIsNone(until)

    def test_active_cooldown_returns_not_cleared(self):
        with mock.patch('active_tier.cooldown_until',
                        return_value='2026-12-31T00:00:00+00:00'):
            cleared, until = h.cooldown_cleared('tier1')
        self.assertFalse(cleared)
        self.assertEqual(until, '2026-12-31T00:00:00+00:00')

    def test_exception_defensive_not_cleared(self):
        with mock.patch('active_tier.cooldown_until',
                        side_effect=RuntimeError('boom')):
            cleared, until = h.cooldown_cleared('tier1')
        self.assertFalse(cleared)
        self.assertIsNone(until)


# -------------------- end-to-end main() --------------------


class MainEndToEndTest(_IsolatedAgentsRoot):
    """Full main() with safe_write_inbox + larry_alerts mocked."""

    def setUp(self):
        super().setUp()
        os.environ[h.ENV_AUTORESUME_ENABLED] = 'true'

    def _patch_swi(self, *, fail: bool = False):
        captured: dict = {}

        def fake(target_agent, task_dict, source_agent, filename):
            captured['target_agent'] = target_agent
            captured['task_dict'] = task_dict
            captured['source_agent'] = source_agent
            captured['filename'] = filename
            if fail:
                # Use the real DispatchRejected so the except branch fires.
                import safe_write_inbox as swi
                raise swi.DispatchRejected('test injection')
            return h.INBOXES_ROOT / target_agent / filename

        # Patch the attribute on the imported module — h.redispatch resolves
        # safe_write_inbox lazily, so we patch the actual swi module.
        import safe_write_inbox as swi
        patcher = mock.patch.object(swi, 'safe_write_inbox', side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured

    def test_redispatches_when_cleared_and_enabled(self):
        self._write_marker('paused-task-001', tier='tier1')
        self._write_archive('paused-task-001', agent='forge')
        captured = self._patch_swi()
        with mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(captured['target_agent'], 'forge')
        self.assertEqual(captured['source_agent'], 'auto-retry')
        new_env = captured['task_dict']
        self.assertNotIn('session_id', new_env)
        self.assertNotIn('resume_session_id', new_env)
        self.assertTrue(new_env['task_id'].startswith('paused-task-001-resume-'))
        self.assertEqual(new_env['source'], 'auto-retry')
        # In-flight marker cleared after successful dispatch.
        self.assertFalse((h.IN_FLIGHT_DIR / 'paused-task-001.json').exists())

    def test_skipped_when_cooldown_active(self):
        self._write_marker('paused-task-002')
        self._write_archive('paused-task-002')
        captured = self._patch_swi()
        with mock.patch('active_tier.cooldown_until',
                        return_value='2030-01-01T00:00:00+00:00'):
            rc = h.main()
        self.assertEqual(rc, 0)
        # No dispatch happened, marker preserved.
        self.assertEqual(captured, {})
        self.assertTrue((h.IN_FLIGHT_DIR / 'paused-task-002.json').exists())

    def test_skipped_when_archive_missing(self):
        self._write_marker('paused-task-orphan')
        # No archive file.
        captured = self._patch_swi()
        with mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(captured, {})
        # Marker preserved so the operator can investigate.
        self.assertTrue((h.IN_FLIGHT_DIR / 'paused-task-orphan.json').exists())

    def test_dry_run_does_not_dispatch(self):
        os.environ[h.ENV_AUTORESUME_ENABLED] = 'false'
        self._write_marker('paused-task-dry')
        self._write_archive('paused-task-dry')
        captured = self._patch_swi()
        with mock.patch('active_tier.cooldown_until', return_value=None), \
                mock.patch.object(h, 'dm_larry', return_value=True) as m_dm:
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(captured, {})
        # Marker NOT cleared in dry-run.
        self.assertTrue((h.IN_FLIGHT_DIR / 'paused-task-dry.json').exists())
        # One-time activation DM fired.
        self.assertTrue(m_dm.called)

    def test_per_run_cap(self):
        # 6 markers, cap is 5.
        for i in range(6):
            self._write_marker(f'paused-task-{i:03d}')
            self._write_archive(f'paused-task-{i:03d}')
        captured_calls: list = []

        def fake(target_agent, task_dict, source_agent, filename):
            captured_calls.append(filename)
            return h.INBOXES_ROOT / target_agent / filename

        import safe_write_inbox as swi
        with mock.patch.object(swi, 'safe_write_inbox', side_effect=fake), \
                mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured_calls), h.MAX_RESUMES_PER_RUN)
        # The leftover marker should still be on disk for the next tick.
        remaining = sorted(
            p.stem for p in h.IN_FLIGHT_DIR.glob('*.json')
        )
        self.assertEqual(len(remaining), 1)

    def test_per_task_attempt_budget(self):
        self._write_marker('paused-budget-test')
        self._write_archive('paused-budget-test')

        # Pre-seed state at the cap.
        h.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        h.STATE_FILE.write_text(json.dumps({'tasks': {
            'paused-budget-test': {
                'attempts': h.MAX_RESUME_ATTEMPTS,
                'exhausted_alerted': False,
                'activation_alerted': False,
                'last_attempt_iso': None,
            }
        }}))

        captured = self._patch_swi()
        with mock.patch('active_tier.cooldown_until', return_value=None), \
                mock.patch.object(h, 'dm_larry', return_value=True) as m_dm:
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(captured, {})  # no dispatch
        self.assertTrue(m_dm.called)
        # Marker still present, exhausted_alerted now True.
        new_state = json.loads(h.STATE_FILE.read_text())
        self.assertTrue(
            new_state['tasks']['paused-budget-test']['exhausted_alerted']
        )

    def test_failed_dispatch_increments_attempt_but_keeps_marker(self):
        self._write_marker('paused-fail-test')
        self._write_archive('paused-fail-test')
        self._patch_swi(fail=True)
        with mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        # Marker preserved so the next tick retries (until budget hits).
        self.assertTrue((h.IN_FLIGHT_DIR / 'paused-fail-test.json').exists())
        new_state = json.loads(h.STATE_FILE.read_text())
        self.assertEqual(
            new_state['tasks']['paused-fail-test']['attempts'], 1,
        )

    def test_legacy_marker_without_agent_id_scans_all_inboxes(self):
        self._write_marker('legacy-task', agent_id=None, tier=None)
        # Archive lives under beacon, not forge.
        os.makedirs(h.INBOXES_ROOT / 'beacon' / '.archive', exist_ok=True)
        self._write_archive('legacy-task', agent='beacon')
        captured = self._patch_swi()
        with mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get('target_agent'), 'beacon')


# -------------------- regression: agent_runner marker shape --------------


class AgentRunnerMarkerShapeTest(unittest.TestCase):
    """_mark_paused_on_tier1 must record agent_id + tier when supplied so
    this healer can resolve targets deterministically."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ar-marker-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        import agent_runner as ar
        self.ar = ar
        self._orig = ar.IN_FLIGHT_DIR
        ar.IN_FLIGHT_DIR = Path(self.tmp)

    def tearDown(self):
        self.ar.IN_FLIGHT_DIR = self._orig

    def test_records_agent_id_and_tier_when_supplied(self):
        self.ar._mark_paused_on_tier1(
            'task-shape', 'rate_limit',
            agent_id='forge', tier='tier2',
        )
        data = json.loads((Path(self.tmp) / 'task-shape.json').read_text())
        self.assertEqual(data['paused_on_tier1']['agent_id'], 'forge')
        self.assertEqual(data['paused_on_tier1']['tier'], 'tier2')
        self.assertEqual(data['paused_on_tier1']['failure_type'], 'rate_limit')

    def test_omits_agent_id_when_not_supplied(self):
        # Backwards-compat: older callers / tests pass only 2 args.
        self.ar._mark_paused_on_tier1('task-legacy', 'auth_401')
        data = json.loads((Path(self.tmp) / 'task-legacy.json').read_text())
        self.assertNotIn('agent_id', data['paused_on_tier1'])
        self.assertNotIn('tier', data['paused_on_tier1'])


class OutOfBandResolutionTest(_IsolatedAgentsRoot):
    """The 2026-06-04 false-positive fix: a paused task already resolved
    out-of-band (Larry re-dispatched, or a fresh session superseded it) must
    NOT be re-dispatched — clear the stale marker silently instead."""

    def setUp(self):
        super().setUp()
        os.environ[h.ENV_AUTORESUME_ENABLED] = 'true'

    def _patch_swi_capture(self):
        captured: dict = {}

        def fake(target_agent, task_dict, source_agent, filename):
            captured['called'] = True
            return h.INBOXES_ROOT / target_agent / filename

        import safe_write_inbox as swi
        p = mock.patch.object(swi, 'safe_write_inbox', side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return captured

    def test_resolved_clears_marker_without_redispatch(self):
        self._write_marker('paused-resolved-001', tier='tier1')
        self._write_archive('paused-resolved-001', agent='forge')
        captured = self._patch_swi_capture()
        with mock.patch.object(h, 'resolved_out_of_band',
                               return_value=(True, 'larry_action')), \
             mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        # No re-dispatch, and the stale marker was cleared.
        self.assertNotIn('called', captured)
        self.assertFalse(
            (h.IN_FLIGHT_DIR / 'paused-resolved-001.json').exists())

    def test_not_resolved_proceeds_to_redispatch(self):
        self._write_marker('paused-live-001', tier='tier1')
        self._write_archive('paused-live-001', agent='forge')
        captured = self._patch_swi_capture()
        with mock.patch.object(h, 'resolved_out_of_band',
                               return_value=(False, None)), \
             mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertTrue(captured.get('called'))

    def test_resolution_check_failsafe_proceeds(self):
        # The wrapper returns (False, None) on any infra error, so a real
        # paused task still re-dispatches — verified via the live path.
        self._write_marker('paused-failsafe-001', tier='tier1')
        self._write_archive('paused-failsafe-001', agent='forge')
        captured = self._patch_swi_capture()
        with mock.patch('task_resolution.resolved_out_of_band',
                        side_effect=RuntimeError('supabase down')), \
             mock.patch('active_tier.cooldown_until', return_value=None):
            rc = h.main()
        self.assertEqual(rc, 0)
        # Error swallowed -> treated as not-resolved -> re-dispatch proceeds.
        self.assertTrue(captured.get('called'))


if __name__ == '__main__':
    unittest.main()
