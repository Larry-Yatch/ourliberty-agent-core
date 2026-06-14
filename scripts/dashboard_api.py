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

import base64
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.utils import get_authorization_scheme_param  # noqa: F401  # reserved
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# supabase_factory and test_isolation_guard live in scripts/ alongside this
# file. Add scripts/ to sys.path once at module load so every lazy import
# in this file finds them regardless of which handler runs first.
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))


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


def _captures_json_path() -> Path:
    """Path to the durable-capture sibling registry (Missions v2 Phase 1).
    Env-overridable so tests redirect reads/writes to a tmpdir without
    touching the deployed checkout's `agents/beacon/captures.json`."""
    override = os.environ.get('OURLIBERTY_CAPTURES_JSON')
    if override:
        return Path(override)
    return _repo_root() / 'agents' / 'beacon' / 'captures.json'


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
# Manual-pin (spec § 6.5): when mode='off', the override file's CONTENTS carry
# the tier the operator pinned. The scheduler (rotate_active_tier._override_
# pinned_tier) honors it every tick. An empty file maps to tier1 — the
# historical Off behavior — so older clients that just touched the file still
# pin tier1.
ROTATION_VALID_TIERS: frozenset[str] = frozenset({'tier1', 'tier2'})
ROTATION_DEFAULT_PINNED_TIER = 'tier1'
# Live tier the load-gated scheduler is CURRENTLY running on, read from the
# state file rotate_active_tier maintains via the active_tier helpers (mirrors
# active_tier.STATE_REL). Surfaced as current_tier so the dashboard can show
# which tier Auto is actually on — pinned_tier is null in Auto, where the load
# gate (not the operator) owns the tier.
ROTATION_ACTIVE_TIER_STATE_REL = Path('blackboard') / 'active-tier.json'

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


# ---- /api/system/agent-queue response models (Agent Queue panel) ----
#
# One agent's dispatch lifecycle, with lanes that fit its archetype
# (docs/agent-queue-generalization-brief.md):
#   - BUILDER (forge): queued / building / in_review /
#     done_today(merged|changes_requested|failed). UNCHANGED from Phase 1.
#   - WORKER (mirror, beacon, pulse): queued / active /
#     done_today(succeeded|failed).
# ALL lane keys are always present (empty when N/A); `archetype` tells the UI
# which lanes to render. Builder lane item SHAPES stay byte-for-byte
# compatible with Phase 1, so the deployed forge panel is unaffected until the
# UI ships — hence the separate builder/worker done-item models below rather
# than one widened model.

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


class ActiveItem(BaseModel):
    # WORKER active lane: one in-flight registry entry for this agent.
    task_id: Optional[str]
    age_seconds: Optional[float]


class DoneItem(BaseModel):
    # BUILDER done item. `extra='forbid'` keeps this shape exact so a worker
    # done dict can never validate as a builder item under the response Union
    # (and vice versa) — the two models discriminate purely by field set.
    model_config = ConfigDict(extra='forbid')
    task_id: Optional[str]
    pr_url: Optional[str]
    # outcome is 'merged' | 'changes_requested' | 'failed'; reason carries the
    # raw chain_events event_type (review_pass / review_revision /
    # review_escalate / marker_error / preflight_reject / cost_budget).
    outcome: str
    reason: Optional[str]
    at: Optional[str]


class WorkerDoneItem(BaseModel):
    # WORKER done item, from today's session_done chain_events.
    # outcome is 'succeeded' | 'failed' (payload.success True/False).
    model_config = ConfigDict(extra='forbid')
    task_id: Optional[str]
    outcome: str
    at: Optional[str]
    message: Optional[str]


class AgentQueueResponse(BaseModel):
    agent: str
    archetype: str  # 'builder' | 'worker'
    queued: list[QueuedItem]
    building: list[BuildingItem]
    in_review: list[ReviewItem]
    active: list[ActiveItem]
    done_today: list[Union[DoneItem, WorkerDoneItem]]
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


class CapturesResponse(BaseModel):
    # Mirrors MissionsResponse: the captures array passes through verbatim
    # (parked/promoted/dropped all present — the dashboard Parked lane filters
    # to state=='parked'), plus an mtime-derived last_synced_at.
    captures: list[dict[str, Any]]
    last_synced_at: Optional[str]
    schema_version: Optional[int] = None


class MissionsDerivedResponse(BaseModel):
    # Missions v2 Phase 2 § 3.3 — the relocated derive endpoint's canonical
    # shape. `missions` + `orphans` match the dashboard's pre-Phase-2
    # MissionListResponse byte-for-byte (sans the additive orphan-readability
    # keys), which is what the § 4 parity test pins. `parked` + the orphan
    # state_badge/terminal/label fields are additive (existing reads ignore
    # unknown keys). `schema_version` is the endpoint contract version (1),
    # NOT the registry's schema_version.
    schema_version: int
    missions: list[dict[str, Any]]
    orphans: list[dict[str, Any]]
    parked: list[dict[str, Any]]
    last_synced_at: Optional[str]
    as_of: str


class NewMissionRequest(BaseModel):
    name: str = Field(..., min_length=1)
    brief: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    spec_docs: list[str] = Field(default_factory=list)


class NewMissionResponse(BaseModel):
    mission_id: str
    pr_url: str
    branch: str


class CaptureActionRequest(BaseModel):
    # Missions v2 Phase 3 § 4 — POST /api/missions/captures/{id}/action body.
    action: str = Field(..., min_length=1)  # promote | drop | snooze
    # promote overrides (all optional — defaults inferred from the capture):
    name: Optional[str] = None
    brief: Optional[str] = None
    repo: Optional[str] = None
    spec_docs: Optional[list[str]] = None
    # drop:
    reason: Optional[str] = None
    # snooze (ISO-8601 datetime; null clears the snooze):
    snoozed_until: Optional[str] = None


class CaptureActionResponse(BaseModel):
    # promote/drop are PR-backed → {pr_url, branch[, mission_id]}; snooze is a
    # direct committer write → {applied, snoozed_until}. All optional so one
    # model covers both shapes (§ 4 contract).
    pr_url: Optional[str] = None
    branch: Optional[str] = None
    mission_id: Optional[str] = None
    applied: Optional[bool] = None
    snoozed_until: Optional[str] = None


class MissionActionRequest(BaseModel):
    # Missions v2 Phase 3 § 5 — POST /api/system/missions/{id}/action body.
    action: str = Field(..., min_length=1)  # defer | resume | reprioritize
    # defer: optional human-readable reason recorded in deferred_reason.
    reason: Optional[str] = None
    # reprioritize: new optional priority int (additive schema; null clears it).
    priority: Optional[int] = None


class MissionActionResponse(BaseModel):
    # All three mission write-backs are PR-backed → {pr_url, branch} (§ 5).
    pr_url: Optional[str] = None
    branch: Optional[str] = None


# ---- /api/larry/* response + request models (E4.4e PR-B2) ----

class LarryActionRequest(BaseModel):
    source_event_id: str
    action: str
    comment: Optional[str] = None


class LarryActionResponse(BaseModel):
    # None when the action executed but its audit row could not be written
    # (audit #31: best-effort audit, see _handle_larry_action). audit_persisted
    # is then False and audit_error carries the cause.
    action_event_id: Optional[str]
    envelope_written: Optional[str]
    target_agent: Optional[str]
    # Audit #31: False when the side effect (envelope delivery / medic
    # reconcile) succeeded but the larry_action audit-row write failed. The
    # read_at claim is deliberately KEPT in that case (a retry must not
    # re-deliver the envelope), so the action is reported as a success with the
    # audit gap surfaced in-band rather than as an opaque 500.
    audit_persisted: bool = True
    audit_error: Optional[str] = None
    # Set only for decisions reconciled directly by the dashboard (Medic
    # silence): 'unsilenced' | 'unsilence-noop' | 'kept-silenced' | an error
    # tag. None for the normal envelope-routed actions.
    medic_reconcile: Optional[str] = None


class LarryAllowlistResponse(BaseModel):
    allowed_emails: list[str]


class RotationModeResponse(BaseModel):
    # Effective rotation mode the dashboard renders beside kill_switch_active.
    # 'off' when the runtime override file is present OR the config default is
    # disabled; 'auto' only when neither forces it off. pinned_tier is the tier
    # the operator pinned while 'off' (tier1|tier2); it is null in 'auto' mode,
    # where the load-gated scheduler owns the tier. current_tier is the tier the
    # scheduler is actually running on right now (live state), surfaced in every
    # mode so the UI can show which tier Auto landed on.
    mode: str
    pinned_tier: Optional[str]
    current_tier: Optional[str]
    override_active: bool
    config_enabled: bool
    as_of: str


class RotationModeRequest(BaseModel):
    mode: str
    # Required when mode='off' (which tier to pin); ignored for 'auto'.
    # Defaults to tier1 when omitted on an 'off' request, preserving the
    # pre-pin "Off = force Tier 1" behavior.
    pinned_tier: Optional[str] = None


class RotationModeUpdateResponse(BaseModel):
    mode: str
    pinned_tier: Optional[str]
    current_tier: Optional[str]
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
#
# LIVENESS CAVEAT (forge-queue-in-review-lane): in production only
# preflight_reject is push-emitted with agent='forge' today. auto_merge /
# marker_error / cost_budget were designed to arrive via the shipper's
# outbox-notifier.log source, which is dead (parser can't read the
# notifier's line shape) — and even once that parser is fixed, those log
# lines carry no agent= kv, so they'd land as agent='notifier' and still
# not match the .eq('agent','forge') fetch. Until producers emit them with
# correct agent attribution, in_review closure rests on Mirror's verdict
# rows (below) and done_today's 'failed' outcomes rest on preflight_reject
# alone. Tests that seed agent='forge' auto_merge rows pin the intended
# contract, not current production reality.
_QUEUE_TERMINAL_EVENT_TYPES = (
    'auto_merge', 'marker_error', 'preflight_reject',
    'cost_budget', 'review_escalate',
)

# Mirror's review verdicts also close an in_review entry. Mirror — NOT the
# building agent — emits these rows (same attribution seam as done_today's
# verdict join), so they arrive via `verdict_rows`, keyed back to the build
# by task_id. A REVISION closes the current entry; the subsequent re-review
# dispatch push-emits a fresh review_request which re-opens the lane.
_QUEUE_VERDICT_EVENT_TYPES = (
    'review_pass', 'review_revision', 'review_escalate',
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


# Fetch window for the agent-queue lanes. done_today only needs today;
# in_review needs the active-review horizon (hours to a few days). The bound
# keeps the unpaginated PostgREST read under its row cap as chain_events
# grows (review_request & co. are never retention-pruned) and doubles as an
# age-out: a review_request whose review died without any closing event
# (wedged session reaped pre-verdict, dropped best-effort verdict emit)
# falls out of the lane after this many days instead of ghosting forever.
_QUEUE_EVENTS_WINDOW_DAYS = 14


def _fetch_chain_events_for_agent(
    supabase_client: Any, agent: str,
) -> Optional[list[dict[str, Any]]]:
    """Pull this agent's recent chain_events rows for the in_review /
    done_today lanes (bounded to _QUEUE_EVENTS_WINDOW_DAYS).

    Returns None when the client is None (test env / no creds) or on any
    query error — callers degrade the affected lanes to empty rather than
    500ing. None vs [] matters for in_review: deriving with genuinely-empty
    verdict rows is fine, but deriving after a FAILED verdict fetch would
    resurrect every open review_request as a phantom lane entry.

    `payload` is selected so the WORKER done_today lane can read
    `payload.success` / `payload.message`; the builder derivations ignore it.
    """
    if supabase_client is None:
        return None
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=_QUEUE_EVENTS_WINDOW_DAYS)
    ).isoformat()
    try:
        resp = (
            supabase_client.table('chain_events')
            .select('agent,event_type,task_id,pr_url,ts,payload')
            .eq('agent', agent)
            .gte('ts', cutoff)
            .execute()
        )
    except Exception:  # noqa: BLE001 — never 500 on a read-only dashboard lane
        return None
    return list(getattr(resp, 'data', None) or [])


