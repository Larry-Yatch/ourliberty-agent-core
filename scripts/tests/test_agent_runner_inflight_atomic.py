#!/usr/bin/env python3
"""Regression tests: agent_runner in-flight registry writes are crash-atomic.

The in-flight registry (`IN_FLIGHT_DIR/<task_stem>.json`) is the durable
"a live worker is handling this task" signal, read as a cross-process gate
by ~20 modules (dispatch_sentinel, cleanup_stale_worktrees,
heal_abandoned_inbox_tasks.has_in_flight_worker, inbox_watcher orphan
reaping, dashboard_api, ...). Most readers do
`json.loads(path.read_text())` inside `except (OSError, JSONDecodeError):
treat-as-absent`.

Before this fix the two writers truncate-wrote in place:
  - `_register_in_flight`  (raw `open(...,'w'); json.dump`)
  - `_mark_paused_on_tier1` (read-modify-write via `write_text`)
so a reader polling mid-write could see a partial file → JSONDecodeError →
"not in flight" → risk of re-dispatching a live (paid) task or GC'ing a
live worktree. Both now route through `atomic_io.atomic_write_json`
(temp+fsync+os.replace), so the destination is *always* either the intact
old file or the intact new file — never a torn one.

These tests assert atomic-replace semantics by forcing the publish step
(`os.replace`) to fail mid-write and verifying the destination is left
absent-or-complete with no stray temp file, plus the happy-path contents.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_inflight_atomic
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import agent_runner as ar  # noqa: E402


class _InFlightDirCase(unittest.TestCase):
    """Redirect IN_FLIGHT_DIR at a fresh temp dir per test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ar-inflight-atomic-'))
        self._orig_dir = ar.IN_FLIGHT_DIR
        ar.IN_FLIGHT_DIR = self.tmp

    def tearDown(self):
        ar.IN_FLIGHT_DIR = self._orig_dir

    def _siblings(self, name: str) -> list[str]:
        """Names in IN_FLIGHT_DIR — used to assert no leftover `.tmp`."""
        return sorted(p.name for p in self.tmp.iterdir())


class RegisterInFlightAtomicTest(_InFlightDirCase):
    def test_register_writes_expected_entry(self):
        ar._register_in_flight(task_stem='demo-task', agent_id='forge', pid=4321)
        written = json.loads((self.tmp / 'demo-task.json').read_text())
        self.assertEqual(written['task_stem'], 'demo-task')
        self.assertEqual(written['agent_id'], 'forge')
        self.assertEqual(written['pid'], 4321)
        self.assertIn('started_at', written)
        # No stray temp left behind.
        self.assertEqual(self._siblings('demo-task'), ['demo-task.json'])

    def test_no_partial_file_on_midwrite_failure_when_absent(self):
        """Mid-write crash with no prior file → destination stays ABSENT
        (never a truncated file that would still satisfy path.exists())."""
        dest = self.tmp / 'demo-task.json'
        # _register_in_flight swallows OSError (best-effort registration);
        # the point is the *destination* is never left torn.
        with mock.patch('atomic_io.os.replace',
                        side_effect=OSError('simulated crash')):
            ar._register_in_flight(task_stem='demo-task', agent_id='forge', pid=1)
        self.assertFalse(dest.exists(),
                         'destination must not exist after a failed first write')
        # The unique temp must have been cleaned up — dir is empty.
        self.assertEqual(self._siblings('demo-task'), [])

    def test_prior_entry_preserved_on_midwrite_failure(self):
        """Mid-write crash with a prior valid file → the OLD entry survives
        intact (atomic replace: either old-whole or new-whole, never torn)."""
        dest = self.tmp / 'demo-task.json'
        ar._register_in_flight(task_stem='demo-task', agent_id='forge', pid=100)
        original = json.loads(dest.read_text())
        with mock.patch('atomic_io.os.replace',
                        side_effect=OSError('simulated crash')):
            ar._register_in_flight(task_stem='demo-task', agent_id='forge', pid=200)
        # Old entry intact and still valid JSON; no torn read possible.
        self.assertEqual(json.loads(dest.read_text()), original)
        self.assertEqual(self._siblings('demo-task'), ['demo-task.json'])


class MarkPausedOnTier1AtomicTest(_InFlightDirCase):
    def test_marker_written_on_empty_file(self):
        ar._mark_paused_on_tier1(task_stem='demo-task', failure_type='quota',
                                 agent_id='forge', tier='tier1')
        written = json.loads((self.tmp / 'demo-task.json').read_text())
        self.assertIn('paused_on_tier1', written)
        self.assertEqual(written['paused_on_tier1']['failure_type'], 'quota')
        self.assertEqual(written['paused_on_tier1']['agent_id'], 'forge')
        self.assertEqual(written['paused_on_tier1']['tier'], 'tier1')
        self.assertEqual(self._siblings('demo-task'), ['demo-task.json'])

    def test_rmw_preserves_existing_sibling_fields(self):
        """The read-modify-write merges the marker into existing content
        rather than clobbering it — a prior sibling field survives."""
        dest = self.tmp / 'demo-task.json'
        dest.write_text(json.dumps({'task_stem': 'demo-task', 'pid': 999}))
        ar._mark_paused_on_tier1(task_stem='demo-task', failure_type='auth_401')
        written = json.loads(dest.read_text())
        # Pre-existing fields preserved...
        self.assertEqual(written['task_stem'], 'demo-task')
        self.assertEqual(written['pid'], 999)
        # ...and the new marker added.
        self.assertEqual(written['paused_on_tier1']['failure_type'], 'auth_401')

    def test_prior_content_preserved_on_midwrite_failure(self):
        """Mid-write crash during the RMW publish → the prior file is left
        intact (no torn read losing the whole entry)."""
        dest = self.tmp / 'demo-task.json'
        dest.write_text(json.dumps({'task_stem': 'demo-task', 'pid': 999}))
        original = json.loads(dest.read_text())
        with mock.patch('atomic_io.os.replace',
                        side_effect=OSError('simulated crash')):
            ar._mark_paused_on_tier1(task_stem='demo-task', failure_type='quota')
        self.assertEqual(json.loads(dest.read_text()), original)
        self.assertEqual(self._siblings('demo-task'), ['demo-task.json'])


if __name__ == '__main__':
    unittest.main()
