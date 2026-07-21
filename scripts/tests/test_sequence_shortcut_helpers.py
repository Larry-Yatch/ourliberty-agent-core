#!/usr/bin/env python3
"""Behavior tests for `scripts/sequence_shortcut_helpers.py` (PR-S4
rectification M1 + L3).

Replaces the aspirational `ShortcutMutationShapes` class in
`test_outbox_notifier_sequence_handlers.py`. Those tests asserted shape
without executing the mutation. With the real helpers in place, each
shortcut gets a happy-path, idempotency, atomic-write-correctness, and
audit_log-shape test.

Test isolation discipline (PR #137): every test reroutes
`sequence_shortcut_helpers.AGENTS_ROOT` to a per-test tmpdir. NO writes
ever reach `~/agents/` (real droplet state). The AST-walk leak gate
(`test_no_production_path_leaks.py`) enforces this — see the canonical
fix string in that file's docstring for the pattern.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_sequence_shortcut_helpers
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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import build_sequence_validator as bsv  # noqa: E402
import sequence_shortcut_helpers as ssh  # noqa: E402
import task_cancel  # noqa: E402


def _make_step(step_id, deps=None, status='pending', pr_url=None,
               dispatched_at=None, merged_at=None):
    return {
        'step_id': step_id,
        'label': f'Step {step_id}',
        'depends_on': deps or [],
        'dispatch_text': (
            f'Build {step_id} per spec § X. Review focus: Y.'
        ),
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'feature-development',
        'status': status,
        'dispatched_at': dispatched_at,
        'merged_at': merged_at,
        'pr_url': pr_url,
        'current_actor': None,
        'failure_reason': None,
    }


def _make_sequence(seq_id='helper-test-001', status='active',
                   steps=None, current_steps=None, audit_log=None):
    if steps is None:
        steps = [
            _make_step('s1', deps=[]),
            _make_step('s2', deps=['s1']),
        ]
    return {
        'seq_id': seq_id,
        'label': f'Sequence {seq_id}',
        'spec_doc': 'agents/beacon/specs/build-sequence-orchestrator.md',
        'created_at': '2026-05-27T00:00:00+00:00',
        'created_by': 'beacon',
        'status': status,
        'current_steps': current_steps if current_steps is not None else [],
        'steps': steps,
        'audit_log': audit_log if audit_log is not None else [
            {'ts': '2026-05-27T00:00:00+00:00', 'event': 'sequence-created',
             'actor': 'beacon'},
        ],
    }


class _HelpersHarness(unittest.TestCase):
    """Reroute `sequence_shortcut_helpers.AGENTS_ROOT` to a per-test
    tmpdir so no test write reaches `~/agents/` on the droplet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._ssh_original = ssh.AGENTS_ROOT
        ssh.AGENTS_ROOT = self._root
        (self._root / 'blackboard' / 'build-sequences').mkdir(
            parents=True, exist_ok=True,
        )

    def tearDown(self):
        ssh.AGENTS_ROOT = self._ssh_original
        self._tmp.cleanup()

    def _write_sequence(self, seq):
        path = (
            ssh.AGENTS_ROOT / 'blackboard' / 'build-sequences'
            / f'{seq["seq_id"]}.json'
        )
        path.write_text(json.dumps(seq, indent=2))
        return path

    def _read_sequence(self, seq_id):
        path = (
            ssh.AGENTS_ROOT / 'blackboard' / 'build-sequences'
            / f'{seq_id}.json'
        )
        return json.loads(path.read_text())


# ============================================================================
# pause
# ============================================================================


class ApplyPauseTests(_HelpersHarness):

    def test_happy_path_active_to_paused(self):
        seq = _make_sequence(seq_id='p-active', status='active')
        self._write_sequence(seq)
        result = ssh.apply_pause('p-active', actor='larry')
        self.assertTrue(result.applied)
        self.assertFalse(result.error)
        on_disk = self._read_sequence('p-active')
        self.assertEqual(on_disk['status'], 'paused')
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'paused')
        self.assertEqual(last['actor'], 'larry')
        self.assertIn('ts', last)
        # Validator still passes.
        v = bsv.validate_dag(on_disk)
        self.assertTrue(v.valid, v.errors)

    def test_idempotent_when_already_paused(self):
        seq = _make_sequence(seq_id='p-idem', status='paused')
        self._write_sequence(seq)
        before = self._read_sequence('p-idem')
        result = ssh.apply_pause('p-idem', actor='larry')
        self.assertFalse(result.applied)
        self.assertFalse(result.error)
        self.assertIn('already paused', result.reason)
        # On-disk file unchanged.
        self.assertEqual(self._read_sequence('p-idem'), before)

    def test_no_op_on_terminal_status(self):
        seq = _make_sequence(seq_id='p-terminal', status='complete')
        self._write_sequence(seq)
        before = self._read_sequence('p-terminal')
        result = ssh.apply_pause('p-terminal')
        self.assertFalse(result.applied)
        # On-disk unchanged.
        self.assertEqual(self._read_sequence('p-terminal'), before)

    def test_missing_file_returns_error(self):
        result = ssh.apply_pause('does-not-exist')
        self.assertFalse(result.applied)
        self.assertTrue(result.error)
        self.assertIn('not found', result.reason)


# ============================================================================
# resume
# ============================================================================


class ApplyResumeTests(_HelpersHarness):

    def test_happy_path_paused_to_active(self):
        seq = _make_sequence(seq_id='r-paused', status='paused')
        self._write_sequence(seq)
        result = ssh.apply_resume('r-paused', actor='larry')
        self.assertTrue(result.applied)
        on_disk = self._read_sequence('r-paused')
        self.assertEqual(on_disk['status'], 'active')
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'resumed')
        self.assertEqual(last['actor'], 'larry')

    def test_idempotent_when_already_active(self):
        seq = _make_sequence(seq_id='r-active', status='active')
        self._write_sequence(seq)
        before = self._read_sequence('r-active')
        result = ssh.apply_resume('r-active')
        self.assertFalse(result.applied)
        self.assertEqual(self._read_sequence('r-active'), before)

    def test_resume_from_pending_is_noop_with_hint(self):
        seq = _make_sequence(seq_id='r-pending', status='pending')
        self._write_sequence(seq)
        result = ssh.apply_resume('r-pending')
        self.assertFalse(result.applied)
        self.assertIn('kicked off', result.reason.lower())

    def test_resume_from_terminal_is_noop(self):
        seq = _make_sequence(seq_id='r-failed', status='failed')
        self._write_sequence(seq)
        result = ssh.apply_resume('r-failed')
        self.assertFalse(result.applied)


