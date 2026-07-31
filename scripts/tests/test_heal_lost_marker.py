#!/usr/bin/env python3
"""Tests for heal_lost_marker (the render-keyed lost-marker net).

Covers the spec's success criteria:
  - a ledger entry WITH emission evidence (each surface: bot-log DMed line,
    inbox live, .archive, .invalid, in-flight, worktree, outbox-notifier line,
    pending-approvals store) -> no alert
  - a ledger entry with NO evidence past the grace window -> exactly one
    lost-marker:<task_id> alert
  - still inside grace -> no alert (lag tolerance)
  - run twice -> each lost marker alerted exactly once (idempotent)
  - kill-switch short-circuit; best-effort config defaults; malformed ledger
    lines skipped; scan-window bound.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_lost_marker
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_lost_marker as h  # noqa: E402
import heal_phantom_dispatch_claim as hpdc  # noqa: E402


NOW = 1_750_000_000.0  # fixed epoch for deterministic grace math
MIN = 60.0
TASK = 'lost-approval-abc-001'


def _iso(age_min: float, now: float = NOW) -> str:
    return datetime.fromtimestamp(now - age_min * MIN, tz=timezone.utc).isoformat()


class _Env:
    """Temp env with the agents/log/worktree roots set + writable dirs."""

    def __init__(self, case: unittest.TestCase):
        self._td = tempfile.TemporaryDirectory()
        case.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        patch = mock.patch.dict(os.environ, {
            'OURLIBERTY_AGENTS_ROOT': str(self.root / 'agents'),
            'OURLIBERTY_LOG_DIR': str(self.root / 'agents' / 'logs'),
            'OURLIBERTY_WORKTREES_ROOT': str(self.root / 'worktrees'),
        })
        patch.start()
        case.addCleanup(patch.stop)
        # ensure OURLIBERTY_MARKER_RENDER_LEDGER isn't leaking from the outer env
        # so the ledger resolves under the tmp agents root.
        os.environ.pop('OURLIBERTY_MARKER_RENDER_LEDGER', None)
        for d in (hpdc.forge_inbox_dir(), hpdc.in_flight_dir(),
                  hpdc.worktrees_root(), hpdc.log_dir(), h.blackboard(),
                  h.pending_approvals_path().parent):
            d.mkdir(parents=True, exist_ok=True)
        (hpdc.forge_inbox_dir() / '.archive').mkdir(exist_ok=True)
        (hpdc.forge_inbox_dir() / '.invalid').mkdir(exist_ok=True)

    def write_ledger(self, *records: dict) -> None:
        lines = [json.dumps(r) for r in records]
        h.render_ledger_path().write_text('\n'.join(lines) + '\n')

    def ledger_record(self, task_id: str = TASK, age_min: float = 20,
                      now: float = NOW, **extra) -> dict:
        rec = {
            'task_id': task_id,
            'agent': 'beacon',
            'marker_type': 'approval_request',
            'rendered_at': _iso(age_min, now),
            'summary': 'ship the thing',
            'phase': None,
        }
        rec.update(extra)
        return rec

    def write_bot_log(self, *lines: str) -> None:
        hpdc.beacon_log_file().write_text('\n'.join(lines) + '\n')

    def write_pending(self, task_id: str, bucket: str = 'pending') -> None:
        h.pending_approvals_path().write_text(json.dumps({
            'version': 1,
            'pending': [{'id': task_id}] if bucket == 'pending' else [],
            'history': [{'id': task_id}] if bucket == 'history' else [],
        }))


class LedgerReadTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env(self)
        self.cfg = {}

    def test_reads_entry_in_window(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        entries = h.read_ledger_entries(NOW, self.cfg)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['task_id'], TASK)

    def test_malformed_line_skipped(self):
        h.render_ledger_path().write_text(
            '{ not json\n' + json.dumps(self.env.ledger_record(age_min=20)) + '\n')
        entries = h.read_ledger_entries(NOW, self.cfg)
        self.assertEqual(len(entries), 1)

    def test_missing_task_id_skipped(self):
        rec = self.env.ledger_record(age_min=20)
        rec.pop('task_id')
        self.env.write_ledger(rec)
        self.assertEqual(h.read_ledger_entries(NOW, self.cfg), [])

    def test_outside_scan_window_ignored(self):
        # default scan window 1440 min; 2000 min old is outside it.
        self.env.write_ledger(self.env.ledger_record(age_min=2000))
        self.assertEqual(h.read_ledger_entries(NOW, self.cfg), [])

    def test_missing_ledger_file_is_empty(self):
        self.assertEqual(h.read_ledger_entries(NOW, self.cfg), [])


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env(self)
        self.cfg = {}

    def _run(self, age_min=20):
        self.env.write_ledger(self.env.ledger_record(age_min=age_min))
        return h.run_once(NOW, self.cfg, alerted=set())

    # --- no evidence -> alert ---
    def test_lost_marker_after_grace_alerts_once(self):
        alerts = self._run(age_min=20)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'], f'lost-marker:{TASK}')
        self.assertIn(TASK, alerts[0]['message'])
        self.assertIn('never reached you', alerts[0]['message'])

    def test_inside_grace_no_alert(self):
        # 5 min < 15-min grace -> lag tolerance.
        self.assertEqual(self._run(age_min=5), [])

    # --- each emission surface suppresses ---
    def test_bot_log_dmed_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        self.env.write_bot_log(
            f'[2026-06-03T12:00:00+0000] [INFO] approval DMed for {TASK}')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_live_inbox_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        (hpdc.forge_inbox_dir() / f'{TASK}.json').write_text('{}')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_archive_inbox_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        (hpdc.forge_inbox_dir() / '.archive' / f'{TASK}.json').write_text('{}')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_invalid_inbox_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        (hpdc.forge_inbox_dir() / '.invalid' / f'{TASK}.json').write_text('{}')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_in_flight_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        (hpdc.in_flight_dir() / f'{TASK}.json').write_text('{}')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_worktree_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        (hpdc.worktrees_root() / f'wt-forge-{TASK}').mkdir()
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_outbox_notifier_line_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        hpdc.outbox_log_file().write_text(
            f'[2026-06-03 23:44:46] [notifier] dispatched (task={TASK})\n')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_pending_approvals_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        self.env.write_pending(TASK, bucket='pending')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_pending_approvals_history_suppresses(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        self.env.write_pending(TASK, bucket='history')
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    # --- dedup ---
    def test_dedup_skips_already_alerted(self):
        self.env.write_ledger(self.env.ledger_record(age_min=20))
        first = h.run_once(NOW, self.cfg, alerted=set())
        self.assertEqual(len(first), 1)
        already = {first[0]['key']}
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=already), [])

    def test_dedup_key_is_task_id_plus_rendered_at(self):
        rec = self.env.ledger_record(age_min=20)
        self.env.write_ledger(rec)
        alerts = h.run_once(NOW, self.cfg, alerted=set())
        self.assertEqual(alerts[0]['key'], f'{TASK}|{rec["rendered_at"]}')


class ConfigTest(unittest.TestCase):
    def test_defaults_when_missing(self):
        cfg = h.load_config(Path('/nonexistent/lost-marker.json'))
        self.assertEqual(cfg, {})
        self.assertEqual(h._grace_sec(cfg), 15 * 60.0)
        self.assertEqual(h._scan_window_sec(cfg), 1440 * 60.0)

    def test_bad_json_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'bad.json'
            p.write_text('{ not json')
            self.assertEqual(h.load_config(p), {})

    def test_shipped_config_loads(self):
        cfg = h.load_config()  # config/lost-marker-patterns.json
        self.assertEqual(cfg.get('grace_minutes'), 15)
        self.assertEqual(cfg.get('scan_window_minutes'), 1440)


class _FakeAlerts:
    def __init__(self):
        self.calls = []

    def append_alert(self, **kw):
        self.calls.append(kw)
        return True


class MainEmitTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env(self)
        self._fake = _FakeAlerts()
        mod_patch = mock.patch.dict(sys.modules, {'larry_alerts': self._fake})
        mod_patch.start()
        self.addCleanup(mod_patch.stop)

    def _write_lost(self, age_min: float = 20):
        h.render_ledger_path().write_text(
            json.dumps(self.env.ledger_record(age_min=age_min, now=time.time()))
            + '\n')

    def test_main_emits_one_lost_marker_alert(self):
        self._write_lost()
        rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._fake.calls), 1)
        call = self._fake.calls[0]
        self.assertEqual(call['source'], 'heal-lost-marker')
        self.assertEqual(call['severity'], 'warning')
        self.assertEqual(call['route'], 'escalate')
        self.assertEqual(call['subject'], f'lost-marker:{TASK}')

    def test_main_idempotent_across_runs(self):
        self._write_lost()
        self.assertEqual(h.main(), 0)
        self.assertEqual(h.main(), 0)
        self.assertEqual(len(self._fake.calls), 1,
                         'lost marker must alert exactly once across ticks')

    def test_main_quiet_when_emitted(self):
        self._write_lost()
        self.env.write_bot_log(f'[ts] [INFO] approval DMed for {TASK}')
        self.assertEqual(h.main(), 0)
        self.assertEqual(self._fake.calls, [])

    def test_kill_switch_short_circuits(self):
        self._write_lost()
        h.kill_switch().write_text('')
        self.assertEqual(h.main(), 0)
        self.assertEqual(self._fake.calls, [])


if __name__ == '__main__':
    unittest.main()