def _derive_in_review(
    rows: list[dict[str, Any]],
    verdict_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """IN_REVIEW lane: a task whose latest `review_request` has no later
    closing event for the same task_id. `since` = that review_request ts.

    Closing events come from two row sets, mirroring `_derive_done_today`'s
    attribution split: the agent's own terminal events (`rows`,
    _QUEUE_TERMINAL_EVENT_TYPES) and Mirror's review verdicts
    (`verdict_rows`, _QUEUE_VERDICT_EVENT_TYPES) joined by task_id.
    `verdict_rows` is required, not defaulted: deriving without it silently
    regresses to a lane nothing can close — the caller decides whether a
    missing fetch means [] (no verdicts) or skip-the-lane (fetch failed)."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        tid = r.get('task_id')
        if not tid:
            continue
        by_task.setdefault(tid, []).append(r)
    verdicts_by_task: dict[str, list[dict[str, Any]]] = {}
    for r in verdict_rows:
        tid = r.get('task_id')
        if not tid or r.get('event_type') not in _QUEUE_VERDICT_EVENT_TYPES:
            continue
        verdicts_by_task.setdefault(tid, []).append(r)
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

        def _later_than_request(e: dict[str, Any]) -> bool:
            dt = _ts_to_dt(e.get('ts'))
            return dt is not None and dt > latest_rr_dt

        if any(_later_than_request(e) for e in evs
               if e.get('event_type') in _QUEUE_TERMINAL_EVENT_TYPES):
            continue
        if any(_later_than_request(e)
               for e in verdicts_by_task.get(tid, ())):
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
        # Per-type outcome mapping over the same verdict set the in_review
        # lane closes on (_QUEUE_VERDICT_EVENT_TYPES) — keep the two in sync
        # or a task can vanish from in_review without ever landing here.
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


# Only forge opens PRs / runs the worktree+review lifecycle today, so it is
# the sole BUILDER. Everything else in AGENT_NAMES is a WORKER whose outcome
# is its session result, not a merge. (Mirror uses worktrees but reviews
# rather than ships — worker per the brief's DECIDED archetype model.)
_BUILDER_AGENTS: tuple[str, ...] = ('forge',)

# Worker done_today `message` is a free-text session summary; cap it so a
# runaway payload can't bloat the lane.
_WORKER_DONE_MESSAGE_MAXLEN = 280


def _archetype_for(agent: str) -> str:
    return 'builder' if agent in _BUILDER_AGENTS else 'worker'


def _reader_agent_queue_active(
    agents_root: Path, agent: str, now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """ACTIVE lane (WORKER only): in-flight registry entries for this agent.

    Reads the dispatch-sentinel registry via `_load_in_flight_index`
    (`<agents_root>/state/in-flight/*.json`, each `{task_stem, agent_id, pid,
    started_at}`), filters to `agent_id == agent`, and emits
    `{task_id: task_stem, age_seconds: now(UTC) - started_at}` newest-first
    (most recently started first). Degrades to `[]` when the dir is absent
    (handled by `_load_in_flight_index`). Never raises.
    """
    now = now or datetime.now(timezone.utc)
    index = _load_in_flight_index(agents_root)
    rows: list[tuple[Optional[datetime], dict[str, Any]]] = []
    for stem, entry in index.items():
        if entry.get('agent_id') != agent:
            continue
        started_raw = entry.get('started_at')
        started_dt = (
            _ts_to_dt(started_raw) if isinstance(started_raw, str) else None
        )
        age = (now - started_dt).total_seconds() if started_dt else None
        rows.append((started_dt, {'task_id': stem, 'age_seconds': age}))
    # Newest-first: most recently started at the top. Entries with an
    # unparseable started_at sort last (treated as oldest).
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda x: x[0] or _epoch, reverse=True)
    return [item for _dt, item in rows]


def _derive_worker_done_today(
    rows: list[dict[str, Any]], agent: str, now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """DONE_TODAY lane (WORKER): today's `session_done` events for `agent`
    (UTC day boundary, per `_reader_costs_today`), classified by
    `payload.success`: True -> 'succeeded', else -> 'failed'. Item:
    `{task_id, outcome, at, message}` (message truncated). Dedup by task_id
    keeping the latest ts; sort by `at` descending."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for r in rows:
        if r.get('agent') != agent or r.get('event_type') != 'session_done':
            continue
        dt = _ts_to_dt(r.get('ts'))
        if dt is None:
            continue
        dt = dt.astimezone(timezone.utc)
        if dt.date() != today:
            continue
        payload = r.get('payload')
        payload = payload if isinstance(payload, dict) else {}
        outcome = 'succeeded' if payload.get('success') else 'failed'
        msg = payload.get('message')
        if isinstance(msg, str):
            if len(msg) > _WORKER_DONE_MESSAGE_MAXLEN:
                msg = msg[:_WORKER_DONE_MESSAGE_MAXLEN]
        else:
            msg = None
        candidates.append((dt, {
            'task_id': r.get('task_id'),
            'outcome': outcome,
            'at': r.get('ts'),
            'message': msg,
        }))

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
    """Assemble one agent's dispatch lifecycle, routed by archetype.

    ALL lane keys are always present (empty when N/A) so the response is
    uniform; `archetype` tells the UI which lanes to render.

    BUILDER (forge): queued + building are filesystem-only; in_review +
    done_today come from chain_events and degrade to [] when
    `supabase_client` is None. done_today joins the agent's own
    session/failure events against Mirror's review verdicts (a separate
    fetch, since Mirror — not the building agent — emits them). UNCHANGED
    from Phase 1.

    WORKER (mirror, beacon, pulse): queued (filesystem) + active (in-flight
    registry, filesystem) + done_today (this agent's own session_done events,
    classified by payload.success). building + in_review stay empty.
    """
    now = now or datetime.now(timezone.utc)
    archetype = _archetype_for(agent)
    queued = _reader_agent_queue_queued(agents_root, agent, now=now)

    if archetype == 'builder':
        rows = _fetch_chain_events_for_agent(supabase_client, agent)
        verdict_rows = (
            rows if agent == 'mirror'
            else _fetch_chain_events_for_agent(supabase_client, 'mirror')
        )
        # None = fetch failed (vs [] = no rows). in_review needs BOTH row
        # sets to be trustworthy: deriving with a failed verdict fetch would
        # resurrect every open review_request as a phantom entry, so the
        # lane degrades to [] for this poll instead. done_today keeps its
        # original degrade-to-empty-inputs behavior.
        in_review = (
            [] if rows is None or verdict_rows is None
            else _derive_in_review(rows, verdict_rows)
        )
        return {
            'agent': agent,
            'archetype': archetype,
            'queued': queued,
            'building': _reader_agent_queue_building(
                agents_root, worktrees_root, agent, now=now,
            ),
            'in_review': in_review,
            'active': [],
            'done_today': _derive_done_today(
                rows or [], verdict_rows or [], agent, now=now,
            ),
            'captured_at': _now_utc_iso(now),
        }

    rows = _fetch_chain_events_for_agent(supabase_client, agent)
    return {
        'agent': agent,
        'archetype': archetype,
        'queued': queued,
        'building': [],
        'in_review': [],
        'active': _reader_agent_queue_active(agents_root, agent, now=now),
        'done_today': _derive_worker_done_today(rows or [], agent, now=now),
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


# Process-lifetime cache of the gh-CLI token so we don't shell out per request.
# A sentinel object distinguishes "not looked up yet" from a cached miss/hit;
# a successful lookup is cached, a failure is NOT (so a transient gh hiccup is
# retried next call). Reset in tests via `_GH_CLI_TOKEN_CACHE = _UNSET`.
_UNSET = object()
_GH_CLI_TOKEN_CACHE: Any = _UNSET


def _github_token() -> Optional[str]:
    """Read the GitHub token at request time. Prefer GITHUB_TOKEN / GH_TOKEN
    (loaded from /home/larry/credentials/.env.larry by the systemd unit). When
    neither env var is set, fall back to the gh CLI's stored auth
    (`gh auth token`) — the dashboard-api host has gh authenticated but no token
    env var, so the env-only read would leave every GitHub feature unauthed.
    The gh result is cached for the process lifetime. Returns None if no source
    yields a token (callers degrade fail-safe)."""
    tok = (
        os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or ''
    ).strip()
    if tok:
        return tok
    global _GH_CLI_TOKEN_CACHE
    if _GH_CLI_TOKEN_CACHE is not _UNSET:
        return _GH_CLI_TOKEN_CACHE
    try:
        proc = subprocess.run(
            ['gh', 'auth', 'token'],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None  # gh missing/hung — don't cache; retry next call
    if proc.returncode == 0:
        cli_tok = (proc.stdout or '').strip()
        if cli_tok:
            _GH_CLI_TOKEN_CACHE = cli_tok  # cache only a real success
            return cli_tok
    return None


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


def _reader_captures(captures_path: Path) -> dict[str, Any]:
    """Return {captures, last_synced_at, schema_version} for GET
    /api/missions/captures. Mirrors `_reader_missions`: missing file → 200
    with an empty list + null timestamp; malformed JSON → HTTPException(500)
    with a structured body (never a stack trace)."""
    if not captures_path.exists():
        return {
            'captures': [],
            'last_synced_at': None,
            'schema_version': None,
        }
    try:
        raw = captures_path.read_text()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as e:
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={'error': 'captures.json malformed', 'detail': first_line},
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'captures.json malformed',
                'detail': 'top-level JSON is not an object',
            },
        )
    captures = data.get('captures')
    if not isinstance(captures, list):
        captures = []
    schema_version = data.get('schema_version')
    if not isinstance(schema_version, int):
        schema_version = None
    return {
        'captures': captures,
        'last_synced_at': _iso(_safe_mtime(captures_path)),
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


# ---------------------------------------------------------------------------
# Missions v2 Phase 2 — the relocated derive endpoint (GET /api/missions/derived)
# (spec: agents/beacon/specs/missions-v2-phase2-resurfacing-and-derive.md § 3-4)
#
# The mission-phase derive (phase / aggregate / orphan) is lifted VERBATIM from
# the dashboard's `lib/mission-queries.ts` so there is one derive, no TS↔Python
# drift. The pure helpers below (derive_phase_for_task, aggregate_mission_phase,
# detect_orphans + the infrastructure-task filter set, summarize_task_events)
# are byte-faithful ports — the § 4 parity test deep-equals their output against
# a committed expected-output JSON that the dashboard side asserts against too.
#
# Read-only: no consumer writes through this endpoint. Mission-task pr_state
# stays None (matching the dashboard route, parity-pinned by § 4). ORPHAN
# terminal detection additionally does a bounded, fail-safe GitHub PR-state
# read (_resolve_orphan_pr_states, § 3.4): a merged sequence step rarely
# carries an `auto_merge` event under its own task_id, so an event-only derive
# leaves every merged orphan stuck at `building` and the lane never collapses.
# The read reuses the GITHUB_TOKEN already loaded by the systemd unit (no new
# credential — the 4-artifact obligation does NOT trigger), is batched one
# GraphQL call per repo, and degrades to the event-only path (pr_state=None) on
# any error or missing token — so the board never blocks or 500s on GitHub.

# Phase rank for mission-level aggregation. Higher = more advanced.
# "deferred" is handled separately (mission-level override).
_PHASE_RANK: dict[str, int] = {
    'drafting': 0,
    'ready': 1,
    'in_flight': 2,
    'awaiting_merge': 3,
    'shipped': 4,
}

# Orphan readability (§ 3.4). An unmerged orphan quiet for longer than this is
# badged `stalled` — kept VISIBLE and flagged, NEVER hidden. Single tunable
# constant; the parity test pins the invariant that no unmerged orphan is ever
# terminal regardless of age.
_STALE_AFTER_DAYS = 14

# Trailing window for orphan detection (matches the dashboard route).
_ORPHAN_WINDOW_DAYS = 30


def _ev_payload(ev: dict[str, Any]) -> dict[str, Any]:
    p = ev.get('payload')
    return p if isinstance(p, dict) else {}


def _is_session_start(ev: dict[str, Any]) -> bool:
    if ev.get('event_type') != 'session_start':
        return False
    agent = (ev.get('agent') or '').lower()
    return agent == 'forge' or agent == 'mirror'


def _is_auto_merge(ev: dict[str, Any]) -> bool:
    return ev.get('event_type') == 'auto_merge'


def _is_mirror_review_pass(ev: dict[str, Any]) -> bool:
    if ev.get('event_type') != 'marker_emit':
        return False
    payload = _ev_payload(ev)
    # `(payload.marker_type ?? payload.marker) ?? ''` — null-coalesce, so an
    # explicit empty string is NOT replaced by payload.marker (matches TS).
    marker_type = payload.get('marker_type')
    if marker_type is None:
        marker_type = payload.get('marker')
    if marker_type is None:
        marker_type = ''
    if not isinstance(marker_type, str):
        marker_type = ''
    upper = marker_type.upper()
    return upper == 'REVIEW_PASS' or upper == 'MIRROR_REVIEW_PASS'


def _is_escalation(ev: dict[str, Any]) -> bool:
    return ev.get('event_type') == 'escalation'


def derive_phase_for_task(
    events: list[dict[str, Any]],
    pr_state: Optional[str],
) -> str:
    """Port of `derivePhaseForTask` (mission-queries.ts § 5.2 rule order).

    Most-specific first: shipped > awaiting_merge > in_flight > ready.
    """
    if any(_is_auto_merge(e) for e in events) or pr_state == 'MERGED':
        return 'shipped'
    if any(_is_mirror_review_pass(e) for e in events) or any(
        _is_escalation(e) for e in events
    ):
        return 'awaiting_merge'
    if any(_is_session_start(e) for e in events):
        return 'in_flight'
    return 'ready'


def aggregate_mission_phase(
    entry: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> Optional[str]:
    """Port of `aggregateMissionPhase`. deferred override; no tasks → entry
    phase hint; shipped only when every task is shipped; else most-advanced
    non-shipped phase."""
    if entry.get('phase') == 'deferred':
        return 'deferred'
    if not tasks:
        return entry.get('phase')
    non_shipped = [t for t in tasks if t.get('derived_phase') != 'shipped']
    if not non_shipped:
        return 'shipped'
    best = 'drafting'
    for t in non_shipped:
        dp = t.get('derived_phase')
        if dp == 'deferred':
            continue
        if _PHASE_RANK.get(dp, 0) > _PHASE_RANK.get(best, 0):
            best = dp
    return best


# Agents that exclusively emit infrastructure plumbing events. Tasks they own
# are never user-facing missions; surfacing them buries the real one-off PRs.
INFRASTRUCTURE_AGENT_NAMES: frozenset[str] = frozenset({
    'deploy-notifier',
    'watchdog',
    'sync.service',
    'outbox-notifier',
    'dead-letter',
    'sentinel',
})

# task_id prefixes that mark infrastructure plumbing regardless of emitter.
INFRASTRUCTURE_TASK_ID_PREFIXES: tuple[str, ...] = (
    'notify-',
    'dead-letter-',
    'dead-letter:',
    'install-drift:',
    'deploy-notifier:',
    'sync-blocked:',
    'sync-blocked-',
    'pr-url-unrewritable:',
    'bots:',
    'auto-restarted:',
    'pulse-auto-',
    'cycle-finding-',
    'pulse-thread-',
    'synthetic-fixture-',
    'claude_max_',
    'inbox-stall:',
)

_TEST_FIXTURE_LENGTH_CAP = 35


def _looks_like_alert_message_not_task_id(task_id: str) -> bool:
    return ' ' in task_id


def _looks_like_test_fixture(task_id: str) -> bool:
    if len(task_id) > _TEST_FIXTURE_LENGTH_CAP:
        return False
    return task_id.startswith('t-') or task_id.startswith('task-')


def _looks_like_mirror_review_session(task_id: str) -> bool:
    return task_id.startswith('review-')


def is_infrastructure_task(task_id: str, agent: Optional[str]) -> bool:
    """Port of `isInfrastructureTask`."""
    if agent and (agent in INFRASTRUCTURE_AGENT_NAMES or agent.startswith('heal-')):
        return True
    for prefix in INFRASTRUCTURE_TASK_ID_PREFIXES:
        if task_id.startswith(prefix):
            return True
    if _looks_like_alert_message_not_task_id(task_id):
        return True
    if _looks_like_test_fixture(task_id):
        return True
    if _looks_like_mirror_review_session(task_id):
        return True
    return False


# task_id shapes that ARE orphans (so the Orphans lane still surfaces them) but
# are NOT buildable initiatives — they must never be auto-proposed onto the
# curated `proposed` missions lane (the autoregister healer's decision queue).
# This is a STRICTER gate than is_infrastructure_task: the Orphans lane is an
# everything-in-flight view, while the proposed lane is a short, high-signal
# accept/dismiss queue. Categories swept: chain-incident / alert artifacts
# (carry a `:`), desktop capture hashes (captures lane), sequence-step proposals,
# translation / generated-rule / dated-digest artifacts, and stale
# test-fixture-shaped ids. Chosen NOT to collide with genuine buildable ids
# (e.g. `p2-digest-generator`, `harden-test-prod-write-isolation-001`,
# `log-dir-test-isolation-leak-001` all stay proposable).
_NON_PROPOSABLE_TASK_ID_PREFIXES: tuple[str, ...] = (
    'step-',                # bare sequence-step proposal
    'real-', 'prod-',       # stale test-fixture-shaped ids
    'test-isolation-',      # stale test-fixture-shaped ids (prefix only — a real
                            # `*-test-isolation-*` initiative is unaffected)
    'unreg-approval-',      # chain-incident artifact (hyphen variant; the `:`
                            # variant is caught by the bare-colon rule)
    'ceo-digest-',          # generated digest artifact
    'weekly-', 'check-i-',  # dated temporal digest artifacts
)

# Substrings that mark translation / generated-rule artifacts regardless of
# position. Deliberately narrow (no bare `digest`/`summary`) so buildable ids are
# never swept.
_NON_PROPOSABLE_TASK_ID_SUBSTRINGS: tuple[str, ...] = (
    'alert-translation',
    'dispatch-translations',
    'g-rule',
)

_DESKTOP_CAPTURE_HASH_RE = re.compile(r'^desktop-[0-9a-f]{6,}$')
_SEQUENCE_STEP_RE = re.compile(r'^seq-.*-step-')


def is_proposable_initiative(task_id: str, agent: Optional[str] = None) -> bool:
    """True iff ``task_id`` is a genuine buildable initiative worth surfacing on
    the curated `proposed` missions lane.

    The proposed lane is a SHORT decision queue, so the bar is higher than the
    Orphans lane's: an orphan that is infrastructure, a chain-incident/alert
    artifact, a desktop capture, a sequence-step proposal, a translation/rule/
    dated-digest artifact, or a stale test-fixture id is an orphan but NOT a
    buildable mission. Conservative by design — anything not matching a known
    noise shape is treated as proposable (err toward keeping a live buildable)."""
    if not isinstance(task_id, str) or not task_id.strip():
        return False
    if is_infrastructure_task(task_id, agent):
        return False
    if ':' in task_id:
        # transcript-not-persisted:, approval-request:, pipeline-stall:,
        # sequence-invalid:, failure:, unreviewed-merge:, wedged-worktree:,
        # no-session-revision:, install-drift-timer: — all incident/alert noise.
        return False
    if '-' not in task_id:
        # Degenerate single-token id (e.g. `summary`, `20`) — never an initiative.
        return False
    if _DESKTOP_CAPTURE_HASH_RE.match(task_id):
        return False
    if _SEQUENCE_STEP_RE.match(task_id):
        return False
    if task_id.startswith(_NON_PROPOSABLE_TASK_ID_PREFIXES):
        return False
    if any(s in task_id for s in _NON_PROPOSABLE_TASK_ID_SUBSTRINGS):
        return False
    return True


def _ts_key(ts: Optional[str]) -> datetime:
    """Sort key: parsed ts, with unparseable/missing sorting oldest."""
    return _ts_to_dt(ts) or datetime.min.replace(tzinfo=timezone.utc)


def detect_orphans(
    events: list[dict[str, Any]],
    registered_task_ids: set[str],
) -> list[dict[str, Any]]:
    """Port of `detectOrphans`. task_ids in chain_events but not registered in
    any mission; infrastructure events filtered out. Output newest-first by
    last event ts. The caller supplies the time window (no filtering here)."""
    by_task: dict[str, dict[str, Any]] = {}
    for ev in events:
        tid = ev.get('task_id')
        if not tid or tid in registered_task_ids:
            continue
        if is_infrastructure_task(tid, ev.get('agent')):
            continue
        existing = by_task.get(tid)
        if existing is None:
            by_task[tid] = {
                'task_id': tid,
                'last_event_ts': ev.get('ts'),
                'agent': ev.get('agent'),
                'pr_url': ev.get('pr_url'),
            }
            continue
        ev_dt = _ts_to_dt(ev.get('ts'))
        ex_dt = _ts_to_dt(existing['last_event_ts'])
        if ev_dt is not None and ex_dt is not None and ev_dt > ex_dt:
            by_task[tid] = {
                'task_id': tid,
                'last_event_ts': ev.get('ts'),
                'agent': ev.get('agent') if ev.get('agent') is not None
                else existing['agent'],
                'pr_url': ev.get('pr_url') if ev.get('pr_url') is not None
                else existing['pr_url'],
            }
        elif not existing.get('pr_url') and ev.get('pr_url'):
            existing['pr_url'] = ev.get('pr_url')
    orphans = list(by_task.values())
    orphans.sort(key=lambda o: _ts_key(o.get('last_event_ts')), reverse=True)
    return orphans


def summarize_task_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Port of `summarizeTaskEvents`. events are newest-first."""
    if not events:
        return {'last_event_ts': None, 'pr_url': None, 'agent': None}
    newest = events[0]
    pr_event = next((e for e in events if e.get('pr_url')), None)
    return {
        'last_event_ts': newest.get('ts'),
        'pr_url': pr_event.get('pr_url') if pr_event else None,
        'agent': newest.get('agent') if newest.get('agent') is not None else None,
    }


# Common technical acronyms — kept fully uppercase in humanized labels so
# "Structural Pr Url Validator" reads as "Structural PR URL Validator". Ported
# from the dashboard's OrphansLane humanizer so the relocated `label` renders
# identically (one derive, no drift).
_ACRONYMS: frozenset[str] = frozenset({
    'api', 'cd', 'ci', 'cli', 'css', 'db', 'dag', 'dm', 'e2e', 'fk', 'gh',
    'html', 'http', 'https', 'id', 'io', 'ip', 'jsx', 'json', 'ms', 'oauth',
    'pm', 'pr', 'qa', 'rls', 'sdk', 'sql', 'ssh', 'tcp', 'tls', 'ts', 'tsx',
    'ui', 'url', 'ux', 'v1', 'v2', 'v3', 'wip', 'yaml',
})

_HUMANIZE_SPLIT_RE = re.compile(r'[_:]')


def _humanize_task_id(task_id: str) -> str:
    """Port of OrphansLane.humanizeTaskId — kebab/underscore/colon → Title Case
    with known acronyms uppercased."""
    parts = _HUMANIZE_SPLIT_RE.sub('-', task_id).split('-')
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        lower = part.lower()
        if lower in _ACRONYMS:
            out.append(lower.upper())
        else:
            out.append(part[0:1].upper() + part[1:])
    return ' '.join(out)


def _orphan_label_and_location(
    events: list[dict[str, Any]],
    task_id: str,
) -> tuple[str, Optional[str], Optional[str]]:
    """Resolve an orphan's readable label + repo/branch (§ 3.4).

    Label resolution order: desktop chat title (latest desktop_session_*
    event's payload.title) > repo/branch (from event payload) > humanized
    task_id. Events are newest-first. Degrades gracefully when title is absent
    (the desktop emitter may not yet populate payload.title)."""
    title: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    for ev in events:  # newest-first
        payload = _ev_payload(ev)
        et = ev.get('event_type') or ''
        if title is None and isinstance(et, str) and et.startswith('desktop_session'):
            t = payload.get('title')
            if isinstance(t, str) and t.strip():
                title = t.strip()
        if repo is None:
            r = payload.get('repo')
            if isinstance(r, str) and r.strip():
                repo = r.strip()
                b = payload.get('branch')
                branch = b.strip() if isinstance(b, str) and b.strip() else None
    if title:
        label = title
    elif repo and branch:
        label = f'{repo}/{branch}'
    elif repo:
        label = repo
    else:
        label = _humanize_task_id(task_id)
    return label, repo, branch


# Orphan terminal-state detection (§ 3.4). An orphan's "done-ness" is NOT
# reliably present in chain_events — a merged sequence step rarely carries an
# `auto_merge` event under its own task_id — so the event-only derive leaves
# every merged orphan stuck at `building` and the lane never collapses. The
# documented fix is a bounded, fail-safe GitHub PR-state read for orphans that
# carry a pr_url. MERGED → shipped, CLOSED → closed (both terminal/hidden), OPEN
# → stays visible. Reuses GITHUB_TOKEN (no new credential).
_PR_URL_RE = re.compile(r'github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)')
# Defensive upper bound so a pathological orphan set can't fan out into an
# unbounded GitHub query. _PR_STATE_TOTAL_BUDGET_S caps the WALL TIME across all
# per-repo calls combined (not per call), so a slow GitHub never stalls the
# board past the dashboard's own 5s client timeout — it degrades to event-only
# for the repos it couldn't reach in budget.
_MAX_PR_STATE_LOOKUPS = 200
_PR_STATE_TOTAL_BUDGET_S = 4.0


def _parse_pr_url(url: Optional[str]) -> Optional[tuple[str, str, int]]:
    """`https://github.com/<owner>/<repo>/pull/<n>` → (owner, repo, n)."""
    if not isinstance(url, str):
        return None
    m = _PR_URL_RE.search(url)
    if not m:
        return None
    try:
        return m.group(1), m.group(2), int(m.group(3))
    except (ValueError, IndexError):
        return None


def _resolve_orphan_pr_states(pr_urls: list[str]) -> dict[str, str]:
    """Map each PR url → its GitHub state ('OPEN' | 'CLOSED' | 'MERGED').

    Bounded (≤ _MAX_PR_STATE_LOOKUPS), batched (one GraphQL call per repo), and
    FAIL-SAFE: returns whatever it resolved ({} on a missing token, network /
    timeout error, non-200, or malformed response) so callers fall back to the
    event-only derive (pr_state=None) — exactly the pre-fix behavior. NEVER
    raises. Read-only; reuses the existing GITHUB_TOKEN.
    """
    token = _github_token()
    if not token or not pr_urls:
        return {}
    # Dedup + bound; group PR numbers per (owner, repo) for one query each.
    by_repo: dict[tuple[str, str], dict[int, str]] = {}
    seen: set[str] = set()
    total = 0
    for url in pr_urls:
        if url in seen:
            continue
        seen.add(url)
        parsed = _parse_pr_url(url)
        if parsed is None:
            continue
        owner, repo, number = parsed
        by_repo.setdefault((owner, repo), {})[number] = url
        total += 1
        if total >= _MAX_PR_STATE_LOOKUPS:
            break
    out: dict[str, str] = {}
    headers = {'Authorization': f'bearer {token}', 'Accept': 'application/json'}
    # owner/name go through GraphQL variables (never string-interpolated) so a
    # malformed pr_url can't break or inject the query; only the integer PR
    # numbers — which _parse_pr_url guarantees are ints — are interpolated.
    deadline = time.monotonic() + _PR_STATE_TOTAL_BUDGET_S
    for (owner, repo), num_to_url in by_repo.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            break  # out of wall-clock budget → degrade to event-only for the rest
        fields = '\n'.join(
            f'  p{n}: pullRequest(number: {n}) {{ state }}' for n in num_to_url
        )
        query = (
            'query($owner: String!, $name: String!) {\n'
            '  repository(owner: $owner, name: $name) {\n'
            f'{fields}\n  }}\n}}'
        )
        try:
            resp = _github_api_request(
                'POST', 'https://api.github.com/graphql',
                headers=headers,
                json_body={
                    'query': query,
                    'variables': {'owner': owner, 'name': repo},
                },
                timeout=remaining,
            )
            if getattr(resp, 'status_code', None) != 200:
                continue
            data = resp.json()
        except Exception:  # noqa: BLE001 — fail-safe: never break the derive
            continue
        # GitHub returns 200 with {"data": null, "errors": [...]} on a
        # whole-query failure (bad token, rate-limit, malformed query); the
        # `or {}` keeps a present-but-null `data` from raising here (this line
        # is past the try, so an AttributeError would escape and 500 the route).
        repo_node = (
            (data.get('data') or {}).get('repository')
            if isinstance(data, dict) else None
        )
        if not isinstance(repo_node, dict):
            continue
        for n, url in num_to_url.items():
            node = repo_node.get(f'p{n}')
            if isinstance(node, dict) and isinstance(node.get('state'), str):
                out[url] = node['state']
    return out


def _derive_orphan_readability(
    orphan: dict[str, Any],
    events: list[dict[str, Any]],
    now: Optional[datetime] = None,
    pr_state: Optional[str] = None,
) -> dict[str, Any]:
    """Additive Phase-2 orphan fields (§ 3.4). Hiding is driven by a REAL
    terminal signal — a merged or explicitly-closed PR — NEVER by a clock.

    `pr_state` is the live GitHub state ('MERGED' | 'CLOSED' | 'OPEN') for an
    orphan carrying a pr_url, or None when unavailable (no token, network error,
    or no PR). When None we fall back to the event-only derive, which can only
    reach `shipped` via an `auto_merge` event — the conservative pre-fix path.
    A quiet-but-OPEN (or PR-less) orphan is `stalled` and stays VISIBLE: nothing
    in-flight is ever hidden. `closed` (PR closed unmerged) is a deliberate
    human action, so it is terminal/hidden with its own badge.
    """
    now = now or datetime.now(timezone.utc)
    derived_phase = derive_phase_for_task(events, pr_state)
    last_dt = _ts_to_dt(orphan.get('last_event_ts'))
    stale_cutoff = now - timedelta(days=_STALE_AFTER_DAYS)
    is_stale = last_dt is not None and last_dt < stale_cutoff

    if derived_phase == 'shipped':
        state_badge, terminal, stalled = 'shipped', True, False
    elif pr_state == 'CLOSED':
        # PR explicitly closed without merging — deliberately dropped → terminal
        # (distinct badge). An explicit close is a human action, unlike a stall.
        state_badge, terminal, stalled = 'closed', True, False
    elif is_stale:
        # Unmerged + quiet past the threshold: overlooked-important bucket.
        state_badge, terminal, stalled = 'stalled', False, True
    elif derived_phase == 'awaiting_merge':
        state_badge, terminal, stalled = 'in-review', False, False
    else:  # in_flight / ready — active, unmerged
        state_badge, terminal, stalled = 'building', False, False

    label, repo, branch = _orphan_label_and_location(events, orphan.get('task_id', ''))
    return {
        'derived_phase': derived_phase,
        'state_badge': state_badge,
        'terminal': terminal,
        'stalled': stalled,
        'label': label,
        'repo': repo,
        'branch': branch,
    }


# Phase 4 § 4 — the meaning-layer field contract. Authored by the Narrator
# (scripts/missions_narrator.py), never computed here. Absence is legal and
# renders the neutral "briefing…" state: a missing/malformed field surfaces as
# None, NEVER as a raw machine value. These mirror the validators so a
# hand-edited or partial capture can't leak a half-formed briefing to the card.
_VALID_RISKS = ('safe', 'medium', 'careful')
_VALID_RECOMMENDED_ACTIONS = ('delegate', 'promote', 'drop', 'snooze')


def _meaning_layer_fields(cap: dict[str, Any]) -> dict[str, Any]:
    """Extract the Phase 4 meaning-layer fields from a capture, validating each
    so absence/garbage degrades to the neutral None state (§ 4: "A card with no
    briefing yet renders a neutral state — never raw machine fields")."""
    briefing = cap.get('briefing')
    if not (isinstance(briefing, dict)
            and all(isinstance(briefing.get(k), str) and briefing.get(k)
                    for k in ('what', 'why', 'suggest'))):
        briefing = None
    else:
        briefing = {k: briefing[k] for k in ('what', 'why', 'suggest')}

    risk = cap.get('risk')
    if risk not in _VALID_RISKS:
        risk = None

    risk_note = cap.get('risk_note')
    if not (isinstance(risk_note, str) and risk_note):
        risk_note = None

    recommended_action = cap.get('recommended_action')
    if recommended_action not in _VALID_RECOMMENDED_ACTIONS:
        recommended_action = None

    provenance = cap.get('briefing_provenance')
    if not (isinstance(provenance, dict) and provenance):
        provenance = None

    return {
        'briefing': briefing,
        'risk': risk,
        'risk_note': risk_note,
        'recommended_action': recommended_action,
        'briefing_provenance': provenance,
    }


def _parked_from_captures(
    captures: list[dict[str, Any]], now: datetime,
) -> list[dict[str, Any]]:
    """Build the `parked[]` array (§ 3.3) from captures.json. Only state=='parked'
    captures; `aging` is the GC healer's persisted flag (Phase 1 — never
    recomputed here). `area` is reserved (always None today — scene-graph T8).

    Phase 3 § 4.3: a capture snoozed past `now` (`snoozed_until` in the future)
    is suppressed from the Parked lane / resurfacing until the snooze elapses.

    Phase 4 § 4: the meaning-layer fields (briefing/risk/risk_note/
    recommended_action/briefing_provenance) ride along, validated so an
    un-briefed capture surfaces None (neutral state) rather than raw machine
    fields."""
    out: list[dict[str, Any]] = []
    for cap in captures:
        if not isinstance(cap, dict) or cap.get('state') != 'parked':
            continue
        if _is_snoozed(cap, now):
            continue
        origin = cap.get('origin') if isinstance(cap.get('origin'), dict) else {}
        entry = {
            'capture_id': cap.get('id'),
            'title': cap.get('title'),
            'label': cap.get('label'),
            'repo': origin.get('repo'),
            'area': origin.get('area'),
            'aging': cap.get('aging') is True,
            'last_touched': cap.get('last_touched'),
        }
        entry.update(_meaning_layer_fields(cap))
        out.append(entry)
    return out


def _fetch_events_for_task_ids(
    supabase_client: Any,
    task_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Port of `fetchEventsForTaskIds`. Single query for a set of task_ids;
    returns a dict keyed by task_id with events newest-first. Degrades to {}
    when the client is None (no creds / test env) or on any query error."""
    if supabase_client is None or not task_ids:
        return {}
    try:
        resp = (
            supabase_client.table('chain_events')
            .select('event_type,task_id,agent,pr_url,ts,payload')
            .in_('task_id', task_ids)
            .order('ts', desc=True)
            .execute()
        )
    except Exception:  # noqa: BLE001 — never 500 a read-only derive
        return {}
    rows = list(getattr(resp, 'data', None) or [])
    rows.sort(key=lambda r: _ts_key(r.get('ts')), reverse=True)
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in rows:
        tid = ev.get('task_id')
        if not tid:
            continue
        out.setdefault(tid, []).append(ev)
    return out


def _fetch_recent_chain_events(
    supabase_client: Any,
    days: int = _ORPHAN_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Port of `fetchRecentChainEvents`. All chain_events in the trailing
    window, newest-first. Degrades to [] when the client is None or on error."""
    if supabase_client is None:
        return []
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    try:
        resp = (
            supabase_client.table('chain_events')
            .select('event_type,task_id,agent,pr_url,ts,payload')
            .gte('ts', since)
            .order('ts', desc=True)
            .execute()
        )
    except Exception:  # noqa: BLE001 — never 500 a read-only derive
        return []
    rows = list(getattr(resp, 'data', None) or [])
    rows.sort(key=lambda r: _ts_key(r.get('ts')), reverse=True)
    return rows


def _build_derived_response(
    *,
    entries: list[dict[str, Any]],
    last_synced_at: Optional[str],
    captures: list[dict[str, Any]],
    events_by_task_id: dict[str, list[dict[str, Any]]],
    recent_events: list[dict[str, Any]],
    now: Optional[datetime] = None,
    pr_state_resolver: Optional[
        Callable[[list[str]], dict[str, str]]
    ] = None,
) -> dict[str, Any]:
    """Pure derive: build the full /api/missions/derived response (pre-filter).

    `missions` + `orphans` (minus the additive orphan-readability keys) match
    the dashboard's MissionListResponse byte-for-byte — that's what § 4 pins.

    `pr_state_resolver` (optional) maps orphan pr_urls → live GitHub states for
    terminal detection (§ 3.4). None → no PR-state read (event-only derive); the
    route injects the real, fail-safe resolver, tests inject a stub. Mission-task
    pr_state always stays None regardless (parity-pinned).
    """
    now = now or datetime.now(timezone.utc)

    registered_task_ids: set[str] = set()
    for entry in entries:
        tids = entry.get('task_ids')
        if isinstance(tids, list):
            registered_task_ids.update(t for t in tids if isinstance(t, str))

    missions: list[dict[str, Any]] = []
    for entry in entries:
        task_ids = entry.get('task_ids')
        task_ids = task_ids if isinstance(task_ids, list) else []
        tasks: list[dict[str, Any]] = []
        for tid in task_ids:
            events = events_by_task_id.get(tid, [])
            summary = summarize_task_events(events)
            tasks.append({
                'task_id': tid,
                'derived_phase': derive_phase_for_task(events, None),
                'last_event_ts': summary['last_event_ts'],
                'pr_url': summary['pr_url'],
                'pr_state': None,
                'agent': summary['agent'],
            })
        mission = dict(entry)  # spread the raw registry entry verbatim
        mission['tasks'] = tasks
        mission['aggregate_phase'] = aggregate_mission_phase(entry, tasks)
        missions.append(mission)

    # Group recent events by task_id (newest-first preserved) for orphan
    # readability, then derive orphans + enrich.
    events_by_orphan: dict[str, list[dict[str, Any]]] = {}
    for ev in recent_events:
        tid = ev.get('task_id')
        if tid:
            events_by_orphan.setdefault(tid, []).append(ev)

    orphans = detect_orphans(recent_events, registered_task_ids)
    # Resolve live PR states for orphans carrying a pr_url so a merged/closed
    # orphan is detected as terminal even with no auto_merge event under its
    # task_id (§ 3.4). Fail-safe: a {} result → event-only derive (pre-fix).
    pr_state_by_url: dict[str, str] = {}
    if pr_state_resolver is not None:
        orphan_pr_urls = [o['pr_url'] for o in orphans if o.get('pr_url')]
        if orphan_pr_urls:
            # Backstop: the resolver is contracted to never raise, but a derive
            # must never 500 — if it does raise, degrade to the event-only path.
            try:
                pr_state_by_url = pr_state_resolver(orphan_pr_urls) or {}
            except Exception:  # noqa: BLE001 — fail-safe: never break the derive
                pr_state_by_url = {}
    for o in orphans:
        o.update(_derive_orphan_readability(
            o, events_by_orphan.get(o['task_id'], []), now,
            pr_state=pr_state_by_url.get(o.get('pr_url')),
        ))

    return {
        'schema_version': 1,
        'missions': missions,
        'orphans': orphans,
        'parked': _parked_from_captures(captures, now),
        'last_synced_at': last_synced_at,
        'as_of': _now_utc_iso(now),
    }


def _apply_derived_filters(
    response: dict[str, Any],
    *,
    repo: Optional[str],
    task_id: Optional[str],
) -> dict[str, Any]:
    """Apply the optional, AND-combined ?repo= / ?task_id= filters (§ 3.2).

    repo narrows missions (entry repo), orphans (derived repo), and parked
    (capture repo). task_id narrows to that task plus its collisions — other
    ACTIVE (non-shipped / non-terminal) tasks on the same repo.
    """
    missions = response['missions']
    orphans = response['orphans']
    parked = response['parked']

    if repo:
        missions = [m for m in missions if m.get('repo') == repo]
        orphans = [o for o in orphans if o.get('repo') == repo]
        parked = [p for p in parked if p.get('repo') == repo]

    if task_id:
        target_repo: Optional[str] = None
        for m in missions:
            for t in m.get('tasks', []):
                if t.get('task_id') == task_id:
                    target_repo = m.get('repo')
        if target_repo is None:
            for o in orphans:
                if o.get('task_id') == task_id:
                    target_repo = o.get('repo')
        # AND with an explicit ?repo= — a mismatch yields no collisions.
        if repo is not None and target_repo is not None and target_repo != repo:
            target_repo = None

        if target_repo is None:
            # Unknown task (or repo mismatch): no active set to report.
            missions = []
            orphans = []
        else:
            collisions: list[dict[str, Any]] = []
            for m in missions:
                if m.get('repo') != target_repo:
                    continue
                kept = [
                    t for t in m.get('tasks', [])
                    if t.get('task_id') == task_id
                    or t.get('derived_phase') != 'shipped'
                ]
                if kept:
                    mm = dict(m)
                    mm['tasks'] = kept
                    collisions.append(mm)
            missions = collisions
            orphans = [
                o for o in orphans
                if o.get('repo') == target_repo
                and (o.get('task_id') == task_id or not o.get('terminal'))
            ]

    response['missions'] = missions
    response['orphans'] = orphans
    response['parked'] = parked
    return response


def _handle_missions_derived(
    *,
    missions_path: Path,
    captures_path: Path,
    supabase_client: Any,
    repo: Optional[str],
    task_id: Optional[str],
    now: Optional[datetime] = None,
    pr_state_resolver: Optional[
        Callable[[list[str]], dict[str, str]]
    ] = None,
) -> dict[str, Any]:
    """Pure handler for GET /api/missions/derived. Reads the registry +
    captures, fetches chain_events, derives, applies filters.

    `pr_state_resolver` defaults to None (no PR-state read) so direct callers
    and unit tests are network-free; the route passes the real
    `_resolve_orphan_pr_states`. See `_build_derived_response`."""
    missions_data = _reader_missions(missions_path)
    captures_data = _reader_captures(captures_path)
    entries = missions_data.get('missions') or []

    registered: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        tids = entry.get('task_ids')
        if not isinstance(tids, list):
            continue
        for tid in tids:
            if isinstance(tid, str) and tid not in seen:
                seen.add(tid)
                registered.append(tid)

    events_by_task_id = _fetch_events_for_task_ids(supabase_client, registered)
    recent_events = _fetch_recent_chain_events(
        supabase_client, _ORPHAN_WINDOW_DAYS, now,
    )

    response = _build_derived_response(
        entries=entries,
        last_synced_at=missions_data.get('last_synced_at'),
        captures=captures_data.get('captures') or [],
        events_by_task_id=events_by_task_id,
        recent_events=recent_events,
        now=now,
        pr_state_resolver=pr_state_resolver,
    )
    return _apply_derived_filters(response, repo=repo, task_id=task_id)


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


# ---------------------------------------------------------------------------
# Missions v2 Phase 0 — desktop session ingest
# (spec: agents/beacon/specs/missions-v2-phase0-desktop-session-feed.md)
#
# Desktop Claude Code sessions have no Supabase creds and must NOT hold the
# all-access service-role key. They POST a desktop_session_* event here; this
# endpoint (on the droplet, which DOES hold the key) writes it via the
# canonical chain_event_emit.emit_event helper. The handler PINS agent to
# 'desktop-claude' and rejects any event_type outside the desktop set, so a
# leaked ingest token can only write desktop-session cards as desktop-claude —
# nothing else.
# ---------------------------------------------------------------------------

DESKTOP_AGENT = 'desktop-claude'
ALLOWED_DESKTOP_EVENT_TYPES = (
    'desktop_session_start',
    'desktop_session_active',
    'desktop_session_done',
)
INGEST_HEADER_NAME = 'X-Ingest-Token'
INGEST_TOKEN_ENV = 'DESKTOP_INGEST_TOKEN'
# Desktop session metadata is tiny (repo/branch/title/host/flags). Cap the
# payload so a leaked ingest token can't bloat chain_events with multi-MB rows
# (the dashboard fetches event payloads with SELECT *). Generous vs. real use.
MAX_DESKTOP_PAYLOAD_BYTES = 16384


class DesktopSessionIngestRequest(BaseModel):
    event_type: str
    task_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class DesktopSessionIngestResponse(BaseModel):
    ok: bool
    event_id: Optional[str] = None


def _expected_ingest_token() -> Optional[str]:
    """Read the ingest token at request time so a restart picks up a rotation."""
    tok = os.environ.get(INGEST_TOKEN_ENV, '').strip()
    return tok or None


def _require_ingest_token(request: Request) -> None:
    """Auth dependency for the desktop ingest endpoint. A DEDICATED token,
    separate from DASHBOARD_API_TOKEN, so the desktop holds a narrow-scope
    secret rather than the read-everything dashboard token."""
    provided = request.headers.get(INGEST_HEADER_NAME)
    if provided is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f'missing {INGEST_HEADER_NAME}',
        )
    expected = _expected_ingest_token()
    if not expected:
        # Server misconfigured — refuse to claim auth passed. Log it: a droplet
        # restarted before its EnvironmentFile is sourced would otherwise 401
        # every desktop ingest silently, and no cards would appear.
        logger.warning(
            'desktop ingest rejected: %s is not configured on this service — '
            'every ingest request 401s until it is set.', INGEST_TOKEN_ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f'invalid {INGEST_HEADER_NAME}',
        )
    if not secrets.compare_digest(
        provided.encode('utf-8'), expected.encode('utf-8')
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f'invalid {INGEST_HEADER_NAME}',
        )


# Test seam: tests monkeypatch this to inject a recording fake emitter so the
# handler can be exercised without supabase-py installed.
def _get_desktop_emit():
    """Return (emit_event, compute_event_id) from the chain-event helpers.

    Lazy import (mirrors _import_chain_event_helpers) so dashboard_api loads
    on hosts without supabase-py.
    """
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import chain_event_emit  # noqa: PLC0415
    import chain_event_shipper as ces  # noqa: PLC0415
    return chain_event_emit.emit_event, ces.compute_event_id


def _handle_desktop_session_ingest(
    *,
    event_type: str,
    task_id: Optional[str],
    payload: dict[str, Any],
    now: Optional[datetime] = None,
    emit_resolver: Any = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/ingest/desktop-session.

    Validates event_type, pins agent='desktop-claude', writes the row via
    emit_event. Returns {'ok', 'event_id'}; the route maps ok=False to 502.
    Raises HTTPException(400) on a bad event_type / non-object payload.
    """
    if event_type not in ALLOWED_DESKTOP_EVENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid event_type={event_type!r}',
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='payload must be a JSON object',
        )
    try:
        payload_bytes = len(json.dumps(payload).encode('utf-8'))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='payload is not JSON-serializable',
        )
    if payload_bytes > MAX_DESKTOP_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f'payload too large ({payload_bytes} > {MAX_DESKTOP_PAYLOAD_BYTES} bytes)',
        )
    emit_event, compute_event_id = (emit_resolver or _get_desktop_emit)()
    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()
    # agent is PINNED here — the request body has no agent field, so a caller
    # cannot emit as forge/mirror/beacon. event_id is deterministic over
    # (task_id, event_type, ts); the same ts feeds emit_event so the returned
    # id matches the written row.
    ok = emit_event(
        event_type=event_type,
        agent=DESKTOP_AGENT,
        task_id=task_id,
        payload=payload,
        ts=ts_iso,
    )
    event_id = compute_event_id(task_id, event_type, ts_iso) if ok else None
    return {'ok': bool(ok), 'event_id': event_id}


# ---------------------------------------------------------------------------
# POST /api/ingest/capture — durable one-gesture capture (Missions v2 Phase 1)
# (spec: agents/beacon/specs/missions-v2-phase1-durable-capture.md § 4)
#
# Reuses the SAME X-Ingest-Token as the desktop-session ingest (the desktop
# holds no DB/git creds). Unlike that endpoint — which emits a chain_event —
# a capture is appended atomically to the in-repo `agents/beacon/captures.json`
# (tmp+rename) under a lock and is NOT turned into a PR: a PR-per-capture is
# too heavy for a low-ceremony, multiple-per-day gesture. Durability + audit
# come from the GC healer's batched commit (§ 6), which version-controls any
# captures.json delta on its timer. The endpoint pins `source` to a fixed set
# so a leaked ingest token can only write known capture sources.
# ---------------------------------------------------------------------------

CAPTURE_DEFAULT_SOURCE = 'desktop-chat'
# Fixed set the server pins `source` to. The desktop gesture ships
# 'desktop-chat'; the others are admitted now so future Telegram/agent capture
# sources (spec § 9) need no schema change, only an emitter.
CAPTURE_ALLOWED_SOURCES = ('desktop-chat', 'telegram', 'agent')
# Frozen allowlist of capture labels, beside CAPTURE_ALLOWED_SOURCES (spec
# park-the-nudge § 4). An optional first-class `label` tags a capture's
# provenance — e.g. a recurring Pulse Check I proposal parked in the Missions
# lane. Same threat model as the source pin: a leaked ingest token may only
# write KNOWN labels. V1 member: 'pulse-check-i'; a new label is one tuple
# entry, no schema change.
CAPTURE_ALLOWED_LABELS = ('pulse-check-i',)
# captures.json registry schema. v2 adds the optional first-class `label`
# field on each capture record; v1 records read back as label-absent (→ None).
CAPTURES_SCHEMA_VERSION = 2
# A capture is tiny (title + a sentence of note + origin). Cap it so a leaked
# ingest token can't bloat captures.json with a multi-MB row. Spec § 4: 413.
MAX_CAPTURE_PAYLOAD_BYTES = 4096
# Re-POST collapse window: an identical (title, origin.session_id) within this
# many seconds maps onto the existing capture id rather than double-parking.
CAPTURE_IDEMPOTENCY_WINDOW_SEC = 600
# Cap the slug so a long title can't produce an unwieldy id.
_CAPTURE_SLUG_MAX = 48

# Serializes the read-modify-write of captures.json within the single uvicorn
# worker so concurrent POSTs can't lose an append (last-writer-wins on the
# whole file). The atomic tmp+rename guards a reader from seeing a partial file.
_CAPTURE_INGEST_LOCK = __import__('threading').Lock()


class CaptureIngestRequest(BaseModel):
    title: str
    note: Optional[str] = None
    origin: dict[str, Any] = Field(default_factory=dict)
    label: Optional[str] = None


class CaptureIngestResponse(BaseModel):
    ok: bool
    capture_id: Optional[str] = None


# Test seam: tests monkeypatch this to make generated ids deterministic.
def _gen_capture_suffix() -> str:
    """Short random suffix for a capture id (`cap-<slug>-<suffix>`)."""
    return secrets.token_hex(2)


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string to an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_snoozed(cap: dict[str, Any], now: datetime) -> bool:
    """True iff the capture is snoozed past `now` (`snoozed_until` in the
    future). A null / absent / unparseable / past `snoozed_until` → not snoozed.
    Fail-open to VISIBLE: a bad value never hides a capture (§ 4.3)."""
    until = _parse_iso_utc(cap.get('snoozed_until'))
    return until is not None and until > now


def _read_captures_registry(captures_path: Path) -> dict[str, Any]:
    """Load captures.json as a mutable registry dict ({schema_version,
    captures}). Missing file → a fresh empty registry. Malformed JSON →
    HTTPException(500), so the write path never appends onto a corrupt file."""
    if not captures_path.exists():
        return {'schema_version': CAPTURES_SCHEMA_VERSION, 'captures': []}
    try:
        raw = captures_path.read_text()
        data = (json.loads(raw) if raw.strip()
                else {'schema_version': CAPTURES_SCHEMA_VERSION, 'captures': []})
    except (OSError, json.JSONDecodeError) as e:
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={'error': 'captures.json malformed', 'detail': first_line},
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'captures.json malformed',
                'detail': 'top-level JSON is not an object',
            },
        )
    if not isinstance(data.get('captures'), list):
        data['captures'] = []
    data.setdefault('schema_version', CAPTURES_SCHEMA_VERSION)
    return data


def _atomic_write_captures(path: Path, registry: dict[str, Any]) -> None:
    """Write `registry` to captures.json atomically (tmp in the same dir +
    os.replace). A unique tmp name keeps concurrent writers from clobbering a
    shared temp file even though the lock already serializes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(registry, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _find_recent_capture(
    captures: list[Any], title: str, session_id: Any,
    now: datetime, window_sec: int,
) -> Optional[dict[str, Any]]:
    """Return the most recent capture matching (title, origin.session_id)
    captured within `window_sec` of `now`, or None. Used to collapse a
    duplicate re-POST onto an existing id."""
    for cap in reversed(captures):
        if not isinstance(cap, dict) or cap.get('title') != title:
            continue
        cap_origin = cap.get('origin') if isinstance(cap.get('origin'), dict) else {}
        if cap_origin.get('session_id') != session_id:
            continue
        captured = _parse_iso_utc(cap_origin.get('captured_at'))
        if captured is None:
            continue
        if 0 <= (now - captured).total_seconds() <= window_sec:
            return cap
    return None


def _unique_capture_id(
    title: str, captures: list[Any], suffix_gen: Any,
) -> str:
    """Generate `cap-<kebab(title)>-<suffix>` not already present in
    `captures`. Falls back to a count-disambiguated form if the suffix
    generator keeps colliding (e.g. a deterministic test stub)."""
    slug = _kebab_case(title)[:_CAPTURE_SLUG_MAX].strip('-') or 'capture'
    existing = {c.get('id') for c in captures if isinstance(c, dict)}
    for _ in range(50):
        cid = f'cap-{slug}-{suffix_gen()}'
        if cid not in existing:
            return cid
    return f'cap-{slug}-{len(captures)}-{suffix_gen()}'


def _handle_capture_ingest(
    *,
    title: Any,
    note: Any,
    origin: Any,
    label: Any = None,
    captures_path: Path,
    now: Optional[datetime] = None,
    window_sec: int = CAPTURE_IDEMPOTENCY_WINDOW_SEC,
    suffix_gen: Any = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/ingest/capture.

    Validates the body, pins `source`, generates a `cap-…` id, sets
    state='parked', and atomically appends to captures.json under a lock.
    Idempotent: a re-POST with the same (title, origin.session_id) inside
    `window_sec` returns the existing id without double-parking. Raises
    HTTPException(400) on a bad body and (413) over the size cap.
    """
    title = title.strip() if isinstance(title, str) else ''
    if not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='title is required and must be non-empty',
        )
    if note is not None and not isinstance(note, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='note must be a string',
        )
    if not isinstance(origin, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='origin must be a JSON object',
        )
    source = origin.get('source') or CAPTURE_DEFAULT_SOURCE
    if source not in CAPTURE_ALLOWED_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid origin.source={source!r}',
        )
    # `label` is optional and allowlisted (spec § 4): None is back-compat
    # (desktop/telegram captures carry none); a non-None value must be known so
    # a leaked ingest token can only write a recognized label.
    if label is not None and label not in CAPTURE_ALLOWED_LABELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid label={label!r}',
        )
    try:
        body_bytes = len(json.dumps(
            {'title': title, 'note': note, 'origin': origin, 'label': label},
        ).encode('utf-8'))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='capture is not JSON-serializable',
        )
    if body_bytes > MAX_CAPTURE_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f'capture too large ({body_bytes} > {MAX_CAPTURE_PAYLOAD_BYTES} bytes)',
        )

    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()
    session_id = origin.get('session_id')
    suffix_gen = suffix_gen or _gen_capture_suffix

    with _CAPTURE_INGEST_LOCK:
        registry = _read_captures_registry(captures_path)
        captures = registry['captures']
        existing = _find_recent_capture(captures, title, session_id, now, window_sec)
        if existing is not None:
            return {'ok': True, 'capture_id': existing.get('id')}
        capture_id = _unique_capture_id(title, captures, suffix_gen)
        captures.append({
            'id': capture_id,
            'title': title,
            'note': note or '',
            'state': 'parked',
            'label': label,
            'origin': {
                'source': source,
                'session_id': session_id,
                'repo': origin.get('repo'),
                'branch': origin.get('branch'),
                'captured_at': ts_iso,
            },
            'last_touched': ts_iso,
            'promoted_to': None,
        })
        # Stamp the current schema on every write so a pre-existing v1 file is
        # upgraded in place the first time a capture is appended.
        registry['schema_version'] = CAPTURES_SCHEMA_VERSION
        _atomic_write_captures(captures_path, registry)
    return {'ok': True, 'capture_id': capture_id}


# ---------------------------------------------------------------------------
# Missions v2 Phase 3 — capture write-back (POST /api/missions/captures/{id}/action)
# (spec: agents/beacon/specs/missions-v2-phase3-writeback-autoregister.md § 4)
#
# `promote` and `drop` are PR-backed: they reuse the _handle_new_mission
# GitHub-REST mechanism (GET main ref → create branch → PUT contents → POST PR)
# via the shared _open_registry_pr helper, so the LOCAL missions.json/captures.json
# are never mutated — they update via `git pull` on merge (no drift in the shared
# checkout). `promote` PUTs BOTH files onto one branch so the new mission entry
# and the capture's promoted_to/state land atomically in ONE PR.
#
# `snooze` is the exception: a trivial, reversible date field that routes DIRECT
# through the single captures.json committer primitives (_CAPTURE_INGEST_LOCK +
# _atomic_write_captures — the SAME writer the ingest path uses, NOT a second
# one), per the § 4.3 decision. It returns {applied: true} rather than a PR.

_MISSIONS_REPO_REL = 'agents/beacon/missions.json'
_CAPTURES_REPO_REL = 'agents/beacon/captures.json'


def _open_registry_pr(
    *,
    branch: str,
    title: str,
    pr_body: str,
    files: list[tuple[str, dict[str, Any]]],
    token: str,
    repo_full: str,
) -> str:
    """Open ONE PR editing one or more in-repo JSON files (the generalized
    _handle_new_mission mechanism). `files` is a list of (repo-relative path,
    new registry dict); each is PUT onto the same fresh branch, so they land
    atomically in a single PR. Returns the PR html_url. Raises HTTPException on
    any GitHub error (502), a duplicate branch (409), or an empty html_url.

    The caller computes the mutated registries from a local read; this helper
    never touches the local working copy — the merge `git pull` does."""
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
            detail={'error': 'github main ref missing sha', 'detail': ''},
        )

    # 2. Create the branch — atomic at GitHub. 422 → already exists → 409.
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
                'hint': 'An action PR for this capture is already in flight.',
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

    # 3+4. For each file: read its current blob sha on the branch (inherits
    #      main's content), then PUT the replacement onto the branch.
    for rel_path, registry in files:
        contents_get = _github_api_request(
            'GET', f'{api_base}/contents/{rel_path}?ref={branch}',
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
                    'path': rel_path,
                    'detail': f'status={contents_get.status_code}',
                },
            )
        new_text = json.dumps(registry, indent=2) + '\n'
        put_body: dict[str, Any] = {
            'message': title,
            'content': base64.b64encode(
                new_text.encode('utf-8'),
            ).decode('ascii'),
            'branch': branch,
        }
        if file_sha:
            put_body['sha'] = file_sha
        put_resp = _github_api_request(
            'PUT', f'{api_base}/contents/{rel_path}',
            headers=api_headers, json_body=put_body,
        )
        if put_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    'error': 'github put contents failed',
                    'path': rel_path,
                    'detail': f'status={put_resp.status_code}',
                },
            )

    # 5. Open the PR.
    pr_resp = _github_api_request(
        'POST', f'{api_base}/pulls', headers=api_headers,
        json_body={'title': title, 'head': branch, 'base': 'main', 'body': pr_body},
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
            detail={'error': 'github create pr returned no html_url', 'detail': ''},
        )
    return pr_url


def _find_capture(captures: list[Any], capture_id: str) -> dict[str, Any]:
    """Return the capture dict with id == capture_id, or 404."""
    for cap in captures:
        if isinstance(cap, dict) and cap.get('id') == capture_id:
            return cap
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={'error': 'capture not found', 'capture_id': capture_id},
    )


def _require_parked(cap: dict[str, Any], capture_id: str) -> None:
    """Guard: write-back actions only apply to a still-parked capture. A
    promoted/dropped capture is state-terminal — re-acting is a 409 (also the
    idempotency guard against a double-click re-opening a second PR)."""
    state = cap.get('state')
    if state != 'parked':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                'error': 'capture not actionable',
                'capture_id': capture_id,
                'state': state,
                'hint': 'only parked captures can be promoted/dropped/snoozed',
            },
        )


def _handle_capture_snooze(
    *,
    capture_id: str,
    snoozed_until: Any,
    captures_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """`snooze` — set or clear a capture's `snoozed_until` (§ 4.3). DIRECT write
    via the single captures.json committer (NOT PR-backed): `snoozed_until=None`
    clears the snooze; an ISO-8601 datetime defers resurfacing until it passes.
    Returns {applied: True, snoozed_until}. 404 if no such capture; 409 if it
    isn't parked; 400 on a malformed or non-future date."""
    now = now or datetime.now(timezone.utc)
    parsed: Optional[datetime] = None
    if snoozed_until is not None:
        parsed = _parse_iso_utc(snoozed_until)
        if parsed is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='snoozed_until must be an ISO-8601 datetime or null',
            )
        if parsed <= now:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='snoozed_until must be in the future',
            )

    with _CAPTURE_INGEST_LOCK:
        registry = _read_captures_registry(captures_path)
        cap = _find_capture(registry['captures'], capture_id)
        _require_parked(cap, capture_id)
        cap['snoozed_until'] = parsed.isoformat() if parsed else None
        registry['schema_version'] = CAPTURES_SCHEMA_VERSION
        _atomic_write_captures(captures_path, registry)
    return {'applied': True, 'snoozed_until': cap['snoozed_until']}


def _handle_capture_promote(
    *,
    capture_id: str,
    overrides: dict[str, Any],
    captures_path: Path,
    missions_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """`promote` — capture → mission (§ 4.1). Opens ONE PR editing BOTH
    missions.json (new `phase: drafting` entry) and captures.json (the capture's
    `promoted_to` + `state: promoted`) so they land atomically. The local files
    are NOT mutated. Optional overrides: name / brief / repo / spec_docs
    (defaults inferred from the capture). Returns {pr_url, branch, mission_id}."""
    now = now or datetime.now(timezone.utc)
    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'no GITHUB_TOKEN env nor gh auth token on dashboard-api host',
            },
        )
    repo_full = _missions_repo_full()

    with _NEW_MISSION_LOCK:
        cap_registry = _read_captures_registry(captures_path)
        cap = _find_capture(cap_registry['captures'], capture_id)
        _require_parked(cap, capture_id)

        cap_origin = cap.get('origin') if isinstance(cap.get('origin'), dict) else {}
        name = (overrides.get('name') or cap.get('title') or '').strip()
        mission_id = _kebab_case(name)
        if not mission_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    'error': 'invalid mission name',
                    'detail': 'name (override or capture title) kebab-cases to empty',
                },
            )

        missions_registry = _read_missions_registry(missions_path)
        for existing in missions_registry['missions']:
            if isinstance(existing, dict) and existing.get('id') == mission_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        'error': 'mission_id collision',
                        'id': mission_id,
                        'existing_entry_brief': existing.get('brief', ''),
                    },
                )

        brief = (
            overrides.get('brief')
            or cap.get('note')
            or cap.get('title')
            or ''
        )
        repo = overrides.get('repo') or cap_origin.get('repo') or ''
        spec_docs = overrides.get('spec_docs') or []
        new_entry: dict[str, Any] = {
            'id': mission_id,
            'name': name,
            'phase': 'drafting',
            'brief': brief,
            'spec_docs': list(spec_docs),
            'task_ids': [],
            'repo': repo,
            'created': now.date().isoformat(),
            'deferred_reason': None,
        }
        updated_missions = {
            'schema_version': missions_registry.get('schema_version', 1),
            'missions': missions_registry['missions'] + [new_entry],
        }

        # Mutate the in-memory capture (never written to disk locally — only PUT
        # to the branch): promoted_to + terminal state.
        cap['promoted_to'] = mission_id
        cap['state'] = 'promoted'
        cap_registry['schema_version'] = CAPTURES_SCHEMA_VERSION

        branch = f'feat/promote-capture-{capture_id}'
        title = f'chore(missions): promote capture {capture_id} -> mission {mission_id}'
        pr_body = '\n'.join([
            f'Promote capture `{capture_id}` to mission `{mission_id}`.',
            '',
            f'**Brief:** {brief}',
            '',
            'Edits both `missions.json` (new `drafting` entry) and '
            '`captures.json` (`promoted_to` + `state: promoted`) in one PR so '
            'they land atomically. No dispatch — Larry dispatches the mission '
            '(Missions v2 Phase 3 § 4.1).',
        ])
        pr_url = _open_registry_pr(
            branch=branch,
            title=title,
            pr_body=pr_body,
            files=[
                (_MISSIONS_REPO_REL, updated_missions),
                (_CAPTURES_REPO_REL, cap_registry),
            ],
            token=token,
            repo_full=repo_full,
        )

    return {'pr_url': pr_url, 'branch': branch, 'mission_id': mission_id}


def _handle_capture_drop(
    *,
    capture_id: str,
    reason: Any,
    captures_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """`drop` — retire a capture (§ 4.2). PR-backed (auditable — never a silent
    delete): sets `state: dropped` (+ optional `drop_reason`). The local file is
    NOT mutated. Returns {pr_url, branch}."""
    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'no GITHUB_TOKEN env nor gh auth token on dashboard-api host',
            },
        )
    repo_full = _missions_repo_full()
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='reason must be a string',
        )

    with _NEW_MISSION_LOCK:
        cap_registry = _read_captures_registry(captures_path)
        cap = _find_capture(cap_registry['captures'], capture_id)
        _require_parked(cap, capture_id)

        cap['state'] = 'dropped'
        if reason:
            cap['drop_reason'] = reason
        cap_registry['schema_version'] = CAPTURES_SCHEMA_VERSION

        branch = f'chore/drop-capture-{capture_id}'
        title = f'chore(missions): drop capture {capture_id}'
        pr_body = '\n'.join([
            f'Drop capture `{capture_id}` (`state: dropped`).',
            *(['', f'**Reason:** {reason}'] if reason else []),
            '',
            'Auditable retire — the GC healer moves dropped captures to the '
            'collapsed lane (Missions v2 Phase 3 § 4.2).',
        ])
        pr_url = _open_registry_pr(
            branch=branch,
            title=title,
            pr_body=pr_body,
            files=[(_CAPTURES_REPO_REL, cap_registry)],
            token=token,
            repo_full=repo_full,
        )

    return {'pr_url': pr_url, 'branch': branch}


