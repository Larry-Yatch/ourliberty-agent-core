#!/usr/bin/env python3
"""Tests for watchdog.py — adapted-for-our-topology system health monitor.

Phase D3.5-prep (2026-05-12). Covers each of the 9 checks, the auto-restart
machinery, the cgroup-aggregate check, and the systemd-state allow-list
(M1 fix: auto-restart SubState is treated as alive).

Run:
    python3 -m unittest scripts.tests.test_watchdog
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_lease  # noqa: E402  # TTL constant for the dispatch-lease tests
import larry_alerts  # noqa: E402
import watchdog  # noqa: E402


def _systemctl_show_stub(active_state: str, sub_state: str = 'running'):
    """Build a fake subprocess.run result for `systemctl show -p ActiveState -p SubState`."""
    text = f'ActiveState={active_state}\nSubState={sub_state}\n'
    return mock.MagicMock(stdout=text, returncode=0)


class _IsolatedRootsTest(unittest.TestCase):
    """Base — points watchdog + larry_alerts at a tempdir so writes don't escape."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(watchdog, 'AGENTS_ROOT', self._tmp_path),
            mock.patch.object(watchdog, 'BLACKBOARD',
                              self._tmp_path / 'blackboard'),
            mock.patch.object(watchdog, 'LOG_DIR', self._tmp_path / 'logs'),
            mock.patch.object(watchdog, 'HEALTH_FILE',
                              self._tmp_path / 'blackboard' / 'system-health.json'),
            mock.patch.object(watchdog, '_FLAP_STREAK_DIR',
                              self._tmp_path / 'state' / 'auto-restart-flap'),
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', self._tmp_path),
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              self._tmp_path / 'blackboard' / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              self._tmp_path / 'state' / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              self._tmp_path / 'state' / 'beacon-alerts-offset.txt'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


# ---------- systemd state machinery ----------


class IsServiceAliveTest(_IsolatedRootsTest):
    def test_active_running_is_alive(self):
        with mock.patch.object(watchdog.subprocess, 'run',
                               return_value=_systemctl_show_stub('active', 'running')):
            self.assertTrue(watchdog.is_service_alive('test.service'))

    def test_activating_is_alive(self):
        with mock.patch.object(watchdog.subprocess, 'run',
                               return_value=_systemctl_show_stub('activating', 'start')):
            self.assertTrue(watchdog.is_service_alive('test.service'))

    def test_auto_restart_substate_is_alive(self):
        # M1 fix — systemd has a restart scheduled; watchdog stays out.
        with mock.patch.object(watchdog.subprocess, 'run',
                               return_value=_systemctl_show_stub('failed', 'auto-restart')):
            self.assertTrue(watchdog.is_service_alive('test.service'))

    def test_failed_is_dead(self):
        with mock.patch.object(watchdog.subprocess, 'run',
                               return_value=_systemctl_show_stub('failed', 'failed')):
            self.assertFalse(watchdog.is_service_alive('test.service'))

    def test_inactive_is_dead(self):
        with mock.patch.object(watchdog.subprocess, 'run',
                               return_value=_systemctl_show_stub('inactive', 'dead')):
            self.assertFalse(watchdog.is_service_alive('test.service'))

    def test_subprocess_exception_returns_false(self):
        with mock.patch.object(watchdog.subprocess, 'run',
                               side_effect=Exception('boom')):
            self.assertFalse(watchdog.is_service_alive('test.service'))


# ---------- restartable-service checks ----------


