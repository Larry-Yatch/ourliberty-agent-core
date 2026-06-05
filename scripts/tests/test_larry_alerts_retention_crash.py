"""Crash-atomicity (#8) and concurrent-append (#16) tests for
scripts/larry_alerts_retention.py — the PR-E2 deferred findings.

unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- #8 crash-atomicity: a crash injected between the live-file rewrite and the
  offset decrement (and the symmetric before-rewrite case) is completed
  idempotently on the next tick by _recover_pending, with NO stranded alerts and
  NO offset over-decrement. Post-crash appends (which land at the file tail) are
  preserved across recovery.
- #16 concurrent append: a tight multi-process append storm running concurrently
  with the retention rewrite never loses an appended alert, because every
  appender and the rewrite take the same sidecar flock.
- the intent journal is created during a pass and removed on success.
"""
from __future__ import annotations

import importlib
import json
import multiprocessing
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
NOW_TS = NOW.isoformat()


def _old_line(i: int, days: int = 30) -> str:
    rec = {'ts': (NOW - timedelta(days=days)).isoformat(), 'source': 'watchdog',
           'severity': 'warning', 'message': f'old {i}'}
    return json.dumps(rec, ensure_ascii=False) + '\n'


def _fresh_line(seq: int) -> str:
    rec = {'ts': NOW_TS, 'source': 'stress', 'severity': 'warning',
           'message': f'fresh {seq}', 'seq': seq}
    return json.dumps(rec, ensure_ascii=False) + '\n'


# Top-level worker (importable/picklable for the 'spawn' start method).
def _append_worker(alerts_file_str: str, seqs) -> None:
    sys.path.insert(0, str(_SCRIPTS_DIR))
    import larry_alerts
    larry_alerts.ALERTS_FILE = Path(alerts_file_str)
    for s in seqs:
        larry_alerts._locked_append(_fresh_line(s))


class _Base(unittest.TestCase):
    def setUp(self):
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        self._prev_enable = os.environ.get(
            'OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED')
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'agents'
        for sub in ('logs', 'blackboard', 'state'):
            (self.root / sub).mkdir(parents=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        os.environ['OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED'] = '1'
        import larry_alerts_retention as mod
        self.mod = importlib.reload(mod)
        self.alerts = self.root / 'blackboard' / 'larry-alerts.jsonl'
        self.archive_dir = self.root / 'blackboard' / 'archive'
        self.beacon = self.root / 'state' / 'beacon-alerts-offset.txt'
        self.medic = self.root / 'state' / 'medic-alerts-offset.txt'
        self.cursors = self.root / 'state' / 'chain-event-cursors.json'
        self.journal = self.mod._journal_path(self.alerts)

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

    def _set_offsets(self, beacon, medic):
        self.beacon.write_text(str(beacon))
        self.medic.write_text(str(medic))

    def _run(self, **kw):
        return self.mod.run_once(
            now=NOW, config=(14, kw.pop('min_retained', 2)),
            alerts_file=self.alerts, archive_dir=self.archive_dir,
            beacon_offset_file=self.beacon, medic_offset_file=self.medic,
            shipper_cursors_file=self.cursors, **kw,
        )

    def _read_lines(self):
        return self.alerts.read_text(encoding='utf-8').splitlines()


class JournalLifecycleTest(_Base):
    def test_journal_absent_after_clean_run(self):
        # 3 old + 2 fresh, both offsets at 3 → safe_cut=3 (drop the 3 old).
        self._write_alerts([_old_line(i) for i in range(3)]
                           + [_fresh_line(100), _fresh_line(101)])
        self._set_offsets(3, 3)
        counts = self._run()
        self.assertEqual(counts['safe_cut'], 3)
        self.assertEqual(counts['archived'], 3)
        self.assertFalse(self.journal.exists(), 'journal must be cleared on success')
        # Offsets decremented to absolute targets 0/0; survivors are the 2 fresh.
        self.assertEqual(self.beacon.read_text(), '0')
        self.assertEqual(self.medic.read_text(), '0')
        self.assertEqual(len(self._read_lines()), 2)

    def test_no_op_leaves_no_journal(self):
        self._write_alerts([_fresh_line(1)])
        self._set_offsets(0, 0)
        counts = self._run()
        self.assertEqual(counts['safe_cut'], 0)
        self.assertFalse(self.journal.exists())


class CrashRecoveryTest(_Base):
    """Inject a crash mid-transaction, then re-run and assert idempotent
    completion with no stranded alerts."""

    def _seed(self):
        # 3 old (delivered: offsets=3) + 2 fresh survivors. safe_cut=3 → targets 0/0.
        self._write_alerts([_old_line(i) for i in range(3)]
                           + [_fresh_line(100), _fresh_line(101)])
        self._set_offsets(3, 3)

    def test_crash_after_rewrite_before_offsets(self):
        """Rewrite landed (inode swapped) but offsets not yet written. Recovery
        must complete ONLY the offset decrement (inode-changed branch)."""
        self._seed()
        boom = RuntimeError('simulated SIGKILL before offset write')
        orig = self.mod._write_offset
        calls = {'n': 0}

        def crashing_write_offset(path, offset):
            calls['n'] += 1
            raise boom

        self.mod._write_offset = crashing_write_offset
        with self.assertRaises(RuntimeError):
            self._run()
        self.mod._write_offset = orig

        # Mid-crash state: rewrite done (only 2 survivors), journal present,
        # offsets still stale at 3 (never decremented).
        self.assertTrue(self.journal.exists())
        self.assertEqual(len(self._read_lines()), 2)
        self.assertEqual(self.beacon.read_text(), '3')

        # Recovery on the next tick completes the decrement idempotently.
        counts = self._run()
        self.assertTrue(counts['recovered'])
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.beacon.read_text(), '0')
        self.assertEqual(self.medic.read_text(), '0')
        # Survivors intact, nothing stranded or double-dropped.
        lines = self._read_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn('"seq": 100', lines[0])
        self.assertIn('"seq": 101', lines[1])

    def test_crash_before_rewrite(self):
        """Journal written but the rewrite never happened (inode unchanged).
        Recovery must drop the recorded leading lines from the current file."""
        self._seed()
        orig = self.mod._atomic_rewrite

        def crashing_rewrite(path, lines):
            raise RuntimeError('simulated SIGKILL before rewrite')

        self.mod._atomic_rewrite = crashing_rewrite
        with self.assertRaises(RuntimeError):
            self._run()
        self.mod._atomic_rewrite = orig

        # Mid-crash: file untouched (5 lines), offsets still 3, journal present.
        self.assertTrue(self.journal.exists())
        self.assertEqual(len(self._read_lines()), 5)
        self.assertEqual(self.beacon.read_text(), '3')

        counts = self._run()
        self.assertTrue(counts['recovered'])
        self.assertFalse(self.journal.exists())
        # The 3 old leading lines are dropped; the 2 fresh survivors remain.
        lines = self._read_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn('"seq": 100', lines[0])
        self.assertEqual(self.beacon.read_text(), '0')

    def test_crash_before_rewrite_preserves_post_crash_appends(self):
        """An alert appended between the crash and recovery (at the file tail)
        must survive recovery, not be clobbered by replaying a stale snapshot."""
        self._seed()
        orig = self.mod._atomic_rewrite
        self.mod._atomic_rewrite = lambda p, lines: (_ for _ in ()).throw(
            RuntimeError('crash before rewrite'))
        with self.assertRaises(RuntimeError):
            self._run()
        self.mod._atomic_rewrite = orig

        # A healer appends a fresh critical alert after the crash, before recovery.
        import larry_alerts
        larry_alerts.ALERTS_FILE = self.alerts
        self.assertTrue(larry_alerts._locked_append(_fresh_line(999)))
        self.assertEqual(len(self._read_lines()), 6)  # 3 old + 2 fresh + 1 new

        counts = self._run()
        self.assertTrue(counts['recovered'])
        lines = self._read_lines()
        # 3 old dropped; 2 original survivors + the post-crash append remain.
        self.assertEqual(len(lines), 3)
        joined = '\n'.join(lines)
        self.assertIn('"seq": 999', joined)
        self.assertIn('"seq": 100', joined)
        self.assertIn('"seq": 101', joined)
        self.assertNotIn('"old', joined)