def _handle_capture_action(
    *,
    capture_id: str,
    action: str,
    args: dict[str, Any],
    captures_path: Path,
    missions_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Dispatch POST /api/missions/captures/{id}/action to the per-action
    handler. `promote`/`drop` are PR-backed → {pr_url, branch}; `snooze` is a
    direct committer write → {applied: True, snoozed_until}. Unknown action →
    400."""
    if action == 'promote':
        return _handle_capture_promote(
            capture_id=capture_id,
            overrides={
                k: args[k] for k in ('name', 'brief', 'repo', 'spec_docs')
                if k in args and args[k] is not None
            },
            captures_path=captures_path,
            missions_path=missions_path,
            now=now,
        )
    if action == 'drop':
        return _handle_capture_drop(
            capture_id=capture_id,
            reason=args.get('reason'),
            captures_path=captures_path,
            now=now,
        )
    if action == 'snooze':
        return _handle_capture_snooze(
            capture_id=capture_id,
            snoozed_until=args.get('snoozed_until'),
            captures_path=captures_path,
            now=now,
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f'invalid action={action!r}; expected promote|drop|snooze',
    )


# Missions v2 Phase 3 — mission write-back (POST /api/system/missions/{id}/action)
# (spec: agents/beacon/specs/missions-v2-phase3-writeback-autoregister.md § 5)
#
# defer / resume / reprioritize are ALL PR-backed (missions.json is the curated
# registry — every change auditable). Each is a single-field edit via the shared
# _open_registry_pr helper (the generalized _handle_new_mission mechanism), so the
# LOCAL missions.json is never mutated — it updates via `git pull` on merge.
#
#   defer        — phase: deferred + deferred_reason. The derive's
#                  aggregate_mission_phase already treats deferred as a mission-
#                  level override (Phase 2 § 3.4), so NO new derive logic is
#                  needed — the board reflects it on merge.
#   resume       — clear the override (phase back to drafting; deferred_reason
#                  null). The derive recomputes the real phase from the mission's
#                  tasks once the override is gone.
#   reprioritize — set the additive optional `priority` int (absent = default);
#                  drives board row order (a thin sort, no new lane). null clears.


def _find_mission(missions: list[Any], mission_id: str) -> dict[str, Any]:
    """Return the mission dict with id == mission_id, or 404."""
    for mission in missions:
        if isinstance(mission, dict) and mission.get('id') == mission_id:
            return mission
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={'error': 'mission not found', 'mission_id': mission_id},
    )


def _handle_mission_defer(
    *,
    mission_id: str,
    reason: Any,
    missions_path: Path,
) -> dict[str, Any]:
    """`defer` — set a mission's `phase: deferred` + `deferred_reason` (§ 5).
    PR-backed (missions.json only). 404 if no such mission; 409 if already
    deferred (idempotency guard against a double-click reopening a second PR);
    400 if reason is non-string. Returns {pr_url, branch}."""
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='reason must be a string',
        )
    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'no GITHUB_TOKEN env nor gh auth token on dashboard-api host',
            },
        )
    repo_full = _missions_repo_full()

    with _NEW_MISSION_LOCK:
        registry = _read_missions_registry(missions_path)
        mission = _find_mission(registry['missions'], mission_id)
        if mission.get('phase') == 'deferred':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'mission already deferred',
                    'mission_id': mission_id,
                    'hint': 'resume it before deferring again',
                },
            )

        mission['phase'] = 'deferred'
        mission['deferred_reason'] = reason if reason else None

        branch = f'chore/defer-mission-{mission_id}'
        title = f'chore(missions): defer mission {mission_id}'
        pr_body = '\n'.join([
            f'Defer mission `{mission_id}` (`phase: deferred`).',
            *(['', f'**Reason:** {reason}'] if reason else []),
            '',
            'Single-field registry edit. The derive already treats `deferred` as '
            'a mission-level override, so the board reflects it on merge with no '
            'new derive logic (Missions v2 Phase 3 § 5).',
        ])
        pr_url = _open_registry_pr(
            branch=branch,
            title=title,
            pr_body=pr_body,
            files=[(_MISSIONS_REPO_REL, registry)],
            token=token,
            repo_full=repo_full,
        )

    return {'pr_url': pr_url, 'branch': branch}


def _handle_mission_resume(
    *,
    mission_id: str,
    missions_path: Path,
) -> dict[str, Any]:
    """`resume` — clear a mission's deferred override (§ 5). Resets `phase` to
    `drafting` (the base hint — the derive recomputes the real phase from the
    mission's tasks once the override is gone) and drops `deferred_reason`.
    PR-backed (missions.json only). 404 if no such mission; 409 if not deferred
    (nothing to resume). Returns {pr_url, branch}."""
    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'no GITHUB_TOKEN env nor gh auth token on dashboard-api host',
            },
        )
    repo_full = _missions_repo_full()

    with _NEW_MISSION_LOCK:
        registry = _read_missions_registry(missions_path)
        mission = _find_mission(registry['missions'], mission_id)
        if mission.get('phase') != 'deferred':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'mission not deferred',
                    'mission_id': mission_id,
                    'phase': mission.get('phase'),
                    'hint': 'only a deferred mission can be resumed',
                },
            )

        mission['phase'] = 'drafting'
        mission['deferred_reason'] = None

        branch = f'chore/resume-mission-{mission_id}'
        title = f'chore(missions): resume mission {mission_id}'
        pr_body = '\n'.join([
            f'Resume mission `{mission_id}` — clears the `deferred` override '
            '(`phase: drafting`, `deferred_reason: null`).',
            '',
            'Single-field registry edit. The derive recomputes the real phase '
            'from the mission\'s tasks once the override is gone (Missions v2 '
            'Phase 3 § 5).',
        ])
        pr_url = _open_registry_pr(
            branch=branch,
            title=title,
            pr_body=pr_body,
            files=[(_MISSIONS_REPO_REL, registry)],
            token=token,
            repo_full=repo_full,
        )

    return {'pr_url': pr_url, 'branch': branch}


def _handle_mission_reprioritize(
    *,
    mission_id: str,
    priority: Any,
    missions_path: Path,
) -> dict[str, Any]:
    """`reprioritize` — set a mission's additive optional `priority` int (§ 5).
    Drives board row ordering (a thin sort; no new lane). `priority=null` clears
    it (back to default). PR-backed (missions.json only). 404 if no such mission;
    400 if priority is neither an int nor null. Returns {pr_url, branch}."""
    # bool is an int subclass — reject it explicitly so `true`/`false` don't slip
    # through as 1/0.
    if priority is not None and (
        isinstance(priority, bool) or not isinstance(priority, int)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='priority must be an integer or null',
        )
    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'no GITHUB_TOKEN env nor gh auth token on dashboard-api host',
            },
        )
    repo_full = _missions_repo_full()

    with _NEW_MISSION_LOCK:
        registry = _read_missions_registry(missions_path)
        mission = _find_mission(registry['missions'], mission_id)

        if priority is None:
            mission.pop('priority', None)
            priority_label = 'default'
        else:
            mission['priority'] = priority
            priority_label = str(priority)

        branch = f'chore/reprioritize-mission-{mission_id}'
        title = (
            f'chore(missions): reprioritize mission {mission_id} '
            f'-> {priority_label}'
        )
        pr_body = '\n'.join([
            f'Reprioritize mission `{mission_id}` (`priority: {priority_label}`).',
            '',
            'Single-field additive registry edit. `priority` drives board row '
            'ordering (a thin sort; absent = default). null clears it back to '
            'default (Missions v2 Phase 3 § 5).',
        ])
        pr_url = _open_registry_pr(
            branch=branch,
            title=title,
            pr_body=pr_body,
            files=[(_MISSIONS_REPO_REL, registry)],
            token=token,
            repo_full=repo_full,
        )

    return {'pr_url': pr_url, 'branch': branch}


def _handle_mission_accept(
    *,
    mission_id: str,
    missions_path: Path,
) -> dict[str, Any]:
    """`accept` — claim an auto-proposed orphan thread into a real mission (§ 6).
    Flips `phase: proposed -> drafting` (the orphan's task_id is already in the
    entry's `task_ids` from when the healer proposed it; the flip graduates the
    proposal into a drafting mission). PR-backed (missions.json only) — mirrors
    `resume`'s shape. 404 if no such mission; 409 if not proposed (nothing to
    accept). Returns {pr_url, branch}."""
    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'no GITHUB_TOKEN env nor gh auth token on dashboard-api host',
            },
        )
    repo_full = _missions_repo_full()

    with _NEW_MISSION_LOCK:
        registry = _read_missions_registry(missions_path)
        mission = _find_mission(registry['missions'], mission_id)
        if mission.get('phase') != 'proposed':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'mission not proposed',
                    'mission_id': mission_id,
                    'phase': mission.get('phase'),
                    'hint': 'only a proposed mission can be accepted',
                },
            )

        mission['phase'] = 'drafting'

        branch = f'chore/accept-mission-{mission_id}'
        title = f'chore(missions): accept proposed mission {mission_id}'
        pr_body = '\n'.join([
            f'Accept proposed mission `{mission_id}` — claims the orphan into a '
            'drafting mission (`phase: proposed -> drafting`).',
            '',
            'Single-field registry edit. The orphan\'s task_id is already in the '
            'entry\'s `task_ids` (the healer registered it on propose); accepting '
            'graduates the proposal into a real mission the derive ranks normally '
            '(Missions v2 Phase 3 § 6).',
        ])
        pr_url = _open_registry_pr(
            branch=branch,
            title=title,
            pr_body=pr_body,
            files=[(_MISSIONS_REPO_REL, registry)],
            token=token,
            repo_full=repo_full,
        )

    return {'pr_url': pr_url, 'branch': branch}


def _handle_mission_dismiss(
    *,
    mission_id: str,
    missions_path: Path,
) -> dict[str, Any]:
    """`dismiss` — acknowledge an auto-proposed orphan thread so the board stops
    surfacing it (§ 6). Sets the ADDITIVE `acknowledged: true` flag; `phase`
    STAYS `proposed`. PR-backed (missions.json only). 404 if no such mission;
    409 if not proposed (only a proposed thread is dismissable). Returns
    {pr_url, branch}.

    Re-proposal suppression is structural: the proposed entry persists, so its
    task_id stays registered and the autoregister healer's detect_orphans never
    re-surfaces it (heal_orphan_autoregister.registered_task_ids). `acknowledged`
    is additive metadata the dashboard's Proposed affordance reads to hide the
    dismissed thread — it is NOT consulted by the healer."""
    token = _github_token()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'github token missing',
                'detail': 'no GITHUB_TOKEN env nor gh auth token on dashboard-api host',
            },
        )
    repo_full = _missions_repo_full()

    with _NEW_MISSION_LOCK:
        registry = _read_missions_registry(missions_path)
        mission = _find_mission(registry['missions'], mission_id)
        if mission.get('phase') != 'proposed':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'mission not proposed',
                    'mission_id': mission_id,
                    'phase': mission.get('phase'),
                    'hint': 'only a proposed mission can be dismissed',
                },
            )

        mission['acknowledged'] = True

        branch = f'chore/dismiss-mission-{mission_id}'
        title = f'chore(missions): dismiss proposed mission {mission_id}'
        pr_body = '\n'.join([
            f'Dismiss proposed mission `{mission_id}` — sets `acknowledged: true` '
            '(`phase` stays `proposed`).',
            '',
            'Single-field additive registry edit. The proposed entry persists '
            '(its task_id stays registered), so the autoregister healer already '
            'never re-proposes it; `acknowledged` lets the board hide the '
            'dismissed thread from the Proposed affordance (Missions v2 Phase 3 '
            '§ 6).',
        ])
        pr_url = _open_registry_pr(
            branch=branch,
            title=title,
            pr_body=pr_body,
            files=[(_MISSIONS_REPO_REL, registry)],
            token=token,
            repo_full=repo_full,
        )

    return {'pr_url': pr_url, 'branch': branch}


def _handle_mission_action(
    *,
    mission_id: str,
    action: str,
    args: dict[str, Any],
    missions_path: Path,
) -> dict[str, Any]:
    """Dispatch POST /api/system/missions/{id}/action to the per-action handler.
    defer / resume / reprioritize / accept / dismiss are all PR-backed →
    {pr_url, branch}. Unknown action → 400."""
    if action == 'defer':
        return _handle_mission_defer(
            mission_id=mission_id,
            reason=args.get('reason'),
            missions_path=missions_path,
        )
    if action == 'resume':
        return _handle_mission_resume(
            mission_id=mission_id,
            missions_path=missions_path,
        )
    if action == 'reprioritize':
        return _handle_mission_reprioritize(
            mission_id=mission_id,
            priority=args.get('priority'),
            missions_path=missions_path,
        )
    if action == 'accept':
        return _handle_mission_accept(
            mission_id=mission_id,
            missions_path=missions_path,
        )
    if action == 'dismiss':
        return _handle_mission_dismiss(
            mission_id=mission_id,
            missions_path=missions_path,
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f'invalid action={action!r}; expected '
            'defer|resume|reprioritize|accept|dismiss'
        ),
    )


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
        from supabase_factory import get_supabase_client  # type: ignore  # noqa: PLC0415
        return get_supabase_client(url, key)
    except ImportError:
        return None


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


def _medic_silence_fingerprint(source: dict[str, Any]) -> Optional[str]:
    """Return the fingerprint iff `source` is a Medic-originated silence
    decision: an `approval_request` with `payload.proposing_agent == 'medic'`
    and a non-empty `payload.fingerprint`.

    These decisions are reconciled DIRECTLY by the dashboard rather than routed
    to an agent's inbox. The reject's only effect is a pure suppression-file
    operation (`larry_alerts.unsilence`), and there is no live Medic inbox
    executor for these envelopes — routing a generic 'follow the suggested
    envelope' prompt to Beacon (the legacy approval_request path) silently
    drops Medic's instruction, so the silence was never actually lifted.
    Performing it server-side also matches the alert-toil principle: push
    reconciliation down, don't make an agent interpret-and-act."""
    if source.get('event_type') != 'approval_request':
        return None
    payload = source.get('payload')
    if not isinstance(payload, dict):
        return None
    if payload.get('proposing_agent') != 'medic':
        return None
    fp = payload.get('fingerprint')
    return fp if isinstance(fp, str) and fp else None


def _reconcile_medic_silence(fingerprint: str, action: str) -> dict[str, Any]:
    """Apply Larry's decision on a Medic silence directly, server-side, and
    return an audit-detail dict.

      reject  -> larry_alerts.unsilence(fp): lift the silence so the alert
                 fires again (the root cause is unfixed).
      approve -> no-op: keep the silence in place.

    Best-effort: a missing/failed unsilence is recorded in the audit detail,
    never raised, so a dashboard click can't 500 on it. Lazy import keeps
    larry_alerts out of the module import path and lets tests patch it."""
    detail: dict[str, Any] = {'fingerprint': fingerprint}
    if action == 'reject':
        try:
            import larry_alerts
            removed = larry_alerts.unsilence(fingerprint)
            detail['medic_reconcile'] = 'unsilenced' if removed else 'unsilence-noop'
        except Exception as e:  # noqa: BLE001 — never 500 the action
            detail['medic_reconcile'] = f'unsilence-error:{type(e).__name__}'
    else:  # approve
        detail['medic_reconcile'] = 'kept-silenced'
    return detail


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
    # except mark_done (which is idempotent against the read state). This is a
    # cheap fast-fail; the AUTHORITATIVE mutex is the atomic compare-and-set
    # below. nervous-system-audit #10 (2026-06-05): /api/larry/action is a
    # synchronous endpoint, so Starlette runs it in a threadpool — two
    # concurrent approve/reject clicks on the same event can BOTH pass this
    # check-then-act read and BOTH write a dispatch envelope (duplicate agent
    # run). The check and the read_at flip were not atomic; they are now.
    if source.get('read_at') is not None and action != 'mark_done':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='source event already acted on',
        )

    envelope_written: Optional[str] = None
    target_agent: Optional[str] = None
    reconcile_detail: dict[str, Any] = {}

    # Decide the action's side-effect plan FIRST — this is pure (no DB write,
    # no inbox write). The 400 path-injection guards live here so a malformed
    # request fails BEFORE the atomic claim below, never churning read_at.
    medic_fp = _medic_silence_fingerprint(source)
    is_medic_silence = medic_fp is not None and action in ('approve', 'reject')
    envelope_candidate: Optional[Path] = None
    envelope_payload: Optional[dict[str, Any]] = None
    if not is_medic_silence and action != 'mark_done':
        target_agent, filename, envelope_payload = _build_envelope_for_action(
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
        envelope_candidate = (agent_inbox / filename).resolve()
        if envelope_candidate.parent != agent_inbox.resolve():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='invalid envelope filename',
            )

    # #10: CLAIM the event atomically BEFORE any side effect. The conditional
    # UPDATE flips read_at NULL→ts only if it is still NULL; only the winning
    # request gets a non-empty result, so a loser of a concurrent race → 409
    # and never writes the envelope / reconciles the silence (the side effect
    # fires at most once). We accept EITHER the returned rows (`data`, present
    # with the default `returning=representation`) OR the affected-row `count`
    # (requested below) as proof of the claim, so the CAS does not silently
    # break if the client is ever built with `returning=minimal`. mark_done is
    # idempotent (no side effect) and flips read_at unconditionally below.
    read_at_claimed = False
    if action != 'mark_done':
        claim_resp = (
            supabase_client.table('chain_events')
            .update({'read_at': ts_iso}, count='exact')
            .eq('event_id', source_event_id)
            .is_('read_at', 'null')
            .execute()
        )
        claimed_rows = getattr(claim_resp, 'data', None) or []
        claimed_count = getattr(claim_resp, 'count', None)
        if not claimed_rows and not (claimed_count and claimed_count > 0):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='source event already acted on',
            )
        read_at_claimed = True

    # Side effects — gated by the claim above. If the write raises, RELEASE the
    # claim (read_at back to NULL) so the half-applied action can be retried
    # instead of being permanently 409'd. (Validation 400s already fired above,
    # before the claim, so they never reach here.)
    try:
        if is_medic_silence:
            # Medic silence decisions reconcile DIRECTLY here — no agent
            # envelope. (Reject lifts the silence; Approve keeps it.) The
            # shared audit block below records the outcome with
            # target_agent/envelope both None. _reconcile_medic_silence never
            # raises (it records unsilence-error in the detail), so the
            # release path below is intentionally not exercised for it.
            reconcile_detail = _reconcile_medic_silence(medic_fp, action)
        elif envelope_candidate is not None:
            _atomic_write_envelope(envelope_candidate, envelope_payload)
            envelope_written = str(envelope_candidate)
    except Exception:
        if read_at_claimed:
            try:
                (
                    supabase_client.table('chain_events')
                    .update({'read_at': None})
                    .eq('event_id', source_event_id)
                    .execute()
                )
            except Exception:  # noqa: BLE001 — best-effort release; never mask
                logger.exception(
                    'failed to release read_at claim after side-effect error '
                    'on event %s', source_event_id,
                )
        raise

    # mark_done flow flips read_at (idempotent — no claim was taken). The
    # non-mark_done flows already flipped it via the atomic claim above.
    if action == 'mark_done':
        supabase_client.table('chain_events').update(
            {'read_at': ts_iso}
        ).eq('event_id', source_event_id).execute()

    # Insert the larry_action audit row. Top-level `actor` column per
    # migration 0006; payload mirrors spec § 5.2 verbatim.
    #
    # Audit #31 (2026-06-05): the side effect above (envelope delivery / medic
    # reconcile) IS the action and is already irreversible by the time we reach
    # here — read_at is flipped NULL→ts and, for non-mark_done, the envelope is
    # in the agent inbox. The audit row is a RECORD of that action, not part of
    # performing it. So we DELIBERATELY do NOT release the read_at claim if the
    # audit write fails: releasing it would let an operator retry re-deliver the
    # already-delivered envelope (double-delivery → the agent runs the approved
    # action twice; cf. the PR-E paid-re-run hazard), which is strictly worse
    # than a logged audit gap. Instead the audit write is best-effort: on
    # failure we log loudly and return success with audit_persisted=False +
    # audit_error so the gap is visible in-band (and the action — which truly
    # happened — is reported as such rather than surfacing an opaque 500 that
    # would 409 on retry with no record of what landed).
    source_task_id = source.get('task_id')
    audit_persisted = True
    audit_error: Optional[str] = None
    action_event_id: Optional[str] = None
    try:
        compute_event_id, sanitize_payload = _import_chain_event_helpers()
        action_payload = {
            'source_event_id': source_event_id,
            'source_event_type': source.get('event_type'),
            'action': action,
            'comment': comment,
            'envelope_written': envelope_written,
            'target_agent': target_agent,
        }
        # Direct-reconcile decisions (Medic silence) record their outcome
        # (unsilenced / kept-silenced / …) + fingerprint in the audit row.
        if reconcile_detail:
            action_payload.update(reconcile_detail)
        # Audit #58: key the audit id on source_event_id too, so two distinct
        # larry_actions on different source events sharing a task_id in the same
        # microsecond produce distinct ids rather than silently dropping one row
        # via ignore_duplicates.
        action_event_id = compute_event_id(
            source_task_id, 'larry_action', ts_iso, extra=source_event_id,
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
    except Exception as exc:  # noqa: BLE001 — best-effort audit; keep the claim
        audit_persisted = False
        audit_error = str(exc)
        action_event_id = None
        logger.exception(
            'larry_action AUDIT GAP: action executed but audit-row write '
            'failed for source event %s (action=%s, actor=%s, '
            'envelope_written=%s, target_agent=%s). read_at claim is KEPT to '
            'prevent envelope double-delivery on retry; this action has no '
            'audit row.',
            source_event_id, action, actor, envelope_written, target_agent,
        )

    result = {
        'action_event_id': action_event_id,
        'envelope_written': envelope_written,
        'target_agent': target_agent,
        'audit_persisted': audit_persisted,
    }
    if audit_error is not None:
        result['audit_error'] = audit_error
    if reconcile_detail:
        result['medic_reconcile'] = reconcile_detail['medic_reconcile']
    return result


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
    # Audit #22: the master kill switch must fail safe and stay consistent with
    # the scheduler (rotate_active_tier._load_rotation_config). bool() of any
    # non-empty string is True, so a quoted-bool typo like {"enabled": "false"}
    # would wrongly read as ENABLED here. Treat rotation as on only when the
    # value is the JSON boolean ``true``; every other type collapses to off.
    return block.get('enabled') is True


def _read_rotation_pinned_tier(agents_root: Path) -> str:
    """Tier pinned by the override file's contents — mirrors
    ``rotate_active_tier._override_pinned_tier`` so the dashboard reflects the
    tier the scheduler will actually pin. Empty/unrecognized/unreadable maps to
    tier1 (the historical Off behavior). Callers check override presence first;
    this is only meaningful when the override file exists."""
    path = agents_root / ROTATION_OVERRIDE_FILE_NAME
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return ROTATION_DEFAULT_PINNED_TIER
    tier = raw.strip().lower()
    return tier if tier in ROTATION_VALID_TIERS else ROTATION_DEFAULT_PINNED_TIER


def _read_rotation_active_tier(agents_root: Path) -> str:
    """The tier the scheduler is CURRENTLY running on, from the live state
    file rotate_active_tier maintains (blackboard/active-tier.json). Mirrors
    ``active_tier.read``'s fallback contract: a missing/unreadable/malformed
    file, a non-dict payload, or an unknown ``tier`` all collapse to tier1, so
    the dashboard never wedges on a parse error."""
    path = agents_root / ROTATION_ACTIVE_TIER_STATE_REL
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ROTATION_DEFAULT_PINNED_TIER
    if not isinstance(data, dict):
        return ROTATION_DEFAULT_PINNED_TIER
    tier = data.get('tier')
    return tier if tier in ROTATION_VALID_TIERS else ROTATION_DEFAULT_PINNED_TIER


def _reader_rotation_mode(
    agents_root: Path, models_path: Path, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Resolve the effective rotation mode from the override file + config.

    ``off`` whenever the override file is present OR the config default is
    disabled; ``auto`` only when neither forces it off. The component
    signals are surfaced so the UI can show *why* it's off. ``pinned_tier`` is
    the tier the scheduler will hold while off (file contents when the override
    is present; tier1 when off purely because config is disabled), and ``None``
    in auto mode where the load gate owns the tier. ``current_tier`` is the tier
    the scheduler is actually running on right now (live state file) — surfaced
    in every mode so the UI can show which tier Auto landed on; in Off it
    converges to ``pinned_tier`` within a tick."""
    override_active = (agents_root / ROTATION_OVERRIDE_FILE_NAME).exists()
    config_enabled = _read_rotation_config_enabled(models_path)
    mode = 'auto' if (config_enabled and not override_active) else 'off'
    if mode == 'auto':
        pinned_tier: Optional[str] = None
    elif override_active:
        pinned_tier = _read_rotation_pinned_tier(agents_root)
    else:
        # Off only because config is disabled (no override file). The
        # scheduler's config-disabled path forces tier1; reflect that.
        pinned_tier = ROTATION_DEFAULT_PINNED_TIER
    return {
        'mode': mode,
        'pinned_tier': pinned_tier,
        'current_tier': _read_rotation_active_tier(agents_root),
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
    pinned_tier: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Toggle the rotation override file + write the larry_action audit row.

    Raises HTTPException for 4xx; returns the resulting mode state on
    success. Idempotent on the filesystem: re-pinning the same tier or
    removing an absent override file is a no-op.

    For ``mode='off'`` the chosen ``pinned_tier`` (tier1|tier2, default tier1)
    is written as the override file's contents; the scheduler honors it every
    tick. ``mode='auto'`` removes the file and ignores ``pinned_tier``.
    """
    if mode not in ROTATION_VALID_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid mode={mode!r}',
        )
    # Resolve + validate the pinned tier up front so an invalid value 400s
    # before we mutate the filesystem. Only meaningful for 'off'.
    resolved_tier = (pinned_tier or ROTATION_DEFAULT_PINNED_TIER).strip().lower()
    if mode == 'off' and resolved_tier not in ROTATION_VALID_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid pinned_tier={pinned_tier!r}',
        )
    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()
    override_path = agents_root / ROTATION_OVERRIDE_FILE_NAME

    # off → write the override file with the pinned tier as its contents;
    # auto → remove it. The scheduler reads contents/presence on its next tick.
    # Atomic write (tmp + fsync + os.replace) so a scheduler tick that reads the
    # file concurrently never observes a truncated/empty body mid-write — a
    # partial read would strip()->'' -> tier1 fallback and force a spurious
    # one-tick tier1 + manual_override event. Matches the repo's atomic-write
    # discipline (atomic_io is the shared helper).
    if mode == 'off':
        override_path.parent.mkdir(parents=True, exist_ok=True)
        _import_atomic_io().atomic_write_text(override_path, resolved_tier)
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
        'pinned_tier': resolved_tier if mode == 'off' else None,
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


