#!/usr/bin/env python3
"""Tests for heal_systemd_install_drift (E1.5.2).

Covers repo/installed scan, drift detection (repo has unit, installed
dir does not), state dedup, dry-run activation pattern, and reconciliation GC.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_systemd_install_drift
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_systemd_install_drift as h  # noqa: E402
import larry_alerts as la  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test.

    Why: heal_systemd_install_drift's LOG_FILE / STATE_FILE / HEARTBEAT_FILE
    / KILL_SWITCH derive from AGENTS_ROOT at import time. Without this
    redirection, running tests in a worktree pollutes prod
    `/home/larry/agents/...` state. Reload the module so its module-level
    constants pick up the override.
    """

    def setUp(self):
        super().setUp()
        self._isolated_tmp = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
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


def _make_repo_systemd(td: Path, units: list[str]) -> Path:
    d = td / 'repo-systemd'
    d.mkdir()
    for u in units:
        (d / u).write_text(f'# {u}\n')
    return d


def _make_installed(td: Path, units: list[str]) -> Path:
    d = td / 'installed'
    d.mkdir()
    for u in units:
        (d / u).write_text(f'# {u}\n')
    return d


class ListUnitsTest(_IsolatedAgentsRoot):
    def test_lists_service_and_timer_files(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [
                'a.service', 'a.timer', 'README.md',
            ])
            self.assertEqual(h.list_repo_units(r), ['a.service', 'a.timer'])

    def test_installed_units_returns_set(self):
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['a.service'])
            self.assertEqual(h.list_installed_units(i), {'a.service'})

    def test_missing_repo_dir_returns_empty(self):
        self.assertEqual(h.list_repo_units(Path('/tmp/nope-xyzzy')), [])


class DetectDriftTest(_IsolatedAgentsRoot):
    def test_no_drift_when_all_installed(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.service', 'a.timer'])
            i = _make_installed(Path(td), ['a.service', 'a.timer'])
            self.assertEqual(h.detect_drift(r, i), [])

    def test_drift_when_repo_unit_missing_in_installed(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [
                'a.service', 'a.timer', 'b.service', 'b.timer',
            ])
            i = _make_installed(Path(td), ['a.service', 'a.timer'])
            self.assertEqual(
                h.detect_drift(r, i), ['b.service', 'b.timer'],
            )

    def test_extra_installed_units_ignored(self):
        # Operator-installed-but-not-in-repo is NOT this healer's concern.
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.service'])
            i = _make_installed(Path(td), ['a.service', 'extra.service'])
            self.assertEqual(h.detect_drift(r, i), [])


class DedupTest(_IsolatedAgentsRoot):
    def test_first_drift_is_re_DM_eligible(self):
        self.assertTrue(h._should_re_dm({'units': {}}, 'a.timer'))

    def test_just_DMed_blocks_re_DM(self):
        state = {'units': {}}
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        h._record_dm(state, 'a.timer', now=now)
        self.assertFalse(h._should_re_dm(state, 'a.timer', now=now))

    def test_after_window_re_DM_eligible(self):
        state = {'units': {}}
        old = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
        h._record_dm(state, 'a.timer', now=old)
        self.assertTrue(h._should_re_dm(
            state, 'a.timer', now=old + timedelta(hours=13),
        ))


class OrchestrationTest(_IsolatedAgentsRoot):
    """Live + dry-run regression suite covering the *alert-only* missing-install
    behavior. Allowlist is pointed at a nonexistent path so
    `_remediation_allowed('install-drift')` fails safe, keeping these tests as
    the regression guard for the not-allowlisted code path.
    """

    def setUp(self):
        super().setUp()
        self._dm_calls: list[dict] = []

        def fake_dm(message, subject, suggested_action, severity='warning',
                    route='escalate'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
                'route': route,
            })
            return True

        self._dm_patch = mock.patch.object(h, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

        self._allowlist_patch = mock.patch.object(
            h, 'ALLOWLIST_FILE',
            Path(self._isolated_tmp) / 'no-such-allowlist.json',
        )
        self._allowlist_patch.start()
        self.addCleanup(self._allowlist_patch.stop)

    def test_dry_run_with_drift_sends_one_activation_dm(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.service', 'b.timer'])
            i = _make_installed(Path(td), [])
            counts = h.run_once(
                repo_dir=r, installed_dir=i,
                state={'units': {}}, dry_run_override=True,
            )
            self.assertEqual(counts['missing_install'], 2)
            # Only one activation DM despite two drifts.
            self.assertEqual(counts['dm_sent'], 1)
            self.assertEqual(
                self._dm_calls[0]['subject'],
                'install-drift-healer: activate to receive missing-install alerts',
            )

    def test_live_mode_dm_per_unit(self):
        # Not-allowlisted path → manual-dance escalate, but only AFTER the
        # escalation grace elapses. The first tick defers (a deploy-race-safe
        # window); the second, past grace, pages each unit once.
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.service', 'b.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            first = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=t0,
            )
            self.assertEqual(first['dm_sent'], 0)
            self.assertEqual(first['dm_deferred_grace'], 2)
            counts = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False,
                now=t0 + h.ESCALATE_GRACE + timedelta(seconds=1),
            )
            self.assertEqual(counts['dm_sent'], 2)
            subjects = {c['subject'] for c in self._dm_calls}
            self.assertIn('install-drift:a.service', subjects)
            self.assertIn('install-drift:b.timer', subjects)

    def test_suggested_action_includes_install_commands(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['b.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            # First tick defers; the page (with the install-dance) fires past grace.
            h.run_once(repo_dir=r, installed_dir=i, state=state,
                       dry_run_override=False, now=t0)
            h.run_once(repo_dir=r, installed_dir=i, state=state,
                       dry_run_override=False,
                       now=t0 + h.ESCALATE_GRACE + timedelta(seconds=1))
            sug = self._dm_calls[0]['suggested_action']
            self.assertIn('cp', sug)
            self.assertIn('daemon-reload', sug)
            self.assertIn('enable --now', sug)

    def test_no_drift_no_dm(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.service'])
            i = _make_installed(Path(td), ['a.service'])
            counts = h.run_once(
                repo_dir=r, installed_dir=i,
                state={'units': {}}, dry_run_override=False,
            )
            self.assertEqual(counts['dm_sent'], 0)

    def test_dedup_suppresses_second_tick(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            # Tick 1: within grace → deferred, no page yet.
            h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=t0,
            )
            # Tick 2: past grace → the manual-dance page fires exactly once.
            paged = t0 + h.ESCALATE_GRACE + timedelta(seconds=1)
            c2 = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=paged,
            )
            self.assertEqual(c2['dm_sent'], 1)
            self._dm_calls.clear()
            # Tick 3: within RE_DM_WINDOW of the page → dedup-suppressed.
            counts = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=paged + timedelta(hours=1),
            )
            self.assertEqual(counts['dm_sent'], 0)
            self.assertEqual(counts['dm_suppressed_dedup'], 1)

    def test_after_window_re_dms(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            # Tick 1: deferred (within grace). Tick 2: first page (past grace).
            h.run_once(repo_dir=r, installed_dir=i, state=state,
                       dry_run_override=False, now=t0)
            paged = t0 + h.ESCALATE_GRACE + timedelta(seconds=1)
            c2 = h.run_once(repo_dir=r, installed_dir=i, state=state,
                            dry_run_override=False, now=paged)
            self.assertEqual(c2['dm_sent'], 1)
            self._dm_calls.clear()
            # Tick 3: a full RE_DM_WINDOW after the page → the drift re-DMs.
            counts = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=paged + timedelta(hours=13),
            )
            self.assertEqual(counts['dm_sent'], 1)

    def test_reconciled_unit_garbage_collected(self):
        # Patch the alert-retraction helper so the GC pass cannot touch the real
        # prod larry-alerts queue, and assert the reconciled unit's stale alert
        # is retracted exactly once.
        resolved: list[str] = []
        with mock.patch.object(
            h, '_resolve_install_alert', side_effect=resolved.append,
        ), tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False,
            )
            self.assertIn('a.timer', state['units'])
            self.assertEqual(resolved, [])  # nothing reconciled yet
            # Operator installs it.
            (Path(i) / 'a.timer').write_text('# a.timer\n')
            counts = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False,
            )
            self.assertNotIn('a.timer', state['units'])
            self.assertEqual(counts['reconciled_gc'], 1)
            self.assertEqual(resolved, ['a.timer'])

    def test_kill_switch_exits_clean(self):
        with tempfile.TemporaryDirectory() as td:
            kill = Path(td) / 'disable'
            kill.write_text('1')
            with mock.patch.object(h, 'KILL_SWITCH', kill):
                with tempfile.TemporaryDirectory() as td2:
                    r = _make_repo_systemd(Path(td2), ['a.timer'])
                    counts = h.run_once(
                        repo_dir=r, installed_dir=Path(td2),
                        state={'units': {}},
                    )
                    self.assertEqual(counts['dm_sent'], 0)


