#!/usr/bin/env python3
"""Tests for the claude-quota-fixes-v2 bundle (2026-05-27).

Covers four production-path changes:

  1. scripts/beacon_telegram_bot.py — refuse-on-resume guard + t2.stdout
     return (Tier 2 failure body distinct from Tier 1's) + structural
     alignment with agent_runner.py:822-828.
  2. scripts/heal_pipeline_stall.py — Check 8 cursor (advance, never-
     backward, safe-resume on corruption).
  3. scripts/heal_claude_max_burn_rate.py — threshold gating + window
     dedup + LLM-purity assertion.
  4. scripts/heal_tier2_weekly_health_probe.py — success silence +
     three failure-path DM variants.

Test-isolation discipline (PR #137): every test redirects module-level
state paths to a per-test temp directory via monkeypatched constants.
NO writes to `~/agents/` or any other shared filesystem path. All
subprocess invocations are mocked; the real `claude` binary is never
invoked. The burn-rate purity test explicitly grep-asserts no LLM
subprocess calls happen in the healer's execution path.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_beacon_tier2_fallback
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


# ---------------------------------------------------------------------------
# (1) beacon_telegram_bot.py — refuse-on-resume + t2.stdout fixes
# ---------------------------------------------------------------------------


class _TempDirBase(unittest.TestCase):
    """Shared setUp/tearDown for tests that need a per-test temp dir +
    monkeypatched module constants. Subclasses set `self.tmp` to the path."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)


class BeaconBotRefuseOnResumeTest(_TempDirBase):
    """Mock call_beacon with `--resume <session_id>` arg + Tier 1 returning
    rate-limit. Assert NO Tier 2 subprocess invocation; assert DM fires
    via _tier2_refuse_on_resume_dm with the session-bound message."""

    def setUp(self):
        super().setUp()
        self.bot = importlib.import_module('beacon_telegram_bot')
        # Step C quota-ledger writes resolve OURLIBERTY_AGENTS_ROOT at CALL
        # time — class-level patch so no future call_beacon test can write
        # the real anthropic-quota-events.jsonl; bound so tests can assert
        # the emission still happens.
        _p = mock.patch.object(self.bot, '_append_bot_quota_event')
        self.quota_mock = _p.start()
        self.addCleanup(_p.stop)

    def test_resume_plus_rate_limit_refuses_tier2(self):
        # Tier 1 returns rate-limit phrasing on stdout, non-zero exit
        tier1 = mock.Mock(returncode=1, stdout="You've hit your limit · resets 11:30am",
                          stderr='')
        # If Tier 2 were attempted, it'd be a SECOND _run_claude_once call.
        # We track call count to assert Tier 2 was NOT invoked.
        run_mock = mock.Mock(return_value=tier1)
        dm_mock = mock.Mock()
        with mock.patch.object(self.bot, '_run_claude_once', run_mock), \
             mock.patch.object(self.bot, '_tier2_refuse_on_resume_dm', dm_mock), \
             mock.patch.object(self.bot, '_tier2_failure_dm') as failure_dm_mock, \
             mock.patch.object(self.bot, 'tier2_available', return_value=True):
            reply, sess = self.bot.call_beacon('hello', 'sess-abc')
        # Step C: the tier-1 failure must still land a quota-ledger event —
        # this is the only test coverage of the bot's emission call sites.
        self.quota_mock.assert_called()
        # Tier 2 must NOT have been invoked — only one _run_claude_once call
        self.assertEqual(run_mock.call_count, 1,
                         'Tier 2 subprocess must not be invoked on --resume failure')
        # Refuse-on-resume DM fires; the generic failure DM does NOT
        dm_mock.assert_called_once()
        failure_dm_mock.assert_not_called()
        # Reply body mentions refused / session-bound
        self.assertIn('Tier 2 retry refused', reply)
        self.assertEqual(sess, 'sess-abc')

    def test_no_resume_falls_through_to_tier2_attempt(self):
        # Same Tier 1 failure but NO session_id → should attempt Tier 2
        tier1 = mock.Mock(returncode=1, stdout='401 Unauthorized', stderr='')
        tier2 = mock.Mock(returncode=0,
                          stdout=json.dumps({'result': 'ok', 'session_id': 's2'}),
                          stderr='')
        run_mock = mock.Mock(side_effect=[tier1, tier2])
        with mock.patch.object(self.bot, '_run_claude_once', run_mock), \
             mock.patch.object(self.bot, 'tier2_available', return_value=True), \
             mock.patch.object(self.bot, '_tier2_refuse_on_resume_dm') as refuse_dm:
            reply, sess = self.bot.call_beacon('hello', None)
        # Tier 2 WAS attempted (two calls); refuse-on-resume DM did NOT fire
        self.assertEqual(run_mock.call_count, 2,
                         'Tier 2 must be attempted when no --resume is set')
        refuse_dm.assert_not_called()
        self.assertEqual(reply, 'ok')
        self.assertEqual(sess, 's2')


