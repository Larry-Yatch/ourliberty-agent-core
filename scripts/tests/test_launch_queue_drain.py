#!/usr/bin/env python3
"""Tests for launch_queue_drain.py — the Beacon-side drain of the
build-launch-queue (projects-v3 P3, p3-launch-queue-drain, spec
`agents/beacon/specs/projects-v3-p3-pipeline.md` § 7 step 3).

The three things Mirror reviews this step for, each pinned by a test class:

  * IdempotencyTest — idempotent on phase id. A re-queue of the same phase never
    authors a second sequence and never dispatches a second build; a pending
    sequence (Mirror dispatch lost to a crash) is re-kicked to the SAME inbox
    filename, an active/complete one is left alone.
  * ReusesOrchestratorMachineryTest — the authored sequence passes the REAL
    `build_sequence_validator.validate_dag`, and the build "kick" is the
    canonical `review-sequence-dag <seq-id>` routing-signal dispatched
    orchestrator → mirror via `safe_write_inbox`.
  * NonCommitterTest — the drain is a pure filesystem actor: it never imports or
    calls git, and it never writes projects.json.

The Mirror dispatch hits `safe_write_inbox.safe_write_inbox`, which raises under
the test jail (`refuse_under_test('inbox-write')`). Tests patch
`lqd.safe_write_inbox.safe_write_inbox` with a recorder so the drain logic is
exercised without a real inbox write — the recorder asserts the dispatch shape.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_launch_queue_drain
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import build_sequence_validator  # noqa: E402
import launch_dedup_guard as ldg  # noqa: E402
import launch_queue_drain as lqd  # noqa: E402


def _entry(phase_id='aging-idea', *, project_id='aging-idea',
           spec_ref='agents/beacon/specs/aging-idea.md',
           phase_title='Aging Idea', desired_end_state='ship the thing',
           repo='ourliberty-agent-core', **extra):
    e = {
        'phase_id': phase_id,
        'project_id': project_id,
        'seq_id': f'launch-{phase_id}',
        'spec_ref': spec_ref,
        'phase_title': phase_title,
        'desired_end_state': desired_end_state,
        'repo': repo,
        'requested_at': '2026-06-17T00:00:00+00:00',
        'requested_by': 'larry@test',
    }
    e.update(extra)
    return e


class _MirrorRecorder:
    """Stand-in for safe_write_inbox.safe_write_inbox. Records each dispatch and
    returns a Path (the real fn's contract) so dispatch_mirror_preflight works."""

    def __init__(self, inbox_dir: Path):
        self.inbox_dir = inbox_dir
        self.calls: list[dict] = []

    def __call__(self, *, target_agent, task_dict, source_agent, filename):
        self.calls.append({
            'target_agent': target_agent,
            'task_dict': dict(task_dict),
            'source_agent': source_agent,
            'filename': filename,
        })
        return self.inbox_dir / filename


class _DrainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='launch-drain-'))
        self.queue_dir = self.tmp / 'build-launch-queue'
        self.sequences_dir = self.tmp / 'build-sequences'
        self.inbox_dir = self.tmp / 'inbox'
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = _MirrorRecorder(self.inbox_dir)
        self._patch = mock.patch.object(
            lqd.safe_write_inbox, 'safe_write_inbox', self.recorder)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _queue(self, entry: dict) -> Path:
        path = self.queue_dir / f'{entry["phase_id"]}.json'
        path.write_text(json.dumps(entry, indent=2) + '\n')
        return path

    def _drain(self):
        return lqd.drain_once(
            queue_dir=self.queue_dir, sequences_dir=self.sequences_dir)

    def _seq_path(self, phase_id='aging-idea') -> Path:
        return self.sequences_dir / f'launch-{phase_id}.json'

    def _read_seq(self, phase_id='aging-idea') -> dict:
        return json.loads(self._seq_path(phase_id).read_text())


# ==================== happy path ====================


