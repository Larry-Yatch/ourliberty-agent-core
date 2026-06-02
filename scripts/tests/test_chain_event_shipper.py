#!/usr/bin/env python3
"""Tests for chain_event_shipper (E4.4d PR-B).

Covers:
  - parse_journal_record (structured + regex-fallback paths)
  - parse_log_line (each event type, kv extraction, unknown rejection)
  - parse_pulse_escalations (array shape, ts fallback, sha256 dedup gate)
  - parse_jsonl_line (larry-alerts.jsonl + sentinel-alerts.jsonl)
  - tail_file (cursor resume across drain passes; inode-rotation detection)
  - EventBuffer (cap enforcement, overflow drops oldest, drain+clear cycle)
  - compute_event_id (deterministic across re-parses of the same line)
  - drain_once orchestration with a mocked Supabase sink
  - KNOWN_EVENT_TYPES discipline (unknown types dropped, never inserted)
  - sanitize_payload redacts credential-shaped keys

Subprocess invocations are NEVER live — journalctl is replaced by a
fixture iterator. Supabase client is replaced by an in-memory mock.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_chain_event_shipper
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import chain_event_shipper as ces  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='chain-shipper-test-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self._tmp, sub), exist_ok=True)
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp
        importlib.reload(ces)

    def tearDown(self):
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        shutil.rmtree(self._tmp, ignore_errors=True)
        importlib.reload(ces)
        super().tearDown()


# -------------------- pure helpers --------------------


class TestComputeEventId(unittest.TestCase):

    def test_deterministic(self):
        a = ces.compute_event_id('t1', 'session_start', '2026-05-25T19:08:48Z')
        b = ces.compute_event_id('t1', 'session_start', '2026-05-25T19:08:48Z')
        self.assertEqual(a, b)

    def test_changes_on_any_field(self):
        base = ces.compute_event_id('t1', 'session_start', '2026-05-25T19:08:48Z')
        self.assertNotEqual(
            base,
            ces.compute_event_id('t2', 'session_start', '2026-05-25T19:08:48Z'),
        )
        self.assertNotEqual(
            base,
            ces.compute_event_id('t1', 'session_done', '2026-05-25T19:08:48Z'),
        )
        self.assertNotEqual(
            base,
            ces.compute_event_id('t1', 'session_start', '2026-05-25T19:08:49Z'),
        )

    def test_nullable_task_id(self):
        self.assertIsInstance(
            ces.compute_event_id(None, 'healer_fire', '2026-05-25T19:08:48Z'),
            str,
        )


class TestSanitizePayload(unittest.TestCase):

    def test_redacts_credential_keys(self):
        payload = {
            'task_id': 'abc',
            'service_role_key': 'eyJhbGciOiJSUzI1NiI...',
            'api_key': 'sk-real-secret',
            'nested': {'access_token': 'tok-xyz', 'duration_sec': 12},
        }
        out = ces.sanitize_payload(payload)
        self.assertEqual(out['task_id'], 'abc')
        self.assertEqual(out['service_role_key'], '<redacted>')
        self.assertEqual(out['api_key'], '<redacted>')
        self.assertEqual(out['nested']['access_token'], '<redacted>')
        self.assertEqual(out['nested']['duration_sec'], 12)

    def test_leaves_scalars(self):
        self.assertEqual(ces.sanitize_payload(42), 42)
        self.assertEqual(ces.sanitize_payload('hello'), 'hello')
        self.assertEqual(ces.sanitize_payload(None), None)


class TestMakeEventRejectsUnknown(unittest.TestCase):

    def test_known_type_yields_event(self):
        ev = ces.make_event(
            agent='forge', event_type='session_start',
            ts='2026-05-25T19:08:48Z', task_id='abc',
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'session_start')

    def test_unknown_type_returns_none(self):
        ev = ces.make_event(
            agent='forge', event_type='hot_patched_unknown_event',
            ts='2026-05-25T19:08:48Z', task_id='abc',
        )
        self.assertIsNone(ev)


# -------------------- parsers --------------------


class TestParseJournalRecord(unittest.TestCase):

    def test_structured_fields(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',  # 2025-05-25 ish
            'MESSAGE': 'session_start agent=forge task_id=abc',
            'OURLIBERTY_EVENT_TYPE': 'session_start',
            'OURLIBERTY_AGENT': 'forge',
            'OURLIBERTY_TASK_ID': 'abc-001',
            'OURLIBERTY_MODEL': 'claude-opus-4-7',
            'OURLIBERTY_TASK_TYPE': 'feature-development',
        }
        ev = ces.parse_journal_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'session_start')
        self.assertEqual(ev.agent, 'forge')
        self.assertEqual(ev.task_id, 'abc-001')
        self.assertEqual(ev.payload.get('model'), 'claude-opus-4-7')
        self.assertEqual(ev.payload.get('task_type'), 'feature-development')

    def test_regex_fallback(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': (
                'inbox_watcher: [mirror] start task=pr-101-review '
                'model=claude-opus-4-7 timeout=14400s'
            ),
        }
        ev = ces.parse_journal_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'session_start')
        self.assertEqual(ev.agent, 'mirror')
        self.assertEqual(ev.task_id, 'pr-101-review')

    def test_session_done_with_cost(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': (
                'inbox_watcher: [forge] done task=abc '
                'success=True duration=120.5s attempts=1 cost=$0.42'
            ),
        }
        ev = ces.parse_journal_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'session_done')
        self.assertEqual(ev.cost_usd, 0.42)
        self.assertEqual(ev.payload.get('duration_sec'), 120.5)

    def test_unknown_structured_type_returns_none(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': 'mystery',
            'OURLIBERTY_EVENT_TYPE': 'invented_event_type',
            'OURLIBERTY_AGENT': 'forge',
        }
        self.assertIsNone(ces.parse_journal_record(rec))

    def test_non_chain_message_returns_none(self):
        rec = {
            '__REALTIME_TIMESTAMP': '1748189328000000',
            'MESSAGE': 'Started ourliberty-inbox-watcher.service.',
        }
        self.assertIsNone(ces.parse_journal_record(rec))


class TestParseLogLine(unittest.TestCase):

    def test_marker_emit(self):
        line = (
            '[2026-05-25T19:42:13+00:00] [INFO] MARKER_EMIT '
            'agent=forge task=abc-001 verdict=PROCEED\n'
        )
        ev = ces.parse_log_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'marker_emit')
        self.assertEqual(ev.agent, 'forge')
        self.assertEqual(ev.task_id, 'abc-001')

    def test_auto_merge_with_pr_url(self):
        line = (
            "[2026-05-25T19:42:13Z] [INFO] AUTO_MERGE task=abc-001 "
            "pr='https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42' "
            "outcome=merged\n"
        )
        ev = ces.parse_log_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'auto_merge')
        self.assertEqual(
            ev.pr_url,
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42',
        )

    def test_cost_budget_extracts_cost(self):
        line = (
            '[2026-05-25T19:42:13Z] [INFO] COST_BUDGET '
            'task=abc agent=forge cost_usd=1.23 limit_usd=5\n'
        )
        ev = ces.parse_log_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.cost_usd, 1.23)
        self.assertEqual(ev.payload.get('limit_usd'), '5')

    def test_non_event_line_returns_none(self):
        line = '[2026-05-25T19:42:13Z] [INFO] random log without keyword\n'
        self.assertIsNone(ces.parse_log_line(line))

    def test_malformed_line_returns_none(self):
        self.assertIsNone(ces.parse_log_line('not a log line at all'))


class TestParsePulseEscalations(unittest.TestCase):

    def test_array_shape(self):
        content = json.dumps([
            {
                'ts': '2026-05-25T19:00:00Z',
                'task_id': 'esc-1',
                'severity': 'red',
                'headline': 'inbox-watcher memleak root cause',
                'needs_response': True,
            },
            {
                'ts': '2026-05-25T19:05:00Z',
                'headline': 'log rotation overdue',
                'severity': 'yellow',
            },
        ])
        events = ces.parse_pulse_escalations(content)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, 'escalation')
        self.assertEqual(events[0].agent, 'pulse')
        self.assertEqual(events[0].payload.get('severity'), 'red')

    def test_dict_wrapper_shape(self):
        content = json.dumps({
            'escalations': [
                {'ts': '2026-05-25T19:00:00Z', 'headline': 'h1',
                 'severity': 'red', 'task_id': 'e1'},
            ],
        })
        events = ces.parse_pulse_escalations(content)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload.get('headline'), 'h1')

    def test_invalid_json_returns_empty(self):
        self.assertEqual(ces.parse_pulse_escalations('not json'), [])

    def test_empty_array_returns_empty(self):
        self.assertEqual(ces.parse_pulse_escalations('[]'), [])


class TestParseJsonlLine(unittest.TestCase):

    def test_larry_alerts(self):
        rec = {
            'ts': '2026-05-25T19:00:00Z',
            'source': 'watchdog',
            'severity': 'warning',
            'subject': 'memory-climbing',
            'message': 'memory_current=3.1G',
        }
        ev = ces.parse_jsonl_line(json.dumps(rec) + '\n',
                                  source='larry_alerts')
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'larry_alert')
        self.assertEqual(ev.agent, 'watchdog')
        self.assertEqual(ev.task_id, 'memory-climbing')

    def test_sentinel_alerts(self):
        rec = {
            'ts': '2026-05-25T19:00:00Z',
            'source': 'dispatch-sentinel',
            'subject': 'inbox-staleness',
            'message': 'task X stuck >30 min',
        }
        ev = ces.parse_jsonl_line(json.dumps(rec) + '\n',
                                  source='sentinel_alerts')
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'sentinel_alert')

    def test_malformed_line_returns_none(self):
        self.assertIsNone(
            ces.parse_jsonl_line('not json\n', source='larry_alerts'))


# -------------------- tail_file (cursor + rotation) --------------------


class TestTailFile(_IsolatedAgentsRoot):

    def test_resumes_from_cursor(self):
        path = Path(self._tmp) / 'logs' / 'sample.log'
        path.write_text('line1\nline2\n')
        cursor = ces.FileCursor()
        first_pass = list(ces.tail_file(path, cursor))
        self.assertEqual([line for line, _ in first_pass],
                         ['line1\n', 'line2\n'])
        last_cursor = first_pass[-1][1]
        # Append more, ensure second pass picks up only the new lines.
        with open(path, 'a') as fh:
            fh.write('line3\nline4\n')
        second_pass = list(ces.tail_file(path, last_cursor))
        self.assertEqual([line for line, _ in second_pass],
                         ['line3\n', 'line4\n'])

    def test_rotation_detected_via_size_shrink(self):
        # Realistic logrotate sequence: original file accumulates content,
        # logrotate moves it aside, daemon writes fresh smaller content
        # to a new file. The detector spots cursor.offset > new_size and
        # rewinds to 0.
        path = Path(self._tmp) / 'logs' / 'rot.log'
        path.write_text('old1\nold2\nold3\nold4\n')
        first_pass = list(ces.tail_file(path, ces.FileCursor()))
        last_cursor = first_pass[-1][1]
        self.assertEqual(last_cursor.offset, len('old1\nold2\nold3\nold4\n'))
        # Rotation: file replaced with smaller fresh content.
        path.unlink()
        path.write_text('new1\n')
        second_pass = list(ces.tail_file(path, last_cursor))
        self.assertEqual([line for line, _ in second_pass], ['new1\n'])

    def test_rotation_detected_via_fingerprint_change(self):
        # When the new file is the same size as the cursor offset (a
        # genuinely adversarial / rare case), the fingerprint check
        # catches it — provided the file is large enough to fingerprint.
        path = Path(self._tmp) / 'logs' / 'rot.log'
        original = ('A' * 80) + '\n'
        path.write_text(original)
        first_pass = list(ces.tail_file(path, ces.FileCursor()))
        last_cursor = first_pass[-1][1]
        self.assertNotEqual(last_cursor.fp_sha, '')
        path.unlink()
        replacement = ('B' * 80) + '\n'  # same size, totally different
        path.write_text(replacement)
        second_pass = list(ces.tail_file(path, last_cursor))
        self.assertEqual([line for line, _ in second_pass], [replacement])

    def test_missing_file_returns_empty(self):
        path = Path(self._tmp) / 'logs' / 'does-not-exist.log'
        self.assertEqual(list(ces.tail_file(path, ces.FileCursor())), [])

    def test_partial_trailing_line_preserved_for_next_pass(self):
        path = Path(self._tmp) / 'logs' / 'partial.log'
        path.write_text('full1\npartial-no-newline')
        cursor = ces.FileCursor()
        first = list(ces.tail_file(path, cursor))
        self.assertEqual([line for line, _ in first], ['full1\n'])
        # Now finish the partial line; second pass should pick it up.
        with open(path, 'a') as fh:
            fh.write('-completed\n')
        last_cursor = first[-1][1]
        second = list(ces.tail_file(path, last_cursor))
        self.assertEqual([line for line, _ in second],
                         ['partial-no-newline-completed\n'])


# -------------------- EventBuffer --------------------


class TestEventBuffer(_IsolatedAgentsRoot):

    def test_append_and_drain_roundtrip(self):
        buf = ces.EventBuffer(path=Path(self._tmp) / 'state' / 'buf.jsonl')
        rows = [{'event_id': f'evt-{i}', 'ts': '2026-05-25T19:00:00Z',
                 'agent': 'forge', 'event_type': 'session_start',
                 'payload': {}} for i in range(5)]
        dropped = buf.append(rows)
        self.assertEqual(dropped, 0)
        out = buf.drain()
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]['event_id'], 'evt-0')
        buf.clear()
        self.assertEqual(buf.drain(), [])

    def test_overflow_drops_oldest(self):
        buf = ces.EventBuffer(
            path=Path(self._tmp) / 'state' / 'buf.jsonl',
            max_lines=3, max_bytes=10**9,
        )
        for i in range(10):
            buf.append([{'event_id': f'evt-{i}'}])
        out = buf.drain()
        self.assertEqual(len(out), 3)
        # Oldest dropped — first remaining row should be evt-7 (last 3).
        ids = {row['event_id'] for row in out}
        self.assertIn('evt-9', ids)
        self.assertNotIn('evt-0', ids)


# -------------------- drain_once orchestration --------------------


class _MockSink:
    """In-memory replacement for SupabaseSink."""

    def __init__(self, raise_on_insert: bool = False) -> None:
        self.rows: list[dict] = []
        self.raise_on_insert = raise_on_insert

    def insert_rows(self, rows: list[dict]) -> None:
        if self.raise_on_insert:
            raise RuntimeError('simulated supabase outage')
        self.rows.extend(rows)


class TestDrainOnce(_IsolatedAgentsRoot):

    def _empty_journal(self):
        return iter(())

    def test_ingests_all_five_sources(self):
        outbox_log = Path(self._tmp) / 'logs' / 'outbox-notifier.log'
        outbox_log.write_text(
            '[2026-05-25T19:42:13Z] [INFO] MARKER_EMIT agent=forge '
            'task=abc verdict=PROCEED\n'
        )
        pulse_path = Path(self._tmp) / 'blackboard' / 'pulse-escalations.json'
        pulse_path.write_text(json.dumps([
            {'ts': '2026-05-25T19:00:00Z', 'task_id': 'esc-1',
             'severity': 'red', 'headline': 'h'},
        ]))
        larry_path = Path(self._tmp) / 'blackboard' / 'larry-alerts.jsonl'
        larry_path.write_text(json.dumps({
            'ts': '2026-05-25T19:00:00Z', 'source': 'watchdog',
            'severity': 'warning', 'subject': 'mem',
            'message': 'x',
        }) + '\n')
        sentinel_path = Path(self._tmp) / 'blackboard' / 'sentinel-alerts.jsonl'
        sentinel_path.write_text(json.dumps({
            'ts': '2026-05-25T19:00:00Z', 'source': 'dispatch-sentinel',
            'subject': 'stale-task', 'message': 'y',
        }) + '\n')
        journal_records = [
            {'__REALTIME_TIMESTAMP': '1748189328000000',
             'MESSAGE': 'session_start agent=mirror task_id=pr-101',
             'OURLIBERTY_EVENT_TYPE': 'session_start',
             'OURLIBERTY_AGENT': 'mirror',
             'OURLIBERTY_TASK_ID': 'pr-101'},
        ]
        sink = _MockSink()
        buffer = ces.EventBuffer(path=Path(self._tmp) / 'state' / 'buf.jsonl')
        cursors = {}
        pulse_cursor = ces.PulseCursor()
        stats, _new_pulse = ces.drain_once(
            sink, buffer, cursors, pulse_cursor,
            ces._setup_logging(),
            journal_iter_fn=lambda: iter(journal_records),
            pulse_path=pulse_path, outbox_log_path=outbox_log,
            larry_alerts_path=larry_path,
            sentinel_alerts_path=sentinel_path,
        )
        self.assertEqual(stats.journal, 1)
        self.assertEqual(stats.outbox_log, 1)
        self.assertEqual(stats.pulse_escalations, 1)
        self.assertEqual(stats.larry_alerts, 1)
        self.assertEqual(stats.sentinel_alerts, 1)
        self.assertEqual(stats.inserted, 5)
        event_types = {row['event_type'] for row in sink.rows}
        self.assertEqual(event_types, {
            'session_start', 'marker_emit', 'escalation',
            'larry_alert', 'sentinel_alert',
        })

    def test_pulse_snapshot_only_processed_when_sha_changes(self):
        pulse_path = Path(self._tmp) / 'blackboard' / 'pulse-escalations.json'
        pulse_path.write_text(json.dumps([
            {'ts': '2026-05-25T19:00:00Z', 'task_id': 'esc-1',
             'severity': 'red', 'headline': 'h'},
        ]))
        sink = _MockSink()
        buffer = ces.EventBuffer(path=Path(self._tmp) / 'state' / 'buf.jsonl')
        cursors = {}
        pulse_cursor = ces.PulseCursor()
        stats1, new_pulse = ces.drain_once(
            sink, buffer, cursors, pulse_cursor, ces._setup_logging(),
            journal_iter_fn=self._empty_journal,
            pulse_path=pulse_path,
            outbox_log_path=Path(self._tmp) / 'logs' / 'absent.log',
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'absent.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'absent2.jsonl',
        )
        self.assertEqual(stats1.pulse_escalations, 1)
        # Same content -> no new events on second drain.
        stats2, _ = ces.drain_once(
            sink, buffer, cursors, new_pulse, ces._setup_logging(),
            journal_iter_fn=self._empty_journal,
            pulse_path=pulse_path,
            outbox_log_path=Path(self._tmp) / 'logs' / 'absent.log',
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'absent.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'absent2.jsonl',
        )
        self.assertEqual(stats2.pulse_escalations, 0)

    def test_supabase_failure_buffers_rows(self):
        outbox_log = Path(self._tmp) / 'logs' / 'outbox-notifier.log'
        outbox_log.write_text(
            '[2026-05-25T19:42:13Z] [INFO] MARKER_EMIT agent=forge '
            'task=abc verdict=PROCEED\n'
        )
        sink = _MockSink(raise_on_insert=True)
        buffer = ces.EventBuffer(path=Path(self._tmp) / 'state' / 'buf.jsonl')
        cursors = {}
        stats, _ = ces.drain_once(
            sink, buffer, cursors, ces.PulseCursor(), ces._setup_logging(),
            journal_iter_fn=self._empty_journal,
            pulse_path=Path(self._tmp) / 'blackboard' / 'pulse-absent.json',
            outbox_log_path=outbox_log,
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'absent.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'absent2.jsonl',
        )
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.buffered, 1)
        self.assertEqual(len(sink.rows), 0)
        # Buffer should now contain the row for next-tick replay.
        on_disk = buffer.drain()
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0]['event_type'], 'marker_emit')

    def test_buffer_flush_on_reconnect_is_fifo(self):
        # First drain pass fails -> rows buffered.
        # Second pass appends new line, succeeds -> buffered rows flush
        # FIFO BEFORE the new fresh rows.
        outbox_log = Path(self._tmp) / 'logs' / 'outbox-notifier.log'
        outbox_log.write_text(
            '[2026-05-25T19:42:10Z] [INFO] MARKER_EMIT agent=forge '
            'task=task-first verdict=PROCEED\n'
        )
        buffer_path = Path(self._tmp) / 'state' / 'buf.jsonl'
        buffer = ces.EventBuffer(path=buffer_path)
        cursors: dict[str, ces.FileCursor] = {}

        # Pass 1: insert fails.
        failing_sink = _MockSink(raise_on_insert=True)
        ces.drain_once(
            failing_sink, buffer, cursors, ces.PulseCursor(),
            ces._setup_logging(),
            journal_iter_fn=self._empty_journal,
            pulse_path=Path(self._tmp) / 'blackboard' / 'absent.json',
            outbox_log_path=outbox_log,
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'a.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'b.jsonl',
        )
        self.assertEqual(len(failing_sink.rows), 0)
        self.assertEqual(len(buffer.drain()), 1)

        # Pass 2: append a new line; insert succeeds.
        with open(outbox_log, 'a') as fh:
            fh.write('[2026-05-25T19:42:11Z] [INFO] MARKER_EMIT agent=forge '
                     'task=task-second verdict=PROCEED\n')
        ok_sink = _MockSink(raise_on_insert=False)
        ces.drain_once(
            ok_sink, buffer, cursors, ces.PulseCursor(),
            ces._setup_logging(),
            journal_iter_fn=self._empty_journal,
            pulse_path=Path(self._tmp) / 'blackboard' / 'absent.json',
            outbox_log_path=outbox_log,
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'a.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'b.jsonl',
        )
        # Buffered "first" flushed BEFORE the fresh "second".
        self.assertEqual(len(ok_sink.rows), 2)
        self.assertEqual(ok_sink.rows[0]['task_id'], 'task-first')
        self.assertEqual(ok_sink.rows[1]['task_id'], 'task-second')
        # Buffer empty after successful flush.
        self.assertEqual(buffer.drain(), [])

    def test_buffer_overflow_under_chronic_outage(self):
        outbox_log = Path(self._tmp) / 'logs' / 'outbox-notifier.log'
        # Cap buffer to 2 to trigger overflow with our 3 lines.
        original_cap = ces.BUFFER_MAX_LINES
        ces.BUFFER_MAX_LINES = 2
        try:
            outbox_log.write_text(''.join(
                f'[2026-05-25T19:42:1{i}Z] [INFO] MARKER_EMIT agent=forge '
                f'task=task-{i} verdict=PROCEED\n' for i in range(3)
            ))
            sink = _MockSink(raise_on_insert=True)
            buffer = ces.EventBuffer(
                path=Path(self._tmp) / 'state' / 'buf.jsonl',
                max_lines=2, max_bytes=10**9,
            )
            stats, _ = ces.drain_once(
                sink, buffer, {}, ces.PulseCursor(), ces._setup_logging(),
                journal_iter_fn=self._empty_journal,
                pulse_path=Path(self._tmp) / 'blackboard' / 'absent.json',
                outbox_log_path=outbox_log,
                larry_alerts_path=Path(self._tmp) / 'blackboard' / 'a.jsonl',
                sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'b.jsonl',
            )
            self.assertEqual(stats.buffered, 3)
            self.assertEqual(stats.dropped_buffer_overflow, 1)
            kept = buffer.drain()
            self.assertEqual(len(kept), 2)
        finally:
            ces.BUFFER_MAX_LINES = original_cap

    def test_unknown_event_type_is_dropped(self):
        # Structured field with unknown event_type → drop + count, never insert.
        sink = _MockSink()
        buffer = ces.EventBuffer(path=Path(self._tmp) / 'state' / 'buf.jsonl')
        journal_records = [
            {'__REALTIME_TIMESTAMP': '1748189328000000',
             'MESSAGE': 'mystery',
             'OURLIBERTY_EVENT_TYPE': 'imagined_event_type',
             'OURLIBERTY_AGENT': 'forge',
             'OURLIBERTY_TASK_ID': 'abc'},
        ]
        stats, _ = ces.drain_once(
            sink, buffer, {}, ces.PulseCursor(), ces._setup_logging(),
            journal_iter_fn=lambda: iter(journal_records),
            pulse_path=Path(self._tmp) / 'blackboard' / 'absent.json',
            outbox_log_path=Path(self._tmp) / 'logs' / 'absent.log',
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'a.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'b.jsonl',
        )
        self.assertEqual(stats.dropped_unknown_type, 1)
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(sink.rows, [])

    def test_cursor_persists_resume_no_duplicate_inserts(self):
        # First drain pass: ingest 2 lines; second pass with appended lines
        # ingests ONLY the new ones — proves cursor resume works.
        outbox_log = Path(self._tmp) / 'logs' / 'outbox-notifier.log'
        outbox_log.write_text(
            '[2026-05-25T19:42:10Z] [INFO] MARKER_EMIT agent=forge '
            'task=t1 verdict=PROCEED\n'
            '[2026-05-25T19:42:11Z] [INFO] MARKER_EMIT agent=forge '
            'task=t2 verdict=PROCEED\n'
        )
        sink = _MockSink()
        buffer = ces.EventBuffer(path=Path(self._tmp) / 'state' / 'buf.jsonl')
        cursors: dict[str, ces.FileCursor] = {}

        ces.drain_once(
            sink, buffer, cursors, ces.PulseCursor(), ces._setup_logging(),
            journal_iter_fn=self._empty_journal,
            pulse_path=Path(self._tmp) / 'blackboard' / 'absent.json',
            outbox_log_path=outbox_log,
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'a.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'b.jsonl',
        )
        self.assertEqual(len(sink.rows), 2)

        # Append more lines; second drain pass should pick up only those.
        with open(outbox_log, 'a') as fh:
            fh.write('[2026-05-25T19:42:12Z] [INFO] MARKER_EMIT agent=forge '
                     'task=t3 verdict=PROCEED\n')
        sink2 = _MockSink()
        ces.drain_once(
            sink2, buffer, cursors, ces.PulseCursor(), ces._setup_logging(),
            journal_iter_fn=self._empty_journal,
            pulse_path=Path(self._tmp) / 'blackboard' / 'absent.json',
            outbox_log_path=outbox_log,
            larry_alerts_path=Path(self._tmp) / 'blackboard' / 'a.jsonl',
            sentinel_alerts_path=Path(self._tmp) / 'blackboard' / 'b.jsonl',
        )
        self.assertEqual(len(sink2.rows), 1)
        self.assertEqual(sink2.rows[0]['task_id'], 't3')


# -------------------- KNOWN_EVENT_TYPES discipline --------------------


class TestKnownEventTypesContract(unittest.TestCase):

    def test_known_set_matches_spec(self):
        # Spec § 5.1 (E4.4d) enumeration plus the push-instrumented writer
        # types added by E4.4e PR-A (spec § 5.1) and the clarify_response
        # type added by clarify-round-visibility § 6 (pushed from
        # outbox_notifier._handle_beacon_clarification_response). Keep this
        # in sync if the set changes. The review_pass/review_revision/
        # review_escalate types were added by check-x-verdict-emission
        # (pushed from outbox_notifier._emit_mirror_verdict_chain_event at
        # the Mirror verdict-classification site). The ceo_digest type was
        # added by the N6 digest generator (approvals-queue-rework spec),
        # push-emitted by scripts/ceo_digest_generator.py.
        spec_listed = {
            'session_start', 'session_done', 'marker_emit', 'auto_merge',
            'marker_error', 'cost_budget', 'review_request',
            'build_dispatched', 'preflight_proceed', 'preflight_clarify',
            'preflight_reject', 'escalation', 'larry_alert', 'sentinel_alert',
            'healer_fire',
            'approval_request', 'clarify_request', 'clarify_response',
            'larry_action',
            'review_pass', 'review_revision', 'review_escalate',
            'ceo_digest',
        }
        self.assertEqual(ces.KNOWN_EVENT_TYPES, frozenset(spec_listed))


if __name__ == '__main__':
    unittest.main()
