#!/usr/bin/env python3
"""Regression: build-sequence read-modify-write is serialized across writers.

`atomic_io.atomic_write_json` makes each individual write torn-free, but it does
NOTHING for the load -> mutate -> write WINDOW. Two writers can each read the
same on-disk sequence, each append a distinct `audit_log` entry to their own
copy, and the second write clobbers the first — a lost update. Because audit_log
entries are a control channel (e.g. `dag-preflight-sync-lag-detected` arms a
healer check), a silently dropped entry can wedge the pipeline.

`sequence_shortcut_helpers.locked_sequence_update` is THE choke point that closes
that window: every writer routes its whole RMW through it, so only one is inside
the critical section at a time.

These tests prove the guarantee end-to-end:

  * `test_concurrent_locked_rmw_preserves_both_updates` — two writers whose RMW
    windows are forced to overlap (a barrier + an in-window sleep) both survive
    when routed through the lock.
  * `test_unlocked_rmw_loses_an_update_control` — the SAME interleaving WITHOUT
    the lock deterministically loses one update. This is the control that proves
    the harness can actually detect the bug, so the locked test's success is
    meaningful rather than vacuous.
  * `test_reentrant_nested_lock_does_not_deadlock` — an outer coarse lock (the
    advancer wrapping a whole per-sequence tick) nesting an `apply_*` helper
    (which self-locks) runs the inner RMW inside the same critical section
    instead of self-blocking on the non-re-entrant flock.

`PerWriterSerializationTest` then pins the MIGRATION rather than the helper: the
tests above pass whether or not any production writer actually routes through the
choke point, so on their own they prove a lock EXISTS, not that it is USED. Each
test below drives a real production writer from N concurrent processes and
asserts the outcome a lost update destroys — the same cross-process shape
`test_ledger_rmw_concurrency` uses for the #917 ledger migration:

  * `test_apply_step_pr_opened_no_lost_updates` — the `@_under_seq_lock`
    decorator path (all nine `apply_*` / `claim_*` helpers share it).
  * `test_claim_completion_signal_claimed_exactly_once` — the one-shot claim that
    gates the exactly-once completion DM. An unserialized read-check-write lets
    two processes both win and double-DM Larry.
  * `test_heal_pipeline_stall_audit_no_lost_updates` and
    `test_outbox_notifier_dag_audit_no_lost_updates` — the two audit_log writers
    whose lost update was demonstrated concretely on PR #1053. These entries are
    the CONTROL CHANNEL that arms heal_pipeline_stall Checks 9/12, so a dropped
    one disarms the backstop.

STILL UNPINNED (deliberately, fixture cost): `build_sequence_advancer.tick`'s
coarse per-sequence lock, its `_drain_kickoff_inbox` span,
`launch_queue_drain._drain_one`, and `build_sequence_kickoff`'s
`@_kickoff_under_lock`. Reverting any of those four leaves this file green — if
you touch them, add a proof here rather than assuming this file covers them.

Test isolation (PR #137): `sequence_shortcut_helpers.AGENTS_ROOT` is rerouted to
a per-test tmpdir; no write reaches `~/agents/`.

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_sequence_locked_rmw
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import atomic_io  # noqa: E402
import file_lock  # noqa: E402
import sequence_shortcut_helpers as ssh  # noqa: E402


def _seed_sequence(seq_id: str, status: str = 'active') -> dict:
    return {
        'seq_id': seq_id,
        'label': f'Sequence {seq_id}',
        'status': status,
        'current_steps': [],
        'steps': [],
        'audit_log': [
            {'ts': '2026-05-27T00:00:00+00:00', 'event': 'sequence-created',
             'actor': 'beacon'},
        ],
    }


class _LockedRmwHarness(unittest.TestCase):
    """Reroute `sequence_shortcut_helpers.AGENTS_ROOT` to a per-test tmpdir so
    no test write reaches `~/agents/` on the droplet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._ssh_original = ssh.AGENTS_ROOT
        ssh.AGENTS_ROOT = self._root
        self._seq_dir = self._root / 'blackboard' / 'build-sequences'
        self._seq_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        ssh.AGENTS_ROOT = self._ssh_original
        self._tmp.cleanup()

    def _path(self, seq_id: str) -> Path:
        return self._seq_dir / f'{seq_id}.json'

    def _write(self, seq: dict) -> Path:
        path = self._path(seq['seq_id'])
        path.write_text(json.dumps(seq, indent=2))
        return path

    def _read(self, seq_id: str) -> dict:
        return json.loads(self._path(seq_id).read_text())

    def _events(self, seq_id: str) -> list[str]:
        return [e.get('event') for e in self._read(seq_id)['audit_log']]


