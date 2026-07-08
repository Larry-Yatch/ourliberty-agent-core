"""Tests for scripts/heal_undispatched_pr_review.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Covers the pure, dependency-free core:
- parse_open_prs: normalize gh JSON (incl. isDraft + label names + _repo), skip bad
- task_id_for_branch: strip forge/ for Forge PRs, repo-qualified pr-id for opt-in
- _is_reviewable_pr: forge always; else only when auto-review-labeled AND non-draft
- fetch_open_prs: multi-repo combine; None only when EVERY repo's gh call failed
- select_orphaned_prs: reviewability filter, age grace, already-dispatched skip,
  unparseable createdAt skip, task_id augmentation
- _synthesize_build_data: the envelope handed to _dispatch_mirror_review
- healer_enabled: env activation default-ON with dry-run override
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import heal_undispatched_pr_review as h  # noqa: E402

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _pr(number, branch, *, age_minutes=60, last_commit_minutes=None, url=None,
        title='t', is_draft=False, labels=(),
        repo='Larry-Yatch/ourliberty-agent-core'):
    created = (NOW - timedelta(minutes=age_minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
    row = {
        'number': number,
        'url': url or f'https://github.com/{repo}/pull/{number}',
        'headRefName': branch,
        'createdAt': created,
        'title': title,
        'isDraft': is_draft,
        'labels': list(labels),
        '_repo': repo,
    }
    # When set, the newest-commit timestamp the hand-PR grace debounces on.
    if last_commit_minutes is not None:
        row['lastCommitAt'] = (
            NOW - timedelta(minutes=last_commit_minutes)
        ).strftime('%Y-%m-%dT%H:%M:%SZ')
    return row


class TestParseOpenPrs(unittest.TestCase):
    def test_parses_well_formed_rows(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi"}]')
        rows = h.parse_open_prs(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['number'], 1)
        self.assertEqual(rows[0]['headRefName'], 'forge/a')

    def test_skips_rows_missing_required_fields(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a"},'  # no createdAt
               '{"number":2,"url":"https://x/2","headRefName":"forge/b",'
               '"createdAt":"2026-06-10T00:00:00Z"}]')
        rows = h.parse_open_prs(raw)
        self.assertEqual([r['number'] for r in rows], [2])

    def test_bad_json_and_non_list_return_empty(self):
        self.assertEqual(h.parse_open_prs('not json'), [])
        self.assertEqual(h.parse_open_prs('{"a":1}'), [])
        self.assertEqual(h.parse_open_prs(''), [])

    def test_carries_is_draft(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi","isDraft":true}]')
        rows = h.parse_open_prs(raw)
        self.assertTrue(rows[0]['isDraft'])

    def test_is_draft_defaults_false_when_absent(self):
        # gh always returns a requested field, but a mock / older gh might omit it.
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi"}]')
        self.assertFalse(h.parse_open_prs(raw)[0]['isDraft'])

    def test_reduces_commits_to_newest_last_commit_at(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi","commits":['
               '{"committedDate":"2026-06-10T01:00:00Z"},'
               '{"committedDate":"2026-06-10T03:00:00Z"},'
               '{"committedDate":"2026-06-10T02:00:00Z"}]}]')
        self.assertEqual(
            h.parse_open_prs(raw)[0]['lastCommitAt'], '2026-06-10T03:00:00Z')

    def test_last_commit_at_none_when_commits_absent_or_empty(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi"},'
               '{"number":2,"url":"https://x/2","headRefName":"forge/b",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi","commits":[]}]')
        rows = h.parse_open_prs(raw)
        self.assertIsNone(rows[0]['lastCommitAt'])
        self.assertIsNone(rows[1]['lastCommitAt'])

    def test_malformed_commits_do_not_crash_row_survives(self):
        # commits with non-dicts / dicts lacking committedDate must not crash the
        # parser; the row survives with lastCommitAt None (createdAt fallback).
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi",'
               '"commits":[{"foo":1},"notadict",null]}]')
        rows = h.parse_open_prs(raw)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['lastCommitAt'])

    def test_commits_not_a_list_is_tolerated(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi","commits":"oops"}]')
        rows = h.parse_open_prs(raw)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['lastCommitAt'])

    def test_carries_label_names(self):
        # gh returns labels as [{name,color,...}]; we flatten to names.
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi",'
               '"labels":[{"name":"auto-review","color":"ededed"},{"name":"bug"}]}]')
        self.assertEqual(h.parse_open_prs(raw)[0]['labels'], ['auto-review', 'bug'])

    def test_labels_defaults_empty_when_absent_or_malformed(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi","labels":null}]')
        self.assertEqual(h.parse_open_prs(raw)[0]['labels'], [])

    def test_tags_repo(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi"}]')
        rows = h.parse_open_prs(raw, 'Larry-Yatch/ourliberty-dashboard')
        self.assertEqual(rows[0]['_repo'], 'Larry-Yatch/ourliberty-dashboard')
        # repo is optional (pure-parse callers) → _repo is None when omitted.
        self.assertIsNone(h.parse_open_prs(raw)[0]['_repo'])


class TestTaskIdForBranch(unittest.TestCase):
    def test_strips_forge_prefix(self):
        self.assertEqual(h.task_id_for_branch('forge/harden-x-001'), 'harden-x-001')

    def test_passthrough_non_forge(self):
        self.assertEqual(h.task_id_for_branch('main'), 'main')

    def test_human_branch_keys_by_pr_number_no_repo(self):
        # No forge/ prefix, no repo → bare pr-<number> fallback.
        self.assertEqual(h.task_id_for_branch('fix/install-drift-dedup', 625), 'pr-625')

    def test_opt_in_branch_keys_by_repo_and_pr_number(self):
        # With a repo, the opt-in id is repo-qualified so PR #N in two repos can't
        # collide onto one review-request dedup key.
        self.assertEqual(
            h.task_id_for_branch('fix/x', 80, 'Larry-Yatch/ourliberty-dashboard'),
            'pr-ourliberty-dashboard-80')
        self.assertEqual(
            h.task_id_for_branch('work/y', 80, 'Larry-Yatch/ourliberty-agent-core'),
            'pr-ourliberty-agent-core-80')

    def test_forge_prefix_wins_over_pr_number_and_repo(self):
        # A forge build PR keeps its branch-derived id (NOT repo-qualified) even
        # with number+repo present, so the backstop's filename lines up with the
        # inline dispatch's key.
        self.assertEqual(
            h.task_id_for_branch('forge/harden-x-001', 412,
                                 'Larry-Yatch/ourliberty-dashboard'),
            'harden-x-001')


class TestIsReviewablePr(unittest.TestCase):
    LBL = [h.AUTO_REVIEW_LABEL]

    def test_forge_always_reviewable_regardless_of_label_or_draft(self):
        self.assertTrue(h._is_reviewable_pr('forge/x', is_draft=False, labels=[]))
        self.assertTrue(h._is_reviewable_pr('forge/x', is_draft=True, labels=[]))

    def test_claude_non_draft_is_reviewable_without_label(self):
        # forge-cold-start-revision S4: claude/* (Claude Code laptop PRs) are a
        # reliable exclusive prefix → auto-routed when non-draft, no label needed.
        self.assertTrue(
            h._is_reviewable_pr('claude/missions-bug-k404pz', is_draft=False, labels=[]))

    def test_claude_draft_is_not_reviewable(self):
        self.assertFalse(
            h._is_reviewable_pr('claude/wip-x', is_draft=True, labels=[]))

    def test_labeled_non_draft_is_reviewable(self):
        self.assertTrue(h._is_reviewable_pr('fix/x', is_draft=False, labels=self.LBL))
        self.assertTrue(h._is_reviewable_pr('docs/x', is_draft=False, labels=self.LBL))

    def test_unlabeled_non_forge_is_not_reviewable(self):
        # The agent-PR safety case: a non-draft fix/feat/chore PR with no opt-in
        # label is left alone (it may be agent-authored, must not auto-merge).
        self.assertFalse(h._is_reviewable_pr('fix/x', is_draft=False, labels=[]))
        self.assertFalse(h._is_reviewable_pr('feat/new-mission-abc', is_draft=False, labels=[]))

    def test_labeled_but_draft_is_not_reviewable(self):
        self.assertFalse(h._is_reviewable_pr('fix/x', is_draft=True, labels=self.LBL))

    def test_empty_branch_not_reviewable(self):
        self.assertFalse(h._is_reviewable_pr('', is_draft=False, labels=self.LBL))

    def test_none_labels_tolerated(self):
        self.assertFalse(h._is_reviewable_pr('fix/x', is_draft=False, labels=None))


class TestSelectOrphanedPrs(unittest.TestCase):
    def _none_dispatched(self, _task_id, _head_sha=None):
        return False

    def test_selects_forge_pr_past_grace_with_no_review(self):
        prs = [_pr(412, 'forge/harden-test-prod-write-isolation-001', age_minutes=60)]
        sel = h.select_orphaned_prs(prs, NOW, self._none_dispatched)
        self.assertEqual([p['number'] for p in sel], [412])
        self.assertEqual(sel[0]['task_id'], 'harden-test-prod-write-isolation-001')

    def test_selects_labeled_non_draft_pr(self):
        # The #509/#510/#625 gap: a non-draft PR carrying auto-review is now
        # routed, keyed by repo-qualified PR number.
        prs = [_pr(625, 'fix/install-drift-dedup', age_minutes=60,
                   labels=[h.AUTO_REVIEW_LABEL])]
        sel = h.select_orphaned_prs(prs, NOW, self._none_dispatched)
        self.assertEqual([p['number'] for p in sel], [625])
        self.assertEqual(sel[0]['task_id'], 'pr-ourliberty-agent-core-625')

    def test_selects_labeled_dashboard_pr(self):
        # Multi-repo: a labeled non-draft DASHBOARD PR is routed too (the #80/#81
        # gap), keyed by the dashboard-qualified id so it can't collide with an
        # agent-core PR of the same number.
        prs = [_pr(80, 'work/p7-universal-card', age_minutes=60,
                   labels=[h.AUTO_REVIEW_LABEL],
                   repo='Larry-Yatch/ourliberty-dashboard')]
        sel = h.select_orphaned_prs(prs, NOW, self._none_dispatched)
        self.assertEqual([p['number'] for p in sel], [80])
        self.assertEqual(sel[0]['task_id'], 'pr-ourliberty-dashboard-80')

    def test_skips_unlabeled_non_forge_pr(self):
        # Safety: a non-draft non-forge PR with NO opt-in label is left alone —
        # it could be agent-authored (e.g. feat/new-mission-* must be CLOSED, not
        # merged). This is the blocker that killed the branch-only design.
        prs = [_pr(1, 'fix/some-agent-work', age_minutes=60),
               _pr(2, 'feat/new-mission-xyz', age_minutes=60)]
        self.assertEqual(h.select_orphaned_prs(prs, NOW, self._none_dispatched), [])

    def test_skips_labeled_draft_pr(self):
        # Draft is the "still iterating" safety valve — never auto-routed even when
        # labeled (a Mirror PASS auto-merges, so a draft must not be picked up).
        prs = [_pr(3, 'fix/wip', age_minutes=60, is_draft=True,
                   labels=[h.AUTO_REVIEW_LABEL])]
        self.assertEqual(h.select_orphaned_prs(prs, NOW, self._none_dispatched), [])

    def test_forge_pr_routed_even_if_draft(self):
        # Forge build PRs are reviewed unconditionally; draft state is ignored.
        prs = [_pr(11, 'forge/x', age_minutes=60, is_draft=True)]
        self.assertEqual([p['number'] for p in
                          h.select_orphaned_prs(prs, NOW, self._none_dispatched)], [11])

    def test_skips_pr_within_grace_window(self):
        # Created 5 min ago, grace is 10 min → too fresh, let inline path fire.
        prs = [_pr(3, 'forge/fresh', age_minutes=5)]
        self.assertEqual(h.select_orphaned_prs(prs, NOW, self._none_dispatched), [])

    def test_selects_at_grace_boundary(self):
        prs = [_pr(4, 'forge/edge', age_minutes=h.DISPATCH_GRACE_MINUTES + 1)]
        self.assertEqual([p['number'] for p in
                          h.select_orphaned_prs(prs, NOW, self._none_dispatched)], [4])

    def test_skips_already_dispatched(self):
        prs = [_pr(5, 'forge/done', age_minutes=60)]
        sel = h.select_orphaned_prs(
            prs, NOW, already_dispatched=lambda t, head=None: True)
        self.assertEqual(sel, [])

    def test_forwards_current_head_sha_to_probe(self):
        # The PR's headRefOid must reach the dedup probe so a review of an
        # OLDER head doesn't mask a needed re-review of the current head.
        seen = {}

        def probe(task_id, head_sha=None):
            seen['task_id'] = task_id
            seen['head_sha'] = head_sha
            return False

        pr = _pr(9, 'forge/x', age_minutes=60)
        pr['headRefOid'] = 'deadbeefcafe0000'
        sel = h.select_orphaned_prs([pr], NOW, probe)
        self.assertEqual([p['number'] for p in sel], [9])
        self.assertEqual(seen['head_sha'], 'deadbeefcafe0000')

    def test_re_reviews_when_only_older_head_dispatched(self):
        # Probe says "dispatched" only for the OLD head; current head differs →
        # the PR is selected for a fresh review (the head-drift fix).
        reviewed_head = 'aaaaaaaaaaaa'

        def probe(_task_id, head_sha=None):
            return head_sha == reviewed_head

        pr = _pr(10, 'forge/x', age_minutes=60)
        pr['headRefOid'] = 'bbbbbbbbbbbb'  # PR advanced past the reviewed head
        sel = h.select_orphaned_prs([pr], NOW, probe)
        self.assertEqual([p['number'] for p in sel], [10])
        # And when the current head IS the reviewed one, it's skipped.
        pr['headRefOid'] = reviewed_head
        self.assertEqual(h.select_orphaned_prs([pr], NOW, probe), [])

    def test_skips_unparseable_created_at(self):
        bad = _pr(6, 'forge/bad', age_minutes=60)
        bad['createdAt'] = 'not-a-date'
        self.assertEqual(h.select_orphaned_prs([bad], NOW, self._none_dispatched), [])

    def test_dispatched_probe_exception_treated_as_undispatched(self):
        def boom(_t, _head=None):
            raise RuntimeError('probe down')
        prs = [_pr(7, 'forge/x', age_minutes=60)]
        sel = h.select_orphaned_prs(prs, NOW, boom)
        self.assertEqual([p['number'] for p in sel], [7])  # fail-open to dispatch

    def test_custom_grace_minutes(self):
        prs = [_pr(8, 'forge/x', age_minutes=20)]
        # With a 30-min grace, a 20-min-old PR is still too fresh.
        self.assertEqual(
            h.select_orphaned_prs(prs, NOW, self._none_dispatched, grace_minutes=30), [])

    # ---- per-class grace: hand/claude PRs debounce on last commit ----

    def test_hand_pr_dispatched_fast_when_last_commit_is_old(self):
        # The fix: a just-OPENED auto-review PR whose last commit is well past the
        # short hand grace dispatches now — it no longer eats the full 10-min Forge
        # grace (which only exists to let inline dispatch win, and hand PRs have no
        # inline path).
        prs = [_pr(50, 'work/handspun', age_minutes=0, last_commit_minutes=10,
                   labels=[h.AUTO_REVIEW_LABEL])]
        sel = h.select_orphaned_prs(prs, NOW, self._none_dispatched)
        self.assertEqual([p['number'] for p in sel], [50])

    def test_hand_pr_within_last_commit_grace_skipped(self):
        # Still pushing: last commit 1 min ago (< HAND_PR_GRACE_MINUTES) → debounced
        # even though the PR itself was opened an hour ago.
        prs = [_pr(51, 'work/handspun', age_minutes=60, last_commit_minutes=1,
                   labels=[h.AUTO_REVIEW_LABEL])]
        self.assertEqual(h.select_orphaned_prs(prs, NOW, self._none_dispatched), [])

    def test_hand_pr_falls_back_to_created_at_without_commit_data(self):
        # No lastCommitAt (older gh / mock) → gate on createdAt with the SHORT
        # hand grace, not the long Forge one.
        fresh = _pr(52, 'work/handspun', age_minutes=1, labels=[h.AUTO_REVIEW_LABEL])
        self.assertEqual(h.select_orphaned_prs([fresh], NOW, self._none_dispatched), [])
        settled = _pr(53, 'work/handspun', age_minutes=5, labels=[h.AUTO_REVIEW_LABEL])
        self.assertEqual([p['number'] for p in
                          h.select_orphaned_prs([settled], NOW, self._none_dispatched)],
                         [53])

    def test_forge_pr_ignores_last_commit_and_uses_long_open_grace(self):
        # Regression: Forge PRs are UNCHANGED — gated on createdAt with the full
        # grace, ignoring lastCommitAt. A fresh commit neither makes a 15-min-old
        # forge PR wait nor dispatches a 5-min-old one early.
        old = _pr(54, 'forge/x', age_minutes=15, last_commit_minutes=0)
        self.assertEqual([p['number'] for p in
                          h.select_orphaned_prs([old], NOW, self._none_dispatched)], [54])
        fresh = _pr(55, 'forge/x', age_minutes=5, last_commit_minutes=0)
        self.assertEqual(h.select_orphaned_prs([fresh], NOW, self._none_dispatched), [])

    def test_custom_hand_grace_minutes(self):
        # 4-min-old last commit is within a 5-min hand grace → still debounced.
        prs = [_pr(56, 'work/handspun', age_minutes=60, last_commit_minutes=4,
                   labels=[h.AUTO_REVIEW_LABEL])]
        self.assertEqual(
            h.select_orphaned_prs(prs, NOW, self._none_dispatched,
                                  hand_grace_minutes=5), [])


class TestSynthesizeBuildData(unittest.TestCase):
    def test_envelope_shape(self):
        pr = {**_pr(412, 'forge/harden-x-001'), 'task_id': 'harden-x-001'}
        data = h._synthesize_build_data(pr)
        self.assertEqual(data['task_id'], 'harden-x-001')
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/harden-x-001')
        self.assertEqual(data['dispatched_by'], 'heal-undispatched-pr-review')
        # No claude_session_id — the build session is gone; revisions start fresh.
        self.assertNotIn('claude_session_id', data)

    def test_threads_head_sha_from_headRefOid(self):
        # The PR head we already have from gh-pr-list rides the envelope so the
        # dispatch records the right commit without a second gh round-trip.
        pr = {**_pr(412, 'forge/harden-x-001'), 'task_id': 'harden-x-001',
              'headRefOid': 'cafe1234beef5678'}
        self.assertEqual(
            h._synthesize_build_data(pr)['head_sha'], 'cafe1234beef5678')

    def test_target_repo_follows_pr_repo(self):
        # A dashboard PR's envelope targets the dashboard repo (repo-agnostic
        # pipeline), not a hardcoded agent-core.
        pr = {**_pr(80, 'work/x', repo='Larry-Yatch/ourliberty-dashboard'),
              'task_id': 'pr-ourliberty-dashboard-80'}
        self.assertEqual(
            h._synthesize_build_data(pr)['target_repo'], 'ourliberty-dashboard')

    def test_target_repo_defaults_when_repo_absent(self):
        # Defensive: a row with no _repo (e.g. older fixture) falls back to
        # agent-core rather than emitting an envelope with no target_repo.
        pr = {**_pr(1, 'forge/x'), 'task_id': 'x'}
        pr.pop('_repo', None)
        self.assertEqual(
            h._synthesize_build_data(pr)['target_repo'], 'ourliberty-agent-core')


class TestFetchOpenPrs(unittest.TestCase):
    """Multi-repo combine semantics (the per-repo gh call is mocked)."""

    def _run(self, per_repo):
        # per_repo: dict repo -> list|None (None models a gh failure for that repo)
        from unittest import mock
        with mock.patch.object(h, 'REPOS',
                               ('Larry-Yatch/ourliberty-agent-core',
                                'Larry-Yatch/ourliberty-dashboard')), \
                mock.patch.object(h, '_fetch_repo_prs',
                                  side_effect=lambda repo: per_repo.get(repo)):
            return h.fetch_open_prs()

    def test_combines_rows_from_both_repos(self):
        out = self._run({
            'Larry-Yatch/ourliberty-agent-core': [{'number': 1}],
            'Larry-Yatch/ourliberty-dashboard': [{'number': 2}],
        })
        self.assertEqual(sorted(r['number'] for r in out), [1, 2])

    def test_one_repo_failing_still_returns_the_other(self):
        # A single-repo gh hiccup must NOT blind the sweep to the other repo.
        out = self._run({
            'Larry-Yatch/ourliberty-agent-core': None,          # errored
            'Larry-Yatch/ourliberty-dashboard': [{'number': 2}],
        })
        self.assertEqual([r['number'] for r in out], [2])

    def test_all_repos_failing_returns_none(self):
        # Only None when EVERY repo failed → main logs "gh unavailable".
        self.assertIsNone(self._run({
            'Larry-Yatch/ourliberty-agent-core': None,
            'Larry-Yatch/ourliberty-dashboard': None,
        }))

    def test_empty_repos_is_success_not_failure(self):
        # Repos with zero open PRs are a success ([]), distinct from a gh failure.
        out = self._run({
            'Larry-Yatch/ourliberty-agent-core': [],
            'Larry-Yatch/ourliberty-dashboard': [],
        })
        self.assertEqual(out, [])


class TestFetchRepoPrsCommitsFallback(unittest.TestCase):
    """_fetch_repo_prs retries WITHOUT `commits` when gh rejects the field, so a gh
    too old to know it degrades to no lastCommitAt instead of blinding the sweep."""

    def _proc(self, returncode, stdout='', stderr=''):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_retries_without_commits_on_field_error(self):
        from unittest import mock
        good = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
                '"createdAt":"2026-06-10T00:00:00Z","title":"hi"}]')
        calls = []

        def fake_run(cmd, **kw):
            fields = cmd[cmd.index('--json') + 1]
            calls.append(fields)
            if 'commits' in fields:
                return self._proc(1, stderr='Unknown JSON field: "commits"')
            return self._proc(0, stdout=good)

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rows = h._fetch_repo_prs('Larry-Yatch/ourliberty-agent-core')
        self.assertEqual([r['number'] for r in rows], [1])
        self.assertIsNone(rows[0]['lastCommitAt'])  # degraded, not dark
        self.assertEqual(len(calls), 2)
        self.assertIn('commits', calls[0])
        self.assertNotIn('commits', calls[1])

    def test_uses_commits_with_no_retry_when_supported(self):
        from unittest import mock
        good = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
                '"createdAt":"2026-06-10T00:00:00Z","title":"hi",'
                '"commits":[{"committedDate":"2026-06-10T05:00:00Z"}]}]')
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[cmd.index('--json') + 1])
            return self._proc(0, stdout=good)

        with mock.patch.object(h.subprocess, 'run', side_effect=fake_run):
            rows = h._fetch_repo_prs('Larry-Yatch/ourliberty-agent-core')
        self.assertEqual(rows[0]['lastCommitAt'], '2026-06-10T05:00:00Z')
        self.assertEqual(len(calls), 1)  # supported → no retry

    def test_returns_none_when_both_attempts_fail(self):
        from unittest import mock
        with mock.patch.object(
                h.subprocess, 'run',
                side_effect=lambda cmd, **kw: self._proc(
                    1, stderr='gh: not authenticated')):
            self.assertIsNone(
                h._fetch_repo_prs('Larry-Yatch/ourliberty-agent-core'))


class TestHealerEnabled(unittest.TestCase):
    def test_default_on(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.ENV_ENABLED, None)
            self.assertTrue(h.healer_enabled())

    def test_explicit_false_is_dry_run(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {h.ENV_ENABLED: 'false'}):
            self.assertFalse(h.healer_enabled())

    def test_explicit_true(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {h.ENV_ENABLED: 'true'}):
            self.assertTrue(h.healer_enabled())


class _FakeNotifier:
    """Stand-in for outbox_notifier: tracks which tasks have a dispatched review.
    `dispatch_succeeds` controls whether _dispatch_mirror_review actually records
    one (modelling RoutingDenied/missing-target_repo, which the real call swallows
    as a WARN and returns)."""

    def __init__(self, dispatch_succeeds=True, pr_open=True):
        self.dispatched: set[str] = set()
        self.dispatch_calls: list[tuple[dict, str]] = []
        self.dispatch_succeeds = dispatch_succeeds
        self.pr_open = pr_open  # for the TOCTOU recheck

    def _review_request_already_dispatched(self, fname, current_head_sha=None):
        return fname in self.dispatched

    def _dispatch_mirror_review(self, data, url):
        self.dispatch_calls.append((data, url))
        if self.dispatch_succeeds:
            self.dispatched.add(f'review-{data["task_id"]}.json')

    def _parse_pr_url(self, url):
        return ('Larry-Yatch/ourliberty-agent-core', 412)

    def _gh_pr_is_open(self, repo_coords, pr_number):
        return self.pr_open


class _FakeSafeWriteInbox:
    # main() derives Mirror's inbox path from INBOXES_ROOT for two probes:
    # the post-dispatch `.claimed/` race check and recent_review_record. Default
    # at a nonexistent tree so both find nothing (empty `.claimed/` ⇒ False; the
    # recent-review scan hits its OSError no-match leg), keeping the existing
    # main-flow tests' outcomes (e.g. a failed dispatch still escalates) unless a
    # test patches the probe or overrides INBOXES_ROOT.
    INBOXES_ROOT = Path('/nonexistent-test-inboxes-root')

    @staticmethod
    def canonical_inbox_name(name):
        return name


class TestMainDispatchFlow(unittest.TestCase):
    """Exercise main()'s effectful dispatch→verify→escalate-once flow with the
    notifier/safe_write_inbox deps injected via sys.modules."""

    @staticmethod
    def _pr_realnow(number, branch, age_minutes=60, *, is_draft=False, labels=(),
                    repo='Larry-Yatch/ourliberty-agent-core', head=None):
        # main() compares createdAt against the real wall clock, so these
        # fixtures must be dated relative to actual now (not the fixed NOW).
        created = (datetime.now(timezone.utc)
                   - timedelta(minutes=age_minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
        return {
            'number': number,
            'url': f'https://github.com/{repo}/pull/{number}',
            'headRefName': branch,
            'createdAt': created,
            'title': 't',
            'headRefOid': head,
            'isDraft': is_draft,
            'labels': list(labels),
            '_repo': repo,
        }

    def _run_main(self, *, prs, dispatch_succeeds, enabled=True, failed=None,
                  pr_open=True, inboxes_root=None, head_status=False,
                  recent_review=None):
        import os
        from unittest import mock
        fake_notifier = _FakeNotifier(dispatch_succeeds=dispatch_succeeds,
                                      pr_open=pr_open)
        # Per-test INBOXES_ROOT override without mutating the shared fake class,
        # so the `.claimed/` race check can be pointed at a populated tree.
        fake_swi = _FakeSafeWriteInbox
        if inboxes_root is not None:
            fake_swi = type('_SWI', (_FakeSafeWriteInbox,),
                            {'INBOXES_ROOT': Path(inboxes_root)})
        state = {'failed_prs': dict(failed or {})}
        saved = {}
        alerts: list[dict] = []
        env = {h.ENV_ENABLED: 'true' if enabled else 'false'}
        with mock.patch.dict(sys.modules, {
                    'outbox_notifier': fake_notifier,
                    'safe_write_inbox': fake_swi,
                }), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(h, 'kill_switch_active', return_value=False), \
                mock.patch.object(h, 'heartbeat'), \
                mock.patch.object(h, 'fetch_open_prs', return_value=prs), \
                mock.patch.object(h, 'load_state', return_value=state), \
                mock.patch.object(h, 'save_state',
                                  side_effect=lambda s: saved.update(s)), \
                mock.patch.object(h, 'head_has_passing_review_status',
                                  return_value=head_status) as status_probe, \
                mock.patch.object(h, 'recent_review_record',
                                  return_value=recent_review) as recent_probe, \
                mock.patch.object(h, 'emit_failed_alert',
                                  side_effect=lambda pr: alerts.append(pr) or True):
            rc = h.main()
        self._status_probe = status_probe
        self._recent_probe = recent_probe
        return rc, fake_notifier, saved, alerts

    def test_successful_dispatch_no_alert(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(fn.dispatch_calls), 1)
        self.assertEqual(fn.dispatch_calls[0][0]['task_id'], 'harden-x-001')
        self.assertEqual(alerts, [])
        self.assertEqual(saved.get('failed_prs'), {})

    def test_failed_dispatch_escalates_once(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=False)
        self.assertEqual(len(fn.dispatch_calls), 1)
        self.assertEqual(len(alerts), 1)  # escalated
        self.assertIn(prs[0]['url'].rstrip('/'), saved.get('failed_prs', {}))

    def test_already_escalated_pr_not_re_alerted(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        url = prs[0]['url'].rstrip('/')
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=False, failed={url: 'earlier'})
        self.assertEqual(len(fn.dispatch_calls), 1)  # still attempts dispatch
        self.assertEqual(alerts, [])  # but does NOT re-page

    def test_dry_run_dispatches_nothing(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, enabled=False)
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])

    def test_recovered_pr_clears_prior_failure(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        url = prs[0]['url'].rstrip('/')
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, failed={url: 'earlier'})
        # Dispatch now succeeds → the stale failure record is cleared.
        self.assertNotIn(url, saved.get('failed_prs', {}))

    def test_toctou_pr_closed_since_listing_skips_dispatch(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, pr_open=False)
        self.assertEqual(fn.dispatch_calls, [])  # merged/closed → not reviewed
        self.assertEqual(alerts, [])

    def test_gh_unavailable_is_clean_noop(self):
        rc, fn, saved, alerts = self._run_main(prs=None, dispatch_succeeds=True)
        self.assertEqual(rc, 0)
        self.assertEqual(fn.dispatch_calls, [])

    def test_labeled_pr_dispatched_with_repo_qualified_task_id(self):
        # End-to-end: a non-draft auto-review-labeled agent-core PR routes a Mirror
        # review keyed by the repo-qualified pr id, targeting its own repo.
        prs = [self._pr_realnow(625, 'fix/install-drift-dedup', age_minutes=60,
                                labels=[h.AUTO_REVIEW_LABEL])]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(len(fn.dispatch_calls), 1)
        data, _url = fn.dispatch_calls[0]
        self.assertEqual(data['task_id'], 'pr-ourliberty-agent-core-625')
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(alerts, [])

    def test_labeled_dashboard_pr_dispatched_to_dashboard(self):
        # Multi-repo end-to-end: a labeled DASHBOARD PR routes a review targeting
        # the dashboard repo, keyed by the dashboard-qualified id.
        prs = [self._pr_realnow(80, 'work/p7-universal-card', age_minutes=60,
                                labels=[h.AUTO_REVIEW_LABEL],
                                repo='Larry-Yatch/ourliberty-dashboard')]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(len(fn.dispatch_calls), 1)
        data, _url = fn.dispatch_calls[0]
        self.assertEqual(data['task_id'], 'pr-ourliberty-dashboard-80')
        self.assertEqual(data['target_repo'], 'ourliberty-dashboard')
        self.assertEqual(alerts, [])

    def test_unlabeled_pr_dispatches_nothing(self):
        # No opt-in label → left alone (the agent-PR safety case), end-to-end.
        prs = [self._pr_realnow(626, 'fix/some-work', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])

    def test_labeled_draft_pr_dispatches_nothing(self):
        prs = [self._pr_realnow(627, 'fix/wip', age_minutes=60, is_draft=True,
                                labels=[h.AUTO_REVIEW_LABEL])]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])

    def test_claimed_race_dispatch_suppresses_false_positive_alert(self):
        # The exact false-positive shape (G-rule heal-undispatched-pr-review-
        # claimed-race-fp-001): the backstop dispatches a review, inbox_watcher
        # relocates the task into `.claimed/<slot>/` within ~2-3s, so the shared
        # presence predicate (modelled by dispatch_succeeds=False ⇒ nothing in
        # the inbox root / archive / .invalid) reads it as absent. Because the
        # task IS in a `.claimed/` slot, the dispatch must count as landed and
        # NO critical alert may fire.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            slot = Path(td) / 'mirror' / '.claimed' / '2'
            slot.mkdir(parents=True)
            (slot / 'review-harden-x-001.json').write_text('{}')
            prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
            rc, fn, saved, alerts = self._run_main(
                prs=prs, dispatch_succeeds=False, inboxes_root=td)
        self.assertEqual(len(fn.dispatch_calls), 1)  # it did try to dispatch
        self.assertEqual(alerts, [])  # …but the FP page is suppressed
        self.assertEqual(saved.get('failed_prs'), {})  # no failure-ledger entry

    # ---- ground-truth back-off (the PR #865 triple-dispatch, 2026-07-08) ----

    def test_passing_head_skips_dispatch(self):
        # A SUCCESS mirror-review status on the CURRENT head = this head already
        # PASSED; the backstop must stand down (the 17:00Z dup: re-reviewing a
        # PASSed head overwrote a merged PR's findings comment with a stale red).
        # recency (mocked None) runs first and doesn't block → status probe runs.
        prs = [self._pr_realnow(865, 'forge/completeness-pr3-build',
                                age_minutes=60, head='6de22a9058eb')]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, head_status=True)
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])
        self._status_probe.assert_called_once_with(
            'Larry-Yatch/ourliberty-agent-core', '6de22a9058eb')

    def test_status_error_or_failure_falls_through_to_dispatch(self):
        # No PASS status (gh error OR a failure/REVISION status → probe False)
        # and no recent record → fall through and rescue, NOT a permanent skip
        # (the fail-quiet anti-pattern). The window-bounded recency guard, not
        # this terminal status check, is what holds off an in-flight REVISION.
        prs = [self._pr_realnow(865, 'forge/completeness-pr3-build',
                                age_minutes=60, head='6de22a9058eb')]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, head_status=False,
            recent_review=None)
        self.assertEqual(len(fn.dispatch_calls), 1)
        self.assertEqual(alerts, [])

    def test_recent_review_activity_skips_dispatch(self):
        # A review record for the task written inside the window (incl. the
        # -revN names the dedup never matches) = cascade mid-cycle; stand down
        # (the 16:35Z dup: head moved while the REVISION round was in flight).
        # recency runs BEFORE the gh status probe, so the probe isn't called.
        prs = [self._pr_realnow(865, 'forge/completeness-pr3-build',
                                age_minutes=60, head='668573d21e95')]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, head_status=True,
            recent_review='.archive/review-completeness-pr3-build-rev1.json '
                          'written 2m ago')
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])
        self._status_probe.assert_not_called()  # local recency short-circuits

    def test_no_status_no_recent_record_dispatches(self):
        # The genuine #412 orphan shape: no PASS status on the head, no review
        # records at all → the backstop acts, exactly as before.
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60,
                                head='abc123')]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, head_status=False,
            recent_review=None)
        self.assertEqual(len(fn.dispatch_calls), 1)
        self.assertEqual(alerts, [])

    def test_missing_head_skips_status_probe_but_still_checks_recency(self):
        # No headRefOid (older gh / mocks): can't consult commit statuses, but
        # the recency guard still applies before dispatching.
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, head_status=True)
        self._status_probe.assert_not_called()
        self._recent_probe.assert_called_once()
        self.assertEqual(len(fn.dispatch_calls), 1)


class TestReviewTaskInClaimed(unittest.TestCase):
    """Pure helper: is the review task present in any `.claimed/<slot>/`?"""

    def _mirror(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        mirror = Path(self._td.name) / 'mirror'
        mirror.mkdir(parents=True)
        return mirror

    def test_present_in_slot_returns_true(self):
        mirror = self._mirror()
        slot = mirror / '.claimed' / '1'
        slot.mkdir(parents=True)
        (slot / 'review-pr-ourliberty-agent-core-910.json').write_text('{}')
        self.assertTrue(h._review_task_in_claimed(
            'review-pr-ourliberty-agent-core-910.json', mirror))

    def test_present_in_slot_zero_returns_true(self):
        mirror = self._mirror()
        slot = mirror / '.claimed' / '0'
        slot.mkdir(parents=True)
        (slot / 'review-pr-ourliberty-agent-core-910.json').write_text('{}')
        self.assertTrue(h._review_task_in_claimed(
            'review-pr-ourliberty-agent-core-910.json', mirror))

    def test_absent_from_all_slots_returns_false(self):
        mirror = self._mirror()
        (mirror / '.claimed' / '0').mkdir(parents=True)
        (mirror / '.claimed' / '1').mkdir(parents=True)
        (mirror / '.claimed' / '0' / 'review-other-999.json').write_text('{}')
        self.assertFalse(h._review_task_in_claimed(
            'review-pr-ourliberty-agent-core-910.json', mirror))

    def test_missing_claimed_dir_returns_false_without_raising(self):
        mirror = self._mirror()  # no `.claimed` created at all
        self.assertFalse(h._review_task_in_claimed(
            'review-pr-ourliberty-agent-core-910.json', mirror))


class TestHeadHasPassingReviewStatus(unittest.TestCase):
    """The gh combined-status probe: True only on a definitive SUCCESS
    mirror-review status; False on a failure/absent status OR any gh error
    (so the caller falls through to the recency guard, never a permanent skip)."""

    def _run(self, *, stdout='[]', returncode=0, exc=None):
        from unittest import mock
        proc = mock.Mock(returncode=returncode, stdout=stdout, stderr='')
        patcher = (
            mock.patch.object(h.subprocess, 'run', side_effect=exc) if exc
            else mock.patch.object(h.subprocess, 'run', return_value=proc)
        )
        with patcher:
            return h.head_has_passing_review_status(
                'Larry-Yatch/ourliberty-agent-core', '6de22a9058eb')

    def test_success_state_is_true(self):
        # jq returns the mirror-review context's states; a success → True.
        self.assertIs(self._run(stdout='["success"]'), True)

    def test_failure_state_only_is_false(self):
        # A REVISION/ESCALATE verdict posts a failure status → not a PASS.
        self.assertIs(self._run(stdout='["failure"]'), False)

    def test_no_matching_status_is_false(self):
        self.assertIs(self._run(stdout='[]'), False)

    def test_nonzero_exit_is_false(self):
        self.assertIs(self._run(returncode=1), False)

    def test_timeout_is_false(self):
        import subprocess as sp
        self.assertIs(self._run(exc=sp.TimeoutExpired(cmd='gh', timeout=1)), False)

    def test_garbage_stdout_is_false(self):
        self.assertIs(self._run(stdout='not json'), False)
        self.assertIs(self._run(stdout='{"a": 1}'), False)

    def test_context_constant_matches_notifier(self):
        # The probe filters on the exact context the notifier posts verdicts
        # under; if the notifier's constant drifts, this healer goes blind.
        import outbox_notifier as on
        self.assertEqual(
            h.MIRROR_REVIEW_STATUS_CONTEXT, on._MIRROR_REVIEW_STATUS_CONTEXT)


class TestRecentReviewRecord(unittest.TestCase):
    """The review-record recency scan over Mirror's inbox/.archive/.invalid."""

    TASK = 'completeness-pr3-build'

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name)
        (self.inbox / '.archive').mkdir()
        (self.inbox / '.invalid').mkdir()
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, relpath, *, age_minutes=0.0):
        import os
        p = self.inbox / relpath
        p.write_text('{}')
        ts = (self.now - timedelta(minutes=age_minutes)).timestamp()
        os.utime(p, (ts, ts))
        return p

    def _probe(self, stem=None):
        # recent_review_record takes the canonical review-record STEM
        # (`review-<task>`), matching what the writer produces on disk.
        return h.recent_review_record(
            stem or f'review-{self.TASK}', self.inbox, self.now)

    def test_empty_tree_is_none(self):
        self.assertIsNone(self._probe())

    def test_nonexistent_inbox_is_none(self):
        self.assertIsNone(h.recent_review_record(
            f'review-{self.TASK}', Path('/nonexistent-mirror-inbox'), self.now))

    def test_fresh_archived_base_record_blocks(self):
        self._write(f'.archive/review-{self.TASK}.json', age_minutes=5)
        self.assertIsNotNone(self._probe())

    def test_fresh_archived_rev_round_blocks(self):
        # THE #865 record shape the head-aware dedup never matched: the
        # revision-round file (which also recorded no head).
        self._write(f'.archive/review-{self.TASK}-rev1.json', age_minutes=2)
        self.assertIsNotNone(self._probe())

    def test_fresh_replan_round_blocks(self):
        self._write(f'.archive/review-{self.TASK}-replan2-rev1.json',
                    age_minutes=2)
        self.assertIsNotNone(self._probe())

    def test_fresh_uniquified_collision_blocks(self):
        self._write(f'.archive/review-{self.TASK}.2.json', age_minutes=2)
        self.assertIsNotNone(self._probe())

    def test_stale_archived_record_does_not_block(self):
        self._write(f'.archive/review-{self.TASK}-rev1.json',
                    age_minutes=h.RECENT_REVIEW_ACTIVITY_MINUTES + 30)
        self.assertIsNone(self._probe())

    def test_live_inbox_record_blocks_regardless_of_age(self):
        # A queued review is pending no matter how long it has queued.
        self._write(f'review-{self.TASK}-rev1.json',
                    age_minutes=h.RECENT_REVIEW_ACTIVITY_MINUTES * 10)
        self.assertIsNotNone(self._probe())

    def test_future_mtime_blocks(self):
        # Clock skew: a record "from the future" reads as recent (conservative).
        self._write(f'.archive/review-{self.TASK}.json', age_minutes=-10)
        self.assertIsNotNone(self._probe())

    def test_invalid_leg_scanned(self):
        self._write(f'.invalid/review-{self.TASK}.json', age_minutes=5)
        self.assertIsNotNone(self._probe())

    def test_sibling_task_prefix_does_not_block(self):
        # A DIFFERENT task whose id starts with this one must not hold the
        # backstop off this task's PR.
        self._write(f'.archive/review-{self.TASK}-extra-leg.json',
                    age_minutes=2)
        self.assertIsNone(self._probe())

    def test_rev_without_digits_does_not_block(self):
        # `-revamp` is a sibling task name, not a revision round.
        self._write(f'.archive/review-{self.TASK}-revamp.json', age_minutes=2)
        self.assertIsNone(self._probe())

    def test_lost_result_subdir_not_scanned(self):
        # .archive/.lost-result/ redispatch pacing belongs to the shared dedup
        # predicate (debounce + cap); this scan must not double-gate it.
        lost = self.inbox / '.archive' / '.lost-result'
        lost.mkdir()
        self._write(f'.archive/.lost-result/review-{self.TASK}.json',
                    age_minutes=2)
        self.assertIsNone(self._probe())

    def test_canonical_stem_matches_sanitized_on_disk_name(self):
        # A task_id with a path-structural byte is written under its SANITIZED
        # name (canonical_inbox_name turns `/` → `-`), so the recency scan must
        # be given the canonical stem — the exact name on disk — or it goes
        # blind while the cascade is mid-cycle (the #865 class for exotic ids).
        import safe_write_inbox
        raw_task = 'foo/bar'
        stem = safe_write_inbox.canonical_inbox_name(f'review-{raw_task}.json')
        stem = stem[:-len('.json')] if stem.endswith('.json') else stem
        self.assertEqual(stem, 'review-foo-bar')  # sanitized on write
        self._write(f'.archive/{stem}-rev1.json', age_minutes=2)
        # Probing with the canonical stem finds it; the raw stem would not.
        self.assertIsNotNone(self._probe(stem=stem))
        self.assertIsNone(self._probe(stem=f'review-{raw_task}'))


