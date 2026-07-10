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


class DeferredClaimUnclaimTest(_InboxTmpBase):
    """A claimed task hitting a transient-defer path is returned to the inbox,
    not stranded under .claimed/<slot>/ until restart (Mirror rev-1 finding)."""

    def _run_one_pass(self, agent: str, slot: int, total_slots: int,
                      process_side_effect) -> None:
        inbox_watcher._shutdown.clear()
        patches = [
            mock.patch.object(inbox_watcher, "process_task",
                              side_effect=process_side_effect),
            mock.patch.object(inbox_watcher.dispatch_lease, "try_acquire",
                              return_value={"acquired": True, "nonce": None}),
            mock.patch.object(inbox_watcher.dispatch_lease, "release"),
        ]
        for p in patches:
            p.start()
        try:
            inbox_watcher.agent_loop(agent, {}, slot=slot, total_slots=total_slots)
        finally:
            for p in patches:
                p.stop()
            inbox_watcher._shutdown.clear()

    def test_deferred_task_returned_to_inbox(self):
        self._write_task("mirror", "defer-1")

        def deferring_process(agent, task_file, cfg, slot=0):
            # Simulate rotation-gate / TIER_HOLD: leave the file in place and
            # stop the loop after this one task.
            inbox_watcher._shutdown.set()

        self._run_one_pass("mirror", slot=1, total_slots=2, process_side_effect=deferring_process)

        # Back in the live inbox where the next poll can re-see it.
        self.assertTrue((self.inboxes / "mirror" / "defer-1.json").exists())
        # Not stranded under .claimed/<slot>/.
        claimed = self.inboxes / "mirror" / ".claimed" / "1"
        self.assertEqual(
            list(claimed.glob("*.json")) if claimed.exists() else [], []
        )

    def test_terminal_task_not_returned_to_inbox(self):
        # When process_task takes a terminal path (archives the file), the claim
        # is already gone and must NOT be resurrected into the inbox.
        self._write_task("mirror", "done-1")

        def archiving_process(agent, task_file, cfg, slot=0):
            inbox_watcher.move_to(task_file, self.inboxes / agent / ".archive")
            inbox_watcher._shutdown.set()

        self._run_one_pass("mirror", slot=1, total_slots=2, process_side_effect=archiving_process)

        self.assertFalse((self.inboxes / "mirror" / "done-1.json").exists())
        archived = list((self.inboxes / "mirror" / ".archive").glob("done-1*.json"))
        self.assertEqual(len(archived), 1)


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

    def test_two_same_head_paid_orphans_get_distinct_markers(self):
        # Two slots' paid orphans for the SAME PR head (distinct task files)
        # must each land as their OWN lost-result marker — a shared filename
        # would clobber one verdict-loss record with the other (§4 PR2 / §5
        # lost-verdict class: no shared marker filename across slots).
        head = "deadbeefcafe0001"
        for i in (0, 1):
            claimed = self._claim_with_age(
                "mirror", f"paid-same-head-{i}", slot=i,
                age_sec=inbox_watcher.CLAIM_ORPHAN_CEILING_SEC + 60,
                task={"task_id": f"paid-same-head-{i}",
                      "head_sha": head, "prompt": "x"},
            )
            (self.in_flight / f"paid-same-head-{i}.json").write_text(json.dumps({
                "task_stem": f"paid-same-head-{i}", "agent_id": "mirror",
                "pid": os.getpid(),
            }))
            self.assertFalse((self.inboxes / "mirror" / f"paid-same-head-{i}.json").exists())
            del claimed
        inbox_watcher.sweep_claimed_orphans()
        lost = (self.inboxes / "mirror" / ".archive"
                / inbox_watcher.safe_write_inbox.LOST_RESULT_SUBDIR)
        markers = sorted(p.name for p in lost.glob("paid-same-head-*.json"))
        # Both preserved as distinct markers; neither clobbered the other.
        self.assertEqual(len(markers), 2, markers)

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

    def test_concluded_orphan_archived_not_requeued(self):
        # A stranded claim whose review ALREADY concluded (a consumed verdict
        # outbox is archived) must be ARCHIVED, never re-queued — re-queuing a
        # concluded review pays for an Opus re-review of an already-reviewed PR
        # (the 2026-07-10 PR #854 class).
        claimed = self._claim_with_age(
            "mirror", "concluded-1", slot=1,
            age_sec=inbox_watcher.CLAIM_ORPHAN_CEILING_SEC + 60,
            task={"task_id": "concluded-1", "prompt": "x"},
        )
        outbox_archive = self.outboxes / "mirror" / ".archive"
        outbox_archive.mkdir(parents=True, exist_ok=True)
        (outbox_archive / "concluded-1.json").write_text("{}")  # delivered verdict
        n = inbox_watcher.sweep_claimed_orphans()
        self.assertEqual(n, 0)  # NOT re-queued
        self.assertFalse(claimed.exists())
        self.assertFalse((self.inboxes / "mirror" / "concluded-1.json").exists())
        # Archived to the plain .archive/ (NOT the lost-result marker — the
        # verdict was delivered, not lost).
        archive = self.inboxes / "mirror" / ".archive"
        self.assertTrue((archive / "concluded-1.json").exists())
        lost = archive / inbox_watcher.safe_write_inbox.LOST_RESULT_SUBDIR
        self.assertFalse(any(lost.glob("concluded-1*.json")) if lost.exists() else False)

    def test_concluded_beats_stale_inflight_entry(self):
        # A concluded review stranded by a watcher death often ALSO leaves a
        # stale in-flight entry from the prior boot (sweep runs before
        # reap_orphans clears it). The concluded-check must win over the
        # in-flight paid-orphan branch, else the review is mislabeled
        # `lost-result` and re-dispatched for a paid re-review (#854).
        claimed = self._claim_with_age(
            "mirror", "concluded-stale-1", slot=0,
            age_sec=inbox_watcher.CLAIM_ORPHAN_CEILING_SEC + 60,
            task={"task_id": "concluded-stale-1", "prompt": "x"},
        )
        outbox_archive = self.outboxes / "mirror" / ".archive"
        outbox_archive.mkdir(parents=True, exist_ok=True)
        (outbox_archive / "concluded-stale-1.json").write_text("{}")  # verdict delivered
        (self.in_flight / "concluded-stale-1.json").write_text(json.dumps({
            "task_stem": "concluded-stale-1", "agent_id": "mirror", "pid": os.getpid(),
        }))  # stale in-flight entry lingers
        n = inbox_watcher.sweep_claimed_orphans()
        self.assertEqual(n, 0)
        self.assertFalse(claimed.exists())
        # Clean .archive/ (NOT the lost-result marker → no re-dispatch).
        archive = self.inboxes / "mirror" / ".archive"
        self.assertTrue((archive / "concluded-stale-1.json").exists())
        lost = archive / inbox_watcher.safe_write_inbox.LOST_RESULT_SUBDIR
        self.assertFalse(any(lost.glob("concluded-stale-1*.json")) if lost.exists() else False)

    def test_same_name_inbox_archive_marks_concluded(self):
        # The re-dispatch case: a review request with the SAME filename already
        # sits in .archive/ → the claim is a duplicate of a concluded review.
        (self.inboxes / "mirror" / ".archive").mkdir(parents=True, exist_ok=True)
        self.assertTrue(inbox_watcher._review_already_concluded(
            "mirror", "concluded-1", "review-concluded-1.json"
        ) is False)  # no signal yet
        (self.inboxes / "mirror" / ".archive" / "review-concluded-1.json").write_text("{}")
        self.assertTrue(inbox_watcher._review_already_concluded(
            "mirror", "concluded-1", "review-concluded-1.json"
        ))


