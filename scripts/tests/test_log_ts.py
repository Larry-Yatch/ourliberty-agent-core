#!/usr/bin/env python3
"""Tests for log_ts.parse_log_ts — the shared log-timestamp parser.

The single source of truth for the recurring ~6h-skew bug class: a naive
outbox-notifier stamp must be read as HOST-LOCAL (the writer's zone), not
pinned to UTC. TZ is pinned per-test for deterministic, host-independent
expectations.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_log_ts
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import log_ts  # noqa: E402


class _TZPinned(unittest.TestCase):
    """Base that pins the process TZ so naive→system-local is deterministic."""

    TZ = 'America/Denver'  # droplet zone: MDT = -06:00 in June

    def setUp(self):
        self._tz_orig = os.environ.get('TZ')
        os.environ['TZ'] = self.TZ
        time.tzset()

    def tearDown(self):
        if self._tz_orig is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self._tz_orig
        time.tzset()


class NaiveHostLocalTest(_TZPinned):
    """A naive value is the outbox-notifier shape: read in the SYSTEM zone."""

    def test_space_separated_naive_reads_as_host_local(self):
        # 00:00:23 MDT (-06:00) == 06:00:23 UTC. The whole bug was reading this
        # as 00:00:23 UTC and shifting every event 6h into the past.
        dt = log_ts.parse_log_ts('2026-06-11 00:00:23')
        self.assertEqual(dt, datetime(2026, 6, 11, 6, 0, 23, tzinfo=timezone.utc))
        self.assertEqual(dt.isoformat(), '2026-06-11T06:00:23+00:00')

    def test_t_separated_naive_reads_as_host_local(self):
        dt = log_ts.parse_log_ts('2026-06-11T00:00:23')
        self.assertEqual(dt, datetime(2026, 6, 11, 6, 0, 23, tzinfo=timezone.utc))

    def test_naive_with_microseconds(self):
        dt = log_ts.parse_log_ts('2026-06-11 00:00:23.500000')
        self.assertEqual(
            dt, datetime(2026, 6, 11, 6, 0, 23, 500000, tzinfo=timezone.utc))

    def test_winter_offset_is_mst(self):
        # America/Denver shifts to MST (-07:00) in January, so the helper must
        # use the zone's offset AT THAT DATE, not a fixed -06:00.
        dt = log_ts.parse_log_ts('2026-01-15 00:00:00')
        self.assertEqual(dt, datetime(2026, 1, 15, 7, 0, 0, tzinfo=timezone.utc))


class NaiveUnderUTCTest(unittest.TestCase):
    """Under a UTC host, the same naive value carries no shift — proving the
    semantic is 'system-local', not a hardcoded Denver offset."""

    def setUp(self):
        self._tz_orig = os.environ.get('TZ')
        os.environ['TZ'] = 'UTC'
        time.tzset()

    def tearDown(self):
        if self._tz_orig is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self._tz_orig
        time.tzset()

    def test_naive_under_utc_host_is_unshifted(self):
        dt = log_ts.parse_log_ts('2026-06-11 00:00:23')
        self.assertEqual(dt, datetime(2026, 6, 11, 0, 0, 23, tzinfo=timezone.utc))


class AwareInputsTest(_TZPinned):
    """Aware values honor their own offset regardless of host TZ."""

    def test_trailing_z(self):
        dt = log_ts.parse_log_ts('2026-06-11T06:00:23Z')
        self.assertEqual(dt, datetime(2026, 6, 11, 6, 0, 23, tzinfo=timezone.utc))

    def test_explicit_utc_offset(self):
        dt = log_ts.parse_log_ts('2026-06-11T06:00:23+00:00')
        self.assertEqual(dt, datetime(2026, 6, 11, 6, 0, 23, tzinfo=timezone.utc))

    def test_numeric_offset_no_colon(self):
        # beacon-bot shape: [2026-06-11T00:00:23-0600]
        dt = log_ts.parse_log_ts('2026-06-11T00:00:23-0600')
        self.assertEqual(dt, datetime(2026, 6, 11, 6, 0, 23, tzinfo=timezone.utc))

    def test_colon_offset(self):
        dt = log_ts.parse_log_ts('2026-06-11T00:00:23-06:00')
        self.assertEqual(dt, datetime(2026, 6, 11, 6, 0, 23, tzinfo=timezone.utc))

    def test_microseconds_with_offset(self):
        # inbox-watcher shape: aware UTC isoformat with microseconds.
        dt = log_ts.parse_log_ts('2026-05-26T04:46:20.823929+00:00')
        self.assertEqual(
            dt, datetime(2026, 5, 26, 4, 46, 20, 823929, tzinfo=timezone.utc))

    def test_space_separated_aware(self):
        dt = log_ts.parse_log_ts('2026-06-11 00:00:23-0600')
        self.assertEqual(dt, datetime(2026, 6, 11, 6, 0, 23, tzinfo=timezone.utc))


class AlwaysAwareUTCTest(_TZPinned):
    """Every successful parse returns an aware datetime pinned to UTC."""

    def test_result_is_aware_utc(self):
        for raw in ('2026-06-11 00:00:23', '2026-06-11T00:00:23-0600',
                    '2026-06-11T06:00:23Z'):
            dt = log_ts.parse_log_ts(raw)
            self.assertIsNotNone(dt, raw)
            self.assertEqual(dt.utcoffset(), timezone.utc.utcoffset(None), raw)


class UnparseableReturnsNoneTest(_TZPinned):
    def test_none_input(self):
        self.assertIsNone(log_ts.parse_log_ts(None))  # type: ignore[arg-type]

    def test_non_string_input(self):
        self.assertIsNone(log_ts.parse_log_ts(12345))  # type: ignore[arg-type]

    def test_empty_string(self):
        self.assertIsNone(log_ts.parse_log_ts(''))

    def test_whitespace_only(self):
        self.assertIsNone(log_ts.parse_log_ts('   '))

    def test_garbage(self):
        self.assertIsNone(log_ts.parse_log_ts('not-a-timestamp'))

    def test_partial_garbage(self):
        self.assertIsNone(log_ts.parse_log_ts('2026-13-99 99:99:99'))


if __name__ == '__main__':
    unittest.main()
