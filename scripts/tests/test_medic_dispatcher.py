"""Tests for scripts/medic_dispatcher.py (PR1 Medic scaffold).

Verbatim coverage of the brief's PR1 test list:

  * Dispatcher
    - owned-class filter: matched -> batched; unmatched -> left for
      Beacon, offset still advances past it correctly.
    - enable-flag off -> no-op.
    - kill-switch present (either file) -> no-op.
    - rate-window not-ok -> defer (no-op).
    - empty / no-qualifying batch -> fast exit, no operator invocation
      (the run_medic.sh subprocess is mocked and asserted unused).
    - Offset never regresses.
    - NEVER touches Beacon's offset file.

  * Config loaders
    - owned-classes + action-policy missing / malformed -> fail safe
      (WARN, treated as empty / fallback default), never raise.

  * Escalate-only contract
    - PR1 must NOT contain any mutating system call (no
      `systemctl restart/enable`, no `cp` to `/etc`, no re-dispatch
      writes) in the operator path. Asserted as a source-grep on the
      operator + dispatcher + run_medic.sh.

All subprocess / Claude-invocation / file IO is mocked. The tests
never spin a real Claude, never write to the real queue, never touch
real systemd.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import medic_dispatcher  # noqa: E402
import medic_ledger  # noqa: E402


def _alert(source: str, subject: str, severity: str = 'warning',
           message: str = 'test alert', suggested_action: str = '') -> dict:
    rec = {
        'ts': '2026-05-31T12:00:00+00:00',
        'source': source,
        'severity': severity,
        'message': message,
    }
    if subject:
        rec['subject'] = subject
    if suggested_action:
        rec['suggested_action'] = suggested_action
    return rec


def _write_alerts(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def _seed_default_owned_config(repo_dir: Path) -> None:
    """A minimal owned-classes config plus action-policy mirroring the
    shipping defaults. Tests that need to vary the config override the
    files directly."""
    cfg_dir = repo_dir / 'config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'medic-owned-classes.json').write_text(json.dumps({
        '$schema_version': '1',
        'owned_classes': [
            {'source': 'sentinel', 'subject_prefix': 'inbox-stall',
             'action_tier_hint': 'reversible'},
            {'source': 'watchdog', 'subject_prefix': '',
             'severity': 'critical', 'action_tier_hint': 'reversible'},
            {'source': 'heal-pipeline-stall', 'subject_prefix': 'pipeline-stall',
             'action_tier_hint': 'judgment'},
        ],
    }))
    (cfg_dir / 'medic-action-policy.json').write_text(json.dumps({
        '$schema_version': '1',
        'default_tier': 'judgment',
        'tiers': {'restart-daemon': 'reversible'},
    }))


class _IsolatedDispatcher(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT + OURLIBERTY_REPO_DIR to tmp
    dirs and reload the dispatcher so its module-level constants pick
    up the overrides. Sets the enable flag on by default so individual
    tests only need to clear it to test the off path. Disarms the
    rate-window stub's WARN by default (no behavior change; just less
    log noise).
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='medic-dispatcher-')
        self.agents_root = Path(self._tmpdir) / 'agents'
        self.repo_dir = Path(self._tmpdir) / 'agent-core'
        for sub in ('logs', 'state', 'blackboard'):
            (self.agents_root / sub).mkdir(parents=True, exist_ok=True)
        _seed_default_owned_config(self.repo_dir)
        # Stash + override env.
        self._env_snapshot = {
            'OURLIBERTY_AGENTS_ROOT': os.environ.get('OURLIBERTY_AGENTS_ROOT'),
            'OURLIBERTY_REPO_DIR': os.environ.get('OURLIBERTY_REPO_DIR'),
            'OURLIBERTY_MEDIC_ENABLED': os.environ.get('OURLIBERTY_MEDIC_ENABLED'),
        }
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        os.environ['OURLIBERTY_REPO_DIR'] = str(self.repo_dir)
        os.environ['OURLIBERTY_MEDIC_ENABLED'] = '1'
        importlib.reload(medic_dispatcher)
        importlib.reload(medic_ledger)
        # Re-reload dispatcher so it picks up the reloaded ledger module
        # rather than the stale reference cached from the prior reload.
        importlib.reload(medic_dispatcher)
        # Sanity-check paths picked up the env override.
        self.assertTrue(str(medic_dispatcher.AGENTS_ROOT).startswith(self._tmpdir))
        self.assertTrue(str(medic_dispatcher.REPO_DIR).startswith(self._tmpdir))
        # The rate-window gauges read live state (heal_claude_max_burn_rate
        # reads ~/agents/blackboard/anthropic-quota-events.jsonl;
        # active_tier reads ~/agents/blackboard/active-tier.json which is
        # NOT environment-isolated). Default both shims to a "cool window"
        # response so legacy tests stay deterministic regardless of the
        # host's real OAuth tier state. Tests that exercise the gauge
        # logic itself override these patchers.
        self._cool_event_patch = mock.patch.object(
            medic_dispatcher, '_recent_rate_limit_events', return_value=0)
        self._cool_cooldown_patch = mock.patch.object(
            medic_dispatcher, '_active_tier_in_rate_limit_cooldown',
            return_value=False)
        self._cool_event_patch.start()
        self._cool_cooldown_patch.start()

    def tearDown(self) -> None:
        self._cool_event_patch.stop()
        self._cool_cooldown_patch.stop()
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(medic_dispatcher)
        importlib.reload(medic_ledger)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # Helper: returns a mocked _invoke_operator that records its calls
    # so tests can assert run_medic.sh was (or was not) invoked.
    def _patch_invoke(self, returncode: int = 0):
        return mock.patch.object(
            medic_dispatcher, '_invoke_operator', return_value=returncode)


class OwnedClassFilterTest(_IsolatedDispatcher):
    def test_matched_alert_is_batched(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke, \
                mock.patch.object(medic_dispatcher, '_write_batch',
                                  wraps=medic_dispatcher._write_batch) as wb:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_called_once()
        wb.assert_called_once()
        # Batch payload contains exactly the owned alert.
        batch_arg = wb.call_args.args[0]
        self.assertEqual(len(batch_arg), 1)
        self.assertEqual(batch_arg[0]['source'], 'sentinel')
        self.assertEqual(batch_arg[0]['subject'], 'inbox-stall:agent-a')
        self.assertEqual(batch_arg[0]['owned_class']['action_tier_hint'],
                         'reversible')
        # Offset advanced past the consumed line.
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_unmatched_alert_passes_through_and_offset_advances(self) -> None:
        # An alert from a non-owned source. Beacon's existing path handles it.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('some-other-healer', 'foo:bar'),
        ])
        with self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_not_called()
        # Offset still advanced past the unowned line so future ticks
        # don't re-scan it.
        self.assertEqual(medic_dispatcher._read_offset(), 1)
        # Alert is still in the file -- Medic did NOT swallow it.
        lines = medic_dispatcher.ALERTS_FILE.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['source'], 'some-other-healer')

    def test_owned_and_unowned_mixed_only_owned_batched(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('some-other-healer', 'foo'),                  # unowned
            _alert('sentinel', 'inbox-stall:x'),                  # owned
            _alert('different-source', 'bar'),                    # unowned
            _alert('heal-pipeline-stall', 'pipeline-stall:abc'),  # owned
        ])
        with self._patch_invoke() as invoke, \
                mock.patch.object(medic_dispatcher, '_write_batch',
                                  wraps=medic_dispatcher._write_batch) as wb:
            medic_dispatcher.main([])
        invoke.assert_called_once()
        batch_arg = wb.call_args.args[0]
        self.assertEqual({b['source'] for b in batch_arg},
                         {'sentinel', 'heal-pipeline-stall'})
        # Offset advanced past all four lines.
        self.assertEqual(medic_dispatcher._read_offset(), 4)

    def test_severity_gate_on_watchdog(self) -> None:
        # The watchdog owned-class entry requires severity=critical.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('watchdog', 'forge-bot-silent', severity='warning'),  # NOT critical -> unowned
            _alert('watchdog', 'beacon-bot-silent', severity='critical'),  # owned
        ])
        with self._patch_invoke(), \
                mock.patch.object(medic_dispatcher, '_write_batch',
                                  wraps=medic_dispatcher._write_batch) as wb:
            medic_dispatcher.main([])
        batch_arg = wb.call_args.args[0]
        self.assertEqual(len(batch_arg), 1)
        self.assertEqual(batch_arg[0]['subject'], 'beacon-bot-silent')

    def test_notifications_and_approval_requests_are_never_owned(self) -> None:
        # The queue contains notifications + approval_requests written by
        # other producers; Medic must never own them (its own escalation
        # writes go through this path too).
        notify_rec = {
            'ts': '2026-05-31T12:00:00+00:00',
            'source': 'sentinel',
            'kind': 'notification',
            'intent': 'review-pass',
            'message': 'hi',
            'chat_id': 123,
        }
        approval_rec = {
            'ts': '2026-05-31T12:00:00+00:00',
            'source': 'sentinel',
            'kind': 'approval_request',
            'approval_id': 'foo',
            'chat_id': 123,
            'body': 'bar',
        }
        _write_alerts(medic_dispatcher.ALERTS_FILE, [notify_rec, approval_rec])
        with self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        # Offset advanced past both lines anyway (no qualifying alerts).
        self.assertEqual(medic_dispatcher._read_offset(), 2)


class GateTest(_IsolatedDispatcher):
    def test_enable_flag_off_is_noop(self) -> None:
        os.environ['OURLIBERTY_MEDIC_ENABLED'] = '0'
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_not_called()
        # Offset did NOT advance.
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_enable_flag_unset_is_noop(self) -> None:
        del os.environ['OURLIBERTY_MEDIC_ENABLED']
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_medic_kill_switch_blocks(self) -> None:
        medic_dispatcher.MEDIC_KILL_SWITCH.parent.mkdir(parents=True,
                                                       exist_ok=True)
        medic_dispatcher.MEDIC_KILL_SWITCH.touch()
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_shared_healers_kill_switch_blocks(self) -> None:
        medic_dispatcher.HEALERS_KILL_SWITCH.parent.mkdir(parents=True,
                                                         exist_ok=True)
        medic_dispatcher.HEALERS_KILL_SWITCH.touch()
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_rate_window_not_ok_defers(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.object(medic_dispatcher, '_rate_window_ok',
                               return_value=False), \
                self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        # Deferred -- offset must NOT have advanced.
        self.assertEqual(medic_dispatcher._read_offset(), 0)


class RateWindowGaugeTest(_IsolatedDispatcher):
    """End-to-end behavior of _rate_window_ok() against the two real
    gauges: heal_claude_max_burn_rate.recent_rate_limit_event_count and
    active_tier.cooldown_until. Both gauges are patched at the shim
    layer (_recent_rate_limit_events / _active_tier_in_rate_limit_cooldown)
    so the test never touches the real ledger or active-tier state.
    """

    def test_cool_window_proceeds_to_normal_path(self) -> None:
        # No events, no cooldown -> normal escalate path runs. The
        # _IsolatedDispatcher setUp defaults the shims to cool, so this
        # test verifies the inherited fixture wiring matches expectations.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_called_once()
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_hot_event_count_defers(self) -> None:
        # Event count over the default threshold -> defer. No batch,
        # no operator invocation, offset unchanged.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        events_over = medic_dispatcher.DEFAULT_RATE_WINDOW_MAX_EVENTS + 1
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               return_value=events_over), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=False), \
                self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_not_called()
        # Unprocessed owned alert must be retried next tick.
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_event_count_at_threshold_does_not_defer(self) -> None:
        # Boundary: events == threshold should NOT defer (we defer on
        # strict >); confirms threshold semantics.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        at_threshold = medic_dispatcher.DEFAULT_RATE_WINDOW_MAX_EVENTS
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               return_value=at_threshold), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=False), \
                self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_called_once()

    def test_active_tier_cooldown_defers(self) -> None:
        # No 429 events but a tier cooldown is active -> defer.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               return_value=0), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=True), \
                self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_not_called()
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_event_gauge_raises_fails_open(self) -> None:
        # A broken gauge must NOT permanently silence Medic: the
        # per-session timeout + lock are the real blast-radius bound.
        # Event shim raises -> _rate_window_ok() returns True (fail open).
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               side_effect=RuntimeError('gauge boom')), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=False), \
                self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        # Fail-open: gauge raise must NOT block dispatch.
        self.assertEqual(rc, 0)
        invoke.assert_called_once()
        # The gauge raised before _active_tier_in_rate_limit_cooldown got a
        # chance to run; the function returned True directly. Sanity-check
        # the helper in isolation too.
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               side_effect=RuntimeError('gauge boom')):
            self.assertTrue(medic_dispatcher._rate_window_ok())

    def test_active_tier_gauge_raises_fails_open(self) -> None:
        # Same fail-open contract for the active-tier gauge.
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               return_value=0), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  side_effect=OSError('cooldown read boom')):
            self.assertTrue(medic_dispatcher._rate_window_ok())

    def test_threshold_env_override(self) -> None:
        # Setting OURLIBERTY_MEDIC_RATE_WINDOW_MAX_EVENTS=0 makes any
        # nonzero event count a defer.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.dict(os.environ,
                             {medic_dispatcher.RATE_WINDOW_MAX_EVENTS_ENV_VAR:
                                  '0'}), \
                mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                                  return_value=1), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=False), \
                self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_threshold_env_malformed_uses_default(self) -> None:
        with mock.patch.dict(os.environ,
                             {medic_dispatcher.RATE_WINDOW_MAX_EVENTS_ENV_VAR:
                                  'not-an-int'}):
            self.assertEqual(medic_dispatcher._rate_window_max_events(),
                             medic_dispatcher.DEFAULT_RATE_WINDOW_MAX_EVENTS)


class EmptyBatchFastExitTest(_IsolatedDispatcher):
    def test_empty_queue_no_invocation(self) -> None:
        # Queue file does not exist at all.
        self.assertFalse(medic_dispatcher.ALERTS_FILE.exists())
        with self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_not_called()
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_queue_with_only_unowned_alerts_no_invocation(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('some-other-healer', 'foo'),
            _alert('different-source', 'bar'),
        ])
        with self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        # Offset still advances past unowned lines so we don't re-scan.
        self.assertEqual(medic_dispatcher._read_offset(), 2)


class OffsetDisciplineTest(_IsolatedDispatcher):
    def test_offset_never_regresses(self) -> None:
        # Manually plant a high offset.
        medic_dispatcher._write_offset(100)
        self.assertEqual(medic_dispatcher._read_offset(), 100)
        # An attempt to advance with a smaller value is a no-op.
        medic_dispatcher._advance_offset(5)
        self.assertEqual(medic_dispatcher._read_offset(), 100)

    def test_offset_advances_forward(self) -> None:
        medic_dispatcher._write_offset(3)
        medic_dispatcher._advance_offset(10)
        self.assertEqual(medic_dispatcher._read_offset(), 10)

    def test_dispatcher_never_writes_beacon_offset(self) -> None:
        # Beacon's offset path is hard-coded inside larry_alerts.py at
        # state/beacon-alerts-offset.txt. The dispatcher must never
        # touch it. Plant a sentinel value and verify it is unchanged
        # across a full main() invocation that DOES advance Medic's
        # offset.
        beacon_offset_file = self.agents_root / 'state' / 'beacon-alerts-offset.txt'
        beacon_offset_file.parent.mkdir(parents=True, exist_ok=True)
        beacon_offset_file.write_text('42')
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
            _alert('some-other-healer', 'foo'),
        ])
        with self._patch_invoke():
            medic_dispatcher.main([])
        # Beacon's offset is byte-for-byte unchanged.
        self.assertEqual(beacon_offset_file.read_text(), '42')
        # Medic's offset advanced through both lines.
        self.assertEqual(medic_dispatcher._read_offset(), 2)

    def test_malformed_line_does_not_block_advance(self) -> None:
        # Write a valid owned alert + a junk line + a valid unowned alert.
        path = medic_dispatcher.ALERTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(_alert('sentinel', 'inbox-stall:agent-a')) + '\n')
            f.write('not json at all\n')
            f.write(json.dumps(_alert('different-source', 'bar')) + '\n')
        with self._patch_invoke():
            medic_dispatcher.main([])
        # All three lines consumed.
        self.assertEqual(medic_dispatcher._read_offset(), 3)


class LedgerRecordingTest(_IsolatedDispatcher):
    def test_one_ledger_entry_per_owned_alert(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
            _alert('different-source', 'bar'),
            _alert('heal-pipeline-stall', 'pipeline-stall:xyz'),
        ])
        with self._patch_invoke():
            medic_dispatcher.main([])
        recs = medic_ledger.read_recent(50)
        self.assertEqual(len(recs), 2)
        fps = {r['fingerprint'] for r in recs}
        self.assertEqual(fps, {
            'sentinel:inbox-stall:agent-a',
            'heal-pipeline-stall:pipeline-stall:xyz',
        })
        for r in recs:
            self.assertEqual(r['outcome'], 'escalated')
            self.assertEqual(r['attempt'], 1)

    def test_no_double_record_when_operator_acted(self) -> None:
        # PR2 invariant: if medic_actions.py records an 'acted' entry for a
        # fingerprint DURING this run, the dispatcher must NOT also append an
        # 'escalated' entry for it. Simulate the operator acting by having the
        # mocked _invoke_operator write the acted record itself.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),   # operator will "act"
            _alert('heal-pipeline-stall', 'pipeline-stall:xyz'),  # escalate
        ])
        acted_fp = 'sentinel:inbox-stall:agent-a'

        def _fake_operator(_batch_path):
            medic_ledger.append_record(
                'sentinel', 'inbox-stall:agent-a', 'reversible', 'acted', 1,
                notes='acted by medic_actions this run')
            return 0

        with mock.patch.object(medic_dispatcher, '_invoke_operator',
                               side_effect=_fake_operator):
            medic_dispatcher.main([])

        recs = medic_ledger.read_recent(50)
        by_fp = {}
        for r in recs:
            by_fp.setdefault(r['fingerprint'], []).append(r['outcome'])
        # The acted fingerprint has exactly ONE record (the acted one) -- the
        # dispatcher skipped its post-record.
        self.assertEqual(by_fp[acted_fp], ['acted'])
        # The non-acted owned alert still gets its escalated record.
        self.assertEqual(by_fp['heal-pipeline-stall:pipeline-stall:xyz'],
                         ['escalated'])

    def test_attempt_counter_increments_on_recurrence(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke():
            medic_dispatcher.main([])
        # Same fingerprint appears again in a second tick.
        with open(medic_dispatcher.ALERTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(_alert('sentinel', 'inbox-stall:agent-a')) + '\n')
        with self._patch_invoke():
            medic_dispatcher.main([])
        recs = medic_ledger.read_recent(50)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]['attempt'], 1)
        self.assertEqual(recs[1]['attempt'], 2)


class ConfigLoaderFailSafeTest(_IsolatedDispatcher):
    def test_owned_classes_missing_means_no_alerts_owned(self) -> None:
        (self.repo_dir / 'config' / 'medic-owned-classes.json').unlink()
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke:
            # Must not raise.
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_not_called()

    def test_owned_classes_malformed_treated_as_empty(self) -> None:
        (self.repo_dir / 'config' / 'medic-owned-classes.json').write_text(
            '{not valid json')
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke() as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_not_called()

    def test_owned_classes_root_not_object_treated_as_empty(self) -> None:
        (self.repo_dir / 'config' / 'medic-owned-classes.json').write_text(
            json.dumps([1, 2, 3]))
        with self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()

    def test_owned_classes_entry_missing_source_skipped(self) -> None:
        (self.repo_dir / 'config' / 'medic-owned-classes.json').write_text(
            json.dumps({
                'owned_classes': [
                    {'subject_prefix': 'foo'},  # missing source -> skip
                    {'source': 'sentinel', 'subject_prefix': 'inbox-stall'},
                ],
            }))
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:x'),
        ])
        with self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_called_once()  # the second entry still loaded fine

    def test_action_policy_missing_uses_judgment_default(self) -> None:
        (self.repo_dir / 'config' / 'medic-action-policy.json').unlink()
        policy = medic_dispatcher._load_action_policy()
        self.assertEqual(policy['default_tier'], 'judgment')
        self.assertEqual(policy['tiers'], {})

    def test_action_policy_malformed_uses_fallback(self) -> None:
        (self.repo_dir / 'config' / 'medic-action-policy.json').write_text(
            '{not valid json')
        policy = medic_dispatcher._load_action_policy()
        self.assertEqual(policy['default_tier'], 'judgment')
        self.assertEqual(policy['tiers'], {})


class EscalateOnlyContractTest(unittest.TestCase):
    """PR1 must NOT contain any mutating system call in the operator
    path. Asserted as a source-grep over the operator + dispatcher +
    run_medic.sh. Guards against PR2 scope leaking into PR1.

    Patterns surveyed (matched verbatim, case-sensitive):

      - `systemctl restart`, `systemctl start`, `systemctl enable`,
        `systemctl reload`, `systemctl daemon-reload`
      - `cp ` / `mv ` into `/etc/`
      - `gh pr merge`, `gh pr create`, `gh pr close`
      - `safe_write_inbox` (the inbox re-dispatch primitive)
      - `dispatch_lease`, `acquire_lease` (chain re-dispatch path)

    The operator's bash allowlist (.claude/settings.json) is the hard
    runtime guard; this test is the source-level defense in depth.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parent.parent.parent
        cls.targets = [
            cls.repo_root / 'scripts' / 'medic_dispatcher.py',
            cls.repo_root / 'scripts' / 'run_medic.sh',
            cls.repo_root / 'scripts' / 'medic_ledger.py',
            cls.repo_root / 'agents' / 'medic' / 'CLAUDE.md',
        ]

    def _grep(self, needle: str) -> list[str]:
        hits: list[str] = []
        for path in self.targets:
            if not path.exists():
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            for line_idx, line in enumerate(text.splitlines(), start=1):
                # Skip comments + strings that name the forbidden command
                # only descriptively (the operator protocol enumerates
                # what it MUST NOT run -- those mentions are documentation,
                # not executions). Heuristic: skip lines whose first
                # non-whitespace char is '#', '*', or '-' (markdown),
                # and skip lines where the needle appears inside a quote.
                stripped = line.lstrip()
                if stripped.startswith(('#', '*', '-', '"', "'", '|', '`')):
                    continue
                if needle in line:
                    # Allow the deny: block in CLAUDE.md descriptions
                    # by skipping any line that also contains 'No ' or
                    # 'no ' as a negation marker -- the operator
                    # protocol uses that shape exclusively.
                    if ' No ' in line or line.lstrip().startswith('No '):
                        continue
                    hits.append(f'{path.name}:{line_idx}: {line.strip()}')
        return hits

    def test_no_systemctl_mutation(self) -> None:
        for needle in (
            'systemctl restart ',
            'systemctl start ',
            'systemctl enable ',
            'systemctl reload ',
            'systemctl daemon-reload',
        ):
            self.assertEqual(
                self._grep(needle), [],
                f'PR1 must not invoke `{needle.strip()}` in the operator path',
            )

    def test_no_etc_writes(self) -> None:
        for needle in ('cp ', 'mv '):
            for hit in self._grep(needle):
                self.assertNotIn(
                    '/etc/', hit,
                    f'PR1 must not copy/move into /etc/: {hit}',
                )

    def test_no_gh_pr_mutation(self) -> None:
        for needle in ('gh pr merge', 'gh pr create', 'gh pr close',
                       'gh pr edit'):
            self.assertEqual(
                self._grep(needle), [],
                f'PR1 must not run `{needle}`',
            )

    def test_no_inbox_redispatch(self) -> None:
        for needle in ('safe_write_inbox', 'acquire_lease'):
            self.assertEqual(
                self._grep(needle), [],
                f'PR1 must not call the chain re-dispatch primitive `{needle}`',
            )

    def test_act_branch_is_stubbed(self) -> None:
        # The dispatcher's batched payload must always record
        # outcome=escalated for PR1. (PR2 will branch on action tier
        # to acted vs escalated -- but not yet.)
        body = (Path(__file__).resolve().parent.parent
                / 'medic_dispatcher.py').read_text()
        self.assertIn("outcome='escalated'", body)
        self.assertNotIn("outcome='acted'", body)


if __name__ == '__main__':
    unittest.main()