class ConcurrentLockedRmwTests(_LockedRmwHarness):

    def test_concurrent_locked_rmw_preserves_both_updates(self):
        """Two writers whose RMW windows overlap both survive under the lock."""
        seq_id = 'locked-rmw-001'
        self._write(_seed_sequence(seq_id))
        path = self._path(seq_id)

        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def writer(event: str, in_window_sleep: float) -> None:
            try:
                start.wait()  # both threads race for the lock at the same instant
                with ssh.locked_sequence_update(seq_id) as held:
                    self.assertTrue(held, 'flock should be held (not degraded)')
                    data = json.loads(path.read_text())  # fresh read, IN lock
                    time.sleep(in_window_sleep)           # widen the RMW window
                    data['audit_log'].append({'event': event, 'actor': 't'})
                    atomic_io.atomic_write_json(path, data)
            except BaseException as exc:  # noqa: BLE001 — surface to main thread
                errors.append(exc)

        # Stagger the in-window sleeps so, whichever wins the race, the LOSER's
        # read happens after the winner's write — proving the read is genuinely
        # inside the critical section (not merely last-writer-wins by luck).
        t1 = threading.Thread(target=writer, args=('writer-a', 0.10))
        t2 = threading.Thread(target=writer, args=('writer-b', 0.10))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(errors, [], f'writer thread(s) raised: {errors}')
        self.assertFalse(t1.is_alive() or t2.is_alive(), 'writer thread hung')

        events = self._events(seq_id)
        # Both distinct appends survived — the lost update is closed.
        self.assertIn('writer-a', events)
        self.assertIn('writer-b', events)
        self.assertEqual(
            len(events), 3,
            f'expected seed + 2 appends, got audit_log events={events}',
        )

    def test_unlocked_rmw_loses_an_update_control(self):
        """CONTROL: the identical interleaving WITHOUT the lock loses one update.

        This proves the test methodology can detect the lost-update bug — so the
        locked test above is a meaningful pass, not a vacuous one."""
        seq_id = 'unlocked-rmw-001'
        self._write(_seed_sequence(seq_id))
        path = self._path(seq_id)

        both_have_read = threading.Barrier(2)
        errors: list[BaseException] = []

        def racer(event: str) -> None:
            try:
                data = json.loads(path.read_text())  # read OUTSIDE any lock
                both_have_read.wait()  # force both to read the SAME seed state
                data['audit_log'].append({'event': event, 'actor': 't'})
                atomic_io.atomic_write_json(path, data)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=racer, args=('racer-a',))
        t2 = threading.Thread(target=racer, args=('racer-b',))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(errors, [], f'racer thread(s) raised: {errors}')
        events = self._events(seq_id)
        # Deterministic lost update: both read seed (1 entry), each wrote seed+1,
        # the second write clobbered the first → exactly one append survives.
        self.assertEqual(
            len(events), 2,
            f'unlocked RMW should lose one update; got events={events}',
        )

    def test_reentrant_nested_lock_does_not_deadlock(self):
        """An outer coarse lock nesting a self-locking `apply_*` must run the
        inner RMW inside the same critical section (the flock is not re-entrant;
        a second real acquire on the same thread would self-block then degrade).

        This is the advancer's coarse-tick + fine-grained-`apply_*` pattern."""
        seq_id = 'reentrant-001'
        self._write(_seed_sequence(seq_id, status='active'))

        done = threading.Event()
        result_box: dict[str, object] = {}

        def run() -> None:
            with ssh.locked_sequence_update(seq_id) as outer_held:
                result_box['outer_held'] = outer_held
                # apply_pause is @_under_seq_lock → nested acquire on same thread.
                result_box['pause'] = ssh.apply_pause(seq_id, actor='larry')
            done.set()

        worker = threading.Thread(target=run)
        worker.start()
        # If re-entrancy were broken the inner acquire blocks ~5s (timeout) then
        # degrades; a generous join still catches a true deadlock.
        finished = done.wait(timeout=10)
        worker.join(timeout=10)

        self.assertTrue(finished, 'nested lock deadlocked (re-entrancy broken)')
        self.assertTrue(result_box.get('outer_held'), 'outer flock not held')
        pause = result_box.get('pause')
        self.assertTrue(getattr(pause, 'applied', False),
                        f'nested apply_pause did not apply: {pause}')
        on_disk = self._read(seq_id)
        self.assertEqual(on_disk['status'], 'paused')
        self.assertEqual(on_disk['audit_log'][-1]['event'], 'paused')


