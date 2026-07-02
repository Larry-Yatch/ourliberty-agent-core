"""Regression tests for _fetch_chain_events_for_agent pagination.

Before the fix the fetch issued a single query with no order/limit, so
PostgREST silently capped it at 1000 rows and returned an arbitrary slice —
the in_review/done_today lanes then dropped closing events or review_requests
once an agent exceeded 1000 events in the window. These tests prove the fetch
now returns the COMPLETE window by paging under a stable order.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import dashboard_api as d  # noqa: E402


class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Minimal PostgREST fluent-builder stand-in. Records the .range() windows
    and slices a fixed in-memory row list, capping each response at
    ``server_cap`` to emulate PostgREST's max-rows ceiling."""

    def __init__(self, rows, server_cap=1000, raise_on_execute=False):
        self._rows = rows
        self._cap = server_cap
        self._raise = raise_on_execute
        self.ranges = []
        self.orders = []
        self._lo = 0
        self._hi = None

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def order(self, col, **k):
        self.orders.append(col)
        return self

    def range(self, lo, hi):
        self._lo, self._hi = lo, hi
        self.ranges.append((lo, hi))
        return self

    def execute(self):
        if self._raise:
            raise RuntimeError('boom')
        window = self._rows[self._lo:self._hi + 1]
        return _FakeResp(window[:self._cap])


class _FakeClient:
    def __init__(self, rows, **kw):
        self.q = _FakeQuery(rows, **kw)

    def table(self, name):
        return self.q


def _rows(n):
    return [{'event_id': f'e{i:05}', 'agent': 'forge', 'event_type': 'x',
             'ts': f'2026-06-{1 + i // 1440:02}T00:00:00+00:00'} for i in range(n)]


class FetchPaginationTests(unittest.TestCase):
    def test_returns_full_window_across_pages(self):
        client = _FakeClient(_rows(1178))
        out = d._fetch_chain_events_for_agent(client, 'forge')
        self.assertEqual(len(out), 1178)                       # nothing truncated
        self.assertEqual([r['event_id'] for r in out],
                         [f'e{i:05}' for i in range(1178)])     # order preserved
        self.assertEqual(client.q.ranges, [(0, 999), (1000, 1999)])
        self.assertIn('ts', client.q.orders)                   # stable order applied
        self.assertIn('event_id', client.q.orders)

    def test_exactly_page_size_triggers_confirming_empty_page(self):
        client = _FakeClient(_rows(1000))
        out = d._fetch_chain_events_for_agent(client, 'forge')
        self.assertEqual(len(out), 1000)
        # A full first page can't prove the tail, so a second (empty) page runs.
        self.assertEqual(client.q.ranges, [(0, 999), (1000, 1999)])

    def test_short_first_page_stops_immediately(self):
        client = _FakeClient(_rows(42))
        out = d._fetch_chain_events_for_agent(client, 'forge')
        self.assertEqual(len(out), 42)
        self.assertEqual(client.q.ranges, [(0, 999)])          # one query only

    def test_none_client_returns_none(self):
        self.assertIsNone(d._fetch_chain_events_for_agent(None, 'forge'))

    def test_query_error_returns_none_not_partial(self):
        # None (not []) so callers degrade the lane instead of resurrecting
        # every open review_request as a phantom.
        client = _FakeClient(_rows(10), raise_on_execute=True)
        self.assertIsNone(d._fetch_chain_events_for_agent(client, 'forge'))

    def test_max_pages_backstop(self):
        # Server never returns a short page (cap==page size, infinite rows-ish):
        # the loop must stop at the backstop, not spin forever.
        client = _FakeClient(_rows(60000))
        out = d._fetch_chain_events_for_agent(client, 'forge')
        self.assertEqual(len(client.q.ranges), d._QUEUE_EVENTS_MAX_PAGES)
        self.assertEqual(len(out), d._QUEUE_EVENTS_MAX_PAGES * d._QUEUE_EVENTS_PAGE_SIZE)


if __name__ == '__main__':
    unittest.main()
