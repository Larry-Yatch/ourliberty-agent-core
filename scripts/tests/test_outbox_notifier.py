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
from unittest import mock

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
        # Uses the unified build_notify_prompt template now (intent=dead-letter).
        self.assertEqual(data['intent'], 'dead-letter')
        self.assertIn('dead-letter', data['prompt'])
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


class BuildNotifyPromptTest(unittest.TestCase):
    """The refined Option-C hybrid notify-prompt template (commit 4a)."""

    def test_default_result_notification_intent_block_present(self):
        prompt = on.build_notify_prompt(
            intent='result-notification',
            sender='beacon',
            task_id='t-1',
            success=True,
            output='cycle complete; 3 observations recorded',
        )
        # Header tag
        self.assertIn('[Inter-agent notify | intent=result-notification | from=beacon', prompt)
        self.assertIn('task=t-1', prompt)
        self.assertIn('status=SUCCESS', prompt)
        # Framing
        self.assertIn('not a new task request', prompt)
        # Action verb
        self.assertIn('Journal it in your activity log', prompt)
        self.assertIn('Do not generate new work', prompt)
        # Output section
        self.assertIn('3 observations recorded', prompt)

    def test_failure_status_in_header(self):
        prompt = on.build_notify_prompt(
            intent='result-notification',
            sender='forge',
            task_id='t-fail',
            success=False,
            output='',
            error='claude timed out after 3 attempts',
        )
        self.assertIn('status=FAILED', prompt)
        self.assertIn('claude timed out', prompt)

    def test_clarification_response_includes_remaining_count(self):
        prompt = on.build_notify_prompt(
            intent='clarification-response',
            sender='beacon',
            task_id='watchdog-001',
            success=True,
            output='use line range 730-740 from the doc',
            intent_kwargs={'remaining': 2},
        )
        self.assertIn('clarification-response', prompt)
        self.assertIn('CLARIFY_REQUEST on task `watchdog-001`', prompt)
        self.assertIn('2 clarification', prompt)
        self.assertIn('PROCEED', prompt)
        self.assertIn('REJECT', prompt)

    def test_clarification_request_to_beacon_includes_counter(self):
        prompt = on.build_notify_prompt(
            intent='clarify',
            sender='forge',
            task_id='watchdog-001',
            success=True,
            output='Which line range covers the watchdog warning?',
            intent_kwargs={'next_count': 1, 'max_count': 3},
        )
        self.assertIn('clarify', prompt)
        self.assertIn('clarification 1 of 3', prompt)
        self.assertIn('answer in-scope', prompt)
        self.assertIn('escalate to Larry', prompt)
        self.assertIn('Which line range', prompt)

    def test_preflight_proceed_intent(self):
        prompt = on.build_notify_prompt(
            intent='ack-proceed',
            sender='forge',
            task_id='watchdog-001',
            success=True,
            output='Will edit docs/operating-manual.md L730-L740',
        )
        self.assertIn('ack-proceed', prompt)
        self.assertIn('PROCEED on task `watchdog-001`', prompt)
        self.assertIn('build phase will dispatch', prompt)

    def test_preflight_rejection_with_reason(self):
        prompt = on.build_notify_prompt(
            intent='reject',
            sender='forge',
            task_id='t-bad-spec',
            success=True,
            output='Spec references nonexistent file foo.md',
            intent_kwargs={'reason': 'Spec references nonexistent file foo.md'},
        )
        self.assertIn('reject', prompt)
        self.assertIn('REJECTED task `t-bad-spec`', prompt)
        self.assertIn('nonexistent file', prompt)
        self.assertIn('Do not retry without addressing', prompt)

    def test_dead_letter_intent(self):
        prompt = on.build_notify_prompt(
            intent='dead-letter',
            sender='inbox-watcher',
            task_id='t-rejected',
            success=False,
            output='',
            intent_kwargs={'reason': 'F24 prompt too short'},
        )
        self.assertIn('dead-letter', prompt)
        self.assertIn('F24 prompt too short', prompt)

    def test_marker_error_intent(self):
        prompt = on.build_notify_prompt(
            intent='marker-error',
            sender='outbox-notifier',
            task_id='t-broken',
            success=False,
            output='=== PROCEED ===\n{bad json}',
            intent_kwargs={
                'reason': 'invalid JSON: Expecting property name',
                'task_id': 't-broken',
            },
        )
        self.assertIn('marker-error', prompt)
        self.assertIn('could not be parsed', prompt)
        self.assertIn('Re-read your CLAUDE.md', prompt)

    def test_unknown_intent_falls_back_to_default_action(self):
        prompt = on.build_notify_prompt(
            intent='unknown-future-intent',
            sender='beacon',
            task_id='t-x',
            success=True,
            output='some output',
        )
        # Falls back to result-notification action block
        self.assertIn('Journal it in your activity log', prompt)

    def test_missing_intent_kwargs_does_not_crash(self):
        # clarification-response template needs {remaining} but we omit it
        prompt = on.build_notify_prompt(
            intent='clarification-response',
            sender='beacon',
            task_id='t-1',
            success=True,
            output='answer text',
            # intent_kwargs intentionally missing 'remaining'
        )
        # Should render (with unsubstituted template) rather than crash
        self.assertIn('clarification-response', prompt)
        self.assertIn('answer text', prompt)

    def test_short_output_padded_to_validator_floor(self):
        prompt = on.build_notify_prompt(
            intent='result-notification',
            sender='beacon',
            task_id='t-tiny',
            success=True,
            output='ok',
        )
        import dispatch_validator
        self.assertGreaterEqual(len(prompt), dispatch_validator.MIN_PROMPT_LEN)


class ForgeMarkerRoutingTest(unittest.TestCase):
    """Marker-driven routing for Forge outboxes — process_outbox integration."""

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

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _forge_outbox(self, marker_text, **overrides):
        outbox = _good_outbox(
            agent='forge',
            source='beacon',
            task_id='t-pf',
            result=(
                'Read the spec. Traced the line numbers. Ready to act.\n\n'
                + marker_text
            ),
        )
        outbox.update(overrides)
        return outbox

    def test_proceed_marker_routes_to_beacon_as_forge_result(self):
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "t-pf", "preflight_summary": "Will edit X line 12."}\n'
            '=== END_PROCEED ==='
        )
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 't-pf.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['source'], 'forge-result')
        self.assertEqual(data['intent'], 'ack-proceed')
        self.assertIn('PROCEED on task `t-pf`', data['prompt'])
        self.assertIn('Will edit X line 12', data['prompt'])

    def test_clarify_request_routes_to_beacon_as_forge_question(self):
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-pf", "question": "Which line range exactly?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = self._forge_outbox(
            marker,
            clarification_count=0,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 't-pf.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        # Source is forge-question (not forge-result) → tells Beacon's flow
        # this is a question that should be answered, not a result to journal
        self.assertEqual(data['source'], 'forge-question')
        self.assertEqual(data['intent'], 'clarify')
        # Counter propagated for the next leg
        self.assertEqual(data['clarification_count'], 1)
        self.assertEqual(data['max_clarifications'], 3)
        self.assertIn('clarification 1 of 3', data['prompt'])
        self.assertIn('Which line range', data['prompt'])

    def test_reject_marker_routes_to_beacon_as_forge_result(self):
        marker = (
            '=== REJECT ===\n'
            '{"task_id": "t-pf", "reason": "Spec references missing file."}\n'
            '=== END_REJECT ==='
        )
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 't-pf.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        data = json.loads(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))[0].read_text()
        )
        self.assertEqual(data['source'], 'forge-result')
        self.assertEqual(data['intent'], 'reject')
        self.assertIn('Spec references missing file', data['prompt'])

    def test_clarify_at_budget_exhausted_converts_to_exhausted_intent(self):
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-pf", "question": "Final ambiguity?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = self._forge_outbox(
            marker,
            clarification_count=3,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 't-pf.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        data = json.loads(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))[0].read_text()
        )
        # Source becomes forge-result (terminates the question round); intent
        # is clarification-exhausted (not generic reject) so Beacon sees why.
        self.assertEqual(data['source'], 'forge-result')
        self.assertEqual(data['intent'], 'clarification-exhausted')
        # Reason includes the question that exhausted budget
        self.assertIn('Final ambiguity?', data['prompt'])
        self.assertIn('budget', data['prompt'].lower())

    def test_clarify_marker_bypasses_depth_cap(self):
        """The preflight protocol is multi-hop; depth cap must not block it."""
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-pf", "question": "Q?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = self._forge_outbox(
            marker,
            # This would normally trigger depth-cap (>1)
            source_task_file='/inboxes/forge/notify-prior.json',
            _notify_depth=1,
            clarification_count=1,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 't-pf-resume.json', outbox)

        result = on.process_outbox(f)
        # Marker-driven path should bypass depth cap and notify regardless
        self.assertEqual(result, 'notified-marker')
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)

    def test_clarify_marker_bypasses_should_notify_back_filter(self):
        """If source is 'beacon-clarification' (reply leg), normally archived.
        But a marker present means the protocol round is in progress — must notify."""
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "t-pf", "preflight_summary": "Got the clarification, building."}\n'
            '=== END_PROCEED ==='
        )
        outbox = self._forge_outbox(
            marker,
            source='beacon-clarification',
            clarification_count=1,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 't-pf-resumed.json', outbox)

        result = on.process_outbox(f)
        # Without marker handling this would be 'archived-no-notify'
        self.assertEqual(result, 'notified-marker')

    def test_malformed_marker_dead_letters_back_to_forge(self):
        marker = '=== PROCEED ===\n{bad json}\n=== END_PROCEED ==='
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 't-pf-bad.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')

        # Marker-error notify lands in FORGE's inbox (not Beacon's)
        forge_inbox = on.INBOXES_ROOT / 'forge'
        marker_errors = list(forge_inbox.glob('marker-error-*.json'))
        self.assertEqual(len(marker_errors), 1)
        data = json.loads(marker_errors[0].read_text())
        self.assertEqual(data['source'], 'outbox-notifier')
        self.assertEqual(data['intent'], 'marker-error')
        # original_source propagated so the recovered marker can route back
        self.assertEqual(data['original_source'], 'beacon')
        # First retry → count starts at 1
        self.assertEqual(data['marker_error_count'], 1)
        self.assertIn('could not be parsed', data['prompt'])
        self.assertIn('invalid JSON', data['prompt'])

    def test_clarify_notify_propagates_target_repo_and_branch(self):
        """4b post-test-2 fix: clarification cascade must propagate
        target_repo/branch through BOTH notify hops. Without these on the
        notify task, the answer leg dies at Forge's worktree gate."""
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-pf", "question": "Which line?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = self._forge_outbox(
            marker,
            target_repo='ourliberty-agent-core',
            branch='forge/t-pf',
            pr_title='docs: example title',
            pr_body='example body',
            clarification_count=0,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 't-pf-clarify.json', outbox)
        on.process_outbox(f)

        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/t-pf')
        self.assertEqual(data['pr_title'], 'docs: example title')
        self.assertEqual(data['pr_body'], 'example body')

    def test_clarification_answer_leg_propagates_target_repo_and_branch(self):
        """Beacon's answer to a forge-question must carry target_repo/branch
        back to Forge so her re-preflight invocation passes the worktree gate."""
        # Simulate Beacon's outbox responding to a forge-question.
        outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='t-pf',
            target_repo='ourliberty-agent-core',
            branch='forge/t-pf',
            pr_title='docs: example title',
            pr_body='example body',
            clarification_count=1,
            max_clarifications=3,
            result='Use line 258 — the systemd-units table row.',
        )
        f = self._write_outbox('beacon', 't-pf-answer.json', outbox)
        on.process_outbox(f)

        notifies = list((on.INBOXES_ROOT / 'forge').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/t-pf')
        self.assertEqual(data['pr_title'], 'docs: example title')
        self.assertEqual(data['pr_body'], 'example body')

    def test_marker_error_propagates_target_repo_and_branch(self):
        """4b review fix: malformed-marker retry must carry target_repo/branch
        forward or Forge's `worktree_enabled` watcher will reject the retry."""
        marker = '=== PROCEED ===\n{bad json}\n=== END_PROCEED ==='
        outbox = self._forge_outbox(
            marker,
            target_repo='ourliberty-agent-core',
            branch='forge/watchdog-fix-001',
        )
        f = self._write_outbox('forge', 't-pf-bad-fields.json', outbox)
        on.process_outbox(f)

        marker_errors = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(marker_errors), 1)
        data = json.loads(marker_errors[0].read_text())
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/watchdog-fix-001')

    def test_marker_error_recovered_marker_routes_via_original_source(self):
        """The C1 fix: after a malformed marker, Forge's recovered output has
        source=outbox-notifier (infra), but original_source=beacon propagates
        through, so the recovered marker reaches Beacon, not a dead end."""
        # Simulate Forge's outbox AFTER she received a marker-error notify and
        # re-emitted a clean PROCEED. The outbox carries propagated fields.
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "t-recovered", "preflight_summary": "Recovered cleanly."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge',
            source='outbox-notifier',  # the previous marker-error notify
            task_id='t-recovered',
            result='Got the marker-error notify, here is my corrected marker.\n\n' + marker,
            original_source='beacon',  # propagated from the marker-error envelope
            marker_error_count=1,
        )
        f = self._write_outbox('forge', 't-recovered.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        # Recovered marker landed in BEACON's inbox (not archived as dead end)
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['intent'], 'ack-proceed')
        self.assertEqual(data['source'], 'forge-result')

    def test_marker_error_retry_cap_dead_letters_to_dispatcher(self):
        """The C2 fix: when marker_error_count exceeds MAX_MARKER_ERROR_RETRIES,
        the notifier dead-letters to Beacon instead of looping back to Forge."""
        marker = '=== PROCEED ===\n{still bad json}\n=== END_PROCEED ==='
        outbox = _good_outbox(
            agent='forge',
            source='outbox-notifier',
            task_id='t-loop',
            result=marker,
            original_source='beacon',
            marker_error_count=on.MAX_MARKER_ERROR_RETRIES,  # next retry exceeds cap
        )
        f = self._write_outbox('forge', 't-loop.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')

        # No new marker-error notify to forge (cap exceeded)
        forge_marker_errors = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(forge_marker_errors), 0)

        # Dead-letter to Beacon instead
        beacon_dead_letters = list(
            (on.INBOXES_ROOT / 'beacon').glob('dead-letter-marker-*.json')
        )
        self.assertEqual(len(beacon_dead_letters), 1)
        data = json.loads(beacon_dead_letters[0].read_text())
        self.assertEqual(data['intent'], 'dead-letter')
        self.assertIn('malformed markers', data['prompt'])
        self.assertIn('cap=3', data['prompt'])

    def test_multiple_markers_dead_letters_back_to_forge(self):
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "t-1", "preflight_summary": "x"}\n'
            '=== END_PROCEED ===\n\n'
            'Wait, actually:\n\n'
            '=== REJECT ===\n'
            '{"task_id": "t-1", "reason": "y"}\n'
            '=== END_REJECT ==='
        )
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 't-pf-dup.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')

        marker_errors = list((on.INBOXES_ROOT / 'forge').glob('marker-error-*.json'))
        self.assertEqual(len(marker_errors), 1)
        data = json.loads(marker_errors[0].read_text())
        self.assertIn('expected exactly one', data['prompt'])

    def test_full_cascade_three_forge_two_beacon(self):
        """M5 — End-to-end cascade across CLARIFY → answer → CLARIFY → answer
        → PROCEED. Verifies clarification_count propagates correctly through
        both marker-driven (forge) and default-path (beacon) legs."""
        # === Round 1: Forge clarifies (count 0 → 1) ===
        forge_outbox_1 = _good_outbox(
            agent='forge', source='beacon', task_id='t-cascade',
            result=(
                'Need a quick clarification.\n\n'
                '=== CLARIFY_REQUEST ===\n'
                '{"task_id": "t-cascade", "question": "Which line range?"}\n'
                '=== END_CLARIFY_REQUEST ==='
            ),
            clarification_count=0,
            max_clarifications=3,
        )
        f1 = self._write_outbox('forge', 't-cascade-r1.json', forge_outbox_1)
        r1 = on.process_outbox(f1)
        self.assertEqual(r1, 'notified-marker')

        # Beacon got a forge-question notify with count=1 propagated
        beacon_inbox_1 = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(beacon_inbox_1), 1)
        beacon_task_1 = json.loads(beacon_inbox_1[0].read_text())
        self.assertEqual(beacon_task_1['source'], 'forge-question')
        self.assertEqual(beacon_task_1['intent'], 'clarify')
        self.assertEqual(beacon_task_1['clarification_count'], 1)
        self.assertEqual(beacon_task_1['max_clarifications'], 3)

        # === Round 2: Beacon answers (default path; count=1 carries forward) ===
        beacon_outbox_1 = _good_outbox(
            agent='beacon', source='forge-question',
            source_task_file=str(beacon_inbox_1[0]),
            task_id='notify-t-cascade-r1',
            result='Line range is L730-L740. Use that.',
            clarification_count=1,
            max_clarifications=3,
        )
        f2 = self._write_outbox('beacon', 'b-r1.json', beacon_outbox_1)
        r2 = on.process_outbox(f2)
        self.assertEqual(r2, 'notified')

        # Forge got a beacon-clarification with count=1 forwarded
        forge_inbox_1 = list((on.INBOXES_ROOT / 'forge').glob('notify-*.json'))
        self.assertEqual(len(forge_inbox_1), 1)
        forge_task_1 = json.loads(forge_inbox_1[0].read_text())
        self.assertEqual(forge_task_1['source'], 'beacon-clarification')
        self.assertEqual(forge_task_1['intent'], 'clarification-response')
        self.assertEqual(forge_task_1['clarification_count'], 1)
        # The notify prompt tells Forge she has 2 remaining
        self.assertIn('2 clarification', forge_task_1['prompt'])

        # === Round 3: Forge clarifies again (count 1 → 2) ===
        forge_outbox_2 = _good_outbox(
            agent='forge', source='beacon-clarification', task_id='t-cascade',
            result=(
                '=== CLARIFY_REQUEST ===\n'
                '{"task_id": "t-cascade", "question": "And the file path?"}\n'
                '=== END_CLARIFY_REQUEST ==='
            ),
            clarification_count=1,
            max_clarifications=3,
        )
        f3 = self._write_outbox('forge', 't-cascade-r3.json', forge_outbox_2)
        r3 = on.process_outbox(f3)
        # Note: source is beacon-clarification (reply leg) but marker handling
        # overrides the default archive-no-notify behavior
        self.assertEqual(r3, 'notified-marker')

        # Second beacon notify, count now 2
        beacon_inbox_2 = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(beacon_inbox_2), 2)
        # Get the newer one
        newer = max(beacon_inbox_2, key=lambda p: p.stat().st_mtime)
        beacon_task_2 = json.loads(newer.read_text())
        self.assertEqual(beacon_task_2['clarification_count'], 2)
        self.assertIn('clarification 2 of 3', beacon_task_2['prompt'])

        # === Round 4: Beacon answers (count=2 carries forward) ===
        beacon_outbox_2 = _good_outbox(
            agent='beacon', source='forge-question',
            source_task_file=str(newer),
            task_id='notify-t-cascade-r3',
            result='docs/operating-manual.md',
            clarification_count=2,
            max_clarifications=3,
        )
        f4 = self._write_outbox('beacon', 'b-r2.json', beacon_outbox_2)
        r4 = on.process_outbox(f4)
        self.assertEqual(r4, 'notified')

        forge_inbox_2 = list((on.INBOXES_ROOT / 'forge').glob('notify-*.json'))
        self.assertEqual(len(forge_inbox_2), 2)
        newer_forge = max(forge_inbox_2, key=lambda p: p.stat().st_mtime)
        forge_task_2 = json.loads(newer_forge.read_text())
        self.assertEqual(forge_task_2['clarification_count'], 2)
        # 1 clarification left
        self.assertIn('1 clarification', forge_task_2['prompt'])

        # === Round 5: Forge PROCEEDs (terminal) ===
        forge_outbox_3 = _good_outbox(
            agent='forge', source='beacon-clarification', task_id='t-cascade',
            result=(
                'Got it. Building.\n\n'
                '=== PROCEED ===\n'
                '{"task_id": "t-cascade", "preflight_summary": "Edit docs/operating-manual.md L730-L740."}\n'
                '=== END_PROCEED ==='
            ),
            clarification_count=2,
            max_clarifications=3,
        )
        f5 = self._write_outbox('forge', 't-cascade-r5.json', forge_outbox_3)
        r5 = on.process_outbox(f5)
        self.assertEqual(r5, 'notified-marker')

        # Final notify to Beacon with intent=ack-proceed
        beacon_inbox_3 = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(beacon_inbox_3), 3)
        final = max(beacon_inbox_3, key=lambda p: p.stat().st_mtime)
        final_task = json.loads(final.read_text())
        self.assertEqual(final_task['intent'], 'ack-proceed')
        self.assertIn('Edit docs/operating-manual.md', final_task['prompt'])

    def test_forge_outbox_without_marker_uses_default_routing(self):
        """Backward-compat: a Forge outbox with no marker (legacy or test
        scenario) follows the existing should_notify_back path."""
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-legacy',
            result='Plain text result, no marker block here at all.',
        )
        f = self._write_outbox('forge', 't-legacy.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        # Default routing → forge-result
        self.assertEqual(data['source'], 'forge-result')
        # Default template → result-notification intent
        self.assertIn('intent=result-notification', data['prompt'])

    def test_non_forge_agent_with_marker_text_uses_default_routing(self):
        """Only Forge outputs are marker-parsed. A marker-shaped string in
        another agent's output should NOT trigger marker handling."""
        outbox = _good_outbox(
            agent='beacon', source='pulse', task_id='t-beacon',
            result=(
                'Discussing the preflight protocol:\n\n'
                '=== PROCEED ===\n'
                '{"task_id": "x", "preflight_summary": "example"}\n'
                '=== END_PROCEED ==='
            ),
        )
        f = self._write_outbox('beacon', 't-beacon.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        data = json.loads(
            list((on.INBOXES_ROOT / 'pulse').glob('notify-*.json'))[0].read_text()
        )
        # Beacon's output isn't parsed for Forge markers
        self.assertEqual(data['source'], 'beacon-result')
        self.assertIn('intent=result-notification', data['prompt'])


class HeadlessClarificationRoutingTest(unittest.TestCase):
    """task-25 — chain-routing gap #5: headless Beacon clarification-response
    must route as `--resume` of the ORIGINAL Forge task, NOT as a fresh
    `notify-notify-{task}` envelope. Closes the doubled-prefix branch bug +
    depth-multiplied awareness notifies that surfaced 2026-05-20 on task-22.

    Mirrors `ProcessOutboxTest` setup — temp dirs, swi/rv state rebound,
    on.ensure_dirs(). Kept as a separate class so the test names group
    cleanly under the task-25 fix surface.
    """

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

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    # ---- Happy path: Beacon clarification-response → resume envelope ----

    def test_clarification_response_routes_as_resume_envelope(self):
        """Beacon answers Forge's CLARIFY_REQUEST → continuation envelope
        lands on Forge's inbox under the ORIGINAL task_id (NOT notify-{...}).
        Source = beacon-clarification, intent = clarification-response,
        phase = preflight, resume_session_id = Forge's preflight session."""
        beacon_outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='notify-task-22-agents-root-path-isolation',
            forge_session_id='forge-sess-original-001',
            result='Use pytest, not unittest. The convention in this repo is pytest discovery.',
            clarification_count=1,
            max_clarifications=3,
            target_repo='ourliberty-agent-core',
            branch='forge/task-22-agents-root-path-isolation',
        )
        f = self._write_outbox('beacon', 'notify-task-22.json', beacon_outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'clarification-resume-dispatched')

        # The continuation envelope landed in Forge's inbox.
        forge_inbox = on.INBOXES_ROOT / 'forge'
        files = list(forge_inbox.glob('*.json'))
        self.assertEqual(len(files), 1, f'expected exactly one envelope, got {files}')

        dest = files[0]
        # Filename pattern: resume-<task>-r<count>.json — NOT notify-notify-*.
        self.assertTrue(dest.name.startswith('resume-'),
                        f'filename should be resume-prefixed, got {dest.name}')
        self.assertNotIn('notify-notify', dest.name)

        envelope = json.loads(dest.read_text())
        # task_id = ORIGINAL (stripped of `notify-` prefix).
        self.assertEqual(envelope['task_id'],
                         'task-22-agents-root-path-isolation')
        # Resume opt-in: explicit field that the watcher honors regardless
        # of phase. Carries Forge's preflight session.
        self.assertEqual(envelope['resume_session_id'],
                         'forge-sess-original-001')
        # Source is a dialogue-leg suffix (allowed regardless of FRESH_DISPATCH_ROUTES).
        self.assertEqual(envelope['source'], 'beacon-clarification')
        # Intent matches the existing notify-template vocabulary.
        self.assertEqual(envelope['intent'], 'clarification-response')
        # Phase stays 'preflight' — semantically Forge re-runs preflight
        # with the answer; the marker she emits next is preflight-discipline.
        self.assertEqual(envelope['phase'], 'preflight')
        # target_repo / branch propagated so Forge's worktree gate accepts.
        self.assertEqual(envelope['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(envelope['branch'],
                         'forge/task-22-agents-root-path-isolation')

    def test_no_doubled_prefix_file_in_any_inbox(self):
        """Acceptance criterion: no `notify-notify-{task}` file appears in
        ANY inbox after the clarification-response routes. Verifies the
        cascade stops at the continuation envelope rather than producing
        the doubled-prefix bug shape."""
        beacon_outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='notify-task-X',
            forge_session_id='forge-sess-001',
            result='Answer text that satisfies the validator floor for length' * 5,
            clarification_count=1,
            max_clarifications=3,
        )
        f = self._write_outbox('beacon', 'notify-task-X.json', beacon_outbox)

        on.process_outbox(f)

        # No `notify-notify-*` file anywhere.
        for agent in on.AGENT_IDS:
            inbox = on.INBOXES_ROOT / agent
            bad = list(inbox.glob('notify-notify-*.json'))
            self.assertEqual(
                bad, [],
                f'doubled-prefix file should not exist in {agent} inbox: {bad}',
            )
            # Also not in archive.
            archive = inbox / '.archive'
            if archive.exists():
                bad_arch = list(archive.glob('notify-notify-*.json'))
                self.assertEqual(
                    bad_arch, [],
                    f'doubled-prefix file should not exist in {agent} archive: {bad_arch}',
                )

    def test_forge_marker_on_original_task_id_is_accepted_after_resume(self):
        """Acceptance criterion: when Forge emits a marker against the
        ORIGINAL task_id after picking up the resume envelope, the marker
        is accepted (no marker-task-id-mismatch). Round-trip via
        _classify_forge_marker on a synthesized Forge outbox shape."""
        # The continuation envelope (round 1) gave Forge task_id=task-X.
        # Forge processes, re-runs preflight, emits PROCEED with that
        # same task_id. Her outbox naturally carries task_id=task-X.
        forge_outbox_data = {
            'agent': 'forge',
            'task_id': 'task-X',  # ORIGINAL, not notify-notify-task-X
            'source': 'beacon-clarification',
            'original_source': None,
            'result': (
                'Got it.\n\n'
                '=== PROCEED ===\n'
                '{"task_id": "task-X", "preflight_summary": "ok."}\n'
                '=== END_PROCEED ==='
            ),
            'clarification_count': 1,
            'max_clarifications': 3,
            'claude_session_id': 'forge-sess-001',
        }
        # If task_ids matched, this returns a decision; if not, raises.
        decision = on._classify_forge_marker(forge_outbox_data)
        self.assertIsNotNone(decision)
        self.assertEqual(decision['marker_type'], 'proceed')
        self.assertEqual(decision['intent'], 'ack-proceed')

    def test_depth_multiplication_does_not_recur_across_three_rounds(self):
        """Acceptance criterion: awareness notifies are capped at depth 1
        regardless of how many clarification rounds occur. Run round 1 +
        round 2 + round 3 and confirm no notify-notify-notify-* file.
        Each round uses a distinct filename (clarification_count varies),
        so all three continuation envelopes coexist cleanly."""
        for round_num, count in enumerate([1, 2, 3], start=1):
            beacon_outbox = _good_outbox(
                agent='beacon',
                source='forge-question',
                task_id='notify-task-Y',
                forge_session_id='forge-sess-Y',
                result=f'Round {round_num} answer text long enough to satisfy validator: ' * 5,
                clarification_count=count,
                max_clarifications=5,
            )
            f = self._write_outbox('beacon', f'notify-task-Y-r{round_num}.json',
                                   beacon_outbox)
            res = on.process_outbox(f)
            self.assertEqual(res, 'clarification-resume-dispatched',
                             f'round {round_num} should have resume-dispatched')

        forge_inbox = on.INBOXES_ROOT / 'forge'
        # No `notify-*-notify-*` cascade prefix.
        for name in [p.name for p in forge_inbox.iterdir() if p.is_file()]:
            self.assertNotIn('notify-notify', name,
                             f'{name} carries the doubled-prefix bug shape')
            self.assertNotIn('notify-notify-notify', name)

        # Three distinct continuation envelopes (one per round).
        resumes = sorted(p.name for p in forge_inbox.glob('resume-*.json'))
        self.assertEqual(
            resumes,
            [
                'resume-task-Y-r1.json',
                'resume-task-Y-r2.json',
                'resume-task-Y-r3.json',
            ],
        )

    def test_idempotent_when_outbox_reprocessed(self):
        """Daemon-crash safety: if the daemon crashes between dispatch and
        archive, the next poll re-processes the same outbox. The handler
        should detect the prior write (inbox / archive / invalid) and skip
        — the same idempotency shape as _handle_beacon_headless_approval_request.
        """
        beacon_outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='notify-task-Z',
            forge_session_id='forge-sess-Z',
            result='Answer text that easily clears the validator min-length floor.' * 3,
            clarification_count=1,
            max_clarifications=3,
        )
        f1 = self._write_outbox('beacon', 'notify-task-Z.json', beacon_outbox)
        on.process_outbox(f1)
        # Simulate daemon crash: outbox got archived but a fresh poll
        # finds a "same identity" beacon outbox a second time. Write the
        # outbox file again and re-run.
        f2 = self._write_outbox('beacon', 'notify-task-Z.json', beacon_outbox)
        res = on.process_outbox(f2)
        # Handler still returns the dispatched sentinel (idempotency
        # short-circuit kept the existing file in place).
        self.assertEqual(res, 'clarification-resume-dispatched')
        # Only one resume envelope exists (file + archive copy from
        # round-trip archiving).
        forge_inbox = on.INBOXES_ROOT / 'forge'
        resumes = list(forge_inbox.glob('resume-*.json'))
        archived_resumes = list((forge_inbox / '.archive').glob('resume-*.json'))
        self.assertEqual(len(resumes) + len(archived_resumes), 1,
                         f'expected exactly one continuation envelope across '
                         f'inbox+archive, got inbox={resumes} '
                         f'archive={archived_resumes}')

    def test_missing_forge_session_id_falls_back_to_default_routing(self):
        """Graceful degradation: pre-task-25 chains (in-flight at upgrade
        time) won't have forge_session_id propagated. The handler must fall
        through to default notify routing so Beacon's answer still reaches
        Forge. The doubled-prefix bug recurs in that case, but the chain
        completes — better than dead-lettering legacy traffic."""
        beacon_outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='notify-task-legacy',
            # No forge_session_id — simulates pre-task-25 cascade in flight
            # when the upgrade ships.
            result='Legacy answer; chain in flight at upgrade time.' * 4,
            clarification_count=1,
            max_clarifications=3,
        )
        f = self._write_outbox('beacon', 'notify-task-legacy.json',
                               beacon_outbox)
        result = on.process_outbox(f)
        # Falls through to default routing (the pre-task-25 path).
        self.assertEqual(result, 'notified')
        # No resume envelope (handler declined).
        forge_inbox = on.INBOXES_ROOT / 'forge'
        self.assertEqual(list(forge_inbox.glob('resume-*.json')), [])
        # The legacy notify-notify-* file does appear (this is the bug
        # shape; we're explicitly accepting it for legacy traffic). The
        # important thing is the chain progresses.
        notifies = list(forge_inbox.glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)

    def test_clarify_request_notify_carries_forge_session_id(self):
        """End-to-end propagation: when Forge emits CLARIFY_REQUEST, the
        notify going to Beacon's inbox MUST carry `forge_session_id` so
        `inbox_watcher._build_outbox` can propagate it across Beacon's
        round-trip and the clarification-response handler has the field
        when it fires. Without this field on the upstream notify, the
        downstream resume path silently degrades to legacy behavior."""
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-pf", "question": "Which line range?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        forge_outbox = _good_outbox(
            agent='forge',
            source='beacon',
            task_id='t-pf',
            result=marker,
            clarification_count=0,
            max_clarifications=3,
            claude_session_id='forge-preflight-session-XYZ',
            target_repo='ourliberty-agent-core',
            branch='forge/t-pf',
        )
        f = self._write_outbox('forge', 't-pf.json', forge_outbox)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        # The notify going to Beacon should carry the new field.
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify_data = json.loads(notifies[0].read_text())
        self.assertEqual(notify_data['forge_session_id'],
                         'forge-preflight-session-XYZ')

    def test_full_resume_cascade_one_clarification_round(self):
        """End-to-end: Forge CLARIFY_REQUEST → notify to Beacon (carries
        forge_session_id) → simulate Beacon's outbox (with forge_session_id
        propagated, as inbox_watcher._build_outbox would do in production)
        → continuation envelope routes back to Forge on the ORIGINAL task_id
        with resume_session_id populated. No doubled-prefix file."""
        # Round 1: Forge clarifies.
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "task-cascade", "question": "Q?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        forge_outbox = _good_outbox(
            agent='forge',
            source='beacon',
            task_id='task-cascade',
            result=marker,
            clarification_count=0,
            max_clarifications=3,
            claude_session_id='forge-sess-cascade',
            target_repo='ourliberty-agent-core',
            branch='forge/task-cascade',
        )
        f1 = self._write_outbox('forge', 'task-cascade-r1.json', forge_outbox)
        on.process_outbox(f1)

        # Confirm Beacon got a notify with forge_session_id.
        beacon_notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(beacon_notifies), 1)
        beacon_notify = json.loads(beacon_notifies[0].read_text())
        self.assertEqual(beacon_notify['forge_session_id'],
                         'forge-sess-cascade')
        self.assertEqual(beacon_notify['clarification_count'], 1)

        # Round 2: simulate Beacon's outbox (as inbox_watcher._build_outbox
        # would emit it after Beacon processed the notify). Critically, the
        # outbox carries forge_session_id forward via the envelope_fields
        # propagation that task-25 added to _build_outbox.
        beacon_outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            source_task_file=str(beacon_notifies[0]),
            task_id='notify-task-cascade',
            result='Answer to the clarification question — meaningful guidance text.',
            clarification_count=1,
            max_clarifications=3,
            forge_session_id='forge-sess-cascade',  # propagated by _build_outbox
            target_repo='ourliberty-agent-core',
            branch='forge/task-cascade',
        )
        f2 = self._write_outbox('beacon', 'notify-task-cascade.json',
                                beacon_outbox)
        result = on.process_outbox(f2)
        self.assertEqual(result, 'clarification-resume-dispatched')

        # Continuation envelope landed on Forge's inbox keyed on the ORIGINAL.
        forge_inbox = on.INBOXES_ROOT / 'forge'
        resumes = list(forge_inbox.glob('resume-*.json'))
        self.assertEqual(len(resumes), 1)
        envelope = json.loads(resumes[0].read_text())
        self.assertEqual(envelope['task_id'], 'task-cascade')
        self.assertEqual(envelope['resume_session_id'], 'forge-sess-cascade')
        self.assertEqual(envelope['phase'], 'preflight')
        self.assertEqual(envelope['source'], 'beacon-clarification')

        # No doubled-prefix file anywhere across the cascade.
        for agent in on.AGENT_IDS:
            self.assertEqual(
                list((on.INBOXES_ROOT / agent).glob('notify-notify-*.json')),
                [],
            )


