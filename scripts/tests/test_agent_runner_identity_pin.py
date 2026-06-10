#!/usr/bin/env python3
"""Tests for the deterministic agent-identity pin (2026-06-10 hotfix).

A dispatched worker spawns with cwd = the fresh worktree ROOT, which has no
top-level CLAUDE.md (agent identities live in agents/<agent>/ subdirs). With
no agent CLAUDE.md resolvable by walking UP from cwd, identity used to resolve
nondeterministically and drew BEACON instead of FORGE (ccd-s1), stalling the
chain. `agent_runner.identity_pin_args` / `build_identity_pin_system_prompt`
fix the worker's identity to the dispatched `expected_agent` by appending an
authoritative statement to the system prompt — derived purely from the agent
NAME, reading no file, so resolution is independent of the worktree's
contents and survives --resume.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_identity_pin
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import agent_runner as ar  # noqa: E402


class BuildIdentityPinSystemPromptTest(unittest.TestCase):
    """Direct coverage of the authoritative identity statement builder."""

    def test_names_dispatched_agent_lowercase(self):
        pin = ar.build_identity_pin_system_prompt('forge')
        self.assertIn(ar.IDENTITY_PIN_MARKER, pin)
        self.assertIn('`forge`', pin)
        self.assertIn('agents/forge/CLAUDE.md', pin)

    def test_normalizes_case_and_whitespace(self):
        pin = ar.build_identity_pin_system_prompt('  FORGE  ')
        self.assertIn('`forge`', pin)
        self.assertNotIn('FORGE', pin)

    def test_marks_pin_authoritative(self):
        # The whole point of the pin is that it OVERRIDES discovered CLAUDE.md.
        pin = ar.build_identity_pin_system_prompt('mirror')
        self.assertIn('AUTHORITATIVE', pin)

    def test_reads_no_file_independent_of_worktree_contents(self):
        # Regression for the root cause: identity must NOT depend on a
        # CLAUDE.md being discoverable. The builder derives everything from
        # the name, so a bogus agent with no identity file on disk anywhere
        # still produces a deterministic, correctly-named pin.
        pin = ar.build_identity_pin_system_prompt('nonexistent-agent-xyz')
        self.assertIn('`nonexistent-agent-xyz`', pin)
        self.assertIn(ar.IDENTITY_PIN_MARKER, pin)

    def test_ccd_s1_regression_forge_not_beacon(self):
        # The exact mis-identification that stalled ccd-s1: a forge dispatch
        # drew Beacon. The pin for forge must assert forge and tell the worker
        # to ignore any sibling agent's CLAUDE.md (e.g. beacon's) in the tree.
        pin = ar.build_identity_pin_system_prompt('forge')
        self.assertIn('`forge`', pin)
        self.assertNotIn('beacon', pin.lower())
        self.assertIn('ignore', pin.lower())


class IdentityPinArgsTest(unittest.TestCase):
    """The CLI-arg wrapper consumed by run_claude's spawn path."""

    def test_empty_when_no_expected_agent(self):
        self.assertEqual(ar.identity_pin_args(None), [])
        self.assertEqual(ar.identity_pin_args(''), [])

    def test_appends_system_prompt_when_set(self):
        args = ar.identity_pin_args('forge')
        self.assertEqual(args[0], '--append-system-prompt')
        self.assertEqual(len(args), 2)
        self.assertEqual(args[1], ar.build_identity_pin_system_prompt('forge'))

    def test_pin_is_deterministic_for_same_agent(self):
        # Same dispatched agent -> identical pin every time, regardless of
        # ambient state (no cwd, no file reads). This is what makes a worktree
        # with a missing/ambiguous CLAUDE.md still resolve to the dispatched
        # agent.
        self.assertEqual(
            ar.identity_pin_args('forge'),
            ar.identity_pin_args('forge'),
        )


if __name__ == '__main__':
    unittest.main()