class HappyPathTest(_DrainTestBase):
    def test_new_phase_authors_sequence_dispatches_and_clears_queue(self):
        qpath = self._queue(_entry('aging-idea'))
        result = self._drain()

        self.assertEqual(result.launched, ['aging-idea'])
        self.assertEqual(result.redispatched, [])
        self.assertEqual(result.already_launched, [])
        self.assertEqual(result.dead_lettered, [])
        # Queue file consumed.
        self.assertFalse(qpath.exists())
        # Sequence authored at status pending.
        seq = self._read_seq('aging-idea')
        self.assertEqual(seq['seq_id'], 'launch-aging-idea')
        self.assertEqual(seq['status'], 'pending')
        self.assertEqual(len(seq['steps']), 1)
        self.assertEqual(seq['steps'][0]['step_id'], 'aging-idea')
        # Exactly one Mirror dispatch.
        self.assertEqual(len(self.recorder.calls), 1)

    def test_empty_tick_is_a_noop(self):
        result = self._drain()
        self.assertEqual(result.to_dict(), {
            'launched': [], 'redispatched': [], 'already_launched': [],
            'dead_lettered': [], 'dispatch_failed': [], 'deduped': [],
        })
        self.assertEqual(self.recorder.calls, [])


# ==================== idempotency on phase id ====================


class IdempotencyTest(_DrainTestBase):
    def test_requeue_after_pending_seq_redispatches_same_filename_no_second_seq(self):
        # First drain authors the sequence (status pending) + dispatches once.
        self._queue(_entry('aging-idea'))
        self._drain()
        seq_first = self._read_seq('aging-idea')
        first_filename = self.recorder.calls[0]['filename']

        # A re-queue (e.g. a double-click after the first queue file drained)
        # finds the pending sequence: re-dispatch Mirror to the SAME filename,
        # do NOT author a second sequence.
        self._queue(_entry('aging-idea'))
        result = self._drain()

        self.assertEqual(result.redispatched, ['aging-idea'])
        self.assertEqual(result.launched, [])
        # No second sequence — file is byte-identical to the first authoring.
        self.assertEqual(self._read_seq('aging-idea'), seq_first)
        # Re-dispatch overwrote the SAME deterministic inbox filename.
        self.assertEqual(len(self.recorder.calls), 2)
        self.assertEqual(self.recorder.calls[1]['filename'], first_filename)

    def test_requeue_after_active_seq_clears_queue_no_dispatch(self):
        self._queue(_entry('aging-idea'))
        self._drain()
        self.assertEqual(len(self.recorder.calls), 1)

        # Mirror PASSed and the notifier flipped the sequence to active.
        seq = self._read_seq('aging-idea')
        seq['status'] = 'active'
        self._seq_path('aging-idea').write_text(json.dumps(seq, indent=2) + '\n')

        # A re-queue of an already-launched phase must NOT dispatch a 2nd build.
        qpath = self._queue(_entry('aging-idea'))
        result = self._drain()

        self.assertEqual(result.already_launched, ['aging-idea'])
        self.assertEqual(result.launched, [])
        self.assertEqual(result.redispatched, [])
        self.assertFalse(qpath.exists())
        # No new Mirror dispatch.
        self.assertEqual(len(self.recorder.calls), 1)

    def test_requeue_after_complete_seq_clears_queue_no_dispatch(self):
        self._queue(_entry('aging-idea'))
        self._drain()
        seq = self._read_seq('aging-idea')
        seq['status'] = 'complete'
        self._seq_path('aging-idea').write_text(json.dumps(seq, indent=2) + '\n')

        qpath = self._queue(_entry('aging-idea'))
        result = self._drain()

        self.assertEqual(result.already_launched, ['aging-idea'])
        self.assertFalse(qpath.exists())
        self.assertEqual(len(self.recorder.calls), 1)

    def test_double_queue_in_one_tick_still_single_dispatch(self):
        # Two queue files can't both exist (endpoint locks on filename), but a
        # re-author within a tick still must not double-dispatch: drain the same
        # phase twice in one process without re-queueing in between.
        self._queue(_entry('aging-idea'))
        lqd._drain_one(
            self.queue_dir / 'aging-idea.json', self.sequences_dir,
            lqd.datetime.now(lqd.timezone.utc), lqd.DrainResult())
        # Sequence now exists; re-queue + drain again.
        self._queue(_entry('aging-idea'))
        result = self._drain()
        # Pending → re-dispatch (recovers a lost kick), but never a 2nd build.
        self.assertEqual(result.launched, [])
        self.assertIn('aging-idea', result.redispatched)


