#!/usr/bin/env python3
"""Tests for gh_burn_sampler (gh-api-burn phase 1, Part C).

Covers:
  - build_sample flattens a gh_budget.remaining() dict into the burn-log record.
  - main appends one well-formed JSON line given a mocked rate_limit reading.
  - main writes NO line when the budget read is unavailable (nulls would pollute
    the analyzer).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_gh_burn_sampler
"""
from __future__ import annotations

try:
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

import gh_burn_sampler as sampler  # noqa: E402


_READING = {
    'graphql': {'remaining': 4200, 'limit': 5000, 'reset': 1_700_000_000},
    'rest': {'remaining': 4800, 'limit': 5000, 'reset': 1_700_000_000},
}


class BuildSampleTests(unittest.TestCase):
    def test_flattens_reading(self):
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        s = sampler.build_sample(_READING, now=now)
        self.assertEqual(s['ts'], now.isoformat())
        self.assertEqual(s['graphql_remaining'], 4200)
        self.assertEqual(s['graphql_limit'], 5000)
        self.assertEqual(s['graphql_reset'], 1_700_000_000)
        self.assertEqual(s['rest_remaining'], 4800)
        self.assertEqual(s['rest_limit'], 5000)

    def test_tolerates_missing_blocks(self):
        s = sampler.build_sample({})
        self.assertIsNone(s['graphql_remaining'])
        self.assertIsNone(s['rest_limit'])
        self.assertIn('ts', s)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='gh-burn-sampler-test-'))
        self.log = self.tmp / 'gh-api-burn.jsonl'

    def test_appends_one_well_formed_line(self):
        with mock.patch.object(sampler, 'BURN_LOG', self.log), \
                mock.patch.object(sampler.gh_budget, 'remaining',
                                  return_value=_READING):
            rc = sampler.main()
        self.assertEqual(rc, 0)
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec['graphql_remaining'], 4200)
        self.assertEqual(rec['graphql_limit'], 5000)

    def test_appends_across_runs(self):
        with mock.patch.object(sampler, 'BURN_LOG', self.log), \
                mock.patch.object(sampler.gh_budget, 'remaining',
                                  return_value=_READING):
            sampler.main()
            sampler.main()
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_writes_nothing_when_budget_unavailable(self):
        with mock.patch.object(sampler, 'BURN_LOG', self.log), \
                mock.patch.object(sampler.gh_budget, 'remaining',
                                  return_value={}):
            rc = sampler.main()
        self.assertEqual(rc, 0)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
