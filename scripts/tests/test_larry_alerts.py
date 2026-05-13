#!/usr/bin/env python3
"""Tests for larry_alerts.py — shared alert queue + cooldown gating.

Phase D3.5-prep (2026-05-12). Cooldown semantics, per-line ack helpers,
malformed-line handling.

Run:
    python3 -m unittest scripts.tests.test_larry_alerts
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import larry_alerts  # noqa: E402


class _IsolatedQueueTest(unittest.TestCase):
    """Base class that points the queue + cooldown paths at a tempdir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', self._tmp_path),
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              self._tmp_path / 'blackboard' / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              self._tmp_path / 'state' / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              self._tmp_path / 'state' / 'beacon-alerts-offset.txt'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class AppendAlertTest(_IsolatedQueueTest):
    def test_first_append_writes_record(self):
        result = larry_alerts.append_alert(
            source='watchdog', severity='critical',
            message='test message', subject='disk',
        )
        self.assertTrue(result)
        contents = larry_alerts.ALERTS_FILE.read_text().strip()
        record = json.loads(contents)
        self.assertEqual(record['source'], 'watchdog')
        self.assertEqual(record['severity'], 'critical')
        self.assertEqual(record['message'], 'test message')
        self.assertEqual(record['subject'], 'disk')
        self.assertIn('ts', record)

    def test_suggested_action_round_trip(self):
        larry_alerts.append_alert(
            source='watchdog', severity='critical',
            message='down', subject='x',
            suggested_action='sudo systemctl restart x',
        )
        record = json.loads(larry_alerts.ALERTS_FILE.read_text().strip())
        self.assertEqual(record['suggested_action'], 'sudo systemctl restart x')

    def test_invalid_severity_rejected(self):
        result = larry_alerts.append_alert(
            source='watchdog', severity='info', message='x',
        )
        self.assertFalse(result)
        self.assertFalse(larry_alerts.ALERTS_FILE.exists())


class CooldownTest(_IsolatedQueueTest):
    def test_critical_cooldown_suppresses_second_append(self):
        first = larry_alerts.append_alert(
            source='watchdog', severity='critical',
            subject='disk', message='first',
        )
        second = larry_alerts.append_alert(
            source='watchdog', severity='critical',
            subject='disk', message='second',
        )
        self.assertTrue(first)
        self.assertFalse(second)
        # File should only have one line.
        lines = larry_alerts.ALERTS_FILE.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('first', lines[0])

    def test_different_subjects_have_independent_cooldown(self):
        a = larry_alerts.append_alert(
            source='watchdog', severity='critical',
            subject='bots:mirror', message='mirror down',
        )
        b = larry_alerts.append_alert(
            source='watchdog', severity='critical',
            subject='bots:forge', message='forge down',
        )
        self.assertTrue(a)
        self.assertTrue(b)
        lines = larry_alerts.ALERTS_FILE.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_critical_and_warning_independent_buckets(self):
        a = larry_alerts.append_alert(
            source='watchdog', severity='critical',
            subject='disk', message='c',
        )
        b = larry_alerts.append_alert(
            source='watchdog', severity='warning',
            subject='disk', message='w',
        )
        self.assertTrue(a)
        self.assertTrue(b)

    def test_warning_cooldown_is_longer_than_critical(self):
        # Past critical window (10min) but inside warning window (60min):
        # critical alert allowed, second warning alert suppressed.
        with mock.patch.object(larry_alerts, 'CRITICAL_COOLDOWN_SEC', 1):
            with mock.patch.object(larry_alerts, 'WARNING_COOLDOWN_SEC', 3600):
                larry_alerts.append_alert(
                    source='watchdog', severity='warning',
                    subject='disk', message='w',
                )
                time.sleep(1.1)
                # Critical can fire (different bucket).
                self.assertTrue(larry_alerts.append_alert(
                    source='watchdog', severity='critical',
                    subject='disk', message='c',
                ))
                # Warning still in cooldown.
                self.assertFalse(larry_alerts.append_alert(
                    source='watchdog', severity='warning',
                    subject='disk', message='w2',
                ))

    def test_cooldown_expires_allows_new_append(self):
        with mock.patch.object(larry_alerts, 'CRITICAL_COOLDOWN_SEC', 0):
            larry_alerts.append_alert(
                source='watchdog', severity='critical',
                subject='disk', message='first',
            )
            self.assertTrue(larry_alerts.append_alert(
                source='watchdog', severity='critical',
                subject='disk', message='second',
            ))


