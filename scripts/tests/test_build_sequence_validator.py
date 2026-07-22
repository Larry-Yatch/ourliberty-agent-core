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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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


def _fake_git(table: dict, default=(1, '')):
    """Build an injectable git runner from a {argv-tuple: (rc, stdout)} map.

    Records every argv it's asked to run on the returned function's
    `.calls` list so tests can assert which git probes fired."""
    def run(argv):
        run.calls.append(list(argv))
        return table.get(tuple(argv), default)
    run.calls = []
    return run


class SpecDocPresenceTest(unittest.TestCase):
    """Incident 2026-06-10 sync-lag guard: distinguish a spec_doc that is
    merged-but-not-yet-synced (behind origin) from one that was never
    authored. git + local_exists are injected so the test is hermetic."""

    SPEC = 'agents/beacon/specs/missions-v2-phase2-resurfacing-and-derive.md'

    def test_present_locally_short_circuits(self):
        git = _fake_git({})
        res = bsv.check_spec_doc_presence(
            self.SPEC, repo_root=Path('/nope'),
            git=git, local_exists=lambda: True,
        )
        self.assertEqual(res.status, bsv.SPEC_DOC_PRESENT)
        self.assertTrue(res)  # __bool__ is True only when present
        self.assertEqual(git.calls, [])  # no git probes when present

    def test_missing_locally_present_on_origin_is_behind_origin(self):
        """The incident case: spec exists on origin/main, absent locally."""
        git = _fake_git({
            ('rev-parse', '--verify', '--quiet', 'origin/main'): (0, 'abc123'),
            ('cat-file', '-e', f'origin/main:{self.SPEC}'): (0, ''),
            ('rev-list', '--count', 'HEAD..origin/main'): (0, '1'),
        })
        res = bsv.check_spec_doc_presence(
            self.SPEC, repo_root=Path('/repo'),
            git=git, local_exists=lambda: False,
        )
        self.assertEqual(res.status, bsv.SPEC_DOC_BEHIND_ORIGIN)
        self.assertFalse(res)
        self.assertEqual(res.behind_by, 1)
        self.assertIn('ourliberty-sync.service', res.message)
        self.assertIn('do not re-author', res.message.lower())

    def test_missing_locally_absent_on_origin_is_not_authored(self):
        """The genuine missing-spec case: absent locally AND on origin/main."""
        git = _fake_git({
            ('rev-parse', '--verify', '--quiet', 'origin/main'): (0, 'abc123'),
            ('cat-file', '-e', f'origin/main:{self.SPEC}'): (1, ''),
        })
        res = bsv.check_spec_doc_presence(
            self.SPEC, repo_root=Path('/repo'),
            git=git, local_exists=lambda: False,
        )
        self.assertEqual(res.status, bsv.SPEC_DOC_NOT_AUTHORED)
        self.assertFalse(res)
        self.assertIn('author', res.message.lower())

    def test_missing_locally_no_origin_main_is_indeterminate(self):
        """Not a synced checkout (origin/main unresolved) → don't guess."""
        git = _fake_git({
            ('rev-parse', '--verify', '--quiet', 'origin/main'): (1, ''),
        })
        res = bsv.check_spec_doc_presence(
            self.SPEC, repo_root=Path('/repo'),
            git=git, local_exists=lambda: False,
        )
        self.assertEqual(res.status, bsv.SPEC_DOC_INDETERMINATE)
        self.assertFalse(res)

    def test_behind_origin_with_uncountable_commits_still_classifies(self):
        """rev-list failing to return a count doesn't break classification."""
        git = _fake_git({
            ('rev-parse', '--verify', '--quiet', 'origin/main'): (0, 'abc'),
            ('cat-file', '-e', f'origin/main:{self.SPEC}'): (0, ''),
            ('rev-list', '--count', 'HEAD..origin/main'): (1, ''),
        })
        res = bsv.check_spec_doc_presence(
            self.SPEC, repo_root=Path('/repo'),
            git=git, local_exists=lambda: False,
        )
        self.assertEqual(res.status, bsv.SPEC_DOC_BEHIND_ORIGIN)
        self.assertIsNone(res.behind_by)

    def test_behind_origin_zero_count_avoids_contradictory_message(self):
        """rev-list returning '0' (file on origin/main yet absent locally
        while HEAD is not behind — e.g. an uncommitted local deletion) must
        NOT print the self-contradictory 'behind by 0 commit(s)'."""
        git = _fake_git({
            ('rev-parse', '--verify', '--quiet', 'origin/main'): (0, 'abc'),
            ('cat-file', '-e', f'origin/main:{self.SPEC}'): (0, ''),
            ('rev-list', '--count', 'HEAD..origin/main'): (0, '0'),
        })
        res = bsv.check_spec_doc_presence(
            self.SPEC, repo_root=Path('/repo'),
            git=git, local_exists=lambda: False,
        )
        self.assertEqual(res.status, bsv.SPEC_DOC_BEHIND_ORIGIN)
        self.assertNotIn('by 0 commit', res.message)
        self.assertIn('one or more commits', res.message)

    def test_empty_spec_doc_is_indeterminate(self):
        res = bsv.check_spec_doc_presence('', repo_root=Path('/repo'),
                                          git=_fake_git({}))
        self.assertEqual(res.status, bsv.SPEC_DOC_INDETERMINATE)

    def test_non_string_spec_doc_is_indeterminate(self):
        res = bsv.check_spec_doc_presence(None, repo_root=Path('/repo'),
                                          git=_fake_git({}))
        self.assertEqual(res.status, bsv.SPEC_DOC_INDETERMINATE)


