"""Cordon-and-drain in heal_stale_daemon_code (PR 2 of the restart-inflight
guard; spec docs/restart-inflight-guard-spec.md, #981/#989/#995).

This is the PRODUCER: the first code that writes a cordon against the live
system. Its failure mode is a silent full-queue stall, so the stall cases are
tested before the happy path.

Coverage:
- RENEWAL (the flaw the adversarial pass caught): a drain longer than
  CORDON_TTL_SEC keeps the cordon continuously live; without renewal the
  healer's own cordon would expire mid-drain, the watcher would resume taking
  work, and the restart would kill a live session — the bug this prevents.
- release on every path: success, ceiling, and an exception mid-drain
- scope: only AGENT_HOSTING_UNITS cordon; everything else restarts immediately
- the ceiling backstop, and its loud log
- cordon-write failure degrades to today's restart-now, never silent
- SEAM: producer and consumer resolve the SAME cordon path
"""

from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import agent_work_in_flight as awif  # noqa: E402
import heal_stale_daemon_code as h  # noqa: E402
import inbox_watcher as iw  # noqa: E402

AGENT_UNIT = 'ourliberty-inbox-watcher.service'
OTHER_UNIT = 'ourliberty-dashboard-api.service'


class CordonDrainFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ol-pr2-'))
        self._saved = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        (self.root / 'state' / 'restart-cordon').mkdir(parents=True, exist_ok=True)
        (self.root / 'logs').mkdir(parents=True, exist_ok=True)
        self._log = mock.patch.object(h, 'log')
        self.log = self._log.start()
        self.addCleanup(self._log.stop)
        # Never touch systemd from a test.
        self._restart = mock.patch.object(h, '_restart_unit_now',
                                          return_value=(0, ''))
        self.restart = self._restart.start()
        self.addCleanup(self._restart.stop)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._saved

    def _cordon(self):
        p = h._cordon_file(AGENT_UNIT)
        return json.loads(p.read_text()) if p.exists() else None

    def _logged(self, needle):
        return any(needle in str(c.args[0]) for c in self.log.call_args_list)


class RenewalTests(CordonDrainFixture):
    """The catch: CORDON_TTL_SEC is 600s but a review may run 2100s."""

    def test_drain_longer_than_ttl_keeps_cordon_live(self):
        # Simulate a drain that outlasts the TTL. The cordon must never be
        # observably expired at any point, or the watcher resumes taking work
        # mid-drain and the restart kills a live session.
        clock = {'t': 1_000_000.0}
        observed_expiry_gaps = []

        def fake_time():
            return clock['t']

        def fake_sleep(sec):
            clock['t'] += sec
            # Whatever the watcher would see right now:
            data = self._cordon()
            observed_expiry_gaps.append(data['expires_at'] - clock['t'])

        calls = {'n': 0}

        def work_in_flight(*a, **k):
            calls['n'] += 1
            # Busy for ~1200s of simulated time (2x the 600s TTL), then quiet.
            return calls['n'] * h.DRAIN_POLL_SEC < 1200

        with mock.patch.object(h.time, 'time', side_effect=fake_time), \
                mock.patch.object(h.time, 'sleep', side_effect=fake_sleep), \
                mock.patch.object(h.agent_work_in_flight, 'agent_work_in_flight',
                                  side_effect=work_in_flight):
            h.write_cordon(AGENT_UNIT, 'test')
            drained, waited = h.drain_agent_work(AGENT_UNIT, 'test')

        self.assertTrue(drained)
        self.assertGreater(waited, h.CORDON_TTL_SEC,
                           'drain must have outlasted the TTL for this to test anything')
        self.assertTrue(observed_expiry_gaps, 'no drain polls observed')
        # NEVER expired from the watcher's point of view.
        self.assertGreater(min(observed_expiry_gaps), 0,
                           'cordon expired mid-drain — the watcher would have '
                           'resumed taking work and the restart would kill it')

    def test_renew_interval_is_below_ttl(self):
        # Structural guard: renewal must be comfortably inside the TTL, or a
        # slow poll could let it lapse between renewals.
        self.assertLess(h.CORDON_RENEW_EVERY_SEC, h.CORDON_TTL_SEC)
        self.assertLessEqual(h.CORDON_RENEW_EVERY_SEC, h.CORDON_TTL_SEC / 2)

    def test_ceiling_exceeds_review_session_ceiling(self):
        # The backstop must never fire on a single legitimate review.
        import agent_runner
        self.assertGreater(h.RESTART_DEFER_CEILING_SEC,
                           agent_runner.REVIEW_SESSION_CEILING_SECONDS)


