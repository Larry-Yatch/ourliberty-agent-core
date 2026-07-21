#!/usr/bin/env python3
"""test_alert_outcomes.py — the XIV-b alert outcome ledger.

The ledger is the feedback channel that makes alert usefulness measurable:
one row per outcome, keyed to the alert's chain_events event_id. These tests
pin the write contract (never raises, refuses unknown outcomes), the
denormalized analytics columns, the aggregate read-model, and the
`auto_resolved` hook on alert retraction.

Run:
    python3 -m unittest scripts.tests.test_alert_outcomes
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
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import alert_outcomes  # noqa: E402
import larry_alerts  # noqa: E402


class _LedgerTest(unittest.TestCase):
    """Points the ledger at a tempdir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(alert_outcomes, 'AGENTS_ROOT', tmp_path),
            mock.patch.object(
                alert_outcomes, 'OUTCOMES_FILE',
                tmp_path / 'blackboard' / 'alert-outcomes.jsonl'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _rows(self) -> list:
        path = alert_outcomes.OUTCOMES_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text().splitlines() if line.strip()]


class RecordOutcomeTest(_LedgerTest):
    def test_writes_row_with_denormalized_columns(self):
        self.assertTrue(alert_outcomes.record_outcome(
            'not_useful', event_id='ev1', source='watchdog',
            subject='disk-full', tier='NOW', tier_source='translation',
            task_id='t-1',
        ))
        row = self._rows()[0]
        self.assertEqual(row['outcome'], 'not_useful')
        self.assertEqual(row['event_id'], 'ev1')
        self.assertEqual(row['source'], 'watchdog')
        self.assertEqual(row['subject'], 'disk-full')
        self.assertEqual(row['tier'], 'NOW')
        self.assertEqual(row['tier_source'], 'translation')
        self.assertEqual(row['task_id'], 't-1')
        self.assertEqual(row['actor'], 'larry')
        self.assertIn('ts', row)

    def test_unknown_outcome_is_refused_not_written(self):
        # A typo must not silently create a fourth bucket the aggregate drops.
        self.assertFalse(alert_outcomes.record_outcome('sort_of_useful'))
        self.assertEqual(self._rows(), [])

    def test_absent_fields_are_omitted_not_nulled(self):
        self.assertTrue(alert_outcomes.record_outcome('acted', event_id='ev2'))
        row = self._rows()[0]
        for absent in ('source', 'subject', 'tier', 'tier_source', 'task_id'):
            self.assertNotIn(absent, row)

    def test_appends_rather_than_overwrites(self):
        alert_outcomes.record_outcome('acted', event_id='a')
        alert_outcomes.record_outcome('not_useful', event_id='b')
        self.assertEqual([r['event_id'] for r in self._rows()], ['a', 'b'])

    def test_never_raises_on_unwritable_ledger(self):
        # Point at a path whose parent cannot be created — the write fails, the
        # caller gets False, nothing propagates.
        with mock.patch.object(
            alert_outcomes, 'OUTCOMES_FILE',
            Path('/proc/definitely-not-writable/outcomes.jsonl'),
        ):
            self.assertFalse(alert_outcomes.record_outcome('acted'))

    def test_unserializable_value_returns_false(self):
        self.assertFalse(alert_outcomes.record_outcome(
            'acted', source=object(),  # type: ignore[arg-type]
        ))


class RecordForAlertTest(_LedgerTest):
    def test_lifts_fields_off_the_alert_row(self):
        alert = {
            'ts': '2026-07-21T12:00:00+00:00',
            'source': 'medic', 'subject': 'medic-silenced-needs-fix',
            'tier': 'SOON', 'tier_source': 'translation',
            'severity': 'warning', 'message': 'm',
        }
        self.assertTrue(alert_outcomes.record_outcome_for_alert(
            alert, 'auto_resolved', actor='system'))
        row = self._rows()[0]
        self.assertEqual(row['source'], 'medic')
        self.assertEqual(row['subject'], 'medic-silenced-needs-fix')
        self.assertEqual(row['tier'], 'SOON')
        self.assertEqual(row['actor'], 'system')

    def test_non_dict_alert_is_refused(self):
        self.assertFalse(
            alert_outcomes.record_outcome_for_alert('nope', 'acted'))  # type: ignore[arg-type]

    def test_event_id_matches_the_shipper_derivation(self):
        # The join key is worthless unless both sides derive it identically.
        import chain_event_shipper as ces
        alert = {'ts': '2026-07-21T12:00:00+00:00', 'source': 'watchdog',
                 'subject': 'disk-full'}
        expected = ces.compute_event_id(
            ces.alert_event_task_id(alert), 'larry_alert', alert['ts'])
        self.assertEqual(alert_outcomes.alert_event_id(alert), expected)


