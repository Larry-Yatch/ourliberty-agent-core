"""Unit tests for scripts/heal_silent_loop_death.find_recent_dispatches (PR-B,
audit #41).

Two fixes verified here:
  * the processed/ branch now skips directories and non-.json entries (it used
    to iterate raw entries, so a same-named directory could read as 'alive');
  * the slug is matched as a whole, boundary-delimited token with a length
    floor (id_match), not as a bare substring, so an unrelated artifact that
    merely contains the slug as an infix no longer masks a dead loop.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_silent_loop_death as hsld  # noqa: E402

SLUG = 'team-access-queue-check'  # 23 chars, above the id_match floor


class FindRecentDispatchesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='silent-loop-')
        self.inbox = Path(self._tmp) / 'forge'
        self.inbox.mkdir()
        (self.inbox / 'processed').mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, rel, mtime=None):
        p = self.inbox / rel
        p.write_text('{}')
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_active_dispatch_matches_bounded_slug(self):
        self._write(f'{SLUG}-20260605T010000Z.json')
        self.assertTrue(hsld.find_recent_dispatches(SLUG, [self.inbox]))

    def test_result_file_excluded(self):
        self._write(f'{SLUG}-20260605T010000Z-result.json')
        self.assertFalse(hsld.find_recent_dispatches(SLUG, [self.inbox]))

    def test_infix_only_does_not_match(self):
        # slug appears only inside a larger token (no boundary) -> no false alive
        self._write(f'x{SLUG}-extra.json')
        self.assertFalse(hsld.find_recent_dispatches(SLUG, [self.inbox]))

    def test_short_real_slug_still_matches_its_own_dispatch(self):
        # Real loop slugs are legitimately short ('re-queue'=8, 'self-task'=9,
        # 'queue-check'=11). A length floor would make these live loops read as
        # dead and emit a spurious LOOP_DEATH_DETECTED, so the structured-name
        # match uses min_len=1 — each slug still matches its own dispatch file.
        for slug in ('re-queue', 'self-task', 'queue-check'):
            self._write(f'{slug}-20260605T010000Z.json')
        for slug in ('re-queue', 'self-task', 'queue-check'):
            with self.subTest(slug=slug):
                self.assertTrue(hsld.find_recent_dispatches(slug, [self.inbox]))

    def test_processed_dir_entry_is_skipped(self):
        # a *directory* (not a file) whose name contains the slug must not count
        (self.inbox / 'processed' / f'{SLUG}-stale-dir.json').mkdir()
        self.assertFalse(hsld.find_recent_dispatches(SLUG, [self.inbox]))

    def test_processed_fresh_file_counts(self):
        self._write(f'processed/{SLUG}-20260605T010000Z.json', mtime=time.time())
        self.assertTrue(hsld.find_recent_dispatches(SLUG, [self.inbox]))

    def test_processed_stale_file_does_not_count(self):
        old = time.time() - (hsld.STALE_MIN * 60 + 600)
        self._write(f'processed/{SLUG}-20260605T010000Z.json', mtime=old)
        self.assertFalse(hsld.find_recent_dispatches(SLUG, [self.inbox]))


if __name__ == '__main__':
    unittest.main()
