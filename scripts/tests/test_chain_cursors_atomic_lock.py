#!/usr/bin/env python3
"""Tests for the M3 seam-audit fix: atomic + flock-guarded log-cursors file.

chain_event_shipper.save_log_cursors and larry_alerts_retention.
_refresh_shipper_cursor both write the SAME file (chain-event-cursors.json) from
two processes. The shipper's save was a plain non-atomic write_text and neither
side took a shared lock, so:

  * a mid-write crash left a torn file that load_log_cursors swallows as {},
    resetting every cursor (outbox_log/larry_alerts/sentinel_alerts) to 0 → a
    full re-read + mass re-upsert storm; and
  * retention's load-modify-write could clobber an offset the shipper had just
    advanced (a non-torn lost-update).

The sibling save_journal_cursor was already hardened under PR-E2 #18; M3
propagates the same atomic-write + shared-flock contract to save_log_cursors and
makes retention's refresh a locked read-modify-write transaction.

Pure modules (no fastapi), so these run on bare system Python.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import chain_event_shipper as ces  # noqa: E402
import file_lock  # noqa: E402
import larry_alerts_retention as lar  # noqa: E402


class _CursorTmp(unittest.TestCase):
    """Point ces.LOG_CURSORS_FILE at a fresh tmp file per test."""

    def setUp(self):
        super().setUp()
        self._dir = Path(tempfile.mkdtemp(prefix='m3-cursors-'))
        self._orig = ces.LOG_CURSORS_FILE
        ces.LOG_CURSORS_FILE = self._dir / 'chain-event-cursors.json'

    def tearDown(self):
        ces.LOG_CURSORS_FILE = self._orig
        super().tearDown()

    def _save(self, mapping):
        ces.save_log_cursors({k: ces.FileCursor(offset=v) for k, v in mapping.items()})

    def _offsets(self):
        return {k: c.offset for k, c in ces.load_log_cursors().items()}


# -------------------- atomicity --------------------

class TestSaveLogCursorsAtomic(_CursorTmp):
    def test_round_trips_and_leaves_no_temp(self):
        self._save({'outbox_log': 12, 'larry_alerts': 7})
        self.assertEqual(self._offsets(), {'outbox_log': 12, 'larry_alerts': 7})
        # No leftover atomic-write temp files alongside the cursors file.
        leftovers = [p.name for p in self._dir.iterdir()
                     if p != ces.LOG_CURSORS_FILE
                     and not p.name.endswith('.lock')]
        self.assertEqual(leftovers, [])

    def test_mid_write_failure_leaves_prior_file_intact(self):
        # A valid prior file the shipper must never lose to a torn half-write.
        self._save({'outbox_log': 100, 'larry_alerts': 50})

        # Simulate a crash at the publish step (os.replace) of the atomic write.
        orig_replace = ces.atomic_io.os.replace

        def boom(src, dst):
            raise OSError('simulated crash before rename')

        ces.atomic_io.os.replace = boom
        try:
            # save_log_cursors is best-effort and swallows the OSError.
            self._save({'outbox_log': 999, 'larry_alerts': 999})
        finally:
            ces.atomic_io.os.replace = orig_replace

        # The old file is still intact and parseable — never torn → never {}.
        self.assertEqual(self._offsets(), {'outbox_log': 100, 'larry_alerts': 50})
        # The failed write left no temp file behind.
        leftovers = [p.name for p in self._dir.iterdir()
                     if p != ces.LOG_CURSORS_FILE
                     and not p.name.endswith('.lock')]
        self.assertEqual(leftovers, [])


# -------------------- locking / lost-update --------------------

class TestLogCursorsLocking(_CursorTmp):
    def test_transaction_holds_exclusive_lock(self):
        lock_path = file_lock.sidecar_lock_path(ces.LOG_CURSORS_FILE)
        with ces.log_cursors_transaction():
            # A second acquirer (same lock file, distinct fd) must be excluded.
            with self.assertRaises(file_lock.LockTimeout):
                with file_lock.exclusive_lock(lock_path, timeout=0.05):
                    pass

    def test_transaction_reads_fresh_state_not_stale_snapshot(self):
        # Shipper persists advanced offsets for the keys it owns.
        self._save({'outbox_log': 50, 'larry_alerts': 30})
        # Shipper drains again and advances further (a concurrent writer would
        # have done this between a stale load and save under the old code).
        self._save({'outbox_log': 80, 'larry_alerts': 60})

        # Retention now refreshes ONLY its own key via the locked transaction.
        with ces.log_cursors_transaction() as cursors:
            cursors['larry_alerts'] = ces.FileCursor(offset=0)

        # outbox_log's advance is preserved (not clobbered by a stale snapshot)
        # and larry_alerts is reset to 0 as intended.
        self.assertEqual(self._offsets(), {'outbox_log': 80, 'larry_alerts': 0})


class TestRetentionRefreshIntegration(_CursorTmp):
    def test_refresh_preserves_concurrently_advanced_other_key(self):
        # Seed the shipper's owned keys at advanced offsets.
        self._save({'outbox_log': 80, 'sentinel_alerts': 40, 'larry_alerts': 25})

        alerts = self._dir / 'larry-alerts.jsonl'
        alerts.write_text('{"a": 1}\n{"b": 2}\n')

        ok = lar._refresh_shipper_cursor(
            ces.LOG_CURSORS_FILE, lar.SHIPPER_CURSOR_KEY, alerts,
        )
        self.assertTrue(ok)

        cursors = ces.load_log_cursors()
        # Retention's key is reset to a full re-read (offset 0)...
        self.assertEqual(cursors[lar.SHIPPER_CURSOR_KEY].offset, 0)
        # ...while the shipper's other advanced offsets are untouched.
        self.assertEqual(cursors['outbox_log'].offset, 80)
        self.assertEqual(cursors['sentinel_alerts'].offset, 40)
        # The cursors file remains valid JSON (no torn write).
        json.loads(ces.LOG_CURSORS_FILE.read_text())


if __name__ == '__main__':
    unittest.main()
