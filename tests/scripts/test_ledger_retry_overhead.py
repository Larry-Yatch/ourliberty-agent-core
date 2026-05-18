"""Unit tests for scripts/ledger_weekly.py::compute_retry_overhead.

Covers the v2 (2026-05-18) heuristic tuning that fixed the over-counting
bug where all `notify-*` workflow rows were classified as retries.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from ledger_weekly import CostRow, _is_retry_task_id, compute_retry_overhead  # noqa: E402


def row(task_id: str, cost_usd: float = 1.0, agent: str = "forge") -> CostRow:
    """Build a minimal CostRow for tests."""
    return CostRow(
        ts=datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc),
        agent=agent,
        task_id=task_id,
        model="claude-opus-4-7",
        cost_usd=cost_usd,
        duration_sec=10.0,
        source="inbox-watcher",
    )


class IsRetryTaskIdTests(unittest.TestCase):
    """Discriminator behaviour for individual task_id shapes."""

    # ---- Excluded: notify-* (workflow, not retry) ----

    def test_plain_notify_not_retry(self):
        """notify-foo is the inter-agent workflow channel, not a retry."""
        self.assertFalse(_is_retry_task_id("notify-build-pulse-check-i-001"))

    def test_notify_of_cycle_fix_not_retry(self):
        """A notify-* wrapping a cycle-fix result is workflow, not retry.

        Regression: v1 counted this as retry because '-cycle-fix-' substring
        matched. v2 excludes anything that starts with notify-.
        """
        self.assertFalse(
            _is_retry_task_id("notify-cycle-fix-interactive-dirty-tree-20260516T101000Z")
        )

    def test_notify_of_marker_error_not_retry(self):
        """A notify-* wrapping a marker-error result is workflow."""
        self.assertFalse(_is_retry_task_id("notify-marker-error-foo-1"))

    def test_notify_with_revision_in_name_not_retry(self):
        """notify-* takes precedence over the -revision- infix."""
        self.assertFalse(_is_retry_task_id("notify-foo-revision-1"))

    # ---- Included: actual retry shapes ----

    def test_marker_error_is_retry(self):
        """marker-error-* is the first round of the marker-error cascade."""
        self.assertTrue(_is_retry_task_id("marker-error-smoke-5a-pf-no-marker-1"))

    def test_double_marker_error_is_retry(self):
        """marker-error-marker-error-* is the second cascade round."""
        self.assertTrue(_is_retry_task_id("marker-error-marker-error-foo-1-2"))

    def test_cycle_fix_is_retry(self):
        """cycle-fix-* is a Pulse-driven re-dispatch (retry shape)."""
        self.assertTrue(_is_retry_task_id("cycle-fix-interactive-dirty-tree-001"))

    def test_revision_infix_is_retry(self):
        """A task_id containing -revision- is a Forge revision round."""
        self.assertTrue(_is_retry_task_id("build-feature-foo-revision-2"))

    # ---- Excluded: normal task shapes ----

    def test_feature_development_not_retry(self):
        self.assertFalse(_is_retry_task_id("build-ledger-001"))

    def test_doc_only_not_retry(self):
        self.assertFalse(_is_retry_task_id("opmanual-d35-5d-shipped-section-001"))

    def test_empty_string_not_retry(self):
        self.assertFalse(_is_retry_task_id(""))


class ComputeRetryOverheadTests(unittest.TestCase):
    """Aggregation behaviour given a mixed-shape batch of CostRows."""

    def test_zero_rows(self):
        out = compute_retry_overhead([], total_usd=0.0)
        self.assertEqual(out["total_retry_cost_usd"], 0.0)
        self.assertEqual(out["percent_of_total"], 0.0)

    def test_only_workflow_notify_yields_zero(self):
        """Regression: the v1 bug. 88 notify-* rows totalling $26.51 should
        no longer be counted as retries."""
        rows = [row(f"notify-task-{i}", cost_usd=0.30) for i in range(10)]
        out = compute_retry_overhead(rows, total_usd=3.0)
        self.assertEqual(out["total_retry_cost_usd"], 0.0)
        self.assertEqual(out["percent_of_total"], 0.0)

    def test_only_real_retries_sums_correctly(self):
        rows = [
            row("marker-error-foo-1", cost_usd=0.30),
            row("cycle-fix-bar-001", cost_usd=0.50),
            row("build-baz-revision-1", cost_usd=0.40),
        ]
        out = compute_retry_overhead(rows, total_usd=1.2)
        self.assertAlmostEqual(out["total_retry_cost_usd"], 1.2, places=6)
        self.assertAlmostEqual(out["percent_of_total"], 100.0, places=6)

    def test_mixed_batch_counts_only_real_retries(self):
        rows = [
            row("build-feature-foo", cost_usd=1.00),         # normal: not retry
            row("notify-build-feature-foo", cost_usd=0.25),  # workflow: not retry
            row("notify-build-feature-foo", cost_usd=0.30),  # workflow: not retry
            row("marker-error-feature-foo-1", cost_usd=0.40),  # retry: counted
            row("cycle-fix-foo-001", cost_usd=0.50),         # retry: counted
        ]
        out = compute_retry_overhead(rows, total_usd=2.45)
        # Real retry overhead is $0.90 = 36.7% of $2.45
        self.assertAlmostEqual(out["total_retry_cost_usd"], 0.90, places=6)
        self.assertAlmostEqual(out["percent_of_total"], 0.90 / 2.45 * 100.0, places=4)

    def test_zero_total_does_not_divide_by_zero(self):
        rows = [row("marker-error-foo-1", cost_usd=0.0)]
        out = compute_retry_overhead(rows, total_usd=0.0)
        self.assertEqual(out["percent_of_total"], 0.0)


if __name__ == "__main__":
    unittest.main()
