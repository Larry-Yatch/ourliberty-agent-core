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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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
        for token in ['approve', 'Approve', 'APPROVE', 'go', 'ok', 'okay', 'ship', 'ship it']:
            r = ah.parse_user_reply(token)
            self.assertEqual(r['action'], 'approve', f'token={token!r} should approve')

    def test_ambiguous_text_does_not_approve(self):
        for text in [
            'looks good, approve',           # extra context
            'approve if it is safe',
            'yes please',
            'yes',                           # bare yes now reserved for general use
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

    def test_approve_graduation_parses_template(self):
        """Phase C: the dedicated graduation grammar parses to a template-
        targeted action (never a generic approve)."""
        for text in ['approve graduation restart-daemon',
                     'Approve Graduation restart-daemon',
                     '  approve   graduation   restart-daemon  ']:
            r = ah.parse_user_reply(text)
            self.assertEqual(r['action'], 'approve_graduation', f'text={text!r}')
            self.assertEqual(r['template'], 'restart-daemon')

    def test_bare_approve_is_not_graduation(self):
        self.assertEqual(ah.parse_user_reply('approve')['action'], 'approve')

    def test_approve_graduation_bad_template_bounces(self):
        # missing template, or non-kebab garbage -> not a graduation command
        self.assertEqual(ah.parse_user_reply('approve graduation')['action'],
                         'none')


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

    def test_most_recent_excludes_graduation(self):
        """Phase C: a graduation entry (kind=='graduation') must NEVER be
        resolved by a bare `approve` / most_recent_pending — only by its
        explicit named approval."""
        grad = _good_payload(task_id='graduation-restart-daemon')
        grad['kind'] = 'graduation'
        grad['template'] = 'restart-daemon'
        ah.add_pending(grad, chat_id=1)
        # a graduation alone -> nothing for a bare approve to pick up
        self.assertIsNone(ah.most_recent_pending())
        # a normal entry alongside is still returned; the graduation is skipped
        ah.add_pending(_good_payload(task_id='t-normal'), chat_id=1)
        self.assertEqual(ah.most_recent_pending()['id'], 't-normal')

    def test_most_recent_excludes_healer_promoted(self):
        """Seam audit H2: a healer/dashboard-routed entry (its dispatch_payload
        stamped bare_approvable=False) must NEVER be picked by a bare `approve`
        / most_recent_pending — EVEN when it is the newest pending entry. It is
        resolved by id from the Approvals tab, not by a bare approve Larry
        intended for a plan he was actually DMed about."""
        # genuine operator-DMed entry first, forced OLDER
        ah.add_pending(_good_payload(task_id='t-operator'), chat_id=1)
        s = ah.load_state()
        s['pending'][0]['created_at'] = '2026-05-11T20:00:00Z'
        ah.save_state(s)
        # healer promotes a stranded direction-ask LATER (newer created_at)
        healer = _good_payload(task_id='heal-direction-ask',
                               target_agent='beacon', bare_approvable=False,
                               origin='heal-unregistered-approval')
        ah.add_pending(healer, chat_id=1)
        # a bare approve resolves the operator entry, NOT the newer healer card
        recent = ah.most_recent_pending()
        self.assertEqual(recent['id'], 't-operator')

    def test_most_recent_with_only_healer_promoted_is_none(self):
        """A healer card alone leaves nothing for a bare approve to dispatch —
        so a stray `approve` can't fire a healer-promoted card at the wrong
        target (the H2 dispatch-to-wrong-target bug)."""
        healer = _good_payload(task_id='heal-direction-ask',
                               target_agent='beacon', bare_approvable=False)
        ah.add_pending(healer, chat_id=1)
        self.assertIsNone(ah.most_recent_pending())

    def test_is_operator_dispatchable(self):
        # a plain bot entry (no marker) is bare-approvable
        self.assertTrue(ah._is_operator_dispatchable(
            {'dispatch_payload': _good_payload(task_id='t')}))
        # an explicit bare_approvable=False (healer/dashboard) is not
        self.assertFalse(ah._is_operator_dispatchable(
            {'dispatch_payload': _good_payload(task_id='t',
                                               bare_approvable=False)}))
        # a graduation is not (subsumes the prior graduation exclusion)
        grad = _good_payload(task_id='g')
        grad['kind'] = 'graduation'
        self.assertFalse(ah._is_operator_dispatchable({'dispatch_payload': grad}))
        # backward compat: an entry with no dispatch_payload stays dispatchable
        self.assertTrue(ah._is_operator_dispatchable({'id': 'x'}))

    def test_find_graduation_pending_exact_template(self):
        grad = _good_payload(task_id='graduation-restart-daemon')
        grad['kind'] = 'graduation'
        grad['template'] = 'restart-daemon'
        ah.add_pending(grad, chat_id=1)
        self.assertEqual(
            ah.find_graduation_pending('restart-daemon')['id'],
            'graduation-restart-daemon')
        self.assertIsNone(ah.find_graduation_pending('retry-sync-push'))

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

    def test_resolve_self_committed_clears_dashboard_pending(self):
        """A bare (self-committing) resolve mirrors the resolution to the
        dashboard so the Approvals tab clears immediately instead of waiting on
        the heal_stale_approvals 10-min timer (the stale-'blocking' window)."""
        import chain_event_emit as cee
        cleared = []
        orig = cee.clear_approval_request
        cee.clear_approval_request = lambda tid, **kw: cleared.append(tid)
        try:
            ah.add_pending(_good_payload(task_id='t-clear'), chat_id=1)
            ah.resolve('t-clear', 'approved')
        finally:
            cee.clear_approval_request = orig
        self.assertEqual(cleared, ['t-clear'])

    def test_resolve_with_caller_state_leaves_clear_to_healer(self):
        """When the caller owns the txn (state passed in), resolve does NOT
        mirror — the caller commits later and heal_stale_approvals reconciles.
        Guards the own-is-False gate against a premature dashboard clear."""
        import chain_event_emit as cee
        cleared = []
        orig = cee.clear_approval_request
        cee.clear_approval_request = lambda tid, **kw: cleared.append(tid)
        try:
            state = {'pending': [{'id': 't-ext'}], 'history': []}
            moved = ah.resolve('t-ext', 'approved', state=state)
        finally:
            cee.clear_approval_request = orig
        self.assertIsNotNone(moved)
        self.assertEqual(cleared, [])
        # The caller's state was still mutated (moved to history) as before.
        self.assertEqual(state['pending'], [])
        self.assertEqual(len(state['history']), 1)

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
        self.assertIn('approve / go / ok / ship it', msg)
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
        self.assertIn('approve / go / ok / ship it', msg)


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

    def test_dispatch_carries_origin_task_id_onto_forge_envelope(self):
        # Delegate-tracking Slice 2a: an entry stamped with origin_task_id
        # (Slice 1) threads it onto the dispatched Forge envelope; the marker
        # id (envelope task_id / routing key) is untouched.
        entry = {
            'id': 'fix-thing-001',
            'target_agent': 'forge',
            'origin_task_id': 'delegate-cap-fix-thing-ab12',
            'dispatch_payload': _good_payload(task_id='fix-thing-001'),
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['origin_task_id'], 'delegate-cap-fix-thing-ab12')
        self.assertEqual(data['task_id'], 'fix-thing-001')

    def test_dispatch_omits_origin_task_id_when_absent(self):
        entry = {
            'id': 'plain-001',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(task_id='plain-001'),
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertNotIn('origin_task_id', data)

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

    def test_dispatch_to_beacon_resources_to_larry(self):
        # Self-targeted approval (binary direction-ask APPROVAL_REQUEST with
        # target_agent=beacon). source='beacon' would raise RoutingDenied at
        # the self-dispatch (beacon->beacon) hard-topology check; re-sourcing
        # to 'larry' lands the task in Beacon's inbox without raising.
        entry = {
            'id': 't-self',
            'target_agent': 'beacon',
            'dispatch_payload': _good_payload(
                task_id='t-self', target_agent='beacon'),
        }
        dest = ah.dispatch_approved(entry)
        self.assertTrue(dest.exists())
        self.assertEqual(dest.parent.name, 'beacon')
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'larry')

    def test_dispatch_to_forge_keeps_beacon_source(self):
        # Regression guard: non-self targets are unaffected — source stays
        # 'beacon' for forge (and by symmetry mirror / build_sequence_advancer).
        entry = {
            'id': 't-forge',
            'target_agent': 'forge',
            'dispatch_payload': _good_payload(
                task_id='t-forge', target_agent='forge'),
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'beacon')

    def test_dispatch_to_beacon_preserves_reply_chat_id(self):
        # The source=larry re-source must NOT trip the accepts_from_user
        # soft-reroute (only USER_DM_SOURCES={telegram-webhook} does), so a
        # positive reply_chat_id still lands in Beacon's inbox intact and the
        # closing-DM chain keeps its thread anchor.
        entry = {
            'id': 't-self-chat',
            'target_agent': 'beacon',
            'chat_id': 7998341473,
            'dispatch_payload': _good_payload(
                task_id='t-self-chat', target_agent='beacon'),
        }
        dest = ah.dispatch_approved(entry)
        self.assertEqual(dest.parent.name, 'beacon')
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'larry')
        self.assertEqual(data['reply_chat_id'], 7998341473)

    def test_dispatch_to_beacon_preserves_replan_budget(self):
        # Replan-budget propagation is independent of the re-source: a
        # self-targeted replan approval still carries replan_count/max_replans.
        entry = {
            'id': 't-self-replan',
            'target_agent': 'beacon',
            '_replan_count': 2,
            '_max_replans': 3,
            'dispatch_payload': _good_payload(
                task_id='t-self-replan', target_agent='beacon'),
        }
        dest = ah.dispatch_approved(entry)
        with open(dest) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'larry')
        self.assertEqual(data['replan_count'], 2)
        self.assertEqual(data['max_replans'], 3)


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