class CheckAutoRestartTest(_IsolatedRootsTest):
    def test_alive_service_returns_ok_no_action(self):
        with mock.patch.object(watchdog, '_systemctl_states',
                               return_value=('active', 'running')):
            with mock.patch.object(watchdog, '_attempt_start') as mock_start:
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'ok')
        mock_start.assert_not_called()

    def test_dead_service_attempts_start_and_recovers(self):
        with mock.patch.object(watchdog, '_systemctl_states',
                               return_value=('failed', 'failed')):
            with mock.patch.object(watchdog, '_attempt_start', return_value=True):
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'recovered')
        # Recovered alert should land in queue as routine info (digest lane,
        # no DM) per alert-pipeline-rework P1b.
        contents = larry_alerts.ALERTS_FILE.read_text().strip()
        self.assertIn('auto-restarted', contents)
        self.assertIn('"info"', contents)
        self.assertIn('"digest"', contents)
        # The recovery subject carries a `:recovered` suffix, keeping it
        # distinct from the genuine down alert so a translation can silence
        # this benign restart-window race without muting real outages.
        record = json.loads(contents.splitlines()[-1])
        self.assertEqual(record['subject'], 'ourliberty-x:recovered')
        self.assertTrue(record['subject'].endswith(':recovered'))

    def test_dead_service_start_fails_appends_critical(self):
        with mock.patch.object(watchdog, '_systemctl_states',
                               return_value=('failed', 'failed')):
            with mock.patch.object(watchdog, '_attempt_start', return_value=False):
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'down')
        contents = larry_alerts.ALERTS_FILE.read_text().strip()
        self.assertIn('auto-restart failed', contents)
        self.assertIn('"critical"', contents)
        record = json.loads(contents.splitlines()[-1])
        self.assertEqual(record['subject'], 'ourliberty-x')
        self.assertIn('sudo systemctl status', record['suggested_action'])

    def test_systemctl_error_treated_as_down_and_started(self):
        # (None, None) = systemctl query failed → treat as down, actuate.
        with mock.patch.object(watchdog, '_systemctl_states',
                               return_value=(None, None)):
            with mock.patch.object(watchdog, '_attempt_start',
                                   return_value=True) as mock_start:
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'recovered')
        mock_start.assert_called_once()

    # ---- auto-restart flap detection (audit fix) ----

    def test_single_auto_restart_tick_defers_no_actuation_no_alert(self):
        # First observation in auto-restart: defer to systemd, no actuation,
        # no alert (a normal restart looks like this for one tick).
        with mock.patch.object(watchdog, '_systemctl_states',
                               return_value=('activating', 'auto-restart')):
            with mock.patch.object(watchdog, '_attempt_start') as mock_start:
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'auto-restart-wait')
        self.assertEqual(result['streak'], 1)
        mock_start.assert_not_called()
        self.assertFalse(larry_alerts.ALERTS_FILE.exists()
                         and larry_alerts.ALERTS_FILE.read_text().strip())

    def test_sustained_auto_restart_streak_escalates_critical_no_actuation(self):
        with mock.patch.object(watchdog, '_systemctl_states',
                               return_value=('activating', 'auto-restart')):
            with mock.patch.object(watchdog, '_attempt_start') as mock_start:
                first = watchdog._check_auto_restart('ourliberty-x', 'x')
                second = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(first['status'], 'auto-restart-wait')
        self.assertEqual(second['status'], 'flapping')
        self.assertEqual(second['streak'], 2)
        mock_start.assert_not_called()  # never actuate during auto-restart
        contents = larry_alerts.ALERTS_FILE.read_text().strip()
        self.assertIn('crash-looping', contents)
        self.assertIn('"critical"', contents)
        record = json.loads(contents.splitlines()[-1])
        self.assertEqual(record['subject'], 'ourliberty-x-flapping')

    def test_confirmed_down_breaks_auto_restart_streak(self):
        # auto-restart tick (streak 1) → a confirmed dead state with a FAILED
        # start (no recovery) must still reset the streak, so the next
        # auto-restart observation starts fresh at 1 (not escalate to flapping).
        with mock.patch.object(watchdog, '_attempt_start', return_value=False):
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('activating', 'auto-restart')):
                first = watchdog._check_auto_restart('ourliberty-x', 'x')
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('failed', 'failed')):
                down = watchdog._check_auto_restart('ourliberty-x', 'x')
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('activating', 'auto-restart')):
                again = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(first['streak'], 1)
        self.assertEqual(down['status'], 'down')
        self.assertEqual(again['streak'], 1)  # streak reset by the down tick
        self.assertEqual(again['status'], 'auto-restart-wait')

    def test_systemctl_error_does_not_reset_streak(self):
        # An unknown (None) reading must NOT reset the streak — else a flapping
        # service could evade detection by interleaving systemctl-error ticks.
        with mock.patch.object(watchdog, '_attempt_start', return_value=False):
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('activating', 'auto-restart')):
                watchdog._check_auto_restart('ourliberty-x', 'x')  # streak 1
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=(None, None)):
                watchdog._check_auto_restart('ourliberty-x', 'x')  # no reset
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('activating', 'auto-restart')):
                again = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(again['status'], 'flapping')
        self.assertEqual(again['streak'], 2)

    def test_recovery_resets_flap_streak(self):
        # An auto-restart tick, then the service comes up clean → streak reset,
        # so a later single auto-restart tick is back to streak 1 (no alert).
        with mock.patch.object(watchdog, '_attempt_start', return_value=True):
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('activating', 'auto-restart')):
                watchdog._check_auto_restart('ourliberty-x', 'x')
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('active', 'running')):
                ok = watchdog._check_auto_restart('ourliberty-x', 'x')
            with mock.patch.object(watchdog, '_systemctl_states',
                                   return_value=('activating', 'auto-restart')):
                again = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(ok['status'], 'ok')
        self.assertEqual(again['streak'], 1)
        self.assertEqual(again['status'], 'auto-restart-wait')


# ---------- inbox-watcher memory (V2 RSS) ----------


class CheckInboxWatcherMemoryTest(_IsolatedRootsTest):
    def test_main_pid_zero_returns_unavailable(self):
        with mock.patch.object(watchdog, '_inbox_watcher_main_pid', return_value=None):
            result = watchdog.check_inbox_watcher_memory()
        self.assertEqual(result['status'], 'unavailable')

    def test_rss_unreadable_returns_unavailable(self):
        with mock.patch.object(watchdog, '_inbox_watcher_main_pid', return_value=99999):
            with mock.patch.object(watchdog, '_read_process_rss', return_value=None):
                result = watchdog.check_inbox_watcher_memory()
        self.assertEqual(result['status'], 'unavailable')

    def test_normal_rss_returns_ok(self):
        with mock.patch.object(watchdog, '_inbox_watcher_main_pid', return_value=1234):
            with mock.patch.object(watchdog, '_read_process_rss',
                                   return_value=100 * 1024 * 1024):  # 100 MB
                result = watchdog.check_inbox_watcher_memory()
        self.assertEqual(result['status'], 'ok')
        self.assertAlmostEqual(result['rss_mb'], 104.9, delta=1.0)

    def test_over_threshold_triggers_restart(self):
        rss = int(watchdog.WATCHER_PROCESS_RSS_THRESHOLD_BYTES * 1.1)
        with mock.patch.object(watchdog, '_inbox_watcher_main_pid', return_value=1234):
            with mock.patch.object(watchdog, '_read_process_rss', return_value=rss):
                with mock.patch.object(watchdog.subprocess, 'run') as mock_run:
                    with mock.patch.object(watchdog.time, 'sleep'):
                        result = watchdog.check_inbox_watcher_memory()
        self.assertEqual(result['status'], 'restarted')
        # sudo -n systemctl restart should have been called.
        called_args = [c.args[0] for c in mock_run.call_args_list]
        self.assertTrue(any(
            args[:4] == ['sudo', '-n', 'systemctl', 'restart']
            for args in called_args
        ))
        # Critical alert appended.
        contents = larry_alerts.ALERTS_FILE.read_text()
        self.assertIn('inbox-watcher process RSS', contents)

    def test_cooldown_skips_restart(self):
        rss = int(watchdog.WATCHER_PROCESS_RSS_THRESHOLD_BYTES * 1.1)
        cooldown_marker = self._tmp_path / 'state' / 'inbox-watcher-mem-restart-cooldown'
        cooldown_marker.parent.mkdir(parents=True, exist_ok=True)
        cooldown_marker.touch()
        with mock.patch.object(watchdog, '_inbox_watcher_main_pid', return_value=1234):
            with mock.patch.object(watchdog, '_read_process_rss', return_value=rss):
                with mock.patch.object(watchdog.subprocess, 'run') as mock_run:
                    result = watchdog.check_inbox_watcher_memory()
        self.assertEqual(result['status'], 'cooldown')
        # No restart call.
        for c in mock_run.call_args_list:
            self.assertNotIn('restart', c.args[0])


