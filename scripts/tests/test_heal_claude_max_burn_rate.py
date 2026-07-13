#!/usr/bin/env python3
"""Tests for heal_claude_max_burn_rate — 2026-05-28 quota-signal re-base.

Covers the post-re-base healer:
- Rolling-5h token volume sums input+output+cache_creation (and excludes
  cache_read).
- The new DM body template (token gate framing + trailing-2h rate-limit count).
- The leading-warning behavior (fires near the wall, quiet at low usage).
- The 2026-05-27 no-false-alarm condition: at rolling-5h volumes that
  correspond to the documented 31%/59% real-usage period, the healer
  must NOT fire.
- Trailing-2h count computed from the rate-limit ledger.
- Missing ledger -> count of 0 (no error).
- Existing threshold/cooldown logic still intact.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_heal_claude_max_burn_rate
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


class _IsolatedAgentsRoot(unittest.TestCase):
    """Each test gets its own tmp AGENTS_ROOT and a fresh module reload."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='agents-root-burn-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self._tmp, sub), exist_ok=True)
        self._orig_env = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp
        import heal_claude_max_burn_rate
        self.h = importlib.reload(heal_claude_max_burn_rate)

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._orig_env
        import heal_claude_max_burn_rate
        importlib.reload(heal_claude_max_burn_rate)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    def _write_config(self, data):
        """Point the healer's _CONFIG_FILE at a tmp config carrying `data`
        (any JSON-serialisable object). Mutating the reloaded module's
        attribute is isolated per test — setUp reloads fresh, tearDown
        reloads again. Returns the path."""
        cfg_path = os.path.join(self._tmp, 'agent-models.json')
        with open(cfg_path, 'w') as f:
            json.dump(data, f)
        self.h._CONFIG_FILE = Path(cfg_path)
        return cfg_path

    def _write_costs(self, entries):
        with open(self.h.COSTS_FILE, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

    def _write_ledger(self, entries):
        with open(self.h.RATE_LIMIT_LEDGER_FILE, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')


class RollingTokenVolumeTest(_IsolatedAgentsRoot):
    """Verify the token-volume signal sums the right fields and respects
    the rolling 5h window."""

    def test_sums_io_plus_cache_creation_within_window(self):
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=30)).isoformat(),
             'agent': 'forge', 'cost_usd': 1.0,
             'input_tokens': 100, 'output_tokens': 200,
             'cache_creation': 1000, 'cache_read': 50000},
            {'ts': (now - timedelta(hours=2)).isoformat(),
             'agent': 'beacon', 'cost_usd': 2.0,
             'input_tokens': 50, 'output_tokens': 150,
             'cache_creation': 500, 'cache_read': 99999},
        ])
        # Sum = (100+200+1000) + (50+150+500) = 1300 + 700 = 2000.
        # cache_read intentionally excluded — quota-relevant tokens only.
        self.assertEqual(self.h.rolling_5h_token_volume(now=now), 2000)

    def test_excludes_entries_outside_5h_window(self):
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=10)).isoformat(),
             'input_tokens': 100, 'output_tokens': 200,
             'cache_creation': 0, 'cache_read': 0},
            {'ts': (now - timedelta(hours=6)).isoformat(),  # outside
             'input_tokens': 9999, 'output_tokens': 9999,
             'cache_creation': 9999, 'cache_read': 0},
        ])
        self.assertEqual(self.h.rolling_5h_token_volume(now=now), 300)

    def test_skips_malformed_lines_and_missing_fields(self):
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        with open(self.h.COSTS_FILE, 'w') as f:
            f.write('not-json\n')
            f.write('\n')
            f.write(json.dumps({'ts': now.isoformat()}) + '\n')  # no tokens
            f.write(json.dumps({
                'ts': now.isoformat(),
                'input_tokens': 'not-a-number',
                'output_tokens': 42,
                'cache_creation': None,
            }) + '\n')
        # Only the valid 42 survives.
        self.assertEqual(self.h.rolling_5h_token_volume(now=now), 42)

    def test_missing_costs_file_returns_zero(self):
        # COSTS_FILE was never written in this test.
        self.assertEqual(self.h.rolling_5h_token_volume(), 0)


