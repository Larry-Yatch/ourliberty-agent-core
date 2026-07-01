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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class AuthCooldownTest(unittest.TestCase):
    """Step A rotation fix: ``set_cooldown(kind='auth_401')`` parks a tier
    on a fixed 30-min window without consuming the rate-limit backoff
    counter. The 2026-05-29 storm proved an auth-401 with no cooldown
    storms; this branch is the circuit-breaker."""

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

    def test_auth_401_kind_uses_fixed_30min_window(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        until_iso = active_tier.set_cooldown(
            'tier2',
            raw_excerpt='Invalid authentication credentials',
            now=now,
            kind='auth_401',
        )
        # Fixed 30-min cooldown; the auth-401 message body is ignored on
        # purpose — auth messages don't carry a reset time.
        from datetime import datetime as _dt
        until_dt = _dt.fromisoformat(until_iso)
        self.assertEqual(until_dt - now, timedelta(minutes=30))

    def test_auth_401_does_not_touch_rate_limit_backoff_counter(self):
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        # Seed the rate-limit backoff for tier2 with two unparseable
        # rate_limits → counter at 2.
        active_tier.set_cooldown('tier2', raw_excerpt='nope', now=now)
        active_tier.set_cooldown('tier2', raw_excerpt='nope', now=now)
        self.assertEqual(active_tier.read()['cooldown_backoff']['tier2'], 2)
        # An auth_401 cooldown must NOT consume or reset that counter —
        # auth + rate-limit streaks are independent signals.
        active_tier.set_cooldown('tier2', raw_excerpt='auth',
                                 now=now, kind='auth_401')
        self.assertEqual(active_tier.read()['cooldown_backoff']['tier2'], 2)

    def test_auth_401_does_not_shorten_longer_rate_limit_cooldown(self):
        # MONOTONIC (spec § 7): a cooldown is extend-only — it is never
        # shortened. A tier rate-limited until 3pm (+3h) that then also fails
        # auth (+30m) stays benched until 3pm: it is unusable for the rate
        # limit regardless of auth, and a fresh shorter event must not
        # prematurely un-bench a still-walled tier. (Pre-§7 this overwrote to
        # 30m — the REQUIRED-FIX bug.)
        from datetime import datetime, timezone
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        active_tier.set_cooldown('tier2', raw_excerpt='resets 3pm', now=now)
        active_tier.set_cooldown('tier2', raw_excerpt='auth',
                                 now=now, kind='auth_401')
        state = active_tier.read()
        from datetime import datetime as _dt, timedelta
        until_dt = _dt.fromisoformat(state['cooldowns']['tier2'])
        self.assertEqual(until_dt - now, timedelta(hours=3))

    def test_auth_401_rejects_invalid_tier(self):
        with self.assertRaises(ValueError):
            active_tier.set_cooldown('bogus', kind='auth_401')


class TierAuthOkTest(unittest.TestCase):
    """Step A rotation fix + 2026-05-30 setup-token-awareness: pre-engage
    auth gate.

    ``tier_auth_ok`` mirrors ``agent_runner._apply_tier_auth`` precedence:
    if a tier's setup-token env var (CLAUDE_CODE_OAUTH_TOKEN_TIER{1,2})
    is non-empty, the gate returns True (that token is the live dispatch
    auth — presence is the signal). Only when the setup-token is
    unconfigured does the gate fall back to the legacy
    ``credentials.json.claudeAiOauth.expiresAt`` >= 10-min-future check.

    The existing legacy-fallback test cases below run with setup-token
    env cleared in setUp so they continue to exercise the credentials.json
    path byte-for-byte. New ``SetupTokenShortCircuit*`` cases at the end
    cover the post-fix short-circuit precedence."""

    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Stand up a fake home pair under the tmp dir and redirect
        # active_tier's module-level constants. Tests restore them in
        # tearDown so the rest of the suite sees the real production paths.
        self._prev_tier1_home = active_tier.TIER1_HOME
        self._prev_tier2_home = active_tier.TIER2_HOME
        self.t1_home = self.root / 'tier1-home'
        self.t2_home = self.root / 'tier2-home'
        self.t1_home.mkdir(parents=True)
        self.t2_home.mkdir(parents=True)
        active_tier.TIER1_HOME = str(self.t1_home)
        active_tier.TIER2_HOME = str(self.t2_home)
        # Clear setup-token env vars so the existing legacy cases below
        # exercise the credentials.json fallback unchanged. Tests that
        # need a setup-token set it explicitly via os.environ; tearDown
        # restores any prior real value so the dev box's tokens aren't
        # clobbered.
        self._prev_t1 = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER1', None)
        self._prev_t2 = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        # Point the durable-token file fallback at a nonexistent path so these
        # legacy credentials.json cases are isolated from the real
        # ~/credentials/.env.larry on the dev box / droplet — which
        # _setup_token_for_tier now reads when the env var is unset.
        self._prev_env_file = os.environ.get('OURLIBERTY_CREDENTIALS_ENV_FILE')
        os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = str(
            self.root / 'no-such.env')

    def tearDown(self):
        import os
        active_tier.TIER1_HOME = self._prev_tier1_home
        active_tier.TIER2_HOME = self._prev_tier2_home
        for name, prev in (
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER1', self._prev_t1),
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER2', self._prev_t2),
            ('OURLIBERTY_CREDENTIALS_ENV_FILE', self._prev_env_file),
        ):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        self.tmp.cleanup()

    def _write_creds(self, home_path, expires_at_ms):
        creds_dir = home_path / '.claude'
        creds_dir.mkdir(parents=True, exist_ok=True)
        (creds_dir / '.credentials.json').write_text(json.dumps({
            'claudeAiOauth': {
                'accessToken': 'tok',
                'refreshToken': 'rtok',
                'expiresAt': expires_at_ms,
                'scopes': [],
                'subscriptionType': 'max',
            },
        }))

    def _ms(self, dt):
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def test_future_expiry_is_ok(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self._write_creds(self.t2_home,
                          self._ms(now + timedelta(hours=8)))
        self.assertTrue(active_tier.tier_auth_ok('tier2', now=now))

    def test_already_expired_is_not_ok(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self._write_creds(self.t2_home,
                          self._ms(now - timedelta(minutes=1)))
        self.assertFalse(active_tier.tier_auth_ok('tier2', now=now))

    def test_expiring_inside_lead_margin_is_not_ok(self):
        # _AUTH_EXPIRY_MIN_LEAD is 10 min; an expiry 5 min out leaves no
        # headroom for the CLI's auto-refresh and must NOT pass the gate.
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self._write_creds(self.t2_home,
                          self._ms(now + timedelta(minutes=5)))
        self.assertFalse(active_tier.tier_auth_ok('tier2', now=now))

    def test_missing_credentials_file_is_not_ok(self):
        # No file → not auth-ok. A target tier the runner can't even read
        # the credentials of must never be trusted as a switch target.
        from datetime import datetime, timezone
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(active_tier.tier_auth_ok('tier2', now=now))

    def test_corrupt_credentials_file_is_not_ok(self):
        creds = self.t2_home / '.claude' / '.credentials.json'
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text('{not valid json')
        from datetime import datetime, timezone
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(active_tier.tier_auth_ok('tier2', now=now))

    def test_missing_claudeAiOauth_block_is_not_ok(self):
        creds = self.t1_home / '.claude' / '.credentials.json'
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({'other_key': 'value'}))
        from datetime import datetime, timezone
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(active_tier.tier_auth_ok('tier1', now=now))

    def test_missing_expiresAt_is_not_ok(self):
        creds = self.t1_home / '.claude' / '.credentials.json'
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({
            'claudeAiOauth': {'accessToken': 'tok'},
        }))
        from datetime import datetime, timezone
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(active_tier.tier_auth_ok('tier1', now=now))

    def test_invalid_tier_returns_false(self):
        # No exception — defensive shape. A bogus tier value must never
        # promote into a "this target is fine" branch.
        self.assertFalse(active_tier.tier_auth_ok('tier-bogus'))

    # ---- setup-token short-circuit (2026-05-30) -----------------------

    def test_setup_token_present_short_circuits_with_no_credentials_file(self):
        # The credentials.json was never created for tier2 — under legacy
        # semantics this would be False. With a setup-token configured,
        # the gate must return True because the setup-token is the live
        # dispatch auth (presence = usability per agent_runner contract).
        import os
        from datetime import datetime, timezone
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'sk-ant-tier2-token-fake'
        self.assertTrue(active_tier.tier_auth_ok('tier2', now=now))

    def test_setup_token_present_short_circuits_with_expired_credentials(self):
        # The exact 2026-05-30 false-block shape: tier2's credentials.json
        # has lapsed (the setup-token path no longer exercises/refreshes
        # it) but the setup-token itself is still valid. The gate must
        # NOT false-block the flip.
        import os
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self._write_creds(
            self.t2_home, self._ms(now - timedelta(hours=16)),
        )
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'sk-ant-tier2-token-fake'
        self.assertTrue(active_tier.tier_auth_ok('tier2', now=now))

    def test_empty_setup_token_falls_back_to_credentials_check(self):
        # Empty string is misconfiguration, not an override — must be
        # treated as unset so the legacy expiresAt check runs. With an
        # expired credentials.json and an empty setup-token, the gate
        # returns False (preserving the defensive "can't verify → False"
        # posture).
        import os
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        self._write_creds(
            self.t2_home, self._ms(now - timedelta(minutes=1)),
        )
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = ''
        self.assertFalse(active_tier.tier_auth_ok('tier2', now=now))

    def test_setup_token_scoped_per_tier(self):
        # Tier1's setup-token presence must NOT promote tier2 to auth-ok.
        # The env-var mapping is per-tier; cross-tier leak would let a
        # tier1-only deployment falsely greenlight a tier2 flip.
        import os
        from datetime import datetime, timezone
        now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = 'sk-ant-tier1-token-fake'
        # tier2 has no setup-token AND no credentials.json — legacy path
        # returns False, and the tier1 token must not bleed across.
        self.assertFalse(active_tier.tier_auth_ok('tier2', now=now))
        # Symmetric: tier1 IS auth-ok via its own setup-token.
        self.assertTrue(active_tier.tier_auth_ok('tier1', now=now))


class SetupTokenFileFallbackTest(unittest.TestCase):
    """The durable-token file fallback (the fix for the recurring false
    "Tier 2 down"). When the setup-token env var is absent, _setup_token_for_tier
    must read it from the credentials EnvironmentFile so an env-less process
    (manual SSH check, run_*.sh debug run, ad-hoc healer) authenticates exactly
    like a systemd daemon — instead of falling through to the frozen-stale
    credentials.json and reporting a false "down"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_file = self.root / '.env.larry'
        self._prev = {
            k: os.environ.get(k) for k in (
                'CLAUDE_CODE_OAUTH_TOKEN_TIER1',
                'CLAUDE_CODE_OAUTH_TOKEN_TIER2',
                'OURLIBERTY_CREDENTIALS_ENV_FILE',
            )
        }
        os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER1', None)
        os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = str(self.env_file)

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_reads_token_from_file_when_env_absent(self):
        self.env_file.write_text(
            'CLAUDE_CODE_OAUTH_TOKEN_TIER2=sk-ant-file-token\n')
        self.assertEqual(
            active_tier._setup_token_for_tier('tier2'), 'sk-ant-file-token')

    def test_env_var_takes_precedence_over_file(self):
        # Hot path: a process that inherited the env never reads the file.
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'sk-ant-env-token'
        self.env_file.write_text(
            'CLAUDE_CODE_OAUTH_TOKEN_TIER2=sk-ant-file-token\n')
        self.assertEqual(
            active_tier._setup_token_for_tier('tier2'), 'sk-ant-env-token')

    def test_missing_file_returns_none(self):
        # No env file at all -> None (then the caller's credentials.json path).
        self.assertIsNone(active_tier._setup_token_for_tier('tier2'))

    def test_handles_quotes_and_export_prefix(self):
        self.env_file.write_text(
            'export CLAUDE_CODE_OAUTH_TOKEN_TIER1="sk-ant-quoted"\n')
        self.assertEqual(
            active_tier._setup_token_for_tier('tier1'), 'sk-ant-quoted')

    def test_empty_value_in_file_is_treated_as_unset(self):
        self.env_file.write_text('CLAUDE_CODE_OAUTH_TOKEN_TIER2=\n')
        self.assertIsNone(active_tier._setup_token_for_tier('tier2'))

    def test_only_matching_tier_line_is_read(self):
        self.env_file.write_text(
            'OTHER_VAR=nope\n'
            'CLAUDE_CODE_OAUTH_TOKEN_TIER1=sk-ant-t1\n'
            'CLAUDE_CODE_OAUTH_TOKEN_TIER2=sk-ant-t2\n')
        self.assertEqual(
            active_tier._setup_token_for_tier('tier1'), 'sk-ant-t1')
        self.assertEqual(
            active_tier._setup_token_for_tier('tier2'), 'sk-ant-t2')

    def test_non_utf8_file_degrades_to_none_not_crash(self):
        # A binary / partial-write / non-UTF-8 credentials file must NOT crash
        # the auth gate (read_text would raise UnicodeDecodeError, which is not
        # an OSError) — it must degrade to a clean miss.
        self.env_file.write_bytes(b'CLAUDE_CODE_OAUTH_TOKEN_TIER2=\xff\xfe\x00bad\n')
        self.assertIsNone(active_tier._setup_token_for_tier('tier2'))

    def test_tier_auth_ok_true_from_file_token_without_credentials_json(self):
        # The end-to-end fix: no env var, no credentials.json, but the durable
        # token is in the file -> tier_auth_ok is True (was a false "down").
        from datetime import datetime, timezone
        now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
        self.env_file.write_text(
            'CLAUDE_CODE_OAUTH_TOKEN_TIER2=sk-ant-file-token\n')
        self.assertTrue(active_tier.tier_auth_ok('tier2', now=now))


class ActiveSetupTokenTest(unittest.TestCase):
    """active_setup_token() resolves the *active* tier (blackboard/
    active-tier.json) then that tier's setup-token. This is what run_cycle.sh
    uses so the /cycle heartbeat authenticates against the SAME account the
    team does — following a dashboard switch / rotation — instead of pinning to
    HOME's auto-refreshing ~/.claude/.credentials.json."""

    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # active-tier.json lives under OURLIBERTY_AGENTS_ROOT/blackboard.
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        # Control the setup-token env vars + disable the on-disk env-file
        # fallback so the dev box's real ~/credentials/.env.larry can't leak in.
        self._prev_t1 = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER1', None)
        self._prev_t2 = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        self._prev_env_file = os.environ.get('OURLIBERTY_CREDENTIALS_ENV_FILE')
        os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = str(
            self.root / 'no-such.env')

    def tearDown(self):
        import os
        for name, prev in (
            ('OURLIBERTY_AGENTS_ROOT', self._prev_root),
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER1', self._prev_t1),
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER2', self._prev_t2),
            ('OURLIBERTY_CREDENTIALS_ENV_FILE', self._prev_env_file),
        ):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        self.tmp.cleanup()

    def _set_active_tier(self, tier):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': tier}))

    def test_returns_active_tier2_token(self):
        import os
        self._set_active_tier('tier2')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'sk-tier2'
        self.assertEqual(active_tier.active_setup_token(), 'sk-tier2')

    def test_follows_active_tier_to_tier1(self):
        import os
        # Active tier is tier1 → return tier1's token even though tier2's is set.
        self._set_active_tier('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = 'sk-tier1'
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'sk-tier2'
        self.assertEqual(active_tier.active_setup_token(), 'sk-tier1')

    def test_none_when_unconfigured(self):
        # Active tier set, but no token env var + env-file fallback missing →
        # None, so run_cycle.sh cleanly falls back to credentials.json.
        self._set_active_tier('tier2')
        self.assertIsNone(active_tier.active_setup_token())

    def test_missing_state_defaults_to_tier1_token(self):
        import os
        # No active-tier.json → read() defaults to tier1; resolve tier1's token.
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = 'sk-tier1-default'
        self.assertEqual(active_tier.active_setup_token(), 'sk-tier1-default')


class DurableClaudeEnvTest(unittest.TestCase):
    """W4 (spec §10-W4): select_durable_claude_env round-robins a tier per run
    and returns (env, tier); durable_claude_env is the back-compat wrapper.
    Patches select_dispatch_tier / _setup_token_for_tier so it's isolated from
    tier/env state."""

    def test_select_returns_env_and_tier_with_token(self):
        with mock.patch.object(active_tier, 'select_dispatch_tier',
                               return_value='tier3'), \
             mock.patch.object(active_tier, '_setup_token_for_tier',
                               return_value='sk-durable'):
            env, tier = active_tier.select_durable_claude_env({'PATH': '/bin'})
        self.assertEqual(tier, 'tier3')
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], 'sk-durable')
        self.assertEqual(env['HOME'], active_tier.TIER1_HOME)  # token = real home
        self.assertEqual(env['PATH'], '/bin')  # base preserved

    def test_none_when_no_tier_available(self):
        with mock.patch.object(active_tier, 'select_dispatch_tier',
                               return_value=None):
            env, tier = active_tier.select_durable_claude_env({'PATH': '/bin'})
        self.assertIsNone(env)   # caller MUST skip the run
        self.assertIsNone(tier)

    def test_never_raises_degrades_to_none(self):
        # A resolution error (e.g. malformed config surfacing through the
        # selector) must degrade to (None, None) so the generator takes its
        # graceful fallback, never crashes.
        with mock.patch.object(active_tier, 'select_dispatch_tier',
                               side_effect=RuntimeError('boom')):
            env, tier = active_tier.select_durable_claude_env({'PATH': '/bin'})
        self.assertIsNone(env)
        self.assertIsNone(tier)
        # The wrapper is likewise fail-safe (base env unchanged).
        with mock.patch.object(active_tier, 'select_dispatch_tier',
                               side_effect=RuntimeError('boom')):
            wrapped = active_tier.durable_claude_env({'PATH': '/bin'})
        self.assertEqual(wrapped['PATH'], '/bin')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', wrapped)

    def test_creds_fallback_swaps_home_and_pins_git(self):
        with mock.patch.object(active_tier, 'select_dispatch_tier',
                               return_value='tier2'), \
             mock.patch.object(active_tier, '_setup_token_for_tier',
                               return_value=None):
            env, tier = active_tier.select_durable_claude_env({'PATH': '/bin'})
        self.assertEqual(tier, 'tier2')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertEqual(env['HOME'], active_tier.home_for_tier('tier2'))
        self.assertIn('GH_CONFIG_DIR', env)
        self.assertIn('GIT_CONFIG_GLOBAL', env)

    def test_wrapper_sets_token_from_selected_tier(self):
        with mock.patch.object(active_tier, 'select_dispatch_tier',
                               return_value='tier1'), \
             mock.patch.object(active_tier, '_setup_token_for_tier',
                               return_value='sk-durable'):
            env = active_tier.durable_claude_env({'PATH': '/bin'})
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], 'sk-durable')
        self.assertEqual(env['PATH'], '/bin')

    def test_wrapper_noop_when_no_tier(self):
        # Back-compat: wrapper returns the base env unchanged (no token) so a
        # non-adopting caller still gets a usable env.
        with mock.patch.object(active_tier, 'select_dispatch_tier',
                               return_value=None):
            env = active_tier.durable_claude_env({'PATH': '/bin'})
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertEqual(env['PATH'], '/bin')

    def test_defaults_to_os_environ_copy(self):
        os.environ['OL_MARKER_DCE'] = '1'
        try:
            with mock.patch.object(active_tier, 'select_dispatch_tier',
                                   return_value='tier1'), \
                 mock.patch.object(active_tier, '_setup_token_for_tier',
                                   return_value='tok'):
                env = active_tier.durable_claude_env()
            self.assertEqual(env.get('OL_MARKER_DCE'), '1')
            env['OL_MARKER_DCE'] = '2'  # mutating the copy must not touch os.environ
            self.assertEqual(os.environ['OL_MARKER_DCE'], '1')
        finally:
            os.environ.pop('OL_MARKER_DCE', None)


if __name__ == '__main__':
    unittest.main()