# ==================== reuses orchestrator machinery ====================


class ReusesOrchestratorMachineryTest(_DrainTestBase):
    def test_authored_sequence_passes_real_validate_dag(self):
        self._queue(_entry('aging-idea'))
        self._drain()
        seq = self._read_seq('aging-idea')
        validation = build_sequence_validator.validate_dag(seq)
        self.assertTrue(
            validation.valid,
            f'authored sequence failed validate_dag: {validation.errors}')

    def test_dispatch_is_canonical_mirror_routing_signal(self):
        self._queue(_entry('aging-idea'))
        self._drain()
        call = self.recorder.calls[0]
        self.assertEqual(call['target_agent'], 'mirror')
        self.assertEqual(call['source_agent'], 'orchestrator')
        self.assertEqual(call['filename'],
                         'review-sequence-dag-launch-aging-idea.json')
        task = call['task_dict']
        self.assertEqual(task['prompt'], 'review-sequence-dag launch-aging-idea')
        self.assertEqual(task['source'], 'orchestrator')
        self.assertEqual(task['phase'], 'routing-signal')

    def test_dispatch_text_within_500_chars(self):
        self._queue(_entry('aging-idea', desired_end_state='x' * 1000))
        self._drain()
        seq = self._read_seq('aging-idea')
        self.assertLessEqual(len(seq['steps'][0]['dispatch_text']), 500)


# ==================== dead-letter / robustness ====================


