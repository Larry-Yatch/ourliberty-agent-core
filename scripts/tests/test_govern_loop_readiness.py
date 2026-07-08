"""Tests for scripts/govern_loop_readiness.py.

Coverage (slice-7 kickoff trip-wire):
- measure: the three thresholds (decisions / joined keys / span), empty-key
  rows excluded from both counts, build_outcome rows never counted as
  decisions, legacy rows without `kind` count as decisions.
- read_ledger_rows: bad JSONL lines skipped, missing file -> [].
- run_once state machine: not-ready -> progress recorded, NO alert; ready ->
  alert once + last_nudge_ts stamped; within RENUDGE_SEC -> silent; past
  RENUDGE_SEC -> re-nudge (park-don't-decay); kicked stamp -> permanently
  silent even when ready; alert failure/suppression -> no stamp, retried
  next pass; alert fn raising -> caught, never crashes the pass.
- fail-safe: malformed ledger + malformed state file never raise.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import govern_loop_readiness as gr  # noqa: E402

NOW = datetime(2026, 7, 22, 18, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _decision(key: str, days_ago: float = 0) -> dict:
    return {'ts': _iso(NOW - timedelta(days=days_ago)), 'kind': 'decision',
            'decision_key': key, 'outcome': 'approved', 'actor': 'x'}


def _outcome(key: str, days_ago: float = 0) -> dict:
    return {'ts': _iso(NOW - timedelta(days=days_ago)),
            'kind': 'build_outcome', 'decision_key': key,
            'build_outcome': 'merged', 'actor': 'reconcile'}


def _ready_rows() -> list[dict]:
    rows = [_decision(f'k{i}', days_ago=15) for i in range(gr.MIN_DECISIONS)]
    rows += [_outcome(f'k{i}', days_ago=1) for i in range(gr.MIN_JOINED)]
    return rows


class MeasureTest(unittest.TestCase):
    def test_ready_when_all_three_met(self):
        m = gr.measure(_ready_rows(), now_ts=NOW.timestamp())
        self.assertTrue(m['ready'])
        self.assertEqual(m['decisions'], gr.MIN_DECISIONS)
        self.assertEqual(m['joined'], gr.MIN_JOINED)
        self.assertGreaterEqual(m['span_days'], gr.MIN_SPAN_DAYS)

    def test_each_threshold_gates(self):
        rows = _ready_rows()
        short = [dict(r, ts=_iso(NOW - timedelta(days=2))) for r in rows]
        cases = [
            rows[1:],                                   # one decision short
            rows[:-1],                                  # one joined key short
            short,                                      # span too short
        ]
        for rows_case in cases:
            m = gr.measure(rows_case, now_ts=NOW.timestamp())
            self.assertFalse(m['ready'])

    def test_empty_key_and_legacy_rows(self):
        rows = [
            _decision('', days_ago=1),                       # excluded
            {'ts': _iso(NOW), 'decision_key': 'legacy-1',
             'outcome': 'approved'},                         # no kind -> decision
            dict(_outcome('', days_ago=1)),                  # empty-key join excl.
            _outcome('legacy-1'),
        ]
        m = gr.measure(rows, now_ts=NOW.timestamp())
        self.assertEqual(m['decisions'], 1)
        self.assertEqual(m['joined'], 1)

    def test_duplicate_outcome_rows_count_one_key(self):
        rows = [_outcome('k1', days_ago=2), _outcome('k1', days_ago=1)]
        self.assertEqual(gr.measure(rows, now_ts=NOW.timestamp())['joined'], 1)


class SandboxedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Restore, never pop: _bootstrap set the process-wide sandbox root.
        self._env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(gr)
        (self.tmp / 'state').mkdir(parents=True)
        self.calls: list[dict] = []

    def tearDown(self):
        if self._env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._env_orig
        shutil.rmtree(self.tmp, ignore_errors=True)
        importlib.reload(gr)

    def _alert(self, **kw):
        self.calls.append(kw)
        return True

    def _write_ledger(self, rows: list[dict]) -> None:
        text = '\n'.join(json.dumps(r) for r in rows)
        (self.tmp / 'state' / 'decision-outcome-ledger.jsonl').write_text(text)

    def _state(self) -> dict:
        return json.loads(
            (self.tmp / 'state' / 'govern-loop-kickoff.json').read_text())


class ReadLedgerTest(SandboxedTest):
    def test_missing_file_and_bad_lines(self):
        self.assertEqual(gr.read_ledger_rows(), [])
        (self.tmp / 'state' / 'decision-outcome-ledger.jsonl').write_text(
            'not json\n' + json.dumps(_decision('k1')) + '\n[1,2]\n')
        self.assertEqual(len(gr.read_ledger_rows()), 1)


class RunOnceTest(SandboxedTest):
    def test_not_ready_records_progress_no_alert(self):
        self._write_ledger([_decision('k1', days_ago=1)])
        state = gr.run_once(now=NOW, alert_fn=self._alert)
        self.assertEqual(self.calls, [])
        self.assertFalse(state['progress']['ready'])
        self.assertFalse(self._state()['progress']['ready'])

    def test_ready_nudges_once_then_waits_a_week(self):
        self._write_ledger(_ready_rows())
        gr.run_once(now=NOW, alert_fn=self._alert)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['route'], 'escalate')
        self.assertTrue(self.calls[0]['needs_larry'])
        self.assertIn('kick_govern_loop_assessor.sh', self.calls[0]['message'])
        # next day: silent
        gr.run_once(now=NOW + timedelta(days=1), alert_fn=self._alert)
        self.assertEqual(len(self.calls), 1)
        # day 8: re-nudge (park-don't-decay)
        gr.run_once(now=NOW + timedelta(days=8), alert_fn=self._alert)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._state()['nudge_count'], 2)

    def test_kicked_is_permanently_silent(self):
        (self.tmp / 'state' / 'govern-loop-kickoff.json').write_text(
            json.dumps({'kicked': _iso(NOW)}))
        self._write_ledger(_ready_rows())
        gr.run_once(now=NOW + timedelta(days=30), alert_fn=self._alert)
        self.assertEqual(self.calls, [])

    def test_suppressed_alert_not_stamped_and_retried(self):
        self._write_ledger(_ready_rows())
        gr.run_once(now=NOW, alert_fn=lambda **kw: False)  # cooldown
        self.assertNotIn('last_nudge_ts', self._state())
        gr.run_once(now=NOW + timedelta(days=1), alert_fn=self._alert)
        self.assertEqual(len(self.calls), 1)  # retried next pass

    def test_raising_alert_fn_never_crashes(self):
        self._write_ledger(_ready_rows())

        def boom(**kw):
            raise RuntimeError('bot down')

        state = gr.run_once(now=NOW, alert_fn=boom)
        self.assertTrue(state['progress']['ready'])
        self.assertNotIn('last_nudge_ts', self._state())

    def test_concurrent_kick_stamp_is_never_erased(self):
        # The race the review confirmed: watcher reads state (no stamp), the
        # kick script stamps `kicked`, watcher writes back. The write-side
        # re-read must preserve the stamp — else nudges resume after a kick.
        self._write_ledger(_ready_rows())
        state_path = self.tmp / 'state' / 'govern-loop-kickoff.json'

        def stamping_alert(**kw):
            # Fires mid-run_once, after the watcher's initial read: simulates
            # the kick landing inside the read-modify-write window.
            state_path.write_text(
                json.dumps({'kicked': _iso(NOW), 'kicked_by': 'test'}))
            return True

        gr.run_once(now=NOW, alert_fn=stamping_alert)
        self.assertEqual(self._state()['kicked'], _iso(NOW))
        # and the preserved stamp silences the next pass
        gr.run_once(now=NOW + timedelta(days=8), alert_fn=self._alert)
        self.assertEqual(self.calls, [])

    def test_malformed_state_file_never_raises(self):
        (self.tmp / 'state' / 'govern-loop-kickoff.json').write_text('{bad')
        self._write_ledger(_ready_rows())
        gr.run_once(now=NOW, alert_fn=self._alert)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
