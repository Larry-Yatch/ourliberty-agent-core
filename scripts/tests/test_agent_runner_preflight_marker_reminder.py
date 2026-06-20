#!/usr/bin/env python3
"""Tests for the deterministic preflight marker reminder (2026-06-19).

A phase=preflight Forge dispatch must end its turn with exactly one marker
block, but the task `prompt` is authored upstream and often omits the
"decide + emit one marker" reminder and uses build-phase imperative verbs
that prime Forge to ACT instead of DECIDE. That produced the recurring
outbox-notifier WARN `phase=preflight requires ONE marker block ... none
found`. `agent_runner.preflight_marker_reminder_args` appends an
authoritative, last-in-context marker-discipline reminder to the worker's
system prompt on every Forge preflight dispatch — paralleling the identity
pin — so the requirement no longer depends on the task author remembering it.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_preflight_marker_reminder
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

import agent_runner as ar  # noqa: E402


class BuildPreflightMarkerReminderSystemPromptTest(unittest.TestCase):
    """Direct coverage of the reminder text builder."""

    def test_carries_marker_and_decide_not_act(self):
        text = ar.build_preflight_marker_reminder_system_prompt()
        self.assertIn(ar.PREFLIGHT_MARKER_REMINDER_MARKER, text)
        # Substance: preflight decides, does not write code / open PRs.
        self.assertIn('phase=preflight', text)
        self.assertIn('DECIDES', text)

    def test_names_all_three_marker_types(self):
        text = ar.build_preflight_marker_reminder_system_prompt()
        self.assertIn('=== PROCEED ===', text)
        self.assertIn('=== CLARIFY_REQUEST ===', text)
        self.assertIn('=== REJECT ===', text)

    def test_warns_prose_ending_dead_letters(self):
        # The exact failure mode being neutralized: a turn that ends on prose.
        text = ar.build_preflight_marker_reminder_system_prompt()
        self.assertIn('dead-letter', text.lower())

    def test_deterministic(self):
        self.assertEqual(
            ar.build_preflight_marker_reminder_system_prompt(),
            ar.build_preflight_marker_reminder_system_prompt(),
        )


class PreflightMarkerReminderArgsTest(unittest.TestCase):
    """The CLI-arg wrapper consumed by run_claude's spawn path."""

    def test_appends_for_forge_preflight(self):
        args = ar.preflight_marker_reminder_args('preflight', 'forge')
        self.assertEqual(args[0], '--append-system-prompt')
        self.assertEqual(len(args), 2)
        self.assertEqual(
            args[1], ar.build_preflight_marker_reminder_system_prompt()
        )

    def test_normalizes_case_and_whitespace(self):
        # The dispatch fields may arrive with stray case/whitespace; the gate
        # must still fire for a Forge preflight.
        self.assertEqual(
            ar.preflight_marker_reminder_args('  Preflight  ', '  FORGE  '),
            ar.preflight_marker_reminder_args('preflight', 'forge'),
        )
        self.assertEqual(len(ar.preflight_marker_reminder_args('PREFLIGHT', 'Forge')), 2)

    def test_empty_for_build_and_revision_phases(self):
        # Markers are preflight-only; build uses 'PR opened:', revision uses
        # 'Revision N applied:'. Those phases must NOT carry the reminder.
        self.assertEqual(ar.preflight_marker_reminder_args('build', 'forge'), [])
        self.assertEqual(ar.preflight_marker_reminder_args('revision', 'forge'), [])

    def test_empty_when_phase_missing(self):
        self.assertEqual(ar.preflight_marker_reminder_args(None, 'forge'), [])
        self.assertEqual(ar.preflight_marker_reminder_args('', 'forge'), [])

    def test_empty_for_non_forge_agent(self):
        # Other agents have their own marker grammars; this reminder is Forge's.
        self.assertEqual(ar.preflight_marker_reminder_args('preflight', 'mirror'), [])
        self.assertEqual(ar.preflight_marker_reminder_args('preflight', 'beacon'), [])
        self.assertEqual(ar.preflight_marker_reminder_args('preflight', None), [])


if __name__ == '__main__':
    unittest.main()
