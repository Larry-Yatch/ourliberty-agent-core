#!/usr/bin/env python3
"""Tests for daemon_restart_manifest (deploy-restart-gap-001, 2026-06-03).

Covers the brief's two required test families:
  1. Changed-file -> unit mapping (unrelated / entrypoint / imported-module /
     shared-base each-once).
  2. Manifest completeness: every persistent Type=simple daemon has an entry
     (fail if a daemon is unmapped — enforcement).

Plus manifest integrity (every watch_path exists; entrypoint self-included)
and the healer-side union that folds the manifest into the stale-daemon
watchlist.

Run:
    python3 -m unittest scripts.tests.test_daemon_restart_manifest
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import daemon_restart_manifest as m  # noqa: E402

_REPO_ROOT = _REPO_SCRIPTS.parent


# -------------------- pure-logic mapping (synthetic manifest) --------------------

def _synthetic_manifest() -> dict:
    """A small, hand-built manifest exercising the mapping logic in
    isolation: three units, one shared base imported by two of them."""
    return {
        'units': {
            'ourliberty-alpha.service': {
                'entrypoint': 'scripts/alpha.py',
                'watch_paths': ['scripts/alpha.py', 'scripts/shared_base.py'],
            },
            'ourliberty-bravo.service': {
                'entrypoint': 'scripts/bravo.py',
                'watch_paths': ['scripts/bravo.py', 'scripts/shared_base.py'],
            },
            'ourliberty-charlie.service': {
                'entrypoint': 'scripts/charlie.py',
                'watch_paths': ['scripts/charlie.py'],
            },
        }
    }


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.man = _synthetic_manifest()

    def test_unrelated_change_restarts_nothing(self):
        self.assertEqual(
            m.units_for_changed_paths(['docs/readme.md', 'config/x.json'], self.man),
            [],
        )

    def test_empty_changeset_restarts_nothing(self):
        self.assertEqual(m.units_for_changed_paths([], self.man), [])

    def test_entrypoint_change_restarts_that_unit(self):
        self.assertEqual(
            m.units_for_changed_paths(['scripts/charlie.py'], self.man),
            ['ourliberty-charlie.service'],
        )

    def test_imported_module_change_restarts_that_unit(self):
        # alpha imports shared_base but not bravo's entrypoint; a change to
        # alpha.py alone restarts only alpha.
        self.assertEqual(
            m.units_for_changed_paths(['scripts/alpha.py'], self.man),
            ['ourliberty-alpha.service'],
        )

    def test_shared_base_change_restarts_all_dependents_once(self):
        result = m.units_for_changed_paths(['scripts/shared_base.py'], self.man)
        self.assertEqual(
            result,
            ['ourliberty-alpha.service', 'ourliberty-bravo.service'],
        )
        # each-once: no duplicates even though both share the base.
        self.assertEqual(len(result), len(set(result)))

    def test_mixed_change_unions_dependents(self):
        result = m.units_for_changed_paths(
            ['scripts/charlie.py', 'scripts/shared_base.py', 'docs/x.md'],
            self.man,
        )
        self.assertEqual(
            result,
            [
                'ourliberty-alpha.service',
                'ourliberty-bravo.service',
                'ourliberty-charlie.service',
            ],
        )

    def test_malformed_manifest_is_safe(self):
        self.assertEqual(m.units_for_changed_paths(['scripts/a.py'], {}), [])
        self.assertEqual(
            m.units_for_changed_paths(['scripts/a.py'], {'units': 'nope'}), [],
        )


# -------------------- completeness + integrity (real committed manifest) ------

class CommittedManifestTests(unittest.TestCase):
    """Enforcement against the real systemd/ unit files + committed JSON."""

    def setUp(self):
        self.manifest = m.load_manifest()
        self.assertTrue(self.manifest, 'committed manifest failed to load')
        self.units = m.manifest_units(self.manifest)

    def test_every_persistent_simple_daemon_is_mapped(self):
        expected = set(m.persistent_simple_units().keys())
        actual = set(self.units.keys())
        missing = expected - actual
        extra = actual - expected
        self.assertEqual(
            missing, set(),
            f'persistent Type=simple daemons missing a manifest entry: {missing}',
        )
        self.assertEqual(
            extra, set(),
            f'manifest entries that are not persistent Type=simple daemons: {extra}',
        )

    def test_timer_driven_simple_units_are_excluded(self):
        # ourliberty-cycle.service is Type=simple but timer-driven; it must
        # NOT appear in the manifest (it re-reads code on each fire).
        self.assertNotIn('ourliberty-cycle.service', self.units)

    def test_every_watch_path_exists_on_disk(self):
        for unit, spec in self.units.items():
            for rel in spec['watch_paths']:
                self.assertTrue(
                    (_REPO_ROOT / rel).is_file(),
                    f'{unit}: watch_path {rel} does not exist on disk',
                )

    def test_entrypoint_is_in_its_own_watch_paths(self):
        for unit, spec in self.units.items():
            self.assertIn(
                spec['entrypoint'], spec['watch_paths'],
                f'{unit}: entrypoint not present in its own watch_paths',
            )

    def test_committed_manifest_matches_regeneration(self):
        # Guards against a hand-edit drifting from the scan-derived truth.
        # If a real import was added, `regenerate` must be re-run + committed.
        rebuilt = m.build_manifest()['units']
        for unit, spec in rebuilt.items():
            self.assertIn(unit, self.units, f'{unit} missing from committed manifest')
            self.assertEqual(
                sorted(spec['watch_paths']),
                sorted(self.units[unit]['watch_paths']),
                f'{unit}: committed watch_paths drifted from import scan; '
                f're-run `python3 scripts/daemon_restart_manifest.py regenerate`',
            )


# -------------------- healer-side union --------------------

class HealerWatchlistUnionTests(unittest.TestCase):
    def test_manifest_folds_into_healer_watchlist(self):
        import heal_stale_daemon_code as h
        eff = h.build_effective_watchlist()
        # shared-base entrypoint (3 bots) must be present from the manifest.
        atb = [p for p in eff if p.name == 'agent_telegram_bot.py']
        self.assertTrue(atb, 'agent_telegram_bot.py absent from effective watchlist')
        self.assertEqual(
            eff[atb[0]],
            {
                'ourliberty-forge-bot.service',
                'ourliberty-mirror-bot.service',
                'ourliberty-pulse-bot.service',
            },
        )

    def test_hardcoded_subprocess_dep_preserved(self):
        # marker.py is invoked as a subprocess (not imported), so the import
        # scan won't include it; the hardcoded watchlist must still cover it.
        import heal_stale_daemon_code as h
        eff = h.build_effective_watchlist()
        mk = [p for p in eff if p.name == 'marker.py']
        self.assertTrue(mk, 'marker.py dropped from effective watchlist')
        self.assertIn('ourliberty-outbox-notifier.service', eff[mk[0]])


if __name__ == '__main__':
    unittest.main()
