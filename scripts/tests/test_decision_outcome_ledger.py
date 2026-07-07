"""Tests for scripts/decision_outcome_ledger.py.

Coverage (Operator Feed Loop slice 1 — the decision-outcome ledger):
- record_decision: persists to the ledger file; preserves decision_key /
  outcome / actor / cleared / notes; coerces an unknown outcome to 'expired'
  and a None/non-str decision_key to ''.
- records_for_key: returns only the matching key's rows, oldest first; empty
  key -> [].
- outcome_counts: tallies by outcome across all rows, and filtered to one actor.
- read_recent: bounded, oldest-first slice; limit<=0 -> [].
- fail safe on read: missing file -> records_for_key []/outcome_counts {}/
  read_recent []; a ledger with a malformed line is skipped while the
  surrounding valid rows still surface.
- fail safe on write: when the ledger directory is unwritable (simulated by a
  read-only state/ dir under a tmp root), record_decision returns False rather
  than raising.

Mirrors the isolation + fail-safe shape of test_medic_ledger.py.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import decision_outcome_ledger as dol  # noqa: E402


class _IsolatedLedger(unittest.TestCase):
    """Point OURLIBERTY_AGENTS_ROOT at a tmp dir + reload the module so
    LEDGER_FILE / LOG_FILE pick up the override (mirrors test_medic_ledger)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='decision-ledger-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        importlib.reload(dol)

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(dol)
        # restore any read-only dir so cleanup can remove it
        for p in (Path(self._tmpdir) / 'state', Path(self._tmpdir)):
            try:
                p.chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class RecordDecisionTest(_IsolatedLedger):
    def test_round_trips_all_fields(self) -> None:
        self.assertTrue(dol.record_decision(
            'taskA::pr1', 'approved', actor='dashboard', cleared=3,
            notes='looks good'))
        recs = dol.read_recent(10)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r['decision_key'], 'taskA::pr1')
        self.assertEqual(r['outcome'], 'approved')
        self.assertEqual(r['actor'], 'dashboard')
        self.assertEqual(r['cleared'], 3)
        self.assertEqual(r['notes'], 'looks good')
        self.assertIn('ts', r)

    def test_unknown_outcome_coerced_to_expired(self) -> None:
        self.assertTrue(dol.record_decision('k', 'bogus', actor='x', cleared=1))
        self.assertEqual(dol.read_recent(1)[0]['outcome'], 'expired')

    def test_none_key_collapses_to_empty_string(self) -> None:
        self.assertTrue(dol.record_decision(None, 'rejected', actor='telegram',
                                            cleared=2))
        self.assertEqual(dol.read_recent(1)[0]['decision_key'], '')

    def test_missing_actor_uses_placeholder(self) -> None:
        self.assertTrue(dol.record_decision('k', 'approved', cleared=1))
        self.assertEqual(dol.read_recent(1)[0]['actor'], '?')

    def test_non_int_cleared_defaults_to_zero(self) -> None:
        self.assertTrue(dol.record_decision('k', 'approved', actor='x',
                                            cleared='oops'))
        self.assertEqual(dol.read_recent(1)[0]['cleared'], 0)


class RecordsForKeyTest(_IsolatedLedger):
    def test_filters_to_matching_key_oldest_first(self) -> None:
        dol.record_decision('kA', 'approved', actor='dashboard', cleared=1)
        dol.record_decision('kB', 'rejected', actor='dashboard', cleared=1)
        dol.record_decision('kA', 'modified', actor='telegram', cleared=1)
        got = dol.records_for_key('kA')
        self.assertEqual([r['outcome'] for r in got], ['approved', 'modified'])

    def test_empty_key_returns_empty(self) -> None:
        dol.record_decision('kA', 'approved', actor='x', cleared=1)
        self.assertEqual(dol.records_for_key(''), [])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(dol.records_for_key('anything'), [])


