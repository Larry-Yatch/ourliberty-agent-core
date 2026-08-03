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
# Binary suggested_action so the resolution-gate tests exercise a promotable
# (non-needs-triage) decision — the resolution machinery is orthogonal to the
# binary/needs-triage split, so the fixture must be a genuine binary ask.
PR294_ALERT = {
    'ts': _ts(3),
    'source': 'pulse/beacon-result',
    'severity': 'warning',
    'route': 'escalate',
    'subject': 'PR #294 Mirror review gap — source=larry routing miss',
    'message': 'PR #294 Mirror review gap needs your call before I proceed.',
    'suggested_action': 'Choose rebuild-the-review or waive-the-gate',
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
    return lambda n, repo=None: True if n in wanted else None


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
            gh_probe=lambda n, repo=None: None)
        self.assertIsNone(reason)

    def test_open_pr_is_no_signal(self):
        reason = h.resolution_signal(
            PR294_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=lambda n, repo=None: False)
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
            gh_probe=lambda n, repo=None: None)
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
            gh_probe=lambda n, repo=None: None)
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
            after_ts=DEPLOY_NOTIFIER_A['ts'], gh_probe=lambda n, repo=None: None)
        self.assertIsNotNone(reason)

    def test_live_ask_has_no_signal(self):
        reason = h.resolution_signal(
            CYCLE_TIMER_ALERT, self._empty(), [CYCLE_TIMER_ALERT],
            DEFAULT_HEURISTICS, gh_probe=lambda n, repo=None: None)
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
        check = self._check(state, [PR294_ALERT], lambda n, repo=None: None)
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
        check = self._check(state, [CYCLE_TIMER_ALERT], lambda n, repo=None: None)
        out = h.evaluate([CYCLE_TIMER_ALERT], DEFAULT_HEURISTICS, state, {},
                         now=NOW, resolution_check=check)
        self.assertEqual(len(out), 1)

    def test_pulse_source_escalation_not_promoted(self):
        # Guard 2: a source=pulse operational escalation (Pulse observing its
        # OWN prior stale-approval notice) must NOT be promoted; the
        # SKIP_PULSE_SOURCE path is exercised.
        pulse_alert = {
            'ts': _ts(1),
            'source': 'pulse',
            'route': 'escalate',
            'needs_larry': True,
            'subject': 'stale-pending-approval-unreg-approval-ce90b1a4c981',
            'message': 'A prior healer-created approval is still pending.',
            'suggested_action': 'Choose retire-it or keep-it',
        }
        with mock.patch.object(h, 'log') as mock_log:
            out = h.evaluate([pulse_alert], DEFAULT_HEURISTICS,
                             self._empty(), {}, now=NOW)
        self.assertEqual(out, [])
        self.assertTrue(any(
            'SKIP_PULSE_SOURCE' in call.args[0] for call in mock_log.call_args_list),
            'SKIP_PULSE_SOURCE should be logged for a source=pulse escalation')

    def test_non_pulse_source_escalation_still_promoted(self):
        # Guard 2 is source-SCOPED, not blanket: an identically-shaped alert from
        # a non-pulse source (incl. a Pulse-relayed 'pulse/<origin>-result') is
        # still promoted.
        control_alert = {
            'ts': _ts(1),
            'source': 'pulse/beacon-result',
            'route': 'escalate',
            'needs_larry': True,
            'subject': 'stale-pending-approval-unreg-approval-ce90b1a4c981',
            'message': 'A prior healer-created approval is still pending.',
            'suggested_action': 'Choose retire-it or keep-it',
        }
        out = h.evaluate([control_alert], DEFAULT_HEURISTICS,
                         self._empty(), {}, now=NOW)
        self.assertEqual(len(out), 1)

    def test_merged_pr_in_message_body_not_promoted(self):
        # Guard 1: a merged PR referenced in the message/suggested_action (NOT
        # the subject) triggers the skip; SKIP_MERGED_PR is surfaced in the log.
        merged_body_alert = {
            'ts': _ts(1),
            'source': 'heal-dispatch-router',
            'route': 'escalate',
            'needs_larry': True,
            'subject': 'm3-pr2-re-dispatch-routing-gap',
            'message': 'The m3-pr2 re-dispatch never routed; see PR #25.',
            'suggested_action': 'Choose rebuild-the-dispatch or waive-it',
        }
        state = self._empty()
        check = self._check(state, [merged_body_alert], _gh_merged([25]))
        with mock.patch.object(h, 'log') as mock_log:
            out = h.evaluate([merged_body_alert], DEFAULT_HEURISTICS, state, {},
                             now=NOW, resolution_check=check)
        self.assertEqual(out, [])
        self.assertTrue(any(
            'SKIP_MERGED_PR' in call.args[0] for call in mock_log.call_args_list),
            'SKIP_MERGED_PR should be logged for a merged-ref skip')

    def test_merged_ref_in_body_undetermined_probe_still_promoted(self):
        # Guard 1 tri-state contract: an undetermined probe (None) over the same
        # body-referenced PR must NOT skip — the ask is still surfaced.
        merged_body_alert = {
            'ts': _ts(1),
            'source': 'heal-dispatch-router',
            'route': 'escalate',
            'needs_larry': True,
            'subject': 'm3-pr2-re-dispatch-routing-gap',
            'message': 'The m3-pr2 re-dispatch never routed; see PR #25.',
            'suggested_action': 'Choose rebuild-the-dispatch or waive-it',
        }
        state = self._empty()
        check = self._check(state, [merged_body_alert], lambda n, repo=None: None)
        out = h.evaluate([merged_body_alert], DEFAULT_HEURISTICS, state, {},
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
            now=NOW, gh_probe=lambda n, repo=None: None)
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
    """The needs_larry unrouted-PR nudge is a NON-binary alert: since the
    needs-triage fix it is NOT promoted to the Approvals tab (Approve/Reject on
    it fell to a generic Beacon envelope — a paid no-op — and it never auto-
    retired). The nudge alert itself is untouched and still visible in the alert
    stream; only the promotion is suppressed."""

    def _empty(self):
        return {'pending': [], 'history': []}

    def test_unrouted_pr_not_promoted(self):
        # criterion 1: a non-binary alert-derived ask is not promoted; its
        # identity is recorded in the skip sink so main() dedups it next tick.
        sink = []
        out = h.evaluate([UNROUTED_PR_ALERT], DEFAULT_HEURISTICS,
                         self._empty(), {}, now=NOW, skipped_needs_triage=sink)
        self.assertEqual(out, [])
        self.assertEqual(
            [s['identity'] for s in sink],
            [h.decision_identity(UNROUTED_PR_ALERT)])

    def test_refired_alert_still_not_promoted(self):
        # A second identical fire for PR #28 in the same tick -> still no card.
        out = h.evaluate([UNROUTED_PR_ALERT, dict(UNROUTED_PR_ALERT)],
                         DEFAULT_HEURISTICS, self._empty(), {}, now=NOW)
        self.assertEqual(out, [])

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


class NeedsTriagePreventionTest(unittest.TestCase):
    """The needs-triage suppression gate: a non-binary alert-derived ask is NOT
    promoted (criterion 1); a binary ask IS (criterion 2); a marker-backed ask IS
    even when its alert text is non-binary (criterion 3)."""

    def _empty(self):
        return {'pending': [], 'history': []}

    def _non_binary(self):
        # Approval-class (the 'Reply' prefix + 'needs your call' in the message)
        # but NOT a binary decision: parse_binary_options returns None.
        return dict(PR294_ALERT, suggested_action='Reply with how to close it')

    def test_non_binary_alert_is_not_promoted(self):
        sink = []
        alert = self._non_binary()
        out = h.evaluate([alert], DEFAULT_HEURISTICS, self._empty(), {},
                         now=NOW, skipped_needs_triage=sink)
        self.assertEqual(out, [])
        self.assertEqual([s['identity'] for s in sink],
                         [h.decision_identity(alert)])
        self.assertEqual(sink[0]['task_id'],
                         h.derive_task_id(h.decision_identity(alert)))

    def test_suppression_works_without_a_sink(self):
        out = h.evaluate([self._non_binary()], DEFAULT_HEURISTICS,
                         self._empty(), {}, now=NOW)
        self.assertEqual(out, [])

    def test_binary_alert_still_promoted(self):
        out = h.evaluate([DEPLOY_NOTIFIER_ALERT], DEFAULT_HEURISTICS,
                         self._empty(), {}, now=NOW)
        self.assertEqual(len(out), 1)
        self.assertNotIn('needs triage', out[0]['summary'].lower())

    def test_marker_backed_non_binary_alert_still_promoted(self):
        # Even though the alert text is non-binary, a recovered Beacon marker
        # backs a clean card — the gate keys on (marker is None AND non-binary).
        alert = dict(PR412_STALL_ALERT, suggested_action='Reply mirror-review')
        sink = []
        out = h.evaluate([alert], DEFAULT_HEURISTICS, self._empty(), {},
                         now=NOW, marker_lookup=lambda rec: PR412_MARKER,
                         skipped_needs_triage=sink)
        self.assertEqual(len(out), 1)
        self.assertIn('recovered_marker', out[0])
        self.assertEqual(sink, [])


