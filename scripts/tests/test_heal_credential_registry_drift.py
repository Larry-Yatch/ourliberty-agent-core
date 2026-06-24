#!/usr/bin/env python3
"""Tests for heal_credential_registry_drift (E1.5.2).

Covers the pure-logic surface (scanners, drift detection, state-file dedup,
DM rendering) plus orchestration with kill-switch + dry-run + activation
flow. Subprocess invocations are stubbed; no live gh / file-system writes
under the user's home dir.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_credential_registry_drift
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_credential_registry_drift as h  # noqa: E402

_PREV_CREDENTIALS_ENV_FILE = None


def setUpModule():
    # Isolate the durable-token file fallback. active_tier._setup_token_for_tier
    # now reads ~/credentials/.env.larry when the env var is unset; point it at
    # a nonexistent path so this module's no-token suppression and distinctness
    # cases stay reachable on the droplet (where the real file exists). Tests
    # that need a token set the env var (overrides) or patch the function.
    global _PREV_CREDENTIALS_ENV_FILE
    _PREV_CREDENTIALS_ENV_FILE = os.environ.get('OURLIBERTY_CREDENTIALS_ENV_FILE')
    os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = (
        '/nonexistent-ourliberty-test/.env.larry')


def tearDownModule():
    if _PREV_CREDENTIALS_ENV_FILE is None:
        os.environ.pop('OURLIBERTY_CREDENTIALS_ENV_FILE', None)
    else:
        os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = _PREV_CREDENTIALS_ENV_FILE


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test.

    Why: heal_credential_registry_drift's LOG_FILE / STATE_FILE /
    HEARTBEAT_FILE / KILL_SWITCH derive from AGENTS_ROOT at import time.
    Without this redirection, running tests in a worktree pollutes prod
    `/home/larry/agents/...` state. Reload the module so its module-level
    constants pick up the override.

    Subclasses that exercise `_run_claude_auth_status` directly (rather than
    going through `check_tier_distinctness`) set `_stub_tier_probe = False`
    to disable the default stub.
    """

    _stub_tier_probe = True

    def setUp(self):
        super().setUp()
        self._isolated_tmp = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
            os.makedirs(os.path.join(self._isolated_tmp, sub), exist_ok=True)
        self._isolated_env_orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_tmp
        importlib.reload(h)
        # Stub the tier-distinctness probe to return distinct identities by
        # default so the existing credential-drift tests don't shell out to
        # the real `claude` binary. Tests focused on the tier check
        # override this via mock.patch within the test method.
        if self._stub_tier_probe:
            self._tier_probe_patch = mock.patch.object(
                h, '_run_claude_auth_status',
                side_effect=lambda home: ('ok', f'distinct-{home}'),
            )
            self._tier_probe_patch.start()
            self.addCleanup(self._tier_probe_patch.stop)

    def tearDown(self):
        if self._isolated_env_orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._isolated_env_orig
        importlib.reload(h)
        shutil.rmtree(self._isolated_tmp, ignore_errors=True)
        super().tearDown()


def _registry(entries=None):
    return {
        '$schema_version': 1,
        'credentials': entries or [],
        'known_storage_locations': {
            'env_file:/home/larry/credentials/.env.larry': {
                'description': 'primary env file',
                'scanner_strategy': 'parse KEY=value lines',
            },
            'gh_cli:/home/larry/.config/gh/hosts.yml': {
                'description': 'gh CLI keychain',
                'scanner_strategy': 'gh auth status',
            },
            'claude_cli:/home/larry/.claude/.credentials.json': {
                'description': 'Claude OAuth',
                'scanner_strategy': 'JSON parse',
            },
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': {
                'description': 'workspace-mcp OAuth refresh tokens',
                'scanner_strategy': 'ls credentials/',
            },
        },
    }


def _cred(name, location='env_file:/home/larry/credentials/.env.larry', **overrides):
    base = {
        'name': name,
        'storage_location': location,
        'credential_type': 'api_token',
        'purpose': 'test',
        'rotation_type': 'scheduled',
        'cadence_days': 365,
        'created_at': '2026-05-19',
        'last_rotated_at': '2026-05-19',
        'next_rotation_due': '2027-05-19',
        'calendar_event_url': None,
        'runbook_path': 'docs/runbooks/rotate-vercel-token.md',
        'severity_if_lapsed': 'high',
        'owner_role': 'larry',
        'scopes': ['scope'],
        'notes': 'test',
    }
    base.update(overrides)
    return base


