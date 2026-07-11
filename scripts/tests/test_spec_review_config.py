#!/usr/bin/env python3
"""Tests for the spec-gauntlet foundations slice.

Covers agents/beacon/specs/spec-gauntlet-gate.md §3.6 (config + live override,
read FRESH per call, no module-level cache; fail-safe to disabled) and §3.5
(the `spec_review_round` chain-event type is registered so emit_event cannot
silently drop it).

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_spec_review_config
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import spec_review_config as src  # noqa: E402


class ConfigResolutionTest(unittest.TestCase):
    """Override -> runtime -> repo resolution, mirroring trust_policy."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.override = tmp / 'spec-review.override.json'
        self.runtime = tmp / 'config' / 'spec-review.json'
        self.repo = tmp / 'repo' / 'config' / 'spec-review.json'
        self.runtime.parent.mkdir(parents=True, exist_ok=True)
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        # Redirect the module's three path tiers at the tmp tree.
        self._orig = (
            src.OVERRIDE_CONFIG_PATH, src.RUNTIME_CONFIG_PATH, src.REPO_CONFIG_PATH)
        src.OVERRIDE_CONFIG_PATH = self.override
        src.RUNTIME_CONFIG_PATH = self.runtime
        src.REPO_CONFIG_PATH = self.repo
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        (src.OVERRIDE_CONFIG_PATH, src.RUNTIME_CONFIG_PATH,
         src.REPO_CONFIG_PATH) = self._orig

    @staticmethod
    def _write(path: Path, obj: dict):
        path.write_text(json.dumps(obj))

    def test_missing_everything_fails_safe_to_disabled(self):
        # No file at any tier -> DEFAULTS -> gate OFF (never blocks the pipeline).
        self.assertFalse(src.is_enabled())
        self.assertEqual(src.load_config()['max_rounds'], src.DEFAULTS['max_rounds'])

    def test_repo_is_lowest_tier(self):
        self._write(self.repo, {'enabled': True})
        self.assertTrue(src.is_enabled())

    def test_runtime_wins_over_repo(self):
        self._write(self.repo, {'enabled': True})
        self._write(self.runtime, {'enabled': False})
        self.assertFalse(src.is_enabled())

    def test_override_wins_over_all(self):
        self._write(self.repo, {'enabled': False})
        self._write(self.runtime, {'enabled': False})
        self._write(self.override, {'enabled': True})
        self.assertTrue(src.is_enabled())

    def test_partial_override_overlays_defaults(self):
        # A minimal override ({"enabled": true}) still yields a complete config.
        self._write(self.override, {'enabled': True})
        cfg = src.load_config()
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['per_step_ceiling_s'], src.DEFAULTS['per_step_ceiling_s'])
        self.assertEqual(cfg['gated_sites'], src.DEFAULTS['gated_sites'])

    def test_malformed_override_fails_safe_to_disabled(self):
        self.override.write_text('{not valid json')
        # Malformed -> DEFAULTS (disabled), never raises, never fail-open ON.
        self.assertFalse(src.is_enabled())

    def test_non_bool_enabled_is_disabled(self):
        # The `enabled: "false"` truthy-string trap must NOT enable the gate.
        self._write(self.override, {'enabled': 'true'})
        self.assertFalse(src.is_enabled())

    def test_read_is_cache_free_across_calls(self):
        # AC-3: flipping the override takes effect on the very next call, with no
        # process restart and no module-level cache to invalidate.
        self._write(self.override, {'enabled': False})
        self.assertFalse(src.is_enabled())
        self._write(self.override, {'enabled': True})
        self.assertTrue(src.is_enabled())      # picked up WITHOUT reimport
        self._write(self.override, {'enabled': False})
        self.assertFalse(src.is_enabled())     # and flips back the same way

    def test_gated_sites_helper(self):
        self._write(self.override, {'gated_sites': ['bot_chat']})
        self.assertEqual(src.gated_sites(), ['bot_chat'])


class EventTypeRegistrationTest(unittest.TestCase):
    """§3.5: the round event type must be registered or emit_event drops it."""

    def test_spec_review_round_registered(self):
        import chain_event_shipper as ces
        self.assertIn('spec_review_round', ces.KNOWN_EVENT_TYPES)


if __name__ == '__main__':
    unittest.main()