# ============================================================================
# cancel
# ============================================================================


class ApplyCancelTests(_HelpersHarness):

    def test_happy_path_active_to_failed_with_reason(self):
        seq = _make_sequence(seq_id='c-active', status='active')
        self._write_sequence(seq)
        result = ssh.apply_cancel(
            'c-active', actor='larry', reason='wrong direction',
        )
        self.assertTrue(result.applied)
        on_disk = self._read_sequence('c-active')
        self.assertEqual(on_disk['status'], 'failed')
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'cancelled')
        self.assertEqual(last['actor'], 'larry')
        self.assertEqual(last['reason'], 'wrong direction')

    def test_cancel_without_reason_omits_field(self):
        seq = _make_sequence(seq_id='c-no-reason', status='active')
        self._write_sequence(seq)
        result = ssh.apply_cancel('c-no-reason', actor='larry')
        self.assertTrue(result.applied)
        last = self._read_sequence('c-no-reason')['audit_log'][-1]
        self.assertEqual(last['event'], 'cancelled')
        self.assertNotIn('reason', last)

    def test_idempotent_when_already_failed(self):
        seq = _make_sequence(seq_id='c-idem', status='failed')
        self._write_sequence(seq)
        before = self._read_sequence('c-idem')
        result = ssh.apply_cancel('c-idem')
        self.assertFalse(result.applied)
        self.assertEqual(self._read_sequence('c-idem'), before)

    def test_terminal_states_are_noop(self):
        for status in ('complete', 'archived'):
            with self.subTest(status=status):
                seq = _make_sequence(seq_id=f'c-{status}', status=status)
                self._write_sequence(seq)
                before = self._read_sequence(f'c-{status}')
                result = ssh.apply_cancel(f'c-{status}')
                self.assertFalse(result.applied)
                self.assertEqual(self._read_sequence(f'c-{status}'), before)

    # ---- cancel must STOP the work, not just the bookkeeping ----

    def _marker(self, step_id):
        return task_cancel.cancel_marker_path(self._root, step_id)

    def test_cancel_requests_a_stop_for_every_in_flight_step(self):
        seq = _make_sequence(
            seq_id='c-inflight', status='active', current_steps=['s1', 's2'])
        self._write_sequence(seq)
        result = ssh.apply_cancel(
            'c-inflight', actor='larry', reason='wrong direction')
        self.assertTrue(result.applied)
        for step_id in ('s1', 's2'):
            with self.subTest(step_id=step_id):
                path = self._marker(step_id)
                self.assertTrue(path.exists(), f'no cancel marker for {step_id}')
                body = json.loads(path.read_text())
                self.assertEqual(body['reason'], 'wrong direction')
                self.assertEqual(body['actor'], 'larry')
                self.assertIn('requested_at', body)
        # agent_runner must read back exactly what we wrote.
        self.assertEqual(
            task_cancel.is_cancel_requested(self._root, 's1'),
            'wrong direction')
        self.assertIn('2 in-flight step(s)', result.reason)

    def test_cancel_without_reason_still_stops_the_work(self):
        seq = _make_sequence(
            seq_id='c-noreason-stop', status='active', current_steps=['s1'])
        self._write_sequence(seq)
        ssh.apply_cancel('c-noreason-stop', actor='larry')
        self.assertEqual(
            task_cancel.is_cancel_requested(self._root, 's1'),
            'cancelled by request')

    def test_no_in_flight_steps_writes_no_markers(self):
        seq = _make_sequence(
            seq_id='c-idle', status='active', current_steps=[])
        self._write_sequence(seq)
        result = ssh.apply_cancel('c-idle')
        self.assertTrue(result.applied)
        self.assertNotIn('in-flight', result.reason)
        blackboard = self._root / 'blackboard'
        self.assertEqual(
            [p.name for p in blackboard.glob('cancel-task-*.json')], [])

    def test_noop_cancel_does_not_stop_anything(self):
        # Already-failed / terminal sequences short-circuit BEFORE the stop
        # request — re-cancelling must not kill a worker that a later retry or
        # an unrelated re-dispatch legitimately started on that step id.
        for status in ('failed', 'complete', 'archived'):
            with self.subTest(status=status):
                seq = _make_sequence(
                    seq_id=f'c-noop-{status}', status=status,
                    current_steps=['s1'])
                self._write_sequence(seq)
                result = ssh.apply_cancel(f'c-noop-{status}')
                self.assertFalse(result.applied)
                self.assertFalse(self._marker('s1').exists())

    def test_unsafe_step_id_is_refused_not_sanitized(self):
        # A path-traversing step id must never write outside the blackboard.
        # Refused rather than sanitized: agent_runner polls the RAW stem, so a
        # sanitized filename would be a marker nobody ever reads.
        seq = _make_sequence(
            seq_id='c-unsafe', status='active',
            current_steps=['../../state/pwned', 'ok-step'])
        self._write_sequence(seq)
        result = ssh.apply_cancel('c-unsafe')
        self.assertTrue(result.applied)
        self.assertFalse((self._root / 'state' / 'pwned.json').exists())
        self.assertTrue(self._marker('ok-step').exists())
        self.assertIn('1 in-flight step(s)', result.reason)

    def test_marker_failure_never_costs_the_status_flip(self):
        # The durable guarantee is status=failed (it blocks auto-merge). A
        # marker write blowing up must not undo or error that.
        seq = _make_sequence(
            seq_id='c-writefail', status='active', current_steps=['s1'])
        self._write_sequence(seq)
        original = task_cancel.request_cancel

        def _boom(*_a, **_k):
            raise OSError('disk full')

        task_cancel.request_cancel = _boom
        try:
            result = ssh.apply_cancel('c-writefail')
        finally:
            task_cancel.request_cancel = original
        self.assertTrue(result.applied)
        self.assertEqual(self._read_sequence('c-writefail')['status'], 'failed')

    def test_malformed_current_steps_is_tolerated(self):
        for bad in ('not-a-list', None, 42):
            with self.subTest(current_steps=bad):
                seq = _make_sequence(seq_id='c-bad', status='active')
                seq['current_steps'] = bad
                self._write_sequence(seq)
                result = ssh.apply_cancel('c-bad')
                self.assertTrue(result.applied)
                self.assertEqual(
                    self._read_sequence('c-bad')['status'], 'failed')


