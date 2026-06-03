#!/usr/bin/env python3
"""Tests for heal_pulse_check_staleness (the pulse-check liveness watcher).

Covers:
  - fresh vs stale heartbeat staleness math (cadence + grace)
  - missing / malformed cadence entry => fail-closed pulse-check-no-cadence
  - event_driven entry => never time-stale
  - no heartbeat yet => bootstrap fallback to the newest proposal artifact
  - heartbeat ts parsing + mtime fallback for legacy/non-JSON content
  - discover_heartbeat_ids catches an unregistered check on disk
  - load_cadence_config raises loudly on unreadable / shape-invalid JSON
  - the shipped config covers every canonical check (no prod false-fire)
  - main() routes stale alerts through larry_alerts

larry_alerts is stubbed in the main()-level test — no real queue is touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_pulse_check_staleness
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_pulse_check_staleness as w  # noqa: E402


NOW = 1_750_000_000.0  # fixed epoch for deterministic staleness math
HOUR = 3600.0


def _write_heartbeat(bb: Path, check_id: str, age_hours: float) -> Path:
    bb.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(NOW - age_hours * HOUR, tz=timezone.utc)
    path = bb / f'pulse-check-{check_id}.heartbeat'
    path.write_text(json.dumps({'ts': ts.isoformat(), 'check': check_id}))
    return path


def _write_artifact(bb: Path, rel: str, age_hours: float) -> Path:
    path = bb / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{}')
    mtime = NOW - age_hours * HOUR
    os.utime(path, (mtime, mtime))
    return path


class StalenessMathTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.bb = Path(self._td.name) / 'blackboard'
        self.bb.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_fresh_heartbeat_no_alert(self):
        _write_heartbeat(self.bb, 'ix', age_hours=10)
        entry = {'cadence_hours': 168, 'grace_hours': 36}
        self.assertIsNone(w.evaluate_check('ix', entry, self.bb, NOW))

    def test_stale_heartbeat_alerts(self):
        _write_heartbeat(self.bb, 'ix', age_hours=240)  # 10 days > 168+36h
        entry = {'cadence_hours': 168, 'grace_hours': 36, 'label': 'Friction'}
        alert = w.evaluate_check('ix', entry, self.bb, NOW)
        self.assertIsNotNone(alert)
        self.assertEqual(alert['subject'], 'pulse-check-stale:ix')
        self.assertIn('Friction', alert['message'])

    def test_exactly_at_threshold_not_stale(self):
        _write_heartbeat(self.bb, 'iv', age_hours=204)  # == 168+36, boundary
        entry = {'cadence_hours': 168, 'grace_hours': 36}
        self.assertIsNone(w.evaluate_check('iv', entry, self.bb, NOW))

    def test_missing_entry_fail_closed(self):
        alert = w.evaluate_check('iv', None, self.bb, NOW)
        self.assertIsNotNone(alert)
        self.assertEqual(alert['subject'], 'pulse-check-no-cadence:iv')

    def test_malformed_cadence_fail_closed(self):
        for bad in ({'cadence_hours': 0}, {'cadence_hours': 'soon'},
                    {'grace_hours': 5}):
            alert = w.evaluate_check('v', bad, self.bb, NOW)
            self.assertIsNotNone(alert, bad)
            self.assertEqual(alert['subject'], 'pulse-check-no-cadence:v')

    def test_event_driven_never_stale(self):
        # No heartbeat at all, yet event-driven => no alert.
        entry = {'event_driven': True, 'label': 'Cost ceiling'}
        self.assertIsNone(w.evaluate_check('vii', entry, self.bb, NOW))

    def test_no_signal_at_all_alerts_stale(self):
        entry = {'cadence_hours': 168, 'grace_hours': 36}
        alert = w.evaluate_check('x', entry, self.bb, NOW)
        self.assertIsNotNone(alert)
        self.assertEqual(alert['subject'], 'pulse-check-stale:x')
        self.assertIn('never', alert['message'].lower())


class BootstrapFallbackTest(unittest.TestCase):
    """No heartbeat yet (first deploy) -> fall back to the proposal artifact."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.bb = Path(self._td.name) / 'blackboard'
        self.bb.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_recent_artifact_keeps_check_fresh(self):
        _write_artifact(self.bb, 'pulse-check-ix-proposals/check-ix-2026.json',
                        age_hours=20)
        entry = {'cadence_hours': 168, 'grace_hours': 36}
        self.assertIsNone(w.evaluate_check('ix', entry, self.bb, NOW))

    def test_old_artifact_is_stale(self):
        _write_artifact(self.bb, 'pulse-check-ix-proposals/check-ix-old.json',
                        age_hours=300)
        entry = {'cadence_hours': 168, 'grace_hours': 36}
        alert = w.evaluate_check('ix', entry, self.bb, NOW)
        self.assertIsNotNone(alert)
        self.assertEqual(alert['subject'], 'pulse-check-stale:ix')

    def test_heartbeat_dominates_when_newer_than_artifact(self):
        _write_artifact(self.bb, 'pulse-check-ix-proposals/check-ix-old.json',
                        age_hours=300)
        _write_heartbeat(self.bb, 'ix', age_hours=5)  # newer => fresh
        entry = {'cadence_hours': 168, 'grace_hours': 36}
        self.assertIsNone(w.evaluate_check('ix', entry, self.bb, NOW))

    def test_newest_artifact_wins(self):
        _write_artifact(self.bb, 'pulse-check-i/check-i-old.json',
                        age_hours=300)
        _write_artifact(self.bb, 'pulse-check-i/check-i-new.json',
                        age_hours=10)
        entry = {'cadence_hours': 48, 'grace_hours': 24}
        self.assertIsNone(w.evaluate_check('i', entry, self.bb, NOW))


class HeartbeatParseTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.bb = Path(self._td.name) / 'blackboard'
        self.bb.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_json_ts_used(self):
        path = _write_heartbeat(self.bb, 'ix', age_hours=12)
        ts = w._read_heartbeat_ts(path)
        self.assertAlmostEqual(ts, NOW - 12 * HOUR, delta=2)

    def test_non_json_falls_back_to_mtime(self):
        path = self.bb / 'pulse-check-iii.heartbeat'
        path.write_text('2026-06-03T00:00:00+00:00')  # legacy plain-ts shape
        os.utime(path, (NOW - 5 * HOUR, NOW - 5 * HOUR))
        ts = w._read_heartbeat_ts(path)
        self.assertAlmostEqual(ts, NOW - 5 * HOUR, delta=2)


class DiscoveryAndConfigTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.bb = Path(self._td.name) / 'blackboard'
        self.bb.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_discover_unregistered_check_alerts(self):
        # A heartbeat for a check that isn't in the (empty) config -> no-cadence.
        _write_heartbeat(self.bb, 'xi', age_hours=1)
        alerts = w.evaluate_all({}, self.bb, now=NOW)
        subjects = {a['subject'] for a in alerts}
        self.assertIn('pulse-check-no-cadence:xi', subjects)

    def test_evaluate_all_fail_closed_on_empty_config(self):
        alerts = w.evaluate_all({}, self.bb, now=NOW)
        subjects = {a['subject'] for a in alerts}
        for cid in w.CANONICAL_CHECKS:
            self.assertIn(f'pulse-check-no-cadence:{cid}', subjects)

    def test_load_config_raises_on_bad_json(self):
        p = Path(self._td.name) / 'bad.json'
        p.write_text('{ not json')
        with self.assertRaises(ValueError):
            w.load_cadence_config(p)

    def test_load_config_raises_without_checks_object(self):
        p = Path(self._td.name) / 'noChecks.json'
        p.write_text('{"_schema": {}}')
        with self.assertRaises(ValueError):
            w.load_cadence_config(p)


class ShippedConfigTest(unittest.TestCase):
    """The shipped registry must cover every canonical check, or production
    would fire a fail-closed no-cadence alert for the gap."""

    def test_real_config_covers_canonical_set(self):
        cfg = w.load_cadence_config()  # default CONFIG_FILE
        for cid in w.CANONICAL_CHECKS:
            self.assertIn(cid, cfg, f'check {cid} missing from cadence config')
            entry = cfg[cid]
            if entry.get('event_driven'):
                continue
            self.assertIsInstance(entry.get('cadence_hours'), (int, float),
                                  f'{cid} cadence_hours not numeric')
            self.assertGreater(entry['cadence_hours'], 0, cid)


class _FakeAlerts:
    def __init__(self):
        self.calls = []

    def append_alert(self, **kw):
        self.calls.append(kw)
        return True


class MainEmitTest(unittest.TestCase):
    def setUp(self):
        # Hermetic: patch.dict restores os.environ and sys.modules to their
        # exact prior state on cleanup (re-installing the genuine larry_alerts
        # module rather than popping it), so this file can't leak its stub into
        # later tests under `unittest discover` collection order.
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        (root / 'blackboard').mkdir(parents=True)

        env_patch = mock.patch.dict(
            os.environ, {'OURLIBERTY_AGENTS_ROOT': str(root)}
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self._fake = _FakeAlerts()
        mod_patch = mock.patch.dict(sys.modules, {'larry_alerts': self._fake})
        mod_patch.start()
        self.addCleanup(mod_patch.stop)

    def test_main_emits_stale_alerts(self):
        # Empty blackboard => every canonical check has no signal => stale,
        # plus the real config drives them. main() should fire via larry_alerts.
        rc = w.main()
        self.assertEqual(rc, 0)
        self.assertTrue(self._fake.calls, 'expected at least one alert emitted')
        for call in self._fake.calls:
            self.assertEqual(call['source'], 'heal-pulse-check-staleness')
            self.assertEqual(call['severity'], 'warning')

    def test_kill_switch_short_circuits(self):
        (Path(self._td.name) / 'healers.disabled').write_text('')
        rc = w.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._fake.calls, [])


if __name__ == '__main__':
    unittest.main()
