#!/usr/bin/env python3
"""Tests for larry_alerts.py — shared alert queue + cooldown gating.

Phase D3.5-prep (2026-05-12). Cooldown semantics, per-line ack helpers,
malformed-line handling.

Run:
    python3 -m unittest scripts.tests.test_larry_alerts
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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
            source='watchdog', severity='bogus', message='x',
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


class InfoSeverityTest(_IsolatedQueueTest):
    """Phase 1b (alert-pipeline-rework): the `info` severity below `warning`."""

    def test_info_is_valid_severity(self):
        self.assertIn('info', larry_alerts.VALID_SEVERITIES)

    def test_info_appends_and_defaults_to_digest(self):
        ok = larry_alerts.append_alert(
            source='watchdog', severity='info',
            subject='ourliberty-mirror-bot', message='auto-restarted',
        )
        self.assertTrue(ok)
        record = json.loads(larry_alerts.ALERTS_FILE.read_text().strip())
        self.assertEqual(record['severity'], 'info')
        # No DM: an info alert with no explicit route lands on the digest lane.
        self.assertEqual(record['route'], 'digest')

    def test_info_respects_explicit_route(self):
        # An info emitter that explicitly wants a DM can still pass escalate.
        ok = larry_alerts.append_alert(
            source='watchdog', severity='info',
            subject='disk', message='loud info', route='escalate',
        )
        self.assertTrue(ok)
        record = json.loads(larry_alerts.ALERTS_FILE.read_text().strip())
        self.assertEqual(record['route'], 'escalate')

    def test_info_cooldown_window_is_longest(self):
        self.assertEqual(
            larry_alerts._cooldown_window('info'), larry_alerts.INFO_COOLDOWN_SEC)
        self.assertGreater(
            larry_alerts.INFO_COOLDOWN_SEC, larry_alerts.WARNING_COOLDOWN_SEC)
        self.assertGreater(
            larry_alerts.WARNING_COOLDOWN_SEC, larry_alerts.CRITICAL_COOLDOWN_SEC)

    def test_info_and_warning_independent_buckets(self):
        a = larry_alerts.append_alert(
            source='watchdog', severity='info',
            subject='disk', message='i', route='escalate',
        )
        b = larry_alerts.append_alert(
            source='watchdog', severity='warning',
            subject='disk', message='w',
        )
        self.assertTrue(a)
        self.assertTrue(b)

    def test_info_glyph_in_raw_body(self):
        text = larry_alerts._render_raw_alert_body({
            'source': 'watchdog', 'severity': 'info',
            'subject': 'disk', 'message': 'routine',
        })
        self.assertIn('ℹ', text)
        self.assertNotIn('⚠', text)
        self.assertNotIn('🚨', text)


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

    def test_corrupt_silence_file_fails_loud(self):
        # Audit fix: a corrupt/unreadable silence file must NOT suppress —
        # permanent silent suppression of a possibly-real alert is the
        # irreversible-bad direction. Fail loud so the alert surfaces and can
        # be re-silenced cleanly.
        path = larry_alerts._silence_path('a:b')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not json{')
        self.assertFalse(larry_alerts.is_silenced('a:b'))

    def test_non_dict_silence_file_fails_loud(self):
        # A well-formed JSON that isn't an object (e.g. a bare number) is also
        # malformed → don't suppress.
        path = larry_alerts._silence_path('a:b')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('12345')
        self.assertFalse(larry_alerts.is_silenced('a:b'))

    def test_ttl_zero_expires_immediately_not_permanent(self):
        # Audit fix: ttl_sec=0 must mean "expires immediately", NOT permanent.
        # The old `if ttl_sec` falsy-check turned 0 into a None (eternal) until.
        self.assertTrue(larry_alerts.silence('a:b', ttl_sec=0, now=1000))
        data = json.loads(larry_alerts._silence_path('a:b').read_text())
        self.assertEqual(data['until'], 1000)  # base+0, a real deadline (not None)
        self.assertFalse(larry_alerts.is_silenced('a:b', now=1000))
        self.assertFalse(larry_alerts.is_silenced('a:b', now=1001))

    def test_until_zero_deadline_is_expired_not_permanent(self):
        # A literal until=0 (epoch) is a long-past deadline → expired, not
        # permanent (the old `until in (None, 0)` treated 0 as eternal).
        path = larry_alerts._silence_path('a:b')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'key': 'a:b', 'until': 0}))
        self.assertFalse(larry_alerts.is_silenced('a:b', now=1000))

    def test_silence_write_is_atomic_no_tmp_left(self):
        larry_alerts.silence('a:b', reason='x')
        # No leftover .tmp.* sidecar in the silence dir.
        leftovers = [p.name for p in larry_alerts.SILENCE_ROOT.iterdir()
                     if '.tmp.' in p.name]
        self.assertEqual(leftovers, [])

    def test_long_key_silence_round_trip(self):
        # A capped (over-long) key must still write+read a silence without an
        # OSError from a NAME_MAX overflow on the atomic-write tmp file.
        key = 'forge:' + ('x' * 400)
        self.assertTrue(larry_alerts.silence(key))
        self.assertTrue(larry_alerts.is_silenced(key))


class ResolveAlertTest(unittest.TestCase):
    """resolve_alert: retract pending escalate line(s) for a key and keep the
    line-index consumer cursors (beacon, medic) consistent so the next real
    alert is never skipped. Paths injected per-call for full isolation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        self.alerts = self.td / 'larry-alerts.jsonl'
        self.beacon = self.td / 'beacon-alerts-offset.txt'
        self.medic = self.td / 'medic-alerts-offset.txt'
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _line(source, subject, route='escalate', **extra):
        rec = {'source': source, 'subject': subject, 'route': route,
               'message': 'm'}
        rec.update(extra)
        return json.dumps(rec) + '\n'

    def _write_lines(self, lines):
        self.alerts.write_text(''.join(lines))

    def _resolve(self, key):
        return larry_alerts.resolve_alert(
            key, consumer_offset_files=[self.beacon, self.medic],
            alerts_file=self.alerts)

    def test_removes_matching_escalate_line_and_returns_count(self):
        self._write_lines([
            self._line('s', 'a'),
            self._line('heal-systemd-install-drift', 'install-drift:x.timer'),
            self._line('s', 'b'),
        ])
        removed = self._resolve(
            'heal-systemd-install-drift:install-drift:x.timer')
        self.assertEqual(removed, 1)
        survivors = self.alerts.read_text().splitlines()
        self.assertEqual(len(survivors), 2)
        self.assertNotIn('x.timer', self.alerts.read_text())

    def test_no_match_is_noop(self):
        original = [self._line('s', 'a'), self._line('s', 'b')]
        self._write_lines(original)
        removed = self._resolve('nope:nothing')
        self.assertEqual(removed, 0)
        self.assertEqual(self.alerts.read_text(), ''.join(original))

    def test_missing_file_returns_zero(self):
        self.assertEqual(self._resolve('any:key'), 0)

    def test_closure_and_digest_lines_are_not_retracted(self):
        # Only escalate (or legacy no-route) lines retract; a self-healed
        # closure/digest line for the same key is harmless and must remain.
        self._write_lines([
            self._line('src', 'k', route='closure'),
            self._line('src', 'k', route='digest'),
        ])
        self.assertEqual(self._resolve('src:k'), 0)
        self.assertEqual(len(self.alerts.read_text().splitlines()), 2)

    def test_legacy_no_route_line_is_retracted(self):
        # A record written before the route field existed rendered as escalate.
        rec = {'source': 'src', 'subject': 'k', 'message': 'm'}  # no 'route'
        self._write_lines([json.dumps(rec) + '\n'])
        self.assertEqual(self._resolve('src:k'), 1)
        self.assertEqual(self.alerts.read_text(), '')

    def test_cursor_decrement_before_cursor_no_skip(self):
        # The load-bearing bookkeeping: remove a line BEFORE a consumer's cursor
        # → that cursor decrements so it keeps pointing at the same logical
        # next-unread line (never skips it). A removal AT/AFTER the cursor leaves
        # it untouched (the line was undelivered anyway).
        # Lines: L0, L1(target), L2, L3.  beacon=3 (next=L3), medic=1 (next=L1).
        self._write_lines([
            self._line('s', 'L0'),
            self._line('heal', 'install-drift:t'),  # L1 — removed
            self._line('s', 'L2'),
            self._line('s', 'L3'),
        ])
        self.beacon.write_text('3')
        self.medic.write_text('1')
        removed = self._resolve('heal:install-drift:t')
        self.assertEqual(removed, 1)

        survivors = self.alerts.read_text().splitlines()
        self.assertEqual(len(survivors), 3)  # L0, L2, L3

        # beacon was at 3 (about to deliver L3); a line before it was removed, so
        # it decrements to 2 — which is L3's NEW index. Without the decrement it
        # would read index 3 (past EOF) and silently skip L3.
        self.assertEqual(self.beacon.read_text(), '2')
        self.assertEqual(
            json.loads(survivors[int(self.beacon.read_text())])['subject'],
            'L3')

        # medic was at 1 (about to deliver the now-removed L1); the removal is
        # NOT strictly before the cursor, so it stays at 1 — which is now L2, the
        # correct next undelivered line. medic never skips L2.
        self.assertEqual(self.medic.read_text(), '1')
        self.assertEqual(
            json.loads(survivors[int(self.medic.read_text())])['subject'],
            'L2')

    def test_backup_written_before_rewrite(self):
        self._write_lines([
            self._line('s', 'keep'),
            self._line('heal', 'install-drift:t'),
        ])
        self._resolve('heal:install-drift:t')
        backup = self.alerts.parent / (self.alerts.name + '.resolve.bak')
        self.assertTrue(backup.exists())
        # Backup holds the full PRE-rewrite content (both lines).
        self.assertEqual(len(backup.read_text().splitlines()), 2)

    def test_cursor_decrement_happens_before_file_rewrite(self):
        # Crash-safety invariant: consumer cursors must be decremented BEFORE the
        # live file is rewritten, so a crash between the two leaves cursors
        # pointing into the still-intact file (re-deliver, never skip). Lock the
        # ordering in by spying on the write order.
        self._write_lines([
            self._line('s', 'L0'),
            self._line('heal', 'install-drift:t'),  # removed (index 1)
            self._line('s', 'L2'),
        ])
        self.beacon.write_text('3')  # both removals fall before this cursor
        self.medic.write_text('3')
        order: list[Path] = []
        real = larry_alerts.atomic_io.atomic_write_text

        def spy(path, text, **kw):
            order.append(Path(path))
            return real(path, text, **kw)

        with mock.patch.object(
            larry_alerts.atomic_io, 'atomic_write_text', side_effect=spy,
        ):
            self._resolve('heal:install-drift:t')
        # Both offset files must be written strictly before the live alerts file.
        self.assertIn(self.alerts, order)
        alerts_pos = order.index(self.alerts)
        self.assertLess(order.index(self.beacon), alerts_pos)
        self.assertLess(order.index(self.medic), alerts_pos)

    def test_notification_and_approval_records_never_match(self):
        self._write_lines([
            json.dumps({'source': 'src', 'kind': 'notification',
                        'subject': 'k', 'message': 'm'}) + '\n',
            json.dumps({'source': 'src', 'kind': 'approval_request',
                        'subject': 'k'}) + '\n',
        ])
        self.assertEqual(self._resolve('src:k'), 0)
        self.assertEqual(len(self.alerts.read_text().splitlines()), 2)