class ClassifyForgeMarkerTest(unittest.TestCase):
    """Unit test the _classify_forge_marker helper in isolation."""

    def test_no_marker_returns_none(self):
        decision = on._classify_forge_marker({
            'agent': 'forge', 'result': 'No markers in this text.',
        })
        self.assertIsNone(decision)

    def test_empty_result_returns_none(self):
        self.assertIsNone(on._classify_forge_marker({'agent': 'forge', 'result': ''}))
        self.assertIsNone(on._classify_forge_marker({'agent': 'forge'}))

    def test_proceed_returns_decision(self):
        result = (
            '=== PROCEED ===\n'
            '{"task_id": "t-1", "preflight_summary": "ok"}\n'
            '=== END_PROCEED ==='
        )
        decision = on._classify_forge_marker({
            'agent': 'forge', 'result': result, 'task_id': 't-1',
        })
        self.assertEqual(decision['marker_type'], 'proceed')
        self.assertEqual(decision['intent'], 'ack-proceed')
        self.assertEqual(decision['notify_source'], 'forge-result')
        self.assertIsNone(decision['next_clarification_count'])

    def test_clarify_under_budget(self):
        result = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-1", "question": "Q?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        decision = on._classify_forge_marker({
            'agent': 'forge',
            'result': result,
            'clarification_count': 0,
            'max_clarifications': 3,
        })
        self.assertEqual(decision['marker_type'], 'clarify_request')
        self.assertEqual(decision['intent'], 'clarify')
        self.assertEqual(decision['notify_source'], 'forge-question')
        self.assertEqual(decision['next_clarification_count'], 1)

    def test_clarify_at_budget_becomes_exhausted(self):
        result = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-1", "question": "Last Q?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        decision = on._classify_forge_marker({
            'agent': 'forge',
            'result': result,
            'clarification_count': 3,
            'max_clarifications': 3,
        })
        # Converted to clarification-exhausted (specific, not generic reject)
        self.assertEqual(decision['marker_type'], 'clarify_request')
        self.assertEqual(decision['intent'], 'clarification-exhausted')
        self.assertEqual(decision['notify_source'], 'forge-result')
        # The reason carries the question text for context
        self.assertIn('Last Q?', decision['intent_kwargs']['reason'])

    def test_marker_task_id_mismatch_raises_malformed(self):
        # 4b discipline: marker.task_id MUST match envelope.task_id.
        # The 4a smoke caught a drift where Forge emitted a different id;
        # 4b raises MalformedForgeMarker so the cascade re-asks Forge.
        result = (
            '=== PROCEED ===\n'
            '{"task_id": "drifted-id", "preflight_summary": "ok"}\n'
            '=== END_PROCEED ==='
        )
        with self.assertRaises(on.fph.MalformedForgeMarker) as cm:
            on._classify_forge_marker({
                'agent': 'forge', 'result': result, 'task_id': 'envelope-id',
            })
        self.assertIn('drifted-id', str(cm.exception))
        self.assertIn('envelope-id', str(cm.exception))

    def test_marker_task_id_match_succeeds(self):
        result = (
            '=== PROCEED ===\n'
            '{"task_id": "matched", "preflight_summary": "ok"}\n'
            '=== END_PROCEED ==='
        )
        decision = on._classify_forge_marker({
            'agent': 'forge', 'result': result, 'task_id': 'matched',
        })
        self.assertEqual(decision['marker_type'], 'proceed')

    def test_marker_without_task_id_field_passes(self):
        # Marker payload may legitimately omit task_id (CLARIFY_REQUEST with
        # only a "question" field would be invalid per the schema, but the
        # mismatch check is permissive when the marker side is absent).
        result = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "matched", "question": "Q?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        # Envelope has no task_id — mismatch check is skipped.
        decision = on._classify_forge_marker({
            'agent': 'forge', 'result': result,
            'clarification_count': 0, 'max_clarifications': 3,
        })
        self.assertIsNotNone(decision)


class BuildPhaseDispatchTest(unittest.TestCase):
    """Phase D3 commit 4b: PROCEED marker → build-phase task to Forge."""

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
        self._rv_models_path = rv.MODELS_CONFIG_PATH
        rv.REPO_ROOT = self._root / 'repo'
        rv.MODELS_CONFIG_PATH = rv.REPO_ROOT / 'config' / 'agent-models.json'
        # Seed a realistic agent-models.json so check_target_repo exercises
        # the production allow-list shape (forge: ourliberty-agent-core).
        # Without this, the test passes via the fail-open path and misses
        # any regression that breaks the realistic flow.
        rv.MODELS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rv.MODELS_CONFIG_PATH.write_text(json.dumps({
            'agents': {
                'forge': {
                    'worktree_enabled': True,
                    'allowed_repos': ['ourliberty-agent-core'],
                },
            },
        }))
        rv.invalidate_cache()
        rv.invalidate_models_cache()
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.MODELS_CONFIG_PATH = self._rv_models_path
        rv.invalidate_cache()
        rv.invalidate_models_cache()
        self._tmp.cleanup()

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _forge_proceed_outbox(self, **overrides):
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "t-pf", "preflight_summary": "Will edit watchdog doc."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-pf',
            claude_session_id='sess-preflight-xyz',
            result=f'Read the spec. Ready to act.\n\n{marker}',
        )
        outbox.update(overrides)
        return outbox

    def test_proceed_writes_build_phase_task_to_forge(self):
        outbox = self._forge_proceed_outbox(target_repo='ourliberty-agent-core')
        f = self._write_outbox('forge', 't-pf.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        # Beacon still gets the ack-proceed notify (existing 4a behavior).
        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('notify-*.json')
        )
        self.assertEqual(len(beacon_notifies), 1)

        # New 4b: Forge ALSO gets a build-phase task.
        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1)
        build_data = json.loads(forge_builds[0].read_text())
        self.assertEqual(build_data['phase'], 'build')
        self.assertEqual(build_data['source'], 'beacon')
        self.assertEqual(build_data['session_id'], 'sess-preflight-xyz')
        self.assertEqual(build_data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(build_data['task_id'], 't-pf')
        self.assertEqual(build_data['dispatched_by'], 'outbox-notifier')
        self.assertIn('Build phase', build_data['prompt'])

    def test_proceed_without_session_id_skips_build_phase(self):
        outbox = self._forge_proceed_outbox(
            claude_session_id=None,
            target_repo='ourliberty-agent-core',
        )
        f = self._write_outbox('forge', 't-pf-nosid.json', outbox)

        result = on.process_outbox(f)
        # Notify-to-Beacon still happens; build-phase dispatch skipped.
        self.assertEqual(result, 'notified-marker')

        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('notify-*.json')
        )
        self.assertEqual(len(beacon_notifies), 1)
        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 0)

    def test_proceed_with_branch_propagates_to_build_task(self):
        outbox = self._forge_proceed_outbox(
            target_repo='ourliberty-agent-core',
            branch='forge/watchdog-fix-001',
        )
        f = self._write_outbox('forge', 't-pf-branch.json', outbox)

        on.process_outbox(f)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1)
        build_data = json.loads(forge_builds[0].read_text())
        self.assertEqual(build_data['branch'], 'forge/watchdog-fix-001')

    def test_proceed_with_max_clarifications_propagates(self):
        outbox = self._forge_proceed_outbox(
            target_repo='ourliberty-agent-core',
            max_clarifications=5,
        )
        f = self._write_outbox('forge', 't-pf-mc.json', outbox)
        on.process_outbox(f)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        build_data = json.loads(forge_builds[0].read_text())
        self.assertEqual(build_data['max_clarifications'], 5)

    def test_clarify_request_does_not_trigger_build_phase(self):
        # Only PROCEED triggers build phase. CLARIFY/REJECT do not.
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-clr", "question": "Which file?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-clr',
            claude_session_id='sess-clr',
            clarification_count=0, max_clarifications=3,
            target_repo='ourliberty-agent-core',
            result=f'Need more info.\n\n{marker}',
        )
        f = self._write_outbox('forge', 't-clr.json', outbox)

        on.process_outbox(f)

        # Beacon gets the clarify notify; Forge inbox stays empty.
        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 0)

    def test_reject_does_not_trigger_build_phase(self):
        marker = (
            '=== REJECT ===\n'
            '{"task_id": "t-rej", "reason": "Spec impossible."}\n'
            '=== END_REJECT ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-rej',
            target_repo='ourliberty-agent-core',
            result=f'Cannot proceed.\n\n{marker}',
        )
        f = self._write_outbox('forge', 't-rej.json', outbox)

        on.process_outbox(f)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 0)

    def test_marker_task_id_mismatch_dead_letters_no_build_phase(self):
        # Forge emits PROCEED with a drifted marker task_id → marker-error
        # cascade fires, no build-phase task gets written.
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "drifted", "preflight_summary": "x"}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='envelope-id',
            claude_session_id='sess-1',
            target_repo='ourliberty-agent-core',
            result=f'Reasoning.\n\n{marker}',
        )
        f = self._write_outbox('forge', 'envelope-id.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 0)

    def test_target_repo_outside_allow_list_blocks_build_dispatch(self):
        # If a PROCEED outbox carries a target_repo that's NOT in forge's
        # allowed_repos, safe_write_inbox in _dispatch_build_phase raises
        # RoutingDenied. The notify-to-Beacon still goes through; the
        # build-phase dispatch is logged as a failure.
        outbox = self._forge_proceed_outbox(
            target_repo='unauthorized-repo',
        )
        f = self._write_outbox('forge', 't-pf-bad-repo.json', outbox)

        result = on.process_outbox(f)
        # Notify still succeeds (target_repo doesn't gate notify writes
        # to Beacon — Beacon has no allowed_repos).
        self.assertEqual(result, 'notified-marker')

        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('notify-*.json')
        )
        self.assertEqual(len(beacon_notifies), 1)
        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 0)

    def test_build_dispatch_idempotent_on_existing_file(self):
        # Simulate a notifier crash + restart: the same preflight outbox
        # is processed twice. The second pass should NOT write a duplicate
        # build task.
        outbox = self._forge_proceed_outbox(target_repo='ourliberty-agent-core')
        f1 = self._write_outbox('forge', 't-pf-idem.json', outbox)
        on.process_outbox(f1)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1)

        # Second pass — write the same preflight outbox back and process.
        f2 = self._write_outbox('forge', 't-pf-idem.json', outbox)
        on.process_outbox(f2)
        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1, 'duplicate build task written')

    def test_pr_title_and_body_propagate_to_build_task(self):
        outbox = self._forge_proceed_outbox(
            target_repo='ourliberty-agent-core',
            pr_title='fix(watchdog): clarify enabled flag in docs',
            pr_body='## Summary\nDocumentation fix.',
        )
        f = self._write_outbox('forge', 't-pf-prfields.json', outbox)
        on.process_outbox(f)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1)
        build_data = json.loads(forge_builds[0].read_text())
        self.assertEqual(
            build_data['pr_title'],
            'fix(watchdog): clarify enabled flag in docs',
        )
        self.assertEqual(build_data['pr_body'], '## Summary\nDocumentation fix.')
        self.assertIn('PR title:', build_data['prompt'])


# ============================================================================
# D3.5 commit 5a — Mirror review marker pipeline + preflight-discipline gate
# ============================================================================


PR_URL_FIXTURE = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'


def _mirror_pass_marker(task_id='t-rev', summary='AC coverage clean.'):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL_FIXTURE, 'summary': summary,
    })
    return f'=== REVIEW_PASS ===\n{payload}\n=== END_REVIEW_PASS ==='


def _mirror_revision_marker(
    task_id='t-rev', severity='medium', confidence='high', findings=None,
):
    if findings is None:
        findings = [{
            'file': 'scripts/foo.py', 'line_range': 'L12-L15',
            'severity': 'medium', 'description': 'Missing validation.',
        }]
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL_FIXTURE,
        'findings': findings, 'severity': severity, 'confidence': confidence,
    })
    return f'=== REVIEW_REVISION ===\n{payload}\n=== END_REVIEW_REVISION ==='


def _mirror_escalate_marker(
    task_id='t-rev', severity='high', confidence='high',
    reason='Spec mismatch; needs replan.',
):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL_FIXTURE,
        'reason': reason, 'severity': severity, 'confidence': confidence,
    })
    return f'=== REVIEW_ESCALATE ===\n{payload}\n=== END_REVIEW_ESCALATE ==='


def _mirror_emergency_marker(
    task_id='t-rev',
    reason='Diff adds plaintext credentials.',
    evidence='+    "secret": "abc123"',
):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL_FIXTURE,
        'reason': reason, 'evidence': evidence,
    })
    return (
        f'=== REVIEW_EMERGENCY_HALT ===\n{payload}\n'
        f'=== END_REVIEW_EMERGENCY_HALT ==='
    )


def _mirror_outbox_body(marker_text='', **overrides):
    """Synthetic Mirror outbox with optional marker embedded in result.

    `source` defaults to 'beacon' because the review-request was dispatched
    BY beacon (logically). task_id defaults to 't-rev'. `phase: 'review'`
    is propagated so the envelope matches what `_dispatch_mirror_review`
    actually writes.
    """
    base = _good_outbox(
        agent='mirror', source='beacon', task_id='t-rev', phase='review',
        result=(
            'Reviewed the PR diff. Coverage clean.\n\n' + marker_text
            if marker_text else 'Reviewed but emitted no marker.'
        ),
    )
    base.update(overrides)
    return base


class ClassifyMirrorMarkerTest(unittest.TestCase):
    """Unit tests for `_classify_mirror_marker` — routing decision shape."""

    def test_review_pass_intent(self):
        data = _mirror_outbox_body(_mirror_pass_marker())
        decision = on._classify_mirror_marker(data)
        self.assertIsNotNone(decision)
        self.assertEqual(decision['marker_type'], 'review_pass')
        self.assertEqual(decision['intent'], 'review-pass')
        self.assertEqual(decision['notify_source'], 'mirror-result')
        self.assertFalse(decision['auto_promoted'])
        self.assertEqual(decision['intent_kwargs']['pr_url'], PR_URL_FIXTURE)

    def test_review_revision_high_confidence_routes_as_revision(self):
        data = _mirror_outbox_body(
            _mirror_revision_marker(confidence='high'),
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_revision')
        self.assertEqual(decision['intent'], 'review-revision')
        self.assertFalse(decision['auto_promoted'])
        self.assertEqual(decision['intent_kwargs']['finding_count'], 1)
        self.assertEqual(decision['intent_kwargs']['confidence'], 'high')

    def test_review_revision_low_confidence_auto_promotes_to_escalate(self):
        data = _mirror_outbox_body(
            _mirror_revision_marker(confidence='low'),
        )
        decision = on._classify_mirror_marker(data)
        # Marker type stays revision (Mirror's verdict), but intent escalates.
        self.assertEqual(decision['marker_type'], 'review_revision')
        self.assertEqual(decision['intent'], 'review-escalate')
        self.assertTrue(decision['auto_promoted'])
        # Reason from build_auto_promote_reason should be in intent_kwargs
        self.assertIn('reason', decision['intent_kwargs'])
        self.assertIn('low', decision['intent_kwargs']['reason'])

    def test_review_escalate_intent(self):
        data = _mirror_outbox_body(_mirror_escalate_marker())
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_escalate')
        self.assertEqual(decision['intent'], 'review-escalate')
        self.assertEqual(decision['intent_kwargs']['severity'], 'high')

    def test_review_emergency_halt_intent(self):
        data = _mirror_outbox_body(_mirror_emergency_marker())
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_emergency_halt')
        self.assertEqual(decision['intent'], 'review-emergency-halt')
        self.assertIn('credentials', decision['intent_kwargs']['reason'])
        self.assertIn('secret', decision['intent_kwargs']['evidence'])

    def test_no_marker_returns_none(self):
        data = _mirror_outbox_body(result='Just chat output, no marker.')
        self.assertIsNone(on._classify_mirror_marker(data))

    def test_envelope_task_id_mismatch_raises(self):
        # Marker says t-other but envelope says t-rev.
        marker = _mirror_pass_marker(task_id='t-other')
        data = _mirror_outbox_body(marker, task_id='t-rev')
        with self.assertRaises(on.mrh.MalformedMirrorMarker) as ctx:
            on._classify_mirror_marker(data)
        self.assertIn('task_id', str(ctx.exception))

    def test_malformed_marker_propagates(self):
        # Missing required field (no summary on PASS).
        bad_payload = json.dumps({'task_id': 't-rev', 'pr_url': PR_URL_FIXTURE})
        marker = (
            f'=== REVIEW_PASS ===\n{bad_payload}\n=== END_REVIEW_PASS ==='
        )
        data = _mirror_outbox_body(marker)
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)

    def test_multi_marker_propagates(self):
        marker = _mirror_pass_marker() + '\n' + _mirror_revision_marker()
        data = _mirror_outbox_body(marker)
        with self.assertRaises(on.mrh.MultipleMirrorMarkers):
            on._classify_mirror_marker(data)