class ScanEnvFileTest(_IsolatedAgentsRoot):
    def test_returns_keys_with_nonempty_values(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('FOO=bar\nEMPTY=\nQUOTED="hello"\n')
            self.assertEqual(h.scan_env_file(p), {'FOO', 'QUOTED'})

    def test_skips_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text('# comment\n\nFOO=bar\n  \n')
            self.assertEqual(h.scan_env_file(p), {'FOO'})

    def test_missing_file_returns_empty_set(self):
        self.assertEqual(
            h.scan_env_file(Path('/tmp/does-not-exist-xyzzy')), set(),
        )

    def test_boolean_flag_values_are_skipped(self):
        # A boolean on/off tunable is not a credential — it must never be
        # returned, so it can't trip MISSING_REGISTRY_ENTRY (the durable
        # class-fix for the *_ENABLED feature-flag false-positive treadmill).
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text(
                'OURLIBERTY_BOARD_DRAIN_ENABLED=true\n'
                'OURLIBERTY_NEWMISSION_INGEST_ENABLED=1\n'
                'FLAG_OFF=false\n'
                'FLAG_NO=no\n'
                'FLAG_ON="on"\n'
                'FLAG_CAPS=TRUE\n'
                'REAL_TOKEN=ghp_abcdef123\n'
            )
            self.assertEqual(h.scan_env_file(p), {'REAL_TOKEN'})

    def test_credential_value_resembling_bool_prefix_still_returned(self):
        # Guard the skip stays narrow: only an EXACT boolean literal is dropped.
        # A real secret that merely starts with "true"/"on" is still a credential.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '.env'
            p.write_text(
                'TRUE_SECRET=truextra\n'   # not exactly "true"
                'ONE_TOKEN=10\n'           # not exactly "1" or "0"
            )
            self.assertEqual(h.scan_env_file(p), {'TRUE_SECRET', 'ONE_TOKEN'})


class ScanClaudeCliTest(_IsolatedAgentsRoot):
    def test_detects_active_oauth(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'creds.json'
            p.write_text(json.dumps({
                'claudeAiOauth': {'accessToken': 'sk-ant-...'}
            }))
            self.assertEqual(h.scan_claude_cli(p), {'CLAUDE_MAX_OAUTH'})

    def test_empty_token_means_not_active(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'creds.json'
            p.write_text(json.dumps({'claudeAiOauth': {'accessToken': ''}}))
            self.assertEqual(h.scan_claude_cli(p), set())

    def test_missing_file_returns_empty_set(self):
        self.assertEqual(
            h.scan_claude_cli(Path('/tmp/nope-xyzzy.json')), set(),
        )


class ScanWorkspaceMcpTest(_IsolatedAgentsRoot):
    def test_returns_token_when_json_file_present(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'a@b.com.json').write_text('{}')
            self.assertEqual(
                h.scan_workspace_mcp(d), {'GOOGLE_OAUTH_REFRESH_TOKEN'},
            )

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(h.scan_workspace_mcp(Path(td)), set())

    def test_empty_json_file_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'a.json').write_text('')
            self.assertEqual(h.scan_workspace_mcp(Path(td)), set())


class DetectDriftTest(_IsolatedAgentsRoot):
    def test_no_drift_when_registry_and_scans_match(self):
        reg = _registry([
            _cred('FOO_TOKEN'),
            _cred('GITHUB_GH_OAUTH_TOKEN',
                  location='gh_cli:/home/larry/.config/gh/hosts.yml'),
        ])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {'FOO_TOKEN'},
            'gh_cli:/home/larry/.config/gh/hosts.yml': {'GITHUB_GH_OAUTH_TOKEN'},
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': set(),
        }
        drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        # Registry has no Claude or Google entry; scans report nothing —
        # so no drift in either direction.
        self.assertEqual(drifts, [])

    def test_missing_registry_entry_when_credential_in_env_but_not_registry(self):
        reg = _registry([_cred('FOO_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {
                'FOO_TOKEN', 'BAR_TOKEN',
            },
            'gh_cli:/home/larry/.config/gh/hosts.yml': set(),
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': set(),
        }
        drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertIn(('BAR_TOKEN', 'MISSING_REGISTRY_ENTRY'), kinds)

    def test_missing_credential_when_registry_entry_has_no_match(self):
        reg = _registry([_cred('FOO_TOKEN'), _cred('GHOST_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {'FOO_TOKEN'},
            'gh_cli:/home/larry/.config/gh/hosts.yml': set(),
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': set(),
        }
        drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertIn(('GHOST_TOKEN', 'MISSING_CREDENTIAL'), kinds)

    def test_ignored_key_produces_no_drift(self):
        # A non-credential key (feature flag) listed in the env_file location's
        # ignored_keys allowlist must NOT be reported as MISSING_REGISTRY_ENTRY.
        reg = _registry([_cred('FOO_TOKEN')])
        env_loc = 'env_file:/home/larry/credentials/.env.larry'
        reg['known_storage_locations'][env_loc]['ignored_keys'] = [
            'OURLIBERTY_NEWMISSION_INGEST_ENABLED',
        ]
        scans = {
            env_loc: {'FOO_TOKEN', 'OURLIBERTY_NEWMISSION_INGEST_ENABLED'},
            'gh_cli:/home/larry/.config/gh/hosts.yml': set(),
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': set(),
        }
        drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        names = [name for name, _, _ in drifts]
        self.assertNotIn('OURLIBERTY_NEWMISSION_INGEST_ENABLED', names)
        self.assertEqual(drifts, [])

    def test_non_ignored_unregistered_key_still_drifts(self):
        # Regression guard: an ignore-list present must NOT suppress a genuinely
        # unregistered NON-ignored key — it still drifts as MISSING_REGISTRY_ENTRY.
        reg = _registry([_cred('FOO_TOKEN')])
        env_loc = 'env_file:/home/larry/credentials/.env.larry'
        reg['known_storage_locations'][env_loc]['ignored_keys'] = [
            'OURLIBERTY_NEWMISSION_INGEST_ENABLED',
        ]
        scans = {
            env_loc: {
                'FOO_TOKEN',
                'OURLIBERTY_NEWMISSION_INGEST_ENABLED',
                'BAR_TOKEN',
            },
            'gh_cli:/home/larry/.config/gh/hosts.yml': set(),
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': set(),
        }
        drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertIn(('BAR_TOKEN', 'MISSING_REGISTRY_ENTRY'), kinds)
        self.assertNotIn(
            ('OURLIBERTY_NEWMISSION_INGEST_ENABLED', 'MISSING_REGISTRY_ENTRY'),
            kinds,
        )

    def test_ignored_keys_absent_is_backward_compatible(self):
        # No ignored_keys configured → behaves exactly as before (key drifts).
        reg = _registry([_cred('FOO_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {
                'FOO_TOKEN', 'OURLIBERTY_NEWMISSION_INGEST_ENABLED',
            },
            'gh_cli:/home/larry/.config/gh/hosts.yml': set(),
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': set(),
        }
        drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertIn(
            ('OURLIBERTY_NEWMISSION_INGEST_ENABLED', 'MISSING_REGISTRY_ENTRY'),
            kinds,
        )

    def test_workspace_mcp_directory_prefix_matches_entry(self):
        reg = _registry([_cred(
            'GOOGLE_OAUTH_REFRESH_TOKEN',
            location=(
                'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/'
                'agent.beacon.ourliberty@gmail.com.json'
            ),
        )])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': set(),
            'gh_cli:/home/larry/.config/gh/hosts.yml': set(),
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': {
                'GOOGLE_OAUTH_REFRESH_TOKEN',
            },
        }
        drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        self.assertEqual(drifts, [])


class MissingCredentialSuppressionTest(_IsolatedAgentsRoot):
    """M1 (integration-seam audit 2026-06-06): the drift scanner must share
    ONE notion of "is this tier configured" with `check_tier_distinctness`.

    A claude_cli `.credentials.json` is allowed to be absent in two supported
    states, and neither should fire a `MISSING_CREDENTIAL` drift:
      - the tier dispatches via a long-lived setup-token (the Tier 2 steady
        state where the operator lets creds.json lapse on purpose), and
      - `auth_orchestrator` has moved the live file aside to a
        `*.pre-orchestrator-*` backup mid-recovery.
    """

    _TIER2_LOC = ('claude_cli:/home/larry/.claude-larry-personal/.claude/'
                  '.credentials.json')

    def _tier2_registry(self):
        reg = _registry([
            _cred('LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2',
                  location=self._TIER2_LOC),
        ])
        reg['known_storage_locations'][self._TIER2_LOC] = {
            'description': 'Claude OAuth Tier 2',
            'scanner_strategy': 'JSON parse',
        }
        return reg

    @staticmethod
    def _empty_scans(reg):
        return {loc: set() for loc in reg['known_storage_locations']}

    def test_setup_token_suppresses_missing_credential(self):
        # (a) Tier 2 setup-token configured + creds.json absent → no drift.
        reg = self._tier2_registry()
        scans = self._empty_scans(reg)
        with mock.patch.dict(
            os.environ,
            {'CLAUDE_CODE_OAUTH_TOKEN_TIER2': 'sk-ant-oat-redacted'},
            clear=False,
        ):
            drifts, live_keys = h.detect_drift(reg, scan_overrides=scans)
        missing = [name for name, kind, _ in drifts
                   if kind == 'MISSING_CREDENTIAL']
        self.assertNotIn('LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2', missing)
        self.assertNotIn(
            'LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2:MISSING_CREDENTIAL',
            live_keys,
        )

    def test_unconfigured_tier_still_flags_missing_credential(self):
        # (b) No setup-token and no recovery → genuine drift still fires.
        reg = self._tier2_registry()
        scans = self._empty_scans(reg)
        with mock.patch.object(
            h.active_tier, '_setup_token_for_tier', return_value=None,
        ):
            drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertIn(
            ('LARRY_PERSONAL_CLAUDE_MAX_OAUTH_TIER2', 'MISSING_CREDENTIAL'),
            kinds,
        )

    def test_pre_orchestrator_backup_suppresses_missing_credential(self):
        # (c) auth_orchestrator moved the live file aside → suppress.
        with tempfile.TemporaryDirectory() as td:
            creds = Path(td) / '.credentials.json'
            (Path(td) / '.credentials.json.pre-orchestrator-1700000000'
             ).write_text('{}')
            loc = f'claude_cli:{creds}'
            reg = {
                '$schema_version': 1,
                'credentials': [_cred('CLAUDE_MAX_OAUTH', location=loc)],
                'known_storage_locations': {
                    loc: {'description': 'Claude OAuth',
                          'scanner_strategy': 'JSON parse'},
                },
            }
            scans = {loc: set()}
            with mock.patch.object(
                h.active_tier, '_setup_token_for_tier', return_value=None,
            ):
                drifts, _ = h.detect_drift(reg, scan_overrides=scans)
            missing = [name for name, kind, _ in drifts
                       if kind == 'MISSING_CREDENTIAL']
            self.assertNotIn('CLAUDE_MAX_OAUTH', missing)

    def test_no_backup_and_no_token_still_flags(self):
        # Control for (c): same path, no backup file → drift fires.
        with tempfile.TemporaryDirectory() as td:
            creds = Path(td) / '.credentials.json'
            loc = f'claude_cli:{creds}'
            reg = {
                '$schema_version': 1,
                'credentials': [_cred('CLAUDE_MAX_OAUTH', location=loc)],
                'known_storage_locations': {
                    loc: {'description': 'Claude OAuth',
                          'scanner_strategy': 'JSON parse'},
                },
            }
            scans = {loc: set()}
            with mock.patch.object(
                h.active_tier, '_setup_token_for_tier', return_value=None,
            ):
                drifts, _ = h.detect_drift(reg, scan_overrides=scans)
            kinds = [(name, kind) for name, kind, _ in drifts]
            self.assertIn(('CLAUDE_MAX_OAUTH', 'MISSING_CREDENTIAL'), kinds)

    def test_tier_mapping_tracks_active_tier_home(self):
        # The location->tier map derives from active_tier._credentials_path, so
        # moving TIER2_HOME there moves the mapping here too (no hardcoded
        # substring to drift out of sync).
        self.assertEqual(
            h._claude_cli_tier_for_location(self._TIER2_LOC), 'tier2')
        self.assertIsNone(
            h._claude_cli_tier_for_location('claude_cli:/tmp/elsewhere.json'))
        moved = '/home/larry/.claude-tier2-relocated'
        with mock.patch.object(h.active_tier, 'TIER2_HOME', moved):
            relocated = f'claude_cli:{moved}/.claude/.credentials.json'
            self.assertEqual(
                h._claude_cli_tier_for_location(relocated), 'tier2')
            # The old path is no longer tier2 once the home moves.
            self.assertIsNone(
                h._claude_cli_tier_for_location(self._TIER2_LOC))

    def test_setup_token_does_not_mask_non_claude_locations(self):
        # The suppression is scoped to claude_cli only — an env_file drift is
        # untouched even with both setup-tokens configured.
        reg = _registry([_cred('GHOST_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': set(),
            'gh_cli:/home/larry/.config/gh/hosts.yml': set(),
            'claude_cli:/home/larry/.claude/.credentials.json': set(),
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/':
                set(),
        }
        with mock.patch.dict(
            os.environ,
            {'CLAUDE_CODE_OAUTH_TOKEN_TIER1': 't1',
             'CLAUDE_CODE_OAUTH_TOKEN_TIER2': 't2'},
            clear=False,
        ):
            drifts, _ = h.detect_drift(reg, scan_overrides=scans)
        kinds = [(name, kind) for name, kind, _ in drifts]
        self.assertIn(('GHOST_TOKEN', 'MISSING_CREDENTIAL'), kinds)


class DedupTest(_IsolatedAgentsRoot):
    def test_first_drift_is_re_dm_eligible(self):
        state = {'drifts': {}}
        self.assertTrue(h._should_re_dm(state, 'FOO', 'MISSING_CREDENTIAL'))

    def test_just_DMed_blocks_re_DM(self):
        state = {'drifts': {}}
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        h._record_dm(state, 'FOO', 'MISSING_CREDENTIAL', now=now)
        self.assertFalse(
            h._should_re_dm(state, 'FOO', 'MISSING_CREDENTIAL', now=now),
        )

    def test_after_window_re_DM_eligible(self):
        state = {'drifts': {}}
        old = datetime(2026, 5, 19, 6, 0, tzinfo=timezone.utc)
        h._record_dm(state, 'FOO', 'MISSING_CREDENTIAL', now=old)
        later = old + timedelta(hours=7)
        self.assertTrue(
            h._should_re_dm(state, 'FOO', 'MISSING_CREDENTIAL', now=later),
        )

    def test_dm_count_increments(self):
        state = {'drifts': {}}
        h._record_dm(state, 'FOO', 'MISSING_CREDENTIAL')
        h._record_dm(state, 'FOO', 'MISSING_CREDENTIAL')
        self.assertEqual(
            state['drifts']['FOO:MISSING_CREDENTIAL']['dm_count'], 2,
        )

    def test_reconciled_keys_returned(self):
        state = {'drifts': {
            'FOO:MISSING_CREDENTIAL': {'dm_count': 1},
            'BAR:MISSING_REGISTRY_ENTRY': {'dm_count': 1},
        }}
        live = {'FOO:MISSING_CREDENTIAL'}
        gone = h._reconciled_keys(state, live)
        self.assertEqual(gone, ['BAR:MISSING_REGISTRY_ENTRY'])


class KillSwitchTest(_IsolatedAgentsRoot):
    def test_kill_switch_exits_clean(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(h, 'KILL_SWITCH', Path(td) / 'on'):
                (Path(td) / 'on').write_text('1')
                counts = h.run_once(
                    registry=_registry(),
                    state={'drifts': {}},
                    scan_overrides={},
                )
                self.assertEqual(counts['dm_sent'], 0)


class OrchestrationTest(_IsolatedAgentsRoot):
    def setUp(self):
        super().setUp()
        self._dm_calls = []

        def fake_dm(message, subject, suggested_action, severity='warning'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
            })
            return True

        self._dm_patch = mock.patch.object(h, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

    def test_dry_run_with_drift_sends_one_activation_dm(self):
        reg = _registry([_cred('FOO_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {
                'FOO_TOKEN', 'BAR_TOKEN',
            },
        }
        counts = h.run_once(
            registry=reg, state={'drifts': {}},
            scan_overrides=scans, dry_run_override=True,
        )
        self.assertEqual(counts['dm_sent'], 1)
        self.assertEqual(self._dm_calls[0]['subject'],
                         'credential-drift-healer: activate to receive drift alerts')

    def test_live_mode_dm_per_drift(self):
        reg = _registry([_cred('FOO_TOKEN'), _cred('GHOST_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {
                'FOO_TOKEN', 'BAR_TOKEN',  # GHOST missing; BAR extra
            },
        }
        counts = h.run_once(
            registry=reg, state={'drifts': {}},
            scan_overrides=scans, dry_run_override=False,
        )
        # 1 MISSING_REGISTRY_ENTRY (BAR) + 1 MISSING_CREDENTIAL (GHOST).
        self.assertEqual(counts['missing_registry_entry'], 1)
        self.assertEqual(counts['missing_credential'], 1)
        self.assertEqual(counts['dm_sent'], 2)
        subjects = {c['subject'] for c in self._dm_calls}
        self.assertIn('credential-drift:MISSING_REGISTRY_ENTRY:BAR_TOKEN', subjects)
        self.assertIn('credential-drift:MISSING_CREDENTIAL:GHOST_TOKEN', subjects)

    def test_dedup_suppresses_second_tick(self):
        reg = _registry([_cred('FOO_TOKEN'), _cred('GHOST_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {'FOO_TOKEN'},
        }
        state = {'drifts': {}}
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        h.run_once(
            registry=reg, state=state, scan_overrides=scans,
            dry_run_override=False, now=now,
        )
        self._dm_calls.clear()
        # Second tick, same drift, only 1 hour later — DM should suppress.
        counts = h.run_once(
            registry=reg, state=state, scan_overrides=scans,
            dry_run_override=False, now=now + timedelta(hours=1),
        )
        self.assertEqual(counts['dm_sent'], 0)
        self.assertEqual(counts['dm_suppressed_dedup'], 1)
        self.assertEqual(self._dm_calls, [])

    def test_after_window_re_DMs(self):
        reg = _registry([_cred('GHOST_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': set(),
        }
        state = {'drifts': {}}
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        h.run_once(
            registry=reg, state=state, scan_overrides=scans,
            dry_run_override=False, now=now,
        )
        self._dm_calls.clear()
        counts = h.run_once(
            registry=reg, state=state, scan_overrides=scans,
            dry_run_override=False, now=now + timedelta(hours=7),
        )
        self.assertEqual(counts['dm_sent'], 1)

    def test_reconciled_drift_garbage_collected(self):
        reg = _registry([_cred('FOO_TOKEN'), _cred('GHOST_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {'FOO_TOKEN'},
        }
        state = {'drifts': {}}
        h.run_once(
            registry=reg, state=state, scan_overrides=scans,
            dry_run_override=False,
        )
        self.assertIn('GHOST_TOKEN:MISSING_CREDENTIAL', state['drifts'])
        # Operator installs the credential — next tick reports no drift.
        scans_fixed = {
            'env_file:/home/larry/credentials/.env.larry': {
                'FOO_TOKEN', 'GHOST_TOKEN',
            },
        }
        counts = h.run_once(
            registry=reg, state=state, scan_overrides=scans_fixed,
            dry_run_override=False,
        )
        self.assertNotIn('GHOST_TOKEN:MISSING_CREDENTIAL', state['drifts'])
        self.assertEqual(counts['reconciled_gc'], 1)

    def test_dry_run_no_drift_no_dm(self):
        reg = _registry([_cred('FOO_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {'FOO_TOKEN'},
        }
        counts = h.run_once(
            registry=reg, state={'drifts': {}}, scan_overrides=scans,
            dry_run_override=True,
        )
        self.assertEqual(counts['dm_sent'], 0)

    def test_dry_run_activation_only_once_per_state(self):
        reg = _registry([_cred('FOO_TOKEN')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {
                'FOO_TOKEN', 'BAR_TOKEN', 'BAZ_TOKEN',
            },
        }
        state = {'drifts': {}}
        h.run_once(
            registry=reg, state=state, scan_overrides=scans,
            dry_run_override=True,
        )
        # Two drifts, but activation DM fires only once.
        self.assertEqual(
            sum(1 for c in self._dm_calls
                if c['subject'].startswith('credential-drift-healer:')),
            1,
        )


class RenderingTest(_IsolatedAgentsRoot):
    def test_missing_registry_entry_message_mentions_4_artifacts(self):
        msg, subj, sug = h._render_missing_registry_entry(
            'FOO', {'storage_location': 'env_file:/home/larry/credentials/.env.larry'},
        )
        self.assertIn('4-artifact', msg)
        self.assertIn('FOO', msg)
        self.assertIn('FOO', subj)

    def test_missing_credential_message_mentions_severity(self):
        msg, subj, sug = h._render_missing_credential(
            'FOO',
            {'storage_location': 'env_file:/x', 'runbook_path': 'docs/x.md',
             'severity_if_lapsed': 'critical'},
        )
        self.assertIn('critical', msg)
        self.assertIn('docs/x.md', sug)


class ResolveRegistryOriginTest(_IsolatedAgentsRoot):
    """origin/main canonical reads + fetch-fallback behavior."""

    def test_origin_success_reads_from_git_show_stdout(self):
        """Happy path: git fetch + git show succeed, stdout is parsed."""
        registry_json = json.dumps(_registry([_cred('CANON_TOKEN')]))
        with mock.patch('subprocess.run') as mock_sub:
            # First call: git fetch (rc=0). Second call: git show (stdout=json).
            mock_sub.side_effect = [
                mock.Mock(returncode=0, stdout='', stderr=''),
                mock.Mock(returncode=0, stdout=registry_json, stderr=''),
            ]
            reg = h.resolve_registry(
                source='origin', state={'drifts': {}, '_meta': {}},
            )
        self.assertIsNotNone(reg)
        self.assertEqual(reg['credentials'][0]['name'], 'CANON_TOKEN')
        # Verify the two subprocess calls have the expected arg shape.
        self.assertEqual(mock_sub.call_count, 2)
        fetch_args = mock_sub.call_args_list[0].args[0]
        show_args = mock_sub.call_args_list[1].args[0]
        self.assertEqual(fetch_args[0:3], ['git', '-C', str(h.REPO_ROOT)])
        self.assertIn('fetch', fetch_args)
        self.assertIn('main', fetch_args)
        self.assertIn('show', show_args)
        self.assertTrue(
            any('origin/main:' in a for a in show_args),
            f'no origin/main:<path> arg in {show_args!r}',
        )

    def test_origin_fetch_fail_falls_back_to_local_with_dm(self):
        """git fetch returns non-zero: log WARN, fire fallback DM once,
        read local file as fallback. State records the DM."""
        # Write a registry to the local REGISTRY_PATH (the fallback source).
        local_reg = _registry([_cred('LOCAL_TOKEN')])
        # Patch REGISTRY_PATH to a temp file so the fallback succeeds.
        with tempfile.TemporaryDirectory() as td:
            local_path = Path(td) / 'token-rotation-schedule.json'
            local_path.write_text(json.dumps(local_reg))
            dm_calls = []

            def fake_dm(message, subject, suggested_action, severity='warning'):
                dm_calls.append({
                    'message': message, 'subject': subject,
                    'suggested_action': suggested_action, 'severity': severity,
                })
                return True

            state = {'drifts': {}, '_meta': {}}
            with mock.patch.object(h, 'REGISTRY_PATH', local_path), \
                 mock.patch.object(h, 'dm_larry', fake_dm), \
                 mock.patch('subprocess.run') as mock_sub:
                mock_sub.return_value = mock.Mock(
                    returncode=128, stdout='', stderr='fatal: could not read',
                )
                reg = h.resolve_registry(source='origin', state=state)

            self.assertIsNotNone(reg)
            self.assertEqual(reg['credentials'][0]['name'], 'LOCAL_TOKEN')
            self.assertEqual(len(dm_calls), 1)
            self.assertIn('origin/main', dm_calls[0]['subject'])
            self.assertIn('falling back', dm_calls[0]['message'].lower())
            self.assertIn(
                'fetch_fallback_last_dm', state.get('_meta', {}),
            )

    def test_fetch_fallback_dm_suppressed_within_cooldown(self):
        """Second origin failure inside the 6h window must not re-DM."""
        local_reg = _registry([_cred('LOCAL_TOKEN')])
        with tempfile.TemporaryDirectory() as td:
            local_path = Path(td) / 'token-rotation-schedule.json'
            local_path.write_text(json.dumps(local_reg))
            dm_calls = []

            def fake_dm(message, subject, suggested_action, severity='warning'):
                dm_calls.append({'subject': subject})
                return True

            now1 = datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc)
            now2 = now1 + timedelta(hours=1)  # 1h later — well inside 6h
            state = {'drifts': {}, '_meta': {}}
            with mock.patch.object(h, 'REGISTRY_PATH', local_path), \
                 mock.patch.object(h, 'dm_larry', fake_dm), \
                 mock.patch('subprocess.run') as mock_sub:
                mock_sub.return_value = mock.Mock(
                    returncode=128, stdout='', stderr='fatal',
                )
                h.resolve_registry(source='origin', state=state, now=now1)
                h.resolve_registry(source='origin', state=state, now=now2)
            self.assertEqual(len(dm_calls), 1)  # only first tick DMs

    def test_source_local_bypasses_fetch(self):
        """`--source local` must not call subprocess.run at all (no fetch,
        no fallback DM). Reads the local file directly."""
        local_reg = _registry([_cred('LOCAL_ONLY')])
        with tempfile.TemporaryDirectory() as td:
            local_path = Path(td) / 'token-rotation-schedule.json'
            local_path.write_text(json.dumps(local_reg))
            dm_calls = []

            def fake_dm(message, subject, suggested_action, severity='warning'):
                dm_calls.append({'subject': subject})
                return True

            state = {'drifts': {}, '_meta': {}}
            with mock.patch.object(h, 'REGISTRY_PATH', local_path), \
                 mock.patch.object(h, 'dm_larry', fake_dm), \
                 mock.patch('subprocess.run') as mock_sub:
                reg = h.resolve_registry(source='local', state=state)
            self.assertIsNotNone(reg)
            self.assertEqual(reg['credentials'][0]['name'], 'LOCAL_ONLY')
            mock_sub.assert_not_called()
            self.assertEqual(dm_calls, [])

    def test_origin_classification_uses_canonical_not_local(self):
        """The PR #113 incident: origin/main contains an entry that local
        doesn't (or vice versa). The healer must classify based on
        origin/main contents. Here: local says one cred is missing-in-
        registry; origin says it's already registered → no drift."""
        # origin has BOTH FOO and BAR registered.
        origin_reg = _registry([_cred('FOO'), _cred('BAR')])
        # local would have only FOO registered (stale checkout).
        local_reg = _registry([_cred('FOO')])
        scans = {
            'env_file:/home/larry/credentials/.env.larry': {'FOO', 'BAR'},
        }
        with tempfile.TemporaryDirectory() as td:
            local_path = Path(td) / 'token-rotation-schedule.json'
            local_path.write_text(json.dumps(local_reg))
            with mock.patch.object(h, 'REGISTRY_PATH', local_path), \
                 mock.patch('subprocess.run') as mock_sub:
                # origin fetch + show succeed and return the canonical
                # registry. Local would be the stale fallback if origin
                # failed, but origin succeeds here.
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout='', stderr=''),
                    mock.Mock(returncode=0, stdout=json.dumps(origin_reg),
                              stderr=''),
                ]
                reg = h.resolve_registry(
                    source='origin', state={'drifts': {}, '_meta': {}},
                )
            # detect_drift against the canonical registry → no drift.
            drifts, _ = h.detect_drift(reg, scan_overrides=scans)
            self.assertEqual(drifts, [])
            # Sanity: against the stale local registry, BAR WOULD have
            # looked like MISSING_REGISTRY_ENTRY. This documents the
            # exact failure mode the canonical-source fix prevents.
            stale_drifts, _ = h.detect_drift(local_reg, scan_overrides=scans)
            stale_kinds = [(n, k) for n, k, _ in stale_drifts]
            self.assertIn(('BAR', 'MISSING_REGISTRY_ENTRY'), stale_kinds)


class CliArgsTest(unittest.TestCase):
    """--source local|origin and --check-tiers CLI flag plumbing."""

    def test_default_is_origin(self):
        args = h._parse_args([])
        self.assertEqual(args.source, 'origin')

    def test_explicit_origin(self):
        args = h._parse_args(['--source', 'origin'])
        self.assertEqual(args.source, 'origin')

    def test_explicit_local(self):
        args = h._parse_args(['--source', 'local'])
        self.assertEqual(args.source, 'local')

    def test_invalid_source_rejected(self):
        with self.assertRaises(SystemExit):
            h._parse_args(['--source', 'bogus'])

    def test_check_tiers_default_false(self):
        args = h._parse_args([])
        self.assertFalse(args.check_tiers)

    def test_check_tiers_flag_sets_true(self):
        args = h._parse_args(['--check-tiers'])
        self.assertTrue(args.check_tiers)


# -------------------- tier-distinctness check --------------------


class TierDistinctnessTest(_IsolatedAgentsRoot):
    """`check_tier_distinctness` directly: distinct, identical, logged-out,
    unparseable, timeout. The 2026-05-28 incident was Tier 2 silently
    authenticating as Tier 1's account — these tests verify the healer
    would catch that and the other shapes that mask the same problem.
    """

    def setUp(self):
        super().setUp()
        # These tests exercise the legacy creds.json distinctness path, so the
        # setup-token short-circuit must be inactive. Clear any ambient
        # CLAUDE_CODE_OAUTH_TOKEN_TIER{1,2} (the live host has them set) and
        # restore on teardown.
        self._token_env_orig = {}
        for name in (
            'CLAUDE_CODE_OAUTH_TOKEN_TIER1',
            'CLAUDE_CODE_OAUTH_TOKEN_TIER2',
        ):
            self._token_env_orig[name] = os.environ.pop(name, None)
        self.addCleanup(self._restore_token_env)
        self._dm_calls: list[dict] = []

        def fake_dm(message, subject, suggested_action, severity='warning'):
            self._dm_calls.append({
                'message': message, 'subject': subject,
                'suggested_action': suggested_action, 'severity': severity,
            })
            return True

        self._dm_patch = mock.patch.object(h, 'dm_larry', fake_dm)
        self._dm_patch.start()
        self.addCleanup(self._dm_patch.stop)

    def _restore_token_env(self):
        for name, prev in self._token_env_orig.items():
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev

    def _probe(self, mapping):
        """Build a probe stub that maps HOME → (status, identity)."""
        def probe(home):
            return mapping[home]
        return probe

    def test_distinct_orgs_no_dm(self):
        """Tier 1 and Tier 2 ok with different orgIds → no DM, no
        live_keys, no state change. The healthy steady state."""
        state = {'drifts': {}}
        counts, live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('ok', 'org-tier1'),
                h.TIER2_HOME: ('ok', 'org-tier2'),
            }),
        )
        self.assertEqual(counts['tier_distinct'], 1)
        self.assertEqual(counts['tier_dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])
        self.assertEqual(live, set())
        self.assertEqual(state['drifts'], {})

    def test_identical_orgs_dms_larry(self):
        """Both tiers authenticate as the SAME orgId — the 2026-05-28
        failure mode. DM fires, live_keys preserves dedup state."""
        state = {'drifts': {}}
        counts, live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('ok', 'org-shared'),
                h.TIER2_HOME: ('ok', 'org-shared'),
            }),
        )
        self.assertEqual(counts['tier_identical'], 1)
        self.assertEqual(counts['tier_dm_sent'], 1)
        self.assertEqual(len(self._dm_calls), 1)
        self.assertEqual(
            self._dm_calls[0]['subject'], 'tier-distinctness:claude-oauth',
        )
        self.assertIn('org-shared', self._dm_calls[0]['message'])
        self.assertIn(
            'tier-distinctness:claude-oauth',
            live.pop() if live else '',
        )
        # State recorded the DM so a follow-up tick will dedup.
        self.assertIn('tier-distinctness:claude-oauth', state['drifts'])

    def test_tier2_logged_out_dms_larry(self):
        """Tier 2 not logged-in at all → DM. (This is the post-incident
        state on 2026-05-26 just before Larry re-authed Tier 2.)"""
        state = {'drifts': {}}
        counts, live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('ok', 'org-tier1'),
                h.TIER2_HOME: ('logged_out', None),
            }),
        )
        self.assertEqual(counts['tier_logged_out'], 1)
        self.assertEqual(counts['tier_dm_sent'], 1)
        self.assertEqual(len(self._dm_calls), 1)
        self.assertIn('logged-out', self._dm_calls[0]['message'])
        self.assertIn(
            h._drift_key(h.TIER_DISTINCTNESS_NAME, h.TIER_DISTINCTNESS_KIND),
            live,
        )

    def test_unparseable_dms_larry(self):
        """Tier 1 returns unparseable JSON (CLI broken / wrong version) →
        DM. We can't reason about a tier whose status we can't read."""
        state = {'drifts': {}}
        counts, live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('unparseable', None),
                h.TIER2_HOME: ('ok', 'org-tier2'),
            }),
        )
        self.assertEqual(counts['tier_unparseable'], 1)
        self.assertEqual(counts['tier_dm_sent'], 1)
        self.assertIn('unparseable', self._dm_calls[0]['message'])
        self.assertNotEqual(live, set())

    def test_timeout_no_dm_no_state_change(self):
        """A transient `claude auth status` hang must NEVER page Larry.
        Counts the timeout, but no DM and no state mutation. This is
        the 'do not alarm on unknown' clause from the spec."""
        state = {'drifts': {}}
        counts, live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('timeout', None),
                h.TIER2_HOME: ('ok', 'org-tier2'),
            }),
        )
        self.assertEqual(counts['tier_timeout'], 1)
        self.assertEqual(counts['tier_dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])
        # No prior alarm → no live_keys preserved.
        self.assertEqual(live, set())
        self.assertEqual(state['drifts'], {})

    def test_timeout_preserves_prior_alarm_state(self):
        """If we previously DMed about identical tiers and now the probe
        times out, we must NOT let the run_once GC sweep clear the dedup
        state — otherwise the next probe would re-DM immediately."""
        drift_key = h._drift_key(
            h.TIER_DISTINCTNESS_NAME, h.TIER_DISTINCTNESS_KIND,
        )
        state = {'drifts': {drift_key: {
            'dm_count': 1,
            'last_dm_at': datetime(2026, 5, 28, 12, 0,
                                   tzinfo=timezone.utc).isoformat(),
        }}}
        counts, live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('timeout', None),
                h.TIER2_HOME: ('timeout', None),
            }),
        )
        self.assertEqual(counts['tier_timeout'], 1)
        self.assertIn(drift_key, live)
        self.assertIn(drift_key, state['drifts'])

    def test_dedup_suppresses_second_tick_within_window(self):
        """Same tier-distinctness drift within the 6h window → no second
        DM. Matches the existing credential-drift dedup behavior."""
        state = {'drifts': {}}
        probe = self._probe({
            h.TIER1_HOME: ('ok', 'org-shared'),
            h.TIER2_HOME: ('ok', 'org-shared'),
        })
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        h.check_tier_distinctness(state, now=now, probe=probe)
        self._dm_calls.clear()
        counts, _ = h.check_tier_distinctness(
            state, now=now + timedelta(hours=1), probe=probe,
        )
        self.assertEqual(counts['tier_dm_sent'], 0)
        self.assertEqual(counts['tier_dm_suppressed_dedup'], 1)
        self.assertEqual(self._dm_calls, [])

    def test_resolution_clears_alarm_state_via_run_once_gc(self):
        """After Larry re-auths Tier 2 to a distinct account, the next
        run_once tick must garbage-collect the tier-distinctness dedup
        entry — otherwise the alarm would silently linger in state."""
        # Pre-seed state as if we had previously alarmed on identical tiers.
        drift_key = h._drift_key(
            h.TIER_DISTINCTNESS_NAME, h.TIER_DISTINCTNESS_KIND,
        )
        state = {
            'drifts': {drift_key: {
                'dm_count': 1,
                'last_dm_at': datetime(2026, 5, 28, 0, 0,
                                       tzinfo=timezone.utc).isoformat(),
            }},
        }
        # Patch the module probe so run_once sees distinct tiers.
        with mock.patch.object(
            h, '_run_claude_auth_status',
            side_effect=self._probe({
                h.TIER1_HOME: ('ok', 'org-tier1'),
                h.TIER2_HOME: ('ok', 'org-tier2'),
            }),
        ):
            counts = h.run_once(
                registry=_registry(), state=state,
                scan_overrides={}, dry_run_override=False,
            )
        self.assertEqual(counts['tier_distinct'], 1)
        self.assertNotIn(drift_key, state['drifts'])
        self.assertEqual(counts['reconciled_gc'], 1)


