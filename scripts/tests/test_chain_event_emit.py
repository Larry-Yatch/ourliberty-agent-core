#!/usr/bin/env python3
"""Tests for chain_event_emit (E4.4e PR-A push writer).

Covers:
  - emit_event success path: upsert called with correct row shape, returns True
  - emit_event failure path: returns False, logs WARN, does not raise
  - unknown event_type rejected before any Supabase call
  - event_id is deterministic via compute_event_id
  - payload sanitization redacts credential-shaped keys before insert
  - client unavailable (env unset) returns False without raising

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_chain_event_emit
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import chain_event_emit as cee  # noqa: E402
import chain_event_shipper as ces  # noqa: E402


class _RecordingClient:
    """Mock supabase client capturing the final upsert call."""

    def __init__(self, *, raise_on_execute: Exception | None = None):
        self.captured_table: str | None = None
        self.captured_rows: list[dict[str, Any]] | None = None
        self.captured_kwargs: dict[str, Any] | None = None
        self._raise = raise_on_execute

    def table(self, name: str):
        self.captured_table = name
        return self

    def upsert(self, rows, **kwargs):
        self.captured_rows = rows
        self.captured_kwargs = kwargs
        return self

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self


class TestEmitEvent(unittest.TestCase):

    def setUp(self):
        cee.reset_client_for_testing()
        self._silent_logger = logging.getLogger('test-chain_event_emit')
        self._silent_logger.setLevel(logging.CRITICAL)

    def test_success_path_returns_true_and_calls_upsert(self):
        client = _RecordingClient()
        ok = cee.emit_event(
            event_type='approval_request',
            agent='beacon',
            task_id='task-001',
            payload={'proposing_agent': 'beacon', 'prompt': 'hello'},
            ts='2026-05-26T18:00:00+00:00',
            client=client,
        )
        self.assertTrue(ok)
        self.assertEqual(client.captured_table, 'chain_events')
        self.assertIsNotNone(client.captured_rows)
        self.assertEqual(len(client.captured_rows), 1)
        row = client.captured_rows[0]
        self.assertEqual(row['event_type'], 'approval_request')
        self.assertEqual(row['agent'], 'beacon')
        self.assertEqual(row['task_id'], 'task-001')
        self.assertEqual(row['ts'], '2026-05-26T18:00:00+00:00')
        self.assertIn('event_id', row)
        self.assertEqual(
            row['event_id'],
            ces.compute_event_id('task-001', 'approval_request',
                                  '2026-05-26T18:00:00+00:00'),
        )
        # Upsert dedup contract — same shape as SupabaseSink uses.
        self.assertEqual(client.captured_kwargs.get('on_conflict'), 'event_id')
        self.assertTrue(client.captured_kwargs.get('ignore_duplicates'))

    def test_unknown_event_type_returns_false_without_upsert(self):
        client = _RecordingClient()
        ok = cee.emit_event(
            event_type='not_a_real_type',
            agent='beacon',
            task_id='task-002',
            payload={},
            ts='2026-05-26T18:00:00+00:00',
            client=client,
            logger=self._silent_logger,
        )
        self.assertFalse(ok)
        self.assertIsNone(client.captured_rows)

    def test_upsert_failure_returns_false_does_not_raise(self):
        client = _RecordingClient(raise_on_execute=RuntimeError('boom'))
        # Should NOT propagate the RuntimeError.
        ok = cee.emit_event(
            event_type='clarify_request',
            agent='forge',
            task_id='task-003',
            payload={},
            ts='2026-05-26T18:00:00+00:00',
            client=client,
            logger=self._silent_logger,
        )
        self.assertFalse(ok)

    def test_event_id_deterministic(self):
        client_a = _RecordingClient()
        client_b = _RecordingClient()
        cee.emit_event(
            event_type='approval_request', agent='beacon', task_id='task-x',
            payload={}, ts='2026-05-26T18:00:00+00:00', client=client_a,
        )
        cee.emit_event(
            event_type='approval_request', agent='beacon', task_id='task-x',
            payload={}, ts='2026-05-26T18:00:00+00:00', client=client_b,
        )
        self.assertEqual(
            client_a.captured_rows[0]['event_id'],
            client_b.captured_rows[0]['event_id'],
        )

    def test_payload_sanitized_before_insert(self):
        client = _RecordingClient()
        cee.emit_event(
            event_type='approval_request',
            agent='beacon',
            task_id='task-004',
            payload={
                'prompt': 'hello',
                'api_key': 'sk-secret-12345',
                'nested': {'access_token': 'tok-xyz'},
            },
            ts='2026-05-26T18:00:00+00:00',
            client=client,
        )
        row = client.captured_rows[0]
        self.assertEqual(row['payload']['prompt'], 'hello')
        self.assertEqual(row['payload']['api_key'], '<redacted>')
        self.assertEqual(row['payload']['nested']['access_token'], '<redacted>')

    def test_client_unavailable_returns_false(self):
        # No client passed; module-level client never set; env unset.
        original_url = cee.os.environ.pop('SUPABASE_URL', None)
        original_key = cee.os.environ.pop('SUPABASE_SERVICE_ROLE_KEY', None)
        try:
            cee.reset_client_for_testing()
            ok = cee.emit_event(
                event_type='approval_request',
                agent='beacon',
                task_id='task-no-client',
                payload={},
                logger=self._silent_logger,
            )
            self.assertFalse(ok)
        finally:
            if original_url is not None:
                cee.os.environ['SUPABASE_URL'] = original_url
            if original_key is not None:
                cee.os.environ['SUPABASE_SERVICE_ROLE_KEY'] = original_key
            cee.reset_client_for_testing()


if __name__ == '__main__':
    unittest.main()