# ---------- inbox-watcher cgroup-aggregate (new, Q6) ----------


class CheckInboxWatcherCgroupTest(_IsolatedRootsTest):
    def test_memory_max_unset_returns_unavailable(self):
        with mock.patch.object(watchdog, '_read_memory_max', return_value=None):
            result = watchdog.check_inbox_watcher_cgroup()
        self.assertEqual(result['status'], 'unavailable')

    def _patch_cgroup_open(self, cgroup_value: str, raise_fnf: bool = False):
        """Return a context manager that intercepts only the cgroup memory.current open.

        Scoping the open mock too broadly (mock.patch('builtins.open')) blocks
        larry_alerts.append_alert's own write — tests can't then read the
        queue contents back. This dispatcher passes other paths through.
        """
        real_open = open

        def fake_open(path, *args, **kwargs):
            if 'memory.current' in str(path):
                if raise_fnf:
                    raise FileNotFoundError(path)
                return mock.mock_open(read_data=cgroup_value).return_value
            return real_open(path, *args, **kwargs)

        return mock.patch('builtins.open', side_effect=fake_open)

    def test_cgroup_file_missing_returns_unavailable(self):
        with mock.patch.object(watchdog, '_read_memory_max', return_value=2 * 1024**3):
            with self._patch_cgroup_open('', raise_fnf=True):
                result = watchdog.check_inbox_watcher_cgroup()
        self.assertEqual(result['status'], 'unavailable')

    def test_below_warn_ratio_returns_ok(self):
        # 500 MB / 2 GB = 25%
        with mock.patch.object(watchdog, '_read_memory_max',
                               return_value=2 * 1024**3):
            with self._patch_cgroup_open('524288000'):
                with mock.patch.object(watchdog, '_inbox_watcher_main_pid',
                                       return_value=1234):
                    with mock.patch.object(watchdog, '_read_process_rss',
                                           return_value=100 * 1024 * 1024):
                        result = watchdog.check_inbox_watcher_cgroup()
        self.assertEqual(result['status'], 'ok')
        self.assertLess(result['ratio'], 0.5)

    def test_warn_ratio_appends_warning(self):
        # 1.7 GB / 2 GB = 85%
        with mock.patch.object(watchdog, '_read_memory_max',
                               return_value=2 * 1024**3):
            with self._patch_cgroup_open(str(int(1.7 * 1024**3))):
                with mock.patch.object(watchdog, '_inbox_watcher_main_pid',
                                       return_value=1234):
                    with mock.patch.object(watchdog, '_read_process_rss',
                                           return_value=200 * 1024 * 1024):
                        result = watchdog.check_inbox_watcher_cgroup()
        self.assertEqual(result['status'], 'warning')
        contents = larry_alerts.ALERTS_FILE.read_text()
        self.assertIn('warning', contents)
        self.assertIn('inbox-watcher cgroup memory warning', contents)

    def test_critical_ratio_appends_critical(self):
        # 1.95 GB / 2 GB = 97.5%
        with mock.patch.object(watchdog, '_read_memory_max',
                               return_value=2 * 1024**3):
            with self._patch_cgroup_open(str(int(1.95 * 1024**3))):
                with mock.patch.object(watchdog, '_inbox_watcher_main_pid',
                                       return_value=1234):
                    with mock.patch.object(watchdog, '_read_process_rss',
                                           return_value=200 * 1024 * 1024):
                        result = watchdog.check_inbox_watcher_cgroup()
        self.assertEqual(result['status'], 'critical')
        contents = larry_alerts.ALERTS_FILE.read_text()
        self.assertIn('critical', contents)
        self.assertIn('CRITICAL', contents)

    def test_payload_includes_parent_and_children_breakdown(self):
        with mock.patch.object(watchdog, '_read_memory_max',
                               return_value=2 * 1024**3):
            with self._patch_cgroup_open(str(int(0.5 * 1024**3))):
                with mock.patch.object(watchdog, '_inbox_watcher_main_pid',
                                       return_value=1234):
                    with mock.patch.object(watchdog, '_read_process_rss',
                                           return_value=100 * 1024 * 1024):
                        result = watchdog.check_inbox_watcher_cgroup()
        self.assertIn('parent_rss_mb', result)
        self.assertIn('children_mb', result)


# ---------- disk + memory ----------


class CheckDiskTest(_IsolatedRootsTest):
    def _statvfs_stub(self, used_pct):
        stat = mock.MagicMock()
        stat.f_blocks = 1000
        stat.f_frsize = 4096
        stat.f_bavail = int(1000 * (1 - used_pct / 100))
        return stat

    def test_normal_returns_ok(self):
        with mock.patch.object(watchdog.os, 'statvfs',
                               return_value=self._statvfs_stub(50)):
            result = watchdog.check_disk()
        self.assertEqual(result['status'], 'ok')

    def test_warn_appends_warning_alert(self):
        with mock.patch.object(watchdog.os, 'statvfs',
                               return_value=self._statvfs_stub(85)):
            result = watchdog.check_disk()
        self.assertEqual(result['status'], 'warning')
        self.assertIn('warning', larry_alerts.ALERTS_FILE.read_text())

    def test_critical_appends_critical_alert(self):
        with mock.patch.object(watchdog.os, 'statvfs',
                               return_value=self._statvfs_stub(95)):
            result = watchdog.check_disk()
        self.assertEqual(result['status'], 'critical')
        self.assertIn('critical', larry_alerts.ALERTS_FILE.read_text())


