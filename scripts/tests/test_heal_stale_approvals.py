"""Tests for scripts/heal_stale_approvals.py (auto-clear node N1).

unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- classify_approval: pending NEVER cleared; history cleared; unknown uncertain.
- classify_clarify: clarify_response clears; progressed-past clears; live kept;
  history-resolved-before-clarify-ts kept (uncertain).
- run_once end-to-end with a fake Supabase client: clears only resolved rows,
  never the still-pending approval, writes a backup, is idempotent, and
  --dry-run writes nothing.
- load_beacon_approvals: missing / malformed file is conservative (empty).

Task_ids use the reserved `zz-fixture-` namespace so they can never be confused
with live mock rows (real-* / prod-*) per spec § 7.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


# -------------------- fake Supabase client --------------------

class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Mimics the PostgREST fluent builder for the subset this job uses."""

    def __init__(self, store, update_log):
        self._store = store
        self._update_log = update_log
        self._eq: dict = {}
        self._null: list = []
        self._in = None
        self._range = None
        self._update_patch = None

    def select(self, _cols):
        return self

    def is_(self, col, _val):
        self._null.append(col)
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals):
        self._in = (col, list(vals))
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def update(self, patch):
        self._update_patch = patch
        return self

    def execute(self):
        if self._update_patch is not None:
            col, vals = self._in
            valset = set(vals)
            n = 0
            for row in self._store:
                if row.get(col) in valset:
                    row.update(self._update_patch)
                    n += 1
            self._update_log.append({'ids': list(vals), 'n': n})
            return _Resp([])
        rows = []
        for row in self._store:
            if any(row.get(c) is not None for c in self._null):
                continue
            if all(row.get(k) == v for k, v in self._eq.items()):
                rows.append(row)
        if self._range is not None:
            a, b = self._range
            rows = rows[a:b + 1]
        return _Resp([dict(r) for r in rows])


class _FakeClient:
    def __init__(self, store):
        self._store = store
        self.update_log: list = []

    def table(self, _name):
        return _FakeQuery(self._store, self.update_log)


def _row(event_id, event_type, task_id, ts, read_at=None):
    return {
        'event_id': event_id,
        'event_type': event_type,
        'agent': 'forge',
        'task_id': task_id,
        'ts': ts,
        'read_at': read_at,
    }


