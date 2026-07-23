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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Import-time sandbox (canonical Gap-A shape, see scripts/tests/conftest.py):
# the healer's transitive imports freeze AGENTS_ROOT-derived paths at import,
# so the env must be set BEFORE the import below. Guarded so the #436 gate
# env / tests/__init__.py (and any outer harness) win. Interim until the
# per-module bootstrap (docs/test-jail-spec.md Layer A) lands.
if not os.environ.get('OURLIBERTY_AGENTS_ROOT'):
    _SANDBOX_ROOT = tempfile.mkdtemp(prefix='ol-test-agents-root-')
    os.makedirs(os.path.join(_SANDBOX_ROOT, 'logs'), exist_ok=True)
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _SANDBOX_ROOT
    os.environ.setdefault(
        'OURLIBERTY_WORKTREES_ROOT', os.path.join(_SANDBOX_ROOT, 'worktrees'))
    os.environ.setdefault(
        'OURLIBERTY_LOG_DIR', os.path.join(_SANDBOX_ROOT, 'logs'))

import heal_unregistered_approval as h  # noqa: E402
import beacon_approval_handler as approval  # noqa: E402


# Runtime backstop: h resolves agents_root() at CALL time, and an
# alphabetically-earlier suite's tearDown can pop OURLIBERTY_AGENTS_ROOT
# before this module's tests RUN (discover imports everything first, then
# runs) — so the env pin above cannot be the only barrier. Patch the two
# write funnels for the whole module; tests that assert on them (e.g.
# PromoteRaceTest) layer their own patches on top.
_MODULE_WRITE_PATCHES = [
    mock.patch.object(h, 'log'),
    mock.patch.object(h, 'heartbeat'),
]


def setUpModule():
    for _p in _MODULE_WRITE_PATCHES:
        _p.start()


def tearDownModule():
    for _p in _MODULE_WRITE_PATCHES:
        _p.stop()


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

    def test_needs_larry_signal_promotes_without_decision_phrasing(self):
        # The unrouted-PR nudge has no decision verb/phrase — it qualifies
        # SOLELY on the explicit needs_larry signal (the non-whack-a-mole path).
        self.assertTrue(h.is_approval_class(UNROUTED_PR_ALERT, DEFAULT_HEURISTICS))

    def test_needs_larry_still_excluded_when_not_escalate(self):
        # The signal never overrides the route gate.
        rec = dict(UNROUTED_PR_ALERT, route='digest')
        self.assertFalse(h.is_approval_class(rec, DEFAULT_HEURISTICS))

    def test_needs_larry_notification_kind_still_excluded(self):
        # A notification carrying needs_larry is still a follow-up, not a card.
        self.assertFalse(
            h.is_approval_class(MEDIC_NOTIFICATION_PR28, DEFAULT_HEURISTICS))

    def test_escalate_without_signal_or_phrase_not_promoted(self):
        # No needs_larry, no decision verb/phrase -> stays off the tab (no flood).
        self.assertFalse(
            h.is_approval_class(ESCALATE_NO_SIGNAL_ALERT, DEFAULT_HEURISTICS))

    def test_dispatch_prefix_promotes_without_needs_larry(self):
        # Defense-in-depth: even if an actionable alert forgot needs_larry, the
        # 'Dispatch' suggested_action prefix catches it (shipped config).
        rec = {k: v for k, v in UNROUTED_PR_ALERT.items() if k != 'needs_larry'}
        cfg = h.load_heuristics()  # real config/unregistered-approval-heuristics.json
        self.assertTrue(h.is_approval_class(rec, cfg))


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

    def test_payload_is_not_bare_approvable(self):
        """Seam audit H2: a promoted card is healer/dashboard-routed, so its
        payload must carry bare_approvable=False (and origin) — that is the
        stamp beacon_approval_handler.most_recent_pending filters on so a bare
        `approve` never dispatches this card to the wrong target."""
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        payload = h.build_approval_payload(DEPLOY_NOTIFIER_ALERT, key)
        self.assertIs(payload['bare_approvable'], False)
        self.assertEqual(payload['origin'], h.HEALER_SOURCE)
        # and the queue owner agrees this entry is not operator-dispatchable
        self.assertFalse(
            approval._is_operator_dispatchable({'dispatch_payload': payload}))

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


# -------------------- reconciler fixtures (skip / dedup / retire) -----------

# An ask whose referenced PR is the resolution anchor. PR #294 was MERGED.
PR294_ALERT = {
    'ts': _ts(3),
    'source': 'pulse/beacon-result',
    'severity': 'warning',
    'route': 'escalate',
    'subject': 'PR #294 Mirror review gap — source=larry routing miss',
    'message': 'PR #294 Mirror review gap needs your call before I proceed.',
    'suggested_action': 'Reply with how to close the mirror review gap',
}

# Two phrasings of ONE decision (the real 2026-06-04 deploy-notifier pair).
DEPLOY_NOTIFIER_A = {
    'ts': _ts(2),
    'source': 'pulse/beacon-result',
    'route': 'escalate',
    'subject': 'Beacon needs your call: deploy-notifier alert-translations',
    'message': 'Beacon needs your call on the deploy-notifier alert-translations.',
    'suggested_action': 'Choose ship-now or hold',
}
DEPLOY_NOTIFIER_B = {
    'ts': _ts(1),
    'source': 'pulse/beacon-result',
    'route': 'escalate',
    'subject': 'deploy-notifier:READY alert-translations — Beacon needs your call',
    'message': 'deploy-notifier READY; Beacon needs your call on alert-translations.',
    'suggested_action': 'Choose ship-now or hold',
}

# A live, unresolved direction-ask with no machine resolution signal.
CYCLE_TIMER_ALERT = {
    'ts': _ts(2),
    'source': 'pulse/beacon-result',
    'route': 'escalate',
    'subject': 'cycle-timer-daemon-reload-checkpoint',
    'message': 'Beacon needs your call: reload the cycle-timer daemon now, or wait?',
    'suggested_action': 'Choose reload-now or wait-for-window',
}

# Audit §4 regression: a LIVE direction-ask that merely MENTIONS an unrelated
# PR in a parenthetical 're:' aside. The incidental '#5' must NOT become the
# decision's resolution anchor — otherwise merging unrelated PR #5 would
# skip-before-promote or auto-retire this still-open ask off the Approvals tab.
INCIDENTAL_REF_ALERT = {
    'ts': _ts(2),
    'source': 'pulse/beacon-result',
    'route': 'escalate',
    'subject': 'Need direction: pick option A vs B (re: design #5)',
    'message': 'Beacon needs your call: pick option A vs B (re: design #5).',
    'suggested_action': 'Choose option-A or option-B',
}

# The real heal-pipeline-stall unrouted-PR nudge: an ACTIONABLE alert (only Larry
# can route the review) whose suggested_action carries no decision verb/phrase, so
# it promotes ONLY via the explicit needs_larry signal. Its subject anchors PR #28
# so the card auto-retires when the PR routes/merges.
UNROUTED_PR_ALERT = {
    'ts': _ts(1),
    'source': 'heal-pipeline-stall',
    'severity': 'warning',
    'route': 'escalate',
    'needs_larry': True,
    'subject': 'pipeline-stall:unrouted-pr:PR#28',
    'message': (
        'PR #28 (ourliberty-agent-core) on branch `foo` opened 40 min ago has NO '
        'review-request dispatch logged. Mirror will not review until Larry '
        'manually routes it.'
    ),
    'suggested_action': (
        'Dispatch a Mirror review via Beacon chat: '
        '`dispatch mirror review pr=https://github.com/x/y/pull/28`.'
    ),
}

