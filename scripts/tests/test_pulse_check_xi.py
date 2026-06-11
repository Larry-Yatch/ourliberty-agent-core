#!/usr/bin/env python3
"""Tests for pulse_check_xi (catalog accuracy meter as a Pulse check).

The ourliberty-graph meter is NEVER actually run — every test feeds a synthetic
meter --json report (via --fixture or build_artifact directly), so the suite is
hermetic and has no cross-repo dependency.

Covers:
  - build_artifact: distillation + drifted-card filtering (non-VERIFIED only)
  - run_meter: MeterUnavailable when the meter binary is absent (fail-closed)
  - main(--fixture): clean report -> rc 0, artifact written, NO alert
  - main(--fixture): over-gate report -> rc 0, artifact written, digest alert
  - main(): meter unavailable -> rc 1 (so run_check escalates), NO heartbeat path

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_xi
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix='pulse-xi-test-')
# Isolate before import: the module resolves AGENTS_ROOT / GRAPH_DIR at import.
os.environ['OURLIBERTY_AGENTS_ROOT'] = str(Path(_TMP) / 'agents')
os.environ['OURLIBERTY_GRAPH_DIR'] = str(Path(_TMP) / 'graph')  # no meter inside

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_xi as pxi  # noqa: E402


def _report(over_gate=False, drifted_ids=()):
    cards = [{'id': f'ok-{i}', 'verdict': 'VERIFIED'} for i in range(3)]
    for cid in drifted_ids:
        cards.append({'id': cid, 'verdict': 'DRIFTED',
                      'dependents_missing': ['x'], 'detail': ''})
    total = len(cards)
    needs = len(drifted_ids)
    return {
        'cards_total': total,
        'verified': total - needs,
        'needs_attention': needs,
        'attention_rate': round(needs / total, 3),
        'gate': 0.10,
        'over_gate': over_gate,
        'tool_health': {'scanner_import_edges': 100, 'graphify_overlap_edges': 80,
                        'graphify_coverage_ratio': 0.8},
        'cards': cards,
    }


def _write_fixture(report):
    fd, path = tempfile.mkstemp(suffix='.json', dir=_TMP)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(report, fh)
    return path


class TestBuildArtifact(unittest.TestCase):

    def test_distills_and_filters_drifted(self):
        art = pxi.build_artifact(_report(over_gate=True, drifted_ids=['a', 'b']))
        self.assertEqual(art['check'], 'xi')
        self.assertEqual(art['cards_total'], 5)
        self.assertEqual(art['needs_attention'], 2)
        self.assertTrue(art['over_gate'])
        self.assertEqual({d['id'] for d in art['drifted']}, {'a', 'b'})
        # VERIFIED cards never appear in the drifted list.
        self.assertTrue(all(d['verdict'] != 'VERIFIED' for d in art['drifted']))

    def test_clean_report_has_no_drifted(self):
        art = pxi.build_artifact(_report(over_gate=False))
        self.assertEqual(art['drifted'], [])
        self.assertFalse(art['over_gate'])


class TestRunMeterFailClosed(unittest.TestCase):

    def test_missing_meter_raises(self):
        # GRAPH_DIR points at a dir with no pipeline/accuracy_meter.py.
        with self.assertRaises(pxi.MeterUnavailable):
            pxi.run_meter()


class TestBlindMeterGuard(unittest.TestCase):

    def test_zero_import_edges_is_unavailable_not_drift(self):
        # A meter pointed at non-existent source repos finds 0 import edges and
        # reports 100% drift; that must fail-closed (escalate), not pass through
        # as real catalog drift.
        blind = _report(over_gate=True, drifted_ids=['a', 'b'])
        blind['tool_health']['scanner_import_edges'] = 0
        blind['tool_health']['graphify_coverage_ratio'] = None
        fake = type('P', (), {'stdout': json.dumps(blind), 'stderr': '',
                              'returncode': 1})()
        with mock.patch.object(pxi, 'METER') as meter, \
                mock.patch('subprocess.run', return_value=fake):
            meter.exists.return_value = True
            with self.assertRaises(pxi.MeterUnavailable):
                pxi.run_meter()

    def test_nonzero_edges_passes(self):
        ok = _report(over_gate=False)  # tool_health edges = 100
        fake = type('P', (), {'stdout': json.dumps(ok), 'stderr': '',
                              'returncode': 0})()
        with mock.patch.object(pxi, 'METER') as meter, \
                mock.patch('subprocess.run', return_value=fake):
            meter.exists.return_value = True
            report = pxi.run_meter()
        self.assertEqual(report['tool_health']['scanner_import_edges'], 100)


class TestMain(unittest.TestCase):

    def test_clean_writes_artifact_no_alert(self):
        fx = _write_fixture(_report(over_gate=False))
        with mock.patch('larry_alerts.append_alert') as alert:
            rc = pxi.main(['--fixture', fx])
        self.assertEqual(rc, 0)
        alert.assert_not_called()
        arts = list(pxi.ARTIFACT_DIR.glob('check-xi-*.json'))
        self.assertTrue(arts, 'expected an artifact to be written')

    def test_over_gate_writes_artifact_and_digest_alert(self):
        fx = _write_fixture(_report(over_gate=True, drifted_ids=['pm-queries']))
        with mock.patch('larry_alerts.append_alert', return_value=True) as alert:
            rc = pxi.main(['--fixture', fx])
        self.assertEqual(rc, 0)
        alert.assert_called_once()
        kw = alert.call_args.kwargs
        self.assertEqual(kw['route'], 'digest')
        self.assertEqual(kw['subject'], 'catalog-accuracy-drift')
        self.assertIn('pm-queries', kw['message'])

    def test_dry_run_writes_nothing(self):
        fx = _write_fixture(_report(over_gate=True, drifted_ids=['a']))
        before = set(pxi.ARTIFACT_DIR.glob('check-xi-*.json')) \
            if pxi.ARTIFACT_DIR.exists() else set()
        with mock.patch('larry_alerts.append_alert') as alert:
            rc = pxi.main(['--fixture', fx, '--dry-run'])
        self.assertEqual(rc, 0)
        alert.assert_not_called()
        after = set(pxi.ARTIFACT_DIR.glob('check-xi-*.json')) \
            if pxi.ARTIFACT_DIR.exists() else set()
        self.assertEqual(before, after)

    def test_meter_unavailable_returns_nonzero(self):
        # No --fixture, meter binary absent -> MeterUnavailable -> rc 1.
        rc = pxi.main([])
        self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
