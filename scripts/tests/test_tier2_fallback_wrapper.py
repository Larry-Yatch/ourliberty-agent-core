#!/usr/bin/env python3
"""Tests for the Claude Max Tier 2 quota+auth fallback wrapper
(claude-quota-tier2-fallback-wrapper, 2026-05-26).

Covers the detection + dispatch shape added to:

  - scripts/beacon_telegram_bot.py — classify_tier1_failure, tier2_available
  - scripts/agent_runner.py — classify_tier1_failure, tier2_available,
    _mark_paused_on_tier1, _dm_tier2_unavailable

We deliberately do NOT exercise the full Popen flow in run_claude — the
cancel-marker + concurrency-guard scaffolding is integration-heavy and
already covered by existing test files. Instead we test the pure
classification + state-marker helpers, and assert the regex shapes match
the strings observed in the 2026-05-26 incident.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_tier2_fallback_wrapper
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
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


# ---- agent_runner classification + state tests --------------------------

class AgentRunnerClassifyTest(unittest.TestCase):
    """Detection lives in agent_runner; test both regexes against the
    strings the Claude CLI actually emitted on 2026-05-26."""

    def setUp(self):
        # Defer import so we don't blow up at module-load time if the file
        # is being edited.
        self.ar = importlib.import_module('agent_runner')

    def test_rate_limit_phrase_hit_your_limit(self):
        # Today's incident phrasing — verbatim from beacon_telegram_bot.log
        out = "You've hit your limit · resets 11:30am"
        self.assertEqual(self.ar.classify_tier1_failure(out, ''), 'rate_limit')

    def test_rate_limit_phrase_5_hour(self):
        self.assertEqual(
            self.ar.classify_tier1_failure(
                'You have used your 5-hour quota.', ''),
            'rate_limit',
        )

    def test_rate_limit_phrase_resets_with_time(self):
        self.assertEqual(
            self.ar.classify_tier1_failure('Quota resets 12 hours from now.', ''),
            'rate_limit',
        )

    def test_auth_401_invalid_credentials(self):
        out = 'Invalid authentication credentials'
        self.assertEqual(self.ar.classify_tier1_failure(out, ''), 'auth_401')

    def test_auth_401_bare_status_code(self):
        # The CLI prints '401' as part of the error body in some shapes
        self.assertEqual(
            self.ar.classify_tier1_failure('HTTP 401 Unauthorized', ''),
            'auth_401',
        )

    def test_auth_401_failed_to_authenticate(self):
        self.assertEqual(
            self.ar.classify_tier1_failure('Failed to authenticate', ''),
            'auth_401',
        )

    def test_clean_output_returns_none(self):
        # No false positives on success-shaped output
        ok = json.dumps({'result': 'ok', 'session_id': 'abc'})
        self.assertIsNone(self.ar.classify_tier1_failure(ok, ''))

    def test_stderr_input_combined_with_stdout(self):
        # The fix's whole point: detection must read BOTH streams. If the
        # message landed on stderr only (or vice versa), still classify.
        self.assertEqual(
            self.ar.classify_tier1_failure('', 'You have hit your limit.'),
            'rate_limit',
        )
        self.assertEqual(
            self.ar.classify_tier1_failure('', 'HTTP 401'),
            'auth_401',
        )

    def test_rate_limit_takes_precedence_over_auth(self):
        # When both match, rate-limit wins — the recovery shape is the
        # same (Tier 2 retry), but the DM body diverges.
        mixed = "hit your limit AND 401 from upstream"
        self.assertEqual(
            self.ar.classify_tier1_failure(mixed, ''), 'rate_limit',
        )

    def test_session_lost_not_auth_401(self):
        # The 2026-06-10 18:57Z incident: a lost --resume session whose
        # UUID contains the digits '401'. Must classify session_lost, NOT
        # auth_401 (the bare-401 matcher used to trip on the UUID).
        out = 'No conversation found with session ID: 32401737-1d2e-4f3a-...'
        self.assertEqual(
            self.ar.classify_tier1_failure(out, ''), 'session_lost',
        )

    def test_session_lost_case_insensitive(self):
        out = 'no conversation found with session id: abc'
        self.assertEqual(
            self.ar.classify_tier1_failure(out, ''), 'session_lost',
        )

    def test_session_lost_on_stderr_stream(self):
        self.assertEqual(
            self.ar.classify_tier1_failure(
                '', 'No conversation found with session ID: deadbeef'),
            'session_lost',
        )

    def test_uuid_embedded_401_no_auth_context_returns_none(self):
        # A bare UUID/hex run that happens to contain '401' but carries no
        # auth context must NOT classify as auth_401.
        out = 'request id 32401737beef failed during processing'
        self.assertIsNone(self.ar.classify_tier1_failure(out, ''))

    def test_uuid_with_dashes_embedded_401_returns_none(self):
        out = 'trace 32401737-1d2e-4f3a-9b8c-aabbccddeeff timed out'
        self.assertIsNone(self.ar.classify_tier1_failure(out, ''))

    def test_real_http_401_still_auth_401(self):
        # The UUID-safe regex must keep matching genuine HTTP 401s.
        self.assertEqual(
            self.ar.classify_tier1_failure('HTTP 401 Unauthorized', ''),
            'auth_401',
        )

    def test_standalone_401_with_punctuation_is_auth_401(self):
        self.assertEqual(
            self.ar.classify_tier1_failure('upstream returned 401.', ''),
            'auth_401',
        )

    def test_rate_limit_still_top_precedence_over_session_lost(self):
        # Rate-limit precedence is unchanged even when a session-lost phrase
        # co-occurs.
        mixed = ('You have hit your limit. Also: No conversation found '
                 'with session ID: 32401737-...')
        self.assertEqual(
            self.ar.classify_tier1_failure(mixed, ''), 'rate_limit',
        )


class Tier2AvailableTest(unittest.TestCase):
    """tier2_available() is a filesystem probe. Mock TIER2_HOME to a
    temp directory and toggle the credentials file."""

    def setUp(self):
        self.ar = importlib.import_module('agent_runner')

    def test_returns_false_when_credentials_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.ar, 'TIER2_HOME', tmp):
                self.assertFalse(self.ar.tier2_available())

    def test_returns_true_when_credentials_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds_dir = Path(tmp, '.claude')
            creds_dir.mkdir(parents=True)
            (creds_dir / '.credentials.json').write_text('{}')
            with mock.patch.object(self.ar, 'TIER2_HOME', tmp):
                self.assertTrue(self.ar.tier2_available())


class Tier2FallbackAvailableTest(unittest.TestCase):
    """_tier2_fallback_available(): setup-token (preferred) OR creds file. A
    valid setup-token must survive an expired/removed credentials.json so the
    fallback is never falsely declared unavailable (2026-06-03 fix)."""

    def setUp(self):
        self.ar = importlib.import_module('agent_runner')

    def test_true_when_setup_token_present_even_without_creds_file(self):
        with mock.patch('active_tier._setup_token_for_tier',
                        return_value='sk-ant-oat01-x'), \
                mock.patch.object(self.ar, 'tier2_available', return_value=False):
            self.assertTrue(self.ar._tier2_fallback_available())

    def test_true_when_no_token_but_creds_file_present(self):
        with mock.patch('active_tier._setup_token_for_tier', return_value=None), \
                mock.patch.object(self.ar, 'tier2_available', return_value=True):
            self.assertTrue(self.ar._tier2_fallback_available())

    def test_false_when_no_token_and_no_creds_file(self):
        with mock.patch('active_tier._setup_token_for_tier', return_value=None), \
                mock.patch.object(self.ar, 'tier2_available', return_value=False):
            self.assertFalse(self.ar._tier2_fallback_available())


class MarkPausedOnTier1Test(unittest.TestCase):
    """The in-flight state-file marker that heal_pipeline_stall's Check 8
    will key on. Test that the helper:
      - is a no-op when task_stem is None
      - writes paused_on_tier1 with failure_type + at
      - preserves existing keys in the in-flight file
    """

    def setUp(self):
        self.ar = importlib.import_module('agent_runner')
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_noop_when_task_stem_none(self):
        with mock.patch.object(self.ar, 'IN_FLIGHT_DIR', self.tmp_path):
            self.ar._mark_paused_on_tier1(None, 'rate_limit')
        self.assertEqual(list(self.tmp_path.iterdir()), [])

    def test_writes_marker_on_fresh_task(self):
        with mock.patch.object(self.ar, 'IN_FLIGHT_DIR', self.tmp_path):
            self.ar._mark_paused_on_tier1('task-abc', 'auth_401')
        target = self.tmp_path / 'task-abc.json'
        self.assertTrue(target.exists())
        data = json.loads(target.read_text())
        self.assertEqual(data['paused_on_tier1']['failure_type'], 'auth_401')
        self.assertIn('at', data['paused_on_tier1'])

    def test_preserves_existing_keys(self):
        # The in-flight file pre-exists with pid/started_at; our marker
        # must not clobber those.
        target = self.tmp_path / 'task-xyz.json'
        target.write_text(json.dumps({
            'task_stem': 'task-xyz', 'agent_id': 'forge',
            'pid': 1234, 'started_at': '2026-05-26T18:00:00',
        }))
        with mock.patch.object(self.ar, 'IN_FLIGHT_DIR', self.tmp_path):
            self.ar._mark_paused_on_tier1('task-xyz', 'rate_limit')
        data = json.loads(target.read_text())
        self.assertEqual(data['pid'], 1234)
        self.assertEqual(data['agent_id'], 'forge')
        self.assertEqual(data['paused_on_tier1']['failure_type'], 'rate_limit')


class DmTier2UnavailableTierLabelTest(unittest.TestCase):
    """_dm_tier2_unavailable must name the ACTUAL failing tier in the alert
    body rather than a hardcoded 'Tier 1' — under rotation the primary
    subprocess can run on either tier."""

    def setUp(self):
        self.ar = importlib.import_module('agent_runner')

    def _capture_alert(self, **kwargs):
        captured = {}

        def _fake_append_alert(**akw):
            captured.update(akw)
            return True

        import larry_alerts as la
        with mock.patch.object(la, 'append_alert', _fake_append_alert):
            self.ar._dm_tier2_unavailable(**kwargs)
        return captured

    def test_names_tier2_when_failing_tier_is_tier2(self):
        captured = self._capture_alert(
            failure_type='auth_401', task_stem='t', agent_id='forge',
            session_id=None, tier='tier2',
        )
        self.assertIn('Tier 2', captured.get('message', ''))
        self.assertNotIn('Tier 1', captured.get('message', ''))

    def test_defaults_to_tier1_label_when_unspecified(self):
        captured = self._capture_alert(
            failure_type='auth_401', task_stem='t', agent_id='forge',
            session_id=None,
        )
        self.assertIn('Tier 1', captured.get('message', ''))


# ---- beacon_telegram_bot classification + Tier 2 ------------------------

class _BotImporter:
    """beacon_telegram_bot has env-required imports at module load
    (TELEGRAM_BOT_TOKEN_BEACON + TELEGRAM_ALLOWED_CHAT_IDS). We can't
    import it directly under unittest without those env vars."""

    @staticmethod
    def load_with_env():
        os.environ.setdefault('TELEGRAM_BOT_TOKEN_BEACON', 'TEST_TOKEN')
        os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_IDS', '12345')
        # Force a clean import each test so module-level state doesn't leak
        if 'beacon_telegram_bot' in sys.modules:
            del sys.modules['beacon_telegram_bot']
        return importlib.import_module('beacon_telegram_bot')


class BotClassifyTest(unittest.TestCase):
    """Bot's classify_tier1_failure mirrors agent_runner's. We test the
    same shapes here so the two modules can't drift independently."""

    def setUp(self):
        self.bot = _BotImporter.load_with_env()

    def test_rate_limit_phrase(self):
        out = "You've hit your limit · resets 11:30am"
        self.assertEqual(
            self.bot.classify_tier1_failure(out, ''), 'rate_limit',
        )

    def test_auth_401_phrase(self):
        out = 'Invalid authentication credentials'
        self.assertEqual(
            self.bot.classify_tier1_failure(out, ''), 'auth_401',
        )

    def test_clean_output_returns_none(self):
        self.assertIsNone(
            self.bot.classify_tier1_failure('hello world', ''),
        )

    def test_tier2_available_filesystem_probe(self):
        # tier2_available() should be False when the path doesn't exist
        with mock.patch.object(self.bot, 'TIER2_HOME', '/nonexistent/path'):
            self.assertFalse(self.bot.tier2_available())


