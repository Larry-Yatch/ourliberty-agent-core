#!/usr/bin/env python3
"""Tests for cycle_tier_state (PR-β tier state machine).

Covers: read defaults, tier transitions (advance / reset / record_iter
clean+dirty), atomic-write semantics, corruption recovery.

Run::

    cd ~/agent-core && python3 -m pytest scripts/tests/test_cycle_tier_state.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import cycle_tier_state as cts  # noqa: E402


class _TierStateTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prior_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        # Reload to pick up new env (also unsets any stale cached path).
        importlib.reload(cts)

    def tearDown(self):
        if self._prior_root is not None:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prior_root
        else:
            del os.environ['OURLIBERTY_AGENTS_ROOT']
        importlib.reload(cts)

    def _state_file(self) -> Path:
        return self.tmp / cts.STATE_REL


class TestReadDefaults(_TierStateTestBase):

    def test_missing_file_returns_default_and_creates_it(self):
        state = cts.read_tier_state()
        self.assertEqual(state['tier'], 1)
        self.assertEqual(state['consecutive_clean'], 0)
        self.assertIsNone(state['last_signal_at'])
        self.assertTrue(self._state_file().exists())

    def test_corrupt_json_falls_back_to_default(self):
        self._state_file().parent.mkdir(parents=True, exist_ok=True)
        self._state_file().write_text('{not-json')
        with mock.patch.object(cts, '_dm_corruption_once') as dm:
            state = cts.read_tier_state()
        self.assertEqual(state['tier'], 1)
        dm.assert_called_once()
        # File should be rewritten with valid defaults.
        rewritten = json.loads(self._state_file().read_text())
        self.assertEqual(rewritten['tier'], 1)

    def test_schema_invalid_falls_back_to_default(self):
        self._state_file().parent.mkdir(parents=True, exist_ok=True)
        self._state_file().write_text(json.dumps({'tier': 'huge',
                                                  'consecutive_clean': -1}))
        with mock.patch.object(cts, '_dm_corruption_once') as dm:
            state = cts.read_tier_state()
        self.assertEqual(state['tier'], 1)
        dm.assert_called_once()


class TestAdvanceTier(_TierStateTestBase):

    def test_advance_promotes_one_step(self):
        new = cts.advance_tier()
        self.assertEqual(new, 2)
        self.assertEqual(cts.read_tier_state()['tier'], 2)

    def test_advance_caps_at_max_tier(self):
        cts.advance_tier()  # 1->2
        cts.advance_tier()  # 2->3
        cts.advance_tier()  # 3->3 (cap)
        self.assertEqual(cts.read_tier_state()['tier'], 3)

    def test_advance_zeroes_consecutive_clean(self):
        cts.record_iter_result(True)
        cts.advance_tier()
        self.assertEqual(cts.read_tier_state()['consecutive_clean'], 0)


class TestResetToTier1(_TierStateTestBase):

    def test_reset_from_tier_3_back_to_tier_1(self):
        cts.advance_tier()
        cts.advance_tier()
        self.assertEqual(cts.read_tier_state()['tier'], 3)
        state = cts.reset_to_tier_1()
        self.assertEqual(state['tier'], 1)
        self.assertEqual(state['consecutive_clean'], 0)
        self.assertIsNotNone(state['last_signal_at'])

    def test_reset_stamps_last_signal_at(self):
        state = cts.reset_to_tier_1()
        self.assertIsNotNone(state['last_signal_at'])
        # Subsequent read sees the stamp persisted.
        self.assertEqual(
            cts.read_tier_state()['last_signal_at'],
            state['last_signal_at'],
        )


class TestRecordIterResult(_TierStateTestBase):

    def test_dirty_iter_resets_to_tier_1(self):
        cts.advance_tier()
        state = cts.record_iter_result(False)
        self.assertEqual(state['tier'], 1)
        self.assertEqual(state['consecutive_clean'], 0)

    def test_clean_iter_increments_consecutive(self):
        state = cts.record_iter_result(True)
        self.assertEqual(state['consecutive_clean'], 1)
        state = cts.record_iter_result(True)
        self.assertEqual(state['consecutive_clean'], 2)

    def test_three_clean_iters_promote_tier(self):
        for _ in range(3):
            cts.record_iter_result(True)
        state = cts.read_tier_state()
        self.assertEqual(state['tier'], 2)
        # Counter reset on promotion.
        self.assertEqual(state['consecutive_clean'], 0)

    def test_no_promotion_past_tier_3(self):
        # Get to tier 3 first.
        for _ in range(3):
            cts.record_iter_result(True)
        for _ in range(3):
            cts.record_iter_result(True)
        self.assertEqual(cts.read_tier_state()['tier'], 3)
        # Three more clean iters: should NOT promote past 3.
        for _ in range(3):
            cts.record_iter_result(True)
        self.assertEqual(cts.read_tier_state()['tier'], 3)


class TestAtomicWrite(_TierStateTestBase):

    def test_write_uses_tmp_replace(self):
        # No .tmp leftover after a successful write.
        cts.advance_tier()
        leftovers = list(self._state_file().parent.glob('*.tmp'))
        self.assertEqual(leftovers, [],
                         f'expected no .tmp left over; saw {leftovers!r}')

    def test_partial_write_safety(self):
        """If the underlying tmp file is left around from a crashed write,
        a subsequent successful write must replace the canonical file
        atomically without surfacing the tmp content as state."""
        path = self._state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Simulate a stale tmp from a prior crashed write.
        (path.with_suffix(path.suffix + '.tmp')).write_text('crashed garbage')
        cts.advance_tier()
        # Final file is well-formed JSON with our tier value.
        data = json.loads(path.read_text())
        self.assertEqual(data['tier'], 2)


if __name__ == '__main__':
    unittest.main()