class DeadLetterTest(_DrainTestBase):
    def test_malformed_json_is_dead_lettered(self):
        bad = self.queue_dir / 'broken.json'
        bad.write_text('{not json')
        result = self._drain()
        self.assertEqual(result.dead_lettered, ['broken.json'])
        self.assertFalse(bad.exists())
        self.assertTrue((self.queue_dir / '.failed' / 'broken.json').exists())
        self.assertEqual(self.recorder.calls, [])

    def test_entry_without_phase_id_is_dead_lettered(self):
        p = self.queue_dir / 'noid.json'
        p.write_text(json.dumps({'project_id': 'x'}) + '\n')
        result = self._drain()
        self.assertEqual(result.dead_lettered, ['noid.json'])
        self.assertTrue((self.queue_dir / '.failed' / 'noid.json').exists())

    def test_invalid_target_repo_is_dead_lettered(self):
        # Belt-and-suspenders for Bug 1: a queue entry whose repo is not a
        # buildable repo (e.g. `ol-work` inherited from a capture origin) is
        # dead-lettered, never authored into an unbuildable sequence that would
        # sit `dispatched` forever (the 2026-06-19 wedge).
        with mock.patch.object(
            lqd, '_valid_repos',
            return_value=frozenset({'ourliberty-agent-core', 'ourliberty-dashboard'}),
        ):
            self._queue(_entry('bad-repo-phase', repo='ol-work'))
            result = self._drain()
        self.assertEqual(result.dead_lettered, ['bad-repo-phase.json'])
        self.assertEqual(result.launched, [])
        # No sequence authored, no Mirror dispatch, queue file moved to .failed.
        self.assertFalse(self._seq_path('bad-repo-phase').exists())
        self.assertEqual(self.recorder.calls, [])
        self.assertTrue(
            (self.queue_dir / '.failed' / 'bad-repo-phase.json').exists())

    def test_unreadable_config_fails_open_and_authors(self):
        # Fail open: if repo_paths can't be read (empty valid set), the drain
        # does NOT dead-letter — it authors as before, so a transient config
        # read miss never blocks otherwise-fine work. There are TWO repo seams:
        # the early `_valid_repos()` gate AND the author-time
        # `routing_validator.allowed_repos_for('forge')` gate in
        # `_resolve_target_repo`. On the droplet config/agent-models.json is
        # present, so the un-mocked author-time gate would still reject
        # 'ol-work'; empty BOTH to exercise the fail-open path.
        with mock.patch.object(lqd, '_valid_repos', return_value=frozenset()), \
                mock.patch.object(lqd.routing_validator, 'allowed_repos_for',
                                  return_value=[]):
            self._queue(_entry('aging-idea', repo='ol-work'))
            result = self._drain()
        self.assertEqual(result.launched, ['aging-idea'])
        self.assertEqual(result.dead_lettered, [])
        self.assertTrue(self._seq_path('aging-idea').exists())

    def test_dispatch_failure_leaves_queue_file_for_retry(self):
        self._queue(_entry('aging-idea'))
        with mock.patch.object(lqd.safe_write_inbox, 'safe_write_inbox',
                               side_effect=RuntimeError('inbox down')):
            result = lqd.drain_once(
                queue_dir=self.queue_dir, sequences_dir=self.sequences_dir)
        self.assertEqual(result.dispatch_failed, ['aging-idea'])
        # Sequence WAS authored (pending); queue file LEFT for next-tick retry.
        self.assertTrue(self._seq_path('aging-idea').exists())
        self.assertTrue((self.queue_dir / 'aging-idea.json').exists())

        # Next tick (inbox recovered): pending seq → re-dispatch, clear queue.
        result2 = self._drain()
        self.assertEqual(result2.redispatched, ['aging-idea'])
        self.assertFalse((self.queue_dir / 'aging-idea.json').exists())

    def test_max_per_tick_bounds_work(self):
        for i in range(5):
            self._queue(_entry(f'phase-{i}', project_id=f'proj-{i}'))
        result = lqd.drain_once(
            queue_dir=self.queue_dir, sequences_dir=self.sequences_dir,
            max_per_tick=2)
        self.assertEqual(len(result.launched), 2)
        # 3 queue files remain for the next tick.
        remaining = [p for p in self.queue_dir.iterdir()
                     if p.is_file() and p.suffix == '.json']
        self.assertEqual(len(remaining), 3)


# ==================== author-time target_repo guard ====================