class BeaconBotPrimaryUsesTier1SetupTokenTest(_TempDirBase):
    """The primary call_beacon attempt must authenticate via Tier 1's
    setup-token (the account HOME=/home/larry is bound to), NOT HOME's
    auto-refreshing credentials.json. Regression guard for the 2026-06-25
    dead-default-token incident: the primary 401'd on every message and a
    --resume session could not recover (Tier 2 fallback refused as
    session-bound), leaving Beacon dead to Larry."""

    def setUp(self):
        super().setUp()
        self.bot = importlib.import_module('beacon_telegram_bot')
        _p = mock.patch.object(self.bot, '_append_bot_quota_event')
        self.quota_mock = _p.start()
        self.addCleanup(_p.stop)

    def test_primary_attempt_passes_tier1_setup_token(self):
        ok = mock.Mock(
            returncode=0,
            stdout=json.dumps({'result': 'hi', 'session_id': 'sess-x'}),
            stderr='',
        )
        run_mock = mock.Mock(return_value=ok)
        with mock.patch.object(self.bot, '_run_claude_once', run_mock), \
             mock.patch.object(self.bot.active_tier, '_setup_token_for_tier',
                               return_value='tier1-tok') as tok_mock:
            reply, sess = self.bot.call_beacon('hello', 'sess-x')
        # One successful primary call — no fallback needed.
        self.assertEqual(run_mock.call_count, 1)
        # The primary attempt authenticated via the Tier 1 setup-token, so a
        # rotted credentials.json no longer 401s the heartbeat.
        self.assertEqual(
            run_mock.call_args_list[0].kwargs.get('oauth_token'), 'tier1-tok')
        tok_mock.assert_any_call('tier1')
        # --resume continuity preserved (HOME unchanged, same-account token).
        self.assertEqual(reply, 'hi')
        self.assertEqual(sess, 'sess-x')


class BeaconBotHonorsTierPinTest(_TempDirBase):
    """Regression guard for the 2026-06-29 incident: with the team pinned to
    Tier 2 (active_tier.read()['tier'] == 'tier2'), the PRIMARY call_beacon
    attempt must route to Tier 2 (HOME = active_tier.current_home() =
    TIER2_HOME, token = the Tier 2 setup-token) — NOT hardwired Tier 1. Before
    the fix the bot always started on Tier 1 and silently drained it even while
    the rest of the team was on Tier 2."""

    def setUp(self):
        super().setUp()
        self.bot = importlib.import_module('beacon_telegram_bot')
        _p = mock.patch.object(self.bot, '_append_bot_quota_event')
        self.quota_mock = _p.start()
        self.addCleanup(_p.stop)

    def test_primary_routes_to_pinned_tier2(self):
        ok = mock.Mock(
            returncode=0,
            stdout=json.dumps({'result': 'pong', 'session_id': 's2'}),
            stderr='',
        )
        run_mock = mock.Mock(return_value=ok)
        tok_map = {'tier1': 'tier1-tok', 'tier2': 'tier2-tok'}
        with mock.patch.object(self.bot.active_tier, 'read',
                               return_value={'tier': 'tier2'}), \
             mock.patch.object(self.bot.active_tier, '_setup_token_for_tier',
                               side_effect=lambda t: tok_map[t]) as tok_mock, \
             mock.patch.object(self.bot, '_run_claude_once', run_mock):
            reply, sess = self.bot.call_beacon('ping', 'sess-prev')
        # Single primary call — no fallback.
        self.assertEqual(run_mock.call_count, 1)
        kwargs = run_mock.call_args_list[0].kwargs
        # Primary HOME swapped to the PINNED tier (Tier 2), token is Tier 2's.
        self.assertEqual(kwargs.get('home_override'), self.bot.active_tier.TIER2_HOME)
        self.assertEqual(kwargs.get('oauth_token'), 'tier2-tok')
        # The active tier's token was requested (not hardwired 'tier1').
        tok_mock.assert_any_call('tier2')
        self.assertEqual(reply, 'pong')
        self.assertEqual(sess, 's2')