class RetireNeedsTriageTest(unittest.TestCase):
    """criterion 5: the retire pass clears live needs-triage cards, is
    idempotent, and NEVER touches a binary / mirror-review / recheck_target
    card. It fails closed on promoted_source and recheck_target."""

    def _card(self, task_id, *, summary=None, payload_extra=None):
        payload = {
            'task_id': task_id,
            'summary': summary if summary is not None else h.NEEDS_TRIAGE_SUMMARY,
            'target_agent': 'beacon',
            'origin': h.HEALER_SOURCE,
            'bare_approvable': False,
        }
        if payload_extra:
            payload.update(payload_extra)
        return {
            'id': task_id, 'status': 'pending',
            'plan_summary': payload['summary'],
            'dispatch_payload': payload,
        }

    def test_needs_triage_card_is_retired(self):
        tid = h.derive_task_id('ref:unrouted-pr')
        state = {'pending': [self._card(tid)], 'history': []}
        retired = h.retire_needs_triage_cards(state)
        self.assertEqual([t for t, _ in retired], [tid])
        self.assertEqual(state['pending'], [])
        self.assertTrue(any(
            e['id'] == tid and e['status'] == 'expired'
            for e in state['history']))

    def test_retire_is_idempotent(self):
        tid = h.derive_task_id('ref:unrouted-pr')
        state = {'pending': [self._card(tid)], 'history': []}
        h.retire_needs_triage_cards(state)
        retired2 = h.retire_needs_triage_cards(state)
        self.assertEqual(retired2, [])

    def test_binary_card_is_not_retired(self):
        tid = h.derive_task_id('ref:binary')
        binary_summary = ('Direction needed (promoted from a missed marker): '
                          'Approve = ship; Reject = hold.')
        state = {'pending': [self._card(tid, summary=binary_summary)],
                 'history': []}
        self.assertEqual(h.retire_needs_triage_cards(state), [])
        self.assertEqual(len(state['pending']), 1)

    def test_mirror_review_card_is_not_retired(self):
        # Fail closed: even with a matching summary, a promoted_source card is
        # the #1060 for-larry-mirror-review class and must never be retired.
        tid = h.derive_task_id('ref:mirror')
        state = {'pending': [self._card(
            tid, payload_extra={'promoted_source': h.PROMOTED_SOURCE_FORLARRY})],
            'history': []}
        self.assertEqual(h.retire_needs_triage_cards(state), [])
        self.assertEqual(len(state['pending']), 1)

    def test_recheck_target_card_is_not_retired(self):
        # Fail closed: a recheck_target-bearing card is never retired.
        tid = h.derive_task_id('ref:recheck')
        state = {'pending': [self._card(
            tid, payload_extra={'recheck_target': {'pr_url': 'https://x/pull/1'}})],
            'history': []}
        self.assertEqual(h.retire_needs_triage_cards(state), [])
        self.assertEqual(len(state['pending']), 1)

    def test_foreign_id_card_is_not_retired(self):
        # A card whose id is not the healer's unreg-approval-* prefix is ignored
        # even if its summary matches.
        state = {'pending': [self._card('mirror-review-pr9-001')], 'history': []}
        self.assertEqual(h.retire_needs_triage_cards(state), [])
        self.assertEqual(len(state['pending']), 1)


class NeedsTriageSummaryConstantTest(unittest.TestCase):
    """criterion 6: the needs-triage summary lives in exactly one module
    constant, referenced by both the creation path and the retire matcher."""

    def test_build_payload_uses_the_constant(self):
        rec = {'route': 'escalate', 'subject': 'x',
               'message': 'needs your call', 'suggested_action': 'Reply now'}
        p = h.build_approval_payload(rec, 'x')
        self.assertEqual(p['summary'], h.NEEDS_TRIAGE_SUMMARY)

    def test_constant_reads_as_needs_triage(self):
        self.assertIn('needs triage', h.NEEDS_TRIAGE_SUMMARY.lower())


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
    # Binary so the marker-recovery FALLBACK path (no recovered marker) still
    # produces an alert-derived card — the recovery machinery matches on the
    # task_id/PR-ref in the message, independent of this suggested_action.
    'suggested_action': 'Choose dispatch-the-review or defer-it',
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

    def test_payload_is_marked_as_a_promoted_stranded_escalation(self):
        # agent-core #1058: the dashboard keys the Approve-executes routing on
        # this marker, so it must be stamped on EVERY for-Larry promotion.
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        payload = h.build_for_larry_approval_payload(FORLARRY_DECISION_RECORD, key)
        self.assertEqual(payload['promoted_source'], h.PROMOTED_SOURCE_FORLARRY)

    def test_payload_stamps_recheck_target_from_the_record(self):
        key = h.forlarry_dedup_key(FORLARRY_DECISION_RECORD['id'])
        payload = h.build_for_larry_approval_payload(FORLARRY_DECISION_RECORD, key)
        self.assertEqual(payload['recheck_target'], {
            'task_id': FORLARRY_TASK,
            'pr_url': FORLARRY_DECISION_RECORD['pr_url'],
            'target_repo': 'ourliberty-agent-core',
            'head_sha': 'abc12345',
            'round': 1,
            'replan_count': 0,
        })
        # ... and the card text promises the action Approve will now take.
        self.assertIn('re-dispatch the Mirror review', payload['summary'])
        self.assertIn('re-dispatch the Mirror review', payload['prompt'])

    def test_payload_without_a_coordinate_says_approve_cannot_execute(self):
        # Fail-closed twin: no pr_url ⇒ no recheck_target, and the card must
        # SAY Approve has nothing to execute instead of promising an action.
        rec = dict(FORLARRY_DECISION_RECORD)
        rec.pop('pr_url')
        key = h.forlarry_dedup_key(rec['id'])
        payload = h.build_for_larry_approval_payload(rec, key)
        self.assertNotIn('recheck_target', payload)
        self.assertIn('cannot be auto-executed', payload['summary'])
        self.assertEqual(payload['promoted_source'], h.PROMOTED_SOURCE_FORLARRY)