class CheckMemoryTest(_IsolatedRootsTest):
    def _meminfo(self, used_pct):
        total = 8 * 1024 * 1024  # kB → 8 GB
        available = int(total * (1 - used_pct / 100))
        return f'MemTotal:    {total} kB\nMemAvailable: {available} kB\n'

    def _patch_meminfo_open(self, meminfo: str):
        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path) == '/proc/meminfo':
                return mock.mock_open(read_data=meminfo).return_value
            return real_open(path, *args, **kwargs)

        return mock.patch('builtins.open', side_effect=fake_open)

    def test_normal_returns_ok(self):
        with self._patch_meminfo_open(self._meminfo(50)):
            result = watchdog.check_memory()
        self.assertEqual(result['status'], 'ok')

    def test_warn_appends_warning(self):
        with self._patch_meminfo_open(self._meminfo(85)):
            result = watchdog.check_memory()
        self.assertEqual(result['status'], 'warning')
        self.assertIn('warning', larry_alerts.ALERTS_FILE.read_text())


# ---------- log growth ----------


class CheckLogGrowthTest(_IsolatedRootsTest):
    def test_missing_log_returns_warning(self):
        result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')

    def test_recent_log_returns_ok(self):
        log_dir = self._tmp_path / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / 'inbox_watcher.log').write_text('hello\n')
        result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'ok')

    def test_stale_log_with_watcher_down_is_critical(self):
        log_dir = self._tmp_path / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        f = log_dir / 'inbox_watcher.log'
        f.write_text('old\n')
        # Force age > 5 min.
        import os as _os
        _os.utime(f, (1, 1))
        with mock.patch.object(watchdog, 'is_service_alive', return_value=False):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'critical')

    # ---- in-flight-aware inbox counting ----

    def _stale_log(self):
        log_dir = self._tmp_path / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        f = log_dir / 'inbox_watcher.log'
        f.write_text('old\n')
        import os as _os
        import time as _time
        stale = _time.time() - 600  # age > 5 min, well under 12h
        _os.utime(f, (stale, stale))

    def _very_stale_log(self):
        # Age well past the retired 12h backstop (43200s) — mtime = now-50000.
        log_dir = self._tmp_path / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        f = log_dir / 'inbox_watcher.log'
        f.write_text('old\n')
        import os as _os
        import time as _time
        stale = _time.time() - 50000  # ~13.9h > 12h
        _os.utime(f, (stale, stale))

    def _inbox_task(self, agent: str, task_id: str):
        inbox = self._tmp_path / 'inboxes' / agent
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f'build-{task_id}.json').write_text(
            json.dumps({'task_id': task_id})
        )

    def _in_flight_marker(self, task_id: str, pid: int):
        d = self._tmp_path / 'state' / 'in-flight'
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{task_id}.json').write_text(
            json.dumps({'task_stem': task_id, 'pid': pid})
        )

    def test_in_flight_alive_task_is_ok_no_warn(self):
        # Inbox task that is actively building (marker + live pid) -> ok.
        self._stale_log()
        self._inbox_task('forge', 'task-build-001')
        # Real live pid rather than a fake one + a _pid_alive patch: liveness now
        # lives in agent_work_in_flight, and mocking watchdog's private copy would
        # no longer reach it (PR 1b). os.getpid() is unambiguously alive.
        self._in_flight_marker('task-build-001', pid=os.getpid())
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, 'log') as logged:
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'ok')
        self.assertIn('active agent session', result['reason'])
        for call in logged.call_args_list:
            self.assertNotIn('Watcher log stale', call.args[0])

    def test_live_session_suppresses_other_inbox_queue(self):
        # Live in-flight session in inbox A globally suppresses even a genuine
        # non-in-flight queued task sitting in inbox B.
        self._stale_log()
        self._inbox_task('forge', 'task-build-001')
        self._in_flight_marker('task-build-001', pid=os.getpid())
        self._inbox_task('mirror', 'task-queued-elsewhere')
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, 'log') as logged:
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'ok')
        self.assertIn('active agent session', result['reason'])
        for call in logged.call_args_list:
            self.assertNotIn('Watcher log stale', call.args[0])

    def test_marker_error_only_inbox_is_ok(self):
        # No live session; the only inbox file is an outbox-notifier dead-letter.
        self._stale_log()
        inbox = self._tmp_path / 'inboxes' / 'forge'
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / 'marker-error-deadletter.json').write_text(
            json.dumps({'task_id': 'task-dead-letter'})
        )
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, 'log') as logged:
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'ok')
        for call in logged.call_args_list:
            self.assertNotIn('Watcher log stale', call.args[0])

    def test_genuine_build_no_session_still_warns(self):
        # No live session, healthy watcher, fresh build envelope -> real stall.
        self._stale_log()
        self._inbox_task('forge', 'task-build-fresh')
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['queued_inboxes'], 1)

    def test_queued_task_no_marker_still_warns(self):
        # Inbox task with NO in-flight marker -> genuinely queued -> warn.
        self._stale_log()
        self._inbox_task('forge', 'task-queued-002')
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['queued_inboxes'], 1)

    def test_in_flight_dead_pid_counts_as_queued_and_warns(self):
        # Marker present but pid dead -> not in-flight -> counts as queued.
        self._stale_log()
        self._inbox_task('forge', 'task-dead-003')
        self._in_flight_marker('task-dead-003', pid=999999)
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, '_pid_alive', return_value=False):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['queued_inboxes'], 1)

    # ---- dispatch-lease suppression (active Mirror review) ----

    def _dispatch_lease(self, agent: str, pid: int, age_seconds: float = 0.0,
                        slot: int = 0):
        import time as _time
        d = self._tmp_path / 'state' / 'dispatch-leases'
        d.mkdir(parents=True, exist_ok=True)
        # Mirrors inbox_watcher._slot_identity: slot 0 keeps the legacy
        # `inbox:<agent>` spelling, higher slots suffix `:<n>`.
        identity = f'inbox:{agent}' if slot == 0 else f'inbox:{agent}:{slot}'
        # Real lease filenames carry the literal colon (inbox:<agent>.lease) —
        # _safe_identity only rewrites '/' and '..', not ':'.
        (d / f'{identity}.lease').write_text(json.dumps({
            'identity': identity,
            'holder_pid': pid,
            'timestamp_renewed': _time.time() - age_seconds,
        }))

    def test_active_mirror_review_lease_suppresses_warn(self):
        # The Mirror-active scenario the fix targets: Mirror's review-request
        # envelope sits in its inbox AND inbox:mirror is held by a live pid,
        # but there is NO in-flight marker (the shared-task_stem clobber deleted
        # it). The dispatch lease is the suppression signal -> ok, no WARN.
        self._stale_log()
        self._inbox_task('mirror', 'pr-ourliberty-agent-core-713')
        self._dispatch_lease('mirror', pid=os.getpid())
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, 'log') as logged:
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'ok')
        self.assertIn('dispatch lease', result['reason'])
        for call in logged.call_args_list:
            self.assertNotIn('Watcher log stale', call.args[0])

    def test_active_slot1_review_lease_suppresses_warn(self):
        # REGRESSION (PR 1b, spec correction #995): higher review slots hold
        # `inbox:mirror:<n>` leases, and the previous implementation opened only
        # the slot-0 spelling — so a live review in Mirror slot 1 read as NO
        # session and watchdog could call a busy watcher stalled. Mirror runs two
        # slots and #971's killed review was in slot 1 precisely.
        self._stale_log()
        self._inbox_task('mirror', 'pr-ourliberty-agent-core-971')
        self._dispatch_lease('mirror', pid=os.getpid(), slot=1)
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, 'log') as logged:
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'ok')
        self.assertIn('dispatch lease', result['reason'])
        for call in logged.call_args_list:
            self.assertNotIn('Watcher log stale', call.args[0])

    def test_expired_slot1_lease_does_not_suppress(self):
        # Guardrail on the widened glob: slot-awareness must not weaken the TTL
        # test — a stale slot-1 lease is not a live session.
        self._stale_log()
        self._inbox_task('mirror', 'pr-ourliberty-agent-core-971')
        self._dispatch_lease('mirror', pid=os.getpid(), slot=1,
                             age_seconds=dispatch_lease.TTL_SECONDS + 5)
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')

    def test_mirror_review_envelope_no_session_still_warns(self):
        # Guardrail: a review-request envelope with NO held lease and NO marker
        # is a genuinely stalled review (not an active session) -> must warn.
        self._stale_log()
        self._inbox_task('mirror', 'pr-ourliberty-agent-core-713')
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['queued_inboxes'], 1)

    def test_stale_dispatch_lease_does_not_suppress(self):
        # Lease present but past TTL (holder died without releasing) -> not a
        # live session -> the queued review still warns.
        self._stale_log()
        self._inbox_task('mirror', 'pr-ourliberty-agent-core-713')
        self._dispatch_lease('mirror', pid=4242,
                             age_seconds=dispatch_lease.TTL_SECONDS + 60)
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, '_pid_alive', return_value=True):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['queued_inboxes'], 1)

    def test_dispatch_lease_dead_holder_does_not_suppress(self):
        # Lease fresh but holder pid dead -> not a live session -> warn.
        self._stale_log()
        self._inbox_task('mirror', 'pr-ourliberty-agent-core-713')
        self._dispatch_lease('mirror', pid=999999)
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, '_pid_alive', return_value=False):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['queued_inboxes'], 1)

    # ---- process-alive gate past the retired 12h backstop ----

    def test_idle_over_12h_alive_watcher_is_ok(self):
        # G-rule regression guard (watchdog-log-growth-idle-overnight-001):
        # a >12h-stale log with a live watcher and empty inboxes is expected
        # overnight idle, not a stall -> ok, and must NOT flip to warning.
        self._very_stale_log()
        # An inboxes root that exists but is empty -> live_files == 0.
        (self._tmp_path / 'inboxes' / 'forge').mkdir(parents=True, exist_ok=True)
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True), \
                mock.patch.object(watchdog, 'log') as logged:
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'ok')
        self.assertIn('idle', result['reason'])
        for call in logged.call_args_list:
            self.assertNotIn('Watcher log stale', call.args[0])

    def test_queued_work_over_12h_alive_watcher_still_warns(self):
        # Guardrail: the stall path must NOT be blinded past 12h. A >12h-stale
        # log with a genuinely-queued (non-in-flight) task still warns.
        self._very_stale_log()
        self._inbox_task('forge', 'task-queued-over-12h')
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True):
            result = watchdog.check_log_growth()
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['queued_inboxes'], 1)