class SameHeadGuardTest(_InboxTmpBase):
    """No two review slots review one PR head CONCURRENTLY (§4 PR2). The atomic
    file-claim only stops two slots grabbing the SAME file; two DISTINCT
    review-requests for one head would still land on two slots. The head-lease
    (`review-head:<agent>:<sha>`) is the race-free arbiter: a slot that cannot
    acquire it defers its claim (returns the task to the inbox) instead of
    running a duplicate concurrent review of the identical diff."""

    def _run_one_pass(self, *, head_acquires, task):
        inbox_watcher._shutdown.clear()
        self._write_task("mirror", "rev-1", task)
        processed: list = []
        seen_identities: list[str] = []

        def process(agent, task_file, cfg, slot=0):
            processed.append(task_file)
            inbox_watcher._shutdown.set()

        def acquire(identity, *a, **k):
            seen_identities.append(identity)
            if identity.startswith("review-head:"):
                if not head_acquires:
                    # Simulate a sibling slot already holding this head; also
                    # release the loop so the single pass terminates.
                    inbox_watcher._shutdown.set()
                    return {"acquired": False, "nonce": None}
                return {"acquired": True, "nonce": "hn"}
            return {"acquired": True, "nonce": None}

        patches = [
            mock.patch.object(inbox_watcher, "process_task", side_effect=process),
            mock.patch.object(inbox_watcher.dispatch_lease, "try_acquire",
                              side_effect=acquire),
            mock.patch.object(inbox_watcher.dispatch_lease, "release"),
            mock.patch.object(inbox_watcher.dispatch_lease, "Heartbeat",
                              return_value=mock.MagicMock()),
        ]
        for p in patches:
            p.start()
        try:
            inbox_watcher.agent_loop("mirror", {}, slot=1, total_slots=2)
        finally:
            for p in patches:
                p.stop()
            inbox_watcher._shutdown.clear()
        return processed, seen_identities

    def test_defers_when_head_lease_unavailable(self):
        processed, ids = self._run_one_pass(
            head_acquires=False,
            task={"task_id": "rev-1", "head_sha": "abc123def456", "prompt": "x"},
        )
        # The duplicate concurrent review is NOT run.
        self.assertEqual(processed, [])
        # The head-lease was consulted with the namespaced identity.
        self.assertIn("review-head:mirror:abc123def456", ids)
        # Deferred (returned to the live inbox), not stranded or invalidated —
        # it runs as a benign serial re-review once the winner frees the head.
        self.assertTrue((self.inboxes / "mirror" / "rev-1.json").exists())
        claimed = self.inboxes / "mirror" / ".claimed" / "1"
        self.assertEqual(
            list(claimed.glob("*.json")) if claimed.exists() else [], []
        )

    def test_processes_when_head_lease_free(self):
        processed, ids = self._run_one_pass(
            head_acquires=True,
            task={"task_id": "rev-1", "head_sha": "abc123def456", "prompt": "x"},
        )
        self.assertEqual(len(processed), 1)  # the review ran
        self.assertIn("review-head:mirror:abc123def456", ids)

    def test_no_head_sha_skips_guard(self):
        # A task without head_sha (non-review / pre-head-recording envelope)
        # never touches the head-lease — the guard is review-specific.
        processed, ids = self._run_one_pass(
            head_acquires=True,
            task={"task_id": "rev-1", "prompt": "x"},
        )
        self.assertEqual(len(processed), 1)
        self.assertFalse(any(i.startswith("review-head:") for i in ids))