class MirrorMarkerRoutingTest(unittest.TestCase):
    """process_outbox integration: Mirror outbox → Beacon notify, marker-error
    cascade on malformed markers."""

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
        # D3.5 5d — block any real `gh pr merge` shell-out during tests.
        # Mirror PASS markers in this class now trigger _auto_merge_pr in
        # production; without this override, integration tests would
        # subprocess.run against the real GitHub repo. Default override
        # returns a synthetic `merged` outcome; individual tests can
        # replace _auto_merge_override with their own outcome shape.
        self._auto_merge_calls: list[tuple[str, str]] = []
        def _default_auto_merge(pr_url, task_id):
            self._auto_merge_calls.append((pr_url, task_id))
            return {
                'merge_outcome': 'merged',
                'merge_reason': 'squash-merged + branch deleted (test override)',
                'pr_number': 42,
                'repo_coords': 'test-owner/test-repo',
            }
        self._original_auto_merge_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = _default_auto_merge
        # Reroute larry_alerts file targets into the tmpdir so the
        # EMERGENCY_HALT priority DM + cost-budget DMs don't write to
        # ~/agents during tests.
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
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
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        on._AUTO_MERGE_FN_OVERRIDE = self._original_auto_merge_override
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_mirror_outbox(self, name, body):
        outbox_dir = on.OUTBOXES_ROOT / 'mirror'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _get_beacon_notify(self):
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1, f'expected 1 beacon notify, got {len(notifies)}')
        return json.loads(notifies[0].read_text())

    def test_review_pass_notifies_beacon(self):
        body = _mirror_outbox_body(_mirror_pass_marker())
        f = self._write_mirror_outbox('t-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['source'], 'mirror-result')
        self.assertEqual(data['intent'], 'review-pass')
        self.assertIn(PR_URL_FIXTURE, data['prompt'])
        self.assertIn('APPROVED', data['prompt'])

    def test_review_revision_notifies_beacon_with_finding_count(self):
        body = _mirror_outbox_body(_mirror_revision_marker(confidence='high'))
        f = self._write_mirror_outbox('t-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['intent'], 'review-revision')
        self.assertIn('1 finding', data['prompt'])

    def test_review_revision_low_confidence_promotes_to_escalate(self):
        body = _mirror_outbox_body(_mirror_revision_marker(confidence='low'))
        f = self._write_mirror_outbox('t-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        # Verified: low-confidence revision routes as escalate.
        self.assertEqual(data['intent'], 'review-escalate')
        self.assertIn('ESCALATED', data['prompt'])

    def test_review_escalate_notifies_beacon(self):
        body = _mirror_outbox_body(_mirror_escalate_marker())
        f = self._write_mirror_outbox('t-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['intent'], 'review-escalate')
        self.assertIn('Spec mismatch', data['prompt'])

    def test_review_emergency_halt_notifies_beacon(self):
        body = _mirror_outbox_body(_mirror_emergency_marker())
        f = self._write_mirror_outbox('t-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['intent'], 'review-emergency-halt')
        self.assertIn('credentials', data['prompt'])
        # D3.5 5d — REVIEW_EMERGENCY_HALT now TRIPS the halt-file.
        self.assertTrue(on.EMERGENCY_HALT_FLAG.exists())
        envelope = json.loads(on.EMERGENCY_HALT_FLAG.read_text())
        self.assertEqual(envelope['activated_by'], 'mirror-marker')
        self.assertEqual(envelope['task_id'], 't-rev')
        self.assertIn('credentials', envelope['reason'])

    def test_malformed_marker_dead_letters_to_mirror(self):
        # Missing required field — PASS without `summary`.
        bad_payload = json.dumps({'task_id': 't-rev', 'pr_url': PR_URL_FIXTURE})
        marker = (
            f'=== REVIEW_PASS ===\n{bad_payload}\n=== END_REVIEW_PASS ==='
        )
        body = _mirror_outbox_body(marker)
        f = self._write_mirror_outbox('t-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        # No notify to Beacon
        self.assertEqual(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json')), [],
        )
        # marker-error notify lands in Mirror's inbox
        mirror_notifies = list(
            (on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json')
        )
        self.assertEqual(len(mirror_notifies), 1)
        data = json.loads(mirror_notifies[0].read_text())
        self.assertEqual(data['intent'], 'marker-error')
        self.assertEqual(data['marker_error_count'], 1)
        self.assertIn('summary', data['prompt'])

    def test_no_marker_falls_through_to_default_routing(self):
        # Mirror's chat-mode outputs (no marker) should NOT trigger marker
        # routing — they go via the default path (notify Beacon as
        # mirror-result with intent=result-notification).
        body = _mirror_outbox_body(
            result=('Reviewed but reporting back in chat mode without '
                    'a marker — Larry asked me a question, not a review.'),
        )
        f = self._write_mirror_outbox('t-chat.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')
        data = self._get_beacon_notify()
        self.assertEqual(data['source'], 'mirror-result')
        self.assertEqual(data['intent'], 'result-notification')


class PreflightDisciplineGateTest(unittest.TestCase):
    """5a's deferred-from-4b runtime gate: phase=preflight WITHOUT a marker
    must dead-letter back to Forge via the marker-error cascade."""

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

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def test_preflight_without_marker_dead_letters_to_forge(self):
        # phase=preflight, but Forge wrote a build-style response with no
        # marker block. This is the failure shape the gate exists to catch.
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-bad-pf',
            phase='preflight',
            result=('I read the spec and started editing the file '
                    'directly — committed the change to a branch named '
                    'fix/typo. PR opened: https://github.com/Larry-Yatch/'
                    'ourliberty-agent-core/pull/99'),
        )
        f = self._write_outbox('forge', 't-bad-pf.json', outbox)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        # marker-error notify lands in Forge's inbox with the sharper prompt
        forge_notifies = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(forge_notifies), 1)
        data = json.loads(forge_notifies[0].read_text())
        self.assertEqual(data['intent'], 'marker-error')
        self.assertIn('preflight', data['prompt'].lower())
        self.assertIn('decides', data['prompt'].lower())

    def test_build_phase_without_marker_falls_through_normally(self):
        # phase=build, no marker — this is EXPECTED (build responses don't
        # carry markers per Forge's CLAUDE.md). The gate must not fire here.
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-build-ok',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/t-build-ok',
            result=('PR opened: https://github.com/Larry-Yatch/'
                    'ourliberty-agent-core/pull/100\n\n'
                    'Fixed the typo per spec.'),
        )
        f = self._write_outbox('forge', 't-build-ok.json', outbox)
        result = on.process_outbox(f)
        # Default routing path (no marker, no preflight gate fire) →
        # notifies Beacon with the build result. ALSO dispatches a
        # review-request to Mirror (next test class verifies that side).
        self.assertEqual(result, 'notified')

    def test_preflight_with_marker_does_not_fire_gate(self):
        # The gate is only for phase=preflight WITHOUT a marker. Preflight
        # WITH a marker takes the normal marker-driven routing path.
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "t-pf-ok", "preflight_summary": "Will fix typo."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-pf-ok',
            phase='preflight', result=marker,
        )
        f = self._write_outbox('forge', 't-pf-ok.json', outbox)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

    def test_no_phase_field_does_not_fire_gate(self):
        # Legacy Forge outboxes (pre-D3 dispatches still in flight) may
        # have no phase field. The gate must not fire on those — only
        # explicit phase=preflight + no marker triggers it.
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='t-legacy',
            result='Some plain text response from a legacy dispatch.',
        )
        outbox.pop('phase', None)
        f = self._write_outbox('forge', 't-legacy.json', outbox)
        result = on.process_outbox(f)
        # No marker, no phase → default routing path (Beacon notify).
        self.assertEqual(result, 'notified')


class MirrorReviewDispatchTest(unittest.TestCase):
    """Forge build-phase outbox carrying 'PR opened:' triggers a review-
    request task to Mirror's inbox (parallel to _dispatch_build_phase)."""

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

    def _build_outbox(self, **overrides):
        base = _good_outbox(
            agent='forge', source='beacon', task_id='t-built',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/t-built',
            result=('PR opened: https://github.com/Larry-Yatch/'
                    'ourliberty-agent-core/pull/77\n\n'
                    'Implemented the fix; tests pass.'),
        )
        base.update(overrides)
        return base

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def test_pr_opened_dispatches_review_request_to_mirror(self):
        body = self._build_outbox()
        f = self._write_outbox('forge', 't-built.json', body)
        on.process_outbox(f)
        # review-request task in Mirror's inbox
        review_tasks = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(review_tasks), 1)
        data = json.loads(review_tasks[0].read_text())
        self.assertEqual(data['task_id'], 't-built')
        self.assertEqual(data['source'], 'beacon')
        self.assertEqual(data['phase'], 'review')
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/t-built')
        self.assertIn('pull/77', data['pr_url'])
        self.assertEqual(data['revision_count'], 0)
        # max_revisions sourced from mirror_review_handler default
        self.assertEqual(data['max_revisions'], on.mrh.DEFAULT_MAX_REVISIONS)
        self.assertEqual(data['dispatched_by'], 'outbox-notifier')

    def test_pr_opened_also_notifies_beacon(self):
        # The review dispatch is ADDITIVE — Beacon still gets her notify
        # via the default routing path so she can journal "PR opened."
        body = self._build_outbox()
        f = self._write_outbox('forge', 't-built.json', body)
        on.process_outbox(f)
        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('notify-*.json')
        )
        self.assertEqual(len(beacon_notifies), 1)
        data = json.loads(beacon_notifies[0].read_text())
        self.assertEqual(data['source'], 'forge-result')

    def test_build_without_pr_url_skips_review_dispatch(self):
        # If Forge's build response doesn't start with "PR opened:", no
        # review-request fires. (Beacon still gets her notify; she sees
        # the missing PR URL and decides what to do.)
        body = self._build_outbox(
            result='Tried to build but hit a compile error; need clarification.',
        )
        f = self._write_outbox('forge', 't-failed-build.json', body)
        on.process_outbox(f)
        review_tasks = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(review_tasks, [])

    def test_build_without_target_repo_skips_review_dispatch(self):
        # Mirror's worktree gate (now active per 5a's agent-models.json)
        # requires target_repo. If Forge's build envelope somehow lost
        # target_repo, dispatching without it would dead-letter back to
        # her — instead, log + skip and let Larry investigate.
        body = self._build_outbox()
        body.pop('target_repo', None)
        f = self._write_outbox('forge', 't-no-repo.json', body)
        on.process_outbox(f)
        review_tasks = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(review_tasks, [])

    def test_preflight_outbox_does_not_trigger_review_dispatch(self):
        # The trigger is phase=build specifically. A preflight outbox even
        # with "PR opened:" in the result text (Forge fast-pathed) should
        # NOT dispatch a review — the preflight-discipline gate catches
        # that case and dead-letters to Forge.
        body = self._build_outbox(
            phase='preflight',
            result='PR opened: https://github.com/Larry-Yatch/'
                   'ourliberty-agent-core/pull/77\n\nI fast-pathed.',
        )
        f = self._write_outbox('forge', 't-fastpath.json', body)
        result = on.process_outbox(f)
        # Gate fires → marker-error, NOT review dispatch
        self.assertEqual(result, 'marker-error')
        review_tasks = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(review_tasks, [])

    def test_idempotency_duplicate_outbox_does_not_double_dispatch(self):
        # If the notifier crashes between dispatch and archive of a build
        # outbox, re-processing the same outbox should NOT write a second
        # review-request (Mirror would otherwise spawn a duplicate review).
        body = self._build_outbox()
        f = self._write_outbox('forge', 't-built.json', body)
        on.process_outbox(f)
        # Simulate re-processing: re-write the outbox + run again. (In
        # production this happens when the notifier crashes after the
        # dispatch write but before the archive move.)
        f2 = self._write_outbox('forge', 't-built.json', body)
        on.process_outbox(f2)
        review_tasks = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        # Still exactly one review-request, despite two process_outbox calls.
        self.assertEqual(len(review_tasks), 1)

    def test_pr20_marker_error_retry_build_dispatches_review(self):
        """5d-followup-2: post-marker-error-retry build envelope where Forge's
        result narrates status bullets and puts `PR opened:` on its own line
        at the END of the result (PR #20's verbatim shape from
        outboxes/forge/.archive/beacon-specs-ledger-pulsei-001.2.json) now
        dispatches the Mirror review-request. Prior \\A anchor silently
        dropped this — Beacon got her notify but Mirror never started review.
        """
        # Verbatim narrative-then-URL shape from the failed PR #20 dispatch.
        # The build envelope carries the standard `phase: build` + `agent:
        # forge` + `source: beacon` regardless of whether it followed a
        # marker-error retry — by the time process_outbox sees it, the
        # retry-origin metadata is irrelevant to this code path.
        body = self._build_outbox(
            task_id='beacon-specs-ledger-pulsei-001',
            branch='forge/beacon-specs-ledger-pulsei-001',
            result=(
                'Build phase contract is already satisfied on this branch:\n'
                '- Branch `forge/beacon-specs-ledger-pulsei-001` pushed to origin\n'
                '- Commit `62edc9c`: `docs: add specs for Ledger (CFO agent) '
                'and Pulse Check I (optimization mode)`\n'
                '- Files added: `agents/beacon/specs/ledger.md` (+151), '
                '`agents/beacon/specs/pulse-check-i.md` (+94)\n'
                '- PR #20 open against main, awaiting Mirror\n\n'
                'PR opened: https://github.com/Larry-Yatch/'
                'ourliberty-agent-core/pull/20'
            ),
        )
        f = self._write_outbox(
            'forge', 'beacon-specs-ledger-pulsei-001.json', body,
        )
        on.process_outbox(f)
        review_tasks = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-*.json')
        )
        self.assertEqual(len(review_tasks), 1)
        review = json.loads(review_tasks[0].read_text())
        self.assertEqual(review['task_id'], 'beacon-specs-ledger-pulsei-001')
        self.assertEqual(
            review['pr_url'],
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/20',
        )
        self.assertEqual(review['phase'], 'review')
        self.assertEqual(review['source'], 'beacon')
        # Default routing notify to Beacon also fires (additive dispatch)
        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('notify-*.json')
        )
        self.assertEqual(len(beacon_notifies), 1)


# ============================================================================
# D3.5 commit 5a — Post-review fixes (C-1, M-1, M-2, M-3, m-2)
# ============================================================================


class MarkerOutputForPromptTest(unittest.TestCase):
    """C-1 fix: _marker_output_for_prompt must produce real body text for
    each of Mirror's 4 marker types (not '(no reason)' fallback)."""

    def _decision(self, marker_type, payload, intent_kwargs=None):
        return {
            'marker_type': marker_type,
            'payload': payload,
            'intent': on.mrh.derive_intent(marker_type),
            'notify_source': 'mirror-result',
            'intent_kwargs': intent_kwargs or {},
            'auto_promoted': False,
            'next_clarification_count': None,
        }

    def test_review_pass_body_has_summary(self):
        payload = {
            'task_id': 't-rev', 'pr_url': PR_URL_FIXTURE,
            'summary': 'AC coverage clean across the board.',
        }
        body = on._marker_output_for_prompt({}, self._decision(
            'review_pass', payload,
        ))
        self.assertIn('coverage clean', body)
        self.assertNotEqual(body, '(no reason)')

    def test_review_revision_body_has_finding_summary(self):
        payload = {
            'task_id': 't-rev', 'pr_url': PR_URL_FIXTURE,
            'findings': [
                {'file': 'a.py', 'line_range': 'L10',
                 'severity': 'medium',
                 'description': 'Missing input validation on path arg.'},
                {'file': 'b.py', 'line_range': 'L22',
                 'severity': 'low', 'description': 'Variable name unclear.'},
            ],
            'severity': 'medium', 'confidence': 'high',
        }
        body = on._marker_output_for_prompt({}, self._decision(
            'review_revision', payload,
        ))
        self.assertIn('2 finding', body)
        self.assertIn('Missing input validation', body)

    def test_review_escalate_body_has_reason(self):
        payload = {
            'task_id': 't-rev', 'pr_url': PR_URL_FIXTURE,
            'reason': 'Implemented X, spec said Y.',
            'severity': 'high', 'confidence': 'high',
        }
        body = on._marker_output_for_prompt({}, self._decision(
            'review_escalate', payload,
        ))
        self.assertIn('Implemented X', body)

    def test_review_emergency_halt_body_has_reason_and_evidence(self):
        payload = {
            'task_id': 't-rev', 'pr_url': PR_URL_FIXTURE,
            'reason': 'Plaintext credentials in diff.',
            'evidence': '+    "aws_secret": "AKIA..."',
        }
        body = on._marker_output_for_prompt({}, self._decision(
            'review_emergency_halt', payload,
        ))
        self.assertIn('Plaintext credentials', body)
        self.assertIn('AKIA', body)


class DispatchIdempotencyInvalidCheckTest(unittest.TestCase):
    """M-1 fix: _dispatch_mirror_review + _dispatch_build_phase skip when
    the target file exists in .invalid/ (not just inbox or .archive/)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT', 'BLACKBOARD',
            'LOG_FILE', 'DEAD_LETTER_STATE', 'EMERGENCY_HALT_FLAG',
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
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        self._tmp.cleanup()

    def test_mirror_review_skips_when_in_invalid(self):
        # Plant a stale review-request in Mirror's .invalid/
        invalid_dir = on.INBOXES_ROOT / 'mirror' / '.invalid'
        invalid_dir.mkdir(parents=True, exist_ok=True)
        (invalid_dir / 'review-t-stuck.json').write_text('{}')

        data = {
            'task_id': 't-stuck',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-stuck',
        }
        on._dispatch_mirror_review(data, 'https://github.com/x/y/pull/1')
        # Should NOT have written a fresh review-request to the live inbox
        live = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(live, [])

    def test_build_phase_skips_when_in_invalid(self):
        invalid_dir = on.INBOXES_ROOT / 'forge' / '.invalid'
        invalid_dir.mkdir(parents=True, exist_ok=True)
        (invalid_dir / 'build-t-stuck.json').write_text('{}')

        data = {
            'task_id': 't-stuck',
            'claude_session_id': 'sess-abc',
            'target_repo': 'ourliberty-agent-core',
        }
        on._dispatch_build_phase(data)
        live = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(live, [])


class MirrorReviewMetadataPropagationTest(unittest.TestCase):
    """M-2 fix: _dispatch_mirror_review propagates pr_title/pr_body/
    max_clarifications when present on the source envelope."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT', 'BLACKBOARD',
            'LOG_FILE', 'DEAD_LETTER_STATE', 'EMERGENCY_HALT_FLAG',
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
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        self._tmp.cleanup()

    def test_pr_title_pr_body_max_clarifications_propagate(self):
        data = {
            'task_id': 't-meta',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-meta',
            'pr_title': 'fix(watchdog): clarify enabled flag',
            'pr_body': '## Summary\nDoc fix.',
            'max_clarifications': 5,
        }
        on._dispatch_mirror_review(data, 'https://github.com/x/y/pull/1')
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(review['pr_title'], 'fix(watchdog): clarify enabled flag')
        self.assertEqual(review['pr_body'], '## Summary\nDoc fix.')
        self.assertEqual(review['max_clarifications'], 5)

    def test_omitted_metadata_does_not_crash(self):
        # Forge envelopes that don't carry the metadata fields should still
        # dispatch successfully — the fields are optional.
        data = {
            'task_id': 't-bare',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-bare',
        }
        on._dispatch_mirror_review(data, 'https://github.com/x/y/pull/1')
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertNotIn('pr_title', review)
        self.assertNotIn('pr_body', review)


