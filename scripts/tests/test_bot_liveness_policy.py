#!/usr/bin/env python3
"""Tests for scripts/bot_liveness_policy.py.

Covers the spec-required cases:
  (1) Malformed policy → BotPolicyError (schema gate).
  (2) systemd-mode liveness reads `systemctl is-active`.
  (3) tmux-or-systemd mode considers BOTH paths.
  (4) recovery_command shape per mode.
  (5) Bot missing from policy defaults to systemd mode.
  (6) desired_state: valid values gate, default 'up', and the live
      policy file's `systemd_unit` for every bot resolves to a real
      unit file on disk (desired-state reconciler enforcement, per
      desired-state-reconciler-brief.md §6).

Run: python3 -m unittest scripts.tests.test_bot_liveness_policy
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


class DesiredStateSchemaTest(unittest.TestCase):
    """Case (6) part 1: desired_state validation + default.

    The reconciler reads `desired_state` to decide whether to restart a
    down bot. A malformed value would silently mis-reconcile (e.g. typo
    'Down' would default-fall-through to 'up' and the bot would be
    fought back online). The schema gate catches it at load.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_desired_state_up_loads(self):
        policy = dict(VALID_POLICY)
        policy['forge'] = {'mode': 'systemd', 'desired_state': 'up',
                           'systemd_unit': 'ourliberty-forge-bot.service'}
        p = _write_policy(self._tmp_path, policy)
        loaded = bot_liveness_policy.load_policy(p)
        self.assertEqual(bot_liveness_policy.desired_state('forge', policy=loaded), 'up')

    def test_valid_desired_state_down_loads(self):
        policy = dict(VALID_POLICY)
        policy['forge'] = {'mode': 'systemd', 'desired_state': 'down',
                           'systemd_unit': 'ourliberty-forge-bot.service'}
        p = _write_policy(self._tmp_path, policy)
        loaded = bot_liveness_policy.load_policy(p)
        self.assertEqual(bot_liveness_policy.desired_state('forge', policy=loaded), 'down')

    def test_bad_desired_state_raises(self):
        bad = dict(VALID_POLICY)
        bad['forge'] = {'mode': 'systemd', 'desired_state': 'Down',
                        'systemd_unit': 'ourliberty-forge-bot.service'}
        p = _write_policy(self._tmp_path, bad)
        with self.assertRaises(bot_liveness_policy.BotPolicyError) as ctx:
            bot_liveness_policy.load_policy(p)
        self.assertIn('desired_state', str(ctx.exception))

    def test_absent_desired_state_defaults_to_up(self):
        # VALID_POLICY entries above don't include desired_state; default is 'up'.
        self.assertEqual(
            bot_liveness_policy.desired_state('forge', policy=VALID_POLICY),
            bot_liveness_policy.DESIRED_UP,
        )

    def test_missing_bot_defaults_to_up(self):
        # Bot not in policy at all → default entry → desired_state='up'.
        self.assertEqual(
            bot_liveness_policy.desired_state('newbot', policy=VALID_POLICY),
            'up',
        )


class LivePolicySystemdUnitsTest(unittest.TestCase):
    """Case (6) part 2: every bot in the shipped policy file resolves a
    `systemd_unit` whose unit file exists on the droplet.

    The reconciler's actuation path is `sudo systemctl restart <unit>`.
    A typoed unit name would let the policy validate, the watchdog
    reconcile happily, and `systemctl restart` silently no-op — exactly
    the kind of green-without-teeth gate the brief warns against.

    Skipped where /etc/systemd cannot be inspected (e.g. CI runners
    without our units installed); the assertion has teeth on the droplet
    where it matters.
    """

    SYSTEMD_UNIT_DIRS = (
        Path('/etc/systemd/system'),
        Path('/usr/lib/systemd/system'),
        Path('/lib/systemd/system'),
    )

    def test_every_policy_bot_has_unit_file(self):
        policy = bot_liveness_policy.load_policy()
        agents = bot_liveness_policy.policy_agents(policy)
        self.assertGreater(len(agents), 0, 'policy declares no bots — gate is empty')

        any_dir_present = any(d.exists() for d in self.SYSTEMD_UNIT_DIRS)
        if not any_dir_present:
            self.skipTest('no systemd unit directory present (non-droplet env)')

        missing: list[str] = []
        for agent in agents:
            unit = bot_liveness_policy.systemd_unit(agent, policy=policy)
            found = any((d / unit).exists() for d in self.SYSTEMD_UNIT_DIRS if d.exists())
            if not found:
                missing.append(f'{agent} -> {unit}')
        self.assertEqual(
            missing, [],
            f'policy bots without an installed unit file: {missing}',
        )


if __name__ == '__main__':
    unittest.main()
