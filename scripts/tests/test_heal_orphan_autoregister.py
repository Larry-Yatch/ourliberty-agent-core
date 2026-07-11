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

import contextlib
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

    def test_sequence_owned_seq_id_and_steps_not_proposed(self):
        # projects-v3-p4 (a bare/complete sequence seq_id) and its step ids each
        # PASS is_proposable_initiative, so only the sequence-owned collapse keeps
        # the catcher from auto-proposing them (the live #655 follow-up symptom).
        owned = {'projects-v3-p4', 'p4-cleanup-committer', 'p4-postmerge-exec'}
        for tid in owned:
            reg = self._empty_reg()
            rows = [_ev(tid, self.now - timedelta(hours=2),
                        event_type='session_start', pr_url=None)]
            res = h.scan_and_propose(reg, rows, derive, self.now,
                                     pr_state_resolver=lambda urls: {},
                                     collapsed_task_ids=owned)
            self.assertEqual(res.proposed, [], tid)
            self.assertEqual(reg['missions'], [], tid)

    def test_without_collapse_the_seq_id_would_be_proposed(self):
        # Proves the collapse is the mechanism: drop the collapse set and the very
        # same seq_id surfaces as a proposed orphan (prior behavior, the bug).
        reg = self._empty_reg()
        rows = [_ev('projects-v3-p4', self.now - timedelta(hours=2),
                    event_type='session_start', pr_url=None)]
        res = h.scan_and_propose(reg, rows, derive, self.now,
                                 pr_state_resolver=lambda urls: {})
        self.assertEqual([tid for tid, _ in res.proposed], ['projects-v3-p4'])


# --------------------------------------------- retirement (self-clean the lane)


class RetireStaleProposalsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
        # The reliable gh terminal gate shells `gh` — an effectful edge. Seam it OFF
        # by default (loader returns a no-op gate) so the event-only / filter
        # assertions never touch gh. The gh-gate tests below inject `terminal_gate=`
        # explicitly, which takes precedence over this loader.
        _gate = mock.patch.object(h, '_load_terminal_gate',
                                  return_value=lambda tids: (False, None))
        _gate.start()
        self.addCleanup(_gate.stop)

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

    def test_retires_sequence_owned_proposal(self):
        # The live #655 follow-up: a proposal whose task_id is a sequence seq_id is
        # retired with reason 'sequence-owned' (additive ack + provenance, phase
        # stays proposed, never hard-deleted).
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-projects-v3-p4', 'projects-v3-p4'),
        ]}
        retired = h.retire_stale_proposals(
            reg, [], derive, self.now,
            pr_state_resolver=lambda urls: {},
            collapsed_task_ids={'projects-v3-p4', 'p4-cleanup-committer'})
        self.assertEqual(retired, [('proposed-projects-v3-p4', 'sequence-owned')])
        entry = reg['missions'][0]
        self.assertTrue(entry['acknowledged'])
        self.assertEqual(entry['phase'], 'proposed')            # not hard-deleted
        self.assertEqual(entry['retired_by'], 'heal_orphan_autoregister')
        self.assertEqual(entry['retired_reason'], 'sequence-owned')

    def test_sequence_owned_retire_is_idempotent(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-projects-v3-p4', 'projects-v3-p4'),
        ]}
        owned = {'projects-v3-p4'}
        first = h.retire_stale_proposals(reg, [], derive, self.now,
                                         pr_state_resolver=lambda urls: {},
                                         collapsed_task_ids=owned)
        self.assertEqual([r for _, r in first], ['sequence-owned'])
        stamp = reg['missions'][0]['retired_at']
        second = h.retire_stale_proposals(reg, [], derive, self.now,
                                          pr_state_resolver=lambda urls: {},
                                          collapsed_task_ids=owned)
        self.assertEqual(second, [])                            # no re-touch
        self.assertEqual(reg['missions'][0]['retired_at'], stamp)

    def test_sequence_owned_keeps_entry_with_a_non_owned_member(self):
        # Retire only when EVERY task_id is sequence-owned; a mixed entry is kept.
        reg = {'schema_version': 1, 'missions': [
            {'id': 'proposed-mix', 'phase': 'proposed',
             'task_ids': ['projects-v3-p4', 'auth-setup-token-wiring'],
             'proposed_by': 'heal_orphan_autoregister'},
        ]}
        retired = h.retire_stale_proposals(reg, [], derive, self.now,
                                           pr_state_resolver=lambda urls: {},
                                           collapsed_task_ids={'projects-v3-p4'})
        self.assertEqual(retired, [])
        self.assertNotIn('acknowledged', reg['missions'][0])

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

    # -- the RELIABLE gh terminal gate: drains shipped work the event-only path is
    #    blind to (no auto_merge event + no resolvable pr_url). Positive evidence
    #    only, bounded per tick, fail-safe.

    def test_gh_gate_retires_indeterminate_terminal(self):
        # Buildable, passes the filter, NO events (event-only path indeterminate) —
        # but the gh gate finds the work shipped -> retire 'orphan-terminal' + signal.
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-p2-digest-generator', 'p2-digest-generator'),
        ]}
        retired = h.retire_stale_proposals(
            reg, [], derive, self.now,
            pr_state_resolver=lambda urls: {},
            terminal_gate=lambda tids: (True, 'pr_merged:#252'))
        self.assertEqual(retired, [('proposed-p2-digest-generator', 'orphan-terminal')])
        entry = reg['missions'][0]
        self.assertTrue(entry['acknowledged'])
        self.assertEqual(entry['phase'], 'proposed')          # relabel, never delete
        self.assertEqual(entry['retired_reason'], 'orphan-terminal')
        self.assertEqual(entry['retired_signal'], 'pr_merged:#252')

    def test_gh_gate_keeps_when_not_terminal(self):
        # gh gate returns not-terminal (open / unresolvable) -> KEEP (fail-safe).
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-auth-setup-token-wiring', 'auth-setup-token-wiring'),
        ]}
        retired = h.retire_stale_proposals(
            reg, [], derive, self.now,
            pr_state_resolver=lambda urls: {},
            terminal_gate=lambda tids: (False, None))
        self.assertEqual(retired, [])
        self.assertNotIn('acknowledged', reg['missions'][0])

    def test_gh_gate_error_keeps_failsafe(self):
        # A gate that RAISES must never manufacture a drop.
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-auth-setup-token-wiring', 'auth-setup-token-wiring'),
        ]}
        def _boom(tids):
            raise RuntimeError('gh exploded')
        retired = h.retire_stale_proposals(
            reg, [], derive, self.now,
            pr_state_resolver=lambda urls: {}, terminal_gate=_boom)
        self.assertEqual(retired, [])
        self.assertNotIn('acknowledged', reg['missions'][0])

    def test_gh_gate_checks_every_candidate_no_budget(self):
        # The gate matches in-memory over a once-fetched PR index (cheap), so EVERY
        # candidate is checked each tick — no per-candidate budget, no starvation of
        # terminal entries behind still-live ones.
        calls = []
        def _gate(tids):
            calls.append(tids[0])
            return (True, 'pr_merged:#1')
        reg = {'schema_version': 1, 'missions': [
            self._proposed(f'proposed-buildable-task-{i}', f'buildable-task-{i}')
            for i in range(5)
        ]}
        retired = h.retire_stale_proposals(
            reg, [], derive, self.now,
            pr_state_resolver=lambda urls: {}, terminal_gate=_gate)
        self.assertEqual(len(calls), 5)          # every candidate gated, none skipped
        self.assertEqual(len(retired), 5)
        self.assertEqual(sum(1 for m in reg['missions'] if m.get('acknowledged')), 5)

    def test_gh_gate_not_consulted_when_event_path_resolves(self):
        # When the event-only path already decides terminal, the gh gate is NOT
        # consulted (no wasted gh round-trip).
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/53'
        called = []
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-p3-dashboard-proposed-lane',
                           'p3-dashboard-proposed-lane'),
        ]}
        rows = [_ev('p3-dashboard-proposed-lane', self.now - timedelta(days=2),
                    event_type='pr_opened', pr_url=url)]
        retired = h.retire_stale_proposals(
            reg, rows, derive, self.now,
            pr_state_resolver=lambda urls: {url: 'MERGED'},
            terminal_gate=lambda tids: called.append(tids) or (True, 'x'))
        self.assertEqual([r for _, r in retired], ['orphan-terminal'])
        self.assertEqual(called, [])           # event path resolved -> gate untouched


