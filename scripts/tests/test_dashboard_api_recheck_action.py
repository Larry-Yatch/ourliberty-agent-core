#!/usr/bin/env python3
"""Tests for action=recheck on /api/larry/action — the Approvals tab's third
exit ("Fixed — re-review"), approvals-third-action-recheck slice 2.

Context (RSDPM #59, 2026-07-25): a review escalation had only Approve (dispatch
a Forge revision against already-fixed code) and Reject (dispatch nothing, and
— because review dispatch is head-blind once a task concludes — strand the PR
permanently). `recheck` re-dispatches the Mirror review at the CURRENT head.

The builder is pure apart from one `gh` shell-out, which is patched throughout.

Run:
    cd /home/larry/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_recheck_action
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

os.environ.setdefault('DASHBOARD_API_TOKEN', 'test-token-value')

import dashboard_api as da  # noqa: E402
from fastapi import HTTPException  # noqa: E402

PR_URL = 'https://github.com/Larry-Yatch/RSDPM/pull/59'
STAMPED_HEAD = 'd4f17fc2a285439efecdb2f41efdd758527cc38c'
LIVE_HEAD = '1a3757dcbd534fe429a6dfc016634fb06fc0dd75'

FULL_TARGET = {
    'task_id': 'pr-RSDPM-59',
    'pr_url': PR_URL,
    'target_repo': 'RSDPM',
    'head_sha': STAMPED_HEAD,
    'round': 1,
}


class ParsePrUrlTest(unittest.TestCase):
    def test_parses_owner_repo_and_number(self):
        self.assertEqual(
            da._parse_recheck_pr_url(PR_URL), ('Larry-Yatch/RSDPM', 59))

    def test_tolerates_trailing_slash_and_whitespace(self):
        self.assertEqual(
            da._parse_recheck_pr_url(f'  {PR_URL}/  '),
            ('Larry-Yatch/RSDPM', 59))

    def test_rejects_non_pr_urls(self):
        for bad in (
            'https://github.com/Larry-Yatch/RSDPM/issues/59',
            'https://github.com/Larry-Yatch/RSDPM/pull/',
            'https://evil.example/Larry-Yatch/RSDPM/pull/59',
            'https://github.com/Larry-Yatch/RSDPM/pull/59/files',
            '', 'nonsense',
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(da._parse_recheck_pr_url(bad))


class BuildRecheckEnvelopeTest(unittest.TestCase):
    def _build(self, target=None, comment=None, head=LIVE_HEAD, prompt='old findings'):
        payload = {'prompt': prompt}
        if target is not None:
            payload['recheck_target'] = target
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=head,
        ):
            return da._build_recheck_envelope(
                payload=payload, comment=comment, task_id='pr-RSDPM-59')

    def test_dispatches_to_mirror_at_the_live_head(self):
        agent, filename, env = self._build(FULL_TARGET)
        self.assertEqual(agent, 'mirror')
        self.assertEqual(filename, 'review-pr-RSDPM-59-rev1.json')
        # The LIVE head, not the stale one stamped on the card.
        self.assertEqual(env['head_sha'], LIVE_HEAD)
        self.assertNotEqual(env['head_sha'], STAMPED_HEAD)
        self.assertTrue(env['head_moved'])
        self.assertEqual(env['phase'], 'review')
        self.assertEqual(env['dispatched_by'], 'dashboard-recheck')

    def test_carries_prior_findings_forward(self):
        _, _, env = self._build(FULL_TARGET, prompt='the GRANT is inert vs RLS')
        self.assertEqual(env['previous_findings'], ['the GRANT is inert vs RLS'])
        self.assertIn('the GRANT is inert vs RLS', env['prompt'])

    def test_round_advances_the_filename(self):
        target = dict(FULL_TARGET, round=3)
        _, filename, env = self._build(target)
        self.assertEqual(filename, 'review-pr-RSDPM-59-rev3.json')
        self.assertEqual(env['revision_count'], 3)
        self.assertIn('next would be round 4', env['prompt'])

    def test_unmoved_head_is_allowed_but_flagged(self):
        # Deliberate re-run on identical bits: allowed (spec §8 Q2), but the
        # response must let the UI warn that it will likely re-return the
        # same verdict.
        _, _, env = self._build(FULL_TARGET, head=STAMPED_HEAD)
        self.assertFalse(env['head_moved'])

    def test_operator_comment_is_framed_as_context_not_instruction(self):
        _, _, env = self._build(FULL_TARGET, comment='changed the mechanism')
        self.assertIn("Operator's note on what changed: changed the mechanism",
                      env['prompt'])
        self.assertEqual(env['comment'], 'changed the mechanism')
        # The review protocol still governs — the marker contract is restated
        # after the operator text.
        self.assertIn('REVIEW_PASS', env['prompt'])
        self.assertLess(env['prompt'].index('changed the mechanism'),
                        env['prompt'].index('REVIEW_PASS'))

    def test_missing_recheck_target_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._build(None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('no recheck_target', ctx.exception.detail)

    def test_non_dict_recheck_target_is_400(self):
        for bogus in ('pr-59', [], 0, True):
            with self.subTest(bogus=bogus):
                with self.assertRaises(HTTPException) as ctx:
                    self._build(bogus)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_incomplete_coordinate_is_400(self):
        for field in ('pr_url', 'target_repo'):
            with self.subTest(missing=field):
                with self.assertRaises(HTTPException) as ctx:
                    self._build(dict(FULL_TARGET, **{field: None}))
                self.assertEqual(ctx.exception.status_code, 400)

    def test_malformed_pr_url_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._build(dict(FULL_TARGET, pr_url='https://evil.example/x/pull/1'))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unresolvable_head_is_502_and_never_falls_back_to_stamped(self):
        # The card's head is stale BY DEFINITION — staleness is the premise of
        # the click. Re-reviewing it would re-review the replaced code.
        with self.assertRaises(HTTPException) as ctx:
            self._build(FULL_TARGET, head=None)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn('card is untouched', ctx.exception.detail)

    def test_replan_iteration_uses_the_replan_filename_grammar(self):
        # Must match `outbox_notifier._dispatch_mirror_review_rerun` exactly
        # (D3.5 5c-followup-2 HIGH-1) or the two dispatch paths collide.
        target = dict(FULL_TARGET, replan_count=2, round=1)
        _, filename, env = self._build(target)
        self.assertEqual(filename, 'review-pr-RSDPM-59-replan2-rev1.json')
        self.assertEqual(env['replan_count'], 2)

    def test_same_round_in_different_replans_gets_distinct_names(self):
        # The collision this fixes: `revision_count` resets per replan, so both
        # of these were `rev1` and the second overwrote the first's name.
        names = {
            self._build(dict(FULL_TARGET, replan_count=n, round=1))[1]
            for n in (1, 2)
        }
        self.assertEqual(len(names), 2)

    def test_no_replan_keeps_the_bare_name_and_omits_the_field(self):
        # Back-compat: cards stamped before `replan_count` existed carry none,
        # and must resolve to exactly the filename they already did.
        _, filename, env = self._build(FULL_TARGET)
        self.assertEqual(filename, 'review-pr-RSDPM-59-rev1.json')
        self.assertNotIn('replan_count', env)

    def test_bogus_replan_count_degrades_to_the_bare_name(self):
        for bogus in ('2', -1, None, [], 1.5, 0):
            with self.subTest(bogus=bogus):
                _, filename, env = self._build(
                    dict(FULL_TARGET, replan_count=bogus))
                self.assertEqual(filename, 'review-pr-RSDPM-59-rev1.json')
                self.assertNotIn('replan_count', env)

    def test_bogus_round_falls_back_to_one(self):
        for bogus in (0, -1, '2', None, 1.5):
            with self.subTest(bogus=bogus):
                _, filename, _ = self._build(dict(FULL_TARGET, round=bogus))
                self.assertEqual(filename, 'review-pr-RSDPM-59-rev1.json')


class RecheckWiringTest(unittest.TestCase):
    def test_recheck_is_a_valid_action(self):
        self.assertIn('recheck', da.LARRY_ACTION_VALID_ACTIONS)

    def test_recheck_is_not_an_alert_rating(self):
        self.assertNotIn('recheck', da.ALERT_RATING_ACTIONS)

    def test_mirror_is_an_allowed_target(self):
        self.assertIn('mirror', da.ALLOWED_TARGET_AGENTS)

    def test_recheck_routes_through_the_approval_request_matrix(self):
        source = {
            'event_type': 'approval_request',
            'event_id': 'ev1',
            'task_id': 'mirror-review-pr-RSDPM-59',
            'payload': {'prompt': 'findings', 'recheck_target': FULL_TARGET},
        }
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=LIVE_HEAD,
        ):
            agent, filename, _ = da._build_envelope_for_action(
                source=source, action='recheck', comment=None, actor='larry')
        self.assertEqual((agent, filename),
                         ('mirror', 'review-pr-RSDPM-59-rev1.json'))

    def test_recheck_on_a_non_approval_event_is_rejected(self):
        source = {
            'event_type': 'larry_alert', 'event_id': 'ev2',
            'task_id': 't', 'payload': {},
        }
        with self.assertRaises(HTTPException):
            da._build_envelope_for_action(
                source=source, action='recheck', comment=None, actor='larry')

    def test_recheck_resolves_the_card_as_modified(self):
        # Regression guard for the gap that would otherwise leave the card
        # pending forever: `recheck` writes no Beacon envelope, so the fan-out
        # map is the ONLY thing that closes it.
        src = Path(da.__file__).read_text()
        self.assertIn("'recheck': 'modified'", src)

    def test_modified_is_a_valid_decision_outcome(self):
        import decision_resolve
        self.assertIn('modified', decision_resolve.VALID_OUTCOMES)


if __name__ == '__main__':
    unittest.main()
