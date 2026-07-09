#!/usr/bin/env python3
"""test_suite_guardian_stage.py — PR-3 tests for the Main-Suite Green Guardian
staged-autonomy machine (spec §5 D3+D4, decisions L3/L4/L8/L10).

Covers (spec §8, PR-3):
  * stage precedence (L3): effective = min(config grant, dial max, penalty cap);
    unreadable config -> Stage 0; the dial always wins;
  * proportional downgrade + evidence reset (L10): regression -> stage-1;
    auto-merged-regression / scope-violation -> hard reset to Stage 0; a tighter
    standing cap is never loosened; penalty_at_run is the evidence floor; a
    graduation epoch bump reconciles a stale penalty away;
  * SHA-bound merge eligibility (L4): the outbox scope gate binds to the CURRENT
    head SHA, passes an in-scope guardian fix, and blocks + downgrades an
    out-of-scope one;
  * diff-gate fail-closed (L4): an unresolved head SHA or unreadable changed-files
    holds the merge rather than allowing it.

All state hits tmp files (explicit path= args or the _bootstrap sandbox root); no
Supabase, DM, gh, or worktree touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_suite_guardian_stage
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import suite_guardian_ledger as ledger  # noqa: E402
import suite_guardian_stage as stage    # noqa: E402


def _write_config(repo_root: Path, *, stage_num, epoch=0, mode='shadow'):
    cfg_dir = repo_root / 'config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    body = {'stage': stage_num, 'stage_epoch': epoch, 'mode': mode}
    (cfg_dir / 'suite-guardian.json').write_text(
        json.dumps(body) + '\n', encoding='utf-8')


class StagePrecedenceTest(unittest.TestCase):
    """L3: effective_stage = min(config grant, dial max, penalty cap)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / 'stage-state.json'

    def tearDown(self):
        self._tmp.cleanup()

    def test_dial_caps_below_config_grant(self):
        # Config grants Stage 3 but the balanced dial permits at most Stage 2.
        _write_config(self.root, stage_num=3)
        self.assertEqual(
            stage.effective_stage(self.root, level='balanced', path=self.state), 2)

    def test_loose_dial_lets_full_config_grant_through(self):
        _write_config(self.root, stage_num=3)
        self.assertEqual(
            stage.effective_stage(self.root, level='loose', path=self.state), 3)

    def test_config_grant_caps_below_dial(self):
        # A generous dial cannot raise the guardian above the human-granted stage.
        _write_config(self.root, stage_num=1)
        self.assertEqual(
            stage.effective_stage(self.root, level='loose', path=self.state), 1)

    def test_unreadable_config_is_stage_zero(self):
        # No config file at all -> Stage 0 (shadow), regardless of a loose dial.
        self.assertEqual(
            stage.effective_stage(self.root, level='loose', path=self.state), 0)

    def test_malformed_config_is_stage_zero(self):
        (self.root / 'config').mkdir(parents=True, exist_ok=True)
        (self.root / 'config' / 'suite-guardian.json').write_text(
            '{ not json', encoding='utf-8')
        self.assertEqual(
            stage.effective_stage(self.root, level='loose', path=self.state), 0)

    def test_penalty_cap_lowers_below_config_and_dial(self):
        _write_config(self.root, stage_num=3)
        stage.apply_downgrade(cause=stage.CAUSE_REGRESSION, current_stage=3,
                              run_seq=5, path=self.state)
        # config=3, dial=loose(3), penalty_cap=2 -> effective 2.
        self.assertEqual(
            stage.effective_stage(self.root, level='loose', path=self.state), 2)

    def test_dial_map_is_the_spec_table(self):
        self.assertEqual(stage.DIAL_MAX_STAGE,
                         {'conservative': 1, 'balanced': 2, 'loose': 3})


