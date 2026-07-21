#!/usr/bin/env python3
"""Tests for `task_cancel` — the in-flight cancel-marker contract shared by
`agent_runner` (reader) and `sequence_shortcut_helpers.apply_cancel` (writer).

The point of this module is that the two sides can't drift, so the tests that
matter are the round-trip ones: what the writer writes, the reader must read.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_task_cancel
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
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import task_cancel  # noqa: E402


class CancelMarkerPathTests(unittest.TestCase):

    def test_path_shape_matches_what_agent_runner_polls_for(self):
        root = Path('/tmp/agents')
        self.assertEqual(
            task_cancel.cancel_marker_path(root, 'my-task-001'),
            root / 'blackboard' / 'cancel-task-my-task-001.json')

    def test_stem_is_used_verbatim_not_sanitized(self):
        # agent_runner interpolates the RAW task_stem. If this helper slugified
        # it, the writer and reader would look at different files and the
        # cancel would silently never fire.
        root = Path('/tmp/agents')
        for stem in ('Task_With Spaces', 'a:b', 'UPPER-case', 'x' * 80):
            with self.subTest(stem=stem):
                self.assertEqual(
                    task_cancel.cancel_marker_path(root, stem).name,
                    f'cancel-task-{stem}.json')


class SafeStemTests(unittest.TestCase):

    def test_accepts_real_task_ids(self):
        for stem in (
            'route-ourliberty-graph-prs-to-mirror-001',
            'p3-launch-queue-drain',
            'step_1',
            'a:b',
        ):
            with self.subTest(stem=stem):
                self.assertTrue(task_cancel.is_safe_task_stem(stem))

    def test_rejects_traversal_and_junk(self):
        for stem in (
            '../../state/pwned', 'a/b', 'a\\b', '.hidden', '..', '',
            None, 42, [], {},
        ):
            with self.subTest(stem=stem):
                self.assertFalse(task_cancel.is_safe_task_stem(stem))


class RoundTripTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_returns_the_reason(self):
        self.assertTrue(task_cancel.request_cancel(
            self.root, 's1', reason='wrong direction', actor='larry'))
        self.assertEqual(
            task_cancel.is_cancel_requested(self.root, 's1'),
            'wrong direction')

    def test_absent_marker_reads_as_none(self):
        self.assertIsNone(task_cancel.is_cancel_requested(self.root, 'nope'))

    def test_default_reason_when_none_given(self):
        task_cancel.request_cancel(self.root, 's1')
        self.assertEqual(
            task_cancel.is_cancel_requested(self.root, 's1'),
            'cancelled by request')

    def test_payload_shape(self):
        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        task_cancel.request_cancel(
            self.root, 's1', reason='r', actor='beacon', now=now)
        body = json.loads(
            task_cancel.cancel_marker_path(self.root, 's1').read_text())
        self.assertEqual(body, {
            'reason': 'r', 'actor': 'beacon',
            'requested_at': '2026-07-21T12:00:00+00:00',
        })

    def test_creates_the_blackboard_dir_if_missing(self):
        # A fresh agents root (or a test tmpdir) has no blackboard/ yet; the
        # write must not fail on that.
        self.assertFalse((self.root / 'blackboard').exists())
        self.assertTrue(task_cancel.request_cancel(self.root, 's1'))

    def test_re_request_overwrites_with_the_newer_reason(self):
        task_cancel.request_cancel(self.root, 's1', reason='first')
        task_cancel.request_cancel(self.root, 's1', reason='second')
        self.assertEqual(
            task_cancel.is_cancel_requested(self.root, 's1'), 'second')

    def test_unsafe_stem_writes_nothing_and_returns_false(self):
        self.assertFalse(
            task_cancel.request_cancel(self.root, '../../state/pwned'))
        self.assertFalse((self.root / 'state').exists())

    def test_clear_removes_the_marker_and_is_idempotent(self):
        task_cancel.request_cancel(self.root, 's1')
        task_cancel.clear_cancel(self.root, 's1')
        self.assertIsNone(task_cancel.is_cancel_requested(self.root, 's1'))
        task_cancel.clear_cancel(self.root, 's1')  # must not raise

    def test_malformed_marker_still_counts_as_a_cancel(self):
        # Fail TOWARD stopping: a marker on disk means a human asked for this
        # to stop, and a JSON typo must not keep the worker alive. Matches
        # agent_runner._check_cancel's pre-existing tolerance.
        path = task_cancel.cancel_marker_path(self.root, 's1')
        path.parent.mkdir(parents=True, exist_ok=True)
        for junk in ('{not json', '', '[]', 'null', '"a string"'):
            with self.subTest(body=junk):
                path.write_text(junk)
                self.assertIsNotNone(
                    task_cancel.is_cancel_requested(self.root, 's1'))

    def test_marker_without_a_reason_key_reads_as_the_default(self):
        path = task_cancel.cancel_marker_path(self.root, 's1')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'actor': 'larry'}))
        self.assertEqual(
            task_cancel.is_cancel_requested(self.root, 's1'),
            'cancelled by request')

    def test_write_failure_fails_quiet(self):
        # The caller has already made the durable guarantee (sequence failed →
        # auto-merge blocked); a marker we can't write costs tokens, not
        # correctness, so it must return False rather than raise.
        blocked = self.root / 'blackboard'
        blocked.write_text('i am a file, not a directory')
        self.assertFalse(task_cancel.request_cancel(self.root, 's1'))


class AgentRunnerReaderParityTests(unittest.TestCase):
    """`agent_runner` is the only consumer that matters — pin that its wrappers
    resolve to the same file this module writes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)

    def tearDown(self):
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        self._tmp.cleanup()

    def test_agent_runner_reads_what_this_module_writes(self):
        import agent_runner  # noqa: PLC0415 — imported late, after env is set
        original = agent_runner.AGENTS_ROOT
        agent_runner.AGENTS_ROOT = self.root
        try:
            task_cancel.request_cancel(self.root, 's1', reason='stop it')
            self.assertEqual(agent_runner._check_cancel('s1'), 'stop it')
            agent_runner._clear_cancel('s1')
            self.assertIsNone(agent_runner._check_cancel('s1'))
        finally:
            agent_runner.AGENTS_ROOT = original


if __name__ == '__main__':
    unittest.main()
