#!/usr/bin/env python3
"""Tests for compute_approval_verification (approvals-freshness slice 2, Part C).

The anti-false-clean tri-state a decision card carries so the Approvals tab can
never read as fully-checked while a large share of it is unprobed:

  premise_stale marker present            -> 'stale'      (+ evidence)
  no freshness_probe on the entry         -> 'unverified'
  freshness_probe present and not stale   -> 'verified'

The load-bearing property under test: an entry with NO freshness_probe computes
`verification == 'unverified'` (the common case today), never silently fresh.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_approval_verification
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dashboard_api as da  # noqa: E402

_PROBE = {'kind': 'file_contains', 'path': 'x.txt', 'needle': 'y'}


def _entry(**kw) -> dict:
    return kw


class ComputeApprovalVerificationTest(unittest.TestCase):
    def test_no_probe_is_unverified(self) -> None:
        # The common case today: no freshness_probe → machine-flagged unverified.
        out = da.compute_approval_verification(_entry(id='a1'))
        self.assertEqual(out, {'verification': 'unverified'})

    def test_empty_dispatch_payload_is_unverified(self) -> None:
        out = da.compute_approval_verification(
            _entry(id='a2', dispatch_payload={}))
        self.assertEqual(out, {'verification': 'unverified'})

    def test_probe_present_is_verified(self) -> None:
        out = da.compute_approval_verification(
            _entry(id='a3', dispatch_payload={'freshness_probe': _PROBE}))
        self.assertEqual(out, {'verification': 'verified'})

    def test_premise_stale_is_stale_with_evidence(self) -> None:
        entry = _entry(
            id='a4',
            dispatch_payload={'freshness_probe': _PROBE},
            premise_stale={'evidence': 'file no longer contains y',
                           'kind': 'file_contains',
                           'probed_at': '2026-06-02T12:00:00+00:00'},
        )
        out = da.compute_approval_verification(entry)
        self.assertEqual(out['verification'], 'stale')
        self.assertEqual(out['verification_evidence'], 'file no longer contains y')

    def test_stale_marker_wins_over_probe(self) -> None:
        # A stamped premise_stale marker takes precedence over the probe's mere
        # presence — a demoted card must never read 'verified'.
        entry = _entry(
            id='a5',
            dispatch_payload={'freshness_probe': _PROBE},
            premise_stale={'kind': 'file_contains'},  # no evidence string
        )
        out = da.compute_approval_verification(entry)
        self.assertEqual(out['verification'], 'stale')
        self.assertNotIn('verification_evidence', out)  # evidence optional

    def test_non_dict_entry_is_unverified(self) -> None:
        for bad in (None, 'nope', 42, ['x']):
            self.assertEqual(
                da.compute_approval_verification(bad),
                {'verification': 'unverified'})


if __name__ == '__main__':
    unittest.main()
