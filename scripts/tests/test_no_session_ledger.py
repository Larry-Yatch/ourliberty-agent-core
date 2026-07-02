"""Tests for no_session_ledger.py (S1 / M3) — the durable obligation ledger
backing the cold-start no-session revision self-heal.

Each test isolates the on-disk store by pointing LEDGER_FILE at a per-test
tmp dir; time is injected via the ``now=`` params so age-based logic is
deterministic.

NOTE: stdlib unittest only — pytest is NOT installed in the droplet
test/regression environment, so an autouse pytest fixture here would make
every test silently fail to collect under the unittest runner.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import no_session_ledger as nsl  # noqa: E402

T0 = datetime(2026, 6, 23, 20, 0, 0, tzinfo=timezone.utc)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_ledger = nsl.LEDGER_FILE
        nsl.LEDGER_FILE = Path(self._tmp.name) / 'no-session-ledger.json'
        self.addCleanup(setattr, nsl, 'LEDGER_FILE', self._orig_ledger)

    def _open(self, task='t1', **kw):
        defaults = dict(pr_url='https://gh/o/r/pull/1', branch='claude/x',
                        target_repo='ourliberty-agent-core', head_sha='abc123',
                        round_num=1, now=T0)
        defaults.update(kw)
        nsl.open_obligation(task, **defaults)

    def test_open_creates_open_row(self):
        self._open('t1')
        row = nsl.get_obligation('t1')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], nsl.OPEN)
        self.assertEqual(row['pr_url'], 'https://gh/o/r/pull/1')
        self.assertEqual(row['head_sha'], 'abc123')
        self.assertEqual(row['round'], 1)
        self.assertEqual(row['opened_at'], nsl._iso(T0))
        self.assertIsNone(row['resolved_at'])

    def test_open_idempotent_preserves_opened_at_bumps_round(self):
        self._open('t1', round_num=1, head_sha='sha1', now=T0)
        later = T0 + timedelta(minutes=30)
        self._open('t1', round_num=2, head_sha='sha2', now=later)
        row = nsl.get_obligation('t1')
        self.assertEqual(row['opened_at'], nsl._iso(T0))          # preserved
        self.assertEqual(row['last_dispatch_at'], nsl._iso(later))  # bumped
        self.assertEqual(row['round'], 2)
        self.assertEqual(row['head_sha'], 'sha2')
        # Still exactly one row.
        self.assertEqual(list(nsl._load().keys()), ['t1'])

    def test_resolve_clears_open_returns_true(self):
        self._open('t1')
        self.assertTrue(
            nsl.resolve_obligation('t1', resolution='review-pass', now=T0))
        row = nsl.get_obligation('t1')
        self.assertEqual(row['status'], nsl.RESOLVED)
        self.assertEqual(row['resolution'], 'review-pass')
        self.assertIsNotNone(row['resolved_at'])

    def test_resolve_unknown_or_already_resolved_returns_false(self):
        self.assertFalse(nsl.resolve_obligation('nope', now=T0))
        self._open('t1')
        self.assertTrue(nsl.resolve_obligation('t1', now=T0))
        # second time: nothing open
        self.assertFalse(nsl.resolve_obligation('t1', now=T0))

    def test_reopen_after_resolve_flips_back_to_open(self):
        self._open('t1', now=T0)
        nsl.resolve_obligation('t1', now=T0)
        reopen = T0 + timedelta(hours=1)
        self._open('t1', round_num=2, now=reopen)
        row = nsl.get_obligation('t1')
        self.assertEqual(row['status'], nsl.OPEN)
        self.assertIsNone(row['resolved_at'])
        self.assertEqual(row['round'], 2)

    def test_list_open_excludes_resolved(self):
        self._open('open1')
        self._open('done1')
        nsl.resolve_obligation('done1', now=T0)
        open_ids = {r['task_id'] for r in nsl.list_open()}
        self.assertEqual(open_ids, {'open1'})

    def test_list_open_filters_by_age(self):
        self._open('fresh', now=T0)
        old = T0 - timedelta(minutes=90)
        self._open('stale', now=old)
        now = T0 + timedelta(minutes=1)
        stuck = nsl.list_open(now=now, older_than_minutes=60)
        self.assertEqual({r['task_id'] for r in stuck}, {'stale'})

    def test_prune_drops_aged_resolved_keeps_open(self):
        self._open('keepopen', now=T0)
        self._open('oldresolved', now=T0)
        nsl.resolve_obligation('oldresolved', now=T0)
        # A write far in the future triggers prune of the aged resolved row.
        future = T0 + timedelta(days=nsl._RESOLVED_RETENTION_DAYS + 1)
        self._open('trigger', now=future)
        ids = set(nsl._load().keys())
        self.assertNotIn('oldresolved', ids)       # pruned
        self.assertTrue({'keepopen', 'trigger'} <= ids)  # open rows survive

    def test_prune_never_evicts_open_rows(self):
        # The cap must bound only RESOLVED-row growth; an OPEN obligation is
        # never evicted (losing one would blind the backstop). cap=3, with 5
        # open + 2 resolved → all 5 open survive, resolved dropped to honor
        # the cap.
        orig_max = nsl._MAX_ROWS
        nsl._MAX_ROWS = 3
        self.addCleanup(setattr, nsl, '_MAX_ROWS', orig_max)
        for i in range(5):
            nsl.open_obligation(f'open{i}', pr_url=f'p{i}',
                                now=T0 + timedelta(minutes=i))
        for i in range(2):
            nsl.open_obligation(f'res{i}', pr_url=f'r{i}', now=T0)
            nsl.resolve_obligation(f'res{i}', now=T0)
        # Any write triggers a prune.
        nsl.open_obligation('trigger', pr_url='t',
                            now=T0 + timedelta(minutes=10))
        rows = nsl._load()
        open_ids = {k for k, v in rows.items() if v['status'] == nsl.OPEN}
        self.assertTrue(
            {'open0', 'open1', 'open2', 'open3', 'open4', 'trigger'} <= open_ids)
        self.assertNotIn('res0', rows)  # resolved dropped
        self.assertNotIn('res1', rows)

    def test_corrupt_file_degrades_to_empty(self):
        nsl.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        nsl.LEDGER_FILE.write_text('{ not json')
        self.assertEqual(nsl._load(), {})
        self.assertIsNone(nsl.get_obligation('anything'))
        self.assertEqual(nsl.list_open(), [])
        # And a write recovers cleanly.
        self._open('t1')
        self.assertEqual(nsl.get_obligation('t1')['status'], nsl.OPEN)

    def test_empty_task_id_is_noop(self):
        nsl.open_obligation('', pr_url='x', now=T0)
        self.assertEqual(nsl._load(), {})
        self.assertFalse(nsl.resolve_obligation('', now=T0))

    def test_persisted_json_is_readable_dict(self):
        self._open('t1')
        on_disk = json.loads(nsl.LEDGER_FILE.read_text())
        self.assertIsInstance(on_disk, dict)
        self.assertEqual(on_disk['t1']['status'], nsl.OPEN)


if __name__ == '__main__':
    unittest.main()
