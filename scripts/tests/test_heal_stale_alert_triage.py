#!/usr/bin/env python3
"""Tests for scripts/heal_stale_alert_triage.py (terminal-state reconciler § 3.2).

unittest (repo convention; pytest isn't installed on the droplet).

Coverage — the conservative guard (spec terminal-state-reconciliation.md § 1, § 6):
- classify_stuck_row: MERGED/CLOSED past grace ⇒ resolve; OPEN / UNKNOWN /
  within-grace / wrong-status / missing-or-bad dispatched_at / no-task_id ⇒ keep.
  An OPEN or UNKNOWN probe can only ever leave a phantom for another cycle — it
  can NEVER falsely retire a live row.
- reconcile_stuck_alert_triage end-to-end against an on-disk alert-triage.json:
  resolves only the terminal rows, leaves OPEN/UNKNOWN/within-grace pending,
  is idempotent, and --dry-run writes nothing.

Task_ids use the reserved `zz-fixture-` namespace so they can never be confused
with live work.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import alert_triage_state as ats  # noqa: E402
import task_terminal_state as tts  # noqa: E402

NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
OLD_TS = '2026-06-13T00:00:00+00:00'        # 36h before NOW (past grace)
FRESH_TS = '2026-06-14T11:30:00+00:00'      # 0.5h before NOW (within grace)
DISPATCHED = 'action-dispatched'


def _row(status=DISPATCHED, dispatched_at=OLD_TS, task_id='zz-fixture-pr'):
    """A per-alert lifecycle row in the action-dispatched stage."""
    return {
        'status': status,
        'dispatched_at': dispatched_at,
        'dispatch_task_id': task_id,
        'dispatch_target_agent': 'forge',
    }


class _Base(unittest.TestCase):
    """Repoints AGENTS_ROOT at a temp dir + reloads both the reconciler and
    alert_triage_state so their path resolution targets the sandbox."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'agents'
        (self.root / 'logs').mkdir(parents=True)
        (self.root / 'blackboard').mkdir(parents=True)
        (self.root / 'state').mkdir(parents=True)
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        importlib.reload(ats)
        import heal_stale_alert_triage as mod
        self.mod = importlib.reload(mod)
        self.state_path = self.root / ats.STATE_REL

    def tearDown(self):
        self._tmp.cleanup()
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        importlib.reload(ats)

    def _write_state(self, rows):
        self.state_path.write_text(json.dumps(rows))

    def _load(self):
        return json.loads(self.state_path.read_text())


# -------------------- root resolution --------------------

class TestAgentsRootResolution(unittest.TestCase):
    """An EMPTY OURLIBERTY_AGENTS_ROOT must fall back to ~/agents, NOT Path('')
    = cwd (mirrors the rest of the healer family)."""

    def setUp(self):
        self._saved = os.environ.get('OURLIBERTY_AGENTS_ROOT')

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._saved
        import heal_stale_alert_triage as mod
        importlib.reload(mod)

    def _reload(self):
        import heal_stale_alert_triage as mod
        return importlib.reload(mod)

    def test_empty_override_falls_back_to_home(self):
        os.environ['OURLIBERTY_AGENTS_ROOT'] = ''
        mod = self._reload()
        self.assertEqual(mod.AGENTS_ROOT, Path.home() / 'agents')

    def test_unset_override_falls_back_to_home(self):
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        mod = self._reload()
        self.assertEqual(mod.AGENTS_ROOT, Path.home() / 'agents')

    def test_set_override_is_honored(self):
        os.environ['OURLIBERTY_AGENTS_ROOT'] = '/tmp/zz-fixture-triage-root'
        mod = self._reload()
        self.assertEqual(mod.AGENTS_ROOT, Path('/tmp/zz-fixture-triage-root'))


# -------------------- classify_stuck_row (pure) --------------------

