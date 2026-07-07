"""Tests for scripts/medic_actions.py (PR2 reversible-action handlers).

Verbatim coverage of the PR2 acceptance list:

  - target NOT in allowlist -> refuse, outcome='skipped', NO subprocess,
    not-permitted result.
  - recurrence: a prior outcome='acted' ledger record for the fingerprint
    -> refuse, no subprocess, already-acted result.
  - any of the three gates set/off -> refuse, no subprocess.
  - restart success: allowlisted + gates ok + no prior act; mocked restart
    returns 0 and is-active='active' -> action runs, verify passes, ledger
    gets outcome='acted', success result.
  - verify fails: restart returns 0 but is-active != 'active' -> FAILURE
    result (not success), surfaced. Plus restart-error (non-zero rc).
  - privileged/judgment action types never reach restart_daemon /
    retrigger_inbox (the CLI rejects them; no subprocess).
  - malformed / missing config/medic-reversible-targets.json -> empty
    allowlist, every target refused, never raises.

ALL subprocess / systemctl / sudo / file IO is isolated: the two
subprocess shims (_run_restart / _is_active) are patched, and the ledger
path is redirected to a tmp OURLIBERTY_AGENTS_ROOT. The tests never run a
real systemctl, never touch the real ledger, never touch real systemd.
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import medic_actions  # noqa: E402
import medic_ledger  # noqa: E402

# An allowlisted unit + a fingerprint the operator would pass in.
ALLOWED_DAEMON = 'ourliberty-inbox-watcher.service'
ALLOWED_OUTBOX = 'ourliberty-outbox-notifier.service'
ALERT_FP = 'watchdog:ourliberty-inbox-watcher.service'


def _seed_targets(repo_dir: Path,
                  restart_units=(ALLOWED_DAEMON, ALLOWED_OUTBOX),
                  retrigger_targets=(ALLOWED_DAEMON,)) -> None:
    cfg_dir = repo_dir / 'config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'medic-reversible-targets.json').write_text(json.dumps({
        '$schema_version': '1',
        'restart_daemon_units': list(restart_units),
        'retrigger_inbox_targets': list(retrigger_targets),
    }))


class _IsolatedActions(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT + OURLIBERTY_REPO_DIR to tmp dirs and
    reload the modules so their constants pick up the override. Enable flag
    on by default; kill switches absent by default."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix='medic-actions-')
        self.agents_root = Path(self._tmpdir) / 'agents'
        self.repo_dir = Path(self._tmpdir) / 'agent-core'
        for sub in ('logs', 'state'):
            (self.agents_root / sub).mkdir(parents=True, exist_ok=True)
        _seed_targets(self.repo_dir)
        self._env_snapshot = {
            'OURLIBERTY_AGENTS_ROOT': os.environ.get('OURLIBERTY_AGENTS_ROOT'),
            'OURLIBERTY_REPO_DIR': os.environ.get('OURLIBERTY_REPO_DIR'),
            'OURLIBERTY_MEDIC_ENABLED': os.environ.get('OURLIBERTY_MEDIC_ENABLED'),
        }
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.agents_root)
        os.environ['OURLIBERTY_REPO_DIR'] = str(self.repo_dir)
        os.environ['OURLIBERTY_MEDIC_ENABLED'] = '1'
        importlib.reload(medic_ledger)
        importlib.reload(medic_actions)
        # Re-reload so medic_actions binds the freshly-reloaded ledger module.
        importlib.reload(medic_actions)
        self.assertTrue(str(medic_actions.AGENTS_ROOT).startswith(self._tmpdir))
        self.assertTrue(str(medic_actions.REPO_DIR).startswith(self._tmpdir))

    def tearDown(self) -> None:
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(medic_ledger)
        importlib.reload(medic_actions)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _patch_subprocess(self, rc: int = 0, active: str = 'active'):
        """Patch both shims; return (restart_mock, isactive_mock) context."""
        return (
            mock.patch.object(medic_actions, '_run_restart', return_value=rc),
            mock.patch.object(medic_actions, '_is_active', return_value=active),
        )

    def _ledger_records(self) -> list:
        return medic_ledger.read_recent(100)


class AllowlistTest(_IsolatedActions):
    def test_target_not_in_allowlist_is_refused(self) -> None:
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p as active_m:
            res = medic_actions.restart_daemon(
                'ourliberty-not-a-real-unit.service', ALERT_FP, attempt=1)
        self.assertFalse(res['ok'])
        self.assertEqual(res['outcome'], 'skipped')
        self.assertEqual(res['reason'], 'not-permitted')
        # No subprocess invoked.
        restart_m.assert_not_called()
        active_m.assert_not_called()
        # Recorded as skipped (no 'acted' record).
        recs = self._ledger_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['outcome'], 'skipped')

    def test_retrigger_target_not_in_allowlist_is_refused(self) -> None:
        # The outbox notifier is allowed for restart-daemon but NOT in the
        # retrigger_inbox allowlist -> retrigger must refuse it.
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.retrigger_inbox(ALLOWED_OUTBOX, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'not-permitted')
        restart_m.assert_not_called()


class RecurrenceGateTest(_IsolatedActions):
    def test_prior_acted_record_blocks_second_action(self) -> None:
        # Seed an 'acted' record whose fingerprint matches what the handler
        # will look up (handler keys on the passed-in fingerprint string).
        src, subj = medic_actions._fp_parts(ALERT_FP)
        medic_ledger.append_record(
            source=src, subject=subj, classification='reversible',
            outcome='acted', attempt=1, notes='prior act')
        self.assertTrue(medic_ledger.has_acted(ALERT_FP))
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p as active_m:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'already-acted')
        self.assertEqual(res['outcome'], 'skipped')
        restart_m.assert_not_called()
        active_m.assert_not_called()


class GateTest(_IsolatedActions):
    def test_enable_flag_off_refuses(self) -> None:
        os.environ['OURLIBERTY_MEDIC_ENABLED'] = '0'
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p as active_m:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'gate-enable-flag-off')
        restart_m.assert_not_called()
        active_m.assert_not_called()

    def test_medic_kill_switch_refuses(self) -> None:
        medic_actions.MEDIC_KILL_SWITCH.parent.mkdir(parents=True,
                                                     exist_ok=True)
        medic_actions.MEDIC_KILL_SWITCH.touch()
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'gate-medic-kill-switch')
        restart_m.assert_not_called()

    def test_healers_kill_switch_refuses(self) -> None:
        medic_actions.HEALERS_KILL_SWITCH.parent.mkdir(parents=True,
                                                       exist_ok=True)
        medic_actions.HEALERS_KILL_SWITCH.touch()
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.retrigger_inbox(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'gate-healers-kill-switch')
        restart_m.assert_not_called()


class SilenceFalsePositiveTest(_IsolatedActions):
    """silence-false-positive handler: reversible, allowlist-gated, and
    coupled with exactly ONE Approve/Reject decision on Larry's Approvals
    tab. The 2026-06-04 forge-no-pr backstop -- Medic durably quiets a
    confirmed benign fingerprint instead of escalating attempt N+1 to Larry,
    but posts a one-time decision (keep silenced vs unsilence) so the unfixed
    root cause is not silently masked."""

    SILENCEABLE_FP = ('heal-pipeline-stall:pipeline-stall:forge-no-pr:'
                      'forge-queue-api-preflight-20260603T231401Z-clarify1')

    def setUp(self) -> None:
        super().setUp()
        # Re-seed the allowlist with a silenceable_subjects pattern.
        (self.repo_dir / 'config' / 'medic-reversible-targets.json').write_text(
            json.dumps({
                '$schema_version': '1',
                'restart_daemon_units': [ALLOWED_DAEMON],
                'retrigger_inbox_targets': [ALLOWED_DAEMON],
                'silenceable_subjects': ['pipeline-stall:forge-no-pr:'],
            }))
        # Isolate larry_alerts paths into the same tmp tree so silence files
        # never touch the real ~/agents.
        la = medic_actions.larry_alerts
        self._la_patches = [
            mock.patch.object(la, 'AGENTS_ROOT', self.agents_root),
            mock.patch.object(la, 'ALERTS_FILE',
                              self.agents_root / 'blackboard' / 'larry-alerts.jsonl'),
            mock.patch.object(la, 'COOLDOWN_ROOT',
                              self.agents_root / 'state' / 'alert-cooldown'),
            mock.patch.object(la, 'SILENCE_ROOT',
                              self.agents_root / 'state' / 'alert-silenced'),
        ]
        # SAFETY: stub the Approvals-tab emitter so no test ever writes a real
        # chain_events row to production Supabase. Captured for assertions.
        import chain_event_emit
        self._emit_patch = mock.patch.object(
            chain_event_emit, 'emit_event', return_value=True)
        self._emit_mock = self._emit_patch.start()
        for p in self._la_patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._la_patches:
            p.stop()
        self._emit_patch.stop()
        super().tearDown()

    def _decisions(self) -> list:
        """The payload kwargs of every approval_request emitted this test."""
        return [c.kwargs for c in self._emit_mock.call_args_list]

    def test_silences_allowlisted_fingerprint_and_suppresses_alert(self) -> None:
        la = medic_actions.larry_alerts
        res = medic_actions.silence_false_positive(
            self.SILENCEABLE_FP, reason='build shipped as PR #294')
        self.assertTrue(res['ok'])
        self.assertEqual(res['reason'], 'silenced')
        self.assertTrue(la.is_silenced(self.SILENCEABLE_FP))
        # The healer's next append_alert for this fingerprint is now dropped.
        src, subj = self.SILENCEABLE_FP.split(':', 1)
        self.assertFalse(la.append_alert(src, 'warning', 'stall', subject=subj))
        # Ledger shows an 'acted' record (reversible classification).
        recs = self._ledger_records()
        self.assertEqual(recs[-1]['outcome'], 'acted')
        self.assertEqual(recs[-1]['classification'], 'reversible')

    def test_zero_ttl_silence_does_not_false_report_write_failure(self) -> None:
        # ttl_sec=0 is an immediately-expired (no-op) silence, so is_silenced is
        # legitimately False afterward. The verify step must NOT read that as a
        # 'silence-write-failed' refusal — the write itself succeeded.
        res = medic_actions.silence_false_positive(
            self.SILENCEABLE_FP, reason='zero ttl', ttl_sec=0)
        self.assertTrue(res['ok'], res)
        self.assertNotEqual(res['reason'], 'silence-write-failed')
        # The file was written even though the silence is already expired.
        self.assertTrue(medic_actions.larry_alerts._silence_path(
            self.SILENCEABLE_FP).exists())

    def test_fingerprint_not_in_allowlist_is_refused(self) -> None:
        res = medic_actions.silence_false_positive(
            'watchdog:disk-full-critical', reason='nope')
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'not-permitted')
        self.assertFalse(
            medic_actions.larry_alerts.is_silenced('watchdog:disk-full-critical'))
        self.assertEqual(self._ledger_records()[-1]['outcome'], 'skipped')

    def test_idempotent_second_silence(self) -> None:
        medic_actions.silence_false_positive(self.SILENCEABLE_FP, reason='a')
        res = medic_actions.silence_false_positive(self.SILENCEABLE_FP, reason='b')
        self.assertTrue(res['ok'])
        self.assertEqual(res['reason'], 'already-silenced')

    def test_enable_flag_off_refuses_silence(self) -> None:
        os.environ['OURLIBERTY_MEDIC_ENABLED'] = '0'
        res = medic_actions.silence_false_positive(self.SILENCEABLE_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'gate-enable-flag-off')
        self.assertFalse(medic_actions.larry_alerts.is_silenced(self.SILENCEABLE_FP))

    def test_empty_fingerprint_refused(self) -> None:
        res = medic_actions.silence_false_positive('', reason='x')
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'no-target')

    def test_cli_silence_returns_zero_on_success(self) -> None:
        rc = medic_actions._main(
            ['silence-false-positive', '--fingerprint', self.SILENCEABLE_FP,
             '--reason', 'shipped'])
        self.assertEqual(rc, 0)
        self.assertTrue(
            medic_actions.larry_alerts.is_silenced(self.SILENCEABLE_FP))

    def test_silence_posts_one_approval_decision(self) -> None:
        """Silencing must not be silent: it couples with exactly one
        Approve/Reject decision on the Approvals tab (an approval_request
        chain_event from agent='medic'), carrying the operator's remediation
        text and the silenced fingerprint in the decision prompt."""
        res = medic_actions.silence_false_positive(
            self.SILENCEABLE_FP, reason='build shipped as PR #294',
            remediation='Add retry-sibling reconciliation to '
                        'heal_pipeline_stall.py _forge_pr_create_inferred_failure')
        self.assertTrue(res['ok'])
        self.assertIn('Approvals tab', res['detail'])
        decisions = self._decisions()
        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertEqual(d['event_type'], 'approval_request')
        self.assertEqual(d['agent'], 'medic')
        payload = d['payload']
        self.assertEqual(payload['proposing_agent'], 'medic')
        self.assertIn('retry-sibling reconciliation', payload['prompt'])
        self.assertIn(self.SILENCEABLE_FP, payload['prompt'])
        # Reject envelope must carry the unsilence instruction; approve must not.
        self.assertIn('unsilence',
                      payload['suggested_envelope_for_reject']['prompt'])
        self.assertIn('do NOT',
                      payload['suggested_envelope_for_approve']['prompt'])

    def test_silence_without_remediation_uses_generic_fix(self) -> None:
        """Coupling holds even if the operator supplied no remediation text —
        a generic 'patch it at the source' instruction fills the decision."""
        res = medic_actions.silence_false_positive(self.SILENCEABLE_FP)
        self.assertTrue(res['ok'])
        decisions = self._decisions()
        self.assertEqual(len(decisions), 1)
        self.assertIn('patch it at the source', decisions[0]['payload']['prompt'])

    def test_idempotent_silence_does_not_re_post_decision(self) -> None:
        """One-shot: the second (idempotent) silence returns early and must
        NOT post a second decision, so a re-confirmed false positive can't
        re-junk the Approvals tab."""
        medic_actions.silence_false_positive(self.SILENCEABLE_FP, reason='a')
        res = medic_actions.silence_false_positive(self.SILENCEABLE_FP, reason='b')
        self.assertEqual(res['reason'], 'already-silenced')
        self.assertEqual(len(self._decisions()), 1)

    def test_refused_silence_posts_no_decision(self) -> None:
        """A refused silence (not in allowlist) performs nothing, so it must
        not post a decision either."""
        medic_actions.silence_false_positive(
            'watchdog:disk-full-critical', reason='nope')
        self.assertEqual(self._decisions(), [])


class RestartSuccessTest(_IsolatedActions):
    def test_restart_daemon_success_records_acted(self) -> None:
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p as active_m:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP,
                                               attempt=1)
        self.assertTrue(res['ok'])
        self.assertEqual(res['outcome'], 'acted')
        self.assertEqual(res['reason'], 'acted')
        restart_m.assert_called_once_with(ALLOWED_DAEMON)
        active_m.assert_called_once_with(ALLOWED_DAEMON)
        recs = self._ledger_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['outcome'], 'acted')
        self.assertEqual(recs[0]['classification'], 'reversible')
        # The acted record's fingerprint matches the alert fingerprint so the
        # recurrence gate + dispatcher dedup line up.
        self.assertEqual(recs[0]['fingerprint'], ALERT_FP)
        self.assertTrue(medic_ledger.has_acted(ALERT_FP))
        # A VERIFIED success IS counted as handled (contrast with the
        # 'acted-failed' path, which is not) -- the dispatcher advances past it.
        self.assertIn(ALERT_FP, medic_ledger.acted_fingerprints())

    def test_retrigger_inbox_success(self) -> None:
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p:
            res = medic_actions.retrigger_inbox(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        self.assertEqual(res['action'], 'retrigger-inbox')
        restart_m.assert_called_once_with(ALLOWED_DAEMON)


class VerifyFailureTest(_IsolatedActions):
    def test_restart_zero_but_not_active_is_failure(self) -> None:
        restart_p, active_p = self._patch_subprocess(rc=0, active='failed')
        with restart_p, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'verify-failed')
        # An action WAS attempted on the system but did NOT verify -> recorded
        # 'acted-failed' (audit M2): it arms the recurrence gate (no retry loop)
        # but is NOT counted as a verified success, so the dispatcher still
        # escalates this still-down daemon rather than treating it as handled.
        self.assertEqual(res['outcome'], 'acted-failed')
        self.assertIn('failed', res['detail'])
        recs = self._ledger_records()
        self.assertEqual(recs[-1]['outcome'], 'acted-failed')
        # Recurrence gate armed...
        self.assertTrue(medic_ledger.has_acted(ALERT_FP))
        # ...but NOT counted as a handled/verified success.
        self.assertNotIn(ALERT_FP, medic_ledger.acted_fingerprints())

    def test_restart_nonzero_rc_is_failure(self) -> None:
        restart_p, active_p = self._patch_subprocess(rc=1, active='active')
        with restart_p, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'restart-error')
        # A non-zero restart rc is likewise 'acted-failed': armed, not handled.
        self.assertEqual(res['outcome'], 'acted-failed')
        self.assertTrue(medic_ledger.has_acted(ALERT_FP))
        self.assertNotIn(ALERT_FP, medic_ledger.acted_fingerprints())


class WatchdogCoordinationTest(_IsolatedActions):
    """Stage-2 restart guard: before actuating, _act_restart must refuse +
    escalate diagnose-only when a peer (watchdog flap / a recent
    watchdog-or-systemd restart) is already managing the unit, so Medic does
    not reset systemd's StartLimit backoff on a flapping unit. Fail-safe: an
    unreadable/corrupt marker must NOT block a legitimate restart."""

    def _state_dir(self) -> Path:
        d = self.agents_root / 'state'
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_flap(self, unit: str, streak: int = 2, age_sec: float = 0.0) -> Path:
        flap_dir = self._state_dir() / 'auto-restart-flap'
        flap_dir.mkdir(parents=True, exist_ok=True)
        name = unit[:-len('.service')] if unit.endswith('.service') else unit
        path = flap_dir / name
        path.write_text(str(streak))
        if age_sec:
            old = time.time() - age_sec
            os.utime(path, (old, old))
        return path

    def _short(self, unit: str) -> str:
        name = unit[:-len('.service')] if unit.endswith('.service') else unit
        return name[len('ourliberty-'):] if name.startswith('ourliberty-') else name

    def _write_mem_cooldown(self, unit: str, age_sec: float = 0.0) -> Path:
        path = self._state_dir() / f'{self._short(unit)}-mem-restart-cooldown'
        path.touch()
        if age_sec:
            old = time.time() - age_sec
            os.utime(path, (old, old))
        return path

    def _write_reconcile_cooldown(self, unit: str, count: int = 1,
                                  window_age_sec: float = 0.0,
                                  body=None) -> Path:
        path = self._state_dir() / f'{self._short(unit)}-reconcile-cooldown'
        if body is None:
            body = json.dumps({
                'window_start': time.time() - window_age_sec,
                'count': count,
                'paged': False,
            })
        path.write_text(body)
        return path

    # ---- refuse paths ----

    def test_fresh_flap_streak_refuses_restart(self) -> None:
        self._write_flap(ALLOWED_DAEMON, streak=2)
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p as active_m:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['outcome'], 'skipped')
        self.assertEqual(res['reason'], 'flapping-defer-to-systemd')
        # No actuation: backoff is not reset.
        restart_m.assert_not_called()
        active_m.assert_not_called()
        # Refusal is 'skipped' -> recurrence gate NOT armed; Medic may retry
        # once the flap settles.
        self.assertFalse(medic_ledger.has_acted(ALERT_FP))

    def test_fresh_flap_streak_refuses_outbox_too(self) -> None:
        self._write_flap(ALLOWED_OUTBOX, streak=3)
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(
                ALLOWED_OUTBOX, 'watchdog:' + ALLOWED_OUTBOX)
        self.assertEqual(res['reason'], 'flapping-defer-to-systemd')
        restart_m.assert_not_called()

    def test_fresh_mem_restart_cooldown_refuses(self) -> None:
        self._write_mem_cooldown(ALLOWED_DAEMON)  # touched now
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'recently-restarted')
        restart_m.assert_not_called()

    def test_fresh_reconcile_cooldown_refuses(self) -> None:
        self._write_reconcile_cooldown(ALLOWED_DAEMON, count=1)
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'recently-restarted')
        restart_m.assert_not_called()

    def test_coordination_runs_after_recurrence_gate(self) -> None:
        # A flap marker is present AND the fingerprint was already acted: the
        # earlier recurrence gate wins (already-acted), confirming ordering.
        src, subj = medic_actions._fp_parts(ALERT_FP)
        medic_ledger.append_record(
            source=src, subject=subj, classification='reversible',
            outcome='acted', attempt=1, notes='prior act')
        self._write_flap(ALLOWED_DAEMON)
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertEqual(res['reason'], 'already-acted')
        restart_m.assert_not_called()

    # ---- proceed paths ----

    def test_no_markers_proceeds(self) -> None:
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        restart_m.assert_called_once_with(ALLOWED_DAEMON)

    def test_stale_flap_streak_proceeds(self) -> None:
        # Marker older than the freshness window => watchdog stopped updating
        # it; treat as stale and proceed.
        self._write_flap(ALLOWED_DAEMON, streak=2,
                         age_sec=medic_actions._FLAP_FRESH_WINDOW_SEC + 60)
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        restart_m.assert_called_once_with(ALLOWED_DAEMON)

    def test_stale_mem_cooldown_proceeds(self) -> None:
        self._write_mem_cooldown(
            ALLOWED_DAEMON,
            age_sec=medic_actions._MEM_RESTART_COOLDOWN_SEC + 60)
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        restart_m.assert_called_once_with(ALLOWED_DAEMON)

    def test_expired_reconcile_window_proceeds(self) -> None:
        self._write_reconcile_cooldown(
            ALLOWED_DAEMON, count=3,
            window_age_sec=medic_actions._RECONCILE_WINDOW_SEC + 120)
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        restart_m.assert_called_once_with(ALLOWED_DAEMON)

    def test_reconcile_marker_count_zero_proceeds(self) -> None:
        # A fresh window with no recorded restart yet must not block.
        self._write_reconcile_cooldown(ALLOWED_DAEMON, count=0)
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        restart_m.assert_called_once_with(ALLOWED_DAEMON)

    # ---- fail-safe (read error -> proceed) ----

    def test_corrupt_reconcile_marker_proceeds(self) -> None:
        self._write_reconcile_cooldown(ALLOWED_DAEMON, body='{not json')
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        restart_m.assert_called_once_with(ALLOWED_DAEMON)

    def test_marker_read_error_proceeds(self) -> None:
        # An unreadable marker must NOT block: stat() on the flap marker raises
        # OSError -> fail-safe treats it as absent and proceeds with the
        # existing gates. Scope the fault to the flap-marker path only so the
        # config/ledger IO under the same call is unaffected.
        self._write_flap(ALLOWED_DAEMON)
        real_stat = Path.stat

        def flaky_stat(self, *a, **k):
            if 'auto-restart-flap' in str(self):
                raise OSError('simulated unreadable marker')
            return real_stat(self, *a, **k)

        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p as restart_m, active_p, \
                mock.patch('pathlib.Path.stat', new=flaky_stat):
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        restart_m.assert_called_once_with(ALLOWED_DAEMON)


class UnsupportedActionTest(_IsolatedActions):
    def test_privileged_action_type_never_reaches_handler(self) -> None:
        with mock.patch.object(medic_actions, '_run_restart') as restart_m, \
                mock.patch.object(medic_actions, '_is_active') as active_m, \
                mock.patch('sys.stdout'):
            rc = medic_actions._main(
                ['rotate-credential', '--target', ALLOWED_DAEMON,
                 '--fingerprint', ALERT_FP])
        self.assertEqual(rc, 1)
        restart_m.assert_not_called()
        active_m.assert_not_called()

    def test_judgment_action_type_never_reaches_handler(self) -> None:
        with mock.patch.object(medic_actions, '_run_restart') as restart_m, \
                mock.patch.object(medic_actions, '_is_active'), \
                mock.patch('sys.stdout'):
            rc = medic_actions._main(
                ['diagnose-only', '--target', ALLOWED_DAEMON])
        self.assertEqual(rc, 1)
        restart_m.assert_not_called()

    def test_cli_restart_daemon_success_exits_zero(self) -> None:
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p, active_p, mock.patch('sys.stdout'):
            rc = medic_actions._main(
                ['restart-daemon', '--unit', ALLOWED_DAEMON,
                 '--fingerprint', ALERT_FP, '--attempt', '1'])
        self.assertEqual(rc, 0)


class ConfigFailSafeTest(_IsolatedActions):
    def test_missing_config_means_empty_allowlist(self) -> None:
        (self.repo_dir / 'config' / 'medic-reversible-targets.json').unlink()
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        # Empty allowlist -> even a normally-allowed unit is refused.
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'not-permitted')
        restart_m.assert_not_called()

    def test_malformed_config_means_empty_allowlist(self) -> None:
        (self.repo_dir / 'config' / 'medic-reversible-targets.json').write_text(
            '{not valid json')
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            # Must not raise.
            res = medic_actions.retrigger_inbox(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'not-permitted')
        restart_m.assert_not_called()

    def test_config_root_not_object_is_empty_allowlist(self) -> None:
        (self.repo_dir / 'config' / 'medic-reversible-targets.json').write_text(
            json.dumps([1, 2, 3]))
        loaded = medic_actions._load_reversible_targets()
        self.assertEqual(loaded['restart_daemon_units'], [])
        self.assertEqual(loaded['retrigger_inbox_targets'], [])


class Stage1SilenceOnlyPostureTest(SilenceFalsePositiveTest):
    """Pin the SHIPPED Stage-1 production posture (config/medic-reversible-
    targets.json with the act-branch live): the enable flag is ON, the restart
    allowlists are EMPTY, and only the two proven-benign silence classes are
    enabled. With this exact config:

      - restart-daemon and retrigger-inbox are REFUSED not-permitted (the empty
        allowlist makes every restart classification fall through to diagnose-
        only escalation, exactly as in the prior escalate-only era), and NO
        subprocess fires; and
      - silence-false-positive still SUCCEEDS for either shipped benign class
        and is REFUSED for any other fingerprint; and
      - with the enable flag OFF, every action (restart and silence) refuses.

    This locks the activation contract: turning the act-branch on must not let a
    restart fire while the Stage-2 watchdog-coordination guard is unbuilt.
    """

    # The two benign classes actually shipped in silenceable_subjects.
    SILENCEABLE_FORGE_NO_PR = ('heal-pipeline-stall:pipeline-stall:forge-no-pr:'
                               'forge-queue-api-preflight-20260603T231401Z-clarify1')
    SILENCEABLE_PR_INFERRED = ('heal-pipeline-stall:pipeline-stall:'
                               'pr-create-inferred-failure:some-task-20260605T010101Z')

    def setUp(self) -> None:
        super().setUp()
        # Overwrite with the EXACT shipped Stage-1 config: empty restart arrays,
        # both real benign silence classes.
        (self.repo_dir / 'config' / 'medic-reversible-targets.json').write_text(
            json.dumps({
                '$schema_version': '1',
                'restart_daemon_units': [],
                'retrigger_inbox_targets': [],
                'silenceable_subjects': [
                    'pipeline-stall:forge-no-pr:',
                    'pipeline-stall:pr-create-inferred-failure:',
                ],
            }))

    def test_restart_daemon_refused_when_allowlist_empty(self) -> None:
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p as active_m:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'not-permitted')
        self.assertEqual(res['outcome'], 'skipped')
        restart_m.assert_not_called()
        active_m.assert_not_called()

    def test_retrigger_inbox_refused_when_allowlist_empty(self) -> None:
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            res = medic_actions.retrigger_inbox(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'not-permitted')
        restart_m.assert_not_called()

    def test_silence_forge_no_pr_class_succeeds(self) -> None:
        res = medic_actions.silence_false_positive(
            self.SILENCEABLE_FORGE_NO_PR, reason='build shipped as PR #294')
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['reason'], 'silenced')
        self.assertTrue(
            medic_actions.larry_alerts.is_silenced(self.SILENCEABLE_FORGE_NO_PR))

    def test_silence_pr_create_inferred_failure_class_succeeds(self) -> None:
        res = medic_actions.silence_false_positive(
            self.SILENCEABLE_PR_INFERRED, reason='inferred failure was benign')
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['reason'], 'silenced')
        self.assertTrue(
            medic_actions.larry_alerts.is_silenced(self.SILENCEABLE_PR_INFERRED))

    def test_silence_unallowlisted_fingerprint_refused(self) -> None:
        res = medic_actions.silence_false_positive(
            'watchdog:disk-full-critical', reason='nope')
        self.assertFalse(res['ok'])
        self.assertEqual(res['reason'], 'not-permitted')

    def test_enable_flag_off_refuses_all_actions(self) -> None:
        os.environ['OURLIBERTY_MEDIC_ENABLED'] = '0'
        restart_p, active_p = self._patch_subprocess()
        with restart_p as restart_m, active_p:
            r_restart = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        r_silence = medic_actions.silence_false_positive(
            self.SILENCEABLE_FORGE_NO_PR, reason='gate off')
        self.assertEqual(r_restart['reason'], 'gate-enable-flag-off')
        self.assertEqual(r_silence['reason'], 'gate-enable-flag-off')
        restart_m.assert_not_called()
        self.assertFalse(
            medic_actions.larry_alerts.is_silenced(self.SILENCEABLE_FORGE_NO_PR))


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


class GraduationExecutionRecordTest(_IsolatedActions):
    """Stage 1 of the Check-V graduation unify: a verified Medic action whose
    name matches a registry template records a clean/failed execution into the
    executions store Check V reads — the streak input the loop was missing.
    Non-template actions and refusals record nothing."""

    def setUp(self) -> None:
        super().setUp()
        # Own our registry fixture rather than reading the real worktree
        # config/auto-fix-patterns.json (whose path is Path(__file__)-anchored,
        # not env-scoped): 'restart-daemon' present + probation, 'retrigger-
        # inbox' absent. Repointing BOTH module constants also guarantees the
        # failure-path demotion hook can never touch the real registry.
        import alert_triage_state
        import pulse_check_v
        reg = self.repo_dir / 'config' / 'auto-fix-patterns.json'
        reg.write_text(json.dumps({'patterns': [{
            'template': 'restart-daemon', 'state': 'probation',
            'reversible': True, 'permanent_guard': False,
        }]}))
        for mod, attr in ((alert_triage_state, 'AUTO_FIX_PATTERNS_FILE'),
                          (pulse_check_v, 'REGISTRY_FILE')):
            p = mock.patch.object(mod, attr, reg)
            p.start()
            self.addCleanup(p.stop)

    def _executions(self) -> dict:
        import alert_triage_state
        return alert_triage_state.load_executions()

    def test_verified_restart_records_success_execution(self) -> None:
        # restart-daemon IS a registry template, so a verified restart accrues
        # a clean execution toward its graduation streak.
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertTrue(res['ok'])
        execs = self._executions().get('restart-daemon', [])
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]['outcome'], 'success')
        self.assertFalse(execs[0]['larry_correction_signal'])

    def test_unverified_restart_records_failure_execution(self) -> None:
        # Ran but did not verify -> 'acted-failed' -> a FAILURE execution
        # (breaks the streak; the recorder's demotion hook no-ops on a
        # probation template).
        restart_p, active_p = self._patch_subprocess(rc=0, active='activating')
        with restart_p, active_p:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP)
        self.assertFalse(res['ok'])
        execs = self._executions().get('restart-daemon', [])
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]['outcome'], 'failure')

    def test_two_verified_restarts_accrue_two_executions(self) -> None:
        # The point of the feature: successive clean restarts build a streak.
        # Distinct fingerprints so the one-action-per-fingerprint recurrence
        # gate does not block the second act.
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p, active_p:
            r1 = medic_actions.restart_daemon(ALLOWED_DAEMON, 'watchdog:fp-1')
            r2 = medic_actions.restart_daemon(ALLOWED_DAEMON, 'watchdog:fp-2')
        self.assertTrue(r1['ok'] and r2['ok'])
        execs = self._executions().get('restart-daemon', [])
        self.assertEqual(len(execs), 2)
        self.assertTrue(all(e['outcome'] == 'success' for e in execs))

    def test_non_template_action_records_no_execution(self) -> None:
        # retrigger-inbox is a real Medic action but NOT a registry template,
        # so it must not create a track record (registry gate).
        restart_p, active_p = self._patch_subprocess(rc=0, active='active')
        with restart_p, active_p:
            res = medic_actions.retrigger_inbox(
                ALLOWED_DAEMON, 'watchdog:retrigger-probe')
        self.assertTrue(res['ok'])
        self.assertNotIn('retrigger-inbox', self._executions())

    def test_refused_restart_records_no_execution(self) -> None:
        # An allowlist refusal never ran the action, so nothing is recorded.
        restart_p, active_p = self._patch_subprocess()
        with restart_p, active_p:
            res = medic_actions.restart_daemon(
                'ourliberty-not-a-real-unit.service', ALERT_FP)
        self.assertFalse(res['ok'])
        self.assertEqual(self._executions(), {})


if __name__ == '__main__':
    unittest.main()