# ---------- bot desired-state reconciler ----------


def _stub_policy(*agents: str, desired: dict | None = None) -> dict:
    """Build a minimal in-memory policy with the named agents."""
    desired = desired or {}
    policy: dict = {'_schema': {'version': 2}}
    for a in agents:
        policy[a] = {
            'mode': 'systemd',
            'desired_state': desired.get(a, 'up'),
            'systemd_unit': f'ourliberty-{a}-bot.service',
        }
    return policy


class ReconcileBotDesiredStateTest(_IsolatedRootsTest):
    """Brief §8 unit tests: branch table for the reconciler."""

    def _patch(self, *, policy: dict, alive_for: dict[str, bool],
               restart_succeeds: bool = True):
        """Patch policy load, is_service_alive, and _attempt_restart.

        alive_for maps unit-name -> bool. _attempt_restart returns
        restart_succeeds (and we capture which units were restarted).
        """
        self.restarted: list[str] = []

        def fake_restart(unit, wait_sec=5):
            self.restarted.append(unit)
            return restart_succeeds

        return [
            mock.patch.object(watchdog.bot_liveness_policy, 'load_policy',
                              return_value=policy),
            mock.patch.object(watchdog, 'is_service_alive',
                              side_effect=lambda u: alive_for.get(u, False)),
            mock.patch.object(watchdog, '_attempt_restart', side_effect=fake_restart),
        ]

    def _run(self, patches):
        for p in patches:
            p.start()
        try:
            return watchdog.reconcile_bot_desired_state()
        finally:
            for p in patches:
                p.stop()

    # --- branch: up + alive → no-op ---

    def test_up_alive_is_noop(self):
        policy = _stub_policy('forge')
        result = self._run(self._patch(
            policy=policy, alive_for={'ourliberty-forge-bot.service': True},
        ))
        self.assertEqual(result['bots']['forge']['action'], 'noop')
        self.assertEqual(self.restarted, [])
        # No alert emitted.
        self.assertFalse(larry_alerts.ALERTS_FILE.exists())

    # --- branch: up + down → restart attempted ---

    def test_up_down_triggers_restart(self):
        policy = _stub_policy('forge')
        result = self._run(self._patch(
            policy=policy, alive_for={'ourliberty-forge-bot.service': False},
        ))
        self.assertEqual(result['bots']['forge']['action'], 'restarted')
        self.assertEqual(self.restarted, ['ourliberty-forge-bot.service'])
        # Recovery alert is a 'warning' on the recovered subject.
        contents = larry_alerts.ALERTS_FILE.read_text()
        self.assertIn('bots:forge:recovered', contents)
        self.assertIn('"warning"', contents)

    def test_up_down_restart_fails_emits_critical(self):
        policy = _stub_policy('forge')
        result = self._run(self._patch(
            policy=policy,
            alive_for={'ourliberty-forge-bot.service': False},
            restart_succeeds=False,
        ))
        self.assertEqual(result['bots']['forge']['action'], 'restart_failed')
        contents = larry_alerts.ALERTS_FILE.read_text()
        self.assertIn('"critical"', contents)
        self.assertIn('bots:forge:down', contents)

    # --- branch: down + down → suppressed (NEVER restart intended-down bots) ---

    def test_down_down_is_suppressed(self):
        policy = _stub_policy('forge', desired={'forge': 'down'})
        result = self._run(self._patch(
            policy=policy, alive_for={'ourliberty-forge-bot.service': False},
        ))
        self.assertEqual(result['bots']['forge']['action'], 'suppressed')
        # NEVER restart an intended-down bot.
        self.assertEqual(self.restarted, [])
        # Down-alert is suppressed for intended-down bots — no alert file written.
        self.assertFalse(larry_alerts.ALERTS_FILE.exists())

    # --- branch: down + alive → divergence INFO, no actuation ---

    def test_down_alive_logs_divergence_no_actuation(self):
        policy = _stub_policy('forge', desired={'forge': 'down'})
        result = self._run(self._patch(
            policy=policy, alive_for={'ourliberty-forge-bot.service': True},
        ))
        self.assertEqual(result['bots']['forge']['action'], 'divergence')
        # Reconciler does NOT enforce shutdown.
        self.assertEqual(self.restarted, [])
        # And does not page Larry — divergence is a watchdog.log INFO.
        self.assertFalse(larry_alerts.ALERTS_FILE.exists())

    # --- flapping cap: M+1 down ticks → actuation halts, pages once ---

    def test_flapping_cap_halts_actuation_and_pages_once(self):
        policy = _stub_policy('forge')
        # Three consecutive ticks attempt restart (each fails → still down).
        for _ in range(watchdog.RECONCILE_MAX_ATTEMPTS):
            self._run(self._patch(
                policy=policy,
                alive_for={'ourliberty-forge-bot.service': False},
                restart_succeeds=False,
            ))
        # Marker now records M attempts in current window.
        marker = watchdog._reconcile_marker_path('forge')
        self.assertTrue(marker.exists())
        state = json.loads(marker.read_text())
        self.assertEqual(state['count'], watchdog.RECONCILE_MAX_ATTEMPTS)

        # Reset alerts to isolate the flapping-tick output.
        larry_alerts.ALERTS_FILE.write_text('')
        # 4th tick: still down → should NOT actuate, should page flapping.
        result = self._run(self._patch(
            policy=policy,
            alive_for={'ourliberty-forge-bot.service': False},
            restart_succeeds=False,
        ))
        self.assertEqual(result['bots']['forge']['action'], 'flapping')
        self.assertEqual(self.restarted, [], 'must NOT restart past the M-cap')
        contents = larry_alerts.ALERTS_FILE.read_text()
        self.assertIn('bot-reconcile-flapping:forge', contents)
        self.assertIn('"critical"', contents)

        # 5th tick (still down): paged flag set → no further alert.
        larry_alerts.ALERTS_FILE.write_text('')
        # Also clear larry_alerts cooldown so a re-emit WOULD reach the file
        # — proves suppression is from the marker, not the alert cooldown.
        import shutil
        if larry_alerts.COOLDOWN_ROOT.exists():
            shutil.rmtree(larry_alerts.COOLDOWN_ROOT)
        self._run(self._patch(
            policy=policy,
            alive_for={'ourliberty-forge-bot.service': False},
            restart_succeeds=False,
        ))
        self.assertEqual(self.restarted, [])
        # No second flapping page in the same window.
        post = larry_alerts.ALERTS_FILE.read_text() if larry_alerts.ALERTS_FILE.exists() else ''
        self.assertNotIn('bot-reconcile-flapping', post)

    def test_recovery_clears_marker(self):
        # A bot that comes back alive should reset its flapping window so
        # a later outage gets a fresh M-attempt budget.
        policy = _stub_policy('forge')
        # Tick 1: down, restart fails.
        self._run(self._patch(
            policy=policy,
            alive_for={'ourliberty-forge-bot.service': False},
            restart_succeeds=False,
        ))
        marker = watchdog._reconcile_marker_path('forge')
        self.assertTrue(marker.exists())
        # Tick 2: now alive.
        self._run(self._patch(
            policy=policy, alive_for={'ourliberty-forge-bot.service': True},
        ))
        self.assertFalse(marker.exists(), 'recovery must clear flapping marker')

    # --- beacon: reconciled by the unified path, no double-restart ---

    def test_beacon_reconciled_by_unified_path_no_double_restart(self):
        # Mirror: beacon is now driven by the reconciler alone — the
        # bespoke `check_beacon_bot` carve-out is retired so the two
        # paths cannot race to restart it.
        self.assertFalse(hasattr(watchdog, 'check_beacon_bot'),
                         'check_beacon_bot carve-out must be retired')
        self.assertNotIn('ourliberty-beacon-bot', watchdog.AUTO_RESTART_SERVICES)

        policy = _stub_policy('beacon')
        result = self._run(self._patch(
            policy=policy, alive_for={'ourliberty-beacon-bot.service': False},
        ))
        # Reconciler restarted beacon exactly once.
        self.assertEqual(self.restarted, ['ourliberty-beacon-bot.service'])
        self.assertEqual(result['bots']['beacon']['action'], 'restarted')

    # --- inbox-watcher / outbox-notifier paths NOT regressed ---

    def test_existing_inbox_watcher_carve_out_preserved(self):
        # The reconciler must not subsume the inbox-watcher / outbox-
        # notifier auto-restart paths (those are services, not bots).
        self.assertIn('ourliberty-inbox-watcher', watchdog.AUTO_RESTART_SERVICES)
        self.assertIn('ourliberty-outbox-notifier', watchdog.AUTO_RESTART_SERVICES)


