#!/usr/bin/env python3
"""Tests for the active-tier HOME plumbing in ``agent_runner.run_claude``
(spec § 6.2).

We exercise the Popen / subprocess.run call sites with mocks and assert
the ``env['HOME']`` value tracks ``active_tier.current_home()`` for the
primary subprocess and ``active_tier.other_home()`` for the
failure-fallback retry. The fallback's existing ``--resume`` refusal must
keep working unchanged when the active state flips.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_active_tier
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

import agent_runner as ar  # noqa: E402
import active_tier  # noqa: E402
import larry_alerts  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook name
    # This module mocks subprocess.Popen/run to inspect the env it WOULD spawn
    # with; the Layer B claude-spawn guard sits just before that mocked call and
    # would breach first. Opt out for the module so the guard is a pass-through
    # to the mock (the #428 real-tree scanner still runs).
    global _SAVED_SENTINEL
    _SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook name
    _chokepoint_optout.reengage_guards(_SAVED_SENTINEL)


def _ok_response(text='ok', session_id='sid-new'):
    return json.dumps({'result': text, 'session_id': session_id})


class _FakeProc:
    """Minimal stand-in for ``subprocess.Popen``'s returned object.

    Returns the configured ``returncode`` on first ``poll()`` so the
    run_claude loop exits immediately without sleeping. ``stdin`` is a
    sink; ``stdout`` / ``stderr`` are pre-loaded readers.
    """

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


def _pin_log_dir(tc, root):
    """Route agent_runner.log()'s write-time OURLIBERTY_LOG_DIR resolution
    into the test's tmp tree so un-mocked log() calls (incl. the #438
    TRANSCRIPT_NOT_PERSISTED ERROR) can't land in the real ~/agents/logs/.
    Guards the bare `python3 -m unittest` invocation — the discover gate
    (#436) pins this process-wide, but direct runs have no other layer.
    Restore registers via addCleanup, not tearDown: unittest skips tearDown
    when setUp raises (e.g. the mkdir below), but cleanups still run.
    """
    prev = os.environ.get('OURLIBERTY_LOG_DIR')

    def _restore():
        if prev is None:
            os.environ.pop('OURLIBERTY_LOG_DIR', None)
        else:
            os.environ['OURLIBERTY_LOG_DIR'] = prev

    tc.addCleanup(_restore)
    os.environ['OURLIBERTY_LOG_DIR'] = str(root / 'logs')
    (root / 'logs').mkdir(parents=True, exist_ok=True)


class RunClaudeActiveTierEnvTest(unittest.TestCase):
    """Verify that ``env['HOME']`` follows active-tier state across both
    the primary and fallback subprocess invocations."""

    def setUp(self):
        # Per-test scratch agents-root: the state file is rooted under it
        # via active_tier._state_path()'s OURLIBERTY_AGENTS_ROOT lookup.
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import os
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        # Working dir must exist for Popen's cwd= arg
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)
        _pin_log_dir(self, self.root)

    def tearDown(self):
        import os
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self.tmp.cleanup()

    def _write_state(self, tier):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': tier}))

    def _run(self, popen_proc, t2_result=None, setup_token='setup-tok'):
        """Drive run_claude with the supplied Popen result + optional
        Tier 2 ``subprocess.run`` result. Returns the captured env dicts
        ``(popen_env, run_env)`` (run_env is None if no Tier 2 retry
        fired)."""
        popen_env = {}
        run_env = {}

        def fake_popen(cmd, **kwargs):
            popen_env.update(kwargs.get('env', {}))
            return popen_proc

        def fake_run(cmd, **kwargs):
            run_env.update(kwargs.get('env', {}))
            return t2_result if t2_result else mock.Mock(
                returncode=0, stdout=_ok_response(), stderr='',
            )

        # Patches: stub out all the heavy collaborators that run_claude
        # touches. Each patch is justified inline so future readers can
        # tell what's load-bearing and what's just noise-suppression.
        patches = [
            # token manager: hand back a stable token + account
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
                                  check_for_rate_limit=lambda _o: False,
                                  detect_cap_in_output=lambda _o: False,
                                  report_rate_limit=lambda *a, **k: None,
                                  report_success=lambda *a, **k: None,
                              )),
            # concurrency guard: always grant a slot
            mock.patch.object(ar, 'get_guard', return_value=_NoopGuard()),
            # the two subprocess entry points
            mock.patch('agent_runner.subprocess.Popen',
                       side_effect=fake_popen),
            mock.patch('agent_runner.subprocess.run',
                       side_effect=fake_run),
            # filesystem probes the fallback path needs to evaluate True
            mock.patch.object(ar, 'tier2_available', return_value=True),
            # Control the per-tier setup-token deterministically — the real
            # _setup_token_for_tier falls back to the on-disk credentials
            # file, which would otherwise leak ambient state into HOME logic.
            mock.patch.object(ar.active_tier, '_setup_token_for_tier',
                              side_effect=lambda tier: setup_token),
            # Tier-2 unavailable DM + paused marker must not fire in
            # tests; if they do, surface the call so the test fails loud
            mock.patch.object(ar, '_dm_tier2_unavailable'),
            mock.patch.object(ar, '_mark_paused_on_tier1'),
            # ledger I/O — no-op so tests don't touch ~/agents
            mock.patch.object(ar, 'append_rate_limit_event'),
            # #438 transcript check: success with a mock session id would
            # write a REAL critical alert to the live larry-alerts ledger
            mock.patch.object(larry_alerts, 'append_alert'),
            # CLAUDE.md poison guard + landmine scrub — irrelevant here
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            # suppress the time.sleep inside the cancel-poll loop and the
            # retry backoff (the fake proc returns immediately, but the
            # cancel-poll loop body still sleeps once)
            mock.patch('agent_runner.time.sleep'),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        return ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            timeout=60,
        ), popen_env, run_env

    # ---- primary HOME (default tier1) ----------------------------------

    def test_primary_home_tier1_when_state_missing(self):
        # Acceptance: missing state → HOME=/home/larry (today's behavior).
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _run_env) = self._run(proc)
        self.assertEqual(popen_env['HOME'], '/home/larry')

    def test_primary_home_tier1_when_state_tier1(self):
        self._write_state('tier1')
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _run_env) = self._run(proc)
        self.assertEqual(popen_env['HOME'], '/home/larry')

    def test_primary_home_tier2_with_setup_token_keeps_real_home(self):
        # Root fix: token auth is HOME-independent, so a tier2 dispatch keeps
        # the fully-provisioned real home (MCP/settings/projects/transcripts).
        self._write_state('tier2')
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _run_env) = self._run(proc)
        self.assertEqual(popen_env['HOME'], '/home/larry')

    def test_primary_home_tier2_without_setup_token_swaps(self):
        # Legacy fallback preserved: with NO setup-token, auth must come from
        # the tier's creds.json, so HOME still swaps to the tier home.
        self._write_state('tier2')
        proc = _FakeProc(0, stdout=_ok_response())
        (_result, popen_env, _run_env) = self._run(proc, setup_token=None)
        self.assertEqual(
            popen_env['HOME'], '/home/larry/.claude-larry-personal',
        )

    # ---- fallback HOME ------------------------------------------------

    def test_fallback_from_tier1_with_setup_token_keeps_real_home(self):
        # Tier 1 rate-limit → fallback to tier2 token, but with a setup-token
        # HOME stays real for the fallback subprocess too.
        self._write_state('tier1')
        proc = _FakeProc(
            1,
            stdout="You've hit your limit · resets 11:30am",
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, run_env) = self._run(proc, t2_result=t2)
        self.assertEqual(popen_env['HOME'], '/home/larry')
        self.assertEqual(run_env['HOME'], '/home/larry')

    def test_fallback_from_tier1_without_setup_token_swaps(self):
        # No setup-token → fallback subprocess swaps to the tier2 home.
        self._write_state('tier1')
        proc = _FakeProc(
            1,
            stdout="You've hit your limit · resets 11:30am",
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, run_env) = self._run(
            proc, t2_result=t2, setup_token=None)
        self.assertEqual(popen_env['HOME'], '/home/larry')
        self.assertEqual(
            run_env['HOME'], '/home/larry/.claude-larry-personal',
        )

    def test_fallback_from_tier2_with_setup_token_keeps_real_home(self):
        # state=tier2 primary fails → fallback to tier1 token. With a
        # setup-token both subprocesses keep the real home.
        self._write_state('tier2')
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, run_env) = self._run(proc, t2_result=t2)
        self.assertEqual(popen_env['HOME'], '/home/larry')
        self.assertEqual(run_env['HOME'], '/home/larry')

    def test_fallback_from_tier2_without_setup_token_swaps(self):
        # No setup-token → primary swaps to tier2 home, fallback to tier1 home.
        self._write_state('tier2')
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        t2 = mock.Mock(returncode=0, stdout=_ok_response(), stderr='')
        (_result, popen_env, run_env) = self._run(
            proc, t2_result=t2, setup_token=None)
        self.assertEqual(
            popen_env['HOME'], '/home/larry/.claude-larry-personal',
        )
        self.assertEqual(run_env['HOME'], '/home/larry')


class ResumeNoFallbackRefusalTest(unittest.TestCase):
    """The existing ``--resume`` refusal must remain intact: a Tier 1
    failure during a resume session NEVER falls back, regardless of the
    active-tier state, because session IDs are account-bound."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import os
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)
        _pin_log_dir(self, self.root)

    def tearDown(self):
        import os
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self.tmp.cleanup()

    def test_resume_refuses_fallback_when_tier1_rate_limits(self):
        proc = _FakeProc(
            1,
            stdout="You've hit your limit · resets 11:30am",
            stderr='',
        )
        run_called = mock.Mock()

        def fake_popen(cmd, **kwargs):
            return proc

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
                                  check_for_rate_limit=lambda _o: False,
                                  detect_cap_in_output=lambda _o: False,
                                  report_rate_limit=lambda *a, **k: None,
                                  report_success=lambda *a, **k: None,
                              )),
            mock.patch.object(ar, 'get_guard', return_value=_NoopGuard()),
            mock.patch('agent_runner.subprocess.Popen',
                       side_effect=fake_popen),
            mock.patch('agent_runner.subprocess.run',
                       side_effect=run_called),
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

        # Refused fallback → no subprocess.run() call ever happened
        run_called.assert_not_called()
        self.assertFalse(success)
        self.assertIn('--resume', output)
        self.assertIn('account-bound', output)


