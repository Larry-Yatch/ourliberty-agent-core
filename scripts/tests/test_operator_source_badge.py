"""Tests for the operator-queue source-badge provenance contract
(mission_rank.derive_source), slice 8.

The badge is backbone: ONE normalized enum, computed once, so every surface
renders the same "where did this come from" without sniffing per-producer
provenance shapes. These pin the mapping + the native-source passthrough that
lets slice-9 pulse/medic stamp their own source.
"""

from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import mission_rank as mr  # noqa: E402


class DeriveSourceTest(unittest.TestCase):
    def test_mission_proposed_by_mapping(self):
        self.assertEqual(mr.derive_source(
            {'proposed_by': 'heal_orphan_autoregister'}), 'auto')
        self.assertEqual(mr.derive_source({'proposed_by': 'closeout'}),
                         'follow-up')
        self.assertEqual(mr.derive_source({'proposed_by': 'beacon'}), 'beacon')
        self.assertEqual(mr.derive_source(
            {'proposed_by': 'retrospective-author'}), 'beacon')

    def test_capture_origin_mapping(self):
        self.assertEqual(mr.derive_source(
            {'origin': {'source': 'desktop-chat'}}), 'you')
        self.assertEqual(mr.derive_source(
            {'origin': {'source': 'agent'}}), 'beacon')
        # any other non-empty agent origin also falls to beacon (the
        # catch-all: a capture the system made, not Larry).
        self.assertEqual(mr.derive_source(
            {'origin': {'source': 'slack'}}), 'beacon')
        self.assertEqual(mr.derive_source(
            {'origin': {'source': ''}}), 'unknown')  # empty → not a signal

    def test_native_source_passthrough_for_future_producers(self):
        # slice-9 pulse/medic stamp their own source; the brain must honor it.
        self.assertEqual(mr.derive_source({'source': 'pulse'}), 'pulse')
        self.assertEqual(mr.derive_source({'source': 'medic'}), 'medic')
        # native wins over legacy fields
        self.assertEqual(mr.derive_source(
            {'source': 'medic', 'proposed_by': 'heal_orphan_autoregister'}),
            'medic')

    def test_unknown_and_junk_never_raise_or_guess(self):
        self.assertEqual(mr.derive_source({}), 'unknown')
        self.assertEqual(mr.derive_source({'proposed_by': 'mystery'}), 'unknown')
        self.assertEqual(mr.derive_source({'source': 'not-an-enum'}), 'unknown')
        self.assertEqual(mr.derive_source({'origin': 'not-a-dict'}), 'unknown')
        self.assertEqual(mr.derive_source(None), 'unknown')
        self.assertEqual(mr.derive_source('nope'), 'unknown')

    def test_every_mapped_value_is_in_the_closed_enum(self):
        for v in mr._PROPOSED_BY_SOURCE.values():
            self.assertIn(v, mr._SOURCES)


class ScoreCardStampsSourceTest(unittest.TestCase):
    def test_entry_carries_source(self):
        project = {'key': 'factory', 'name': 'Factory', 'stage': 'steady',
                   'weight': 0.2, 'north_star': 'less toil'}
        entry = mr.score_card(
            {'id': 'm1', 'name': 'X', 'repo': 'ourliberty-agent-core',
             'proposed_by': 'closeout'},
            project, llm=None, risk_fn=lambda s: 'safe')
        self.assertEqual(entry['source'], 'follow-up')


if __name__ == '__main__':
    unittest.main()