# ---- call_beacon flow tests --------------------------------------------

class CallBeaconTier2Test(unittest.TestCase):
    """End-to-end-ish: mock _run_claude_once to exercise the Tier 2
    branch in call_beacon. Verifies that:
      - rate-limit-classified Tier 1 failure → Tier 2 retry attempted
      - successful Tier 2 → reply returned, log shape correct
      - Tier 2 unavailable → DM fired, error returned
      - --resume on bot is allowed (bot does not enforce resume-discipline)
    """

    def setUp(self):
        # These tests exercise the legacy HOME-swap fallback (no setup-token),
        # so clear any ambient CLAUDE_CODE_OAUTH_TOKEN_TIER2 the live host has
        # set; restore it on teardown. The token-present behavior is covered
        # by CallBeaconTier2SetupTokenTest below.
        self._prev_t2 = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        # Isolate the durable-token file fallback (_setup_token_for_tier reads
        # ~/credentials/.env.larry when the env var is unset) so the no-token
        # HOME-swap path stays reachable on the droplet where the file exists.
        self._prev_env_file = os.environ.get('OURLIBERTY_CREDENTIALS_ENV_FILE')
        os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = (
            '/nonexistent-ourliberty-test/.env.larry')
        self.bot = _BotImporter.load_with_env()
        # Step C quota-ledger writes resolve OURLIBERTY_AGENTS_ROOT at CALL
        # time — these call_beacon tests drive real tier-failure paths, so
        # without this patch every run appends to the real
        # anthropic-quota-events.jsonl (test-jail audit, the #412 class).
        _p = mock.patch.object(self.bot, '_append_bot_quota_event')
        self.quota_mock = _p.start()
        self.addCleanup(_p.stop)
        # #780 added a pre-flight tier gate to call_beacon: it calls
        # active_tier.select_dispatch_tier(...) and returns a canned
        # "All accounts are rate-limited" reply when that yields None — which
        # it does under the sandbox (no live tier state), short-circuiting
        # before the tier1->tier2 fallback branch these tests exercise. Pin a
        # healthy primary so call_beacon reaches the fallback path.
        _pt = mock.patch.object(self.bot.active_tier, 'select_dispatch_tier',
                                return_value='tier1')
        _pt.start()
        self.addCleanup(_pt.stop)
        # The §6/§8 pool-aware fallback (fallback_tier_pool_aware) now picks the
        # SIBLING primary first — on the live pool that resolves to tier3, not
        # tier2. These tests are specifically about the tier1->TIER2 (laptop
        # HOME-swap) fallback: they assert home_override==TIER2_HOME and a
        # "Tier 2 ..." reply. Pin the fallback to tier2 so the branch under test
        # is the one exercised. (Also covers the §4 TOCTOU re-derivation, which
        # calls the same fn.)
        _pf = mock.patch.object(self.bot.active_tier,
                                'fallback_tier_pool_aware', return_value='tier2')
        _pf.start()
        self.addCleanup(_pf.stop)

    def tearDown(self):
        if self._prev_t2 is None:
            os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        else:
            os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = self._prev_t2
        if self._prev_env_file is None:
            os.environ.pop('OURLIBERTY_CREDENTIALS_ENV_FILE', None)
        else:
            os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = self._prev_env_file

    def test_tier1_success_no_tier2_attempted(self):
        ok = mock.Mock(returncode=0, stdout=json.dumps({
            'result': 'hi', 'session_id': 'new-sid',
        }), stderr='')
        with mock.patch.object(self.bot, '_run_claude_once',
                                return_value=ok) as run_mock:
            reply, sid = self.bot.call_beacon('hello', None)
        self.assertEqual(reply, 'hi')
        self.assertEqual(sid, 'new-sid')
        # Only one invocation (Tier 1) — no Tier 2 retry
        self.assertEqual(run_mock.call_count, 1)

    def test_tier1_rate_limit_triggers_tier2_success(self):
        tier1 = mock.Mock(
            returncode=1,
            stdout="You've hit your limit · resets 11:30am",
            stderr='',
        )
        tier2 = mock.Mock(
            returncode=0,
            stdout=json.dumps({'result': 'recovered', 'session_id': 'sid2'}),
            stderr='',
        )
        with mock.patch.object(self.bot, '_run_claude_once',
                                side_effect=[tier1, tier2]) as run_mock, \
             mock.patch.object(self.bot, 'tier2_available',
                                return_value=True):
            reply, sid = self.bot.call_beacon('hello', None)
        self.assertEqual(reply, 'recovered')
        self.assertEqual(sid, 'sid2')
        # Two invocations: Tier 1 then Tier 2
        self.assertEqual(run_mock.call_count, 2)
        # The Tier 2 call must have had HOME swapped
        tier2_kwargs = run_mock.call_args_list[1].kwargs
        self.assertEqual(tier2_kwargs.get('home_override'),
                         self.bot.TIER2_HOME)

    def test_tier1_auth_401_triggers_tier2_attempt(self):
        tier1 = mock.Mock(
            returncode=1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        tier2 = mock.Mock(
            returncode=0,
            stdout=json.dumps({'result': 'ok', 'session_id': 's'}),
            stderr='',
        )
        with mock.patch.object(self.bot, '_run_claude_once',
                                side_effect=[tier1, tier2]), \
             mock.patch.object(self.bot, 'tier2_available',
                                return_value=True):
            reply, _ = self.bot.call_beacon('hello', None)
        self.assertEqual(reply, 'ok')

    def test_tier1_failure_tier2_unavailable_dms_larry(self):
        tier1 = mock.Mock(
            returncode=1,
            stdout='Invalid authentication credentials',
            stderr='',
        )
        with mock.patch.object(self.bot, '_run_claude_once',
                                return_value=tier1), \
             mock.patch.object(self.bot, 'tier2_available',
                                return_value=False), \
             mock.patch.object(self.bot.larry_alerts,
                                'append_alert') as dm_mock:
            reply, _ = self.bot.call_beacon('hello', None)
        self.assertIn('Tier 2 unavailable', reply)
        dm_mock.assert_called_once()
        # The subject must include the failure-type bucket
        kwargs = dm_mock.call_args.kwargs
        self.assertIn('claude_tier1_failed_tier2_unavailable',
                      kwargs.get('subject', ''))
        self.assertIn('auth_401', kwargs.get('subject', ''))

    def test_tier1_failure_tier2_also_fails_dms_larry(self):
        tier1 = mock.Mock(
            returncode=1,
            stdout="You've hit your limit",
            stderr='',
        )
        tier2 = mock.Mock(returncode=1, stdout='also broken', stderr='')
        with mock.patch.object(self.bot, '_run_claude_once',
                                side_effect=[tier1, tier2]), \
             mock.patch.object(self.bot, 'tier2_available',
                                return_value=True), \
             mock.patch.object(self.bot.larry_alerts,
                                'append_alert') as dm_mock:
            reply, _ = self.bot.call_beacon('hello', None)
        self.assertIn('Tier 2 retry also failed', reply)
        dm_mock.assert_called_once()


class CallBeaconTier2SetupTokenTest(unittest.TestCase):
    """When the Tier 2 long-lived setup-token is configured, the bot's Tier 2
    fallback authenticates via it (mirroring agent_runner._apply_tier_auth)
    instead of the HOME-swap->credentials.json path — eliminating the false
    'Tier 2 down' failures once the Tier 2 creds.json lapsed."""

    _TIER2_TOKEN = 'FAKE-beacon-tier2-token-not-a-real-key-zzzzzzzzzzzz'

    def setUp(self):
        self._prev_t2 = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN_TIER2')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = self._TIER2_TOKEN
        self.bot = _BotImporter.load_with_env()
        # See CallBeaconTier2Test.setUp — same quota-ledger barrier.
        _p = mock.patch.object(self.bot, '_append_bot_quota_event')
        self.quota_mock = _p.start()
        self.addCleanup(_p.stop)
        # See CallBeaconTier2Test.setUp — #780 pre-flight tier gate. Pin a
        # healthy primary so call_beacon reaches the tier1->tier2 fallback.
        _pt = mock.patch.object(self.bot.active_tier, 'select_dispatch_tier',
                                return_value='tier1')
        _pt.start()
        self.addCleanup(_pt.stop)
        # See CallBeaconTier2Test.setUp — the pool-aware fallback picks tier3 on
        # the live pool; pin tier2 so _setup_token_for_tier reads the TIER2 env
        # (the fake token this class sets) and the retry HOME is TIER2_HOME.
        _pf = mock.patch.object(self.bot.active_tier,
                                'fallback_tier_pool_aware', return_value='tier2')
        _pf.start()
        self.addCleanup(_pf.stop)

    def tearDown(self):
        if self._prev_t2 is None:
            os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        else:
            os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = self._prev_t2

    def test_fallback_applies_setup_token(self):
        tier1 = mock.Mock(
            returncode=1, stdout='Invalid authentication credentials', stderr='',
        )
        tier2 = mock.Mock(
            returncode=0,
            stdout=json.dumps({'result': 'recovered', 'session_id': 'sid2'}),
            stderr='',
        )
        with mock.patch.object(self.bot, '_run_claude_once',
                                side_effect=[tier1, tier2]) as run_mock:
            reply, sid = self.bot.call_beacon('hello', None)
        self.assertEqual(reply, 'recovered')
        self.assertEqual(sid, 'sid2')
        # The Tier 2 call carried the setup-token. On the setup-token path HOME
        # is intentionally DECOUPLED from the tier (auth rides on the token, not
        # on the tier HOME's credentials.json), so call_beacon keeps the real
        # HOME (TIER1_HOME) rather than swapping to TIER2_HOME — mirroring
        # agent_runner._apply_tier_auth. (The HOME-swap is only used on the
        # no-token creds.json fallback, covered by CallBeaconTier2Test.)
        tier2_kwargs = run_mock.call_args_list[1].kwargs
        self.assertEqual(tier2_kwargs.get('oauth_token'), self._TIER2_TOKEN)
        self.assertEqual(tier2_kwargs.get('home_override'), self.bot.active_tier.TIER1_HOME)

    def test_token_present_skips_unavailable_gate(self):
        # creds.json missing (tier2_available False) must NOT short-circuit
        # the retry when a setup-token is configured — dispatch via the token
        # does not depend on creds.json.
        tier1 = mock.Mock(
            returncode=1, stdout='Invalid authentication credentials', stderr='',
        )
        tier2 = mock.Mock(
            returncode=0,
            stdout=json.dumps({'result': 'recovered', 'session_id': 'sid2'}),
            stderr='',
        )
        with mock.patch.object(self.bot, '_run_claude_once',
                                side_effect=[tier1, tier2]) as run_mock, \
             mock.patch.object(self.bot, 'tier2_available', return_value=False):
            reply, _sid = self.bot.call_beacon('hello', None)
        self.assertEqual(reply, 'recovered')
        # Two calls: the unavailable gate did not abort before the retry.
        self.assertEqual(run_mock.call_count, 2)

    def test_token_value_not_logged(self):
        tier1 = mock.Mock(
            returncode=1, stdout='Invalid authentication credentials', stderr='',
        )
        tier2 = mock.Mock(returncode=1, stdout='still broken', stderr='')
        logged: list[str] = []
        with mock.patch.object(self.bot, '_run_claude_once',
                                side_effect=[tier1, tier2]), \
             mock.patch.object(self.bot, 'tier2_available', return_value=True), \
             mock.patch.object(self.bot, 'log',
                                side_effect=lambda m: logged.append(m)), \
             mock.patch.object(self.bot.larry_alerts, 'append_alert'):
            self.bot.call_beacon('hello', None)
        self.assertFalse(
            any(self._TIER2_TOKEN in m for m in logged),
            'setup-token value leaked into a log line',
        )


# ---- credential-drift healer Tier 2 scan -------------------------------

class HealCredentialRegistryDriftTier2Test(unittest.TestCase):
    """scan_claude_cli now disambiguates Tier 1 vs Tier 2 by path."""

    def setUp(self):
        self.healer = importlib.import_module('heal_credential_registry_drift')
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_creds(self, path: Path, token: str = 'sk-deadbeef'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'claudeAiOauth': {'accessToken': token},
        }))

    def test_tier1_path_returns_claude_max_oauth(self):
        creds = self.tmp_path / '.claude' / '.credentials.json'
        self._write_creds(creds)
        self.assertEqual(
            self.healer.scan_claude_cli(creds), {'CLAUDE_MAX_OAUTH'},
        )

    def test_tier2_path_returns_tier2_name(self):
        creds = self.tmp_path / 'claude-larry-personal' / '.claude' / '.credentials.json'
        self._write_creds(creds)
        self.assertEqual(
            self.healer.scan_claude_cli(creds),
            {'LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2'},
        )

    def test_missing_file_returns_empty_set(self):
        creds = self.tmp_path / 'claude-larry-personal' / '.claude' / '.credentials.json'
        self.assertEqual(self.healer.scan_claude_cli(creds), set())

    def test_malformed_file_returns_empty_set(self):
        # A malformed Tier 2 file is conflated with 'missing' — same DM action.
        creds = self.tmp_path / 'claude-larry-personal' / '.claude' / '.credentials.json'
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text('not json')
        self.assertEqual(self.healer.scan_claude_cli(creds), set())

    def test_empty_access_token_returns_empty_set(self):
        creds = self.tmp_path / 'claude-larry-personal' / '.claude' / '.credentials.json'
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({'claudeAiOauth': {'accessToken': ''}}))
        self.assertEqual(self.healer.scan_claude_cli(creds), set())