# -------------------- chain_event payload builder (E4.4e PR-A) --------------------


class BuildAutonomyDecisionChainEventTest(unittest.TestCase):
    """autonomy_decision: one record powering the Automated Work feed
    (decision=auto_approve) and the needs-Larry view (decision=force_ask)."""

    def _payload(self, **o):
        p = {
            'task_id': 'task-auto-001',
            'summary': 'Reconcile the projects store',
            'target_agent': 'forge',
            'target_repo': 'ourliberty-agent-core',
            'task_type': 'feature-development',
            'pr_title': 'feat(projects): completion-reconcile',
        }
        p.update(o)
        return p

    def test_kwargs_and_known_type(self):
        import chain_event_shipper as ces
        row = ah.build_autonomy_decision_chain_event(
            self._payload(), decision='auto_approve', rule={'action': 'auto_approve'})
        self.assertEqual(row['event_type'], 'autonomy_decision')
        self.assertIn(row['event_type'], ces.KNOWN_EVENT_TYPES)  # emit_event won't drop it
        self.assertEqual(row['agent'], 'beacon')
        self.assertEqual(row['task_id'], 'task-auto-001')
        self.assertEqual(row['id_extra'], 'auto_approve')
        self.assertIn('ts', row)

    def test_auto_approve_is_dispatched(self):
        rule = {'action': 'auto_approve', 'repos': ['ourliberty-agent-core']}
        row = ah.build_autonomy_decision_chain_event(
            self._payload(), decision='auto_approve', rule=rule, source='beacon')
        p = row['payload']
        self.assertEqual(p['decision'], 'auto_approve')
        self.assertTrue(p['dispatched'])  # fired without Larry → Automated Work
        self.assertEqual(p['source'], 'beacon')
        self.assertEqual(p['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(p['matched_rule'], rule)
        self.assertEqual(p['summary'], 'Reconcile the projects store')

    def test_force_ask_and_reject_not_dispatched(self):
        for dec in ('force_ask', 'reject'):
            row = ah.build_autonomy_decision_chain_event(
                self._payload(), decision=dec, rule=None,
                source='pulse-auto-dispatch')
            p = row['payload']
            self.assertEqual(p['decision'], dec)
            self.assertFalse(p['dispatched'], dec)  # waiting on Larry → needs-Larry
            self.assertEqual(p['source'], 'pulse-auto-dispatch')
            self.assertIsNone(p['matched_rule'])

    def test_missing_task_id_defaults_to_unknown(self):
        row = ah.build_autonomy_decision_chain_event(
            {'summary': 'x'}, decision='force_ask')
        self.assertEqual(row['task_id'], 'unknown')


class BuildApprovalRequestChainEventTest(unittest.TestCase):
    """Spec § 4 contract: 6-field payload + dual concrete envelopes."""

    def _payload(self, **overrides):
        p = {
            'task_id': 'task-emit-001',
            'summary': 'Plan summary',
            'target_agent': 'forge',
            'prompt': 'A complete plan prompt for Forge to act on.',
            'target_repo': 'ourliberty-agent-core',
        }
        p.update(overrides)
        return p

    def test_returns_emit_event_kwargs(self):
        row = ah.build_approval_request_chain_event(self._payload())
        # Caller does chain_event_emit.emit_event(**row) — confirm kwargs.
        self.assertEqual(row['event_type'], 'approval_request')
        self.assertEqual(row['agent'], 'beacon')
        self.assertEqual(row['task_id'], 'task-emit-001')
        self.assertIn('ts', row)
        self.assertIn('payload', row)

    def test_payload_has_all_six_spec_fields(self):
        row = ah.build_approval_request_chain_event(self._payload())
        payload = row['payload']
        for field in (
            'proposing_agent', 'target_agent', 'prompt', 'dedup_identity',
            'suggested_envelope_for_approve', 'suggested_envelope_for_reject',
        ):
            self.assertIn(field, payload, f'missing field {field!r}')
        self.assertEqual(payload['proposing_agent'], 'beacon')
        self.assertEqual(payload['target_agent'], 'forge')
        # dedup_identity is the dispatch task_id — NOT a timestamp/UUID
        # (per PR-A dispatch task discipline).
        self.assertEqual(payload['dedup_identity'], 'task-emit-001')

    def test_both_envelopes_present(self):
        row = ah.build_approval_request_chain_event(self._payload())
        approve_env = row['payload']['suggested_envelope_for_approve']
        reject_env = row['payload']['suggested_envelope_for_reject']
        self.assertIsInstance(approve_env, dict)
        self.assertIsInstance(reject_env, dict)
        for env in (approve_env, reject_env):
            for required in ('task_id', 'source', 'dedup_identity',
                              'timeout', 'prompt'):
                self.assertIn(required, env)
            self.assertEqual(env['source'], 'dashboard')
            self.assertEqual(env['timeout'], 600)

    def test_event_id_substituted_into_envelopes(self):
        # Compute expected event_id via the same path the builder uses.
        ts = '2026-05-26T18:00:00+00:00'
        row = ah.build_approval_request_chain_event(self._payload(), ts=ts)
        import chain_event_shipper as ces  # local import; module-level loaded
        expected_event_id = ces.compute_event_id(
            'task-emit-001', 'approval_request', ts,
        )
        approve_env = row['payload']['suggested_envelope_for_approve']
        reject_env = row['payload']['suggested_envelope_for_reject']
        # task_id placeholder
        self.assertEqual(approve_env['task_id'], f'larry-approval-{expected_event_id}')
        self.assertEqual(reject_env['task_id'], f'larry-reject-{expected_event_id}')
        # dedup_identity placeholder
        self.assertEqual(approve_env['dedup_identity'],
                          f'larry-approval:{expected_event_id}')
        self.assertEqual(reject_env['dedup_identity'],
                          f'larry-reject:{expected_event_id}')
        # prompt references the event_id
        self.assertIn(expected_event_id, approve_env['prompt'])
        self.assertIn(expected_event_id, reject_env['prompt'])

    def test_actor_omitted_from_envelopes(self):
        # Per spec § 11 OQ1 resolution: dashboard fills `actor` at write
        # time from the authed user. Builder must NOT stub it.
        row = ah.build_approval_request_chain_event(self._payload())
        approve_env = row['payload']['suggested_envelope_for_approve']
        reject_env = row['payload']['suggested_envelope_for_reject']
        self.assertNotIn('actor', approve_env)
        self.assertNotIn('actor', reject_env)

    def test_reject_prompt_has_no_comment_placeholder(self):
        # Dashboard appends `Optional comment: <text>` at POST-time iff
        # the user supplies one. Builder must NOT carry a literal placeholder.
        row = ah.build_approval_request_chain_event(self._payload())
        reject_prompt = row['payload']['suggested_envelope_for_reject']['prompt']
        self.assertNotIn('<comment', reject_prompt)
        self.assertNotIn('{comment', reject_prompt)
        self.assertNotIn('Optional comment', reject_prompt)

    def test_ts_default_is_iso_utc(self):
        row = ah.build_approval_request_chain_event(self._payload())
        ts = row['ts']
        # Must round-trip through datetime.fromisoformat without error
        # and end up UTC-aware.
        parsed = datetime.fromisoformat(ts.replace('Z', '+00:00')
                                          if ts.endswith('Z') else ts)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_target_agent_defaults_to_forge(self):
        # Some marker payloads may omit target_agent (rare; usually present)
        # — default per existing add_pending behavior.
        p = self._payload()
        p.pop('target_agent')
        row = ah.build_approval_request_chain_event(p)
        self.assertEqual(row['payload']['target_agent'], 'forge')


class IsCompletionClaimTest(unittest.TestCase):
    """Prose guard: gate fait-accompli dispatch claims, not intent/normal talk.

    harden-authoritative-dispatch-confirmation (2026-06-04). The defining
    sample is the 2026-06-03 phantom Beacon DM'd Larry with no marker behind it.
    """

    PHANTOM = "Approved — `deploy-notifier-ready-logonly` dispatches to Forge now"

    def setUp(self):
        # Use the embedded defaults so the test never depends on the repo file
        # state (and never poisons the module cache for other tests).
        self.cfg = ah.load_completion_claim_patterns(force=True)
        ah._completion_claim_patterns = None

    def tearDown(self):
        ah._completion_claim_patterns = None

    # ---- the documented phantom must gate ----

    def test_documented_phantom_gates(self):
        self.assertTrue(ah.is_completion_claim(self.PHANTOM, self.cfg))

    def test_hard_completion_phrases_gate(self):
        for text in [
            'Done — dispatched to Forge.',
            'It dispatches to Forge now.',
            'That goes to Forge now.',
            'Approved and dispatched.',
            'Shipped to Forge — Mirror will review.',
            'Sent to Forge.',
            'The task is now in Forge.',
            "It's in Forge's inbox.",
        ]:
            self.assertTrue(
                ah.is_completion_claim(text, self.cfg),
                f'should gate: {text!r}',
            )

    def test_approval_pair_gates_with_target(self):
        # "approved —" + a target word, no intent marker → fait accompli.
        self.assertTrue(
            ah.is_completion_claim('Approved — sending this to Forge.', self.cfg)
        )

    # ---- intent / normal talk must NOT gate (don't muzzle Beacon) ----

    def test_brief_protected_intent_not_gated(self):
        # The brief explicitly protects present-progressive first-person intent.
        for text in [
            "I'm dispatching deploy-notifier now.",
            "I'm about to dispatch this to Forge.",
            "I'll send this to Forge once you approve.",
            "Ready to dispatch — want me to send it to Forge?",
            "If you approve, this dispatches to Forge.",
        ]:
            self.assertFalse(
                ah.is_completion_claim(text, self.cfg),
                f'should NOT gate (intent): {text!r}',
            )

    def test_approval_pair_with_intent_marker_not_gated(self):
        # "approved —" present but framed as future/conditional intent.
        self.assertFalse(
            ah.is_completion_claim(
                'Approved — I will dispatch this to Forge once tests pass.',
                self.cfg,
            )
        )

    def test_normal_talk_not_gated(self):
        for text in [
            'Forge handles deploy tasks; Mirror reviews the PR.',
            'I considered dispatching this but it needs more scope first.',
            "Here's the plan. Approve and it'll go out.",
            'The PR is open and Mirror is reviewing.',
            'What would you like me to focus on?',
        ]:
            self.assertFalse(
                ah.is_completion_claim(text, self.cfg),
                f'should NOT gate (normal): {text!r}',
            )

    def test_non_string_and_empty(self):
        self.assertFalse(ah.is_completion_claim('', self.cfg))
        self.assertFalse(ah.is_completion_claim(None, self.cfg))  # type: ignore[arg-type]

    def test_case_insensitive(self):
        self.assertTrue(
            ah.is_completion_claim('DISPATCHED TO FORGE.', self.cfg)
        )


class LoadCompletionClaimPatternsTest(unittest.TestCase):
    """Config loader caches, accepts overrides, and fails safe to defaults."""

    def tearDown(self):
        ah._completion_claim_patterns = None

    def test_missing_file_falls_back_to_defaults(self):
        missing = Path(tempfile.gettempdir()) / 'does-not-exist-completion.json'
        cfg = ah.load_completion_claim_patterns(missing)
        self.assertEqual(
            sorted(cfg['completion_claim_phrases']),
            sorted(ah._DEFAULT_COMPLETION_CLAIM_PATTERNS['completion_claim_phrases']),
        )

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False
        ) as fh:
            fh.write('{ not json')
            bad = Path(fh.name)
        try:
            cfg = ah.load_completion_claim_patterns(bad)
            self.assertIn('dispatched to forge', cfg['completion_claim_phrases'])
        finally:
            bad.unlink()

    def test_custom_path_not_cached(self):
        with tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False
        ) as fh:
            json.dump({'completion_claim_phrases': ['zzz only']}, fh)
            custom = Path(fh.name)
        try:
            ah._completion_claim_patterns = None
            cfg = ah.load_completion_claim_patterns(custom)
            self.assertEqual(cfg['completion_claim_phrases'], ['zzz only'])
            # A custom path must not poison the module cache.
            self.assertIsNone(ah._completion_claim_patterns)
        finally:
            custom.unlink()

    def test_repo_config_loads_and_gates_phantom(self):
        # The shipped config file must itself gate the documented phantom.
        cfg = ah.load_completion_claim_patterns(
            ah.COMPLETION_CLAIM_PATTERNS_PATH, force=True
        )
        self.assertTrue(
            ah.is_completion_claim(IsCompletionClaimTest.PHANTOM, cfg)
        )


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
