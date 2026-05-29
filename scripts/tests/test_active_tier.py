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
        # Payload fields round-trip verbatim. read() also merges in the
        # cooldown defaults (added § 6.3) so the asserted shape is a
        # superset rather than equality.
        for key, value in payload.items():
            self.assertEqual(state[key], value)
        self.assertEqual(state.get('cooldowns'), {})
        self.assertEqual(state.get('cooldown_backoff'), {})

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


class ActiveTierCooldownTest(unittest.TestCase):
    """Cover the per-tier rate-limit cooldown helpers added in spec § 6.3.

    The cooldown gate is what the watcher reads to skip dispatching to a
    rate-limited account; agent_runner is what writes it after a rate_limit
    failure where no tier switch happened. Both ends must round-trip via
    these helpers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import os
        self._prev = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)

    def tearDown(self):
        import os
        if self._prev is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev
        self.tmp.cleanup()

    # ---- parse_reset_time -----------------------------------------------

    def test_parse_resets_3_30pm_returns_future_utc(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        parsed = active_tier.parse_reset_time('hit your limit, resets 3:30pm',
                                              now=now)
        # 15:30 UTC same day → 3.5h later, within the [5min, 5h] bounds
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.hour, 15)
        self.assertEqual(parsed.minute, 30)

    def test_parse_resets_already_past_rolls_to_next_day_but_drops_oob(self):
        # If "resets 3:30pm" but it's currently 4pm UTC, the next occurrence
        # is tomorrow at 15:30 → ~23.5h out → out-of-bounds → None.
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 16, 0, 0, tzinfo=timezone.utc)
        parsed = active_tier.parse_reset_time('resets 3:30pm', now=now)
        self.assertIsNone(parsed)

    def test_parse_unparseable_returns_none(self):
        for raw in ('', 'no reset here', 'resets soon', 'resets at midnight',
                    None, 12345):
            self.assertIsNone(active_tier.parse_reset_time(raw))

    def test_parse_24h_format(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        parsed = active_tier.parse_reset_time('5-hour limit; resets 14:00',
                                              now=now)
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.hour, parsed.minute), (14, 0))

    # ---- set_cooldown ---------------------------------------------------

    def test_set_cooldown_with_parseable_reset_uses_parsed_until(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        until_iso = active_tier.set_cooldown(
            'tier1', raw_excerpt='resets 3pm', now=now,
        )
        # 15:00 UTC same day
        self.assertTrue(until_iso.startswith('2026-05-28T15:00:00'))
        # No backoff counter consumed when parse succeeds
        state = active_tier.read()
        self.assertNotIn('tier1', state['cooldown_backoff'])

    def test_set_cooldown_unparseable_uses_capped_backoff(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        # 1st unparseable rate_limit → 1 min cooldown
        active_tier.set_cooldown('tier1', raw_excerpt='no resets phrase',
                                 now=now)
        state = active_tier.read()
        self.assertEqual(state['cooldown_backoff']['tier1'], 1)
        from datetime import datetime as _dt
        until_dt = _dt.fromisoformat(state['cooldowns']['tier1'])
        self.assertEqual((until_dt - now).total_seconds(), 60.0)

        # 2nd → 2 min
        active_tier.set_cooldown('tier1', raw_excerpt='still nothing', now=now)
        state = active_tier.read()
        self.assertEqual(state['cooldown_backoff']['tier1'], 2)
        until_dt = _dt.fromisoformat(state['cooldowns']['tier1'])
        self.assertEqual((until_dt - now).total_seconds(), 120.0)

        # Many — capped at 30 min
        for _ in range(20):
            active_tier.set_cooldown('tier1', raw_excerpt='still nothing',
                                     now=now)
        state = active_tier.read()
        until_dt = _dt.fromisoformat(state['cooldowns']['tier1'])
        self.assertLessEqual((until_dt - now).total_seconds(), 30 * 60)
        self.assertGreaterEqual((until_dt - now).total_seconds(), 30 * 60 - 1)

    def test_set_cooldown_parseable_resets_backoff_counter(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        # Build up some backoff
        active_tier.set_cooldown('tier1', raw_excerpt='unparseable', now=now)
        active_tier.set_cooldown('tier1', raw_excerpt='unparseable', now=now)
        self.assertEqual(active_tier.read()['cooldown_backoff']['tier1'], 2)
        # A subsequent parseable rate_limit clears the counter for that tier
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=now)
        self.assertNotIn('tier1', active_tier.read()['cooldown_backoff'])

    def test_set_cooldown_rejects_invalid_tier(self):
        with self.assertRaises(ValueError):
            active_tier.set_cooldown('bogus', raw_excerpt='resets 3pm')

    # ---- cooldown_until / clear_cooldown --------------------------------

    def test_cooldown_until_returns_none_when_no_cooldown(self):
        self.assertIsNone(active_tier.cooldown_until('tier1'))

    def test_cooldown_until_returns_iso_when_active(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=now)
        # Read at a time before expiry
        check = active_tier.cooldown_until('tier1', now=now)
        self.assertTrue(check.startswith('2026-05-28T15:00:00'))

    def test_cooldown_clears_at_expiry(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=now)
        # Past the expiry: 15:00 + 1s
        after = datetime(2026, 5, 28, 15, 0, 1, tzinfo=timezone.utc)
        self.assertIsNone(active_tier.cooldown_until('tier1', now=after))

    def test_cooldown_only_on_specified_tier(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=now)
        # Tier 2 still uncooled
        self.assertIsNone(active_tier.cooldown_until('tier2', now=now))

    def test_clear_cooldown_drops_state(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        active_tier.set_cooldown('tier1', raw_excerpt='unparseable', now=now)
        active_tier.clear_cooldown('tier1')
        state = active_tier.read()
        self.assertNotIn('tier1', state['cooldowns'])
        self.assertNotIn('tier1', state['cooldown_backoff'])

    # ---- set_next_switch_due --------------------------------------------

    def test_set_next_switch_due_round_trip(self):
        from datetime import datetime, timezone
        when = datetime(2026, 5, 28, 14, 0, 0, tzinfo=timezone.utc)
        active_tier.set_next_switch_due(when)
        state = active_tier.read()
        self.assertEqual(state['next_switch_due'],
                         '2026-05-28T14:00:00+00:00')

    def test_set_next_switch_due_none_clears(self):
        from datetime import datetime, timezone
        when = datetime(2026, 5, 28, 14, 0, 0, tzinfo=timezone.utc)
        active_tier.set_next_switch_due(when)
        active_tier.set_next_switch_due(None)
        self.assertIsNone(active_tier.read()['next_switch_due'])


if __name__ == '__main__':
    unittest.main()