# A route=escalate alert with NO needs_larry and no decision verb/phrase — an
# informational-critical escalate that must stay OFF the Approvals tab (no flood).
ESCALATE_NO_SIGNAL_ALERT = {
    'ts': _ts(1),
    'source': 'heal-pipeline-stall',
    'severity': 'warning',
    'route': 'escalate',
    'subject': 'pipeline-stall:cycle-timer-lag',
    'message': 'The cycle timer is running 12 min behind schedule; still firing.',
    'suggested_action': 'On the droplet: check `systemctl status ourliberty-cycle.timer`.',
}

# A medic-diagnosis follow-up about the SAME PR #28, written as a notification —
# excluded at the front door so it can never mint a second card.
MEDIC_NOTIFICATION_PR28 = {
    'ts': _ts(1),
    'source': 'medic-diagnosis',
    'kind': 'notification',
    'route': 'escalate',
    'needs_larry': True,
    'subject': 'medic-diagnosis:PR#28-unrouted-followup',
    'message': 'Diagnosed PR #28 as unrouted; see the unrouted-PR nudge.',
    'suggested_action': 'Dispatch a Mirror review for PR #28.',
}


def _gh_merged(numbers):
    """Fake gh probe: True for the given resolved PR/issue numbers, else None
    (undetermined — the conservative default)."""
    wanted = set(numbers)
    return lambda n: True if n in wanted else None


class DecisionIdentityTest(unittest.TestCase):
    def test_two_phrasings_share_identity(self):
        self.assertEqual(
            h.decision_identity(DEPLOY_NOTIFIER_A),
            h.decision_identity(DEPLOY_NOTIFIER_B),
        )

    def test_referenced_pr_anchors_identity(self):
        self.assertEqual(h.decision_identity(PR294_ALERT), 'ref:294')

    def test_distinct_decisions_dont_collapse(self):
        self.assertNotEqual(
            h.decision_identity(CYCLE_TIMER_ALERT),
            h.decision_identity(DEPLOY_NOTIFIER_A),
        )

    def test_no_subject_is_deterministic_hash(self):
        ident = h.decision_identity({'source': 'x', 'message': 'y'})
        self.assertTrue(ident.startswith('nosubject:'))


class ResolutionSignalTest(unittest.TestCase):
    def _empty(self):
        return {'pending': [], 'history': []}

    def test_merged_pr_is_a_signal(self):
        reason = h.resolution_signal(
            PR294_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=_gh_merged([294]))
        self.assertIsNotNone(reason)
        self.assertIn('294', reason)

    def test_undetermined_gh_is_no_signal(self):
        # Conservative: gh can't confirm -> promote/keep, never skip/retire.
        reason = h.resolution_signal(
            PR294_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=lambda n: None)
        self.assertIsNone(reason)

    def test_open_pr_is_no_signal(self):
        reason = h.resolution_signal(
            PR294_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=lambda n: False)
        self.assertIsNone(reason)

    def test_resolved_beacon_history_is_a_signal(self):
        state = {'pending': [], 'history': [{
            'id': 'beacon-deploy-notifier-001',
            'status': 'approved',
            'plan_summary': 'Shipped the deploy-notifier alert-translations fix.',
            'dispatch_payload': {},
        }]}
        reason = h.resolution_signal(
            DEPLOY_NOTIFIER_A, state, [], DEFAULT_HEURISTICS,
            gh_probe=lambda n: None)
        self.assertIsNotNone(reason)
        self.assertIn('history', reason)

    def test_pending_beacon_entry_is_not_a_signal(self):
        # A still-pending (unresolved) beacon entry must NOT count as resolved.
        state = {'pending': [{
            'id': 'beacon-deploy-notifier-001',
            'status': 'pending',
            'plan_summary': 'deploy-notifier alert-translations',
        }], 'history': []}
        reason = h.resolution_signal(
            DEPLOY_NOTIFIER_A, state, [], DEFAULT_HEURISTICS,
            gh_probe=lambda n: None)
        self.assertIsNone(reason)

    def test_later_resolution_alert_is_a_signal(self):
        resolved = {
            'ts': _ts(0),
            'route': 'digest',
            'subject': 'deploy-notifier:READY alert-translations — Beacon needs your call',
            'message': 'deploy-notifier alert-translations shipped; resolved.',
        }
        reason = h.resolution_signal(
            DEPLOY_NOTIFIER_A, self._empty(), [resolved], DEFAULT_HEURISTICS,
            after_ts=DEPLOY_NOTIFIER_A['ts'], gh_probe=lambda n: None)
        self.assertIsNotNone(reason)

    def test_live_ask_has_no_signal(self):
        reason = h.resolution_signal(
            CYCLE_TIMER_ALERT, self._empty(), [CYCLE_TIMER_ALERT],
            DEFAULT_HEURISTICS, gh_probe=lambda n: None)
        self.assertIsNone(reason)


class SkipBeforePromoteTest(unittest.TestCase):
    def _empty(self):
        return {'pending': [], 'history': []}

    def _check(self, state, alerts, gh_probe):
        return lambda rec: h.resolution_signal(
            rec, state, alerts, DEFAULT_HEURISTICS,
            after_ts=rec.get('ts'), gh_probe=gh_probe)

    def test_merged_pr_ask_not_promoted(self):
        # ACCEPTANCE: an ask whose referenced PR is merged -> not promoted.
        state = self._empty()
        check = self._check(state, [PR294_ALERT], _gh_merged([294]))
        out = h.evaluate([PR294_ALERT], DEFAULT_HEURISTICS, state, {},
                         now=NOW, resolution_check=check)
        self.assertEqual(out, [])

    def test_undetermined_pr_ask_still_promoted(self):
        # Conservative branch: gh can't confirm -> still surfaced.
        state = self._empty()
        check = self._check(state, [PR294_ALERT], lambda n: None)
        out = h.evaluate([PR294_ALERT], DEFAULT_HEURISTICS, state, {},
                         now=NOW, resolution_check=check)
        self.assertEqual(len(out), 1)

    def test_two_phrasings_promote_one_card(self):
        # ACCEPTANCE: two phrasings of one decision -> exactly one card.
        out = h.evaluate([DEPLOY_NOTIFIER_A, DEPLOY_NOTIFIER_B],
                         DEFAULT_HEURISTICS, self._empty(), {}, now=NOW)
        self.assertEqual(len(out), 1)

    def test_live_ask_promoted_once(self):
        state = self._empty()
        check = self._check(state, [CYCLE_TIMER_ALERT], lambda n: None)
        out = h.evaluate([CYCLE_TIMER_ALERT], DEFAULT_HEURISTICS, state, {},
                         now=NOW, resolution_check=check)
        self.assertEqual(len(out), 1)