def _import_supabase_chunk():
    """Lazy import of the shared chunked-clear helper (stdlib-only, so it
    always imports; kept lazy only to share the scripts_dir sys.path setup)."""
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import supabase_chunk  # noqa: PLC0415
    return supabase_chunk


def _import_atomic_io():
    """Lazy import of the shared atomic-write helper (stdlib-only; lazy only to
    share the scripts_dir sys.path setup)."""
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import atomic_io  # noqa: PLC0415
    return atomic_io


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
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from test_isolation_guard import refuse_under_test  # noqa: PLC0415
    refuse_under_test('claude-spawn')
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

        # Audit #32 (2026-06-05): the clears are batched in chunks of 200
        # because a single .in_(all_ids) would blow the PostgREST URL length,
        # and PostgREST gives us no client-side transaction (there is no
        # cleanup RPC in the schema). So a mid-loop .execute() failure leaves
        # EARLIER chunks already cleared in the DB while the exception aborts
        # the handler — previously surfacing as an opaque HTTP 500 that dropped
        # the success payload, so the operator never learned which rows were
        # cleared or where the (single, written-up-front) backup lives. The
        # shared `chunked_clear` helper enforces this contract: on failure it
        # raises ChunkedClearError carrying the cleared-so-far list, which we
        # turn into an HTTP 500 whose detail carries backup_path + that list so
        # the read_at -> NULL reversal is recoverable. (The backup already
        # contains ALL intended rows; reversing the uncleared ones is a harmless
        # no-op since their read_at is still NULL.)
        sc = _import_supabase_chunk()
        ids = [r['event_id'] for r in rows if r.get('event_id')]
        try:
            cleared_event_ids = sc.chunked_clear(
                supabase_client, 'chain_events', ids, {'read_at': ts_iso},
            )
        except sc.ChunkedClearError as exc:
            logger.exception(
                'cleanup-review PARTIAL CLEAR: chunk %d failed after %d/%d '
                'rows cleared; backup at %s',
                exc.chunk_index, exc.cleared_count, exc.total, backup_path,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    'error': 'cleanup-review partial clear',
                    'message': str(exc.cause),
                    'backup_path': backup_path,
                    'cleared_event_ids': exc.cleared_ids,
                    'cleared_count': exc.cleared_count,
                    'total_to_clear': exc.total,
                },
            ) from exc

        # task_ids for the success payload, derived only from rows we actually
        # cleared (every chunk succeeded if we reach here).
        cleared_set = set(cleared_event_ids)
        seen: set[str] = set()
        for r, _why in to_clear:
            if r.get('event_id') not in cleared_set:
                continue
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


