#!/usr/bin/env python3
"""Fixtures for `beacon_approval_handler` — approval-gate state machine.

Phase D3 commit 3 (`D3-approval`). Covers user-reply pattern detection
(strict whitelist), marker extraction from Beacon's response (with
malformed-JSON + missing-field rejections), pending-state CRUD, pause/
resume flag ops, reminder schedule (6h/24h/72h with dedup), trust-policy
bridge, dispatch via safe_write_inbox, and all DM formatters.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_beacon_approval_handler
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import beacon_approval_handler as ah  # noqa: E402
import routing_validator as rv          # noqa: E402
import safe_write_inbox as swi          # noqa: E402
import trust_policy as tp               # noqa: E402


class ParseUserReplyTest(unittest.TestCase):
    """Strict whitelist on positive confirmation; prefix grammar on modify/reject."""

    def test_approve_tokens_fire(self):
        for token in ['approve', 'Approve', 'APPROVE', 'yes', 'go', 'ok', 'okay', 'ship', 'ship it', '  yes  ']:
            r = ah.parse_user_reply(token)
            self.assertEqual(r['action'], 'approve', f'token={token!r} should approve')

    def test_ambiguous_text_does_not_approve(self):
        for text in [
            'looks good, approve',           # extra context
            'approve if it is safe',
            'yes please',
            'yeah',
            'do it',
            'sounds approved',
            'go for it but check X first',
        ]:
            r = ah.parse_user_reply(text)
            self.assertEqual(r['action'], 'none', f'text={text!r} should bounce')

    def test_modify_prefix(self):
        r = ah.parse_user_reply('modify: use camelCase instead')
        self.assertEqual(r['action'], 'modify')
        self.assertEqual(r['reason'], 'use camelCase instead')

    def test_modify_case_insensitive_with_spaces(self):
        r = ah.parse_user_reply('  Modify :   change the target repo  ')
        self.assertEqual(r['action'], 'modify')
        self.assertEqual(r['reason'], 'change the target repo')

    def test_reject_prefix(self):
        r = ah.parse_user_reply('reject: not now')
        self.assertEqual(r['action'], 'reject')
        self.assertEqual(r['reason'], 'not now')

    def test_pause_commands(self):
        for cmd in ['pause', '/pause', ' PAUSE ', '/Pause']:
            r = ah.parse_user_reply(cmd)
            self.assertEqual(r['action'], 'pause', f'cmd={cmd!r}')

    def test_resume_commands(self):
        for cmd in ['resume', '/resume', '  Resume  ']:
            r = ah.parse_user_reply(cmd)
            self.assertEqual(r['action'], 'resume', f'cmd={cmd!r}')

    def test_non_command_returns_none(self):
        for text in ['hello', 'what is the plan?', '', '   ', None, 42]:
            r = ah.parse_user_reply(text)
            self.assertEqual(r['action'], 'none')


class ExtractApprovalRequestTest(unittest.TestCase):
    """Marker extraction. Strict on missing fields + malformed JSON."""

    def test_extracts_valid_marker(self):
        response = (
            "Here's the plan:\n\n"
            "I'll enable the watchdog timer.\n\n"
            "=== APPROVAL_REQUEST ===\n"
            '{"task_id": "watchdog-001", "summary": "Enable watchdog timer", '
            '"target_agent": "forge", "prompt": "Enable ourliberty-watchdog.timer..."}\n'
            "=== END_APPROVAL_REQUEST ===\n\n"
            "Awaiting your call."
        )
        payload, narrative = ah.extract_approval_request(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['task_id'], 'watchdog-001')
        # Narrative has marker stripped
        self.assertNotIn('APPROVAL_REQUEST', narrative)
        self.assertIn("Here's the plan", narrative)
        self.assertIn('Awaiting your call', narrative)

    def test_no_marker_returns_unchanged(self):
        response = "Just a regular reply, no approval needed."
        payload, narrative = ah.extract_approval_request(response)
        self.assertIsNone(payload)
        self.assertEqual(narrative, response)

    def test_malformed_json_raises(self):
        response = (
            "=== APPROVAL_REQUEST ===\n"
            '{not valid json}\n'
            "=== END_APPROVAL_REQUEST ===\n"
        )
        with self.assertRaises(ah.MalformedApprovalMarker):
            ah.extract_approval_request(response)

    def test_missing_required_field_raises(self):
        response = (
            "=== APPROVAL_REQUEST ===\n"
            '{"task_id": "t-1", "summary": "x", "target_agent": "forge"}\n'
            "=== END_APPROVAL_REQUEST ===\n"
        )
        with self.assertRaises(ah.MalformedApprovalMarker) as ctx:
            ah.extract_approval_request(response)
        self.assertIn('prompt', str(ctx.exception))

    def test_empty_response_safe(self):
        payload, narrative = ah.extract_approval_request('')
        self.assertIsNone(payload)
        self.assertEqual(narrative, '')


def _good_payload(**overrides):
    p = {
        'task_id': 'task-001',
        'summary': 'A plan summary',
        'target_agent': 'forge',
        'prompt': 'x' * 200,
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'feature-development',
    }
    p.update(overrides)
    return p


class StateCrudTest(unittest.TestCase):
    """Pending-approvals state file: add, find, resolve, history cap."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._original_path = ah.PENDING_APPROVALS_PATH
        ah.PENDING_APPROVALS_PATH = self._root / 'pending.json'

    def tearDown(self):
        ah.PENDING_APPROVALS_PATH = self._original_path
        self._tmp.cleanup()

    def test_add_pending_persists(self):
        entry = ah.add_pending(_good_payload(task_id='t-1'), chat_id=999)
        self.assertEqual(entry['status'], 'pending')
        self.assertEqual(entry['chat_id'], 999)
        state = ah.load_state()
        self.assertEqual(len(state['pending']), 1)
        self.assertEqual(state['pending'][0]['id'], 't-1')

    def test_find_by_id(self):
        ah.add_pending(_good_payload(task_id='t-a'), chat_id=1)
        ah.add_pending(_good_payload(task_id='t-b'), chat_id=1)
        self.assertIsNotNone(ah.find_pending_by_id('t-b'))
        self.assertIsNone(ah.find_pending_by_id('t-none'))

    def test_most_recent_pending(self):
        ah.add_pending(_good_payload(task_id='t-old'), chat_id=1)
        # Hack the created_at to make a clear ordering
        s = ah.load_state()
        s['pending'][0]['created_at'] = '2026-05-11T20:00:00Z'
        ah.save_state(s)
        ah.add_pending(_good_payload(task_id='t-new'), chat_id=1)
        recent = ah.most_recent_pending()
        self.assertEqual(recent['id'], 't-new')

    def test_most_recent_ignores_paused_during_creation(self):
        ah.add_pending(_good_payload(task_id='t-paused'),
                          chat_id=1, queued_during_pause=True)
        self.assertIsNone(ah.most_recent_pending())
        ah.add_pending(_good_payload(task_id='t-normal'), chat_id=1)
        self.assertEqual(ah.most_recent_pending()['id'], 't-normal')

    def test_resolve_moves_to_history(self):
        ah.add_pending(_good_payload(task_id='t-r'), chat_id=1)
        moved = ah.resolve('t-r', 'approved', note='looks good')
        self.assertEqual(moved['status'], 'approved')
        self.assertEqual(moved['resolution_note'], 'looks good')
        state = ah.load_state()
        self.assertEqual(state['pending'], [])
        self.assertEqual(len(state['history']), 1)

    def test_resolve_invalid_status_raises(self):
        ah.add_pending(_good_payload(task_id='t-r'), chat_id=1)
        with self.assertRaises(ValueError):
            ah.resolve('t-r', 'bogus')

    def test_resolve_unknown_id_returns_none(self):
        self.assertIsNone(ah.resolve('t-missing', 'approved'))

    def test_pop_paused_backlog(self):
        ah.add_pending(_good_payload(task_id='t1'), 1, queued_during_pause=True)
        ah.add_pending(_good_payload(task_id='t2'), 1, queued_during_pause=True)
        ah.add_pending(_good_payload(task_id='t3'), 1)  # normal
        backlog = ah.pop_paused_backlog()
        self.assertEqual(len(backlog), 2)
        # The two paused entries should now have queued_during_pause = False
        state = ah.load_state()
        for entry in state['pending']:
            self.assertFalse(entry.get('queued_during_pause'))

    def test_history_capped(self):
        for i in range(ah.HISTORY_CAP + 50):
            ah.add_pending(_good_payload(task_id=f't-{i}'), 1)
            ah.resolve(f't-{i}', 'approved')
        state = ah.load_state()
        self.assertLessEqual(len(state['history']), ah.HISTORY_CAP)