class OutcomeCountsTest(_IsolatedLedger):
    def _seed(self) -> None:
        dol.record_decision('k1', 'approved', actor='dashboard', cleared=1)
        dol.record_decision('k2', 'approved', actor='dashboard', cleared=1)
        dol.record_decision('k3', 'rejected', actor='telegram', cleared=1)

    def test_counts_all_outcomes(self) -> None:
        self._seed()
        self.assertEqual(dol.outcome_counts(),
                         {'approved': 2, 'rejected': 1})

    def test_counts_filtered_by_actor(self) -> None:
        self._seed()
        self.assertEqual(dol.outcome_counts(actor='dashboard'),
                         {'approved': 2})
        self.assertEqual(dol.outcome_counts(actor='telegram'),
                         {'rejected': 1})

    def test_missing_file_returns_empty_dict(self) -> None:
        self.assertEqual(dol.outcome_counts(), {})


class ReadRecentTest(_IsolatedLedger):
    def test_bounded_and_oldest_first(self) -> None:
        for i in range(5):
            dol.record_decision(f'k{i}', 'approved', actor='x', cleared=1)
        got = dol.read_recent(3)
        self.assertEqual([r['decision_key'] for r in got], ['k2', 'k3', 'k4'])

    def test_nonpositive_limit_returns_empty(self) -> None:
        dol.record_decision('k', 'approved', actor='x', cleared=1)
        self.assertEqual(dol.read_recent(0), [])
        self.assertEqual(dol.read_recent(-1), [])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(dol.read_recent(10), [])


class FailSafeTest(_IsolatedLedger):
    def test_malformed_line_skipped_valid_survives(self) -> None:
        dol.record_decision('good1', 'approved', actor='x', cleared=1)
        with open(dol.LEDGER_FILE, 'a', encoding='utf-8') as f:
            f.write('{not valid json\n')
        dol.record_decision('good2', 'rejected', actor='x', cleared=1)
        keys = [r['decision_key'] for r in dol.read_recent(10)]
        self.assertEqual(keys, ['good1', 'good2'])

    @unittest.skipIf(os.geteuid() == 0, 'root ignores directory permissions')
    def test_unwritable_dir_returns_false(self) -> None:
        state_dir = dol.LEDGER_FILE.parent
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o500)  # read+execute, no write
        try:
            self.assertFalse(
                dol.record_decision('k', 'approved', actor='x', cleared=1))
        finally:
            state_dir.chmod(0o755)


class ResolveDecisionWiringTest(_IsolatedLedger):
    """Prove the hook in decision_resolve.resolve_decision actually writes a
    ledger row on a genuine resolution — and stays silent on a no-op. Patches
    the four store legs so no real store is touched; only the total-cleared
    count (and thus the hook's gate) varies."""

    def setUp(self) -> None:
        super().setUp()
        import decision_resolve as dr
        self.dr = dr
        self._orig = (dr._resolve_chain_events, dr._resolve_escalations,
                      dr._resolve_alerts, dr._resolve_pending)

    def tearDown(self) -> None:
        (self.dr._resolve_chain_events, self.dr._resolve_escalations,
         self.dr._resolve_alerts, self.dr._resolve_pending) = self._orig
        super().tearDown()

    def _patch_legs(self, *, chain=0, esc=0, alerts=0, pending=0) -> None:
        self.dr._resolve_chain_events = lambda key, client, log: chain
        self.dr._resolve_escalations = lambda key, log: esc
        self.dr._resolve_alerts = lambda key, log: alerts
        self.dr._resolve_pending = lambda entry_id, outcome, note, log: pending

    def test_genuine_resolution_writes_one_row(self) -> None:
        self._patch_legs(chain=1)
        self.dr.resolve_decision('taskZ::prX', 'approved', actor='dashboard')
        rows = dol.read_recent(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['decision_key'], 'taskZ::prX')
        self.assertEqual(rows[0]['outcome'], 'approved')
        self.assertEqual(rows[0]['actor'], 'dashboard')
        self.assertEqual(rows[0]['cleared'], 1)

    def test_noop_resolution_writes_nothing(self) -> None:
        # All legs clear 0 (e.g. a healer re-driving an already-resolved
        # decision) -> total 0 -> no ledger row, no double count.
        self._patch_legs()
        self.dr.resolve_decision('already::done', 'approved', actor='heal')
        self.assertEqual(dol.read_recent(10), [])


if __name__ == '__main__':
    unittest.main()
