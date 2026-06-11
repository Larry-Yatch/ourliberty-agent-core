#!/usr/bin/env python3
"""Tests for the PID-reuse guard in dispatch_lease reclaim (2026-06-05 audit
#27): a stale lease whose holder PID has been recycled to an unrelated process
within the same boot must be reclaimed WITHOUT signalling that PID.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dispatch_lease_reclaim_identity
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_lease as dl  # noqa: E402


class ReclaimIdentityGuardTest(unittest.TestCase):
    IDENT = "forge:build:task-001"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._orig = {k: getattr(dl, k) for k in ("LEASES_DIR", "LEASE_LOG", "DIVERGENCE_LOG")}
        dl.LEASES_DIR = root / "leases"
        dl.LEASE_LOG = root / "lease-ops.log"
        dl.DIVERGENCE_LOG = root / "lease-div.log"
        self._env = os.environ.get("GM_DEDUP_USE_LEASES")
        os.environ["GM_DEDUP_USE_LEASES"] = "enforce"
        # Stable, matching boot id so _pid_alive_same_boot's boot check passes.
        self._boot = mock.patch.object(dl, "_boot_id", return_value="testboot")
        self._boot.start()
        self.addCleanup(self._boot.stop)
        # Don't actually sleep through the SIGTERM grace.
        self._sleep = mock.patch.object(dl.time, "sleep")
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(dl, k, v)
        if self._env is None:
            os.environ.pop("GM_DEDUP_USE_LEASES", None)
        else:
            os.environ["GM_DEDUP_USE_LEASES"] = self._env

    def _write_stale_lease(self, holder_pid=99999, holder_starttime=12345):
        path = dl._lease_path(self.IDENT)
        path.write_text(json.dumps({
            "identity": self.IDENT,
            "holder_pid": holder_pid,
            "holder_starttime": holder_starttime,
            "boot_id": "testboot",
            "nonce": "old-nonce",
            "timestamp_created": time.time() - 9999,
            "timestamp_renewed": time.time() - 9999,  # age >> TTL -> stale
        }))
        return path

    def test_reclaim_does_not_kill_a_recycled_pid(self):
        self._write_stale_lease()
        kills = []
        with mock.patch.object(dl.pid_identity, "still_same_process", return_value=False), \
             mock.patch.object(dl.os, "kill", side_effect=lambda p, s: kills.append((p, s))):
            res = dl.try_acquire(self.IDENT, holder_pid=os.getpid())
        # Lease was reclaimed (we now hold it) ...
        self.assertTrue(res["acquired"])
        # ... but the recycled PID was never SIGTERMed or SIGKILLed.
        sigs = [s for _, s in kills]
        self.assertNotIn(signal.SIGTERM, sigs)
        self.assertNotIn(signal.SIGKILL, sigs)

    def test_acquire_persists_holder_starttime(self):
        # The whole guard depends on this field being recorded at acquire time.
        res = dl.try_acquire("forge:fresh:task-xyz", holder_pid=os.getpid())
        self.assertTrue(res["acquired"])
        lease = json.loads(dl._lease_path("forge:fresh:task-xyz").read_text())
        self.assertIn("holder_starttime", lease)

    @unittest.skipUnless(os.path.exists("/proc/self/stat"), "requires Linux /proc")
    def test_reclaim_spares_recycled_pid_end_to_end(self):
        # Integration: a REAL live process whose recorded start time does NOT
        # match (simulating a recycled PID) must not be signalled — no mocking of
        # still_same_process, the real /proc read drives the decision.
        import subprocess
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        real_st = dl.pid_identity.proc_starttime(child.pid)
        self.assertIsNotNone(real_st)
        self._write_stale_lease(holder_pid=child.pid, holder_starttime=real_st + 100000)
        res = dl.try_acquire(self.IDENT, holder_pid=os.getpid())
        self.assertTrue(res["acquired"])
        self.assertIsNone(child.poll(), "innocent recycled-PID process was signalled")

    def test_reclaim_kills_a_genuinely_stale_holder(self):
        self._write_stale_lease()
        kills = []
        with mock.patch.object(dl.pid_identity, "still_same_process", return_value=True), \
             mock.patch.object(dl.os, "kill", side_effect=lambda p, s: kills.append((p, s))):
            res = dl.try_acquire(self.IDENT, holder_pid=os.getpid())
        self.assertTrue(res["acquired"])
        sigs = [s for _, s in kills]
        self.assertIn(signal.SIGTERM, sigs)
        self.assertIn(signal.SIGKILL, sigs)


if __name__ == "__main__":
    unittest.main()


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)