# ============================================================================
# retry
# ============================================================================


class ApplyRetryTests(_HelpersHarness):

    def test_happy_path_failed_step_to_pending(self):
        seq = _make_sequence(
            seq_id='rt-seq',
            status='paused',
            steps=[
                _make_step('a', deps=[], status='merged',
                           merged_at='2026-05-27T01:00:00Z',
                           pr_url='https://example.com/pr/1'),
                _make_step('b', deps=['a'], status='failed',
                           dispatched_at='2026-05-27T02:00:00Z',
                           pr_url='https://example.com/pr/2'),
            ],
            current_steps=['b'],
        )
        self._write_sequence(seq)
        result = ssh.apply_retry('rt-seq', 'b', actor='larry')
        self.assertTrue(result.applied)
        on_disk = self._read_sequence('rt-seq')
        b = next(s for s in on_disk['steps'] if s['step_id'] == 'b')
        self.assertEqual(b['status'], 'pending')
        self.assertIsNone(b['dispatched_at'])
        self.assertIsNone(b['pr_url'])
        self.assertIsNone(b['current_actor'])
        self.assertIsNone(b['failure_reason'])
        self.assertIsNone(b['merged_at'])
        self.assertNotIn('b', on_disk['current_steps'])
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'step-retried')
        self.assertEqual(last['step_id'], 'b')
        self.assertEqual(last['actor'], 'larry')
        # Validator still passes after retry.
        v = bsv.validate_dag(on_disk)
        self.assertTrue(v.valid, v.errors)

    def test_idempotent_when_step_already_pending(self):
        seq = _make_sequence(
            seq_id='rt-idem',
            status='paused',
            steps=[_make_step('a', status='pending')],
        )
        self._write_sequence(seq)
        before = self._read_sequence('rt-idem')
        result = ssh.apply_retry('rt-idem', 'a')
        self.assertFalse(result.applied)
        self.assertFalse(result.error)
        self.assertEqual(self._read_sequence('rt-idem'), before)

    def test_merged_step_is_immutable(self):
        seq = _make_sequence(
            seq_id='rt-merged',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z',
                           pr_url='https://example.com/pr/1'),
            ],
        )
        self._write_sequence(seq)
        before = self._read_sequence('rt-merged')
        result = ssh.apply_retry('rt-merged', 'a')
        self.assertFalse(result.applied)
        self.assertEqual(self._read_sequence('rt-merged'), before)

    def test_unknown_step_is_error(self):
        seq = _make_sequence(seq_id='rt-nostep', status='active')
        self._write_sequence(seq)
        result = ssh.apply_retry('rt-nostep', 'nonexistent')
        self.assertFalse(result.applied)
        self.assertTrue(result.error)


# ============================================================================
# skip
# ============================================================================


class ApplySkipTests(_HelpersHarness):

    def test_happy_path_failed_step_to_merged(self):
        seq = _make_sequence(
            seq_id='sk-seq',
            status='active',
            steps=[
                _make_step('a', deps=[], status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
                _make_step('b', deps=['a'], status='failed'),
            ],
            current_steps=['b'],
        )
        self._write_sequence(seq)
        result = ssh.apply_skip(
            'sk-seq', 'b', actor='larry',
            reason='Work done out-of-band via hotfix',
        )
        self.assertTrue(result.applied)
        on_disk = self._read_sequence('sk-seq')
        b = next(s for s in on_disk['steps'] if s['step_id'] == 'b')
        # Per spec § 5.4 + preflight Q4: skip uses `merged`, NOT a new
        # `skipped` step status.
        self.assertEqual(b['status'], 'merged')
        self.assertIsNotNone(b['merged_at'])
        # The skip flips the last non-terminal step in this fixture, so
        # finalization also appends a sequence-complete entry after the
        # step-skipped one. Find step-skipped by event, not by position.
        skipped = next(
            e for e in on_disk['audit_log'] if e['event'] == 'step-skipped'
        )
        self.assertEqual(skipped['step_id'], 'b')
        self.assertEqual(skipped['actor'], 'larry')
        self.assertEqual(skipped['reason'], 'Work done out-of-band via hotfix')
        # `skipped` is NOT a valid step status per the schema enum.
        self.assertNotIn('skipped', bsv.VALID_STEP_STATUS)
        v = bsv.validate_dag(on_disk)
        self.assertTrue(v.valid, v.errors)

    def test_skip_without_reason_omits_field(self):
        seq = _make_sequence(
            seq_id='sk-no-reason',
            status='active',
            steps=[
                _make_step('a', status='failed'),
            ],
        )
        self._write_sequence(seq)
        result = ssh.apply_skip('sk-no-reason', 'a', actor='larry')
        self.assertTrue(result.applied)
        skipped = next(
            e for e in self._read_sequence('sk-no-reason')['audit_log']
            if e['event'] == 'step-skipped'
        )
        self.assertNotIn('reason', skipped)

    def test_idempotent_when_step_already_merged(self):
        seq = _make_sequence(
            seq_id='sk-idem',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
            ],
        )
        self._write_sequence(seq)
        before = self._read_sequence('sk-idem')
        result = ssh.apply_skip('sk-idem', 'a')
        self.assertFalse(result.applied)
        self.assertEqual(self._read_sequence('sk-idem'), before)

    def test_unknown_step_is_error(self):
        seq = _make_sequence(seq_id='sk-nostep', status='active')
        self._write_sequence(seq)
        result = ssh.apply_skip('sk-nostep', 'nonexistent')
        self.assertFalse(result.applied)
        self.assertTrue(result.error)


# ============================================================================
# Cross-cutting: atomic-write correctness + schema invariance
# ============================================================================


class AtomicWriteCorrectness(_HelpersHarness):
    """The helpers use tmp + os.replace. A failure mid-write must NOT
    corrupt the canonical file. We monkeypatch `os.replace` in the
    helpers module to raise OSError and confirm the on-disk file is
    unchanged + the helper surfaces an error Result."""

    def test_pause_failed_replace_leaves_file_untouched(self):
        import os as _os
        seq = _make_sequence(seq_id='atom-pause', status='active')
        path = self._write_sequence(seq)
        before = path.read_text()

        original_replace = ssh.os.replace

        def _fail(src, dst):
            raise OSError('simulated replace failure')

        ssh.os.replace = _fail
        try:
            result = ssh.apply_pause('atom-pause')
        finally:
            ssh.os.replace = original_replace

        self.assertFalse(result.applied)
        self.assertTrue(result.error)
        self.assertIn('write failed', result.reason.lower())
        # Canonical sequence file byte-identical.
        self.assertEqual(path.read_text(), before)
        # No leftover .tmp file.
        tmp = path.with_suffix(path.suffix + '.tmp')
        self.assertFalse(
            tmp.exists(),
            'helper must clean up the partial .tmp on write failure',
        )


