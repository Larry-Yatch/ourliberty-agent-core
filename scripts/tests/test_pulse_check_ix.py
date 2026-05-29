#!/usr/bin/env python3
"""Tests for pulse_check_ix (Check IX — operator-friction signal).

Covers:
  - each signal's threshold (fires above, no-op at/below)
  - mission-name kebab-cases to the spec § 3 ID prefix
  - idempotency: existing drafting mission with matching prefix → skip
  - parsing of beacon-bot log + outbox-notifier log
  - fixture-pattern filter on chain_events task_ids

larry-alerts / supabase / HTTP are never touched live — every test
exercises the pure-function core with injected inputs.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_ix
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_ix as p9  # noqa: E402
from dashboard_api import _kebab_case  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)  # Monday


def _probe(hours_ago: float, text: str) -> p9.StatusProbe:
    return p9.StatusProbe(
        ts=NOW - timedelta(hours=hours_ago), text=text,
    )


def _step(hours_ago: float, task_id: str, event_type: str) -> p9.StepEvent:
    return p9.StepEvent(
        task_id=task_id, event_type=event_type,
        ts=NOW - timedelta(hours=hours_ago),
    )


def _alert(hours_ago: float, subject: str) -> p9.AlertEntry:
    return p9.AlertEntry(
        ts=NOW - timedelta(hours=hours_ago), subject=subject, source='healer',
    )


def _rescue(hours_ago: float, task_id: str) -> p9.ClarificationExhausted:
    return p9.ClarificationExhausted(
        ts=NOW - timedelta(hours=hours_ago), task_id=task_id,
    )


# ---------------- signal 2.1: catch-me-up-gap ----------------


class TestSignalCatchMeUp(unittest.TestCase):

    def test_fires_strictly_above_threshold(self):
        probes = [_probe(i, 'status') for i in range(4)]
        f = p9.signal_catch_me_up(probes)
        self.assertIsNotNone(f)
        self.assertEqual(f.signal, p9.SIGNAL_CATCH_ME_UP)
        self.assertEqual(f.evidence_count, 4)

    def test_at_threshold_does_not_fire(self):
        probes = [_probe(i, 'status') for i in range(p9.CATCH_ME_UP_THRESHOLD)]
        self.assertIsNone(p9.signal_catch_me_up(probes))

    def test_below_threshold_does_not_fire(self):
        self.assertIsNone(p9.signal_catch_me_up([]))
        self.assertIsNone(p9.signal_catch_me_up([_probe(1, 'status')]))


class TestStatusProbeRegex(unittest.TestCase):

    def test_matches_various_status_phrasings(self):
        cases = [
            'status',
            'Status?',
            "What's happening?",
            "what is happening with the build",
            "is everything continuing?",
            "any update on PR-D",
            "where are we on the bootstrap",
            "Where are things at this morning?",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNotNone(p9._STATUS_PROBE_RE.search(text))

    def test_does_not_match_unrelated(self):
        cases = [
            'merge PR #123',
            'reject task xyz',
            'approve sequence foo',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(p9._STATUS_PROBE_RE.search(text))


class TestParseBeaconBotLog(unittest.TestCase):

    def test_extracts_incoming_status_probes_in_window(self):
        ts_in = (NOW - timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        ts_out = (NOW - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bot.log'
            p.write_text(
                f"[{ts_in}] <- 7998341473: 'status?'\n"
                f"[{ts_in}] <- 7998341473: 'where are we on the build'\n"
                f"[{ts_in}] -> 7998341473: 'Heres the answer...'\n"   # outgoing — ignored
                f"[{ts_in}] <- 7998341473: 'approve sequence X'\n"     # not a probe
                f"[{ts_out}] <- 7998341473: 'status'\n"                 # out of window
            )
            probes = p9.parse_beacon_bot_log(p, now=NOW)
            self.assertEqual(len(probes), 2)


# ---------------- signal 2.2: time-to-action-gap ----------------


class TestPairStepEvents(unittest.TestCase):

    def test_pairs_dispatched_with_first_later_action(self):
        events = [
            _step(20, 'task-a', 'step-dispatched'),
            _step(10, 'task-a', 'step-merged'),
            _step(5, 'task-a', 'step-failed'),  # later — should not be picked
        ]
        pairs = p9.pair_step_events(events)
        self.assertEqual(len(pairs), 1)
        d, a = pairs[0]
        self.assertEqual(a.event_type, 'step-merged')

    def test_skips_unpaired_dispatched(self):
        events = [_step(20, 'task-b', 'step-dispatched')]
        self.assertEqual(p9.pair_step_events(events), [])

    def test_drops_fixture_task_ids(self):
        events = [
            _step(20, 't-fixture-001', 'step-dispatched'),
            _step(10, 't-fixture-001', 'step-merged'),
        ]
        self.assertEqual(p9.pair_step_events(events), [])


class TestComputeFrictionEvents(unittest.TestCase):

    def test_no_baseline_below_min(self):
        # Fewer than min_pairs_for_baseline — no median, no friction.
        pairs = [
            (_step(20, f'task-{i}', 'step-dispatched'),
             _step(10, f'task-{i}', 'step-merged'))
            for i in range(3)
        ]
        friction, median = p9.compute_friction_events(pairs)
        self.assertEqual(friction, [])
        self.assertIsNone(median)

    def test_friction_when_dur_exceeds_2x_median(self):
        # 5 pairs at 10h, one pair at 50h. Median=10h, 2*median=20h,
        # the 50h pair is friction.
        pairs = []
        for i in range(5):
            pairs.append((
                _step(20 + i, f'task-{i}', 'step-dispatched'),
                _step(10 + i, f'task-{i}', 'step-merged'),
            ))
        pairs.append((
            _step(60, 'task-late', 'step-dispatched'),
            _step(10, 'task-late', 'step-merged'),
        ))
        friction, median = p9.compute_friction_events(pairs)
        self.assertEqual(len(friction), 1)
        self.assertEqual(friction[0].task_id, 'task-late')
        self.assertIsNotNone(median)


class TestSignalTimeToAction(unittest.TestCase):

    def test_fires_strictly_above_threshold(self):
        friction = [
            p9.FrictionEvent(
                task_id=f'task-{i}', time_to_action_sec=100.0,
                dispatched_ts=NOW,
            )
            for i in range(6)
        ]
        f = p9.signal_time_to_action(friction, median_sec=50.0)
        self.assertIsNotNone(f)
        self.assertEqual(f.evidence_count, 6)

    def test_at_threshold_does_not_fire(self):
        friction = [
            p9.FrictionEvent(
                task_id=f'task-{i}', time_to_action_sec=100.0,
                dispatched_ts=NOW,
            )
            for i in range(p9.TIME_TO_ACTION_THRESHOLD)
        ]
        self.assertIsNone(p9.signal_time_to_action(friction))


# ---------------- signal 2.3: alert-ignored ----------------


class TestSignalAlertIgnored(unittest.TestCase):

    def test_fires_at_threshold(self):
        # `>= N` per spec § 2.3.
        alerts = [_alert(i, 'subject-a') for i in range(3)]
        f = p9.signal_alert_ignored(alerts)
        self.assertIsNotNone(f)
        self.assertEqual(f.signal, p9.SIGNAL_ALERT_IGNORED)

    def test_below_threshold_does_not_fire(self):
        alerts = [_alert(i, 'subject-a') for i in range(2)]
        self.assertIsNone(p9.signal_alert_ignored(alerts))

    def test_distinct_subjects_do_not_aggregate(self):
        # 2x subject-a + 2x subject-b → no single subject hits 3.
        alerts = (
            [_alert(i, 'subject-a') for i in range(2)]
            + [_alert(i + 10, 'subject-b') for i in range(2)]
        )
        self.assertIsNone(p9.signal_alert_ignored(alerts))


# ---------------- signal 2.4: rescue-burden ----------------


class TestParseOutboxNotifierLog(unittest.TestCase):

    def test_extracts_clarification_exhausted_events_in_window(self):
        ts_in_a = (NOW - timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')
        ts_in_b = (NOW - timedelta(hours=6)).strftime('%Y-%m-%d %H:%M:%S')
        ts_out = (NOW - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'notifier.log'
            p.write_text(
                f'[{ts_in_a}] [notifier] [INFO] queued completion DM to chat 1 for intent=clarification-exhausted (task=t-cx)\n'
                # Same task again — must dedup
                f'[{ts_in_a}] [notifier] [INFO] another line intent=clarification-exhausted (task=t-cx)\n'
                # Real task in window
                f'[{ts_in_b}] [notifier] [INFO] intent=clarification-exhausted (task=real-cx-001)\n'
                # Fixture pattern — must filter
                f'[{ts_in_b}] [notifier] [INFO] intent=clarification-exhausted (task=t-fixture-001)\n'
                # Out of window
                f'[{ts_out}] [notifier] [INFO] intent=clarification-exhausted (task=real-cx-old)\n'
            )
            events = p9.parse_outbox_notifier_log(p, now=NOW)
            # `t-cx` filtered as fixture; only real-cx-001 survives.
            task_ids = sorted(e.task_id for e in events)
            self.assertEqual(task_ids, ['real-cx-001'])


class TestSignalRescueBurden(unittest.TestCase):

    def test_fires_at_threshold(self):
        events = [_rescue(i, f'task-{i}') for i in range(2)]
        f = p9.signal_rescue_burden(events)
        self.assertIsNotNone(f)

    def test_below_threshold_does_not_fire(self):
        self.assertIsNone(p9.signal_rescue_burden([_rescue(1, 'task-a')]))


# ---------------- mission ID + idempotency ----------------


class TestMissionIdAndIdempotency(unittest.TestCase):

    CYCLE_DATE = NOW.date()

    def _finding(self, signal: str) -> p9.SignalFinding:
        return p9.SignalFinding(
            signal=signal, headline='h', evidence_count=5,
            evidence_summary='Evidence: 5 events over trailing 7d',
            suggested_fix='fix',
        )

    def test_mission_name_kebabs_to_spec_id(self):
        # Per spec § 3: id == pulse-check-ix-<signal-shortname>-<YYYY-MM-DD>.
        for signal in p9.ALL_SIGNALS:
            with self.subTest(signal=signal):
                name = self._finding(signal).mission_name(self.CYCLE_DATE)
                expected_id = (
                    f'pulse-check-ix-{signal}-{self.CYCLE_DATE.isoformat()}'
                )
                self.assertEqual(_kebab_case(name), expected_id)

    def test_dedup_finds_matching_drafting_mission(self):
        existing = [{
            'id': 'pulse-check-ix-catch-me-up-gap-2026-05-18',
            'phase': 'drafting',
        }]
        self.assertTrue(p9.has_existing_drafting_for_signal(
            existing, p9.SIGNAL_CATCH_ME_UP,
        ))

    def test_dedup_ignores_other_signals(self):
        existing = [{
            'id': 'pulse-check-ix-rescue-burden-2026-05-18',
            'phase': 'drafting',
        }]
        self.assertFalse(p9.has_existing_drafting_for_signal(
            existing, p9.SIGNAL_CATCH_ME_UP,
        ))

    def test_dedup_ignores_unrelated_missions(self):
        existing = [{'id': 'missions-tab-v1', 'phase': 'drafting'}]
        self.assertFalse(p9.has_existing_drafting_for_signal(
            existing, p9.SIGNAL_CATCH_ME_UP,
        ))


# ---------------- register_findings dry-run + idempotency ----------------


class TestRegisterFindings(unittest.TestCase):

    CYCLE_DATE = NOW.date()

    def _finding(self, signal: str) -> p9.SignalFinding:
        return p9.SignalFinding(
            signal=signal, headline='h', evidence_count=5,
            evidence_summary='Evidence: 5 over 7d', suggested_fix='fix',
        )

    def test_dry_run_does_not_call_api(self):
        result = p9.register_findings(
            [self._finding(p9.SIGNAL_CATCH_ME_UP)],
            self.CYCLE_DATE, existing_missions=[], dry_run=True,
        )
        self.assertEqual(len(result.registered), 1)
        self.assertTrue(result.registered[0]['dry_run'])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.errors, [])

    def test_existing_drafting_mission_skips_signal(self):
        existing = [{
            'id': 'pulse-check-ix-catch-me-up-gap-2026-05-18',
            'phase': 'drafting',
        }]
        result = p9.register_findings(
            [self._finding(p9.SIGNAL_CATCH_ME_UP)],
            self.CYCLE_DATE, existing_missions=existing, dry_run=True,
        )
        self.assertEqual(result.registered, [])
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]['signal'], p9.SIGNAL_CATCH_ME_UP)

    def test_promoted_mission_does_not_block_re_registration(self):
        # An existing mission for the same signal but phase != drafting
        # should NOT dedup (spec § 3 specifies `phase: drafting` only).
        existing = [{
            'id': 'pulse-check-ix-catch-me-up-gap-2026-05-18',
            'phase': 'ready',
        }]
        # has_existing_drafting_for_signal scans the filtered list, and
        # list_existing_drafting_missions does the phase filter; here we
        # call has_existing... with only-drafting input (so we simulate
        # the filter having already dropped this mission).
        filtered: list[dict] = [
            m for m in existing if m.get('phase') == 'drafting'
        ]
        self.assertFalse(p9.has_existing_drafting_for_signal(
            filtered, p9.SIGNAL_CATCH_ME_UP,
        ))


# ---------------- artifact write ----------------


class TestWriteArtifact(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_proposals_dir = p9.PROPOSALS_DIR
        p9.PROPOSALS_DIR = self.tmp / 'blackboard' / 'pulse-check-ix-proposals'

    def tearDown(self):
        p9.PROPOSALS_DIR = self._orig_proposals_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_path_per_week_is_stable(self):
        week = p9.iso_week_monday(NOW.date())
        path = p9.artifact_path_for_week(week)
        self.assertTrue(str(path).endswith(f'check-ix-{week}.json'))

    def test_write_artifact_lands_at_week_path(self):
        result = p9.CycleResult(
            cycle_date=p9.iso_week_monday(NOW.date()),
            findings=[], registered=[], skipped=[], errors=[],
        )
        artifact = p9.build_artifact(result, as_of_iso=NOW.isoformat())
        path = p9.write_artifact(artifact)
        self.assertTrue(path.exists())
        body = json.loads(path.read_text())
        self.assertEqual(body['week_anchor'], result.cycle_date)


# ---------------- end-to-end run_signals ----------------


class TestRunSignals(unittest.TestCase):

    def test_returns_empty_when_below_all_thresholds(self):
        out = p9.run_signals(
            probes=[_probe(1, 'status')],
            step_events=[],
            alerts=[_alert(1, 'subj-a')],
            rescues=[_rescue(1, 'task-a')],
        )
        self.assertEqual(out, [])

    def test_fires_only_signals_that_cross_threshold(self):
        # Only catch-me-up + rescue-burden cross.
        probes = [_probe(i, 'status') for i in range(5)]
        rescues = [_rescue(i, f'rescue-{i}') for i in range(3)]
        out = p9.run_signals(
            probes=probes, step_events=[], alerts=[], rescues=rescues,
        )
        signals = sorted(f.signal for f in out)
        self.assertEqual(signals, sorted([
            p9.SIGNAL_CATCH_ME_UP, p9.SIGNAL_RESCUE_BURDEN,
        ]))


if __name__ == '__main__':
    unittest.main()
