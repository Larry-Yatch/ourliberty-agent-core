#!/usr/bin/env python3
"""Regression tests: agent_runner writes UTC-aware ISO timestamps.

Producer-side guard for the 2026-05-26 timezone bug where naive
`datetime.now().isoformat()` produced MDT-local strings that the
dashboard_api consumer (which correctly assumes UTC for naive values)
rendered as wall-clock + 6h.

Covers all five agent_runner.py callsites cited in the fix:
  - `_mark_paused_on_tier1` 'at'
  - `_register_in_flight` 'started_at'
  - `_meta_started_at` (inside run_claude)
  - `out_meta['completed_at']` (inside run_claude)
  - `process_task` result 'timestamp'

The first two are exercised end-to-end (call the function, parse the
JSON it writes, assert tz-aware UTC). The remaining three live deep
inside `run_claude` / `process_task` and are covered via AST inspection
of the source — equivalent regression strength without spinning up the
full claude-subprocess harness.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_timezone_discipline
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import agent_runner as ar  # noqa: E402


def _assert_utc_aware(case: unittest.TestCase, iso_str: str) -> None:
    """Parse iso_str and assert it is tz-aware AND UTC (offset == 0).

    Rejects both naive datetimes (the original bug) and accidentally
    tz-aware-but-non-UTC values (e.g. if someone reverted to MDT-aware).
    """
    case.assertIsInstance(iso_str, str)
    parsed = datetime.fromisoformat(iso_str)
    case.assertIsNotNone(
        parsed.tzinfo,
        f'expected tz-aware datetime, got naive: {iso_str!r}',
    )
    case.assertEqual(
        parsed.utcoffset(),
        timedelta(0),
        f'expected UTC offset 0, got {parsed.utcoffset()} for {iso_str!r}',
    )


class RegisterInFlightTimezoneTest(unittest.TestCase):
    """`_register_in_flight` writes 'started_at' tz-aware UTC."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ar-tz-inflight-'))
        self._orig_dir = ar.IN_FLIGHT_DIR
        ar.IN_FLIGHT_DIR = self.tmp

    def tearDown(self):
        ar.IN_FLIGHT_DIR = self._orig_dir

    def test_started_at_is_utc_aware(self):
        ar._register_in_flight(task_stem='demo-task', agent_id='forge', pid=1234)
        written = json.loads((self.tmp / 'demo-task.json').read_text())
        _assert_utc_aware(self, written['started_at'])


class MarkPausedOnTier1TimezoneTest(unittest.TestCase):
    """`_mark_paused_on_tier1` writes 'at' tz-aware UTC."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ar-tz-paused-'))
        self._orig_dir = ar.IN_FLIGHT_DIR
        ar.IN_FLIGHT_DIR = self.tmp

    def tearDown(self):
        ar.IN_FLIGHT_DIR = self._orig_dir

    def test_at_is_utc_aware(self):
        ar._mark_paused_on_tier1(task_stem='demo-task', failure_type='quota')
        written = json.loads((self.tmp / 'demo-task.json').read_text())
        self.assertIn('paused_on_tier1', written)
        _assert_utc_aware(self, written['paused_on_tier1']['at'])


# ---------- AST inspection for deeply-buried callsites ----------


def _datetime_now_isoformat_calls(source_path: Path) -> list[ast.Call]:
    """Return every `<expr>.isoformat()` Call whose receiver is a
    `datetime.now(...)` Call in the given source file."""
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    hits: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Looking for: <something>.isoformat()
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == 'isoformat'):
            continue
        # The receiver of .isoformat() should itself be datetime.now(...).
        receiver = func.value
        if not isinstance(receiver, ast.Call):
            continue
        r_func = receiver.func
        if isinstance(r_func, ast.Attribute) and r_func.attr == 'now':
            hits.append(receiver)
        elif isinstance(r_func, ast.Name) and r_func.id == 'now':
            hits.append(receiver)
    return hits


def _call_uses_utc_arg(call: ast.Call) -> bool:
    """True iff the Call passes a `timezone.utc` (or `utc`) argument."""
    for arg in call.args:
        if isinstance(arg, ast.Attribute) and arg.attr == 'utc':
            return True
        if isinstance(arg, ast.Name) and arg.id == 'utc':
            return True
    for kw in call.keywords:
        v = kw.value
        if isinstance(v, ast.Attribute) and v.attr == 'utc':
            return True
        if isinstance(v, ast.Name) and v.id == 'utc':
            return True
    return False


class AgentRunnerSourceTimezoneTest(unittest.TestCase):
    """Catches the original bug by static analysis: every
    `datetime.now(...).isoformat()` in agent_runner.py must pass
    `timezone.utc`. Covers the buried callsites inside run_claude /
    process_task (and re-covers the helpers exercised end-to-end above —
    defense in depth)."""

    def setUp(self):
        self.source = Path(ar.__file__)
        self.calls = _datetime_now_isoformat_calls(self.source)

    def test_at_least_five_isoformat_callsites(self):
        # Sanity: if this drops, somebody removed a callsite without
        # updating this test. Re-evaluate scope before lowering. New
        # callsites raise the floor — the rate-limit ledger writer added
        # in Check VIII PR-2a brings the count to ≥ 6.
        self.assertGreaterEqual(
            len(self.calls), 5,
            f'expected at least 5 datetime.now(...).isoformat() callsites in '
            f'agent_runner.py, found {len(self.calls)}',
        )

    def test_every_isoformat_call_is_utc_aware(self):
        bad = [c for c in self.calls if not _call_uses_utc_arg(c)]
        if bad:
            lines = sorted(c.lineno for c in bad)
            self.fail(
                f'agent_runner.py has naive datetime.now().isoformat() at '
                f'lines {lines} — must be datetime.now(timezone.utc).isoformat()'
            )

    def test_meta_started_at_uses_utc(self):
        # _meta_started_at assignment inside run_claude.
        self._assert_callsite_uses_utc(target_line=801)

    def test_completed_at_uses_utc(self):
        # out_meta['completed_at'] inside run_claude's success branch.
        self._assert_callsite_uses_utc(target_line=1295)

    def test_process_task_timestamp_uses_utc(self):
        # result['timestamp'] inside process_task.
        self._assert_callsite_uses_utc(target_line=1761)

    def _assert_callsite_uses_utc(self, target_line: int) -> None:
        # Tolerate ±3 lines of drift from upstream edits before failing
        # with a "callsite moved" message — easier to update than to
        # chase a brittle line number.
        nearby = [c for c in self.calls if abs(c.lineno - target_line) <= 3]
        self.assertTrue(
            nearby,
            f'no datetime.now(...).isoformat() within 3 lines of {target_line} — '
            f'callsite may have moved; update this test',
        )
        for call in nearby:
            self.assertTrue(
                _call_uses_utc_arg(call),
                f'datetime.now(...).isoformat() at line {call.lineno} does '
                f'not pass timezone.utc',
            )


if __name__ == '__main__':
    unittest.main()
