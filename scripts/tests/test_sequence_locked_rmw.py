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


if __name__ == '__main__':
    unittest.main()