class BeaconBotStaleCrossTierSessionSelfHealsTest(_TempDirBase):
    """A session created on the old tier and resumed under the now-pinned
    tier's HOME errors with 'No conversation found with session ID' — emitted
    on STDOUT, not stderr. The retry-without-resume guard must read the
    COMBINED streams so this self-heals into a fresh session instead of
    dead-ending the chat (the 2026-06-29 stuck-session symptom)."""

    def setUp(self):
        super().setUp()
        self.bot = importlib.import_module('beacon_telegram_bot')
        _p = mock.patch.object(self.bot, '_append_bot_quota_event')
        self.quota_mock = _p.start()
        self.addCleanup(_p.stop)

    def test_stdout_session_error_retries_without_resume(self):
        # Stale-session error on STDOUT (not a rate-limit/auth-401), nonzero.
        stale = mock.Mock(
            returncode=1,
            stdout='No conversation found with session ID: 1b5ed242',
            stderr='',
        )
        fresh = mock.Mock(
            returncode=0,
            stdout=json.dumps({'result': 'recovered', 'session_id': 'new-sess'}),
            stderr='',
        )
        run_mock = mock.Mock(side_effect=[stale, fresh])
        with mock.patch.object(self.bot, '_run_claude_once', run_mock):
            reply, sess = self.bot.call_beacon('hi', 'stale-sess')
        # Two calls: the stale --resume attempt, then a fresh retry.
        self.assertEqual(run_mock.call_count, 2)
        first_cmd = run_mock.call_args_list[0].args[0]
        second_cmd = run_mock.call_args_list[1].args[0]
        self.assertIn('--resume', first_cmd)
        self.assertNotIn('--resume', second_cmd)
        self.assertEqual(reply, 'recovered')
        self.assertEqual(sess, 'new-sess')


class BeaconBotT2StdoutReturnedTest(_TempDirBase):
    """Verify the t2.stdout fix at the former line 338. Mock Tier 1 and
    Tier 2 with DISTINCT stdouts; assert the returned body contains Tier
    2's stdout, NOT Tier 1's. Before this fix the body was result.stdout
    (Tier 1's), masking the real Tier 2 failure mode."""

    def setUp(self):
        super().setUp()
        self.bot = importlib.import_module('beacon_telegram_bot')
        # See BeaconBotRefuseOnResumeTest.setUp — same class-level barrier.
        _p = mock.patch.object(self.bot, '_append_bot_quota_event')
        self.quota_mock = _p.start()
        self.addCleanup(_p.stop)

    def test_tier2_failure_body_echoes_tier2_stdout(self):
        # Tier 1 phrase must actually trip classify_tier1_failure — use the
        # canonical 'hit your limit' phrasing from the 2026-05-26 incident.
        tier1 = mock.Mock(
            returncode=1,
            stdout="You've hit your limit · TIER_ONE_MARKER",
            stderr='',
        )
        tier2 = mock.Mock(returncode=1,
                          stdout='auth_401: provider not enabled (TIER 2 distinct)',
                          stderr='tier2 stderr distinct token')
        run_mock = mock.Mock(side_effect=[tier1, tier2])
        with mock.patch.object(self.bot, '_run_claude_once', run_mock), \
             mock.patch.object(self.bot, 'tier2_available', return_value=True), \
             mock.patch.object(self.bot, '_tier2_failure_dm') as dm_mock:
            reply, _ = self.bot.call_beacon('hello', None)
        self.quota_mock.assert_called()
        # The chat reply body must contain Tier 2's stdout, NOT Tier 1's
        self.assertIn('TIER 2 distinct', reply)
        self.assertNotIn('TIER_ONE_MARKER', reply)
        # _tier2_failure_dm must be called with the Tier 2 stdout/stderr so
        # Larry can distinguish failure modes from a single DM
        dm_mock.assert_called_once()
        call_args = dm_mock.call_args
        # Positional args: (failure_type, tier2_stdout, tier2_stderr)
        self.assertEqual(len(call_args.args), 3)
        self.assertIn('TIER 2 distinct', call_args.args[1])
        self.assertIn('tier2 stderr distinct', call_args.args[2])


