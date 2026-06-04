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
            mock.patch.object(larry_alerts, 'SILENCE_ROOT',
                              self._tmp_path / 'state' / 'alert-silenced'),
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


class AppendNotificationTest(_IsolatedQueueTest):
    """D3.5 5a-followup: chain-completion DM notifications."""

    def test_append_notification_writes_record(self):
        ok = larry_alerts.append_notification(
            source='outbox-notifier',
            intent='review-pass',
            message='Mirror approved PR #42 on task `t-abc`.',
            chat_id=7998341473,
            task_id='t-abc',
        )
        self.assertTrue(ok)
        pending = larry_alerts.read_pending(0)
        self.assertEqual(len(pending), 1)
        _, record = pending[0]
        self.assertEqual(record['kind'], 'notification')
        self.assertEqual(record['intent'], 'review-pass')
        self.assertEqual(record['chat_id'], 7998341473)
        self.assertEqual(record['task_id'], 't-abc')
        self.assertEqual(
            record['message'],
            'Mirror approved PR #42 on task `t-abc`.',
        )

    def test_append_notification_no_cooldown_gating(self):
        # Notifications are 1:1 with task completions; same intent firing
        # multiple times in quick succession (different task_ids) must NOT
        # be suppressed.
        for tid in ('t-a', 't-b', 't-c'):
            ok = larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-pass',
                message=f'Approved {tid}',
                chat_id=7998341473,
                task_id=tid,
            )
            self.assertTrue(ok, f'{tid} should not be suppressed')
        pending = larry_alerts.read_pending(0)
        self.assertEqual(len(pending), 3)
        for _, record in pending:
            self.assertEqual(record['kind'], 'notification')

    def test_append_notification_without_task_id(self):
        # task_id is optional.
        ok = larry_alerts.append_notification(
            source='outbox-notifier',
            intent='review-pass',
            message='Approved.',
            chat_id=7998341473,
        )
        self.assertTrue(ok)
        _, record = larry_alerts.read_pending(0)[0]
        self.assertNotIn('task_id', record)


class FormatDmNotificationTest(unittest.TestCase):
    """D3.5 5a-followup: format_dm renders notifications with intent emoji,
    not source/severity prefix."""

    def test_review_pass_uses_checkmark(self):
        text = larry_alerts.format_dm({
            'kind': 'notification',
            'intent': 'review-pass',
            'message': 'Mirror approved PR #42.',
            'chat_id': 1,
        })
        self.assertTrue(text.startswith('✓'))
        self.assertIn('Mirror approved', text)
        # No source-prefix, no severity emoji
        self.assertNotIn('outbox-notifier', text)
        self.assertNotIn('🚨', text)
        self.assertNotIn('⚠', text)

    def test_review_revision_uses_warning(self):
        text = larry_alerts.format_dm({
            'kind': 'notification', 'intent': 'review-revision',
            'message': 'Revisions requested.', 'chat_id': 1,
        })
        self.assertTrue(text.startswith('⚠'))

    def test_review_escalate_uses_warning(self):
        text = larry_alerts.format_dm({
            'kind': 'notification', 'intent': 'review-escalate',
            'message': 'Escalated.', 'chat_id': 1,
        })
        self.assertTrue(text.startswith('⚠'))

    def test_review_emergency_halt_uses_stop(self):
        text = larry_alerts.format_dm({
            'kind': 'notification', 'intent': 'review-emergency-halt',
            'message': 'EMERGENCY.', 'chat_id': 1,
        })
        self.assertTrue(text.startswith('🛑'))

    def test_reject_uses_cross(self):
        text = larry_alerts.format_dm({
            'kind': 'notification', 'intent': 'reject',
            'message': 'Forge rejected.', 'chat_id': 1,
        })
        self.assertTrue(text.startswith('✗'))

    def test_clarification_exhausted_uses_cross(self):
        text = larry_alerts.format_dm({
            'kind': 'notification', 'intent': 'clarification-exhausted',
            'message': 'Budget exhausted.', 'chat_id': 1,
        })
        self.assertTrue(text.startswith('✗'))

    def test_unknown_intent_falls_back_to_mailbox_emoji(self):
        text = larry_alerts.format_dm({
            'kind': 'notification', 'intent': 'future-intent',
            'message': 'Something.', 'chat_id': 1,
        })
        self.assertTrue(text.startswith('📬'))

    def test_alert_format_unchanged_by_5a_followup(self):
        # Regression check: alerts (no `kind` field, OR kind != notification)
        # still render with the legacy source-prefix + severity emoji shape.
        text = larry_alerts.format_dm({
            'source': 'watchdog', 'severity': 'critical',
            'subject': 'bots:mirror',
            'message': 'Mirror is DOWN.',
            'suggested_action': 'sudo systemctl restart x',
        })
        self.assertIn('🚨', text)
        self.assertIn('watchdog', text)
        self.assertIn('bots:mirror', text)
        self.assertIn('Mirror is DOWN', text)
        self.assertIn('Run:', text)


