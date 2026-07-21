#!/usr/bin/env python3
"""Tests for flip_readiness_gauge (the default-deny -> autonomy doorbell).

Covers the five pure criterion computations (green / red / indeterminate),
the substrate loaders' partial-data contract (missing vs unreadable), the
doorbell/regression transition machine (false->true rings once; true->true
silent; true->false warns), the 2-consecutive-dark escalation, and the main()
integration path (artifact write, same-day sentinel idempotency, exit 0 on a
dark substrate).

emit_alert is monkeypatched to a recorder — NO live larry-alerts IO — and every
IO test writes into a per-test tmp tree (the module path constants are patched
to that tree), so the suite makes ZERO live-tree writes (the _bootstrap sandbox
redirects AGENTS_ROOT to tmp as defense-in-depth on top of that).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_flip_readiness_gauge
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

import flip_readiness_gauge as frg  # noqa: E402

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# -------------------- criterion 1: escalation precision --------------------


class TestCriterion1(unittest.TestCase):

    def _rows(self, approved, rejected, *, days_ago=1.0):
        rows = [{'outcome': 'approved', 'ts': _iso(days_ago)} for _ in range(approved)]
        rows += [{'outcome': 'rejected', 'ts': _iso(days_ago)} for _ in range(rejected)]
        return rows

    def test_green_above_threshold(self):
        r = frg.compute_criterion_1(self._rows(19, 1), 'ok', now=NOW)
        self.assertIs(r.green, True)
        self.assertAlmostEqual(r.value, 0.95)

    def test_red_below_threshold(self):
        r = frg.compute_criterion_1(self._rows(8, 2), 'ok', now=NOW)
        self.assertIs(r.green, False)
        self.assertAlmostEqual(r.value, 0.80)

    def test_zero_decisions_is_indeterminate_not_green(self):
        r = frg.compute_criterion_1([], 'empty', now=NOW)
        self.assertIsNone(r.green)

    def test_out_of_window_rows_excluded(self):
        # 40d-old rows fall outside the 30d window -> no in-window decisions.
        r = frg.compute_criterion_1(self._rows(10, 0, days_ago=40), 'ok', now=NOW)
        self.assertIsNone(r.green)

    def test_dark_ledger_is_indeterminate(self):
        r = frg.compute_criterion_1([], 'error', now=NOW)
        self.assertIsNone(r.green)
        self.assertEqual(r.substrate_status, 'error')


# -------------------- criterion 2: backstop-caught misses --------------------


class TestCriterion2(unittest.TestCase):

    def test_zero_misses_is_green(self):
        r = frg.compute_criterion_2({'no-session-revision': [], 'rebase-obligation': []},
                                    False, now=NOW)
        self.assertIs(r.green, True)
        self.assertEqual(r.value, 0)

    def test_in_window_catch_is_red(self):
        by_label = {'no-session-revision': [{'opened_at': _iso(3)}],
                    'rebase-obligation': []}
        r = frg.compute_criterion_2(by_label, False, now=NOW)
        self.assertIs(r.green, False)
        self.assertEqual(r.value, 1)

    def test_out_of_window_catch_excluded(self):
        by_label = {'no-session-revision': [{'opened_at': _iso(40)}]}
        r = frg.compute_criterion_2(by_label, False, now=NOW)
        self.assertIs(r.green, True)

    def test_dark_ledger_is_indeterminate(self):
        r = frg.compute_criterion_2({}, True, now=NOW)
        self.assertIsNone(r.green)
        self.assertEqual(r.substrate_status, 'error')


# -------------------- criterion 3: verified auto-fix templates --------------------


class TestCriterion3(unittest.TestCase):

    def _tmpl(self, n_success, n_corrected, *, days_after_pr1=1.0):
        ts = (frg.PR1_CUTOFF + timedelta(days=days_after_pr1)).isoformat()
        execs = [{'outcome': 'success', 'larry_correction_signal': False, 'ts': ts}
                 for _ in range(n_success)]
        execs += [{'outcome': 'success', 'larry_correction_signal': True, 'ts': ts}
                  for _ in range(n_corrected)]
        return {'executions': execs}

    def test_three_qualifying_templates_is_green(self):
        templates = {f't{i}': self._tmpl(21, 0) for i in range(3)}
        r = frg.compute_criterion_3(templates, 'ok')
        self.assertIs(r.green, True)
        self.assertEqual(r.value, 3)

    def test_two_qualifying_is_red(self):
        templates = {f't{i}': self._tmpl(21, 0) for i in range(2)}
        r = frg.compute_criterion_3(templates, 'ok')
        self.assertIs(r.green, False)
        self.assertEqual(r.value, 2)

    def test_correction_signal_excluded_from_confirmed(self):
        # 19 confirmed + 5 corrected = 24 runs, rate 19/24 = 79% < 95% -> not
        # qualifying even though total >= 20.
        templates = {'t': self._tmpl(19, 5)}
        r = frg.compute_criterion_3(templates, 'ok')
        self.assertEqual(r.value, 0)

    def test_pre_pr1_executions_excluded(self):
        pre = (frg.PR1_CUTOFF - timedelta(days=5)).isoformat()
        templates = {'t': {'executions': [
            {'outcome': 'success', 'larry_correction_signal': False, 'ts': pre}
            for _ in range(30)]}}
        r = frg.compute_criterion_3(templates, 'ok')
        self.assertEqual(r.value, 0)  # all excluded -> no post-PR-1 runs

    def test_below_min_runs_not_qualifying(self):
        templates = {'t': self._tmpl(19, 0)}  # 19 < 20 runs
        r = frg.compute_criterion_3(templates, 'ok')
        self.assertEqual(r.value, 0)

    def test_dark_templates_indeterminate(self):
        r = frg.compute_criterion_3({}, 'error')
        self.assertIsNone(r.green)


# -------------------- criterion 4: over-silence audit --------------------


class TestCriterion4(unittest.TestCase):

    def test_no_findings_is_green(self):
        r = frg.compute_criterion_4({'over_silence_findings': []}, 'ok')
        self.assertIs(r.green, True)

    def test_findings_present_is_red(self):
        xiv = {'over_silence_findings': [{'source': 's', 'signature': 'x'}]}
        r = frg.compute_criterion_4(xiv, 'ok')
        self.assertIs(r.green, False)
        self.assertEqual(r.value, 1)

    def test_dark_xiv_indeterminate(self):
        r = frg.compute_criterion_4({}, 'error')
        self.assertIsNone(r.green)


# -------------------- criterion 5: projected approval volume --------------------


class TestCriterion5(unittest.TestCase):

    def _rows(self, auto, manual, *, days_ago=1.0):
        rows = [{'outcome': 'approved', 'ts': _iso(days_ago),
                 'notes': 'auto_approved by rule: {...}'} for _ in range(auto)]
        rows += [{'outcome': 'approved', 'ts': _iso(days_ago), 'notes': ''}
                 for _ in range(manual)]
        return rows

    def test_projection_at_or_below_current_is_green(self):
        # 1 of 10 needed a human -> projected 0.10 <= current 0.12.
        xiv = {'fleet': {'ask_rate': 0.12}}
        r = frg.compute_criterion_5(xiv, 'ok', self._rows(9, 1), 'ok', now=NOW)
        self.assertIs(r.green, True)

    def test_projection_above_current_is_red(self):
        # 5 of 10 needed a human -> projected 0.50 > current 0.12.
        xiv = {'fleet': {'ask_rate': 0.12}}
        r = frg.compute_criterion_5(xiv, 'ok', self._rows(5, 5), 'ok', now=NOW)
        self.assertIs(r.green, False)

    def test_no_in_window_decisions_indeterminate(self):
        xiv = {'fleet': {'ask_rate': 0.12}}
        r = frg.compute_criterion_5(xiv, 'ok', [], 'ok', now=NOW)
        self.assertIsNone(r.green)

    def test_missing_ask_rate_indeterminate(self):
        r = frg.compute_criterion_5({'fleet': {}}, 'ok', self._rows(9, 1), 'ok', now=NOW)
        self.assertIsNone(r.green)

    def test_dark_xiv_indeterminate(self):
        r = frg.compute_criterion_5({}, 'error', self._rows(9, 1), 'ok', now=NOW)
        self.assertIsNone(r.green)


# -------------------- substrate loaders (partial-data contract) --------------------


class TestLoaders(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='frg-loaders-'))

    def test_missing_ledger_is_dark(self):
        rows, status = frg.load_ledger_rows(self.tmp / 'nope.jsonl')
        self.assertEqual(status, 'error')
        self.assertEqual(rows, [])

    def test_ledger_reads_rows(self):
        p = self.tmp / 'ledger.jsonl'
        p.write_text('{"outcome":"approved","ts":"x"}\n\nnot-json\n{"a":1}\n')
        rows, status = frg.load_ledger_rows(p)
        self.assertEqual(status, 'ok')
        self.assertEqual(len(rows), 2)  # blank + non-json skipped

    def test_missing_backstop_file_is_empty_not_dark(self):
        by_label, dark = frg.load_backstop_ledgers(self.tmp)
        self.assertFalse(dark)  # missing files legitimately mean zero obligations
        self.assertEqual(by_label['no-session-revision'], [])

    def test_unreadable_backstop_file_is_dark(self):
        (self.tmp / 'no-session-revision-ledger.json').write_text('{bad json')
        _by, dark = frg.load_backstop_ledgers(self.tmp)
        self.assertTrue(dark)

    def test_backstop_dict_and_list_shapes(self):
        (self.tmp / 'no-session-revision-ledger.json').write_text(
            json.dumps({'k1': {'opened_at': 'x'}, 'k2': {'opened_at': 'y'}}))
        (self.tmp / 'rebase-obligation-ledger.json').write_text(
            json.dumps([{'opened_at': 'z'}]))
        by_label, dark = frg.load_backstop_ledgers(self.tmp)
        self.assertFalse(dark)
        self.assertEqual(len(by_label['no-session-revision']), 2)
        self.assertEqual(len(by_label['rebase-obligation']), 1)

    def test_xiv_missing_dir_is_dark(self):
        _obj, status = frg.latest_xiv_artifact(self.tmp / 'nope')
        self.assertEqual(status, 'error')

    def test_xiv_own_dark_run_is_dark(self):
        xdir = self.tmp / 'xiv'
        xdir.mkdir()
        (xdir / 'check-xiv-2026-07-20.json').write_text(
            json.dumps({'sources': {'log': 'error'}, 'fleet': {}}))
        _obj, status = frg.latest_xiv_artifact(xdir)
        self.assertEqual(status, 'error')

    def test_xiv_ok_run_reads(self):
        xdir = self.tmp / 'xiv'
        xdir.mkdir()
        (xdir / 'check-xiv-2026-07-20.json').write_text(
            json.dumps({'sources': {'log': 'ok'}, 'fleet': {'ask_rate': 0.1}}))
        obj, status = frg.latest_xiv_artifact(xdir)
        self.assertEqual(status, 'ok')
        self.assertEqual(obj['fleet']['ask_rate'], 0.1)


# -------------------- build_artifact + all_green semantics --------------------


def _cr(green):
    return frg.CriterionResult('k', 'L', green, 1.0, 0.9, 0.1, 'd', 'ok')


class TestBuildArtifact(unittest.TestCase):

    def test_all_green_requires_every_true(self):
        art = frg.build_artifact([_cr(True)] * 5, now=NOW,
                                 substrate_status={'ledger': 'ok'})
        self.assertTrue(art['all_green'])

    def test_indeterminate_blocks_all_green(self):
        art = frg.build_artifact([_cr(True)] * 4 + [_cr(None)], now=NOW,
                                 substrate_status={})
        self.assertFalse(art['all_green'])

    def test_one_red_blocks_all_green(self):
        art = frg.build_artifact([_cr(True)] * 4 + [_cr(False)], now=NOW,
                                 substrate_status={})
        self.assertFalse(art['all_green'])


# -------------------- transition machine (doorbell / regression) --------------------


class _EmitRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return True


class TestTransition(unittest.TestCase):

    def setUp(self):
        self._real_emit = frg.emit_alert
        self.rec = _EmitRecorder()
        frg.emit_alert = self.rec

    def tearDown(self):
        frg.emit_alert = self._real_emit

    def _artifact(self, all_green):
        return {'as_of': NOW.isoformat(), 'all_green': all_green,
                'criteria': {'k': _cr(all_green).as_dict()}}

    def test_false_to_true_rings_doorbell_once(self):
        state = frg.handle_transition(
            self._artifact(True), [_cr(True)], {'last_all_green': False}, now=NOW)
        self.assertEqual(len(self.rec.calls), 1)
        self.assertEqual(self.rec.calls[0]['subject'], 'flip-readiness-doorbell')
        self.assertTrue(state['last_all_green'])
        self.assertIn('last_doorbell_as_of', state)

    def test_true_to_true_is_silent(self):
        frg.handle_transition(
            self._artifact(True), [_cr(True)], {'last_all_green': True}, now=NOW)
        self.assertEqual(self.rec.calls, [])

    def test_true_to_false_warns_once(self):
        state = frg.handle_transition(
            self._artifact(False), [_cr(False)], {'last_all_green': True}, now=NOW)
        self.assertEqual(len(self.rec.calls), 1)
        self.assertEqual(self.rec.calls[0]['subject'], 'flip-readiness-regression')
        self.assertFalse(state['last_all_green'])

    def test_false_to_false_is_silent(self):
        frg.handle_transition(
            self._artifact(False), [_cr(False)], {'last_all_green': False}, now=NOW)
        self.assertEqual(self.rec.calls, [])

    def test_first_ever_run_all_green_rings(self):
        # No prior state (empty dict) -> prev_all_green falsy -> ring.
        frg.handle_transition(self._artifact(True), [_cr(True)], {}, now=NOW)
        self.assertEqual(len(self.rec.calls), 1)


# -------------------- dark escalation --------------------


class TestDarkEscalation(unittest.TestCase):

    def setUp(self):
        self._real_emit = frg.emit_alert
        self.rec = _EmitRecorder()
        frg.emit_alert = self.rec

    def tearDown(self):
        frg.emit_alert = self._real_emit

    def test_first_dark_run_does_not_escalate(self):
        new_state = {}
        frg.handle_dark_escalation({'ledger': 'error'}, {}, new_state)
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(new_state['consecutive_dark']['ledger'], 1)

    def test_second_consecutive_dark_escalates_once(self):
        new_state = {}
        prev = {'consecutive_dark': {'ledger': 1}}
        frg.handle_dark_escalation({'ledger': 'error'}, prev, new_state)
        self.assertEqual(len(self.rec.calls), 1)
        self.assertIn('flip-readiness-dark:ledger', self.rec.calls[0]['subject'])

    def test_recovered_substrate_resets_counter(self):
        new_state = {}
        prev = {'consecutive_dark': {'ledger': 5}}
        frg.handle_dark_escalation({'ledger': 'ok'}, prev, new_state)
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(new_state['consecutive_dark']['ledger'], 0)


# -------------------- main() integration (tmp-rooted, zero live writes) --------------------


class TestMainIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='frg-main-'))
        # Redirect every module path constant into the tmp tree.
        self._saved = {k: getattr(frg, k) for k in (
            'AGENTS_ROOT', 'LEDGER_FILE', 'TEMPLATES_FILE', 'XIV_ARTIFACT_DIR',
            'STATE_DIR', 'ARTIFACT_DIR', 'STATE_FILE', 'LOG_FILE')}
        frg.AGENTS_ROOT = self.tmp
        frg.LEDGER_FILE = self.tmp / 'state' / 'decision-outcome-ledger.jsonl'
        frg.TEMPLATES_FILE = self.tmp / 'state' / 'action-template-executions.json'
        frg.XIV_ARTIFACT_DIR = self.tmp / 'blackboard' / 'pulse-check-xiv'
        frg.STATE_DIR = self.tmp / 'state'
        frg.ARTIFACT_DIR = self.tmp / 'blackboard' / 'flip-readiness'
        frg.STATE_FILE = frg.ARTIFACT_DIR / 'gauge-state.json'
        frg.LOG_FILE = self.tmp / 'logs' / 'flip-readiness-gauge.log'
        frg.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        (self.tmp / 'state').mkdir(parents=True, exist_ok=True)
        self._real_emit = frg.emit_alert
        self.rec = _EmitRecorder()
        frg.emit_alert = self.rec

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(frg, k, v)
        frg.emit_alert = self._real_emit

    def test_all_dark_substrates_exit_zero_and_not_green(self):
        rc = frg.main([])
        self.assertEqual(rc, 0)
        artifacts = list(frg.ARTIFACT_DIR.glob('flip-readiness-*.json'))
        self.assertEqual(len(artifacts), 1)
        art = json.loads(artifacts[0].read_text())
        self.assertFalse(art['all_green'])
        # The three data substrates are dark -> their criteria indeterminate.
        for key in ('escalation_precision', 'verified_templates',
                    'over_silence_audit', 'projected_approval_volume'):
            self.assertIsNone(art['criteria'][key]['green'], key)
        self.assertEqual(art['substrate']['ledger'], 'error')
        # Absent backstop ledger files legitimately mean zero caught misses ->
        # criterion 2 is genuinely green (documented design; not dark).
        self.assertIs(art['criteria']['backstop_caught_misses']['green'], True)

    def test_same_day_sentinel_idempotent(self):
        self.assertEqual(frg.main([]), 0)
        # A second same-day run without --force is a no-op (no second artifact
        # rewrite path exercised; still exits 0).
        self.assertEqual(frg.main([]), 0)
        artifacts = list(frg.ARTIFACT_DIR.glob('flip-readiness-*.json'))
        self.assertEqual(len(artifacts), 1)

    def test_dry_run_writes_no_artifact_or_state(self):
        rc = frg.main(['--dry-run'])
        self.assertEqual(rc, 0)
        self.assertEqual(list(frg.ARTIFACT_DIR.glob('flip-readiness-*.json')), [])
        self.assertFalse(frg.STATE_FILE.exists())
        self.assertEqual(self.rec.calls, [])  # no DM in dry-run

    def test_all_green_writes_artifact_and_rings(self):
        # Seed all five substrates so every criterion is genuinely green.
        (frg.LEDGER_FILE).parent.mkdir(parents=True, exist_ok=True)
        ledger_lines = []
        for _ in range(19):
            ledger_lines.append(json.dumps({'outcome': 'approved', 'ts': _iso(1),
                                            'notes': 'auto_approved by rule'}))
        ledger_lines.append(json.dumps({'outcome': 'rejected', 'ts': _iso(1),
                                        'notes': ''}))
        frg.LEDGER_FILE.write_text('\n'.join(ledger_lines) + '\n')

        ts = (frg.PR1_CUTOFF + timedelta(days=1)).isoformat()
        templates = {f't{i}': {'executions': [
            {'outcome': 'success', 'larry_correction_signal': False, 'ts': ts}
            for _ in range(21)]} for i in range(3)}
        frg.TEMPLATES_FILE.write_text(json.dumps({'action_templates': templates}))

        frg.XIV_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        (frg.XIV_ARTIFACT_DIR / 'check-xiv-2026-07-20.json').write_text(json.dumps({
            'sources': {'log': 'ok'}, 'fleet': {'ask_rate': 0.99},
            'over_silence_findings': []}))

        rc = frg.main([])
        self.assertEqual(rc, 0)
        art = json.loads(next(frg.ARTIFACT_DIR.glob('flip-readiness-*.json')).read_text())
        self.assertTrue(art['all_green'], art['criteria'])
        # First-ever all-green run rings the doorbell exactly once.
        doorbells = [c for c in self.rec.calls if c['subject'] == 'flip-readiness-doorbell']
        self.assertEqual(len(doorbells), 1)
        # State persisted so a re-run stays silent.
        state = json.loads(frg.STATE_FILE.read_text())
        self.assertTrue(state['last_all_green'])


if __name__ == '__main__':
    unittest.main()
