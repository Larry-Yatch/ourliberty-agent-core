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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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
        # Inputs with NO delimiter at all. 5b-followup Bug A added a
        # diagnostic raise when delimiters are present but JSON is missing
        # — those cases moved to test_loose_delimiter_* tests below.
        for response in [
            '', None,
            'No marker block here.',
            'Just chat output, no review markers.',
        ]:
            mtype, payload, narr = mrh.parse_mirror_marker(response)
            self.assertIsNone(mtype, f'should not match: {response!r}')
            self.assertIsNone(payload)

    def test_loose_delimiter_with_prose_raises_diagnostic(self):
        # D3.5 5b-followup Bug A: same shape as Forge's loose-delimiter
        # detection. Mirror writes prose between delimiters → strict
        # regex misses → loose detector fires actionable error.
        response = (
            'Reviewed the PR diff.\n\n'
            '=== REVIEW_PASS ===\n'
            'Coverage clean, approved.\n'
            '=== END_REVIEW_PASS ==='
        )
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        msg = str(ctx.exception)
        self.assertIn('JSON object', msg)
        self.assertIn('NOT prose', msg)

    def test_loose_delimiter_with_unterminated_block_raises_diagnostic(self):
        response = (
            'Some narrative.\n'
            '=== REVIEW_REVISION ===\n'
            '{"task_id": "x"}'  # no closing block
        )
        with self.assertRaises(mrh.MalformedMirrorMarker):
            mrh.parse_mirror_marker(response)

    def test_bare_review_pass_keyword_raises_diagnostic(self):
        # D3.5 5d-followup (auto-merge-gap-pr16-001): the PR #16 failure
        # shape. Mirror ended her response with bare `REVIEW_PASS` on its
        # own line, no `===` delimiters, no JSON. Pre-fix the parser
        # silently returned (None, None, ...); outbox_notifier fell through
        # past the auto-merge block and the PR sat unmerged for 7h+.
        response = (
            'Verified diff is purely additive; hashes resolve on main.\n'
            'Shape matches prior doc-only PRs (#13, #14, #15).\n'
            '\n'
            'REVIEW_PASS'
        )
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        msg = str(ctx.exception)
        self.assertIn('Bare marker keyword', msg)
        self.assertIn('REVIEW_PASS', msg)
        self.assertIn('=== REVIEW_PASS ===', msg)

    def test_bare_review_revision_keyword_raises_diagnostic(self):
        response = 'Found a missing-validation issue.\nREVIEW_REVISION\n'
        with self.assertRaises(mrh.MalformedMirrorMarker) as ctx:
            mrh.parse_mirror_marker(response)
        self.assertIn('Bare marker keyword', str(ctx.exception))
        self.assertIn('REVIEW_REVISION', str(ctx.exception))

    def test_bare_keyword_with_trailing_whitespace_raises(self):
        # Trailing whitespace / newlines around the bare keyword must not
        # let it slip past the detector — Mirror's outputs commonly end
        # with a newline.
        response = 'Reviewed.\n\n  REVIEW_PASS  \n\n'
        with self.assertRaises(mrh.MalformedMirrorMarker):
            mrh.parse_mirror_marker(response)

    def test_keyword_mid_sentence_does_not_trigger_bare_check(self):
        # Defensive: a marker keyword appearing inside narrative prose
        # ("I considered REVIEW_REVISION but...") must NOT trip the bare
        # check — it isn't alone on its line. Without the marker block,
        # this is just chat-mode output and should return (None, None, ...).
        response = (
            'I considered REVIEW_REVISION here but the diff is clean, '
            'so this would be REVIEW_PASS territory.'
        )
        mtype, payload, _ = mrh.parse_mirror_marker(response)
        self.assertIsNone(mtype)
        self.assertIsNone(payload)

    def test_well_formed_marker_with_keyword_above_not_misdetected(self):
        # Regression guard: when a properly-formed marker block IS present,
        # the bare-keyword detector must not fire on the keyword inside the
        # `=== KEYWORD ===` opener (because the strict regex matched first
        # and the bare-keyword check only runs on the no-match branch).
        response = 'Narrative above.\n\n' + _pass_marker()
        mtype, payload, _ = mrh.parse_mirror_marker(response)
        self.assertEqual(mtype, 'review_pass')
        self.assertIsNotNone(payload)

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


