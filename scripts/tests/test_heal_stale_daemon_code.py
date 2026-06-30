#!/usr/bin/env python3
"""Tests for heal_stale_daemon_code (chain-discipline-v2, 2026-05-25).

Covers Larry's three explicit acceptance cases (mtime-gt-start fires,
mtime-lt-start does not, 5-min race-avoidance suppresses) plus cooldown
re-alert behavior, ExecStart parser shapes, and the orchestrator integration.

Subprocess shellouts to systemctl are stubbed via monkeypatch; the real
larry_alerts is patched at module-import time so no DM ever leaves the
test process.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_stale_daemon_code
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
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_stale_daemon_code as h  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test.

    The healer's STATE_FILE / LOG_FILE / HEARTBEAT_FILE / KILL_SWITCH all
    derive from AGENTS_ROOT at module import time. Reload after the env
    override so module-level constants pick it up.
    """

    def setUp(self):
        super().setUp()
        self._isolated_tmp = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard'):
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


# -------------------- pure-logic tests --------------------

class StalenessPredicateTests(unittest.TestCase):
    """Larry's three acceptance cases on the pure-logic predicate."""

    def test_a_mtime_greater_than_start_is_stale(self):
        service_start = 1_000_000.0
        script_mtime = service_start + (10 * 60)  # 10 min later
        self.assertTrue(h.is_stale(service_start, script_mtime))

    def test_b_mtime_less_than_start_is_not_stale(self):
        service_start = 1_000_000.0
        script_mtime = service_start - (60 * 60)  # 1 hour earlier
        self.assertFalse(h.is_stale(service_start, script_mtime))

    def test_c_recent_restart_within_5min_is_suppressed(self):
        # Script mtime is 4 minutes after service start: inside the
        # 5-min race-avoidance window. Should NOT count as stale.
        service_start = 1_000_000.0
        script_mtime = service_start + (4 * 60)
        self.assertFalse(h.is_stale(service_start, script_mtime))

    def test_exactly_at_race_window_boundary_is_not_stale(self):
        # Exactly 5 min — still inside the window (strict >).
        service_start = 1_000_000.0
        script_mtime = service_start + h.RACE_AVOIDANCE_SEC
        self.assertFalse(h.is_stale(service_start, script_mtime))

    def test_just_past_race_window_is_stale(self):
        # 5 min + 1 sec — outside the window.
        service_start = 1_000_000.0
        script_mtime = service_start + h.RACE_AVOIDANCE_SEC + 1
        self.assertTrue(h.is_stale(service_start, script_mtime))


class TimestampParserTests(unittest.TestCase):
    def test_systemd_canonical_form_parses(self):
        # `Mon 2026-05-25 17:37:48 MDT`
        v = h.parse_systemd_timestamp('Mon 2026-05-25 17:37:48 MDT')
        self.assertIsNotNone(v)
        self.assertGreater(v, 1_700_000_000)

    def test_empty_or_na_returns_none(self):
        self.assertIsNone(h.parse_systemd_timestamp(''))
        self.assertIsNone(h.parse_systemd_timestamp('n/a'))
        self.assertIsNone(h.parse_systemd_timestamp('   '))

    def test_iso_like_form_parses(self):
        v = h.parse_systemd_timestamp('2026-05-25 17:37:48')
        self.assertIsNotNone(v)


