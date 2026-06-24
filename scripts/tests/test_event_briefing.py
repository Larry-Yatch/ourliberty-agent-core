#!/usr/bin/env python3
"""Tests for scripts/event_briefing.py — the chain-event meaning layer (#5).

Covers the deterministic plain-language briefing authored onto alert
(larry_alert / sentinel_alert) and autonomy_decision chain_events:

  - risk primitives: map_risk (careful escalator wins; auto_approve⇒safe,
    force_ask⇒medium, reject/unknown⇒careful) + classify_careful_text;
  - alert_briefing: a translation match maps plain_language_summary⇒what,
    recommended_action⇒suggest, severity/tier⇒risk (URGENT⇒careful,
    WARNING⇒medium, INFO⇒safe); the careful-keyword escalator overrides;
    the real _translation_match lookup is exercised (exact + ':'-prefix-strip);
    escalation (pulse) is never briefed; a no-match / empty / non-dict record
    returns None (headline fallback) and never raises;
  - decision_briefing: every auto-fired decision gets a briefing; an
    auto_approve that touches careful-class work still surfaces as careful;
  - the shipper bakes the briefing into the alert payload at emit
    (parse_jsonl_line integration).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_event_briefing
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import event_briefing as eb  # noqa: E402

# A controlled translation table — exercises the real _translation_match
# resolver (exact, ':'-prefix-strip, '*' catch-all) without depending on the
# live config/alert-translations.json contents.
_FAKE_TRANSLATIONS = {
    '_schema': {'version': 1},
    'heal-systemd-install-drift': {
        'install-drift': {
            'severity': 'URGENT', 'tier': 'NOW',
            'plain_language_summary': 'A systemd unit is not installed.',
            'recommended_action': 'Run sudo cp ... then daemon-reload.',
        },
    },
    'heal-pipeline-stall': {
        # bare key; a 'pipeline-stall:forge-no-pr' subject matches via prefix-strip
        'pipeline-stall': {
            'severity': 'WARNING', 'tier': 'SOON',
            'plain_language_summary': 'A build stalled mid-pipeline.',
            'recommended_action': 'Inspect the worktree and re-dispatch.',
        },
    },
    'outbox-notifier': {
        'review-pass': {
            'severity': 'INFO', 'tier': 'FYI',
            'plain_language_summary': 'A PR auto-merged on review pass.',
            'recommended_action': 'None — routine success.',
        },
    },
    'empty-source': {
        'empty-subject': {'severity': 'INFO', 'tier': 'FYI'},  # no summary/action
    },
}


def _patch_translations():
    """Patch the lazily-imported translation loader so alert_briefing resolves
    against _FAKE_TRANSLATIONS via the real _translation_match."""
    import alert_triage_state as ats
    return mock.patch.object(ats, 'load_translations',
                             return_value=_FAKE_TRANSLATIONS)


class RiskPrimitivesTest(unittest.TestCase):
    def test_map_risk(self):
        self.assertEqual(eb.map_risk('auto_approve', False), 'safe')
        self.assertEqual(eb.map_risk('force_ask', False), 'medium')
        self.assertEqual(eb.map_risk('reject', False), 'careful')
        self.assertEqual(eb.map_risk('something-unknown', False), 'careful')
        # the careful escalator wins over an otherwise-safe action
        self.assertEqual(eb.map_risk('auto_approve', True), 'careful')

    def test_classify_careful_text(self):
        self.assertTrue(eb.classify_careful_text('please deploy to production'))
        self.assertTrue(eb.classify_careful_text('rotate the credential'))
        self.assertFalse(eb.classify_careful_text('fix a small typo'))
        self.assertFalse(eb.classify_careful_text(''))
        # word-boundary: "drops" must not trip the "drop" keyword
        self.assertFalse(eb.classify_careful_text('the cache drops stale rows'))


class AlertBriefingTest(unittest.TestCase):
    def test_urgent_translation_maps_to_careful(self):
        with _patch_translations():
            b = eb.alert_briefing(
                {'source': 'heal-systemd-install-drift', 'subject': 'install-drift'},
                'larry_alert')
        self.assertIsNotNone(b)
        self.assertEqual(b['risk'], 'careful')
        self.assertEqual(b['briefing']['what'], 'A systemd unit is not installed.')
        self.assertEqual(b['briefing']['suggest'], 'Run sudo cp ... then daemon-reload.')
        self.assertIn('risk_note', b)  # careful carries the catch note
        self.assertEqual(set(b['briefing']), {'what', 'why', 'suggest'})

    def test_prefix_strip_match_warning_to_medium(self):
        with _patch_translations():
            b = eb.alert_briefing(
                {'source': 'heal-pipeline-stall', 'subject': 'pipeline-stall:forge-no-pr'},
                'sentinel_alert')
        self.assertEqual(b['risk'], 'medium')
        self.assertIn('risk_note', b)

    def test_info_maps_to_safe_no_note(self):
        with _patch_translations():
            b = eb.alert_briefing(
                {'source': 'outbox-notifier', 'subject': 'review-pass'}, 'larry_alert')
        self.assertEqual(b['risk'], 'safe')
        self.assertNotIn('risk_note', b)

    def test_careful_keyword_escalates_over_info(self):
        with _patch_translations():
            b = eb.alert_briefing(
                {'source': 'outbox-notifier', 'subject': 'review-pass',
                 'body': 'this will deploy to production'}, 'larry_alert')
        self.assertEqual(b['risk'], 'careful')

    def test_escalation_event_never_briefed(self):
        with _patch_translations():
            self.assertIsNone(
                eb.alert_briefing({'source': 'pulse', 'subject': 'x'}, 'escalation'))

    def test_no_translation_returns_none(self):
        with _patch_translations():
            self.assertIsNone(eb.alert_briefing(
                {'source': 'no-such-source', 'subject': 'mystery'}, 'larry_alert'))

    def test_empty_translation_returns_none(self):
        # entry exists but carries neither summary nor action — no better than headline
        with _patch_translations():
            self.assertIsNone(eb.alert_briefing(
                {'source': 'empty-source', 'subject': 'empty-subject'}, 'larry_alert'))

    def test_non_dict_and_bad_event_type_never_raise(self):
        with _patch_translations():
            self.assertIsNone(eb.alert_briefing(None, 'larry_alert'))
            self.assertIsNone(eb.alert_briefing({}, 'sentinel_alert'))
            self.assertIsNone(eb.alert_briefing({'subject': 'x'}, 'auto_merge'))

    def test_translation_import_failure_degrades_to_none(self):
        # if the translation layer can't be consulted, never raise — fall back.
        import alert_triage_state as ats
        with mock.patch.object(ats, 'load_translations',
                               side_effect=RuntimeError('boom')):
            self.assertIsNone(eb.alert_briefing(
                {'source': 'heal-systemd-install-drift', 'subject': 'install-drift'},
                'larry_alert'))


class DecisionBriefingTest(unittest.TestCase):
    def test_auto_approve_is_safe(self):
        d = eb.decision_briefing({
            'decision': 'auto_approve', 'summary': 'agent-core: fix a lint warning',
            'task_type': 'bugfix', 'target_repo': 'agent-core',
            'rule_label': 'beacon → forge · agent-core'})
        self.assertEqual(d['risk'], 'safe')
        self.assertEqual(d['briefing']['what'], 'agent-core: fix a lint warning')
        self.assertIn('rule:', d['briefing']['why'])
        self.assertNotIn('risk_note', d)

    def test_careful_class_auto_approve_surfaces_careful(self):
        d = eb.decision_briefing({
            'decision': 'auto_approve', 'summary': 'rotate the production credential',
            'task_type': 'chore'})
        self.assertEqual(d['risk'], 'careful')
        self.assertIn('risk_note', d)

    def test_what_fallback_without_summary(self):
        d = eb.decision_briefing({
            'decision': 'auto_approve', 'task_type': 'feature-development',
            'target_repo': 'dashboard'})
        self.assertEqual(d['briefing']['what'], 'feature-development in dashboard')
        d2 = eb.decision_briefing({'decision': 'auto_approve'})
        self.assertTrue(d2['briefing']['what'])  # never empty
        self.assertEqual(set(d2['briefing']), {'what', 'why', 'suggest'})


class ShipperIntegrationTest(unittest.TestCase):
    """parse_jsonl_line bakes the briefing into the alert payload at emit."""

    def test_alert_line_carries_briefing_in_payload(self):
        import chain_event_shipper as ces
        line = json.dumps({
            'ts': '2026-06-23T00:00:00+00:00', 'source': 'heal-systemd-install-drift',
            'subject': 'install-drift', 'severity': 'red'})
        with _patch_translations():
            ev = ces.parse_jsonl_line(line, source='larry_alerts')
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, 'larry_alert')
        self.assertIn('briefing', ev.payload)
        self.assertEqual(ev.payload['risk'], 'careful')
        self.assertEqual(ev.payload['briefing']['what'], 'A systemd unit is not installed.')

    def test_untranslated_alert_has_no_briefing(self):
        import chain_event_shipper as ces
        line = json.dumps({
            'ts': '2026-06-23T00:00:00+00:00', 'source': 'mystery-source',
            'subject': 'nothing-here'})
        with _patch_translations():
            ev = ces.parse_jsonl_line(line, source='larry_alerts')
        self.assertIsNotNone(ev)
        self.assertNotIn('briefing', ev.payload)  # headline fallback intact


class NarratorParityTest(unittest.TestCase):
    """Drift tripwire — event_briefing keeps a deliberate self-contained copy of
    the risk primitives (to keep the shipper daemon's import surface light), so
    this asserts the copy stays in lockstep with missions_narrator's source."""

    def test_careful_keywords_match_narrator(self):
        import missions_narrator as mn
        self.assertEqual(eb._CAREFUL_KEYWORDS, mn._CAREFUL_KEYWORDS)

    def test_map_risk_truth_table_matches_narrator(self):
        import missions_narrator as mn
        for action in ('auto_approve', 'force_ask', 'reject', 'unknown'):
            for careful in (False, True):
                self.assertEqual(
                    eb.map_risk(action, careful), mn.map_risk(action, careful),
                    f'map_risk divergence: action={action} careful={careful}')


class NonStringInputTest(unittest.TestCase):
    """A mis-authored config entry / payload value (non-string) must not raise —
    the field reads str()-coerce so the functions are raise-free on their own."""

    def test_alert_briefing_coerces_non_string_translation_fields(self):
        fake = {'weird': {'subj': {
            'severity': 'WARNING', 'tier': 'SOON',
            'plain_language_summary': 12345,       # non-string
            'recommended_action': ['a', 'list'],   # non-string
        }}}
        import alert_triage_state as ats
        with mock.patch.object(ats, 'load_translations', return_value=fake):
            b = eb.alert_briefing({'source': 'weird', 'subject': 'subj'}, 'larry_alert')
        self.assertIsNotNone(b)
        self.assertEqual(b['risk'], 'medium')
        self.assertTrue(all(isinstance(v, str) for v in b['briefing'].values()))

    def test_decision_briefing_coerces_non_string_fields(self):
        d = eb.decision_briefing({
            'decision': 'auto_approve', 'summary': 999, 'task_type': None,
            'target_repo': 42, 'rule_label': ['x']})
        self.assertIn(d['risk'], eb.VALID_RISKS)
        self.assertEqual(set(d['briefing']), {'what', 'why', 'suggest'})
        self.assertTrue(all(isinstance(v, str) for v in d['briefing'].values()))


if __name__ == '__main__':
    unittest.main()