class TerminalGateTest(unittest.TestCase):
    """The reach-back-independent PR-state terminal gate: a batched fetch (once per
    tick, deep window) + in-memory branch/title match reusing the SHARED matcher
    task_resolution.pr_matches_task — so old shipped work whose PR aged out of the
    event-only path is still retired, with NO per-task gh fan-out."""

    @staticmethod
    def _idx(number, branch, state, title='x'):
        return ({'number': number, 'headRefName': branch, 'state': state,
                 'title': title}, state.lower())

    def test_index_gate_matches_merged_by_branch(self):
        gate = h._terminal_gate_from_index(
            [self._idx(252, 'forge/p2-digest-generator', 'MERGED')])
        self.assertEqual(gate(['p2-digest-generator']), (True, 'pr_merged:#252'))

    def test_index_gate_matches_closed_unmerged(self):
        gate = h._terminal_gate_from_index(
            [self._idx(99, 'forge/abandoned-task', 'CLOSED')])
        self.assertEqual(gate(['abandoned-task']), (True, 'pr_closed:#99'))

    def test_index_gate_no_match_keeps(self):
        gate = h._terminal_gate_from_index(
            [self._idx(1, 'forge/other', 'MERGED', title='unrelated')])
        self.assertEqual(gate(['not-in-index']), (False, None))

    def test_index_gate_all_task_ids_must_match(self):
        # EVERY task_id must be terminal; one unmatched -> not terminal -> KEEP.
        gate = h._terminal_gate_from_index(
            [self._idx(5, 'forge/task-a', 'MERGED')])
        self.assertFalse(gate(['task-a', 'task-b'])[0])

    def test_fetch_returns_none_when_all_gh_fail(self):
        # A total gh outage -> None -> the tick degrades to the event-only path
        # (fail-safe: never a false drop).
        with mock.patch('subprocess.run',
                        side_effect=FileNotFoundError('gh missing')):
            self.assertIsNone(h._fetch_terminal_prs(('o/repo-a', 'o/repo-b')))

    def test_fetch_builds_index_on_success(self):
        def _fake_run(cmd, **kw):
            state = cmd[cmd.index('--state') + 1]
            out = json.dumps(
                [{'number': 7, 'headRefName': 'forge/t', 'state': 'MERGED',
                  'title': 'x'}]) if state == 'merged' else '[]'
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr='')
        with mock.patch('subprocess.run', side_effect=_fake_run):
            index = h._fetch_terminal_prs(('o/repo-a',))
        self.assertIsNotNone(index)
        self.assertTrue(any(pr['number'] == 7 for pr, _ in index))

    def test_index_gate_matches_forge_iteration_branch(self):
        # The real backlog case: a merged Forge re-attempt branch carries an extra
        # `-002` the proposal's task_id lacks. The gate (via the fixed matcher) now
        # retires it instead of keeping it forever.
        gate = h._terminal_gate_from_index(
            [self._idx(53, 'forge/p3-dashboard-proposed-lane-002', 'MERGED')])
        self.assertEqual(
            gate(['p3-dashboard-proposed-lane']), (True, 'pr_merged:#53'))


# ------------------------------- flag stuck proposals (the un-retirable residue)


class FlagStuckProposalsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)

    @staticmethod
    def _keep_gate(_tids):
        return (False, None)            # nothing matches -> nothing auto-retires

    @staticmethod
    def _match_gate(_tids):
        return (True, 'pr_merged:#1')   # the gate matches -> retire, not flag

    def _proposed(self, entry_id, created, **extra):
        e = {'id': entry_id, 'phase': 'proposed', 'task_ids': [entry_id],
             'proposed_by': 'heal_orphan_autoregister', 'created': created}
        e.update(extra)
        return e

    def test_flags_old_unmatched(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-stuck', '2026-06-01'),   # 21d old
        ]}
        flagged = h.flag_stuck_proposals(reg, self.now, terminal_gate=self._keep_gate)
        self.assertEqual(flagged, ['proposed-stuck'])
        e = reg['missions'][0]
        self.assertIs(e['needs_decision'], True)
        self.assertIn('keep or drop', e['needs_decision_reason'])
        self.assertEqual(e['needs_decision_since'], self.now.isoformat())
        self.assertEqual(e['phase'], 'proposed')   # surfaced, NOT retired

    def test_skips_young(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-fresh', '2026-06-20'),   # 2d old
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=self._keep_gate), [])
        self.assertNotIn('needs_decision', reg['missions'][0])

    def test_skips_gate_matched(self):
        # An old card the gate matches is about to be retired -> not "stuck".
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-shipped', '2026-06-01'),
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=self._match_gate), [])
        self.assertNotIn('needs_decision', reg['missions'][0])

    def test_covers_all_owners_including_closeout(self):
        # No proposed_by guard: a closeout-authored deferred note (which retire's
        # ownership guard skips) still gets surfaced for a decision.
        reg = {'schema_version': 1, 'missions': [
            self._proposed('closeout-note', '2026-06-01', proposed_by='closeout'),
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=self._keep_gate),
            ['closeout-note'])

    def test_skips_retrospective_author_owned(self):
        # The weekly retrospective author manages its own re-proposal/dismiss
        # lifecycle, so its standing proposals (empty task_ids by design) must
        # NOT get the 14d stuck flag — that would re-clutter the needs-you lane
        # this alert-pipeline-rework declutters (review #749 finding 2).
        reg = {'schema_version': 1, 'missions': [
            {'id': 'retrospective-sig-2026-06-15', 'phase': 'proposed',
             'task_ids': [], 'proposed_by': 'retrospective-author',
             'created': '2026-06-01'},
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=self._keep_gate), [])
        self.assertNotIn('needs_decision', reg['missions'][0])

    def test_idempotent_already_flagged(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-stuck', '2026-06-01', needs_decision=True),
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=self._keep_gate), [])

    def test_skips_acknowledged(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-dismissed', '2026-06-01', acknowledged=True),
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=self._keep_gate), [])

    def test_undateable_created_skipped(self):
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-nodate', 'not-a-date'),
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=self._keep_gate), [])

    def test_gate_error_is_failsafe_skip(self):
        def _boom(_tids):
            raise RuntimeError('gate exploded')
        reg = {'schema_version': 1, 'missions': [
            self._proposed('proposed-stuck', '2026-06-01'),
        ]}
        self.assertEqual(
            h.flag_stuck_proposals(reg, self.now, terminal_gate=_boom), [])
        self.assertNotIn('needs_decision', reg['missions'][0])


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
    def setUp(self):
        # run_once builds the reliable terminal gate (a gh fetch) once per tick;
        # seam it OFF so these integration tests stay hermetic (no network).
        _gate = mock.patch.object(h, '_load_terminal_gate',
                                  return_value=lambda tids: (False, None))
        _gate.start()
        self.addCleanup(_gate.stop)

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
                                   return_value={'ourliberty-agent-core': core}), \
                    mock.patch.object(h, 'list_open_new_mission_prs', return_value=[]):
                rc = h.run_once(dry_run=False, events_fetcher=lambda: None,
                                derive=derive, now=now)
            self.assertEqual(rc, 0)
            # registry untouched (no events -> nothing proposed; ingest still runs
            # but there are no new-mission PRs)
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


# ----------------------------------------------------- new-mission PR ingestion


def _mentry(mid='m1', name='M1', phase='drafting'):
    return {'id': mid, 'name': name, 'phase': phase, 'task_ids': [],
            'repo': 'ourliberty-agent-core', 'created': '2026-06-15',
            'deferred_reason': None}


class NewMissionEnableTest(unittest.TestCase):
    def test_default_disabled_soak(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.ENV_NEWMISSION_INGEST, None)
            self.assertFalse(h.newmission_ingest_enabled())

    def test_explicit_true_enables(self):
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}):
            self.assertTrue(h.newmission_ingest_enabled())

    def test_false_and_other_values_disabled(self):
        for v in ('false', 'yes', '1', ''):
            with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: v}):
                self.assertFalse(h.newmission_ingest_enabled(), v)


class ListOpenNewMissionPrsTest(unittest.TestCase):
    def _proc(self, rows):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr='')

    def test_filters_to_prefix(self):
        rows = [
            {'number': 1, 'headRefName': 'feat/new-mission-alpha'},
            {'number': 2, 'headRefName': 'forge/some-task'},          # excluded
            {'number': 3, 'headRefName': 'feat/new-mission-beta'},
            {'number': 4, 'headRefName': None},                        # excluded
        ]
        with mock.patch.object(h, '_gh', return_value=self._proc(rows)):
            out = h.list_open_new_mission_prs('r/r')
        self.assertEqual(out, [
            {'number': 1, 'branch': 'feat/new-mission-alpha'},
            {'number': 3, 'branch': 'feat/new-mission-beta'},
        ])

    def test_gh_failure_returns_empty(self):
        bad = subprocess.CompletedProcess([], 1, stdout='', stderr='boom')
        with mock.patch.object(h, '_gh', return_value=bad):
            self.assertEqual(h.list_open_new_mission_prs('r/r'), [])


