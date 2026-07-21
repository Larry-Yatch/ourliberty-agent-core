"""Tests for the restart cordon (inbox_watcher) + the shared
``agent_work_in_flight`` predicate.

Spec: docs/restart-inflight-guard-spec.md (PR #981, revised #989).

The cordon's FAIL-OPEN contract is the highest-consequence surface in this
change: a cordon left behind by a dead restarter would silently stall ALL
dispatch — worse than the killed reviews it prevents. Every ambiguous state
(missing / malformed / expired / dead holder) must read as NOT cordoned.

Coverage:
- cordon honored: live + unexpired blocks new work; tasks stay in the inbox
- fail-open: absent, malformed, non-dict, expired, unparseable expiry, dead pid,
  wrong boot_id, pid<=0, missing fields → NOT cordoned
- shared predicate: live/dead in-flight marker pid; held/expired/dead-holder
  lease; SLOT-AWARE (inbox:mirror:1 counts — the #971 slot); marker-absent +
  lease-held (the Mirror clobber shape); unreadable state skipped
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import agent_work_in_flight as awif  # noqa: E402
import dispatch_lease  # noqa: E402
import inbox_watcher as iw  # noqa: E402

_DEAD_PID = 2 ** 22  # far above /proc/sys/kernel/pid_max on any sane box


_FAKE_BOOT = "11111111-2222-3333-4444-555555555555"


class CordonFixture(unittest.TestCase):
    """Per-test sandbox; OURLIBERTY_AGENTS_ROOT redirect is what both modules
    resolve through at CALL time."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ol-cordon-"))
        self._saved_env = os.environ.get("OURLIBERTY_AGENTS_ROOT")
        os.environ["OURLIBERTY_AGENTS_ROOT"] = str(self.root)
        (self.root / "state" / "restart-cordon").mkdir(parents=True, exist_ok=True)
        (self.root / "state" / "in-flight").mkdir(parents=True, exist_ok=True)
        (self.root / "state" / "dispatch-leases").mkdir(parents=True, exist_ok=True)
        # The cordon is boot-scoped and /proc is Linux-only; pin the current boot
        # id so these tests exercise the real comparison on any dev box.
        self._boot_patch = mock.patch.object(iw, "_current_boot_id",
                                             return_value=_FAKE_BOOT)
        self._boot_patch.start()
        self.addCleanup(self._boot_patch.stop)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("OURLIBERTY_AGENTS_ROOT", None)
        else:
            os.environ["OURLIBERTY_AGENTS_ROOT"] = self._saved_env

    def _write_cordon(self, **over):
        body = {
            "unit": iw.CORDON_UNIT,
            "reason": "stale-lib-restart",
            "created_at": time.time(),
            "expires_at": time.time() + 600,
            "pid": os.getpid(),
            "boot_id": _FAKE_BOOT,
        }
        body.update(over)
        p = iw._restart_cordon_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body))
        return p

    def _write_marker(self, stem="t1", pid=None):
        p = self.root / "state" / "in-flight" / f"{stem}.json"
        p.write_text(json.dumps({"pid": os.getpid() if pid is None else pid}))
        return p

    def _write_lease(self, identity, *, pid=None, age=0.0):
        p = self.root / "state" / "dispatch-leases" / f"{identity}.lease"
        p.write_text(json.dumps({
            "identity": identity,
            "holder_pid": os.getpid() if pid is None else pid,
            "timestamp_renewed": time.time() - age,
        }))
        return p


class CordonHonoredTests(CordonFixture):
    def test_live_cordon_is_active(self):
        self._write_cordon()
        self.assertEqual(iw._restart_cordon_active(), "stale-lib-restart")

    def test_cordon_without_reason_gets_default(self):
        self._write_cordon(reason=None)
        self.assertEqual(iw._restart_cordon_active(), "restart-pending")


class CordonFailOpenTests(CordonFixture):
    """Every one of these must read NOT cordoned. A false positive here stalls
    the entire dispatch queue with no alarm."""

    def test_absent_file(self):
        self.assertIsNone(iw._restart_cordon_active())

    def test_malformed_json(self):
        p = iw._restart_cordon_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        self.assertIsNone(iw._restart_cordon_active())

    def test_empty_file(self):
        p = iw._restart_cordon_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        self.assertIsNone(iw._restart_cordon_active())

    def test_non_dict_payload(self):
        p = iw._restart_cordon_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(["cordon"]))
        self.assertIsNone(iw._restart_cordon_active())

    def test_expired(self):
        self._write_cordon(expires_at=time.time() - 1)
        self.assertIsNone(iw._restart_cordon_active())

    def test_missing_expiry(self):
        self._write_cordon(expires_at=None)
        self.assertIsNone(iw._restart_cordon_active())

    def test_unparseable_expiry(self):
        self._write_cordon(expires_at="soon")
        self.assertIsNone(iw._restart_cordon_active())

    def test_dead_holder_pid(self):
        self._write_cordon(pid=_DEAD_PID)
        self.assertIsNone(iw._restart_cordon_active())

    def test_nonpositive_pid(self):
        # pid<=0 targets a process GROUP / every process — never a valid holder.
        self._write_cordon(pid=0)
        self.assertIsNone(iw._restart_cordon_active())
        self._write_cordon(pid=-1)
        self.assertIsNone(iw._restart_cordon_active())

    def test_missing_pid(self):
        self._write_cordon(pid=None)
        self.assertIsNone(iw._restart_cordon_active())

    def test_wrong_boot_id(self):
        # pids are reused across reboots; a pre-reboot cordon must not be kept
        # alive by an unrelated same-numbered pid afterwards.
        self._write_cordon(boot_id="00000000-0000-0000-0000-000000000000")
        self.assertIsNone(iw._restart_cordon_active())

    def test_missing_boot_id(self):
        self._write_cordon(boot_id=None)
        self.assertIsNone(iw._restart_cordon_active())

    def test_boot_id_unreadable_is_not_cordoned(self):
        # UNVERIFIABLE == NOT ALIVE: if /proc cannot be read we cannot prove the
        # cordon belongs to this boot, and trusting it risks a permanent stall.
        self._write_cordon()
        with mock.patch.object(iw, "_current_boot_id", return_value=None):
            self.assertIsNone(iw._restart_cordon_active())


