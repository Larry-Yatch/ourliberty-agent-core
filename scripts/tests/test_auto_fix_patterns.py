#!/usr/bin/env python3
"""Shape + invariant tests for config/auto-fix-patterns.json (PR A).

The registry is the single source of truth for what Pulse may auto-fix.
These tests lock the record shape and the safety invariants Check 0 (PR B)
and Check V (PR C) will depend on, plus the derived-guarded-set rule that
SUPERSEDES the standalone guard-list file.

Run::

    cd ~/agent-core && python3 -m pytest scripts/tests/test_auto_fix_patterns.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import cycle_prime_ledger as cpl  # noqa: E402

_REGISTRY = _REPO_SCRIPTS.parent / 'config' / 'auto-fix-patterns.json'

_RECORD_FIELDS = {
    'template', 'state', 'reversible', 'permanent_guard',
    'clean_streak', 'total_dispatches', 'last_larry_correction_at',
    'graduated_at', 'plain_language',
}
_PLAIN_FIELDS = {'what', 'action', 'why_safe'}
_VALID_STATES = {'probation', 'graduated'}


def _load() -> dict:
    return json.loads(_REGISTRY.read_text())


class TestRegistryFile(unittest.TestCase):
    def test_file_exists_and_parses(self):
        self.assertTrue(_REGISTRY.exists(), f'missing registry {_REGISTRY}')
        data = _load()
        self.assertIn('_schema', data)
        self.assertIn('patterns', data)
        self.assertIsInstance(data['patterns'], list)
        self.assertTrue(data['patterns'], 'registry must seed at least one record')

    def test_schema_block_documents_graduation_gate(self):
        schema = _load()['_schema']
        self.assertEqual(schema['graduate_min_clean_streak'], 3)
        # The guard_list relationship must record the SUPERSEDE decision.
        self.assertIn('SUPERSEDES', schema['guard_list_relationship'])


class TestRecordShape(unittest.TestCase):
    def setUp(self):
        self.patterns = _load()['patterns']

    def test_every_record_has_exact_fields(self):
        for r in self.patterns:
            self.assertEqual(
                set(r), _RECORD_FIELDS,
                f'record {r.get("template")!r} has wrong field set')
            self.assertEqual(set(r['plain_language']), _PLAIN_FIELDS)
            for k in _PLAIN_FIELDS:
                self.assertTrue(r['plain_language'][k].strip(),
                                f'{r["template"]}.{k} is empty')

    def test_templates_are_canonical_and_unique(self):
        seen = set()
        for r in self.patterns:
            t = r['template']
            # Must round-trip through the ledger's canonicaliser.
            self.assertEqual(cpl.canonical_intervention_id(t, 'x'), f'{t}:x')
            self.assertNotIn(t, seen, f'duplicate template {t!r}')
            seen.add(t)

    def test_field_types(self):
        for r in self.patterns:
            self.assertIn(r['state'], _VALID_STATES)
            self.assertIsInstance(r['reversible'], bool)
            self.assertIsInstance(r['permanent_guard'], bool)
            self.assertIsInstance(r['clean_streak'], int)
            self.assertIsInstance(r['total_dispatches'], int)

    def test_seeded_records_start_cold(self):
        # PR A seeds nothing as graduated; the ladder must be earned.
        for r in self.patterns:
            self.assertEqual(r['state'], 'probation')
            self.assertEqual(r['clean_streak'], 0)
            self.assertEqual(r['total_dispatches'], 0)
            self.assertIsNone(r['graduated_at'])
            self.assertIsNone(r['last_larry_correction_at'])


class TestSafetyInvariants(unittest.TestCase):
    def setUp(self):
        self.patterns = _load()['patterns']

    def test_permanent_guard_implies_irreversible_seed(self):
        # The seeded guard floor (credential/money/destructive) is irreversible.
        for r in self.patterns:
            if r['permanent_guard']:
                self.assertFalse(
                    r['reversible'],
                    f'{r["template"]} is permanent_guard but marked reversible')

    def test_no_permanent_guard_is_graduated(self):
        for r in self.patterns:
            if r['permanent_guard']:
                self.assertNotEqual(r['state'], 'graduated')
                self.assertIsNone(r['graduated_at'])

    def test_guard_floor_present(self):
        # Credential, money, and destructive floors must all be seeded guarded.
        guarded = {r['template'] for r in self.patterns if r['permanent_guard']}
        safety_floor = {'rotate-credential', 'issue-refund', 'delete-resource'}
        self.assertTrue(
            safety_floor.issubset(guarded),
            f'safety floor not guarded: {safety_floor - guarded}')
        # 'uncategorized' is ALSO permanent_guard, but for a different reason:
        # it is the non-graduating classify-me bucket the ledger normalizes
        # untagged interventions into — not a destructive action. It must
        # never graduate, so it carries permanent_guard=true too.
        self.assertEqual(guarded, safety_floor | {'uncategorized'})


class TestDerivedGuardedSet(unittest.TestCase):
    """The guarded set is a DERIVED VIEW, not a separate file."""

    def test_no_standalone_guard_list_file(self):
        legacy = _REPO_SCRIPTS.parent / 'config' / 'action-template-guard-list.json'
        self.assertFalse(
            legacy.exists(),
            'standalone guard-list is SUPERSEDED by the registry; it must not exist')

    def test_derived_view_rule(self):
        # { template | state != 'graduated' or permanent_guard }
        patterns = _load()['patterns']
        guarded = {
            r['template'] for r in patterns
            if r['state'] != 'graduated' or r['permanent_guard']
        }
        # Every seeded record is probation, so all are guarded for now.
        self.assertEqual(guarded, {r['template'] for r in patterns})


if __name__ == '__main__':
    unittest.main()
