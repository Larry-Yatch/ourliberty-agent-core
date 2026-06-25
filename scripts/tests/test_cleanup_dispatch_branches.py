#!/usr/bin/env python3
"""Tests for cleanup_dispatch_branches.py — the Forge/Mirror dispatch-branch GC.

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_cleanup_dispatch_branches

Covers:
  - classify(): the full safe-to-prune bar, including that active/open-PR
    override the age + work-accounted gates, and that every indeterminate
    signal KEEPs.
  - the branch-name reconstruction sanitizer agrees byte-for-byte with
    worktree_manager (so the active-dispatch guard never builds a name no live
    branch carries).
  - compute_net_change() against real git repos: empty-WIP, squash-merged
    (content-on-main), and genuinely-unique branches.
  - active_dispatch_branches(): reconstruction from inbox + live in-flight.
  - delete_remote() idempotency: an already-deleted remote ref counts as success.
  - sweep_repo() end-to-end on a real repo with mocked gh: dry-run deletes
    nothing and leaves open-PR / active / unique / too-young branches untouched;
    a gh-unavailable open-PR query refuses to prune the repo.
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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import cleanup_dispatch_branches as cdb  # noqa: E402
import worktree_manager as wm  # noqa: E402

NOW = 1_000_000_000.0
OLD_TS = int(NOW - cdb.MIN_AGE_SECONDS - 3600)   # comfortably past the age floor
YOUNG_TS = int(NOW - 3600)                        # 1h old — inside the floor


def _real_now():
    # Sweep tests use real git commit dates (2020 = old, now() = young), so they
    # must be evaluated against the real wall clock, not the abstract NOW above.
    return datetime.now(timezone.utc).timestamp()


def _facts(**kw):
    base = dict(
        name='forge/x', scope='local', committer_ts=OLD_TS,
        is_active=False, is_open_pr_head=False, is_merged_pr_head=False,
        net_change='empty-wip',
    )
    base.update(kw)
    return cdb.BranchFacts(**base)


# ---------- classify(): the safety bar ----------


class ClassifyTest(unittest.TestCase):
    def test_empty_wip_old_is_pruned(self):
        d = cdb.classify(_facts(net_change='empty-wip'), NOW)
        self.assertEqual(d.action, 'prune')
        self.assertIn('abandoned', d.reason)

    def test_content_on_main_is_pruned(self):
        d = cdb.classify(_facts(net_change='content-on-main'), NOW)
        self.assertEqual(d.action, 'prune')
        self.assertIn('squash', d.reason)

    def test_merged_pr_head_is_pruned(self):
        d = cdb.classify(_facts(is_merged_pr_head=True, net_change='unique'), NOW)
        self.assertEqual(d.action, 'prune')
        self.assertIn('merged', d.reason)

    def test_unique_content_is_kept(self):
        d = cdb.classify(_facts(net_change='unique'), NOW)
        self.assertEqual(d.action, 'keep')
        self.assertIn('unique', d.reason)

    def test_unknown_is_kept(self):
        d = cdb.classify(_facts(net_change='unknown'), NOW)
        self.assertEqual(d.action, 'keep')

    def test_active_overrides_age_and_accounting(self):
        # Even an old, empty-WIP branch is kept when a live dispatch uses it.
        d = cdb.classify(_facts(is_active=True, net_change='empty-wip'), NOW)
        self.assertEqual(d.action, 'keep')
        self.assertIn('active', d.reason)

    def test_open_pr_head_overrides_accounting(self):
        # An open-PR head whose content is already on main must NOT be pruned —
        # the remote branch is the live PR head. (The squash lands only on merge.)
        d = cdb.classify(
            _facts(is_open_pr_head=True, net_change='content-on-main'), NOW)
        self.assertEqual(d.action, 'keep')
        self.assertIn('open-PR', d.reason)

    def test_too_young_is_kept_even_if_accounted(self):
        d = cdb.classify(
            _facts(committer_ts=YOUNG_TS, net_change='empty-wip'), NOW)
        self.assertEqual(d.action, 'keep')
        self.assertIn('young', d.reason)

    def test_active_beats_open_pr_in_reason_order(self):
        d = cdb.classify(_facts(is_active=True, is_open_pr_head=True), NOW)
        self.assertEqual(d.action, 'keep')
        self.assertIn('active', d.reason)


# ---------- classify(): the opt-in --by-pr-state mode ----------


class ByPrStateClassifyTest(unittest.TestCase):
    def test_merged_pr_head_pruned_ignoring_age_and_content(self):
        # Young + unique content would be KEPT in normal mode; by-pr-state prunes
        # it purely on the merged-PR state.
        d = cdb.classify(
            _facts(committer_ts=YOUNG_TS, is_merged_pr_head=True,
                   net_change='unique'),
            NOW, by_pr_state=True)
        self.assertEqual(d.action, 'prune')
        self.assertIn('merged', d.reason)
        self.assertIn('by-pr-state', d.reason)

    def test_closed_pr_head_pruned_ignoring_age(self):
        # A CLOSED-unmerged PR head is never pruned in normal mode (no signal for
        # it); by-pr-state prunes it, bypassing the age floor.
        d = cdb.classify(
            _facts(committer_ts=YOUNG_TS, is_closed_pr_head=True,
                   net_change='unique'),
            NOW, by_pr_state=True)
        self.assertEqual(d.action, 'prune')
        self.assertIn('closed', d.reason)
        self.assertIn('by-pr-state', d.reason)

    def test_no_pr_branch_is_kept_even_if_empty_wip(self):
        # OUT OF SCOPE for this mode: a no-PR branch (here empty-WIP, which
        # normal mode prunes) is KEPT because it has no merged/closed PR.
        d = cdb.classify(_facts(net_change='empty-wip'), NOW, by_pr_state=True)
        self.assertEqual(d.action, 'keep')
        self.assertIn('no merged/closed', d.reason)

    def test_no_pr_branch_content_on_main_is_kept(self):
        d = cdb.classify(
            _facts(net_change='content-on-main'), NOW, by_pr_state=True)
        self.assertEqual(d.action, 'keep')
        self.assertIn('no merged/closed', d.reason)

    def test_active_guard_still_intact(self):
        # The active-dispatch guard is NEVER bypassed, even for a merged-PR head.
        d = cdb.classify(
            _facts(is_active=True, is_merged_pr_head=True), NOW, by_pr_state=True)
        self.assertEqual(d.action, 'keep')
        self.assertIn('active', d.reason)

    def test_open_pr_guard_still_intact(self):
        # An open PR head is kept even if it also somehow matched a closed head.
        d = cdb.classify(
            _facts(is_open_pr_head=True, is_closed_pr_head=True),
            NOW, by_pr_state=True)
        self.assertEqual(d.action, 'keep')
        self.assertIn('open-PR', d.reason)

    def test_merged_takes_precedence_over_closed(self):
        d = cdb.classify(
            _facts(is_merged_pr_head=True, is_closed_pr_head=True),
            NOW, by_pr_state=True)
        self.assertEqual(d.action, 'prune')
        self.assertIn('merged', d.reason)

    def test_default_mode_ignores_closed_head(self):
        # Without by_pr_state, a CLOSED-unmerged head with unique content is KEPT
        # (the closed signal is by-pr-state-only).
        d = cdb.classify(
            _facts(is_closed_pr_head=True, net_change='unique'), NOW)
        self.assertEqual(d.action, 'keep')
        self.assertIn('unique', d.reason)


# ---------- branch-name reconstruction consistency ----------


class ReconstructBranchNameTest(unittest.TestCase):
    CORPUS = (
        'task-001', 'feat/cool.thing', 'medic-silence-cpu:high@web-01',
        'abc_123-XYZ', '!@#$%', 'a' * 60, '../../etc', 'review-larry-work',
        'alert#294', 'task with spaces', 'v1.2.3', 'review-x', '',
    )

    def test_sanitizer_agrees_with_worktree_manager(self):
        for tid in self.CORPUS:
            self.assertEqual(
                cdb._sanitize_task_id(tid), wm._sanitize_task_id(tid),
                f'sanitizer divergence on {tid!r}')

    def test_reconstruct_matches_derive_branch_name(self):
        for agent in cdb.DISPATCH_AGENTS:
            for tid in self.CORPUS:
                self.assertEqual(
                    cdb.reconstruct_branch_name(agent, tid),
                    wm.derive_branch_name(agent, tid),
                    f'branch-name divergence on {agent}/{tid!r}')

    def test_mirror_review_task_maps_to_mirror_review_branch(self):
        self.assertEqual(
            cdb.reconstruct_branch_name('mirror', 'review-abc-001'),
            'mirror/review-abc-001')


# ---------- git-backed helpers ----------


def _run(repo, *args, env_extra=None):
    env = dict(os.environ)
    env.update({
        'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
        'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
    })
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ['git', *args], cwd=str(repo), env=env,
        capture_output=True, text=True, check=True)


def _commit(repo, msg, *, allow_empty=False, date=None):
    args = ['commit', '-m', msg]
    if allow_empty:
        args.insert(1, '--allow-empty')
    env = None
    if date:
        env = {'GIT_AUTHOR_DATE': date, 'GIT_COMMITTER_DATE': date}
    _run(repo, *args, env_extra=env)


class GitRepoMixin:
    def _make_repo(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix='cdb-repo-'))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(d)]))
        _run(d, 'init', '-q', '-b', 'main')
        (d / 'base.txt').write_text('base\n')
        _run(d, 'add', '-A')
        _commit(d, 'base')
        # Synthesize origin/main as a remote-tracking ref without a real remote.
        _run(d, 'update-ref', 'refs/remotes/origin/main', 'main')
        return d


class ComputeNetChangeTest(GitRepoMixin, unittest.TestCase):
    def test_empty_wip_only(self):
        repo = self._make_repo()
        _run(repo, 'checkout', '-q', '-b', 'forge/empty')
        _commit(repo, '[WIP][session-start] empty', allow_empty=True)
        self.assertEqual(cdb.compute_net_change(repo, 'forge/empty', 'local'),
                         'empty-wip')

    def test_unique_content(self):
        repo = self._make_repo()
        _run(repo, 'checkout', '-q', '-b', 'forge/uniq')
        (repo / 'base.txt').write_text('base\nlocal-unmerged-change\n')
        _run(repo, 'add', '-A')
        _commit(repo, 'real work')
        self.assertEqual(cdb.compute_net_change(repo, 'forge/uniq', 'local'),
                         'unique')

    def test_content_already_on_main_squash_merged(self):
        repo = self._make_repo()
        # Branch adds newfile.txt.
        _run(repo, 'checkout', '-q', '-b', 'forge/sq')
        (repo / 'newfile.txt').write_text('shipped content\n')
        _run(repo, 'add', '-A')
        _commit(repo, 'add newfile')
        # Simulate a squash-merge: main independently gains the SAME content as a
        # brand-new commit (different SHA), then origin/main advances to it.
        _run(repo, 'checkout', '-q', 'main')
        (repo / 'newfile.txt').write_text('shipped content\n')
        _run(repo, 'add', '-A')
        _commit(repo, 'squash: add newfile')
        _run(repo, 'update-ref', 'refs/remotes/origin/main', 'main')
        # The branch's only changed file is now byte-identical to origin/main.
        self.assertEqual(cdb.compute_net_change(repo, 'forge/sq', 'local'),
                         'content-on-main')

    def test_rename_delete_with_matching_new_side_is_kept(self):
        # A branch that renames old.py -> new.py (net: old.py deleted, new.py
        # added) whose NEW side matches origin/main must NOT be pruned — its
        # deletion of old.py is unmerged work. Rename detection would otherwise
        # collapse this to just 'new.py' and misclassify it content-on-main.
        repo = self._make_repo()
        (repo / 'old.py').write_text('shared body\n')
        _run(repo, 'add', '-A')
        _commit(repo, 'add old.py')
        _run(repo, 'update-ref', 'refs/remotes/origin/main', 'main')
        # Branch renames old.py -> new.py.
        _run(repo, 'checkout', '-q', '-b', 'forge/rename')
        _run(repo, 'mv', 'old.py', 'new.py')
        _commit(repo, 'rename old->new')
        # origin/main independently gains new.py with identical content but STILL
        # has old.py (the rename/deletion is the branch's unmerged contribution).
        _run(repo, 'checkout', '-q', 'main')
        (repo / 'new.py').write_text('shared body\n')
        _run(repo, 'add', '-A')
        _commit(repo, 'add new.py too (old.py still present)')
        _run(repo, 'update-ref', 'refs/remotes/origin/main', 'main')
        self.assertEqual(cdb.compute_net_change(repo, 'forge/rename', 'local'),
                         'unique')

    def test_no_merge_base_is_unknown(self):
        repo = self._make_repo()
        # An orphan branch shares no history with main -> merge-base fails.
        _run(repo, 'checkout', '-q', '--orphan', 'forge/orphan')
        (repo / 'only.txt').write_text('x\n')
        _run(repo, 'add', '-A')
        _commit(repo, 'orphan root')
        self.assertEqual(cdb.compute_net_change(repo, 'forge/orphan', 'local'),
                         'unknown')


class ListCandidatesTest(GitRepoMixin, unittest.TestCase):
    def test_lists_only_dispatch_branches(self):
        repo = self._make_repo()
        for b in ('forge/a', 'mirror/review-b', 'feature/c'):
            _run(repo, 'branch', b, 'main')
        names = {n for n, _ in cdb.list_candidate_branches(repo, 'local')}
        self.assertEqual(names, {'forge/a', 'mirror/review-b'})
        self.assertNotIn('main', names)
        self.assertNotIn('feature/c', names)


# ---------- active-dispatch reconstruction ----------


class ActiveDispatchBranchesTest(unittest.TestCase):
    def test_reconstructs_from_inbox_and_in_flight(self):
        root = Path(tempfile.mkdtemp(prefix='cdb-agents-'))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(root)]))
        inboxes = root / 'inboxes'
        (inboxes / 'forge').mkdir(parents=True)
        (inboxes / 'mirror').mkdir(parents=True)
        (inboxes / 'forge' / 'task-aaa.json').write_text(
            json.dumps({'task_id': 'build-feature-001'}))
        # An explicit branch field is honored verbatim.
        (inboxes / 'forge' / 'task-bbb.json').write_text(
            json.dumps({'task_id': 'x', 'branch': 'forge/custom-name'}))
        (inboxes / 'mirror' / 'review-ccc.json').write_text(
            json.dumps({'task_id': 'review-build-feature-001'}))
        # A terminal task in .archive/ must NOT be protected.
        (inboxes / 'forge' / '.archive').mkdir()
        (inboxes / 'forge' / '.archive' / 'old.json').write_text(
            json.dumps({'task_id': 'archived-task-999'}))

        inflight = root / 'state' / 'in-flight'
        inflight.mkdir(parents=True)
        (inflight / 'live.json').write_text(json.dumps(
            {'agent_id': 'forge', 'task_stem': 'forge/live-build-002',
             'pid': os.getpid()}))
        (inflight / 'dead.json').write_text(json.dumps(
            {'agent_id': 'forge', 'task_stem': 'forge/dead-build-003',
             'pid': 999_999_999}))

        with mock.patch.object(cdb, 'INBOXES_ROOT', inboxes), \
             mock.patch.object(cdb, 'IN_FLIGHT_DIR', inflight):
            names = cdb.active_dispatch_branches()

        self.assertIn('forge/build-feature-001', names)
        self.assertIn('forge/custom-name', names)
        self.assertIn('mirror/review-build-feature-001', names)
        self.assertIn('forge/live-build-002', names)        # live pid
        self.assertNotIn('forge/dead-build-003', names)     # dead pid -> not active
        self.assertNotIn('forge/archived-task-999', names)  # .archive excluded


# ---------- deletion idempotency ----------


class DeleteLocalAccountingTest(GitRepoMixin, unittest.TestCase):
    def test_mixed_chunk_counts_real_deletions_no_misleading_log(self):
        # A chunk containing an already-gone ref must still count the genuinely
        # deleted siblings and not re-log them as 'delete failed'.
        repo = self._make_repo()
        _run(repo, 'branch', 'forge/a', 'main')
        _run(repo, 'branch', 'forge/b', 'main')
        # 'forge/ghost' was never created — git -D will fail for it, rc!=0 for
        # the whole chunk, but forge/a and forge/b are still deleted.
        with mock.patch.object(cdb, 'DELETE_CHUNK', 25):
            deleted = cdb.delete_local(repo, ['forge/a', 'forge/ghost', 'forge/b'])
        self.assertEqual(deleted, 2)
        remaining = {n for n, _ in cdb.list_candidate_branches(repo, 'local')}
        self.assertEqual(remaining, set())


class DeleteRemoteIdempotencyTest(unittest.TestCase):
    def test_already_deleted_ref_counts_as_success(self):
        def fake_git(repo, *args, timeout=None):
            # Chunk push fails; per-ref retry reports "remote ref does not exist".
            if 'push' in args and '--delete' in args:
                refs = [a for a in args[args.index('--delete') + 1:]]
                if len(refs) > 1:
                    return subprocess.CompletedProcess(args, 1, '', 'error: failed')
                return subprocess.CompletedProcess(
                    args, 1, '', 'error: unable to delete: remote ref does not exist')
            return subprocess.CompletedProcess(args, 0, '', '')

        with mock.patch.object(cdb, '_git', side_effect=fake_git):
            deleted = cdb.delete_remote(Path('/x'), ['forge/a', 'forge/b'])
        self.assertEqual(deleted, 2)


# ---------- end-to-end sweep ----------


class SweepRepoTest(GitRepoMixin, unittest.TestCase):
    def _build_backlog(self) -> Path:
        repo = self._make_repo()
        old = '2020-01-01T00:00:00'
        # forge/abandoned — empty WIP, old -> prunable.
        _run(repo, 'checkout', '-q', '-b', 'forge/abandoned-task-aaa')
        _commit(repo, '[WIP][session-start] abandoned', allow_empty=True, date=old)
        # forge/unique — real unmerged content, old -> KEEP.
        _run(repo, 'checkout', '-q', 'main')
        _run(repo, 'checkout', '-q', '-b', 'forge/unique-task-bbb')
        (repo / 'base.txt').write_text('base\nunmerged\n')
        _run(repo, 'add', '-A')
        _commit(repo, 'real work', date=old)
        # mirror/openpr — empty WIP, old, but an OPEN PR head -> KEEP.
        _run(repo, 'checkout', '-q', 'main')
        _run(repo, 'checkout', '-q', '-b', 'mirror/review-open-ccc')
        _commit(repo, '[WIP][session-start] open', allow_empty=True, date=old)
        # forge/young — empty WIP but fresh -> KEEP (age floor).
        _run(repo, 'checkout', '-q', 'main')
        _run(repo, 'checkout', '-q', '-b', 'forge/young-task-ddd')
        _commit(repo, '[WIP][session-start] young', allow_empty=True)  # now()
        _run(repo, 'checkout', '-q', 'main')
        return repo

    def test_dry_run_selects_safe_only_and_deletes_nothing(self):
        repo = self._build_backlog()
        active = {'forge/unique-task-bbb'}  # also covered by net_change=unique

        def fake_heads(r, state):
            return {'mirror/review-open-ccc'} if state == 'open' else set()

        with mock.patch.object(cdb, 'fetch_prune'), \
             mock.patch.object(cdb, '_gh_pr_heads', side_effect=fake_heads):
            res = cdb.sweep_repo(repo, active, _real_now(), dry_run=True)

        # Dry-run identifies the one safe branch, deletes nothing.
        self.assertEqual(res.local_pruned, 1)
        self.assertFalse(res.gh_unavailable)
        remaining = {n for n, _ in cdb.list_candidate_branches(repo, 'local')}
        self.assertEqual(remaining, {
            'forge/abandoned-task-aaa', 'forge/unique-task-bbb',
            'mirror/review-open-ccc', 'forge/young-task-ddd'})

    def test_live_run_prunes_only_the_abandoned_branch(self):
        repo = self._build_backlog()

        def fake_heads(r, state):
            return {'mirror/review-open-ccc'} if state == 'open' else set()

        with mock.patch.object(cdb, 'fetch_prune'), \
             mock.patch.object(cdb, '_gh_pr_heads', side_effect=fake_heads):
            res = cdb.sweep_repo(repo, set(), _real_now(), dry_run=False)

        self.assertEqual(res.local_pruned, 1)
        remaining = {n for n, _ in cdb.list_candidate_branches(repo, 'local')}
        self.assertEqual(remaining, {
            'forge/unique-task-bbb', 'mirror/review-open-ccc',
            'forge/young-task-ddd'})
        self.assertNotIn('forge/abandoned-task-aaa', remaining)

    def test_gh_unavailable_refuses_to_prune(self):
        repo = self._build_backlog()
        with mock.patch.object(cdb, 'fetch_prune'), \
             mock.patch.object(cdb, '_gh_pr_heads', return_value=None):
            res = cdb.sweep_repo(repo, set(), _real_now(), dry_run=False)
        self.assertTrue(res.gh_unavailable)
        self.assertEqual(res.local_pruned, 0)
        # Nothing deleted.
        remaining = {n for n, _ in cdb.list_candidate_branches(repo, 'local')}
        self.assertIn('forge/abandoned-task-aaa', remaining)

    def test_batch_cap_defers_overflow(self):
        repo = self._make_repo()
        old = '2020-01-01T00:00:00'
        for i in range(5):
            _run(repo, 'checkout', '-q', 'main')
            _run(repo, 'checkout', '-q', '-b', f'forge/abandoned-{i:03d}')
            _commit(repo, '[WIP][session-start]', allow_empty=True, date=old)
        _run(repo, 'checkout', '-q', 'main')

        with mock.patch.object(cdb, 'fetch_prune'), \
             mock.patch.object(cdb, '_gh_pr_heads', return_value=set()), \
             mock.patch.object(cdb, 'MAX_LOCAL_DELETES_PER_RUN', 2):
            res = cdb.sweep_repo(repo, set(), _real_now(), dry_run=False)

        self.assertEqual(res.local_pruned, 2)
        self.assertEqual(res.local_capped, 3)
        remaining = {n for n, _ in cdb.list_candidate_branches(repo, 'local')}
        self.assertEqual(len(remaining), 3)  # 5 - 2 pruned


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
