#!/usr/bin/env python3
"""Fixtures for `dispatch_sentinel` — stall detection scans.

Phase D3-prep (2026-05-11). Three scan kinds: inbox stalls, in-flight
stalls (D3-specific), and stale leases. Plus dedup state, per-model
thresholds, and garbage-collection of resolved alerts.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_dispatch_sentinel
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_sentinel as ds  # noqa: E402
import larry_alerts  # noqa: E402


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test.

    Why: dispatch_sentinel's STATE_FILE / LOG_FILE / ALERTS_LOG /
    LEASES_DIR / IN_FLIGHT_DIR / AGENT_MODELS_FILE derive from AGENTS_ROOT
    at import time. Without this redirection, running tests in a worktree
    pollutes prod `/home/larry/agents/...` state. Reload the module so its
    module-level constants pick up the override. The existing per-test
    attribute overrides (which point at a separate test root) layer on
    top — fine, just redundant defense-in-depth.
    """

    def setUp(self):
        super().setUp()
        self._isolated_tmp = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
            os.makedirs(os.path.join(self._isolated_tmp, sub), exist_ok=True)
        self._isolated_env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_tmp
        importlib.reload(ds)

    def tearDown(self):
        if self._isolated_env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_env_orig
        importlib.reload(ds)
        shutil.rmtree(self._isolated_tmp, ignore_errors=True)
        super().tearDown()


def _redirect_larry_alerts(root: Path) -> dict:
    """Point larry_alerts paths at the test root; return originals for restore."""
    originals = {
        'AGENTS_ROOT': larry_alerts.AGENTS_ROOT,
        'ALERTS_FILE': larry_alerts.ALERTS_FILE,
        'COOLDOWN_ROOT': larry_alerts.COOLDOWN_ROOT,
        'OFFSET_FILE': larry_alerts.OFFSET_FILE,
    }
    larry_alerts.AGENTS_ROOT = root
    larry_alerts.ALERTS_FILE = root / 'blackboard' / 'larry-alerts.jsonl'
    larry_alerts.COOLDOWN_ROOT = root / 'state' / 'alert-cooldown'
    larry_alerts.OFFSET_FILE = root / 'state' / 'beacon-alerts-offset.txt'
    return originals


def _restore_larry_alerts(originals: dict) -> None:
    for name, value in originals.items():
        setattr(larry_alerts, name, value)


