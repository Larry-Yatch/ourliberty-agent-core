#!/usr/bin/env python3
"""Tests for scripts/rotate_cycle_journal.py — the /cycle journal rotator.

Covers the contract that keeps runbooks/cycle-journal.md's per-commit git blob
small (the 2026-07-07 droplet-overload root fix):

  - Keeps the newest KEEP_RECENT entries live; archives the older tail.
  - Lossless: live(kept) + archive(evicted) reconstructs the original entries
    in original order (newest-first live, oldest tail in archive).
  - No-op when the journal already holds <= KEEP_RECENT entries.
  - Idempotent: a second run over an already-trimmed journal changes nothing.
  - Archive chunks roll at the size cap; subsequent evictions append to the
    current chunk rather than rewriting prior (frozen) chunks.
  - Preamble (content before the first entry) is preserved verbatim.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_rotate_cycle_journal
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import rotate_cycle_journal as rcj  # noqa: E402

PREAMBLE = (
    "# /cycle Journal\n\n"
    "**Append-only chronological journal.**\n\n---\n\n"
)


def make_entry(n: int, body: str = "body line\n") -> str:
    # Newest-first in the file means higher N sits above lower N.
    return f"## Iteration ~{n} — 2026-07-0{(n % 9) + 1}T00:00Z\n\n{body}\n"


def make_journal(count: int, body: str = "body line\n") -> str:
    # Entries newest-first: N=count at top down to N=1 at bottom.
    return PREAMBLE + "".join(make_entry(n, body) for n in range(count, 0, -1))


class RotateCycleJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.journal = self.root / "runbooks" / "cycle-journal.md"
        self.journal.parent.mkdir(parents=True)
        self.archive_dir = self.journal.parent / "journal-archive"

    def tearDown(self):
        self.tmp.cleanup()

    def _live_entries(self):
        _, entries = rcj.split_journal(self.journal.read_text())
        return entries

    def _archive_entries(self):
        out = []
        for chunk in sorted(self.archive_dir.glob(rcj.CHUNK_GLOB)):
            _, entries = rcj.split_journal(chunk.read_text())
            out.extend(entries)
        return out

    def test_split_roundtrip(self):
        text = make_journal(5)
        preamble, entries = rcj.split_journal(text)
        self.assertEqual(preamble, PREAMBLE)
        self.assertEqual(len(entries), 5)
        self.assertEqual(preamble + "".join(entries), text)

    def test_noop_when_small(self):
        self.journal.write_text(make_journal(10))
        summary = rcj.rotate(self.journal, keep_recent=40, chunk_cap=1 << 20)
        self.assertEqual(summary["status"], "noop")
        self.assertEqual(len(self._live_entries()), 10)
        self.assertFalse(self.archive_dir.exists())

    def test_keeps_newest_and_archives_rest_lossless(self):
        original = make_journal(100)
        self.journal.write_text(original)
        _, orig_entries = rcj.split_journal(original)

        summary = rcj.rotate(self.journal, keep_recent=40, chunk_cap=1 << 20)
        self.assertEqual(summary["status"], "rotated")
        self.assertEqual(summary["kept"], 40)
        self.assertEqual(summary["evicted"], 60)

        live = self._live_entries()
        arch = self._archive_entries()
        # Live keeps the newest (top) 40 in order; archive holds the older tail.
        self.assertEqual(live, orig_entries[:40])
        self.assertEqual(arch, orig_entries[40:])
        self.assertEqual(live + arch, orig_entries)  # fully lossless
        # Preamble preserved verbatim in the live file.
        self.assertTrue(self.journal.read_text().startswith(PREAMBLE))

    def test_idempotent(self):
        self.journal.write_text(make_journal(100))
        rcj.rotate(self.journal, keep_recent=40, chunk_cap=1 << 20)
        chunks_before = sorted(p.name for p in self.archive_dir.glob(rcj.CHUNK_GLOB))
        live_before = self.journal.read_text()

        summary = rcj.rotate(self.journal, keep_recent=40, chunk_cap=1 << 20)
        self.assertEqual(summary["status"], "noop")
        chunks_after = sorted(p.name for p in self.archive_dir.glob(rcj.CHUNK_GLOB))
        self.assertEqual(chunks_before, chunks_after)
        self.assertEqual(live_before, self.journal.read_text())

    def test_chunks_roll_at_cap(self):
        # Fat bodies so each entry is ~2KB; a small cap forces multiple chunks.
        self.journal.write_text(make_journal(60, body="x" * 2000 + "\n"))
        summary = rcj.rotate(self.journal, keep_recent=10, chunk_cap=8 * 1024)
        self.assertEqual(summary["evicted"], 50)
        chunks = sorted(self.archive_dir.glob(rcj.CHUNK_GLOB))
        self.assertGreater(len(chunks), 1, "cap should have rolled >1 chunk")
        # Every chunk stays within a reasonable bound of the cap (one entry of
        # slack for the roll-after-exceed rule).
        for c in chunks:
            self.assertLessEqual(c.stat().st_size, 8 * 1024 + 2200)
        # Still lossless across all chunks.
        _, orig_entries = rcj.split_journal(make_journal(60, body="x" * 2000 + "\n"))
        self.assertEqual(self._live_entries() + self._archive_entries(), orig_entries)

    def test_incremental_eviction_appends_to_current_chunk(self):
        # First rotation lands the initial tail.
        self.journal.write_text(make_journal(45))
        rcj.rotate(self.journal, keep_recent=40, chunk_cap=1 << 20)
        first_chunks = sorted(p.name for p in self.archive_dir.glob(rcj.CHUNK_GLOB))
        self.assertEqual(len(first_chunks), 1)
        arch_after_first = len(self._archive_entries())

        # Simulate a new cycle prepending one fresh entry, then rotate again.
        text = self.journal.read_text()
        preamble, entries = rcj.split_journal(text)
        self.journal.write_text(preamble + make_entry(999) + "".join(entries))
        summary = rcj.rotate(self.journal, keep_recent=40, chunk_cap=1 << 20)

        self.assertEqual(summary["status"], "rotated")
        self.assertEqual(summary["evicted"], 1)
        # No new chunk file — the single evicted entry appended to the current.
        self.assertEqual(
            sorted(p.name for p in self.archive_dir.glob(rcj.CHUNK_GLOB)),
            first_chunks,
        )
        self.assertEqual(len(self._archive_entries()), arch_after_first + 1)
        self.assertEqual(len(self._live_entries()), 40)


if __name__ == "__main__":
    unittest.main()
