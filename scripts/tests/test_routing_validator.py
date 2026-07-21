#!/usr/bin/env python3
"""Fixtures for `routing_validator` — hard topology + soft IDENTITY reroute.

Phase D3-prep (2026-05-11). Adapted from upstream's
`scripts/tests/test_routing_*` shape; the hard-topology layer is Larry-shaped
and unique to this fork.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_routing_validator
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import routing_validator as rv  # noqa: E402


class HardTopologyTest(unittest.TestCase):
    """Hard topology: explicit (source, target) allow-list + dialogue bypass."""

    def test_allowed_routes_pass(self):
        for source, target in [
            ('pulse', 'beacon'),
            ('beacon', 'forge'),
            ('beacon', 'mirror'),
            ('beacon', 'pulse'),
            ('forge', 'beacon'),
            ('mirror', 'beacon'),
            # D3.5 commit 5a: mirror → forge added for 5b's revision loop.
            ('mirror', 'forge'),
            ('larry', 'forge'),
            ('forge-question', 'beacon'),
            ('mirror-question', 'beacon'),
        ]:
            ok, reason = rv.check_hard_topology(source, target)
            self.assertTrue(ok, f'{source}->{target} should pass, got {reason}')

    def test_disallowed_routes_blocked(self):
        for source, target in [
            ('pulse', 'forge'),       # Pulse can't dispatch work
            ('pulse', 'mirror'),
            ('forge', 'mirror'),      # Review trigger goes via outbox-notifier, not direct
            ('forge', 'pulse'),
            ('mirror', 'pulse'),
        ]:
            ok, _ = rv.check_hard_topology(source, target)
            self.assertFalse(ok, f'{source}->{target} should be denied')

    def test_self_dispatch_denied(self):
        for agent in ['beacon', 'forge', 'mirror', 'pulse']:
            ok, reason = rv.check_hard_topology(agent, agent)
            self.assertFalse(ok)
            self.assertIn('self-dispatch', reason)

    def test_dialogue_suffixes_bypass(self):
        # -result, -clarification, -answer all bypass hard topology
        for source, target in [
            ('beacon-result', 'pulse'),
            ('forge-result', 'beacon'),
            ('beacon-clarification', 'forge'),
            ('beacon-clarification', 'mirror'),
            ('mirror-answer', 'beacon'),
        ]:
            ok, _ = rv.check_hard_topology(source, target)
            self.assertTrue(ok, f'{source}->{target} should bypass via dialogue suffix')

    def test_system_sources_bypass(self):
        for source in ['telegram-webhook', 'cron', 'continuation', 'orchestrator']:
            for target in ['beacon', 'forge', 'mirror', 'pulse']:
                ok, _ = rv.check_hard_topology(source, target)
                self.assertTrue(ok, f'{source}->{target} should pass (system source)')

    def test_unknown_source_denied(self):
        ok, reason = rv.check_hard_topology('rogue-agent', 'beacon')
        self.assertFalse(ok)
        self.assertIn('no allowed routes', reason)

    def test_empty_inputs_denied(self):
        self.assertFalse(rv.check_hard_topology('', 'beacon')[0])
        self.assertFalse(rv.check_hard_topology('beacon', '')[0])


class DashboardRouteTest(unittest.TestCase):
    """2026-06-10 incident: dashboard Approve/Reject envelopes must route to
    beacon, and an unlisted source must STILL be denied (no over-broad allow)."""

    def test_dashboard_to_beacon_accepted(self):
        ok, reason = rv.check_hard_topology('dashboard', 'beacon')
        self.assertTrue(ok, f'dashboard->beacon should pass, got {reason}')
        # End-to-end through the combined entry point (no soft-reroute config).
        final, rerouted, _ = rv.validate_route(
            'beacon', {'source': 'dashboard', 'prompt': 'approve'},
        )
        self.assertEqual(final, 'beacon')
        self.assertFalse(rerouted)

    def test_dashboard_to_non_beacon_still_denied(self):
        # The fix is scoped: dashboard may only reach beacon, not arbitrary agents.
        for target in ('forge', 'mirror', 'pulse'):
            ok, _ = rv.check_hard_topology('dashboard', target)
            self.assertFalse(ok, f'dashboard->{target} must remain denied')

    def test_unlisted_source_still_denied(self):
        # Regression guard against an over-broad allow: a source that is NOT in
        # FRESH_DISPATCH_ROUTES / SYSTEM_SOURCES / a dialogue leg must be denied.
        ok, reason = rv.check_hard_topology('made-up-surface', 'beacon')
        self.assertFalse(ok)
        self.assertIn('no allowed routes', reason)


class SoftRerouteTest(unittest.TestCase):
    """Per-agent IDENTITY.md constraints. Validator fails open when missing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_root = rv.REPO_ROOT
        rv.REPO_ROOT = Path(self._tmp.name)
        rv.invalidate_cache()

    def tearDown(self):
        rv.REPO_ROOT = self._original_root
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _write_identity(self, agent, content):
        d = rv.REPO_ROOT / 'agents' / agent
        d.mkdir(parents=True, exist_ok=True)
        (d / 'IDENTITY.md').write_text(content)

    def test_missing_identity_is_passthrough(self):
        # No IDENTITY.md → no constraints → target unchanged.
        target, rerouted, reason = rv.validate_inbox_write(
            'beacon', {'source': 'pulse', 'prompt': 'hi'},
        )
        self.assertEqual(target, 'beacon')
        self.assertFalse(rerouted)
        self.assertIsNone(reason)

    def test_identity_without_constraints_section_is_passthrough(self):
        self._write_identity('beacon', '# Identity\n\n## About\n\nBeacon does X.\n')
        target, rerouted, _ = rv.validate_inbox_write(
            'beacon', {'source': 'pulse', 'prompt': 'hi'},
        )
        self.assertEqual(target, 'beacon')
        self.assertFalse(rerouted)

    def test_accepts_from_user_false_reroutes_dm(self):
        self._write_identity(
            'beacon',
            '# Identity\n\n## Routing Constraints\n\n'
            '- accepts_from_user: false\n'
            '- escalation_target: forge\n',
        )
        target, rerouted, reason = rv.validate_inbox_write(
            'beacon',
            {'source': 'telegram-webhook', 'reply_chat_id': 12345, 'prompt': 'hi'},
        )
        self.assertEqual(target, 'forge')
        self.assertTrue(rerouted)
        self.assertIn('accepts_from_user=false', reason)

    def test_group_chat_not_treated_as_dm(self):
        self._write_identity(
            'beacon',
            '# Identity\n\n## Routing Constraints\n\n- accepts_from_user: false\n',
        )
        target, rerouted, _ = rv.validate_inbox_write(
            'beacon',
            {'source': 'telegram-webhook', 'reply_chat_id': -100, 'prompt': 'hi'},
        )
        self.assertEqual(target, 'beacon')
        self.assertFalse(rerouted)