class SchemaInvariantAfterMutation(_HelpersHarness):
    """Across every helper, the PR-S2 schema must hold: top-level
    fields stay within REQUIRED_SEQ_FIELDS, status within
    VALID_SEQUENCE_STATUS, step status within VALID_STEP_STATUS, and
    the validator's `validate_dag` returns valid=True."""

    def _assert_schema(self, seq):
        self.assertIn(seq['status'], bsv.VALID_SEQUENCE_STATUS)
        top_level_extra = set(seq.keys()) - set(bsv.REQUIRED_SEQ_FIELDS)
        self.assertEqual(top_level_extra, set())
        for step in seq['steps']:
            self.assertIn(step['status'], bsv.VALID_STEP_STATUS)
        v = bsv.validate_dag(seq)
        self.assertTrue(v.valid, v.errors)

    def test_pause_preserves_schema(self):
        seq = _make_sequence(seq_id='inv-pause', status='active')
        self._write_sequence(seq)
        ssh.apply_pause('inv-pause')
        self._assert_schema(self._read_sequence('inv-pause'))

    def test_resume_preserves_schema(self):
        seq = _make_sequence(seq_id='inv-resume', status='paused')
        self._write_sequence(seq)
        ssh.apply_resume('inv-resume')
        self._assert_schema(self._read_sequence('inv-resume'))

    def test_cancel_preserves_schema(self):
        seq = _make_sequence(seq_id='inv-cancel', status='active')
        self._write_sequence(seq)
        ssh.apply_cancel('inv-cancel', reason='test')
        self._assert_schema(self._read_sequence('inv-cancel'))

    def test_retry_preserves_schema(self):
        seq = _make_sequence(
            seq_id='inv-retry',
            status='paused',
            steps=[
                _make_step('a', deps=[], status='merged',
                           merged_at='2026-05-27T01:00:00Z',
                           pr_url='https://example.com/pr/1'),
                _make_step('b', deps=['a'], status='failed'),
            ],
            current_steps=['b'],
        )
        self._write_sequence(seq)
        ssh.apply_retry('inv-retry', 'b')
        self._assert_schema(self._read_sequence('inv-retry'))

    def test_skip_preserves_schema(self):
        seq = _make_sequence(
            seq_id='inv-skip',
            status='active',
            steps=[_make_step('a', status='failed')],
        )
        self._write_sequence(seq)
        ssh.apply_skip('inv-skip', 'a')
        self._assert_schema(self._read_sequence('inv-skip'))

    def test_step_merged_preserves_schema(self):
        seq = _make_sequence(
            seq_id='inv-step-merged',
            status='active',
            steps=[_make_step('a', status='dispatched',
                              dispatched_at='2026-05-27T00:30:00Z')],
            current_steps=['a'],
        )
        self._write_sequence(seq)
        ssh.apply_step_merged(
            seq_id='inv-step-merged',
            step_id='a',
            pr_url='https://github.com/x/y/pull/1',
            merged_at='2026-05-27T01:00:00Z',
        )
        self._assert_schema(self._read_sequence('inv-step-merged'))


# ============================================================================
# V6 (orchestrator-rectification-v2) — apply_step_merged
# ============================================================================


class ApplyStepMergedTests(_HelpersHarness):
    """V6: outbox-notifier-emitted signal when AUTO_MERGE merges a PR
    whose task_id matches a step in an active sequence."""

    def _dispatched_seq(self, seq_id='step-merged-001'):
        return _make_sequence(
            seq_id=seq_id,
            status='active',
            steps=[
                _make_step(
                    'step-1', status='dispatched',
                    dispatched_at='2026-05-27T00:30:00Z',
                ),
                _make_step('step-2', deps=['step-1']),
            ],
            current_steps=['step-1'],
        )

    def test_happy_path_dispatched_to_merged(self):
        seq = self._dispatched_seq()
        self._write_sequence(seq)
        result = ssh.apply_step_merged(
            seq_id='step-merged-001',
            step_id='step-1',
            pr_url='https://github.com/x/y/pull/42',
            merged_at='2026-05-27T01:00:00Z',
        )
        self.assertTrue(result.applied)
        self.assertFalse(result.error)
        on_disk = self._read_sequence('step-merged-001')
        step1 = next(s for s in on_disk['steps'] if s['step_id'] == 'step-1')
        self.assertEqual(step1['status'], 'merged')
        self.assertEqual(step1['merged_at'], '2026-05-27T01:00:00Z')
        self.assertEqual(step1['pr_url'], 'https://github.com/x/y/pull/42')
        self.assertNotIn('step-1', on_disk['current_steps'])

    def test_appends_step_merged_audit_event(self):
        seq = self._dispatched_seq()
        initial_log_len = len(seq['audit_log'])
        self._write_sequence(seq)
        ssh.apply_step_merged(
            seq_id='step-merged-001',
            step_id='step-1',
            pr_url='https://github.com/x/y/pull/42',
            merged_at='2026-05-27T01:00:00Z',
            actor='notifier',
        )
        on_disk = self._read_sequence('step-merged-001')
        self.assertEqual(len(on_disk['audit_log']), initial_log_len + 1)
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'step-merged')
        self.assertEqual(last['step_id'], 'step-1')
        self.assertEqual(last['pr_url'], 'https://github.com/x/y/pull/42')
        self.assertEqual(last['actor'], 'notifier')
        self.assertEqual(last['ts'], '2026-05-27T01:00:00Z')

    def test_idempotent_on_already_merged(self):
        """Notifier crash-then-resume re-processes the same outbox; the
        second call must be a clean no-op rather than rewriting fields."""
        seq = _make_sequence(
            seq_id='step-merged-002',
            status='active',
            steps=[_make_step(
                'step-1', status='merged',
                merged_at='2026-05-27T00:00:00Z',
                pr_url='https://github.com/x/y/pull/1',
            )],
            current_steps=[],
        )
        self._write_sequence(seq)
        result = ssh.apply_step_merged(
            seq_id='step-merged-002',
            step_id='step-1',
            pr_url='https://github.com/x/y/pull/999',  # different URL
            merged_at='2026-05-28T00:00:00Z',
        )
        self.assertFalse(result.applied)
        self.assertFalse(result.error)
        # On-disk state UNCHANGED — original pr_url + merged_at preserved.
        on_disk = self._read_sequence('step-merged-002')
        step1 = next(s for s in on_disk['steps'] if s['step_id'] == 'step-1')
        self.assertEqual(step1['pr_url'], 'https://github.com/x/y/pull/1')
        self.assertEqual(step1['merged_at'], '2026-05-27T00:00:00Z')

    def test_missing_step_returns_hard_error(self):
        seq = self._dispatched_seq()
        self._write_sequence(seq)
        result = ssh.apply_step_merged(
            seq_id='step-merged-001',
            step_id='does-not-exist',
            pr_url='https://github.com/x/y/pull/1',
            merged_at='2026-05-27T01:00:00Z',
        )
        self.assertFalse(result.applied)
        self.assertTrue(result.error)

    def test_missing_sequence_file_returns_hard_error(self):
        result = ssh.apply_step_merged(
            seq_id='absent-sequence',
            step_id='step-1',
            pr_url='https://github.com/x/y/pull/1',
            merged_at='2026-05-27T01:00:00Z',
        )
        self.assertFalse(result.applied)
        self.assertTrue(result.error)

    def test_pending_step_also_transitions_to_merged(self):
        # The advancer's typical pre-dispatch state for a step is 'pending'
        # before flipping to 'dispatched'. If the notifier signal fires
        # while we're still in 'pending' (theoretical race; preserves the
        # invariant), it should still flip cleanly.
        seq = _make_sequence(
            seq_id='step-merged-003',
            status='active',
            steps=[_make_step('step-1', status='pending')],
            current_steps=['step-1'],
        )
        self._write_sequence(seq)
        result = ssh.apply_step_merged(
            seq_id='step-merged-003',
            step_id='step-1',
            pr_url='https://github.com/x/y/pull/7',
            merged_at='2026-05-27T01:00:00Z',
        )
        self.assertTrue(result.applied)
        on_disk = self._read_sequence('step-merged-003')
        step1 = next(s for s in on_disk['steps'] if s['step_id'] == 'step-1')
        self.assertEqual(step1['status'], 'merged')


