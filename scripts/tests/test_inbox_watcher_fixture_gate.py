#!/usr/bin/env python3
"""Regression gate for the inbox-watcher fixture-suppression boundary.

Background: 2026-05-28 — the inbox-watcher dispatched test/fixture envelopes to
an LLM as if they were real tasks, in a self-replicating notify/dead-letter/
marker-error cascade that burned $261 of real Opus in a day. PR #147's fixture
allowlist only guarded Pulse's /cycle path; it was NOT enforced at the watcher's
dispatch boundary, and the cascade buried fixture ids behind wrapper prefixes
that a raw is_fixture_task_id missed. This test IS the enforcement mechanism for
the gate (per "every rule needs an enforcement mechanism").

Covers:
  1. fixture_patterns.matched_fixture_envelope peels routing wrappers + seq
     suffixes and re-tests the allowlist at each layer; real ids pass through.
  2. inbox_watcher.process_task archives a fixture envelope to .archive/ with a
     `fixture-suppressed` $0 cost row and NEVER calls agent_runner.run_claude.
  3. A non-fixture envelope passes the gate (reaches the validator).

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_inbox_watcher_fixture_gate
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import fixture_patterns  # noqa: E402
import inbox_watcher  # noqa: E402


class EnvelopeNameMatchingTest(unittest.TestCase):
    """matched_fixture_envelope / is_fixture_envelope_name behavior."""

    def test_bare_fixture_ids_match(self):
        for name in ("t-foo", "notify-t-x", "notify-q-7", "marker-error-t-z",
                     "marker-error-opmanual-d35", "task-001", "task-legacy"):
            self.assertTrue(fixture_patterns.is_fixture_envelope_name(name), name)

    def test_wrapped_fixture_ids_match(self):
        # The exact acceptance-criteria forms from the brief.
        self.assertEqual(
            fixture_patterns.matched_fixture_envelope("marker-error-notify-t-pf-1"),
            "notify-t-",
        )
        self.assertEqual(
            fixture_patterns.matched_fixture_envelope("notify-dead-letter-notify-q-1.18"),
            "notify-q-",
        )
        # Doubled-notify dead-letter cascade around the task-legacy sibling.
        self.assertTrue(fixture_patterns.is_fixture_envelope_name(
            "dead-letter-notify-notify-task-legacy.18"))
        # Stacked seq suffixes.
        self.assertTrue(fixture_patterns.is_fixture_envelope_name("notify-t-pf-1.3.7"))

    def test_real_ids_do_not_match(self):
        for name in ("notify-deploy-prod-42", "tier2-fallback-fix",
                     "pulse-cycle-upgrade-design-pass", "deploy-001",
                     "mirror-review-pr-200", "notify-task-legacy-prod-work"):
            self.assertIsNone(fixture_patterns.matched_fixture_envelope(name), name)
            self.assertFalse(fixture_patterns.is_fixture_envelope_name(name), name)

    def test_non_string_and_empty(self):
        for bad in (None, 123, {"task_id": "t-x"}, "", []):
            self.assertIsNone(fixture_patterns.matched_fixture_envelope(bad))
            self.assertFalse(fixture_patterns.is_fixture_envelope_name(bad))


class DispatchGateTest(unittest.TestCase):
    """process_task suppresses fixtures and lets real tasks reach the validator."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fixture-gate-test-"))
        self.inboxes = self.tmp / "inboxes"
        self.outboxes = self.tmp / "outboxes"
        self.blackboard = self.tmp / "blackboard"
        self.costs = self.blackboard / "costs.jsonl"
        for agent in ("beacon", "forge", "mirror", "pulse"):
            (self.inboxes / agent).mkdir(parents=True, exist_ok=True)
        self.blackboard.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(inbox_watcher, "INBOXES_ROOT", self.inboxes),
            mock.patch.object(inbox_watcher, "OUTBOXES_ROOT", self.outboxes),
            mock.patch.object(inbox_watcher, "BLACKBOARD", self.blackboard),
            mock.patch.object(inbox_watcher, "COSTS_FILE", self.costs),
            mock.patch.object(inbox_watcher, "LOG_FILE", self.tmp / "watcher.log"),
            # Guard rail: if the gate leaks, the dispatch attempt blows up loudly.
            mock.patch.object(
                inbox_watcher.agent_runner, "run_claude",
                side_effect=AssertionError("run_claude must not be called for a fixture"),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_envelope(self, agent: str, name: str, task: dict) -> Path:
        f = self.inboxes / agent / f"{name}.json"
        f.write_text(json.dumps(task))
        return f

    def _last_cost_row(self) -> dict:
        lines = [l for l in self.costs.read_text().splitlines() if l.strip()]
        self.assertTrue(lines, "expected a cost row to be appended")
        return json.loads(lines[-1])

    def test_wrapped_fixture_envelope_is_suppressed(self):
        agent = "beacon"
        name = "marker-error-notify-t-pf-1.18"
        task_file = self._write_envelope(agent, name, {
            "task_id": "t-pf", "prompt": "should never run", "source": "forge",
        })
        inbox_watcher.process_task(agent, task_file, models_config={})

        # Not dispatched: no outbox written.
        outbox_dir = self.outboxes / agent
        self.assertEqual(
            list(outbox_dir.glob("*.json")) if outbox_dir.exists() else [],
            [],
            "fixture must not produce an outbox",
        )
        # Archived, removed from the live inbox.
        self.assertFalse(task_file.exists())
        archived = list((self.inboxes / agent / ".archive").glob("*.json"))
        self.assertEqual(len(archived), 1)
        # $0 fixture-suppressed cost row.
        row = self._last_cost_row()
        self.assertEqual(row["task_type"], "fixture-suppressed")
        self.assertEqual(row["cost_usd"], 0.0)
        self.assertEqual(row["agent"], agent)

    def test_fixture_task_id_field_is_suppressed_even_with_neutral_filename(self):
        # Defense in depth: the envelope NAME is neutral but the task_id field
        # is a fixture — still suppressed via matched_fixture_pattern.
        agent = "forge"
        task_file = self._write_envelope(agent, "incoming-2026", {
            "task_id": "t-loop-1", "prompt": "x", "source": "beacon",
        })
        inbox_watcher.process_task(agent, task_file, models_config={})
        self.assertFalse(task_file.exists())
        self.assertEqual(len(list((self.inboxes / agent / ".archive").glob("*.json"))), 1)
        self.assertEqual(self._last_cost_row()["task_type"], "fixture-suppressed")

    def test_real_envelope_passes_gate_to_validator(self):
        # A non-fixture task must NOT be fixture-suppressed; it proceeds to the
        # validator. We stub the validator to reject so we can prove the gate
        # let it through (lands in .invalid, not .archive) without an LLM call.
        agent = "forge"
        task_file = self._write_envelope(agent, "real-build-123", {
            "task_id": "real-build-123", "prompt": "do real work", "source": "beacon",
        })
        with mock.patch.object(
            inbox_watcher.dispatch_validator, "validate_task",
            return_value=(False, "stubbed-reject"),
        ):
            inbox_watcher.process_task(agent, task_file, models_config={})
        # Reached the validator -> .invalid, NOT fixture-suppressed -> .archive.
        self.assertFalse(task_file.exists())
        self.assertEqual(len(list((self.inboxes / agent / ".archive").glob("*.json"))), 0)
        self.assertEqual(len(list((self.inboxes / agent / ".invalid").glob("*.json"))), 1)
        self.assertFalse(self.costs.exists() and self.costs.read_text().strip(),
                         "real task that never dispatched should not write a cost row")


if __name__ == "__main__":
    unittest.main()
