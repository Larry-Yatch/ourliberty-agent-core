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
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import larry_alerts  # noqa: E402
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
        # The delivery-failure meta-alert path calls larry_alerts.append_alert,
        # whose module-level paths are NOT env-isolated. Redirect them at the
        # same tmp queue so a test that exercises the give-up path never writes
        # the real ~/agents queue. medic_dispatcher.ALERTS_FILE already points
        # here via the env override, so both modules share one tmp file.
        self._la_paths = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', self.agents_root),
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              medic_dispatcher.ALERTS_FILE),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              self.agents_root / 'state' / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              self.agents_root / 'state'
                              / 'beacon-alerts-offset.txt'),
        ]
        for p in self._la_paths:
            p.start()

    def tearDown(self) -> None:
        for p in self._la_paths:
            p.stop()
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
    # so tests can assert run_medic.sh was (or was not) invoked. Note this
    # variant delivers NOTHING -- under delivery-gating an owned alert run
    # through it is recorded escalate-failed. Use _patch_invoke_delivering
    # when the test expects the operator to deliver an escalation.
    def _patch_invoke(self, returncode: int = 0):
        return mock.patch.object(
            medic_dispatcher, '_invoke_operator', return_value=returncode)

    def _patch_invoke_delivering(self, fingerprints, returncode: int = 0):
        """Patch _invoke_operator with a fake operator that simulates the Medic
        operator delivering an escalation: it appends a source='medic'
        notification embedding each fingerprint to the queue -- the same signal
        the dispatcher diff-matches on to confirm delivery."""
        def _op(_batch_path):
            with open(medic_dispatcher.ALERTS_FILE, 'a', encoding='utf-8') as f:
                for fp in fingerprints:
                    f.write(json.dumps({
                        'ts': '2026-06-02T00:00:00+00:00',
                        'source': 'medic',
                        'kind': 'notification',
                        'intent': 'medic-diagnosis',
                        'message': f'Diagnose-only for fingerprint {fp}.',
                        'chat_id': 123,
                    }) + '\n')
            return returncode
        return mock.patch.object(
            medic_dispatcher, '_invoke_operator', side_effect=_op)