# ============================================================================
# Sequence-completion finalization (fix-apply-skip-finalization)
#
# Coverage for the _check_sequence_completion helper wired into
# apply_skip and apply_step_merged. The zombie-active gap surfaced on
# operator-ux-rollout 2026-05-29: skip mutated the final step to merged
# but never triggered the sequence-level rollup, leaving status=active +
# current_steps naming the just-skipped step.
# ============================================================================


class SequenceCompletionFinalizationTests(_HelpersHarness):

    def test_skip_on_last_pending_step_finalizes_sequence(self):
        """apply_skip on the final non-terminal step → sequence flips
        active → complete, current_steps cleared, audit_log records
        sequence-complete attributed to the apply_skip caller."""
        seq = _make_sequence(
            seq_id='fin-skip-last',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
                _make_step('b', deps=['a'], status='failed'),
            ],
            current_steps=['b'],
        )
        self._write_sequence(seq)
        result = ssh.apply_skip(
            'fin-skip-last', 'b', actor='larry',
            reason='superseded by replacement mission',
        )
        self.assertTrue(result.applied)
        self.assertFalse(result.error)
        on_disk = self._read_sequence('fin-skip-last')
        self.assertEqual(on_disk['status'], 'complete')
        self.assertEqual(on_disk['current_steps'], [])
        events = [e['event'] for e in on_disk['audit_log']]
        self.assertIn('step-skipped', events)
        self.assertIn('sequence-complete', events)
        completion = on_disk['audit_log'][-1]
        self.assertEqual(completion['event'], 'sequence-complete')
        self.assertEqual(completion['actor'], 'larry')
        self.assertIn('ts', completion)
        # Schema invariant holds across the finalization mutation.
        v = bsv.validate_dag(on_disk)
        self.assertTrue(v.valid, v.errors)

    def test_skip_on_non_last_step_does_not_finalize(self):
        """apply_skip on a non-last step → step flips to merged and is
        removed from current_steps, but the sequence stays active with
        the other steps still present in current_steps."""
        seq = _make_sequence(
            seq_id='fin-skip-mid',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
                _make_step('b', deps=['a'], status='failed'),
                _make_step('c', deps=['a'], status='dispatched',
                           dispatched_at='2026-05-27T02:00:00Z'),
            ],
            current_steps=['b', 'c'],
        )
        self._write_sequence(seq)
        result = ssh.apply_skip('fin-skip-mid', 'b', actor='larry')
        self.assertTrue(result.applied)
        on_disk = self._read_sequence('fin-skip-mid')
        # Sequence stays live; only the skipped step leaves current_steps.
        self.assertEqual(on_disk['status'], 'active')
        self.assertEqual(on_disk['current_steps'], ['c'])
        events = [e['event'] for e in on_disk['audit_log']]
        self.assertNotIn('sequence-complete', events)

    def test_step_merged_on_last_pending_step_finalizes_sequence(self):
        """apply_step_merged on the final non-terminal step → sequence
        rollup fires. Validates the substantive-mutation finalization
        path (the canonical AUTO_MERGE flow that motivated V6)."""
        seq = _make_sequence(
            seq_id='fin-merged-last',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
                _make_step('b', deps=['a'], status='dispatched',
                           dispatched_at='2026-05-27T02:00:00Z'),
            ],
            current_steps=['b'],
        )
        self._write_sequence(seq)
        result = ssh.apply_step_merged(
            seq_id='fin-merged-last',
            step_id='b',
            pr_url='https://github.com/x/y/pull/77',
            merged_at='2026-05-27T03:00:00Z',
            actor='notifier',
        )
        self.assertTrue(result.applied)
        on_disk = self._read_sequence('fin-merged-last')
        self.assertEqual(on_disk['status'], 'complete')
        self.assertEqual(on_disk['current_steps'], [])
        completion = on_disk['audit_log'][-1]
        self.assertEqual(completion['event'], 'sequence-complete')
        self.assertEqual(completion['actor'], 'notifier')

    def test_step_merged_idempotency_branch_finalizes_zombie_sequence(self):
        """The zombie-recovery path: step was previously skipped to
        merged but the sequence never finalized (pre-fix world). A later
        apply_step_merged call on the same step short-circuits the
        step-level mutation but the new idempotency-branch finalization
        call still flips the sequence to complete."""
        seq = _make_sequence(
            seq_id='fin-zombie',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
                _make_step('b', deps=['a'], status='merged',
                           merged_at='2026-05-27T02:00:00Z'),
            ],
            current_steps=['b'],  # zombie residue from the pre-fix skip
        )
        self._write_sequence(seq)
        result = ssh.apply_step_merged(
            seq_id='fin-zombie',
            step_id='b',
            pr_url='https://github.com/x/y/pull/88',
            merged_at='2026-05-27T03:00:00Z',
            actor='notifier',
        )
        # Step-level no-op (was already merged), but sequence finalized.
        self.assertFalse(result.applied)
        self.assertFalse(result.error)
        on_disk = self._read_sequence('fin-zombie')
        self.assertEqual(on_disk['status'], 'complete')
        self.assertEqual(on_disk['current_steps'], [])
        completion = on_disk['audit_log'][-1]
        self.assertEqual(completion['event'], 'sequence-complete')
        self.assertEqual(completion['actor'], 'notifier')

    def test_cancel_then_all_merged_keeps_failed_status(self):
        """Regression guard: a cancelled (status=failed) sequence whose
        remaining steps later get flipped to merged must NOT auto-flip
        to complete — apply_cancel is operator-overriding terminal."""
        seq = _make_sequence(
            seq_id='fin-cancel-then-merge',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
                _make_step('b', deps=['a'], status='dispatched',
                           dispatched_at='2026-05-27T02:00:00Z'),
            ],
            current_steps=['b'],
        )
        self._write_sequence(seq)
        cancel_result = ssh.apply_cancel(
            'fin-cancel-then-merge', actor='larry', reason='abandoned',
        )
        self.assertTrue(cancel_result.applied)
        # Now flip the last step to merged via the notifier path.
        merged_result = ssh.apply_step_merged(
            seq_id='fin-cancel-then-merge',
            step_id='b',
            pr_url='https://github.com/x/y/pull/9',
            merged_at='2026-05-27T03:00:00Z',
            actor='notifier',
        )
        self.assertTrue(merged_result.applied)
        on_disk = self._read_sequence('fin-cancel-then-merge')
        # Sequence stays failed; finalization gate refused to overwrite.
        self.assertEqual(on_disk['status'], 'failed')
        events = [e['event'] for e in on_disk['audit_log']]
        self.assertNotIn('sequence-complete', events)

    def test_double_finalization_is_idempotent(self):
        """Calling _check_sequence_completion twice on the same
        already-finalized sequence: second call is a clean no-op (no
        duplicate sequence-complete audit entry, no mutation)."""
        seq = _make_sequence(
            seq_id='fin-double',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
            ],
            current_steps=['a'],
        )
        self._write_sequence(seq)
        # First trigger: step-merged-on-zombie path finalizes.
        ssh.apply_step_merged(
            seq_id='fin-double',
            step_id='a',
            pr_url='https://github.com/x/y/pull/1',
            merged_at='2026-05-27T02:00:00Z',
            actor='notifier',
        )
        after_first = self._read_sequence('fin-double')
        self.assertEqual(after_first['status'], 'complete')
        completed_count = sum(
            1 for e in after_first['audit_log']
            if e['event'] == 'sequence-complete'
        )
        self.assertEqual(completed_count, 1)
        # Second trigger: same call again. Step still merged, sequence
        # already complete → no further mutation, no audit churn.
        ssh.apply_step_merged(
            seq_id='fin-double',
            step_id='a',
            pr_url='https://github.com/x/y/pull/1',
            merged_at='2026-05-27T02:00:00Z',
            actor='notifier',
        )
        after_second = self._read_sequence('fin-double')
        self.assertEqual(after_second, after_first)

    def test_skip_passes_actor_through_to_completion_audit(self):
        """The sequence-complete audit entry carries the apply_skip
        caller's actor, not a hardcoded value. Mirrors the per-call
        attribution Beacon needs for the dashboard build-sequences tab."""
        seq = _make_sequence(
            seq_id='fin-skip-actor',
            status='active',
            steps=[
                _make_step('a', status='merged',
                           merged_at='2026-05-27T01:00:00Z'),
                _make_step('b', deps=['a'], status='failed'),
            ],
            current_steps=['b'],
        )
        self._write_sequence(seq)
        ssh.apply_skip('fin-skip-actor', 'b', actor='beacon')
        on_disk = self._read_sequence('fin-skip-actor')
        completion = on_disk['audit_log'][-1]
        self.assertEqual(completion['event'], 'sequence-complete')
        self.assertEqual(completion['actor'], 'beacon')


