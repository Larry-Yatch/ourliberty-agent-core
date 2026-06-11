"""Tests for scripts/file_lock.py (shared advisory cross-process flock).

unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- sidecar_lock_path derives <dir>/<name>.lock (suffix kept).
- a held lock blocks a second exclusive acquire; a bounded acquire raises
  LockTimeout rather than hanging.
- mutual exclusion under real multi-process contention: N processes each do M
  read-modify-write increments on a shared counter file; the lock guarantees the
  final value is exactly N*M (no lost updates).
- a platform without fcntl degrades to a no-op context manager (body still runs).
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import file_lock  # noqa: E402


# Top-level workers (must be importable/picklable for the 'spawn' start method).

def _hold_worker(lock_path: str, ready_q, release_evt) -> None:
    """Acquire the lock, signal readiness, hold until told to release."""
    with file_lock.exclusive_lock(lock_path):
        ready_q.put('held')
        release_evt.wait(timeout=30)


def _increment_worker(lock_path: str, counter_path: str, iterations: int) -> None:
    """RMW a shared counter file `iterations` times, each under the lock."""
    cp = Path(counter_path)
    for _ in range(iterations):
        with file_lock.exclusive_lock(lock_path):
            try:
                value = int(cp.read_text())
            except (FileNotFoundError, ValueError):
                value = 0
            cp.write_text(str(value + 1))


class SidecarPathTest(unittest.TestCase):
    def test_keeps_extension(self):
        p = file_lock.sidecar_lock_path('/a/b/larry-alerts.jsonl')
        self.assertEqual(p, Path('/a/b/larry-alerts.jsonl.lock'))


class TimeoutTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self._tmp.name) / 'x.lock'

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_bounded_acquire_raises_when_held_by_another_process(self):
        ctx = multiprocessing.get_context()
        ready_q = ctx.Queue()
        release_evt = ctx.Event()
        holder = ctx.Process(
            target=_hold_worker, args=(str(self.lock_path), ready_q, release_evt))
        holder.start()
        try:
            self.assertEqual(ready_q.get(timeout=10), 'held')  # lock is now held
            with self.assertRaises(file_lock.LockTimeout):
                with file_lock.exclusive_lock(self.lock_path, timeout=0.3):
                    pass
        finally:
            release_evt.set()
            holder.join(timeout=10)

    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_acquire_succeeds_after_release(self):
        # Sanity: once nobody holds it, a bounded acquire works immediately.
        with file_lock.exclusive_lock(self.lock_path, timeout=1.0):
            pass
        with file_lock.exclusive_lock(self.lock_path, timeout=1.0):
            pass


class MutualExclusionTest(unittest.TestCase):
    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_no_lost_updates_under_contention(self):
        with tempfile.TemporaryDirectory() as d:
            lock_path = Path(d) / 'c.lock'
            counter_path = Path(d) / 'counter'
            counter_path.write_text('0')
            n_procs, iters = 4, 200
            ctx = multiprocessing.get_context()
            procs = [
                ctx.Process(target=_increment_worker,
                            args=(str(lock_path), str(counter_path), iters))
                for _ in range(n_procs)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=30)
                self.assertEqual(p.exitcode, 0)
            # Without the lock this RMW races and the total is < n_procs*iters.
            self.assertEqual(int(counter_path.read_text()), n_procs * iters)


class NoFlockDegradesTest(unittest.TestCase):
    def test_no_op_when_fcntl_unavailable(self):
        orig = file_lock._HAVE_FLOCK
        file_lock._HAVE_FLOCK = False
        try:
            ran = []
            with file_lock.exclusive_lock('/nonexistent/dir/should-not-matter.lock'):
                ran.append(True)
            self.assertEqual(ran, [True])
        finally:
            file_lock._HAVE_FLOCK = orig


if __name__ == '__main__':
    unittest.main()
