#!/usr/bin/env python3
"""Tests for agent_runner.append_rate_limit_event — Check VIII PR-2a.

Verifies schema conformance, idempotency on (ts, agent, task_id), and the
'never raises' contract (a ledger I/O failure must not break the agent run).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_rate_limit_ledger
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import agent_runner  # noqa: E402


_EXPECTED_KEYS = {
    'ts', 'agent', 'task_id', 'model', 'account',
    'retry_after_sec', 'raw_excerpt',
}


def _read_ledger(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


class AppendRateLimitEventTest(unittest.TestCase):
    def setUp(self):
        # Each test gets its own ledger path; tmp_path equivalent without
        # depending on pytest fixtures since this is a unittest.TestCase.
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / 'anthropic-quota-events.jsonl'

    def tearDown(self):
        self._tmp.cleanup()

    def test_appends_one_record_with_full_schema(self):
        result = agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='check-viii-pr-2a',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit; resets at 2026-05-28T14:00:00Z',
            retry_after_sec=300,
            ts='2026-05-28T12:00:00+00:00',
            ledger_path=self.ledger,
        )
        self.assertEqual(result, self.ledger)
        records = _read_ledger(self.ledger)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(set(rec.keys()), _EXPECTED_KEYS)
        self.assertEqual(rec['ts'], '2026-05-28T12:00:00+00:00')
        self.assertEqual(rec['agent'], 'forge')
        self.assertEqual(rec['task_id'], 'check-viii-pr-2a')
        self.assertEqual(rec['model'], 'claude-opus-4-7')
        self.assertEqual(rec['account'], 'tier1')
        self.assertEqual(rec['retry_after_sec'], 300)
        self.assertIn('hit your limit', rec['raw_excerpt'])

    def test_retry_after_sec_defaults_to_null(self):
        agent_runner.append_rate_limit_event(
            agent='beacon',
            task_id='t-001',
            model='claude-sonnet-4-6',
            account='tier1',
            stderr='5-hour limit reached',
            ts='2026-05-28T13:00:00+00:00',
            ledger_path=self.ledger,
        )
        rec = _read_ledger(self.ledger)[0]
        self.assertIsNone(rec['retry_after_sec'])

    def test_raw_excerpt_truncates_to_300_chars(self):
        long_stderr = 'x' * 1000
        agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='t-002',
            model='claude-opus-4-7',
            account='tier1',
            stderr=long_stderr,
            ts='2026-05-28T14:00:00+00:00',
            ledger_path=self.ledger,
        )
        rec = _read_ledger(self.ledger)[0]
        self.assertEqual(len(rec['raw_excerpt']), 300)

    def test_idempotent_on_ts_agent_task_id_tuple(self):
        payload = dict(
            agent='forge',
            task_id='check-viii-pr-2a',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit',
            ts='2026-05-28T12:00:00+00:00',
            ledger_path=self.ledger,
        )
        first = agent_runner.append_rate_limit_event(**payload)
        second = agent_runner.append_rate_limit_event(**payload)
        self.assertEqual(first, self.ledger)
        self.assertIsNone(second)
        self.assertEqual(len(_read_ledger(self.ledger)), 1)

    def test_different_task_id_appends_distinct_record(self):
        agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='t-A',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit',
            ts='2026-05-28T12:00:00+00:00',
            ledger_path=self.ledger,
        )
        agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='t-B',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit',
            ts='2026-05-28T12:00:00+00:00',
            ledger_path=self.ledger,
        )
        records = _read_ledger(self.ledger)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            sorted(r['task_id'] for r in records),
            ['t-A', 't-B'],
        )

    def test_different_ts_same_agent_task_appends_distinct(self):
        agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='t-C',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit',
            ts='2026-05-28T12:00:00+00:00',
            ledger_path=self.ledger,
        )
        agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='t-C',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit',
            ts='2026-05-28T17:00:00+00:00',
            ledger_path=self.ledger,
        )
        self.assertEqual(len(_read_ledger(self.ledger)), 2)

    def test_writes_default_ts_when_omitted(self):
        result = agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='t-default-ts',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit',
            ledger_path=self.ledger,
        )
        self.assertEqual(result, self.ledger)
        rec = _read_ledger(self.ledger)[0]
        # ISO-8601 with timezone — non-empty + parseable.
        from datetime import datetime
        parsed = datetime.fromisoformat(rec['ts'].replace('Z', '+00:00'))
        self.assertIsNotNone(parsed.tzinfo)

    def test_io_failure_returns_none_does_not_raise(self):
        # Path that can't be created (parent is a file, not a dir).
        blocker = Path(self._tmp.name) / 'blocker'
        blocker.write_text('not-a-dir')
        bad_path = blocker / 'ledger.jsonl'
        result = agent_runner.append_rate_limit_event(
            agent='forge',
            task_id='t-bad',
            model='claude-opus-4-7',
            account='tier1',
            stderr='hit your limit',
            ts='2026-05-28T12:00:00+00:00',
            ledger_path=bad_path,
        )
        self.assertIsNone(result)

    def test_env_override_redirects_default_path(self, ):
        import os
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp.name
        try:
            resolved = agent_runner._rate_limit_ledger_path()
        finally:
            del os.environ['OURLIBERTY_AGENTS_ROOT']
        self.assertEqual(
            resolved,
            Path(self._tmp.name) / 'blackboard' / 'anthropic-quota-events.jsonl',
        )


if __name__ == '__main__':
    unittest.main()
