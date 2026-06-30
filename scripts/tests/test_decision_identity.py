#!/usr/bin/env python3
"""Tests for decision_identity.canonical_decision_key (Approval-Sync Phase 2,
Change A — spec docs/approval-sync-phase2-spec.md §1.1 / §3).

The whole point of the canonical key is that the SAME logical needs-Larry item,
authored into four stores under four different id shapes, collapses to ONE join
string. These tests pin that collapse:

  - the wrapped (`mirror-review-…`), bare (`pr-<repo>-<num>`), and url-only
    forms of the SAME PR all produce the identical `pr-<repo>-<num>` key;
  - a `pr_url` coordinate WINS over a task_id (spec precedence);
  - a session-less wrapper id with no pr_url strips its ONE known prefix;
  - the underivable case (no id, no url) returns None — the safe "no join".

Pure function: no sandbox side effects, but we still engage _bootstrap first for
loader parity with the rest of the suite.

Run:
    python3 -m unittest scripts.tests.test_decision_identity
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

from decision_identity import canonical_decision_key  # noqa: E402


class CanonicalDecisionKeyTest(unittest.TestCase):
    PR_URL = 'https://github.com/ourliberty/ourliberty-agent-core/pull/763'
    COORD = 'pr-ourliberty-agent-core-763'

    def test_pr_url_yields_coordinate(self):
        self.assertEqual(canonical_decision_key(None, self.PR_URL), self.COORD)

    def test_bare_coordinate_task_id_passthrough(self):
        # A task_id already shaped like the coordinate, no pr_url, is verbatim.
        self.assertEqual(canonical_decision_key(self.COORD, None), self.COORD)

    def test_wrapped_id_plus_pr_url_collapses_to_coordinate(self):
        # The live mirror-review shape: id keyed `mirror-review:<stem>` (a colon
        # the wrapper-prefix list does NOT strip) but carrying a pr_url. The PR
        # coordinate is the only stable cross-surface identity, so it must win.
        wrapped = 'mirror-review:ourliberty-agent-core-763'
        self.assertEqual(
            canonical_decision_key(wrapped, self.PR_URL), self.COORD)

    def test_all_three_forms_of_same_pr_agree(self):
        # wrapped+url, bare coord, url-only — the SAME PR, one key (spec §3).
        keys = {
            canonical_decision_key('mirror-review:anything', self.PR_URL),
            canonical_decision_key(self.COORD, None),
            canonical_decision_key(None, self.PR_URL),
        }
        self.assertEqual(keys, {self.COORD})

    def test_pr_url_wins_over_unrelated_task_id(self):
        # Precedence: a derivable pr_url coordinate beats whatever the task_id is.
        self.assertEqual(
            canonical_decision_key('heal-something-else', self.PR_URL),
            self.COORD,
        )

    def test_sessionless_wrapper_prefix_stripped(self):
        # No pr_url → strip ONE known wrapper prefix, keep the stem.
        self.assertEqual(
            canonical_decision_key('heal-pipeline-stall-x', None),
            'pipeline-stall-x',
        )
        self.assertEqual(
            canonical_decision_key('mirror-review-abc', None), 'abc')
        self.assertEqual(canonical_decision_key('fix-zzz', None), 'zzz')

    def test_bare_prefix_with_empty_stem_not_overmatched(self):
        # A bare prefix must NOT collapse to '' (which would over-match); it is
        # returned verbatim instead.
        self.assertEqual(canonical_decision_key('heal-', None), 'heal-')

    def test_non_wrapper_task_id_passthrough(self):
        self.assertEqual(
            canonical_decision_key('build-feature-12', None),
            'build-feature-12',
        )

    def test_none_inputs_return_none(self):
        self.assertIsNone(canonical_decision_key(None, None))
        self.assertIsNone(canonical_decision_key('', None))
        self.assertIsNone(canonical_decision_key('   ', None))

    def test_unparseable_pr_url_falls_back_to_task_id(self):
        self.assertEqual(
            canonical_decision_key('task-9', 'https://example.com/not-a-pr'),
            'task-9',
        )

    def test_non_string_pr_url_ignored(self):
        self.assertEqual(canonical_decision_key('task-9', 12345), 'task-9')


if __name__ == '__main__':
    unittest.main()
