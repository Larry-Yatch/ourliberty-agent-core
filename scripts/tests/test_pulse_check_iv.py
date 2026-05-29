#!/usr/bin/env python3
"""Tests for pulse_check_iv (PR-β marker-drift enforcement analyzer).

Covers: proposal-firing rule (rate > 2/week over trailing 4w), no-LLM
discipline, sentinel idempotency, atomic-write semantics.

Run::

    cd ~/agent-core && python3 -m pytest scripts/tests/test_pulse_check_iv.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_iv as p4  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)  # Monday


def _event(days_ago: float, suffix: str = 'beacon') -> p4.MarkerEvent:
    return p4.MarkerEvent(
        ts=NOW - timedelta(days=days_ago),
        event_type=f'{p4.EVENT_PREFIX}{suffix}',
        task_id='real-task',
    )


class TestRunCheck(unittest.TestCase):

    def test_no_events_returns_none(self):
        result = p4.run_check(events=[], now=NOW)
        self.assertEqual(result.rule_fired, 'none')
        self.assertEqual(result.event_count, 0)
        self.assertEqual(result.rate_per_week, 0.0)

    def test_at_threshold_does_not_fire(self):
        # 4w / 28d window, floor 2/week => 8 events at the floor.
        # Strict >, so 8 events (== 2.0/week) must NOT fire.
        events = [_event(days_ago=i * 3.0) for i in range(8)]
        result = p4.run_check(events=events, now=NOW)
        self.assertEqual(result.rule_fired, 'none')
        self.assertAlmostEqual(result.rate_per_week, 2.0, places=2)

    def test_above_threshold_fires_propose(self):
        # 9 events in 28d => 9/4 = 2.25/week > 2.
        events = [_event(days_ago=i * 3.0) for i in range(9)]
        result = p4.run_check(events=events, now=NOW)
        self.assertEqual(result.rule_fired, 'propose')
        self.assertEqual(result.event_count, 9)

    def test_old_events_outside_window_dropped(self):
        events = [_event(days_ago=i * 3.0) for i in range(9)]
        # Add 10 old ones from 6 months ago — should NOT pull rate up.
        events.extend([_event(days_ago=180.0 + i, suffix='old')
                       for i in range(10)])
        result = p4.run_check(events=events, now=NOW)
        self.assertEqual(result.event_count, 9)


class TestArtifactShape(unittest.TestCase):

    def test_build_artifact_contains_required_keys(self):
        result = p4.run_check(
            events=[_event(days_ago=i) for i in range(10)], now=NOW,
        )
        artifact = p4.build_artifact(result)
        for key in ('as_of', 'week_anchor', 'check', 'rule_fired',
                    'event_count', 'rate_per_week', 'lookback_days',
                    'examples', 'rationale', 'applied'):
            self.assertIn(key, artifact)
        self.assertEqual(artifact['check'], 'IV')
        self.assertFalse(artifact['applied'])


class TestSentinelIdempotency(unittest.TestCase):
    """Sentinel-cum-artifact: the artifact file gates re-execution within
    the same ISO week. main() bails early when the file is present."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prior_root = None
        import os
        self._prior_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        # We poke the module-level constants too — analyzers cache them
        # at import (matches pulse_check_viii.py test pattern).
        self._orig_dir = p4.PROPOSALS_DIR
        p4.PROPOSALS_DIR = self.tmp / 'blackboard' / 'pulse-check-iv-proposals'

    def tearDown(self):
        import os
        p4.PROPOSALS_DIR = self._orig_dir
        if self._prior_root is not None:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prior_root
        else:
            del os.environ['OURLIBERTY_AGENTS_ROOT']
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_path_for_week_uses_iso_monday(self):
        week = p4.iso_week_monday(NOW.date())
        self.assertEqual(week, '2026-05-25')
        path = p4.artifact_path_for_week(week)
        self.assertTrue(str(path).endswith(f'check-iv-{week}.json'))

    def test_write_artifact_is_atomic(self):
        result = p4.run_check(
            events=[_event(days_ago=i) for i in range(10)], now=NOW,
        )
        artifact = p4.build_artifact(result)
        path = p4.write_artifact(artifact)
        # No .tmp left over.
        leftovers = list(path.parent.glob('*.tmp'))
        self.assertEqual(leftovers, [])


class TestNoLLMCalls(unittest.TestCase):
    """Self-protection check (Mirror review focus #3): the analyzer must
    not import any LLM client."""

    def test_module_does_not_import_anthropic_or_openai(self):
        src = (_REPO_SCRIPTS / 'pulse_check_iv.py').read_text()
        for forbidden in ('import anthropic', 'from anthropic',
                          'import openai', 'from openai',
                          'subprocess.*claude'):
            self.assertNotIn(forbidden, src,
                             f'{forbidden!r} must not appear in '
                             'pulse_check_iv.py (Mirror focus #3)')


class TestFixturePath(unittest.TestCase):

    def test_fixture_load_round_trip(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                         delete=False) as fh:
            json.dump({
                'events': [
                    {'ts': NOW.isoformat(),
                     'event_type': 'mirror_marker_invisible:forge',
                     'task_id': 'real-001'},
                ],
            }, fh)
            path = fh.name
        try:
            # Direct fixture-shape parse via the constructor path the
            # CLI uses.
            with open(path) as fh:
                raw = json.load(fh)
            events = [
                p4.MarkerEvent(
                    ts=p4._parse_ts(e['ts']),
                    event_type=e['event_type'],
                    task_id=e.get('task_id', ''),
                )
                for e in raw['events']
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type,
                             'mirror_marker_invisible:forge')
        finally:
            Path(path).unlink()


if __name__ == '__main__':
    unittest.main()
