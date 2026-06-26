"""Tests for heal_merged_pr_board_reconcile — the off-board merged-PR surfacer.

All gh + signal IO is injected/sandboxed: pr_lister is a fake (no live gh), the
registry is in-memory, and the for-Larry signal is written to a tmp path. Nothing
touches live ~/agents, Supabase, or GitHub.

    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_merged_pr_board_reconcile
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import for_larry_signal  # noqa: E402
import heal_merged_pr_board_reconcile as h  # noqa: E402

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)

# A long, specific mission id (>= MIN_TOKEN_LEN) used across the tests.
_MID = 'offboard-initiative-alpha'


def _mission(mid=_MID, *, phase='drafting', task_ids=None, spec_docs=None, **extra):
    m = {'id': mid, 'name': mid.replace('-', ' '), 'phase': phase,
         'task_ids': task_ids if task_ids is not None else []}
    if spec_docs is not None:
        m['spec_docs'] = spec_docs
    m.update(extra)
    return m


def _pr(number, state, *, branch='', title='', repo_url='https://github.com/o/r'):
    return {'number': number, 'state': state, 'title': title,
            'headRefName': branch, 'url': f'{repo_url}/pull/{number}'}


def _lister(prs_by_repo):
    """A pr_lister returning the given PRs per repo; None marks a gh failure."""
    def _l(repo, *, limit, timeout):  # noqa: ARG001
        return prs_by_repo.get(repo)
    return _l


class MatchTokens(unittest.TestCase):
    def test_id_and_spec_basenames(self):
        m = _mission(spec_docs=['agents/beacon/specs/offboard-initiative-alpha.md'])
        toks = h.match_tokens(m)
        self.assertIn(_MID, toks)
        self.assertIn('offboard-initiative-alpha', toks)  # spec stem (no dir/.md)

    def test_anchor_and_dir_stripped(self):
        m = _mission(mid='x', spec_docs=['a/b/long-spec-basename-here.md#section'])
        self.assertEqual(h.match_tokens(m), ['long-spec-basename-here'])

    def test_short_tokens_filtered(self):
        # 'short-id' (8) is below MIN_TOKEN_LEN -> dropped; no spec -> empty.
        self.assertEqual(h.match_tokens(_mission(mid='short-id')), [])

    def test_dedup_order_preserving(self):
        m = _mission(spec_docs=[f'specs/{_MID}.md'])  # spec stem == id
        self.assertEqual(h.match_tokens(m), [_MID])


class OffboardActivePredicate(unittest.TestCase):
    def test_drafting_empty_taskids_is_offboard(self):
        self.assertTrue(h.is_offboard_active_mission(_mission(phase='drafting')))

    def test_in_flight_and_ready_included(self):
        self.assertTrue(h.is_offboard_active_mission(_mission(phase='in_flight')))
        self.assertTrue(h.is_offboard_active_mission(_mission(phase='ready')))

    def test_pr_backed_taskids_excluded(self):
        # The GC owns task-linked missions — this healer must not double-handle.
        m = _mission(task_ids=['some-pr-backed-task'])
        self.assertFalse(h.is_offboard_active_mission(m))

    def test_only_review_shaped_taskids_is_offboard(self):
        # review-shaped ids never back a PR -> not probeable -> still off-board.
        m = _mission(task_ids=['review-foo', 'notify-bar', 'dag-preflight-baz'])
        self.assertTrue(h.is_offboard_active_mission(m))

    def test_non_active_phase_excluded(self):
        for phase in ('proposed', 'shipped', 'deferred'):
            self.assertFalse(h.is_offboard_active_mission(_mission(phase=phase)))

    def test_archived_ack_retired_excluded(self):
        self.assertFalse(h.is_offboard_active_mission(_mission(archived=True)))
        self.assertFalse(h.is_offboard_active_mission(_mission(acknowledged=True)))
        self.assertFalse(
            h.is_offboard_active_mission(_mission(retired_at='2026-06-01T00:00:00Z')))

    def test_non_dict_safe(self):
        self.assertFalse(h.is_offboard_active_mission('nope'))


class ProbeMergedMatch(unittest.TestCase):
    def _probe(self, prs_by_repo):
        return h.probe_merged_match([_MID], prs_by_repo, min_len=h.MIN_TOKEN_LEN)

    def test_merged_match_returns_evidence(self):
        ev = self._probe({'r': [_pr(7, 'MERGED', branch=f'forge/{_MID}')]})
        self.assertIsNotNone(ev)
        self.assertEqual(ev['pr_number'], 7)
        self.assertEqual(ev['repo'], 'r')

    def test_match_on_title(self):
        ev = self._probe({'r': [_pr(8, 'MERGED', title=f'feat: {_MID} done')]})
        self.assertEqual(ev['pr_number'], 8)

    def test_open_match_vetoes(self):
        # An OPEN matching PR means still building -> not "looks shipped".
        ev = self._probe({'r': [
            _pr(9, 'MERGED', branch=f'forge/{_MID}'),
            _pr(10, 'OPEN', branch=f'forge/{_MID}-followup'),
        ]})
        self.assertIsNone(ev)

    def test_open_in_other_repo_vetoes_merged(self):
        # Decide only after collecting ALL repos: open in r2 vetoes merged in r1.
        ev = self._probe(
            {'r1': [_pr(1, 'MERGED', branch=f'forge/{_MID}')],
             'r2': [_pr(2, 'OPEN', branch=f'forge/{_MID}')]})
        self.assertIsNone(ev)

    def test_no_match_returns_none(self):
        self.assertIsNone(self._probe({'r': [_pr(3, 'MERGED', branch='forge/other')]}))

    def test_gh_failure_skips_repo_no_fabrication(self):
        # None == gh failure for that repo; must not crash or fabricate a match.
        self.assertIsNone(self._probe({'r': None}))

    def test_closed_unmerged_is_not_shipped(self):
        self.assertIsNone(self._probe({'r': [_pr(4, 'CLOSED', branch=f'forge/{_MID}')]}))

    def test_empty_tokens_returns_none(self):
        self.assertIsNone(h.probe_merged_match(
            [], {'r': [_pr(5, 'MERGED', branch=f'forge/{_MID}')]},
            min_len=h.MIN_TOKEN_LEN))


class BuildActiveSignals(unittest.TestCase):
    def test_surfaces_only_offboard_looks_shipped(self):
        registry = {'missions': [
            _mission('offboard-looks-shipped-alpha', phase='drafting'),   # surfaced
            _mission('task-linked-mission-beta',                          # GC's job
                     task_ids=['task-linked-mission-beta-pr']),
            _mission('offboard-still-building-gamma', phase='in_flight'),  # open -> skip
            _mission('proposed-idea-delta', phase='proposed'),            # not active
        ]}
        prs = {'r': [
            _pr(11, 'MERGED', branch='forge/offboard-looks-shipped-alpha'),
            _pr(12, 'MERGED', branch='forge/task-linked-mission-beta'),
            _pr(13, 'OPEN', branch='forge/offboard-still-building-gamma'),
        ]}
        active = h.build_active_signals(registry, prs_by_repo=prs, now=_NOW)
        self.assertEqual(
            list(active), [h.SIGNAL_PREFIX + 'offboard-looks-shipped-alpha'])
        rec = active[h.SIGNAL_PREFIX + 'offboard-looks-shipped-alpha']
        self.assertEqual(rec['pr_number'], 11)
        self.assertEqual(rec['severity'], 'info')
        self.assertEqual(rec['source'], h.SIGNAL_BY)
        self.assertEqual(rec['ts'], _NOW.isoformat())  # stamps the injected now

    def test_id_less_mission_skipped(self):
        # No usable id -> not keyable/actionable -> never surfaced (no ':None').
        registry = {'missions': [
            {'name': 'no id here', 'phase': 'drafting', 'task_ids': [],
             'spec_docs': ['specs/long-spec-basename-x.md']}]}
        active = h.build_active_signals(
            registry,
            prs_by_repo={'r': [_pr(1, 'MERGED', branch='forge/long-spec-basename-x')]},
            now=_NOW)
        self.assertEqual(active, {})

    def test_does_not_mutate_registry(self):
        registry = {'missions': [_mission('offboard-looks-shipped-alpha')]}
        before = registry['missions'][0]['phase']
        h.build_active_signals(
            registry,
            prs_by_repo={'r': [
                _pr(1, 'MERGED', branch='forge/offboard-looks-shipped-alpha')]},
            now=_NOW)
        self.assertEqual(registry['missions'][0]['phase'], before)  # never retired
        self.assertNotIn('shipped_at', registry['missions'][0])

    def test_bad_registry_safe(self):
        self.assertEqual(
            h.build_active_signals({}, prs_by_repo={}, now=_NOW), {})


class RunCycleSelfClearing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            suffix='.json', delete=False, prefix='for-larry-test-')
        self._tmp.close()
        self.signal_path = Path(self._tmp.name)
        # start from an empty doc
        self.signal_path.unlink(missing_ok=True)

    def _active_keys(self):
        return {e['key'] for e in for_larry_signal.active_entries(path=self.signal_path)}

    def test_surface_then_self_clear(self):
        reg = {'missions': [_mission('offboard-looks-shipped-alpha')]}
        match = {'r': [_pr(21, 'MERGED', branch='forge/offboard-looks-shipped-alpha')]}

        # Tick 1: PR merged -> surfaced.
        h.run_cycle(apply=True, pr_lister=_lister(match), repos=['r'], now=_NOW,
                    read_registry=lambda: reg, signal_path=self.signal_path)
        self.assertIn(
            h.SIGNAL_PREFIX + 'offboard-looks-shipped-alpha', self._active_keys())

        # Tick 2: match gone (e.g. mission shipped/branch deleted) -> auto-resolved.
        h.run_cycle(apply=True, pr_lister=_lister({'r': []}), repos=['r'], now=_NOW,
                    read_registry=lambda: reg, signal_path=self.signal_path)
        self.assertNotIn(
            h.SIGNAL_PREFIX + 'offboard-looks-shipped-alpha', self._active_keys())

    def test_dry_run_writes_nothing(self):
        reg = {'missions': [_mission('offboard-looks-shipped-alpha')]}
        match = {'r': [_pr(22, 'MERGED', branch='forge/offboard-looks-shipped-alpha')]}
        res = h.run_cycle(apply=False, pr_lister=_lister(match), repos=['r'],
                          now=_NOW, read_registry=lambda: reg,
                          signal_path=self.signal_path)
        self.assertEqual(len(res['active']), 1)
        self.assertEqual(self._active_keys(), set())  # nothing written

    def test_bad_registry_degrades(self):
        res = h.run_cycle(apply=True, pr_lister=_lister({}), repos=['r'], now=_NOW,
                          read_registry=lambda: None, signal_path=self.signal_path)
        self.assertEqual(res, {'active': {}, 'written': [], 'resolved': []})

    def test_no_candidates_skips_gh(self):
        # Lazy gate: a registry with no off-board mission must not hit gh at all.
        calls = []

        def lister(repo, *, limit, timeout):  # noqa: ARG001
            calls.append(repo)
            return []

        reg = {'missions': [_mission('task-linked-mission', task_ids=['t-pr'])]}
        h.run_cycle(apply=False, pr_lister=lister, repos=['r'], now=_NOW,
                    read_registry=lambda: reg, signal_path=self.signal_path)
        self.assertEqual(calls, [])  # gh never listed

    def test_kill_switch_short_circuits(self):
        ksp = h._kill_switch_path()
        ksp.parent.mkdir(parents=True, exist_ok=True)
        ksp.write_text('')
        try:
            self.assertEqual(h.main([]), 0)  # exits 0 without running a tick
        finally:
            ksp.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
