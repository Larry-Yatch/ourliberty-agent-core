#!/usr/bin/env python3
"""test_suite_guardian_proposal.py — PR-2 tests for the Main-Suite Green Guardian
propose->approve->dispatch loop + the outcome ledger.

Covers (spec § 8, PR-2):
  * one-decision-per-run batching (L1): all actionable findings -> ONE pending
    entry (+ one approval_request), chat_id=0, bare_approvable=False;
  * serial-drain cap (D2.1): at most OPEN_FIX_CAP fixes dispatched in flight;
  * dedup (find_by_id_any_state): no double-emit / no re-propose of a live row;
  * reject -> parked (L9): a rejected proposal parks and is never re-proposed;
  * edge-triggered escalation (D2.3): genuine break pages once per episode,
    re-arms only after the victim returns green;
  * observable-based resolution (D2.6): victim green >=2 runs AND named poison
    test present + passing resolves, regardless of merge provenance;
  * abandoned age-out (D2.6): approved-but-dead obligation terminates.

Plus focused ledger unit tests. All state hits tmp files via _bootstrap's
redirection; every real side effect is a recording fake (no Supabase, no DM,
no worktree).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_suite_guardian_proposal
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import main_suite_guardian as g  # noqa: E402
import suite_guardian_ledger as ledger  # noqa: E402


_T0 = datetime(2026, 7, 8, 3, 0, tzinfo=timezone.utc)


class RecordingDeps:
    """A ProposalDeps whose every side effect records into lists. ``decisions``
    maps a run_task_id -> 'approved'|'rejected'|None; ``pending_ids`` is the set
    of already-surfaced run ids (drives the find_pending dedup); ``poison`` is the
    set of poison test names considered present."""

    def __init__(self, *, decisions=None, pending_ids=None, poison=None):
        self._decisions = dict(decisions or {})
        self._pending_ids = set(pending_ids or ())
        self._poison = set(poison or ())
        self.added = []
        self.emitted = []
        self.cards = []
        self.resolved_cards = []
        self.escalated = []
        self.dispatched = []
        self._fix_seq = 0

    def as_deps(self) -> g.ProposalDeps:
        return g.ProposalDeps(
            add_pending=self._add_pending,
            find_pending=self._find_pending,
            emit_approval_request=self._emit,
            lookup_decision=self._lookup,
            upsert_card=self._upsert_card,
            resolve_card=self._resolve_card,
            escalate=self._escalate,
            dispatch_fix=self._dispatch_fix,
            poison_present=self._poison_present,
        )

    def _add_pending(self, payload, chat_id):
        entry = {'id': payload['task_id'], 'chat_id': chat_id,
                 'dispatch_payload': payload, 'status': 'pending'}
        self.added.append((payload, chat_id))
        self._pending_ids.add(payload['task_id'])
        return entry

    def _find_pending(self, task_id):
        return {'id': task_id} if task_id in self._pending_ids else None

    def _emit(self, payload):
        self.emitted.append(payload)
        return True

    def _lookup(self, run_task_id):
        return self._decisions.get(run_task_id)

    def _upsert_card(self, key, record):
        self.cards.append((key, record))

    def _resolve_card(self, key):
        self.resolved_cards.append(key)
        return True

    def _escalate(self, **kw):
        self.escalated.append(kw)

    def _dispatch_fix(self, **kw):
        self._fix_seq += 1
        fid = f'fix-{self._fix_seq}'
        self.dispatched.append(kw)
        return fid

    def _poison_present(self, name):
        return name in self._poison


class _TmpStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.registry_path = self.tmp / 'main-suite-guardian.json'
        self.ledger_path = self.tmp / 'suite-guardian-ledger.json'

    def _write_registry(self, tests):
        reg = g.new_registry()
        reg['tests'] = tests
        g.save_registry(self.registry_path, reg)

    def _cycle(self, run_result, deps, *, now=_T0):
        return g.run_proposal_cycle(
            Path('/nonexistent-repo'), run_result,
            registry_path=self.registry_path, ledger_path=self.ledger_path,
            deps=deps, now=now, chat_id=0,
        )


# --- pure helpers ------------------------------------------------------------

class PureHelpersTest(unittest.TestCase):

    def test_run_entry_task_id_is_date_scoped(self):
        self.assertEqual(g._run_entry_task_id(_T0), 'suite-guardian-run-2026-07-08')

    def test_poison_test_name_is_grepable_slug(self):
        name = g.poison_test_name('test_foo.TestBar.test_baz')
        self.assertEqual(name, 'test_poison_test_foo_testbar_test_baz')

    def test_should_escalate_break_edge(self):
        self.assertTrue(g.should_escalate_break(
            {'classification': g.CLS_GENUINE_BREAK, 'consecutive_red_runs': 2}))
        # one red is not yet the edge
        self.assertFalse(g.should_escalate_break(
            {'classification': g.CLS_GENUINE_BREAK, 'consecutive_red_runs': 1}))
        # already escalated this episode
        self.assertFalse(g.should_escalate_break(
            {'classification': g.CLS_GENUINE_BREAK, 'consecutive_red_runs': 3,
             'break_escalated': True}))
        # order-flakes never page
        self.assertFalse(g.should_escalate_break(
            {'classification': g.CLS_ORDER_FLAKE, 'consecutive_red_runs': 9}))

    def test_fix_prompt_names_poison_test_and_scope(self):
        p = g.build_fix_task_prompt('t.A.b', g.CLS_ORDER_FLAKE, 'test_poison_x')
        self.assertIn('test_poison_x', p)
        self.assertIn('scripts/tests/**', p)
        self.assertIn('ORDER-FLAKE', p)


# --- select_actionable -------------------------------------------------------

class SelectActionableTest(_TmpStateTest):

    def test_only_order_flake_and_genuine_break(self):
        run = {'status': g.RUN_RED, 'classifications': {
            'a': g.CLS_ORDER_FLAKE, 'b': g.CLS_GENUINE_BREAK,
            'c': g.CLS_ENV_FAIL, 'd': g.CLS_BACKLOG, 'e': g.CLS_INFRA_FLAKE,
        }}
        picks = g.select_actionable(run, g.new_registry(),
                                    ledger_path=self.ledger_path)
        self.assertEqual({p['test_id'] for p in picks}, {'a', 'b'})

    def test_skips_live_and_parked_rows(self):
        ledger.open_proposal('a', run_task_id='r1', poison_test_name='pa',
                             now=_T0, path=self.ledger_path)  # live/proposed
        ledger.open_proposal('b', run_task_id='r1', poison_test_name='pb',
                             now=_T0, path=self.ledger_path)
        ledger.set_decision('b', ledger.DEC_PARKED, now=_T0, path=self.ledger_path)
        run = {'status': g.RUN_RED, 'classifications': {
            'a': g.CLS_ORDER_FLAKE, 'b': g.CLS_ORDER_FLAKE, 'c': g.CLS_ORDER_FLAKE,
        }}
        picks = g.select_actionable(run, g.new_registry(),
                                    ledger_path=self.ledger_path)
        self.assertEqual({p['test_id'] for p in picks}, {'c'})

    def test_skips_registry_parked(self):
        self._write_registry({'a': {'classification': g.CLS_ORDER_FLAKE,
                                     'parked': True}})
        reg = g.load_registry(self.registry_path)
        run = {'status': g.RUN_RED, 'classifications': {'a': g.CLS_ORDER_FLAKE}}
        picks = g.select_actionable(run, reg, ledger_path=self.ledger_path)
        self.assertEqual(picks, [])


# --- one decision per run ----------------------------------------------------

class OneDecisionPerRunTest(_TmpStateTest):

    def test_batches_all_findings_into_one_pending(self):
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {
            'a': g.CLS_ORDER_FLAKE, 'b': g.CLS_ORDER_FLAKE,
            'c': g.CLS_GENUINE_BREAK,
        }}
        summary = self._cycle(run, deps.as_deps())
        # exactly one pending + one approval_request for the whole run
        self.assertEqual(len(deps.added), 1)
        self.assertEqual(len(deps.emitted), 1)
        payload, chat_id = deps.added[0]
        self.assertEqual(chat_id, 0)
        self.assertEqual(payload['task_id'], 'suite-guardian-run-2026-07-08')
        self.assertFalse(payload['bare_approvable'])
        self.assertEqual({p['test_id'] for p in payload['proposals']},
                         {'a', 'b', 'c'})
        # ledger has three open, proposed obligations
        self.assertEqual(len(ledger.list_open(path=self.ledger_path)), 3)
        self.assertEqual(sorted(summary['proposed']), ['a', 'b', 'c'])
        self.assertEqual(summary['pending_entry'], 'suite-guardian-run-2026-07-08')

    def test_fyi_card_is_needs_larry_false(self):
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {'a': g.CLS_ORDER_FLAKE}}
        self._cycle(run, deps.as_deps())
        self.assertEqual(len(deps.cards), 1)
        _key, record = deps.cards[0]
        self.assertIs(record['needs_larry'], False)

    def test_quiet_run_resolves_card_and_emits_nothing(self):
        deps = RecordingDeps()
        run = {'status': g.RUN_GREEN, 'classifications': {}}
        self._cycle(run, deps.as_deps())
        self.assertEqual(deps.added, [])
        self.assertEqual(deps.cards, [])
        self.assertEqual(deps.resolved_cards, [g.FYI_CARD_KEY])


# --- dedup -------------------------------------------------------------------

class DedupTest(_TmpStateTest):

    def test_existing_pending_suppresses_reemit(self):
        # a same-day pending entry already exists for this run id
        deps = RecordingDeps(pending_ids={'suite-guardian-run-2026-07-08'})
        run = {'status': g.RUN_RED, 'classifications': {'a': g.CLS_ORDER_FLAKE}}
        self._cycle(run, deps.as_deps())
        self.assertEqual(deps.added, [])   # no double add_pending
        self.assertEqual(deps.emitted, [])  # no double approval_request
        # obligation still opened so the state is tracked
        self.assertIsNotNone(ledger.get('a', path=self.ledger_path))

    def test_second_run_does_not_reopen_live_row(self):
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {'a': g.CLS_ORDER_FLAKE}}
        self._cycle(run, deps.as_deps())
        # a later run the same day sees the row is already open -> nothing new
        deps2 = RecordingDeps(pending_ids={'suite-guardian-run-2026-07-08'})
        summary = self._cycle(run, deps2.as_deps())
        self.assertEqual(summary['proposed'], [])
        self.assertEqual(deps2.added, [])


# --- reject -> parked --------------------------------------------------------

class RejectParksTest(_TmpStateTest):

    def test_rejected_proposal_parks_and_is_never_reproposed(self):
        # seed a proposed obligation under run id 'batch-1'
        ledger.open_proposal('a', run_task_id='batch-1', poison_test_name='pa',
                             now=_T0, path=self.ledger_path)
        deps = RecordingDeps(decisions={'batch-1': 'rejected'})
        run = {'status': g.RUN_RED, 'classifications': {'a': g.CLS_ORDER_FLAKE}}
        summary = self._cycle(run, deps.as_deps())
        self.assertIn('a', summary['parked'])
        row = ledger.get('a', path=self.ledger_path)
        self.assertEqual(row['status'], ledger.RESOLVED)
        self.assertEqual(row['resolution'], 'parked')
        self.assertEqual(row['decision'], ledger.DEC_PARKED)
        # not re-proposed even though it is still red this run
        self.assertEqual(summary['proposed'], [])
        self.assertEqual(deps.added, [])

    def test_approved_proposal_stays_open_for_drain(self):
        ledger.open_proposal('a', run_task_id='batch-1', poison_test_name='pa',
                             now=_T0, path=self.ledger_path)
        deps = RecordingDeps(decisions={'batch-1': 'approved'})
        run = {'status': g.RUN_RED, 'classifications': {}}
        self._cycle(run, deps.as_deps())
        row = ledger.get('a', path=self.ledger_path)
        self.assertEqual(row['status'], ledger.OPEN)
        self.assertEqual(row['decision'], ledger.DEC_APPROVED)


# --- serial drain cap --------------------------------------------------------

class SerialDrainTest(_TmpStateTest):

    def _seed_approved(self, n):
        for i in range(n):
            tid = f't{i}'
            ledger.open_proposal(tid, run_task_id='b', poison_test_name=f'p{i}',
                                 now=_T0 + timedelta(seconds=i),
                                 path=self.ledger_path)
            ledger.set_decision(tid, ledger.DEC_APPROVED, now=_T0,
                                path=self.ledger_path)

    def test_dispatches_at_most_cap(self):
        self._seed_approved(5)
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {}}
        summary = self._cycle(run, deps.as_deps())
        self.assertEqual(len(summary['dispatched']), ledger.OPEN_FIX_CAP)
        self.assertEqual(len(deps.dispatched), ledger.OPEN_FIX_CAP)
        self.assertEqual(ledger.count_open_dispatched_fixes(path=self.ledger_path),
                         ledger.OPEN_FIX_CAP)

    def test_drain_is_fifo_by_proposal_age(self):
        self._seed_approved(5)
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {}}
        self._cycle(run, deps.as_deps())
        dispatched = {kw['test_id'] for kw in deps.dispatched}
        self.assertEqual(dispatched, {'t0', 't1', 't2'})

    def test_no_new_dispatch_when_cap_full(self):
        self._seed_approved(3)
        for i in range(3):  # already dispatched -> cap full
            ledger.mark_dispatched(f't{i}', f'fix-{i}', now=_T0,
                                   path=self.ledger_path)
        # add one more approved, undispatched
        ledger.open_proposal('t9', run_task_id='b', poison_test_name='p9',
                             now=_T0, path=self.ledger_path)
        ledger.set_decision('t9', ledger.DEC_APPROVED, now=_T0,
                            path=self.ledger_path)
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {}}
        summary = self._cycle(run, deps.as_deps())
        self.assertEqual(summary['dispatched'], [])


# --- edge-triggered escalation -----------------------------------------------

class EscalationTest(_TmpStateTest):

    def test_pages_once_per_episode_and_rearms_after_green(self):
        # red episode at 2 consecutive reds -> pages once, latches
        self._write_registry({'a': {'classification': g.CLS_GENUINE_BREAK,
                                     'consecutive_red_runs': 2,
                                     'consecutive_green_runs': 0}})
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {}}
        s1 = self._cycle(run, deps.as_deps())
        self.assertEqual(s1['escalated'], ['a'])
        self.assertTrue(g.load_registry(self.registry_path)
                        ['tests']['a']['break_escalated'])

        # still red next run -> does NOT re-page (latched)
        reg = g.load_registry(self.registry_path)
        reg['tests']['a']['consecutive_red_runs'] = 3
        g.save_registry(self.registry_path, reg)
        deps2 = RecordingDeps()
        self.assertEqual(self._cycle(run, deps2.as_deps())['escalated'], [])

        # victim returns green -> latch resets (new episode possible)
        reg = g.load_registry(self.registry_path)
        reg['tests']['a']['consecutive_red_runs'] = 0
        reg['tests']['a']['consecutive_green_runs'] = 1
        g.save_registry(self.registry_path, reg)
        deps3 = RecordingDeps()
        self._cycle(run, deps3.as_deps())
        self.assertFalse(g.load_registry(self.registry_path)
                         ['tests']['a']['break_escalated'])

        # breaks again 2 reds -> pages again
        reg = g.load_registry(self.registry_path)
        reg['tests']['a']['consecutive_red_runs'] = 2
        reg['tests']['a']['consecutive_green_runs'] = 0
        g.save_registry(self.registry_path, reg)
        deps4 = RecordingDeps()
        self.assertEqual(self._cycle(run, deps4.as_deps())['escalated'], ['a'])

    def test_order_flake_never_escalates(self):
        self._write_registry({'a': {'classification': g.CLS_ORDER_FLAKE,
                                     'consecutive_red_runs': 9}})
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {}}
        self.assertEqual(self._cycle(run, deps.as_deps())['escalated'], [])
        self.assertEqual(deps.escalated, [])


# --- observable-based resolution ---------------------------------------------

class ObservableResolutionTest(_TmpStateTest):

    def _seed_dispatched(self, tid='a', poison='pa'):
        ledger.open_proposal(tid, run_task_id='b', poison_test_name=poison,
                             now=_T0, path=self.ledger_path)
        ledger.set_decision(tid, ledger.DEC_APPROVED, now=_T0,
                            path=self.ledger_path)
        ledger.mark_dispatched(tid, 'fix-a', now=_T0, path=self.ledger_path)

    def test_resolves_on_green_streak_and_poison_present(self):
        self._seed_dispatched()
        self._write_registry({'a': {'classification': g.CLS_ORDER_FLAKE,
                                     'consecutive_green_runs': 2,
                                     'consecutive_red_runs': 0}})
        deps = RecordingDeps(poison={'pa'})
        run = {'status': g.RUN_GREEN, 'classifications': {}}
        summary = self._cycle(run, deps.as_deps())
        self.assertIn('a', summary['resolved'])
        row = ledger.get('a', path=self.ledger_path)
        self.assertEqual(row['status'], ledger.RESOLVED)
        self.assertEqual(row['resolution'], 'observed-green')

    def test_not_resolved_without_poison(self):
        self._seed_dispatched()
        self._write_registry({'a': {'classification': g.CLS_ORDER_FLAKE,
                                     'consecutive_green_runs': 2,
                                     'consecutive_red_runs': 0}})
        deps = RecordingDeps(poison=set())  # poison test absent
        run = {'status': g.RUN_GREEN, 'classifications': {}}
        summary = self._cycle(run, deps.as_deps())
        self.assertEqual(summary['resolved'], [])
        self.assertEqual(ledger.get('a', path=self.ledger_path)['status'],
                         ledger.OPEN)

    def test_not_resolved_without_green_streak(self):
        self._seed_dispatched()
        self._write_registry({'a': {'classification': g.CLS_ORDER_FLAKE,
                                     'consecutive_green_runs': 1,
                                     'consecutive_red_runs': 0}})
        deps = RecordingDeps(poison={'pa'})
        run = {'status': g.RUN_GREEN, 'classifications': {}}
        summary = self._cycle(run, deps.as_deps())
        self.assertEqual(summary['resolved'], [])


# --- abandoned age-out -------------------------------------------------------

class AbandonedAgeOutTest(_TmpStateTest):

    def test_approved_but_dead_ages_out(self):
        old = _T0 - timedelta(days=8)
        ledger.open_proposal('a', run_task_id='b', poison_test_name='pa',
                             now=old, path=self.ledger_path)
        ledger.set_decision('a', ledger.DEC_APPROVED, now=old,
                            path=self.ledger_path)
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {}}
        summary = self._cycle(run, deps.as_deps())
        self.assertIn('a', summary['abandoned'])
        row = ledger.get('a', path=self.ledger_path)
        self.assertEqual(row['status'], ledger.ABANDONED)

    def test_fresh_approved_does_not_age_out(self):
        ledger.open_proposal('a', run_task_id='b', poison_test_name='pa',
                             now=_T0, path=self.ledger_path)
        ledger.set_decision('a', ledger.DEC_APPROVED, now=_T0,
                            path=self.ledger_path)
        deps = RecordingDeps()
        run = {'status': g.RUN_RED, 'classifications': {}}
        summary = self._cycle(run, deps.as_deps())
        self.assertEqual(summary['abandoned'], [])

    def test_abandoned_is_reeligible_once(self):
        old = _T0 - timedelta(days=8)
        ledger.open_proposal('a', run_task_id='b', poison_test_name='pa',
                             now=old, path=self.ledger_path)
        ledger.set_decision('a', ledger.DEC_APPROVED, now=old,
                            path=self.ledger_path)
        ledger.age_out_abandoned(now=_T0, path=self.ledger_path)
        # re-proposing after abandonment opens a fresh, re-eligible obligation
        row = ledger.open_proposal('a', run_task_id='b2', poison_test_name='pa',
                                   now=_T0, path=self.ledger_path)
        self.assertEqual(row['status'], ledger.OPEN)
        self.assertTrue(row['reeligible'])


# --- ledger unit tests -------------------------------------------------------

class LedgerUnitTest(_TmpStateTest):

    def test_open_proposal_idempotent(self):
        r1 = ledger.open_proposal('a', run_task_id='b', poison_test_name='pa',
                                  now=_T0, path=self.ledger_path)
        r2 = ledger.open_proposal('a', run_task_id='b2', poison_test_name='pa2',
                                  now=_T0 + timedelta(hours=1),
                                  path=self.ledger_path)
        self.assertEqual(r1['proposed_at'], r2['proposed_at'])  # preserved
        self.assertEqual(r2['run_task_id'], 'b2')  # refreshed
        self.assertEqual(len(ledger.list_open(path=self.ledger_path)), 1)

    def test_parked_is_never_reopened(self):
        ledger.open_proposal('a', run_task_id='b', poison_test_name='pa',
                             now=_T0, path=self.ledger_path)
        ledger.set_decision('a', ledger.DEC_PARKED, now=_T0, path=self.ledger_path)
        r = ledger.open_proposal('a', run_task_id='b2', poison_test_name='pa',
                                 now=_T0, path=self.ledger_path)
        self.assertEqual(r['decision'], ledger.DEC_PARKED)
        self.assertEqual(r['status'], ledger.RESOLVED)

    def test_corrupt_file_degrades_to_empty(self):
        self.ledger_path.write_text('not json{', encoding='utf-8')
        self.assertEqual(ledger.list_open(path=self.ledger_path), [])
        # and a write still succeeds over the corrupt file
        ledger.open_proposal('a', run_task_id='b', poison_test_name='pa',
                             now=_T0, path=self.ledger_path)
        self.assertEqual(len(ledger.list_open(path=self.ledger_path)), 1)

    def test_set_decision_invalid_raises(self):
        with self.assertRaises(ValueError):
            ledger.set_decision('a', 'bogus', path=self.ledger_path)


if __name__ == '__main__':
    unittest.main()