class OwnedClassFilterTest(_IsolatedDispatcher):
    def test_matched_alert_is_batched(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke_delivering(
                ['sentinel:inbox-stall:agent-a']) as invoke, \
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
        with self._patch_invoke_delivering(
                ['sentinel:inbox-stall:x',
                 'heal-pipeline-stall:pipeline-stall:abc']) as invoke, \
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
        with self._patch_invoke_delivering(
                ['sentinel:inbox-stall:agent-a']) as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_called_once()
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_high_soft_event_count_no_cooldown_proceeds(self) -> None:
        # The 2026-06-02 incident shape: a high soft 429-event count (200 --
        # dominated by Beacon's normal chat/opus chatter) with NO hard
        # cooldown must NOT defer. The soft count is advisory-only now; the
        # hard cooldown is the sole deferral trigger.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               return_value=200), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=False), \
                self._patch_invoke_delivering(
                    ['sentinel:inbox-stall:agent-a']) as invoke:
            rc = medic_dispatcher.main([])
        self.assertEqual(rc, 0)
        invoke.assert_called_once()
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_hard_cooldown_defers_even_with_low_event_count(self) -> None:
        # Cooldown is PRIMARY: zero soft events but an active tier cooldown
        # still defers. (Complements test_active_tier_cooldown_defers by
        # asserting the ordering explicitly.)
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               return_value=0), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=True), \
                self._patch_invoke() as invoke:
            medic_dispatcher.main([])
        invoke.assert_not_called()
        self.assertEqual(medic_dispatcher._read_offset(), 0)

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

    def test_event_gauge_raises_does_not_block(self) -> None:
        # The soft 429-event gauge is advisory-only: a raise from it must NOT
        # block dispatch (and must not even be observed as a gate). The
        # cooldown gate is cool, so the tick proceeds normally.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               side_effect=RuntimeError('gauge boom')), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  return_value=False), \
                self._patch_invoke_delivering(
                    ['sentinel:inbox-stall:agent-a']) as invoke:
            rc = medic_dispatcher.main([])
        # Advisory gauge raise must NOT block dispatch.
        self.assertEqual(rc, 0)
        invoke.assert_called_once()
        self.assertEqual(medic_dispatcher._read_offset(), 1)
        # Sanity-check the helper in isolation: cooldown cool (setUp shim) +
        # advisory event gauge raising -> still ok.
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               side_effect=RuntimeError('gauge boom')):
            self.assertTrue(medic_dispatcher._rate_window_ok())

    def test_active_tier_gauge_raises_fails_open(self) -> None:
        # The cooldown gauge is the sole gate; a raise from it must fail open
        # so a broken gauge cannot permanently silence Medic.
        with mock.patch.object(medic_dispatcher, '_recent_rate_limit_events',
                               return_value=0), \
                mock.patch.object(medic_dispatcher,
                                  '_active_tier_in_rate_limit_cooldown',
                                  side_effect=OSError('cooldown read boom')):
            self.assertTrue(medic_dispatcher._rate_window_ok())


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
        with self._patch_invoke_delivering(['sentinel:inbox-stall:agent-a']):
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
        with self._patch_invoke_delivering(['sentinel:inbox-stall:agent-a']):
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
        with self._patch_invoke_delivering([
                'sentinel:inbox-stall:agent-a',
                'heal-pipeline-stall:pipeline-stall:xyz']):
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
            # Simulate medic_actions.py recording the authoritative 'acted'
            # record for the first fingerprint (no notification needed -- the
            # act path is the delivery), and the operator delivering a normal
            # escalation notification for the second.
            medic_ledger.append_record(
                'sentinel', 'inbox-stall:agent-a', 'reversible', 'acted', 1,
                notes='acted by medic_actions this run')
            with open(medic_dispatcher.ALERTS_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'ts': '2026-06-02T00:00:00+00:00',
                    'source': 'medic',
                    'kind': 'notification',
                    'intent': 'medic-diagnosis',
                    'message': ('Diagnose-only for fingerprint '
                                'heal-pipeline-stall:pipeline-stall:xyz.'),
                    'chat_id': 123,
                }) + '\n')
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
        with self._patch_invoke_delivering(['sentinel:inbox-stall:agent-a']):
            medic_dispatcher.main([])
        # Same fingerprint appears again in a second tick.
        with open(medic_dispatcher.ALERTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(_alert('sentinel', 'inbox-stall:agent-a')) + '\n')
        with self._patch_invoke_delivering(['sentinel:inbox-stall:agent-a']):
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