class ReconcileRetireTest(unittest.TestCase):
    def test_merged_pr_card_is_retired(self):
        # ACCEPTANCE: previously promoted ask whose PR is merged -> retired.
        subject = PR294_ALERT['subject']
        task_id = h.derive_task_id(subject)  # legacy ledger key == subject
        state = {'pending': [{
            'id': task_id, 'status': 'pending',
            'plan_summary': 'promoted #294 card',
        }], 'history': []}
        promoted = {subject: _ts(1)}  # legacy {key: ts} shape
        retired, remaining = h.reconcile_retire(
            promoted, state, [], DEFAULT_HEURISTICS, now=NOW,
            gh_probe=_gh_merged([294]))
        self.assertEqual([t for t, _ in retired], [task_id])
        self.assertEqual(remaining, {})
        # dropped from pending, moved to history as auto-retired ('expired')
        self.assertEqual(state['pending'], [])
        self.assertTrue(any(
            e['id'] == task_id and e['status'] == 'expired'
            for e in state['history']))

    def test_live_ask_card_not_retired(self):
        # ACCEPTANCE (regression): a live unresolved ask is NOT retired.
        subject = CYCLE_TIMER_ALERT['subject']
        task_id = h.derive_task_id(subject)
        state = {'pending': [{
            'id': task_id, 'status': 'pending', 'plan_summary': 'live ask',
        }], 'history': []}
        promoted = {subject: _ts(1)}
        retired, remaining = h.reconcile_retire(
            promoted, state, [CYCLE_TIMER_ALERT], DEFAULT_HEURISTICS,
            now=NOW, gh_probe=lambda n: None)
        self.assertEqual(retired, [])
        self.assertEqual(remaining, promoted)
        self.assertEqual(len(state['pending']), 1)  # still live

    def test_retire_is_idempotent(self):
        # Second pass over an already-retired ledger is a no-op (no thrash).
        subject = PR294_ALERT['subject']
        task_id = h.derive_task_id(subject)
        state = {'pending': [{'id': task_id, 'status': 'pending'}], 'history': []}
        promoted = {subject: _ts(1)}
        _, remaining = h.reconcile_retire(
            promoted, state, [], DEFAULT_HEURISTICS, now=NOW,
            gh_probe=_gh_merged([294]))
        retired2, remaining2 = h.reconcile_retire(
            remaining, state, [], DEFAULT_HEURISTICS, now=NOW,
            gh_probe=_gh_merged([294]))
        self.assertEqual(retired2, [])
        self.assertEqual(remaining2, {})

    def test_rich_ledger_entry_retired(self):
        # The new ledger value shape ({task_id, subject, promoted_at}) retires too.
        subject = PR294_ALERT['subject']
        identity = h.decision_identity(PR294_ALERT)  # 'ref:294'
        task_id = h.derive_task_id(identity)
        state = {'pending': [{'id': task_id, 'status': 'pending'}], 'history': []}
        promoted = {identity: {
            'task_id': task_id, 'subject': subject, 'promoted_at': _ts(1)}}
        retired, remaining = h.reconcile_retire(
            promoted, state, [], DEFAULT_HEURISTICS, now=NOW,
            gh_probe=_gh_merged([294]))
        self.assertEqual([t for t, _ in retired], [task_id])
        self.assertEqual(remaining, {})


class UnroutedPrPromotionTest(unittest.TestCase):
    """The needs_larry signal path end-to-end: an actionable alert with no
    decision phrasing becomes exactly ONE Approvals-tab card that dedups on
    re-fire and auto-retires when its PR resolves — reusing the SAME dedup/retire
    machinery as the phrasing-based decision-asks."""

    def _empty(self):
        return {'pending': [], 'history': []}

    def test_unrouted_pr_promoted_to_one_card(self):
        out = h.evaluate([UNROUTED_PR_ALERT], DEFAULT_HEURISTICS,
                         self._empty(), {}, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['target_agent'], 'beacon')

    def test_refired_alert_not_duplicated(self):
        # A second identical fire for PR #28 in the same tick -> one card.
        out = h.evaluate([UNROUTED_PR_ALERT, dict(UNROUTED_PR_ALERT)],
                         DEFAULT_HEURISTICS, self._empty(), {}, now=NOW)
        self.assertEqual(len(out), 1)

    def test_already_registered_alert_not_duplicated(self):
        # An already-pending card for PR #28 -> the re-fire is skipped.
        key = h.alert_dedup_key(UNROUTED_PR_ALERT)
        state = {'pending': [{'id': 'x', 'plan_summary': key}], 'history': []}
        out = h.evaluate([UNROUTED_PR_ALERT], DEFAULT_HEURISTICS, state, {},
                         now=NOW)
        self.assertEqual(out, [])

    def test_medic_notification_not_a_second_card(self):
        # The medic follow-up about the same PR is a notification -> never a card.
        out = h.evaluate([MEDIC_NOTIFICATION_PR28], DEFAULT_HEURISTICS,
                         self._empty(), {}, now=NOW)
        self.assertEqual(out, [])

    def test_escalate_without_needs_larry_not_promoted(self):
        # An informational-critical escalate stays off the tab (no flood).
        out = h.evaluate([ESCALATE_NO_SIGNAL_ALERT], DEFAULT_HEURISTICS,
                         self._empty(), {}, now=NOW)
        self.assertEqual(out, [])

    def test_resolved_pr_card_is_retired(self):
        # When PR #28 routes/merges, the promoted card auto-retires — same
        # gh_ref_resolved-driven retire path as the decision-ask cards.
        identity = h.decision_identity(UNROUTED_PR_ALERT)
        self.assertEqual(identity, 'ref:28')
        task_id = h.derive_task_id(identity)
        state = {'pending': [{'id': task_id, 'status': 'pending'}], 'history': []}
        promoted = {identity: {
            'task_id': task_id, 'subject': UNROUTED_PR_ALERT['subject'],
            'promoted_at': _ts(1)}}
        retired, remaining = h.reconcile_retire(
            promoted, state, [], DEFAULT_HEURISTICS, now=NOW,
            gh_probe=_gh_merged([28]))
        self.assertEqual([t for t, _ in retired], [task_id])
        self.assertEqual(remaining, {})
        self.assertEqual(state['pending'], [])


class IncidentalRefAnchorTest(unittest.TestCase):
    """Audit §4: a '#<n>' only anchors resolution when it is GENUINELY
    referential (subject-leading or after a PR/issue/closing keyword). An
    incidental / parenthetical mention must never resolve a live decision."""

    def _empty(self):
        return {'pending': [], 'history': []}

    def _check(self, state, alerts, gh_probe):
        return lambda rec: h.resolution_signal(
            rec, state, alerts, DEFAULT_HEURISTICS,
            after_ts=rec.get('ts'), gh_probe=gh_probe)

    # ---- parse_ref_numbers: only referential positions count ----
    def test_parenthetical_re_ref_is_ignored(self):
        self.assertEqual(
            h.parse_ref_numbers('Need direction: pick option A vs B (re: design #5)'),
            [])

    def test_buried_midsubject_ref_is_ignored(self):
        # A bare number floating mid-subject is not an anchor.
        self.assertEqual(h.parse_ref_numbers('deploy notifier #5 needs a call'), [])

    def test_post_parenthetical_ref_is_not_promoted_to_leading(self):
        # Blanking a parenthetical aside must NOT turn a trailing/buried '#n'
        # into a subject-leading match (the leading check runs on the raw text).
        self.assertEqual(h.parse_ref_numbers('(re: design) #5'), [])
        self.assertEqual(h.parse_ref_numbers('(foo) #5'), [])

    def test_pr_keyword_ref_is_parsed(self):
        self.assertEqual(
            h.parse_ref_numbers('PR #294 Mirror review gap'), [294])

    def test_closing_keyword_ref_is_parsed(self):
        self.assertEqual(h.parse_ref_numbers('closes #5 — ship or hold?'), [5])
        self.assertEqual(h.parse_ref_numbers('Direction on issue #42'), [42])

    def test_subject_leading_ref_is_parsed(self):
        self.assertEqual(h.parse_ref_numbers('#294: rebase or close?'), [294])

    # ---- decision identity is not hijacked by the incidental ref ----
    def test_incidental_ref_does_not_anchor_identity(self):
        ident = h.decision_identity(INCIDENTAL_REF_ALERT)
        self.assertNotEqual(ident, 'ref:5')
        self.assertFalse(ident.startswith('ref:'))

    # ---- skip-before-promote: live ask survives an unrelated merged PR ----
    def test_incidental_ref_ask_still_promoted_when_unrelated_pr_merged(self):
        state = self._empty()
        check = self._check(state, [INCIDENTAL_REF_ALERT], _gh_merged([5]))
        out = h.evaluate([INCIDENTAL_REF_ALERT], DEFAULT_HEURISTICS, state, {},
                         now=NOW, resolution_check=check)
        self.assertEqual(len(out), 1)  # NOT skipped-before-promote

    def test_incidental_ref_is_not_a_resolution_signal(self):
        reason = h.resolution_signal(
            INCIDENTAL_REF_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=_gh_merged([5]))
        self.assertIsNone(reason)

    # ---- retire: live ask is NOT auto-retired when unrelated PR #5 merges ----
    def test_incidental_ref_card_not_retired_when_unrelated_pr_merged(self):
        subject = INCIDENTAL_REF_ALERT['subject']
        identity = h.decision_identity(INCIDENTAL_REF_ALERT)
        task_id = h.derive_task_id(identity)
        state = {'pending': [{
            'id': task_id, 'status': 'pending', 'plan_summary': 'live ask',
        }], 'history': []}
        promoted = {identity: {
            'task_id': task_id, 'subject': subject, 'promoted_at': _ts(1)}}
        retired, remaining = h.reconcile_retire(
            promoted, state, [], DEFAULT_HEURISTICS, now=NOW,
            gh_probe=_gh_merged([5]))
        self.assertEqual(retired, [])           # not auto-retired
        self.assertEqual(remaining, promoted)   # left in the ledger
        self.assertEqual(len(state['pending']), 1)  # still on the tab

    # ---- the genuine 'about PR #<n>' case still resolves ----
    def test_genuine_pr_ask_skipped_before_promote_when_merged(self):
        state = self._empty()
        check = self._check(state, [PR294_ALERT], _gh_merged([294]))
        out = h.evaluate([PR294_ALERT], DEFAULT_HEURISTICS, state, {},
                         now=NOW, resolution_check=check)
        self.assertEqual(out, [])  # genuine merged PR -> skipped

    def test_genuine_pr_card_retired_when_merged(self):
        subject = PR294_ALERT['subject']
        identity = h.decision_identity(PR294_ALERT)  # 'ref:294'
        task_id = h.derive_task_id(identity)
        state = {'pending': [{'id': task_id, 'status': 'pending'}], 'history': []}
        promoted = {identity: {
            'task_id': task_id, 'subject': subject, 'promoted_at': _ts(1)}}
        retired, remaining = h.reconcile_retire(
            promoted, state, [], DEFAULT_HEURISTICS, now=NOW,
            gh_probe=_gh_merged([294]))
        self.assertEqual([t for t, _ in retired], [task_id])  # genuine -> retired
        self.assertEqual(remaining, {})