# GET /api/missions/captures — the Parked-lane data source (Missions v2
# Phase 1 § 5). Serves captures.json verbatim + an mtime-derived
# last_synced_at; the dashboard filters to state=='parked'.
@app.get(
    '/api/missions/captures',
    response_model=CapturesResponse,
    dependencies=[Depends(_require_token)],
)
def get_missions_captures() -> dict[str, Any]:
    return _reader_captures(_captures_json_path())


# GET /api/missions/derived — the relocated mission-phase derive (Missions v2
# Phase 2 § 3). Source-of-truth phase/aggregate/orphan derivation, ported
# byte-faithfully from the dashboard's lib/mission-queries.ts (parity-gated,
# § 4), plus the additive parked[] array + orphan-readability fields. Optional
# ?repo= / ?task_id= filters (§ 3.2). Read-only; same X-Dashboard-Token gate as
# /api/system/missions. Orphan terminal detection does a bounded, fail-safe
# GitHub PR-state read (_resolve_orphan_pr_states, § 3.4) — reusing the existing
# GITHUB_TOKEN, degrading to the event-only derive on any error.
@app.get(
    '/api/missions/derived',
    response_model=MissionsDerivedResponse,
    dependencies=[Depends(_require_token)],
)
def get_missions_derived(
    repo: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
) -> dict[str, Any]:
    return _handle_missions_derived(
        missions_path=_missions_json_path(),
        captures_path=_captures_json_path(),
        supabase_client=_get_larry_action_supabase_client(),
        repo=repo,
        task_id=task_id,
        pr_state_resolver=_resolve_orphan_pr_states,
    )


