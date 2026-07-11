"""Tests for atomic_io.locked_update — the cross-process read-modify-write lock.

``atomic_write_json`` makes the file *swap* atomic; ``locked_update`` serializes
the surrounding load→mutate→write so two processes can't lost-update each other.
These cover the contract: it yields and runs the body, it reports whether the
lock was actually held, and it FAIL-OPENs (runs the body unserialized, never
wedges) when the lock is contended past the timeout or fcntl is unavailable.

stdlib unittest only — pytest is NOT installed in the droplet test/regression
environment.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import atomic_io  # noqa: E402
import file_lock  # noqa: E402


def _hold_sidecar_worker(data_path: str, ready_q, release_evt) -> None:
    """Hold the sidecar lock for ``data_path`` until told to release."""
    lock_path = file_lock.sidecar_lock_path(data_path)
    with file_lock.exclusive_lock(lock_path):
        ready_q.put('held')
        release_evt.wait(timeout=30)


class LockedUpdateBasicsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / 'state.json'

    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_yields_true_and_runs_body(self):
        ran = []
        with atomic_io.locked_update(self.data) as held:
            ran.append(True)
            atomic_io.atomic_write_json(self.data, {'a': 1})
        self.assertEqual(ran, [True])
        self.assertTrue(held)

    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_creates_sidecar_next_to_data(self):
        with atomic_io.locked_update(self.data):
            pass
        self.assertTrue((self.data.parent / (self.data.name + '.lock')).exists())

    def test_degrades_to_body_when_flock_unavailable(self):
        orig = file_lock._HAVE_FLOCK
        file_lock._HAVE_FLOCK = False
        try:
            ran = []
            with atomic_io.locked_update(self.data) as held:
                ran.append(True)
            self.assertEqual(ran, [True])   # body still runs
            self.assertFalse(held)          # but not serialized
        finally:
            file_lock._HAVE_FLOCK = orig

    def test_degrades_when_acquire_raises_oserror(self):
        """A transient OSError while creating the .lock sidecar (EROFS/ENOSPC/
        EMFILE) must NOT escape — it degrades to an unserialized run, so the
        ledgers' "never raises" contract (esp. suite_guardian's out-of-try
        decorator) holds."""
        orig = file_lock.exclusive_lock

        def _boom(*a, **k):
            raise OSError('sidecar unwritable')

        file_lock.exclusive_lock = _boom
        try:
            ran = []
            with atomic_io.locked_update(self.data) as held:
                ran.append(True)
            self.assertEqual(ran, [True])   # body ran, no raise
            self.assertFalse(held)          # unserialized
        finally:
            file_lock.exclusive_lock = orig

    def test_body_exception_still_propagates(self):
        """Degrading on ACQUISITION failure must not swallow an exception raised
        by the BODY — only acquisition errors are absorbed."""
        class Sentinel(RuntimeError):
            pass
        with self.assertRaises(Sentinel):
            with atomic_io.locked_update(self.data):
                raise Sentinel()

    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_fail_open_when_contended_past_timeout(self):
        """A peer holding the lock must NOT wedge us: past the bounded timeout the
        body still runs (held=False), degrading to pre-lock behavior."""
        ctx = multiprocessing.get_context()
        ready_q = ctx.Queue()
        release_evt = ctx.Event()
        holder = ctx.Process(
            target=_hold_sidecar_worker,
            args=(str(self.data), ready_q, release_evt))
        holder.start()
        try:
            self.assertEqual(ready_q.get(timeout=10), 'held')
            ran = []
            with atomic_io.locked_update(self.data, timeout=0.3) as held:
                ran.append(True)
            self.assertEqual(ran, [True])   # never wedged
            self.assertFalse(held)          # ran unserialized
        finally:
            release_evt.set()
            holder.join(timeout=10)


def _counts(**overrides) -> dict:
    """Expected degrade_counts() dict: all three causes at 0, then overrides.
    Encodes the stable-shape contract (every cause key always present)."""
    base = {'no_flock': 0, 'timeout': 0, 'sidecar_oserror': 0}
    base.update(overrides)
    return base


class LockedUpdateDegradeTelemetryTest(unittest.TestCase):
    """Every fail-open degrade must be OBSERVABLE (per-cause counter + throttled
    WARN) and the telemetry must itself be fail-safe — it runs on the ledger RMW
    path whose "never raises" contract the degrade is upholding."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / 'state.json'
        atomic_io._reset_degrade_telemetry()
        self.addCleanup(atomic_io._reset_degrade_telemetry)

    def test_no_flock_degrade_is_counted_as_no_flock(self):
        orig = file_lock._HAVE_FLOCK
        file_lock._HAVE_FLOCK = False
        try:
            with atomic_io.locked_update(self.data) as held:
                self.assertFalse(held)
        finally:
            file_lock._HAVE_FLOCK = orig
        self.assertEqual(atomic_io.degrade_counts(), _counts(no_flock=1))

    def test_sidecar_oserror_degrade_is_counted_distinctly(self):
        orig = file_lock.exclusive_lock

        def _boom(*a, **k):
            raise OSError('sidecar unwritable')

        file_lock.exclusive_lock = _boom
        try:
            with atomic_io.locked_update(self.data) as held:
                self.assertFalse(held)
        finally:
            file_lock.exclusive_lock = orig
        # A transient sidecar write failure is NOT a timeout — it must land in
        # its own bucket even though LockTimeout also subclasses OSError.
        self.assertEqual(atomic_io.degrade_counts(), _counts(sidecar_oserror=1))

    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_timeout_degrade_is_counted_as_timeout(self):
        ctx = multiprocessing.get_context()
        ready_q = ctx.Queue()
        release_evt = ctx.Event()
        holder = ctx.Process(
            target=_hold_sidecar_worker,
            args=(str(self.data), ready_q, release_evt))
        holder.start()
        try:
            self.assertEqual(ready_q.get(timeout=10), 'held')
            with atomic_io.locked_update(self.data, timeout=0.3) as held:
                self.assertFalse(held)
        finally:
            release_evt.set()
            holder.join(timeout=10)
        self.assertEqual(atomic_io.degrade_counts(), _counts(timeout=1))

    @unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
    def test_successful_lock_records_no_degrade(self):
        with atomic_io.locked_update(self.data) as held:
            self.assertTrue(held)
        self.assertEqual(atomic_io.degrade_counts(), _counts())  # stable shape, all 0

    def test_warn_is_rate_limited_but_every_degrade_is_counted(self):
        """A looping daemon degrading on every call must emit ONE WARN per cause
        per interval, yet the counter must tally EVERY degrade (the storm signal
        lives in the count + the per-process fan-out, not in log volume)."""
        orig = file_lock._HAVE_FLOCK
        file_lock._HAVE_FLOCK = False
        try:
            with self.assertLogs(atomic_io._DEGRADE_LOG.name, level='WARNING') as cm:
                for _ in range(5):
                    with atomic_io.locked_update(self.data):
                        pass
        finally:
            file_lock._HAVE_FLOCK = orig
        self.assertEqual(len(cm.records), 1)                       # throttled
        self.assertIn('cause=no_flock', cm.records[0].getMessage())
        self.assertEqual(atomic_io.degrade_counts(), _counts(no_flock=5))  # all tallied

    def test_telemetry_failure_never_escapes_into_the_rmw(self):
        """If the WARN emit itself raises (handler at ENOSPC, logging misconfig),
        _note_degrade must swallow it — the RMW still runs and never raises."""
        def _explode(*a, **k):
            raise RuntimeError('logging is down')

        orig_warn = atomic_io._DEGRADE_LOG.warning
        orig_flock = file_lock._HAVE_FLOCK
        atomic_io._DEGRADE_LOG.warning = _explode
        file_lock._HAVE_FLOCK = False
        try:
            ran = []
            with atomic_io.locked_update(self.data) as held:
                ran.append(True)
            self.assertEqual(ran, [True])   # body ran despite telemetry blowup
            self.assertFalse(held)
        finally:
            atomic_io._DEGRADE_LOG.warning = orig_warn
            file_lock._HAVE_FLOCK = orig_flock
        # The count is still recorded — the tally happens before the WARN emit.
        self.assertEqual(atomic_io.degrade_counts(), _counts(no_flock=1))


if __name__ == '__main__':
    unittest.main()
