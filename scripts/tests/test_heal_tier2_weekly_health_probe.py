#!/usr/bin/env python3
"""Tests for setup-token auth in ``heal_tier2_weekly_health_probe.run_probe``.

The Tier 2 weekly/keep-alive probe used to authenticate purely by HOME-swapping
to ``/home/larry/.claude-larry-personal`` and reading that dir's
``.credentials.json``. Once the long-lived ``claude setup-token`` became the
primary Tier 2 dispatch auth (CLAUDE_CODE_OAUTH_TOKEN_TIER2), that creds.json
was intentionally no longer refreshed and lapsed (2026-05-31). The probe, still
on the HOME-swap-only path, then 401'd on every run and emitted a FALSE
"Tier 2 OAuth expired" signal even though real dispatch auth was healthy.

The fix makes ``run_probe`` mirror ``agent_runner._apply_tier_auth``: prefer the
Tier 2 setup-token, falling back to the HOME-swap/creds.json path only when no
setup-token is configured. The probe now exercises the SAME auth source a real
dispatch uses.

This file covers:

- The Tier 2 setup-token lands in the probe subprocess env (HOME-swap intact).
- An unset / empty setup-token → no token injected (legacy HOME-swap-only path).
- A setup-token-present run does not depend on the creds.json under TIER2_HOME.
- The token value never reaches the probe's log file.

Run::

    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_heal_tier2_weekly_health_probe
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

import heal_tier2_weekly_health_probe as hp  # noqa: E402

_TIER2_TOKEN = 'FAKE-oauth-setuptoken-tier2-not-a-real-key-yyyyyyyyyyyyyyyy'
_MODEL_ID = 'claude-haiku-test-not-a-real-model'


def _ok_stdout():
    return json.dumps({'result': 'PROBE_OK', 'session_id': 'sid-x'})


class _ProbeHarness(unittest.TestCase):
    """Per-test isolation: redirect the probe's on-disk paths to a tmpdir and
    own the setup-token env vars so the absent-token path is reachable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Own the auth env vars: clear any inherited value so the no-token
        # path is reachable; individual tests opt in by setting them.
        self._prev_env = {}
        for name in (
            'CLAUDE_CODE_OAUTH_TOKEN_TIER1',
            'CLAUDE_CODE_OAUTH_TOKEN_TIER2',
            'CLAUDE_CODE_OAUTH_TOKEN',
        ):
            self._prev_env[name] = os.environ.pop(name, None)
        # Redirect module-level paths so logging / heartbeat / state / kill
        # switch never touch the live agents root.
        self._prev_paths = {
            'LOG_FILE': hp.LOG_FILE,
            'HEARTBEAT_FILE': hp.HEARTBEAT_FILE,
            'STATE_FILE': hp.STATE_FILE,
            'KILL_SWITCH': hp.KILL_SWITCH,
        }
        hp.LOG_FILE = self.root / 'logs' / 'probe.log'
        hp.HEARTBEAT_FILE = self.root / 'blackboard' / 'probe.heartbeat'
        hp.STATE_FILE = self.root / 'state' / 'probe-state.json'
        hp.KILL_SWITCH = self.root / 'healers.disabled'

    def tearDown(self):
        for name, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        for name, prev in self._prev_paths.items():
            setattr(hp, name, prev)
        self.tmp.cleanup()

    def _run_probe(self, returncode=0, stdout=None, stderr=''):
        """Drive run_probe with a fake subprocess; return (result, env) where
        env is the dict passed to subprocess.run."""
        captured = {}
        if stdout is None:
            stdout = _ok_stdout()

        def fake_run(cmd, **kwargs):
            captured.update(kwargs.get('env', {}))
            return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

        with mock.patch.object(hp.subprocess, 'run', side_effect=fake_run):
            result = hp.run_probe(_MODEL_ID)
        return result, captured


class SetupTokenAppliedTest(_ProbeHarness):
    """When the Tier 2 setup-token is configured, the probe authenticates via
    it (mirroring the dispatch path) while keeping the HOME-swap intact."""

    def test_setup_token_lands_in_subprocess_env(self):
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        (ok, _stdout, _stderr, _code), env = self._run_probe()
        self.assertTrue(ok)
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER2_TOKEN)
        # HOME-swap stays — session/project dirs live under HOME/.claude.
        self.assertEqual(env['HOME'], hp.TIER2_HOME)

    def test_no_setup_token_falls_back_to_home_swap_only(self):
        # No setup-token configured → the probe must NOT inject a token; it
        # relies on the legacy HOME-swap->credentials.json path.
        (ok, _stdout, _stderr, _code), env = self._run_probe()
        self.assertTrue(ok)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertEqual(env['HOME'], hp.TIER2_HOME)

    def test_empty_setup_token_treated_as_unset(self):
        # An empty env var is misconfiguration, not an intentional override;
        # the probe must fall back to the HOME-swap-only path.
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = ''
        (ok, _stdout, _stderr, _code), env = self._run_probe()
        self.assertTrue(ok)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)