class CliSubcommandTest(_IsolatedQueueTest):
    """The shell-callable CLI exposes append_notification and
    append_approval_request (so the Medic operator can escalate via a stable
    allowlist-matchable form), while append_alert remains the ONLY subcommand
    that writes an alert-kind record."""

    def _last_record(self) -> dict:
        lines = larry_alerts.ALERTS_FILE.read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_append_notification_subcommand_writes_record(self):
        rc = larry_alerts.main([
            'append_notification', '--source', 'medic',
            '--intent', 'medic-diagnosis', '--message', 'hello',
            '--chat-id', '4242'])
        self.assertEqual(rc, 0)
        rec = self._last_record()
        self.assertEqual(rec['kind'], 'notification')
        self.assertEqual(rec['source'], 'medic')
        self.assertEqual(rec['intent'], 'medic-diagnosis')
        self.assertEqual(rec['message'], 'hello')
        self.assertEqual(rec['chat_id'], 4242)
        self.assertNotIn('task_id', rec)

    def test_append_notification_subcommand_with_task_id(self):
        rc = larry_alerts.main([
            'append_notification', '--source', 'medic',
            '--intent', 'medic-action-taken', '--message', 'acted',
            '--chat-id', '7', '--task-id', 't-99'])
        self.assertEqual(rc, 0)
        self.assertEqual(self._last_record()['task_id'], 't-99')

    def test_append_approval_request_subcommand_writes_record(self):
        rc = larry_alerts.main([
            'append_approval_request', '--source', 'medic',
            '--approval-id', 'medic-fp-1', '--body', 'do X',
            '--chat-id', '7'])
        self.assertEqual(rc, 0)
        rec = self._last_record()
        self.assertEqual(rec['kind'], 'approval_request')
        self.assertEqual(rec['approval_id'], 'medic-fp-1')
        self.assertEqual(rec['body'], 'do X')
        self.assertEqual(rec['source'], 'medic')
        self.assertEqual(rec['chat_id'], 7)

    def test_approval_request_source_defaults_when_omitted(self):
        rc = larry_alerts.main([
            'append_approval_request', '--approval-id', 'a-1',
            '--body', 'b', '--chat-id', '1'])
        self.assertEqual(rc, 0)
        self.assertEqual(self._last_record()['source'], 'outbox-notifier')

    def test_append_alert_remains_only_alert_path(self):
        # append_alert writes an alert-kind record (no `kind` field -> treated
        # as an alert by the dispatcher's owned-class matcher).
        rc = larry_alerts.main([
            'append_alert', '--source', 'watchdog', '--severity', 'critical',
            '--message', 'boom', '--subject', 'x'])
        self.assertEqual(rc, 0)
        self.assertNotIn('kind', self._last_record())
        # Neither new subcommand ever writes an alert-kind record.
        larry_alerts.main([
            'append_notification', '--source', 'medic', '--intent', 'i',
            '--message', 'm', '--chat-id', '1'])
        self.assertEqual(self._last_record()['kind'], 'notification')
        larry_alerts.main([
            'append_approval_request', '--approval-id', 'a', '--body', 'b',
            '--chat-id', '1'])
        self.assertEqual(self._last_record()['kind'], 'approval_request')

    def test_chat_id_must_be_int(self):
        with self.assertRaises(SystemExit):
            larry_alerts.main([
                'append_notification', '--source', 'medic', '--intent', 'i',
                '--message', 'm', '--chat-id', 'not-an-int'])

    def test_unknown_subcommand_rejected(self):
        with self.assertRaises(SystemExit):
            larry_alerts.main(['frobnicate'])


class SilenceLayerTest(_IsolatedQueueTest):
    """Durable silence layer: Medic-written suppression that append_alert
    honors (the 2026-06-04 forge-no-pr false-positive backstop)."""

    FP = ('heal-pipeline-stall:pipeline-stall:forge-no-pr:'
          'forge-queue-api-preflight-20260603T231401Z-clarify1')

    def _src_subj(self):
        src, subj = self.FP.split(':', 1)
        return src, subj

    def test_silence_suppresses_append_alert(self):
        src, subj = self._src_subj()
        # Without a silence, the alert appends.
        self.assertTrue(larry_alerts.append_alert(src, 'warning', 'stall',
                                                  subject=subj))
        # Silence the exact fingerprint, then the next alert is dropped.
        self.assertTrue(larry_alerts.silence(self.FP, reason='shipped PR #294'))
        self.assertTrue(larry_alerts.is_silenced(self.FP))
        self.assertFalse(larry_alerts.append_alert(src, 'warning', 'stall',
                                                   subject=subj))

    def test_silence_is_independent_of_cooldown(self):
        # A silence outlives the cooldown window: even with cooldown cleared,
        # a silenced alert stays suppressed.
        src, subj = self._src_subj()
        larry_alerts.silence(self.FP)
        key = f'{src}:{subj}'
        # Force-clear any cooldown file so only the silence can suppress.
        cd = larry_alerts._cooldown_path('warning', key)
        if cd.exists():
            cd.unlink()
        self.assertFalse(larry_alerts.append_alert(src, 'warning', 'm',
                                                   subject=subj))

    def test_ttl_expiry(self):
        larry_alerts.silence('a:b', ttl_sec=100, now=1000)
        self.assertTrue(larry_alerts.is_silenced('a:b', now=1050))
        self.assertFalse(larry_alerts.is_silenced('a:b', now=1200))

    def test_permanent_silence_has_null_until(self):
        larry_alerts.silence('a:b')
        data = json.loads(larry_alerts._silence_path('a:b').read_text())
        self.assertIsNone(data['until'])
        self.assertTrue(larry_alerts.is_silenced('a:b'))

    def test_unsilence_restores_delivery(self):
        src, subj = self._src_subj()
        larry_alerts.silence(self.FP)
        self.assertTrue(larry_alerts.unsilence(self.FP))
        self.assertFalse(larry_alerts.is_silenced(self.FP))
        self.assertTrue(larry_alerts.append_alert(src, 'warning', 'm',
                                                  subject=subj))

    def test_unsilence_missing_is_false(self):
        self.assertFalse(larry_alerts.unsilence('never:silenced'))

    def test_unreadable_silence_file_fails_quiet(self):
        # A silence file that exists but is unparseable still suppresses.
        path = larry_alerts._silence_path('a:b')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not json{')
        self.assertTrue(larry_alerts.is_silenced('a:b'))


if __name__ == '__main__':
    unittest.main()