class InodeUnavailableTest(_Base):
    def test_skips_destructive_tick_when_inode_unreadable(self):
        """If the alerts-file inode can't be captured, the pass must NOT rewrite
        or decrement offsets (crash-safe recovery would be impossible)."""
        self._write_alerts([_old_line(i) for i in range(3)]
                           + [_fresh_line(1), _fresh_line(2)])
        self._set_offsets(3, 3)
        orig_fstat = self.mod.os.fstat

        def boom_fstat(fd):
            raise OSError('no inode')

        self.mod.os.fstat = boom_fstat
        try:
            counts = self._run()
        finally:
            self.mod.os.fstat = orig_fstat
        # Nothing destructive happened: file + offsets untouched, no journal.
        self.assertEqual(counts['archived'], 0)
        self.assertEqual(len(self._read_lines()), 5)
        self.assertEqual(self.beacon.read_text(), '3')
        self.assertFalse(self.journal.exists())


class ConcurrentAppendTest(_Base):
    """#16: an append storm concurrent with the rewrite loses no alert."""

    @unittest.skipUnless(
        __import__('file_lock').have_flock(), 'requires fcntl.flock')
    def test_no_appended_alert_lost_across_rewrite(self):
        n_appenders, per = 2, 250
        for trial in range(4):
            # Fresh fixture each trial: 200 old (all delivered) → one big rewrite.
            self._write_alerts([_old_line(i) for i in range(200)])
            self._set_offsets(200, 200)
            if self.journal.exists():
                self.journal.unlink()

            base = trial * 10_000
            ctx = multiprocessing.get_context()
            seq_sets = [
                list(range(base + k * per, base + (k + 1) * per))
                for k in range(n_appenders)
            ]
            procs = [
                ctx.Process(target=_append_worker, args=(str(self.alerts), s))
                for s in seq_sets
            ]
            for p in procs:
                p.start()
            # Rewrite concurrently with the in-flight append storm.
            self._run(min_retained=0)
            for p in procs:
                p.join(timeout=60)
                self.assertEqual(p.exitcode, 0)

            # Every appended fresh line must survive in the live file (they are
            # never archived — fresh ts, past the offsets). Old lines archived.
            live = self.alerts.read_text(encoding='utf-8')
            expected = set(s for ss in seq_sets for s in ss)
            found = set()
            for line in live.splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('source') == 'stress':
                    found.add(rec['seq'])
            missing = expected - found
            self.assertEqual(
                missing, set(),
                f'trial {trial}: lost {len(missing)} appended alert(s): '
                f'{sorted(missing)[:10]}')


if __name__ == '__main__':
    unittest.main()