class _Base(unittest.TestCase):
    """Repoints AGENTS_ROOT at a temp dir + reloads the module so its
    module-level path constants pick up the env var."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'agents'
        (self.root / 'logs').mkdir(parents=True)
        (self.root / 'blackboard' / 'backups').mkdir(parents=True)
        (self.root / 'state').mkdir(parents=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        import heal_stale_approvals as mod
        self.mod = importlib.reload(mod)
        self.backup_dir = self.root / 'blackboard' / 'backups'

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)


# -------------------- classification --------------------

class TestClassifyApproval(_Base):
    def test_pending_never_cleared(self):
        clear, reason = self.mod.classify_approval(
            _row('e1', 'approval_request', 'zz-fixture-pending', '2026-06-01T00:00:00+00:00'),
            pending_ids={'zz-fixture-pending'}, history_ids=set())
        self.assertFalse(clear)
        self.assertIn('live', reason)

    def test_history_cleared(self):
        clear, _ = self.mod.classify_approval(
            _row('e2', 'approval_request', 'zz-fixture-done', '2026-06-01T00:00:00+00:00'),
            pending_ids=set(), history_ids={'zz-fixture-done'})
        self.assertTrue(clear)

    def test_unknown_uncertain_not_cleared(self):
        clear, reason = self.mod.classify_approval(
            _row('e3', 'approval_request', 'zz-fixture-ghost', '2026-06-01T00:00:00+00:00'),
            pending_ids=set(), history_ids=set())
        self.assertFalse(clear)
        self.assertIn('uncertain', reason)

    def test_pending_wins_even_if_also_in_history(self):
        # A task re-opened: present in both buckets. Pending guard must win.
        clear, reason = self.mod.classify_approval(
            _row('e4', 'approval_request', 'zz-fixture-reopened', '2026-06-01T00:00:00+00:00'),
            pending_ids={'zz-fixture-reopened'}, history_ids={'zz-fixture-reopened'})
        self.assertFalse(clear)
        self.assertIn('live', reason)


class TestClassifyClarify(_Base):
    TS = '2026-06-01T00:00:00+00:00'

    def test_clarify_response_after_clears(self):
        clear, _ = self.mod.classify_clarify(
            _row('c1', 'clarify_request', 'zz-fixture-c', self.TS),
            cresp_ts={'zz-fixture-c': ['2026-06-02T00:00:00+00:00']},
            hist_resolved_at={}, history_ids=set())
        self.assertTrue(clear)

    def test_progressed_past_clears_without_response(self):
        # No clarify_response, but the task's approval resolved at/after the
        # clarify ts -> progressed past. This is the under-clear case (b).
        clear, reason = self.mod.classify_clarify(
            _row('c2', 'clarify_request', 'zz-fixture-shipped', self.TS),
            cresp_ts={},
            hist_resolved_at={'zz-fixture-shipped': '2026-06-02T00:00:00+00:00'},
            history_ids={'zz-fixture-shipped'})
        self.assertTrue(clear)
        self.assertIn('progressed past', reason)

    def test_live_clarify_kept(self):
        clear, reason = self.mod.classify_clarify(
            _row('c3', 'clarify_request', 'zz-fixture-live', self.TS),
            cresp_ts={}, hist_resolved_at={}, history_ids=set())
        self.assertFalse(clear)
        self.assertIn('live', reason)

    def test_history_resolved_before_clarify_is_uncertain(self):
        # Task in history but resolved BEFORE the clarify ts: the clarify is a
        # fresh question posted after that resolution -> kept (uncertain).
        clear, reason = self.mod.classify_clarify(
            _row('c4', 'clarify_request', 'zz-fixture-fresh', self.TS),
            cresp_ts={},
            hist_resolved_at={'zz-fixture-fresh': '2026-05-30T00:00:00+00:00'},
            history_ids={'zz-fixture-fresh'})
        self.assertFalse(clear)
        self.assertIn('uncertain', reason)


# -------------------- load_beacon_approvals --------------------

class TestLoadBeaconApprovals(_Base):
    def test_missing_file_is_empty(self):
        pending, history, hist = self.mod.load_beacon_approvals(
            self.root / 'state' / 'nope.json')
        self.assertEqual((pending, history, hist), (set(), set(), {}))

    def test_malformed_file_is_empty(self):
        p = self.root / 'state' / 'bad.json'
        p.write_text('{not json')
        pending, history, hist = self.mod.load_beacon_approvals(p)
        self.assertEqual((pending, history, hist), (set(), set(), {}))

    def test_parses_pending_history_and_latest_resolved(self):
        p = self.root / 'state' / 'ba.json'
        p.write_text(json.dumps({
            'pending': [{'id': 'zz-fixture-p'}],
            'history': [
                {'task_id': 'zz-fixture-h', 'resolved_at': '2026-06-01T00:00:00+00:00'},
                {'task_id': 'zz-fixture-h', 'resolved_at': '2026-06-02T00:00:00+00:00'},
            ],
        }))
        pending, history, hist = self.mod.load_beacon_approvals(p)
        self.assertEqual(pending, {'zz-fixture-p'})
        self.assertEqual(history, {'zz-fixture-h'})
        self.assertEqual(hist['zz-fixture-h'], '2026-06-02T00:00:00+00:00')


# -------------------- run_once integration --------------------

class TestRunOnce(_Base):
    def _store(self):
        return [
            _row('a-pending', 'approval_request', 'zz-fixture-pending', '2026-06-01T00:00:00+00:00'),
            _row('a-done', 'approval_request', 'zz-fixture-done', '2026-06-01T00:00:00+00:00'),
            _row('c-live', 'clarify_request', 'zz-fixture-live', '2026-06-01T00:00:00+00:00'),
            _row('c-ship', 'clarify_request', 'zz-fixture-shipped', '2026-06-01T00:00:00+00:00'),
            # A clarify_response signal row (not a decision row).
            _row('resp-1', 'clarify_response', 'zz-fixture-live2', '2026-06-02T00:00:00+00:00'),
        ]

    def _beacon_state(self):
        return (
            {'zz-fixture-pending'},                                  # pending
            {'zz-fixture-done', 'zz-fixture-shipped'},               # history
            {'zz-fixture-shipped': '2026-06-02T00:00:00+00:00'},     # resolved_at
        )

    def test_clears_resolved_keeps_pending_and_live(self):
        store = self._store()
        client = _FakeClient(store)
        counts = self.mod.run_once(
            client, beacon_state=self._beacon_state(), now=NOW,
            backup_dir=self.backup_dir)

        by_id = {r['event_id']: r for r in store}
        # Resolved approval + progressed-past clarify cleared.
        self.assertIsNotNone(by_id['a-done']['read_at'])
        self.assertIsNotNone(by_id['c-ship']['read_at'])
        # Still-pending approval + live clarify untouched.
        self.assertIsNone(by_id['a-pending']['read_at'])
        self.assertIsNone(by_id['c-live']['read_at'])

        self.assertEqual(counts['clear_approval'], 1)
        self.assertEqual(counts['clear_clarify'], 1)
        self.assertEqual(counts['cleared'], 2)
        # a-pending (approval still pending) + c-live (clarify, no signal).
        self.assertEqual(counts['kept_live'], 2)
        self.assertEqual(counts['kept_uncertain'], 0)

        backups = list(self.backup_dir.glob('heal-stale-approvals-*.json'))
        self.assertEqual(len(backups), 1)
        backed = {r['event_id'] for r in json.loads(backups[0].read_text())}
        self.assertEqual(backed, {'a-done', 'c-ship'})

    def test_dry_run_writes_nothing(self):
        store = self._store()
        client = _FakeClient(store)
        counts = self.mod.run_once(
            client, beacon_state=self._beacon_state(), now=NOW,
            dry_run=True, backup_dir=self.backup_dir)
        self.assertTrue(all(r['read_at'] is None for r in store))
        self.assertEqual(client.update_log, [])
        self.assertEqual(list(self.backup_dir.glob('*.json')), [])
        self.assertEqual(counts['clear_approval'], 1)  # still classified
        self.assertEqual(counts['cleared'], 0)         # but not applied

    def test_idempotent_second_run_clears_nothing(self):
        store = self._store()
        client = _FakeClient(store)
        self.mod.run_once(client, beacon_state=self._beacon_state(), now=NOW,
                          backup_dir=self.backup_dir)
        counts2 = self.mod.run_once(client, beacon_state=self._beacon_state(),
                                    now=NOW, backup_dir=self.backup_dir)
        # Already-cleared rows have read_at set, so they aren't refetched.
        self.assertEqual(counts2['pending_approval'], 1)   # only a-pending
        self.assertEqual(counts2['clear_approval'], 0)
        self.assertEqual(counts2['clear_clarify'], 0)
        self.assertEqual(counts2['cleared'], 0)


if __name__ == '__main__':
    unittest.main()
