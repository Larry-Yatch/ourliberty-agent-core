#!/usr/bin/env python3
"""Fixtures for `forge_preflight_handler` — preflight marker grammar + budget.

Phase D3 commit 4a (`D3-forge` part 1). Covers:
  - Marker extraction (PROCEED, CLARIFY_REQUEST, REJECT) with narrative stripping
  - Malformed-JSON, missing-field, and multiple-marker rejection paths
  - Clarification-budget evaluation (allow vs exhausted) under default and
    explicit max values, with non-int / negative tolerances
  - Routing helpers (notify-source derivation, intent mapping)

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_forge_preflight_handler
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import forge_preflight_handler as fph  # noqa: E402


# -------------------- marker fixtures --------------------

def _proceed_marker(task_id='t-1', summary='Spec is clear, will edit foo.md L12-L18.'):
    payload = json.dumps({'task_id': task_id, 'preflight_summary': summary})
    return f'=== PROCEED ===\n{payload}\n=== END_PROCEED ==='


def _clarify_marker(task_id='t-1', question='Should X be Y or Z?'):
    payload = json.dumps({'task_id': task_id, 'question': question})
    return f'=== CLARIFY_REQUEST ===\n{payload}\n=== END_CLARIFY_REQUEST ==='


def _reject_marker(task_id='t-1', reason='Spec references missing file foo.md.'):
    payload = json.dumps({'task_id': task_id, 'reason': reason})
    return f'=== REJECT ===\n{payload}\n=== END_REJECT ==='


# -------------------- parse_forge_marker --------------------

class ParseForgeMarkerTest(unittest.TestCase):
    """Marker extraction with narrative stripping; malformed/multi rejections."""

    def test_extracts_proceed(self):
        narrative_above = 'Read the spec, traced the line numbers, ready to build.\n\n'
        response = narrative_above + _proceed_marker(task_id='watchdog-doc-fix-001')
        mtype, payload, narr = fph.parse_forge_marker(response)
        self.assertEqual(mtype, 'proceed')
        self.assertEqual(payload['task_id'], 'watchdog-doc-fix-001')
        self.assertIn('Spec is clear', payload['preflight_summary'])
        # Narrative should have marker stripped
        self.assertNotIn('=== PROCEED', narr)
        self.assertIn('Read the spec', narr)

    def test_extracts_clarify_request(self):
        response = 'Spec is ambiguous on point X.\n\n' + _clarify_marker(
            task_id='watchdog-001', question='Which line range?',
        )
        mtype, payload, narr = fph.parse_forge_marker(response)
        self.assertEqual(mtype, 'clarify_request')
        self.assertEqual(payload['question'], 'Which line range?')
        self.assertNotIn('=== CLARIFY_REQUEST', narr)

    def test_extracts_reject(self):
        response = _reject_marker(reason='Required file missing.')
        mtype, payload, narr = fph.parse_forge_marker(response)
        self.assertEqual(mtype, 'reject')
        self.assertEqual(payload['reason'], 'Required file missing.')

    def test_no_marker_returns_none(self):
        # Inputs with NO delimiter at all. 5b-followup Bug A added a
        # diagnostic raise when delimiters are present in any form
        # (including degenerate ones like `===PROCEED===` without spaces)
        # — those moved to test_loose_delimiter_* tests.
        for response in [
            '',
            None,
            'Just text, no markers here.',
            'PROCEED but no delimiters around it',
            'Discussing the marker contract in narrative without ===.',
        ]:
            mtype, payload, narr = fph.parse_forge_marker(response or '')
            self.assertIsNone(mtype, f'response={response!r}')
            self.assertIsNone(payload)

    def test_loose_delimiter_with_no_json_raises_diagnostic(self):
        # D3.5 5b-followup Bug A: Forge sometimes writes prose inside the
        # marker block instead of strict JSON (observed live 2026-05-13).
        # The strict regex misses; the loose detector fires the actionable
        # error explaining the JSON-only contract.
        response = (
            'Preflight checks done.\n\n'
            '=== PROCEED ===\n'
            'Preflight passed. File verified at line 1536. Plan: insert one '
            'line. No ambiguity.\n'
            '=== END_PROCEED ==='
        )
        with self.assertRaises(fph.MalformedForgeMarker) as ctx:
            fph.parse_forge_marker(response)
        msg = str(ctx.exception)
        self.assertIn('JSON object', msg)
        self.assertIn('NOT prose', msg)

    def test_loose_delimiter_with_unterminated_block_raises_diagnostic(self):
        # Opening delimiter + JSON but no closing block. Strict regex misses;
        # loose detector still fires the actionable error.
        response = '=== PROCEED ===\n{"task_id": "x", "preflight_summary": "..."}'
        with self.assertRaises(fph.MalformedForgeMarker):
            fph.parse_forge_marker(response)

    def test_malformed_json_raises(self):
        response = (
            '=== PROCEED ===\n'
            '{this is not json}\n'
            '=== END_PROCEED ==='
        )
        with self.assertRaises(fph.MalformedForgeMarker):
            fph.parse_forge_marker(response)

    def test_missing_required_field_proceed(self):
        # PROCEED needs preflight_summary
        response = (
            '=== PROCEED ===\n'
            '{"task_id": "t-1"}\n'
            '=== END_PROCEED ==='
        )
        with self.assertRaises(fph.MalformedForgeMarker) as ctx:
            fph.parse_forge_marker(response)
        self.assertIn('preflight_summary', str(ctx.exception))

    def test_missing_required_field_clarify(self):
        response = (
            '=== CLARIFY_REQUEST ===\n'
            '{"task_id": "t-1"}\n'  # missing 'question'
            '=== END_CLARIFY_REQUEST ==='
        )
        with self.assertRaises(fph.MalformedForgeMarker) as ctx:
            fph.parse_forge_marker(response)
        self.assertIn('question', str(ctx.exception))

    def test_missing_required_field_reject(self):
        response = (
            '=== REJECT ===\n'
            '{"task_id": "t-1"}\n'  # missing 'reason'
            '=== END_REJECT ==='
        )
        with self.assertRaises(fph.MalformedForgeMarker):
            fph.parse_forge_marker(response)

    def test_multiple_markers_raises(self):
        response = _proceed_marker() + '\n\nWait, actually:\n\n' + _reject_marker()
        with self.assertRaises(fph.MultipleForgeMarkers) as ctx:
            fph.parse_forge_marker(response)
        # Both marker kinds named in the error message
        self.assertIn('proceed', str(ctx.exception).lower())
        self.assertIn('reject', str(ctx.exception).lower())

    def test_two_clarify_markers_raises(self):
        response = _clarify_marker(question='Q1') + '\n' + _clarify_marker(question='Q2')
        with self.assertRaises(fph.MultipleForgeMarkers) as ctx:
            fph.parse_forge_marker(response)
        # Error message names the kind + count
        self.assertIn('clarify_request=2', str(ctx.exception))

    def test_two_proceed_markers_raises(self):
        response = _proceed_marker(summary='first') + '\n\n' + _proceed_marker(summary='second')
        with self.assertRaises(fph.MultipleForgeMarkers):
            fph.parse_forge_marker(response)

    def test_two_reject_markers_raises(self):
        response = _reject_marker(reason='r1') + '\n' + _reject_marker(reason='r2')
        with self.assertRaises(fph.MultipleForgeMarkers):
            fph.parse_forge_marker(response)

    def test_case_sensitivity_lowercase_marker_ignored(self):
        # Marker keywords are part of the contract — lowercase shouldn't match
        response = (
            '=== proceed ===\n'
            '{"task_id": "t-1", "preflight_summary": "x"}\n'
            '=== end_proceed ==='
        )
        mtype, _, _ = fph.parse_forge_marker(response)
        self.assertIsNone(mtype)


# -------------------- evaluate_clarification_budget --------------------

class ClarificationBudgetTest(unittest.TestCase):

    def test_default_max_allows_first_round(self):
        decision, next_count, max_count = fph.evaluate_clarification_budget({})
        self.assertEqual(decision, 'allow')
        self.assertEqual(next_count, 1)
        self.assertEqual(max_count, fph.DEFAULT_MAX_CLARIFICATIONS)

    def test_at_max_minus_one_allows(self):
        decision, nc, _ = fph.evaluate_clarification_budget({
            'clarification_count': 2,
            'max_clarifications': 3,
        })
        self.assertEqual(decision, 'allow')
        self.assertEqual(nc, 3)

    def test_at_max_exhausts(self):
        decision, nc, mc = fph.evaluate_clarification_budget({
            'clarification_count': 3,
            'max_clarifications': 3,
        })
        self.assertEqual(decision, 'exhausted')
        self.assertEqual(nc, 4)
        self.assertEqual(mc, 3)

    def test_max_zero_exhausts_immediately(self):
        decision, _, _ = fph.evaluate_clarification_budget({
            'max_clarifications': 0,
        })
        self.assertEqual(decision, 'exhausted')

    def test_negative_count_treated_as_zero(self):
        decision, nc, _ = fph.evaluate_clarification_budget({
            'clarification_count': -5,
            'max_clarifications': 3,
        })
        self.assertEqual(decision, 'allow')
        self.assertEqual(nc, 1)

    def test_non_int_count_treated_as_zero(self):
        decision, nc, _ = fph.evaluate_clarification_budget({
            'clarification_count': 'two',
            'max_clarifications': 3,
        })
        self.assertEqual(decision, 'allow')
        self.assertEqual(nc, 1)

    def test_non_int_max_uses_default(self):
        decision, _, mc = fph.evaluate_clarification_budget({
            'max_clarifications': 'three',
        })
        self.assertEqual(mc, fph.DEFAULT_MAX_CLARIFICATIONS)


# -------------------- clarifications_remaining --------------------

class ClarificationsRemainingTest(unittest.TestCase):

    def test_no_count_uses_default_max(self):
        self.assertEqual(
            fph.clarifications_remaining({}),
            fph.DEFAULT_MAX_CLARIFICATIONS,
        )

    def test_explicit_values(self):
        self.assertEqual(
            fph.clarifications_remaining({
                'clarification_count': 1,
                'max_clarifications': 3,
            }),
            2,
        )
        self.assertEqual(
            fph.clarifications_remaining({
                'clarification_count': 3,
                'max_clarifications': 3,
            }),
            0,
        )

    def test_overflow_clamps_to_zero(self):
        # If count > max somehow, remaining shouldn't go negative
        self.assertEqual(
            fph.clarifications_remaining({
                'clarification_count': 5,
                'max_clarifications': 3,
            }),
            0,
        )


# -------------------- routing helpers --------------------

class RoutingHelpersTest(unittest.TestCase):

    def test_derive_notify_source_clarify(self):
        self.assertEqual(
            fph.derive_notify_source('clarify_request', 'forge'),
            'forge-question',
        )

    def test_derive_notify_source_proceed_reject(self):
        self.assertEqual(
            fph.derive_notify_source('proceed', 'forge'),
            'forge-result',
        )
        self.assertEqual(
            fph.derive_notify_source('reject', 'forge'),
            'forge-result',
        )

    def test_derive_intent_basic(self):
        self.assertEqual(fph.derive_intent('proceed'), 'ack-proceed')
        self.assertEqual(fph.derive_intent('reject'), 'reject')
        self.assertEqual(
            fph.derive_intent('clarify_request', budget_decision='allow'),
            'clarify',
        )

    def test_derive_intent_exhausted_clarify_becomes_exhausted(self):
        self.assertEqual(
            fph.derive_intent('clarify_request', budget_decision='exhausted'),
            'clarification-exhausted',
        )

    def test_build_exhausted_reason_preserves_question(self):
        reason = fph.build_exhausted_reason(
            payload={'question': 'Which line range exactly?'},
            next_count=4,
            max_count=3,
        )
        self.assertIn('Which line range exactly?', reason)
        self.assertIn('4', reason)
        self.assertIn('3', reason)

    def test_build_exhausted_reason_no_question_uses_placeholder(self):
        reason = fph.build_exhausted_reason(
            payload={},
            next_count=4,
            max_count=3,
        )
        self.assertIn('no question text recorded', reason)


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
            'proceed': {'task_id': 't-001', 'preflight_summary': 'spec is buildable'},
            'clarify_request': {'task_id': 't-001', 'question': 'which file?'},
            'reject': {'task_id': 't-001', 'reason': 'spec contradicts itself'},
        }

    def test_render_then_parse_roundtrip(self):
        for mtype, payload in self._payloads().items():
            with self.subTest(mtype=mtype):
                rendered = fph.render_marker(mtype, **payload)
                ptype, ppayload, narrative = fph.parse_forge_marker(rendered)
                self.assertEqual(ptype, mtype)
                self.assertEqual(ppayload, payload)
                self.assertEqual(narrative.strip(), '')

    def test_unknown_marker_type_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'unknown marker type'):
            fph.render_marker('bogus', task_id='t-001')

    def test_missing_required_field_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, 'missing required fields'):
            fph.render_marker('proceed', task_id='t-001')  # missing preflight_summary

    def test_extra_fields_preserved_in_payload(self):
        rendered = fph.render_marker(
            'proceed', task_id='t-001', preflight_summary='ok', session_id='s1',
        )
        _, parsed, _ = fph.parse_forge_marker(rendered)
        self.assertEqual(parsed.get('session_id'), 's1')

    def test_keyword_and_required_fields_cover_every_marker_type(self):
        for mtype in fph.MARKER_TYPES:
            with self.subTest(mtype=mtype):
                self.assertIn(mtype, fph.MARKER_KEYWORDS)
                self.assertIn(mtype, fph.REQUIRED_FIELDS)


# -------------------- render_marker_skeleton / render_all_marker_skeletons --------------------

class RenderMarkerSkeletonTest(unittest.TestCase):
    """Fill-in-the-blank skeleton renderer used by the none-found retry prompt.

    The skeleton must stay parser-synced: it routes through render_marker, so
    its delimiters and field set track MARKER_KEYWORDS + REQUIRED_FIELDS and
    cannot drift into bare-literal rot.
    """

    def test_skeleton_contains_delimiters_and_required_fields(self):
        for mtype in fph.MARKER_TYPES:
            with self.subTest(mtype=mtype):
                skel = fph.render_marker_skeleton(mtype)
                keyword = fph.MARKER_KEYWORDS[mtype]
                self.assertIn(f'=== {keyword} ===', skel)
                self.assertIn(f'=== END_{keyword} ===', skel)
                for field in fph.REQUIRED_FIELDS[mtype]:
                    self.assertIn(f'"{field}"', skel)

    def test_skeleton_placeholders_for_unfilled_fields(self):
        skel = fph.render_marker_skeleton('proceed')
        self.assertIn('<preflight_summary>', skel)
        self.assertIn('<task_id>', skel)

    def test_skeleton_injects_supplied_field_values(self):
        skel = fph.render_marker_skeleton('proceed', field_values={'task_id': 'real-x'})
        self.assertIn('"real-x"', skel)
        self.assertNotIn('<task_id>', skel)
        # Unsupplied field stays a placeholder.
        self.assertIn('<preflight_summary>', skel)

    def test_unknown_skeleton_type_raises(self):
        with self.assertRaisesRegex(ValueError, 'unknown marker type'):
            fph.render_marker_skeleton('bogus')

    def test_all_skeletons_cover_every_marker_type(self):
        combined = fph.render_all_marker_skeletons()
        for mtype in fph.MARKER_TYPES:
            with self.subTest(mtype=mtype):
                self.assertIn(f'=== {fph.MARKER_KEYWORDS[mtype]} ===', combined)

    def test_all_skeletons_injects_shared_field_values(self):
        combined = fph.render_all_marker_skeletons(field_values={'task_id': 'shared-1'})
        # task_id appears once per marker type, all carrying the injected value.
        self.assertEqual(combined.count('"shared-1"'), len(fph.MARKER_TYPES))


if __name__ == '__main__':
    unittest.main()