class FetchBranchMissionEntryTest(unittest.TestCase):
    def _content_proc(self, registry):
        import base64
        b64 = base64.b64encode(json.dumps(registry).encode()).decode()
        # GitHub returns base64 with embedded newlines; simulate that.
        chunked = '\n'.join(b64[i:i + 60] for i in range(0, len(b64), 60))
        return subprocess.CompletedProcess([], 0, stdout=chunked + '\n', stderr='')

    def test_returns_named_entry(self):
        reg = {'missions': [_mentry('alpha'), _mentry('beta')]}
        with mock.patch.object(h, '_gh', return_value=self._content_proc(reg)):
            entry = h.fetch_branch_mission_entry('r/r', 'feat/new-mission-beta', 'beta')
        self.assertEqual(entry['id'], 'beta')

    def test_missing_id_returns_none(self):
        reg = {'missions': [_mentry('alpha')]}
        with mock.patch.object(h, '_gh', return_value=self._content_proc(reg)):
            self.assertIsNone(
                h.fetch_branch_mission_entry('r/r', 'feat/new-mission-zzz', 'zzz'))

    def test_gh_failure_returns_none(self):
        bad = subprocess.CompletedProcess([], 1, stdout='', stderr='404')
        with mock.patch.object(h, '_gh', return_value=bad):
            self.assertIsNone(
                h.fetch_branch_mission_entry('r/r', 'feat/new-mission-x', 'x'))


class IngestNewMissionPrsTest(unittest.TestCase):
    def _patch(self, prs, fetch):
        return (
            mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs),
            mock.patch.object(h, 'fetch_branch_mission_entry', side_effect=fetch),
        )

    def test_appends_new_mission_and_marks_closeable(self):
        reg = {'schema_version': 1, 'missions': []}
        prs = [{'number': 512, 'branch': 'feat/new-mission-foo'}]
        p1, p2 = self._patch(prs, lambda r, b, mid: _mentry('foo'))
        with p1, p2:
            ingested, closeable = h.ingest_new_mission_prs(reg, 'r/r')
        self.assertEqual([m['id'] for m in reg['missions']], ['foo'])
        self.assertEqual(ingested, [(512, 'foo')])
        self.assertEqual(closeable, [(512, 'foo')])

    def test_duplicate_id_not_appended_but_closeable(self):
        reg = {'schema_version': 1, 'missions': [_mentry('foo')]}
        prs = [{'number': 512, 'branch': 'feat/new-mission-foo'}]
        # fetch must not even be needed for a dup; assert it's not called.
        fetch = mock.Mock(side_effect=AssertionError('should not fetch a dup'))
        with mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs), \
                mock.patch.object(h, 'fetch_branch_mission_entry', fetch):
            ingested, closeable = h.ingest_new_mission_prs(reg, 'r/r')
        self.assertEqual(len(reg['missions']), 1)         # unchanged
        self.assertEqual(ingested, [])
        self.assertEqual(closeable, [(512, 'foo')])       # close the redundant PR

    def test_fetch_failure_leaves_pr_open(self):
        reg = {'schema_version': 1, 'missions': []}
        prs = [{'number': 9, 'branch': 'feat/new-mission-bar'}]
        p1, p2 = self._patch(prs, lambda r, b, mid: None)
        with p1, p2:
            ingested, closeable = h.ingest_new_mission_prs(reg, 'r/r')
        self.assertEqual(reg['missions'], [])
        self.assertEqual(ingested, [])
        self.assertEqual(closeable, [])                   # NOT closeable — retry

    def test_malformed_entry_leaves_pr_open(self):
        reg = {'schema_version': 1, 'missions': []}
        prs = [{'number': 9, 'branch': 'feat/new-mission-bar'}]
        # id mismatch (smuggled different id) — must be rejected by the guard.
        p1, p2 = self._patch(prs, lambda r, b, mid: _mentry('OTHER'))
        with p1, p2:
            ingested, closeable = h.ingest_new_mission_prs(reg, 'r/r')
        self.assertEqual(reg['missions'], [])
        self.assertEqual(closeable, [])

    def test_bounded_per_tick(self):
        reg = {'schema_version': 1, 'missions': []}
        prs = [{'number': i, 'branch': f'feat/new-mission-m{i}'} for i in range(5)]
        with mock.patch.object(h, 'NEWMISSION_MAX_PER_TICK', 2), \
                mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs), \
                mock.patch.object(h, 'fetch_branch_mission_entry',
                                  side_effect=lambda r, b, mid: _mentry(mid)):
            ingested, _ = h.ingest_new_mission_prs(reg, 'r/r')
        self.assertEqual(len(ingested), 2)