class DowngradeEvidenceResetTest(unittest.TestCase):
    """L10: proportional downgrade + evidence reset."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / 'stage-state.json'
        self.ledger = self.root / 'ledger.json'

    def tearDown(self):
        self._tmp.cleanup()

    def test_regression_is_proportional_minus_one(self):
        st = stage.apply_downgrade(cause=stage.CAUSE_REGRESSION, current_stage=2,
                                   run_seq=10, path=self.state)
        self.assertEqual(st['penalty_cap'], 1)
        self.assertEqual(st['penalty_at_run'], 10)

    def test_auto_merged_regression_hard_resets_to_zero(self):
        st = stage.apply_downgrade(cause=stage.CAUSE_AUTO_MERGED_REGRESSION,
                                   current_stage=3, run_seq=4, path=self.state)
        self.assertEqual(st['penalty_cap'], 0)

    def test_scope_violation_hard_resets_to_zero(self):
        st = stage.apply_downgrade(cause=stage.CAUSE_SCOPE_VIOLATION,
                                   current_stage=3, run_seq=4, path=self.state)
        self.assertEqual(st['penalty_cap'], 0)

    def test_a_tighter_standing_cap_is_never_loosened(self):
        stage.apply_downgrade(cause=stage.CAUSE_SCOPE_VIOLATION, current_stage=3,
                              run_seq=4, path=self.state)  # cap 0
        st = stage.apply_downgrade(cause=stage.CAUSE_REGRESSION, current_stage=1,
                                   run_seq=6, path=self.state)  # would be cap 0 too
        self.assertEqual(st['penalty_cap'], 0)
        # The evidence floor still advances on the fresh regression.
        self.assertEqual(st['penalty_at_run'], 6)

    def test_evidence_floor_excludes_pre_downgrade_windows(self):
        # A window closed BEFORE the downgrade must not count toward re-promotion.
        ledger.open_proposal('t.old', run_task_id='r1', poison_test_name='p',
                             path=self.ledger)
        ledger.set_decision('t.old', ledger.DEC_APPROVED, path=self.ledger)
        ledger.mark_dispatched('t.old', 'fix.old', filed_stage=1, path=self.ledger)
        ledger.record_observation('t.old', green_streak=2, poison_present=True,
                                 current_run=2, path=self.ledger)
        ledger.close_windows(2 + ledger.WINDOW_RUNS, path=self.ledger)
        # Downgrade AFTER that window resolved (floor = run 20).
        st = stage.apply_downgrade(cause=stage.CAUSE_REGRESSION, current_stage=2,
                                   run_seq=20, path=self.state)
        floor = stage.evidence_floor_run(st)
        self.assertEqual(floor, 20)
        # Scoped past the floor, the pre-downgrade window is invisible.
        self.assertEqual(
            ledger.count_closed_windows(since_run=floor, path=self.ledger), 0)
        # Unscoped, it still exists.
        self.assertEqual(ledger.count_closed_windows(path=self.ledger), 1)

    def test_graduation_epoch_bump_reconciles_stale_penalty(self):
        _write_config(self.root, stage_num=2, epoch=0)
        stage.apply_downgrade(cause=stage.CAUSE_REGRESSION, current_stage=2,
                              run_seq=3, path=self.state)
        # A deliberate human graduation bumps the epoch -> penalty is superseded.
        _write_config(self.root, stage_num=3, epoch=1)
        cleared = stage.reconcile_epoch(self.root, path=self.state)
        self.assertTrue(cleared)
        st = stage.load_stage_state(path=self.state)
        self.assertIsNone(st['penalty_cap'])
        self.assertEqual(
            stage.effective_stage(self.root, level='loose', path=self.state), 3)


class GraduationEvalTest(unittest.TestCase):
    """D3: pure graduation-evidence evaluation."""

    def test_stage0_to_1_needs_seven_clean_runs(self):
        self.assertIsNone(stage.evaluate_graduation(
            config_stage=0, dial_level='loose', completed_runs=6,
            flip_flop_count=0, closed_windows=0, auto_filed_closed_windows=0,
            same_leak_regressions=0, scope_violations=0))
        cand = stage.evaluate_graduation(
            config_stage=0, dial_level='loose', completed_runs=7,
            flip_flop_count=0, closed_windows=0, auto_filed_closed_windows=0,
            same_leak_regressions=0, scope_violations=0)
        self.assertIsNotNone(cand)
        self.assertEqual(cand['target_stage'], 1)

    def test_flip_flop_blocks_stage0_to_1(self):
        self.assertIsNone(stage.evaluate_graduation(
            config_stage=0, dial_level='loose', completed_runs=9,
            flip_flop_count=1, closed_windows=0, auto_filed_closed_windows=0,
            same_leak_regressions=0, scope_violations=0))

    def test_dial_ceiling_makes_the_card_inert(self):
        # Earned Stage 2 on evidence, but the balanced dial pins at Stage 2 and
        # the target would need loose -> withheld until the dial permits it.
        self.assertIsNone(stage.evaluate_graduation(
            config_stage=2, dial_level='balanced', completed_runs=99,
            flip_flop_count=0, closed_windows=99, auto_filed_closed_windows=99,
            same_leak_regressions=0, scope_violations=0))

    def test_scope_violation_blocks_stage1_to_2(self):
        self.assertIsNone(stage.evaluate_graduation(
            config_stage=1, dial_level='loose', completed_runs=99,
            flip_flop_count=0, closed_windows=99, auto_filed_closed_windows=0,
            same_leak_regressions=0, scope_violations=1))

    def test_stage1_to_2_on_enough_closed_windows(self):
        cand = stage.evaluate_graduation(
            config_stage=1, dial_level='loose', completed_runs=99,
            flip_flop_count=0, closed_windows=stage.GRAD_STAGE1_MIN_WINDOWS,
            auto_filed_closed_windows=0, same_leak_regressions=0,
            scope_violations=0)
        self.assertIsNotNone(cand)
        self.assertEqual(cand['target_stage'], 2)


class ApplyGraduationTest(unittest.TestCase):
    """apply-graduation is the SINGLE config mutator (Check-V pattern)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_raises_stage_and_bumps_epoch(self):
        _write_config(self.root, stage_num=1, epoch=4)
        self.assertTrue(stage.apply_graduation(2, repo_root=self.root))
        cfg = json.loads((self.root / 'config' / 'suite-guardian.json')
                         .read_text('utf-8'))
        self.assertEqual(cfg['stage'], 2)
        self.assertEqual(cfg['stage_epoch'], 5)

    def test_refuses_non_increment(self):
        _write_config(self.root, stage_num=2, epoch=0)
        self.assertFalse(stage.apply_graduation(2, repo_root=self.root))
        self.assertFalse(stage.apply_graduation(1, repo_root=self.root))

    def test_refuses_out_of_range(self):
        _write_config(self.root, stage_num=1)
        self.assertFalse(stage.apply_graduation(4, repo_root=self.root))
        self.assertFalse(stage.apply_graduation(0, repo_root=self.root))


