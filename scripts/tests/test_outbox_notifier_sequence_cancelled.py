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


class TestQueueReleaseHonoursCancel(_Harness):
    """609e item 3 — the RELEASE path.

    A PR held behind a blocker is re-gated when that blocker merges. If the
    operator stopped the build while it sat in the queue, the release must NOT
    merge it. This path never ran through the review-pass gate — it re-enters
    via `_attempt_auto_merge_with_gates` — so it needs its own coverage.
    """

    def _queue_entry(self):
        return {
            'pr_url': _PR, 'pr_number': 9,
            'repo': 'Larry-Yatch/ourliberty-agent-core',
            'task_id': 'seq-s1-step-a', 'summary': 's',
            'blocker_pr_number': 4, 'reply_chat_id': None,
            'changed_files': [], 'queued_at': '2026-07-21T00:00:00+00:00',
        }

    def test_release_of_a_stopped_build_does_not_merge(self):
        self._write(_seq('s1', 'failed', ['seq-s1-step-a'], cancelled=True))
        with mock.patch.object(
                    on, '_load_auto_merge_queue',
                    return_value=[self._queue_entry()]), \
                mock.patch.object(on, '_queue_remove_pr'), \
                mock.patch.object(on, '_auto_merge_pr') as merge_fn, \
                mock.patch.object(on, '_fire_review_pass_outcome_dm') as dm:
            on._queue_release(4, 'Larry-Yatch/ourliberty-agent-core')
        merge_fn.assert_not_called()
        # The release still closes the loop with Larry — a silent drop would
        # strand the entry with no final word after its "queued behind #4" DM.
        dm.assert_called_once()
        self.assertEqual(
            dm.call_args[0][1].get('merge_outcome'),
            'skipped_sequence_cancelled')

    def test_release_of_a_live_build_still_merges(self):
        # The gate must not become a blanket refusal on the release path.
        self._write(_seq('s1', 'active', ['seq-s1-step-a'], cancelled=False))
        merge_fn = mock.Mock(return_value={'merge_outcome': 'merged'})
        with mock.patch.object(
                    on, '_load_auto_merge_queue',
                    return_value=[self._queue_entry()]), \
                mock.patch.object(on, '_queue_remove_pr'), \
                mock.patch.object(
                    on, '_AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST', True), \
                mock.patch.object(on, '_AUTO_MERGE_FN_OVERRIDE', merge_fn), \
                mock.patch.object(on, '_fire_review_pass_outcome_dm'):
            on._queue_release(4, 'Larry-Yatch/ourliberty-agent-core')
        merge_fn.assert_called_once()


class TestCancelledWordingIsPlainLanguage(_Harness):
    """609e item 2 — both surfaces must SAY what happened.

    Without their own entries, `skipped_sequence_cancelled` fell through to the
    generic review-pass DM body (which only says Mirror approved) and to the
    notify's default line ("REQUESTED; outcome in Larry's DM"). Both imply
    something is still coming. This outcome is terminal by intent: nothing will
    retry it, and the reader has to know that.
    """

    _OUTCOME = 'skipped_sequence_cancelled'

    def test_dm_variant_exists_and_says_stopped_and_not_retrying(self):
        body = on._REVIEW_PASS_DM_VARIANTS.get(self._OUTCOME)
        self.assertIsNotNone(body, 'no DM variant — falls back to the generic '
                                   'review-pass body that never mentions the stop')
        lowered = body.lower()
        self.assertIn('stopped', lowered)
        self.assertIn('not retry', lowered)
        # Must never imply the merge happened.
        self.assertNotIn('auto-merged', lowered)

    def test_dm_variant_renders_with_the_standard_fields(self):
        # The variants are .format()-ed against the marker decision; a typo'd
        # placeholder would KeyError mid-merge on the daemon.
        rendered = on._REVIEW_PASS_DM_VARIANTS[self._OUTCOME].format(
            pr_url=_PR, task_id='seq-s1-step-a', summary='did a thing',
            pr_number=9, repo_coords='Larry-Yatch/ourliberty-agent-core',
            merge_reason='', blocker_pr_number='', overlap_files='',
            regression_detail='',
        )
        self.assertIn(_PR, rendered)
        self.assertIn('seq-s1-step-a', rendered)

    def test_notify_line_says_refused_and_will_not_retry(self):
        line = on._render_review_pass_merge_status_line(
            {'merge_outcome': self._OUTCOME})
        lowered = line.lower()
        self.assertIn('not merged', lowered)
        self.assertIn('not retry', lowered)
        # The generic fallback would claim it was merely "REQUESTED".
        self.assertNotIn('requested', lowered)

    def test_no_raw_enum_leaks_into_either_surface(self):
        # The whole point of item 2: Larry reads these, not the enum.
        for text in (on._REVIEW_PASS_DM_VARIANTS[self._OUTCOME],
                     on._render_review_pass_merge_status_line(
                         {'merge_outcome': self._OUTCOME})):
            self.assertNotIn('skipped_sequence_cancelled', text)
            self.assertNotIn('sequence_cancelled', text)

    def test_merged_still_reads_as_merged(self):
        # Guard against the new branch shadowing the success path.
        self.assertIn(
            'MERGED',
            on._render_review_pass_merge_status_line(
                {'merge_outcome': 'merged'}))


if __name__ == '__main__':
    unittest.main()
