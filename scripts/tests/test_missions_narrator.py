#!/usr/bin/env python3
"""Tests for scripts/missions_narrator.py — the Phase 4 Narrator pass.

Spec: agents/beacon/specs/missions-v2-phase4-meaning-layer.md § 5 / § 11.

The § 11 1a proof obligations covered here:
  - the Narrator authors a deterministic briefing + risk for a seeded capture
    (use_llm=False → the raw briefing, no claude spawn);
  - risk maps correctly through trust_policy.evaluate (injected policies pin
    auto_approve ⇒ safe, force_ask ⇒ medium, force_ask+careful / reject /
    auto_approve+careful ⇒ careful — the careful escalator wins over the trust
    action so an auto-firing deploy/credential card still reads as careful);
  - absence: a capture the Narrator hasn't touched carries no meaning-layer
    fields, and needs_briefing is idempotent so a re-sweep doesn't re-author;
  - the single-committer invariant: run() writes captures.json ATOMICALLY to a
    plain (non-git) tmp file and never git-commits.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_missions_narrator
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import missions_narrator as mn  # noqa: E402
from test_isolation_guard import TestIsolationBreach  # noqa: E402

NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)

# Injected trust policies — the deterministic substrate for the risk mapping.
_POLICY_SAFE = {
    'version': 1, 'default_action': 'force_ask',
    'rules': [{'source': '*', 'target': '*', 'action': 'auto_approve'}],
}
_POLICY_ASK = {'version': 1, 'default_action': 'force_ask', 'rules': []}
_POLICY_REJECT = {
    'version': 1, 'default_action': 'force_ask',
    'rules': [{'source': '*', 'target': '*', 'action': 'reject'}],
}


def _capture(**over):
    cap = {
        'id': 'cap-test-1',
        'title': 'Reviewer bot loses comments on rebase',
        'note': 'When a branch is rebased the reviewer sometimes drops comments.',
        'state': 'parked',
        'origin': {'repo': 'ourliberty-agent-core'},
        'last_touched': '2026-06-01T00:00:00+00:00',
    }
    cap.update(over)
    return cap


class RiskMappingTest(unittest.TestCase):
    def test_map_risk_table(self):
        self.assertEqual(mn.map_risk('auto_approve', False), 'safe')
        # careful escalator wins even under auto_approve — an auto-firing
        # deploy/migrate/credential card must still read as careful (board honesty).
        self.assertEqual(mn.map_risk('auto_approve', True), 'careful')
        self.assertEqual(mn.map_risk('force_ask', False), 'medium')
        self.assertEqual(mn.map_risk('force_ask', True), 'careful')
        self.assertEqual(mn.map_risk('reject', False), 'careful')
        self.assertEqual(mn.map_risk('reject', True), 'careful')

    def test_classify_careful_keyword(self):
        self.assertTrue(mn.classify_careful(_capture(title='Delete prod table')))
        self.assertTrue(mn.classify_careful(_capture(note='this would send an email')))
        self.assertFalse(mn.classify_careful(
            _capture(title='Tidy a tooltip', note='cosmetic copy tweak', label=None)))

    def test_derive_risk_through_trust_policy(self):
        # auto_approve ⇒ safe
        risk, careful = mn.derive_risk(_capture(), _POLICY_SAFE)
        self.assertEqual(risk, 'safe')
        # force_ask, reversible ⇒ medium
        risk, careful = mn.derive_risk(_capture(), _POLICY_ASK)
        self.assertEqual(risk, 'medium')
        self.assertFalse(careful)
        # force_ask + careful keyword ⇒ careful
        risk, careful = mn.derive_risk(
            _capture(title='Delete the production database'), _POLICY_ASK)
        self.assertEqual(risk, 'careful')
        self.assertTrue(careful)
        # reject-class ⇒ careful
        risk, _ = mn.derive_risk(_capture(), _POLICY_REJECT)
        self.assertEqual(risk, 'careful')


class AuthorMeaningLayerTest(unittest.TestCase):
    def test_authors_deterministic_briefing_and_fields(self):
        fields = mn.author_meaning_layer(
            _capture(), now=NOW, policy=_POLICY_ASK, use_llm=False)
        # briefing is the 3-part contract, all non-empty strings
        self.assertEqual(set(fields['briefing']), {'what', 'why', 'suggest'})
        for v in fields['briefing'].values():
            self.assertIsInstance(v, str)
            self.assertTrue(v)
        self.assertEqual(fields['risk'], 'medium')
        self.assertIn('risk_note', fields)
        self.assertIn(fields['recommended_action'],
                      ('delegate', 'promote', 'drop', 'snooze'))
        prov = fields['briefing_provenance']
        self.assertEqual(prov['by'], 'beacon')
        self.assertEqual(prov['from_state'], 'parked')
        self.assertEqual(prov['at'], NOW.isoformat())

    def test_deterministic_repeatable(self):
        a = mn.author_meaning_layer(_capture(), now=NOW, policy=_POLICY_ASK, use_llm=False)
        b = mn.author_meaning_layer(_capture(), now=NOW, policy=_POLICY_ASK, use_llm=False)
        self.assertEqual(a, b)

    def test_safe_risk_has_no_risk_note(self):
        fields = mn.author_meaning_layer(
            _capture(), now=NOW, policy=_POLICY_SAFE, use_llm=False)
        self.assertEqual(fields['risk'], 'safe')
        self.assertNotIn('risk_note', fields)

    def test_aging_capture_recommends_drop(self):
        fields = mn.author_meaning_layer(
            _capture(aging=True), now=NOW, policy=_POLICY_ASK, use_llm=False)
        self.assertEqual(fields['recommended_action'], 'drop')

    def test_llm_path_is_guarded_under_test(self):
        # generate_briefing_voice must hit the claude-spawn guard under test —
        # proving a test can never silently spawn the real CLI.
        with self.assertRaises(TestIsolationBreach):
            mn.generate_briefing_voice('prompt')


class NeedsBriefingTest(unittest.TestCase):
    def test_unbriefed_parked_needs_briefing(self):
        self.assertTrue(mn.needs_briefing(_capture()))

    def test_promote_restamps_stale_briefing(self):
        # A capture briefed while parked, then promoted: its provenance still
        # reads from_state='parked' but state is now 'promoted' ⇒ re-author so
        # the meaning layer matches the new state (§ 4). This is the behavior the
        # parked-only gate used to suppress.
        cap = _capture(
            state='promoted',
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'parked'})
        self.assertTrue(mn.needs_briefing(cap))

    def test_drop_restamps_stale_briefing(self):
        cap = _capture(
            state='dropped',
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'parked'})
        self.assertTrue(mn.needs_briefing(cap))

    def test_briefed_for_current_state_is_idempotent(self):
        cap = _capture(
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'parked'})
        self.assertFalse(mn.needs_briefing(cap))

    def test_promoted_and_briefed_for_current_state_is_idempotent(self):
        # Once re-authored after a promote, provenance reads from_state=
        # 'promoted'; a re-sweep is a no-op (idempotent on unchanged cards).
        cap = _capture(
            state='promoted',
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'promoted'})
        self.assertFalse(mn.needs_briefing(cap))

    def test_snooze_keeps_state_parked_so_no_rebrief(self):
        # Snooze only sets snoozed_until; state stays 'parked', so a card already
        # briefed for 'parked' is NOT re-authored by a snooze (its meaning is
        # unchanged — only resurfacing is deferred).
        cap = _capture(
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'parked'},
            snoozed_until='2026-07-01T00:00:00+00:00')
        self.assertFalse(mn.needs_briefing(cap))

    def test_state_change_invalidates_briefing(self):
        # provenance stamped from a different state ⇒ re-author (§ 4).
        cap = _capture(
            state='parked',
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'promoted'})
        self.assertTrue(mn.needs_briefing(cap))

    def test_done_card_is_never_rebriefed(self):
        # Phase S S3: a card the GC healer auto-closed to `done` carries a
        # parked-state briefing; without the skip-states guard the from_state
        # mismatch would re-brief it on every sweep. The guard short-circuits.
        cap = _capture(
            state='done',
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'parked'})
        self.assertFalse(mn.needs_briefing(cap))

    def test_review_close_card_is_never_rebriefed(self):
        # A `review_close` card owns its closeout (author_closeout), not a parked
        # briefing — the parked-card sweep must skip it.
        cap = _capture(
            state='review_close',
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'parked'})
        self.assertFalse(mn.needs_briefing(cap))


class AuthorCloseoutTest(unittest.TestCase):
    """Phase S S3: the Narrator authors a plain-language closeout (what · outcome
    · note) for a merged risky card. use_llm=False forces the deterministic raw
    path — no claude spawn — which is also the head-less fallback."""

    def test_raw_closeout_shape_and_provenance(self):
        cap = _capture(state='review_close')
        fields = mn.author_closeout(
            cap, 'https://github.com/o/r/pull/542', NOW, events=[], use_llm=False)
        self.assertEqual(set(fields['closeout']), {'what', 'outcome', 'note'})
        # PR number is the only identifier surfaced (operator-clickable).
        self.assertIn('542', fields['closeout']['outcome'])
        prov = fields['closeout_provenance']
        self.assertEqual(prov['by'], 'beacon')
        self.assertEqual(prov['model'], 'raw')  # no LLM under use_llm=False
        self.assertEqual(prov['pr_url'], 'https://github.com/o/r/pull/542')
        self.assertEqual(prov['from_state'], 'review_close')

    def test_closeout_degrades_without_pr_url(self):
        # A missing/unparseable pr_url still yields an operator-readable closeout.
        fields = mn.author_closeout(_capture(), None, NOW, events=[], use_llm=False)
        self.assertEqual(set(fields['closeout']), {'what', 'outcome', 'note'})
        self.assertNotIn('PR #', fields['closeout']['outcome'])

    def test_closeout_does_not_mutate_capture(self):
        # Pure: author_closeout returns fields, never writes onto the capture
        # (the GC healer owns the single write — single-committer invariant).
        cap = _capture(state='review_close')
        before = dict(cap)
        mn.author_closeout(cap, None, NOW, events=[], use_llm=False)
        self.assertEqual(cap, before)

    def test_closeout_under_test_refuses_claude_spawn(self):
        # use_llm=True must hit the claude-spawn guard under test (no real spawn).
        with self.assertRaises(TestIsolationBreach):
            mn.author_closeout(_capture(), 'https://github.com/o/r/pull/1', NOW,
                               events=[], use_llm=True)


class RunSweepTest(unittest.TestCase):
    """run() writes the delta to disk atomically and NEVER git-commits — the
    single-committer invariant. A plain tmp file (no git repo) proves it: a
    sweep that tried to commit would fail here."""

    def _write_registry(self, captures):
        d = tempfile.mkdtemp()
        path = Path(d) / 'captures.json'
        path.write_text(json.dumps({'schema_version': 1, 'captures': captures}))
        return path

    def test_briefs_stale_and_writes_disk(self):
        path = self._write_registry([
            _capture(id='needs-it'),
            _capture(id='already', briefing={'what': 'a', 'why': 'b', 'suggest': 'c'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'parked'}),
            # promoted AND already briefed for its current state ⇒ a valid skip.
            _capture(id='promoted-current', state='promoted',
                     briefing={'what': 'p', 'why': 'q', 'suggest': 'r'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'promoted'}),
        ])
        n = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=path)
        self.assertEqual(n, 1)
        reg = json.loads(path.read_text())
        by_id = {c['id']: c for c in reg['captures']}
        # the unbriefed parked capture got a full, valid meaning layer
        self.assertEqual(set(by_id['needs-it']['briefing']),
                         {'what', 'why', 'suggest'})
        self.assertEqual(by_id['needs-it']['risk'], 'medium')
        # captures already briefed for their CURRENT state are untouched
        self.assertEqual(by_id['already']['briefing'],
                         {'what': 'a', 'why': 'b', 'suggest': 'c'})
        self.assertEqual(by_id['promoted-current']['briefing'],
                         {'what': 'p', 'why': 'q', 'suggest': 'r'})

    def test_rebriefs_capture_after_state_change(self):
        # A capture briefed while parked, then promoted by an endpoint, is stale
        # (state != from_state); the sweep re-authors it and restamps from_state.
        path = self._write_registry([
            _capture(id='promoted-stale', state='promoted',
                     briefing={'what': 'old', 'why': 'old', 'suggest': 'old'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'parked'}),
        ])
        n = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=path)
        self.assertEqual(n, 1)
        cap = json.loads(path.read_text())['captures'][0]
        self.assertEqual(cap['briefing_provenance']['from_state'], 'promoted')
        # a second sweep is a no-op now that provenance matches the state.
        n2 = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=path)
        self.assertEqual(n2, 0)

    def test_dry_run_does_not_write(self):
        path = self._write_registry([_capture(id='x')])
        before = path.read_text()
        n = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK,
                   captures_file=path, dry_run=True)
        self.assertEqual(n, 1)
        self.assertEqual(path.read_text(), before)

    def test_idempotent_second_sweep_is_noop(self):
        path = self._write_registry([_capture(id='x')])
        mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=path)
        n2 = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=path)
        self.assertEqual(n2, 0)

    def test_missing_file_is_failsafe(self):
        missing = Path(tempfile.mkdtemp()) / 'nope.json'
        # read_captures_registry treats a missing file as a fresh empty registry
        n = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=missing)
        self.assertEqual(n, 0)

    def test_concurrent_write_during_sweep_is_not_reverted(self):
        # A writer (snooze/ingest endpoint, GC healer) that lands a change AFTER
        # the narrator's initial read but BEFORE its write must NOT be reverted.
        # The narrator re-reads fresh and applies only its field deltas, so the
        # author sweep's stale snapshot never clobbers concurrent state.
        path = self._write_registry([
            _capture(id='briefme'),
            _capture(id='snoozed',
                     briefing={'what': 'a', 'why': 'b', 'suggest': 'c'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'parked'}),
        ])
        real_author = mn.author_meaning_layer

        def author_then_concurrent_write(cap, *a, **k):
            fields = real_author(cap, *a, **k)
            # Simulate another process writing captures.json mid-sweep: snooze an
            # existing capture and ingest a brand-new one.
            reg = json.loads(path.read_text())
            for c in reg['captures']:
                if c['id'] == 'snoozed':
                    c['snoozed_until'] = '2026-07-01T00:00:00+00:00'
            reg['captures'].append(_capture(id='late-ingest', state='inbox'))
            path.write_text(json.dumps(reg))
            return fields

        mn.author_meaning_layer = author_then_concurrent_write
        try:
            n = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=path)
        finally:
            mn.author_meaning_layer = real_author

        self.assertEqual(n, 1)
        reg = json.loads(path.read_text())
        by_id = {c['id']: c for c in reg['captures']}
        # the narrator's briefing landed on the capture it authored
        self.assertEqual(by_id['briefme']['risk'], 'medium')
        # the concurrent snooze was preserved, not reverted to the stale snapshot
        self.assertEqual(by_id['snoozed']['snoozed_until'],
                         '2026-07-01T00:00:00+00:00')
        # the capture ingested mid-sweep was not dropped from disk
        self.assertIn('late-ingest', by_id)

    def test_stale_risk_note_cleared_when_downgraded_to_safe(self):
        # a capture re-briefed from medium→safe must not keep a dangling note.
        path = self._write_registry([
            _capture(id='x', risk='medium', risk_note='old note'),
        ])
        mn.run(now=NOW, use_llm=False, policy=_POLICY_SAFE, captures_file=path)
        reg = json.loads(path.read_text())
        cap = reg['captures'][0]
        self.assertEqual(cap['risk'], 'safe')
        self.assertNotIn('risk_note', cap)


class ParseBriefingJsonTest(unittest.TestCase):
    """Contract C (spec § 5): tolerate fenced / prose-wrapped / trailing-comma
    model replies; only fall through (None) when no JSON object is extractable."""

    _GOOD = {'what': 'w', 'why': 'y', 'suggest': 's'}

    def test_raw_json_object(self):
        self.assertEqual(mn.parse_briefing_json(json.dumps(self._GOOD)), self._GOOD)

    def test_fenced_json_block(self):
        text = '```json\n' + json.dumps(self._GOOD) + '\n```'
        self.assertEqual(mn.parse_briefing_json(text), self._GOOD)

    def test_bare_fence_no_lang(self):
        text = '```\n' + json.dumps(self._GOOD) + '\n```'
        self.assertEqual(mn.parse_briefing_json(text), self._GOOD)

    def test_prose_wrapped_object(self):
        text = ('Sure! Here is the briefing you asked for:\n'
                + json.dumps(self._GOOD)
                + '\nLet me know if you want changes.')
        self.assertEqual(mn.parse_briefing_json(text), self._GOOD)

    def test_trailing_comma_repaired(self):
        text = '{"what": "w", "why": "y", "suggest": "s",}'
        self.assertEqual(mn.parse_briefing_json(text), self._GOOD)

    def test_fenced_with_trailing_comma_and_prose(self):
        text = ('Here you go:\n```json\n'
                '{\n  "what": "w",\n  "why": "y",\n  "suggest": "s",\n}\n```\n'
                'Hope that helps.')
        self.assertEqual(mn.parse_briefing_json(text), self._GOOD)

    def test_braces_inside_string_values_not_miscounted(self):
        good = {'what': 'use {curly} braces', 'why': 'y', 'suggest': 's'}
        text = 'Prefix prose ' + json.dumps(good) + ' suffix'
        self.assertEqual(mn.parse_briefing_json(text), good)

    def test_truly_unparseable_returns_none(self):
        self.assertIsNone(mn.parse_briefing_json('no json here at all'))
        self.assertIsNone(mn.parse_briefing_json(''))
        self.assertIsNone(mn.parse_briefing_json('   '))

    def test_non_object_json_returns_none(self):
        # A bare array / scalar is valid JSON but not a briefing dict.
        self.assertIsNone(mn.parse_briefing_json('[1, 2, 3]'))
        self.assertIsNone(mn.parse_briefing_json('"just a string"'))


class AuthorCapturesInRegistryTest(unittest.TestCase):
    """Contract A (spec § 3): the folded sweep authors needs_briefing captures
    in place, bounded per tick, fail-safe per capture, never writing/committing
    (it only mutates the dict — the GC healer owns the single write)."""

    def _reg(self, *caps):
        return {'schema_version': 1, 'captures': list(caps)}

    def test_briefs_pending_in_place_and_counts(self):
        reg = self._reg(
            _capture(id='a'),
            _capture(id='b', briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'parked'}),
            # promoted AND already briefed for its current state ⇒ a valid skip.
            _capture(id='c', state='promoted',
                     briefing={'what': 'p', 'why': 'q', 'suggest': 'r'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'promoted'}),
        )
        briefed, deferred = mn.author_captures_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        self.assertEqual((briefed, deferred), (1, 0))
        by_id = {c['id']: c for c in reg['captures']}
        self.assertEqual(by_id['a']['risk'], 'medium')
        self.assertEqual(set(by_id['a']['briefing']), {'what', 'why', 'suggest'})
        # captures already briefed for their current state are untouched
        self.assertEqual(by_id['b']['briefing'],
                         {'what': 'x', 'why': 'y', 'suggest': 'z'})
        self.assertEqual(by_id['c']['briefing'],
                         {'what': 'p', 'why': 'q', 'suggest': 'r'})

    def test_respects_max_per_tick_and_defers_rest(self):
        reg = self._reg(*[_capture(id=f'c{i}') for i in range(5)])
        briefed, deferred = mn.author_captures_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_ASK, max_per_tick=2)
        self.assertEqual((briefed, deferred), (2, 3))
        briefed_now = [c for c in reg['captures'] if isinstance(c.get('briefing'), dict)]
        self.assertEqual(len(briefed_now), 2)

    def test_per_capture_error_skipped_not_fatal(self):
        reg = self._reg(_capture(id='a'), _capture(id='boom'), _capture(id='c'))
        real_author = mn.author_meaning_layer

        def flaky(cap, *a, **k):
            if cap.get('id') == 'boom':
                raise RuntimeError('author exploded')
            return real_author(cap, *a, **k)

        mn.author_meaning_layer = flaky
        try:
            briefed, deferred = mn.author_captures_in_registry(
                reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        finally:
            mn.author_meaning_layer = real_author
        # two briefed, the exploding one skipped (still pending → deferred).
        self.assertEqual(briefed, 2)
        self.assertEqual(deferred, 1)
        by_id = {c['id']: c for c in reg['captures']}
        self.assertEqual(by_id['a']['risk'], 'medium')
        self.assertNotIn('briefing', by_id['boom'])
        self.assertEqual(by_id['c']['risk'], 'medium')

    def test_idempotent_second_sweep_is_noop(self):
        reg = self._reg(_capture(id='a'))
        mn.author_captures_in_registry(reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        briefed, deferred = mn.author_captures_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        self.assertEqual((briefed, deferred), (0, 0))

    def test_empty_or_malformed_registry_is_failsafe(self):
        self.assertEqual(mn.author_captures_in_registry({}, now=NOW, use_llm=False), (0, 0))
        self.assertEqual(
            mn.author_captures_in_registry({'captures': 'nope'}, now=NOW, use_llm=False),
            (0, 0))

    def test_stale_risk_note_cleared_on_rebrief_to_safe(self):
        reg = self._reg(_capture(id='a', risk='medium', risk_note='old note'))
        mn.author_captures_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_SAFE)
        cap = reg['captures'][0]
        self.assertEqual(cap['risk'], 'safe')
        self.assertNotIn('risk_note', cap)


# ----------------- projects-v3 P2 Contract A: funnel-mission layer -----------


def _mission(**over):
    m = {
        'id': 'mission-test-1',
        'name': 'Reviewer bot loses comments on rebase',
        'brief': 'When a branch is rebased the reviewer drops comments.',
        'phase': 'proposed',
        'repo': 'ourliberty-agent-core',
        'proposed_by': 'heal_orphan_autoregister',
        'task_ids': ['orphan-task-1'],
    }
    m.update(over)
    return m


class MissionSuggestedSourceTest(unittest.TestCase):
    def test_canonical_agents_map_through(self):
        self.assertEqual(mn.mission_suggested_source(_mission(proposed_by='beacon')), 'beacon')
        self.assertEqual(mn.mission_suggested_source(_mission(proposed_by='medic-healer')), 'medic')
        self.assertEqual(mn.mission_suggested_source(_mission(proposed_by='pulse_cycle')), 'pulse')

    def test_orphan_autoregister_is_not_suggested(self):
        self.assertIsNone(mn.mission_suggested_source(_mission()))
        self.assertIsNone(mn.mission_suggested_source(_mission(proposed_by=None)))
        self.assertIsNone(mn.mission_suggested_source(_mission(proposed_by='forge')))


class MissionIsDeadTest(unittest.TestCase):
    def test_live_proposed_is_not_dead(self):
        self.assertFalse(mn.mission_is_dead(_mission()))

    def test_acknowledged_retired_or_archived_is_dead(self):
        self.assertTrue(mn.mission_is_dead(_mission(acknowledged=True)))
        self.assertTrue(mn.mission_is_dead(_mission(retired_at='2026-06-14T00:00:00+00:00')))
        for phase in ('retired', 'archived', 'closed'):
            self.assertTrue(mn.mission_is_dead(_mission(phase=phase)))


class MissionRiskMappingTest(unittest.TestCase):
    def test_auto_approve_is_safe(self):
        risk, careful = mn.derive_mission_risk(_mission(), _POLICY_SAFE)
        self.assertEqual(risk, 'safe')
        self.assertFalse(careful)

    def test_force_ask_is_medium(self):
        risk, _ = mn.derive_mission_risk(_mission(), _POLICY_ASK)
        self.assertEqual(risk, 'medium')

    def test_careful_keyword_escalates_to_careful(self):
        risk, careful = mn.derive_mission_risk(
            _mission(name='Delete the production database'), _POLICY_ASK)
        self.assertEqual(risk, 'careful')
        self.assertTrue(careful)

    def test_reject_is_careful(self):
        risk, _ = mn.derive_mission_risk(_mission(), _POLICY_REJECT)
        self.assertEqual(risk, 'careful')


class RenderRawMissionBriefingTest(unittest.TestCase):
    def test_three_part_contract_non_empty(self):
        b = mn.render_raw_mission_briefing(_mission(), 'medium', False, suggested=False)
        self.assertEqual(set(b), {'what', 'why', 'suggest'})
        for v in b.values():
            self.assertIsInstance(v, str)
            self.assertTrue(v)

    def test_never_leaks_machine_fields(self):
        # The card must never surface the machine brief, id, or task_ids — only
        # the human-facing name (the no-raw-metadata-leak guard, § 5).
        m = _mission(brief='SECRET-MACHINE-BRIEF', id='mission-xyz-id',
                     task_ids=['orphan-task-leak'])
        for suggested in (True, False):
            b = mn.render_raw_mission_briefing(m, 'medium', False, suggested=suggested)
            blob = ' '.join(b.values())
            self.assertNotIn('SECRET-MACHINE-BRIEF', blob)
            self.assertNotIn('mission-xyz-id', blob)
            self.assertNotIn('orphan-task-leak', blob)
            self.assertIn('Reviewer bot loses comments on rebase', b['what'])

    def test_suggested_vs_orphan_why_differs(self):
        s = mn.render_raw_mission_briefing(_mission(), 'medium', False, suggested=True)
        o = mn.render_raw_mission_briefing(_mission(), 'medium', False, suggested=False)
        self.assertNotEqual(s['why'], o['why'])


class BuildMissionBriefingPromptTest(unittest.TestCase):
    def test_prompt_excludes_machine_metadata(self):
        m = _mission(brief='SECRET-MACHINE-BRIEF', id='mission-xyz-id',
                     task_ids=['orphan-task-leak'])
        prompt = mn.build_mission_briefing_prompt(m, [], 'medium', suggested=False)
        self.assertNotIn('SECRET-MACHINE-BRIEF', prompt)
        self.assertNotIn('mission-xyz-id', prompt)
        self.assertNotIn('orphan-task-leak', prompt)
        self.assertIn('Reviewer bot loses comments on rebase', prompt)


class AuthorMissionMeaningLayerTest(unittest.TestCase):
    def test_authors_deterministic_fields(self):
        fields = mn.author_mission_meaning_layer(
            _mission(), now=NOW, policy=_POLICY_ASK, use_llm=False)
        self.assertEqual(set(fields['briefing']), {'what', 'why', 'suggest'})
        self.assertEqual(fields['risk'], 'medium')
        self.assertIn('risk_note', fields)
        self.assertIn(fields['recommended_action'],
                      ('delegate', 'promote', 'drop', 'snooze'))
        prov = fields['briefing_provenance']
        self.assertEqual(prov['by'], 'beacon')
        self.assertEqual(prov['model'], 'raw')
        self.assertEqual(prov['from_state'], 'proposed')
        self.assertEqual(prov['at'], NOW.isoformat())

    def test_safe_risk_has_no_risk_note(self):
        fields = mn.author_mission_meaning_layer(
            _mission(), now=NOW, policy=_POLICY_SAFE, use_llm=False)
        self.assertEqual(fields['risk'], 'safe')
        self.assertNotIn('risk_note', fields)

    def test_does_not_mutate_mission(self):
        m = _mission()
        before = dict(m)
        mn.author_mission_meaning_layer(m, now=NOW, policy=_POLICY_ASK, use_llm=False)
        self.assertEqual(m, before)

    def test_llm_path_is_guarded_under_test(self):
        with self.assertRaises(TestIsolationBreach):
            mn.author_mission_meaning_layer(
                _mission(), now=NOW, policy=_POLICY_ASK, use_llm=True)


class MissionNeedsBriefingTest(unittest.TestCase):
    def test_unbriefed_proposed_orphan_needs_briefing(self):
        self.assertTrue(mn.mission_needs_briefing(_mission()))

    def test_unbriefed_proposed_suggested_needs_briefing(self):
        self.assertTrue(mn.mission_needs_briefing(_mission(proposed_by='beacon')))

    def test_non_proposed_is_skipped(self):
        for phase in ('drafting', 'ready', 'in_progress'):
            self.assertFalse(mn.mission_needs_briefing(_mission(phase=phase)))

    def test_dead_mission_is_skipped(self):
        self.assertFalse(mn.mission_needs_briefing(_mission(acknowledged=True)))
        self.assertFalse(mn.mission_needs_briefing(_mission(phase='retired')))

    def test_briefed_for_current_phase_is_idempotent(self):
        m = _mission(
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'proposed'})
        self.assertFalse(mn.mission_needs_briefing(m))

    def test_stale_from_state_restamps(self):
        m = _mission(
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'drafting'})
        self.assertTrue(mn.mission_needs_briefing(m))

    def test_non_dict_is_failsafe(self):
        self.assertFalse(mn.mission_needs_briefing('nope'))


class AuthorMissionsInRegistryTest(unittest.TestCase):
    """Contract A (spec § 3): the GC-tick sweep authors needs_briefing funnel
    missions (orphan + suggested) in place, bounded per tick, fail-safe per
    mission, never writing/committing (the GC healer owns the single write)."""

    def _reg(self, *missions):
        return {'schema_version': 1, 'missions': list(missions)}

    def test_briefs_orphan_and_suggested_in_place_and_counts(self):
        reg = self._reg(
            _mission(id='orphan', proposed_by='heal_orphan_autoregister'),
            _mission(id='suggested', proposed_by='beacon'),
            # already briefed for proposed ⇒ a valid skip.
            _mission(id='done', briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'proposed'}),
            # non-proposed ⇒ skipped.
            _mission(id='accepted', phase='drafting'),
        )
        briefed, deferred = mn.author_missions_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        self.assertEqual((briefed, deferred), (2, 0))
        by_id = {m['id']: m for m in reg['missions']}
        self.assertEqual(by_id['orphan']['risk'], 'medium')
        self.assertEqual(set(by_id['orphan']['briefing']), {'what', 'why', 'suggest'})
        self.assertEqual(by_id['suggested']['risk'], 'medium')
        self.assertEqual(by_id['done']['briefing'],
                         {'what': 'x', 'why': 'y', 'suggest': 'z'})
        self.assertNotIn('briefing', by_id['accepted'])

    def test_respects_max_per_tick_and_defers_rest(self):
        reg = self._reg(*[_mission(id=f'm{i}') for i in range(5)])
        briefed, deferred = mn.author_missions_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_ASK, max_per_tick=2)
        self.assertEqual((briefed, deferred), (2, 3))

    def test_per_mission_error_skipped_not_fatal(self):
        reg = self._reg(_mission(id='a'), _mission(id='boom'), _mission(id='c'))
        real_author = mn.author_mission_meaning_layer

        def flaky(m, *a, **k):
            if m.get('id') == 'boom':
                raise RuntimeError('author exploded')
            return real_author(m, *a, **k)

        mn.author_mission_meaning_layer = flaky
        try:
            briefed, deferred = mn.author_missions_in_registry(
                reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        finally:
            mn.author_mission_meaning_layer = real_author
        self.assertEqual((briefed, deferred), (2, 1))
        by_id = {m['id']: m for m in reg['missions']}
        self.assertEqual(by_id['a']['risk'], 'medium')
        self.assertNotIn('briefing', by_id['boom'])
        self.assertEqual(by_id['c']['risk'], 'medium')

    def test_idempotent_second_sweep_is_noop(self):
        reg = self._reg(_mission(id='a'))
        mn.author_missions_in_registry(reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        briefed, deferred = mn.author_missions_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_ASK)
        self.assertEqual((briefed, deferred), (0, 0))

    def test_empty_or_malformed_registry_is_failsafe(self):
        self.assertEqual(mn.author_missions_in_registry({}, now=NOW, use_llm=False), (0, 0))
        self.assertEqual(
            mn.author_missions_in_registry({'missions': 'nope'}, now=NOW, use_llm=False),
            (0, 0))

    def test_stale_risk_note_cleared_on_rebrief_to_safe(self):
        reg = self._reg(_mission(id='a', risk='medium', risk_note='old note'))
        mn.author_missions_in_registry(
            reg, now=NOW, use_llm=False, policy=_POLICY_SAFE)
        m = reg['missions'][0]
        self.assertEqual(m['risk'], 'safe')
        self.assertNotIn('risk_note', m)


# ---------------- approval-card meaning layer (projects-v3 P7.2a) -------------


def _approval(**over):
    """A pending approval chain_events row (approval_request by default)."""
    ev = {
        'event_id': 'ev-1',
        'event_type': 'approval_request',
        'agent': 'beacon',
        'ts': '2026-06-14T00:00:00+00:00',
        'read_at': None,
        'payload': {'prompt': 'Please approve the spec for the new feature.'},
    }
    ev.update(over)
    return ev


class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Mimics the supabase-py chained query used by the approval sweep:
    .select().in_().order().limit().execute() (read) and
    .update().eq().execute() (write — recorded on the client)."""

    def __init__(self, client):
        self._client = client
        self._payload = None
        self._eq = None

    def select(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        if self._payload is not None:
            self._client.updates.append((self._eq[1], self._payload))
            return _FakeResp(None)
        return _FakeResp([dict(r) for r in self._client.rows])


class _FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []  # list of (event_id, {'payload': ...})

    def table(self, name):
        assert name == 'chain_events'
        return _FakeQuery(self)


class ApprovalRiskTest(unittest.TestCase):
    def test_policy_match_maps_through_trust_policy(self):
        # A matching rule (auto_approve) → safe, regardless of severity.
        risk, careful = mn.derive_approval_risk(
            _approval(payload={'prompt': 'tidy a tooltip', 'severity': 'critical'}),
            _POLICY_SAFE)
        self.assertEqual(risk, 'safe')
        self.assertFalse(careful)

    def test_no_rule_floors_at_default_and_severity_escalates(self):
        # _POLICY_ASK has no rules → the force_ask DEFAULT applies (floor = medium);
        # the event's severity may ESCALATE it (critical → careful) but never drop
        # it below the floor — an unclassified approval is still a real decision.
        crit = mn.derive_approval_risk(
            _approval(payload={'prompt': 'x', 'severity': 'critical'}), _POLICY_ASK)
        self.assertEqual(crit[0], 'careful')
        warn = mn.derive_approval_risk(
            _approval(payload={'prompt': 'x', 'severity': 'warning'}), _POLICY_ASK)
        self.assertEqual(warn[0], 'medium')
        # info / no severity must NOT drop the force_ask default below medium
        # (no under-warning on the queue's most decision-dense surface).
        info = mn.derive_approval_risk(
            _approval(payload={'prompt': 'x', 'severity': 'info'}), _POLICY_ASK)
        self.assertEqual(info[0], 'medium')
        none = mn.derive_approval_risk(
            _approval(payload={'prompt': 'x'}), _POLICY_ASK)
        self.assertEqual(none[0], 'medium')

    def test_careful_keyword_wins(self):
        # An irreversible/outward request reads careful even with no rule + low sev.
        risk, careful = mn.derive_approval_risk(
            _approval(payload={'prompt': 'Deploy to production and delete the table',
                               'severity': 'info'}),
            _POLICY_ASK)
        self.assertEqual(risk, 'careful')
        self.assertTrue(careful)

    def test_careful_escalator_wins_over_matched_auto_approve(self):
        # The careful escalator wins even inside the policy-match branch: an
        # auto_approve rule still reads careful for an irreversible request.
        risk, careful = mn.derive_approval_risk(
            _approval(payload={'prompt': 'delete the production database'}),
            _POLICY_SAFE)
        self.assertEqual(risk, 'careful')
        self.assertTrue(careful)

    def test_reject_rule_is_careful(self):
        risk, _ = mn.derive_approval_risk(_approval(), _POLICY_REJECT)
        self.assertEqual(risk, 'careful')


class ApprovalMeaningLayerTest(unittest.TestCase):
    def test_authors_deterministic_fields(self):
        fields = mn.author_approval_meaning_layer(
            _approval(payload={'prompt': 'x', 'severity': 'warning'}),
            now=NOW, policy=_POLICY_ASK, use_llm=False)
        self.assertEqual(set(fields['briefing']), {'what', 'why', 'suggest'})
        self.assertTrue(all(isinstance(v, str) and v
                            for v in fields['briefing'].values()))
        self.assertEqual(fields['risk'], 'medium')
        self.assertIn('risk_note', fields)  # medium → a note
        prov = fields['briefing_provenance']
        self.assertEqual(prov['by'], 'beacon')
        self.assertEqual(prov['model'], 'raw')  # no LLM under use_llm=False
        self.assertEqual(prov['source'], 'approval')
        # The Approvals queue uses Approve/Reject, not delegate/drop — so the
        # approval meaning layer carries NO recommended_action.
        self.assertNotIn('recommended_action', fields)

    def test_safe_has_no_risk_note(self):
        fields = mn.author_approval_meaning_layer(
            _approval(payload={'prompt': 'x'}), now=NOW, policy=_POLICY_SAFE,
            use_llm=False)
        self.assertEqual(fields['risk'], 'safe')
        self.assertNotIn('risk_note', fields)

    def test_raw_briefing_does_not_echo_the_prompt_blob(self):
        # The fallback is a plain summary, not the raw request (the card already
        # shows the full prompt on expand).
        long_prompt = 'SECRET-MARKER ' * 50
        fields = mn.author_approval_meaning_layer(
            _approval(payload={'prompt': long_prompt}), now=NOW,
            policy=_POLICY_ASK, use_llm=False)
        self.assertNotIn('SECRET-MARKER', json.dumps(fields['briefing']))


class ApprovalNeedsBriefingTest(unittest.TestCase):
    def test_unbriefed_pending_needs_briefing(self):
        self.assertTrue(mn.approval_needs_briefing(_approval()))
        self.assertTrue(mn.approval_needs_briefing(
            _approval(event_type='clarify_request',
                      payload={'question': 'which repo?'})))

    def test_already_briefed_is_idempotent(self):
        ev = _approval(payload={'prompt': 'x', 'briefing': {'what': 'a'}})
        self.assertFalse(mn.approval_needs_briefing(ev))

    def test_resolved_row_is_skipped(self):
        self.assertFalse(mn.approval_needs_briefing(
            _approval(read_at='2026-06-14T00:00:00+00:00')))

    def test_non_briefable_types_skipped(self):
        for t in ('larry_alert', 'sentinel_alert', 'card_message'):
            self.assertFalse(mn.approval_needs_briefing(
                _approval(event_type=t, payload={'message': 'fyi'})))

    def test_escalation_with_summary_skipped_without_briefed(self):
        self.assertFalse(mn.approval_needs_briefing(
            _approval(event_type='escalation',
                      payload={'summary': 'already explained'})))
        self.assertTrue(mn.approval_needs_briefing(
            _approval(event_type='escalation', payload={'headline': 'h'})))


class ApprovalSweepTest(unittest.TestCase):
    def _rows(self):
        return [
            _approval(event_id='a1',
                      payload={'prompt': 'Deploy to production'}),       # careful
            _approval(event_id='a2', event_type='clarify_request',
                      payload={'question': 'Which repo should this land in?'}),
            _approval(event_id='a3',
                      payload={'prompt': 'x', 'briefing': {'what': 'done'}}),  # skip
            _approval(event_id='a4', event_type='larry_alert',
                      payload={'message': 'fyi'}),                        # skip
            _approval(event_id='a5',
                      read_at='2026-06-14T00:00:00+00:00',
                      payload={'prompt': 'y'}),                          # skip
        ]

    def test_briefs_pending_writes_payload_back(self):
        client = _FakeClient(self._rows())
        briefed, deferred = mn.author_pending_approvals(
            client=client, now=NOW, use_llm=False, policy=_POLICY_ASK)
        self.assertEqual(briefed, 2)      # a1 + a2 only
        self.assertEqual(deferred, 0)
        wrote = {eid: payload['payload'] for eid, payload in client.updates}
        self.assertEqual(set(wrote), {'a1', 'a2'})
        # a1: a production/deploy request reads careful + keeps its prompt.
        self.assertEqual(wrote['a1']['risk'], 'careful')
        self.assertEqual(wrote['a1']['prompt'], 'Deploy to production')
        self.assertEqual(set(wrote['a1']['briefing']), {'what', 'why', 'suggest'})
        self.assertEqual(wrote['a1']['briefing_provenance']['source'], 'approval')
        # a2: a plain question with no rule → the force_ask default floors at
        # medium (no under-warning), so it carries a risk_note.
        self.assertEqual(wrote['a2']['risk'], 'medium')
        self.assertIn('risk_note', wrote['a2'])

    def test_per_tick_bound(self):
        client = _FakeClient(self._rows())
        briefed, deferred = mn.author_pending_approvals(
            client=client, now=NOW, use_llm=False, policy=_POLICY_ASK,
            max_per_tick=1)
        self.assertEqual(briefed, 1)
        self.assertEqual(deferred, 1)
        self.assertEqual(len(client.updates), 1)

    def test_empty_pending_is_noop(self):
        client = _FakeClient([])
        self.assertEqual(
            mn.author_pending_approvals(client=client, now=NOW, use_llm=False),
            (0, 0))

    def test_no_supabase_client_is_noop(self):
        # No injected client and no configured supabase → the sweep no-ops with no
        # fetch/update (the real cli-is-None branch).
        with patch.object(mn, '_approval_client', return_value=None):
            self.assertEqual(
                mn.author_pending_approvals(now=NOW, use_llm=False), (0, 0))


class ClaudeEnvDurableTokenTest(unittest.TestCase):
    """The narrator's claude spawn authenticates with the active tier's durable
    setup-token (not HOME's rotting credentials.json) — fail-safe to a no-op."""

    def test_sets_oauth_token_from_active_tier(self):
        with patch.object(mn.active_tier, 'active_setup_token',
                          return_value='sk-durable-xyz'):
            env = mn._claude_env()
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], 'sk-durable-xyz')

    def test_no_token_configured_is_noop(self):
        # No setup-token → env carries no injected token (claude falls back to
        # the default credential exactly as before).
        with patch.dict(mn.os.environ, {}, clear=False), \
                patch.object(mn.active_tier, 'active_setup_token',
                             return_value=None):
            mn.os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
            env = mn._claude_env()
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)

    def test_resolution_error_is_fail_safe(self):
        # active_setup_token raising must not break the spawn — env is unchanged.
        with patch.object(mn.active_tier, 'active_setup_token',
                          side_effect=RuntimeError('boom')):
            mn.os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
            env = mn._claude_env()  # must not raise
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)

    def test_env_is_a_copy_preserving_other_vars(self):
        with patch.dict(mn.os.environ, {'OL_MARKER_XYZ': '1'}, clear=False), \
                patch.object(mn.active_tier, 'active_setup_token',
                             return_value='tok'):
            env = mn._claude_env()
        self.assertEqual(env.get('OL_MARKER_XYZ'), '1')  # inherits process env


if __name__ == '__main__':
    unittest.main()
