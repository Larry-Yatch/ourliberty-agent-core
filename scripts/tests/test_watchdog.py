#!/usr/bin/env python3
"""Tests for watchdog.py — adapted-for-our-topology system health monitor.

Phase D3.5-prep (2026-05-12). Covers each of the 9 checks, the auto-restart
machinery, the cgroup-aggregate check, and the systemd-state allow-list
(M1 fix: auto-restart SubState is treated as alive).

Run:
    python3 -m unittest scripts.tests.test_watchdog
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

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
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True):
            with mock.patch.object(watchdog, '_attempt_start') as mock_start:
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'ok')
        mock_start.assert_not_called()

    def test_dead_service_attempts_start_and_recovers(self):
        with mock.patch.object(watchdog, 'is_service_alive', return_value=False):
            with mock.patch.object(watchdog, '_attempt_start', return_value=True):
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'recovered')
        # Recovered alert should land in queue (warning severity).
        contents = larry_alerts.ALERTS_FILE.read_text().strip()
        self.assertIn('auto-restarted', contents)
        self.assertIn('"warning"', contents)

    def test_dead_service_start_fails_appends_critical(self):
        with mock.patch.object(watchdog, 'is_service_alive', return_value=False):
            with mock.patch.object(watchdog, '_attempt_start', return_value=False):
                result = watchdog._check_auto_restart('ourliberty-x', 'x')
        self.assertEqual(result['status'], 'down')
        contents = larry_alerts.ALERTS_FILE.read_text().strip()
        self.assertIn('auto-restart failed', contents)
        self.assertIn('"critical"', contents)
        record = json.loads(contents.splitlines()[-1])
        self.assertEqual(record['subject'], 'ourliberty-x')
        self.assertIn('sudo systemctl status', record['suggested_action'])


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


# ---------- bots ----------


class CheckBotsTest(_IsolatedRootsTest):
    def test_all_bots_alive_returns_ok(self):
        with mock.patch.object(watchdog, 'is_service_alive', return_value=True):
            result = watchdog.check_bots()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['down'], [])

    def test_one_bot_down_alerts_only_that_bot(self):
        def alive(svc):
            return 'forge' not in svc
        with mock.patch.object(watchdog, 'is_service_alive', side_effect=alive):
            result = watchdog.check_bots()
        self.assertEqual(result['status'], 'critical')
        self.assertEqual(result['down'], ['ourliberty-forge-bot'])
        contents = larry_alerts.ALERTS_FILE.read_text()
        self.assertIn('ourliberty-forge-bot', contents)
        # Subject should be subject-specific (M3 fix).
        record = json.loads(contents.strip().splitlines()[0])
        self.assertEqual(record['subject'], 'bots:forge')

    def test_two_bots_down_get_independent_subjects(self):
        # M3 fix: cooldown keys are per-bot.
        def alive(svc):
            return 'forge' not in svc and 'mirror' not in svc
        with mock.patch.object(watchdog, 'is_service_alive', side_effect=alive):
            watchdog.check_bots()
        lines = larry_alerts.ALERTS_FILE.read_text().strip().splitlines()
        subjects = {json.loads(l)['subject'] for l in lines}
        self.assertEqual(subjects, {'bots:forge', 'bots:mirror'})

    def test_beacon_bot_not_in_alert_only_set(self):
        # beacon-bot is the carve-out — should NOT appear in check_bots.
        self.assertNotIn('beacon', watchdog.ALERT_ONLY_BOTS)


# ---------- orchestration ----------


class RunAllChecksTest(_IsolatedRootsTest):
    def test_healthy_when_all_checks_ok(self):
        ok_result = {'status': 'ok'}
        with mock.patch.multiple(
            watchdog,
            check_inbox_watcher=mock.MagicMock(return_value=ok_result),
            check_outbox_notifier=mock.MagicMock(return_value=ok_result),
            check_beacon_bot=mock.MagicMock(return_value=ok_result),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok_result),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=ok_result),
            check_disk=mock.MagicMock(return_value=ok_result),
            check_memory=mock.MagicMock(return_value=ok_result),
            check_log_growth=mock.MagicMock(return_value=ok_result),
            check_bots=mock.MagicMock(return_value=ok_result),
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
            check_beacon_bot=mock.MagicMock(return_value=ok),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=warn),
            check_disk=mock.MagicMock(return_value=ok),
            check_memory=mock.MagicMock(return_value=ok),
            check_log_growth=mock.MagicMock(return_value=ok),
            check_bots=mock.MagicMock(return_value=ok),
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
            check_beacon_bot=mock.MagicMock(return_value=ok),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=ok),
            check_disk=mock.MagicMock(return_value=crit),
            check_memory=mock.MagicMock(return_value=ok),
            check_log_growth=mock.MagicMock(return_value=ok),
            check_bots=mock.MagicMock(return_value=ok),
        ):
            results = watchdog.run_all_checks()
        self.assertEqual(results['overall'], 'critical')

    def test_health_file_written(self):
        ok = {'status': 'ok'}
        with mock.patch.multiple(
            watchdog,
            check_inbox_watcher=mock.MagicMock(return_value=ok),
            check_outbox_notifier=mock.MagicMock(return_value=ok),
            check_beacon_bot=mock.MagicMock(return_value=ok),
            check_inbox_watcher_memory=mock.MagicMock(return_value=ok),
            check_inbox_watcher_cgroup=mock.MagicMock(return_value=ok),
            check_disk=mock.MagicMock(return_value=ok),
            check_memory=mock.MagicMock(return_value=ok),
            check_log_growth=mock.MagicMock(return_value=ok),
            check_bots=mock.MagicMock(return_value=ok),
        ):
            watchdog.run_all_checks()
        self.assertTrue(watchdog.HEALTH_FILE.exists())
        data = json.loads(watchdog.HEALTH_FILE.read_text())
        self.assertEqual(data['overall'], 'healthy')


# ---------- constants sanity ----------


class TopologyConstantsTest(unittest.TestCase):
    def test_auto_restart_includes_beacon_bot_carve_out(self):
        # C1 fix — beacon-bot must be auto-restarted.
        self.assertIn('ourliberty-beacon-bot', watchdog.AUTO_RESTART_SERVICES)

    def test_alert_only_set_does_not_include_beacon(self):
        self.assertNotIn('beacon', watchdog.ALERT_ONLY_BOTS)

    def test_alert_only_set_covers_three_other_bots(self):
        self.assertEqual(set(watchdog.ALERT_ONLY_BOTS),
                         {'forge', 'mirror', 'pulse'})


if __name__ == '__main__':
    unittest.main()
