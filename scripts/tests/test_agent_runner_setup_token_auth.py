#!/usr/bin/env python3
"""Tests for long-lived setup-token auth wiring in ``agent_runner.run_claude``.

The 2026-05-29 auth_401 storm (~140 events in one day) traced to concurrent
dispatches all racing the single-use OAuth refresh in
``$HOME/.claude/.credentials.json``. The fix replaces credentials.json auth
with a non-refreshing ``claude setup-token`` per tier, read from
``CLAUDE_CODE_OAUTH_TOKEN_TIER{1,2}`` and injected into the subprocess
``CLAUDE_CODE_OAUTH_TOKEN`` env var.

This file covers:

- The active tier's setup-token lands in the subprocess env.
- An unset/empty setup-token → byte-for-byte legacy behavior
  (token-manager default value; HOME-swap intact).
- The failure-fallback subprocess picks the OTHER tier's setup-token (or
  falls back to legacy when the other tier has no setup-token).
- The ``--resume`` no-fallback refusal stays intact under the new path.
- Token values never leak into log files.

Run::

    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_setup_token_auth
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

import agent_runner as ar  # noqa: E402
import larry_alerts  # noqa: E402


_TIER1_TOKEN = 'FAKE-oauth-setuptoken-tier1-not-a-real-key-xxxxxxxxxxxxxxxx'
_TIER2_TOKEN = 'FAKE-oauth-setuptoken-tier2-not-a-real-key-yyyyyyyyyyyyyyyy'
_DEFAULT_TOKEN = 'oauth-default-from-token-manager-zzzzzzzzzzzzzzzzzzzzzzzzzz'


def _ok_response(text='ok', session_id='sid-new'):
    return json.dumps({'result': text, 'session_id': session_id})


class _FakeProc:
    """Minimal stand-in for ``subprocess.Popen``'s returned object — copied
    from test_agent_runner_active_tier so the two test files stay
    structurally aligned without import-time coupling."""

    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.pid = 4242
        self.stdin = mock.MagicMock()
        self.stdout = mock.MagicMock()
        self.stdout.read.return_value = stdout
        self.stderr = mock.MagicMock()
        self.stderr.read.return_value = stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class _NoopGuard:
    def wait_for_slot(self, *_a, **_kw):
        return True

    def release(self, *_a, **_kw):
        pass

    def active_count(self):
        return 0


class _RunClaudeHarness(unittest.TestCase):
    """Common scaffolding: per-test agents-root, tier-state writer, and
    run_claude driver that returns the captured Popen + subprocess.run env
    dicts so individual tests can assert on the auth path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        # Tests own the setup-token env vars: clear any inherited value so
        # the absent-token path is reachable, and individual tests opt-in
        # by setting them via monkeypatching.
        self._prev_t1 = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER1', None)
        self._prev_t2 = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        # Isolate the durable-token file fallback: _setup_token_for_tier now
        # reads ~/credentials/.env.larry when the env var is unset, so point it
        # at a nonexistent path to keep the absent-token path reachable on the
        # droplet (where the real file exists).
        self._prev_env_file = os.environ.get('OURLIBERTY_CREDENTIALS_ENV_FILE')
        os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = str(
            self.root / 'no-such.env')
        # Sandbox ar.log's write-time resolution: most tests here don't mock
        # ar.log, so without this the 'Running'/'Completed' lines (and the
        # #438 TRANSCRIPT_NOT_PERSISTED ERROR) land in the real
        # ~/agents/logs/forge.log. Distinct attr name so it can't collide
        # with TokenValueNeverLoggedTest's own _prev_log_dir handling.
        self._harness_prev_log_dir = os.environ.get('OURLIBERTY_LOG_DIR')
        os.environ['OURLIBERTY_LOG_DIR'] = str(self.root / 'logs')
        (self.root / 'logs').mkdir(parents=True, exist_ok=True)
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        for name, prev in (
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER1', self._prev_t1),
            ('CLAUDE_CODE_OAUTH_TOKEN_TIER2', self._prev_t2),
            ('OURLIBERTY_CREDENTIALS_ENV_FILE', self._prev_env_file),
            ('OURLIBERTY_LOG_DIR', self._harness_prev_log_dir),
        ):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        self.tmp.cleanup()

    def _write_state(self, tier):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': tier}))

    def _drive(self, popen_proc, t2_result=None, session_id=None):
        """Run run_claude with the given fake processes; return
        ``(result_tuple, popen_env, t2_env)`` where t2_env is {} if no
        fallback subprocess.run was issued."""
        popen_env = {}
        t2_env = {}

        def fake_popen(cmd, **kwargs):
            popen_env.update(kwargs.get('env', {}))
            return popen_proc

        def fake_run(cmd, **kwargs):
            t2_env.update(kwargs.get('env', {}))
            if t2_result is not None:
                return t2_result
            return mock.Mock(
                returncode=0, stdout=_ok_response(), stderr='',
            )

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: (_DEFAULT_TOKEN, 'acct1'),
                                  check_for_rate_limit=lambda _o: False,
                                  detect_cap_in_output=lambda _o: False,
                                  report_rate_limit=lambda *a, **k: None,
                                  report_success=lambda *a, **k: None,
                              )),
            mock.patch.object(ar, 'get_guard', return_value=_NoopGuard()),
            mock.patch('agent_runner.subprocess.Popen',
                       side_effect=fake_popen),
            mock.patch('agent_runner.subprocess.run',
                       side_effect=fake_run),
            mock.patch.object(ar, 'tier2_available', return_value=True),
            mock.patch.object(ar, '_dm_tier2_unavailable'),
            mock.patch.object(ar, '_mark_paused_on_tier1'),
            mock.patch.object(ar, 'append_rate_limit_event'),
            # #438 transcript check: a successful run with a mock session id
            # has no transcript on disk, so the check would write a REAL
            # critical alert to ~/agents/blackboard/larry-alerts.jsonl.
            mock.patch.object(larry_alerts, 'append_alert'),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        result = ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            session_id=session_id,
            timeout=60,
        )
        return result, popen_env, t2_env


