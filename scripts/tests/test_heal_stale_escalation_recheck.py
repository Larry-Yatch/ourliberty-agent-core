#!/usr/bin/env python3
"""Tests for heal_stale_escalation_recheck.

Covers the pure surface (PR-url parsing, ledger CRUD, check/compare
predicates) and every rung of the decision ladder with gh shelled-out calls
mocked. No subprocess is ever live here — `gh_json` and `update_branch` are
monkey-patched, and the destructive verb additionally sits behind `gh_write`,
which refuses under test.

The RSDPM #111 regression (the incident this healer exists for) is pinned
end-to-end in StaleGateSignatureTest.

Run:
    cd ~/agent-core && python3 -m unittest \
        scripts.tests.test_heal_stale_escalation_recheck
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_stale_escalation_recheck as h  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test, because the
    module's LOG_FILE / STATE_FILE / HEARTBEAT_FILE / KILL_SWITCH are bound at
    import time and would otherwise write into prod `/home/larry/agents`."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self._tmp, sub), exist_ok=True)
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp
        importlib.reload(h)

    def tearDown(self):
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        importlib.reload(h)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()


# -------------------- pure helpers --------------------

class ParsePrUrlTest(unittest.TestCase):
    def test_parses_a_well_formed_pr_url(self):
        self.assertEqual(
            h.parse_pr_url('https://github.com/Larry-Yatch/RSDPM/pull/111'),
            ('Larry-Yatch/RSDPM', 111),
        )

    def test_tolerates_a_trailing_slash(self):
        self.assertEqual(
            h.parse_pr_url('https://github.com/Larry-Yatch/RSDPM/pull/111/'),
            ('Larry-Yatch/RSDPM', 111),
        )

    def test_rejects_non_pr_and_malformed_urls(self):
        for bad in (
            'https://github.com/Larry-Yatch/RSDPM/issues/111',  # not a PR
            'https://github.com/Larry-Yatch/RSDPM/pull/abc',    # non-numeric
            'https://github.com/RSDPM/pull/111',                # too short
            'not-a-url',
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(h.parse_pr_url(bad))


class PrChecksFailingTest(unittest.TestCase):
    def test_true_on_a_hard_failure(self):
        pr = {'statusCheckRollup': [
            {'conclusion': 'SUCCESS'}, {'conclusion': 'FAILURE'},
        ]}
        self.assertTrue(h.pr_checks_failing(pr))

    def test_false_when_everything_passed(self):
        pr = {'statusCheckRollup': [
            {'conclusion': 'SUCCESS'}, {'conclusion': 'SUCCESS'},
        ]}
        self.assertFalse(h.pr_checks_failing(pr))

    def test_pending_is_not_a_failure(self):
        """An in-flight check must not read as red — that would let the healer
        act on a PR whose verdict is not in yet."""
        pr = {'statusCheckRollup': [{'state': 'PENDING'}]}
        self.assertFalse(h.pr_checks_failing(pr))

    def test_missing_or_malformed_rollup_is_not_a_failure(self):
        self.assertFalse(h.pr_checks_failing({}))
        self.assertFalse(h.pr_checks_failing({'statusCheckRollup': 'nope'}))


class LedgerTest(_IsolatedAgentsRoot):
    def test_missing_file_degrades_to_empty(self):
        self.assertEqual(h.load_ledger(), {'actioned': {}})

    def test_malformed_file_degrades_to_empty_rather_than_raising(self):
        h.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        h.STATE_FILE.write_text('{not json')
        self.assertEqual(h.load_ledger(), {'actioned': {}})

    def test_round_trip(self):
        ledger = {'actioned': {'r#1@abc': {'ts': 'now'}}}
        h.save_ledger(ledger)
        self.assertEqual(h.load_ledger()['actioned'], ledger['actioned'])

    def test_key_is_scoped_to_repo_pr_and_head(self):
        """Two PRs, or two heads of one PR, must never share a ledger key —
        otherwise actioning one silently suppresses the other."""
        a = h.ledger_key('o/r', 111, 'a' * 40)
        b = h.ledger_key('o/r', 112, 'a' * 40)
        c = h.ledger_key('o/r', 111, 'b' * 40)
        self.assertEqual(len({a, b, c}), 3)


class BaseBranchIsGreenTest(_IsolatedAgentsRoot):
    def _with_runs(self, runs):
        return patch.object(h, 'gh_json', return_value=runs)

    def test_green_on_latest_completed_success(self):
        with self._with_runs([{'status': 'completed', 'conclusion': 'success'}]):
            self.assertTrue(h.base_branch_is_green('o/r', 'main'))

    def test_not_green_on_latest_completed_failure(self):
        with self._with_runs([{'status': 'completed', 'conclusion': 'failure'}]):
            self.assertFalse(h.base_branch_is_green('o/r', 'main'))

    def test_skips_in_flight_runs_and_reads_the_last_verdict(self):
        with self._with_runs([
            {'status': 'in_progress', 'conclusion': None},
            {'status': 'completed', 'conclusion': 'success'},
        ]):
            self.assertTrue(h.base_branch_is_green('o/r', 'main'))

    def test_unreadable_response_is_not_green(self):
        """Positive evidence only: a probe that cannot answer keeps the card."""
        with self._with_runs(None):
            self.assertFalse(h.base_branch_is_green('o/r', 'main'))
        with self._with_runs([]):
            self.assertFalse(h.base_branch_is_green('o/r', 'main'))


class BranchIsBehindBaseTest(_IsolatedAgentsRoot):
    def test_behind_and_diverged_both_count_as_behind(self):
        for status in ('behind', 'diverged'):
            with self.subTest(status=status):
                with patch.object(h, 'gh_json', return_value={'status': status}):
                    self.assertTrue(
                        h.branch_is_behind_base('o/r', 'main', 'a' * 40))

    def test_ahead_or_identical_is_not_behind(self):
        for status in ('ahead', 'identical'):
            with self.subTest(status=status):
                with patch.object(h, 'gh_json', return_value={'status': status}):
                    self.assertFalse(
                        h.branch_is_behind_base('o/r', 'main', 'a' * 40))

    def test_unreadable_compare_is_not_behind(self):
        with patch.object(h, 'gh_json', return_value=None):
            self.assertFalse(h.branch_is_behind_base('o/r', 'main', 'a' * 40))


# -------------------- card scanning --------------------

CARD_HEAD = 'f2b287ea0a5b2398445d88ba86e71a4bdefd05d2'


def _card(card_id='mirror-review-pr-RSDPM-111-f2b287ea',
          pr_url='https://github.com/Larry-Yatch/RSDPM/pull/111',
          head_sha=CARD_HEAD, with_target=True):
    entry = {'id': card_id, 'dispatch_payload': {}}
    if with_target:
        entry['dispatch_payload']['recheck_target'] = {
            'task_id': 'pr-RSDPM-111', 'pr_url': pr_url,
            'target_repo': 'RSDPM', 'head_sha': head_sha,
            'round': 1, 'replan_count': 0,
        }
    return entry


class CandidateCardsTest(_IsolatedAgentsRoot):
    def _with_pending(self, pending):
        return patch.object(h.approval, 'load_state',
                            return_value={'pending': pending})

    def test_picks_up_a_well_formed_session_less_card(self):
        with self._with_pending([_card()]):
            got = h.candidate_cards()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['repo'], 'Larry-Yatch/RSDPM')
        self.assertEqual(got[0]['pr_number'], 111)
        self.assertEqual(got[0]['head_sha'], CARD_HEAD)

    def test_includes_promoted_cards_that_carry_a_coordinate(self):
        """agent-core #1058: heal_unregistered_approval's promoted stranded
        escalations are the SAME decision class; with a recheck_target they
        enter the ladder — but flagged for rule 1 only (see full_ladder)."""
        with self._with_pending([_card(card_id='unreg-approval-abc123')]):
            got = h.candidate_cards()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['card_id'], 'unreg-approval-abc123')
        self.assertFalse(got[0]['full_ladder'])

    def test_mirror_review_cards_get_the_full_ladder(self):
        with self._with_pending([_card()]):
            got = h.candidate_cards()
        self.assertTrue(got[0]['full_ladder'])

    def test_promoted_prefix_matches_the_promoters_own_constant(self):
        """The prefix is imported, not retyped — a promoter rename must not
        silently re-hide promoted cards from this ladder."""
        import heal_unregistered_approval as hua
        self.assertEqual(h.PROMOTED_CARD_PREFIX,
                         f'{hua.PROMOTED_TASK_PREFIX}-')
        self.assertIn(h.PROMOTED_CARD_PREFIX, h.CARD_ID_PREFIXES)

    def test_ignores_promoted_cards_without_a_coordinate(self):
        """A larry-alert promotion (or a pre-fix promoted card) carries no
        recheck_target — out of scope, exactly like a coordinate-less
        mirror-review card."""
        with self._with_pending([
            _card(card_id='unreg-approval-abc123', with_target=False),
        ]):
            self.assertEqual(h.candidate_cards(), [])

    def test_ignores_cards_that_are_not_mirror_review(self):
        with self._with_pending([_card(card_id='graduation-tpl-widen-001')]):
            self.assertEqual(h.candidate_cards(), [])

    def test_ignores_a_card_with_no_recheck_target(self):
        """Without a structured coordinate we would be guessing the PR. Skip
        rather than regex it back out of the prose summary."""
        with self._with_pending([_card(with_target=False)]):
            self.assertEqual(h.candidate_cards(), [])

    def test_ignores_a_card_whose_pr_url_will_not_parse(self):
        with self._with_pending([_card(pr_url='https://example.com/nope')]):
            self.assertEqual(h.candidate_cards(), [])

    def test_unreadable_approval_state_yields_no_candidates(self):
        with patch.object(h.approval, 'load_state',
                          side_effect=RuntimeError('boom')):
            self.assertEqual(h.candidate_cards(), [])


# -------------------- the decision ladder --------------------

class _LadderBase(_IsolatedAgentsRoot):
    def setUp(self):
        super().setUp()
        self.retired: list[tuple[str, str]] = []
        self.updated: list[tuple[str, int]] = []

        def fake_resolve(card_id, status, note=''):
            self.retired.append((card_id, note))
            return {'id': card_id, 'status': status}

        def fake_update(repo, pr_number):
            self.updated.append((repo, pr_number))
            return True, 'updated'

        self._p1 = patch.object(h.approval, 'resolve', side_effect=fake_resolve)
        self._p2 = patch.object(h, 'update_branch', side_effect=fake_update)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    def run_ladder(self, pr, *, base_green=True, behind=True, ledger=None,
                   card_id='mirror-review-pr-RSDPM-111-f2b287ea',
                   full_ladder=True):
        card = {
            'card_id': card_id,
            'repo': 'Larry-Yatch/RSDPM', 'pr_number': 111,
            'head_sha': CARD_HEAD,
            'pr_url': 'https://github.com/Larry-Yatch/RSDPM/pull/111',
            'full_ladder': full_ladder,
        }
        with patch.object(h, 'probe_pr', return_value=pr), \
                patch.object(h, 'base_branch_is_green', return_value=base_green), \
                patch.object(h, 'branch_is_behind_base', return_value=behind):
            return h.evaluate(card, ledger or {'actioned': {}}, False, [3])


def _open_pr(**over):
    pr = {
        'state': 'OPEN', 'isDraft': False, 'mergeStateStatus': 'UNSTABLE',
        'headRefOid': CARD_HEAD, 'baseRefName': 'main',
        'statusCheckRollup': [{'conclusion': 'FAILURE'}],
    }
    pr.update(over)
    return pr


class TerminalPrTest(_LadderBase):
    def test_merged_pr_retires_the_card(self):
        tag = self.run_ladder(_open_pr(state='MERGED'))
        self.assertEqual(tag, 'retired-merged')
        self.assertEqual(len(self.retired), 1)
        self.assertIn('moot', self.retired[0][1])
        self.assertEqual(self.updated, [])

    def test_closed_pr_retires_the_card(self):
        """Nothing else in the system retires a card for a CLOSED PR."""
        tag = self.run_ladder(_open_pr(state='CLOSED'))
        self.assertEqual(tag, 'retired-closed')
        self.assertEqual(self.updated, [])

    def test_probe_failure_leaves_the_card_alone(self):
        tag = self.run_ladder(None)
        self.assertEqual(tag, 'probe-failed')
        self.assertEqual(self.retired, [])
        self.assertEqual(self.updated, [])


class SupersededHeadTest(_LadderBase):
    def test_moved_head_retires_as_superseded_without_acting(self):
        tag = self.run_ladder(_open_pr(headRefOid='0ed52438' + 'f' * 32))
        self.assertEqual(tag, 'retired-superseded')
        self.assertEqual(self.updated, [], 'must not update an already-moved head')
        self.assertIn('superseded', self.retired[0][1])


class PromotedCardLadderDepthTest(_LadderBase):
    """Promoted stranded-escalation cards get rule 1 ONLY.

    Review round 1 applied the whole ladder to them. That automates the very
    stranding the sibling fix exists to prevent: retiring a promoted card is
    unrecoverable (its for-Larry record was cleared at promote time and
    `_healer_task_registered` matches the retired card in history, so it can
    never be re-promoted), and the modal promoted card comes from the
    notifier's ACTION_NEEDED bucket, which cuts no replacement card at all.
    Worse, rule 2 would fire on the FIRST tick for any PR pushed since its
    escalation, because the promoted coordinate carries the escalation-era
    head — so Larry's only decision surface would vanish before he saw it.
    """

    PROMOTED = 'unreg-approval-de9cda4efdbd'

    def _run(self, pr, **kw):
        return self.run_ladder(pr, card_id=self.PROMOTED, full_ladder=False,
                               **kw)

    def test_depth_is_derived_from_the_id_not_the_passed_flag(self):
        # The flag is observability only; a card dict lacking it (or lying)
        # must not change which rungs run, in either direction.
        self.assertEqual(
            self.run_ladder(_open_pr(), card_id=self.PROMOTED,
                            full_ladder=True),
            'left-pending-promoted-card')
        self.assertEqual(
            self.run_ladder(_open_pr(), full_ladder=False), 'acted')

    def test_moved_head_does_not_retire_a_promoted_card(self):
        tag = self._run(_open_pr(headRefOid='0ed52438' + 'f' * 32))
        self.assertEqual(tag, 'left-pending-promoted-card')
        self.assertEqual(self.retired, [], 'the only decision surface survives')
        self.assertEqual(self.updated, [])

    def test_stale_gate_does_not_act_on_a_promoted_card(self):
        # The full-ladder shape (base green, behind, checks red) that would
        # otherwise update-branch — and whose head move then feeds rule 2.
        tag = self._run(_open_pr())
        self.assertEqual(tag, 'left-pending-promoted-card')
        self.assertEqual(self.updated, [])
        self.assertEqual(self.retired, [])

    def test_rule_one_still_retires_a_promoted_card(self):
        # A terminal PR genuinely moots the decision — safe for every class,
        # and it is what keeps merged promoted cards off the tab.
        for state, tag in (('MERGED', 'retired-merged'),
                           ('CLOSED', 'retired-closed')):
            with self.subTest(state=state):
                self.retired.clear()
                self.assertEqual(self._run(_open_pr(state=state)), tag)
                self.assertEqual(len(self.retired), 1)

    def test_same_shapes_still_act_on_a_mirror_review_card(self):
        # The narrowing must be scoped to the promoted class, not global.
        self.assertEqual(
            self.run_ladder(_open_pr(headRefOid='0ed52438' + 'f' * 32)),
            'retired-superseded')
        self.assertEqual(self.run_ladder(_open_pr()), 'acted')


class StaleGateSignatureTest(_LadderBase):
    """The RSDPM #111 shape: OPEN at the card's head, own checks red, base
    green, branch behind. This is the only rung that takes an action."""

    def test_acts_then_retires(self):
        tag = self.run_ladder(_open_pr())
        self.assertEqual(tag, 'acted')
        self.assertEqual(self.updated, [('Larry-Yatch/RSDPM', 111)])
        self.assertEqual(len(self.retired), 1)
        self.assertIn('blocker cleared', self.retired[0][1])

    def test_records_the_ledger_key_so_it_cannot_repeat(self):
        ledger = {'actioned': {}}
        card = {
            'card_id': 'c', 'repo': 'Larry-Yatch/RSDPM', 'pr_number': 111,
            'head_sha': CARD_HEAD, 'pr_url': 'u',
        }
        with patch.object(h, 'probe_pr', return_value=_open_pr()), \
                patch.object(h, 'base_branch_is_green', return_value=True), \
                patch.object(h, 'branch_is_behind_base', return_value=True):
            h.evaluate(card, ledger, False, [3])
        self.assertIn(h.ledger_key('Larry-Yatch/RSDPM', 111, CARD_HEAD),
                      ledger['actioned'])

    def test_an_already_actioned_head_is_not_actioned_twice(self):
        key = h.ledger_key('Larry-Yatch/RSDPM', 111, CARD_HEAD)
        tag = self.run_ladder(_open_pr(), ledger={'actioned': {key: {}}})
        self.assertEqual(tag, 'left-pending-already-actioned')
        self.assertEqual(self.updated, [])

    def test_a_failed_update_does_not_retire_the_card(self):
        """If the act fails the card must survive — retiring it would strand
        the PR with no decision and no card."""
        with patch.object(h, 'update_branch', return_value=(False, 'nope')):
            tag = self.run_ladder(_open_pr())
        self.assertEqual(tag, 'act-failed')
        self.assertEqual(self.retired, [])


class StaleGateGuardsTest(_LadderBase):
    """Each guard, removed one at a time, must block the action. These are the
    mutation tests: delete any single clause in rule 3 and one of these fails."""

    def test_draft_pr_is_left_alone(self):
        tag = self.run_ladder(_open_pr(isDraft=True))
        self.assertEqual(tag, 'left-pending-draft')
        self.assertEqual(self.updated, [])

    def test_conflicted_pr_is_left_alone(self):
        for status in ('DIRTY', 'CONFLICTING'):
            with self.subTest(status=status):
                self.updated.clear()
                tag = self.run_ladder(_open_pr(mergeStateStatus=status))
                self.assertEqual(tag, 'left-pending-conflict')
                self.assertEqual(self.updated, [])

    def test_pr_whose_checks_are_not_red_is_left_alone(self):
        """No red checks means nothing is demonstrably blocked — acting here
        would update branches on healthy PRs for no reason."""
        tag = self.run_ladder(
            _open_pr(statusCheckRollup=[{'conclusion': 'SUCCESS'}]))
        self.assertEqual(tag, 'left-pending-checks-not-red')
        self.assertEqual(self.updated, [])

    def test_red_base_is_left_alone(self):
        """The blocker has NOT cleared — this is the pre-05:43 state of #111."""
        tag = self.run_ladder(_open_pr(), base_green=False)
        self.assertEqual(tag, 'left-pending-base-not-green')
        self.assertEqual(self.updated, [])

    def test_branch_already_current_is_left_alone(self):
        """Base is green and the branch already has it, so the red checks are
        the PR's own problem — a real escalation. Leave it for a human."""
        tag = self.run_ladder(_open_pr(), behind=False)
        self.assertEqual(tag, 'left-pending-not-behind')
        self.assertEqual(self.updated, [])

    def test_missing_base_ref_is_left_alone(self):
        tag = self.run_ladder(_open_pr(baseRefName=None))
        self.assertEqual(tag, 'left-pending-no-base')
        self.assertEqual(self.updated, [])


