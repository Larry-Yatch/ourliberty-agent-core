#!/usr/bin/env python3
"""Tests for dispatch_lease.is_held — the read-only "is this lease live?"
predicate added so peers (e.g. heal_abandoned_inbox_tasks, audit H1) can poll a
lease they do not own without acquiring/reclaiming it.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_dispatch_lease_is_held
"""
from __future__ import annotations

import os
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


class IsHeldTest(unittest.TestCase):
    IDENT = "inbox:forge"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._orig = {k: getattr(dl, k) for k in ("LEASES_DIR", "LEASE_LOG")}
        dl.LEASES_DIR = root / "leases"
        dl.LEASE_LOG = root / "lease-ops.log"
        self.addCleanup(lambda: [setattr(dl, k, v) for k, v in self._orig.items()])
        self._env = os.environ.get("GM_DEDUP_USE_LEASES")
        os.environ["GM_DEDUP_USE_LEASES"] = "enforce"
        self.addCleanup(self._restore_env)
        # Stable boot id so _pid_alive_same_boot's boot check is deterministic.
        self._boot = mock.patch.object(dl, "_boot_id", return_value="testboot")
        self._boot.start()
        self.addCleanup(self._boot.stop)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("GM_DEDUP_USE_LEASES", None)
        else:
            os.environ["GM_DEDUP_USE_LEASES"] = self._env

    def _write_lease(self, *, holder_pid, renewed_offset=0.0, boot_id="testboot"):
        dl.LEASES_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        dl._atomic_write(dl._lease_path(self.IDENT), {
            "identity": self.IDENT,
            "holder_pid": holder_pid,
            "boot_id": boot_id,
            "nonce": "n",
            "timestamp_created": now,
            "timestamp_renewed": now + renewed_offset,
        })

    def test_live_holder_within_ttl_is_held(self):
        self._write_lease(holder_pid=os.getpid())  # our own pid is alive
        self.assertTrue(dl.is_held(self.IDENT))

    def test_no_lease_file_is_not_held(self):
        self.assertFalse(dl.is_held(self.IDENT))

    def test_stale_past_ttl_is_not_held(self):
        # Aged past TTL even though pid is alive — the abandoned/dead-watcher case.
        self._write_lease(holder_pid=os.getpid(),
                          renewed_offset=-(dl.TTL_SECONDS + 5))
        self.assertFalse(dl.is_held(self.IDENT))

    def test_dead_holder_is_not_held(self):
        self._write_lease(holder_pid=2147483640)  # not a live pid
        self.assertFalse(dl.is_held(self.IDENT))

    def test_different_boot_is_not_held(self):
        self._write_lease(holder_pid=os.getpid(), boot_id="some-other-boot")
        self.assertFalse(dl.is_held(self.IDENT))

    def test_off_mode_is_never_held(self):
        self._write_lease(holder_pid=os.getpid())
        os.environ["GM_DEDUP_USE_LEASES"] = "off"
        self.assertFalse(dl.is_held(self.IDENT))

    def test_read_only_does_not_create_leases_dir(self):
        # is_held must not mkdir the leases dir as a side effect (it runs on a
        # high-frequency healer read path; _lease_path() would have).
        self.assertFalse(dl.LEASES_DIR.exists())
        self.assertFalse(dl.is_held(self.IDENT))
        self.assertFalse(dl.LEASES_DIR.exists())

    def test_identity_with_slash_is_sanitized_consistently(self):
        # is_held must resolve the same on-disk path the acquirer wrote to.
        ident = "inbox:forge/sub"
        dl.LEASES_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        dl._atomic_write(dl._lease_path(ident), {
            "holder_pid": os.getpid(), "boot_id": "testboot",
            "timestamp_renewed": now,
        })
        self.assertTrue(dl.is_held(ident))


if __name__ == "__main__":
    unittest.main()
