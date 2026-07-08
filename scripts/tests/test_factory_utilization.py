"""Tests for scripts/factory_utilization.py.

Coverage (Operator Feed Loop slice 6b — utilization KPI producer):
- sanitize_tick: trip-wire row -> minimal ledger tick; junk rows dropped.
- merge_ticks: union by ts (incoming wins), chronological, TICKS_MAX bound,
  corrupt ledger rows (no/bad ts, wrong types) dropped instead of raising.
- window_stats: busy%/saturated%/means per trailing window; empty window ->
  None percentages, never a ZeroDivisionError.
- diagnose: the four constraint verdicts (capacity / supply / drained /
  balanced) + the two unknowns (cold ledger, unreadable shelf).
- run_once: merges trip-wire history into a durable ledger across passes,
  writes the state file atomically, and never raises on missing/malformed
  inputs (fail-safe contract).
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import factory_utilization as fu  # noqa: E402

NOW = datetime(2026, 7, 8, 18, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _trip_row(minutes_ago: int, active: float = 0, cap: float = 6,
              backlog: float = 0) -> dict:
    return {
        'ts': _iso(NOW - timedelta(minutes=minutes_ago)),
        'backlog_depth': backlog,
        'oldest_wait_sec': 0.0,
        'concurrency_active': active,
        'cap': cap,
        'rate_limit_count': 0,
    }


def _tick(minutes_ago: int, active: float = 0, cap: float = 6,
          backlog: float = 0) -> dict:
    return {'ts': _iso(NOW - timedelta(minutes=minutes_ago)),
            'active': active, 'cap': cap, 'backlog': backlog}


class SanitizeTickTest(unittest.TestCase):
    def test_trip_wire_row_maps_to_minimal_tick(self):
        t = fu.sanitize_tick(_trip_row(5, active=2, cap=6, backlog=1))
        self.assertEqual(t['active'], 2.0)
        self.assertEqual(t['cap'], 6.0)
        self.assertEqual(t['backlog'], 1.0)

    def test_junk_rows_dropped(self):
        self.assertIsNone(fu.sanitize_tick('nope'))
        self.assertIsNone(fu.sanitize_tick({'ts': 12345}))
        self.assertIsNone(fu.sanitize_tick({'ts': 'not-a-date'}))

    def test_wrong_typed_fields_default(self):
        t = fu.sanitize_tick({'ts': _iso(NOW), 'concurrency_active': 'many',
                              'cap': True, 'backlog_depth': None})
        self.assertEqual((t['active'], t['cap'], t['backlog']), (0.0, 0.0, 0.0))


class MergeTicksTest(unittest.TestCase):
    def test_union_dedupe_and_order(self):
        a, b = _tick(30), _tick(10)
        dup = dict(b, active=5)  # same ts as b — incoming wins
        merged = fu.merge_ticks([b, a], [dup])
        self.assertEqual([t['ts'] for t in merged], [a['ts'], b['ts']])
        self.assertEqual(merged[1]['active'], 5)

    def test_corrupt_ledger_rows_dropped_not_raised(self):
        merged = fu.merge_ticks(
            ['junk', {'no_ts': 1}, {'ts': 'bad'}, None, _tick(5)], [])
        self.assertEqual(len(merged), 1)

    def test_future_dated_ticks_dropped(self):
        future = _tick(-120)  # 2h from NOW — beyond FUTURE_SLACK_SEC
        merged = fu.merge_ticks([future, _tick(5)], [], now_ts=NOW.timestamp())
        self.assertEqual(len(merged), 1)
        # without now_ts (no horizon) the row is kept — merge stays pure
        self.assertEqual(len(fu.merge_ticks([future, _tick(5)], [])), 2)

    def test_bounded_at_ticks_max(self):
        many = [_tick(i) for i in range(fu.TICKS_MAX + 50)]
        merged = fu.merge_ticks(many, [])
        self.assertEqual(len(merged), fu.TICKS_MAX)
        # newest kept, oldest dropped
        self.assertEqual(merged[-1]['ts'], _tick(0)['ts'])


class WindowStatsTest(unittest.TestCase):
    def test_empty_window_is_none_not_division_error(self):
        s = fu.window_stats([_tick(60 * 48)], NOW.timestamp(), 24 * 3600)
        self.assertEqual(s['ticks'], 0)
        self.assertIsNone(s['busy_pct'])

    def test_busy_and_saturated_pcts(self):
        ticks = [
            _tick(10, active=0),                          # idle
            _tick(20, active=3),                          # busy
            _tick(30, active=6, cap=6, backlog=2),        # busy + saturated
            _tick(40, active=6, cap=6, backlog=0),        # at cap, no queue
        ]
        s = fu.window_stats(ticks, NOW.timestamp(), 24 * 3600)
        self.assertEqual(s['ticks'], 4)
        self.assertEqual(s['busy_pct'], 0.75)
        self.assertEqual(s['saturated_pct'], 0.25)
        self.assertEqual(s['mean_cap'], 6.0)


class DiagnoseTest(unittest.TestCase):
    def _day(self, ticks=20, busy=0.0, saturated=0.0):
        return {'ticks': ticks, 'busy_pct': busy, 'saturated_pct': saturated}

    def test_cold_ledger_is_unknown(self):
        c, reason = fu.diagnose(self._day(ticks=3), shelf=10)
        self.assertEqual(c, 'unknown')
        self.assertIn('not enough history', reason)

    def test_capacity_wins_over_supply(self):
        c, _ = fu.diagnose(self._day(busy=0.2, saturated=0.4), shelf=10)
        self.assertEqual(c, 'capacity')

    def test_idle_with_shelf_is_supply(self):
        c, reason = fu.diagnose(self._day(busy=0.1), shelf=19)
        self.assertEqual(c, 'supply')
        self.assertIn('19 ranked', reason)

    def test_idle_unreadable_shelf_is_unknown(self):
        c, _ = fu.diagnose(self._day(busy=0.1), shelf=None)
        self.assertEqual(c, 'unknown')

    def test_idle_empty_shelf_is_drained(self):
        c, _ = fu.diagnose(self._day(busy=0.1), shelf=0)
        self.assertEqual(c, 'drained')

    def test_busy_is_balanced(self):
        c, _ = fu.diagnose(self._day(busy=0.8), shelf=0)
        self.assertEqual(c, 'balanced')


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Restore, never pop: _bootstrap set this to the process-wide test
        # sandbox; a bare pop would strip the redirect for every later suite
        # in the same discover run (the #428 real-~/agents leak class).
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(fu)
        (self.tmp / 'state').mkdir(parents=True)

    def tearDown(self):
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(fu)

    def _write(self, name: str, obj) -> None:
        (self.tmp / 'state' / name).write_text(json.dumps(obj))

    def test_missing_everything_never_raises(self):
        state = fu.run_once(now=NOW)
        self.assertEqual(state['ticks'], [])
        self.assertEqual(state['computed']['constraint'], 'unknown')
        self.assertTrue((self.tmp / 'state' / 'factory-utilization.json').exists())

    def test_malformed_inputs_never_raise(self):
        (self.tmp / 'state' / 'readiness-trip-wire.json').write_text('{bad')
        (self.tmp / 'state' / 'mission-rank.json').write_text('[]')
        (self.tmp / 'state' / 'factory-utilization.json').write_text('"str"')
        state = fu.run_once(now=NOW)
        self.assertEqual(state['computed']['shelf_ranked'], None)

    def test_ledger_accumulates_across_passes(self):
        self._write('readiness-trip-wire.json',
                    {'history': [_trip_row(20, active=1)]})
        fu.run_once(now=NOW)
        # trip-wire history rolls: old row gone, new row present
        self._write('readiness-trip-wire.json',
                    {'history': [_trip_row(5, active=0)]})
        state = fu.run_once(now=NOW)
        self.assertEqual(len(state['ticks']), 2)  # durable union survived roll

    def test_supply_verdict_end_to_end(self):
        rows = [_trip_row(15 * i, active=0) for i in range(fu.MIN_TICKS + 2)]
        self._write('readiness-trip-wire.json', {'history': rows})
        self._write('mission-rank.json', {'ranked': [{'name': 'x'}] * 19})
        state = fu.run_once(now=NOW)
        self.assertEqual(state['computed']['constraint'], 'supply')
        self.assertEqual(state['computed']['shelf_ranked'], 19)
        self.assertEqual(state['computed']['windows']['24h']['busy_pct'], 0.0)


if __name__ == '__main__':
    unittest.main()
