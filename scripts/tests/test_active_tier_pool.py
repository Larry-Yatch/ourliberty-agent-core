#!/usr/bin/env python3
"""Tests for the per-task tier-dispatch capacity pool added to
``scripts/active_tier.py`` (spec: docs/specs/tier-dispatch-spec.md).

Covers the selector engine — config fail-safe (§ 12), operator pin (§ 4 step 0
/ § 16), round-robin counter (§ 7), account-filtered burn (§ 8), near_cap +
reserve guard (§ 8), the durable session->tier map (§ 5 / I2), monotonic +
flocked cooldown (§ 7), and select_dispatch_tier's four paths (§ 4).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_active_tier_pool
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import active_tier  # noqa: E402

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class _PoolBase(unittest.TestCase):
    """Hermetic base: tmp agents-root, tmp homes, tier1/tier3 setup-tokens
    present (usable), tier2 absent (unusable) unless a test opts in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'blackboard').mkdir(parents=True, exist_ok=True)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)
        self._env = {}
        self._set_env('OURLIBERTY_AGENTS_ROOT', str(self.root))
        # No credentials env-file -> tier_auth falls to credentials.json (absent).
        self._set_env('OURLIBERTY_CREDENTIALS_ENV_FILE', str(self.root / 'noenv'))
        self._set_env('CLAUDE_CODE_OAUTH_TOKEN_TIER1', 'sk-ant-oat01-t1')
        self._set_env('CLAUDE_CODE_OAUTH_TOKEN_TIER3', 'sk-ant-oat01-t3')
        self._set_env('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)  # unset
        # Hermetic homes so tier2's credentials.json check can't see a real file.
        for attr, sub in (('TIER1_HOME', 'h1'), ('TIER2_HOME', 'h2'),
                          ('TIER3_HOME', 'h3')):
            p = self.root / sub
            p.mkdir(exist_ok=True)
            self._patch_attr(attr, str(p))
        # Default: config points at a tmp file with the canonical pool block.
        self._config_path = self.root / 'agent-models.json'
        self._write_config({
            'primary': ['tier1', 'tier3'], 'fallback': ['tier2'],
            'proactive_cap_fraction': 0.85, 'proactive_release_fraction': 0.70,
            't2_reserve_fraction': 0.25, 'max_5h_budget_tokens': 10_000_000,
            'hold_alert_minutes': 10,
        })
        self._cfg_patch = mock.patch.object(
            active_tier, '_config_file', lambda: self._config_path)
        self._cfg_patch.start()

    def tearDown(self):
        self._cfg_patch.stop()
        mock.patch.stopall()
        for k, prev in self._env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        self.tmp.cleanup()

    def _set_env(self, key, value):
        self._env[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _patch_attr(self, attr, value):
        p = mock.patch.object(active_tier, attr, value)
        p.start()

    def _write_config(self, pool_block):
        self._config_path.write_text(json.dumps({'tier_pool': pool_block}))

    def _write_costs(self, rows):
        path = self.root / 'blackboard' / 'costs.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

    def _cost_row(self, account, tokens, when=None):
        when = when or _NOW
        return {'ts': when.isoformat(), 'account': account,
                'input_tokens': tokens, 'output_tokens': 0, 'cache_creation': 0}


class ConfigFailSafeTest(_PoolBase):
    def test_canonical_block(self):
        cfg = active_tier._tier_pool_config()
        self.assertEqual(cfg['primary'], ['tier1', 'tier3'])
        self.assertEqual(cfg['fallback'], ['tier2'])
        self.assertEqual(cfg['max_5h_budget_tokens'], 10_000_000)

    def test_missing_file_degrades_to_tier1_only_no_nearcap(self):
        self._config_path.unlink()
        cfg = active_tier._tier_pool_config()
        self.assertEqual(cfg['primary'], ['tier1'])
        self.assertIsNone(cfg['max_5h_budget_tokens'])

    def test_malformed_json_degrades(self):
        self._config_path.write_text('{not json')
        cfg = active_tier._tier_pool_config()
        self.assertEqual(cfg['primary'], ['tier1'])

    def test_partial_block_coerced(self):
        self._write_config({'primary': 'tier1', 'proactive_cap_fraction': 'x'})
        cfg = active_tier._tier_pool_config()
        self.assertEqual(cfg['primary'], ['tier1'])  # non-list -> default
        self.assertEqual(cfg['proactive_cap_fraction'], 0.85)

    def test_invalid_tiers_filtered_at_config(self):
        # Bogus tiers are dropped IN the config reader so every consumer sees
        # clean lists (review fix).
        self._write_config({'primary': ['tier1', 'bogus', 'tier3'],
                            'fallback': ['nope', 'tier2']})
        cfg = active_tier._tier_pool_config()
        self.assertEqual(cfg['primary'], ['tier1', 'tier3'])
        self.assertEqual(cfg['fallback'], ['tier2'])

    def test_all_invalid_primary_falls_back_to_default(self):
        self._write_config({'primary': ['bogus', 'nope']})
        self.assertEqual(active_tier._tier_pool_config()['primary'], ['tier1'])


class OperatorPinTest(_PoolBase):
    def test_absent_is_none(self):
        self.assertIsNone(active_tier.read_operator_pin())

    def test_valid_tier_pins(self):
        (self.root / 'rotation.disabled').write_text('tier3\n')
        self.assertEqual(active_tier.read_operator_pin(), 'tier3')
        self.assertEqual(active_tier.select_dispatch_tier(), 'tier3')

    def test_empty_or_invalid_is_none(self):
        (self.root / 'rotation.disabled').write_text('')
        self.assertIsNone(active_tier.read_operator_pin())
        (self.root / 'rotation.disabled').write_text('garbage')
        self.assertIsNone(active_tier.read_operator_pin())

    def test_pin_overrides_session_and_pool(self):
        (self.root / 'rotation.disabled').write_text('tier1')
        # Even a benched tier1 is returned — pin is forced (rollback lever).
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=_NOW)
        self.assertEqual(active_tier.select_dispatch_tier(session_tier='tier3'),
                         'tier1')


class RoundRobinTest(_PoolBase):
    def test_alternates_and_persists_counter(self):
        picks = [active_tier.round_robin(['tier1', 'tier3']) for _ in range(4)]
        self.assertEqual(picks, ['tier1', 'tier3', 'tier1', 'tier3'])
        # Counter persisted to disk.
        data = json.loads((self.root / 'state' / 'tier-rr-counter').read_text())
        self.assertEqual(data['counter'], 4)

    def test_single_pool_no_counter_churn(self):
        self.assertEqual(active_tier.round_robin(['tier1']), 'tier1')
        self.assertFalse((self.root / 'state' / 'tier-rr-counter').exists())

    def test_empty_pool_none(self):
        self.assertIsNone(active_tier.round_robin([]))


class BurnReaderTest(_PoolBase):
    def test_account_filter_and_window(self):
        self._write_costs([
            self._cost_row('tier1', 100),
            self._cost_row('tier3', 5),
            self._cost_row('tier1', 50, when=_NOW - timedelta(hours=6)),  # old
            self._cost_row('tier1', 7, when=_NOW - timedelta(hours=1)),
        ])
        self.assertEqual(
            active_tier.rolling_5h_token_volume(account='tier1', now=_NOW), 107)
        self.assertEqual(
            active_tier.rolling_5h_token_volume(account='tier3', now=_NOW), 5)

    def test_sentinel_accounts_ignored(self):
        self._write_costs([
            self._cost_row('fixture', 999),
            self._cost_row('skipped', 999),
            self._cost_row(None, 999),
            self._cost_row('tier1', 3),
        ])
        self.assertEqual(
            active_tier.rolling_5h_token_volume(account='tier1', now=_NOW), 3)
        # Unfiltered total also excludes sentinels.
        self.assertEqual(active_tier.rolling_5h_token_volume(now=_NOW), 3)

    def test_fail_open_missing_and_corrupt(self):
        self.assertEqual(active_tier.rolling_5h_token_volume(account='tier1'), 0)
        (self.root / 'blackboard' / 'costs.jsonl').write_text('{bad\nalso bad')
        self.assertEqual(
            active_tier.rolling_5h_token_volume(account='tier1', now=_NOW), 0)


class NearCapReserveTest(_PoolBase):
    def test_nearcap_false_when_budget_none(self):
        self._config_path.unlink()  # degraded -> budget None
        self._write_costs([self._cost_row('tier1', 999_999_999)])
        self.assertFalse(active_tier.near_cap('tier1', now=_NOW))

    def test_nearcap_trips_above_cap_with_hysteresis(self):
        # cap = 0.85*10M = 8.5M; release = 0.70*10M = 7.0M.
        self._write_costs([self._cost_row('tier1', 9_000_000)])
        self.assertTrue(active_tier.near_cap('tier1', now=_NOW))
        # Drop to 7.5M (between release and cap): latched -> still True.
        self._write_costs([self._cost_row('tier1', 7_500_000)])
        self.assertTrue(active_tier.near_cap('tier1', now=_NOW))
        # Drop below release (6.9M): clears.
        self._write_costs([self._cost_row('tier1', 6_900_000)])
        self.assertFalse(active_tier.near_cap('tier1', now=_NOW))

    def test_nearcap_pure_read_does_not_write_latch(self):
        # update_latch=False must never mutate routing state (review fix:
        # an observability poll must not flip the hysteresis latch).
        self._write_costs([self._cost_row('tier1', 9_000_000)])
        self.assertTrue(active_tier.near_cap('tier1', now=_NOW,
                                             update_latch=False))
        self.assertFalse((self.root / 'state'
                          / 'tier-near-cap-latch.json').exists())
        # And tier_pool_status (which uses the pure read) leaves no latch file.
        active_tier.tier_pool_status(now=_NOW)
        self.assertFalse((self.root / 'state'
                          / 'tier-near-cap-latch.json').exists())

    def test_reserve_fails_safe_when_budget_none(self):
        # FAIL-SAFE (§8): with no budget to bound the spill, hold rather than
        # drain the laptop -> reserve NOT ok.
        self._config_path.unlink()
        self.assertFalse(active_tier.fallback_reserve_ok('tier2', now=_NOW))

    def test_reserve_guard(self):
        # reserve line = 0.25*10M = 2.5M.
        self._write_costs([self._cost_row('tier2', 2_000_000)])
        self.assertTrue(active_tier.fallback_reserve_ok('tier2', now=_NOW))
        self._write_costs([self._cost_row('tier2', 3_000_000)])
        self.assertFalse(active_tier.fallback_reserve_ok('tier2', now=_NOW))


class SessionMapTest(_PoolBase):
    def test_record_and_lookup(self):
        active_tier.record_session_tier('sess-A', 'tier3', now=_NOW)
        self.assertEqual(active_tier.lookup_session_tier('sess-A'), 'tier3')
        self.assertIsNone(active_tier.lookup_session_tier('unknown'))

    def test_invalid_inputs_noop(self):
        active_tier.record_session_tier('', 'tier1')
        active_tier.record_session_tier('s', 'tierX')
        self.assertIsNone(active_tier.lookup_session_tier('s'))

    def test_prune_stale(self):
        old = _NOW - timedelta(days=20)
        active_tier.record_session_tier('old', 'tier1', now=old)
        active_tier.record_session_tier('new', 'tier3', now=_NOW)
        self.assertIsNone(active_tier.lookup_session_tier('old'))
        self.assertEqual(active_tier.lookup_session_tier('new'), 'tier3')


class MonotonicCooldownTest(_PoolBase):
    def test_short_does_not_shorten_long(self):
        # Long parsed reset (~4h59 out via 24h "resets 16:59" is fragile);
        # use a controlled now and a parseable HH:MM ~4h ahead.
        long_until = active_tier.set_cooldown(
            'tier1', raw_excerpt='resets 3:30pm', now=_NOW)  # 15:30 -> +3h30
        # auth_401 = +30min, strictly shorter; must NOT replace the long bench.
        active_tier.set_cooldown('tier1', kind='auth_401', now=_NOW)
        self.assertEqual(active_tier.cooldown_until('tier1', now=_NOW), long_until)

    def test_longer_extends(self):
        active_tier.set_cooldown('tier1', kind='auth_401', now=_NOW)  # +30m
        longer = active_tier.set_cooldown(
            'tier1', raw_excerpt='resets 3:30pm', now=_NOW)  # +3h30
        self.assertEqual(active_tier.cooldown_until('tier1', now=_NOW), longer)


class SelectDispatchTierTest(_PoolBase):
    def test_path2_round_robin_healthy_primaries(self):
        picks = {active_tier.select_dispatch_tier(now=_NOW) for _ in range(6)}
        self.assertEqual(picks, {'tier1', 'tier3'})

    def test_session_binding_usable_and_benched(self):
        self.assertEqual(
            active_tier.select_dispatch_tier(session_tier='tier3', now=_NOW),
            'tier3')
        active_tier.set_cooldown('tier3', raw_excerpt='resets 3pm', now=_NOW)
        self.assertIsNone(
            active_tier.select_dispatch_tier(session_tier='tier3', now=_NOW))

    def test_one_primary_benched_uses_other(self):
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=_NOW)
        picks = {active_tier.select_dispatch_tier(now=_NOW) for _ in range(4)}
        self.assertEqual(picks, {'tier3'})

    def test_path4_emergency_fallback_under_reserve(self):
        # Make tier2 usable via its setup-token; bench both primaries.
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'sk-ant-oat01-t2'
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=_NOW)
        active_tier.set_cooldown('tier3', raw_excerpt='resets 3pm', now=_NOW)
        self.assertEqual(active_tier.select_dispatch_tier(now=_NOW), 'tier2')
        # Reserve exhausted -> hold (None).
        self._write_costs([self._cost_row('tier2', 3_000_000)])
        self.assertIsNone(active_tier.select_dispatch_tier(now=_NOW))

    def test_all_benched_holds(self):
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=_NOW)
        active_tier.set_cooldown('tier3', raw_excerpt='resets 3pm', now=_NOW)
        # tier2 has no token -> unusable; nothing available.
        self.assertIsNone(active_tier.select_dispatch_tier(now=_NOW))

    def test_near_cap_primary_still_used_when_no_healthy(self):
        # Both primaries near cap (path 2 empty) -> path 3 uses them anyway.
        self._write_costs([
            self._cost_row('tier1', 9_000_000),
            self._cost_row('tier3', 9_000_000),
        ])
        pick = active_tier.select_dispatch_tier(now=_NOW)
        self.assertIn(pick, {'tier1', 'tier3'})


class TierPoolStatusTest(_PoolBase):
    def test_status_snapshot(self):
        st = active_tier.tier_pool_status(now=_NOW)
        self.assertEqual(st['primary'], ['tier1', 'tier3'])
        self.assertTrue(st['tiers']['tier1']['usable'])
        self.assertFalse(st['tiers']['tier2']['usable'])
        self.assertIsNone(st['operator_pin'])


if __name__ == '__main__':
    unittest.main()