# ---------- orchestration ----------


class RunAllChecksTest(_IsolatedRootsTest):
    def test_healthy_when_all_checks_ok(self):
        ok_result = {'status': 'ok'}
        with mock.patch.multiple(
            watchdog,
            check_inbox_watcher=mock.MagicMock(return_value=ok_result),
            check_outbox_notifier=mock.MagicMock(return_value=ok_result),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok_result),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=ok_result),
            check_disk=mock.MagicMock(return_value=ok_result),
            check_memory=mock.MagicMock(return_value=ok_result),
            check_log_growth=mock.MagicMock(return_value=ok_result),
            check_orphaned_journalctl_followers=mock.MagicMock(return_value=ok_result),
            reconcile_bot_desired_state=mock.MagicMock(return_value=ok_result),
        ):
            results = watchdog.run_all_checks()
        self.assertEqual(results['overall'], 'healthy')
        self.assertEqual(len(results['checks']), 9)

    def test_warning_when_any_check_warning(self):
        ok = {'status': 'ok'}
        warn = {'status': 'warning'}
        with mock.patch.multiple(
            watchdog,
            check_inbox_watcher=mock.MagicMock(return_value=ok),
            check_outbox_notifier=mock.MagicMock(return_value=ok),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=warn),
            check_disk=mock.MagicMock(return_value=ok),
            check_memory=mock.MagicMock(return_value=ok),
            check_log_growth=mock.MagicMock(return_value=ok),
            check_orphaned_journalctl_followers=mock.MagicMock(return_value=ok),
            reconcile_bot_desired_state=mock.MagicMock(return_value=ok),
        ):
            results = watchdog.run_all_checks()
        self.assertEqual(results['overall'], 'warning')

    def test_critical_when_any_check_critical(self):
        ok = {'status': 'ok'}
        crit = {'status': 'critical'}
        with mock.patch.multiple(
            watchdog,
            check_inbox_watcher=mock.MagicMock(return_value=ok),
            check_outbox_notifier=mock.MagicMock(return_value=ok),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=ok),
            check_disk=mock.MagicMock(return_value=crit),
            check_memory=mock.MagicMock(return_value=ok),
            check_log_growth=mock.MagicMock(return_value=ok),
            check_orphaned_journalctl_followers=mock.MagicMock(return_value=ok),
            reconcile_bot_desired_state=mock.MagicMock(return_value=ok),
        ):
            results = watchdog.run_all_checks()
        self.assertEqual(results['overall'], 'critical')

    def test_health_file_written(self):
        ok = {'status': 'ok'}
        with mock.patch.multiple(
            watchdog,
            check_inbox_watcher=mock.MagicMock(return_value=ok),
            check_outbox_notifier=mock.MagicMock(return_value=ok),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=ok),
            check_disk=mock.MagicMock(return_value=ok),
            check_memory=mock.MagicMock(return_value=ok),
            check_log_growth=mock.MagicMock(return_value=ok),
            check_orphaned_journalctl_followers=mock.MagicMock(return_value=ok),
            reconcile_bot_desired_state=mock.MagicMock(return_value=ok),
        ):
            watchdog.run_all_checks()
        self.assertTrue(watchdog.HEALTH_FILE.exists())
        data = json.loads(watchdog.HEALTH_FILE.read_text())
        self.assertEqual(data['overall'], 'healthy')


