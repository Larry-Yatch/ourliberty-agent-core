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

    def test_all_tasks_terminal_ships(self):
        states = {'a': h.tts.MERGED, 'b': h.tts.CLOSED}
        self.assertEqual(
            h.classify_mission('in_flight', ['a', 'b'], states).action, 'ship')

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


if __name__ == '__main__':
    unittest.main()
