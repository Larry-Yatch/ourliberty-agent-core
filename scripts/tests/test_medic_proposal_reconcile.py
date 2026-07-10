#!/usr/bin/env python3
"""Tests for the medic-proposal reconciler (slice 9).

Focus (the reconcile() pure step, all I/O injected):
  - PROPOSE a recurring not-graduated fingerprint (emit called, state recorded).
  - DEDUP: a fingerprint already tracked is not re-proposed.
  - FILTER: non-'not-graduated' reasons, non-skipped outcomes, and stale-only
    (not currently recurring) fingerprints are NOT proposed.
  - SKIP proposing a fingerprint Medic has already acted on (graduated).
  - SELF-RETRACT: a tracked card whose condition cleared (no recent qualifying
    skip) or whom Medic has since acted on is retracted and dropped from state.
  - transport error (retract returns None) KEEPS the card tracked (retry).
  - a terminal server reason ('not-found'/'not-parked') drops it from state.
  - note parsing round-trips the (action, target) into a work-shaped title.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_medic_proposal_reconcile
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import medic_proposal_reconcile as mpr  # noqa: E402

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def rec(fp, action='restart-daemon', target='foo.service', reason='not-graduated',
        outcome='skipped', age_min=1, subject='foo'):
    ts = (NOW - timedelta(minutes=age_min)).isoformat()
    return {
        'ts': ts, 'fingerprint': fp, 'subject': subject, 'outcome': outcome,
        'notes': f'{action} refused for {target}: {reason}',
    }


_UNSET = object()


class _Spy:
    """Injectable emit/retract that records calls and returns scripted values."""

    def __init__(self, emit_id='cap-new', retract_resp=_UNSET):
        self.emitted = []
        self.retracted = []
        self._emit_id = emit_id
        # sentinel so an EXPLICIT None (transport error) can be injected without
        # being coerced back to the success default.
        self._retract_resp = {'retracted': True} if retract_resp is _UNSET else retract_resp

    def emit(self, *, title, note):
        self.emitted.append({'title': title, 'note': note})
        return self._emit_id

    def retract(self, cap_id, reason=None):
        self.retracted.append({'capture_id': cap_id, 'reason': reason})
        return self._retract_resp


def run(records, state, spy, acted=frozenset()):
    return mpr.reconcile(
        now=NOW, records=records, state=dict(state),
        has_acted=lambda fp: fp in acted,
        emit=spy.emit, retract=spy.retract,
    )


class NoteParsingTest(unittest.TestCase):
    def test_parses_action_target_reason(self):
        p = mpr._parse_note('restart-daemon refused for beacon-bot.service: not-graduated')
        self.assertEqual(p, {'action': 'restart-daemon',
                             'target': 'beacon-bot.service',
                             'reason': 'not-graduated'})

    def test_rejects_non_refusal_note(self):
        self.assertIsNone(mpr._parse_note('some unrelated note'))
        self.assertIsNone(mpr._parse_note(None))

    def test_latest_wins_per_fingerprint(self):
        recs = [rec('watchdog:foo', age_min=90), rec('watchdog:foo', age_min=5, target='bar.service')]
        latest = mpr.latest_qualifying(recs, NOW)
        self.assertEqual(latest['watchdog:foo']['target'], 'bar.service')


class ProposeTest(unittest.TestCase):
    def test_proposes_recurring_not_graduated(self):
        spy = _Spy(emit_id='cap-1')
        out = run([rec('watchdog:foo')], {}, spy)
        self.assertEqual(out['proposed'], ['watchdog:foo'])
        self.assertEqual(out['state']['watchdog:foo']['capture_id'], 'cap-1')
        self.assertEqual(len(spy.emitted), 1)
        self.assertIn('restart-daemon', spy.emitted[0]['title'])
        self.assertIn('foo.service', spy.emitted[0]['title'])

    def test_dedup_already_tracked(self):
        spy = _Spy()
        state = {'watchdog:foo': {'capture_id': 'cap-old', 'parked_at': NOW.isoformat()}}
        out = run([rec('watchdog:foo')], state, spy)
        self.assertEqual(out['proposed'], [])
        self.assertEqual(spy.emitted, [])

    def test_ignores_non_not_graduated_reason(self):
        spy = _Spy()
        out = run([rec('watchdog:foo', reason='not-permitted'),
                   rec('watchdog:bar', reason='flapping-defer-to-systemd')], {}, spy)
        self.assertEqual(out['proposed'], [])

    def test_ignores_non_skipped_outcome(self):
        spy = _Spy()
        out = run([rec('watchdog:foo', outcome='acted')], {}, spy)
        self.assertEqual(out['proposed'], [])

    def test_does_not_propose_stale_only_fingerprint(self):
        # seen 3h ago (> RECUR_WINDOW_H=2) and never since -> not currently recurring
        spy = _Spy()
        out = run([rec('watchdog:foo', age_min=180)], {}, spy)
        self.assertEqual(out['proposed'], [])

    def test_does_not_propose_already_acted(self):
        spy = _Spy()
        out = run([rec('watchdog:foo')], {}, spy, acted={'watchdog:foo'})
        self.assertEqual(out['proposed'], [])

    def test_emit_failure_leaves_unproposed(self):
        spy = _Spy(emit_id=None)
        out = run([rec('watchdog:foo')], {}, spy)
        self.assertEqual(out['proposed'], [])
        self.assertNotIn('watchdog:foo', out['state'])


class SelfRetractTest(unittest.TestCase):
    def _tracked(self, fp='watchdog:foo', cid='cap-1'):
        return {fp: {'capture_id': cid, 'parked_at': NOW.isoformat()}}

    def test_retracts_when_condition_cleared(self):
        # tracked but NO qualifying record this run -> stale -> retract
        spy = _Spy(retract_resp={'retracted': True})
        out = run([], self._tracked(), spy)
        self.assertEqual([r[0] for r in out['retracted']], ['watchdog:foo'])
        self.assertNotIn('watchdog:foo', out['state'])
        self.assertEqual(spy.retracted[0]['reason'], 'condition-cleared')

    def test_retracts_when_medic_acted(self):
        # still recurring, but Medic has since acted (graduated) -> retract
        spy = _Spy(retract_resp={'retracted': True})
        out = run([rec('watchdog:foo')], self._tracked(), spy, acted={'watchdog:foo'})
        self.assertEqual([r[1] for r in out['retracted']], ['medic-acted'])
        self.assertNotIn('watchdog:foo', out['state'])

    def test_keeps_while_still_recurring(self):
        spy = _Spy()
        out = run([rec('watchdog:foo', age_min=10)], self._tracked(), spy)
        self.assertEqual(out['retracted'], [])
        self.assertEqual(out['kept'], ['watchdog:foo'])
        self.assertEqual(spy.retracted, [])  # no retract attempted

    def test_transport_error_keeps_tracked(self):
        spy = _Spy(retract_resp=None)  # None = transport error / unknown
        out = run([], self._tracked(), spy)
        self.assertEqual(out['retracted'], [])
        self.assertIn('watchdog:foo', out['state'])  # kept to retry next run

    def test_terminal_server_reason_drops_from_state(self):
        # card already gone/handled server-side -> stop tracking
        spy = _Spy(retract_resp={'retracted': False, 'reason': 'not-found'})
        out = run([], self._tracked(), spy)
        self.assertNotIn('watchdog:foo', out['state'])
        self.assertEqual([r[0] for r in out['retracted']], ['watchdog:foo'])

    def test_stale_boundary_uses_stale_window(self):
        # 4h old: past RECUR (2h, so not re-proposed) but within STALE (6h) -> KEEP
        spy = _Spy()
        out = run([rec('watchdog:foo', age_min=240)], self._tracked(), spy)
        self.assertEqual(out['kept'], ['watchdog:foo'])
        self.assertEqual(spy.retracted, [])


class IncrementalPersistTest(unittest.TestCase):
    def test_persist_called_after_each_emit_and_retract(self):
        # one fp to propose, one already-tracked fp to retract (stale) -> persist
        # fires per mutation so a crash can't re-propose an already-parked card.
        snapshots = []
        spy = _Spy(emit_id='cap-new')

        def persist(st):
            snapshots.append(dict(st))

        state = {'old:gone': {'capture_id': 'cap-old', 'parked_at': NOW.isoformat()}}
        mpr.reconcile(
            now=NOW, records=[rec('watchdog:foo')], state=state,
            has_acted=lambda fp: False, emit=spy.emit, retract=spy.retract,
            persist=persist,
        )
        # at least two persists (one after the propose, one after the retract),
        # and the FIRST snapshot already carries the freshly-proposed card.
        self.assertGreaterEqual(len(snapshots), 2)
        self.assertIn('watchdog:foo', snapshots[0])

    def test_no_persist_arg_is_a_noop(self):
        spy = _Spy()
        # default persist=None must not raise
        out = mpr.reconcile(
            now=NOW, records=[rec('watchdog:foo')], state={},
            has_acted=lambda fp: False, emit=spy.emit, retract=spy.retract,
        )
        self.assertEqual(out['proposed'], ['watchdog:foo'])


if __name__ == '__main__':
    unittest.main()
