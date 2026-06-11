#!/usr/bin/env python3
"""Deterministic tests for the audit-due nudge — no git/file/network dependency.

Covers the pure threshold logic (evaluate) and the firing contract (fire once per audit
anchor; no-op when already nagged; re-arm when a newer audit lands; fail-open).
"""

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401
import os, sys, json, tempfile, pathlib, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import audit_due_nudge as nudge  # noqa: E402


class TestEvaluate(unittest.TestCase):
    def test_due_on_pr_threshold(self):
        v = nudge.evaluate({'audit': 'A.md', 'pr_count': nudge.PR_THRESHOLD, 'churn_loc': 0})
        self.assertTrue(v['due'])
        self.assertEqual(len(v['reasons']), 1)

    def test_due_on_loc_threshold(self):
        v = nudge.evaluate({'audit': 'A.md', 'pr_count': 0, 'churn_loc': nudge.LOC_THRESHOLD})
        self.assertTrue(v['due'])

    def test_not_due_below_both(self):
        v = nudge.evaluate({'audit': 'A.md', 'pr_count': nudge.PR_THRESHOLD - 1,
                            'churn_loc': nudge.LOC_THRESHOLD - 1})
        self.assertFalse(v['due'])
        self.assertEqual(v['reasons'], [])

    def test_both_reasons_when_both_cross(self):
        v = nudge.evaluate({'audit': 'A.md', 'pr_count': nudge.PR_THRESHOLD + 5,
                            'churn_loc': nudge.LOC_THRESHOLD + 5})
        self.assertTrue(v['due'])
        self.assertEqual(len(v['reasons']), 2)  # this is the path that previously NameError'd


class TestFiringContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sent = pathlib.Path(self.tmp) / 'state' / 'audit-due-nudge.json'
        self._orig_sent = nudge.SENTINEL
        nudge.SENTINEL = self.sent
        self.dms = []
        self._orig_dm = nudge._dm
        nudge._dm = lambda body: (self.dms.append(body), True)[1]
        self._orig_gs = nudge.gather_signals   # restored in tearDown (test isolation)

    def tearDown(self):
        nudge.SENTINEL = self._orig_sent
        nudge._dm = self._orig_dm
        nudge.gather_signals = self._orig_gs

    def _with_signals(self, sig):
        nudge.gather_signals = lambda: sig

    def test_fires_once_then_noops(self):
        self._with_signals({'audit': 'AUDIT_main_20260605.md', 'date': '2026-06-05',
                            'pr_count': 99, 'churn_loc': 99999})
        nudge.main()
        self.assertEqual(len(self.dms), 1, 'should DM on first due fire')
        self.assertTrue(self.sent.exists())
        nudge.main()
        self.assertEqual(len(self.dms), 1, 'must NOT re-DM for the same audit anchor')

    def test_rearms_on_newer_audit(self):
        self._with_signals({'audit': 'AUDIT_main_20260605.md', 'date': '2026-06-05',
                            'pr_count': 99, 'churn_loc': 99999})
        nudge.main()
        # a newer audit lands AND it's already over threshold -> re-arm + fire again
        self._with_signals({'audit': 'AUDIT_main_20260710.md', 'date': '2026-07-10',
                            'pr_count': 99, 'churn_loc': 99999})
        nudge.main()
        self.assertEqual(len(self.dms), 2, 'a newer audit anchor must re-arm the nudge')
        self.assertEqual(json.loads(self.sent.read_text())['audit'], 'AUDIT_main_20260710.md')

    def test_not_due_no_dm(self):
        self._with_signals({'audit': 'AUDIT_main_20260605.md', 'date': '2026-06-05',
                            'pr_count': 1, 'churn_loc': 1})
        nudge.main()
        self.assertEqual(self.dms, [])
        self.assertFalse(self.sent.exists())

    def test_no_audit_noop(self):
        self._with_signals(None)
        nudge.main()
        self.assertEqual(self.dms, [])


if __name__ == '__main__':
    unittest.main()
