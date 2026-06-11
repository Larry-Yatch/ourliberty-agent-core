"""Unit tests for scripts/id_match.id_matches (PR-B, audit #9/#19/#26/#41)."""
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

import id_match  # noqa: E402
from id_match import DEFAULT_MIN_LEN, id_matches  # noqa: E402


class TokenBoundaryTest(unittest.TestCase):
    def test_matches_bounded_token(self):
        self.assertTrue(id_matches('rate-limit-resilience', 'feat(rate-limit-resilience): x'))
        self.assertTrue(id_matches('rate-limit-resilience', 'wt-forge-rate-limit-resilience-001'))

    def test_rejects_substring_inside_larger_token(self):
        # the classic false match: needle is a strict infix of a longer word
        self.assertFalse(id_matches('rate-limit-resil', 'rate-limit-resilience-extra'))
        self.assertFalse(id_matches('configuration-x', 'reconfiguration-xenon'))

    def test_match_is_case_insensitive(self):
        self.assertTrue(id_matches('Rate-Limit-Resilience', 'FEAT(rate-limit-RESILIENCE): x'))

    def test_underscore_and_punctuation_are_boundaries(self):
        self.assertTrue(id_matches('long-task-id-here', 'prefix_long-task-id-here_suffix'))
        self.assertTrue(id_matches('long-task-id-here', 'a.long-task-id-here.b'))


class LengthFloorTest(unittest.TestCase):
    def test_named_dangerous_short_tokens_never_match(self):
        # the exact tokens the audit flagged (api/auth/rotation) are < floor
        for short in ('api', 'auth', 'rotation', 'b', 'e4-2', 'task-34'):
            self.assertFalse(id_matches(short, f'feat({short}): unrelated change'),
                             f'{short!r} should be below the length floor')

    def test_floor_boundary_value(self):
        eleven = 'a' * 11
        twelve = 'a' * 12
        self.assertFalse(id_matches(eleven, f'x-{eleven}-y'))
        self.assertTrue(id_matches(twelve, f'x-{twelve}-y'))

    def test_caller_may_lower_floor(self):
        self.assertTrue(id_matches('api', 'wt-forge-api-001', min_len=1))
        # boundary still enforced even with a low floor
        self.assertFalse(id_matches('api', 'wt-forge-apiserver-001', min_len=1))


class EmptyAndTypeTest(unittest.TestCase):
    def test_empty_or_none_returns_false(self):
        self.assertFalse(id_matches('', 'anything'))
        self.assertFalse(id_matches(None, 'anything'))
        self.assertFalse(id_matches('long-task-id-here', ''))
        self.assertFalse(id_matches('long-task-id-here', None))

    def test_regex_metacharacters_in_needle_are_literal(self):
        # a '.' in the id must match a literal '.', not "any char"
        self.assertTrue(id_matches('build.v2.candidate', 'x build.v2.candidate y'))
        self.assertFalse(id_matches('build.v2.candidate', 'x buildxv2xcandidate y'))


class DefaultExportTest(unittest.TestCase):
    def test_default_floor_is_twelve(self):
        self.assertEqual(DEFAULT_MIN_LEN, 12)
        self.assertEqual(id_match.DEFAULT_MIN_LEN, 12)


if __name__ == '__main__':
    unittest.main()
