#!/usr/bin/env python3
"""Tests for scripts/gh_pr_snapshot_refresher.py — the SINGLE gh-list caller.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage (gh-api-burn phase 2):
  - fetch_repo_prs tries rich fields, falls back to core fields, returns None
    only when both fail (the carry-forward signal).
  - build_snapshot writes FRESH entries for successful fetches and CARRIES the
    prior entry (old as_of) forward on a failed fetch — never blanking a repo.
  - refresh honors the phase-1 budget backoff (skip + no write), writes nothing
    on a total failure (prior snapshot left intact), and atomic-writes on any
    partial/full success.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_gh_pr_snapshot_refresher
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
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
import gh_pr_snapshot_refresher as ref  # noqa: E402

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _pr(number, state='OPEN'):
    return {'number': number, 'state': state, 'title': '', 'headRefName': '',
            'url': f'https://x/pull/{number}'}


def _tmp_snapshot_path():
    d = tempfile.mkdtemp(prefix='ourliberty-test-refresher-')
    return Path(d) / 'gh-open-pr-snapshot.json'


class FetchRepoPrsTest(unittest.TestCase):
    def test_primary_success_no_core_fallback(self):
        calls = []

        def fake_gh_list(repo, fields, *, limit, timeout):
            calls.append(fields)
            return [_pr(1)]

        with mock.patch.object(ref, '_gh_list', side_effect=fake_gh_list):
            out = ref.fetch_repo_prs('owner/repo')
        self.assertEqual([p['number'] for p in out], [1])
        self.assertEqual(calls, [ref.PRIMARY_FIELDS])  # core never attempted

    def test_falls_back_to_core_when_primary_fails(self):
        calls = []

        def fake_gh_list(repo, fields, *, limit, timeout):
            calls.append(fields)
            return None if fields == ref.PRIMARY_FIELDS else [_pr(2)]

        with mock.patch.object(ref, '_gh_list', side_effect=fake_gh_list):
            out = ref.fetch_repo_prs('owner/repo')
        self.assertEqual([p['number'] for p in out], [2])
        self.assertEqual(calls, [ref.PRIMARY_FIELDS, ref.CORE_FIELDS])

    def test_none_when_both_fail(self):
        with mock.patch.object(ref, '_gh_list', return_value=None):
            self.assertIsNone(ref.fetch_repo_prs('owner/repo'))


class BuildSnapshotTest(unittest.TestCase):
    def test_success_writes_fresh_entry(self):
        now_iso = ref._now_iso(NOW)
        out = ref.build_snapshot(
            {'owner/a': [_pr(1)]}, prior=None, now_iso=now_iso)
        self.assertEqual(out['schema'], snap.SCHEMA)
        self.assertEqual(out['as_of'], now_iso)
        self.assertEqual(out['repos']['owner/a']['as_of'], now_iso)
        self.assertEqual([p['number'] for p in out['repos']['owner/a']['prs']], [1])

    def test_failure_carries_prior_entry_with_old_as_of(self):
        old_iso = '2026-07-11T11:00:00+00:00'
        prior = {
            'schema': snap.SCHEMA, 'as_of': old_iso,
            'repos': {'owner/a': {'as_of': old_iso, 'prs': [_pr(5)]}},
        }
        now_iso = ref._now_iso(NOW)
        out = ref.build_snapshot({'owner/a': None}, prior=prior, now_iso=now_iso)
        # Top-level as_of advances (attempt time) but the repo entry is carried
        # UNCHANGED — old as_of preserved so a reader sees it as stale.
        self.assertEqual(out['as_of'], now_iso)
        self.assertEqual(out['repos']['owner/a']['as_of'], old_iso)
        self.assertEqual([p['number'] for p in out['repos']['owner/a']['prs']], [5])

    def test_failure_with_no_prior_omits_repo(self):
        now_iso = ref._now_iso(NOW)
        out = ref.build_snapshot({'owner/a': None}, prior=None, now_iso=now_iso)
        self.assertNotIn('owner/a', out['repos'])

    def test_mixed_fresh_and_carried(self):
        old_iso = '2026-07-11T11:00:00+00:00'
        prior = {
            'schema': snap.SCHEMA, 'as_of': old_iso,
            'repos': {'owner/b': {'as_of': old_iso, 'prs': [_pr(9)]}},
        }
        now_iso = ref._now_iso(NOW)
        out = ref.build_snapshot(
            {'owner/a': [_pr(1)], 'owner/b': None}, prior=prior, now_iso=now_iso)
        self.assertEqual(out['repos']['owner/a']['as_of'], now_iso)
        self.assertEqual(out['repos']['owner/b']['as_of'], old_iso)


class RefreshTest(unittest.TestCase):
    def test_budget_low_skips_and_does_not_write(self):
        path = _tmp_snapshot_path()
        with mock.patch.object(ref.gh_budget, 'should_skip', return_value=True), \
                mock.patch.object(ref, 'fetch_repo_prs',
                                  side_effect=AssertionError('must not fetch')):
            n = ref.refresh(repos=['owner/a'], path=path, now=NOW)
        self.assertEqual(n, 0)
        self.assertFalse(path.exists())  # prior snapshot (absent) left untouched

    def test_total_failure_leaves_prior_intact(self):
        path = _tmp_snapshot_path()
        # Seed a prior snapshot; a total-fail refresh must not overwrite it.
        prior_bytes = json.dumps({'schema': snap.SCHEMA, 'as_of': 'old',
                                  'repos': {'owner/a': {'as_of': 'old',
                                                        'prs': [_pr(3)]}}})
        path.write_text(prior_bytes, encoding='utf-8')
        with mock.patch.object(ref.gh_budget, 'should_skip', return_value=False), \
                mock.patch.object(ref, 'fetch_repo_prs', return_value=None):
            n = ref.refresh(repos=['owner/a'], path=path, now=NOW)
        self.assertEqual(n, 0)
        self.assertEqual(path.read_text(encoding='utf-8'), prior_bytes)

    def test_success_writes_atomic_snapshot(self):
        path = _tmp_snapshot_path()
        with mock.patch.object(ref.gh_budget, 'should_skip', return_value=False), \
                mock.patch.object(ref, 'fetch_repo_prs', return_value=[_pr(1)]):
            n = ref.refresh(repos=['owner/a'], path=path, now=NOW)
        self.assertEqual(n, 1)
        written = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(written['schema'], snap.SCHEMA)
        self.assertEqual(
            [p['number'] for p in written['repos']['owner/a']['prs']], [1])

    def test_partial_success_writes_fresh_plus_carried(self):
        path = _tmp_snapshot_path()
        old_iso = '2026-07-11T11:00:00+00:00'
        path.write_text(json.dumps({
            'schema': snap.SCHEMA, 'as_of': old_iso,
            'repos': {'owner/b': {'as_of': old_iso, 'prs': [_pr(9)]}},
        }), encoding='utf-8')

        def fetch(repo, *, limit, timeout):
            return [_pr(1)] if repo == 'owner/a' else None

        with mock.patch.object(ref.gh_budget, 'should_skip', return_value=False), \
                mock.patch.object(ref, 'fetch_repo_prs', side_effect=fetch):
            n = ref.refresh(repos=['owner/a', 'owner/b'], path=path, now=NOW)
        self.assertEqual(n, 1)
        written = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(written['repos']['owner/a']['as_of'], ref._now_iso(NOW))
        self.assertEqual(written['repos']['owner/b']['as_of'], old_iso)

    def test_written_snapshot_is_readable_by_the_reader(self):
        # End-to-end contract: what the refresher writes, the reader reads fresh.
        path = _tmp_snapshot_path()
        with mock.patch.object(ref.gh_budget, 'should_skip', return_value=False), \
                mock.patch.object(ref, 'fetch_repo_prs',
                                  return_value=[_pr(1, 'OPEN'), _pr(2, 'MERGED')]):
            ref.refresh(repos=['owner/a'], path=path, now=NOW)
        loaded = snap.load_snapshot(path)
        out = snap.all_prs('owner/a', snapshot=loaded, now=NOW.timestamp())
        self.assertEqual([p['number'] for p in out], [1, 2])
        open_only = snap.open_prs('owner/a', snapshot=loaded, now=NOW.timestamp())
        self.assertEqual([p['number'] for p in open_only], [1])


class MainExitCodeTest(unittest.TestCase):
    def test_main_always_returns_zero(self):
        with mock.patch.object(ref, 'refresh', return_value=0):
            self.assertEqual(ref.main(), 0)
        with mock.patch.object(ref, 'refresh', return_value=3):
            self.assertEqual(ref.main(), 0)


if __name__ == '__main__':
    unittest.main()