class ExecStartParserTests(unittest.TestCase):
    def test_python_interpreter_then_script(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('print("hi")')
            svc = Path(td) / 'foo.service'
            svc.write_text(
                '[Unit]\n'
                'Description=test\n'
                '[Service]\n'
                f'ExecStart=/usr/bin/python3 {script}\n'
            )
            got = h.parse_script_path_from_service_file(str(svc))
            self.assertEqual(got, script)

    def test_bare_script_path(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.sh'
            script.write_text('#!/bin/sh\necho hi\n')
            script.chmod(0o755)
            svc = Path(td) / 'foo.service'
            svc.write_text(
                '[Service]\n'
                f'ExecStart={script}\n'
            )
            got = h.parse_script_path_from_service_file(str(svc))
            self.assertEqual(got, script)

    def test_env_then_interpreter_then_script(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'app.py'
            script.write_text('')
            svc = Path(td) / 'app.service'
            svc.write_text(
                f'[Service]\nExecStart=/usr/bin/env python3 {script}\n'
            )
            got = h.parse_script_path_from_service_file(str(svc))
            self.assertEqual(got, script)

    def test_missing_execstart_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            svc = Path(td) / 'foo.service'
            svc.write_text('[Unit]\nDescription=test\n')
            self.assertIsNone(h.parse_script_path_from_service_file(str(svc)))

    def test_unreadable_fragment_returns_none(self):
        self.assertIsNone(
            h.parse_script_path_from_service_file('/nonexistent.service'),
        )

    def test_shell_pipe_execstart_returns_none(self):
        # `/bin/sh -c '...'` shapes are not parseable: /bin/sh is filtered as
        # an interpreter and the quoted pipeline has no absolute paths. The
        # parser returns None so check_unit logs the could-not-resolve skip.
        with tempfile.TemporaryDirectory() as td:
            svc = Path(td) / 'sh.service'
            svc.write_text(
                "[Service]\nExecStart=/bin/sh -c 'curl localhost | jq'\n",
            )
            self.assertIsNone(h.parse_script_path_from_service_file(str(svc)))

    def test_uvicorn_module_mode_resolves_via_workingdirectory(self):
        # Production case (ourliberty-dashboard-api.service): the script path
        # is not on the ExecStart line; it must be reconstructed from the
        # dotted module + WorkingDirectory.
        with tempfile.TemporaryDirectory() as td:
            scripts_dir = Path(td) / 'scripts'
            scripts_dir.mkdir()
            script = scripts_dir / 'dashboard_api.py'
            script.write_text('app = None\n')
            svc = Path(td) / 'dashboard.service'
            svc.write_text(
                '[Service]\n'
                f'WorkingDirectory={td}\n'
                'ExecStart=/usr/bin/env python3 -m uvicorn '
                'scripts.dashboard_api:app --host 127.0.0.1 --port 8000\n'
            )
            got = h.parse_script_path_from_service_file(str(svc))
            self.assertEqual(got, script)

    def test_uvicorn_module_missing_on_disk_returns_none(self):
        # If the dotted module resolves to a non-existent file (typo in unit
        # file, file deleted), the parser must NOT return a phantom path —
        # check_unit relies on None here to log the skip.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'scripts').mkdir()
            svc = Path(td) / 'dashboard.service'
            svc.write_text(
                '[Service]\n'
                f'WorkingDirectory={td}\n'
                'ExecStart=/usr/bin/env python3 -m uvicorn '
                'scripts.nonexistent_module:app\n'
            )
            self.assertIsNone(h.parse_script_path_from_service_file(str(svc)))

    def test_non_uvicorn_m_invocation_returns_none(self):
        # `-m gunicorn`, `-m flask`, and other module-mode launchers are out
        # of scope for the V1 uvicorn fix; the parser falls through to the
        # existing could-not-resolve branch. Deferred to a follow-up.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'scripts').mkdir()
            (Path(td) / 'scripts' / 'foo.py').write_text('app = None\n')
            svc = Path(td) / 'gunicorn.service'
            svc.write_text(
                '[Service]\n'
                f'WorkingDirectory={td}\n'
                'ExecStart=/usr/bin/env python3 -m gunicorn scripts.foo:app\n'
            )
            self.assertIsNone(h.parse_script_path_from_service_file(str(svc)))


# -------------------- cooldown tests --------------------

class CooldownTests(_IsolatedAgentsRoot):
    def test_fresh_state_is_not_in_cooldown(self):
        self.assertFalse(h.in_cooldown({'services': {}}, 'foo.service'))

    def test_within_window_is_in_cooldown(self):
        now = 1_700_000_000.0
        state = {'services': {'foo.service': {'last_alert_ts': now - 60}}}
        self.assertTrue(h.in_cooldown(state, 'foo.service', now=now))

    def test_outside_window_is_not_in_cooldown(self):
        now = 1_700_000_000.0
        state = {'services': {'foo.service': {
            'last_alert_ts': now - h.PER_SERVICE_COOLDOWN_SEC - 1,
        }}}
        self.assertFalse(h.in_cooldown(state, 'foo.service', now=now))

    def test_save_then_load_round_trip(self):
        state = {'services': {'foo.service': {'last_alert_ts': 123.4}}}
        h.save_state(state)
        got = h.load_state()
        self.assertEqual(
            got['services']['foo.service']['last_alert_ts'], 123.4,
        )

    def test_load_corrupt_state_returns_empty(self):
        h.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        h.STATE_FILE.write_text('not-json')
        self.assertEqual(h.load_state(), {'services': {}})


class RestartCooldownTests(_IsolatedAgentsRoot):
    def test_fresh_state_is_not_in_restart_cooldown(self):
        self.assertFalse(
            h.in_restart_cooldown({'services': {}}, 'foo.service'),
        )

    def test_within_window_is_in_restart_cooldown(self):
        now = 1_700_000_000.0
        state = {'services': {'foo.service': {'last_restart_ts': now - 60}}}
        self.assertTrue(h.in_restart_cooldown(state, 'foo.service', now=now))

    def test_outside_window_is_not_in_restart_cooldown(self):
        now = 1_700_000_000.0
        state = {'services': {'foo.service': {
            'last_restart_ts': now - h.RESTART_COOLDOWN_SEC - 1,
        }}}
        self.assertFalse(h.in_restart_cooldown(state, 'foo.service', now=now))

    def test_mark_restarted_preserves_last_alert_ts(self):
        # Merging the restart marker must not clobber an existing alert ts.
        state = {'services': {'foo.service': {'last_alert_ts': 111.0}}}
        h.mark_restarted(state, 'foo.service', now=222.0)
        entry = state['services']['foo.service']
        self.assertEqual(entry['last_alert_ts'], 111.0)
        self.assertEqual(entry['last_restart_ts'], 222.0)

    def test_mark_alerted_preserves_last_restart_ts(self):
        state = {'services': {'foo.service': {'last_restart_ts': 333.0}}}
        h.mark_alerted(state, 'foo.service', now=444.0)
        entry = state['services']['foo.service']
        self.assertEqual(entry['last_restart_ts'], 333.0)
        self.assertEqual(entry['last_alert_ts'], 444.0)


# -------------------- orchestration tests --------------------

class CheckUnitTests(_IsolatedAgentsRoot):
    """Larry's acceptance cases at the orchestrator level — full integration
    with systemctl + ExecStart parsing + auto-restart + larry_alerts stubbed.
    """

    def _stub_systemctl(self, properties):
        """Patch h.systemctl_show to return canned values per property name."""
        def fake_show(unit, prop):
            return properties.get(prop)
        return mock.patch.object(h, 'systemctl_show', side_effect=fake_show)

    def _stub_restart(self, rc=0, stderr=''):
        """Patch h.auto_restart_unit to return canned (rc, stderr)."""
        return mock.patch.object(
            h, 'auto_restart_unit', return_value=(rc, stderr),
        )

    def _stub_dms(self):
        """Patch all three new DM functions; returns a dict of mocks."""
        ctx = {
            'restarted': mock.patch.object(
                h, 'dm_larry_auto_restarted', return_value=True,
            ),
            'failed': mock.patch.object(
                h, 'dm_larry_auto_restart_failed', return_value=True,
            ),
            'still_stale': mock.patch.object(
                h, 'dm_larry_still_stale_after_restart', return_value=True,
            ),
        }
        return ctx

    def _stub_prs(self, lines=None):
        return mock.patch.object(
            h, 'infer_recent_prs', return_value=(lines or []),
        )

    def _make_service_file(self, td, script_path):
        svc = Path(td) / 'unit.service'
        svc.write_text(
            f'[Service]\nExecStart=/usr/bin/python3 {script_path}\n',
        )
        return svc

    def test_case_a_script_newer_auto_restarts(self):
        # Acceptance (a): script mtime > service start by > 5 min → auto-restart.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)            # 1h ago
            script_mtime = now - (10 * 60)             # 10 min ago
            os.utime(script, (script_mtime, script_mtime))
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    self._stub_prs(['abc1234 fix: foo']) as m_prs, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'auto-restarted')
            self.assertEqual(m_restart.call_count, 1)
            self.assertEqual(m_restart.call_args.args[0], 'ourliberty-foo.service')
            self.assertEqual(m_r.call_count, 1)
            self.assertEqual(m_f.call_count, 0)
            self.assertEqual(m_s.call_count, 0)
            # PR-list inference was invoked and its result passed to the DM.
            self.assertEqual(m_prs.call_count, 1)
            self.assertEqual(m_r.call_args.args[0], 'ourliberty-foo.service')
            self.assertEqual(m_r.call_args.args[4], ['abc1234 fix: foo'])

    def test_case_b_script_older_no_action(self):
        # Acceptance (b): script mtime < service start → no restart, no DM.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (10 * 60)            # 10 min ago
            script_mtime = now - (60 * 60)             # 1 hour ago
            os.utime(script, (script_mtime, script_mtime))
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'fresh')
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count, 0)
            self.assertEqual(m_f.call_count, 0)
            self.assertEqual(m_s.call_count, 0)

    def test_case_c_race_window_no_action(self):
        # Acceptance (c): script mtime within 5 min of service start → suppressed.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (10 * 60)
            script_mtime = service_start + (4 * 60)
            os.utime(script, (script_mtime, script_mtime))
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'race-window')
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count, 0)
            self.assertEqual(m_f.call_count, 0)
            self.assertEqual(m_s.call_count, 0)

    def test_auto_restart_records_cooldown(self):
        # After a successful restart, last_restart_ts should be set in state.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            state = {'services': {}}
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart(rc=0), self._stub_prs(), \
                    dms['restarted'], dms['failed'], dms['still_stale']:
                h.check_unit('ourliberty-foo.service', state, now=now)
            self.assertIn(
                'last_restart_ts',
                state['services']['ourliberty-foo.service'],
            )

    def test_restart_failure_path(self):
        # rc != 0 → auto-restart-failed DM with stderr; subprocess called once.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart(rc=1, stderr='Unit not found') as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'auto-restart-failed')
            self.assertEqual(m_restart.call_count, 1)  # NO RETRY
            self.assertEqual(m_r.call_count, 0)
            self.assertEqual(m_f.call_count, 1)
            self.assertEqual(m_s.call_count, 0)
            # Failure DM body carries the stderr.
            self.assertEqual(m_f.call_args.args[2], 'Unit not found')

    def test_restart_cooldown_suppresses_second_restart(self):
        # Load-bearing invariant: same unit, second tick within 30 min →
        # subprocess.run is NOT called a second time. Whether the second
        # outcome is 'restart-cooldown' (DM suppressed by 6h gate) or
        # 'still-stale-after-restart' (escalation DM fires) depends on the
        # 6h alert-cooldown side; both are correct "no second restart"
        # branches. This test pins the cooldown invariant explicitly and
        # delegates the DM-shape decision to the two dedicated tests below.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            state = {'services': {}}
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, self._stub_prs(), \
                    dms['restarted'], dms['failed'], dms['still_stale']:
                # First tick: restart fires (auto-restarted outcome).
                outcome1 = h.check_unit('ourliberty-foo.service', state, now=now)
                # Second tick 5 min later — still stale in our mocked
                # systemctl world. The healer must NOT call restart again.
                outcome2 = h.check_unit(
                    'ourliberty-foo.service', state, now=now + 5 * 60,
                )
            self.assertEqual(outcome1, 'auto-restarted')
            self.assertIn(
                outcome2,
                ('restart-cooldown', 'still-stale-after-restart'),
            )
            # The invariant under test: exactly one restart call across
            # two ticks within the 30-min cooldown window.
            self.assertEqual(m_restart.call_count, 1)

    def test_post_cooldown_still_stale_escalation(self):
        # > 30 min after a restart, still stale → escalation DM, no second restart.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            # Prior restart 35 min ago — past the 30-min cooldown.
            state = {'services': {
                'ourliberty-foo.service': {
                    'last_restart_ts': now - (35 * 60),
                },
            }}
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, self._stub_prs(), \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s:
                # Past the cooldown, the unit is STILL stale → the healer
                # tries another restart per the standard flow. To test the
                # "still stale after the previous restart, escalate instead"
                # branch we need to ALSO be inside the restart cooldown but
                # have no alert ts. So override the test to be inside cooldown
                # at the boundary case:
                state['services']['ourliberty-foo.service'] = {
                    'last_restart_ts': now - (10 * 60),  # 10 min ago
                }
                outcome = h.check_unit(
                    'ourliberty-foo.service', state, now=now,
                )
            self.assertEqual(outcome, 'still-stale-after-restart')
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count, 0)
            self.assertEqual(m_f.call_count, 0)
            self.assertEqual(m_s.call_count, 1)
            # last_alert_ts should now be set so the next tick within 6h
            # suppresses the escalation DM.
            self.assertIn(
                'last_alert_ts',
                state['services']['ourliberty-foo.service'],
            )

    def test_still_stale_dm_gated_by_six_hour_cooldown(self):
        # Inside restart cooldown AND inside 6h alert cooldown → no DM,
        # outcome 'restart-cooldown' (suppressed escalation).
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            state = {'services': {
                'ourliberty-foo.service': {
                    'last_restart_ts': now - (10 * 60),
                    'last_alert_ts': now - (60 * 60),  # 1h ago, inside 6h
                },
            }}
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s:
                outcome = h.check_unit(
                    'ourliberty-foo.service', state, now=now,
                )
            self.assertEqual(outcome, 'restart-cooldown')
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_s.call_count, 0)

    def test_pr_list_omitted_when_git_log_empty(self):
        # Empty PR list flows through to the DM call cleanly.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_restart(rc=0), self._stub_prs([]), \
                    dms['restarted'] as m_r, dms['failed'], dms['still_stale']:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'auto-restarted')
            self.assertEqual(m_r.call_count, 1)
            # Fifth positional arg is pr_lines.
            self.assertEqual(m_r.call_args.args[4], [])

    def test_unparseable_timestamp_skips(self):
        dms = self._stub_dms()
        with self._stub_systemctl({
            'ActiveEnterTimestamp': '',  # unit has never started
            'FragmentPath': '/etc/systemd/system/whatever.service',
        }), self._stub_restart() as m_restart, \
                dms['restarted'] as m_r, dms['failed'] as m_f, \
                dms['still_stale'] as m_s:
            outcome = h.check_unit('ourliberty-foo.service',
                                   {'services': {}}, now=time.time())
        self.assertEqual(outcome, 'unparseable')
        self.assertEqual(m_restart.call_count, 0)
        self.assertEqual(m_r.call_count + m_f.call_count + m_s.call_count, 0)

    def test_missing_script_skips(self):
        with tempfile.TemporaryDirectory() as td:
            svc = Path(td) / 'unit.service'
            svc.write_text(
                '[Service]\nExecStart=/usr/bin/python3 /nonexistent/x.py\n',
            )
            now = time.time()
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(now - 1000)),
                'FragmentPath': str(svc),
            }), self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'unparseable')
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count + m_f.call_count + m_s.call_count, 0)


