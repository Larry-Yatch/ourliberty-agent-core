#!/usr/bin/env python3
"""Tests for heal_systemd_install_drift (E1.5.2).

Covers repo/installed scan, drift detection (repo has unit, installed
dir does not), state dedup, dry-run activation pattern, and reconciliation GC.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_systemd_install_drift
"""
from __future__ import annotations

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

        def fake_dm(message, subject, suggested_action, severity='warning'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
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
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.service', 'b.timer'])
            i = _make_installed(Path(td), [])
            counts = h.run_once(
                repo_dir=r, installed_dir=i,
                state={'units': {}}, dry_run_override=False,
            )
            self.assertEqual(counts['dm_sent'], 2)
            subjects = {c['subject'] for c in self._dm_calls}
            self.assertIn('install-drift:a.service', subjects)
            self.assertIn('install-drift:b.timer', subjects)

    def test_suggested_action_includes_install_commands(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['b.timer'])
            i = _make_installed(Path(td), [])
            h.run_once(
                repo_dir=r, installed_dir=i,
                state={'units': {}}, dry_run_override=False,
            )
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
            now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=now,
            )
            self._dm_calls.clear()
            counts = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=now + timedelta(hours=1),
            )
            self.assertEqual(counts['dm_sent'], 0)
            self.assertEqual(counts['dm_suppressed_dedup'], 1)

    def test_after_window_re_dms(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
            h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=now,
            )
            self._dm_calls.clear()
            counts = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False, now=now + timedelta(hours=13),
            )
            self.assertEqual(counts['dm_sent'], 1)

    def test_reconciled_unit_garbage_collected(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), ['a.timer'])
            i = _make_installed(Path(td), [])
            state = {'units': {}}
            h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False,
            )
            self.assertIn('a.timer', state['units'])
            # Operator installs it.
            (Path(i) / 'a.timer').write_text('# a.timer\n')
            counts = h.run_once(
                repo_dir=r, installed_dir=i, state=state,
                dry_run_override=False,
            )
            self.assertNotIn('a.timer', state['units'])
            self.assertEqual(counts['reconciled_gc'], 1)

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

        def fake_dm(message, subject, suggested_action, severity='warning'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
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
                if c['subject'] == 'stuck-timer:ourliberty-cycle.timer'
            ]
            self.assertEqual(len(stuck_dms), 1)
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
    .timer = cp + daemon-reload + enable --now + verify; .service = cp +
    daemon-reload (no enable, no verify).
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

    def test_service_skips_enable(self):
        def plan(cmd):
            return mock.MagicMock(returncode=0, stdout='', stderr='')
        ran, fake_run = self._fake_runs_record(plan)
        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(h, '_systemctl_show', return_value={}):
            rc, stderr = h._remediate_missing_install('foo.service')
        self.assertEqual(rc, 0)
        # cp + daemon-reload only — no enable line for a .service.
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


class InstallRemediationOrchestrationTest(_IsolatedAgentsRoot):
    """`run_once()` missing-install loop, allowlist-active path: remediate-then-
    notify on success, fallback alert on failure, .timer-vs-.service enable
    difference, dry-run unchanged.
    """

    def setUp(self):
        super().setUp()
        self._dm_calls: list[dict] = []

        def fake_dm(message, subject, suggested_action, severity='warning'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
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
        self.assertEqual(dm['subject'], 'install-drift:ourliberty-x.timer')
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

            with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
                counts = h.run_once(
                    repo_dir=r, installed_dir=i,
                    state={'units': {}, 'stuck_timers': {}},
                    dry_run_override=False,
                )

        self.assertEqual(counts['install_healed'], 0)
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(len(self._dm_calls), 1)
        dm = self._dm_calls[0]
        self.assertEqual(dm['subject'], 'install-drift:ourliberty-x.timer')
        # Manual-dance fallback: cp + daemon-reload + enable --now in suggested.
        self.assertIn('cp', dm['suggested_action'])
        self.assertIn('daemon-reload', dm['suggested_action'])
        self.assertIn('enable --now', dm['suggested_action'])

    def test_timer_enable_line_present_service_not(self):
        with tempfile.TemporaryDirectory() as td:
            r = _make_repo_systemd(Path(td), [
                'svc.service', 'svc.timer',
            ])
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


if __name__ == '__main__':
    unittest.main()
