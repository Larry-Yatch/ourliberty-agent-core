#!/usr/bin/env python3
"""Tests for post_merge_verifier.find_originating_chat (audit #44).

Source 2 (ship-tracking files) used to return the FIRST file with any
originating_chat_id, ignoring the PR/feature — so it could route a
verification briefing to the wrong operator chat. It must only trust a
tracking file that corresponds to this PR/feature, else fall through to the
briefing fallback.
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

import post_merge_verifier as pmv  # noqa: E402


class FindOriginatingChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ship = self.tmp / 'ship-tracking'
        self.ship.mkdir(parents=True, exist_ok=True)
        self._orig = {
            'SHIP_TRACKING_DIR': pmv.SHIP_TRACKING_DIR,
            'PR_ROUTING_FILE': pmv.PR_ROUTING_FILE,
            'BRIEFING_CHAT': pmv.BRIEFING_CHAT,
        }
        pmv.SHIP_TRACKING_DIR = self.ship
        # Point routing at a non-existent file so Source 1 is always skipped.
        pmv.PR_ROUTING_FILE = self.tmp / 'no-routing.json'
        pmv.BRIEFING_CHAT = -999

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(pmv, k, v)

    def _track(self, name: str, payload: dict):
        (self.ship / name).write_text(json.dumps(payload))

    def test_does_not_return_unrelated_feature_chat(self):
        # A tracking file for feature A must NOT be returned for feature B's PR;
        # with no match, fall through to the briefing fallback.
        self._track('feature-a.json',
                    {'feature': 'feature-a', 'originating_chat_id': 111})
        chat = pmv.find_originating_chat(pr_num=42, feature='feature-b')
        self.assertEqual(chat, pmv.BRIEFING_CHAT)

    def test_returns_matching_feature_chat(self):
        self._track('feature-a.json',
                    {'feature': 'feature-a', 'originating_chat_id': 111})
        self._track('feature-b.json',
                    {'feature': 'feature-b', 'originating_chat_id': 222})
        self.assertEqual(
            pmv.find_originating_chat(pr_num=42, feature='feature-b'), 222
        )

    def test_matches_by_pr_number(self):
        self._track('x.json', {'pr_num': 42, 'originating_chat_id': 333})
        self.assertEqual(
            pmv.find_originating_chat(pr_num=42, feature='uncategorized'), 333
        )

    def test_matches_by_filename_stem(self):
        self._track('feature-c.json', {'originating_chat_id': 444})
        self.assertEqual(
            pmv.find_originating_chat(pr_num=7, feature='feature-c'), 444
        )

    def test_uncategorized_does_not_wildcard_match(self):
        # A feature slug of 'uncategorized' must not match an arbitrary file by
        # feature/stem (that would reintroduce the wrong-routing bug); with no
        # pr_num match either, fall back.
        self._track('something.json', {'originating_chat_id': 555})
        self.assertEqual(
            pmv.find_originating_chat(pr_num=1, feature='uncategorized'),
            pmv.BRIEFING_CHAT,
        )

    def test_tracking_matches_helper(self):
        self.assertTrue(pmv._tracking_matches(
            Path('feature-b.json'), {'feature': 'feature-b'}, 42, 'feature-b'))
        self.assertFalse(pmv._tracking_matches(
            Path('feature-a.json'), {'feature': 'feature-a'}, 42, 'feature-b'))
        self.assertTrue(pmv._tracking_matches(
            Path('x.json'), {'pr_number': 42}, 42, 'feature-b'))


if __name__ == '__main__':
    unittest.main()