class PauseFlagTest(unittest.TestCase):
    """File-flag based pause."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._original = ah.APPROVALS_PAUSED_FLAG
        ah.APPROVALS_PAUSED_FLAG = self._root / 'APPROVALS_PAUSED'

    def tearDown(self):
        ah.APPROVALS_PAUSED_FLAG = self._original
        self._tmp.cleanup()

    def test_is_paused_default_false(self):
        self.assertFalse(ah.is_paused())

    def test_set_and_clear(self):
        ah.set_paused(True)
        self.assertTrue(ah.is_paused())
        ah.set_paused(False)
        self.assertFalse(ah.is_paused())

    def test_set_paused_false_when_already_clear_is_safe(self):
        ah.set_paused(False)  # idempotent
        self.assertFalse(ah.is_paused())


class TrustDecisionTest(unittest.TestCase):
    """Bridge to trust_policy.evaluate with marker payload shape."""

    def setUp(self):
        # Empty runtime policy → force_ask default.
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._original_runtime = tp.RUNTIME_POLICY_PATH
        self._original_repo = tp.REPO_POLICY_PATH
        tp.RUNTIME_POLICY_PATH = self._root / 'runtime.json'
        tp.REPO_POLICY_PATH = self._root / 'repo.json'

    def tearDown(self):
        tp.RUNTIME_POLICY_PATH = self._original_runtime
        tp.REPO_POLICY_PATH = self._original_repo
        self._tmp.cleanup()

    def test_empty_policy_forces_ask(self):
        action, rule = ah.trust_decision(_good_payload())
        self.assertEqual(action, 'force_ask')

    def test_auto_approve_rule_fires(self):
        tp.RUNTIME_POLICY_PATH.write_text(json.dumps({
            'version': 1, 'default_action': 'force_ask',
            'rules': [{
                'source': 'beacon', 'target': 'forge',
                'task_type': 'doc-only',
                'repos': ['ourliberty-agent-core'],
                'action': 'auto_approve',
            }],
        }))
        action, rule = ah.trust_decision(_good_payload(task_type='doc-only'))
        self.assertEqual(action, 'auto_approve')

    def test_reject_rule_fires(self):
        tp.RUNTIME_POLICY_PATH.write_text(json.dumps({
            'version': 1, 'default_action': 'force_ask',
            'rules': [{
                'repos': ['TruPath-*'], 'action': 'reject',
            }],
        }))
        action, rule = ah.trust_decision(_good_payload(target_repo='TruPath-website'))
        self.assertEqual(action, 'reject')


class ReminderScheduleTest(unittest.TestCase):
    """6h / 24h / 72h schedule with dedup + pause suppression."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._original_state = ah.PENDING_APPROVALS_PATH
        self._original_flag = ah.APPROVALS_PAUSED_FLAG
        ah.PENDING_APPROVALS_PATH = self._root / 'pending.json'
        ah.APPROVALS_PAUSED_FLAG = self._root / 'APPROVALS_PAUSED'

    def tearDown(self):
        ah.PENDING_APPROVALS_PATH = self._original_state
        ah.APPROVALS_PAUSED_FLAG = self._original_flag
        self._tmp.cleanup()

    def test_no_reminders_for_fresh_entry(self):
        ah.add_pending(_good_payload(task_id='t-1'), 1)
        due = ah.due_reminders()
        self.assertEqual(due, [])

    def test_six_hour_reminder_fires_after_threshold(self):
        ah.add_pending(_good_payload(task_id='t-1'), 1)
        # Backdate to 7h ago
        s = ah.load_state()
        s['pending'][0]['created_at'] = (
            datetime.now(timezone.utc) - timedelta(hours=7)
        ).isoformat()
        ah.save_state(s)
        due = ah.due_reminders()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][1], 6)  # 6h threshold matched

    def test_reminder_dedup_via_record(self):
        ah.add_pending(_good_payload(task_id='t-1'), 1)
        s = ah.load_state()
        s['pending'][0]['created_at'] = (
            datetime.now(timezone.utc) - timedelta(hours=7)
        ).isoformat()
        ah.save_state(s)
        ah.record_reminder_sent('t-1', 6)
        due = ah.due_reminders()
        self.assertEqual(due, [], 'already-sent reminder should not refire')

    def test_higher_threshold_fires_when_age_exceeds_it(self):
        ah.add_pending(_good_payload(task_id='t-1'), 1)
        s = ah.load_state()
        s['pending'][0]['created_at'] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
        # Already sent the 6h reminder
        s['pending'][0]['reminders_sent'] = [6]
        ah.save_state(s)
        due = ah.due_reminders()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][1], 24)

    def test_pause_suppresses_reminders(self):
        ah.add_pending(_good_payload(task_id='t-1'), 1)
        s = ah.load_state()
        s['pending'][0]['created_at'] = (
            datetime.now(timezone.utc) - timedelta(hours=7)
        ).isoformat()
        ah.save_state(s)
        ah.set_paused(True)
        try:
            due = ah.due_reminders()
            self.assertEqual(due, [])
        finally:
            ah.set_paused(False)

    def test_paused_during_creation_entries_skip_reminders(self):
        ah.add_pending(_good_payload(task_id='t-1'), 1, queued_during_pause=True)
        s = ah.load_state()
        s['pending'][0]['created_at'] = (
            datetime.now(timezone.utc) - timedelta(hours=7)
        ).isoformat()
        ah.save_state(s)
        due = ah.due_reminders()
        self.assertEqual(due, [])


