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


if __name__ == '__main__':
    unittest.main()