class SentinelScansTest(_IsolatedAgentsRoot):
    """The three scan kinds + per-model thresholds + dedup."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {
            'AGENTS_ROOT': ds.AGENTS_ROOT,
            'STATE_FILE': ds.STATE_FILE,
            'LOG_FILE': ds.LOG_FILE,
            'ALERTS_LOG': ds.ALERTS_LOG,
            'LEASES_DIR': ds.LEASES_DIR,
            'IN_FLIGHT_DIR': ds.IN_FLIGHT_DIR,
            'AGENT_MODELS_FILE': ds.AGENT_MODELS_FILE,
        }
        ds.AGENTS_ROOT = self._root
        ds.STATE_FILE = self._root / 'state' / 'dispatch-sentinel.json'
        ds.LOG_FILE = self._root / 'logs' / 'dispatch-sentinel.log'
        ds.ALERTS_LOG = self._root / 'blackboard' / 'sentinel-alerts.jsonl'
        ds.LEASES_DIR = self._root / 'state' / 'dispatch-leases'
        ds.IN_FLIGHT_DIR = self._root / 'state' / 'in-flight'
        ds.AGENT_MODELS_FILE = self._root / 'config' / 'agent-models.json'
        self._la_originals = _redirect_larry_alerts(self._root)

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(ds, name, value)
        _restore_larry_alerts(self._la_originals)
        self._tmp.cleanup()
        super().tearDown()

    def _write_inbox_task(self, agent, name, mtime_age_seconds, body=None):
        inbox = self._root / 'inboxes' / agent
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / name
        with open(f, 'w') as fh:
            json.dump(body or {'prompt': 'x' * 200, 'source': 'beacon', 'task_id': name}, fh)
        old_time = time.time() - mtime_age_seconds
        os.utime(f, (old_time, old_time))
        return f

    def _write_in_flight(self, name, started_age_seconds, agent='forge',
                         model='sonnet-4.6', iso=True, pid=None):
        # PRODUCTION format: agent_runner writes started_at as an ISO-8601
        # STRING. The old fixture wrote an epoch float, which masked the bug
        # where scan_in_flight float()'d the ISO string and skipped every entry.
        # pid defaults to THIS test process (alive) so the §3.5 pid-alive check
        # treats the entry as a genuine running stall; tests that exercise the
        # dead-pid reconcile path pass an explicit dead pid.
        from datetime import datetime, timedelta, timezone
        ds.IN_FLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        f = ds.IN_FLIGHT_DIR / name
        if iso:
            started = (datetime.now(timezone.utc)
                       - timedelta(seconds=started_age_seconds)).isoformat()
        else:
            started = time.time() - started_age_seconds  # legacy epoch float
        with open(f, 'w') as fh:
            json.dump({
                'task_stem': name.replace('.json', ''),
                'agent_id': agent,
                'started_at': started,
                'model': model,
                'pid': os.getpid() if pid is None else pid,
            }, fh)
        return f

    # A terminal_state_fn that never reports terminal — keeps over-threshold
    # entries as genuine stalls without consulting the real `gh` probe.
    _KEEP = staticmethod(lambda task_stem: ds.tts.UNKNOWN)

    def _write_lease(self, name, renewed_age_seconds):
        ds.LEASES_DIR.mkdir(parents=True, exist_ok=True)
        f = ds.LEASES_DIR / name
        with open(f, 'w') as fh:
            json.dump({
                'identity': 'inbox:forge',
                'timestamp_renewed': time.time() - renewed_age_seconds,
                'pid': 99999,
            }, fh)
        return f

    def test_inbox_stall_detected_after_threshold(self):
        self._write_inbox_task('beacon', 'fresh.json', mtime_age_seconds=60)
        self._write_inbox_task('beacon', 'stale.json',
                                mtime_age_seconds=ds.INBOX_STALL_SECONDS + 60)
        stalls = ds.scan_inbox('beacon', time.time())
        names = [s['file'] for s in stalls]
        self.assertIn('stale.json', names)
        self.assertNotIn('fresh.json', names)

    def test_inbox_not_before_cooldown_respected(self):
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._write_inbox_task(
            'beacon', 'cooldown.json',
            mtime_age_seconds=ds.INBOX_STALL_SECONDS + 60,
            body={
                'prompt': 'x' * 200, 'source': 'beacon', 'task_id': 'cd',
                'not_before': future,
            },
        )
        stalls = ds.scan_inbox('beacon', time.time())
        self.assertEqual(stalls, [], 'task with future not_before should not stall')

    def test_in_flight_stall_per_model_threshold(self):
        # Sonnet threshold = 30m. 25m → not stalled. 35m → stalled.
        # Uses the PRODUCTION ISO started_at — this test would FAIL on the old
        # float()-only parse (every entry skipped → no stall ever detected).
        self._write_in_flight('fresh.json', started_age_seconds=25 * 60, model='sonnet-4.6')
        self._write_in_flight('old.json', started_age_seconds=35 * 60, model='sonnet-4.6')
        stalls = ds.scan_in_flight(time.time(), terminal_state_fn=self._KEEP)
        names = [s['file'] for s in stalls]
        self.assertIn('old.json', names)
        self.assertNotIn('fresh.json', names)

    def test_in_flight_stall_detected_with_legacy_epoch_float(self):
        # Back-compat: a legacy epoch-float started_at still parses.
        self._write_in_flight('legacy.json', started_age_seconds=35 * 60,
                              model='sonnet-4.6', iso=False)
        stalls = ds.scan_in_flight(time.time(), terminal_state_fn=self._KEEP)
        self.assertIn('legacy.json', [s['file'] for s in stalls])

    def test_started_epoch_parses_iso_and_float_and_garbage(self):
        from datetime import datetime, timezone
        iso = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            ds._started_epoch({'started_at': iso.isoformat()}),
            iso.timestamp(), places=3)
        self.assertEqual(ds._started_epoch({'started_at': 1700000000.0}), 1700000000.0)
        self.assertEqual(ds._started_epoch({'timestamp_started': 1700000000}), 1700000000.0)
        self.assertEqual(ds._started_epoch({'started_at': 'not-a-date'}), 0.0)
        self.assertEqual(ds._started_epoch({}), 0.0)

    def test_in_flight_stall_opus_has_higher_threshold(self):
        # Opus threshold = 60m. 35m should NOT stall on opus.
        self._write_in_flight('opus.json', started_age_seconds=35 * 60, model='claude-opus-4-7')
        stalls = ds.scan_in_flight(time.time(), terminal_state_fn=self._KEEP)
        self.assertEqual(stalls, [], 'opus 35m task should not stall (60m threshold)')

    # ---- §3.5 terminal-state reconcile (conservative guard) ----

    def test_in_flight_dead_pid_reconciled_not_nagged(self):
        # Dead worker pid ⇒ phantom record retired, no stall surfaced.
        f = self._write_in_flight('dead.json', started_age_seconds=35 * 60,
                                   model='sonnet-4.6', pid=2_147_483_646)
        stalls = ds.scan_in_flight(
            time.time(),
            pid_alive_fn=lambda pid: False,
            terminal_state_fn=self._KEEP,
        )
        self.assertEqual(stalls, [], 'dead-pid entry should reconcile, not nag')
        self.assertFalse(f.exists(), 'phantom record should be removed')

    def test_in_flight_merged_pr_reconciled_not_nagged(self):
        # Live pid but the work's PR is MERGED ⇒ terminal ⇒ retire.
        f = self._write_in_flight('merged.json', started_age_seconds=35 * 60,
                                   model='sonnet-4.6')
        stalls = ds.scan_in_flight(
            time.time(),
            pid_alive_fn=lambda pid: True,
            terminal_state_fn=lambda task_stem: ds.tts.MERGED,
        )
        self.assertEqual(stalls, [], 'merged-PR entry should reconcile, not nag')
        self.assertFalse(f.exists(), 'phantom record should be removed')

    def test_in_flight_open_pr_kept_and_nagged(self):
        # Live pid + OPEN PR ⇒ genuine stall ⇒ keep + surface. Record stays.
        f = self._write_in_flight('open.json', started_age_seconds=35 * 60,
                                   model='sonnet-4.6')
        stalls = ds.scan_in_flight(
            time.time(),
            pid_alive_fn=lambda pid: True,
            terminal_state_fn=lambda task_stem: ds.tts.OPEN,
        )
        self.assertIn('open.json', [s['file'] for s in stalls])
        self.assertTrue(f.exists(), 'live/open entry must NOT be reconciled')

    def test_in_flight_unknown_probe_kept_and_nagged(self):
        # Live pid + UNKNOWN (gh failure / no match) ⇒ conservative keep.
        f = self._write_in_flight('unknown.json', started_age_seconds=35 * 60,
                                   model='sonnet-4.6')
        stalls = ds.scan_in_flight(
            time.time(),
            pid_alive_fn=lambda pid: True,
            terminal_state_fn=lambda task_stem: ds.tts.UNKNOWN,
        )
        self.assertIn('unknown.json', [s['file'] for s in stalls])
        self.assertTrue(f.exists(), 'UNKNOWN entry must NOT be reconciled')

    def test_in_flight_legit_no_pr_suppressed_before_age_alert(self):
        # A task whose CORRECT terminal outcome is no PR (mirror-review-*) can
        # NEVER satisfy the terminal-PR probe, so a lingering in-flight record
        # would age into a stall alert purely from PR-absence. The no-PR-
        # legitimacy classifier gate suppresses that age-alert even with a live
        # pid and an UNKNOWN terminal probe.
        f = self._write_in_flight(
            'mirror-review-pr-ourliberty-agent-core-931.json',
            started_age_seconds=35 * 60, agent='mirror', model='sonnet-4.6')
        stalls = ds.scan_in_flight(
            time.time(),
            pid_alive_fn=lambda pid: True,
            terminal_state_fn=lambda task_stem: ds.tts.UNKNOWN,
        )
        self.assertEqual(
            stalls, [], 'legit-no-PR review must not age-alert as a stall')
        self.assertTrue(
            f.exists(), 'suppressed record is left in place, not reconciled')

    def test_in_flight_unknown_shape_still_nagged(self):
        # Conservative: a plain build-shaped task the classifier can't positively
        # call legit-no-PR (UNKNOWN) still surfaces as a genuine stall.
        f = self._write_in_flight('reconcile-hardening-mission-001.json',
                                   started_age_seconds=35 * 60, model='sonnet-4.6')
        stalls = ds.scan_in_flight(
            time.time(),
            pid_alive_fn=lambda pid: True,
            terminal_state_fn=lambda task_stem: ds.tts.UNKNOWN,
        )
        self.assertIn('reconcile-hardening-mission-001.json',
                      [s['file'] for s in stalls])
        self.assertTrue(f.exists())

    def test_pid_alive_helper(self):
        self.assertTrue(ds._pid_alive(os.getpid()))
        self.assertFalse(ds._pid_alive(None))
        self.assertFalse(ds._pid_alive(0))
        self.assertFalse(ds._pid_alive('not-an-int'))
        self.assertFalse(ds._pid_alive(2_147_483_646))

    def test_stale_lease_detected(self):
        self._write_lease('fresh.lease', renewed_age_seconds=300)
        self._write_lease('stale.lease', renewed_age_seconds=ds.STALE_LEASE_SECONDS + 60)
        stalls = ds.scan_stale_leases(time.time())
        names = [s['file'] for s in stalls]
        self.assertIn('stale.lease', names)
        self.assertNotIn('fresh.lease', names)

    def test_per_model_threshold_helper(self):
        self.assertEqual(ds._per_model_threshold('claude-opus-4-7'), 60 * 60)
        self.assertEqual(ds._per_model_threshold('claude-sonnet-4-6'), 30 * 60)
        self.assertEqual(ds._per_model_threshold('claude-haiku-4-5-20251001'), 15 * 60)
        self.assertEqual(ds._per_model_threshold(None), ds.DEFAULT_IN_FLIGHT_THRESHOLD)
        self.assertEqual(ds._per_model_threshold('unknown-model'), ds.DEFAULT_IN_FLIGHT_THRESHOLD)


class SentinelMainTest(_IsolatedAgentsRoot):
    """End-to-end main() — alerts dedup across runs, GC on resolution."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {
            'AGENTS_ROOT': ds.AGENTS_ROOT,
            'STATE_FILE': ds.STATE_FILE,
            'LOG_FILE': ds.LOG_FILE,
            'ALERTS_LOG': ds.ALERTS_LOG,
            'LEASES_DIR': ds.LEASES_DIR,
            'IN_FLIGHT_DIR': ds.IN_FLIGHT_DIR,
            'AGENT_MODELS_FILE': ds.AGENT_MODELS_FILE,
        }
        ds.AGENTS_ROOT = self._root
        ds.STATE_FILE = self._root / 'state' / 'dispatch-sentinel.json'
        ds.LOG_FILE = self._root / 'logs' / 'dispatch-sentinel.log'
        ds.ALERTS_LOG = self._root / 'blackboard' / 'sentinel-alerts.jsonl'
        ds.LEASES_DIR = self._root / 'state' / 'dispatch-leases'
        ds.IN_FLIGHT_DIR = self._root / 'state' / 'in-flight'
        ds.AGENT_MODELS_FILE = self._root / 'config' / 'agent-models.json'
        self._la_originals = _redirect_larry_alerts(self._root)

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(ds, name, value)
        _restore_larry_alerts(self._la_originals)
        self._tmp.cleanup()
        super().tearDown()

    def _stale_inbox(self, agent='beacon', name='stale.json'):
        inbox = self._root / 'inboxes' / agent
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / name
        with open(f, 'w') as fh:
            json.dump({'prompt': 'x' * 200, 'source': 'beacon', 'task_id': 't'}, fh)
        old_time = time.time() - ds.INBOX_STALL_SECONDS - 60
        os.utime(f, (old_time, old_time))
        return f

    def test_alert_fires_once_then_dedups(self):
        stale = self._stale_inbox()
        # First run alerts.
        ds.main()
        state1 = json.loads(ds.STATE_FILE.read_text())
        self.assertEqual(state1['last_new_alerts'], 1)
        alerts1 = ds.ALERTS_LOG.read_text().strip().splitlines()
        self.assertEqual(len(alerts1), 1)
        # Second run — still stale, but already alerted → no new record.
        ds.main()
        state2 = json.loads(ds.STATE_FILE.read_text())
        self.assertEqual(state2['last_new_alerts'], 0)
        alerts2 = ds.ALERTS_LOG.read_text().strip().splitlines()
        self.assertEqual(len(alerts2), 1, 'alerts log should not double-write')

    def test_resolved_stall_garbage_collected_from_state(self):
        stale = self._stale_inbox()
        ds.main()
        state1 = json.loads(ds.STATE_FILE.read_text())
        self.assertEqual(len(state1['alerted']), 1)
        stale.unlink()  # operator resolved it
        ds.main()
        state2 = json.loads(ds.STATE_FILE.read_text())
        self.assertEqual(state2['alerted'], {}, 'gc should drop resolved alerts')


