#!/usr/bin/env python3
"""Regression tests for the chain_event_shipper journal regex (E4.4d).

Pins parse_journal_record's regex-fallback path against the REAL MESSAGE
format that ourliberty-inbox-watcher emits today:

    inbox_watcher: [<agent>] <verb> task=<id> [model=...] [success=...]
                   [duration=<N>s] [cost=$<N>] [other ignored kvs...]

The previous regex expected `session_start agent=X task_id=Y` and never
matched a real line — chain_events stayed empty, the Operations tab's
Chain Event Feed was blank, D4 Mirror task_type override had nothing to
compute against. These tests use Larry's verbatim samples so the format
shift is detectable at test time, not after a production drain shows
journal=0.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import chain_event_shipper as ces  # noqa: E402


_FORGE_START = (
    'inbox_watcher: [forge] start '
    'task=healer-alert-plain-language-translation-layer '
    'model=claude-opus-4-7 timeout=14400s resume=ac476bb5-ead...'
)
_BEACON_DONE = (
    'inbox_watcher: [beacon] done task=notify-X '
    'success=True duration=50.01s attempts=1 '
    'cost=$0.24309324999999998'
)


class TestJournalRegexForgeStart(unittest.TestCase):

    def test_parses_forge_start_sample(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': _FORGE_START,
        }
        ev = ces.parse_journal_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'session_start')
        self.assertEqual(ev.agent, 'forge')
        self.assertEqual(
            ev.task_id, 'healer-alert-plain-language-translation-layer'
        )
        self.assertIsNone(ev.cost_usd)
        self.assertIsNone(ev.payload.get('duration_sec'))
        self.assertEqual(ev.payload.get('model'), 'claude-opus-4-7')


class TestJournalRegexBeaconDone(unittest.TestCase):

    def test_parses_beacon_done_sample(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': _BEACON_DONE,
        }
        ev = ces.parse_journal_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'session_done')
        self.assertEqual(ev.agent, 'beacon')
        self.assertEqual(ev.task_id, 'notify-X')
        self.assertEqual(ev.payload.get('duration_sec'), 50.01)
        self.assertIsInstance(ev.payload.get('duration_sec'), float)
        self.assertEqual(ev.cost_usd, 0.24309324999999998)
        self.assertIsInstance(ev.cost_usd, float)
        self.assertEqual(ev.payload.get('success'), 'True')


class TestJournalRegexDefensive(unittest.TestCase):

    def test_done_without_cost_yields_none_cost(self):
        # A session that exits before cost was recorded should still parse.
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': (
                'inbox_watcher: [forge] done task=abc-001 '
                'success=False duration=12.3s attempts=1'
            ),
        }
        ev = ces.parse_journal_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'session_done')
        self.assertIsNone(ev.cost_usd)
        self.assertEqual(ev.payload.get('duration_sec'), 12.3)

    def test_degraded_line_missing_task_returns_none(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': 'inbox_watcher: [forge] start',
        }
        self.assertIsNone(ces.parse_journal_record(rec))

    def test_non_inbox_watcher_line_returns_none(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': (
                'systemd[1]: Started ourliberty-inbox-watcher.service.'
            ),
        }
        self.assertIsNone(ces.parse_journal_record(rec))

    def test_other_inbox_watcher_log_shape_returns_none(self):
        # inbox_watcher emits many non-session lines (worktree setup, etc.).
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': (
                'inbox_watcher: [forge] worktree: created worktree '
                '/home/larry/agent-worktrees/wt-forge-foo for forge/foo'
            ),
        }
        self.assertIsNone(ces.parse_journal_record(rec))


if __name__ == '__main__':
    unittest.main()