# -------------------- render_marker (E1.1) --------------------

class RenderMarkerTest(unittest.TestCase):
    """Render path tests + render <-> parse round-trip (E1.1).

    Complement the parse tests above. If MARKER_KEYWORDS drifts from the
    parser's regex source, the round-trip cases fail. If a new marker type
    is added to MARKER_TYPES without extending MARKER_KEYWORDS or
    REQUIRED_FIELDS, the schema-coverage test fails.

    The cross-handler drift detector lives in test_marker_drift.py.
    """

    def _payloads(self) -> dict:
        return {
            'review_pass': {
                'task_id': 't-001', 'pr_url': 'https://github.com/x/y/pull/1',
                'summary': 'change is correct and complete',
            },
            'review_revision': {
                'task_id': 't-001', 'pr_url': 'https://github.com/x/y/pull/1',
                'findings': [{'file': 'a.py', 'line': 1, 'issue': 'typo'}],
                'severity': 'medium', 'confidence': 'high',
            },
            'review_escalate': {
                'task_id': 't-001', 'pr_url': 'https://github.com/x/y/pull/1',
                'reason': 'spec is internally inconsistent',
                'severity': 'high', 'confidence': 'high',
            },
            'review_emergency_halt': {
                'task_id': 't-001', 'pr_url': 'https://github.com/x/y/pull/1',
                'reason': 'plaintext credential detected in diff',
                'evidence': 'OPENAI_API_KEY=sk-abc... in config.py:12',
            },
        }

    def test_render_then_parse_roundtrip(self):
        for mtype, payload in self._payloads().items():
            with self.subTest(mtype=mtype):
                rendered = mrh.render_marker(mtype, **payload)
                ptype, ppayload, narrative = mrh.parse_mirror_marker(rendered)
                self.assertEqual(ptype, mtype)
                self.assertEqual(ppayload, payload)
                self.assertEqual(narrative.strip(), '')

    def test_unknown_marker_type_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'unknown marker type'):
            mrh.render_marker('bogus', task_id='t-001')

    def test_missing_required_field_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'missing required fields'):
            mrh.render_marker(
                'review_pass', task_id='t-001', pr_url='https://x/y/pull/1',
            )  # missing summary

    def test_extra_fields_preserved_in_payload(self):
        rendered = mrh.render_marker(
            'review_pass',
            task_id='t-001', pr_url='https://x/y/pull/1', summary='ok',
            notes='all green on CI',
        )
        _, parsed, _ = mrh.parse_mirror_marker(rendered)
        self.assertEqual(parsed.get('notes'), 'all green on CI')

    def test_keyword_and_required_fields_cover_every_marker_type(self):
        for mtype in mrh.MARKER_TYPES:
            with self.subTest(mtype=mtype):
                self.assertIn(mtype, mrh.MARKER_KEYWORDS)
                self.assertIn(mtype, mrh.REQUIRED_FIELDS)

    # ---- semantic-gate parity (mirror-marker-discipline-spec-update-001) ----
    # render_marker shares check_marker_semantics with parse_mirror_marker, so
    # render-time self-check and parse-time ingestion can't drift. PR #711 hit
    # three malformed-marker shapes the parser rejected but render let through;
    # these lock render to the same gate.

    def _revision_payload(self, severity='medium', confidence='high',
                          findings=None):
        if findings is None:
            findings = [{'file': 'a.py', 'line': 1, 'issue': 'typo'}]
        return {
            'task_id': 't-001', 'pr_url': 'https://github.com/x/y/pull/1',
            'findings': findings, 'severity': severity, 'confidence': confidence,
        }

    def test_render_revision_out_of_enum_severity_raises_value_error(self):
        # PR #711 shape: severity='blocking' is not a valid value at all.
        # 'high' belongs in ESCALATE, 'critical' in EMERGENCY_HALT — none are
        # valid on REVISION.
        for bad in ('blocking', 'high', 'critical'):
            with self.subTest(severity=bad):
                with self.assertRaisesRegex(ValueError, 'severity'):
                    mrh.render_marker(
                        'review_revision', **self._revision_payload(severity=bad),
                    )

    def test_render_revision_empty_findings_raises_value_error(self):
        # PR #711 shape: REVIEW_REVISION with [] findings should have been PASS.
        with self.assertRaisesRegex(ValueError, 'non-empty list'):
            mrh.render_marker(
                'review_revision', **self._revision_payload(findings=[]),
            )

    def test_render_revision_non_list_findings_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'non-empty list'):
            mrh.render_marker(
                'review_revision',
                **self._revision_payload(findings='not-a-list'),
            )

    def test_render_revision_invalid_confidence_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'confidence'):
            mrh.render_marker(
                'review_revision',
                **self._revision_payload(confidence='vibes'),
            )

    def test_render_revision_valid_low_medium_renders(self):
        # Valid low/medium severity + non-empty findings + valid confidence
        # still produces the canonical block and parses clean.
        for sev in ('low', 'medium'):
            with self.subTest(severity=sev):
                rendered = mrh.render_marker(
                    'review_revision', **self._revision_payload(severity=sev),
                )
                ptype, ppayload, _ = mrh.parse_mirror_marker(rendered)
                self.assertEqual(ptype, 'review_revision')
                self.assertEqual(ppayload['severity'], sev)
                self.assertEqual(len(ppayload['findings']), 1)

    def test_render_escalate_out_of_enum_severity_raises_value_error(self):
        # ESCALATE is severity=high only; low/medium belong to REVISION.
        for bad in ('low', 'medium', 'blocking'):
            with self.subTest(severity=bad):
                with self.assertRaisesRegex(ValueError, 'severity'):
                    mrh.render_marker(
                        'review_escalate',
                        task_id='t-001',
                        pr_url='https://github.com/x/y/pull/1',
                        reason='spec mismatch', severity=bad, confidence='high',
                    )

    def test_shared_helper_exercised_from_both_paths(self):
        # The same check_marker_semantics call gates render AND parse: a payload
        # that render rejects is one parse rejects too (same diagnostic text,
        # different exception type per each path's contract).
        bad = self._revision_payload(severity='blocking')
        with self.assertRaises(ValueError) as render_ctx:
            mrh.render_marker('review_revision', **bad)
        # Build the equivalent raw marker block and run it through the parser.
        block = (
            '=== REVIEW_REVISION ===\n'
            + json.dumps(bad)
            + '\n=== END_REVIEW_REVISION ==='
        )
        with self.assertRaises(mrh.MalformedMirrorMarker) as parse_ctx:
            mrh.parse_mirror_marker(block)
        self.assertIn('blocking', str(render_ctx.exception))
        self.assertIn('blocking', str(parse_ctx.exception))


class CheckMarkerSemanticsTest(unittest.TestCase):
    """The shared validator directly — both callers route through it."""

    def test_pass_and_emergency_halt_are_noops(self):
        # Markers without severity/confidence/findings fields don't trip it.
        mrh.check_marker_semantics(
            'review_pass',
            {'task_id': 't', 'pr_url': 'u', 'summary': 's'},
        )
        mrh.check_marker_semantics(
            'review_emergency_halt',
            {'task_id': 't', 'pr_url': 'u', 'reason': 'r', 'evidence': 'e'},
        )

    def test_valid_revision_passes(self):
        mrh.check_marker_semantics('review_revision', {
            'severity': 'medium', 'confidence': 'high',
            'findings': [{'file': 'x'}],
        })

    def test_valid_escalate_passes(self):
        mrh.check_marker_semantics('review_escalate', {
            'severity': 'high', 'confidence': 'medium', 'reason': 'r',
        })


if __name__ == '__main__':
    unittest.main()
