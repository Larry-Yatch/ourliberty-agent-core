#!/usr/bin/env python3
"""dashboard_api.py — read-only droplet status JSON API (E3.1).

A FastAPI service exposing 7 GET endpoints that surface agent OS state for
the upcoming E3.2 Next.js dashboard. Binds to 127.0.0.1:8000; Nginx fronts
it in E3.3. Every endpoint requires the `X-Dashboard-Token` header,
compared in constant time against `DASHBOARD_API_TOKEN` from
`/home/larry/credentials/.env.larry` (loaded via systemd EnvironmentFile).

Design (mirrors `deploy_notifier.py` E2.2 path-isolation pattern):
  - `AGENTS_ROOT` honors `OURLIBERTY_AGENTS_ROOT` env override so the test
    suite redirects filesystem reads to a tmpdir without polluting
    `~/agents/`.
  - Pure `_reader_*` functions per endpoint take `agents_root` + optional
    params and return plain dicts — directly callable from tests without
    spinning up the HTTP layer.
  - Pydantic response models give us free OpenAPI schema + free validation.
  - CORS allows exactly one origin: `https://dashboard.ourliberty.dev`.
    Preview-URL hostnames are handled in E3.2 via a Vercel env-var
    indirection, not by widening CORS here.
  - Auto-docs at `/docs` and `/openapi.json` are gated by the same auth
    dependency.

External (non-stdlib) deps: fastapi, uvicorn[standard]. Installed on the
droplet via `pip3 install --user fastapi 'uvicorn[standard]'` — see
`systemd/INSTALL.md` "Dashboard API (E3.1)" subsection.

Run locally (after pip install):
    DASHBOARD_API_TOKEN=dev uvicorn scripts.dashboard_api:app \\
        --host 127.0.0.1 --port 8000 --log-level info
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.utils import get_authorization_scheme_param  # noqa: F401  # reserved
from pydantic import BaseModel, Field


# ---- AGENTS_ROOT + derived paths (env-overridable for test isolation) ----

def _agents_root() -> Path:
    return Path(os.environ.get(
        'OURLIBERTY_AGENTS_ROOT', '/home/larry/agents',
    ))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _cgroup_base() -> Path:
    """Cgroup v2 directory for the inbox-watcher slice.

    Env-overridable so tests can point at a synthetic tree without
    touching /sys/fs/cgroup.
    """
    return Path(os.environ.get(
        'OURLIBERTY_CGROUP_BASE',
        '/sys/fs/cgroup/system.slice/ourliberty-inbox-watcher.service',
    ))


def _worktrees_root() -> Path:
    """Where Forge / Mirror checkout worktrees live. Env-overridable for tests."""
    return Path(os.environ.get(
        'OURLIBERTY_WORKTREES_ROOT', '/home/larry/agent-worktrees',
    ))


def _sequence_blackboard_root() -> Path:
    """Multi-step build orchestrator blackboard dir (PR-S2 created it via
    .gitkeep; PR-S3a reads it). Derived from `_agents_root()` so test
    isolation via OURLIBERTY_AGENTS_ROOT covers this endpoint too."""
    return _agents_root() / 'blackboard' / 'build-sequences'


def _missions_json_path() -> Path:
    """Path to the mission registry. Env-overridable so tests redirect
    reads to a tmpdir without touching the real `agents/beacon/missions.json`.
    Defaults to the in-repo path so production reads from the deployed
    checkout's working copy."""
    override = os.environ.get('OURLIBERTY_MISSIONS_JSON')
    if override:
        return Path(override)
    return _repo_root() / 'agents' / 'beacon' / 'missions.json'


def _agent_models_json_path() -> Path:
    """Path to config/agent-models.json (carries the rotation.enabled
    default). Env-overridable so tests redirect reads to a tmpdir without
    touching the deployed checkout."""
    override = os.environ.get('OURLIBERTY_AGENT_MODELS_JSON')
    if override:
        return Path(override)
    return _repo_root() / 'config' / 'agent-models.json'


# ---- agent + healer registries ----

AGENT_NAMES: tuple[str, ...] = ('beacon', 'forge', 'mirror', 'pulse')
# Bot-driven agents have always-on systemd .service units; the other two
# are inbox-watcher-dispatched. Per spec §8: return null for the latter
# and disambiguate via `bot_model`.
BOT_MODEL: dict[str, str] = {
    'beacon': 'systemd-bot',
    'forge': 'systemd-bot',
    'mirror': 'inbox-watcher',
    'pulse': 'inbox-watcher',
}

# Best-effort fallback cadence for healers whose systemd timer can't be
# parsed at request time. Used to compute "stale" (>2× cadence since last
# heartbeat). All values are minutes.
HEALER_CADENCE_MIN_FALLBACK: dict[str, int] = {
    'deploy-notifier': 2,
    'sync-deploy-targets': 12 * 60,
    'heal-pr-auto-merge': 5,
    'heal-credential-registry-drift': 6 * 60,
    'heal-systemd-install-drift': 6 * 60,
    'heal-abandoned-inbox-tasks': 5,
    'heal-blocked-inbox-age': 5,
    'heal-empty-inbox-files': 30,
    'heal-recovery-already-merged': 30,
    'heal-restart-dedup-obsolete': 30,
    'heal-silent-loop-death': 5,
    'heal-zombie-main-workers': 5,
}

# Cap the cycle-journal entry body returned in JSON so a runaway file
# doesn't bloat /cycle-journal/recent responses.
JOURNAL_BODY_CAP_BYTES = 4 * 1024

# Cycle journal header. The real file uses headers like
# `## Iteration 58 — 2026-05-21 00:41 UTC (interactive)`. Be lenient: any
# `## ` H2 with an ISO-ish date or "Iteration N" prefix kicks off a new
# entry. Per spec §8 we surface parse_warnings rather than 500-ing.
JOURNAL_HEADER_RE = re.compile(
    r'^##\s+(?:Iteration\s+\d+\s*[—–-]\s*)?'  # optional "Iteration N — "
    r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?:\s+UTC)?'
    r'(?:\s*\(([^)]*)\))?'  # optional "(interactive)" tag
    r'\s*$',
    re.IGNORECASE,
)

# Headline regex: first ## H2 in the body that looks like a date line is
# treated as the headline (we just take the H2 line itself, stripped).
# In the real journal, the "Found:" / "Did:" sections under each entry
# tend to be bold-labeled bullets; we surface the iteration tag as
# headline for now and let the UI render body_markdown.

# /api/system/build-sequences classification (PR-S3a). Spec § 5.1 lists
# six sequence-level status values: pending, active, paused, complete,
# failed, archived. Beacon's 2026-05-27 CLARIFY locked: {active, paused}
# → active panel and {complete, failed} → archived panel. We extend
# faithfully to cover the other two values (pending → active panel;
# archived → archived panel) so no data is dropped on the floor.
SEQUENCE_STATUS_ARCHIVED_VALUES: frozenset[str] = frozenset({
    'complete', 'failed', 'archived',
})
# Archive subdirs are named `YYYY-MM` per spec § 5.1. Anything else under
# .archive/ is ignored so stray scratch dirs can't pollute the response.
SEQUENCE_ARCHIVE_YYYY_MM_RE = re.compile(r'^\d{4}-(?:0[1-9]|1[0-2])$')

# Auth.
HEADER_NAME = 'X-Dashboard-Token'
TOKEN_ENV = 'DASHBOARD_API_TOKEN'

# E4.4e PR-B2: Larry action endpoint auth. `X-Actor` carries the
# dashboard-authenticated user's email (set by the Next.js Route Handler
# AFTER Supabase Google OAuth). The droplet validates against this
# hardcoded allowlist (option A per spec § 6.4 — single-Larry V1 scope).
HEADER_ACTOR = 'X-Actor'
LARRY_ACTION_ALLOWED_EMAILS: frozenset[str] = frozenset({
    'larry@sealteamleaders.com',
})

# Per spec § 7.3 — path-injection guard for envelope writes. Both the
# frozenset check AND the resolve-prefix check fire on every envelope.
ALLOWED_TARGET_AGENTS: frozenset[str] = frozenset({
    'beacon', 'forge', 'mirror', 'pulse',
})
LARRY_ACTION_VALID_ACTIONS: frozenset[str] = frozenset({
    'approve', 'reject', 'comment', 'mark_done',
})

# Account-tier rotation Auto/Off switch (dashboard-rotation-switch-001).
# The live control is a runtime override file — touching ~/agents/
# rotation.disabled forces the scheduler off on its next ~2-min tick,
# exactly like config rotation.enabled=false, but mutates NO tracked file.
# Mirrors the ~/agents/healers.disabled idiom. Two-state only: no force-on.
ROTATION_OVERRIDE_FILE_NAME = 'rotation.disabled'
ROTATION_VALID_MODES: frozenset[str] = frozenset({'auto', 'off'})

# Approvals-queue-rework N1 (L8): the agent-reviewed "clean up" button.
# POST /api/larry/cleanup-review runs the SAME triage as
# scripts/triage_decisions.py (no fork), auto-clears confirmed-stale rows
# (backup-first), and surfaces still-live items with a reason each. Low-
# confidence (UNCERTAIN) rows escalate to a verification subagent rather
# than being guessed — we NEVER clear an item we cannot confirm resolved.
CLEANUP_REVIEW_VERIFY_MODEL = 'claude-sonnet-4-6'
CLEANUP_REVIEW_VERIFY_TIMEOUT_S = 120

# CORS.
CORS_ORIGIN = 'https://dashboard.ourliberty.dev'

# Limits.
TASKS_RECENT_MAX = 100
CYCLE_JOURNAL_MAX_N = 50

# Healer log scan window (tail count). 200 lines is enough to see the
# last few WARN/ERROR markers for cheap classification.
HEALER_LOG_TAIL_LINES = 200

# subprocess timeouts. Short — these run on every /agents/status hit.
SYSTEMCTL_TIMEOUT_S = 5.0


# ---- Pydantic response models ----

class HealthResponse(BaseModel):
    status: str
    version: str
    agents_root: str
    timestamp: str


class AgentStatus(BaseModel):
    name: str
    bot_active: Optional[bool]
    bot_model: str
    in_flight_count: int
    in_flight_task_ids: list[str]
    last_activity_at: Optional[str]
    last_outbox_archive_at: Optional[str]


class AgentsStatusResponse(BaseModel):
    agents: list[AgentStatus]
    as_of: str


class TaskRecent(BaseModel):
    task_id: str
    agent: Optional[str]
    spec_summary: str
    outcome: str
    pr_url: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_seconds: Optional[float]
    cost_usd: Optional[float]


class TasksRecentResponse(BaseModel):
    tasks: list[TaskRecent]
    limit: int
    returned: int
    as_of: str


class CostsTodayResponse(BaseModel):
    date_utc: str
    total_usd: float
    by_agent: dict[str, float]
    task_count: int
    as_of: str


class CostsByDay(BaseModel):
    date_utc: str
    total_usd: float
    task_count: int


class CostsWeekResponse(BaseModel):
    window_start_utc: str
    window_end_utc: str
    total_usd: float
    by_day: list[CostsByDay]
    by_agent: dict[str, float]
    task_count: int
    as_of: str


class CycleEntry(BaseModel):
    started_at: Optional[str]
    headline: str
    findings_count: int
    body_markdown: str


class CycleJournalResponse(BaseModel):
    entries: list[CycleEntry]
    n: int
    returned: int
    as_of: str
    parse_warnings: list[str] = Field(default_factory=list)


class HealerStatus(BaseModel):
    name: str
    last_run_at: Optional[str]
    last_result: str
    last_summary: str
    next_scheduled_at: Optional[str]
    kill_switch_active: bool


class HealersStatusResponse(BaseModel):
    healers: list[HealerStatus]
    as_of: str


# ---- /api/system/* response models (E4.4d PR-C) ----

class SystemActiveSession(BaseModel):
    pid: int
    agent: Optional[str]
    task_id: Optional[str]
    task_type: Optional[str]
    model: Optional[str]
    started_at: Optional[str]
    duration_sec: Optional[float]


class SystemActiveSessionsResponse(BaseModel):
    captured_at: str
    sessions: list[SystemActiveSession]


class SystemCgroupStatsResponse(BaseModel):
    captured_at: str
    memory_current_bytes: int
    memory_peak_bytes: int
    memory_max_bytes: Optional[int]
    memory_high_bytes: Optional[int]
    memory_events_max: int
    memory_events_high: int
    cpu_user_usec: int
    cpu_system_usec: int