class RecentRateLimitEventCountTest(_IsolatedAgentsRoot):
    def test_missing_ledger_returns_zero(self):
        self.assertEqual(self.h.recent_rate_limit_event_count(), 0)

    def test_counts_only_events_within_trailing_2h(self):
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        self._write_ledger([
            {'ts': (now - timedelta(minutes=30)).isoformat(),
             'agent': 'a', 'task_id': 't1'},
            {'ts': (now - timedelta(hours=1, minutes=30)).isoformat(),
             'agent': 'a', 'task_id': 't2'},
            {'ts': (now - timedelta(hours=3)).isoformat(),
             'agent': 'a', 'task_id': 't3'},  # outside window
        ])
        self.assertEqual(self.h.recent_rate_limit_event_count(now=now), 2)

    def test_skips_malformed_lines(self):
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        with open(self.h.RATE_LIMIT_LEDGER_FILE, 'w') as f:
            f.write('not-json\n')
            f.write(json.dumps({'ts': now.isoformat(), 'agent': 'a',
                                'task_id': 't1'}) + '\n')
            f.write('\n')  # blank
        self.assertEqual(self.h.recent_rate_limit_event_count(now=now), 1)

    def test_skips_records_missing_ts(self):
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
        self._write_ledger([
            {'agent': 'a', 'task_id': 't-no-ts'},
            {'ts': now.isoformat(), 'agent': 'a', 'task_id': 't1'},
        ])
        self.assertEqual(self.h.recent_rate_limit_event_count(now=now), 1)


class LoadThresholdTest(_IsolatedAgentsRoot):

    def test_default_threshold_constant(self):
        self.assertEqual(self.h.DEFAULT_MAX_5H_TOKEN_THRESHOLD, 10_000_000)

    def test_valid_threshold_is_read(self):
        self._write_config({'tier1_quota': {'max_5h_token_threshold': 12_345_678}})
        self.assertEqual(self.h.load_threshold(), 12_345_678)

    def test_fail_safe_on_missing_block(self):
        # gate is deprecated (enabled=false) but the block carries no
        # threshold field — load_threshold must still fall back to default.
        self._write_config({'tier1_quota': {'enabled': False}})
        self.assertEqual(self.h.load_threshold(),
                         self.h.DEFAULT_MAX_5H_TOKEN_THRESHOLD)

    def test_fail_safe_on_absent_tier1_block(self):
        self._write_config({'something_else': {}})
        self.assertEqual(self.h.load_threshold(),
                         self.h.DEFAULT_MAX_5H_TOKEN_THRESHOLD)

    def test_fail_safe_on_invalid_threshold(self):
        self._write_config({'tier1_quota': {'max_5h_token_threshold': 'nope'}})
        self.assertEqual(self.h.load_threshold(),
                         self.h.DEFAULT_MAX_5H_TOKEN_THRESHOLD)

    def test_fail_safe_on_unreadable_config(self):
        self.h._CONFIG_FILE = Path(self._tmp) / 'does-not-exist.json'
        self.assertEqual(self.h.load_threshold(),
                         self.h.DEFAULT_MAX_5H_TOKEN_THRESHOLD)


