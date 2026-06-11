#!/usr/bin/env python3
"""PR-I — timezone & temporal-window correctness regression tests.

2026-06-05 full-codebase audit:
  * #28 pulse_check_iii.fetch_durations_from_supabase — pair the LATEST start
        with the EARLIEST done after it (one clean session span), not the
        earliest start with the latest done (inflated multi-session span).
  * #30 pulse_check_vii — the per-date artifact sentinel must be content-aware
        so a SECOND qualifying proposal the same calendar day isn't lost.
  * #38 ledger_weekly — a missing/corrupt prior sidecar must not be labelled
        'vs prior week' nor trip the drift gate against the wrong window.

(#23 DST tests live in test_heal_systemd_install_drift.py.)

Run::

    cd ~/agent-core && python3 -m unittest scripts.tests.test_pr_i_temporal
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_iii as p3  # noqa: E402
import pulse_check_vii as p7  # noqa: E402
import ledger_weekly as lw  # noqa: E402


# ----------------------------- #28 -----------------------------

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Minimal stand-in for the supabase query builder used by
    fetch_durations_from_supabase: filters by event_type, slices by range."""

    def __init__(self, rows):
        self._rows = rows
        self._etype = None
        self._slice = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        if col == 'event_type':
            self._etype = val
        return self

    def gte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def range(self, lo, hi):
        matching = [r for r in self._rows if r.get('event_type') == self._etype]
        self._slice = matching[lo:hi + 1]
        return self

    def execute(self):
        return _FakeResult(self._slice)


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeQuery(self._rows)


def _ev(event_type, tid, ts, **payload):
    return {
        'event_type': event_type, 'task_id': tid, 'ts': ts,
        'agent': 'forge', 'payload': payload or {},
    }


class TestSessionPairing(unittest.TestCase):

    def _durations(self, rows):
        out = p3.fetch_durations_from_supabase(_FakeClient(rows))
        return {d.task_id: d.duration_sec for d in out}

    def test_single_clean_session(self):
        rows = [
            _ev('session_start', 't1', '2026-06-01T10:00:00+00:00'),
            _ev('session_done', 't1', '2026-06-01T10:05:00+00:00'),
        ]
        self.assertEqual(self._durations(rows), {'t1': 300.0})

    def test_recurring_task_uses_latest_start_earliest_done_after(self):
        # Two sessions reuse the same task_id. The OLD code paired earliest
        # start (10:00) with latest done (14:03) → ~4h. The clean span is the
        # second session: 14:00 → 14:03 = 180s.
        rows = [
            _ev('session_start', 't1', '2026-06-01T10:00:00+00:00'),
            _ev('session_done', 't1', '2026-06-01T10:05:00+00:00'),
            _ev('session_start', 't1', '2026-06-01T14:00:00+00:00'),
            _ev('session_done', 't1', '2026-06-01T14:03:00+00:00'),
        ]
        self.assertEqual(self._durations(rows), {'t1': 180.0})

    def test_done_only_before_latest_start_is_dropped(self):
        # The only done predates the latest start → no clean span → no row
        # (rather than a negative/inflated duration).
        rows = [
            _ev('session_start', 't1', '2026-06-01T09:00:00+00:00'),
            _ev('session_done', 't1', '2026-06-01T09:30:00+00:00'),
            _ev('session_start', 't1', '2026-06-01T14:00:00+00:00'),
        ]
        self.assertEqual(self._durations(rows), {})

    def test_start_without_done_skipped(self):
        rows = [_ev('session_start', 't1', '2026-06-01T10:00:00+00:00')]
        self.assertEqual(self._durations(rows), {})

    def test_z_suffix_timestamps(self):
        rows = [
            _ev('session_start', 't1', '2026-06-01T10:00:00Z'),
            _ev('session_done', 't1', '2026-06-01T10:01:00Z'),
        ]
        self.assertEqual(self._durations(rows), {'t1': 60.0})


# ----------------------------- #30 -----------------------------

def _prop(rule, band):
    return p7.Proposal(
        rule=rule, band=band, current_threshold_usd=50.0,
        proposed_threshold_usd=60.0, rationale='r', detail={})


def _result(proposals, anchor_date='2026-06-01'):
    return p7.CheckVIIResult(
        proposals=proposals, as_of_iso='2026-06-01T00:00:00+00:00',
        anchor_date=anchor_date, rows_total=len(proposals))