# The verbatim build-outbox result text Forge emitted in the 2026-06-20 incident
# (seq launch-system-self-awareness-slice-1-state-log) — a no-PR, already-merged
# refusal naming PR #602. Used as the canonical fixture so the parser is pinned
# to a real outcome, not a synthetic one.
_REAL_NO_DELTA_RESULT = (
    "I cannot open a PR here, and I'm stopping rather than fabricating one.\n\n"
    "**This slice is already built and merged.** PR #602 "
    '"feat(system-awareness): work-in-flight State Log (Slice 1)" merged to '
    "`main` at 2026-06-20T01:10:37Z, and that commit (`790a8ca0`) is already in "
    "this branch's history. `git diff main..HEAD` is empty — my worktree is "
    "byte-identical to `main`. There is no delta to commit.\n\n"
    "- **D1** — `scripts/system_state_log.py` (577 lines)\n"
    "- **Tests** — 22 passed, exit 0.\n\n"
    "This is a stale/duplicate build dispatch — the slice was built and merged "
    "via #602 (almost certainly a concurrent Forge session)."
)


class ParseAlreadyMergedPrRefTests(unittest.TestCase):
    """Pure parser — two-tier: PREFER the canonical structured contract line
    `NO PR — already merged: #<N>` (authoritative); else fall back to a no-delta
    CUE plus exactly one distinct PR number."""

    def test_real_incident_extracts_602(self):
        self.assertEqual(
            ssh.parse_already_merged_pr_ref(_REAL_NO_DELTA_RESULT), 602,
        )

    def test_no_cue_returns_none(self):
        # Names a PR but no already-merged / no-delta cue → not this outcome.
        self.assertIsNone(
            ssh.parse_already_merged_pr_ref('Opened PR #602, build in progress.')
        )

    def test_cue_but_no_pr_number_returns_none(self):
        self.assertIsNone(
            ssh.parse_already_merged_pr_ref(
                'This slice is already merged; no delta to commit.'
            )
        )

    def test_ambiguous_multiple_distinct_prs_returns_none(self):
        # Refuse to guess which PR is the step's work.
        self.assertIsNone(
            ssh.parse_already_merged_pr_ref(
                'Already merged via #602, which supersedes #595. No delta.'
            )
        )

    def test_repeated_same_pr_is_not_ambiguous(self):
        self.assertEqual(
            ssh.parse_already_merged_pr_ref(
                'Already merged via #602. PR #602 satisfies the spec. No delta.'
            ),
            602,
        )

    def test_full_pull_url_form(self):
        self.assertEqual(
            ssh.parse_already_merged_pr_ref(
                'No delta — already merged: '
                'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602'
            ),
            602,
        )

    def test_bare_integers_do_not_match_only_the_hash_pr(self):
        # 'D1', '577 lines', '22 tests' carry no leading # or /pull/.
        self.assertEqual(
            ssh.parse_already_merged_pr_ref(
                'Already merged. D1 is 577 lines, 22 tests pass. PR #602 covers it.'
            ),
            602,
        )

    def test_canonical_no_pr_lead_line(self):
        self.assertEqual(
            ssh.parse_already_merged_pr_ref('NO PR — already merged: #777'), 777,
        )

    def test_canonical_line_is_authoritative_over_other_prs(self):
        # The structured contract line names THE merged PR explicitly, so other
        # PR numbers in the surrounding prose (a superseded attempt) must NOT make
        # it ambiguous the way the prose tier would — this is the durability win.
        self.assertEqual(
            ssh.parse_already_merged_pr_ref(
                'NO PR — already merged: #602\n\n'
                'This superseded my earlier #595 attempt; `git diff main..HEAD` '
                'is empty.'
            ),
            602,
        )

    def test_canonical_line_lenient_separators(self):
        # Colon / hyphen stand in for the em-dash, optional `PR` token before the
        # number — a minor transcription drift still parses as tier 1.
        for text in (
            'NO PR: already merged #602',
            'NO PR - already merged: #602',
            'NO PR — already merged PR #602',
        ):
            with self.subTest(text=text):
                self.assertEqual(ssh.parse_already_merged_pr_ref(text), 602)

    def test_canonical_line_full_url_form(self):
        self.assertEqual(
            ssh.parse_already_merged_pr_ref(
                'NO PR — already merged: '
                'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602'
            ),
            602,
        )

    def test_canonical_url_form_authoritative_over_other_prs(self):
        # The URL form of the canonical line must be tier-1 authoritative too:
        # a superseded PR mentioned in the explanation paragraph below must NOT
        # drag it into tier-2 ambiguity → None (the `#<N>` and URL forms of the
        # same contract must reach the SAME answer).
        self.assertEqual(
            ssh.parse_already_merged_pr_ref(
                'NO PR — already merged: '
                'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602\n\n'
                'This work was already landed by #551 in an earlier attempt.'
            ),
            602,
        )

    def test_url_fragment_does_not_phantom_a_second_pr(self):
        # A deep PR link's OWN #fragment (named or numeric) must not be read as a
        # second distinct PR — that would make a single-PR result spuriously
        # ambiguous (a safe-but-suboptimal false negative). Prose tier → 602.
        for url in (
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602'
            '#issuecomment-123',
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602#603',
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    ssh.parse_already_merged_pr_ref(
                        f'Already merged via {url}. No delta to commit.'
                    ),
                    602,
                )

    def test_empty_or_non_str(self):
        self.assertIsNone(ssh.parse_already_merged_pr_ref(''))
        self.assertIsNone(ssh.parse_already_merged_pr_ref(None))