class SystemWorktree(BaseModel):
    name: str
    agent: Optional[str]
    task_id: Optional[str]
    branch: Optional[str]
    age_seconds: Optional[float]
    is_in_flight: bool


class SystemWorktreesResponse(BaseModel):
    captured_at: str
    worktrees: list[SystemWorktree]


# ---- /api/system/agent-queue response models (Forge Queue panel, Phase 1) ----
#
# One agent's dispatch lifecycle as four lanes: queued (inbox files not yet
# picked up), building (worktree in-flight), in_review (PR awaiting Mirror),
# done_today (terminal outcomes from today, UTC). See docs/forge-queue-brief.md.

class QueuedItem(BaseModel):
    task_id: str
    waited_seconds: float


class BuildingItem(BaseModel):
    task_id: Optional[str]
    branch: Optional[str]
    age_seconds: Optional[float]


class ReviewItem(BaseModel):
    task_id: str
    pr_url: Optional[str]
    since: Optional[str]


class DoneItem(BaseModel):
    task_id: Optional[str]
    pr_url: Optional[str]
    # outcome is 'merged' | 'changes_requested' | 'failed'; reason carries the
    # raw chain_events event_type (review_pass / review_revision /
    # review_escalate / marker_error / preflight_reject / cost_budget).
    outcome: str
    reason: Optional[str]
    at: Optional[str]


class AgentQueueResponse(BaseModel):
    agent: str
    queued: list[QueuedItem]
    building: list[BuildingItem]
    in_review: list[ReviewItem]
    done_today: list[DoneItem]
    captured_at: str


# ---- /api/system/build-sequences response model (PR-S3a) ----
#
# Spec § 5.6 commits only to "a JSON list of all sequence files + their
# current state." The 2026-05-27 PR-S3a CLARIFY locked the contract as
# {active, archived} with raw sequence-file dicts (no field projection):
# active = files with status in {active, paused, pending} or unknown;
# archived = files with status in {complete, failed, archived} or
# anything under `.archive/YYYY-MM/`. PR-S3b consumes this verbatim.

class BuildSequencesResponse(BaseModel):
    active: list[dict[str, Any]]
    archived: list[dict[str, Any]]
    parse_warnings: list[str] = Field(default_factory=list)
    as_of: str


# ---- /api/system/missions request + response models (E4.4f PR-A) ----
#
# Spec § 5.1 schema: missions.json carries `schema_version` + a `missions`
# array of {id, name, phase, brief, spec_docs, task_ids, repo, created,
# deferred_reason}. The GET response passes the array through verbatim
# (no projection) and adds `last_synced_at` from the file's mtime so the
# dashboard can render "as of N seconds ago" without a separate call.
#
# The POST route opens a GitHub PR via the REST API; per the PR-A
# clarify-response (Beacon 2026-05-28), this avoids touching the shared
# /home/larry/agent-core checkout. Race-safety: an in-process lock
# serializes concurrent POSTs (uvicorn unit runs a single worker per
# systemd/ourliberty-dashboard-api.service), and `POST /git/refs` at
# GitHub is atomic — a duplicate branch returns 422, which we map to 409.

class MissionsResponse(BaseModel):
    missions: list[dict[str, Any]]
    last_synced_at: Optional[str]
    schema_version: Optional[int] = None


class NewMissionRequest(BaseModel):
    name: str = Field(..., min_length=1)
    brief: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    spec_docs: list[str] = Field(default_factory=list)


class NewMissionResponse(BaseModel):
    mission_id: str
    pr_url: str
    branch: str


# ---- /api/larry/* response + request models (E4.4e PR-B2) ----

class LarryActionRequest(BaseModel):
    source_event_id: str
    action: str
    comment: Optional[str] = None


class LarryActionResponse(BaseModel):
    action_event_id: str
    envelope_written: Optional[str]
    target_agent: Optional[str]


class LarryAllowlistResponse(BaseModel):
    allowed_emails: list[str]


class RotationModeResponse(BaseModel):
    # Effective rotation mode the dashboard renders beside kill_switch_active.
    # 'off' when the runtime override file is present OR the config default is
    # disabled; 'auto' only when neither forces it off.
    mode: str
    override_active: bool
    config_enabled: bool
    as_of: str


class RotationModeRequest(BaseModel):
    mode: str


class RotationModeUpdateResponse(BaseModel):
    mode: str
    override_active: bool
    config_enabled: bool
    action_event_id: str
    as_of: str


class CleanupReviewKeptItem(BaseModel):
    task_id: str
    reason: str


class CleanupReviewResponse(BaseModel):
    # task_ids of rows the engine auto-cleared (confirmed-stale / mock /
    # subagent-verified resolved). Deduped — multiple rows can share a id.
    cleared: list[str]
    # Items that still need Larry, each with a one-line reason.
    kept: list[CleanupReviewKeptItem]
    # Path to the pre-clear backup (every clear is reversible via read_at
    # -> NULL from this file). None when nothing was cleared.
    backup_path: Optional[str] = None
    # How many low-confidence rows were handed to the verification subagent.
    uncertain_reviewed: int = 0


# ---- helpers ----

def _now_utc_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _safe_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _git_short_sha() -> str:
    """Best-effort short git sha; 'dev' if not in a git repo."""
    try:
        proc = subprocess.run(
            ['git', '-C', str(_repo_root()), 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 'dev'
    if proc.returncode != 0:
        return 'dev'
    sha = proc.stdout.strip()
    return sha or 'dev'


def _systemctl_is_active(unit: str, timeout: float = SYSTEMCTL_TIMEOUT_S) -> Optional[bool]:
    """Return True/False, or None if systemctl isn't callable / errored."""
    try:
        proc = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    # Exit codes: 0=active, 3=inactive/failed/unknown. We treat any non-0
    # as "not active" rather than None unless the call itself blew up.
    out = proc.stdout.strip()
    if out == 'active':
        return True
    if out in ('inactive', 'failed', 'activating', 'deactivating', 'unknown'):
        return False
    # Defensive: unfamiliar output → unknown.
    return None


def _list_timer_next(unit: str, timeout: float = SYSTEMCTL_TIMEOUT_S) -> Optional[str]:
    """Return the next-scheduled ISO timestamp for `ourliberty-<unit>.timer`, or None."""
    timer_unit = unit if unit.endswith('.timer') else f'{unit}.timer'
    try:
        proc = subprocess.run(
            [
                'systemctl', 'list-timers', timer_unit,
                '--no-pager', '--no-legend',
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ''
    if not line:
        return None
    # `list-timers --no-legend` columns: NEXT LEFT LAST PASSED UNIT ACTIVATES
    # NEXT is typically `Wed 2026-05-21 02:00:00 UTC` (4 tokens). Best-effort.
    parts = line.split()
    if len(parts) < 4:
        return None
    nxt = ' '.join(parts[1:4])  # date + time + tz
    try:
        # systemd typically uses "YYYY-MM-DD HH:MM:SS TZ"
        dt = datetime.strptime(nxt, '%Y-%m-%d %H:%M:%S %Z')
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).isoformat()
    except ValueError:
        # Fall through; some locales prefix a weekday.
        try:
            stripped = ' '.join(parts[2:5]) if parts[1].endswith(parts[1][-4:]) else nxt
            dt = datetime.strptime(stripped, '%Y-%m-%d %H:%M:%S %Z')
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).isoformat()
        except ValueError:
            return None


# ---- auth dependency ----

def _expected_token() -> Optional[str]:
    """Read the expected token at request time so a service restart can
    pick up a rotated token without rebuilding the app object."""
    tok = os.environ.get(TOKEN_ENV, '').strip()
    return tok or None


def _require_token(request: Request) -> str:
    provided = request.headers.get(HEADER_NAME)
    if provided is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f'missing {HEADER_NAME}',
        )
    expected = _expected_token()
    if not expected:
        # Server misconfigured — refuse to claim auth passed.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f'invalid {HEADER_NAME}',
        )
    # Constant-time compare. Encode to bytes to avoid the early-exit
    # ascii-only short-circuit some Python builds had pre-3.7.
    if not secrets.compare_digest(provided.encode('utf-8'), expected.encode('utf-8')):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f'invalid {HEADER_NAME}',
        )
    return provided


def _require_actor(request: Request) -> str:
    """Validate `X-Actor` against the hardcoded allowlist.

    Per spec § 6.3 + § 6.4 (option A): the dashboard sets `X-Actor` to the
    Supabase-authenticated user's email; the droplet refuses anything not
    on `LARRY_ACTION_ALLOWED_EMAILS`. Errors are deliberately generic ('
    unauthorized') so we never echo the rejected actor value back to the
    caller — a 401 body shouldn't be a confirmed-email oracle.
    """
    provided = request.headers.get(HEADER_ACTOR)
    if not provided or provided not in LARRY_ACTION_ALLOWED_EMAILS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='unauthorized',
        )
    return provided


# ---- pure readers (no FastAPI in signatures) ----

def _reader_health(agents_root: Path, now: Optional[datetime] = None) -> dict[str, Any]:
    return {
        'status': 'ok',
        'version': _git_short_sha(),
        'agents_root': str(agents_root),
        'timestamp': _now_utc_iso(now),
    }


def _agent_inbox_pending(agents_root: Path, agent: str) -> tuple[int, list[str]]:
    """Return (count, sorted-task-ids) of pending inbox tasks for `agent`."""
    inbox = agents_root / 'inboxes' / agent
    if not inbox.is_dir():
        return 0, []
    task_ids: list[str] = []
    try:
        for entry in inbox.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith('.') or not name.endswith('.json'):
                continue
            task_ids.append(name[:-len('.json')])
    except OSError:
        return 0, []
    task_ids.sort()
    return len(task_ids), task_ids


def _agent_archive_mtimes(agents_root: Path, agent: str) -> tuple[Optional[float], Optional[float]]:
    """Return (last_activity_mtime, last_outbox_archive_mtime).

    last_activity_mtime = max(outbox/.archive/*.json + inbox/.archive/*.json)
    last_outbox_archive_mtime = max(outbox/.archive/*.json)
    """
    outbox_archive = agents_root / 'outboxes' / agent / '.archive'
    inbox_archive = agents_root / 'inboxes' / agent / '.archive'
    outbox_max: Optional[float] = None
    inbox_max: Optional[float] = None
    if outbox_archive.is_dir():
        try:
            for f in outbox_archive.iterdir():
                if f.is_file() and f.name.endswith('.json'):
                    mt = _safe_mtime(f)
                    if mt is not None and (outbox_max is None or mt > outbox_max):
                        outbox_max = mt
        except OSError:
            pass
    if inbox_archive.is_dir():
        try:
            for f in inbox_archive.iterdir():
                if f.is_file() and f.name.endswith('.json'):
                    mt = _safe_mtime(f)
                    if mt is not None and (inbox_max is None or mt > inbox_max):
                        inbox_max = mt
        except OSError:
            pass
    candidates = [m for m in (outbox_max, inbox_max) if m is not None]
    last_activity = max(candidates) if candidates else None
    return last_activity, outbox_max


def _reader_agents_status(
    agents_root: Path, now: Optional[datetime] = None,
    is_active_fn=None,
) -> dict[str, Any]:
    is_active = is_active_fn or _systemctl_is_active
    agents: list[dict[str, Any]] = []
    for name in AGENT_NAMES:
        model = BOT_MODEL[name]
        if model == 'systemd-bot':
            bot_active = is_active(f'ourliberty-{name}-bot.service')
        else:
            bot_active = None
        count, ids = _agent_inbox_pending(agents_root, name)
        last_act, last_outbox = _agent_archive_mtimes(agents_root, name)
        agents.append({
            'name': name,
            'bot_active': bot_active,
            'bot_model': model,
            'in_flight_count': count,
            'in_flight_task_ids': ids,
            'last_activity_at': _iso(last_act),
            'last_outbox_archive_at': _iso(last_outbox),
        })
    return {'agents': agents, 'as_of': _now_utc_iso(now)}


# ---- costs.jsonl + outbox-archive readers ----

def _load_costs_jsonl(agents_root: Path) -> list[dict[str, Any]]:
    """Parse every line of costs.jsonl; silently skip malformed lines."""
    path = agents_root / 'blackboard' / 'costs.jsonl'
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open('r') as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows


