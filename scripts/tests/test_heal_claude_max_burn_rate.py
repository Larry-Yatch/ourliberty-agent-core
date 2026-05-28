#!/usr/bin/env python3
"""Tests for heal_claude_max_burn_rate — Check VIII PR-2a DM reframe.

Covers:
- The new DM body template (pace indicator + trailing-2h rate-limit count).
- Trailing-2h count computed from the rate-limit ledger.
- Missing ledger → count of 0 (no error).
- Existing threshold/cooldown logic unchanged.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_heal_claude_max_burn_rate
"""
from __future__ import annotations

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

    def _write_costs(self, entries):
        with open(self.h.COSTS_FILE, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

    def _write_ledger(self, entries):
        with open(self.h.RATE_LIMIT_LEDGER_FILE, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')


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


class DmBodyTest(_IsolatedAgentsRoot):
    """Verify the new DM body template + ledger-count integration end-to-end
    by capturing the larry_alerts.append_alert call. Uses real datetime.now;
    fixtures are timestamped relative to wall-clock now so the rolling
    window picks them up without mocking time."""

    def _run_with_spend(self, spend_now_usd, ledger_events=None, threshold=60.0):
        now = datetime.now(timezone.utc)
        self._write_costs([
            {'ts': (now - timedelta(minutes=10)).isoformat(),
             'agent': 'forge', 'cost_usd': spend_now_usd},
        ])
        if ledger_events is not None:
            self._write_ledger(ledger_events)
        captured = {}

        def fake_append_alert(**kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(self.h, 'load_threshold',
                               return_value=threshold), \
             mock.patch('larry_alerts.append_alert',
                        side_effect=fake_append_alert):
            rc = self.h.run()
        return rc, captured, now

    def test_dm_body_uses_new_pace_indicator_template(self):
        # 85% of $60 → fires.
        rc, captured, _ = self._run_with_spend(51.0, ledger_events=[])
        self.assertEqual(rc, 0)
        body = captured.get('message', '')
        self.assertIn('Trailing 5h LLM pace at', body)
        self.assertIn('% of dollar gate', body)
        self.assertIn('$51.00 of $60.00', body)
        self.assertIn('Pace indicator only', body)
        self.assertIn('https://console.anthropic.com/settings/usage', body)
        self.assertIn('Recent rate-limit events (trailing 2h): 0', body)
        # Old "quota wall" language is gone.
        self.assertNotIn('quota wall', body)
        self.assertNotIn('Consider pausing dispatches', body)

    def test_dm_body_includes_trailing_2h_ledger_count(self):
        now = datetime.now(timezone.utc)
        rc, captured, _ = self._run_with_spend(
            55.0,
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

    def test_threshold_and_cooldown_unchanged(self):
        # Below 80% threshold → no DM fires (existing logic preserved).
        rc, captured, _ = self._run_with_spend(40.0)
        self.assertEqual(rc, 0)
        self.assertEqual(captured, {})
        # Constants intact.
        self.assertEqual(self.h.ALERT_THRESHOLD_FRACTION, 0.80)
        self.assertEqual(self.h.WINDOW_HOURS, 5)
        self.assertEqual(self.h.DM_COOLDOWN_HOURS, 5)


if __name__ == '__main__':
    unittest.main()
