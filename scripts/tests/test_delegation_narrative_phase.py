#!/usr/bin/env python3
"""Delegate-thread-narrator — the shared narrative-phase resolver.

`dashboard_api.resolve_delegation_narrative_phase` collapses the read-side
`_delegation_trail_field` phase + the GC healer's persisted terminal stamps
(`spawned.outcome`, `shipped_note`/`shipped_pr_url`, `failure_signaled`) +
`has_open_approval` into ONE narrative phase over {handed_off, waiting_approval,
building, in_review, review_passed, merged, closed_failed, None} — the SAME
resolver the sweep and the dashboard both read. These tests pin the
most-terminal-wins precedence and the honest-merge rule.

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_delegation_narrative_phase
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api as da  # noqa: E402

DELEGATE_ID = 'delegate-cap-fix-the-thing-ab12'
PR = 'https://github.com/o/r/pull/7'


def _ev(event_type, *, pr_url=None, ts='2026-07-11T12:00:00Z'):
    return {'event_type': event_type, 'pr_url': pr_url, 'ts': ts,
            'payload': {'origin_task_id': DELEGATE_ID}}


def _cap(**over):
    cap = {'id': 'cap-fix-the-thing-ab12', 'state': 'parked',
           'title': 'Fix the thing',
           'spawned': {'kind': 'delegate', 'task_id': DELEGATE_ID}}
    cap.update(over)
    return cap


def _resolve(cap, m=None, **kw):
    return da.resolve_delegation_narrative_phase(cap, m, **kw)


class PhasePrecedenceTest(unittest.TestCase):
    def test_no_signal_is_none(self):
        # A non-delegate card / nothing observed → nothing to narrate.
        cap = {'id': 'c', 'state': 'parked',
               'spawned': {'kind': 'orphan', 'task_id': 't-1'}}
        got = _resolve(cap, {})
        self.assertIsNone(got['narrative_phase'])
        self.assertIsNone(got['narrative_pr_url'])

    def test_handed_off(self):
        # `handed_off` is gated on the LEDGER RECEIPT (#974) — the team's "I've
        # got this" — not on the click. This test asserted the pre-#974 contract
        # and was merged red in #975 seven seconds after #974 landed; it has
        # never passed. The receipt is what earns the calm phrasing.
        got = _resolve(_cap(), {}, dispatched_by_origin={DELEGATE_ID: 'f-tid'})
        self.assertEqual(got['narrative_phase'], 'handed_off')
        self.assertIsNone(got['narrative_pr_url'])

    def test_no_receipt_is_stalled(self):
        # The other half of the receipt gate: delegated, past the grace window,
        # no receipt, no build signal ⇒ nothing is carrying this. Must resolve
        # `stalled`, NOT None — a None here is what silenced the narrator for
        # every stalled card (found 2026-07-21: 13–29-day-old delegations that
        # never dispatched, all rendering as calm blanks).
        got = _resolve(_cap(), {})
        self.assertEqual(got['narrative_phase'], 'stalled')
        self.assertIsNone(got['narrative_pr_url'])

    def test_waiting_approval_wins_over_handed_off(self):
        got = _resolve(_cap(), {}, has_open_approval=True,
                       dispatched_by_origin={DELEGATE_ID: 'f-tid'})
        self.assertEqual(got['narrative_phase'], 'waiting_approval')
        self.assertIsNone(got['narrative_pr_url'])

    def test_waiting_approval_wins_over_stalled(self):
        # A delegation parked on LARRY is waiting on him, not neglected by the
        # team — it must never be narrated as stalled.
        got = _resolve(_cap(), {}, has_open_approval=True)
        self.assertEqual(got['narrative_phase'], 'waiting_approval')

    def test_declined_resolves_declined_not_stalled(self):
        # The resolver is the ONE place the board and the thread agree on what a
        # card's state IS, so a rejected delegation must resolve `declined` here
        # — not `stalled` with the declined logic bolted onto one read path. What
        # differs downstream is what each DOES with it (the board renders it, the
        # narrator withholds the post); that split is tested in the narration
        # suite, not by giving the two surfaces different answers.
        got = _resolve(_cap(), {}, declined_origins={DELEGATE_ID})
        self.assertEqual(got['narrative_phase'], 'declined')
        self.assertIsNone(got['narrative_pr_url'])

    def test_waiting_approval_wins_over_declined(self):
        got = _resolve(_cap(), {}, has_open_approval=True,
                       declined_origins={DELEGATE_ID})
        self.assertEqual(got['narrative_phase'], 'waiting_approval')

    def test_build_signal_wins_over_declined(self):
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_request')]},
                       declined_origins={DELEGATE_ID})
        self.assertEqual(got['narrative_phase'], 'in_review')

    def test_declined_defaults_off(self):
        # Omitting the set keeps the pre-fix answer, so no caller changes meaning
        # by accident.
        self.assertEqual(_resolve(_cap(), {})['narrative_phase'], 'stalled')

    def test_build_signal_wins_over_missing_receipt(self):
        # A receipt-less card with a real review event is demonstrably being
        # worked; the trail phase outranks the receipt gate. Guards against a
        # regression where the stalled rung swallows live build signal.
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_request')]})
        self.assertEqual(got['narrative_phase'], 'in_review')

    def test_building_from_native_session_start(self):
        native = {DELEGATE_ID: [{'event_type': 'session_start',
                                 'agent': 'forge'}]}
        got = _resolve(_cap(), {}, native_build_events=native)
        self.assertEqual(got['narrative_phase'], 'building')

    def test_in_review(self):
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_request')]})
        self.assertEqual(got['narrative_phase'], 'in_review')

    def test_review_passed_carries_pr_url(self):
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_pass', pr_url=PR)]})
        self.assertEqual(got['narrative_phase'], 'review_passed')
        self.assertEqual(got['narrative_pr_url'], PR)


class MergeStampTest(unittest.TestCase):
    def test_outcome_merged_wins(self):
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'merged'})
        got = _resolve(cap, {DELEGATE_ID: [_ev('review_pass', pr_url=PR)]})
        self.assertEqual(got['narrative_phase'], 'merged')
        # PR url overlays from the trail when no shipped stamp carried one.
        self.assertEqual(got['narrative_pr_url'], PR)

    def test_shipped_note_alone_is_merged(self):
        # reconcile_completed_cards (S3) sets shipped_note but NOT spawned.outcome.
        cap = _cap(shipped_note='shipped in PR #7')
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'merged')

    def test_shipped_pr_url_supplies_merged_url(self):
        cap = _cap(shipped_pr_url=PR)
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'merged')
        self.assertEqual(got['narrative_pr_url'], PR)

    def test_merged_no_pr_url_is_none_url(self):
        # A no-PR delegation that still merged linked work: merged, but no url.
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'merged'})
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'merged')
        self.assertIsNone(got['narrative_pr_url'])

    def test_review_passed_never_inferred_as_merged(self):
        # Honest: a bare review_passed trail with NO merge stamp stays
        # review_passed — never silently promoted to merged.
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_pass', pr_url=PR)]})
        self.assertEqual(got['narrative_phase'], 'review_passed')


class ClosedFailedTest(unittest.TestCase):
    def test_closed_plus_failure_signaled(self):
        cap = _cap(
            spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                     'outcome': 'closed'},
            failure_signaled={'reason': 'closed without merge', 'at': 'x'})
        got = _resolve(cap, {})
        self.assertEqual(got['narrative_phase'], 'closed_failed')

    def test_closed_without_failure_signal_is_not_closed_failed(self):
        # outcome closed but no failure_signaled → falls through (no phantom
        # closed_failed); with no trail it degrades to handed_off.
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'closed'})
        got = _resolve(cap, {})
        self.assertNotEqual(got['narrative_phase'], 'closed_failed')


class DelegationDiedTest(unittest.TestCase):
    """delegate-died-surface-001: a delegated card whose delegate task
    terminally failed (timeout/spawn-failure) with no PR and no open approval
    resolves the distinct `delegation_died` phase — hard evidence from the
    terminal envelope, surfaced as 'still needs you', NOT blended into
    stalled/handed_off. Precedence: below merge/closed_failed stamps and below
    has_open_approval, above the receipt/grace guesswork, and yielding to any
    real build event."""

    def test_died_resolves_delegation_died(self):
        got = _resolve(_cap(), {}, died_by_origin={DELEGATE_ID: {'timed_out': False}})
        self.assertEqual(got['narrative_phase'], 'delegation_died')
        self.assertIsNone(got['narrative_pr_url'])
        self.assertFalse(got['narrative_died_timed_out'])

    def test_died_timed_out_flag_surfaces(self):
        got = _resolve(_cap(), {}, died_by_origin={DELEGATE_ID: {'timed_out': True}})
        self.assertEqual(got['narrative_phase'], 'delegation_died')
        self.assertTrue(got['narrative_died_timed_out'])

    def test_died_beats_stalled_no_receipt(self):
        # Same past-grace/no-receipt card that would otherwise read `stalled`
        # (see PhasePrecedenceTest.test_no_receipt_is_stalled) — the terminal
        # envelope outranks the guess.
        got = _resolve(_cap(), {}, died_by_origin={DELEGATE_ID: {'timed_out': False}})
        self.assertEqual(got['narrative_phase'], 'delegation_died')

    def test_waiting_approval_wins_over_died(self):
        # A re-delegation now parked on Larry is waiting on HIM, not dead.
        got = _resolve(_cap(), {}, has_open_approval=True,
                       died_by_origin={DELEGATE_ID: {'timed_out': True}})
        self.assertEqual(got['narrative_phase'], 'waiting_approval')

    def test_real_build_event_wins_over_died(self):
        # If something actually ran and reached review, the trail says so — the
        # died floor only applies when the delegation produced nothing at all.
        got = _resolve(_cap(), {DELEGATE_ID: [_ev('review_request')]},
                       died_by_origin={DELEGATE_ID: {'timed_out': True}})
        self.assertEqual(got['narrative_phase'], 'in_review')

    def test_merge_stamp_wins_over_died(self):
        cap = _cap(spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                            'outcome': 'merged'})
        got = _resolve(cap, {}, died_by_origin={DELEGATE_ID: {'timed_out': True}})
        self.assertEqual(got['narrative_phase'], 'merged')

    def test_closed_failed_wins_over_died(self):
        cap = _cap(
            spawned={'kind': 'delegate', 'task_id': DELEGATE_ID,
                     'outcome': 'closed'},
            failure_signaled={'reason': 'closed without merge', 'at': 'x'})
        got = _resolve(cap, {}, died_by_origin={DELEGATE_ID: {'timed_out': True}})
        self.assertEqual(got['narrative_phase'], 'closed_failed')

    def test_died_defaults_off(self):
        # Omitting the map keeps the pre-fix answer — no caller changes meaning
        # by accident.
        self.assertEqual(_resolve(_cap(), {})['narrative_phase'], 'stalled')


class ScanDelegateDiedOriginsTest(unittest.TestCase):
    """The filesystem read that PROVES a delegation died: it inspects Beacon's
    outbox archive for the delegate task's most-recent terminal envelope.
    Positive-evidence only — a death is surfaced solely from a matching
    terminal-failure NO-OUTCOME envelope, never inferred from absence."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.archive = self.root / 'outboxes' / 'beacon' / '.archive'
        self.archive.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self, name, payload):
        import json
        (self.archive / name).write_text(json.dumps(payload))

    def _scan(self, ids):
        return da.scan_delegate_died_origins(ids, agents_root=self.root)

    def test_timeout_envelope_is_a_death(self):
        self._seed(f'{DELEGATE_ID}.json',
                   {'exit_code': -1, 'result': 'TIMEOUT after 600s',
                    'duration_sec': 600.0})
        got = self._scan([DELEGATE_ID])
        self.assertIn(DELEGATE_ID, got)
        self.assertTrue(got[DELEGATE_ID]['timed_out'])

    def test_timed_out_flag_counts_as_death(self):
        self._seed(f'{DELEGATE_ID}.json',
                   {'exit_code': -1, 'result': 'stopped', 'timed_out': True})
        got = self._scan([DELEGATE_ID])
        self.assertTrue(got[DELEGATE_ID]['timed_out'])

    def test_non_timeout_failure_is_death_not_timed_out(self):
        self._seed(f'{DELEGATE_ID}.json',
                   {'exit_code': 1, 'result': 'crashed'})
        got = self._scan([DELEGATE_ID])
        self.assertIn(DELEGATE_ID, got)
        self.assertFalse(got[DELEGATE_ID]['timed_out'])

    def test_success_is_not_a_death(self):
        self._seed(f'{DELEGATE_ID}.json',
                   {'exit_code': 0, 'result': 'done'})
        self.assertEqual(self._scan([DELEGATE_ID]), {})

    def test_approval_request_is_an_outcome_not_death(self):
        # A handed-off proposal is an outcome, never a death — even at exit -1.
        self._seed(f'{DELEGATE_ID}.json',
                   {'exit_code': -1,
                    'result': 'APPROVAL_REQUEST\ntimeout after 600s'})
        self.assertEqual(self._scan([DELEGATE_ID]), {})

    def test_pr_url_is_shipped_not_death(self):
        self._seed(f'{DELEGATE_ID}.json',
                   {'exit_code': -1,
                    'result': 'PR opened: https://github.com/o/r/pull/9'})
        self.assertEqual(self._scan([DELEGATE_ID]), {})

    def test_most_recent_envelope_wins(self):
        import os, time
        old = self.archive / f'{DELEGATE_ID}.1.json'
        new = self.archive / f'{DELEGATE_ID}.2.json'
        import json
        old.write_text(json.dumps({'exit_code': -1, 'result': 'TIMEOUT after 600s'}))
        new.write_text(json.dumps({'exit_code': 0, 'result': 'done'}))
        # Force `new` to be strictly newer regardless of write granularity.
        past = time.time() - 100
        os.utime(old, (past, past))
        self.assertEqual(self._scan([DELEGATE_ID]), {})

    def test_unwanted_ids_ignored(self):
        self._seed(f'{DELEGATE_ID}.json',
                   {'exit_code': -1, 'result': 'TIMEOUT after 600s'})
        self.assertEqual(self._scan(['delegate-someone-else-zz99']), {})

    def test_missing_archive_is_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                da.scan_delegate_died_origins([DELEGATE_ID], agents_root=Path(d)),
                {})

    def test_empty_ids_short_circuits(self):
        self.assertEqual(self._scan([]), {})


if __name__ == '__main__':
    unittest.main()