def _ts_to_dt(ts_str: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts_str, str):
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _scan_outbox_archive_by_task(agents_root: Path) -> dict[str, dict[str, Any]]:
    """For each archived outbox task, return the most-recent result file
    keyed by task_id. Used to fill in outcome / pr_url / completed_at on
    /tasks/recent."""
    by_task: dict[str, dict[str, Any]] = {}
    for agent in AGENT_NAMES:
        archive = agents_root / 'outboxes' / agent / '.archive'
        if not archive.is_dir():
            continue
        try:
            for f in archive.iterdir():
                if not f.is_file() or not f.name.endswith('.json'):
                    continue
                mt = _safe_mtime(f)
                if mt is None:
                    continue
                # Filename shape: <task_id>.json or <task_id>.<n>.json (re-runs).
                stem = f.name[:-len('.json')]
                # Drop trailing .<digit> suffix on duplicates.
                if '.' in stem:
                    base, _, tail = stem.rpartition('.')
                    if tail.isdigit():
                        stem = base
                task_id = stem
                try:
                    payload = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    payload = {}
                prev = by_task.get(task_id)
                if prev is None or mt > prev['__mtime']:
                    by_task[task_id] = {
                        '__mtime': mt,
                        'agent': agent,
                        'payload': payload if isinstance(payload, dict) else {},
                    }
        except OSError:
            continue
    return by_task


def _outcome_from_outbox(payload: dict[str, Any]) -> str:
    """Best-effort outcome classification from an outbox archive payload."""
    # Forge outbox shapes: {"intent": "build-result", "status": "SUCCESS", ...}
    # Mirror outbox shapes carry a `result_marker` field with REVIEW_PASS etc.
    marker = payload.get('result_marker') or payload.get('marker')
    if isinstance(marker, str):
        m = marker.upper()
        if 'REVIEW_PASS' in m:
            return 'review_pass'
        if 'REVIEW_REVISION' in m:
            return 'review_revision'
        if 'REVIEW_ESCALATE' in m:
            return 'review_escalate'
        if 'REVIEW_EMERGENCY_HALT' in m or 'EMERGENCY_HALT' in m:
            return 'review_emergency_halt'
    intent = payload.get('intent')
    if isinstance(intent, str):
        i = intent.lower()
        if i == 'review-pass':
            return 'review_pass'
        if i == 'review-revision':
            return 'review_revision'
        if i == 'review-escalate':
            return 'review_escalate'
        if i == 'review-emergency-halt':
            return 'review_emergency_halt'
    return 'unknown'


def _pr_url_from_outbox(payload: dict[str, Any]) -> Optional[str]:
    """Pull a github PR URL out of a payload if one is reachable."""
    for key in ('pr_url', 'url'):
        v = payload.get(key)
        if isinstance(v, str) and v.startswith('https://github.com/'):
            return v
    summary = payload.get('summary')
    if isinstance(summary, str):
        m = re.search(r'https://github\.com/[^\s)]+', summary)
        if m:
            return m.group(0)
    return None