class EscalationGraceTest(_IsolatedAgentsRoot):
    """The escalation grace window (ESCALATE_GRACE) and the stand-down DM.

    A failed/disabled auto-install is NOT paged on first sight — a freshly-merged
    unit the post-merge trigger catches mid-deploy clears within minutes and is
    GC'd, never DMing. A drift still present past the window pages; when a unit
    that WAS paged later reconciles, a one-line stand-down DM closes the loop.
    Allowlist points at a nonexistent file so the not-allowlisted escalate path
    runs directly, without remediation mocks.
    """

    def setUp(self):
        super().setUp()
        self._dm_calls: list[dict] = []

        def fake_dm(message, subject, suggested_action, severity='warning',
                    route='escalate'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
                'route': route,
            })
            return True

        self._dm_patch = mock.patch.object(h, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

        self._allowlist_patch = mock.patch.object(
            h, 'ALLOWLIST_FILE',
            Path(self._isolated_tmp) / 'no-such-allowlist.json',
        )
        self._allowlist_patch.start()
        self.addCleanup(self._allowlist_patch.stop)

    def test_within_escalate_grace_first_sight_then_expiry(self):
        state = {'units': {}}
        t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        # First sight: stamps first_seen_at and defers; leaves dedup window open.
        self.assertTrue(h._within_escalate_grace(state, 'a.timer', now=t0))
        self.assertIn('first_seen_at', state['units']['a.timer'])
        self.assertNotIn('last_dm_at', state['units']['a.timer'])
        # Inside the window → still defers.
        self.assertTrue(h._within_escalate_grace(
            state, 'a.timer', now=t0 + h.ESCALATE_GRACE - timedelta(seconds=1)))
        # Past the window → escalate.
        self.assertFalse(h._within_escalate_grace(
            state, 'a.timer', now=t0 + h.ESCALATE_GRACE + timedelta(seconds=1)))

    def test_grace_helper_corrupt_stamp_restarts_clock(self):
        state = {'units': {'a.timer': {
            'dm_count': 0, 'first_seen_at': 'not-a-date'}}}
        t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        # Corrupt stamp → restart the clock (defer) rather than escalate on garbage.
        self.assertTrue(h._within_escalate_grace(state, 'a.timer', now=t0))
        self.assertEqual(
            state['units']['a.timer']['first_seen_at'], t0.isoformat())

    def test_deploy_race_resolves_within_grace_never_pages(self):
        # The headline fix: a drift the post-merge trigger catches, then resolves
        # within the grace window, pages ZERO times and stands down ZERO times.
        with mock.patch.object(
            h, '_resolve_install_alert', return_value=0,
        ) as res, tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            # Tick 1: drift detected mid-deploy → deferred, no page.
            c1 = h.run_once(repo_dir=r, installed_dir=i, state=state,
                            dry_run_override=False, now=t0)
            self.assertEqual(c1['dm_sent'], 0)
            self.assertEqual(c1['dm_deferred_grace'], 1)
            self.assertIn('a.timer', state['units'])
            # Deploy finishes installing the unit, still inside the grace window.
            (Path(i) / 'a.timer').write_text('# a.timer\n')
            c2 = h.run_once(repo_dir=r, installed_dir=i, state=state,
                            dry_run_override=False, now=t0 + timedelta(minutes=5))
        self.assertNotIn('a.timer', state['units'])
        self.assertEqual(c2['reconciled_gc'], 1)
        self.assertEqual(c2['stand_down'], 0)
        self.assertEqual(self._dm_calls, [])  # never paged, never stood down
        res.assert_called_once_with('a.timer')

    def test_stand_down_dm_after_paged_drift_resolves(self):
        # A drift that persisted past grace pages; when it later reconciles a
        # stand-down closure DM fires (gated on a real queue line being removed).
        with mock.patch.object(
            h, '_resolve_install_alert', return_value=1,
        ) as res, tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            h.run_once(repo_dir=r, installed_dir=i, state=state,
                       dry_run_override=False, now=t0)  # defer
            paged = t0 + h.ESCALATE_GRACE + timedelta(seconds=1)
            c2 = h.run_once(repo_dir=r, installed_dir=i, state=state,
                            dry_run_override=False, now=paged)  # page
            self.assertEqual(c2['dm_sent'], 1)
            self.assertEqual(
                self._dm_calls[-1]['subject'], 'install-drift:a.timer')
            self._dm_calls.clear()
            # Operator installs it; next tick reconciles and stands down.
            (Path(i) / 'a.timer').write_text('# a.timer\n')
            c3 = h.run_once(repo_dir=r, installed_dir=i, state=state,
                            dry_run_override=False, now=paged + timedelta(hours=1))
        self.assertEqual(c3['reconciled_gc'], 1)
        self.assertEqual(c3['stand_down'], 1)
        self.assertEqual(len(self._dm_calls), 1)
        sd = self._dm_calls[0]
        self.assertEqual(sd['subject'], 'install-resolved:a.timer')
        self.assertEqual(sd['route'], 'closure')
        self.assertIn('Stand down', sd['message'])
        res.assert_called_once_with('a.timer')

    def test_no_stand_down_when_unit_deleted_from_repo(self):
        # A paged unit later REMOVED from the repo (a revert) also leaves
        # live_set: the stale 🔴 is still retracted, but no false "now installed"
        # stand-down fires (the unit was never installed on the droplet).
        with mock.patch.object(
            h, '_resolve_install_alert', return_value=1,
        ) as res, tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            t0 = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            h.run_once(repo_dir=r, installed_dir=i, state=state,
                       dry_run_override=False, now=t0)  # defer
            paged = t0 + h.ESCALATE_GRACE + timedelta(seconds=1)
            c2 = h.run_once(repo_dir=r, installed_dir=i, state=state,
                            dry_run_override=False, now=paged)  # page
            self.assertEqual(c2['dm_sent'], 1)
            self._dm_calls.clear()
            # Unit reverted out of the repo (never installed on the droplet).
            (Path(r) / 'a.timer').unlink()
            c3 = h.run_once(repo_dir=r, installed_dir=i, state=state,
                            dry_run_override=False, now=paged + timedelta(hours=1))
        self.assertEqual(c3['reconciled_gc'], 1)
        self.assertEqual(c3['stand_down'], 0)  # no false "installed" all-clear
        self.assertEqual(self._dm_calls, [])
        res.assert_called_once_with('a.timer')  # stale 🔴 still retracted

    def test_pre_grace_state_pages_immediately_not_re_deferred(self):
        # State written by the pre-grace code (already paged, no first_seen_at)
        # must re-page once past RE_DM_WINDOW, not start a fresh grace window.
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            t0 = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
            state = {'units': {'a.timer': {
                'dm_count': 1,
                'last_dm_at': (t0 - timedelta(hours=13)).isoformat(),
            }}}
            counts = h.run_once(repo_dir=r, installed_dir=i, state=state,
                                dry_run_override=False, now=t0)
        self.assertEqual(counts['dm_sent'], 1)  # re-paged, not deferred
        self.assertEqual(counts['dm_deferred_grace'], 0)
        self.assertEqual(self._dm_calls[0]['subject'], 'install-drift:a.timer')


class DetectStuckTimersTest(_IsolatedAgentsRoot):
    """Regression on the 2026-05-30 ourliberty-cycle.timer infinity-trap.

    Trap predicate: ActiveState=active AND NextElapseUSecRealtime empty AND
    NextElapseUSecMonotonic=infinity. Either condition alone is normal
    transient state.
    """

    def _stub_show(self, per_unit_props):
        def fake_show(unit):
            return per_unit_props.get(unit)
        return mock.patch.object(h, '_systemctl_show', side_effect=fake_show)

    def test_classifies_three_units_correctly(self):
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['a.timer', 'b.timer', 'c.timer'])
            per = {
                'a.timer': {
                    'ActiveState': 'active',
                    'NextElapseUSecRealtime': 'Sat 2026-05-30 18:00:00 MDT',
                    'NextElapseUSecMonotonic': '5h 10min 20s',
                    'LastTriggerUSec': 'Sat 2026-05-30 17:00:00 MDT',
                },
                'b.timer': {
                    'ActiveState': 'active',
                    'NextElapseUSecRealtime': '',
                    'NextElapseUSecMonotonic': 'infinity',
                    'LastTriggerUSec': 'Sat 2026-05-30 17:43:15 MDT',
                },
                'c.timer': {
                    'ActiveState': 'inactive',
                    'NextElapseUSecRealtime': '',
                    'NextElapseUSecMonotonic': 'infinity',
                    'LastTriggerUSec': 'n/a',
                },
            }
            with self._stub_show(per):
                stuck = h.detect_stuck_timers(i)
            self.assertEqual(len(stuck), 1)
            self.assertEqual(stuck[0]['unit'], 'b.timer')
            self.assertEqual(
                stuck[0]['last_trigger'], 'Sat 2026-05-30 17:43:15 MDT',
            )

    def test_show_failure_skips_unit_and_logs(self):
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['ok.timer', 'broken.timer'])
            per = {
                'ok.timer': {
                    'ActiveState': 'active',
                    'NextElapseUSecRealtime': '',
                    'NextElapseUSecMonotonic': 'infinity',
                    'LastTriggerUSec': 'Sat 2026-05-30 17:43:15 MDT',
                },
                'broken.timer': None,  # _systemctl_show returns None on failure
            }
            with self._stub_show(per):
                stuck = h.detect_stuck_timers(i)
            self.assertEqual([s['unit'] for s in stuck], ['ok.timer'])

    def test_non_timer_units_ignored(self):
        # `.service` files installed alongside timers must not be probed.
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['app.service', 'app.timer'])
            seen: list[str] = []

            def fake_show(unit):
                seen.append(unit)
                return {
                    'ActiveState': 'active',
                    'NextElapseUSecRealtime': '',
                    'NextElapseUSecMonotonic': 'infinity',
                    'LastTriggerUSec': '',
                }

            with mock.patch.object(h, '_systemctl_show', side_effect=fake_show):
                stuck = h.detect_stuck_timers(i)
            self.assertEqual(seen, ['app.timer'])
            self.assertEqual([s['unit'] for s in stuck], ['app.timer'])