class TaskHeadShaTest(unittest.TestCase):
    """`_task_head_sha` reads top-level first, then the chain-envelope
    `context` nesting, and fails safe to None (no guard) on anything else."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="head-sha-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _f(self, body) -> Path:
        p = self.tmp / "t.json"
        p.write_text(body if isinstance(body, str) else json.dumps(body))
        return p

    def test_top_level(self):
        self.assertEqual(
            inbox_watcher._task_head_sha(self._f({"head_sha": "aa11"})), "aa11")

    def test_nested_context(self):
        self.assertEqual(
            inbox_watcher._task_head_sha(self._f({"context": {"head_sha": "bb22"}})),
            "bb22")

    def test_absent_and_malformed_are_none(self):
        self.assertIsNone(inbox_watcher._task_head_sha(self._f({"task_id": "x"})))
        self.assertIsNone(inbox_watcher._task_head_sha(self._f("not json{")))
        self.assertIsNone(inbox_watcher._task_head_sha(self._f({"head_sha": ""})))
        self.assertIsNone(inbox_watcher._task_head_sha(self.tmp / "missing.json"))


class HeadLeaseIdentityTest(unittest.TestCase):
    """The head-lease lives in its OWN namespace, distinct from the per-slot
    inbox leases, so head arbitration never collides with slot arbitration."""

    def test_namespaced_and_distinct_from_slot_leases(self):
        hid = inbox_watcher._head_lease_identity("mirror", "cafe1234")
        self.assertEqual(hid, "review-head:mirror:cafe1234")
        self.assertNotEqual(hid, inbox_watcher._slot_identity("mirror", 0))
        self.assertNotEqual(hid, inbox_watcher._slot_identity("mirror", 1))
        # A distinct dispatch-lease file, so holding a head never blocks a slot.
        self.assertNotEqual(
            dispatch_lease._lease_path(hid),
            dispatch_lease._lease_path(inbox_watcher._slot_identity("mirror", 1)),
        )


class EmitReviewQueueWaitTest(unittest.TestCase):
    """PR3 observability: review-start queue-wait is recorded to the local
    ledger AND emitted as a review_queue_wait chain_event, best-effort, and a
    missing PR-open time yields NO sample (never fabricated)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qw-emit-test-"))
        self.ledger = self.tmp / "blackboard" / "mirror-queue-wait.jsonl"
        self._patches = [
            mock.patch.object(inbox_watcher, "BLACKBOARD", self.tmp / "blackboard"),
            mock.patch.object(inbox_watcher, "MIRROR_QUEUE_WAIT_LEDGER", self.ledger),
            mock.patch.object(inbox_watcher, "LOG_FILE", self.tmp / "watcher.log"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sample_written_and_event_emitted(self):
        import datetime as _dt
        # PR opened 30 min before review-start → queue_wait ~1800s.
        created = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1800)
        captured = {}

        def _fake_emit(**kwargs):
            captured.update(kwargs)
            return True

        fake_cee = mock.MagicMock()
        fake_cee.emit_event.side_effect = _fake_emit
        with mock.patch.object(inbox_watcher, "_pr_created_at", return_value=created), \
                mock.patch.dict(sys.modules, {"chain_event_emit": fake_cee}):
            inbox_watcher.emit_review_queue_wait("task-abc", "https://x/pull/9", 1)

        rows = [json.loads(l) for l in self.ledger.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_id"], "task-abc")
        self.assertEqual(row["pr_url"], "https://x/pull/9")
        self.assertEqual(row["review_slot"], 1)
        self.assertGreaterEqual(row["queue_wait_sec"], 1700)
        self.assertLessEqual(row["queue_wait_sec"], 1900)
        # Chain event carries the canonical type + slot.
        self.assertEqual(captured.get("event_type"), "review_queue_wait")
        self.assertEqual(captured.get("agent"), "mirror")
        self.assertEqual(captured.get("payload", {}).get("review_slot"), 1)

    def test_no_pr_open_time_emits_nothing(self):
        fake_cee = mock.MagicMock()
        with mock.patch.object(inbox_watcher, "_pr_created_at", return_value=None), \
                mock.patch.dict(sys.modules, {"chain_event_emit": fake_cee}):
            inbox_watcher.emit_review_queue_wait("task-abc", "https://x/pull/9", 0)
        self.assertFalse(self.ledger.exists())
        fake_cee.emit_event.assert_not_called()

    def test_missing_pr_url_short_circuits(self):
        # No pr_url → _pr_created_at never consulted, no ledger, no event.
        fake_cee = mock.MagicMock()
        with mock.patch.object(inbox_watcher, "_pr_created_at") as pca, \
                mock.patch.dict(sys.modules, {"chain_event_emit": fake_cee}):
            inbox_watcher.emit_review_queue_wait("task-abc", None, 0)
            pca.assert_not_called()
        self.assertFalse(self.ledger.exists())
        fake_cee.emit_event.assert_not_called()


class ReviewQueueWaitEventTypeRegisteredTest(unittest.TestCase):
    """emit_event drops any type absent from KNOWN_EVENT_TYPES, so PR3's new
    type MUST be registered or every queue-wait row silently vanishes."""

    def test_registered(self):
        import chain_event_shipper
        self.assertIn("review_queue_wait", chain_event_shipper.KNOWN_EVENT_TYPES)


class ActivationConfigTest(unittest.TestCase):
    """PR3 activation guard: the shipped config sets mirror review_slots=2. A
    rollback/typo that drops it back to 1 must fail this test loudly."""

    def test_mirror_review_slots_is_two(self):
        # Read the REPO-local config (this checkout), not the deployed
        # ~/agent-core copy MODELS_FILE points at — the activation lives here.
        cfg_path = _REPO_SCRIPTS.parent / "config" / "agent-models.json"
        cfg = json.loads(cfg_path.read_text())
        self.assertEqual(
            cfg["agents"]["mirror"].get("review_slots"), 2,
            "mirror review_slots must be 2 (mirror-two-slot-review §4 PR3 activation)",
        )


if __name__ == "__main__":
    unittest.main()
