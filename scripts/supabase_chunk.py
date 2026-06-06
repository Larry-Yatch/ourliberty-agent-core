#!/usr/bin/env python3
"""Shared chunked PostgREST clear with a partial-failure contract.

Five+ call sites (dashboard_api cleanup-review, clear_verified, triage_decisions,
approvals_cleanup, heal_stale_approvals) all set `read_at` on a batch of
`chain_events` rows the same way: split the id list into chunks of 200 (a single
`.in_(all_ids)` would blow the PostgREST URL-length limit) and issue one
`.update(values).in_(id_column, chunk).execute()` per chunk. There is no clear
RPC in the schema and PostgREST gives no client-side transaction, so a mid-loop
`.execute()` failure leaves EARLIER chunks already written in the DB while the
exception aborts the caller. Previously each copy propagated an opaque error and
lost track of WHICH rows it had already cleared.

`chunked_clear` makes that contract uniform: it tracks cleared ids incrementally
and, on a chunk failure, raises `ChunkedClearError` carrying the cleared-so-far
list so the caller can report exactly what was written and reverse precisely
those rows (the backup, written up front by the caller, already holds them all;
reversing an uncleared row is a harmless no-op since its read_at is still NULL).
"""
from __future__ import annotations

from typing import Any


class ChunkedClearError(Exception):
    """A chunked clear failed mid-loop.

    Attributes:
      cleared_ids: ids successfully cleared BEFORE the failing chunk.
      cleared_count: ``len(cleared_ids)``.
      total: total number of ids the clear was asked to process.
      chunk_index: zero-based index of the chunk that failed.
      cause: the underlying client exception (also chained via ``__cause__``).
    """

    def __init__(
        self,
        cleared_ids: list[Any],
        total: int,
        chunk_index: int,
        cause: BaseException,
    ) -> None:
        self.cleared_ids = list(cleared_ids)
        self.cleared_count = len(self.cleared_ids)
        self.total = total
        self.chunk_index = chunk_index
        self.cause = cause
        super().__init__(
            f'chunked clear failed at chunk {chunk_index}: '
            f'{self.cleared_count}/{total} id(s) cleared before failure: {cause}'
        )


def chunked_clear(
    client: Any,
    table: str,
    ids: list[Any],
    values: dict[str, Any],
    *,
    chunk_size: int = 200,
    id_column: str = 'event_id',
) -> list[Any]:
    """Apply ``values`` to ``table`` rows whose ``id_column`` is in ``ids``,
    chunk by chunk, and return the list of ids actually cleared.

    Issues one ``client.table(table).update(values).in_(id_column, chunk)
    .execute()`` per ``chunk_size``-sized slice of ``ids``. On the common path
    every chunk succeeds and the returned list equals ``ids`` (order preserved).

    On a chunk's ``.execute()`` raising, stops and raises `ChunkedClearError`
    carrying the ids cleared by the PRIOR (successful) chunks — the caller owns
    the recovery/reversal decision. An empty ``ids`` is a no-op returning ``[]``.
    """
    cleared: list[Any] = []
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        try:
            client.table(table).update(values).in_(id_column, chunk).execute()
        except Exception as exc:  # noqa: BLE001 — surfaced via ChunkedClearError
            raise ChunkedClearError(
                cleared_ids=cleared,
                total=len(ids),
                chunk_index=i // chunk_size,
                cause=exc,
            ) from exc
        cleared.extend(chunk)
    return cleared