class CheckTargetRepoTest(unittest.TestCase):
    """Phase D3 commit 4b: allowed_repos gating on target_repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp.name)
        self._original_root = rv.REPO_ROOT
        self._original_models_path = rv.MODELS_CONFIG_PATH
        rv.REPO_ROOT = tmp_root
        rv.MODELS_CONFIG_PATH = tmp_root / 'config' / 'agent-models.json'
        rv.invalidate_models_cache()

    def tearDown(self):
        rv.REPO_ROOT = self._original_root
        rv.MODELS_CONFIG_PATH = self._original_models_path
        rv.invalidate_models_cache()
        self._tmp.cleanup()

    def _write_models(self, content):
        rv.MODELS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rv.MODELS_CONFIG_PATH.write_text(content)
        rv.invalidate_models_cache()

    def test_target_repo_in_allowed_passes(self):
        self._write_models(
            '{"agents": {"forge": {"allowed_repos": ["repo-a", "repo-b"]}}}'
        )
        ok, reason = rv.check_target_repo('forge', 'repo-a')
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_target_repo_not_in_allowed_denied(self):
        self._write_models(
            '{"agents": {"forge": {"allowed_repos": ["repo-a"]}}}'
        )
        ok, reason = rv.check_target_repo('forge', 'sneaky-repo')
        self.assertFalse(ok)
        self.assertIn('sneaky-repo', reason)
        self.assertIn('allowed_repos', reason)

    def test_target_repo_none_passes(self):
        # Legacy / non-worktree tasks have no target_repo.
        self._write_models(
            '{"agents": {"forge": {"allowed_repos": ["repo-a"]}}}'
        )
        ok, _ = rv.check_target_repo('forge', None)
        self.assertTrue(ok)
        ok, _ = rv.check_target_repo('forge', '')
        self.assertTrue(ok)

    def test_no_allowed_repos_configured_passes(self):
        # Agent has no allowed_repos field → no gating.
        self._write_models(
            '{"agents": {"beacon": {"inbox_model": "x"}}}'
        )
        ok, _ = rv.check_target_repo('beacon', 'whatever-repo')
        self.assertTrue(ok)

    def test_missing_models_file_passes(self):
        # No agent-models.json at all → no gating (back-compat).
        rv.invalidate_models_cache()
        ok, _ = rv.check_target_repo('forge', 'whatever')
        self.assertTrue(ok)

    def test_malformed_models_file_passes(self):
        self._write_models('{not valid json')
        ok, _ = rv.check_target_repo('forge', 'whatever')
        self.assertTrue(ok)

    def test_non_list_allowed_repos_passes(self):
        # Defensive: if someone sets allowed_repos to a string by mistake,
        # treat as no-config rather than crash.
        self._write_models(
            '{"agents": {"forge": {"allowed_repos": "repo-a"}}}'
        )
        ok, _ = rv.check_target_repo('forge', 'repo-a')
        self.assertTrue(ok)

    def test_allowed_repos_for_helper(self):
        self._write_models(
            '{"agents": {"forge": {"allowed_repos": ["x", "y"]}}}'
        )
        self.assertEqual(rv.allowed_repos_for('forge'), ['x', 'y'])
        self.assertEqual(rv.allowed_repos_for('mirror'), [])

    def test_cache_invalidates(self):
        self._write_models(
            '{"agents": {"forge": {"allowed_repos": ["repo-a"]}}}'
        )
        ok, _ = rv.check_target_repo('forge', 'repo-a')
        self.assertTrue(ok)
        # Change config; without invalidation we'd see old data.
        self._write_models(
            '{"agents": {"forge": {"allowed_repos": ["repo-b"]}}}'
        )
        ok, _ = rv.check_target_repo('forge', 'repo-a')
        self.assertFalse(ok)


class ValidateRouteTest(unittest.TestCase):
    """Combined entry point — hard topology raises, soft reroute returns."""

    def test_allowed_route_passes_through(self):
        final, rerouted, _ = rv.validate_route(
            'beacon', {'source': 'pulse', 'prompt': 'hi'},
        )
        self.assertEqual(final, 'beacon')
        self.assertFalse(rerouted)

    def test_denied_route_raises(self):
        with self.assertRaises(rv.RoutingDenied):
            rv.validate_route('forge', {'source': 'pulse', 'prompt': 'hi'})

    def test_dialogue_leg_routes_freely(self):
        # beacon-clarification can target any agent (dialogue suffix bypass)
        final, _, _ = rv.validate_route(
            'forge', {'source': 'beacon-clarification', 'prompt': 'answer'},
        )
        self.assertEqual(final, 'forge')


class CanonicalRepoTests(unittest.TestCase):
    """origin.repo arrives spelled however the emitting machine's checkout dir is
    named. canonical_repo collapses those spellings WITHOUT widening authorization."""

    def test_bare_checkout_name_canonicalizes(self):
        # THE BUG: droplet checkout dir is `agent-core`, so every droplet-emitted
        # capture carried a target_repo that failed the allowed_repos scope check.
        self.assertEqual(rv.canonical_repo('agent-core'), 'ourliberty-agent-core')

    def test_already_canonical_is_unchanged(self):
        self.assertEqual(
            rv.canonical_repo('ourliberty-agent-core'), 'ourliberty-agent-core')

    def test_owner_qualified_form(self):
        self.assertEqual(
            rv.canonical_repo('Larry-Yatch/ourliberty-agent-core'),
            'ourliberty-agent-core')

    def test_worktree_suffix_stripped(self):
        self.assertEqual(
            rv.canonical_repo('agent-core.wt-forge-task-001'), 'ourliberty-agent-core')

    def test_repo_without_namespace_prefix_survives(self):
        # RSDPM has no `ourliberty-` prefix; naive prefix logic would mangle it.
        self.assertEqual(rv.canonical_repo('RSDPM'), 'RSDPM')

    def test_unknown_repo_returned_unchanged(self):
        # Must NOT invent a repo. Spelling normalizer, not an authorizer.
        self.assertEqual(
            rv.canonical_repo('some-unrelated-repo'), 'some-unrelated-repo')

    def test_does_not_widen_authorization(self):
        # Canonicalizing an unknown name must not make it pass the scope check.
        ok, _ = rv.check_target_repo('forge', rv.canonical_repo('some-unrelated-repo'))
        self.assertFalse(ok)

    def test_falsy_and_non_string_inputs_pass_through(self):
        for value in (None, '', 0, [], {}):
            self.assertEqual(rv.canonical_repo(value), value)


if __name__ == '__main__':
    unittest.main()
