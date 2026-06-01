"""Tests for scripts/medic_ledger.py.

Coverage (PR1 Medic scaffold):
- fingerprint stability: same (source, subject) -> same key; None/missing
  fields collapse to a defined shape (no crash).
- attempt counter: increments per matching fingerprint; returns 0 for
  unseen fingerprints.
- append_record: persists to the ledger file; preserves classification
  + outcome + attempt; coerces invalid values to safe defaults.
- fail safe on read: missing file -> attempt_count 0, recent []; a
  ledger with a malformed line is skipped (the surrounding valid lines
  still surface).
- fail safe on write: when the ledger directory is unwritable
  (simulated by a read-only path under a tmp root), append_record
  returns False rather than raising.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import medic_ledger  # noqa: E402


class _IsolatedLedger(unittest.TestCase):
    """Point OURLIBERTY_AGENTS_ROOT at a tmp dir + reload the module so
    LEDGER_FILE / LOG_FILE pick up the override. Mirrors the
    _TempAgentsRootMixin pattern in test_heal_pipeline_stall.py."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='medic-ledger-')
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmpdir
        importlib.reload(medic_ledger)

    def tearDown(self) -> None:
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(medic_ledger)
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class FingerprintTest(_IsolatedLedger):
    def test_same_source_subject_yields_same_key(self) -> None:
        a = medic_ledger.fingerprint('sentinel', 'inbox-stall:agent-x')
        b = medic_ledger.fingerprint('sentinel', 'inbox-stall:agent-x')
        self.assertEqual(a, b)
        self.assertEqual(a, 'sentinel:inbox-stall:agent-x')

    def test_different_source_yields_different_key(self) -> None:
        a = medic_ledger.fingerprint('sentinel', 'inbox-stall')
        b = medic_ledger.fingerprint('watchdog', 'inbox-stall')
        self.assertNotEqual(a, b)

    def test_missing_source_uses_placeholder(self) -> None:
        # No crash; '?' placeholder so a None-source row stays distinct.
        a = medic_ledger.fingerprint(None, 'foo')
        self.assertEqual(a, '?:foo')

    def test_missing_subject_yields_empty_suffix(self) -> None:
        a = medic_ledger.fingerprint('watchdog', None)
        self.assertEqual(a, 'watchdog:')


class AttemptCountTest(_IsolatedLedger):
    def test_missing_file_returns_zero(self) -> None:
        self.assertEqual(medic_ledger.attempt_count('any:thing'), 0)

    def test_increments_per_matching_fingerprint(self) -> None:
        fp = medic_ledger.fingerprint('sentinel', 'inbox-stall:agent-x')
        self.assertTrue(medic_ledger.append_record(
            'sentinel', 'inbox-stall:agent-x', 'reversible', 'escalated', 1))
        self.assertEqual(medic_ledger.attempt_count(fp), 1)
        self.assertTrue(medic_ledger.append_record(
            'sentinel', 'inbox-stall:agent-x', 'reversible', 'escalated', 2))
        self.assertEqual(medic_ledger.attempt_count(fp), 2)

    def test_unseen_fingerprint_returns_zero(self) -> None:
        medic_ledger.append_record(
            'sentinel', 'inbox-stall:agent-a', 'reversible', 'escalated', 1)
        # Different subject = different fingerprint.
        self.assertEqual(
            medic_ledger.attempt_count(
                medic_ledger.fingerprint('sentinel', 'inbox-stall:agent-z')),
            0,
        )


class AppendRecordTest(_IsolatedLedger):
    def test_append_persists_known_shape(self) -> None:
        ok = medic_ledger.append_record(
            'watchdog', 'forge-bot-silent', 'reversible', 'escalated', 1,
            notes='PR1 stub'
        )
        self.assertTrue(ok)
        contents = medic_ledger.LEDGER_FILE.read_text().strip().splitlines()
        self.assertEqual(len(contents), 1)
        rec = json.loads(contents[0])
        self.assertEqual(rec['source'], 'watchdog')
        self.assertEqual(rec['subject'], 'forge-bot-silent')
        self.assertEqual(rec['classification'], 'reversible')
        self.assertEqual(rec['outcome'], 'escalated')
        self.assertEqual(rec['attempt'], 1)
        self.assertEqual(rec['fingerprint'], 'watchdog:forge-bot-silent')
        self.assertEqual(rec['notes'], 'PR1 stub')
        self.assertIn('ts', rec)

    def test_unknown_classification_coerced_to_unknown(self) -> None:
        medic_ledger.append_record(
            'sentinel', 'inbox-stall', 'invented-tier', 'escalated', 1)
        rec = json.loads(medic_ledger.LEDGER_FILE.read_text().splitlines()[0])
        self.assertEqual(rec['classification'], 'unknown')

    def test_invalid_attempt_coerced_to_one(self) -> None:
        medic_ledger.append_record(
            'sentinel', 'inbox-stall', 'reversible', 'escalated', 0)
        rec = json.loads(medic_ledger.LEDGER_FILE.read_text().splitlines()[0])
        self.assertEqual(rec['attempt'], 1)


class FailSafeTest(_IsolatedLedger):
    def test_malformed_line_skipped(self) -> None:
        # Write one good entry, then a junk line, then another good entry.
        medic_ledger.append_record(
            'sentinel', 'a', 'reversible', 'escalated', 1)
        with open(medic_ledger.LEDGER_FILE, 'a', encoding='utf-8') as f:
            f.write('{not valid json\n')
        medic_ledger.append_record(
            'sentinel', 'b', 'reversible', 'escalated', 1)
        recs = medic_ledger.read_recent(10)
        # The malformed line is dropped; the two valid entries survive.
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]['subject'], 'a')
        self.assertEqual(recs[1]['subject'], 'b')

    def test_append_returns_false_when_path_unwritable(self) -> None:
        # Replace the ledger file path with one whose parent cannot be
        # created (a regular file standing in for the directory).
        sentinel = Path(self._tmpdir) / 'blocked'
        sentinel.write_text('not a directory')
        original = medic_ledger.LEDGER_FILE
        try:
            medic_ledger.LEDGER_FILE = sentinel / 'nope.jsonl'
            ok = medic_ledger.append_record(
                'sentinel', 'x', 'reversible', 'escalated', 1)
            self.assertFalse(ok)
        finally:
            medic_ledger.LEDGER_FILE = original


if __name__ == '__main__':
    unittest.main()
