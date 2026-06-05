"""Unit tests for scripts/triage_decisions._is_mock (PR-B, audit #36).

The mock-marker test must match a marker only as a whole leading component, not
as a bare substring, or a real task_id like 'unreal-deploy-001' or
'prod-failover-check' is misclassified MOCK and silently cleared from Larry's
queue under --apply.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import triage_decisions as td  # noqa: E402


class IsMockTest(unittest.TestCase):
    def test_marker_as_leading_component_is_mock(self):
        for tid in ('real-1', 'real-pf-3', 'prod-clr-9', 'prod-fail-001',
                    'task-cascade-x', 'real-deploy-001'):
            self.assertTrue(td._is_mock(tid), tid)

    def test_exact_marker_is_mock(self):
        # markers that do not end in a separator still match when the id is
        # exactly the marker
        self.assertTrue(td._is_mock('prod-fail'))
        self.assertTrue(td._is_mock('task-cascade'))

    def test_substring_marker_is_not_mock(self):
        # audit #36: these real ids merely CONTAIN a marker substring and must
        # NOT be classified mock
        for tid in ('unreal-deploy-001',      # contains 'real-' but not as prefix
                    'prod-failover-check',     # 'prod-fail' + 'over' — not a boundary
                    'surreal-task-cascade'):   # 'real-'/'task-cascade' mid-id
            self.assertFalse(td._is_mock(tid), tid)

    def test_empty_and_none(self):
        self.assertFalse(td._is_mock(''))
        self.assertFalse(td._is_mock(None))


if __name__ == '__main__':
    unittest.main()
