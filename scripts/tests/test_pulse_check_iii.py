#!/usr/bin/env python3
"""Tests for pulse_check_iii (E4.4d PR-B).

Covers:
  - compute_percentile (linear interpolation correctness)
  - bucket_durations + compute_bucket_stats (grouping per (agent, task_type))
  - current_threshold_for_bucket (Mirror task_type override path + default)
  - propose_threshold (sample-size floor, no-change skip, bounded-delta flag)
  - detect_rollback_signal + annotate_rollbacks (window + threshold gates)
  - run_check end-to-end on a 30-day synthetic fixture (smoke per spec § 5.10)
  - build_proposal_artifact shape + format_digest readability

Supabase is NEVER touched — every test exercises the pure-function core.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_iii
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_iii as p3  # noqa: E402


class TestComputePercentile(unittest.TestCase):

    def test_known_values(self):
        # 10 evenly spaced values [1..10]; p90 should fall between 9 and 10.
        values = list(range(1, 11))
        self.assertAlmostEqual(p3.compute_percentile(values, 0.90), 9.1,
                               places=4)
        # p99 is between 9.91 and 10.
        self.assertAlmostEqual(p3.compute_percentile(values, 0.99), 9.91,
                               places=4)

    def test_single_value(self):
        self.assertEqual(p3.compute_percentile([42.0], 0.90), 42.0)

    def test_empty(self):
        self.assertEqual(p3.compute_percentile([], 0.90), 0.0)

    def test_p50_matches_median(self):
        values = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0, 5.0, 3.0]
        # median of these is 3.5
        self.assertAlmostEqual(p3.compute_percentile(values, 0.50), 3.5,
                               places=4)


class TestBucketing(unittest.TestCase):

    def _mk_duration(self, agent, task_type, dur):
        return p3.SessionDuration(
            agent=agent, task_type=task_type, duration_sec=dur,
            ts='2026-05-01T00:00:00+00:00',
        )

    def test_groups_per_agent_task_type(self):
        ds = [
            self._mk_duration('mirror', 'feature-development', 120),
            self._mk_duration('mirror', 'feature-development', 200),
            self._mk_duration('mirror', 'doc-only', 30),
            self._mk_duration('forge', '_default', 60),
        ]
        stats = p3.compute_bucket_stats(ds)
        keyed = {(s.agent, s.task_type): s for s in stats}
        self.assertEqual(keyed[('mirror', 'feature-development')].sample_size, 2)
        self.assertEqual(keyed[('mirror', 'doc-only')].sample_size, 1)
        self.assertEqual(keyed[('forge', '_default')].sample_size, 1)


class TestCurrentThresholdLookup(unittest.TestCase):

    CONFIG = {
        'session_duration_seconds_default': 900,
        'mirror_review_overrides_seconds': {
            'doc-only': 900,
            'bug-investigation': 1500,
            'feature-development': 2100,
            '_default': 1500,
        },
        'forge_overrides_seconds': {
            '_default': 900,
            'feature-development': 1800,
        },
        'beacon_overrides_seconds': {
            '_default': 900,
        },
        'pulse_overrides_seconds': {
            '_default': 900,
        },
    }

    def test_mirror_specific(self):
        self.assertEqual(
            p3.current_threshold_for_bucket(
                self.CONFIG, 'mirror', 'feature-development'),
            2100,
        )

    def test_mirror_default_override(self):
        self.assertEqual(
            p3.current_threshold_for_bucket(
                self.CONFIG, 'mirror', 'unknown-task-type'),
            1500,
        )

    def test_forge_specific(self):
        self.assertEqual(
            p3.current_threshold_for_bucket(
                self.CONFIG, 'forge', 'feature-development'),
            1800,
        )

    def test_forge_default_override(self):
        self.assertEqual(
            p3.current_threshold_for_bucket(
                self.CONFIG, 'forge', 'unknown-task-type'),
            900,
        )

    def test_non_mirror_non_forge_falls_back_to_default(self):
        self.assertEqual(
            p3.current_threshold_for_bucket(
                self.CONFIG, 'pulse', 'feature-development'),
            900,
        )

    def test_forge_without_override_block_falls_back_to_default(self):
        config = {'session_duration_seconds_default': 900}
        self.assertEqual(
            p3.current_threshold_for_bucket(
                config, 'forge', 'feature-development'),
            900,
        )

    def test_empty_config_returns_none(self):
        self.assertIsNone(
            p3.current_threshold_for_bucket({}, 'forge', '_default'))

    def test_beacon_default_override_returns_beacon_value(self):
        config = {
            'session_duration_seconds_default': 900,
            'beacon_overrides_seconds': {'_default': 2147},
        }
        self.assertEqual(
            p3.current_threshold_for_bucket(config, 'beacon', '_default'),
            2147,
        )

    def test_beacon_task_type_override_returns_that_value(self):
        config = {
            'session_duration_seconds_default': 900,
            'beacon_overrides_seconds': {'feature-development': 1800},
        }
        self.assertEqual(
            p3.current_threshold_for_bucket(
                config, 'beacon', 'feature-development'),
            1800,
        )

    def test_beacon_fallback_to_default_when_no_override(self):
        config = {'session_duration_seconds_default': 900}
        self.assertEqual(
            p3.current_threshold_for_bucket(
                config, 'beacon', 'feature-development'),
            900,
        )

    def test_pulse_default_override_returns_pulse_value(self):
        config = {
            'session_duration_seconds_default': 900,
            'pulse_overrides_seconds': {'_default': 262},
        }
        self.assertEqual(
            p3.current_threshold_for_bucket(config, 'pulse', '_default'),
            262,
        )

    def test_pulse_task_type_override_returns_that_value(self):
        config = {
            'session_duration_seconds_default': 900,
            'pulse_overrides_seconds': {'feature-development': 1800},
        }
        self.assertEqual(
            p3.current_threshold_for_bucket(
                config, 'pulse', 'feature-development'),
            1800,
        )

    def test_pulse_fallback_to_default_when_no_override(self):
        config = {'session_duration_seconds_default': 900}
        self.assertEqual(
            p3.current_threshold_for_bucket(
                config, 'pulse', 'feature-development'),
            900,
        )


class TestProposeThreshold(unittest.TestCase):

    def test_below_sample_floor_returns_none(self):
        stats = p3.BucketStats(
            agent='forge', task_type='_default', sample_size=3,
            median_sec=100, p90_sec=180, p99_sec=240,
        )
        self.assertIsNone(p3.propose_threshold(stats, {}))

    def test_at_sample_floor_proposes(self):
        stats = p3.BucketStats(
            agent='forge', task_type='_default', sample_size=10,
            median_sec=100, p90_sec=180, p99_sec=240,
        )
        prop = p3.propose_threshold(stats, {})
        self.assertIsNotNone(prop)
        self.assertEqual(prop.proposed_threshold_sec, 180)
        self.assertIsNone(prop.current_threshold_sec)

    def test_bounded_delta_flag_fires_above_50pct(self):
        # Current=200, p90=400 → delta=100% > 50% bound → high_attention.
        stats = p3.BucketStats(
            agent='forge', task_type='_default', sample_size=20,
            median_sec=180, p90_sec=400, p99_sec=520,
        )
        config = {'session_duration_seconds_default': 200}
        prop = p3.propose_threshold(stats, config)
        self.assertTrue(prop.high_attention)
        self.assertIn('regime-change-suspected', prop.rationale)

    def test_small_delta_does_not_flag(self):
        # Current=200, p90=220 → delta=10% < 50%.
        stats = p3.BucketStats(
            agent='forge', task_type='_default', sample_size=20,
            median_sec=180, p90_sec=220, p99_sec=280,
        )
        config = {'session_duration_seconds_default': 200}
        prop = p3.propose_threshold(stats, config)
        self.assertFalse(prop.high_attention)
        self.assertIn('loosen', prop.rationale)


class TestRollbackDetection(unittest.TestCase):

    def test_rollback_when_tightened_and_false_positives(self):
        applied_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        last = {
            'tightening': True,
            'applied_at': applied_at,
            'prior_threshold_sec': 900,
        }
        self.assertTrue(p3.detect_rollback_signal(
            last_applied_threshold=last,
            recent_false_positives=5,
        ))

    def test_no_rollback_when_no_history(self):
        self.assertFalse(p3.detect_rollback_signal(
            last_applied_threshold=None,
            recent_false_positives=10,
        ))

    def test_no_rollback_when_loosening(self):
        applied_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        last = {'tightening': False, 'applied_at': applied_at,
                'prior_threshold_sec': 900}
        self.assertFalse(p3.detect_rollback_signal(
            last_applied_threshold=last,
            recent_false_positives=10,
        ))

    def test_no_rollback_outside_window(self):
        applied_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        last = {'tightening': True, 'applied_at': applied_at,
                'prior_threshold_sec': 900}
        self.assertFalse(p3.detect_rollback_signal(
            last_applied_threshold=last,
            recent_false_positives=10,
        ))

    def test_no_rollback_when_below_fp_threshold(self):
        applied_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        last = {'tightening': True, 'applied_at': applied_at,
                'prior_threshold_sec': 900}
        self.assertFalse(p3.detect_rollback_signal(
            last_applied_threshold=last,
            recent_false_positives=2,
        ))

    def test_annotate_rollbacks_rewrites_proposed_to_prior(self):
        prop = p3.Proposal(
            bucket='(mirror, feature-development)',
            agent='mirror', task_type='feature-development',
            current_threshold_sec=1500,
            proposed_threshold_sec=1300,
            sample_size=15, p90_sec=1300, p99_sec=1700, median_sec=900,
            rationale='base',
        )
        applied_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        history = {
            'mirror:feature-development': {
                'tightening': True,
                'applied_at': applied_at,
                'prior_threshold_sec': 2100,
            },
        }
        p3.annotate_rollbacks(
            [prop],
            apply_history=history,
            false_positive_counts={'mirror:feature-development': 5},
        )
        self.assertTrue(prop.rollback)
        self.assertEqual(prop.proposed_threshold_sec, 2100)
        self.assertIn('rollback', prop.rationale)


class TestRunCheckSyntheticFixture(unittest.TestCase):
    """End-to-end smoke per spec § 5.10 acceptance: 30-day synthetic
    chain_events fixture → plausible proposals.json."""

    def _synthesize(self):
        random.seed(42)
        out = []
        # Mirror feature-development: ~1700s baseline.
        for _ in range(50):
            out.append(p3.SessionDuration(
                agent='mirror', task_type='feature-development',
                duration_sec=random.gauss(1700, 250),
                ts='2026-05-10T00:00:00+00:00',
            ))
        # Mirror doc-only: ~600s baseline.
        for _ in range(30):
            out.append(p3.SessionDuration(
                agent='mirror', task_type='doc-only',
                duration_sec=random.gauss(600, 100),
                ts='2026-05-10T00:00:00+00:00',
            ))
        # Forge feature-development: ~200s baseline.
        for _ in range(60):
            out.append(p3.SessionDuration(
                agent='forge', task_type='feature-development',
                duration_sec=random.gauss(200, 50),
                ts='2026-05-10T00:00:00+00:00',
            ))
        # Pulse cycle: ~30s. Too few samples → should be skipped.
        for _ in range(5):
            out.append(p3.SessionDuration(
                agent='pulse', task_type='_default',
                duration_sec=random.gauss(30, 5),
                ts='2026-05-10T00:00:00+00:00',
            ))
        return out

    def test_produces_proposals_for_well_sampled_buckets(self):
        config = {
            'session_duration_seconds_default': 900,
            'mirror_review_overrides_seconds': {
                'feature-development': 2100,
                'doc-only': 900,
                '_default': 1500,
            },
        }
        durations = self._synthesize()
        artifact = p3.run_check(durations=durations, config=config)
        # All three well-sampled buckets should be present.
        buckets = {p['bucket'] for p in artifact['proposals']}
        self.assertIn('(mirror, feature-development)', buckets)
        self.assertIn('(mirror, doc-only)', buckets)
        self.assertIn('(forge, feature-development)', buckets)
        # Pulse bucket excluded (only 5 samples < floor).
        self.assertNotIn('(pulse, _default)', buckets)

    def test_artifact_has_expected_shape(self):
        config = {'session_duration_seconds_default': 900}
        artifact = p3.run_check(durations=self._synthesize(), config=config)
        self.assertIn('as_of', artifact)
        self.assertEqual(artifact['lookback_days'], p3.LOOKBACK_DAYS)
        self.assertEqual(artifact['sample_size_floor'], p3.SAMPLE_SIZE_FLOOR)
        self.assertEqual(artifact['applied'], False)
        self.assertIsInstance(artifact['proposals'], list)
        for prop in artifact['proposals']:
            self.assertIn('bucket', prop)
            self.assertIn('proposed_threshold_sec', prop)
            self.assertIn('sample_size', prop)
            self.assertIn('rationale', prop)


class TestForgeBranch(unittest.TestCase):
    """End-to-end Forge-bucket Check III behavior (Round E 2026-05-27).

    Mirrors the Mirror-branch formula exactly: raw p90, no margin, no
    rounding, sample-size floor of 10. The only Forge-specific surface
    is the `forge_overrides_seconds[task_type]` lookup added to
    current_threshold_for_bucket.
    """

    FORGE_CONFIG = {
        'session_duration_seconds_default': 900,
        'mirror_review_overrides_seconds': {
            'feature-development': 2100,
            'doc-only': 900,
            '_default': 1500,
        },
        'forge_overrides_seconds': {
            '_default': 900,
            'feature-development': 1800,
        },
    }

    def _mk(self, agent, task_type, dur):
        return p3.SessionDuration(
            agent=agent, task_type=task_type, duration_sec=dur,
            ts='2026-05-15T00:00:00+00:00',
        )

    def _buckets(self, artifact):
        return {p['bucket']: p for p in artifact['proposals']}

    def test_a_forge_within_headroom_no_proposal(self):
        # 30 Forge feature-development events at 1800s — p90 == current.
        durations = [self._mk('forge', 'feature-development', 1800)
                     for _ in range(30)]
        artifact = p3.run_check(
            durations=durations, config=self.FORGE_CONFIG)
        buckets = self._buckets(artifact)
        # No-change skip per run_check: proposed == current → not emitted.
        self.assertNotIn('(forge, feature-development)', buckets)

    def test_b_forge_p90_exceeds_threshold_proposal_emitted(self):
        # 30 Forge feature-development events at 2400s — p90 = 2400 > 1800.
        durations = [self._mk('forge', 'feature-development', 2400)
                     for _ in range(30)]
        artifact = p3.run_check(
            durations=durations, config=self.FORGE_CONFIG)
        buckets = self._buckets(artifact)
        self.assertIn('(forge, feature-development)', buckets)
        prop = buckets['(forge, feature-development)']
        self.assertEqual(prop['agent'], 'forge')
        self.assertEqual(prop['task_type'], 'feature-development')
        self.assertEqual(prop['current_threshold_sec'], 1800)
        self.assertEqual(prop['proposed_threshold_sec'], 2400)
        self.assertEqual(prop['sample_size'], 30)
        # delta_ratio ≈ 0.33 → below 50% bound → not high-attention.
        self.assertFalse(prop['high_attention'])
        self.assertIn('loosen', prop['rationale'])

    def test_c_forge_insufficient_samples_skipped(self):
        # 3 events — below SAMPLE_SIZE_FLOOR (10) → propose_threshold None.
        durations = [self._mk('forge', 'bug-investigation', 3000)
                     for _ in range(3)]
        artifact = p3.run_check(
            durations=durations, config=self.FORGE_CONFIG)
        buckets = self._buckets(artifact)
        self.assertNotIn('(forge, bug-investigation)', buckets)

    def test_d_mixed_mirror_forge_independence(self):
        # 30 Forge feature-development at 2400s (current=1800 → propose 2400).
        # 30 Mirror feature-development at 2100s (current=2100 → no-change skip).
        durations = (
            [self._mk('forge', 'feature-development', 2400) for _ in range(30)]
            + [self._mk('mirror', 'feature-development', 2100)
               for _ in range(30)]
        )
        artifact = p3.run_check(
            durations=durations, config=self.FORGE_CONFIG)
        buckets = self._buckets(artifact)
        self.assertIn('(forge, feature-development)', buckets)
        self.assertNotIn('(mirror, feature-development)', buckets)
        # Sanity: Forge proposal stays tagged forge, doesn't drift to mirror.
        forge_prop = buckets['(forge, feature-development)']
        self.assertEqual(forge_prop['agent'], 'forge')
        self.assertEqual(forge_prop['current_threshold_sec'], 1800)


class TestFormatDigest(unittest.TestCase):

    def test_empty_proposals_message(self):
        artifact = {
            'as_of': '2026-05-26T00:00:00+00:00',
            'proposals': [],
        }
        digest = p3.format_digest(artifact)
        self.assertIn('no proposed threshold changes this cycle', digest)

    def test_proposal_digest_includes_command(self):
        artifact = {
            'as_of': '2026-05-26T00:00:00+00:00',
            'proposals': [
                {
                    'bucket': '(mirror, feature-development)',
                    'current_threshold_sec': 2100,
                    'proposed_threshold_sec': 1800,
                    'sample_size': 25,
                    'high_attention': False,
                    'rollback': False,
                    'rationale': 'tighten 2100s → 1800s',
                },
            ],
        }
        digest = p3.format_digest(artifact)
        self.assertIn('approve threshold-update-2026-05-26', digest)
        self.assertIn('(mirror, feature-development)', digest)


class TestCadenceGate(unittest.TestCase):
    """The 14-day gate (2026-07-07 timer conversion) is anchored on the newest
    archived artifact date — never the liveness heartbeat, which refreshes on
    any clean exit including --dry-run and the heartbeat seeder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = p3.PROPOSALS_HISTORY_DIR
        p3.PROPOSALS_HISTORY_DIR = self.tmp
        self.addCleanup(setattr, p3, 'PROPOSALS_HISTORY_DIR', self._orig)

    def _write_artifact(self, days_ago: int):
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date()
        (self.tmp / f'check-iii-{d.isoformat()}.json').write_text('{}')

    def test_no_artifact_means_run(self):
        self.assertIsNone(p3.days_since_last_artifact())

    def test_age_anchors_on_newest(self):
        self._write_artifact(30)
        self._write_artifact(5)
        age = p3.days_since_last_artifact()
        self.assertIsNotNone(age)
        self.assertLess(abs(age - 5), 1.5)

    def test_unparseable_names_ignored(self):
        (self.tmp / 'check-iii-not-a-date.json').write_text('{}')
        self.assertIsNone(p3.days_since_last_artifact())

    def test_main_skips_inside_cadence(self):
        # Fresh artifact => gate skips before any Supabase / config access.
        self._write_artifact(5)
        self.assertEqual(p3.main([]), 0)

    def test_main_runs_when_due_and_force_bypasses(self):
        # 14-day-old artifact => gate passes; the run proceeds past the gate
        # (with no fixture it hits the Supabase fetch guard => rc 1, which
        # proves the gate did NOT short-circuit). --force with a fresh
        # artifact must do the same.
        self._write_artifact(14)
        self.assertEqual(p3.main([]), 1)
        self._write_artifact(2)
        self.assertEqual(p3.main(['--force']), 1)


if __name__ == '__main__':
    unittest.main()