class JustFiredGuardTest(_IsolatedAgentsRoot):
    """The false-positive guard: a timer reading the infinity anchor but that
    fired within JUST_FIRED_GRACE_S is a transient post-fire recompute, not a
    stall, and must NOT be classified stuck. Regression on the recurring
    2026-06-01 12:00 leak that 8fe7bef's OnCalendar conversion did not fix.
    """

    # Fixed naive-local "now" so timestamp math is deterministic.
    NOW = datetime(2026, 6, 1, 12, 0, 5)

    def _trap_props(self, last_trigger):
        return {
            'ActiveState': 'active',
            'NextElapseUSecRealtime': '',
            'NextElapseUSecMonotonic': 'infinity',
            'LastTriggerUSec': last_trigger,
        }

    def _detect(self, last_trigger):
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['t.timer'])
            per = {'t.timer': self._trap_props(last_trigger)}
            with mock.patch.object(h, '_systemctl_show',
                                   side_effect=lambda u: per.get(u)):
                return h.detect_stuck_timers(i, now=self.NOW)

    def test_just_fired_timer_in_trap_is_not_stuck(self):
        # Fired 5s ago at the top of the period — the leak's exact shape.
        stuck = self._detect('Mon 2026-06-01 12:00:00 MDT')
        self.assertEqual(stuck, [])

    def test_stale_trigger_in_trap_is_still_stuck(self):
        # Last fired 12h ago and lost its anchor — a genuine stall.
        stuck = self._detect('Mon 2026-06-01 00:00:00 MDT')
        self.assertEqual([s['unit'] for s in stuck], ['t.timer'])

    def test_unparseable_trigger_falls_back_to_stuck(self):
        # 'n/a' / garbage can't prove recency, so we keep the prior behavior
        # (classify stuck) rather than silently suppress a real stall.
        self.assertEqual(
            [s['unit'] for s in self._detect('n/a')], ['t.timer'])
        self.assertEqual(
            [s['unit'] for s in self._detect('')], ['t.timer'])

    def test_parse_systemd_timestamp(self):
        # Audit #23: a recognized zone abbreviation makes the parse tz-AWARE so
        # the trigger carries its true instant (MDT = UTC-6).
        self.assertEqual(
            h._parse_systemd_timestamp('Mon 2026-06-01 18:00:09 MDT'),
            datetime(2026, 6, 1, 18, 0, 9,
                     tzinfo=timezone(timedelta(hours=-6))))
        self.assertEqual(
            h._parse_systemd_timestamp('Sun 2026-01-04 03:00:00 MST'),
            datetime(2026, 1, 4, 3, 0, 0,
                     tzinfo=timezone(timedelta(hours=-7))))
        # Unknown / absent zone falls back to naive (prior behavior).
        self.assertEqual(
            h._parse_systemd_timestamp('Mon 2026-06-01 18:00:09 XYZ'),
            datetime(2026, 6, 1, 18, 0, 9))
        self.assertEqual(
            h._parse_systemd_timestamp('Mon 2026-06-01 18:00:09'),
            datetime(2026, 6, 1, 18, 0, 9))
        self.assertIsNone(h._parse_systemd_timestamp(''))
        self.assertIsNone(h._parse_systemd_timestamp('n/a'))
        self.assertIsNone(h._parse_systemd_timestamp('garbage'))

    def test_recently_triggered_window(self):
        now = self.NOW
        self.assertTrue(
            h._recently_triggered('Mon 2026-06-01 12:00:00 MDT', now=now))
        self.assertFalse(
            h._recently_triggered('Mon 2026-06-01 11:00:00 MDT', now=now))
        # Unparseable -> None so the caller can fall back.
        self.assertIsNone(h._recently_triggered('n/a', now=now))

    def test_recently_triggered_dst_spring_forward(self):
        # Audit #23: at the spring-forward gap the timer fired 30 real seconds
        # before `now`, but on opposite DST offsets. An absolute (aware)
        # comparison must read it as JUST FIRED, not ~1h stale.
        mdt = timezone(timedelta(hours=-6))
        now = datetime(2026, 3, 8, 3, 0, 0, tzinfo=mdt)  # 09:00:00Z
        # 01:59:30 MST (-7) == 08:59:30Z -> 30s before now.
        self.assertTrue(
            h._recently_triggered('Sun 2026-03-08 01:59:30 MST', now=now))

    def test_recently_triggered_dst_fall_back(self):
        # At the fall-back fold a full real hour elapsed across repeated
        # wall-clock; the aware comparison must NOT treat it as just-fired
        # (naive math would have called it 60s and skipped a wedged timer).
        mst = timezone(timedelta(hours=-7))
        # 01:30:00 MDT (-6) == 07:30:00Z; 01:31:00 MST (-7) == 08:31:00Z -> 61m.
        now = datetime(2026, 11, 1, 1, 31, 0, tzinfo=mst)
        self.assertFalse(
            h._recently_triggered('Sun 2026-11-01 01:30:00 MDT', now=now))


class TriggeredUnitActiveGuardTest(_IsolatedAgentsRoot):
    """The triggered-unit-active guard: a `.timer` reads the infinity anchor for
    the ENTIRE duration its triggered `Unit=` service runs (systemd computes no
    next elapse while the triggered unit is active). ourliberty-cycle.service is
    Type=simple and a /cycle runs 3-20min, far past JUST_FIRED_GRACE_S=120, so
    any tick landing mid-cycle misread the normal running state as stuck. While
    the triggered unit is active/activating we must NOT classify the timer stuck;
    a genuinely wedged timer (triggered unit inactive/dead) still must.
    """

    # Well past the 120s just-fired grace so the just-fired filter never fires;
    # the triggered-unit guard is what's under test. Naive-local to match the
    # trap timestamps below.
    NOW = datetime(2026, 7, 6, 6, 0, 5)

    def _trap_props(self, last_trigger, triggered_unit='ourliberty-cycle.service'):
        return {
            'ActiveState': 'active',
            'NextElapseUSecRealtime': '',
            'NextElapseUSecMonotonic': 'infinity',
            'LastTriggerUSec': last_trigger,
            'Unit': triggered_unit,
        }

    def _detect(self, trap_props, triggered_state):
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['ourliberty-cycle.timer'])
            per = {'ourliberty-cycle.timer': trap_props}
            with mock.patch.object(
                h, '_systemctl_show', side_effect=lambda u: per.get(u)
            ), mock.patch.object(
                h, '_triggered_unit_active_state',
                return_value=triggered_state,
            ):
                return h.detect_stuck_timers(i, now=self.NOW)

    def test_triggered_unit_active_is_not_stuck(self):
        # In-flight cycle: infinity anchor is expected, not a stall. Even though
        # the last trigger is well past the grace, the running service suppresses.
        stuck = self._detect(
            self._trap_props('Mon 2026-07-06 06:05:10 MDT'), 'active')
        self.assertEqual(stuck, [])

    def test_triggered_unit_activating_is_not_stuck(self):
        stuck = self._detect(
            self._trap_props('Mon 2026-07-06 06:05:10 MDT'), 'activating')
        self.assertEqual(stuck, [])

    def test_triggered_unit_inactive_still_stuck(self):
        # Triggered service is dead AND the anchor is gone AND last trigger is
        # stale — a genuine 2026-05-31-class wedge; must still classify stuck.
        stuck = self._detect(
            self._trap_props('Mon 2026-07-05 00:00:00 MDT'), 'inactive')
        self.assertEqual([s['unit'] for s in stuck], ['ourliberty-cycle.timer'])

    def test_triggered_unit_failed_still_stuck(self):
        stuck = self._detect(
            self._trap_props('Mon 2026-07-05 00:00:00 MDT'), 'failed')
        self.assertEqual([s['unit'] for s in stuck], ['ourliberty-cycle.timer'])

    def test_triggered_unit_probe_failure_falls_back_to_stuck(self):
        # ActiveState can't be read (shell-out failure -> None): do NOT suppress;
        # fall back to the prior predicate so a real stall is never hidden.
        stuck = self._detect(
            self._trap_props('Mon 2026-07-05 00:00:00 MDT'), None)
        self.assertEqual([s['unit'] for s in stuck], ['ourliberty-cycle.timer'])

    def test_no_triggered_unit_prop_falls_back_to_stuck(self):
        # Timer with no resolvable Unit= (empty prop): _triggered_unit_active_state
        # is never consulted; fall back to classify stuck.
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['t.timer'])
            per = {'t.timer': self._trap_props(
                'Mon 2026-07-05 00:00:00 MDT', triggered_unit='')}
            with mock.patch.object(
                h, '_systemctl_show', side_effect=lambda u: per.get(u)
            ), mock.patch.object(
                h, '_triggered_unit_active_state',
            ) as probe:
                stuck = h.detect_stuck_timers(i, now=self.NOW)
            probe.assert_not_called()
            self.assertEqual([s['unit'] for s in stuck], ['t.timer'])

    def test_just_fired_skips_before_triggered_unit_probe(self):
        # A just-fired timer is skipped by the earlier filter; the triggered-unit
        # probe must not even run (order + short-circuit intact).
        with tempfile.TemporaryDirectory() as td:
            i = _make_installed(Path(td), ['ourliberty-cycle.timer'])
            per = {'ourliberty-cycle.timer': self._trap_props(
                'Mon 2026-07-06 06:00:00 MDT')}
            with mock.patch.object(
                h, '_systemctl_show', side_effect=lambda u: per.get(u)
            ), mock.patch.object(
                h, '_triggered_unit_active_state',
            ) as probe:
                stuck = h.detect_stuck_timers(i, now=self.NOW)
            probe.assert_not_called()
            self.assertEqual(stuck, [])


class TriggeredUnitActiveStateProbeTest(_IsolatedAgentsRoot):
    """_triggered_unit_active_state shell-out adapter: parses ActiveState; on
    rc != 0, shell failure, or absent property returns None (guard falls back).
    """

    def test_parses_active_state(self):
        completed = mock.MagicMock()
        completed.returncode = 0
        completed.stdout = 'ActiveState=active\n'
        completed.stderr = ''
        with mock.patch.object(h.subprocess, 'run', return_value=completed):
            self.assertEqual(
                h._triggered_unit_active_state('ourliberty-cycle.service'),
                'active')

    def test_nonzero_rc_returns_none(self):
        completed = mock.MagicMock()
        completed.returncode = 1
        completed.stdout = ''
        completed.stderr = 'Unit not found'
        with mock.patch.object(h.subprocess, 'run', return_value=completed):
            self.assertIsNone(h._triggered_unit_active_state('bogus.service'))

    def test_timeout_returns_none(self):
        def fake_run(*args, **kwargs):
            raise h.subprocess.TimeoutExpired(cmd=args[0], timeout=10)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            self.assertIsNone(h._triggered_unit_active_state('any.service'))

    def test_absent_property_returns_none(self):
        completed = mock.MagicMock()
        completed.returncode = 0
        completed.stdout = 'SomethingElse=x\n'
        completed.stderr = ''
        with mock.patch.object(h.subprocess, 'run', return_value=completed):
            self.assertIsNone(h._triggered_unit_active_state('any.service'))