class ReadPendingTest(_IsolatedQueueTest):
    def test_empty_queue_returns_empty(self):
        self.assertEqual(larry_alerts.read_pending(0), [])

    def test_reads_from_offset(self):
        for i in range(3):
            larry_alerts.append_alert(
                source='watchdog', severity='warning',
                subject=f's{i}', message=f'msg{i}',
            )
        out = larry_alerts.read_pending(1)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][0], 1)
        self.assertEqual(out[1][0], 2)
        self.assertEqual(out[0][1]['message'], 'msg1')

    def test_malformed_lines_marked_not_raised(self):
        # Inject a bad line directly.
        larry_alerts.append_alert(
            source='watchdog', severity='warning',
            subject='s', message='good',
        )
        with open(larry_alerts.ALERTS_FILE, 'a') as f:
            f.write('not-json-at-all\n')
            f.write('\n')  # blank line
        out = larry_alerts.read_pending(0)
        self.assertEqual(len(out), 3)
        self.assertFalse(out[0][1].get('_malformed'))
        self.assertTrue(out[1][1].get('_malformed'))
        self.assertTrue(out[2][1].get('_malformed'))


class OffsetTest(_IsolatedQueueTest):
    def test_default_offset_zero(self):
        self.assertEqual(larry_alerts.read_offset(), 0)

    def test_write_then_read_offset(self):
        larry_alerts.write_offset(42)
        self.assertEqual(larry_alerts.read_offset(), 42)

    def test_atomic_write_uses_tmp_rename(self):
        larry_alerts.write_offset(7)
        # Tmp file should not linger.
        self.assertFalse(
            (larry_alerts.OFFSET_FILE.with_suffix('.tmp')).exists()
        )
        self.assertEqual(larry_alerts.read_offset(), 7)

    def test_corrupted_offset_falls_back_to_zero(self):
        larry_alerts.OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        larry_alerts.OFFSET_FILE.write_text('not-a-number')
        self.assertEqual(larry_alerts.read_offset(), 0)


class FormatDmTest(unittest.TestCase):
    def test_critical_uses_alarm_emoji(self):
        text = larry_alerts.format_dm({
            'source': 'watchdog', 'severity': 'critical',
            'subject': 'disk', 'message': 'CRITICAL 95%',
        })
        self.assertIn('🚨', text)
        self.assertIn('watchdog', text)
        self.assertIn('[disk]', text)
        self.assertIn('CRITICAL 95%', text)

    def test_warning_uses_warn_emoji(self):
        text = larry_alerts.format_dm({
            'source': 'sentinel', 'severity': 'warning',
            'subject': 'inbox-stall:foo', 'message': 'stuck',
        })
        self.assertIn('⚠', text)
        self.assertIn('sentinel', text)

    def test_suggested_action_in_body(self):
        text = larry_alerts.format_dm({
            'source': 'watchdog', 'severity': 'critical',
            'subject': 'bots:mirror', 'message': 'down',
            'suggested_action': 'sudo systemctl restart ourliberty-mirror-bot',
        })
        self.assertIn('Run:', text)
        self.assertIn('ourliberty-mirror-bot', text)

    def test_malformed_line_formatted(self):
        text = larry_alerts.format_dm({'_malformed': True, 'raw': 'garbage'})
        self.assertIn('Bad alert', text)
        self.assertIn('garbage', text)


if __name__ == '__main__':
    unittest.main()