@app.post(
    '/api/system/missions/new',
    response_model=NewMissionResponse,
    dependencies=[Depends(_require_token)],
)
def post_system_missions_new(body: NewMissionRequest) -> dict[str, Any]:
    return _handle_new_mission(body=body, missions_path=_missions_json_path())


# POST /api/missions/captures/{capture_id}/action — capture write-back (Missions
# v2 Phase 3 § 4). promote/drop are PR-backed (reuse the new-mission GitHub-REST
# mechanism; promote edits missions.json + captures.json in ONE PR); snooze
# routes direct through the single captures.json committer. Same auth as
# /api/larry/action: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/missions/captures/{capture_id}/action',
    response_model=CaptureActionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_capture_action(
    capture_id: str,
    body: CaptureActionRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_capture_action(
        capture_id=capture_id,
        action=body.action,
        args=body.model_dump(exclude={'action'}, exclude_none=True),
        captures_path=_captures_json_path(),
        missions_path=_missions_json_path(),
    )


# POST /api/system/missions/{mission_id}/action — mission write-back (Missions
# v2 Phase 3 § 5 + § 6). defer/resume/reprioritize/accept/dismiss are ALL
# PR-backed (missions.json is the curated registry — every change auditable) and
# reuse the new-mission GitHub-REST mechanism via _open_registry_pr. accept/dismiss
# act on auto-proposed orphan threads (§ 6): accept flips phase proposed->drafting,
# dismiss sets the additive acknowledged flag (phase stays proposed). Same auth as
# /api/larry/action: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/system/missions/{mission_id}/action',
    response_model=MissionActionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_mission_action(
    mission_id: str,
    body: MissionActionRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_mission_action(
        mission_id=mission_id,
        action=body.action,
        args=body.model_dump(exclude={'action'}, exclude_none=True),
        missions_path=_missions_json_path(),
    )


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


# POST /api/ingest/desktop-session — the desktop's only write path. Token-gated
# by the dedicated X-Ingest-Token (NOT the dashboard read token). See the
# Missions v2 Phase 0 block above for why agent is pinned + event_type is
# constrained.
@app.post(
    '/api/ingest/desktop-session',
    response_model=DesktopSessionIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_require_ingest_token)],
)
def post_desktop_session_ingest(
    body: DesktopSessionIngestRequest,
    response: Response,
) -> dict[str, Any]:
    result = _handle_desktop_session_ingest(
        event_type=body.event_type,
        task_id=body.task_id,
        payload=body.payload,
    )
    if not result['ok']:
        # emit_event returned False — Supabase unreachable / supabase-py
        # missing. Best-effort contract: the hook ignores the body; the 502
        # just signals "not persisted" to anyone watching.
        response.status_code = status.HTTP_502_BAD_GATEWAY
    return result


# POST /api/ingest/capture — durable one-gesture capture. Same X-Ingest-Token
# as desktop-session ingest; appends atomically to captures.json (no PR — the
# GC healer batch-commits). See the Missions v2 Phase 1 block above.
@app.post(
    '/api/ingest/capture',
    response_model=CaptureIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_require_ingest_token)],
)
def post_capture_ingest(body: CaptureIngestRequest) -> dict[str, Any]:
    return _handle_capture_ingest(
        title=body.title,
        note=body.note,
        origin=body.origin,
        label=body.label,
        captures_path=_captures_json_path(),
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
        pinned_tier=body.pinned_tier,
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
