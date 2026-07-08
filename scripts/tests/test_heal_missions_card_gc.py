#!/usr/bin/env python3
"""Tests for heal_missions_card_gc (missions-v2 Phase 1 § 6).

Covers the four Mirror-review focus areas plus the spec § 8 1b proof plan:
  * idempotency — a closed (or synthetic-done'd) session is never re-retired;
    an already-aging capture is never re-flagged; a no-op tick mutates nothing.
  * never-delete-parked — aging only adds a flag; the captures list length and
    every other field are preserved.
  * merge / delete / repo-gone / idle classification — each retire trigger fires
    once, and every indeterminate (None) signal errs toward KEEP.
  * atomic writes — captures.json is written via tmp+rename; a malformed file is
    refused (None) rather than appended onto.
  * commit-and-push — refuses to commit off-main; no-op on a clean tree; commits
    a real delta on a temp git repo.

All effectful edges are driven through the injectable seams (emit_fn /
events_fetcher / now) so no test touches the live Supabase table, gh, or the
real captures.json. Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_missions_card_gc
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_missions_card_gc as h  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _start(task_id, ts, *, repo='ourliberty-agent-core', branch='forge/x'):
    return {'task_id': task_id, 'event_type': h.EVENT_START, 'ts': _iso(ts),
            'payload': {'repo': repo, 'branch': branch}}


def _done(task_id, ts):
    return {'task_id': task_id, 'event_type': h.EVENT_DONE, 'ts': _iso(ts),
            'payload': {}}


def _active(task_id, ts):
    return {'task_id': task_id, 'event_type': h.EVENT_ACTIVE, 'ts': _iso(ts),
            'payload': {}}


# ---------------------------------------------------------------- time helpers


class BusinessDaysTest(unittest.TestCase):
    def test_zero_when_end_not_after_start(self):
        d = datetime(2026, 6, 8, tzinfo=timezone.utc)  # Monday
        self.assertEqual(h.business_days_between(d, d), 0)
        self.assertEqual(h.business_days_between(d, d - timedelta(days=2)), 0)

    def test_skips_weekends(self):
        # Mon 2026-06-08 -> next Mon 2026-06-15 spans one full weekend.
        start = datetime(2026, 6, 8, tzinfo=timezone.utc)
        end = datetime(2026, 6, 15, tzinfo=timezone.utc)
        # Tue,Wed,Thu,Fri,Mon = 5 business days (Sat/Sun excluded).
        self.assertEqual(h.business_days_between(start, end), 5)

    def test_parse_iso_utc_tolerant(self):
        self.assertIsNone(h.parse_iso_utc(None))
        self.assertIsNone(h.parse_iso_utc('not-a-date'))
        got = h.parse_iso_utc('2026-06-09T18:22:00Z')
        self.assertEqual(got, datetime(2026, 6, 9, 18, 22, tzinfo=timezone.utc))


# ---------------------------------------------------- open-session detection


class GatherOpenSessionsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)

    def test_start_with_no_done_is_open(self):
        rows = [_start('t1', self.now - timedelta(hours=1))]
        out = h.gather_open_sessions(rows, self.now)
        self.assertEqual([s.task_id for s in out], ['t1'])
        self.assertEqual(out[0].repo, 'ourliberty-agent-core')
        self.assertEqual(out[0].branch, 'forge/x')

    def test_done_after_start_closes_card(self):
        s = self.now - timedelta(hours=2)
        rows = [_start('t1', s), _done('t1', s + timedelta(minutes=5))]
        self.assertEqual(h.gather_open_sessions(rows, self.now), [])

    def test_done_before_a_newer_start_stays_open(self):
        # restart after a done: latest start has no later done -> open.
        rows = [
            _start('t1', self.now - timedelta(hours=3)),
            _done('t1', self.now - timedelta(hours=2)),
            _start('t1', self.now - timedelta(hours=1)),
        ]
        out = h.gather_open_sessions(rows, self.now)
        self.assertEqual([s.task_id for s in out], ['t1'])

    def test_idle_seconds_uses_latest_activity(self):
        rows = [
            _start('t1', self.now - timedelta(hours=5)),
            _active('t1', self.now - timedelta(hours=1)),
        ]
        out = h.gather_open_sessions(rows, self.now)
        self.assertAlmostEqual(out[0].idle_seconds, 3600, delta=2)

    def test_ignores_unrelated_event_types(self):
        rows = [{'task_id': 't1', 'event_type': 'pr_opened', 'ts': _iso(self.now)}]
        self.assertEqual(h.gather_open_sessions(rows, self.now), [])


# ---------------------------------------------------------------- classify


class ClassifySessionTest(unittest.TestCase):
    def _sess(self, idle=0.0):
        return h.OpenSession(task_id='t', repo='r', branch='forge/x',
                             last_activity_ts=None, idle_seconds=idle)

    def test_repo_gone_retires(self):
        d = h.classify_session(self._sess(), repo_present=False,
                               branch_merged=None, branch_deleted=None,
                               idle_seconds=0)
        self.assertEqual((d.action, d.reason), ('retire', 'repo-dir-gone'))

    def test_branch_merged_retires(self):
        d = h.classify_session(self._sess(), repo_present=True,
                               branch_merged=True, branch_deleted=False,
                               idle_seconds=0)
        self.assertEqual(d.action, 'retire')
        self.assertEqual(d.reason, 'branch-merged')

    def test_branch_deleted_retires(self):
        d = h.classify_session(self._sess(), repo_present=True,
                               branch_merged=False, branch_deleted=True,
                               idle_seconds=0)
        self.assertEqual(d.action, 'retire')
        self.assertEqual(d.reason, 'branch-deleted')

    def test_idle_past_floor_retires(self):
        d = h.classify_session(self._sess(), repo_present=True,
                               branch_merged=False, branch_deleted=False,
                               idle_seconds=h.STALE_SESSION_IDLE_SECONDS + 1)
        self.assertEqual(d.action, 'retire')

    def test_all_indeterminate_keeps(self):
        # Every signal None and not idle -> KEEP (conservative).
        d = h.classify_session(self._sess(idle=10), repo_present=True,
                               branch_merged=None, branch_deleted=None,
                               idle_seconds=10)
        self.assertEqual(d.action, 'keep')


class BranchFactsTest(unittest.TestCase):
    def test_trunk_never_merged_or_deleted(self):
        s = h.OpenSession('t', 'r', 'main', None, 0)
        sig = h.RepoSignals(present=True, merged_heads=set(), branches=set())
        self.assertEqual(h._branch_facts(s, sig), (False, False))

    def test_none_signals_propagate_as_indeterminate(self):
        s = h.OpenSession('t', 'r', 'forge/x', None, 0)
        sig = h.RepoSignals(present=True, merged_heads=None, branches=None)
        self.assertEqual(h._branch_facts(s, sig), (None, None))

    def test_merged_and_deleted_resolved(self):
        s = h.OpenSession('t', 'r', 'forge/x', None, 0)
        sig = h.RepoSignals(present=True, merged_heads={'forge/x'},
                            branches={'main'})  # forge/x absent -> deleted
        self.assertEqual(h._branch_facts(s, sig), (True, True))


# ------------------------------------------------ retire (effectful, seamed)


class RetireStaleSessionsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
        self.emitted = []

        def fake_emit(*, event_type, agent, task_id, payload, ts=None, **_):
            self.emitted.append((event_type, agent, task_id, payload, ts))
            return True

        self.fake_emit = fake_emit

    def test_retires_idle_session_with_synthetic_done(self):
        rows = [_start('t1', self.now - timedelta(hours=48),
                       repo='unknown-repo', branch='main')]
        res = h.retire_stale_sessions(rows, {}, self.now,
                                      emit_fn=self.fake_emit, dry_run=False)
        self.assertEqual([tid for tid, _ in res.retired], ['t1'])
        self.assertEqual(len(self.emitted), 1)
        et, agent, tid, payload, ts = self.emitted[0]
        self.assertEqual(et, h.EVENT_DONE)
        self.assertEqual(agent, h.DESKTOP_AGENT)
        self.assertEqual(tid, 't1')
        self.assertTrue(payload['synthetic'])
        self.assertEqual(payload['retired_by'], 'heal_missions_card_gc')
        self.assertEqual(ts, self.now.isoformat())

    def test_dry_run_emits_nothing(self):
        rows = [_start('t1', self.now - timedelta(hours=48),
                       repo='unknown-repo', branch='main')]
        res = h.retire_stale_sessions(rows, {}, self.now,
                                      emit_fn=self.fake_emit, dry_run=True)
        self.assertEqual(len(self.emitted), 0)
        self.assertEqual(len(res.retired), 1)
        self.assertIn('dry-run', res.retired[0][1])

    def test_closed_session_is_not_retired_idempotent(self):
        # A synthetic done already landed -> next tick sees it closed, no re-emit.
        s = self.now - timedelta(hours=48)
        rows = [_start('t1', s, repo='unknown-repo', branch='main'),
                _done('t1', s + timedelta(hours=1))]
        res = h.retire_stale_sessions(rows, {}, self.now,
                                      emit_fn=self.fake_emit, dry_run=False)
        self.assertEqual(res.retired, [])
        self.assertEqual(len(self.emitted), 0)

    def test_fresh_session_is_kept(self):
        rows = [_start('t1', self.now - timedelta(minutes=10),
                       repo='unknown-repo', branch='main')]
        res = h.retire_stale_sessions(rows, {}, self.now,
                                      emit_fn=self.fake_emit, dry_run=False)
        self.assertEqual(res.retired, [])
        self.assertEqual(res.kept, 1)

    def test_emit_failure_leaves_card_open(self):
        rows = [_start('t1', self.now - timedelta(hours=48),
                       repo='unknown-repo', branch='main')]

        def failing_emit(**_):
            return False

        res = h.retire_stale_sessions(rows, {}, self.now,
                                      emit_fn=failing_emit, dry_run=False)
        self.assertEqual(res.retired, [])
        self.assertEqual(res.emit_failures, ['t1'])

    def test_repo_gone_retires_via_config(self):
        # A configured repo whose dir is absent -> repo-dir-gone retire.
        rows = [_start('t1', self.now - timedelta(minutes=1),
                       repo='ourliberty-agent-core', branch='forge/x')]
        repo_paths = {'ourliberty-agent-core': Path('/nonexistent/repo/xyz')}
        with mock.patch.object(h, 'merged_pr_heads') as mh, \
                mock.patch.object(h, 'existing_branches') as eb:
            res = h.retire_stale_sessions(rows, repo_paths, self.now,
                                          emit_fn=self.fake_emit, dry_run=False)
            mh.assert_not_called()  # never even probed git on a gone repo
            eb.assert_not_called()
        self.assertEqual([tid for tid, _ in res.retired], ['t1'])
        self.assertEqual(self.emitted[0][3]['retire_reason'], 'repo-dir-gone')


# ----------------------------------------------- capture aging (never delete)


class AgeParkedCapturesTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)  # Monday

    def _reg(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def test_ages_old_parked_capture(self):
        old = (self.now - timedelta(days=14)).isoformat()
        reg = self._reg({'id': 'cap-a', 'state': 'parked', 'last_touched': old})
        newly = h.age_parked_captures(reg, self.now)
        self.assertEqual(newly, ['cap-a'])
        self.assertTrue(reg['captures'][0]['aging'])

    def test_recent_capture_not_aged(self):
        recent = (self.now - timedelta(days=1)).isoformat()
        reg = self._reg({'id': 'cap-a', 'state': 'parked', 'last_touched': recent})
        self.assertEqual(h.age_parked_captures(reg, self.now), [])
        self.assertNotIn('aging', reg['captures'][0])

    def test_already_aging_is_idempotent(self):
        old = (self.now - timedelta(days=30)).isoformat()
        reg = self._reg({'id': 'cap-a', 'state': 'parked',
                         'last_touched': old, 'aging': True})
        self.assertEqual(h.age_parked_captures(reg, self.now), [])

    def test_non_parked_states_ignored(self):
        old = (self.now - timedelta(days=30)).isoformat()
        reg = self._reg(
            {'id': 'cap-p', 'state': 'promoted', 'last_touched': old},
            {'id': 'cap-d', 'state': 'dropped', 'last_touched': old},
        )
        self.assertEqual(h.age_parked_captures(reg, self.now), [])

    def test_never_deletes_and_preserves_fields(self):
        old = (self.now - timedelta(days=30)).isoformat()
        reg = self._reg({'id': 'cap-a', 'state': 'parked', 'last_touched': old,
                         'title': 'T', 'note': 'N', 'promoted_to': None})
        h.age_parked_captures(reg, self.now)
        self.assertEqual(len(reg['captures']), 1)
        cap = reg['captures'][0]
        self.assertEqual(cap['title'], 'T')
        self.assertEqual(cap['note'], 'N')
        self.assertEqual(cap['state'], 'parked')  # state untouched
        self.assertIsNone(cap['promoted_to'])


# ---------------------------------------------- captures.json read / write


class CaptureIoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cap-io-test-')
        self.path = Path(self.tmp) / 'captures.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty_registry(self):
        reg = h.read_captures_registry(self.path)
        self.assertEqual(reg, {'schema_version': 1, 'captures': []})

    def test_malformed_file_returns_none(self):
        self.path.write_text('{not json')
        self.assertIsNone(h.read_captures_registry(self.path))

    def test_wrong_shape_returns_none(self):
        self.path.write_text(json.dumps({'captures': 'not-a-list'}))
        self.assertIsNone(h.read_captures_registry(self.path))

    def test_atomic_write_round_trip(self):
        reg = {'schema_version': 1, 'captures': [{'id': 'cap-a'}]}
        h.atomic_write_captures(self.path, reg)
        self.assertEqual(json.loads(self.path.read_text()), reg)
        # No stray tmp files left behind.
        leftovers = [p for p in os.listdir(self.tmp) if p != 'captures.json']
        self.assertEqual(leftovers, [])


# ---------------------------------------- §3.3 missions phase reconcile


class ReadMissionsRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='missions-io-test-')
        self.path = Path(self.tmp) / 'missions.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty_registry(self):
        self.assertEqual(
            h.read_missions_registry(self.path),
            {'schema_version': 1, 'missions': []})

    def test_malformed_file_returns_none(self):
        self.path.write_text('{not json')
        self.assertIsNone(h.read_missions_registry(self.path))

    def test_wrong_shape_returns_none(self):
        self.path.write_text(json.dumps({'missions': 'not-a-list'}))
        self.assertIsNone(h.read_missions_registry(self.path))


class ClassifyMissionTest(unittest.TestCase):
    """Pure decision logic — the conservative guard at the unit level."""

    def test_all_tasks_merged_ships(self):
        states = {'a': h.tts.MERGED, 'b': h.tts.MERGED}
        self.assertEqual(
            h.classify_mission('in_flight', ['a', 'b'], states).action, 'ship')

    def test_closed_unmerged_retires(self):
        # PR-3 R2: all tasks terminal but one CLOSED-unmerged (abandoned) ⇒
        # retire, never ship — abandoned work must not be recorded as shipped.
        states = {'a': h.tts.MERGED, 'b': h.tts.CLOSED}
        self.assertEqual(
            h.classify_mission('in_flight', ['a', 'b'], states).action, 'retire')

    def test_one_open_task_keeps(self):
        states = {'a': h.tts.MERGED, 'b': h.tts.OPEN}
        self.assertEqual(
            h.classify_mission('in_flight', ['a', 'b'], states).action, 'keep')

    def test_unknown_task_keeps(self):
        states = {'a': h.tts.MERGED, 'b': h.tts.UNKNOWN}
        self.assertEqual(
            h.classify_mission('ready', ['a', 'b'], states).action, 'keep')

    def test_missing_task_state_keeps(self):
        # A task_id absent from the probe map counts as non-terminal.
        self.assertEqual(
            h.classify_mission('drafting', ['a', 'b'], {'a': h.tts.MERGED}).action,
            'keep')

    def test_non_reconcilable_phase_keeps(self):
        states = {'a': h.tts.MERGED}
        for phase in ('proposed', 'deferred', 'shipped'):
            self.assertEqual(
                h.classify_mission(phase, ['a'], states).action, 'keep',
                f'phase={phase} must never be auto-shipped')

    def test_empty_task_ids_keeps(self):
        # Vacuous-truth guard: no tasks ⇒ not "all terminal".
        self.assertEqual(h.classify_mission('ready', [], {}).action, 'keep')

    def test_review_shaped_ids_ignored_in_terminal_gate(self):
        # A non-terminal review-shaped id (dag-preflight-/notify-/review-) must
        # NOT block shipping when the PR-backed ids are all terminal.
        states = {'real-pr-task': h.tts.MERGED}
        for review_id in ('dag-preflight-clarify-x', 'notify-foo', 'review-bar'):
            self.assertEqual(
                h.classify_mission(
                    'ready', ['real-pr-task', review_id], states).action,
                'ship',
                f'review id {review_id} must not block ship')

    def test_unmerged_pr_backed_task_keeps_despite_review_id(self):
        # No false-ship: a genuinely-unmerged PR-backed id keeps the mission
        # even when every other id is an (ignored) review id.
        states = {'real-pr-task': h.tts.OPEN}
        self.assertEqual(
            h.classify_mission(
                'ready', ['real-pr-task', 'dag-preflight-z'], states).action,
            'keep')

    def test_only_review_shaped_ids_keeps(self):
        # All ids review-shaped ⇒ no probeable task ⇒ keep (flagged downstream).
        decision = h.classify_mission(
            'ready', ['dag-preflight-z', 'review-y'], {})
        self.assertEqual(decision.action, 'keep')
        self.assertEqual(decision.reason, 'no probeable task_ids')


class ReviewShapedTaskIdTest(unittest.TestCase):
    def test_predicate_matches_known_prefixes(self):
        for tid in ('dag-preflight-x', 'notify-y', 'review-z'):
            self.assertTrue(h.is_review_shaped_task_id(tid))

    def test_predicate_rejects_pr_backed_and_nonstrings(self):
        for tid in ('clarify-shipper-extend', 'pulse-cycle-alpha-1', '', None, 7):
            self.assertFalse(h.is_review_shaped_task_id(tid))

    def test_probeable_filters_review_and_blanks(self):
        ids = ['real-a', 'dag-preflight-x', 'notify-y', '', 'real-b', 5]
        self.assertEqual(h.probeable_task_ids(ids), ['real-a', 'real-b'])


class ReconcileMissionPhasesTest(unittest.TestCase):
    """terminal⇒ship AND live/indeterminate⇒keep (spec §6 conservative guard)."""

    def _now(self):
        return datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)

    def _registry(self):
        return {'schema_version': 1, 'missions': [
            {'id': 'm-shipped', 'phase': 'in_flight', 'task_ids': ['t1', 't2']},
            {'id': 'm-live', 'phase': 'ready', 'task_ids': ['t3', 't4']},
            {'id': 'm-proposed', 'phase': 'proposed', 'task_ids': ['t5']},
        ]}

    def test_merged_mission_ships_others_kept(self):
        reg = self._registry()
        terminal = {'t1': h.tts.MERGED, 't2': h.tts.MERGED}
        probe = lambda tid: terminal.get(tid, h.tts.OPEN)
        res = h.reconcile_mission_phases(reg, self._now(), probe_fn=probe, dry_run=False)
        by_id = {m['id']: m for m in reg['missions']}
        # terminal ⇒ shipped, with audit provenance preserved.
        self.assertEqual(by_id['m-shipped']['phase'], 'shipped')
        self.assertEqual(by_id['m-shipped']['prior_phase'], 'in_flight')
        self.assertEqual(by_id['m-shipped']['shipped_by'], 'heal_missions_card_gc')
        self.assertIn('shipped_at', by_id['m-shipped'])
        # live (t3 OPEN) ⇒ kept untouched.
        self.assertEqual(by_id['m-live']['phase'], 'ready')
        self.assertNotIn('prior_phase', by_id['m-live'])
        # proposed lane never touched (owned by another PR).
        self.assertEqual(by_id['m-proposed']['phase'], 'proposed')
        self.assertEqual([s[0] for s in res.shipped], ['m-shipped'])

    def test_unknown_probe_keeps_all(self):
        reg = self._registry()
        res = h.reconcile_mission_phases(
            reg, self._now(), probe_fn=lambda tid: h.tts.UNKNOWN, dry_run=False)
        self.assertEqual(res.shipped, [])
        self.assertTrue(all(m['phase'] != 'shipped' for m in reg['missions']))

    def test_dry_run_does_not_mutate(self):
        reg = self._registry()
        res = h.reconcile_mission_phases(
            reg, self._now(), probe_fn=lambda tid: h.tts.MERGED, dry_run=True)
        # Reported as would-ship, but the registry is untouched.
        self.assertTrue(res.shipped)
        by_id = {m['id']: m for m in reg['missions']}
        self.assertEqual(by_id['m-shipped']['phase'], 'in_flight')
        self.assertNotIn('shipped_at', by_id['m-shipped'])

    def test_review_id_does_not_block_ship_and_is_not_probed(self):
        # A reconcilable mission whose only non-terminal id is review-shaped
        # ships once its PR-backed id is terminal — and the review id is never
        # probed (a permanently-unsatisfiable id can't block shipped-detection).
        reg = {'schema_version': 1, 'missions': [
            {'id': 'm-rev', 'phase': 'ready',
             'task_ids': ['real-pr', 'dag-preflight-real']},
        ]}
        probed: list[str] = []

        def probe(tid):
            probed.append(tid)
            return h.tts.MERGED if tid == 'real-pr' else h.tts.OPEN
        res = h.reconcile_mission_phases(
            reg, self._now(), probe_fn=probe, dry_run=False)
        self.assertEqual([s[0] for s in res.shipped], ['m-rev'])
        self.assertEqual(reg['missions'][0]['phase'], 'shipped')
        self.assertEqual(reg['missions'][0]['prior_phase'], 'ready')
        self.assertEqual(probed, ['real-pr'])  # review id never probed

    def test_real_unmerged_pr_backed_task_does_not_ship(self):
        # No false-ship regression: a genuine OPEN PR-backed id keeps the
        # mission even though the other id is an (ignored) review id.
        reg = {'schema_version': 1, 'missions': [
            {'id': 'm-live', 'phase': 'ready',
             'task_ids': ['real-pr', 'review-x']},
        ]}
        res = h.reconcile_mission_phases(
            reg, self._now(), probe_fn=lambda tid: h.tts.OPEN, dry_run=False)
        self.assertEqual(res.shipped, [])
        self.assertEqual(reg['missions'][0]['phase'], 'ready')

    def test_empty_probeable_flagged_past_grace_not_shipped(self):
        # A reconcilable mission with no probeable task_id (empty, or only
        # review-shaped ids) that has lingered past the grace is flagged for
        # manual reconcile — never auto-shipped.
        old = (self._now()
               - timedelta(days=h.EMPTY_PROBEABLE_MISSION_GRACE_DAYS + 1))
        reg = {'schema_version': 1, 'missions': [
            {'id': 'm-empty', 'phase': 'drafting', 'task_ids': [],
             'created': old.isoformat()},
            {'id': 'm-review-only', 'phase': 'ready',
             'task_ids': ['dag-preflight-y'], 'created': old.isoformat()},
        ]}
        res = h.reconcile_mission_phases(
            reg, self._now(), probe_fn=lambda tid: h.tts.MERGED, dry_run=False)
        self.assertEqual(res.shipped, [])
        flagged_ids = sorted(mid for mid, _ in res.flagged)
        self.assertEqual(flagged_ids, ['m-empty', 'm-review-only'])
        self.assertTrue(all(m['phase'] != 'shipped' for m in reg['missions']))

    def test_empty_probeable_within_grace_not_flagged(self):
        # Same shape but freshly created (within grace) ⇒ kept silently, not
        # flagged: a young mission with no probeable task isn't stale yet.
        recent = self._now() - timedelta(days=1)
        reg = {'schema_version': 1, 'missions': [
            {'id': 'm-fresh', 'phase': 'drafting', 'task_ids': [],
             'created': recent.isoformat()},
        ]}
        res = h.reconcile_mission_phases(
            reg, self._now(), probe_fn=lambda tid: h.tts.MERGED, dry_run=False)
        self.assertEqual(res.flagged, [])
        self.assertEqual(res.shipped, [])


# ----------------------------------- Phase S (S3) completion reconcile


class PrNoteHelpersTest(unittest.TestCase):
    def test_pr_number_from_url(self):
        self.assertEqual(h.pr_number_from_url('https://github.com/o/r/pull/541'), '541')
        self.assertIsNone(h.pr_number_from_url('https://github.com/o/r/issues/9'))
        self.assertIsNone(h.pr_number_from_url(None))

    def test_shipped_note(self):
        self.assertEqual(h.shipped_note('https://github.com/o/r/pull/541'),
                         'shipped in PR #541')
        # Degrades to a PR-less phrasing when unparseable.
        self.assertEqual(h.shipped_note(None), 'shipped (linked work merged)')

    def test_pr_url_from_events_picks_first_nonnull(self):
        events = [
            {'event_type': 'a', 'pr_url': None},
            {'event_type': 'b', 'pr_url': 'https://github.com/o/r/pull/7'},
            {'event_type': 'c', 'pr_url': 'https://github.com/o/r/pull/8'},
        ]
        self.assertEqual(h._pr_url_from_events(events),
                         'https://github.com/o/r/pull/7')
        self.assertIsNone(h._pr_url_from_events([{'event_type': 'x'}]))


class ClassifyCompletionTest(unittest.TestCase):
    def test_not_verified_keeps(self):
        d = h.classify_completion({'risk': 'safe'}, verified_merged=False)
        self.assertEqual(d.action, 'keep')

    def test_safe_verified_auto_closes(self):
        d = h.classify_completion({'risk': 'safe'}, verified_merged=True)
        self.assertEqual(d.action, 'auto_close')

    def test_medium_verified_closeouts(self):
        d = h.classify_completion({'risk': 'medium'}, verified_merged=True)
        self.assertEqual(d.action, 'closeout')

    def test_careful_verified_closeouts(self):
        d = h.classify_completion({'risk': 'careful'}, verified_merged=True)
        self.assertEqual(d.action, 'closeout')

    def test_unbriefed_verified_closeouts_fail_toward_caution(self):
        # No risk field yet ⇒ route to closeout (Larry review), never auto-close.
        d = h.classify_completion({}, verified_merged=True)
        self.assertEqual(d.action, 'closeout')


class ReconcileCompletedCardsTest(unittest.TestCase):
    """safe⇒done auto-close, risky⇒review_close closeout, gate-mismatch⇒keep,
    verify/author error⇒keep, idempotent on closed cards, dry-run no-mutate."""

    def _now(self):
        return datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)

    def _cap(self, cid, risk, **over):
        cap = {
            'id': cid, 'state': 'parked', 'risk': risk,
            'title': f'card {cid}',
            'spawned': {'kind': 'delegate', 'task_id': f'delegate-{cid}',
                        'stamped_at': '2026-06-15T00:00:00+00:00'},
        }
        cap.update(over)
        return cap

    def _registry(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def _verify_merged(self, pr_url):
        return lambda task_id, dispatched_at: (True, pr_url)

    def _closeout_stub(self, cap, pr_url, now):
        return {'closeout': {'what': 'w', 'outcome': 'o', 'note': 'n'},
                'closeout_provenance': {'by': 'beacon', 'pr_url': pr_url}}

    def test_safe_card_auto_closes_with_shipped_note(self):
        reg = self._registry(self._cap('c1', 'safe', aging=True))
        res = h.reconcile_completed_cards(
            reg, self._now(),
            verify_fn=self._verify_merged('https://github.com/o/r/pull/541'),
            closeout_fn=self._closeout_stub, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'done')
        self.assertEqual(cap['shipped_note'], 'shipped in PR #541')
        self.assertEqual(cap['shipped_pr_url'], 'https://github.com/o/r/pull/541')
        self.assertEqual(cap['closed_by'], 'heal_missions_card_gc')
        self.assertIn('closed_at', cap)
        self.assertNotIn('aging', cap)  # a closed card is no longer an aging nudge
        self.assertEqual([cid for cid, _ in res.closed], ['c1'])
        self.assertTrue(res.changed)

    def test_medium_card_routes_to_closeout_review(self):
        reg = self._registry(self._cap('c2', 'medium'))
        res = h.reconcile_completed_cards(
            reg, self._now(),
            verify_fn=self._verify_merged('https://github.com/o/r/pull/9'),
            closeout_fn=self._closeout_stub, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'review_close')
        self.assertTrue(cap['awaiting_ack'])
        self.assertEqual(cap['closeout'], {'what': 'w', 'outcome': 'o', 'note': 'n'})
        self.assertEqual(cap['shipped_pr_url'], 'https://github.com/o/r/pull/9')
        self.assertEqual(res.closeouts, ['c2'])

    def test_unbriefed_card_routes_to_closeout(self):
        cap = self._cap('c3', None)
        cap.pop('risk')
        reg = self._registry(cap)
        h.reconcile_completed_cards(
            reg, self._now(),
            verify_fn=self._verify_merged('https://github.com/o/r/pull/3'),
            closeout_fn=self._closeout_stub, dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'review_close')

    def test_gate_mismatch_keeps_card_parked(self):
        # One leg of the belt-and-suspenders gate fails ⇒ verify_fn returns
        # (False, pr_url); the card stays parked for a retry next tick.
        reg = self._registry(self._cap('c4', 'safe'))
        res = h.reconcile_completed_cards(
            reg, self._now(),
            verify_fn=lambda t, d: (False, 'https://github.com/o/r/pull/4'),
            closeout_fn=self._closeout_stub, dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertEqual(res.closed, [])
        self.assertFalse(res.changed)
        self.assertEqual(res.kept, 1)

    def test_verify_error_keeps_card(self):
        def _boom(task_id, dispatched_at):
            raise RuntimeError('supabase down')
        reg = self._registry(self._cap('c5', 'safe'))
        res = h.reconcile_completed_cards(
            reg, self._now(), verify_fn=_boom,
            closeout_fn=self._closeout_stub, dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertEqual(res.kept, 1)

    def test_closeout_author_error_keeps_card(self):
        def _boom(cap, pr_url, now):
            raise RuntimeError('claude down')
        reg = self._registry(self._cap('c6', 'careful'))
        res = h.reconcile_completed_cards(
            reg, self._now(),
            verify_fn=self._verify_merged('https://github.com/o/r/pull/6'),
            closeout_fn=_boom, dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertEqual(res.kept, 1)

    def test_non_parked_and_no_spawn_are_skipped(self):
        already_done = self._cap('c7', 'safe', state='done')
        no_spawn = {'id': 'c8', 'state': 'parked', 'risk': 'safe'}
        reg = self._registry(already_done, no_spawn)
        res = h.reconcile_completed_cards(
            reg, self._now(),
            verify_fn=self._verify_merged('https://github.com/o/r/pull/7'),
            closeout_fn=self._closeout_stub, dry_run=False)
        # Idempotent: a done card is untouched; a card with no spawned.task_id
        # is not a completion candidate.
        self.assertEqual(reg['captures'][0]['state'], 'done')
        self.assertEqual(reg['captures'][1]['state'], 'parked')
        self.assertFalse(res.changed)

    def test_dry_run_does_not_mutate(self):
        reg = self._registry(self._cap('c9', 'safe'), self._cap('c10', 'medium'))
        res = h.reconcile_completed_cards(
            reg, self._now(),
            verify_fn=self._verify_merged('https://github.com/o/r/pull/9'),
            closeout_fn=self._closeout_stub, dry_run=True)
        # Reported as would-close, but both captures stay parked.
        self.assertTrue(res.changed)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertEqual(reg['captures'][1]['state'], 'parked')
        self.assertNotIn('closeout', reg['captures'][1])


# ---------------------------------------- S4: failure rings back (§ 3 S4 / § 9)


class CardDeepLinkTest(unittest.TestCase):
    def test_deep_link_targets_the_card(self):
        self.assertEqual(
            h.card_deep_link('c1'),
            'https://dashboard.ourliberty.dev/missions?card=c1')


class ReconcileFailedCardsTest(unittest.TestCase):
    def _now(self):
        return datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)

    def _cap(self, cid, risk, **over):
        cap = {
            'id': cid, 'state': 'parked', 'risk': risk,
            'title': f'card {cid}',
            'spawned': {'kind': 'delegate', 'task_id': f'delegate-{cid}',
                        'stamped_at': '2026-06-15T00:00:00+00:00'},
        }
        cap.update(over)
        return cap

    def _registry(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def test_failed_card_rings_loud_and_stamps_but_stays_parked(self):
        reg = self._registry(self._cap('c1', 'careful'))
        ring = mock.Mock(return_value={'rung': True})
        res = h.reconcile_failed_cards(
            reg, self._now(),
            failure_fn=lambda t: 'forge_reject: tests broke',
            ring_fn=ring, dry_run=False)
        cap = reg['captures'][0]
        # card surfaces for Larry — it does NOT close.
        self.assertEqual(cap['state'], 'parked')
        self.assertEqual(cap['failure_signaled']['reason'], 'forge_reject: tests broke')
        self.assertEqual(cap['failure_signaled']['by'], h.COMPLETED_BY)
        self.assertIn('at', cap['failure_signaled'])
        self.assertEqual(res.rung, [('c1', 'forge_reject: tests broke')])
        self.assertTrue(res.changed)
        # ring is loud blocked-on-you, carrying the plain-English reason + deep link.
        _, kw = ring.call_args
        self.assertEqual(kw['capture_id'], 'c1')
        self.assertTrue(kw['blocked'])
        self.assertEqual(kw['risk'], 'careful')
        self.assertEqual(kw['title'], 'card c1')
        self.assertEqual(kw['detail'], 'forge_reject: tests broke')
        self.assertEqual(kw['deep_link'],
                         'https://dashboard.ourliberty.dev/missions?card=c1')

    def test_healthy_card_is_not_rung(self):
        reg = self._registry(self._cap('c2', 'safe'))
        ring = mock.Mock()
        res = h.reconcile_failed_cards(
            reg, self._now(), failure_fn=lambda t: None,
            ring_fn=ring, dry_run=False)
        ring.assert_not_called()
        self.assertNotIn('failure_signaled', reg['captures'][0])
        self.assertFalse(res.changed)
        self.assertEqual(res.kept, 1)

    def test_already_signaled_card_is_skipped(self):
        cap = self._cap('c3', 'safe', failure_signaled={'reason': 'x', 'at': 'y'})
        reg = self._registry(cap)
        failure_fn = mock.Mock()
        ring = mock.Mock()
        res = h.reconcile_failed_cards(
            reg, self._now(), failure_fn=failure_fn, ring_fn=ring, dry_run=False)
        failure_fn.assert_not_called()
        ring.assert_not_called()
        self.assertFalse(res.changed)

    def test_non_parked_and_no_spawn_are_skipped(self):
        done = self._cap('c4', 'safe', state='done')
        no_spawn = {'id': 'c5', 'state': 'parked', 'risk': 'safe'}
        reg = self._registry(done, no_spawn)
        failure_fn = mock.Mock(return_value='boom')
        ring = mock.Mock()
        res = h.reconcile_failed_cards(
            reg, self._now(), failure_fn=failure_fn, ring_fn=ring, dry_run=False)
        failure_fn.assert_not_called()
        ring.assert_not_called()
        self.assertFalse(res.changed)

    def test_detect_error_keeps_card_unstamped(self):
        def _boom(task_id):
            raise RuntimeError('supabase down')
        reg = self._registry(self._cap('c6', 'safe'))
        res = h.reconcile_failed_cards(
            reg, self._now(), failure_fn=_boom,
            ring_fn=mock.Mock(), dry_run=False)
        self.assertNotIn('failure_signaled', reg['captures'][0])
        self.assertEqual(res.kept, 1)
        self.assertFalse(res.changed)

    def test_ring_error_keeps_card_unstamped_for_retry(self):
        def _boom(**kw):
            raise RuntimeError('alerts down')
        reg = self._registry(self._cap('c7', 'safe'))
        res = h.reconcile_failed_cards(
            reg, self._now(), failure_fn=lambda t: 'mirror_emergency_halt: stuck',
            ring_fn=_boom, dry_run=False)
        self.assertNotIn('failure_signaled', reg['captures'][0])
        self.assertEqual(res.kept, 1)

    def test_dry_run_reports_without_ringing_or_stamping(self):
        reg = self._registry(self._cap('c8', 'careful'))
        ring = mock.Mock()
        res = h.reconcile_failed_cards(
            reg, self._now(), failure_fn=lambda t: 'forge_reject: nope',
            ring_fn=ring, dry_run=True)
        ring.assert_not_called()
        self.assertNotIn('failure_signaled', reg['captures'][0])
        self.assertTrue(res.changed)
        self.assertIn('(dry-run)', res.rung[0][1])


# ------------------------------ G3: terminal-state backstop (completeness-pr2)


class ReconcileTerminalCapturesTest(unittest.TestCase):
    """The task_terminal_state backstop that catches the terminal cases S3/S4
    miss: MERGED-without-auto_merge-event ⇒ complete-by-risk; CLOSED-unmerged ⇒
    ring + keep parked; UNKNOWN/OPEN ⇒ KEEP (the failed-delegate shape). Stamps
    spawned.outcome by replacing the top-level key (lost-update-safe)."""

    def _now(self):
        return datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)

    def _cap(self, cid, risk, **over):
        cap = {
            'id': cid, 'state': 'parked', 'risk': risk,
            'title': f'card {cid}',
            'spawned': {'kind': 'delegate', 'task_id': f'delegate-{cid}',
                        'stamped_at': '2026-07-01T00:00:00+00:00'},
        }
        cap.update(over)
        return cap

    def _registry(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def test_merged_safe_card_auto_closes_to_done(self):
        reg = self._registry(self._cap('c1', 'safe', aging=True))
        res = h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.MERGED, ring_fn=mock.Mock(), dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'done')
        self.assertEqual(cap['spawned']['outcome'], 'merged')
        self.assertEqual(cap['closed_by'], h.TERMINAL_BACKSTOP_BY)
        self.assertEqual(cap['shipped_note'], 'shipped (linked work merged)')
        self.assertIn('closed_at', cap)
        self.assertNotIn('aging', cap)
        self.assertEqual([cid for cid, _ in res.completed], ['c1'])
        self.assertTrue(res.changed)

    def test_merged_risky_card_routes_to_review_close(self):
        reg = self._registry(self._cap('c2', 'careful'))
        ring = mock.Mock()
        res = h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.MERGED, ring_fn=ring, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'review_close')
        self.assertTrue(cap['awaiting_ack'])
        self.assertEqual(cap['spawned']['outcome'], 'merged')
        self.assertEqual(res.closeouts, ['c2'])
        ring.assert_not_called()

    def test_merged_replaces_spawned_top_level_not_in_place(self):
        # The lost-update delta merge diffs top-level keys against a shallow
        # snapshot; an in-place nested mutation would be invisible. Assert we
        # rebind a NEW spawned dict so the stamp is a detectable top-level delta.
        cap = self._cap('c3', 'safe')
        original_spawned = cap['spawned']
        reg = self._registry(cap)
        h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.MERGED, ring_fn=mock.Mock(), dry_run=False)
        self.assertIsNot(reg['captures'][0]['spawned'], original_spawned)
        self.assertNotIn('outcome', original_spawned)

    def test_closed_card_rings_and_stays_parked(self):
        reg = self._registry(self._cap('c4', 'medium'))
        ring = mock.Mock(return_value={'rung': True})
        res = h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.CLOSED, ring_fn=ring, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'parked')  # surfaces, never auto-closes
        self.assertEqual(cap['spawned']['outcome'], 'closed')
        self.assertEqual(cap['failure_signaled']['by'], h.TERMINAL_BACKSTOP_BY)
        self.assertIn('closed without merging', cap['failure_signaled']['reason'])
        self.assertEqual([cid for cid, _ in res.closed_failed], ['c4'])
        self.assertTrue(res.changed)
        _, kw = ring.call_args
        self.assertEqual(kw['capture_id'], 'c4')
        self.assertTrue(kw['blocked'])
        self.assertEqual(kw['risk'], 'medium')
        self.assertEqual(kw['deep_link'],
                         'https://dashboard.ourliberty.dev/missions?card=c4')

    def test_unknown_probe_keeps_card_open(self):
        # The failed-delegate shape: no PR ever opened ⇒ UNKNOWN ⇒ KEEP.
        reg = self._registry(self._cap('c5', 'safe'))
        ring = mock.Mock()
        res = h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.UNKNOWN, ring_fn=ring, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'parked')
        self.assertNotIn('outcome', cap['spawned'])
        self.assertNotIn('failure_signaled', cap)
        ring.assert_not_called()
        self.assertFalse(res.changed)
        self.assertEqual(res.kept, 1)

    def test_open_probe_keeps_card(self):
        reg = self._registry(self._cap('c6', 'safe'))
        res = h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.OPEN, ring_fn=mock.Mock(), dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertFalse(res.changed)
        self.assertEqual(res.kept, 1)

    def test_already_stamped_outcome_is_skipped(self):
        cap = self._cap('c7', 'safe')
        cap['spawned']['outcome'] = 'merged'
        reg = self._registry(cap)
        probe = mock.Mock(return_value=h.tts.MERGED)
        res = h.reconcile_terminal_captures(
            reg, self._now(), terminal_fn=probe, ring_fn=mock.Mock(), dry_run=False)
        probe.assert_not_called()  # idempotent: no re-probe of a stamped card
        self.assertFalse(res.changed)

    def test_failure_signaled_card_left_to_s4(self):
        cap = self._cap('c8', 'safe', failure_signaled={'reason': 'x', 'at': 'y'})
        reg = self._registry(cap)
        probe = mock.Mock(return_value=h.tts.MERGED)
        res = h.reconcile_terminal_captures(
            reg, self._now(), terminal_fn=probe, ring_fn=mock.Mock(), dry_run=False)
        probe.assert_not_called()
        self.assertFalse(res.changed)

    def test_non_parked_and_no_spawn_are_skipped(self):
        done = self._cap('c9', 'safe', state='done')
        no_spawn = {'id': 'c10', 'state': 'parked', 'risk': 'safe'}
        reg = self._registry(done, no_spawn)
        probe = mock.Mock(return_value=h.tts.MERGED)
        res = h.reconcile_terminal_captures(
            reg, self._now(), terminal_fn=probe, ring_fn=mock.Mock(), dry_run=False)
        probe.assert_not_called()
        self.assertFalse(res.changed)

    def test_probe_error_keeps_card(self):
        def _boom(task_id):
            raise RuntimeError('gh down')
        reg = self._registry(self._cap('c11', 'safe'))
        res = h.reconcile_terminal_captures(
            reg, self._now(), terminal_fn=_boom, ring_fn=mock.Mock(), dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertNotIn('outcome', reg['captures'][0]['spawned'])
        self.assertEqual(res.kept, 1)
        self.assertFalse(res.changed)

    def test_ring_error_keeps_card_unstamped(self):
        def _boom(**kw):
            raise RuntimeError('alerts down')
        reg = self._registry(self._cap('c12', 'safe'))
        res = h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.CLOSED, ring_fn=_boom, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'parked')
        self.assertNotIn('outcome', cap['spawned'])
        self.assertNotIn('failure_signaled', cap)
        self.assertEqual(res.kept, 1)

    def test_dry_run_does_not_mutate(self):
        reg = self._registry(self._cap('c13', 'safe'), self._cap('c14', 'medium'))
        res = h.reconcile_terminal_captures(
            reg, self._now(),
            terminal_fn=lambda t: h.tts.MERGED, ring_fn=mock.Mock(), dry_run=True)
        self.assertTrue(res.changed)
        for cap in reg['captures']:
            self.assertEqual(cap['state'], 'parked')
            self.assertNotIn('outcome', cap['spawned'])

    def test_default_terminal_fn_wraps_shared_probe(self):
        with mock.patch.object(h.tts, 'task_terminal_state',
                               return_value=h.tts.MERGED) as probe:
            fn = h._default_terminal_fn()
            self.assertEqual(fn('some-task'), h.tts.MERGED)
        probe.assert_called_once_with('some-task')


# ---------------------------------------- S7: in-flight overrules a late pause


class ApplyPendingActionTest(unittest.TestCase):
    def _now(self):
        return datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)

    def test_drop_sets_dropped_state_and_reason(self):
        cap = {'id': 'c1', 'state': 'parked',
               'pending_action': {'action': 'drop', 'args': {'reason': 'stale'}}}
        action = h.apply_pending_action(cap, self._now())
        self.assertEqual(action, 'drop')
        self.assertEqual(cap['state'], 'dropped')
        self.assertEqual(cap['drop_reason'], 'stale')
        self.assertNotIn('pending_action', cap)
        self.assertEqual(cap['pending_action_applied']['action'], 'drop')

    def test_snooze_sets_snoozed_until(self):
        cap = {'id': 'c2', 'state': 'parked',
               'pending_action': {'action': 'snooze',
                                  'args': {'snoozed_until': '2026-07-01'}}}
        action = h.apply_pending_action(cap, self._now())
        self.assertEqual(action, 'snooze')
        self.assertEqual(cap['snoozed_until'], '2026-07-01')
        self.assertNotIn('pending_action', cap)

    def test_unknown_action_returns_none_and_does_not_mutate(self):
        cap = {'id': 'c3', 'state': 'parked',
               'pending_action': {'action': 'promote', 'args': {}}}
        self.assertIsNone(h.apply_pending_action(cap, self._now()))
        self.assertEqual(cap['state'], 'parked')
        self.assertIn('pending_action', cap)

    def test_no_pending_returns_none(self):
        cap = {'id': 'c4', 'state': 'parked'}
        self.assertIsNone(h.apply_pending_action(cap, self._now()))


class ReconcileDeferredActionsTest(unittest.TestCase):
    def _now(self):
        return datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)

    def _cap(self, cid, action, *, args=None, task_id=f'delegate-x', **over):
        cap = {
            'id': cid, 'state': 'parked',
            'pending_action': {'action': action, 'args': args or {}},
        }
        if task_id is not None:
            cap['spawned'] = {'kind': 'delegate', 'task_id': task_id}
        cap.update(over)
        return cap

    def _registry(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def test_in_flight_keeps_pending_uninterrupted(self):
        reg = self._registry(self._cap('c1', 'drop', task_id='delegate-c1'))
        res = h.reconcile_deferred_actions(
            reg, self._now(), in_flight_fn=lambda t: True, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'parked')  # never interrupted
        self.assertIn('pending_action', cap)
        self.assertFalse(res.changed)
        self.assertEqual(res.kept, 1)

    def test_safe_stop_applies_drop(self):
        reg = self._registry(
            self._cap('c2', 'drop', args={'reason': 'obsolete'}, task_id='delegate-c2'))
        res = h.reconcile_deferred_actions(
            reg, self._now(), in_flight_fn=lambda t: False, dry_run=False)
        cap = reg['captures'][0]
        self.assertEqual(cap['state'], 'dropped')
        self.assertEqual(cap['drop_reason'], 'obsolete')
        self.assertEqual(res.applied, [('c2', 'drop')])
        self.assertTrue(res.changed)

    def test_safe_stop_applies_snooze(self):
        reg = self._registry(
            self._cap('c3', 'snooze', args={'snoozed_until': '2026-07-01'},
                      task_id='delegate-c3'))
        res = h.reconcile_deferred_actions(
            reg, self._now(), in_flight_fn=lambda t: False, dry_run=False)
        self.assertEqual(reg['captures'][0]['snoozed_until'], '2026-07-01')
        self.assertEqual(res.applied, [('c3', 'snooze')])

    def test_no_task_id_applies_immediately_without_probing(self):
        reg = self._registry(self._cap('c4', 'drop', task_id=None))
        probe = mock.Mock()
        res = h.reconcile_deferred_actions(
            reg, self._now(), in_flight_fn=probe, dry_run=False)
        probe.assert_not_called()
        self.assertEqual(reg['captures'][0]['state'], 'dropped')
        self.assertEqual(res.applied, [('c4', 'drop')])

    def test_probe_error_keeps_pending(self):
        def _boom(task_id):
            raise RuntimeError('events down')
        reg = self._registry(self._cap('c5', 'drop', task_id='delegate-c5'))
        res = h.reconcile_deferred_actions(
            reg, self._now(), in_flight_fn=_boom, dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertIn('pending_action', reg['captures'][0])
        self.assertEqual(res.kept, 1)

    def test_dry_run_reports_without_applying(self):
        reg = self._registry(self._cap('c6', 'drop', task_id='delegate-c6'))
        res = h.reconcile_deferred_actions(
            reg, self._now(), in_flight_fn=lambda t: False, dry_run=True)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertIn('pending_action', reg['captures'][0])
        self.assertTrue(res.changed)
        self.assertIn('(dry-run)', res.applied[0][1])

    def test_non_deferred_pending_is_skipped(self):
        reg = self._registry(self._cap('c7', 'promote', task_id='delegate-c7'))
        res = h.reconcile_deferred_actions(
            reg, self._now(), in_flight_fn=lambda t: False, dry_run=False)
        self.assertEqual(reg['captures'][0]['state'], 'parked')
        self.assertIn('pending_action', reg['captures'][0])
        self.assertFalse(res.changed)
        self.assertEqual(res.kept, 0)


# ---------------------------------------- _default_in_flight_fn (S4<->S7 gate)


class DefaultInFlightFnTest(unittest.TestCase):
    """The production in-flight probe must treat a detected terminal failure as a
    safe stop (S4<->S7) so a deferred pause/drop on work that later fails applies
    instead of deferring forever (failed work derives in_flight/awaiting_merge
    permanently)."""

    def test_failure_is_safe_stop_short_circuits_before_fetch(self):
        import build_sequence_advancer as bsa
        import chain_event_emit
        with mock.patch.object(chain_event_emit, '_get_client', return_value=object()), \
                mock.patch.object(bsa, 'chain_event_says_failed',
                                  return_value='forge_reject: tests broke'), \
                mock.patch.object(h, '_fetch_task_events') as fetch:
            fn = h._default_in_flight_fn()
            self.assertFalse(fn('delegate-x'))
            fetch.assert_not_called()

    def test_in_flight_when_not_failed(self):
        import build_sequence_advancer as bsa
        import chain_event_emit
        events = [{'task_id': 'delegate-x', 'event_type': 'session_start',
                   'agent': 'forge', 'pr_url': None,
                   'ts': '2026-06-15T10:00:00+00:00', 'payload': {}}]
        with mock.patch.object(chain_event_emit, '_get_client', return_value=object()), \
                mock.patch.object(bsa, 'chain_event_says_failed', return_value=None), \
                mock.patch.object(h, '_fetch_task_events', return_value=events):
            fn = h._default_in_flight_fn()
            self.assertTrue(fn('delegate-x'))

    def test_not_in_flight_when_no_events_and_not_failed(self):
        import build_sequence_advancer as bsa
        import chain_event_emit
        with mock.patch.object(chain_event_emit, '_get_client', return_value=object()), \
                mock.patch.object(bsa, 'chain_event_says_failed', return_value=None), \
                mock.patch.object(h, '_fetch_task_events', return_value=[]):
            fn = h._default_in_flight_fn()
            self.assertFalse(fn('delegate-x'))


# ---------------------------------------- commit + push (real temp git repo)


class CommitAndPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cap-git-test-')
        self.repo = Path(self.tmp) / 'repo'
        self.repo.mkdir()
        self._git('init', '-q', '-b', 'main')
        self._git('config', 'user.email', 'test@test')
        self._git('config', 'user.name', 'Test')
        (self.repo / 'agents' / 'beacon').mkdir(parents=True)
        self.cap = self.repo / h.CAPTURES_REL
        self.cap.write_text(json.dumps({'schema_version': 1, 'captures': []}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=str(self.repo), check=True,
                       capture_output=True, text=True)

    def test_nothing_when_clean(self):
        self.assertEqual(h.commit_and_push_captures(self.repo, 'audit'), 'nothing')

    def test_refuses_off_main(self):
        self._git('checkout', '-q', '-b', 'forge/feature')
        self.cap.write_text(json.dumps({'schema_version': 1,
                                        'captures': [{'id': 'x'}]}) + '\n')
        self.assertEqual(h.commit_and_push_captures(self.repo, 'audit'),
                         'wrong-branch')

    def test_commits_delta_then_clean(self):
        # No remote configured: commit succeeds, push fails -> 'push-failed',
        # but the delta IS committed locally (durability half is the commit).
        self.cap.write_text(json.dumps({'schema_version': 1,
                                        'captures': [{'id': 'x'}]}) + '\n')
        status = h.commit_and_push_captures(self.repo, 'audit-line')
        self.assertEqual(status, 'push-failed')  # no origin remote in the test
        # The commit landed: working tree is clean and HEAD message carries audit.
        log = subprocess.run(['git', 'log', '-1', '--pretty=%B'],
                             cwd=str(self.repo), capture_output=True, text=True)
        self.assertIn('GC healer', log.stdout)
        self.assertIn('audit-line', log.stdout)
        # A second call finds nothing to commit (idempotent).
        self.assertEqual(h.commit_and_push_captures(self.repo, 'audit'), 'nothing')

    def test_missions_nothing_when_clean(self):
        # No missions.json delta → 'nothing' (the GC healer's per-tick missions
        # commit is cheap on a clean tree).
        miss = self.repo / h.MISSIONS_REL
        miss.write_text(json.dumps({'schema_version': 1, 'missions': []}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed missions')
        self.assertEqual(h.commit_and_push_missions(self.repo, 'audit'), 'nothing')

    def test_missions_commits_any_pending_delta(self):
        # Contract D: a missions.json delta authored by a SEPARATE cleanup (the
        # GC healer didn't ship it) is still committed by the healer as the
        # single committer. No remote → push fails, but the commit lands.
        miss = self.repo / h.MISSIONS_REL
        miss.write_text(json.dumps({'schema_version': 1, 'missions': []}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed missions')
        # A cleanup removes a mission / edits the file on disk (uncommitted).
        miss.write_text(json.dumps(
            {'schema_version': 1, 'missions': [{'id': 'm-cleanup'}]}) + '\n')
        status = h.commit_and_push_missions(self.repo, 'cleanup-delta-audit')
        self.assertEqual(status, 'push-failed')  # no origin remote in the test
        log = subprocess.run(['git', 'log', '-1', '--pretty=%B'],
                             cwd=str(self.repo), capture_output=True, text=True)
        self.assertIn('commit missions.json delta', log.stdout)
        self.assertIn('cleanup-delta-audit', log.stdout)
        # Working tree is clean for missions.json (the delta is now committed).
        self.assertEqual(
            subprocess.run(['git', 'diff', '--quiet', '--', h.MISSIONS_REL],
                           cwd=str(self.repo)).returncode, 0)
        # Idempotent: a second call finds nothing.
        self.assertEqual(h.commit_and_push_missions(self.repo, 'audit'), 'nothing')


# ----------------------------------------------------- run_once integration


class RunOnceTest(unittest.TestCase):
    def test_clean_board_is_noop(self):
        # No open sessions, no captures path -> nothing retired/aged/committed.
        now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(h, 'load_repo_paths', return_value={}):
            rc = h.run_once(
                dry_run=False,
                emit_fn=lambda **_: True,
                events_fetcher=lambda: [],
                now=now,
            )
        self.assertEqual(rc, 0)

    def test_unavailable_chain_events_skips_retire_phase(self):
        now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(h, 'load_repo_paths', return_value={}):
            rc = h.run_once(
                dry_run=False,
                emit_fn=lambda **_: True,
                events_fetcher=lambda: None,  # client unavailable
                now=now,
            )
        self.assertEqual(rc, 0)

    def test_end_to_end_dry_run_retires_and_ages(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-test-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            old = (now - timedelta(days=30)).isoformat()
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1,
                'captures': [{'id': 'cap-a', 'state': 'parked',
                              'last_touched': old}],
            }) + '\n')
            rows = [_start('t1', now - timedelta(hours=48),
                           repo='unknown-repo', branch='main')]
            emitted = []
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}):
                rc = h.run_once(
                    dry_run=True,
                    emit_fn=lambda **k: emitted.append(k) or True,
                    events_fetcher=lambda: rows,
                    now=now,
                )
            self.assertEqual(rc, 0)
            # dry-run: no synthetic done emitted, captures.json not mutated.
            self.assertEqual(emitted, [])
            reg = json.loads((core / h.CAPTURES_REL).read_text())
            self.assertNotIn('aging', reg['captures'][0])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_narrator_sweep_briefs_and_writes_before_commit(self):
        # Contract A: the folded sweep runs in LIVE mode (use_llm=True), mutates
        # the in-memory registry, and the healer's SINGLE write lands the
        # briefing on disk — proving the healer is the sole captures.json writer.
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-narrate-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1,
                'captures': [{'id': 'cap-a', 'state': 'parked',
                              'last_touched': now.isoformat()}],
            }) + '\n')

            calls = {}

            def fake_author(registry, *, now=None, use_llm=True, **_):
                calls['use_llm'] = use_llm
                n = 0
                for c in registry.get('captures', []):
                    if c.get('state') == 'parked' and 'briefing' not in c:
                        c['briefing'] = {'what': 'w', 'why': 'y', 'suggest': 's'}
                        c['risk'] = 'safe'
                        n += 1
                return (n, 0)

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=fake_author,
                    now=now,
                )
            self.assertEqual(rc, 0)
            self.assertTrue(calls['use_llm'])  # LIVE mode → author with the LLM
            reg = json.loads((core / h.CAPTURES_REL).read_text())
            self.assertEqual(reg['captures'][0]['briefing'],
                             {'what': 'w', 'why': 'y', 'suggest': 's'})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_concurrent_ingest_during_sweep_survives_write(self):
        # § 2 lost-update guard: a capture the dashboard ingests DURING the slow
        # Narrator sweep must survive the healer's write. The healer re-reads
        # captures.json fresh just before writing and applies only its own
        # per-capture deltas, so the concurrently-ingested capture is preserved.
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-lost-update-ingest-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            cap_file = core / h.CAPTURES_REL
            cap_file.write_text(json.dumps({
                'schema_version': 1,
                'captures': [{'id': 'cap-a', 'state': 'parked',
                              'last_touched': now.isoformat()}],
            }) + '\n')

            def fake_author(registry, *, now=None, use_llm=True, **_):
                # Simulate a SEPARATE process (dashboard API) ingesting a new
                # capture onto disk mid-sweep, then brief cap-a in memory.
                disk = json.loads(cap_file.read_text())
                disk['captures'].append({'id': 'cap-b', 'state': 'parked',
                                         'last_touched': now.isoformat()})
                cap_file.write_text(json.dumps(disk) + '\n')
                n = 0
                for c in registry.get('captures', []):
                    if c.get('id') == 'cap-a':
                        c['briefing'] = {'what': 'w', 'why': 'y', 'suggest': 's'}
                        n += 1
                return (n, 0)

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=fake_author,
                    now=now,
                )
            self.assertEqual(rc, 0)
            reg = json.loads(cap_file.read_text())
            by_id = {c['id']: c for c in reg['captures']}
            # the concurrently-ingested capture survived the healer's write
            self.assertEqual(set(by_id), {'cap-a', 'cap-b'})
            # our briefing delta still landed on cap-a
            self.assertEqual(by_id['cap-a']['briefing'],
                             {'what': 'w', 'why': 'y', 'suggest': 's'})
            # the untouched concurrent capture was not mutated by the healer
            self.assertNotIn('briefing', by_id['cap-b'])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_concurrent_state_change_on_briefed_capture_survives(self):
        # § 2 lost-update guard, second failure mode: if the dashboard snoozes a
        # capture WHILE it is being briefed, the field-level merge applies only the
        # briefing fields — the dashboard's state change is NOT clobbered.
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-lost-update-state-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            cap_file = core / h.CAPTURES_REL
            cap_file.write_text(json.dumps({
                'schema_version': 1,
                'captures': [{'id': 'cap-a', 'state': 'parked',
                              'last_touched': now.isoformat()}],
            }) + '\n')

            def fake_author(registry, *, now=None, use_llm=True, **_):
                # dashboard flips cap-a parked→snoozed on disk mid-sweep
                disk = json.loads(cap_file.read_text())
                disk['captures'][0]['state'] = 'snoozed'
                disk['captures'][0]['snooze_until'] = '2026-07-01T00:00:00+00:00'
                cap_file.write_text(json.dumps(disk) + '\n')
                for c in registry.get('captures', []):
                    if c.get('id') == 'cap-a':
                        c['briefing'] = {'what': 'w', 'why': 'y', 'suggest': 's'}
                return (1, 0)

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=fake_author,
                    now=now,
                )
            self.assertEqual(rc, 0)
            cap = json.loads(cap_file.read_text())['captures'][0]
            # the dashboard's concurrent state change survived (not clobbered)
            self.assertEqual(cap['state'], 'snoozed')
            self.assertEqual(cap['snooze_until'], '2026-07-01T00:00:00+00:00')
            # and our briefing field was still merged on
            self.assertEqual(cap['briefing'],
                             {'what': 'w', 'why': 'y', 'suggest': 's'})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dry_run_narrator_uses_no_llm_and_writes_nothing(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-narrate-dry-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1,
                'captures': [{'id': 'cap-a', 'state': 'parked',
                              'last_touched': now.isoformat()}],
            }) + '\n')
            before = (core / h.CAPTURES_REL).read_text()

            calls = {}

            def fake_author(registry, *, now=None, use_llm=True, **_):
                calls['use_llm'] = use_llm
                # mutate to prove the dry-run discards the in-memory change
                for c in registry.get('captures', []):
                    c['briefing'] = {'what': 'w', 'why': 'y', 'suggest': 's'}
                return (1, 0)

            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}):
                rc = h.run_once(
                    dry_run=True,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=fake_author,
                    now=now,
                )
            self.assertEqual(rc, 0)
            self.assertFalse(calls['use_llm'])  # dry-run → deterministic raw, no spawn
            self.assertEqual((core / h.CAPTURES_REL).read_text(), before)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_author_fn_resolves_to_narrator_sweep(self):
        # When no author_fn seam is injected, run_once lazily binds the real
        # missions_narrator.author_captures_in_registry (no circular import).
        import missions_narrator as mn_mod
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-default-author-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1, 'captures': [],
            }) + '\n')
            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'), \
                    mock.patch.object(mn_mod, 'author_captures_in_registry',
                                      return_value=(0, 0)) as spy:
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    now=now,
                )
            self.assertEqual(rc, 0)
            spy.assert_called_once()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_completion_closes_safe_card_through_write(self):
        # End-to-end: a parked card whose spawned work is verified-merged is
        # auto-closed to `done` and the healer's SINGLE write lands it on disk
        # (proving completion rides the same single-committer write path).
        now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-complete-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1,
                'captures': [{
                    'id': 'cap-x', 'state': 'parked', 'risk': 'safe',
                    'last_touched': now.isoformat(),
                    'spawned': {'kind': 'delegate', 'task_id': 'delegate-cap-x',
                                'stamped_at': '2026-06-15T00:00:00+00:00'},
                }],
            }) + '\n')

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),  # skip the narrator
                    completion_verify_fn=lambda t, d: (
                        True, 'https://github.com/o/r/pull/600'),
                    completion_closeout_fn=lambda c, p, n: {},
                    now=now,
                )
            self.assertEqual(rc, 0)
            cap = json.loads((core / h.CAPTURES_REL).read_text())['captures'][0]
            self.assertEqual(cap['state'], 'done')
            self.assertEqual(cap['shipped_note'], 'shipped in PR #600')
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failure_rings_doorbell_through_write(self):
        # End-to-end S4: a parked card whose linked work failed rings the loud
        # blocked-on-you doorbell, stays parked, and the failure_signaled stamp
        # lands on disk via the healer's SINGLE write (single-committer invariant).
        now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-failure-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1,
                'captures': [{
                    'id': 'cap-f', 'state': 'parked', 'risk': 'careful',
                    'title': 'ship the thing',
                    'last_touched': now.isoformat(),
                    'spawned': {'kind': 'delegate', 'task_id': 'delegate-cap-f',
                                'stamped_at': '2026-06-15T00:00:00+00:00'},
                }],
            }) + '\n')

            rings = []
            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),
                    completion_verify_fn=lambda t, d: (False, None),
                    completion_closeout_fn=lambda c, p, n: {},
                    failure_fn=lambda t: 'forge_reject: tests broke',
                    ring_fn=lambda **kw: rings.append(kw) or {'rung': True},
                    in_flight_fn=lambda t: False,
                    now=now,
                )
            self.assertEqual(rc, 0)
            cap = json.loads((core / h.CAPTURES_REL).read_text())['captures'][0]
            self.assertEqual(cap['state'], 'parked')  # surfaced, not closed
            self.assertEqual(cap['failure_signaled']['reason'],
                             'forge_reject: tests broke')
            self.assertEqual(len(rings), 1)
            self.assertTrue(rings[0]['blocked'])
            self.assertEqual(rings[0]['detail'], 'forge_reject: tests broke')
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_deferred_drop_applies_after_safe_stop_through_write(self):
        # End-to-end S7: a card carrying a pending drop whose linked work has
        # reached a safe stop (in_flight_fn False) is dropped, and the new state
        # lands on disk via the healer's SINGLE write.
        now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-deferred-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1,
                'captures': [{
                    'id': 'cap-d', 'state': 'parked', 'risk': 'safe',
                    'last_touched': now.isoformat(),
                    'spawned': {'kind': 'delegate', 'task_id': 'delegate-cap-d',
                                'stamped_at': '2026-06-15T00:00:00+00:00'},
                    'pending_action': {'action': 'drop', 'args': {'reason': 'obsolete'}},
                }],
            }) + '\n')

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),
                    completion_verify_fn=lambda t, d: (False, None),
                    completion_closeout_fn=lambda c, p, n: {},
                    failure_fn=lambda t: None,
                    ring_fn=lambda **kw: {'rung': True},
                    in_flight_fn=lambda t: False,
                    now=now,
                )
            self.assertEqual(rc, 0)
            cap = json.loads((core / h.CAPTURES_REL).read_text())['captures'][0]
            self.assertEqual(cap['state'], 'dropped')
            self.assertEqual(cap['drop_reason'], 'obsolete')
            self.assertNotIn('pending_action', cap)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_narrator_sweep_error_does_not_abort_tick(self):
        # A sweep that raises is fail-safe: logged, briefed=0, tick still rc=0.
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-narrate-err-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(json.dumps({
                'schema_version': 1,
                'captures': [{'id': 'cap-a', 'state': 'parked',
                              'last_touched': now.isoformat()}],
            }) + '\n')

            def boom(registry, **_):
                raise RuntimeError('sweep exploded')

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=boom,
                    now=now,
                )
            self.assertEqual(rc, 0)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pending_missions_delta_is_committed_even_when_nothing_ships(self):
        # Contract D end-to-end: a missions.json delta left by a SEPARATE cleanup
        # (the GC healer's own reconcile ships nothing this tick) is committed by
        # the healer within ONE tick. Uses a real git repo so the commit lands.
        now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-missions-cleanup-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)

            def git(*args):
                subprocess.run(['git', *args], cwd=str(core), check=True,
                               capture_output=True, text=True)

            git('init', '-q', '-b', 'main')
            git('config', 'user.email', 'test@test')
            git('config', 'user.name', 'Test')
            (core / h.CAPTURES_REL).write_text(
                json.dumps({'schema_version': 1, 'captures': []}) + '\n')
            (core / h.MISSIONS_REL).write_text(json.dumps(
                {'schema_version': 1,
                 'missions': [{'id': 'm-old', 'phase': 'proposed'}]}) + '\n')
            git('add', '.')
            git('commit', '-q', '-m', 'seed')

            # A cleanup retires the mission on disk (uncommitted) — the healer's
            # reconcile won't ship anything, but it must still persist this delta.
            (core / h.MISSIONS_REL).write_text(
                json.dumps({'schema_version': 1, 'missions': []}) + '\n')

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),
                    mission_probe_fn=lambda tid: 'open',
                    now=now,
                )
            self.assertEqual(rc, 0)
            # The cleanup delta is now committed: working tree is clean for
            # missions.json and the audit names the GC-healer commit.
            self.assertEqual(
                subprocess.run(['git', 'diff', '--quiet', '--', h.MISSIONS_REL],
                               cwd=str(core)).returncode, 0,
                'missions.json delta should be committed within one tick')
            log = subprocess.run(['git', 'log', '-1', '--pretty=%B'],
                                 cwd=str(core), capture_output=True, text=True)
            self.assertIn('commit missions.json delta', log.stdout)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class RunOnceMissionNarratorTest(unittest.TestCase):
    """projects-v3 P2 Contract A: the GC-tick folds a funnel-mission Narrator
    sweep (orphan + suggested) into the SAME single missions.json write+commit —
    the healer stays the sole committer, lost-update safe, no spawn on dry-run."""

    def test_mission_sweep_briefs_and_writes_before_commit(self):
        # LIVE mode (use_llm=True): the sweep mutates the in-memory registry and
        # the healer's SINGLE write lands the briefing on disk.
        now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-mission-narrate-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(
                json.dumps({'schema_version': 1, 'captures': []}) + '\n')
            (core / h.MISSIONS_REL).write_text(json.dumps({
                'schema_version': 1,
                'missions': [{'id': 'm-a', 'phase': 'proposed',
                              'name': 'Reviewer bot bug',
                              'proposed_by': 'heal_orphan_autoregister'}],
            }) + '\n')

            calls = {}

            def fake_mission_author(registry, *, now=None, use_llm=True, **_):
                calls['use_llm'] = use_llm
                n = 0
                for m in registry.get('missions', []):
                    if m.get('phase') == 'proposed' and 'briefing' not in m:
                        m['briefing'] = {'what': 'w', 'why': 'y', 'suggest': 's'}
                        m['risk'] = 'medium'
                        n += 1
                return (n, 0)

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),  # skip capture narrator
                    mission_author_fn=fake_mission_author,
                    mission_probe_fn=lambda tid: 'open',
                    now=now,
                )
            self.assertEqual(rc, 0)
            self.assertTrue(calls['use_llm'])
            reg = json.loads((core / h.MISSIONS_REL).read_text())
            self.assertEqual(reg['missions'][0]['briefing'],
                             {'what': 'w', 'why': 'y', 'suggest': 's'})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_concurrent_mission_change_during_sweep_survives(self):
        # § 2 lost-update guard for missions: a dashboard accept/drop landing on
        # disk DURING the slow sweep must survive the healer's write. The healer
        # re-reads fresh and applies only its own per-mission briefing deltas.
        now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-mission-lost-update-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(
                json.dumps({'schema_version': 1, 'captures': []}) + '\n')
            miss_file = core / h.MISSIONS_REL
            miss_file.write_text(json.dumps({
                'schema_version': 1,
                'missions': [{'id': 'm-a', 'phase': 'proposed',
                              'name': 'Reviewer bot bug',
                              'proposed_by': 'beacon'}],
            }) + '\n')

            def fake_mission_author(registry, *, now=None, use_llm=True, **_):
                # A separate process (dashboard) ingests a new proposed mission
                # AND flips m-a's phase on disk mid-sweep; brief m-a in memory.
                disk = json.loads(miss_file.read_text())
                disk['missions'][0]['phase'] = 'drafting'  # dashboard accepted it
                disk['missions'].append({'id': 'm-b', 'phase': 'proposed',
                                         'name': 'New suggestion',
                                         'proposed_by': 'medic'})
                miss_file.write_text(json.dumps(disk) + '\n')
                for m in registry.get('missions', []):
                    if m.get('id') == 'm-a':
                        m['briefing'] = {'what': 'w', 'why': 'y', 'suggest': 's'}
                return (1, 0)

            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'):
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),
                    mission_author_fn=fake_mission_author,
                    mission_probe_fn=lambda tid: 'open',
                    now=now,
                )
            self.assertEqual(rc, 0)
            reg = json.loads(miss_file.read_text())
            by_id = {m['id']: m for m in reg['missions']}
            # the concurrently-ingested mission survived the healer's write
            self.assertEqual(set(by_id), {'m-a', 'm-b'})
            # the dashboard's concurrent phase change survived (not clobbered)
            self.assertEqual(by_id['m-a']['phase'], 'drafting')
            # our briefing delta still merged onto m-a
            self.assertEqual(by_id['m-a']['briefing'],
                             {'what': 'w', 'why': 'y', 'suggest': 's'})
            # the untouched concurrent mission was not mutated by the healer
            self.assertNotIn('briefing', by_id['m-b'])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dry_run_mission_sweep_uses_no_llm_and_writes_nothing(self):
        now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-mission-narrate-dry-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(
                json.dumps({'schema_version': 1, 'captures': []}) + '\n')
            (core / h.MISSIONS_REL).write_text(json.dumps({
                'schema_version': 1,
                'missions': [{'id': 'm-a', 'phase': 'proposed',
                              'name': 'Reviewer bot bug'}],
            }) + '\n')
            before = (core / h.MISSIONS_REL).read_text()

            calls = {}

            def fake_mission_author(registry, *, now=None, use_llm=True, **_):
                calls['use_llm'] = use_llm
                for m in registry.get('missions', []):
                    m['briefing'] = {'what': 'w', 'why': 'y', 'suggest': 's'}
                return (1, 0)

            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}):
                rc = h.run_once(
                    dry_run=True,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),
                    mission_author_fn=fake_mission_author,
                    mission_probe_fn=lambda tid: 'open',
                    now=now,
                )
            self.assertEqual(rc, 0)
            self.assertFalse(calls['use_llm'])  # dry-run → deterministic raw, no spawn
            self.assertEqual((core / h.MISSIONS_REL).read_text(), before)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_mission_author_fn_resolves_to_narrator_sweep(self):
        # No mission_author_fn injected → run_once lazily binds the real
        # missions_narrator.author_missions_in_registry (no circular import).
        import missions_narrator as mn_mod
        now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-mission-default-author-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.CAPTURES_REL).write_text(
                json.dumps({'schema_version': 1, 'captures': []}) + '\n')
            (core / h.MISSIONS_REL).write_text(json.dumps({
                'schema_version': 1, 'missions': [],
            }) + '\n')
            import larry_alerts
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(larry_alerts, 'append_alert'), \
                    mock.patch.object(mn_mod, 'author_missions_in_registry',
                                      return_value=(0, 0)) as spy:
                rc = h.run_once(
                    dry_run=False,
                    emit_fn=lambda **_: True,
                    events_fetcher=lambda: [],
                    author_fn=lambda registry, **_: (0, 0),
                    mission_probe_fn=lambda tid: 'open',
                    now=now,
                )
            self.assertEqual(rc, 0)
            spy.assert_called_once()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