class SpecDocCliTest(unittest.TestCase):
    """CLI `check-spec-doc <seq-id|path>` exit-code coverage.

    Hermetic: instead of leaning on the ambient checkout (whose `origin/main`
    only resolves when the surrounding worktree happened to fetch it — the
    non-determinism that false-BLOCKed clean PRs #838/#839/#849 under the
    regression gate), every git-touching case runs against a throwaway git
    repo built under a /tmp TemporaryDirectory. The CLI is pointed at the
    fixture via SPEC_DOC_REPO_ROOT_ENV, so `origin/main` deterministically
    resolves regardless of the ambient checkout's fetch state. The env seam is
    inert when unset (production behavior unchanged)."""

    PRESENT_SPEC = 'agents/beacon/specs/present.md'
    ABSENT_SPEC = 'agents/beacon/specs/__this_spec_does_not_exist__.md'
    BEHIND_SPEC = 'agents/beacon/specs/behind-origin.md'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.script = _SCRIPTS_DIR / 'build_sequence_validator.py'
        # Isolate git from any ambient user/system config and supply a fixed
        # identity so commits succeed without touching the real gitconfig.
        self._git_env = {
            **os.environ,
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_AUTHOR_NAME': 'Forge Test',
            'GIT_AUTHOR_EMAIL': 'forge-test@example.invalid',
            'GIT_COMMITTER_NAME': 'Forge Test',
            'GIT_COMMITTER_EMAIL': 'forge-test@example.invalid',
        }
        self.repo = self._build_fixture_repo()

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ['git', '-C', str(cwd), *args],
            check=True, capture_output=True, text=True,
            env=self._git_env, timeout=30,
        )

    def _commit_spec(self, work: Path, rel_spec: str, msg: str) -> None:
        target = work / rel_spec
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'# {rel_spec}\n')
        self._git(work, 'add', rel_spec)
        self._git(work, 'commit', '-m', msg)

    def _build_fixture_repo(self) -> Path:
        """Init a work repo, commit PRESENT_SPEC, push `main` to a local bare
        origin, and fetch so `origin/main` resolves. Then advance origin with
        BEHIND_SPEC while resetting the work tree back — so BEHIND_SPEC exists
        on origin/main but not in the working copy (the behind-origin case),
        and ABSENT_SPEC exists in neither (the not-authored case)."""
        work = self.tmpdir / 'work'
        origin = self.tmpdir / 'origin.git'
        work.mkdir()
        self._git(work, 'init', '-q')
        # Version-robust default-branch pin (older git lacks `init -b`).
        self._git(work, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self._commit_spec(work, self.PRESENT_SPEC, 'add present spec')

        subprocess.run(
            ['git', 'init', '-q', '--bare', str(origin)],
            check=True, capture_output=True, text=True,
            env=self._git_env, timeout=30,
        )
        self._git(work, 'remote', 'add', 'origin', str(origin))
        self._git(work, 'push', '-q', '-u', 'origin', 'main')

        # Advance origin/main by one commit that adds BEHIND_SPEC, then move the
        # local branch + work tree back so that file is present on origin/main
        # but absent locally. The push updates the origin/main tracking ref to
        # the advanced commit; the reset leaves that ref untouched.
        self._commit_spec(work, self.BEHIND_SPEC, 'add spec that lands on origin only')
        self._git(work, 'push', '-q', 'origin', 'main')
        self._git(work, 'reset', '--hard', '-q', 'HEAD~1')
        return work

    def _seq_file(self, spec_doc: str) -> Path:
        f = self.tmpdir / 'seq.json'
        seq = _valid_sequence()
        seq['spec_doc'] = spec_doc
        f.write_text(json.dumps(seq))
        return f

    def _run(self, path: Path, *, use_fixture: bool = True
             ) -> subprocess.CompletedProcess:
        env = {**os.environ}
        if use_fixture:
            env[bsv.SPEC_DOC_REPO_ROOT_ENV] = str(self.repo)
        else:
            env.pop(bsv.SPEC_DOC_REPO_ROOT_ENV, None)
        return subprocess.run(
            [sys.executable, str(self.script), 'check-spec-doc', str(path)],
            capture_output=True, text=True, timeout=30, env=env,
        )

    def test_cli_present_spec_exits_zero(self):
        # Present in the fixture working copy → short-circuits before git.
        proc = self._run(self._seq_file(self.PRESENT_SPEC))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn('OK', proc.stdout)

    def test_cli_absent_spec_exits_one_not_authored(self):
        # Absent locally AND on origin/main (which resolves in the fixture) →
        # deterministically NOT_AUTHORED. This is the branch that flaked to
        # INDETERMINATE (exit 0) whenever the ambient origin/main didn't fetch.
        proc = self._run(self._seq_file(self.ABSENT_SPEC))
        self.assertEqual(proc.returncode, 1, msg=proc.stdout)
        self.assertIn('NOT_AUTHORED', proc.stderr)

    def test_cli_behind_origin_spec_exits_three(self):
        # Present on origin/main, absent in the working copy → behind-origin.
        proc = self._run(self._seq_file(self.BEHIND_SPEC))
        self.assertEqual(proc.returncode, 3, msg=proc.stdout + proc.stderr)
        self.assertIn('BEHIND_ORIGIN', proc.stderr)

    def test_cli_missing_sequence_file_exits_one(self):
        proc = self._run(self.tmpdir / 'nope.json')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('not a file', proc.stderr)

    def test_env_seam_inert_when_unset(self):
        # With the seam unset, the CLI anchors at REPO_ROOT (the ambient
        # checkout). A file that exists in this repo resolves PRESENT without
        # any git probe, proving the unset path preserves production behavior.
        proc = self._run(self._seq_file('agents/mirror/CLAUDE.md'),
                         use_fixture=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn('OK', proc.stdout)


def _seq_with_target(target_repo: str, spec_doc: str = 'BUILD_PLAN.md',
                     n_steps: int = 3) -> dict:
    """A valid sequence whose steps all declare `target_repo` and whose
    top-level spec_doc is `spec_doc`. Used by the target_repo resolution
    tests (mirrors rsdpm-v0-001's uniform-target shape)."""
    steps = []
    for i in range(n_steps):
        step = _valid_step(f's{i}', deps=[f's{i-1}'] if i else [])
        step['target_repo'] = target_repo
        steps.append(step)
    seq = _valid_sequence(steps=steps, current_steps=['s0'], status='pending')
    seq['spec_doc'] = spec_doc
    return seq


class EffectiveTargetRepoTest(unittest.TestCase):
    """effective_target_repo — the repo a sequence's steps predominantly hit."""

    def test_uniform_steps(self):
        seq = _seq_with_target('RSDPM', n_steps=20)
        self.assertEqual(bsv.effective_target_repo(seq), 'RSDPM')

    def test_majority_wins(self):
        s0 = _valid_step('s0'); s0['target_repo'] = 'RSDPM'
        s1 = _valid_step('s1', deps=['s0']); s1['target_repo'] = 'RSDPM'
        s2 = _valid_step('s2', deps=['s1']); s2['target_repo'] = 'ourliberty-dashboard'
        seq = _valid_sequence(steps=[s0, s1, s2], current_steps=['s0'])
        self.assertEqual(bsv.effective_target_repo(seq), 'RSDPM')

    def test_tie_breaks_to_first_seen(self):
        s0 = _valid_step('s0'); s0['target_repo'] = 'RSDPM'
        s1 = _valid_step('s1', deps=['s0']); s1['target_repo'] = 'ourliberty-graph'
        seq = _valid_sequence(steps=[s0, s1], current_steps=['s0'])
        self.assertEqual(bsv.effective_target_repo(seq), 'RSDPM')

    def test_no_steps_returns_none(self):
        seq = _valid_sequence()
        seq['steps'] = []
        self.assertIsNone(bsv.effective_target_repo(seq))

    def test_non_string_target_ignored(self):
        s0 = _valid_step('s0'); s0['target_repo'] = ''
        s1 = _valid_step('s1', deps=['s0']); s1['target_repo'] = 'RSDPM'
        seq = _valid_sequence(steps=[s0, s1], current_steps=['s0'])
        self.assertEqual(bsv.effective_target_repo(seq), 'RSDPM')

    def test_non_dict_input_returns_none(self):
        self.assertIsNone(bsv.effective_target_repo('nope'))


class ResolveSpecDocRepoRootTest(unittest.TestCase):
    """resolve_spec_doc_repo_root — which checkout a spec_doc resolves against.

    The env override wins; agent-core / unset / unmappable → None (REPO_ROOT,
    unchanged); a mapped cross-repo target → its checkout path."""

    REPO_PATHS = {
        'ourliberty-agent-core': Path('/home/larry/agent-core'),
        'RSDPM': Path('/home/larry/RSDPM'),
    }

    def test_env_override_wins_over_target_repo(self):
        seq = _seq_with_target('RSDPM')
        root = bsv.resolve_spec_doc_repo_root(
            seq,
            env={bsv.SPEC_DOC_REPO_ROOT_ENV: '/tmp/fixture'},
            repo_paths=self.REPO_PATHS,
        )
        self.assertEqual(root, Path('/tmp/fixture'))

    def test_agent_core_target_resolves_none(self):
        # The common case MUST stay byte-for-byte: None → REPO_ROOT downstream.
        seq = _seq_with_target('ourliberty-agent-core')
        root = bsv.resolve_spec_doc_repo_root(
            seq, env={}, repo_paths=self.REPO_PATHS)
        self.assertIsNone(root)

    def test_mapped_cross_repo_target_resolves_checkout(self):
        seq = _seq_with_target('RSDPM')
        root = bsv.resolve_spec_doc_repo_root(
            seq, env={}, repo_paths=self.REPO_PATHS)
        self.assertEqual(root, Path('/home/larry/RSDPM'))

    def test_unmapped_target_falls_back_to_none(self):
        seq = _seq_with_target('some-unknown-repo')
        root = bsv.resolve_spec_doc_repo_root(
            seq, env={}, repo_paths=self.REPO_PATHS)
        self.assertIsNone(root)

    def test_no_target_falls_back_to_none(self):
        seq = _valid_sequence()
        seq['steps'] = []
        root = bsv.resolve_spec_doc_repo_root(
            seq, env={}, repo_paths=self.REPO_PATHS)
        self.assertIsNone(root)


class TargetRepoSpecDocPresenceTest(unittest.TestCase):
    """Integration: a cross-repo sequence resolves its spec_doc against the
    target repo's local checkout — present there → PRESENT; absent there and
    on its origin/main → NOT_AUTHORED. Reuses SpecDocCliTest's hermetic /tmp
    git-fixture pattern so origin/main resolves deterministically."""

    PRESENT_SPEC = 'BUILD_PLAN.md'
    ABSENT_SPEC = '__no_such_spec__.md'
    REPO_X = 'RSDPM'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self._git_env = {
            **os.environ,
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_AUTHOR_NAME': 'Forge Test',
            'GIT_AUTHOR_EMAIL': 'forge-test@example.invalid',
            'GIT_COMMITTER_NAME': 'Forge Test',
            'GIT_COMMITTER_EMAIL': 'forge-test@example.invalid',
        }
        self.checkout = self._build_fixture_repo()
        self.repo_paths = {self.REPO_X: self.checkout}

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(['git', '-C', str(cwd), *args], check=True,
                       capture_output=True, text=True, env=self._git_env,
                       timeout=30)

    def _build_fixture_repo(self) -> Path:
        """A repoX checkout with PRESENT_SPEC committed + pushed to a local
        bare origin (so origin/main resolves), and ABSENT_SPEC in neither."""
        work = self.tmpdir / 'repoX'
        origin = self.tmpdir / 'origin.git'
        work.mkdir()
        self._git(work, 'init', '-q')
        self._git(work, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        (work / self.PRESENT_SPEC).write_text('# BUILD_PLAN\n')
        self._git(work, 'add', self.PRESENT_SPEC)
        self._git(work, 'commit', '-m', 'add build plan')
        subprocess.run(['git', 'init', '-q', '--bare', str(origin)],
                       check=True, capture_output=True, text=True,
                       env=self._git_env, timeout=30)
        self._git(work, 'remote', 'add', 'origin', str(origin))
        self._git(work, 'push', '-q', '-u', 'origin', 'main')
        return work

    def test_spec_present_in_target_checkout_is_present(self):
        seq = _seq_with_target(self.REPO_X, spec_doc=self.PRESENT_SPEC)
        root = bsv.resolve_spec_doc_repo_root(
            seq, env={}, repo_paths=self.repo_paths)
        self.assertEqual(root, self.checkout)
        presence = bsv.check_spec_doc_presence(seq['spec_doc'], repo_root=root)
        self.assertEqual(presence.status, bsv.SPEC_DOC_PRESENT)
        self.assertTrue(presence)

    def test_spec_absent_in_target_checkout_and_origin_is_not_authored(self):
        seq = _seq_with_target(self.REPO_X, spec_doc=self.ABSENT_SPEC)
        root = bsv.resolve_spec_doc_repo_root(
            seq, env={}, repo_paths=self.repo_paths)
        self.assertEqual(root, self.checkout)
        presence = bsv.check_spec_doc_presence(seq['spec_doc'], repo_root=root)
        self.assertEqual(presence.status, bsv.SPEC_DOC_NOT_AUTHORED)
        self.assertFalse(presence)


if __name__ == '__main__':
    unittest.main()
