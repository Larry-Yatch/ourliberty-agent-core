"""Tests for scripts/heal_undispatched_pr_review.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Covers the pure, dependency-free core:
- parse_open_prs: normalize gh JSON (incl. isDraft + label names), skip malformed
- task_id_for_branch: strip forge/ for Forge PRs, pr-<number> for opt-in PRs
- _is_reviewable_pr: forge always; else only when auto-review-labeled AND non-draft
- select_orphaned_prs: reviewability filter, age grace, already-dispatched skip,
  unparseable createdAt skip, task_id augmentation
- _synthesize_build_data: the envelope handed to _dispatch_mirror_review
- healer_enabled: env activation default-ON with dry-run override
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import heal_undispatched_pr_review as h  # noqa: E402

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _pr(number, branch, *, age_minutes=60, url=None, title='t', is_draft=False,
        labels=()):
    created = (NOW - timedelta(minutes=age_minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {
        'number': number,
        'url': url or f'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/{number}',
        'headRefName': branch,
        'createdAt': created,
        'title': title,
        'isDraft': is_draft,
        'labels': list(labels),
    }


class TestParseOpenPrs(unittest.TestCase):
    def test_parses_well_formed_rows(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi"}]')
        rows = h.parse_open_prs(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['number'], 1)
        self.assertEqual(rows[0]['headRefName'], 'forge/a')

    def test_skips_rows_missing_required_fields(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"forge/a"},'  # no createdAt
               '{"number":2,"url":"https://x/2","headRefName":"forge/b",'
               '"createdAt":"2026-06-10T00:00:00Z"}]')
        rows = h.parse_open_prs(raw)
        self.assertEqual([r['number'] for r in rows], [2])

    def test_bad_json_and_non_list_return_empty(self):
        self.assertEqual(h.parse_open_prs('not json'), [])
        self.assertEqual(h.parse_open_prs('{"a":1}'), [])
        self.assertEqual(h.parse_open_prs(''), [])

    def test_carries_is_draft(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi","isDraft":true}]')
        rows = h.parse_open_prs(raw)
        self.assertTrue(rows[0]['isDraft'])

    def test_is_draft_defaults_false_when_absent(self):
        # gh always returns a requested field, but a mock / older gh might omit it.
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi"}]')
        self.assertFalse(h.parse_open_prs(raw)[0]['isDraft'])

    def test_carries_label_names(self):
        # gh returns labels as [{name,color,...}]; we flatten to names.
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi",'
               '"labels":[{"name":"auto-review","color":"ededed"},{"name":"bug"}]}]')
        self.assertEqual(h.parse_open_prs(raw)[0]['labels'], ['auto-review', 'bug'])

    def test_labels_defaults_empty_when_absent_or_malformed(self):
        raw = ('[{"number":1,"url":"https://x/1","headRefName":"fix/a",'
               '"createdAt":"2026-06-10T00:00:00Z","title":"hi","labels":null}]')
        self.assertEqual(h.parse_open_prs(raw)[0]['labels'], [])


class TestTaskIdForBranch(unittest.TestCase):
    def test_strips_forge_prefix(self):
        self.assertEqual(h.task_id_for_branch('forge/harden-x-001'), 'harden-x-001')

    def test_passthrough_non_forge(self):
        self.assertEqual(h.task_id_for_branch('main'), 'main')

    def test_human_branch_keys_by_pr_number(self):
        # No forge/ prefix → key by PR number (stable + unique), not the branch.
        self.assertEqual(h.task_id_for_branch('fix/install-drift-dedup', 625), 'pr-625')

    def test_forge_prefix_wins_over_pr_number(self):
        # A forge build PR keeps its branch-derived id even with a number present,
        # so the backstop's filename lines up with the inline dispatch's key.
        self.assertEqual(h.task_id_for_branch('forge/harden-x-001', 412), 'harden-x-001')


class TestIsReviewablePr(unittest.TestCase):
    LBL = [h.AUTO_REVIEW_LABEL]

    def test_forge_always_reviewable_regardless_of_label_or_draft(self):
        self.assertTrue(h._is_reviewable_pr('forge/x', is_draft=False, labels=[]))
        self.assertTrue(h._is_reviewable_pr('forge/x', is_draft=True, labels=[]))

    def test_labeled_non_draft_is_reviewable(self):
        self.assertTrue(h._is_reviewable_pr('fix/x', is_draft=False, labels=self.LBL))
        self.assertTrue(h._is_reviewable_pr('docs/x', is_draft=False, labels=self.LBL))

    def test_unlabeled_non_forge_is_not_reviewable(self):
        # The agent-PR safety case: a non-draft fix/feat/chore PR with no opt-in
        # label is left alone (it may be agent-authored, must not auto-merge).
        self.assertFalse(h._is_reviewable_pr('fix/x', is_draft=False, labels=[]))
        self.assertFalse(h._is_reviewable_pr('feat/new-mission-abc', is_draft=False, labels=[]))

    def test_labeled_but_draft_is_not_reviewable(self):
        self.assertFalse(h._is_reviewable_pr('fix/x', is_draft=True, labels=self.LBL))

    def test_empty_branch_not_reviewable(self):
        self.assertFalse(h._is_reviewable_pr('', is_draft=False, labels=self.LBL))

    def test_none_labels_tolerated(self):
        self.assertFalse(h._is_reviewable_pr('fix/x', is_draft=False, labels=None))


class TestSelectOrphanedPrs(unittest.TestCase):
    def _none_dispatched(self, _task_id, _head_sha=None):
        return False

    def test_selects_forge_pr_past_grace_with_no_review(self):
        prs = [_pr(412, 'forge/harden-test-prod-write-isolation-001', age_minutes=60)]
        sel = h.select_orphaned_prs(prs, NOW, self._none_dispatched)
        self.assertEqual([p['number'] for p in sel], [412])
        self.assertEqual(sel[0]['task_id'], 'harden-test-prod-write-isolation-001')

    def test_selects_labeled_non_draft_pr(self):
        # The #509/#510/#625 gap: a non-draft PR carrying auto-review is now
        # routed, keyed by PR number.
        prs = [_pr(625, 'fix/install-drift-dedup', age_minutes=60,
                   labels=[h.AUTO_REVIEW_LABEL])]
        sel = h.select_orphaned_prs(prs, NOW, self._none_dispatched)
        self.assertEqual([p['number'] for p in sel], [625])
        self.assertEqual(sel[0]['task_id'], 'pr-625')

    def test_skips_unlabeled_non_forge_pr(self):
        # Safety: a non-draft non-forge PR with NO opt-in label is left alone —
        # it could be agent-authored (e.g. feat/new-mission-* must be CLOSED, not
        # merged). This is the blocker that killed the branch-only design.
        prs = [_pr(1, 'fix/some-agent-work', age_minutes=60),
               _pr(2, 'feat/new-mission-xyz', age_minutes=60)]
        self.assertEqual(h.select_orphaned_prs(prs, NOW, self._none_dispatched), [])

    def test_skips_labeled_draft_pr(self):
        # Draft is the "still iterating" safety valve — never auto-routed even when
        # labeled (a Mirror PASS auto-merges, so a draft must not be picked up).
        prs = [_pr(3, 'fix/wip', age_minutes=60, is_draft=True,
                   labels=[h.AUTO_REVIEW_LABEL])]
        self.assertEqual(h.select_orphaned_prs(prs, NOW, self._none_dispatched), [])

    def test_forge_pr_routed_even_if_draft(self):
        # Forge build PRs are reviewed unconditionally; draft state is ignored.
        prs = [_pr(11, 'forge/x', age_minutes=60, is_draft=True)]
        self.assertEqual([p['number'] for p in
                          h.select_orphaned_prs(prs, NOW, self._none_dispatched)], [11])

    def test_skips_pr_within_grace_window(self):
        # Created 5 min ago, grace is 10 min → too fresh, let inline path fire.
        prs = [_pr(3, 'forge/fresh', age_minutes=5)]
        self.assertEqual(h.select_orphaned_prs(prs, NOW, self._none_dispatched), [])

    def test_selects_at_grace_boundary(self):
        prs = [_pr(4, 'forge/edge', age_minutes=h.DISPATCH_GRACE_MINUTES + 1)]
        self.assertEqual([p['number'] for p in
                          h.select_orphaned_prs(prs, NOW, self._none_dispatched)], [4])

    def test_skips_already_dispatched(self):
        prs = [_pr(5, 'forge/done', age_minutes=60)]
        sel = h.select_orphaned_prs(
            prs, NOW, already_dispatched=lambda t, head=None: True)
        self.assertEqual(sel, [])

    def test_forwards_current_head_sha_to_probe(self):
        # The PR's headRefOid must reach the dedup probe so a review of an
        # OLDER head doesn't mask a needed re-review of the current head.
        seen = {}

        def probe(task_id, head_sha=None):
            seen['task_id'] = task_id
            seen['head_sha'] = head_sha
            return False

        pr = _pr(9, 'forge/x', age_minutes=60)
        pr['headRefOid'] = 'deadbeefcafe0000'
        sel = h.select_orphaned_prs([pr], NOW, probe)
        self.assertEqual([p['number'] for p in sel], [9])
        self.assertEqual(seen['head_sha'], 'deadbeefcafe0000')

    def test_re_reviews_when_only_older_head_dispatched(self):
        # Probe says "dispatched" only for the OLD head; current head differs →
        # the PR is selected for a fresh review (the head-drift fix).
        reviewed_head = 'aaaaaaaaaaaa'

        def probe(_task_id, head_sha=None):
            return head_sha == reviewed_head

        pr = _pr(10, 'forge/x', age_minutes=60)
        pr['headRefOid'] = 'bbbbbbbbbbbb'  # PR advanced past the reviewed head
        sel = h.select_orphaned_prs([pr], NOW, probe)
        self.assertEqual([p['number'] for p in sel], [10])
        # And when the current head IS the reviewed one, it's skipped.
        pr['headRefOid'] = reviewed_head
        self.assertEqual(h.select_orphaned_prs([pr], NOW, probe), [])

    def test_skips_unparseable_created_at(self):
        bad = _pr(6, 'forge/bad', age_minutes=60)
        bad['createdAt'] = 'not-a-date'
        self.assertEqual(h.select_orphaned_prs([bad], NOW, self._none_dispatched), [])

    def test_dispatched_probe_exception_treated_as_undispatched(self):
        def boom(_t, _head=None):
            raise RuntimeError('probe down')
        prs = [_pr(7, 'forge/x', age_minutes=60)]
        sel = h.select_orphaned_prs(prs, NOW, boom)
        self.assertEqual([p['number'] for p in sel], [7])  # fail-open to dispatch

    def test_custom_grace_minutes(self):
        prs = [_pr(8, 'forge/x', age_minutes=20)]
        # With a 30-min grace, a 20-min-old PR is still too fresh.
        self.assertEqual(
            h.select_orphaned_prs(prs, NOW, self._none_dispatched, grace_minutes=30), [])


class TestSynthesizeBuildData(unittest.TestCase):
    def test_envelope_shape(self):
        pr = {**_pr(412, 'forge/harden-x-001'), 'task_id': 'harden-x-001'}
        data = h._synthesize_build_data(pr)
        self.assertEqual(data['task_id'], 'harden-x-001')
        self.assertEqual(data['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(data['branch'], 'forge/harden-x-001')
        self.assertEqual(data['dispatched_by'], 'heal-undispatched-pr-review')
        # No claude_session_id — the build session is gone; revisions start fresh.
        self.assertNotIn('claude_session_id', data)

    def test_threads_head_sha_from_headRefOid(self):
        # The PR head we already have from gh-pr-list rides the envelope so the
        # dispatch records the right commit without a second gh round-trip.
        pr = {**_pr(412, 'forge/harden-x-001'), 'task_id': 'harden-x-001',
              'headRefOid': 'cafe1234beef5678'}
        self.assertEqual(
            h._synthesize_build_data(pr)['head_sha'], 'cafe1234beef5678')


class TestHealerEnabled(unittest.TestCase):
    def test_default_on(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.ENV_ENABLED, None)
            self.assertTrue(h.healer_enabled())

    def test_explicit_false_is_dry_run(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {h.ENV_ENABLED: 'false'}):
            self.assertFalse(h.healer_enabled())

    def test_explicit_true(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {h.ENV_ENABLED: 'true'}):
            self.assertTrue(h.healer_enabled())


class _FakeNotifier:
    """Stand-in for outbox_notifier: tracks which tasks have a dispatched review.
    `dispatch_succeeds` controls whether _dispatch_mirror_review actually records
    one (modelling RoutingDenied/missing-target_repo, which the real call swallows
    as a WARN and returns)."""

    def __init__(self, dispatch_succeeds=True, pr_open=True):
        self.dispatched: set[str] = set()
        self.dispatch_calls: list[tuple[dict, str]] = []
        self.dispatch_succeeds = dispatch_succeeds
        self.pr_open = pr_open  # for the TOCTOU recheck

    def _review_request_already_dispatched(self, fname, current_head_sha=None):
        return fname in self.dispatched

    def _dispatch_mirror_review(self, data, url):
        self.dispatch_calls.append((data, url))
        if self.dispatch_succeeds:
            self.dispatched.add(f'review-{data["task_id"]}.json')

    def _parse_pr_url(self, url):
        return ('Larry-Yatch/ourliberty-agent-core', 412)

    def _gh_pr_is_open(self, repo_coords, pr_number):
        return self.pr_open


class _FakeSafeWriteInbox:
    @staticmethod
    def canonical_inbox_name(name):
        return name


class TestMainDispatchFlow(unittest.TestCase):
    """Exercise main()'s effectful dispatch→verify→escalate-once flow with the
    notifier/safe_write_inbox deps injected via sys.modules."""

    @staticmethod
    def _pr_realnow(number, branch, age_minutes=60, *, is_draft=False, labels=()):
        # main() compares createdAt against the real wall clock, so these
        # fixtures must be dated relative to actual now (not the fixed NOW).
        created = (datetime.now(timezone.utc)
                   - timedelta(minutes=age_minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
        return {
            'number': number,
            'url': f'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/{number}',
            'headRefName': branch,
            'createdAt': created,
            'title': 't',
            'isDraft': is_draft,
            'labels': list(labels),
        }

    def _run_main(self, *, prs, dispatch_succeeds, enabled=True, failed=None,
                  pr_open=True):
        import os
        from unittest import mock
        fake_notifier = _FakeNotifier(dispatch_succeeds=dispatch_succeeds,
                                      pr_open=pr_open)
        state = {'failed_prs': dict(failed or {})}
        saved = {}
        alerts: list[dict] = []
        env = {h.ENV_ENABLED: 'true' if enabled else 'false'}
        with mock.patch.dict(sys.modules, {
                    'outbox_notifier': fake_notifier,
                    'safe_write_inbox': _FakeSafeWriteInbox,
                }), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(h, 'kill_switch_active', return_value=False), \
                mock.patch.object(h, 'heartbeat'), \
                mock.patch.object(h, 'fetch_open_prs', return_value=prs), \
                mock.patch.object(h, 'load_state', return_value=state), \
                mock.patch.object(h, 'save_state',
                                  side_effect=lambda s: saved.update(s)), \
                mock.patch.object(h, 'emit_failed_alert',
                                  side_effect=lambda pr: alerts.append(pr) or True):
            rc = h.main()
        return rc, fake_notifier, saved, alerts

    def test_successful_dispatch_no_alert(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(fn.dispatch_calls), 1)
        self.assertEqual(fn.dispatch_calls[0][0]['task_id'], 'harden-x-001')
        self.assertEqual(alerts, [])
        self.assertEqual(saved.get('failed_prs'), {})

    def test_failed_dispatch_escalates_once(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=False)
        self.assertEqual(len(fn.dispatch_calls), 1)
        self.assertEqual(len(alerts), 1)  # escalated
        self.assertIn(prs[0]['url'].rstrip('/'), saved.get('failed_prs', {}))

    def test_already_escalated_pr_not_re_alerted(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        url = prs[0]['url'].rstrip('/')
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=False, failed={url: 'earlier'})
        self.assertEqual(len(fn.dispatch_calls), 1)  # still attempts dispatch
        self.assertEqual(alerts, [])  # but does NOT re-page

    def test_dry_run_dispatches_nothing(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, enabled=False)
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])

    def test_recovered_pr_clears_prior_failure(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        url = prs[0]['url'].rstrip('/')
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, failed={url: 'earlier'})
        # Dispatch now succeeds → the stale failure record is cleared.
        self.assertNotIn(url, saved.get('failed_prs', {}))

    def test_toctou_pr_closed_since_listing_skips_dispatch(self):
        prs = [self._pr_realnow(412, 'forge/harden-x-001', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(
            prs=prs, dispatch_succeeds=True, pr_open=False)
        self.assertEqual(fn.dispatch_calls, [])  # merged/closed → not reviewed
        self.assertEqual(alerts, [])

    def test_gh_unavailable_is_clean_noop(self):
        rc, fn, saved, alerts = self._run_main(prs=None, dispatch_succeeds=True)
        self.assertEqual(rc, 0)
        self.assertEqual(fn.dispatch_calls, [])

    def test_labeled_pr_dispatched_with_pr_number_task_id(self):
        # End-to-end: a non-draft auto-review-labeled PR routes a Mirror review
        # keyed pr-<number>.
        prs = [self._pr_realnow(625, 'fix/install-drift-dedup', age_minutes=60,
                                labels=[h.AUTO_REVIEW_LABEL])]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(len(fn.dispatch_calls), 1)
        self.assertEqual(fn.dispatch_calls[0][0]['task_id'], 'pr-625')
        self.assertEqual(alerts, [])

    def test_unlabeled_pr_dispatches_nothing(self):
        # No opt-in label → left alone (the agent-PR safety case), end-to-end.
        prs = [self._pr_realnow(626, 'fix/some-work', age_minutes=60)]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])

    def test_labeled_draft_pr_dispatches_nothing(self):
        prs = [self._pr_realnow(627, 'fix/wip', age_minutes=60, is_draft=True,
                                labels=[h.AUTO_REVIEW_LABEL])]
        rc, fn, saved, alerts = self._run_main(prs=prs, dispatch_succeeds=True)
        self.assertEqual(fn.dispatch_calls, [])
        self.assertEqual(alerts, [])


if __name__ == '__main__':
    unittest.main()