class EvaluateIdempotencyTest(unittest.TestCase):
    def test_promoted_ledger_blocks_second_promotion(self):
        # ACCEPTANCE: run twice -> no duplicate cards.
        empty = {'pending': [], 'history': []}
        out1 = h.evaluate([CYCLE_TIMER_ALERT], DEFAULT_HEURISTICS, empty, {},
                          now=NOW, resolution_check=lambda r: None)
        self.assertEqual(len(out1), 1)
        identity = h.decision_identity(CYCLE_TIMER_ALERT)
        ledger = {identity: {
            'task_id': out1[0]['task_id'],
            'subject': CYCLE_TIMER_ALERT['subject'],
            'promoted_at': _ts(0),
        }}
        out2 = h.evaluate([CYCLE_TIMER_ALERT], DEFAULT_HEURISTICS, empty, ledger,
                          now=NOW, resolution_check=lambda r: None)
        self.assertEqual(out2, [])

    def test_legacy_subject_ledger_still_dedups(self):
        empty = {'pending': [], 'history': []}
        legacy = {CYCLE_TIMER_ALERT['subject']: _ts(0)}  # old {subject: ts}
        out = h.evaluate([CYCLE_TIMER_ALERT], DEFAULT_HEURISTICS, empty, legacy,
                         now=NOW, resolution_check=lambda r: None)
        self.assertEqual(out, [])


class ClearResolvedByTaskIdTest(unittest.TestCase):
    """The read_at clear path the retire pass reuses (no raw Supabase write)."""

    class _FakeTable:
        def __init__(self, store):
            self.store = store
            self._op = None
            self._payload = None
            self._ids = None

        def update(self, payload):
            self._op, self._payload = 'update', payload
            return self

        def in_(self, _col, ids):
            self._ids = ids
            return self

        def execute(self):
            for r in self.store['rows']:
                if r['event_id'] in (self._ids or []):
                    r['read_at'] = self._payload['read_at']
            return self

    class _FakeClient:
        def __init__(self, rows):
            self.store = {'rows': rows}

        def table(self, _name):
            return ClearResolvedByTaskIdTest._FakeTable(self.store)

    def test_clears_only_matching_task_ids(self):
        import heal_stale_approvals as stale
        rows = [
            {'event_id': 'e1', 'event_type': 'approval_request',
             'task_id': 'unreg-approval-keep', 'ts': _ts(1), 'read_at': None},
            {'event_id': 'e2', 'event_type': 'approval_request',
             'task_id': 'unreg-approval-retire', 'ts': _ts(1), 'read_at': None},
        ]
        client = self._FakeClient(rows)
        with mock.patch.object(stale, 'fetch_pending', return_value=list(rows)), \
             mock.patch.object(stale, '_backup', return_value=Path('/tmp/x.json')):
            cleared = stale.clear_resolved_by_task_id(
                client, ['unreg-approval-retire'], now=NOW)
        self.assertEqual(cleared, 1)
        self.assertIsNone(rows[0]['read_at'])           # kept
        self.assertEqual(rows[1]['read_at'], NOW.isoformat())  # cleared

    def test_no_task_ids_is_noop(self):
        import heal_stale_approvals as stale
        self.assertEqual(stale.clear_resolved_by_task_id(None, [], now=NOW), 0)