class BeaconBotPatternMatchTest(_TempDirBase):
    """Structural assertion: the bot's refuse-on-resume code mirrors
    agent_runner.py:822-828 — same `if session_id` guard, DM call, early
    return. We verify by string-presence of the marker phrases in BOTH
    files. A literal AST-comparison is overkill; this catches drift."""

    def test_both_sites_use_session_bound_terminology(self):
        ar_src = (Path(__file__).resolve().parent.parent
                  / 'agent_runner.py').read_text()
        bot_src = (Path(__file__).resolve().parent.parent
                   / 'beacon_telegram_bot.py').read_text()
        # Both must reference the account-bound rationale
        for src, name in ((ar_src, 'agent_runner.py'),
                          (bot_src, 'beacon_telegram_bot.py')):
            self.assertIn('TIER2_FALLBACK_SKIPPED', src,
                          f'{name} missing TIER2_FALLBACK_SKIPPED log marker')
            self.assertIn('resume_session_account_bound', src,
                          f'{name} missing resume_session_account_bound cause')

    def test_bot_docstring_documents_refuse_on_resume(self):
        bot = importlib.import_module('beacon_telegram_bot')
        doc = bot.call_beacon.__doc__ or ''
        # The new docstring must explicitly state that --resume sessions
        # are refused, mirroring agent_runner.py's rationale
        self.assertIn('--resume', doc)
        self.assertIn('account', doc.lower())
        # Old behavior phrase must NOT be present
        self.assertNotIn('cold-start is acceptable', doc)


# ---------------------------------------------------------------------------
# (2) heal_pipeline_stall.py — Check 8 cursor
# ---------------------------------------------------------------------------