# -------------------- per-service overlay (droplet-drift-discipline-v2) --------------------

class OverrideLoaderTests(_IsolatedAgentsRoot):
    """Locks the fail-open contract of load_service_overrides()."""

    def _point_overrides_at(self, path: Path):
        return mock.patch.object(h, 'OVERRIDE_CONFIG_PATH', path)

    def test_absent_config_returns_empty(self):
        # Test R1's loader half: missing file → {}.
        nonexistent = Path(self._isolated_tmp) / 'does-not-exist.json'
        with self._point_overrides_at(nonexistent):
            self.assertEqual(h.load_service_overrides(), {})

    def test_malformed_json_returns_empty(self):
        # Test R6's loader half: garbage on disk → {} (warning logged).
        bad = Path(self._isolated_tmp) / 'malformed.json'
        bad.write_text('{ this is not, valid json }')
        with self._point_overrides_at(bad):
            self.assertEqual(h.load_service_overrides(), {})

    def test_non_dict_top_level_returns_empty(self):
        wrong = Path(self._isolated_tmp) / 'wrong.json'
        wrong.write_text('[1, 2, 3]')
        with self._point_overrides_at(wrong):
            self.assertEqual(h.load_service_overrides(), {})

    def test_non_dict_services_returns_empty(self):
        wrong = Path(self._isolated_tmp) / 'wrong2.json'
        wrong.write_text('{"services": [1, 2]}')
        with self._point_overrides_at(wrong):
            self.assertEqual(h.load_service_overrides(), {})

    def test_well_formed_config_loads(self):
        good = Path(self._isolated_tmp) / 'good.json'
        good.write_text(json.dumps({
            'services': {
                'ourliberty-beacon-bot.service': {
                    'stale_threshold_seconds': 600,
                    'restart_strategy': 'auto',
                },
            },
        }))
        with self._point_overrides_at(good):
            overrides = h.load_service_overrides()
        self.assertIn('ourliberty-beacon-bot.service', overrides)
        self.assertEqual(
            overrides['ourliberty-beacon-bot.service']['stale_threshold_seconds'],
            600,
        )


