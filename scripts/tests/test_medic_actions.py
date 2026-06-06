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


if __name__ == '__main__':
    unittest.main()