class Check8CursorTest(_TempDirBase):
    """Cursor advances correctly across simulated runs over a log file.
    Simulate a log with 3 entries with DISTINCT (agent, outcome, reason)
    signatures across 2 hours, then append a 4th entry; assert only the
    new entry alerts on the next run."""

    def setUp(self):
        super().setUp()
        # Build a temp AGENTS_ROOT structure
        (self.tmp / 'logs').mkdir()
        (self.tmp / 'state').mkdir()
        (self.tmp / 'blackboard').mkdir()
        self.healer = importlib.reload(
            importlib.import_module('heal_pipeline_stall'))
        # Redirect all AGENTS_ROOT-derived paths to the temp dir
        self.healer.AGENTS_ROOT = self.tmp
        self.healer.LOG_FILE = self.tmp / 'logs' / 'heal-pipeline-stall.log'
        self.healer.STATE_FILE = self.tmp / 'blackboard' / 'state.json'
        self.healer.CHECK8_CURSOR_FILE = (
            self.tmp / 'state' / 'check-8-cursor.json'
        )
        # Force the in-window check to accept old timestamps
        self.healer.SCAN_WINDOW_SECONDS = 86400 * 365  # 1 year, very generous

    def _write_log(self, log_path: Path, lines: list[str]) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')

    def test_first_run_alerts_all_signatures_then_second_run_silent(self):
        log_path = self.tmp / 'logs' / 'beacon_telegram_bot.log'
        # Three entries with DISTINCT signatures within scan window.
        # Use current-time-relative so SCAN_WINDOW filtering doesn't drop them.
        base = datetime.now(timezone.utc) - timedelta(minutes=30)
        entries = [
            (base + timedelta(minutes=0),
             'UNAVAILABLE', 'rate_limit'),
            (base + timedelta(minutes=10),
             'FAILED', 'rate_limit'),
            (base + timedelta(minutes=20),
             'SKIPPED', 'auth_401'),
        ]
        log_lines = []
        for ts, outcome, reason in entries:
            ts_str = ts.isoformat()
            log_lines.append(
                f'[{ts_str}] [WARN] TIER2_FALLBACK_{outcome} '
                f'reason={reason} extra=context'
            )
        self._write_log(log_path, log_lines)
        # First run — only the two ACTIONABLE signatures alert. SKIPPED is
        # by-design + auto-remediated, so it's log-only (no DM) as of the
        # tier2-skipped-suppression change.
        alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 2,
                         f'expected 2 actionable alerts on first run, got {len(alerts)}')
        subjects = sorted(a['subject'] for a in alerts)
        self.assertEqual(subjects, [
            'pipeline-stall:tier2-fallback-failed-rate_limit:beacon-bot',
            'pipeline-stall:tier2-fallback-unavailable-rate_limit:beacon-bot',
        ])
        # Cursor still contains all THREE keys — it advances before the
        # outcome branch, so SKIPPED is recorded even though it didn't alert.
        cursor = self.healer.load_check8_cursor()
        self.assertEqual(len(cursor), 3)
        # Second run on UNCHANGED log — all entries are at/below cursor; no alerts
        alerts2 = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts2), 0,
                         'second run on same log must produce no alerts')

    def test_new_entry_after_cursor_fires_alert(self):
        log_path = self.tmp / 'logs' / 'beacon_telegram_bot.log'
        base = datetime.now(timezone.utc) - timedelta(minutes=30)
        first_batch = [
            (base + timedelta(minutes=0),
             'UNAVAILABLE', 'rate_limit'),
        ]
        log_lines = [
            f'[{ts.isoformat()}] [WARN] TIER2_FALLBACK_{outcome} '
            f'reason={reason}'
            for ts, outcome, reason in first_batch
        ]
        self._write_log(log_path, log_lines)
        # First run — one alert, cursor advances
        self.healer.check_tier2_fallback_failures({})
        # Append a 4th-style entry: same signature, NEWER ts
        new_ts = base + timedelta(minutes=25)
        with open(log_path, 'a') as f:
            f.write(f'[{new_ts.isoformat()}] [WARN] '
                    f'TIER2_FALLBACK_UNAVAILABLE reason=rate_limit\n')
        # Second run — must see the new entry and alert
        alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1,
                         'new entry past cursor must produce a fresh alert')


