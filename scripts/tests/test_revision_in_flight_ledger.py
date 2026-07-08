"""Tests for revision_in_flight_ledger.py — the durable "Forge revision in
flight" flag that prevents duplicate concurrent Mirror review dispatches
(notifier-concurrent-scan-dup-review-dispatch-001).

Each test isolates the on-disk store by pointing LEDGER_FILE at a per-test
tmp dir; time is injected via the ``now=`` params so TTL logic is
deterministic.

NOTE: stdlib unittest only — pytest is NOT installed in the droplet
test/regression environment.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import revision_in_flight_ledger as rifl  # noqa: E402

T0 = datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc)
HEAD_A = 'aaaaaaaaaaaa'
HEAD_B = 'bbbbbbbbbbbb'


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_ledger = rifl.LEDGER_FILE
        rifl.LEDGER_FILE = Path(self._tmp.name) / 'revision-in-flight-ledger.json'
        self.addCleanup(setattr, rifl, 'LEDGER_FILE', self._orig_ledger)

    # -------------------- set --------------------

    def test_mark_creates_in_flight_row(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A,
                            pr_url='https://gh/o/r/pull/1', round_num=1, now=T0)
        row = rifl.get('t1')
        self.assertIsNotNone(row)
        self.assertEqual(row['head_sha'], HEAD_A)
        self.assertEqual(row['pr_url'], 'https://gh/o/r/pull/1')
        self.assertEqual(row['round'], 1)
        self.assertEqual(row['set_at'], rifl._iso(T0))

    def test_mark_empty_task_id_noops(self):
        rifl.mark_in_flight('', head_sha=HEAD_A, now=T0)
        self.assertEqual(rifl._load(), {})

    def test_mark_refresh_bumps_head_round_and_set_at(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, round_num=1, now=T0)
        later = T0 + timedelta(minutes=5)
        rifl.mark_in_flight('t1', head_sha=HEAD_B, round_num=2, now=later)
        row = rifl.get('t1')
        self.assertEqual(row['head_sha'], HEAD_B)
        self.assertEqual(row['round'], 2)
        self.assertEqual(row['set_at'], rifl._iso(later))

    def test_mark_missing_head_stores_null(self):
        rifl.mark_in_flight('t1', head_sha=None, now=T0)
        self.assertIsNone(rifl.get('t1')['head_sha'])

    # -------------------- guard: suppress --------------------

    def test_is_in_flight_same_head_suppresses(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        self.assertTrue(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=T0)
        )

    def test_is_in_flight_no_row_returns_false(self):
        self.assertFalse(
            rifl.is_in_flight('nope', current_head_sha=HEAD_A, now=T0)
        )

    def test_is_in_flight_empty_task_id_returns_false(self):
        self.assertFalse(rifl.is_in_flight('', current_head_sha=HEAD_A, now=T0))

    def test_is_in_flight_unknown_current_head_suppresses_conservatively(self):
        # current head can't be resolved (gh hiccup) — suppress to keep the
        # duplicate-dispatch window closed; reconcile retries + TTL bound it.
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        self.assertTrue(
            rifl.is_in_flight('t1', current_head_sha=None, now=T0)
        )

    def test_is_in_flight_unknown_recorded_head_suppresses_conservatively(self):
        rifl.mark_in_flight('t1', head_sha=None, now=T0)
        self.assertTrue(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=T0)
        )

    # -------------------- guard: new-head re-review passes through --------------------

    def test_is_in_flight_new_head_lets_re_review_through(self):
        # Design question (1): a landed revision produces a NEW head — the
        # re-review at that head must NOT be suppressed, even before the rerun
        # clear fires.
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        self.assertFalse(
            rifl.is_in_flight('t1', current_head_sha=HEAD_B, now=T0)
        )

    # -------------------- clear --------------------

    def test_clear_removes_row(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        self.assertTrue(rifl.clear('t1'))
        self.assertIsNone(rifl.get('t1'))
        self.assertFalse(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=T0)
        )

    def test_clear_absent_row_returns_false(self):
        self.assertFalse(rifl.clear('nope'))

    def test_clear_empty_task_id_returns_false(self):
        self.assertFalse(rifl.clear(''))

    def test_clear_is_idempotent(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        self.assertTrue(rifl.clear('t1'))
        self.assertFalse(rifl.clear('t1'))

    def test_clear_only_targets_named_task(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        rifl.mark_in_flight('t2', head_sha=HEAD_B, now=T0)
        # Inject now=T0 so clear()'s _save -> _prune uses the same injected time
        # as the marks; without it _prune runs on the real wall clock and evicts
        # the still-valid t2 row once real UTC exceeds T0 + TTL.
        rifl.clear('t1', now=T0)
        self.assertIsNone(rifl.get('t1'))
        self.assertIsNotNone(rifl.get('t2'))

    # -------------------- TTL expiry --------------------

    def test_is_in_flight_ttl_lapsed_returns_false(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        past_ttl = T0 + timedelta(seconds=rifl._DEFAULT_TTL_SECONDS + 60)
        self.assertFalse(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=past_ttl)
        )

    def test_is_in_flight_within_ttl_still_suppresses(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0)
        within = T0 + timedelta(seconds=rifl._DEFAULT_TTL_SECONDS - 60)
        self.assertTrue(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=within)
        )

    def test_custom_ttl_seconds_param(self):
        rifl.mark_in_flight('t1', head_sha=HEAD_A, now=T0, ttl_seconds=100)
        at_90 = T0 + timedelta(seconds=90)
        at_110 = T0 + timedelta(seconds=110)
        self.assertTrue(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=at_90,
                              ttl_seconds=100)
        )
        self.assertFalse(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=at_110,
                              ttl_seconds=100)
        )

    def test_save_prunes_ttl_lapsed_rows(self):
        rifl.mark_in_flight('old', head_sha=HEAD_A, now=T0)
        # A write far in the future prunes the aged 'old' row.
        future = T0 + timedelta(seconds=rifl._DEFAULT_TTL_SECONDS + 3600)
        rifl.mark_in_flight('fresh', head_sha=HEAD_B, now=future)
        state = rifl._load()
        self.assertIn('fresh', state)
        self.assertNotIn('old', state)

    # -------------------- fail-safe --------------------

    def test_load_corrupt_file_returns_empty(self):
        rifl.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        rifl.LEDGER_FILE.write_text('{ not valid json')
        self.assertEqual(rifl._load(), {})
        self.assertFalse(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=T0)
        )

    def test_load_non_dict_returns_empty(self):
        rifl.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        rifl.LEDGER_FILE.write_text('[1, 2, 3]')
        self.assertEqual(rifl._load(), {})

    def test_row_with_unparseable_set_at_is_not_in_flight(self):
        rifl.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        rifl.LEDGER_FILE.write_text(
            '{"t1": {"task_id": "t1", "head_sha": "aaaaaaaaaaaa", '
            '"set_at": "not-a-date"}}'
        )
        self.assertFalse(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=T0)
        )

    # -------------------- lifecycle integration --------------------

    def test_full_lifecycle_set_suppress_clear_flow(self):
        # set (revision dispatched at HEAD_A)
        rifl.mark_in_flight('t1', head_sha=HEAD_A, round_num=1, now=T0)
        # concurrent reconcile scan at the same head → suppressed
        self.assertTrue(
            rifl.is_in_flight('t1', current_head_sha=HEAD_A, now=T0)
        )
        # Forge pushes revision (new head) + rerun path clears the flag
        self.assertTrue(rifl.clear('t1'))
        # subsequent review at the new head flows normally
        self.assertFalse(
            rifl.is_in_flight('t1', current_head_sha=HEAD_B, now=T0)
        )


if __name__ == '__main__':
    unittest.main()
