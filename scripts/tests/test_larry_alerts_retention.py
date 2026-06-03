"""Tests for scripts/larry_alerts_retention.py (cursor-safe alert retention).

unittest (repo convention; pytest isn't installed on the droplet).

Coverage (matches the spec's required matrix):
- load_retention_config: missing / malformed / invalid-field → conservative
  defaults; valid file parsed.
- compute_retention_cut: "whichever retains MORE" = min(days_cut, lines_cut);
  unparseable ts halts the day-cut so undateable lines are never archived.
- run_once cursor-safety: NEVER archives a line at or after a consumer offset;
  beacon + medic offsets are decremented by exactly safe_cut.
- retention window honored (only lines outside the window/floor are archived).
- archived content == removed lines EXACTLY (byte-for-byte, incl. newlines).
- idempotent re-run is a no-op.
- kill-switch (healers.disabled file AND env var) respected.
- chain-shipper cursor refreshed to offset 0 against the rewritten file.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


def _days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).isoformat()


def _alert_line(i: int, days: int) -> str:
    rec = {'ts': _days_ago(days), 'source': 'watchdog', 'severity': 'warning',
           'message': f'fixture alert {i}'}
    return json.dumps(rec, ensure_ascii=False) + '\n'


class _Base(unittest.TestCase):
    """Repoints AGENTS_ROOT at a temp dir + reloads the module so its
    module-level path constants pick up the env var."""

    def setUp(self):
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        self._prev_enable = os.environ.get(
            'OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED')
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'agents'
        (self.root / 'logs').mkdir(parents=True)
        (self.root / 'blackboard').mkdir(parents=True)
        (self.root / 'state').mkdir(parents=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        # Ensure the env kill-switch is enabled for the run_once tests.
        os.environ['OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED'] = '1'
        import larry_alerts_retention as mod
        self.mod = importlib.reload(mod)

        self.alerts = self.root / 'blackboard' / 'larry-alerts.jsonl'
        self.archive_dir = self.root / 'blackboard' / 'archive'
        self.beacon = self.root / 'state' / 'beacon-alerts-offset.txt'
        self.medic = self.root / 'state' / 'medic-alerts-offset.txt'
        self.cursors = self.root / 'state' / 'chain-event-cursors.json'

    def tearDown(self):
        self._tmp.cleanup()
        for var, prev in (
            ('OURLIBERTY_AGENTS_ROOT', self._prev_root),
            ('OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED', self._prev_enable),
        ):
            if prev is not None:
                os.environ[var] = prev
            else:
                os.environ.pop(var, None)

    def _write_alerts(self, lines):
        self.alerts.write_text(''.join(lines), encoding='utf-8')

    def _run(self, **kw):
        return self.mod.run_once(
            now=NOW, alerts_file=self.alerts, archive_dir=self.archive_dir,
            beacon_offset_file=self.beacon, medic_offset_file=self.medic,
            shipper_cursors_file=self.cursors, **kw,
        )


# -------------------- load_retention_config --------------------

class TestLoadConfig(_Base):
    def test_missing_file_defaults(self):
        days, min_lines = self.mod.load_retention_config(self.root / 'nope.json')
        self.assertEqual(days, self.mod.DEFAULT_RETENTION_DAYS)
        self.assertEqual(min_lines, self.mod.DEFAULT_MIN_RETAINED_LINES)

    def test_malformed_file_defaults(self):
        p = self.root / 'bad.json'
        p.write_text('{not json')
        days, min_lines = self.mod.load_retention_config(p)
        self.assertEqual(days, self.mod.DEFAULT_RETENTION_DAYS)
        self.assertEqual(min_lines, self.mod.DEFAULT_MIN_RETAINED_LINES)

    def test_valid_file_parsed(self):
        p = self.root / 'ok.json'
        p.write_text(json.dumps({'retention_days': 30, 'min_retained_lines': 100}))
        days, min_lines = self.mod.load_retention_config(p)
        self.assertEqual(days, 30)
        self.assertEqual(min_lines, 100)

    def test_invalid_fields_fall_back(self):
        p = self.root / 'bad2.json'
        p.write_text(json.dumps({'retention_days': -5, 'min_retained_lines': 'x'}))
        days, min_lines = self.mod.load_retention_config(p)
        self.assertEqual(days, self.mod.DEFAULT_RETENTION_DAYS)
        self.assertEqual(min_lines, self.mod.DEFAULT_MIN_RETAINED_LINES)

    def test_zero_min_lines_is_valid(self):
        p = self.root / 'zero.json'
        p.write_text(json.dumps({'retention_days': 14, 'min_retained_lines': 0}))
        _, min_lines = self.mod.load_retention_config(p)
        self.assertEqual(min_lines, 0)


# -------------------- compute_retention_cut --------------------

class TestComputeCut(_Base):
    def test_whichever_retains_more_lines_floor_wins(self):
        # 100 lines all 40 days old → days_cut=100, but floor keeps last 500,
        # so lines_cut=0 → retain MORE = keep all → cut=0.
        lines = [_alert_line(i, 40) for i in range(100)]
        cut = self.mod.compute_retention_cut(lines, 14, 500, NOW)
        self.assertEqual(cut, 0)

    def test_whichever_retains_more_window_wins(self):
        # 600 lines all 1 day old → days_cut=0 (all inside window), so even
        # though lines_cut=100, retain MORE = keep all → cut=0.
        lines = [_alert_line(i, 1) for i in range(600)]
        cut = self.mod.compute_retention_cut(lines, 14, 500, NOW)
        self.assertEqual(cut, 0)

    def test_both_constraints_cut_is_min(self):
        # 700 lines, first 300 are 40d old, rest are 1d old.
        # days_cut=300, lines_cut=max(0,700-500)=200 → min=200.
        lines = ([_alert_line(i, 40) for i in range(300)]
                 + [_alert_line(i, 1) for i in range(400)])
        cut = self.mod.compute_retention_cut(lines, 14, 500, NOW)
        self.assertEqual(cut, 200)

    def test_unparseable_ts_halts_day_cut(self):
        # An undateable line in the leading run stops days_cut so we never
        # archive a line we can't prove is old.
        lines = [_alert_line(0, 40), 'not json\n', _alert_line(2, 40)]
        cut = self.mod.compute_retention_cut(lines, 14, 0, NOW)
        self.assertEqual(cut, 1)


# -------------------- run_once cursor safety + window --------------------

class TestRunOnce(_Base):
    def test_cursor_safety_caps_at_consumer_offset(self):
        # 700 old lines (all 40d) → policy would cut 200 (floor=500). But a
        # consumer is only caught up to line 150 → safe_cut must cap at 150.
        lines = [_alert_line(i, 40) for i in range(700)]
        self._write_alerts(lines)
        self.beacon.write_text('150')
        self.medic.write_text('600')  # medic ahead; min picks beacon
        counts = self._run()
        self.assertEqual(counts['retention_cut'], 200)
        self.assertEqual(counts['safe_cut'], 150)
        self.assertEqual(counts['archived'], 150)
        # Beacon/medic decremented by exactly safe_cut.
        self.assertEqual(int(self.beacon.read_text()), 0)
        self.assertEqual(int(self.medic.read_text()), 450)
        # Live file now has the 550 survivors.
        survivors = self.alerts.read_text().splitlines()
        self.assertEqual(len(survivors), 550)

    def test_never_archives_at_or_after_offset(self):
        # The strict invariant: the highest archived line index < both offsets.
        lines = [_alert_line(i, 40) for i in range(700)]
        self._write_alerts(lines)
        self.beacon.write_text('120')
        self.medic.write_text('90')  # medic is the binding (smaller) offset
        counts = self._run()
        self.assertEqual(counts['safe_cut'], 90)
        # No pending alert archived: archived count <= min(offsets).
        self.assertLessEqual(counts['archived'], 90)

    def test_archived_content_equals_removed_lines_exactly(self):
        lines = ([_alert_line(i, 40) for i in range(300)]
                 + [_alert_line(i, 1) for i in range(400)])
        self._write_alerts(lines)
        self.beacon.write_text('700')
        self.medic.write_text('700')
        counts = self._run()
        self.assertEqual(counts['safe_cut'], 200)  # min(300, 200)
        archive = self.archive_dir / f'larry-alerts-{NOW.strftime("%Y%m%d")}.jsonl'
        self.assertEqual(archive.read_text(), ''.join(lines[:200]))
        # Survivors are exactly the tail.
        self.assertEqual(self.alerts.read_text(), ''.join(lines[200:]))
        # Concatenation round-trips to the original (nothing lost/dup'd).
        self.assertEqual(archive.read_text() + self.alerts.read_text(),
                         ''.join(lines))

    def test_window_honored_recent_lines_never_archived(self):
        # All lines recent → nothing archived even though there are > floor.
        lines = [_alert_line(i, 1) for i in range(600)]
        self._write_alerts(lines)
        self.beacon.write_text('600')
        self.medic.write_text('600')
        counts = self._run()
        self.assertEqual(counts['safe_cut'], 0)
        self.assertEqual(counts['archived'], 0)
        self.assertFalse(self.archive_dir.exists())

    def test_idempotent_second_run_is_noop(self):
        lines = [_alert_line(i, 40) for i in range(700)]
        self._write_alerts(lines)
        self.beacon.write_text('700')
        self.medic.write_text('700')
        first = self._run()
        self.assertEqual(first['safe_cut'], 200)
        after_first = self.alerts.read_text()
        second = self._run()
        self.assertEqual(second['safe_cut'], 0)
        self.assertEqual(second['archived'], 0)
        # Live file unchanged by the no-op second pass.
        self.assertEqual(self.alerts.read_text(), after_first)

    def test_backlog_clean_consumers_caught_up(self):
        # Mirrors the production first-run: 1226 lines, both consumers at 1226.
        lines = [_alert_line(i, 40) for i in range(1226)]
        self._write_alerts(lines)
        self.beacon.write_text('1226')
        self.medic.write_text('1226')
        counts = self._run()
        # floor 500 → cut 1226-500 = 726; consumers caught up so safe_cut=726.
        self.assertEqual(counts['safe_cut'], 726)
        self.assertEqual(len(self.alerts.read_text().splitlines()), 500)
        self.assertEqual(int(self.beacon.read_text()), 500)
        self.assertEqual(int(self.medic.read_text()), 500)

    def test_dry_run_writes_nothing(self):
        lines = [_alert_line(i, 40) for i in range(700)]
        self._write_alerts(lines)
        self.beacon.write_text('700')
        self.medic.write_text('700')
        counts = self._run(dry_run=True)
        self.assertEqual(counts['safe_cut'], 200)  # still computed
        self.assertEqual(counts['archived'], 0)     # but not applied
        self.assertEqual(len(self.alerts.read_text().splitlines()), 700)
        self.assertFalse(self.archive_dir.exists())
        self.assertEqual(int(self.beacon.read_text()), 700)

    def test_no_final_newline_preserved(self):
        # Last line without a trailing newline must round-trip byte-for-byte.
        body = ''.join(_alert_line(i, 40) for i in range(600))
        body += json.dumps({'ts': _days_ago(40), 'message': 'tail'}) + ''  # no \n
        self.alerts.write_text(body, encoding='utf-8')
        self.beacon.write_text('601')
        self.medic.write_text('601')
        counts = self._run()
        self.assertEqual(counts['total_lines'], 601)
        self.assertEqual(counts['safe_cut'], 101)  # 601-500
        archive = self.archive_dir / f'larry-alerts-{NOW.strftime("%Y%m%d")}.jsonl'
        self.assertEqual(archive.read_text() + self.alerts.read_text(), body)


# -------------------- chain-shipper cursor refresh --------------------

class TestShipperCursorRefresh(_Base):
    def test_cursor_reset_to_offset_zero(self):
        lines = [_alert_line(i, 40) for i in range(700)]
        self._write_alerts(lines)
        self.beacon.write_text('700')
        self.medic.write_text('700')
        # Seed a stale cursor pointing at the end of the OLD file.
        self.cursors.write_text(json.dumps({
            'larry_alerts': {'inode': 111, 'offset': 99999, 'fp_sha': 'stale'},
            'outbox_log': {'inode': 222, 'offset': 5, 'fp_sha': 'keep'},
        }))
        counts = self._run()
        self.assertTrue(counts['cursor_refreshed'])
        data = json.loads(self.cursors.read_text())
        # larry_alerts reset to offset 0 (full re-read; dedup absorbs).
        self.assertEqual(data['larry_alerts']['offset'], 0)
        self.assertNotEqual(data['larry_alerts']['inode'], 111)
        # Unrelated cursors are left intact.
        self.assertEqual(data['outbox_log']['offset'], 5)


# -------------------- kill switch --------------------

class TestKillSwitch(_Base):
    def test_healers_disabled_file_blocks(self):
        (self.root / 'healers.disabled').write_text('')
        self.assertTrue(self.mod.kill_switch_active())

    def test_env_var_false_blocks(self):
        os.environ['OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED'] = 'false'
        self.assertTrue(self.mod.kill_switch_active())

    def test_env_var_true_allows(self):
        os.environ['OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED'] = '1'
        self.assertFalse(self.mod.kill_switch_active())

    def test_main_noop_when_disabled(self):
        # main() should early-exit 0 and touch nothing when disabled.
        lines = [_alert_line(i, 40) for i in range(700)]
        self._write_alerts(lines)
        self.beacon.write_text('700')
        self.medic.write_text('700')
        os.environ['OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED'] = 'no'
        # Reload so module-level constants (ALERTS_FILE etc.) point at temp root,
        # then call main with argv patched to no flags.
        argv = sys.argv
        sys.argv = ['larry_alerts_retention.py']
        try:
            rc = self.mod.main()
        finally:
            sys.argv = argv
        self.assertEqual(rc, 0)
        # File untouched.
        self.assertEqual(len(self.alerts.read_text().splitlines()), 700)


if __name__ == '__main__':
    unittest.main()