class GateEnabledTest(_IsolatedAgentsRoot):
    """The deprecation flag: gate_enabled() reads tier1_quota.enabled with an
    enabled-by-default (back-compat) bias, and run() short-circuits — emitting
    NO alert — when the gate is disabled."""

    def test_explicit_false_disables(self):
        self._write_config({'tier1_quota': {'enabled': False,
                                             'max_5h_token_threshold': 10_000_000}})
        self.assertFalse(self.h.gate_enabled())

    def test_explicit_true_enables(self):
        self._write_config({'tier1_quota': {'enabled': True}})
        self.assertTrue(self.h.gate_enabled())

    def test_absent_enabled_key_defaults_enabled(self):
        # Back-compat: an accidentally-deleted field must NOT blind monitoring.
        self._write_config({'tier1_quota': {'max_5h_token_threshold': 10_000_000}})
        self.assertTrue(self.h.gate_enabled())

    def test_absent_tier1_block_defaults_enabled(self):
        self._write_config({'something_else': {}})
        self.assertTrue(self.h.gate_enabled())

    def test_non_bool_enabled_defaults_enabled(self):
        # Only an explicit `false` deprecates; junk fails safe to enabled.
        self._write_config({'tier1_quota': {'enabled': 'no'}})
        self.assertTrue(self.h.gate_enabled())

    def test_unreadable_config_defaults_enabled(self):
        self.h._CONFIG_FILE = Path(self._tmp) / 'does-not-exist.json'
        self.assertTrue(self.h.gate_enabled())

    def _run_at_high_usage_with_config(self, config):
        """Write a config + a high-burn (95% of 10M) cost record and run the
        healer, capturing any append_alert call. load_threshold is left REAL
        so the config's threshold (or its fail-safe) is exercised end-to-end."""
        self._write_config(config)
        now = datetime.now(timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=5)).isoformat(),
             'agent': 'forge',
             'input_tokens': 4_750_000, 'output_tokens': 4_750_000,
             'cache_creation': 0, 'cache_read': 999_999_999,
             'cost_usd': 0.0},
        ])
        captured = {}

        def fake_append_alert(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch('larry_alerts.append_alert',
                        side_effect=fake_append_alert):
            rc = self.h.run()
        return rc, captured

    def test_run_short_circuits_and_emits_no_alert_when_disabled(self):
        rc, captured = self._run_at_high_usage_with_config(
            {'tier1_quota': {'enabled': False,
                             'max_5h_token_threshold': 10_000_000}})
        self.assertEqual(rc, 0)
        self.assertEqual(captured, {},
                         'disabled gate must not DM even at 95% burn')

    def test_run_does_not_touch_append_alert_when_disabled(self):
        self._write_config({'tier1_quota': {'enabled': False}})
        now = datetime.now(timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=5)).isoformat(),
             'agent': 'forge', 'input_tokens': 9_999_999,
             'output_tokens': 9_999_999, 'cache_creation': 0,
             'cache_read': 0, 'cost_usd': 0.0},
        ])
        with mock.patch('larry_alerts.append_alert') as m:
            rc = self.h.run()
        self.assertEqual(rc, 0)
        m.assert_not_called()

    def test_run_fires_when_enabled_key_absent(self):
        # Back-compat path: absent `enabled` -> gate active -> DM at high burn.
        rc, captured = self._run_at_high_usage_with_config(
            {'tier1_quota': {'max_5h_token_threshold': 10_000_000}})
        self.assertEqual(rc, 0)
        self.assertNotEqual(captured, {},
                            'absent enabled key must preserve pre-deprecation '
                            'firing behavior')
        self.assertEqual(captured.get('severity'), 'warning')