class DeliveryGatingTest(_IsolatedDispatcher):
    """Gate-at-emission: an owned alert is recorded 'escalated' only when a
    matching source='medic' record actually landed in the queue this run.
    Otherwise it is 'escalate-failed' and its offset line is held for retry.
    """

    def test_delivered_owned_alert_records_escalated(self) -> None:
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke_delivering(['sentinel:inbox-stall:agent-a']):
            medic_dispatcher.main([])
        recs = medic_ledger.read_recent(50)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['outcome'], 'escalated')
        self.assertEqual(recs[0]['fingerprint'], 'sentinel:inbox-stall:agent-a')
        # Delivered -> offset advances past the line.
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_undelivered_owned_alert_escalate_failed_and_holds_offset(self) -> None:
        # The operator runs but delivers NOTHING (broken notify path). The
        # owned alert must be recorded escalate-failed and the offset must NOT
        # advance past it, so the next tick retries.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with self._patch_invoke():  # delivers nothing
            medic_dispatcher.main([])
        recs = medic_ledger.read_recent(50)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['outcome'], 'escalate-failed')
        # Offset held at 0 -- the line retries next tick.
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_partial_delivery_holds_offset_at_first_undelivered(self) -> None:
        # Two owned alerts: the first delivers, the second does not. The
        # offset advances past the delivered line but is held at the
        # undelivered one (line 1), so only the failure retries.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:a'),     # line 0 -> delivered
            _alert('sentinel', 'inbox-stall:b'),     # line 1 -> NOT delivered
        ])
        with self._patch_invoke_delivering(['sentinel:inbox-stall:a']):
            medic_dispatcher.main([])
        recs = medic_ledger.read_recent(50)
        by_fp = {r['fingerprint']: r['outcome'] for r in recs}
        self.assertEqual(by_fp['sentinel:inbox-stall:a'], 'escalated')
        self.assertEqual(by_fp['sentinel:inbox-stall:b'], 'escalate-failed')
        # Offset clamped to the first undelivered line (index 1).
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_acted_exclusion_preserved_under_delivery_gating(self) -> None:
        # A fingerprint medic_actions.py recorded 'acted' this run is handled
        # via the act path (no notification needed) and must NOT also get an
        # escalate-failed record from the dispatcher.
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])

        def _acting_operator(_batch_path):
            medic_ledger.append_record(
                'sentinel', 'inbox-stall:agent-a', 'reversible', 'acted', 1,
                notes='acted by medic_actions this run')
            return 0

        with mock.patch.object(medic_dispatcher, '_invoke_operator',
                               side_effect=_acting_operator):
            medic_dispatcher.main([])
        recs = medic_ledger.read_recent(50)
        outcomes = [r['outcome'] for r in recs
                    if r['fingerprint'] == 'sentinel:inbox-stall:agent-a']
        self.assertEqual(outcomes, ['acted'])
        # Acted is handled -> offset advances.
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_repeated_failure_emits_meta_alert_and_advances(self) -> None:
        # After ESCALATE_FAILED_RETRY_LIMIT consecutive delivery failures for a
        # fingerprint, the dispatcher emits one meta-alert and lets the offset
        # advance past the line so it cannot wedge the cursor forever.
        fp = 'sentinel:inbox-stall:agent-a'
        # Pre-seed (LIMIT - 1) prior escalate-failed records so this run is the
        # one that crosses the threshold.
        for _ in range(medic_dispatcher.ESCALATE_FAILED_RETRY_LIMIT - 1):
            medic_ledger.append_record(
                'sentinel', 'inbox-stall:agent-a', 'reversible',
                'escalate-failed', 1, notes='prior failure')
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])
        with mock.patch.object(medic_dispatcher,
                               '_emit_delivery_failure_meta_alert') as meta, \
                self._patch_invoke():  # still delivers nothing
            medic_dispatcher.main([])
        meta.assert_called_once()
        # Auto-retry exhausted -> offset advances past the wedged line.
        self.assertEqual(medic_dispatcher._read_offset(), 1)
        # The crossing run still recorded an escalate-failed row.
        failed = [r for r in medic_ledger.read_recent(50)
                  if r['outcome'] == 'escalate-failed']
        self.assertEqual(len(failed),
                         medic_dispatcher.ESCALATE_FAILED_RETRY_LIMIT)

    def test_failed_restart_still_escalates_not_silently_handled(self) -> None:
        # Audit M2: a reversible-owned daemon restart that ran but did NOT
        # verify records 'acted-failed' (NOT 'acted'). The dispatcher must NOT
        # treat that as handled -- with the operator delivering no escalation,
        # the fingerprint must be recorded 'escalate-failed' and its offset
        # line HELD for retry, exactly as an undelivered escalation would be.
        # (Before the fix the failed restart recorded 'acted', the dispatcher's
        # newly_acted skip swallowed it, and the still-down daemon's first
        # escalation was silently dropped while the offset advanced.)
        fp = 'sentinel:inbox-stall:agent-a'
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])

        def _failed_restart_operator(_batch_path):
            # Simulate medic_actions.py recording an attempted-but-unverified
            # restart, then NOT delivering any source='medic' escalation.
            medic_ledger.append_record(
                'sentinel', 'inbox-stall:agent-a', 'reversible',
                'acted-failed', 1, notes='restart ran but did not verify')
            return 0

        with mock.patch.object(medic_dispatcher, '_invoke_operator',
                               side_effect=_failed_restart_operator):
            medic_dispatcher.main([])

        recs = medic_ledger.read_recent(50)
        outcomes = [r['outcome'] for r in recs if r['fingerprint'] == fp]
        # The acted-failed row from the operator PLUS a dispatcher
        # escalate-failed row (the failed restart was NOT treated as handled).
        self.assertIn('acted-failed', outcomes)
        self.assertIn('escalate-failed', outcomes)
        # Offset HELD at 0 -- the still-down daemon's line retries next tick.
        self.assertEqual(medic_dispatcher._read_offset(), 0)

    def test_failed_restart_that_does_escalate_records_escalated(self) -> None:
        # Companion to the above: if the operator records 'acted-failed' AND
        # delivers a diagnose-only escalation (the CLAUDE.md ok=False fallback),
        # the dispatcher confirms delivery and records 'escalated', advancing.
        fp = 'sentinel:inbox-stall:agent-a'
        _write_alerts(medic_dispatcher.ALERTS_FILE, [
            _alert('sentinel', 'inbox-stall:agent-a'),
        ])

        def _failed_then_escalating_operator(_batch_path):
            medic_ledger.append_record(
                'sentinel', 'inbox-stall:agent-a', 'reversible',
                'acted-failed', 1, notes='restart ran but did not verify')
            with open(medic_dispatcher.ALERTS_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'ts': '2026-06-02T00:00:00+00:00',
                    'source': 'medic',
                    'kind': 'notification',
                    'intent': 'medic-diagnosis',
                    'message': (f'Diagnose-only for fingerprint {fp}. Restart '
                                'ran but unit did not return to active.'),
                    'chat_id': 123,
                }) + '\n')
            return 0

        with mock.patch.object(medic_dispatcher, '_invoke_operator',
                               side_effect=_failed_then_escalating_operator):
            medic_dispatcher.main([])

        recs = medic_ledger.read_recent(50)
        outcomes = [r['outcome'] for r in recs if r['fingerprint'] == fp]
        self.assertIn('escalated', outcomes)
        self.assertNotIn('escalate-failed', outcomes)
        # Delivered -> offset advances past the line.
        self.assertEqual(medic_dispatcher._read_offset(), 1)

    def test_meta_alert_routes_to_non_owned_source(self) -> None:
        # The real meta-alert helper must write source='medic-dispatcher'
        # (NOT a Medic-owned class) so it cannot loop back into Medic's batch.
        entry = {
            'fingerprint': 'sentinel:inbox-stall:agent-a',
            'subject': 'inbox-stall:agent-a',
            'source': 'sentinel',
        }
        medic_dispatcher._emit_delivery_failure_meta_alert(entry, 3)
        lines = medic_dispatcher.ALERTS_FILE.read_text().strip().splitlines()
        rec = json.loads(lines[-1])
        self.assertEqual(rec['source'], 'medic-dispatcher')
        self.assertEqual(rec['subject'], 'sentinel:inbox-stall:agent-a')
        # Not owned by Medic -> _match_owned returns None.
        owned = medic_dispatcher._load_owned_classes()
        self.assertIsNone(medic_dispatcher._match_owned(rec, owned))


