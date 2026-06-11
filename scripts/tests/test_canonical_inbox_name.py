#!/usr/bin/env python3
"""Tests for PR-A.5 — canonical_inbox_name writer/reader convergence
(2026-06-05 audit follow-up to PR-A #353).

PR-A made the inbox/outbox WRITE sanitize the filename, but the idempotency
readers (outbox_notifier dedup checks, heal_pipeline_stall archive lookups) still
rebuilt the RAW name, so a task_id with a path-structural byte ('/', control)
made them diverge -> duplicate dispatch. PR-A.5 routes both through one shared
canonical name.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_canonical_inbox_name
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import fnmatch
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import routing_validator as rv      # noqa: E402
import safe_write_inbox as swi      # noqa: E402


class CanonicalInboxNameTest(unittest.TestCase):
    def test_legit_name_unchanged(self):
        for n in ("build-task-001.json", "review-medic-silence-cpu:high@web-01.json"):
            self.assertEqual(swi.canonical_inbox_name(n), n)

    def test_structural_byte_flattened(self):
        out = swi.canonical_inbox_name("build-a/b/c.json")
        self.assertNotIn("/", out)
        self.assertEqual(out, "build-a-b-c.json")

    def test_idempotent(self):
        # Includes the multibyte + oversized-extension cases that broke the
        # char-vs-byte truncation: the writer re-canonicalizes the reader's
        # already-canonical name, so non-idempotence => name divergence.
        for n in ("build-a/b.json", "x" * 300 + ".json", "../../etc/passwd.json",
                  "build-" + "é" * 120 + ".json", "short." + "e" * 300,
                  "build-" + "🛠" * 80 + ".json"):
            once = swi.canonical_inbox_name(n)
            self.assertEqual(swi.canonical_inbox_name(once), once, f"not idempotent: {n!r}")

    def test_truncated_names_fit_byte_cap(self):
        for n in ("build-" + "z" * 400 + ".json", "build-" + "é" * 200 + ".json",
                  "short." + "e" * 300):
            out = swi.canonical_inbox_name(n)
            self.assertLessEqual(len(out.encode("utf-8")), swi.MAX_FILENAME_BYTES, repr(n))


class WriterReaderConvergenceTest(unittest.TestCase):
    """The on-disk name safe_write_inbox writes must equal what a reader rebuilds
    via canonical_inbox_name for the same logical filename."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._orig = (swi.AGENTS_ROOT, swi.INBOXES_ROOT, swi.ROUTING_EVENTS_LOG)
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / "inboxes"
        swi.ROUTING_EVENTS_LOG = self._root / "logs" / "routing-events.jsonl"
        self._orig_rv = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / "repo"
        rv.invalidate_cache()

    def tearDown(self):
        swi.AGENTS_ROOT, swi.INBOXES_ROOT, swi.ROUTING_EVENTS_LOG = self._orig
        rv.REPO_ROOT = self._orig_rv
        rv.invalidate_cache()
        self._tmp.cleanup()

    def _task(self):
        return {"task_id": "t", "prompt": "x" * 150, "source": "beacon"}

    def test_writer_on_disk_name_equals_reader_canonical(self):
        # Each is a previously-divergent class: '/'-bearing, multibyte-over-cap,
        # and long-ASCII-over-cap. The writer's on-disk name must equal what a
        # reader rebuilds via canonical_inbox_name.
        for task_id in ("medic-silence-disk /var/log",
                        "é" * 130,
                        "z" * 400):
            logical = f"build-{task_id}.json"
            dest = swi.safe_write_inbox("forge", self._task(), "beacon", logical)
            reader_name = swi.canonical_inbox_name(logical)
            self.assertEqual(dest.name, reader_name, f"diverged for {task_id[:20]!r}…")
            self.assertTrue((dest.parent / reader_name).exists())


class HealPipelineGlobAlignmentTest(unittest.TestCase):
    """heal_pipeline_stall's retry-sibling glob must match the outbox sibling
    names inbox_watcher ACTUALLY writes (driven through the real _unique_dest,
    not a hand-rolled copy), including the dot-only edge."""

    def setUp(self):
        import inbox_watcher  # noqa: E402
        self.iw = inbox_watcher
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _real_outbox_sibling(self, task_id: str) -> str:
        # Exactly what inbox_watcher does: sanitize the whole filename, then let
        # _unique_dest derive the next sibling when the base already exists.
        name = swi.sanitize_component(f"{task_id}.json")
        (self.dir / name).write_text("{}")  # base present -> sibling .1
        return self.iw._unique_dest(self.dir, name).name

    def _heal_glob(self, task_id: str) -> str:
        archive_stem = swi.sanitize_component(f"{task_id}.json")[:-len(".json")]
        return f"{archive_stem}.*.json"

    def test_glob_matches_real_sibling(self):
        for task_id in ("forge-supply-chain-3", "a/b/c", "..", ".", "x" * 60,
                        "medic-silence-disk /var/log"):
            sib = self._real_outbox_sibling(task_id)
            pat = self._heal_glob(task_id)
            self.assertTrue(fnmatch.fnmatch(sib, pat),
                            f"task_id={task_id!r}: real sibling {sib!r} !~ glob {pat!r}")


if __name__ == "__main__":
    unittest.main()
