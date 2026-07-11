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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import forge_preflight_handler as fph  # noqa: E402
import outbox_notifier as on        # noqa: E402
import routing_validator as rv      # noqa: E402
import safe_write_inbox as swi      # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


# Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir for the whole module so
# tests running inside a Forge worktree don't pollute prod /home/larry/agents/
# logs/state. Mirrors PR #53 pattern (heal_pr_auto_merge tests). Per-test
# monkeypatches of on.AGENTS_ROOT / on.LOG_FILE / ... still override on top.
_AGENTS_ROOT_BACKUP = None
_AGENTS_ROOT_TMPDIR = None
_LIVE_EMIT_BACKUP = None

# build-mirror-review-status — shared ordered call log. The inert status
# override (installed in setUpModule) and per-class merge overrides both
# append here so a test can assert the `mirror-review` status POST fires
# before auto-merge without shelling out to real `gh`.
_MIRROR_STATUS_CALL_LOG: list[tuple] = []


def _inert_mirror_review_status(data, marker_decision):
    """Stand-in for `_post_mirror_review_commit_status` during process_outbox
    integration tests — records the verdict + mapped state, no `gh` shell-out."""
    mtype = marker_decision.get('marker_type')
    state = 'success' if mtype == 'review_pass' else 'failure'
    payload = marker_decision.get('payload') or {}
    _MIRROR_STATUS_CALL_LOG.append(
        ('status', mtype, state, payload.get('pr_url')),
    )
    return state


def _inert_mirror_findings_comment(data, marker_decision):
    """Stand-in for `_post_mirror_findings_comment` during process_outbox
    integration tests — records the verdict, no `gh` shell-out. Mirrors the
    inert status override so non-PASS verdicts routed through process_outbox
    never try to post a real PR comment."""
    mtype = marker_decision.get('marker_type')
    if mtype not in ('review_revision', 'review_escalate'):
        return None
    payload = marker_decision.get('payload') or {}
    _MIRROR_STATUS_CALL_LOG.append(
        ('findings', mtype, payload.get('pr_url')),
    )
    return 'created'


def setUpModule():  # noqa: N802 — unittest hook name
    global _AGENTS_ROOT_BACKUP, _AGENTS_ROOT_TMPDIR, _LIVE_EMIT_BACKUP
    global _CHOKEPOINT_SAVED_SENTINEL
    # This module drives gh-write / larry-alerts / inbox-write chokepoints
    # against the rerouted tmpdir tree below; opt out of the Layer B guards so
    # they pass through to the test's mocks (the #428 real-tree scan still runs).
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()
    _AGENTS_ROOT_BACKUP = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    _AGENTS_ROOT_TMPDIR = tempfile.mkdtemp(prefix='outbox-notifier-test-')
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_TMPDIR
    # forge-queue-in-review-lane: review dispatches now push-emit a chain
    # event via chain_event_emit. Under `unittest discover` (which never
    # runs tests/__init__.py — the verified #428 gap) nothing else sets the
    # guard, and a shell with SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY sourced
    # (any droplet session) would build a LIVE client and upsert fixture
    # rows into the real chain_events table. Force the kill-switch for the
    # whole module; tests that exercise emit behavior mock emit_event.
    _LIVE_EMIT_BACKUP = os.environ.get('OURLIBERTY_DISABLE_LIVE_EMIT')
    os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'
    for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
        Path(_AGENTS_ROOT_TMPDIR, sub).mkdir(exist_ok=True)
    importlib.reload(swi)  # swi follows OURLIBERTY_AGENTS_ROOT too (prod-write isolation)
    importlib.reload(on)
    # Install the inert status override so any class routing a Mirror verdict
    # through process_outbox never shells out to real `gh`. Tests that exercise
    # the real helper restore `on._POST_STATUS_FN_OVERRIDE = None` themselves.
    on._POST_STATUS_FN_OVERRIDE = _inert_mirror_review_status
    # Same posture for the Contract A findings-comment upsert (§ 4): inert
    # during process_outbox integration so non-PASS verdicts never shell out
    # to `gh` for a PR comment. MirrorFindingsCommentTest restores it to None.
    on._POST_FINDINGS_FN_OVERRIDE = _inert_mirror_findings_comment
    # merged-PR REVIEW_REVISION guard (the #764 race): every review_revision
    # routed through process_outbox now calls `_gh_pr_is_open`. Default it to
    # None (state-unknown → fail-open → the pre-guard "proceed" behavior) so
    # routing tests stay deterministic and never shell out to real `gh`. This is
    # behavior-identical to the status quo — real `_gh_pr_is_open('x/y', N)`
    # already returns None in the sandbox (the fixture repo doesn't exist) — it
    # just removes the nondeterministic subprocess. Classes asserting a specific
    # state override it per-test (MergedPrReviewRevisionGuardTest sets MERGED;
    # MirrorReviewDispatchTest sets OPEN).
    on._gh_pr_is_open = lambda *a, **k: None


def tearDownModule():  # noqa: N802 — unittest hook name
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)
    if _AGENTS_ROOT_BACKUP is None:
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
    else:
        os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_BACKUP
    if _LIVE_EMIT_BACKUP is None:
        os.environ.pop('OURLIBERTY_DISABLE_LIVE_EMIT', None)
    else:
        os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = _LIVE_EMIT_BACKUP
    if _AGENTS_ROOT_TMPDIR:
        shutil.rmtree(_AGENTS_ROOT_TMPDIR, ignore_errors=True)
    importlib.reload(swi)  # swi follows OURLIBERTY_AGENTS_ROOT too (prod-write isolation)
    importlib.reload(on)


def _good_outbox(**overrides):
    """Default result outbox shape (matches inbox_watcher._build_outbox)."""
    outbox = {
        'task_id': 'real-001',
        'agent': 'beacon',
        'source_task_file': '/home/larry/agents/inboxes/beacon/real-001.json',
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
                'source_task_file': '/inboxes/beacon/notify-real-001.json',
            }),
            1,
        )
        # Plain task → depth 0
        self.assertEqual(
            on._current_notify_depth({
                'source_task_file': '/inboxes/beacon/real-001.json',
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
        outbox = _good_outbox(agent='beacon', source='pulse', task_id='real-1')
        f = self._write_outbox('beacon', 'real-1.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        # Outbox archived
        self.assertFalse(f.exists())
        archive = on.OUTBOXES_ROOT / 'beacon' / '.archive' / 'real-1.json'
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
            agent='beacon', source='pulse', task_id='prod-fail',
            exit_code=-1, error='claude timed out after 3 attempts',
            result='',
        )
        f = self._write_outbox('beacon', 'prod-fail.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')

        notifies = list((on.INBOXES_ROOT / 'pulse').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        notify_data = json.loads(notifies[0].read_text())
        self.assertIn('FAILED', notify_data['prompt'])
        self.assertIn('claude timed out', notify_data['prompt'])

    def test_forge_question_routes_back_as_clarification(self):
        outbox = _good_outbox(
            agent='beacon', source='forge-question', task_id='realq-1',
            result='Use camelCase for the new field; the existing convention in agent-models.json is camelCase.',
        )
        f = self._write_outbox('beacon', 'realq-1.json', outbox)

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
        outbox = _good_outbox(agent='beacon', source='telegram-webhook', task_id='real-tg')
        f = self._write_outbox('beacon', 'real-tg.json', outbox)

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
            agent='forge', source='beacon-clarification', task_id='real-clar',
        )
        f = self._write_outbox('forge', 'real-clar.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'archived-no-notify')

    def test_self_dispatch_skipped(self):
        outbox = _good_outbox(agent='beacon', source='beacon', task_id='real-self')
        f = self._write_outbox('beacon', 'real-self.json', outbox)

        result = on.process_outbox(f)
        # Should be filtered by _should_notify_back (returns False for self-dispatch)
        # so archived-no-notify is the right return.
        self.assertIn(result, ('archived-no-notify', 'skip-self'))

    def test_depth_cap_blocks_second_hop_notify(self):
        # source_task_file starts with notify- → already depth 1
        outbox = _good_outbox(
            agent='beacon', source='pulse', task_id='real-deep',
            source_task_file='/home/larry/agents/inboxes/beacon/notify-prior.json',
        )
        f = self._write_outbox('beacon', 'real-deep.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'depth-cap')
        # No notify written
        self.assertEqual(
            list((on.INBOXES_ROOT / 'pulse').glob('notify-*.json')),
            [],
        )

    def test_explicit_notify_depth_field_respected(self):
        outbox = _good_outbox(
            agent='beacon', source='pulse', task_id='real-explicit-depth',
            _notify_depth=1,
        )
        f = self._write_outbox('beacon', 'real-explicit-depth.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'depth-cap')

    def test_short_result_prompt_padded_to_validator_floor(self):
        outbox = _good_outbox(agent='beacon', source='pulse', task_id='real-short', result='ok')
        f = self._write_outbox('beacon', 'real-short.json', outbox)

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
            task_id='real-1',
            success=True,
            output='cycle complete; 3 observations recorded',
        )
        # Header tag
        self.assertIn('[Inter-agent notify | intent=result-notification | from=beacon', prompt)
        self.assertIn('task=real-1', prompt)
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
            task_id='prod-fail',
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
            task_id='real-bad-spec',
            success=True,
            output='Spec references nonexistent file foo.md',
            intent_kwargs={'reason': 'Spec references nonexistent file foo.md'},
        )
        self.assertIn('reject', prompt)
        self.assertIn('REJECTED task `real-bad-spec`', prompt)
        self.assertIn('nonexistent file', prompt)
        self.assertIn('Do not retry without addressing', prompt)

    def test_dead_letter_intent(self):
        prompt = on.build_notify_prompt(
            intent='dead-letter',
            sender='inbox-watcher',
            task_id='real-rejected',
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
            task_id='real-broken',
            success=False,
            output='=== PROCEED ===\n{bad json}',
            intent_kwargs={
                'reason': 'invalid JSON: Expecting property name',
                'task_id': 'real-broken',
            },
        )
        self.assertIn('marker-error', prompt)
        self.assertIn('could not be parsed', prompt)
        self.assertIn('Re-read your CLAUDE.md', prompt)

    def test_unknown_intent_falls_back_to_default_action(self):
        prompt = on.build_notify_prompt(
            intent='unknown-future-intent',
            sender='beacon',
            task_id='real-x',
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
            task_id='real-1',
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
            task_id='real-tiny',
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
            task_id='real-pf',
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
            '{"task_id": "real-pf", "preflight_summary": "Will edit X line 12."}\n'
            '=== END_PROCEED ==='
        )
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 'real-pf.json', outbox)

        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['source'], 'forge-result')
        self.assertEqual(data['intent'], 'ack-proceed')
        self.assertIn('PROCEED on task `real-pf`', data['prompt'])
        self.assertIn('Will edit X line 12', data['prompt'])

    def test_clarify_request_routes_to_beacon_as_forge_question(self):
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "real-pf", "question": "Which line range exactly?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = self._forge_outbox(
            marker,
            clarification_count=0,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 'real-pf.json', outbox)

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
            '{"task_id": "real-pf", "reason": "Spec references missing file."}\n'
            '=== END_REJECT ==='
        )
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 'real-pf.json', outbox)

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
            '{"task_id": "real-pf", "question": "Final ambiguity?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = self._forge_outbox(
            marker,
            clarification_count=3,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 'real-pf.json', outbox)

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
            '{"task_id": "real-pf", "question": "Q?"}\n'
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
        f = self._write_outbox('forge', 'real-pf-resume.json', outbox)

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
            '{"task_id": "real-pf", "preflight_summary": "Got the clarification, building."}\n'
            '=== END_PROCEED ==='
        )
        outbox = self._forge_outbox(
            marker,
            source='beacon-clarification',
            clarification_count=1,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 'real-pf-resumed.json', outbox)

        result = on.process_outbox(f)
        # Without marker handling this would be 'archived-no-notify'
        self.assertEqual(result, 'notified-marker')

    def test_malformed_marker_dead_letters_back_to_forge(self):
        marker = '=== PROCEED ===\n{bad json}\n=== END_PROCEED ==='
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 'real-pf-bad.json', outbox)

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
            '{"task_id": "real-pf", "question": "Which line?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = self._forge_outbox(
            marker,
            target_repo='ourliberty-agent-core',
            branch='forge/real-pf',
            pr_title='docs: example title',
            pr_body='example body',
            clarification_count=0,
            max_clarifications=3,
        )
        f = self._write_outbox('forge', 'real-pf-clarify.json', outbox)
        on.process_outbox(f)

        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/real-pf')
        self.assertEqual(data['pr_title'], 'docs: example title')
        self.assertEqual(data['pr_body'], 'example body')

    def test_clarification_answer_leg_propagates_target_repo_and_branch(self):
        """Beacon's answer to a forge-question must carry target_repo/branch
        back to Forge so her re-preflight invocation passes the worktree gate."""
        # Simulate Beacon's outbox responding to a forge-question.
        outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='real-pf',
            target_repo='ourliberty-agent-core',
            branch='forge/real-pf',
            pr_title='docs: example title',
            pr_body='example body',
            clarification_count=1,
            max_clarifications=3,
            result='Use line 258 — the systemd-units table row.',
        )
        f = self._write_outbox('beacon', 'real-pf-answer.json', outbox)
        on.process_outbox(f)

        notifies = list((on.INBOXES_ROOT / 'forge').glob('notify-*.json'))
        self.assertEqual(len(notifies), 1)
        data = json.loads(notifies[0].read_text())
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/real-pf')
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
        f = self._write_outbox('forge', 'real-pf-bad-fields.json', outbox)
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
            '{"task_id": "real-recovered", "preflight_summary": "Recovered cleanly."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge',
            source='outbox-notifier',  # the previous marker-error notify
            task_id='real-recovered',
            result='Got the marker-error notify, here is my corrected marker.\n\n' + marker,
            original_source='beacon',  # propagated from the marker-error envelope
            marker_error_count=1,
        )
        f = self._write_outbox('forge', 'real-recovered.json', outbox)

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
            task_id='prod-loop',
            result=marker,
            original_source='beacon',
            marker_error_count=on.MAX_MARKER_ERROR_RETRIES,  # next retry exceeds cap
        )
        f = self._write_outbox('forge', 'prod-loop.json', outbox)

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
            '{"task_id": "real-1", "preflight_summary": "x"}\n'
            '=== END_PROCEED ===\n\n'
            'Wait, actually:\n\n'
            '=== REJECT ===\n'
            '{"task_id": "real-1", "reason": "y"}\n'
            '=== END_REJECT ==='
        )
        outbox = self._forge_outbox(marker)
        f = self._write_outbox('forge', 'real-pf-dup.json', outbox)

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
            agent='forge', source='beacon', task_id='real-cascade',
            result=(
                'Need a quick clarification.\n\n'
                '=== CLARIFY_REQUEST ===\n'
                '{"task_id": "real-cascade", "question": "Which line range?"}\n'
                '=== END_CLARIFY_REQUEST ==='
            ),
            clarification_count=0,
            max_clarifications=3,
        )
        f1 = self._write_outbox('forge', 'real-cascade-r1.json', forge_outbox_1)
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
            task_id='notify-real-cascade-r1',
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
            agent='forge', source='beacon-clarification', task_id='real-cascade',
            result=(
                '=== CLARIFY_REQUEST ===\n'
                '{"task_id": "real-cascade", "question": "And the file path?"}\n'
                '=== END_CLARIFY_REQUEST ==='
            ),
            clarification_count=1,
            max_clarifications=3,
        )
        f3 = self._write_outbox('forge', 'real-cascade-r3.json', forge_outbox_2)
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
            task_id='notify-real-cascade-r3',
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
            agent='forge', source='beacon-clarification', task_id='real-cascade',
            result=(
                'Got it. Building.\n\n'
                '=== PROCEED ===\n'
                '{"task_id": "real-cascade", "preflight_summary": "Edit docs/operating-manual.md L730-L740."}\n'
                '=== END_PROCEED ==='
            ),
            clarification_count=2,
            max_clarifications=3,
        )
        f5 = self._write_outbox('forge', 'real-cascade-r5.json', forge_outbox_3)
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
            agent='forge', source='beacon', task_id='real-legacy',
            result='Plain text result, no marker block here at all.',
        )
        f = self._write_outbox('forge', 'real-legacy.json', outbox)

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
            agent='beacon', source='pulse', task_id='real-beacon',
            result=(
                'Discussing the preflight protocol:\n\n'
                '=== PROCEED ===\n'
                '{"task_id": "x", "preflight_summary": "example"}\n'
                '=== END_PROCEED ==='
            ),
        )
        f = self._write_outbox('beacon', 'real-beacon.json', outbox)

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
            task_id='notify-real-legacy',
            # No forge_session_id — simulates pre-task-25 cascade in flight
            # when the upgrade ships.
            result='Legacy answer; chain in flight at upgrade time.' * 4,
            clarification_count=1,
            max_clarifications=3,
        )
        f = self._write_outbox('beacon', 'notify-real-legacy.json',
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
            '{"task_id": "real-pf", "question": "Which line range?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        forge_outbox = _good_outbox(
            agent='forge',
            source='beacon',
            task_id='real-pf',
            result=marker,
            clarification_count=0,
            max_clarifications=3,
            claude_session_id='forge-preflight-session-XYZ',
            target_repo='ourliberty-agent-core',
            branch='forge/real-pf',
        )
        f = self._write_outbox('forge', 'real-pf.json', forge_outbox)
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
            '{"task_id": "real-1", "preflight_summary": "ok"}\n'
            '=== END_PROCEED ==='
        )
        decision = on._classify_forge_marker({
            'agent': 'forge', 'result': result, 'task_id': 'real-1',
        })
        self.assertEqual(decision['marker_type'], 'proceed')
        self.assertEqual(decision['intent'], 'ack-proceed')
        self.assertEqual(decision['notify_source'], 'forge-result')
        self.assertIsNone(decision['next_clarification_count'])

    def test_clarify_under_budget(self):
        result = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "real-1", "question": "Q?"}\n'
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
            '{"task_id": "real-1", "question": "Last Q?"}\n'
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
                'agent': 'forge', 'result': result, 'task_id': 'real-mismatch',
            })
        self.assertIn('drifted-id', str(cm.exception))
        self.assertIn('real-mismatch', str(cm.exception))

    def test_marker_task_id_mismatch_records_deliverable_claim(self):
        # The cross-identity bridge (2026-06-20 redundant-build incident): on a
        # marker-task_id mismatch, record the CLAIMED id so a later board Launch
        # of that id can de-duplicate against this in-flight work.
        import launch_dedup_guard
        result = (
            '=== PROCEED ===\n'
            '{"task_id": "drifted-id", "preflight_summary": "ok"}\n'
            '=== END_PROCEED ==='
        )
        with mock.patch.object(launch_dedup_guard, 'record_claim') as rec:
            with self.assertRaises(on.fph.MalformedForgeMarker):
                on._classify_forge_marker({
                    'agent': 'forge', 'result': result,
                    'task_id': 'real-mismatch', 'target_repo': 'ourliberty-agent-core',
                })
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs['claimed_task_id'], 'drifted-id')
        self.assertEqual(rec.call_args.kwargs['envelope_task_id'], 'real-mismatch')

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


class EmitClarifyRequestChainEventTest(unittest.TestCase):
    """E4.4e PR-A: clarify_request chain_event emission at classification.

    Spec § 4 source #5 payload contract:
      asking_agent, task_id, question, resume_session_id (4 fields).
    """

    def test_emit_helper_carries_all_four_spec_fields_for_forge(self):
        captured = {}

        def _fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            on._emit_clarify_request_chain_event(
                data={
                    'task_id': 'task-clarify-001',
                    'claude_session_id': 'sess-abc-xyz',
                },
                marker_decision={
                    'marker_type': 'clarify_request',
                    'intent': 'clarify',
                    'payload': {
                        'task_id': 'task-clarify-001',
                        'question': 'Which interpretation of envelope X is right?',
                    },
                },
                agent='forge',
            )
        self.assertEqual(captured.get('event_type'), 'clarify_request')
        self.assertEqual(captured.get('agent'), 'forge')
        self.assertEqual(captured.get('task_id'), 'task-clarify-001')
        payload = captured.get('payload') or {}
        # All four spec § 4 fields.
        self.assertEqual(payload.get('asking_agent'), 'forge')
        self.assertEqual(payload.get('task_id'), 'task-clarify-001')
        self.assertEqual(payload.get('question'),
                          'Which interpretation of envelope X is right?')
        self.assertEqual(payload.get('resume_session_id'), 'sess-abc-xyz')

    def test_emit_helper_differentiates_asking_agent_for_mirror(self):
        captured = {}

        def _fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            on._emit_clarify_request_chain_event(
                data={'task_id': 'real-2', 'claude_session_id': 's-2'},
                marker_decision={
                    'intent': 'clarify',
                    'payload': {'question': 'Mirror question?'},
                },
                agent='mirror',
            )
        self.assertEqual(captured.get('agent'), 'mirror')
        self.assertEqual(captured['payload']['asking_agent'], 'mirror')

    def test_emit_helper_does_not_raise_on_emit_failure(self):
        # Daemon-never-wedge: if emit_event raises, the helper swallows
        # and logs. This is the invariant the BLE001 noqa in the helper
        # is justifying.
        def _raise(**_):
            raise RuntimeError('supabase blew up')

        with mock.patch.object(on.chain_event_emit, 'emit_event', _raise):
            # Should NOT propagate.
            on._emit_clarify_request_chain_event(
                data={'task_id': 'real-3'},
                marker_decision={
                    'intent': 'clarify',
                    'payload': {'question': 'Q?'},
                },
                agent='forge',
            )

    def test_emit_helper_handles_missing_payload_fields_gracefully(self):
        # Payload may be empty (defensive) — helper substitutes empty
        # strings rather than raising KeyError.
        captured = {}

        def _fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            on._emit_clarify_request_chain_event(
                data={'task_id': 'real-4'},
                marker_decision={'intent': 'clarify', 'payload': {}},
                agent='forge',
            )
        payload = captured.get('payload') or {}
        self.assertEqual(payload.get('question'), '')
        self.assertEqual(payload.get('resume_session_id'), '')


class EmitPreflightOutcomeChainEventTest(unittest.TestCase):
    """check-x-verdict-emission: Forge preflight-outcome chain_event emission.

    Sibling to EmitClarifyRequestChainEventTest. Records the preflight OUTCOME
    mix (proceed/clarify/reject) at the Forge classification site so Check X
    can read the mix from chain_events.
    """

    def _capture(self, marker_decision, *, task_id='task-pf-001'):
        captured = {}

        def _fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            on._emit_preflight_outcome_chain_event(
                data={'task_id': task_id},
                marker_decision=marker_decision,
                agent='forge',
            )
        return captured

    def test_proceed_marker_emits_preflight_proceed(self):
        captured = self._capture(
            {'marker_type': 'proceed', 'intent': 'build', 'payload': {}})
        self.assertEqual(captured.get('event_type'), 'preflight_proceed')
        self.assertEqual(captured.get('agent'), 'forge')
        self.assertEqual(captured.get('task_id'), 'task-pf-001')
        payload = captured.get('payload') or {}
        self.assertEqual(payload.get('agent'), 'forge')
        self.assertEqual(payload.get('marker_type'), 'proceed')
        self.assertEqual(payload.get('intent'), 'build')

    def test_reject_marker_emits_preflight_reject(self):
        captured = self._capture(
            {'marker_type': 'reject', 'intent': 'reject', 'payload': {}})
        self.assertEqual(captured.get('event_type'), 'preflight_reject')

    def test_in_budget_clarify_emits_preflight_clarify(self):
        captured = self._capture(
            {'marker_type': 'clarify_request', 'intent': 'clarify',
             'payload': {}})
        self.assertEqual(captured.get('event_type'), 'preflight_clarify')

    def test_budget_exhausted_clarify_emits_preflight_reject(self):
        # A clarify_request that did NOT route as 'clarify' (budget exhausted)
        # is structurally a reject and is recorded as preflight_reject.
        captured = self._capture(
            {'marker_type': 'clarify_request',
             'intent': 'clarification-exhausted', 'payload': {}})
        self.assertEqual(captured.get('event_type'), 'preflight_reject')

    def test_unknown_marker_emits_nothing(self):
        captured = self._capture(
            {'marker_type': 'something_else', 'intent': 'x', 'payload': {}})
        self.assertEqual(captured, {})

    def test_does_not_raise_on_emit_failure(self):
        # Daemon-never-wedge: emit_event raising must not propagate.
        def _raise(**_):
            raise RuntimeError('supabase blew up')

        with mock.patch.object(on.chain_event_emit, 'emit_event', _raise):
            on._emit_preflight_outcome_chain_event(
                data={'task_id': 'real-pf'},
                marker_decision={'marker_type': 'proceed', 'intent': 'build'},
                agent='forge',
            )


class EmitMirrorVerdictChainEventTest(unittest.TestCase):
    """check-x-verdict-emission: Mirror verdict chain_event emission.

    Sibling to EmitPreflightOutcomeChainEventTest. Records the Mirror verdict
    mix (PASS/REVISION/ESCALATE) keyed on the ROUTED intent, so an auto-
    promoted / budget-exhausted REVISION is recorded as review_escalate.
    """

    def _capture(self, marker_decision, *, task_id='task-rev-001'):
        captured = {}

        def _fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            on._emit_mirror_verdict_chain_event(
                data={'task_id': task_id},
                marker_decision=marker_decision,
                agent='mirror',
            )
        return captured

    def test_pass_verdict_emits_review_pass(self):
        captured = self._capture({
            'marker_type': 'review_pass',
            'intent': 'review-pass',
            'payload': {'pr_url': 'https://example/pr/1'},
        })
        self.assertEqual(captured.get('event_type'), 'review_pass')
        self.assertEqual(captured.get('agent'), 'mirror')
        self.assertEqual(captured.get('task_id'), 'task-rev-001')
        self.assertEqual(captured.get('pr_url'), 'https://example/pr/1')
        payload = captured.get('payload') or {}
        self.assertEqual(payload.get('verdict'), 'pass')
        self.assertEqual(payload.get('marker_type'), 'review_pass')
        self.assertFalse(payload.get('auto_promoted'))
        self.assertFalse(payload.get('budget_exhausted'))

    def test_clean_revision_emits_review_revision(self):
        captured = self._capture({
            'marker_type': 'review_revision',
            'intent': 'review-revision',
            'payload': {},
        })
        self.assertEqual(captured.get('event_type'), 'review_revision')
        self.assertEqual((captured.get('payload') or {}).get('verdict'),
                         'revision')

    def test_escalate_verdict_emits_review_escalate(self):
        captured = self._capture({
            'marker_type': 'review_escalate',
            'intent': 'review-escalate',
            'payload': {},
        })
        self.assertEqual(captured.get('event_type'), 'review_escalate')

    def test_auto_promoted_revision_emits_review_escalate(self):
        # REVISION auto-promoted to ESCALATE (low confidence): routed intent is
        # review-escalate, so it is recorded as an escalation for the mix.
        captured = self._capture({
            'marker_type': 'review_revision',
            'intent': 'review-escalate',
            'auto_promoted': True,
            'payload': {},
        })
        self.assertEqual(captured.get('event_type'), 'review_escalate')
        self.assertTrue((captured.get('payload') or {}).get('auto_promoted'))

    def test_emergency_halt_folds_into_review_escalate(self):
        captured = self._capture({
            'marker_type': 'review_emergency_halt',
            'intent': 'emergency-halt',
            'payload': {},
        })
        self.assertEqual(captured.get('event_type'), 'review_escalate')

    def test_unknown_verdict_emits_nothing(self):
        captured = self._capture({
            'marker_type': 'notify',
            'intent': 'notify',
            'payload': {},
        })
        self.assertEqual(captured, {})

    def test_does_not_raise_on_emit_failure(self):
        def _raise(**_):
            raise RuntimeError('supabase blew up')

        with mock.patch.object(on.chain_event_emit, 'emit_event', _raise):
            on._emit_mirror_verdict_chain_event(
                data={'task_id': 'real-rev'},
                marker_decision={'marker_type': 'review_pass',
                                 'intent': 'review-pass', 'payload': {}},
                agent='mirror',
            )


class EmitClarifyResponseChainEventTest(unittest.TestCase):
    """clarify-round-visibility § 6: clarify_response chain_event emission.

    Sibling to EmitClarifyRequestChainEventTest. The helper fires from
    `_handle_beacon_clarification_response` after Beacon's answer routes
    to Forge's resume envelope, and pushes a `clarify_response` row so the
    dashboard can render the Q+A round-trip.
    """

    def test_emit_helper_carries_all_payload_fields(self):
        captured = {}

        def _fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            on._emit_clarify_response_chain_event(
                task_id='task-clarify-001',
                question='Inbound clarify prompt from Forge',
                answer='Beacon answer body',
                clarification_round=1,
            )
        self.assertEqual(captured.get('event_type'), 'clarify_response')
        self.assertEqual(captured.get('agent'), 'beacon')
        self.assertEqual(captured.get('task_id'), 'task-clarify-001')
        payload = captured.get('payload') or {}
        self.assertEqual(payload.get('task_id'), 'task-clarify-001')
        self.assertEqual(payload.get('clarification_round'), 1)
        self.assertEqual(payload.get('question'),
                         'Inbound clarify prompt from Forge')
        self.assertEqual(payload.get('answer'), 'Beacon answer body')
        self.assertIn('responded_at', payload)

    def test_emit_helper_does_not_raise_on_emit_failure(self):
        # Daemon-never-wedge: emit_event raising must not propagate.
        def _raise(**_):
            raise RuntimeError('supabase blew up')

        with mock.patch.object(on.chain_event_emit, 'emit_event', _raise):
            on._emit_clarify_response_chain_event(
                task_id='real-r',
                question='Q',
                answer='A',
                clarification_round=2,
            )

    def test_handler_fires_emit_on_successful_resume_dispatch(self):
        # Integration: _handle_beacon_clarification_response calls the
        # emit helper exactly once with fields sourced from the Beacon
        # outbox. The shape mirrors the HeadlessClarificationRoutingTest
        # happy path so any future shape drift surfaces here too.
        captured = []

        def _fake_emit(**kwargs):
            captured.append(kwargs)
            return True

        beacon_outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='notify-task-roundtrip-001',
            forge_session_id='forge-sess-rt-001',
            result='Beacon\'s answer text long enough for any future validator.' * 3,
            clarification_count=1,
            max_clarifications=3,
            target_repo='ourliberty-agent-core',
            branch='forge/task-roundtrip-001',
            prompt='[Inter-agent notify | intent=clarify | from=forge | '
                   'task=task-roundtrip-001]\n\nForge\'s clarify question body.',
        )

        # Need tmp dirs because the handler writes to the inbox.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        on_orig = {n: getattr(on, n) for n in (
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT',
            'BLACKBOARD', 'LOG_FILE', 'DEAD_LETTER_STATE',
            'EMERGENCY_HALT_FLAG',
        )}
        on.AGENTS_ROOT = root
        on.INBOXES_ROOT = root / 'inboxes'
        on.OUTBOXES_ROOT = root / 'outboxes'
        on.BLACKBOARD = root / 'blackboard'
        on.LOG_FILE = root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        swi_orig = {n: getattr(swi, n) for n in (
            'AGENTS_ROOT', 'INBOXES_ROOT', 'ROUTING_EVENTS_LOG',
        )}
        swi.AGENTS_ROOT = root
        swi.INBOXES_ROOT = root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = root / 'logs' / 'routing-events.jsonl'
        rv_orig = rv.REPO_ROOT
        rv.REPO_ROOT = root / 'repo'
        rv.invalidate_cache()
        on.ensure_dirs()

        def _restore():
            for n, v in on_orig.items():
                setattr(on, n, v)
            for n, v in swi_orig.items():
                setattr(swi, n, v)
            rv.REPO_ROOT = rv_orig
            rv.invalidate_cache()
        self.addCleanup(_restore)

        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            result = on._handle_beacon_clarification_response(beacon_outbox)

        # Resume envelope landed (sanity — confirms we hit the success
        # branch where the emit fires).
        self.assertIsNotNone(result)
        self.assertEqual(len(captured), 1,
                         f'expected exactly one emit, got {len(captured)}')
        kwargs = captured[0]
        self.assertEqual(kwargs.get('event_type'), 'clarify_response')
        self.assertEqual(kwargs.get('agent'), 'beacon')
        # task_id stripped of `notify-` prefix.
        self.assertEqual(kwargs.get('task_id'), 'task-roundtrip-001')
        payload = kwargs.get('payload') or {}
        self.assertEqual(payload.get('clarification_round'), 1)
        self.assertIn('Forge\'s clarify question body', payload.get('question', ''))
        self.assertIn('Beacon\'s answer text', payload.get('answer', ''))

    def test_handler_does_not_emit_when_falling_through_to_default_routing(self):
        # Negative case: a Beacon outbox without forge_session_id falls back
        # to default notify routing (the legacy path). The emit must NOT fire
        # because clarify_response is for the resume-dispatch surface only.
        captured = []

        def _fake_emit(**kwargs):
            captured.append(kwargs)
            return True

        legacy_outbox = _good_outbox(
            agent='beacon',
            source='forge-question',
            task_id='notify-real-legacy',
            # No forge_session_id — handler returns None early.
            result='Legacy answer; chain in flight at upgrade time.',
            clarification_count=1,
            max_clarifications=3,
        )
        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            res = on._handle_beacon_clarification_response(legacy_outbox)
        self.assertIsNone(res)
        self.assertEqual(captured, [],
                         'emit must not fire on legacy fall-through path')

    def test_handler_does_not_emit_for_non_clarification_beacon_outbox(self):
        # Negative case: a Beacon outbox whose source is not `*-question`
        # is not a clarification response. Handler returns early; no emit.
        captured = []

        def _fake_emit(**kwargs):
            captured.append(kwargs)
            return True

        regular_outbox = _good_outbox(
            agent='beacon',
            source='telegram-webhook',  # Not *-question.
            task_id='real-regular',
            result='Beacon doing some other work.',
        )
        with mock.patch.object(on.chain_event_emit, 'emit_event', _fake_emit):
            res = on._handle_beacon_clarification_response(regular_outbox)
        self.assertIsNone(res)
        self.assertEqual(captured, [],
                         'emit must not fire when handler declines the path')


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
            '{"task_id": "real-pf", "preflight_summary": "Will edit watchdog doc."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='real-pf',
            claude_session_id='sess-preflight-xyz',
            result=f'Read the spec. Ready to act.\n\n{marker}',
        )
        outbox.update(overrides)
        return outbox

    def test_proceed_writes_build_phase_task_to_forge(self):
        outbox = self._forge_proceed_outbox(target_repo='ourliberty-agent-core')
        f = self._write_outbox('forge', 'real-pf.json', outbox)

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
        self.assertEqual(build_data['task_id'], 'real-pf')
        self.assertEqual(build_data['dispatched_by'], 'outbox-notifier')
        self.assertIn('Build phase', build_data['prompt'])

    def test_proceed_without_session_id_skips_build_phase(self):
        outbox = self._forge_proceed_outbox(
            claude_session_id=None,
            target_repo='ourliberty-agent-core',
        )
        f = self._write_outbox('forge', 'real-pf-nosid.json', outbox)

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
        f = self._write_outbox('forge', 'real-pf-branch.json', outbox)

        # Fail-open the phantom-build terminal guard (no real `gh` shell-out);
        # PhantomBuildTerminalGuardTest covers the terminal-state behavior.
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch', return_value=None,
        ):
            on.process_outbox(f)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1)
        build_data = json.loads(forge_builds[0].read_text())
        self.assertEqual(build_data['branch'], 'forge/watchdog-fix-001')

    def test_resumed_build_with_stale_proceed_does_not_redispatch(self):
        # resumed-build-stale-marker guard (fix 1a). A `phase=build` outbox
        # RESUMES the preflight session, so a stale `=== PROCEED ===` is still
        # in its transcript. The classifier must NOT re-discover it and re-
        # dispatch the build (the 2026-06-20 strand). Embedding the marker in
        # `result` exercises the same classification path the session-log scan
        # hits. Before the guard this wrote a build-phase task + returned
        # 'notified-marker'; after it, neither happens.
        stale = (
            '=== PROCEED ===\n'
            '{"task_id": "slice-x", "preflight_summary": "stale"}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='slice-x',
            phase='build', target_repo='ourliberty-agent-core',
            claude_session_id='sess-build-xyz',
            result=f'Build phase narrative; work shipped.\n\n{stale}',
        )
        f = self._write_outbox('forge', 'slice-x.json', outbox)

        result = on.process_outbox(f)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(
            forge_builds, [],
            msg='a resumed build must not re-dispatch itself off a stale PROCEED',
        )
        self.assertNotEqual(result, 'notified-marker')

    def test_proceed_with_max_clarifications_propagates(self):
        outbox = self._forge_proceed_outbox(
            target_repo='ourliberty-agent-core',
            max_clarifications=5,
        )
        f = self._write_outbox('forge', 'real-pf-mc.json', outbox)
        on.process_outbox(f)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        build_data = json.loads(forge_builds[0].read_text())
        self.assertEqual(build_data['max_clarifications'], 5)

    def test_clarify_request_does_not_trigger_build_phase(self):
        # Only PROCEED triggers build phase. CLARIFY/REJECT do not.
        marker = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "prod-clr", "question": "Which file?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='prod-clr',
            claude_session_id='sess-clr',
            clarification_count=0, max_clarifications=3,
            target_repo='ourliberty-agent-core',
            result=f'Need more info.\n\n{marker}',
        )
        f = self._write_outbox('forge', 'prod-clr.json', outbox)

        on.process_outbox(f)

        # Beacon gets the clarify notify; Forge inbox stays empty.
        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 0)

    def test_reject_does_not_trigger_build_phase(self):
        marker = (
            '=== REJECT ===\n'
            '{"task_id": "real-rej", "reason": "Spec impossible."}\n'
            '=== END_REJECT ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='real-rej',
            target_repo='ourliberty-agent-core',
            result=f'Cannot proceed.\n\n{marker}',
        )
        f = self._write_outbox('forge', 'real-rej.json', outbox)

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
            agent='forge', source='beacon', task_id='real-mismatch',
            claude_session_id='sess-1',
            target_repo='ourliberty-agent-core',
            result=f'Reasoning.\n\n{marker}',
        )
        f = self._write_outbox('forge', 'real-mismatch.json', outbox)

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
        f = self._write_outbox('forge', 'real-pf-bad-repo.json', outbox)

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
        f1 = self._write_outbox('forge', 'real-pf-idem.json', outbox)
        on.process_outbox(f1)

        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1)

        # Second pass — write the same preflight outbox back and process.
        f2 = self._write_outbox('forge', 'real-pf-idem.json', outbox)
        on.process_outbox(f2)
        forge_builds = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(forge_builds), 1, 'duplicate build task written')

    def test_pr_title_and_body_propagate_to_build_task(self):
        outbox = self._forge_proceed_outbox(
            target_repo='ourliberty-agent-core',
            pr_title='fix(watchdog): clarify enabled flag in docs',
            pr_body='## Summary\nDocumentation fix.',
        )
        f = self._write_outbox('forge', 'real-pf-prfields.json', outbox)
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