class RunOnceIngestTest(unittest.TestCase):
    """run_once ingests + closes only when ENABLED and only for missions confirmed
    on origin/main; defaults to observe-only; tolerates gh failure."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='run-once-ingest-')
        self.core = Path(self.tmp) / 'agent-core'
        (self.core / 'agents' / 'beacon').mkdir(parents=True)
        (self.core / h.MISSIONS_REL).write_text(
            json.dumps({'schema_version': 1, 'missions': []}) + '\n')
        self.now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        # Seam off the terminal-gate gh fetch run_once builds each tick (hermetic).
        _gate = mock.patch.object(h, '_load_terminal_gate',
                                  return_value=lambda tids: (False, None))
        _gate.start()
        self.addCleanup(_gate.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return h.run_once(dry_run=False, events_fetcher=lambda: [],
                          derive=derive, pr_state_resolver=lambda urls: {},
                          now=self.now)

    def _registry(self):
        return json.loads((self.core / h.MISSIONS_REL).read_text())

    def test_ingests_and_closes_when_enabled_and_on_main(self):
        prs = [{'number': 512, 'branch': 'feat/new-mission-foo'}]
        closed = []
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}), \
                mock.patch.object(h, 'load_repo_paths',
                                  return_value={'ourliberty-agent-core': self.core}), \
                mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs), \
                mock.patch.object(h, 'fetch_branch_mission_entry',
                                  side_effect=lambda r, b, mid: _mentry('foo')), \
                mock.patch.object(h, 'commit_and_push_missions', return_value='committed'), \
                mock.patch.object(h, '_missions_ids_on_main', return_value={'foo'}), \
                mock.patch.object(h, 'close_new_mission_pr',
                                  side_effect=lambda r, n, m: closed.append((n, m)) or True):
            rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual([m['id'] for m in self._registry()['missions']], ['foo'])
        self.assertEqual(closed, [(512, 'foo')])

    def test_does_not_close_when_not_on_main(self):
        # Ingested + committed locally, but the mission is NOT yet on origin/main
        # (e.g. push lagged) -> PR stays open, retry next tick.
        prs = [{'number': 512, 'branch': 'feat/new-mission-foo'}]
        closed = []
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}), \
                mock.patch.object(h, 'load_repo_paths',
                                  return_value={'ourliberty-agent-core': self.core}), \
                mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs), \
                mock.patch.object(h, 'fetch_branch_mission_entry',
                                  side_effect=lambda r, b, mid: _mentry('foo')), \
                mock.patch.object(h, 'commit_and_push_missions', return_value='committed'), \
                mock.patch.object(h, '_missions_ids_on_main', return_value=set()), \
                mock.patch.object(h, 'close_new_mission_pr',
                                  side_effect=lambda r, n, m: closed.append((n, m)) or True):
            self._run()
        self.assertEqual([m['id'] for m in self._registry()['missions']], ['foo'])
        self.assertEqual(closed, [])

    def test_default_disabled_observes_only(self):
        # No env => soak/observe: list to log, but never fetch/append/close.
        prs = [{'number': 512, 'branch': 'feat/new-mission-foo'}]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.ENV_NEWMISSION_INGEST, None)
            with mock.patch.object(h, 'load_repo_paths',
                                   return_value={'ourliberty-agent-core': self.core}), \
                    mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs), \
                    mock.patch.object(h, 'fetch_branch_mission_entry',
                                      side_effect=AssertionError('disabled: no fetch')), \
                    mock.patch.object(h, 'close_new_mission_pr',
                                      side_effect=AssertionError('disabled: no close')):
                rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._registry()['missions'], [])

    def test_gh_failure_tolerated_no_crash(self):
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}), \
                mock.patch.object(h, 'load_repo_paths',
                                  return_value={'ourliberty-agent-core': self.core}), \
                mock.patch.object(h, 'list_open_new_mission_prs',
                                  side_effect=RuntimeError('gh exploded')):
            rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._registry()['missions'], [])

    def test_ingests_even_when_chain_events_unavailable(self):
        # THE point of the ordering tidy-up: a chain_events outage skips
        # propose/retire but must NOT strand new-mission PRs — ingestion still
        # runs, lands the mission, and closes the PR.
        prs = [{'number': 512, 'branch': 'feat/new-mission-foo'}]
        closed = []
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}), \
                mock.patch.object(h, 'load_repo_paths',
                                  return_value={'ourliberty-agent-core': self.core}), \
                mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs), \
                mock.patch.object(h, 'fetch_branch_mission_entry',
                                  side_effect=lambda r, b, mid: _mentry('foo')), \
                mock.patch.object(h, 'commit_and_push_missions', return_value='committed'), \
                mock.patch.object(h, '_missions_ids_on_main', return_value={'foo'}), \
                mock.patch.object(h, 'close_new_mission_pr',
                                  side_effect=lambda r, n, m: closed.append((n, m)) or True):
            # chain_events unavailable (events_fetcher returns None)
            rc = h.run_once(dry_run=False, events_fetcher=lambda: None,
                            derive=derive, pr_state_resolver=lambda urls: {},
                            now=self.now)
        self.assertEqual(rc, 0)
        self.assertEqual([m['id'] for m in self._registry()['missions']], ['foo'])
        self.assertEqual(closed, [(512, 'foo')])

    def test_scan_partial_mutation_not_committed_with_ingest(self):
        # If scan_and_propose leaves a partial in-memory mutation then raises, a
        # same-tick ingest must NOT commit the half-applied propose work — only the
        # cleanly-ingested mission lands (the on-disk re-read discards the partial).
        def _scan_then_raise(registry, *a, **k):
            registry['missions'].append({'id': 'PARTIAL-junk', 'name': 'x',
                                         'phase': 'proposed'})
            raise RuntimeError('scan blew up mid-loop')
        prs = [{'number': 512, 'branch': 'feat/new-mission-foo'}]
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}), \
                mock.patch.object(h, 'load_repo_paths',
                                  return_value={'ourliberty-agent-core': self.core}), \
                mock.patch.object(h, 'scan_and_propose', side_effect=_scan_then_raise), \
                mock.patch.object(h, 'list_open_new_mission_prs', return_value=prs), \
                mock.patch.object(h, 'fetch_branch_mission_entry',
                                  side_effect=lambda r, b, mid: _mentry('foo')), \
                mock.patch.object(h, 'commit_and_push_missions', return_value='committed'), \
                mock.patch.object(h, '_missions_ids_on_main', return_value={'foo'}), \
                mock.patch.object(h, 'close_new_mission_pr',
                                  side_effect=lambda r, n, m: True):
            rc = h.run_once(dry_run=False, events_fetcher=lambda: [{'x': 1}],
                            derive=derive, pr_state_resolver=lambda urls: {},
                            now=self.now)
        self.assertEqual(rc, 0)
        ids = [m['id'] for m in self._registry()['missions']]
        self.assertEqual(ids, ['foo'])           # ONLY the ingested entry
        self.assertNotIn('PARTIAL-junk', ids)    # partial scan work discarded


# --------------------------------------------------- new-mission QUEUE drain


def _write_queue(queue_dir: Path, entry: dict) -> Path:
    """Drop a queue file `<id>.json` (the dashboard's atomic write equivalent)."""
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / f"{entry['id']}.json"
    path.write_text(json.dumps(entry) + '\n')
    return path


class ListQueuedNewMissionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='queue-list-')
        self.qd = Path(self.tmp) / 'new-mission-queue'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_dir_returns_empty(self):
        self.assertEqual(h.list_queued_new_missions(self.qd), [])

    def test_lists_json_name_sorted(self):
        _write_queue(self.qd, _mentry('beta'))
        _write_queue(self.qd, _mentry('alpha'))
        out = h.list_queued_new_missions(self.qd)
        self.assertEqual([e['id'] for _, e in out], ['alpha', 'beta'])

    def test_skips_malformed_and_non_object_leaving_them(self):
        self.qd.mkdir(parents=True)
        (self.qd / 'bad.json').write_text('{not json')
        (self.qd / 'arr.json').write_text('[1, 2]')
        _write_queue(self.qd, _mentry('good'))
        out = h.list_queued_new_missions(self.qd)
        self.assertEqual([e['id'] for _, e in out], ['good'])
        # malformed files are LEFT in place for a human, not deleted.
        self.assertTrue((self.qd / 'bad.json').exists())

    def test_ignores_non_json_and_tmp_files(self):
        self.qd.mkdir(parents=True)
        (self.qd / 'foo.json.tmp').write_text('partial')  # in-flight atomic write
        (self.qd / 'note.txt').write_text('x')
        _write_queue(self.qd, _mentry('good'))
        out = h.list_queued_new_missions(self.qd)
        self.assertEqual([e['id'] for _, e in out], ['good'])


class DrainNewMissionQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='queue-drain-')
        self.qd = Path(self.tmp) / 'new-mission-queue'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_appends_and_marks_deletable(self):
        p = _write_queue(self.qd, _mentry('foo'))
        reg = {'schema_version': 1, 'missions': []}
        drained, deletable = h.drain_new_mission_queue(reg, self.qd)
        self.assertEqual([m['id'] for m in reg['missions']], ['foo'])
        self.assertEqual(drained, ['foo'])
        self.assertEqual(deletable, [(p, 'foo')])

    def test_preserves_full_entry_fields(self):
        entry = _mentry('foo')
        entry['brief'] = 'keep me'
        entry['spec_docs'] = ['a.md']
        _write_queue(self.qd, entry)
        reg = {'schema_version': 1, 'missions': []}
        h.drain_new_mission_queue(reg, self.qd)
        self.assertEqual(reg['missions'][0]['brief'], 'keep me')
        self.assertEqual(reg['missions'][0]['spec_docs'], ['a.md'])

    def test_duplicate_id_not_appended_but_deletable(self):
        p = _write_queue(self.qd, _mentry('foo'))
        reg = {'schema_version': 1, 'missions': [_mentry('foo')]}
        drained, deletable = h.drain_new_mission_queue(reg, self.qd)
        self.assertEqual(len(reg['missions']), 1)         # unchanged
        self.assertEqual(drained, [])
        self.assertEqual(deletable, [(p, 'foo')])         # redundant file → delete

    def test_no_id_left_in_place(self):
        self.qd.mkdir(parents=True)
        (self.qd / 'weird.json').write_text(json.dumps({'name': 'x'}) + '\n')
        reg = {'schema_version': 1, 'missions': []}
        drained, deletable = h.drain_new_mission_queue(reg, self.qd)
        self.assertEqual(reg['missions'], [])
        self.assertEqual((drained, deletable), ([], []))

    def test_malformed_entry_left_in_place(self):
        # id present but missing required name/phase → invalid → not drained.
        self.qd.mkdir(parents=True)
        (self.qd / 'm.json').write_text(
            json.dumps({'id': 'm', 'task_ids': []}) + '\n')
        reg = {'schema_version': 1, 'missions': []}
        drained, deletable = h.drain_new_mission_queue(reg, self.qd)
        self.assertEqual(reg['missions'], [])
        self.assertEqual((drained, deletable), ([], []))

    def test_non_list_task_ids_rejected(self):
        self.qd.mkdir(parents=True)
        (self.qd / 'm.json').write_text(json.dumps(
            {'id': 'm', 'name': 'M', 'phase': 'drafting',
             'task_ids': 'oops'}) + '\n')
        reg = {'schema_version': 1, 'missions': []}
        drained, deletable = h.drain_new_mission_queue(reg, self.qd)
        self.assertEqual((drained, deletable), ([], []))

    def test_bounded_per_tick(self):
        for i in range(5):
            _write_queue(self.qd, _mentry(f'm{i}'))
        reg = {'schema_version': 1, 'missions': []}
        with mock.patch.object(h, 'NEWMISSION_MAX_PER_TICK', 2):
            drained, _ = h.drain_new_mission_queue(reg, self.qd)
        self.assertEqual(len(drained), 2)


class QueueDirParityTest(unittest.TestCase):
    def test_healer_and_dashboard_resolve_the_same_queue_dir(self):
        # The dashboard WRITES queue files; this healer DRAINS them. The two paths
        # are defined independently (different modules) but MUST agree — if they
        # ever diverge, queued missions silently never register. Pin them together.
        with mock.patch.dict(os.environ,
                             {'OURLIBERTY_AGENTS_ROOT': '/tmp/agts-parity'}):
            self.assertEqual(
                str(h._new_mission_queue_dir()),
                str(derive._new_mission_queue_dir()),
            )


class DeleteQueueFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='queue-del-')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_removes_existing(self):
        p = Path(self.tmp) / 'x.json'
        p.write_text('{}')
        self.assertTrue(h.delete_queue_file(p))
        self.assertFalse(p.exists())

    def test_already_gone_is_success(self):
        self.assertTrue(h.delete_queue_file(Path(self.tmp) / 'nope.json'))


