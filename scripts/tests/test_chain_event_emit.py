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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import logging
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import chain_event_emit as cee  # noqa: E402
import chain_event_shipper as ces  # noqa: E402
from _fake_supabase import make_fake_supabase  # noqa: E402


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


class _ClearResp:
    def __init__(self, data, count):
        self.data = data
        self.count = count


class _ClearClient:
    """Mock supabase client capturing the keyed read_at UPDATE + its filters.

    Mirrors the PostgREST fluent builder for the subset clear_approval_request
    uses: table().update(values, count=).eq().eq().is_().execute()."""

    def __init__(self, *, data=None, count=0, raise_on_execute=None):
        self.captured_table = None
        self.captured_update = None
        self.captured_count_kw = None
        self.filters = {}
        self.is_null = []
        self._data = data
        self._count = count
        self._raise = raise_on_execute
        self.executed = False

    def table(self, name):
        self.captured_table = name
        return self

    def update(self, values, **kwargs):
        self.captured_update = values
        self.captured_count_kw = kwargs.get('count')
        return self

    def eq(self, col, val):  # noqa: A003 — mirrors supabase-py builder name
        self.filters[col] = val
        return self

    def is_(self, col, val):
        self.is_null.append((col, val))
        return self

    def execute(self):
        self.executed = True
        if self._raise is not None:
            raise self._raise
        return _ClearResp(self._data, self._count)


class TestClearApprovalRequest(unittest.TestCase):
    """clear_approval_request — the resolution-side mirror of emit_event: a
    single keyed read_at UPDATE, idempotent on the read_at-null guard, best
    effort (never raises). No healer sweep, no backup file."""

    def setUp(self):
        cee.reset_client_for_testing()
        self._silent_logger = logging.getLogger('test-clear-approval')
        self._silent_logger.setLevel(logging.CRITICAL)

    def test_empty_task_id_returns_false_without_touching_client(self):
        client = _ClearClient(data=[{'event_id': 'e'}], count=1)
        ok = cee.clear_approval_request(
            '', client=client, logger=self._silent_logger)
        self.assertFalse(ok)
        self.assertFalse(client.executed)

    def test_success_sets_read_at_with_correct_filters(self):
        client = _ClearClient(data=[{'event_id': 'e1'}], count=1)
        ok = cee.clear_approval_request(
            'task-42', ts='2026-06-09T22:00:00+00:00', client=client)
        self.assertTrue(ok)
        self.assertEqual(client.captured_table, 'chain_events')
        # Sets read_at to the supplied ts...
        self.assertEqual(client.captured_update, {'read_at': '2026-06-09T22:00:00+00:00'})
        self.assertEqual(client.captured_count_kw, 'exact')
        # ...scoped to this task's pending approval_request rows only.
        self.assertEqual(client.filters.get('event_type'), 'approval_request')
        self.assertEqual(client.filters.get('task_id'), 'task-42')
        self.assertIn(('read_at', 'null'), client.is_null)

    def test_true_when_only_count_reports_a_row(self):
        # PostgREST may return count without echoing rows in data.
        client = _ClearClient(data=[], count=2)
        self.assertTrue(cee.clear_approval_request('task-x', client=client))

    def test_false_when_no_row_matched(self):
        client = _ClearClient(data=[], count=0)
        self.assertFalse(cee.clear_approval_request('task-absent', client=client))

    def test_supabase_error_returns_false_does_not_raise(self):
        client = _ClearClient(raise_on_execute=RuntimeError('supabase down'))
        ok = cee.clear_approval_request(
            'task-err', client=client, logger=self._silent_logger)
        self.assertFalse(ok)

    def test_client_unavailable_returns_false(self):
        # No client passed; module-level client never set; env unset.
        original_url = cee.os.environ.pop('SUPABASE_URL', None)
        original_key = cee.os.environ.pop('SUPABASE_SERVICE_ROLE_KEY', None)
        try:
            cee.reset_client_for_testing()
            ok = cee.clear_approval_request(
                'task-no-client', logger=self._silent_logger)
            self.assertFalse(ok)
        finally:
            if original_url is not None:
                cee.os.environ['SUPABASE_URL'] = original_url
            if original_key is not None:
                cee.os.environ['SUPABASE_SERVICE_ROLE_KEY'] = original_key
            cee.reset_client_for_testing()


@unittest.skipIf(
    'pytest' in sys.modules,
    'conftest autouse fixture neutralizes _get_client under pytest; run via '
    'unittest (python3 -m unittest scripts.tests.test_chain_event_emit)',
)
class TestGetClientPinsTimeout(unittest.TestCase):
    """_get_client must pin the PostgREST timeout so a Supabase black-hole
    fails fast instead of blocking the single-threaded notifier loop."""

    _GUARD_KEYS = (
        'OURLIBERTY_DISABLE_LIVE_EMIT', 'PYTEST_CURRENT_TEST',
        'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY',
    )

    def setUp(self):
        cee.reset_client_for_testing()
        self._saved = {k: cee.os.environ.get(k) for k in self._GUARD_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                cee.os.environ.pop(k, None)
            else:
                cee.os.environ[k] = v
        cee.reset_client_for_testing()

    def _build_client(self, fake) -> Any:
        """Lift the test-isolation guards and build a client against `fake`.

        The fake create_client makes this safe (no network). tearDown restores
        the guards even if an assertion fails mid-block.
        """
        with mock.patch.dict(sys.modules, {'supabase': fake}):
            cee.os.environ.pop('OURLIBERTY_DISABLE_LIVE_EMIT', None)
            cee.os.environ.pop('PYTEST_CURRENT_TEST', None)
            cee.os.environ['SUPABASE_URL'] = 'https://example.supabase.co'
            cee.os.environ['SUPABASE_SERVICE_ROLE_KEY'] = 'svc-role-key'
            return cee._get_client()

    def test_get_client_passes_postgrest_timeout_option(self):
        capture: dict[str, Any] = {}
        client = self._build_client(make_fake_supabase(capture))
        self.assertIsNotNone(client)
        self.assertEqual(capture.get('url'), 'https://example.supabase.co')
        # The whole point: an options object carrying the shipper's timeout.
        options = capture.get('options')
        self.assertIsNotNone(
            options, 'create_client called without options= — timeout not pinned')
        self.assertEqual(
            options.postgrest_client_timeout, ces.SUPABASE_TIMEOUT_SEC)
        self.assertEqual(ces.SUPABASE_TIMEOUT_SEC, 15)

    def test_get_client_degrades_when_create_client_rejects_options(self):
        # supabase-py whose create_client signature doesn't accept options=:
        # _get_client must fall back to an un-pinned client, NOT raise into
        # emit_event (which calls _get_client outside its try/except).
        capture: dict[str, Any] = {}
        client = self._build_client(
            make_fake_supabase(capture, reject_options=True))
        self.assertIsNotNone(client)  # fell back rather than raising
        self.assertIsNone(capture.get('options'))  # bare create_client(url, key)


if __name__ == '__main__':
    unittest.main()
