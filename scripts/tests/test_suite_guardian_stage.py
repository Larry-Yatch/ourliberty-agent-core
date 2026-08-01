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



class GraduationAskOnceTest(unittest.TestCase):
    """A DECLINED graduation must stay declined.

    `_emit_card` dedups only against a card that is currently PENDING, and
    resolving an approval removes it from pending on REJECT as well as approve —
    so without a persisted marker the card re-files on the next run, and every
    run after that, forever. Declining is a legitimate call (holding a component
    in shadow one more window), so it must not become a nightly nag. The sibling
    `maybe_emit_l8_card` already guards itself this way.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / 'stage-state.json'
        self.emits = []

        def _fake_emit(payload, *, chat_id=None):
            self.emits.append(payload['task_id'])
            return {'id': payload['task_id']}

        p = mock.patch.object(stage, '_emit_card', _fake_emit)
        p.start()
        self.addCleanup(p.stop)

    def _candidate(self, target=1):
        return {'target_stage': target, 'evidence': {'completed_runs': 17},
                'dial_note': None}

    def _decision(self, status):
        """Fake the resolved approval the ask is read back from."""
        return mock.patch.object(
            stage, '_graduation_decision',
            lambda _tid: {'rejected': 'declined',
                          'approved': 'approved'}.get(status))

    def test_second_run_does_not_refile(self):
        first = stage.emit_graduation(self._candidate(), state_path=self.state,
                                      completed_runs=17)
        self.assertIsNotNone(first)
        second = stage.emit_graduation(self._candidate(), state_path=self.state,
                                       completed_runs=17)
        self.assertIsNone(second)          # the nag that used to fire nightly
        self.assertEqual(len(self.emits), 1)

    def test_a_decline_stays_declined_forever(self):
        stage.emit_graduation(self._candidate(), state_path=self.state,
                              completed_runs=17)
        with self._decision('rejected'):
            # far past any cooldown — a no is durable, not merely paced
            for run in (18, 25, 100):
                self.assertIsNone(stage.emit_graduation(
                    self._candidate(), state_path=self.state, completed_runs=run))
        self.assertEqual(len(self.emits), 1)

    def test_approved_but_never_applied_re_asks(self):
        # Approval is only step one: a Forge task must open a config-only PR and
        # that PR must merge before `stage` moves. If it never lands, the
        # guardian is still at Stage 0 — suppressing forever would trade the nag
        # for a SILENT PERMANENT STALL on the one component whose whole defect is
        # that it cannot tell anyone anything.
        stage.emit_graduation(self._candidate(), state_path=self.state,
                              completed_runs=17)
        with self._decision('approved'):
            # inside the cooldown: quiet, the PR may simply still be in flight
            self.assertIsNone(stage.emit_graduation(
                self._candidate(), state_path=self.state, completed_runs=18))
            # past it: speak up again
            self.assertIsNotNone(stage.emit_graduation(
                self._candidate(), state_path=self.state, completed_runs=20))
        self.assertEqual(len(self.emits), 2)

    def test_expired_is_not_a_decline(self):
        # `expired` means the card aged out / was reconciled away — Larry never
        # answered. The proposal loop's lookup folds expired into 'rejected';
        # doing that here would silence a graduation he never even saw.
        stage.emit_graduation(self._candidate(), state_path=self.state,
                              completed_runs=17)
        with self._decision('expired'):    # -> None (undecided), not 'declined'
            self.assertIsNotNone(stage.emit_graduation(
                self._candidate(), state_path=self.state, completed_runs=21))

    def test_corrupt_marker_does_not_kill_graduation(self):
        # A partial write / hand-edit / older schema must read as "no ask on
        # record" and re-ask, not raise into a caller that swallows exceptions
        # and thereby disables graduation permanently and silently.
        for bad in ('nonsense', ['a'], 42):
            st = stage.load_stage_state(path=self.state)
            st['graduation_asked'] = bad
            stage.save_stage_state(st, path=self.state)
            self.assertIsNotNone(stage.emit_graduation(
                self._candidate(), state_path=self.state, completed_runs=17))

    def test_decision_lookup_maps_statuses(self):
        # `expired` must read as UNDECIDED, not declined — the distinction the
        # proposal loop's own lookup collapses.
        import beacon_approval_handler as ah
        for status, want in (('expired', None), ('rejected', 'declined'),
                             ('approved', 'approved'), ('modified', 'approved'),
                             ('pending', None)):
            state = {'pending': [], 'history': [{'id': 'x', 'status': status}]}
            with mock.patch.object(ah, 'load_state', lambda _s=state: _s):
                self.assertEqual(stage._graduation_decision('x'), want, status)

    def test_unknown_id_is_undecided(self):
        import beacon_approval_handler as ah
        with mock.patch.object(ah, 'load_state',
                               lambda: {'pending': [], 'history': []}):
            self.assertIsNone(stage._graduation_decision('never-asked'))

    def test_newest_decision_wins_over_a_reused_id(self):
        # THE SHADOWING BUG. `_graduation_task_id` is deterministic and the
        # re-ask reuses it, so history can hold TWO entries under one id.
        # `find_by_id_any_state` returns the FIRST (oldest) match, so a decline
        # that FOLLOWS an approval was invisible and the nag came back.
        # Exercises the REAL lookup — the other tests fake `_graduation_decision`
        # wholesale, which is exactly why this slipped through.
        import beacon_approval_handler as ah
        dup = stage._graduation_task_id(1)
        state = {'pending': [], 'history': [
            {'id': dup, 'status': 'approved'},   # ask #1: yes, never applied
            {'id': dup, 'status': 'rejected'},   # ask #2: no
        ]}
        with mock.patch.object(ah, 'load_state', lambda: state):
            self.assertEqual(stage._graduation_decision(dup), 'declined')

    def test_a_live_pending_ask_outranks_history(self):
        import beacon_approval_handler as ah
        dup = stage._graduation_task_id(1)
        state = {'pending': [{'id': dup, 'status': 'pending'}],
                 'history': [{'id': dup, 'status': 'rejected'}]}
        with mock.patch.object(ah, 'load_state', lambda: state):
            self.assertIsNone(stage._graduation_decision(dup))

    def test_decline_after_a_re_ask_is_durable_end_to_end(self):
        # Full sequence through the REAL lookup: ask -> approve -> PR never
        # lands -> re-ask -> DECLINE -> silent from then on.
        import beacon_approval_handler as ah
        dup = stage._graduation_task_id(1)
        hist: list = []
        state = {'pending': [], 'history': hist}
        with mock.patch.object(ah, 'load_state', lambda: state):
            self.assertIsNotNone(stage.emit_graduation(
                self._candidate(), state_path=self.state, completed_runs=18))
            hist.append({'id': dup, 'status': 'approved'})     # yes, unapplied
            self.assertIsNotNone(stage.emit_graduation(        # re-ask past cooldown
                self._candidate(), state_path=self.state, completed_runs=22))
            hist.append({'id': dup, 'status': 'rejected'})     # no
            for run in (26, 40, 200):
                self.assertIsNone(stage.emit_graduation(
                    self._candidate(), state_path=self.state, completed_runs=run))
        self.assertEqual(len(self.emits), 2)

    def test_decision_lookup_is_fail_safe(self):
        import beacon_approval_handler as ah
        self.assertIsNone(stage._graduation_decision(None))

        def _boom():
            raise RuntimeError('approval store unreadable')

        with mock.patch.object(ah, 'load_state', _boom):
            self.assertIsNone(stage._graduation_decision('x'))
        # A malformed store must read as undecided, not raise.
        with mock.patch.object(ah, 'load_state', lambda: ['not', 'a', 'dict']):
            self.assertIsNone(stage._graduation_decision('x'))
        with mock.patch.object(ah, 'load_state',
                               lambda: {'pending': None, 'history': None}):
            self.assertIsNone(stage._graduation_decision('x'))

    def test_failed_emit_does_not_consume_the_ask(self):
        # deps unavailable / Supabase down => _emit_card returns None. That must
        # NOT burn the one ask, or a transient outage silences graduation forever.
        with mock.patch.object(stage, '_emit_card', lambda payload, *, chat_id=None: None):
            self.assertIsNone(stage.emit_graduation(self._candidate(),
                                                    state_path=self.state))
        self.assertIsNotNone(stage.emit_graduation(self._candidate(),
                                                   state_path=self.state))

    def test_a_downgrade_re_asks(self):
        # The evidence floor moving (L10 reset) changes what Larry would be
        # judging, so a fresh ask is legitimate.
        stage.emit_graduation(self._candidate(), state_path=self.state)
        st = stage.load_stage_state(path=self.state)
        st['penalty_at_run'] = 42          # downgrade lands, floor moves
        stage.save_stage_state(st, path=self.state)
        self.assertIsNotNone(stage.emit_graduation(self._candidate(),
                                                   state_path=self.state))
        self.assertEqual(len(self.emits), 2)

    def test_a_different_target_stage_is_its_own_ask(self):
        stage.emit_graduation(self._candidate(1), state_path=self.state)
        self.assertIsNotNone(stage.emit_graduation(self._candidate(2),
                                                   state_path=self.state))


class L10RegressionWiringTest(unittest.TestCase):
    """L10 END-TO-END: a resolved fix whose victim goes red again must actually
    reach `mark_regressed`, the counters, the downgrade, and the graduation bar.

    These are WIRING tests on purpose. The pre-existing L10 tests above all call
    `apply_downgrade` with the cause passed in as a literal and feed
    `evaluate_graduation` literal tallies — so every one of them stayed green
    while NOTHING in the guardian ever called `mark_regressed`. The counter was
    structurally pinned at 0 and the Stage-2 bar's regression clause could not
    fire. Asserting the plumbing, not just the units, is the whole point here.

    Every state file is explicit tmp: `apply_downgrade` otherwise writes the
    process-wide sandbox stage state and would leak a penalty_cap into the
    classes above."""

    VICTIM = 'test_mod.Case.test_victim'
    POISON = 'test_poison_victim'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / 'stage-state.json'
        self.ledger = self.root / 'ledger.json'
        self.notices = []

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def _resolved_row(self, *, filed_stage=1, resolved_run=10):
        """Drive a row through the REAL lifecycle to RESOLVED — no hand-built
        dicts, so this breaks if the lifecycle itself changes."""
        ledger.open_proposal(self.VICTIM, run_task_id='run-1',
                             poison_test_name=self.POISON, path=self.ledger)
        ledger.set_decision(self.VICTIM, ledger.DEC_APPROVED, path=self.ledger)
        ledger.mark_dispatched(self.VICTIM, 'fix-task-1',
                               filed_stage=filed_stage, path=self.ledger)
        ledger.record_observation(self.VICTIM, green_streak=2, poison_present=True,
                                  current_run=resolved_run, path=self.ledger)
        rows = ledger.list_by_status(ledger.RESOLVED, path=self.ledger)
        self.assertEqual(len(rows), 1, 'fixture failed to reach RESOLVED')
        return rows[0]

    def _registry(self, *, victim_red, poison_red=False):
        return {'_meta': {'completed_runs': 20}, 'tests': {
            self.VICTIM: {'consecutive_red_runs': 3 if victim_red else 0},
            f'test_mod.Case.{self.POISON}': {
                'consecutive_red_runs': 2 if poison_red else 0},
        }}

    def _detect(self, registry, *, effective_stage=1, current_run=20):
        import main_suite_guardian as g
        return g.detect_regressions(
            registry, ledger_path=self.ledger, effective_stage=effective_stage,
            current_run=current_run, stage_state_path=self.state,
            notify_downgrade=lambda **kw: self.notices.append(kw),
        )

    # -- the wiring -------------------------------------------------------
    def test_same_leak_regression_reaches_counter_and_downgrades(self):
        self._resolved_row()
        out = self._detect(self._registry(victim_red=True, poison_red=True))

        self.assertEqual([r['attribution'] for r in out], ['same-leak'])
        # The counter the Stage-2 bar reads — pinned at 0 before this change.
        self.assertEqual(ledger.count_same_leak_regressions(path=self.ledger), 1)
        # The downgrade actually landed in stage state.
        st = stage.load_stage_state(path=self.state)
        self.assertEqual(st.get('penalty_cap'), 0)
        self.assertEqual(st.get('penalty_cause'), stage.CAUSE_REGRESSION)
        # And Larry hears about it (L10: briefings channel, not the Approvals tab).
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0]['cause'], stage.CAUSE_REGRESSION)

    def test_same_leak_regression_blocks_the_stage_2_card(self):
        self._resolved_row()
        self._detect(self._registry(victim_red=True, poison_red=True))
        # Feed the REAL counter into the REAL bar — the join that never existed.
        self.assertIsNone(stage.evaluate_graduation(
            config_stage=1, dial_level='balanced', completed_runs=99,
            flip_flop_count=0, closed_windows=99, auto_filed_closed_windows=0,
            same_leak_regressions=ledger.count_same_leak_regressions(
                path=self.ledger),
            scope_violations=ledger.count_scope_violations(path=self.ledger),
        ))

    def test_regressed_row_can_never_bank_its_window(self):
        self._resolved_row(resolved_run=1)
        self._detect(self._registry(victim_red=True, poison_red=True))
        # 14+ runs have passed, so this WOULD close were it not regressed.
        ledger.close_windows(99, path=self.ledger)
        self.assertEqual(ledger.count_closed_windows(path=self.ledger), 0)

    def test_auto_filed_fix_hard_resets_to_stage_zero(self):
        self._resolved_row(filed_stage=2)
        out = self._detect(self._registry(victim_red=True, poison_red=True),
                           effective_stage=2)
        self.assertEqual(out[0]['cause'], stage.CAUSE_AUTO_MERGED_REGRESSION)
        self.assertEqual(
            stage.load_stage_state(path=self.state).get('penalty_cap'), 0)

    # -- the other direction (a guarantee test must exercise both) ---------
    def test_new_polluter_is_recorded_but_costs_nothing(self):
        self._resolved_row()
        out = self._detect(self._registry(victim_red=True, poison_red=False))

        self.assertEqual([r['attribution'] for r in out], ['new-polluter'])
        self.assertFalse(out[0]['downgraded'])
        # Recorded as regressed (so it cannot bank a window) but NOT counted
        # against the fix, and no penalty — L10's explicit carve-out.
        self.assertEqual(ledger.count_same_leak_regressions(path=self.ledger), 0)
        self.assertIsNone(
            stage.load_stage_state(path=self.state).get('penalty_cap'))
        self.assertEqual(self.notices, [])

    def test_victim_still_green_is_not_a_regression(self):
        self._resolved_row()
        self.assertEqual(self._detect(self._registry(victim_red=False)), [])
        self.assertEqual(ledger.count_same_leak_regressions(path=self.ledger), 0)

    def test_regression_is_counted_once_not_every_night(self):
        self._resolved_row()
        reg = self._registry(victim_red=True, poison_red=True)
        self.assertEqual(len(self._detect(reg)), 1)
        # Still red the next night — must not re-mark or re-downgrade.
        self.assertEqual(self._detect(reg), [])
        self.assertEqual(ledger.count_same_leak_regressions(path=self.ledger), 1)
        self.assertEqual(len(self.notices), 1)


if __name__ == '__main__':
    unittest.main()
