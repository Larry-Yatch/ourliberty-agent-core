#!/usr/bin/env python3
"""scope_usage_parser.py — log-parser-based scope usage analyzer (E1.5.2).

When Pulse fires a `scope_audit` rotation reminder, it needs to tell Larry
which scopes are actually being exercised so the audit decision ("can we
drop X?") is data-backed instead of guesswork. This module reads existing
logs and grep-counts scope-mapped tool calls — no instrumentation required.

Approach:
  - GitHub gh-OAuth: grep ~/agents/logs/outbox-notifier.log +
    heal-pr-auto-merge.log for `gh <subcommand>` invocations. Map
    subcommand → scope via _GH_SUBCMD_TO_SCOPE table.
  - workspace-mcp: grep inbox-watcher journal entries for the
    `tool=workspace-mcp_<name>` shape Beacon's agent_runner emits. Map
    tool name → scope via _MCP_TOOL_TO_SCOPE.
  - Vercel + Claude: return None. Vercel isn't yet observable (will be
    instrumented in E2.2 when deploy_notifier ships); Claude Max is a
    single scope (the subscription itself).

Library use:
    from scope_usage_parser import analyze_scope_usage
    counts = analyze_scope_usage('GITHUB_GH_OAUTH_TOKEN', days=90)
    # -> {'repo': 47, 'workflow': 12, 'gist': 0, 'read:org': 3}
    # or None for credentials we can't observe via logs yet.

The grep-based approach is intentionally fragile: it produces ROUGH counts
sufficient for a first scope-audit decision in 2027. If the audit reveals
the counts are misleading (e.g. one scope shows zero usage but is actually
load-bearing for an edge case), instrument the call site directly.

Stdlib only.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import sys

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from log_ts import parse_log_ts  # noqa: E402  (shared log-ts parser)

AGENTS_ROOT = Path('/home/larry/agents')
LOG_DIR = AGENTS_ROOT / 'logs'

OUTBOX_NOTIFIER_LOG = LOG_DIR / 'outbox-notifier.log'
HEAL_PR_AUTO_MERGE_LOG = LOG_DIR / 'heal-pr-auto-merge.log'
INBOX_WATCHER_LOG = LOG_DIR / 'inbox-watcher.log'

# gh CLI subcommand → required OAuth scope. Coarse but accurate enough for
# audit triage. Built from gh's documented scope requirements + observed
# subcommands in this repo's healers/notifier/agent code.
_GH_SUBCMD_TO_SCOPE: dict[str, str] = {
    'pr': 'repo',
    'repo': 'repo',
    'run': 'workflow',
    'workflow': 'workflow',
    'gist': 'gist',
    'release': 'repo',
    'issue': 'repo',
    'label': 'repo',
}

# `gh api <endpoint>` — endpoint prefix → scope.
_GH_API_ENDPOINT_TO_SCOPE: tuple[tuple[str, str], ...] = (
    ('repos/', 'repo'),
    ('orgs/', 'read:org'),
    ('gists/', 'gist'),
    ('user/', 'repo'),
)

# workspace-mcp tool name fragments → scope. Observed in agent_runner +
# inbox-watcher logs as `tool=workspace-mcp_<name>` or similar.
_MCP_TOOL_TO_SCOPE: tuple[tuple[str, str], ...] = (
    ('doc', 'docs'),
    ('drive_file', 'drive'),
    ('drive', 'drive'),
    ('gmail', 'gmail'),
    ('calendar', 'calendar'),
)

# Match the `[YYYY-MM-DD...]` prefix, capturing optional microseconds and tz
# offset too. This parser reads THREE logs with DIFFERENT zones:
# outbox-notifier.log is naive host-local (space-separated), while
# inbox-watcher.log and heal-pr-auto-merge.log are aware UTC isoformat
# (`datetime.now(timezone.utc).isoformat()` — T-separated, `+00:00`). Dropping
# the offset would make the naive and aware sources indistinguishable and
# mis-shift one of them ~6h.
_TS_RE = re.compile(
    r'\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)'
)

# `gh <subcommand>` — matches anywhere in the line, captures the subcommand.
_GH_INVOCATION_RE = re.compile(r'\bgh\s+(\w+)')

# `gh api <endpoint>` — captures the endpoint path.
_GH_API_RE = re.compile(r'\bgh\s+api\s+(\S+)')

# workspace-mcp tool call — Beacon's agent_runner emits these as
# `tool=workspace-mcp_<name>` per inbox_watcher's journal.
_MCP_TOOL_RE = re.compile(r'tool=workspace-mcp[_-]([a-zA-Z0-9_]+)')


def _parse_ts(line: str) -> Optional[datetime]:
    m = _TS_RE.search(line)
    if not m:
        return None
    # _TS_RE keeps the offset (above) so the three mixed-zone sources stay
    # distinguishable; parse_log_ts then reads the naive outbox-notifier stamp
    # as host-local and the aware inbox-watcher / heal-pr-auto-merge stamps by
    # their own offset — a bare `.replace(tzinfo=utc)` threw the naive one ~6h
    # into the past.
    return parse_log_ts(m.group(1))


def _iter_lines_in_window(
    paths: list[Path], cutoff: datetime,
) -> list[str]:
    """Read each path; return lines whose timestamp is >= cutoff, or lines
    with no parseable timestamp (kept defensively — better to over-count
    than to silently drop). Missing paths are skipped silently."""
    lines: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for raw in f:
                    ts = _parse_ts(raw)
                    if ts is None or ts >= cutoff:
                        lines.append(raw)
        except OSError:
            continue
    return lines


def _scope_for_gh_subcmd(subcmd: str) -> Optional[str]:
    return _GH_SUBCMD_TO_SCOPE.get(subcmd)


def _scope_for_gh_api(endpoint: str) -> Optional[str]:
    endpoint = endpoint.lstrip('/').lstrip('"').lstrip("'")
    for prefix, scope in _GH_API_ENDPOINT_TO_SCOPE:
        if endpoint.startswith(prefix):
            return scope
    return None


def _scope_for_mcp_tool(name: str) -> Optional[str]:
    lower = name.lower()
    for fragment, scope in _MCP_TOOL_TO_SCOPE:
        if fragment in lower:
            return scope
    return None


def _analyze_github_gh_oauth(
    days: int,
    log_paths: Optional[list[Path]] = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Count gh CLI invocations by inferred scope across the lookback window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    paths = log_paths if log_paths is not None else [
        OUTBOX_NOTIFIER_LOG, HEAL_PR_AUTO_MERGE_LOG,
    ]
    counts: dict[str, int] = {}
    for line in _iter_lines_in_window(paths, cutoff):
        # `gh api` is a special case: dispatch by endpoint prefix.
        m_api = _GH_API_RE.search(line)
        if m_api:
            scope = _scope_for_gh_api(m_api.group(1))
            if scope:
                counts[scope] = counts.get(scope, 0) + 1
            continue
        m = _GH_INVOCATION_RE.search(line)
        if not m:
            continue
        scope = _scope_for_gh_subcmd(m.group(1))
        if scope:
            counts[scope] = counts.get(scope, 0) + 1
    return counts


def _analyze_workspace_mcp(
    days: int,
    log_paths: Optional[list[Path]] = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Count workspace-mcp tool invocations by inferred scope."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    paths = log_paths if log_paths is not None else [INBOX_WATCHER_LOG]
    counts: dict[str, int] = {}
    for line in _iter_lines_in_window(paths, cutoff):
        for m in _MCP_TOOL_RE.finditer(line):
            scope = _scope_for_mcp_tool(m.group(1))
            if scope:
                counts[scope] = counts.get(scope, 0) + 1
    return counts


def analyze_scope_usage(
    credential_name: str,
    days: int = 90,
    log_paths: Optional[list[Path]] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, int]]:
    """Return {scope: count} for `credential_name` over the lookback window,
    or None if the credential isn't observable via logs yet.

    Args:
        credential_name: matches a registry entry's `name`. Today only
            GITHUB_GH_OAUTH_TOKEN and GOOGLE_OAUTH_REFRESH_TOKEN return
            counts; VERCEL_TOKEN + CLAUDE_MAX_OAUTH + bot tokens return
            None.
        days: lookback window in days. Default 90.
        log_paths: override for testing — when set, replaces the default
            log file list for this credential's analyzer.
        now: override for testing — anchor the lookback window. Defaults
            to UTC now.

    Returns:
        A dict mapping scope name → invocation count, or None for
        credentials we can't observe via log-parsing.
    """
    if credential_name == 'GITHUB_GH_OAUTH_TOKEN':
        return _analyze_github_gh_oauth(days, log_paths, now)
    if credential_name == 'GOOGLE_OAUTH_REFRESH_TOKEN':
        return _analyze_workspace_mcp(days, log_paths, now)
    # Vercel, Claude Max, all 4 Telegram bot tokens — not observable via
    # current logs. Deferred to dedicated instrumentation if logs prove
    # too fragile.
    return None