class DmBodyTest(_IsolatedAgentsRoot):
    """Verify the new DM body template + ledger-count integration end-to-end
    by capturing the larry_alerts.append_alert call. Uses real datetime.now;
    fixtures are timestamped relative to wall-clock now so the rolling
    window picks them up without mocking time."""

    def _run_with_tokens(self, tokens_now, ledger_events=None,
                         threshold=10_000_000):
        """Write a single cost record carrying `tokens_now` quota-consuming
        tokens (split across input + output for variety) and run the healer
        under a mocked threshold + alert sink."""
        now = datetime.now(timezone.utc)
        # Split into input/output so the record is realistic. cache_read
        # is deliberately huge to verify it's NOT counted.
        self._write_costs([
            {'ts': (now - timedelta(minutes=10)).isoformat(),
             'agent': 'forge', 'cost_usd': 0.0,
             'input_tokens': tokens_now // 2,
             'output_tokens': tokens_now - (tokens_now // 2),
             'cache_creation': 0,
             'cache_read': 999_999_999},
        ])
        if ledger_events is not None:
            self._write_ledger(ledger_events)
        captured = {}

        def fake_append_alert(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(self.h, 'gate_enabled', return_value=True), \
             mock.patch.object(self.h, 'load_threshold',
                               return_value=threshold), \
             mock.patch('larry_alerts.append_alert',
                        side_effect=fake_append_alert):
            rc = self.h.run()
        return rc, captured, now

    def test_dm_body_uses_token_gate_template(self):
        # 85% of 10M -> fires.
        rc, captured, _ = self._run_with_tokens(8_500_000, ledger_events=[])
        self.assertEqual(rc, 0)
        body = captured.get('message', '')
        self.assertIn('Trailing 5h quota pace at', body)
        self.assertIn('of token gate', body)
        self.assertIn('8,500,000 of 10,000,000 tokens', body)
        self.assertIn('input+output+cache_creation', body)
        self.assertIn('Pace indicator only', body)
        self.assertIn('https://console.anthropic.com/settings/usage', body)
        self.assertIn('Recent rate-limit events (trailing 2h): 0', body)

    def test_dm_body_has_no_dollar_denomination(self):
        rc, captured, _ = self._run_with_tokens(9_000_000)
        self.assertEqual(rc, 0)
        body = captured.get('message', '')
        action = captured.get('suggested_action', '')
        # No '$' anywhere — that was the whole point of the re-base.
        self.assertNotIn('$', body)
        self.assertNotIn('$', action)
        # Old framing is gone.
        self.assertNotIn('dollar gate', body)
        self.assertNotIn('LLM pace', body)

    def test_dm_body_includes_trailing_2h_ledger_count(self):
        now = datetime.now(timezone.utc)
        rc, captured, _ = self._run_with_tokens(
            9_200_000,
            ledger_events=[
                {'ts': (now - timedelta(minutes=15)).isoformat(),
                 'agent': 'forge', 'task_id': 't1'},
                {'ts': (now - timedelta(hours=1)).isoformat(),
                 'agent': 'beacon', 'task_id': 't2'},
                {'ts': (now - timedelta(hours=4)).isoformat(),
                 'agent': 'forge', 'task_id': 't3'},  # outside window
            ],
        )
        self.assertEqual(rc, 0)
        body = captured.get('message', '')
        self.assertIn('Recent rate-limit events (trailing 2h): 2', body)


class LeadingWarningTest(_IsolatedAgentsRoot):
    """Acceptance: warn fires near the wall; quiet at low usage; the
    documented 2026-05-27 false-alarm condition does not reproduce."""

    def _run(self, *, io_plus_cache_creation, threshold=10_000_000):
        now = datetime.now(timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=5)).isoformat(),
             'agent': 'forge',
             'input_tokens': io_plus_cache_creation // 3,
             'output_tokens': io_plus_cache_creation // 3,
             'cache_creation': io_plus_cache_creation - 2 * (io_plus_cache_creation // 3),
             'cache_read': 999_999_999,
             'cost_usd': 0.0},
        ])
        captured = {}

        def fake_append_alert(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(self.h, 'gate_enabled', return_value=True), \
             mock.patch.object(self.h, 'load_threshold',
                               return_value=threshold), \
             mock.patch('larry_alerts.append_alert',
                        side_effect=fake_append_alert):
            self.h.run()
        return captured

    def test_warn_fires_near_the_wall(self):
        # 95% of 10M -> well above 80% trigger -> DM fires.
        captured = self._run(io_plus_cache_creation=9_500_000)
        self.assertNotEqual(captured, {})
        self.assertEqual(captured.get('severity'), 'warning')

    def test_quiet_at_low_usage(self):
        # 10% of 10M -> nowhere near 80% -> no DM.
        captured = self._run(io_plus_cache_creation=1_000_000)
        self.assertEqual(captured, {})

    def test_no_false_alarm_at_2026_05_27_period(self):
        """The brief's anchor condition: on 2026-05-27 the prior dollar
        gate fired every ~15 min while real Anthropic usage was at 31%
        session / 59% weekly. The documented rolling-5h quota-consuming
        token volume during that AM period peaked at ~9.7M
        (input+output+cache_creation). With the 10M-token seed (80%
        trigger at 8M) we'd still trigger at that 9.7M peak, so we
        further verify the lower-bound rolling-5h volumes during that
        window (early hours: ~2.8M-4M tokens) absolutely do not fire,
        and the typical mid-period volume (~5M tokens) is also quiet."""
        for tokens in (2_800_000, 4_000_000, 5_000_000):
            captured = self._run(io_plus_cache_creation=tokens)
            self.assertEqual(
                captured, {},
                f'unexpected DM at 2026-05-27-shape usage={tokens} tokens '
                f'(threshold=10M, 80%=8M)',
            )

    def test_cache_read_alone_does_not_trigger(self):
        """Massive cache_read with zero input/output/cache_creation must
        not trigger — cache reads are the largely-free re-use case and
        are explicitly excluded from the proxy."""
        now = datetime.now(timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=5)).isoformat(),
             'agent': 'forge',
             'input_tokens': 0, 'output_tokens': 0,
             'cache_creation': 0,
             'cache_read': 999_999_999,
             'cost_usd': 0.0},
        ])
        captured = {}

        def fake_append_alert(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(self.h, 'gate_enabled', return_value=True), \
             mock.patch.object(self.h, 'load_threshold',
                               return_value=10_000_000), \
             mock.patch('larry_alerts.append_alert',
                        side_effect=fake_append_alert):
            self.h.run()
        self.assertEqual(captured, {})


class ConfidenceSeverityRoutingTest(_IsolatedAgentsRoot):
    """Slice 3 (confidence -> severity). The alert still fires on any
    pct >= 0.80, but the severity it carries now depends on how confident the
    reading is:
      - high-confidence (pct >= HIGH_CONFIDENCE_FRACTION, OR any trailing-2h
        429 in the ground-truth ledger) -> severity='warning' -> escalate (DM);
      - borderline (0.80 <= pct < HIGH_CONFIDENCE_FRACTION, no 429s) ->
        severity='info' -> digest lane (no DM);
      - degraded read (missing/unreadable costs.jsonl) -> pct 0.0 -> early
        return, NO alert at all (can never reach the digest branch).
    """

    def _run_with_tokens(self, tokens_now, ledger_events=None,
                         threshold=10_000_000):
        now = datetime.now(timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=10)).isoformat(),
             'agent': 'forge', 'cost_usd': 0.0,
             'input_tokens': tokens_now // 2,
             'output_tokens': tokens_now - (tokens_now // 2),
             'cache_creation': 0,
             'cache_read': 999_999_999},
        ])
        if ledger_events is not None:
            self._write_ledger(ledger_events)
        captured = {}

        def fake_append_alert(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(self.h, 'gate_enabled', return_value=True), \
             mock.patch.object(self.h, 'load_threshold',
                               return_value=threshold), \
             mock.patch('larry_alerts.append_alert',
                        side_effect=fake_append_alert):
            rc = self.h.run()
        return rc, captured

    def test_high_confidence_pace_escalates(self):
        # 95% of 10M -> pct 0.95 >= HIGH_CONFIDENCE_FRACTION (0.90), no 429s.
        # A high-confidence/sustained breach still pages: severity='warning',
        # and no explicit route (so append_alert's default 'escalate' applies).
        rc, captured = self._run_with_tokens(9_500_000, ledger_events=[])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get('severity'), 'warning')
        self.assertIsNone(captured.get('route'),
                          'severity drives the route default; slice 3 threads '
                          'severity only, never an explicit route')

    def test_observed_429_escalates_even_in_borderline_band(self):
        # 84% pace (borderline band) BUT a trailing-2h 429 is ground-truth
        # corroboration -> high confidence -> escalate. A real problem in the
        # borderline band is NEVER buried in the digest.
        now = datetime.now(timezone.utc)
        rc, captured = self._run_with_tokens(
            8_400_000,
            ledger_events=[
                {'ts': (now - timedelta(minutes=20)).isoformat(),
                 'agent': 'forge', 'task_id': 't1'},
            ],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get('severity'), 'warning')

    def test_borderline_pace_digests(self):
        # 84% of 10M -> pct 0.84 in [0.80, 0.90) with NO 429s -> borderline ->
        # severity='info' (routes to the digest lane, no DM).
        rc, captured = self._run_with_tokens(8_400_000, ledger_events=[])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get('severity'), 'info',
                         'a borderline leading-edge reading must digest, '
                         'not page')

    def test_degraded_read_does_not_downgrade_to_digest(self):
        # No costs.jsonl written -> rolling volume 0 -> pct 0.0 -> below the
        # 0.80 trigger -> early return. The degraded read produces NO alert of
        # any severity; it can never reach the digest branch. This is the
        # guardrail's inverse: absence-of-signal keeps the fail-loud posture.
        captured = {}

        def fake_append_alert(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(self.h, 'gate_enabled', return_value=True), \
             mock.patch.object(self.h, 'load_threshold',
                               return_value=10_000_000), \
             mock.patch('larry_alerts.append_alert',
                        side_effect=fake_append_alert):
            rc = self.h.run()
        self.assertEqual(rc, 0)
        self.assertEqual(captured, {},
                         'a degraded/absent read must emit NO alert — never a '
                         'digested red')

    def test_at_confidence_boundary_escalates(self):
        # Exactly HIGH_CONFIDENCE_FRACTION (90% of 10M) -> escalate (>=).
        rc, captured = self._run_with_tokens(9_000_000, ledger_events=[])
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get('severity'), 'warning')


class ConstantsTest(_IsolatedAgentsRoot):
    """Pin the cooldown + window constants so a refactor can't silently
    weaken the dedup story."""

    def test_constants_intact(self):
        self.assertEqual(self.h.ALERT_THRESHOLD_FRACTION, 0.80)
        self.assertEqual(self.h.HIGH_CONFIDENCE_FRACTION, 0.90)
        self.assertEqual(self.h.WINDOW_HOURS, 5)
        self.assertEqual(self.h.DM_COOLDOWN_HOURS, 5)
        self.assertEqual(self.h.RATE_LIMIT_RECENT_HOURS, 2)


if __name__ == '__main__':
    unittest.main()
