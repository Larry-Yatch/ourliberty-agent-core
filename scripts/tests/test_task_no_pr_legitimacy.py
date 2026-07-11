#!/usr/bin/env python3
"""Per-rule coverage for task_no_pr_legitimacy.expects_no_pr — the shared
"did this task legitimately conclude without a PR?" classifier. Verifies each
verdict rule and, critically, the precedence ordering (outbox-truth must win
over the coarse ``build-`` prefix for build-SEQUENCE preflight tasks)."""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import task_no_pr_legitimacy as tnpl  # noqa: E402
import task_terminal_state as tts  # noqa: E402


class _TmpLogMixin(unittest.TestCase):
    """Redirect the UNKNOWN observability log into a temp dir so per-rule tests
    never touch the real /home/larry/agents/logs tree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get('OURLIBERTY_LOG_DIR')
        os.environ['OURLIBERTY_LOG_DIR'] = self._tmp.name

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
        else:
            os.environ['OURLIBERTY_LOG_DIR'] = self._prev
        self._tmp.cleanup()


class LegitNoPrShapeRules(_TmpLogMixin):

    def test_strong_legit_prefixes(self) -> None:
        for tid, prefix in [
            ('mirror-review-pr-ourliberty-agent-core-931', 'mirror-review-'),
            ('review-sequence-dag-endpoint-adoption', 'review-sequence-dag-'),
            ('dag-preflight-endpoint-adoption', 'dag-preflight-'),
            ('review-shape-drift-001', 'review-'),
            ('kickoff-build-seq-endpoint', 'kickoff-'),
            ('notify-crit-hold-001', 'notify-'),
        ]:
            with self.subTest(tid=tid):
                verdict, reason = tnpl.expects_no_pr(tid)
                self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
                self.assertEqual(reason, f'shape:{prefix}*')

    def test_weak_preflight_clarify_pointer_no_outbox(self) -> None:
        for tid in ('endpoint-adoption-preflight',
                    'endpoint-adoption-clarify'):
            with self.subTest(tid=tid):
                verdict, reason = tnpl.expects_no_pr(tid)
                self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
                self.assertEqual(reason, 'shape:preflight-or-clarify-pointer')


class ExpectsPrShapeRules(_TmpLogMixin):

    def test_expects_pr_prefixes(self) -> None:
        for tid, prefix in [
            ('build-endpoint-adoption', 'build-'),
            ('opmanual-update-runbook', 'opmanual-'),
        ]:
            with self.subTest(tid=tid):
                verdict, reason = tnpl.expects_no_pr(tid)
                self.assertEqual(verdict, tnpl.EXPECTS_PR)
                self.assertEqual(reason, f'shape:{prefix}*')

    def test_task_type_expects_pr(self) -> None:
        for tt in ('doc-only', 'build'):
            with self.subTest(tt=tt):
                verdict, reason = tnpl.expects_no_pr(
                    'some-task-001', outbox={'task_type': tt})
                self.assertEqual(verdict, tnpl.EXPECTS_PR)
                self.assertEqual(reason, f'task_type:{tt}')

    def test_task_type_legit_no_pr(self) -> None:
        for tt in ('direction-ask', 'notification', 'review'):
            with self.subTest(tt=tt):
                verdict, reason = tnpl.expects_no_pr(
                    'some-task-001', outbox={'task_type': tt})
                self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
                self.assertEqual(reason, f'task_type:{tt}')


class OutboxTruthRules(_TmpLogMixin):

    def test_reject_and_clarify_markers_are_legit(self) -> None:
        for marker, label in [
            ('=== REJECT_REQUEST ===', 'outbox:reject_request'),
            ('=== CLARIFY_REQUEST ===', 'outbox:clarify_request'),
        ]:
            with self.subTest(marker=marker):
                verdict, reason = tnpl.expects_no_pr(
                    'x-001', outbox={'result': f'narr {marker} more'})
                self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
                self.assertEqual(reason, label)

    def test_proceed_then_no_pr_expects_pr(self) -> None:
        verdict, reason = tnpl.expects_no_pr(
            'x-001', outbox={'result': 'ack === PROCEED === building'})
        self.assertEqual(verdict, tnpl.EXPECTS_PR)
        self.assertEqual(reason, 'outbox:proceed-then-no-pr')

    def test_errored_outbox_expects_pr(self) -> None:
        verdict, reason = tnpl.expects_no_pr(
            'x-001', outbox={'exit_code': 1, 'result': 'boom'})
        self.assertEqual(verdict, tnpl.EXPECTS_PR)
        self.assertEqual(reason, 'outbox:consumed-but-errored')

    def test_preflight_exit_outbox_is_legit(self) -> None:
        verdict, reason = tnpl.expects_no_pr(
            'x-001',
            outbox={'phase': 'preflight', 'exit_code': 0, 'attempts': 1,
                    'result': 'narration only, no marker delimiter'})
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'outbox:preflight_exit')

    def test_no_buildable_delta_prose_is_legit(self) -> None:
        verdict, reason = tnpl.expects_no_pr(
            'x-001', outbox={'result': 'concluded: no buildable delta here'})
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'outbox:no-buildable-delta')


class PrecedenceOrdering(_TmpLogMixin):
    """The load-bearing ordering: outbox-truth must beat the coarse build-
    prefix so a build-SEQUENCE preflight task (build- name, preflight-exit
    outbox) resolves LEGIT, not misclassified as a PR-bearing build."""

    def test_build_sequence_preflight_exit_beats_build_prefix(self) -> None:
        verdict, reason = tnpl.expects_no_pr(
            'build-sequence-orchestrator-pr-s1-spec-adoption',
            outbox={'phase': 'preflight', 'exit_code': 0, 'attempts': 1,
                    'result': 'preflight narration, chose not to build'})
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'outbox:preflight_exit')

    def test_build_sequence_reject_beats_build_prefix(self) -> None:
        verdict, reason = tnpl.expects_no_pr(
            'build-sequence-orchestrator-pr-s1-spec-adoption',
            outbox={'result': '=== REJECT_REQUEST === out of scope'})
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'outbox:reject_request')

    def test_errored_build_sequence_preflight_still_expects_pr(self) -> None:
        # An ERRORED preflight (non-zero exit) is a real failure, not a clean
        # no-PR — the errored rule must beat the weak preflight-pointer shape.
        verdict, reason = tnpl.expects_no_pr(
            'orchestrator-pr-s1-spec-adoption-preflight',
            outbox={'phase': 'preflight', 'exit_code': 2,
                    'result': 'crashed mid-preflight'})
        self.assertEqual(verdict, tnpl.EXPECTS_PR)
        self.assertEqual(reason, 'outbox:consumed-but-errored')

    def test_strong_legit_prefix_ignores_outbox(self) -> None:
        # A mirror-review task with an (implausible) errored outbox is still
        # LEGIT by shape — strong prefixes short-circuit before outbox-truth.
        verdict, reason = tnpl.expects_no_pr(
            'mirror-review-pr-ourliberty-agent-core-931',
            outbox={'exit_code': 1, 'result': 'noise'})
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'shape:mirror-review-*')


class TargetPrOperatingRule(_TmpLogMixin):
    """rebase-*/resolve-* tasks force-push an EXISTING PR #N and open no PR of
    their own, so legitimacy tracks PR #N's terminal state. The gh probe
    (task_terminal_state) is mocked so no live gh call is made and the
    verdict-per-target-state mapping is exercised deterministically."""

    def setUp(self) -> None:
        super().setUp()
        p_repos = mock.patch.object(
            tts, 'default_repos', return_value=['ourliberty-agent-core'])
        p_qual = mock.patch.object(
            tts, '_qualify_repo', side_effect=lambda r: r)
        p_repos.start()
        self.addCleanup(p_repos.stop)
        p_qual.start()
        self.addCleanup(p_qual.stop)

    def _probe(self, *states: str) -> None:
        it = iter(states)
        p = mock.patch.object(
            tts, 'pr_coordinate_state',
            side_effect=lambda repo, number, **kw: (next(it), None))
        p.start()
        self.addCleanup(p.stop)

    def _forbid_probe(self) -> None:
        def boom(*a, **k):
            raise AssertionError('gh probe must NOT happen on this path')
        p = mock.patch.object(tts, 'pr_coordinate_state', side_effect=boom)
        p.start()
        self.addCleanup(p.stop)

    def test_rebase_target_merged_is_legit(self) -> None:
        self._probe(tts.MERGED)
        verdict, reason = tnpl.expects_no_pr('rebase-pr-860-001')
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'target-pr#860-terminal')

    def test_rebase_target_closed_is_legit(self) -> None:
        self._probe(tts.CLOSED)
        verdict, reason = tnpl.expects_no_pr('rebase-pr-860-001')
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'target-pr#860-terminal')

    def test_rebase_retry_target_open_expects_pr(self) -> None:
        self._probe(tts.OPEN)
        verdict, reason = tnpl.expects_no_pr('rebase-pr-860-001-retry1')
        self.assertEqual(verdict, tnpl.EXPECTS_PR)
        self.assertEqual(reason, 'target-pr#860-open')

    def test_resolve_target_terminal_is_legit(self) -> None:
        self._probe(tts.MERGED)
        verdict, reason = tnpl.expects_no_pr('resolve-pr123')
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'target-pr#123-terminal')

    def test_target_unknown_state_stays_unknown(self) -> None:
        self._probe(tts.UNKNOWN)
        verdict, reason = tnpl.expects_no_pr('rebase-forge-pr-42')
        self.assertEqual(verdict, tnpl.UNKNOWN)
        self.assertEqual(reason, 'target-pr#42-unknown')

    def test_open_wins_over_terminal_across_repos(self) -> None:
        # A same-number PR still OPEN in ANY tracked repo blocks suppression.
        p_repos = mock.patch.object(
            tts, 'default_repos', return_value=['repo-a', 'repo-b'])
        p_repos.start()
        self.addCleanup(p_repos.stop)
        self._probe(tts.MERGED, tts.OPEN)
        verdict, reason = tnpl.expects_no_pr('rebase-pr-500-001')
        self.assertEqual(verdict, tnpl.EXPECTS_PR)
        self.assertEqual(reason, 'target-pr#500-open')

    def test_rebase_without_pr_number_is_unknown(self) -> None:
        # No parseable pr<N>: no probe, conservative UNKNOWN default.
        self._forbid_probe()
        verdict, reason = tnpl.expects_no_pr('rebase-some-branch-cleanup')
        self.assertEqual(verdict, tnpl.UNKNOWN)
        self.assertEqual(reason, 'unclassified-shape')

    def test_mirror_review_keeps_fastpath_no_probe(self) -> None:
        # mirror-review is the review-shaped target-PR member but keeps its
        # unconditional strong-shape verdict — no gh probe, no behavior change.
        self._forbid_probe()
        verdict, reason = tnpl.expects_no_pr(
            'mirror-review-pr-ourliberty-agent-core-931')
        self.assertEqual(verdict, tnpl.LEGIT_NO_PR)
        self.assertEqual(reason, 'shape:mirror-review-*')


class UnknownDefault(_TmpLogMixin):

    def test_genuine_build_stem_is_unknown(self) -> None:
        verdict, reason = tnpl.expects_no_pr('reconcile-hardening-mission-001')
        self.assertEqual(verdict, tnpl.UNKNOWN)
        self.assertEqual(reason, 'unclassified-shape')

    def test_empty_or_non_str_is_unknown(self) -> None:
        for bad in ('', None, 123):
            with self.subTest(bad=bad):
                verdict, reason = tnpl.expects_no_pr(bad)  # type: ignore[arg-type]
                self.assertEqual(verdict, tnpl.UNKNOWN)
                self.assertEqual(reason, 'empty-or-non-str-task-id')

    def test_unknown_verdict_is_logged(self) -> None:
        tnpl.expects_no_pr('reconcile-hardening-mission-001')
        log = Path(self._tmp.name) / 'task_no_pr_legitimacy.log'
        self.assertTrue(log.exists())
        body = log.read_text()
        self.assertIn('reconcile-hardening-mission-001', body)
        self.assertIn('unclassified-shape', body)

    def test_log_failure_never_raises(self) -> None:
        # A read-only / missing log FS must not perturb the verdict.
        os.environ['OURLIBERTY_LOG_DIR'] = '/proc/nonexistent/cannot-write'
        verdict, _ = tnpl.expects_no_pr('reconcile-hardening-mission-001')
        self.assertEqual(verdict, tnpl.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
