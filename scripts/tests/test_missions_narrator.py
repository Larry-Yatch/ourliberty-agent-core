#!/usr/bin/env python3
"""Tests for scripts/missions_narrator.py — the Phase 4 Narrator pass.

Spec: agents/beacon/specs/missions-v2-phase4-meaning-layer.md § 5 / § 11.

The § 11 1a proof obligations covered here:
  - the Narrator authors a deterministic briefing + risk for a seeded capture
    (use_llm=False → the raw briefing, no claude spawn);
  - risk maps correctly through trust_policy.evaluate (injected policies pin
    auto_approve ⇒ safe, force_ask ⇒ medium, force_ask+careful / reject ⇒
    careful);
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
        self.assertEqual(mn.map_risk('auto_approve', True), 'safe')
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

    def test_non_parked_never_needs_briefing(self):
        self.assertFalse(mn.needs_briefing(_capture(state='promoted')))

    def test_briefed_for_current_state_is_idempotent(self):
        cap = _capture(
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'parked'})
        self.assertFalse(mn.needs_briefing(cap))

    def test_state_change_invalidates_briefing(self):
        # provenance stamped from a different state ⇒ re-author (§ 4).
        cap = _capture(
            state='parked',
            briefing={'what': 'x', 'why': 'y', 'suggest': 'z'},
            briefing_provenance={'by': 'beacon', 'from_state': 'promoted'})
        self.assertTrue(mn.needs_briefing(cap))


class RunSweepTest(unittest.TestCase):
    """run() writes the delta to disk atomically and NEVER git-commits — the
    single-committer invariant. A plain tmp file (no git repo) proves it: a
    sweep that tried to commit would fail here."""

    def _write_registry(self, captures):
        d = tempfile.mkdtemp()
        path = Path(d) / 'captures.json'
        path.write_text(json.dumps({'schema_version': 1, 'captures': captures}))
        return path

    def test_briefs_only_unbriefed_parked_and_writes_disk(self):
        path = self._write_registry([
            _capture(id='needs-it'),
            _capture(id='already', briefing={'what': 'a', 'why': 'b', 'suggest': 'c'},
                     briefing_provenance={'by': 'beacon', 'from_state': 'parked'}),
            _capture(id='not-parked', state='promoted'),
        ])
        n = mn.run(now=NOW, use_llm=False, policy=_POLICY_ASK, captures_file=path)
        self.assertEqual(n, 1)
        reg = json.loads(path.read_text())
        by_id = {c['id']: c for c in reg['captures']}
        # the unbriefed parked capture got a full, valid meaning layer
        self.assertEqual(set(by_id['needs-it']['briefing']),
                         {'what', 'why', 'suggest'})
        self.assertEqual(by_id['needs-it']['risk'], 'medium')
        # the already-briefed and non-parked captures are untouched
        self.assertEqual(by_id['already']['briefing'],
                         {'what': 'a', 'why': 'b', 'suggest': 'c'})
        self.assertNotIn('briefing', by_id['not-parked'])

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


if __name__ == '__main__':
    unittest.main()
