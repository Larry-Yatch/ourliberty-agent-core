#!/usr/bin/env python3
"""Tests for scripts/calibrate_tier_budget.py — the § 12b self-tuning per-tier
5h budget job. Verifies wall reading, the tighten/bootstrap/loosen/hold logic,
the atomic override write, and that the selector actually reads the result.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_calibrate_tier_budget
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
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

import calibrate_tier_budget as cal  # noqa: E402
import active_tier  # noqa: E402

_NOW = datetime(2026, 6, 30, 18, 0, 0, tzinfo=timezone.utc)


class _CalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'blackboard').mkdir(parents=True, exist_ok=True)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)
        (self.root / 'logs').mkdir(parents=True, exist_ok=True)
        self._env = {}
        self._set('OURLIBERTY_AGENTS_ROOT', str(self.root))
        self._set('OURLIBERTY_CREDENTIALS_ENV_FILE', str(self.root / 'no.env'))
        # Patch the module-frozen paths onto the tmp tree.
        self._patch('RATE_LIMIT_LEDGER_FILE',
                    self.root / 'blackboard' / 'anthropic-quota-events.jsonl')
        self._patch('LOG_FILE', self.root / 'logs' / 'cal.log')
        self._patch('HEARTBEAT_FILE', self.root / 'blackboard' / 'cal.heartbeat')
        self._patch('KILL_SWITCH', self.root / 'healers.disabled')

    def tearDown(self):
        mock.patch.stopall()
        for k, prev in self._env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        self.tmp.cleanup()

    def _set(self, k, v):
        self._env[k] = os.environ.get(k)
        os.environ[k] = v

    def _patch(self, attr, value):
        mock.patch.object(cal, attr, value).start()

    def _write_walls(self, rows):
        # rows: list of (account, ts_datetime, failure_class)
        path = self.root / 'blackboard' / 'anthropic-quota-events.jsonl'
        lines = [json.dumps({'ts': ts.isoformat(), 'account': acct,
                             'failure_class': fc, 'agent': 'x', 'task_id': ''})
                 for acct, ts, fc in rows]
        path.write_text('\n'.join(lines) + '\n')

    def _write_costs(self, rows):
        # rows: list of (account, tokens, ts_datetime)
        path = self.root / 'blackboard' / 'costs.jsonl'
        lines = [json.dumps({'ts': ts.isoformat(), 'account': acct,
                             'input_tokens': toks, 'output_tokens': 0,
                             'cache_creation': 0}) for acct, toks, ts in rows]
        path.write_text('\n'.join(lines) + '\n')


class ReadWallsTest(_CalBase):
    def test_filters_class_account_and_window(self):
        self._write_walls([
            ('tier1', _NOW - timedelta(hours=1), 'rate_limit'),
            ('tier1', _NOW - timedelta(days=20), 'rate_limit'),   # too old
            ('tier1', _NOW - timedelta(hours=2), 'auth_401'),     # wrong class
            ('tier3', _NOW - timedelta(hours=3), 'rate_limit'),
            ('fixture', _NOW - timedelta(hours=1), 'rate_limit'),  # sentinel
        ])
        walls = cal.read_walls(now=_NOW)
        self.assertEqual(len(walls.get('tier1', [])), 1)
        self.assertEqual(len(walls.get('tier3', [])), 1)
        self.assertNotIn('fixture', walls)

    def test_missing_ledger_fail_open(self):
        self.assertEqual(cal.read_walls(now=_NOW), {})


class ComputeBudgetsTest(_CalBase):
    def test_tighten_from_walls(self):
        # Two tier1 walls; both see an 8M rolling-5h burn -> 0.90*8M = 7.2M.
        self._write_costs([('tier1', 8_000_000, _NOW - timedelta(hours=3))])
        self._write_walls([
            ('tier1', _NOW - timedelta(hours=1), 'rate_limit'),
            ('tier1', _NOW - timedelta(hours=2), 'rate_limit'),
        ])
        budgets = cal.compute_budgets(now=_NOW)
        self.assertEqual(budgets['tier1'], 7_200_000)

    def test_floor_guards_low_wall(self):
        # A single-ish anomalous wall with tiny burn must not drive below floor.
        self._write_costs([('tier1', 100_000, _NOW - timedelta(hours=1))])
        self._write_walls([
            ('tier1', _NOW - timedelta(minutes=30), 'rate_limit'),
            ('tier1', _NOW - timedelta(minutes=20), 'rate_limit'),
        ])
        budgets = cal.compute_budgets(now=_NOW)
        self.assertEqual(budgets['tier1'], cal.MIN_BUDGET_FLOOR)

    def test_bootstrap_no_override_without_enough_walls(self):
        # One wall only (< MIN_WALLS) and no prior override -> tier omitted.
        self._write_costs([('tier1', 8_000_000, _NOW - timedelta(hours=1))])
        self._write_walls([('tier1', _NOW - timedelta(minutes=30), 'rate_limit')])
        self.assertNotIn('tier1', cal.compute_budgets(now=_NOW))

    def test_loosen_toward_default_when_no_walls(self):
        # Prior override well below default, no walls -> raise x1.05.
        cal.write_override({'tier1': 5_000_000})
        budgets = cal.compute_budgets(now=_NOW)   # empty ledger
        self.assertEqual(budgets['tier1'], int(5_000_000 * cal.LOOSEN_FACTOR))

    def test_loosen_drops_override_at_default(self):
        # Prior override just under default; x1.05 crosses it -> drop (use default).
        cfg = active_tier._tier_pool_config()
        default = cfg['max_5h_budget_tokens']
        cal.write_override({'tier1': int(default / cal.LOOSEN_FACTOR) + 1})
        self.assertNotIn('tier1', cal.compute_budgets(now=_NOW))

    def test_hold_when_too_few_walls_but_prior_exists(self):
        cal.write_override({'tier1': 6_000_000})
        self._write_walls([('tier1', _NOW - timedelta(minutes=30), 'rate_limit')])
        self.assertEqual(cal.compute_budgets(now=_NOW)['tier1'], 6_000_000)


class OverrideRoundTripTest(_CalBase):
    def test_write_read_and_selector_reads_it(self):
        cal.write_override({'tier3': 4_000_000})
        # The selector's _effective_budget reads the same file.
        cfg = active_tier._tier_pool_config()
        self.assertEqual(active_tier._effective_budget('tier3', cfg), 4_000_000)
        # A tier without an override falls back to the config default.
        self.assertEqual(active_tier._effective_budget('tier1', cfg),
                         cfg['max_5h_budget_tokens'])


class MainTest(_CalBase):
    def test_kill_switch_skips(self):
        (self.root / 'healers.disabled').write_text('')
        with mock.patch.object(cal, 'write_override') as w:
            self.assertEqual(cal.main(), 0)
        w.assert_not_called()

    def test_main_writes_and_is_failsafe(self):
        self._write_costs([('tier1', 8_000_000, _NOW - timedelta(hours=3))])
        self._write_walls([
            ('tier1', _NOW - timedelta(hours=1), 'rate_limit'),
            ('tier1', _NOW - timedelta(hours=2), 'rate_limit'),
        ])
        # Uses real now(); walls are within 14d of any run date near build time.
        self.assertEqual(cal.main(), 0)
        self.assertTrue((self.root / 'blackboard' / 'cal.heartbeat').exists())

    def test_compute_error_is_failsafe(self):
        with mock.patch.object(cal, 'compute_budgets',
                               side_effect=RuntimeError('boom')):
            self.assertEqual(cal.main(), 0)  # must not raise


if __name__ == '__main__':
    unittest.main()
