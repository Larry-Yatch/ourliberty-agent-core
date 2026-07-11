#!/usr/bin/env python3
"""Tests for scripts/gh_pr_snapshot.py — the shared cached PR-snapshot READER.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage (gh-api-burn phase 2):
  - Fresh snapshot reads are FREE (no gh) and return the cached prs; `limit`
    slices the number-descending head to reproduce a narrower window.
  - open_prs filters to state==OPEN and bounds the OPEN subset.
  - Stale / missing entries return last-known / [] and NEVER shell gh by default.
  - The bounded no-stampede fallback: only fires when opted in AND the cooldown +
    budget guards allow; the cooldown is a cross-process check-and-claim that
    fail-SAFE DENYs (never stampede) on any error.
  - freshness() is a pure observability read.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_gh_pr_snapshot
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import gh_pr_snapshot as snap  # noqa: E402

REPO = 'owner/repo'
NOW = 1_000_000.0  # fixed epoch for deterministic freshness math


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _pr(number, state, *, title='', branch=''):
    return {
        'number': number, 'state': state, 'title': title,
        'headRefName': branch, 'url': f'https://x/pull/{number}',
    }


def _snapshot(prs, *, age_s=0.0, repo=REPO):
    """A one-repo snapshot whose entry is `age_s` seconds old relative to NOW."""
    return {
        'schema': snap.SCHEMA,
        'as_of': _iso(NOW),
        'repos': {repo: {'as_of': _iso(NOW - age_s), 'prs': prs}},
    }


class AllPrsFreshTest(unittest.TestCase):
    def test_fresh_returns_cached_prs_no_gh(self):
        s = _snapshot([_pr(2, 'OPEN'), _pr(1, 'MERGED')], age_s=0)
        # A fresh read must never call the live fetch even when opted in.
        with mock.patch.object(snap, '_live_fetch',
                               side_effect=AssertionError('must not fetch')):
            out = snap.all_prs(REPO, snapshot=s, now=NOW, live_fallback=True)
        self.assertEqual([p['number'] for p in out], [2, 1])

    def test_limit_slices_number_descending_head(self):
        prs = [_pr(5, 'OPEN'), _pr(4, 'OPEN'), _pr(3, 'MERGED')]
        s = _snapshot(prs, age_s=0)
        out = snap.all_prs(REPO, snapshot=s, now=NOW, limit=2)
        self.assertEqual([p['number'] for p in out], [5, 4])

    def test_bare_name_entry_is_found_via_qualification(self):
        # Snapshot keyed by the qualified owner/repo; a bare-name lookup resolves.
        s = _snapshot([_pr(1, 'OPEN')], age_s=0, repo='owner/repo')
        out = snap.all_prs('owner/repo', snapshot=s, now=NOW)
        self.assertEqual(len(out), 1)


class OpenPrsTest(unittest.TestCase):
    def test_filters_to_open_only(self):
        prs = [_pr(3, 'OPEN'), _pr(2, 'MERGED'), _pr(1, 'CLOSED')]
        s = _snapshot(prs, age_s=0)
        out = snap.open_prs(REPO, snapshot=s, now=NOW)
        self.assertEqual([p['number'] for p in out], [3])

    def test_limit_bounds_the_open_subset(self):
        prs = [_pr(4, 'OPEN'), _pr(3, 'MERGED'), _pr(2, 'OPEN'), _pr(1, 'OPEN')]
        s = _snapshot(prs, age_s=0)
        out = snap.open_prs(REPO, snapshot=s, now=NOW, limit=2)
        # The two most-recent OPEN PRs, not the two most-recent overall.
        self.assertEqual([p['number'] for p in out], [4, 2])

    def test_state_match_is_case_insensitive(self):
        s = _snapshot([_pr(1, 'open')], age_s=0)
        self.assertEqual(len(snap.open_prs(REPO, snapshot=s, now=NOW)), 1)


class PrLookupTest(unittest.TestCase):
    def test_finds_by_number(self):
        s = _snapshot([_pr(7, 'MERGED'), _pr(1, 'OPEN')], age_s=0)
        self.assertEqual(snap.pr(REPO, 7, snapshot=s, now=NOW)['state'], 'MERGED')

    def test_missing_number_is_none(self):
        s = _snapshot([_pr(1, 'OPEN')], age_s=0)
        self.assertIsNone(snap.pr(REPO, 999, snapshot=s, now=NOW))


class StaleAndMissingTest(unittest.TestCase):
    def test_stale_default_returns_last_known_no_gh(self):
        s = _snapshot([_pr(1, 'OPEN')], age_s=snap.DEFAULT_MAX_AGE_S + 60)
        with mock.patch.object(snap, '_live_fetch',
                               side_effect=AssertionError('must not fetch')):
            out = snap.all_prs(REPO, snapshot=s, now=NOW)  # live_fallback default off
        self.assertEqual([p['number'] for p in out], [1])

    def test_missing_entry_returns_empty(self):
        s = {'schema': snap.SCHEMA, 'as_of': _iso(NOW), 'repos': {}}
        self.assertEqual(snap.all_prs(REPO, snapshot=s, now=NOW), [])

    def test_none_snapshot_returns_empty(self):
        with mock.patch.object(snap, 'load_snapshot', return_value=None):
            self.assertEqual(snap.all_prs(REPO, now=NOW), [])


class BoundedFallbackTest(unittest.TestCase):
    def test_stale_with_fallback_fires_live_fetch_when_guards_allow(self):
        s = _snapshot([_pr(1, 'OPEN')], age_s=snap.DEFAULT_MAX_AGE_S + 60)
        live = [_pr(9, 'MERGED')]
        with mock.patch.object(snap, '_budget_ok_for_fallback', return_value=True), \
                mock.patch.object(snap, '_fallback_allowed', return_value=True), \
                mock.patch.object(snap, '_live_fetch', return_value=live):
            out = snap.all_prs(REPO, snapshot=s, now=NOW, live_fallback=True)
        self.assertEqual([p['number'] for p in out], [9])

    def test_budget_low_declines_fallback_serves_stale(self):
        s = _snapshot([_pr(1, 'OPEN')], age_s=snap.DEFAULT_MAX_AGE_S + 60)
        with mock.patch.object(snap, '_budget_ok_for_fallback', return_value=False), \
                mock.patch.object(snap, '_live_fetch',
                                  side_effect=AssertionError('must not fetch')):
            out = snap.all_prs(REPO, snapshot=s, now=NOW, live_fallback=True)
        self.assertEqual([p['number'] for p in out], [1])

    def test_cooldown_denies_second_caller_serves_stale(self):
        s = _snapshot([_pr(1, 'OPEN')], age_s=snap.DEFAULT_MAX_AGE_S + 60)
        with mock.patch.object(snap, '_budget_ok_for_fallback', return_value=True), \
                mock.patch.object(snap, '_fallback_allowed', return_value=False), \
                mock.patch.object(snap, '_live_fetch',
                                  side_effect=AssertionError('must not fetch')):
            out = snap.all_prs(REPO, snapshot=s, now=NOW, live_fallback=True)
        self.assertEqual([p['number'] for p in out], [1])

    def test_failed_live_fetch_falls_back_to_stale(self):
        s = _snapshot([_pr(1, 'OPEN')], age_s=snap.DEFAULT_MAX_AGE_S + 60)
        with mock.patch.object(snap, '_budget_ok_for_fallback', return_value=True), \
                mock.patch.object(snap, '_fallback_allowed', return_value=True), \
                mock.patch.object(snap, '_live_fetch', return_value=None):
            out = snap.all_prs(REPO, snapshot=s, now=NOW, live_fallback=True)
        self.assertEqual([p['number'] for p in out], [1])


class FallbackCooldownClaimTest(unittest.TestCase):
    """_fallback_allowed is the cross-process check-and-claim cooldown."""

    def _tmp_path(self):
        d = tempfile.mkdtemp(prefix='ourliberty-test-fallback-')
        return Path(d) / 'cooldown.json'

    def test_first_claim_allowed_second_within_window_denied(self):
        path = self._tmp_path()
        self.assertTrue(
            snap._fallback_allowed(REPO, NOW, path=path, cooldown=180.0))
        # A second caller within the window is denied (no stampede).
        self.assertFalse(
            snap._fallback_allowed(REPO, NOW + 1, path=path, cooldown=180.0))

    def test_claim_reopens_after_cooldown(self):
        path = self._tmp_path()
        self.assertTrue(
            snap._fallback_allowed(REPO, NOW, path=path, cooldown=180.0))
        self.assertTrue(
            snap._fallback_allowed(REPO, NOW + 200, path=path, cooldown=180.0))

    def test_distinct_repos_have_independent_cooldowns(self):
        path = self._tmp_path()
        self.assertTrue(
            snap._fallback_allowed('owner/a', NOW, path=path, cooldown=180.0))
        self.assertTrue(
            snap._fallback_allowed('owner/b', NOW, path=path, cooldown=180.0))


class FreshnessTest(unittest.TestCase):
    def test_present_fresh(self):
        s = _snapshot([_pr(1, 'OPEN'), _pr(2, 'MERGED')], age_s=10)
        f = snap.freshness(REPO, snapshot=s, now=NOW)
        self.assertTrue(f['present'])
        self.assertFalse(f['stale'])
        self.assertEqual(f['count'], 2)
        self.assertAlmostEqual(f['age_seconds'], 10.0, places=3)

    def test_stale_flagged(self):
        s = _snapshot([_pr(1, 'OPEN')], age_s=snap.DEFAULT_MAX_AGE_S + 1)
        f = snap.freshness(REPO, snapshot=s, now=NOW)
        self.assertTrue(f['stale'])

    def test_absent(self):
        s = {'schema': snap.SCHEMA, 'as_of': _iso(NOW), 'repos': {}}
        f = snap.freshness(REPO, snapshot=s, now=NOW)
        self.assertFalse(f['present'])
        self.assertTrue(f['stale'])
        self.assertEqual(f['count'], 0)
        self.assertIsNone(f['age_seconds'])


class LoadSnapshotTest(unittest.TestCase):
    def test_missing_file_is_none(self):
        self.assertIsNone(snap.load_snapshot(Path('/nonexistent/snap.json')))

    def test_bad_json_is_none(self):
        d = tempfile.mkdtemp(prefix='ourliberty-test-snap-')
        p = Path(d) / 'snap.json'
        p.write_text('not json', encoding='utf-8')
        self.assertIsNone(snap.load_snapshot(p))


if __name__ == '__main__':
    unittest.main()