class TierDistinctnessSetupTokenShortCircuitTest(TierDistinctnessTest):
    """When a tier dispatches via its long-lived setup-token, its creds.json
    `claude auth status` state is irrelevant: a logged-out / unparseable read
    there is NOT a dispatch-auth failure and must not fire the
    "cannot authenticate" distinctness DM. This is the fix for the recurring
    false "Tier 2 down" signal that arose once the Tier 2 creds.json lapsed
    (intentionally unrefreshed) while the setup-token stayed valid.
    """

    def test_tier2_logged_out_suppressed_when_setup_token_present(self):
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'FAKE-tier2-token-xxxx'
        state = {'drifts': {}}
        counts, live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('ok', 'org-tier1'),
                h.TIER2_HOME: ('logged_out', None),
            }),
        )
        self.assertEqual(counts['tier_distinct'], 1)
        self.assertEqual(counts['tier_dm_sent'], 0)
        self.assertEqual(counts['tier_logged_out'], 0)
        self.assertEqual(self._dm_calls, [])
        self.assertEqual(live, set())

    def test_tier2_unparseable_suppressed_when_setup_token_present(self):
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'FAKE-tier2-token-xxxx'
        state = {'drifts': {}}
        counts, _live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('ok', 'org-tier1'),
                h.TIER2_HOME: ('unparseable', None),
            }),
        )
        self.assertEqual(counts['tier_distinct'], 1)
        self.assertEqual(counts['tier_dm_sent'], 0)
        self.assertEqual(self._dm_calls, [])

    def test_identical_orgs_still_dm_with_setup_tokens(self):
        # The genuine duplicate-account failure mode (both tiers 'ok' with the
        # SAME identity) is NOT masked by the short-circuit — it only fires
        # when both report 'ok' from creds.json, which the token override
        # never touches.
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = 'FAKE-tier1-token-xxxx'
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'FAKE-tier2-token-yyyy'
        state = {'drifts': {}}
        counts, _live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('ok', 'org-shared'),
                h.TIER2_HOME: ('ok', 'org-shared'),
            }),
        )
        self.assertEqual(counts['tier_identical'], 1)
        self.assertEqual(counts['tier_dm_sent'], 1)

    def test_logged_out_tier_without_token_still_dms(self):
        # Tier 1 has no setup-token and is logged-out → still alarms, even
        # though Tier 2 has a token. The short-circuit is strictly per-tier.
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER2'] = 'FAKE-tier2-token-xxxx'
        state = {'drifts': {}}
        counts, _live = h.check_tier_distinctness(
            state,
            probe=self._probe({
                h.TIER1_HOME: ('logged_out', None),
                h.TIER2_HOME: ('ok', 'org-tier2'),
            }),
        )
        self.assertEqual(counts['tier_logged_out'], 1)
        self.assertEqual(counts['tier_dm_sent'], 1)