class PromoteRaceTest(unittest.TestCase):
    """Seam audit L2: the promote pass re-checks is_already_registered against a
    FRESH load_state() under the shared lock before appending. A real
    APPROVAL_REQUEST Beacon registers between the tick's lock-free snapshot and
    the append must NOT be duplicated into a second card."""

    def _drive_main(self, snapshot_state, fresh_state):
        """Run h.main() with the slow/external collaborators stubbed, driving
        the promote loop with one to-promote payload and a controlled
        snapshot→fresh load_state() sequence. Returns the patched add_pending
        mock so the caller can assert whether the append happened."""
        import contextlib
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        payload = h.build_approval_payload(DEPLOY_NOTIFIER_ALERT, key)
        payload['_source_ts'] = DEPLOY_NOTIFIER_ALERT['ts']
        add_pending = mock.MagicMock(return_value={'id': payload['task_id']})
        with mock.patch.object(h, 'kill_switch',
                               return_value=Path('/nonexistent/kill-switch')), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'log'), \
             mock.patch.object(h, 'load_heuristics',
                               return_value=DEFAULT_HEURISTICS), \
             mock.patch.object(h, 'read_alerts',
                               return_value=[DEPLOY_NOTIFIER_ALERT]), \
             mock.patch.object(h, 'load_promoted', return_value={}), \
             mock.patch.object(h, 'save_promoted'), \
             mock.patch.object(h, 'reconcile_retire', return_value=([], {})), \
             mock.patch.object(h, 'evaluate', return_value=[payload]), \
             mock.patch.object(h, '_chat_id', return_value=4242), \
             mock.patch.object(h, 'doorbell_counts', return_value=(0, 0)), \
             mock.patch.object(h, 'emit_approval_event', return_value=True), \
             mock.patch.object(approval, 'state_lock',
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(approval, 'save_state'), \
             mock.patch.object(approval, 'add_pending', new=add_pending), \
             mock.patch.object(approval, 'load_state',
                               side_effect=[snapshot_state, fresh_state]):
            rc = h.main()
        self.assertEqual(rc, 0)
        return add_pending

    def test_concurrent_registration_skips_duplicate_append(self):
        key = h.alert_dedup_key(DEPLOY_NOTIFIER_ALERT)
        # snapshot (top of tick): empty -> evaluate decides "promote it".
        snapshot = {'pending': [], 'history': []}
        # fresh (re-loaded inside the lock): Beacon already registered the REAL
        # APPROVAL_REQUEST for the same decision (subject collision guard hits).
        fresh = {'pending': [{'id': 'beacon-real-001',
                              'plan_summary': key,
                              'dispatch_payload': {'target_agent': 'beacon'}}],
                 'history': []}
        add_pending = self._drive_main(snapshot, fresh)
        add_pending.assert_not_called()  # no duplicate card appended

    def test_no_race_appends_once_under_lock(self):
        # snapshot empty AND fresh still empty -> healer legitimately promotes,
        # appending exactly once, with state threaded so the lock is held once.
        snapshot = {'pending': [], 'history': []}
        fresh = {'pending': [], 'history': []}
        add_pending = self._drive_main(snapshot, fresh)
        self.assertEqual(add_pending.call_count, 1)
        # appended under the held lock: state=fresh passed (own=False), not None
        _, kwargs = add_pending.call_args
        self.assertIs(kwargs.get('state'), fresh)


# ----- marker recovery from Beacon's outbox archive (the #412 BUG-2 fix) -----

# The pipeline-stall alert that triggered #412's promotion: it NAMES the
# un-dispatched task in its message, which is how the recovered marker is matched.
PR412_STALL_ALERT = {
    'ts': _ts(1),
    'source': 'heal-pipeline-stall',
    'severity': 'warning',
    'route': 'escalate',
    'subject': 'pipeline-stall:pr412-approval-request-not-dispatched',
    'message': (
        'PR #412 Mirror review still not dispatched. Beacon emitted '
        'APPROVAL_REQUEST for mirror-review-pr412-001 but outbox-notifier did '
        'NOT create the Mirror task.'
    ),
    'suggested_action': "Reply 'mirror-review 412' to Beacon to dispatch.",
}

# The real marker Beacon emitted, as it would be recovered from her outbox
# archive (parsed payload of the APPROVAL_REQUEST block).
PR412_MARKER = {
    'task_id': 'mirror-review-pr412-001',
    'summary': 'Dispatch Mirror review of PR #412 (test-only isolation '
               'hardening); the auto-dispatch was missed.',
    'target_agent': 'mirror',
    'task_type': 'code-review',
    'prompt': 'Review PR #412 ...',
}


def _marker_block(payload: dict) -> str:
    return (
        'Some narrative before.\n\n=== APPROVAL_REQUEST ===\n'
        + json.dumps(payload)
        + '\n=== END_APPROVAL_REQUEST ===\nTrailing narrative.'
    )


class LoadBeaconOutboxMarkersTest(unittest.TestCase):
    def _write_outbox(self, d, name, result_text):
        p = d / name
        p.write_text(json.dumps({'agent': 'beacon', 'result': result_text}),
                     encoding='utf-8')
        return p

    def test_recovers_marker_in_window(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write_outbox(d, 'cycle-finding-pr412.json',
                               _marker_block(PR412_MARKER))
            got = h.load_beacon_outbox_markers(NOW, 24, archive_dir=d)
            self.assertEqual([m['task_id'] for m in got], ['mirror-review-pr412-001'])

    def test_skips_files_outside_window(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            p = self._write_outbox(d, 'old.json', _marker_block(PR412_MARKER))
            old = (NOW - timedelta(hours=48)).timestamp()
            os.utime(p, (old, old))
            self.assertEqual(h.load_beacon_outbox_markers(NOW, 24, archive_dir=d), [])

    def test_skips_outbox_without_marker_and_malformed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write_outbox(d, 'no-marker.json', 'just narrative, no marker')
            self._write_outbox(d, 'bad.json',
                               '=== APPROVAL_REQUEST ===\n{not json\n'
                               '=== END_APPROVAL_REQUEST ===')
            self.assertEqual(h.load_beacon_outbox_markers(NOW, 24, archive_dir=d), [])

    def test_missing_archive_dir_is_empty(self):
        self.assertEqual(
            h.load_beacon_outbox_markers(NOW, 24, archive_dir=Path('/no/such/dir')),
            [])


class MatchMarkerForRecordTest(unittest.TestCase):
    def test_matches_by_task_id_named_in_alert(self):
        m = h.match_marker_for_record(PR412_STALL_ALERT, [PR412_MARKER])
        self.assertIsNotNone(m)
        self.assertEqual(m['task_id'], 'mirror-review-pr412-001')

    def test_matches_by_shared_pr_ref(self):
        # task_id not named, but both anchor on PR #412 via a keyword ref.
        alert = dict(PR412_STALL_ALERT,
                     message='Mirror review for pr #412 was never dispatched.',
                     subject='review gap')
        marker = dict(PR412_MARKER, task_id='zzz', summary='review of pr #412')
        self.assertIsNotNone(h.match_marker_for_record(alert, [marker]))

    def test_no_match_returns_none(self):
        alert = dict(PR412_STALL_ALERT, subject='unrelated', message='nothing',
                     suggested_action='do x')
        marker = dict(PR412_MARKER, task_id='other-task-999', summary='unrelated')
        self.assertIsNone(h.match_marker_for_record(alert, [marker]))

    def test_empty_markers_returns_none(self):
        self.assertIsNone(h.match_marker_for_record(PR412_STALL_ALERT, []))

    def test_short_task_id_not_substring_matched(self):
        # A <6-char task_id is too collision-prone to match by substring.
        alert = dict(PR412_STALL_ALERT, message='see abc', subject='x',
                     suggested_action='')
        self.assertIsNone(
            h.match_marker_for_record(alert, [dict(PR412_MARKER, task_id='abc')]))

    def test_task_id_not_matched_inside_longer_alnum_token(self):
        # Word-boundary: the id must not match inside a longer alphanumeric run.
        alert = dict(PR412_STALL_ALERT,
                     message='unrelated task mirror-review-pr412-0019 elsewhere',
                     subject='x', suggested_action='')
        self.assertIsNone(h.match_marker_for_record(alert, [PR412_MARKER]))

    def test_unknown_target_falls_back_to_beacon(self):
        marker = dict(PR412_MARKER, target_agent='some-typo')
        p = h.build_approval_payload_from_marker(marker, PR412_STALL_ALERT, 'k')
        self.assertEqual(p['target_agent'], 'beacon')


class BuildFromMarkerTest(unittest.TestCase):
    def test_clean_payload_from_marker(self):
        key = h.alert_dedup_key(PR412_STALL_ALERT)
        p = h.build_approval_payload_from_marker(PR412_MARKER, PR412_STALL_ALERT, key)
        # Real, readable summary — not the 'needs triage' fallback.
        self.assertIn('Dispatch Mirror review of PR #412', p['summary'])
        self.assertNotIn('needs triage', p['summary'])
        self.assertEqual(p['target_agent'], 'mirror')
        self.assertEqual(p['task_type'], 'code-review')
        self.assertEqual(p['recovered_marker']['task_id'], 'mirror-review-pr412-001')
        # Dedup/ledger keys preserved (same as the alert-derived payload).
        self.assertEqual(p['task_id'], h.derive_task_id(key))
        self.assertEqual(p['origin'], h.HEALER_SOURCE)
        self.assertFalse(p['bare_approvable'])
        self.assertEqual(p['promoted_from_alert'], key)

    def test_recovered_marker_strips_private_keys(self):
        marker = dict(PR412_MARKER, _outbox_mtime=123.0)
        p = h.build_approval_payload_from_marker(marker, PR412_STALL_ALERT, 'k')
        self.assertNotIn('_outbox_mtime', p['recovered_marker'])


class EvaluateWithMarkerLookupTest(unittest.TestCase):
    def _empty_state(self):
        return {'pending': [], 'history': []}

    def test_promotes_clean_card_when_marker_found(self):
        out = h.evaluate(
            [PR412_STALL_ALERT], DEFAULT_HEURISTICS, self._empty_state(), {},
            now=NOW, marker_lookup=lambda rec: PR412_MARKER)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['target_agent'], 'mirror')
        self.assertIn('recovered_marker', out[0])
        self.assertNotIn('needs triage', out[0]['summary'])

    def test_falls_back_to_alert_card_when_no_marker(self):
        out = h.evaluate(
            [PR412_STALL_ALERT], DEFAULT_HEURISTICS, self._empty_state(), {},
            now=NOW, marker_lookup=lambda rec: None)
        self.assertEqual(len(out), 1)
        self.assertNotIn('recovered_marker', out[0])
        self.assertEqual(out[0]['target_agent'], 'beacon')  # alert-derived default

    def test_marker_lookup_exception_falls_back(self):
        def boom(_rec):
            raise RuntimeError('archive read blew up')
        out = h.evaluate(
            [PR412_STALL_ALERT], DEFAULT_HEURISTICS, self._empty_state(), {},
            now=NOW, marker_lookup=boom)
        self.assertEqual(len(out), 1)  # still promotes, via the fallback
        self.assertNotIn('recovered_marker', out[0])

    def test_one_marker_backs_at_most_one_card_per_tick(self):
        # Two distinct approval-class alerts both correlate to the same marker;
        # only the first gets the clean card, the second falls back (no double
        # dispatch proposal for one marker).
        alert_a = dict(PR412_STALL_ALERT, subject='pr412-stall-a')
        alert_b = dict(PR412_STALL_ALERT, subject='pr412-stall-b')
        out = h.evaluate(
            [alert_a, alert_b], DEFAULT_HEURISTICS, self._empty_state(), {},
            now=NOW, marker_lookup=lambda rec: PR412_MARKER)
        self.assertEqual(len(out), 2)
        with_marker = [p for p in out if 'recovered_marker' in p]
        self.assertEqual(len(with_marker), 1)  # exactly one clean card


# ---- for-larry-escalations second scan source (the PR #854 blind spot) ----

# The real 2026-07-08 stranded record: a session-less Mirror REVIEW_ESCALATE on
# PR #854 that landed as an OPEN for-Larry record but never got an
# APPROVAL_REQUEST, so it never reached the Approvals tab.
FORLARRY_TASK = 'sentinel-in-flight-stall-translation-001'
FORLARRY_DECISION_RECORD = {
    'id': f'mirror-review:{FORLARRY_TASK}',
    'source': 'mirror-review',
    'headline': f'Session-less PR needs you: `{FORLARRY_TASK}`',
    'context': (
        'Mirror wants changes but the auto-fix loop cannot proceed. Go unstick '
        'it: re-dispatch a Forge build or close the PR. '
        'PR: https://github.com/Larry-Yatch/ourliberty-agent-core/pull/854'
    ),
    'severity': 'warning',
    'pr_url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/854',
    'head_sha': 'abc12345',
    'dedup_identity': (
        'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/854@abc12345'
    ),
    'decision_key': None,
    'for_larry': True,
    'resolved': False,
    'ts': _ts(6),
    'updated_at': _ts(6),
}

# The hyphen-id decision approval _emit_no_session_decision_approval would
# register for the SAME record (bare form, no head8 suffix).
FORLARRY_HYPHEN_APPROVAL_ID = f'mirror-review-{FORLARRY_TASK}'

FORLARRY_HEURISTICS = dict(
    DEFAULT_HEURISTICS, for_larry_decision_sources=['mirror-review'])


class ForLarryDecisionClassTest(unittest.TestCase):
    def test_open_mirror_review_record_is_decision_class(self):
        self.assertTrue(
            h.is_forlarry_decision_class(FORLARRY_DECISION_RECORD, FORLARRY_HEURISTICS))

    def test_resolved_record_is_not_decision_class(self):
        rec = dict(FORLARRY_DECISION_RECORD, resolved=True)
        self.assertFalse(h.is_forlarry_decision_class(rec, FORLARRY_HEURISTICS))

    def test_non_decision_source_is_not_decision_class(self):
        # An action-needed / FYI record from another source must never promote.
        rec = dict(FORLARRY_DECISION_RECORD,
                   id='ops-alert:disk-low', source='ops-alert')
        self.assertFalse(h.is_forlarry_decision_class(rec, FORLARRY_HEURISTICS))

    def test_source_without_matching_id_prefix_is_not_decision_class(self):
        # source is decision-class but the id lacks the '<source>:' prefix.
        rec = dict(FORLARRY_DECISION_RECORD, id='weird-id-no-prefix')
        self.assertFalse(h.is_forlarry_decision_class(rec, FORLARRY_HEURISTICS))

    def test_predicate_falls_back_to_default_sources(self):
        # heuristics without the key → built-in default (mirror-review) applies.
        self.assertTrue(
            h.is_forlarry_decision_class(FORLARRY_DECISION_RECORD, DEFAULT_HEURISTICS))


class ForLarryNormalizationTest(unittest.TestCase):
    def test_colon_id_normalizes_to_hyphen(self):
        self.assertEqual(
            h.forlarry_norm_id(FORLARRY_DECISION_RECORD['id']),
            FORLARRY_HYPHEN_APPROVAL_ID)

    def test_registered_matches_bare_hyphen_id(self):
        state = {'pending': [{'id': FORLARRY_HYPHEN_APPROVAL_ID}], 'history': []}
        self.assertTrue(
            h.is_forlarry_registered(FORLARRY_HYPHEN_APPROVAL_ID, state))

    def test_registered_matches_head8_suffix_variant(self):
        state = {'pending': [
            {'id': FORLARRY_HYPHEN_APPROVAL_ID + '-deadbeef'}], 'history': []}
        self.assertTrue(
            h.is_forlarry_registered(FORLARRY_HYPHEN_APPROVAL_ID, state))

    def test_registered_in_history_also_matches(self):
        state = {'pending': [], 'history': [
            {'id': FORLARRY_HYPHEN_APPROVAL_ID, 'status': 'approved'}]}
        self.assertTrue(
            h.is_forlarry_registered(FORLARRY_HYPHEN_APPROVAL_ID, state))

    def test_unrelated_id_does_not_match(self):
        # PR #42's approval must not match PR #421's normalized id (delimiter).
        state = {'pending': [
            {'id': FORLARRY_HYPHEN_APPROVAL_ID + '9'}], 'history': []}
        self.assertFalse(
            h.is_forlarry_registered(FORLARRY_HYPHEN_APPROVAL_ID, state))


class BuildForLarryPayloadTest(unittest.TestCase):
    def test_payload_targets_beacon_with_required_fields(self):
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        payload = h.build_for_larry_approval_payload(FORLARRY_DECISION_RECORD, key)
        self.assertEqual(payload['target_agent'], 'beacon')
        self.assertEqual(payload['task_id'], h.derive_task_id(key))
        self.assertEqual(payload['promoted_from_alert'], key)
        self.assertEqual(payload['origin'], h.HEALER_SOURCE)
        self.assertFalse(payload['bare_approvable'])
        for field in approval.REQUIRED_FIELDS['approval_request']:
            self.assertIn(field, payload)
        # the queue owner agrees this healer card is not operator-dispatchable
        self.assertFalse(
            approval._is_operator_dispatchable({'dispatch_payload': payload}))

    def test_payload_carries_pr_link_and_normalized_id(self):
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        payload = h.build_for_larry_approval_payload(FORLARRY_DECISION_RECORD, key)
        self.assertIn('pull/854', payload['summary'])
        self.assertEqual(payload['_subject'], FORLARRY_DECISION_RECORD['id'])
        self.assertEqual(payload['_forlarry_norm_id'], FORLARRY_HYPHEN_APPROVAL_ID)


class EvaluateForLarryTest(unittest.TestCase):
    def _empty_state(self):
        return {'pending': [], 'history': []}

    def test_open_decision_record_promoted(self):
        # criterion: open decision-class record, no matching approval -> promote.
        out = h.evaluate_for_larry(
            [FORLARRY_DECISION_RECORD], FORLARRY_HEURISTICS,
            self._empty_state(), {})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['target_agent'], 'beacon')
        self.assertEqual(
            out[0]['promoted_from_alert'],
            h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id']))

    def test_matching_hyphen_approval_dedups(self):
        # criterion: a record that DID get an approval (hyphen id) -> no promote.
        state = {'pending': [{'id': FORLARRY_HYPHEN_APPROVAL_ID}], 'history': []}
        out = h.evaluate_for_larry(
            [FORLARRY_DECISION_RECORD], FORLARRY_HEURISTICS, state, {})
        self.assertEqual(out, [])

    def test_matching_hyphen_approval_with_head8_dedups(self):
        state = {'pending': [
            {'id': FORLARRY_HYPHEN_APPROVAL_ID + '-abc12345'}], 'history': []}
        out = h.evaluate_for_larry(
            [FORLARRY_DECISION_RECORD], FORLARRY_HEURISTICS, state, {})
        self.assertEqual(out, [])

    def test_resolved_record_skipped(self):
        # criterion: resolved=True -> no promote.
        rec = dict(FORLARRY_DECISION_RECORD, resolved=True)
        out = h.evaluate_for_larry(
            [rec], FORLARRY_HEURISTICS, self._empty_state(), {})
        self.assertEqual(out, [])

    def test_action_needed_record_skipped(self):
        # criterion: non-decision (other source) -> no promote.
        rec = dict(FORLARRY_DECISION_RECORD,
                   id='ops-alert:disk-low', source='ops-alert')
        out = h.evaluate_for_larry(
            [rec], FORLARRY_HEURISTICS, self._empty_state(), {})
        self.assertEqual(out, [])

    def test_already_promoted_skipped(self):
        # criterion: promoted-ledger dedup (namespaced key) -> idempotent re-run.
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        out = h.evaluate_for_larry(
            [FORLARRY_DECISION_RECORD], FORLARRY_HEURISTICS,
            self._empty_state(), {key: {'task_id': 'x', 'promoted_at': _ts(0)}})
        self.assertEqual(out, [])

    def test_duplicate_records_same_tick_promoted_once(self):
        out = h.evaluate_for_larry(
            [FORLARRY_DECISION_RECORD, dict(FORLARRY_DECISION_RECORD)],
            FORLARRY_HEURISTICS, self._empty_state(), {})
        self.assertEqual(len(out), 1)


class ForLarryConfigTest(unittest.TestCase):
    def test_shipped_config_lists_mirror_review(self):
        cfg = h.load_heuristics()  # the real config file
        self.assertIn('mirror-review', cfg['for_larry_decision_sources'])

    def test_missing_config_falls_back_to_default_sources(self):
        cfg = h.load_heuristics(Path('/nonexistent/heuristics.json'))
        self.assertEqual(cfg['for_larry_decision_sources'],
                         list(h.DEFAULT_FORLARRY_DECISION_SOURCES))


class ForLarryMainIntegrationTest(unittest.TestCase):
    """The second source, end-to-end through main(): a stranded for-Larry
    decision record is promoted via the SAME locked add_pending/emit/ledger
    machinery, and recorded under a namespaced ledger key."""

    def _drive_main(self, records, snapshot_state, fresh_state, promoted=None):
        import contextlib
        add_pending = mock.MagicMock(side_effect=lambda p, **kw: {'id': p['task_id']})
        save_promoted = mock.MagicMock()
        with mock.patch.object(h, 'kill_switch',
                               return_value=Path('/nonexistent/kill-switch')), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'log'), \
             mock.patch.object(h, 'load_heuristics',
                               return_value=FORLARRY_HEURISTICS), \
             mock.patch.object(h, 'read_alerts', return_value=[]), \
             mock.patch.object(h, 'read_for_larry_records', return_value=records), \
             mock.patch.object(h, 'load_beacon_outbox_markers', return_value=[]), \
             mock.patch.object(h, 'load_promoted', return_value=promoted or {}), \
             mock.patch.object(h, 'save_promoted', new=save_promoted), \
             mock.patch.object(h, 'reconcile_retire',
                               side_effect=lambda led, *a, **k: ([], led)), \
             mock.patch.object(h, '_chat_id', return_value=4242), \
             mock.patch.object(h, 'doorbell_counts', return_value=(0, 0)), \
             mock.patch('for_larry_escalations.clear', return_value=True), \
             mock.patch.object(h, 'emit_approval_event', return_value=True), \
             mock.patch.object(approval, 'state_lock',
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(approval, 'save_state'), \
             mock.patch.object(approval, 'add_pending', new=add_pending), \
             mock.patch.object(approval, 'load_state',
                               side_effect=[snapshot_state, fresh_state]):
            rc = h.main()
        self.assertEqual(rc, 0)
        return add_pending, save_promoted

    def test_stranded_record_promoted_and_ledgered(self):
        add_pending, save_promoted = self._drive_main(
            [FORLARRY_DECISION_RECORD],
            {'pending': [], 'history': []},
            {'pending': [], 'history': []})
        self.assertEqual(add_pending.call_count, 1)
        appended = add_pending.call_args[0][0]
        self.assertEqual(appended['target_agent'], 'beacon')
        self.assertNotIn('_forlarry_norm_id', appended)  # helper key stripped
        # recorded under the namespaced ledger key
        ledger = save_promoted.call_args[0][0]
        self.assertIn(
            h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id']), ledger)

    def test_already_registered_hyphen_approval_not_duplicated(self):
        # Beacon already registered the hyphen decision approval -> no append.
        add_pending, _ = self._drive_main(
            [FORLARRY_DECISION_RECORD],
            {'pending': [{'id': FORLARRY_HYPHEN_APPROVAL_ID}], 'history': []},
            {'pending': [{'id': FORLARRY_HYPHEN_APPROVAL_ID}], 'history': []})
        add_pending.assert_not_called()

    def test_idempotent_when_ledger_already_has_record(self):
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        add_pending, _ = self._drive_main(
            [FORLARRY_DECISION_RECORD],
            {'pending': [], 'history': []},
            {'pending': [], 'history': []},
            promoted={key: {'task_id': 'x', 'promoted_at': _ts(0)}})
        add_pending.assert_not_called()


# ===================================================================
# doorbell<->tab reconciler invariant (doorbell-tab-approval-reconciler-001):
# idempotency (defect 1), null-chat (defect 2), resolve-on-promote (defect 3),
# verify-rendered / repair-failure alert (defect 4), doorbell reconcile (A).
# ===================================================================

class HealerTaskRegisteredTest(unittest.TestCase):
    """Defect 1: the healer registers a for-larry card under its OWN deterministic
    `unreg-approval-<hash>` task_id, NOT the hyphen decision id — so
    is_forlarry_registered alone misses it and the card re-promotes every tick.
    `_healer_task_registered` is the belt-and-suspenders guard: it recognizes the
    healer's own promoted task_id already sitting in pending/history."""

    def _state_with_entry(self, entry_id):
        return {'pending': [{'id': entry_id}], 'history': []}

    def test_true_when_own_task_id_in_pending(self):
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        task_id = h.derive_task_id(key)
        self.assertTrue(
            h._healer_task_registered(task_id, self._state_with_entry(task_id)))

    def test_true_when_own_task_id_in_history(self):
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        task_id = h.derive_task_id(key)
        state = {'pending': [],
                 'history': [{'id': task_id, 'status': 'approved'}]}
        self.assertTrue(h._healer_task_registered(task_id, state))

    def test_false_when_absent(self):
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        task_id = h.derive_task_id(key)
        self.assertFalse(
            h._healer_task_registered(task_id, {'pending': [], 'history': []}))

    def test_evaluate_for_larry_skips_when_own_task_registered(self):
        # The regression: the hyphen id is NOT in state (Beacon never registered
        # it), but the healer's OWN card from a prior tick IS. Must not re-promote.
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        task_id = h.derive_task_id(key)
        state = self._state_with_entry(task_id)
        out = h.evaluate_for_larry(
            [FORLARRY_DECISION_RECORD], FORLARRY_HEURISTICS, state, {})
        self.assertEqual(out, [])


class PrimaryChatIdTest(unittest.TestCase):
    """Defect 2: chat resolution. _primary_chat_id is the LOWEST allowed chat
    (the #812 null-chat fix); _chat_id honors the explicit override first, then
    falls back to it, and is None only when neither exists."""

    def test_primary_picks_lowest_allowed(self):
        with mock.patch.dict(
                os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '900, 100, 500'}):
            self.assertEqual(h._primary_chat_id(), 100)

    def test_primary_none_when_unset(self):
        with mock.patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': ''}):
            self.assertIsNone(h._primary_chat_id())

    def test_primary_ignores_garbage_tokens(self):
        with mock.patch.dict(
                os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': 'x, 42, y'}):
            self.assertEqual(h._primary_chat_id(), 42)

    def test_chat_id_override_wins(self):
        with mock.patch.dict(
                os.environ,
                {'OURLIBERTY_APPROVAL_HEALER_CHAT_ID': '7',
                 'TELEGRAM_ALLOWED_CHAT_IDS': '100'}):
            self.assertEqual(h._chat_id(), 7)

    def test_chat_id_falls_back_to_primary(self):
        with mock.patch.dict(
                os.environ,
                {'OURLIBERTY_APPROVAL_HEALER_CHAT_ID': '',
                 'TELEGRAM_ALLOWED_CHAT_IDS': '100, 50'}):
            self.assertEqual(h._chat_id(), 50)

    def test_chat_id_none_when_nothing_resolvable(self):
        with mock.patch.dict(
                os.environ,
                {'OURLIBERTY_APPROVAL_HEALER_CHAT_ID': '',
                 'TELEGRAM_ALLOWED_CHAT_IDS': ''}):
            self.assertIsNone(h._chat_id())


class DoorbellCountsTest(unittest.TestCase):
    """Behavior A: reconcile against the SAME State Log snapshot the doorbell
    counts. doorbell_counts delegates to doorbell_notifier.load_waiting and never
    raises — an unreadable snapshot is no signal (None), not a crash."""

    def test_reads_load_waiting_counts(self):
        import doorbell_notifier as dbell
        with mock.patch.object(
                dbell, 'load_waiting',
                return_value={'pending_approvals': 2, 'escalations': 3}):
            self.assertEqual(h.doorbell_counts(), (2, 3))

    def test_negative_and_missing_coerce_to_zero(self):
        import doorbell_notifier as dbell
        with mock.patch.object(
                dbell, 'load_waiting', return_value={'pending_approvals': -1}):
            self.assertEqual(h.doorbell_counts(), (0, 0))

    def test_unreadable_snapshot_is_none(self):
        import doorbell_notifier as dbell
        with mock.patch.object(
                dbell, 'load_waiting', side_effect=RuntimeError('boom')):
            self.assertIsNone(h.doorbell_counts())

    def test_reconcile_log_never_alerts(self):
        # The reconcile heartbeat is observability only (actionable-only, F):
        # it must not fan out to the self-failure alert path.
        with mock.patch.object(h, 'doorbell_counts', return_value=(1, 1)), \
             mock.patch.object(h, '_emit_self_failure') as fail:
            h._log_doorbell_reconcile(
                promoted_count=1, repair_failures=0, retired=0)
        fail.assert_not_called()


class ReconcilerMainTest(unittest.TestCase):
    """End-to-end through main() for the reconciler invariant: null-chat skip
    (defect 2), resolve-on-promote (defect 3), and verify-rendered / repair-
    failure alert (defect 4), all sharing one hermetic driver."""

    def _drive(self, *, records, chat_id, emit=True, fresh=None,
               snapshot=None, promoted=None):
        import contextlib
        snapshot = snapshot or {'pending': [], 'history': []}
        fresh = fresh if fresh is not None else {'pending': [], 'history': []}
        add_pending = mock.MagicMock(
            side_effect=lambda p, **kw: {'id': p['task_id']})
        save_promoted = mock.MagicMock()
        self_fail = mock.MagicMock()
        fle_clear = mock.MagicMock(return_value=True)
        with mock.patch.object(h, 'kill_switch',
                               return_value=Path('/nonexistent/kill-switch')), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'log'), \
             mock.patch.object(h, 'load_heuristics',
                               return_value=FORLARRY_HEURISTICS), \
             mock.patch.object(h, 'read_alerts', return_value=[]), \
             mock.patch.object(h, 'read_for_larry_records',
                               return_value=records), \
             mock.patch.object(h, 'load_beacon_outbox_markers',
                               return_value=[]), \
             mock.patch.object(h, 'load_promoted', return_value=promoted or {}), \
             mock.patch.object(h, 'save_promoted', new=save_promoted), \
             mock.patch.object(h, 'reconcile_retire',
                               side_effect=lambda led, *a, **k: ([], led)), \
             mock.patch.object(h, '_chat_id', return_value=chat_id), \
             mock.patch.object(h, 'doorbell_counts', return_value=(0, 0)), \
             mock.patch.object(h, '_emit_self_failure', new=self_fail), \
             mock.patch('for_larry_escalations.clear', new=fle_clear), \
             mock.patch.object(h, 'emit_approval_event', return_value=emit), \
             mock.patch.object(approval, 'state_lock',
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(approval, 'save_state'), \
             mock.patch.object(approval, 'add_pending', new=add_pending), \
             mock.patch.object(approval, 'load_state',
                               side_effect=[snapshot, fresh]):
            rc = h.main()
        return {'rc': rc, 'add_pending': add_pending,
                'save_promoted': save_promoted, 'self_fail': self_fail,
                'fle_clear': fle_clear}

    # --- defect 2: null chat ---
    def test_null_chat_skips_and_alerts(self):
        r = self._drive(records=[FORLARRY_DECISION_RECORD], chat_id=None)
        self.assertEqual(r['rc'], 1)
        r['add_pending'].assert_not_called()      # never registers null-chat
        self.assertEqual(r['self_fail'].call_count, 1)  # actionable alert (F)

    # --- defect 3: resolve source escalation on confirmed render ---
    def test_confirmed_render_resolves_source_record(self):
        r = self._drive(records=[FORLARRY_DECISION_RECORD], chat_id=4242,
                        emit=True)
        self.assertEqual(r['rc'], 0)
        r['add_pending'].assert_called_once()
        r['fle_clear'].assert_called_once_with(FORLARRY_DECISION_RECORD['id'])
        r['self_fail'].assert_not_called()        # happy path: no nag (F)

    # --- defect 4: unconfirmed render is a repair-failure, not silent ok ---
    def test_failed_render_alerts_and_does_not_resolve(self):
        r = self._drive(records=[FORLARRY_DECISION_RECORD], chat_id=4242,
                        emit=False)
        self.assertEqual(r['rc'], 0)
        r['add_pending'].assert_called_once()     # pending entry still written
        r['fle_clear'].assert_not_called()        # source stays open (counted)
        self.assertEqual(r['self_fail'].call_count, 1)  # exactly one alert
        # nothing recorded in the ledger for an unconfirmed render
        ledger = r['save_promoted'].call_args[0][0]
        self.assertNotIn(
            h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id']), ledger)

    # --- happy path with nothing to promote: reconcile heartbeat, no alert ---
    def test_clean_tick_no_promotions_no_alert(self):
        r = self._drive(records=[], chat_id=4242)
        self.assertEqual(r['rc'], 0)
        r['add_pending'].assert_not_called()
        r['self_fail'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
