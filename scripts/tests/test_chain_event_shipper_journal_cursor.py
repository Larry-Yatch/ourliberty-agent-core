"""Manual journal-cursor tests for scripts/chain_event_shipper.py (PR-E2 #18).

unittest (repo convention; pytest isn't installed on the droplet).

The bug: iter_journalctl let journalctl own --cursor-file advancement, so a
timeout/SIGTERM mid-stream could terminate it AFTER it advanced the cursor past
records streamed-but-never-consumed → silent permanent loss. Fix: resume with
--after-cursor and advance the persisted cursor only as far as records are
actually consumed (each record's own __CURSOR), mirroring the tail_file sources.

These cover the unit-testable half (cursor accounting + command construction);
the end-to-end systemd-255 SIGTERM-vs-cursor-flush timing still needs a droplet
soak, which no local test can reproduce.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import io
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import chain_event_shipper as ces  # noqa: E402


def _rec(i: int) -> dict:
    return {
        '__REALTIME_TIMESTAMP': f'17481893{i:02d}000000',
        'MESSAGE': f'session_start agent=mirror task_id=pr-{i}',
        'OURLIBERTY_EVENT_TYPE': 'session_start',
        'OURLIBERTY_AGENT': 'mirror',
        'OURLIBERTY_TASK_ID': f'pr-{i}',
    }


class _MockSink:
    def __init__(self): self.rows = []
    def insert_rows(self, rows): self.rows.extend(rows)


class _NoBuffer:
    def drain(self): return []
    def clear(self): pass
    def append(self, rows): return 0


class DrainCursorAdvanceTest(unittest.TestCase):
    def _drain(self, journal_iter_fn, journal_cursor=''):
        return ces.drain_once(
            _MockSink(), _NoBuffer(), {}, ces.PulseCursor(), ces._setup_logging(),
            journal_cursor=journal_cursor,
            journal_iter_fn=journal_iter_fn,
            pulse_path=Path('/nonexistent/pulse.json'),
            outbox_log_path=Path('/nonexistent/o.log'),
            larry_alerts_path=Path('/nonexistent/l.jsonl'),
            sentinel_alerts_path=Path('/nonexistent/s.jsonl'),
        )

    def test_advances_to_last_consumed_record(self):
        records = lambda: iter([(_rec(i), f'curs-{i}') for i in range(5)])
        stats, _pulse, jc = self._drain(records, journal_cursor='curs-start')
        self.assertEqual(stats.journal, 5)
        self.assertEqual(jc, 'curs-4')

    def test_partial_consume_does_not_advance_past_unconsumed(self):
        # Models a deadline/SIGTERM break: journalctl streamed 5 records but the
        # consumer only got 3 before the break (the generator simply yields 3).
        # The cursor must stop at the last CONSUMED record, so the next drain
        # re-reads records 3 and 4 via --after-cursor=curs-2 rather than skipping.
        def partial():
            for i in range(3):
                yield _rec(i), f'curs-{i}'
        _stats, _pulse, jc = self._drain(partial, journal_cursor='curs-start')
        self.assertEqual(jc, 'curs-2')
        self.assertNotEqual(jc, 'curs-4')

    def test_empty_journal_keeps_prior_cursor(self):
        _stats, _pulse, jc = self._drain(lambda: iter(()), journal_cursor='curs-keep')
        self.assertEqual(jc, 'curs-keep')

    def test_record_without_cursor_does_not_advance(self):
        # A record lacking a usable __CURSOR yields '' → we keep the prior cursor
        # (never persist an un-resumable position) but still process the record.
        def no_cursor():
            yield _rec(0), ''
        stats, _pulse, jc = self._drain(no_cursor, journal_cursor='curs-prior')
        self.assertEqual(stats.journal, 1)
        self.assertEqual(jc, 'curs-prior')


class IterJournalctlCommandTest(unittest.TestCase):
    """Monkeypatch Popen to capture the command + feed fake stdout."""

    def _run(self, after_cursor, stdout_lines):
        captured = {}

        class _FakeProc:
            def __init__(self):
                self.stdout = iter(stdout_lines)
                self.stderr = io.StringIO('')
                self.returncode = 0
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        def fake_popen(cmd, **kw):
            captured['cmd'] = cmd
            return _FakeProc()

        orig = ces.subprocess.Popen
        ces.subprocess.Popen = fake_popen
        try:
            out = list(ces.iter_journalctl(after_cursor=after_cursor, unit='u.service'))
        finally:
            ces.subprocess.Popen = orig
        return captured['cmd'], out

    def test_no_cursor_file_flag_ever(self):
        cmd, _ = self._run('', [])
        self.assertNotIn('--cursor-file', ' '.join(cmd))
        # No stored cursor → no --after-cursor (drain from journal start).
        self.assertFalse(any(a.startswith('--after-cursor') for a in cmd))

    def test_after_cursor_passed_when_present(self):
        cmd, _ = self._run('s=abc;i=1', [])
        self.assertIn('--after-cursor=s=abc;i=1', cmd)
        self.assertNotIn('--cursor-file', ' '.join(cmd))

    def test_yields_record_and_its_own_cursor(self):
        import json
        lines = [
            json.dumps({'__CURSOR': 's=1', 'MESSAGE': 'a'}) + '\n',
            json.dumps({'__CURSOR': 's=2', 'MESSAGE': 'b'}) + '\n',
            '\n',  # blank line skipped
            'not-json\n',  # undecodable skipped
        ]
        _cmd, out = self._run('', lines)
        self.assertEqual([c for _, c in out], ['s=1', 's=2'])
        self.assertEqual([r['MESSAGE'] for r, _ in out], ['a', 'b'])


class JournalCursorFileTest(unittest.TestCase):
    def test_round_trip_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / 'cursor.journal'
            self.assertEqual(ces.load_journal_cursor(cf), '')  # missing → ''
            ces.save_journal_cursor('s=abc;i=2;b=z', cf)
            self.assertEqual(ces.load_journal_cursor(cf), 's=abc;i=2;b=z')
            # No leftover temp files beside it.
            leftovers = [p.name for p in Path(d).iterdir() if p.name != 'cursor.journal']
            self.assertEqual(leftovers, [])

    def test_empty_cursor_not_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / 'cursor.journal'
            ces.save_journal_cursor('', cf)
            self.assertFalse(cf.exists())


if __name__ == '__main__':
    unittest.main()