class PrimaryDispatchTokenSelectionTest(_RunClaudeHarness):
    """The primary subprocess env carries the setup-token of the active
    tier when one is configured, falling back to the token-manager default
    otherwise."""

    def test_tier1_setup_token_used_when_state_tier1(self):
        self._write_state('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = _TIER1_TOKEN
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _t2_env) = self._drive(proc)
        # Setup-token won; HOME-swap remained intact for session storage.
        self.assertEqual(popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER1_TOKEN)
        self.assertEqual(popen_env['HOME'], '/home/larry')

    def test_tier2_setup_token_used_when_state_tier2(self):
        self._write_state('tier2')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = _TIER1_TOKEN
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _t2_env) = self._drive(proc)
        self.assertEqual(popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER2_TOKEN)
        self.assertEqual(
            popen_env['HOME'], '/home/larry/.claude-larry-personal',
        )

    def test_tier1_default_when_active_tier_token_unset(self):
        # Active=tier1 but only TIER2 token configured → primary dispatch
        # MUST fall back to the legacy default token; no cross-tier leak.
        self._write_state('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _t2_env) = self._drive(proc)
        self.assertEqual(
            popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _DEFAULT_TOKEN,
        )

    def test_empty_string_token_treated_as_unset(self):
        # An empty CLAUDE_CODE_OAUTH_TOKEN_TIER1 is misconfiguration, not
        # an intentional override; we must NOT inject an empty token (the
        # CLI ignores empties anyway, but the legacy path is the right
        # behavior here so byte-for-byte parity holds).
        self._write_state('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = ''
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _t2_env) = self._drive(proc)
        self.assertEqual(
            popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _DEFAULT_TOKEN,
        )


class AbsentTokenIsByteForByteLegacyTest(_RunClaudeHarness):
    """When neither tier has a setup-token, dispatch env is identical to
    today's behavior — token = token-manager default, HOME = active tier
    home. This is the safety property that lets us ship before tokens are
    provisioned everywhere."""

    def test_no_setup_tokens_preserves_legacy_primary_env(self):
        self._write_state('tier1')
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _t2_env) = self._drive(proc)
        self.assertEqual(
            popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _DEFAULT_TOKEN,
        )
        self.assertEqual(popen_env['HOME'], '/home/larry')

    def test_no_setup_tokens_preserves_legacy_fallback_env(self):
        # Tier 1 fails; Tier 2 fallback fires. Without setup-tokens, the
        # fallback subprocess must look exactly like the historical path:
        # HOME swapped to the other tier, token = default.
        self._write_state('tier1')
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, t2_env) = self._drive(proc, t2_result=t2)
        self.assertEqual(popen_env['HOME'], '/home/larry')
        self.assertEqual(
            popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _DEFAULT_TOKEN,
        )
        self.assertEqual(
            t2_env['HOME'], '/home/larry/.claude-larry-personal',
        )
        self.assertEqual(
            t2_env['CLAUDE_CODE_OAUTH_TOKEN'], _DEFAULT_TOKEN,
        )