class RunClaudeAuthStatusTest(_IsolatedAgentsRoot):
    """Subprocess wrapper: `claude auth status` JSON parsing + classification."""

    _stub_tier_probe = False

    def _mock_run(self, stdout='', stderr='', returncode=0, side_effect=None):
        m = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        if side_effect is not None:
            return mock.patch('subprocess.run', side_effect=side_effect)
        return mock.patch('subprocess.run', return_value=m)

    def test_ok_with_orgid(self):
        body = json.dumps({
            'loggedIn': True, 'email': 'a@b.com',
            'orgId': '43441a1c-123f-4933-8f7f-55572925600f',
        })
        with self._mock_run(stdout=body):
            status, ident = h._run_claude_auth_status('/some/home')
        self.assertEqual(status, 'ok')
        self.assertEqual(ident, '43441a1c-123f-4933-8f7f-55572925600f')

    def test_ok_orgid_missing_falls_back_to_email(self):
        body = json.dumps({'loggedIn': True, 'email': 'a@b.com'})
        with self._mock_run(stdout=body):
            status, ident = h._run_claude_auth_status('/some/home')
        self.assertEqual(status, 'ok')
        self.assertEqual(ident, 'a@b.com')

    def test_logged_out(self):
        body = json.dumps({'loggedIn': False, 'authMethod': 'none'})
        with self._mock_run(stdout=body):
            status, ident = h._run_claude_auth_status('/some/home')
        self.assertEqual(status, 'logged_out')
        self.assertIsNone(ident)

    def test_unparseable_json(self):
        with self._mock_run(stdout='not json at all'):
            status, ident = h._run_claude_auth_status('/some/home')
        self.assertEqual(status, 'unparseable')
        self.assertIsNone(ident)

    def test_logged_in_but_no_identity_is_unparseable(self):
        """Defensive: loggedIn=true but neither orgId nor email present —
        treat as unparseable so we surface the unexpected shape."""
        body = json.dumps({'loggedIn': True})
        with self._mock_run(stdout=body):
            status, ident = h._run_claude_auth_status('/some/home')
        self.assertEqual(status, 'unparseable')

    def test_timeout(self):
        import subprocess
        with self._mock_run(side_effect=subprocess.TimeoutExpired(
            cmd=['claude', 'auth', 'status'], timeout=20,
        )):
            status, ident = h._run_claude_auth_status(
                '/some/home', timeout_s=20,
            )
        self.assertEqual(status, 'timeout')
        self.assertIsNone(ident)

    def test_filenotfound_is_unparseable(self):
        with self._mock_run(side_effect=FileNotFoundError('no claude bin')):
            status, ident = h._run_claude_auth_status('/some/home')
        self.assertEqual(status, 'unparseable')

    def test_home_env_passed_to_subprocess(self):
        """The HOME env var is the whole mechanism — verify it's set."""
        body = json.dumps({'loggedIn': True, 'orgId': 'org-x'})
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=body, stderr='',
            )
            h._run_claude_auth_status('/some/test/home')
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs['env']['HOME'], '/some/test/home')
        self.assertEqual(kwargs['timeout'], h.CLAUDE_AUTH_TIMEOUT_S)


if __name__ == '__main__':
    unittest.main()
