#!/usr/bin/env python3
"""Tests for heal_review_ceiling_fit — the weekly READ-ONLY Mirror
review-ceiling fit-monitor.

Covers the spec's acceptance set:
  - duration parsing + percentile math on a fixture costs.jsonl (mixed
    agents/task_ids -> only mirror PR-reviews counted).
  - HEADROOM_LOW fires when p95 >= ceiling * ratio; not otherwise.
  - false-kill detection: a fired PR that gh reports MERGED is flagged
    (the gh call is mocked).
  - the digest is routed non-escalating (route != 'escalate').
  - recommendation math: no-signal -> "no change"; low-headroom -> raise to
    round_up(p99 * multiplier).
plus the Mirror review-focus invariants: read-only, empty-sample safety, and
gh-failure -> 'unknown' (not a false-kill).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_review_ceiling_fit
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_review_ceiling_fit as h  # noqa: E402


def _rules(window_days=30, ratio=0.8, mult=1.25, enabled=True):
    return {
        'enabled': enabled,
        'window_days': window_days,
        'headroom_low_ratio': ratio,
        'recommend_multiplier': mult,
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class DurationParsingTest(unittest.TestCase):
    """Only mirror PR-review rows (agent==mirror, task_id pr-/review*,
    positive duration_sec) inside the window are counted."""

    def setUp(self):
        self.now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
        self.window_start = self.now - timedelta(days=30)
        recent = _iso(self.now - timedelta(days=1))
        self.rows = [
            # counted: mirror pr-review
            {'agent': 'mirror', 'task_id': 'pr-700-review',
             'duration_sec': 600, 'ts': recent},
            # counted: mirror review* task_id
            {'agent': 'mirror', 'task_id': 'review-foo',
             'duration_sec': 1200, 'ts': recent},
            # excluded: wrong agent
            {'agent': 'forge', 'task_id': 'pr-700-build',
             'duration_sec': 9999, 'ts': recent},
            # excluded: mirror but non-review task_id
            {'agent': 'mirror', 'task_id': 'dag-preflight-x',
             'duration_sec': 8888, 'ts': recent},
            # excluded: out of window
            {'agent': 'mirror', 'task_id': 'pr-1-review',
             'duration_sec': 4242, 'ts': _iso(self.now - timedelta(days=99))},
            # excluded: missing duration
            {'agent': 'mirror', 'task_id': 'pr-2-review', 'ts': recent},
            # excluded: zero/negative duration
            {'agent': 'mirror', 'task_id': 'pr-3-review',
             'duration_sec': 0, 'ts': recent},
            # excluded: malformed (not a dict handled by iter; here a non-row)
            {'agent': 'mirror', 'task_id': 'pr-4-review',
             'duration_sec': 'oops', 'ts': recent},
        ]

    def test_only_mirror_reviews_in_window_counted(self):
        durs = h.collect_review_durations(self.rows, self.window_start, self.now)
        self.assertEqual(sorted(durs), [600.0, 1200.0])

    def test_concurrent_slots_both_counted(self):
        # mirror-two-slot-review §4 PR2 audit: two review slots produce two cost
        # rows with OVERLAPPING wall-clock (same ts). Because each row carries
        # its OWN duration_sec (never a gap between consecutive rows), both are
        # sampled independently — concurrency does not distort the distribution.
        overlap = _iso(self.now - timedelta(hours=2))
        rows = [
            {'agent': 'mirror', 'task_id': 'pr-800-review',
             'duration_sec': 900, 'ts': overlap},   # slot 0
            {'agent': 'mirror', 'task_id': 'pr-801-review',
             'duration_sec': 1500, 'ts': overlap},  # slot 1, same instant
        ]
        durs = h.collect_review_durations(rows, self.window_start, self.now)
        self.assertEqual(sorted(durs), [900.0, 1500.0])

    def test_is_mirror_review_row_predicate(self):
        self.assertTrue(h.is_mirror_review_row(
            {'agent': 'mirror', 'task_id': 'pr-9-review', 'duration_sec': 1}))
        self.assertTrue(h.is_mirror_review_row(
            {'agent': 'mirror', 'task_id': 'review-x', 'duration_sec': 1}))
        self.assertFalse(h.is_mirror_review_row(
            {'agent': 'mirror', 'task_id': 'build-x', 'duration_sec': 1}))
        self.assertFalse(h.is_mirror_review_row(
            {'agent': 'forge', 'task_id': 'pr-x-review', 'duration_sec': 1}))
        # bool is not a valid duration (True/False are ints in Python)
        self.assertFalse(h.is_mirror_review_row(
            {'agent': 'mirror', 'task_id': 'pr-x-review', 'duration_sec': True}))


class PercentileTest(unittest.TestCase):
    """Percentile math, with the empty-sample safety the Mirror focus demands."""

    def test_empty_is_zero(self):
        self.assertEqual(h.compute_percentile([], 0.95), 0.0)

    def test_single_value(self):
        self.assertEqual(h.compute_percentile([42.0], 0.99), 42.0)

    def test_interpolation(self):
        vals = [float(x) for x in range(1, 101)]  # 1..100
        # p50 of 1..100 with (n-1)*p interpolation = 50.5
        self.assertAlmostEqual(h.compute_percentile(vals, 0.50), 50.5)
        self.assertAlmostEqual(h.compute_percentile(vals, 0.99), 99.01)

    def test_distribution_all_zero_on_empty(self):
        d = h.duration_distribution([])
        self.assertEqual(d['count'], 0)
        for k in ('p50', 'p90', 'p95', 'p99', 'max'):
            self.assertEqual(d[k], 0.0)

    def test_distribution_fields(self):
        d = h.duration_distribution([100.0, 200.0, 300.0])
        self.assertEqual(d['count'], 3)
        self.assertEqual(d['max'], 300.0)
        self.assertEqual(d['p50'], 200.0)


class HeadroomLowTest(unittest.TestCase):
    """HEADROOM_LOW fires iff p95 >= ceiling * ratio (and samples exist and the
    ceiling is enabled)."""

    def test_fires_when_p95_crosses_threshold(self):
        # ceiling 2100, ratio 0.8 -> threshold 1680. p95 of these is >= 1680.
        durs = [1700.0] * 20
        dist = h.duration_distribution(durs)
        rec = h.compute_recommendation(dist, 2100, 0, 0, _rules())
        self.assertTrue(rec['headroom_low'])
        self.assertTrue(rec['raise_recommended'])

    def test_does_not_fire_with_healthy_headroom(self):
        # p95 well under 1680 -> no flag.
        durs = [600.0] * 20
        dist = h.duration_distribution(durs)
        rec = h.compute_recommendation(dist, 2100, 0, 0, _rules())
        self.assertFalse(rec['headroom_low'])
        self.assertFalse(rec['raise_recommended'])

    def test_does_not_fire_on_empty_samples(self):
        dist = h.duration_distribution([])
        rec = h.compute_recommendation(dist, 2100, 0, 0, _rules())
        self.assertFalse(rec['headroom_low'])
        self.assertFalse(rec['raise_recommended'])


class RecommendationMathTest(unittest.TestCase):
    """no-signal -> no change; signal -> raise to round_up_to_5min(p99*mult)."""

    def test_round_up_to_5min(self):
        self.assertEqual(h.round_up_to_5min(0), 0)
        self.assertEqual(h.round_up_to_5min(1), 300)
        self.assertEqual(h.round_up_to_5min(300), 300)
        self.assertEqual(h.round_up_to_5min(301), 600)
        self.assertEqual(h.round_up_to_5min(1830 * 1.25), 2400)  # 2287.5 -> 2400

    def test_no_signal_recommends_no_change(self):
        durs = [600.0] * 50  # healthy, well under ceiling
        dist = h.duration_distribution(durs)
        rec = h.compute_recommendation(dist, 2100, 0, 0, _rules())
        self.assertFalse(rec['raise_recommended'])
        self.assertIn('no change', rec['recommendation_text'])
        # never recommends lowering below the current ceiling
        self.assertEqual(rec['recommended_ceiling_sec'], 2100)

    def test_low_headroom_recommends_round_up_p99(self):
        durs = [1900.0] * 100  # p99 ~1900, crosses 0.8*2100=1680
        dist = h.duration_distribution(durs)
        rec = h.compute_recommendation(dist, 2100, 0, 0, _rules())
        self.assertTrue(rec['raise_recommended'])
        expected = h.round_up_to_5min(dist['p99'] * 1.25)
        self.assertEqual(rec['recommended_ceiling_sec'], max(expected, 2100))
        self.assertIn('RAISE', rec['recommendation_text'])

    def test_false_kill_forces_raise_even_with_headroom(self):
        durs = [600.0] * 50  # healthy headroom, but a false-kill happened
        dist = h.duration_distribution(durs)
        rec = h.compute_recommendation(dist, 2100, 1, 1, _rules())
        self.assertTrue(rec['false_kill'])
        self.assertTrue(rec['raise_recommended'])
        self.assertIn('FALSE_KILL', rec['recommendation_text'])

    def test_disabled_ceiling_no_headroom_analysis(self):
        durs = [600.0] * 10
        dist = h.duration_distribution(durs)
        rec = h.compute_recommendation(dist, 0, 0, 0, _rules())
        self.assertFalse(rec['raise_recommended'])
        self.assertIn('DISABLED', rec['recommendation_text'])


class FiringParseTest(unittest.TestCase):
    """Parse REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED lines, windowed + deduped."""

    def setUp(self):
        self.now = datetime(2026, 6, 26, 12, 0, 0)  # naive host-local
        self.window_start = self.now - timedelta(days=30)

    def test_parses_task_and_pr(self):
        log = ("[2026-06-25 09:00:00] [notifier] [INFO] "
               "REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED task='pr-700-review' "
               "pr_url='https://github.com/x/y/pull/700' timeout_seconds=2100 — z\n")
        firings = h.parse_timeout_firings(log, self.window_start, self.now)
        self.assertEqual(len(firings), 1)
        self.assertEqual(firings[0]['task_id'], 'pr-700-review')
        self.assertEqual(firings[0]['pr_url'],
                         'https://github.com/x/y/pull/700')

    def test_excludes_out_of_window(self):
        log = ("[2026-01-01 09:00:00] [notifier] [INFO] "
               "REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED task='pr-1-review' "
               "pr_url='https://github.com/x/y/pull/1' timeout_seconds=2100\n")
        firings = h.parse_timeout_firings(log, self.window_start, self.now)
        self.assertEqual(firings, [])

    def test_dedup_by_task(self):
        line = ("[2026-06-25 09:00:00] [notifier] [INFO] "
                "REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED task='pr-700-review' "
                "pr_url='https://github.com/x/y/pull/700' timeout_seconds=2100\n")
        firings = h.parse_timeout_firings(line + line, self.window_start, self.now)
        self.assertEqual(len(firings), 1)

    def test_none_fields_tolerated(self):
        log = ("[2026-06-25 09:00:00] [notifier] [INFO] "
               "REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED task=None pr_url=None "
               "timeout_seconds=2100\n")
        firings = h.parse_timeout_firings(log, self.window_start, self.now)
        self.assertEqual(len(firings), 1)
        self.assertIsNone(firings[0]['task_id'])
        self.assertIsNone(firings[0]['pr_url'])

    def test_empty_log_no_firings(self):
        self.assertEqual(
            h.parse_timeout_firings('', self.window_start, self.now), [])


class FalseKillTest(unittest.TestCase):
    """A fired PR that gh reports MERGED is a false-kill; gh failures degrade to
    'unknown' (never a false-kill)."""

    def test_merged_pr_is_false_kill(self):
        firings = [{'task_id': 'pr-700-review',
                    'pr_url': 'https://github.com/x/y/pull/700'}]
        counts = h.classify_false_kills(firings, state_fn=lambda url: 'merged')
        self.assertEqual(counts['false_kills'], 1)
        self.assertEqual(counts['unknown'], 0)

    def test_open_pr_not_a_false_kill(self):
        firings = [{'task_id': 'pr-700-review',
                    'pr_url': 'https://github.com/x/y/pull/700'}]
        counts = h.classify_false_kills(firings, state_fn=lambda url: 'open')
        self.assertEqual(counts['false_kills'], 0)
        self.assertEqual(counts['not_merged'], 1)

    def test_gh_failure_is_unknown_not_false_kill(self):
        firings = [{'task_id': 'pr-700-review',
                    'pr_url': 'https://github.com/x/y/pull/700'}]
        counts = h.classify_false_kills(firings, state_fn=lambda url: 'unknown')
        self.assertEqual(counts['false_kills'], 0)
        self.assertEqual(counts['unknown'], 1)

    def test_missing_pr_url_is_unknown(self):
        firings = [{'task_id': 'pr-700-review', 'pr_url': None}]
        # state_fn must NOT be called for a None url.
        def _boom(url):
            raise AssertionError('state_fn called for a None pr_url')
        counts = h.classify_false_kills(firings, state_fn=_boom)
        self.assertEqual(counts['unknown'], 1)

    def test_gh_pr_state_degrades_on_subprocess_error(self):
        with mock.patch.object(h.subprocess, 'run',
                               side_effect=OSError('no gh')):
            self.assertEqual(
                h.gh_pr_state('https://github.com/x/y/pull/1'), 'unknown')

    def test_gh_pr_state_merged_via_mergedAt(self):
        fake = mock.Mock(returncode=0,
                         stdout=json.dumps({'state': 'OPEN',
                                            'mergedAt': '2026-06-01T00:00:00Z'}),
                         stderr='')
        with mock.patch.object(h.subprocess, 'run', return_value=fake):
            self.assertEqual(
                h.gh_pr_state('https://github.com/x/y/pull/1'), 'merged')


class DigestRoutingTest(unittest.TestCase):
    """The digest is emitted route='digest' (non-escalating) — it can NEVER
    page Larry. Asserted by capturing the append_alert kwargs."""

    def test_emit_digest_routes_non_escalating(self):
        captured = {}

        class _StubLA:
            @staticmethod
            def append_alert(**kwargs):
                captured.update(kwargs)
                return True

        with mock.patch.dict(sys.modules, {'larry_alerts': _StubLA}):
            ok = h.emit_digest('body')
        self.assertTrue(ok)
        self.assertEqual(captured['source'], 'review-ceiling-fit')
        self.assertNotEqual(captured['route'], 'escalate')
        self.assertEqual(captured['route'], 'digest')

    def test_emit_digest_never_raises_on_import_failure(self):
        # A broken larry_alerts must degrade to False, not crash the healer.
        class _Boom:
            @staticmethod
            def append_alert(**kwargs):
                raise RuntimeError('boom')

        with mock.patch.dict(sys.modules, {'larry_alerts': _Boom}):
            self.assertFalse(h.emit_digest('body'))


class DigestBodyTest(unittest.TestCase):
    """The digest body carries distribution + headroom + firing + false-kill +
    recommendation; an empty window still emits a terse OK line."""

    def test_empty_window_terse_ok(self):
        dist = h.duration_distribution([])
        rec = h.compute_recommendation(dist, 2100, 0, 0, _rules())
        body = h.build_digest(dist, 2100, [], {'false_kills': 0, 'unknown': 0,
                                               'not_merged': 0}, rec, 30)
        self.assertIn('OK', body)
        self.assertIn('no Mirror PR-review samples', body)

    def test_notable_body_has_all_sections(self):
        durs = [1900.0] * 100
        dist = h.duration_distribution(durs)
        firings = [{'task_id': 'pr-1-review', 'pr_url': 'u'}]
        counts = {'false_kills': 1, 'unknown': 0, 'not_merged': 0}
        rec = h.compute_recommendation(dist, 2100, 1, 1, _rules())
        body = h.build_digest(dist, 2100, firings, counts, rec, 30)
        self.assertIn('Durations:', body)
        self.assertIn('Headroom:', body)
        self.assertIn('Firings:', body)
        self.assertIn('Recommendation:', body)
        self.assertIn('FALSE_KILL', body)


class ConfigTest(unittest.TestCase):
    """Rules load with defaults on a missing/malformed file."""

    def test_missing_file_uses_defaults(self):
        with mock.patch.object(h, 'RULES_FILE', Path('/nonexistent/x.json')):
            rules = h.load_rules()
        self.assertEqual(rules['window_days'], h.DEFAULT_WINDOW_DAYS)
        self.assertEqual(rules['headroom_low_ratio'], h.DEFAULT_HEADROOM_LOW_RATIO)
        self.assertTrue(rules['enabled'])

    def test_get_current_ceiling_reads_env(self):
        with mock.patch.dict(h.os.environ,
                             {'OL_REVIEW_SESSION_CEILING_SECONDS': '1800'}):
            self.assertEqual(h.get_current_ceiling(), 1800)

    def test_get_current_ceiling_default_when_unset(self):
        env = dict(h.os.environ)
        env.pop('OL_REVIEW_SESSION_CEILING_SECONDS', None)
        with mock.patch.dict(h.os.environ, env, clear=True):
            self.assertEqual(h.get_current_ceiling(), h.DEFAULT_CEILING_SECONDS)


if __name__ == '__main__':
    unittest.main()
