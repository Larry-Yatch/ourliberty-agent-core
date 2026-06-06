#!/usr/bin/env python3
"""Tests for scripts/supabase_chunk.py::chunked_clear — the shared chunked
PostgREST clear used by dashboard_api cleanup-review, clear_verified,
triage_decisions, approvals_cleanup, and heal_stale_approvals.

The contract under test is the partial-failure one: a single `.in_(all_ids)`
would blow the PostgREST URL length, so the clear is batched, and there is no
clear RPC / client-side transaction. A mid-loop `.execute()` failure must NOT
be silent — it must raise ChunkedClearError carrying EXACTLY the ids the prior
(successful) chunks wrote, so the caller can report and reverse precisely those.

Run:
    cd /home/larry/agent-core && \\
        python3 -m unittest scripts.tests.test_supabase_chunk
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

from supabase_chunk import chunked_clear, ChunkedClearError  # noqa: E402


class _FakeBuilder:
    """One PostgREST update().in_().execute() chain over a parent recorder.

    `fail_on_chunk` (a 0-based chunk index on the parent) makes the matching
    execute() raise, modelling a mid-loop PostgREST error.
    """

    def __init__(self, parent: '_FakeClient'):
        self.parent = parent
        self._values: Optional[dict[str, Any]] = None
        self._col: Optional[str] = None
        self._ids: list[Any] = []

    def update(self, values: dict[str, Any]):
        self._values = values
        return self

    def in_(self, col: str, ids: list[Any]):
        self._col = col
        self._ids = list(ids)
        return self

    def execute(self):
        idx = len(self.parent.executed_chunks)
        if idx == self.parent.fail_on_chunk:
            raise RuntimeError(f'simulated PostgREST failure on chunk {idx}')
        # Record only chunks that "land" in the DB.
        self.parent.executed_chunks.append(list(self._ids))
        self.parent.cleared_in_db.update(self._ids)
        self.parent.update_values = self._values
        self.parent.id_columns.add(self._col)
        return object()


class _FakeClient:
    def __init__(self, fail_on_chunk: int = -1):
        self.fail_on_chunk = fail_on_chunk
        self.executed_chunks: list[list[Any]] = []
        self.cleared_in_db: set[Any] = set()
        self.tables: list[str] = []
        self.update_values: Optional[dict[str, Any]] = None
        self.id_columns: set[Optional[str]] = set()

    def table(self, name: str):
        self.tables.append(name)
        return _FakeBuilder(self)


VALUES = {'read_at': '2026-06-06T00:00:00+00:00'}


class ChunkedClearHappyPath(unittest.TestCase):

    def test_empty_ids_is_noop(self):
        client = _FakeClient()
        out = chunked_clear(client, 'chain_events', [], VALUES)
        self.assertEqual(out, [])
        self.assertEqual(client.executed_chunks, [])
        self.assertEqual(client.tables, [])  # no .table() call at all

    def test_single_partial_chunk(self):
        client = _FakeClient()
        ids = [f'e{i}' for i in range(5)]
        out = chunked_clear(client, 'chain_events', ids, VALUES, chunk_size=200)
        self.assertEqual(out, ids)  # order preserved, all returned
        self.assertEqual(client.executed_chunks, [ids])
        self.assertEqual(client.update_values, VALUES)
        self.assertEqual(client.id_columns, {'event_id'})

    def test_chunk_boundaries_exact_multiple(self):
        client = _FakeClient()
        ids = [f'e{i}' for i in range(6)]
        out = chunked_clear(client, 'chain_events', ids, VALUES, chunk_size=2)
        self.assertEqual(out, ids)
        self.assertEqual(client.executed_chunks,
                         [['e0', 'e1'], ['e2', 'e3'], ['e4', 'e5']])

    def test_chunk_boundaries_remainder(self):
        client = _FakeClient()
        ids = [f'e{i}' for i in range(5)]
        out = chunked_clear(client, 'chain_events', ids, VALUES, chunk_size=2)
        self.assertEqual(out, ids)
        self.assertEqual(client.executed_chunks,
                         [['e0', 'e1'], ['e2', 'e3'], ['e4']])

    def test_custom_id_column(self):
        client = _FakeClient()
        chunked_clear(client, 'other', ['a'], VALUES, id_column='task_id')
        self.assertEqual(client.id_columns, {'task_id'})
        self.assertEqual(client.tables, ['other'])


class ChunkedClearPartialFailure(unittest.TestCase):

    def test_midloop_failure_carries_cleared_so_far(self):
        # 5 ids, chunk_size 2 -> chunks [e0,e1], [e2,e3], [e4].
        # Fail on chunk index 2 (the third chunk, [e4]): first two landed.
        client = _FakeClient(fail_on_chunk=2)
        ids = [f'e{i}' for i in range(5)]
        with self.assertRaises(ChunkedClearError) as ctx:
            chunked_clear(client, 'chain_events', ids, VALUES, chunk_size=2)
        err = ctx.exception
        self.assertEqual(err.cleared_ids, ['e0', 'e1', 'e2', 'e3'])
        self.assertEqual(err.cleared_count, 4)
        self.assertEqual(err.total, 5)
        self.assertEqual(err.chunk_index, 2)
        # cleared-so-far matches exactly what actually landed in the DB.
        self.assertEqual(set(err.cleared_ids), client.cleared_in_db)
        # cause is chained and preserved.
        self.assertIsInstance(err.cause, RuntimeError)
        self.assertIsInstance(err.__cause__, RuntimeError)

    def test_failure_on_first_chunk_reports_zero_cleared(self):
        client = _FakeClient(fail_on_chunk=0)
        ids = [f'e{i}' for i in range(5)]
        with self.assertRaises(ChunkedClearError) as ctx:
            chunked_clear(client, 'chain_events', ids, VALUES, chunk_size=2)
        err = ctx.exception
        self.assertEqual(err.cleared_ids, [])
        self.assertEqual(err.cleared_count, 0)
        self.assertEqual(err.chunk_index, 0)
        self.assertEqual(client.cleared_in_db, set())

    def test_cleared_ids_snapshot_is_independent(self):
        # Mutating the returned/raised list must not be the helper's internal
        # accumulator (defensive copy in the exception).
        client = _FakeClient(fail_on_chunk=1)
        ids = ['e0', 'e1', 'e2']
        with self.assertRaises(ChunkedClearError) as ctx:
            chunked_clear(client, 'chain_events', ids, VALUES, chunk_size=1)
        err = ctx.exception
        snapshot = list(err.cleared_ids)
        err.cleared_ids.append('tampered')
        self.assertEqual(snapshot, ['e0'])


if __name__ == '__main__':
    unittest.main()