class SystemctlShowParserTest(_IsolatedAgentsRoot):
    """_systemctl_show shell-out adapter: parses the KEY=VALUE block; on
    rc != 0 or shell failure returns None + INFO log.
    """

    def test_parses_keyvalue_block(self):
        completed = mock.MagicMock()
        completed.returncode = 0
        completed.stdout = (
            'NextElapseUSecRealtime=\n'
            'NextElapseUSecMonotonic=infinity\n'
            'LastTriggerUSec=Sat 2026-05-30 17:43:15 MDT\n'
            'ActiveState=active\n'
        )
        completed.stderr = ''
        with mock.patch.object(h.subprocess, 'run', return_value=completed):
            props = h._systemctl_show('ourliberty-cycle.timer')
        self.assertEqual(props['ActiveState'], 'active')
        self.assertEqual(props['NextElapseUSecRealtime'], '')
        self.assertEqual(props['NextElapseUSecMonotonic'], 'infinity')
        self.assertEqual(
            props['LastTriggerUSec'], 'Sat 2026-05-30 17:43:15 MDT',
        )

    def test_nonzero_rc_returns_none(self):
        completed = mock.MagicMock()
        completed.returncode = 1
        completed.stdout = ''
        completed.stderr = 'Failed to get properties'
        with mock.patch.object(h.subprocess, 'run', return_value=completed):
            self.assertIsNone(h._systemctl_show('bogus.timer'))

    def test_timeout_returns_none(self):
        def fake_run(*args, **kwargs):
            raise h.subprocess.TimeoutExpired(cmd=args[0], timeout=10)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            self.assertIsNone(h._systemctl_show('any.timer'))


class StuckTimerOrchestrationTest(_IsolatedAgentsRoot):
    """run_once stuck-timer pass: dry-run DMs per unit; enabled mode heals
    via inline daemon-reload + restart then sends closure DM; cooldown via
    _should_re_dm in the `stuck_timers` state bucket.
    """

    def setUp(self):
        super().setUp()
        self._dm_calls: list[dict] = []

        def fake_dm(message, subject, suggested_action, severity='warning',
                    route='escalate'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
                'route': route,
            })
            return True

        self._dm_patch = mock.patch.object(h, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

    def _stub_show(self, per_unit):
        def fake_show(unit):
            entry = per_unit.get(unit)
            if callable(entry):
                return entry()
            return entry
        return mock.patch.object(h, '_systemctl_show', side_effect=fake_show)

    def test_dry_run_dms_per_unit_with_recovery_command(self):
        # Intentional asymmetry vs missing-install dry-run: stuck-timer is an
        # active fault — operator needs the unit name + recovery command.
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [])
            i = _make_installed(Path(td), ['ourliberty-cycle.timer'])
            stuck_props = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': '',
                'NextElapseUSecMonotonic': 'infinity',
                'LastTriggerUSec': 'Sat 2026-05-30 17:43:15 MDT',
            }
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            with self._stub_show({'ourliberty-cycle.timer': stuck_props}), \
                    mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=True,
                )
            self.assertEqual(counts['stuck_timer'], 1)
            self.assertEqual(counts['timer_healed'], 0)
            # DM emitted; no subprocess shell-out for restart.
            stuck_dms = [
                c for c in self._dm_calls
                if c['subject'].startswith('stuck-timer:')
            ]
            self.assertEqual(len(stuck_dms), 1)
            self.assertIn(
                'sudo systemctl daemon-reload', stuck_dms[0]['message'],
            )
            self.assertIn(
                'sudo systemctl restart ourliberty-cycle.timer',
                stuck_dms[0]['message'],
            )
            self.assertEqual(ran, [], f'no shell-out expected, got {ran!r}')

    def test_enabled_mode_heals_via_reload_then_restart(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [])
            i = _make_installed(Path(td), ['ourliberty-cycle.timer'])
            stuck_props = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': '',
                'NextElapseUSecMonotonic': 'infinity',
                'LastTriggerUSec': 'Sat 2026-05-30 17:43:15 MDT',
            }
            healed_props = dict(stuck_props)
            healed_props['NextElapseUSecRealtime'] = (
                'Sat 2026-05-30 18:00:00 MDT'
            )
            healed_props['NextElapseUSecMonotonic'] = '5h 10min 20s'
            show_calls = {'n': 0}

            def show_seq():
                show_calls['n'] += 1
                return stuck_props if show_calls['n'] == 1 else healed_props

            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            with self._stub_show({'ourliberty-cycle.timer': show_seq}), \
                    mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=False,
                )
            self.assertEqual(counts['stuck_timer'], 1)
            self.assertEqual(counts['timer_healed'], 1)
            # daemon-reload precedes restart.
            self.assertEqual(len(ran), 2)
            self.assertEqual(
                ran[0], ['sudo', '-n', 'systemctl', 'daemon-reload'],
            )
            self.assertEqual(
                ran[1],
                ['sudo', '-n', 'systemctl', 'restart',
                 'ourliberty-cycle.timer'],
            )
            stuck_dms = [
                c for c in self._dm_calls
                if c['subject'] == 'stuck-timer-healed:ourliberty-cycle.timer'
            ]
            self.assertEqual(len(stuck_dms), 1)
            # Routine successful heal → digest (not DM'd; surfaced in digest).
            self.assertEqual(stuck_dms[0]['route'], 'digest')
            self.assertIn('Auto-healed', stuck_dms[0]['message'])
            self.assertIn(
                'Sat 2026-05-30 18:00:00 MDT', stuck_dms[0]['message'],
            )

    def test_cooldown_suppresses_second_tick(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [])
            i = _make_installed(Path(td), ['ourliberty-cycle.timer'])
            stuck_props = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': '',
                'NextElapseUSecMonotonic': 'infinity',
                'LastTriggerUSec': '',
            }
            state = {'units': {}, 'stuck_timers': {}}
            now = datetime(2026, 5, 30, 18, 0, tzinfo=timezone.utc)
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            with self._stub_show({'ourliberty-cycle.timer': stuck_props}), \
                    mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=True, now=now,
                )
                first_dm_count = len(self._dm_calls)
                first_ran = list(ran)
                counts2 = h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=True,
                    now=now + timedelta(hours=1),
                )
            # No new DM. No shell-out either (dry-run never shells out anyway).
            self.assertEqual(len(self._dm_calls), first_dm_count)
            self.assertEqual(ran, first_ran)
            self.assertEqual(counts2['stuck_timer'], 1)
            self.assertGreaterEqual(counts2['dm_suppressed_dedup'], 1)

    def test_counts_two_detected_one_healed(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [])
            i = _make_installed(Path(td), [
                'good.timer', 'bad.timer',
            ])
            stuck_props = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': '',
                'NextElapseUSecMonotonic': 'infinity',
                'LastTriggerUSec': '',
            }
            healed = dict(stuck_props)
            healed['NextElapseUSecRealtime'] = 'Sat 2026-05-30 18:05:00 MDT'

            # Two stuck timers; restart succeeds on `good.timer`, fails on
            # `bad.timer`. Post-restart `_systemctl_show` for good returns
            # healed; bad path never reaches post-show.
            good_calls = {'n': 0}

            def show_for(unit):
                if unit == 'good.timer':
                    good_calls['n'] += 1
                    return stuck_props if good_calls['n'] == 1 else healed
                return stuck_props

            def fake_run(cmd, **kwargs):
                if cmd[3] == 'daemon-reload':
                    return mock.MagicMock(returncode=0, stdout='', stderr='')
                # restart cmd: rc=1 for bad, rc=0 for good.
                unit = cmd[4]
                rc = 1 if unit == 'bad.timer' else 0
                stderr = 'systemctl complained' if rc else ''
                return mock.MagicMock(returncode=rc, stdout='', stderr=stderr)

            with mock.patch.object(
                h, '_systemctl_show', side_effect=show_for,
            ), mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=False,
                )
            self.assertEqual(counts['stuck_timer'], 2)
            self.assertEqual(counts['timer_healed'], 1)

    def test_enabled_mode_heals_even_when_dm_cooldown_active(self):
        # Regression: in enabled mode the heal must fire on EVERY detection;
        # the RE_DM_WINDOW cooldown throttles only the closure DM. Previously
        # the cooldown gated the heal itself, so a timer still/again stuck on
        # the next 12h tick was suppressed and left unhealed (cadence == window
        # == 12h, so this bit routinely).
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [])
            i = _make_installed(Path(td), ['ourliberty-cycle.timer'])
            stuck_props = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': '',
                'NextElapseUSecMonotonic': 'infinity',
                'LastTriggerUSec': '',
            }
            state = {'units': {}, 'stuck_timers': {}}
            now = datetime(2026, 5, 30, 18, 0, tzinfo=timezone.utc)
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            restart_prefix = ['sudo', '-n', 'systemctl', 'restart']
            with self._stub_show({'ourliberty-cycle.timer': stuck_props}), \
                    mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                counts1 = h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=False, now=now,
                )
                dms_after_first = len(self._dm_calls)
                restarts_after_first = [
                    c for c in ran if c[:4] == restart_prefix
                ]
                # Second tick one hour later — well inside RE_DM_WINDOW (12h).
                counts2 = h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=False,
                    now=now + timedelta(hours=1),
                )
            restarts_total = [c for c in ran if c[:4] == restart_prefix]
            # First tick: healed and DM'd once.
            self.assertEqual(counts1['timer_healed'], 1)
            self.assertEqual(dms_after_first, 1)
            self.assertEqual(len(restarts_after_first), 1)
            # Second tick (inside cooldown): healed AGAIN, but no new DM.
            self.assertEqual(counts2['timer_healed'], 1)
            self.assertGreaterEqual(counts2['dm_suppressed_dedup'], 1)
            self.assertEqual(len(self._dm_calls), 1)
            self.assertEqual(len(restarts_total), 2)


def _write_allowlist(td: Path, classes) -> Path:
    p = td / 'auto-remediation-allowlist.json'
    p.write_text(_json_dump({'classes': classes}))
    return p


def _json_dump(obj) -> str:
    import json as _json
    return _json.dumps(obj)