class Check8CursorCorruptionTest(_TempDirBase):
    """Simulate a corrupted cursor file at boot. Assert: WARN logged,
    cursors treated as empty (so all in-window log lines re-process),
    AND the cursor file is freshly written with valid JSON post-run."""

    def setUp(self):
        super().setUp()
        (self.tmp / 'logs').mkdir()
        (self.tmp / 'state').mkdir()
        (self.tmp / 'blackboard').mkdir()
        self.healer = importlib.reload(
            importlib.import_module('heal_pipeline_stall'))
        self.healer.AGENTS_ROOT = self.tmp
        self.healer.LOG_FILE = self.tmp / 'logs' / 'heal-pipeline-stall.log'
        self.healer.STATE_FILE = self.tmp / 'blackboard' / 'state.json'
        self.healer.CHECK8_CURSOR_FILE = (
            self.tmp / 'state' / 'check-8-cursor.json'
        )
        self.healer.SCAN_WINDOW_SECONDS = 86400 * 365

    def test_corrupt_cursor_does_not_crash_resets_to_empty(self):
        # Write garbage to the cursor file
        self.healer.CHECK8_CURSOR_FILE.write_text('this is not json {{{')
        # load_check8_cursor must return {} and rewrite the file
        cursor = self.healer.load_check8_cursor()
        self.assertEqual(cursor, {})
        # The file is now valid JSON (freshly written)
        post = json.loads(self.healer.CHECK8_CURSOR_FILE.read_text())
        self.assertEqual(post, {'version': 1, 'cursors': {}})

    def test_corrupt_cursor_full_run_does_not_crash_and_advances(self):
        # Pre-corrupt the cursor, write a log line, then run the full check
        self.healer.CHECK8_CURSOR_FILE.write_text('}}garbage{{')
        log_path = self.tmp / 'logs' / 'beacon_telegram_bot.log'
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        log_path.write_text(
            f'[{ts}] [WARN] TIER2_FALLBACK_UNAVAILABLE reason=rate_limit\n'
        )
        # Must not raise
        alerts = self.healer.check_tier2_fallback_failures({})
        self.assertEqual(len(alerts), 1)
        # Cursor file now has the new ts (post-run write)
        post = json.loads(self.healer.CHECK8_CURSOR_FILE.read_text())
        self.assertEqual(post.get('version'), 1)
        self.assertGreaterEqual(len(post.get('cursors', {})), 1)


# ---------------------------------------------------------------------------
# (3) heal_claude_max_burn_rate.py — threshold + window dedup + purity
# ---------------------------------------------------------------------------


