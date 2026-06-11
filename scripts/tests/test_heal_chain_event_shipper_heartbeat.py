#!/usr/bin/env python3
"""Tests for heal_chain_event_shipper_heartbeat.py.

Covers the systemd-aware intentional-off suppression added 2026-06-04: the
shipper ships DEFAULT-OFF behind its activation gate and never writes a
heartbeat, so the pre-fix healer DM'd Larry forever. `intentionally_off()`
now reads the gate / service state (read-only) before alarming, and fails
LOUD on any probe error.

All systemctl access is mocked; no real systemd runs.

Run:
    python3 -m unittest scripts.tests.test_heal_chain_event_shipper_heartbeat
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_chain_event_shipper_heartbeat as h  # noqa: E402


def _fake_systemctl(mapping):
    return lambda args: mapping.get(tuple(args), (-1, ''))


ENV_PROBE = ('show', h.SHIPPER_SERVICE, '-p', 'Environment')
ENABLED_PROBE = ('is-enabled', h.SHIPPER_SERVICE)


class IntentionallyOffTest(unittest.TestCase):
    def test_gate_closed_suppresses(self):
        m = {ENV_PROBE: (0, 'Environment=OURLIBERTY_CHAIN_SHIPPER_ENABLED=false')}
        with mock.patch.object(h, '_systemctl', _fake_systemctl(m)):
            off, reason = h.intentionally_off()
        self.assertTrue(off)
        self.assertIn('gate closed', reason)

    def test_gate_open_does_not_suppress(self):
        m = {
            ENV_PROBE: (0, 'Environment=OURLIBERTY_CHAIN_SHIPPER_ENABLED=true'),
            ENABLED_PROBE: (0, 'enabled'),
        }
        with mock.patch.object(h, '_systemctl', _fake_systemctl(m)):
            off, _ = h.intentionally_off()
        self.assertFalse(off)

    def test_service_disabled_suppresses(self):
        m = {ENV_PROBE: (0, 'Environment='), ENABLED_PROBE: (1, 'disabled')}
        with mock.patch.object(h, '_systemctl', _fake_systemctl(m)):
            off, reason = h.intentionally_off()
        self.assertTrue(off)
        self.assertIn('disabled', reason)

    def test_probe_failure_fails_loud(self):
        with mock.patch.object(h, '_systemctl', lambda args: (-1, '')):
            off, _ = h.intentionally_off()
        self.assertFalse(off)


class MainSuppressionTest(unittest.TestCase):
    def _run_main(self, *, stale, off):
        with mock.patch.object(h, 'kill_switch_active', return_value=False), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'check_staleness',
                               return_value=(stale, 999.0, 'r')), \
             mock.patch.object(h, 'intentionally_off', return_value=off), \
             mock.patch.object(h, '_dm_larry', return_value=True) as dm:
            rc = h.main()
        return rc, dm

    def test_suppresses_dm_when_gate_closed(self):
        rc, dm = self._run_main(stale=True, off=(True, 'gate closed'))
        self.assertEqual(rc, 0)
        dm.assert_not_called()

    def test_dms_when_gate_open_and_stale(self):
        rc, dm = self._run_main(stale=True, off=(False, ''))
        self.assertEqual(rc, 0)
        dm.assert_called_once()


if __name__ == '__main__':
    unittest.main()