class RemediationAllowlistTest(_IsolatedAgentsRoot):
    """Allowlist gate: allowed / not-listed / missing-file / malformed -> last
    two fail safe. Loader must never raise.
    """

    def test_allowed_when_class_listed(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_allowlist(
                Path(td), ['install-drift', 'stuck-timer'],
            )
            with mock.patch.object(h, 'ALLOWLIST_FILE', p):
                self.assertTrue(h._remediation_allowed('install-drift'))

    def test_not_allowed_when_class_not_listed(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_allowlist(Path(td), ['stuck-timer'])
            with mock.patch.object(h, 'ALLOWLIST_FILE', p):
                self.assertFalse(h._remediation_allowed('install-drift'))

    def test_missing_file_fails_safe(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'does-not-exist.json'
            with mock.patch.object(h, 'ALLOWLIST_FILE', p):
                self.assertFalse(h._remediation_allowed('install-drift'))

    def test_malformed_json_fails_safe(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'broken.json'
            p.write_text('{ this is not json')
            with mock.patch.object(h, 'ALLOWLIST_FILE', p):
                self.assertFalse(h._remediation_allowed('install-drift'))

    def test_unexpected_shape_fails_safe(self):
        with tempfile.TemporaryDirectory() as td:
            top_is_list = Path(td) / 'list.json'
            top_is_list.write_text('["install-drift"]')
            with mock.patch.object(h, 'ALLOWLIST_FILE', top_is_list):
                self.assertFalse(h._remediation_allowed('install-drift'))

            classes_not_list = Path(td) / 'shape.json'
            classes_not_list.write_text(
                _json_dump({'classes': 'install-drift'}),
            )
            with mock.patch.object(h, 'ALLOWLIST_FILE', classes_not_list):
                self.assertFalse(h._remediation_allowed('install-drift'))

            classes_mixed = Path(td) / 'mixed.json'
            classes_mixed.write_text(
                _json_dump({'classes': ['install-drift', 7]}),
            )
            with mock.patch.object(h, 'ALLOWLIST_FILE', classes_mixed):
                self.assertFalse(h._remediation_allowed('install-drift'))


class RemediateMissingInstallTest(_IsolatedAgentsRoot):
    """`_remediate_missing_install` shell-out sequence — fully mocked.
    .timer (or a standalone [Install] daemon) = cp + daemon-reload + enable --now
    + verify; a service WITHOUT [Install] (timer-activated, like foo.service here
    which isn't in the repo) = cp + daemon-reload only (no enable, no verify).
    See RemediateMissingInstallClassAwareTest for the [Install] discrimination.
    """

    def _fake_runs_record(self, plan):
        """plan: callable(cmd) -> MagicMock(returncode, stderr, stdout)."""
        ran: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            ran.append(list(cmd))
            return plan(list(cmd))
        return ran, fake_run

    def test_timer_success_path(self):
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        healed_props = {
            'ActiveState': 'active',
            'NextElapseUSecRealtime': 'Sat 2026-05-31 18:00:00 MDT',
            'NextElapseUSecMonotonic': '5h',
            'LastTriggerUSec': '',
        }
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value=healed_props):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, '')
        # cp, daemon-reload, enable --now (post-verify uses _systemctl_show
        # which is mocked separately, no extra subprocess.run).
        self.assertEqual(len(ran), 3)
        self.assertEqual(ran[0][:3], ['sudo', '-n', 'cp'])
        self.assertEqual(
            ran[1], ['sudo', '-n', 'systemctl', 'daemon-reload'],
        )
        self.assertEqual(
            ran[2],
            ['sudo', '-n', 'systemctl', 'enable', '--now', 'foo.timer'],
        )

    def test_service_without_install_skips_enable(self):
        # foo.service is not in the repo -> no [Install] detectable -> treated as
        # timer-activated -> cp + daemon-reload only (its sibling timer starts it).
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value={}):
            rc, stderr = h._remediate_missing_install('foo.service')
        self.assertEqual(rc, 0)
        # cp + daemon-reload only — no enable line for a no-[Install] service.
        self.assertEqual(len(ran), 2)
        self.assertEqual(ran[0][:3], ['sudo', '-n', 'cp'])
        self.assertEqual(
            ran[1], ['sudo', '-n', 'systemctl', 'daemon-reload'],
        )
        for cmd in ran:
            self.assertNotIn('enable', cmd)

    def test_cp_failure_returns_nonzero(self):
        def plan(cmd):
            if cmd[2] == 'cp':
                return mock.MagicMock(
                    returncode=1, stdout='', stderr='permission denied',
                )
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('cp failed', stderr)
        # Stops after cp — daemon-reload never reached.
        self.assertEqual(len(ran), 1)

    def test_daemon_reload_failure_returns_nonzero(self):
        def plan(cmd):
            if cmd[2] == 'cp':
                return mock.MagicMock(returncode=0, stdout='', stderr='')
            if cmd[3] == 'daemon-reload':
                return mock.MagicMock(
                    returncode=1, stdout='', stderr='bus is down',
                )
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('daemon-reload failed', stderr)

    def test_enable_failure_returns_nonzero(self):
        def plan(cmd):
            if 'enable' in cmd:
                return mock.MagicMock(
                    returncode=1, stdout='', stderr='masked',
                )
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('enable --now failed', stderr)

    def test_verify_failure_returns_nonzero(self):
        # cp / reload / enable all succeed, but the post-enable systemctl-show
        # comes back with ActiveState != active.
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        bad_props = {
            'ActiveState': 'failed',
            'NextElapseUSecRealtime': '',
            'NextElapseUSecMonotonic': 'infinity',
            'LastTriggerUSec': '',
        }
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value=bad_props):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('verify failed', stderr)

    def test_verify_empty_next_fire_returns_nonzero(self):
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        partial = {
            'ActiveState': 'active',
            'NextElapseUSecRealtime': '',
            'NextElapseUSecMonotonic': '5h',
            'LastTriggerUSec': '',
        }
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value=partial):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('verify failed', stderr)

    def test_timer_just_fired_empty_next_fire_is_success(self):
        # Prod 2026-06-10 false-negative: a `Persistent=true` timer whose
        # `enable --now` catches a missed schedule fires its service
        # IMMEDIATELY; for ~1s afterward systemd reports NextElapseUSecRealtime
        # empty / NextElapse=infinity while it recomputes the next elapse. That
        # transient is a SUCCESSFUL install — the just-fired grace
        # (_recently_triggered against LastTriggerUSec) must make this rc==0, not
        # a verify failure that falls back to a 🔴 URGENT manual-dance alert.
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        just_fired = datetime.now().strftime('%a %Y-%m-%d %H:%M:%S')
        props = {
            'ActiveState': 'active',
            'NextElapseUSecRealtime': '',
            'NextElapseUSecMonotonic': 'infinity',
            'LastTriggerUSec': just_fired,
        }
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value=props):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, '')

    def test_timer_empty_next_fire_no_recent_trigger_still_fails(self):
        # The inverse: empty NextElapse + a STALE last-trigger (well outside the
        # grace window) is a genuine verify failure — the install did not take.
        # The just-fired grace must not mask a real failure.
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        stale = (datetime.now() - timedelta(hours=1)).strftime(
            '%a %Y-%m-%d %H:%M:%S')
        props = {
            'ActiveState': 'active',
            'NextElapseUSecRealtime': '',
            'NextElapseUSecMonotonic': 'infinity',
            'LastTriggerUSec': stale,
        }
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value=props):
            rc, stderr = h._remediate_missing_install('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('NextElapseUSecRealtime empty', stderr)


class InstallRemediationOrchestrationTest(_IsolatedAgentsRoot):
    """`run_once()` missing-install loop, allowlist-active path: remediate-then-
    notify on success, fallback alert on failure, .timer-vs-.service enable
    difference, dry-run unchanged.
    """

    def setUp(self):
        super().setUp()
        self._dm_calls: list[dict] = []

        def fake_dm(message, subject, suggested_action, severity='warning',
                    route='escalate'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
                'route': route,
            })
            return True

        self._dm_patch = mock.patch.object(h, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

        self._allowlist_path = _write_allowlist(
            Path(self._isolated_tmp), ['install-drift', 'stuck-timer'],
        )
        self._allowlist_patch = mock.patch.object(
            h, 'ALLOWLIST_FILE', self._allowlist_path,
        )
        self._allowlist_patch.start()
        self.addCleanup(self._allowlist_patch.stop)

    def _patch_show(self, props):
        return mock.patch.object(h, '_systemctl_show', return_value=props)

    def test_remediate_success_emits_healed_dm_and_increments_count(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['ourliberty-x.timer'])
            i = _make_installed(Path(td), [])
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            healed_props = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': 'Sun 2026-05-31 18:00:00 MDT',
                'NextElapseUSecMonotonic': '5h',
                'LastTriggerUSec': '',
            }
            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                    self._patch_show(healed_props):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=False,
                )

        self.assertEqual(counts['missing_install'], 1)
        self.assertEqual(counts['install_healed'], 1)
        self.assertEqual(counts['dm_sent'], 1)
        # Exactly one DM, the healed one — no manual-dance alert.
        self.assertEqual(len(self._dm_calls), 1)
        dm = self._dm_calls[0]
        self.assertEqual(dm['subject'], 'install-healed:ourliberty-x.timer')
        # Routine successful heal → digest (surfaced in the daily digest, no DM).
        self.assertEqual(dm['route'], 'digest')
        self.assertIn('Auto-installed', dm['message'])
        self.assertIn('Sun 2026-05-31 18:00:00 MDT', dm['message'])
        self.assertIn('systemctl status', dm['suggested_action'])
        # Manual-dance phrasing must NOT be present.
        self.assertNotIn('ssh larry@', dm['suggested_action'])

    def test_remediate_failure_falls_back_to_manual_dance_alert(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['ourliberty-x.timer'])
            i = _make_installed(Path(td), [])

            def fake_run(cmd, **kwargs):
                # cp succeeds; daemon-reload fails.
                if cmd[2] == 'cp':
                    return mock.MagicMock(returncode=0, stdout='', stderr='')
                if cmd[3] == 'daemon-reload':
                    return mock.MagicMock(
                        returncode=1, stdout='', stderr='bus down',
                    )
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            state = {'units': {}, 'stuck_timers': {}}
            t0 = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                # Tick 1: auto-install fails but the drift is fresh → deferred,
                # no page (covers the deploy-sync race that resolves on its own).
                first = h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=False, now=t0,
                )
                self.assertEqual(first['dm_sent'], 0)
                self.assertEqual(first['dm_deferred_grace'], 1)
                # Tick 2: install still failing past the grace window → the
                # manual-dance page fires.
                counts = h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=False,
                    now=t0 + h.ESCALATE_GRACE + timedelta(seconds=1),
                )

        self.assertEqual(counts['install_healed'], 0)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(len(self._dm_calls), 1)
        dm = self._dm_calls[0]
        self.assertEqual(dm['subject'], 'install-drift:ourliberty-x.timer')
        # Failed heal → escalate (DM Larry the manual action).
        self.assertEqual(dm['route'], 'escalate')
        # Manual-dance fallback: cp + daemon-reload + enable --now in suggested.
        self.assertIn('cp', dm['suggested_action'])
        self.assertIn('daemon-reload', dm['suggested_action'])
        self.assertIn('enable --now', dm['suggested_action'])

    def test_timer_activated_service_not_enabled_only_timer(self):
        # svc.service has no [Install] (_make_repo_systemd writes a bare file),
        # so it is started by its sibling timer — only svc.timer is enable --now'd.
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['svc.service', 'svc.timer'])
            i = _make_installed(Path(td), [])
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            healed_props = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': 'Sun 2026-05-31 18:00:00 MDT',
                'NextElapseUSecMonotonic': '5h',
                'LastTriggerUSec': '',
            }
            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                    self._patch_show(healed_props):
                h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=False,
                )

        enable_cmds = [c for c in ran if 'enable' in c]
        self.assertEqual(len(enable_cmds), 1)
        self.assertIn('svc.timer', enable_cmds[0])
        self.assertNotIn('svc.service', enable_cmds[0])


    def test_dry_run_unchanged_no_remediation_shell_out(self):
        # With allowlist active, dry-run must still emit a single activation
        # DM and never shell out. (Remediation only fires in live mode.)
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer', 'b.service'])
            i = _make_installed(Path(td), [])
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=True,
                )
        self.assertEqual(counts['missing_install'], 2)
        self.assertEqual(counts['install_healed'], 0)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(
            self._dm_calls[0]['subject'],
            'install-drift-healer: activate to receive missing-install alerts',
        )
        self.assertEqual(ran, [])