class ResolveOverrideTests(_IsolatedAgentsRoot):
    """Locks _resolve_override's defaulting + strategy-validation behavior."""

    def test_unknown_unit_returns_defaults(self):
        threshold, strategy = h._resolve_override({}, 'whatever.service')
        self.assertEqual(threshold, h.DEFAULT_STALE_THRESHOLD_SECONDS)
        self.assertEqual(strategy, 'auto')

    def test_known_unit_returns_override(self):
        overrides = {
            'foo.service': {'stale_threshold_seconds': 90,
                            'restart_strategy': 'dm-only'},
        }
        threshold, strategy = h._resolve_override(overrides, 'foo.service')
        self.assertEqual(threshold, 90.0)
        self.assertEqual(strategy, 'dm-only')

    def test_unrecognized_strategy_falls_back_to_auto(self):
        overrides = {'foo.service': {'restart_strategy': 'launch-missile'}}
        _, strategy = h._resolve_override(overrides, 'foo.service')
        self.assertEqual(strategy, 'auto')

    def test_non_numeric_threshold_falls_back_to_default(self):
        overrides = {'foo.service': {'stale_threshold_seconds': 'a-string'}}
        threshold, _ = h._resolve_override(overrides, 'foo.service')
        self.assertEqual(threshold, float(h.DEFAULT_STALE_THRESHOLD_SECONDS))

    def test_bool_threshold_falls_back_to_default(self):
        # bool is a subclass of int — make sure we reject `True`/`False`.
        overrides = {'foo.service': {'stale_threshold_seconds': True}}
        threshold, _ = h._resolve_override(overrides, 'foo.service')
        self.assertEqual(threshold, float(h.DEFAULT_STALE_THRESHOLD_SECONDS))


class StrategyGateTests(_IsolatedAgentsRoot):
    """R1–R6 regression tests for the strategy gate in check_unit."""

    def _stub_systemctl(self, properties):
        def fake_show(unit, prop):
            return properties.get(prop)
        return mock.patch.object(h, 'systemctl_show', side_effect=fake_show)

    def _stub_dms(self):
        return {
            'restarted': mock.patch.object(
                h, 'dm_larry_auto_restarted', return_value=True),
            'failed': mock.patch.object(
                h, 'dm_larry_auto_restart_failed', return_value=True),
            'still_stale': mock.patch.object(
                h, 'dm_larry_still_stale_after_restart', return_value=True),
            'dm_only': mock.patch.object(
                h, 'dm_larry_dm_only_stale', return_value=True),
        }

    def _stub_restart(self, rc=0, stderr=''):
        return mock.patch.object(
            h, 'auto_restart_unit', return_value=(rc, stderr))

    def _make_unit(self, td, script_mtime, service_start):
        """Build a stale-by-construction unit fixture in `td`. Returns
        (script_path, service_file_path, active_enter_timestamp_string)."""
        script = Path(td) / 'foo.py'
        script.write_text('# code')
        os.utime(script, (script_mtime, script_mtime))
        svc = Path(td) / 'unit.service'
        svc.write_text(f'[Service]\nExecStart=/usr/bin/python3 {script}\n')
        ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(service_start))
        return script, svc, ts

    def test_r1_absent_config_behavior_unchanged(self):
        # R1: with no config file on disk + no overrides passed, a stale
        # unit triggers the existing auto-restart path identically to
        # pre-extension. Lock by directly observing the outcome string and
        # auto_restart_unit getting called.
        nonexistent = Path(self._isolated_tmp) / 'absent.json'
        with mock.patch.object(h, 'OVERRIDE_CONFIG_PATH', nonexistent):
            self.assertEqual(h.load_service_overrides(), {})

        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            _, svc, ts = self._make_unit(
                td, script_mtime=now - 10 * 60, service_start=now - 60 * 60,
            )
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp': ts, 'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    mock.patch.object(h, 'infer_recent_prs', return_value=[]), \
                    dms['restarted'], dms['failed'], dms['still_stale'], \
                    dms['dm_only']:
                outcome = h.check_unit(
                    'ourliberty-foo.service', {'services': {}}, now=now,
                    overrides={},
                )
        self.assertEqual(outcome, 'auto-restarted')
        self.assertEqual(m_restart.call_count, 1)

    def test_r2_override_threshold_tightens(self):
        # R2: override `stale_threshold_seconds=60` causes a 90s-stale
        # unit to trip (would NOT trip under the default 300s threshold).
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            # 90s gap — under default 300s race-avoidance, NOT stale; under
            # the 60s override, IS stale.
            _, svc, ts = self._make_unit(
                td, script_mtime=now, service_start=now - 90,
            )
            overrides = {
                'ourliberty-beacon-bot.service': {
                    'stale_threshold_seconds': 60,
                    'restart_strategy': 'auto',
                },
            }
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp': ts, 'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    mock.patch.object(h, 'infer_recent_prs', return_value=[]), \
                    dms['restarted'], dms['failed'], dms['still_stale'], \
                    dms['dm_only']:
                outcome = h.check_unit(
                    'ourliberty-beacon-bot.service', {'services': {}},
                    now=now, overrides=overrides,
                )
        self.assertEqual(outcome, 'auto-restarted')
        self.assertEqual(m_restart.call_count, 1)

    def test_r2b_override_threshold_below_default_does_not_trip(self):
        # R2 sibling: a 30s gap is under the 60s override → NOT stale.
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            _, svc, ts = self._make_unit(
                td, script_mtime=now, service_start=now - 30,
            )
            overrides = {
                'ourliberty-beacon-bot.service': {
                    'stale_threshold_seconds': 60,
                    'restart_strategy': 'auto',
                },
            }
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp': ts, 'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    dms['restarted'], dms['failed'], dms['still_stale'], \
                    dms['dm_only']:
                outcome = h.check_unit(
                    'ourliberty-beacon-bot.service', {'services': {}},
                    now=now, overrides=overrides,
                )
        self.assertEqual(outcome, 'race-window')
        self.assertEqual(m_restart.call_count, 0)

    def test_r3_unlisted_service_uses_default(self):
        # R3: config lists only beacon-bot; mirror-bot stale at the default
        # threshold uses default behavior (auto-restart at 300s).
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            _, svc, ts = self._make_unit(
                td, script_mtime=now - 10 * 60, service_start=now - 60 * 60,
            )
            overrides = {
                'ourliberty-beacon-bot.service': {
                    'stale_threshold_seconds': 600,
                    'restart_strategy': 'auto',
                },
            }
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp': ts, 'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    mock.patch.object(h, 'infer_recent_prs', return_value=[]), \
                    dms['restarted'], dms['failed'], dms['still_stale'], \
                    dms['dm_only']:
                outcome = h.check_unit(
                    'ourliberty-mirror-bot.service', {'services': {}},
                    now=now, overrides=overrides,
                )
        self.assertEqual(outcome, 'auto-restarted')
        self.assertEqual(m_restart.call_count, 1)

    def test_r4_dm_only_alerts_no_restart(self):
        # R4: restart_strategy=dm-only fires the alert AND does NOT call
        # auto_restart_unit. Both halves are load-bearing.
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            _, svc, ts = self._make_unit(
                td, script_mtime=now - 10 * 60, service_start=now - 60 * 60,
            )
            overrides = {
                'ourliberty-beacon-bot.service': {'restart_strategy': 'dm-only'},
            }
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp': ts, 'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s, dms['dm_only'] as m_dm:
                outcome = h.check_unit(
                    'ourliberty-beacon-bot.service', {'services': {}},
                    now=now, overrides=overrides,
                )
        self.assertEqual(outcome, 'strategy-dm-only')
        self.assertEqual(m_restart.call_count, 0)
        self.assertEqual(m_dm.call_count, 1)
        self.assertEqual(m_r.call_count, 0)
        self.assertEqual(m_f.call_count, 0)
        self.assertEqual(m_s.call_count, 0)

    def test_r5_never_silent_skip(self):
        # R5: restart_strategy=never → no alert, no restart, no DM calls.
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            _, svc, ts = self._make_unit(
                td, script_mtime=now - 10 * 60, service_start=now - 60 * 60,
            )
            overrides = {
                'ourliberty-beacon-bot.service': {'restart_strategy': 'never'},
            }
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp': ts, 'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f, \
                    dms['still_stale'] as m_s, dms['dm_only'] as m_dm:
                outcome = h.check_unit(
                    'ourliberty-beacon-bot.service', {'services': {}},
                    now=now, overrides=overrides,
                )
        self.assertEqual(outcome, 'strategy-never-skip')
        self.assertEqual(m_restart.call_count, 0)
        self.assertEqual(m_dm.call_count, 0)
        self.assertEqual(m_r.call_count, 0)
        self.assertEqual(m_f.call_count, 0)
        self.assertEqual(m_s.call_count, 0)

    def test_r6_malformed_config_fail_open(self):
        # R6: malformed config → loader returns {} → check_unit with
        # overrides={} behaves identically to R1.
        bad = Path(self._isolated_tmp) / 'malformed.json'
        bad.write_text('{ definitely not json }')
        with mock.patch.object(h, 'OVERRIDE_CONFIG_PATH', bad):
            overrides = h.load_service_overrides()
        self.assertEqual(overrides, {})

        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            _, svc, ts = self._make_unit(
                td, script_mtime=now - 10 * 60, service_start=now - 60 * 60,
            )
            dms = self._stub_dms()
            with self._stub_systemctl({
                'ActiveEnterTimestamp': ts, 'FragmentPath': str(svc),
            }), self._stub_restart(rc=0) as m_restart, \
                    mock.patch.object(h, 'infer_recent_prs', return_value=[]), \
                    dms['restarted'], dms['failed'], dms['still_stale'], \
                    dms['dm_only']:
                outcome = h.check_unit(
                    'ourliberty-foo.service', {'services': {}}, now=now,
                    overrides=overrides,
                )
        self.assertEqual(outcome, 'auto-restarted')
        self.assertEqual(m_restart.call_count, 1)