class TestPipelineBackoffReason(unittest.TestCase):
    """pipeline_backoff_reason: local checks (active review, recency) before the
    gh status probe; only a genuine orphan (no signal anywhere) returns None."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def _pr(self, *, head='abc123', repo='Larry-Yatch/ourliberty-agent-core'):
        return {'number': 865, 'task_id': 'completeness-pr3-build',
                '_repo': repo, 'headRefOid': head}

    def _run(self, pr, *, live=(False, ''), recent=None, passing=False):
        from unittest import mock
        with mock.patch.object(h.pipeline_live_state, 'pr_review_in_progress',
                               return_value=live) as live_p, \
                mock.patch.object(h, 'recent_review_record',
                                  return_value=recent) as rec_p, \
                mock.patch.object(h, 'head_has_passing_review_status',
                                  return_value=passing) as pass_p:
            result = h.pipeline_backoff_reason(
                pr, self.inbox, 'review-completeness-pr3-build', self.now)
        return result, live_p, rec_p, pass_p

    def test_active_review_backs_off_without_touching_recency_or_gh(self):
        r, live_p, rec_p, pass_p = self._run(
            self._pr(), live=(True, 'live_pid'))
        self.assertIn('active review', r)
        rec_p.assert_not_called()   # short-circuits before local recency
        pass_p.assert_not_called()  # and before the gh probe

    def test_recent_record_backs_off_without_touching_gh(self):
        r, live_p, rec_p, pass_p = self._run(
            self._pr(), recent='.archive/review-...-rev1.json written 2m ago')
        self.assertIn('recent review record', r)
        pass_p.assert_not_called()  # local recency short-circuits the gh probe

    def test_passing_status_backs_off(self):
        r, *_ = self._run(self._pr(), passing=True)
        self.assertIn('passing mirror-review status', r)

    def test_no_signal_is_orphan(self):
        r, *_ = self._run(self._pr(), passing=False)
        self.assertIsNone(r)

    def test_missing_repo_skips_gh_probe(self):
        pr = self._pr(repo=None)
        r, live_p, rec_p, pass_p = self._run(pr, passing=True)
        pass_p.assert_not_called()  # no repo → never guess one for the probe
        self.assertIsNone(r)

    def test_missing_head_skips_gh_probe(self):
        pr = self._pr(head=None)
        r, live_p, rec_p, pass_p = self._run(pr, passing=True)
        pass_p.assert_not_called()
        self.assertIsNone(r)


if __name__ == '__main__':
    unittest.main()
