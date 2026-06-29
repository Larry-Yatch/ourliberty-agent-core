#!/usr/bin/env python3
"""Tests for pulse_check_retrospective_author (alert-pipeline-rework P3b — Stage B).

Covers the spec § PHASE 3 acceptance criteria (line 72):

  1. seeded recurring+benign elevation → ONE `proposed` card, automate-now,
     with a concrete pre-drafted fix;
  2. declining (board dismissal) prevents re-proposal;
  3. defect-class → fix-permanently mission;
  4. decision-class → no card (counted only in the summary);
  5. weekly trend line carried onto the author artifact (= Phase-4 verification);
  6. truncated author artifact doesn't permanently suppress the weekly run;

…plus the deterministic gate (the real authority over the LLM): a hallucinated
automate-now on a reject-heavy or template-less bucket is downgraded to
fix-permanently, and the bounded-LLM fallback (classifier → None) skips posting.

The claude CLI / Supabase / HTTP are NEVER touched live — the classifier and the
mission-poster are injectable seams; tests pass stubs.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_pulse_check_retrospective_author
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_retrospective_author as rb  # noqa: E402


NOW = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)  # Monday
WEEK = '2026-06-29'


# ---------- fixture helpers ----------


def _candidate(
    sig: str,
    *,
    source: str = 'medic',
    subject: str = 'build-failed',
    count: int = 4,
    weeks: int = 3,
    histogram: dict | None = None,
    recurrence_actionable: bool = True,
    dedup_status: str = 'fresh',
    trend: str = 'rising',
) -> dict:
    return {
        'root_signature': sig,
        'source': source,
        'normalized_subject': subject,
        'count_this_period': count,
        'weeks_recurring': weeks,
        'resolution_histogram': histogram if histogram is not None
        else {'approve': 0, 'reject': 0, 'silence': 4, 'ack': 0, 'none': 0},
        'examples': [],
        'event_types': ['larry_alert'],
        'live_unresolved': False,
        'trend': trend,
        'last_period_count': 2,
        'dedup_status': dedup_status,
        'recurrence_actionable': recurrence_actionable,
    }


def _artifact(candidates: list[dict], *, trend_line: str = 'TREND') -> dict:
    return {
        'as_of': NOW.isoformat(),
        'week_anchor': WEEK,
        'lookback_days': 7,
        'summary': {'trend_line': trend_line},
        'candidates': candidates,
        'resolved': [],
    }


def _stub_classifier(mapping: dict[str, dict], *, cost: float | None = 0.01):
    """Return a classifier seam that yields `mapping` (sig → cls obj)."""
    def classifier(candidates):
        return dict(mapping), cost
    return classifier


class _RecordingPoster:
    """Stands in for post_proposed_mission: records calls, returns 200/queued."""

    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, *, name, brief, predraft, spec_docs,
                 api_base=None, token=None, repo='ourliberty-agent-core'):
        mid = rb._kebab(name)
        self.calls.append({
            'name': name, 'brief': brief, 'predraft': predraft,
            'spec_docs': spec_docs, 'mission_id': mid,
        })
        return self.status, {'mission_id': mid, 'status': 'queued'}


# ==================== AC1 — recurring+benign → one automate-now card ====================


class AutomateNowCardTest(unittest.TestCase):
    def test_seeded_recurring_benign_yields_one_proposed_automate_now_card(self):
        sig = 'medic::build-failed'
        cand = _candidate(sig)
        artifact = _artifact([cand])
        classifier = _stub_classifier({sig: {
            'root_signature': sig,
            'classification': 'automate-now',
            'template_id': '2',
            'file_key': 'config/medic-silenceable-subjects.json',
            'value': 'build-failed',
            'diff_sketch': 'add "build-failed" to silenceable_subjects[]',
            'rationale': 'recurring benign silence-only line',
        }})
        poster = _RecordingPoster()
        ledger: dict = {}
        result, new_ledger = rb.author_cycle(
            artifact, ledger=ledger, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        # Exactly one card posted, automate-now, concrete pre-draft present.
        self.assertEqual(len(poster.calls), 1)
        call = poster.calls[0]
        pd = call['predraft']
        self.assertEqual(pd['classification'], 'automate-now')
        self.assertEqual(pd['template_id'], '2')
        self.assertEqual(pd['value'], 'build-failed')
        self.assertTrue(pd['diff_sketch'])
        self.assertEqual(pd['root_signature'], sig)
        self.assertEqual(call['mission_id'], f'retrospective-medic-build-failed-{WEEK}')
        # Ledger `proposed` flag set so a future run dedups it.
        self.assertTrue(new_ledger[sig]['proposed'])
        # Summary counts.
        self.assertEqual(result.counts()['automate-now'], 1)
        self.assertEqual(len(result.posted), 1)


# ==================== AC2 — declining prevents re-proposal ====================


class DeclinePreventsReproposalTest(unittest.TestCase):
    def test_board_dismissal_reconciles_ledger_and_blocks_repost(self):
        sig = 'medic::build-failed'
        cand = _candidate(sig, count=5)
        artifact = _artifact([cand])
        # The board already has a DISMISSED proposed card for this signature
        # (acknowledged:true), carrying its count_at_proposal in predraft.
        existing = [{
            'id': f'retrospective-medic-build-failed-{WEEK}',
            'phase': 'proposed', 'acknowledged': True,
            'predraft': {'root_signature': sig, 'count_at_proposal': 4},
        }]
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'automate-now',
            'template_id': '2', 'value': 'build-failed', 'diff_sketch': 'x',
        }})
        poster = _RecordingPoster()
        ledger: dict = {}
        result, new_ledger = rb.author_cycle(
            artifact, ledger=ledger, existing_missions=existing,
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        # The dismissal is reconciled into the ledger…
        self.assertTrue(new_ledger[sig]['dismissed'])
        self.assertEqual(new_ledger[sig]['dismissed_at_count'], 4)
        # …and (the existing-id collision) the card is NOT re-posted.
        self.assertEqual(poster.calls, [])
        self.assertEqual(len(result.skipped), 1)

    def test_live_proposed_card_dedups_without_repost(self):
        # A LIVE (not acknowledged) proposed card for the same signature → skip.
        sig = 'pulse::heal-pipeline-stall'
        cand = _candidate(sig, source='pulse', subject='heal-pipeline-stall')
        artifact = _artifact([cand])
        existing = [{
            'id': 'some-other-id',
            'phase': 'proposed', 'acknowledged': False,
            'predraft': {'root_signature': sig, 'count_at_proposal': 3},
        }]
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'fix-permanently',
            'rationale': 'real defect',
        }})
        poster = _RecordingPoster()
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=existing,
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        self.assertEqual(poster.calls, [])
        self.assertEqual(len(result.skipped), 1)


# ==================== AC3 — defect-class → fix-permanently mission ====================


class FixPermanentlyTest(unittest.TestCase):
    def test_defect_class_posts_fix_permanently_no_template(self):
        sig = 'shipper::crash-loop'
        cand = _candidate(sig, source='shipper', subject='crash-loop',
                          histogram={'approve': 0, 'reject': 0, 'silence': 0,
                                     'ack': 4, 'none': 0})
        artifact = _artifact([cand])
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'fix-permanently',
            'rationale': 'real crash that needs a code fix',
        }})
        poster = _RecordingPoster()
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        self.assertEqual(len(poster.calls), 1)
        pd = poster.calls[0]['predraft']
        self.assertEqual(pd['classification'], 'fix-permanently')
        self.assertNotIn('template_id', pd)  # no pre-drafted template change
        self.assertEqual(result.counts()['fix-permanently'], 1)


# ==================== AC4 — decision-class → no card, counted in summary ====================


class KeepElevatingTest(unittest.TestCase):
    def test_decision_class_posts_no_card_but_counts(self):
        sig = 'beacon::approval-request'
        cand = _candidate(sig, source='beacon', subject='approval-request',
                          recurrence_actionable=False)
        artifact = _artifact([cand])
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'keep-elevating',
            'rationale': 'genuine per-instance decision',
        }})
        poster = _RecordingPoster()
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        self.assertEqual(poster.calls, [])  # no card
        self.assertEqual(result.counts()['keep-elevating'], 1)  # but counted


# ==================== deterministic gate (real authority over the LLM) ====================


class GateDowngradesTest(unittest.TestCase):
    def test_reject_heavy_histogram_downgrades_automate_now(self):
        # The LLM says automate-now, but the operator rejected this elevation —
        # never benign. Gate downgrades to fix-permanently.
        sig = 'medic::risky-restart'
        cand = _candidate(sig, subject='risky-restart',
                          histogram={'approve': 1, 'reject': 2, 'silence': 1,
                                     'ack': 0, 'none': 0})
        artifact = _artifact([cand])
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'automate-now',
            'template_id': '3', 'value': 'x', 'diff_sketch': 'y',
        }})
        poster = _RecordingPoster()
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        self.assertEqual(result.counts()['automate-now'], 0)
        self.assertEqual(result.counts()['fix-permanently'], 1)
        self.assertEqual(poster.calls[0]['predraft']['classification'],
                         'fix-permanently')

    def test_unknown_template_downgrades_automate_now(self):
        sig = 'medic::build-failed'
        cand = _candidate(sig)
        artifact = _artifact([cand])
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'automate-now',
            'template_id': '99',  # not in TEMPLATE_REGISTRY
        }})
        poster = _RecordingPoster()
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        self.assertEqual(result.counts()['fix-permanently'], 1)

    def test_non_recurrence_actionable_downgrades_automate_now(self):
        sig = 'medic::build-failed'
        cand = _candidate(sig, recurrence_actionable=False)
        artifact = _artifact([cand])
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'automate-now',
            'template_id': '2',
        }})
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=_RecordingPoster(),
        )
        self.assertEqual(result.counts()['fix-permanently'], 1)

    def test_only_fresh_candidates_are_classified(self):
        fresh = _candidate('a::fresh', subject='fresh')
        suppressed = _candidate('b::sup', subject='sup',
                                dedup_status='suppressed-dismissed')
        already = _candidate('c::prop', subject='prop',
                             dedup_status='already-proposed')
        artifact = _artifact([fresh, suppressed, already])
        classifier = _stub_classifier({'a::fresh': {
            'root_signature': 'a::fresh', 'classification': 'fix-permanently',
        }})
        poster = _RecordingPoster()
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        # Only the fresh one is a decision; the other two were never eligible.
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(result.decisions[0].candidate['root_signature'], 'a::fresh')


# ==================== bounded-LLM fallback ====================


class LLMFallbackTest(unittest.TestCase):
    def test_classifier_none_skips_posting(self):
        sig = 'medic::build-failed'
        artifact = _artifact([_candidate(sig)])

        def failing_classifier(candidates):
            return None, None  # claude failed

        poster = _RecordingPoster()
        result, ledger = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=failing_classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        self.assertEqual(poster.calls, [])
        self.assertTrue(result.llm_unavailable)
        self.assertEqual(result.decisions, [])

    def test_parse_classifications_tolerates_fence_and_prose(self):
        raw = (
            'Here you go:\n```json\n'
            '[{"root_signature": "x::y", "classification": "keep-elevating"}]'
            '\n```\n'
        )
        parsed = rb.parse_classifications(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['x::y']['classification'], 'keep-elevating')

    def test_parse_classifications_returns_none_on_garbage(self):
        self.assertIsNone(rb.parse_classifications('not json at all'))


# ==================== AC5 — weekly trend line carried onto author artifact ====================


class TrendLineCarriedTest(unittest.TestCase):
    def test_author_artifact_carries_stage_a_trend_line(self):
        trend = ('3 elevation bucket(s) this period: 1 new, 1 rising, '
                 '1 falling, 2 resolved since last period.')
        artifact = _artifact([], trend_line=trend)
        result, _ = rb.author_cycle(
            artifact, ledger={}, existing_missions=[],
            classifier=_stub_classifier({}), week_anchor=WEEK, now=NOW,
            poster=_RecordingPoster(),
        )
        out = rb.build_author_artifact(
            result, week_anchor=WEEK, as_of_iso=NOW.isoformat(),
            source_trend_line=(artifact['summary']['trend_line']),
        )
        self.assertEqual(out['summary']['trend_line'], trend)


# ==================== AC6 — truncated artifact doesn't permanently suppress ====================


class SentinelHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='retro-author-sentinel-'))

    def test_truncated_sentinel_is_not_a_valid_completion_marker(self):
        path = self.tmp / 'retrospective-author-2026-06-29.json'
        path.write_text('{ "week_anchor": ')  # truncated mid-write
        self.assertFalse(rb._artifact_is_valid_sentinel(path))

    def test_zero_byte_sentinel_is_not_valid(self):
        path = self.tmp / 'z.json'
        path.write_text('')
        self.assertFalse(rb._artifact_is_valid_sentinel(path))

    def test_complete_artifact_is_valid(self):
        path = self.tmp / 'ok.json'
        path.write_text(json.dumps({'week_anchor': '2026-06-29', 'summary': {}}))
        self.assertTrue(rb._artifact_is_valid_sentinel(path))


# ==================== mission identity / prefix dedup ====================


class MissionIdentityTest(unittest.TestCase):
    def test_id_matches_dashboard_kebab(self):
        from dashboard_api import _kebab_case
        name = rb.mission_name('medic::build-failed', WEEK)
        self.assertEqual(rb._kebab(name), _kebab_case(name))
        self.assertEqual(rb.mission_id_for('medic::build-failed', WEEK),
                         f'retrospective-medic-build-failed-{WEEK}')


# ==================== poster error handling ====================


class PosterErrorTest(unittest.TestCase):
    def test_non_2xx_recorded_as_error_no_ledger_flag(self):
        sig = 'medic::build-failed'
        artifact = _artifact([_candidate(sig)])
        classifier = _stub_classifier({sig: {
            'root_signature': sig, 'classification': 'fix-permanently',
        }})
        poster = _RecordingPoster(status=500)
        ledger: dict = {}
        result, new_ledger = rb.author_cycle(
            artifact, ledger=ledger, existing_missions=[],
            classifier=classifier, week_anchor=WEEK, now=NOW, poster=poster,
        )
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result.posted), 0)
        self.assertNotIn(sig, new_ledger)  # no proposed flag on a failed post


if __name__ == '__main__':
    unittest.main()