class MirrorMarkerErrorReplyChatIdTest(unittest.TestCase):
    """M-3 fix: _notify_mirror_marker_error propagates reply_chat_id so a
    Telegram-initiated review whose marker errors three-strikes still closes
    the chat thread via the eventual dead-letter."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT', 'BLACKBOARD',
            'LOG_FILE', 'DEAD_LETTER_STATE', 'EMERGENCY_HALT_FLAG',
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
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        self._tmp.cleanup()

    def test_reply_chat_id_propagates_into_marker_error_notify(self):
        data = {
            'agent': 'mirror', 'source': 'beacon',
            'task_id': 't-chat', 'reply_chat_id': 7998341473,
        }
        on._notify_mirror_marker_error(data, 'some marker parse error')
        notifies = list(
            (on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json')
        )
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertEqual(notify['reply_chat_id'], 7998341473)


class PrUrlRegexAnchoredTest(unittest.TestCase):
    """m-2 fix + 5d-followup-2 relaxation: _PR_URL_RE is anchored to start-
    of-LINE (re.MULTILINE) so a `PR opened:` URL on its own line wins from
    any position in the result, while a mid-paragraph stale URL still
    doesn't false-match. PR #20's narrative-then-URL build response shape
    is the canonical case that motivated the line-anchor relaxation."""

    def test_pr_url_at_start_extracted(self):
        result = (
            'PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/'
            'pull/77\n\nDetails follow.'
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/77',
        )

    def test_pr_url_with_leading_whitespace_extracted(self):
        result = (
            '   PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/'
            'pull/77\n\nDetails.'
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/77',
        )

    def test_pr_url_only_in_narrative_returns_none(self):
        # Forge discussed a stale URL mid-paragraph without ever putting
        # `PR opened:` at line-start. The line anchor must NOT match this —
        # the m-2 false-match protection is preserved across the relaxation.
        result = (
            'I considered re-using last week\'s branch where '
            'PR opened: https://github.com/x/y/pull/99 — but instead, '
            'I built fresh and ran into a compile error. No PR yet.'
        )
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

    def test_narrative_before_real_url_at_line_start_matches(self):
        # 5d-followup-2: Forge's narrative-then-URL shape (PR #20's actual
        # build response) now matches — the URL is on its own line at line
        # start, just preceded by status bullets. The strict-string anchor
        # used to silently drop this, breaking the Mirror review dispatch.
        result = (
            'Briefly considered https://github.com/x/y/pull/99 but rejected.\n'
            'PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/'
            'pull/77'
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/77',
        )

    def test_pr_20_envelope_shape_matches(self):
        # Verbatim shape of PR #20's archived Forge build outbox result.
        # Narrative paragraph with status bullets, then `PR opened: <url>`
        # on its own line at the end. This dispatch missed Mirror review
        # because of the start-of-string anchor; the fix must catch it.
        result = (
            'Build phase contract is already satisfied on this branch:\n'
            '- Branch `forge/beacon-specs-ledger-pulsei-001` pushed to origin\n'
            '- Commit `62edc9c`: `docs: add specs for Ledger (CFO agent) and '
            'Pulse Check I (optimization mode)`\n'
            '- Files added: `agents/beacon/specs/ledger.md` (+151), '
            '`agents/beacon/specs/pulse-check-i.md` (+94)\n'
            '- PR #20 open against main, awaiting Mirror\n\n'
            'PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/'
            'pull/20'
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/20',
        )

    def test_empty_result_returns_none(self):
        self.assertIsNone(on._extract_pr_url_from_build_result(''))
        self.assertIsNone(on._extract_pr_url_from_build_result(None))


# ============================================================================
# D3.5 5a-followup — Larry-DM-on-task-complete (Bug A + Gap B)
# ============================================================================


class MaybeDmLarryTest(unittest.TestCase):
    """_maybe_dm_larry appends a notification to larry-alerts.jsonl when
    intent is terminal-from-Larry's-perspective AND reply_chat_id is set."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        # larry_alerts owns its own paths; reroute them.
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        self._la = la

    def tearDown(self):
        for name, value in self._la_originals.items():
            setattr(self._la, name, value)
        self._tmp.cleanup()

    def _read_notifications(self):
        pending = self._la.read_pending(0)
        return [rec for _, rec in pending if rec.get('kind') == 'notification']

    def _decision(self, intent, marker_type=None, payload=None, intent_kwargs=None):
        return {
            'marker_type': marker_type or intent.replace('-', '_'),
            'payload': payload or {},
            'intent': intent,
            'notify_source': 'mirror-result',
            'intent_kwargs': intent_kwargs or {},
            'auto_promoted': False,
            'next_clarification_count': None,
        }

    def test_review_pass_with_chat_id_queues_notification(self):
        data = {
            'task_id': 't-pass', 'reply_chat_id': 7998341473,
            'agent': 'mirror',
        }
        decision = self._decision('review-pass', 'review_pass', payload={
            'task_id': 't-pass',
            'pr_url': 'https://github.com/x/y/pull/1',
            'summary': 'All clean.',
        })
        on._maybe_dm_larry(data, decision)
        notifs = self._read_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['intent'], 'review-pass')
        self.assertEqual(notifs[0]['chat_id'], 7998341473)
        self.assertEqual(notifs[0]['task_id'], 't-pass')
        self.assertIn('Mirror approved', notifs[0]['message'])
        self.assertIn('pull/1', notifs[0]['message'])
        self.assertIn('All clean', notifs[0]['message'])

    def test_review_revision_does_not_dm_in_5b(self):
        # D3.5 5b: REVIEW_REVISION is mid-chain (revision auto-dispatched to
        # Forge). Larry only gets DM on terminal intents — escalate (incl.
        # budget-exhausted downgrade), pass, emergency-halt, reject,
        # clarification-exhausted. Confirming the 5a behavior was changed.
        data = {'task_id': 't-rev', 'reply_chat_id': 7998341473, 'agent': 'mirror'}
        decision = self._decision('review-revision', 'review_revision', payload={
            'task_id': 't-rev',
            'pr_url': 'https://github.com/x/y/pull/2',
            'findings': [{'file': 'a'}, {'file': 'b'}, {'file': 'c'}],
            'severity': 'medium', 'confidence': 'high',
        }, intent_kwargs={
            'pr_url': 'https://github.com/x/y/pull/2',
            'finding_count': 3, 'severity': 'medium', 'confidence': 'high',
        })
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_review_escalate_renders_reason(self):
        data = {'task_id': 't-esc', 'reply_chat_id': 7998341473, 'agent': 'mirror'}
        decision = self._decision('review-escalate', 'review_escalate', payload={
            'task_id': 't-esc',
            'pr_url': 'https://github.com/x/y/pull/3',
            'reason': 'Spec mismatch.',
            'severity': 'high', 'confidence': 'high',
        }, intent_kwargs={
            'pr_url': 'https://github.com/x/y/pull/3',
            'reason': 'Spec mismatch.',
            'severity': 'high', 'confidence': 'high',
        })
        on._maybe_dm_larry(data, decision)
        notifs = self._read_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertIn('Spec mismatch', notifs[0]['message'])

    def test_review_emergency_does_not_dm_in_5d(self):
        # D3.5 5d: REVIEW_EMERGENCY_HALT is no longer in TERMINAL_DM_INTENTS.
        # The broadcast priority alert (kind: alert, fired from
        # `_trip_emergency_halt`) carries the recovery command + reaches
        # all authorized chats; a targeted closing DM on top would be a
        # duplicate notification with stale "decide whether to close
        # without merge" wording. Confirming the inversion vs 5a/5b/5c.
        data = {'task_id': 't-halt', 'reply_chat_id': 7998341473, 'agent': 'mirror'}
        decision = self._decision('review-emergency-halt', 'review_emergency_halt', payload={
            'task_id': 't-halt',
            'pr_url': 'https://github.com/x/y/pull/4',
            'reason': 'Plaintext credentials.',
            'evidence': '+    "aws_key": "AKIA..."',
        }, intent_kwargs={
            'pr_url': 'https://github.com/x/y/pull/4',
            'reason': 'Plaintext credentials.',
            'evidence': '+    "aws_key": "AKIA..."',
        })
        on._maybe_dm_larry(data, decision)
        # No notification queued — broadcast alert (fired separately by
        # _trip_emergency_halt) is the channel for EMERGENCY_HALT.
        self.assertEqual(self._read_notifications(), [])

    def test_non_terminal_intent_does_not_dm(self):
        # ack-proceed is mid-chain — Forge proceeded, Beacon journals,
        # nothing for Larry to do yet. No DM.
        data = {'task_id': 't-mid', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('ack-proceed', 'proceed')
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_clarify_intent_does_not_dm(self):
        # Clarifications are mid-chain (Beacon answers Forge); no Larry DM.
        data = {'task_id': 't-clar', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('clarify', 'clarify_request')
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_result_notification_does_not_dm(self):
        # Generic result-notification is the mid-chain catch-all; no DM.
        data = {'task_id': 't-result', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('result-notification')
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_missing_reply_chat_id_does_not_dm(self):
        # Autonomous Pulse-initiated runs have no originating chat.
        data = {'task_id': 't-auto', 'agent': 'mirror'}  # no reply_chat_id
        decision = self._decision('review-pass', 'review_pass', payload={
            'task_id': 't-auto', 'pr_url': 'x', 'summary': 'y',
        })
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_non_int_reply_chat_id_does_not_dm(self):
        # Defensive: a corrupted reply_chat_id (string, list, whatever)
        # gets logged and skipped — not propagated to a DM.
        data = {
            'task_id': 't-bad-chat', 'reply_chat_id': 'not-a-number',
            'agent': 'mirror',
        }
        decision = self._decision('review-pass', 'review_pass', payload={
            'task_id': 't-bad-chat', 'pr_url': 'x', 'summary': 'y',
        })
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_reject_intent_dms(self):
        # Forge rejected the spec at preflight — terminal for Larry.
        data = {'task_id': 't-rej', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('reject', 'reject', payload={
            'task_id': 't-rej', 'reason': 'Required file missing.',
        }, intent_kwargs={'reason': 'Required file missing.'})
        on._maybe_dm_larry(data, decision)
        notifs = self._read_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertIn('REJECTED', notifs[0]['message'])
        self.assertIn('Required file missing', notifs[0]['message'])

    def test_clarification_exhausted_dms(self):
        # Forge ran out of clarifications — terminal for Larry.
        data = {'task_id': 't-cx', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision(
            'clarification-exhausted', 'clarify_request',
            payload={'task_id': 't-cx', 'question': 'Final question?'},
            intent_kwargs={'reason': 'Final question?'},
        )
        on._maybe_dm_larry(data, decision)
        notifs = self._read_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertIn('exhausted', notifs[0]['message'])


# ============================================================================
# D3.5 5b — Forge↔Mirror revision loop
# ============================================================================


class RevisionLoopTest(unittest.TestCase):
    """End-to-end: Mirror REVISION → Forge revision dispatch → Forge revision
    outbox → Mirror re-review. Plus budget-exhaust + strict gate paths."""

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
        # larry_alerts paths for DM-related side checks
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        self._la = la
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(self._la, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _mirror_revision_outbox(self, **overrides):
        """Mirror outbox with a REVIEW_REVISION marker — the trigger for
        the 5b revision-loop dispatch."""
        marker = _mirror_revision_marker(
            task_id='t-loop', severity='medium', confidence='high',
        )
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='t-loop', phase='review',
            target_repo='ourliberty-agent-core',
            branch='forge/t-loop',
            result=f'Found 1 medium finding.\n\n{marker}',
        )
        # 5b prerequisite: forge_build_session_id must be on the envelope.
        body['forge_build_session_id'] = 'forge-build-sess-abc'
        body['pr_url'] = 'https://github.com/x/y/pull/77'
        body.update(overrides)
        return body

    def _forge_revision_outbox(self, round_num=1, **overrides):
        """Forge outbox after a revision dispatch — must start with the
        Revision N applied: preamble per the 5b strict gate."""
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-loop',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/t-loop',
            claude_session_id='forge-build-sess-abc',
            result=(
                f'Revision {round_num} applied: added input validation on '
                f'foo.py L12-L15 per Mirror finding.\n\n'
                f'Tests pass; pushed to forge/t-loop.'
            ),
        )
        body['pr_url'] = 'https://github.com/x/y/pull/77'
        body['revision_count'] = round_num
        body['max_revisions'] = 3
        body.update(overrides)
        return body

    # ----- Mirror REVISION → Forge revision dispatch -----

    def test_review_revision_dispatches_revision_to_forge(self):
        body = self._mirror_revision_outbox()
        f = self._write_outbox('mirror', 't-loop.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        # Revision task lands in Forge's inbox keyed on round number
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-t-loop-*.json')
        )
        self.assertEqual(len(revisions), 1)
        revision = json.loads(revisions[0].read_text())
        self.assertEqual(revision['task_id'], 't-loop')
        self.assertEqual(revision['source'], 'beacon')
        self.assertEqual(revision['phase'], 'revision')
        self.assertEqual(revision['session_id'], 'forge-build-sess-abc')
        self.assertEqual(revision['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(revision['branch'], 'forge/t-loop')
        self.assertEqual(revision['revision_count'], 1)
        self.assertEqual(revision['max_revisions'], 3)
        self.assertEqual(revision['dispatched_by'], 'outbox-notifier')
        # Findings serialized into the prompt
        self.assertIn('Mirror\'s findings on this PR', revision['prompt'])
        self.assertIn('medium', revision['prompt'])

    def test_review_revision_low_confidence_does_not_dispatch(self):
        # Auto-promote (5a behavior) blocks the revision dispatch — Mirror's
        # uncertainty means the auto-fix loop shouldn't run; escalate instead.
        marker = _mirror_revision_marker(task_id='t-low', confidence='low')
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='t-low',
            phase='review', target_repo='ourliberty-agent-core',
            branch='forge/t-low',
            result=f'Maybe?\n\n{marker}',
        )
        body['forge_build_session_id'] = 'forge-sess-xyz'
        body['pr_url'] = 'https://github.com/x/y/pull/78'
        f = self._write_outbox('mirror', 't-low.json', body)
        on.process_outbox(f)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-*.json')
        )
        self.assertEqual(revisions, [])
        # Beacon got the escalate notify instead
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        self.assertEqual(json.loads(notifies[0].read_text())['intent'], 'review-escalate')

    def test_review_revision_budget_exhausted_does_not_dispatch(self):
        # revision_count + 1 > max_revisions → downgrade to escalate.
        body = self._mirror_revision_outbox(
            revision_count=3, max_revisions=3,
        )
        f = self._write_outbox('mirror', 't-loop-exhausted.json', body)
        on.process_outbox(f)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-*.json')
        )
        self.assertEqual(revisions, [])
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertEqual(notify['intent'], 'review-escalate')
        # Budget-exhausted reason is in the prompt body
        self.assertIn('budget', notify['prompt'].lower())

    def test_review_revision_missing_forge_session_skips_dispatch(self):
        # Defensive: if forge_build_session_id propagation broke somewhere
        # upstream, the dispatch should skip rather than write a bogus task.
        body = self._mirror_revision_outbox()
        body.pop('forge_build_session_id', None)
        f = self._write_outbox('mirror', 't-no-session.json', body)
        on.process_outbox(f)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-*.json')
        )
        self.assertEqual(revisions, [])

    def test_revision_dispatch_idempotent_on_reprocess(self):
        body = self._mirror_revision_outbox()
        f = self._write_outbox('mirror', 't-loop.json', body)
        on.process_outbox(f)
        # Re-write the same outbox + re-process (simulates notifier crash
        # between dispatch and archive)
        f2 = self._write_outbox('mirror', 't-loop.json', body)
        on.process_outbox(f2)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-t-loop-*.json')
        )
        # Still exactly one revision-task, despite two process_outbox calls
        self.assertEqual(len(revisions), 1)

    # ----- Forge revision outbox → Mirror re-review dispatch -----

    def test_forge_revision_outbox_dispatches_rereview(self):
        body = self._forge_revision_outbox(round_num=1)
        f = self._write_outbox('forge', 'revision-t-loop-1.json', body)
        on.process_outbox(f)
        # Mirror inbox gets a fresh review-request keyed on round number
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-t-loop-rev*.json')
        )
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(review['task_id'], 't-loop')
        self.assertEqual(review['source'], 'beacon')
        self.assertEqual(review['phase'], 'review')
        self.assertEqual(review['revision_count'], 1)
        self.assertEqual(review['max_revisions'], 3)
        self.assertIn('Re-review phase', review['prompt'])
        self.assertIn('revision 1', review['prompt'])

    def test_forge_revision_outbox_missing_preamble_dead_letters(self):
        # Strict gate per Larry's Option 3 signoff: revision phase MUST
        # start with "Revision N applied:" preamble. Missing → marker-error
        # cascade back to Forge.
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-bad-rev',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='forge-sess',
            # NO preamble; Forge fast-pathed past the protocol
            result='I applied the fix but forgot to use the required format.',
        )
        f = self._write_outbox('forge', 'revision-t-bad-rev-1.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        # Marker-error notify lands in Forge's inbox
        marker_errors = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(marker_errors), 1)
        notify = json.loads(marker_errors[0].read_text())
        self.assertEqual(notify['intent'], 'marker-error')
        self.assertIn('Revision N applied', notify['prompt'])
        # No Mirror re-review dispatched
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')),
            [],
        )

    def test_rereview_dispatch_idempotent_on_reprocess(self):
        body = self._forge_revision_outbox(round_num=1)
        f = self._write_outbox('forge', 'revision-t-loop-1.json', body)
        on.process_outbox(f)
        f2 = self._write_outbox('forge', 'revision-t-loop-1.json', body)
        on.process_outbox(f2)
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-t-loop-rev*.json')
        )
        self.assertEqual(len(reviews), 1)

    def test_forge_revision_round_2_extracted_correctly(self):
        # Round 2 prefix should parse and feed Mirror's revision_count=2.
        body = self._forge_revision_outbox(round_num=2)
        f = self._write_outbox('forge', 'revision-t-loop-2.json', body)
        on.process_outbox(f)
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-t-loop-rev*.json')
        )
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(review['revision_count'], 2)
        self.assertIn('revision 2', review['prompt'])

    # ----- session_id propagation through the chain -----

    def test_forge_session_id_propagates_into_mirror_review_request(self):
        # 5a's _dispatch_mirror_review extended in 5b to carry
        # forge_build_session_id forward (so a downstream REVIEW_REVISION
        # can resume Forge's build session).
        build = _good_outbox(
            agent='forge', source='beacon', task_id='t-thread',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/t-thread',
            claude_session_id='forge-build-thread-sess',
            result=(
                'PR opened: https://github.com/x/y/pull/99\n\n'
                'Done.'
            ),
        )
        f = self._write_outbox('forge', 't-thread.json', build)
        on.process_outbox(f)
        # Mirror's review-request envelope should now carry the field.
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-t-thread.json'))
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(
            review['forge_build_session_id'], 'forge-build-thread-sess',
        )

    # ----- build phase remains lenient (Option 3) -----

    def test_build_phase_missing_pr_url_falls_through_to_default_routing(self):
        # Strict gate is revision-only per Larry's Option 3 signoff. A
        # build response without the PR URL prefix (legitimate blocker
        # paragraph) must still reach Beacon via default routing — NOT
        # marker-error cascade. Confirms 5a behavior preserved.
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-blocker',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/t-blocker',
            claude_session_id='forge-blocker-sess',
            result=(
                'Compile error in foo.py — the spec asked me to import '
                'bar which does not exist. Need clarification on the '
                'real module name before I can proceed.'
            ),
        )
        f = self._write_outbox('forge', 't-blocker.json', body)
        result = on.process_outbox(f)
        # Default routing fires (not marker-error)
        self.assertEqual(result, 'notified')
        # Beacon got the blocker paragraph via forge-result default routing
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertIn('Compile error', notify['prompt'])
        # NO marker-error notify to Forge
        self.assertEqual(
            list((on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')),
            [],
        )
        # NO Mirror dispatch (no PR URL to review)
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')),
            [],
        )

    # ----- budget-exhausted DM (escalate intent fires Larry DM) -----

    def test_budget_exhausted_queues_larry_dm(self):
        # When REVIEW_REVISION downgrades to escalate via budget exhaustion,
        # the auto-DM hook should fire (review-escalate is in
        # TERMINAL_DM_INTENTS). Confirms Larry hears about loop termination.
        body = self._mirror_revision_outbox(
            revision_count=3, max_revisions=3, reply_chat_id=7998341473,
        )
        f = self._write_outbox('mirror', 't-loop-exhausted.json', body)
        on.process_outbox(f)
        # Check larry-alerts.jsonl for the notification
        pending = self._la.read_pending(0)
        notifications = [r for _, r in pending if r.get('kind') == 'notification']
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]['intent'], 'review-escalate')
        self.assertIn('budget', notifications[0]['message'].lower())


class ExtractRevisionSummaryTest(unittest.TestCase):
    """Tests for the revision-summary preamble regex."""

    def test_extracts_round_and_summary(self):
        result = on._extract_revision_summary_from_result(
            'Revision 2 applied: added missing validation per Mirror.'
        )
        self.assertEqual(result, (2, 'added missing validation per Mirror.'))

    def test_leading_whitespace_ok(self):
        result = on._extract_revision_summary_from_result(
            '\n\n  Revision 1 applied: fixed it.'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 1)

    def test_case_insensitive_keyword(self):
        result = on._extract_revision_summary_from_result(
            'REVISION 3 APPLIED: case test.'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 3)

    def test_missing_returns_none(self):
        self.assertIsNone(on._extract_revision_summary_from_result(''))
        self.assertIsNone(on._extract_revision_summary_from_result(None))
        self.assertIsNone(on._extract_revision_summary_from_result(
            'just a narrative without the preamble'
        ))

    def test_buried_in_narrative_returns_none(self):
        # Anchored to start; a "Revision N applied:" mention deeper in the
        # text shouldn't match (Forge could be discussing a prior revision).
        self.assertIsNone(on._extract_revision_summary_from_result(
            'I considered earlier work where Revision 1 applied: stuff.\n'
            'Then realized I need to fix something else.'
        ))

    def test_multiline_summary_only_first_line(self):
        # The summary is one-line (stops at newline). Narrative below is
        # preserved by the caller but doesn't extend the summary.
        result = on._extract_revision_summary_from_result(
            'Revision 1 applied: one-line summary.\n'
            'Detailed narrative below explaining changes.'
        )
        self.assertEqual(result, (1, 'one-line summary.'))


# ============================================================================
# D3.5 5b — Post-review fixes (C-1, M-2, M-3, M-4, m-5, m-6)
# ============================================================================


