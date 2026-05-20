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

import heal_credential_registry_drift as h  # noqa: E402


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


class ScanEnvFileTest(unittest.TestCase):
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


class ScanClaudeCliTest(unittest.TestCase):
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


class ScanWorkspaceMcpTest(unittest.TestCase):
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


class DetectDriftTest(unittest.TestCase):
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


class DedupTest(unittest.TestCase):
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


class KillSwitchTest(unittest.TestCase):
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


class OrchestrationTest(unittest.TestCase):
    def setUp(self):
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


class RenderingTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