class L4ScopeGateTest(unittest.TestCase):
    """L4: SHA-bound, fail-closed guardian-fix scope gate in outbox_notifier."""

    def setUp(self):
        import outbox_notifier as on
        self.on = on
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = self.root / 'ledger.json'
        self.state = self.root / 'stage-state.json'
        # Point both guardian state files at this test's tmp tree so the gate's
        # ledger join + downgrade write land in isolation.
        self._ledger_patch = mock.patch.object(
            ledger, 'default_ledger_path', return_value=self.ledger)
        self._state_patch = mock.patch.object(
            stage, 'stage_state_path', return_value=self.state)
        self._ledger_patch.start()
        self._state_patch.start()
        # Never touch the queue / DM for real.
        self._queue_patch = mock.patch.object(on, '_queue_remove_pr',
                                              return_value=None)
        self._queue_patch.start()
        import larry_alerts as la
        self._alerts = []
        self._alert_patch = mock.patch.object(
            la, 'append_notification',
            side_effect=lambda *a, **k: self._alerts.append((a, k)))
        self._alert_patch.start()

    def tearDown(self):
        self._ledger_patch.stop()
        self._state_patch.stop()
        self._queue_patch.stop()
        self._alert_patch.stop()
        self._tmp.cleanup()

    def _seed_guardian_fix(self, task_id='fix.abc', test_id='t.victim',
                           filed_stage=2):
        ledger.open_proposal(test_id, run_task_id='r1', poison_test_name='p',
                            path=self.ledger)
        ledger.set_decision(test_id, ledger.DEC_APPROVED, path=self.ledger)
        ledger.mark_dispatched(test_id, task_id, filed_stage=filed_stage,
                              path=self.ledger)
        return task_id, test_id

    def test_non_guardian_pr_is_a_noop(self):
        # No ledger row joins this task_id -> the gate does not apply.
        out = self.on._l4_guardian_scope_gate('o/r', 42, 'unrelated-task',
                                              'url', 0)
        self.assertIsNone(out)

    def test_in_scope_fix_binds_to_head_and_passes(self):
        task_id, _ = self._seed_guardian_fix()
        with mock.patch.object(self.on, '_gh_pr_head_sha', return_value='deadbeef'), \
             mock.patch.object(self.on, '_gh_pr_changed_files',
                               return_value=['scripts/tests/test_a.py',
                                             'scripts/tests/test_b.py']):
            out = self.on._l4_guardian_scope_gate('o/r', 42, task_id, 'url', 0)
        self.assertIsNone(out)  # clean — every path under scripts/tests/**

    def test_out_of_scope_blocks_and_downgrades(self):
        task_id, test_id = self._seed_guardian_fix(filed_stage=2)
        with mock.patch.object(self.on, '_gh_pr_head_sha', return_value='deadbeef'), \
             mock.patch.object(self.on, '_gh_pr_changed_files',
                               return_value=['scripts/tests/test_a.py',
                                             'scripts/outbox_notifier.py']):
            out = self.on._l4_guardian_scope_gate('o/r', 42, task_id, 'url', 0)
        self.assertIsNotNone(out)
        self.assertEqual(out['merge_outcome'], 'held_scope_violation')
        # Ledger stamped the scope violation (hard graduation disqualifier).
        row = next(r for r in ledger.list_by_status(ledger.OPEN, path=self.ledger)
                   if r['test_id'] == test_id)
        self.assertTrue(row['scope_violation'])
        # A Stage-0 downgrade was imposed (L10).
        st = stage.load_stage_state(path=self.state)
        self.assertEqual(st['penalty_cap'], 0)
        self.assertEqual(st['penalty_cause'], stage.CAUSE_SCOPE_VIOLATION)
        # Larry was escalated exactly once (the only paging path here).
        self.assertEqual(len(self._alerts), 1)

    def test_fail_closed_on_unresolved_head_sha(self):
        task_id, _ = self._seed_guardian_fix()
        with mock.patch.object(self.on, '_gh_pr_head_sha', return_value=None), \
             mock.patch.object(self.on, '_gh_pr_changed_files',
                               return_value=['scripts/tests/test_a.py']):
            out = self.on._l4_guardian_scope_gate('o/r', 42, task_id, 'url', 0)
        self.assertIsNotNone(out)
        self.assertEqual(out['merge_outcome'], 'held_scope_fail_closed')

    def test_fail_closed_on_unreadable_changed_files(self):
        task_id, _ = self._seed_guardian_fix()
        with mock.patch.object(self.on, '_gh_pr_head_sha', return_value='deadbeef'), \
             mock.patch.object(self.on, '_gh_pr_changed_files', return_value=None):
            out = self.on._l4_guardian_scope_gate('o/r', 42, task_id, 'url', 0)
        self.assertIsNotNone(out)
        self.assertEqual(out['merge_outcome'], 'held_scope_fail_closed')


if __name__ == '__main__':
    unittest.main()
