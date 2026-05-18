"""Unit tests for scripts/task_type_inference.py.

Verifies the discriminator handles every prefix observed in the 2026-05-18
costs.jsonl distribution, plus regression cases for empty/unknown inputs.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from task_type_inference import infer_task_type  # noqa: E402


class InferTaskTypeTests(unittest.TestCase):

    # ---- Notification (largest bucket: 88 rows in 2026-05-18) ----

    def test_notify_returns_notification(self):
        self.assertEqual(infer_task_type("notify-build-pulse-check-i-001"), "notification")

    def test_notify_of_cycle_fix_still_notification(self):
        """notify-* takes precedence over downstream substrings."""
        self.assertEqual(
            infer_task_type("notify-cycle-fix-foo"), "notification"
        )

    def test_notify_of_marker_error_still_notification(self):
        self.assertEqual(
            infer_task_type("notify-marker-error-foo"), "notification"
        )

    # ---- Retry shapes ----

    def test_marker_error_returns_retry(self):
        self.assertEqual(
            infer_task_type("marker-error-smoke-5a-pf-no-marker-1"), "retry"
        )

    def test_marker_error_cascade_returns_retry(self):
        self.assertEqual(
            infer_task_type("marker-error-marker-error-foo-1-2"), "retry"
        )

    def test_cycle_fix_returns_retry(self):
        self.assertEqual(
            infer_task_type("cycle-fix-interactive-dirty-tree-001"), "retry"
        )

    # ---- Cycle (autonomous Pulse cycles, NOT cycle-fix) ----

    def test_cycle_returns_cycle(self):
        """`cycle-` prefix without `-fix` is the Pulse autonomous loop."""
        self.assertEqual(infer_task_type("cycle-20260518T084158Z"), "cycle")

    def test_cycle_fix_precedence_over_cycle(self):
        """cycle-fix-* must NOT collapse to plain cycle."""
        self.assertEqual(infer_task_type("cycle-fix-foo-001"), "retry")

    # ---- Workflow / build / review / smoke ----

    def test_build_returns_build(self):
        self.assertEqual(infer_task_type("build-ledger-001"), "build")

    def test_review_returns_review(self):
        self.assertEqual(infer_task_type("review-build-foo-001"), "review")

    def test_auto_merge_returns_auto_merge(self):
        self.assertEqual(infer_task_type("auto-merge-gap-pr16-001"), "auto-merge")

    def test_smoke_returns_smoke_test(self):
        self.assertEqual(infer_task_type("smoke-d3-notifier-001"), "smoke-test")

    def test_opmanual_returns_doc_only(self):
        self.assertEqual(
            infer_task_type("opmanual-d35-5b-shipped-note-001"), "doc-only"
        )

    def test_dead_letter_returns_dead_letter(self):
        self.assertEqual(
            infer_task_type("dead-letter-foo-001"), "dead-letter"
        )

    # ---- Unclassified residual ----

    def test_empty_string_returns_unclassified(self):
        self.assertEqual(infer_task_type(""), "unclassified")

    def test_unknown_prefix_returns_unclassified(self):
        """Agent-named or unfamiliar prefixes fall through to unclassified."""
        self.assertEqual(
            infer_task_type("pulse-iter23b-close-decommission-001"),
            "unclassified",
        )
        self.assertEqual(
            infer_task_type("beacon-memory-migration-001"),
            "unclassified",
        )
        self.assertEqual(
            infer_task_type("tunables-first-review-date-001"),
            "unclassified",
        )

    def test_unclassified_distinct_from_unknown(self):
        """The discriminator never returns the legacy "unknown" label —
        that's reserved for "writer set nothing", separate from "inference
        gave up". Keeps the two failure modes distinguishable in the data."""
        for tid in ("", "totally-unrecognized", "pulse-foo", "x"):
            self.assertNotEqual(infer_task_type(tid), "unknown")


if __name__ == "__main__":
    unittest.main()
