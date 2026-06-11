#!/usr/bin/env python3
"""Structural guard for the Mirror bug-hunt corpus, with focus on the reuse lens.

The corpus (review/known-bug-patterns.json) is read by Mirror's bug-hunt gate AND
appended to by the Phase-2 Pulse distill loop, so a malformed entry would silently
break lens routing. This validates the shape and pins the invariant that makes
Lens I (reuse/reinvention) safe: it must stay ADVISORY (blocking=false), or it
would start gating merges on the young, brittle v1 librarian.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_reuse_lens_corpus
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import unittest
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[2] / 'review' / 'known-bug-patterns.json'
REQUIRED_FIELDS = {
    'class_id', 'name', 'description', 'detection_signature',
    'review_lens', 'severity_default', 'example_finding_ids', 'blocking',
}
REUSE_CLASS_ID = 'reinvents-cataloged-shelf-component'
REUSE_LENS = 'reuse-reinvention'


class TestCorpusStructure(unittest.TestCase):

    def setUp(self):
        self.corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
        self.classes = self.corpus['classes']

    def test_every_class_has_required_fields_and_types(self):
        seen_ids = set()
        for c in self.classes:
            missing = REQUIRED_FIELDS - set(c)
            self.assertFalse(missing, f'{c.get("class_id")} missing {missing}')
            self.assertIsInstance(c['blocking'], bool, c['class_id'])
            self.assertIsInstance(c['example_finding_ids'], list, c['class_id'])
            self.assertNotIn(c['class_id'], seen_ids,
                             f'duplicate class_id {c["class_id"]}')
            seen_ids.add(c['class_id'])


class TestReuseLensInvariants(unittest.TestCase):

    def setUp(self):
        self.corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
        self.reuse = next(
            (c for c in self.corpus['classes']
             if c['class_id'] == REUSE_CLASS_ID), None)

    def test_reuse_class_present(self):
        self.assertIsNotNone(
            self.reuse, f'{REUSE_CLASS_ID} missing from corpus')

    def test_reuse_class_is_advisory_not_blocking(self):
        # The load-bearing invariant: Lens I must never gate a merge. A future
        # edit flipping this to true would silently turn the advisory reuse
        # nudge into a hard gate on the v1 librarian (PLAN §9-Q4).
        self.assertIs(self.reuse['blocking'], False)

    def test_reuse_class_routes_to_its_lens(self):
        self.assertEqual(self.reuse['review_lens'], REUSE_LENS)

    def test_reuse_class_not_claimed_as_audit_bug(self):
        # Forward-looking class: it should carry no escaped-bug finding ids
        # (those belong to the 64-bug audit corpus only).
        self.assertEqual(self.reuse['example_finding_ids'], [])


if __name__ == '__main__':
    unittest.main()
