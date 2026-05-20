#!/usr/bin/env python3
"""Tests for heal_systemd_install_drift (E1.5.2).

Covers repo/installed scan, drift detection (repo has unit, installed
dir does not), state dedup, dry-run activation pattern, and reconciliation GC.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_systemd_install_drift
"""
from __future__ import annotations

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


class ListUnitsTest(unittest.TestCase):
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


class DetectDriftTest(unittest.TestCase):
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


class DedupTest(unittest.TestCase):
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


class OrchestrationTest(unittest.TestCase):
    def setUp(self):
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


if __name__ == '__main__':
    unittest.main()