class DeliveryFingerprintMatchTest(unittest.TestCase):
    """nervous-system-audit #4 (2026-06-05): delivery confirmation matches the
    fingerprint as a boundary-delimited token, so a short fingerprint can't be
    falsely confirmed by the escalation text of a longer one it prefixes (which
    would silently drop the short alert — recorded escalated, never retried)."""

    def test_prefix_fingerprint_not_falsely_delivered(self):
        # Only the LONGER fingerprint was escalated. The shorter one (a prefix
        # of it) must NOT be reported delivered.
        short = 'pipeline-stall:forge-no-pr'
        long = 'pipeline-stall:forge-no-pr:task-42'
        records = [{'message': f'Diagnose-only for fingerprint {long}.'}]
        delivered = medic_dispatcher._delivered_fingerprints(
            records, {short, long},
        )
        self.assertEqual(delivered, {long})

    def test_exact_fingerprint_delivered(self):
        fp = 'sentinel:inbox-stall:agent-a'
        records = [{'message': f'Diagnose-only for fingerprint {fp}.'}]
        self.assertEqual(
            medic_dispatcher._delivered_fingerprints(records, {fp}),
            {fp},
        )

    def test_both_distinct_fingerprints_delivered(self):
        a = 'sentinel:inbox-stall:a'
        b = 'sentinel:inbox-stall:b'
        records = [
            {'message': f'Acted on {a} ok.'},
            {'body': f'Approval request for {b} pending.'},
        ]
        self.assertEqual(
            medic_dispatcher._delivered_fingerprints(records, {a, b}),
            {a, b},
        )

    def test_approval_id_structural_match_confirms_exact_fp(self):
        # approval_id `medic-<fp>-<ts>` confirms delivery for the approval tier
        # (where the fp may only be guaranteed in this templated field) — via a
        # STRUCTURAL match, not a substring scan.
        fp = 'sentinel:inbox-stall:agent-a'
        records = [{'approval_id': f'medic-{fp}-20260605T000000Z'}]
        self.assertEqual(
            medic_dispatcher._delivered_fingerprints(records, {fp}),
            {fp},
        )

    def test_approval_id_does_not_confirm_prefix_fp(self):
        # The longer fp's approval_id must NOT confirm a shorter prefix fp —
        # the substring false-positive stays closed in approval_id too.
        short = 'pipeline-stall:forge-no-pr'
        long = 'pipeline-stall:forge-no-pr:task-42'
        records = [{'approval_id': f'medic-{long}-20260605T000000Z'}]
        self.assertEqual(
            medic_dispatcher._delivered_fingerprints(records, {short, long}),
            {long},
        )

    def test_fp_in_approval_id_helper(self):
        f = medic_dispatcher._fp_in_approval_id
        self.assertTrue(f('a:b', 'medic-a:b-1733000000'))
        self.assertTrue(f('a-b:c-d', 'medic-a-b:c-d-20260605T000000Z'))
        self.assertFalse(f('a:b', 'medic-a:b-c-1733000000'))   # longer fp
        self.assertFalse(f('a:b', 'notmedic-a:b-1733000000'))  # wrong prefix
        self.assertFalse(f('a:b', 'medic-a:b'))                # no -ts segment

    def test_fp_token_boundary_helper(self):
        f = medic_dispatcher._fp_token_in_text
        # bounded by delimiters -> match
        self.assertTrue(f('a:b', 'see a:b here'))
        self.assertTrue(f('a:b', 'a:b'))
        self.assertTrue(f('a:b', 'ends with a:b'))
        self.assertTrue(f('a:b', '(a:b)'))
        # substring of a longer fingerprint token -> no match
        self.assertFalse(f('a:b', 'a:b-c is longer'))
        self.assertFalse(f('a:b', 'prefix a:bcd'))
        self.assertFalse(f('a:b', 'xa:b leading'))


