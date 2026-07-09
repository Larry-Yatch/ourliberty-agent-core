#!/usr/bin/env python3
"""Regression gate for the mirror-two-slot-review PR1 slot plumbing.

Spec: docs/specs/mirror-two-slot-review-spec.md §3.1–3.3, §4 PR1. PR1 lands
inert (review_slots absent => 1 thread), so these tests exercise the new
primitives directly:

  1. Slot-indexed lease identities — slot 0 keeps the legacy ``inbox:<agent>``
     spelling (healers grep for it); higher slots get an ``:<n>`` suffix.
  2. Atomic rename-based claim — two slots scanning the same inbox NEVER both
     claim the same task file (the loser's os.rename raises → None).
  3. Orphan-claim sweep — a task stranded in ``.claimed/<slot>/`` past the
     session ceiling is re-queued exactly once; a paid run (in-flight registry
     entry present) is archived, NOT re-dispatched; a fresh claim is left alone.
  4. review_slots config read — absent/malformed => 1 (inert).

Run:
    cd /home/larry/agent-core && \
        python3 -m unittest scripts.tests.test_inbox_watcher_slot_claim
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_lease  # noqa: E402
import inbox_watcher  # noqa: E402


class SlotIdentityTest(unittest.TestCase):
    """Slot 0 keeps the legacy lease spelling; higher slots get :<n> (§3.1)."""

    def test_slot0_is_legacy_spelling(self):
        for agent in inbox_watcher.AGENTS:
            self.assertEqual(
                inbox_watcher._slot_identity(agent, 0), f"inbox:{agent}"
            )
        # The exact string every healer/tool greps for during rollout.
        self.assertEqual(inbox_watcher._slot_identity("mirror", 0), "inbox:mirror")

    def test_higher_slots_are_suffixed(self):
        self.assertEqual(inbox_watcher._slot_identity("mirror", 1), "inbox:mirror:1")
        self.assertEqual(inbox_watcher._slot_identity("mirror", 2), "inbox:mirror:2")

    def test_lease_paths_distinct_and_slot0_unchanged(self):
        # dispatch_lease keys off the identity string; the colon survives
        # sanitization so each slot gets its own lease file, and slot 0 resolves
        # to the SAME path the pre-slot code used for "inbox:mirror".
        p0 = dispatch_lease._lease_path("inbox:mirror")
        p0_slot = dispatch_lease._lease_path(inbox_watcher._slot_identity("mirror", 0))
        p1 = dispatch_lease._lease_path(inbox_watcher._slot_identity("mirror", 1))
        self.assertEqual(p0, p0_slot)
        self.assertNotEqual(p0, p1)


class ReviewSlotsConfigTest(unittest.TestCase):
    """review_slots read generically; absent/malformed => 1 (§3.3)."""

    def test_absent_defaults_to_one(self):
        cfg = {"agents": {"mirror": {"name": "mirror"}}}
        self.assertEqual(inbox_watcher._review_slots_for("mirror", cfg), 1)
        self.assertEqual(inbox_watcher._review_slots_for("forge", cfg), 1)
        self.assertEqual(inbox_watcher._review_slots_for("mirror", {}), 1)

    def test_explicit_value_honored(self):
        cfg = {"agents": {"mirror": {"review_slots": 2}}}
        self.assertEqual(inbox_watcher._review_slots_for("mirror", cfg), 2)
        # String coercion (JSON authored as a string) still works.
        cfg2 = {"agents": {"mirror": {"review_slots": "3"}}}
        self.assertEqual(inbox_watcher._review_slots_for("mirror", cfg2), 3)

    def test_malformed_and_nonpositive_floor_to_one(self):
        for bad in ("two", None, 0, -4, [], {}):
            cfg = {"agents": {"mirror": {"review_slots": bad}}}
            self.assertEqual(
                inbox_watcher._review_slots_for("mirror", cfg), 1, repr(bad)
            )


class AgentForTaskFileTest(unittest.TestCase):
    """write_invalid must resolve the owning agent even from a claimed path."""

    def test_unclaimed_path(self):
        p = Path("/x/inboxes/mirror/task-1.json")
        self.assertEqual(inbox_watcher._agent_for_task_file(p), "mirror")

    def test_claimed_path(self):
        p = Path("/x/inboxes/mirror/.claimed/1/task-1.json")
        self.assertEqual(inbox_watcher._agent_for_task_file(p), "mirror")


class _InboxTmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="slot-claim-test-"))
        self.inboxes = self.tmp / "inboxes"
        self.outboxes = self.tmp / "outboxes"
        self.in_flight = self.tmp / "state" / "in-flight"
        for agent in inbox_watcher.AGENTS:
            (self.inboxes / agent).mkdir(parents=True, exist_ok=True)
        self.in_flight.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(inbox_watcher, "INBOXES_ROOT", self.inboxes),
            mock.patch.object(inbox_watcher, "OUTBOXES_ROOT", self.outboxes),
            mock.patch.object(inbox_watcher, "IN_FLIGHT_DIR", self.in_flight),
            mock.patch.object(inbox_watcher, "LOG_FILE", self.tmp / "watcher.log"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_task(self, agent: str, name: str, task: dict | None = None) -> Path:
        f = self.inboxes / agent / f"{name}.json"
        f.write_text(json.dumps(task or {"task_id": name, "prompt": "x"}))
        return f


class ClaimAtomicityTest(_InboxTmpBase):
    """The rename-based claim is the single-owner arbiter (§3.2)."""

    def test_claim_moves_out_of_inbox(self):
        f = self._write_task("mirror", "task-1")
        claimed = inbox_watcher._claim_task("mirror", f, slot=0)
        self.assertIsNotNone(claimed)
        self.assertTrue(claimed.exists())
        self.assertEqual(claimed.parent, self.inboxes / "mirror" / ".claimed" / "0")
        self.assertFalse(f.exists())

    def test_scan_inbox_skips_claimed(self):
        f = self._write_task("mirror", "task-1")
        inbox_watcher._claim_task("mirror", f, slot=0)
        # A claimed task lives under a dotdir; it is no longer "queued".
        self.assertEqual(inbox_watcher.scan_inbox("mirror"), [])

    def test_two_slots_never_double_claim(self):
        # Head-to-head: for each task, two threads (slot 0 + slot 1) race to
        # claim the SAME file at a synchronized instant. Exactly one must win.
        n = 60
        files = [self._write_task("mirror", f"race-{i}") for i in range(n)]
        winners: dict[str, list[str]] = {}
        lock = threading.Lock()

        for f in files:
            barrier = threading.Barrier(2)
            results: dict[int, Path | None] = {}

            def claim(slot: int, tf: Path = f, b: threading.Barrier = barrier):
                b.wait()
                results[slot] = inbox_watcher._claim_task("mirror", tf, slot)

            t0 = threading.Thread(target=claim, args=(0,))
            t1 = threading.Thread(target=claim, args=(1,))
            t0.start(); t1.start()
            t0.join(); t1.join()

            got = [(slot, res) for slot, res in results.items() if res is not None]
            self.assertEqual(
                len(got), 1,
                f"{f.name}: expected exactly one winning claim, got {results}",
            )
            with lock:
                slot, path = got[0]
                winners.setdefault(f.name, []).append(str(path))
                # The winner parked it in ITS OWN slot dir.
                self.assertEqual(
                    path.parent,
                    self.inboxes / "mirror" / ".claimed" / str(slot),
                )

        # Every task claimed exactly once, none left in the live inbox.
        self.assertEqual(len(winners), n)
        self.assertEqual(inbox_watcher.scan_inbox("mirror"), [])


class SweepClaimedOrphansTest(_InboxTmpBase):
    """Startup sweep re-queues stranded claims once, never re-bills paid work."""

    def _claim_with_age(self, agent: str, name: str, slot: int,
                        age_sec: float, task: dict | None = None) -> Path:
        f = self._write_task(agent, name, task)
        claimed = inbox_watcher._claim_task(agent, f, slot)
        assert claimed is not None
        old = time.time() - age_sec
        os.utime(claimed, (old, old))
        return claimed

    def test_old_orphan_requeued_exactly_once(self):
        claimed = self._claim_with_age(
            "mirror", "orphan-1", slot=1,
            age_sec=inbox_watcher.CLAIM_ORPHAN_CEILING_SEC + 60,
        )
        n = inbox_watcher.sweep_claimed_orphans()
        self.assertEqual(n, 1)
        self.assertFalse(claimed.exists())  # gone from .claimed
        requeued = self.inboxes / "mirror" / "orphan-1.json"
        self.assertTrue(requeued.exists())  # back in the live inbox
        # Idempotent: a second sweep finds nothing to move.
        self.assertEqual(inbox_watcher.sweep_claimed_orphans(), 0)
        self.assertTrue(requeued.exists())

    def test_fresh_claim_not_swept(self):
        claimed = self._claim_with_age(
            "mirror", "fresh-1", slot=0, age_sec=5,
        )
        self.assertEqual(inbox_watcher.sweep_claimed_orphans(), 0)
        self.assertTrue(claimed.exists())  # left in place; may still be in flight

    def test_paid_orphan_archived_not_requeued(self):
        # A stranded claim WITH a live in-flight registry entry means the LLM
        # already spawned (paid). Never re-dispatch — archive under lost-result.
        claimed = self._claim_with_age(
            "mirror", "paid-1", slot=1,
            age_sec=inbox_watcher.CLAIM_ORPHAN_CEILING_SEC + 60,
            task={"task_id": "paid-1", "prompt": "x"},
        )
        (self.in_flight / "paid-1.json").write_text(json.dumps({
            "task_stem": "paid-1", "agent_id": "mirror", "pid": os.getpid(),
        }))
        n = inbox_watcher.sweep_claimed_orphans()
        self.assertEqual(n, 0)  # not re-queued
        self.assertFalse(claimed.exists())
        self.assertFalse((self.inboxes / "mirror" / "paid-1.json").exists())
        # Landed under the lost-result marker dir.
        lost = (self.inboxes / "mirror" / ".archive"
                / inbox_watcher.safe_write_inbox.LOST_RESULT_SUBDIR)
        self.assertTrue(any(lost.glob("paid-1*.json")))


if __name__ == "__main__":
    unittest.main()
