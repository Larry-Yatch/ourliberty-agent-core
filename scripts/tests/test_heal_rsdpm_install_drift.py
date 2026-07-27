#!/usr/bin/env python3
"""Tests for heal_rsdpm_install_drift.

Covers the three required behaviors — baseline-adopt-on-first-run,
drift-detected-alert-once, no-alert-when-clean — plus mode/owner drift, the
cheap node22 signal, and dry-run activation. All fixtures are /tmp-rooted; the
real install at /usr/local/lib/rsdpm is NEVER touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_rsdpm_install_drift
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
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_rsdpm_install_drift as h  # noqa: E402


def _make_install(td: Path) -> Path:
    """Build a /tmp fixture mirroring /usr/local/lib/rsdpm: the three tracked
    scripts + a small node22/ tree with a bin/node sentinel."""
    root = td / 'rsdpm'
    root.mkdir()
    for name in h.TRACKED_SCRIPTS:
        p = root / name
        p.write_text(f'#!/usr/bin/env bash\n# {name}\necho ok\n')
        p.chmod(0o755)
    node_bin = root / h.NODE22_SUBDIR / 'bin'
    node_bin.mkdir(parents=True)
    (node_bin / 'node').write_bytes(b'\x7fELF fake node binary payload')
    lib = root / h.NODE22_SUBDIR / 'lib'
    lib.mkdir()
    (lib / 'node_modules.txt').write_text('vendored\n')
    return root


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test so the
    module-level STATE_FILE / LOG_FILE / HEARTBEAT_FILE / KILL_SWITCH never
    touch prod ~/agents state. Reload the module to rebind the constants."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='agents-root-rsdpm-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self._tmp, sub), exist_ok=True)
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp
        importlib.reload(h)

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

    def tearDown(self):
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(h)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()


class FingerprintTest(_IsolatedAgentsRoot):
    def test_manifest_captures_scripts_and_node22(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            m = h.build_manifest(inst)
            for name in h.TRACKED_SCRIPTS:
                self.assertTrue(m['scripts'][name]['present'])
                self.assertTrue(m['scripts'][name]['sha256'])
                self.assertEqual(m['scripts'][name]['mode'], '0o755')
            self.assertTrue(m['node22']['present'])
            self.assertTrue(m['node22']['node_bin_sha256'])
            self.assertGreater(m['node22']['file_count'], 0)
            self.assertGreater(m['node22']['total_bytes'], 0)

    def test_missing_script_marked_absent(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            (inst / 'refresh.sh').unlink()
            m = h.build_manifest(inst)
            self.assertFalse(m['scripts']['refresh.sh']['present'])


class DetectDriftTest(_IsolatedAgentsRoot):
    def test_no_drift_identical_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            base = h.build_manifest(inst)
            cur = h.build_manifest(inst)
            self.assertEqual(h.detect_drift(base, cur), [])

    def test_content_drift_detected(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            base = h.build_manifest(inst)
            (inst / 'drift-check.sh').write_text('# tampered\n')
            cur = h.build_manifest(inst)
            drifts = h.detect_drift(base, cur)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0]['target'], 'drift-check.sh')
            self.assertIn('content', drifts[0]['kinds'])

    def test_mode_drift_detected(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            base = h.build_manifest(inst)
            (inst / 'alert-emit.py').chmod(0o777)
            cur = h.build_manifest(inst)
            drifts = h.detect_drift(base, cur)
            self.assertEqual(len(drifts), 1)
            self.assertIn('mode', drifts[0]['kinds'])

    def test_node22_size_drift_detected(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            base = h.build_manifest(inst)
            (inst / h.NODE22_SUBDIR / 'lib' / 'extra.txt').write_text('new\n')
            cur = h.build_manifest(inst)
            drifts = h.detect_drift(base, cur)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0]['target'], 'node22/')
            self.assertTrue({'file_count', 'size'} & set(drifts[0]['kinds']))


class RunOnceTest(_IsolatedAgentsRoot):
    def test_first_run_adopts_baseline_no_alert(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            state = {'baseline': None, 'dedup': {}}
            counts = h.run_once(install_dir=inst, state=state,
                                dry_run_override=False)
            self.assertEqual(counts['baseline_adopted'], 1)
            self.assertEqual(counts['dm_sent'], 0)
            self.assertEqual(self._dm_calls, [])
            self.assertIsNotNone(state['baseline'])

    def test_no_alert_when_clean(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            base = h.build_manifest(inst)
            state = {'baseline': base, 'dedup': {}}
            counts = h.run_once(install_dir=inst, state=state,
                                dry_run_override=False)
            self.assertEqual(counts['clean'], 1)
            self.assertEqual(counts['dm_sent'], 0)
            self.assertEqual(self._dm_calls, [])

    def test_drift_alerts_exactly_once_then_adopts(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            base = h.build_manifest(inst)
            state = {'baseline': base, 'dedup': {}}
            # Introduce content drift.
            (inst / 'alert-emit.py').write_text('# malicious swap\n')

            counts = h.run_once(install_dir=inst, state=state,
                                dry_run_override=False)
            self.assertEqual(counts['drift_detected'], 1)
            self.assertEqual(counts['dm_sent'], 1)
            self.assertEqual(len(self._dm_calls), 1)
            self.assertIn('alert-emit.py', self._dm_calls[0]['message'])
            self.assertEqual(counts['baseline_readopted'], 1)

            # Second tick against the SAME drifted install: baseline was adopted,
            # so there is no drift and no second alert (notify-once-then-adopt).
            counts2 = h.run_once(install_dir=inst, state=state,
                                 dry_run_override=False)
            self.assertEqual(counts2['clean'], 1)
            self.assertEqual(counts2['dm_sent'], 0)
            self.assertEqual(len(self._dm_calls), 1)

    def test_dry_run_sends_activation_notice_and_keeps_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            base = h.build_manifest(inst)
            state = {'baseline': base, 'dedup': {}}
            (inst / 'refresh.sh').write_text('# changed\n')

            counts = h.run_once(install_dir=inst, state=state,
                                dry_run_override=True)
            self.assertEqual(counts['drift_detected'], 1)
            self.assertEqual(len(self._dm_calls), 1)
            self.assertIn('activate', self._dm_calls[0]['subject'])
            # Baseline is NOT adopted in dry-run: an activated run must still see
            # the drift and page it.
            self.assertEqual(state['baseline'], base)

    def test_kill_switch_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            inst = _make_install(Path(td))
            kill = Path(self._tmp) / 'healers.disabled'
            kill.write_text('stop\n')
            with mock.patch.object(h, 'KILL_SWITCH', kill):
                base = h.build_manifest(inst)
                state = {'baseline': base, 'dedup': {}}
                (inst / 'drift-check.sh').write_text('# drift\n')
                counts = h.run_once(install_dir=inst, state=state,
                                    dry_run_override=False)
            self.assertEqual(counts['dm_sent'], 0)
            self.assertEqual(self._dm_calls, [])


if __name__ == '__main__':
    unittest.main()
