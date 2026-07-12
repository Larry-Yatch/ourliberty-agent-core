#!/usr/bin/env python3
"""Tests for active_tier's wrapper-bridge CLI verbs (select-dispatch-env /
report-dispatch-failure) — the seam that lets the two BASH direct-`claude`
spawns (run_cycle.sh, run_medic.sh) join the per-task tier-dispatch pool
instead of riding the single active tier (which pre-pool concentrated the
cycle's burn on tier1 while tier3 idled).

Covers the cli_* functions in-process (the __main__ block only routes argv):
  * select-dispatch-env: TIER=/KEY=VALUE line protocol, round-robin across
    healthy primaries, delta-only env emission (no HOME line on the
    setup-token path when HOME already matches), operator-pin precedence,
    exit 3 + empty stdout on an all-held pool.
  * report-dispatch-failure: rate-limit output → cooldown benched + quota
    ledger row; non-wall output → no-op; unknown tier → exit 2; unreadable
    file → best-effort no-op.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_active_tier_cli
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import active_tier  # noqa: E402

try:  # dotted/pytest path: relative import within the scripts.tests package
    from .test_active_tier_pool import _PoolBase, _NOW
except ImportError:  # discover loads this module top-level (no package parent)
    from test_active_tier_pool import _PoolBase, _NOW


def _parse_lines(out):
    """CLI stdout -> ordered dict of KEY -> VALUE (splitting on first '=')."""
    pairs = {}
    for line in out.splitlines():
        if not line:
            continue
        key, _, value = line.partition('=')
        pairs[key] = value
    return pairs


class SelectDispatchEnvTest(_PoolBase):
    def _base_env(self):
        # A wrapper's env: HOME already at the tier1 home (prod reality),
        # no bare OAuth token set.
        env = os.environ.copy()
        env['HOME'] = active_tier.TIER1_HOME
        env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
        return env

    def test_emits_tier_and_token_delta_only(self):
        code, out = active_tier.cli_select_dispatch_env(
            base_env=self._base_env(), now=_NOW)
        self.assertEqual(code, 0)
        pairs = _parse_lines(out)
        self.assertIn(pairs['TIER'], {'tier1', 'tier3'})
        # Token-only protocol: the setup-token is the ONLY line besides TIER=.
        # A wrapper never receives a HOME swap (the units' ProtectHome mount
        # can't satisfy one — see cli_select_dispatch_env).
        expected_token = ('sk-ant-oat01-t1' if pairs['TIER'] == 'tier1'
                          else 'sk-ant-oat01-t3')
        self.assertEqual(pairs['CLAUDE_CODE_OAUTH_TOKEN'], expected_token)
        self.assertNotIn('HOME', pairs)
        self.assertEqual(set(pairs), {'TIER', 'CLAUDE_CODE_OAUTH_TOKEN'})
        # First line is the TIER marker (the shell parses line 1 only).
        self.assertTrue(out.startswith('TIER='))

    def test_tokenless_selected_tier_exits_3_for_legacy_fallback(self):
        # Force the pool to select a tier that has NO setup-token (tier2 via
        # the emergency fallback): it would require a HOME swap the wrapper
        # can't do, so the CLI reports "no tier" (exit 3) and the wrapper
        # falls back to the legacy active-setup-token path (HOME stays put).
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'sk-ant-oat01-t2'
        # Give tier2 a real usable home + creds so select_dispatch_tier would
        # otherwise pick it, but strip its setup-token so the durable-env path
        # is token-less (HOME-swap shape).
        os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=_NOW)
        active_tier.set_cooldown('tier3', raw_excerpt='resets 3pm', now=_NOW)
        # Make tier2 auth-ok via a valid credentials.json (token-less path).
        creds = Path(active_tier.TIER2_HOME) / '.claude' / '.credentials.json'
        creds.parent.mkdir(parents=True, exist_ok=True)
        future_ms = int((_NOW.timestamp() + 3600) * 1000)
        creds.write_text(json.dumps({'claudeAiOauth': {'expiresAt': future_ms}}))
        code, out = active_tier.cli_select_dispatch_env(
            base_env=self._base_env(), now=_NOW)
        self.assertEqual(code, 3)
        self.assertEqual(out, '')

    def test_round_robins_across_primaries(self):
        tiers = set()
        for _ in range(6):
            _code, out = active_tier.cli_select_dispatch_env(
                base_env=self._base_env(), now=_NOW)
            tiers.add(_parse_lines(out)['TIER'])
        self.assertEqual(tiers, {'tier1', 'tier3'})

    def test_operator_pin_wins(self):
        (self.root / 'rotation.disabled').write_text('tier3\n')
        for _ in range(3):
            _code, out = active_tier.cli_select_dispatch_env(
                base_env=self._base_env(), now=_NOW)
            self.assertEqual(_parse_lines(out)['TIER'], 'tier3')

    def test_all_held_exits_3_empty(self):
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm', now=_NOW)
        active_tier.set_cooldown('tier3', raw_excerpt='resets 3pm', now=_NOW)
        # tier2 has no token in _PoolBase -> unusable; pool is fully held.
        code, out = active_tier.cli_select_dispatch_env(
            base_env=self._base_env(), now=_NOW)
        self.assertEqual(code, 3)
        self.assertEqual(out, '')


class ReportDispatchFailureTest(_PoolBase):
    def setUp(self):
        super().setUp()
        self.out_file = self.root / 'cycle.last-output.json'

    def _ledger_rows(self):
        path = self.root / 'blackboard' / 'anthropic-quota-events.jsonl'
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln]

    def test_rate_limit_benches_tier_and_ledgers(self):
        # A real CLI wall: the usage-limit message is a bare (non-JSON) dump,
        # so the whole thing is scanned.
        self.out_file.write_text(
            'Claude usage limit reached — you have hit your limit, '
            'resets 3:30pm.')
        code, cls = active_tier.cli_report_dispatch_failure(
            'tier3', 'pulse', str(self.out_file), now=_NOW)
        self.assertEqual((code, cls), (0, 'rate_limit'))
        self.assertIsNotNone(active_tier.cooldown_until('tier3', now=_NOW))
        # The other primary stays healthy — only the walled tier benches.
        self.assertIsNone(active_tier.cooldown_until('tier1', now=_NOW))
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['account'], 'tier3')
        self.assertEqual(rows[0]['failure_class'], 'rate_limit')
        self.assertEqual(rows[0]['agent'], 'pulse')

    def test_quoted_limit_text_inside_result_does_not_bench(self):
        # The FALSE-positive case: the agent transcript (a valid --output-format
        # json envelope) QUOTES rate-limit language inside .result — e.g. Medic
        # triaging a quota alert — and the run failed for an unrelated reason.
        # The .result exclusion must keep the healthy tier unbenched and the
        # ledger clean.
        self.out_file.write_text(json.dumps({
            'type': 'result', 'subtype': 'error_during_execution',
            'is_error': True,
            'result': 'Investigated the alert quoting "you have hit your '
                      'limit, resets 3:30pm" — it was a false positive.',
        }))
        code, cls = active_tier.cli_report_dispatch_failure(
            'tier3', 'medic', str(self.out_file), now=_NOW)
        self.assertEqual((code, cls), (0, ''))
        self.assertIsNone(active_tier.cooldown_until('tier3', now=_NOW))
        self.assertEqual(self._ledger_rows(), [])

    def test_real_wall_outside_result_still_benches(self):
        # A real wall's CLI message is emitted OUTSIDE .result; even inside a
        # JSON-ish envelope it must still bench (the exclusion only drops the
        # quoted-prose FP, never a genuine wall).
        self.out_file.write_text(
            '{"type":"result","subtype":"success","result":"ok"}\n'
            'Claude usage limit reached — you have hit your limit, resets 3pm.')
        code, cls = active_tier.cli_report_dispatch_failure(
            'tier1', 'pulse', str(self.out_file), now=_NOW)
        self.assertEqual((code, cls), (0, 'rate_limit'))
        self.assertIsNotNone(active_tier.cooldown_until('tier1', now=_NOW))

    def test_session_lost_is_not_benched_and_returns_empty(self):
        # session_lost is a known class the classifier recognizes but which we
        # never bench — the wrapper must not log a bench that didn't happen.
        self.out_file.write_text('No conversation found with session ID abc')
        code, cls = active_tier.cli_report_dispatch_failure(
            'tier1', 'medic', str(self.out_file), now=_NOW)
        self.assertEqual((code, cls), (0, ''))
        self.assertIsNone(active_tier.cooldown_until('tier1', now=_NOW))
        self.assertEqual(self._ledger_rows(), [])

    def test_non_wall_output_is_noop(self):
        self.out_file.write_text('{"result": "some ordinary failure text"}')
        code, cls = active_tier.cli_report_dispatch_failure(
            'tier1', 'medic', str(self.out_file), now=_NOW)
        self.assertEqual((code, cls), (0, ''))
        self.assertIsNone(active_tier.cooldown_until('tier1', now=_NOW))
        self.assertEqual(self._ledger_rows(), [])

    def test_unknown_tier_exits_2(self):
        self.out_file.write_text('hit your limit')
        code, cls = active_tier.cli_report_dispatch_failure(
            'tier9', 'pulse', str(self.out_file), now=_NOW)
        self.assertEqual((code, cls), (2, ''))

    def test_missing_output_file_is_noop(self):
        code, cls = active_tier.cli_report_dispatch_failure(
            'tier1', 'pulse', str(self.root / 'nope.json'), now=_NOW)
        self.assertEqual((code, cls), (0, ''))
        self.assertIsNone(active_tier.cooldown_until('tier1', now=_NOW))


if __name__ == '__main__':
    unittest.main()