def _make_repo_with_content(td: Path, units: dict) -> Path:
    d = td / 'repo-content'
    d.mkdir()
    for u, text in units.items():
        (d / u).write_text(text)
    return d


def _make_installed_with_content(td: Path, units: dict) -> Path:
    d = td / 'installed-content'
    d.mkdir()
    for u, text in units.items():
        (d / u).write_text(text)
    return d


class DetectContentDriftTest(_IsolatedAgentsRoot):
    """Content drift = unit present in BOTH dirs whose installed MAIN file
    differs from the repo copy. Drop-ins under `<unit>.d/` are NOT compared;
    a single trailing newline is normalized.
    """

    def test_flags_differing_content(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_with_content(Path(td), {
                'a.timer': '[Timer]\nOnUnitActiveSec=1h\n',
            })
            i = _make_installed_with_content(Path(td), {
                'a.timer': '[Timer]\nOnUnitActiveSec=1d\n',
            })
            self.assertEqual(h.detect_content_drift(r, i), ['a.timer'])

    def test_ignores_identical_content(self):
        with tempfile.TemporaryDirectory() as td:
            same = '[Timer]\nOnUnitActiveSec=1h\n'
            r = _make_repo_with_content(Path(td), {'a.timer': same})
            i = _make_installed_with_content(Path(td), {'a.timer': same})
            self.assertEqual(h.detect_content_drift(r, i), [])

    def test_single_trailing_newline_normalized(self):
        # Repo has trailing newline, installed does not — NOT drift.
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_with_content(Path(td), {
                'a.timer': '[Timer]\nOnUnitActiveSec=1h\n',
            })
            i = _make_installed_with_content(Path(td), {
                'a.timer': '[Timer]\nOnUnitActiveSec=1h',
            })
            self.assertEqual(h.detect_content_drift(r, i), [])

    def test_double_trailing_newline_is_drift(self):
        # Only ONE trailing newline is normalized; a second is real drift.
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_with_content(Path(td), {
                'a.timer': '[Timer]\nOnUnitActiveSec=1h\n',
            })
            i = _make_installed_with_content(Path(td), {
                'a.timer': '[Timer]\nOnUnitActiveSec=1h\n\n',
            })
            self.assertEqual(h.detect_content_drift(r, i), ['a.timer'])

    def test_ignores_units_missing_from_either_side(self):
        with tempfile.TemporaryDirectory() as td:
            # repo-only b.timer (missing-install territory) + installed-only
            # c.timer (operator install) must both be ignored here.
            r = _make_repo_with_content(Path(td), {
                'a.timer': 'x\n', 'b.timer': 'y\n',
            })
            i = _make_installed_with_content(Path(td), {
                'a.timer': 'x\n', 'c.timer': 'z\n',
            })
            self.assertEqual(h.detect_content_drift(r, i), [])

    def test_ignores_dropin_overrides(self):
        # Main file identical; a differing drop-in under a.timer.d/ must NOT
        # count as drift (operator live-tuning via `systemctl edit`).
        with tempfile.TemporaryDirectory() as td:
            same = '[Timer]\nOnUnitActiveSec=1h\n'
            r = _make_repo_with_content(Path(td), {'a.timer': same})
            i = _make_installed_with_content(Path(td), {'a.timer': same})
            dropin = i / 'a.timer.d'
            dropin.mkdir()
            (dropin / 'override.conf').write_text(
                '[Timer]\nOnUnitActiveSec=30m\n',
            )
            self.assertEqual(h.detect_content_drift(r, i), [])


class ServiceClassifyTest(_IsolatedAgentsRoot):
    """`_service_is_long_running`: Type=oneshot -> False; active non-oneshot
    -> True; shell-out failure -> False (safe default never restarts).
    """

    def _show(self, stdout, rc=0):
        completed = mock.MagicMock()
        completed.returncode = rc
        completed.stdout = stdout
        completed.stderr = ''
        return mock.patch.object(h.subprocess, 'run', return_value=completed)

    def test_oneshot_is_not_long_running(self):
        with self._show('Type=oneshot\nActiveState=active\n'):
            self.assertFalse(h._service_is_long_running('x.service'))

    def test_active_simple_is_long_running(self):
        with self._show('Type=simple\nActiveState=active\n'):
            self.assertTrue(h._service_is_long_running('x.service'))

    def test_inactive_simple_is_not_long_running(self):
        with self._show('Type=simple\nActiveState=inactive\n'):
            self.assertFalse(h._service_is_long_running('x.service'))

    def test_nonzero_rc_is_not_long_running(self):
        with self._show('', rc=1):
            self.assertFalse(h._service_is_long_running('x.service'))

    def test_shellout_failure_is_not_long_running(self):
        def boom(*a, **k):
            raise h.subprocess.TimeoutExpired(cmd=a[0], timeout=10)
        with mock.patch.object(h.subprocess, 'run', side_effect=boom):
            self.assertFalse(h._service_is_long_running('x.service'))


class RemediateContentDriftTest(_IsolatedAgentsRoot):
    """`_remediate_content_drift` shell-out sequence — fully mocked.
    .timer = cp + daemon-reload + restart + verify; long-running .service =
    cp + daemon-reload + restart; oneshot .service = cp + daemon-reload only.
    """

    def _fake_runs_record(self, plan):
        ran: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            ran.append(list(cmd))
            return plan(list(cmd))
        return ran, fake_run

    def test_timer_restart_and_verify(self):
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        healed = {
            'ActiveState': 'active',
            'NextElapseUSecRealtime': 'Sat 2026-06-03 18:00:00 MDT',
            'NextElapseUSecMonotonic': '1h',
            'LastTriggerUSec': '',
        }
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value=healed):
            rc, stderr = h._remediate_content_drift('foo.timer')
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, '')
        # cp, daemon-reload, restart (verify uses mocked _systemctl_show).
        self.assertEqual(len(ran), 3)
        self.assertEqual(ran[0][:3], ['sudo', '-n', 'cp'])
        self.assertEqual(ran[1], ['sudo', '-n', 'systemctl', 'daemon-reload'])
        self.assertEqual(
            ran[2], ['sudo', '-n', 'systemctl', 'restart', 'foo.timer'],
        )

    def test_timer_verify_failure_returns_nonzero(self):
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        bad = {
            'ActiveState': 'failed',
            'NextElapseUSecRealtime': '',
            'NextElapseUSecMonotonic': 'infinity',
            'LastTriggerUSec': '',
        }
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value=bad):
            rc, stderr = h._remediate_content_drift('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('verify failed', stderr)

    def test_oneshot_service_no_restart_no_enable(self):
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(
                    h, '_service_is_long_running', return_value=False):
            rc, stderr = h._remediate_content_drift('foo.service')
        self.assertEqual(rc, 0)
        # cp + daemon-reload only — no restart, no enable.
        self.assertEqual(len(ran), 2)
        self.assertEqual(ran[0][:3], ['sudo', '-n', 'cp'])
        self.assertEqual(ran[1], ['sudo', '-n', 'systemctl', 'daemon-reload'])
        for cmd in ran:
            self.assertNotIn('enable', cmd)
            self.assertNotIn('restart', cmd)

    def test_long_running_service_restarts(self):
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(
                    h, '_service_is_long_running', return_value=True):
            rc, stderr = h._remediate_content_drift('foo.service')
        self.assertEqual(rc, 0)
        self.assertEqual(len(ran), 3)
        self.assertEqual(
            ran[2], ['sudo', '-n', 'systemctl', 'restart', 'foo.service'],
        )
        for cmd in ran:
            self.assertNotIn('enable', cmd)

    def test_reload_failure_returns_nonzero(self):
        def plan(cmd):
            if cmd[2] == 'cp':
                return mock.MagicMock(returncode=0, stdout='', stderr='')
            if cmd[3] == 'daemon-reload':
                return mock.MagicMock(
                    returncode=1, stdout='', stderr='bus down',
                )
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, stderr = h._remediate_content_drift('foo.timer')
        self.assertNotEqual(rc, 0)
        self.assertIn('daemon-reload failed', stderr)


class ContentDriftOrchestrationTest(_IsolatedAgentsRoot):
    """`run_once()` content-drift loop: dry-run emits a single activation DM
    and never shells out; live+allowed remediates-then-notifies; live failure
    falls back to the manual-dance DM; kill-switch blocks all action.
    """

    def setUp(self):
        super().setUp()
        self._dm_calls: list[dict] = []

        def fake_dm(message, subject, suggested_action, severity='warning',
                    route='escalate'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
                'route': route,
            })
            return True

        self._dm_patch = mock.patch.object(h, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

        self._allowlist_path = _write_allowlist(
            Path(self._isolated_tmp), ['install-drift', 'stuck-timer'],
        )
        self._allowlist_patch = mock.patch.object(
            h, 'ALLOWLIST_FILE', self._allowlist_path,
        )
        self._allowlist_patch.start()
        self.addCleanup(self._allowlist_patch.stop)

    def _drifted_dirs(self, td, unit='ourliberty-x.timer'):
        r = _make_repo_with_content(Path(td), {unit: 'repo-content\n'})
        i = _make_installed_with_content(Path(td), {unit: 'stale-content\n'})
        return r, i

    def test_live_remediate_emits_healed_dm(self):
        with tempfile.TemporaryDirectory() as td:
            r, i = self._drifted_dirs(td)
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            healed = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': 'Sun 2026-06-03 18:00:00 MDT',
                'NextElapseUSecMonotonic': '1h',
                'LastTriggerUSec': '',
            }
            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                    mock.patch.object(h, '_systemctl_show', return_value=healed):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=False,
                )
        self.assertEqual(counts['content_drift'], 1)
        self.assertEqual(counts['content_healed'], 1)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(len(self._dm_calls), 1)
        dm = self._dm_calls[0]
        # Healed path must NOT reuse the imperative `install-drift:` subject —
        # that routes to the "run the install dance" URGENT copy. It carries a
        # distinct non-imperative healed subject instead.
        self.assertEqual(dm['subject'], 'content-healed:ourliberty-x.timer')
        # Routine successful heal → digest (no escalate DM).
        self.assertEqual(dm['route'], 'digest')
        self.assertIn('Auto-reconciled', dm['message'])
        self.assertIn('Sun 2026-06-03 18:00:00 MDT', dm['message'])
        # restart issued to re-anchor the timer.
        self.assertIn(
            ['sudo', '-n', 'systemctl', 'restart', 'ourliberty-x.timer'], ran,
        )

    def test_live_failure_falls_back_to_manual_dance(self):
        with tempfile.TemporaryDirectory() as td:
            r, i = self._drifted_dirs(td)

            def fake_run(cmd, **kwargs):
                if cmd[2] == 'cp':
                    return mock.MagicMock(returncode=0, stdout='', stderr='')
                if cmd[3] == 'daemon-reload':
                    return mock.MagicMock(
                        returncode=1, stdout='', stderr='bus down',
                    )
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            # _systemctl_show only matters for the (skipped) stuck-timer pass;
            # return a healthy timer so it is not classified stuck.
            healthy = {
                'ActiveState': 'active',
                'NextElapseUSecRealtime': 'Sun 2026-06-03 18:00:00 MDT',
                'NextElapseUSecMonotonic': '1h',
                'LastTriggerUSec': '',
            }
            state = {'units': {}, 'stuck_timers': {}}
            t0 = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                    mock.patch.object(h, '_systemctl_show', return_value=healthy):
                # Tick 1: reconcile fails but the drift is fresh → deferred.
                first = h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=False, now=t0,
                )
                self.assertEqual(first['dm_sent'], 0)
                self.assertEqual(first['dm_deferred_grace'], 1)
                # Tick 2: still drifted past the grace window → manual-dance page.
                counts = h.run_once(
                    repo_dir=r, installed_dir=i, state=state,
                    dry_run_override=False,
                    now=t0 + h.ESCALATE_GRACE + timedelta(seconds=1),
                )
        self.assertEqual(counts['content_drift'], 1)
        self.assertEqual(counts['content_healed'], 0)
        self.assertEqual(counts['dm_sent'], 1)
        dm = self._dm_calls[0]
        self.assertEqual(dm['subject'], 'install-drift:ourliberty-x.timer')
        self.assertIn('cp', dm['suggested_action'])
        self.assertIn('daemon-reload', dm['suggested_action'])

    def test_dry_run_single_activation_dm_no_shell_out(self):
        with tempfile.TemporaryDirectory() as td:
            r, i = self._drifted_dirs(td)
            ran: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                ran.append(list(cmd))
                return mock.MagicMock(returncode=0, stdout='', stderr='')

            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=True,
                )
        self.assertEqual(counts['content_drift'], 1)
        self.assertEqual(counts['content_healed'], 0)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(
            self._dm_calls[0]['subject'],
            'install-drift-healer: activate to receive missing-install alerts',
        )
        # No privileged (sudo) action in dry-run. A read-only `systemctl show`
        # from the stuck-timer pass is allowed; cp/restart/enable are not.
        sudo_cmds = [c for c in ran if c and c[0] == 'sudo']
        self.assertEqual(sudo_cmds, [])

    def test_kill_switch_blocks_content_drift(self):
        with tempfile.TemporaryDirectory() as td:
            kill = Path(td) / 'disable'
            kill.write_text('1')
            with mock.patch.object(h, 'KILL_SWITCH', kill):
                with tempfile.TemporaryDirectory() as td2:
                    r, i = self._drifted_dirs(td2)
                    counts = h.run_once(
                        repo_dir=r, installed_dir=i,
                        state={'units': {}, 'stuck_timers': {}},
                        dry_run_override=False,
                    )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(counts['content_drift'], 0)
        self.assertEqual(len(self._dm_calls), 0)