# --------------------------------------------------------------------------- #
# Per-writer proofs: does the MIGRATION hold, not just the helper?
# --------------------------------------------------------------------------- #
# Cross-process, matching `test_ledger_rmw_concurrency`'s shape for the #917
# ledger migration: N processes each apply a DISJOINT block of mutations to the
# SAME sequence file. Every mutation is distinct, so a correct serialized RMW
# ends with exactly N*M of them; a lost update drops some. Kept modest — a lost
# update reproduces at low counts and this runs on every regression pass.
_N_PROCS = 4
_PER_PROC = 25
_TOTAL = _N_PROCS * _PER_PROC

# Exit code a claim-race worker uses to report "I won the one-shot claim".
_CLAIMED_EXIT = 7


# Top-level workers (importable/picklable for the 'spawn' start method). Each
# re-points its own process's AGENTS_ROOT at the shared tmpdir where needed; the
# two audit appenders take an explicit seq_path so they need no root.

def _apply_step_pr_opened_worker(
    root: str, seq_id: str, base: int, count: int,
) -> None:
    import sequence_shortcut_helpers as ssh_child
    ssh_child.AGENTS_ROOT = Path(root)
    for i in range(base, base + count):
        result = ssh_child.apply_step_pr_opened(
            seq_id, f'step-{i}', f'https://github.com/x/y/pull/{i}',
        )
        if not result.applied:
            raise SystemExit(f'step-{i} not applied: {result.reason}')


def _claim_completion_worker(root: str, seq_id: str) -> None:
    import sequence_shortcut_helpers as ssh_child
    ssh_child.AGENTS_ROOT = Path(root)
    result = ssh_child.claim_completion_signal(seq_id, actor='notifier')
    if getattr(result, 'error', False):
        raise SystemExit(f'hard error claiming: {result.reason}')
    raise SystemExit(_CLAIMED_EXIT if result.applied else 0)


def _heal_audit_worker(seq_path: str, base: int, count: int) -> None:
    import heal_pipeline_stall as hps
    for i in range(base, base + count):
        ok = hps._append_sequence_audit(Path(seq_path), {
            'ts': f'2026-05-27T00:00:{i:02d}+00:00',
            'event': 'dag-preflight-sync-lag-detected',
            'actor': 'heal-pipeline-stall',
            'mirror_task_id': f'mirror-{i}',
        })
        if not ok:
            raise SystemExit(f'heal append {i} reported failure')


def _notifier_audit_worker(
    seq_path: str, seq_id: str, base: int, count: int,
) -> None:
    import outbox_notifier as on
    for i in range(base, base + count):
        ok = on._append_dag_audit_entry(
            Path(seq_path), seq_id,
            'dag-preflight-revision-routed', f'mirror-{i}',
        )
        if not ok:
            raise SystemExit(f'notifier append {i} reported failure')


def _run(worker, *args) -> list[int]:
    """Start `_N_PROCS` workers over disjoint id blocks; return their exitcodes.

    A worker taking (…, base, count) gets its own block; a worker with no block
    args (the claim race) is started `_N_PROCS` times with identical args.
    """
    ctx = multiprocessing.get_context()
    procs = []
    for n in range(_N_PROCS):
        if worker in (_apply_step_pr_opened_worker, _heal_audit_worker,
                      _notifier_audit_worker):
            p_args = (*args, n * _PER_PROC, _PER_PROC)
        else:
            p_args = args
        procs.append(ctx.Process(target=worker, args=p_args))
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    return [p.exitcode for p in procs]


@unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
class PerWriterSerializationTest(_LockedRmwHarness):
    """Each test drives a REAL production writer concurrently. These are the
    tests that fail if a migration site is reverted — the helper-level tests
    above stay green either way, so without these the migration is unpinned."""

    def _audit_events(self, seq_id: str, event: str) -> list[dict]:
        return [
            e for e in self._read(seq_id)['audit_log']
            if isinstance(e, dict) and e.get('event') == event
        ]

    def test_apply_step_pr_opened_no_lost_updates(self):
        """The `@_under_seq_lock` decorator path: every concurrent step update
        survives. Without the decorator, overlapping helpers each write from
        their own snapshot and steps silently revert to `dispatched`."""
        seq_id = 'apply-race-001'
        seq = _seed_sequence(seq_id)
        seq['steps'] = [
            {'step_id': f'step-{i}', 'status': 'dispatched', 'depends_on': []}
            for i in range(_TOTAL)
        ]
        self._write(seq)

        codes = _run(_apply_step_pr_opened_worker, str(self._root), seq_id)
        self.assertEqual(
            codes, [0] * _N_PROCS,
            f'worker(s) failed (exitcodes={codes}) — see child stderr',
        )

        on_disk = self._read(seq_id)
        missing = [
            s['step_id'] for s in on_disk['steps']
            if s.get('status') != 'reviewing' or not s.get('pr_url')
        ]
        self.assertEqual(
            missing, [],
            f'lost update: {len(missing)} of {_TOTAL} step updates were '
            f'clobbered (first few: {missing[:5]})',
        )
        self.assertEqual(
            len(self._audit_events(seq_id, 'step-pr-opened')), _TOTAL,
            'lost update: audit_log is missing step-pr-opened entries',
        )

    def test_claim_completion_signal_claimed_exactly_once(self):
        """`is_completion_signaled`'s docstring promises `claim_completion_signal`
        "re-checks under the read-modify-write so two racing ticks can't both
        win". Unserialized, two processes both read an unsignaled sequence, both
        append the marker, and Larry gets the completion DM twice."""
        seq_id = 'claim-race-001'
        seq = _seed_sequence(seq_id, status='complete')
        self._write(seq)

        codes = _run(_claim_completion_worker, str(self._root), seq_id)
        self.assertNotIn(
            1, codes, f'a worker hit a hard error (exitcodes={codes})')
        winners = codes.count(_CLAIMED_EXIT)
        markers = self._audit_events(
            seq_id, ssh.SEQUENCE_COMPLETE_SIGNALED_EVENT)
        self.assertEqual(
            len(markers), 1,
            f'exactly-once broken: {len(markers)} completion markers on disk '
            f'(a duplicate marker means a duplicate Larry DM)',
        )
        self.assertEqual(
            winners, 1,
            f'exactly-once broken: {winners} of {_N_PROCS} processes were told '
            f'they won the claim',
        )

    def test_heal_pipeline_stall_audit_no_lost_updates(self):
        """`heal_pipeline_stall._append_sequence_audit` — half of the pair whose
        lost update was demonstrated on PR #1053. A dropped
        `dag-preflight-sync-lag-detected` disarms Check 12."""
        seq_id = 'heal-race-001'
        self._write(_seed_sequence(seq_id))

        codes = _run(_heal_audit_worker, str(self._path(seq_id)))
        self.assertEqual(
            codes, [0] * _N_PROCS,
            f'worker(s) failed (exitcodes={codes}) — see child stderr',
        )
        entries = self._audit_events(
            seq_id, 'dag-preflight-sync-lag-detected')
        ids = {e.get('mirror_task_id') for e in entries}
        self.assertEqual(
            len(ids), _TOTAL,
            f'lost update: {_TOTAL - len(ids)} control-channel audit entries '
            f'were clobbered ({len(ids)} of {_TOTAL} survived)',
        )

    def test_outbox_notifier_dag_audit_no_lost_updates(self):
        """`outbox_notifier._append_dag_audit_entry` — the other half. A dropped
        `dag-preflight-revision-routed` disarms Check 9, and the sync-lag branch
        writes no Beacon notify, so the REVISION is lost in total silence."""
        seq_id = 'notifier-race-001'
        self._write(_seed_sequence(seq_id))

        codes = _run(
            _notifier_audit_worker, str(self._path(seq_id)), seq_id)
        self.assertEqual(
            codes, [0] * _N_PROCS,
            f'worker(s) failed (exitcodes={codes}) — see child stderr',
        )
        entries = self._audit_events(
            seq_id, 'dag-preflight-revision-routed')
        ids = {e.get('mirror_task_id') for e in entries}
        self.assertEqual(
            len(ids), _TOTAL,
            f'lost update: {_TOTAL - len(ids)} control-channel audit entries '
            f'were clobbered ({len(ids)} of {_TOTAL} survived)',
        )


if __name__ == '__main__':
    unittest.main()
