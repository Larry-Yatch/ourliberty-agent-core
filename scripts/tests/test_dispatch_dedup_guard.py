#!/usr/bin/env python3
"""Tests for dispatch_dedup_guard.

Focus (nervous-system-audit 2026-06-05): the prompt-hash dedup ledger was DEAD
— nothing wrote `prompt_hash`/`identity` rows, so the content-duplicate refusal
never fired. The guard now records each approved dispatch; these tests prove the
record→detect loop closes (and that the filename-chain check is untouched).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_dispatch_dedup_guard
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_dedup_guard as ddg  # noqa: E402


class _IsolatedGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(ddg, 'LEDGER', self.root / 'logs' / 'dispatch_ledger.jsonl'),
            mock.patch.object(ddg, 'GUARD_LOG',
                              self.root / 'blackboard' / 'dispatch-dedup-refusals.jsonl'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _task(self, name: str, prompt: str = 'do the thing') -> Path:
        f = self.root / name
        f.write_text(json.dumps({'prompt': prompt, 'task_id': name}))
        return f

    def _run(self, target: str, task_file: Path) -> int:
        argv = ['dispatch_dedup_guard.py', '--target-agent', target,
                '--task-file', str(task_file)]
        with mock.patch.object(sys, 'argv', argv):
            try:
                ddg.main()
            except SystemExit as e:
                return e.code if isinstance(e.code, int) else 1
        return 0


class LedgerRoundTripTest(_IsolatedGuard):
    def test_record_then_recent_finds_it(self):
        ddg.record_dispatch('main', 'abc123', '/tmp/x.json')
        recent = ddg.recent_dispatches('main', ddg.HASH_DEDUP_MIN)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]['prompt_hash'], 'abc123')
        self.assertEqual(recent[0]['target_agent'], 'main')

    def test_record_failure_is_swallowed(self):
        # Ledger dir can't be created (a file sits where the 'logs' dir should
        # be) → record_dispatch must swallow the OSError, not raise (fail-open
        # on our own bookkeeping).
        ddg.LEDGER.parent.write_text('i am a file, not a dir')
        ddg.record_dispatch('main', 'abc', '/tmp/x.json')  # must not raise

    def test_old_entries_outside_window_ignored(self):
        old = {
            'ts': (datetime.now(tz=timezone.utc)
                   - timedelta(minutes=ddg.HASH_DEDUP_MIN + 5)).isoformat(),
            'identity': 'main', 'target_agent': 'main',
            'prompt_hash': 'stale', 'task_file': '/tmp/old.json',
        }
        ddg.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        ddg.LEDGER.write_text(json.dumps(old) + '\n')
        self.assertEqual(ddg.recent_dispatches('main', ddg.HASH_DEDUP_MIN), [])


class MainFlowTest(_IsolatedGuard):
    def test_first_dispatch_ok_and_recorded(self):
        rc = self._run('main', self._task('fresh-task.json'))
        self.assertEqual(rc, 0)
        # The approval was recorded so a repeat can be caught.
        self.assertTrue(ddg.LEDGER.exists())
        recent = ddg.recent_dispatches('main', ddg.HASH_DEDUP_MIN)
        self.assertEqual(len(recent), 1)

    def test_duplicate_prompt_to_same_agent_refused(self):
        first = self._run('main', self._task('a.json', prompt='identical body'))
        self.assertEqual(first, 0)
        # Different filename, same prompt + same target → content duplicate.
        second = self._run('main', self._task('b.json', prompt='identical body'))
        self.assertEqual(second, 1)
        refusals = ddg.GUARD_LOG.read_text()
        self.assertIn('prompt-hash-duplicate', refusals)

    def test_literal_same_task_file_rerun_not_self_blocked(self):
        # A retry that re-runs the guard on the EXACT same task_file must not be
        # refused by its own prior recording (the hash check targets re-slugged
        # duplicates under a different filename, not literal re-runs).
        task = self._task('retryable.json', prompt='same body each time')
        self.assertEqual(self._run('main', task), 0)
        self.assertEqual(self._run('main', task), 0)  # re-run, still OK

    def test_substring_agent_name_not_false_matched(self):
        # 'main' must not match a ledger row for 'maintenance' (boundary-safe).
        ddg.record_dispatch('maintenance', ddg.prompt_hash('shared body'),
                            str(self.root / 'other.json'))
        rc = self._run('main', self._task('mine.json', prompt='shared body'))
        self.assertEqual(rc, 0)  # different agent → not a duplicate

    def test_same_prompt_different_agent_allowed(self):
        self.assertEqual(self._run('main', self._task('a.json', prompt='same')), 0)
        # Same prompt body but a DIFFERENT target agent is not a duplicate.
        self.assertEqual(self._run('forge', self._task('b.json', prompt='same')), 0)

    def test_filename_chain_depth_still_refused(self):
        chained = '20260101T010101Z-20260101T020202Z-20260101T030303Z-task.json'
        rc = self._run('main', self._task(chained))
        self.assertEqual(rc, 1)
        self.assertIn('chain-depth-exceeded', ddg.GUARD_LOG.read_text())


if __name__ == '__main__':
    unittest.main()
