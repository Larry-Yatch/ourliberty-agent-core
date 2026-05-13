#!/usr/bin/env python3
"""Fixtures for `mirror_review_handler` — review marker grammar + budget.

Phase D3.5 commit 5a (`D3.5-mirror-review` part 1). Covers:
  - Marker extraction (REVIEW_PASS, REVIEW_REVISION, REVIEW_ESCALATE,
    REVIEW_EMERGENCY_HALT) with narrative stripping
  - Malformed-JSON, missing-field, multi-marker rejection paths
  - Severity / confidence vocabulary enforcement
  - REVIEW_REVISION empty-findings rejection (should have been PASS)
  - Confidence-promote rule: low-confidence REVISION → ESCALATE
  - Revision-budget evaluation (allow vs exhausted) under default and
    explicit max values, with non-int / negative tolerances (forward-compat
    for 5b)
  - Routing helpers (notify-source, intent mapping with auto-promote)

Mirrors test_forge_preflight_handler.py shape; this is the second instance
of the same test pattern.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_mirror_review_handler
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import mirror_review_handler as mrh  # noqa: E402


PR_URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'


# -------------------- marker fixtures --------------------

def _pass_marker(task_id='t-1', summary='AC coverage clean; no findings.'):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL, 'summary': summary,
    })
    return f'=== REVIEW_PASS ===\n{payload}\n=== END_REVIEW_PASS ==='


def _revision_marker(
    task_id='t-1', severity='medium', confidence='high',
    findings=None,
):
    if findings is None:
        findings = [{
            'file': 'scripts/foo.py', 'line_range': 'L12-L15',
            'severity': 'medium',
            'description': 'Missing input validation on path.',
        }]
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL,
        'findings': findings, 'severity': severity, 'confidence': confidence,
    })
    return f'=== REVIEW_REVISION ===\n{payload}\n=== END_REVIEW_REVISION ==='


def _escalate_marker(
    task_id='t-1', severity='high', confidence='high',
    reason='Spec required X but diff implements Y; not a fix-in-place.',
):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL,
        'reason': reason, 'severity': severity, 'confidence': confidence,
    })
    return f'=== REVIEW_ESCALATE ===\n{payload}\n=== END_REVIEW_ESCALATE ==='


def _emergency_marker(
    task_id='t-1',
    reason='Diff adds AWS credentials in plaintext to config/secrets.json.',
    evidence='+    "aws_access_key": "AKIA...."',
):
    payload = json.dumps({
        'task_id': task_id, 'pr_url': PR_URL,
        'reason': reason, 'evidence': evidence,
    })
    return (
        f'=== REVIEW_EMERGENCY_HALT ===\n{payload}\n'
        f'=== END_REVIEW_EMERGENCY_HALT ==='
    )


# -------------------- parse_mirror_marker --------------------

class ParseMirrorMarkerTest(unittest.TestCase):
    """Marker extraction with narrative stripping; vocab validation."""

    def test_extracts_review_pass(self):
        narrative_above = 'Read spec + diff. Coverage clean.\n\n'
        response = narrative_above + _pass_marker(task_id='watchdog-doc-fix-001')
        mtype, payload, narr = mrh.parse_mirror_marker(response)
        self.assertEqual(mtype, 'review_pass')
        self.assertEqual(payload['task_id'], 'watchdog-doc-fix-001')
        self.assertEqual(payload['pr_url'], PR_URL)
        self.assertIn('coverage clean', payload['summary'].lower())
        self.assertNotIn('=== REVIEW_PASS', narr)
        self.assertIn('Read spec', narr)

    def test_extracts_review_revision(self):
        response = 'Found a missing-validation issue.\n\n' + _revision_marker()
        mtype, payload, narr = mrh.parse_mirror_marker(response)
        self.assertEqual(mtype, 'review_revision')
        self.assertEqual(payload['severity'], 'medium')
        self.assertEqual(payload['confidence'], 'high')
        self.assertEqual(len(payload['findings']), 1)
        self.assertNotIn('=== REVIEW_REVISION', narr)

    def test_extracts_review_escalate(self):
        response = _escalate_marker(reason='Spec mismatch.')
        mtype, payload, _ = mrh.parse_mirror_marker(response)
        self.assertEqual(mtype, 'review_escalate')
        self.assertEqual(payload['severity'], 'high')

    def test_extracts_review_emergency_halt(self):
        response = _emergency_marker()
        mtype, payload, _ = mrh.parse_mirror_marker(response)
        self.assertEqual(mtype, 'review_emergency_halt')
        self.assertIn('credentials', payload['reason'])
        self.assertIn('AKIA', payload['evidence'])

    def test_no_marker_returns_none(self):
        for response in [
            '', None,
            'No marker block here.',
            'Started writing === REVIEW_PASS but never closed it',
            '=== REVIEW_PASS ===\nbut no JSON',
        ]:
            mtype, payload, narr = mrh.parse_mirror_marker(response)
            self.assertIsNone(mtype, f'should not match: {response!r}')
            self.assertIsNone(payload)

    def test_invalid_json_raises(self):
        response = (
            '=== REVIEW_PASS ===\n'
            '{not valid json, oops}\n'
            '=== END_REVIEW_PASS ==='
        )
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('invalid JSON', str(ctx.exception))

    def test_missing_required_field_raises(self):
        # REVIEW_PASS without `summary`
        bad_payload = json.dumps({'task_id': 't-1', 'pr_url': PR_URL})
        response = (
            f'=== REVIEW_PASS ===\n{bad_payload}\n=== END_REVIEW_PASS ==='
        )
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('missing required fields', str(ctx.exception))
        self.assertIn('summary', str(ctx.exception))

    def test_missing_pr_url_on_revision_raises(self):
        bad_payload = json.dumps({
            'task_id': 't-1', 'findings': [{'file': 'x'}],
            'severity': 'medium', 'confidence': 'high',
        })
        response = (
            f'=== REVIEW_REVISION ===\n{bad_payload}\n'
            f'=== END_REVIEW_REVISION ==='
        )
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('pr_url', str(ctx.exception))

    def test_invalid_severity_raises(self):
        response = _revision_marker(severity='spicy')
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        # Phrasing now lists the per-marker allowed set; check for the
        # severity token rather than the literal old wording.
        self.assertIn("'spicy'", str(ctx.exception))

    def test_revision_severity_high_rejected(self):
        # m-1 review fix: REVISION top-level severity is low|medium only.
        # high belongs to ESCALATE; severity=high on REVISION means Mirror
        # mis-categorized (the right marker is ESCALATE).
        response = _revision_marker(severity='high')
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('ESCALATE', str(ctx.exception))

    def test_revision_severity_critical_rejected(self):
        # m-1 review fix: critical belongs to EMERGENCY_HALT.
        response = _revision_marker(severity='critical')
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('EMERGENCY_HALT', str(ctx.exception))

    def test_escalate_severity_low_rejected(self):
        # m-1 review fix: ESCALATE is severity=high only.
        response = _escalate_marker(severity='low')
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn("'low'", str(ctx.exception))

    def test_escalate_severity_medium_rejected(self):
        # m-1 review fix: medium belongs to REVISION.
        response = _escalate_marker(severity='medium')
        with self.assertRaises(mrh.MalformedMirrorMarker):
            mrh.parse_mirror_marker(response)

    def test_invalid_confidence_raises(self):
        response = _revision_marker(confidence='vibes')
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('invalid confidence', str(ctx.exception))

    def test_empty_findings_on_revision_raises(self):
        response = _revision_marker(findings=[])
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('should have been REVIEW_PASS', str(ctx.exception))

    def test_findings_not_list_raises(self):
        bad_payload = json.dumps({
            'task_id': 't-1', 'pr_url': PR_URL,
            'findings': 'not-a-list',
            'severity': 'medium', 'confidence': 'high',
        })
        response = (
            f'=== REVIEW_REVISION ===\n{bad_payload}\n'
            f'=== END_REVIEW_REVISION ==='
        )
        with self.assertRaises(mrh.MalformedMirrorMarker):
            mrh.parse_mirror_marker(response)

    def test_emergency_halt_does_not_require_severity_or_confidence(self):
        # EMERGENCY_HALT carries `reason` + `evidence` only; severity is
        # implicit (the marker type encodes critical+safety).
        response = _emergency_marker()
        mtype, payload, _ = mrh.parse_mirror_marker(response)
        self.assertEqual(mtype, 'review_emergency_halt')
        # No severity / confidence required; parser shouldn't reject for
        # their absence.
        self.assertNotIn('severity', payload)


class MultipleMarkerRejectionTest(unittest.TestCase):
    """One marker per response — multi-marker outputs dead-letter to Mirror."""

    def test_heterogeneous_multi_raises(self):
        response = _pass_marker() + '\n\n' + _revision_marker()
        with self.assertRaises(mrh.MultipleMirrorMarkers) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('2 marker blocks', str(ctx.exception))
        # Both kinds named in the error so Mirror sees what to drop.
        self.assertIn('review_pass', str(ctx.exception))
        self.assertIn('review_revision', str(ctx.exception))

    def test_homogeneous_multi_raises(self):
        # Two PASS markers in one response — equally ambiguous.
        response = _pass_marker(task_id='a') + '\n' + _pass_marker(task_id='b')
        with self.assertRaises(mrh.MultipleMirrorMarkers) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('review_pass=2', str(ctx.exception))

    def test_three_different_markers_raises(self):
        response = (
            _pass_marker() + '\n' + _revision_marker() + '\n' + _escalate_marker()
        )
        with self.assertRaises(mrh.MultipleMirrorMarkers) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('3 marker blocks', str(ctx.exception))


# -------------------- confidence-promote rule --------------------

class AutoPromoteTest(unittest.TestCase):
    """REVIEW_REVISION with confidence:low routes as ESCALATE."""

    def test_revision_low_confidence_promotes(self):
        response = _revision_marker(confidence='low')
        _, payload, _ = mrh.parse_mirror_marker(response)
        self.assertTrue(mrh.should_auto_promote('review_revision', payload))

    def test_revision_high_confidence_does_not_promote(self):
        response = _revision_marker(confidence='high')
        _, payload, _ = mrh.parse_mirror_marker(response)
        self.assertFalse(mrh.should_auto_promote('review_revision', payload))

    def test_revision_medium_confidence_does_not_promote(self):
        response = _revision_marker(confidence='medium')
        _, payload, _ = mrh.parse_mirror_marker(response)
        self.assertFalse(mrh.should_auto_promote('review_revision', payload))

    def test_escalate_never_promotes(self):
        # Already at escalation tier; no further promotion shape exists.
        response = _escalate_marker(confidence='low')
        _, payload, _ = mrh.parse_mirror_marker(response)
        self.assertFalse(mrh.should_auto_promote('review_escalate', payload))

    def test_pass_never_promotes(self):
        response = _pass_marker()
        _, payload, _ = mrh.parse_mirror_marker(response)
        self.assertFalse(mrh.should_auto_promote('review_pass', payload))

    def test_emergency_never_promotes(self):
        response = _emergency_marker()
        _, payload, _ = mrh.parse_mirror_marker(response)
        self.assertFalse(mrh.should_auto_promote('review_emergency_halt', payload))


# -------------------- revision-budget (forward-compat for 5b) --------------------

class EvaluateRevisionBudgetTest(unittest.TestCase):
    """Stateless budget evaluation; 5b uses this to gate revision dispatches."""

    def test_first_round_allow(self):
        envelope = {'revision_count': 0, 'max_revisions': 3}
        decision, next_count, max_count = mrh.evaluate_revision_budget(envelope)
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 1)
        self.assertEqual(max_count, 3)

    def test_at_max_exhausted(self):
        envelope = {'revision_count': 3, 'max_revisions': 3}
        decision, _, _ = mrh.evaluate_revision_budget(envelope)
        self.assertEqual(decision, 'exhausted')

    def test_above_max_exhausted(self):
        envelope = {'revision_count': 5, 'max_revisions': 3}
        decision, _, _ = mrh.evaluate_revision_budget(envelope)
        self.assertEqual(decision, 'exhausted')

    def test_default_max_when_missing(self):
        decision, _, max_count = mrh.evaluate_revision_budget(
            {'revision_count': 0},
        )
        self.assertEqual(decision, 'allow')
        self.assertEqual(max_count, mrh.DEFAULT_MAX_REVISIONS)

    def test_default_count_when_missing(self):
        decision, next_count, _ = mrh.evaluate_revision_budget(
            {'max_revisions': 5},
        )
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 1)

    def test_negative_count_treated_as_zero(self):
        decision, next_count, _ = mrh.evaluate_revision_budget(
            {'revision_count': -1, 'max_revisions': 3},
        )
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 1)

    def test_non_int_max_treated_as_default(self):
        decision, _, max_count = mrh.evaluate_revision_budget(
            {'revision_count': 0, 'max_revisions': 'three'},
        )
        self.assertEqual(decision, 'allow')
        self.assertEqual(max_count, mrh.DEFAULT_MAX_REVISIONS)


class RevisionsRemainingTest(unittest.TestCase):

    def test_full_budget_when_count_zero(self):
        self.assertEqual(
            mrh.revisions_remaining({'revision_count': 0, 'max_revisions': 3}),
            3,
        )

    def test_partial_budget(self):
        self.assertEqual(
            mrh.revisions_remaining({'revision_count': 2, 'max_revisions': 3}),
            1,
        )

    def test_zero_when_exhausted(self):
        self.assertEqual(
            mrh.revisions_remaining({'revision_count': 5, 'max_revisions': 3}),
            0,
        )


# -------------------- routing helpers --------------------

class DeriveIntentTest(unittest.TestCase):

    def test_review_pass_intent(self):
        self.assertEqual(mrh.derive_intent('review_pass'), 'review-pass')

    def test_review_revision_intent(self):
        self.assertEqual(
            mrh.derive_intent('review_revision'), 'review-revision',
        )

    def test_review_revision_auto_promoted_becomes_escalate(self):
        self.assertEqual(
            mrh.derive_intent('review_revision', auto_promoted=True),
            'review-escalate',
        )

    def test_review_revision_budget_exhausted_becomes_escalate(self):
        # D3.5 5b: budget_exhausted downgrades same as auto_promoted.
        self.assertEqual(
            mrh.derive_intent('review_revision', budget_exhausted=True),
            'review-escalate',
        )

    def test_review_revision_both_flags_becomes_escalate(self):
        # Both flags true (Mirror low-confidence AND budget exhausted) still
        # routes as escalate — no ordering subtlety.
        self.assertEqual(
            mrh.derive_intent(
                'review_revision', auto_promoted=True, budget_exhausted=True,
            ),
            'review-escalate',
        )

    def test_review_escalate_intent(self):
        self.assertEqual(
            mrh.derive_intent('review_escalate'), 'review-escalate',
        )

    def test_review_emergency_halt_intent(self):
        self.assertEqual(
            mrh.derive_intent('review_emergency_halt'),
            'review-emergency-halt',
        )

    def test_unknown_marker_falls_back_to_generic(self):
        # Defensive: future marker types shouldn't crash the classifier.
        self.assertEqual(
            mrh.derive_intent('review_what_is_this'),
            'result-notification',
        )


class DeriveNotifySourceTest(unittest.TestCase):

    def test_default_returns_mirror_result(self):
        self.assertEqual(mrh.derive_notify_source(), 'mirror-result')

    def test_custom_agent_id(self):
        # Defensive: signature accepts an agent_id for parity with the
        # Forge handler, though Mirror is the only caller in 5a.
        self.assertEqual(
            mrh.derive_notify_source('mirror'), 'mirror-result',
        )


class BuildAutoPromoteReasonTest(unittest.TestCase):

    def test_includes_finding_count(self):
        payload = {
            'findings': [
                {'file': 'a', 'severity': 'medium'},
                {'file': 'b', 'severity': 'low'},
            ],
            'confidence': 'low',
        }
        reason = mrh.build_auto_promote_reason(payload)
        self.assertIn('2 finding', reason)
        self.assertIn('low', reason)
        self.assertIn('ESCALATE', reason)

    def test_zero_findings_does_not_crash(self):
        # parse_mirror_marker rejects empty findings, so this branch shouldn't
        # fire in practice — but the helper shouldn't blow up if called
        # defensively.
        reason = mrh.build_auto_promote_reason({'findings': []})
        self.assertIn('0 finding', reason)


class BuildBudgetExhaustedReasonTest(unittest.TestCase):
    """D3.5 5b: budget-exhausted reason includes finding context + budget figures."""

    def test_includes_round_and_max(self):
        payload = {
            'findings': [{'file': 'a'}, {'file': 'b'}],
            'severity': 'medium',
        }
        reason = mrh.build_budget_exhausted_reason(payload, next_count=4, max_count=3)
        self.assertIn('2 finding', reason)
        self.assertIn('medium', reason)
        self.assertIn('reach 4', reason)
        self.assertIn('budget of 3', reason)
        self.assertIn('ESCALATE', reason)

    def test_zero_findings_does_not_crash(self):
        reason = mrh.build_budget_exhausted_reason(
            {'findings': []}, next_count=4, max_count=3,
        )
        self.assertIn('0 finding', reason)


if __name__ == '__main__':
    unittest.main()
