#!/usr/bin/env python3
"""nervous-system-audit #13 (2026-06-05): when the outbox write fails AFTER a
paid run_claude call, process_task must ARCHIVE the task (so it cannot re-run
and re-pay + duplicate side effects), NOT bump_requeue it back into the inbox.

This mirrors the module's own reap_orphans_on_startup policy ("NEVER re-dispatch
paid work that may have completed") and the run_claude-exception path, which
already archives.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher as iw  # noqa: E402


class OutboxWriteFailureArchivesTest(unittest.TestCase):
    AGENT = 'beacon'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig = {
            'INBOXES_ROOT': iw.INBOXES_ROOT,
            'OUTBOXES_ROOT': iw.OUTBOXES_ROOT,
            'BLACKBOARD': iw.BLACKBOARD,
            'COSTS_FILE': iw.COSTS_FILE,
            # iw.LOG_FILE is computed at IMPORT from AGENTS_ROOT (test-jail H3).
            # Under a full `discover` run the gate's env can pin
            # OURLIBERTY_AGENTS_ROOT at the REAL ~/agents before the per-file
            # bootstrap fires, so LOG_FILE stays frozen to the live log and
            # every process_task log() below ('outbox write failed for
            # real-paid-001 …', 'simulated ENOSPC') would append to it.
            # Redirect the log sink too so this test cannot leak into prod.
            'LOG_FILE': iw.LOG_FILE,
        }
        iw.INBOXES_ROOT = self.root / 'inboxes'
        iw.OUTBOXES_ROOT = self.root / 'outboxes'
        iw.BLACKBOARD = self.root / 'blackboard'
        iw.COSTS_FILE = iw.BLACKBOARD / 'costs.jsonl'
        iw.LOG_FILE = self.root / 'logs' / 'inbox_watcher.log'
        (iw.INBOXES_ROOT / self.AGENT).mkdir(parents=True, exist_ok=True)
        (iw.INBOXES_ROOT / self.AGENT / '.archive').mkdir(parents=True, exist_ok=True)
        (iw.OUTBOXES_ROOT / self.AGENT).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(iw, k, v)
        self.tmp.cleanup()

    def _write_task(self) -> Path:
        task = {
            'task_id': 'real-paid-001',
            'prompt': 'do the work',
            'source': 'beacon',
        }
        p = iw.INBOXES_ROOT / self.AGENT / 'real-paid-001.json'
        p.write_text(json.dumps(task))
        return p

    def test_outbox_write_failure_archives_without_requeue(self):
        task_file = self._write_task()
        # Models config: the agent has no worktree_enabled → skips worktree.
        models_config = {'agents': {self.AGENT: {'inbox_model': 'claude-sonnet-4-6'}}}

        real_write_text = Path.write_text

        def selective_write(self_path, *a, **k):
            # Fail ONLY the outbox write; leave inbox / archive writes alone.
            if str(self_path).startswith(str(iw.OUTBOXES_ROOT)):
                raise OSError('simulated ENOSPC on outbox write')
            return real_write_text(self_path, *a, **k)

        def fake_run_claude(**kwargs):
            # The agent ran and PAID — populate the out_meta the way the real
            # runner would so the cost-ledger assertion below is meaningful.
            kwargs['out_meta']['cost_usd'] = 0.05
            kwargs['out_meta']['completed_at'] = '2026-06-05T00:00:00Z'
            return (True, 'agent output', 'sess-123')

        with mock.patch.object(
            iw.agent_runner, 'run_claude', side_effect=fake_run_claude,
        ), mock.patch.object(
            iw, 'dispatch_validator',
        ) as dv, mock.patch.object(
            iw, 'routing_validator',
        ) as rvm, mock.patch.object(
            iw, '_rotation_gate_block_reason', return_value=None,
        ), mock.patch.object(
            iw, 'bump_requeue',
        ) as bump, mock.patch.object(
            Path, 'write_text', selective_write,
        ):
            dv.validate_task.return_value = (True, '')
            rvm.check_hard_topology.return_value = (True, '')
            rvm.check_target_repo.return_value = (True, '')
            iw.process_task(self.AGENT, task_file, models_config)

        # The paid task was NOT requeued (no re-run / re-pay).
        bump.assert_not_called()
        # It is gone from the live inbox …
        self.assertFalse(task_file.exists())
        # … and now in .archive, so the next poll cannot pick it up again.
        archived = list((iw.INBOXES_ROOT / self.AGENT / '.archive').glob('*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text())['task_id'], 'real-paid-001')
        # No outbox was persisted (the write failed).
        self.assertEqual(
            list((iw.OUTBOXES_ROOT / self.AGENT).glob('*.json')), [],
        )
        # … but the paid spend was still recorded to the cost ledger (#13:
        # archiving must not make a paid run invisible to budget accounting).
        self.assertTrue(iw.COSTS_FILE.exists())
        rows = [json.loads(ln) for ln in iw.COSTS_FILE.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['task_id'], 'real-paid-001')
        self.assertEqual(rows[0]['cost_usd'], 0.05)


class ProcessTaskTierHoldTest(unittest.TestCase):
    """W1 (spec §4): when run_claude returns TIER_HOLD (no dispatch tier
    available — a TOCTOU after the gate, or a resume whose bound tier benched),
    process_task must HOLD the task in the inbox — no outbox, no archive, no
    requeue-bump — so the next poll re-evaluates. NEVER drop held work."""

    AGENT = 'forge'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig = {k: getattr(iw, k) for k in
                      ('INBOXES_ROOT', 'OUTBOXES_ROOT', 'BLACKBOARD',
                       'COSTS_FILE', 'LOG_FILE')}
        iw.INBOXES_ROOT = self.root / 'inboxes'
        iw.OUTBOXES_ROOT = self.root / 'outboxes'
        iw.BLACKBOARD = self.root / 'blackboard'
        iw.COSTS_FILE = iw.BLACKBOARD / 'costs.jsonl'
        iw.LOG_FILE = self.root / 'logs' / 'inbox_watcher.log'
        (iw.INBOXES_ROOT / self.AGENT).mkdir(parents=True, exist_ok=True)
        (iw.INBOXES_ROOT / self.AGENT / '.archive').mkdir(
            parents=True, exist_ok=True)
        (iw.OUTBOXES_ROOT / self.AGENT).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(iw, k, v)
        self.tmp.cleanup()

    def test_tier_hold_keeps_task_in_inbox(self):
        task = {'task_id': 'held-001', 'prompt': 'work', 'source': 'forge'}
        task_file = iw.INBOXES_ROOT / self.AGENT / 'held-001.json'
        task_file.write_text(json.dumps(task))
        models_config = {'agents': {self.AGENT: {'inbox_model': 'claude-sonnet-4-6'}}}

        def fake_run_claude(**kwargs):
            return (False, 'TIER_HOLD: no dispatch tier available', None)

        with mock.patch.object(
            iw.agent_runner, 'run_claude', side_effect=fake_run_claude,
        ), mock.patch.object(iw, 'dispatch_validator') as dv, \
                mock.patch.object(iw, 'routing_validator') as rvm, \
                mock.patch.object(iw, '_rotation_gate_block_reason',
                                  return_value=None), \
                mock.patch.object(iw, 'bump_requeue') as bump:
            dv.validate_task.return_value = (True, '')
            rvm.check_hard_topology.return_value = (True, '')
            rvm.check_target_repo.return_value = (True, '')
            iw.process_task(self.AGENT, task_file, models_config)

        # Held, not dropped: still in the live inbox, not archived.
        self.assertTrue(task_file.exists())
        self.assertEqual(
            list((iw.INBOXES_ROOT / self.AGENT / '.archive').glob('*.json')), [])
        # No outbox written, no requeue-bump (a hold is not a failure).
        self.assertEqual(
            list((iw.OUTBOXES_ROOT / self.AGENT).glob('*.json')), [])
        bump.assert_not_called()


if __name__ == '__main__':
    unittest.main()
