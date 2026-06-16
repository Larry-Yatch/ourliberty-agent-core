#!/usr/bin/env python3
"""Tests for heal_missions_board_drain (Missions v2 Phase S, step S-6 / S8).

Covers the Mirror-review focus areas for the one-time board drain:
  * only closes verified-merged drafts — a promoted draft is closed ONLY when
    EVERY resolved task_id passes the belt-and-suspenders verify gate; any
    unverified/indeterminate/unresolved draft is KEPT (fail-safe).
  * risk routing reuses S-2 — a `safe` draft auto-closes to `done`; a
    `medium`/`careful`/un-briefed draft moves to `review_close` (awaiting ack),
    never silently closed.
  * proposed surfaced, NOT auto-dismissed — the proposed pass writes a
    batch-review artifact and never mutates missions.json.
  * idempotent — a closed draft (no longer `promoted`) is skipped on re-run; the
    surfacing artifact regenerates deterministically.
  * atomic / dry-run — dry-run mutates nothing and writes nothing; apply writes
    via tmp+rename and (per the single-committer invariant) never commits.

All effectful edges run through the injectable seams (verify_fn / paths / now /
OURLIBERTY_AGENTS_ROOT) so no test touches the live Supabase table, gh, or the
real captures.json / blackboard. Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_missions_board_drain
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
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_missions_board_drain as d  # noqa: E402

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
_PR = 'https://github.com/o/r/pull/541'


def _verify_all(merged_task_ids: set, pr=_PR):
    """A verify_fn seam: returns (True, pr) for task_ids in the set, else (False, pr)."""
    def _v(task_id, dispatched_at):  # noqa: ARG001
        return (task_id in merged_task_ids, pr if task_id in merged_task_ids else None)
    return _v


def _promoted(cid, *, risk=None, spawned=None, promoted_to=None):
    cap = {'id': cid, 'state': 'promoted', 'title': cid}
    if risk is not None:
        cap['risk'] = risk
    if spawned is not None:
        cap['spawned'] = spawned
    if promoted_to is not None:
        cap['promoted_to'] = promoted_to
    return cap


# ---------------------------------------------------------- task-id resolution


class ResolveDraftTaskIdsTest(unittest.TestCase):
    def test_spawned_task_id_wins(self):
        cap = _promoted('c1', spawned={'kind': 'delegate', 'task_id': 'delegate-c1'},
                        promoted_to='m-ignored')
        self.assertEqual(d.resolve_draft_task_ids(cap, {}), ['delegate-c1'])

    def test_resolves_via_spawned_mission_id(self):
        cap = _promoted('c2', spawned={'kind': 'mission', 'mission_id': 'm-x'})
        missions = {'m-x': {'id': 'm-x', 'task_ids': ['t1', 't2']}}
        self.assertEqual(d.resolve_draft_task_ids(cap, missions), ['t1', 't2'])

    def test_resolves_via_promoted_to(self):
        cap = _promoted('c3', promoted_to='m-y')
        missions = {'m-y': {'id': 'm-y', 'task_ids': ['t3']}}
        self.assertEqual(d.resolve_draft_task_ids(cap, missions), ['t3'])

    def test_unresolved_returns_empty(self):
        # No spawned, promoted_to points at a mission that isn't in the registry.
        cap = _promoted('c4', promoted_to='m-missing')
        self.assertEqual(d.resolve_draft_task_ids(cap, {}), [])
        # spawned with neither task_id nor a resolvable mission_id.
        self.assertEqual(d.resolve_draft_task_ids(_promoted('c5', spawned={}), {}), [])


# ------------------------------------------------------- belt-and-suspenders


class VerifyAllMergedTest(unittest.TestCase):
    def test_all_merged_true_with_pr(self):
        ok, pr = d._verify_all_merged(['t1', 't2'], None, _verify_all({'t1', 't2'}))
        self.assertTrue(ok)
        self.assertEqual(pr, _PR)

    def test_one_unmerged_short_circuits_false(self):
        seen = []

        def _v(task_id, dispatched_at):  # noqa: ARG001
            seen.append(task_id)
            return (task_id == 't1', _PR if task_id == 't1' else None)

        ok, _ = d._verify_all_merged(['t1', 't2', 't3'], None, _v)
        self.assertFalse(ok)
        self.assertEqual(seen, ['t1', 't2'])  # stopped at the first unverified

    def test_verify_error_keeps(self):
        def _v(task_id, dispatched_at):  # noqa: ARG001
            raise RuntimeError('gh blew up')

        ok, _ = d._verify_all_merged(['t1'], None, _v)
        self.assertFalse(ok)


# ------------------------------------------------- promoted-draft reconcile


class ReconcilePromotedDraftsTest(unittest.TestCase):
    def _reg(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def test_safe_verified_auto_closes(self):
        cap = _promoted('c-safe', risk='safe', spawned={'task_id': 't1'})
        reg = self._reg(cap)
        res = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'t1'}), dry_run=False)
        self.assertEqual([c[0] for c in res.closed], ['c-safe'])
        self.assertEqual(cap['state'], d.gc.COMPLETED_STATE_DONE)
        self.assertEqual(cap['closed_by'], d.DRAIN_BY)
        self.assertEqual(cap['shipped_note'], 'shipped in PR #541')
        self.assertEqual(cap['shipped_pr_url'], _PR)

    def test_risky_verified_moves_to_review(self):
        cap = _promoted('c-med', risk='medium', spawned={'task_id': 't1'})
        reg = self._reg(cap)
        res = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'t1'}), dry_run=False)
        self.assertEqual(res.closeouts, ['c-med'])
        self.assertEqual(cap['state'], d.gc.COMPLETED_STATE_REVIEW)
        self.assertTrue(cap['awaiting_ack'])
        self.assertEqual(cap['drain_closeout'], d.DRAIN_CLOSEOUT_NOTE)

    def test_unbriefed_verified_moves_to_review(self):
        # No risk key ⇒ classify_completion routes to closeout (fail toward review).
        cap = _promoted('c-unbriefed', spawned={'task_id': 't1'})
        reg = self._reg(cap)
        res = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'t1'}), dry_run=False)
        self.assertEqual(res.closeouts, ['c-unbriefed'])
        self.assertEqual(cap['state'], d.gc.COMPLETED_STATE_REVIEW)

    def test_unverified_kept(self):
        cap = _promoted('c-open', risk='safe', spawned={'task_id': 't1'})
        reg = self._reg(cap)
        res = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all(set()), dry_run=False)
        self.assertEqual(res.closed, [])
        self.assertEqual(res.kept, 1)
        self.assertEqual(cap['state'], 'promoted')  # untouched

    def test_unresolved_kept_and_recorded(self):
        cap = _promoted('c-noid', risk='safe', promoted_to='m-missing')
        reg = self._reg(cap)
        res = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'whatever'}), dry_run=False)
        self.assertEqual(res.unresolved, ['c-noid'])
        self.assertEqual(res.kept, 1)
        self.assertEqual(cap['state'], 'promoted')

    def test_mission_task_ids_must_all_merge(self):
        cap = _promoted('c-mission', risk='safe', spawned={'mission_id': 'm1'})
        missions = {'m1': {'id': 'm1', 'task_ids': ['t1', 't2']}}
        reg = self._reg(cap)
        # only t1 merged ⇒ not all ⇒ keep
        res = d.reconcile_promoted_drafts(
            reg, missions, _NOW, verify_fn=_verify_all({'t1'}), dry_run=False)
        self.assertEqual(res.kept, 1)
        self.assertEqual(cap['state'], 'promoted')
        # both merged ⇒ close
        res2 = d.reconcile_promoted_drafts(
            reg, missions, _NOW, verify_fn=_verify_all({'t1', 't2'}), dry_run=False)
        self.assertEqual([c[0] for c in res2.closed], ['c-mission'])

    def test_ignores_non_promoted_states(self):
        parked = {'id': 'p1', 'state': 'parked', 'spawned': {'task_id': 't1'}}
        done = {'id': 'd1', 'state': 'done', 'spawned': {'task_id': 't1'}}
        reg = self._reg(parked, done)
        res = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'t1'}), dry_run=False)
        self.assertFalse(res.changed)
        self.assertEqual(parked['state'], 'parked')
        self.assertEqual(done['state'], 'done')

    def test_dry_run_does_not_mutate(self):
        cap = _promoted('c-safe', risk='safe', spawned={'task_id': 't1'})
        reg = self._reg(cap)
        res = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'t1'}), dry_run=True)
        self.assertEqual([c[0] for c in res.closed], ['c-safe'])  # reported
        self.assertEqual(cap['state'], 'promoted')  # but untouched
        self.assertNotIn('closed_at', cap)

    def test_idempotent_rerun_is_noop(self):
        cap = _promoted('c-safe', risk='safe', spawned={'task_id': 't1'})
        reg = self._reg(cap)
        d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'t1'}), dry_run=False)
        # second pass: the card is now `done`, no longer promoted ⇒ skipped.
        res2 = d.reconcile_promoted_drafts(
            reg, {}, _NOW, verify_fn=_verify_all({'t1'}), dry_run=False)
        self.assertFalse(res2.changed)


# ------------------------------------------------ proposed-lane surfacing


class GatherProposedBacklogTest(unittest.TestCase):
    def _reg(self):
        return {'schema_version': 1, 'missions': [
            {'id': 'p-1', 'phase': 'proposed', 'task_ids': ['a']},
            {'id': 'p-2', 'phase': 'proposed', 'task_ids': ['b'], 'acknowledged': True},
            {'id': 'm-draft', 'phase': 'drafting', 'task_ids': ['c']},
            {'id': 'm-ship', 'phase': 'shipped', 'task_ids': ['d']},
        ]}

    def test_only_unacknowledged_proposed(self):
        out = d.gather_proposed_backlog(self._reg())
        self.assertEqual([m['id'] for m in out], ['p-1'])

    def test_item_shape(self):
        out = d.gather_proposed_backlog(self._reg())
        self.assertEqual(set(out[0]), {'id', 'name', 'task_ids', 'brief', 'created'})


class SurfaceProposedBacklogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='drain-surface-')
        self._prev = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self.tmp
        self.reg = {'schema_version': 1, 'missions': [
            {'id': 'p-1', 'phase': 'proposed', 'task_ids': ['a']},
            {'id': 'p-2', 'phase': 'proposed', 'acknowledged': True},
        ]}

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev

    def test_dry_run_writes_nothing(self):
        res = d.surface_proposed_backlog(self.reg, _NOW, dry_run=True)
        self.assertEqual(res.count, 1)
        self.assertFalse(res.written)
        self.assertFalse(res.artifact_path.exists())

    def test_apply_writes_artifact(self):
        res = d.surface_proposed_backlog(self.reg, _NOW, dry_run=False)
        self.assertTrue(res.written)
        self.assertTrue(res.artifact_path.exists())
        body = json.loads(res.artifact_path.read_text())
        self.assertEqual(body['count'], 1)
        self.assertEqual([i['id'] for i in body['items']], ['p-1'])
        self.assertEqual(body['generated_by'], d.DRAIN_BY)
        self.assertIn('not auto-dismissed', body['note'])

    def test_does_not_mutate_missions(self):
        before = json.dumps(self.reg, sort_keys=True)
        d.surface_proposed_backlog(self.reg, _NOW, dry_run=False)
        self.assertEqual(json.dumps(self.reg, sort_keys=True), before)

    def test_idempotent_rewrite(self):
        first = d.surface_proposed_backlog(self.reg, _NOW, dry_run=False)
        body1 = first.artifact_path.read_text()
        second = d.surface_proposed_backlog(self.reg, _NOW, dry_run=False)
        self.assertEqual(second.artifact_path.read_text(), body1)


# -------------------------------------------------------- end-to-end run


class RunDrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='drain-e2e-')
        self.agents = tempfile.mkdtemp(prefix='drain-agents-')
        self._prev = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self.agents
        self.cap_path = Path(self.tmp) / 'captures.json'
        self.miss_path = Path(self.tmp) / 'missions.json'
        self.cap_path.write_text(json.dumps({'schema_version': 1, 'captures': [
            _promoted('c-safe', risk='safe', spawned={'task_id': 't1'}),
            _promoted('c-open', risk='safe', spawned={'task_id': 't2'}),
        ]}))
        self.miss_path.write_text(json.dumps({'schema_version': 1, 'missions': [
            {'id': 'p-1', 'phase': 'proposed', 'task_ids': ['a']},
            {'id': 'p-2', 'phase': 'proposed', 'acknowledged': True},
        ]}))

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev

    def _run(self, dry_run):
        return d.run_drain(
            dry_run=dry_run,
            verify_fn=_verify_all({'t1'}),  # only c-safe's work merged
            captures_reg_path=self.cap_path,
            missions_reg_path=self.miss_path,
            now=_NOW,
        )

    def test_dry_run_writes_nothing(self):
        rc = self._run(dry_run=True)
        self.assertEqual(rc, 0)
        caps = json.loads(self.cap_path.read_text())['captures']
        self.assertTrue(all(c['state'] == 'promoted' for c in caps))
        self.assertFalse(d.drain_artifact_path(_NOW).exists())

    def test_apply_closes_verified_and_surfaces(self):
        rc = self._run(dry_run=False)
        self.assertEqual(rc, 0)
        by_id = {c['id']: c for c in json.loads(self.cap_path.read_text())['captures']}
        self.assertEqual(by_id['c-safe']['state'], 'done')   # verified ⇒ closed
        self.assertEqual(by_id['c-open']['state'], 'promoted')  # unverified ⇒ kept
        # missions.json never mutated.
        miss = {m['id']: m for m in json.loads(self.miss_path.read_text())['missions']}
        self.assertEqual(miss['p-1']['phase'], 'proposed')
        # surfacing artifact written with the one unattended item.
        art = json.loads(d.drain_artifact_path(_NOW).read_text())
        self.assertEqual([i['id'] for i in art['items']], ['p-1'])

    def test_idempotent_second_apply(self):
        self._run(dry_run=False)
        before = self.cap_path.read_text()
        self._run(dry_run=False)  # c-safe is now `done`, no longer promoted
        self.assertEqual(self.cap_path.read_text(), before)


if __name__ == '__main__':
    unittest.main()
