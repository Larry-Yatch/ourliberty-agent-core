#!/usr/bin/env python3
"""Tests for promote_alerts.py — N4 needs-CEO-attention promotion rule.

Spec: agents/beacon/specs/approvals-queue-rework.md L6 / node N4.

Run:
    python3 -m unittest scripts.tests.test_promote_alerts
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import promote_alerts as pa  # noqa: E402
import for_larry_signal as fls  # noqa: E402

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
FX = 'zz-fixture-'  # all test identities use the reserved fixture namespace


def _cand(dedup, *, severity='critical', explicit=False, ts=None,
          headline='boom', task_id=None):
    return pa.Candidate(
        dedup_identity=FX + dedup,
        severity=severity,
        task_id=task_id or (FX + dedup),
        ts=ts or NOW.isoformat(),
        headline=headline,
        detail='detail',
        explicit=explicit,
        raw={},
    )


class _Emitter:
    """Capturing fake for the surfacing seam."""

    def __init__(self, result=True):
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def __call__(self, candidate, reason):
        self.calls.append((candidate.dedup_identity, reason))
        return self.result


# -------------------- severity --------------------

class SeverityTest(unittest.TestCase):
    def test_normalize_lowercases_and_trims(self):
        self.assertEqual(pa.normalize_severity('  CRITICAL '), 'critical')

    def test_normalize_non_string_is_none(self):
        self.assertIsNone(pa.normalize_severity(None))
        self.assertIsNone(pa.normalize_severity(3))

    def test_critical_meets_critical_bar(self):
        self.assertTrue(pa.meets_severity_bar('critical', 'critical'))

    def test_warning_below_critical_bar(self):
        self.assertFalse(pa.meets_severity_bar('warning', 'critical'))

    def test_unknown_severity_never_meets_bar(self):
        self.assertFalse(pa.meets_severity_bar('apocalyptic', 'critical'))
        self.assertFalse(pa.meets_severity_bar(None, 'critical'))

    def test_uses_larry_alerts_vocabulary(self):
        # Sanity: the rank table is derived from the reused canonical vocab.
        self.assertIn('critical', pa._SEVERITY_RANK)
        self.assertIn('warning', pa._SEVERITY_RANK)


# -------------------- config --------------------

class ConfigTest(unittest.TestCase):
    def test_missing_file_returns_conservative_defaults(self):
        cfg = pa.load_config(Path('/nonexistent/promotion-rules.json'))
        self.assertEqual(cfg['severity_bar'], 'critical')
        self.assertEqual(cfg['self_resolve_window_seconds'], 600)
        self.assertTrue(cfg['promotion_enabled'])

    def test_malformed_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cfg.json'
            p.write_text('{not json')
            cfg = pa.load_config(p)
            self.assertEqual(cfg, pa.DEFAULT_CONFIG)

    def test_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cfg.json'
            p.write_text(json.dumps({'self_resolve_window_seconds': 30}))
            cfg = pa.load_config(p)
            self.assertEqual(cfg['self_resolve_window_seconds'], 30)
            # Untouched keys keep defaults.
            self.assertEqual(cfg['severity_bar'], 'critical')


# -------------------- normalize_entry / read_candidates --------------------

class NormalizeEntryTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(pa.DEFAULT_CONFIG)

    def test_dedup_identity_prefers_dedup_field(self):
        c = pa.normalize_entry(
            {'dedup_identity': FX + 'd', 'task_id': FX + 't', 'severity': 'critical'},
            self.cfg,
        )
        self.assertEqual(c.dedup_identity, FX + 'd')

    def test_dedup_falls_back_to_task_id(self):
        c = pa.normalize_entry({'task_id': FX + 't', 'severity': 'critical'}, self.cfg)
        self.assertEqual(c.dedup_identity, FX + 't')

    def test_no_identity_returns_none(self):
        self.assertIsNone(pa.normalize_entry({'severity': 'critical'}, self.cfg))

    def test_explicit_flag_detected(self):
        c = pa.normalize_entry(
            {'task_id': FX + 't', 'escalate_to_larry': True, 'severity': 'warning'},
            self.cfg,
        )
        self.assertTrue(c.explicit)

    def test_read_candidates_dict_shape(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'esc.json'
            p.write_text(json.dumps({'escalations': [
                {'task_id': FX + 'a', 'severity': 'critical'},
                {'severity': 'critical'},  # no identity → skipped
            ]}))
            cands = pa.read_candidates(p, self.cfg)
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0].dedup_identity, FX + 'a')

    def test_read_candidates_bare_list(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'esc.json'
            p.write_text(json.dumps([{'task_id': FX + 'a', 'severity': 'critical'}]))
            self.assertEqual(len(pa.read_candidates(p, self.cfg)), 1)

    def test_read_candidates_missing_file(self):
        self.assertEqual(pa.read_candidates(Path('/nope.json'), self.cfg), [])


# -------------------- decide (pure) --------------------

class DecideTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(pa.DEFAULT_CONFIG)

    def test_explicit_promotes_immediately(self):
        d, r = pa.decide(_cand('x', severity='warning', explicit=True),
                         first_seen=NOW, now=NOW, cfg=self.cfg)
        self.assertEqual(d, pa.PROMOTE)
        self.assertEqual(r, 'explicit-flag')

    def test_below_bar_skips(self):
        d, r = pa.decide(_cand('x', severity='warning'),
                         first_seen=NOW, now=NOW, cfg=self.cfg)
        self.assertEqual(d, pa.SKIP)
        self.assertEqual(r, 'below-severity-bar')

    def test_critical_within_window_holds(self):
        d, r = pa.decide(_cand('x', severity='critical'),
                         first_seen=NOW, now=NOW + timedelta(seconds=10),
                         cfg=self.cfg)
        self.assertEqual(d, pa.HOLD)

    def test_critical_past_window_promotes(self):
        d, r = pa.decide(_cand('x', severity='critical'),
                         first_seen=NOW, now=NOW + timedelta(seconds=601),
                         cfg=self.cfg)
        self.assertEqual(d, pa.PROMOTE)
        self.assertEqual(r, 'critical-persisted')

    def test_disabled_skips_everything(self):
        cfg = dict(self.cfg, promotion_enabled=False)
        d, r = pa.decide(_cand('x', severity='critical', explicit=True),
                         first_seen=NOW, now=NOW + timedelta(seconds=999), cfg=cfg)
        self.assertEqual(d, pa.SKIP)


# -------------------- run_cycle (integration of the pieces) --------------------

class RunCycleTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(pa.DEFAULT_CONFIG)

    def _run(self, candidates, state, now, emitter=None):
        emitter = emitter or _Emitter()
        summary = pa.run_cycle(
            candidates=candidates, state=state, cfg=self.cfg, now=now,
            emit_fn=emitter, persist=False,
        )
        return summary, emitter

    def test_critical_first_cycle_holds_not_promoted(self):
        summary, emitter = self._run([_cand('a')], {}, NOW)
        self.assertEqual(emitter.calls, [])
        self.assertEqual(summary['promoted'], [])
        self.assertEqual(summary['held'], [FX + 'a'])

    def test_critical_persists_then_promotes_next_cycle(self):
        # Cycle 1: held, state records first_seen.
        s1, e1 = self._run([_cand('a')], {}, NOW)
        self.assertEqual(e1.calls, [])
        # Reconstruct state as run_cycle would have persisted it.
        state = {FX + 'a': {
            'first_seen_ts': NOW.isoformat(),
            'last_seen_ts': NOW.isoformat(),
            'severity': 'critical',
            'task_id': FX + 'a',
            'promoted': False,
        }}
        # Cycle 2, one cycle later, escalation still present.
        s2, e2 = self._run([_cand('a')], state, NOW + timedelta(seconds=601))
        self.assertEqual(len(e2.calls), 1)
        self.assertEqual(e2.calls[0][1], 'critical-persisted')
        self.assertEqual(s2['promoted'], [FX + 'a'])

    def test_self_resolved_within_one_cycle_never_promotes(self):
        # Seen critical at cycle 1 (held)...
        state = {FX + 'a': {
            'first_seen_ts': NOW.isoformat(), 'last_seen_ts': NOW.isoformat(),
            'severity': 'critical', 'task_id': FX + 'a', 'promoted': False,
        }}
        # ...gone by cycle 2 (absent from candidates) → self-resolved.
        summary, emitter = self._run([], state, NOW + timedelta(seconds=601))
        self.assertEqual(emitter.calls, [])
        self.assertEqual(summary['promoted'], [])

    def test_explicit_promotes_first_cycle(self):
        summary, emitter = self._run([_cand('a', explicit=True)], {}, NOW)
        self.assertEqual(len(emitter.calls), 1)
        self.assertEqual(emitter.calls[0][1], 'explicit-flag')

    def test_routine_warning_never_reaches_larry(self):
        summary, emitter = self._run([_cand('a', severity='warning')], {}, NOW)
        self.assertEqual(emitter.calls, [])
        self.assertEqual(summary['skipped'], [FX + 'a'])

    def test_already_promoted_not_re_promoted(self):
        state = {FX + 'a': {
            'first_seen_ts': NOW.isoformat(), 'last_seen_ts': NOW.isoformat(),
            'severity': 'critical', 'task_id': FX + 'a', 'promoted': True,
            'promoted_ts': NOW.isoformat(),
        }}
        summary, emitter = self._run([_cand('a')], state, NOW + timedelta(seconds=999))
        self.assertEqual(emitter.calls, [])
        self.assertEqual(summary['promoted'], [])

    def test_failed_emit_leaves_unpromoted_for_retry(self):
        failing = _Emitter(result=False)
        state = {FX + 'a': {
            'first_seen_ts': NOW.isoformat(), 'last_seen_ts': NOW.isoformat(),
            'severity': 'critical', 'task_id': FX + 'a', 'promoted': False,
        }}
        summary, _ = self._run([_cand('a')], state,
                               NOW + timedelta(seconds=601), emitter=failing)
        self.assertEqual(len(failing.calls), 1)
        self.assertEqual(summary['promoted'], [])
        self.assertIn(FX + 'a', summary['held'])


# -------------------- state IO --------------------

class StateIOTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.json'
            cands = {FX + 'a': {'first_seen_ts': NOW.isoformat(), 'promoted': True}}
            pa.write_state(cands, p)
            loaded = pa.load_state(p)
            self.assertEqual(loaded, cands)

    def test_load_missing_returns_empty(self):
        self.assertEqual(pa.load_state(Path('/nope/state.json')), {})


# -------------------- §5.1 for-Larry durable signal (mirror matrix) --------------------

class ForLarryRecordBuilderTest(unittest.TestCase):
    """The pure builder: only PROMOTED, still-present candidates yield records."""

    def test_promoted_candidate_yields_record(self):
        state = {FX + 'a': {'promoted': True, 'promoted_ts': NOW.isoformat()}}
        out = pa.for_larry_active_records([_cand('a')], state)
        key = fls.ESCALATION_KEY_PREFIX + FX + 'a'
        self.assertIn(key, out)
        self.assertEqual(out[key]['dedup_identity'], FX + 'a')
        self.assertEqual(out[key]['source_kind'], 'critical-unhandled')

    def test_unpromoted_candidate_yields_nothing(self):
        state = {FX + 'a': {'promoted': False}}
        self.assertEqual(pa.for_larry_active_records([_cand('a')], state), {})

    def test_candidate_absent_from_state_yields_nothing(self):
        self.assertEqual(pa.for_larry_active_records([_cand('a')], {}), {})


class ForLarrySignalCycleTest(unittest.TestCase):
    """run_cycle(persist=True) feeds the canonical signal: crosses-bar→written,
    self-resolves→cleared, below-bar→none (spec §5.1 mirror focus)."""

    def setUp(self):
        self.cfg = dict(pa.DEFAULT_CONFIG)
        self._dir = tempfile.TemporaryDirectory()
        self.signal = Path(self._dir.name) / 'for-larry-escalations.json'
        self._prev = os.environ.get('OURLIBERTY_FOR_LARRY_SIGNAL_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = str(self.signal)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_SIGNAL_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = self._prev
        self._dir.cleanup()

    def _keys(self):
        return {e['key'] for e in fls.active_entries()}

    def _run(self, candidates, state, now):
        return pa.run_cycle(
            candidates=candidates, state=state, cfg=self.cfg, now=now,
            emit_fn=_Emitter(), persist=True,
        )

    def test_crosses_bar_writes_record(self):
        state = {FX + 'a': {
            'first_seen_ts': NOW.isoformat(), 'last_seen_ts': NOW.isoformat(),
            'severity': 'critical', 'task_id': FX + 'a', 'promoted': False,
        }}
        self._run([_cand('a')], state, NOW + timedelta(seconds=601))
        self.assertEqual(self._keys(), {fls.ESCALATION_KEY_PREFIX + FX + 'a'})

    def test_self_resolves_clears_record(self):
        # Establish an active promoted record.
        state = {FX + 'a': {
            'first_seen_ts': NOW.isoformat(), 'last_seen_ts': NOW.isoformat(),
            'severity': 'critical', 'task_id': FX + 'a', 'promoted': False,
        }}
        self._run([_cand('a')], state, NOW + timedelta(seconds=601))
        self.assertEqual(self._keys(), {fls.ESCALATION_KEY_PREFIX + FX + 'a'})
        # Next cycle: candidate gone → self-resolved → record cleared.
        carried = pa.load_state()
        self._run([], carried, NOW + timedelta(seconds=1202))
        self.assertEqual(self._keys(), set())
        rec = fls.load_records()[fls.ESCALATION_KEY_PREFIX + FX + 'a']
        self.assertIs(rec['resolved'], True)

    def test_below_bar_writes_nothing(self):
        self._run([_cand('a', severity='warning')], {}, NOW)
        self.assertEqual(self._keys(), set())

    def test_held_critical_first_cycle_writes_nothing(self):
        # Critical but still within self-resolve window → held, not promoted.
        self._run([_cand('a')], {}, NOW)
        self.assertEqual(self._keys(), set())


if __name__ == '__main__':
    unittest.main()