class GhPrMergeInfoTests(unittest.TestCase):
    """gh-truth gate — returns (url, merged_at) ONLY when state == MERGED."""

    @staticmethod
    def _run_returning(rc=0, stdout='', raise_exc=None):
        def _run(argv, **kwargs):  # noqa: ANN001 — test stub
            if raise_exc is not None:
                raise raise_exc
            return SimpleNamespace(returncode=rc, stdout=stdout, stderr='')
        return _run

    def test_merged_returns_url_and_time(self):
        out = json.dumps({
            'state': 'MERGED',
            'url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602',
            'mergedAt': '2026-06-20T01:10:37Z',
        })
        with mock.patch.object(ssh.subprocess, 'run', self._run_returning(stdout=out)):
            info = ssh.gh_pr_merge_info('Larry-Yatch/ourliberty-agent-core', 602)
        self.assertEqual(info, (
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602',
            '2026-06-20T01:10:37Z',
        ))

    def test_open_pr_returns_none(self):
        out = json.dumps({'state': 'OPEN', 'url': 'x', 'mergedAt': None})
        with mock.patch.object(ssh.subprocess, 'run', self._run_returning(stdout=out)):
            self.assertIsNone(ssh.gh_pr_merge_info('o/r', 5))

    def test_merged_but_missing_url_returns_none(self):
        out = json.dumps({'state': 'MERGED', 'url': '', 'mergedAt': '2026-06-20T01:10:37Z'})
        with mock.patch.object(ssh.subprocess, 'run', self._run_returning(stdout=out)):
            self.assertIsNone(ssh.gh_pr_merge_info('o/r', 5))

    def test_nonzero_exit_returns_none(self):
        with mock.patch.object(ssh.subprocess, 'run', self._run_returning(rc=1)):
            self.assertIsNone(ssh.gh_pr_merge_info('o/r', 5))

    def test_transport_error_returns_none(self):
        with mock.patch.object(
            ssh.subprocess, 'run',
            self._run_returning(raise_exc=FileNotFoundError('gh not found')),
        ):
            self.assertIsNone(ssh.gh_pr_merge_info('o/r', 5))

    def test_bad_pr_number_does_not_shell_out(self):
        called = {'n': 0}

        def _run(argv, **kwargs):  # noqa: ANN001 — test stub
            called['n'] += 1
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')

        with mock.patch.object(ssh.subprocess, 'run', _run):
            self.assertIsNone(ssh.gh_pr_merge_info('o/r', 0))
        self.assertEqual(called['n'], 0)


# ============================================================================
# step-failed (push-signal non-merge terminals)
# ============================================================================


