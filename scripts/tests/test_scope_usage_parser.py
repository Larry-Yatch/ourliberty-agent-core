#!/usr/bin/env python3
"""Tests for scope_usage_parser (E1.5.2).

Covers gh-subcommand → scope mapping, gh-api endpoint dispatch,
workspace-mcp tool → scope mapping, lookback window, and the public
analyze_scope_usage dispatcher.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_scope_usage_parser
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import scope_usage_parser as sup  # noqa: E402


class GhSubcommandMappingTest(unittest.TestCase):
    def test_pr_maps_to_repo(self):
        self.assertEqual(sup._scope_for_gh_subcmd('pr'), 'repo')

    def test_workflow_maps_to_workflow(self):
        self.assertEqual(sup._scope_for_gh_subcmd('workflow'), 'workflow')

    def test_gist_maps_to_gist(self):
        self.assertEqual(sup._scope_for_gh_subcmd('gist'), 'gist')

    def test_unknown_subcmd_returns_none(self):
        self.assertIsNone(sup._scope_for_gh_subcmd('alias'))


class GhApiEndpointMappingTest(unittest.TestCase):
    def test_repos_endpoint_maps_to_repo(self):
        self.assertEqual(sup._scope_for_gh_api('repos/foo/bar'), 'repo')

    def test_orgs_endpoint_maps_to_read_org(self):
        self.assertEqual(sup._scope_for_gh_api('orgs/myorg'), 'read:org')

    def test_unknown_endpoint_returns_none(self):
        self.assertIsNone(sup._scope_for_gh_api('rate_limit'))


class GithubScopeAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'fake.log'

    def _write(self, lines):
        self.log.write_text('\n'.join(lines) + '\n')

    def test_counts_pr_invocations_as_repo(self):
        self._write([
            '[2026-05-19T10:00:00] gh pr merge 42 --squash',
            '[2026-05-19T10:01:00] gh pr list --state open',
        ])
        out = sup._analyze_github_gh_oauth(
            days=30, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out.get('repo'), 2)

    def test_counts_workflow_separately(self):
        self._write([
            '[2026-05-19T10:00:00] gh pr merge 1',
            '[2026-05-19T10:01:00] gh workflow run release',
        ])
        out = sup._analyze_github_gh_oauth(
            days=30, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out.get('repo'), 1)
        self.assertEqual(out.get('workflow'), 1)

    def test_gh_api_repos_routes_to_repo_scope(self):
        self._write([
            '[2026-05-19T10:00:00] gh api repos/foo/bar/pulls',
        ])
        out = sup._analyze_github_gh_oauth(
            days=30, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out.get('repo'), 1)

    def test_gh_api_orgs_routes_to_read_org(self):
        self._write([
            '[2026-05-19T10:00:00] gh api orgs/myorg/members',
        ])
        out = sup._analyze_github_gh_oauth(
            days=30, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out.get('read:org'), 1)

    def test_lookback_window_excludes_old_lines(self):
        self._write([
            '[2025-01-01T00:00:00] gh pr merge 1',
        ])
        out = sup._analyze_github_gh_oauth(
            days=7, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        # 7-day lookback excludes the 2025 line.
        self.assertEqual(out.get('repo', 0), 0)

    def test_missing_log_returns_empty(self):
        missing = Path(self.tmp.name) / 'does-not-exist.log'
        out = sup._analyze_github_gh_oauth(
            days=30, log_paths=[missing],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out, {})


class WorkspaceMcpAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'inbox.log'

    def _write(self, lines):
        self.log.write_text('\n'.join(lines) + '\n')

    def test_doc_tool_maps_to_docs_scope(self):
        self._write([
            '[2026-05-19T10:00:00] [INFO] tool=workspace-mcp_create_doc',
            '[2026-05-19T10:01:00] [INFO] tool=workspace-mcp_get_doc',
        ])
        out = sup._analyze_workspace_mcp(
            days=30, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out.get('docs'), 2)

    def test_drive_tool_maps_to_drive_scope(self):
        self._write([
            '[2026-05-19T10:00:00] [INFO] tool=workspace-mcp_search_drive',
        ])
        out = sup._analyze_workspace_mcp(
            days=30, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out.get('drive'), 1)

    def test_unknown_tool_skipped(self):
        self._write([
            '[2026-05-19T10:00:00] tool=workspace-mcp_random_thing',
        ])
        out = sup._analyze_workspace_mcp(
            days=30, log_paths=[self.log],
            now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out, {})


class AnalyzeScopeUsageDispatchTest(unittest.TestCase):
    """Public entry point dispatch."""

    def test_github_routes_to_gh_analyzer(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'fake.log'
            log.write_text('[2026-05-19T10:00:00] gh pr merge 1\n')
            out = sup.analyze_scope_usage(
                'GITHUB_GH_OAUTH_TOKEN', days=30, log_paths=[log],
                now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            )
        self.assertIsNotNone(out)
        self.assertEqual(out.get('repo'), 1)

    def test_google_routes_to_mcp_analyzer(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'inbox.log'
            log.write_text('[2026-05-19T10:00:00] tool=workspace-mcp_get_doc\n')
            out = sup.analyze_scope_usage(
                'GOOGLE_OAUTH_REFRESH_TOKEN', days=30, log_paths=[log],
                now=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            )
        self.assertIsNotNone(out)
        self.assertEqual(out.get('docs'), 1)

    def test_vercel_returns_none(self):
        self.assertIsNone(sup.analyze_scope_usage('VERCEL_TOKEN'))

    def test_claude_returns_none(self):
        self.assertIsNone(sup.analyze_scope_usage('CLAUDE_MAX_OAUTH'))

    def test_telegram_bot_returns_none(self):
        self.assertIsNone(sup.analyze_scope_usage('TELEGRAM_BOT_TOKEN_BEACON'))


if __name__ == '__main__':
    unittest.main()
