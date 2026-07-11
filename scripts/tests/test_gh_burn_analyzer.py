#!/usr/bin/env python3
"""Tests for gh_burn_analyzer (gh-api-burn phase 1, Part D).

Covers (mirrors the pulse_check_iii/viii artifact+approval conventions):
  - analyze: <24h of samples -> insufficient_data, should_emit False.
  - analyze: >=24h with exhaustion -> exhaustion_observed, should_emit True,
    peak/exhausted-hours/headroom computed.
  - analyze: >=24h healthy (no exhaustion) -> healthy, should_emit False.
  - run: <24h -> writes artifact, emits NO ping.
  - run: >=24h exhaustion -> writes artifact + emits exactly one approval_request
    (target_agent beacon), artifact emitted True.
  - run: re-run same day / a prior emitted artifact present -> idempotent no-op
    (emitted flag gates the ping).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_gh_burn_analyzer
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import gh_burn_analyzer as analyzer  # noqa: E402

_BASE = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def _samples(hours, floors):
    """Two samples per hour bucket (a full reading + the hour's floor). Each hour
    gets a distinct reset epoch so buckets group by reset, exactly like a live
    GraphQL rate-limit window."""
    out = []
    for h in range(hours):
        reset = 1_700_000_000 + h * 3600
        hour_ts = _BASE + timedelta(hours=h)
        floor = floors[h]
        for rem, minute in ((5000, 0), (floor, 30)):
            out.append({
                'ts': (hour_ts + timedelta(minutes=minute)).isoformat(),
                'graphql_remaining': rem,
                'graphql_limit': 5000,
                'graphql_reset': reset,
            })
    return out


class AnalyzeTests(unittest.TestCase):
    def test_insufficient_data_under_24h(self):
        art = analyzer.analyze(_samples(3, [3000, 3000, 3000]), now=_NOW)
        self.assertFalse(art['enough_data'])
        self.assertEqual(art['finding'], 'insufficient_data')
        self.assertFalse(art['should_emit'])

    def test_exhaustion_over_24h(self):
        floors = [3000] * 26
        for h in (5, 6, 7, 8):  # four hours hit the floor
            floors[h] = 10
        art = analyzer.analyze(_samples(26, floors), now=_NOW)
        self.assertTrue(art['enough_data'])
        self.assertEqual(art['finding'], 'exhaustion_observed')
        self.assertTrue(art['should_emit'])
        self.assertEqual(art['exhausted_hours'], 4)
        self.assertEqual(art['peak_consumed_per_hour'], 4990)  # 5000 - 10
        self.assertEqual(art['total_hours_observed'], 26)
        self.assertEqual(art['graphql_ceiling'], 5000)

    def test_healthy_over_24h(self):
        art = analyzer.analyze(_samples(26, [3000] * 26), now=_NOW)
        self.assertTrue(art['enough_data'])
        self.assertEqual(art['finding'], 'healthy')
        self.assertFalse(art['should_emit'])
        self.assertEqual(art['exhausted_hours'], 0)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.hist = Path(tempfile.mkdtemp(prefix='gh-burn-analysis-test-'))

    def _artifact_path(self):
        return self.hist / f'gh-burn-{_NOW.date().isoformat()}.json'

    def test_insufficient_writes_artifact_no_ping(self):
        emit = mock.Mock(return_value=True)
        art = analyzer.run(samples=_samples(3, [3000, 3000, 3000]),
                           now=_NOW, emit_approval=emit, history_dir=self.hist)
        emit.assert_not_called()
        self.assertFalse(art['emitted'])
        self.assertEqual(art['finding'], 'insufficient_data')
        self.assertTrue(self._artifact_path().exists())

    def test_healthy_writes_artifact_no_ping(self):
        emit = mock.Mock(return_value=True)
        analyzer.run(samples=_samples(26, [3000] * 26),
                     now=_NOW, emit_approval=emit, history_dir=self.hist)
        emit.assert_not_called()

    def test_exhaustion_emits_exactly_one_ping(self):
        floors = [3000] * 26
        for h in (5, 6, 7):
            floors[h] = 10
        emit = mock.Mock(return_value=True)
        art = analyzer.run(samples=_samples(26, floors),
                           now=_NOW, emit_approval=emit, history_dir=self.hist)
        emit.assert_called_once()
        payload = emit.call_args.args[0]
        self.assertEqual(payload['target_agent'], 'beacon')
        self.assertEqual(payload['task_id'], analyzer.PHASE2_TASK_ID)
        self.assertIn('phase-2', payload['summary'])
        self.assertTrue(art['emitted'])
        # Artifact persisted the emitted flag for the idempotency gate.
        saved = json.loads(self._artifact_path().read_text())
        self.assertTrue(saved['emitted'])

    def test_rerun_same_day_is_idempotent(self):
        floors = [3000] * 26
        for h in (5, 6, 7):
            floors[h] = 10
        emit = mock.Mock(return_value=True)
        samples = _samples(26, floors)
        analyzer.run(samples=samples, now=_NOW, emit_approval=emit,
                     history_dir=self.hist)
        # Second daily fire, same data + same day: the emitted flag must gate it.
        art2 = analyzer.run(samples=samples, now=_NOW, emit_approval=emit,
                            history_dir=self.hist)
        emit.assert_called_once()  # still exactly one across both runs
        self.assertFalse(art2['should_emit'])
        # The same-day artifact was NOT downgraded away from emitted=True.
        saved = json.loads(self._artifact_path().read_text())
        self.assertTrue(saved['emitted'])

    def test_prior_emitted_artifact_blocks_new_ping(self):
        # A prior day already pinged; today shows exhaustion again -> no re-ping.
        prior = self.hist / 'gh-burn-2026-07-08.json'
        prior.write_text(json.dumps({'emitted': True, 'finding':
                                     'exhaustion_observed'}))
        floors = [3000] * 26
        for h in (5, 6, 7):
            floors[h] = 10
        emit = mock.Mock(return_value=True)
        art = analyzer.run(samples=_samples(26, floors), now=_NOW,
                           emit_approval=emit, history_dir=self.hist)
        emit.assert_not_called()
        self.assertFalse(art['emitted'])


class DefaultEmitChatIdTests(unittest.TestCase):
    """chat_id resolution in _default_emit (#812 null-chat-at-creation fix):
    LARRY_CHAT_ID wins when a valid non-zero int; otherwise fall back to the
    TELEGRAM_ALLOWED_CHAT_IDS primary rather than registering chat_id=0."""

    def _emit_and_capture(self, env):
        captured = {}

        def fake_add_pending(payload, chat_id, **kw):
            captured['chat_id'] = chat_id

        fake_ah = mock.Mock()
        fake_ah.add_pending = fake_add_pending
        fake_ah.build_approval_request_chain_event = lambda p: {}
        fake_ce = mock.Mock()
        fake_ce.emit_event = lambda **kw: True
        with mock.patch.dict('sys.modules', {'beacon_approval_handler': fake_ah,
                                             'chain_event_emit': fake_ce}), \
                mock.patch.dict('os.environ', env, clear=False):
            analyzer._default_emit({'task_id': 't'})
        return captured

    def test_primary_chat_id_lowest_allowed(self):
        with mock.patch.dict('os.environ',
                             {'TELEGRAM_ALLOWED_CHAT_IDS': '900, 100, 500'}):
            self.assertEqual(analyzer._primary_chat_id(), 100)

    def test_primary_chat_id_none_when_empty(self):
        with mock.patch.dict('os.environ', {'TELEGRAM_ALLOWED_CHAT_IDS': ''}):
            self.assertIsNone(analyzer._primary_chat_id())

    def test_falls_back_to_allowed_when_larry_unset(self):
        env = {'TELEGRAM_ALLOWED_CHAT_IDS': '7998341473'}
        env['LARRY_CHAT_ID'] = ''
        cap = self._emit_and_capture(env)
        self.assertEqual(cap['chat_id'], 7998341473)

    def test_larry_chat_id_takes_precedence(self):
        env = {'LARRY_CHAT_ID': '4242',
               'TELEGRAM_ALLOWED_CHAT_IDS': '7998341473'}
        cap = self._emit_and_capture(env)
        self.assertEqual(cap['chat_id'], 4242)


if __name__ == '__main__':
    unittest.main()
