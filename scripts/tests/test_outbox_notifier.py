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


if __name__ == '__main__':
    unittest.main()