class AuthCircuitBreakerTest(unittest.TestCase):
    """Step A rotation fix: a primary-tier auth_401 must park the active
    tier via set_cooldown(kind='auth_401') so a single bad token cannot
    storm. Pre-fix, auth_401 only triggered the fallback leg and never
    cooled down the bad tier — every dispatch in the window repeated the
    failure (~every 90s) until rotation was disabled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import os
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)
        _pin_log_dir(self, self.root)

    def tearDown(self):
        import os
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self.tmp.cleanup()

    def _write_state(self, tier):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': tier}))

    def test_auth_401_on_primary_sets_cooldown_on_active_tier(self):
        # State is tier1; the primary subprocess returns an auth_401.
        # Expect: active_tier.set_cooldown(<active tier>, kind='auth_401')
        # is called BEFORE the Tier 2 fallback fires.
        self._write_state('tier1')
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        set_cooldown_calls = []

        def fake_set_cooldown(tier, raw_excerpt='', now=None,
                              kind='rate_limit'):
            set_cooldown_calls.append(
                {'tier': tier, 'kind': kind},
            )

        def fake_popen(cmd, **kwargs):
            return proc

        def fake_run(cmd, **kwargs):
            return mock.Mock(
                returncode=0, stdout=_ok_response(), stderr='',
            )

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
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
            mock.patch.object(larry_alerts, 'append_alert'),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
            mock.patch.object(active_tier, 'set_cooldown',
                              side_effect=fake_set_cooldown),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            timeout=60,
        )
        # The active tier (tier1) gets parked with kind=auth_401.
        auth_calls = [c for c in set_cooldown_calls
                      if c['kind'] == 'auth_401']
        self.assertEqual(len(auth_calls), 1)
        self.assertEqual(auth_calls[0]['tier'], 'tier1')

    def test_auth_401_on_tier2_when_active(self):
        # Mirror direction: state=tier2, primary auth_401 → tier2 parks.
        self._write_state('tier2')
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        set_cooldown_calls = []

        def fake_set_cooldown(tier, raw_excerpt='', now=None,
                              kind='rate_limit'):
            set_cooldown_calls.append(
                {'tier': tier, 'kind': kind},
            )

        def fake_popen(cmd, **kwargs):
            return proc

        def fake_run(cmd, **kwargs):
            return mock.Mock(
                returncode=0, stdout=_ok_response(), stderr='',
            )

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
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
            mock.patch.object(larry_alerts, 'append_alert'),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
            mock.patch.object(active_tier, 'set_cooldown',
                              side_effect=fake_set_cooldown),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            timeout=60,
        )
        auth_calls = [c for c in set_cooldown_calls
                      if c['kind'] == 'auth_401']
        self.assertEqual(len(auth_calls), 1)
        self.assertEqual(auth_calls[0]['tier'], 'tier2')

    def test_rate_limit_still_uses_default_kind(self):
        # Regression guard: rate_limit must keep landing as kind='rate_limit'
        # (default), not the auth_401 branch.
        self._write_state('tier1')
        proc = _FakeProc(
            1,
            stdout="You've hit your limit · resets 11:30am",
            stderr='',
        )
        set_cooldown_calls = []

        def fake_set_cooldown(tier, raw_excerpt='', now=None,
                              kind='rate_limit'):
            set_cooldown_calls.append(
                {'tier': tier, 'kind': kind},
            )

        def fake_popen(cmd, **kwargs):
            return proc

        def fake_run(cmd, **kwargs):
            return mock.Mock(
                returncode=0, stdout=_ok_response(), stderr='',
            )

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
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
            mock.patch.object(larry_alerts, 'append_alert'),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
            mock.patch.object(active_tier, 'set_cooldown',
                              side_effect=fake_set_cooldown),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            timeout=60,
        )
        kinds = {c['kind'] for c in set_cooldown_calls}
        # rate_limit path must NOT silently route through auth_401.
        self.assertNotIn('auth_401', kinds)


class TierFailureLogTaggingTest(unittest.TestCase):
    """Step A rotation fix: the failure log line must carry the actual
    failing tier name. Pre-fix, the legacy `TIER1_FAILURE_DETECTED` token
    hardcoded `tier1` even when the call ran on Tier 2 under rotation —
    incident triage couldn't tell which account actually failed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import os
        self._prev_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        self.workdir = self.root / 'work'
        self.workdir.mkdir(parents=True, exist_ok=True)
        _pin_log_dir(self, self.root)

    def tearDown(self):
        import os
        if self._prev_root is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prev_root
        self.tmp.cleanup()

    def _write_state(self, tier):
        path = self.root / 'blackboard' / 'active-tier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'tier': tier}))

    def _run_with_failure(self, active_tier_name):
        self._write_state(active_tier_name)
        proc = _FakeProc(
            1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        log_lines = []

        def fake_log(agent_id, message, level='INFO'):
            log_lines.append({'level': level, 'message': message})

        def fake_popen(cmd, **kwargs):
            return proc

        def fake_run(cmd, **kwargs):
            return mock.Mock(
                returncode=0, stdout=_ok_response(), stderr='',
            )

        patches = [
            mock.patch.object(ar, 'get_manager',
                              return_value=mock.Mock(
                                  get_token=lambda: ('tok', 'acct1'),
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
            mock.patch.object(larry_alerts, 'append_alert'),
            mock.patch.object(ar, 'quarantine_parent_claude_md_poison'),
            mock.patch.object(ar, 'scrub_tmp_identity_landmines'),
            mock.patch('agent_runner.time.sleep'),
            mock.patch.object(active_tier, 'set_cooldown'),
            mock.patch.object(ar, 'log', side_effect=fake_log),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        ar.run_claude(
            agent_id='forge',
            prompt='hello',
            working_dir=str(self.workdir),
            timeout=60,
        )
        return log_lines

    def test_failure_log_names_tier1_when_active(self):
        lines = self._run_with_failure('tier1')
        detection_lines = [
            l for l in lines
            if 'TIER_FAILURE_DETECTED' in l['message']
        ]
        self.assertEqual(len(detection_lines), 1)
        self.assertIn('tier=tier1', detection_lines[0]['message'])

    def test_failure_log_names_tier2_when_active(self):
        # The pre-fix log token hardcoded "tier1" even when state=tier2;
        # this is the regression guard that the rename actually plumbs
        # through the real failing tier.
        lines = self._run_with_failure('tier2')
        detection_lines = [
            l for l in lines
            if 'TIER_FAILURE_DETECTED' in l['message']
        ]
        self.assertEqual(len(detection_lines), 1)
        self.assertIn('tier=tier2', detection_lines[0]['message'])


if __name__ == '__main__':
    unittest.main()
