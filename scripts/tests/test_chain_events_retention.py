"""Tests for scripts/chain_events_retention.py (retention node N2).

unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- load_retention_config: missing / malformed -> conservative defaults; valid
  file parsed; invalid days falls back; decision types stripped from the list.
- run_once end-to-end with a fake Supabase client: deletes only bookkeeping
  rows older than the window, keeps recent bookkeeping rows, NEVER deletes a
  decision row (even one inside the cutoff and even if config lists its type),
  writes an archive, is idempotent, and --dry-run writes nothing.

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


def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (NOW - timedelta(days=n)).isoformat()


# -------------------- fake Supabase client --------------------

class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Mimics the PostgREST fluent builder for the subset this job uses:
    select / eq / lt / range / delete / in_ / execute."""

    def __init__(self, store, delete_log):
        self._store = store
        self._delete_log = delete_log
        self._eq: dict = {}
        self._lt: dict = {}
        self._in = None
        self._range = None
        self._is_delete = False

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def lt(self, col, val):
        self._lt[col] = val
        return self

    def in_(self, col, vals):
        self._in = (col, list(vals))
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def delete(self):
        self._is_delete = True
        return self

    def execute(self):
        if self._is_delete:
            col, vals = self._in
            valset = set(vals)
            removed = [r for r in self._store if r.get(col) in valset]
            self._store[:] = [r for r in self._store if r.get(col) not in valset]
            self._delete_log.append({'ids': list(vals), 'n': len(removed)})
            return _Resp([])
        rows = []
        for row in self._store:
            if not all(row.get(k) == v for k, v in self._eq.items()):
                continue
            if not all(str(row.get(k)) < v for k, v in self._lt.items()):
                continue
            rows.append(row)
        if self._range is not None:
            a, b = self._range
            rows = rows[a:b + 1]
        return _Resp([dict(r) for r in rows])


class _FakeClient:
    def __init__(self, store):
        self._store = store
        self.delete_log: list = []

    def table(self, _name):
        return _FakeQuery(self._store, self.delete_log)


def _row(event_id, event_type, task_id, ts, read_at=None):
    return {
        'event_id': event_id,
        'event_type': event_type,
        'agent': 'forge',
        'task_id': task_id,
        'ts': ts,
        'read_at': read_at,
        'payload': {'note': 'fixture'},
    }


class _Base(unittest.TestCase):
    """Repoints AGENTS_ROOT at a temp dir + reloads the module so its
    module-level path constants pick up the env var."""

    def setUp(self):
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'agents'
        (self.root / 'logs').mkdir(parents=True)
        (self.root / 'blackboard' / 'retention-archive').mkdir(parents=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        import chain_events_retention as mod
        self.mod = importlib.reload(mod)
        self.archive_dir = self.root / 'blackboard' / 'retention-archive'

    def tearDown(self):
        self._tmp.cleanup()
        if self._prev_root is not None:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        else:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)


# -------------------- load_retention_config --------------------

class TestLoadConfig(_Base):
    def test_missing_file_defaults(self):
        days, types = self.mod.load_retention_config(self.root / 'nope.json')
        self.assertEqual(days, self.mod.DEFAULT_RETENTION_DAYS)
        self.assertEqual(types, list(self.mod.DEFAULT_BOOKKEEPING_TYPES))

    def test_malformed_file_defaults(self):
        p = self.root / 'bad.json'
        p.write_text('{not json')
        days, types = self.mod.load_retention_config(p)
        self.assertEqual(days, self.mod.DEFAULT_RETENTION_DAYS)
        self.assertEqual(types, list(self.mod.DEFAULT_BOOKKEEPING_TYPES))

    def test_valid_file_parsed(self):
        p = self.root / 'ok.json'
        p.write_text(json.dumps({
            'retention_days': 30,
            'bookkeeping_event_types': ['session_start', 'session_done', 'foo_marker'],
        }))
        days, types = self.mod.load_retention_config(p)
        self.assertEqual(days, 30)
        self.assertEqual(types, ['session_start', 'session_done', 'foo_marker'])

    def test_invalid_days_falls_back(self):
        p = self.root / 'baddays.json'
        p.write_text(json.dumps({'retention_days': -5,
                                 'bookkeeping_event_types': ['session_start']}))
        days, types = self.mod.load_retention_config(p)
        self.assertEqual(days, self.mod.DEFAULT_RETENTION_DAYS)
        self.assertEqual(types, ['session_start'])

    def test_decision_types_stripped_from_config(self):
        p = self.root / 'evil.json'
        p.write_text(json.dumps({
            'retention_days': 14,
            'bookkeeping_event_types': ['session_start', 'approval_request'],
        }))
        days, types = self.mod.load_retention_config(p)
        self.assertEqual(types, ['session_start'])
        self.assertNotIn('approval_request', types)

    def test_only_decision_types_falls_back_to_defaults(self):
        p = self.root / 'alldec.json'
        p.write_text(json.dumps({
            'retention_days': 14,
            'bookkeeping_event_types': ['approval_request', 'clarify_request'],
        }))
        _, types = self.mod.load_retention_config(p)
        self.assertEqual(types, list(self.mod.DEFAULT_BOOKKEEPING_TYPES))


