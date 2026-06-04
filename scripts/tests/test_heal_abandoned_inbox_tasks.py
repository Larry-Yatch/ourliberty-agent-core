#!/usr/bin/env python3
"""Tests for scripts/heal_abandoned_inbox_tasks.py.

Focus: the 2026-06-04 fix — a task whose PR already merged (completion
handler crashed before clearing the inbox file) must NOT be re-dispatched.
`task_already_resolved` is the 4th gate; failsafe still recovers genuinely
abandoned tasks on any reconciliation error.

Run:
    python3 -m unittest scripts.tests.test_heal_abandoned_inbox_tasks
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_abandoned_inbox_tasks as h  # noqa: E402


class _IsolatedRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='heal-abandoned-')
        # The module binds AGENTS_ROOT at import time to a hard-coded path;
        # rebind the derived constants onto the tmp tree directly.
        root = Path(self._tmp)
        self._patches = [
            mock.patch.object(h, 'AGENTS_ROOT', root),
            mock.patch.object(h, 'KILL_SWITCH', root / 'healers.disabled'),
            mock.patch.object(h, 'LOG_FILE', root / 'logs' / 'h.log'),
            mock.patch.object(h, 'INBOXES_ROOT', root / 'inboxes'),
            mock.patch.object(h, 'LEASE_DIR', root / 'state' / 'leases'),
        ]
        for p in self._patches:
            p.start()
        (root / 'inboxes' / 'forge').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_old_task(self, agent: str, task_id: str, age_min: int = 120):
        path = h.INBOXES_ROOT / agent / f'{task_id}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"task_id": "%s"}' % task_id)
        old = time.time() - age_min * 60
        os.utime(path, (old, old))
        return path


class ResolvedGateTest(_IsolatedRoot):
    def test_skips_recovery_when_already_resolved(self):
        path = self._write_old_task('forge', 'shipped-task-001')
        with mock.patch.object(h, 'get_active_claude_cwds', return_value=set()), \
             mock.patch.object(h, 'task_already_resolved',
                               return_value='pr_merged:#294'), \
             mock.patch.object(h, 'recover_task') as recover_m:
            scanned, healed = h.scan_agent_inbox('forge', set())
        recover_m.assert_not_called()
        self.assertEqual(healed, 0)
        # File untouched (not renamed) — recovery was skipped, not performed.
        self.assertTrue(path.exists())

    def test_recovers_when_not_resolved(self):
        self._write_old_task('forge', 'abandoned-task-001')
        with mock.patch.object(h, 'get_active_claude_cwds', return_value=set()), \
             mock.patch.object(h, 'task_already_resolved', return_value=None), \
             mock.patch.object(h, 'recover_task', return_value=True) as recover_m:
            scanned, healed = h.scan_agent_inbox('forge', set())
        recover_m.assert_called_once()
        self.assertEqual(healed, 1)

    def test_lease_and_worker_gates_still_short_circuit(self):
        # An active lease means the resolution check is never even reached.
        self._write_old_task('forge', 'leased-task-001')
        (h.LEASE_DIR).mkdir(parents=True, exist_ok=True)
        (h.LEASE_DIR / 'leased-task-001.lock').write_text('')
        with mock.patch.object(h, 'get_active_claude_cwds', return_value=set()), \
             mock.patch.object(h, 'task_already_resolved') as resolved_m, \
             mock.patch.object(h, 'recover_task') as recover_m:
            h.scan_agent_inbox('forge', set())
        resolved_m.assert_not_called()
        recover_m.assert_not_called()


class TaskAlreadyResolvedTest(_IsolatedRoot):
    def test_delegates_to_task_resolution_with_check_pr(self):
        from datetime import datetime, timezone
        since = datetime(2026, 6, 4, tzinfo=timezone.utc)
        with mock.patch('task_resolution.resolved_out_of_band',
                        return_value=(True, 'pr_merged:#7')) as tr_m:
            reason = h.task_already_resolved('t-1', since)
        self.assertEqual(reason, 'pr_merged:#7')
        self.assertTrue(tr_m.call_args.kwargs['check_pr'])

    def test_failsafe_returns_none_on_error(self):
        from datetime import datetime, timezone
        since = datetime(2026, 6, 4, tzinfo=timezone.utc)
        with mock.patch('task_resolution.resolved_out_of_band',
                        side_effect=RuntimeError('gh down')):
            self.assertIsNone(h.task_already_resolved('t-1', since))


if __name__ == '__main__':
    unittest.main()