class ApplyStepFailedTests(_HelpersHarness):

    def _active_with_dispatched_step(self, seq_id='f-seq'):
        return _make_sequence(
            seq_id=seq_id,
            status='active',
            steps=[
                _make_step('s1', deps=[], status='dispatched',
                           dispatched_at='2026-06-24T00:00:00Z'),
                _make_step('s2', deps=['s1'], status='pending'),
            ],
            current_steps=['s1'],
        )

    def test_happy_path_flips_step_failed_and_pauses_sequence(self):
        self._write_sequence(self._active_with_dispatched_step())
        result = ssh.apply_step_failed(
            'f-seq', 's1', 'Forge preflight REJECT (spec unsafe)',
        )
        self.assertTrue(result.applied)
        self.assertFalse(result.error)
        on_disk = self._read_sequence('f-seq')
        s1 = next(s for s in on_disk['steps'] if s['step_id'] == 's1')
        self.assertEqual(s1['status'], 'failed')
        self.assertEqual(s1['failure_reason'],
                         'Forge preflight REJECT (spec unsafe)')
        self.assertIsNone(s1['current_actor'])
        # Sequence paused.
        self.assertEqual(on_disk['status'], 'paused')
        # Failed step STAYS in current_steps for operator visibility.
        self.assertIn('s1', on_disk['current_steps'])
        # Audit: step-failed THEN sequence-paused.
        events = [e['event'] for e in on_disk['audit_log']]
        self.assertEqual(events[-2:], ['step-failed', 'sequence-paused'])
        # Validator still passes.
        v = bsv.validate_dag(on_disk)
        self.assertTrue(v.valid, v.errors)

    def test_idempotent_when_already_failed(self):
        seq = _make_sequence(
            seq_id='f-idem', status='paused',
            steps=[_make_step('s1', status='failed')],
            current_steps=['s1'],
        )
        seq['steps'][0]['failure_reason'] = 'original reason'
        self._write_sequence(seq)
        before = self._read_sequence('f-idem')
        result = ssh.apply_step_failed('f-idem', 's1', 'a different reason')
        self.assertFalse(result.applied)
        self.assertFalse(result.error)
        self.assertIn('already', result.reason)
        # On-disk unchanged — original reason preserved.
        self.assertEqual(self._read_sequence('f-idem'), before)

    def test_merged_step_is_never_clobbered(self):
        # A late/duplicate failure signal must not overwrite a real merge
        # (success/failure race resolves in favor of success).
        seq = _make_sequence(
            seq_id='f-merged', status='active',
            steps=[_make_step('s1', status='merged',
                              merged_at='2026-06-24T00:00:00Z',
                              pr_url='https://github.com/x/y/pull/1')],
        )
        self._write_sequence(seq)
        before = self._read_sequence('f-merged')
        result = ssh.apply_step_failed('f-merged', 's1', 'late failure')
        self.assertFalse(result.applied)
        self.assertIn('merged', result.reason)
        self.assertEqual(self._read_sequence('f-merged'), before)

    def test_missing_step_returns_error(self):
        self._write_sequence(self._active_with_dispatched_step())
        result = ssh.apply_step_failed('f-seq', 'nope', 'reason')
        self.assertFalse(result.applied)
        self.assertTrue(result.error)
        self.assertIn('not found', result.reason)


# ============================================================================
# step-pr-opened (pr_url + substatus at PR-open)
# ============================================================================


class ApplyStepPrOpenedTests(_HelpersHarness):

    def _active_with_dispatched_step(self, seq_id='o-seq'):
        return _make_sequence(
            seq_id=seq_id,
            status='active',
            steps=[
                _make_step('s1', deps=[], status='dispatched',
                           dispatched_at='2026-06-24T00:00:00Z'),
            ],
            current_steps=['s1'],
        )

    def test_happy_path_records_pr_url_and_flips_reviewing(self):
        self._write_sequence(self._active_with_dispatched_step())
        result = ssh.apply_step_pr_opened(
            'o-seq', 's1', 'https://github.com/x/y/pull/42',
        )
        self.assertTrue(result.applied)
        self.assertFalse(result.error)
        on_disk = self._read_sequence('o-seq')
        s1 = on_disk['steps'][0]
        self.assertEqual(s1['status'], 'reviewing')
        self.assertEqual(s1['pr_url'], 'https://github.com/x/y/pull/42')
        self.assertEqual(s1['current_actor'], 'mirror')
        # Sequence status untouched — PR-open is not a sequence transition.
        self.assertEqual(on_disk['status'], 'active')
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'step-pr-opened')
        self.assertEqual(last['pr_url'], 'https://github.com/x/y/pull/42')
        v = bsv.validate_dag(on_disk)
        self.assertTrue(v.valid, v.errors)

    def test_idempotent_same_pr_url_while_reviewing(self):
        seq = _make_sequence(
            seq_id='o-idem', status='active',
            steps=[_make_step('s1', status='reviewing',
                              pr_url='https://github.com/x/y/pull/7')],
            current_steps=['s1'],
        )
        seq['steps'][0]['current_actor'] = 'mirror'
        self._write_sequence(seq)
        before = self._read_sequence('o-idem')
        result = ssh.apply_step_pr_opened(
            'o-idem', 's1', 'https://github.com/x/y/pull/7',
        )
        self.assertFalse(result.applied)
        self.assertFalse(result.error)
        self.assertEqual(self._read_sequence('o-idem'), before)

    def test_terminal_step_is_not_walked_backward(self):
        for term in ('merged', 'failed'):
            with self.subTest(status=term):
                seq = _make_sequence(
                    seq_id=f'o-{term}', status='active',
                    steps=[_make_step('s1', status=term,
                                      pr_url='https://github.com/x/y/pull/1')],
                )
                self._write_sequence(seq)
                before = self._read_sequence(f'o-{term}')
                result = ssh.apply_step_pr_opened(
                    f'o-{term}', 's1', 'https://github.com/x/y/pull/99',
                )
                self.assertFalse(result.applied)
                self.assertIn('terminal', result.reason)
                self.assertEqual(self._read_sequence(f'o-{term}'), before)

    def test_missing_step_returns_error(self):
        self._write_sequence(self._active_with_dispatched_step())
        result = ssh.apply_step_pr_opened(
            'o-seq', 'nope', 'https://github.com/x/y/pull/1',
        )
        self.assertFalse(result.applied)
        self.assertTrue(result.error)
        self.assertIn('not found', result.reason)


if __name__ == '__main__':
    unittest.main()
