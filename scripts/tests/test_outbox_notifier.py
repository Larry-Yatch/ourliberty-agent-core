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
        # 5a does NOT trip the halt-file yet — that's 5d.
        self.assertFalse(on.EMERGENCY_HALT_FLAG.exists())

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
    """m-2 fix: _PR_URL_RE is anchored to start-of-string so a stale PR URL
    discussed in narrative doesn't false-match before the real one."""

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
        # Forge wrote a narrative that DISCUSSES a stale URL but didn't
        # actually open a PR. Anchored regex must NOT match this.
        result = (
            'I considered re-using last week\'s branch where '
            'PR opened: https://github.com/x/y/pull/99 — but instead, '
            'I built fresh and ran into a compile error. No PR yet.'
        )
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

    def test_stale_url_before_real_url_first_one_wins_only_at_start(self):
        # Worst-case: Forge starts with narrative discussing a stale URL,
        # then writes the real "PR opened:" later. Anchored regex returns
        # None — the build response did NOT follow the contract (URL must
        # be at start). Default routing fallback handles it; Beacon sees
        # the result and decides.
        result = (
            'Briefly considered https://github.com/x/y/pull/99 but rejected.\n'
            'PR opened: https://github.com/Larry-Yatch/ourliberty-agent-core/'
            'pull/77'
        )
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

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

    def test_review_emergency_renders_reason_and_evidence(self):
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
        notifs = self._read_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertIn('Plaintext credentials', notifs[0]['message'])
        self.assertIn('AKIA', notifs[0]['message'])

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


if __name__ == '__main__':
    unittest.main()
