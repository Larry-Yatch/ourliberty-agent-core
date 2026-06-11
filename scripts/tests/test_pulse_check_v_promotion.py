#!/usr/bin/env python3
"""Tests for the Phase C promotion loop in pulse_check_v.

Covers (each maps to a docs/pulse-triage-phase-c-brief.md **Enforcement:** line):
  - streak computation from the executions store (clean = success+uncorrected;
    a failure OR a Larry-correction breaks the streak from the executions
    source, never from the vestigial payload.larry_modified ledger field)
  - 3-clean reversible probation pattern -> graduation candidate
  - plain-language render: all four sections + no enum/jargon leak + raw fallback
    when a plain_language field is absent
  - apply_graduation flips probation->graduated + stamps graduated_at; refuses a
    permanent_guard floor pattern
  - permanent_guard NEVER graduates at clean_streak>=3
  - graduated + subsequent failure/correction auto-demotes to probation with
    clean_streak=0 and emits no approval
  - reconcile_streaks writes the derived streak back to the registry cache
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_v as p5  # noqa: E402


NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _ex(*, outcome='success', correction=False, days_ago=0.0):
    return {
        'outcome': outcome,
        'larry_correction_signal': correction,
        'ts': (NOW - timedelta(days=days_ago)).isoformat(),
    }


def _pattern(template, *, state='probation', reversible=True,
             permanent_guard=False, clean_streak=0, plain_language=None):
    return {
        'template': template,
        'state': state,
        'reversible': reversible,
        'permanent_guard': permanent_guard,
        'clean_streak': clean_streak,
        'total_dispatches': 0,
        'last_larry_correction_at': None,
        'graduated_at': None,
        'plain_language': plain_language if plain_language is not None else {
            'what': f'the {template} signal fires',
            'action': f'run the {template} fix automatically',
            'why_safe': 'the fix can be undone if it misbehaves',
        },
    }


def _registry(*patterns):
    return {'_schema': {'version': 1}, 'patterns': list(patterns)}


class TestStreak(unittest.TestCase):

    def test_empty_is_zero(self):
        self.assertEqual(p5.consecutive_clean_streak([]), 0)
        self.assertEqual(p5.consecutive_clean_streak(None), 0)

    def test_all_clean(self):
        execs = [_ex(days_ago=i) for i in range(5, 0, -1)]
        self.assertEqual(p5.consecutive_clean_streak(execs), 5)

    def test_failure_breaks_streak_from_tail(self):
        execs = [_ex(days_ago=4), _ex(outcome='failure', days_ago=3),
                 _ex(days_ago=2), _ex(days_ago=1)]
        # only the trailing two clean executions count
        self.assertEqual(p5.consecutive_clean_streak(execs), 2)

    def test_larry_correction_breaks_streak(self):
        """A Larry-correction breaks the streak — and it is read from the
        executions source (larry_correction_signal), NOT payload.larry_modified."""
        execs = [_ex(days_ago=3), _ex(correction=True, days_ago=2),
                 _ex(days_ago=1)]
        self.assertEqual(p5.consecutive_clean_streak(execs), 1)

    def test_trailing_failure_is_zero(self):
        execs = [_ex(days_ago=2), _ex(outcome='failure', days_ago=1)]
        self.assertEqual(p5.consecutive_clean_streak(execs), 0)


class TestCandidates(unittest.TestCase):

    def test_three_clean_reversible_probation_graduates(self):
        doc = _registry(_pattern('restart-daemon'))
        store = {'restart-daemon': [_ex(days_ago=i) for i in range(3, 0, -1)]}
        cands = p5.find_graduation_candidates(doc, store)
        self.assertEqual([c['template'] for c in cands], ['restart-daemon'])
        self.assertEqual(cands[0]['clean_streak'], 3)

    def test_below_threshold_no_candidate(self):
        doc = _registry(_pattern('restart-daemon'))
        store = {'restart-daemon': [_ex(days_ago=i) for i in range(2, 0, -1)]}
        self.assertEqual(p5.find_graduation_candidates(doc, store), [])

    def test_permanent_guard_never_graduates(self):
        """permanent_guard floor: even at clean_streak>=3 (and reversible-or-not)
        a permanent_guard pattern produces no graduation proposal."""
        doc = _registry(_pattern('rotate-credential', reversible=False,
                                 permanent_guard=True))
        store = {'rotate-credential': [_ex(days_ago=i) for i in range(5, 0, -1)]}
        self.assertEqual(p5.find_graduation_candidates(doc, store), [])

    def test_irreversible_never_graduates(self):
        doc = _registry(_pattern('something', reversible=False,
                                 permanent_guard=False))
        store = {'something': [_ex(days_ago=i) for i in range(5, 0, -1)]}
        self.assertEqual(p5.find_graduation_candidates(doc, store), [])

    def test_already_graduated_not_a_candidate(self):
        doc = _registry(_pattern('restart-daemon', state='graduated'))
        store = {'restart-daemon': [_ex(days_ago=i) for i in range(5, 0, -1)]}
        self.assertEqual(p5.find_graduation_candidates(doc, store), [])

    def test_uncategorized_sentinel_never_graduates(self):
        doc = _registry(_pattern('uncategorized', reversible=True,
                                 permanent_guard=False))
        store = {'uncategorized': [_ex(days_ago=i) for i in range(5, 0, -1)]}
        self.assertEqual(p5.find_graduation_candidates(doc, store), [])


class TestRender(unittest.TestCase):

    # config-enum / jargon tokens that must NEVER leak into a Larry-facing render
    FORBIDDEN = ('probation', 'graduated', 'permanent_guard', 'clean_streak',
                 'NEVER_GRADUATE', 'larry_correction_signal')

    def test_all_four_sections_render(self):
        rec = _pattern('restart-daemon')
        execs = [_ex(days_ago=11), _ex(days_ago=6), _ex(days_ago=0)]
        text = p5.render_graduation_approval(rec, 3, execs)
        self.assertIn('WHAT IT IS:', text)
        self.assertIn('WHAT PULSE WOULD NOW DO AUTOMATICALLY:', text)
        self.assertIn("WHY IT'S SAFE:", text)
        self.assertIn('TRACK RECORD:', text)
        self.assertIn('3/3 clean', text)
        self.assertIn('over 11 days', text)
        self.assertIn('approve graduation restart-daemon', text)

    def test_no_enum_jargon_leak(self):
        rec = _pattern('restart-daemon')
        text = p5.render_graduation_approval(rec, 3, [_ex()]).lower()
        for tok in self.FORBIDDEN:
            self.assertNotIn(tok.lower(), text,
                             f'enum/jargon token leaked: {tok!r}')

    def test_raw_fallback_when_field_absent(self):
        """A missing plain_language field falls through to a safe raw form
        rather than crashing or leaking an enum."""
        rec = _pattern('restart-daemon', plain_language={})  # all fields absent
        text = p5.render_graduation_approval(rec, 3, [_ex()])
        # does not crash, still renders the four sections, uses the template-
        # name fallback for WHAT IT IS
        self.assertIn('WHAT IT IS:', text)
        self.assertIn('restart-daemon', text)
        for tok in self.FORBIDDEN:
            self.assertNotIn(tok.lower(), text.lower())


class TestEmitProposals(unittest.TestCase):
    """The dispatch envelope a graduation proposal carries — the path
    `approve graduation <template>` -> dispatch_approved -> Forge's inbox.
    Forge is worktree_enabled, so the envelope MUST carry target_repo or
    inbox_watcher refuses the task and no config PR ever opens."""

    def setUp(self):
        import beacon_approval_handler as approval
        import larry_alerts as la
        self.approval = approval
        self.la = la
        self._orig_add = approval.add_pending
        self._orig_find = approval.find_graduation_pending
        self._orig_append = la.append_approval_request
        self.added = []
        self.pending_templates = set()

        def _fake_add(payload, chat_id=0):
            self.added.append(payload)
            self.pending_templates.add(payload.get('template'))
            return {'id': payload['task_id'], 'dispatch_payload': payload}

        def _fake_find(template, state=None):
            return ({'id': f'graduation-{template}'}
                    if template in self.pending_templates else None)

        approval.add_pending = _fake_add
        approval.find_graduation_pending = _fake_find
        la.append_approval_request = lambda *a, **k: None

    def tearDown(self):
        self.approval.add_pending = self._orig_add
        self.approval.find_graduation_pending = self._orig_find
        self.la.append_approval_request = self._orig_append

    def _cand(self, template='restart-daemon'):
        return {'template': template, 'clean_streak': 3,
                'record': _pattern(template), 'executions': [_ex()]}

    def test_envelope_carries_target_repo_and_config_pr_fields(self):
        created = p5.emit_graduation_proposals([self._cand()], chat_id=42)
        self.assertEqual(len(created), 1)
        self.assertEqual(len(self.added), 1)
        payload = self.added[0]
        # The fix: without target_repo, forge's worktree dispatch refuses the
        # task and the config PR never opens.
        self.assertEqual(payload['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(payload['target_agent'], 'forge')
        self.assertEqual(payload['task_type'], 'doc-only')
        self.assertIn('restart-daemon', payload['pr_title'])
        self.assertEqual(payload['template'], 'restart-daemon')
        self.assertEqual(payload['task_id'], 'graduation-restart-daemon')

    def test_dedup_skips_when_already_pending(self):
        # First emit registers a pending entry; the second monthly run must not
        # re-emit (add_pending has no id-dedup, and find_graduation_pending now
        # returns the existing entry).
        p5.emit_graduation_proposals([self._cand()], chat_id=42)
        again = p5.emit_graduation_proposals([self._cand()], chat_id=42)
        self.assertEqual(again, [])
        self.assertEqual(len(self.added), 1)


class _RegistryFileBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.reg_path = self.tmp / 'auto-fix-patterns.json'
        self._orig_reg = p5.REGISTRY_FILE
        p5.REGISTRY_FILE = self.reg_path

    def tearDown(self):
        p5.REGISTRY_FILE = self._orig_reg

    def _write(self, doc):
        self.reg_path.write_text(json.dumps(doc, indent=2))

    def _read(self):
        return json.loads(self.reg_path.read_text())


class TestApplyGraduation(_RegistryFileBase):

    def test_flips_probation_to_graduated_and_stamps(self):
        self._write(_registry(_pattern('restart-daemon')))
        ok = p5.apply_graduation('restart-daemon', path=self.reg_path,
                                 now='2026-06-04T12:00:00+00:00')
        self.assertTrue(ok)
        rec = self._read()['patterns'][0]
        self.assertEqual(rec['state'], 'graduated')
        self.assertEqual(rec['graduated_at'], '2026-06-04T12:00:00+00:00')

    def test_refuses_permanent_guard(self):
        self._write(_registry(_pattern('rotate-credential', reversible=False,
                                       permanent_guard=True)))
        ok = p5.apply_graduation('rotate-credential', path=self.reg_path)
        self.assertFalse(ok)
        self.assertEqual(self._read()['patterns'][0]['state'], 'probation')

    def test_unknown_template_returns_false(self):
        self._write(_registry(_pattern('restart-daemon')))
        self.assertFalse(p5.apply_graduation('nope', path=self.reg_path))


class TestDemotion(_RegistryFileBase):

    def test_graduated_failure_auto_demotes_no_approval(self):
        """A failure recorded against a graduated pattern demotes it to
        probation with clean_streak=0 — automatically, no approval emitted."""
        self._write(_registry(_pattern('restart-daemon', state='graduated',
                                       clean_streak=4)))
        # demote_on_adverse_execution reads the default REGISTRY_FILE, which the
        # base class has pointed at our tmp file.
        ok = p5.demote_on_adverse_execution('restart-daemon',
                                            reason='failed execution')
        self.assertTrue(ok)
        rec = self._read()['patterns'][0]
        self.assertEqual(rec['state'], 'probation')
        self.assertEqual(rec['clean_streak'], 0)
        self.assertIsNotNone(rec['last_larry_correction_at'])

    def test_graduated_correction_auto_demotes(self):
        self._write(_registry(_pattern('restart-daemon', state='graduated',
                                       clean_streak=3)))
        ok = p5.demote_on_adverse_execution('restart-daemon',
                                            reason='Larry-correction',
                                            correction=True)
        self.assertTrue(ok)
        self.assertEqual(self._read()['patterns'][0]['state'], 'probation')

    def test_adverse_on_probation_is_noop(self):
        """An adverse execution on an already-probation pattern is a no-op —
        the streak breaks naturally via consecutive_clean_streak()."""
        self._write(_registry(_pattern('restart-daemon', state='probation',
                                       clean_streak=2)))
        ok = p5.demote_on_adverse_execution('restart-daemon', reason='failed')
        self.assertFalse(ok)
        # state untouched
        self.assertEqual(self._read()['patterns'][0]['state'], 'probation')


class TestReconcile(_RegistryFileBase):

    def test_writes_derived_streak_into_cache(self):
        doc = _registry(_pattern('restart-daemon', clean_streak=0))
        store = {'restart-daemon': [_ex(days_ago=i) for i in range(4, 0, -1)]}
        changed = p5.reconcile_streaks(doc, store)
        self.assertTrue(changed)
        self.assertEqual(doc['patterns'][0]['clean_streak'], 4)

    def test_no_change_when_already_reconciled(self):
        doc = _registry(_pattern('restart-daemon', clean_streak=2))
        store = {'restart-daemon': [_ex(days_ago=i) for i in range(2, 0, -1)]}
        self.assertFalse(p5.reconcile_streaks(doc, store))


if __name__ == '__main__':
    unittest.main()
