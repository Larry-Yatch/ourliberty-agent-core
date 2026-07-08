#!/usr/bin/env python3
"""Tests for pulse_check_xiv (Check XIV V1 — alert-precision meter).

Covers signature normalization (regex ORDER is load-bearing: UUID/SHA before
digits), the pure metric core (compute_metrics), the classify re-run mapping
(tier 3/2/1/4 via injected registry+translations), the dark-source exit-0
contract (§ 7.2b), and the V1 safety property (§ 7.6): the run writes NOTHING to
alert-translations.json and emits NO `approve` shortcut.

larry-alerts / config IO is never touched live — emit_alert is monkeypatched and
the sandbox (via _bootstrap) redirects AGENTS_ROOT to a tmp tree.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_xiv
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_xiv as p14  # noqa: E402


NOVEL = p14.NOVEL_FALLTHROUGH_RATIONALE
_ROUTE = lambda source, subject, reversible: 'escalate'  # noqa: E731 hermetic route_fn


def _cr(source, subject, tier, rationale=''):
    """Build a ClassifiedRecord with its signature normalized like production."""
    return p14.ClassifiedRecord(
        source=source,
        signature=p14.normalize_signature(subject),
        subject=subject,
        message=f'{source}:{subject}',
        tier=tier,
        rationale=rationale,
    )


class TestNormalizeSignature(unittest.TestCase):

    def test_digits_collapse_to_hash(self):
        self.assertEqual(p14.normalize_signature('check-i-2026-07-07'),
                         'check-i-#-#-#')

    def test_uuid_collapses_before_digits_mangle_it(self):
        # A UUID has digits inside it; the UUID pass must fire first so the whole
        # token becomes a single '@', not a digit-shredded remnant.
        sig = p14.normalize_signature(
            'task 550e8400-e29b-41d4-a716-446655440000 failed')
        self.assertEqual(sig, 'task @ failed')

    def test_sha_collapses_before_digit_pass(self):
        # ORDER GUARD: a 40-hex SHA must map to a single '@'. If the digit pass
        # ran first it would rewrite the numeric chars of the hex and the SHA
        # regex could no longer match — the exact bug § 3 warns about.
        sig = p14.normalize_signature(
            'commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 landed')
        self.assertEqual(sig, 'commit @ landed')

    def test_short_sha_seven_hex_collapses(self):
        self.assertEqual(p14.normalize_signature('abandoned deadbee'),
                         'abandoned @')

    def test_pr_number_collapses(self):
        self.assertEqual(
            p14.normalize_signature(
                'auto-merge-conflict:Larry-Yatch/ourliberty-agent-core:833'),
            'auto-merge-conflict:larry-yatch/ourliberty-agent-core:#')

    def test_none_and_whitespace(self):
        self.assertEqual(p14.normalize_signature(None), '')
        self.assertEqual(p14.normalize_signature('  a   b  '), 'a b')

    def test_case_folded(self):
        self.assertEqual(p14.normalize_signature('WeeklY Digest'),
                         'weekly digest')


class TestComputeMetrics(unittest.TestCase):

    def test_rates_and_recurrence(self):
        records = [
            _cr('src', 'a', 3), _cr('src', 'a', 3), _cr('src', 'a', 3),
            _cr('src', 'b', 4, NOVEL),
        ]
        m = compute = p14.compute_metrics(records)
        fleet = m['fleet']
        self.assertEqual(fleet['volume'], 4)
        self.assertEqual(fleet['silence_rate'], 0.75)   # 3/4 tier-3
        self.assertEqual(fleet['ask_rate'], 0.25)       # 1/4 tier-4
        self.assertEqual(fleet['dispatch_rate'], 0.0)
        self.assertEqual(fleet['distinct_signatures'], 2)
        self.assertEqual(fleet['recurrence'], 2.0)      # 4 / 2 distinct
        src = m['per_source']['src']
        self.assertEqual(src['novelty'], 1.0)           # the one tier-4 is novel

    def test_dispatch_rate_counts_tier1_and_tier2(self):
        records = [_cr('s', 'x', 1), _cr('s', 'y', 2), _cr('s', 'z', 3)]
        m = p14.compute_metrics(records)
        self.assertAlmostEqual(m['fleet']['dispatch_rate'], round(2/3, 4))

    def test_recurring_novel_candidate_threshold(self):
        # 3 identical novel tier-4 -> candidate; 2 identical -> not.
        records = (
            [_cr('s', 'novel-a', 4, NOVEL)] * 3
            + [_cr('s', 'novel-b', 4, NOVEL)] * 2
        )
        m = p14.compute_metrics(records)
        sigs = {c['signature'] for c in m['recurring_novel_candidates']}
        self.assertIn('novel-a', sigs)
        self.assertNotIn('novel-b', sigs)

    def test_non_novel_tier4_not_a_candidate(self):
        # A tier-4 that is NOT the novel-fallthrough (e.g. never_silence) must
        # not count toward recurring-novel candidates.
        records = [_cr('s', 'k', 4, 'known never-silence pattern')] * 5
        m = p14.compute_metrics(records)
        self.assertEqual(m['recurring_novel_candidates'], [])

    def test_over_silence_finding(self):
        # High volume + near-total silence -> over-silence finding.
        records = [_cr('noisy', 'q', 3) for _ in range(p14.OVER_SILENCE_MIN_VOLUME)]
        m = p14.compute_metrics(records)
        self.assertEqual(len(m['over_silence_findings']), 1)
        f = m['over_silence_findings'][0]
        self.assertEqual(f['source'], 'noisy')
        self.assertEqual(f['silence_rate'], 1.0)
        self.assertGreaterEqual(f['volume'], p14.OVER_SILENCE_MIN_VOLUME)

    def test_over_silence_respects_volume_floor(self):
        records = [_cr('quiet', 'q', 3) for _ in range(p14.OVER_SILENCE_MIN_VOLUME - 1)]
        m = p14.compute_metrics(records)
        self.assertEqual(m['over_silence_findings'], [])

    def test_noise_candidate_share_is_reported_proxy(self):
        records = (
            [_cr('s', 'sil', 3)] * 4                     # 4 silenced
            + [_cr('s', 'rec-novel', 4, NOVEL)] * 3      # 3 recurring-novel
        )
        m = p14.compute_metrics(records)
        # (4 tier-3 + 3 recurring-novel) / 7 total
        self.assertEqual(m['fleet']['noise_candidate_share'], round(7/7, 4))


class TestClassifyReRun(unittest.TestCase):
    """The meter re-derives tier via alert_triage_state.classify with injected
    registry+translations — no join to stored triage."""

    def _run(self, raw, registry, translations):
        rec = p14.AlertRecord(
            ts=datetime.now(timezone.utc), source=str(raw.get('source') or ''),
            subject=raw.get('subject'), message='m', raw=raw)
        out = p14.classify_records([rec], registry=registry,
                                   translations=translations, route_fn=_ROUTE)
        return out[0]

    def test_translation_match_is_tier3(self):
        raw = {'source': 'known', 'subject': 'subj'}
        translations = {'known': {'subj': {'plain_language_summary': 'x'}}}
        c = self._run(raw, {}, translations)
        self.assertEqual(c.tier, 3)
        self.assertFalse(c.is_novel)

    def test_graduated_template_is_tier1(self):
        raw = {'source': 's', 'subject': 'x', 'template': 'tpl'}
        registry = {'tpl': {'template': 'tpl', 'state': 'graduated'}}
        c = self._run(raw, registry, {})
        self.assertEqual(c.tier, 1)

    def test_guarded_template_is_tier2(self):
        raw = {'source': 's', 'subject': 'x', 'template': 'tpl'}
        registry = {'tpl': {'template': 'tpl', 'state': 'probation'}}
        c = self._run(raw, registry, {})
        self.assertEqual(c.tier, 2)

    def test_fallthrough_is_tier4_novel(self):
        raw = {'source': 's', 'subject': 'brand-new'}
        c = self._run(raw, {}, {})
        self.assertEqual(c.tier, 4)
        self.assertTrue(c.is_novel)
        self.assertEqual(c.rationale, NOVEL)


class TestDarkSourceContract(unittest.TestCase):
    """§ 7.2b — a missing/unreadable log is a clean 0-exit with sources.log
    == 'error', a heartbeat still fires, and NO pulse-check-failed:xiv."""

    def setUp(self):
        self._emitted = []
        self._orig_emit = p14.emit_alert
        p14.emit_alert = lambda **kw: self._emitted.append(kw) or True
        # Guarantee the log is absent -> dark path.
        try:
            p14.ALERTS_FILE.unlink()
        except OSError:
            pass
        # Reset the dark counter so the first dark run is #1 (below escalate).
        p14.write_dark_run_count(0)

    def tearDown(self):
        p14.emit_alert = self._orig_emit

    def test_dark_run_exits_zero_and_writes_error_status(self):
        from datetime import datetime as _dt
        rc = p14.main([])
        self.assertEqual(rc, 0)
        art_path = p14.artifact_path_for(_dt.now(timezone.utc))
        art = json.loads(art_path.read_text())
        self.assertEqual(art['sources']['log'], 'error')
        # First dark run must not DM (escalate only after 2 consecutive).
        self.assertEqual(self._emitted, [])

    def test_second_dark_run_escalates(self):
        p14.write_dark_run_count(1)   # pretend one dark run already happened
        rc = p14.main(['--force'])
        self.assertEqual(rc, 0)
        subjects = [e['subject'] for e in self._emitted]
        self.assertIn('pulse-check-xiv-dark', subjects)

    def test_heartbeat_fires_on_dark_run_no_failure_alert(self):
        import pulse_check_heartbeat as hb
        p14.write_dark_run_count(0)
        rc = hb.run_check('xiv', p14.main, argv=[], log_fn=p14.log)
        self.assertEqual(rc, 0)
        self.assertTrue(hb.heartbeat_path('xiv').exists())


class TestV1SafetyProperty(unittest.TestCase):
    """§ 7.6 — V1 changes NO config. It must never write alert-translations.json
    and never emit an `approve check-xiv-update` shortcut (that is XIV-c)."""

    def test_run_writes_nothing_to_translations_config(self):
        # Behavioral proof (not a prose scan — the docstring legitimately NAMES
        # alert-translations.json to explain what V1 avoids): capture every path
        # the meter writes during a full run and assert none is the translations
        # config. All writes must land under the XIV artifact dir / state file.
        written: list[Path] = []
        orig_write = p14.atomic_write_json
        orig_emit = p14.emit_alert
        p14.atomic_write_json = lambda path, *a, **k: (written.append(Path(path))
                                                       or orig_write(path, *a, **k))
        p14.emit_alert = lambda **kw: True
        try:
            p14.ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            p14.ALERTS_FILE.write_text(json.dumps({
                'ts': now.isoformat(), 'source': 's', 'subject': 'x',
                'message': 'm'}) + '\n')
            try:
                p14.artifact_path_for(now).unlink()
            except OSError:
                pass
            self.assertEqual(p14.main([]), 0)
        finally:
            p14.atomic_write_json = orig_write
            p14.emit_alert = orig_emit
        self.assertTrue(written, 'expected at least the artifact write')
        for path in written:
            self.assertNotEqual(path.name, 'alert-translations.json')
            # Every write stays inside the XIV artifact tree.
            self.assertIn('pulse-check-xiv', str(path))

    def test_emitted_alerts_stay_in_xiv_namespace_and_carry_no_approve(self):
        emitted = []
        orig = p14.emit_alert
        p14.emit_alert = lambda **kw: emitted.append(kw) or True
        try:
            # A synthetic over-silence artifact drives every emit path.
            findings = [{'source': 'noisy', 'signature': 'q',
                         'volume': 99, 'silence_rate': 1.0}]
            art = {
                'as_of': datetime.now(timezone.utc).isoformat(),
                'window': {'days': 14},
                'fleet': {'volume': 99, 'silence_rate': 1.0, 'ask_rate': 0.0,
                          'dispatch_rate': 0.0, 'noise_candidate_share': 1.0},
                'over_silence_findings': findings,
                'recurring_novel_candidates_capped': [],
            }
            p14.emit_digest_and_surfaces(art, now=datetime.now(timezone.utc))
        finally:
            p14.emit_alert = orig
        self.assertTrue(emitted)
        for e in emitted:
            self.assertTrue(e['subject'].startswith('pulse-check-xiv'),
                            f'unexpected subject {e["subject"]!r}')
            self.assertNotIn('approve', e['message'].lower())


class TestDigestGating(unittest.TestCase):

    def test_first_monday_detection(self):
        from datetime import date
        self.assertTrue(p14.is_first_monday(date(2026, 7, 6)))    # 1st Monday
        self.assertFalse(p14.is_first_monday(date(2026, 7, 13)))  # 2nd Monday
        self.assertFalse(p14.is_first_monday(date(2026, 7, 7)))   # Tuesday

    def test_digest_fires_when_over_silence_trips_off_cycle(self):
        emitted = []
        orig = p14.emit_alert
        p14.emit_alert = lambda **kw: emitted.append(kw) or True
        try:
            # A Tuesday (not first Monday) but over-silence present -> digest fires.
            tuesday = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
            art = {
                'as_of': tuesday.isoformat(), 'window': {'days': 14},
                'fleet': {'volume': 60, 'silence_rate': 1.0, 'ask_rate': 0.0,
                          'dispatch_rate': 0.0, 'noise_candidate_share': 1.0},
                'over_silence_findings': [{'source': 'n', 'signature': 'q',
                                           'volume': 60, 'silence_rate': 1.0}],
                'recurring_novel_candidates_capped': [],
            }
            p14.emit_digest_and_surfaces(art, now=tuesday)
        finally:
            p14.emit_alert = orig
        subjects = [e['subject'] for e in emitted]
        self.assertIn('pulse-check-xiv-digest', subjects)

    def test_no_digest_on_quiet_off_cycle_day(self):
        emitted = []
        orig = p14.emit_alert
        p14.emit_alert = lambda **kw: emitted.append(kw) or True
        try:
            tuesday = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
            art = {
                'as_of': tuesday.isoformat(), 'window': {'days': 14},
                'fleet': {'volume': 10, 'silence_rate': 0.5, 'ask_rate': 0.5,
                          'dispatch_rate': 0.0, 'noise_candidate_share': 0.5},
                'over_silence_findings': [],
                'recurring_novel_candidates_capped': [],
            }
            p14.emit_digest_and_surfaces(art, now=tuesday)
        finally:
            p14.emit_alert = orig
        self.assertEqual(emitted, [])   # alert-toil applies to XIV itself


class TestMainHappyPath(unittest.TestCase):

    def setUp(self):
        self._orig_emit = p14.emit_alert
        p14.emit_alert = lambda **kw: True
        p14.ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        lines = []
        for i in range(6):
            lines.append(json.dumps({
                'ts': (now - timedelta(hours=i)).isoformat(),
                'source': 'demo-source',
                'subject': f'thing-{i}',
                'message': f'demo message {i}',
            }))
        p14.ALERTS_FILE.write_text('\n'.join(lines) + '\n')
        # Fresh artifact dir for a deterministic run.
        self._art_path = p14.artifact_path_for(now)
        try:
            self._art_path.unlink()
        except OSError:
            pass

    def tearDown(self):
        p14.emit_alert = self._orig_emit

    def test_run_writes_artifact_with_metrics_and_exits_zero(self):
        rc = p14.main([])
        self.assertEqual(rc, 0)
        art = json.loads(self._art_path.read_text())
        self.assertEqual(art['sources']['log'], 'ok')
        self.assertEqual(art['fleet']['volume'], 6)
        self.assertIn('demo-source', art['per_source'])
        self.assertIn('proxy_note', art)

    def test_same_day_rerun_is_idempotent_noop(self):
        self.assertEqual(p14.main([]), 0)
        mtime1 = self._art_path.stat().st_mtime_ns
        # Second run same day without --force must not rewrite the artifact.
        self.assertEqual(p14.main([]), 0)
        self.assertEqual(self._art_path.stat().st_mtime_ns, mtime1)


if __name__ == '__main__':
    unittest.main()
