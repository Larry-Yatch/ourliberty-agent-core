#!/usr/bin/env python3
"""Tests for scripts/retire_parked_capture_rows.py — the one-time clear of the
retired `parked_capture` chain_events rows.

Run:
    python3 -m unittest scripts.tests.test_retire_parked_capture_rows
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import retire_parked_capture_rows as rpc  # noqa: E402
from _fake_reconcile_client import FakeReconcileClient  # noqa: E402


class RetireParkedCaptureRowsTest(unittest.TestCase):
    def test_dry_run_reports_open_count_writes_nothing(self):
        client = FakeReconcileClient(open_task_ids=['capture-a', 'capture-b'])
        out = rpc.retire(apply=False, client=client)
        self.assertEqual(out, {'open': 2, 'applied': False})
        self.assertEqual(client.cleared, [])
        self.assertEqual(client.upserts, [])

    def test_dry_run_open_none_when_unreadable(self):
        client = FakeReconcileClient(select_raises=True)
        out = rpc.retire(apply=False, client=client)
        self.assertEqual(out, {'open': None, 'applied': False})

    def test_apply_clears_all_open_emits_nothing_and_backs_up(self):
        client = FakeReconcileClient(open_task_ids=['capture-a', 'capture-b'])
        with tempfile.TemporaryDirectory() as tmp:
            out = rpc.retire(apply=True, client=client, backup_dir=tmp)
            self.assertEqual(out['cleared'], 2)
            self.assertEqual(out['emitted'], 0)
            self.assertFalse(out['skipped'])
            self.assertTrue(out['applied'])
            # Every clear is scoped to the parked_capture event_type.
            self.assertEqual(sorted(client.cleared),
                             [('parked_capture', 'capture-a'),
                              ('parked_capture', 'capture-b')])
            self.assertEqual(client.upserts, [])
            # A reversible backup of the cleared task_ids was written.
            self.assertIsNotNone(out['backup'])
            saved = json.loads(Path(out['backup']).read_text())
            self.assertEqual(saved['event_type'], 'parked_capture')
            self.assertEqual(saved['task_ids'], ['capture-a', 'capture-b'])

    def test_apply_no_backup_skips_backup_file(self):
        client = FakeReconcileClient(open_task_ids=['capture-a'])
        with tempfile.TemporaryDirectory() as tmp:
            out = rpc.retire(apply=True, client=client, backup=False,
                             backup_dir=tmp)
            self.assertEqual(out['cleared'], 1)
            self.assertIsNone(out['backup'])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_apply_idempotent_nothing_open_no_backup_written(self):
        client = FakeReconcileClient(open_task_ids=[])
        with tempfile.TemporaryDirectory() as tmp:
            out = rpc.retire(apply=True, client=client, backup_dir=tmp)
            self.assertEqual(out['cleared'], 0)
            self.assertEqual(out['emitted'], 0)
            self.assertFalse(out['skipped'])
            self.assertIsNone(out['backup'])         # nothing open -> no backup
            self.assertEqual(client.cleared, [])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_apply_skips_on_unreadable_open_set_no_blind_clear(self):
        # A read failure must NOT clear anything — reconcile skips instead.
        client = FakeReconcileClient(select_raises=True)
        out = rpc.retire(apply=True, client=client)
        self.assertTrue(out['skipped'])
        self.assertEqual(client.cleared, [])
        self.assertIsNone(out['backup'])

    def test_apply_aborts_clear_when_backup_write_fails(self):
        # Backup dir that can't be created (a regular file at that path) -> abort
        # the clear rather than clear without a reversible record.
        client = FakeReconcileClient(open_task_ids=['capture-a'])
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / 'not-a-dir'
            blocker.write_text('x')
            out = rpc.retire(apply=True, client=client,
                             backup_dir=str(blocker / 'sub'))
            self.assertTrue(out['skipped'])
            self.assertEqual(out['cleared'], 0)
            self.assertEqual(client.cleared, [])     # nothing cleared
            self.assertIsNone(out['backup'])


if __name__ == '__main__':
    unittest.main()
