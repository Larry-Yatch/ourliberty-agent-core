#!/usr/bin/env python3
"""Tests for scripts/active_tier.py — the OAuth-account rotation state
helper.

Covers the contract from ``agents/beacon/specs/account-rotation.md`` § 6.2:

  - Missing / corrupt state must default to tier1 (today's behavior must
    never regress on a parse failure).
  - ``current_home()`` / ``other_home()`` map the tier value to the two
    on-disk HOME directories.
  - ``set_tier()`` / ``set_draining()`` round-trip cleanly through the
    state file.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_active_tier
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import active_tier  # noqa: E402


class ActiveTierStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Module reads OURLIBERTY_AGENTS_ROOT at call time, so setting it
        # in setUp via monkeypatch isn't necessary; we use the env var
        # directly and clean up in tearDown.
        self._prev = sys.modules.get('os').environ.get('OURLIBERTY_AGENTS_ROOT')
        import os
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)

    def tearDown(self):
        import os
        if self._prev is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev
        self.tmp.cleanup()

    # ---- read() default semantics --------------------------------------

    def test_missing_state_returns_tier1_default(self):
        # No file at all → today's behavior must be preserved
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')
        self.assertFalse(state['draining'])
        self.assertIsNone(state['since'])
        self.assertIsNone(state['next_switch_due'])

    def test_corrupt_state_returns_tier1_default(self):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not valid json {{{')
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier1')

    def test_non_dict_payload_returns_default(self):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        # A bare JSON array is valid JSON but the wrong shape — must not
        # KeyError downstream.
        path.write_text(json.dumps(['tier1']))
        self.assertEqual(active_tier.read()['tier'], 'tier1')

    def test_unknown_tier_value_returns_default(self):
        # Defensive: an unrecognized tier value (e.g., a future "tier3"
        # written by a newer scheduler running against an older runner)
        # falls back to tier1 rather than authenticating against an
        # unknown HOME.
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': 'tier-bogus'}))
        self.assertEqual(active_tier.read()['tier'], 'tier1')

    def test_well_formed_tier1_state_returned_verbatim(self):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'tier': 'tier1',
            'since': '2026-05-28T12:00:00+00:00',
            'next_switch_due': '2026-05-28T14:00:00+00:00',
            'draining': True,
        }
        path.write_text(json.dumps(payload))
        state = active_tier.read()
        self.assertEqual(state, payload)

    def test_well_formed_tier2_state_returned_verbatim(self):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'tier': 'tier2', 'since': None,
                   'next_switch_due': None, 'draining': False}
        path.write_text(json.dumps(payload))
        self.assertEqual(active_tier.read()['tier'], 'tier2')

    # ---- current_home() / other_home() mapping -------------------------

    def test_current_home_tier1_returns_slash_home_larry(self):
        # Default state — today's behavior; the inherited HOME on the
        # droplet is /home/larry, which the helper must echo back.
        self.assertEqual(active_tier.current_home(), '/home/larry')

    def test_other_home_tier1_returns_personal(self):
        self.assertEqual(
            active_tier.other_home(),
            '/home/larry/.claude-larry-personal',
        )

    def test_current_home_tier2_returns_personal(self):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': 'tier2'}))
        self.assertEqual(
            active_tier.current_home(),
            '/home/larry/.claude-larry-personal',
        )

    def test_other_home_tier2_returns_slash_home_larry(self):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': 'tier2'}))
        self.assertEqual(active_tier.other_home(), '/home/larry')

    def test_missing_state_current_home_is_tier1(self):
        # Acceptance criterion: missing state → tier1 → /home/larry. The
        # primary subprocess HOME on a freshly-deployed droplet (no state
        # file written yet) must be the system account home, not whatever
        # the orchestrator inherited.
        self.assertEqual(active_tier.current_home(), '/home/larry')

    # ---- set_tier() / set_draining() round-trip ------------------------

    def test_set_tier_round_trip(self):
        active_tier.set_tier('tier2')
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier2')
        self.assertIsNotNone(state['since'])
        # Round-trip back
        active_tier.set_tier('tier1')
        self.assertEqual(active_tier.read()['tier'], 'tier1')

    def test_set_tier_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            active_tier.set_tier('tier-bogus')

    def test_set_draining_preserves_other_fields(self):
        active_tier.set_tier('tier2')
        active_tier.set_draining(True)
        state = active_tier.read()
        self.assertEqual(state['tier'], 'tier2')
        self.assertTrue(state['draining'])
        active_tier.set_draining(False)
        self.assertFalse(active_tier.read()['draining'])


if __name__ == '__main__':
    unittest.main()