class TestClassifyStuckRow(_Base):
    GRACE = 2.0

    def _probe(self, state):
        return lambda _tid: state

    def test_merged_past_grace_resolves(self):
        resolve, reason = self.mod.classify_stuck_row(
            _row(task_id='zz-fixture-merged'), NOW, self.GRACE,
            self._probe(tts.MERGED))
        self.assertTrue(resolve)
        self.assertIn('terminal', reason)

    def test_closed_past_grace_resolves(self):
        resolve, _ = self.mod.classify_stuck_row(
            _row(task_id='zz-fixture-closed'), NOW, self.GRACE,
            self._probe(tts.CLOSED))
        self.assertTrue(resolve)

    def test_open_kept(self):
        resolve, reason = self.mod.classify_stuck_row(
            _row(task_id='zz-fixture-open'), NOW, self.GRACE,
            self._probe(tts.OPEN))
        self.assertFalse(resolve)
        self.assertIn('not terminal', reason)

    def test_unknown_kept(self):
        # Tier-1 auto-fix rows carry a canonical_intervention_id as their
        # dispatch_task_id, which matches no PR ⇒ UNKNOWN ⇒ kept (by design).
        resolve, reason = self.mod.classify_stuck_row(
            _row(task_id='intervention-abc123'), NOW, self.GRACE,
            self._probe(tts.UNKNOWN))
        self.assertFalse(resolve)
        self.assertIn('not terminal', reason)

    def test_within_grace_kept_even_if_terminal(self):
        resolve, reason = self.mod.classify_stuck_row(
            _row(dispatched_at=FRESH_TS, task_id='zz-fixture-fresh'),
            NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(resolve)
        self.assertIn('within grace', reason)

    def test_wrong_status_kept(self):
        for status in ('pending', 'triaged-tier-2', 'resolved'):
            resolve, reason = self.mod.classify_stuck_row(
                _row(status=status, task_id='zz-fixture-x'), NOW, self.GRACE,
                self._probe(tts.MERGED))
            self.assertFalse(resolve, status)
            self.assertIn(DISPATCHED, reason)

    def test_missing_dispatched_at_kept(self):
        row = _row(task_id='zz-fixture-nots')
        row['dispatched_at'] = None
        resolve, reason = self.mod.classify_stuck_row(
            row, NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(resolve)
        self.assertIn('dispatched_at', reason)

    def test_unparseable_dispatched_at_kept(self):
        resolve, _ = self.mod.classify_stuck_row(
            _row(dispatched_at='not-a-date', task_id='zz-fixture-bad'),
            NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(resolve)

    def test_no_task_id_kept(self):
        row = _row(task_id=None)
        resolve, reason = self.mod.classify_stuck_row(
            row, NOW, self.GRACE, self._probe(tts.MERGED))
        self.assertFalse(resolve)
        self.assertIn('dispatch_task_id', reason)

    def test_probe_not_called_until_past_grace(self):
        # The slow gh probe must be skipped for fresh rows (cost + correctness).
        calls = []
        self.mod.classify_stuck_row(
            _row(dispatched_at=FRESH_TS, task_id='zz-fixture-fresh'),
            NOW, self.GRACE, lambda tid: (calls.append(tid) or tts.MERGED))
        self.assertEqual(calls, [])


# -------------------- reconcile_stuck_alert_triage (integration) --------------------

class TestReconcileStuckAlertTriage(_Base):
    def _probe(self, states):
        return lambda tid: states.get(tid, tts.UNKNOWN)

    def _seed(self):
        self._write_state({
            'alert-merged': _row(task_id='zz-fixture-merged'),
            'alert-open': _row(task_id='zz-fixture-open'),
            'alert-fresh': _row(dispatched_at=FRESH_TS, task_id='zz-fixture-fresh'),
            'alert-triaged': _row(status='triaged-tier-2', task_id='zz-fixture-t'),
        })
        return self._probe({
            'zz-fixture-merged': tts.MERGED,
            'zz-fixture-open': tts.OPEN,
            'zz-fixture-fresh': tts.MERGED,   # terminal but within grace -> keep
        })

    def test_resolves_terminal_keeps_open_and_within_grace(self):
        probe = self._seed()
        counts = self.mod.reconcile_stuck_alert_triage(now=NOW, probe=probe)

        self.assertEqual(counts['dispatched'], 3)  # merged, open, fresh
        self.assertEqual(counts['resolved'], 1)
        self.assertEqual(counts['kept'], 2)        # open, fresh

        state = self._load()
        self.assertEqual(state['alert-merged']['status'], 'resolved')
        self.assertIn('terminal-state', state['alert-merged']['resolution'])
        self.assertEqual(state['alert-open']['status'], DISPATCHED)
        self.assertEqual(state['alert-fresh']['status'], DISPATCHED)
        self.assertEqual(state['alert-triaged']['status'], 'triaged-tier-2')

    def test_dry_run_writes_nothing(self):
        probe = self._seed()
        counts = self.mod.reconcile_stuck_alert_triage(
            now=NOW, probe=probe, dry_run=True)
        self.assertEqual(counts['resolved'], 1)  # classified
        state = self._load()
        self.assertEqual(state['alert-merged']['status'], DISPATCHED)  # not applied

    def test_unknown_probe_never_resolves(self):
        # The five-rows-stuck-10+-days case: every probe is UNKNOWN. None may be
        # resolved — a phantom kept for another cycle beats a falsely-retired row.
        self._write_state({
            'alert-a': _row(task_id='zz-fixture-a'),
            'alert-b': _row(task_id='zz-fixture-b'),
        })
        counts = self.mod.reconcile_stuck_alert_triage(
            now=NOW, probe=lambda _tid: tts.UNKNOWN)
        self.assertEqual(counts['resolved'], 0)
        self.assertEqual(counts['kept'], 2)
        state = self._load()
        self.assertTrue(all(r['status'] == DISPATCHED for r in state.values()))

    def test_idempotent_second_run(self):
        probe = self._seed()
        self.mod.reconcile_stuck_alert_triage(now=NOW, probe=probe)
        counts2 = self.mod.reconcile_stuck_alert_triage(now=NOW, probe=probe)
        self.assertEqual(counts2['resolved'], 0)
        self.assertEqual(counts2['dispatched'], 2)  # merged now resolved
        self.assertEqual(counts2['kept'], 2)


if __name__ == '__main__':
    unittest.main()