class TargetRepoGuardTest(_DrainTestBase):
    """The launch-drain validates the entry's `repo` against Forge's
    allowed_repos at AUTHOR time (config/agent-models.json via
    routing_validator), so a non-allowed value fails here — not at dispatch as
    an opaque RoutingDenied that strands the build (the 'ol-work' incident,
    2026-06-19)."""

    def test_canonical_repo_authors_fine(self):
        self._queue(_entry('canon', project_id='canon',
                           repo='ourliberty-dashboard'))
        result = self._drain()
        self.assertEqual(result.launched, ['canon'])
        self.assertEqual(result.dead_lettered, [])
        seq = self._read_seq('canon')
        self.assertEqual(seq['steps'][0]['target_repo'], 'ourliberty-dashboard')

    def test_short_form_alias_maps_to_canonical(self):
        # `dashboard` is the bare short form of the canonical `ourliberty-
        # dashboard` — it normalizes to the canonical name (the alias can only
        # ever resolve onto an already-allowed repo). The early `_valid_repos()`
        # gate keys on the canonical repo_paths names (so the bare `dashboard`
        # would trip it before the author-time alias resolver runs); include the
        # alias in the mocked valid set so the entry reaches the resolver, which
        # maps it onto the real `ourliberty-dashboard` allow-list entry.
        with mock.patch.object(
            lqd, '_valid_repos',
            return_value=frozenset({'ourliberty-agent-core',
                                    'ourliberty-dashboard', 'dashboard'}),
        ):
            self._queue(_entry('aliased', project_id='aliased', repo='dashboard'))
            result = self._drain()
        self.assertEqual(result.launched, ['aliased'])
        self.assertEqual(result.dead_lettered, [])
        seq = self._read_seq('aliased')
        self.assertEqual(seq['steps'][0]['target_repo'], 'ourliberty-dashboard')

    def test_unknown_repo_rejected_at_author_time_with_alert(self):
        # 'ol-work' is a one-off bad value with no alias scheme — it must be
        # rejected at author time: dead-lettered (no sequence authored, no
        # Mirror dispatch) AND surfaced to Larry naming the phase + bad repo.
        # To reach the AUTHOR-time seam this test is about, 'ol-work' must first
        # survive the earlier `_valid_repos()` gate (which would otherwise dead-
        # letter it silently, with no alert); admit it there so it fails at
        # `_resolve_target_repo` → routing_validator.allowed_repos_for('forge'),
        # which (real config) has no 'ol-work' and no 'ourliberty-ol-work' alias.
        with mock.patch.object(
            lqd, '_valid_repos',
            return_value=frozenset({'ourliberty-agent-core',
                                    'ourliberty-dashboard', 'ol-work'}),
        ), mock.patch.object(lqd.larry_alerts, 'append_alert') as alert:
            qpath = self._queue(
                _entry('badrepo', project_id='badrepo', repo='ol-work'))
            result = self._drain()

        self.assertEqual(result.dead_lettered, ['badrepo.json'])
        self.assertEqual(result.launched, [])
        # No doomed sequence authored; no Mirror dispatch (no dispatch-time
        # RoutingDenied possible).
        self.assertFalse(self._seq_path('badrepo').exists())
        self.assertEqual(self.recorder.calls, [])
        # Queue file moved to .failed (dead-letter), not silently left.
        self.assertFalse(qpath.exists())
        self.assertTrue((self.queue_dir / '.failed' / 'badrepo.json').exists())
        # Larry-actionable alert names the phase + the bad repo.
        self.assertEqual(alert.call_count, 1)
        kwargs = alert.call_args.kwargs
        self.assertEqual(kwargs['source'], 'launch-queue-drain')
        self.assertEqual(kwargs['subject'], 'badrepo')
        self.assertIn('ol-work', kwargs['message'])
        self.assertIn('badrepo', kwargs['message'])

    def test_missing_repo_falls_back_to_default_build_repo(self):
        entry = _entry('norepo', project_id='norepo')
        del entry['repo']
        self._queue(entry)
        result = self._drain()
        self.assertEqual(result.launched, ['norepo'])
        seq = self._read_seq('norepo')
        self.assertEqual(seq['steps'][0]['target_repo'], lqd._DEFAULT_BUILD_REPO)


# ==================== non-committer ====================


class NonCommitterTest(_DrainTestBase):
    def test_drain_never_writes_projects_json(self):
        self._queue(_entry('aging-idea'))
        self._drain()
        # The drain only writes under sequences_dir + the inbox (recorded); it
        # never touches a projects.json. Assert none was created anywhere in tmp.
        stray = list(self.tmp.rglob('projects.json'))
        self.assertEqual(stray, [], f'drain wrote projects.json: {stray}')

    def test_module_imports_no_git(self):
        # The drain is a pure-filesystem non-committer (single-committer
        # invariant): it must not import subprocess/git machinery at module load.
        src = (Path(lqd.__file__)).read_text()
        self.assertNotIn('import subprocess', src)
        self.assertNotIn('subprocess.', src)