class BuildDedupSpawnFailureOverrideTest(unittest.TestCase):
    """Build-dedup wedge fix (2026-06-09): a stale archived build-<task>.json
    must NOT permanently block re-dispatch when the prior build was a spawn-
    failure (worker never ran, no PR). The override fires ONLY on a definitive
    terminal spawn-failure; absence of a terminal result keeps the conservative
    crash-recovery skip."""

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

    # ---- seeding helpers -------------------------------------------------

    def _seed_inbox_archive(self, task_id):
        """Plant the stale build-<task>.json in Forge's inbox archive."""
        archive = on.INBOXES_ROOT / 'forge' / '.archive'
        archive.mkdir(parents=True, exist_ok=True)
        (archive / f'build-{task_id}.json').write_text('{}')

    def _seed_outbox_result(self, task_id, body, suffix='.1'):
        """Plant a build-phase RESULT envelope in Forge's outbox archive."""
        archive = on.OUTBOXES_ROOT / 'forge' / '.archive'
        archive.mkdir(parents=True, exist_ok=True)
        f = archive / f'{task_id}{suffix}.json'
        f.write_text(json.dumps(body))
        return f

    def _spawn_failure_result(self, task_id):
        # Mirrors the real artifact register-ol-db-ro-url-credential.1.json:
        # exit_code -1, 'All retries exhausted', duration null, no PR.
        return {
            'task_id': task_id,
            'agent': 'forge',
            'source': 'beacon',
            'phase': 'build',
            'exit_code': -1,
            'result': 'All retries exhausted',
            'error': 'All retries exhausted',
            'duration_sec': None,
            'pr_url': None,
            'account_tier': None,
            'cost_usd': None,
            'completed_at': '2026-06-09T16:30:22.620537+00:00',
        }

    def _ran_build_result(self, task_id):
        return {
            'task_id': task_id,
            'agent': 'forge',
            'source': 'beacon',
            'phase': 'build',
            'exit_code': 0,
            'result': (
                'PR opened: '
                'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/401'
                '\n\nImplemented the change.'
            ),
            'error': None,
            'duration_sec': 142.5,
            'cost_usd': 0.41,
            'completed_at': '2026-06-09T17:00:00.000000+00:00',
        }

    def _build_data(self, task_id):
        return {
            'task_id': task_id,
            'claude_session_id': 'sess-resume-xyz',
            'target_repo': 'ourliberty-agent-core',
            'branch': f'forge/{task_id}',
        }

    # ---- tests -----------------------------------------------------------

    def test_build_dedup_overridden_on_spawn_failure(self):
        task_id = 'wedged-task'
        self._seed_inbox_archive(task_id)
        self._seed_outbox_result(task_id, self._spawn_failure_result(task_id))

        # The spawn-failure override falls through to the phantom-build terminal
        # guard; fail it open (no real `gh`) so this test isolates the override.
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch', return_value=None,
        ):
            on._dispatch_build_phase(self._build_data(task_id))

        # The override fired: a fresh build task is written to the live inbox.
        live = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(len(live), 1, 'spawn-failure should allow re-dispatch')
        self.assertIn('BUILD_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_build_dedup_still_skips_when_prior_build_ran(self):
        task_id = 'really-built-task'
        self._seed_inbox_archive(task_id)
        self._seed_outbox_result(task_id, self._ran_build_result(task_id))

        on._dispatch_build_phase(self._build_data(task_id))

        live = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(live, [], 'a real prior build must still skip')
        self.assertNotIn('BUILD_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_build_dedup_skips_when_no_terminal_result(self):
        # Notifier crashed mid-build: the inbox artifact is archived but NO
        # terminal build result exists. The override must NOT fire.
        task_id = 'crashed-midbuild-task'
        self._seed_inbox_archive(task_id)
        # (no outbox result seeded)

        on._dispatch_build_phase(self._build_data(task_id))

        live = list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))
        self.assertEqual(live, [], 'no terminal result -> conservative skip')
        self.assertNotIn('BUILD_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_build_dedup_inbox_present_always_skips(self):
        # A build currently in the LIVE inbox is in-flight; even with a prior
        # spawn-failure result present, never double-dispatch it.
        task_id = 'inflight-task'
        live_inbox = on.INBOXES_ROOT / 'forge'
        live_inbox.mkdir(parents=True, exist_ok=True)
        (live_inbox / f'build-{task_id}.json').write_text('{}')
        self._seed_outbox_result(task_id, self._spawn_failure_result(task_id))

        on._dispatch_build_phase(self._build_data(task_id))

        # Still exactly the one we planted; no second build written.
        live = list(live_inbox.glob('build-*.json'))
        self.assertEqual(len(live), 1)
        self.assertNotIn('BUILD_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_spawn_failure_helper_defensive(self):
        # Missing archive dir -> False.
        self.assertFalse(on._prior_build_was_spawn_failure('no-such-task'))

        # Corrupt result envelope -> False (parse error is conservative).
        archive = on.OUTBOXES_ROOT / 'forge' / '.archive'
        archive.mkdir(parents=True, exist_ok=True)
        (archive / 'corrupt-task.1.json').write_text('{not valid json')
        self.assertFalse(on._prior_build_was_spawn_failure('corrupt-task'))

        # A preflight (non-build) result must not satisfy the build check.
        preflight = self._spawn_failure_result('pf-task')
        preflight['phase'] = 'preflight'
        self._seed_outbox_result('pf-task', preflight)
        self.assertFalse(on._prior_build_was_spawn_failure('pf-task'))

        # Prefix-collision guard: a result for `<task>-v2` must not match `<task>`.
        self._seed_outbox_result(
            'prefix-task-v2', self._spawn_failure_result('prefix-task-v2')
        )
        self.assertFalse(on._prior_build_was_spawn_failure('prefix-task'))

        # The positive case still returns True.
        self._seed_outbox_result(
            'good-fail', self._spawn_failure_result('good-fail')
        )
        self.assertTrue(on._prior_build_was_spawn_failure('good-fail'))


class PhantomBuildTerminalGuardTest(unittest.TestCase):
    """phantom-build-phase terminal guard (cap-phantom-build-phase-after-marker-
    error-retry-pr-4d78). A marker-error retry can resume a Forge preflight
    session and re-discover a STALE `=== PROCEED ===` in the resumed transcript,
    re-classifying it as a fresh proceed and re-dispatching a build for an
    ALREADY-TERMINAL task. The GitHub-truth guard in `_dispatch_build_phase`
    skips the dispatch ONLY when the build branch's PR is in a terminal state
    (MERGED / CLOSED-unmerged); every other case (OPEN PR / no PR / gh failure /
    no branch) fails open and dispatches."""

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

    def _build_data(self, task_id, **overrides):
        data = {
            'task_id': task_id,
            'claude_session_id': 'sess-resume-xyz',
            'target_repo': 'ourliberty-agent-core',
            'branch': f'forge/{task_id}',
        }
        data.update(overrides)
        return data

    def _forge_builds(self):
        return list((on.INBOXES_ROOT / 'forge').glob('build-*.json'))

    def _beacon_notifies(self):
        return list(
            (on.INBOXES_ROOT / 'beacon').glob(
                'notify-phantom-build-suppressed-*.json'
            )
        )

    # ---- criterion 1: phantom suppressed on terminal PR ------------------

    def test_phantom_suppressed_when_branch_pr_merged(self):
        task_id = 'fix-classifier-session-lost-002'
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch', return_value='MERGED',
        ):
            on._dispatch_build_phase(self._build_data(task_id))

        self.assertEqual(
            self._forge_builds(), [],
            msg='a build whose branch already MERGED must not be dispatched',
        )
        self.assertIn('PHANTOM_BUILD_SUPPRESSED', on.LOG_FILE.read_text())
        # Best-effort informational journal to Beacon was written.
        self.assertEqual(len(self._beacon_notifies()), 1)

    def test_phantom_suppressed_when_branch_pr_closed_unmerged(self):
        # CLOSED-without-merge is terminal too — also a phantom.
        task_id = 'closed-unmerged-task'
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch', return_value='CLOSED',
        ):
            on._dispatch_build_phase(self._build_data(task_id))

        self.assertEqual(self._forge_builds(), [])
        self.assertIn('PHANTOM_BUILD_SUPPRESSED', on.LOG_FILE.read_text())

    # ---- criterion 2: legitimate paths still dispatch --------------------

    def test_dispatched_when_no_pr_for_branch(self):
        # No PR for the branch -> None -> fail-open -> dispatch.
        task_id = 'first-build-task'
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch', return_value=None,
        ):
            on._dispatch_build_phase(self._build_data(task_id))

        self.assertEqual(len(self._forge_builds()), 1)
        self.assertNotIn('PHANTOM_BUILD_SUPPRESSED', on.LOG_FILE.read_text())

    def test_dispatched_when_branch_pr_open_replan(self):
        # An OPEN PR is the legitimate replan/revision re-dispatch — NEVER skip.
        task_id = 'replan-task'
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch', return_value='OPEN',
        ):
            on._dispatch_build_phase(self._build_data(task_id))

        self.assertEqual(len(self._forge_builds()), 1)
        self.assertNotIn('PHANTOM_BUILD_SUPPRESSED', on.LOG_FILE.read_text())

    def test_failopen_when_gh_lookup_raises(self):
        # daemon-never-wedge: any exception from the lookup -> fail-open.
        task_id = 'gh-outage-task'
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch',
            side_effect=RuntimeError('gh boom'),
        ):
            on._dispatch_build_phase(self._build_data(task_id))

        self.assertEqual(len(self._forge_builds()), 1)
        self.assertIn('failing open', on.LOG_FILE.read_text())

    def test_dispatched_when_branch_absent(self):
        # `branch` is optional; absent -> the lookup can't run -> dispatch.
        # The stub raises to prove the guard never invokes it without a branch.
        task_id = 'no-branch-task'
        data = self._build_data(task_id)
        data.pop('branch')
        with mock.patch.object(
            on, '_gh_terminal_pr_state_for_branch',
            side_effect=AssertionError('lookup must not run without a branch'),
        ):
            on._dispatch_build_phase(data)

        self.assertEqual(len(self._forge_builds()), 1)
        self.assertNotIn('PHANTOM_BUILD_SUPPRESSED', on.LOG_FILE.read_text())


class GhTerminalPrStateForBranchTest(unittest.TestCase):
    """Unit tests for `_gh_terminal_pr_state_for_branch`'s state collapse +
    fail-open transport handling (mocking the `gh pr list` subprocess)."""

    def _patch_run(self, **kwargs):
        return mock.patch.object(
            on.subprocess, 'run', return_value=mock.Mock(**kwargs),
        )

    def test_open_wins_over_terminal_siblings(self):
        # A reopened/replanned branch with both a merged and an open PR is OPEN.
        out = json.dumps([
            {'number': 1, 'state': 'MERGED'},
            {'number': 2, 'state': 'OPEN'},
        ])
        with self._patch_run(returncode=0, stdout=out, stderr=''):
            self.assertEqual(
                on._gh_terminal_pr_state_for_branch('o/r', 'b'), 'OPEN',
            )

    def test_merged_when_only_merged(self):
        out = json.dumps([{'number': 1, 'state': 'MERGED'}])
        with self._patch_run(returncode=0, stdout=out, stderr=''):
            self.assertEqual(
                on._gh_terminal_pr_state_for_branch('o/r', 'b'), 'MERGED',
            )

    def test_closed_when_only_closed_unmerged(self):
        out = json.dumps([{'number': 1, 'state': 'CLOSED'}])
        with self._patch_run(returncode=0, stdout=out, stderr=''):
            self.assertEqual(
                on._gh_terminal_pr_state_for_branch('o/r', 'b'), 'CLOSED',
            )

    def test_none_when_no_pr_for_branch(self):
        with self._patch_run(returncode=0, stdout='[]', stderr=''):
            self.assertIsNone(on._gh_terminal_pr_state_for_branch('o/r', 'b'))

    def test_none_on_nonzero_exit(self):
        with self._patch_run(returncode=1, stdout='', stderr='auth required'):
            self.assertIsNone(on._gh_terminal_pr_state_for_branch('o/r', 'b'))

    def test_none_on_unparseable_output(self):
        with self._patch_run(returncode=0, stdout='not json', stderr=''):
            self.assertIsNone(on._gh_terminal_pr_state_for_branch('o/r', 'b'))

    def test_none_on_timeout(self):
        with mock.patch.object(
            on.subprocess, 'run',
            side_effect=subprocess.TimeoutExpired(cmd='gh', timeout=1),
        ):
            self.assertIsNone(on._gh_terminal_pr_state_for_branch('o/r', 'b'))


# ============================================================================
# D3.5 commit 5a — Mirror review marker pipeline + preflight-discipline gate
# ============================================================================


PR_URL_FIXTURE = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'


def _mirror_pass_marker(task_id='real-rev', summary='AC coverage clean.'):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL_FIXTURE, 'summary': summary,
    })
    return f'=== REVIEW_PASS ===\n{payload}\n=== END_REVIEW_PASS ==='


def _mirror_revision_marker(
    task_id='real-rev', severity='medium', confidence='high', findings=None,
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
    task_id='real-rev', severity='high', confidence='high',
    reason='Spec mismatch; needs replan.',
):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL_FIXTURE,
        'reason': reason, 'severity': severity, 'confidence': confidence,
    })
    return f'=== REVIEW_ESCALATE ===\n{payload}\n=== END_REVIEW_ESCALATE ==='


