"""Seam-audit L1: the approval-state root must be ONE path for all three
modules that touch the pending-approvals queue.

unittest (repo convention; pytest isn't installed on the droplet).

beacon_approval_handler OWNS the queue (writes via add_pending/save_state and
guards it with a file_lock sidecar). heal_stale_approvals READS it, and
heal_unregistered_approval WRITES to it (delegating through the handler's
add_pending). Before L1 the handler hardcoded ~/agents while both healers
honored OURLIBERTY_AGENTS_ROOT — so when the override was set, the handler
wrote/locked a different file than the healers read. These tests pin the
override at a temp dir and assert every module resolves to the SAME file and
the SAME lock sidecar.

Uses the reserved `zz-fixture-` task_id namespace so fixtures can never be
confused with live rows.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


class ApprovalRootEnvTest(unittest.TestCase):
    """Repoints the override at a temp dir + reloads all three modules so their
    module-level path constants pick up the env var, then asserts agreement."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'agents'
        (self.root / 'state').mkdir(parents=True)
        (self.root / 'blackboard').mkdir(parents=True)
        # Save/restore, never pop: under full discover an ambient sandbox
        # root (gate env, tests/__init__, or a sibling module's import-time
        # pin) may be set — popping it would un-sandbox every later test
        # whose module resolves the env at call time (test-jail audit H2/H3).
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)

        # Reload the handler FIRST so its PENDING_APPROVALS_PATH is env-derived,
        # then the healers (heal_unregistered binds the handler module object, so
        # its delegated writes target the reloaded path).
        import file_lock
        import beacon_approval_handler as handler
        import heal_stale_approvals as stale
        import heal_unregistered_approval as unreg
        self.file_lock = file_lock
        self.handler = importlib.reload(handler)
        self.stale = importlib.reload(stale)
        self.unreg = importlib.reload(unreg)

    def tearDown(self):
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self._tmp.cleanup()
        # Re-freeze module state against the RESTORED env (sandbox if one
        # was ambient, default otherwise) for any later test in the run.
        import beacon_approval_handler as handler
        import heal_stale_approvals as stale
        import heal_unregistered_approval as unreg
        importlib.reload(handler)
        importlib.reload(stale)
        importlib.reload(unreg)

    def test_handler_path_is_under_override(self):
        expected = self.root / 'state' / 'beacon-pending-approvals.json'
        self.assertEqual(self.handler.PENDING_APPROVALS_PATH, expected)

    def test_all_three_modules_agree_on_pending_file(self):
        expected = self.root / 'state' / 'beacon-pending-approvals.json'
        # Handler (writer/owner) and stale-healer (reader) expose constants.
        self.assertEqual(self.handler.PENDING_APPROVALS_PATH, expected)
        self.assertEqual(self.stale.PENDING_APPROVALS, expected)
        # Unregistered-healer writes by delegating to the handler's add_pending;
        # its `approval` alias must be the same env-derived handler.
        self.assertEqual(self.unreg.approval.PENDING_APPROVALS_PATH, expected)

    def test_lock_sidecar_follows_the_pending_file(self):
        # The handler guards the queue with a sidecar keyed to its path (PR #384,
        # #48). It must key off the env-derived path so the lock protects the
        # same file all writers touch.
        expected_lock = self.file_lock.sidecar_lock_path(
            self.root / 'state' / 'beacon-pending-approvals.json')
        self.assertEqual(
            self.file_lock.sidecar_lock_path(self.handler.PENDING_APPROVALS_PATH),
            expected_lock)
        self.assertEqual(
            self.file_lock.sidecar_lock_path(self.stale.PENDING_APPROVALS),
            expected_lock)

    def test_write_then_read_roundtrips_through_one_file(self):
        # End-to-end: a handler write lands at the env-derived path, and the
        # stale-healer's loader reads that exact file back.
        payload = {
            'task_id': 'zz-fixture-l1-roundtrip',
            'summary': 'seam-audit L1 fixture',
            'target_agent': 'forge',
            'prompt': 'noop',
        }
        self.handler.add_pending(payload, chat_id=1)

        expected = self.root / 'state' / 'beacon-pending-approvals.json'
        self.assertTrue(expected.exists(),
                        'handler wrote to the env-derived path')

        pending_ids, _history, _resolved = self.stale.load_beacon_approvals()
        self.assertIn('zz-fixture-l1-roundtrip', pending_ids)


if __name__ == '__main__':
    unittest.main()
