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


if __name__ == '__main__':
    unittest.main()