class ReleaseTests(CordonDrainFixture):
    """An un-cleared cordon stalls dispatch until its TTL."""

    def test_cordon_cleared_after_successful_drain(self):
        with mock.patch.object(h.agent_work_in_flight, 'agent_work_in_flight',
                               return_value=False):
            h.auto_restart_unit(AGENT_UNIT)
        self.assertIsNone(self._cordon())
        self.restart.assert_called_once()

    def test_cordon_cleared_when_ceiling_hit(self):
        with mock.patch.object(h.agent_work_in_flight, 'agent_work_in_flight',
                               return_value=True), \
                mock.patch.object(h, 'RESTART_DEFER_CEILING_SEC', 0):
            h.auto_restart_unit(AGENT_UNIT)
        self.assertIsNone(self._cordon())
        self.assertTrue(self._logged('RESTART_FORCED_OVER_CEILING'))
        self.restart.assert_called_once()

    def test_cordon_cleared_when_restart_raises(self):
        self.restart.side_effect = RuntimeError('systemctl exploded')
        with mock.patch.object(h.agent_work_in_flight, 'agent_work_in_flight',
                               return_value=False):
            with self.assertRaises(RuntimeError):
                h.auto_restart_unit(AGENT_UNIT)
        self.assertIsNone(self._cordon(),
                          'a cordon left behind by a crashing healer stalls dispatch')

    def test_cordon_cleared_when_drain_raises(self):
        with mock.patch.object(h, 'drain_agent_work',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                h.auto_restart_unit(AGENT_UNIT)
        self.assertIsNone(self._cordon())


class ScopeTests(CordonDrainFixture):
    def test_non_agent_unit_restarts_immediately(self):
        # A dashboard-api restart must not wait on a Mirror review.
        with mock.patch.object(h, 'drain_agent_work') as drain, \
                mock.patch.object(h.agent_work_in_flight, 'agent_work_in_flight',
                                  return_value=True):
            rc, _ = h.auto_restart_unit(OTHER_UNIT)
        self.assertEqual(rc, 0)
        drain.assert_not_called()
        self.assertIsNone(self._cordon())

    def test_agent_unit_waits_for_work(self):
        with mock.patch.object(h, 'drain_agent_work',
                               return_value=(True, 42.0)) as drain:
            h.auto_restart_unit(AGENT_UNIT)
        drain.assert_called_once()
        self.assertTrue(self._logged('RESTART_DRAINED'))


class FailOpenTests(CordonDrainFixture):
    def test_cordon_write_failure_degrades_to_restart_now(self):
        # An IO fault must not leave a stale daemon running forever; falling
        # back to today's behavior is no worse than today. But never silently.
        with mock.patch.object(h, 'write_cordon', return_value=False), \
                mock.patch.object(h, 'drain_agent_work') as drain:
            rc, _ = h.auto_restart_unit(AGENT_UNIT)
        self.assertEqual(rc, 0)
        drain.assert_not_called()
        self.assertTrue(self._logged('cordon unavailable'))


class SeamTests(CordonDrainFixture):
    """Producer and consumer must agree on the contract, or the guard silently
    does nothing."""

    def test_cordon_path_matches_watcher(self):
        self.assertEqual(h._cordon_file(AGENT_UNIT), iw._restart_cordon_file())

    def test_cordon_path_is_anchored_to_the_agents_root(self):
        # NOT tautological. Comparing the two callers to each other passes even
        # when the shared helper is broken — they agree on the WRONG path. This
        # pins the path to the configured root independently of either caller.
        path = h._cordon_file(AGENT_UNIT)
        self.assertEqual(path.parent, self.root / 'state' / 'restart-cordon')
        self.assertEqual(path.name, f'{AGENT_UNIT}.json')
        self.assertTrue(str(path).startswith(str(self.root)),
                        f'cordon escaped the agents root: {path}')

    def test_agent_hosting_units_matches_watcher_unit(self):
        self.assertEqual(h.AGENT_HOSTING_UNITS, frozenset({iw.CORDON_UNIT}))

    def test_written_cordon_is_honored_by_the_watcher(self):
        # End-to-end on the file contract: what the healer writes, the watcher
        # must read as an ACTIVE cordon. Pin the boot id on BOTH sides rather
        # than skipping on non-Linux — a skipped seam test is a seam test that
        # never runs on the machine where the code is written.
        fake_boot = '11111111-2222-3333-4444-555555555555'
        with mock.patch.object(h, '_boot_id', return_value=fake_boot), \
                mock.patch.object(iw, '_current_boot_id', return_value=fake_boot):
            h.write_cordon(AGENT_UNIT, 'stale-lib-restart')
            self.assertEqual(iw._restart_cordon_active(), 'stale-lib-restart')

    def test_cleared_cordon_is_not_honored(self):
        fake_boot = '11111111-2222-3333-4444-555555555555'
        with mock.patch.object(h, '_boot_id', return_value=fake_boot), \
                mock.patch.object(iw, '_current_boot_id', return_value=fake_boot):
            h.write_cordon(AGENT_UNIT, 'stale-lib-restart')
            h.clear_cordon(AGENT_UNIT)
            self.assertIsNone(iw._restart_cordon_active())


if __name__ == '__main__':
    unittest.main()