class RunOnceQueueDrainTest(unittest.TestCase):
    """run_once drains the queue (primary) + closes the PR backstop, only when
    ENABLED and only for missions confirmed on origin/main; defaults observe-only;
    runs even when chain_events is unavailable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='run-once-queue-')
        self.core = Path(self.tmp) / 'agent-core'
        (self.core / 'agents' / 'beacon').mkdir(parents=True)
        (self.core / h.MISSIONS_REL).write_text(
            json.dumps({'schema_version': 1, 'missions': []}) + '\n')
        self.qd = Path(self.tmp) / 'new-mission-queue'
        self.now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        # Seam off the terminal-gate gh fetch run_once builds each tick (hermetic).
        _gate = mock.patch.object(h, '_load_terminal_gate',
                                  return_value=lambda tids: (False, None))
        _gate.start()
        self.addCleanup(_gate.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _registry(self):
        return json.loads((self.core / h.MISSIONS_REL).read_text())

    def _ctx(self, on_main, prs=None, closed=None):
        """Common run_once patches; caller supplies the on-main id set."""
        return [
            mock.patch.object(h, 'load_repo_paths',
                              return_value={'ourliberty-agent-core': self.core}),
            mock.patch.object(h, '_new_mission_queue_dir', return_value=self.qd),
            mock.patch.object(h, 'list_open_new_mission_prs',
                              return_value=prs if prs is not None else []),
            mock.patch.object(h, 'commit_and_push_missions', return_value='committed'),
            mock.patch.object(h, '_missions_ids_on_main', return_value=on_main),
        ]

    def _run(self, events_fetcher=lambda: []):
        return h.run_once(dry_run=False, events_fetcher=events_fetcher,
                          derive=derive, pr_state_resolver=lambda urls: {},
                          now=self.now)

    def test_drains_and_deletes_when_enabled_and_on_main(self):
        p = _write_queue(self.qd, _mentry('foo'))
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}):
            with contextlib.ExitStack() as es:
                for cm in self._ctx({'foo'}):
                    es.enter_context(cm)
                rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual([m['id'] for m in self._registry()['missions']], ['foo'])
        self.assertFalse(p.exists())            # removed after on-main confirm

    def test_does_not_delete_when_not_on_main(self):
        # Drained + committed locally, but the mission is NOT yet on origin/main
        # (push lagged) → leave the queue file, retry next tick.
        p = _write_queue(self.qd, _mentry('foo'))
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}):
            with contextlib.ExitStack() as es:
                for cm in self._ctx(set()):
                    es.enter_context(cm)
                self._run()
        self.assertEqual([m['id'] for m in self._registry()['missions']], ['foo'])
        self.assertTrue(p.exists())             # NOT deleted — retry

    def test_default_disabled_observes_only(self):
        p = _write_queue(self.qd, _mentry('foo'))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.ENV_NEWMISSION_INGEST, None)
            with contextlib.ExitStack() as es:
                for cm in self._ctx({'foo'}):
                    es.enter_context(cm)
                rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._registry()['missions'], [])   # nothing drained
        self.assertTrue(p.exists())                          # queue file untouched

    def test_drains_even_when_chain_events_unavailable(self):
        p = _write_queue(self.qd, _mentry('foo'))
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}):
            with contextlib.ExitStack() as es:
                for cm in self._ctx({'foo'}):
                    es.enter_context(cm)
                rc = self._run(events_fetcher=lambda: None)  # chain_events down
        self.assertEqual(rc, 0)
        self.assertEqual([m['id'] for m in self._registry()['missions']], ['foo'])
        self.assertFalse(p.exists())

    def test_queue_and_pr_backstop_both_register_same_tick(self):
        # The queue (primary) and the PR backstop coexist: a queued mission AND an
        # in-flight feat/new-mission-* PR both land in one commit.
        p = _write_queue(self.qd, _mentry('foo'))
        prs = [{'number': 9, 'branch': 'feat/new-mission-bar'}]
        closed = []
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}):
            with contextlib.ExitStack() as es:
                for cm in self._ctx({'foo', 'bar'}, prs=prs):
                    es.enter_context(cm)
                es.enter_context(mock.patch.object(
                    h, 'fetch_branch_mission_entry',
                    side_effect=lambda r, b, mid: _mentry('bar')))
                es.enter_context(mock.patch.object(
                    h, 'close_new_mission_pr',
                    side_effect=lambda r, n, m: closed.append((n, m)) or True))
                self._run()
        ids = sorted(m['id'] for m in self._registry()['missions'])
        self.assertEqual(ids, ['bar', 'foo'])
        self.assertFalse(p.exists())             # queue file removed
        self.assertEqual(closed, [(9, 'bar')])   # PR backstop closed


# ------------------------------------------------- § 2 lost-update write guard


class RunOnceLostUpdateGuardTest(unittest.TestCase):
    """run_once holds its read-time snapshot across chain_events + gh
    round-trips; a concurrent writer's delta landing in that window (dashboard
    delegate `spawned` stamp, GC phase flip, another ingest) must survive the
    healer's write. Mirrors heal_missions_card_gc's lost-update tests, plus the
    append-aware cases the GC merge doesn't need (this healer authors NEW
    entries via propose/ingest/drain)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='run-once-lost-update-')
        self.core = Path(self.tmp) / 'agent-core'
        (self.core / 'agents' / 'beacon').mkdir(parents=True)
        self.mfile = self.core / h.MISSIONS_REL
        self.qd = Path(self.tmp) / 'new-mission-queue'
        self.now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        _gate = mock.patch.object(h, '_load_terminal_gate',
                                  return_value=lambda tids: (False, None))
        _gate.start()
        self.addCleanup(_gate.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_missions(self, missions):
        self.mfile.write_text(json.dumps(
            {'schema_version': 1, 'missions': missions}) + '\n')

    def _disk(self):
        return json.loads(self.mfile.read_text())

    def _ctx(self, on_main):
        import larry_alerts
        return [
            mock.patch.object(h, 'load_repo_paths',
                              return_value={'ourliberty-agent-core': self.core}),
            mock.patch.object(h, '_new_mission_queue_dir', return_value=self.qd),
            mock.patch.object(h, 'list_open_new_mission_prs', return_value=[]),
            mock.patch.object(h, 'commit_and_push_missions', return_value='committed'),
            mock.patch.object(h, '_missions_ids_on_main', return_value=on_main),
            # the stuck-flag tick emits an info digest — keep the tests hermetic
            mock.patch.object(larry_alerts, 'append_alert'),
        ]

    def _run(self, events_fetcher, on_main=frozenset()):
        """One LIVE tick (queue-drain enabled) with the given events seam — the
        seam runs AFTER the registry read and BEFORE the write, so its side
        effect lands inside the lost-update window."""
        with mock.patch.dict(os.environ, {h.ENV_NEWMISSION_INGEST: 'true'}):
            with contextlib.ExitStack() as es:
                for cm in self._ctx(set(on_main)):
                    es.enter_context(cm)
                return h.run_once(dry_run=False, events_fetcher=events_fetcher,
                                  derive=derive, pr_state_resolver=lambda urls: {},
                                  now=self.now)

    def _concurrent(self, mutate):
        """events_fetcher that simulates a SEPARATE process writing
        missions.json mid-tick, then reports no chain_events."""
        def fetch():
            disk = self._disk()
            mutate(disk)
            self.mfile.write_text(json.dumps(disk) + '\n')
            return []
        return fetch

    def test_concurrent_append_survives_our_append(self):
        # Another writer registers m-concurrent mid-tick while this tick drains
        # queued `foo` — the whole-file write used to clobber m-concurrent.
        self._write_missions([])
        _write_queue(self.qd, _mentry('foo'))
        rc = self._run(self._concurrent(
            lambda d: d['missions'].append(_mentry('m-concurrent'))))
        self.assertEqual(rc, 0)
        ids = sorted(m['id'] for m in self._disk()['missions'])
        self.assertEqual(ids, ['foo', 'm-concurrent'])

    def test_concurrent_field_stamp_on_untouched_entry_survives(self):
        # The dashboard stamps a delegate `spawned` on del-1 mid-tick; this tick
        # never touches del-1 (its work is the queue append) — the stamp survives.
        self._write_missions([_mentry('del-1')])
        _write_queue(self.qd, _mentry('foo'))

        def stamp(disk):
            disk['missions'][0]['spawned'] = {'session': 's-42', 'at': self.now.isoformat()}
        rc = self._run(self._concurrent(stamp))
        self.assertEqual(rc, 0)
        by_id = {m['id']: m for m in self._disk()['missions']}
        self.assertEqual(set(by_id), {'del-1', 'foo'})
        self.assertEqual(by_id['del-1']['spawned'],
                         {'session': 's-42', 'at': self.now.isoformat()})

    def test_field_delta_merges_with_concurrent_change_on_same_entry(self):
        # This tick flags note-1 stuck (needs_decision) WHILE a concurrent writer
        # sets an unrelated field on the same entry — both must land.
        stuck = {'id': 'note-1', 'phase': 'proposed', 'task_ids': ['t-x'],
                 'proposed_by': 'closeout-author', 'created': '2026-05-01'}
        self._write_missions([stuck])
        rc = self._run(self._concurrent(
            lambda d: d['missions'][0].__setitem__('note', 'concurrent')))
        self.assertEqual(rc, 0)
        e = self._disk()['missions'][0]
        self.assertIs(e['needs_decision'], True)      # our flag landed
        self.assertEqual(e['note'], 'concurrent')     # their field survived

    def test_our_append_dedups_against_concurrent_same_id(self):
        # A concurrent writer lands the SAME id we drained this tick — keep the
        # fresh (their) copy untouched, never a duplicate.
        self._write_missions([])
        _write_queue(self.qd, _mentry('foo'))
        theirs = dict(_mentry('foo'), origin='dashboard')
        rc = self._run(self._concurrent(
            lambda d: d['missions'].append(theirs)))
        self.assertEqual(rc, 0)
        missions = self._disk()['missions']
        self.assertEqual([m['id'] for m in missions], ['foo'])
        self.assertEqual(missions[0]['origin'], 'dashboard')

    def test_entry_deleted_mid_tick_is_not_readded(self):
        # A concurrent writer deletes the entry this tick flagged — drop our
        # stale delta rather than resurrecting the entry. (A bystander entry
        # stays, so the empty-re-read wipe guard doesn't fire.)
        stuck = {'id': 'note-1', 'phase': 'proposed', 'task_ids': ['t-x'],
                 'proposed_by': 'closeout-author', 'created': '2026-05-01'}
        self._write_missions([stuck, _mentry('bystander')])
        _write_queue(self.qd, _mentry('foo'))   # keeps the tick's write firing

        def drop_note(disk):
            disk['missions'] = [m for m in disk['missions'] if m['id'] != 'note-1']
        rc = self._run(self._concurrent(drop_note))
        self.assertEqual(rc, 0)
        self.assertEqual([m['id'] for m in self._disk()['missions']],
                         ['bystander', 'foo'])

    def test_empty_fresh_reread_skips_write_never_wipes(self):
        # A registry that HAD entries at read time re-reads empty just before
        # the write (transiently truncated/missing file, e.g. mid `git pull`
        # rewriting it) — refuse the write: rebuilding the file from this
        # tick's appends alone would wipe every existing mission and push the
        # wipe to main.
        self._write_missions([_mentry('keep-1'), _mentry('keep-2')])
        _write_queue(self.qd, _mentry('foo'))
        rc = self._run(self._concurrent(
            lambda d: d.__setitem__('missions', [])))
        self.assertEqual(rc, 0)
        # the write was skipped — the truncated file was NOT replaced by an
        # appends-only registry (no {foo}-only wipe committed)
        self.assertEqual(self._disk()['missions'], [])
        p = self.qd / 'foo.json'
        self.assertTrue(p.exists())   # nothing persisted → queue file kept

    def test_bootstrap_missing_file_still_writes_appends(self):
        # First-ever tick: missions.json does not exist. The empty-re-read
        # guard must NOT block bootstrap (nothing existed at read time, so an
        # appends-only write is exactly right).
        self.assertFalse(self.mfile.exists())
        _write_queue(self.qd, _mentry('foo'))
        rc = self._run(lambda: [])
        self.assertEqual(rc, 0)
        self.assertEqual([m['id'] for m in self._disk()['missions']], ['foo'])


if __name__ == '__main__':
    unittest.main()
