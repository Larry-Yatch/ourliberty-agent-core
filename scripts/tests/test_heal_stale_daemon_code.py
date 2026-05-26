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


# -------------------- orchestration tests --------------------

class CheckUnitTests(_IsolatedAgentsRoot):
    """Larry's acceptance cases at the orchestrator level — full integration
    with systemctl + ExecStart parsing + larry_alerts stubbed.
    """

    def _stub_systemctl(self, properties):
        """Patch h.systemctl_show to return canned values per property name."""
        def fake_show(unit, prop):
            return properties.get(prop)
        return mock.patch.object(h, 'systemctl_show', side_effect=fake_show)

    def _stub_append_alert(self, return_value=True):
        """Patch the dynamically-imported larry_alerts to be a no-op."""
        return mock.patch.object(
            h, 'dm_larry_about_stale', return_value=return_value,
        )

    def _make_service_file(self, td, script_path):
        svc = Path(td) / 'unit.service'
        svc.write_text(
            f'[Service]\nExecStart=/usr/bin/python3 {script_path}\n',
        )
        return svc

    def test_case_a_script_newer_alerts(self):
        # Acceptance (a): script mtime > service start by > 5 min → alert.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)            # 1h ago
            script_mtime = now - (10 * 60)             # 10 min ago
            os.utime(script, (script_mtime, script_mtime))
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_append_alert(True) as m_dm:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'alerted')
            self.assertEqual(m_dm.call_count, 1)

    def test_case_b_script_older_no_alert(self):
        # Acceptance (b): script mtime < service start → no alert.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (10 * 60)            # 10 min ago
            script_mtime = now - (60 * 60)             # 1 hour ago
            os.utime(script, (script_mtime, script_mtime))
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_append_alert() as m_dm:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'fresh')
            self.assertEqual(m_dm.call_count, 0)

    def test_case_c_race_window_no_alert(self):
        # Acceptance (c): script mtime within 5 min of service start → suppressed.
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            # Service started 10 min ago; script touched 6 min ago — that's
            # 4 min after service start, inside the 5-min race window.
            service_start = now - (10 * 60)
            script_mtime = service_start + (4 * 60)
            os.utime(script, (script_mtime, script_mtime))
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_append_alert() as m_dm:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'race-window')
            self.assertEqual(m_dm.call_count, 0)

    def test_cooldown_active_suppresses_realert(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            state = {'services': {
                'ourliberty-foo.service': {'last_alert_ts': now - 60},
            }}
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_append_alert() as m_dm:
                outcome = h.check_unit('ourliberty-foo.service',
                                       state, now=now)
            self.assertEqual(outcome, 'cooldown')
            self.assertEqual(m_dm.call_count, 0)

    def test_cooldown_expired_realerts(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / 'foo.py'
            script.write_text('# code')
            svc = self._make_service_file(td, script)
            now = time.time()
            service_start = now - (60 * 60)
            script_mtime = now - (10 * 60)
            os.utime(script, (script_mtime, script_mtime))
            # Last alert was 7h ago — past the 6h cooldown.
            state = {'services': {
                'ourliberty-foo.service': {
                    'last_alert_ts': now - (7 * 60 * 60),
                },
            }}
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(service_start)),
                'FragmentPath': str(svc),
            }), self._stub_append_alert(True) as m_dm:
                outcome = h.check_unit('ourliberty-foo.service',
                                       state, now=now)
            self.assertEqual(outcome, 'alerted')
            self.assertEqual(m_dm.call_count, 1)

    def test_unparseable_timestamp_skips(self):
        with self._stub_systemctl({
            'ActiveEnterTimestamp': '',  # unit has never started
            'FragmentPath': '/etc/systemd/system/whatever.service',
        }), self._stub_append_alert() as m_dm:
            outcome = h.check_unit('ourliberty-foo.service',
                                   {'services': {}}, now=time.time())
        self.assertEqual(outcome, 'unparseable')
        self.assertEqual(m_dm.call_count, 0)

    def test_missing_script_skips(self):
        with tempfile.TemporaryDirectory() as td:
            svc = Path(td) / 'unit.service'
            svc.write_text(
                '[Service]\nExecStart=/usr/bin/python3 /nonexistent/x.py\n',
            )
            now = time.time()
            with self._stub_systemctl({
                'ActiveEnterTimestamp':
                    time.strftime('%Y-%m-%d %H:%M:%S',
                                  time.localtime(now - 1000)),
                'FragmentPath': str(svc),
            }), self._stub_append_alert() as m_dm:
                outcome = h.check_unit('ourliberty-foo.service',
                                       {'services': {}}, now=now)
            self.assertEqual(outcome, 'unparseable')
            self.assertEqual(m_dm.call_count, 0)


class MainTests(_IsolatedAgentsRoot):
    def test_kill_switch_short_circuits(self):
        h.KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        h.KILL_SWITCH.touch()
        with mock.patch.object(h, 'list_ourliberty_services') as m_list:
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(m_list.call_count, 0)

    def test_no_units_short_circuits(self):
        with mock.patch.object(
            h, 'list_ourliberty_services', return_value=[],
        ):
            self.assertEqual(h.main(), 0)


if __name__ == '__main__':
    unittest.main()