class RevisionFollowupFixesTest(unittest.TestCase):
    """Tests for the 6 issues caught by the independent reviewer + Bug A/B/C."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT', 'BLACKBOARD',
            'LOG_FILE', 'DEAD_LETTER_STATE', 'EMERGENCY_HALT_FLAG',
            '_MODELS_CONFIG_PATH',
        ]:
            self._originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        on._MODELS_CONFIG_PATH = self._root / 'config' / 'agent-models.json'
        on._invalidate_loop_bounds_cache()
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
        # larry_alerts paths (for Bug C tests that verify DM-queue side effects)
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        self._la = la
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        on._invalidate_loop_bounds_cache()
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(self._la, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_models_config(self, body):
        on._MODELS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        on._MODELS_CONFIG_PATH.write_text(json.dumps(body))
        on._invalidate_loop_bounds_cache()

    def _write_outbox(self, agent, name, body):
        d = on.OUTBOXES_ROOT / agent
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text(json.dumps(body))
        return f

    # ---- C-1: forge_build_session_id propagates through revision dispatch ----

    def test_c1_revision_dispatch_propagates_forge_build_session_id(self):
        # The C-1 review fix: revision task envelope must carry
        # forge_build_session_id so _build_outbox propagates it to Forge's
        # revision outbox, so round 2's REVIEW_REVISION can find it.
        data = {
            'task_id': 't-loop',
            'forge_build_session_id': 'forge-sess-abc',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-loop',
            'pr_url': 'https://github.com/x/y/pull/1',
            'revision_count': 0,
            'max_revisions': 3,
            'reply_chat_id': 7998341473,
        }
        decision = {
            'marker_type': 'review_revision',
            'payload': {
                'task_id': 't-loop', 'pr_url': data['pr_url'],
                'findings': [
                    {'file': 'a.py', 'line_range': 'L10', 'severity': 'medium',
                     'description': 'fix this'},
                ],
                'severity': 'medium', 'confidence': 'high',
            },
        }
        on._dispatch_revision_to_forge(data, decision)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-t-loop-*.json')
        )
        self.assertEqual(len(revisions), 1)
        task = json.loads(revisions[0].read_text())
        # Both session_id (for --resume) and forge_build_session_id (for
        # forward-propagation to round 2) must be set.
        self.assertEqual(task['session_id'], 'forge-sess-abc')
        self.assertEqual(task['forge_build_session_id'], 'forge-sess-abc')

    # ---- M-2: marker-error propagates revision-phase fields ----

    def test_m2_marker_error_propagates_revision_phase_fields(self):
        # Forge revision outbox without preamble triggers marker-error.
        # The marker-error notify must carry phase, forge_build_session_id,
        # revision_count, max_revisions, pr_url forward so Forge's retry
        # can re-emit cleanly with full revision context.
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-no-preamble',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/t-no-preamble',
            claude_session_id='forge-build-sess',
            result='I forgot the required preamble format.',
        )
        body['forge_build_session_id'] = 'forge-build-sess'
        body['revision_count'] = 2
        body['max_revisions'] = 3
        body['pr_url'] = 'https://github.com/x/y/pull/1'
        body['reply_chat_id'] = 7998341473
        f = self._write_outbox('forge', 'revision-t-no-preamble-2.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')

        marker_errors = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(marker_errors), 1)
        notify = json.loads(marker_errors[0].read_text())
        # All 5 revision-phase envelope fields must propagate.
        self.assertEqual(notify['phase'], 'revision')
        self.assertEqual(notify['forge_build_session_id'], 'forge-build-sess')
        self.assertEqual(notify['revision_count'], 2)
        self.assertEqual(notify['max_revisions'], 3)
        self.assertEqual(notify['pr_url'], 'https://github.com/x/y/pull/1')
        # session_id propagates too (already covered by existing test; verify)
        self.assertEqual(notify['session_id'], 'forge-build-sess')

    # ---- M-3: round_num validation against envelope ----

    def test_m3_round_zero_rejected_as_marker_error(self):
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-zero',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess', result='Revision 0 applied: nope.',
        )
        body['revision_count'] = 1
        f = self._write_outbox('forge', 'revision-t-zero-1.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        marker_errors = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(marker_errors), 1)
        notify = json.loads(marker_errors[0].read_text())
        self.assertIn('round number must be', notify['prompt'])
        self.assertIn('round 1', notify['prompt'])

    def test_m3_round_mismatch_rejected_as_marker_error(self):
        # Envelope says round 2, Forge wrote "Revision 1 applied:" (round drift).
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-drift',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess',
            result='Revision 1 applied: fixed the thing.',
        )
        body['revision_count'] = 2
        f = self._write_outbox('forge', 'revision-t-drift-2.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        notify = json.loads(
            next(iter((on.INBOXES_ROOT / 'forge').glob('marker-error-*.json'))).read_text()
        )
        self.assertIn('does not match', notify['prompt'])
        self.assertIn('revision_count', notify['prompt'])
        self.assertIn('Revision 2 applied', notify['prompt'])

    def test_m3_round_high_inflation_rejected(self):
        # Forge tries to skip ahead — envelope round 1, she writes "Revision 99
        # applied" (potentially trying to force-exhaust budget).
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-inflate',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess',
            result='Revision 99 applied: skipping ahead.',
        )
        body['revision_count'] = 1
        f = self._write_outbox('forge', 'revision-t-inflate-1.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')

    def test_m3_correct_round_passes(self):
        # Sanity: round matches envelope → dispatches the re-review normally.
        body = _good_outbox(
            agent='forge', source='beacon', task_id='t-ok',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess',
            result='Revision 2 applied: all findings addressed.',
        )
        body['revision_count'] = 2
        body['pr_url'] = 'https://github.com/x/y/pull/1'
        f = self._write_outbox('forge', 'revision-t-ok-2.json', body)
        on.process_outbox(f)
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-t-ok-rev*.json')
        )
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(review['revision_count'], 2)

    # ---- M-4: max_revisions read from config file ----

    def test_m4_max_revisions_read_from_config(self):
        # Write a config with max_revisions=5; verify _load reads it.
        self._write_models_config({
            'agents': {'forge': {'allowed_repos': ['ourliberty-agent-core']}},
            'loop_bounds': {'max_revisions': 5, 'max_replans': 2},
        })
        self.assertEqual(on._load_max_revisions_from_config(), 5)

    def test_m4_falls_back_to_default_when_config_missing(self):
        # No config file at all → fall back to DEFAULT_MAX_REVISIONS (3).
        self.assertEqual(
            on._load_max_revisions_from_config(),
            on.mrh.DEFAULT_MAX_REVISIONS,
        )

    def test_m4_falls_back_when_loop_bounds_missing(self):
        # Config exists but no loop_bounds key.
        self._write_models_config({'agents': {}})
        self.assertEqual(
            on._load_max_revisions_from_config(),
            on.mrh.DEFAULT_MAX_REVISIONS,
        )

    def test_m4_falls_back_when_value_invalid(self):
        # max_revisions present but not a non-negative int.
        self._write_models_config({
            'loop_bounds': {'max_revisions': 'three'},
        })
        self.assertEqual(
            on._load_max_revisions_from_config(),
            on.mrh.DEFAULT_MAX_REVISIONS,
        )

    def test_m4_falls_back_when_malformed_json(self):
        on._MODELS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        on._MODELS_CONFIG_PATH.write_text('{not valid json')
        on._invalidate_loop_bounds_cache()
        self.assertEqual(
            on._load_max_revisions_from_config(),
            on.mrh.DEFAULT_MAX_REVISIONS,
        )

    def test_m4_dispatch_mirror_review_uses_config_value(self):
        # End-to-end: configure max_revisions=7; verify Mirror's
        # review-request envelope carries 7 (not the hardcoded default 3).
        self._write_models_config({
            'agents': {'forge': {'allowed_repos': ['ourliberty-agent-core']},
                       'mirror': {'allowed_repos': ['ourliberty-agent-core']}},
            'loop_bounds': {'max_revisions': 7},
        })
        on._dispatch_mirror_review(
            {
                'task_id': 't-cfg',
                'target_repo': 'ourliberty-agent-core',
                'branch': 'forge/t-cfg',
                'claude_session_id': 'sess',
            },
            'https://github.com/x/y/pull/1',
        )
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(review['max_revisions'], 7)

    # ---- m-5: budget evaluated even when auto_promoted ----

    def test_m5_low_confidence_at_budget_exhausted_uses_budget_reason(self):
        # Low confidence + budget exhausted: reason text should mention
        # the budget cap (stronger termination signal), with audit-trail
        # note that auto-promote would also have routed escalate.
        data = {
            'task_id': 't-both',
            'agent': 'mirror',
            'result': (
                'Found something I am not sure about.\n\n'
                '=== REVIEW_REVISION ===\n'
                + json.dumps({
                    'task_id': 't-both',
                    'pr_url': 'https://github.com/x/y/pull/1',
                    'findings': [{'file': 'a', 'line_range': 'L1',
                                 'severity': 'low', 'description': 'maybe?'}],
                    'severity': 'low', 'confidence': 'low',
                })
                + '\n=== END_REVIEW_REVISION ==='
            ),
            'revision_count': 3,
            'max_revisions': 3,
        }
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['intent'], 'review-escalate')
        self.assertTrue(decision['auto_promoted'])
        self.assertTrue(decision['budget_exhausted'])
        reason = decision['intent_kwargs']['reason']
        # Budget-exhausted reason is primary; auto-promote noted as appendix.
        self.assertIn('budget', reason.lower())
        self.assertIn('confidence: low', reason)

    # ---- m-6: DM lead line is cause-agnostic ----

    def test_m6_escalate_dm_template_does_not_say_mirror_escalated(self):
        # The template lead shouldn't claim Mirror initiated the escalation
        # — it can fire from auto-promote or budget-exhaust too. Lead is
        # cause-agnostic; body's {reason} carries the specifics.
        template = on.DM_TEMPLATES['review-escalate']
        self.assertNotIn('Mirror escalated', template)
        self.assertIn('Review escalated', template)
        self.assertIn('{reason}', template)

    # ---- M-7 (second-pass): Mirror marker-error propagates revision fields ----

    def test_m7_mirror_marker_error_propagates_forge_session_and_phase(self):
        # If Mirror's REVIEW_REVISION marker has bad JSON, the marker-error
        # retry task must carry forge_build_session_id + phase forward so
        # her clean retry's outbox has the field, _build_outbox propagates,
        # and _dispatch_revision_to_forge can find the session to --resume.
        data = {
            'agent': 'mirror', 'source': 'beacon', 'task_id': 't-mirror-retry',
            'phase': 'review',
            'forge_build_session_id': 'forge-build-sess',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-mirror-retry',
            'pr_url': 'https://github.com/x/y/pull/5',
            'revision_count': 0,
            'max_revisions': 3,
            'reply_chat_id': 7998341473,
            'claude_session_id': 'mirror-sess',
        }
        on._notify_mirror_marker_error(data, 'bad JSON in marker')
        notifies = list(
            (on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json')
        )
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        # The two new fields (M-7 fix) must propagate.
        self.assertEqual(notify['forge_build_session_id'], 'forge-build-sess')
        self.assertEqual(notify['phase'], 'review')
        # Plus the existing propagation should still work.
        self.assertEqual(notify['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(notify['pr_url'], 'https://github.com/x/y/pull/5')
        self.assertEqual(notify['revision_count'], 0)
        self.assertEqual(notify['reply_chat_id'], 7998341473)

    # ---- M-8 (second-pass): previous_findings threaded through chain ----

    def test_m8_revision_dispatch_carries_previous_findings(self):
        # _dispatch_revision_to_forge writes findings into the prompt AND
        # threads them through the envelope as previous_findings so Mirror's
        # re-review prompt can include them on round 2+.
        data = {
            'task_id': 't-findings',
            'forge_build_session_id': 'forge-sess',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-findings',
            'pr_url': 'https://github.com/x/y/pull/1',
            'revision_count': 0,
            'max_revisions': 3,
        }
        findings = [
            {'file': 'a.py', 'line_range': 'L10', 'severity': 'medium',
             'description': 'missing validation'},
            {'file': 'b.py', 'line_range': 'L20', 'severity': 'low',
             'description': 'unclear name'},
        ]
        decision = {
            'marker_type': 'review_revision',
            'payload': {
                'task_id': 't-findings', 'pr_url': data['pr_url'],
                'findings': findings,
                'severity': 'medium', 'confidence': 'high',
            },
        }
        on._dispatch_revision_to_forge(data, decision)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-t-findings-*.json')
        )
        self.assertEqual(len(revisions), 1)
        task = json.loads(revisions[0].read_text())
        self.assertEqual(task['previous_findings'], findings)

    def test_m8_rereview_prompt_includes_previous_findings(self):
        # Forge's revision outbox carries previous_findings via _build_outbox
        # propagation; _dispatch_mirror_review_rerun reads them and injects
        # into Mirror's re-review prompt. Without this, Mirror would
        # re-derive findings from scratch on round 2.
        data = {
            'task_id': 't-rerev',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-rerev',
            'pr_url': 'https://github.com/x/y/pull/1',
            'revision_count': 1,
            'max_revisions': 3,
            'forge_build_session_id': 'forge-sess',
            'previous_findings': [
                {'file': 'a.py', 'line_range': 'L10', 'severity': 'medium',
                 'description': 'critical-finding-keyword-marker'},
            ],
        }
        on._dispatch_mirror_review_rerun(data, 1, 'fixed it')
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-t-rerev-rev*.json')
        )
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        # The finding's description must appear in the re-review prompt.
        self.assertIn('critical-finding-keyword-marker', review['prompt'])
        self.assertIn('Your findings from the previous round', review['prompt'])
        # AND the envelope propagates findings forward for round 2+ (in case
        # Mirror flags REVIEW_REVISION again on this re-review).
        self.assertEqual(
            review['previous_findings'],
            data['previous_findings'],
        )

    def test_m8_rereview_with_no_previous_findings_does_not_render_section(self):
        # If somehow previous_findings is missing or empty, prompt should
        # still render (degrade) without the findings section.
        data = {
            'task_id': 't-empty-findings',
            'target_repo': 'ourliberty-agent-core',
            'pr_url': 'https://github.com/x/y/pull/1',
            'revision_count': 1,
            'max_revisions': 3,
        }
        on._dispatch_mirror_review_rerun(data, 1, 'summary')
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        # Prompt still renders, just without the findings section.
        self.assertIn('Re-review phase', review['prompt'])
        self.assertNotIn('Your findings from the previous round', review['prompt'])

    # ---- m-9 (second-pass): pr_url missing skips dispatch ----

    def test_m9_rereview_missing_pr_url_skips_dispatch(self):
        # Defensive: don't substitute literal '(unknown)' for missing pr_url
        # — it propagates as a marker value into the next revision prompt.
        # Instead, log + skip the dispatch (matches target_repo gate shape).
        data = {
            'task_id': 't-no-url',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/t-no-url',
            'revision_count': 1,
            'max_revisions': 3,
            # NO pr_url
        }
        on._dispatch_mirror_review_rerun(data, 1, 'summary')
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        # Should NOT have dispatched
        self.assertEqual(reviews, [])

    # ---- Bug B: envelope task_id stays ORIGINAL across marker-error retries ----

    def test_bug_b_forge_marker_error_keeps_original_task_id(self):
        # 2026-05-13 live test failed because marker-error retries wrapped
        # the envelope task_id as `marker-error-<orig>-<N>`. Forge emitted
        # her marker with the ORIGINAL task_id (semantically correct — it's
        # her task), the 4b task_id-mismatch check then rejected every
        # retry, cascade exhausted on round 3. Fix: envelope task_id stays
        # original; filename + marker_error_count handle uniqueness.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'opmanual-d35-5b-shipped-note-001',
            'phase': 'preflight', 'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 7998341473,
            'result': '=== PROCEED ===\n<prose, no JSON>\n=== END_PROCEED ===',
        }
        on._notify_forge_marker_error(data, 'no JSON in marker block')
        notifies = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        # The envelope task_id MUST be the original, not the wrapper.
        self.assertEqual(
            notify['task_id'], 'opmanual-d35-5b-shipped-note-001',
        )
        # marker_error_count carries the retry counter.
        self.assertEqual(notify['marker_error_count'], 1)
        # Filename has the wrap for disk-level uniqueness.
        self.assertIn('marker-error-opmanual-d35-5b-shipped-note-001-1',
                      notifies[0].name)

    def test_bug_b_forge_retry_2_also_keeps_original_task_id(self):
        # On retry 2, the previous envelope had task_id=original AND
        # marker_error_count=1. The retry-2 envelope should still have
        # task_id=original, marker_error_count=2.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'opmanual-d35-5b-shipped-note-001',
            'marker_error_count': 1,  # this is now the 2nd retry
            'original_source': 'beacon',
            'phase': 'preflight', 'target_repo': 'ourliberty-agent-core',
            'result': 'still no JSON',
        }
        on._notify_forge_marker_error(data, 'second attempt also failed')
        notifies = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertEqual(
            notify['task_id'], 'opmanual-d35-5b-shipped-note-001',
        )
        self.assertEqual(notify['marker_error_count'], 2)

    def test_bug_b_mirror_marker_error_keeps_original_task_id(self):
        data = {
            'agent': 'mirror', 'source': 'beacon',
            'task_id': 't-mirror-bad-marker',
            'phase': 'review', 'forge_build_session_id': 'forge-sess',
            'target_repo': 'ourliberty-agent-core',
            'result': '=== REVIEW_PASS ===\n<prose>\n=== END_REVIEW_PASS ===',
        }
        on._notify_mirror_marker_error(data, 'prose, not JSON')
        notifies = list(
            (on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json')
        )
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertEqual(notify['task_id'], 't-mirror-bad-marker')
        self.assertEqual(notify['marker_error_count'], 1)

    # ---- Bug C: dead-letter triggers Larry DM ----

    def test_bug_c_dead_letter_queues_larry_dm(self):
        # When marker-error retries exhaust, the dead-letter notify to
        # Beacon should ALSO queue a closing DM to Larry's chat thread.
        # Without this (the failed 2026-05-13 live test), the chat goes
        # silent after "approved + dispatched".
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 't-exhausted',
            'reply_chat_id': 7998341473,
            'marker_error_count': 3,
            'original_source': 'beacon',
        }
        on._dead_letter_marker_error_to_dispatcher(
            data, 'beacon', 'no JSON in marker block', 4,
        )
        # Beacon got the inter-agent dead-letter notify.
        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('dead-letter-*.json')
        )
        self.assertEqual(len(beacon_notifies), 1)
        # AND Larry got a closing DM via larry-alerts.jsonl
        pending = self._la.read_pending(0)
        notifs = [r for _, r in pending if r.get('kind') == 'notification']
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['intent'], 'dead-letter')
        self.assertEqual(notifs[0]['chat_id'], 7998341473)
        self.assertEqual(notifs[0]['task_id'], 't-exhausted')
        self.assertIn('failed after 4 marker-error retries', notifs[0]['message'])
        self.assertIn('no JSON', notifs[0]['message'])

    def test_bug_c_dead_letter_without_chat_id_does_not_queue_dm(self):
        # Autonomous Pulse-initiated runs have no reply_chat_id. The
        # dead-letter still writes to Beacon's inbox; no DM is queued
        # (silent skip in _maybe_dm_larry).
        data = {
            'agent': 'forge', 'source': 'beacon', 'task_id': 't-auto',
            # NO reply_chat_id
            'original_source': 'beacon',
        }
        on._dead_letter_marker_error_to_dispatcher(
            data, 'beacon', 'reason', 4,
        )
        pending = self._la.read_pending(0)
        notifs = [r for _, r in pending if r.get('kind') == 'notification']
        self.assertEqual(notifs, [])
        # But the dead-letter to Beacon still landed.
        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('dead-letter-*.json')
        )
        self.assertEqual(len(beacon_notifies), 1)

    def test_bug_c_dead_letter_template_renders_with_retry_count(self):
        # Template should substitute task_id, reason, retry_count.
        template = on.DM_TEMPLATES['dead-letter']
        rendered = template.format(
            task_id='t-foo', reason='bad marker', retry_count=3,
        )
        self.assertIn('t-foo', rendered)
        self.assertIn('bad marker', rendered)
        self.assertIn('3', rendered)
        self.assertIn('no PR was opened', rendered)

    # ---- Bug E (live re-test): _notify_forge_marker_error propagates reply_chat_id ----

    def test_bug_e_forge_marker_error_propagates_reply_chat_id(self):
        # 2026-05-13 live re-test: PR #5 completed successfully (Mirror
        # PASSed) but Larry got no closing DM. Root cause: Forge slipped
        # on JSON-vs-prose, marker-error fired, retry task dropped
        # reply_chat_id. Every downstream hop's propagation block found
        # None to propagate. Mirror's outbox had reply_chat_id=None,
        # _maybe_dm_larry silently skipped.
        # Symmetric with the existing M-3 fix on _notify_mirror_marker_error.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'opmanual-d35-5b-shipped-note-001',
            'phase': 'preflight', 'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 7998341473,
            'result': '=== PROCEED ===\n<prose>\n=== END_PROCEED ===',
        }
        on._notify_forge_marker_error(data, 'no JSON in marker')
        notifies = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertEqual(notify['reply_chat_id'], 7998341473)

    def test_bug_e_forge_marker_error_omits_when_absent(self):
        # Defensive: if reply_chat_id is absent (autonomous Pulse runs),
        # the retry task must NOT inject a falsy reply_chat_id key.
        data = {
            'agent': 'forge', 'source': 'beacon', 'task_id': 't-no-chat',
            'phase': 'preflight', 'target_repo': 'ourliberty-agent-core',
            'result': 'failed',
        }
        on._notify_forge_marker_error(data, 'reason')
        notifies = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertNotIn('reply_chat_id', notify)


# -------------------- D3.5 5c — Beacon auto-replan loop --------------------


def _beacon_approval_request_marker(
    task_id='task-001',
    summary=(
        'Address Mirror\'s concern about missing input validation by '
        'narrowing the parser interface and adding boundary checks.'
    ),
    target_agent='forge',
    prompt='x' * 200,
):
    """Synthetic Beacon APPROVAL_REQUEST marker block — same shape Beacon
    emits in chat-mode, now extracted from outboxes by 5c."""
    payload = json.dumps({
        'task_id': task_id,
        'summary': summary,
        'target_agent': target_agent,
        'prompt': prompt,
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'feature-development',
    })
    return (
        f'=== APPROVAL_REQUEST ===\n{payload}\n'
        f'=== END_APPROVAL_REQUEST ==='
    )


class BeaconReplanLoopTest(unittest.TestCase):
    """End-to-end: Mirror REVIEW_ESCALATE → Beacon receives notify with
    replan_count + max_replans + mirror_escalate_reason → Beacon emits
    APPROVAL_REQUEST → notifier extracts + queues approval-request alert
    → bot DMs Larry. Plus budget-exhaust + discipline-gate paths.

    Mirrors `RevisionLoopTest` setUp/tearDown shape — full tmpdir reroute
    of AGENTS_ROOT + INBOXES_ROOT + OUTBOXES_ROOT + ROUTING_EVENTS_LOG +
    larry_alerts queue + pending-approvals state. Tests run in isolation.
    """

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
        # larry_alerts paths
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        self._la = la
        # beacon_approval_handler state path (notifier writes pending entries)
        import beacon_approval_handler as ah
        self._ah_original = ah.PENDING_APPROVALS_PATH
        ah.PENDING_APPROVALS_PATH = self._root / 'state' / 'pending-approvals.json'
        self._ah = ah
        # trust_policy runtime path — tests override per-case via _set_policy
        import trust_policy as tp
        self._tp = tp
        self._tp_original_runtime = tp.RUNTIME_POLICY_PATH
        self._tp_original_repo = tp.REPO_POLICY_PATH
        tp.RUNTIME_POLICY_PATH = self._root / 'trust-policy.json'
        tp.REPO_POLICY_PATH = self._root / 'trust-policy-repo.json'  # absent
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(self._la, name, value)
        self._ah.PENDING_APPROVALS_PATH = self._ah_original
        self._tp.RUNTIME_POLICY_PATH = self._tp_original_runtime
        self._tp.REPO_POLICY_PATH = self._tp_original_repo
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _set_policy(self, default_action='force_ask', rules=None):
        """Write a synthetic trust-policy file for the test case."""
        policy = {
            'version': 1,
            'default_action': default_action,
            'rules': rules or [],
        }
        self._tp.RUNTIME_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._tp.RUNTIME_POLICY_PATH.write_text(json.dumps(policy))

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _read_alerts(self):
        """Return parsed records from the synthetic larry-alerts.jsonl."""
        if not self._la.ALERTS_FILE.exists():
            return []
        lines = self._la.ALERTS_FILE.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _beacon_replan_outbox(
        self,
        marker_text=None,
        narrative_prefix='I will revise the spec to address Mirror\'s point.',
        replan_count=0,
        max_replans=2,
        mirror_escalate_reason='Missing input validation in the parser.',
        reply_chat_id=7998341473,
        **overrides,
    ):
        """Synthetic Beacon outbox responding to a review-escalate inbox
        dispatch. Marker is appended after the narrative — _route_beacon_
        replan_approval extracts it via approval.extract_approval_request.

        Defaults match the discipline-gate-pass shape: task_id='task-001'
        matches the envelope, the summary shares >2 >3-char tokens with
        mirror_escalate_reason ('validation', 'parser', etc).
        """
        if marker_text is None:
            marker_text = _beacon_approval_request_marker(task_id='task-001')
        result = (
            f'{narrative_prefix}\n\n{marker_text}'
            if marker_text else narrative_prefix
        )
        # source='outbox-notifier' mirrors what the notifier writes when
        # routing Mirror's escalate marker to Beacon's inbox (via the
        # mirror-result notify_source → outbox-notifier filename).
        # 5c-followup fix: envelope task_id has the `notify-` prefix in
        # production (the marker-routing block in process_outbox prefixes
        # it for filename disambiguation). Match that shape here so the
        # discipline-gate prefix-stripping logic is exercised. Beacon's
        # marker payload uses the ORIGINAL task_id (no prefix) per her
        # Shape 8 guidance — that's already what _beacon_approval_request_
        # marker emits as task_id='task-001'.
        body = _good_outbox(
            agent='beacon',
            source='mirror-result',
            task_id='notify-task-001',
            result=result,
        )
        body['inbound_intent'] = 'review-escalate'
        body['replan_count'] = replan_count
        body['max_replans'] = max_replans
        if mirror_escalate_reason:
            body['mirror_escalate_reason'] = mirror_escalate_reason
        if reply_chat_id is not None:
            body['reply_chat_id'] = reply_chat_id
        body.update(overrides)
        return body

    # ----- Mirror REVIEW_ESCALATE → Beacon notify carries replan budget -----

    def test_mirror_escalate_notify_carries_replan_count_and_reason(self):
        """Mirror's REVIEW_ESCALATE outbox processed by the notifier
        produces a notify-task on Beacon's inbox that carries replan_count,
        max_replans, and mirror_escalate_reason for the next leg."""
        marker = _mirror_escalate_marker(
            task_id='task-001', reason='Missing input validation in the parser.',
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='task-001',
            target_repo='ourliberty-agent-core',
        )
        body['reply_chat_id'] = 7998341473
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        # Notify task on Beacon's inbox
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        self.assertEqual(notify['intent'], 'review-escalate')
        self.assertEqual(notify['replan_count'], 0)
        self.assertEqual(notify['max_replans'], 2)  # from loop_bounds
        self.assertIn(
            'input validation', notify['mirror_escalate_reason'].lower(),
        )

    def test_mirror_escalate_propagates_inbound_replan_count(self):
        """When Mirror's envelope carries replan_count=1 (because Beacon
        already replanned once and this is the second escalate), the
        notify to Beacon must carry replan_count=1 forward."""
        marker = _mirror_escalate_marker(task_id='task-001')
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='task-001',
            target_repo='ourliberty-agent-core',
        )
        body['replan_count'] = 1
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        notify = json.loads(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))[0].read_text()
        )
        self.assertEqual(notify['replan_count'], 1)

    # ----- Beacon outbox with APPROVAL_REQUEST → extracted and routed -----

    def test_beacon_approval_request_adds_pending_and_queues_alert(self):
        body = self._beacon_replan_outbox()
        f = self._write_outbox('beacon', 'beacon-replan.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')
        # Pending-approvals state has the entry
        state = self._ah.load_state()
        pending = state.get('pending', [])
        self.assertEqual(len(pending), 1)
        entry = pending[0]
        self.assertEqual(entry['id'], 'task-001')
        self.assertEqual(entry['chat_id'], 7998341473)
        self.assertEqual(entry.get('_replan_count'), 1)
        self.assertEqual(entry.get('_max_replans'), 2)
        # Approval-request alert queued
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 1)
        rec = approval_records[0]
        self.assertEqual(rec['approval_id'], 'task-001')
        self.assertEqual(rec['chat_id'], 7998341473)
        self.assertIn('task-001', rec['body'])

    def test_beacon_no_marker_falls_through_to_default_routing(self):
        """Beacon's response with no APPROVAL_REQUEST is a push-back via
        prose — should default-route, NOT trigger the replan path."""
        body = self._beacon_replan_outbox(marker_text='')
        f = self._write_outbox('beacon', 'beacon-pushback.json', body)
        result = on.process_outbox(f)
        # Default routing notifies the source-agent equivalent; pulse here
        # is not the source (source is 'mirror-result' which is an infra
        # source — no primary_agent_id). So this should archive without
        # notify, NOT 'notified-replan'.
        self.assertNotEqual(result, 'notified-replan')
        # No pending entry
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        # No approval-request alert
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 0)

    def test_beacon_replan_without_inbound_intent_does_not_route(self):
        """Defense in depth: if `inbound_intent` is missing (e.g., chat-mode
        outbox somehow lands in the notifier path), the replan path must
        NOT fire — Beacon's chat-mode flow handles those."""
        body = self._beacon_replan_outbox()
        body.pop('inbound_intent', None)
        f = self._write_outbox('beacon', 'beacon-chat.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-replan')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    # ----- Discipline gate (level 3) -----

    def test_discipline_gate_task_id_mismatch_falls_through(self):
        marker = _beacon_approval_request_marker(task_id='task-WRONG')
        body = self._beacon_replan_outbox(marker_text=marker)
        f = self._write_outbox('beacon', 'beacon-mismatch.json', body)
        result = on.process_outbox(f)
        # Falls through to default routing
        self.assertNotEqual(result, 'notified-replan')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    def test_discipline_gate_no_reason_overlap_falls_through(self):
        marker = _beacon_approval_request_marker(
            task_id='task-001',
            summary='Implement an entirely unrelated thing instead.',
        )
        body = self._beacon_replan_outbox(
            marker_text=marker,
            mirror_escalate_reason='Missing input validation in the parser.',
        )
        f = self._write_outbox('beacon', 'beacon-norefr.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-replan')

    def test_discipline_gate_empty_mirror_reason_auto_passes(self):
        """When mirror_escalate_reason is empty (defensive fallback), the
        reason-overlap check is skipped — task_id match is still enforced."""
        marker = _beacon_approval_request_marker(
            task_id='task-001',
            summary='Any unrelated summary at all.',
        )
        body = self._beacon_replan_outbox(
            marker_text=marker, mirror_escalate_reason='',
        )
        f = self._write_outbox('beacon', 'beacon-empty.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')

    # ----- Budget gate -----

    def test_replan_budget_exhausted_suppresses_marker(self):
        """replan_count=2 already → next_count=3 > max_replans=2 → exhausted.
        No pending entry; budget-exhaust notification queued for Larry."""
        body = self._beacon_replan_outbox(replan_count=2, max_replans=2)
        f = self._write_outbox('beacon', 'beacon-exhaust.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')  # handled, just suppressed
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        # Larry-notification queued
        alerts = self._read_alerts()
        notifs = [a for a in alerts if a.get('kind') == 'notification']
        self.assertEqual(len(notifs), 1)
        self.assertIn('exhausted', notifs[0]['message'].lower())
        self.assertEqual(notifs[0]['chat_id'], 7998341473)

    def test_replan_budget_remaining_proceeds(self):
        """replan_count=1, max_replans=2 → next=2 ≤ max → allowed."""
        body = self._beacon_replan_outbox(replan_count=1, max_replans=2)
        f = self._write_outbox('beacon', 'beacon-r1.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 1)
        self.assertEqual(state['pending'][0].get('_replan_count'), 2)

    # ----- Malformed marker -----

    def test_malformed_approval_marker_falls_through(self):
        """Malformed APPROVAL_REQUEST (missing required field) logs WARN
        and falls through to default routing — no cascade per 5c sign-off."""
        bad_payload = json.dumps({'task_id': 'task-001'})  # missing fields
        bad_marker = (
            f'=== APPROVAL_REQUEST ===\n{bad_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = self._beacon_replan_outbox(marker_text=bad_marker)
        f = self._write_outbox('beacon', 'beacon-bad.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-replan')

    # ----- Missing reply_chat_id -----

    def test_missing_reply_chat_id_falls_through(self):
        body = self._beacon_replan_outbox(reply_chat_id=None)
        body.pop('reply_chat_id', None)
        f = self._write_outbox('beacon', 'beacon-nochat.json', body)
        result = on.process_outbox(f)
        # Without a chat_id we can't route the approval DM — fall through
        # so the narrative reaches Mirror by default routing.
        self.assertNotEqual(result, 'notified-replan')

    # ----- dispatch_approved propagates replan_count downstream -----

    def test_dispatch_approved_writes_replan_count_to_forge_envelope(self):
        """Round-trip: notifier extracts marker → add_pending(replan_count=1)
        → dispatch_approved → Forge inbox task has replan_count=1 +
        max_replans=2 on the envelope."""
        body = self._beacon_replan_outbox(replan_count=0, max_replans=2)
        f = self._write_outbox('beacon', 'beacon-rt.json', body)
        on.process_outbox(f)
        entry = self._ah.find_pending_by_id('task-001')
        self.assertIsNotNone(entry)
        # Simulate Larry approving — bot calls dispatch_approved
        dest = self._ah.dispatch_approved(entry)
        forge_task = json.loads(Path(dest).read_text())
        self.assertEqual(forge_task['replan_count'], 1)
        self.assertEqual(forge_task['max_replans'], 2)
        self.assertEqual(forge_task['reply_chat_id'], 7998341473)

    # ----- Idempotency (Med-10 review fix) -----

    def test_replay_dedups_pending_by_task_id(self):
        """Med-10 fix: second process_outbox on a duplicate outbox detects
        the existing pending entry and skips add_pending + alert queue."""
        body = self._beacon_replan_outbox()
        f1 = self._write_outbox('beacon', 'beacon-rep.json', body)
        on.process_outbox(f1)
        # Replay — write a fresh copy of the same outbox
        f2 = self._write_outbox('beacon', 'beacon-rep2.json', body)
        result = on.process_outbox(f2)
        # Should be 'notified-replan' (handled by replan path) but dedup
        # short-circuited the duplicate add.
        self.assertEqual(result, 'notified-replan')
        state = self._ah.load_state()
        self.assertEqual(
            len(state.get('pending', [])), 1,
            f'expected 1 pending entry after replay, got {len(state.get("pending", []))}',
        )
        # Exactly one approval-request alert should be queued.
        alerts = self._read_alerts()
        approval_records = [
            a for a in alerts if a.get('kind') == 'approval_request'
        ]
        self.assertEqual(len(approval_records), 1)

    def test_replay_dedups_history_after_auto_approve(self):
        """Med-X1 second-pass fix: replay of a previously-auto-approved outbox
        (entry moved from pending to history) is also dedup'd. Without this,
        a notifier crash between resolve() and outbox archive would let the
        replay re-dispatch the Forge task, overwriting the prior one."""
        self._set_policy(default_action='auto_approve')
        body = self._beacon_replan_outbox()
        f1 = self._write_outbox('beacon', 'beacon-aa1.json', body)
        on.process_outbox(f1)
        # First run: entry should be in history (auto-approved + resolved)
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        self.assertEqual(len(state.get('history', [])), 1)
        # Replay scenario — same outbox content arrives again
        f2 = self._write_outbox('beacon', 'beacon-aa2.json', body)
        result = on.process_outbox(f2)
        self.assertEqual(result, 'notified-replan')
        # Should NOT add a second history entry
        state2 = self._ah.load_state()
        self.assertEqual(len(state2.get('pending', [])), 0)
        self.assertEqual(
            len(state2.get('history', [])), 1,
            f'replay should not double-dispatch; got {len(state2.get("history", []))} history entries',
        )
        # Forge inbox should have exactly one task (the first dispatch);
        # the safe_write_inbox call from the replay would have been skipped
        # by the dedup gate before reaching the inbox write.
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('task-001.json'))
        self.assertEqual(len(forge_tasks), 1)

    # ----- C-1 regression: replan_count propagates through dispatch hops -----

    def test_dispatch_build_phase_propagates_replan_count(self):
        """C-1 fix: replan_count + max_replans flow through preflight→build."""
        # Synthesize a Forge preflight outbox carrying replan_count=1
        preflight = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='preflight', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            result=(
                'Plan looks clean.\n=== PROCEED ===\n'
                '{"task_id": "task-001", "preflight_summary": "Ready."}\n'
                '=== END_PROCEED ==='
            ),
        )
        preflight['replan_count'] = 1
        preflight['max_replans'] = 2
        f = self._write_outbox('forge', 'task-001.json', preflight)
        on.process_outbox(f)
        # Forge's build task should now exist with replan_count=1
        build_tasks = list(
            (on.INBOXES_ROOT / 'forge').glob('build-*.json')
        )
        self.assertEqual(len(build_tasks), 1)
        build = json.loads(build_tasks[0].read_text())
        self.assertEqual(build['replan_count'], 1)
        self.assertEqual(build['max_replans'], 2)

    def test_dispatch_mirror_review_propagates_replan_count(self):
        """C-1 fix: replan_count + max_replans flow through build→review."""
        build = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            result='PR opened: https://github.com/x/y/pull/77\n\nBuild done.',
        )
        build['replan_count'] = 1
        build['max_replans'] = 2
        f = self._write_outbox('forge', 'task-001.json', build)
        on.process_outbox(f)
        review_tasks = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-*.json')
        )
        self.assertEqual(len(review_tasks), 1)
        review = json.loads(review_tasks[0].read_text())
        self.assertEqual(review['replan_count'], 1)
        self.assertEqual(review['max_replans'], 2)

    def test_dispatch_revision_to_forge_propagates_replan_count(self):
        """C-X1 second-pass fix: replan_count + max_replans flow through
        the revision-loop dispatch too. Without this, any task that goes
        through a revision round before re-escalating has the replan
        budget silently reset to 0."""
        marker = _mirror_revision_marker(
            task_id='task-001', confidence='high', severity='medium',
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='task-001',
            target_repo='ourliberty-agent-core',
            branch='forge/task-001',
        )
        body['forge_build_session_id'] = 'sess-abc'
        body['pr_url'] = 'https://github.com/x/y/pull/77'
        body['replan_count'] = 1
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        # Revision task to Forge should carry the replan budget forward.
        revision_tasks = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-task-001-*.json')
        )
        self.assertEqual(len(revision_tasks), 1)
        revision = json.loads(revision_tasks[0].read_text())
        self.assertEqual(revision['replan_count'], 1)
        self.assertEqual(revision['max_replans'], 2)

    def test_dispatch_mirror_review_rerun_propagates_replan_count(self):
        """C-X1 second-pass fix: re-review dispatch also carries replan_count."""
        # Synthesize a Forge revision outbox with replan budget on envelope.
        revision_outbox = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            claude_session_id='sess-abc',
            result=(
                'Revision 1 applied: added validation per Mirror finding.\n\n'
                'Tests pass; pushed to forge/task-001.'
            ),
        )
        revision_outbox['pr_url'] = 'https://github.com/x/y/pull/77'
        revision_outbox['revision_count'] = 1
        revision_outbox['max_revisions'] = 3
        revision_outbox['replan_count'] = 1
        revision_outbox['max_replans'] = 2
        f = self._write_outbox('forge', 'task-001.json', revision_outbox)
        on.process_outbox(f)
        # D3.5 5c-followup-2 HIGH-1 keyed filename when replan_count>0
        rereviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-task-001-replan*-rev*.json')
        )
        self.assertEqual(len(rereviews), 1)
        rereview = json.loads(rereviews[0].read_text())
        self.assertEqual(rereview['replan_count'], 1)
        self.assertEqual(rereview['max_replans'], 2)

    # ----- C-2 regression: findings text augments mirror_escalate_reason -----

    def test_auto_promoted_escalate_reason_includes_findings(self):
        """C-2 fix: low-confidence REVISION auto-promoted to ESCALATE must
        carry Mirror's finding descriptions in mirror_escalate_reason, not
        just the procedural framing. Without findings in the reason text,
        Beacon's level-3 discipline gate can never find token overlap."""
        marker = _mirror_revision_marker(
            task_id='task-001', confidence='low',
            findings=[
                {'file': 'parser.py', 'line_range': 'L12-L15',
                 'severity': 'medium',
                 'description': 'Missing input validation for malformed inputs.'},
            ],
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='task-001',
            target_repo='ourliberty-agent-core',
        )
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        notify = json.loads(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))[0].read_text()
        )
        # Reason should contain procedural text AND finding description
        reason = notify.get('mirror_escalate_reason', '')
        self.assertIn('Findings:', reason)
        self.assertIn('input validation', reason)
        # And it should still mention the auto-promote (procedural framing)
        self.assertIn('auto-promot', reason.lower())

    def test_budget_exhausted_escalate_reason_includes_findings(self):
        """C-2 fix: budget-exhausted REVISION downgrade to ESCALATE must
        also carry findings in the reason."""
        marker = _mirror_revision_marker(
            task_id='task-001', confidence='high',
            findings=[
                {'file': 'parser.py', 'line_range': 'L20',
                 'severity': 'medium',
                 'description': 'Edge case for empty input not handled.'},
            ],
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='task-001',
            target_repo='ourliberty-agent-core',
        )
        body['revision_count'] = 3  # at the max, next would exhaust
        body['max_revisions'] = 3
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        notify = json.loads(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))[0].read_text()
        )
        reason = notify.get('mirror_escalate_reason', '')
        self.assertIn('Findings:', reason)
        self.assertIn('empty input', reason)
        self.assertIn('budget', reason.lower())

    # ----- Med-11: missing test coverage -----

    def test_auto_approve_trust_path_dispatches_immediately(self):
        """When trust_policy yields auto_approve, the notifier dispatches
        the new Forge task directly (no Larry DM beyond the confirmation
        notification) and resolves the pending entry as approved."""
        self._set_policy(default_action='auto_approve')
        body = self._beacon_replan_outbox()
        f = self._write_outbox('beacon', 'beacon-aa.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')
        # Forge inbox has the dispatched task
        forge_tasks = list(
            (on.INBOXES_ROOT / 'forge').glob('task-001.json')
        )
        self.assertEqual(len(forge_tasks), 1)
        forge_task = json.loads(forge_tasks[0].read_text())
        self.assertEqual(forge_task['replan_count'], 1)
        self.assertEqual(forge_task['max_replans'], 2)
        self.assertEqual(forge_task['reply_chat_id'], 7998341473)
        # Pending entry is resolved + in history
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        history = state.get('history', [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['status'], 'approved')
        # Confirmation notification queued
        alerts = self._read_alerts()
        notifs = [a for a in alerts if a.get('kind') == 'notification']
        self.assertEqual(len(notifs), 1)
        # NO approval-request alert (auto-approve skips the force_ask path)
        approval_records = [
            a for a in alerts if a.get('kind') == 'approval_request'
        ]
        self.assertEqual(len(approval_records), 0)

    def test_reject_trust_path_dms_larry_without_dispatch(self):
        """When trust_policy yields reject, the notifier DMs Larry the
        policy rejection and does NOT add a pending entry."""
        self._set_policy(default_action='reject')
        body = self._beacon_replan_outbox()
        f = self._write_outbox('beacon', 'beacon-rj.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')
        # No pending entry
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        # Rejection notification queued
        alerts = self._read_alerts()
        notifs = [
            a for a in alerts
            if a.get('kind') == 'notification' and a.get('intent') == 'reject'
        ]
        self.assertEqual(len(notifs), 1)
        # No Forge task dispatched
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('task-001.json'))
        self.assertEqual(len(forge_tasks), 0)

    def test_inbound_intent_propagates_through_full_chain(self):
        """End-to-end: Mirror REVIEW_ESCALATE → notifier writes notify to
        Beacon's inbox with intent=review-escalate → inbox_watcher fires
        Beacon (here simulated by reading the notify file) → outbox would
        carry inbound_intent=review-escalate. We test the inbox-side
        contract: the notify task on Beacon's inbox has `intent=review-
        escalate`, which inbox_watcher._build_outbox would surface as
        `inbound_intent` on the outbox."""
        marker = _mirror_escalate_marker(task_id='task-001')
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='task-001',
            target_repo='ourliberty-agent-core',
        )
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify = json.loads(notifies[0].read_text())
        # This is the field inbox_watcher reads to populate inbound_intent
        self.assertEqual(notify['intent'], 'review-escalate')

    def test_non_escalate_intent_omits_replan_count_on_notify(self):
        """Regression: a review-pass / review-revision (mid-chain) marker
        decision should NOT add replan_count + max_replans to the notify
        task. Only review-escalate carries the replan budget."""
        marker = _mirror_pass_marker(task_id='task-001')
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='task-001',
            target_repo='ourliberty-agent-core',
        )
        # Even if envelope has replan_count from a prior cycle, the
        # marker-driven routing should only carry it on escalate notifies.
        body['replan_count'] = 1
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        notify = json.loads(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))[0].read_text()
        )
        self.assertNotIn('replan_count', notify)
        self.assertNotIn('max_replans', notify)
        self.assertNotIn('mirror_escalate_reason', notify)