class CredentialsFreshnessIndependenceTest(_ProbeHarness):
    """With the setup-token present, a probe run must succeed independent of
    the .credentials.json under TIER2_HOME — that file is intentionally
    unrefreshed and lapsed, and the probe must not read or gate on it."""

    def test_setup_token_run_ignores_absent_credentials_file(self):
        # Point HOME at a tmp dir that has NO .claude/.credentials.json. If a
        # future change reintroduced a creds.json freshness gate in run_probe,
        # this would fail — the regression guard the spec asks for.
        fake_home = self.root / 'tier2-home'
        fake_home.mkdir(parents=True, exist_ok=True)
        self.assertFalse((fake_home / '.claude' / '.credentials.json').exists())
        with mock.patch.object(hp, 'TIER2_HOME', str(fake_home)):
            os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
            (ok, _stdout, _stderr, _code), env = self._run_probe()
        self.assertTrue(ok)
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER2_TOKEN)
        self.assertEqual(env['HOME'], str(fake_home))


class TokenNeverLoggedTest(_ProbeHarness):
    """The token value must never reach the probe's log file — on either the
    success or failure path."""

    def _drive_run(self, probe_ok):
        rc = 0 if probe_ok else 1
        stdout = _ok_stdout() if probe_ok else 'Invalid authentication credentials'

        def fake_run(cmd, **kwargs):
            return mock.Mock(returncode=rc, stdout=stdout, stderr='')

        with mock.patch.object(hp.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(hp, 'load_haiku_model_id', return_value=_MODEL_ID), \
                mock.patch.object(hp.larry_alerts, 'append_alert', return_value=True):
            return hp.run()

    def test_token_absent_from_log_on_success_and_failure(self):
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        self._drive_run(probe_ok=True)
        self._drive_run(probe_ok=False)
        self.assertTrue(hp.LOG_FILE.exists())
        text = hp.LOG_FILE.read_text()
        self.assertNotIn(_TIER2_TOKEN, text)


class ClassifyFailureTest(unittest.TestCase):
    """classify_failure must separate auth failures (re-auth) from
    probe-environment failures (token likely fine; do NOT re-auth). The
    2026-06-03 incident was 30h of TimeoutExpired misread as 'Tier 2 down'."""

    def test_timeout_is_env_not_auth(self):
        reason, _detail, hint = hp.classify_failure(
            '', "subprocess failed: TimeoutExpired: ... timed out after 60 seconds",
            -1)
        self.assertEqual(reason, 'timeout')
        self.assertIs(hint, hp._ENV_HINT)

    def test_oom_kill_is_env(self):
        for code in (-9, 137):
            reason, _d, hint = hp.classify_failure('', '', code)
            self.assertEqual(reason, 'killed')
            self.assertIs(hint, hp._ENV_HINT)
        reason, _d, hint = hp.classify_failure('', 'MemoryError', 1)
        self.assertEqual(reason, 'killed')

    def test_binary_missing_is_env(self):
        reason, _d, hint = hp.classify_failure(
            '', 'subprocess failed: FileNotFoundError: ...', -1)
        self.assertEqual(reason, 'binary_missing')
        self.assertIs(hint, hp._ENV_HINT)

    def test_401_is_auth(self):
        for blob in ('... "api_error_status":401 ...',
                     'Failed to authenticate. API Error: 401',
                     'Invalid authentication credentials'):
            reason, _d, hint = hp.classify_failure(blob, '', 1)
            self.assertEqual(reason, 'auth_401', blob)
            self.assertIs(hint, hp._AUTH_HINT)

    def test_unknown_is_generic(self):
        reason, _d, hint = hp.classify_failure('weird output', '', 2)
        self.assertEqual(reason, 'other')
        self.assertIs(hint, hp._GENERIC_HINT)


class AlertRoutingTest(_ProbeHarness):
    """run() must route the alert's suggested_action by failure mode: a
    timeout points at the env hint (not the re-auth runbook); a 401 points at
    the re-auth runbooks. Guards against regressing to a single generic hint."""

    def _run_failed(self, stdout, stderr, returncode):
        captured = {}

        def fake_run(cmd, **kwargs):
            return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

        def fake_alert(**kwargs):
            captured.update(kwargs)
            return True

        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        with mock.patch.object(hp.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(hp, 'load_haiku_model_id', return_value=_MODEL_ID), \
                mock.patch.object(hp.larry_alerts, 'append_alert',
                                  side_effect=fake_alert):
            hp.run()
        return captured

    def test_timeout_routes_to_env_hint(self):
        cap = self._run_failed(
            '', 'subprocess failed: TimeoutExpired: ... after 60 seconds', -1)
        self.assertEqual(cap['suggested_action'], hp._ENV_HINT)
        self.assertIn('[timeout]', cap['message'])
        self.assertEqual(cap['subject'], 'tier2_weekly_probe_failed')

    def test_401_routes_to_auth_hint(self):
        cap = self._run_failed(
            json.dumps({'is_error': True, 'api_error_status': 401,
                        'result': 'Invalid authentication credentials'}), '', 1)
        self.assertEqual(cap['suggested_action'], hp._AUTH_HINT)
        self.assertIn('[auth_401]', cap['message'])

    def test_failure_alert_never_leaks_token(self):
        cap = self._run_failed('Invalid authentication credentials', '', 1)
        self.assertNotIn(_TIER2_TOKEN, cap['message'])
        self.assertNotIn(_TIER2_TOKEN, cap['suggested_action'])


if __name__ == '__main__':
    unittest.main()
