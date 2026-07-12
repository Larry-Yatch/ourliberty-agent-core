#!/usr/bin/env python3
"""Delegate-tracking Slice 2b — the operator-queue surface (where Larry actually
delegates). `_project_delegation_fields` attaches `delegation_needs_you` (open
approval, via the #942 pending-store origin join) + `delegation_build_phase` /
`delegation_pr_url` (the review trail) to each ranked entry, keyed by the
deterministic origin id `delegate-<entry id>`.

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_operator_queue_delegation
"""

from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api as da  # noqa: E402

DELEGATE = 'delegate-m1'


class _StubTable:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        return type('R', (), {'data': self._rows})()


class _StubClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _StubTable(self._rows)


class ProjectDelegationFieldsTest(unittest.TestCase):
    def setUp(self):
        self._store = da._agents_root() / 'state' / 'beacon-pending-approvals.json'
        self._store.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: self._store.exists() and self._store.unlink())

    def _write_pending(self, pending, history=None):
        self._store.write_text(json.dumps(
            {'pending': pending, 'history': history or []}))

    def test_open_approval_and_build_trail(self):
        self._write_pending([{
            'id': 'appr1', 'origin_task_id': DELEGATE,
            'status': 'pending', 'created_at': '2026-07-11T12:00:00Z',
        }])
        rows = [{'event_type': 'review_pass',
                 'pr_url': 'https://github.com/o/r/pull/7',
                 'ts': '2026-07-11T12:00:00Z',
                 'payload': {'origin_task_id': DELEGATE}}]
        ranked = [{'id': 'm1', 'name': 'Do the thing'}]
        da._project_delegation_fields(ranked, _StubClient(rows))
        self.assertEqual(ranked[0]['delegation_needs_you']['approval_id'], 'appr1')
        self.assertEqual(ranked[0]['delegation_build_phase'], 'review_passed')
        self.assertEqual(ranked[0]['delegation_pr_url'],
                         'https://github.com/o/r/pull/7')

    def test_merged_pr_flips_review_passed_to_merged(self):
        self._write_pending([])
        pr = 'https://github.com/o/r/pull/7'
        rows = [{'event_type': 'review_pass', 'pr_url': pr, 'ts': 't',
                 'payload': {'origin_task_id': DELEGATE}}]
        ranked = [{'id': 'm1', 'name': 'X'}]
        da._project_delegation_fields(
            ranked, _StubClient(rows), lambda urls: {pr: 'MERGED'})
        self.assertEqual(ranked[0]['delegation_build_phase'], 'merged')

    def test_no_resolver_stays_review_passed(self):
        self._write_pending([])
        pr = 'https://github.com/o/r/pull/7'
        rows = [{'event_type': 'review_pass', 'pr_url': pr, 'ts': 't',
                 'payload': {'origin_task_id': DELEGATE}}]
        ranked = [{'id': 'm1', 'name': 'X'}]
        da._project_delegation_fields(ranked, _StubClient(rows))  # no resolver
        self.assertEqual(ranked[0]['delegation_build_phase'], 'review_passed')

    def test_open_pr_stays_review_passed(self):
        self._write_pending([])
        pr = 'https://github.com/o/r/pull/7'
        rows = [{'event_type': 'review_pass', 'pr_url': pr, 'ts': 't',
                 'payload': {'origin_task_id': DELEGATE}}]
        ranked = [{'id': 'm1', 'name': 'X'}]
        da._project_delegation_fields(
            ranked, _StubClient(rows), lambda urls: {pr: 'OPEN'})
        self.assertEqual(ranked[0]['delegation_build_phase'], 'review_passed')

    def test_in_review_without_open_approval(self):
        # approved (dispatched) → no needs-you, but the build trail shows
        self._write_pending([], history=[{
            'id': 'appr2', 'origin_task_id': DELEGATE, 'status': 'approved'}])
        rows = [{'event_type': 'review_request', 'ts': 't',
                 'payload': {'origin_task_id': DELEGATE}}]
        ranked = [{'id': 'm1', 'name': 'X'}]
        da._project_delegation_fields(ranked, _StubClient(rows))
        self.assertIsNone(ranked[0]['delegation_needs_you'])
        self.assertEqual(ranked[0]['delegation_build_phase'], 'in_review')

    def test_non_delegated_entry_is_neutral(self):
        self._write_pending([])
        ranked = [{'id': 'm2', 'name': 'never delegated'}]
        da._project_delegation_fields(ranked, _StubClient([]))
        self.assertIsNone(ranked[0]['delegation_needs_you'])
        self.assertIsNone(ranked[0]['delegation_build_phase'])
        self.assertIsNone(ranked[0]['delegation_pr_url'])

    def test_missing_id_is_safe(self):
        ranked = [{'name': 'no-id-row'}]
        da._project_delegation_fields(ranked, None)
        self.assertIsNone(ranked[0]['delegation_needs_you'])
        self.assertIsNone(ranked[0]['delegation_build_phase'])

    def test_empty_ranked_is_safe(self):
        ranked: list = []
        da._project_delegation_fields(ranked, None)  # must not raise
        self.assertEqual(ranked, [])


if __name__ == '__main__':
    unittest.main()
