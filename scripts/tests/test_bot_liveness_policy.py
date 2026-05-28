#!/usr/bin/env python3
"""Tests for scripts/bot_liveness_policy.py.

Covers the five spec-required cases:
  (1) Malformed policy → BotPolicyError (schema gate).
  (2) systemd-mode liveness reads `systemctl is-active`.
  (3) tmux-or-systemd mode considers BOTH paths.
  (4) recovery_command shape per mode.
  (5) Bot missing from policy defaults to systemd mode.

Run: python3 -m unittest scripts.tests.test_bot_liveness_policy
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import bot_liveness_policy  # noqa: E402


VALID_POLICY = {
    '_schema': {'version': 1, 'purpose': 'test'},
    'beacon': {'mode': 'systemd', 'systemd_unit': 'ourliberty-beacon-bot.service'},
    'forge': {'mode': 'systemd', 'systemd_unit': 'ourliberty-forge-bot.service'},
    'mirror': {'mode': 'systemd', 'systemd_unit': 'ourliberty-mirror-bot.service'},
    'pulse': {
        'mode': 'tmux-or-systemd',
        'tmux_session': 'pulse-bot',
        'systemd_unit': 'ourliberty-pulse-bot.service',
        'launcher': 'scripts/pulse_telegram_bot.sh',
    },
}


def _write_policy(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / 'bot-liveness-policy.json'
    p.write_text(json.dumps(data))
    return p


class LoadPolicyTest(unittest.TestCase):
    """Case (1): malformed policy raises BotPolicyError."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_policy_loads(self):
        p = _write_policy(self._tmp_path, VALID_POLICY)
        loaded = bot_liveness_policy.load_policy(p)
        self.assertEqual(set(bot_liveness_policy.policy_agents(loaded)),
                         {'beacon', 'forge', 'mirror', 'pulse'})

    def test_bad_mode_raises(self):
        bad = dict(VALID_POLICY)
        bad['forge'] = {'mode': 'docker', 'systemd_unit': 'x'}
        p = _write_policy(self._tmp_path, bad)
        with self.assertRaises(bot_liveness_policy.BotPolicyError) as ctx:
            bot_liveness_policy.load_policy(p)
        self.assertIn('mode', str(ctx.exception))

    def test_systemd_missing_unit_raises(self):
        bad = dict(VALID_POLICY)
        bad['forge'] = {'mode': 'systemd'}
        p = _write_policy(self._tmp_path, bad)
        with self.assertRaises(bot_liveness_policy.BotPolicyError):
            bot_liveness_policy.load_policy(p)

    def test_tmux_or_systemd_missing_required_raises(self):
        bad = dict(VALID_POLICY)
        bad['pulse'] = {
            'mode': 'tmux-or-systemd',
            'tmux_session': 'pulse-bot',
            # missing systemd_unit + launcher
        }
        p = _write_policy(self._tmp_path, bad)
        with self.assertRaises(bot_liveness_policy.BotPolicyError) as ctx:
            bot_liveness_policy.load_policy(p)
        msg = str(ctx.exception)
        self.assertIn('systemd_unit', msg)
        self.assertIn('launcher', msg)

    def test_missing_file_raises(self):
        with self.assertRaises(bot_liveness_policy.BotPolicyError):
            bot_liveness_policy.load_policy(self._tmp_path / 'does-not-exist.json')

    def test_env_override_path(self):
        p = _write_policy(self._tmp_path, VALID_POLICY)
        with mock.patch.dict(os.environ, {bot_liveness_policy.POLICY_PATH_ENV: str(p)}):
            loaded = bot_liveness_policy.load_policy()
        self.assertIn('pulse', loaded)


class SystemdLivenessTest(unittest.TestCase):
    """Case (2): systemd-mode bot returns alive only when is-active succeeds."""

    def setUp(self):
        self.policy = VALID_POLICY

    def test_alive_when_is_active_succeeds(self):
        def fake_run(cmd, *_a, **_kw):
            self.assertEqual(cmd[:2], ['systemctl', 'is-active'])
            return mock.MagicMock(returncode=0, stdout='active\n')
        with mock.patch.object(bot_liveness_policy.subprocess, 'run', side_effect=fake_run):
            alive, mode = bot_liveness_policy.check_liveness('forge', policy=self.policy)
        self.assertTrue(alive)
        self.assertEqual(mode, 'systemd')

    def test_down_when_is_active_fails(self):
        with mock.patch.object(bot_liveness_policy.subprocess, 'run',
                               return_value=mock.MagicMock(returncode=3, stdout='inactive\n')):
            alive, mode = bot_liveness_policy.check_liveness('forge', policy=self.policy)
        self.assertFalse(alive)
        self.assertEqual(mode, 'down')

    def test_subprocess_exception_treated_as_down(self):
        with mock.patch.object(bot_liveness_policy.subprocess, 'run',
                               side_effect=Exception('boom')):
            alive, mode = bot_liveness_policy.check_liveness('forge', policy=self.policy)
        self.assertFalse(alive)
        self.assertEqual(mode, 'down')


