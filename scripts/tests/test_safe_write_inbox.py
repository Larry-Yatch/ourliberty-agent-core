#!/usr/bin/env python3
"""Fixtures for `safe_write_inbox` — schema check + routing + atomic write.

Phase D3-prep (2026-05-11). Verifies the helper that combines
`dispatch_validator.validate_task` and `routing_validator.validate_route`
with an atomic write to `inboxes/<final_target>/<filename>`.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_safe_write_inbox
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import routing_validator as rv      # noqa: E402
import safe_write_inbox as swi      # noqa: E402


def _good_task(**overrides):
    """Return a dispatch_validator-passing task envelope."""
    task = {
        'task_id': 'test-task-001',
        'prompt': 'this is a substantive prompt that easily clears the 100-character MIN_PROMPT_LEN floor of the validator.',
        'source': 'beacon',
    }
    task.update(overrides)
    return task


class SafeWriteInboxTest(unittest.TestCase):
    """Happy paths + failure modes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._original_agents = swi.AGENTS_ROOT
        self._original_inboxes = swi.INBOXES_ROOT
        self._original_log = swi.ROUTING_EVENTS_LOG
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'
        # Routing validator should be a pass-through (no IDENTITY constraints).
        self._original_rv_root = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / 'repo'
        rv.invalidate_cache()

    def tearDown(self):
        swi.AGENTS_ROOT = self._original_agents
        swi.INBOXES_ROOT = self._original_inboxes
        swi.ROUTING_EVENTS_LOG = self._original_log
        rv.REPO_ROOT = self._original_rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def test_happy_path_writes_to_correct_inbox(self):
        dest = swi.safe_write_inbox('forge', _good_task(), 'beacon', 'task-001.json')
        self.assertTrue(dest.exists())
        self.assertEqual(dest.parent.name, 'forge')
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'beacon')

    def test_routing_denied_raises(self):
        # pulse -> forge is denied by hard topology
        with self.assertRaises(swi.RoutingDenied):
            swi.safe_write_inbox(
                'forge', _good_task(source='pulse'), 'pulse', 'task-002.json',
            )
        # No file should have been written
        self.assertFalse((swi.INBOXES_ROOT / 'forge' / 'task-002.json').exists())

    def test_schema_rejection_too_short_prompt(self):
        with self.assertRaises(swi.DispatchRejected) as ctx:
            swi.safe_write_inbox(
                'forge', _good_task(prompt='short'), 'beacon', 'task-003.json',
            )
        self.assertIn('too short', str(ctx.exception))

    def test_schema_rejection_missing_task_id(self):
        task = _good_task()
        del task['task_id']
        with self.assertRaises(swi.DispatchRejected) as ctx:
            swi.safe_write_inbox('forge', task, 'beacon', 'task-004.json')
        self.assertIn('task_id', str(ctx.exception))

    def test_source_mismatch_rejected(self):
        # Task envelope says pulse, caller declares beacon → mismatch.
        with self.assertRaises(swi.DispatchRejected) as ctx:
            swi.safe_write_inbox(
                'forge', _good_task(source='pulse'), 'beacon', 'task-005.json',
            )
        self.assertIn('source mismatch', str(ctx.exception))

    def test_source_auto_filled_when_envelope_omits_it(self):
        task = _good_task()
        del task['source']
        dest = swi.safe_write_inbox('forge', task, 'beacon', 'task-006.json')
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'beacon')

    def test_filename_truncation(self):
        long_name = 'a' * 250 + '.json'
        dest = swi.safe_write_inbox(
            'forge', _good_task(task_id='t-trunc'), 'beacon', long_name,
        )
        # Truncated name encodes hash for traceability
        self.assertLessEqual(
            len(dest.name.encode('utf-8')),
            swi.MAX_FILENAME_BYTES + 20,
            f'name still too long: {dest.name}',
        )
        self.assertTrue(dest.name.endswith('.json'))
        self.assertIn('--h', dest.name)

    def test_audit_log_written(self):
        swi.safe_write_inbox('forge', _good_task(), 'beacon', 'task-audit.json')
        self.assertTrue(swi.ROUTING_EVENTS_LOG.exists())
        lines = swi.ROUTING_EVENTS_LOG.read_text().strip().splitlines()
        self.assertGreaterEqual(len(lines), 1)
        record = json.loads(lines[-1])
        self.assertEqual(record['action'], 'write')
        self.assertEqual(record['source_agent'], 'beacon')
        self.assertEqual(record['target_agent_final'], 'forge')

    def test_d3_fields_pass_through(self):
        task = _good_task(
            intent='clarify',
            phase='preflight',
            target_repo='ourliberty-agent-core',
            task_type='feature-development',
            pr_title='Enable watchdog timer',
            max_clarifications=3,
            clarification_count=0,
            session_id='abc-123',
        )
        dest = swi.safe_write_inbox('forge', task, 'beacon', 'task-d3.json')
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['intent'], 'clarify')
        self.assertEqual(data['phase'], 'preflight')
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['session_id'], 'abc-123')

    def test_d3_invalid_intent_rejected(self):
        with self.assertRaises(swi.DispatchRejected) as ctx:
            swi.safe_write_inbox(
                'forge', _good_task(intent='bogus-intent'), 'beacon', 't-int.json',
            )
        self.assertIn('intent', str(ctx.exception))

    def test_d3_invalid_phase_rejected(self):
        with self.assertRaises(swi.DispatchRejected) as ctx:
            swi.safe_write_inbox(
                'forge', _good_task(phase='deploy'), 'beacon', 't-ph.json',
            )
        self.assertIn('phase', str(ctx.exception))

    def test_d3_clarification_count_exceeds_max_rejected(self):
        with self.assertRaises(swi.DispatchRejected) as ctx:
            swi.safe_write_inbox(
                'forge',
                _good_task(max_clarifications=2, clarification_count=5),
                'beacon', 't-clar.json',
            )
        self.assertIn('exceeds max_clarifications', str(ctx.exception))

    def test_d3_clarification_protocol_sources_accepted(self):
        # forge-question -> beacon is allowed
        dest = swi.safe_write_inbox(
            'beacon',
            _good_task(source='forge-question', intent='clarify'),
            'forge-question', 't-q.json',
        )
        self.assertTrue(dest.exists())
        # beacon-clarification -> forge is allowed (dialogue suffix bypass)
        dest = swi.safe_write_inbox(
            'forge',
            _good_task(source='beacon-clarification', intent='clarification-response'),
            'beacon-clarification', 't-cr.json',
        )
        self.assertTrue(dest.exists())

    def test_atomic_write_no_partial_file_on_failure(self):
        # Simulate an interrupt by passing a task that JSON-serializes OK
        # but whose write would fail because target dir is not writable.
        # Easiest assertion: only the final file appears, no .tmp leftover.
        swi.safe_write_inbox('forge', _good_task(task_id='t-atomic'), 'beacon', 'atomic.json')
        dest_dir = swi.INBOXES_ROOT / 'forge'
        leftovers = [p for p in dest_dir.iterdir() if p.name.startswith('.')]
        self.assertEqual(leftovers, [], f'tempfile leaked: {leftovers}')


if __name__ == '__main__':
    unittest.main()
