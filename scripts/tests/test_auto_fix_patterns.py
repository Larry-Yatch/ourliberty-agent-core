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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import alert_triage_state as ats  # noqa: E402
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

    # These two replace the original test_seeded_records_start_cold, which
    # pinned the day-one SEED against a file the system legitimately mutates:
    # it asserted state=='probation' for every record (so any first graduation
    # turned main red) and clean_streak==0 (which Check V's reconcile
    # overwrites from the executions store on every run). Both are asserted
    # here as state-independent INVARIANTS instead, so neither a graduation nor
    # a reconcile can break them.

    def test_ungraduated_records_carry_no_graduation_residue(self):
        # Must hold after a trust loss too, not just at seed time: demote()
        # returns a record to exactly this shape. clean_streak and
        # total_dispatches are deliberately NOT asserted — both are caches
        # Check V derives from the executions store, so a probation record may
        # legitimately carry non-zero values.
        for r in (p for p in self.patterns if p['state'] != 'graduated'):
            self.assertIsNone(
                r['graduated_at'],
                f'{r["template"]} is not graduated but carries a graduated_at '
                f'stamp — demote() must clear it')

    def test_graduated_records_satisfy_every_graduation_invariant(self):
        for r in (p for p in self.patterns if p['state'] == 'graduated'):
            t = r['template']
            # A real timestamp, not merely "not null" — '' / 0 / False must fail.
            self.assertIsInstance(
                r['graduated_at'], str,
                f'{t} graduated_at must be an ISO-8601 string')
            try:
                graduated_at = datetime.fromisoformat(r['graduated_at'])
            except ValueError:
                self.fail(f'{t} graduated_at {r["graduated_at"]!r} is not ISO-8601')
            # Only reversible, non-guarded templates may ever hold this state.
            self.assertTrue(
                r['reversible'],
                f'{t} is graduated but marked irreversible')
            self.assertFalse(
                r['permanent_guard'],
                f'{t} is graduated but carries the permanent_guard floor')
            # A past correction does not bar re-graduation, but a correction
            # NEWER than the graduation means a demotion was missed.
            if r['last_larry_correction_at'] is not None:
                self.assertLess(
                    datetime.fromisoformat(r['last_larry_correction_at']),
                    graduated_at,
                    f'{t} is graduated but was corrected after it graduated — '
                    f'demote_on_adverse_execution should have demoted it')


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
        # Drive the PRODUCTION consumer, not a restatement of it. Rebuilding
        # the { state != 'graduated' or permanent_guard } comprehension here
        # and then asserting against it would pass for every possible registry
        # content — it never touches the code that actually gates auto-fix.
        # alert_triage_state.classify() is that code (§ 6.6 gates 2 and 3).
        registry = ats.load_registry(_REGISTRY)
        self.assertEqual(
            set(registry), {r['template'] for r in _load()['patterns']},
            'load_registry() dropped or invented a template')
        for r in _load()['patterns']:
            t = r['template']
            verdict = ats.classify(
                {'source': 'test', 'subject': t, 'template': t},
                registry=registry, translations={},
                route_fn=lambda *_a, **_k: 'escalate')
            if r['permanent_guard'] or r['state'] != 'graduated':
                self.assertEqual(
                    (verdict['tier'], verdict['decision']), (2, 'ask'),
                    f'{t} is guarded but classify() did not route it to ask')
            else:
                self.assertEqual(
                    (verdict['tier'], verdict['decision']), (1, 'auto-fix'),
                    f'{t} is graduated and non-guarded but classify() did not '
                    f'route it to auto-fix')

    def test_permanent_guard_beats_graduated_state_in_classify(self):
        # Every record in the live registry that is permanent_guard is ALSO
        # probation, so `permanent_guard` and `state != 'graduated'` never
        # disagree on real data — a classify() that dropped the guard check
        # entirely would still pass the loop above. This pins the half that
        # disagrees, using a shape the real registry may never hold (see
        # TestSafetyInvariants.test_no_permanent_guard_is_graduated): if the
        # floor were ever bypassed upstream, classify() must STILL refuse to
        # auto-fix it.
        impossible = {
            'template': 'rotate-credential',
            'state': 'graduated',
            'reversible': False,
            'permanent_guard': True,
            'clean_streak': 9_999,
            'total_dispatches': 9_999,
            'last_larry_correction_at': None,
            'graduated_at': '2026-01-01T00:00:00+00:00',
        }
        verdict = ats.classify(
            {'source': 'test', 'subject': 'x', 'template': 'rotate-credential'},
            registry={'rotate-credential': impossible}, translations={},
            route_fn=lambda *_a, **_k: 'escalate')
        self.assertEqual(
            (verdict['tier'], verdict['decision']), (2, 'ask'),
            'a permanent_guard record marked graduated must still route to '
            'ask — the guard floor outranks state')
        self.assertIn('permanent_guard floor', verdict['rationale'])


if __name__ == '__main__':
    unittest.main()