class BuildPromotedRecheckTargetTest(unittest.TestCase):
    def test_full_record_yields_a_complete_coordinate(self):
        target = h.build_promoted_recheck_target(FORLARRY_DECISION_RECORD)
        self.assertEqual(target['task_id'], FORLARRY_TASK)
        self.assertEqual(target['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(target['head_sha'], 'abc12345')
        self.assertEqual(target['round'], 1)

    def test_target_repo_is_the_bare_name_mirrors_inbox_accepts(self):
        """Review round 1 defect: this stamped the `owner/repo` slug from the
        PR URL. Every gate downstream (routing_validator.check_target_repo,
        inbox_watcher's repo_paths lookup) matches BARE canonical names by
        exact membership — so the dispatched review was black-holed to
        mirror/.invalid while the card cleared and the API reported success."""
        import routing_validator as rv
        target = h.build_promoted_recheck_target(FORLARRY_DECISION_RECORD)
        repo = target['target_repo']
        self.assertNotIn('/', repo)
        # The authoritative check, not a re-derivation of the naming rule.
        ok, reason = rv.check_target_repo('mirror', repo)
        self.assertTrue(ok, reason)

    def test_unloadable_repo_config_fails_the_stamp_closed(self):
        """Review round 2 defect: check_target_repo FAILS OPEN — it returns
        ok=True when the agent has no configured allowed_repos, which is what
        an unreadable/malformed agent-models.json produces (_load_models_config
        collapses any error to {} and caches it). canonical_repo also returns
        the name unchanged with nothing to match against, so trusting `ok`
        alone re-stamped the raw owner/repo slug and silently reproduced the
        round-1 black-hole for the whole tick."""
        import routing_validator as rv
        saved = rv._MODELS_CACHE.get('config')
        rv._MODELS_CACHE['config'] = {}          # simulate the failed load
        try:
            # The gate itself still says "fine" — that is the trap.
            self.assertEqual(
                rv.check_target_repo('mirror', 'Larry-Yatch/x'), (True, None))
            self.assertIsNone(
                h.build_promoted_recheck_target(FORLARRY_DECISION_RECORD))
            self.assertIsNone(
                h.dispatchable_target_repo('Larry-Yatch/ourliberty-agent-core'))
        finally:
            if saved is None:
                rv._MODELS_CACHE.pop('config', None)
            else:
                rv._MODELS_CACHE['config'] = saved

    def test_repo_outside_mirrors_allowlist_fails_the_stamp_closed(self):
        """A PR in a repo Mirror may not target must not get a coordinate:
        stamping one would promise an Approve that the inbox then discards."""
        rec = dict(
            FORLARRY_DECISION_RECORD,
            pr_url='https://github.com/Larry-Yatch/some-other-repo/pull/7',
        )
        self.assertIsNone(h.build_promoted_recheck_target(rec))

    def test_round_follows_the_records_revision_count(self):
        # revision_count is the round already reviewed, so the next is +1 —
        # matching outbox_notifier._build_recheck_target.
        rec = dict(FORLARRY_DECISION_RECORD, revision_count=2, replan_count=1)
        target = h.build_promoted_recheck_target(rec)
        self.assertEqual(target['round'], 3)
        self.assertEqual(target['replan_count'], 1)

    def test_missing_round_fields_fall_back_to_round_one(self):
        # Records written before the fields existed keep the prior behavior.
        target = h.build_promoted_recheck_target(FORLARRY_DECISION_RECORD)
        self.assertEqual(target['round'], 1)
        self.assertEqual(target['replan_count'], 0)

    def test_bogus_round_fields_fall_back_to_round_one(self):
        for bogus in ('2', -1, None, [], 1.5):
            with self.subTest(bogus=bogus):
                rec = dict(FORLARRY_DECISION_RECORD,
                           revision_count=bogus, replan_count=bogus)
                target = h.build_promoted_recheck_target(rec)
                self.assertEqual(target['round'], 1)
                self.assertEqual(target['replan_count'], 0)

    def test_missing_head_uses_the_resolver(self):
        rec = dict(FORLARRY_DECISION_RECORD)
        rec.pop('head_sha')
        calls = []

        def resolver(owner_repo, number):
            calls.append((owner_repo, number))
            return 'f' * 40

        target = h.build_promoted_recheck_target(rec, head_resolver=resolver)
        self.assertEqual(calls, [('Larry-Yatch/ourliberty-agent-core', 854)])
        self.assertEqual(target['head_sha'], 'f' * 40)

    def test_record_head_wins_over_the_resolver(self):
        # The record's head is the head the review actually covered — never
        # burn a gh call (or risk a moved-head mismatch) when it is present.
        target = h.build_promoted_recheck_target(
            FORLARRY_DECISION_RECORD,
            head_resolver=lambda *_: self.fail('resolver must not be called'))
        self.assertEqual(target['head_sha'], 'abc12345')

    def test_unresolvable_head_still_yields_a_dispatchable_coordinate(self):
        """Review round 1 defect: an unresolvable head returned None, so ONE
        transient gh flake permanently downgraded the card — nothing ever
        re-stamps an existing pending entry, so Approve 400'd forever. The
        dispatch consumer never needed the head (it resolves live and refuses
        to use a stamped one), so the head is now simply omitted."""
        rec = dict(FORLARRY_DECISION_RECORD)
        rec.pop('head_sha')
        for resolver in (None, lambda *_: None):
            with self.subTest(resolver=resolver):
                target = h.build_promoted_recheck_target(
                    rec, head_resolver=resolver)
                self.assertIsNotNone(target)
                self.assertNotIn('head_sha', target)
                # Everything the dispatch actually validates is present.
                for field in ('task_id', 'pr_url', 'target_repo'):
                    self.assertTrue(target.get(field), field)

    def test_resolver_exception_omits_the_head_without_losing_the_card(self):
        rec = dict(FORLARRY_DECISION_RECORD)
        rec.pop('head_sha')

        def boom(*_):
            raise RuntimeError('gh unavailable')

        target = h.build_promoted_recheck_target(rec, head_resolver=boom)
        self.assertIsNotNone(target)
        self.assertNotIn('head_sha', target)

    def test_headless_card_still_promises_the_approve_action(self):
        # The card text is driven by coordinate presence, so a headless
        # coordinate must still say Approve will dispatch — it will.
        rec = dict(FORLARRY_DECISION_RECORD)
        rec.pop('head_sha')
        key = h.forlarry_dedup_key(rec['id'])
        payload = h.build_for_larry_approval_payload(rec, key)
        self.assertIn('recheck_target', payload)
        self.assertIn('re-dispatch the Mirror review', payload['summary'])

    def test_missing_or_malformed_pr_url_fails_the_stamp_closed(self):
        for bad in (None, '', 'https://example.com/nope',
                    'https://github.com/o/r/issues/854'):
            with self.subTest(pr_url=bad):
                rec = dict(FORLARRY_DECISION_RECORD, pr_url=bad)
                self.assertIsNone(h.build_promoted_recheck_target(rec))

    def test_id_without_task_portion_fails_the_stamp_closed(self):
        rec = dict(FORLARRY_DECISION_RECORD, id='no-colon-id')
        self.assertIsNone(h.build_promoted_recheck_target(rec))


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


# ===================================================================
# THIRD SOURCE — beacon-pending-approvals local store
# (reconcile-local-pending-approvals-to-decide-tab-001):
# a directly-registered pending entry with NO approval_request chain_event
# (the suite-guardian-graduation-stage-1 class: add_pending + Telegram DM, no
# chain_event, chat_id=0) is missing from the decide tab. The healer mints the
# MISSING chain_event under the entry's OWN id so the tab count matches Beacon.
# ===================================================================

# The real 2026-07-30 directly-registered entry: on disk with a full
# dispatch_payload but no chain_event and chat_id=0.
SUITE_GUARDIAN_PENDING_ENTRY = {
    'id': 'suite-guardian-graduation-stage-1',
    'decision_key': 'suite-guardian-graduation-stage-1',
    'created_at': _ts(6),
    'chat_id': 0,
    'status': 'pending',
    'plan_summary': (
        'Main-Suite Green Guardian has earned graduation to Stage 1.\n\n'
        'Approve to open a config-only PR flipping config/suite-guardian.json '
        'to stage 1.'
    ),
    'target_agent': 'forge',
    'dispatch_payload': {
        'task_id': 'suite-guardian-graduation-stage-1',
        'summary': 'Main-Suite Green Guardian has earned graduation to Stage 1.',
        'target_agent': 'forge',
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'doc-only',
        'pr_title': 'chore(suite-guardian): graduate to autonomy stage 1',
        'prompt': 'Open a config-only PR that raises the stage.',
    },
}


class IsBeaconPendingDecisionTest(unittest.TestCase):
    def test_directly_registered_entry_is_a_decision(self):
        self.assertTrue(
            h.is_beacon_pending_decision(SUITE_GUARDIAN_PENDING_ENTRY))

    def test_plan_summary_only_still_qualifies(self):
        entry = {'id': 'x', 'status': 'pending', 'plan_summary': 'decide this'}
        self.assertTrue(h.is_beacon_pending_decision(entry))

    def test_resolved_status_never_carded(self):
        entry = dict(SUITE_GUARDIAN_PENDING_ENTRY, status='approved')
        self.assertFalse(h.is_beacon_pending_decision(entry))

    def test_contentless_entry_skipped(self):
        entry = {'id': 'x', 'status': 'pending'}
        self.assertFalse(h.is_beacon_pending_decision(entry))

    def test_missing_id_skipped(self):
        entry = {'status': 'pending', 'plan_summary': 'y'}
        self.assertFalse(h.is_beacon_pending_decision(entry))

    def test_non_dict_skipped(self):
        self.assertFalse(h.is_beacon_pending_decision('not-a-dict'))


class BuildBeaconPendingPayloadTest(unittest.TestCase):
    def test_task_id_is_the_entry_own_id(self):
        p = h.build_beacon_pending_card_payload(SUITE_GUARDIAN_PENDING_ENTRY)
        # Mint under the entry's OWN id (NOT a derived unreg-approval-<hash>) so
        # the card shares identity with the real pending entry.
        self.assertEqual(p['task_id'], 'suite-guardian-graduation-stage-1')
        self.assertFalse(p['task_id'].startswith(h.PROMOTED_TASK_PREFIX + '-'))

    def test_prompt_carries_plan_summary(self):
        p = h.build_beacon_pending_card_payload(SUITE_GUARDIAN_PENDING_ENTRY)
        self.assertIn('earned graduation to Stage 1', p['prompt'])

    def test_target_agent_from_dispatch_payload(self):
        p = h.build_beacon_pending_card_payload(SUITE_GUARDIAN_PENDING_ENTRY)
        self.assertEqual(p['target_agent'], 'forge')

    def test_ledger_key_is_namespaced(self):
        p = h.build_beacon_pending_card_payload(SUITE_GUARDIAN_PENDING_ENTRY)
        self.assertEqual(
            p['_ledger_key'],
            h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1')


class EvaluateBeaconPendingTest(unittest.TestCase):
    def test_uncarded_entry_is_minted(self):
        out = h.evaluate_beacon_pending(
            [SUITE_GUARDIAN_PENDING_ENTRY], open_card_task_ids=set(),
            promoted={})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['task_id'], 'suite-guardian-graduation-stage-1')

    def test_entry_with_open_card_not_duplicated(self):
        # A normally-registered approval DID emit its chain_event -> its id is in
        # the open-card set -> never double-carded.
        out = h.evaluate_beacon_pending(
            [SUITE_GUARDIAN_PENDING_ENTRY],
            open_card_task_ids={'suite-guardian-graduation-stage-1'},
            promoted={})
        self.assertEqual(out, [])

    def test_entry_already_in_ledger_skipped(self):
        key = h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1'
        out = h.evaluate_beacon_pending(
            [SUITE_GUARDIAN_PENDING_ENTRY], open_card_task_ids=set(),
            promoted={key: {'task_id': 'suite-guardian-graduation-stage-1'}})
        self.assertEqual(out, [])


class MintBeaconPendingCardTest(unittest.TestCase):
    def test_emits_chain_event_with_real_chat_and_summary(self):
        payload = h.build_beacon_pending_card_payload(
            SUITE_GUARDIAN_PENDING_ENTRY)
        captured = {}

        def fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(h.chain_event_emit, 'emit_event',
                               side_effect=fake_emit):
            ok = h.mint_beacon_pending_card(payload, chat_id=4242)

        self.assertTrue(ok)
        self.assertEqual(captured['event_type'], 'approval_request')
        self.assertEqual(captured['task_id'], 'suite-guardian-graduation-stage-1')
        # a REAL chat_id is threaded into the card (never 0)
        self.assertEqual(captured['payload'].get('reply_chat_id'), 4242)
        self.assertIn('earned graduation to Stage 1', captured['payload']['prompt'])

    def test_helper_keys_stripped_before_emit(self):
        payload = h.build_beacon_pending_card_payload(
            SUITE_GUARDIAN_PENDING_ENTRY)
        seen = {}

        def fake_emit(**kwargs):
            seen.update(kwargs)
            return True

        with mock.patch.object(h.chain_event_emit, 'emit_event',
                               side_effect=fake_emit):
            h.mint_beacon_pending_card(payload, chat_id=4242)
        # transient helper keys never leak into the chain_event payload
        self.assertNotIn('_ledger_key', seen['payload'])
        self.assertNotIn('_subject', seen['payload'])


class ReconcileBeaconPendingRetireTest(unittest.TestCase):
    def test_entry_gone_from_pending_is_retired(self):
        key = h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1'
        promoted = {key: {'task_id': 'suite-guardian-graduation-stage-1',
                          'source': h.PROMOTED_SOURCE_BEACON_PENDING}}
        retired, remaining = h.reconcile_beacon_pending_retire(
            promoted, pending_ids=set())
        self.assertEqual(retired, ['suite-guardian-graduation-stage-1'])
        self.assertNotIn(key, remaining)

    def test_still_pending_entry_is_kept(self):
        key = h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1'
        promoted = {key: {'task_id': 'suite-guardian-graduation-stage-1'}}
        retired, remaining = h.reconcile_beacon_pending_retire(
            promoted, pending_ids={'suite-guardian-graduation-stage-1'})
        self.assertEqual(retired, [])
        self.assertIn(key, remaining)

    def test_non_beaconpending_keys_untouched(self):
        promoted = {'ref:5': _ts(0), 'forlarry:x': {'task_id': 'y'}}
        retired, remaining = h.reconcile_beacon_pending_retire(
            promoted, pending_ids=set())
        self.assertEqual(retired, [])
        self.assertEqual(remaining, promoted)


class BeaconPendingMainIntegrationTest(unittest.TestCase):
    """The third source, end-to-end through main(): the five spec cases."""

    def _drive_main(self, *, pending_entries, open_ids, promoted=None,
                    chat_id=4242, emit=True):
        import contextlib
        add_pending = mock.MagicMock(
            side_effect=lambda p, **kw: {'id': p['task_id']})
        save_promoted = mock.MagicMock()
        clear_read_at = mock.MagicMock(return_value=0)
        emit_event = mock.MagicMock(
            side_effect=emit if callable(emit) else (lambda **kw: emit))
        state = {'pending': list(pending_entries), 'history': []}
        with mock.patch.object(h, 'kill_switch',
                               return_value=Path('/nonexistent/kill-switch')), \
             mock.patch.object(h, 'heartbeat'), \
             mock.patch.object(h, 'log'), \
             mock.patch.object(h, 'load_heuristics',
                               return_value=DEFAULT_HEURISTICS), \
             mock.patch.object(h, 'read_alerts', return_value=[]), \
             mock.patch.object(h, 'read_for_larry_records', return_value=[]), \
             mock.patch.object(h, 'load_beacon_outbox_markers',
                               return_value=[]), \
             mock.patch.object(h, 'load_promoted',
                               return_value=promoted or {}), \
             mock.patch.object(h, 'save_promoted', new=save_promoted), \
             mock.patch.object(h, 'reconcile_retire',
                               side_effect=lambda led, *a, **k: ([], led)), \
             mock.patch.object(h, 'open_approval_card_task_ids',
                               return_value=open_ids), \
             mock.patch.object(h, '_clear_retired_read_at', new=clear_read_at), \
             mock.patch.object(h, '_chat_id', return_value=chat_id), \
             mock.patch.object(h, 'doorbell_counts', return_value=(0, 0)), \
             mock.patch.object(h.chain_event_emit, 'emit_event',
                               new=emit_event), \
             mock.patch.object(approval, 'state_lock',
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(approval, 'save_state'), \
             mock.patch.object(approval, 'add_pending', new=add_pending), \
             mock.patch.object(approval, 'load_state', return_value=state):
            rc = h.main()
        return {
            'rc': rc, 'add_pending': add_pending,
            'save_promoted': save_promoted, 'emit_event': emit_event,
            'clear_read_at': clear_read_at,
        }

    # --- case 1: directly-registered entry, no card -> mint ONE ---
    def test_uncarded_entry_mints_one_card_with_real_chat(self):
        r = self._drive_main(
            pending_entries=[SUITE_GUARDIAN_PENDING_ENTRY], open_ids=set())
        self.assertEqual(r['rc'], 0)
        # exactly one chain_event minted, under the entry's own id
        self.assertEqual(r['emit_event'].call_count, 1)
        kwargs = r['emit_event'].call_args.kwargs
        self.assertEqual(kwargs['event_type'], 'approval_request')
        self.assertEqual(kwargs['task_id'], 'suite-guardian-graduation-stage-1')
        self.assertEqual(kwargs['payload'].get('reply_chat_id'), 4242)
        # NEVER a new pending entry (it already exists)
        r['add_pending'].assert_not_called()
        # recorded under the namespaced ledger key
        ledger = r['save_promoted'].call_args[0][0]
        self.assertIn(
            h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1',
            ledger)

    # --- case 2: entry already has an open card -> NO duplicate ---
    def test_entry_with_open_card_not_duplicated(self):
        r = self._drive_main(
            pending_entries=[SUITE_GUARDIAN_PENDING_ENTRY],
            open_ids={'suite-guardian-graduation-stage-1'})
        self.assertEqual(r['rc'], 0)
        r['emit_event'].assert_not_called()
        r['add_pending'].assert_not_called()

    # --- case 3: entry moved to resolved/history -> minted card retired ---
    def test_resolved_entry_retires_minted_card(self):
        key = h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1'
        # entry no longer in pending (Beacon resolved it), ledger still has it
        r = self._drive_main(
            pending_entries=[], open_ids=set(),
            promoted={key: {'task_id': 'suite-guardian-graduation-stage-1',
                            'source': h.PROMOTED_SOURCE_BEACON_PENDING}})
        self.assertEqual(r['rc'], 0)
        # the minted card's read_at is cleared (card leaves the tab)
        r['clear_read_at'].assert_called_once_with(
            ['suite-guardian-graduation-stage-1'])
        # ledger no longer carries the retired key
        ledger = r['save_promoted'].call_args[0][0]
        self.assertNotIn(key, ledger)
        r['emit_event'].assert_not_called()

    # --- case 4: re-run, still pending + already carded -> no second card ---
    def test_rerun_already_carded_no_second_mint(self):
        key = h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1'
        r = self._drive_main(
            pending_entries=[SUITE_GUARDIAN_PENDING_ENTRY], open_ids=set(),
            promoted={key: {'task_id': 'suite-guardian-graduation-stage-1',
                            'source': h.PROMOTED_SOURCE_BEACON_PENDING}})
        self.assertEqual(r['rc'], 0)
        r['emit_event'].assert_not_called()

    # --- case 5a: malformed entry -> skipped, no raise ---
    def test_malformed_entry_skipped_without_raising(self):
        r = self._drive_main(
            pending_entries=['not-a-dict', {'status': 'pending'}],
            open_ids=set())
        self.assertEqual(r['rc'], 0)
        r['emit_event'].assert_not_called()

    # --- case 5b: emit error -> skipped, no raise, entry left for next tick ---
    def test_emit_error_skipped_without_raising(self):
        def boom(**kwargs):
            raise RuntimeError('supabase down')

        r = self._drive_main(
            pending_entries=[SUITE_GUARDIAN_PENDING_ENTRY], open_ids=set(),
            emit=boom)
        self.assertEqual(r['rc'], 0)
        # nothing recorded in the ledger for an unemitted card
        ledger = r['save_promoted'].call_args[0][0] \
            if r['save_promoted'].called else {}
        self.assertNotIn(
            h.BEACONPENDING_LEDGER_PREFIX + 'suite-guardian-graduation-stage-1',
            ledger)

    # --- fail-closed: open-card set unavailable -> no mint this tick ---
    def test_open_card_fetch_unavailable_fails_closed(self):
        r = self._drive_main(
            pending_entries=[SUITE_GUARDIAN_PENDING_ENTRY], open_ids=None)
        self.assertEqual(r['rc'], 0)
        r['emit_event'].assert_not_called()

    # --- null chat -> skip mint rather than stamp chat_id=0 ---
    def test_no_chat_skips_mint(self):
        r = self._drive_main(
            pending_entries=[SUITE_GUARDIAN_PENDING_ENTRY], open_ids=set(),
            chat_id=None)
        self.assertEqual(r['rc'], 0)
        r['emit_event'].assert_not_called()


# ===================================================================
# Birth-time freshness gate (approvals-freshness-3-birth-probe-001):
# a card whose carried freshness_probe is already FALSE at mint time is
# SUPPRESSED (and logged); every other outcome promotes exactly as today.
# ===================================================================

_FP = h.freshness_probe  # the slice-1 evaluator module, imported by the healer


def _card(task_id='unreg-approval-x', probe=None, **extra):
    """A minimal promote-batch payload, optionally carrying a freshness_probe."""
    p = {
        'task_id': task_id,
        'summary': 'Direction needed: migration 0033 is not live yet.',
        'promoted_from_alert': 'subject:migration-0033',
    }
    p.update(extra)
    if probe is not None:
        p['freshness_probe'] = probe
    return p


class ExtractFreshnessProbeTest(unittest.TestCase):
    def test_top_level_probe(self):
        probe = {'kind': 'pr_state', 'task_id': 't'}
        self.assertEqual(
            h.extract_freshness_probe({'freshness_probe': probe}), probe)

    def test_nested_dispatch_payload_probe(self):
        probe = {'kind': 'json_path', 'path': '/x', 'key': 'a', 'expected': 1}
        rec = {'dispatch_payload': {'freshness_probe': probe}}
        self.assertEqual(h.extract_freshness_probe(rec), probe)

    def test_non_dict_probe_is_absent(self):
        self.assertIsNone(
            h.extract_freshness_probe({'freshness_probe': 'not-a-dict'}))

    def test_no_probe_is_none(self):
        self.assertIsNone(h.extract_freshness_probe({'summary': 'x'}))

    def test_non_dict_source_skipped(self):
        self.assertIsNone(h.extract_freshness_probe(None, 'str', 42))

    def test_first_source_in_order_wins(self):
        a = {'kind': 'sql', 'query': 'select 1'}
        b = {'kind': 'pr_state', 'task_id': 't'}
        self.assertEqual(
            h.extract_freshness_probe(
                {'freshness_probe': a}, {'freshness_probe': b}),
            a)


class ApplyBirthFreshnessGateTest(unittest.TestCase):
    """The heart of slice 3: only an explicit FALSE suppresses; everything else
    promotes, and each suppression emits ONE structured, greppable line."""

    def test_false_suppresses_and_logs(self):
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        with mock.patch.object(h, 'log') as mock_log:
            kept, suppressed = h.apply_birth_freshness_gate(
                [card], evaluator=lambda p: _FP.FALSE)
        self.assertEqual(kept, [])
        self.assertEqual(suppressed, [card])
        # Never-silently-drop: exactly one structured suppression line carrying
        # the task id, probe kind, the FALSE verdict, and the card summary.
        supp_lines = [c.args[0] for c in mock_log.call_args_list
                      if c.args and 'BIRTH_FRESHNESS_SUPPRESS' in c.args[0]]
        self.assertEqual(len(supp_lines), 1)
        line = supp_lines[0]
        self.assertIn(card['task_id'], line)
        self.assertIn('pr_state', line)
        self.assertIn('verdict=FALSE', line)
        self.assertIn('migration 0033', line)

    def test_true_promotes(self):
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        kept, suppressed = h.apply_birth_freshness_gate(
            [card], evaluator=lambda p: _FP.TRUE)
        self.assertEqual(kept, [card])
        self.assertEqual(suppressed, [])

    def test_indeterminate_promotes(self):
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        kept, suppressed = h.apply_birth_freshness_gate(
            [card], evaluator=lambda p: _FP.INDETERMINATE)
        self.assertEqual(kept, [card])
        self.assertEqual(suppressed, [])

    def test_no_probe_promotes_unchanged(self):
        card = _card(probe=None)
        # A raising evaluator proves it is NEVER called for a probe-less card.
        def _boom(_p):
            raise AssertionError('evaluator must not run without a probe')
        kept, suppressed = h.apply_birth_freshness_gate(
            [card], evaluator=_boom)
        self.assertEqual(kept, [card])
        self.assertEqual(suppressed, [])

    def test_evaluator_raises_promotes(self):
        card = _card(probe={'kind': 'sql', 'query': 'select 1'})
        def _raise(_p):
            raise RuntimeError('db exploded')
        with mock.patch.object(h, 'log') as mock_log:
            kept, suppressed = h.apply_birth_freshness_gate(
                [card], evaluator=_raise)
        self.assertEqual(kept, [card])   # fail toward the human -> promote
        self.assertEqual(suppressed, [])
        self.assertTrue(any(
            c.args and 'BIRTH_FRESHNESS_ERROR' in c.args[0]
            for c in mock_log.call_args_list))

    def test_unexpected_verdict_promotes_failsafe(self):
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        kept, suppressed = h.apply_birth_freshness_gate(
            [card], evaluator=lambda p: 'WEIRD')
        self.assertEqual(kept, [card])
        self.assertEqual(suppressed, [])

    def test_mixed_batch_partitions(self):
        false_card = _card(task_id='unreg-approval-false',
                           probe={'kind': 'pr_state', 'task_id': 'a'})
        keep_card = _card(task_id='unreg-approval-keep',
                          probe={'kind': 'pr_state', 'task_id': 'b'})
        no_probe = _card(task_id='unreg-approval-none')
        verdicts = {'a': _FP.FALSE, 'b': _FP.TRUE}
        kept, suppressed = h.apply_birth_freshness_gate(
            [false_card, keep_card, no_probe],
            evaluator=lambda p: verdicts[p['task_id']])
        self.assertEqual(suppressed, [false_card])
        self.assertEqual(kept, [keep_card, no_probe])

    def test_default_evaluator_is_freshness_probe_evaluate(self):
        # No injected evaluator: an unknown/malformed probe collapses to
        # INDETERMINATE inside the real evaluate() -> the card promotes.
        card = _card(probe={'kind': 'nonexistent-kind'})
        kept, suppressed = h.apply_birth_freshness_gate([card])
        self.assertEqual(kept, [card])
        self.assertEqual(suppressed, [])


class BuilderCarriesFreshnessProbeTest(unittest.TestCase):
    """Each promote-payload builder propagates a source freshness_probe onto the
    payload so it rides into dispatch_payload and the birth gate can honor it."""

    def test_alert_payload_carries_probe(self):
        probe = {'kind': 'file_lacks', 'repo': '/r', 'path': 'p', 'substring': 's'}
        rec = {'subject': 'sub', 'message': 'm',
               'suggested_action': 'Choose ship or hold', 'freshness_probe': probe}
        payload = h.build_approval_payload(rec, 'sub')
        self.assertEqual(payload.get('freshness_probe'), probe)

    def test_alert_payload_without_probe_unchanged(self):
        rec = {'subject': 'sub', 'message': 'm',
               'suggested_action': 'Choose ship or hold'}
        payload = h.build_approval_payload(rec, 'sub')
        self.assertNotIn('freshness_probe', payload)

    def test_marker_payload_carries_probe_from_marker(self):
        probe = {'kind': 'pr_state', 'task_id': 't'}
        marker = {'task_id': 't', 'summary': 's', 'target_agent': 'beacon',
                  'freshness_probe': probe}
        payload = h.build_approval_payload_from_marker(
            marker, {'subject': 'sub'}, 'sub')
        self.assertEqual(payload.get('freshness_probe'), probe)

    def test_for_larry_payload_carries_probe(self):
        probe = {'kind': 'json_path', 'path': '/c', 'key': 'k', 'expected': 1}
        rec = dict(FORLARRY_DECISION_RECORD, freshness_probe=probe)
        key = h.forlarry_dedup_key(rec['id'])
        payload = h.build_for_larry_approval_payload(rec, key)
        self.assertEqual(payload.get('freshness_probe'), probe)


class BirthGateMainIntegrationTest(unittest.TestCase):
    """End-to-end through main(): a stranded for-Larry card whose probe is FALSE
    is NOT promoted; one whose probe is TRUE promotes exactly as today."""

    def _drive(self, *, records, verdict):
        import contextlib
        add_pending = mock.MagicMock(
            side_effect=lambda p, **kw: {'id': p['task_id']})
        save_promoted = mock.MagicMock()
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
             mock.patch.object(h, 'load_promoted', return_value={}), \
             mock.patch.object(h, 'save_promoted', new=save_promoted), \
             mock.patch.object(h, 'reconcile_retire',
                               side_effect=lambda led, *a, **k: ([], led)), \
             mock.patch.object(h, '_chat_id', return_value=4242), \
             mock.patch.object(h, 'doorbell_counts', return_value=(0, 0)), \
             mock.patch.object(h, '_emit_self_failure'), \
             mock.patch('for_larry_escalations.clear', return_value=True), \
             mock.patch.object(h, 'emit_approval_event', return_value=True), \
             mock.patch.object(h.freshness_probe, 'evaluate',
                               return_value=verdict), \
             mock.patch.object(approval, 'state_lock',
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(approval, 'save_state'), \
             mock.patch.object(approval, 'add_pending', new=add_pending), \
             mock.patch.object(approval, 'load_state',
                               side_effect=[{'pending': [], 'history': []},
                                            {'pending': [], 'history': []}]):
            rc = h.main()
        return {'rc': rc, 'add_pending': add_pending,
                'save_promoted': save_promoted}

    def test_false_probe_card_not_promoted(self):
        rec = dict(FORLARRY_DECISION_RECORD,
                   freshness_probe={'kind': 'pr_state', 'task_id': 't'})
        r = self._drive(records=[rec], verdict=_FP.FALSE)
        self.assertEqual(r['rc'], 0)
        r['add_pending'].assert_not_called()   # suppressed at birth

    def test_true_probe_card_promoted(self):
        rec = dict(FORLARRY_DECISION_RECORD,
                   freshness_probe={'kind': 'pr_state', 'task_id': 't'})
        r = self._drive(records=[rec], verdict=_FP.TRUE)
        self.assertEqual(r['rc'], 0)
        r['add_pending'].assert_called_once()  # promotes exactly as today


# ===================================================================
# Birth-suppression durability + human surface
# (approvals-freshness-suppression-visibility-001): a withheld card must be
# recoverable from a durable record and visible on the alert path, exactly once
# per card even though it is re-suppressed on every tick.
# ===================================================================

class BirthSuppressionRecordTest(unittest.TestCase):
    """Criterion 1 + 3: one durable record carrying the FULL card payload, and
    identity-keyed dedup that holds across repeated ticks."""

    def setUp(self):
        self.store = Path(
            tempfile.mkdtemp(prefix='ol-birth-suppress-')) / 'suppressed.json'
        for target, kw in (('birth_suppressed_state_file',
                            {'return_value': self.store}),
                           ('_emit_birth_suppression_alert', {}),
                           ('log', {})):
            patcher = mock.patch.object(h, target, **kw)
            setattr(self, target.lstrip('_'), patcher.start())
            self.addCleanup(patcher.stop)

    def _suppress(self, cards, verdict=_FP.FALSE, evaluator=None):
        """One tick: gate the batch, then record + alert whatever it withheld."""
        _kept, suppressed = h.apply_birth_freshness_gate(
            cards, evaluator=evaluator or (lambda _p: verdict))
        return h.record_and_alert_birth_suppressions(suppressed)

    def _stored(self):
        return json.loads(self.store.read_text(encoding='utf-8'))['suppressed']

    def test_false_verdict_writes_one_recoverable_record(self):
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        new = self._suppress([card])
        self.assertEqual(len(new), 1)
        stored = self._stored()
        self.assertEqual(list(stored), ['subject:migration-0033'])
        rec = stored['subject:migration-0033']
        # The whole point: the card is reconstructable, not just summarized.
        self.assertEqual(rec['card'], card)
        self.assertEqual(rec['probe'], card['freshness_probe'])
        self.assertEqual(rec['verdict'], _FP.FALSE)
        self.assertEqual(rec['task_id'], card['task_id'])
        self.assertEqual(rec['identity'], 'subject:migration-0033')
        self.assertTrue(rec['suppressed_at'])

    def test_repeated_ticks_yield_one_record_and_one_alert(self):
        # The suppressed card is NOT written to the promoted ledger, so the same
        # source alert is re-scanned and re-suppressed every tick. Dedup must
        # absorb that: one card -> one record, one notification. Forever.
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        for _ in range(5):
            self._suppress([card])
        self.assertEqual(len(self._stored()), 1)
        self.assertEqual(self.emit_birth_suppression_alert.call_count, 1)

    def test_keep_paths_write_no_record_and_raise_no_alert(self):
        probed = _card(probe={'kind': 'pr_state', 'task_id': 't'})

        def _raise(_p):
            raise RuntimeError('evaluator exploded')

        cases = [
            ([probed], _FP.TRUE, None),
            ([probed], _FP.INDETERMINATE, None),
            ([probed], None, _raise),               # evaluator error
            ([_card(probe=None)], _FP.FALSE, None),  # no probe: never evaluated
        ]
        for cards, verdict, evaluator in cases:
            with self.subTest(verdict=verdict, evaluator=bool(evaluator)):
                self.assertEqual(
                    self._suppress(cards, verdict=verdict, evaluator=evaluator),
                    [])
        self.assertFalse(self.store.exists())
        self.emit_birth_suppression_alert.assert_not_called()

    def test_identity_falls_back_to_task_id(self):
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        card.pop('promoted_from_alert')
        self._suppress([card])
        self.assertEqual(list(self._stored()), [card['task_id']])

    def test_id_less_cards_do_not_collapse_into_one_bucket(self):
        # An empty dedup key would silently drop every record but the first.
        a = {'summary': 'a', 'freshness_probe': {'kind': 'pr_state'}}
        b = {'summary': 'b', 'freshness_probe': {'kind': 'pr_state'}}
        self._suppress([a, b])
        stored = self._stored()
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(k.startswith('cardhash:') for k in stored))

    def test_malformed_store_reads_as_empty(self):
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text('{not json', encoding='utf-8')
        self.assertEqual(h.load_birth_suppressed(self.store), {})
        self._suppress([_card(probe={'kind': 'pr_state', 'task_id': 't'})])
        self.assertEqual(len(self._stored()), 1)

    def test_store_is_bounded_evicting_oldest_first(self):
        records = {
            f'id-{i}': {'suppressed_at': f'2026-08-0{i}T00:00:00+00:00'}
            for i in range(1, 4)
        }
        with mock.patch.object(h, 'MAX_BIRTH_SUPPRESSION_RECORDS', 2):
            h.save_birth_suppressed(records, self.store)
        self.assertEqual(sorted(self._stored()), ['id-2', 'id-3'])


class BirthSuppressionAlertTest(unittest.TestCase):
    """Criterion 2: the suppression reaches the alert path with everything needed
    to answer 'no, that one was real' — and a failing notifier never raises."""

    def setUp(self):
        self.store = Path(
            tempfile.mkdtemp(prefix='ol-birth-alert-')) / 'suppressed.json'
        for target, kw in (('birth_suppressed_state_file',
                            {'return_value': self.store}), ('log', {})):
            patcher = mock.patch.object(h, target, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_alert_carries_identity_probe_and_recovery_pointer(self):
        card = _card(probe={'kind': 'json_path', 'path': '/c', 'key': 'k',
                            'expected': 1})
        with mock.patch('larry_alerts.append_alert') as append:
            h.record_and_alert_birth_suppressions([card])
        append.assert_called_once()
        kw = append.call_args.kwargs
        self.assertEqual(kw['source'], h.HEALER_SOURCE)
        self.assertEqual(kw['route'], 'escalate')   # never demoted to digest/hold
        self.assertTrue(kw['needs_larry'])          # a withheld decision is his call
        self.assertEqual(kw['subject'],
                         'birth-suppressed:subject:migration-0033')
        for expected in (card['task_id'], 'subject:migration-0033', 'json_path',
                         'migration 0033', str(self.store)):
            self.assertIn(expected, kw['message'])

    def test_alert_failure_still_records_and_never_raises(self):
        card = _card(probe={'kind': 'pr_state', 'task_id': 't'})
        with mock.patch('larry_alerts.append_alert',
                        side_effect=RuntimeError('telegram down')):
            new = h.record_and_alert_birth_suppressions([card])
        self.assertEqual(len(new), 1)
        self.assertIn('subject:migration-0033',
                      h.load_birth_suppressed(self.store))


class BirthSuppressionFailurePostureTest(unittest.TestCase):
    """Criterion 5: recording/notifying is observability only — a raising recorder
    or notifier must still promote the kept cards and never crash the tick."""

    def _drive(self, **patches):
        import contextlib
        # One card carrying a FALSE probe (suppressed) + one with no probe
        # (promotes as today), so a broken recorder is visible against a card
        # that must still reach the tab.
        suppressed_rec = dict(FORLARRY_DECISION_RECORD,
                              freshness_probe={'kind': 'pr_state', 'task_id': 't'})
        keep_rec = dict(
            FORLARRY_DECISION_RECORD,
            id='mirror-review:keep-task',
            pr_url='https://github.com/Larry-Yatch/ourliberty-agent-core/pull/999',
            head_sha='def67890',
            dedup_identity=(
                'https://github.com/Larry-Yatch/ourliberty-agent-core/'
                'pull/999@def67890'),
        )
        add_pending = mock.MagicMock(
            side_effect=lambda p, **kw: {'id': p['task_id']})
        patchers = [
            mock.patch.object(h, 'kill_switch',
                              return_value=Path('/nonexistent/kill-switch')),
            mock.patch.object(h, 'heartbeat'),
            mock.patch.object(h, 'log'),
            mock.patch.object(h, 'load_heuristics',
                              return_value=FORLARRY_HEURISTICS),
            mock.patch.object(h, 'read_alerts', return_value=[]),
            mock.patch.object(h, 'read_for_larry_records',
                              return_value=[suppressed_rec, keep_rec]),
            mock.patch.object(h, 'load_beacon_outbox_markers', return_value=[]),
            mock.patch.object(h, 'load_promoted', return_value={}),
            mock.patch.object(h, 'save_promoted'),
            mock.patch.object(h, 'reconcile_retire',
                              side_effect=lambda led, *a, **k: ([], led)),
            mock.patch.object(h, '_chat_id', return_value=4242),
            mock.patch.object(h, 'doorbell_counts', return_value=(0, 0)),
            mock.patch.object(h, '_emit_self_failure'),
            mock.patch('for_larry_escalations.clear', return_value=True),
            mock.patch.object(h, 'emit_approval_event', return_value=True),
            mock.patch.object(h.freshness_probe, 'evaluate',
                              return_value=_FP.FALSE),
            mock.patch.object(approval, 'state_lock',
                              return_value=contextlib.nullcontext()),
            mock.patch.object(approval, 'save_state'),
            mock.patch.object(approval, 'add_pending', new=add_pending),
            mock.patch.object(approval, 'load_state',
                              side_effect=lambda *a, **k: {'pending': [],
                                                           'history': []}),
        ]
        patchers += [mock.patch.object(h, target, **kw)
                     for target, kw in patches.items()]
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            rc = h.main()
        return rc, add_pending

    def test_raising_recorder_still_promotes(self):
        rc, add_pending = self._drive(
            record_birth_suppressions={'side_effect': RuntimeError('disk full')})
        self.assertEqual(rc, 0)
        add_pending.assert_called_once()

    def test_raising_notifier_still_promotes(self):
        rc, add_pending = self._drive(
            birth_suppressed_state_file={
                'return_value': Path(
                    tempfile.mkdtemp(prefix='ol-birth-posture-')) / 's.json'},
            _emit_birth_suppression_alert={
                'side_effect': RuntimeError('notify exploded')})
        self.assertEqual(rc, 0)
        add_pending.assert_called_once()


# ---- repo-qualified ref resolution (the cross-repo collision fix) ----

# The real 2026-08-03 alert. RSDPM #172 was OPEN and genuinely unrouted, but
# agent-core #172 is a docs PR merged 2026-05-28 — and the healer skipped the
# ask every tick because it resolved the bare number against agent-core.
RSDPM_172_ALERT = {
    'ts': _ts(1),
    'source': 'heal-pipeline-stall',
    'severity': 'warning',
    'route': 'escalate',
    'needs_larry': True,
    'subject': 'pipeline-stall:unrouted-pr:PR#172',
    'message': (
        'PR #172 (Larry-Yatch/RSDPM) on branch `fix/coverage-floor-ci` opened '
        '73 min ago has NO review-request dispatch logged in '
        'routing-events.jsonl.'
    ),
    'suggested_action': (
        'Dispatch a Mirror review via Beacon chat: `dispatch mirror review '
        'pr=https://github.com/Larry-Yatch/RSDPM/pull/172`.'
    ),
}


class AlertRefRepoTest(unittest.TestCase):
    """`alert_ref_repo` reads the repo the alert itself names."""

    def test_prefers_the_pr_url(self):
        self.assertEqual(h.alert_ref_repo(RSDPM_172_ALERT), 'Larry-Yatch/RSDPM')

    def test_a_parenthesised_slug_is_NOT_trusted(self):
        """A repo-shaped slug in prose is REFUSED, by measurement: across 663
        live alerts that pattern also matched parenthesised BRANCH names
        (`spec/m14-workspace-boundary`, `fix/deep-review-status-post-alert`) and
        other slash-y text (`dependents/seams`). Probing an invented repo is
        fail-safe (gh errors -> None -> keep), but it silently disables a
        legitimate skip, so the slug is not read at all."""
        self.assertIsNone(h.alert_ref_repo(
            {'message': 'PR #9 (Larry-Yatch/RSDPM) on branch `x` opened'}))
        self.assertIsNone(h.alert_ref_repo(
            {'message': 'stalled (spec/m14-workspace-boundary) needs a call'}))

    def test_a_url_wins_even_when_a_decoy_slug_is_present(self):
        rec = {'message': 'PR #1 (Larry-Yatch/decoy) on branch `x`',
               'suggested_action': 'https://github.com/Larry-Yatch/RSDPM/pull/1'}
        self.assertEqual(h.alert_ref_repo(rec), 'Larry-Yatch/RSDPM')

    def test_none_when_the_alert_carries_no_pr_url(self):
        """agent-core alerts carry no URL — they MUST stay on the historic
        default so this change is a no-op for them."""
        self.assertIsNone(h.alert_ref_repo(PR294_ALERT))
        self.assertIsNone(h.alert_ref_repo(
            {'message': 'PR #28 (ourliberty-agent-core) on branch `foo`'}))

    def test_never_raises_on_junk(self):
        for src in (None, 42, [], {'message': None}, ''):
            self.assertIsNone(h.alert_ref_repo(src))


class GhRefResolvedRepoArgTest(unittest.TestCase):
    """`gh_ref_resolved` itself must ASK the repo it was given.

    Every other test here injects a `gh_probe` stub, which sits at exactly this
    seam — so without this class the real probe could ignore its `repo` argument
    entirely and the whole suite would stay green
    ([[mutation-evidence-is-void-when-mocks-sit-at-the-seam]])."""

    def _capture(self):
        seen = []

        def fake_gh_state(kind, number, repo, timeout):
            seen.append((kind, number, repo))
            return 'MERGED'
        return fake_gh_state, seen

    def test_probes_the_repo_it_was_given(self):
        fake, seen = self._capture()
        with mock.patch.object(h, '_gh_state', fake):
            self.assertIs(h.gh_ref_resolved(172, 'Larry-Yatch/RSDPM'), True)
        self.assertEqual([r for _k, _n, r in seen], ['Larry-Yatch/RSDPM'])

    def test_falls_back_to_the_default_repo_when_none(self):
        fake, seen = self._capture()
        with mock.patch.object(h, '_gh_state', fake):
            h.gh_ref_resolved(294)
        self.assertEqual([r for _k, _n, r in seen], [h.ref_repo()])


class CrossRepoRefResolutionTest(unittest.TestCase):
    """THE REGRESSION: a bare `#<n>` is ambiguous across repos, and the decision
    identity (`ref:<n>`) carries none. agent-core is past #1085 while RSDPM is
    around #172, so every RSDPM number also exists in agent-core as a merged PR
    — resolving against the default repo marked every RSDPM ask moot forever."""

    def _empty(self):
        return {'pending': [], 'history': []}

    def _probe_merged_only_in(self, repo):
        """gh stand-in: #172 is merged in `repo`, OPEN everywhere else."""
        seen = []

        def probe(n, r=None):
            seen.append((n, r))
            return True if r == repo else False
        return probe, seen

    def test_rsdpm_ask_is_not_skipped_by_the_agent_core_pr(self):
        probe, seen = self._probe_merged_only_in(
            'Larry-Yatch/ourliberty-agent-core')
        reason = h.resolution_signal(
            RSDPM_172_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=probe)
        self.assertIsNone(reason, 'a live RSDPM ask was skipped by a '
                                  'same-numbered agent-core PR')
        self.assertIn(('172', 'Larry-Yatch/RSDPM'),
                      [(str(n), r) for n, r in seen])

    def test_rsdpm_ask_IS_skipped_when_the_rsdpm_pr_really_merged(self):
        """The fix must not make the gate blind — the correct repo still skips."""
        probe, _ = self._probe_merged_only_in('Larry-Yatch/RSDPM')
        reason = h.resolution_signal(
            RSDPM_172_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=probe)
        self.assertIsNotNone(reason)
        self.assertIn('Larry-Yatch/RSDPM#172', reason)

    def test_repoless_alert_still_uses_the_default_repo(self):
        """Zero behavior change for the agent-core alerts that name no repo."""
        seen = []
        h.resolution_signal(
            PR294_ALERT, self._empty(), [], DEFAULT_HEURISTICS,
            gh_probe=lambda n, r=None: seen.append((n, r)) or None)
        self.assertEqual([r for _n, r in seen], [h.ref_repo()])

    def test_builder_stamps_the_repo_for_the_ledger(self):
        payload = h.build_approval_payload(RSDPM_172_ALERT, 'ref:172')
        self.assertEqual(payload['_ref_repo'], 'Larry-Yatch/RSDPM')
        # ...and it is transient: never persisted onto the card itself.
        h._strip_helper_keys(payload)
        self.assertNotIn('_ref_repo', payload)

    def test_retire_probes_the_repo_the_ledger_carried(self):
        """The alert has aged out of the scan window by retire time, so the repo
        has to be CARRIED — otherwise the retire falls back to the default repo
        and an unrelated same-numbered PR retires a live card."""
        probe, seen = self._probe_merged_only_in(
            'Larry-Yatch/ourliberty-agent-core')
        task_id = h.derive_task_id('ref:172')
        state = {'pending': [{'id': task_id, 'status': 'pending'}],
                 'history': []}
        promoted = {'ref:172': {
            'task_id': task_id, 'subject': RSDPM_172_ALERT['subject'],
            'promoted_at': _ts(1), 'ref_repo': 'Larry-Yatch/RSDPM'}}
        retired, remaining = h.reconcile_retire(
            promoted, state, [], DEFAULT_HEURISTICS, now=NOW, gh_probe=probe)
        self.assertEqual(retired, [], 'a live RSDPM card was retired by the '
                                      'agent-core PR of the same number')
        self.assertEqual(remaining, promoted)
        self.assertIn(('172', 'Larry-Yatch/RSDPM'),
                      [(str(n), r) for n, r in seen])

    def test_legacy_ledger_entry_without_a_repo_uses_the_default(self):
        """Entries written before the field existed carry no repo; they are all
        agent-core asks, so falling back to the default is correct for them."""
        seen = []
        task_id = h.derive_task_id('ref:294')
        promoted = {'ref:294': {
            'task_id': task_id,
            # The retire path scans the SUBJECT for refs, not the ledger key.
            'subject': 'PR #294 Mirror review gap — source=larry routing miss',
            'promoted_at': _ts(1)}}
        h.reconcile_retire(
            promoted, {'pending': [], 'history': []}, [], DEFAULT_HEURISTICS,
            now=NOW, gh_probe=lambda n, r=None: seen.append((n, r)) or None)
        self.assertEqual([r for _n, r in seen], [h.ref_repo()])


if __name__ == '__main__':
    unittest.main()