# Med-8 regression test lives in test_beacon_approval_handler — the
# adapt-threshold logic is in validate_replan_discipline. See
# ValidateReplanDisciplineTest.test_short_mirror_reason_adapts_threshold.


# -------------------- D3.5 5c-followup-2 (audit C-1 + C-2 + Miss #3) --------------------


class ReplanDispatchKeyingTest(unittest.TestCase):
    """C-1 + C-2 audit fixes: build/review filenames key by replan_count.

    Without round-keyed filenames, the .archive/ dedup check on iteration 2
    would silently drop the dispatch — defeating the replan loop entirely.
    These tests fail on the pre-fix code: iteration 1 lands the file at
    `build-<task>.json`, iteration 2 also tries `build-<task>.json`, finds it
    archived, returns. Round-keying separates them.
    """

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

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    # ----- C-1: _dispatch_build_phase keyed by replan_count -----

    def test_build_dispatch_keyed_by_replan_count_iteration_2(self):
        """Replan iteration 2 must NOT collide with iteration 1's archive."""
        # Simulate iteration 1 having archived a build task
        forge_inbox_archive = on.INBOXES_ROOT / 'forge' / '.archive'
        forge_inbox_archive.mkdir(parents=True, exist_ok=True)
        (forge_inbox_archive / 'build-task-001.json').write_text('{}')

        # Iteration 2 preflight proceeds — dispatch should land at the
        # replan-keyed filename, not collide
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "task-001", "preflight_summary": "Iteration 2 ready."}\n'
            '=== END_PROCEED ==='
        )
        preflight = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='preflight', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            claude_session_id='sess-iter2-xyz',
            result=f'Ready.\n\n{marker}',
        )
        preflight['replan_count'] = 1
        preflight['max_replans'] = 2
        f = self._write_outbox('forge', 'task-001.json', preflight)
        on.process_outbox(f)

        # New build task lands at the keyed filename
        keyed = list((on.INBOXES_ROOT / 'forge').glob('build-task-001-replan1.json'))
        self.assertEqual(
            len(keyed), 1,
            f'expected build-task-001-replan1.json (round-keyed); '
            f'inbox contents: {list((on.INBOXES_ROOT / "forge").iterdir())}',
        )

    def test_build_dispatch_unkeyed_when_replan_count_zero(self):
        """Backward-compat: replan_count=0 (or missing) uses the legacy
        unkeyed filename so prior idempotency behavior is preserved."""
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "task-001", "preflight_summary": "Fresh dispatch."}\n'
            '=== END_PROCEED ==='
        )
        preflight = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='preflight', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            claude_session_id='sess-fresh-xyz',
            result=f'Ready.\n\n{marker}',
        )
        # replan_count omitted (defaults to 0)
        f = self._write_outbox('forge', 'task-001.json', preflight)
        on.process_outbox(f)

        legacy = list((on.INBOXES_ROOT / 'forge').glob('build-task-001.json'))
        self.assertEqual(len(legacy), 1)
        keyed = list((on.INBOXES_ROOT / 'forge').glob('build-task-001-replan*.json'))
        self.assertEqual(len(keyed), 0)

    # ----- C-2: _dispatch_mirror_review keyed by replan_count -----

    def test_mirror_review_dispatch_keyed_by_replan_count_iteration_2(self):
        """Replan iteration 2's first Mirror review must NOT collide with
        iteration 1's archived review task."""
        mirror_inbox_archive = on.INBOXES_ROOT / 'mirror' / '.archive'
        mirror_inbox_archive.mkdir(parents=True, exist_ok=True)
        (mirror_inbox_archive / 'review-task-001.json').write_text('{}')

        build = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            result='PR opened: https://github.com/x/y/pull/77\n\nBuild done.',
        )
        build['replan_count'] = 1
        build['max_replans'] = 2
        f = self._write_outbox('forge', 'task-001.json', build)
        on.process_outbox(f)

        keyed = list((on.INBOXES_ROOT / 'mirror').glob('review-task-001-replan1.json'))
        self.assertEqual(
            len(keyed), 1,
            f'expected review-task-001-replan1.json (round-keyed); '
            f'inbox contents: {list((on.INBOXES_ROOT / "mirror").iterdir())}',
        )

    def test_mirror_review_dispatch_unkeyed_when_replan_count_zero(self):
        """Backward-compat: round 1 still uses the legacy unkeyed filename."""
        build = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            result='PR opened: https://github.com/x/y/pull/77\n\nBuild done.',
        )
        # replan_count omitted
        f = self._write_outbox('forge', 'task-001.json', build)
        on.process_outbox(f)

        legacy = list((on.INBOXES_ROOT / 'mirror').glob('review-task-001.json'))
        self.assertEqual(len(legacy), 1)
        keyed = list((on.INBOXES_ROOT / 'mirror').glob('review-task-001-replan*.json'))
        self.assertEqual(len(keyed), 0)

    # ----- HIGH-1 (PR #10 review): combined-state revision filename keying -----

    def test_revision_dispatch_keyed_by_replan_count_combined_state(self):
        """HIGH-1: revision dispatch filename ALSO keys by replan_count to
        avoid colliding with prior replan iteration's archived revisions.
        Without this, replan iter 2's first revision (revision_count=1)
        would collide with replan iter 1's archived revision-{task}-1.json
        and silently drop — same shape as C-1/C-2 on the inner loop."""
        # Seed archive with replan-iter-1's revision file (the collision target)
        forge_archive = on.INBOXES_ROOT / 'forge' / '.archive'
        forge_archive.mkdir(parents=True, exist_ok=True)
        (forge_archive / 'revision-task-001-1.json').write_text('{}')

        # Synthesize Mirror REVIEW_REVISION outbox for replan iter 2
        marker_payload = json.dumps({
            'task_id': 'task-001',
            'pr_url': 'https://github.com/x/y/pull/77',
            'findings': [{
                'file': 'foo.py', 'line_range': 'L12-L15',
                'severity': 'medium',
                'description': 'Missing validation.',
            }],
            'severity': 'medium', 'confidence': 'high',
        })
        marker = (
            f'=== REVIEW_REVISION ===\n{marker_payload}\n'
            f'=== END_REVIEW_REVISION ==='
        )
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='task-001',
            phase='review', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            result=f'Findings.\n\n{marker}',
        )
        body['forge_build_session_id'] = 'sess-abc'
        body['pr_url'] = 'https://github.com/x/y/pull/77'
        body['replan_count'] = 1  # replan iteration 2
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'task-001.json', body)
        on.process_outbox(f)
        # Revision task should land at the replan-keyed filename, not collide
        keyed = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-task-001-replan1-*.json')
        )
        self.assertEqual(
            len(keyed), 1,
            f'expected replan-keyed revision filename; inbox: '
            f'{list((on.INBOXES_ROOT / "forge").iterdir())}',
        )
        # And the legacy unkeyed filename should NOT have been written
        # (the archived collider is still the only one there)
        legacy_inbox = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-task-001-1.json')
        )
        self.assertEqual(len(legacy_inbox), 0)

    def test_rerun_review_dispatch_keyed_by_replan_count_combined_state(self):
        """HIGH-1 sibling: _dispatch_mirror_review_rerun also keys by
        replan_count to avoid colliding with prior replan iteration's
        archived re-reviews."""
        mirror_archive = on.INBOXES_ROOT / 'mirror' / '.archive'
        mirror_archive.mkdir(parents=True, exist_ok=True)
        (mirror_archive / 'review-task-001-rev1.json').write_text('{}')

        revision_outbox = _good_outbox(
            agent='forge', source='beacon', task_id='task-001',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/task-001',
            claude_session_id='sess-abc',
            result=(
                'Revision 1 applied: validation added per Mirror finding.\n'
                '\nTests pass; pushed to forge/task-001.'
            ),
        )
        revision_outbox['pr_url'] = 'https://github.com/x/y/pull/77'
        revision_outbox['revision_count'] = 1
        revision_outbox['max_revisions'] = 3
        revision_outbox['replan_count'] = 1
        revision_outbox['max_replans'] = 2
        f = self._write_outbox('forge', 'task-001.json', revision_outbox)
        on.process_outbox(f)
        keyed = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-task-001-replan1-rev*.json')
        )
        self.assertEqual(len(keyed), 1)
        legacy = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-task-001-rev1.json')
        )
        self.assertEqual(len(legacy), 0)


class PRUrlRegexAcceptsBothPrefixesTest(unittest.TestCase):
    """Miss #3 fix: _PR_URL_RE accepts `PR opened:` OR `PR updated:` as
    first-line prefix. Forge's CLAUDE.md drift on existing-PR-update flows
    surfaced when she led with status narrative and put `PR opened:` as
    paragraph 2 — the strict anchor missed, auto-Mirror-review didn't fire."""

    def test_pr_opened_still_matches(self):
        result = 'PR opened: https://github.com/x/y/pull/42\n\nDid the work.'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/42',
        )

    def test_pr_updated_matches(self):
        result = 'PR updated: https://github.com/x/y/pull/8\n\nAdded a commit to the existing PR.'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/8',
        )

    def test_pr_opened_at_paragraph_2_now_matches(self):
        """5d-followup-2: line-anchor relaxation. Forge's lenient build-phase
        shape (status narrative then URL on its own line) now wins. This was
        the silent-drop failure mode for the 5c fill-in dispatch and PR #20's
        sibling — both had the URL on a non-first line and missed Mirror
        review under the prior \\A anchor."""
        result = (
            "Commit `8e5c692` pushed to `forge/task-001`, the head branch "
            "of PR #8 (OPEN).\n\nPR opened: https://github.com/x/y/pull/8"
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/8',
        )

    def test_pr_updated_at_paragraph_2_now_matches(self):
        """Symmetric line-anchor check for the `updated` alternative."""
        result = (
            "Commit added to existing PR.\n\n"
            "PR updated: https://github.com/x/y/pull/8"
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/8',
        )

    def test_pr_opened_with_leading_whitespace_matches(self):
        """Regex allows leading horizontal whitespace on the prefix line."""
        result = '   PR opened: https://github.com/x/y/pull/1\n'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/1',
        )

    def test_invalid_pr_url_returns_none(self):
        result = 'PR opened: not-a-url\n'
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

    def test_pr_other_verb_does_not_match(self):
        """Only opened|updated. Not closed|merged|reopened — those would
        signal terminal states that shouldn't auto-fire review."""
        result = 'PR closed: https://github.com/x/y/pull/8\n'
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

    def test_newline_between_PR_and_verb_does_not_match(self):
        """HIGH-2 (PR #10 review): the verb must be on the SAME line as PR.
        `\\s` would have accepted `PR\\nopened: <url>` which violates the
        CLAUDE.md discipline contract. Tightened to `[ \\t]` so the regex
        matches the discipline rule."""
        result = 'PR\nopened: https://github.com/x/y/pull/1'
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

    def test_leading_blank_line_then_PR_matches(self):
        """5d-followup-2: with the line-anchor relaxation, a leading blank
        line before `PR opened:` is fine — the URL is still on its own line
        at line-start. Symmetric with the narrative-before-URL case."""
        result = '\nPR opened: https://github.com/x/y/pull/1'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/1',
        )


# -------------------- D3.5 5c-followup-3 (audit 3.A + 5.A) --------------------


