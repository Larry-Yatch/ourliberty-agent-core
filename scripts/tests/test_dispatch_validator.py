#!/usr/bin/env python3
"""Fixtures for `dispatch_validator` — task envelope schema gating.

The validator runs at two points: safe_write_inbox (write-time gate) and
inbox_watcher.process_task (defense-in-depth at dispatch-time). Coverage
focuses on the schema fields whose vocabulary expands across phases —
ALLOWED_SOURCES, ALLOWED_INTENTS, ALLOWED_PHASES.

D3.5 commit 5a expands the vocab with Mirror review markers. The rest of
this file is forward-compat smoke coverage so a future intent/phase
addition is harder to land without exercising the validator first.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_dispatch_validator
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_validator as dv  # noqa: E402


def _make_task(**overrides):
    base = {
        'task_id': 't-smoke',
        'prompt': 'x' * (dv.MIN_PROMPT_LEN + 10),
        'source': 'beacon',
    }
    base.update(overrides)
    return base


class CoreValidationTest(unittest.TestCase):
    """Schema invariants every dispatched task must satisfy."""

    def test_minimal_valid_task_passes(self):
        ok, reason = dv.validate_task(_make_task())
        self.assertTrue(ok, reason)

    def test_short_prompt_rejected(self):
        ok, reason = dv.validate_task(_make_task(prompt='too short'))
        self.assertFalse(ok)
        self.assertIn('prompt too short', reason)

    def test_unknown_source_rejected(self):
        ok, reason = dv.validate_task(_make_task(source='wandering-agent'))
        self.assertFalse(ok)
        self.assertIn('not in allowed list', reason)

    def test_missing_task_id_rejected(self):
        t = _make_task()
        t.pop('task_id')
        ok, reason = dv.validate_task(t)
        self.assertFalse(ok)
        self.assertIn('task_id', reason)


class TaskIdPathSafetyTest(unittest.TestCase):
    """PR-A follow-up (audit #53): task_id front-door path-safety gate.

    task_id flows into inbox filenames (f'{task_id}.json') and worktree
    dir names. The downstream sanitizers keep writes contained, but a
    task_id with '/', '\\', NUL, a control byte, or a bare '.'/'..' is
    rejected here so it can never (a) sanitize-collide onto a legit task's
    inbox file (os.replace overwrites) or (b) diverge from the raw name the
    idempotency readers reconstruct. Real ids — including ':' '@' '#' and
    spaces — must still pass unchanged.
    """

    def test_forward_slash_rejected(self):
        ok, reason = dv.validate_task(_make_task(task_id='../../etc/passwd'))
        self.assertFalse(ok)
        self.assertIn('unsafe path character', reason)

    def test_backslash_rejected(self):
        ok, reason = dv.validate_task(_make_task(task_id='a\\b'))
        self.assertFalse(ok)
        self.assertIn('unsafe path character', reason)

    def test_nul_byte_rejected(self):
        ok, reason = dv.validate_task(_make_task(task_id='a\x00b'))
        self.assertFalse(ok)
        self.assertIn('unsafe path character', reason)

    def test_control_byte_rejected(self):
        for ctrl in ('\n', '\t', '\r', '\x1b', '\x7f'):
            ok, reason = dv.validate_task(_make_task(task_id=f'a{ctrl}b'))
            self.assertFalse(ok, f'{ctrl!r} should be rejected')
            self.assertIn('unsafe path character', reason)

    def test_bare_dot_components_rejected(self):
        for hostile in ('.', '..', '...', '....'):
            ok, reason = dv.validate_task(_make_task(task_id=hostile))
            self.assertFalse(ok, f'{hostile!r} should be rejected')
            self.assertIn('all dots', reason)

    def test_collision_pair_both_rejected(self):
        # Two distinct hostile ids that would sanitize-COLLIDE to the same
        # on-disk inbox name ('a-b.json') — neither may reach the writer.
        for hostile in ('a/b', 'a\\b'):
            ok, _ = dv.validate_task(_make_task(task_id=hostile))
            self.assertFalse(ok)

    def test_real_ids_with_colon_at_hash_space_still_pass(self):
        # The whole point: do not regress valid ids that carry printable
        # punctuation. These must remain dispatchable.
        for good in (
            'medic-silence-cpu:high@web-01',
            'alert#294',
            'v1.2.3',
            'task with spaces',
            'forge-supply-chain-3-20260605T090000Z',
            'abc_123-XYZ',
        ):
            ok, reason = dv.validate_task(_make_task(task_id=good))
            self.assertTrue(ok, f'{good!r} should pass: {reason}')

    def test_embedded_dots_allowed(self):
        # Only an ALL-dots id is rejected; embedded dots are fine.
        ok, reason = dv.validate_task(_make_task(task_id='a..b'))
        self.assertTrue(ok, reason)

    def test_helper_returns_reason(self):
        # Direct unit coverage of the named check.
        ok, reason = dv._validate_task_id_chars('a/b')
        self.assertFalse(ok)
        ok, _ = dv._validate_task_id_chars('medic:1@web')
        self.assertTrue(ok)


class AllowedIntentsTest(unittest.TestCase):
    """Intent vocabulary, including D3.5 5a review-marker additions."""

    def test_d3_preflight_intents_accepted(self):
        for intent in (
            'ack-proceed', 'clarify', 'clarification-response',
            'clarification-exhausted', 'reject',
            'result-notification', 'dead-letter', 'marker-error',
        ):
            ok, reason = dv.validate_task(_make_task(intent=intent))
            self.assertTrue(ok, f'{intent}: {reason}')

    def test_d35_review_intents_accepted(self):
        # 5a vocab additions — Mirror review markers + Beacon replan request.
        for intent in (
            'review-pass', 'review-revision', 'review-escalate',
            'review-emergency-halt', 'replan-request',
        ):
            ok, reason = dv.validate_task(_make_task(intent=intent))
            self.assertTrue(ok, f'{intent}: {reason}')

    def test_unknown_intent_rejected(self):
        ok, reason = dv.validate_task(_make_task(intent='ship-it-yolo'))
        self.assertFalse(ok)
        self.assertIn('not in allowed set', reason)

    def test_intent_omitted_is_legal(self):
        # `intent` is optional — many notify paths omit it.
        t = _make_task()
        t.pop('intent', None)
        ok, _ = dv.validate_task(t)
        self.assertTrue(ok)


class AllowedPhasesTest(unittest.TestCase):
    """Phase vocabulary, including D3.5 5a review/revision additions."""

    def test_d3_phases_accepted(self):
        for phase in ('preflight', 'build'):
            ok, reason = dv.validate_task(_make_task(phase=phase))
            self.assertTrue(ok, f'{phase}: {reason}')

    def test_d35_phases_accepted(self):
        # 5a adds `review` (Mirror dispatch); 5b will activate `revision`
        # (Forge revision under --resume after REVIEW_REVISION marker).
        for phase in ('review', 'revision'):
            ok, reason = dv.validate_task(_make_task(phase=phase))
            self.assertTrue(ok, f'{phase}: {reason}')

    def test_unknown_phase_rejected(self):
        ok, reason = dv.validate_task(_make_task(phase='polish'))
        self.assertFalse(ok)
        self.assertIn('not in allowed set', reason)


class MirrorSourceVocabularyTest(unittest.TestCase):
    """Mirror's dialogue sources (already in vocab pre-5a; smoke-check)."""

    def test_mirror_question_source_accepted(self):
        # mirror-question was added in D3-prep for parity with forge-question
        # but until 5a (REVIEW_QUESTION marker in 5b) Mirror never emitted it.
        # Smoke-test the vocab so a future deletion is caught.
        ok, reason = dv.validate_task(_make_task(source='mirror-question'))
        self.assertTrue(ok, reason)

    def test_mirror_result_source_accepted(self):
        ok, reason = dv.validate_task(_make_task(source='mirror-result'))
        self.assertTrue(ok, reason)


class ClarificationBudgetTest(unittest.TestCase):
    """Envelope counters carried through the preflight cascade."""

    def test_negative_counter_rejected(self):
        ok, reason = dv.validate_task(_make_task(clarification_count=-1))
        self.assertFalse(ok)
        self.assertIn('non-negative int', reason)

    def test_count_above_max_rejected(self):
        ok, reason = dv.validate_task(_make_task(
            clarification_count=5, max_clarifications=3,
        ))
        self.assertFalse(ok)
        self.assertIn('exceeds max_clarifications', reason)

    def test_count_equal_max_accepted(self):
        # Equal is the final-round case — the next CLARIFY would convert
        # to clarification-exhausted; validator should still accept.
        ok, _ = dv.validate_task(_make_task(
            clarification_count=3, max_clarifications=3,
        ))
        self.assertTrue(ok)


class RoutingSignalPromptPrefixExemptionTest(unittest.TestCase):
    """bootstrap-002 gap V3 (2026-05-27): Beacon's marker template for
    APPROVAL_REQUEST has no slot for the phase field, so DAG-preflight
    dispatches cannot get phase=routing-signal set. Defense-in-depth:
    accept exemption by canonical prompt prefix as well."""

    def test_review_sequence_dag_prompt_exempt_even_without_phase(self):
        # Beacon's actual emission on 2026-05-27: target_agent=mirror,
        # task_type=code-review (NOT phase=routing-signal — phase field
        # absent because marker.py render template has no slot for it),
        # prompt=`review-sequence-dag orchestrator-bootstrap-002` (~45 chars).
        # Old behavior: MIN_PROMPT_LEN rejection. New behavior: exempt by prefix.
        ok, reason = dv.validate_task(_make_task(
            prompt='review-sequence-dag orchestrator-bootstrap-002',
            target_agent='mirror',
            task_type='code-review',
            # phase deliberately omitted to match Beacon's marker emission
        ))
        self.assertTrue(ok, reason)

    def test_kickoff_prompt_exempt_even_without_phase_or_target_agent(self):
        # Defense-in-depth — kickoff dispatches normally have
        # target_agent=build_sequence_advancer (already exempt) but if a
        # caller forgets that field, the prefix still saves them.
        ok, reason = dv.validate_task(_make_task(
            prompt='kickoff orchestrator-bootstrap-002',
            target_agent='beacon',  # NOT the expected build_sequence_advancer
            phase=None,
        ))
        self.assertTrue(ok, reason)

    def test_short_non_routing_prompt_still_rejected(self):
        # Regression guard: only canonical routing-signal prefixes get the
        # exemption. A random short prompt to mirror should still fail.
        ok, reason = dv.validate_task(_make_task(
            prompt='short non-canonical prompt',
            target_agent='mirror',
            phase='code-review',
        ))
        self.assertFalse(ok)
        self.assertIn('prompt too short', reason)

    def test_kickoff_prefix_with_no_seqid_is_not_exempt(self):
        # Audit fix: the prefix alone must NOT bypass the min-length guard. A
        # degenerate `kickoff` with no seq-id (an F24 empty-prompt bug that
        # happens to carry the prefix) is not a routing signal and must be
        # rejected.
        ok, reason = dv.validate_task(_make_task(
            prompt='kickoff ',  # stripped to 'kickoff' — no seq-id payload
            target_agent='mirror',
            phase='code-review',
        ))
        self.assertFalse(ok)
        self.assertIn('prompt too short', reason)

    def test_review_sequence_dag_prefix_with_no_seqid_is_not_exempt(self):
        ok, reason = dv.validate_task(_make_task(
            prompt='review-sequence-dag    ',  # whitespace-only seq-id
            target_agent='mirror',
            phase='code-review',
        ))
        self.assertFalse(ok)
        self.assertIn('prompt too short', reason)

    def test_routing_signal_prompt_helper_requires_payload(self):
        self.assertTrue(dv._is_routing_signal_prompt('kickoff seq-123'))
        self.assertTrue(dv._is_routing_signal_prompt('review-sequence-dag s1'))
        self.assertFalse(dv._is_routing_signal_prompt('kickoff'))
        self.assertFalse(dv._is_routing_signal_prompt('kickoff '))
        self.assertFalse(dv._is_routing_signal_prompt('not-a-signal seq'))


if __name__ == '__main__':
    unittest.main()
