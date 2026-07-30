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


class ApprovedEscalationModeTest(unittest.TestCase):
    """Approve on a promoted stranded-escalation card (agent-core #1058).

    The generic Beacon LLM envelope had no wired action for this card class —
    on PR #1058 the $1 Beacon session diagnosed "merge it" and then could
    neither merge nor reach Larry, so Approve recorded the decision, cleared
    the card, and did nothing. With a recheck_target, Approve now dispatches a
    fresh Mirror re-review MECHANICALLY; without one it refuses pre-claim so
    the card stays visible instead of silently clearing.
    """

    PROMOTED_PAYLOAD = {
        'prompt': 'healer narration about the stranded escalation',
        'promoted_source': 'for-larry-mirror-review',
        'recheck_target': FULL_TARGET,
    }

    def _source(self, payload):
        return {
            'event_type': 'approval_request',
            'event_id': 'ev-promoted-1',
            'task_id': 'unreg-approval-de9cda4efdbd',
            'payload': payload,
        }

    def _approve(self, payload, head=LIVE_HEAD, comment=None):
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=head,
        ):
            return da._build_envelope_for_action(
                source=self._source(payload), action='approve',
                comment=comment, actor='larry')

    def _recheck(self, payload, head=LIVE_HEAD, comment=None):
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=head,
        ):
            return da._build_envelope_for_action(
                source=self._source(payload), action='recheck',
                comment=comment, actor='larry')

    def test_approve_dispatches_a_mirror_review_not_a_beacon_envelope(self):
        agent, filename, env = self._approve(self.PROMOTED_PAYLOAD)
        self.assertEqual(agent, 'mirror')
        self.assertEqual(filename, 'review-pr-RSDPM-59-rev1.json')
        self.assertEqual(env['dispatched_by'], 'dashboard-stranded-escalation')
        self.assertEqual(env['head_sha'], LIVE_HEAD)
        self.assertEqual(env['task_id'], 'pr-RSDPM-59')

    def test_approve_framing_is_on_the_merits_not_revision_applied(self):
        # Nothing was fixed — the prompt must not claim a revision landed, and
        # the card's meta-prose must not be presented as Mirror's findings.
        _, _, env = self._approve(self.PROMOTED_PAYLOAD)
        self.assertIn('stranded', env['prompt'])
        self.assertNotIn('has been applied', env['prompt'])
        self.assertNotIn('previous_findings', env)
        self.assertIn('REVIEW_PASS', env['prompt'])
        self.assertIn('REVIEW_ESCALATE', env['prompt'])

    def test_card_prose_rides_as_context_not_findings(self):
        _, _, env = self._approve(self.PROMOTED_PAYLOAD)
        self.assertIn('healer narration about the stranded escalation',
                      env['prompt'])
        self.assertNotIn('Your findings from the previous round',
                         env['prompt'])

    def test_recheck_on_a_promoted_card_uses_the_same_framing_as_approve(self):
        # Review round 1 defect: recheck_target presence lights up the tab's
        # third button on promoted cards, and that branch ran the default mode
        # — telling Mirror a revision had been applied to an untouched head and
        # handing her the healer's narration as her own prior findings, which
        # outbox_notifier then threads into any resulting Forge revision.
        _, _, env = self._recheck(self.PROMOTED_PAYLOAD)
        self.assertEqual(env['dispatched_by'], 'dashboard-stranded-escalation')
        self.assertNotIn('has been applied', env['prompt'])
        self.assertNotIn('previous_findings', env)
        self.assertNotIn('Your findings from the previous round',
                         env['prompt'])

    def test_recheck_on_an_ordinary_card_keeps_the_revision_framing(self):
        _, _, env = self._recheck({
            'prompt': 'the GRANT is inert', 'recheck_target': FULL_TARGET,
        })
        self.assertEqual(env['dispatched_by'], 'dashboard-recheck')
        self.assertIn('has been applied', env['prompt'])
        self.assertEqual(env['previous_findings'], ['the GRANT is inert'])

    def test_approve_without_coordinate_is_a_400_pre_claim(self):
        # The 400 fires in the builder, which the handler calls BEFORE the
        # read_at claim — so the card survives the refusal intact.
        payload = {'prompt': 'p', 'promoted_source': 'for-larry-mirror-review'}
        with self.assertRaises(HTTPException) as ctx:
            self._approve(payload)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('nothing it can execute', ctx.exception.detail)

    def test_no_coordinate_400_does_not_promise_a_url_the_card_lacks(self):
        # The commonest no-coordinate cause is a source record with no pr_url,
        # in which case the card body has no URL either — so the refusal must
        # not send the operator looking for one. It names the task instead.
        payload = {'prompt': 'p', 'promoted_source': 'for-larry-mirror-review'}
        with self.assertRaises(HTTPException) as ctx:
            self._approve(payload)
        self.assertNotIn('URL in the card text', ctx.exception.detail)
        self.assertIn('unreg-approval-de9cda4efdbd', ctx.exception.detail)

    def test_ordinary_approvals_keep_the_beacon_route(self):
        # No promoted_source ⇒ the legacy path, byte-for-byte: even a payload
        # that happens to carry a recheck_target (every mirror-review-* card
        # does) must NOT be hijacked — its Approve promise is a Forge
        # revision via Beacon, not a re-review.
        agent, filename, env = self._approve({
            'prompt': 'findings', 'recheck_target': FULL_TARGET,
        })
        self.assertEqual(agent, 'beacon')
        self.assertEqual(filename, 'larry-approval-ev-promoted-1.json')
        self.assertIn('Larry approved the pending proposal', env['prompt'])

    def test_marker_alone_cannot_hijack_a_non_promoted_card(self):
        # beacon_approval_handler's marker parser copies LLM-authored JSON keys
        # onto the payload verbatim, so `promoted_source` is reachable by a
        # Beacon-authored APPROVAL_REQUEST. Without the healer-minted task_id
        # prefix it must NOT reroute Approve — otherwise the pending entry pops
        # as approved while the plan Larry actually approved never dispatches.
        source = {
            'event_type': 'approval_request',
            'event_id': 'ev-beacon-1',
            'task_id': 'replan-some-beacon-task-001',
            'payload': dict(self.PROMOTED_PAYLOAD),
        }
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=LIVE_HEAD,
        ):
            agent, filename, env = da._build_envelope_for_action(
                source=source, action='approve', comment=None, actor='larry')
        self.assertEqual(agent, 'beacon')
        self.assertEqual(filename, 'larry-approval-ev-beacon-1.json')
        self.assertIn('Larry approved the pending proposal', env['prompt'])

    def test_marker_alone_cannot_400_a_non_promoted_card(self):
        # The other half: a coordinate-less imitation must not make a genuine
        # card permanently un-approvable either.
        source = {
            'event_type': 'approval_request',
            'event_id': 'ev-beacon-2',
            'task_id': 'replan-some-beacon-task-002',
            'payload': {'prompt': 'a real plan',
                        'promoted_source': 'for-larry-mirror-review'},
        }
        agent, _, _ = da._build_envelope_for_action(
            source=source, action='approve', comment=None, actor='larry')
        self.assertEqual(agent, 'beacon')

    def test_unknown_mode_raises_instead_of_defaulting(self):
        # A bare else would let a typo ('approved_escalation') silently produce
        # the revision-applied framing + findings carry on an untouched head.
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=LIVE_HEAD,
        ):
            with self.assertRaises(ValueError):
                da._build_recheck_envelope(
                    payload=dict(self.PROMOTED_PAYLOAD), comment=None,
                    task_id='pr-RSDPM-59', mode='approved_escalation')

    def test_the_routing_matrix_has_always_rejected_mark_done_here(self):
        """The builder rejects mark_done on an approval_request — but the
        handler skipped the builder for that verb, so this check never ran.
        The end-to-end guard is pinned in
        test_dashboard_api_larry_action.MarkDoneOnDecisionTest."""
        with self.assertRaises(HTTPException) as ctx:
            da._build_envelope_for_action(
                source=self._source(self.PROMOTED_PAYLOAD), action='mark_done',
                comment=None, actor='larry')
        self.assertEqual(ctx.exception.status_code, 400)

    def test_identity_predicate_requires_both_prefix_and_marker(self):
        promoted = self._source(self.PROMOTED_PAYLOAD)
        self.assertTrue(da._is_promoted_stranded_escalation(
            promoted, promoted['payload']))
        # marker without the healer-minted id
        self.assertFalse(da._is_promoted_stranded_escalation(
            {'task_id': 'replan-x'}, self.PROMOTED_PAYLOAD))
        # healer-minted id without the marker
        self.assertFalse(da._is_promoted_stranded_escalation(
            {'task_id': 'unreg-approval-abc'}, {'prompt': 'p'}))
        # neither, and a non-string task_id
        self.assertFalse(da._is_promoted_stranded_escalation({}, {}))
        self.assertFalse(da._is_promoted_stranded_escalation(
            {'task_id': None}, self.PROMOTED_PAYLOAD))

    def test_mode_selector_follows_the_card_not_the_action(self):
        promoted = self._source(self.PROMOTED_PAYLOAD)
        self.assertEqual(
            da._recheck_mode_for_payload(promoted, promoted['payload']),
            'stranded-escalation')
        ordinary = self._source({'prompt': 'f', 'recheck_target': FULL_TARGET})
        self.assertEqual(
            da._recheck_mode_for_payload(ordinary, ordinary['payload']),
            'operator-fixed')

    def test_every_selectable_mode_is_a_known_mode(self):
        # The selector and the builder's whitelist must not drift apart, or the
        # builder's new ValueError becomes a 500 on a real click.
        for src in (self._source(self.PROMOTED_PAYLOAD),
                    self._source({'prompt': 'f'})):
            self.assertIn(
                da._recheck_mode_for_payload(src, src['payload']),
                da.RECHECK_ENVELOPE_MODES)

    def test_reject_on_a_promoted_card_is_unchanged(self):
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=LIVE_HEAD,
        ):
            agent, filename, _ = da._build_envelope_for_action(
                source=self._source(self.PROMOTED_PAYLOAD), action='reject',
                comment=None, actor='larry')
        self.assertEqual(agent, 'beacon')
        self.assertEqual(filename, 'larry-reject-ev-promoted-1.json')

    def test_pr_url_grammars_agree_across_the_producer_and_consumer(self):
        """The promoter parses the PR URL to build the coordinate; this module
        re-parses it at dispatch. If the two grammars drift, the healer stamps
        a coordinate whose URL the dashboard rejects — a 400 on a card whose
        text promises mechanical execution, discovered only when Larry clicks.
        Table-driven so it pins BEHAVIOR, not a shared regex literal."""
        import heal_unregistered_approval as hua
        cases = [
            'https://github.com/Larry-Yatch/RSDPM/pull/59',
            'https://github.com/Larry-Yatch/RSDPM/pull/59/',
            '  https://github.com/Larry-Yatch/RSDPM/pull/59  ',
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1058',
            'https://github.com/Larry-Yatch/RSDPM/issues/59',
            'https://github.com/Larry-Yatch/RSDPM/pull/59/files',
            'https://github.com/Larry-Yatch/RSDPM/pull/abc',
            'https://evil.example/Larry-Yatch/RSDPM/pull/59',
            'https://github.com/RSDPM/pull/59',
            '', 'nonsense',
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(
                    hua.parse_pr_url(url), da._parse_recheck_pr_url(url),
                    f'PR-URL grammars disagree on {url!r}')

    def test_id_prefix_matches_the_promoter_constant(self):
        """Second cross-module literal, same drift hazard as the marker: if the
        promoter renames its task-id prefix, _is_promoted_stranded_escalation
        returns False for every real card and Approve silently reverts to the
        generic Beacon envelope — the #1058 no-op restored, tests still green
        because they hard-code the id."""
        import heal_unregistered_approval as hua
        self.assertEqual(da.PROMOTED_STRANDED_ESCALATION_ID_PREFIX,
                         f'{hua.PROMOTED_TASK_PREFIX}-')
        # ... and a real promoted task_id actually carries it.
        self.assertTrue(
            hua.derive_task_id(hua.forlarry_dedup_key('mirror-review:t'))
            .startswith(da.PROMOTED_STRANDED_ESCALATION_ID_PREFIX))

    def test_marker_string_matches_the_promoter_constant(self):
        # dashboard_api keys the routing on a literal (importing the healer
        # into the API process is undesirable); this pins the two against
        # drift — if the promoter's constant ever changes, this fails before
        # the routing silently stops matching.
        import heal_unregistered_approval as hua
        self.assertEqual(hua.PROMOTED_SOURCE_FORLARRY,
                         'for-larry-mirror-review')
        self.assertEqual(
            self.PROMOTED_PAYLOAD['promoted_source'],
            hua.PROMOTED_SOURCE_FORLARRY)

    def test_recheck_mode_output_is_unchanged_by_the_mode_param(self):
        # The default mode must reproduce the pre-#1058 recheck prompt exactly
        # (the "Revision N has been applied" framing and findings carry).
        with mock.patch.object(
            da, '_gh_pr_head_sha_for_recheck', return_value=LIVE_HEAD,
        ):
            _, _, env = da._build_recheck_envelope(
                payload={'prompt': 'old findings',
                         'recheck_target': FULL_TARGET},
                comment=None, task_id='pr-RSDPM-59')
        self.assertIn('Revision 1 has been applied', env['prompt'])
        self.assertEqual(env['previous_findings'], ['old findings'])
        self.assertEqual(env['dispatched_by'], 'dashboard-recheck')


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