class MedicAllowlistShapeTest(unittest.TestCase):
    """The Medic operator allowlist must permit ONLY the append_notification
    and append_approval_request subcommands of larry_alerts.py -- never
    append_alert (which would loop Medic back into its own batch) -- and the
    legacy inline `python3 -c` escalation entry must be gone."""

    @classmethod
    def setUpClass(cls) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        settings = repo_root / 'agents' / 'medic' / '.claude' / 'settings.json'
        cls.allow = json.loads(settings.read_text())['permissions']['allow']

    def test_append_notification_permitted(self) -> None:
        self.assertIn(
            'Bash(python3 /home/larry/agent-core/scripts/larry_alerts.py '
            'append_notification:*)',
            self.allow)

    def test_append_approval_request_permitted(self) -> None:
        self.assertIn(
            'Bash(python3 /home/larry/agent-core/scripts/larry_alerts.py '
            'append_approval_request:*)',
            self.allow)

    def test_append_alert_not_permitted(self) -> None:
        for entry in self.allow:
            self.assertNotIn('larry_alerts.py append_alert', entry)
        # The broad entry that would permit ANY larry_alerts subcommand
        # (including append_alert) must be gone.
        self.assertNotIn(
            'Bash(python3 /home/larry/agent-core/scripts/larry_alerts.py:*)',
            self.allow)

    def test_no_inline_python_c_larry_alerts_entry(self) -> None:
        # The removed broad inline form for larry_alerts must be gone. The
        # out-of-scope medic_actions inline entry is intentionally untouched,
        # so scope the check to larry_alerts entries only.
        self.assertNotIn(
            'Bash(python3 -c import sys; sys.path.insert*larry_alerts*)',
            self.allow)
        for entry in self.allow:
            if 'larry_alerts' in entry:
                self.assertNotIn('-c import sys', entry)


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

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
