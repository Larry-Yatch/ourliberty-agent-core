"""Unit tests for scripts/telegram_text_utils.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telegram_text_utils import strip_leading_slash  # noqa: E402


class StripLeadingSlashTests(unittest.TestCase):
    def test_strips_single_leading_slash(self):
        self.assertEqual(strip_leading_slash("/optimize"), "optimize")

    def test_preserves_double_leading_slash(self):
        """`//` is reserved as an escape for literal-slash passthrough."""
        self.assertEqual(strip_leading_slash("//optimize"), "//optimize")

    def test_passthrough_when_no_slash(self):
        self.assertEqual(strip_leading_slash("optimize"), "optimize")

    def test_empty_string(self):
        self.assertEqual(strip_leading_slash(""), "")

    def test_only_slash(self):
        self.assertEqual(strip_leading_slash("/"), "")

    def test_strips_only_first_slash(self):
        self.assertEqual(strip_leading_slash("/foo/bar"), "foo/bar")

    def test_does_not_touch_internal_slashes(self):
        self.assertEqual(strip_leading_slash("foo/bar"), "foo/bar")

    def test_strips_slash_before_whitespace(self):
        self.assertEqual(strip_leading_slash("/ optimize"), " optimize")


if __name__ == "__main__":
    unittest.main()
