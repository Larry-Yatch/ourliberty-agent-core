#!/usr/bin/env python3
"""Tests for heal_approvals_surface_drift (the decide-tab parity sentinel).

Maps to the spec's seven acceptance criteria:
  1. parity holds                              -> NO alert
  2. an item in A missing from B past grace    -> exactly ONE alert, named + directed
  3. a card in B with nothing in A past grace  -> one alert (stale-card direction)
  4. the divergence reconciles                 -> ledger entry cleared, no repeat
  5. a single-tick transient divergence        -> NO alert (grace holds)
  6. an item marked premise_stale              -> not flagged in either direction
  7. the sentinel performs ZERO writes to approval stores / chain_events

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_approvals_surface_drift
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

# Import-time sandbox (canonical Gap-A shape, see scripts/tests/conftest.py): the
# sentinel's transitive imports freeze AGENTS_ROOT-derived paths at import, so the
# env must be set BEFORE the import below.
if not os.environ.get('OURLIBERTY_AGENTS_ROOT'):
    _SANDBOX_ROOT = tempfile.mkdtemp(prefix='ol-test-agents-root-')
    os.makedirs(os.path.join(_SANDBOX_ROOT, 'logs'), exist_ok=True)
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _SANDBOX_ROOT
    os.environ.setdefault(
        'OURLIBERTY_WORKTREES_ROOT', os.path.join(_SANDBOX_ROOT, 'worktrees'))
    os.environ.setdefault(
        'OURLIBERTY_LOG_DIR', os.path.join(_SANDBOX_ROOT, 'logs'))

import heal_approvals_surface_drift as d  # noqa: E402
import heal_unregistered_approval as heal  # noqa: E402

# Runtime backstop (same shape as the sibling healer suite): the module resolves
# agents_root() at CALL time, so an alphabetically-earlier suite's tearDown can
# pop the env before these tests RUN. Patch the write funnels module-wide.
_MODULE_WRITE_PATCHES = [
    mock.patch.object(d, 'log'),
    mock.patch.object(d, 'heartbeat'),
]


def setUpModule():
    for _p in _MODULE_WRITE_PATCHES:
        _p.start()


def tearDownModule():
    for _p in _MODULE_WRITE_PATCHES:
        _p.stop()


NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
HEURISTICS = {'scan_window_hours': 24}


def _pending(entry_id, **extra):
    entry = {
        'id': entry_id,
        'status': 'pending',
        'plan_summary': f'decide {entry_id}',
        'dispatch_payload': {'prompt': f'do {entry_id}', 'target_agent': 'forge'},
    }
    entry.update(extra)
    return entry


def _alert(subject, **extra):
    record = {
        'ts': (NOW - timedelta(minutes=5)).isoformat(),
        'source': 'pipeline-stall',
        'severity': 'warning',
        'subject': subject,
        'message': f'{subject} needs a decision',
        'route': 'escalate',
        'needs_larry': True,
    }
    record.update(extra)
    return record


def _tick(state=None, for_larry=None, alerts=None, cards=None, tracked=None,
          grace=3, now=NOW):
    """One run_tick with everything injected. No I/O."""
    return d.run_tick(
        state or {'pending': [], 'history': []},
        for_larry or [],
        alerts or [],
        HEURISTICS,
        set(cards or []),
        tracked if tracked is not None else {},
        now=now,
        grace=grace,
    )


class ParityHoldsTest(unittest.TestCase):
    """Criterion 1 — when every awaiting item has a card and every card has an
    awaiting item, nothing is reported and nothing is tracked."""

    def test_pending_entry_with_its_card_is_not_drift(self):
        tracked, divergences, to_alert = _tick(
            state={'pending': [_pending('suite-guardian-graduation-stage-1')]},
            cards=['suite-guardian-graduation-stage-1'],
        )
        self.assertEqual(divergences, [])
        self.assertEqual(to_alert, [])
        self.assertEqual(tracked, {})

    def test_promoted_alert_matches_its_derived_card_id(self):
        """The apples-to-apples case: the card the promoter mints is titled
        `unreg-approval-<hash>`, not the alert's subject. The promoter-card alias
        is what keeps that from reading as permanent drift."""
        record = _alert('pipeline-stall:unrouted-pr:PR#1084')
        card_id = heal.derive_task_id(heal.decision_identity(record))
        _tracked, divergences, to_alert = _tick(
            alerts=[record], cards=[card_id])
        self.assertEqual(divergences, [])
        self.assertEqual(to_alert, [])

    def test_empty_everywhere_is_parity(self):
        tracked, divergences, to_alert = _tick()
        self.assertEqual((divergences, to_alert, tracked), ([], [], {}))


class MissingCardTest(unittest.TestCase):
    """Criterion 2 — an item awaiting Larry with no card alerts exactly once,
    naming the item and the direction."""

    def test_actionable_alert_without_a_card_alerts_after_grace(self):
        record = _alert('pipeline-stall:unrouted-pr:PR#1084')
        tracked = {}
        alerted_rounds = []
        for round_no in range(1, 5):
            tracked, divergences, to_alert = _tick(
                alerts=[record], tracked=tracked, grace=3)
            self.assertEqual(len(divergences), 1)
            self.assertEqual(divergences[0]['direction'], d.MISSING_CARD)
            if to_alert:
                alerted_rounds.append(round_no)
        # Exactly one alert, on the tick grace is reached — never again.
        self.assertEqual(alerted_rounds, [3])

    def test_alert_message_names_item_direction_and_remedy(self):
        record = _alert('pipeline-stall:unrouted-pr:PR#1084')
        _tracked, divergences, _to_alert = _tick(alerts=[record], grace=1)
        message = d.alert_message(divergences[0], 3)
        self.assertIn('pipeline-stall:unrouted-pr:PR#1084', message)
        self.assertIn('NOT on the decide tab', message)
        self.assertIn('heal_unregistered_approval', message)  # the remedy

    def test_directly_registered_pending_entry_without_a_card(self):
        """The suite-guardian class: registered straight into the local store,
        never carded."""
        _tracked, divergences, _to_alert = _tick(
            state={'pending': [_pending('suite-guardian-graduation-stage-1')]},
            cards=['some-other-card'], grace=1)
        missing = [x for x in divergences if x['direction'] == d.MISSING_CARD]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['label'],
                         'suite-guardian-graduation-stage-1')
        self.assertEqual(missing[0]['origin'], 'pending')

    def test_open_for_larry_record_without_a_card(self):
        _tracked, divergences, _to_alert = _tick(
            for_larry=[{'id': 'mirror-review:pr-1084', 'source': 'mirror-review',
                        'resolved': False}], grace=1)
        self.assertEqual(len(divergences), 1)
        self.assertEqual(divergences[0]['origin'], 'for-larry')

    def test_resolved_for_larry_record_is_not_awaiting(self):
        _tracked, divergences, _to_alert = _tick(
            for_larry=[{'id': 'mirror-review:pr-1084', 'source': 'mirror-review',
                        'resolved': True}], grace=1)
        self.assertEqual(divergences, [])

    def test_non_actionable_alert_is_not_awaiting(self):
        """route!=escalate or no needs_larry stamp -> not unambiguously awaiting
        Larry, so it is never counted as drift."""
        self.assertEqual(_tick(alerts=[_alert('x', needs_larry=False)])[1], [])
        self.assertEqual(_tick(alerts=[_alert('x', route='digest')])[1], [])

    def test_out_of_window_alert_is_not_awaiting(self):
        old = _alert('stale-ask', ts=(NOW - timedelta(days=9)).isoformat())
        self.assertEqual(_tick(alerts=[old])[1], [])

    def test_resolved_alert_is_excluded_from_awaiting(self):
        record = _alert('pipeline-stall:unrouted-pr:PR#1084')
        _tracked, divergences, _to_alert = d.run_tick(
            {'pending': []}, [], [record], HEURISTICS, set(), {},
            now=NOW, grace=1,
            resolution_check=lambda _r: 'SKIP_MERGED_PR referenced #1084',
        )
        self.assertEqual(divergences, [])

    def test_sentinels_own_alerts_never_count_as_awaiting(self):
        """Self-reference guard: the sentinel's alerts are route=escalate +
        needs_larry, so without this exclusion it would flag itself forever."""
        own = _alert('heal-approvals-surface-drift:missing_card:x',
                     source=d.HEALER_SOURCE)
        self.assertEqual(_tick(alerts=[own])[1], [])


class OrphanCardTest(unittest.TestCase):
    """Criterion 3 — an open card with nothing awaiting behind it."""

    def test_card_with_nothing_awaiting_alerts_after_grace(self):
        tracked = {}
        for _ in range(3):
            tracked, divergences, to_alert = _tick(
                cards=['resolved-long-ago'], tracked=tracked, grace=3)
        self.assertEqual(len(divergences), 1)
        self.assertEqual(divergences[0]['direction'], d.ORPHAN_CARD)
        self.assertEqual(divergences[0]['label'], 'resolved-long-ago')
        self.assertEqual(len(to_alert), 1)
        self.assertIn('nothing is awaiting you',
                      d.alert_message(to_alert[0], 3))

    def test_mock_card_ids_are_never_orphans(self):
        self.assertEqual(_tick(cards=['real-deploy-001'], grace=1)[1], [])

    def test_both_directions_reported_together(self):
        _tracked, divergences, _to_alert = _tick(
            state={'pending': [_pending('needs-a-card')]},
            cards=['card-with-no-item'], grace=1)
        self.assertEqual(
            sorted(x['direction'] for x in divergences),
            [d.MISSING_CARD, d.ORPHAN_CARD])


class ReconcileTest(unittest.TestCase):
    """Criterion 4 — when the divergence clears, the ledger entry is dropped so a
    later recurrence can alert again."""

    def test_reconciled_item_clears_state_and_can_realert(self):
        record = _alert('pipeline-stall:unrouted-pr:PR#1084')
        card_id = heal.derive_task_id(heal.decision_identity(record))
        tracked = {}
        for _ in range(3):
            tracked, _div, to_alert = _tick(
                alerts=[record], tracked=tracked, grace=3)
        self.assertEqual(len(to_alert), 1)
        self.assertEqual(len(tracked), 1)

        # The promoter catches up: the card exists -> parity -> ledger cleared.
        tracked, divergences, to_alert = _tick(
            alerts=[record], cards=[card_id], tracked=tracked, grace=3)
        self.assertEqual(divergences, [])
        self.assertEqual(tracked, {})

        # It regresses later: a FRESH grace window, and it alerts again.
        alerted = []
        for round_no in range(1, 4):
            tracked, _div, to_alert = _tick(
                alerts=[record], tracked=tracked, grace=3)
            if to_alert:
                alerted.append(round_no)
        self.assertEqual(alerted, [3])

    def test_persisting_divergence_alerts_only_once(self):
        tracked = {}
        alerts_sent = 0
        for _ in range(10):
            tracked, _div, to_alert = _tick(
                cards=['orphan'], tracked=tracked, grace=2)
            alerts_sent += len(to_alert)
        self.assertEqual(alerts_sent, 1)
        self.assertEqual(tracked[f'{d.ORPHAN_CARD}|orphan']['ticks'], 10)


class GraceTest(unittest.TestCase):
    """Criterion 5 — a promote/retire caught in flight is normal churn."""

    def test_single_tick_divergence_does_not_alert(self):
        tracked, divergences, to_alert = _tick(cards=['mid-promote'], grace=3)
        self.assertEqual(len(divergences), 1)
        self.assertEqual(to_alert, [])
        self.assertEqual(tracked[f'{d.ORPHAN_CARD}|mid-promote']['ticks'], 1)

    def test_transient_divergence_that_clears_never_alerts(self):
        record = _alert('mid-flight-ask')
        card_id = heal.derive_task_id(heal.decision_identity(record))
        tracked, _div, to_alert = _tick(alerts=[record], grace=3)
        self.assertEqual(to_alert, [])
        tracked, divergences, to_alert = _tick(
            alerts=[record], cards=[card_id], tracked=tracked, grace=3)
        self.assertEqual((divergences, to_alert, tracked), ([], [], {}))

    def test_grace_ticks_env_override_falls_back_on_garbage(self):
        with mock.patch.dict(
                os.environ,
                {'OURLIBERTY_APPROVAL_DRIFT_GRACE_TICKS': 'not-a-number'}):
            self.assertEqual(d.grace_ticks(), d.DEFAULT_GRACE_TICKS)
        with mock.patch.dict(
                os.environ, {'OURLIBERTY_APPROVAL_DRIFT_GRACE_TICKS': '0'}):
            self.assertEqual(d.grace_ticks(), d.DEFAULT_GRACE_TICKS)
        with mock.patch.dict(
                os.environ, {'OURLIBERTY_APPROVAL_DRIFT_GRACE_TICKS': '5'}):
            self.assertEqual(d.grace_ticks(), 5)


class PremiseStaleTest(unittest.TestCase):
    """Criterion 6 — a card freshness_probe is demoting BY DESIGN is not drift,
    in either direction."""

    def test_premise_stale_entry_is_not_a_missing_card(self):
        entry = _pending('moot-ask', premise_stale={'kind': 'sql',
                                                    'evidence': '0033 is live'})
        _tracked, divergences, _to_alert = _tick(
            state={'pending': [entry]}, grace=1)
        self.assertEqual(divergences, [])

    def test_premise_stale_entrys_card_is_not_an_orphan(self):
        entry = _pending('moot-ask', premise_stale={'kind': 'sql'})
        _tracked, divergences, _to_alert = _tick(
            state={'pending': [entry]}, cards=['moot-ask'], grace=1)
        self.assertEqual(divergences, [])

    def test_non_pending_entry_is_not_awaiting(self):
        _tracked, divergences, _to_alert = _tick(
            state={'pending': [_pending('done', status='approved')]}, grace=1)
        self.assertEqual(divergences, [])


class ObserveOnlyTest(unittest.TestCase):
    """Criterion 7 — the sentinel writes NOTHING to any approval store or to
    chain_events. The reconciler owns remediation; a bug in here can only ever
    produce a wrong alert, never a corrupted tab."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ol-drift-observe-')
        self.state_path = Path(self.tmp) / 'ledger.json'

    def _run_main_with(self, *, state, alerts, cards):
        """main() with every store read stubbed and every MUTATING entry point
        replaced by a tripwire that fails the test if it is ever called."""
        import beacon_approval_handler as approval
        import chain_event_emit
        import for_larry_escalations as fle
        import larry_alerts as la

        def _boom(name):
            def _raise(*_a, **_k):
                raise AssertionError(f'observe-only violated: {name} called')
            return _raise

        patches = [
            mock.patch.object(d, 'kill_switch',
                              return_value=Path(self.tmp) / 'no-kill-switch'),
            mock.patch.object(d, 'state_file', return_value=self.state_path),
            mock.patch.object(heal, 'open_approval_card_task_ids',
                              return_value=set(cards)),
            mock.patch.object(heal, 'load_heuristics', return_value=HEURISTICS),
            mock.patch.object(heal, 'read_alerts', return_value=alerts),
            mock.patch.object(heal, 'read_for_larry_records', return_value=[]),
            mock.patch.object(heal, 'resolution_signal', return_value=None),
            mock.patch.object(approval, 'load_state', return_value=state),
            # Every write path an approvals-touching healer could reach.
            mock.patch.object(approval, 'add_pending', _boom('add_pending')),
            mock.patch.object(approval, 'resolve', _boom('resolve')),
            mock.patch.object(approval, 'save_state', _boom('save_state')),
            mock.patch.object(approval, 'demote', _boom('demote')),
            mock.patch.object(chain_event_emit, 'emit_event',
                              _boom('emit_event')),
            mock.patch.object(fle, 'clear', _boom('for_larry.clear')),
            mock.patch.object(la, 'append_alert', mock.DEFAULT),
        ]
        started = [p.start() for p in patches]
        try:
            rc = d.main()
        finally:
            for p in patches:
                p.stop()
        return rc, started[-1]

    def test_no_writes_on_a_clean_parity_tick(self):
        rc, append_alert = self._run_main_with(
            state={'pending': [_pending('carded')]}, alerts=[], cards=['carded'])
        self.assertEqual(rc, 0)
        append_alert.assert_not_called()

    def test_no_writes_even_when_it_alerts(self):
        """The alerting path is the one most likely to reach for a store — it
        must still only append to the alert ledger."""
        os.environ['OURLIBERTY_APPROVAL_DRIFT_GRACE_TICKS'] = '1'
        try:
            rc, append_alert = self._run_main_with(
                state={'pending': [_pending('never-carded')]},
                alerts=[], cards=[])
        finally:
            os.environ.pop('OURLIBERTY_APPROVAL_DRIFT_GRACE_TICKS', None)
        self.assertEqual(rc, 0)
        self.assertEqual(append_alert.call_count, 1)
        kwargs = append_alert.call_args.kwargs
        self.assertEqual(kwargs['source'], d.HEALER_SOURCE)
        self.assertTrue(kwargs['needs_larry'])
        self.assertIn('never-carded', kwargs['message'])

    def test_unreadable_tab_skips_the_tick_without_touching_the_ledger(self):
        """A None from the open-card fetch is an OUTAGE, not an empty tab. It
        must not flag every awaiting item at once, and must not age the grace
        counters — otherwise an outage manufactures a false alert storm."""
        d.save_tracked({'missing_card|x': {'ticks': 1}}, self.state_path)
        before = self.state_path.read_bytes()
        with mock.patch.object(d, 'kill_switch',
                               return_value=Path(self.tmp) / 'none'), \
                mock.patch.object(d, 'state_file', return_value=self.state_path), \
                mock.patch.object(heal, 'open_approval_card_task_ids',
                                  return_value=None):
            self.assertEqual(d.main(), 0)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_kill_switch_stops_the_tick(self):
        switch = Path(self.tmp) / 'healers.disabled'
        switch.write_text('off')
        with mock.patch.object(d, 'kill_switch', return_value=switch), \
                mock.patch.object(heal, 'open_approval_card_task_ids') as fetch:
            self.assertEqual(d.main(), 0)
        fetch.assert_not_called()


class LedgerTest(unittest.TestCase):
    """The sentinel's own state file — the only thing it writes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ol-drift-ledger-')
        self.path = Path(self.tmp) / 'drift.json'

    def test_roundtrip(self):
        tracked = {'missing_card|abc': {'ticks': 2, 'alerted_at': None}}
        d.save_tracked(tracked, self.path)
        self.assertEqual(d.load_tracked(self.path), tracked)
        self.assertIn('_schema', json.loads(self.path.read_text()))

    def test_missing_or_corrupt_file_reads_as_empty(self):
        self.assertEqual(d.load_tracked(self.path), {})
        self.path.write_text('{not json')
        self.assertEqual(d.load_tracked(self.path), {})


if __name__ == '__main__':
    unittest.main()