def _proc(pid: int, ppid: int, etimes: int, args: str) -> dict:
    return {'pid': pid, 'ppid': ppid, 'etimes': etimes, 'args': args}


class OrphanedJournalctlReaperTest(_IsolatedRootsTest):
    """check_orphaned_journalctl_followers — reap init-reparented follow leaks."""

    AGED = watchdog.ORPHAN_FOLLOWER_MIN_AGE_SEC + 3600
    YOUNG = watchdog.ORPHAN_FOLLOWER_MIN_AGE_SEC - 3600

    _WRAPPER_ARGS = (
        "bash -c journalctl -fu ourliberty-inbox-watcher.service "
        "--since '1 minute ago' --no-pager 2>&1 | grep --line-buffered -E pr-104"
    )
    _CHILD_ARGS = (
        "journalctl -fu ourliberty-inbox-watcher.service "
        "--since 1 minute ago --no-pager"
    )

    def _run(self, procs):
        """Run the reaper over a fake process table; return (result, kill_calls)."""
        with mock.patch.object(watchdog, '_enumerate_processes',
                               return_value=procs), \
             mock.patch.object(watchdog.os, 'kill') as mkill, \
             mock.patch.object(watchdog.larry_alerts, 'append_alert') as malert:
            result = watchdog.check_orphaned_journalctl_followers()
        pids = sorted(c.args[0] for c in mkill.call_args_list)
        # Every kill must be SIGTERM.
        for c in mkill.call_args_list:
            self.assertEqual(c.args[1], signal.SIGTERM)
        # Benign self-heal never pages Larry.
        malert.assert_not_called()
        return result, pids

    def test_reaps_wrapper_and_child(self):
        procs = [
            _proc(100, 1, self.AGED, self._WRAPPER_ARGS),
            _proc(101, 100, self.AGED, self._CHILD_ARGS),
            _proc(9999, 1234, 50, 'some-unrelated-process'),
        ]
        result, pids = self._run(procs)
        self.assertEqual(pids, [100, 101])       # whole subtree reaped
        self.assertEqual(result['status'], 'recovered')
        self.assertEqual(result['reaped'], 2)
        self.assertEqual(result['roots'], 1)

    def test_ignores_daemon_owned_follower(self):
        # chain_event_shipper (alive, pid 200) owns its journalctl child (201).
        procs = [
            _proc(200, 1, self.AGED,
                  '/usr/bin/python3 scripts/chain_event_shipper.py'),
            _proc(201, 200, self.AGED,
                  'journalctl -fu ourliberty-inbox-watcher.service --output=json'),
        ]
        result, pids = self._run(procs)
        self.assertEqual(pids, [])               # child's parent is alive, not init
        self.assertEqual(result['reaped'], 0)
        self.assertEqual(result['status'], 'ok')

    def test_ignores_young_orphan(self):
        procs = [_proc(100, 1, self.YOUNG, self._WRAPPER_ARGS)]
        result, pids = self._run(procs)
        self.assertEqual(pids, [])
        self.assertEqual(result['reaped'], 0)

    def test_ignores_non_follow_journalctl(self):
        # Orphaned + aged, but no follow flag → not a leak this reaper owns.
        procs = [_proc(100, 1, self.AGED,
                       'journalctl -u ourliberty-inbox-watcher.service -n 50')]
        result, pids = self._run(procs)
        self.assertEqual(pids, [])
        self.assertEqual(result['reaped'], 0)

    def test_grep_f_after_pipe_does_not_false_match(self):
        # journalctl has NO follow flag; the `-f` belongs to grep past the pipe.
        args = "bash -c journalctl -u foo.service --no-pager | grep -f /tmp/patterns"
        procs = [_proc(100, 1, self.AGED, args)]
        result, pids = self._run(procs)
        self.assertEqual(pids, [])
        self.assertEqual(result['reaped'], 0)

    def test_chained_command_f_flag_does_not_false_match(self):
        # A non-follow journalctl chained (via ; && &) to a *different* command
        # whose own -f flag must NOT be misattributed to journalctl. Guards the
        # shell-separator boundary in _JOURNALCTL_FOLLOW_RE.
        for args in (
            "bash -c journalctl -u foo -n5 ; tail -f /var/log/x",
            "bash -c journalctl -u foo && tail -f /var/log/x",
            "bash -c journalctl -u foo; grep -f pat file",
            "journalctl -u foo -o cat -n 200 && systemctl -f restart x",
        ):
            with self.subTest(args=args):
                result, pids = self._run([_proc(100, 1, self.AGED, args)])
                self.assertEqual(pids, [])
                self.assertEqual(result['reaped'], 0)

    def test_real_leaked_wrapper_with_redirect_still_matches(self):
        # The actual observed leak has `2>&1` (contains &) before the pipe; the
        # follow flag precedes it, so the lazy scan must still match.
        args = (
            "bash -c journalctl -fu ourliberty-inbox-watcher.service "
            "--since '1 minute ago' --no-pager 2>&1 | grep --line-buffered -E pr-104"
        )
        result, pids = self._run([_proc(100, 1, self.AGED, args)])
        self.assertEqual(pids, [100])
        self.assertEqual(result['reaped'], 1)

    def test_reaps_bare_backgrounded_follower(self):
        procs = [_proc(300, 1, self.AGED,
                       'journalctl --follow -u ourliberty-inbox-watcher.service')]
        result, pids = self._run(procs)
        self.assertEqual(pids, [300])
        self.assertEqual(result['roots'], 1)
        self.assertEqual(result['reaped'], 1)

    def test_process_lookup_error_is_tolerated(self):
        procs = [
            _proc(100, 1, self.AGED, self._WRAPPER_ARGS),
            _proc(101, 100, self.AGED, self._CHILD_ARGS),
        ]
        with mock.patch.object(watchdog, '_enumerate_processes',
                               return_value=procs), \
             mock.patch.object(watchdog.os, 'kill',
                               side_effect=[ProcessLookupError(), None]), \
             mock.patch.object(watchdog.larry_alerts, 'append_alert'):
            result = watchdog.check_orphaned_journalctl_followers()
        # One pid vanished mid-reap; the run still succeeds and counts the rest.
        self.assertEqual(result['status'], 'recovered')
        self.assertEqual(result['reaped'], 1)

    def test_empty_process_table_is_noop(self):
        result, pids = self._run([])
        self.assertEqual(pids, [])
        self.assertEqual(result, {'status': 'ok', 'reaped': 0})

    def test_enumerate_processes_parses_ps_output(self):
        sample = (
            "  100     1  86400 bash -c journalctl -fu unit | grep x\n"
            "  101   100  86400 journalctl -fu unit\n"
            "bad line without enough fields\n"
        )
        with mock.patch.object(watchdog.subprocess, 'run',
                               return_value=mock.MagicMock(stdout=sample)):
            procs = watchdog._enumerate_processes()
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0]['pid'], 100)
        self.assertEqual(procs[0]['ppid'], 1)
        self.assertEqual(procs[0]['etimes'], 86400)
        self.assertTrue(procs[1]['args'].startswith('journalctl -fu unit'))


# ---------- constants sanity ----------


class TopologyConstantsTest(unittest.TestCase):
    def test_auto_restart_covers_only_services_not_bots(self):
        # Bots are reconciled by reconcile_bot_desired_state(); the
        # AUTO_RESTART_SERVICES tuple is for non-bot daemons only.
        self.assertEqual(
            set(watchdog.AUTO_RESTART_SERVICES),
            {'ourliberty-inbox-watcher', 'ourliberty-outbox-notifier'},
        )

    def test_beacon_carve_out_retired(self):
        # The bespoke check_beacon_bot path is gone; reconciler owns it.
        self.assertFalse(hasattr(watchdog, 'check_beacon_bot'))
        self.assertNotIn('ourliberty-beacon-bot', watchdog.AUTO_RESTART_SERVICES)


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