class BudgetTest(_LadderBase):
    def test_exhausted_budget_defers_without_acting(self):
        card = {
            'card_id': 'c', 'repo': 'Larry-Yatch/RSDPM', 'pr_number': 111,
            'head_sha': CARD_HEAD, 'pr_url': 'u',
        }
        with patch.object(h, 'probe_pr', return_value=_open_pr()), \
                patch.object(h, 'base_branch_is_green', return_value=True), \
                patch.object(h, 'branch_is_behind_base', return_value=True):
            tag = h.evaluate(card, {'actioned': {}}, False, [0])
        self.assertEqual(tag, 'deferred-budget')
        self.assertEqual(self.updated, [])
        self.assertEqual(self.retired, [])


class DryRunTest(_LadderBase):
    def test_dry_run_neither_updates_nor_retires(self):
        card = {
            'card_id': 'c', 'repo': 'Larry-Yatch/RSDPM', 'pr_number': 111,
            'head_sha': CARD_HEAD, 'pr_url': 'u',
        }
        with patch.object(h, 'probe_pr', return_value=_open_pr()), \
                patch.object(h, 'base_branch_is_green', return_value=True), \
                patch.object(h, 'branch_is_behind_base', return_value=True):
            tag = h.evaluate(card, {'actioned': {}}, True, [3])
        self.assertEqual(tag, 'dry-run-would-act')
        self.assertEqual(self.updated, [])
        self.assertEqual(self.retired, [])


