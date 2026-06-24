"""Tests for rebase_obligation_ledger.py (forge-post-open-mergeable-rebase-001)
— the durable obligation ledger backing the post-open auto-rebase backstop.

Each test isolates the on-disk store by pointing LEDGER_FILE at a per-test tmp
dir; time is injected via the ``now=`` params so age-based logic is
deterministic.

NOTE: stdlib unittest only — pytest is NOT installed in the droplet
test/regression environment.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import rebase_obligation_ledger as rol  # noqa: E402

T0 = datetime(2026, 6, 24, 20, 0, 0, tzinfo=timezone.utc)


class RebaseLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_ledger = rol.LEDGER_FILE
        rol.LEDGER_FILE = Path(self._tmp.name) / 'rebase-obligation-ledger.json'
        self.addCleanup(setattr, rol, 'LEDGER_FILE', self._orig_ledger)

    def _open(self, task='t1', **kw):
        defaults = dict(pr_url='https://gh/o/r/pull/1', branch='forge/x',
                        target_repo='ourliberty-agent-core', head_sha='abc123',
                        round_num=1, now=T0)
        defaults.update(kw)
        rol.open_obligation(task, **defaults)

    def test_open_creates_open_row(self):
        self._open('t1')
        row = rol.get_obligation('t1')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], rol.OPEN)
        self.assertEqual(row['pr_url'], 'https://gh/o/r/pull/1')
        self.assertEqual(row['head_sha'], 'abc123')
        self.assertEqual(row['round'], 1)
        self.assertEqual(row['opened_at'], rol._iso(T0))
        self.assertIsNone(row['resolved_at'])

    def test_open_idempotent_preserves_opened_at_bumps_round(self):
        self._open('t1', round_num=1, head_sha='sha1', now=T0)
        later = T0 + timedelta(minutes=30)
        self._open('t1', round_num=2, head_sha='sha2', now=later)
        row = rol.get_obligation('t1')
        self.assertEqual(row['opened_at'], rol._iso(T0))             # preserved
        self.assertEqual(row['last_dispatch_at'], rol._iso(later))  # bumped
        self.assertEqual(row['round'], 2)
        self.assertEqual(row['head_sha'], 'sha2')
        self.assertEqual(list(rol._load().keys()), ['t1'])

    def test_resolve_clears_open_returns_true(self):
        self._open('t1')
        self.assertTrue(
            rol.resolve_obligation('t1', resolution='mergeable', now=T0))
        row = rol.get_obligation('t1')
        self.assertEqual(row['status'], rol.RESOLVED)
        self.assertEqual(row['resolution'], 'mergeable')
        self.assertIsNotNone(row['resolved_at'])

    def test_resolve_unknown_or_already_resolved_returns_false(self):
        self.assertFalse(rol.resolve_obligation('nope', now=T0))
        self._open('t1')
        self.assertTrue(rol.resolve_obligation('t1', now=T0))
        self.assertFalse(rol.resolve_obligation('t1', now=T0))

    def test_reopen_after_resolve_flips_back_to_open(self):
        self._open('t1', now=T0)
        rol.resolve_obligation('t1', now=T0)
        reopen = T0 + timedelta(hours=1)
        self._open('t1', round_num=2, now=reopen)
        row = rol.get_obligation('t1')
        self.assertEqual(row['status'], rol.OPEN)
        self.assertIsNone(row['resolved_at'])
        self.assertEqual(row['round'], 2)

    def test_list_open_excludes_resolved(self):
        self._open('open1')
        self._open('done1')
        rol.resolve_obligation('done1', now=T0)
        open_ids = {r['task_id'] for r in rol.list_open()}
        self.assertEqual(open_ids, {'open1'})

    def test_list_open_filters_by_age(self):
        self._open('fresh', now=T0)
        old = T0 - timedelta(minutes=90)
        self._open('stale', now=old)
        now = T0 + timedelta(minutes=1)
        stuck = rol.list_open(now=now, older_than_minutes=60)
        self.assertEqual({r['task_id'] for r in stuck}, {'stale'})

    def test_prune_never_evicts_open_rows(self):
        orig_max = rol._MAX_ROWS
        rol._MAX_ROWS = 3
        self.addCleanup(setattr, rol, '_MAX_ROWS', orig_max)
        for i in range(5):
            rol.open_obligation(f'open{i}', pr_url=f'p{i}',
                                now=T0 + timedelta(minutes=i))
        for i in range(2):
            rol.open_obligation(f'res{i}', pr_url=f'r{i}', now=T0)
            rol.resolve_obligation(f'res{i}', now=T0)
        rol.open_obligation('trigger', pr_url='t',
                            now=T0 + timedelta(minutes=10))
        rows = rol._load()
        open_ids = {k for k, v in rows.items() if v['status'] == rol.OPEN}
        self.assertTrue(
            {'open0', 'open1', 'open2', 'open3', 'open4', 'trigger'} <= open_ids)
        self.assertNotIn('res0', rows)
        self.assertNotIn('res1', rows)

    def test_corrupt_file_degrades_to_empty(self):
        rol.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        rol.LEDGER_FILE.write_text('{ not json')
        self.assertEqual(rol._load(), {})
        self.assertIsNone(rol.get_obligation('anything'))
        self.assertEqual(rol.list_open(), [])
        self._open('t1')
        self.assertEqual(rol.get_obligation('t1')['status'], rol.OPEN)

    def test_empty_task_id_is_noop(self):
        rol.open_obligation('', pr_url='x', now=T0)
        self.assertEqual(rol._load(), {})
        self.assertFalse(rol.resolve_obligation('', now=T0))

    def test_distinct_state_file_from_no_session_ledger(self):
        # Sibling ledger must use its own file so rebase + cold-start
        # obligations never collide.
        self.assertTrue(str(rol.LEDGER_FILE).endswith('rebase-obligation-ledger.json'))


if __name__ == '__main__':
    unittest.main()
