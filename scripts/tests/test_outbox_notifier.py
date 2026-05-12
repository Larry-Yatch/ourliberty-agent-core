#!/usr/bin/env python3
"""Fixtures for `outbox_notifier` — back-channel routing + dead-letter scan.

Phase D3 commit 2 (`D3-notifier`). Covers the routing decisions for each
outbox shape (bare-agent source, *-question source, *-result reply leg,
system source, self-dispatch), the depth-1 cap, dead-letter scan with
state dedup + GC, and the source-suffix derivation that distinguishes
clarification answers from work results.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_outbox_notifier
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import outbox_notifier as on        # noqa: E402
import routing_validator as rv      # noqa: E402
import safe_write_inbox as swi      # noqa: E402


def _good_outbox(**overrides):
    """Default result outbox shape (matches inbox_watcher._build_outbox)."""
    outbox = {
        'task_id': 'task-001',
        'agent': 'beacon',
        'source_task_file': '/home/larry/agents/inboxes/beacon/task-001.json',
        'reply_chat_id': None,
        'source': 'pulse',
        'started_at': '2026-05-11T20:00:00Z',
        'completed_at': '2026-05-11T20:00:05Z',
        'duration_sec': 5,
        'exit_code': 0,
        'model': 'claude-sonnet-4-6',
        'account_id': 'oauth',
        'attempts': 1,
        'result': 'task complete — observations recorded.',
        'claude_session_id': 'sess-abc-123',
        'cost_usd': 0.03,
        'usage': {},
    }
    outbox.update(overrides)
    return outbox


class HelperFunctionsTest(unittest.TestCase):
    """The small pure helpers — primary_agent, should_notify, notify_back_source, depth."""

    def test_primary_agent_id(self):
        self.assertEqual(on._primary_agent_id('pulse'), 'pulse')
        self.assertEqual(on._primary_agent_id('beacon-result'), 'beacon')
        self.assertEqual(on._primary_agent_id('forge-question'), 'forge')
        self.assertEqual(on._primary_agent_id('mirror-clarification'), 'mirror')
        self.assertIsNone(on._primary_agent_id('telegram-webhook'))
        self.assertIsNone(on._primary_agent_id('cron'))
        self.assertIsNone(on._primary_agent_id('larry'))
        self.assertIsNone(on._primary_agent_id(''))

    def test_should_notify_back(self):
        # Bare agent: notify back
        self.assertTrue(on._should_notify_back('pulse', 'beacon'))
        self.assertTrue(on._should_notify_back('beacon', 'forge'))
        # *-question: notify back
        self.assertTrue(on._should_notify_back('forge-question', 'beacon'))
        self.assertTrue(on._should_notify_back('mirror-question', 'beacon'))
        # *-result, *-clarification reply legs: don't notify back
        self.assertFalse(on._should_notify_back('pulse-result', 'beacon'))
        self.assertFalse(on._should_notify_back('beacon-clarification', 'forge'))
        self.assertFalse(on._should_notify_back('mirror-answer', 'beacon'))
        # System sources: don't notify back
        self.assertFalse(on._should_notify_back('telegram-webhook', 'beacon'))
        self.assertFalse(on._should_notify_back('cron', 'pulse'))
        self.assertFalse(on._should_notify_back('larry', 'beacon'))
        # Self-dispatch: don't notify back
        self.assertFalse(on._should_notify_back('beacon', 'beacon'))
        self.assertFalse(on._should_notify_back('forge-question', 'forge'))

    def test_notify_back_source_for_work_result(self):
        # Bare-agent dispatcher → -result suffix
        self.assertEqual(on._notify_back_source('beacon', 'pulse'), 'beacon-result')
        self.assertEqual(on._notify_back_source('forge', 'beacon'), 'forge-result')

    def test_notify_back_source_for_clarification(self):
        # *-question dispatcher → -clarification suffix
        self.assertEqual(
            on._notify_back_source('beacon', 'forge-question'),
            'beacon-clarification',
        )
        self.assertEqual(
            on._notify_back_source('beacon', 'mirror-question'),
            'beacon-clarification',
        )

    def test_current_notify_depth_explicit_field(self):
        self.assertEqual(on._current_notify_depth({'_notify_depth': 0}), 0)
        self.assertEqual(on._current_notify_depth({'_notify_depth': 1}), 1)
        self.assertEqual(on._current_notify_depth({'_notify_depth': 2}), 2)

    def test_current_notify_depth_from_filename(self):
        # source_task_file starts with notify- → depth 1
        self.assertEqual(
            on._current_notify_depth({
                'source_task_file': '/inboxes/beacon/notify-task-001.json',
            }),
            1,
        )
        # Plain task → depth 0
        self.assertEqual(
            on._current_notify_depth({
                'source_task_file': '/inboxes/beacon/task-001.json',
            }),
            0,
        )

    def test_truncate(self):
        short = 'hello'
        self.assertEqual(on._truncate(short), 'hello')
        long = 'x' * (on.MAX_RESULT_TEXT_CHARS + 100)
        out = on._truncate(long)
        self.assertIn('truncated', out)
        self.assertLess(len(out), len(long))


class ProcessOutboxTest(unittest.TestCase):
    """End-to-end process_outbox: file in, notify written or archived."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT',
            'BLACKBOARD', 'LOG_FILE', 'DEAD_LETTER_STATE',
            'EMERGENCY_HALT_FLAG',
        ]:
            self._originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'

        # safe_write_inbox shares state
        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
            'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'

        self._rv_root = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / 'repo'
        rv.invalidate_cache()
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def test_pulse_to_beacon_result_notifies_back_to_pulse(self):
        outbox = _good_outbox(agent='beacon', source='pulse', task_id='t-1')
        f = self._write_outbox('beacon', 't-1.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        # Outbox archived
        self.assertFalse(f.exists())
        archive = on.OUTBOXES_ROOT / 'beacon' / '.archive' / 't-1.json'
        self.assertTrue(archive.exists())

        # Notify landed in pulse's inbox
        pulse_inbox = on.INBOXES_ROOT / 'pulse'
        notifies = list(pulse_inbox.glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify_data = json.loads(notifies[0].read_text())
        self.assertEqual(notify_data['source'], 'beacon-result')
        self.assertEqual(notify_data['_notify_depth'], 1)
        self.assertIn('SUCCESS', notify_data['prompt'])
        # session_id propagated
        self.assertEqual(notify_data['session_id'], 'sess-abc-123')

    def test_failed_result_still_notifies_with_failed_framing(self):
        outbox = _good_outbox(
            agent='beacon', source='pulse', task_id='t-fail',
            exit_code=-1, error='claude timed out after 3 attempts',
            result='',
        )
        f = self._write_outbox('beacon', 't-fail.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        notifies = list((on.INBOXES_ROOT / 'pulse').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify_data = json.loads(notifies[0].read_text())
        self.assertIn('FAILED', notify_data['prompt'])
        self.assertIn('claude timed out', notify_data['prompt'])

    def test_forge_question_routes_back_as_clarification(self):
        outbox = _good_outbox(
            agent='beacon', source='forge-question', task_id='q-1',
            result='Use camelCase for the new field; the existing convention in agent-models.json is camelCase.',
        )
        f = self._write_outbox('beacon', 'q-1.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        # Notify lands in FORGE's inbox (primary agent of forge-question)
        forge_inbox = on.INBOXES_ROOT / 'forge'
        notifies = list(forge_inbox.glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify_data = json.loads(notifies[0].read_text())
        # Source = beacon-clarification (not beacon-result) because original was a question
        self.assertEqual(notify_data['source'], 'beacon-clarification')
        # Intent tagged
        self.assertEqual(notify_data['intent'], 'clarification-response')

    def test_system_source_outbox_archived_no_notify(self):
        outbox = _good_outbox(agent='beacon', source='telegram-webhook', task_id='t-tg')
        f = self._write_outbox('beacon', 't-tg.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'archived-no-notify')
        # No notify written anywhere
        for agent in on.AGENT_IDS:
            self.assertEqual(
                list((on.INBOXES_ROOT / agent).glob('notify-*.json')),
                [],
            )

    def test_reply_leg_source_archived_no_notify(self):
        # beacon-clarification IS a reply leg — don't double-notify
        outbox = _good_outbox(
            agent='forge', source='beacon-clarification', task_id='t-clar',
        )
        f = self._write_outbox('forge', 't-clar.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'archived-no-notify')

    def test_self_dispatch_skipped(self):
        outbox = _good_outbox(agent='beacon', source='beacon', task_id='t-self')
        f = self._write_outbox('beacon', 't-self.json', outbox)

        result = on.process_outbox(f)
        # Should be filtered by _should_notify_back (returns False for self-dispatch)
        # so archived-no-notify is the right return.
        self.assertIn(result, ('archived-no-notify', 'skip-self'))

    def test_depth_cap_blocks_second_hop_notify(self):
        # source_task_file starts with notify- → already depth 1
        outbox = _good_outbox(
            agent='beacon', source='pulse', task_id='t-deep',
            source_task_file='/home/larry/agents/inboxes/beacon/notify-prior.json',
        )
        f = self._write_outbox('beacon', 't-deep.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'depth-cap')
        # No notify written
        self.assertEqual(
            list((on.INBOXES_ROOT / 'pulse').glob('notify-*.json')),
            [],
        )

    def test_explicit_notify_depth_field_respected(self):
        outbox = _good_outbox(
            agent='beacon', source='pulse', task_id='t-explicit-depth',
            _notify_depth=1,
        )
        f = self._write_outbox('beacon', 't-explicit-depth.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'depth-cap')

    def test_short_result_prompt_padded_to_validator_floor(self):
        outbox = _good_outbox(agent='beacon', source='pulse', task_id='t-short', result='ok')
        f = self._write_outbox('beacon', 't-short.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        notifies = list((on.INBOXES_ROOT / 'pulse').glob('notify-*.json'))
        notify_data = json.loads(notifies[0].read_text())
        # Prompt must be >= MIN_PROMPT_LEN (100) to clear validator
        import dispatch_validator
        self.assertGreaterEqual(len(notify_data['prompt']), dispatch_validator.MIN_PROMPT_LEN)

    def test_partial_json_returns_skipped_no_archive(self):
        # Simulate a half-written outbox (watcher mid-write)
        f = on.OUTBOXES_ROOT / 'beacon'
        f.mkdir(parents=True, exist_ok=True)
        partial = f / 'partial.json'
        partial.write_text('{ "agent": "beacon", "source"')  # truncated JSON

        result = on.process_outbox(partial)
        self.assertEqual(result, 'partial-json')
        # File NOT archived — next cycle will retry once write completes
        self.assertTrue(partial.exists())


class DeadLetterScanTest(unittest.TestCase):
    """The .invalid/ scan that notifies dispatchers of validator rejections."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT',
            'BLACKBOARD', 'LOG_FILE', 'DEAD_LETTER_STATE',
            'EMERGENCY_HALT_FLAG',
        ]:
            self._originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'

        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
            'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'

        self._rv_root = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / 'repo'
        rv.invalidate_cache()
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _plant_invalid(self, agent, name, source, prompt='', reason='F24 empty-prompt'):
        invalid_dir = on.INBOXES_ROOT / agent / '.invalid'
        invalid_dir.mkdir(parents=True, exist_ok=True)
        task_file = invalid_dir / name
        task = {
            'task_id': name.replace('.json', ''),
            'source': source,
            'prompt': prompt or 'too short',
        }
        task_file.write_text(json.dumps(task))
        reason_file = invalid_dir / name.replace('.json', '.reason')
        reason_file.write_text(reason)
        return task_file, reason_file

    def test_dead_letter_notifies_source_agent(self):
        self._plant_invalid('forge', 'bad-task.json', source='beacon')
        n = on.scan_dead_letters()
        self.assertEqual(n, 1)

        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-dead-letter-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['source'], 'forge-result')
        self.assertIn('DEAD LETTER', data['prompt'])
        self.assertIn('F24 empty-prompt', data['prompt'])

    def test_dead_letter_dedup_across_runs(self):
        self._plant_invalid('forge', 'bad.json', source='beacon')
        n1 = on.scan_dead_letters()
        self.assertEqual(n1, 1)
        n2 = on.scan_dead_letters()
        self.assertEqual(n2, 0, 'second run should dedup')

    def test_dead_letter_skips_system_source(self):
        self._plant_invalid('forge', 'sys.json', source='telegram-webhook')
        n = on.scan_dead_letters()
        self.assertEqual(n, 0)

    def test_dead_letter_gc_on_resolution(self):
        task_file, reason_file = self._plant_invalid('forge', 'gc.json', source='beacon')
        on.scan_dead_letters()
        # Operator resolves the .invalid file
        task_file.unlink()
        reason_file.unlink()
        on.scan_dead_letters()
        state = json.loads(on.DEAD_LETTER_STATE.read_text())
        # Key should be gc'd out of state
        self.assertNotIn('forge:gc.json', state['processed'])


if __name__ == '__main__':
    unittest.main()
