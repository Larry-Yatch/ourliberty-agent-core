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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import time
import types
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
            mock.patch.object(h, 'IN_FLIGHT_DIR', root / 'state' / 'in-flight'),
            # Default the per-agent dispatch-lease gate to "free" so scan tests
            # are hermetic (it otherwise reads dispatch_lease's real, un-isolated
            # path). The gate itself is exercised directly in DispatchLeaseGateTest.
            mock.patch.object(h, 'has_live_dispatch_lease', return_value=False),
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

    def test_live_in_flight_worker_short_circuits(self):
        # A LIVE in-flight registry entry (real per-task gate) means the
        # resolution check is never even reached and recovery is skipped.
        self._write_old_task('forge', 'in-flight-task-001')
        h.IN_FLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        (h.IN_FLIGHT_DIR / 'in-flight-task-001.json').write_text(
            json.dumps({'task_stem': 'in-flight-task-001', 'pid': os.getpid()}))
        with mock.patch.object(h, 'get_active_claude_cwds', return_value=set()), \
             mock.patch.object(h, 'task_already_resolved') as resolved_m, \
             mock.patch.object(h, 'recover_task') as recover_m:
            h.scan_agent_inbox('forge', set())
        resolved_m.assert_not_called()
        recover_m.assert_not_called()

    def test_dead_in_flight_pid_does_not_block_recovery(self):
        # A stale in-flight entry whose pid is dead is the abandoned case —
        # recovery must proceed (the old has_lease was dead and never gated;
        # this confirms a dead worker doesn't falsely keep blocking).
        self._write_old_task('forge', 'stale-inflight-001')
        h.IN_FLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        (h.IN_FLIGHT_DIR / 'stale-inflight-001.json').write_text(
            json.dumps({'task_stem': 'stale-inflight-001', 'pid': 2147483640}))
        with mock.patch.object(h, 'get_active_claude_cwds', return_value=set()), \
             mock.patch.object(h, 'task_already_resolved', return_value=None), \
             mock.patch.object(h, 'recover_task', return_value=True) as recover_m:
            scanned, healed = h.scan_agent_inbox('forge', set())
        recover_m.assert_called_once()
        self.assertEqual(healed, 1)

    def test_worker_gate_matches_sanitized_task_id(self):
        # Regression (#6): a task_id with '.'/':'/space is sanitized to '-' in
        # the worktree name. The gate must match the SANITIZED stem, else a live
        # worker is missed and an actively-building task is wrongly recovered.
        task_id = 'forge-queue.api:build-20260605T010000Z'
        self._write_old_task('forge', task_id)
        safe = h._worktree_safe_stem(task_id)  # '.'/':' -> '-'
        worktree = f'wt-forge-{safe}-20260605T010101Z'
        self.assertNotIn(task_id[:50], worktree)   # raw substring would miss
        self.assertTrue(h.has_active_worker(task_id, {worktree}))
        # And end-to-end: scan short-circuits (no recovery) when that worker is live.
        with mock.patch.object(h, 'get_active_claude_cwds', return_value={worktree}), \
             mock.patch.object(h, 'task_already_resolved') as resolved_m, \
             mock.patch.object(h, 'recover_task') as recover_m:
            h.scan_agent_inbox('forge', {worktree})
        resolved_m.assert_not_called()
        recover_m.assert_not_called()

    def test_worker_gate_short_stem_does_not_false_match(self):
        # audit #26: a short/common task stem ('b') must NOT match an unrelated
        # worktree by bare substring, which made the gate report a phantom live
        # worker and permanently strand the abandoned task. Below id_match's
        # floor it no longer matches, so recovery proceeds.
        self.assertFalse(h.has_active_worker('b', {'wt-forge-rebuild-pipeline-001'}))
        # ...but a legitimately-short stem MUST still match its OWN worktree, or
        # we would double-dispatch live work. The match uses min_len=1 (the
        # worktree name is structured), so no length floor drops it.
        self.assertTrue(h.has_active_worker('b', {'wt-forge-b-20260605T010101Z'}))
        # A long stem that appears only as an INFIX of a larger worktree token
        # (not boundary-delimited) must not match.
        self.assertFalse(
            h.has_active_worker('configuration-management',
                                {'wt-forge-reconfiguration-management-extras-1'}))
        # Sanity: the same stem as a real boundary-delimited token DOES match.
        self.assertTrue(
            h.has_active_worker('configuration-management',
                                {'wt-forge-configuration-management-001'}))


