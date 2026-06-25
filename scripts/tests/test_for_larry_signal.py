#!/usr/bin/env python3
"""Tests for for_larry_signal.py — the canonical durable for-Larry signal.

Spec: agents/beacon/specs/operator-needs-you-feed.md §5.1 + §5.2 (Part A).

Run:
    python3 -m unittest scripts.tests.test_for_larry_signal
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
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import for_larry_signal as fls  # noqa: E402

ESC = fls.ESCALATION_KEY_PREFIX
CLR = fls.CLARIFY_EXHAUSTED_KEY_PREFIX


class _SignalFileTest(unittest.TestCase):
    """Base: redirect the signal file to a per-test temp path."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / 'for-larry-escalations.json'
        self._prev = os.environ.get('OURLIBERTY_FOR_LARRY_SIGNAL_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = str(self.path)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_SIGNAL_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = self._prev
        self._dir.cleanup()

    def _keys(self):
        return {e.get('key') for e in fls.active_entries()}


class PathTest(_SignalFileTest):
    def test_env_override_wins(self):
        self.assertEqual(fls.signal_path(), self.path)


class UpsertResolveTest(_SignalFileTest):
    def test_missing_file_reads_empty(self):
        self.assertEqual(fls.load_records(), {})
        self.assertEqual(fls.active_entries(), [])

    def test_upsert_then_active(self):
        fls.upsert_record(ESC + 'a', {'headline': 'boom', 'severity': 'critical'})
        entries = fls.active_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['headline'], 'boom')
        self.assertIs(entries[0]['for_larry'], True)
        self.assertIs(entries[0]['resolved'], False)
        self.assertEqual(entries[0]['key'], ESC + 'a')
        self.assertTrue(entries[0].get('ts'))

    def test_resolve_clears_from_active(self):
        fls.upsert_record(ESC + 'a', {'headline': 'boom'})
        self.assertTrue(fls.resolve_record(ESC + 'a'))
        self.assertEqual(fls.active_entries(), [])
        # The record persists (audit) but is marked resolved.
        rec = fls.load_records()[ESC + 'a']
        self.assertIs(rec['resolved'], True)
        self.assertTrue(rec.get('resolved_at'))

    def test_resolve_missing_is_noop(self):
        self.assertFalse(fls.resolve_record(ESC + 'nope'))

    def test_resolve_already_resolved_is_noop(self):
        fls.upsert_record(ESC + 'a', {'headline': 'boom'})
        self.assertTrue(fls.resolve_record(ESC + 'a'))
        self.assertFalse(fls.resolve_record(ESC + 'a'))

    def test_reupsert_unresolves(self):
        fls.upsert_record(ESC + 'a', {'headline': 'boom'})
        fls.resolve_record(ESC + 'a')
        self.assertEqual(fls.active_entries(), [])
        # Trigger re-appears → re-upsert flips it back to active.
        fls.upsert_record(ESC + 'a', {'headline': 'boom again'})
        self.assertEqual(self._keys(), {ESC + 'a'})

    def test_written_file_is_valid_json_with_schema(self):
        fls.upsert_record(ESC + 'a', {'headline': 'boom'})
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc['version'], 1)
        self.assertIn('updated_at', doc)
        self.assertIn(ESC + 'a', doc['records'])


class SyncPrefixTest(_SignalFileTest):
    def test_writes_active_and_resolves_absent(self):
        # First sync: two escalations promoted.
        fls.sync_prefix(
            {ESC + 'a': {'headline': 'a'}, ESC + 'b': {'headline': 'b'}}, ESC,
        )
        self.assertEqual(self._keys(), {ESC + 'a', ESC + 'b'})
        # Second sync: 'a' left the snapshot → self-resolves; 'b' stays.
        out = fls.sync_prefix({ESC + 'b': {'headline': 'b'}}, ESC)
        self.assertEqual(self._keys(), {ESC + 'b'})
        self.assertIn(ESC + 'a', out['resolved'])

    def test_sync_only_touches_its_prefix(self):
        # A CLARIFY-exhausted record from the other producer.
        fls.upsert_record(CLR + 't1', {'headline': 'stuck'})
        # promote_alerts sync with an EMPTY active set must not resolve it.
        fls.sync_prefix({}, ESC)
        self.assertIn(CLR + 't1', self._keys())

    def test_empty_sync_no_churn_when_nothing_changes(self):
        # No file yet, empty active → still writes once so the file exists.
        fls.sync_prefix({}, ESC)
        self.assertTrue(self.path.exists())
        mtime = self.path.stat().st_mtime_ns
        # A second empty sync with nothing to change must not rewrite.
        fls.sync_prefix({}, ESC)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)


class CorruptFileTest(_SignalFileTest):
    def test_corrupt_file_degrades_to_empty(self):
        self.path.write_text('{ not json')
        self.assertEqual(fls.load_records(), {})
        self.assertEqual(fls.active_entries(), [])
        # And a write recovers cleanly.
        fls.upsert_record(ESC + 'a', {'headline': 'boom'})
        self.assertEqual(self._keys(), {ESC + 'a'})


if __name__ == '__main__':
    unittest.main()
