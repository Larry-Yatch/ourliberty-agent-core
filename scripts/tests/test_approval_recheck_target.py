"""Tests for the session-less escalation approval's head resolution and
`recheck_target` stamping (approvals-third-action-recheck slice 1).

Two defects are under test, both proven live on RSDPM #59 (2026-07-25):

1. The mirror-result envelope carries no `head_sha` (it is not in
   `CHAIN_CONTEXT_FIELDS`, so `build_chain_envelope` drops it on the
   marker-notify hop). That collapsed the approval id to the bare
   `mirror-review-<task_id>`, and Contract D matches `history` as well as
   `pending` — so once a bare-id card was decided, every later escalation on
   that task was silently suppressed.
2. `pr_url` reached the card only inside the prose summary, leaving no
   structured coordinate for the third Approvals action to act on.

NOTE: stdlib unittest only — pytest is NOT installed in the droplet
test/regression environment.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import outbox_notifier as notifier  # noqa: E402

PR_URL = 'https://github.com/Larry-Yatch/RSDPM/pull/59'
LIVE_HEAD = '1a3757dcbd534fe429a6dfc016634fb06fc0dd75'
ENV_HEAD = 'd4f17fc2a285439efecdb2f41efdd758527cc38c'


class ResolveEscalationHeadShaTest(unittest.TestCase):
    """Envelope head wins when present; otherwise fall back to a live lookup."""

    def test_envelope_head_preferred_without_gh_call(self):
        with mock.patch.object(notifier, '_gh_pr_head_sha') as gh:
            got = notifier._resolve_escalation_head_sha(
                {'head_sha': ENV_HEAD}, PR_URL)
        self.assertEqual(got, ENV_HEAD)
        gh.assert_not_called()

    def test_falls_back_to_live_lookup_when_envelope_has_no_head(self):
        # The real RSDPM #59 shape: pr_url + target_repo present, head absent.
        with mock.patch.object(
            notifier, '_gh_pr_head_sha', return_value=LIVE_HEAD,
        ) as gh:
            got = notifier._resolve_escalation_head_sha({}, PR_URL)
        self.assertEqual(got, LIVE_HEAD)
        gh.assert_called_once()

    def test_no_pr_url_yields_none_without_gh_call(self):
        with mock.patch.object(notifier, '_gh_pr_head_sha') as gh:
            self.assertIsNone(notifier._resolve_escalation_head_sha({}, None))
        gh.assert_not_called()

    def test_unresolvable_head_yields_none_rather_than_raising(self):
        # gh outage / backoff. Must degrade to the coarse id, never crash the
        # escalation path — a coarse card beats no card.
        with mock.patch.object(notifier, '_gh_pr_head_sha', return_value=None):
            self.assertIsNone(
                notifier._resolve_escalation_head_sha({}, PR_URL))

    def test_unparseable_pr_url_yields_none(self):
        with mock.patch.object(notifier, '_gh_pr_head_sha') as gh:
            self.assertIsNone(
                notifier._resolve_escalation_head_sha({}, 'not-a-pr-url'))
        gh.assert_not_called()

    def test_empty_string_envelope_head_is_not_treated_as_present(self):
        with mock.patch.object(
            notifier, '_gh_pr_head_sha', return_value=LIVE_HEAD,
        ):
            got = notifier._resolve_escalation_head_sha(
                {'head_sha': ''}, PR_URL)
        self.assertEqual(got, LIVE_HEAD)


class BuildRecheckTargetTest(unittest.TestCase):
    """All three coordinates required; round is the NEXT review round."""

    def _build(self, **kw):
        defaults = dict(
            task_id='pr-RSDPM-59', pr_url=PR_URL, target_repo='RSDPM',
            head_sha=LIVE_HEAD, data={},
        )
        defaults.update(kw)
        return notifier._build_recheck_target(**defaults)

    def test_full_coordinate_stamps_all_fields(self):
        got = self._build()
        self.assertEqual(got, {
            'task_id': 'pr-RSDPM-59',
            'pr_url': PR_URL,
            'target_repo': 'RSDPM',
            'head_sha': LIVE_HEAD,
            'round': 1,
            'replan_count': 0,
        })

    def test_replan_count_rides_along_from_the_envelope(self):
        # `revision_count` resets to 0 on each replan's first review dispatch,
        # so `round` alone cannot name a round on a replanned task. Without the
        # count, replan 2 round 1 and replan 1 round 1 both spell `rev1`.
        got = self._build(data={'replan_count': 2, 'revision_count': 0})
        self.assertEqual(got['replan_count'], 2)
        self.assertEqual(got['round'], 1)

    def test_absent_replan_count_is_zero_not_missing(self):
        # Consumers branch on `> 0`; a MISSING key would make an old card and a
        # replan-0 card indistinguishable from a malformed one.
        self.assertEqual(self._build()['replan_count'], 0)

    def test_bogus_replan_count_falls_back_to_zero(self):
        for bogus in ('2', -1, None, [], 1.5):
            with self.subTest(bogus=bogus):
                got = self._build(data={'replan_count': bogus})
                self.assertEqual(got['replan_count'], 0)

    def test_round_advances_past_prior_revision_count(self):
        self.assertEqual(self._build(data={'revision_count': 2})['round'], 3)

    def test_bogus_revision_count_falls_back_to_round_one(self):
        for bogus in ('2', -1, None, [], 1.5):
            with self.subTest(bogus=bogus):
                got = self._build(data={'revision_count': bogus})
                self.assertEqual(got['round'], 1)

    def test_missing_any_coordinate_yields_none(self):
        # Fails CLOSED: no target -> no button -> operator keeps Approve/Reject.
        for field in ('pr_url', 'target_repo', 'head_sha'):
            with self.subTest(missing=field):
                self.assertIsNone(self._build(**{field: None}))
                self.assertIsNone(self._build(**{field: ''}))


class EmitDecisionApprovalTest(unittest.TestCase):
    """End-to-end shape of the emitted approval payload."""

    def setUp(self):
        self.added = []
        self.emitted = []

        p_add = mock.patch.object(
            notifier.approval, 'add_pending',
            side_effect=lambda payload, chat_id=None: self.added.append(payload))
        p_find = mock.patch.object(
            notifier.approval, 'find_by_id_any_state', return_value=None)
        p_chain = mock.patch.object(
            notifier.approval, 'build_approval_request_chain_event',
            side_effect=lambda payload, **kw: {'payload': payload})
        p_emit = mock.patch.object(
            notifier.chain_event_emit, 'emit_event',
            side_effect=lambda **kw: self.emitted.append(kw))
        p_chat = mock.patch.object(
            notifier, '_primary_chat_id', return_value=1)
        for p in (p_add, p_find, p_chain, p_emit, p_chat):
            p.start()
            self.addCleanup(p.stop)

    def _emit(self, data, head=LIVE_HEAD):
        with mock.patch.object(
            notifier, '_gh_pr_head_sha', return_value=head,
        ):
            notifier._emit_no_session_decision_approval(
                data, {'intent_kwargs': {'reason': 'because'}, 'payload': {}})

    def test_rsdpm_59_shape_gets_head_scoped_id_and_recheck_target(self):
        # The exact live envelope: pr_url + target_repo, NO head_sha.
        self._emit({
            'task_id': 'pr-RSDPM-59',
            'pr_url': PR_URL,
            'target_repo': 'RSDPM',
        })
        self.assertEqual(len(self.added), 1)
        payload = self.added[0]
        # Regression: was the bare `mirror-review-pr-RSDPM-59`, which let a
        # decided history row suppress every later escalation on the task.
        self.assertEqual(payload['task_id'], 'mirror-review-pr-RSDPM-59-1a3757dc')
        self.assertEqual(payload['recheck_target']['pr_url'], PR_URL)
        self.assertEqual(payload['recheck_target']['head_sha'], LIVE_HEAD)
        self.assertEqual(payload['recheck_target']['round'], 1)

    def test_unresolvable_head_keeps_bare_id_and_omits_recheck_target(self):
        self._emit({
            'task_id': 'pr-RSDPM-59',
            'pr_url': PR_URL,
            'target_repo': 'RSDPM',
        }, head=None)
        payload = self.added[0]
        self.assertEqual(payload['task_id'], 'mirror-review-pr-RSDPM-59')
        self.assertNotIn('recheck_target', payload)

    def test_contract_d_still_skips_an_existing_approval(self):
        with mock.patch.object(
            notifier.approval, 'find_by_id_any_state', return_value={'id': 'x'},
        ):
            self._emit({'task_id': 't', 'pr_url': PR_URL, 'target_repo': 'RSDPM'})
        self.assertEqual(self.added, [])

    def test_distinct_heads_produce_distinct_ids(self):
        # The property Contract D always assumed it had: a second escalation on
        # a NEW head must not collide with the decided card for the old head.
        ids = []
        for head in (ENV_HEAD, LIVE_HEAD):
            self.added.clear()
            self._emit({
                'task_id': 'pr-RSDPM-59', 'pr_url': PR_URL,
                'target_repo': 'RSDPM', 'head_sha': head,
            })
            ids.append(self.added[0]['task_id'])
        self.assertNotEqual(ids[0], ids[1])


class ChainEventRecheckTargetTest(unittest.TestCase):
    """The tab feed carries the coordinate only when the payload has one."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        import beacon_approval_handler as bah
        self.bah = bah

    def _payload(self, **extra):
        base = {'task_id': 'mirror-review-x', 'prompt': 'p', 'summary': 's'}
        base.update(extra)
        return self.bah.build_approval_request_chain_event(base)['payload']

    def test_recheck_target_forwarded_when_present(self):
        target = {'task_id': 'pr-RSDPM-59', 'pr_url': PR_URL,
                  'target_repo': 'RSDPM', 'head_sha': LIVE_HEAD, 'round': 1}
        self.assertEqual(
            self._payload(recheck_target=target)['recheck_target'], target)

    def test_absent_by_default_so_existing_payloads_are_unchanged(self):
        self.assertNotIn('recheck_target', self._payload())

    def test_non_dict_or_empty_recheck_target_is_not_forwarded(self):
        for bogus in ({}, None, 'pr-59', [], 0):
            with self.subTest(bogus=bogus):
                self.assertNotIn(
                    'recheck_target', self._payload(recheck_target=bogus))

    def test_no_third_suggested_envelope_is_emitted(self):
        # `recheck` dispatches via safe_write_inbox, not an LLM envelope to
        # Beacon — so there is deliberately nothing to suggest.
        payload = self._payload(recheck_target={'pr_url': PR_URL})
        self.assertNotIn('suggested_envelope_for_recheck', payload)

    def test_promoted_source_forwarded_when_present(self):
        # agent-core #1058: the dashboard's Approve-executes routing keys on
        # this marker in the CHAIN payload (it only ever sees the chain row).
        payload = self._payload(promoted_source='for-larry-mirror-review')
        self.assertEqual(payload['promoted_source'], 'for-larry-mirror-review')

    def test_promoted_source_absent_by_default(self):
        self.assertNotIn('promoted_source', self._payload())

    def test_non_string_promoted_source_is_not_forwarded(self):
        for bogus in ('', None, 0, [], {}):
            with self.subTest(bogus=bogus):
                self.assertNotIn(
                    'promoted_source', self._payload(promoted_source=bogus))


if __name__ == '__main__':
    unittest.main()