class SentinelLarryAlertsTest(_IsolatedAgentsRoot):
    """D3.5-prep additions — larry-alerts hook + cold-start re-arming (C2 fix)."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {
            'AGENTS_ROOT': ds.AGENTS_ROOT,
            'STATE_FILE': ds.STATE_FILE,
            'LOG_FILE': ds.LOG_FILE,
            'ALERTS_LOG': ds.ALERTS_LOG,
            'LEASES_DIR': ds.LEASES_DIR,
            'IN_FLIGHT_DIR': ds.IN_FLIGHT_DIR,
            'AGENT_MODELS_FILE': ds.AGENT_MODELS_FILE,
        }
        ds.AGENTS_ROOT = self._root
        ds.STATE_FILE = self._root / 'state' / 'dispatch-sentinel.json'
        ds.LOG_FILE = self._root / 'logs' / 'dispatch-sentinel.log'
        ds.ALERTS_LOG = self._root / 'blackboard' / 'sentinel-alerts.jsonl'
        ds.LEASES_DIR = self._root / 'state' / 'dispatch-leases'
        ds.IN_FLIGHT_DIR = self._root / 'state' / 'in-flight'
        ds.AGENT_MODELS_FILE = self._root / 'config' / 'agent-models.json'
        self._la_originals = _redirect_larry_alerts(self._root)

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(ds, name, value)
        _restore_larry_alerts(self._la_originals)
        self._tmp.cleanup()
        super().tearDown()

    def _stale_inbox(self, agent='beacon', name='stale.json'):
        inbox = self._root / 'inboxes' / agent
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / name
        with open(f, 'w') as fh:
            json.dump({'prompt': 'x' * 200, 'source': 'beacon', 'task_id': 't'}, fh)
        old_time = time.time() - ds.INBOX_STALL_SECONDS - 60
        os.utime(f, (old_time, old_time))
        return f

    def test_load_state_missing_file_is_cold_start(self):
        state, cold = ds.load_state()
        self.assertEqual(state, {'alerted': {}})
        self.assertTrue(cold)

    def test_load_state_corrupted_file_is_cold_start(self):
        ds.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ds.STATE_FILE.write_text('}{ not json')
        state, cold = ds.load_state()
        self.assertEqual(state, {'alerted': {}})
        self.assertTrue(cold)

    def test_load_state_valid_file_not_cold_start(self):
        ds.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ds.STATE_FILE.write_text(json.dumps({'alerted': {'k': 1.0}}))
        state, cold = ds.load_state()
        self.assertEqual(state['alerted'], {'k': 1.0})
        self.assertFalse(cold)

    def test_cold_start_suppresses_larry_alert(self):
        # State file missing → first sweep records to sentinel-alerts.jsonl
        # but NOT to larry-alerts.jsonl (C2 fix — don't flood after corruption).
        self._stale_inbox()
        ds.main()
        # Sentinel's own alert log: written
        self.assertTrue(ds.ALERTS_LOG.exists())
        self.assertGreaterEqual(len(ds.ALERTS_LOG.read_text().strip().splitlines()), 1)
        # Larry's queue: should be empty after cold-start sweep
        self.assertFalse(
            larry_alerts.ALERTS_FILE.exists() and
            larry_alerts.ALERTS_FILE.read_text().strip()
        )

    def test_second_run_appends_larry_alert_for_new_stall(self):
        # First sweep with no stalls (cold start, no-op).
        ds.main()
        # Now state file exists → second sweep is NOT a cold start.
        # Plant a stall and re-run.
        self._stale_inbox()
        ds.main()
        self.assertTrue(larry_alerts.ALERTS_FILE.exists())
        lines = larry_alerts.ALERTS_FILE.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record['source'], 'sentinel')
        self.assertEqual(record['severity'], 'warning')
        self.assertIn('beacon', record['message'])

    def test_already_alerted_stall_does_not_re_DM(self):
        # First sweep, cold-start path → records to disk, no DM.
        self._stale_inbox()
        ds.main()
        # Second sweep — same stall, already in alerted, should NOT re-fire to larry-alerts.
        ds.main()
        self.assertFalse(
            larry_alerts.ALERTS_FILE.exists() and
            larry_alerts.ALERTS_FILE.read_text().strip()
        )


class StallMessageCopyTest(unittest.TestCase):
    """A slot-occupied Forge queue must be explained as a HELD SLOT, never left
    to read as a Tier 2 OAuth expiry (the 2026-06-24
    forge-post-open-mergeable-rebase-001 incident)."""

    def test_forge_inbox_stall_names_held_slot_not_auth(self):
        msg = ds._stall_dm_message({
            'kind': 'inbox-stall', 'agent': 'forge',
            'file': 'build-x.json', 'age_hours': 3.1,
        })
        self.assertIn('build slot', msg.lower())
        self.assertIn('wedged-session reaper', msg)
        # Steers away from the wrong default.
        self.assertIn('auth', msg.lower())

    def test_in_flight_stall_explicitly_rules_out_oauth(self):
        msg = ds._stall_dm_message({
            'kind': 'in-flight-stall', 'agent': 'forge',
            'file': 'build-x.json', 'age_hours': 3.9,
            'threshold_seconds': 3600, 'pid': 2060999,
        })
        self.assertIn('held slot', msg.lower())
        self.assertIn('2060999', msg)
        self.assertIn('OAuth', msg)  # named only to say it is NOT the cause
        self.assertIn('not an', msg.lower())

    def test_non_forge_inbox_stall_has_no_slot_note(self):
        msg = ds._stall_dm_message({
            'kind': 'inbox-stall', 'agent': 'beacon',
            'file': 'x.json', 'age_hours': 3.1,
        })
        self.assertNotIn('build slot', msg.lower())


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout


class TwoSlotStallMessageTest(unittest.TestCase):
    """mirror-two-slot-review §4 PR2 audit: the 'single build slot' copy is
    Forge-scoped (Forge is not slot-scaled); Mirror gets the slot-agnostic copy;
    stale-lease messages render whatever identity is recorded, so slot leases
    (inbox:mirror:1) and head-leases (review-head:mirror:<sha>) surface with no
    per-slot enumeration. Pure over `_stall_dm_message` — no I/O."""

    def test_forge_inbox_stall_names_single_slot(self):
        msg = ds._stall_dm_message(
            {'kind': 'inbox-stall', 'agent': 'forge', 'file': 'b.json',
             'age_hours': 4})
        self.assertIn('single build slot', msg)

    def test_mirror_inbox_stall_omits_single_slot_copy(self):
        msg = ds._stall_dm_message(
            {'kind': 'inbox-stall', 'agent': 'mirror', 'file': 'r.json',
             'age_hours': 4})
        self.assertNotIn('single build slot', msg)

    def test_stale_lease_renders_slot_and_head_identities(self):
        for identity in ('inbox:mirror:1', 'review-head:mirror:cafe1234'):
            msg = ds._stall_dm_message(
                {'kind': 'stale-lease', 'agent': 'mirror',
                 'identity': identity, 'age_hours': 1})
            self.assertIn(identity, msg)


_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