# -------------------- run_once integration --------------------

class TestRunOnce(_Base):
    CONFIG = (14, ['session_start', 'session_done'])

    def _store(self):
        return [
            # OLD bookkeeping -> deletable
            _row('s1', 'session_start', 'zz-fixture-old1', _days_ago(20)),
            _row('s2', 'session_done', 'zz-fixture-old2', _days_ago(15)),
            # RECENT bookkeeping -> kept (inside window)
            _row('s3', 'session_start', 'zz-fixture-new', _days_ago(3)),
            # OLD decision rows -> NEVER deleted (not a bookkeeping type)
            _row('a1', 'approval_request', 'zz-fixture-dec', _days_ago(40)),
            _row('c1', 'clarify_request', 'zz-fixture-clar', _days_ago(40)),
            # OLD noise -> not in bookkeeping list -> kept by this job
            _row('n1', 'larry_alert', 'zz-fixture-alert', _days_ago(40)),
        ]

    def test_deletes_only_expired_bookkeeping(self):
        store = self._store()
        client = _FakeClient(store)
        counts = self.mod.run_once(client, config=self.CONFIG, now=NOW,
                                   archive_dir=self.archive_dir)
        ids = {r['event_id'] for r in store}
        # Expired bookkeeping gone.
        self.assertNotIn('s1', ids)
        self.assertNotIn('s2', ids)
        # Recent bookkeeping + all decisions + noise untouched.
        self.assertIn('s3', ids)
        self.assertIn('a1', ids)
        self.assertIn('c1', ids)
        self.assertIn('n1', ids)
        self.assertEqual(counts['candidates'], 2)
        self.assertEqual(counts['deleted'], 2)
        self.assertEqual(counts['protected_skipped'], 0)

        archives = list(self.archive_dir.glob('chain-events-retention-*.json'))
        self.assertEqual(len(archives), 1)
        archived_ids = {r['event_id'] for r in json.loads(archives[0].read_text())}
        self.assertEqual(archived_ids, {'s1', 's2'})
        # Archive preserves full row incl. payload (restorable).
        self.assertTrue(all('payload' in r
                            for r in json.loads(archives[0].read_text())))

    def test_decision_type_in_config_never_deletes_decision(self):
        # Even if a misconfigured config lists a decision type, the run_once
        # guard strips it and the pending decision row survives.
        store = self._store()
        client = _FakeClient(store)
        counts = self.mod.run_once(
            client,
            config=(14, ['session_start', 'session_done', 'approval_request']),
            now=NOW, archive_dir=self.archive_dir)
        ids = {r['event_id'] for r in store}
        self.assertIn('a1', ids)  # decision survives despite config
        self.assertEqual(counts['candidates'], 2)  # only the two bookkeeping rows

    def test_dry_run_writes_nothing(self):
        store = self._store()
        client = _FakeClient(store)
        counts = self.mod.run_once(client, config=self.CONFIG, now=NOW,
                                   dry_run=True, archive_dir=self.archive_dir)
        ids = {r['event_id'] for r in store}
        self.assertEqual(ids, {'s1', 's2', 's3', 'a1', 'c1', 'n1'})  # nothing removed
        self.assertEqual(client.delete_log, [])
        self.assertEqual(list(self.archive_dir.glob('*.json')), [])
        self.assertEqual(counts['candidates'], 2)  # still classified
        self.assertEqual(counts['deleted'], 0)     # but not applied

    def test_idempotent_second_run_deletes_nothing(self):
        store = self._store()
        client = _FakeClient(store)
        self.mod.run_once(client, config=self.CONFIG, now=NOW,
                          archive_dir=self.archive_dir)
        counts2 = self.mod.run_once(client, config=self.CONFIG, now=NOW,
                                    archive_dir=self.archive_dir)
        self.assertEqual(counts2['candidates'], 0)
        self.assertEqual(counts2['deleted'], 0)


if __name__ == '__main__':
    unittest.main()