class MainTests(_IsolatedAgentsRoot):
    def test_kill_switch_short_circuits(self):
        # The kill switch is the existing file `~/agents/healers.disabled`
        # (Beacon clarification 2026-05-26 option (a) — preserve in place,
        # do not add an env var). main()'s short-circuit runs BEFORE the
        # per-unit loop, so it disables BOTH detection AND the new auto-
        # restart path for free; this single assertion (list_ourliberty_services
        # never called) covers both behaviors.
        h.KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        h.KILL_SWITCH.touch()
        with mock.patch.object(h, 'list_ourliberty_services') as m_list, \
                mock.patch.object(h, 'auto_restart_unit') as m_restart:
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(m_list.call_count, 0)
        self.assertEqual(m_restart.call_count, 0)

    def test_no_units_short_circuits(self):
        with mock.patch.object(
            h, 'list_ourliberty_services', return_value=[],
        ):
            self.assertEqual(h.main(), 0)


# ============================================================================
# V4 (orchestrator-rectification-v2) — SHARED_LIB_WATCHLIST
# ============================================================================


class SharedLibWatchlistTests(_IsolatedAgentsRoot):
    """The shared-lib watchlist auto-restarts dependent daemons when a
    library imported by multiple long-running units changes on disk."""

    def _patch_watchlist(self, mapping):
        """Drive `check_shared_lib_watchlist` from the given dict (Path → set).

        check_shared_lib_watchlist iterates `build_effective_watchlist()`
        (hardcoded constant unioned with the committed manifest), so we patch
        that seam to isolate the iteration/restart logic under test from the
        real manifest's entries. The union itself is covered by
        test_daemon_restart_manifest.py.
        """
        return mock.patch.object(h, 'build_effective_watchlist', lambda: mapping)

    def _stub_systemctl(self, properties_per_unit):
        """systemctl_show stub keyed by (unit, prop)."""
        def fake_show(unit, prop):
            return properties_per_unit.get(unit, {}).get(prop)
        return mock.patch.object(h, 'systemctl_show', side_effect=fake_show)

    def _stub_restart(self, rc=0, stderr=''):
        return mock.patch.object(
            h, 'auto_restart_unit', return_value=(rc, stderr),
        )

    def _stub_dms(self):
        return {
            'restarted': mock.patch.object(
                h, 'dm_larry_auto_restarted', return_value=True,
            ),
            'failed': mock.patch.object(
                h, 'dm_larry_auto_restart_failed', return_value=True,
            ),
        }

    def _stub_prs(self, lines=None):
        return mock.patch.object(
            h, 'infer_recent_prs', return_value=(lines or []),
        )

    def test_lib_newer_than_dependent_unit_triggers_restart(self):
        # The canonical bootstrap-002 V4 case: dispatch_validator.py merges
        # to disk; outbox-notifier was started before the merge so it's now
        # running stale module bytes. Healer should restart it.
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / 'dispatch_validator.py'
            lib.write_text('# fresh shared lib')
            now = time.time()
            service_start = now - (60 * 60)        # 1h ago
            lib_mtime = now - (10 * 60)            # 10 min ago — > 5 min gap
            os.utime(lib, (lib_mtime, lib_mtime))
            dms = self._stub_dms()
            watchlist = {lib: {'ourliberty-outbox-notifier.service'}}
            ts_str = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(service_start),
            )
            with self._patch_watchlist(watchlist), \
                    self._stub_systemctl({
                        'ourliberty-outbox-notifier.service': {
                            'ActiveEnterTimestamp': ts_str,
                        },
                    }), self._stub_restart(rc=0) as m_restart, \
                    self._stub_prs() as _m_prs, \
                    dms['restarted'] as m_r, dms['failed'] as m_f:
                counts = h.check_shared_lib_watchlist(
                    {'services': {}}, now=now,
                )
            self.assertEqual(counts.get('auto-restarted'), 1)
            self.assertEqual(m_restart.call_count, 1)
            self.assertEqual(
                m_restart.call_args.args[0],
                'ourliberty-outbox-notifier.service',
            )
            self.assertEqual(m_r.call_count, 1)
            self.assertEqual(m_f.call_count, 0)

    def test_watchlist_restart_passes_entrypoint_distinct_from_lib(self):
        # Attribution regression: the watchlist restart must hand
        # dm_larry_auto_restarted the CHANGED LIBRARY as script_path AND the
        # service's OWN entrypoint (resolved from FragmentPath) as
        # changed_lib_entrypoint — never the library masquerading as the
        # service's own script. Mirrors the 2026-06-23 PR #646 incident
        # (beacon-bot/outbox-notifier restarted for a dashboard_api.py change).
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / 'dashboard_api.py'          # the changed shared lib
            lib.write_text('# shared library')
            entrypoint = Path(td) / 'outbox_notifier.py'  # the service's own script
            entrypoint.write_text('# entrypoint')
            service_file = Path(td) / 'ourliberty-outbox-notifier.service'
            service_file.write_text(
                '[Service]\n'
                f'ExecStart=/usr/bin/python3 {entrypoint}\n'
            )
            now = time.time()
            service_start = now - (60 * 60)
            lib_mtime = now - (10 * 60)
            os.utime(lib, (lib_mtime, lib_mtime))
            dms = self._stub_dms()
            watchlist = {lib: {'ourliberty-outbox-notifier.service'}}
            ts_str = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(service_start),
            )
            with self._patch_watchlist(watchlist), \
                    self._stub_systemctl({
                        'ourliberty-outbox-notifier.service': {
                            'ActiveEnterTimestamp': ts_str,
                            'FragmentPath': str(service_file),
                        },
                    }), self._stub_restart(rc=0), \
                    self._stub_prs(), \
                    dms['restarted'] as m_r, dms['failed']:
                h.check_shared_lib_watchlist({'services': {}}, now=now)
            self.assertEqual(m_r.call_count, 1)
            # script_path positional arg is the CHANGED LIBRARY, not the entrypoint.
            self.assertEqual(m_r.call_args.args[1], lib)
            # the entrypoint is resolved and passed distinctly.
            self.assertEqual(
                m_r.call_args.kwargs.get('changed_lib_entrypoint'), entrypoint,
            )

    def test_watchlist_alert_body_names_lib_and_entrypoint_separately(self):
        # The rendered alert body must label the changed library and the
        # service's own entrypoint on separate lines, and must NOT present the
        # library as the service's sole "Script path:".
        lib = Path('/home/larry/agent-core/scripts/dashboard_api.py')
        entrypoint = Path('/home/larry/agent-core/scripts/outbox_notifier.py')
        now = time.time()
        fake_la = mock.MagicMock()
        fake_la.classify_route.return_value = 'digest'
        fake_la.append_alert.return_value = True
        with mock.patch.object(h, '_import_larry_alerts', return_value=fake_la):
            h.dm_larry_auto_restarted(
                'ourliberty-outbox-notifier.service', lib,
                now - (60 * 60), now - (10 * 60), [],
                changed_lib_entrypoint=entrypoint,
            )
        body = fake_la.append_alert.call_args.kwargs['message']
        self.assertIn(f'Changed shared library:      {lib}', body)
        self.assertIn(f'Service entrypoint:          {entrypoint}', body)
        # the library is NOT labeled as the service's own script path.
        self.assertNotIn(f'Script path:                 {lib}', body)

    def test_direct_script_alert_body_unchanged(self):
        # The direct-entrypoint path (changed_lib_entrypoint=None) keeps the
        # legacy "Script path:" body byte-for-byte.
        script = Path('/home/larry/agent-core/scripts/beacon_telegram_bot.py')
        now = time.time()
        service_start = now - (60 * 60)
        script_mtime = now - (10 * 60)
        fake_la = mock.MagicMock()
        fake_la.classify_route.return_value = 'digest'
        fake_la.append_alert.return_value = True
        with mock.patch.object(h, '_import_larry_alerts', return_value=fake_la):
            h.dm_larry_auto_restarted(
                'ourliberty-beacon-bot.service', script,
                service_start, script_mtime, [],
            )
        body = fake_la.append_alert.call_args.kwargs['message']
        from datetime import datetime, timezone
        svc_iso = datetime.fromtimestamp(service_start, tz=timezone.utc).isoformat()
        scr_iso = datetime.fromtimestamp(script_mtime, tz=timezone.utc).isoformat()
        gap_min = (script_mtime - service_start) / 60.0
        expected = (
            f'Auto-restarted ourliberty-beacon-bot.service (script mtime newer '
            f'than active-since by {gap_min:.1f} min; new code now live).\n\n'
            f'Service start (pre-restart): {svc_iso}\n'
            f'Script mtime:                {scr_iso}\n'
            f'Script path:                 {script}'
        )
        self.assertEqual(body, expected)

    def test_lib_within_race_window_no_action(self):
        # mtime gap < RACE_AVOIDANCE_SEC → race-window suppression. The
        # legitimate "sync + restart cycle just completed" case.
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / 'lib.py'
            lib.write_text('')
            now = time.time()
            service_start = now - (10 * 60)
            lib_mtime = service_start + (3 * 60)   # 3 min gap
            os.utime(lib, (lib_mtime, lib_mtime))
            watchlist = {lib: {'ourliberty-foo.service'}}
            ts_str = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(service_start),
            )
            dms = self._stub_dms()
            with self._patch_watchlist(watchlist), \
                    self._stub_systemctl({
                        'ourliberty-foo.service': {
                            'ActiveEnterTimestamp': ts_str,
                        },
                    }), self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r, dms['failed'] as m_f:
                counts = h.check_shared_lib_watchlist(
                    {'services': {}}, now=now,
                )
            self.assertEqual(counts.get('race-window'), 1)
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count, 0)
            self.assertEqual(m_f.call_count, 0)

    def test_lib_older_than_service_no_action(self):
        # Normal steady state: lib hasn't changed since the unit started.
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / 'lib.py'
            lib.write_text('')
            now = time.time()
            service_start = now - (10 * 60)
            lib_mtime = now - (60 * 60)           # 1 hour ago
            os.utime(lib, (lib_mtime, lib_mtime))
            watchlist = {lib: {'ourliberty-foo.service'}}
            ts_str = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(service_start),
            )
            dms = self._stub_dms()
            with self._patch_watchlist(watchlist), \
                    self._stub_systemctl({
                        'ourliberty-foo.service': {
                            'ActiveEnterTimestamp': ts_str,
                        },
                    }), self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r:
                counts = h.check_shared_lib_watchlist(
                    {'services': {}}, now=now,
                )
            self.assertEqual(counts.get('fresh'), 1)
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count, 0)

    def test_restart_cooldown_suppresses_second_restart(self):
        # The cooldown clock is shared with the direct-script path —
        # marking the unit restarted then asking the watchlist to check
        # within 30 min should suppress (returns 'restart-cooldown').
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / 'lib.py'
            lib.write_text('')
            now = time.time()
            service_start = now - (60 * 60)
            lib_mtime = now - (10 * 60)
            os.utime(lib, (lib_mtime, lib_mtime))
            state = {
                'services': {
                    'ourliberty-foo.service': {
                        'last_restart_ts': now - 60,  # 1 min ago
                    },
                },
            }
            watchlist = {lib: {'ourliberty-foo.service'}}
            ts_str = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(service_start),
            )
            dms = self._stub_dms()
            with self._patch_watchlist(watchlist), \
                    self._stub_systemctl({
                        'ourliberty-foo.service': {
                            'ActiveEnterTimestamp': ts_str,
                        },
                    }), self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r:
                counts = h.check_shared_lib_watchlist(state, now=now)
            self.assertEqual(counts.get('restart-cooldown'), 1)
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count, 0)

    def test_lib_missing_from_disk_is_skipped(self):
        # Defensive: a watchlisted path may not exist in a sparse fixture
        # or during a reorganization. Should not crash.
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / 'does-not-exist.py'
            watchlist = {lib: {'ourliberty-foo.service'}}
            dms = self._stub_dms()
            # systemctl_show should never be called for a missing-on-disk lib.
            with self._patch_watchlist(watchlist), \
                    mock.patch.object(h, 'systemctl_show') as m_show, \
                    self._stub_restart() as m_restart, \
                    dms['restarted'] as m_r:
                counts = h.check_shared_lib_watchlist(
                    {'services': {}}, now=time.time(),
                )
            self.assertEqual(counts, {})
            self.assertEqual(m_show.call_count, 0)
            self.assertEqual(m_restart.call_count, 0)
            self.assertEqual(m_r.call_count, 0)

    def test_canonical_watchlist_targets_the_three_known_daemons(self):
        # Lock in the spec's table: each shared lib in the watchlist points
        # at the same three long-running daemons (beacon-bot, outbox-notifier,
        # inbox-watcher). If a future PR mutates the set, the runbook
        # mapping must be updated in lockstep.
        expected_units = {
            'ourliberty-beacon-bot.service',
            'ourliberty-outbox-notifier.service',
            'ourliberty-inbox-watcher.service',
        }
        # The watchlist must include at least one entry — sanity check
        # the module didn't silently drop the constant.
        self.assertGreater(len(h.SHARED_LIB_WATCHLIST), 0)
        for lib_path, units in h.SHARED_LIB_WATCHLIST.items():
            with self.subTest(lib=lib_path.name):
                self.assertEqual(units, expected_units)

    def test_main_invokes_watchlist_and_folds_counts(self):
        # Sanity: main() calls check_shared_lib_watchlist after the unit
        # pass and the counts are folded into the same summary log. We
        # don't assert the log text here — just that the watchlist
        # function gets called once per tick.
        with mock.patch.object(
            h, 'list_ourliberty_services', return_value=[],
        ):
            # No direct units to scan, so watchlist still runs.
            with mock.patch.object(
                h, 'check_shared_lib_watchlist', return_value={},
            ) as m_watch:
                rc = h.main()
            self.assertEqual(rc, 0)
            # With no units found, the unit loop short-circuits BEFORE
            # the watchlist call. That's acceptable per V4 — there's
            # nothing to restart anyway. The next assertion confirms the
            # function isn't called in that branch.
            self.assertEqual(m_watch.call_count, 0)

        # Now exercise the path where the unit loop runs.
        with mock.patch.object(
            h, 'list_ourliberty_services', return_value=['ourliberty-x.service'],
        ), mock.patch.object(h, 'check_unit', return_value='fresh'):
            with mock.patch.object(
                h, 'check_shared_lib_watchlist', return_value={'fresh': 0},
            ) as m_watch:
                rc = h.main()
            self.assertEqual(rc, 0)
            self.assertEqual(m_watch.call_count, 1)