class SafeKeyTest(unittest.TestCase):
    def test_clean_key_unchanged(self):
        # A key with only filesystem-safe chars passes through verbatim (no
        # churn for existing cooldown/silence files).
        self.assertEqual(larry_alerts._safe_key('watchdog:my-service.unit'),
                         'watchdog:my-service.unit')

    def test_distinct_dirty_keys_do_not_collide(self):
        # `forge:a/b` and `forge:a b` both sanitize to `forge:a_b`; the hash
        # suffix keeps them distinct so one alert's silence can't suppress the
        # other.
        k1 = larry_alerts._safe_key('forge:a/b')
        k2 = larry_alerts._safe_key('forge:a b')
        self.assertNotEqual(k1, k2)
        self.assertTrue(k1.startswith('forge:a_b.'))
        self.assertTrue(k2.startswith('forge:a_b.'))

    def test_safe_key_is_deterministic(self):
        self.assertEqual(larry_alerts._safe_key('forge:a/b'),
                         larry_alerts._safe_key('forge:a/b'))

    def test_over_long_key_is_capped_and_disambiguated(self):
        # A near-NAME_MAX key must be capped so the base name + `.tmp.<pid>`
        # atomic-write suffix can't overflow the filesystem 255-byte limit.
        long_clean = 'forge:' + ('a' * 400)
        out = larry_alerts._safe_key(long_clean)
        self.assertLessEqual(len(out), larry_alerts._MAX_SAFE_KEY_LEN + 11)
        # Two distinct over-long keys with the same 200-char prefix stay distinct.
        other = 'forge:' + ('a' * 399) + 'b'
        self.assertNotEqual(larry_alerts._safe_key(long_clean),
                            larry_alerts._safe_key(other))


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