class BeaconReplanPauseTest(unittest.TestCase):
    """Audit 5.A fix: notifier-side replan path respects approval.is_paused().

    Without this, Beacon's auto-replan APPROVAL_REQUEST during a /pause
    would DM Larry the approval prompt, violating the pause contract.
    """

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
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        self._la = la
        import beacon_approval_handler as ah
        self._ah_original = ah.PENDING_APPROVALS_PATH
        self._ah_paused_flag = ah.APPROVALS_PAUSED_FLAG
        ah.PENDING_APPROVALS_PATH = self._root / 'state' / 'pending-approvals.json'
        ah.APPROVALS_PAUSED_FLAG = self._root / 'blackboard' / 'APPROVALS_PAUSED'
        self._ah = ah
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(self._la, name, value)
        self._ah.PENDING_APPROVALS_PATH = self._ah_original
        self._ah.APPROVALS_PAUSED_FLAG = self._ah_paused_flag
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _read_alerts(self):
        if not self._la.ALERTS_FILE.exists():
            return []
        lines = self._la.ALERTS_FILE.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _beacon_replan_outbox(self):
        marker_payload = json.dumps({
            'task_id': 'task-001',
            'summary': (
                "Address Mirror's input validation concern in the parser."
            ),
            'target_agent': 'forge',
            'prompt': 'x' * 200,
            'target_repo': 'ourliberty-agent-core',
            'task_type': 'feature-development',
        })
        marker = (
            f'=== APPROVAL_REQUEST ===\n{marker_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = _good_outbox(
            agent='beacon',
            source='mirror-result',
            task_id='notify-task-001',
            result=f'Revising.\n\n{marker}',
        )
        body['inbound_intent'] = 'review-escalate'
        body['replan_count'] = 0
        body['max_replans'] = 2
        body['mirror_escalate_reason'] = (
            'Missing input validation in the parser.'
        )
        body['reply_chat_id'] = 7998341473
        return body

    def test_pause_queues_replan_entry_without_alert(self):
        """During /pause, the replan entry persists with queued_during_pause
        but no approval-request alert is queued. /resume surfaces backlog."""
        self._ah.set_paused(True)
        body = self._beacon_replan_outbox()
        f = self._write_outbox('beacon', 'beacon-paused.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')
        state = self._ah.load_state()
        pending = state.get('pending', [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['id'], 'task-001')
        self.assertTrue(pending[0].get('queued_during_pause'))
        self.assertEqual(pending[0].get('_replan_count'), 1)
        # No approval-request alert
        alerts = self._read_alerts()
        approval_records = [
            a for a in alerts if a.get('kind') == 'approval_request'
        ]
        self.assertEqual(
            len(approval_records), 0,
            'pause must suppress the approval-request DM',
        )

    def test_pause_resume_surfaces_replan_backlog(self):
        """After /resume, pop_paused_backlog returns the queued replan
        entry with its _replan_count intact for the bot to DM."""
        self._ah.set_paused(True)
        body = self._beacon_replan_outbox()
        f = self._write_outbox('beacon', 'beacon-resume.json', body)
        on.process_outbox(f)
        self._ah.set_paused(False)
        backlog = self._ah.pop_paused_backlog()
        self.assertEqual(len(backlog), 1)
        self.assertEqual(backlog[0]['id'], 'task-001')
        self.assertEqual(backlog[0].get('_replan_count'), 1)

    def test_no_pause_proceeds_normally(self):
        """Sanity: no pause → alert queued as before."""
        body = self._beacon_replan_outbox()
        f = self._write_outbox('beacon', 'beacon-normal.json', body)
        on.process_outbox(f)
        alerts = self._read_alerts()
        approval_records = [
            a for a in alerts if a.get('kind') == 'approval_request'
        ]
        self.assertEqual(len(approval_records), 1)


# -------------------- D3.5 5d — auto-merge + EMERGENCY_HALT + cost gate --------------------


class ParsePrUrlTest(unittest.TestCase):
    """D3.5 5d — `_parse_pr_url` extracts (repo_coords, pr_number)."""

    def test_canonical_url(self):
        out = on._parse_pr_url(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42',
        )
        self.assertEqual(out, ('Larry-Yatch/ourliberty-agent-core', 42))

    def test_trailing_slash(self):
        out = on._parse_pr_url(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42/',
        )
        self.assertEqual(out, ('Larry-Yatch/ourliberty-agent-core', 42))

    def test_with_query_string(self):
        out = on._parse_pr_url(
            'https://github.com/owner/repo/pull/123?foo=bar',
        )
        self.assertEqual(out, ('owner/repo', 123))

    def test_with_fragment(self):
        out = on._parse_pr_url(
            'https://github.com/owner/repo/pull/123#issuecomment-456',
        )
        self.assertEqual(out, ('owner/repo', 123))

    def test_with_files_suffix(self):
        # gh emits both bare and `/files` etc. URLs.
        out = on._parse_pr_url('https://github.com/owner/repo/pull/7/files')
        self.assertEqual(out, ('owner/repo', 7))

    def test_not_a_pr_url(self):
        self.assertIsNone(on._parse_pr_url(
            'https://github.com/owner/repo/issues/42',
        ))

    def test_not_github(self):
        self.assertIsNone(on._parse_pr_url('https://example.com/foo/bar/pull/1'))

    def test_empty_string(self):
        self.assertIsNone(on._parse_pr_url(''))

    def test_none(self):
        self.assertIsNone(on._parse_pr_url(None))

    def test_zero_pr_number(self):
        self.assertIsNone(on._parse_pr_url(
            'https://github.com/owner/repo/pull/0',
        ))


class AutoMergePRTest(unittest.TestCase):
    """D3.5 5d — `_auto_merge_pr` shell-out semantics + outcome distinguishing."""

    PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
    TASK_ID = 'test-task-001'

    def _mock_run(self, *, returncode=0, stdout='', stderr=''):
        """Build a fake subprocess.CompletedProcess-shaped result."""
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def test_success_outcome(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._mock_run(returncode=0)
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'merged')
        self.assertEqual(result['pr_number'], 42)
        self.assertEqual(result['repo_coords'], 'Larry-Yatch/ourliberty-agent-core')

    def test_gh_command_shape(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._mock_run(returncode=0)
            on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        args, kwargs = m_run.call_args
        cmd = args[0]
        self.assertEqual(cmd[:3], ['gh', 'pr', 'merge'])
        self.assertIn('42', cmd)
        self.assertIn('--squash', cmd)
        self.assertIn('--delete-branch', cmd)
        self.assertIn('--repo', cmd)
        # Repo coords directly after --repo
        repo_idx = cmd.index('--repo')
        self.assertEqual(cmd[repo_idx + 1], 'Larry-Yatch/ourliberty-agent-core')

    def test_failure_conflict_distinguishable(self):
        """gh exit != 0 AND state != MERGED → failed."""
        merge_proc = self._mock_run(
            returncode=1, stderr='not mergeable: the merge commit cannot be cleanly created',
        )
        view_proc = self._mock_run(
            returncode=0, stdout=json.dumps({'state': 'OPEN'}),
        )
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertIn('not mergeable', result['merge_reason'])
        self.assertEqual(result['pr_number'], 42)

    def test_failure_branch_protection(self):
        merge_proc = self._mock_run(
            returncode=1,
            stderr='Pull request not mergeable: required reviews are not approved',
        )
        view_proc = self._mock_run(
            returncode=0, stdout=json.dumps({'state': 'OPEN'}),
        )
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertIn('required reviews', result['merge_reason'])

    def test_failure_auth_expired(self):
        merge_proc = self._mock_run(
            returncode=1,
            stderr='HTTP 401: Bad credentials (https://api.github.com/...)',
        )
        view_proc = self._mock_run(
            returncode=1, stderr='HTTP 401: Bad credentials',
        )
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        # state recheck also failed → state=None; outcome is still failed
        # but the reason carries the original merge stderr.
        self.assertIn('Bad credentials', result['merge_reason'])

    def test_failure_timeout(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = on.subprocess.TimeoutExpired(
                cmd=['gh'], timeout=on._AUTO_MERGE_TIMEOUT_S,
            )
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertIn('timed out', result['merge_reason'])

    def test_failure_gh_missing(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = FileNotFoundError('gh')
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertIn('gh CLI not found', result['merge_reason'])

    def test_failure_os_error(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = OSError('Broken pipe')
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertIn('Broken pipe', result['merge_reason'])

    def test_malformed_pr_url_no_shellout(self):
        """A garbage pr_url should not attempt the gh shell-out at all."""
        with mock.patch.object(on.subprocess, 'run') as m_run:
            result = on._auto_merge_pr('not-a-url', self.TASK_ID)
        m_run.assert_not_called()
        self.assertEqual(result['merge_outcome'], 'failed')
        self.assertIn('malformed', result['merge_reason'].lower())

    def test_already_merged_resume_path(self):
        """Non-zero gh merge BUT state=MERGED → already_merged (success)."""
        merge_proc = self._mock_run(
            returncode=1, stderr='Pull request is already merged',
        )
        view_proc = self._mock_run(
            returncode=0, stdout=json.dumps({'state': 'MERGED'}),
        )
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'already_merged')
        self.assertEqual(result['pr_number'], 42)
        self.assertEqual(result['repo_coords'], 'Larry-Yatch/ourliberty-agent-core')


class AlreadyMergedResumeTest(unittest.TestCase):
    """D3.5 5d — resume after daemon crash: PR was merged on first pass,
    second pass sees gh failure but state=MERGED → success-shaped DM."""

    PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'

    def test_already_merged_dm_body_is_success_shape(self):
        """The DM body for `already_merged` should be the success body,
        not the failure body — resume-after-crash is a non-failure path."""
        decision = {
            'intent': 'review-pass',
            'payload': {
                'task_id': 't-resume',
                'pr_url': self.PR_URL,
                'summary': 'Resume after crash',
            },
            'intent_kwargs': {
                'pr_url': self.PR_URL,
                'summary': 'Resume after crash',
            },
            'merge_outcome': 'already_merged',
            'merge_result': {
                'merge_outcome': 'already_merged',
                'merge_reason': 'PR was already merged (resume from prior dispatch)',
                'pr_number': 42,
                'repo_coords': 'Larry-Yatch/ourliberty-agent-core',
            },
        }
        body = on._render_dm_message('review-pass', decision)
        self.assertIsNotNone(body)
        self.assertIn('Auto-merged + branch deleted', body)
        # The success shape does NOT contain "FAILED".
        self.assertNotIn('FAILED', body)

    def test_failed_outcome_dm_body_is_failure_shape(self):
        decision = {
            'intent': 'review-pass',
            'payload': {
                'task_id': 't-fail',
                'pr_url': self.PR_URL,
                'summary': 'A summary',
            },
            'intent_kwargs': {
                'pr_url': self.PR_URL,
                'summary': 'A summary',
            },
            'merge_outcome': 'failed',
            'merge_result': {
                'merge_outcome': 'failed',
                'merge_reason': 'conflict on the merge commit',
                'pr_number': 42,
                'repo_coords': 'Larry-Yatch/ourliberty-agent-core',
            },
        }
        body = on._render_dm_message('review-pass', decision)
        self.assertIsNotNone(body)
        self.assertIn('Auto-merge FAILED', body)
        self.assertIn('conflict on the merge commit', body)
        self.assertIn('gh pr merge 42', body)
        self.assertIn('Larry-Yatch/ourliberty-agent-core', body)

    def test_merged_outcome_dm_body(self):
        decision = {
            'intent': 'review-pass',
            'payload': {
                'task_id': 't-ok',
                'pr_url': self.PR_URL,
                'summary': 'Done',
            },
            'intent_kwargs': {
                'pr_url': self.PR_URL,
                'summary': 'Done',
            },
            'merge_outcome': 'merged',
            'merge_result': {
                'merge_outcome': 'merged',
                'merge_reason': 'squash-merged + branch deleted',
                'pr_number': 42,
                'repo_coords': 'Larry-Yatch/ourliberty-agent-core',
            },
        }
        body = on._render_dm_message('review-pass', decision)
        self.assertIsNotNone(body)
        self.assertIn('Auto-merged + branch deleted', body)
        self.assertNotIn('FAILED', body)


class EmergencyHaltTripTest(unittest.TestCase):
    """D3.5 5d — `_trip_emergency_halt` writes the halt file + queues priority DM."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {
            'AGENTS_ROOT': on.AGENTS_ROOT,
            'BLACKBOARD': on.BLACKBOARD,
            'EMERGENCY_HALT_FLAG': on.EMERGENCY_HALT_FLAG,
            'LOG_FILE': on.LOG_FILE,
        }
        on.AGENTS_ROOT = self._root
        on.BLACKBOARD = self._root / 'blackboard'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        on.LOG_FILE = self._root / 'logs' / 'notifier.log'
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        on.BLACKBOARD.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        self._tmp.cleanup()

    def _data(self, task_id='t-halt', reply_chat_id=None):
        d = {'task_id': task_id, 'agent': 'mirror'}
        if reply_chat_id is not None:
            d['reply_chat_id'] = reply_chat_id
        return d

    def _payload(self, reason='credentials in diff', evidence='line 42: API_KEY=...',
                 pr_url='https://github.com/owner/repo/pull/1'):
        return {'reason': reason, 'evidence': evidence, 'pr_url': pr_url,
                'task_id': 't-halt'}

    def _read_alerts(self):
        import larry_alerts as la
        if not la.ALERTS_FILE.exists():
            return []
        return [json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines() if line.strip()]

    def test_halt_file_written_with_envelope(self):
        on._trip_emergency_halt(self._data(), self._payload())
        self.assertTrue(on.EMERGENCY_HALT_FLAG.exists())
        env = json.loads(on.EMERGENCY_HALT_FLAG.read_text())
        self.assertEqual(env['activated_by'], 'mirror-marker')
        self.assertEqual(env['task_id'], 't-halt')
        self.assertEqual(env['reason'], 'credentials in diff')
        self.assertIn('API_KEY', env['evidence'])
        self.assertEqual(env['pr_url'], 'https://github.com/owner/repo/pull/1')
        self.assertIn('activated_at', env)

    def test_halt_idempotent_on_re_trip(self):
        """Re-tripping should NOT overwrite an existing halt file."""
        on.EMERGENCY_HALT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        on.EMERGENCY_HALT_FLAG.write_text(json.dumps({
            'activated_at': '2026-05-14T12:00:00Z',
            'activated_by': 'operator',
            'reason': 'operator-triggered halt earlier today',
        }) + '\n')
        on._trip_emergency_halt(self._data(), self._payload())
        env = json.loads(on.EMERGENCY_HALT_FLAG.read_text())
        # Existing operator envelope is preserved.
        self.assertEqual(env['activated_by'], 'operator')
        self.assertEqual(env['reason'], 'operator-triggered halt earlier today')

    def test_alert_queued_kind_alert(self):
        on._trip_emergency_halt(self._data(), self._payload())
        alerts = self._read_alerts()
        emergency_alerts = [
            a for a in alerts
            if a.get('source') == 'outbox-notifier'
            and a.get('severity') == 'critical'
            and 'emergency-halt' in (a.get('subject') or '')
        ]
        self.assertEqual(len(emergency_alerts), 1)
        a = emergency_alerts[0]
        # kind: alert (no explicit kind field — bot reads missing/alert as broadcast)
        self.assertNotIn('chat_id', a)  # not targeted; broadcast shape
        self.assertEqual(a['severity'], 'critical')
        self.assertEqual(a['subject'], 'emergency-halt:t-halt')
        self.assertIn('EMERGENCY_HALT tripped', a['message'])

    def test_alert_suggested_action_includes_recovery_command(self):
        # D3.5 5d code-review-fix #15: recovery command lives in the
        # `suggested_action` field of the alert record only — not
        # duplicated in the message body (which would double-render on
        # phone). Bot renderer concatenates both fields when DMing.
        on._trip_emergency_halt(self._data(), self._payload())
        alerts = self._read_alerts()
        a = next(x for x in alerts if x.get('severity') == 'critical')
        self.assertEqual(
            a.get('suggested_action'),
            'python3 ~/agent-core/scripts/kill_switch.py resume',
        )
        # Body should NOT contain the recovery command (no duplication).
        self.assertNotIn('kill_switch.py resume', a['message'])

    def test_per_task_cooldown_bucket(self):
        """Two halts for different task_ids should both queue alerts;
        the per-task subject avoids cross-task suppression."""
        on._trip_emergency_halt(self._data(task_id='t-A'), self._payload())
        # Remove file so the second trip's idempotent check doesn't skip
        # (the alert path is separate from the halt-file path, but both
        # share the data-flow we're testing).
        on.EMERGENCY_HALT_FLAG.unlink()
        on._trip_emergency_halt(self._data(task_id='t-B'), self._payload())
        alerts = self._read_alerts()
        emergency = [a for a in alerts if a.get('severity') == 'critical']
        subjects = {a.get('subject') for a in emergency}
        self.assertIn('emergency-halt:t-A', subjects)
        self.assertIn('emergency-halt:t-B', subjects)

    def test_same_task_cooldown_suppresses(self):
        """Re-tripping for the SAME task within 10 min — second alert
        suppressed by cooldown bucket (subject keyed on task_id)."""
        on._trip_emergency_halt(self._data(task_id='t-A'), self._payload())
        on.EMERGENCY_HALT_FLAG.unlink()
        on._trip_emergency_halt(self._data(task_id='t-A'), self._payload())
        alerts = self._read_alerts()
        emergency = [a for a in alerts if a.get('severity') == 'critical']
        self.assertEqual(len(emergency), 1)  # second suppressed

    def test_missing_payload_fields_dont_crash(self):
        on._trip_emergency_halt(self._data(), {})  # no reason/evidence/pr_url
        self.assertTrue(on.EMERGENCY_HALT_FLAG.exists())
        env = json.loads(on.EMERGENCY_HALT_FLAG.read_text())
        self.assertEqual(env['reason'], '(no reason)')
        self.assertEqual(env['evidence'], '(no evidence)')


class CostBudgetGateTest(unittest.TestCase):
    """D3.5 5d — `_check_cost_budget` + `_enforce_cost_budget` semantics."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {
            'AGENTS_ROOT': on.AGENTS_ROOT,
            'BLACKBOARD': on.BLACKBOARD,
            'COSTS_FILE': on.COSTS_FILE,
            'LOG_FILE': on.LOG_FILE,
        }
        on.AGENTS_ROOT = self._root
        on.BLACKBOARD = self._root / 'blackboard'
        on.COSTS_FILE = on.BLACKBOARD / 'costs.jsonl'
        on.LOG_FILE = self._root / 'logs' / 'notifier.log'
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        on.BLACKBOARD.mkdir(parents=True, exist_ok=True)
        # D3.5 5d code-review-fix #4: per-task DM dedup is daemon-lifetime
        # state. Reset between test cases so re-enforcement tests aren't
        # tainted by prior cases that landed the same task_id in the set.
        on._reset_cost_budget_dmed_tasks()
        self.addCleanup(on._reset_cost_budget_dmed_tasks)

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        self._tmp.cleanup()

    def _write_costs(self, *records):
        with open(on.COSTS_FILE, 'a', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def test_missing_ledger_allows_dispatch(self):
        at_cap, current, cap = on._check_cost_budget('t-x', cap_usd=5.0)
        self.assertFalse(at_cap)
        self.assertEqual(current, 0.0)
        self.assertEqual(cap, 5.0)

    def test_below_cap_allows_dispatch(self):
        self._write_costs(
            {'task_id': 't-x', 'cost_usd': 1.0},
            {'task_id': 't-x', 'cost_usd': 2.0},
            {'task_id': 't-other', 'cost_usd': 999.0},  # different task; ignored
        )
        at_cap, current, cap = on._check_cost_budget('t-x', cap_usd=5.0)
        self.assertFalse(at_cap)
        self.assertEqual(current, 3.0)

    def test_at_cap_refuses_dispatch(self):
        self._write_costs(
            {'task_id': 't-x', 'cost_usd': 3.0},
            {'task_id': 't-x', 'cost_usd': 2.0},
        )
        at_cap, current, cap = on._check_cost_budget('t-x', cap_usd=5.0)
        self.assertTrue(at_cap)
        self.assertEqual(current, 5.0)

    def test_over_cap_refuses_dispatch(self):
        self._write_costs(
            {'task_id': 't-x', 'cost_usd': 4.0},
            {'task_id': 't-x', 'cost_usd': 2.0},
        )
        at_cap, current, cap = on._check_cost_budget('t-x', cap_usd=5.0)
        self.assertTrue(at_cap)
        self.assertEqual(current, 6.0)

    def test_malformed_lines_skipped(self):
        # Mix of valid + malformed (not JSON, wrong shape, missing fields)
        # — gate stays robust.
        with open(on.COSTS_FILE, 'w', encoding='utf-8') as f:
            f.write('{"task_id": "t-x", "cost_usd": 1.0}\n')
            f.write('not json at all\n')
            f.write('{"task_id": "t-x"}\n')  # no cost_usd
            f.write('{}\n')                   # no task_id
            f.write('[1,2,3]\n')              # not a dict
            f.write('{"task_id": "t-x", "cost_usd": 2.0}\n')
            f.write('\n')                     # empty line
        at_cap, current, _ = on._check_cost_budget('t-x', cap_usd=5.0)
        self.assertFalse(at_cap)
        self.assertEqual(current, 3.0)

    def test_negative_cost_ignored(self):
        # Defensive — negative cost_usd is meaningless; skip.
        self._write_costs(
            {'task_id': 't-x', 'cost_usd': 1.0},
            {'task_id': 't-x', 'cost_usd': -100.0},
            {'task_id': 't-x', 'cost_usd': 1.0},
        )
        at_cap, current, _ = on._check_cost_budget('t-x', cap_usd=5.0)
        self.assertEqual(current, 2.0)
        self.assertFalse(at_cap)

    def test_bool_cost_ignored(self):
        # bool is an int subclass — defensive guard.
        self._write_costs(
            {'task_id': 't-x', 'cost_usd': True},  # treated as 1 by Python
            {'task_id': 't-x', 'cost_usd': 1.0},
        )
        at_cap, current, _ = on._check_cost_budget('t-x', cap_usd=5.0)
        self.assertEqual(current, 1.0)  # bool skipped, only the 1.0 counts

    def test_enforce_allows_below_cap(self):
        self._write_costs({'task_id': 't-x', 'cost_usd': 1.0})
        data = {'reply_chat_id': 12345}
        ok = on._enforce_cost_budget('t-x', 'build-phase', data)
        self.assertTrue(ok)

    def test_enforce_refuses_at_cap_and_dms_larry(self):
        self._write_costs({'task_id': 't-x', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        ok = on._enforce_cost_budget('t-x', 'build-phase', data)
        self.assertFalse(ok)
        # DM queued
        import larry_alerts as la
        records = [json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines() if line.strip()]
        cost_dms = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms), 1)
        self.assertEqual(cost_dms[0]['chat_id'], 12345)
        self.assertIn('build-phase', cost_dms[0]['message'])
        self.assertIn('cap', cost_dms[0]['message'])

    def test_enforce_no_chat_id_still_refuses(self):
        """Refusal happens regardless of reply_chat_id; DM is best-effort."""
        self._write_costs({'task_id': 't-x', 'cost_usd': 20.0})
        ok = on._enforce_cost_budget('t-x', 'build-phase', {})
        self.assertFalse(ok)

    def test_enforce_dedups_same_task_within_daemon_lifetime(self):
        """First cap-fire DMs Larry; subsequent fires for the same task
        within the same daemon-instance suppress the DM (code-review #4).
        """
        import larry_alerts as la
        self._write_costs({'task_id': 't-x', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        # First refusal — DM queued.
        on._enforce_cost_budget('t-x', 'build-phase', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms_first = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms_first), 1)
        # Second refusal on the SAME task — suppressed.
        on._enforce_cost_budget('t-x', 'mirror-review', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms_second = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms_second), 1, 'second fire should not queue a duplicate DM')
        # Third refusal on the SAME task — still suppressed.
        on._enforce_cost_budget('t-x', 'revision-to-forge', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms_third = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms_third), 1)

    def test_enforce_dedups_per_task_not_globally(self):
        """Cap-fire on task A does NOT suppress DM for task B (different
        task — different dedup bucket)."""
        import larry_alerts as la
        self._write_costs({'task_id': 't-A', 'cost_usd': 20.0})
        self._write_costs({'task_id': 't-B', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        on._enforce_cost_budget('t-A', 'build-phase', data)
        on._enforce_cost_budget('t-B', 'build-phase', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms), 2)
        task_ids = {r.get('task_id') for r in cost_dms}
        self.assertEqual(task_ids, {'t-A', 't-B'})

    def test_enforce_dedups_resets_after_daemon_restart(self):
        """Simulating a daemon restart (test helper clears the set) —
        the same task can DM again. Models the real production behavior
        where the daemon-lifetime set is cleared on every restart, which
        is intentional: Larry may have raised the cap and a restart
        means the issue might be resolved."""
        import larry_alerts as la
        self._write_costs({'task_id': 't-x', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        on._enforce_cost_budget('t-x', 'build-phase', data)
        on._reset_cost_budget_dmed_tasks()  # simulate daemon restart
        on._enforce_cost_budget('t-x', 'mirror-review', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms), 2)

    def test_load_cap_from_config_default(self):
        """Missing/malformed config → default 5.0."""
        on._invalidate_loop_bounds_cache()
        # Point the config path at a non-existent file.
        original = on._MODELS_CONFIG_PATH
        on._MODELS_CONFIG_PATH = self._root / 'nope.json'
        try:
            on._invalidate_loop_bounds_cache()
            cap = on._load_cost_per_task_cap_usd_from_config()
            self.assertEqual(cap, on.DEFAULT_COST_PER_TASK_USD_CAP)
        finally:
            on._MODELS_CONFIG_PATH = original
            on._invalidate_loop_bounds_cache()

    def _with_loop_bounds(self, loop_bounds_dict):
        """Helper: write a config with the given loop_bounds; yield."""
        cfg_path = self._root / 'agent-models.json'
        cfg_path.write_text(json.dumps({'loop_bounds': loop_bounds_dict}))
        original = on._MODELS_CONFIG_PATH
        on._MODELS_CONFIG_PATH = cfg_path
        on._invalidate_loop_bounds_cache()
        self.addCleanup(setattr, on, '_MODELS_CONFIG_PATH', original)
        self.addCleanup(on._invalidate_loop_bounds_cache)

    def test_load_cap_from_valid_config(self):
        """Config with valid cost_per_task_usd → returns that value."""
        self._with_loop_bounds({'cost_per_task_usd': 7.5})
        self.assertEqual(on._load_cost_per_task_cap_usd_from_config(), 7.5)

    def test_load_cap_with_bool_falls_back(self):
        """`true` for cost_per_task_usd → fall back to default (bool is
        an int subclass; defensive guard prevents bool from coercing to 1.0).
        """
        self._with_loop_bounds({'cost_per_task_usd': True})
        self.assertEqual(
            on._load_cost_per_task_cap_usd_from_config(),
            on.DEFAULT_COST_PER_TASK_USD_CAP,
        )

    def test_load_cap_with_negative_falls_back(self):
        """Negative cap is meaningless → default."""
        self._with_loop_bounds({'cost_per_task_usd': -1.0})
        self.assertEqual(
            on._load_cost_per_task_cap_usd_from_config(),
            on.DEFAULT_COST_PER_TASK_USD_CAP,
        )

    def test_load_cap_with_string_falls_back(self):
        """Non-numeric cap → default."""
        self._with_loop_bounds({'cost_per_task_usd': '5.00'})
        self.assertEqual(
            on._load_cost_per_task_cap_usd_from_config(),
            on.DEFAULT_COST_PER_TASK_USD_CAP,
        )


class MirrorMarkerRoutingAutoMergeTest(unittest.TestCase):
    """D3.5 5d — process_outbox marker-routing extensions: auto-merge
    fires on review_pass + result attaches to decision + DM reflects outcome."""

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
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
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
        self._auto_merge_calls: list[tuple[str, str]] = []
        self._merge_outcome_override = {
            'merge_outcome': 'merged',
            'merge_reason': 'squash-merged + branch deleted',
            'pr_number': 42,
            'repo_coords': 'test-owner/test-repo',
        }
        def _override(pr_url, task_id):
            self._auto_merge_calls.append((pr_url, task_id))
            return dict(self._merge_outcome_override)
        self._orig_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = _override
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        on._AUTO_MERGE_FN_OVERRIDE = self._orig_override
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_mirror_outbox(self, name, body):
        outbox_dir = on.OUTBOXES_ROOT / 'mirror'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _body_with_chat(self, marker, chat_id=98765):
        body = _mirror_outbox_body(marker)
        body['reply_chat_id'] = chat_id
        return body

    def _read_notifications(self):
        import larry_alerts as la
        if not la.ALERTS_FILE.exists():
            return []
        return [json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines() if line.strip()]

    def test_auto_merge_fires_on_review_pass(self):
        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('t-pass.json', body)
        on.process_outbox(f)
        self.assertEqual(len(self._auto_merge_calls), 1)
        pr_url, task_id = self._auto_merge_calls[0]
        self.assertEqual(pr_url, PR_URL_FIXTURE)
        self.assertEqual(task_id, 't-rev')  # the marker fixture's task_id

    def test_auto_merge_skipped_on_review_revision(self):
        body = self._body_with_chat(_mirror_revision_marker(confidence='high'))
        f = self._write_mirror_outbox('t-rev.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])

    def test_auto_merge_skipped_on_review_escalate(self):
        body = self._body_with_chat(_mirror_escalate_marker())
        f = self._write_mirror_outbox('t-esc.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])

    def test_auto_merge_skipped_on_emergency_halt(self):
        body = self._body_with_chat(_mirror_emergency_marker())
        f = self._write_mirror_outbox('t-halt.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])

    def test_dm_body_reflects_merged_outcome(self):
        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('t-pass.json', body)
        on.process_outbox(f)
        notifications = [
            r for r in self._read_notifications()
            if r.get('kind') == 'notification'
            and r.get('intent') == 'review-pass'
        ]
        self.assertEqual(len(notifications), 1)
        self.assertIn('Auto-merged + branch deleted', notifications[0]['message'])

    def test_dm_body_reflects_failed_outcome(self):
        self._merge_outcome_override = {
            'merge_outcome': 'failed',
            'merge_reason': 'conflict on the merge commit',
            'pr_number': 42,
            'repo_coords': 'test-owner/test-repo',
        }
        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('t-pass.json', body)
        on.process_outbox(f)
        notifications = [
            r for r in self._read_notifications()
            if r.get('kind') == 'notification'
            and r.get('intent') == 'review-pass'
        ]
        self.assertEqual(len(notifications), 1)
        self.assertIn('Auto-merge FAILED', notifications[0]['message'])
        self.assertIn('conflict on the merge commit', notifications[0]['message'])
        # The manual-fallback command must render the actual PR number +
        # repo coords so Larry can copy-paste from his phone (code review
        # finding #12).
        self.assertIn(
            'gh pr merge 42 --repo test-owner/test-repo --squash --delete-branch',
            notifications[0]['message'],
        )

    def test_resume_after_crash_renders_success_body(self):
        """End-to-end resume path: outbox processed once (override returns
        merged), restored from .archive/, processed again (override returns
        already_merged) — second DM body is the success shape, not failure.

        Simulates: daemon crashes between auto-merge and archive; on
        restart the same outbox is re-processed; `_auto_merge_pr`'s
        `gh pr view` recheck disambiguates already-merged from failure.
        Per Larry's sign-off: already-merged on resume reads as success.
        """
        body = self._body_with_chat(_mirror_pass_marker(), chat_id=98765)
        f = self._write_mirror_outbox('t-resume.json', body)
        # First pass — normal `merged` outcome.
        on.process_outbox(f)
        # Restore the outbox from archive (simulate crash before archive).
        archived = on.OUTBOXES_ROOT / 'mirror' / '.archive' / 't-resume.json'
        self.assertTrue(archived.exists())
        archived.rename(f)
        # Flip the override to the resume-after-crash outcome.
        self._merge_outcome_override = {
            'merge_outcome': 'already_merged',
            'merge_reason': 'PR was already merged (resume from prior dispatch)',
            'pr_number': 42,
            'repo_coords': 'test-owner/test-repo',
        }
        # Second pass — already_merged outcome.
        on.process_outbox(f)
        notifications = [
            r for r in self._read_notifications()
            if r.get('kind') == 'notification'
            and r.get('intent') == 'review-pass'
        ]
        # Two notifications now (one per pass). Both render the success
        # body — already_merged uses the same template as merged because
        # the user-visible state is identical (PR is merged either way).
        self.assertEqual(len(notifications), 2)
        for n in notifications:
            self.assertIn('Auto-merged + branch deleted', n['message'])
            self.assertNotIn('FAILED', n['message'])

    def test_dm_body_reflects_already_merged_outcome(self):
        self._merge_outcome_override = {
            'merge_outcome': 'already_merged',
            'merge_reason': 'PR was already merged (resume from prior dispatch)',
            'pr_number': 42,
            'repo_coords': 'test-owner/test-repo',
        }
        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('t-pass.json', body)
        on.process_outbox(f)
        notifications = [
            r for r in self._read_notifications()
            if r.get('kind') == 'notification'
            and r.get('intent') == 'review-pass'
        ]
        self.assertEqual(len(notifications), 1)
        # User-visible message is identical to `merged` — see _REVIEW_PASS_DM_VARIANTS.
        self.assertIn('Auto-merged + branch deleted', notifications[0]['message'])
        self.assertNotIn('FAILED', notifications[0]['message'])

    def test_override_exception_renders_failed_outcome(self):
        """A buggy override that raises should produce a failed-outcome DM,
        not wedge the daemon. Daemon-never-wedge invariant."""
        def _broken(pr_url, task_id):
            raise RuntimeError('test override deliberately broken')
        on._AUTO_MERGE_FN_OVERRIDE = _broken
        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('t-pass.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        notifications = [
            r for r in self._read_notifications()
            if r.get('kind') == 'notification'
            and r.get('intent') == 'review-pass'
        ]
        self.assertEqual(len(notifications), 1)
        self.assertIn('Auto-merge FAILED', notifications[0]['message'])

    def test_marker_error_no_auto_merge(self):
        """Malformed PASS marker → marker-error cascade → NO auto-merge."""
        bad_payload = json.dumps({'task_id': 't-rev', 'pr_url': PR_URL_FIXTURE})
        marker = (
            f'=== REVIEW_PASS ===\n{bad_payload}\n=== END_REVIEW_PASS ==='
        )
        body = self._body_with_chat(marker)
        f = self._write_mirror_outbox('t-rev.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])


class CostBudgetGateAtDispatchSitesTest(unittest.TestCase):
    """D3.5 5d — verify the cost gate refuses dispatch at each of the
    four dispatch sites when the per-task spend is at-cap."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT',
            'BLACKBOARD', 'LOG_FILE', 'COSTS_FILE',
            'EMERGENCY_HALT_FLAG',
        ]:
            self._originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.COSTS_FILE = on.BLACKBOARD / 'costs.jsonl'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
            'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'
        self._rv_root = rv.REPO_ROOT
        self._rv_models_path = rv.MODELS_CONFIG_PATH
        rv.REPO_ROOT = self._root / 'repo'
        rv.MODELS_CONFIG_PATH = rv.REPO_ROOT / 'config' / 'agent-models.json'
        # Seed agent-models.json so the routing validator's check_target_repo
        # gate accepts `ourliberty-agent-core` for forge + mirror (matches
        # production allowlist shape; same pattern as BuildPhaseDispatchTest).
        rv.MODELS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rv.MODELS_CONFIG_PATH.write_text(json.dumps({
            'agents': {
                'forge': {
                    'worktree_enabled': True,
                    'allowed_repos': ['ourliberty-agent-core'],
                },
                'mirror': {
                    'worktree_enabled': True,
                    'allowed_repos': ['ourliberty-agent-core'],
                },
            },
        }))
        rv.invalidate_cache()
        rv.invalidate_models_cache()
        on.ensure_dirs()
        # Per-task DM dedup is daemon-lifetime state; reset between test
        # cases so dispatch-site coverage isn't tainted by the prior case.
        on._reset_cost_budget_dmed_tasks()
        self.addCleanup(on._reset_cost_budget_dmed_tasks)
        # Pre-load $99 onto the test task to put it past the $5 cap.
        on.BLACKBOARD.mkdir(parents=True, exist_ok=True)
        with open(on.COSTS_FILE, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'task_id': 't-over', 'cost_usd': 99.0}) + '\n')

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.MODELS_CONFIG_PATH = self._rv_models_path
        rv.invalidate_cache()
        rv.invalidate_models_cache()
        self._tmp.cleanup()

    def _forge_inbox_files(self):
        return list((on.INBOXES_ROOT / 'forge').glob('*.json'))

    def _mirror_inbox_files(self):
        return list((on.INBOXES_ROOT / 'mirror').glob('*.json'))

    def test_build_phase_refused_at_cap(self):
        data = {
            'task_id': 't-over',
            'claude_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'feature/x',
            'reply_chat_id': 555,
        }
        on._dispatch_build_phase(data)
        self.assertEqual(self._forge_inbox_files(), [])

    def test_mirror_review_refused_at_cap(self):
        data = {
            'task_id': 't-over',
            'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 555,
        }
        on._dispatch_mirror_review(
            data,
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1',
        )
        self.assertEqual(self._mirror_inbox_files(), [])

    def test_revision_dispatch_refused_at_cap(self):
        data = {
            'task_id': 't-over',
            'forge_build_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 555,
        }
        decision = {'payload': {'findings': []}}
        on._dispatch_revision_to_forge(data, decision)
        self.assertEqual(self._forge_inbox_files(), [])

    def test_review_rerun_refused_at_cap(self):
        data = {
            'task_id': 't-over',
            'target_repo': 'ourliberty-agent-core',
            'pr_url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1',
            'reply_chat_id': 555,
        }
        on._dispatch_mirror_review_rerun(data, round_num=1, summary='fix x')
        self.assertEqual(self._mirror_inbox_files(), [])

    def test_cost_budget_dm_queued_on_refusal(self):
        import larry_alerts as la
        data = {
            'task_id': 't-over',
            'claude_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 555,
        }
        on._dispatch_build_phase(data)
        records = [json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines() if line.strip()]
        cost_dms = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms), 1)
        self.assertEqual(cost_dms[0]['chat_id'], 555)

    def test_under_cap_allows_dispatch(self):
        """Sanity: clean task_id below cap → dispatch proceeds normally."""
        # Use a different task_id with no cost records.
        data = {
            'task_id': 't-clean',
            'claude_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'feature/x',
            'reply_chat_id': 555,
        }
        on._dispatch_build_phase(data)
        self.assertEqual(len(self._forge_inbox_files()), 1)

    def test_idempotent_skip_does_not_fire_cost_dm(self):
        """Second-pass review fix 2-#1: cost-budget gate is positioned
        AFTER the idempotency check, so re-processing an outbox whose
        dispatch already landed (crash-recovery scenario) does NOT fire
        a false-alarm cost-DM to Larry. The idempotency check returns
        early; the gate isn't reached."""
        import larry_alerts as la
        # Pre-load $99 onto t-already so cost would fire if reached.
        with open(on.COSTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'task_id': 't-already', 'cost_usd': 99.0}) + '\n')
        # Pre-stage the build dispatch as already-archived (simulates
        # the daemon having processed this outbox + dispatched + archived
        # in a prior run; now re-processing on resume).
        (on.INBOXES_ROOT / 'forge' / '.archive').mkdir(parents=True, exist_ok=True)
        (on.INBOXES_ROOT / 'forge' / '.archive' / 'build-t-already.json').write_text(
            json.dumps({'task_id': 't-already', 'prompt': 'noop'}),
        )
        data = {
            'task_id': 't-already',
            'claude_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 555,
        }
        on._dispatch_build_phase(data)
        # Idempotency check fires; cost gate is never reached.
        # No new inbox file (the existing .archive entry blocks).
        new_files = [
            p for p in (on.INBOXES_ROOT / 'forge').glob('*.json')
            if p.name != 'build-t-already.json'
        ]
        self.assertEqual(new_files, [])
        # Critical: no cost-budget DM queued.
        if la.ALERTS_FILE.exists():
            records = [
                json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
                if line.strip()
            ]
            cost_dms = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
            self.assertEqual(
                cost_dms, [],
                'crash-recovery should not fire false-alarm cost-DM',
            )


class CostDmTemplateTest(unittest.TestCase):
    """D3.5 5d — cost-budget-exhausted DM template renders correctly."""

    def test_renders_with_full_fields(self):
        decision = {
            'intent': 'cost-budget-exhausted',
            'payload': {'task_id': 't-x'},
            'intent_kwargs': {
                'task_id': 't-x',
                'current_usd': '6.50',
                'cap_usd': '5.00',
                'dispatch_label': 'mirror-review-rerun',
            },
        }
        body = on._render_dm_message('cost-budget-exhausted', decision)
        self.assertIsNotNone(body)
        self.assertIn('t-x', body)
        self.assertIn('$6.50', body)
        self.assertIn('$5.00', body)
        self.assertIn('mirror-review-rerun', body)
        self.assertIn('cost_per_task_usd', body)

    def test_terminal_intent(self):
        self.assertIn('cost-budget-exhausted', on.TERMINAL_DM_INTENTS)

    def test_missing_kwargs_renders_with_placeholders(self):
        """Missing intent_kwargs → renders with '?' placeholders, never crashes."""
        decision = {
            'intent': 'cost-budget-exhausted',
            'payload': {'task_id': 't-x'},
            'intent_kwargs': {},
        }
        body = on._render_dm_message('cost-budget-exhausted', decision)
        self.assertIsNotNone(body)
        # Should contain '?' placeholders where data is missing.
        self.assertIn('?', body)


class LarryDirectDispatchTest(unittest.TestCase):
    """E1.5.2 source-routing fix: when Larry dispatches Mirror directly
    (source='larry') and propagates reply_chat_id, the notifier should
    (a) skip the inter-agent notify (no agent to route to), (b) fire
    auto-merge if the marker is REVIEW_PASS, and (c) DM Larry the result
    via the existing reply_chat_id chain.

    The bug this fixes: PR #45 dispatch on 2026-05-19 — Larry sent the
    design PR straight to Mirror; Mirror's REVIEW_PASS was archived with
    'no routable target' WARN because _primary_agent_id('larry') is None.
    Auto-merge had to fall through to heal-pr-auto-merge (E1.3) instead.
    """

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
        self._auto_merge_calls: list[tuple[str, str]] = []

        def _default_auto_merge(pr_url, task_id):
            self._auto_merge_calls.append((pr_url, task_id))
            return {
                'merge_outcome': 'merged',
                'merge_reason': 'squash-merged (test override)',
                'pr_number': 45,
                'repo_coords': 'test-owner/test-repo',
            }

        self._original_auto_merge_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = _default_auto_merge
        import larry_alerts as la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
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
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        on._AUTO_MERGE_FN_OVERRIDE = self._original_auto_merge_override
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_mirror_outbox(self, name, body):
        outbox_dir = on.OUTBOXES_ROOT / 'mirror'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _read_alerts(self):
        path = self._root / 'blackboard' / 'larry-alerts.jsonl'
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def test_review_pass_with_larry_source_and_chat_id_auto_merges(self):
        # The bug fix: source='larry' + reply_chat_id set must trigger
        # the auto-merge path (and not the 'no routable target' archive).
        body = _mirror_outbox_body(
            _mirror_pass_marker(),
            source='larry',
            reply_chat_id=12345,
        )
        f = self._write_mirror_outbox('t-direct.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')
        # Auto-merge fired.
        self.assertEqual(len(self._auto_merge_calls), 1)
        self.assertEqual(self._auto_merge_calls[0][0], PR_URL_FIXTURE)
        # No beacon notify (no agent target — that's the whole point).
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(notifies, [])
        # Larry got a DM (review-pass with merged outcome).
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['chat_id'], 12345)
        self.assertEqual(alerts[0]['intent'], 'review-pass')

    def test_review_pass_with_larry_source_no_chat_id_archives(self):
        # Regression: without reply_chat_id, there's no Larry-direct target
        # either — preserve the existing 'no routable target' archive path.
        # Auto-merge MUST NOT fire in this case (no closing DM = silent
        # action).
        body = _mirror_outbox_body(
            _mirror_pass_marker(),
            source='larry',
            reply_chat_id=None,
        )
        f = self._write_mirror_outbox('t-no-chat.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'archived-no-notify')
        self.assertEqual(self._auto_merge_calls, [])

    def test_review_revision_with_larry_source_dispatches_revision_not_dm(self):
        # task-19 (2026-05-19): clean REVIEW_REVISION on a Larry-direct
        # Mirror review now dispatches the revision to Forge — same as the
        # source='beacon' flow — and skips the synth DM, because Forge
        # picking up the revision IS the chain-advance signal. PR #46's
        # original over-broad larry-direct branch hijacked this path by
        # firing the synth DM AND skipping `_dispatch_revision_to_forge`,
        # which is the regression the narrowing fix structurally prevents.
        # Verified in depth by LarryDirectDispatchNarrowingTest below.
        body = _mirror_outbox_body(
            _mirror_revision_marker(confidence='high'),
            source='larry',
            reply_chat_id=12345,
            # `_dispatch_revision_to_forge` needs a forge build session
            # to --resume against, plus the standard PR envelope fields.
            forge_build_session_id='forge-build-sess',
            target_repo='ourliberty-agent-core',
            branch='forge/t-rev',
            pr_url=PR_URL_FIXTURE,
            revision_count=0,
            max_revisions=3,
        )
        f = self._write_mirror_outbox('t-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')
        self.assertEqual(self._auto_merge_calls, [])
        # Revision dispatched to Forge.
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-t-rev-*.json')
        )
        self.assertEqual(len(revisions), 1)
        # No synth DM — the chain continues via Forge.
        self.assertEqual(self._read_alerts(), [])

    def test_review_escalate_with_larry_source_dms_without_merge(self):
        body = _mirror_outbox_body(
            _mirror_escalate_marker(),
            source='larry',
            reply_chat_id=12345,
        )
        f = self._write_mirror_outbox('t-esc.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')
        self.assertEqual(self._auto_merge_calls, [])
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['intent'], 'review-escalate')

    def test_nominal_source_beacon_flow_unchanged(self):
        # The fix MUST NOT change the existing beacon-dispatched review
        # flow. Mirror sourced from Beacon should still notify Beacon and
        # auto-merge.
        body = _mirror_outbox_body(
            _mirror_pass_marker(),
            source='beacon',
            reply_chat_id=99,
        )
        f = self._write_mirror_outbox('t-beacon.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        # Auto-merge fired.
        self.assertEqual(len(self._auto_merge_calls), 1)
        # Beacon notify written.
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)

    def test_no_marker_with_larry_source_falls_through_to_default(self):
        # Chat-mode response (no marker) with source='larry' must follow
        # the existing default routing — NOT trip the new larry_direct
        # branch, which is marker-specific. Default routing for source
        # 'larry' archives via _should_notify_back returning False.
        body = _mirror_outbox_body(
            result='Reviewed in chat mode, no marker emitted.',
            source='larry',
            reply_chat_id=12345,
        )
        f = self._write_mirror_outbox('t-chat.json', body)
        result = on.process_outbox(f)
        # Default path archives non-agent-source outboxes; no marker = no
        # larry_direct branch.
        self.assertIn(result, ('archived-no-notify', 'notified'))
        self.assertEqual(self._auto_merge_calls, [])


class LarryDirectDispatchNarrowingTest(unittest.TestCase):
    """task-19 (2026-05-19) — PR #46's source-routing fix was too broad.

    PR #46 added a `larry_direct` branch that intercepted ALL markers from
    source='larry' dispatches, including Forge's PROCEED. The result:
    (a) `_dispatch_build_phase` never fired, so Larry-direct preflights
    silently failed to advance to build, and (b) the synth DM template
    was hardcoded to "Mirror requested revision …" so even ack-proceed
    rendered the wrong body. Symptom: 2026-05-19 task #17 dispatch.

    The narrowing fix:
      * Dispatch helpers (`_dispatch_build_phase`,
        `_dispatch_revision_to_forge`) fire regardless of larry_direct.
      * Synth DM fires only for markers with no follow-up handler
        (today: residual Forge CLARIFY_REQUEST with no dispatcher).
      * Synth DM body branches on intent — no more wrong-template
        "Mirror requested revision" leak on Forge PROCEED.
    """

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
        self._auto_merge_calls: list[tuple[str, str]] = []

        def _default_auto_merge(pr_url, task_id):
            self._auto_merge_calls.append((pr_url, task_id))
            return {
                'merge_outcome': 'merged',
                'merge_reason': 'squash-merged (test override)',
                'pr_number': 46,
                'repo_coords': 'test-owner/test-repo',
            }

        self._original_auto_merge_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = _default_auto_merge

        import larry_alerts as la
        self._la = la
        self._la_originals = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'

        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
            'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'

        # Seed a realistic agent-models.json so the build/revision dispatch
        # exercises the production allow-list shape (forge:
        # ourliberty-agent-core) — mirrors BuildPhaseDispatchTest.setUp.
        self._rv_root = rv.REPO_ROOT
        self._rv_models_path = rv.MODELS_CONFIG_PATH
        rv.REPO_ROOT = self._root / 'repo'
        rv.MODELS_CONFIG_PATH = rv.REPO_ROOT / 'config' / 'agent-models.json'
        rv.MODELS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rv.MODELS_CONFIG_PATH.write_text(json.dumps({
            'agents': {
                'forge': {
                    'worktree_enabled': True,
                    'allowed_repos': ['ourliberty-agent-core'],
                },
            },
        }))
        rv.invalidate_cache()
        rv.invalidate_models_cache()
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(self._la, name, value)
        on._AUTO_MERGE_FN_OVERRIDE = self._original_auto_merge_override
        rv.REPO_ROOT = self._rv_root
        rv.MODELS_CONFIG_PATH = self._rv_models_path
        rv.invalidate_cache()
        rv.invalidate_models_cache()
        self._tmp.cleanup()

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _read_alerts(self):
        path = self._root / 'blackboard' / 'larry-alerts.jsonl'
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text().splitlines()
            if line.strip()
        ]

    # ---------------- regression 1: Forge PROCEED defers to build_phase ----------------

    def test_forge_proceed_with_larry_source_dispatches_build_phase(self):
        # The 2026-05-19 task #17 dispatch failure mode, reproduced as a
        # regression test. Forge emitted PROCEED on a source='larry'
        # preflight; PR #46's over-broad larry_direct branch hijacked it,
        # DM'd the wrong template ("Mirror requested revision"), and
        # skipped `_dispatch_build_phase`. After the narrowing fix:
        # build phase MUST auto-dispatch, NO synth DM fires.
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "t-19-proceed", '
            '"preflight_summary": "Edit foo.py."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='larry', task_id='t-19-proceed',
            claude_session_id='sess-larry-direct-proceed',
            target_repo='ourliberty-agent-core',
            reply_chat_id=7998341473,
            result=f'Spec is clear; ready.\n\n{marker}',
        )
        f = self._write_outbox('forge', 't-19-proceed.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')

        # Build phase dispatched — the existing handler fires for
        # source='larry' too. Without the narrowing fix this would be 0.
        forge_builds = list(
            (on.INBOXES_ROOT / 'forge').glob('build-*.json')
        )
        self.assertEqual(
            len(forge_builds), 1,
            'build phase MUST auto-dispatch for source="larry" PROCEED',
        )
        build_data = json.loads(forge_builds[0].read_text())
        self.assertEqual(build_data['phase'], 'build')
        self.assertEqual(build_data['task_id'], 't-19-proceed')
        self.assertEqual(
            build_data['session_id'], 'sess-larry-direct-proceed',
        )

        # No back-leg notify to Beacon (no upstream agent — correct).
        beacon_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob('notify-*.json')
        )
        self.assertEqual(beacon_notifies, [])

        # No synth DM — the build phase will produce the closing PR DM
        # downstream; an intermediate "PROCEED accepted" DM would just be
        # noise (and the old "Mirror requested revision" text was wrong).
        self.assertEqual(self._read_alerts(), [])

    # ---------------- regression 2: Forge CLARIFY defers, no wrong DM ----------------

    def test_forge_clarify_with_larry_source_does_not_dispatch_build_phase(self):
        # Forge CLARIFY_REQUEST has no follow-up dispatcher when
        # source='larry' (no Beacon to answer). The narrowing must NOT
        # trigger `_dispatch_build_phase` (only PROCEED does), and the
        # synth DM that does fire must use the clarify-specific body —
        # NOT the hardcoded "Mirror requested revision" wording PR #46
        # leaked onto every non-terminal intent.
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-19-clarify", '
            '"question": "Which config file should I modify?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = _good_outbox(
            agent='forge', source='larry', task_id='t-19-clarify',
            claude_session_id='sess-larry-clarify',
            clarification_count=0, max_clarifications=3,
            target_repo='ourliberty-agent-core',
            reply_chat_id=7998341473,
            result=f'Need more info.\n\n{marker}',
        )
        f = self._write_outbox('forge', 't-19-clarify.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')

        # CLARIFY must NOT trip build-phase dispatch.
        forge_builds = list(
            (on.INBOXES_ROOT / 'forge').glob('build-*.json')
        )
        self.assertEqual(forge_builds, [])

        # Synth DM fires (CLARIFY has no other closing signal); body must
        # NOT contain the old hardcoded "Mirror requested revision" text.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        body = alerts[0]['message']
        self.assertNotIn('Mirror requested revision', body)
        self.assertIn('clarification', body.lower())
        self.assertIn('Which config file should I modify?', body)

    # ---------------- regression 3: Mirror REVISION defers to revision dispatch ----------------

    def test_mirror_revision_with_larry_source_dispatches_revision(self):
        # Clean REVIEW_REVISION (high confidence, budget remaining)
        # auto-dispatches a revision task to Forge — for source='larry'
        # just as for source='beacon'. PR #46's branch was skipping the
        # dispatch AND DMing Larry the wrong template; with the
        # narrowing fix, dispatch fires and no synth DM goes out.
        body = _mirror_outbox_body(
            _mirror_revision_marker(
                task_id='t-19-revision', confidence='high',
            ),
            task_id='t-19-revision',
            source='larry',
            reply_chat_id=7998341473,
            forge_build_session_id='forge-build-sess-19',
            target_repo='ourliberty-agent-core',
            branch='forge/t-19-revision',
            pr_url=PR_URL_FIXTURE,
            revision_count=0,
            max_revisions=3,
        )
        f = self._write_outbox('mirror', 't-19-revision.json', body)

        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')
        self.assertEqual(self._auto_merge_calls, [])

        # Revision dispatched to Forge — the existing handler fires.
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-t-19-revision-*.json')
        )
        self.assertEqual(
            len(revisions), 1,
            'revision MUST auto-dispatch for source="larry" REVIEW_REVISION',
        )

        # No synth DM — chain continues via Forge.
        self.assertEqual(self._read_alerts(), [])

    # ---------------- regression 4: PR #46's PASS auto-merge preserved ----------------

    def test_mirror_pass_with_larry_source_still_auto_merges_and_dms(self):
        # PR #46's intended behavior MUST be preserved: source='larry' +
        # REVIEW_PASS auto-merges the PR and DMs Larry with the standard
        # review-pass template (NOT the synth DM).
        body = _mirror_outbox_body(
            _mirror_pass_marker(task_id='t-19-pass'),
            task_id='t-19-pass',
            source='larry',
            reply_chat_id=7998341473,
        )
        f = self._write_outbox('mirror', 't-19-pass.json', body)

        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')

        # Auto-merge fired (the PR #46 gap-fill).
        self.assertEqual(len(self._auto_merge_calls), 1)
        self.assertEqual(self._auto_merge_calls[0][0], PR_URL_FIXTURE)

        # Terminal DM via _maybe_dm_larry (NOT the synth path).
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['intent'], 'review-pass')
        self.assertIn('Mirror approved', alerts[0]['message'])
        self.assertIn('Auto-merged', alerts[0]['message'])

    # ---------------- regression 5: PASS without chat_id archives cleanly ----------------

    def test_mirror_pass_with_larry_source_no_chat_id_archives(self):
        # Pre-existing skip preserved: without reply_chat_id there is no
        # Larry-direct target — the marker archives via the
        # 'no routable target' path and auto-merge does NOT fire (silent
        # action would be worse than no action).
        body = _mirror_outbox_body(
            _mirror_pass_marker(task_id='t-19-pass-nochat'),
            task_id='t-19-pass-nochat',
            source='larry',
            reply_chat_id=None,
        )
        f = self._write_outbox('mirror', 't-19-pass-nochat.json', body)

        result = on.process_outbox(f)
        self.assertEqual(result, 'archived-no-notify')
        self.assertEqual(self._auto_merge_calls, [])
        self.assertEqual(self._read_alerts(), [])

    # ---------------- regression 6: synth DM body branches on intent ----------------

    def test_synth_dm_body_branches_on_intent(self):
        # The DM template MUST render an intent-specific body. Before the
        # fix, every non-terminal intent rendered "Mirror requested
        # revision on PR …" — including Forge's ack-proceed. This test
        # locks in distinct bodies per intent so the template can't
        # silently regress to a one-size-fits-all string.
        data = {
            'task_id': 't-template',
            'reply_chat_id': 7998341473,
        }
        bodies = {}
        for intent, marker_type, payload in (
            ('ack-proceed', 'proceed', {'task_id': 't-template'}),
            (
                'review-revision', 'review_revision',
                {
                    'task_id': 't-template', 'pr_url': PR_URL_FIXTURE,
                    'findings': [
                        {'file': 'a.py', 'line_range': 'L1',
                         'severity': 'low', 'description': 'x'},
                    ],
                },
            ),
            (
                'clarify', 'clarify_request',
                {
                    'task_id': 't-template',
                    'question': 'Which file?',
                },
            ),
        ):
            decision = {
                'marker_type': marker_type,
                'intent': intent,
                'payload': payload,
                'intent_kwargs': {},
                'notify_source': 'forge-result',
                'auto_promoted': False,
                'next_clarification_count': None,
            }
            # Clear any prior alerts so we read just this intent's DM.
            alerts_file = self._root / 'blackboard' / 'larry-alerts.jsonl'
            if alerts_file.exists():
                alerts_file.unlink()
            on._maybe_dm_larry_direct_synth(data, decision)
            alerts = self._read_alerts()
            self.assertEqual(
                len(alerts), 1,
                f'synth DM should fire for intent={intent}',
            )
            bodies[intent] = alerts[0]['message']

        # ack-proceed body must mention PROCEED / build phase, NOT
        # "Mirror requested revision".
        self.assertNotIn('Mirror requested revision', bodies['ack-proceed'])
        self.assertIn('PROCEED', bodies['ack-proceed'])
        self.assertIn('build phase', bodies['ack-proceed'].lower())

        # review-revision body keeps the existing wording (back-compat
        # for any caller that legitimately routes here — today none, but
        # the arm is kept defensively).
        self.assertIn('Mirror requested revision', bodies['review-revision'])
        self.assertIn(PR_URL_FIXTURE, bodies['review-revision'])

        # clarify body cites the actual question, NOT a revision template.
        self.assertNotIn('Mirror requested revision', bodies['clarify'])
        self.assertIn('clarification', bodies['clarify'].lower())
        self.assertIn('Which file?', bodies['clarify'])

        # All three bodies must be distinct — locks in branching.
        self.assertNotEqual(bodies['ack-proceed'], bodies['review-revision'])
        self.assertNotEqual(bodies['ack-proceed'], bodies['clarify'])
        self.assertNotEqual(bodies['review-revision'], bodies['clarify'])


