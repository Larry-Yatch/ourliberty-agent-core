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

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

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


class _FakePopen:
    """Stand-in for subprocess.Popen used by iter_journalctl tests."""

    def __init__(self, stdout_lines, stderr_text='', returncode=0):
        self.stdout = iter(stdout_lines)
        self.stderr = mock.MagicMock()
        self.stderr.read.return_value = stderr_text
        self.returncode = returncode

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


class TestJournalctlCmdConstruction(unittest.TestCase):
    """Pins the journalctl invocation against systemd version drift.

    Droplet runs systemd 255 where `--no-follow` was rejected as an
    unrecognized option — the shipper drained zero records for hours.
    journalctl defaults to non-follow (drain-and-exit), so the flag is
    redundant; this test asserts we never add it back.
    """

    def test_cmd_does_not_pass_no_follow(self):
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured['cmd'] = cmd
            return _FakePopen(stdout_lines=[], returncode=0)

        with mock.patch.object(ces.subprocess, 'Popen', side_effect=fake_popen):
            list(ces.iter_journalctl(once=True, timeout_sec=0.1))

        self.assertIn('cmd', captured)
        self.assertNotIn('--no-follow', captured['cmd'])
        self.assertEqual(captured['cmd'][0], 'journalctl')


class TestJournalctlStderrOnNonZeroExit(unittest.TestCase):
    """Surfaces journalctl stderr so flag/argument breakage is loud.

    The original failure was silent: stderr was discarded and the daemon
    logged journal=0 indefinitely. This test ensures non-zero exits
    surface the stderr text as a warning.
    """

    def test_warning_logged_with_stderr_text(self):
        stderr_text = "journalctl: unrecognized option '--no-follow'"

        def fake_popen(cmd, **kwargs):
            return _FakePopen(
                stdout_lines=[], stderr_text=stderr_text, returncode=1,
            )

        logger = logging.getLogger('chain_event_shipper')
        with mock.patch.object(ces.subprocess, 'Popen', side_effect=fake_popen):
            with self.assertLogs(logger, level='WARNING') as cm:
                list(ces.iter_journalctl(once=True, timeout_sec=0.1))

        joined = '\n'.join(cm.output)
        self.assertIn('journalctl exited non-zero', joined)
        self.assertIn('unrecognized option', joined)
        self.assertIn('returncode=1', joined)

    def test_no_warning_on_clean_exit(self):
        def fake_popen(cmd, **kwargs):
            return _FakePopen(stdout_lines=[], returncode=0)

        logger = logging.getLogger('chain_event_shipper')
        with mock.patch.object(ces.subprocess, 'Popen', side_effect=fake_popen):
            # assertNoLogs would be cleaner but is 3.10+; use assertLogs
            # with a sentinel to verify the warning path stayed silent.
            with self.assertLogs(logger, level='DEBUG') as cm:
                logger.debug('sentinel')  # ensure assertLogs has something
                list(ces.iter_journalctl(once=True, timeout_sec=0.1))

        warnings = [
            m for m in cm.output
            if m.startswith('WARNING') and 'journalctl exited' in m
        ]
        self.assertEqual(warnings, [])


if __name__ == '__main__':
    unittest.main()