class MainTest(_IsolatedAgentsRoot):
    def test_kill_switch_stands_the_healer_down(self):
        h.KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        h.KILL_SWITCH.write_text('')
        with patch.object(h, 'candidate_cards') as cards, \
                patch.object(sys, 'argv', ['heal_stale_escalation_recheck.py']):
            self.assertEqual(h.main(), 0)
        cards.assert_not_called()

    def test_no_candidates_is_a_clean_no_op(self):
        with patch.object(h, 'candidate_cards', return_value=[]), \
                patch.object(sys, 'argv', ['heal_stale_escalation_recheck.py']):
            self.assertEqual(h.main(), 0)

    def test_an_internal_failure_alerts_and_returns_nonzero(self):
        with patch.object(h, 'candidate_cards', side_effect=RuntimeError('x')), \
                patch.object(h.la, 'append_alert') as alert, \
                patch.object(sys, 'argv', ['heal_stale_escalation_recheck.py']):
            self.assertEqual(h.main(), 1)
        alert.assert_called_once()

    def test_end_to_end_dry_run_over_a_real_shaped_card_writes_nothing(self):
        """Wires candidate_cards -> evaluate -> summary on the RSDPM #111 card
        shape, with every gh probe answering 'stale gate'. Dry-run must reach
        the act rung and still leave the card and the ledger untouched."""
        with patch.object(h.approval, 'load_state',
                          return_value={'pending': [_card()]}), \
                patch.object(h, 'probe_pr', return_value=_open_pr()), \
                patch.object(h, 'base_branch_is_green', return_value=True), \
                patch.object(h, 'branch_is_behind_base', return_value=True), \
                patch.object(h.approval, 'resolve') as resolve, \
                patch.object(h, 'update_branch') as update, \
                patch.object(sys, 'argv',
                             ['heal_stale_escalation_recheck.py', '--dry-run']):
            self.assertEqual(h.main(), 0)
        resolve.assert_not_called()
        update.assert_not_called()
        self.assertFalse(h.STATE_FILE.exists())

    def test_heartbeat_is_written_even_when_the_kill_switch_is_on(self):
        h.KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        h.KILL_SWITCH.write_text('')
        with patch.object(sys, 'argv', ['heal_stale_escalation_recheck.py']):
            h.main()
        self.assertTrue(h.HEARTBEAT_FILE.exists())
        self.assertEqual(
            json.loads(h.HEARTBEAT_FILE.read_text())['healer'], h.HEALER_SOURCE)


if __name__ == '__main__':
    unittest.main()
