#!/usr/bin/env python3
"""Tests for scripts/fixture_patterns.is_fixture_envelope_name (2026-05-28).

Parallel to test_pulse_cycle_fixture_allowlist.py which already exercises
is_fixture_task_id + drift mirrors. This file isolates the new
envelope-name helper that the inbox-watcher dispatch gate consumes:
wrapper-peeling, .<seq> tolerance, cycle guard, and the wrapped fixture
forms observed in the 2026-05-28 loop (`marker-error-notify-t-pf-1.json`,
`notify-dead-letter-notify-q-1.18.json`, etc.).

Run:
    python3 -m unittest scripts.tests.test_fixture_patterns
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import fixture_patterns as fp  # noqa: E402


class IsFixtureEnvelopeNameTest(unittest.TestCase):

    # ------------------------------------------------------------------
    # Single-layer wrapped fixtures — the common shape produced by the
    # routing layer wrapping a fixture task_id once.
    # ------------------------------------------------------------------

    def test_single_layer_wrapped_fixtures_match(self):
        cases = [
            "notify-t-pf-1.json",
            "notify-q-1.json",
            "marker-error-t-built.json",
            "marker-error-t-zero-1.json",
            "dead-letter-t-pf.json",
            "marker-error-opmanual-d35-5b-shipped-note-001-1.json",
        ]
        for name in cases:
            with self.subTest(name=name):
                self.assertTrue(
                    fp.is_fixture_envelope_name(name),
                    f"single-layer wrapped fixture {name!r} should match",
                )

    # ------------------------------------------------------------------
    # Doubled-prefix forms — the brief calls these out as the shapes the
    # 2026-05-28 self-replicating loop produced. They bury the fixture
    # task_id behind two or three wrappers.
    # ------------------------------------------------------------------

    def test_doubled_prefix_fixtures_match(self):
        cases = [
            # Brief-named forms:
            "marker-error-notify-t-pf-1.json",
            "notify-dead-letter-notify-q-1.18.json",
            # Doubled `notify-notify-` against the newly-added task-legacy:
            "notify-notify-task-legacy.json",
            # Triple-stack (cascade observation):
            "notify-dead-letter-notify-notify-task-legacy.18.json",
            # marker-error wrapping a notify-wrapped fixture:
            "marker-error-notify-t-built.json",
        ]
        for name in cases:
            with self.subTest(name=name):
                self.assertTrue(
                    fp.is_fixture_envelope_name(name),
                    f"doubled-prefix fixture {name!r} should match",
                )

    # ------------------------------------------------------------------
    # Trailing .<seq> from inbox_watcher._unique_dest collision-rename.
    # ------------------------------------------------------------------

    def test_trailing_seq_suffix_tolerated(self):
        cases = [
            "notify-t-pf.18.json",
            "notify-q-1.5.json",
            "marker-error-t-built.42.json",
            "t-pf.999.json",
        ]
        for name in cases:
            with self.subTest(name=name):
                self.assertTrue(
                    fp.is_fixture_envelope_name(name),
                    f"trailing-seq fixture {name!r} should match",
                )

    # ------------------------------------------------------------------
    # Bare fixture task_id (no wrapper, no .json) — helper delegates to
    # is_fixture_task_id transparently.
    # ------------------------------------------------------------------

    def test_bare_fixture_task_id_matches(self):
        for tid in ("t-pf", "task-001", "task-legacy", "notify-q-1"):
            with self.subTest(tid=tid):
                self.assertTrue(fp.is_fixture_envelope_name(tid))

    # ------------------------------------------------------------------
    # Real task_ids — wrapped + unwrapped — must NOT match. These are
    # pulled from recent main commits and active worktrees so any
    # broadening of the helper would trip this gate.
    # ------------------------------------------------------------------

    def test_real_task_ids_do_not_match(self):
        cases = [
            # Plain real task_ids:
            "e4-4e-live-operations-polish-v1.json",
            "build-watchdog-bot-liveness-policy-001.json",
            "heal-pipeline-stall-reconciliation.json",
            "pr-s4-rectification-v1.json",
            # Real task_ids wrapped by routing — wrapper peels reveal a
            # NON-fixture base, so the helper must NOT flag:
            "notify-e4-4f-missions-tab-v1-pr-b.json",
            "marker-error-orchestrator-rectification-v2-1.json",
            "dead-letter-build-watchdog-bot-liveness-policy-001.json",
            "notify-notify-pr-s4-rectification-v1.json",
            # Real task_id with trailing .<seq> (from a re-dispatch):
            "build-watchdog-bot-liveness-policy-001.3.json",
        ]
        for name in cases:
            with self.subTest(name=name):
                self.assertFalse(
                    fp.is_fixture_envelope_name(name),
                    f"real envelope {name!r} unexpectedly flagged",
                )

    # ------------------------------------------------------------------
    # Cycle guard — pathological input with 20+ nested wrappers must NOT
    # hang and must return False (conservative — never false-positive on
    # an unrecognized base hidden behind cap-exceeding wrappers).
    # ------------------------------------------------------------------

    def test_cycle_guard_bounds_pathological_input(self):
        # 20 stacked `notify-` wrappers around a non-fixture base.
        name = "notify-" * 20 + "real-task.json"
        # Helper must terminate quickly + return False (cap-tripped means
        # conservative non-match, not a hang or a false-positive).
        self.assertFalse(fp.is_fixture_envelope_name(name))

    def test_cycle_guard_does_not_false_positive_on_wrapped_real(self):
        # Pathologically deep wrapping of a real task_id — must remain False.
        name = "notify-dead-letter-" * 10 + "build-real-thing.json"
        self.assertFalse(fp.is_fixture_envelope_name(name))

    # ------------------------------------------------------------------
    # Degenerate inputs (defensive).
    # ------------------------------------------------------------------

    def test_degenerate_inputs_return_false(self):
        for value in ("", None, 42, [], {}, object()):
            with self.subTest(value=value):
                self.assertFalse(fp.is_fixture_envelope_name(value))

    # ------------------------------------------------------------------
    # task-legacy was added in the same commit that introduced this helper.
    # Pin the decision explicitly so a future regression that drops the
    # entry would break this test loudly.
    # ------------------------------------------------------------------

    def test_task_legacy_is_an_exact_fixture(self):
        self.assertIn("task-legacy", fp.FIXTURE_PATTERN_EXACT)
        self.assertTrue(fp.is_fixture_task_id("task-legacy"))
        self.assertTrue(fp.is_fixture_envelope_name("task-legacy.json"))
        self.assertTrue(
            fp.is_fixture_envelope_name("dead-letter-task-legacy.json"),
        )


if __name__ == "__main__":
    unittest.main()
