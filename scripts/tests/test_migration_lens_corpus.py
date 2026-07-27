#!/usr/bin/env python3
"""Structural guard for Lens J (migration safety) — the sibling of test_reuse_lens_corpus.

Lens J exists because three RSDPM migrations (0022, 0029, 0030) merged, sat
unapplied, and broke something live while every surface reported green. Once
apply-on-merge lands, merging a migration EXECUTES it, so this review is the last
point a human is asked anything.

That makes two properties load-bearing, and prose alone does not hold them:

  1. The migration classes must stay `blocking: true` — the exact inverse of Lens
     I's invariant. Lens I is advisory because reuse is a judgment call; Lens J
     blocks because the cost of a false negative is data, already gone.
  2. The lens doc must keep the rules that stop the reviewer becoming a rubber
     stamp: UNKNOWN counts as FAIL, the verdict is written for a non-DBA, and the
     write-and-review conflict is stated out loud rather than papered over.

A future edit that softens either one should have to delete a test to do it.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_migration_lens_corpus
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'review' / 'known-bug-patterns.json'
LENSES = ROOT / 'review' / 'mirror-bughunt-lenses.md'

MIGRATION_LENS = 'migration-safety'
EXPECTED_CLASS_IDS = {
    'migration-irreversible-data-loss',
    'migration-out-of-order-clobber',
    'migration-unhardened-object',
}


class TestMigrationCorpusClasses(unittest.TestCase):

    def setUp(self):
        self.classes = json.loads(CORPUS.read_text(encoding='utf-8'))['classes']
        self.migration = [c for c in self.classes
                          if c['review_lens'] == MIGRATION_LENS]

    def test_the_three_classes_are_present(self):
        self.assertEqual(
            EXPECTED_CLASS_IDS, {c['class_id'] for c in self.migration},
            'Lens J routes by review_lens == "migration-safety"; a renamed or '
            'removed class silently drops that detection signature from the gate.')

    def test_migration_classes_block(self):
        for c in self.migration:
            self.assertTrue(
                c['blocking'],
                f'{c["class_id"]} must stay blocking=True. Lens I is advisory '
                'because reuse is a judgment call; Lens J blocks because a false '
                'negative here is data that is already gone.')

    def test_migration_classes_are_high_severity(self):
        for c in self.migration:
            self.assertEqual('HIGH', c['severity_default'], c['class_id'])

    def test_migration_classes_carry_no_audit_finding_ids(self):
        # They are not among the 64 escaped bugs — they come from the RSDPM
        # apply-gap incidents. Borrowing audit ids would corrupt coverage math.
        for c in self.migration:
            self.assertEqual([], c['example_finding_ids'], c['class_id'])

    def test_audit_coverage_claim_untouched(self):
        corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
        self.assertTrue(corpus['coverage']['all_64_covered'],
                        'all_64_covered describes the audit corpus only; adding '
                        'forward-looking classes must not disturb it.')


class TestMigrationLensDoc(unittest.TestCase):
    """The anti-rubber-stamp rules are the lens. Pin them by substance."""

    def setUp(self):
        self.doc = LENSES.read_text(encoding='utf-8')

    def test_lens_j_section_exists(self):
        self.assertIn('### Lens J — migration safety', self.doc)

    def test_unknown_counts_as_fail(self):
        self.assertIn('UNKNOWN counts as FAIL', self.doc,
                      'The rule that makes this lens capable of saying no: an '
                      'unquantified blast radius is a block, not a pass.')

    def test_gating_row_present(self):
        self.assertIn('| migration-safety | 40 | 60 |', self.doc,
                      'Lowest thresholds in the table, on purpose — with '
                      'apply-on-merge a miss is not latent, it has happened.')

    def test_verdict_is_written_for_a_non_dba(self):
        self.assertIn('## Migration verdict —', self.doc)
        for required in ('**Reversible:**', '**Data at risk:**',
                         '**Recommendation:**', '**What I could not check:**'):
            self.assertIn(required, self.doc,
                          f'verdict template lost its {required} line')

    def test_never_asks_larry_to_approve_sql(self):
        self.assertIn('Never write "approve this SQL?"', self.doc,
                      'The whole reframe: he has said he cannot evaluate SQL, so '
                      'a verdict that asks him to is a rubber stamp with steps.')

    def test_write_and_review_conflict_is_stated(self):
        self.assertIn('the same model writes\nthe migration and reviews it',
                      self.doc,
                      'Say the conflict out loud rather than papering over it.')

    def test_approval_rate_is_tracked(self):
        self.assertIn('never recommended "do not apply" is not reviewing',
                      self.doc)


if __name__ == '__main__':
    unittest.main()
