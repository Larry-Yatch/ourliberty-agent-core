"""Tests for scripts/build_sequence_validator.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
  - Valid sequence passes both schema + DAG checks
  - Cycle detection (A→B→A, A→A self-loop)
  - Missing depends_on reference
  - Schema violations (missing fields, wrong types, invalid status enum)
  - Duplicate step_ids
  - current_steps referencing unknown step_id
  - dispatch_text length cap (500 chars per spec § 5.5)
  - validate_no_concurrent_active: empty / completed-only / paused-only /
    active blocker / pending blocker / unparseable file = blocker
  - CLI: valid file → exit 0, invalid file → exit 1 with errors to stderr
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import build_sequence_validator as bsv  # noqa: E402


def _valid_step(step_id: str, deps: list[str] | None = None,
                status: str = 'pending') -> dict:
    return {
        'step_id': step_id,
        'label': f'Step {step_id}',
        'depends_on': deps or [],
        'dispatch_text': (
            f'Build step {step_id} per spec § X. Review focus: Y.'
        ),
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'feature-development',
        'status': status,
        'dispatched_at': None,
        'merged_at': None,
        'pr_url': None,
        'current_actor': None,
        'failure_reason': None,
    }


def _valid_sequence(steps: list[dict] | None = None,
                    status: str = 'active',
                    current_steps: list[str] | None = None,
                    seq_id: str = 'test-seq-001') -> dict:
    if steps is None:
        steps = [_valid_step('alpha')]
    return {
        'seq_id': seq_id,
        'label': f'Test sequence {seq_id}',
        'spec_doc': 'agents/beacon/specs/test.md',
        'created_at': '2026-05-27T00:00:00+00:00',
        'created_by': 'test',
        'status': status,
        'current_steps': current_steps if current_steps is not None else [],
        'steps': steps,
        'audit_log': [],
    }


class TestValidateDagHappyPath(unittest.TestCase):
    def test_single_step_no_deps(self):
        seq = _valid_sequence()
        result = bsv.validate_dag(seq)
        self.assertTrue(result.valid, msg=result.errors)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.seq_id, 'test-seq-001')
        self.assertTrue(bool(result))

    def test_linear_chain(self):
        seq = _valid_sequence(steps=[
            _valid_step('a'),
            _valid_step('b', deps=['a']),
            _valid_step('c', deps=['b']),
        ])
        self.assertTrue(bsv.validate_dag(seq).valid)

    def test_diamond_dag(self):
        seq = _valid_sequence(steps=[
            _valid_step('root'),
            _valid_step('left', deps=['root']),
            _valid_step('right', deps=['root']),
            _valid_step('leaf', deps=['left', 'right']),
        ])
        self.assertTrue(bsv.validate_dag(seq).valid)

    def test_parallel_no_deps(self):
        seq = _valid_sequence(steps=[
            _valid_step('a'),
            _valid_step('b'),
            _valid_step('c'),
        ])
        self.assertTrue(bsv.validate_dag(seq).valid)


class TestValidateDagCycles(unittest.TestCase):
    def test_two_step_cycle(self):
        # A depends on B, B depends on A.
        seq = _valid_sequence(steps=[
            _valid_step('a', deps=['b']),
            _valid_step('b', deps=['a']),
        ])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        joined = ' | '.join(result.errors)
        self.assertIn('cycle', joined.lower())

    def test_three_step_cycle(self):
        seq = _valid_sequence(steps=[
            _valid_step('a', deps=['c']),
            _valid_step('b', deps=['a']),
            _valid_step('c', deps=['b']),
        ])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('cycle' in e.lower() for e in result.errors))

    def test_self_loop(self):
        seq = _valid_sequence(steps=[_valid_step('a', deps=['a'])])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(
            any('self-loop' in e or 'depends on itself' in e
                for e in result.errors)
        )


class TestValidateDagMissingRefs(unittest.TestCase):
    def test_missing_dep_reference(self):
        seq = _valid_sequence(steps=[
            _valid_step('a'),
            _valid_step('b', deps=['ghost']),
        ])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(
            any('ghost' in e and 'unknown step_id' in e
                for e in result.errors),
            msg=result.errors,
        )

    def test_current_steps_unknown_id(self):
        seq = _valid_sequence(
            steps=[_valid_step('a')],
            current_steps=['ghost'],
        )
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(
            any('current_steps' in e and 'ghost' in e
                for e in result.errors)
        )


class TestValidateDagSchema(unittest.TestCase):
    def test_missing_required_top_field(self):
        seq = _valid_sequence()
        del seq['steps']
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(
            any('missing required top-level field' in e
                for e in result.errors)
        )

    def test_empty_steps_list(self):
        seq = _valid_sequence(steps=[])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('empty' in e for e in result.errors))

    def test_invalid_top_status_enum(self):
        seq = _valid_sequence()
        seq['status'] = 'in_progress'  # not in spec § 5.1 enum
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any("status='in_progress'" in e or 'in_progress' in e
                            for e in result.errors))

    def test_invalid_step_status_enum(self):
        seq = _valid_sequence(steps=[
            {**_valid_step('a'), 'status': 'queued'},
        ])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('queued' in e for e in result.errors))

    def test_invalid_step_actor_enum(self):
        seq = _valid_sequence(steps=[
            {**_valid_step('a'), 'current_actor': 'borg'},
        ])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('borg' in e for e in result.errors))

    def test_step_missing_required_field(self):
        bad_step = _valid_step('a')
        del bad_step['dispatch_text']
        seq = _valid_sequence(steps=[bad_step])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('dispatch_text' in e for e in result.errors))

    def test_duplicate_step_ids(self):
        seq = _valid_sequence(steps=[
            _valid_step('a'),
            _valid_step('a'),
        ])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('duplicate step_id' in e for e in result.errors))

    def test_dispatch_text_too_long(self):
        seq = _valid_sequence(steps=[
            {**_valid_step('a'), 'dispatch_text': 'X' * 501},
        ])
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('500' in e for e in result.errors))

    def test_top_level_not_a_dict(self):
        result = bsv.validate_dag(['not', 'a', 'dict'])
        self.assertFalse(result.valid)

    def test_steps_not_a_list(self):
        seq = _valid_sequence()
        seq['steps'] = 'oops'
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)


class TestValidateGapLog(unittest.TestCase):
    """Spec: agents/beacon/specs/operator-ux-gap-log-field.md.

    `gap_log` is an optional sequence-level field. Existing sequences
    (gap_log absent) MUST continue to validate. When present, entries are
    type-checked but severity values are not enumerated."""

    def _entry(self, **overrides) -> dict:
        base = {
            'ts': '2026-05-28T13:42:00Z',
            'severity': 'medium',
            'finding': 'Message 2 unreachable without notification',
            'surfaced_by': 'bootstrap-003-verifier',
        }
        base.update(overrides)
        return base

    def test_absent_gap_log_validates(self):
        seq = _valid_sequence()
        self.assertNotIn('gap_log', seq)
        self.assertTrue(bsv.validate_dag(seq).valid)

    def test_empty_gap_log_validates(self):
        seq = _valid_sequence()
        seq['gap_log'] = []
        self.assertTrue(bsv.validate_dag(seq).valid)

    def test_valid_gap_log_entry(self):
        seq = _valid_sequence()
        seq['gap_log'] = [self._entry()]
        self.assertTrue(bsv.validate_dag(seq).valid)

    def test_multiple_valid_entries(self):
        seq = _valid_sequence()
        seq['gap_log'] = [
            self._entry(),
            self._entry(severity='FYI', surfaced_by='pulse-check-ix'),
        ]
        self.assertTrue(bsv.validate_dag(seq).valid)

    def test_gap_log_not_a_list(self):
        seq = _valid_sequence()
        seq['gap_log'] = 'not a list'
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('gap_log must be a list' in e for e in result.errors))

    def test_gap_log_entry_not_a_dict(self):
        seq = _valid_sequence()
        seq['gap_log'] = ['oops']
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any('gap_log[0]' in e for e in result.errors))

    def test_gap_log_entry_missing_field(self):
        seq = _valid_sequence()
        entry = self._entry()
        del entry['ts']
        seq['gap_log'] = [entry]
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(any("'ts'" in e for e in result.errors))

    def test_gap_log_entry_non_string_field(self):
        seq = _valid_sequence()
        seq['gap_log'] = [self._entry(severity=3)]
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(
            any('severity' in e and 'non-empty string' in e
                for e in result.errors)
        )

    def test_gap_log_entry_empty_string_field(self):
        seq = _valid_sequence()
        seq['gap_log'] = [self._entry(finding='   ')]
        result = bsv.validate_dag(seq)
        self.assertFalse(result.valid)
        self.assertTrue(
            any('finding' in e and 'non-empty string' in e
                for e in result.errors)
        )

    def test_gap_log_severity_not_enumerated(self):
        """Spec § 3 leaves taxonomy open — any non-empty string works."""
        seq = _valid_sequence()
        seq['gap_log'] = [self._entry(severity='wildly-custom-tier')]
        self.assertTrue(bsv.validate_dag(seq).valid)


class TestValidateNoConcurrentActive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bdir = Path(self._tmp.name) / 'build-sequences'
        self.bdir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, seq_id: str, status: str = 'active'):
        seq = _valid_sequence(seq_id=seq_id, status=status)
        (self.bdir / f'{seq_id}.json').write_text(json.dumps(seq))

    def test_empty_dir_returns_true(self):
        self.assertTrue(bsv.validate_no_concurrent_active(self.bdir))

    def test_missing_dir_returns_true(self):
        self.assertTrue(
            bsv.validate_no_concurrent_active(self.bdir.parent / 'nonexistent')
        )

    def test_only_completed_returns_true(self):
        self._write('a', status='complete')
        self._write('b', status='complete')
        self.assertTrue(bsv.validate_no_concurrent_active(self.bdir))

    def test_only_paused_returns_true(self):
        # Per the brief / CLARIFY: paused is NOT a blocker; Larry can run
        # parallel paused + new sequences.
        self._write('a', status='paused')
        self.assertTrue(bsv.validate_no_concurrent_active(self.bdir))

    def test_only_failed_returns_true(self):
        self._write('a', status='failed')
        self.assertTrue(bsv.validate_no_concurrent_active(self.bdir))

    def test_active_blocks(self):
        self._write('a', status='active')
        self.assertFalse(bsv.validate_no_concurrent_active(self.bdir))

    def test_pending_blocks(self):
        # pending = "created but not started" — still in the live set per
        # the validator's fail-closed default; another pending or active
        # sequence cannot be created alongside.
        self._write('a', status='pending')
        self.assertFalse(bsv.validate_no_concurrent_active(self.bdir))

    def test_active_among_completed_blocks(self):
        self._write('done', status='complete')
        self._write('inflight', status='active')
        self._write('shelved', status='paused')
        self.assertFalse(bsv.validate_no_concurrent_active(self.bdir))

    def test_malformed_json_blocks_fail_closed(self):
        (self.bdir / 'broken.json').write_text('{not json')
        self.assertFalse(bsv.validate_no_concurrent_active(self.bdir))

    def test_hidden_archive_skipped(self):
        # .archive/ subdirs hold rotated old sequences; they should not
        # influence concurrency.
        archive = self.bdir / '.archive'
        archive.mkdir()
        (archive / 'old.json').write_text(json.dumps(
            _valid_sequence(seq_id='old', status='active')
        ))
        self.assertTrue(bsv.validate_no_concurrent_active(self.bdir))


class TestCLI(unittest.TestCase):
    """Subprocess invocation of the CLI entry point."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.script = _SCRIPTS_DIR / 'build_sequence_validator.py'

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.script), str(path)],
            capture_output=True, text=True, timeout=30,
        )

    def test_cli_valid_exits_zero(self):
        f = self.tmpdir / 'good.json'
        f.write_text(json.dumps(_valid_sequence()))
        proc = self._run(f)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn('OK', proc.stdout)

    def test_cli_invalid_exits_one(self):
        f = self.tmpdir / 'bad.json'
        seq = _valid_sequence(steps=[_valid_step('a', deps=['a'])])
        f.write_text(json.dumps(seq))
        proc = self._run(f)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('INVALID', proc.stderr)
        self.assertIn('self-loop', proc.stderr.lower() + proc.stdout.lower())

    def test_cli_unparseable_exits_one(self):
        f = self.tmpdir / 'broken.json'
        f.write_text('{not json')
        proc = self._run(f)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('invalid JSON', proc.stderr)

    def test_cli_missing_file_exits_one(self):
        proc = self._run(self.tmpdir / 'does-not-exist.json')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('not a file', proc.stderr)

    def test_cli_validate_subcommand_resolves_blackboard_path(self):
        """PR-S4 rectification (H5): `validate <seq-id>` expands to
        `<blackboard>/<seq-id>.json` automatically. The blackboard root
        is controlled by OURLIBERTY_AGENTS_ROOT so the subprocess writes
        only to tmpdir."""
        agents_root = self.tmpdir / 'agents'
        bdir = agents_root / 'blackboard' / 'build-sequences'
        bdir.mkdir(parents=True)
        (bdir / 'sub-test-001.json').write_text(
            json.dumps(_valid_sequence(seq_id='sub-test-001')),
        )
        env = {**os.environ, 'OURLIBERTY_AGENTS_ROOT': str(agents_root)}
        proc = subprocess.run(
            [sys.executable, str(self.script), 'validate', 'sub-test-001'],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn('OK', proc.stdout)

    def test_cli_validate_subcommand_unknown_seq_id_exits_one(self):
        """`validate <seq-id>` on a seq-id with no corresponding file
        exits 1 with `not a file` on stderr."""
        agents_root = self.tmpdir / 'agents'
        bdir = agents_root / 'blackboard' / 'build-sequences'
        bdir.mkdir(parents=True)
        env = {**os.environ, 'OURLIBERTY_AGENTS_ROOT': str(agents_root)}
        proc = subprocess.run(
            [sys.executable, str(self.script), 'validate', 'nope'],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('not a file', proc.stderr)


if __name__ == '__main__':
    unittest.main()
