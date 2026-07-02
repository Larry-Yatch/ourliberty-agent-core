"""Shared supabase query-builder fake for the reconcile_open_events tests.

`chain_event_emit.reconcile_open_events` (+ `list_open_event_task_ids` /
`clear_event_by_task_id`) drives three PostgREST verbs — select (list open),
upsert (emit), update (clear). Both the projection-emit suite
(test_approval_sync_phase3a_emit) and the retirement one-shot suite
(test_retire_parked_capture_rows) exercise those same verbs, so the fake lives
here rather than copy-pasted per suite — the drift that motivated
_fake_supabase.py (a different fake, of the `supabase` MODULE) applies equally
here.

Distinct from _fake_supabase.make_fake_supabase: that fakes the `supabase`
module's `create_client` to assert the client-build timeout; this fakes a built
CLIENT's fluent query builder to assert reconcile's read/emit/clear behavior.
"""
from __future__ import annotations


class Resp:
    def __init__(self, data, count):
        self.data = data
        self.count = count


class FakeReconcileClient:
    """Fake supabase client covering the three verbs reconcile_open_events uses:
    select (list open), upsert (emit), update (clear)."""

    def __init__(self, *, open_task_ids=(), select_raises=False):
        self.open = list(open_task_ids)
        self.select_raises = select_raises
        self.upserts = []          # list of row-lists emitted
        self.cleared = []          # (event_type, task_id) pairs
        self._mode = None
        self._cur = {}

    def table(self, name):
        self._cur = {'table': name, 'eq': {}, 'is': []}
        self._mode = None
        return self

    def select(self, cols):
        self._mode = 'select'
        self._cur['range'] = None
        return self

    def range(self, start, end):  # noqa: A003 — mirrors supabase-py
        self._cur['range'] = (start, end)
        return self

    def update(self, values, **kw):
        self._mode = 'update'
        self._cur['update'] = values
        return self

    def upsert(self, rows, **kw):
        self._mode = 'upsert'
        self._cur['rows'] = rows
        return self

    def eq(self, col, val):  # noqa: A003 — mirrors supabase-py
        self._cur['eq'][col] = val
        return self

    def is_(self, col, val):
        self._cur['is'].append((col, val))
        return self

    def execute(self):
        if self._mode == 'select':
            if self.select_raises:
                raise RuntimeError('supabase select down')
            rng = self._cur.get('range')
            page = (self.open[rng[0]:rng[1] + 1] if rng is not None
                    else self.open)
            data = [{'task_id': t} for t in page]
            return Resp(data, len(data))
        if self._mode == 'upsert':
            self.upserts.append(self._cur['rows'])
            return Resp([], 0)
        if self._mode == 'update':
            self.cleared.append((self._cur['eq'].get('event_type'),
                                 self._cur['eq'].get('task_id')))
            return Resp([{'event_id': 'x'}], 1)
        return Resp([], 0)
