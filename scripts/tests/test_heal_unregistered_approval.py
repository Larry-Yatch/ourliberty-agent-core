#!/usr/bin/env python3
"""Tests for heal_unregistered_approval (the direction-ask reconciliation net).

Maps to the brief's acceptance criteria:
  - the exact deploy-notifier alert, with no registered approval, is promoted to
    a target_agent='beacon' approval_request (criterion 1)
  - an ask Beacon already registered via marker is NOT duplicated (criterion 2)
  - a routine (non-decision) escalation is NOT promoted (criterion 3)
  - running twice promotes each alert exactly once (criterion 4)
  - the promoted ask's approve/reject routes Larry's choice back to Beacon —
    asserted against the live dashboard action handler (criterion 5)

Plus: config fallback to built-in defaults, binary-option parsing, needs-triage
fallback, window filtering, self-failure path.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_unregistered_approval
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_unregistered_approval as h  # noqa: E402
import beacon_approval_handler as approval  # noqa: E402


NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


# The real 2026-06-03 deploy-notifier direction-ask, written (wrongly) as a
# pulse/beacon-result larry-alert instead of an APPROVAL_REQUEST marker.
DEPLOY_NOTIFIER_ALERT = {
    'ts': _ts(2),
    'source': 'pulse/beacon-result',
    'severity': 'warning',
    'route': 'escalate',
    'subject': 'deploy-notifier-engine-fix-direction',
    'message': (
        'Beacon is holding the APPROVAL_REQUEST waiting for your direction: '
        'ship the deploy-notifier config-only fix now, or also scope the '
        'engine fix first?'
    ),
    'suggested_action': 'Choose ship-config-only-now or scope-engine-fix-first',
}

# A routine infra escalation — must NOT be promoted.
ROUTINE_ALERT = {
    'ts': _ts(1),
    'source': 'heal-systemd-install-drift',
    'severity': 'warning',
    'route': 'escalate',
    'subject': 'install-drift:ourliberty-heal-foo.timer',
    'message': 'Unit is shipped in the repo but not installed under /etc/systemd/system/.',
    'suggested_action': 'On the droplet (ssh larry@...):\n  sudo cp ...',
}

DEFAULT_HEURISTICS = {
    'scan_window_hours': h.DEFAULT_SCAN_WINDOW_HOURS,
    'suggested_action_prefixes': list(h.DEFAULT_SUGGESTED_ACTION_PREFIXES),
    'decision_phrases': list(h.DEFAULT_DECISION_PHRASES),
}


class IsApprovalClassTest(unittest.TestCase):
    def test_deploy_notifier_alert_is_approval_class(self):
        self.assertTrue(h.is_approval_class(DEPLOY_NOTIFIER_ALERT, DEFAULT_HEURISTICS))

    def test_routine_escalation_not_approval_class(self):
        # criterion 3
        self.assertFalse(h.is_approval_class(ROUTINE_ALERT, DEFAULT_HEURISTICS))

    def test_non_escalate_route_excluded(self):
        rec = dict(DEPLOY_NOTIFIER_ALERT, route='digest')
        self.assertFalse(h.is_approval_class(rec, DEFAULT_HEURISTICS))

    def test_notification_kind_excluded(self):
        rec = dict(DEPLOY_NOTIFIER_ALERT, kind='notification')
        self.assertFalse(h.is_approval_class(rec, DEFAULT_HEURISTICS))

    def test_missing_route_treated_as_escalate(self):
        rec = {k: v for k, v in DEPLOY_NOTIFIER_ALERT.items() if k != 'route'}
        self.assertTrue(h.is_approval_class(rec, DEFAULT_HEURISTICS))

    def test_decision_phrase_in_message_without_prefix(self):
        rec = {
            'route': 'escalate',
            'subject': 'x',
            'message': 'This needs your call before I proceed.',
            'suggested_action': 'See the runbook for details.',
        }
        self.assertTrue(h.is_approval_class(rec, DEFAULT_HEURISTICS))

    def test_prefix_requires_word_boundary(self):
        # "Replying" should NOT match the "Reply" prefix (word-boundary guard).
        rec = {
            'route': 'escalate',
            'subject': 'x',
            'message': 'no decision phrase here',
            'suggested_action': 'Replying to the webhook is automatic.',
        }
        self.assertFalse(h.is_approval_class(rec, DEFAULT_HEURISTICS))


class ParseBinaryOptionsTest(unittest.TestCase):
    def test_choose_or(self):
        self.assertEqual(
            h.parse_binary_options('Choose ship-now or scope-the-fix'),
            ('ship-now', 'scope-the-fix'),
        )

    def test_pick_vs(self):
        self.assertEqual(
            h.parse_binary_options('Pick option-A vs option-B'),
            ('option-A', 'option-B'),
        )

    def test_three_options_not_binary(self):
        self.assertIsNone(h.parse_binary_options('Choose a or b or c'))

    def test_no_split_returns_none(self):
        self.assertIsNone(h.parse_binary_options('Reply with your decision'))

    def test_non_string_returns_none(self):
        self.assertIsNone(h.parse_binary_options(None))


class BuildPayloadTest(unittest.TestCase):
    def test_binary_payload_targets_beacon_and_states_both_options(self):
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        payload = h.build_approval_payload(DEPLOY_NOTIFIER_ALERT, key)
        self.assertEqual(payload['target_agent'], 'beacon')
        self.assertTrue(payload['task_id'].startswith(h.PROMOTED_TASK_PREFIX))
        # both options appear in the summary so the buttons are self-explanatory
        self.assertIn('ship-config-only-now', payload['summary'])
        self.assertIn('scope-engine-fix-first', payload['summary'])
        # required approval_request fields present for the marker/chain helpers
        for field in approval.REQUIRED_FIELDS['approval_request']:
            self.assertIn(field, payload)

    def test_unparseable_falls_back_to_needs_triage(self):
        rec = dict(DEPLOY_NOTIFIER_ALERT,
                   suggested_action='Reply with your decision')
        key = h.alert_dedup_key(rec)
        payload = h.build_approval_payload(rec, key)
        self.assertEqual(payload['target_agent'], 'beacon')
        self.assertIn('needs triage', payload['summary'].lower())
        # carries the original message verbatim for Beacon to formalize
        self.assertIn('holding the APPROVAL_REQUEST', payload['prompt'])


class DedupKeyTest(unittest.TestCase):
    def test_subject_is_the_key(self):
        self.assertEqual(
            h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT),
            'deploy-notifier-engine-fix-direction',
        )

    def test_no_subject_falls_back_to_hash(self):
        rec = {'source': 'x', 'message': 'y'}
        key = h.alert_dedup_key(rec)
        self.assertTrue(key.startswith('nosubject:'))

    def test_task_id_is_deterministic(self):
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        self.assertEqual(h.derive_task_id(key), h.derive_task_id(key))


class AlreadyRegisteredTest(unittest.TestCase):
    def test_beacon_marker_collision_guard(self):
        # criterion 2: a marker Beacon already emitted references the subject.
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        task_id = h.derive_task_id(key)
        state = {
            'pending': [{
                'id': 'deploy-notifier-engine-fix-001',
                'plan_summary': 'Direction: deploy-notifier-engine-fix-direction',
                'dispatch_payload': {'target_agent': 'beacon'},
            }],
            'history': [],
        }
        self.assertTrue(h.is_already_registered(key, task_id, state))

    def test_own_task_id_already_present(self):
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        task_id = h.derive_task_id(key)
        state = {'pending': [{'id': task_id}], 'history': []}
        self.assertTrue(h.is_already_registered(key, task_id, state))

    def test_unrelated_state_not_a_match(self):
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        task_id = h.derive_task_id(key)
        state = {'pending': [{'id': 'something-else', 'plan_summary': 'x'}],
                 'history': []}
        self.assertFalse(h.is_already_registered(key, task_id, state))


class EvaluateTest(unittest.TestCase):
    def _empty_state(self):
        return {'pending': [], 'history': []}

    def test_promotes_deploy_notifier_alert(self):
        # criterion 1
        out = h.evaluate([DEPLOY_NOTIFIER_ALERT], DEFAULT_HEURISTICS,
                         self._empty_state(), {}, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['target_agent'], 'beacon')

    def test_routine_alert_not_promoted(self):
        # criterion 3
        out = h.evaluate([ROUTINE_ALERT], DEFAULT_HEURISTICS,
                         self._empty_state(), {}, now=NOW)
        self.assertEqual(out, [])

    def test_already_promoted_skipped(self):
        # criterion 4 (state-file dedup)
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        out = h.evaluate([DEPLOY_NOTIFIER_ALERT], DEFAULT_HEURISTICS,
                         self._empty_state(), {key: _ts(0)}, now=NOW)
        self.assertEqual(out, [])

    def test_out_of_window_skipped(self):
        old = dict(DEPLOY_NOTIFIER_ALERT, ts=_ts(48))  # > 24h window
        out = h.evaluate([old], DEFAULT_HEURISTICS,
                         self._empty_state(), {}, now=NOW)
        self.assertEqual(out, [])

    def test_duplicate_alerts_same_tick_promoted_once(self):
        out = h.evaluate([DEPLOY_NOTIFIER_ALERT, dict(DEPLOY_NOTIFIER_ALERT)],
                         DEFAULT_HEURISTICS, self._empty_state(), {}, now=NOW)
        self.assertEqual(len(out), 1)

    def test_already_registered_skipped(self):
        # criterion 2 at the evaluate level
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        state = {'pending': [{'id': 'x',
                              'plan_summary': key}], 'history': []}
        out = h.evaluate([DEPLOY_NOTIFIER_ALERT], DEFAULT_HEURISTICS,
                         state, {}, now=NOW)
        self.assertEqual(out, [])


class ConfigFallbackTest(unittest.TestCase):
    def test_missing_file_uses_defaults(self):
        cfg = h.load_heuristics(Path('/nonexistent/heuristics.json'))
        self.assertEqual(cfg['scan_window_hours'], h.DEFAULT_SCAN_WINDOW_HOURS)
        self.assertIn('Choose', cfg['suggested_action_prefixes'])

    def test_shipped_config_is_valid(self):
        cfg = h.load_heuristics()  # the real config/unregistered-approval-heuristics.json
        self.assertGreater(cfg['scan_window_hours'], 0)
        self.assertTrue(cfg['suggested_action_prefixes'])
        self.assertTrue(cfg['decision_phrases'])

    def test_malformed_partial_config_merges_defaults(self):
        with mock.patch.object(Path, 'read_text', return_value='{"scan_window_hours": -5}'):
            cfg = h.load_heuristics(Path('/whatever.json'))
        # invalid window ignored -> default; lists fall back to defaults
        self.assertEqual(cfg['scan_window_hours'], h.DEFAULT_SCAN_WINDOW_HOURS)
        self.assertEqual(cfg['suggested_action_prefixes'],
                         list(h.DEFAULT_SUGGESTED_ACTION_PREFIXES))


class RegisterApprovalTest(unittest.TestCase):
    """criterion 1 (write path) + idempotency of the chain-event emit."""

    def test_register_writes_pending_and_emits_chain_event(self):
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        payload = h.build_approval_payload(DEPLOY_NOTIFIER_ALERT, key)
        payload['_source_ts'] = DEPLOY_NOTIFIER_ALERT['ts']

        captured = {}

        def fake_add_pending(p, chat_id, **kw):
            captured['pending'] = p
            captured['chat_id'] = chat_id
            return {'id': p['task_id']}

        def fake_emit(**kwargs):
            captured['emit'] = kwargs
            return True

        with mock.patch.object(approval, 'add_pending', side_effect=fake_add_pending), \
             mock.patch.object(h.chain_event_emit, 'emit_event', side_effect=fake_emit):
            ok = h.register_approval(dict(payload), chat_id=None)

        self.assertTrue(ok)
        self.assertEqual(captured['pending']['target_agent'], 'beacon')
        self.assertEqual(captured['emit']['event_type'], 'approval_request')
        self.assertEqual(captured['emit']['agent'], 'beacon')
        # ts threaded through from the source alert -> deterministic event_id
        self.assertEqual(captured['emit']['ts'], DEPLOY_NOTIFIER_ALERT['ts'])


class DashboardRoutingTest(unittest.TestCase):
    """criterion 5: the promoted ask, approved/rejected on the dashboard,
    routes Larry's choice back to Beacon. Assert against the live handler."""

    def _chain_event_row(self):
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        payload = h.build_approval_payload(DEPLOY_NOTIFIER_ALERT, key)
        kwargs = approval.build_approval_request_chain_event(
            payload, ts=DEPLOY_NOTIFIER_ALERT['ts'])
        # Shape a chain_events row the dashboard handler would read.
        return {
            'event_type': kwargs['event_type'],
            'task_id': kwargs['task_id'],
            'event_id': f"evt-{kwargs['task_id']}",
            'payload': kwargs['payload'],
        }

    def test_approve_routes_to_beacon(self):
        import dashboard_api
        row = self._chain_event_row()
        target, filename, envelope = dashboard_api._build_envelope_for_action(
            source=row, action='approve', comment=None, actor='larry@example.com')
        self.assertEqual(target, 'beacon')
        self.assertTrue(envelope['task_id'].startswith('larry-approval-'))
        self.assertIn('suggested_envelope_for_approve',
                      row['payload'])

    def test_reject_routes_to_beacon(self):
        import dashboard_api
        row = self._chain_event_row()
        target, filename, envelope = dashboard_api._build_envelope_for_action(
            source=row, action='reject', comment=None, actor='larry@example.com')
        self.assertEqual(target, 'beacon')
        self.assertTrue(envelope['task_id'].startswith('larry-reject-'))

    def test_chain_event_payload_target_is_beacon(self):
        row = self._chain_event_row()
        self.assertEqual(row['payload']['target_agent'], 'beacon')
        self.assertEqual(row['payload']['proposing_agent'], 'beacon')


if __name__ == '__main__':
    unittest.main()