def _reader_tasks_recent(
    agents_root: Path, limit: int = 20, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return up to `limit` most-recent tasks, joined across costs.jsonl
    (cost + duration + agent) and outbox archives (outcome + pr_url + completion).

    `in_flight` tasks (present in inboxes but not yet archived) are
    surfaced with cost_usd=None, completed_at=None.
    """
    costs = _load_costs_jsonl(agents_root)

    # Aggregate costs per task_id — sum costs and durations across multiple
    # cycle entries; pick earliest ts as started_at; pick agent from most
    # recent entry; tolerate missing fields.
    by_task: dict[str, dict[str, Any]] = {}
    for row in costs:
        tid = row.get('task_id')
        if not isinstance(tid, str) or not tid:
            continue
        dt = _ts_to_dt(row.get('ts'))
        cost = row.get('cost_usd')
        cost_v = float(cost) if isinstance(cost, (int, float)) else 0.0
        dur = row.get('duration_sec')
        dur_v = float(dur) if isinstance(dur, (int, float)) else 0.0
        agent = row.get('agent') if isinstance(row.get('agent'), str) else None
        prev = by_task.get(tid)
        if prev is None:
            by_task[tid] = {
                'task_id': tid,
                'agent': agent,
                'cost_usd': cost_v,
                'duration_seconds': dur_v,
                'started_at': dt,
                'completed_at': dt,
            }
        else:
            prev['cost_usd'] += cost_v
            prev['duration_seconds'] += dur_v
            if agent is not None:
                prev['agent'] = agent
            if dt is not None:
                if prev['started_at'] is None or dt < prev['started_at']:
                    prev['started_at'] = dt
                if prev['completed_at'] is None or dt > prev['completed_at']:
                    prev['completed_at'] = dt

    archive_idx = _scan_outbox_archive_by_task(agents_root)

    # Build in-flight set: inbox entries not yet archived.
    in_flight: dict[str, str] = {}
    for agent in AGENT_NAMES:
        count, ids = _agent_inbox_pending(agents_root, agent)
        for tid in ids:
            if tid not in archive_idx:
                in_flight[tid] = agent

    rows: list[TaskRecent] = []
    for tid, c in by_task.items():
        outbox = archive_idx.get(tid)
        if outbox is not None:
            outcome = _outcome_from_outbox(outbox['payload'])
            pr_url = _pr_url_from_outbox(outbox['payload'])
            completed_dt = datetime.fromtimestamp(outbox['__mtime'], tz=timezone.utc)
            spec_summary = ''
            payload = outbox['payload']
            if isinstance(payload.get('summary'), str):
                spec_summary = payload['summary'][:200]
            elif isinstance(payload.get('intent'), str):
                spec_summary = payload['intent']
            agent = outbox['agent'] or c['agent']
        elif tid in in_flight:
            outcome = 'in_flight'
            pr_url = None
            completed_dt = None
            spec_summary = ''
            agent = in_flight[tid] or c['agent']
        else:
            outcome = 'unknown'
            pr_url = None
            completed_dt = c['completed_at']
            spec_summary = ''
            agent = c['agent']
        rows.append(TaskRecent(
            task_id=tid,
            agent=agent,
            spec_summary=spec_summary,
            outcome=outcome,
            pr_url=pr_url,
            started_at=c['started_at'].isoformat() if c['started_at'] else None,
            completed_at=completed_dt.isoformat() if completed_dt else None,
            duration_seconds=c['duration_seconds'] if c['duration_seconds'] > 0 else None,
            cost_usd=c['cost_usd'] if c['cost_usd'] > 0 else None,
        ))

    # Surface in-flight tasks that have no cost rows yet (newly dispatched).
    for tid, agent in in_flight.items():
        if tid in by_task:
            continue
        rows.append(TaskRecent(
            task_id=tid,
            agent=agent,
            spec_summary='',
            outcome='in_flight',
            pr_url=None,
            started_at=None,
            completed_at=None,
            duration_seconds=None,
            cost_usd=None,
        ))

    # Most-recent first by completed_at (fallback to started_at, then task_id).
    def _sort_key(r: TaskRecent) -> tuple[int, str]:
        ts = r.completed_at or r.started_at or ''
        return (1 if ts else 0, ts)
    rows.sort(key=_sort_key, reverse=True)
    truncated = rows[:limit]

    return {
        'tasks': [r.model_dump() for r in truncated],
        'limit': limit,
        'returned': len(truncated),
        'as_of': _now_utc_iso(now),
    }


def _reader_costs_today(
    agents_root: Path, now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    total = 0.0
    by_agent: dict[str, float] = {}
    task_ids: set[str] = set()
    for row in _load_costs_jsonl(agents_root):
        dt = _ts_to_dt(row.get('ts'))
        if dt is None:
            continue
        if dt.astimezone(timezone.utc).date() != today:
            continue
        cost = row.get('cost_usd')
        if isinstance(cost, (int, float)):
            total += float(cost)
            agent = row.get('agent')
            if isinstance(agent, str):
                by_agent[agent] = by_agent.get(agent, 0.0) + float(cost)
        tid = row.get('task_id')
        if isinstance(tid, str) and tid:
            task_ids.add(tid)
    return {
        'date_utc': today.isoformat(),
        'total_usd': round(total, 4),
        'by_agent': {k: round(v, 4) for k, v in by_agent.items()},
        'task_count': len(task_ids),
        'as_of': _now_utc_iso(now),
    }


def _reader_costs_week(
    agents_root: Path, now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    window_start = today - timedelta(days=6)
    # Initialize buckets so empty days still appear.
    by_day_buckets: dict[date, dict[str, Any]] = {}
    for i in range(7):
        d = window_start + timedelta(days=i)
        by_day_buckets[d] = {'total_usd': 0.0, 'task_ids': set()}
    total = 0.0
    by_agent: dict[str, float] = {}
    task_ids: set[str] = set()
    for row in _load_costs_jsonl(agents_root):
        dt = _ts_to_dt(row.get('ts'))
        if dt is None:
            continue
        d = dt.astimezone(timezone.utc).date()
        if d < window_start or d > today:
            continue
        cost = row.get('cost_usd')
        if isinstance(cost, (int, float)):
            total += float(cost)
            by_day_buckets[d]['total_usd'] += float(cost)
            agent = row.get('agent')
            if isinstance(agent, str):
                by_agent[agent] = by_agent.get(agent, 0.0) + float(cost)
        tid = row.get('task_id')
        if isinstance(tid, str) and tid:
            task_ids.add(tid)
            by_day_buckets[d]['task_ids'].add(tid)
    by_day = [
        {
            'date_utc': d.isoformat(),
            'total_usd': round(buckets['total_usd'], 4),
            'task_count': len(buckets['task_ids']),
        }
        for d, buckets in sorted(by_day_buckets.items())
    ]
    return {
        'window_start_utc': window_start.isoformat(),
        'window_end_utc': today.isoformat(),
        'total_usd': round(total, 4),
        'by_day': by_day,
        'by_agent': {k: round(v, 4) for k, v in by_agent.items()},
        'task_count': len(task_ids),
        'as_of': _now_utc_iso(now),
    }


# ---- cycle journal reader ----

def _reader_cycle_journal(
    agents_root: Path, n: int = 5, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Parse the cycle journal at `<repo>/runbooks/cycle-journal.md` and
    return the most-recent N entries. Lenient parser: surface
    parse_warnings rather than 500-ing."""
    path = _repo_root() / 'runbooks' / 'cycle-journal.md'
    warnings: list[str] = []
    if not path.exists():
        return {
            'entries': [],
            'n': n,
            'returned': 0,
            'as_of': _now_utc_iso(now),
            'parse_warnings': [f'cycle journal not found at {path}'],
        }
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        return {
            'entries': [],
            'n': n,
            'returned': 0,
            'as_of': _now_utc_iso(now),
            'parse_warnings': [f'read error: {e}'],
        }
    lines = text.splitlines()
    entries: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for raw in lines:
        m = JOURNAL_HEADER_RE.match(raw)
        if m:
            if current is not None:
                entries.append(current)
            date_part = m.group(1)
            time_part = m.group(2)
            try:
                started = datetime.strptime(
                    f'{date_part} {time_part}', '%Y-%m-%d %H:%M',
                ).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                started = None
                warnings.append(f'unparseable date in header: {raw!r}')
            current = {
                'started_at': started,
                'headline': raw.strip().lstrip('#').strip(),
                'findings_count': 0,
                '_body': [raw],
            }
            continue
        if current is None:
            # Pre-amble before any header — ignore.
            continue
        current['_body'].append(raw)
        # Count findings: lines matching "**(X) ...:**" or "- **(X)" patterns
        # used in the actual journal.
        if re.match(r'\s*-?\s*\*\*\([A-Z]\)\s', raw):
            current['findings_count'] += 1
    if current is not None:
        entries.append(current)
    # Entries are in file order (most-recent-first if file is newest-on-top,
    # which it is — see runbooks/cycle-journal.md).
    n_capped = max(0, min(int(n), CYCLE_JOURNAL_MAX_N))
    truncated = entries[:n_capped]
    out_entries: list[dict[str, Any]] = []
    for e in truncated:
        body = '\n'.join(e['_body'])
        if len(body.encode('utf-8')) > JOURNAL_BODY_CAP_BYTES:
            # Truncate by bytes; back off if mid-multibyte char.
            encoded = body.encode('utf-8')[:JOURNAL_BODY_CAP_BYTES]
            body = encoded.decode('utf-8', errors='ignore') + '\n…[truncated]'
        out_entries.append({
            'started_at': e['started_at'],
            'headline': e['headline'],
            'findings_count': e['findings_count'],
            'body_markdown': body,
        })
    return {
        'entries': out_entries,
        'n': n_capped,
        'returned': len(out_entries),
        'as_of': _now_utc_iso(now),
        'parse_warnings': warnings,
    }


# ---- healers reader ----

def _classify_healer_log(log_path: Path, tail: int = HEALER_LOG_TAIL_LINES) -> tuple[str, str]:
    """Return (last_result, last_summary). Reads up to `tail` last lines."""
    if not log_path.exists():
        return 'stale', 'log file not found'
    try:
        with log_path.open('rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk_size = min(size, 64 * 1024)
            f.seek(max(0, size - chunk_size))
            data = f.read().decode('utf-8', errors='replace')
    except OSError as e:
        return 'error', f'log read failed: {e}'
    last_lines = data.splitlines()[-tail:]
    if not last_lines:
        return 'stale', 'log empty'
    has_error = any('ERROR' in ln for ln in last_lines)
    has_warn = any('WARN' in ln for ln in last_lines)
    last_summary = last_lines[-1].strip()
    if len(last_summary) > 200:
        last_summary = last_summary[:197] + '...'
    if has_error:
        return 'error', last_summary
    if has_warn:
        return 'warn', last_summary
    return 'ok', last_summary


def _reader_healers_status(
    agents_root: Path, now: Optional[datetime] = None,
    list_timer_fn=None,
) -> dict[str, Any]:
    list_timer = list_timer_fn or _list_timer_next
    now = now or datetime.now(timezone.utc)
    blackboard = agents_root / 'blackboard'
    kill_switch_active = (agents_root / 'healers.disabled').exists()
    healers: list[dict[str, Any]] = []
    if not blackboard.is_dir():
        return {'healers': healers, 'as_of': _now_utc_iso(now)}
    try:
        heartbeats = sorted(
            f for f in blackboard.iterdir()
            if f.is_file() and f.name.endswith('.heartbeat')
        )
    except OSError:
        heartbeats = []
    for hb in heartbeats:
        name = hb.name[:-len('.heartbeat')]
        last_run_mt = _safe_mtime(hb)
        last_run_at = _iso(last_run_mt)
        log_path = agents_root / 'logs' / f'{name}.log'
        last_result, last_summary = _classify_healer_log(log_path)
        # Staleness: heartbeat older than 2× expected cadence.
        cadence_min = HEALER_CADENCE_MIN_FALLBACK.get(name)
        if last_run_mt is not None and cadence_min:
            age_sec = (now.timestamp() - last_run_mt)
            if age_sec > (cadence_min * 60 * 2):
                last_result = 'stale'
                last_summary = (
                    f'last heartbeat {int(age_sec // 60)}min ago; '
                    f'expected cadence {cadence_min}min'
                )
        next_at = list_timer(f'ourliberty-{name}')
        healers.append({
            'name': name,
            'last_run_at': last_run_at,
            'last_result': last_result,
            'last_summary': last_summary,
            'next_scheduled_at': next_at,
            'kill_switch_active': kill_switch_active,
        })
    return {'healers': healers, 'as_of': _now_utc_iso(now)}


# ---- /api/system/* readers (E4.4d PR-C) ----
#
# Locked decision-C: droplet API returns RAW signals only. No `stuck` /
# `stuck_reason` booleans; the dashboard route handler at
# /api/operations/stuck-sessions joins this output with chain_events and
# thresholds (config/system_tab_thresholds.json) to compute stuck-state.
# All three readers are uncached: each request re-reads the filesystem.

# Cgroup files map directly to documented systemd cgroup v2 attributes.
# `memory.max` and `memory.high` can be the literal string "max" when no
# limit is configured — we surface that as None rather than coercing.


def _parse_kv_file(text: str) -> dict[str, str]:
    """Parse a key-value file (one `<key> <value>` pair per line)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def _read_cgroup_int(base: Path, name: str) -> int:
    """Read a single-int cgroup file. FileNotFoundError surfaces to caller."""
    return int((base / name).read_text().strip())


def _read_cgroup_int_or_none(base: Path, name: str) -> Optional[int]:
    """Like `_read_cgroup_int` but tolerates the literal `max` sentinel
    used for unlimited memory.max / memory.high."""
    raw = (base / name).read_text().strip()
    if raw == 'max':
        return None
    return int(raw)


def _read_proc_cmdline(pid: int) -> Optional[str]:
    """Read /proc/<pid>/cmdline as a space-joined string. Returns None if
    the process has died mid-read or the file is unreadable."""
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    if not raw:
        return None
    # cmdline is NUL-separated; the last byte is typically also NUL.
    parts = raw.split(b'\x00')
    return ' '.join(p.decode('utf-8', errors='replace') for p in parts if p)


# Worktree dir names follow the pattern `wt-<agent>-<task_id>`. Strict regex
# so we don't mis-parse a stray directory.
_WORKTREE_RE = re.compile(r'^wt-(?P<agent>[a-z]+)-(?P<task_id>[a-z0-9][a-z0-9-]*)$')


def _parse_worktree_name(name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (agent, task_id, branch) from a worktree dirname.

    Branch follows the existing convention `<agent>/<task_id>`. Returns
    (None, None, None) for directories that don't match the pattern.
    """
    m = _WORKTREE_RE.match(name)
    if not m:
        return None, None, None
    agent = m.group('agent')
    task_id = m.group('task_id')
    return agent, task_id, f'{agent}/{task_id}'


def _load_in_flight_index(agents_root: Path) -> dict[str, dict[str, Any]]:
    """Index `~/agents/state/in-flight/*.json` by task_stem.

    Each file is the dispatch-sentinel envelope: `{task_stem, agent_id,
    pid, started_at}`. Tolerates missing dir, bad JSON, and races where a
    file vanishes mid-iteration.
    """
    out: dict[str, dict[str, Any]] = {}
    in_flight_dir = agents_root / 'state' / 'in-flight'
    if not in_flight_dir.is_dir():
        return out
    try:
        entries = list(in_flight_dir.iterdir())
    except OSError:
        return out
    for f in entries:
        if not f.is_file() or not f.name.endswith('.json'):
            continue
        try:
            payload = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        stem = payload.get('task_stem')
        if isinstance(stem, str) and stem:
            out[stem] = payload
    return out


def _detect_model_from_cmdline(cmdline: Optional[str]) -> Optional[str]:
    """Best-effort: pull `--model <name>` or `claude-<family>` token out
    of cmdline. Returns None if no clean match. Pure read, no shell."""
    if not cmdline:
        return None
    m = re.search(r'--model[= ]([\w.-]+)', cmdline)
    if m:
        return m.group(1)
    m = re.search(r'\b(claude-(?:opus|sonnet|haiku)-[\w.-]+)\b', cmdline)
    if m:
        return m.group(1)
    return None


def _reader_system_active_sessions(
    agents_root: Path,
    cgroup_base: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Read cgroup.procs for the inbox-watcher slice, cross-reference
    each PID against the in-flight registry, return one row per session.

    Raw signals only — no stuck/stuck_reason (computed dashboard-side per
    locked decision-C / spec § 5.5).

    Tolerates:
      - Slice not running → raises FileNotFoundError so the route
        returns 503 with a structured body.
      - PID dying between cgroup.procs read and /proc/<pid>/cmdline open
        → that PID is omitted from the response (not a fatal error).
    """
    captured_at = now or datetime.now(timezone.utc)
    procs_text = (cgroup_base / 'cgroup.procs').read_text()
    pids: list[int] = []
    for raw in procs_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            pids.append(int(raw))
        except ValueError:
            continue
    in_flight = _load_in_flight_index(agents_root)
    # Build pid → in-flight entry index for fast lookup.
    in_flight_by_pid: dict[int, dict[str, Any]] = {}
    for stem, entry in in_flight.items():
        pid_v = entry.get('pid')
        if isinstance(pid_v, int):
            in_flight_by_pid[pid_v] = entry
    sessions: list[dict[str, Any]] = []
    for pid in pids:
        cmdline = _read_proc_cmdline(pid)
        entry = in_flight_by_pid.get(pid)
        if entry is None and cmdline is None:
            # Process vanished mid-read AND we have no registry entry —
            # skip it entirely rather than emitting a half-row.
            continue
        agent = entry.get('agent_id') if entry else None
        task_id = entry.get('task_stem') if entry else None
        task_type = entry.get('task_type') if entry else None
        started_at_raw = entry.get('started_at') if entry else None
        started_dt = _ts_to_dt(started_at_raw) if isinstance(started_at_raw, str) else None
        duration_sec: Optional[float] = None
        if started_dt is not None:
            duration_sec = (captured_at - started_dt).total_seconds()
        sessions.append({
            'pid': pid,
            'agent': agent if isinstance(agent, str) else None,
            'task_id': task_id if isinstance(task_id, str) else None,
            'task_type': task_type if isinstance(task_type, str) else None,
            'model': _detect_model_from_cmdline(cmdline),
            'started_at': started_at_raw if isinstance(started_at_raw, str) else None,
            'duration_sec': duration_sec,
        })
    return {
        'captured_at': _now_utc_iso(captured_at),
        'sessions': sessions,
    }


def _reader_system_cgroup_stats(
    cgroup_base: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Read live cgroup memory + cpu numbers. FileNotFoundError surfaces
    to the route for a 503 structured body.

    `memory.events` is a kv file; `cpu.stat` likewise. memory.max /
    memory.high may be the literal `max` sentinel when uncapped — that
    becomes JSON null.
    """
    captured_at = now or datetime.now(timezone.utc)
    memory_current = _read_cgroup_int(cgroup_base, 'memory.current')
    memory_peak = _read_cgroup_int(cgroup_base, 'memory.peak')
    memory_max = _read_cgroup_int_or_none(cgroup_base, 'memory.max')
    memory_high = _read_cgroup_int_or_none(cgroup_base, 'memory.high')
    events_kv = _parse_kv_file((cgroup_base / 'memory.events').read_text())
    cpu_kv = _parse_kv_file((cgroup_base / 'cpu.stat').read_text())
    return {
        'captured_at': _now_utc_iso(captured_at),
        'memory_current_bytes': memory_current,
        'memory_peak_bytes': memory_peak,
        'memory_max_bytes': memory_max,
        'memory_high_bytes': memory_high,
        'memory_events_max': int(events_kv.get('max', '0')),
        'memory_events_high': int(events_kv.get('high', '0')),
        'cpu_user_usec': int(cpu_kv.get('user_usec', '0')),
        'cpu_system_usec': int(cpu_kv.get('system_usec', '0')),
    }


def _reader_system_worktrees(
    agents_root: Path,
    worktrees_root: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """List directories under `worktrees_root`, parse `wt-<agent>-<task_id>`,
    cross-reference in-flight registry.

    Filesystem-only — never shells out to `git worktree list`. Worktree
    names come from filesystem listing, not request input, so there's no
    user-controlled string anywhere near a subprocess.
    """
    captured_at = now or datetime.now(timezone.utc)
    in_flight = _load_in_flight_index(agents_root)
    in_flight_stems = set(in_flight.keys())
    worktrees: list[dict[str, Any]] = []
    if not worktrees_root.is_dir():
        return {
            'captured_at': _now_utc_iso(captured_at),
            'worktrees': worktrees,
        }
    try:
        entries = sorted(worktrees_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return {
            'captured_at': _now_utc_iso(captured_at),
            'worktrees': worktrees,
        }
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        if not name.startswith('wt-'):
            continue
        agent, task_id, branch = _parse_worktree_name(name)
        mt = _safe_mtime(entry)
        age_seconds: Optional[float] = None
        if mt is not None:
            age_seconds = (captured_at - datetime.fromtimestamp(mt, tz=timezone.utc)).total_seconds()
        is_in_flight = task_id in in_flight_stems if task_id else False
        worktrees.append({
            'name': name,
            'agent': agent,
            'task_id': task_id,
            'branch': branch,
            'age_seconds': age_seconds,
            'is_in_flight': is_in_flight,
        })
    return {
        'captured_at': _now_utc_iso(captured_at),
        'worktrees': worktrees,
    }


# ---- /api/system/agent-queue readers (Forge Queue panel, Phase 1) ----
#
# Terminal chain_events that close out a build. auto_merge => merged;
# the rest => failed. Used by BOTH the in_review derivation (a review_request
# with no later terminal event is still awaiting a verdict) and done_today
# (today's terminal events only).
_QUEUE_TERMINAL_EVENT_TYPES = (
    'auto_merge', 'marker_error', 'preflight_reject',
    'cost_budget', 'review_escalate',
)


def _reader_agent_queue_queued(
    agents_root: Path, agent: str, now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """QUEUED lane: inbox dispatches not yet picked up.

    Mirrors `inbox_watcher.scan_inbox`'s matching rule — non-dotfile
    `*.json` — but sorts mtime oldest-first and emits `waited_seconds`
    per item, whereas `_agent_inbox_pending` returns lexically-sorted ids.
    Parameterized on `agents_root` so it stays tmpdir-testable like the
    other dashboard readers. `waited_seconds = now(UTC) - file mtime`.
    """
    now = now or datetime.now(timezone.utc)
    inbox = agents_root / 'inboxes' / agent
    items: list[dict[str, Any]] = []
    if not inbox.is_dir():
        return items
    entries: list[tuple[float, str]] = []
    try:
        for e in os.scandir(inbox):
            if not e.is_file() or e.name.startswith('.') or not e.name.endswith('.json'):
                continue
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            entries.append((mt, e.name))
    except OSError:
        return items
    entries.sort(key=lambda x: x[0])
    for mt, name in entries:
        waited = (now - datetime.fromtimestamp(mt, tz=timezone.utc)).total_seconds()
        items.append({
            'task_id': name[:-len('.json')],
            'waited_seconds': waited,
        })
    return items


def _reader_agent_queue_building(
    agents_root: Path, worktrees_root: Path, agent: str,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """BUILDING lane: reuse the worktrees reader, filtered to this agent's
    in-flight worktrees."""
    wt = _reader_system_worktrees(agents_root, worktrees_root, now=now)
    out: list[dict[str, Any]] = []
    for w in wt['worktrees']:
        if w.get('agent') == agent and w.get('is_in_flight'):
            out.append({
                'task_id': w.get('task_id'),
                'branch': w.get('branch'),
                'age_seconds': w.get('age_seconds'),
            })
    return out


def _fetch_chain_events_for_agent(
    supabase_client: Any, agent: str,
) -> list[dict[str, Any]]:
    """Pull this agent's chain_events rows for the in_review / done_today
    lanes. Returns [] when the client is None (test env / no creds) or on
    any query error — the endpoint degrades to empty review/done lanes
    rather than 500ing."""
    if supabase_client is None:
        return []
    try:
        resp = (
            supabase_client.table('chain_events')
            .select('agent,event_type,task_id,pr_url,ts')
            .eq('agent', agent)
            .execute()
        )
    except Exception:  # noqa: BLE001 — never 500 on a read-only dashboard lane
        return []
    return list(getattr(resp, 'data', None) or [])


def _derive_in_review(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """IN_REVIEW lane: a task whose latest `review_request` has no later
    terminal event for the same task_id. `since` = that review_request ts."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        tid = r.get('task_id')
        if not tid:
            continue
        by_task.setdefault(tid, []).append(r)
    out: list[dict[str, Any]] = []
    for tid, evs in by_task.items():
        reviews = [e for e in evs if e.get('event_type') == 'review_request']
        latest_rr = None
        latest_rr_dt = None
        for e in reviews:
            dt = _ts_to_dt(e.get('ts'))
            if dt is None:
                continue
            if latest_rr_dt is None or dt > latest_rr_dt:
                latest_rr, latest_rr_dt = e, dt
        if latest_rr is None:
            continue
        has_later_terminal = False
        for e in evs:
            if e.get('event_type') not in _QUEUE_TERMINAL_EVENT_TYPES:
                continue
            dt = _ts_to_dt(e.get('ts'))
            if dt is not None and dt > latest_rr_dt:
                has_later_terminal = True
                break
        if has_later_terminal:
            continue
        out.append({
            'task_id': tid,
            'pr_url': latest_rr.get('pr_url'),
            'since': latest_rr.get('ts'),
        })
    out.sort(key=lambda x: x.get('since') or '')
    return out


# Build-completion signals (verified against live chain_events 2026-06-04).
# Mirror — NOT the building agent — emits the review verdicts, carrying the
# build's task_id + pr_url. The building agent emits its own failure markers.
_DONE_FAILURE_EVENT_TYPES = ('marker_error', 'preflight_reject', 'cost_budget')
_DONE_SESSION_EVENT_TYPES = ('session_start', 'session_done')


def _derive_done_today(
    agent_rows: list[dict[str, Any]],
    verdict_rows: list[dict[str, Any]],
    agent: str,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """DONE_TODAY lane: today's build outcomes for `agent` (UTC day boundary,
    per `_reader_costs_today`). Rolling daily window — no storage, self-clears
    at UTC midnight.

    The join: `agent`'s own session_start/session_done events define today's
    taskset. Mirror's review verdicts (`verdict_rows`) are attributed back to
    `agent` via task_id membership in that taskset:
      - review_pass                      => merged
      - review_revision / review_escalate => changes_requested
    Plus `agent`'s own failure markers (agent==<agent> OR task_id in taskset):
      - marker_error / preflight_reject / cost_budget => failed
    `reason` carries the raw event_type. Dedup by task_id keeping the latest
    ts; sort by `at` descending."""
    now = now or datetime.now(timezone.utc)
    today = now.date()

    def today_dt(r: dict[str, Any]) -> Optional[datetime]:
        dt = _ts_to_dt(r.get('ts'))
        if dt is None or dt.astimezone(timezone.utc).date() != today:
            return None
        return dt.astimezone(timezone.utc)

    taskset: set[str] = set()
    for r in agent_rows:
        if (r.get('agent') == agent
                and r.get('event_type') in _DONE_SESSION_EVENT_TYPES):
            tid = r.get('task_id')
            if tid and today_dt(r) is not None:
                taskset.add(tid)

    candidates: list[tuple[datetime, dict[str, Any]]] = []

    def add(r: dict[str, Any], dt: datetime, outcome: str, reason: str) -> None:
        candidates.append((dt, {
            'task_id': r.get('task_id'),
            'pr_url': r.get('pr_url'),
            'outcome': outcome,
            'reason': reason,
            'at': r.get('ts'),
        }))

    for r in verdict_rows:
        tid = r.get('task_id')
        if tid not in taskset:
            continue
        dt = today_dt(r)
        if dt is None:
            continue
        et = r.get('event_type')
        if et == 'review_pass':
            add(r, dt, 'merged', et)
        elif et in ('review_revision', 'review_escalate'):
            add(r, dt, 'changes_requested', et)

    for r in agent_rows:
        et = r.get('event_type')
        if et not in _DONE_FAILURE_EVENT_TYPES:
            continue
        if not (r.get('agent') == agent or r.get('task_id') in taskset):
            continue
        dt = today_dt(r)
        if dt is None:
            continue
        add(r, dt, 'failed', et)

    # Dedup by task_id keeping the latest ts; None task_ids never collapse.
    best: dict[str, tuple[datetime, dict[str, Any]]] = {}
    extras: list[tuple[datetime, dict[str, Any]]] = []
    for dt, item in candidates:
        tid = item['task_id']
        if not tid:
            extras.append((dt, item))
            continue
        if tid not in best or dt > best[tid][0]:
            best[tid] = (dt, item)
    merged = list(best.values()) + extras
    merged.sort(key=lambda x: x[0], reverse=True)
    return [item for _dt, item in merged]


def _reader_agent_queue(
    agents_root: Path,
    worktrees_root: Path,
    agent: str,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Assemble one agent's four-lane dispatch lifecycle.

    queued + building are filesystem-only; in_review + done_today come from
    chain_events and degrade to [] when `supabase_client` is None. done_today
    joins the agent's own session/failure events against Mirror's review
    verdicts (a separate fetch, since Mirror — not the building agent — emits
    them); in_review keeps using only the agent's own rows so its terminal
    detection is unchanged.
    """
    now = now or datetime.now(timezone.utc)
    rows = _fetch_chain_events_for_agent(supabase_client, agent)
    verdict_rows = (
        rows if agent == 'mirror'
        else _fetch_chain_events_for_agent(supabase_client, 'mirror')
    )
    return {
        'agent': agent,
        'queued': _reader_agent_queue_queued(agents_root, agent, now=now),
        'building': _reader_agent_queue_building(
            agents_root, worktrees_root, agent, now=now,
        ),
        'in_review': _derive_in_review(rows),
        'done_today': _derive_done_today(rows, verdict_rows, agent, now=now),
        'captured_at': _now_utc_iso(now),
    }


# ---- /api/system/build-sequences reader (PR-S3a) ----
#
# Spec: agents/beacon/specs/build-sequence-orchestrator.md § 5.6 (panel +
# API endpoint) + § 5.8 (data sources). The reader returns the raw
# sequence-file dicts under {active, archived} keys, partitioned
# server-side by status per the 2026-05-27 CLARIFY contract. Uncached:
# every request re-reads the blackboard dir. The dashboard owns any
# client-side caching.
#
# Failure modes (all graceful, never 500):
#   - blackboard dir missing → {active: [], archived: [], parse_warnings: []}
#   - individual sequence file fails JSON parse → omitted, surfaced in
#     `parse_warnings` (matches the cycle-journal reader convention)
#   - non-`YYYY-MM` subdirs under `.archive/` → ignored
#   - symlinks under either dir → skipped (defense against path traversal
#     even though the dir is hardcoded)
#
# No pagination + no 90d archive-mtime filter in V1 per the 2026-05-27
# CLARIFY (`.archive/` is empty today since the spec-§ 5.1 30-day archiver
# isn't built yet). TODO(PR-S3c): pagination + ?archived_since= once
# archived volume grows.


def _load_sequence_file(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Return (parsed_dict, error_message). On any failure: (None, msg)."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as e:
        return None, f'read failed: {e}'
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f'invalid JSON: {e}'
    if not isinstance(obj, dict):
        return None, 'top-level JSON is not an object'
    return obj, None


def _iter_active_dir_sequence_files(blackboard_root: Path) -> list[Path]:
    """Yield top-level *.json files under the blackboard dir. Skips
    hidden files / dirs (including `.archive/`), symlinks, and
    non-`.json` entries."""
    out: list[Path] = []
    try:
        entries = sorted(blackboard_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.name.startswith('.'):
            continue
        if not entry.is_file():
            continue
        if entry.suffix != '.json':
            continue
        out.append(entry)
    return out


def _iter_archive_sequence_files(archive_root: Path) -> list[Path]:
    """Yield *.json files exactly one level deep under archive_root, only
    inside subdirs named `YYYY-MM`. Skips symlinks at both levels."""
    out: list[Path] = []
    try:
        ym_dirs = sorted(archive_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for ym in ym_dirs:
        if ym.is_symlink() or not ym.is_dir():
            continue
        if not SEQUENCE_ARCHIVE_YYYY_MM_RE.match(ym.name):
            continue
        try:
            files = sorted(ym.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in files:
            if entry.is_symlink():
                continue
            if entry.name.startswith('.'):
                continue
            if not entry.is_file():
                continue
            if entry.suffix != '.json':
                continue
            out.append(entry)
    return out


def _reader_build_sequences(
    blackboard_root: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return the {active, archived, parse_warnings, as_of} response.

    Active = main-dir files with status in {pending, active, paused} or
             missing/unknown status (conservative fallback: surface in
             the operator panel rather than dropping).
    Archived = main-dir files with status in {complete, failed, archived}
               OR any well-formed file under `.archive/YYYY-MM/`.

    See module-level comment above for failure-mode contract.
    """
    active: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    warnings: list[str] = []
    captured_at = now or datetime.now(timezone.utc)

    if not blackboard_root.is_dir():
        return {
            'active': active,
            'archived': archived,
            'parse_warnings': warnings,
            'as_of': _now_utc_iso(captured_at),
        }

    for path in _iter_active_dir_sequence_files(blackboard_root):
        seq, err = _load_sequence_file(path)
        if err is not None or seq is None:
            warnings.append(f'{path.name}: {err}')
            continue
        status = seq.get('status') if isinstance(seq, dict) else None
        if isinstance(status, str) and status in SEQUENCE_STATUS_ARCHIVED_VALUES:
            archived.append(seq)
        else:
            active.append(seq)

    archive_root = blackboard_root / '.archive'
    if archive_root.is_dir():
        for path in _iter_archive_sequence_files(archive_root):
            seq, err = _load_sequence_file(path)
            if err is not None or seq is None:
                # Path includes the YYYY-MM dir so warnings disambiguate
                # same-named files across months.
                warnings.append(f'.archive/{path.parent.name}/{path.name}: {err}')
                continue
            archived.append(seq)

    # TODO(PR-S3c): add `?limit=` + `?offset=` + optional `?archived_since=`
    # once archived volume grows past what the dashboard can render in
    # one poll cycle.
    return {
        'active': active,
        'archived': archived,
        'parse_warnings': warnings,
        'as_of': _now_utc_iso(captured_at),
    }


# ---- /api/system/missions helpers + handler (E4.4f PR-A) ----
#
# GET serves the registry verbatim plus an mtime-derived `last_synced_at`.
# POST opens a PR on the agent-core repo via the GitHub REST API; see the
# request-model comment above for the race-safety contract.

# Module-level lock serializes concurrent POSTs to /api/system/missions/new
# within the single uvicorn worker. Cross-process safety comes from
# GitHub's atomic `POST /git/refs` (duplicate branch → 422 → 409).
_NEW_MISSION_LOCK = __import__('threading').Lock()

_KEBAB_RE = re.compile(r'[^a-z0-9]+')


def _kebab_case(name: str) -> str:
    """Lowercase + collapse non-alphanumerics to single hyphens, strip ends."""
    return _KEBAB_RE.sub('-', name.strip().lower()).strip('-')


def _github_token() -> Optional[str]:
    """Read the GitHub token at request time. Prefer GITHUB_TOKEN
    (loaded from /home/larry/credentials/.env.larry by the systemd unit);
    fall back to GH_TOKEN for parity with gh CLI conventions."""
    tok = (
        os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or ''
    ).strip()
    return tok or None


def _missions_repo_full() -> str:
    """`owner/repo` to open new-mission PRs against. Env-overridable so
    tests don't need to touch the live API."""
    return os.environ.get(
        'OURLIBERTY_MISSIONS_REPO', 'Larry-Yatch/ourliberty-agent-core',
    )


# Test seam: tests monkeypatch this to a recording stub so no live HTTPS
# is made. Real implementation lazy-imports httpx so the module loads on
# hosts without it.
def _github_api_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: float = 10.0,
) -> Any:
    """Perform a single GitHub REST API call. Returns an object with
    `.status_code` and `.json()` (httpx.Response in production)."""
    import httpx  # noqa: PLC0415  # local import keeps cold-start dep optional
    return httpx.request(
        method, url, headers=headers, json=json_body, timeout=timeout,
    )


def _reader_missions(missions_path: Path) -> dict[str, Any]:
    """Return {missions, last_synced_at, schema_version}.

    Missing file → 200 with empty list + null timestamp (defensive default;
    spec acceptance criterion). Malformed JSON raises HTTPException(500)
    with a structured body — never a Flask/FastAPI stack trace.
    """
    if not missions_path.exists():
        return {
            'missions': [],
            'last_synced_at': None,
            'schema_version': None,
        }
    try:
        raw = missions_path.read_text()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as e:
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={'error': 'missions.json malformed', 'detail': first_line},
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'missions.json malformed',
                'detail': 'top-level JSON is not an object',
            },
        )
    missions = data.get('missions')
    if not isinstance(missions, list):
        missions = []
    schema_version = data.get('schema_version')
    if not isinstance(schema_version, int):
        schema_version = None
    return {
        'missions': missions,
        'last_synced_at': _iso(_safe_mtime(missions_path)),
        'schema_version': schema_version,
    }


def _read_missions_registry(missions_path: Path) -> dict[str, Any]:
    """Load the registry as a dict (raw schema), or return a fresh empty
    registry shape if the file is missing. Raises HTTPException(500) on
    malformed JSON — same contract as `_reader_missions` so the POST path
    surfaces parse errors identically."""
    if not missions_path.exists():
        return {'schema_version': 1, 'missions': []}
    try:
        raw = missions_path.read_text()
        data = json.loads(raw) if raw.strip() else {'schema_version': 1, 'missions': []}
    except (OSError, json.JSONDecodeError) as e:
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={'error': 'missions.json malformed', 'detail': first_line},
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'missions.json malformed',
                'detail': 'top-level JSON is not an object',
            },
        )
    if not isinstance(data.get('missions'), list):
        data['missions'] = []
    data.setdefault('schema_version', 1)
    return data


def _handle_new_mission(
    *,
    body: NewMissionRequest,
    missions_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/system/missions/new.

    Steps:
      1. Derive kebab mission_id. Reject 400 if empty after kebab.
      2. Acquire in-process lock (serializes concurrent POSTs).
      3. Read local missions.json (read-only); 409 on dup id.
      4. Call GitHub REST: GET main ref → POST refs (atomic, 422 → 409) →
         PUT contents on branch → POST pulls.
      5. Return {mission_id, pr_url, branch}.

    Local missions.json is NOT mutated — it gets updated via `git pull`
    once the PR merges. This avoids drift vs `origin/main` in the shared
    `/home/larry/agent-core` checkout (heal-droplet-git-drift would alert
    otherwise).
    """
    mission_id = _kebab_case(body.name)
    if not mission_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                'error': 'invalid mission name',
                'detail': 'name kebab-cases to empty string',
            },
        )

    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'GITHUB_TOKEN env not set on dashboard-api service',
            },
        )

    repo_full = _missions_repo_full()
    branch = f'feat/new-mission-{mission_id}'
    now = now or datetime.now(timezone.utc)

    with _NEW_MISSION_LOCK:
        registry = _read_missions_registry(missions_path)
        for existing in registry['missions']:
            if isinstance(existing, dict) and existing.get('id') == mission_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        'error': 'mission_id collision',
                        'id': mission_id,
                        'existing_entry_brief': existing.get('brief', ''),
                    },
                )

        new_entry: dict[str, Any] = {
            'id': mission_id,
            'name': body.name,
            'phase': 'drafting',
            'brief': body.brief,
            'spec_docs': list(body.spec_docs),
            'task_ids': [],
            'repo': body.repo,
            'created': now.date().isoformat(),
            'deferred_reason': None,
        }
        updated_registry = {
            'schema_version': registry.get('schema_version', 1),
            'missions': registry['missions'] + [new_entry],
        }

        api_base = f'https://api.github.com/repos/{repo_full}'
        api_headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

        # 1. Resolve main's SHA.
        ref_resp = _github_api_request(
            'GET', f'{api_base}/git/refs/heads/main', headers=api_headers,
        )
        if ref_resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github get main ref failed',
                    'detail': f'status={ref_resp.status_code}',
                },
            )
        main_sha = ref_resp.json().get('object', {}).get('sha')
        if not isinstance(main_sha, str) or not main_sha:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github main ref missing sha',
                    'detail': '',
                },
            )

        # 2. Create the new branch — atomic at GitHub. 422 → branch
        #    already exists → return 409 to the caller.
        branch_resp = _github_api_request(
            'POST', f'{api_base}/git/refs', headers=api_headers,
            json_body={'ref': f'refs/heads/{branch}', 'sha': main_sha},
        )
        if branch_resp.status_code == 422:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'branch_exists',
                    'branch': branch,
                    'hint': (
                        'Mission name collides with an in-flight mission; '
                        'pick a different name.'
                    ),
                },
            )
        if branch_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github create branch failed',
                    'detail': f'status={branch_resp.status_code}',
                },
            )

        # 3. Get current `agents/beacon/missions.json` blob sha on the
        #    branch (which inherits main's content) so PUT can replace it.
        contents_get = _github_api_request(
            'GET',
            f'{api_base}/contents/agents/beacon/missions.json?ref={branch}',
            headers=api_headers,
        )
        file_sha: Optional[str] = None
        if contents_get.status_code == 200:
            sha_val = contents_get.json().get('sha')
            if isinstance(sha_val, str):
                file_sha = sha_val
        elif contents_get.status_code != 404:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github get contents failed',
                    'detail': f'status={contents_get.status_code}',
                },
            )

        # 4. PUT the updated missions.json onto the branch.
        new_text = json.dumps(updated_registry, indent=2) + '\n'
        put_body: dict[str, Any] = {
            'message': (
                f'feat(missions): register {mission_id} per +New mission flow'
            ),
            'content': __import__('base64').b64encode(
                new_text.encode('utf-8'),
            ).decode('ascii'),
            'branch': branch,
        }
        if file_sha:
            put_body['sha'] = file_sha
        put_resp = _github_api_request(
            'PUT', f'{api_base}/contents/agents/beacon/missions.json',
            headers=api_headers, json_body=put_body,
        )
        if put_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github put contents failed',
                    'detail': f'status={put_resp.status_code}',
                },
            )

        # 5. Open the PR.
        pr_body_parts = [
            f'Register mission `{mission_id}`.',
            '',
            f'**Brief:** {body.brief}',
        ]
        if body.spec_docs:
            pr_body_parts.append('')
            pr_body_parts.append('**Spec docs:**')
            for doc in body.spec_docs:
                pr_body_parts.append(f'- `{doc}`')
        pr_body_parts.append('')
        pr_body_parts.append(
            'Opened by the dashboard +New mission flow '
            '(E4.4f PR-A). Manual review and merge.',
        )
        pr_resp = _github_api_request(
            'POST', f'{api_base}/pulls', headers=api_headers,
            json_body={
                'title': f'feat(missions): register {mission_id}',
                'head': branch,
                'base': 'main',
                'body': '\n'.join(pr_body_parts),
            },
        )
        if pr_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github create pr failed',
                    'detail': f'status={pr_resp.status_code}',
                },
            )
        pr_json = pr_resp.json()
        pr_url = pr_json.get('html_url') if isinstance(pr_json, dict) else None
        if not isinstance(pr_url, str) or not pr_url:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github create pr returned no html_url',
                    'detail': '',
                },
            )

    return {
        'mission_id': mission_id,
        'pr_url': pr_url,
        'branch': branch,
    }


# ---- /api/larry/* handler (E4.4e PR-B2) ----
#
# Spec § 5.2, § 6.3, § 6.4, § 7.1, § 7.2, § 7.3 — Larry-action endpoint
# turns dashboard clicks into chain envelopes. The droplet is the
# canonical audit point: every action writes a `larry_action` row to
# chain_events keyed on (task_id, ts) via the shared `compute_event_id`
# helper. The `actor` column (migration 0006, PR-B1) carries the authed
# email — written as a top-level column, not buried in payload.
#
# We do NOT use `chain_event_emit.emit_event` here: that helper has no
# `actor` kwarg and modifying it is out-of-scope for PR-B2 (see PR-A
# #129). We import only the pure helpers (`compute_event_id`,
# `sanitize_payload`) from chain_event_shipper and write the row
# directly via the supabase client. Same dedup contract:
# on_conflict='event_id', ignore_duplicates=True.


def _import_chain_event_helpers():
    """Import shipper-side helpers without taking a module-level dep.

    Lazy import so the dashboard_api module loads cleanly even on a host
    where supabase-py / shipper deps aren't installed (test environment).
    """
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import chain_event_shipper as ces  # noqa: PLC0415
    return ces.compute_event_id, ces.sanitize_payload


# Test seam: tests monkeypatch this to inject a recording mock.
def _get_larry_action_supabase_client():
    """Build a service-role supabase client for the larry-action endpoint.

    Returns None if env is unset OR supabase-py isn't installed; the
    route raises 503 in that case. Tests override this function to
    inject a recording stub.
    """
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        return None
    try:
        from supabase import create_client  # type: ignore  # noqa: PLC0415
    except ImportError:
        return None
    return create_client(url, key)


def _atomic_write_envelope(path: Path, payload: dict[str, Any]) -> None:
    """Write `payload` as JSON to `path` atomically.

    Inbox watchers MUST NOT observe a partial file — write to a sibling
    `.tmp` then `os.replace` to land the final name in one syscall.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _build_envelope_for_action(
    *,
    source: dict[str, Any],
    action: str,
    comment: Optional[str],
    actor: str,
) -> tuple[str, str, dict[str, Any]]:
    """Return (target_agent, filename, envelope_body) per spec § 7.1.

    Source-event-type → action validity matrix:
      - approval_request → approve | reject (envelope to beacon)
      - clarify_request  → comment           (envelope to asking_agent)
      - larry_alert / escalation / sentinel_alert → mark_done ONLY
        (handled before this function — never reaches here)

    Raises HTTPException(400) for unsupported (event_type, action) pairs
    so dashboard mis-routing surfaces as a 400 rather than a silent
    envelope-to-the-wrong-agent.
    """
    event_type = source.get('event_type')
    payload = source.get('payload') or {}
    if not isinstance(payload, dict):
        payload = {}
    task_id = source.get('task_id')
    source_event_id = source.get('event_id') or ''

    if event_type == 'approval_request':
        target_agent = 'beacon'
        if action == 'approve':
            filename = f'larry-approval-{source_event_id}.json'
            envelope = {
                'task_id': f'larry-approval-{source_event_id}',
                'source': 'dashboard',
                'actor': actor,
                'dedup_identity': f'larry-approval:{source_event_id}',
                'timeout': 600,
                'prompt': (
                    'Larry approved the pending proposal via dashboard. '
                    f'Source event: {source_event_id}. Proceed per the '
                    'approve-path that beacon_approval_handler.py describes '
                    'for this approval_request type. Use the '
                    'suggested_envelope_for_approve payload from the source '
                    'event.'
                ),
            }
            if comment:
                envelope['comment'] = comment
            return target_agent, filename, envelope
        if action == 'reject':
            filename = f'larry-reject-{source_event_id}.json'
            envelope = {
                'task_id': f'larry-reject-{source_event_id}',
                'source': 'dashboard',
                'actor': actor,
                'dedup_identity': f'larry-reject:{source_event_id}',
                'timeout': 600,
                'prompt': (
                    'Larry rejected the pending proposal via dashboard. '
                    f'Source event: {source_event_id}. Optional comment: '
                    f'{comment or ""}. Soft reject — archive the pending '
                    'item, do not abort any in-flight work, route per the '
                    'suggested_envelope_for_reject payload from the source '
                    'event.'
                ),
            }
            if comment:
                envelope['comment'] = comment
            return target_agent, filename, envelope
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'action={action!r} not valid for approval_request',
        )

    if event_type == 'clarify_request':
        if action != 'comment':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='clarify_request only accepts action=comment',
            )
        asking_agent = payload.get('asking_agent')
        if not isinstance(asking_agent, str) or not asking_agent:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='source clarify_request missing asking_agent',
            )
        # round=1 default for V1 dashboard-originated replies. The
        # clarify_request payload schema (spec § 4) does not carry a
        # round counter; future spec work can lift this.
        round_n = 1
        filename = f'resume-{task_id}-r{round_n}.json'
        envelope = {
            'task_id': task_id,
            'source': 'dashboard',
            'actor': actor,
            'resume_session_id': payload.get('resume_session_id'),
            'round': round_n,
            'prompt': comment or '',
        }
        return asking_agent, filename, envelope

    # larry_alert / escalation / sentinel_alert have only the Mark-done
    # affordance (spec § 8.2). Reaching here means the dashboard tried to
    # apply approve / reject / comment to one — surface as 400.
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f'event_type={event_type!r} only supports action=mark_done '
            f'(got action={action!r})'
        ),
    )


def _select_source_event(
    supabase_client: Any, source_event_id: str,
) -> Optional[dict[str, Any]]:
    """Fetch the chain_events row for `source_event_id`. None if missing."""
    resp = (
        supabase_client.table('chain_events')
        .select('*')
        .eq('event_id', source_event_id)
        .limit(1)
        .execute()
    )
    rows = getattr(resp, 'data', None)
    if not rows:
        return None
    row = rows[0]
    return row if isinstance(row, dict) else None


def _handle_larry_action(
    *,
    source_event_id: str,
    action: str,
    comment: Optional[str],
    actor: str,
    agents_root: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/larry/action; see spec § 7.2.

    Raises HTTPException for 4xx; returns the response dict on success.
    """
    if action not in LARRY_ACTION_VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid action={action!r}',
        )
    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()

    source = _select_source_event(supabase_client, source_event_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='source event not found',
        )

    # Already-acted-on lock: read_at IS NOT NULL blocks every action
    # except mark_done (which is idempotent against the read state).
    if source.get('read_at') is not None and action != 'mark_done':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='source event already acted on',
        )

    envelope_written: Optional[str] = None
    target_agent: Optional[str] = None

    if action != 'mark_done':
        target_agent, filename, envelope = _build_envelope_for_action(
            source=source, action=action, comment=comment, actor=actor,
        )
        # Path-injection guard — both checks required.
        if target_agent not in ALLOWED_TARGET_AGENTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'target_agent={target_agent!r} not allowed',
            )
        inbox_root = agents_root / 'inboxes'
        agent_inbox = inbox_root / target_agent
        # Resolve against the agent inbox dir (which we just established
        # is a member of the frozenset). The envelope MUST be a leaf file
        # whose immediate parent is the agent inbox — no subdirectories,
        # no `..` traversal even if it lands back inside the inbox tree.
        # This is the second half of the spec § 7.3 path-injection guard.
        candidate = (agent_inbox / filename).resolve()
        if candidate.parent != agent_inbox.resolve():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='invalid envelope filename',
            )
        _atomic_write_envelope(candidate, envelope)
        envelope_written = str(candidate)

    # Flip read_at on source event (mark_done flow + envelope flows both).
    supabase_client.table('chain_events').update(
        {'read_at': ts_iso}
    ).eq('event_id', source_event_id).execute()

    # Insert the larry_action audit row. Top-level `actor` column per
    # migration 0006; payload mirrors spec § 5.2 verbatim.
    compute_event_id, sanitize_payload = _import_chain_event_helpers()
    source_task_id = source.get('task_id')
    action_payload = {
        'source_event_id': source_event_id,
        'source_event_type': source.get('event_type'),
        'action': action,
        'comment': comment,
        'envelope_written': envelope_written,
        'target_agent': target_agent,
    }
    action_event_id = compute_event_id(
        source_task_id, 'larry_action', ts_iso,
    )
    row: dict[str, Any] = {
        'event_id': action_event_id,
        'ts': ts_iso,
        'agent': 'dashboard',
        'event_type': 'larry_action',
        'actor': actor,
        'payload': sanitize_payload(action_payload),
    }
    if source_task_id:
        row['task_id'] = source_task_id
    supabase_client.table('chain_events').upsert(
        [row], on_conflict='event_id', ignore_duplicates=True,
    ).execute()

    return {
        'action_event_id': action_event_id,
        'envelope_written': envelope_written,
        'target_agent': target_agent,
    }


# ---- /api/system/rotation Auto/Off switch (dashboard-rotation-switch-001) ----
#
# GET returns the effective mode the System tab renders beside
# kill_switch_active. POST toggles the runtime override file
# (~/agents/rotation.disabled): mode=off creates it, mode=auto removes it.
# The scheduler picks the change up on its next ~2-min tick. POST writes a
# larry_action audit row mirroring _handle_larry_action — but without a
# source chain-event, since this is a direct operator toggle, not a response
# to a pending event. NO tracked file is mutated.


def _read_rotation_config_enabled(models_path: Path) -> bool:
    """Read ``rotation.enabled`` from agent-models.json. Missing file,
    malformed JSON, or a missing block all collapse to False (off) — the
    same safe default as the scheduler's ``_load_rotation_config``."""
    try:
        data = json.loads(models_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    block = data.get('rotation')
    if not isinstance(block, dict):
        return False
    return bool(block.get('enabled'))


def _reader_rotation_mode(
    agents_root: Path, models_path: Path, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Resolve the effective rotation mode from the override file + config.

    ``off`` whenever the override file is present OR the config default is
    disabled; ``auto`` only when neither forces it off. The component
    signals are surfaced so the UI can show *why* it's off."""
    override_active = (agents_root / ROTATION_OVERRIDE_FILE_NAME).exists()
    config_enabled = _read_rotation_config_enabled(models_path)
    mode = 'auto' if (config_enabled and not override_active) else 'off'
    return {
        'mode': mode,
        'override_active': override_active,
        'config_enabled': config_enabled,
        'as_of': _now_utc_iso(now),
    }


def _handle_rotation_mode_post(
    *,
    mode: str,
    actor: str,
    agents_root: Path,
    models_path: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Toggle the rotation override file + write the larry_action audit row.

    Raises HTTPException for 4xx; returns the resulting mode state on
    success. Idempotent on the filesystem: touching an existing override
    file or removing an absent one is a no-op.
    """
    if mode not in ROTATION_VALID_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid mode={mode!r}',
        )
    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()
    override_path = agents_root / ROTATION_OVERRIDE_FILE_NAME

    # off → create the override file; auto → remove it. The scheduler reads
    # presence on its next tick.
    if mode == 'off':
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.touch()
    else:  # auto
        try:
            override_path.unlink()
        except FileNotFoundError:
            pass

    # Audit row — mirrors _handle_larry_action's writer (top-level `actor`
    # column per migration 0006; same dedup contract). No source-event
    # lookup / read_at flip: this toggle has no originating chain-event.
    compute_event_id, sanitize_payload = _import_chain_event_helpers()
    action_payload = {
        'control': 'rotation_mode',
        'mode': mode,
        'override_file': str(override_path),
    }
    action_event_id = compute_event_id('rotation-mode', 'larry_action', ts_iso)
    row: dict[str, Any] = {
        'event_id': action_event_id,
        'ts': ts_iso,
        'agent': 'dashboard',
        'event_type': 'larry_action',
        'actor': actor,
        'task_id': 'rotation-mode',
        'payload': sanitize_payload(action_payload),
    }
    supabase_client.table('chain_events').upsert(
        [row], on_conflict='event_id', ignore_duplicates=True,
    ).execute()

    state = _reader_rotation_mode(agents_root, models_path, now=now)
    state['action_event_id'] = action_event_id
    return state


# ---- /api/larry/cleanup-review engine (approvals-queue-rework N1 / L8) ----
#
# The "clean up" button. Given the current pending decision set, run the
# SAME triage as scripts/triage_decisions.py (imported, not forked),
# auto-clear the confirmed-stale (backup-first, reversible), and return the
# items still judged live with a one-line reason each. Low-confidence
# (UNCERTAIN) rows are handed to a verification subagent — the pattern
# proven manually 2026-06-02 — and cleared ONLY when it positively confirms
# resolution. We never clear an item we cannot confirm resolved.


def _import_triage_decisions():
    """Lazy import of triage_decisions so dashboard_api loads cleanly on a
    host without supabase-py (triage_decisions only imports it inside
    _client(), so module import itself is stdlib-only)."""
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import triage_decisions as td  # noqa: PLC0415
    return td


def _beacon_pending_approvals_path(agents_root: Path) -> Path:
    return agents_root / 'state' / 'beacon-pending-approvals.json'


def _cleanup_review_backup_dir(agents_root: Path) -> Path:
    return agents_root / 'blackboard' / 'backups'


def _build_cleanup_review_prompt(items: list[dict[str, Any]]) -> str:
    """Prompt for the verification subagent over UNCERTAIN rows."""
    facts = json.dumps(items, indent=2)
    return (
        'You are verifying whether agent-OS decision rows are still LIVE '
        '(genuinely still need a human decision) or RESOLVED (the work '
        'already shipped or was answered, so the row is safe to clear).\n\n'
        'For each task_id below, decide whether it resolved. Use whatever '
        'evidence you can reach — git log, `gh pr list`/`gh pr view`, and '
        'beacon approval history. Be CONSERVATIVE: only answer "resolved" '
        'when you have positive evidence the work completed. If you are not '
        'sure, answer "unknown" (the caller will keep it for the human).\n\n'
        f'Items (JSON):\n{facts}\n\n'
        'Output ONLY a JSON object mapping each task_id to '
        '{"verdict": "resolved"|"live"|"unknown", "reason": "<one short '
        'sentence>"}. No prose, no markdown fence.'
    )


def _parse_verifier_verdicts(result_text: Optional[str]) -> dict[str, dict[str, str]]:
    """Extract the verdict map from the subagent's freeform result text.

    The subagent is asked for a bare JSON object but may wrap it in prose
    or a code fence; pull the first balanced {...} span and parse it.
    Returns {} on any failure — the caller then keeps every uncertain row.
    """
    if not result_text or not isinstance(result_text, str):
        return {}
    text = result_text.strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for tid, v in data.items():
        if isinstance(v, dict):
            out[tid] = {
                'verdict': str(v.get('verdict') or '').lower(),
                'reason': str(v.get('reason') or ''),
            }
    return out


# Test seam: tests monkeypatch this so the suite never spawns a real
# subagent. Production default shells out to the `claude` CLI.
def _cleanup_review_verify_uncertain(
    items: list[dict[str, Any]],
    *,
    timeout: int = CLEANUP_REVIEW_VERIFY_TIMEOUT_S,
) -> dict[str, dict[str, str]]:
    """Subagent-verify low-confidence rows.

    `items` is a list of {task_id, event_type, ts, why}. Returns a map
    task_id -> {"verdict": ..., "reason": ...}. On ANY failure (claude
    missing, timeout, non-zero exit, unparseable output) returns {} so the
    caller keeps every uncertain row — we never clear what we cannot
    positively confirm resolved.
    """
    if not items:
        return {}
    prompt = _build_cleanup_review_prompt(items)
    try:
        proc = subprocess.run(
            ['claude', '--print', '--model', CLEANUP_REVIEW_VERIFY_MODEL,
             '--output-format', 'json', prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        return {}
    result = data.get('result') if isinstance(data, dict) else None
    return _parse_verifier_verdicts(result)


def _handle_cleanup_review(
    *,
    actor: str,
    agents_root: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/larry/cleanup-review.

    Reuses triage_decisions' classification verbatim (no fork), auto-clears
    STALE+MOCK (and subagent-verified-resolved UNCERTAIN) rows backup-first,
    and returns {cleared, kept, backup_path, uncertain_reviewed}. Never
    clears a row whose source is still genuinely pending or unconfirmed.
    """
    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()
    td = _import_triage_decisions()

    # Current pending decision rows (read_at IS NULL).
    approvals = td._fetch(supabase_client, event_type='approval_request')
    clarifies = td._fetch(supabase_client, event_type='clarify_request')

    # Resolution signals: beacon approval state + clarify_response events.
    ba_path = _beacon_pending_approvals_path(agents_root)
    try:
        beacon_approvals = json.loads(ba_path.read_text())
    except (OSError, json.JSONDecodeError):
        beacon_approvals = {}
    pending_ids, history_ids, hist_resolved_at = td.build_beacon_signals(
        beacon_approvals)
    cresp_ts = td.fetch_clarify_response_ts(supabase_client)

    classified = td.classify_rows(
        approvals, clarifies,
        pending_ids, history_ids, hist_resolved_at, cresp_ts,
    )

    to_clear: list[tuple[dict[str, Any], str]] = []  # (row, reason)
    kept: list[dict[str, str]] = []
    uncertain: list[tuple[dict[str, Any], str, str]] = []  # (row, et, why)

    for cls, et, row, why in classified:
        if cls in ('STALE', 'MOCK'):
            to_clear.append((row, why))
        elif cls == 'LIVE':
            kept.append({'task_id': row.get('task_id') or '', 'reason': why})
        else:  # UNCERTAIN
            uncertain.append((row, et, why))

    # Low-confidence rows: escalate to the verification subagent. Skip the
    # subagent entirely when there's nothing uncertain (no cost on the
    # common path). Anything not positively confirmed `resolved` is KEPT.
    uncertain_reviewed = 0
    if uncertain:
        verify_input = [
            {'task_id': r.get('task_id'), 'event_type': et,
             'ts': r.get('ts'), 'why': why}
            for r, et, why in uncertain
        ]
        uncertain_reviewed = len(verify_input)
        verdicts = _cleanup_review_verify_uncertain(verify_input) or {}
        for r, et, why in uncertain:
            tid = r.get('task_id') or ''
            v = verdicts.get(tid) or {}
            verdict = v.get('verdict') or ''
            reason = v.get('reason') or why
            if verdict == 'resolved':
                to_clear.append((r, f'subagent-verified resolved: {reason}'))
            else:
                kept.append({
                    'task_id': tid,
                    'reason': f'unconfirmed ({verdict or "unknown"}): {reason}',
                })

    # Backup-first, then clear. Backup carries the actor + full rows so the
    # clear is both auditable and reversible (read_at -> NULL from `rows`).
    backup_path: Optional[str] = None
    cleared_task_ids: list[str] = []
    if to_clear:
        rows = [r for r, _why in to_clear]
        backup_dir = _cleanup_review_backup_dir(agents_root)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime('%Y%m%dT%H%M%SZ')
        bpath = backup_dir / f'cleanup-review-{stamp}.json'
        bpath.write_text(json.dumps(
            {'triggered_by': actor, 'ts': ts_iso, 'rows': rows},
            indent=2, default=str,
        ))
        backup_path = str(bpath)

        ids = [r['event_id'] for r in rows if r.get('event_id')]
        for i in range(0, len(ids), 200):
            supabase_client.table('chain_events').update(
                {'read_at': ts_iso}
            ).in_('event_id', ids[i:i + 200]).execute()

        seen: set[str] = set()
        for r, _why in to_clear:
            tid = r.get('task_id')
            if tid and tid not in seen:
                seen.add(tid)
                cleared_task_ids.append(tid)

    return {
        'cleared': cleared_task_ids,
        'kept': kept,
        'backup_path': backup_path,
        'uncertain_reviewed': uncertain_reviewed,
    }


# ---- FastAPI app ----

app = FastAPI(
    title='Ourliberty Dashboard API',
    description=(
        'Read-only droplet status surface for the E3 dashboard. '
        'All endpoints require the X-Dashboard-Token header.'
    ),
    version='0.1.0',
    # Disable default unauthenticated docs routes; we re-register gated
    # versions below so /docs and /openapi.json require the same token.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN],
    allow_methods=['GET', 'OPTIONS'],
    allow_headers=[HEADER_NAME, 'Content-Type'],
    allow_credentials=False,
    max_age=600,
)


@app.get('/health', response_model=HealthResponse, dependencies=[Depends(_require_token)])
def get_health() -> dict[str, Any]:
    return _reader_health(_agents_root())


@app.get('/agents/status', response_model=AgentsStatusResponse, dependencies=[Depends(_require_token)])
def get_agents_status() -> dict[str, Any]:
    return _reader_agents_status(_agents_root())


@app.get('/tasks/recent', response_model=TasksRecentResponse, dependencies=[Depends(_require_token)])
def get_tasks_recent(
    limit: int = Query(20, ge=1, le=TASKS_RECENT_MAX),
) -> dict[str, Any]:
    return _reader_tasks_recent(_agents_root(), limit=limit)


@app.get('/costs/today', response_model=CostsTodayResponse, dependencies=[Depends(_require_token)])
def get_costs_today() -> dict[str, Any]:
    return _reader_costs_today(_agents_root())


@app.get('/costs/week', response_model=CostsWeekResponse, dependencies=[Depends(_require_token)])
def get_costs_week() -> dict[str, Any]:
    return _reader_costs_week(_agents_root())


@app.get('/cycle-journal/recent', response_model=CycleJournalResponse, dependencies=[Depends(_require_token)])
def get_cycle_journal_recent(
    n: int = Query(5, ge=1, le=CYCLE_JOURNAL_MAX_N),
) -> dict[str, Any]:
    return _reader_cycle_journal(_agents_root(), n=n)


@app.get('/healers/status', response_model=HealersStatusResponse, dependencies=[Depends(_require_token)])
def get_healers_status() -> dict[str, Any]:
    return _reader_healers_status(_agents_root())


# ---- /api/system/* routes (E4.4d PR-C) ----
#
# All three are token-gated via the existing _require_token dependency.
# None are server-side cached: each request re-reads filesystem + /proc.
# Per locked decision-C, the droplet returns raw signals only; the
# dashboard route handler combines these with thresholds + chain_events
# to compute stuck-state.


@app.get(
    '/api/system/active-sessions',
    response_model=SystemActiveSessionsResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_active_sessions() -> dict[str, Any]:
    try:
        return _reader_system_active_sessions(_agents_root(), _cgroup_base())
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                'error': 'service-unavailable',
                'message': (
                    f'inbox-watcher cgroup unavailable: {e.filename or e}'
                ),
            },
        )


@app.get(
    '/api/system/cgroup-stats',
    response_model=SystemCgroupStatsResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_cgroup_stats() -> dict[str, Any]:
    try:
        return _reader_system_cgroup_stats(_cgroup_base())
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                'error': 'service-unavailable',
                'message': (
                    f'inbox-watcher cgroup unavailable: {e.filename or e}'
                ),
            },
        )


@app.get(
    '/api/system/worktrees',
    response_model=SystemWorktreesResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_worktrees() -> dict[str, Any]:
    return _reader_system_worktrees(_agents_root(), _worktrees_root())


@app.get(
    '/api/system/agent-queue',
    response_model=AgentQueueResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_agent_queue(
    agent: str = Query('forge'),
) -> dict[str, Any]:
    if agent not in AGENT_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'unknown agent: {agent!r}',
        )
    client = _get_larry_action_supabase_client()
    return _reader_agent_queue(
        _agents_root(), _worktrees_root(), agent, client,
    )


@app.get(
    '/api/system/build-sequences',
    response_model=BuildSequencesResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_build_sequences() -> dict[str, Any]:
    return _reader_build_sequences(_sequence_blackboard_root())


# ---- /api/system/missions routes (E4.4f PR-A) ----


@app.get(
    '/api/system/missions',
    response_model=MissionsResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_missions() -> dict[str, Any]:
    return _reader_missions(_missions_json_path())


@app.post(
    '/api/system/missions/new',
    response_model=NewMissionResponse,
    dependencies=[Depends(_require_token)],
)
def post_system_missions_new(body: NewMissionRequest) -> dict[str, Any]:
    return _handle_new_mission(body=body, missions_path=_missions_json_path())


# ---- /api/larry/* routes (E4.4e PR-B2) ----
#
# POST /api/larry/action turns dashboard UI clicks into chain envelopes
# and writes the audit row. GET /api/larry/allowlist returns the
# email allowlist (option A per spec § 6.4 — hardcoded). Both require
# the existing `X-Dashboard-Token` token gate; POST additionally
# requires a valid `X-Actor` header.


@app.post(
    '/api/larry/action',
    response_model=LarryActionResponse,
    dependencies=[Depends(_require_token)],
)
def post_larry_action(
    body: LarryActionRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    client = _get_larry_action_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='supabase unavailable',
        )
    return _handle_larry_action(
        source_event_id=body.source_event_id,
        action=body.action,
        comment=body.comment,
        actor=actor,
        agents_root=_agents_root(),
        supabase_client=client,
    )


@app.get(
    '/api/larry/allowlist',
    response_model=LarryAllowlistResponse,
    dependencies=[Depends(_require_token)],
)
def get_larry_allowlist() -> dict[str, Any]:
    return {'allowed_emails': sorted(LARRY_ACTION_ALLOWED_EMAILS)}


# ---- /api/system/rotation routes (dashboard-rotation-switch-001) ----
#
# GET is token-gated (read-only state). POST additionally requires a valid
# X-Actor — it mutates the runtime override file + writes an audit row,
# same gate posture as /api/larry/action.


@app.get(
    '/api/system/rotation',
    response_model=RotationModeResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_rotation() -> dict[str, Any]:
    return _reader_rotation_mode(_agents_root(), _agent_models_json_path())


@app.post(
    '/api/system/rotation',
    response_model=RotationModeUpdateResponse,
    dependencies=[Depends(_require_token)],
)
def post_system_rotation(
    body: RotationModeRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    client = _get_larry_action_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='supabase unavailable',
        )
    return _handle_rotation_mode_post(
        mode=body.mode,
        actor=actor,
        agents_root=_agents_root(),
        models_path=_agent_models_json_path(),
        supabase_client=client,
    )


@app.post(
    '/api/larry/cleanup-review',
    response_model=CleanupReviewResponse,
    dependencies=[Depends(_require_token)],
)
def post_larry_cleanup_review(
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    client = _get_larry_action_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='supabase unavailable',
        )
    return _handle_cleanup_review(
        actor=actor,
        agents_root=_agents_root(),
        supabase_client=client,
    )


# Auth-gate the FastAPI auto-docs surfaces too. We override the routes
# FastAPI installed for /docs and /openapi.json to require the same token.

@app.get('/docs', include_in_schema=False, dependencies=[Depends(_require_token)])
def _docs(request: Request):
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(openapi_url='/openapi.json', title=f'{app.title} — Swagger UI')


@app.get('/openapi.json', include_in_schema=False, dependencies=[Depends(_require_token)])
def _openapi_json():
    return app.openapi()


if __name__ == '__main__':
    # Direct `python3 -m scripts.dashboard_api` invocation; production
    # goes through uvicorn in the systemd unit.
    import uvicorn
    uvicorn.run(
        'scripts.dashboard_api:app',
        host='127.0.0.1', port=8000, log_level='info',
    )
