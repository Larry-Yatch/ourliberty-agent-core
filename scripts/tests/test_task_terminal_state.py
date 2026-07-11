#!/usr/bin/env python3
"""Tests for scripts/task_terminal_state.py — the shared terminal-state probe.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage (terminal-state-reconciliation spec § 2 + § 6):
  - task_terminal_state returns each of the 4 states: MERGED / CLOSED / OPEN /
    UNKNOWN, the last on both no-match and gh-failure.
  - Variant matching: a PR found under a re-dispatch variant id (`<id>-001`,
    `fix-<id>-revisions-*`, `<id>-redispatch-*`) or a caller-supplied variant
    still resolves to the base task's state.
  - Conservative posture (spec § 1): an OPEN PR for the id wins over a terminal
    one (KEEP, never falsely retire); a too-short / boundary-non-match id yields
    UNKNOWN rather than a false terminal verdict.
  - The shared kernel: gh_json returns None (the UNKNOWN signal) on every `gh`
    failure mode; classify_state / expand_variants behave as the probes expect.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_task_terminal_state
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import task_terminal_state as tts  # noqa: E402

REPO = ['owner/repo']
TASK = 'tsr-demo-task-alpha'  # comfortably above MATCH_MIN_LEN


class _CompletedProc:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


def _pr(state, *, title='', branch='', number=1):
    return {
        'number': number, 'state': state, 'title': title,
        'headRefName': branch, 'url': f'https://x/pull/{number}',
    }


def _state_with(prs):
    """Run task_terminal_state against a single repo with the shared-snapshot
    read (`_snapshot_all_prs`) stubbed to return `prs`. gh-api-burn phase 2: the
    kernel reads the cached snapshot instead of shelling `gh pr list`, so tests
    inject at that seam."""
    with mock.patch.object(tts, '_snapshot_all_prs', return_value=prs):
        return tts.task_terminal_state(TASK, repos=REPO)


# -------------------- the four states --------------------

class FourStatesTest(unittest.TestCase):
    def test_merged(self):
        prs = [_pr('MERGED', branch=f'forge/{TASK}', title=f'feat: {TASK}')]
        self.assertEqual(_state_with(prs), tts.MERGED)

    def test_closed(self):
        prs = [_pr('CLOSED', branch=f'forge/{TASK}')]
        self.assertEqual(_state_with(prs), tts.CLOSED)

    def test_open(self):
        prs = [_pr('OPEN', branch=f'forge/{TASK}')]
        self.assertEqual(_state_with(prs), tts.OPEN)

    def test_unknown_on_no_match(self):
        # gh queried OK, but no PR carries this task_id -> indeterminate -> KEEP.
        prs = [_pr('MERGED', branch='forge/some-other-task', title='unrelated')]
        self.assertEqual(_state_with(prs), tts.UNKNOWN)

    def test_unknown_on_gh_failure(self):
        # The snapshot read yields [] for every repo (missing/stale snapshot,
        # failed fallback) -> no matches -> indeterminate -> KEEP.
        with mock.patch.object(tts, '_snapshot_all_prs', return_value=[]):
            self.assertEqual(
                tts.task_terminal_state(TASK, repos=REPO), tts.UNKNOWN)

    def test_unknown_on_empty_task_id(self):
        self.assertEqual(tts.task_terminal_state('', repos=REPO), tts.UNKNOWN)

    def test_unknown_on_unrecognized_state(self):
        prs = [_pr('DRAFT', branch=f'forge/{TASK}')]
        self.assertEqual(_state_with(prs), tts.UNKNOWN)


# -------------------- variant matching --------------------

class VariantMatchTest(unittest.TestCase):
    def test_matches_numbered_redispatch_branch(self):
        # Step merged under the `<id>-001` re-dispatch shape (spec § 3.4 case).
        prs = [_pr('MERGED', branch=f'forge/{TASK}-001')]
        self.assertEqual(_state_with(prs), tts.MERGED)

    def test_matches_fix_revisions_title(self):
        # `fix-<id>-revisions-*` carried in the PR title.
        prs = [_pr('MERGED', title=f'fix-{TASK}-revisions-2 cleanup', branch='b')]
        self.assertEqual(_state_with(prs), tts.MERGED)

    def test_matches_redispatch_suffix(self):
        prs = [_pr('CLOSED', branch=f'{TASK}-redispatch-003')]
        self.assertEqual(_state_with(prs), tts.CLOSED)

    def test_matches_caller_supplied_variant(self):
        prs = [_pr('MERGED', branch='forge/totally-different-slug')]
        with mock.patch.object(tts, '_snapshot_all_prs', return_value=prs):
            state = tts.task_terminal_state(
                TASK, variants=['totally-different-slug'], repos=REPO)
        self.assertEqual(state, tts.MERGED)


# -------------------- conservative posture (spec § 1) --------------------

class ConservativePostureTest(unittest.TestCase):
    def test_open_variant_beats_merged_original(self):
        # Original merged but a variant PR is still open -> work is still live
        # under the variant -> OPEN -> KEEP (never falsely retire).
        prs = [
            _pr('MERGED', branch=f'forge/{TASK}', number=1),
            _pr('OPEN', branch=f'forge/{TASK}-001', number=2),
        ]
        self.assertEqual(_state_with(prs), tts.OPEN)

    def test_merged_beats_closed(self):
        prs = [
            _pr('CLOSED', branch=f'forge/{TASK}', number=1),
            _pr('MERGED', branch=f'forge/{TASK}-002', number=2),
        ]
        self.assertEqual(_state_with(prs), tts.MERGED)

    def test_short_id_does_not_false_match(self):
        # An id below MATCH_MIN_LEN must not produce a terminal verdict off a
        # coincidental substring -> UNKNOWN (KEEP).
        prs = [_pr('MERGED', title='feat(api): unrelated', branch='forge/api')]
        with mock.patch.object(tts, '_snapshot_all_prs', return_value=prs):
            self.assertEqual(
                tts.task_terminal_state('api', repos=REPO), tts.UNKNOWN)

    def test_boundary_non_match(self):
        # task_id must match as a whole token, not a substring of a longer slug.
        prs = [_pr('MERGED', branch=f'forge/{TASK}extended')]
        self.assertEqual(_state_with(prs), tts.UNKNOWN)

    def test_one_repo_failure_does_not_mask_terminal_in_another(self):
        # First repo's snapshot read yields [] (missing entry), second returns the
        # merged PR. The snapshot reader degrades to [] (not None) on failure.
        good = [_pr('MERGED', branch=f'forge/{TASK}')]
        with mock.patch.object(tts, '_snapshot_all_prs', side_effect=[[], good]):
            state = tts.task_terminal_state(
                TASK, repos=['owner/a', 'owner/b'])
        self.assertEqual(state, tts.MERGED)


# -------------------- shared kernel: gh_json (UNKNOWN-on-error) --------------------

class GhJsonKernelTest(unittest.TestCase):
    def test_parses_json_on_success(self):
        with mock.patch.object(
                tts.subprocess, 'run',
                return_value=_CompletedProc(0, '{"state": "MERGED"}')):
            self.assertEqual(tts.gh_json(['gh', 'x']), {'state': 'MERGED'})

    def test_none_on_nonzero_exit(self):
        with mock.patch.object(
                tts.subprocess, 'run', return_value=_CompletedProc(1, '')):
            self.assertIsNone(tts.gh_json(['gh', 'x']))

    def test_none_on_timeout(self):
        with mock.patch.object(
                tts.subprocess, 'run',
                side_effect=tts.subprocess.TimeoutExpired(cmd='gh', timeout=1)):
            self.assertIsNone(tts.gh_json(['gh', 'x']))

    def test_none_on_gh_missing(self):
        with mock.patch.object(
                tts.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertIsNone(tts.gh_json(['gh', 'x']))

    def test_none_on_bad_json(self):
        with mock.patch.object(
                tts.subprocess, 'run',
                return_value=_CompletedProc(0, 'not json')):
            self.assertIsNone(tts.gh_json(['gh', 'x']))


# -------------------- shared kernel: classify_state + expand_variants --------------------

class ClassifyStateTest(unittest.TestCase):
    def test_known_states(self):
        self.assertEqual(tts.classify_state('MERGED'), tts.MERGED)
        self.assertEqual(tts.classify_state('closed'), tts.CLOSED)
        self.assertEqual(tts.classify_state(' open '), tts.OPEN)

    def test_unknown_states(self):
        self.assertEqual(tts.classify_state('DRAFT'), tts.UNKNOWN)
        self.assertEqual(tts.classify_state(None), tts.UNKNOWN)
        self.assertEqual(tts.classify_state(42), tts.UNKNOWN)


class ExpandVariantsTest(unittest.TestCase):
    def test_includes_standard_shapes(self):
        out = tts.expand_variants('foo-task')
        self.assertIn('foo-task', out)
        self.assertIn('foo-task-001', out)
        self.assertIn('foo-task-002', out)
        self.assertIn('fix-foo-task-revisions', out)
        self.assertIn('foo-task-redispatch', out)

    def test_includes_and_dedups_caller_variants(self):
        out = tts.expand_variants('foo-task', ['extra', 'foo-task'])
        self.assertIn('extra', out)
        self.assertEqual(out.count('foo-task'), 1)

    def test_strips_known_wrapper_prefixes(self):
        # The prefix-blindness fix: a wrapped id yields the bare stem so the
        # underlying work's PR (which carries only the stem) is matchable.
        self.assertIn('p3a-retro-prep',
                      tts.expand_variants('mirror-review-p3a-retro-prep'))
        self.assertIn('the-stem', tts.expand_variants('heal-the-stem'))
        self.assertIn('the-stem', tts.expand_variants('fix-the-stem'))
        # original id is always kept too
        self.assertIn('mirror-review-p3a-retro-prep',
                      tts.expand_variants('mirror-review-p3a-retro-prep'))

    def test_unprefixed_id_gets_no_bare_stem(self):
        self.assertNotIn('task-id', tts.expand_variants('plain-task-id'))

    def test_bare_prefix_does_not_add_empty_candidate(self):
        self.assertNotIn('', tts.expand_variants('heal-'))


class WrapperPrefixMatchTest(unittest.TestCase):
    """End-to-end: the live #747 shape — a `mirror-review-<stem>` approval id
    must classify MERGED off the PR that carries only `<stem>` on its branch."""

    def test_mirror_review_id_matches_merged_pr_by_stem(self):
        prs = [_pr('MERGED', branch='forge/p3a-retro-prep',
                   title='feat: P3a retrospective Stage A')]
        with mock.patch.object(tts, '_snapshot_all_prs', return_value=prs):
            state = tts.task_terminal_state(
                'mirror-review-p3a-retro-prep', repos=['x'])
        self.assertEqual(state, tts.MERGED)


class DefaultReposTest(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict(
                tts.os.environ,
                {'OURLIBERTY_TERMINAL_STATE_REPOS': 'a/one, b/two'}):
            self.assertEqual(tts.default_repos(), ['a/one', 'b/two'])

    def test_default_when_unset(self):
        with mock.patch.dict(tts.os.environ, {}, clear=False):
            tts.os.environ.pop('OURLIBERTY_TERMINAL_STATE_REPOS', None)
            self.assertEqual(tts.default_repos(), list(tts.DEFAULT_REPOS))

    def test_qualify_bare_repo(self):
        with mock.patch.dict(tts.os.environ, {'OURLIBERTY_GH_OWNER': 'Acme'}):
            self.assertEqual(tts._qualify_repo('core'), 'Acme/core')
        self.assertEqual(tts._qualify_repo('owner/core'), 'owner/core')


# -------------------- direct PR-coordinate probe --------------------

COORD_REPOS = ['owner/agent-core']
COORD_ID = 'pr-agent-core-845'
WRAPPED_COORD_ID = 'mirror-review-pr-agent-core-845'
# The head8-suffixed shape outbox_notifier mints when a head SHA is present:
# `mirror-review-<task_id>-<head8>` (head8 = (head_sha or '')[:8], hex).
HEAD8_WRAPPED_ID = 'mirror-review-pr-agent-core-845-a1b2c3d4'


class ParsePrCoordinateTest(unittest.TestCase):
    def test_bare_coordinate(self):
        self.assertEqual(tts.parse_pr_coordinate(COORD_ID, COORD_REPOS),
                         ('owner/agent-core', 845))

    def test_wrapped_coordinate(self):
        # The 2026-07-08 phantom shape: mirror-review-pr-<repo>-<num>.
        self.assertEqual(tts.parse_pr_coordinate(WRAPPED_COORD_ID, COORD_REPOS),
                         ('owner/agent-core', 845))

    def test_head8_suffixed_wrapped_coordinate(self):
        # The common shape when head_sha is present — a hex tail an all-digits
        # anchor would silently miss, re-opening the phantom class.
        self.assertEqual(tts.parse_pr_coordinate(HEAD8_WRAPPED_ID, COORD_REPOS),
                         ('owner/agent-core', 845))

    def test_all_digit_head8_not_mis_read_as_pr_number(self):
        # Anchoring on the known repo short name (`agent-core`) resolves the
        # greedy ambiguity: the number right after the repo is the PR (#845),
        # the trailing all-digit group is the head8, not the PR number.
        self.assertEqual(
            tts.parse_pr_coordinate('pr-agent-core-845-00001234', COORD_REPOS),
            ('owner/agent-core', 845))

    def test_multi_segment_repo_short_name(self):
        # A real short name has internal dashes; the anchor must match it whole.
        repos = ['Larry-Yatch/ourliberty-agent-core']
        self.assertEqual(
            tts.parse_pr_coordinate(
                'mirror-review-pr-ourliberty-agent-core-857', repos),
            ('Larry-Yatch/ourliberty-agent-core', 857))

    def test_unknown_repo_is_none(self):
        self.assertIsNone(tts.parse_pr_coordinate('pr-other-repo-845', COORD_REPOS))

    def test_short_nonhead_tail_is_none_not_wrong_pr(self):
        # `pr-agent-core-845-001`: a real head8 is 8 hex chars, so a 3-char `001`
        # tail does not match the optional suffix and the whole remainder fails
        # to parse -> None (never mis-read as PR #1). Conservative: don't guess.
        self.assertIsNone(
            tts.parse_pr_coordinate('pr-agent-core-845-001', COORD_REPOS))

    def test_non_coordinate_is_none(self):
        self.assertIsNone(tts.parse_pr_coordinate(TASK, COORD_REPOS))
        self.assertIsNone(tts.parse_pr_coordinate('', COORD_REPOS))
        self.assertIsNone(tts.parse_pr_coordinate(None, COORD_REPOS))


class PrCoordinateStateTest(unittest.TestCase):
    """pr_coordinate_state takes an already-parsed (repo, number)."""

    def test_merged_with_sha(self):
        data = {'state': 'MERGED', 'mergeCommit': {'oid': 'abc123def456'}}
        with mock.patch.object(tts, 'gh_json', return_value=data) as gj:
            state, sha = tts.pr_coordinate_state('owner/agent-core', 845)
        self.assertEqual(state, tts.MERGED)
        self.assertEqual(sha, 'abc123def456')
        # The lookup names the PR by number against the qualified repo.
        args = gj.call_args[0][0]
        self.assertIn('845', args)
        self.assertIn('owner/agent-core', args)

    def test_closed_without_merge_commit(self):
        data = {'state': 'CLOSED', 'mergeCommit': None}
        with mock.patch.object(tts, 'gh_json', return_value=data):
            self.assertEqual(tts.pr_coordinate_state('owner/agent-core', 845),
                             (tts.CLOSED, None))

    def test_open(self):
        with mock.patch.object(tts, 'gh_json', return_value={'state': 'OPEN'}):
            self.assertEqual(tts.pr_coordinate_state('owner/agent-core', 845),
                             (tts.OPEN, None))

    def test_gh_failure_is_unknown(self):
        with mock.patch.object(tts, 'gh_json', return_value=None):
            self.assertEqual(tts.pr_coordinate_state('owner/agent-core', 845),
                             (tts.UNKNOWN, None))


class CoordinateNotWiredIntoCanonicalProbeTest(unittest.TestCase):
    """pr_coordinate_state is a DELIBERATELY SEPARATE primitive: it is NOT wired
    into task_terminal_state, whose token search answers the different "is the
    task live under any branch/variant?" question. This guards against a future
    edit re-introducing the divergence (direct-PR verdict vs OPEN-variant-wins
    _combine) that a merged probe would create."""

    def test_task_terminal_state_does_not_resolve_a_pure_coordinate(self):
        # A coordinate id with a MERGED PR but no token-matching branch/title:
        # task_terminal_state must stay UNKNOWN (it never calls pr view), proving
        # the coordinate leg is not folded in.
        def fake_gh(args, timeout=None):
            if 'view' in args:
                raise AssertionError('task_terminal_state must not gh-pr-view')
            return []
        # Snapshot has no token-matching PR for this coordinate id; and if the
        # coordinate leg were (wrongly) wired in it would gh-pr-view -> the guard
        # above fires. It must stay UNKNOWN via the token search alone.
        with mock.patch.object(tts, '_snapshot_all_prs', return_value=[]), \
                mock.patch.object(tts, 'gh_json', side_effect=fake_gh):
            self.assertEqual(
                tts.task_terminal_state(WRAPPED_COORD_ID, repos=COORD_REPOS),
                tts.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