class SharedPredicateTests(CordonFixture):
    def test_no_state_is_not_in_flight(self):
        self.assertFalse(awif.agent_work_in_flight())

    def test_live_marker_counts(self):
        self._write_marker()
        self.assertTrue(awif.any_live_in_flight_marker())
        self.assertTrue(awif.agent_work_in_flight())

    def test_dead_marker_pid_does_not_count(self):
        self._write_marker(pid=_DEAD_PID)
        self.assertFalse(awif.any_live_in_flight_marker())

    def test_unreadable_marker_skipped(self):
        (self.root / "state" / "in-flight" / "bad.json").write_text("{oops")
        self.assertFalse(awif.any_live_in_flight_marker())

    def test_held_slot0_lease_counts(self):
        self._write_lease("inbox:mirror")
        self.assertTrue(awif.any_live_inbox_lease())

    def test_held_slot1_lease_counts(self):
        # THE #971 SHAPE: the killed review ran in mirror slot 1, whose lease is
        # `inbox:mirror:1`. watchdog's original predicate opened only the slot-0
        # spelling and was blind to it.
        self._write_lease("inbox:mirror:1")
        self.assertTrue(awif.any_live_inbox_lease())
        self.assertTrue(awif.agent_work_in_flight())

    def test_expired_lease_does_not_count(self):
        self._write_lease("inbox:mirror", age=dispatch_lease.TTL_SECONDS + 5)
        self.assertFalse(awif.any_live_inbox_lease())

    def test_dead_lease_holder_does_not_count(self):
        self._write_lease("inbox:mirror", pid=_DEAD_PID)
        self.assertFalse(awif.any_live_inbox_lease())

    def test_marker_clobbered_but_lease_held(self):
        # The Mirror-review clobber shape: the build's _unregister_in_flight
        # deleted the review's marker mid-review. The lease is the survivor, and
        # is why both signals are needed.
        self._write_lease("inbox:mirror:1")
        self.assertFalse(awif.any_live_in_flight_marker())
        self.assertTrue(awif.agent_work_in_flight())

    def test_unrelated_lease_does_not_count(self):
        # review-head:* leases are per-PR-head, not "a watcher thread is busy".
        self._write_lease("review-head:mirror:abc123")
        self.assertFalse(awif.any_live_inbox_lease())

    def test_unreadable_lease_skipped(self):
        (self.root / "state" / "dispatch-leases" / "inbox:mirror.lease").write_text("{")
        self.assertFalse(awif.any_live_inbox_lease())


class CordonBlocksDispatchTests(CordonFixture):
    """The cordon must stop work BEFORE the lease acquire and BEFORE the claim,
    so a cordoned slot never touches a task file."""

    def test_cordoned_loop_takes_no_lease_and_no_claim(self):
        self._write_cordon()
        inbox = self.root / "inboxes" / "mirror"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "review-t1.json").write_text(json.dumps({"task_id": "t1"}))

        stop_after = {"n": 0}

        def fake_wait(_interval):
            stop_after["n"] += 1
            if stop_after["n"] >= 2:
                iw._shutdown.set()
            return True

        with mock.patch.object(iw, "scan_inbox",
                               return_value=[inbox / "review-t1.json"]), \
                mock.patch.object(iw._shutdown, "wait", side_effect=fake_wait), \
                mock.patch.object(iw.dispatch_lease, "try_acquire") as acq, \
                mock.patch.object(iw, "_claim_task") as claim, \
                mock.patch.object(iw, "process_task") as proc, \
                mock.patch.object(iw, "log"):
            try:
                iw.agent_loop("mirror", {}, slot=1, total_slots=2)
            finally:
                iw._shutdown.clear()

        acq.assert_not_called()
        claim.assert_not_called()
        proc.assert_not_called()
        # Drain contract: the task is still in the inbox, not archived/invalid.
        self.assertTrue((inbox / "review-t1.json").exists())


if __name__ == "__main__":
    unittest.main()