class BurnRateMonitorTest(_TempDirBase):
    """79% → no DM. 81% → one DM. 81% on next tick → no second DM.

    Re-based 2026-05-28 onto the token-volume signal — token gate at 10M
    quota-consuming tokens (input+output+cache_creation), 80% trigger at
    8M, replacing the prior $60 dollar gate."""

    def setUp(self):
        super().setUp()
        (self.tmp / 'logs').mkdir()
        (self.tmp / 'state').mkdir()
        (self.tmp / 'blackboard').mkdir()
        self.healer = importlib.reload(
            importlib.import_module('heal_claude_max_burn_rate'))
        self.healer.AGENTS_ROOT = self.tmp
        self.healer.LOG_FILE = self.tmp / 'logs' / 'burn.log'
        self.healer.HEARTBEAT_FILE = self.tmp / 'blackboard' / 'burn.heartbeat'
        self.healer.STATE_FILE = self.tmp / 'state' / 'burn.json'
        self.healer.COSTS_FILE = self.tmp / 'blackboard' / 'costs.jsonl'
        self.healer.KILL_SWITCH = self.tmp / 'healers.disabled'

    def _write_costs(self, total_tokens: int):
        # Single entry inside the 5h window summing to total_tokens of
        # quota-consuming usage. Split across input/output/cache_creation
        # so the record mirrors real costs.jsonl shape.
        ts = datetime.now(timezone.utc).isoformat()
        rec = {
            'ts': ts, 'agent': 'forge', 'model': 'claude-opus-4-7',
            'input_tokens': total_tokens // 3,
            'output_tokens': total_tokens // 3,
            'cache_creation': total_tokens - 2 * (total_tokens // 3),
            'cache_read': 999_999_999,  # excluded — must not affect signal
            'cost_usd': 0.0,
        }
        self.healer.COSTS_FILE.write_text(json.dumps(rec) + '\n')

    def test_below_threshold_no_dm(self):
        # Threshold 10M * 0.80 = 8M. 7.8M is 78% → no DM.
        self._write_costs(7_800_000)
        with mock.patch.object(self.healer, 'gate_enabled', return_value=True), \
             mock.patch.object(self.healer, 'load_threshold',
                               return_value=10_000_000), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert') as dm:
            rc = self.healer.run()
        self.assertEqual(rc, 0)
        dm.assert_not_called()

    def test_at_threshold_fires_dm_once(self):
        # 8.3M / 10M = 83% → fires
        self._write_costs(8_300_000)
        with mock.patch.object(self.healer, 'gate_enabled', return_value=True), \
             mock.patch.object(self.healer, 'load_threshold',
                               return_value=10_000_000), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm:
            self.healer.run()
        self.assertEqual(dm.call_count, 1)

    def test_dm_window_dedup_on_next_tick(self):
        # First tick: fires
        self._write_costs(8_300_000)
        with mock.patch.object(self.healer, 'gate_enabled', return_value=True), \
             mock.patch.object(self.healer, 'load_threshold',
                               return_value=10_000_000), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm1:
            self.healer.run()
        self.assertEqual(dm1.call_count, 1)
        # Second tick — same conditions, still high — must NOT fire again
        with mock.patch.object(self.healer, 'gate_enabled', return_value=True), \
             mock.patch.object(self.healer, 'load_threshold',
                               return_value=10_000_000), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm2:
            self.healer.run()
        dm2.assert_not_called()

    def test_load_threshold_fallback_on_missing_config(self):
        # Point _CONFIG_FILE at a nonexistent path
        with mock.patch.object(self.healer, '_CONFIG_FILE',
                               self.tmp / 'does-not-exist.json'):
            thresh = self.healer.load_threshold()
        self.assertEqual(thresh,
                         self.healer.DEFAULT_MAX_5H_TOKEN_THRESHOLD)

    def test_load_threshold_reads_config(self):
        cfg = self.tmp / 'config.json'
        cfg.write_text(json.dumps({
            'tier1_quota': {'max_5h_token_threshold': 12_500_000}
        }))
        with mock.patch.object(self.healer, '_CONFIG_FILE', cfg):
            thresh = self.healer.load_threshold()
        self.assertEqual(thresh, 12_500_000)


class BurnRateMonitorPurityTest(unittest.TestCase):
    """Assert the burn-rate healer makes no LLM subprocess calls. A quota
    healer that consumes quota would defeat itself. Verified by source
    grep — the module must not import subprocess OR invoke a `claude`
    command at any point in its execution path."""

    def test_no_subprocess_or_claude_invocations(self):
        src_path = (Path(__file__).resolve().parent.parent
                    / 'heal_claude_max_burn_rate.py')
        src = src_path.read_text()
        # Defensive: the literal 'subprocess' module would mean a process
        # invocation site. We forbid it module-wide.
        self.assertNotIn('import subprocess', src,
                         'burn-rate healer must not import subprocess')
        # Forbid any literal claude binary invocation (CLAUDE_BIN /
        # 'claude' as a command arg). Allow the string `claude` to
        # appear in comments / docstrings only — we filter by checking
        # the lines without a leading '#' or inside docstrings.
        non_doc_lines = [
            line for line in src.splitlines()
            if 'claude' in line.lower()
            and not line.lstrip().startswith('#')
            and 'subprocess' not in line
            and 'CLAUDE_BIN' not in line  # not present anyway, defensive
        ]
        # Lines that mention 'claude' should all be in strings/messages,
        # not as live invocations. We don't try to AST-parse — the
        # absence of `import subprocess` above is the load-bearing check.
        # This second check is informational; we just assert no `claude(`
        # call-form is present.
        for line in non_doc_lines:
            self.assertNotIn('claude(', line.lower(),
                             f'unexpected claude invocation: {line!r}')


# ---------------------------------------------------------------------------
# (4) heal_tier2_weekly_health_probe.py — success silence + failure DMs
# ---------------------------------------------------------------------------


class WeeklyProbeTest(_TempDirBase):

    def setUp(self):
        super().setUp()
        (self.tmp / 'logs').mkdir()
        (self.tmp / 'state').mkdir()
        (self.tmp / 'blackboard').mkdir()
        self.healer = importlib.reload(
            importlib.import_module('heal_tier2_weekly_health_probe'))
        self.healer.AGENTS_ROOT = self.tmp
        self.healer.LOG_FILE = self.tmp / 'logs' / 'probe.log'
        self.healer.HEARTBEAT_FILE = self.tmp / 'blackboard' / 'probe.heartbeat'
        self.healer.STATE_FILE = self.tmp / 'state' / 'probe.json'
        self.healer.KILL_SWITCH = self.tmp / 'healers.disabled'
        # Isolate the OAuth-probe DM assertions from the parity check that
        # run() now performs unconditionally: point TIER2_HOME / REPO_SYSTEMD /
        # TIER1_HOME at parity-clean tmp fixtures so check_provisioning_parity
        # finds no drift and never fires its own append_alert. Mirrors
        # test_tier2_provisioning_parity.py's setUp.
        t1 = self.tmp / 'tier1'
        t2 = self.tmp / 'tier2'
        for h in (t1, t2):
            (h / '.claude').mkdir(parents=True)
            (h / '.claude.json').write_text(
                json.dumps({'mcpServers': {'workspace-mcp': {}}}))
        (t2 / '.claude' / '.credentials.json').write_text('{}')
        sysd = self.tmp / 'systemd'
        sysd.mkdir()
        for unit in self.healer.SESSION_UNITS:
            (sysd / unit).write_text(
                f'[Service]\nReadWritePaths=/home/larry/agents {t2}\n')
        self.healer.TIER2_HOME = str(t2)
        self.healer.REPO_SYSTEMD = sysd
        _saved_t1 = self.healer.active_tier.TIER1_HOME
        self.healer.active_tier.TIER1_HOME = str(t1)
        self.addCleanup(
            setattr, self.healer.active_tier, 'TIER1_HOME', _saved_t1)

    def _mock_subprocess(self, stdout: str, stderr: str = '',
                         returncode: int = 0):
        proc = mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)
        return mock.patch.object(
            self.healer.subprocess, 'run', return_value=proc,
        )

    def test_success_silent_no_dm(self):
        # JSON output with PROBE_OK in `result`
        stdout = json.dumps({'result': 'PROBE_OK', 'is_error': False,
                             'session_id': 's1'})
        with self._mock_subprocess(stdout), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert') as dm:
            rc = self.healer.run()
        self.assertEqual(rc, 0)
        dm.assert_not_called()

    def test_wrong_output_fires_dm(self):
        stdout = json.dumps({'result': 'something else entirely',
                             'is_error': False})
        with self._mock_subprocess(stdout), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm:
            self.healer.run()
        dm.assert_called_once()
        kwargs = dm.call_args.kwargs
        self.assertEqual(kwargs.get('subject'),
                         'tier2_weekly_probe_failed')
        self.assertEqual(kwargs.get('severity'), 'warning')

    def test_is_error_true_fires_dm(self):
        stdout = json.dumps({'result': 'PROBE_OK', 'is_error': True})
        with self._mock_subprocess(stdout), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm:
            self.healer.run()
        dm.assert_called_once()

    def test_nonzero_exit_fires_dm(self):
        with self._mock_subprocess('', stderr='auth failed', returncode=1), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm:
            self.healer.run()
        dm.assert_called_once()

    def test_subsequent_failure_within_cooldown_silent(self):
        with self._mock_subprocess('garbage'), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm1:
            self.healer.run()
        self.assertEqual(dm1.call_count, 1)
        # Re-run immediately — cooldown suppresses second DM
        with self._mock_subprocess('garbage'), \
             mock.patch.object(self.healer.larry_alerts, 'append_alert',
                               return_value=True) as dm2:
            self.healer.run()
        dm2.assert_not_called()

    def test_load_haiku_model_id_default(self):
        with mock.patch.object(self.healer, '_CONFIG_FILE',
                               self.tmp / 'nope.json'):
            mid = self.healer.load_haiku_model_id()
        self.assertEqual(mid, self.healer.DEFAULT_HAIKU_MODEL_ID)

    def test_load_haiku_model_id_from_config(self):
        cfg = self.tmp / 'cfg.json'
        cfg.write_text(json.dumps({
            'models': {'haiku': {'model_id': 'claude-haiku-test-id'}}
        }))
        with mock.patch.object(self.healer, '_CONFIG_FILE', cfg):
            mid = self.healer.load_haiku_model_id()
        self.assertEqual(mid, 'claude-haiku-test-id')


if __name__ == '__main__':
    unittest.main()
