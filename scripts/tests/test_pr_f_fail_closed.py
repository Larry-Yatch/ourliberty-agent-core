"""PR-F fail-closed regressions (2026-06-05 audit #5/#40/#42/#50/#56/#64).

#6 (concurrency_guard) and #21 (trust_policy) are covered in their own suites.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


class AwaitQuiescenceRunningAgentsTest(unittest.TestCase):
    """#5: has_running_agents reads the guard's own accounting, not a dead glob."""

    def _has_running(self, active_count_side):
        import await_quiescence as aq
        import concurrency_guard
        guard = mock.Mock()
        if isinstance(active_count_side, Exception):
            guard.active_count.side_effect = active_count_side
        else:
            guard.active_count.return_value = active_count_side
        with mock.patch.object(concurrency_guard, 'get_guard', return_value=guard):
            return aq.has_running_agents()

    def test_occupied_slots_means_running(self):
        self.assertTrue(self._has_running(2))

    def test_zero_slots_means_idle(self):
        self.assertFalse(self._has_running(0))

    def test_guard_error_fails_safe_to_running(self):
        # cannot read the guard → assume agents MIGHT be running (block sync)
        self.assertTrue(self._has_running(RuntimeError('boom')))


class ShippedPrNaiveSinceTsTest(unittest.TestCase):
    """#40: a naive since_ts must not raise TypeError out of shipped_pr."""

    def test_naive_since_ts_does_not_raise(self):
        import task_resolution as tr
        proc = mock.Mock(returncode=0,
                         stdout='[{"number":1,"title":"unrelated",'
                                '"headRefName":"forge/other",'
                                '"mergedAt":"2026-02-01T00:00:00Z"}]')
        with mock.patch.object(tr.subprocess, 'run', return_value=proc):
            # naive datetime — pre-fix this raised TypeError at the < compare
            out = tr.shipped_pr('chain-discipline-gap-001',
                                since_ts=datetime(2020, 1, 1),
                                repos=('owner/repo',))
        self.assertIsNone(out)  # PR doesn't match; key point: it returned, no raise


class HealRecoveryLogOSErrorTest(unittest.TestCase):
    """#42: log() must swallow OSError so a full/RO log FS can't crash the healer."""

    def test_log_swallows_oserror(self):
        import heal_recovery_already_merged as h
        with mock.patch('builtins.open', side_effect=OSError('disk full')):
            h.log('HEARTBEAT', 'should not raise')  # must not raise


class DropletDriftSoftErrorTest(unittest.TestCase):
    """#50: a git error after a successful fetch must not read as 'no drift'."""

    def test_ahead_count_none_on_git_error(self):
        import heal_droplet_git_drift as d
        with mock.patch.object(d, '_git', return_value=(1, '', 'fatal')):
            self.assertIsNone(d.ahead_count('origin/main'))
        with mock.patch.object(d, '_git', return_value=(0, '3\n', '')):
            self.assertEqual(d.ahead_count('origin/main'), 3)

    def test_behind_count_none_on_git_error(self):
        import heal_droplet_git_drift as d
        with mock.patch.object(d, '_git', return_value=(128, '', 'lock')):
            self.assertIsNone(d.behind_count('origin/main'))

    def test_evaluate_ahead_skips_on_none(self):
        import heal_droplet_git_drift as d
        with mock.patch.object(d, 'ahead_count', return_value=None), \
             mock.patch.object(d, 'log') as logm:
            self.assertFalse(d.evaluate_ahead('origin/main', 'main', {}))
        logm.assert_called()  # the soft error is surfaced, not silent


class DispatchDedupRecordTest(unittest.TestCase):
    """#56: record_dispatch reports write failure so main() can fail closed."""

    def test_record_returns_true_on_success(self):
        import dispatch_dedup_guard as g
        m = mock.mock_open()
        with mock.patch.object(type(g.LEDGER), 'open', m, create=True), \
             mock.patch.object(type(g.LEDGER), 'mkdir', create=True):
            self.assertTrue(g.record_dispatch('forge', 'abc', '/tmp/t.json'))

    def test_record_returns_false_on_oserror(self):
        import dispatch_dedup_guard as g
        with mock.patch.object(type(g.LEDGER), 'open',
                               side_effect=OSError('full'), create=True), \
             mock.patch.object(type(g.LEDGER), 'mkdir', create=True):
            self.assertFalse(g.record_dispatch('forge', 'abc', '/tmp/t.json'))


class PulseCheckIWeekEndingTest(unittest.TestCase):
    """#29: a malformed --week-ending exits cleanly (usage error), not a
    ValueError that escapes main() and mis-fires a pulse-check-failed alert."""

    def test_bad_week_ending_returns_usage_exit(self):
        import pulse_check_i as pci
        rc = pci.main(['--week-ending', 'not-a-date', '--force'])
        self.assertEqual(rc, 2)


class ClearVerifiedImportInertTest(unittest.TestCase):
    """#64: importing clear_verified must not connect/mutate Supabase."""

    def test_import_is_inert_and_exposes_main(self):
        import importlib
        # no SUPABASE_* env required just to import
        with mock.patch.dict('os.environ', {}, clear=False):
            mod = importlib.import_module('clear_verified')
        self.assertTrue(callable(mod.main))
        # destructive logic is gated: module body must not build a client
        self.assertFalse(hasattr(mod, 'c'),
                         'clear_verified built a Supabase client at import time')


if __name__ == '__main__':
    unittest.main()