# ---- heal_pipeline_stall Check 8 -----------------------------------------

class HealPipelineStallCheck8Test(unittest.TestCase):
    """Check 8 scans per-agent logs for TIER2_FALLBACK_* markers and
    produces one alert per (agent, outcome, reason) signature in the
    window."""

    def setUp(self):
        self.healer = importlib.import_module('heal_pipeline_stall')
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.logs_dir = self.tmp_path / 'logs'
        self.logs_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_root(self):
        return mock.patch.object(self.healer, 'AGENTS_ROOT', self.tmp_path)

    def _make_log(self, name: str, body: str):
        (self.logs_dir / name).write_text(body)

    def test_no_logs_no_alerts(self):
        with self._patch_root():
            alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(alerts, [])

    def test_unavailable_marker_in_window_alerts(self):
        # Use a current-day timestamp so it's inside the lookback window
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self._make_log('forge.log',
            f'[{ts}] [forge] [WARN] TIER2_FALLBACK_UNAVAILABLE '
            f'reason=auth_401 home=/home/larry/.claude-larry-personal '
            f'(missing credentials file)\n')
        with self._patch_root():
            alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('unavailable', alerts[0]['message'].lower())
        self.assertIn('auth_401', alerts[0]['subject'])
        self.assertIn('forge', alerts[0]['subject'])

    def test_failed_marker_alerts(self):
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self._make_log('mirror.log',
            f'[{ts}] [mirror] [WARN] TIER2_FALLBACK_FAILED '
            f'reason=rate_limit exit=1\n')
        with self._patch_root():
            alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)
        self.assertIn('rate_limit', alerts[0]['subject'])

    def test_skipped_marker_is_log_only(self):
        # SKIPPED is expected + auto-remediated (heal_resume_paused_on_tier1
        # re-dispatches), so it is log-only: zero alerts, no DM. The cursor
        # still advances so a second scan doesn't re-process the line.
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self._make_log('forge.log',
            f'[{ts}] [forge] [WARN] TIER2_FALLBACK_SKIPPED '
            f'reason=auth_401 cause=resume_session_account_bound\n')
        cursor_file = self.tmp_path / 'state' / 'check-8-cursor.json'
        with self._patch_root(), \
                mock.patch.object(self.healer, 'CHECK8_CURSOR_FILE', cursor_file):
            alerts = self.healer.check_tier2_fallback_failures({})
            cursor = self.healer.load_check8_cursor()
        self.assertEqual(alerts, [])
        self.assertIn('forge:SKIPPED:auth_401', cursor)

    def test_old_marker_outside_window_no_alert(self):
        # 2 days ago — well outside the 24h lookback
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self._make_log('forge.log',
            f'[{old_ts}] [forge] [WARN] TIER2_FALLBACK_FAILED '
            f'reason=rate_limit exit=1\n')
        with self._patch_root():
            alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(alerts, [])

    def test_dedup_within_agent_outcome_reason(self):
        # Two markers in the same log with identical (outcome, reason)
        # → one alert
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self._make_log('forge.log',
            f'[{ts}] [forge] [WARN] TIER2_FALLBACK_FAILED reason=rate_limit\n'
            f'[{ts}] [forge] [WARN] TIER2_FALLBACK_FAILED reason=rate_limit\n')
        with self._patch_root():
            alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)

    def test_different_reasons_different_alerts(self):
        # Same agent + outcome, different reason → two alerts
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        self._make_log('forge.log',
            f'[{ts}] [forge] [WARN] TIER2_FALLBACK_FAILED reason=rate_limit\n'
            f'[{ts}] [forge] [WARN] TIER2_FALLBACK_FAILED reason=auth_401\n')
        with self._patch_root():
            alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 2)


if __name__ == '__main__':
    unittest.main()
