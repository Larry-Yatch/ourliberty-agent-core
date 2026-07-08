#!/usr/bin/env python3
"""Tests for scripts/silence_file_auditor.py (G8 standing check).

Covers: the audit() listing (key, age, TTL disposition, suppressed volume from
the sidecar counter), the newest-file sorting, the notable-selection gate, and
the no-DM-when-nothing-notable path. Sentinel-armed, no live gh / no live-tree
writes.

Hermetic under full-suite ordering: like the sibling test_larry_alerts.py, this
mock.patch.object's larry_alerts' SILENCE_ROOT / SILENCE_COUNTER_ROOT onto tmp
paths for the duration of each test rather than reloading the module + mutating
os.environ globally (which corrupts shared module state under `unittest
discover`).
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import larry_alerts  # noqa: E402
import silence_file_auditor as sfa  # noqa: E402


class _IsolatedAuditor(unittest.TestCase):
    """Point the silence + counter roots at a tempdir via patch (no reload, no
    env mutation) so state stays local and full-suite ordering is unaffected."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', tmp),
            mock.patch.object(larry_alerts, 'SILENCE_ROOT',
                              tmp / 'state' / 'alert-silenced'),
            mock.patch.object(larry_alerts, 'SILENCE_COUNTER_ROOT',
                              tmp / 'state' / 'alert-silenced-counts'),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _suppress(self, key: str, n: int) -> None:
        for _ in range(n):
            larry_alerts._note_silenced_suppression(key)


class AuditListingTest(_IsolatedAuditor):
    def test_empty_when_no_silences(self) -> None:
        self.assertEqual(sfa.audit(), [])
        self.assertIn('no active silences', sfa.render(sfa.audit()))

    def test_lists_each_silence_with_volume(self) -> None:
        larry_alerts.silence('a:one')
        larry_alerts.silence('a:two')
        self._suppress('a:one', 5)
        self._suppress('a:two', 2)
        rows = sfa.audit()
        self.assertEqual(len(rows), 2)
        # sorted heaviest-first
        self.assertEqual(rows[0]['key'], 'a:one')
        self.assertEqual(rows[0]['suppressed_count'], 5)
        self.assertEqual(rows[1]['suppressed_count'], 2)
        self.assertTrue(rows[0]['permanent'])

    def test_ttl_disposition_reported(self) -> None:
        # A silence with a past TTL reads as expired; a permanent one does not.
        larry_alerts.silence('a:ttl', ttl_sec=10, now=1000)
        rows = sfa.audit(now=2000)  # well past until=1010
        row = next(r for r in rows if r['key'] == 'a:ttl')
        self.assertFalse(row['permanent'])
        self.assertTrue(row['expired'])

    def test_age_computed_from_ts(self) -> None:
        larry_alerts.silence('a:aged')
        rows = sfa.audit(now=time.time() + 86400)  # +1 day
        row = next(r for r in rows if r['key'] == 'a:aged')
        self.assertIsNotNone(row['age_sec'])
        self.assertGreaterEqual(row['age_sec'], 86400 - 5)


class NotableSelectionTest(_IsolatedAuditor):
    def test_hard_volume_ceiling_is_notable(self) -> None:
        larry_alerts.silence('a:loud')
        self._suppress('a:loud', sfa.HARD_VOLUME_CEILING)
        row = next(r for r in sfa.audit() if r['key'] == 'a:loud')
        self.assertTrue(sfa._is_notable(row))

    def test_young_quiet_permanent_is_not_notable(self) -> None:
        larry_alerts.silence('a:quiet')
        row = next(r for r in sfa.audit() if r['key'] == 'a:quiet')
        self.assertFalse(sfa._is_notable(row))

    def test_stale_active_permanent_is_notable(self) -> None:
        larry_alerts.silence('a:stale')
        self._suppress('a:stale', sfa.ACTIVE_SUPPRESS_FLOOR)
        old = time.time() + (sfa.STALE_PERMANENT_AGE_SEC + 86400)
        row = next(r for r in sfa.audit(now=old) if r['key'] == 'a:stale')
        self.assertTrue(sfa._is_notable(row))


class MainDmTest(_IsolatedAuditor):
    def test_main_dms_only_when_notable(self) -> None:
        larry_alerts.silence('a:loud')
        self._suppress('a:loud', sfa.HARD_VOLUME_CEILING)
        with mock.patch.object(sfa, '_dm') as dm:
            rc = sfa.main(['--once'])
        self.assertEqual(rc, 0)
        dm.assert_called_once()

    def test_main_silent_when_nothing_notable(self) -> None:
        larry_alerts.silence('a:quiet')
        with mock.patch.object(sfa, '_dm') as dm:
            rc = sfa.main(['--once'])
        self.assertEqual(rc, 0)
        dm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