class DuplicateWorkGuardTest(_DrainTestBase):
    """The launch_dedup_guard wiring (2026-06-20 cross-identity redundant-build
    incident): a matching deliverable claim reversibly HOLDS the launch (no
    sequence, no Mirror dispatch); an in-flight spec overlap is advisory-only;
    and any guard failure FAILS OPEN (authors normally)."""

    def _claim(self, claimed_task_id, envelope_task_id='the-standing-brain'):
        ldg.record_claim(
            claimed_task_id=claimed_task_id, envelope_task_id=envelope_task_id,
            path=ldg.claims_path_for_sequences_dir(self.sequences_dir))

    def test_matching_claim_holds_launch_and_skips_build(self):
        self._claim('aging-idea')
        self._queue(_entry('aging-idea'))
        with mock.patch.object(lqd.larry_alerts, 'append_alert') as alert:
            result = self._drain()
        # No sequence authored, no Mirror dispatched, queue file held in .deduped/.
        self.assertEqual(result.deduped, ['aging-idea'])
        self.assertEqual(result.launched, [])
        self.assertFalse(self._seq_path('aging-idea').exists())
        self.assertEqual(self.recorder.calls, [])
        held = self.queue_dir / '.deduped' / 'aging-idea.json'
        self.assertTrue(held.exists())
        self.assertFalse((self.queue_dir / 'aging-idea.json').exists())
        alert.assert_called_once()
        # The alert spells out the reversible re-queue command.
        msg = alert.call_args.kwargs.get('message', '')
        self.assertIn('mv', msg)
        self.assertIn('the-standing-brain', msg)

    def test_inflight_spec_overlap_proceeds_with_advisory(self):
        # A LIVE sibling sequence sharing this launch's spec_doc → advisory only.
        sibling = {
            'seq_id': 'launch-sibling', 'status': 'active',
            'spec_doc': 'agents/beacon/specs/aging-idea.md', 'steps': [],
        }
        (self.sequences_dir).mkdir(parents=True, exist_ok=True)
        (self.sequences_dir / 'launch-sibling.json').write_text(
            json.dumps(sibling, indent=2) + '\n')
        self._queue(_entry('aging-idea'))  # default spec_ref == sibling spec_doc
        with mock.patch.object(lqd.larry_alerts, 'append_alert') as alert:
            result = self._drain()
        # Proceeds: sequence authored + Mirror dispatched, but an advisory fired.
        self.assertEqual(result.launched, ['aging-idea'])
        self.assertEqual(result.deduped, [])
        self.assertTrue(self._seq_path('aging-idea').exists())
        self.assertEqual(len(self.recorder.calls), 1)
        alert.assert_called_once()

    def test_hold_move_failure_leaves_file_and_does_not_count_deduped(self):
        # If the .deduped/ move fails (e.g. EROFS), the launch is NOT counted
        # deduped, no build is authored, and the queue file stays for next-tick
        # re-evaluation — with an honest "could NOT be parked" alert.
        self._claim('aging-idea')
        qpath = self._queue(_entry('aging-idea'))
        with mock.patch.object(lqd.os, 'replace', side_effect=OSError('EROFS')):
            with mock.patch.object(lqd.larry_alerts, 'append_alert') as alert:
                result = self._drain()
        self.assertEqual(result.deduped, [])
        self.assertEqual(result.launched, [])
        self.assertFalse(self._seq_path('aging-idea').exists())
        self.assertEqual(self.recorder.calls, [])
        self.assertTrue(qpath.exists())  # left in place for retry
        self.assertFalse((self.queue_dir / '.deduped' / 'aging-idea.json').exists())
        self.assertIn('could NOT be parked', alert.call_args.kwargs.get('message', ''))

    def test_guard_failure_fails_open_and_authors(self):
        self._queue(_entry('aging-idea'))
        with mock.patch.object(lqd.launch_dedup_guard, 'evaluate',
                               side_effect=RuntimeError('boom')):
            result = self._drain()
        # A guard crash must never block an otherwise-fine launch.
        self.assertEqual(result.launched, ['aging-idea'])
        self.assertEqual(result.deduped, [])
        self.assertTrue(self._seq_path('aging-idea').exists())
        self.assertEqual(len(self.recorder.calls), 1)

    def test_nonmatching_claim_proceeds_normally(self):
        self._claim('some-unrelated-phase')
        self._queue(_entry('aging-idea'))
        result = self._drain()
        self.assertEqual(result.launched, ['aging-idea'])
        self.assertEqual(result.deduped, [])
        self.assertTrue(self._seq_path('aging-idea').exists())


if __name__ == '__main__':
    unittest.main()
