#!/usr/bin/env python3
"""Tests for heal_orphan_autoregister (missions-v2 Phase 3 § 6).

Covers the Mirror-review focus areas:
  * REUSE THE DERIVE (no drift) — the healer drives the REAL dashboard_api derive
    (detect_orphans / is_infrastructure_task / _derive_orphan_readability), so an
    infrastructure task_id is never proposed and a registered task_id is excluded
    by the same function the board uses.
  * IDEMPOTENCY — once a proposed entry registers the orphan's task_id, a second
    tick's detect_orphans excludes it; the entry-id guard is a defensive second
    layer; a no-op tick mutates nothing.
  * ERRS TOWARD NOT PROPOSING — a terminal orphan (merged/closed PR) is skipped; an
    orphan whose live PR-state is indeterminate (pr_url present, not resolved) is
    skipped; chain_events unavailable / missions.json malformed → propose nothing.
  * ATOMIC WRITES — missions.json is written via tmp+rename; a malformed file is
    refused (None) rather than appended onto.
  * COMMIT-AND-PUSH — refuses to commit off-main; no-op on a clean tree; commits a
    real delta on a temp git repo.

The derive itself is the real dashboard_api module (the whole point — no re-port);
only the effectful edges (events_fetcher / pr_state_resolver / now) are seamed so no
test touches the live Supabase table, gh, or the real missions.json. Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_orphan_autoregister
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

import heal_orphan_autoregister as h  # noqa: E402
import dashboard_api as derive  # noqa: E402  (the real derive — reuse, no drift)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ev(task_id, ts, *, event_type='pr_opened', agent='forge',
        pr_url=None, repo='ourliberty-agent-core', branch='forge/x'):
    payload = {}
    if repo:
        payload['repo'] = repo
    if branch:
        payload['branch'] = branch
    return {'task_id': task_id, 'event_type': event_type, 'ts': _iso(ts),
            'agent': agent, 'pr_url': pr_url, 'payload': payload}


# ---------------------------------------------------------- proposal selection


class SelectProposableTest(unittest.TestCase):
    def test_terminal_orphan_skipped(self):
        orphans = [{'task_id': 't1', 'terminal': True, 'pr_url': None}]
        self.assertEqual(h.select_proposable_orphans(orphans, {}), [])

    def test_non_terminal_prless_orphan_proposed(self):
        orphans = [{'task_id': 't1', 'terminal': False, 'pr_url': None}]
        out = h.select_proposable_orphans(orphans, {})
        self.assertEqual([o['task_id'] for o in out], ['t1'])

    def test_indeterminate_pr_state_skipped(self):
        # Has a pr_url but it's NOT in the resolved map -> indeterminate -> skip.
        url = 'https://github.com/o/r/pull/9'
        orphans = [{'task_id': 't1', 'terminal': False, 'pr_url': url}]
        self.assertEqual(h.select_proposable_orphans(orphans, {}), [])

    def test_resolved_open_pr_proposed(self):
        url = 'https://github.com/o/r/pull/9'
        orphans = [{'task_id': 't1', 'terminal': False, 'pr_url': url}]
        out = h.select_proposable_orphans(orphans, {url: 'OPEN'})
        self.assertEqual([o['task_id'] for o in out], ['t1'])


class BuildProposedEntryTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 12, 9, 30, tzinfo=timezone.utc)

    def test_entry_shape(self):
        orphan = {'task_id': 'orphan-foo', 'label': 'Orphan Foo',
                  'repo': 'ourliberty-agent-core', 'branch': 'forge/foo',
                  'last_event_ts': '2026-06-10T00:00:00+00:00'}
        e = h.build_proposed_entry(orphan, self.now)
        self.assertEqual(e['id'], 'proposed-orphan-foo')
        self.assertEqual(e['phase'], 'proposed')
        self.assertEqual(e['task_ids'], ['orphan-foo'])  # idempotency anchor
        self.assertEqual(e['name'], 'Orphan Foo')
        self.assertEqual(e['repo'], 'ourliberty-agent-core')
        self.assertEqual(e['orphan_branch'], 'forge/foo')
        self.assertEqual(e['created'], '2026-06-12')
        self.assertEqual(e['proposed_by'], 'heal_orphan_autoregister')
        self.assertIsNone(e['deferred_reason'])

    def test_label_falls_back_to_task_id(self):
        e = h.build_proposed_entry({'task_id': 'x'}, self.now)
        self.assertEqual(e['name'], 'x')


# ---------------------------------------------------------- registry helpers


class RegistryHelpersTest(unittest.TestCase):
    def test_registered_task_ids_collects_all_phases(self):
        reg = {'missions': [
            {'id': 'm1', 'task_ids': ['a', 'b']},
            {'id': 'proposed-c', 'phase': 'proposed', 'task_ids': ['c']},
            {'id': 'bad', 'task_ids': 'not-a-list'},
        ]}
        self.assertEqual(h.registered_task_ids(reg), {'a', 'b', 'c'})

    def test_read_missing_returns_empty(self):
        tmp = tempfile.mkdtemp()
        try:
            reg = h.read_missions_registry(Path(tmp) / 'missions.json')
            self.assertEqual(reg, {'schema_version': 1, 'missions': []})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_malformed_returns_none(self):
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / 'missions.json'
            p.write_text('{not json')
            self.assertIsNone(h.read_missions_registry(p))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_wrong_shape_returns_none(self):
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / 'missions.json'
            p.write_text(json.dumps({'missions': 'not-a-list'}))
            self.assertIsNone(h.read_missions_registry(p))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_atomic_write_round_trip(self):
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / 'missions.json'
            reg = {'schema_version': 1, 'missions': [{'id': 'x'}]}
            h.atomic_write_missions(p, reg)
            self.assertEqual(json.loads(p.read_text()), reg)
            leftovers = [f for f in os.listdir(tmp) if f != 'missions.json']
            self.assertEqual(leftovers, [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------- scan_and_propose against the REAL derive


class ScanAndProposeTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)

    def _empty_reg(self):
        return {'schema_version': 1, 'missions': []}

    def test_proposes_non_terminal_prless_orphan(self):
        reg = self._empty_reg()
        rows = [_ev('orphan-1', self.now - timedelta(hours=2),
                    event_type='session_start', pr_url=None)]
        res = h.scan_and_propose(reg, rows, derive, self.now,
                                 pr_state_resolver=lambda urls: {})
        self.assertEqual([tid for tid, _ in res.proposed], ['orphan-1'])
        self.assertEqual(reg['missions'][0]['id'], 'proposed-orphan-1')
        self.assertEqual(reg['missions'][0]['phase'], 'proposed')

    def test_infrastructure_orphan_never_proposed(self):
        # `notify-` prefix is infrastructure per the SHARED is_infrastructure_task.
        reg = self._empty_reg()
        rows = [_ev('notify-something', self.now - timedelta(hours=2),
                    agent='deploy-notifier', pr_url=None)]
        res = h.scan_and_propose(reg, rows, derive, self.now,
                                 pr_state_resolver=lambda urls: {})
        self.assertEqual(res.proposed, [])
        self.assertEqual(reg['missions'], [])

    def test_terminal_merged_orphan_skipped(self):
        reg = self._empty_reg()
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/500'
        rows = [_ev('orphan-merged', self.now - timedelta(hours=2), pr_url=url)]
        res = h.scan_and_propose(reg, rows, derive, self.now,
                                 pr_state_resolver=lambda urls: {url: 'MERGED'})
        self.assertEqual(res.proposed, [])
        self.assertEqual(res.skipped_terminal_or_indeterminate, 1)

    def test_indeterminate_pr_state_skipped(self):
        # pr_url present but resolver can't reach it -> err toward NOT proposing.
        reg = self._empty_reg()
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/501'
        rows = [_ev('orphan-unknown', self.now - timedelta(hours=2), pr_url=url)]
        res = h.scan_and_propose(reg, rows, derive, self.now,
                                 pr_state_resolver=lambda urls: {})
        self.assertEqual(res.proposed, [])
        self.assertEqual(res.skipped_terminal_or_indeterminate, 1)

    def test_already_registered_orphan_not_reproposed_idempotent(self):
        # A proposed entry registering the task_id -> detect_orphans excludes it.
        reg = {'schema_version': 1, 'missions': [
            {'id': 'proposed-orphan-1', 'phase': 'proposed',
             'task_ids': ['orphan-1']},
        ]}
        rows = [_ev('orphan-1', self.now - timedelta(hours=2),
                    event_type='session_start', pr_url=None)]
        res = h.scan_and_propose(reg, rows, derive, self.now,
                                 pr_state_resolver=lambda urls: {})
        self.assertEqual(res.proposed, [])
        self.assertEqual(len(reg['missions']), 1)  # nothing appended

    def test_accepted_orphan_not_reproposed(self):
        # Accept flips phase proposed->drafting but keeps the task_id registered.
        reg = {'schema_version': 1, 'missions': [
            {'id': 'proposed-orphan-1', 'phase': 'drafting',
             'task_ids': ['orphan-1']},
        ]}
        rows = [_ev('orphan-1', self.now - timedelta(hours=2),
                    event_type='session_start', pr_url=None)]
        res = h.scan_and_propose(reg, rows, derive, self.now,
                                 pr_state_resolver=lambda urls: {})
        self.assertEqual(res.proposed, [])

    def test_double_tick_is_idempotent(self):
        reg = self._empty_reg()
        rows = [_ev('orphan-1', self.now - timedelta(hours=2),
                    event_type='session_start', pr_url=None)]
        h.scan_and_propose(reg, rows, derive, self.now,
                           pr_state_resolver=lambda urls: {})
        # Second tick over the same registry: detect_orphans now excludes orphan-1.
        res2 = h.scan_and_propose(reg, rows, derive, self.now,
                                  pr_state_resolver=lambda urls: {})
        self.assertEqual(res2.proposed, [])
        self.assertEqual(len(reg['missions']), 1)

    def test_non_buildable_orphan_not_proposed(self):
        # Each is an orphan (detect_orphans surfaces it) but NOT a buildable
        # initiative -> the is_proposable_initiative filter drops it at propose time.
        for tid in ('desktop-05a159bb', 'pipeline-stall:clarify1-suppress',
                    'real-clr', 'seq-x-step-y', 'weekly-2026-06-08'):
            reg = self._empty_reg()
            rows = [_ev(tid, self.now - timedelta(hours=2),
                        event_type='session_start', pr_url=None)]
            res = h.scan_and_propose(reg, rows, derive, self.now,
                                     pr_state_resolver=lambda urls: {})
            self.assertEqual(res.proposed, [], tid)
            self.assertEqual(reg['missions'], [], tid)
            self.assertEqual(res.skipped_non_proposable, 1, tid)


# --------------------------------------------- retirement (self-clean the lane)


class RetireStaleProposalsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)

    def _proposed(self, entry_id, task_id, **extra):
        e = {'id': entry_id, 'phase': 'proposed', 'task_ids': [task_id],
             'proposed_by': 'heal_orphan_autoregister'}
        e.update(extra)
        return e

    def test_retires_entry_failing_filter(self):
        # A proposed entry whose task_id is a noise category (would no longer pass
        # the filter) is retired with audit; phase stays proposed, never deleted.
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-desktop-05a159bb', 'desktop-05a159bb'),
        ]}
        retired = h.retire_stale_proposals(reg, [], derive, self.now,
                                           pr_state_resolver=lambda urls: {})
        self.assertEqual([eid for eid, _ in retired], ['proposed-desktop-05a159bb'])
        entry = reg['missions'][0]
        self.assertTrue(entry['acknowledged'])
        self.assertEqual(entry['phase'], 'proposed')           # not hard-deleted
        self.assertEqual(entry['retired_by'], 'heal_orphan_autoregister')
        self.assertEqual(entry['retired_reason'], 'filter-non-buildable')
        self.assertEqual(entry['retired_at'], self.now.isoformat())

    def test_retires_terminal_merged_orphan(self):
        # A buildable proposal whose orphan PR has merged is terminal -> retire.
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/53'
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-p3-dashboard-proposed-lane',
                           'p3-dashboard-proposed-lane'),
        ]}
        rows = [_ev('p3-dashboard-proposed-lane', self.now - timedelta(days=2),
                    event_type='pr_opened', pr_url=url)]
        retired = h.retire_stale_proposals(reg, rows, derive, self.now,
                                           pr_state_resolver=lambda urls: {url: 'MERGED'})
        self.assertEqual([r for _, r in retired], ['orphan-terminal'])
        self.assertTrue(reg['missions'][0]['acknowledged'])

    def test_preserves_live_buildable_proposal(self):
        # Passes the filter, PR still OPEN -> NOT terminal -> KEEP (err toward not
        # retiring an ambiguous still-live buildable).
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/77'
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-auth-setup-token-wiring', 'auth-setup-token-wiring'),
        ]}
        rows = [_ev('auth-setup-token-wiring', self.now - timedelta(hours=3),
                    event_type='pr_opened', pr_url=url)]
        retired = h.retire_stale_proposals(reg, rows, derive, self.now,
                                           pr_state_resolver=lambda urls: {url: 'OPEN'})
        self.assertEqual(retired, [])
        self.assertNotIn('acknowledged', reg['missions'][0])

    def test_preserves_buildable_with_no_events_indeterminate(self):
        # Passes the filter, no events in window -> indeterminate -> KEEP.
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-catalog-accuracy-drift', 'catalog-accuracy-drift'),
        ]}
        retired = h.retire_stale_proposals(reg, [], derive, self.now,
                                           pr_state_resolver=lambda urls: {})
        self.assertEqual(retired, [])
        self.assertNotIn('acknowledged', reg['missions'][0])

    def test_preserves_buildable_with_indeterminate_pr_state(self):
        # Passes filter, has a PR but its live state could NOT be resolved -> KEEP.
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/88'
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-auth-setup-token-wiring', 'auth-setup-token-wiring'),
        ]}
        rows = [_ev('auth-setup-token-wiring', self.now - timedelta(hours=3),
                    event_type='pr_opened', pr_url=url)]
        retired = h.retire_stale_proposals(reg, rows, derive, self.now,
                                           pr_state_resolver=lambda urls: {})  # unresolved
        self.assertEqual(retired, [])

    def test_already_acknowledged_not_retouched_idempotent(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-desktop-05a159bb', 'desktop-05a159bb',
                           acknowledged=True, retired_at='2026-06-13T00:00:00+00:00'),
        ]}
        retired = h.retire_stale_proposals(reg, [], derive, self.now,
                                           pr_state_resolver=lambda urls: {})
        self.assertEqual(retired, [])
        # The prior retire timestamp is untouched (no churn on re-run).
        self.assertEqual(reg['missions'][0]['retired_at'], '2026-06-13T00:00:00+00:00')

    def test_ignores_entries_not_proposed_by_healer(self):
        reg = {'schema_version': 1, 'missions': [
            {'id': 'm-manual', 'phase': 'proposed', 'task_ids': ['desktop-05a159bb']},
            {'id': 'm-drafting', 'phase': 'drafting', 'task_ids': ['desktop-07e97ba7'],
             'proposed_by': 'heal_orphan_autoregister'},
        ]}
        retired = h.retire_stale_proposals(reg, [], derive, self.now,
                                           pr_state_resolver=lambda urls: {})
        self.assertEqual(retired, [])

    def test_surviving_proposed_ids_excludes_acknowledged(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-live', 'auth-setup-token-wiring'),
            self._proposed('proposed-dead', 'desktop-05a159bb', acknowledged=True),
            {'id': 'm-drafting', 'phase': 'drafting', 'task_ids': ['x']},
        ]}
        self.assertEqual(h.surviving_proposed_ids(reg), ['proposed-live'])


# --------------------------------------- commit + push (real temp git repo)


class CommitAndPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='autoreg-git-test-')
        self.repo = Path(self.tmp) / 'repo'
        self.repo.mkdir()
        self._git('init', '-q', '-b', 'main')
        self._git('config', 'user.email', 'test@test')
        self._git('config', 'user.name', 'Test')
        (self.repo / 'agents' / 'beacon').mkdir(parents=True)
        self.missions = self.repo / h.MISSIONS_REL
        self.missions.write_text(json.dumps({'schema_version': 1, 'missions': []}) + '\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'seed')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=str(self.repo), check=True,
                       capture_output=True, text=True)

    def test_nothing_when_clean(self):
        self.assertEqual(h.commit_and_push_missions(self.repo, 'audit'), 'nothing')

    def test_refuses_off_main(self):
        self._git('checkout', '-q', '-b', 'forge/feature')
        self.missions.write_text(json.dumps({'schema_version': 1,
                                             'missions': [{'id': 'x'}]}) + '\n')
        self.assertEqual(h.commit_and_push_missions(self.repo, 'audit'), 'wrong-branch')

    def test_commits_delta_then_clean(self):
        self.missions.write_text(json.dumps({'schema_version': 1,
                                             'missions': [{'id': 'x'}]}) + '\n')
        status = h.commit_and_push_missions(self.repo, 'audit-line')
        self.assertEqual(status, 'push-failed')  # no origin remote in the test
        log = subprocess.run(['git', 'log', '-1', '--pretty=%B'],
                             cwd=str(self.repo), capture_output=True, text=True)
        self.assertIn('autoregister healer', log.stdout)
        self.assertIn('audit-line', log.stdout)
        self.assertEqual(h.commit_and_push_missions(self.repo, 'audit'), 'nothing')


# ----------------------------------------------------- run_once integration


class RunOnceTest(unittest.TestCase):
    def test_unresolved_path_is_noop(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(h, 'load_repo_paths', return_value={}):
            rc = h.run_once(dry_run=False, events_fetcher=lambda: [],
                            derive=derive, now=now)
        self.assertEqual(rc, 0)

    def test_unavailable_chain_events_proposes_nothing(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-autoreg-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.MISSIONS_REL).write_text(
                json.dumps({'schema_version': 1, 'missions': []}) + '\n')
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}):
                rc = h.run_once(dry_run=False, events_fetcher=lambda: None,
                                derive=derive, now=now)
            self.assertEqual(rc, 0)
            # registry untouched (no events -> nothing proposed)
            reg = json.loads((core / h.MISSIONS_REL).read_text())
            self.assertEqual(reg['missions'], [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dry_run_appends_nothing_to_disk(self):
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.mkdtemp(prefix='run-once-autoreg-dry-')
        try:
            core = Path(tmp) / 'agent-core'
            (core / 'agents' / 'beacon').mkdir(parents=True)
            (core / h.MISSIONS_REL).write_text(
                json.dumps({'schema_version': 1, 'missions': []}) + '\n')
            rows = [_ev('orphan-1', now - timedelta(hours=2),
                        event_type='session_start', pr_url=None)]
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': core}):
                rc = h.run_once(dry_run=True, events_fetcher=lambda: rows,
                                derive=derive, pr_state_resolver=lambda urls: {},
                                now=now)
            self.assertEqual(rc, 0)
            # dry-run: the proposal is computed but NOT written to disk.
            reg = json.loads((core / h.MISSIONS_REL).read_text())
            self.assertEqual(reg['missions'], [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
