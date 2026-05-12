#!/usr/bin/env python3
"""Fixtures for `routing_validator` — hard topology + soft IDENTITY reroute.

Phase D3-prep (2026-05-11). Adapted from upstream's
`scripts/tests/test_routing_*` shape; the hard-topology layer is Larry-shaped
and unique to this fork.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_routing_validator
"""

from __future__ import annotations

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
            ('forge', 'mirror'),      # Review goes via Beacon, not direct
            ('mirror', 'forge'),      # Changes go via Beacon
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


if __name__ == '__main__':
    unittest.main()