class DispatchLeaseScanGateTest(_IsolatedRoot):
    """The agent-level gate: while inbox_watcher holds inbox:<agent>, the healer
    must defer the WHOLE agent (audit H1 — the in-flight/worker gates can't see a
    task still in ensure_worktree_for_task setup)."""

    def test_scan_defers_whole_agent_when_lease_live(self):
        self._write_old_task('forge', 'mid-dispatch-001')
        # Override the _IsolatedRoot default (free) -> lease held.
        with mock.patch.object(h, 'has_live_dispatch_lease', return_value=True), \
             mock.patch.object(h, 'get_active_claude_cwds', return_value=set()), \
             mock.patch.object(h, 'task_already_resolved') as resolved_m, \
             mock.patch.object(h, 'recover_task') as recover_m:
            scanned, healed = h.scan_agent_inbox('forge', set())
        # Deferred before any per-task gate is consulted; nothing renamed.
        resolved_m.assert_not_called()
        recover_m.assert_not_called()
        self.assertEqual((scanned, healed), (0, 0))

    def test_scan_proceeds_when_lease_free(self):
        self._write_old_task('forge', 'lease-free-001')
        with mock.patch.object(h, 'has_live_dispatch_lease', return_value=False), \
             mock.patch.object(h, 'get_active_claude_cwds', return_value=set()), \
             mock.patch.object(h, 'task_already_resolved', return_value=None), \
             mock.patch.object(h, 'recover_task', return_value=True) as recover_m:
            scanned, healed = h.scan_agent_inbox('forge', set())
        recover_m.assert_called_once()
        self.assertEqual(healed, 1)


class DispatchLeaseGateTest(unittest.TestCase):
    """has_live_dispatch_lease delegates the read-only "held?" decision to
    dispatch_lease.is_held (the owning module), and fails open on any error.
    The held/stale/dead/no-file/off-mode logic itself is tested against the real
    predicate in test_dispatch_lease_is_held.py."""

    def _fake_dispatch_lease(self, held):
        fake = types.ModuleType('dispatch_lease')
        fake.is_held = lambda identity: held
        return mock.patch.dict(sys.modules, {'dispatch_lease': fake})

    def test_delegates_true(self):
        with self._fake_dispatch_lease(True):
            self.assertTrue(h.has_live_dispatch_lease('forge'))

    def test_delegates_false(self):
        with self._fake_dispatch_lease(False):
            self.assertFalse(h.has_live_dispatch_lease('forge'))

    def test_passes_inbox_prefixed_identity(self):
        seen = {}
        fake = types.ModuleType('dispatch_lease')
        fake.is_held = lambda identity: seen.setdefault('id', identity) and False
        with mock.patch.dict(sys.modules, {'dispatch_lease': fake}):
            h.has_live_dispatch_lease('beacon')
        self.assertEqual(seen['id'], 'inbox:beacon')

    def test_import_failure_is_failsafe_not_held(self):
        # dispatch_lease unavailable -> failsafe False so recovery is never
        # blocked by a missing optional dependency.
        with mock.patch.dict(sys.modules, {'dispatch_lease': None}), \
             mock.patch.object(h, 'log'):
            self.assertFalse(h.has_live_dispatch_lease('forge'))

    def test_error_in_is_held_is_failsafe_not_held(self):
        fake = types.ModuleType('dispatch_lease')
        def _boom(identity):
            raise RuntimeError('lease subsystem broken')
        fake.is_held = _boom
        with mock.patch.dict(sys.modules, {'dispatch_lease': fake}), \
             mock.patch.object(h, 'log'):
            self.assertFalse(h.has_live_dispatch_lease('forge'))


class PidAliveTest(unittest.TestCase):
    def test_self_pid_is_alive(self):
        self.assertTrue(h._pid_alive(os.getpid()))

    def test_zero_and_negative_are_not_alive(self):
        # pid<=0 would signal a process GROUP — must never read as a live task.
        self.assertFalse(h._pid_alive(0))
        self.assertFalse(h._pid_alive(-1))

    def test_garbage_is_not_alive(self):
        self.assertFalse(h._pid_alive(None))
        self.assertFalse(h._pid_alive('not-a-pid'))

    def test_dead_pid_is_not_alive(self):
        self.assertFalse(h._pid_alive(2147483640))

    def test_eperm_means_alive(self):
        # A process owned by another uid raises PermissionError on kill(pid,0);
        # it EXISTS, so it must read as alive (else a live cross-user worker is
        # re-dispatched).
        with mock.patch.object(h.os, 'kill', side_effect=PermissionError):
            self.assertTrue(h._pid_alive(12345))


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