class ClassifyUnitTest(_IsolatedAgentsRoot):
    """`_classify_unit` 3-way classifier: `.timer` -> 'timer'; `.service`
    with `Type=oneshot` -> 'oneshot'; any other `.service` (Type=simple/notify
    /forking, or no explicit Type, or unreadable) -> 'long-running' (the safe
    default — a daemon-reload alone never restarts a resident daemon).
    """

    def _repo_with(self, unit: str, body: str) -> Path:
        td = Path(tempfile.mkdtemp(prefix='classify-'))
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        (td / unit).write_text(body)
        return td

    def test_timer_is_timer(self):
        td = self._repo_with('x.timer', '[Timer]\nOnCalendar=hourly\n')
        self.assertEqual(h._classify_unit('x.timer', repo_dir=td), 'timer')

    def test_oneshot_service(self):
        td = self._repo_with('x.service', '[Service]\nType=oneshot\n')
        self.assertEqual(h._classify_unit('x.service', repo_dir=td), 'oneshot')

    def test_simple_service_is_long_running(self):
        td = self._repo_with('x.service', '[Service]\nType=simple\n')
        self.assertEqual(
            h._classify_unit('x.service', repo_dir=td), 'long-running')

    def test_notify_service_is_long_running(self):
        td = self._repo_with('x.service', '[Service]\nType=notify\n')
        self.assertEqual(
            h._classify_unit('x.service', repo_dir=td), 'long-running')

    def test_no_type_defaults_long_running(self):
        # systemd defaults a typeless [Service] to Type=simple (resident).
        td = self._repo_with('x.service', '[Service]\nExecStart=/bin/true\n')
        self.assertEqual(
            h._classify_unit('x.service', repo_dir=td), 'long-running')

    def test_last_type_wins(self):
        # systemd uses the last assignment of a key; a oneshot overridden to
        # simple is resident.
        td = self._repo_with('x.service', '[Service]\nType=oneshot\nType=simple\n')
        self.assertEqual(
            h._classify_unit('x.service', repo_dir=td), 'long-running')

    def test_unreadable_service_falls_back_long_running(self):
        td = Path(tempfile.mkdtemp(prefix='classify-'))
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.assertEqual(
            h._classify_unit('missing.service', repo_dir=td), 'long-running')


class RenderRemediationByClassTest(_IsolatedAgentsRoot):
    """The render fns emit class-correct remediation text. `_classify_unit` is
    mocked per case (it reads the real REPO_SYSTEMD_DIR via a default arg bound
    at import; mocking decouples these from on-disk unit files).
    """

    def test_content_drift_long_running_says_restart(self):
        unit = 'ourliberty-inbox-watcher.service'
        with mock.patch.object(h, '_classify_unit', return_value='long-running'):
            _msg, _subj, sug = h._render_content_drift_dry_run(unit)
        self.assertIn(f'systemctl restart {unit}', sug)
        # Must NOT claim the next timer fire re-execs it.
        self.assertNotIn('next timer fire', sug)

    def test_content_drift_oneshot_no_restart(self):
        with mock.patch.object(h, '_classify_unit', return_value='oneshot'):
            _msg, _subj, sug = h._render_content_drift_dry_run('probe.service')
        self.assertNotIn('systemctl restart', sug)
        self.assertIn('daemon-reload', sug)

    def test_content_drift_timer_reanchors(self):
        with mock.patch.object(h, '_classify_unit', return_value='timer'):
            _msg, _subj, sug = h._render_content_drift_dry_run('x.timer')
        self.assertIn('systemctl restart x.timer', sug)

    def test_missing_install_long_running_starts_daemon(self):
        unit = 'ourliberty-inbox-watcher.service'
        with mock.patch.object(h, '_classify_unit', return_value='long-running'):
            _msg, _subj, sug = h._render_missing_install(unit)
        # A resident daemon must actually be started, not merely daemon-reloaded.
        self.assertIn(f'enable --now {unit}', sug)
        self.assertNotIn('activated by its timer', sug)

    def test_missing_install_oneshot_daemon_reload_only(self):
        with mock.patch.object(h, '_classify_unit', return_value='oneshot'):
            _msg, _subj, sug = h._render_missing_install('probe.service')
        self.assertIn('daemon-reload', sug)
        self.assertNotIn('enable --now', sug)

    def test_missing_install_oneshot_no_duplicate_daemon_reload(self):
        # Regression: the oneshot branch used to emit `daemon-reload` twice — the
        # template's fixed reload line plus an enable_line that was itself a
        # second daemon-reload, mislabelled 'oneshot service re-execs on its next
        # timer fire'. The dance must list daemon-reload exactly once.
        with mock.patch.object(h, '_classify_unit', return_value='oneshot'):
            _msg, _subj, sug = h._render_missing_install('probe.service')
        self.assertEqual(sug.count('daemon-reload'), 1, sug)

    def test_missing_install_timer_activated_no_duplicate_daemon_reload(self):
        # Same duplicate-reload bug on the timer-activated (long-running,
        # no-[Install]) branch.
        with mock.patch.object(h, '_classify_unit', return_value='long-running'), \
             mock.patch.object(h, '_activates_via_enable', return_value=False):
            _msg, _subj, sug = h._render_missing_install('ourliberty-cycle.service')
        self.assertEqual(sug.count('daemon-reload'), 1, sug)
        self.assertNotIn('enable --now', sug)

    def test_content_healed_long_running_no_next_fire(self):
        unit = 'ourliberty-inbox-watcher.service'
        with mock.patch.object(h, '_classify_unit', return_value='long-running'):
            msg, subj, _sug = h._render_content_healed(unit, 'unknown')
        self.assertIn('restarted', msg)
        self.assertNotIn('Next fire', msg)
        self.assertTrue(subj.startswith('content-healed:'))

    def test_content_healed_oneshot_mentions_next_fire(self):
        with mock.patch.object(h, '_classify_unit', return_value='oneshot'):
            msg, _subj, _sug = h._render_content_healed(
                'probe.service', 'Sat 2026-06-03 18:00:00 MDT')
        self.assertIn('next timer fire', msg)
        self.assertIn('Next fire', msg)