class DmFormatterTest(unittest.TestCase):
    """Sanity checks on the user-facing strings."""

    def test_approval_dm_contains_summary_and_footer(self):
        entry = {
            'id': 't-1',
            'plan_summary': 'Enable watchdog timer',
            'dispatch_payload': {
                'target_agent': 'forge',
                'target_repo': 'ourliberty-agent-core',
                'task_type': 'doc-only',
            },
        }
        msg = ah.format_approval_dm(entry)
        self.assertIn('t-1', msg)
        self.assertIn('Enable watchdog timer', msg)
        self.assertIn('approve / yes / go / ok / ship it', msg)
        self.assertIn('modify:', msg)
        self.assertIn('reject:', msg)
        self.assertIn('ourliberty-agent-core', msg)

    def test_reminder_includes_age(self):
        entry = {'id': 't-1', 'plan_summary': 'X'}
        msg = ah.format_reminder_dm(entry, 24)
        self.assertIn('24h', msg)
        self.assertIn('t-1', msg)

    def test_bounce_includes_grammar(self):
        msg = ah.format_bounce()
        self.assertIn('approve / yes / go / ok / ship it', msg)


class DispatchApprovedTest(unittest.TestCase):
    """Integration: dispatch_approved invokes safe_write_inbox correctly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
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

    def tearDown(self):
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def test_dispatch_to_forge(self):
        entry = {
            'id': 't-dispatch',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(task_id='t-dispatch'),
        }
        dest = ah.dispatch_approved(entry)
        self.assertTrue(dest.exists())
        self.assertEqual(dest.parent.name, 'forge')
        # Verify source was set to 'beacon' (overriding any payload value)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'beacon')

    def test_dispatch_to_invalid_route_raises(self):
        # forge is target — fine. But what if payload says mirror?
        # Pulse->forge would be wrong source — let's test pulse->forge denial.
        entry = {
            'id': 't-bad',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(task_id='t-bad'),
        }
        # Hack: force the payload to have a non-beacon source — but the
        # dispatch_approved override should still set source=beacon. So
        # this would PASS routing. To test denial we'd need to invoke
        # safe_write_inbox directly with pulse source. Covered by
        # test_safe_write_inbox.test_routing_denied_raises. Just verify
        # the override behavior here.
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'beacon')

    def test_dispatch_propagates_chat_id_as_reply_chat_id(self):
        # Bug A fix (D3.5 5a-followup): entry's chat_id must land in the
        # dispatched task as reply_chat_id so the downstream chain can
        # auto-DM Larry on terminal markers.
        entry = {
            'id': 't-chat',
            'target_agent': 'forge',
            'chat_id': 7998341473,
            'dispatch_payload': _good_payload(task_id='t-chat'),
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['reply_chat_id'], 7998341473)

    def test_dispatch_omits_reply_chat_id_when_chat_id_absent(self):
        # Defensive: entries without chat_id (autonomous Pulse-initiated
        # dispatches, future shapes) must NOT inject a falsy reply_chat_id.
        entry = {
            'id': 't-no-chat',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(task_id='t-no-chat'),
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertNotIn('reply_chat_id', data)


# -------------------- D3.5 5c — replan budget + discipline --------------------


class EvaluateReplanBudgetTest(unittest.TestCase):
    """Symmetric with mirror_review_handler.evaluate_revision_budget tests."""

    def test_allow_at_zero(self):
        decision, next_count, max_count = ah.evaluate_replan_budget(
            {'replan_count': 0, 'max_replans': 2}
        )
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 1)
        self.assertEqual(max_count, 2)

    def test_allow_at_one_of_two(self):
        decision, next_count, max_count = ah.evaluate_replan_budget(
            {'replan_count': 1, 'max_replans': 2}
        )
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 2)
        self.assertEqual(max_count, 2)

    def test_exhausted_at_two_of_two(self):
        decision, next_count, max_count = ah.evaluate_replan_budget(
            {'replan_count': 2, 'max_replans': 2}
        )
        self.assertEqual(decision, 'exhausted')
        self.assertEqual(next_count, 3)
        self.assertEqual(max_count, 2)

    def test_defaults_when_envelope_missing_fields(self):
        decision, next_count, max_count = ah.evaluate_replan_budget({})
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 1)
        self.assertEqual(max_count, ah.DEFAULT_MAX_REPLANS)

    def test_negative_or_non_int_treated_as_zero(self):
        decision, next_count, _ = ah.evaluate_replan_budget(
            {'replan_count': -1, 'max_replans': 'oops'}
        )
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 1)


class ValidateReplanDisciplineTest(unittest.TestCase):
    """Level-3 (medium) gate on Beacon's replan APPROVAL_REQUEST."""

    def _payload(self, **overrides):
        p = {
            'task_id': 'task-001',
            'summary': (
                'Address Mirror\'s concern about missing input validation '
                'in the parser by adding explicit boundary checks.'
            ),
            'target_agent': 'forge',
            'prompt': 'x' * 200,
        }
        p.update(overrides)
        return p

    def test_passes_when_task_id_matches_and_reason_referenced(self):
        envelope = {'task_id': 'task-001'}
        mirror_reason = 'Missing input validation in the parser.'
        ok, err = ah.validate_replan_discipline(
            self._payload(), envelope, mirror_reason,
        )
        self.assertTrue(ok, f'gate should pass but got: {err}')

    def test_task_id_mismatch_fails(self):
        envelope = {'task_id': 'task-002'}
        ok, err = ah.validate_replan_discipline(
            self._payload(task_id='task-001'),
            envelope, 'missing input validation',
        )
        self.assertFalse(ok)
        self.assertIn('task_id', err)

    def test_no_reason_overlap_fails(self):
        envelope = {'task_id': 'task-001'}
        payload = self._payload(
            summary='Implement entirely unrelated feature X.',
        )
        ok, err = ah.validate_replan_discipline(
            payload, envelope, 'missing input validation in the parser',
        )
        self.assertFalse(ok)
        self.assertIn('Mirror', err)

    def test_case_insensitive_reason_match(self):
        envelope = {'task_id': 'task-001'}
        payload = self._payload(
            summary='MISSING VALIDATION addressed by adding checks.',
        )
        ok, err = ah.validate_replan_discipline(
            payload, envelope, 'missing input validation',
        )
        self.assertTrue(ok, f'case-insensitive should pass: {err}')

    def test_empty_mirror_reason_skips_check_two(self):
        # Defensive fallback: no baseline → second check auto-passes.
        envelope = {'task_id': 'task-001'}
        payload = self._payload(summary='Unrelated content.')
        ok, _ = ah.validate_replan_discipline(payload, envelope, '')
        self.assertTrue(ok)

    def test_short_mirror_reason_adapts_threshold(self):
        """Med-8 review fix: when Mirror's reason is terse (<2 long tokens),
        the gate must adapt the threshold to len(mirror_tokens) — otherwise
        Beacon's good-faith summary always fails."""
        envelope = {'task_id': 'task-001'}
        # Mirror reason has only 1 >3-char token ("spec")
        payload = self._payload(
            summary='Address the spec gap by adding a clarifying example.',
        )
        ok, err = ah.validate_replan_discipline(
            payload, envelope, 'Spec gap.',
        )
        self.assertTrue(ok, f'short mirror reason should adapt threshold: {err}')

    def test_short_mirror_reason_still_requires_overlap(self):
        """Med-8 review fix corollary: when Mirror's reason is terse but
        Beacon's summary has zero token overlap, gate still fails."""
        envelope = {'task_id': 'task-001'}
        payload = self._payload(
            summary='Implement unrelated feature X with new module Y.',
        )
        ok, _ = ah.validate_replan_discipline(
            payload, envelope, 'Spec gap.',
        )
        self.assertFalse(ok)

    def test_envelope_notify_prefix_is_stripped_before_compare(self):
        """5c-followup fix: production envelopes carry task_id='notify-<orig>'
        (marker-routing block prefixes it for filename disambiguation).
        Beacon's APPROVAL_REQUEST uses the ORIGINAL task_id per her Shape 8.
        The discipline gate must strip the prefix before comparing — strict
        equality would reject every legitimate replan. Surfaced by Phase 2
        live smoke 2026-05-14."""
        envelope = {'task_id': 'notify-task-001'}
        payload = self._payload(task_id='task-001')
        ok, err = ah.validate_replan_discipline(
            payload, envelope, 'Missing input validation in the parser.',
        )
        self.assertTrue(
            ok, f'gate should pass after stripping notify- prefix: {err}',
        )

    def test_envelope_double_notify_prefix_only_strips_one(self):
        """5c-followup fix corollary: only one `notify-` prefix is stripped,
        not arbitrary repeats. A pathological `notify-notify-task-001`
        envelope (shouldn't happen in production but defensive) compares
        as `notify-task-001` after stripping, which won't match payload's
        `task-001`. Gate correctly rejects."""
        envelope = {'task_id': 'notify-notify-task-001'}
        payload = self._payload(task_id='task-001')
        ok, _ = ah.validate_replan_discipline(
            payload, envelope, 'Missing input validation.',
        )
        self.assertFalse(ok)

    def test_short_tokens_filtered_out(self):
        # "of", "the", "in" — these <4-char words don't count as overlap.
        envelope = {'task_id': 'task-001'}
        payload = self._payload(
            summary='Fix the of in or by but at on as.'
        )
        ok, err = ah.validate_replan_discipline(
            payload, envelope, 'the parser of the foo in the bar.',
        )
        self.assertFalse(ok)


