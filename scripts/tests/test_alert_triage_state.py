#!/usr/bin/env python3
"""Tests for alert_triage_state (PR-β alert-triage lifecycle).

Covers: lifecycle transitions, atomic-write semantics, idempotency on
re-triage.

Run::

    cd ~/agent-core && python3 -m pytest scripts/tests/test_alert_triage_state.py
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import alert_triage_state as ats  # noqa: E402


class _ATSTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prior_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(ats)

    def tearDown(self):
        if self._prior_root is not None:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prior_root
        else:
            del os.environ['OURLIBERTY_AGENTS_ROOT']
        importlib.reload(ats)

    def _state_path(self) -> Path:
        return self.tmp / ats.STATE_REL


class TestReadState(_ATSTestBase):

    def test_missing_file_returns_empty(self):
        self.assertEqual(ats.read_state(), {})

    def test_corrupt_json_returns_empty(self):
        self._state_path().parent.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text('{garbage')
        self.assertEqual(ats.read_state(), {})

    def test_corrupt_json_preserves_prior_bytes_to_sidecar(self):
        # Audit #37: a transient corruption must not silently discard prior
        # lifecycle rows. read_state() moves the corrupt file aside so its
        # bytes are recoverable, then returns {}.
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        original = '{"alert-A": {"x": 1}, "alert-B": {"y": 2}}TRUNCATED'
        path.write_text(original)

        self.assertEqual(ats.read_state(), {})

        # The live path is now gone (moved aside), and exactly one corrupt
        # sidecar holds the original bytes verbatim.
        self.assertFalse(path.exists())
        sidecars = sorted(p for p in path.parent.iterdir()
                          if p.name.startswith(f'{path.name}.corrupt-'))
        self.assertEqual(len(sidecars), 1)
        self.assertEqual(sidecars[0].read_text(), original)

    def test_corrupt_read_then_write_does_not_lose_history(self):
        # End-to-end: corrupt state, then a new record_triage write. The new
        # row lands, AND the prior bytes survive in the sidecar (recoverable),
        # rather than being atomically clobbered with no trace.
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        original = '{"alert-A": {"status": "triaged-tier-1"}}!!corrupt'
        path.write_text(original)

        ats.record_triage('alert-C', 1, 'auto', 'why')

        live = json.loads(path.read_text())
        self.assertEqual(list(live), ['alert-C'])  # forward progress
        sidecars = [p for p in path.parent.iterdir()
                    if p.name.startswith(f'{path.name}.corrupt-')]
        self.assertEqual(len(sidecars), 1)
        self.assertEqual(sidecars[0].read_text(), original)  # history preserved


class TestRecordTriage(_ATSTestBase):

    def test_initial_triage_creates_row(self):
        row = ats.record_triage('alert-1', tier=2, decision='dispatch',
                                rationale='matches healer pattern')
        self.assertEqual(row['status'], 'triaged-tier-2')
        self.assertEqual(row['tier'], 2)
        self.assertEqual(row['decision'], 'dispatch')
        self.assertIsNotNone(row['triaged_at'])
        self.assertIsNone(row['dispatched_at'])

    def test_re_triage_preserves_dispatched_at(self):
        ats.record_triage('alert-1', tier=2, decision='dispatch',
                          rationale='initial')
        ats.mark_dispatched('alert-1', dispatch_ts='2026-05-25T12:00:00Z',
                            target_agent='beacon', task_id='t-99')
        # Re-triage moves status back to triaged-tier-N but keeps
        # dispatch metadata for audit.
        row = ats.record_triage('alert-1', tier=3, decision='snooze',
                                rationale='auto-resolved upstream')
        self.assertEqual(row['status'], 'triaged-tier-3')
        self.assertEqual(row['dispatched_at'], '2026-05-25T12:00:00Z')
        self.assertEqual(row['dispatch_target_agent'], 'beacon')

    def test_invalid_tier_raises(self):
        with self.assertRaises(ValueError):
            ats.record_triage('alert-1', tier=9, decision='x', rationale='y')

    def test_empty_alert_id_raises(self):
        with self.assertRaises(ValueError):
            ats.record_triage('', tier=1, decision='x', rationale='y')


class TestMarkDispatched(_ATSTestBase):

    def test_dispatch_after_triage(self):
        ats.record_triage('a1', tier=1, decision='dispatch', rationale='ok')
        ok = ats.mark_dispatched('a1', '2026-05-25T12:30:00Z', 'forge', 'tid')
        self.assertTrue(ok)
        row = ats.read_state()['a1']
        self.assertEqual(row['status'], 'action-dispatched')
        self.assertEqual(row['dispatch_target_agent'], 'forge')
        self.assertEqual(row['dispatch_task_id'], 'tid')

    def test_dispatch_without_triage_is_noop(self):
        ok = ats.mark_dispatched('unknown', 'ts', 'agent', 'task')
        self.assertFalse(ok)
        self.assertEqual(ats.read_state(), {})


class TestMarkResolved(_ATSTestBase):

    def test_resolve_after_triage(self):
        ats.record_triage('a1', tier=1, decision='dispatch', rationale='ok')
        ats.mark_dispatched('a1', '2026-05-25T12:30:00Z', 'forge', 'tid')
        ok = ats.mark_resolved('a1', '2026-05-25T13:00:00Z', 'PR merged')
        self.assertTrue(ok)
        row = ats.read_state()['a1']
        self.assertEqual(row['status'], 'resolved')
        self.assertEqual(row['resolution'], 'PR merged')

    def test_resolve_without_dispatch_still_allowed(self):
        # Larry may resolve directly without a Pulse-dispatched action.
        ats.record_triage('a1', tier=1, decision='noop', rationale='manual')
        ok = ats.mark_resolved('a1', '2026-05-25T13:00:00Z', 'manual fix')
        self.assertTrue(ok)
        self.assertEqual(ats.read_state()['a1']['status'], 'resolved')

    def test_resolve_unknown_is_noop(self):
        ok = ats.mark_resolved('missing', 'ts', 'no action')
        self.assertFalse(ok)


class TestAtomicWrite(_ATSTestBase):

    def test_no_tmp_left_after_write(self):
        ats.record_triage('a1', tier=1, decision='x', rationale='y')
        leftovers = list(self._state_path().parent.glob('*.tmp'))
        self.assertEqual(leftovers, [])

    def test_state_file_is_valid_json_after_multiple_writes(self):
        ats.record_triage('a1', tier=1, decision='x', rationale='y')
        ats.record_triage('a2', tier=2, decision='z', rationale='w')
        ats.mark_dispatched('a1', 'ts', 'agent', 'task')
        data = json.loads(self._state_path().read_text())
        self.assertIn('a1', data)
        self.assertIn('a2', data)


# -------------------- Phase B: data-driven § 6.6 classification --------------------

# Deterministic route stub so tier classification tests don't depend on the live
# significance config: healed→closure, not-healed→escalate.
def _route_stub(source, subject, healed):
    return 'closure' if healed else 'escalate'


# Fixture registry (the only inputs classify reads, alongside translations).
_FIXTURE_REGISTRY = {
    'restart-daemon': {
        'template': 'restart-daemon', 'state': 'probation',
        'permanent_guard': False,
    },
    'reinstall-systemd-unit': {  # fixture-GRADUATED, non-guarded → Tier 1
        'template': 'reinstall-systemd-unit', 'state': 'graduated',
        'permanent_guard': False,
    },
    'rotate-credential': {  # fixture-GRADUATED but permanent_guard → floor at Tier 2
        'template': 'rotate-credential', 'state': 'graduated',
        'permanent_guard': True,
    },
}

_FIXTURE_TRANSLATIONS = {
    '_schema': {'note': 'metadata, never a source'},
    'heal-known': {
        'known-subject': {'severity': 'INFO', 'tier': 'FYI'},
        'surfaced-subject': {'severity': 'WARNING', 'tier': 'SOON',
                             'never_silence': True},
    },
    # outbox-notifier success alerts carry the pattern in `intent`, subject None;
    # delivery-confirmation alerts carry it only in `kind` (subject + intent None).
    'outbox-notifier': {
        'review-pass': {'severity': 'INFO', 'tier': 'FYI'},
        'approval_request': {'severity': 'INFO', 'tier': 'FYI'},
    },
    # source-level '*' catch-all: any subject under this source matches, but a
    # more-specific subject entry still wins (consulted before the '*' fallback).
    'pulse-cycle': {
        'cycle-blocked': {'severity': 'WARNING', 'tier': 'SOON'},
        '*': {'severity': 'INFO', 'tier': 'FYI'},
    },
    # '*' catch-all tagged never_silence: returned as a truthy dict so classify
    # routes it to Tier-4-surface, not Tier-3-mute.
    'wildcard-surfaced': {
        '*': {'severity': 'WARNING', 'tier': 'SOON', 'never_silence': True},
    },
    # Date-rotating subjects: a stable prefix key matches weekly-<date> /
    # check-i-<date> via the trailing-ISO-date-strip step. ledger is
    # single-purpose (only weekly-<date>); pulse keys 'check-i' NARROWLY so a
    # novel dated pulse subject still falls through to Tier 4.
    'ledger': {
        'weekly': {'severity': 'INFO', 'tier': 'FYI'},
    },
    'pulse': {
        'check-i': {'severity': 'INFO', 'tier': 'FYI'},
    },
}


class TestTranslationMatch(unittest.TestCase):

    def test_exact_subject_match(self):
        self.assertTrue(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-known', 'known-subject'))

    def test_prefix_strip_match(self):
        # subject 'known-subject:extra:detail' strips back to 'known-subject'.
        self.assertTrue(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-known', 'known-subject:extra:detail'))

    def test_unknown_source_no_match(self):
        self.assertFalse(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-other', 'known-subject'))

    def test_schema_key_is_not_a_source(self):
        self.assertFalse(ats._translation_match(
            _FIXTURE_TRANSLATIONS, '_schema', 'note'))

    def test_none_subject_no_match(self):
        self.assertFalse(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-known', None))

    def test_intent_fallback_when_subject_none(self):
        # outbox-notifier carries the pattern in `intent`, not `subject`. With
        # subject None the lookup falls back to intent and finds the entry.
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'outbox-notifier', None, 'review-pass')
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get('tier'), 'FYI')

    def test_both_subject_and_intent_none_no_match(self):
        # Regression guard for the existing 3-arg behavior: no key → None.
        self.assertIsNone(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'outbox-notifier', None, None))

    def test_kind_fallback_when_subject_and_intent_none(self):
        # approval_request delivery confirmations carry the pattern only in
        # `kind`, with subject AND intent both None. The lookup falls back to
        # kind (precedence subject -> intent -> kind) and finds the entry.
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'outbox-notifier', None, None,
            'approval_request')
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get('tier'), 'FYI')

    def test_match_returns_entry_dict(self):
        # The matcher returns the entry so callers can read directives like
        # never_silence; a hit is a (truthy) dict, a miss is None.
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-known', 'surfaced-subject')
        self.assertIsInstance(entry, dict)
        self.assertTrue(entry.get('never_silence'))
        self.assertIsNone(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-known', 'no-such-subject'))

    def test_wildcard_matches_arbitrary_novel_subject(self):
        # A source with a '*' entry catches ANY subject that misses exact +
        # prefix-strip. This is the pulse-cycle self-echo case: Pulse emits
        # ad-hoc per-escalation subjects, none of which are enumerated.
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'pulse-cycle',
            'agent-models-allowlist-not-on-main')
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get('tier'), 'FYI')

    def test_specific_subject_wins_over_wildcard(self):
        # cycle-blocked has its own entry; it must match before the '*' fallback
        # (the fallback is consulted only AFTER the prefix-strip loop misses).
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'pulse-cycle', 'cycle-blocked')
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get('tier'), 'SOON')
        # And the dynamic-suffix form strips back to cycle-blocked, still not '*'.
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'pulse-cycle', 'cycle-blocked:dirty-tree-on-x')
        self.assertEqual(entry.get('tier'), 'SOON')

    def test_wildcard_never_silence_returned_truthy(self):
        # A '*' entry tagged never_silence is returned as a truthy dict so
        # classify Gate 1 routes it to Tier-4-surface rather than Tier-3-mute.
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'wildcard-surfaced', 'anything-here')
        self.assertIsInstance(entry, dict)
        self.assertTrue(entry.get('never_silence'))

    def test_source_without_wildcard_still_misses(self):
        # No '*' entry → a subject miss still returns None (no behavior change
        # for sources that never opted into the catch-all).
        self.assertIsNone(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-known', 'no-such-subject'))

    def test_iso_date_suffix_strips_to_stable_key(self):
        # weekly-<date> strips its trailing -YYYY-MM-DD back to the 'weekly'
        # entry; the rotating date no longer defeats the match.
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'ledger', 'weekly-2026-06-15')
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get('tier'), 'FYI')
        # And a different future date hits the same stable key.
        self.assertIsInstance(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'ledger', 'weekly-2027-01-04'), dict)

    def test_iso_date_suffix_strips_for_pulse_check_i(self):
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'pulse', 'check-i-2026-06-15')
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry.get('tier'), 'FYI')

    def test_iso_date_strip_does_not_over_silence_novel_pulse(self):
        # A novel dated pulse subject strips to a key NOT in the map → None.
        # The narrow 'check-i' key must not catch arbitrary pulse escalations.
        self.assertIsNone(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'pulse', 'mirror-escalate-2026-06-15'))
        # Non-dated pulse subjects are equally unaffected (no suffix to strip).
        self.assertIsNone(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'pulse', 'unreviewed-merge:494'))

    def test_iso_date_strip_inert_without_date_suffix(self):
        # A bare 'weekly' (no date) is an exact match already; a 'weekly'-less
        # subject with no suffix and no entry still misses. The strip only fires
        # on a genuine -YYYY-MM-DD tail.
        self.assertIsInstance(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'ledger', 'weekly'), dict)
        self.assertIsNone(ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'ledger', 'something-else'))

    def test_iso_date_strip_leaves_colon_strip_matches_intact(self):
        # A ':'-suffixed subject still resolves via the existing prefix-strip
        # loop and never reaches the date-strip step (no -YYYY-MM-DD tail).
        entry = ats._translation_match(
            _FIXTURE_TRANSLATIONS, 'heal-known', 'known-subject:extra:detail')
        self.assertIsInstance(entry, dict)
        # And the pulse-cycle '*' echo path is untouched by the date-strip.
        self.assertEqual(
            ats._translation_match(
                _FIXTURE_TRANSLATIONS, 'pulse-cycle', 'novel-echo-subject'
            ).get('tier'), 'FYI')


class TestClassify(unittest.TestCase):

    def _classify(self, alert):
        return ats.classify(alert, registry=_FIXTURE_REGISTRY,
                            translations=_FIXTURE_TRANSLATIONS,
                            route_fn=_route_stub)

    def test_tier3_known_pattern_silences_to_digest(self):
        r = self._classify({'source': 'heal-known', 'subject': 'known-subject'})
        self.assertEqual(r['tier'], 3)
        self.assertEqual(r['route'], 'digest')
        self.assertEqual(r['decision'], 'silence')

    def test_tier3_short_circuits_before_registry(self):
        # A signal that ALSO carries a graduated template must still Tier-3 when
        # it matches the translation table — gate order is translations first.
        r = self._classify({'source': 'heal-known', 'subject': 'known-subject',
                            'template': 'reinstall-systemd-unit'})
        self.assertEqual(r['tier'], 3)

    def test_tier3_intent_fallback_silences_to_digest(self):
        # outbox-notifier success: subject is None, pattern carried in intent.
        # The gate-1 translation match must succeed via the intent fallback and
        # silence to digest instead of falling through to Tier 4.
        r = self._classify({'source': 'outbox-notifier', 'subject': None,
                            'intent': 'review-pass'})
        self.assertEqual(r['tier'], 3)
        self.assertEqual(r['route'], 'digest')
        self.assertEqual(r['decision'], 'silence')

    def test_tier3_kind_fallback_silences_to_digest(self):
        # outbox-notifier approval_request delivery confirmation: subject AND
        # intent are None, the pattern is carried only in `kind`. The gate-1
        # translation match must succeed via the kind fallback and silence to
        # digest instead of falling through to Tier 4.
        r = self._classify({'source': 'outbox-notifier',
                            'kind': 'approval_request'})
        self.assertEqual(r['tier'], 3)
        self.assertEqual(r['route'], 'digest')
        self.assertEqual(r['decision'], 'silence')

    def test_never_silence_match_escalates_not_silenced(self):
        # A known pattern flagged never_silence (e.g. a dark pulse check) must
        # NOT be Tier-3-muted to digest — it falls through to Tier 4 so it
        # escalates with its translation intact.
        r = self._classify({'source': 'heal-known',
                            'subject': 'surfaced-subject'})
        self.assertEqual(r['tier'], 4)
        self.assertEqual(r['route'], 'escalate')
        self.assertNotEqual(r['decision'], 'silence')
        self.assertIn('never-silence', r['rationale'])

    def test_never_silence_prefix_strip_still_escalates(self):
        # The id-suffixed form (subject 'surfaced-subject:iv') strips back to the
        # never_silence entry and still escalates rather than being muted.
        r = self._classify({'source': 'heal-known',
                            'subject': 'surfaced-subject:iv'})
        self.assertEqual(r['tier'], 4)
        self.assertEqual(r['route'], 'escalate')

    def test_pulse_cycle_novel_subject_silences_via_wildcard(self):
        # The self-echo case: a novel pulse-cycle subject (not enumerated) hits
        # the source-level '*' catch-all and silences to digest, so Check 0 does
        # NOT re-classify Pulse's own re-read emission as a fresh Tier-4 alert.
        r = self._classify({'source': 'pulse-cycle',
                            'subject': 'agent-models-allowlist-not-on-main'})
        self.assertEqual(r['tier'], 3)
        self.assertEqual(r['route'], 'digest')
        self.assertEqual(r['decision'], 'silence')

    def test_tier3_ledger_weekly_dated_subject_silences(self):
        # The bug fix: a routine weekly ledger report (weekly-<date>) silences
        # to digest instead of re-escalating as a fresh Tier-4 alert every cycle.
        r = self._classify({'source': 'ledger', 'subject': 'weekly-2026-06-15'})
        self.assertEqual(r['tier'], 3)
        self.assertEqual(r['route'], 'digest')
        self.assertEqual(r['decision'], 'silence')
        # A future date hits the same stable key — the fix is not date-bound.
        self.assertEqual(
            self._classify({'source': 'ledger',
                            'subject': 'weekly-2027-03-01'})['tier'], 3)

    def test_tier3_pulse_check_i_dated_subject_silences(self):
        r = self._classify({'source': 'pulse',
                            'subject': 'check-i-2026-06-15'})
        self.assertEqual(r['tier'], 3)
        self.assertEqual(r['route'], 'digest')
        self.assertEqual(r['decision'], 'silence')

    def test_novel_dated_pulse_subject_still_tier4(self):
        # Regression guard against over-silencing: a novel dated pulse subject
        # (NOT check-i) must still classify Tier 4 — the narrow key + date-strip
        # must not swallow genuine one-off pulse escalations.
        r = self._classify({'source': 'pulse',
                            'subject': 'unreviewed-merge-2026-06-15'})
        self.assertEqual(r['tier'], 4)
        self.assertEqual(r['route'], 'escalate')

    def test_tier2_probation_template_asks(self):
        r = self._classify({'source': 's', 'subject': 'sub',
                            'template': 'restart-daemon'})
        self.assertEqual(r['tier'], 2)
        self.assertEqual(r['route'], 'escalate')
        self.assertEqual(r['decision'], 'ask')
        self.assertEqual(r['template'], 'restart-daemon')

    def test_tier1_graduated_nonguarded_autofixes(self):
        r = self._classify({'source': 's', 'subject': 'sub',
                            'template': 'reinstall-systemd-unit'})
        self.assertEqual(r['tier'], 1)
        self.assertEqual(r['decision'], 'auto-fix')
        self.assertEqual(r['template'], 'reinstall-systemd-unit')
        # Tier-1 route comes from route_fn(healed=True) → closure here.
        self.assertEqual(r['route'], 'closure')

    def test_permanent_guard_never_tier1_even_if_graduated(self):
        # rotate-credential is fixture-graduated BUT permanent_guard → the floor
        # holds: Tier 2 (ask), never Tier 1.
        r = self._classify({'source': 's', 'subject': 'sub',
                            'template': 'rotate-credential'})
        self.assertEqual(r['tier'], 2)
        self.assertEqual(r['route'], 'escalate')
        self.assertIn('permanent_guard', r['rationale'])

    def test_tier4_novel_asks(self):
        r = self._classify({'source': 's', 'subject': 'sub',
                            'template': 'not-in-registry'})
        self.assertEqual(r['tier'], 4)
        self.assertEqual(r['route'], 'escalate')
        self.assertIsNone(r['template'])

    def test_no_template_no_translation_is_tier4(self):
        r = self._classify({'source': 's', 'subject': 'sub'})
        self.assertEqual(r['tier'], 4)


class TestTriageAlert(_ATSTestBase):
    """Orchestration: classify → persist → (Tier-1) ledger link, idempotent."""

    def _ledger_rows(self):
        import cycle_prime_ledger as cpl  # noqa: E402
        path = self.tmp / cpl.LEDGER_REL
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text().splitlines() if line.strip()]

    def _triage(self, alert_id, alert, **kw):
        return ats.triage_alert(alert_id, alert, registry=_FIXTURE_REGISTRY,
                               translations=_FIXTURE_TRANSLATIONS,
                               route_fn=_route_stub, **kw)

    def test_tier1_dispatches_and_records_tagged_ledger_intervention(self):
        row = self._triage('a-grad', {'source': 's', 'subject': 'sub',
                                      'template': 'reinstall-systemd-unit'},
                           iter_num=42)
        self.assertEqual(row['status'], 'action-dispatched')
        self.assertEqual(row['tier'], 1)
        self.assertEqual(row['route'], 'closure')
        self.assertEqual(row['dispatch_target_agent'], ats.AUTO_FIX_AGENT)
        # The B→C link: exactly one tagged intervention with the pattern template.
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'intervention')
        self.assertEqual(rows[0]['tier'], 1)
        self.assertEqual(rows[0]['iter'], 42)
        self.assertTrue(
            rows[0]['intervention_id'].startswith('reinstall-systemd-unit:'))

    def test_tier1_is_idempotent_no_double_ledger_write(self):
        alert = {'source': 's', 'subject': 'sub',
                 'template': 'reinstall-systemd-unit'}
        self._triage('a-grad', alert)
        row2 = self._triage('a-grad', alert)  # re-run same iter/alert
        self.assertEqual(row2['status'], 'action-dispatched')
        # Ledger written exactly once despite two triage passes.
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_tier3_silence_resolves_and_writes_no_ledger(self):
        row = self._triage('a-known', {'source': 'heal-known',
                                       'subject': 'known-subject'})
        self.assertEqual(row['status'], 'resolved')
        self.assertEqual(row['route'], 'digest')
        self.assertEqual(self._ledger_rows(), [])

    def test_tier3_resolved_is_idempotent(self):
        alert = {'source': 'heal-known', 'subject': 'known-subject'}
        self._triage('a-known', alert)
        first_resolved_at = ats.read_state()['a-known']['resolved_at']
        row2 = self._triage('a-known', alert)
        # Re-run is a no-op: same resolved_at, still no ledger.
        self.assertEqual(row2['resolved_at'], first_resolved_at)
        self.assertEqual(self._ledger_rows(), [])

    def test_tier2_probation_awaits_larry_no_ledger(self):
        row = self._triage('a-prob', {'source': 's', 'subject': 'sub',
                                      'template': 'restart-daemon'})
        self.assertEqual(row['status'], 'triaged-tier-2')
        self.assertEqual(row['route'], 'escalate')
        self.assertIsNone(row['dispatched_at'])
        self.assertEqual(self._ledger_rows(), [])

    def test_permanent_guard_graduated_stays_tier2_no_ledger(self):
        row = self._triage('a-guard', {'source': 's', 'subject': 'sub',
                                       'template': 'rotate-credential'})
        self.assertEqual(row['status'], 'triaged-tier-2')
        self.assertEqual(self._ledger_rows(), [])

    def test_tier4_novel_awaits_larry_no_ledger(self):
        row = self._triage('a-novel', {'source': 's', 'subject': 'sub'})
        self.assertEqual(row['status'], 'triaged-tier-4')
        self.assertEqual(row['route'], 'escalate')
        self.assertEqual(self._ledger_rows(), [])


class TestTriageExecutionRecording(_ATSTestBase):
    """Phase C streak INPUT: triage_alert must append an executions record for
    BOTH Tier-1 auto-fixes AND Tier-2 approved-probation fixes, persisting the
    clean/adverse signal so a probation pattern can accrue a track record."""

    def _triage(self, alert_id, alert, **kw):
        return ats.triage_alert(alert_id, alert, registry=_FIXTURE_REGISTRY,
                               translations=_FIXTURE_TRANSLATIONS,
                               route_fn=_route_stub, **kw)

    def _execs(self, template):
        path = self.tmp / ats.ACTION_TEMPLATE_EXEC_REL
        if not path.exists():
            return []
        doc = json.loads(path.read_text())
        return doc.get('action_templates', {}).get(template, {}).get(
            'executions', [])

    def test_tier1_records_one_unverified_execution(self):
        # Item (d): a fix that just ran is 'unverified' by default — the fix
        # executed but no verifier has confirmed the fault cleared. Only a
        # verifier path upgrades it to 'success'.
        self._triage('a-grad', {'source': 's', 'subject': 'sub',
                                'template': 'reinstall-systemd-unit'})
        execs = self._execs('reinstall-systemd-unit')
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]['outcome'], 'unverified')
        self.assertFalse(execs[0]['larry_correction_signal'])

    def test_tier2_approved_records_execution_and_dispatches(self):
        row = self._triage('a-prob', {'source': 's', 'subject': 'sub',
                                      'template': 'restart-daemon'},
                           apply_approved_fix=True)
        self.assertEqual(row['status'], 'action-dispatched')
        execs = self._execs('restart-daemon')
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]['outcome'], 'unverified')
        self.assertFalse(execs[0]['larry_correction_signal'])

    def test_explicit_success_outcome_is_honored(self):
        # A verifier path passes outcome='success' explicitly -> recorded as-is.
        self._triage('a-grad', {'source': 's', 'subject': 'sub',
                                'template': 'reinstall-systemd-unit'},
                     outcome='success')
        execs = self._execs('reinstall-systemd-unit')
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]['outcome'], 'success')

    def test_tier2_without_approval_records_nothing(self):
        row = self._triage('a-prob', {'source': 's', 'subject': 'sub',
                                      'template': 'restart-daemon'})
        self.assertEqual(row['status'], 'triaged-tier-2')
        self.assertEqual(self._execs('restart-daemon'), [])

    def test_tier2_approved_correction_persists_signal(self):
        self._triage('a-prob', {'source': 's', 'subject': 'sub',
                                'template': 'restart-daemon'},
                     apply_approved_fix=True, larry_correction_signal=True)
        execs = self._execs('restart-daemon')
        self.assertEqual(len(execs), 1)
        self.assertTrue(execs[0]['larry_correction_signal'])

    def test_tier2_approved_failure_persists_outcome(self):
        self._triage('a-prob', {'source': 's', 'subject': 'sub',
                                'template': 'restart-daemon'},
                     apply_approved_fix=True, outcome='failure')
        execs = self._execs('restart-daemon')
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]['outcome'], 'failure')


class TestOutcomeDefaults(_ATSTestBase):
    """Item (d): 'unverified' is a first-class outcome and the recorder default;
    the triage-alert CLI exposes a validated --outcome flag."""

    def test_valid_outcomes_membership(self):
        self.assertEqual(set(ats.VALID_OUTCOMES),
                         {'success', 'failure', 'unverified'})

    def test_record_execution_defaults_to_unverified(self):
        ats.record_action_template_execution('some-template')
        doc = json.loads((self.tmp / ats.ACTION_TEMPLATE_EXEC_REL).read_text())
        execs = doc['action_templates']['some-template']['executions']
        self.assertEqual(execs[-1]['outcome'], 'unverified')

    def test_record_execution_rejects_unknown_outcome(self):
        with self.assertRaises(ValueError):
            ats.record_action_template_execution('t', outcome='bogus')

    def test_cli_outcome_flag_rejects_invalid_choice(self):
        with self.assertRaises(SystemExit):
            ats.main(['triage-alert', '--alert-id', 'x',
                      '--alert', '{}', '--outcome', 'bogus'])

    def test_cli_outcome_flag_accepts_valid_choice(self):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            rc = ats.main(['triage-alert', '--alert-id', 'x',
                           '--alert', json.dumps({'source': 's', 'subject': 'sub'}),
                           '--outcome', 'unverified'])
        self.assertEqual(rc, 0)


class TestWatermark(_ATSTestBase):
    """The dedicated Check 0 line watermark store must survive lifecycle writes.

    The whole reason it lives in its own file: a scalar inside alert-triage.json
    is filtered by read_state() and clobbered by _write_state. These tests prove
    the separate store is immune to that."""

    def _watermark_path(self) -> Path:
        return self.tmp / ats.WATERMARK_REL

    def test_missing_file_returns_none(self):
        self.assertIsNone(ats.read_watermark())

    def test_round_trip(self):
        ats.write_watermark(4242)
        self.assertEqual(ats.read_watermark(), 4242)

    def test_overwrite_advances(self):
        ats.write_watermark(10)
        ats.write_watermark(99)
        self.assertEqual(ats.read_watermark(), 99)

    def test_survives_multiple_lifecycle_writes(self):
        # THE load-bearing regression: a watermark written first must still be
        # readable after several record_triage / mark_dispatched writes against
        # the SEPARATE alert-triage.json. This is exactly what the old
        # co-located-scalar design failed.
        ats.write_watermark(1542)
        ats.record_triage('alert-1', tier=2, decision='dispatch', rationale='a')
        ats.record_triage('alert-2', tier=1, decision='auto', rationale='b')
        ats.mark_dispatched('alert-1', 'ts', 'forge', 'tid')
        ats.record_triage('alert-3', tier=4, decision='ask', rationale='c')
        self.assertEqual(ats.read_watermark(), 1542)

    def test_corrupt_file_returns_none(self):
        path = self._watermark_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not json')
        self.assertIsNone(ats.read_watermark())

    def test_wrong_shape_returns_none(self):
        path = self._watermark_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[1, 2, 3]')
        self.assertIsNone(ats.read_watermark())

    def test_missing_key_returns_none(self):
        path = self._watermark_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"something_else": 7}')
        self.assertIsNone(ats.read_watermark())

    def test_non_int_key_returns_none(self):
        path = self._watermark_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"last_claimed_line": "nope"}')
        self.assertIsNone(ats.read_watermark())

    def test_bool_value_rejected(self):
        # bool is an int subclass in Python; a boolean watermark is meaningless.
        path = self._watermark_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"last_claimed_line": true}')
        self.assertIsNone(ats.read_watermark())

    def test_write_preserves_other_keys(self):
        path = self._watermark_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"last_claimed_line": 1, "note": "keep me"}')
        ats.write_watermark(2)
        doc = json.loads(path.read_text())
        self.assertEqual(doc['last_claimed_line'], 2)
        self.assertEqual(doc['note'], 'keep me')

    def test_write_rejects_non_int(self):
        with self.assertRaises(ValueError):
            ats.write_watermark('5')  # type: ignore[arg-type]

    def test_cli_get_missing_prints_missing(self):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            rc = ats.main(['get-watermark'])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), 'MISSING')

    def test_cli_set_then_get(self):
        from io import StringIO
        from contextlib import redirect_stdout
        self.assertEqual(ats.main(['set-watermark', '--line', '777']), 0)
        buf = StringIO()
        with redirect_stdout(buf):
            ats.main(['get-watermark'])
        self.assertEqual(buf.getvalue().strip(), '777')


class TestWatermarkRepair(_ATSTestBase):
    """repair-watermark self-heals a stale (too-large) watermark after the
    retention/compaction job shrinks larry-alerts.jsonl below the recorded
    absolute line number. Reset is to file_length EXACTLY (not trailing-N)."""

    def setUp(self):
        super().setUp()
        # Point the file-length source at a tmp alerts log we control; the
        # helper reads it via the module attribute so this patch takes effect.
        self._prior_alerts_file = ats.larry_alerts.ALERTS_FILE
        self._alerts = self.tmp / 'larry-alerts.jsonl'
        ats.larry_alerts.ALERTS_FILE = self._alerts

    def tearDown(self):
        ats.larry_alerts.ALERTS_FILE = self._prior_alerts_file
        super().tearDown()

    def _write_alerts(self, n: int) -> None:
        self._alerts.write_text(''.join(f'{{"i": {i}}}\n' for i in range(n)))

    def test_gap_resets_to_file_length(self):
        # File compacted down to 5 lines but watermark still points at 1200.
        self._write_alerts(5)
        ats.write_watermark(1200)
        result = ats.repair_watermark()
        self.assertEqual(result, {
            'repaired': True, 'old_watermark': 1200,
            'file_length': 5, 'new_watermark': 5,
        })
        self.assertEqual(ats.read_watermark(), 5)

    def test_noop_when_watermark_within_file(self):
        self._write_alerts(50)
        ats.write_watermark(40)
        result = ats.repair_watermark()
        self.assertEqual(result, {
            'repaired': False, 'old_watermark': 40, 'file_length': 50,
        })
        self.assertEqual(ats.read_watermark(), 40)

    def test_noop_when_watermark_equals_file_length(self):
        # Boundary: watermark == file_length is NOT a gap (nothing after it).
        self._write_alerts(30)
        ats.write_watermark(30)
        result = ats.repair_watermark()
        self.assertFalse(result['repaired'])
        self.assertEqual(ats.read_watermark(), 30)

    def test_missing_watermark_is_noop(self):
        # None is owned by the trailing-100 catchup path, not repair.
        self._write_alerts(10)
        result = ats.repair_watermark()
        self.assertEqual(result, {
            'repaired': False, 'old_watermark': None, 'file_length': 10,
        })
        self.assertIsNone(ats.read_watermark())

    def test_missing_alerts_file_is_zero_length(self):
        # No alerts file -> file_length 0; any concrete watermark > 0 repairs to 0.
        ats.write_watermark(7)
        result = ats.repair_watermark()
        self.assertEqual(result, {
            'repaired': True, 'old_watermark': 7,
            'file_length': 0, 'new_watermark': 0,
        })
        self.assertEqual(ats.read_watermark(), 0)

    def test_idempotent_second_run_is_noop(self):
        self._write_alerts(3)
        ats.write_watermark(900)
        first = ats.repair_watermark()
        self.assertTrue(first['repaired'])
        second = ats.repair_watermark()
        self.assertEqual(second, {
            'repaired': False, 'old_watermark': 3, 'file_length': 3,
        })
        self.assertEqual(ats.read_watermark(), 3)

    def test_cli_emits_single_json_object(self):
        from io import StringIO
        from contextlib import redirect_stdout
        self._write_alerts(2)
        ats.write_watermark(500)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = ats.main(['repair-watermark'])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())  # parses as exactly one object
        self.assertEqual(payload, {
            'repaired': True, 'old_watermark': 500,
            'file_length': 2, 'new_watermark': 2,
        })


class TestLoaders(_ATSTestBase):

    def test_missing_registry_is_empty(self):
        self.assertEqual(ats.load_registry(self.tmp / 'nope.json'), {})

    def test_missing_translations_is_empty(self):
        self.assertEqual(ats.load_translations(self.tmp / 'nope.json'), {})

    def test_live_registry_loads_seeded_templates(self):
        # The real config must parse and expose the seeded probation templates.
        reg = ats.load_registry()
        self.assertIn('restart-daemon', reg)
        self.assertIn('rotate-credential', reg)


class TestLastTriagedIterPersistence(_ATSTestBase):
    """The guard substrate: record_triage stores last_triaged_iter, triage_alert
    threads iter_num, and legacy/direct calls default it to None (back-compat)."""

    def test_record_triage_defaults_iter_none(self):
        row = ats.record_triage('a', 3, 'silence', 'why')
        self.assertIsNone(row['last_triaged_iter'])
        self.assertIsNone(ats.read_state()['a']['last_triaged_iter'])

    def test_record_triage_persists_explicit_iter(self):
        row = ats.record_triage('a', 4, 'ask', 'novel', last_triaged_iter=17)
        self.assertEqual(row['last_triaged_iter'], 17)
        self.assertEqual(ats.read_state()['a']['last_triaged_iter'], 17)

    def test_record_triage_normalizes_bool_iter_to_none(self):
        # A bool is not a valid iter (isinstance(True, int) is True in Python).
        row = ats.record_triage('a', 3, 'silence', 'why', last_triaged_iter=True)
        self.assertIsNone(row['last_triaged_iter'])

    def test_triage_alert_threads_iter_num_onto_row(self):
        ats.triage_alert('a-novel', {'source': 's', 'subject': 'sub'},
                        iter_num=88, registry=_FIXTURE_REGISTRY,
                        translations=_FIXTURE_TRANSLATIONS, route_fn=_route_stub)
        self.assertEqual(ats.read_state()['a-novel']['last_triaged_iter'], 88)

    def test_triage_alert_idempotent_preserves_iter(self):
        alert = {'source': 's', 'subject': 'sub'}
        ats.triage_alert('a-novel', alert, iter_num=5, registry=_FIXTURE_REGISTRY,
                        translations=_FIXTURE_TRANSLATIONS, route_fn=_route_stub)
        # A re-run at a DIFFERENT iter is idempotent for Tier-4-awaiting? No —
        # Tier 4 stays 'triaged-tier-4' (not terminal), so it re-triages. But a
        # terminal (resolved) alert stays put with its original iter.
        ats.triage_alert('a-known', {'source': 'heal-known',
                                     'subject': 'known-subject'},
                        iter_num=9, registry=_FIXTURE_REGISTRY,
                        translations=_FIXTURE_TRANSLATIONS, route_fn=_route_stub)
        ats.triage_alert('a-known', {'source': 'heal-known',
                                     'subject': 'known-subject'},
                        iter_num=99, registry=_FIXTURE_REGISTRY,
                        translations=_FIXTURE_TRANSLATIONS, route_fn=_route_stub)
        # Resolved on iter 9; the idempotent re-run at iter 99 does not overwrite.
        self.assertEqual(ats.read_state()['a-known']['status'], 'resolved')
        self.assertEqual(ats.read_state()['a-known']['last_triaged_iter'], 9)


class TestGuardTier4(_ATSTestBase):
    """The Tier-4 override guard: the deterministic helper is authoritative over
    any in-prompt Tier-4 claim (spec check0-tier4-guard-001)."""

    def _guard(self, alert_id, alert, *, iter_num, claimed_tier=4):
        return ats.guard_tier4(alert_id, alert, iter_num=iter_num,
                               claimed_tier=claimed_tier,
                               registry=_FIXTURE_REGISTRY,
                               translations=_FIXTURE_TRANSLATIONS,
                               route_fn=_route_stub)

    def _record_same_iter(self, alert_id, alert, iter_num):
        """Persist a same-iter triage-alert row (the (a) precondition)."""
        ats.triage_alert(alert_id, alert, iter_num=iter_num,
                        registry=_FIXTURE_REGISTRY,
                        translations=_FIXTURE_TRANSLATIONS, route_fn=_route_stub)

    # (a) The incident, on LIVE config: a medic/medic-diagnosis alert the helper
    # classifies Tier 3, with the LLM claiming Tier 4. Rejected → Tier 3.
    def test_incident_medic_diagnosis_helper3_claim4_rejected(self):
        alert = {'source': 'medic', 'subject': None, 'intent': 'medic-diagnosis'}
        # Ground truth: the live helper classifies this Tier 3, decision silence.
        helper = ats.classify(alert, registry=ats.load_registry(),
                             translations=ats.load_translations())
        self.assertEqual(helper['tier'], 3)
        self.assertEqual(helper['decision'], 'silence')
        # The compliant path recorded a same-iter triage-alert call (helper=3).
        ats.triage_alert('medic-diag', alert, iter_num=7)
        # The LLM now claims Tier 4 anyway. The guard (LIVE config) rejects it.
        verdict = ats.guard_tier4('medic-diag', alert, iter_num=7, claimed_tier=4)
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['helper_tier'], 3)
        self.assertEqual(verdict['authoritative_tier'], 3)
        self.assertTrue(verdict['same_iter_call'])

    # (b) Skip-helper: a genuinely-novel alert (helper==4) but NO same-iter row.
    # Rejected → the write cannot land via the unguarded path.
    def test_skip_helper_novel_no_same_iter_call_rejected(self):
        alert = {'source': 's', 'subject': 'sub'}  # fixture-novel → Tier 4
        verdict = self._guard('a-novel', alert, iter_num=3)
        self.assertFalse(verdict['accepted'])
        self.assertFalse(verdict['same_iter_call'])
        self.assertEqual(verdict['helper_tier'], 4)
        # Authoritative falls to the helper's tier (which is genuinely 4), but
        # accepted=False keeps the unguarded write out — the fix is to persist
        # via the triage-alert path, which records the same-iter call.
        self.assertEqual(verdict['authoritative_tier'], verdict['helper_tier'])

    # (c) Genuine novel: helper==4 AND a same-iter triage-alert call → accepted.
    def test_genuine_novel_with_same_iter_call_accepted_tier4(self):
        alert = {'source': 's', 'subject': 'sub'}  # fixture-novel → Tier 4
        self._record_same_iter('a-novel', alert, iter_num=12)
        verdict = self._guard('a-novel', alert, iter_num=12)
        self.assertTrue(verdict['accepted'])
        self.assertTrue(verdict['same_iter_call'])
        self.assertEqual(verdict['helper_tier'], 4)
        self.assertEqual(verdict['authoritative_tier'], 4)

    # (d) Stale-iter: a row exists but last_triaged_iter != current iter.
    def test_stale_iter_rejected_falls_to_helper(self):
        alert = {'source': 's', 'subject': 'sub'}  # helper == 4
        self._record_same_iter('a-novel', alert, iter_num=5)
        verdict = self._guard('a-novel', alert, iter_num=6)  # different iter
        self.assertFalse(verdict['accepted'])
        self.assertFalse(verdict['same_iter_call'])
        self.assertEqual(verdict['authoritative_tier'], verdict['helper_tier'])

    def test_stale_iter_helper3_adopts_tier3(self):
        # Stale iter AND a Tier-3 helper: unambiguously falls to Tier 3.
        alert = {'source': 'heal-known', 'subject': 'known-subject'}
        self._record_same_iter('a-known', alert, iter_num=5)
        verdict = self._guard('a-known', alert, iter_num=6)
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['authoritative_tier'], 3)

    # (e) Missing/unparseable alert never yields Tier 4.
    def test_missing_alert_never_tier4(self):
        verdict = ats.guard_tier4('a', None, iter_num=1, claimed_tier=4)
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['authoritative_tier'], ats.GUARD_FALLBACK_TIER)
        self.assertNotEqual(verdict['authoritative_tier'], 4)

    def test_missing_alert_never_tier4_even_with_same_iter_row(self):
        # Even a valid same-iter row can't manufacture a Tier 4 from an
        # unreadable alert — classify() can't run, so it falls to the safe tier.
        ats.record_triage('a', 4, 'ask', 'novel', last_triaged_iter=4)
        verdict = ats.guard_tier4('a', None, iter_num=4, claimed_tier=4)
        self.assertTrue(verdict['same_iter_call'])
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['authoritative_tier'], ats.GUARD_FALLBACK_TIER)

    def test_non_dict_alert_never_tier4(self):
        for bad in (['a', 'list'], 'a-string', 42, None):
            verdict = ats.guard_tier4('a', bad, iter_num=1, claimed_tier=4)
            self.assertFalse(verdict['accepted'])
            self.assertNotEqual(verdict['authoritative_tier'], 4)


class TestGuardTier4CLI(_ATSTestBase):
    """The write-boundary + verdict-surface CLIs."""

    def _run(self, argv):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            rc = ats.main(argv)
        out = buf.getvalue().strip()
        return rc, (json.loads(out) if out else None)

    def test_guard_tier4_cli_emits_single_verdict_object(self):
        alert = json.dumps({'source': 'novel-src-xyz', 'subject': 'unheard'})
        rc, payload = self._run(['guard-tier4', '--alert-id', 'n', '--alert',
                                 alert, '--iter', '2', '--claimed-tier', '4'])
        self.assertEqual(rc, 0)
        self.assertEqual(set(payload), {'authoritative_tier', 'accepted',
                                        'helper_tier', 'same_iter_call', 'reason'})
        self.assertFalse(payload['accepted'])  # no same-iter row

    def test_guard_tier4_cli_unparseable_alert_never_tier4(self):
        rc, payload = self._run(['guard-tier4', '--alert-id', 'n', '--alert',
                                 '{bad json', '--iter', '2', '--claimed-tier', '4'])
        self.assertEqual(rc, 0)
        self.assertFalse(payload['accepted'])
        self.assertEqual(payload['authoritative_tier'], ats.GUARD_FALLBACK_TIER)

    def test_triage_cli_tier4_requires_alert_and_iter(self):
        rc, payload = self._run(['triage', '--alert-id', 'x', '--tier', '4',
                                 '--decision', 'ask', '--rationale', 'r'])
        self.assertEqual(rc, 1)
        self.assertFalse(payload['accepted'])
        # Nothing was persisted.
        self.assertEqual(ats.read_state(), {})

    def test_triage_cli_tier4_rejected_when_no_same_iter_call(self):
        alert = json.dumps({'source': 'novel-src-xyz', 'subject': 'unheard'})
        rc, payload = self._run(['triage', '--alert-id', 'x', '--tier', '4',
                                 '--decision', 'ask', '--rationale', 'r',
                                 '--alert', alert, '--iter', '3'])
        self.assertEqual(rc, 1)
        self.assertFalse(payload['accepted'])
        self.assertEqual(ats.read_state(), {})  # write rejected

    def test_triage_cli_tier4_accepted_after_triage_alert_call(self):
        alert = json.dumps({'source': 'novel-src-xyz', 'subject': 'unheard'})
        # Compliant flow: triage-alert first (records the same-iter row, helper
        # classifies this LIVE-novel alert Tier 4), THEN the low-level write.
        rc0, _ = self._run(['triage-alert', '--alert-id', 'x', '--alert', alert,
                            '--iter', '9'])
        self.assertEqual(rc0, 0)
        rc, row = self._run(['triage', '--alert-id', 'x', '--tier', '4',
                             '--decision', 'ask', '--rationale', 'r',
                             '--alert', alert, '--iter', '9'])
        self.assertEqual(rc, 0)
        self.assertEqual(row['tier'], 4)
        self.assertEqual(row['last_triaged_iter'], 9)

    def test_triage_cli_incident_medic_diagnosis_rejected(self):
        # End-to-end at the CLI boundary: the exact incident is refused.
        alert = json.dumps({'source': 'medic', 'subject': None,
                            'intent': 'medic-diagnosis'})
        self._run(['triage-alert', '--alert-id', 'md', '--alert', alert,
                  '--iter', '4'])  # helper records Tier 3
        rc, payload = self._run(['triage', '--alert-id', 'md', '--tier', '4',
                                 '--decision', 'ask', '--rationale', 'novel',
                                 '--alert', alert, '--iter', '4'])
        self.assertEqual(rc, 1)
        self.assertEqual(payload['authoritative_tier'], 3)
        # The persisted row is still Tier 3 (resolved), never overwritten to 4.
        self.assertEqual(ats.read_state()['md']['tier'], 3)

    def test_triage_cli_lower_tiers_unaffected(self):
        # Tiers 1/2/3 need no --alert/--iter and write normally (back-compat).
        for tier, decision in ((1, 'auto-fix'), (2, 'ask'), (3, 'silence')):
            rc, row = self._run(['triage', '--alert-id', f't{tier}', '--tier',
                                 str(tier), '--decision', decision,
                                 '--rationale', 'r'])
            self.assertEqual(rc, 0)
            self.assertEqual(row['tier'], tier)
            self.assertIsNone(row['last_triaged_iter'])


if __name__ == '__main__':
    unittest.main()