def _mirror_emergency_marker(
    task_id='real-rev',
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
    BY beacon (logically). task_id defaults to 'real-rev'. `phase: 'review'`
    is propagated so the envelope matches what `_dispatch_mirror_review`
    actually writes.
    """
    base = _good_outbox(
        agent='mirror', source='beacon', task_id='real-rev', phase='review',
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

    def test_no_marker_non_review_phase_returns_none(self):
        # fix-mirror-verdict-marker-gate-001: the legitimate None path —
        # a Mirror output that is NOT a phase=review dispatch (chat-mode,
        # no phase field) and carries no marker still falls through to
        # default routing. The review-discipline gate must NOT fire here.
        data = _mirror_outbox_body(
            result='Just chat output, no marker.', phase=None,
        )
        self.assertIsNone(on._classify_mirror_marker(data))

    def test_review_phase_prose_verdict_raises_kickback(self):
        # fix-mirror-verdict-marker-gate-001 — the PR #277 regression. A
        # phase=review dispatch whose result is a PROSE verdict
        # ("**Verdict: PASS.**") carries no canonical marker, no `===`
        # delimiters, and no bare REVIEW_* keyword — so parse_mirror_marker
        # returns None. Pre-fix this silently returned None and auto-merge
        # never fired. The gate now RAISES into the marker-error kickback.
        data = _mirror_outbox_body(
            result=(
                '**Verdict: PASS.** Verification summary: all ACs met, '
                'tests green. The REVIEW_PASS marker is emitted above.'
            ),
        )
        with self.assertRaises(on.mrh.MalformedMirrorMarker) as ctx:
            on._classify_mirror_marker(data)
        msg = str(ctx.exception)
        self.assertIn('phase=review', msg)
        self.assertIn('canonical', msg)

    def test_dag_preflight_result_path_returns_none(self):
        # fix-mirror-verdict-marker-gate-001 — highest-risk regression guard.
        # The DAG-preflight path uses a `review-sequence-dag <seq-id>` prompt,
        # emits `result: PASS` (NOT a REVIEW_* marker), and carries no
        # phase=review. The gate must NOT catch it — it short-circuits to
        # None at the top of _classify_mirror_marker.
        data = _mirror_outbox_body(
            result='result: PASS\n\nThe sequence DAG is sound.',
            prompt='review-sequence-dag seq-build-001',
            phase=None,
        )
        self.assertIsNone(on._classify_mirror_marker(data))

    def test_dag_preflight_prompt_excluded_even_if_phase_review(self):
        # Belt-and-suspenders: even if a DAG-preflight envelope somehow
        # carried phase=review, the `review-sequence-dag` prompt prefix
        # short-circuits to None BEFORE the gate is reached.
        data = _mirror_outbox_body(
            result='result: REVISION\n\nThe DAG has a cycle.',
            prompt='review-sequence-dag seq-build-002',
        )
        self.assertIsNone(on._classify_mirror_marker(data))

    def test_envelope_task_id_mismatch_raises(self):
        # Marker says real-other but envelope says real-rev.
        marker = _mirror_pass_marker(task_id='real-other')
        data = _mirror_outbox_body(marker, task_id='real-rev')
        with self.assertRaises(on.mrh.MalformedMirrorMarker) as ctx:
            on._classify_mirror_marker(data)
        self.assertIn('task_id', str(ctx.exception))

    def test_malformed_marker_propagates(self):
        # Missing required field (no summary on PASS).
        bad_payload = json.dumps({'task_id': 'real-rev', 'pr_url': PR_URL_FIXTURE})
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


class ClassifyMirrorMarkerSessionLogFallbackTest(unittest.TestCase):
    """Multi-turn outbox-overwrite recovery (mirror-multiturn-outbox-overwrite-001).

    Reproduces 2026-05-24 PR #77: Mirror invoked Monitor at 22:00:31Z, emitted
    REVIEW_PASS at 22:01:33Z while Monitor was still pending, Monitor fired its
    timeout event at 22:05:31Z waking her for an extra turn, and her final-turn
    text — "Monitor timed out after the review was already complete" —
    overwrote the `result` field. AUTO_MERGE never fired; PR #77 sat for ~1.5h
    until Larry merged manually. Fix: fall back to the Claude session log when
    the outbox `result` has no marker.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._projects_root = Path(self._tmp.name)
        self._patcher = mock.patch.object(
            on, 'CLAUDE_PROJECTS_ROOT', self._projects_root,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _write_session_log(self, session_id, assistant_turns):
        """Build a synthetic Claude session jsonl with the given assistant turns.

        `assistant_turns` is a list of strings — each becomes one assistant
        message with a single text content block. Order is preserved.
        """
        project_dir = self._projects_root / '-fake-project'
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f'{session_id}.jsonl'
        with path.open('w') as fh:
            for text in assistant_turns:
                fh.write(json.dumps({
                    'type': 'assistant',
                    'message': {
                        'role': 'assistant',
                        'content': [{'type': 'text', 'text': text}],
                    },
                }) + '\n')
        return path

    def test_recovers_pass_marker_when_result_overwritten_by_post_marker_turn(self):
        # Exactly the PR #77 shape: Monitor timeout text in the final turn,
        # REVIEW_PASS in the prior turn that's gone from `result`.
        session_id = 'sess-multi-turn-pass'
        self._write_session_log(session_id, [
            'Reviewed the PR diff. AC coverage clean.',
            _mirror_pass_marker(summary='Coverage clean. Approving.'),
            'Monitor timed out after the review was already complete; '
            'no action needed.',
        ])
        data = _mirror_outbox_body(
            result='Monitor timed out after the review was already complete; '
                   'no action needed.',
            claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertIsNotNone(decision)
        self.assertEqual(decision['marker_type'], 'review_pass')
        self.assertEqual(decision['intent'], 'review-pass')
        self.assertIn('Coverage clean', decision['intent_kwargs']['summary'])

    def test_recovers_latest_marker_when_verdict_changed_across_turns(self):
        # PASS then REVISION across turns — the non-PASS verdict wins (here it
        # is also the latest, so both last-wins and conservative-priority agree).
        session_id = 'sess-verdict-changed'
        self._write_session_log(session_id, [
            _mirror_pass_marker(summary='Initial approval.'),
            _mirror_revision_marker(confidence='high'),
            'Final notes after my revised verdict.',
        ])
        data = _mirror_outbox_body(
            result='Final notes after my revised verdict.',
            claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_revision')
        self.assertEqual(decision['intent'], 'review-revision')

    def test_echoed_pass_does_not_override_earlier_revision(self):
        # nervous-system-audit #14 (2026-06-05) — the dangerous ordering:
        # REVISION emitted first, then a LATER turn ECHOES a
        # `=== REVIEW_PASS ===` block. Plain last-wins would pick the echoed
        # PASS and auto-merge a PR Mirror wanted revised. Conservative-priority
        # keeps the non-PASS verdict.
        session_id = 'sess-echoed-pass'
        self._write_session_log(session_id, [
            _mirror_revision_marker(confidence='high'),
            'Restating my earlier note for the record:\n\n'
            + _mirror_pass_marker(summary='(echoed) approving.'),
        ])
        data = _mirror_outbox_body(
            result='Restating my earlier note for the record.',
            claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_revision')
        self.assertEqual(decision['intent'], 'review-revision')

    def test_escalate_beats_a_later_pass(self):
        # Severity priority: an ESCALATE anywhere in the session beats a later
        # PASS — never auto-merge when Mirror flagged a replan-worthy concern.
        session_id = 'sess-escalate-then-pass'
        self._write_session_log(session_id, [
            _mirror_escalate_marker(),
            _mirror_pass_marker(summary='(echoed) looks fine after all.'),
        ])
        data = _mirror_outbox_body(
            result='Echoed pass.', claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_escalate')

    def test_single_pass_verdict_still_routes_pass(self):
        # No conflict — a clean single-verdict REVIEW_PASS session still routes
        # pass (conservative-priority only engages on multi-verdict-type
        # sessions; the normal single-verdict case is unchanged last-wins).
        session_id = 'sess-clean-pass'
        self._write_session_log(session_id, [
            'Reviewed the diff. Looks good.',
            _mirror_pass_marker(summary='All clear.'),
        ])
        data = _mirror_outbox_body(
            result='post-marker chatter', claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_pass')

    def test_fallback_review_phase_raises_when_session_log_absent(self):
        # fix-mirror-verdict-marker-gate-001: no log file AND no recoverable
        # marker in result_text. Under the review-discipline gate, a
        # phase=review dispatch with no marker anywhere RAISES (was: silent
        # None). The session-log absence behavior (no FS crash) is preserved;
        # only the no-marker outcome changed.
        data = _mirror_outbox_body(
            result='Just chat output, no marker.',
            claude_session_id='sess-does-not-exist',
        )
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)

    def test_fallback_review_phase_raises_when_no_session_id(self):
        # fix-mirror-verdict-marker-gate-001: missing claude_session_id — the
        # classifier still must not probe the filesystem, and a phase=review
        # dispatch with no marker RAISES (was: silent None).
        data = _mirror_outbox_body(
            result='Just chat output, no marker.',
            claude_session_id=None,
        )
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)

    def test_happy_path_consults_session_log_first(self):
        # chain-discipline-marker-parser-and-regression-check-001 (2026-05-25):
        # precedence is inverted from the original PR #80 design. The session
        # log is now the AUTHORITATIVE source; the outbox `result` is only a
        # fallback. This test pins the new contract: the classifier MUST call
        # the recovery helper on every classification, not just when result
        # parsing returns None. Previously this test was named
        # `test_happy_path_does_not_consult_session_log` and asserted the
        # opposite — it was updated in the same PR that inverted the
        # precedence.
        session_id = 'sess-marker-in-both'
        self._write_session_log(session_id, [
            _mirror_pass_marker(summary='Coverage clean.'),
        ])
        data = _mirror_outbox_body(
            _mirror_pass_marker(summary='Result-text copy.'),
            claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_pass')
        # When both paths produce the same marker, session-log-first means
        # the session-log copy wins — assert via the summary text.
        self.assertEqual(
            decision['intent_kwargs']['summary'], 'Coverage clean.',
        )

    def test_session_log_scan_runs_before_result_text_parse(self):
        # Always-scan-latest-wins regression: even when result_text would
        # parse cleanly to a marker, the session log scan runs first. Mirror
        # said REVIEW_REVISION in turn N, then her final turn was an
        # innocuous REVIEW_PASS-shaped text. The notifier picks the latest
        # valid marker across all turns — and the session log carries the
        # full sequence, so REVIEW_PASS in the final turn of the log wins,
        # which is the same as result_text would give. We pin the
        # ordering: even if the log scan returned a DIFFERENT marker than
        # result_text, the log scan's answer is authoritative.
        session_id = 'sess-different-from-result'
        self._write_session_log(session_id, [
            _mirror_revision_marker(confidence='high'),
        ])
        data = _mirror_outbox_body(
            # result_text would parse as REVIEW_PASS, but the session log
            # carries REVIEW_REVISION and should win.
            _mirror_pass_marker(summary='Stale result-text.'),
            claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_revision')

    def test_pr_101_shape_post_marker_chatter_does_not_mask_pass(self):
        # PR #101 reproduction. Mirror emitted REVIEW_PASS, then her session
        # was held open by a self-matching pgrep poll loop, and when
        # agent_runner woke her after the kill she emitted "Acknowledged —
        # moot now" as the new final turn. Outbox `result` captured only
        # the new final turn. With the old final-turn-first precedence,
        # REVIEW_PASS was missed and auto-merge never fired. With the new
        # always-scan-first precedence, REVIEW_PASS from the earlier
        # turn wins.
        session_id = 'sess-pr-101-shape'
        self._write_session_log(session_id, [
            'Reviewed PR diff. Coverage clean. All ACs met.',
            _mirror_pass_marker(summary='AC coverage clean. Approving.'),
            'Acknowledged — moot now.',
        ])
        data = _mirror_outbox_body(
            result='Acknowledged — moot now.',
            claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertIsNotNone(decision)
        self.assertEqual(decision['marker_type'], 'review_pass')
        self.assertEqual(decision['intent'], 'review-pass')
        self.assertIn(
            'AC coverage clean', decision['intent_kwargs']['summary'],
        )

    def test_session_log_with_no_markers_falls_back_to_result_text(self):
        # When the session log exists but carries no parseable marker in any
        # turn, the classifier MUST fall back to result_text parsing — this
        # preserves the malformed-marker dead-letter path (a broken marker
        # in result_text still raises and goes through the marker-error
        # cascade) and matches the documented fallback semantics.
        session_id = 'sess-no-markers-anywhere'
        self._write_session_log(session_id, [
            'Reading the diff.',
            'Still reading.',
            'Done reading; about to decide.',
        ])
        data = _mirror_outbox_body(
            _mirror_pass_marker(summary='Decided in result text.'),
            claude_session_id=session_id,
        )
        decision = on._classify_mirror_marker(data)
        self.assertEqual(decision['marker_type'], 'review_pass')
        self.assertEqual(
            decision['intent_kwargs']['summary'], 'Decided in result text.',
        )


class ClassifyMirrorProseVerdictSynthesisTest(unittest.TestCase):
    """mirror-prose-verdict-fallback-001 (2026-06-17).

    A phase=review session whose ONLY verdict signal is an unambiguous prose
    PASS (`**Verdict: PASS.**`, no canonical marker) should synthesize a
    REVIEW_PASS and route down the existing auto-merge path — skipping the
    ~$1/~7min marker-error retry. Strict + irreversibility-guarded: PASS-only,
    no-contradiction gate over the WHOLE session, envelope pr_url required.
    Everything that is NOT an unambiguous prose PASS still raises (retry).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._projects_root = Path(self._tmp.name)
        self._patcher = mock.patch.object(
            on, 'CLAUDE_PROJECTS_ROOT', self._projects_root,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _write_session_log(self, session_id, assistant_turns):
        project_dir = self._projects_root / '-fake-project'
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f'{session_id}.jsonl'
        with path.open('w') as fh:
            for text in assistant_turns:
                fh.write(json.dumps({
                    'type': 'assistant',
                    'message': {
                        'role': 'assistant',
                        'content': [{'type': 'text', 'text': text}],
                    },
                }) + '\n')
        return path

    def test_prose_pass_only_synthesizes_review_pass(self):
        # SUCCESS CRITERION 1: only signal is a prose `Verdict: PASS` →
        # classifies review_pass and routes to auto-merge, NO retry. Here the
        # session log is absent (session_id unresolved), so the fallback
        # scans result_text.
        data = _mirror_outbox_body(
            result=(
                'Reviewed the PR diff. Coverage clean, all ACs met.\n\n'
                '**Verdict: PASS.**'
            ),
            claude_session_id='sess-prose-pass-only',
            pr_url=PR_URL_FIXTURE,
        )
        decision = on._classify_mirror_marker(data)
        self.assertIsNotNone(decision)
        self.assertEqual(decision['marker_type'], 'review_pass')
        self.assertEqual(decision['intent'], 'review-pass')
        self.assertFalse(decision['auto_promoted'])
        # Synthesized payload anchors to the ENVELOPE pr_url — so the
        # merge-boundary mismatch gate merges exactly the PR Mirror reviewed.
        self.assertEqual(decision['intent_kwargs']['pr_url'], PR_URL_FIXTURE)
        self.assertEqual(decision['payload']['pr_url'], PR_URL_FIXTURE)
        self.assertEqual(decision['payload']['task_id'], data['task_id'])
        self.assertIn('prose', decision['payload']['summary'].lower())

    def test_prose_pass_synthesis_emits_info_log_token(self):
        # SUCCESS CRITERION (logging): synthesis emits ONE log line with the
        # stable greppable token MIRROR_PROSE_VERDICT_SYNTHESIZED at INFO (no
        # WARN/ERROR on the happy path) so Pulse's G-rule detector can track
        # prose-verdict frequency without it reading as an error.
        data = _mirror_outbox_body(
            result='Coverage clean.\n\n**Verdict: PASS.**',
            claude_session_id='sess-prose-pass-log',
            pr_url=PR_URL_FIXTURE,
        )
        with mock.patch.object(on, 'log') as mock_log:
            on._classify_mirror_marker(data)
        synth_calls = [
            c for c in mock_log.call_args_list
            if 'MIRROR_PROSE_VERDICT_SYNTHESIZED' in c.args[0]
        ]
        self.assertEqual(len(synth_calls), 1)
        # INFO-level: the call must not pass an elevated level positionally.
        level = (
            synth_calls[0].args[1] if len(synth_calls[0].args) > 1
            else synth_calls[0].kwargs.get('level', 'INFO')
        )
        self.assertEqual(level, 'INFO')

    def test_prose_pass_with_contradicting_verdict_raises(self):
        # SUCCESS CRITERION 2: a prose PASS co-occurring with ANY other prose
        # verdict declaration → still raises (no synthesis of an ambiguous
        # PASS that routes to irreversible auto-merge).
        data = _mirror_outbox_body(
            result=(
                'On reflection:\n\nVerdict: REVISION\n\n'
                'Actually re-reading the diff —\n\nVerdict: PASS'
            ),
            claude_session_id='sess-prose-conflict',
            pr_url=PR_URL_FIXTURE,
        )
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)

    def test_cross_turn_contradiction_scans_full_session(self):
        # The no-contradiction gate must see the WHOLE session, not just the
        # final turn: an earlier-turn prose REVISION must veto a final-turn
        # prose PASS. result_text carries ONLY the PASS, so this passes only
        # if the fallback scans the full session log (not result_text alone).
        session_id = 'sess-prose-cross-turn'
        self._write_session_log(session_id, [
            'Reviewing the diff.',
            'Verdict: REVISION',
            'Verdict: PASS',
        ])
        data = _mirror_outbox_body(
            result='Verdict: PASS',
            claude_session_id=session_id,
            pr_url=PR_URL_FIXTURE,
        )
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)

    def test_loose_keyword_mention_still_raises(self):
        # SUCCESS CRITERION 3: a loose mid-sentence keyword mention with no
        # anchored verdict declaration → still raises. The strict line-anchored
        # regex never matches a token buried in a sentence.
        data = _mirror_outbox_body(
            result=(
                'I considered REVIEW_ESCALATE but the diff is clean, so no '
                'escalation is warranted.'
            ),
            claude_session_id='sess-loose-mention',
            pr_url=PR_URL_FIXTURE,
        )
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)

    def test_prose_non_pass_verdict_still_raises(self):
        # SUCCESS CRITERION 4: a prose NON-PASS verdict (`Verdict: REVISION`)
        # → still raises. We never fabricate findings/severity/confidence from
        # prose; the retry yields a real structured marker.
        data = _mirror_outbox_body(
            result='Found a blocking issue.\n\n**Verdict: REVISION.**',
            claude_session_id='sess-prose-revision',
            pr_url=PR_URL_FIXTURE,
        )
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)

    def test_prose_pass_without_envelope_pr_url_still_raises(self):
        # SUCCESS CRITERION 5: an absent/empty envelope pr_url with a prose
        # PASS → still raises. With no PR to merge against there is nothing to
        # synthesize a faithful REVIEW_PASS for.
        data = _mirror_outbox_body(
            result='Coverage clean.\n\n**Verdict: PASS.**',
            claude_session_id='sess-prose-no-prurl',
        )
        data.pop('pr_url', None)
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
            on._classify_mirror_marker(data)
        data['pr_url'] = ''
        with self.assertRaises(on.mrh.MalformedMirrorMarker):
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
        _MIRROR_STATUS_CALL_LOG.clear()
        def _default_auto_merge(pr_url, task_id):
            self._auto_merge_calls.append((pr_url, task_id))
            _MIRROR_STATUS_CALL_LOG.append(('merge', pr_url))
            return {
                'merge_outcome': 'merged',
                'merge_reason': 'squash-merged + branch deleted (test override)',
                'pr_number': 42,
                'repo_coords': 'test-owner/test-repo',
            }
        self._original_auto_merge_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = _default_auto_merge
        # D3.5 5d-prime — bypass the serializer gates in this test class.
        # The gates' `gh pr view --json mergeable` and `gh pr list` calls
        # aren't mocked here; the bypass preserves the D3.5 5d contract
        # (merge-outcome rendering via _AUTO_MERGE_FN_OVERRIDE).
        # Serializer-specific tests in test_auto_merge_serializer.py mock
        # subprocess.run end-to-end and exercise the gates directly.
        self._original_skip_serializer = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = True
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
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._original_skip_serializer
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
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['source'], 'mirror-result')
        self.assertEqual(data['intent'], 'review-pass')
        self.assertIn(PR_URL_FIXTURE, data['prompt'])
        self.assertIn('APPROVED', data['prompt'])

    def test_review_pass_posts_success_status_before_merge(self):
        # build-mirror-review-status: a PASS posts state=success and does so
        # BEFORE auto-merge fires, so the merge satisfies the required check.
        body = _mirror_outbox_body(_mirror_pass_marker())
        f = self._write_mirror_outbox('real-rev.json', body)
        on.process_outbox(f)
        kinds = [e[0] for e in _MIRROR_STATUS_CALL_LOG]
        self.assertIn('status', kinds)
        self.assertIn('merge', kinds)
        self.assertLess(
            kinds.index('status'), kinds.index('merge'),
            f'status POST must precede auto-merge; got {_MIRROR_STATUS_CALL_LOG}',
        )
        status_entry = next(e for e in _MIRROR_STATUS_CALL_LOG if e[0] == 'status')
        self.assertEqual(status_entry[1], 'review_pass')
        self.assertEqual(status_entry[2], 'success')
        self.assertEqual(status_entry[3], PR_URL_FIXTURE)

    def test_review_revision_posts_failure_status(self):
        # build-mirror-review-status: REVISION posts state=failure so the PR
        # stays blocked from merging (the #303 hole). No merge fires.
        body = _mirror_outbox_body(_mirror_revision_marker(confidence='high'))
        f = self._write_mirror_outbox('real-rev.json', body)
        on.process_outbox(f)
        status_entries = [e for e in _MIRROR_STATUS_CALL_LOG if e[0] == 'status']
        self.assertEqual(len(status_entries), 1)
        self.assertEqual(status_entries[0][1], 'review_revision')
        self.assertEqual(status_entries[0][2], 'failure')
        self.assertNotIn('merge', [e[0] for e in _MIRROR_STATUS_CALL_LOG])

    def test_review_revision_notifies_beacon_with_finding_count(self):
        # forge_build_session_id present → the genuine with-session revision
        # path: the back-leg review-revision notify fires (and Forge gets an
        # auto-resume dispatch). Without a session this would instead route
        # the S2 no-session notify to Beacon (covered separately in
        # RevisionLoopTest.test_review_revision_missing_forge_session_routes_to_beacon).
        body = _mirror_outbox_body(
            _mirror_revision_marker(confidence='high'),
            forge_build_session_id='forge-build-sess-rev',
            pr_url=PR_URL_FIXTURE,
        )
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['intent'], 'review-revision')
        self.assertIn('1 finding', data['prompt'])

    def test_review_revision_low_confidence_promotes_to_escalate(self):
        body = _mirror_outbox_body(_mirror_revision_marker(confidence='low'))
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        # Verified: low-confidence revision routes as escalate.
        self.assertEqual(data['intent'], 'review-escalate')
        self.assertIn('ESCALATED', data['prompt'])

    def test_review_escalate_notifies_beacon(self):
        body = _mirror_outbox_body(_mirror_escalate_marker())
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['intent'], 'review-escalate')
        self.assertIn('Spec mismatch', data['prompt'])

    def test_review_emergency_halt_notifies_beacon(self):
        body = _mirror_outbox_body(_mirror_emergency_marker())
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        data = self._get_beacon_notify()
        self.assertEqual(data['intent'], 'review-emergency-halt')
        self.assertIn('credentials', data['prompt'])
        # D3.5 5d — REVIEW_EMERGENCY_HALT now TRIPS the halt-file.
        self.assertTrue(on.EMERGENCY_HALT_FLAG.exists())
        envelope = json.loads(on.EMERGENCY_HALT_FLAG.read_text())
        self.assertEqual(envelope['activated_by'], 'mirror-marker')
        self.assertEqual(envelope['task_id'], 'real-rev')
        self.assertIn('credentials', envelope['reason'])

    def test_malformed_marker_dead_letters_to_mirror(self):
        # Missing required field — PASS without `summary`.
        bad_payload = json.dumps({'task_id': 'real-rev', 'pr_url': PR_URL_FIXTURE})
        marker = (
            f'=== REVIEW_PASS ===\n{bad_payload}\n=== END_REVIEW_PASS ==='
        )
        body = _mirror_outbox_body(marker)
        f = self._write_mirror_outbox('real-rev.json', body)
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

    def test_no_marker_chat_mode_falls_through_to_default_routing(self):
        # Mirror's chat-mode outputs (no marker, NOT a phase=review dispatch)
        # should NOT trigger marker routing — they go via the default path
        # (notify Beacon as mirror-result with intent=result-notification).
        # fix-mirror-verdict-marker-gate-001: phase is explicitly cleared so
        # this represents a genuine chat-mode output, not a review dispatch
        # (a phase=review no-marker outbox now kicks back — see below).
        body = _mirror_outbox_body(
            result=('Reviewed but reporting back in chat mode without '
                    'a marker — Larry asked me a question, not a review.'),
            phase=None,
        )
        f = self._write_mirror_outbox('real-chat.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified')
        data = self._get_beacon_notify()
        self.assertEqual(data['source'], 'mirror-result')
        self.assertEqual(data['intent'], 'result-notification')

    def test_review_phase_prose_verdict_kicks_back_to_mirror(self):
        # fix-mirror-verdict-marker-gate-001 — the PR #277 regression at the
        # process_outbox integration level. A phase=review outbox whose result
        # is the exact #277 prose-verdict shape must classify as a marker-error
        # and kick back to Mirror (NOT silently fall through to default
        # routing / skip auto-merge).
        body = _mirror_outbox_body(
            result=(
                '**Verdict: PASS.** Verification summary: all ACs met, '
                'tests green. The REVIEW_PASS marker is emitted above.'
            ),
        )
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        # No notify to Beacon (it did not route as a result).
        self.assertEqual(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json')), [],
        )
        # No auto-merge attempted — the fix catches the prose verdict BEFORE
        # it could reach the merge path.
        self.assertEqual(self._auto_merge_calls, [])
        # Kickback notify lands in Mirror's inbox so she re-emits a canonical
        # marker.
        mirror_notifies = list(
            (on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json')
        )
        self.assertEqual(len(mirror_notifies), 1)
        data = json.loads(mirror_notifies[0].read_text())
        self.assertEqual(data['intent'], 'marker-error')
        self.assertEqual(data['marker_error_count'], 1)
        self.assertIn('canonical', data['prompt'].lower())

    def test_review_phase_prose_verdict_retry_exhaustion_dead_letters(self):
        # fix-mirror-verdict-marker-gate-001 — the loud-on-exhaust contract.
        # After MAX_MARKER_ERROR_RETRIES consecutive prose verdicts, the
        # dispatch dead-letters to Beacon AND DMs Larry (never silently
        # dropped). Simulate the final strike by seeding marker_error_count
        # at the cap so the next failure exceeds it.
        body = _mirror_outbox_body(
            result='**Verdict: PASS.** Approving in prose, no marker.',
            source='outbox-notifier',
            original_source='beacon',
            marker_error_count=on.MAX_MARKER_ERROR_RETRIES,
            reply_chat_id=12345,
        )
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        # No further kickback to Mirror — the cascade is exhausted.
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json')), [],
        )
        # Dead-letter lands in Beacon's inbox.
        beacon_dead_letters = list(
            (on.INBOXES_ROOT / 'beacon').glob('dead-letter-marker-*.json')
        )
        self.assertEqual(len(beacon_dead_letters), 1)
        dl = json.loads(beacon_dead_letters[0].read_text())
        self.assertEqual(dl['intent'], 'dead-letter')
        # Larry is escalated via the alerts file (the _maybe_dm_larry path
        # fires because reply_chat_id is present and dead-letter is terminal).
        import larry_alerts as la
        self.assertTrue(la.ALERTS_FILE.exists())
        self.assertIn('real-rev', la.ALERTS_FILE.read_text())


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
            agent='forge', source='beacon', task_id='real-bad-pf',
            phase='preflight',
            result=('I read the spec and started editing the file '
                    'directly — committed the change to a branch named '
                    'fix/typo. PR opened: https://github.com/Larry-Yatch/'
                    'ourliberty-agent-core/pull/99'),
        )
        f = self._write_outbox('forge', 'real-bad-pf.json', outbox)
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
            agent='forge', source='beacon', task_id='prod-build-ok',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/prod-build-ok',
            result=('PR opened: https://github.com/Larry-Yatch/'
                    'ourliberty-agent-core/pull/100\n\n'
                    'Fixed the typo per spec.'),
        )
        f = self._write_outbox('forge', 'prod-build-ok.json', outbox)
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
            '{"task_id": "real-pf-ok", "preflight_summary": "Will fix typo."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='real-pf-ok',
            phase='preflight', result=marker,
        )
        f = self._write_outbox('forge', 'real-pf-ok.json', outbox)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')

    def test_no_phase_field_does_not_fire_gate(self):
        # Legacy Forge outboxes (pre-D3 dispatches still in flight) may
        # have no phase field. The gate must not fire on those — only
        # explicit phase=preflight + no marker triggers it.
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='real-legacy',
            result='Some plain text response from a legacy dispatch.',
        )
        outbox.pop('phase', None)
        f = self._write_outbox('forge', 'real-legacy.json', outbox)
        result = on.process_outbox(f)
        # No marker, no phase → default routing path (Beacon notify).
        self.assertEqual(result, 'notified')


class MirrorReviewDispatchTest(unittest.TestCase):
    """Forge build-phase outbox carrying 'PR opened:' triggers a review-
    request task to Mirror's inbox (parallel to _dispatch_build_phase)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        # _dispatch_mirror_review records the PR head; stub the gh lookup so
        # these dispatch tests stay hermetic (no live `gh pr view`). None keeps
        # the round-0 dedup on its existence-only path, exactly as before this
        # field existed.
        self._orig_gh_head = on._gh_pr_head_sha
        on._gh_pr_head_sha = lambda *a, **k: None
        self.addCleanup(setattr, on, '_gh_pr_head_sha', self._orig_gh_head)
        # The merged/closed dispatch-time guard probes PR open-state; stub it
        # OPEN (a freshly-opened PR) so these dispatch tests stay hermetic and
        # don't shell out to real `gh` against the live pull/77.
        self._orig_gh_is_open = on._gh_pr_is_open
        on._gh_pr_is_open = lambda *a, **k: True
        self.addCleanup(setattr, on, '_gh_pr_is_open', self._orig_gh_is_open)
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
            agent='forge', source='beacon', task_id='prod-built',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/prod-built',
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
        f = self._write_outbox('forge', 'prod-built.json', body)
        on.process_outbox(f)
        # review-request task in Mirror's inbox
        review_tasks = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(review_tasks), 1)
        data = json.loads(review_tasks[0].read_text())
        self.assertEqual(data['task_id'], 'prod-built')
        self.assertEqual(data['source'], 'beacon')
        self.assertEqual(data['phase'], 'review')
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/prod-built')
        self.assertIn('pull/77', data['pr_url'])
        self.assertEqual(data['revision_count'], 0)
        # max_revisions sourced from mirror_review_handler default
        self.assertEqual(data['max_revisions'], on.mrh.DEFAULT_MAX_REVISIONS)
        self.assertEqual(data['dispatched_by'], 'outbox-notifier')

    def test_pr_opened_also_notifies_beacon(self):
        # The review dispatch is ADDITIVE — Beacon still gets her notify
        # via the default routing path so she can journal "PR opened."
        body = self._build_outbox()
        f = self._write_outbox('forge', 'prod-built.json', body)
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
        f = self._write_outbox('forge', 'real-failed-build.json', body)
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
        f = self._write_outbox('forge', 'real-no-repo.json', body)
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
        f = self._write_outbox('forge', 'real-fastpath.json', body)
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
        f = self._write_outbox('forge', 'prod-built.json', body)
        on.process_outbox(f)
        # Simulate re-processing: re-write the outbox + run again. (In
        # production this happens when the notifier crashes after the
        # dispatch write but before the archive move.)
        f2 = self._write_outbox('forge', 'prod-built.json', body)
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
            'task_id': 'real-rev', 'pr_url': PR_URL_FIXTURE,
            'summary': 'AC coverage clean across the board.',
        }
        body = on._marker_output_for_prompt({}, self._decision(
            'review_pass', payload,
        ))
        self.assertIn('coverage clean', body)
        self.assertNotEqual(body, '(no reason)')

    def test_review_revision_body_has_finding_summary(self):
        payload = {
            'task_id': 'real-rev', 'pr_url': PR_URL_FIXTURE,
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
            'task_id': 'real-rev', 'pr_url': PR_URL_FIXTURE,
            'reason': 'Implemented X, spec said Y.',
            'severity': 'high', 'confidence': 'high',
        }
        body = on._marker_output_for_prompt({}, self._decision(
            'review_escalate', payload,
        ))
        self.assertIn('Implemented X', body)

    def test_review_emergency_halt_body_has_reason_and_evidence(self):
        payload = {
            'task_id': 'real-rev', 'pr_url': PR_URL_FIXTURE,
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
        (invalid_dir / 'review-real-stuck.json').write_text('{}')

        data = {
            'task_id': 'real-stuck',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-stuck',
        }
        on._dispatch_mirror_review(data, 'https://github.com/x/y/pull/1')
        # Should NOT have written a fresh review-request to the live inbox
        live = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(live, [])

    def test_build_phase_skips_when_in_invalid(self):
        invalid_dir = on.INBOXES_ROOT / 'forge' / '.invalid'
        invalid_dir.mkdir(parents=True, exist_ok=True)
        (invalid_dir / 'build-real-stuck.json').write_text('{}')

        data = {
            'task_id': 'real-stuck',
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
            'task_id': 'real-meta',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-meta',
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
            'task_id': 'real-bare',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-bare',
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
            'task_id': 'real-chat', 'reply_chat_id': 7998341473,
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
            'task_id': 'real-pass', 'reply_chat_id': 7998341473,
            'agent': 'mirror',
        }
        decision = self._decision('review-pass', 'review_pass', payload={
            'task_id': 'real-pass',
            'pr_url': 'https://github.com/x/y/pull/1',
            'summary': 'All clean.',
        })
        on._maybe_dm_larry(data, decision)
        notifs = self._read_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['intent'], 'review-pass')
        self.assertEqual(notifs[0]['chat_id'], 7998341473)
        self.assertEqual(notifs[0]['task_id'], 'real-pass')
        self.assertIn('Mirror approved', notifs[0]['message'])
        self.assertIn('pull/1', notifs[0]['message'])
        self.assertIn('All clean', notifs[0]['message'])

    def test_review_pass_release_already_merged_suppresses_dm(self):
        # fix-auto-merge-already-merged-skip: the released PR was already
        # merged/closed, so Larry already got the `merged` closing DM when it
        # actually merged. A second DM on this skip path would be duplicate
        # noise — suppress it (same review-pass suppression family as
        # deferred_unknown / held_conflict / held_stale_regression).
        data = {
            'task_id': 'real-already-merged', 'reply_chat_id': 7998341473,
            'agent': 'mirror',
        }
        decision = self._decision('review-pass', 'review_pass', payload={
            'task_id': 'real-already-merged',
            'pr_url': 'https://github.com/x/y/pull/9',
            'summary': 'All clean.',
        })
        decision['merge_outcome'] = 'release_already_merged'
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_review_revision_does_not_dm_in_5b(self):
        # D3.5 5b: REVIEW_REVISION is mid-chain (revision auto-dispatched to
        # Forge). Larry only gets DM on terminal intents — escalate (incl.
        # budget-exhausted downgrade), pass, emergency-halt, reject,
        # clarification-exhausted. Confirming the 5a behavior was changed.
        data = {'task_id': 'real-rev', 'reply_chat_id': 7998341473, 'agent': 'mirror'}
        decision = self._decision('review-revision', 'review_revision', payload={
            'task_id': 'real-rev',
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
        data = {'task_id': 'real-esc', 'reply_chat_id': 7998341473, 'agent': 'mirror'}
        decision = self._decision('review-escalate', 'review_escalate', payload={
            'task_id': 'real-esc',
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
        data = {'task_id': 'real-halt', 'reply_chat_id': 7998341473, 'agent': 'mirror'}
        decision = self._decision('review-emergency-halt', 'review_emergency_halt', payload={
            'task_id': 'real-halt',
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
        data = {'task_id': 'real-mid', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('ack-proceed', 'proceed')
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_clarify_intent_does_not_dm(self):
        # Clarifications are mid-chain (Beacon answers Forge); no Larry DM.
        data = {'task_id': 'real-clar', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('clarify', 'clarify_request')
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_result_notification_does_not_dm(self):
        # Generic result-notification is the mid-chain catch-all; no DM.
        data = {'task_id': 'real-result', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('result-notification')
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_missing_reply_chat_id_does_not_dm(self):
        # Autonomous Pulse-initiated runs have no originating chat.
        data = {'task_id': 'real-auto', 'agent': 'mirror'}  # no reply_chat_id
        decision = self._decision('review-pass', 'review_pass', payload={
            'task_id': 'real-auto', 'pr_url': 'x', 'summary': 'y',
        })
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_non_int_reply_chat_id_does_not_dm(self):
        # Defensive: a corrupted reply_chat_id (string, list, whatever)
        # gets logged and skipped — not propagated to a DM.
        data = {
            'task_id': 'real-bad-chat', 'reply_chat_id': 'not-a-number',
            'agent': 'mirror',
        }
        decision = self._decision('review-pass', 'review_pass', payload={
            'task_id': 'real-bad-chat', 'pr_url': 'x', 'summary': 'y',
        })
        on._maybe_dm_larry(data, decision)
        self.assertEqual(self._read_notifications(), [])

    def test_reject_intent_dms(self):
        # Forge rejected the spec at preflight — terminal for Larry.
        data = {'task_id': 'real-rej', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision('reject', 'reject', payload={
            'task_id': 'real-rej', 'reason': 'Required file missing.',
        }, intent_kwargs={'reason': 'Required file missing.'})
        on._maybe_dm_larry(data, decision)
        notifs = self._read_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertIn('REJECTED', notifs[0]['message'])
        self.assertIn('Required file missing', notifs[0]['message'])

    def test_clarification_exhausted_dms(self):
        # Forge ran out of clarifications — terminal for Larry.
        data = {'task_id': 'real-cx', 'reply_chat_id': 7998341473, 'agent': 'forge'}
        decision = self._decision(
            'clarification-exhausted', 'clarify_request',
            payload={'task_id': 'real-cx', 'question': 'Final question?'},
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
        # forge-cold-start-revision (S2): sandbox the no-session ledger to this
        # test root and stub the gh-backed PR-body fetch so the cold-start path
        # is deterministic and never shells out.
        self._nsl_ledger_orig = on.no_session_ledger.LEDGER_FILE
        on.no_session_ledger.LEDGER_FILE = (
            self._root / 'state' / 'no-session-revision-ledger.json'
        )
        self._fetch_pr_body_orig = on._fetch_pr_body
        on._fetch_pr_body = lambda pr_url: '## Why\nFixture PR intent body.'
        on.ensure_dirs()

    def tearDown(self):
        on.no_session_ledger.LEDGER_FILE = self._nsl_ledger_orig
        on._fetch_pr_body = self._fetch_pr_body_orig
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
            task_id='prod-loop', severity='medium', confidence='high',
        )
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='prod-loop', phase='review',
            target_repo='ourliberty-agent-core',
            branch='forge/prod-loop',
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
            agent='forge', source='beacon', task_id='prod-loop',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/prod-loop',
            claude_session_id='forge-build-sess-abc',
            result=(
                f'Revision {round_num} applied: added input validation on '
                f'foo.py L12-L15 per Mirror finding.\n\n'
                f'Tests pass; pushed to forge/prod-loop.'
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
        f = self._write_outbox('mirror', 'prod-loop.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        # Revision task lands in Forge's inbox keyed on round number
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json')
        )
        self.assertEqual(len(revisions), 1)
        revision = json.loads(revisions[0].read_text())
        self.assertEqual(revision['task_id'], 'prod-loop')
        self.assertEqual(revision['source'], 'beacon')
        self.assertEqual(revision['phase'], 'revision')
        self.assertEqual(revision['session_id'], 'forge-build-sess-abc')
        self.assertEqual(revision['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(revision['branch'], 'forge/prod-loop')
        self.assertEqual(revision['revision_count'], 1)
        self.assertEqual(revision['max_revisions'], 3)
        self.assertEqual(revision['dispatched_by'], 'outbox-notifier')
        # Findings serialized into the prompt
        self.assertIn('Mirror\'s findings on this PR', revision['prompt'])
        self.assertIn('medium', revision['prompt'])

    def test_review_revision_low_confidence_does_not_dispatch(self):
        # Auto-promote (5a behavior) blocks the revision dispatch — Mirror's
        # uncertainty means the auto-fix loop shouldn't run; escalate instead.
        marker = _mirror_revision_marker(task_id='real-low', confidence='low')
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='real-low',
            phase='review', target_repo='ourliberty-agent-core',
            branch='forge/real-low',
            result=f'Maybe?\n\n{marker}',
        )
        body['forge_build_session_id'] = 'forge-sess-xyz'
        body['pr_url'] = 'https://github.com/x/y/pull/78'
        f = self._write_outbox('mirror', 'real-low.json', body)
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
        f = self._write_outbox('mirror', 'prod-loop-exhausted.json', body)
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

    def test_review_revision_missing_forge_session_cold_starts_forge(self):
        # forge-cold-start-revision (S2): a REVISION with no
        # forge_build_session_id (a claude/ PR or heal-rebuilt envelope — the
        # #645 / #653 / PR #412 class) dispatches a FRESH, fully-briefed Forge
        # revision and opens a durable obligation — instead of the old
        # LLM-mediated Beacon notify that could silently dead-end.
        body = self._mirror_revision_outbox()
        body.pop('forge_build_session_id', None)
        f = self._write_outbox('mirror', 'real-no-session.json', body)
        on.process_outbox(f)

        # A fresh Forge revision task was written (round 1).
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json')
        )
        self.assertEqual(len(revisions), 1)
        task = json.loads(revisions[0].read_text())
        # Fresh: no --resume session threaded (truthy guard drops both).
        self.assertNotIn('session_id', task)
        self.assertNotIn('forge_build_session_id', task)
        # Cold-start brief: provenance framing + fetched PR intent + PR + findings.
        self.assertIn('NOT your build', task['prompt'])
        self.assertIn('Fixture PR intent body', task['prompt'])
        self.assertIn('https://github.com/x/y/pull/77', task['prompt'])
        self.assertIn('Missing validation', task['prompt'])

        # No legacy Beacon no-session notify.
        notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob(
                'notify-no-session-revision-*.json'
            )
        )
        self.assertEqual(notifies, [])

        # A durable obligation was opened for the backstop.
        ob = on.no_session_ledger.get_obligation('prod-loop')
        self.assertIsNotNone(ob)
        self.assertEqual(ob['status'], on.no_session_ledger.OPEN)
        self.assertEqual(ob['pr_url'], 'https://github.com/x/y/pull/77')

        # Mirror focus: no warning-severity Larry DM.
        self.assertFalse(self._la.ALERTS_FILE.exists())

    def test_no_session_cold_start_idempotent_on_reprocess(self):
        # Re-processing the same Mirror outbox (notifier crash between write
        # and archive) does NOT write a duplicate Forge revision — the revision
        # filename is keyed on task_id + round.
        body = self._mirror_revision_outbox()
        body.pop('forge_build_session_id', None)
        f = self._write_outbox('mirror', 'real-no-session.json', body)
        on.process_outbox(f)
        f2 = self._write_outbox('mirror', 'real-no-session.json', body)
        on.process_outbox(f2)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json')
        )
        self.assertEqual(len(revisions), 1)

    def test_no_session_cold_start_reopens_obligation_on_idempotent_reprocess(self):
        # Regression (review HIGH): the idempotency early-return (revision file
        # already exists) must STILL open the ledger obligation — a dispatch that
        # crashed between the write and the ledger open would otherwise leave the
        # backstop blind. Simulate by resolving the obligation, then reprocessing
        # (the file exists → early-return) and asserting it re-opened.
        nsl = on.no_session_ledger
        body = self._mirror_revision_outbox()
        body.pop('forge_build_session_id', None)
        on.process_outbox(self._write_outbox('mirror', 'real-no-session.json', body))
        self.assertEqual(nsl.get_obligation('prod-loop')['status'], nsl.OPEN)
        nsl.resolve_obligation('prod-loop', resolution='review-pass')
        # Reprocess: revision file already exists → idempotency early-return.
        on.process_outbox(self._write_outbox('mirror', 'real-no-session.json', body))
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json'))),
            1,  # still idempotent — no duplicate revision
        )
        self.assertEqual(  # but the obligation was re-opened by the early-return
            nsl.get_obligation('prod-loop')['status'], nsl.OPEN,
        )

    def test_no_session_revision_pass_branch_unchanged(self):
        # Regression guard: a normal REVISION WITH a forge_build_session_id
        # still dispatches to Forge and does NOT route to Beacon's no-session
        # path. The S2 change only touches the no-session fallthrough.
        body = self._mirror_revision_outbox()
        f = self._write_outbox('mirror', 'prod-loop.json', body)
        on.process_outbox(f)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json')
        )
        self.assertEqual(len(revisions), 1)
        no_session_notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob(
                'notify-no-session-revision-*.json'
            )
        )
        self.assertEqual(no_session_notifies, [])

    def test_revision_dispatch_idempotent_on_reprocess(self):
        body = self._mirror_revision_outbox()
        f = self._write_outbox('mirror', 'prod-loop.json', body)
        on.process_outbox(f)
        # Re-write the same outbox + re-process (simulates notifier crash
        # between dispatch and archive)
        f2 = self._write_outbox('mirror', 'prod-loop.json', body)
        on.process_outbox(f2)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json')
        )
        # Still exactly one revision-task, despite two process_outbox calls
        self.assertEqual(len(revisions), 1)

    # ----- Forge revision outbox → Mirror re-review dispatch -----

    def test_forge_revision_outbox_dispatches_rereview(self):
        body = self._forge_revision_outbox(round_num=1)
        f = self._write_outbox('forge', 'revision-prod-loop-1.json', body)
        on.process_outbox(f)
        # Mirror inbox gets a fresh review-request keyed on round number
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-prod-loop-rev*.json')
        )
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(review['task_id'], 'prod-loop')
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
            agent='forge', source='beacon', task_id='real-bad-rev',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='forge-sess',
            # NO preamble; Forge fast-pathed past the protocol
            result='I applied the fix but forgot to use the required format.',
        )
        f = self._write_outbox('forge', 'revision-real-bad-rev-1.json', body)
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

    def test_round2_missing_preamble_retry_carries_trap_wording(self):
        # The recurring churn (PR #711/#720/#726): on round 2+ Forge resumes a
        # session that already holds her round-1 "Revision 1 applied:" line, so
        # her new preamble lands mid-response and the strict gate bounces it.
        # The marker-error retry must call out the round-2 trap with the
        # CONCRETE round numbers from the envelope so the bounce self-corrects.
        body = _good_outbox(
            agent='forge', source='beacon', task_id='round2-trap',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='forge-sess',
            result=(
                'Thanks — I see Mirror\'s new findings. I applied them.\n'
                'Revision 2 applied: fixed the off-by-one per finding 2.'
            ),
        )
        body['revision_count'] = 2
        f = self._write_outbox('forge', 'revision-round2-trap-2.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        marker_errors = list(
            (on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')
        )
        self.assertEqual(len(marker_errors), 1)
        prompt = json.loads(marker_errors[0].read_text())['prompt']
        # Round-2 trap callout present, with concrete prior/current rounds.
        self.assertIn('TRAP', prompt)
        self.assertIn('resumed conversation', prompt)
        self.assertIn('Revision 1 applied:', prompt)
        self.assertIn('Revision 2 applied:', prompt)

    def test_rereview_dispatch_idempotent_on_reprocess(self):
        body = self._forge_revision_outbox(round_num=1)
        f = self._write_outbox('forge', 'revision-prod-loop-1.json', body)
        on.process_outbox(f)
        f2 = self._write_outbox('forge', 'revision-prod-loop-1.json', body)
        on.process_outbox(f2)
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-prod-loop-rev*.json')
        )
        self.assertEqual(len(reviews), 1)

    def test_forge_revision_round_2_extracted_correctly(self):
        # Round 2 prefix should parse and feed Mirror's revision_count=2.
        body = self._forge_revision_outbox(round_num=2)
        f = self._write_outbox('forge', 'revision-prod-loop-2.json', body)
        on.process_outbox(f)
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-prod-loop-rev*.json')
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
            agent='forge', source='beacon', task_id='real-thread',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/real-thread',
            claude_session_id='forge-build-thread-sess',
            result=(
                'PR opened: https://github.com/x/y/pull/99\n\n'
                'Done.'
            ),
        )
        f = self._write_outbox('forge', 'real-thread.json', build)
        on.process_outbox(f)
        # Mirror's review-request envelope should now carry the field.
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-real-thread.json'))
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
            agent='forge', source='beacon', task_id='real-blocker',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/real-blocker',
            claude_session_id='forge-blocker-sess',
            result=(
                'Compile error in foo.py — the spec asked me to import '
                'bar which does not exist. Need clarification on the '
                'real module name before I can proceed.'
            ),
        )
        f = self._write_outbox('forge', 'real-blocker.json', body)
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
        f = self._write_outbox('mirror', 'prod-loop-exhausted.json', body)
        on.process_outbox(f)
        # Check larry-alerts.jsonl for the notification
        pending = self._la.read_pending(0)
        notifications = [r for _, r in pending if r.get('kind') == 'notification']
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]['intent'], 'review-escalate')
        self.assertIn('budget', notifications[0]['message'].lower())

    # ----- mirror-review-visibility (Contracts B+C+D) end-to-end -----

    def _sandbox_no_session_surfaces(self):
        """Route the for-Larry feed + approval store to this test root so the
        no-session router's artifacts are observable and never touch prod.
        Restored via addCleanup. emit_event is already kill-switched module-wide
        (OURLIBERTY_DISABLE_LIVE_EMIT=1)."""
        feed = self._root / 'blackboard' / 'for-larry-escalations.json'
        env_orig = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(feed)

        def _restore_env():
            if env_orig is None:
                os.environ.pop('OURLIBERTY_FOR_LARRY_FEED_FILE', None)
            else:
                os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = env_orig
        self.addCleanup(_restore_env)

        ah_orig = on.approval.PENDING_APPROVALS_PATH
        on.approval.PENDING_APPROVALS_PATH = (
            self._root / 'state' / 'beacon-pending-approvals.json'
        )
        self.addCleanup(
            setattr, on.approval, 'PENDING_APPROVALS_PATH', ah_orig,
        )

    def _offchain_doc_revision_outbox(self, **overrides):
        """The #653 shape: a doc PR authored off-chain that Mirror wants
        revised — NO forge_build_session_id (no live build), NO target_repo
        (no chain envelope to re-dispatch). The auto-fix loop can't proceed →
        action-needed."""
        marker = _mirror_revision_marker(
            task_id='offchain-doc', severity='medium', confidence='high',
        )
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='offchain-doc',
            phase='review',
            result=f'Doc PR needs a fix.\n\n{marker}',
        )
        body['pr_url'] = 'https://github.com/x/y/pull/653'
        body['head_sha'] = 'deadbeefcafe'
        body.pop('target_repo', None)
        body.pop('forge_build_session_id', None)
        body.update(overrides)
        return body

    def test_offchain_revision_writes_for_larry_record_no_alert(self):
        # The real gate (spec §10): replay #653. An off-chain doc PR Mirror
        # wants revised, with no session + no target_repo, can't self-heal →
        # a durable for-Larry record lands on the Waiting-on-You feed, and NO
        # standalone larry_alert (Contract C: action bucket never alerts).
        self._sandbox_no_session_surfaces()
        body = self._offchain_doc_revision_outbox()
        f = self._write_outbox('mirror', 'offchain-doc.json', body)
        on.process_outbox(f)

        records = on.for_larry_escalations.list_open()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec['id'], 'mirror-review:offchain-doc')
        self.assertEqual(rec['source'], 'mirror-review')
        self.assertTrue(rec['for_larry'])
        self.assertFalse(rec['resolved'])
        self.assertEqual(
            rec['dedup_identity'],
            'https://github.com/x/y/pull/653@deadbeefcafe',
        )
        # Contract C: action bucket emits NO standalone larry_alert.
        notifications = [
            r for _, r in self._la.read_pending(0)
            if r.get('kind') == 'notification'
        ]
        self.assertEqual(notifications, [])
        # No approval (that's the decision bucket, not this one).
        self.assertFalse(on.approval.PENDING_APPROVALS_PATH.exists())

    def test_offchain_revision_idempotent_same_head(self):
        # Contract D: re-processing the same PR+head SHA writes exactly one
        # record (one artifact per escalation).
        self._sandbox_no_session_surfaces()
        for i in range(2):
            body = self._offchain_doc_revision_outbox()
            f = self._write_outbox('mirror', f'offchain-doc-{i}.json', body)
            on.process_outbox(f)
        self.assertEqual(len(on.for_larry_escalations.list_open()), 1)

    def test_offchain_record_clears_on_terminal_pass(self):
        # Decision d (self-clearing): once the PR reaches a terminal PASS the
        # trigger is gone, so the for-Larry record retracts.
        self._sandbox_no_session_surfaces()
        body = self._offchain_doc_revision_outbox()
        f = self._write_outbox('mirror', 'offchain-doc.json', body)
        on.process_outbox(f)
        self.assertEqual(len(on.for_larry_escalations.list_open()), 1)

        pass_marker = _mirror_pass_marker(task_id='offchain-doc')
        pass_body = _good_outbox(
            agent='mirror', source='beacon', task_id='offchain-doc',
            phase='review',
            result=f'Looks good now.\n\n{pass_marker}',
        )
        pass_body['pr_url'] = 'https://github.com/x/y/pull/653'
        pass_body['head_sha'] = 'deadbeefcafe'
        pf = self._write_outbox('mirror', 'offchain-doc-pass.json', pass_body)
        on.process_outbox(pf)
        self.assertEqual(on.for_larry_escalations.list_open(), [])

    def test_offchain_escalate_emits_binary_approval(self):
        # Decision bucket end-to-end: an off-chain REVIEW_ESCALATE surfaces a
        # binary approval_request (Approvals tab), not a for-Larry record.
        self._sandbox_no_session_surfaces()
        marker = _mirror_escalate_marker(task_id='offchain-esc')
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='offchain-esc',
            phase='review',
            result=f'Needs a human call.\n\n{marker}',
        )
        body['pr_url'] = 'https://github.com/x/y/pull/654'
        body['head_sha'] = 'feedface0001'
        f = self._write_outbox('mirror', 'offchain-esc.json', body)
        on.process_outbox(f)

        # No for-Larry record (that's the action bucket).
        self.assertEqual(on.for_larry_escalations.list_open(), [])
        # A binary approval landed, keyed on PR+head SHA.
        self.assertTrue(on.approval.PENDING_APPROVALS_PATH.exists())
        pending = json.loads(on.approval.PENDING_APPROVALS_PATH.read_text())
        rows = pending if isinstance(pending, list) else pending.get('pending', [])
        approval_ids = [r.get('id') for r in rows]
        self.assertIn('mirror-review-offchain-esc-feedface', approval_ids)


class MergedPrReviewRevisionGuardTest(RevisionLoopTest):
    """Result-time merged/closed-PR guard for a Mirror REVIEW_REVISION (the #764
    desktop-merge race). A review QUEUED while the PR was OPEN but RUN after a
    desktop `merge_reviewed_pr.sh` merge must NOT escalate to Larry or dispatch a
    Forge revision — both are moot on a merged branch. Reuses RevisionLoopTest's
    sandbox; the module-level `_gh_pr_is_open` default is None (fail-open), and
    each test pins the GitHub state it asserts against.

    `runTest = None` keeps unittest from re-collecting the parent's test_* methods
    under this subclass — we only want the parent's setUp/tearDown/helpers."""

    # Suppress inherited parent test methods so they don't run twice (once per
    # class). unittest collects any attribute starting with `test`; rebinding
    # each parent test to None on the subclass de-registers it here while leaving
    # RevisionLoopTest itself untouched.
    for _name in list(vars(RevisionLoopTest)):
        if _name.startswith('test'):
            locals()[_name] = None
    del _name

    # ---- (b) Forge-revision dispatch guard: clean revision, session present ----

    def test_merged_pr_skips_forge_revision_dispatch(self):
        # The genuine in-loop revision (high confidence + forge_build_session_id)
        # would normally dispatch a Forge revision. On a MERGED PR it must not.
        body = self._mirror_revision_outbox()
        f = self._write_outbox('mirror', 'prod-loop.json', body)
        with mock.patch.object(on, '_gh_pr_is_open', return_value=False) as g:
            result = on.process_outbox(f)
        g.assert_called()  # the guard queried GitHub before dispatching
        self.assertEqual(result, 'review-revision-already-merged')
        # No Forge revision dispatched, and no Beacon back-leg notify.
        self.assertEqual(
            list((on.INBOXES_ROOT / 'forge').glob('revision-*.json')), [],
        )
        self.assertEqual(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json')), [],
        )

    def test_open_pr_still_dispatches_forge_revision(self):
        # Fail-closed regression guard: an OPEN PR must still dispatch as before.
        body = self._mirror_revision_outbox()
        f = self._write_outbox('mirror', 'prod-loop.json', body)
        with mock.patch.object(on, '_gh_pr_is_open', return_value=True):
            result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json'))),
            1,
        )

    def test_unknown_state_fails_open_and_dispatches(self):
        # gh hiccup → None → proceed (never silently drop a real revision).
        body = self._mirror_revision_outbox()
        f = self._write_outbox('mirror', 'prod-loop.json', body)
        with mock.patch.object(on, '_gh_pr_is_open', return_value=None):
            result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json'))),
            1,
        )

    # ---- (a) Larry-escalation guard: low-confidence revision, no session ----

    def _no_session_low_confidence_outbox(self):
        """The #764 shape: a session-less REVIEW_REVISION with confidence=low
        (auto-promoted), which normally surfaces a binary 'needs your decision'
        approval_request to Larry."""
        marker = _mirror_revision_marker(
            task_id='merged-764', severity='medium', confidence='low',
        )
        body = _good_outbox(
            agent='mirror', source='beacon', task_id='merged-764',
            phase='review', target_repo='ourliberty-agent-core',
            result=f'Maybe a nit?\n\n{marker}',
        )
        body['pr_url'] = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/764'
        body['head_sha'] = 'd31c24c80000'
        body.pop('forge_build_session_id', None)
        return body

    def test_merged_pr_skips_larry_escalation(self):
        self._sandbox_no_session_surfaces()
        f = self._write_outbox(
            'mirror', 'merged-764.json', self._no_session_low_confidence_outbox(),
        )
        with mock.patch.object(on, '_gh_pr_is_open', return_value=False):
            result = on.process_outbox(f)
        self.assertEqual(result, 'review-revision-already-merged')
        # No binary approval_request emitted (the moot "needs your decision").
        self.assertFalse(on.approval.PENDING_APPROVALS_PATH.exists())
        # No for-Larry action record either.
        self.assertEqual(on.for_larry_escalations.list_open(), [])
        # And no Beacon back-leg escalate notify.
        self.assertEqual(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json')), [],
        )

    def test_open_pr_still_escalates_to_larry(self):
        # Fail-closed regression guard: an OPEN session-less low-confidence
        # revision still surfaces the binary approval_request to Larry.
        self._sandbox_no_session_surfaces()
        f = self._write_outbox(
            'mirror', 'merged-764.json', self._no_session_low_confidence_outbox(),
        )
        with mock.patch.object(on, '_gh_pr_is_open', return_value=True):
            on.process_outbox(f)
        self.assertTrue(on.approval.PENDING_APPROVALS_PATH.exists())
        pending = json.loads(on.approval.PENDING_APPROVALS_PATH.read_text())
        rows = pending if isinstance(pending, list) else pending.get('pending', [])
        ids = [r.get('id') for r in rows]
        self.assertIn('mirror-review-merged-764-d31c24c8', ids)


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
            'task_id': 'prod-loop',
            'forge_build_session_id': 'forge-sess-abc',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/prod-loop',
            'pr_url': 'https://github.com/x/y/pull/1',
            'revision_count': 0,
            'max_revisions': 3,
            'reply_chat_id': 7998341473,
        }
        decision = {
            'marker_type': 'review_revision',
            'payload': {
                'task_id': 'prod-loop', 'pr_url': data['pr_url'],
                'findings': [
                    {'file': 'a.py', 'line_range': 'L10', 'severity': 'medium',
                     'description': 'fix this'},
                ],
                'severity': 'medium', 'confidence': 'high',
            },
        }
        on._dispatch_revision_to_forge(data, decision)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-prod-loop-*.json')
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
            agent='forge', source='beacon', task_id='real-no-preamble',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/real-no-preamble',
            claude_session_id='forge-build-sess',
            result='I forgot the required preamble format.',
        )
        body['forge_build_session_id'] = 'forge-build-sess'
        body['revision_count'] = 2
        body['max_revisions'] = 3
        body['pr_url'] = 'https://github.com/x/y/pull/1'
        body['reply_chat_id'] = 7998341473
        f = self._write_outbox('forge', 'revision-real-no-preamble-2.json', body)
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
            agent='forge', source='beacon', task_id='real-zero',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess', result='Revision 0 applied: nope.',
        )
        body['revision_count'] = 1
        f = self._write_outbox('forge', 'revision-real-zero-1.json', body)
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
            agent='forge', source='beacon', task_id='real-drift',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess',
            result='Revision 1 applied: fixed the thing.',
        )
        body['revision_count'] = 2
        f = self._write_outbox('forge', 'revision-real-drift-2.json', body)
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
            agent='forge', source='beacon', task_id='real-inflate',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess',
            result='Revision 99 applied: skipping ahead.',
        )
        body['revision_count'] = 1
        f = self._write_outbox('forge', 'revision-real-inflate-1.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')

    def test_m3_correct_round_passes(self):
        # Sanity: round matches envelope → dispatches the re-review normally.
        body = _good_outbox(
            agent='forge', source='beacon', task_id='prod-ok',
            phase='revision', target_repo='ourliberty-agent-core',
            claude_session_id='sess',
            result='Revision 2 applied: all findings addressed.',
        )
        body['revision_count'] = 2
        body['pr_url'] = 'https://github.com/x/y/pull/1'
        f = self._write_outbox('forge', 'revision-prod-ok-2.json', body)
        on.process_outbox(f)
        reviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-prod-ok-rev*.json')
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
                'task_id': 'real-cfg',
                'target_repo': 'ourliberty-agent-core',
                'branch': 'forge/real-cfg',
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
            'task_id': 'real-both',
            'agent': 'mirror',
            'result': (
                'Found something I am not sure about.\n\n'
                '=== REVIEW_REVISION ===\n'
                + json.dumps({
                    'task_id': 'real-both',
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
            'agent': 'mirror', 'source': 'beacon', 'task_id': 'prod-mirror-retry',
            'phase': 'review',
            'forge_build_session_id': 'forge-build-sess',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/prod-mirror-retry',
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
            'task_id': 'real-findings',
            'forge_build_session_id': 'forge-sess',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-findings',
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
                'task_id': 'real-findings', 'pr_url': data['pr_url'],
                'findings': findings,
                'severity': 'medium', 'confidence': 'high',
            },
        }
        on._dispatch_revision_to_forge(data, decision)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-findings-*.json')
        )
        self.assertEqual(len(revisions), 1)
        task = json.loads(revisions[0].read_text())
        self.assertEqual(task['previous_findings'], findings)

    def _revision_dispatch_inputs(self, *, revision_count):
        data = {
            'task_id': 'round-trap',
            'forge_build_session_id': 'forge-sess',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/round-trap',
            'pr_url': 'https://github.com/x/y/pull/1',
            'revision_count': revision_count,
            'max_revisions': 3,
        }
        decision = {
            'marker_type': 'review_revision',
            'payload': {
                'task_id': 'round-trap', 'pr_url': data['pr_url'],
                'findings': [
                    {'file': 'a.py', 'line_range': 'L10',
                     'severity': 'medium', 'description': 'fix it'},
                ],
                'severity': 'medium', 'confidence': 'high',
            },
        }
        return data, decision

    def test_round1_revision_dispatch_omits_false_prior_round_trap(self):
        # Mirror finding: the resume-branch prompt appended the ROUND-N trap
        # unconditionally. On round 1 (revision_count=0 → next_count=1) the
        # build session is resumed and NO prior revision preamble exists, so
        # claiming a prior `Revision 0 applied:` line is false (and round 0 is
        # rejected by the M-3 validator). The trap must be gated to round 2+.
        data, decision = self._revision_dispatch_inputs(revision_count=0)
        on._dispatch_revision_to_forge(data, decision)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-round-trap-*.json')
        )
        self.assertEqual(len(revisions), 1)
        prompt = json.loads(revisions[0].read_text())['prompt']
        # No false prior-round claim on the dominant round-1 path.
        self.assertNotIn('TRAP', prompt)
        self.assertNotIn('Revision 0 applied:', prompt)
        self.assertNotIn('already exists', prompt)
        # The core preamble-discipline instruction is still present.
        self.assertIn('VERY FIRST characters', prompt)
        self.assertIn('Revision 1 applied:', prompt)

    def test_round2_revision_dispatch_carries_prior_round_trap(self):
        # On round 2 (revision_count=1 → next_count=2) the resumed session
        # really does hold a `Revision 1 applied:` line, so the trap is true
        # and must name the concrete prior/current round numbers.
        data, decision = self._revision_dispatch_inputs(revision_count=1)
        on._dispatch_revision_to_forge(data, decision)
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-round-trap-*.json')
        )
        self.assertEqual(len(revisions), 1)
        prompt = json.loads(revisions[0].read_text())['prompt']
        self.assertIn('ROUND-2 TRAP', prompt)
        self.assertIn('Revision 1 applied:', prompt)
        self.assertIn('Revision 2 applied:', prompt)

    def test_m8_rereview_prompt_includes_previous_findings(self):
        # Forge's revision outbox carries previous_findings via _build_outbox
        # propagation; _dispatch_mirror_review_rerun reads them and injects
        # into Mirror's re-review prompt. Without this, Mirror would
        # re-derive findings from scratch on round 2.
        data = {
            'task_id': 'real-rerev',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-rerev',
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
            (on.INBOXES_ROOT / 'mirror').glob('review-real-rerev-rev*.json')
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
            'task_id': 'prod-empty-findings',
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
            'task_id': 'real-no-url',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-no-url',
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
            'task_id': 'real-shipped-note',
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
            notify['task_id'], 'real-shipped-note',
        )
        # marker_error_count carries the retry counter.
        self.assertEqual(notify['marker_error_count'], 1)
        # Filename has the wrap for disk-level uniqueness.
        self.assertIn('marker-error-real-shipped-note-1',
                      notifies[0].name)

    def test_bug_b_forge_retry_2_also_keeps_original_task_id(self):
        # On retry 2, the previous envelope had task_id=original AND
        # marker_error_count=1. The retry-2 envelope should still have
        # task_id=original, marker_error_count=2.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'real-shipped-note',
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
            notify['task_id'], 'real-shipped-note',
        )
        self.assertEqual(notify['marker_error_count'], 2)

    def test_bug_b_mirror_marker_error_keeps_original_task_id(self):
        data = {
            'agent': 'mirror', 'source': 'beacon',
            'task_id': 'prod-mirror-bad-marker',
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
        self.assertEqual(notify['task_id'], 'prod-mirror-bad-marker')
        self.assertEqual(notify['marker_error_count'], 1)

    # ---- Bug C: dead-letter triggers Larry DM ----

    def test_bug_c_dead_letter_queues_larry_dm(self):
        # When marker-error retries exhaust, the dead-letter notify to
        # Beacon should ALSO queue a closing DM to Larry's chat thread.
        # Without this (the failed 2026-05-13 live test), the chat goes
        # silent after "approved + dispatched".
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'real-exhausted',
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
        self.assertEqual(notifs[0]['task_id'], 'real-exhausted')
        self.assertIn('failed after 4 marker-error retries', notifs[0]['message'])
        self.assertIn('no JSON', notifs[0]['message'])

    def test_bug_c_dead_letter_without_chat_id_does_not_queue_dm(self):
        # Autonomous Pulse-initiated runs have no reply_chat_id. The
        # dead-letter still writes to Beacon's inbox; no DM is queued
        # (silent skip in _maybe_dm_larry).
        data = {
            'agent': 'forge', 'source': 'beacon', 'task_id': 'real-auto',
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
            task_id='real-foo', reason='bad marker', retry_count=3,
        )
        self.assertIn('real-foo', rendered)
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
            'task_id': 'real-shipped-note',
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
            'agent': 'forge', 'source': 'beacon', 'task_id': 'real-no-chat',
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


class PreflightNoneFoundFillInGrammarTest(unittest.TestCase):
    """Fill-in-the-blank enrichment: a none-found preflight marker-error retry
    embeds the parser-synced grammar for all three preflight marker types so
    Forge pastes-and-fills instead of getting a scold she re-omits."""

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

    def _read_only_notify_prompt(self):
        notifies = list((on.INBOXES_ROOT / 'forge').glob('marker-error-*.json'))
        self.assertEqual(len(notifies), 1)
        return json.loads(notifies[0].read_text())['prompt']

    def test_none_found_preflight_retry_embeds_grammar(self):
        # The exact none-found failure shape: phase=preflight, no marker block.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'real-fillin', 'phase': 'preflight',
            'target_repo': 'ourliberty-agent-core',
            'result': 'I read the spec and it looks fine, will build it.',
        }
        on._notify_forge_marker_error(data, on.PREFLIGHT_NO_MARKER_ERROR_MSG)
        prompt = self._read_only_notify_prompt()
        # Literal delimiters for every preflight marker type appear.
        for keyword in ('PROCEED', 'CLARIFY_REQUEST', 'REJECT'):
            self.assertIn(f'=== {keyword} ===', prompt)
            self.assertIn(f'=== END_{keyword} ===', prompt)
        # Required field names appear.
        for field in ('task_id', 'preflight_summary', 'question', 'reason'):
            self.assertIn(field, prompt)
        # The real task_id is injected into the skeleton.
        self.assertIn('real-fillin', prompt)

    def test_grammar_is_parser_synced_drift_catch(self):
        # Drift-catch: every preflight MARKER_KEYWORD (sourced from the parser
        # module) must appear in the prompt. If a keyword is added/renamed in
        # fph without the grammar tracking it, this fails.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'real-drift', 'phase': 'preflight',
            'target_repo': 'ourliberty-agent-core',
            'result': 'no marker here',
        }
        on._notify_forge_marker_error(data, on.PREFLIGHT_NO_MARKER_ERROR_MSG)
        prompt = self._read_only_notify_prompt()
        for keyword in fph.MARKER_KEYWORDS.values():
            self.assertIn(f'=== {keyword} ===', prompt)
        # And every required field across all marker types is named.
        for fields in fph.REQUIRED_FIELDS.values():
            for field in fields:
                self.assertIn(field, prompt)

    def test_non_none_found_marker_error_unchanged(self):
        # A malformed-JSON marker error (delimiters present, bad JSON) is NOT a
        # none-found case — the grammar must NOT be embedded.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'real-badjson', 'phase': 'preflight',
            'target_repo': 'ourliberty-agent-core',
            'result': '=== PROCEED ===\n{bad json}\n=== END_PROCEED ===',
        }
        on._notify_forge_marker_error(data, 'proceed marker has invalid JSON: x')
        prompt = self._read_only_notify_prompt()
        self.assertNotIn('FILL-IN-THE-BLANK', prompt)
        self.assertNotIn('=== CLARIFY_REQUEST ===', prompt)

    def test_revision_phase_none_found_not_enriched(self):
        # The revision-preamble strict gate reuses _notify_forge_marker_error
        # but phase=revision — preflight grammar must NOT leak in.
        data = {
            'agent': 'forge', 'source': 'beacon',
            'task_id': 'real-rev', 'phase': 'revision',
            'target_repo': 'ourliberty-agent-core',
            'revision_count': 1, 'max_revisions': 3,
            'pr_url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/9',
            'result': 'Fixed the thing but forgot the preamble.',
        }
        on._notify_forge_marker_error(
            data, on.PREFLIGHT_NO_MARKER_ERROR_MSG,  # message irrelevant; phase gates
        )
        prompt = self._read_only_notify_prompt()
        self.assertNotIn('FILL-IN-THE-BLANK', prompt)

    def test_enrichment_via_process_outbox_integration(self):
        # End-to-end: a phase=preflight outbox with no marker flows through
        # process_outbox → _classify_forge_marker raises none-found →
        # _notify_forge_marker_error enriches.
        outbox = _good_outbox(
            agent='forge', source='beacon', task_id='real-e2e',
            phase='preflight', target_repo='ourliberty-agent-core',
            result='I started editing files directly, no marker emitted.',
        )
        outbox_dir = on.OUTBOXES_ROOT / 'forge'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / 'real-e2e.json'
        f.write_text(json.dumps(outbox))
        result = on.process_outbox(f)
        self.assertEqual(result, 'marker-error')
        prompt = self._read_only_notify_prompt()
        self.assertIn('FILL-IN-THE-BLANK', prompt)
        self.assertIn('=== PROCEED ===', prompt)