class FallbackTierTokenSelectionTest(_RunClaudeHarness):
    """The failure-fallback subprocess picks the OTHER tier's setup-token
    when configured; falls back to the legacy default when the other tier
    has no setup-token."""

    def test_fallback_uses_other_tier_setup_token(self):
        self._write_state('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = _TIER1_TOKEN
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, t2_env) = self._drive(proc, t2_result=t2)
        # Primary used Tier1's token; fallback used Tier2's token.
        self.assertEqual(popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER1_TOKEN)
        self.assertEqual(t2_env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER2_TOKEN)
        self.assertEqual(
            t2_env['HOME'], '/home/larry/.claude-larry-personal',
        )

    def test_fallback_reverts_to_default_when_other_tier_token_missing(self):
        # Active tier has a setup-token; the OTHER tier doesn't. The
        # fallback must NOT reuse the active tier's token (that would
        # authenticate as the wrong account); it must revert to the
        # legacy default + HOME-swap path.
        self._write_state('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = _TIER1_TOKEN
        # No CLAUDE_CODE_OAUTH_TOKEN_TIER2 set.
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, t2_env) = self._drive(proc, t2_result=t2)
        self.assertEqual(popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER1_TOKEN)
        self.assertEqual(
            t2_env['CLAUDE_CODE_OAUTH_TOKEN'], _DEFAULT_TOKEN,
        )
        self.assertEqual(
            t2_env['HOME'], '/home/larry/.claude-larry-personal',
        )

    def test_fallback_direction_flips_with_active_tier_state(self):
        # Mirror direction: active=tier2 → fallback uses tier1's
        # setup-token and tier1's HOME.
        self._write_state('tier2')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = _TIER1_TOKEN
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        proc = _FakeProc(
            1,
            stdout="You've hit your limit · resets 11:30am",
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, t2_env) = self._drive(proc, t2_result=t2)
        self.assertEqual(popen_env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER2_TOKEN)
        self.assertEqual(t2_env['CLAUDE_CODE_OAUTH_TOKEN'], _TIER1_TOKEN)
        self.assertEqual(t2_env['HOME'], '/home/larry')


class ResumeNoFallbackRefusalStaysIntactTest(_RunClaudeHarness):
    """Sanity: --resume sessions are account-bound. Even with both
    setup-tokens configured, a resume task that hits Tier 1 failure must
    NOT silently fall back to the other tier — that would orphan the
    session ID. This mirrors the existing
    test_agent_runner_active_tier::ResumeNoFallbackRefusalTest behavior
    under the new auth path."""

    def test_resume_refuses_fallback_with_setup_tokens_configured(self):
        self._write_state('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = _TIER1_TOKEN
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        t2_called = mock.Mock()

        def fake_popen(cmd, **kwargs):
            return proc

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: (_DEFAULT_TOKEN, 'acct1'),
                                  check_for_rate_limit=lambda _o: False,
                                  detect_cap_in_output=lambda _o: False,
                                  report_rate_limit=lambda *a, **k: None,
                                  report_success=lambda *a, **k: None,
                              )),
            mock.patch.object(ar, 'get_guard', return_value=_NoopGuard()),
            mock.patch('agent_runner.subprocess.Popen',
                       side_effect=fake_popen),
            mock.patch('agent_runner.subprocess.run',
                       side_effect=t2_called),
            mock.patch.object(ar, 'tier2_available', return_value=True),
            mock.patch.object(ar, '_dm_tier2_unavailable'),
            mock.patch.object(ar, '_mark_paused_on_tier1'),
            mock.patch.object(ar, 'append_rate_limit_event'),
            mock.patch.object(larry_alerts, 'append_alert'),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        success, output, _ = ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            session_id='sess-abc',
            timeout=60,
        )
        t2_called.assert_not_called()
        self.assertFalse(success)
        self.assertIn('--resume', output)
        self.assertIn('account-bound', output)


class TokenValueNeverLoggedTest(_RunClaudeHarness):
    """Token values must never reach disk. We scan the log file produced by
    a full successful + fallback flow with both setup-tokens populated and
    assert neither token substring appears anywhere."""

    def setUp(self):
        super().setUp()
        # Pin OURLIBERTY_LOG_DIR to a fresh per-test directory so the scan
        # below sees ONLY what this test produced. Order-independent: works
        # whether or not earlier tests left OURLIBERTY_LOG_DIR set (the
        # full-discovery suite has tests that unset it).
        self.log_dir = self.root / 'logs'
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._prev_log_dir = os.environ.get('OURLIBERTY_LOG_DIR')
        os.environ['OURLIBERTY_LOG_DIR'] = str(self.log_dir)

    def tearDown(self):
        if self._prev_log_dir is None:
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
        else:
            os.environ['OURLIBERTY_LOG_DIR'] = self._prev_log_dir
        super().tearDown()

    def test_neither_tier_token_appears_in_log_file(self):
        self._write_state('tier1')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = _TIER1_TOKEN
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = _TIER2_TOKEN
        # Drive a fallback so BOTH log paths (primary + TIER2_FALLBACK_*)
        # get exercised.
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        self._drive(proc, t2_result=t2)

        leaked = []
        for path in self.log_dir.glob('*.log'):
            text = path.read_text()
            if _TIER1_TOKEN in text or _TIER2_TOKEN in text:
                leaked.append(str(path))
        self.assertEqual(leaked, [],
                         f'token value leaked to log file(s): {leaked}')


if __name__ == '__main__':
    unittest.main()