class AddPendingReplanCountTest(unittest.TestCase):
    """add_pending stores replan_count under _replan_count (system-controlled)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._original_path = ah.PENDING_APPROVALS_PATH
        ah.PENDING_APPROVALS_PATH = self._root / 'pending.json'

    def tearDown(self):
        ah.PENDING_APPROVALS_PATH = self._original_path
        self._tmp.cleanup()

    def test_replan_count_zero_omits_field(self):
        entry = ah.add_pending(
            _good_payload(task_id='t-fresh'), chat_id=1,
            replan_count=0,
        )
        self.assertNotIn('_replan_count', entry)
        self.assertNotIn('_max_replans', entry)

    def test_replan_count_positive_stores_underscore_fields(self):
        entry = ah.add_pending(
            _good_payload(task_id='t-replan'), chat_id=1,
            replan_count=1, max_replans=2,
        )
        self.assertEqual(entry.get('_replan_count'), 1)
        self.assertEqual(entry.get('_max_replans'), 2)

    def test_replan_count_positive_uses_default_max_when_unset(self):
        entry = ah.add_pending(
            _good_payload(task_id='t-replan-default'), chat_id=1,
            replan_count=1,
        )
        self.assertEqual(entry.get('_replan_count'), 1)
        self.assertEqual(entry.get('_max_replans'), ah.DEFAULT_MAX_REPLANS)


class DispatchApprovedPropagatesReplanCountTest(unittest.TestCase):
    """dispatch_approved writes replan_count/max_replans onto the task envelope
    ONLY when _replan_count > 0 on the entry (system-controlled, not noise)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
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

    def tearDown(self):
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        rv.REPO_ROOT = self._rv_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def test_fresh_entry_omits_replan_count(self):
        entry = {
            'id': 't-fresh',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(task_id='t-fresh'),
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertNotIn('replan_count', data)
        self.assertNotIn('max_replans', data)

    def test_replan_entry_writes_both_fields(self):
        entry = {
            'id': 't-replan',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(task_id='t-replan'),
            '_replan_count': 1,
            '_max_replans': 2,
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['replan_count'], 1)
        self.assertEqual(data['max_replans'], 2)

    def test_replan_entry_uses_default_max_when_omitted(self):
        entry = {
            'id': 't-replan-default',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(task_id='t-replan-default'),
            '_replan_count': 1,
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['replan_count'], 1)
        self.assertEqual(data['max_replans'], ah.DEFAULT_MAX_REPLANS)


# -------------------- render_marker (E1.1) --------------------

class RenderMarkerTest(unittest.TestCase):
    """Render path tests + render <-> parse round-trip (E1.1).

    Beacon has just one marker type today ('approval_request'); the test
    shape stays symmetric with the Forge / Mirror handler tests so the
    drift detector (test_marker_drift.py) can iterate uniformly.
    """

    def _payload(self) -> dict:
        return {
            'task_id': 't-001',
            'summary': 'add a render_marker helper to Forge handler',
            'target_agent': 'forge',
            'prompt': 'GOAL: add render_marker. CONTEXT: see scripts/...',
        }

    def test_render_then_parse_roundtrip(self):
        rendered = ah.render_marker('approval_request', **self._payload())
        parsed, narrative = ah.extract_approval_request(rendered)
        self.assertEqual(parsed, self._payload())
        self.assertEqual(narrative.strip(), '')

    def test_unknown_marker_type_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'unknown marker type'):
            ah.render_marker('bogus', task_id='t-001')

    def test_missing_required_field_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'missing required fields'):
            ah.render_marker(
                'approval_request', task_id='t-001', summary='x', target_agent='forge',
            )  # missing prompt

    def test_extra_fields_preserved_in_payload(self):
        rendered = ah.render_marker(
            'approval_request',
            **self._payload(),
            target_repo='ourliberty-agent-core',
            task_type='feature-development',
            pr_title='feat: render marker',
            phase='preflight',
            reply_chat_id='12345',
        )
        parsed, _ = ah.extract_approval_request(rendered)
        self.assertEqual(parsed.get('target_repo'), 'ourliberty-agent-core')
        self.assertEqual(parsed.get('phase'), 'preflight')
        self.assertEqual(parsed.get('reply_chat_id'), '12345')

    def test_keyword_and_required_fields_cover_every_marker_type(self):
        for mtype in ah.MARKER_TYPES:
            with self.subTest(mtype=mtype):
                self.assertIn(mtype, ah.MARKER_KEYWORDS)
                self.assertIn(mtype, ah.REQUIRED_FIELDS)


if __name__ == '__main__':
    unittest.main()
