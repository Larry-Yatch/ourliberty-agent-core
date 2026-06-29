"""Parity tests for scripts/marker_paths.py (Phase 2a S4 hardening).

watchdog.py WRITES the restart-coordination markers; medic_actions.py READS
them in _recent_peer_restart to defer to a peer already restarting a unit.
The path conventions used to be REPLICATED in both files -- a silent-drift
risk. They now live in the single shared marker_paths module.

These tests pin two things:

  1. GOLDEN SHAPE -- the shared builders produce the exact documented paths
     (name transforms + the three marker paths). A future move of the module
     that changes a path is caught here.
  2. CROSS-MODULE PARITY -- a marker WRITTEN at watchdog's path is READ by
     medic at the same path. This is the real anti-drift guarantee: if either
     side ever resolved a different path, the cross-read would miss the marker
     and the assertion would fail. We prove it behaviorally by writing the
     marker via watchdog's own builder and asserting medic's
     _recent_peer_restart blocks on it.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import marker_paths  # noqa: E402
import medic_actions  # noqa: E402
import watchdog  # noqa: E402

# The two watcher units Medic may restart (config/medic-reversible-targets.json
# restart_daemon_units; mirrors watchdog.AUTO_RESTART_SERVICES with the
# '.service' suffix that Medic carries on a unit name).
UNITS = ('ourliberty-inbox-watcher.service', 'ourliberty-outbox-notifier.service')


class SharedBuilderGoldenTest(unittest.TestCase):
    """Pin the exact documented path shapes so a silent move is caught."""

    ROOT = Path('/tmp/agents-root-fixture')

    def test_flap_marker_name_strips_suffix_keeps_prefix(self) -> None:
        self.assertEqual(
            marker_paths.flap_marker_name('ourliberty-inbox-watcher.service'),
            'ourliberty-inbox-watcher')
        # Idempotent on an already-suffixless name.
        self.assertEqual(
            marker_paths.flap_marker_name('ourliberty-inbox-watcher'),
            'ourliberty-inbox-watcher')

    def test_unit_short_name_strips_prefix_and_suffix(self) -> None:
        self.assertEqual(
            marker_paths.unit_short_name('ourliberty-inbox-watcher.service'),
            'inbox-watcher')
        self.assertEqual(
            marker_paths.unit_short_name('ourliberty-outbox-notifier.service'),
            'outbox-notifier')
        # A non-ourliberty unit keeps its (suffix-stripped) name.
        self.assertEqual(marker_paths.unit_short_name('foo.service'), 'foo')

    def test_golden_paths(self) -> None:
        state = self.ROOT / 'state'
        self.assertEqual(
            marker_paths.flap_streak_dir(self.ROOT),
            state / 'auto-restart-flap')
        self.assertEqual(
            marker_paths.flap_streak_path(self.ROOT, 'ourliberty-inbox-watcher'),
            state / 'auto-restart-flap' / 'ourliberty-inbox-watcher')
        self.assertEqual(
            marker_paths.mem_restart_cooldown_path(self.ROOT, 'inbox-watcher'),
            state / 'inbox-watcher-mem-restart-cooldown')
        self.assertEqual(
            marker_paths.reconcile_marker_path(self.ROOT, 'inbox-watcher'),
            state / 'inbox-watcher-reconcile-cooldown')

    def test_agents_root_accepts_str(self) -> None:
        # Builders coerce a str root to Path (callers may pass either).
        self.assertEqual(
            marker_paths.reconcile_marker_path('/tmp/x', 'inbox-watcher'),
            Path('/tmp/x/state/inbox-watcher-reconcile-cooldown'))


class WatchdogMedicParityTest(unittest.TestCase):
    """A marker written via watchdog's path builder is read by medic at the
    same path -- the cross-module anti-drift guarantee, proven behaviorally."""

    def setUp(self) -> None:
        self._tmp = Path('/tmp/marker-parity-fixture')
        # Point BOTH modules at one shared agents_root. watchdog froze
        # _FLAP_STREAK_DIR at import from the real home, so re-derive it via the
        # shared builder under the fixture root (the same patch the watchdog
        # suite uses).
        self._patches = [
            mock.patch.object(watchdog, 'AGENTS_ROOT', self._tmp),
            mock.patch.object(watchdog, '_FLAP_STREAK_DIR',
                              marker_paths.flap_streak_dir(self._tmp)),
            mock.patch.object(medic_actions, 'AGENTS_ROOT', self._tmp),
        ]
        for p in self._patches:
            p.start()
        (self._tmp / 'state').mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_flap_marker_parity(self) -> None:
        for unit in UNITS:
            service_name = marker_paths.flap_marker_name(unit)
            # watchdog writes via ITS builder.
            wpath = watchdog._flap_streak_path(service_name)
            wpath.parent.mkdir(parents=True, exist_ok=True)
            wpath.write_text('2')
            # medic reads at the same path -> blocks (defers to systemd).
            block, reason, _ = medic_actions._recent_peer_restart(unit)
            self.assertTrue(block, f'medic missed flap marker for {unit}')
            self.assertEqual(reason, medic_actions.REASON_FLAPPING)
            wpath.unlink()

    def test_mem_cooldown_parity(self) -> None:
        for unit in UNITS:
            short = marker_paths.unit_short_name(unit)
            # watchdog's mem-cooldown path for this short name.
            wpath = marker_paths.mem_restart_cooldown_path(watchdog.AGENTS_ROOT, short)
            wpath.parent.mkdir(parents=True, exist_ok=True)
            wpath.touch()  # touched now -> within the cooldown window
            block, reason, _ = medic_actions._recent_peer_restart(unit)
            self.assertTrue(block, f'medic missed mem-cooldown marker for {unit}')
            self.assertEqual(reason, medic_actions.REASON_RECENTLY_RESTARTED)
            wpath.unlink()

    def test_reconcile_marker_parity(self) -> None:
        for unit in UNITS:
            short = marker_paths.unit_short_name(unit)
            wpath = watchdog._reconcile_marker_path(short)
            wpath.parent.mkdir(parents=True, exist_ok=True)
            wpath.write_text(json.dumps(
                {'window_start': time.time(), 'count': 1, 'paged': False}))
            block, reason, _ = medic_actions._recent_peer_restart(unit)
            self.assertTrue(block, f'medic missed reconcile marker for {unit}')
            self.assertEqual(reason, medic_actions.REASON_RECENTLY_RESTARTED)
            wpath.unlink()

    def test_watchdog_builders_match_shared_module(self) -> None:
        # Direct path-equality between watchdog's exposed builders and the
        # shared module under the same root.
        for unit in UNITS:
            service_name = marker_paths.flap_marker_name(unit)
            short = marker_paths.unit_short_name(unit)
            self.assertEqual(
                watchdog._flap_streak_path(service_name),
                marker_paths.flap_streak_path(watchdog.AGENTS_ROOT, service_name))
            self.assertEqual(
                watchdog._reconcile_marker_path(short),
                marker_paths.reconcile_marker_path(watchdog.AGENTS_ROOT, short))


if __name__ == '__main__':
    unittest.main()