class AggregateTest(_LedgerTest):
    def test_groups_by_source_and_subject(self):
        for _ in range(3):
            alert_outcomes.record_outcome(
                'not_useful', source='watchdog', subject='disk', tier='NOW')
        alert_outcomes.record_outcome(
            'acted', source='watchdog', subject='disk', tier='NOW')
        alert_outcomes.record_outcome(
            'acted', source='medic', subject='stall', tier='SOON')
        agg = alert_outcomes.aggregate()
        self.assertEqual(agg['watchdog::disk']['not_useful'], 3)
        self.assertEqual(agg['watchdog::disk']['acted'], 1)
        self.assertEqual(agg['watchdog::disk']['total'], 4)
        self.assertEqual(agg['watchdog::disk']['tier'], 'NOW')
        self.assertEqual(agg['medic::stall']['total'], 1)

    def test_malformed_line_is_skipped_not_fatal(self):
        alert_outcomes.record_outcome('acted', source='a', subject='b')
        with open(alert_outcomes.OUTCOMES_FILE, 'a', encoding='utf-8') as f:
            f.write('{not json\n')
        alert_outcomes.record_outcome('acted', source='a', subject='b')
        self.assertEqual(alert_outcomes.aggregate()['a::b']['total'], 2)

    def test_missing_ledger_aggregates_to_empty(self):
        self.assertEqual(alert_outcomes.aggregate(), {})


class AutoResolvedHookTest(unittest.TestCase):
    """`resolve_alert` records auto_resolved for each retracted line."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.alerts_file = self.tmp_path / 'blackboard' / 'larry-alerts.jsonl'
        self.alerts_file.parent.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', self.tmp_path),
            mock.patch.object(larry_alerts, 'ALERTS_FILE', self.alerts_file),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              self.tmp_path / 'state' / 'beacon-offset.txt'),
            mock.patch.object(larry_alerts, 'MEDIC_OFFSET_FILE',
                              self.tmp_path / 'state' / 'medic-offset.txt'),
            mock.patch.object(alert_outcomes, 'AGENTS_ROOT', self.tmp_path),
            mock.patch.object(
                alert_outcomes, 'OUTCOMES_FILE',
                self.tmp_path / 'blackboard' / 'alert-outcomes.jsonl'),
            # The retraction's Supabase clear is a live network write.
            mock.patch.object(larry_alerts, '_retract_shipped_alert_events',
                              lambda *a, **k: 0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _write_alert(self, **over):
        rec = {
            'ts': '2026-07-21T12:00:00+00:00', 'source': 'watchdog',
            'severity': 'warning', 'message': 'm', 'route': 'escalate',
            'subject': 'disk-full', 'tier': 'NOW', 'tier_source': 'translation',
        }
        rec.update(over)
        with open(self.alerts_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
        return rec

    def _outcomes(self) -> list:
        path = alert_outcomes.OUTCOMES_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text().splitlines() if line.strip()]

    def test_retraction_records_auto_resolved(self):
        self._write_alert()
        removed = larry_alerts.resolve_alert('watchdog:disk-full')
        self.assertEqual(removed, 1)
        rows = self._outcomes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'auto_resolved')
        self.assertEqual(rows[0]['actor'], 'system')
        self.assertEqual(rows[0]['subject'], 'disk-full')
        self.assertEqual(rows[0]['tier'], 'NOW')

    def test_no_match_records_nothing(self):
        self._write_alert()
        self.assertEqual(larry_alerts.resolve_alert('watchdog:other'), 0)
        self.assertEqual(self._outcomes(), [])

    def test_ledger_failure_does_not_break_retraction(self):
        # The retraction is the real work; a bookkeeping failure must not
        # turn a fire-and-forget call into an exception or a lost removal.
        self._write_alert()
        with mock.patch.object(alert_outcomes, 'record_outcome_for_alert',
                               side_effect=RuntimeError('ledger down')):
            self.assertEqual(larry_alerts.resolve_alert('watchdog:disk-full'), 1)


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # Drives the guarded larry-alerts / alert-outcomes chokepoints against an
    # already-isolated tempdir, so the Layer B guard would breach before the
    # tests' own mocks apply.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