# -------------------- D3.5 5c — Beacon auto-replan loop --------------------


def _beacon_approval_request_marker(
    task_id='real-001',
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

        Defaults match the discipline-gate-pass shape: task_id='real-001'
        matches the envelope, the summary shares >2 >3-char tokens with
        mirror_escalate_reason ('validation', 'parser', etc).
        """
        if marker_text is None:
            marker_text = _beacon_approval_request_marker(task_id='real-001')
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
        # marker emits as task_id='real-001'.
        body = _good_outbox(
            agent='beacon',
            source='mirror-result',
            task_id='notify-real-001',
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
            task_id='real-001', reason='Missing input validation in the parser.',
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='real-001',
            target_repo='ourliberty-agent-core',
        )
        body['reply_chat_id'] = 7998341473
        f = self._write_outbox('mirror', 'real-001.json', body)
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
        marker = _mirror_escalate_marker(task_id='real-001')
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='real-001',
            target_repo='ourliberty-agent-core',
        )
        body['replan_count'] = 1
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'real-001.json', body)
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
        self.assertEqual(entry['id'], 'real-001')
        self.assertEqual(entry['chat_id'], 7998341473)
        self.assertEqual(entry.get('_replan_count'), 1)
        self.assertEqual(entry.get('_max_replans'), 2)
        # Approval-request alert queued
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 1)
        rec = approval_records[0]
        self.assertEqual(rec['approval_id'], 'real-001')
        self.assertEqual(rec['chat_id'], 7998341473)
        self.assertIn('real-001', rec['body'])

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
            task_id='real-001',
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
            task_id='real-001',
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
        bad_payload = json.dumps({'task_id': 'real-001'})  # missing fields
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
        entry = self._ah.find_pending_by_id('real-001')
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
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('real-001.json'))
        self.assertEqual(len(forge_tasks), 1)

    # ----- C-1 regression: replan_count propagates through dispatch hops -----

    def test_dispatch_build_phase_propagates_replan_count(self):
        """C-1 fix: replan_count + max_replans flow through preflight→build."""
        # Synthesize a Forge preflight outbox carrying replan_count=1
        preflight = _good_outbox(
            agent='forge', source='beacon', task_id='real-001',
            phase='preflight', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            result=(
                'Plan looks clean.\n=== PROCEED ===\n'
                '{"task_id": "real-001", "preflight_summary": "Ready."}\n'
                '=== END_PROCEED ==='
            ),
        )
        preflight['replan_count'] = 1
        preflight['max_replans'] = 2
        f = self._write_outbox('forge', 'real-001.json', preflight)
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
            agent='forge', source='beacon', task_id='real-001',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            result='PR opened: https://github.com/x/y/pull/77\n\nBuild done.',
        )
        build['replan_count'] = 1
        build['max_replans'] = 2
        f = self._write_outbox('forge', 'real-001.json', build)
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
            task_id='real-001', confidence='high', severity='medium',
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='real-001',
            target_repo='ourliberty-agent-core',
            branch='forge/real-001',
        )
        body['forge_build_session_id'] = 'sess-abc'
        body['pr_url'] = 'https://github.com/x/y/pull/77'
        body['replan_count'] = 1
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'real-001.json', body)
        on.process_outbox(f)
        # Revision task to Forge should carry the replan budget forward.
        revision_tasks = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-001-*.json')
        )
        self.assertEqual(len(revision_tasks), 1)
        revision = json.loads(revision_tasks[0].read_text())
        self.assertEqual(revision['replan_count'], 1)
        self.assertEqual(revision['max_replans'], 2)

    def test_dispatch_mirror_review_rerun_propagates_replan_count(self):
        """C-X1 second-pass fix: re-review dispatch also carries replan_count."""
        # Synthesize a Forge revision outbox with replan budget on envelope.
        revision_outbox = _good_outbox(
            agent='forge', source='beacon', task_id='real-001',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            claude_session_id='sess-abc',
            result=(
                'Revision 1 applied: added validation per Mirror finding.\n\n'
                'Tests pass; pushed to forge/real-001.'
            ),
        )
        revision_outbox['pr_url'] = 'https://github.com/x/y/pull/77'
        revision_outbox['revision_count'] = 1
        revision_outbox['max_revisions'] = 3
        revision_outbox['replan_count'] = 1
        revision_outbox['max_replans'] = 2
        f = self._write_outbox('forge', 'real-001.json', revision_outbox)
        on.process_outbox(f)
        # D3.5 5c-followup-2 HIGH-1 keyed filename when replan_count>0
        rereviews = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-real-001-replan*-rev*.json')
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
            task_id='real-001', confidence='low',
            findings=[
                {'file': 'parser.py', 'line_range': 'L12-L15',
                 'severity': 'medium',
                 'description': 'Missing input validation for malformed inputs.'},
            ],
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='real-001',
            target_repo='ourliberty-agent-core',
        )
        f = self._write_outbox('mirror', 'real-001.json', body)
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
            task_id='real-001', confidence='high',
            findings=[
                {'file': 'parser.py', 'line_range': 'L20',
                 'severity': 'medium',
                 'description': 'Edge case for empty input not handled.'},
            ],
        )
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='real-001',
            target_repo='ourliberty-agent-core',
        )
        body['revision_count'] = 3  # at the max, next would exhaust
        body['max_revisions'] = 3
        f = self._write_outbox('mirror', 'real-001.json', body)
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
            (on.INBOXES_ROOT / 'forge').glob('real-001.json')
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
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('real-001.json'))
        self.assertEqual(len(forge_tasks), 0)

    def test_inbound_intent_propagates_through_full_chain(self):
        """End-to-end: Mirror REVIEW_ESCALATE → notifier writes notify to
        Beacon's inbox with intent=review-escalate → inbox_watcher fires
        Beacon (here simulated by reading the notify file) → outbox would
        carry inbound_intent=review-escalate. We test the inbox-side
        contract: the notify task on Beacon's inbox has `intent=review-
        escalate`, which inbox_watcher._build_outbox would surface as
        `inbound_intent` on the outbox."""
        marker = _mirror_escalate_marker(task_id='real-001')
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='real-001',
            target_repo='ourliberty-agent-core',
        )
        f = self._write_outbox('mirror', 'real-001.json', body)
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
        marker = _mirror_pass_marker(task_id='real-001')
        body = _mirror_outbox_body(
            marker, source='beacon', task_id='real-001',
            target_repo='ourliberty-agent-core',
        )
        # Even if envelope has replan_count from a prior cycle, the
        # marker-driven routing should only carry it on escalate notifies.
        body['replan_count'] = 1
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'real-001.json', body)
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


# -------------------- closed-loop step 4 — Pulse-auto-dispatch trigger --------------------


class BeaconPulseAutoDispatchTest(unittest.TestCase):
    """Closed-loop step 4: Beacon outbox with source='pulse-auto-dispatch'
    + APPROVAL_REQUEST marker → notifier extracts, runs trust_policy with
    source='pulse-auto-dispatch', adds pending, queues larry-alert.

    Setup/teardown mirrors BeaconReplanLoopTest exactly — same tmpdir
    reroute of AGENTS_ROOT + INBOXES_ROOT + OUTBOXES_ROOT + larry_alerts +
    pending-approvals state + trust_policy paths.
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
        ah.PENDING_APPROVALS_PATH = self._root / 'state' / 'pending-approvals.json'
        self._ah = ah
        import trust_policy as tp
        self._tp = tp
        self._tp_original_runtime = tp.RUNTIME_POLICY_PATH
        self._tp_original_repo = tp.REPO_POLICY_PATH
        tp.RUNTIME_POLICY_PATH = self._root / 'trust-policy.json'
        tp.REPO_POLICY_PATH = self._root / 'trust-policy-repo.json'
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
        if not self._la.ALERTS_FILE.exists():
            return []
        lines = self._la.ALERTS_FILE.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _pulse_auto_dispatch_outbox(
        self,
        marker_text=None,
        narrative_prefix='Plan for the Pulse-flagged proposal below.',
        reply_chat_id=7998341473,
        task_id='real-001',
        **overrides,
    ):
        """Synthetic Beacon outbox responding to a Pulse-auto-dispatch
        inbox envelope. Marker payload's task_id matches the envelope
        task_id by default so the discipline gate passes."""
        if marker_text is None:
            marker_text = _beacon_approval_request_marker(task_id=task_id)
        result = (
            f'{narrative_prefix}\n\n{marker_text}'
            if marker_text else narrative_prefix
        )
        body = _good_outbox(
            agent='beacon',
            source='pulse-auto-dispatch',
            task_id=task_id,
            result=result,
        )
        if reply_chat_id is not None:
            body['reply_chat_id'] = reply_chat_id
        body.update(overrides)
        return body

    # ----- happy path: marker extracted, force_ask queued -----

    def test_approval_request_force_ask_queues_alert(self):
        body = self._pulse_auto_dispatch_outbox()
        f = self._write_outbox('beacon', 'beacon-pad.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-pulse-auto-dispatch')
        state = self._ah.load_state()
        pending = state.get('pending', [])
        self.assertEqual(len(pending), 1)
        entry = pending[0]
        self.assertEqual(entry['id'], 'real-001')
        self.assertEqual(entry['chat_id'], 7998341473)
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 1)
        rec = approval_records[0]
        self.assertEqual(rec['approval_id'], 'real-001')
        self.assertEqual(rec['chat_id'], 7998341473)
        self.assertIn('real-001', rec['body'])

    # ----- no marker → falls through to default routing -----

    def test_no_marker_falls_through(self):
        body = self._pulse_auto_dispatch_outbox(marker_text='')
        f = self._write_outbox('beacon', 'beacon-pad-noop.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-pulse-auto-dispatch')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 0)

    # ----- malformed marker → WARN + fall through -----

    def test_malformed_marker_falls_through(self):
        bad_payload = json.dumps({'task_id': 'real-001'})  # missing fields
        bad_marker = (
            f'=== APPROVAL_REQUEST ===\n{bad_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = self._pulse_auto_dispatch_outbox(marker_text=bad_marker)
        f = self._write_outbox('beacon', 'beacon-pad-bad.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-pulse-auto-dispatch')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    # ----- discipline gate: task_id mismatch falls through -----

    def test_task_id_mismatch_falls_through(self):
        marker = _beacon_approval_request_marker(task_id='task-WRONG')
        body = self._pulse_auto_dispatch_outbox(marker_text=marker)
        f = self._write_outbox('beacon', 'beacon-pad-mismatch.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-pulse-auto-dispatch')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    # ----- missing chat_id → fall through -----

    def test_missing_reply_chat_id_falls_through(self):
        body = self._pulse_auto_dispatch_outbox(reply_chat_id=None)
        body.pop('reply_chat_id', None)
        f = self._write_outbox('beacon', 'beacon-pad-nochat.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-pulse-auto-dispatch')

    # ----- trust_policy: reject path DMs Larry the rejection -----

    def test_trust_policy_reject_queues_rejection_dm(self):
        self._set_policy(default_action='reject')
        body = self._pulse_auto_dispatch_outbox()
        f = self._write_outbox('beacon', 'beacon-pad-rej.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-pulse-auto-dispatch')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        alerts = self._read_alerts()
        notifs = [a for a in alerts if a.get('kind') == 'notification']
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['intent'], 'reject')

    # ----- trust_policy: auto_approve path dispatches + resolves -----

    def test_trust_policy_auto_approve_dispatches(self):
        self._set_policy(default_action='auto_approve')
        body = self._pulse_auto_dispatch_outbox()
        f = self._write_outbox('beacon', 'beacon-pad-aa.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-pulse-auto-dispatch')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        self.assertEqual(len(state.get('history', [])), 1)
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('real-001.json'))
        self.assertEqual(len(forge_tasks), 1)

    # ----- replay dedup: second run skips add_pending -----

    def test_replay_dedups_pending_by_task_id(self):
        body = self._pulse_auto_dispatch_outbox()
        f1 = self._write_outbox('beacon', 'beacon-pad-rep.json', body)
        on.process_outbox(f1)
        f2 = self._write_outbox('beacon', 'beacon-pad-rep2.json', body)
        result = on.process_outbox(f2)
        self.assertEqual(result, 'notified-pulse-auto-dispatch')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 1)
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 1)

    # ----- source guard: replan branch does NOT fire on pulse-auto-dispatch -----

    def test_pulse_auto_dispatch_does_not_trigger_replan_branch(self):
        """Defensive: pulse-auto-dispatch with inbound_intent='review-escalate'
        somehow set on the envelope must still route through the pulse path
        (which uses the source gate), not the replan path."""
        body = self._pulse_auto_dispatch_outbox()
        # Even if the envelope were mis-tagged with the replan intent, the
        # source-keyed pulse gate should claim it first by position.
        f = self._write_outbox('beacon', 'beacon-pad-srcguard.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-pulse-auto-dispatch')

    # ----- regression: replan path still fires on review-escalate -----

    def test_replan_path_still_fires_on_review_escalate(self):
        """Regression guard: adding the new branch did not displace the
        replan branch. A review-escalate envelope still routes there."""
        # Reuse the replan helper shape by hand (avoid reaching into another
        # test class). Source is 'mirror-result' + inbound_intent triggers
        # the replan branch.
        marker = _beacon_approval_request_marker(task_id='real-001')
        body = _good_outbox(
            agent='beacon',
            source='mirror-result',
            task_id='notify-real-001',
            result=f'Plan revision.\n\n{marker}',
        )
        body['inbound_intent'] = 'review-escalate'
        body['replan_count'] = 0
        body['max_replans'] = 2
        body['mirror_escalate_reason'] = (
            'Address Mirror concern about missing input validation in the parser.'
        )
        body['reply_chat_id'] = 7998341473
        f = self._write_outbox('beacon', 'beacon-replan-reg.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-replan')

    # ----- regression: headless source='larry' path still fires -----

    def test_headless_larry_source_path_still_fires(self):
        """Regression guard: source='larry' headless path is unchanged."""
        marker = _beacon_approval_request_marker(task_id='real-001')
        body = _good_outbox(
            agent='beacon',
            source='larry',
            task_id='real-001',
            result=f'Plan ready.\n\n{marker}',
        )
        body['reply_chat_id'] = 7998341473
        f = self._write_outbox('beacon', 'beacon-headless-reg.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'headless-approval-dispatched')


# -------------------- fix-depth1-pulse-approval-extraction-001 (2026-06-12) --------------------


class BeaconPulseDirectionAskTest(unittest.TestCase):
    """A depth=1 Beacon beacon-result with source='pulse' (a Pulse
    direction-ask answer) carrying an APPROVAL_REQUEST marker must be
    extracted + registered through the SAME pipeline as pulse-auto-dispatch:
    trust_policy is consulted, add_pending creates the entry, an
    approval_request chain_event fires, and a force_ask alert reaches Larry.

    Direction-ask accommodations exercised here: reply_chat_id=null falls
    back to the default Larry chat (TELEGRAM_ALLOWED_CHAT_IDS) instead of
    dropping the approval, and the marker's task_id is authoritative (no
    envelope/marker match gate). A markerless source='pulse' result must
    still fall through to default Pulse-notify routing (no regression).

    Setup/teardown mirrors BeaconPulseAutoDispatchTest, plus management of
    the TELEGRAM_ALLOWED_CHAT_IDS env that resolves the chat fallback.
    """

    _DEFAULT_LARRY_CHAT = 7998341473

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
        ah.PENDING_APPROVALS_PATH = self._root / 'state' / 'pending-approvals.json'
        self._ah = ah
        import trust_policy as tp
        self._tp = tp
        self._tp_original_runtime = tp.RUNTIME_POLICY_PATH
        self._tp_original_repo = tp.REPO_POLICY_PATH
        tp.RUNTIME_POLICY_PATH = self._root / 'trust-policy.json'
        tp.REPO_POLICY_PATH = self._root / 'trust-policy-repo.json'
        self._chat_env_backup = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS')
        os.environ['TELEGRAM_ALLOWED_CHAT_IDS'] = str(self._DEFAULT_LARRY_CHAT)
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
        if self._chat_env_backup is None:
            os.environ.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
        else:
            os.environ['TELEGRAM_ALLOWED_CHAT_IDS'] = self._chat_env_backup
        self._tmp.cleanup()

    def _set_policy(self, default_action='force_ask', rules=None):
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
        if not self._la.ALERTS_FILE.exists():
            return []
        lines = self._la.ALERTS_FILE.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _direction_ask_outbox(
        self,
        marker_text=None,
        narrative_prefix='Answering the Pulse direction-ask below.',
        reply_chat_id=None,
        envelope_task_id='direction-ask-001',
        marker_task_id='fix-alert-triage-watermark-durability-001',
        **overrides,
    ):
        """Synthetic depth=1 Beacon beacon-result answering a Pulse
        direction-ask. source='pulse'; the marker proposes a NEW task whose
        task_id differs from the question envelope's; reply_chat_id is null
        (the gap that stranded fix-alert-triage-watermark-durability-001)."""
        if marker_text is None:
            marker_text = _beacon_approval_request_marker(task_id=marker_task_id)
        result = (
            f'{narrative_prefix}\n\n{marker_text}'
            if marker_text else narrative_prefix
        )
        body = _good_outbox(
            agent='beacon',
            source='pulse',
            task_id=envelope_task_id,
            result=result,
            _notify_depth=1,
        )
        body['reply_chat_id'] = reply_chat_id
        body.update(overrides)
        return body

    # ----- happy path: marker extracted + registered via fallback chat -----

    def test_direction_ask_marker_extracted_and_registered(self):
        emitted = []

        def _spy_emit(**kwargs):
            emitted.append(kwargs)

        body = self._direction_ask_outbox()
        f = self._write_outbox('beacon', 'beacon-da.json', body)
        with mock.patch.object(on.chain_event_emit, 'emit_event', _spy_emit):
            result = on.process_outbox(f)
        # Took over routing (NOT a plain Pulse-notify).
        self.assertEqual(result, 'notified-pulse-direction-ask')
        # add_pending registered the marker's (authoritative) task_id with
        # the fallback Larry chat.
        state = self._ah.load_state()
        pending = state.get('pending', [])
        self.assertEqual(len(pending), 1)
        entry = pending[0]
        self.assertEqual(entry['id'], 'fix-alert-triage-watermark-durability-001')
        self.assertEqual(entry['chat_id'], self._DEFAULT_LARRY_CHAT)
        # approval_request chain_event fired.
        self.assertTrue(
            any(e.get('event_type') == 'approval_request' for e in emitted),
            f'expected an approval_request chain_event, got {emitted!r}',
        )
        # force_ask alert reached Larry on the fallback chat.
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 1)
        self.assertEqual(approval_records[0]['chat_id'], self._DEFAULT_LARRY_CHAT)
        self.assertEqual(
            approval_records[0]['approval_id'],
            'fix-alert-triage-watermark-durability-001',
        )

    # ----- regression: markerless source='pulse' result falls through -----

    def test_no_marker_falls_through_to_default_notify(self):
        body = self._direction_ask_outbox(marker_text='')
        f = self._write_outbox('beacon', 'beacon-da-noop.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-pulse-direction-ask')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 0)

    # ----- regression: malformed marker falls through cleanly -----

    def test_malformed_marker_falls_through(self):
        bad_payload = json.dumps({'task_id': 'x'})  # missing required fields
        bad_marker = (
            f'=== APPROVAL_REQUEST ===\n{bad_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = self._direction_ask_outbox(marker_text=bad_marker)
        f = self._write_outbox('beacon', 'beacon-da-bad.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-pulse-direction-ask')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    # ----- trust_policy still consulted: reject path DMs Larry -----

    def test_trust_policy_reject_queues_rejection_dm(self):
        self._set_policy(default_action='reject')
        body = self._direction_ask_outbox()
        f = self._write_outbox('beacon', 'beacon-da-rej.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-pulse-direction-ask')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        alerts = self._read_alerts()
        notifs = [a for a in alerts if a.get('kind') == 'notification']
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['intent'], 'reject')

    # ----- fallback unavailable: no chat env → fall through, not drop-to-DM -----

    def test_no_chat_fallback_available_falls_through(self):
        os.environ.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
        body = self._direction_ask_outbox()
        f = self._write_outbox('beacon', 'beacon-da-nochat.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-pulse-direction-ask')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    # ----- explicit reply_chat_id is honored over the fallback -----

    def test_explicit_reply_chat_id_used_when_present(self):
        body = self._direction_ask_outbox(reply_chat_id=12345)
        f = self._write_outbox('beacon', 'beacon-da-explicit.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-pulse-direction-ask')
        state = self._ah.load_state()
        pending = state.get('pending', [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['chat_id'], 12345)


# -------------------- autonomy-visibility keystone: board-delegate route (2026-06-21) --------------------


class BeaconBoardDelegateTest(unittest.TestCase):
    """The board-delegate dispatch route. A Beacon outbox responding to a board
    "Delegate to team" envelope (source='dashboard' — emitted by the dashboard
    delegate endpoint or the board-drain) carrying an APPROVAL_REQUEST marker
    must be extracted + trust-gated through the SAME pipeline as the pulse
    paths, with the dashboard accommodations:

      - trust is evaluated as a Beacon→Forge dispatch (policy_source='beacon'),
        NOT source='dashboard', so the live agent-core auto_approve rule
        applies — the keystone that un-blocks the board-drain + the manual
        Delegate button (both previously dead-ended at a notify-back);
      - reply_chat_id=null falls back to the default Larry chat;
      - the marker proposes a NEW scoped task whose task_id differs from the
        `delegate-{capture_id}` envelope task_id (no mismatch gate).

    A markerless / non-Forge dashboard result still falls through to default
    routing. Setup/teardown mirrors BeaconPulseDirectionAskTest (it shares the
    chat-fallback + task_id-authoritative accommodations)."""

    _DEFAULT_LARRY_CHAT = 7998341473

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
        ah.PENDING_APPROVALS_PATH = self._root / 'state' / 'pending-approvals.json'
        self._ah = ah
        import trust_policy as tp
        self._tp = tp
        self._tp_original_runtime = tp.RUNTIME_POLICY_PATH
        self._tp_original_repo = tp.REPO_POLICY_PATH
        tp.RUNTIME_POLICY_PATH = self._root / 'trust-policy.json'
        tp.REPO_POLICY_PATH = self._root / 'trust-policy-repo.json'
        self._chat_env_backup = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS')
        os.environ['TELEGRAM_ALLOWED_CHAT_IDS'] = str(self._DEFAULT_LARRY_CHAT)
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
        if self._chat_env_backup is None:
            os.environ.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
        else:
            os.environ['TELEGRAM_ALLOWED_CHAT_IDS'] = self._chat_env_backup
        self._tmp.cleanup()

    def _set_policy(self, default_action='force_ask', rules=None):
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
        if not self._la.ALERTS_FILE.exists():
            return []
        lines = self._la.ALERTS_FILE.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _delegate_outbox(
        self,
        marker_text=None,
        narrative_prefix='Scoped the board-delegated capture below.',
        reply_chat_id=None,
        envelope_task_id='delegate-cap-projects-pm-layer-data-is-stale',
        marker_task_id='cap-projects-pm-layer-data-is-stale-001',
        **overrides,
    ):
        """Synthetic Beacon outbox answering a board-delegate envelope.
        source='dashboard'; reply_chat_id=null (board action, no chat thread);
        the marker proposes a NEW scoped task whose task_id differs from the
        `delegate-{capture_id}` envelope task_id."""
        if marker_text is None:
            marker_text = _beacon_approval_request_marker(task_id=marker_task_id)
        result = (
            f'{narrative_prefix}\n\n{marker_text}'
            if marker_text else narrative_prefix
        )
        body = _good_outbox(
            agent='beacon',
            source='dashboard',
            task_id=envelope_task_id,
            result=result,
        )
        body['reply_chat_id'] = reply_chat_id
        body.update(overrides)
        return body

    # ----- happy path: marker extracted, force_ask queued on fallback chat -----

    def test_delegate_marker_extracted_and_force_ask_queued(self):
        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        pending = state.get('pending', [])
        self.assertEqual(len(pending), 1)
        entry = pending[0]
        # Marker task_id is authoritative (NOT the delegate-... envelope id).
        self.assertEqual(entry['id'], 'cap-projects-pm-layer-data-is-stale-001')
        # reply_chat_id=null → fell back to the default Larry chat.
        self.assertEqual(entry['chat_id'], self._DEFAULT_LARRY_CHAT)
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 1)
        self.assertEqual(approval_records[0]['chat_id'], self._DEFAULT_LARRY_CHAT)
        self.assertEqual(
            approval_records[0]['approval_id'],
            'cap-projects-pm-layer-data-is-stale-001',
        )

    # ----- KEYSTONE: trust is evaluated as source='beacon', so the live -----
    # ----- agent-core auto_approve rule fires for a dashboard-sourced result -----

    def test_trust_evaluated_as_beacon_source_auto_approves(self):
        # The ONLY auto_approve rule keys source='beacon'. A dashboard-sourced
        # result reaching auto_approve PROVES the route evaluates trust with
        # policy_source='beacon' (had it passed source='dashboard', no rule
        # would match and it would fall to the force_ask default).
        self._set_policy(
            default_action='force_ask',
            rules=[{
                'source': 'beacon',
                'target': 'forge',
                'repos': ['ourliberty-agent-core'],
                'action': 'auto_approve',
            }],
        )
        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd-aa.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        # auto_approve resolved the entry (no longer pending) + dispatched.
        self.assertEqual(len(state.get('pending', [])), 0)
        self.assertEqual(len(state.get('history', [])), 1)
        forge_tasks = list(
            (on.INBOXES_ROOT / 'forge').glob(
                'cap-projects-pm-layer-data-is-stale-001.json'))
        self.assertEqual(len(forge_tasks), 1)
        # Auto-approve confirmation DM queued on the fallback chat.
        alerts = self._read_alerts()
        notifs = [a for a in alerts if a.get('kind') == 'notification']
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['intent'], 'review-pass')

    # ----- a dashboard-keyed rule must NOT match (we evaluate as beacon) -----

    def test_dashboard_keyed_rule_does_not_match(self):
        # Inverse of the keystone test: a rule keyed source='dashboard' must
        # NOT fire, because the route deliberately evaluates as 'beacon'. The
        # result falls to the force_ask default → no Forge dispatch.
        self._set_policy(
            default_action='force_ask',
            rules=[{'source': 'dashboard', 'action': 'auto_approve'}],
        )
        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd-noaa.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 1)
        forge_tasks = list((on.INBOXES_ROOT / 'forge').glob('*.json'))
        self.assertEqual(len(forge_tasks), 0)

    # ----- autonomy_decision chain_event records the dispatch (source=beacon) -----

    def test_autonomy_decision_recorded_with_beacon_source(self):
        self._set_policy(
            default_action='force_ask',
            rules=[{
                'source': 'beacon',
                'target': 'forge',
                'repos': ['ourliberty-agent-core'],
                'action': 'auto_approve',
            }],
        )
        emitted = []

        def _spy_emit(**kwargs):
            emitted.append(kwargs)

        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd-ad.json', body)
        with mock.patch.object(on.chain_event_emit, 'emit_event', _spy_emit):
            result = on.process_outbox(f)
        self.assertEqual(result, 'notified-board-delegate')
        autonomy = [e for e in emitted if e.get('event_type') == 'autonomy_decision']
        self.assertEqual(len(autonomy), 1)
        payload = autonomy[0]['payload']
        self.assertEqual(payload['decision'], 'auto_approve')
        self.assertTrue(payload['dispatched'])
        # source attributes the trust evaluation: 'beacon', consistent with
        # the chat dispatch this now matches (NOT 'dashboard').
        self.assertEqual(payload['source'], 'beacon')
        self.assertEqual(payload['target_repo'], 'ourliberty-agent-core')
        self.assertIsNotNone(payload['matched_rule'])

    # ----- trust_policy reject → DM Larry the rejection -----

    def test_trust_policy_reject_queues_rejection_dm(self):
        self._set_policy(default_action='reject')
        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd-rej.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        alerts = self._read_alerts()
        notifs = [a for a in alerts if a.get('kind') == 'notification']
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]['intent'], 'reject')

    # ----- regression: markerless dashboard result falls through -----

    def test_no_marker_falls_through_to_default_notify(self):
        body = self._delegate_outbox(marker_text='')
        f = self._write_outbox('beacon', 'beacon-bd-noop.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)
        alerts = self._read_alerts()
        approval_records = [a for a in alerts if a.get('kind') == 'approval_request']
        self.assertEqual(len(approval_records), 0)

    # ----- regression: malformed marker falls through cleanly -----

    def test_malformed_marker_falls_through(self):
        bad_payload = json.dumps({'task_id': 'x'})  # missing required fields
        bad_marker = (
            f'=== APPROVAL_REQUEST ===\n{bad_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = self._delegate_outbox(marker_text=bad_marker)
        f = self._write_outbox('beacon', 'beacon-bd-bad.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    # ----- fallback unavailable: no chat env → fall through (don't drop) -----

    def test_no_chat_fallback_available_falls_through(self):
        os.environ.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd-nochat.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        self.assertEqual(len(state.get('pending', [])), 0)

    # ----- explicit reply_chat_id honored over the fallback -----

    def test_explicit_reply_chat_id_used_when_present(self):
        body = self._delegate_outbox(reply_chat_id=12345)
        f = self._write_outbox('beacon', 'beacon-bd-explicit.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-board-delegate')
        state = self._ah.load_state()
        pending = state.get('pending', [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['chat_id'], 12345)

    # ----- delegate no-outcome backstop (2026-07-11 revert report) -----
    # A `delegate-*` envelope whose Beacon run produced NO durable outcome must
    # surface Beacon's verdict to Larry instead of dying in archived-no-notify.

    def _no_outcome_alerts(self):
        return [a for a in self._read_alerts()
                if a.get('source') == 'outbox-notifier' and a.get('needs_larry')]

    def test_no_marker_delegate_surfaces_needs_you_alert(self):
        body = self._delegate_outbox(
            marker_text='',
            narrative_prefix=(
                'Scoped it. Already fixed two weeks ago — dismiss the card.'),
        )
        f = self._write_outbox('beacon', 'beacon-bd-noout.json', body)
        on.process_outbox(f)
        alerts = self._no_outcome_alerts()
        self.assertEqual(len(alerts), 1)
        rec = alerts[0]
        self.assertEqual(rec['route'], 'escalate')
        # Cooldown subject = task_id + verdict digest (so a NEW verdict for
        # the same card isn't suppressed by an earlier one's cooldown).
        self.assertTrue(rec['subject'].startswith(
            'delegate-cap-projects-pm-layer-data-is-stale:'))
        # The message names the card and carries Beacon's verdict.
        self.assertIn('cap-projects-pm-layer-data-is-stale', rec['message'])
        self.assertIn('Already fixed two weeks ago', rec['message'])

    def test_malformed_marker_delegate_surfaces_alert(self):
        bad_payload = json.dumps({'task_id': 'x'})  # missing required fields
        bad_marker = (
            f'=== APPROVAL_REQUEST ===\n{bad_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = self._delegate_outbox(marker_text=bad_marker)
        f = self._write_outbox('beacon', 'beacon-bd-badout.json', body)
        on.process_outbox(f)
        self.assertEqual(len(self._no_outcome_alerts()), 1)

    def test_no_marker_non_delegate_dashboard_stays_silent(self):
        # The backstop is scoped to delegate-* envelopes; other dashboard
        # results (card messages etc.) keep the silent fall-through.
        body = self._delegate_outbox(
            marker_text='', envelope_task_id='card-message-cap-something')
        f = self._write_outbox('beacon', 'beacon-bd-cardmsg.json', body)
        on.process_outbox(f)
        self.assertEqual(self._no_outcome_alerts(), [])

    def test_marker_present_no_backstop_alert(self):
        # A durable outcome (force_ask pending approval) means no backstop —
        # the approval flow already reaches Larry.
        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd-withmarker.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-board-delegate')
        self.assertEqual(self._no_outcome_alerts(), [])

    def test_valid_marker_notifier_side_decline_no_alert(self):
        # The route declines for a NOTIFIER-side reason (no reply chat and no
        # fallback chat configured) even though Beacon emitted a well-formed
        # proposal. The backstop must NOT misdiagnose that as "nothing was
        # handed to the team".
        os.environ.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
        body = self._delegate_outbox()
        f = self._write_outbox('beacon', 'beacon-bd-decline.json', body)
        result = on.process_outbox(f)
        self.assertNotEqual(result, 'notified-board-delegate')
        self.assertEqual(self._no_outcome_alerts(), [])

    def test_forfeit_outbox_without_source_alerts(self):
        # A watcher-restart forfeit (reap_orphans) writes NO `source` field, so
        # process_outbox exits at the no-source guard — the forfeit leg of the
        # backstop must still surface the died-verdictless delegate.
        body = self._delegate_outbox(
            marker_text='', narrative_prefix='(forfeit: watcher restarted)')
        body.pop('source', None)
        f = self._write_outbox('beacon', 'beacon-bd-forfeit.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'archived-no-notify')
        self.assertEqual(len(self._no_outcome_alerts()), 1)

    def test_distinct_second_verdict_not_cooldown_suppressed(self):
        # The cooldown key carries a digest of the verdict text: a re-processed
        # IDENTICAL outbox dedups, but a NEW verdict for the same card (e.g.
        # after a force re-delegation) still alerts inside the 6h window.
        first = self._delegate_outbox(
            marker_text='', narrative_prefix='First verdict: already fixed.')
        f1 = self._write_outbox('beacon', 'beacon-bd-v1.json', first)
        on.process_outbox(f1)
        second = self._delegate_outbox(
            marker_text='', narrative_prefix='Second verdict: cannot repro.')
        f2 = self._write_outbox('beacon', 'beacon-bd-v2.json', second)
        on.process_outbox(f2)
        self.assertEqual(len(self._no_outcome_alerts()), 2)


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
        (forge_inbox_archive / 'build-real-001.json').write_text('{}')

        # Iteration 2 preflight proceeds — dispatch should land at the
        # replan-keyed filename, not collide
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "real-001", "preflight_summary": "Iteration 2 ready."}\n'
            '=== END_PROCEED ==='
        )
        preflight = _good_outbox(
            agent='forge', source='beacon', task_id='real-001',
            phase='preflight', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            claude_session_id='sess-iter2-xyz',
            result=f'Ready.\n\n{marker}',
        )
        preflight['replan_count'] = 1
        preflight['max_replans'] = 2
        f = self._write_outbox('forge', 'real-001.json', preflight)
        on.process_outbox(f)

        # New build task lands at the keyed filename
        keyed = list((on.INBOXES_ROOT / 'forge').glob('build-real-001-replan1.json'))
        self.assertEqual(
            len(keyed), 1,
            f'expected build-real-001-replan1.json (round-keyed); '
            f'inbox contents: {list((on.INBOXES_ROOT / "forge").iterdir())}',
        )

    def test_build_dispatch_unkeyed_when_replan_count_zero(self):
        """Backward-compat: replan_count=0 (or missing) uses the legacy
        unkeyed filename so prior idempotency behavior is preserved."""
        marker = (
            '=== PROCEED ===\n'
            '{"task_id": "real-001", "preflight_summary": "Fresh dispatch."}\n'
            '=== END_PROCEED ==='
        )
        preflight = _good_outbox(
            agent='forge', source='beacon', task_id='real-001',
            phase='preflight', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            claude_session_id='sess-fresh-xyz',
            result=f'Ready.\n\n{marker}',
        )
        # replan_count omitted (defaults to 0)
        f = self._write_outbox('forge', 'real-001.json', preflight)
        on.process_outbox(f)

        legacy = list((on.INBOXES_ROOT / 'forge').glob('build-real-001.json'))
        self.assertEqual(len(legacy), 1)
        keyed = list((on.INBOXES_ROOT / 'forge').glob('build-real-001-replan*.json'))
        self.assertEqual(len(keyed), 0)

    # ----- C-2: _dispatch_mirror_review keyed by replan_count -----

    def test_mirror_review_dispatch_keyed_by_replan_count_iteration_2(self):
        """Replan iteration 2's first Mirror review must NOT collide with
        iteration 1's archived review task."""
        mirror_inbox_archive = on.INBOXES_ROOT / 'mirror' / '.archive'
        mirror_inbox_archive.mkdir(parents=True, exist_ok=True)
        (mirror_inbox_archive / 'review-real-001.json').write_text('{}')

        build = _good_outbox(
            agent='forge', source='beacon', task_id='real-001',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            result='PR opened: https://github.com/x/y/pull/77\n\nBuild done.',
        )
        build['replan_count'] = 1
        build['max_replans'] = 2
        f = self._write_outbox('forge', 'real-001.json', build)
        on.process_outbox(f)

        keyed = list((on.INBOXES_ROOT / 'mirror').glob('review-real-001-replan1.json'))
        self.assertEqual(
            len(keyed), 1,
            f'expected review-real-001-replan1.json (round-keyed); '
            f'inbox contents: {list((on.INBOXES_ROOT / "mirror").iterdir())}',
        )

    def test_mirror_review_dispatch_unkeyed_when_replan_count_zero(self):
        """Backward-compat: round 1 still uses the legacy unkeyed filename."""
        build = _good_outbox(
            agent='forge', source='beacon', task_id='real-001',
            phase='build', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            result='PR opened: https://github.com/x/y/pull/77\n\nBuild done.',
        )
        # replan_count omitted
        f = self._write_outbox('forge', 'real-001.json', build)
        on.process_outbox(f)

        legacy = list((on.INBOXES_ROOT / 'mirror').glob('review-real-001.json'))
        self.assertEqual(len(legacy), 1)
        keyed = list((on.INBOXES_ROOT / 'mirror').glob('review-real-001-replan*.json'))
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
        (forge_archive / 'revision-real-001-1.json').write_text('{}')

        # Synthesize Mirror REVIEW_REVISION outbox for replan iter 2
        marker_payload = json.dumps({
            'task_id': 'real-001',
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
            agent='mirror', source='beacon', task_id='real-001',
            phase='review', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            result=f'Findings.\n\n{marker}',
        )
        body['forge_build_session_id'] = 'sess-abc'
        body['pr_url'] = 'https://github.com/x/y/pull/77'
        body['replan_count'] = 1  # replan iteration 2
        body['max_replans'] = 2
        f = self._write_outbox('mirror', 'real-001.json', body)
        on.process_outbox(f)
        # Revision task should land at the replan-keyed filename, not collide
        keyed = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-001-replan1-*.json')
        )
        self.assertEqual(
            len(keyed), 1,
            f'expected replan-keyed revision filename; inbox: '
            f'{list((on.INBOXES_ROOT / "forge").iterdir())}',
        )
        # And the legacy unkeyed filename should NOT have been written
        # (the archived collider is still the only one there)
        legacy_inbox = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-001-1.json')
        )
        self.assertEqual(len(legacy_inbox), 0)

    def test_rerun_review_dispatch_keyed_by_replan_count_combined_state(self):
        """HIGH-1 sibling: _dispatch_mirror_review_rerun also keys by
        replan_count to avoid colliding with prior replan iteration's
        archived re-reviews."""
        mirror_archive = on.INBOXES_ROOT / 'mirror' / '.archive'
        mirror_archive.mkdir(parents=True, exist_ok=True)
        (mirror_archive / 'review-real-001-rev1.json').write_text('{}')

        revision_outbox = _good_outbox(
            agent='forge', source='beacon', task_id='real-001',
            phase='revision', target_repo='ourliberty-agent-core',
            branch='forge/real-001',
            claude_session_id='sess-abc',
            result=(
                'Revision 1 applied: validation added per Mirror finding.\n'
                '\nTests pass; pushed to forge/real-001.'
            ),
        )
        revision_outbox['pr_url'] = 'https://github.com/x/y/pull/77'
        revision_outbox['revision_count'] = 1
        revision_outbox['max_revisions'] = 3
        revision_outbox['replan_count'] = 1
        revision_outbox['max_replans'] = 2
        f = self._write_outbox('forge', 'real-001.json', revision_outbox)
        on.process_outbox(f)
        keyed = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-real-001-replan1-rev*.json')
        )
        self.assertEqual(len(keyed), 1)
        legacy = list(
            (on.INBOXES_ROOT / 'mirror').glob('review-real-001-rev1.json')
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
            "Commit `8e5c692` pushed to `forge/real-001`, the head branch "
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

    def _beacon_replan_outbox(self, target_repo='ourliberty-agent-core'):
        marker_payload = json.dumps({
            'task_id': 'real-001',
            'summary': (
                "Address Mirror's input validation concern in the parser."
            ),
            'target_agent': 'forge',
            'prompt': 'x' * 200,
            'target_repo': target_repo,
            'task_type': 'feature-development',
        })
        marker = (
            f'=== APPROVAL_REQUEST ===\n{marker_payload}\n'
            f'=== END_APPROVAL_REQUEST ==='
        )
        body = _good_outbox(
            agent='beacon',
            source='mirror-result',
            task_id='notify-real-001',
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
        self.assertEqual(pending[0]['id'], 'real-001')
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
        self.assertEqual(backlog[0]['id'], 'real-001')
        self.assertEqual(backlog[0].get('_replan_count'), 1)

    def test_no_pause_proceeds_normally(self):
        """Sanity: no pause → force_ask alert queued as before. Post-autonomy
        (#615) an agent-core replan auto-approves, so this force_ask-path test
        targets a still-ask repo (ourliberty-dashboard)."""
        body = self._beacon_replan_outbox(target_repo='ourliberty-dashboard')
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


class PostMirrorReviewCommitStatusTest(unittest.TestCase):
    """build-mirror-review-status — `_post_mirror_review_commit_status` gh
    interaction: state mapping, head-SHA targeting, error tolerance."""

    def setUp(self):
        # Exercise the real helper (setUpModule installs an inert override).
        self._orig_override = on._POST_STATUS_FN_OVERRIDE
        on._POST_STATUS_FN_OVERRIDE = None

    def tearDown(self):
        on._POST_STATUS_FN_OVERRIDE = self._orig_override

    def _cp(self, *, returncode=0, stdout='', stderr=''):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def _decision(self, marker_type='review_pass', pr_url=PR_URL_FIXTURE):
        return {'marker_type': marker_type, 'payload': {'pr_url': pr_url}}

    def _run_helper(self, decision, *, head_sha='deadbeefcafe',
                    view_rc=0, api_rc=0):
        """Run the helper with subprocess.run mocked; return (state, api_cmds)."""
        api_cmds: list[list[str]] = []

        def _run(cmd, **kwargs):
            if 'view' in cmd:
                stdout = json.dumps({'headRefOid': head_sha}) if view_rc == 0 else ''
                return self._cp(returncode=view_rc, stdout=stdout, stderr='boom')
            if len(cmd) > 1 and cmd[1] == 'api':
                api_cmds.append(cmd)
                return self._cp(returncode=api_rc, stdout='{}', stderr='boom')
            return self._cp(returncode=1)

        with mock.patch.object(on.subprocess, 'run', side_effect=_run):
            state = on._post_mirror_review_commit_status({'task_id': 't'}, decision)
        return state, api_cmds

    def test_pass_posts_success_to_head_sha(self):
        state, api_cmds = self._run_helper(self._decision('review_pass'),
                                           head_sha='abc123sha')
        self.assertEqual(state, 'success')
        self.assertEqual(len(api_cmds), 1)
        cmd = api_cmds[0]
        # Targets the correct head SHA + owner/repo via gh api statuses.
        self.assertIn(
            'repos/Larry-Yatch/ourliberty-agent-core/statuses/abc123sha', cmd,
        )
        self.assertIn('state=success', cmd)
        self.assertIn('context=mirror-review', cmd)
        self.assertIn('description=Mirror review passed', cmd)

    def test_revision_posts_failure(self):
        state, api_cmds = self._run_helper(self._decision('review_revision'))
        self.assertEqual(state, 'failure')
        self.assertEqual(len(api_cmds), 1)
        self.assertIn('state=failure', api_cmds[0])
        self.assertIn('description=REVIEW_REVISION', api_cmds[0])

    def test_escalate_and_emergency_halt_post_failure(self):
        for mtype, desc in (
            ('review_escalate', 'description=REVIEW_ESCALATE'),
            ('review_emergency_halt', 'description=REVIEW_EMERGENCY_HALT'),
        ):
            state, api_cmds = self._run_helper(self._decision(mtype))
            self.assertEqual(state, 'failure')
            self.assertIn('state=failure', api_cmds[0])
            self.assertIn(desc, api_cmds[0])

    def test_gh_view_failure_tolerated(self):
        # No head SHA -> skip the POST, return None, never raise.
        state, api_cmds = self._run_helper(self._decision('review_pass'),
                                           view_rc=1)
        self.assertIsNone(state)
        self.assertEqual(api_cmds, [])

    def test_gh_api_failure_tolerated(self):
        # Status POST itself fails -> return None, never raise.
        state, api_cmds = self._run_helper(self._decision('review_pass'),
                                           api_rc=1)
        self.assertIsNone(state)
        self.assertEqual(len(api_cmds), 1)

    def test_gh_missing_binary_tolerated(self):
        def _raise(cmd, **kwargs):
            raise FileNotFoundError('gh not on PATH')
        with mock.patch.object(on.subprocess, 'run', side_effect=_raise):
            state = on._post_mirror_review_commit_status(
                {'task_id': 't'}, self._decision('review_pass'),
            )
        self.assertIsNone(state)

    def test_invalid_pr_url_skipped_without_shellout(self):
        called = []

        def _run(cmd, **kwargs):
            called.append(cmd)
            return self._cp(returncode=0)

        with mock.patch.object(on.subprocess, 'run', side_effect=_run):
            state = on._post_mirror_review_commit_status(
                {'task_id': 't'}, self._decision('review_pass', pr_url='not-a-url'),
            )
        self.assertIsNone(state)
        self.assertEqual(called, [])

    def test_unknown_marker_type_is_noop(self):
        state, api_cmds = self._run_helper(self._decision('review_unknown'))
        self.assertIsNone(state)
        self.assertEqual(api_cmds, [])


class MirrorFindingsCommentTest(unittest.TestCase):
    """build-mirror-findings-comment — Contract A (mirror-review-visibility § 4).

    Every non-PASS verdict posts/updates EXACTLY ONE Mirror findings comment on
    the PR; a re-review updates it in place (no duplicate). Drives the real
    `_post_mirror_findings_comment` against an in-memory fake `gh` comment store
    so the create-then-update idempotency is exercised end-to-end without a
    network shell-out.
    """

    def setUp(self):
        # Exercise the real helper (setUpModule installs an inert override).
        self._orig_override = on._POST_FINDINGS_FN_OVERRIDE
        on._POST_FINDINGS_FN_OVERRIDE = None

    def tearDown(self):
        on._POST_FINDINGS_FN_OVERRIDE = self._orig_override

    def _cp(self, *, returncode=0, stdout='', stderr=''):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    @staticmethod
    def _extract_body(cmd):
        for i, a in enumerate(cmd):
            if a == '-f' and i + 1 < len(cmd) and cmd[i + 1].startswith('body='):
                return cmd[i + 1][len('body='):]
        return None

    @staticmethod
    def _endpoint(cmd):
        for a in cmd:
            if isinstance(a, str) and a.startswith('repos/'):
                return a
        return ''

    def _fake_gh(self, store, calls, *, list_rc=0, write_rc=0):
        """Build a stateful fake subprocess.run simulating GitHub PR comments.

        `store` is {comment_id: body}; `calls` records ('list'|'create'|'update').
        """
        next_id = [1000]

        def _run(cmd, **kwargs):
            calls_kind = None
            if '--jq' in cmd and '--paginate' in cmd:
                calls_kind = 'list'
                calls.append(calls_kind)
                if list_rc != 0:
                    return self._cp(returncode=list_rc, stderr='boom')
                out = '\n'.join(
                    json.dumps({'id': cid, 'body': body})
                    for cid, body in store.items()
                )
                return self._cp(returncode=0, stdout=out)
            if '-X' in cmd and 'PATCH' in cmd:
                calls_kind = 'update'
                calls.append(calls_kind)
                if write_rc != 0:
                    return self._cp(returncode=write_rc, stderr='boom')
                cid = int(self._endpoint(cmd).rsplit('/', 1)[1])
                store[cid] = self._extract_body(cmd)
                return self._cp(returncode=0, stdout='{}')
            if self._endpoint(cmd).endswith('/comments'):
                calls_kind = 'create'
                calls.append(calls_kind)
                if write_rc != 0:
                    return self._cp(returncode=write_rc, stderr='boom')
                cid = next_id[0]
                next_id[0] += 1
                store[cid] = self._extract_body(cmd)
                return self._cp(returncode=0, stdout=json.dumps({'id': cid}))
            return self._cp(returncode=1)

        return _run

    def _decision(self, marker_type, pr_url=PR_URL_FIXTURE, **extra):
        payload = {'pr_url': pr_url}
        payload.update(extra)
        return {'marker_type': marker_type, 'payload': payload}

    def _revision_decision(self, desc='add a test', pr_url=PR_URL_FIXTURE):
        return self._decision(
            'review_revision', pr_url=pr_url,
            findings=[{'file': 'scripts/x.py', 'line_range': 'L1-L2',
                       'severity': 'medium', 'description': desc}],
            severity='medium', confidence='high',
        )

    # ---- the § 4 idempotency proof: create then update, exactly one comment ----

    def test_revision_creates_then_updates_single_comment(self):
        store: dict = {}
        calls: list = []
        with mock.patch.object(on.subprocess, 'run',
                               side_effect=self._fake_gh(store, calls)):
            # Round 1 — no existing comment -> create.
            r1 = on._post_mirror_findings_comment(
                {'task_id': 't'}, self._revision_decision('round one finding'),
            )
            # Round 2 (re-review) — anchor found -> update in place.
            r2 = on._post_mirror_findings_comment(
                {'task_id': 't'}, self._revision_decision('round two finding'),
            )
        self.assertEqual(r1, 'created')
        self.assertEqual(r2, 'updated')
        # Exactly one comment on the PR after two review rounds.
        self.assertEqual(len(store), 1)
        # The single comment reflects the LATEST round (updated, not stale).
        body = next(iter(store.values()))
        self.assertIn('round two finding', body)
        self.assertNotIn('round one finding', body)
        self.assertIn(on._MIRROR_FINDINGS_ANCHOR, body)
        # Call shape: list+create on round 1, list+update on round 2.
        self.assertEqual(calls, ['list', 'create', 'list', 'update'])

    def test_escalate_creates_comment(self):
        store: dict = {}
        calls: list = []
        decision = self._decision(
            'review_escalate', reason='spec relies on infra that does not exist',
            severity='high', confidence='high',
        )
        with mock.patch.object(on.subprocess, 'run',
                               side_effect=self._fake_gh(store, calls)):
            result = on._post_mirror_findings_comment({'task_id': 't'}, decision)
        self.assertEqual(result, 'created')
        self.assertEqual(len(store), 1)
        body = next(iter(store.values()))
        self.assertIn('REVIEW_ESCALATE', body)
        self.assertIn('infra that does not exist', body)

    def test_pass_posts_no_comment(self):
        store: dict = {}
        calls: list = []
        with mock.patch.object(on.subprocess, 'run',
                               side_effect=self._fake_gh(store, calls)):
            result = on._post_mirror_findings_comment(
                {'task_id': 't'}, self._decision('review_pass', summary='ok'),
            )
        self.assertIsNone(result)
        self.assertEqual(store, {})
        self.assertEqual(calls, [])

    def test_emergency_halt_posts_no_comment(self):
        store: dict = {}
        calls: list = []
        with mock.patch.object(on.subprocess, 'run',
                               side_effect=self._fake_gh(store, calls)):
            result = on._post_mirror_findings_comment(
                {'task_id': 't'},
                self._decision('review_emergency_halt', reason='creds',
                               evidence='AKIA...'),
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_invalid_pr_url_skipped_without_shellout(self):
        store: dict = {}
        calls: list = []
        with mock.patch.object(on.subprocess, 'run',
                               side_effect=self._fake_gh(store, calls)):
            result = on._post_mirror_findings_comment(
                {'task_id': 't'}, self._revision_decision(pr_url='not-a-url'),
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_gh_write_failure_tolerated(self):
        store: dict = {}
        calls: list = []
        with mock.patch.object(on.subprocess, 'run',
                               side_effect=self._fake_gh(store, calls, write_rc=1)):
            result = on._post_mirror_findings_comment(
                {'task_id': 't'}, self._revision_decision(),
            )
        # POST failed -> None, never raises; nothing persisted.
        self.assertIsNone(result)
        self.assertEqual(store, {})

    def test_gh_missing_binary_tolerated(self):
        def _raise(cmd, **kwargs):
            raise FileNotFoundError('gh not on PATH')
        with mock.patch.object(on.subprocess, 'run', side_effect=_raise):
            result = on._post_mirror_findings_comment(
                {'task_id': 't'}, self._revision_decision(),
            )
        self.assertIsNone(result)

    # ---- pure body-render unit checks ----

    def test_render_revision_body_has_anchor_and_findings(self):
        body = on._render_mirror_findings_comment_body(
            'review_revision',
            {'findings': [{'file': 'a.py', 'line_range': 'L5-L9',
                           'severity': 'low', 'description': 'rename foo'}],
             'severity': 'low', 'confidence': 'high'},
        )
        self.assertTrue(body.startswith(on._MIRROR_FINDINGS_ANCHOR))
        self.assertIn('REVIEW_REVISION', body)
        self.assertIn('a.py', body)
        self.assertIn('rename foo', body)

    def test_render_escalate_body_has_anchor_and_reason(self):
        body = on._render_mirror_findings_comment_body(
            'review_escalate',
            {'reason': 'wrong feature built', 'severity': 'high',
             'confidence': 'high'},
        )
        self.assertTrue(body.startswith(on._MIRROR_FINDINGS_ANCHOR))
        self.assertIn('REVIEW_ESCALATE', body)
        self.assertIn('wrong feature built', body)


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


class AutoMergePushEmitTest(unittest.TestCase):
    """S-4 freshness (spec § S5) — `_auto_merge_pr` push-emits the `auto_merge`
    chain_event at the merge moment so the board reflects the merge in one short
    cycle instead of waiting on the shipper's 30-60s log-tail poll, and does so
    with event_id parity to that poll so the pair dedups (no double row)."""

    PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
    TASK_ID = 'test-task-001'

    def _mock_run(self, *, returncode=0, stdout='', stderr=''):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def test_merged_pushes_auto_merge_event(self):
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on.chain_event_emit, 'emit_event') as m_emit:
            m_run.return_value = self._mock_run(returncode=0)
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'merged')
        m_emit.assert_called_once()
        _, kw = m_emit.call_args
        self.assertEqual(kw['event_type'], 'auto_merge')
        self.assertEqual(kw['agent'], 'forge')
        self.assertEqual(kw['task_id'], self.TASK_ID)
        self.assertEqual(kw['payload']['outcome'], 'merged')
        self.assertEqual(kw['pr_url'], self.PR_URL)
        # id_extra is the full log `rest` the shipper would key on.
        self.assertTrue(kw['id_extra'].startswith('AUTO_MERGE '))
        self.assertIn('outcome=merged', kw['id_extra'])

    def test_already_merged_pushes_auto_merge_event(self):
        merge_proc = self._mock_run(returncode=1, stderr='already merged')
        view_proc = self._mock_run(
            returncode=0, stdout=json.dumps({'state': 'MERGED'}),
        )
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on.chain_event_emit, 'emit_event') as m_emit:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'already_merged')
        m_emit.assert_called_once()
        _, kw = m_emit.call_args
        self.assertEqual(kw['payload']['outcome'], 'already_merged')
        self.assertIn('outcome=already_merged', kw['id_extra'])

    def test_failed_merge_does_not_push(self):
        """A failed merge writes no auto_merge row — the board must not flip a
        card to shipped on a failure."""
        merge_proc = self._mock_run(returncode=1, stderr='not mergeable')
        view_proc = self._mock_run(
            returncode=0, stdout=json.dumps({'state': 'OPEN'}),
        )
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on.chain_event_emit, 'emit_event') as m_emit:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        m_emit.assert_not_called()

    def test_emit_failure_does_not_break_merge(self):
        """daemon-never-wedge: a raising push emit never alters the merge."""
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(
                    on.chain_event_emit, 'emit_event',
                    side_effect=RuntimeError('supabase down')):
            m_run.return_value = self._mock_run(returncode=0)
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'merged')

    def test_event_id_parity_with_shipper(self):
        """The push event_id must equal what the shipper's `parse_log_line`
        derives from the very AUTO_MERGE line this merge writes, so the PK
        absorbs the later poll-source row instead of double-writing."""
        captured: dict[str, str] = {}
        real_log = on.log

        def _capture_log(msg, level='INFO', ts=None):
            if msg.startswith('AUTO_MERGE '):
                captured['msg'] = msg
                captured['ts'] = ts
            return real_log(msg, level, ts)

        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on.chain_event_emit, 'emit_event') as m_emit, \
                mock.patch.object(on, 'log', _capture_log):
            m_run.return_value = self._mock_run(returncode=0)
            on._auto_merge_pr(self.PR_URL, self.TASK_ID)

        # Push side: recompute the event_id from the exact kwargs the helper
        # handed emit_event.
        _, kw = m_emit.call_args
        push_event_id = on.ces.compute_event_id(
            kw['task_id'], kw['event_type'], kw['ts'], extra=kw['id_extra'],
        )

        # Poll side: reconstruct the log line and parse it like the shipper.
        self.assertIsNotNone(captured.get('ts'))
        log_line = (
            f"[{captured['ts']}] [notifier] [INFO] {captured['msg']}"
        )
        shipper_event = on.ces.parse_log_line(log_line)
        self.assertIsNotNone(shipper_event)
        self.assertEqual(shipper_event.event_type, 'auto_merge')
        self.assertEqual(push_event_id, shipper_event.event_id)


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
                'task_id': 'real-resume',
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
                'task_id': 'prod-fail',
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
                'task_id': 'prod-ok',
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

    def _data(self, task_id='real-halt', reply_chat_id=None):
        d = {'task_id': task_id, 'agent': 'mirror'}
        if reply_chat_id is not None:
            d['reply_chat_id'] = reply_chat_id
        return d

    def _payload(self, reason='credentials in diff', evidence='line 42: API_KEY=...',
                 pr_url='https://github.com/owner/repo/pull/1'):
        return {'reason': reason, 'evidence': evidence, 'pr_url': pr_url,
                'task_id': 'real-halt'}

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
        self.assertEqual(env['task_id'], 'real-halt')
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
        self.assertEqual(a['subject'], 'emergency-halt:real-halt')
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
        on._trip_emergency_halt(self._data(task_id='real-A'), self._payload())
        # Remove file so the second trip's idempotent check doesn't skip
        # (the alert path is separate from the halt-file path, but both
        # share the data-flow we're testing).
        on.EMERGENCY_HALT_FLAG.unlink()
        on._trip_emergency_halt(self._data(task_id='real-B'), self._payload())
        alerts = self._read_alerts()
        emergency = [a for a in alerts if a.get('severity') == 'critical']
        subjects = {a.get('subject') for a in emergency}
        self.assertIn('emergency-halt:real-A', subjects)
        self.assertIn('emergency-halt:real-B', subjects)

    def test_same_task_cooldown_suppresses(self):
        """Re-tripping for the SAME task within 10 min — second alert
        suppressed by cooldown bucket (subject keyed on task_id)."""
        on._trip_emergency_halt(self._data(task_id='real-A'), self._payload())
        on.EMERGENCY_HALT_FLAG.unlink()
        on._trip_emergency_halt(self._data(task_id='real-A'), self._payload())
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
        at_cap, current, cap = on._check_cost_budget('real-x', cap_usd=5.0)
        self.assertFalse(at_cap)
        self.assertEqual(current, 0.0)
        self.assertEqual(cap, 5.0)

    def test_below_cap_allows_dispatch(self):
        self._write_costs(
            {'task_id': 'real-x', 'cost_usd': 1.0},
            {'task_id': 'real-x', 'cost_usd': 2.0},
            {'task_id': 'real-other', 'cost_usd': 999.0},  # different task; ignored
        )
        at_cap, current, cap = on._check_cost_budget('real-x', cap_usd=5.0)
        self.assertFalse(at_cap)
        self.assertEqual(current, 3.0)

    def test_at_cap_refuses_dispatch(self):
        self._write_costs(
            {'task_id': 'real-x', 'cost_usd': 3.0},
            {'task_id': 'real-x', 'cost_usd': 2.0},
        )
        at_cap, current, cap = on._check_cost_budget('real-x', cap_usd=5.0)
        self.assertTrue(at_cap)
        self.assertEqual(current, 5.0)

    def test_over_cap_refuses_dispatch(self):
        self._write_costs(
            {'task_id': 'real-x', 'cost_usd': 4.0},
            {'task_id': 'real-x', 'cost_usd': 2.0},
        )
        at_cap, current, cap = on._check_cost_budget('real-x', cap_usd=5.0)
        self.assertTrue(at_cap)
        self.assertEqual(current, 6.0)

    def test_malformed_lines_skipped(self):
        # Mix of valid + malformed (not JSON, wrong shape, missing fields)
        # — gate stays robust.
        with open(on.COSTS_FILE, 'w', encoding='utf-8') as f:
            f.write('{"task_id": "real-x", "cost_usd": 1.0}\n')
            f.write('not json at all\n')
            f.write('{"task_id": "real-x"}\n')  # no cost_usd
            f.write('{}\n')                   # no task_id
            f.write('[1,2,3]\n')              # not a dict
            f.write('{"task_id": "real-x", "cost_usd": 2.0}\n')
            f.write('\n')                     # empty line
        at_cap, current, _ = on._check_cost_budget('real-x', cap_usd=5.0)
        self.assertFalse(at_cap)
        self.assertEqual(current, 3.0)

    def test_negative_cost_ignored(self):
        # Defensive — negative cost_usd is meaningless; skip.
        self._write_costs(
            {'task_id': 'real-x', 'cost_usd': 1.0},
            {'task_id': 'real-x', 'cost_usd': -100.0},
            {'task_id': 'real-x', 'cost_usd': 1.0},
        )
        at_cap, current, _ = on._check_cost_budget('real-x', cap_usd=5.0)
        self.assertEqual(current, 2.0)
        self.assertFalse(at_cap)

    def test_bool_cost_ignored(self):
        # bool is an int subclass — defensive guard.
        self._write_costs(
            {'task_id': 'real-x', 'cost_usd': True},  # treated as 1 by Python
            {'task_id': 'real-x', 'cost_usd': 1.0},
        )
        at_cap, current, _ = on._check_cost_budget('real-x', cap_usd=5.0)
        self.assertEqual(current, 1.0)  # bool skipped, only the 1.0 counts

    def test_enforce_allows_below_cap(self):
        self._write_costs({'task_id': 'real-x', 'cost_usd': 1.0})
        data = {'reply_chat_id': 12345}
        ok = on._enforce_cost_budget('real-x', 'build-phase', data)
        self.assertTrue(ok)

    @mock.patch.object(on, '_load_cost_per_task_cap_usd_from_config', return_value=5.0)
    def test_enforce_refuses_at_cap_and_dms_larry(self, _mock_cap):
        self._write_costs({'task_id': 'real-x', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        ok = on._enforce_cost_budget('real-x', 'build-phase', data)
        self.assertFalse(ok)
        # DM queued
        import larry_alerts as la
        records = [json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines() if line.strip()]
        cost_dms = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms), 1)
        self.assertEqual(cost_dms[0]['chat_id'], 12345)
        self.assertIn('build-phase', cost_dms[0]['message'])
        self.assertIn('cap', cost_dms[0]['message'])

    @mock.patch.object(on, '_load_cost_per_task_cap_usd_from_config', return_value=5.0)
    def test_enforce_no_chat_id_still_refuses(self, _mock_cap):
        """Refusal happens regardless of reply_chat_id; DM is best-effort."""
        self._write_costs({'task_id': 'real-x', 'cost_usd': 20.0})
        ok = on._enforce_cost_budget('real-x', 'build-phase', {})
        self.assertFalse(ok)

    @mock.patch.object(on, '_load_cost_per_task_cap_usd_from_config', return_value=5.0)
    def test_enforce_dedups_same_task_within_daemon_lifetime(self, _mock_cap):
        """First cap-fire DMs Larry; subsequent fires for the same task
        within the same daemon-instance suppress the DM (code-review #4).
        """
        import larry_alerts as la
        self._write_costs({'task_id': 'real-x', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        # First refusal — DM queued.
        on._enforce_cost_budget('real-x', 'build-phase', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms_first = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms_first), 1)
        # Second refusal on the SAME task — suppressed.
        on._enforce_cost_budget('real-x', 'mirror-review', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms_second = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms_second), 1, 'second fire should not queue a duplicate DM')
        # Third refusal on the SAME task — still suppressed.
        on._enforce_cost_budget('real-x', 'revision-to-forge', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms_third = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms_third), 1)

    @mock.patch.object(on, '_load_cost_per_task_cap_usd_from_config', return_value=5.0)
    def test_enforce_dedups_per_task_not_globally(self, _mock_cap):
        """Cap-fire on task A does NOT suppress DM for task B (different
        task — different dedup bucket)."""
        import larry_alerts as la
        self._write_costs({'task_id': 'real-A', 'cost_usd': 20.0})
        self._write_costs({'task_id': 'real-B', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        on._enforce_cost_budget('real-A', 'build-phase', data)
        on._enforce_cost_budget('real-B', 'build-phase', data)
        records = [
            json.loads(line) for line in la.ALERTS_FILE.read_text().splitlines()
            if line.strip()
        ]
        cost_dms = [r for r in records if r.get('intent') == 'cost-budget-exhausted']
        self.assertEqual(len(cost_dms), 2)
        task_ids = {r.get('task_id') for r in cost_dms}
        self.assertEqual(task_ids, {'real-A', 'real-B'})

    @mock.patch.object(on, '_load_cost_per_task_cap_usd_from_config', return_value=5.0)
    def test_enforce_dedups_resets_after_daemon_restart(self, _mock_cap):
        """Simulating a daemon restart (test helper clears the set) —
        the same task can DM again. Models the real production behavior
        where the daemon-lifetime set is cleared on every restart, which
        is intentional: Larry may have raised the cap and a restart
        means the issue might be resolved."""
        import larry_alerts as la
        self._write_costs({'task_id': 'real-x', 'cost_usd': 20.0})
        data = {'reply_chat_id': 12345}
        on._enforce_cost_budget('real-x', 'build-phase', data)
        on._reset_cost_budget_dmed_tasks()  # simulate daemon restart
        on._enforce_cost_budget('real-x', 'mirror-review', data)
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
        # D3.5 5d-prime — bypass serializer gates (this class predates
        # the gates and asserts D3.5 5d behavior; the gates' gh calls
        # aren't mocked here).
        self._orig_skip_serializer = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = True
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
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._orig_skip_serializer
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
        f = self._write_mirror_outbox('real-pass.json', body)
        on.process_outbox(f)
        self.assertEqual(len(self._auto_merge_calls), 1)
        pr_url, task_id = self._auto_merge_calls[0]
        self.assertEqual(pr_url, PR_URL_FIXTURE)
        self.assertEqual(task_id, 'real-rev')  # the marker fixture's task_id

    def test_auto_merge_fires_even_when_verdict_emit_raises(self):
        # check-x-verdict-emission ADDITIVE invariant: the review_pass
        # verdict chain_event is best-effort. If emit_event raises (Supabase
        # outage), the auto-merge / notify path must still complete unchanged.
        def _raise(**_):
            raise RuntimeError('supabase blew up')

        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('real-pass.json', body)
        with mock.patch.object(on.chain_event_emit, 'emit_event', _raise):
            result = on.process_outbox(f)
        # Merge still fired despite the emit blowing up.
        self.assertEqual(len(self._auto_merge_calls), 1)
        self.assertEqual(self._auto_merge_calls[0][0], PR_URL_FIXTURE)
        # And the DM still reflects the merged outcome.
        notifications = [
            r for r in self._read_notifications()
            if r.get('kind') == 'notification'
            and r.get('intent') == 'review-pass'
        ]
        self.assertEqual(len(notifications), 1)
        self.assertIn('Auto-merged + branch deleted', notifications[0]['message'])

    def test_auto_merge_skipped_on_review_revision(self):
        body = self._body_with_chat(_mirror_revision_marker(confidence='high'))
        f = self._write_mirror_outbox('real-rev.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])

    def test_auto_merge_skipped_on_review_escalate(self):
        body = self._body_with_chat(_mirror_escalate_marker())
        f = self._write_mirror_outbox('real-esc.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])

    def test_auto_merge_skipped_on_emergency_halt(self):
        body = self._body_with_chat(_mirror_emergency_marker())
        f = self._write_mirror_outbox('real-halt.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])

    def test_dm_body_reflects_merged_outcome(self):
        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('real-pass.json', body)
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
        f = self._write_mirror_outbox('real-pass.json', body)
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
        f = self._write_mirror_outbox('real-resume.json', body)
        # First pass — normal `merged` outcome.
        on.process_outbox(f)
        # Restore the outbox from archive (simulate crash before archive).
        archived = on.OUTBOXES_ROOT / 'mirror' / '.archive' / 'real-resume.json'
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
        f = self._write_mirror_outbox('real-pass.json', body)
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
        f = self._write_mirror_outbox('real-pass.json', body)
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
        bad_payload = json.dumps({'task_id': 'real-rev', 'pr_url': PR_URL_FIXTURE})
        marker = (
            f'=== REVIEW_PASS ===\n{bad_payload}\n=== END_REVIEW_PASS ==='
        )
        body = self._body_with_chat(marker)
        f = self._write_mirror_outbox('real-rev.json', body)
        on.process_outbox(f)
        self.assertEqual(self._auto_merge_calls, [])

    def test_auto_merge_refused_on_marker_envelope_pr_url_mismatch(self):
        # nervous-system-audit #15 (2026-06-05): the PASS marker names a
        # DIFFERENT PR than the one Mirror was dispatched to review (the
        # envelope's pr_url, set by _dispatch_mirror_review). Refuse the merge
        # — never merge a PR other than the reviewed one — and render a failed
        # outcome so Larry sees the gap.
        body = self._body_with_chat(_mirror_pass_marker())  # marker pr=.../pull/42
        body['pr_url'] = (
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/999'
        )
        f = self._write_mirror_outbox('mismatch.json', body)
        on.process_outbox(f)
        # No merge attempted at all.
        self.assertEqual(self._auto_merge_calls, [])
        notifications = [
            r for r in self._read_notifications()
            if r.get('kind') == 'notification'
            and r.get('intent') == 'review-pass'
        ]
        self.assertEqual(len(notifications), 1)
        self.assertIn('Auto-merge FAILED', notifications[0]['message'])

    def test_auto_merge_proceeds_when_marker_matches_envelope_pr_url(self):
        # Control for #15: marker pr_url == envelope pr_url → merge fires.
        body = self._body_with_chat(_mirror_pass_marker())
        body['pr_url'] = PR_URL_FIXTURE
        f = self._write_mirror_outbox('match.json', body)
        on.process_outbox(f)
        self.assertEqual(len(self._auto_merge_calls), 1)
        self.assertEqual(self._auto_merge_calls[0][0], PR_URL_FIXTURE)

    def test_cosmetic_pr_url_variant_is_not_a_mismatch(self):
        # #15 compares NORMALIZED (owner/repo, number) coords, so an
        # agent-authored marker URL that differs only cosmetically (trailing
        # slash + /files) from the envelope must NOT block the merge.
        body = self._body_with_chat(_mirror_pass_marker())  # marker pr=.../pull/42
        body['pr_url'] = PR_URL_FIXTURE + '/files'
        f = self._write_mirror_outbox('cosmetic.json', body)
        on.process_outbox(f)
        self.assertEqual(len(self._auto_merge_calls), 1)
        self.assertEqual(self._auto_merge_calls[0][0], PR_URL_FIXTURE)

    def test_legacy_envelope_without_pr_url_still_merges(self):
        # Graceful degradation: a chain that never propagated an envelope
        # pr_url (legacy) falls through the #15 gate to the existing
        # shape/existence validators — merge still fires on the marker pr_url.
        body = self._body_with_chat(_mirror_pass_marker())
        self.assertNotIn('pr_url', body)  # _mirror_outbox_body sets none
        f = self._write_mirror_outbox('legacy.json', body)
        on.process_outbox(f)
        self.assertEqual(len(self._auto_merge_calls), 1)

    def test_auto_merge_fires_even_when_back_leg_notify_fails(self):
        # nervous-system-audit #12 (2026-06-05): a back-leg notify
        # DispatchRejected must NOT dead-end the outbox before the auto-merge
        # block. Pre-fix, the merge never ran and no AUTO_MERGE log was
        # written, so heal_pr_auto_merge had nothing to retry.
        body = self._body_with_chat(_mirror_pass_marker())
        f = self._write_mirror_outbox('notify-fail.json', body)

        def _raise_notify(*_a, **_k):
            raise swi.DispatchRejected('inbox closed for maintenance')

        with mock.patch.object(
            on.safe_write_inbox, 'safe_write_inbox', _raise_notify,
        ):
            result = on.process_outbox(f)
        # Status preserved for telemetry, but the merge STILL fired.
        self.assertEqual(result, 'notify-failed')
        self.assertEqual(len(self._auto_merge_calls), 1)
        self.assertEqual(self._auto_merge_calls[0][0], PR_URL_FIXTURE)


class ReviewPassDmAwaitsMergeOutcomeTest(unittest.TestCase):
    """fix-review-pass-dm-await-merge-outcome (2026-05-26) — Larry's
    closing DM must reflect the ACTUAL AUTO_MERGE outcome, not the
    optimistic 'fired automatically' state from before merge resolution.

    Key invariants:
      * One DM per resolution moment. (Two for serializer-queued: queued
        + final outcome — that's intentional.)
      * deferred_unknown → no DM until the queue sweep retries and
        resolves.
      * held_conflict → exactly ONE DM (the rebase recipe from
        _dm_larry_rebase_needed). The held_conflict variant from
        _maybe_dm_larry is suppressed.
      * Mirror review summary appears in every closing DM body.

    Mocks the gh-shell-out helpers individually so the gate flow runs
    end-to-end. Does NOT set _AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST (which
    short-circuits gates) — the gates' DM-timing is what we're testing.
    """

    PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
    REPO_COORDS = 'Larry-Yatch/ourliberty-agent-core'
    SUMMARY = 'AC coverage clean.'
    TASK_ID = 'real-rev'
    CHAT_ID = 98765

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT',
            'BLACKBOARD', 'LOG_FILE', 'DEAD_LETTER_STATE',
            'EMERGENCY_HALT_FLAG', 'AUTO_MERGE_QUEUE_FILE',
        ]:
            self._originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        on.AUTO_MERGE_QUEUE_FILE = self._root / 'state' / 'auto-merge-queue.json'
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

        # Per-test mutable state for the merge fn override + call log.
        self._auto_merge_calls: list[tuple[str, str]] = []
        self._merge_outcome_override = {
            'merge_outcome': 'merged',
            'merge_reason': 'squash-merged + branch deleted',
            'pr_number': 42,
            'repo_coords': self.REPO_COORDS,
        }
        # Capture the order of all relevant interleavings so the
        # one-DM-after-merge invariant can be asserted explicitly.
        self._call_order: list[str] = []

        def _override(pr_url, task_id):
            self._auto_merge_calls.append((pr_url, task_id))
            self._call_order.append('auto_merge_pr')
            return dict(self._merge_outcome_override)
        self._orig_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = _override
        # DO NOT bypass the serializer — these tests exercise the gate
        # flow. (Compare: MirrorMarkerRoutingAutoMergeTest bypasses it.)
        self._orig_skip_serializer = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = False
        # Reset module-level serializer state between tests.
        on._reset_auto_merge_queue_state()

        # Default mergeable status — overridden per test.
        self._mergeable_status_responses: list[str] = ['mergeable']
        # Default overlap blocker — None unless test sets it.
        self._overlap_blocker: object = None
        # Default open-state recheck for blockers — True (still open).
        self._is_open_responses: dict[int, bool | None] = {}
        # Default changed-files response — empty.
        self._changed_files_responses: dict[int, list[str]] = {}

        def _mergeable_stub(repo, pr):
            self._call_order.append(f'mergeable_check(pr={pr})')
            if not self._mergeable_status_responses:
                return 'mergeable'
            if len(self._mergeable_status_responses) == 1:
                return self._mergeable_status_responses[0]
            return self._mergeable_status_responses.pop(0)

        def _overlap_stub(pr_number, repo_coords, changed_files):
            self._call_order.append(f'overlap_check(pr={pr_number})')
            return self._overlap_blocker

        def _is_open_stub(repo, pr):
            return self._is_open_responses.get(pr, True)

        def _changed_files_stub(repo, pr):
            return self._changed_files_responses.get(pr, [])

        # fix-auto-merge-freshness-revalidation — the release path re-validates
        # held merges via _gh_pr_merge_freshness. Stub it with a STABLE base
        # (base_sha never moves between hold-time capture and release), so
        # base_moved == False and the regression gate is never consulted —
        # these tests' release behavior is identical to pre-feature. (The
        # freshness gate's block/defer logic is covered hermetically in
        # test_auto_merge_serializer.StaleApprovalRevalidationTest.)
        self._freshness_base_sha = 'base-stable-0000'
        # Release-path mergeable is its OWN dial, decoupled from
        # _mergeable_status_responses (which _mergeable_stub pops): Gate 2 is
        # skipped on the release path, so reading the popped list here would be
        # an accidental ordering coupling. Tests wanting a specific
        # release-time mergeable set _freshness_mergeable directly.
        self._freshness_mergeable = 'mergeable'

        def _freshness_stub(repo, pr):
            return {
                'mergeable': self._freshness_mergeable,
                'merge_state': 'CLEAN',
                'base_sha': self._freshness_base_sha,
                'head_sha': f'head{pr}',
            }

        self._patches = [
            mock.patch.object(on, '_gh_pr_mergeable_status', _mergeable_stub),
            mock.patch.object(on, '_find_overlap_blocker', _overlap_stub),
            mock.patch.object(on, '_gh_pr_is_open', _is_open_stub),
            mock.patch.object(on, '_gh_pr_changed_files', _changed_files_stub),
            mock.patch.object(on, '_gh_pr_merge_freshness', _freshness_stub),
        ]
        for p in self._patches:
            p.start()
        on.ensure_dirs()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        on._AUTO_MERGE_FN_OVERRIDE = self._orig_override
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._orig_skip_serializer
        on._reset_auto_merge_queue_state()
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    # ---------------- helpers ----------------

    def _write_mirror_outbox(self, name, body):
        outbox_dir = on.OUTBOXES_ROOT / 'mirror'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        f.write_text(json.dumps(body))
        return f

    def _pass_body(self, chat_id=None, changed_files=None):
        body = _mirror_outbox_body(
            _mirror_pass_marker(task_id=self.TASK_ID, summary=self.SUMMARY)
        )
        body['reply_chat_id'] = chat_id if chat_id is not None else self.CHAT_ID
        if changed_files is not None:
            body['changed_files'] = changed_files
        return body

    def _review_pass_dms(self):
        import larry_alerts as la
        if not la.ALERTS_FILE.exists():
            return []
        return [
            json.loads(line)
            for line in la.ALERTS_FILE.read_text().splitlines() if line.strip()
            if 'review-pass' in line or 'merge_conflict' in line
        ]

    def _notifications_with_intent(self, intent):
        import larry_alerts as la
        if not la.ALERTS_FILE.exists():
            return []
        return [
            r for r in (
                json.loads(line)
                for line in la.ALERTS_FILE.read_text().splitlines()
                if line.strip()
            )
            if r.get('kind') == 'notification' and r.get('intent') == intent
        ]

    # ---------------- cases ----------------

    def test_clean_mergeable_one_dm_after_merge(self):
        """MERGEABLE on first check → merge fires → ONE DM with `merged` body.

        Call-order assertion: no DM is queued before _auto_merge_pr is
        called. The DM contains the Mirror summary + 'Auto-merged' marker.
        """
        self._mergeable_status_responses = ['mergeable']
        f = self._write_mirror_outbox(
            'clean-pass.json',
            self._pass_body(changed_files=['scripts/foo.py']),
        )
        on.process_outbox(f)

        dms = self._notifications_with_intent('review-pass')
        self.assertEqual(len(dms), 1)
        body = dms[0]['message']
        self.assertIn('Auto-merged', body)
        self.assertIn(self.SUMMARY, body)
        self.assertIn(self.PR_URL, body)
        # _auto_merge_pr fires before any DM queue write — verify via
        # the captured call_order (mergeable_check → auto_merge_pr).
        self.assertIn('auto_merge_pr', self._call_order)
        merge_idx = self._call_order.index('auto_merge_pr')
        mergeable_idx = self._call_order.index('mergeable_check(pr=42)')
        self.assertLess(mergeable_idx, merge_idx)
        self.assertEqual(len(self._auto_merge_calls), 1)

    def test_deferred_unknown_first_then_merged_one_dm_total(self):
        """UNKNOWN on first attempt → NO DM yet → sweep retries with
        second_attempt_on_unknown=True → mergeable → merge → ONE DM.

        This is the 2026-05-26 bug class: the deferred path produced an
        immediate DM with the placeholder 'fired automatically' phrasing
        and the eventual outcome never reached Larry. After the fix, the
        deferred path is silent and the retry fires the real DM.
        """
        self._mergeable_status_responses = ['unknown', 'mergeable']
        f = self._write_mirror_outbox(
            'deferred-pass.json',
            self._pass_body(changed_files=['scripts/foo.py']),
        )
        on.process_outbox(f)
        # No DM after the deferred attempt.
        self.assertEqual(self._notifications_with_intent('review-pass'), [])
        # Queue entry exists for the deferred PR.
        entries = on._load_auto_merge_queue()
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].get('blocker_pr_number'))
        self.assertEqual(entries[0].get('unknown_attempts'), 1)
        # Sweep — retry path.
        on._auto_merge_queue_sweep()
        dms = self._notifications_with_intent('review-pass')
        self.assertEqual(len(dms), 1)
        self.assertIn('Auto-merged', dms[0]['message'])
        self.assertIn(self.SUMMARY, dms[0]['message'])
        self.assertEqual(len(self._auto_merge_calls), 1)

    def test_conflicting_one_dm_with_rebase_recipe(self):
        """CONFLICTING on first check → _auto_merge_pr NOT called → ONE
        DM (rebase recipe) with the Mirror summary inline."""
        self._mergeable_status_responses = ['conflicting']
        f = self._write_mirror_outbox(
            'conflict-pass.json',
            self._pass_body(changed_files=['scripts/foo.py']),
        )
        on.process_outbox(f)

        rebase_dms = self._notifications_with_intent('merge_conflict_manual_rebase')
        pass_dms = self._notifications_with_intent('review-pass')
        # ONE rebase DM, zero review-pass DMs (the held_conflict variant
        # is suppressed — _dm_larry_rebase_needed is canonical).
        self.assertEqual(len(rebase_dms), 1)
        self.assertEqual(len(pass_dms), 0)
        body = rebase_dms[0]['message']
        self.assertIn('Auto-merge BLOCKED', body)
        self.assertIn('Rebase manually', body)
        self.assertIn(self.SUMMARY, body)
        # _auto_merge_pr NOT called for CONFLICTING.
        self.assertEqual(self._auto_merge_calls, [])

    def test_serializer_queued_one_dm_at_queue_time(self):
        """Serializer gate 1 returns blocker → ONE DM with 'HELD behind
        PR #Y' + overlap files; _auto_merge_pr NOT called yet."""
        self._overlap_blocker = 99
        f = self._write_mirror_outbox(
            'queued-pass.json',
            self._pass_body(changed_files=['scripts/foo.py', 'scripts/bar.py']),
        )
        on.process_outbox(f)

        dms = self._notifications_with_intent('review-pass')
        self.assertEqual(len(dms), 1)
        body = dms[0]['message']
        self.assertIn('HELD behind PR #99', body)
        self.assertIn('scripts/foo.py', body)
        self.assertIn('scripts/bar.py', body)
        self.assertIn(self.SUMMARY, body)
        # _auto_merge_pr NOT fired yet — gate 1 short-circuited.
        self.assertEqual(self._auto_merge_calls, [])
        # Queue entry exists with blocker recorded.
        entries = on._load_auto_merge_queue()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get('blocker_pr_number'), 99)

    def test_serializer_release_two_dms_total(self):
        """Held → blocker resolves → release path runs gates again →
        merges → SECOND DM with `merged` body. Two DMs total for the
        held PR (queued + outcome) — the intentional two-event story.
        """
        # Step 1: PR-42 held behind PR-99.
        self._overlap_blocker = 99
        f = self._write_mirror_outbox(
            'queued-pass.json',
            self._pass_body(changed_files=['scripts/foo.py']),
        )
        on.process_outbox(f)

        first_dms = self._notifications_with_intent('review-pass')
        self.assertEqual(len(first_dms), 1)
        self.assertIn('HELD behind PR #99', first_dms[0]['message'])

        # Step 2: blocker resolves. Set up next gate-pass to be clean.
        self._overlap_blocker = None
        self._mergeable_status_responses = ['mergeable']

        on._queue_release(99, self.REPO_COORDS)

        dms = self._notifications_with_intent('review-pass')
        self.assertEqual(len(dms), 2)
        # First DM = queued-behind; second DM = merged outcome.
        self.assertIn('HELD behind PR #99', dms[0]['message'])
        self.assertIn('Auto-merged', dms[1]['message'])
        self.assertEqual(len(self._auto_merge_calls), 1)

    def test_auto_merge_failed_one_dm_with_manual_recipe(self):
        """MERGEABLE → merge fires but fails (e.g. conflict with main on
        actual merge attempt) → ONE DM with `failed` body + manual
        fallback command."""
        self._mergeable_status_responses = ['mergeable']
        self._merge_outcome_override = {
            'merge_outcome': 'failed',
            'merge_reason': 'not mergeable: merge commit cannot be cleanly created',
            'pr_number': 42,
            'repo_coords': self.REPO_COORDS,
        }
        f = self._write_mirror_outbox(
            'failed-pass.json',
            self._pass_body(changed_files=['scripts/foo.py']),
        )
        on.process_outbox(f)

        dms = self._notifications_with_intent('review-pass')
        self.assertEqual(len(dms), 1)
        body = dms[0]['message']
        self.assertIn('Auto-merge FAILED', body)
        self.assertIn('not mergeable', body)
        # Manual-fallback command rendered with concrete PR + repo.
        self.assertIn(
            'gh pr merge 42 --repo Larry-Yatch/ourliberty-agent-core '
            '--squash --delete-branch',
            body,
        )
        self.assertIn(self.SUMMARY, body)

    def test_already_merged_one_dm_with_success_body(self):
        """already_merged (resume-after-crash) → ONE DM with success body
        (identical to `merged` per the variant map)."""
        self._mergeable_status_responses = ['mergeable']
        self._merge_outcome_override = {
            'merge_outcome': 'already_merged',
            'merge_reason': 'PR was already merged (resume from prior dispatch)',
            'pr_number': 42,
            'repo_coords': self.REPO_COORDS,
        }
        f = self._write_mirror_outbox(
            'resumed-pass.json',
            self._pass_body(changed_files=['scripts/foo.py']),
        )
        on.process_outbox(f)

        dms = self._notifications_with_intent('review-pass')
        self.assertEqual(len(dms), 1)
        body = dms[0]['message']
        self.assertIn('Auto-merged', body)
        self.assertNotIn('FAILED', body)
        self.assertIn(self.SUMMARY, body)

    # ---- watchdog merged/closed suppression gate (Pass 3) ----
    def _seed_stale_entry(self, pr_number):
        """Queue one entry whose queued_at is far past any watchdog
        threshold, so Pass 3's age check always passes."""
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        entry = {
            'pr_number': pr_number,
            'repo': self.REPO_COORDS,
            'pr_url': f'{self.PR_URL.rsplit("/", 1)[0]}/{pr_number}',
            'task_id': 'stale-watchdog',
            'summary': self.SUMMARY,
            'queued_at': old,
            'reply_chat_id': self.CHAT_ID,
        }
        on._save_auto_merge_queue([entry])

    def test_watchdog_merged_entry_suppressed_and_removed(self):
        """A stale-aged entry whose OWN PR is MERGED/CLOSED fires NO
        auto_merge_queue_stale DM and is dropped from the queue — a merged
        PR is not stale."""
        self._seed_stale_entry(883)
        # PR #883 resolves MERGED/CLOSED (is_open is False).
        self._is_open_responses[883] = False
        with mock.patch.object(on, '_dm_larry_queue_stale') as dm:
            on._auto_merge_queue_sweep()
            self.assertEqual(dm.call_count, 0)
        # Entry removed; queue empty.
        self.assertEqual(on._load_auto_merge_queue(), [])

    def test_watchdog_open_entry_still_dms_once_and_retained(self):
        """A stale-aged entry whose OWN PR is still OPEN fires exactly one
        watchdog DM (existing one-shot behavior) and is retained."""
        self._seed_stale_entry(884)
        # PR #884 is genuinely OPEN (default stub → True).
        self._is_open_responses[884] = True
        with mock.patch.object(on, '_dm_larry_queue_stale') as dm:
            on._auto_merge_queue_sweep()
            self.assertEqual(dm.call_count, 1)
        remaining = on._load_auto_merge_queue()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].get('pr_number'), 884)
        self.assertTrue(remaining[0].get('watchdog_dm_sent'))


class ReviewPassNotifyGitHubTruthTest(unittest.TestCase):
    """false-success-notify-fix (2026-06-11) — the Mirror REVIEW_PASS
    inter-agent notify to Beacon must report the GitHub-confirmed merge
    state, NOT an optimistic "auto-merge fired" claim.

    Incident: Mirror approved PR #455; auto-merge was HELD behind PR #454
    (overlapping files) so the PR stayed OPEN — but the notify hardcoded
    "Auto-merge has fired automatically" and Beacon reported a merge that
    never happened, sending the operator chasing it. Ground truth
    (`gh pr view 455 --json state`) was state=OPEN, mergedAt=null.

    These run process_outbox end-to-end with the gh shell-outs mocked
    (`gh pr view --json state` => OPEN; the serializer gates decide held vs
    merged) and assert the BEACON NOTIFY prompt — not just Larry's DM —
    reflects reality. The serializer is NOT bypassed; the gate flow is what
    produces the outcome. No real gh, no writes to ~/agents.
    """

    PR_URL = PR_URL_FIXTURE  # .../pull/42
    SUMMARY = 'All 5 ACs met; regression gate clean.'
    TASK_ID = 'real-rev'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT',
            'BLACKBOARD', 'LOG_FILE', 'DEAD_LETTER_STATE',
            'EMERGENCY_HALT_FLAG', 'AUTO_MERGE_QUEUE_FILE',
        ]:
            self._originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        on.AUTO_MERGE_QUEUE_FILE = self._root / 'state' / 'auto-merge-queue.json'
        import larry_alerts as la
        self._la_originals = {
            k: getattr(la, k)
            for k in ('AGENTS_ROOT', 'ALERTS_FILE', 'COOLDOWN_ROOT', 'OFFSET_FILE')
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        self._swi_originals = {
            k: getattr(swi, k)
            for k in ('AGENTS_ROOT', 'INBOXES_ROOT', 'ROUTING_EVENTS_LOG')
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'
        self._rv_root = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / 'repo'
        rv.invalidate_cache()

        # Gate flow runs for real; only the gh shell-outs are mocked. NOT
        # bypassed (would skip the gates that decide held vs merged).
        self._orig_skip_serializer = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = False
        # No FN override → the real existence-state check runs (mocked below
        # to state=OPEN), exercising the post-fix merge-before-notify path.
        self._orig_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = None
        on._reset_auto_merge_queue_state()

        # Configurable gh-mock state (defaults overridden per test).
        self._pr_state = 'OPEN'        # gh pr view --json state
        self._overlap_blocker = None   # gate 1 serializer blocker PR#
        self._mergeable = 'mergeable'  # gate 2 mergeable status
        self._merge_result = {
            'merge_outcome': 'merged',
            'merge_reason': 'squash-merged + branch deleted',
            'pr_number': 42,
            'repo_coords': 'Larry-Yatch/ourliberty-agent-core',
        }
        self._auto_merge_calls: list[tuple] = []

        def _existence_stub(repo, pr):
            return self._pr_state, 'mocked gh pr view --json state'

        def _overlap_stub(pr_number, repo_coords, changed_files):
            return self._overlap_blocker

        def _mergeable_stub(repo, pr):
            return self._mergeable

        def _changed_files_stub(repo, pr):
            return ['scripts/foo.py']

        def _auto_merge_stub(pr_url, task_id):
            self._auto_merge_calls.append((pr_url, task_id))
            return dict(self._merge_result)

        self._patches = [
            mock.patch.object(on, '_pr_url_existence_state', _existence_stub),
            mock.patch.object(on, '_find_overlap_blocker', _overlap_stub),
            mock.patch.object(on, '_gh_pr_mergeable_status', _mergeable_stub),
            mock.patch.object(on, '_gh_pr_changed_files', _changed_files_stub),
            mock.patch.object(on, '_auto_merge_pr', _auto_merge_stub),
            # Isolate from worktree/sequence side-effects of a successful merge.
            mock.patch.object(on, '_teardown_worktrees_for_task', lambda **k: None),
            mock.patch.object(on, '_signal_sequence_step_merged', lambda **k: None),
        ]
        for p in self._patches:
            p.start()
        on.ensure_dirs()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        on._AUTO_MERGE_FN_OVERRIDE = self._orig_override
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._orig_skip_serializer
        on._reset_auto_merge_queue_state()
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_pass_outbox(self, changed_files=('scripts/foo.py',)):
        body = _mirror_outbox_body(
            _mirror_pass_marker(task_id=self.TASK_ID, summary=self.SUMMARY),
        )
        body['changed_files'] = list(changed_files)
        outbox_dir = on.OUTBOXES_ROOT / 'mirror'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / 'pass.json'
        f.write_text(json.dumps(body))
        return f

    def _beacon_notify(self):
        notifies = list((on.INBOXES_ROOT / 'beacon').glob('notify-*.json'))
        self.assertEqual(
            len(notifies), 1, f'expected 1 beacon notify, got {len(notifies)}',
        )
        return json.loads(notifies[0].read_text())

    def _review_pass_dm_count(self):
        import larry_alerts as la
        if not la.ALERTS_FILE.exists():
            return 0
        return sum(
            1
            for line in la.ALERTS_FILE.read_text().splitlines() if line.strip()
            if json.loads(line).get('kind') == 'notification'
            and json.loads(line).get('intent') == 'review-pass'
        )

    # ---- THE INCIDENT: held behind a blocker, PR still OPEN ----
    def test_held_behind_blocker_notify_does_not_claim_merged(self):
        # PR #455-shape: Mirror PASS, gh pr view => OPEN, auto-merge HELD
        # behind an overlapping PR (#454). The notify MUST NOT say the merge
        # fired/merged; it must say queued-behind-#454 + the PR is NOT merged.
        self._pr_state = 'OPEN'
        self._overlap_blocker = 454
        f = self._write_pass_outbox()
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        notify = self._beacon_notify()
        self.assertEqual(notify['intent'], 'review-pass')
        prompt = notify['prompt']
        # The bug: the old template hardcoded "Auto-merge has fired
        # automatically" regardless of the real outcome.
        self.assertNotIn('has fired', prompt)
        self.assertNotIn('Auto-merge fired', prompt)
        self.assertNotIn('now MERGED', prompt)
        # Truthful: queued behind #454, NOT merged.
        self.assertIn('QUEUED behind PR #454', prompt)
        self.assertIn('NOT merged', prompt)
        self.assertIn('APPROVED', prompt)
        # The merge shell-out never fired (gate 1 short-circuited).
        self.assertEqual(self._auto_merge_calls, [])

    # ---- positive control: a real merge => notify says MERGED ----
    def test_real_merge_notify_says_merged(self):
        self._pr_state = 'OPEN'
        self._overlap_blocker = None
        self._mergeable = 'mergeable'
        f = self._write_pass_outbox()
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        prompt = self._beacon_notify()['prompt']
        self.assertIn('now MERGED', prompt)
        self.assertNotIn('QUEUED', prompt)
        self.assertNotIn('NOT merged', prompt)
        # The merge actually fired exactly once.
        self.assertEqual(len(self._auto_merge_calls), 1)

    # ---- conflict: notify says blocked, not merged ----
    def test_conflict_notify_does_not_claim_merged(self):
        self._pr_state = 'OPEN'
        self._overlap_blocker = None
        self._mergeable = 'conflicting'
        f = self._write_pass_outbox()
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        prompt = self._beacon_notify()['prompt']
        self.assertNotIn('has fired', prompt)
        self.assertNotIn('now MERGED', prompt)
        self.assertIn('BLOCKED', prompt)
        self.assertIn('NOT merged', prompt)
        # gate 2 short-circuited before any merge shell-out.
        self.assertEqual(self._auto_merge_calls, [])

    # ---- already-terminal skip: still OPEN-state mismatch, no false merge ----
    def test_already_merged_resume_notify_says_merged_and_skips(self):
        # Resume after crash: gh pr view => MERGED. The merge step skips
        # (no re-merge), the notify truthfully says already-MERGED, and the
        # outbox result is 'auto-merge-skipped' (DM suppressed, like before).
        self._pr_state = 'MERGED'
        f = self._write_pass_outbox()
        result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        prompt = self._beacon_notify()['prompt']
        self.assertIn('already MERGED', prompt)
        self.assertNotIn('has fired', prompt)
        # No re-merge shell-out.
        self.assertEqual(self._auto_merge_calls, [])
        # The closing DM is suppressed for a skip (pre-fix behavior): the
        # truthful notify already went out; no duplicate Larry DM.
        self.assertEqual(self._review_pass_dm_count(), 0)

    def test_deferred_unknown_notify_does_not_claim_merged(self):
        # gh mergeable=UNKNOWN on first attempt → merge deferred to the queue
        # sweep, PR stays OPEN. The notify must say PENDING / NOT merged, never
        # fired/merged; the closing DM is suppressed until the sweep resolves.
        self._pr_state = 'OPEN'
        self._overlap_blocker = None
        self._mergeable = 'unknown'
        f = self._write_pass_outbox()
        result = on.process_outbox(f)
        self.assertEqual(result, 'notified-marker')
        prompt = self._beacon_notify()['prompt']
        self.assertNotIn('has fired', prompt)
        self.assertNotIn('now MERGED', prompt)
        self.assertIn('PENDING', prompt)
        self.assertIn('NOT merged', prompt)
        # No merge shell-out; deferred outcome suppresses the closing DM.
        self.assertEqual(self._auto_merge_calls, [])
        self.assertEqual(self._review_pass_dm_count(), 0)

    def test_render_merge_status_line_is_github_truth_gated(self):
        # Unit-level guard on the renderer: only confirmed merges say MERGED;
        # every held/failed/missing outcome says NOT merged / requested.
        r = on._render_review_pass_merge_status_line
        self.assertIn('MERGED', r({'merge_outcome': 'merged'}))
        self.assertIn('MERGED', r({'merge_outcome': 'already_merged'}))
        held = r({'merge_outcome': 'held_for_blocker',
                  'blocker_pr_number': 7, 'overlap_files': 'a.py'})
        self.assertIn('QUEUED behind PR #7', held)
        self.assertNotIn('now MERGED', held)
        for oc in ('held_conflict', 'held_fail_closed', 'deferred_unknown', 'failed'):
            line = r({'merge_outcome': oc, 'merge_reason': 'x'})
            self.assertNotIn('now MERGED', line, oc)
            self.assertNotIn('has fired', line, oc)
        # Missing/unknown never asserts success.
        none_line = r(None)
        self.assertIn('REQUESTED', none_line)
        self.assertNotIn('now MERGED', none_line)


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
            f.write(json.dumps({'task_id': 'real-over', 'cost_usd': 99.0}) + '\n')

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
            'task_id': 'real-over',
            'claude_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'feature/x',
            'reply_chat_id': 555,
        }
        on._dispatch_build_phase(data)
        self.assertEqual(self._forge_inbox_files(), [])

    def test_mirror_review_refused_at_cap(self):
        data = {
            'task_id': 'real-over',
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
            'task_id': 'real-over',
            'forge_build_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 555,
        }
        decision = {'payload': {'findings': []}}
        on._dispatch_revision_to_forge(data, decision)
        self.assertEqual(self._forge_inbox_files(), [])

    def test_review_rerun_refused_at_cap(self):
        data = {
            'task_id': 'real-over',
            'target_repo': 'ourliberty-agent-core',
            'pr_url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1',
            'reply_chat_id': 555,
        }
        on._dispatch_mirror_review_rerun(data, round_num=1, summary='fix x')
        self.assertEqual(self._mirror_inbox_files(), [])

    def test_cost_budget_dm_queued_on_refusal(self):
        import larry_alerts as la
        data = {
            'task_id': 'real-over',
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
            'task_id': 'real-clean',
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
        # Pre-load $99 onto real-already so cost would fire if reached.
        with open(on.COSTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'task_id': 'real-already', 'cost_usd': 99.0}) + '\n')
        # Pre-stage the build dispatch as already-archived (simulates
        # the daemon having processed this outbox + dispatched + archived
        # in a prior run; now re-processing on resume).
        (on.INBOXES_ROOT / 'forge' / '.archive').mkdir(parents=True, exist_ok=True)
        (on.INBOXES_ROOT / 'forge' / '.archive' / 'build-real-already.json').write_text(
            json.dumps({'task_id': 'real-already', 'prompt': 'noop'}),
        )
        data = {
            'task_id': 'real-already',
            'claude_session_id': 'session-abc',
            'target_repo': 'ourliberty-agent-core',
            'reply_chat_id': 555,
        }
        on._dispatch_build_phase(data)
        # Idempotency check fires; cost gate is never reached.
        # No new inbox file (the existing .archive entry blocks).
        new_files = [
            p for p in (on.INBOXES_ROOT / 'forge').glob('*.json')
            if p.name != 'build-real-already.json'
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
            'payload': {'task_id': 'real-x'},
            'intent_kwargs': {
                'task_id': 'real-x',
                'current_usd': '6.50',
                'cap_usd': '5.00',
                'dispatch_label': 'mirror-review-rerun',
            },
        }
        body = on._render_dm_message('cost-budget-exhausted', decision)
        self.assertIsNotNone(body)
        self.assertIn('real-x', body)
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
            'payload': {'task_id': 'real-x'},
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
        # D3.5 5d-prime — bypass the serializer gates in this test class.
        # The gates' `gh pr view --json mergeable` and `gh pr list` calls
        # aren't mocked here; the bypass preserves the D3.5 5d contract
        # (merge-outcome rendering via _AUTO_MERGE_FN_OVERRIDE).
        # Serializer-specific tests in test_auto_merge_serializer.py mock
        # subprocess.run end-to-end and exercise the gates directly.
        self._original_skip_serializer = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = True
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
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._original_skip_serializer
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
        f = self._write_mirror_outbox('real-direct.json', body)
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
        f = self._write_mirror_outbox('real-no-chat.json', body)
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
            branch='forge/real-rev',
            pr_url=PR_URL_FIXTURE,
            revision_count=0,
            max_revisions=3,
        )
        f = self._write_mirror_outbox('real-rev.json', body)
        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')
        self.assertEqual(self._auto_merge_calls, [])
        # Revision dispatched to Forge.
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-rev-*.json')
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
        f = self._write_mirror_outbox('real-esc.json', body)
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
        f = self._write_mirror_outbox('real-beacon.json', body)
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
        # phase=None because a Larry-driven chat dispatch carries no
        # phase=='review' envelope — so the marker-discipline gate
        # (fix-mirror-verdict-marker-gate-001) does NOT fire here.
        body = _mirror_outbox_body(
            result='Reviewed in chat mode, no marker emitted.',
            source='larry',
            reply_chat_id=12345,
            phase=None,
        )
        f = self._write_mirror_outbox('real-chat.json', body)
        result = on.process_outbox(f)
        # Default path archives non-agent-source outboxes; no marker = no
        # larry_direct branch.
        self.assertIn(result, ('archived-no-notify', 'notified'))
        self.assertEqual(self._auto_merge_calls, [])


class HarvestedVerdictCrossModuleTest(unittest.TestCase):
    """Cross-module contract for harvest-verdict-before-reap (PR #768 / B1).

    heal_wedged_review_sessions._deliver_mirror_verdict writes a SYNTHESIZED
    mirror outbox before Case-1 reaps a hung-but-finished Mirror review. The
    healer's own tests mock the deliver fn, so they prove the healer's
    branching but NOT that the outbox it writes is actually CONSUMED by
    outbox_notifier.process_outbox -> auto-merge. The pre-fix synthesized
    envelope used source='heal-wedged-review-sessions:harvest-before-reap',
    which _primary_agent_id() maps to None and which is not larry_direct, so
    process_outbox took the `(target_agent is None ...) and not larry_direct`
    archive-no-notify early return BEFORE _run_review_pass_auto_merge — the PR
    never merged and the worktree was still removed (the 760/720 loss).

    These tests run the REAL _deliver_mirror_verdict (to build the exact dict
    it ships) and the REAL process_outbox on its output, mocking only the true
    external boundaries: the merge subprocess (via _AUTO_MERGE_FN_OVERRIDE),
    the commit-status / findings-comment gh POSTs (module-level inert
    overrides), and the healer's outbox-dir + Larry-chat-id resolvers.
    """

    PR_CWD = '/home/larry/agent-worktrees/wt-mirror-pr-ourliberty-agent-core-760'
    # The task_id _deliver_mirror_verdict reconstructs from PR_CWD, and the PR
    # url that worktree maps to. The marker payload must carry these so the
    # classifier's marker-vs-envelope task_id check passes and auto-merge has
    # a pr_url.
    HARVEST_TASK_ID = 'pr-ourliberty-agent-core-760'
    HARVEST_PR_URL = (
        'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/760'
    )

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
                'pr_number': 760,
                'repo_coords': 'Larry-Yatch/ourliberty-agent-core',
            }

        self._original_auto_merge_override = on._AUTO_MERGE_FN_OVERRIDE
        on._AUTO_MERGE_FN_OVERRIDE = _default_auto_merge
        self._original_skip_serializer = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = True
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
        self._mirror_outbox_dir = on.OUTBOXES_ROOT / 'mirror'
        self._mirror_outbox_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        import larry_alerts as la
        for name, value in self._la_originals.items():
            setattr(la, name, value)
        on._AUTO_MERGE_FN_OVERRIDE = self._original_auto_merge_override
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._original_skip_serializer
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _marker(self, kind):
        """Build a canonical Mirror verdict marker block whose task_id +
        pr_url match what _deliver_mirror_verdict reconstructs from PR_CWD."""
        if kind == 'pass':
            payload = json.dumps({
                'task_id': self.HARVEST_TASK_ID, 'pr_url': self.HARVEST_PR_URL,
                'summary': 'LGTM',
            })
            return (
                f'=== REVIEW_PASS ===\n{payload}\n=== END_REVIEW_PASS ==='
            )
        if kind == 'revision':
            payload = json.dumps({
                'task_id': self.HARVEST_TASK_ID, 'pr_url': self.HARVEST_PR_URL,
                'findings': [{
                    'file': 'scripts/foo.py', 'line_range': 'L1-L3',
                    'severity': 'medium', 'description': 'Missing validation.',
                }],
                'severity': 'medium', 'confidence': 'high',
            })
            return (
                f'=== REVIEW_REVISION ===\n{payload}\n'
                f'=== END_REVIEW_REVISION ==='
            )
        if kind == 'escalate':
            payload = json.dumps({
                'task_id': self.HARVEST_TASK_ID, 'pr_url': self.HARVEST_PR_URL,
                'reason': 'Spec mismatch; needs replan.',
                'severity': 'high', 'confidence': 'high',
            })
            return (
                f'=== REVIEW_ESCALATE ===\n{payload}\n'
                f'=== END_REVIEW_ESCALATE ==='
            )
        raise ValueError(kind)

    def _harvest_and_process(self, kind, *, chat_id=12345):
        """Run the REAL healer deliver fn to write the synthesized outbox,
        then the REAL process_outbox on it. Returns (process_result,
        written_envelope_dict)."""
        import heal_wedged_review_sessions as h
        cand = h.Candidate(
            pid=9, cwd=self.PR_CWD, tier='mirror', jsonl=Path('/tmp/x.jsonl'),
            session_id='sess-harvest-9', idle_secs=600.0, marker_present=True,
        )
        with mock.patch.object(
            h, '_mirror_outbox_dir', return_value=self._mirror_outbox_dir,
        ), mock.patch.object(
            h, '_larry_primary_chat_id', return_value=chat_id,
        ):
            ok = h._deliver_mirror_verdict(
                cand, self._marker(kind), now_iso='2026-06-30T05:00:00+00:00',
            )
        self.assertTrue(ok, 'healer should report a successful harvest write')
        written = list(self._mirror_outbox_dir.glob('*.json'))
        self.assertEqual(len(written), 1, 'exactly one synthesized outbox')
        envelope = json.loads(written[0].read_text())
        result = on.process_outbox(written[0])
        return result, envelope

    def _read_alerts(self):
        path = self._root / 'blackboard' / 'larry-alerts.jsonl'
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def test_harvested_pass_reaches_auto_merge(self):
        # B1 regression: the synthesized PASS outbox must travel the
        # larry-direct path -> _run_review_pass_auto_merge, NOT the
        # archive-no-notify early return. FAILS on pre-fix code (source was
        # 'heal-wedged-review-sessions:harvest-before-reap' -> archived,
        # auto-merge never called).
        result, envelope = self._harvest_and_process('pass')
        # The synthesized envelope is shaped for larry_direct routing.
        self.assertEqual(envelope['source'], 'larry')
        self.assertIsInstance(envelope['reply_chat_id'], int)
        # Harvest provenance retained for the audit trail.
        self.assertEqual(
            envelope['harvested_by'],
            'heal-wedged-review-sessions:harvest-before-reap',
        )
        # Routed larry-direct (NOT archived-no-notify).
        self.assertEqual(result, 'larry-direct-marker')
        # Auto-merge actually fired against the harvested PR.
        self.assertEqual(len(self._auto_merge_calls), 1)
        self.assertEqual(self._auto_merge_calls[0][0], self.HARVEST_PR_URL)
        # Larry got the review-pass DM.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['intent'], 'review-pass')

    def test_harvested_revision_surfaces_without_merge(self):
        # A harvested clean REVISION must be SURFACED (status+findings posted
        # by process_outbox's pre-routing block; Larry DM'd the findings via
        # the cold-start no-session-revision path) and MUST NOT merge — no
        # forge_build_session_id on a synthesized envelope -> cold start, and
        # source='larry'+chat_id -> _dm_larry_no_session_revision.
        result, envelope = self._harvest_and_process('revision')
        self.assertEqual(result, 'larry-direct-marker')
        self.assertEqual(self._auto_merge_calls, [])
        # Surfaced to Larry (cold-start no-session revision DM), not dropped.
        alerts = self._read_alerts()
        self.assertGreaterEqual(len(alerts), 1)

    def test_harvested_escalate_surfaces_without_merge(self):
        # A harvested ESCALATE must DM Larry and NOT merge.
        result, envelope = self._harvest_and_process('escalate')
        self.assertEqual(result, 'larry-direct-marker')
        self.assertEqual(self._auto_merge_calls, [])
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['intent'], 'review-escalate')


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
        # D3.5 5d-prime — bypass the serializer gates in this test class.
        # The gates' `gh pr view --json mergeable` and `gh pr list` calls
        # aren't mocked here; the bypass preserves the D3.5 5d contract
        # (merge-outcome rendering via _AUTO_MERGE_FN_OVERRIDE).
        # Serializer-specific tests in test_auto_merge_serializer.py mock
        # subprocess.run end-to-end and exercise the gates directly.
        self._original_skip_serializer = on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = True

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
        on._AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST = self._original_skip_serializer
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
            '{"task_id": "real-19-proceed", '
            '"preflight_summary": "Edit foo.py."}\n'
            '=== END_PROCEED ==='
        )
        outbox = _good_outbox(
            agent='forge', source='larry', task_id='real-19-proceed',
            claude_session_id='sess-larry-direct-proceed',
            target_repo='ourliberty-agent-core',
            reply_chat_id=7998341473,
            result=f'Spec is clear; ready.\n\n{marker}',
        )
        f = self._write_outbox('forge', 'real-19-proceed.json', outbox)

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
        self.assertEqual(build_data['task_id'], 'real-19-proceed')
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
            '{"task_id": "real-19-clarify", '
            '"question": "Which config file should I modify?"}\n'
            '=== END_CLARIFY_REQUEST ==='
        )
        outbox = _good_outbox(
            agent='forge', source='larry', task_id='real-19-clarify',
            claude_session_id='sess-larry-clarify',
            clarification_count=0, max_clarifications=3,
            target_repo='ourliberty-agent-core',
            reply_chat_id=7998341473,
            result=f'Need more info.\n\n{marker}',
        )
        f = self._write_outbox('forge', 'real-19-clarify.json', outbox)

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
                task_id='real-19-revision', confidence='high',
            ),
            task_id='real-19-revision',
            source='larry',
            reply_chat_id=7998341473,
            forge_build_session_id='forge-build-sess-19',
            target_repo='ourliberty-agent-core',
            branch='forge/real-19-revision',
            pr_url=PR_URL_FIXTURE,
            revision_count=0,
            max_revisions=3,
        )
        f = self._write_outbox('mirror', 'real-19-revision.json', body)

        result = on.process_outbox(f)
        self.assertEqual(result, 'larry-direct-marker')
        self.assertEqual(self._auto_merge_calls, [])

        # Revision dispatched to Forge — the existing handler fires.
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-19-revision-*.json')
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
            _mirror_pass_marker(task_id='real-19-pass'),
            task_id='real-19-pass',
            source='larry',
            reply_chat_id=7998341473,
        )
        f = self._write_outbox('mirror', 'real-19-pass.json', body)

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
            _mirror_pass_marker(task_id='real-19-pass-nochat'),
            task_id='real-19-pass-nochat',
            source='larry',
            reply_chat_id=None,
        )
        f = self._write_outbox('mirror', 'real-19-pass-nochat.json', body)

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
            'task_id': 'real-template',
            'reply_chat_id': 7998341473,
        }
        bodies = {}
        for intent, marker_type, payload in (
            ('ack-proceed', 'proceed', {'task_id': 'real-template'}),
            (
                'review-revision', 'review_revision',
                {
                    'task_id': 'real-template', 'pr_url': PR_URL_FIXTURE,
                    'findings': [
                        {'file': 'a.py', 'line_range': 'L1',
                         'severity': 'low', 'description': 'x'},
                    ],
                },
            ),
            (
                'clarify', 'clarify_request',
                {
                    'task_id': 'real-template',
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

    # ---------------- dedup-wedge override (2026-06-11, mirrors PR #403) ----------------
    #
    # A stale archived Forge preflight task `<task_id>.json` must NOT
    # permanently block re-dispatch when the prior attempt was a DEFINITIVE
    # non-run (spawn-failure / identity-mismatch reject, no PR). The override
    # fires ONLY on a determinable non-run; in-flight / completed work keeps
    # the conservative skip. ccd-s1 (identity-reject) is the regression case.

    def _seed_inbox_archive(self, task_id, where='.archive'):
        """Plant the stale `<task_id>.json` in Forge's inbox archive/.invalid."""
        d = on.INBOXES_ROOT / 'forge' / where
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{task_id}.json').write_text('{}')

    def _seed_outbox_result(self, task_id, body, suffix='.1'):
        """Plant a Forge RESULT envelope in Forge's outbox archive."""
        d = on.OUTBOXES_ROOT / 'forge' / '.archive'
        d.mkdir(parents=True, exist_ok=True)
        f = d / f'{task_id}{suffix}.json'
        f.write_text(json.dumps(body))
        return f

    def _identity_reject_result(self, task_id):
        # ccd-s1 shape: identity drew BEACON instead of FORGE; the worker
        # emitted the single IDENTITY_MISMATCH line and stopped — no PR.
        return {
            'task_id': task_id,
            'agent': 'forge',
            'source': 'beacon',
            'phase': 'preflight',
            'exit_code': 0,
            'result': 'IDENTITY_MISMATCH: expected=forge loaded=beacon',
            'error': None,
            'duration_sec': 8.3,
            'pr_url': None,
            'completed_at': '2026-06-10T12:15:00.000000+00:00',
        }

    def _spawn_failure_result(self, task_id):
        return {
            'task_id': task_id,
            'agent': 'forge',
            'source': 'beacon',
            'phase': 'preflight',
            'exit_code': -1,
            'result': 'All retries exhausted',
            'error': 'All retries exhausted',
            'duration_sec': None,
            'pr_url': None,
            'completed_at': '2026-06-10T12:15:00.000000+00:00',
        }

    def _completed_result(self, task_id):
        return {
            'task_id': task_id,
            'agent': 'forge',
            'source': 'beacon',
            'phase': 'build',
            'exit_code': 0,
            'result': (
                'PR opened: '
                'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/410'
                '\n\nImplemented the change.'
            ),
            'error': None,
            'duration_sec': 142.5,
            'pr_url': None,
            'completed_at': '2026-06-10T13:00:00.000000+00:00',
        }

    def _genuine_reject_result(self, task_id):
        # A legitimate preflight REJECT (spec not buildable): exit 0, no PR,
        # NO identity-mismatch token. Must stay deduped — re-dispatch would
        # just re-REJECT.
        return {
            'task_id': task_id,
            'agent': 'forge',
            'source': 'beacon',
            'phase': 'preflight',
            'exit_code': 0,
            'result': (
                '=== REJECT ===\n{"task_id": "ccd-s1", "reason": "spec '
                'references a file that does not exist"}\n=== END_REJECT ==='
            ),
            'error': None,
            'duration_sec': 21.0,
            'pr_url': None,
            'completed_at': '2026-06-10T12:20:00.000000+00:00',
        }

    def test_dedup_overridden_on_identity_reject(self):
        # ccd-s1 regression: prior attempt was an IDENTITY_MISMATCH reject
        # (no PR, no real work). The stale archived task file must NOT wedge
        # the retry — the override re-dispatches.
        task_id = 'ccd-s1'
        self._seed_inbox_archive(task_id)
        self._seed_outbox_result(task_id, self._identity_reject_result(task_id))
        body = self._headless_outbox(marker_text=self._marker(task_id=task_id))
        f = self._write_outbox('beacon', 'larry-ccd-s1.json', body)
        status = on.process_outbox(f)
        self.assertEqual(status, 'headless-approval-dispatched')
        # A fresh live preflight task was written despite the stale archive.
        self.assertTrue((on.INBOXES_ROOT / 'forge' / f'{task_id}.json').exists())
        self.assertIn('HEADLESS_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_dedup_overridden_on_spawn_failure(self):
        task_id = 'ccd-s1'
        self._seed_inbox_archive(task_id)
        self._seed_outbox_result(task_id, self._spawn_failure_result(task_id))
        body = self._headless_outbox(marker_text=self._marker(task_id=task_id))
        f = self._write_outbox('beacon', 'larry-spawnfail.json', body)
        status = on.process_outbox(f)
        self.assertEqual(status, 'headless-approval-dispatched')
        self.assertTrue((on.INBOXES_ROOT / 'forge' / f'{task_id}.json').exists())
        self.assertIn('HEADLESS_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_dedup_override_fires_from_invalid_dir(self):
        # The wedge artifact can also live in .invalid/ (a validator-rejected
        # prior dispatch). The override must fire from there too.
        task_id = 'ccd-s1'
        self._seed_inbox_archive(task_id, where='.invalid')
        self._seed_outbox_result(task_id, self._identity_reject_result(task_id))
        body = self._headless_outbox(marker_text=self._marker(task_id=task_id))
        f = self._write_outbox('beacon', 'larry-invalid.json', body)
        on.process_outbox(f)
        self.assertTrue((on.INBOXES_ROOT / 'forge' / f'{task_id}.json').exists())
        self.assertIn('HEADLESS_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_dedup_still_skips_when_prior_completed(self):
        # A prior attempt that opened a PR is real work — re-dispatch must
        # NOT fire; the dedup keeps its skip.
        task_id = 'ccd-s1'
        self._seed_inbox_archive(task_id)
        self._seed_outbox_result(task_id, self._completed_result(task_id))
        body = self._headless_outbox(marker_text=self._marker(task_id=task_id))
        f = self._write_outbox('beacon', 'larry-done.json', body)
        on.process_outbox(f)
        self.assertFalse((on.INBOXES_ROOT / 'forge' / f'{task_id}.json').exists())
        self.assertNotIn('HEADLESS_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_dedup_still_skips_on_genuine_reject(self):
        # A legitimate REJECT (exit 0, no PR, no identity-mismatch token) is
        # NOT a non-run — re-dispatch would just re-REJECT. Must stay deduped.
        task_id = 'ccd-s1'
        self._seed_inbox_archive(task_id)
        self._seed_outbox_result(task_id, self._genuine_reject_result(task_id))
        body = self._headless_outbox(marker_text=self._marker(task_id=task_id))
        f = self._write_outbox('beacon', 'larry-reject.json', body)
        on.process_outbox(f)
        self.assertFalse((on.INBOXES_ROOT / 'forge' / f'{task_id}.json').exists())
        self.assertNotIn('HEADLESS_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_dedup_skips_when_no_terminal_result(self):
        # Notifier crashed mid-flight: the inbox artifact is archived but NO
        # terminal result exists. The override must NOT fire (conservative).
        task_id = 'ccd-s1'
        self._seed_inbox_archive(task_id)
        body = self._headless_outbox(marker_text=self._marker(task_id=task_id))
        f = self._write_outbox('beacon', 'larry-crash.json', body)
        on.process_outbox(f)
        self.assertFalse((on.INBOXES_ROOT / 'forge' / f'{task_id}.json').exists())
        self.assertNotIn('HEADLESS_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_dedup_live_inbox_always_skips(self):
        # A task currently in the LIVE inbox is in-flight; even with a prior
        # non-run result present, never double-dispatch it.
        task_id = 'ccd-s1'
        live_dir = on.INBOXES_ROOT / 'forge'
        live_dir.mkdir(parents=True, exist_ok=True)
        (live_dir / f'{task_id}.json').write_text('{"phase": "preflight"}')
        self._seed_outbox_result(task_id, self._identity_reject_result(task_id))
        body = self._headless_outbox(marker_text=self._marker(task_id=task_id))
        f = self._write_outbox('beacon', 'larry-inflight.json', body)
        on.process_outbox(f)
        # Exactly the one we planted; no second write, no override.
        self.assertEqual(len(list(live_dir.glob(f'{task_id}.json'))), 1)
        self.assertNotIn('HEADLESS_DEDUP_OVERRIDE', on.LOG_FILE.read_text())

    def test_non_run_helper_defensive(self):
        # Missing archive dir -> False.
        self.assertFalse(on._prior_dispatch_was_definitive_non_run('no-such'))
        # Corrupt envelope -> False (parse error is conservative).
        d = on.OUTBOXES_ROOT / 'forge' / '.archive'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'corrupt.1.json').write_text('{not valid json')
        self.assertFalse(on._prior_dispatch_was_definitive_non_run('corrupt'))
        # Prefix-collision guard: a result for `<task>-v2` must not match.
        self._seed_outbox_result(
            'pfx-v2', self._spawn_failure_result('pfx-v2')
        )
        self.assertFalse(on._prior_dispatch_was_definitive_non_run('pfx'))
        # Positive cases.
        self._seed_outbox_result('sf', self._spawn_failure_result('sf'))
        self.assertTrue(on._prior_dispatch_was_definitive_non_run('sf'))
        self._seed_outbox_result('im', self._identity_reject_result('im'))
        self.assertTrue(on._prior_dispatch_was_definitive_non_run('im'))


class ColdStartRevisionPromptTest(unittest.TestCase):
    """Pure-function coverage for the round-1 cold-start brief (S2/M2)."""

    def _build(self, pr_body):
        return on._build_cold_start_revision_prompt(
            task_id='t1', branch='claude/x', pr_url='https://gh/o/r/pull/9',
            next_count=1, max_revisions=3,
            findings_block=(
                "Mirror's findings on this PR:\n  1. [high] a.py L1 — Fix it."
            ),
            pr_body=pr_body,
        )

    def test_brief_includes_provenance_intent_and_findings(self):
        p = self._build('## Why\nThis PR adds X.')
        self.assertIn('NOT your build', p)            # provenance framing
        self.assertIn('## Why\nThis PR adds X.', p)   # PR intent, verbatim
        self.assertIn('Fix it.', p)                   # findings
        self.assertIn('claude/x', p)                  # branch
        self.assertIn('https://gh/o/r/pull/9', p)     # PR url
        self.assertIn('SAME branch', p)               # no-new-PR constraint
        self.assertIn('judgment/values call', p)      # decision escape valve

    def test_brief_degrades_when_pr_body_missing(self):
        p = self._build(None)
        self.assertIn('PR description unavailable', p)
        self.assertIn('NOT your build', p)            # still framed
        self.assertIn('Fix it.', p)                   # still carries findings


class NoSessionRevisionDmTest(unittest.TestCase):
    """Chain-gap #6 (observed 2026-05-20 on PR #59).

    When Larry opens a Claude-as-Forge PR (trivial config/docs edits done
    by Claude on Larry's behalf — source='larry', no Forge build session)
    and Mirror emits REVIEW_REVISION, the existing
    `_dispatch_revision_to_forge` skipped silently because there was no
    `forge_build_session_id` to --resume against. Larry only learned
    about the rejection if he was watching the chat live.

    Fix: when forge_build_session_id is missing AND source='larry' AND
    a reply_chat_id is present, queue a Larry DM with Mirror's findings
    + a manual-redispatch next step. Existing happy path and the
    non-Larry-source WARN are unchanged.
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
        # forge-cold-start-revision (S2): sandbox the no-session ledger + stub
        # the gh-backed PR-body fetch for the cold-start path.
        self._nsl_ledger_orig = on.no_session_ledger.LEDGER_FILE
        on.no_session_ledger.LEDGER_FILE = (
            self._root / 'state' / 'no-session-revision-ledger.json'
        )
        self._fetch_pr_body_orig = on._fetch_pr_body
        on._fetch_pr_body = lambda pr_url: '## Why\nFixture PR intent body.'
        on.ensure_dirs()

    def tearDown(self):
        on.no_session_ledger.LEDGER_FILE = self._nsl_ledger_orig
        on._fetch_pr_body = self._fetch_pr_body_orig
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        for name, value in self._la_originals.items():
            setattr(self._la, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.MODELS_CONFIG_PATH = self._rv_models_path
        rv.invalidate_cache()
        rv.invalidate_models_cache()
        self._tmp.cleanup()

    def _read_alerts(self):
        path = self._root / 'blackboard' / 'larry-alerts.jsonl'
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text().splitlines()
            if line.strip()
        ]

    def _revision_decision(self, findings=None, summary=None, confidence='high'):
        """Build a synthetic decision dict shaped like `_classify_mirror_marker`."""
        payload = {
            'task_id': 'real-claude-as-forge',
            'pr_url': PR_URL_FIXTURE,
            'severity': 'medium',
            'confidence': confidence,
        }
        if findings is not None:
            payload['findings'] = findings
        if summary is not None:
            payload['summary'] = summary
        return {
            'marker_type': 'review_revision',
            'intent': 'review-revision',
            'auto_promoted': False,
            'budget_exhausted': False,
            'payload': payload,
            'intent_kwargs': {},
            'notify_source': 'mirror-result',
            'next_clarification_count': None,
        }

    # ---------------- happy path: session present, no DM, existing dispatch ----------------

    def test_session_present_dispatches_revision_no_larry_dm(self):
        # Regression: the new no-session branch MUST NOT fire when the
        # Forge build session IS present. Existing auto-resume path runs;
        # no Larry DM is queued (Forge picking up the revision is the
        # closing signal, not a DM).
        data = _good_outbox(
            agent='mirror', source='larry', task_id='real-claude-as-forge',
            phase='review', target_repo='ourliberty-agent-core',
            branch='feat/claude-direct',
            pr_url=PR_URL_FIXTURE,
            result='Reviewed.',
        )
        data['forge_build_session_id'] = 'forge-build-sess-xyz'
        data['reply_chat_id'] = 12345
        decision = self._revision_decision(findings=[
            {'file': 'docs/x.md', 'line_range': 'L1',
             'severity': 'low', 'description': 'tweak wording'},
        ])
        on._dispatch_revision_to_forge(data, decision)
        # Forge inbox got the revision (existing path).
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-*.json')
        )
        self.assertEqual(len(revisions), 1)
        # No Larry DM queued (chain advances via Forge).
        self.assertEqual(self._read_alerts(), [])

    # ---------------- no-session + larry + chat → DM queued ----------------

    def test_no_session_larry_source_with_chat_id_queues_dm(self):
        data = _good_outbox(
            agent='mirror', source='larry', task_id='real-claude-as-forge',
            phase='review', target_repo='ourliberty-agent-core',
            branch='feat/claude-direct',
            pr_url=PR_URL_FIXTURE,
            result='Reviewed.',
        )
        # forge_build_session_id intentionally absent — Claude-as-Forge shape.
        data['reply_chat_id'] = 12345
        decision = self._revision_decision(
            summary='Two findings need addressing before merge.',
            findings=[
                {'file': 'scripts/a.py', 'line_range': 'L10-L20',
                 'severity': 'medium', 'description': 'Add input validation.'},
                {'file': 'scripts/b.py', 'line_range': 'L5',
                 'severity': 'low', 'description': 'Rename variable for clarity.'},
            ],
        )
        on._dispatch_revision_to_forge(data, decision)
        # No revision task to Forge — there's no session to resume.
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-*.json')
        )
        self.assertEqual(revisions, [])
        # Larry DM queued with the findings rendered.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        rec = alerts[0]
        self.assertEqual(rec['kind'], 'notification')
        self.assertEqual(rec['intent'], 'review-revision')
        self.assertEqual(rec['chat_id'], 12345)
        self.assertEqual(rec['task_id'], 'real-claude-as-forge')
        body = rec['message']
        self.assertIn(PR_URL_FIXTURE, body)
        self.assertIn('real-claude-as-forge', body)
        self.assertIn('no forge session', body.lower())
        self.assertIn('feat/claude-direct', body)
        self.assertIn('Two findings need addressing', body)
        self.assertIn('Add input validation.', body)
        self.assertIn('Rename variable for clarity.', body)
        # Next-step instruction included.
        self.assertIn('Mirror review', body)

    # ---------------- no-session + larry + no chat_id → Beacon route (S2 M2) ----------------

    def test_no_session_larry_source_without_chat_id_cold_starts(self):
        # forge-cold-start-revision (S2): routing_source='larry' but no chat_id
        # to DM → the chat-targeted `_dm_larry_no_session_revision` path can't
        # fire, so fall through to a FRESH cold-start Forge dispatch (not the
        # old Beacon notify, which could silently dead-end).
        data = _good_outbox(
            agent='mirror', source='larry', task_id='real-no-chat',
            phase='review', target_repo='ourliberty-agent-core',
            branch='feat/claude-direct',
            pr_url=PR_URL_FIXTURE,
            result='Reviewed.',
        )
        # No forge_build_session_id, no reply_chat_id.
        decision = self._revision_decision(
            findings=[{
                'file': 'a.py', 'line_range': 'L1', 'severity': 'medium',
                'description': 'Add validation.',
            }],
        )
        on._dispatch_revision_to_forge(data, decision)
        # A fresh Forge revision was dispatched on the same branch.
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-no-chat-*.json')
        )
        self.assertEqual(len(revisions), 1)
        task = json.loads(revisions[0].read_text())
        self.assertNotIn('session_id', task)
        self.assertIn('NOT your build', task['prompt'])
        self.assertIn('Add validation.', task['prompt'])
        self.assertIn(PR_URL_FIXTURE, task['prompt'])
        # No legacy Beacon no-session notify, no warning-severity Larry alert.
        notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob(
                'notify-no-session-revision-*.json'
            )
        )
        self.assertEqual(notifies, [])
        self.assertEqual(self._read_alerts(), [])
        # Obligation opened for the backstop.
        ob = on.no_session_ledger.get_obligation('real-no-chat')
        self.assertIsNotNone(ob)
        self.assertEqual(ob['status'], on.no_session_ledger.OPEN)

    # ---------------- no-session + source!='larry' → cold-start (S2) ----------------

    def test_no_session_non_larry_source_cold_starts(self):
        # forge-cold-start-revision (S2): the #645 / #653 / PR #412 class — a
        # heal-rebuilt / Beacon-dispatched envelope with no
        # forge_build_session_id (routing_source != 'larry'). Cold-start a fresh
        # Forge revision instead of the old Beacon notify.
        data = _good_outbox(
            agent='mirror', source='beacon', task_id='real-prop-bug',
            phase='review', target_repo='ourliberty-agent-core',
            branch='forge/real-prop-bug',
            pr_url=PR_URL_FIXTURE,
            result='Reviewed.',
        )
        data['reply_chat_id'] = 99999  # set but irrelevant: source!='larry'
        decision = self._revision_decision()
        on._dispatch_revision_to_forge(data, decision)
        # A fresh Forge revision was dispatched.
        revisions = list(
            (on.INBOXES_ROOT / 'forge').glob('revision-real-prop-bug-*.json')
        )
        self.assertEqual(len(revisions), 1)
        task = json.loads(revisions[0].read_text())
        self.assertNotIn('session_id', task)
        self.assertIn('NOT your build', task['prompt'])
        # No legacy Beacon no-session notify, no warning-severity Larry alert.
        notifies = list(
            (on.INBOXES_ROOT / 'beacon').glob(
                'notify-no-session-revision-*.json'
            )
        )
        self.assertEqual(notifies, [])
        self.assertEqual(self._read_alerts(), [])
        ob = on.no_session_ledger.get_obligation('real-prop-bug')
        self.assertIsNotNone(ob)

    # ---------------- DM body rendering: multi/single/empty findings ----------------

    def test_dm_body_renders_multi_finding_payload(self):
        data = _good_outbox(
            agent='mirror', source='larry', task_id='real-multi',
            phase='review', branch='feat/x', pr_url=PR_URL_FIXTURE,
            result='Reviewed.',
        )
        decision = self._revision_decision(findings=[
            {'file': 'a.py', 'line_range': 'L1', 'severity': 'high',
             'description': 'First issue.'},
            {'file': 'b.py', 'line_range': 'L2', 'severity': 'medium',
             'description': 'Second issue.'},
            {'file': 'c.py', 'line_range': 'L3', 'severity': 'low',
             'description': 'Third issue.'},
        ])
        body = on._render_no_session_revision_dm(data, decision)
        self.assertIn('1. [high] a.py L1 — First issue.', body)
        self.assertIn('2. [medium] b.py L2 — Second issue.', body)
        self.assertIn('3. [low] c.py L3 — Third issue.', body)
        self.assertIn('Findings:', body)

    def test_dm_body_renders_single_finding_payload(self):
        data = _good_outbox(
            agent='mirror', source='larry', task_id='real-single',
            phase='review', branch='feat/x', pr_url=PR_URL_FIXTURE,
            result='Reviewed.',
        )
        decision = self._revision_decision(findings=[
            {'file': 'only.py', 'line_range': 'L42', 'severity': 'medium',
             'description': 'Only issue.'},
        ])
        body = on._render_no_session_revision_dm(data, decision)
        self.assertIn('1. [medium] only.py L42 — Only issue.', body)
        # No phantom second entry.
        self.assertNotIn('2. [', body)

    def test_dm_body_handles_summary_only_no_findings(self):
        # Mirror occasionally emits a REVIEW_REVISION payload with a
        # summary but no structured findings list (degraded path). The
        # DM body must still render — summary line present, no Findings
        # header, next-step instruction intact.
        data = _good_outbox(
            agent='mirror', source='larry', task_id='real-summary-only',
            phase='review', branch='feat/x', pr_url=PR_URL_FIXTURE,
            result='Reviewed.',
        )
        decision = self._revision_decision(
            summary='Needs rework — see PR comments.',
            findings=None,
        )
        body = on._render_no_session_revision_dm(data, decision)
        self.assertIn('Needs rework — see PR comments.', body)
        self.assertNotIn('Findings:', body)
        self.assertIn('Mirror review', body)


class PrUrlStructuralShapeCheckTest(unittest.TestCase):
    """Layer 1 — shape + allowlist check (no shell-out, no network).

    Replaces the prior name-based allowlist + canonical-form rewrite
    table. The discipline-correct fix is to validate intrinsic
    properties of the pr_url (shape + existence), not surface forms of
    the task_id, so the gate doesn't grow a fresh allowlist row every
    time a new fixture family leaks into the outbox.

    The repo allowlist is now sourced from `config/agent-models.json`
    `allowed_repos` via `routing_validator.allowed_repos_for('forge')` —
    the SAME source of truth that gates dispatch. These tests run against
    the live repo config (which lists ourliberty-agent-core,
    ourliberty-dashboard, ourliberty-graph for forge); the cache is
    invalidated in setUp so a prior test's temp config can't leak in.
    """

    def setUp(self):
        rv.invalidate_models_cache()

    def tearDown(self):
        rv.invalidate_models_cache()

    def test_canonical_agent_core_url_accepted(self):
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/107'
        )
        self.assertEqual(repo, 'Larry-Yatch/ourliberty-agent-core')
        self.assertEqual(n, 107)
        self.assertEqual(reason, 'ok')

    def test_canonical_dashboard_url_accepted(self):
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/Larry-Yatch/ourliberty-dashboard/pull/42'
        )
        self.assertEqual(repo, 'Larry-Yatch/ourliberty-dashboard')
        self.assertEqual(n, 42)
        self.assertEqual(reason, 'ok')

    def test_canonical_graph_url_accepted(self):
        # Regression target for notifier-autopr-allowlist-from-config-001:
        # ourliberty-graph onboarded into config allowed_repos but the old
        # hardcoded alternation never gained it, so a clean Mirror
        # REVIEW_PASS on a graph PR (PR #1, 2026-06-13) was skipped as
        # pr-url-shape-invalid. Config-sourcing the allowlist accepts it.
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/Larry-Yatch/ourliberty-graph/pull/1'
        )
        self.assertEqual(repo, 'Larry-Yatch/ourliberty-graph')
        self.assertEqual(n, 1)
        self.assertEqual(reason, 'ok')

    def test_wrong_owner_rejected(self):
        # Old validator would rewrite `x/y` and fail closed via rewrite
        # table; new validator just rejects on the regex anchor.
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/x/y/pull/5'
        )
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'shape-mismatch')

    def test_wrong_owner_case_rejected(self):
        # 'ourliberty' as the owner (the 2026-05-29 incident shape).
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/ourliberty/ourliberty-agent-core/pull/3'
        )
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'shape-mismatch')

    def test_pr_number_zero_rejected(self):
        # The crucial gap the old allowlist missed: pull/0 in a known
        # repo. Old code accepted it because owner+repo matched; new
        # code rejects because regex anchors the integer to [1-9]\d*.
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/0'
        )
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'shape-mismatch')

    def test_not_a_url_rejected(self):
        repo, n, reason = on._pr_url_shape_check('not-a-url')
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'shape-mismatch')

    def test_empty_string_rejected(self):
        repo, n, reason = on._pr_url_shape_check('')
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'empty-or-non-string')

    def test_none_rejected(self):
        repo, n, reason = on._pr_url_shape_check(None)
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'empty-or-non-string')

    def test_trailing_path_rejected(self):
        # Strictness check: `/files` after the PR number is rejected at
        # this layer (the gate wants the exact form `gh pr merge` needs;
        # trailing junk should be cleaned up upstream, not silently
        # accepted by the AUTO_MERGE gate).
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/107/files'
        )
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'shape-mismatch')

    def test_unknown_repo_with_correct_owner_rejected(self):
        # Owner + shape are valid, but the repo slug is not in the
        # config-sourced allowlist (allowed_repos_for('forge')). This is
        # now a distinct reason from shape-mismatch: the URL is well-formed
        # and Larry-Yatch-owned, it just points at a repo the chain does
        # not manage. Closed-set anti-spoofing boundary preserved.
        repo, n, reason = on._pr_url_shape_check(
            'https://github.com/Larry-Yatch/some-other-repo/pull/1'
        )
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'repo-not-allowlisted')

    def test_http_scheme_rejected(self):
        # https only — http is rejected (the gate is feeding gh pr merge
        # which always uses https; accept-http is needless surface area).
        repo, n, reason = on._pr_url_shape_check(
            'http://github.com/Larry-Yatch/ourliberty-agent-core/pull/5'
        )
        self.assertIsNone(repo)
        self.assertIsNone(n)
        self.assertEqual(reason, 'shape-mismatch')


class PrUrlExistenceStateTest(unittest.TestCase):
    """Layer 2 — gh pr view --json state existence check.

    Mocks subprocess.run directly so no network shell-out fires.
    """

    REPO = 'Larry-Yatch/ourliberty-agent-core'
    PR = 42

    def _proc(self, *, returncode=0, stdout='', stderr=''):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def test_open_state_returned(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(
                returncode=0, stdout=json.dumps({'state': 'OPEN'}),
            )
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertEqual(state, 'OPEN')
        self.assertEqual(reason, 'ok')

    def test_merged_state_returned(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(
                returncode=0, stdout=json.dumps({'state': 'MERGED'}),
            )
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertEqual(state, 'MERGED')
        self.assertEqual(reason, 'ok')

    def test_closed_state_returned(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(
                returncode=0, stdout=json.dumps({'state': 'CLOSED'}),
            )
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertEqual(state, 'CLOSED')
        self.assertEqual(reason, 'ok')

    def test_http_404_returns_none(self):
        # gh pr view on a nonexistent PR exits non-zero with HTTP 404
        # in stderr. Treated as not-found at the caller.
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(
                returncode=1,
                stderr='HTTP 404: Not Found (https://api.github.com/repos/...)',
            )
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertIsNone(state)
        self.assertIn('404', reason)
        self.assertIn('gh exit=1', reason)

    def test_timeout_returns_none(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = on.subprocess.TimeoutExpired(
                cmd=['gh'], timeout=on._PR_URL_EXISTENCE_TIMEOUT_S,
            )
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertIsNone(state)
        self.assertIn('timeout', reason)

    def test_gh_missing_returns_none(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.side_effect = FileNotFoundError('gh')
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertIsNone(state)
        self.assertIn('FileNotFoundError', reason)

    def test_parse_error_returns_none(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(returncode=0, stdout='not json')
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertIsNone(state)
        self.assertEqual(reason, 'parse-error')

    def test_no_state_field_returns_none(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(
                returncode=0, stdout=json.dumps({'unrelated': 'value'}),
            )
            state, reason = on._pr_url_existence_state(self.REPO, self.PR)
        self.assertIsNone(state)
        self.assertEqual(reason, 'no-state-field')

    def test_uses_ten_second_timeout(self):
        # Layer 2 timeout is intentionally tighter than the general
        # _AUTO_MERGE_TIMEOUT_S so a degraded gh CLI doesn't stall
        # the notifier poll loop.
        self.assertEqual(on._PR_URL_EXISTENCE_TIMEOUT_S, 10)
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(
                returncode=0, stdout=json.dumps({'state': 'OPEN'}),
            )
            on._pr_url_existence_state(self.REPO, self.PR)
        _, kwargs = m_run.call_args
        self.assertEqual(kwargs.get('timeout'), 10)

    def test_gh_command_shape(self):
        with mock.patch.object(on.subprocess, 'run') as m_run:
            m_run.return_value = self._proc(
                returncode=0, stdout=json.dumps({'state': 'OPEN'}),
            )
            on._pr_url_existence_state(self.REPO, self.PR)
        args, _ = m_run.call_args
        cmd = args[0]
        self.assertEqual(cmd[:3], ['gh', 'pr', 'view'])
        self.assertIn('42', cmd)
        self.assertIn('--repo', cmd)
        self.assertIn(self.REPO, cmd)
        self.assertIn('--json', cmd)
        self.assertIn('state', cmd)


class AutoMergeStructuralValidatorIntegrationTest(unittest.TestCase):
    """process_outbox integration — confirms the two-layer validator
    short-circuits the AUTO_MERGE shell-out on the cases the prior
    name-based allowlist either missed (pull/0 in known repo,
    pull/99999) or caught only by surface-form heuristic.

    Layout mirrors MirrorMarkerRoutingTest (per-test tmp AGENTS_ROOT,
    routing-validator sandbox, larry_alerts sandbox). Does NOT install
    `_AUTO_MERGE_FN_OVERRIDE` — that flag triggers the test-mode
    existence-check bypass at the call site. The explicit goal here is
    to exercise the validator end-to-end.
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
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _make_pass_outbox(self, pr_url, task_id='real-rev'):
        payload = json.dumps({
            'task_id': task_id, 'pr_url': pr_url,
            'summary': 'AC coverage clean.',
        })
        marker = (
            f'=== REVIEW_PASS ===\n{payload}\n=== END_REVIEW_PASS ==='
        )
        body = _good_outbox(
            agent='mirror', source='beacon', task_id=task_id, phase='review',
            result='Reviewed.\n\n' + marker,
        )
        outbox_dir = on.OUTBOXES_ROOT / 'mirror'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / f'{task_id}.json'
        f.write_text(json.dumps(body))
        return f

    def _beacon_notify_count(self):
        beacon_inbox = on.INBOXES_ROOT / 'beacon'
        if not beacon_inbox.is_dir():
            return 0
        return len(list(beacon_inbox.glob('notify-*.json')))

    def _larry_dm_count(self):
        import larry_alerts as la
        if not la.ALERTS_FILE.exists():
            return 0
        return sum(1 for _ in la.ALERTS_FILE.read_text().splitlines() if _.strip())

    # ---- Shape-invalid cases short-circuit before any gh shell-out ----

    def test_wrong_owner_skipped_without_shellout(self):
        f = self._make_pass_outbox('https://github.com/x/y/pull/5')
        with mock.patch.object(on.subprocess, 'run') as m_run:
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        m_run.assert_not_called()
        # No DM to Larry queued for the skip outcome.
        self.assertEqual(self._larry_dm_count(), 0)

    def test_known_repo_wrong_owner_case_skipped(self):
        # The 2026-05-29 incident shape: owner='ourliberty' instead of
        # 'Larry-Yatch'. Old allowlist correctly caught this — new
        # validator's regex catches it too (more precisely: nothing
        # outside `Larry-Yatch/<allowed>/pull/[1-9]\d*$` matches).
        f = self._make_pass_outbox(
            'https://github.com/ourliberty/ourliberty-agent-core/pull/3'
        )
        with mock.patch.object(on.subprocess, 'run') as m_run:
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        m_run.assert_not_called()

    def test_pull_zero_in_known_repo_skipped(self):
        # The crucial regression case. The old allowlist's repo-coords
        # check accepted this because owner/repo were canonical; the
        # shell-out to `gh pr merge 0` then wasted cycles + logged the
        # 404. The new shape regex's [1-9]\d* anchor rejects it
        # without any shell-out.
        f = self._make_pass_outbox(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/0'
        )
        with mock.patch.object(on.subprocess, 'run') as m_run:
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        m_run.assert_not_called()

    def test_not_a_url_skipped(self):
        f = self._make_pass_outbox('not-a-url')
        with mock.patch.object(on.subprocess, 'run') as m_run:
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        m_run.assert_not_called()

    # ---- Shape-valid + Layer-2 outcomes ----

    def test_shape_valid_nonexistent_pr_skipped(self):
        # The other side of the 2026-05-29 gap: `pull/99999` in
        # ourliberty-agent-core. Shape valid (passes Layer 1) but the
        # PR doesn't exist. `gh pr view` returns HTTP 404; skipped
        # before any `gh pr merge` attempt.
        f = self._make_pass_outbox(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/99999'
        )
        view_proc = mock.MagicMock()
        view_proc.returncode = 1
        view_proc.stdout = ''
        view_proc.stderr = 'HTTP 404: Not Found'
        merge_called = []
        def _fake_merge(*args, **kwargs):
            merge_called.append(1)
            raise AssertionError('gh pr merge MUST NOT be called on 404')
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on, '_attempt_auto_merge_with_gates',
                                  side_effect=_fake_merge):
            m_run.return_value = view_proc
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        # Existence check fired exactly once; no merge attempt.
        self.assertEqual(m_run.call_count, 1)
        self.assertEqual(merge_called, [])

    def test_shape_valid_merged_pr_skipped_no_remerge(self):
        f = self._make_pass_outbox(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
        )
        view_proc = mock.MagicMock()
        view_proc.returncode = 0
        view_proc.stdout = json.dumps({'state': 'MERGED'})
        view_proc.stderr = ''
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on, '_attempt_auto_merge_with_gates') as m_merge:
            m_run.return_value = view_proc
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        # Existence check fired exactly once; no merge attempt.
        self.assertEqual(m_run.call_count, 1)
        m_merge.assert_not_called()

    def test_shape_valid_closed_pr_skipped(self):
        f = self._make_pass_outbox(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
        )
        view_proc = mock.MagicMock()
        view_proc.returncode = 0
        view_proc.stdout = json.dumps({'state': 'CLOSED'})
        view_proc.stderr = ''
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on, '_attempt_auto_merge_with_gates') as m_merge:
            m_run.return_value = view_proc
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        m_merge.assert_not_called()

    def test_shape_valid_open_pr_proceeds_to_merge(self):
        # Happy path: Layer 1 passes, Layer 2 confirms OPEN, merge fires.
        f = self._make_pass_outbox(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
        )
        view_proc = mock.MagicMock()
        view_proc.returncode = 0
        view_proc.stdout = json.dumps({'state': 'OPEN'})
        view_proc.stderr = ''
        merged_result = {
            'merge_outcome': 'merged',
            'merge_reason': 'squash-merged + branch deleted',
            'pr_number': 42,
            'repo_coords': 'Larry-Yatch/ourliberty-agent-core',
        }
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on, '_attempt_auto_merge_with_gates',
                                  return_value=merged_result) as m_merge:
            m_run.return_value = view_proc
            result = on.process_outbox(f)
        self.assertIn(result, ('larry-direct-marker', 'notified-marker'))
        # Existence check fired once.
        self.assertEqual(m_run.call_count, 1)
        # Merge fired exactly once with the canonical repo coords + PR#.
        m_merge.assert_called_once()
        _, kwargs = m_merge.call_args
        self.assertEqual(kwargs['repo_coords'], 'Larry-Yatch/ourliberty-agent-core')
        self.assertEqual(kwargs['pr_number'], 42)

    def test_shape_valid_existence_check_timeout_skipped(self):
        # Timeout is treated the same as not-found for safety — we
        # never shell out to `gh pr merge` without first having
        # confirmed PR existence.
        f = self._make_pass_outbox(
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'
        )
        def _timeout(*args, **kwargs):
            raise on.subprocess.TimeoutExpired(
                cmd=['gh'], timeout=on._PR_URL_EXISTENCE_TIMEOUT_S,
            )
        with mock.patch.object(on.subprocess, 'run', side_effect=_timeout), \
                mock.patch.object(on, '_attempt_auto_merge_with_gates') as m_merge:
            result = on.process_outbox(f)
        self.assertEqual(result, 'auto-merge-skipped')
        m_merge.assert_not_called()


class OutboxFixtureGateTest(unittest.TestCase):
    """Outbox-side fixture-pattern allowlist gate (extend-fixture-gate-outbox-side).

    The gate fires at the top of process_outbox and short-circuits any outbox
    file whose stem matches a fixture pattern (via matched_fixture_envelope —
    same single-source-of-truth allowlist used by inbox_watcher.py:415, Check
    III/VIII/IX, and run_cycle.sh). The match is filename-only so partial /
    malformed JSON in a fixture file still gets quarantined without invoking
    the marker parser or burning an Opus cycle.
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
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        self._tmp.cleanup()

    def _write(self, agent: str, name: str, body: dict | str | None = None) -> Path:
        outbox_dir = on.OUTBOXES_ROOT / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        f = outbox_dir / name
        if body is None:
            body = _good_outbox()
        if isinstance(body, dict):
            f.write_text(json.dumps(body))
        else:
            f.write_text(body)
        return f

    def test_t_prefix_fixture_filename_quarantined(self):
        # `t-bad-rev.5.json` — the documented 23-burn half of the cost-loop.
        f = self._write('forge', 't-bad-rev.5.json')
        self.assertEqual(on.process_outbox(f), 'fixture-quarantined')
        # File moved into <outbox>/.fixture-quarantine/ — original gone.
        self.assertFalse(f.exists())
        dest = on.OUTBOXES_ROOT / 'forge' / '.fixture-quarantine' / 't-bad-rev.5.json'
        self.assertTrue(dest.exists())
        # No marker-error notify written to Forge's inbox.
        forge_inbox = on.INBOXES_ROOT / 'forge'
        if forge_inbox.is_dir():
            self.assertEqual(list(forge_inbox.glob('marker-error-*.json')), [])

    def test_envelope_id_fixture_filename_quarantined(self):
        # `envelope-id.20.json` — the documented 54-burn half (added to
        # FIXTURE_PATTERN_EXACT in this PR).
        f = self._write('forge', 'envelope-id.20.json')
        self.assertEqual(on.process_outbox(f), 'fixture-quarantined')
        dest = on.OUTBOXES_ROOT / 'forge' / '.fixture-quarantine' / 'envelope-id.20.json'
        self.assertTrue(dest.exists())

    def test_envelope_id_collision_suffix_quarantined(self):
        # `envelope-id.54.json` — _strip_seq_suffix peels the `.54`
        # collision suffix so the bare `envelope-id` matches EXACT. The
        # 58 archived envelope-id outboxes from the documented loop all
        # have this `.N` shape (verified via find on ~/agents/outboxes/).
        f = self._write('forge', 'envelope-id.54.json')
        self.assertEqual(on.process_outbox(f), 'fixture-quarantined')
        dest = on.OUTBOXES_ROOT / 'forge' / '.fixture-quarantine' / 'envelope-id.54.json'
        self.assertTrue(dest.exists())

    def test_notify_t_fixture_wrapper_quarantined(self):
        # `notify-t-pf-answer.json` — wrapper-peel hits `t-` prefix.
        f = self._write('beacon', 'notify-t-pf-answer.json')
        self.assertEqual(on.process_outbox(f), 'fixture-quarantined')
        dest = (
            on.OUTBOXES_ROOT / 'beacon' / '.fixture-quarantine'
            / 'notify-t-pf-answer.json'
        )
        self.assertTrue(dest.exists())

    def test_non_fixture_outbox_falls_through(self):
        # Real task_id passes the gate and continues to existing routing
        # (the body has source='pulse' → notify back to pulse). Regression
        # guard for the "non-fixture happy path".
        body = _good_outbox(agent='beacon', source='pulse', task_id='real-1')
        f = self._write('beacon', 'real-1.json', body)
        # Need safe_write_inbox sandboxed too so the notify lands in tmp.
        swi_inboxes_backup = swi.INBOXES_ROOT
        swi_agents_backup = swi.AGENTS_ROOT
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        try:
            self.assertEqual(on.process_outbox(f), 'notified')
        finally:
            swi.AGENTS_ROOT = swi_agents_backup
            swi.INBOXES_ROOT = swi_inboxes_backup
        # No fixture-quarantine directory was created.
        self.assertFalse((on.OUTBOXES_ROOT / 'beacon' / '.fixture-quarantine').exists())

    def test_quarantine_dir_created_on_first_use(self):
        # `.fixture-quarantine/` does not pre-exist; gate creates it.
        outbox_dir = on.OUTBOXES_ROOT / 'forge'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        qdir = outbox_dir / '.fixture-quarantine'
        self.assertFalse(qdir.exists())
        f = self._write('forge', 't-loop.json')
        self.assertEqual(on.process_outbox(f), 'fixture-quarantined')
        self.assertTrue(qdir.is_dir())

    def test_quarantine_preserves_filename_for_forensics(self):
        # Filename is preserved verbatim under .fixture-quarantine/ so an
        # operator can inspect what was quarantined and when.
        f = self._write('forge', 't-bad-rev.17.json')
        on.process_outbox(f)
        dest = on.OUTBOXES_ROOT / 'forge' / '.fixture-quarantine' / 't-bad-rev.17.json'
        self.assertTrue(dest.exists())
        body = json.loads(dest.read_text())
        self.assertEqual(body['task_id'], 'real-001')  # _good_outbox default

    def test_fixture_quarantine_runs_before_json_parse(self):
        # Partial / malformed JSON in a fixture filename should still
        # quarantine cleanly — the gate matches filename only, no read.
        # This is the cost-saving property: bad fixture content never
        # reaches the marker parser or the Opus session.
        f = self._write('forge', 't-pf-bad.json', body='{not-json}')
        self.assertEqual(on.process_outbox(f), 'fixture-quarantined')
        dest = on.OUTBOXES_ROOT / 'forge' / '.fixture-quarantine' / 't-pf-bad.json'
        self.assertTrue(dest.exists())


class EmissionPathFixtureGateTest(unittest.TestCase):
    """Emission-path fixture gates (mission #2, 2026-05-30).

    Four re-emission sites bypass the read-time process_outbox gate:
    _notify_forge_marker_error, _notify_mirror_marker_error,
    _dead_letter_marker_error_to_dispatcher, and the scan_dead_letters
    .invalid loop. Each calls _is_fixture_emission(task_id) right before its
    safe_write_inbox and suppresses a reserved-namespace fixture so it can't
    be re-injected into an inbox and re-dispatched (the 2026-05-28/29 cost-
    loop shape). A legit task_id still emits.
    """

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

    # ---- helper predicate ----
    def test_is_fixture_emission_flags_reserved_namespace(self):
        self.assertTrue(on._is_fixture_emission('zz-fixture-loop'))
        self.assertTrue(on._is_fixture_emission('t-pf-1'))
        # wrapper-peeling: a cascaded fixture name still resolves.
        self.assertTrue(on._is_fixture_emission('marker-error-zz-fixture-loop'))

    def test_is_fixture_emission_passes_legit_ids(self):
        self.assertFalse(on._is_fixture_emission('real-chat'))
        self.assertFalse(on._is_fixture_emission('opmanual-d35-7-001'))
        self.assertFalse(on._is_fixture_emission(None))

    # ---- site 1: _notify_forge_marker_error ----
    def test_forge_marker_error_suppresses_fixture(self):
        data = {'agent': 'forge', 'source': 'beacon', 'task_id': 'zz-fixture-loop'}
        on._notify_forge_marker_error(data, 'no JSON in marker')
        self.assertEqual(
            list((on.INBOXES_ROOT / 'forge').glob('marker-error-*.json')), []
        )

    def test_forge_marker_error_emits_legit(self):
        data = {'agent': 'forge', 'source': 'beacon', 'task_id': 'real-chat'}
        on._notify_forge_marker_error(data, 'no JSON in marker')
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'forge').glob('marker-error-*.json'))), 1
        )

    # ---- site 2: _dead_letter_marker_error_to_dispatcher ----
    def test_dead_letter_suppresses_fixture(self):
        data = {'agent': 'forge', 'source': 'beacon', 'task_id': 'zz-fixture-loop'}
        on._dead_letter_marker_error_to_dispatcher(data, 'beacon', 'parse err', 4)
        self.assertEqual(
            list((on.INBOXES_ROOT / 'beacon').glob('dead-letter-marker-*.json')), []
        )

    def test_dead_letter_emits_legit(self):
        data = {'agent': 'forge', 'source': 'beacon', 'task_id': 'real-chat'}
        on._dead_letter_marker_error_to_dispatcher(data, 'beacon', 'parse err', 4)
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'beacon').glob('dead-letter-marker-*.json'))), 1
        )

    # ---- site 3: _notify_mirror_marker_error ----
    def test_mirror_marker_error_suppresses_fixture(self):
        data = {'agent': 'mirror', 'source': 'beacon', 'task_id': 'zz-fixture-loop'}
        on._notify_mirror_marker_error(data, 'prose, not JSON')
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json')), []
        )

    def test_mirror_marker_error_emits_legit(self):
        data = {'agent': 'mirror', 'source': 'beacon', 'task_id': 'real-chat'}
        on._notify_mirror_marker_error(data, 'prose, not JSON')
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'mirror').glob('marker-error-*.json'))), 1
        )

    # ---- site 4: scan_dead_letters .invalid loop ----
    def _plant_invalid(self, agent, name, source='beacon'):
        invalid_dir = on.INBOXES_ROOT / agent / '.invalid'
        invalid_dir.mkdir(parents=True, exist_ok=True)
        (invalid_dir / name).write_text(json.dumps({
            'task_id': name.replace('.json', ''),
            'source': source, 'prompt': 'too short',
        }))
        (invalid_dir / name.replace('.json', '.reason')).write_text('F24 empty-prompt')

    def test_scan_dead_letters_suppresses_fixture(self):
        self._plant_invalid('forge', 'zz-fixture-loop.json')
        n = on.scan_dead_letters()
        self.assertEqual(n, 0)
        self.assertEqual(
            list((on.INBOXES_ROOT / 'beacon').glob('notify-dead-letter-*.json')), []
        )

    def test_scan_dead_letters_emits_legit(self):
        self._plant_invalid('forge', 'real-bad-task.json')
        n = on.scan_dead_letters()
        self.assertEqual(n, 1)
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'beacon').glob('notify-dead-letter-*.json'))), 1
        )


class PrUrlRobustExtractionTest(unittest.TestCase):
    """fix-notifier-review-dispatch-reliability (Part A): _PR_URL_RE now
    tolerates a short leading clause (`Done. `, `Result: `) and a `#<num>`
    token between `PR` and the verb, while STILL rejecting a URL merely
    discussed mid-sentence. The PR #303 incident outbox read literally
    `Done. PR #303 opened: <url>` and the old line-start anchor dropped it."""

    def test_done_clause_with_number_token_extracted(self):
        # The verbatim shape of the PR #303 incident outbox result.
        result = (
            'Done. PR #303 opened: https://github.com/Larry-Yatch/'
            'ourliberty-agent-core/pull/303'
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/303',
        )

    def test_result_label_clause_extracted(self):
        result = 'Result: PR opened: https://github.com/x/y/pull/5'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/5',
        )

    def test_number_token_with_updated_verb_extracted(self):
        result = 'PR #12 updated: https://github.com/x/y/pull/12'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/12',
        )

    def test_canonical_opened_unchanged(self):
        # Byte-for-byte identical group(1) to the prior regex.
        result = 'PR opened: https://github.com/x/y/pull/77\n\nDetails.'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/77',
        )

    def test_canonical_updated_unchanged(self):
        result = 'PR updated: https://github.com/x/y/pull/8\n\nAdded a commit.'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/8',
        )

    def test_clause_with_number_then_multiline_body(self):
        result = (
            'Done. PR #41 opened: https://github.com/x/y/pull/41\n\n'
            'Implemented the fix; all tests pass.\n- foo\n- bar'
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/41',
        )

    def test_mid_sentence_discussion_still_returns_none(self):
        # The Part A guarantee: a URL discussed mid-sentence (no clause
        # terminator directly before `PR`) MUST NOT match.
        result = (
            'I considered PR opened: https://github.com/x/y/pull/99 '
            'from last week, but built fresh instead.'
        )
        self.assertIsNone(on._extract_pr_url_from_build_result(result))


class PrUrlAmbiguousReturnsNoneTest(unittest.TestCase):
    """nervous-system-audit #16 (2026-06-05): _extract_pr_url_from_build_result
    refuses to guess when the build result names more than one DISTINCT PR
    URL. The prior first-match `.search` dispatched/merged the wrong PR when a
    stale line preceded the real one; last-match has the mirror failure. On
    ambiguity we return None (skip inline dispatch; reconcile / Larry
    resolves) rather than auto-dispatching the wrong PR in either direction.
    A single (possibly repeated-identical) URL is still returned."""

    def test_distinct_multiple_urls_returns_none(self):
        result = (
            'PR opened: https://github.com/x/y/pull/10\n'
            'On reflection I reworked the branch and opened a clean one.\n'
            'PR opened: https://github.com/x/y/pull/20'
        )
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

    def test_stale_after_real_also_returns_none(self):
        # The mirror of the original #16 hazard: real PR first (contract-
        # following), stale backreference after. Last-match would wrongly pick
        # the stale one; refuse-to-guess returns None.
        result = (
            'PR opened: https://github.com/x/y/pull/7\n'
            'This supersedes last week\'s attempt on:\n'
            'PR updated: https://github.com/x/y/pull/3'
        )
        self.assertIsNone(on._extract_pr_url_from_build_result(result))

    def test_single_match_unchanged(self):
        result = 'PR opened: https://github.com/x/y/pull/5\n\nDetails follow.'
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/5',
        )

    def test_repeated_identical_url_returns_it(self):
        # Same URL on two lines — a harmless echo, not ambiguous. Returned.
        result = (
            'PR opened: https://github.com/x/y/pull/9\n'
            'PR opened: https://github.com/x/y/pull/9'
        )
        self.assertEqual(
            on._extract_pr_url_from_build_result(result),
            'https://github.com/x/y/pull/9',
        )


class ReconcileMissedMirrorReviewsTest(unittest.TestCase):
    """fix-notifier-review-dispatch-reliability (Part B): the reconciliation
    sweep re-dispatches Mirror reviews the inline path dropped, bounded by an
    mtime window + idempotency, and never reviews a non-OPEN PR."""

    _PR = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/303'

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
            'ROUTING_EVENTS_LOG': swi.ROUTING_EVENTS_LOG,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'
        on.ensure_dirs()
        # Default: pretend every probed PR is OPEN. The gh layer is exercised
        # separately; here we want to assert dispatch behavior, not shell out.
        self._gh_open_orig = on._gh_pr_is_open
        self._gh_open_calls = []

        def _fake_open(repo_coords, pr_number):
            self._gh_open_calls.append((repo_coords, pr_number))
            return True
        on._gh_pr_is_open = _fake_open

    def tearDown(self):
        on._gh_pr_is_open = self._gh_open_orig
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        self._tmp.cleanup()

    def _write_archived_build(self, name='prod-built.json', *, task_id='prod-built',
                              result=None, recent=True, **overrides):
        archive_dir = on.OUTBOXES_ROOT / 'forge' / '.archive'
        archive_dir.mkdir(parents=True, exist_ok=True)
        if result is None:
            result = f'Done. PR #303 opened: {self._PR}'
        body = _good_outbox(
            agent='forge', source='beacon', task_id=task_id, phase='build',
            target_repo='ourliberty-agent-core', branch=f'forge/{task_id}',
            result=result,
        )
        body.update(overrides)
        f = archive_dir / name
        f.write_text(json.dumps(body))
        if not recent:
            old = time.time() - (on.RECONCILE_WINDOW_HOURS + 1) * 3600
            os.utime(f, (old, old))
        return f

    def test_happy_path_redispatches_missing_review(self):
        self._write_archived_build()
        on._reconcile_missed_mirror_reviews()
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(reviews), 1)
        review = json.loads(reviews[0].read_text())
        self.assertEqual(review['task_id'], 'prod-built')
        self.assertEqual(review['pr_url'], self._PR)
        self.assertEqual(review['phase'], 'review')
        # The non-canonical phrasing was extracted (Part A) AND re-dispatched.
        self.assertEqual(len(self._gh_open_calls), 1)

    def test_idempotent_when_review_in_inbox(self):
        self._write_archived_build()
        mirror_inbox = on.INBOXES_ROOT / 'mirror'
        mirror_inbox.mkdir(parents=True, exist_ok=True)
        (mirror_inbox / 'review-prod-built.json').write_text('{}')
        on._reconcile_missed_mirror_reviews()
        # The planted file is the only one; no fresh dispatch.
        reviews = list(mirror_inbox.glob('review-*.json'))
        self.assertEqual(len(reviews), 1)
        # Idempotency short-circuits BEFORE the gh open-state check.
        self.assertEqual(self._gh_open_calls, [])

    def test_idempotent_when_review_in_archive(self):
        self._write_archived_build()
        archived = on.INBOXES_ROOT / 'mirror' / '.archive'
        archived.mkdir(parents=True, exist_ok=True)
        (archived / 'review-prod-built.json').write_text('{}')
        on._reconcile_missed_mirror_reviews()
        live = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(live, [])
        self.assertEqual(self._gh_open_calls, [])

    def test_window_bound_skips_old_archive_entry(self):
        self._write_archived_build(recent=False)
        on._reconcile_missed_mirror_reviews()
        reviews = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(reviews, [])
        # Out-of-window entries are filtered before any gh call.
        self.assertEqual(self._gh_open_calls, [])

    def test_no_op_when_no_pr_url(self):
        # A build outbox with no PR URL is not a candidate — zero dispatch,
        # zero gh calls.
        self._write_archived_build(
            result='Hit a compile error; need clarification.',
        )
        on._reconcile_missed_mirror_reviews()
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')), [])
        self.assertEqual(self._gh_open_calls, [])

    def test_skips_closed_or_merged_pr(self):
        self._write_archived_build()
        on._gh_pr_is_open = lambda repo, num: False
        on._reconcile_missed_mirror_reviews()
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')), [])

    def test_skips_when_pr_state_unknown(self):
        self._write_archived_build()
        on._gh_pr_is_open = lambda repo, num: None
        on._reconcile_missed_mirror_reviews()
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')), [])

    def test_non_build_phase_ignored(self):
        self._write_archived_build(name='prefl.json', task_id='prefl',
                                   phase='preflight')
        on._reconcile_missed_mirror_reviews()
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')), [])
        self.assertEqual(self._gh_open_calls, [])

    def test_missing_target_repo_ignored(self):
        f = self._write_archived_build(name='no-repo.json', task_id='no-repo')
        body = json.loads(f.read_text())
        body.pop('target_repo', None)
        f.write_text(json.dumps(body))
        on._reconcile_missed_mirror_reviews()
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')), [])

    def test_no_archive_dir_is_noop(self):
        # Nothing archived yet — sweep must not raise or dispatch.
        on._reconcile_missed_mirror_reviews()
        self.assertEqual(
            list((on.INBOXES_ROOT / 'mirror').glob('review-*.json')), [])


class ReviewRequestChainEventTest(unittest.TestCase):
    """forge-queue-in-review-lane: review dispatches push-emit a
    `review_request` chain_event (agent='forge') so the dashboard's
    in_review lane populates. The emit fires ONLY on a successful inbox
    write — idempotent skips and rejected dispatches emit nothing — and
    an emit failure never breaks the dispatch itself."""

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
        self.captured: list[dict] = []

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        self._tmp.cleanup()

    def _fake_emit(self, **kwargs):
        self.captured.append(kwargs)
        return True

    def _data(self):
        return {
            'task_id': 'real-rr1',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-rr1',
        }

    PR = 'https://github.com/x/y/pull/9'

    def test_first_dispatch_emits_review_request(self):
        with mock.patch.object(on.chain_event_emit, 'emit_event',
                               self._fake_emit):
            on._dispatch_mirror_review(self._data(), self.PR)
        # The dispatch itself wrote the review task...
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))), 1)
        # ...and exactly one review_request event rode along.
        self.assertEqual(len(self.captured), 1)
        ev = self.captured[0]
        self.assertEqual(ev['event_type'], 'review_request')
        self.assertEqual(ev['agent'], 'forge')
        self.assertEqual(ev['task_id'], 'real-rr1')
        self.assertEqual(ev['pr_url'], self.PR)
        self.assertEqual(ev['payload']['revision_count'], 0)
        self.assertEqual(ev['payload']['replan_count'], 0)

    def test_rerun_dispatch_emits_with_round_number(self):
        data = self._data()
        data['pr_url'] = self.PR
        with mock.patch.object(on.chain_event_emit, 'emit_event',
                               self._fake_emit):
            on._dispatch_mirror_review_rerun(data, 2, 'fixed the findings')
        self.assertEqual(len(self.captured), 1)
        ev = self.captured[0]
        self.assertEqual(ev['event_type'], 'review_request')
        self.assertEqual(ev['agent'], 'forge')
        self.assertEqual(ev['pr_url'], self.PR)
        self.assertEqual(ev['payload']['revision_count'], 2)

    def test_dispatch_carries_origin_task_id_onto_envelope_and_event(self):
        # Delegate-tracking Slice 2a: origin_task_id rides both the written
        # Mirror review envelope (→ Mirror's outbox → the verdict) and the
        # review_request event (the join key the dashboard reads).
        data = self._data()
        data['origin_task_id'] = 'delegate-cap-x-ab12'
        with mock.patch.object(on.chain_event_emit, 'emit_event',
                               self._fake_emit):
            on._dispatch_mirror_review(data, self.PR)
        files = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(files), 1)
        env = json.loads(files[0].read_text())
        self.assertEqual(env.get('origin_task_id'), 'delegate-cap-x-ab12')
        self.assertEqual(env.get('task_id'), 'real-rr1')  # routing id untouched
        self.assertEqual(self.captured[0]['payload'].get('origin_task_id'),
                         'delegate-cap-x-ab12')

    def test_dispatch_omits_origin_task_id_when_absent(self):
        with mock.patch.object(on.chain_event_emit, 'emit_event',
                               self._fake_emit):
            on._dispatch_mirror_review(self._data(), self.PR)
        files = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        env = json.loads(files[0].read_text())
        self.assertNotIn('origin_task_id', env)
        self.assertNotIn('origin_task_id', self.captured[0]['payload'])

    def test_rerun_dispatch_carries_origin_task_id(self):
        data = self._data()
        data['pr_url'] = self.PR
        data['origin_task_id'] = 'delegate-cap-x-ab12'
        with mock.patch.object(on.chain_event_emit, 'emit_event',
                               self._fake_emit):
            on._dispatch_mirror_review_rerun(data, 2, 'fixed the findings')
        files = list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))
        self.assertEqual(len(files), 1)
        env = json.loads(files[0].read_text())
        self.assertEqual(env.get('origin_task_id'), 'delegate-cap-x-ab12')
        self.assertEqual(self.captured[0]['payload'].get('origin_task_id'),
                         'delegate-cap-x-ab12')

    def test_idempotent_skip_does_not_emit(self):
        inbox = on.INBOXES_ROOT / 'mirror'
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / 'review-real-rr1.json').write_text('{}')
        with mock.patch.object(on.chain_event_emit, 'emit_event',
                               self._fake_emit):
            on._dispatch_mirror_review(self._data(), self.PR)
        self.assertEqual(self.captured, [])

    def test_rejected_dispatch_does_not_emit(self):
        with mock.patch.object(
            swi, 'safe_write_inbox',
            side_effect=swi.DispatchRejected('denied by validator'),
        ), mock.patch.object(on.chain_event_emit, 'emit_event',
                             self._fake_emit):
            on._dispatch_mirror_review(self._data(), self.PR)
        self.assertEqual(self.captured, [])

    def test_emit_failure_does_not_break_dispatch(self):
        def _raise(**_):
            raise RuntimeError('supabase blew up')

        with mock.patch.object(on.chain_event_emit, 'emit_event', _raise):
            # Must not propagate — daemon-never-wedge.
            on._dispatch_mirror_review(self._data(), self.PR)
        self.assertEqual(
            len(list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))), 1)


class ClarifyExhaustedSignalTest(unittest.TestCase):
    """§5.2: a CLARIFY-exhausted Forge build writes a self-clearing for-Larry
    record; any other classified marker for the same task self-clears it."""

    def setUp(self):
        import for_larry_signal as fls
        self.fls = fls
        self._dir = tempfile.TemporaryDirectory()
        self.signal = Path(self._dir.name) / 'for-larry-escalations.json'
        self._prev = os.environ.get('OURLIBERTY_FOR_LARRY_SIGNAL_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = str(self.signal)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_SIGNAL_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = self._prev
        self._dir.cleanup()

    def _keys(self):
        return {e['key'] for e in self.fls.active_entries()}

    def test_exhausted_writes_record(self):
        on._sync_clarify_exhausted_signal(
            {'task_id': 'zz-fixture-t1', 'target_repo': 'agent-core'},
            {'intent': 'clarification-exhausted',
             'payload': {'question': 'which API?'}},
        )
        key = self.fls.CLARIFY_EXHAUSTED_KEY_PREFIX + 'zz-fixture-t1'
        self.assertEqual(self._keys(), {key})
        rec = self.fls.load_records()[key]
        self.assertIn('which API?', rec['suggested_action'])
        self.assertEqual(rec['repo'], 'agent-core')

    def test_other_marker_clears_record(self):
        data = {'task_id': 'zz-fixture-t1', 'target_repo': 'agent-core'}
        on._sync_clarify_exhausted_signal(
            data, {'intent': 'clarification-exhausted',
                   'payload': {'question': 'q?'}})
        self.assertTrue(self._keys())
        # A subsequent classified marker (e.g. a fresh proceed) self-clears it.
        on._sync_clarify_exhausted_signal(data, {'intent': 'proceed'})
        self.assertEqual(self._keys(), set())

    def test_missing_task_id_is_noop(self):
        on._sync_clarify_exhausted_signal(
            {}, {'intent': 'clarification-exhausted'})
        self.assertFalse(self.signal.exists())

    def test_exhausted_without_question_uses_placeholder(self):
        on._sync_clarify_exhausted_signal(
            {'task_id': 'zz-fixture-t2'},
            {'intent': 'clarification-exhausted'},
        )
        key = self.fls.CLARIFY_EXHAUSTED_KEY_PREFIX + 'zz-fixture-t2'
        rec = self.fls.load_records()[key]
        self.assertIn('no question text recorded', rec['suggested_action'])


class PostMergeBaselineWarmTest(unittest.TestCase):
    """regression-gate-steady-state-warmer (spec PR 2) — the post-merge warmer.

    Asserts the warm hook (a) fires from BOTH auto-merge success branches with
    the correct FETCH_HEAD/repo-root argv + canonical OL_REGRESSION_BASELINE_DIR
    env, (b) is non-blocking + detached, (c) isolates any spawn error from the
    merge outcome, and (d) does NOT fire when the merge failed.
    """

    PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/77'
    TASK_ID = 'warmer-fixture-001'

    def setUp(self):
        # The post-merge warm spawn no-ops when REGBASELINE_WARMING=1 (the
        # re-entrancy guard). These fixtures assert the REAL spawn-command
        # construction, so clear the flag in case this suite is itself running
        # inside a warm's discover pass (which exports it).
        self._saved_warming = os.environ.pop('REGBASELINE_WARMING', None)

    def tearDown(self):
        if self._saved_warming is not None:
            os.environ['REGBASELINE_WARMING'] = self._saved_warming

    def _mock_proc(self, *, returncode=0, stdout='', stderr=''):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    # --- _spawn_post_merge_baseline_warm in isolation -----------------------

    def test_spawn_command_and_canonical_env(self):
        with mock.patch.object(on.subprocess, 'Popen') as m_popen:
            on._spawn_post_merge_baseline_warm(self.TASK_ID, self.PR_URL)
        m_popen.assert_called_once()
        args, kwargs = m_popen.call_args
        argv = args[0]
        self.assertEqual(argv[:2], ['bash', '-c'])
        shell = argv[2]
        # Fetch precedes warm (the new HEAD must be a local object first).
        self.assertIn('fetch --quiet origin main', shell)
        self.assertIn('regression_baseline_cache.py', shell)
        self.assertIn('warm', shell)
        self.assertIn('--sha FETCH_HEAD', shell)
        self.assertIn(f'--repo-root {on._WARM_REPO_ROOT}', shell)
        self.assertIn(f'--timeout-per-sha {on._WARM_TIMEOUT_PER_SHA_S}', shell)
        self.assertLess(
            shell.index('fetch'), shell.index('warm'),
            'fetch must run before warm in the detached child',
        )
        env = kwargs['env']
        self.assertEqual(
            env['OL_REGRESSION_BASELINE_DIR'],
            on.REGRESSION_BASELINE_CANONICAL_DIR,
        )

    def test_spawn_is_detached_and_nonblocking(self):
        with mock.patch.object(on.subprocess, 'Popen') as m_popen:
            on._spawn_post_merge_baseline_warm(self.TASK_ID, self.PR_URL)
        _, kwargs = m_popen.call_args
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdout'], on.subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], on.subprocess.DEVNULL)
        self.assertEqual(kwargs['stdin'], on.subprocess.DEVNULL)

    def test_spawn_error_is_swallowed(self):
        with mock.patch.object(on.subprocess, 'Popen',
                               side_effect=OSError('cannot fork')):
            # Must not raise — a warmer error can never touch the merge path.
            on._spawn_post_merge_baseline_warm(self.TASK_ID, self.PR_URL)

    def test_spawn_skips_when_reentrant(self):
        # Guard against the regbaseline fork-bomb: when already warming, the
        # post-merge spawn must NOT fork another production warm.
        os.environ['REGBASELINE_WARMING'] = '1'
        try:
            with mock.patch.object(on.subprocess, 'Popen') as m_popen:
                on._spawn_post_merge_baseline_warm(self.TASK_ID, self.PR_URL)
            m_popen.assert_not_called()
        finally:
            os.environ.pop('REGBASELINE_WARMING', None)

    # --- integration with _auto_merge_pr success branches -------------------

    def test_merged_branch_fires_warm(self):
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on, '_spawn_post_merge_baseline_warm') as m_warm:
            m_run.return_value = self._mock_proc(returncode=0)
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'merged')
        m_warm.assert_called_once_with(self.TASK_ID, self.PR_URL)

    def test_already_merged_branch_fires_warm(self):
        merge_proc = self._mock_proc(returncode=1, stderr='already merged')
        view_proc = self._mock_proc(
            returncode=0, stdout=json.dumps({'state': 'MERGED'}),
        )
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on, '_spawn_post_merge_baseline_warm') as m_warm:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'already_merged')
        m_warm.assert_called_once_with(self.TASK_ID, self.PR_URL)

    def test_failed_merge_does_not_fire_warm(self):
        merge_proc = self._mock_proc(returncode=1, stderr='not mergeable')
        view_proc = self._mock_proc(
            returncode=0, stdout=json.dumps({'state': 'OPEN'}),
        )
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on, '_spawn_post_merge_baseline_warm') as m_warm:
            m_run.side_effect = [merge_proc, view_proc]
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'failed')
        m_warm.assert_not_called()

    def test_warmer_spawn_failure_leaves_merge_merged(self):
        """End-to-end: a real warm whose Popen raises must not change the
        merge outcome — the swallow happens inside the warm helper."""
        with mock.patch.object(on.subprocess, 'run') as m_run, \
                mock.patch.object(on.subprocess, 'Popen',
                                  side_effect=OSError('cannot fork')):
            m_run.return_value = self._mock_proc(returncode=0)
            result = on._auto_merge_pr(self.PR_URL, self.TASK_ID)
        self.assertEqual(result['merge_outcome'], 'merged')


class CanonicalBaselineDirAgreementTest(unittest.TestCase):
    """The warmer and the Mirror review gate MUST resolve the SAME cache dir —
    the fix is inert otherwise (spec PR 2 correctness lever)."""

    def test_warmer_and_gate_constants_match(self):
        import agent_runner
        self.assertEqual(
            on.REGRESSION_BASELINE_CANONICAL_DIR,
            agent_runner.REGRESSION_BASELINE_CANONICAL_DIR,
        )

    def test_cache_module_resolves_canonical_dir(self):
        import regression_baseline_cache as rbc
        with mock.patch.dict(
            os.environ,
            {'OL_REGRESSION_BASELINE_DIR': on.REGRESSION_BASELINE_CANONICAL_DIR},
        ):
            self.assertEqual(
                str(rbc.baseline_dir()),
                on.REGRESSION_BASELINE_CANONICAL_DIR,
            )

    def test_gate_pin_only_for_mirror_review(self):
        import agent_runner
        self.assertTrue(agent_runner._is_mirror_review_dispatch('review', 'mirror'))
        self.assertFalse(agent_runner._is_mirror_review_dispatch('build', 'forge'))
        self.assertFalse(agent_runner._is_mirror_review_dispatch('review', 'forge'))
        self.assertFalse(agent_runner._is_mirror_review_dispatch('preflight', 'mirror'))


if __name__ == '__main__':
    unittest.main()
