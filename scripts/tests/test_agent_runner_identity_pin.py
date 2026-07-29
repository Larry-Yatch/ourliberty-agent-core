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


class AgentManualPathTest(unittest.TestCase):
    """The manual must be reachable from a worktree of ANY target repo.

    Regression for the lens-loading gap (2026-07-28, specs/mirror-lens-loading-gap.md):
    the pin named `agents/<agent>/CLAUDE.md` relatively, which resolves against
    cwd — a worktree of the repo under review. It therefore resolved ONLY when
    the target repo was agent-core. On RSDPM the manual was unreachable, so
    Mirror never read step 4b and the bug-hunt lenses (A–J) did not run on a
    single RSDPM PR: 178 reviews, median 40s, vs 325s on agent-core.

    Each test here FAILS against the pre-fix pin string, which is the point —
    a guard only ever observed passing proves nothing (standing rule 9).
    """

    def test_candidate_paths_are_absolute(self):
        for path in ar.agent_manual_paths('mirror'):
            self.assertTrue(
                Path(path).is_absolute(),
                'manual path must not depend on cwd: ' + path,
            )

    def test_candidates_cover_runtime_and_repo_copies(self):
        runtime, in_repo = ar.agent_manual_paths('mirror')
        self.assertTrue(runtime.endswith('agents/mirror/workspace/CLAUDE.md'))
        self.assertTrue(in_repo.endswith('agents/mirror/CLAUDE.md'))

    def test_honors_agents_root_redirect(self):
        # Resolved at call time so the sandbox redirect (and any relocation of
        # the runtime tree) is honored — not baked to a literal /home/larry.
        import os as _os
        prior = _os.environ.get('OURLIBERTY_AGENTS_ROOT')
        _os.environ['OURLIBERTY_AGENTS_ROOT'] = '/tmp/redirected-agents-root'
        try:
            runtime = ar.agent_manual_paths('mirror')[0]
        finally:
            if prior is None:
                _os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
            else:
                _os.environ['OURLIBERTY_AGENTS_ROOT'] = prior
        self.assertEqual(
            runtime,
            '/tmp/redirected-agents-root/agents/mirror/workspace/CLAUDE.md',
        )

    def test_pin_carries_the_absolute_paths(self):
        pin = ar.build_identity_pin_system_prompt('mirror')
        for path in ar.agent_manual_paths('mirror'):
            self.assertIn(path, pin)

    def test_pin_forbids_resolving_the_manual_against_cwd(self):
        pin = ar.build_identity_pin_system_prompt('mirror')
        self.assertIn('relative to your cwd', pin.lower())

    def test_pin_denies_the_target_repo_rulebook_as_a_substitute(self):
        # The decoy is the active ingredient: RSDPM/CLAUDE.md auto-loads from
        # the review worktree, is titled "build-agent rulebook", and heads a
        # section "Standing rules — Mirror REJECTS a PR that breaks any of
        # these". It addressed Mirror by name, passed her identity check, and
        # ended her search. An absent manual makes a worker hunt harder; a
        # plausible wrong one makes it stop. So the pin must say, explicitly,
        # that being named by a repo rulebook does not make it the manual.
        pin = ar.build_identity_pin_system_prompt('mirror').lower()
        self.assertIn('target repo', pin)
        self.assertIn('by name', pin)
        self.assertIn('never instead of it', pin)


class IdentityAssertionPreambleTest(unittest.TestCase):
    """The soft preamble must not be satisfiable by the decoy either.

    Review finding on this PR: fixing only the pin left
    `build_expected_agent_assertion` still saying "verify that the CLAUDE.md
    LOADED into your context identifies you as `mirror` ... if it matches,
    proceed normally". That is the instruction Mirror actually followed on
    RSDPM #152 — the target rulebook named her and no other agent, so the
    assertion passed and told her to proceed, and she never opened her real
    manual. A bouncer a decoy walks past is not a bouncer.
    """

    def test_preamble_points_at_the_absolute_manual(self):
        pre = ar.build_expected_agent_assertion('mirror')
        for path in ar.agent_manual_paths('mirror'):
            self.assertIn(path, pre)

    def test_preamble_does_not_accept_a_cwd_claude_md(self):
        pre = ar.build_expected_agent_assertion('mirror').lower()
        self.assertIn('target repo', pre)
        self.assertIn('does not satisfy this', pre)

    def test_preamble_keeps_the_mismatch_contract(self):
        # The escape hatch this preamble exists for must survive the rewrite.
        pre = ar.build_expected_agent_assertion('mirror')
        self.assertIn('IDENTITY_MISMATCH: expected=mirror', pre)
        self.assertIn(ar.IDENTITY_ASSERTION_MARKER, pre)

    def test_preamble_honors_an_explicit_agents_root(self):
        pre = ar.build_expected_agent_assertion('mirror', agents_root='/srv/ar')
        self.assertIn('/srv/ar/agents/mirror/workspace/CLAUDE.md', pre)


class PinInstructionSafetyTest(unittest.TestCase):
    """Two review findings on the pin's new wording."""

    def test_missing_manual_still_demands_a_marker(self):
        # "Say so rather than proceeding" invites a prose-only answer, and a
        # markerless response cannot be routed — the PR #16 shape, where a
        # marker-contract failure left a PR unmerged for 7h+. Failing closed
        # is right; failing closed WITH a marker is what routes.
        pin = ar.build_identity_pin_system_prompt('mirror').lower()
        self.assertIn('marker block', pin)
        self.assertIn('cannot be routed', pin)

    def test_full_read_is_scoped_to_the_first_turn(self):
        # The pin is appended on every invocation INCLUDING --resume, so an
        # unconditional "read it in full" makes every revision round re-read
        # a 44-86 KB manual already in context. The sibling preamble
        # suppresses itself on resume for exactly this reason.
        pin = ar.build_identity_pin_system_prompt('mirror').lower()
        self.assertIn('already read it in\nthis session', pin)


class AgentsRootThreadingTest(unittest.TestCase):
    """The pin must advertise the root the CHILD gets, not the parent's.

    `run_claude` pins `OURLIBERTY_AGENTS_ROOT` into the child env and then
    builds the pin; deriving the root twice, independently, is how a path
    silently starts pointing nowhere — the failure class this PR fixes.
    """

    def test_explicit_root_wins_over_process_env(self):
        runtime = ar.agent_manual_paths('mirror', agents_root='/srv/agents')[0]
        self.assertEqual(
            runtime, '/srv/agents/agents/mirror/workspace/CLAUDE.md')

    def test_pin_args_thread_the_root_through(self):
        args = ar.identity_pin_args('mirror', agents_root='/srv/agents')
        self.assertIn('/srv/agents/agents/mirror/workspace/CLAUDE.md', args[1])

    def test_repo_candidate_is_absolute_without_symlink_resolution(self):
        # .absolute() not .resolve(): absolute is the invariant, following
        # symlinks in a swapped deploy tree is not wanted.
        in_repo = ar.agent_manual_paths('mirror')[1]
        expected = Path(ar.__file__).absolute().parent.parent / \
            'agents' / 'mirror' / 'CLAUDE.md'
        self.assertEqual(in_repo, str(expected))
        self.assertTrue(Path(in_repo).is_absolute())


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