class TmuxOrSystemdLivenessTest(unittest.TestCase):
    """Case (3): tmux-or-systemd bot is alive when EITHER path succeeds."""

    def setUp(self):
        self.policy = VALID_POLICY

    def _runner(self, *, tmux_rc: int, systemd_rc: int):
        def fake_run(cmd, *_a, **_kw):
            if cmd[0] == 'tmux':
                return mock.MagicMock(returncode=tmux_rc, stdout='')
            if cmd[0] == 'systemctl':
                stdout = 'active\n' if systemd_rc == 0 else 'inactive\n'
                return mock.MagicMock(returncode=systemd_rc, stdout=stdout)
            raise AssertionError(f'unexpected cmd: {cmd}')
        return fake_run

    def test_alive_via_tmux_systemd_down(self):
        with mock.patch.object(bot_liveness_policy.subprocess, 'run',
                               side_effect=self._runner(tmux_rc=0, systemd_rc=3)):
            alive, mode = bot_liveness_policy.check_liveness('pulse', policy=self.policy)
        self.assertTrue(alive)
        self.assertEqual(mode, 'tmux')

    def test_alive_via_systemd_tmux_down(self):
        with mock.patch.object(bot_liveness_policy.subprocess, 'run',
                               side_effect=self._runner(tmux_rc=1, systemd_rc=0)):
            alive, mode = bot_liveness_policy.check_liveness('pulse', policy=self.policy)
        self.assertTrue(alive)
        self.assertEqual(mode, 'systemd')

    def test_down_when_both_paths_fail(self):
        with mock.patch.object(bot_liveness_policy.subprocess, 'run',
                               side_effect=self._runner(tmux_rc=1, systemd_rc=3)):
            alive, mode = bot_liveness_policy.check_liveness('pulse', policy=self.policy)
        self.assertFalse(alive)
        self.assertEqual(mode, 'down')

    def test_tmux_preferred_when_both_alive(self):
        # When both paths report alive, tmux wins (intended deployment).
        with mock.patch.object(bot_liveness_policy.subprocess, 'run',
                               side_effect=self._runner(tmux_rc=0, systemd_rc=0)):
            alive, mode = bot_liveness_policy.check_liveness('pulse', policy=self.policy)
        self.assertTrue(alive)
        self.assertEqual(mode, 'tmux')


class RecoveryCommandTest(unittest.TestCase):
    """Case (4): recovery_command returns the right shape per mode."""

    def test_systemd_mode_returns_systemctl_restart(self):
        cmd = bot_liveness_policy.recovery_command('forge', policy=VALID_POLICY)
        self.assertEqual(cmd, 'sudo systemctl restart ourliberty-forge-bot.service')

    def test_tmux_mode_returns_bash_launcher(self):
        cmd = bot_liveness_policy.recovery_command('pulse', policy=VALID_POLICY)
        self.assertEqual(cmd, 'bash scripts/pulse_telegram_bot.sh')

    def test_tmux_mode_does_not_return_systemctl(self):
        # The destructive shape this policy exists to prevent.
        cmd = bot_liveness_policy.recovery_command('pulse', policy=VALID_POLICY)
        self.assertNotIn('systemctl restart', cmd)


class DefaultModeTest(unittest.TestCase):
    """Case (5): bot missing from policy defaults to systemd mode."""

    def test_missing_bot_defaults_to_systemd_unit(self):
        # 'newbot' is not in VALID_POLICY.
        with mock.patch.object(bot_liveness_policy.subprocess, 'run',
                               return_value=mock.MagicMock(returncode=0, stdout='active\n')) as run:
            alive, mode = bot_liveness_policy.check_liveness('newbot', policy=VALID_POLICY)
        self.assertTrue(alive)
        self.assertEqual(mode, 'systemd')
        # And it called systemctl is-active on the conventional unit name.
        called_cmd = run.call_args.args[0]
        self.assertEqual(called_cmd, ['systemctl', 'is-active', 'ourliberty-newbot-bot.service'])

    def test_missing_bot_recovery_uses_conventional_unit(self):
        cmd = bot_liveness_policy.recovery_command('newbot', policy=VALID_POLICY)
        self.assertEqual(cmd, 'sudo systemctl restart ourliberty-newbot-bot.service')


if __name__ == '__main__':
    unittest.main()
