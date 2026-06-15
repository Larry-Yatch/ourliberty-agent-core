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


def _coverage_report(coverage=0.22, lb_gaps=('lib/types.ts', 'lib/api.ts')):
    """A synthetic coverage_meter --json report (the advisory secondary reading)."""
    gaps = [{'file': f, 'dependents': 5, 'subsystem': 'dashboard / lib'} for f in lb_gaps]
    return {
        'universe': 228, 'covered': 51, 'coverage': coverage,
        'load_bearing_total': 192, 'load_bearing_covered': 50,
        'load_bearing_coverage': 0.26,
        'gaps': gaps, 'load_bearing_gaps': gaps,
        'by_subsystem': {}, 'unresolved_cards': [],
    }


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


class TestCoverageReading(unittest.TestCase):
    """The advisory coverage reading folded into the check (catalog-on-build backlog)."""

    def test_build_artifact_folds_coverage(self):
        art = pxi.build_artifact(_report(over_gate=False), _coverage_report())
        self.assertIn('coverage', art)
        self.assertEqual(art['coverage']['coverage'], 0.22)
        self.assertEqual(art['coverage']['restock_backlog'], 2)
        self.assertEqual(art['coverage']['restock_top'][:2], ['lib/types.ts', 'lib/api.ts'])

    def test_build_artifact_without_coverage_omits_key(self):
        art = pxi.build_artifact(_report(over_gate=False))
        self.assertNotIn('coverage', art)

    def test_missing_coverage_meter_returns_none_not_raises(self):
        # GRAPH_DIR has no pipeline/coverage_meter.py -> advisory skip, returns None
        # (NOT MeterUnavailable — coverage must never break the accuracy gate).
        self.assertIsNone(pxi.run_coverage_meter())

    def test_main_folds_coverage_fixture_into_artifact(self):
        fx = _write_fixture(_report(over_gate=False))
        cfx = _write_fixture(_coverage_report())
        before = set(pxi.ARTIFACT_DIR.glob('check-xi-*.json')) \
            if pxi.ARTIFACT_DIR.exists() else set()
        with mock.patch('larry_alerts.append_alert') as alert:
            rc = pxi.main(['--fixture', fx, '--coverage-fixture', cfx])
        self.assertEqual(rc, 0)
        alert.assert_not_called()
        new = set(pxi.ARTIFACT_DIR.glob('check-xi-*.json')) - before
        self.assertEqual(len(new), 1)
        latest = json.loads(new.pop().read_text(encoding='utf-8'))
        self.assertIn('coverage', latest)
        self.assertEqual(latest['coverage']['restock_backlog'], 2)

    def test_over_gate_alert_includes_coverage_clause(self):
        fx = _write_fixture(_report(over_gate=True, drifted_ids=['pm-queries']))
        cfx = _write_fixture(_coverage_report())
        with mock.patch('larry_alerts.append_alert', return_value=True) as alert:
            rc = pxi.main(['--fixture', fx, '--coverage-fixture', cfx])
        self.assertEqual(rc, 0)
        msg = alert.call_args.kwargs['message']
        self.assertIn('Catalog coverage', msg)
        self.assertIn('restock backlog', msg)


if __name__ == '__main__':
    unittest.main()
