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

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import build_sequence_validator as bsv  # noqa: E402
import sequence_shortcut_helpers as ssh  # noqa: E402


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
        last = on_disk['audit_log'][-1]
        self.assertEqual(last['event'], 'step-skipped')
        self.assertEqual(last['step_id'], 'b')
        self.assertEqual(last['actor'], 'larry')
        self.assertEqual(last['reason'], 'Work done out-of-band via hotfix')
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
        last = self._read_sequence('sk-no-reason')['audit_log'][-1]
        self.assertEqual(last['event'], 'step-skipped')
        self.assertNotIn('reason', last)

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


if __name__ == '__main__':
    unittest.main()