class BeaconHeadlessApprovalRequestTest(unittest.TestCase):
    """Task #17 (2026-05-19) — Claude drops a dispatch envelope into
    Beacon's inbox with source='larry'; Beacon's outbox result text
    carries a clean APPROVAL_REQUEST marker; the notifier auto-translates
    that marker into a Forge preflight task without consulting trust_policy
    or DMing Larry (implicit upstream-session approval).

    Mirrors the BeaconReplanLoopTest setUp/tearDown — full tmpdir reroute
    of AGENTS_ROOT + INBOXES_ROOT + OUTBOXES_ROOT + ROUTING_EVENTS_LOG so
    safe_write_inbox lands its synthetic dispatches under the tmpdir.
    """

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

    def _write_outbox(self, agent, name, body):
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _marker(self, **overrides):
        """Build a syntactically clean APPROVAL_REQUEST block. Required
        fields per beacon_approval_handler.REQUIRED_FIELDS: task_id,
        summary, target_agent, prompt."""
        payload = {
            'task_id': 'task-headless-001',
            'summary': 'Add a CLI flag to scripts/foo.py.',
            'target_agent': 'forge',
            'prompt': 'x' * 200,
            'target_repo': 'ourliberty-agent-core',
            'task_type': 'feature-development',
            'pr_title': 'feat(foo): add --bar flag',
        }
        payload.update(overrides)
        return (
            f'=== APPROVAL_REQUEST ===\n{json.dumps(payload)}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )

    def _headless_outbox(
        self,
        marker_text=None,
        narrative_prefix=(
            "I reviewed the Larry-session spec and have a plan to propose."
        ),
        envelope_task_id='dispatch-larry-001',
        reply_chat_id=7998341473,
        **overrides,
    ):
        if marker_text is None:
            marker_text = self._marker()
        result = (
            f'{narrative_prefix}\n\n{marker_text}'
            if marker_text else narrative_prefix
        )
        body = _good_outbox(
            agent='beacon',
            source='larry',
            task_id=envelope_task_id,
            result=result,
        )
        if reply_chat_id is not None:
            body['reply_chat_id'] = reply_chat_id
        body.update(overrides)
        return body

    # ---------------- happy path ----------------

    def test_happy_path_writes_forge_preflight_envelope(self):
        body = self._headless_outbox()
        f = self._write_outbox('beacon', 'larry-001.json', body)
        status = on.process_outbox(f)
        self.assertEqual(status, 'headless-approval-dispatched')
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('*.json'))
        self.assertEqual(len(forge_tasks), 1)
        written = json.loads(forge_tasks[0].read_text())
        self.assertEqual(written['task_id'], 'task-headless-001')
        # Source on the Forge envelope is 'beacon' — anchors back-leg
        # routing so subsequent markers from Forge notify Beacon (not Larry
        # directly) per the standard dispatcher relationship.
        self.assertEqual(written['source'], 'beacon')
        self.assertEqual(written['phase'], 'preflight')
        self.assertEqual(written['target_agent'], 'forge')
        self.assertEqual(written['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(written['pr_title'], 'feat(foo): add --bar flag')
        self.assertEqual(written['reply_chat_id'], 7998341473)
        self.assertEqual(written['dispatched_by'], 'outbox-notifier')
        self.assertEqual(written['task_type'], 'feature-development')

    # ---------------- source/agent gates ----------------

    def test_source_beacon_chatmode_artifact_does_not_fire(self):
        # Chat-mode dispatches arrive with source != 'larry' (the bot
        # writes them with whatever the chat-mode source is). The
        # headless handler must defer to the chat-mode path entirely.
        body = self._headless_outbox()
        body['source'] = 'beacon'   # not 'larry' → handler skips
        # Direct call to verify the gate, since process_outbox would
        # short-circuit on its own gate for this source.
        result = on._handle_beacon_headless_approval_request(
            body, body['result'],
        )
        self.assertIsNone(result)
        # No Forge inbox writes happened.
        forge_dir = on.INBOXES_ROOT / 'forge'
        self.assertFalse(any(forge_dir.glob('*.json')))

    def test_agent_not_beacon_does_not_fire(self):
        body = self._headless_outbox()
        body['agent'] = 'forge'
        result = on._handle_beacon_headless_approval_request(
            body, body['result'],
        )
        self.assertIsNone(result)

    def test_no_marker_in_result_falls_through_to_default_routing(self):
        # source='larry' + no APPROVAL_REQUEST marker in Beacon's response
        # → handler declines and default routing takes over. The Beacon
        # outbox archives via _should_notify_back returning False for a
        # 'larry' source (system source).
        body = self._headless_outbox(
            marker_text='',
            narrative_prefix=(
                'Read the spec; nothing to approve yet — need a clarification.'
            ),
        )
        f = self._write_outbox('beacon', 'larry-noplan.json', body)
        status = on.process_outbox(f)
        # 'larry' is a system source; default routing archives it.
        self.assertEqual(status, 'archived-no-notify')
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('*.json'))
        self.assertEqual(forge_tasks, [])

    # ---------------- malformed marker ----------------

    def test_malformed_marker_missing_required_fields_does_not_crash(self):
        # APPROVAL_REQUEST block present but JSON is missing required
        # fields (e.g. no `prompt`). The MalformedApprovalMarker exception
        # is caught, the function logs WARN and returns None, default
        # routing takes over. No Forge envelope is written.
        bad_payload = json.dumps({
            'task_id': 'task-missing-fields',
            'summary': 'incomplete',
            'target_agent': 'forge',
            # `prompt` deliberately omitted
        })
        bad_marker = (
            f'=== APPROVAL_REQUEST ===\n{bad_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = self._headless_outbox(marker_text=bad_marker)
        f = self._write_outbox('beacon', 'larry-bad.json', body)
        status = on.process_outbox(f)
        # Did not raise; did not dispatch; fell through.
        self.assertNotEqual(status, 'headless-approval-dispatched')
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('*.json'))
        self.assertEqual(forge_tasks, [])

    # ---------------- idempotency ----------------

    def test_idempotent_duplicate_outbox_does_not_double_dispatch(self):
        # Re-processing the same outbox after a crash/replay must not
        # write a second Forge preflight envelope.
        body = self._headless_outbox()
        f = self._write_outbox('beacon', 'larry-idem.json', body)
        on.process_outbox(f)
        # Re-write the same outbox + reprocess.
        f2 = self._write_outbox('beacon', 'larry-idem.json', body)
        status2 = on.process_outbox(f2)
        # Idempotency-skip still archives — the return signals the marker
        # was handled (existing file exists), just not re-written.
        self.assertEqual(status2, 'headless-approval-dispatched')
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('*.json'))
        self.assertEqual(len(forge_tasks), 1)

    # ---------------- marker task_id authoritative ----------------

    def test_marker_task_id_wins_over_envelope_task_id(self):
        # Envelope task_id ('dispatch-larry-001') is the upstream dispatch
        # ticket; Beacon's marker carries the downstream Forge work id
        # ('task-headless-001'). Forge's envelope must be keyed by the
        # marker's task_id — that's what the chat-mode flow does (via
        # dispatch_approved's `f'{payload["task_id"]}.json'`).
        body = self._headless_outbox(
            envelope_task_id='dispatch-larry-001',
            marker_text=self._marker(task_id='real-forge-work-001'),
        )
        f = self._write_outbox('beacon', 'dispatch-larry-001.json', body)
        on.process_outbox(f)
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('*.json'))
        self.assertEqual(len(forge_tasks), 1)
        self.assertEqual(forge_tasks[0].name, 'real-forge-work-001.json')
        written = json.loads(forge_tasks[0].read_text())
        self.assertEqual(written['task_id'], 'real-forge-work-001')

    # ---------------- regression: chat-mode (source!='larry') untouched ----------------

    def test_chat_mode_envelope_untouched_by_headless_handler(self):
        # A Beacon outbox produced by the chat-mode path (source !=
        # 'larry') falls through the headless gate and hits default
        # routing. Confirms we did not break the chat-mode flow.
        body = self._headless_outbox()
        # Chat-mode source — the bot wrote Beacon's inbox task with this.
        body['source'] = 'telegram-webhook'
        f = self._write_outbox('beacon', 'chat-001.json', body)
        status = on.process_outbox(f)
        # 'telegram-webhook' is a system source; default routing archives.
        self.assertNotEqual(status, 'headless-approval-dispatched')
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('*.json'))
        self.assertEqual(forge_tasks, [])


if __name__ == '__main__':
    unittest.main()