class HealedSubjectTranslationTest(_IsolatedAgentsRoot):
    """Bug A regression: the healthy auto-reconcile path emits a subject that
    resolves (via larry_alerts.translate_alert's longest-prefix lookup against
    the real config/alert-translations.json) to a NON-imperative, no-action
    entry — never the URGENT 'run the install dance' copy.
    """

    def test_content_healed_subject_resolves_to_no_action_entry(self):
        with mock.patch.object(h, '_classify_unit', return_value='timer'):
            _msg, subj, _sug = h._render_content_healed(
                'ourliberty-x.timer', 'unknown')
        entry = la.translate_alert('heal-systemd-install-drift', subj)
        self.assertIsNotNone(
            entry, f'healed subject {subj!r} has no translation entry')
        self.assertEqual(entry['severity'], 'INFO')
        self.assertEqual(entry['tier'], 'FYI')
        action = entry['recommended_action'].lower()
        self.assertIn('none', action)
        # No imperative install-dance phrasing.
        self.assertNotIn('run `sudo cp', action)
        self.assertNotIn('could not auto-install', action)

    def test_imperative_install_drift_subject_stays_urgent(self):
        # The genuinely-broken paths (_render_missing_install /
        # _render_content_drift_dry_run) keep the imperative subject and MUST
        # still resolve to the URGENT entry.
        entry = la.translate_alert(
            'heal-systemd-install-drift', 'install-drift:ourliberty-x.service')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['severity'], 'URGENT')
        self.assertEqual(entry['tier'], 'NOW')

    def test_install_resolved_subject_resolves_to_no_action_entry(self):
        # The stand-down subject must resolve to a non-imperative FYI entry, not
        # the URGENT install-dance copy.
        _msg, subj, _sug = h._render_install_resolved('ourliberty-x.timer')
        entry = la.translate_alert('heal-systemd-install-drift', subj)
        self.assertIsNotNone(
            entry, f'stand-down subject {subj!r} has no translation entry')
        self.assertEqual(entry['severity'], 'INFO')
        self.assertEqual(entry['tier'], 'FYI')
        action = entry['recommended_action'].lower()
        self.assertIn('none', action)
        self.assertNotIn('could not auto-install', action)


class RemediateMissingInstallClassAwareTest(_IsolatedAgentsRoot):
    """audit #2: a missing STANDALONE daemon (long-running .service WITH
    [Install], no sibling timer) must be enable --now'd + verified, not just cp'd
    — otherwise it is installed-but-dead yet reported 'install-healed' and
    de-duped from re-alerting. A timer-activated (no-[Install]) service still
    skips enable (its sibling timer starts it)."""

    def _recorder(self, returncode=0, stderr=''):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=returncode, stdout='', stderr=stderr)

        return calls, fake_run

    def test_standalone_daemon_is_enabled_now_and_verified(self):
        # A long-running service WITH [Install] (no sibling timer) must be started.
        calls, fake_run = self._recorder(returncode=0)
        with mock.patch.object(h, '_cp_and_reload', return_value=(0, '')), \
             mock.patch.object(h, '_activates_via_enable', return_value=True), \
             mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(h, '_systemctl_show', return_value={'ActiveState': 'active'}):
            rc, err = h._remediate_missing_install('ourliberty-inbox-watcher.service')
        self.assertEqual((rc, err), (0, ''))
        # The fix: a standalone daemon IS enable --now'd (it was not before).
        self.assertTrue(any('enable' in c and '--now' in c for c in calls), calls)

    def test_standalone_daemon_dead_after_enable_is_honest_failure(self):
        _, fake_run = self._recorder(returncode=0)
        with mock.patch.object(h, '_cp_and_reload', return_value=(0, '')), \
             mock.patch.object(h, '_activates_via_enable', return_value=True), \
             mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(h, '_systemctl_show', return_value={'ActiveState': 'failed'}):
            rc, err = h._remediate_missing_install('x.service')
        self.assertNotEqual(rc, 0)  # not a false 'install-healed'
        self.assertIn('ActiveState', err)

    def test_timer_activated_service_is_not_enabled(self):
        # A long-running service with NO [Install] is started by its sibling
        # timer — enable would fail; cp+daemon-reload is the right remediation.
        calls, fake_run = self._recorder(returncode=0)
        with mock.patch.object(h, '_cp_and_reload', return_value=(0, '')), \
             mock.patch.object(h, '_activates_via_enable', return_value=False), \
             mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rc, err = h._remediate_missing_install('ourliberty-cycle.service')
        self.assertEqual((rc, err), (0, ''))
        self.assertFalse(any('enable' in c for c in calls), calls)

    def test_timer_still_requires_next_elapse(self):
        _, fake_run = self._recorder(returncode=0)
        with mock.patch.object(h, '_cp_and_reload', return_value=(0, '')), \
             mock.patch.object(h, '_activates_via_enable', return_value=True), \
             mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(h, '_systemctl_show', return_value={'ActiveState': 'active'}):
            rc, err = h._remediate_missing_install('x.timer')
        self.assertNotEqual(rc, 0)  # active but no scheduled fire -> verify fails
        self.assertIn('NextElapse', err)

    def test_unit_has_install_section_reads_repo_file(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / 'daemon.service').write_text(
                '[Service]\nType=simple\n[Install]\nWantedBy=multi-user.target\n')
            (repo / 'cyclic.service').write_text('[Service]\nType=simple\n')  # no [Install]
            self.assertTrue(h._unit_has_install_section('daemon.service', repo_dir=repo))
            self.assertFalse(h._unit_has_install_section('cyclic.service', repo_dir=repo))
            self.assertFalse(h._unit_has_install_section('missing.service', repo_dir=repo))

    def test_activates_via_enable_logic(self):
        self.assertTrue(h._activates_via_enable('anything.timer'))
        with mock.patch.object(h, '_classify_unit', return_value='oneshot'):
            self.assertFalse(h._activates_via_enable('x.service'))
        with mock.patch.object(h, '_classify_unit', return_value='long-running'), \
             mock.patch.object(h, '_unit_has_install_section', return_value=True):
            self.assertTrue(h._activates_via_enable('x.service'))  # standalone daemon
        with mock.patch.object(h, '_classify_unit', return_value='long-running'), \
             mock.patch.object(h, '_unit_has_install_section', return_value=False):
            self.assertFalse(h._activates_via_enable('x.service'))  # timer-activated


class TriggeredEntrypointTest(_IsolatedAgentsRoot):
    """`main(['--triggered'])` is the post-merge sync hook: it must run EXACTLY
    one run_once() tick, emit a DISTINCT 'triggered' log line for audit, and
    pass NO dry_run_override (so every gate — kill-switch / allowlist / env /
    dedup — still governs remediate-vs-dry-run inside run_once)."""

    def test_triggered_runs_one_tick_and_logs_distinct_line(self):
        logs: list[str] = []
        with mock.patch.object(h, 'run_once', return_value={}) as run_once, \
             mock.patch.object(h, 'log', side_effect=lambda m, *a, **k: logs.append(m)):
            rc = h.main(['--triggered'])
        self.assertEqual(rc, 0)
        self.assertEqual(run_once.call_count, 1)
        # No dry_run_override forced — gates inside run_once stay authoritative.
        self.assertNotIn('dry_run_override', run_once.call_args.kwargs)
        self.assertTrue(
            any('triggered' in line for line in logs),
            f'expected a distinct triggered log line, got {logs!r}',
        )

    def test_once_alias_behaves_like_triggered(self):
        logs: list[str] = []
        with mock.patch.object(h, 'run_once', return_value={}) as run_once, \
             mock.patch.object(h, 'log', side_effect=lambda m, *a, **k: logs.append(m)):
            rc = h.main(['--once'])
        self.assertEqual(rc, 0)
        self.assertEqual(run_once.call_count, 1)
        self.assertTrue(any('triggered' in line for line in logs))

    def test_no_arg_run_does_not_log_triggered(self):
        logs: list[str] = []
        with mock.patch.object(h, 'run_once', return_value={}) as run_once, \
             mock.patch.object(h, 'log', side_effect=lambda m, *a, **k: logs.append(m)):
            rc = h.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(run_once.call_count, 1)
        self.assertFalse(any('triggered' in line for line in logs))

    def test_triggered_honors_kill_switch_one_tick(self):
        # End-to-end through real run_once: kill-switch present -> clean exit,
        # zero DMs, still exactly one tick. Proves the entrypoint doesn't bypass
        # the gate.
        # KILL_SWITCH is a module constant frozen at import from
        # OURLIBERTY_AGENTS_ROOT; if an earlier test in the run delenv'd that
        # var, the constant froze to the LIVE /home/larry/agents default, so
        # writing/unlinking h.KILL_SWITCH directly would touch the live healers
        # kill switch (order-fragile — and blocked EROFS under the isolation
        # wall). Patch it to a private tmp path like the sibling kill-switch
        # tests (test_kill_switch_exits_clean) so this test is order-independent
        # and never resolves the live tree.
        _kstmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(_kstmp, ignore_errors=True))
        kill = Path(_kstmp) / 'healers.disabled'
        kill.write_text('disabled')
        with mock.patch.object(h, 'KILL_SWITCH', kill), \
                mock.patch.object(h, 'dm_larry', return_value=True) as dm:
            rc = h.main(['--triggered'])
        self.assertEqual(rc, 0)
        self.assertEqual(dm.call_count, 0)


if __name__ == '__main__':
    unittest.main()
