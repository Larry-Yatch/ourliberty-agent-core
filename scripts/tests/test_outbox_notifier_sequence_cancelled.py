#!/usr/bin/env python3
"""Tests for the build-abort auto-merge gate (board-abort-dispatched-build).

Guarantee under test: once a build sequence is ABORTED (the existing
`cancel sequence <id>` shortcut → `sequence_shortcut_helpers.apply_cancel`,
which sets `status: failed` + an `audit_log` event `cancelled`), its in-flight
PR must NOT be auto-merged. Previously a cancel stopped new step dispatch but
in-flight PRs could still auto-merge (build-sequence-orchestrator.md § cancel).

Covers `outbox_notifier._sequence_cancelled` (fail-open) and the two gate
checks that consume it: the shared `_attempt_auto_merge_with_gates` gate and
the review-pass `_run_review_pass_auto_merge` path.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_outbox_notifier_sequence_cancelled
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import outbox_notifier as on  # noqa: E402

_PR = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/9'


def _seq(seq_id, status, step_ids, cancelled=False):
    """Minimal sequence file matching apply_cancel's on-disk contract."""
    audit = [{'ts': '2026-06-20T00:00:00+00:00',
              'event': 'sequence-created', 'actor': 'beacon'}]
    if cancelled:  # exactly what apply_cancel appends
        audit.append({'ts': '2026-06-20T01:00:00+00:00',
                      'event': 'cancelled', 'actor': 'larry',
                      'reason': 'abort test'})
    return {
        'seq_id': seq_id,
        'status': status,
        'steps': [{'step_id': sid, 'status': 'dispatched'} for sid in step_ids],
        'current_steps': [],
        'audit_log': audit,
    }


class _Harness(unittest.TestCase):
    """Reroute outbox_notifier's roots to a per-test tmpdir — no write reaches
    ~/agents/ on the droplet (test-isolation discipline, PR #137)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._root = Path(self._tmp.name)
        self._orig = {n: getattr(on, n)
                      for n in ('AGENTS_ROOT', 'BLACKBOARD', 'LOG_FILE')}
        on.AGENTS_ROOT = self._root
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        self.seq_dir = self._root / 'blackboard' / 'build-sequences'
        self.seq_dir.mkdir(parents=True, exist_ok=True)
        (self._root / 'logs').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(on, n, v)

    def _write(self, seq):
        (self.seq_dir / f"{seq['seq_id']}.json").write_text(json.dumps(seq))


class TestSequenceCancelledHelper(_Harness):
    def test_cancelled_sequence_detected(self):
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=True))
        self.assertTrue(on._sequence_cancelled('seq-s1-step-a'))

    def test_active_sequence_not_cancelled(self):
        self._write(_seq('s1', 'active', ['seq-s1-step-a']))
        self.assertFalse(on._sequence_cancelled('seq-s1-step-a'))

    def test_failed_from_build_error_is_not_cancelled(self):
        # status 'failed' WITHOUT a 'cancelled' audit (a build error, not an
        # operator abort) must NOT block the merge — cancel != failed.
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=False))
        self.assertFalse(on._sequence_cancelled('seq-s1-step-a'))

    def test_unknown_task_id_not_cancelled(self):
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=True))
        self.assertFalse(on._sequence_cancelled('seq-OTHER-step-z'))

    def test_no_sequences_dir_fail_open(self):
        shutil.rmtree(self.seq_dir)
        self.assertFalse(on._sequence_cancelled('seq-s1-step-a'))

    def test_none_and_empty_task_id(self):
        self.assertFalse(on._sequence_cancelled(None))
        self.assertFalse(on._sequence_cancelled(''))

    def test_malformed_sibling_does_not_break_scan(self):
        # An unreadable sibling must not crash the scan or hide a real cancel.
        (self.seq_dir / 'broken.json').write_text('{not valid json')
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=True))
        self.assertTrue(on._sequence_cancelled('seq-s1-step-a'))


class TestAutoMergeGateBlocksCancelled(_Harness):
    def test_shared_gate_skips_cancelled_without_merging(self):
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=True))
        with mock.patch.object(on, '_auto_merge_pr') as merge_fn:
            result = on._attempt_auto_merge_with_gates(
                pr_url=_PR,
                repo_coords='Larry-Yatch/ourliberty-agent-core',
                pr_number=9, task_id='seq-s1-step-a',
                summary='', chat_id=None, changed_files=None,
            )
        merge_fn.assert_not_called()
        self.assertEqual(result['merge_outcome'], 'skipped_sequence_cancelled')

    def test_cancelled_check_beats_test_bypass(self):
        # The cancelled gate sits BEFORE the serializer test-bypass, so a
        # cancelled seq is never merged even when the bypass + an override
        # merge fn are installed. This is the single most security-relevant
        # ordering in the change — lock it with a permanent regression test.
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=True))
        merge_fn = mock.Mock(return_value={'merge_outcome': 'merged'})
        with mock.patch.object(
                    on, '_AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST', True), \
                mock.patch.object(on, '_AUTO_MERGE_FN_OVERRIDE', merge_fn):
            result = on._attempt_auto_merge_with_gates(
                pr_url=_PR,
                repo_coords='Larry-Yatch/ourliberty-agent-core',
                pr_number=9, task_id='seq-s1-step-a',
                summary='', chat_id=None, changed_files=None,
            )
        merge_fn.assert_not_called()
        self.assertEqual(result['merge_outcome'], 'skipped_sequence_cancelled')

    def test_review_pass_path_skips_cancelled(self):
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=True))
        data = {'task_id': 'seq-s1-step-a', 'pr_url': _PR}
        marker_decision = {'payload': {'pr_url': _PR}}
        with mock.patch.object(on, '_auto_merge_pr') as merge_fn, \
                mock.patch.object(
                    on, '_attempt_auto_merge_with_gates') as gate_fn:
            ret = on._run_review_pass_auto_merge(
                data, marker_decision, self._root / 'outbox.json')
        self.assertEqual(ret, 'auto-merge-skipped')
        merge_fn.assert_not_called()
        gate_fn.assert_not_called()  # short-circuits before the gate/gh calls
        self.assertEqual(
            marker_decision['merge_outcome'], 'skipped_sequence_cancelled')


if __name__ == '__main__':
    unittest.main()