class TestCheckViiContentAwareSentinel(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = p7.PROPOSALS_DIR
        self._orig_run = p7.run_check
        self._orig_dm = p7.dm_digest
        self._orig_read = p7._read_log
        p7.PROPOSALS_DIR = self.tmp / 'proposals'
        p7._read_log = lambda *a, **k: []
        self.dm_calls = []
        p7.dm_digest = lambda artifact: self.dm_calls.append(artifact) or True

    def tearDown(self):
        p7.PROPOSALS_DIR = self._orig_dir
        p7.run_check = self._orig_run
        p7.dm_digest = self._orig_dm
        p7._read_log = self._orig_read
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _persisted(self):
        path = p7.artifact_path_for_date('2026-06-01')
        return p7._persisted_proposal_keys(path)

    def test_persisted_keys_helper(self):
        path = p7.artifact_path_for_date('2026-06-01')
        self.assertEqual(p7._persisted_proposal_keys(path), set())  # missing
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{ corrupt')
        self.assertEqual(p7._persisted_proposal_keys(path), set())  # corrupt

    def test_persisted_keys_tolerates_malformed_shapes(self):
        path = p7.artifact_path_for_date('2026-06-01')
        path.parent.mkdir(parents=True, exist_ok=True)
        # explicit null proposals must NOT crash the gate (review fix)
        path.write_text(json.dumps({'proposals': None}))
        self.assertEqual(p7._persisted_proposal_keys(path), set())
        # top-level non-dict
        path.write_text(json.dumps([1, 2, 3]))
        self.assertEqual(p7._persisted_proposal_keys(path), set())
        # non-dict entries inside the list are skipped
        path.write_text(json.dumps({'proposals': ['x', {'rule': 'raise',
                                                        'band': 'low'}]}))
        self.assertEqual(
            p7._persisted_proposal_keys(path), {('raise', 'low')})

    def test_second_same_day_proposal_is_not_lost(self):
        # First run: low-band raise fires → artifact written, DM sent.
        p7.run_check = lambda **k: _result([_prop('raise', 'low')])
        self.assertEqual(p7.main([]), 0)
        self.assertEqual(self._persisted(), {('raise', 'low')})
        self.assertEqual(len(self.dm_calls), 1)

        # Later same day: a high-band remove becomes eligible too. The OLD
        # date-only sentinel skipped this entirely. It must now be persisted
        # and DM'd.
        p7.run_check = lambda **k: _result(
            [_prop('raise', 'low'), _prop('remove', 'high')])
        self.assertEqual(p7.main([]), 0)
        self.assertEqual(
            self._persisted(), {('raise', 'low'), ('remove', 'high')})
        self.assertEqual(len(self.dm_calls), 2)

    def test_identical_proposals_same_day_are_skipped(self):
        p7.run_check = lambda **k: _result([_prop('raise', 'low')])
        self.assertEqual(p7.main([]), 0)
        self.assertEqual(len(self.dm_calls), 1)
        # Re-run with the SAME proposal set → no new keys → skip, no re-DM.
        self.assertEqual(p7.main([]), 0)
        self.assertEqual(len(self.dm_calls), 1)


# ----------------------------- #38 -----------------------------

class TestLedgerPriorWeekGap(unittest.TestCase):

    def _sidecar(self, week_ending, total):
        return {'week_ending': week_ending, 'total_usd': total}

    def test_weeks_back_of(self):
        we = datetime(2026, 6, 1, tzinfo=timezone.utc)  # report week_ending
        self.assertEqual(
            lw._weeks_back_of(self._sidecar('2026-05-25', 100.0), we), 1)
        self.assertEqual(
            lw._weeks_back_of(self._sidecar('2026-05-18', 100.0), we), 2)
        self.assertIsNone(
            lw._weeks_back_of(self._sidecar('2026-05-30', 100.0), we))  # gap≠7
        self.assertIsNone(lw._weeks_back_of({'total_usd': 1.0}, we))  # no field

    def test_delta_carries_weeks_back(self):
        d = lw.compute_delta(120.0, self._sidecar('2026-05-25', 100.0),
                             weeks_back=1)
        self.assertEqual(d['weeks_back'], 1)
        self.assertAlmostEqual(d['percent'], 20.0)

    def test_drift_fires_only_for_genuine_prior_week(self):
        # +50% but the baseline is 2 weeks back (last week's sidecar missing):
        # must NOT be reported as drift.
        gapped = {'absolute_usd': 50.0, 'percent': 50.0, 'weeks_back': 2}
        self.assertFalse(lw._drift_is_real(gapped))
        genuine = {'absolute_usd': 50.0, 'percent': 50.0, 'weeks_back': 1}
        self.assertTrue(lw._drift_is_real(genuine))
        # Unknown weeks_back (bad/missing sidecar date) also fails closed.
        unknown = {'absolute_usd': 50.0, 'percent': 50.0, 'weeks_back': None}
        self.assertFalse(lw._drift_is_real(unknown))
        self.assertFalse(lw._drift_is_real(None))

    def test_window_label(self):
        self.assertEqual(
            lw._delta_window_label({'weeks_back': 1}), 'prior week')
        self.assertEqual(
            lw._delta_window_label({'weeks_back': 3}), '3 weeks ago')
        self.assertEqual(
            lw._delta_window_label({'weeks_back': None}), 'an earlier week')

    def test_report_gaps_prior_week_label_and_suppresses_drift(self):
        # End-to-end: report for week ending 2026-06-01; last week (05-25)
        # sidecar is MISSING, only 05-18 (2 weeks back) exists with a +50%
        # gap. The delta must be labelled '2 weeks ago' and not flagged drift.
        tmp = Path(tempfile.mkdtemp())
        try:
            out_dir = tmp / 'ledger'
            out_dir.mkdir(parents=True)
            (out_dir / 'weekly-2026-05-18.json').write_text(json.dumps(
                {'week_ending': '2026-05-18', 'total_usd': 100.0,
                 'by_task_type': {}}))
            costs = tmp / 'costs.jsonl'
            costs.write_text('')  # no rows → total 0; still computes a delta
            week_ending = datetime(2026, 6, 1, tzinfo=timezone.utc)
            report, _doc, _skipped = lw.compute_weekly_report(
                week_ending, costs, tmp / 'outbox', out_dir)
            self.assertEqual(report.delta_vs_prior_week['weeks_back'], 2)
            md = lw.render_markdown(report, 0)
            self.assertIn('Vs 2 weeks ago', md)
            self.assertNotIn('Vs prior week:', md)
            _msg, is_anom = lw.render_dm_headline(report, Path('/x'))
            self.assertFalse(is_anom)  # 100→0 is -100% but baseline is gapped
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