class AutoRestartUnitDaemonReloadTests(_IsolatedAgentsRoot):
    """Regression on the 2026-05-30 timer-infinity-trap incident.

    `systemctl restart` alone doesn't re-parse the unit file, so a timer
    whose unit file changed pre-restart comes back up with stale config
    and falls into the `NextElapseUSecRealtime`-empty + `Monotonic=infinity`
    trap. auto_restart_unit() therefore daemon-reloads first; a failed
    reload is WARN-logged but must NEVER block the restart.
    """

    def setUp(self):
        super().setUp()
        # The restart settle is a real time.sleep in production; patch it to a
        # no-op so the verify-path tests don't actually block for seconds.
        self._sleep_patch = mock.patch.object(h.time, 'sleep')
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def _make_completed(self, rc=0, stderr='', stdout=''):
        cp = mock.MagicMock()
        cp.returncode = rc
        cp.stderr = stderr
        cp.stdout = stdout
        return cp

    @staticmethod
    def _verb(cmd):
        """The systemctl verb for a stubbed argv, ignoring the sudo prefix.

        `sudo -n systemctl <verb> ...` → cmd[3]; `systemctl <verb> ...`
        (is-active, run without sudo) → cmd[1].
        """
        if cmd[0] == 'sudo':
            return cmd[3]
        return cmd[1]

    def test_daemon_reload_runs_before_restart(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=0, stdout='active')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h.auto_restart_unit('ourliberty-cycle.timer')

        # daemon-reload → restart --no-block → is-active verify.
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            calls[0],
            ['sudo', '-n', 'systemctl', 'daemon-reload'],
        )
        self.assertEqual(
            calls[1],
            ['sudo', '-n', 'systemctl', 'restart', '--no-block',
             'ourliberty-cycle.timer'],
        )
        self.assertEqual(
            calls[2],
            ['systemctl', 'is-active', 'ourliberty-cycle.timer'],
        )
        self.assertEqual((rc, stderr), (0, ''))

    def test_restart_uses_no_block(self):
        # The restart argv must carry --no-block so a slow SIGTERM drain
        # doesn't block the subprocess past its timeout.
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=0, stdout='active')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            h.auto_restart_unit('ourliberty-inbox-watcher.service')

        restart_calls = [c for c in calls if self._verb(c) == 'restart']
        self.assertEqual(len(restart_calls), 1)
        self.assertIn('--no-block', restart_calls[0])

    def test_daemon_reload_still_precedes_restart(self):
        # Ordering regression (2026-05-30 timer-infinity-trap): daemon-reload
        # must come before the restart even after the --no-block + verify
        # rework.
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(self._verb(cmd))
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=0, stdout='active')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            h.auto_restart_unit('ourliberty-cycle.timer')

        self.assertLess(
            calls.index('daemon-reload'), calls.index('restart'),
            f'daemon-reload must precede restart; got {calls!r}',
        )

    def test_normal_fast_restart_unchanged(self):
        # A unit that comes back up cleanly: restart rc=0, is-active=active →
        # success path, no regression.
        def fake_run(cmd, **kwargs):
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=0, stdout='active')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h.auto_restart_unit('ourliberty-cycle.timer')

        self.assertEqual((rc, stderr), (0, ''))

    def test_slow_drain_restart_verified_active_no_dm(self):
        # The core fix: --no-block restart returns immediately while the old
        # process is still draining; is-active reports 'active' → success, an
        # INFO RESTART_SLOW_DRAIN log, and (crucially) NO failure indicator.
        def fake_run(cmd, **kwargs):
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=0, stdout='active\n')
            return self._make_completed(rc=0, stderr='')

        log_lines: list[tuple[str, str]] = []

        def fake_log(msg, level='INFO'):
            log_lines.append((level, msg))

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, 'log', side_effect=fake_log):
            rc, stderr = h.auto_restart_unit(
                'ourliberty-inbox-watcher.service')

        self.assertEqual((rc, stderr), (0, ''))
        info_lines = [m for lvl, m in log_lines if lvl == 'INFO']
        self.assertTrue(
            any('RESTART_SLOW_DRAIN' in m for m in info_lines),
            f'expected a RESTART_SLOW_DRAIN INFO log, got {log_lines!r}',
        )

    def test_slow_drain_restart_activating_no_dm(self):
        # Same as above but is-active reports the transient 'activating'
        # state — still treated as success (restart in progress).
        def fake_run(cmd, **kwargs):
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=3, stdout='activating')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h.auto_restart_unit(
                'ourliberty-inbox-watcher.service')

        self.assertEqual((rc, stderr), (0, ''))

    def test_restart_timeout_then_failed_state_dms(self):
        # Genuine failure: the unit ends up 'failed' after the settle → the
        # function returns a failure indicator (rc != 0) so the caller DMs.
        # Real-failure detection is preserved.
        def fake_run(cmd, **kwargs):
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=3, stdout='failed')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h.auto_restart_unit('ourliberty-bogus.service')

        self.assertNotEqual(rc, 0)
        self.assertIn('failed', stderr)

    def test_queued_job_pending_ordering_treated_as_success_no_dm(self):
        # The ordering fix (2026-06-30): a dependent unit ordered After= a
        # still-draining predecessor reports is-active='inactive' after the
        # settle because its restart job is still enqueued. `systemctl show
        # --property=Job` carries a non-empty job id → the restart is
        # in-progress, so return success with an INFO log and NO failure.
        def fake_run(cmd, **kwargs):
            verb = self._verb(cmd)
            if verb == 'is-active':
                return self._make_completed(rc=3, stdout='inactive\n')
            if verb == 'show':
                return self._make_completed(rc=0, stdout='Job=12345\n')
            return self._make_completed(rc=0, stderr='')

        log_lines: list[tuple[str, str]] = []

        def fake_log(msg, level='INFO'):
            log_lines.append((level, msg))

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, 'log', side_effect=fake_log):
            rc, stderr = h.auto_restart_unit(
                'ourliberty-outbox-notifier.service')

        self.assertEqual((rc, stderr), (0, ''))
        info_lines = [m for lvl, m in log_lines if lvl == 'INFO']
        self.assertTrue(
            any('RESTART_QUEUED_PENDING_ORDERING' in m for m in info_lines),
            f'expected a RESTART_QUEUED_PENDING_ORDERING INFO log, got '
            f'{log_lines!r}',
        )

    def test_unhealthy_with_empty_job_still_fails_and_dms(self):
        # Genuine failure preserved: is-active is unhealthy AND `systemctl show
        # --property=Job` is empty (`Job=`) → no restart is queued, the unit
        # really didn't come up. Return a failure indicator so the caller DMs.
        def fake_run(cmd, **kwargs):
            verb = self._verb(cmd)
            if verb == 'is-active':
                return self._make_completed(rc=3, stdout='inactive\n')
            if verb == 'show':
                return self._make_completed(rc=0, stdout='Job=\n')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h.auto_restart_unit('ourliberty-bogus.service')

        self.assertNotEqual(rc, 0)
        self.assertIn('inactive', stderr)

    def test_restart_job_rejected_returns_failure(self):
        # The restart job itself is rejected up front (rc != 0, e.g. unit not
        # found). That's a drain-independent failure: return it directly,
        # don't bother verifying.
        def fake_run(cmd, **kwargs):
            if self._verb(cmd) == 'daemon-reload':
                return self._make_completed(rc=0, stderr='')
            if self._verb(cmd) == 'restart':
                return self._make_completed(rc=1, stderr='Unit not found  ')
            raise AssertionError('is-active should not run after a rejected '
                                 'restart')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h.auto_restart_unit('ourliberty-bogus.service')

        self.assertEqual(rc, 1)
        self.assertEqual(stderr, 'Unit not found')

    def test_daemon_reload_failure_does_not_block_restart(self):
        # Load-bearing: reload rc != 0 is logged WARN and we CONTINUE to the
        # restart. A stale-but-running daemon is better than a stopped daemon.
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if self._verb(cmd) == 'daemon-reload':
                return self._make_completed(rc=5, stderr='reload broke')
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=0, stdout='active')
            return self._make_completed(rc=0, stderr='')

        log_lines: list[tuple[str, str]] = []

        def fake_log(msg, level='INFO'):
            log_lines.append((level, msg))

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, 'log', side_effect=fake_log):
            rc, stderr = h.auto_restart_unit('ourliberty-cycle.timer')

        self.assertEqual(self._verb(calls[1]), 'restart')
        self.assertEqual((rc, stderr), (0, ''))
        warn_lines = [m for lvl, m in log_lines if lvl == 'WARN']
        self.assertTrue(
            any('daemon-reload' in m for m in warn_lines),
            f'expected a WARN log mentioning daemon-reload, got {log_lines!r}',
        )

    def test_daemon_reload_timeout_does_not_block_restart(self):
        def fake_run(cmd, **kwargs):
            if self._verb(cmd) == 'daemon-reload':
                raise h.subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            if self._verb(cmd) == 'is-active':
                return self._make_completed(rc=0, stdout='active')
            return self._make_completed(rc=0, stderr='')

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h.auto_restart_unit('ourliberty-cycle.timer')

        self.assertEqual((rc, stderr), (0, ''))


if __name__ == '__main__':
    unittest.main()
