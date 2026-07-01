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

# projects_store is the single source of the Project+Phase schema + the
# "Actively working" pipeline derive (projects-v3 P3). stdlib-only, so a
# top-level import is safe (no supabase/heavy deps that force lazy import).
import projects_store  # noqa: E402

# inbox_dispatch_order is the single source of the queued-lane ordering rule,
# shared with inbox_watcher.scan_inbox so the panel matches what builds next
# (forge-queue-fast-track). stdlib-only.
from inbox_dispatch_order import order_pending, read_dispatch_meta  # noqa: E402


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


def _valid_repo_names(models_path: Path) -> frozenset[str]:
    """Buildable repo NAMES from ``config/agent-models.json`` ``repo_paths`` keys
    — the canonical block that gates which repos Forge can build.

    Best-effort: any read/parse error returns an EMPTY set, which every caller
    treats as "can't validate" and FAILS OPEN (never block a launch / drop a
    repo over a transient config read miss). A populated set enables the check.

    Note we validate the build repo by NAME only and never try to derive it from
    the spec's location: every spec lives in agent-core's ``agents/beacon/specs``
    regardless of which repo the build targets (e.g. a ``ourliberty-dashboard``
    build's spec_doc is an agent-core path), so spec location carries no signal
    about the target repo. The phase/project ``repo`` is the only reliable
    source; when it's missing or bogus the launch must reject loudly, not guess."""
    try:
        data = json.loads(models_path.read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    block = data.get('repo_paths') if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return frozenset()
    return frozenset(k for k in block if isinstance(k, str) and k)


def _captures_json_path() -> Path:
    """Path to the durable-capture sibling registry (Missions v2 Phase 1).
    Env-overridable so tests redirect reads/writes to a tmpdir without
    touching the deployed checkout's `agents/beacon/captures.json`."""
    override = os.environ.get('OURLIBERTY_CAPTURES_JSON')
    if override:
        return Path(override)
    return _repo_root() / 'agents' / 'beacon' / 'captures.json'


def _projects_json_path() -> Path:
    """Path to the Projects-tab-v3 pipeline store (projects-v3 P3). The SOLE
    committer is `heal_projects_store.py`; this read surface only reads it.
    Env-overridable so tests redirect reads to a tmpdir without touching the
    deployed checkout's `agents/beacon/projects.json`."""
    override = os.environ.get('OURLIBERTY_PROJECTS_JSON')
    if override:
        return Path(override)
    return _repo_root() / 'agents' / 'beacon' / 'projects.json'


def _state_log_json_path() -> Path:
    """Path to the work-in-flight State Log (system self-awareness Slice 1).
    The SOLE writer is `system_state_log.py` (riding the GC tick); this read
    surface only reads it. It is UNCOMMITTED droplet runtime state, so it lives
    under the agents blackboard — NOT `_repo_root()`. Env-overridable
    (`OURLIBERTY_SYSTEM_STATE_LOG`) so tests redirect reads to a tmpdir, and so
    the path stays in lockstep with the narrator's own resolver of the same
    name."""
    override = os.environ.get('OURLIBERTY_SYSTEM_STATE_LOG')
    if override:
        return Path(override)
    return _agents_root() / 'blackboard' / 'system-state-log.json'


def _new_mission_queue_dir() -> Path:
    """Directory the +New mission flow drops queued mission entries into for
    the missions writer (heal_orphan_autoregister) to drain into missions.json
    on its commit cycle.

    Lives under the agents blackboard — NOT inside the git checkout — so a
    pending file is never untracked-file drift, and keyed off
    `_agents_root()` (OURLIBERTY_AGENTS_ROOT) so tests redirect it to a tmpdir.
    The dashboard is deliberately NOT a git committer: it only produces queue
    files here; the owning healer (the missions single-committer) appends them
    to missions.json on its own commit and removes them once the mission is on
    origin/main. See `heal_orphan_autoregister.drain_new_mission_queue`."""
    return _agents_root() / 'blackboard' / 'new-mission-queue'


def _build_launch_queue_dir() -> Path:
    """Directory the Launch-build flow drops queued launch requests into for
    the Beacon-side drainer (`launch_queue_drain.py`) to author the build
    sequence from.

    Lives under the agents blackboard — NOT inside the git checkout — so a
    pending request is never untracked-file drift, and keyed off
    `_agents_root()` (OURLIBERTY_AGENTS_ROOT) so tests redirect it to a tmpdir.
    The dashboard is deliberately NOT a committer (to the repo OR to the
    projects store): it only produces a queue file here; the drainer authors
    the sequence, runs Mirror DAG preflight, and kicks the build. This mirrors
    the `+New mission` non-committer precedent (`_new_mission_queue_dir`) so the
    single-committer invariant holds (projects.json's sole committer is
    `heal_projects_store.py`; the dashboard and the drainer are non-committers
    to it). See `launch_queue_drain.drain_once`."""
    return _agents_root() / 'blackboard' / 'build-launch-queue'


# Serializes concurrent Launch POSTs in-process (a rapid double-click), mirroring
# `_NEW_MISSION_LOCK`. The drain's deterministic-seq-id existence check is the
# durable idempotency backstop across the post-drain double-click (the queue file
# is gone by then); this lock only collapses simultaneous in-flight POSTs.
_LAUNCH_QUEUE_LOCK = __import__('threading').Lock()


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write `obj` as pretty JSON atomically (unique tmp in the same dir +
    os.replace) so a concurrent reader — the draining healer — never sees a
    partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(obj, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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

# Steering verbs for POST /api/system/build-sequences/{seq_id}/action
# (operator-needs-you-feed spec §5.5). Each delegates to the matching
# sequence_shortcut_helpers.apply_* helper. `skip` and `retry` operate on a
# step (step_id required); `resume` and `cancel` operate on the whole sequence.
BUILD_SEQUENCE_ACTION_VALID_ACTIONS: frozenset[str] = frozenset({
    'resume', 'skip', 'cancel', 'retry',
})
BUILD_SEQUENCE_STEP_ACTIONS: frozenset[str] = frozenset({'skip', 'retry'})

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
ROTATION_VALID_TIERS: frozenset[str] = frozenset({'tier1', 'tier2', 'tier3'})
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
    # Set when the operator has fast-tracked this task via "Build next"
    # (forge-queue-fast-track): the ISO-8601 timestamp of the most recent
    # click. None for a normal FIFO task. Drives the UI's "next" badge; the
    # ordering itself is already applied (fast-tracked first) in the reader.
    fast_tracked_at: Optional[str] = None


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


# ---- POST /api/system/agent-queue/{agent}/fast-track (forge-queue-fast-track) ----
#
# The "Build next" gesture: stamp a queued inbox task with `fast_tracked_at`
# so both the dispatcher and the queued-lane reader float it to the head of
# the queue (newest stamp first, LIFO). Writes ONLY the target task's JSON
# (mtime preserved); never touches what's already building.

class FastTrackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: str


class FastTrackResponse(BaseModel):
    task_id: str
    fast_tracked_at: str


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


class SystemStateLogResponse(BaseModel):
    # The work-in-flight State Log read surface (system self-awareness Slice 1
    # § D3). Serves the narrator's doc verbatim plus two derived freshness
    # signals the consumer needs to degrade honestly:
    #   * `present`  — False when the log has never been written (first tick
    #                  hasn't run, or the file was removed); all doc fields null.
    #   * `stale`    — True when the file is older than STATE_LOG_STALE_AFTER_SEC,
    #                  so Beacon can caveat "this picture is N minutes old".
    # Additive read surface: it NEVER raises on a missing/malformed file — a
    # broken log degrades to present=False rather than 500ing the dashboard.
    present: bool = True
    stale: bool = False
    last_synced_at: Optional[str] = None
    schema_version: Optional[int] = None
    as_of: Optional[str] = None
    narrative_prose: Optional[str] = None
    structured_snapshot: Optional[dict[str, Any]] = None
    provenance: Optional[dict[str, Any]] = None


# ---- /api/system/automated-work response models (system self-awareness:
# the "Automated Work" feed — north star §6 item 2, the autonomy dial-in
# surface). A read of chain_events `autonomy_decision` rows: what the trust
# policy auto-fired without Larry's click.

class AutomatedWorkItem(BaseModel):
    # One auto-fired trust-policy decision ("what the team did on its own").
    # All fields optional so a sparse/older autonomy_decision payload still
    # serializes (the panel degrades per-field rather than dropping the row).
    task_id: Optional[str] = None
    ts: Optional[str] = None
    age_seconds: Optional[int] = None
    pr_url: Optional[str] = None
    decision: Optional[str] = None
    dispatched: Optional[bool] = None
    source: Optional[str] = None
    target_agent: Optional[str] = None
    target_repo: Optional[str] = None
    task_type: Optional[str] = None
    summary: Optional[str] = None
    rule_label: Optional[str] = None
    rule_action: Optional[str] = None
    # Plain-language meaning layer (#5), authored deterministically at read by
    # event_briefing.decision_briefing — the same {what,why,suggest} + risk
    # contract the narrator writes onto captures, rendered by the shared
    # BriefingBlock/RiskBadge. Optional so a sparse/older row still serializes.
    briefing: Optional[dict[str, Any]] = None
    risk: Optional[str] = None
    risk_note: Optional[str] = None


class AutomatedWorkCounts(BaseModel):
    auto_approved: int = 0
    asked: int = 0       # decision=force_ask
    rejected: int = 0    # decision=reject


class AutomatedWorkResponse(BaseModel):
    # `items` = the auto_approve decisions (most-recent first, bounded by
    # `limit`); `counts` cover auto_approved/asked/rejected over the window.
    # Fail-safe: present=False (Supabase unavailable) degrades the panel — the
    # reader NEVER raises, so the dashboard lane never 500s.
    present: bool = True
    window_days: int
    counts: AutomatedWorkCounts
    items: list[AutomatedWorkItem]
    truncated: bool = False


# ---- /api/system/autonomy-posture response model (system self-awareness:
# the *prospective* autonomy surface — the companion to the Automated Work
# feed above). A plain-language read of the LIVE trust policy's current STANCE
# (trust_policy.summarize_policy): what auto-starts, what still asks Larry, and
# the always-on gates. Pure file read; `degraded`=True means the policy file
# couldn't be read and we fail-closed to "everything asks you" (the safe
# default), NEVER a 500.

class AutonomyPostureResponse(BaseModel):
    level: str                       # 'conservative' | 'balanced'
    headline: str
    auto_starts: list[str] = []      # work that starts without Larry's click
    still_asks: list[str] = []       # work that still comes to him first
    gates: list[str] = []            # the always-on backstops
    degraded: bool = False           # policy unreadable → fail-closed to "asks"


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
    # C4 (projects-v3 P1): additive funnel grouping — {primary, secondary} lanes
    # of intake (parked + team-suggested vs orphaned, auto-filtered). Additive:
    # the live board reads missions/orphans/parked; P2 consumes funnel.
    funnel: dict[str, list[dict[str, Any]]]
    # projects-v3 P3 (p3-project-store): additive "Actively working" pipeline —
    # the list of active Projects, each with ordered phase cards (lifecycle
    # state + Desired End State + optional spec/sequence refs) + coarse status +
    # one-off collapse. Additive: the live board reads missions/orphans/parked/
    # funnel; the P3 pipeline UI consumes `pipeline`. Empty list when the store
    # is absent/empty/malformed (the derive never breaks the board).
    pipeline: list[dict[str, Any]]
    last_synced_at: Optional[str]
    as_of: str


class NewMissionRequest(BaseModel):
    name: str = Field(..., min_length=1)
    brief: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    spec_docs: list[str] = Field(default_factory=list)
    # alert-pipeline-rework P3b: the retrospective Stage B author posts cards as
    # `phase: 'proposed'` (a suggestion awaiting accept/dismiss) rather than the
    # default `'drafting'`. Both fields are optional + backward-compatible: an
    # unset phase keeps the legacy drafting behavior, so existing +New callers
    # are unchanged. `proposed_by` records the author (e.g. the check id) and
    # `predraft` carries the structured pre-draft (template/file-key/value/diff
    # sketch + root_signature) the author attaches for a one-click accept.
    phase: Optional[str] = None
    proposed_by: Optional[str] = None
    predraft: Optional[dict[str, Any]] = None


class NewMissionResponse(BaseModel):
    # The +New mission flow no longer opens a PR — it queues the mission for
    # the missions writer (heal_orphan_autoregister) to register on its commit
    # cycle. `status` is 'queued'. `pr_url`/`branch` are retained as optional
    # (always absent now) only so a transitional client reading them degrades
    # to undefined rather than breaking; they can be dropped once no client
    # references them.
    mission_id: str
    status: str = 'queued'
    pr_url: Optional[str] = None
    branch: Optional[str] = None


class CaptureActionRequest(BaseModel):
    # Missions v2 Phase 3 § 4 — POST /api/missions/captures/{id}/action body.
    action: str = Field(..., min_length=1)  # promote | drop | snooze
    # promote overrides (all optional — defaults inferred from the capture).
    # projects-v3 P3: promote MOVES into a project; `brief` seeds the phase's
    # plain-language Desired End State, `north_star_ref` the project's North Star
    # link. `spec_docs` is retained (ignored by project-promote) for a
    # transitional client that still sends it.
    name: Optional[str] = None
    brief: Optional[str] = None
    repo: Optional[str] = None
    north_star_ref: Optional[str] = None
    spec_docs: Optional[list[str]] = None
    # drop:
    reason: Optional[str] = None
    # snooze (ISO-8601 datetime; null clears the snooze):
    snoozed_until: Optional[str] = None


class CaptureActionResponse(BaseModel):
    # All three actions are now one-click (no PR): `drop` and `snooze` are direct
    # captures.json committer writes → {applied, state|snoozed_until}; `promote`
    # queues the mission for the missions writer and flips the capture →
    # {mission_id, status: 'queued', applied}. Every field is optional so one
    # model covers all shapes (§ 4 contract). `pr_url`/`branch` are retained as
    # optional (always absent now) only so a transitional client reading them
    # degrades to undefined rather than breaking; drop once no client references
    # them.
    pr_url: Optional[str] = None
    branch: Optional[str] = None
    mission_id: Optional[str] = None
    # projects-v3 P3 (p3-promote-endpoint): `promote` now MOVES the capture into
    # a new single-phase project at Brainstorm instead of minting a mission —
    # {project_id, phase_id, status: 'promoted', applied}. The capture flips to
    # state:'promoted' (its own committer); the project lands on projects.json
    # (heal_projects_store commits). mission_id stays optional (absent now) for a
    # transitional client.
    project_id: Optional[str] = None
    phase_id: Optional[str] = None
    status: Optional[str] = None
    state: Optional[str] = None
    applied: Optional[bool] = None
    snoozed_until: Optional[str] = None
    # Phase S (S7): a pause(=snooze)/drop on a card whose linked work is in-flight
    # is recorded (not applied) so it never interrupts the run — applied={false},
    # deferred={true}, and pending_action carries the action the healer replays
    # after a safe stop.
    deferred: Optional[bool] = None
    pending_action: Optional[dict[str, Any]] = None


class CaptureThreadMessage(BaseModel):
    # Missions v2 Phase 4 § 8 — one card_message turn. Every field is optional so
    # a malformed/legacy row degrades per-field rather than failing the read.
    # Phase 4b Contract C (§ 7): `id` projects the existing chain_events.event_id
    # so the live-thread poll can dedupe / mark-as-seen by a stable per-message id.
    id: Optional[str] = None
    ts: Optional[str] = None
    direction: Optional[str] = None  # larry_to_team | team_to_larry
    text: Optional[str] = None
    actor: Optional[str] = None
    needs_reply: Optional[bool] = None


class CaptureThreadResponse(BaseModel):
    # GET /api/missions/captures/{id}/thread — oldest-first conversation (§ 8).
    capture_id: str
    messages: list[CaptureThreadMessage] = Field(default_factory=list)
    last_synced_at: str


class CaptureMessageRequest(BaseModel):
    # POST /api/missions/captures/{id}/message body (§ 8).
    text: str = Field(..., min_length=1)


class CaptureMessageResponse(BaseModel):
    # The card_message was emitted + a resume envelope dropped in Beacon's inbox.
    posted: bool
    event_id: str
    direction: str
    envelope_written: Optional[str] = None
    doorbell_resolved: bool = False


class CaptureDelegateRequest(BaseModel):
    # POST /api/missions/captures/{id}/delegate body (delegate-fix spec § 2).
    # Optional — defaults to the capture's recommended_action, else "delegate".
    action: Optional[str] = None  # delegate | promote | drop | snooze


class CaptureDelegateResponse(BaseModel):
    # The delegate proposal (a human-approval-gate APPROVAL_REQUEST) was dropped
    # in Beacon's inbox. `dispatched` is True on both a fresh proposal and a
    # dedup-collapse onto an already-open one; `deduped` distinguishes the two.
    # `pr_url` is reserved for a future action that routes PR-backed (§ 2).
    dispatched: bool
    deduped: Optional[bool] = None
    pr_url: Optional[str] = None


class MissionActionRequest(BaseModel):
    # Missions v2 Phase 3 § 5 + Projects v3 P2 Contract B — POST
    # /api/system/missions/{id}/action body.
    action: str = Field(..., min_length=1)
    # defer: optional human-readable reason recorded in deferred_reason.
    reason: Optional[str] = None
    # reprioritize: new optional priority int (additive schema; null clears it).
    priority: Optional[int] = None
    # snooze (Contract B): ISO-8601 datetime; null clears the snooze.
    snoozed_until: Optional[str] = None


class MissionActionResponse(BaseModel):
    # `defer`/`reprioritize`/`snooze` write-backs are PR-backed → {pr_url, branch}
    # (§ 5 + Contract B). projects-v3 P3 (p3-promote-endpoint): `accept` is now
    # unified onto Promote — it MOVES the proposed mission into a new single-phase
    # project at Brainstorm (no missions.json PR; the mission is suppressed from
    # the funnel by the project's `promoted_from` cross-ref) → {project_id,
    # phase_id, status: 'promoted', applied}.
    pr_url: Optional[str] = None
    branch: Optional[str] = None
    project_id: Optional[str] = None
    phase_id: Optional[str] = None
    status: Optional[str] = None
    applied: Optional[bool] = None


class FunnelPromoteRequest(BaseModel):
    # projects-v3 P3 (p3-promote-endpoint) — POST /api/funnel/promote body. The
    # ONE unified Promote gesture for any funnel item, regardless of lane: `ref`
    # is the item id (a capture_id or a proposed-mission id). `kind` is an
    # optional disambiguator ('capture' | 'mission'); when absent the handler
    # auto-resolves (captures first, then missions).
    ref: str = Field(..., min_length=1)
    kind: Optional[str] = None
    # Optional project overrides (defaults inferred from the source item):
    name: Optional[str] = None
    brief: Optional[str] = None  # seeds the phase's Desired End State
    repo: Optional[str] = None
    north_star_ref: Optional[str] = None


class FunnelPromoteResponse(BaseModel):
    # The funnel item was MOVED into a new single-phase project at Brainstorm.
    # `applied` is False on an idempotent re-promote (the project already
    # existed). `source_kind` records which lane the item came from.
    project_id: str
    phase_id: Optional[str] = None
    status: str
    applied: bool
    source_kind: str


class LaunchBuildRequest(BaseModel):
    # projects-v3 P3 (p3-launch-queue-drain) — POST /api/projects/launch body.
    # The dashboard Launch-build click on a spec-ready phase. `project_id` +
    # `phase_id` locate the phase in projects.json; the dashboard reads the
    # store read-only and queues a launch request for the Beacon-side drainer
    # (it never commits the repo or the projects store — non-committer, the
    # `+New mission` precedent).
    project_id: str = Field(..., min_length=1)
    phase_id: str = Field(..., min_length=1)


class LaunchBuildResponse(BaseModel):
    # The launch request was queued for the drainer. `status` is 'queued'.
    # Idempotency on phase id ultimately rides on the drain's deterministic
    # `launch-<phase_id>` sequence-file existence check (a re-launch after the
    # queue drained never double-dispatches a build); this endpoint additionally
    # 409s a rapid double-click whose first request is still queued.
    phase_id: str
    project_id: str
    status: str = 'queued'
    seq_id: str


class PhaseAdvanceRequest(BaseModel):
    # p3f-phase-transitions — POST /api/projects/advance body. The checkpoint
    # "Ready to spec / go" gesture: advance a phase one forward step in the
    # lifecycle. This endpoint owns ONLY the Brainstorm→Spec checkpoint (the
    # human "refine → go" gate); Spec→Building is owned by Launch + status
    # writeback, Building→Done by status writeback. `project_id` + `phase_id`
    # locate the phase in projects.json; the dashboard writes the lifecycle bump
    # to disk and the projects-store healer commits (non-committer discipline).
    project_id: str = Field(..., min_length=1)
    phase_id: str = Field(..., min_length=1)


class PhaseAdvanceResponse(BaseModel):
    # The phase moved forward one lifecycle step. `from_state`/`to_state` record
    # the transition (brainstorm → spec for the checkpoint this endpoint owns).
    project_id: str
    phase_id: str
    from_state: str
    to_state: str
    status: str = 'advanced'


class EditBrainstormRequest(BaseModel):
    # projects-v3 P6.1 — POST /api/projects/brainstorm body. Larry edits the
    # pre-filled Brainstorm card: `draft` (the AI draft, now his prose) and/or
    # `decisions` (the "Your decisions" fork list). At least one must be present.
    # `project_id` + `phase_id` locate the phase; the dashboard writes the edit to
    # projects.json on disk and the projects-store healer commits (non-committer).
    project_id: str = Field(..., min_length=1)
    phase_id: str = Field(..., min_length=1)
    draft: Optional[str] = None
    decisions: Optional[list[str]] = None


class BrainstormDecisionCard(BaseModel):
    # One "Your decisions" item as the card renders it ({id,title,decision}).
    id: str
    title: str
    decision: str = ''


class EditBrainstormResponse(BaseModel):
    # The edit landed (or was a no-op). `applied` is False on an idempotent
    # re-save. The flat fields mirror the card projection so the dashboard can
    # re-render from the response without a refetch.
    project_id: str
    phase_id: str
    applied: bool
    status: str = 'edited'
    draft: Optional[str] = None
    decisions: Optional[list[BrainstormDecisionCard]] = None
    spec_target_path: Optional[str] = None


class PhaseThreadResponse(BaseModel):
    # GET /api/projects/phases/{ref}/thread — the Brainstorm phase card's
    # conversation, oldest-first (mirrors CaptureThreadResponse). `phase_ref` is
    # the composite project_id::phase_id the conversation keys on.
    phase_ref: str
    messages: list[CaptureThreadMessage] = Field(default_factory=list)
    last_synced_at: str


class SpecAttachRequest(BaseModel):
    # p3f-phase-transitions — POST /api/projects/attach-spec body. Points a Spec-
    # stage phase at its (already-authored) spec doc, making it spec-ready so the
    # Launch button appears. `spec_ref` is a repo-relative path to an EXISTING
    # spec doc — a non-existent path is rejected loudly (spec § 4 guardrail), never
    # written. Non-committer: the dashboard writes the `spec_ref` to disk and the
    # projects-store healer commits.
    project_id: str = Field(..., min_length=1)
    phase_id: str = Field(..., min_length=1)
    spec_ref: str = Field(..., min_length=1)


class SpecAttachResponse(BaseModel):
    # The spec doc was attached to the phase (`spec_ref` set; phase now spec-ready).
    project_id: str
    phase_id: str
    spec_ref: str
    lifecycle_state: str
    status: str = 'spec-attached'


class ProjectArchiveRequest(BaseModel):
    # p3f-reversibility-and-orphan — POST /api/projects/archive body. The
    # Drop/Archive gesture: flip a project's state to `archived` so it leaves
    # "Actively working" and its original funnel source item (capture / mission /
    # orphan) returns to the funnel — a mis-promote is reversible, not a dead end.
    # `project_id` locates the project; the dashboard writes the state flip to
    # disk and the projects-store healer commits (non-committer discipline).
    project_id: str = Field(..., min_length=1)


class ProjectArchiveResponse(BaseModel):
    # The project left the board (or already had — `applied` is False on an
    # idempotent repeat). `state` is the terminal state: 'retired' for a Done
    # project (p3f3 Complete & retire), else 'archived'. p3f2/p3f3: `status` /
    # `message` are HONEST and phase-aware — 'retired'/"Completed — cleared from
    # the board." for a Done project; 'returned-to-funnel'/"Dropped — returned to
    # the funnel." when a not-done funnel-sourced project drops back; else
    # 'archived'/"Archived." The UI toast reads `message` verbatim.
    project_id: str
    state: str = 'archived'
    status: str = 'archived'
    message: str = 'Archived.'
    applied: bool


class MissionThreadResponse(BaseModel):
    # GET /api/system/missions/{id}/thread — oldest-first conversation, mirroring
    # the capture thread (Projects v3 P2 Contract B).
    mission_id: str
    messages: list[CaptureThreadMessage] = Field(default_factory=list)
    last_synced_at: str


class MissionMessageRequest(BaseModel):
    # POST /api/system/missions/{id}/message body (Contract B).
    text: str = Field(..., min_length=1)


class MissionMessageResponse(BaseModel):
    # The card_message was emitted + a resume envelope dropped in Beacon's inbox.
    posted: bool
    event_id: str
    direction: str
    envelope_written: Optional[str] = None
    doorbell_resolved: bool = False


class MissionDelegateRequest(BaseModel):
    # POST /api/system/missions/{id}/delegate body (Contract B). Optional —
    # defaults to "delegate".
    action: Optional[str] = None  # delegate | promote | drop | snooze


class MissionDelegateResponse(BaseModel):
    # The delegate proposal (a human-approval-gate APPROVAL_REQUEST) was dropped
    # in Beacon's inbox. `dispatched` is True on both a fresh proposal and a
    # dedup-collapse; `deduped` distinguishes the two.
    dispatched: bool
    deduped: Optional[bool] = None
    pr_url: Optional[str] = None


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


class AutonomyPostureRequest(BaseModel):
    # The autonomy dial position to apply: conservative | balanced | loose.
    # Validated against trust_policy.AUTONOMY_LEVELS in the handler (400 on an
    # unknown value) before any policy write.
    level: str


class RotationModeUpdateResponse(BaseModel):
    mode: str
    pinned_tier: Optional[str]
    current_tier: Optional[str]
    override_active: bool
    config_enabled: bool
    action_event_id: str
    as_of: str


class BuildSequenceActionRequest(BaseModel):
    # Steering verb: resume | skip | cancel | retry. Validated against
    # BUILD_SEQUENCE_ACTION_VALID_ACTIONS in the handler (400 on unknown).
    action: str
    # Required for step-scoped verbs (skip / retry); ignored for
    # resume / cancel which operate on the whole sequence.
    step_id: Optional[str] = None
    # Optional operator note, forwarded to the helper (cancel / skip) and
    # recorded in the audit payload.
    reason: Optional[str] = None


class BuildSequenceActionResponse(BaseModel):
    applied: bool
    action: str
    seq_id: str
    step_id: Optional[str]
    detail: str
    action_event_id: Optional[str]
    audit_persisted: bool
    audit_error: Optional[str] = None


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


# Worktree dir names follow the pattern `wt-<agent>-<task_id>`, where the
# task_id segment is the SANITIZED stem produced by `worktree_manager.
# _sanitize_task_id` (and its two locked-consistent siblings): every char
# outside `[A-Za-z0-9_-]` is mapped to `-` and the result is capped at 50.
# The task_id class here must therefore admit that full charset — uppercase,
# `_`, and a leading `-` (e.g. raw `:foo` sanitizes to `-foo`) are all real
# dir names. An earlier `[a-z0-9][a-z0-9-]*` class silently failed to parse
# those, so a genuinely-building task whose id wasn't a clean lowercase slug
# never reached the building lane. Agent stays `[a-z]+` (agent ids are
# lowercase) so the split between agent and task_id remains the first `-`.
# ASCII-only by design: forge task_ids are ASCII slugs, and `_sanitize_task_id`
# would only emit a non-ASCII char from a non-ASCII `isalnum()` input (none in
# practice) — not worth mirroring `str.isalnum()`'s Unicode reach in a regex.
_WORKTREE_RE = re.compile(r'^wt-(?P<agent>[a-z]+)-(?P<task_id>[A-Za-z0-9_-]+)$')


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
    # The worktree dir bakes in the SANITIZED task stem (`worktree_manager.
    # _sanitize_task_id`: non-`[A-Za-z0-9_-]` -> `-`, capped at 50), but the
    # in-flight sentinel is keyed by the RAW `task_stem`. Comparing the parsed
    # dir stem against the raw stems misses any task whose id isn't already a
    # clean slug (`foo:bar` -> dir `foo-bar` != stem `foo:bar`; an id > 50
    # chars truncates) — it would read as not-in-flight and silently vanish
    # from the building lane while genuinely building. Match instead on the
    # stem the dir actually carries: the sentinel records it as `worktree_stem`
    # (`agent_runner._register_in_flight`), and falling back to `task_stem`
    # covers both pre-field sentinels and the common slug-clean case where
    # raw == sanitized. Keyed by (agent_id, worktree_stem) so a dir matches
    # only its own agent's sentinel.
    in_flight_by_wt: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in in_flight.values():
        agent_id = entry.get('agent_id')
        wt_stem = entry.get('worktree_stem') or entry.get('task_stem')
        if isinstance(agent_id, str) and isinstance(wt_stem, str) and wt_stem:
            in_flight_by_wt.setdefault((agent_id, wt_stem), entry)
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
        agent, parsed_stem, branch = _parse_worktree_name(name)
        mt = _safe_mtime(entry)
        age_seconds: Optional[float] = None
        if mt is not None:
            age_seconds = (captured_at - datetime.fromtimestamp(mt, tz=timezone.utc)).total_seconds()
        matched = (
            in_flight_by_wt.get((agent, parsed_stem))
            if agent and parsed_stem else None
        )
        is_in_flight = matched is not None
        # Surface the sentinel's canonical (unsanitized) task_stem so the
        # building lane shows the real id and matches the unsanitized id the
        # review_request carries (the building<->in_review dedup keys on it).
        # `branch` keeps the parsed sanitized stem — that IS the on-disk git
        # branch (`derive_branch_name` uses the same sanitizer).
        canonical = matched.get('task_stem') if matched else None
        task_id = canonical if isinstance(canonical, str) and canonical else parsed_stem
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
    # review_obsolete closes a card whose PR merged/closed out of band with
    # no verdict (heal_stale_in_review_reconcile). Not a done_today input, so
    # the phantom silently leaves In-Review without a false 'merged today'.
    'review_obsolete',
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
    `*.json` — AND its dispatch order, via the shared `inbox_dispatch_order`
    helper: fast-tracked tasks first (newest "Build next" click wins, LIFO),
    then oldest-mtime-first FIFO. Sharing the helper is what guarantees the
    panel shows exactly what the forge will build next. `waited_seconds` is
    still `now(UTC) - file mtime` (fast-track preserves mtime, so the wait
    stays honest); `fast_tracked_at` echoes the per-item flag for the UI.
    Parameterized on `agents_root` so it stays tmpdir-testable.

    Tasks already building are excluded: the dispatcher leaves a claimed
    task's inbox file in place for the whole build (it only archives on
    completion — see `inbox_watcher.process_task`), so without this filter
    an in-flight build would double-list in BOTH the queued and building
    lanes. We drop any task whose in-flight key (`task.get('task_id')` or
    the filename stem — the dispatcher's own key) is in this agent's
    in-flight registry. Caveat: a task in the brief pre-`run_claude` window
    (claimed but before its in-flight sentinel exists) can still appear here
    momentarily.
    """
    now = now or datetime.now(timezone.utc)
    inbox = agents_root / 'inboxes' / agent
    items: list[dict[str, Any]] = []
    if not inbox.is_dir():
        return items
    building = {
        stem for stem, entry in _load_in_flight_index(agents_root).items()
        if entry.get('agent_id') == agent
    }
    entries: list[tuple[float, Optional[str], str]] = []
    try:
        for e in os.scandir(inbox):
            if not e.is_file() or e.name.startswith('.') or not e.name.endswith('.json'):
                continue
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            fast_tracked_at, task_id = read_dispatch_meta(Path(e.path))
            stem = e.name[:-len('.json')]
            if (task_id or stem) in building:
                continue  # already building — shown in the building lane, not here
            entries.append((mt, fast_tracked_at, e.name))
    except OSError:
        return items
    for mt, fast_tracked_at, name in order_pending(entries):
        waited = (now - datetime.fromtimestamp(mt, tz=timezone.utc)).total_seconds()
        items.append({
            'task_id': name[:-len('.json')],
            'waited_seconds': waited,
            'fast_tracked_at': fast_tracked_at,
        })
    return items


def _handle_fast_track(
    agents_root: Path, agent: str, task_id: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Stamp a queued inbox task with `fast_tracked_at` so it dispatches next.

    The write counterpart to `_reader_agent_queue_queued`. Validates that
    `task_id` is a single safe filename stem inside the agent's inbox (no
    traversal/escape), requires the task to still be QUEUED, then rewrites
    ONLY that task's JSON — atomically (the dispatcher never sees a partial
    file) and PRESERVING the file mtime so `waited_seconds` and the mtime-age
    inbox healers/reapers stay honest. The ordering itself is then applied by
    `inbox_dispatch_order` on the next scan (dispatcher) / read (panel):
    newest `fast_tracked_at` floats to the head (LIFO). Never touches a task
    that is already building. Returns {task_id, fast_tracked_at}.
    """
    task_id = (task_id or '').strip()
    inbox = agents_root / 'inboxes' / agent
    # The reader emits bare stems (no '.json', no path component), so a
    # well-behaved client never sends these. Reject anything that could escape
    # the inbox or address a dotfile/subdir.
    if (not task_id or '/' in task_id or '\\' in task_id
            or task_id.startswith('.') or '\x00' in task_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='invalid task_id',
        )
    target = inbox / f'{task_id}.json'
    # Defense in depth: the resolved file must sit DIRECTLY inside the inbox.
    try:
        if target.resolve().parent != inbox.resolve():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='invalid task_id',
            )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='invalid task_id',
        ) from exc
    if not target.is_file():
        # Already picked up (building/done) or never existed — nothing to
        # fast-track. 409 (not 404) so the UI can say "already building"
        # rather than blame a broken route.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='task is not queued',
        )
    try:
        st = target.stat()
        payload = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='task payload unreadable',
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='task payload is not an object',
        )
    # CRITICAL: refuse if a build is already running for this task. The
    # dispatcher keeps the inbox file in place for the ENTIRE build and only
    # archives it (move_to) when the build finishes — see
    # inbox_watcher.process_task. Without this guard, a fast-track that lands
    # as the build completes would let the atomic os.replace RECREATE the
    # just-archived file, and the next poll would re-dispatch it: a duplicate
    # build, a duplicate PR, and double spend — violating the watcher's "never
    # re-dispatch paid work" invariant. The in-flight registry (written by
    # run_claude) keys on the same task_stem = task.get('task_id') or the
    # filename stem; a queued (not-yet-claimed) task is absent from it and the
    # file is never moved out from under us, so the write stays safe.
    inflight_key = task_id
    explicit_id = payload.get('task_id')
    if isinstance(explicit_id, str) and explicit_id:
        inflight_key = explicit_id
    inflight = _load_in_flight_index(agents_root).get(inflight_key)
    if inflight and inflight.get('agent_id') == agent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='task is already building',
        )
    stamp = _now_utc_iso(now)
    payload['fast_tracked_at'] = stamp
    _atomic_write_json(target, payload)
    try:
        os.utime(target, (st.st_atime, st.st_mtime))
    except OSError:
        pass  # best-effort; the fast-track ordering already took effect
    return {'task_id': task_id, 'fast_tracked_at': stamp}


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

# PostgREST caps a single response at its default max-rows (1000). Page in
# 1000s under a stable (ts, event_id) order and loop until a short page proves
# the tail, so the FULL window is always returned. Without this the fetch
# silently truncated to an arbitrary, unordered 1000 once an agent exceeded
# 1000 events in the window (forge was at ~1178), so the in_review/done_today
# derivations dropped closing events (phantom cards) or review_requests
# (missing cards) nondeterministically. The MAX_PAGES backstop caps a
# pathological run at 50k rows/agent.
_QUEUE_EVENTS_PAGE_SIZE = 1000
_QUEUE_EVENTS_MAX_PAGES = 50


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
    out: list[dict[str, Any]] = []
    page = 0
    try:
        while True:
            lo = page * _QUEUE_EVENTS_PAGE_SIZE
            resp = (
                supabase_client.table('chain_events')
                .select('agent,event_type,task_id,pr_url,ts,payload')
                .eq('agent', agent)
                .gte('ts', cutoff)
                # (ts, event_id) is a total order (event_id is the PK), so page
                # boundaries are stable — no row skipped or double-counted.
                .order('ts', desc=False)
                .order('event_id', desc=False)
                .range(lo, lo + _QUEUE_EVENTS_PAGE_SIZE - 1)
                .execute()
            )
            rows = list(getattr(resp, 'data', None) or [])
            out.extend(rows)
            if len(rows) < _QUEUE_EVENTS_PAGE_SIZE:
                break  # short page => tail reached
            page += 1
            if page >= _QUEUE_EVENTS_MAX_PAGES:
                # Hard ceiling reached — the window is now INCOMPLETE. Never
                # silently truncate (the exact bug this fetch was fixed for):
                # log loudly so a genuine volume spike is visible, not masked.
                logger.warning(
                    'chain_events fetch for agent=%s hit MAX_PAGES=%d '
                    '(>=%d rows in %dd window) — result TRUNCATED',
                    agent, _QUEUE_EVENTS_MAX_PAGES,
                    _QUEUE_EVENTS_MAX_PAGES * _QUEUE_EVENTS_PAGE_SIZE,
                    _QUEUE_EVENTS_WINDOW_DAYS,
                )
                break
    except Exception:  # noqa: BLE001 — never 500 on a read-only dashboard lane
        return None
    return out


# ---- /api/system/automated-work reader (the "Automated Work" feed) ----
#
# Reads chain_events `autonomy_decision` rows — the durable record of every
# trust-policy decision (agent-core #623). The feed surfaces the auto-fired
# ones ("what the team did on its own"); the asked/rejected counts give the
# dial-in ratio. NO State Log involvement — system_state_log.py is
# local-files-only by design; autonomy_decision lives only in Supabase.

_AUTOMATED_WORK_WINDOW_DAYS = 14
# Bounded fetch so the window counts stay cheap; far above a realistic
# decision volume (a few dozen/week). When more than this many decisions
# exist in the window, counts reflect the most-recent _AUTOMATED_WORK_QUERY_CAP.
_AUTOMATED_WORK_QUERY_CAP = 500


def _automated_work_rule_label(rule: Optional[dict[str, Any]]) -> str:
    """Concise plain-language label for the matched trust rule — NEVER the
    verbose `_note`. `None` (no rule matched → default action) → 'default
    policy'."""
    if not isinstance(rule, dict):
        return 'default policy'
    source = rule.get('source') or '*'
    target = rule.get('target') or '*'
    repos = rule.get('repos')
    if isinstance(repos, str):
        repos = [repos]
    label = f'{source} → {target}'
    if repos:
        label += ' · ' + ', '.join(str(r) for r in repos)
    return label


def _reader_automated_work(
    supabase_client: Any, *, window_days: int, limit: int,
) -> dict[str, Any]:
    """Read autonomy_decision chain_events into the Automated Work feed.

    Fail-safe (never 500): a None client (test env / no creds) or any query
    error degrades to present=False with empty items + zero counts — the
    dashboard panel renders 'no data' rather than the lane erroring. Mirrors
    `_fetch_chain_events_for_agent`'s None-on-error discipline.
    """
    degraded = {
        'present': False,
        'window_days': window_days,
        'counts': {'auto_approved': 0, 'asked': 0, 'rejected': 0},
        'items': [],
        'truncated': False,
    }
    if supabase_client is None:
        return degraded
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).isoformat()
    try:
        resp = (
            supabase_client.table('chain_events')
            .select('task_id,ts,pr_url,payload')
            .eq('event_type', 'autonomy_decision')
            .gte('ts', cutoff)
            .order('ts', desc=True)
            .limit(_AUTOMATED_WORK_QUERY_CAP)
            .execute()
        )
    except Exception:  # noqa: BLE001 — never 500 a read-only dashboard lane
        return degraded
    rows = list(getattr(resp, 'data', None) or [])

    now = datetime.now(timezone.utc)
    # Enrich-at-read (#5): the deterministic plain-language meaning layer. Lazy
    # import (scripts/ is on sys.path at module load) per this file's lazy-sibling
    # convention; cached after first use, so it's free on the hot read path.
    import event_briefing  # noqa: PLC0415
    counts = {'auto_approved': 0, 'asked': 0, 'rejected': 0}
    items: list[dict[str, Any]] = []
    auto_total = 0  # all auto_approve in-window (vs items, which is capped)
    for row in rows:
        payload = row.get('payload')
        if not isinstance(payload, dict):
            continue
        decision = payload.get('decision')
        if decision == 'auto_approve':
            counts['auto_approved'] += 1
        elif decision == 'force_ask':
            counts['asked'] += 1
        elif decision == 'reject':
            counts['rejected'] += 1
        if decision != 'auto_approve':
            continue
        auto_total += 1
        if len(items) >= limit:
            continue  # keep counting auto_total for `truncated`, stop appending
        dt = _ts_to_dt(row.get('ts'))
        age = int((now - dt).total_seconds()) if dt is not None else None
        matched_rule = payload.get('matched_rule')
        item: dict[str, Any] = {
            'task_id': row.get('task_id') or payload.get('task_id'),
            'ts': row.get('ts'),
            'age_seconds': age,
            'pr_url': row.get('pr_url'),
            'decision': decision,
            'dispatched': payload.get('dispatched'),
            'source': payload.get('source'),
            'target_agent': payload.get('target_agent'),
            'target_repo': payload.get('target_repo'),
            'task_type': payload.get('task_type'),
            'summary': payload.get('summary'),
            'rule_label': _automated_work_rule_label(matched_rule),
            'rule_action': matched_rule.get('action')
            if isinstance(matched_rule, dict) else None,
        }
        # Deterministic plain-language meaning layer (briefing/risk/risk_note),
        # rendered by the same BriefingBlock/RiskBadge as the other surfaces.
        # Best-effort: a brief failure never 500s this read-only lane.
        try:
            item.update(event_briefing.decision_briefing(item))
        except Exception:  # noqa: BLE001 — never 500 a read-only dashboard lane
            pass
        items.append(item)
    return {
        'present': True,
        'window_days': window_days,
        'counts': counts,
        'items': items,
        'truncated': auto_total > len(items),
    }


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
        building = _reader_agent_queue_building(
            agents_root, worktrees_root, agent, now=now,
        )
        # A forge build keeps its worktree (and in-flight sentinel) through
        # review — the worktree is only torn down on completion — so a task
        # whose PR is now in review still reads as in-flight and would
        # double-list in BOTH the building and in_review lanes. Mirror the
        # queued->building dedup (see _reader_agent_queue_queued): the earlier
        # lane drops any task that has advanced to the later one; in_review
        # wins. Keyed on task_id, which is safe here: the building lane now
        # surfaces the sentinel's UNSANITIZED task_stem (see
        # _reader_system_worktrees), the same id the review_request carries, so
        # they compare equal even for a non-slug id whose worktree dir was
        # sanitized. When the chain_events fetch
        # is unavailable in_review degrades to [] above, so nothing is dropped
        # and the task stays in building (fail safe toward the earlier lane)
        # rather than vanishing from both.
        in_review_ids = {
            r.get('task_id') for r in in_review if r.get('task_id')
        }
        if in_review_ids:
            building = [
                b for b in building if b.get('task_id') not in in_review_ids
            ]
        return {
            'agent': agent,
            'archetype': archetype,
            'queued': queued,
            'building': building,
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


# A State Log older than this is reported `stale` so Beacon can caveat the
# answer ("this picture is N minutes old"). Sized to the narrator's cadence
# (it rides the GC tick, ~15 min) plus headroom for a skipped/slow tick.
STATE_LOG_STALE_AFTER_SEC = 25 * 60


def _reader_system_state_log(state_log_path: Path) -> dict[str, Any]:
    """Return the work-in-flight State Log doc + freshness signals (Slice 1
    § D3). Fail-safe like `_reader_projects`, NOT `_reader_missions`: a missing
    OR malformed log degrades to `present=False` (never a 500) — the State Log
    is an additive read surface and a broken file must never break the
    dashboard. Missing/unreadable → present=False, stale=True, doc fields null.
    A present, well-formed log carries an mtime-derived `last_synced_at` and a
    `stale` flag computed against STATE_LOG_STALE_AFTER_SEC."""
    empty = {
        'present': False,
        'stale': True,
        'last_synced_at': None,
        'schema_version': None,
        'as_of': None,
        'narrative_prose': None,
        'structured_snapshot': None,
        'provenance': None,
    }
    if not state_log_path.exists():
        return empty
    try:
        raw = state_log_path.read_text()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return dict(empty)
    if not isinstance(data, dict):
        return dict(empty)
    mtime = _safe_mtime(state_log_path)
    stale = mtime is None or (time.time() - mtime) > STATE_LOG_STALE_AFTER_SEC
    schema_version = data.get('schema_version')
    if not isinstance(schema_version, int):
        schema_version = None
    snapshot = data.get('structured_snapshot')
    if not isinstance(snapshot, dict):
        snapshot = None
    provenance = data.get('provenance')
    if not isinstance(provenance, dict):
        provenance = None
    narrative = data.get('narrative_prose')
    if not isinstance(narrative, str):
        narrative = None
    as_of = data.get('as_of')
    if not isinstance(as_of, str):
        as_of = None
    return {
        'present': True,
        'stale': stale,
        'last_synced_at': _iso(mtime),
        'schema_version': schema_version,
        'as_of': as_of,
        'narrative_prose': narrative,
        'structured_snapshot': snapshot,
        'provenance': provenance,
    }


def _reader_projects(projects_path: Path) -> list[dict[str, Any]]:
    """Return the raw `projects` list from the Projects-tab-v3 store, fail-safe.
    Missing file → empty list (the pipeline is just empty). Malformed JSON /
    non-dict / missing key → empty list too: the additive pipeline derive must
    NEVER break the existing board (mirrors `_reader_captures` but degrades to
    empty rather than raising, since the pipeline is an additive section)."""
    if not projects_path.exists():
        return []
    try:
        raw = projects_path.read_text()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    projects = data.get('projects')
    return projects if isinstance(projects, list) else []


# ---------------------------------------------------------------------------
# projects-v3 sequence rollup (sequence-rollup-done-flip) — the read-time join
# between a pipeline phase and the BUILD SEQUENCE it owns. A phase whose work is
# a multi-step sequence has no PR of its own, so the board must roll the
# sequence's completion up to the parent: the parent flips to Done and the
# sequence's child step-cards collapse under it instead of floating loose. Both
# helpers reuse the existing `_reader_build_sequences` response (its
# `{active, archived}` buckets) — no new endpoint, no second source of sequence
# truth — and the `sequence_ref` join key P3 already persists on each phase.
# ---------------------------------------------------------------------------
def _sequence_status_by_id(build_sequences: dict[str, Any]) -> dict[str, str]:
    """``{seq_id: status}`` over every sequence in a ``_reader_build_sequences``
    response (both the ``active`` and ``archived`` buckets — a ``complete``
    sequence lands in ``archived``). Feeds the phase-card rollup
    (``projects_store.build_pipeline``). Fail-safe: a non-dict bucket entry or a
    sequence missing a string ``seq_id``/``status`` is skipped, never raises."""
    out: dict[str, str] = {}
    for bucket in ('active', 'archived'):
        for seq in build_sequences.get(bucket) or []:
            if not isinstance(seq, dict):
                continue
            seq_id = seq.get('seq_id')
            status_val = seq.get('status')
            if isinstance(seq_id, str) and seq_id and isinstance(status_val, str):
                out[seq_id] = status_val
    return out


def _phase_linked_sequence_ids(projects: list[dict[str, Any]]) -> set[str]:
    """The set of ``seq_id``s that are the ``sequence_ref`` of SOME phase, across
    all projects regardless of project state — i.e. the sequences that have a
    parent phase card to be attributed to. A sequence NOT in this set is "bare"
    (e.g. an ordinary meta-dev sequence) and is left untouched (out of scope)."""
    refs: set[str] = set()
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        for phase in proj.get('phases') or []:
            if isinstance(phase, dict):
                ref = phase.get('sequence_ref')
                if isinstance(ref, str) and ref:
                    refs.add(ref)
    return refs


def _collapsed_step_task_ids(
    projects: list[dict[str, Any]], build_sequences: dict[str, Any],
) -> set[str]:
    """task_ids of every STEP belonging to a PHASE-LINKED build sequence — the
    ids the orphan derive must collapse (attribute to the parent phase) rather
    than surface as standalone loose cards. A step's chain-event ``task_id`` is
    its ``step_id`` (the dispatched task id); we also accept an explicit
    ``task_id``/``id`` field for robustness. Only steps of sequences in
    ``_phase_linked_sequence_ids`` are collected — bare (non-phase-linked)
    sequences are out of scope. Fail-safe over junk: a non-dict project/
    sequence/step is skipped, never raises."""
    linked = _phase_linked_sequence_ids(projects)
    if not linked:
        return set()
    out: set[str] = set()
    for bucket in ('active', 'archived'):
        for seq in build_sequences.get(bucket) or []:
            if not isinstance(seq, dict) or seq.get('seq_id') not in linked:
                continue
            for step in seq.get('steps') or []:
                if not isinstance(step, dict):
                    continue
                for key in ('task_id', 'step_id', 'id'):
                    val = step.get(key)
                    if isinstance(val, str) and val:
                        out.add(val)
    return out


def _sequence_owned_task_ids(build_sequences: dict[str, Any]) -> set[str]:
    """Every task_id OWNED by a registered build sequence — its ``seq_id`` PLUS
    every step's ``task_id``/``step_id``/``id`` — over BOTH the ``active`` and
    ``archived`` buckets of a ``_reader_build_sequences`` response, INDEPENDENT of
    phase-linkage. Unlike ``_collapsed_step_task_ids`` (which only collects steps
    of phase-LINKED sequences, to attribute them to a parent phase card), this
    suppresses sequence-owned ids from the loose-orphan surface even for a "bare"
    sequence (e.g. a completed meta-dev sequence with no projects.json phase):
    such an id is sequence work, never a standalone initiative. The orphan
    exclusion set is the UNION of the two. Fail-safe over junk: a non-dict
    bucket entry / sequence / step is skipped, never raises."""
    out: set[str] = set()
    for bucket in ('active', 'archived'):
        for seq in build_sequences.get(bucket) or []:
            if not isinstance(seq, dict):
                continue
            seq_id = seq.get('seq_id')
            if isinstance(seq_id, str) and seq_id:
                out.add(seq_id)
            for step in seq.get('steps') or []:
                if not isinstance(step, dict):
                    continue
                for key in ('task_id', 'step_id', 'id'):
                    val = step.get(key)
                    if isinstance(val, str) and val:
                        out.add(val)
    return out


# ---------------------------------------------------------------------------
# Projects-tab-v3 P3 — the on-disk write side of Promote (p3-promote-endpoint).
#
# Promote is a MOVE (spec § 0 / § 4 decision 2): it relocates a funnel item
# (parked capture / proposed mission) into a NEW single-phase project at
# Brainstorm and removes it from its funnel lane. The dashboard stays a
# NON-committer to projects.json: this writes the new project to disk ATOMICALLY
# under a lock; `heal_projects_store.py` (the SOLE committer) version-controls
# the delta on its next tick (single-committer invariant, spec § 5).
#
# A capture and a mission are removed from the funnel by DIFFERENT mechanisms,
# both reversible with no data loss:
#   * capture — flipped to state:'promoted' on captures.json (its own committer);
#     the parked lane already excludes non-parked captures.
#   * mission — NOT mutated. The funnel derive (_build_funnel) suppresses a
#     proposed mission whose id appears as `promoted_from.mission_id` in an
#     ACTIVE project. Archiving the project un-suppresses it → it returns to the
#     funnel. This avoids a throwaway missions.json PR and keeps the mission's
#     single committer untouched.
# ---------------------------------------------------------------------------
_PROJECTS_INGEST_LOCK = __import__('threading').Lock()


def _read_projects_registry(projects_path: Path) -> dict[str, Any]:
    """Load projects.json as a mutable registry dict ({schema_version,
    projects}). Missing/empty file → a fresh empty registry. Malformed JSON →
    HTTPException(500), so the write path never appends onto a corrupt file
    (mirrors `_read_captures_registry`)."""
    empty = projects_store.empty_registry()
    if not projects_path.exists():
        return empty
    try:
        raw = projects_path.read_text()
        data = json.loads(raw) if raw.strip() else empty
    except (OSError, json.JSONDecodeError) as e:
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={'error': 'projects.json malformed', 'detail': first_line},
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                'error': 'projects.json malformed',
                'detail': 'top-level JSON is not an object',
            },
        )
    if not isinstance(data.get('projects'), list):
        data['projects'] = []
    data.setdefault('schema_version', projects_store.SCHEMA_VERSION)
    return data


def _promoted_from_matches(project: Any, promoted_from: dict[str, Any]) -> bool:
    """True iff `project`'s provenance back-reference matches `promoted_from`
    on (kind + the kind's id key). Used for both idempotency (a double-click
    finds the project it already created) and funnel suppression."""
    if not isinstance(project, dict):
        return False
    pf = project.get('promoted_from')
    if not isinstance(pf, dict):
        return False
    kind = promoted_from.get('kind')
    if pf.get('kind') != kind:
        return False
    id_key = {
        'capture': 'capture_id',
        'mission': 'mission_id',
        'orphan': 'task_id',
    }.get(kind)
    if id_key is None:
        return False
    return bool(promoted_from.get(id_key)) and pf.get(id_key) == promoted_from.get(id_key)


def _find_active_project_by_promoted_from(
    projects: list[Any], promoted_from: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """The ACTIVE project already created from this funnel item, or None. Only
    ACTIVE projects count: archiving a project (the reversibility escape hatch)
    must let the item be re-promoted, so an archived match is not idempotent."""
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        if proj.get('state', projects_store.DEFAULT_PROJECT_STATE) != 'active':
            continue
        if _promoted_from_matches(proj, promoted_from):
            return proj
    return None


def _unique_project_id(base_id: str, projects: list[Any]) -> str:
    """`base_id`, or `base_id-2`, `base_id-3`, … — the first id not already used
    by ANY project (active or archived), so a new promote never silently
    overwrites an existing or archived project."""
    existing = {p.get('id') for p in projects if isinstance(p, dict)}
    if base_id not in existing:
        return base_id
    for n in range(2, 1000):
        candidate = f'{base_id}-{n}'
        if candidate not in existing:
            return candidate
    # Astronomically unlikely; fall back to a length-disambiguated id.
    return f'{base_id}-{len(projects)}'


def _create_project_from_funnel(
    *,
    projects_path: Path,
    title: str,
    desired_end_state: str,
    repo: Optional[str],
    north_star_ref: Optional[str],
    promoted_from: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Atomically append a NEW single-phase project at Brainstorm to
    projects.json (the dashboard is a non-committer — heal_projects_store commits
    the delta). Idempotent: if an ACTIVE project already carries this
    `promoted_from`, return it instead of minting a duplicate (a double-click /
    re-drain is a no-op). Returns
    {project_id, phase_id, status: 'created'|'exists', applied}.

    Caller-held locks: capture-promote holds _CAPTURE_INGEST_LOCK then nests
    _PROJECTS_INGEST_LOCK here (CAPTURE→PROJECTS); the funnel/mission paths take
    only _PROJECTS_INGEST_LOCK — no path takes them in the reverse order, so
    there is no lock inversion."""
    # Drop a non-buildable repo before it lands in projects.json. A capture
    # emitted from a local working dir inherits that dir's name as origin.repo
    # (e.g. `ol-work`), which is not a real repo; storing it would ride all the
    # way to an unbuildable dispatch (the 2026-06-19 wedge). Store None instead
    # — the Launch endpoint re-derives the real repo from the spec at build
    # time. Fail open: an unreadable config (empty valid set) keeps the
    # candidate untouched.
    if repo is not None:
        valid_repos = _valid_repo_names(_agent_models_json_path())
        if valid_repos and repo not in valid_repos:
            logger.info(
                'promote: dropping non-buildable repo %r (not in repo_paths) '
                'for project %r; launch will derive from the spec',
                repo, title,
            )
            repo = None
    with _PROJECTS_INGEST_LOCK:
        registry = _read_projects_registry(projects_path)
        projects = registry['projects']

        existing = _find_active_project_by_promoted_from(projects, promoted_from)
        if existing is not None:
            phases = existing.get('phases') or []
            phase_id = phases[0].get('id') if phases and isinstance(phases[0], dict) else None
            return {
                'project_id': existing.get('id'),
                'phase_id': phase_id,
                'status': 'exists',
                'applied': False,
            }

        project_id = _unique_project_id(projects_store.slugify(title), projects)
        project = projects_store.new_single_phase_project(
            title=title,
            desired_end_state=desired_end_state,
            north_star_ref=north_star_ref,
            repo=repo,
            promoted_from=promoted_from,
            project_id=project_id,
            phase_id=project_id,
            now=now,
        )
        projects.append(project)
        registry['schema_version'] = projects_store.SCHEMA_VERSION
        try:
            _atomic_write_json(projects_path, registry)
        except OSError as e:
            first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={'error': 'projects write failed', 'detail': first_line},
            )
    return {
        'project_id': project_id,
        'phase_id': project_id,
        'status': 'created',
        'applied': True,
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
    queue_dir: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/system/missions/new.

    Registers a mission WITHOUT opening a PR or touching git. Steps:
      1. Derive kebab mission_id. Reject 400 if empty after kebab.
      2. Acquire in-process lock (serializes concurrent POSTs).
      3. Read local missions.json (read-only); 409 on a duplicate id.
      4. 409 if the same id is already queued (an in-flight dup not yet drained).
      5. Atomically drop `<queue_dir>/<mission_id>.json` (the full mission
         entry) and return {mission_id, status: 'queued'}.

    The dashboard is NOT a git committer: it never writes or commits
    `agents/beacon/missions.json`. The owning healer (heal_orphan_autoregister,
    the missions single-committer) drains the queue into missions.json on its
    commit cycle and removes the file once the mission is confirmed on
    origin/main. This honors the machine-owned-file single-committer invariant
    (one committer per auto-committed runtime file) and ends the throwaway-PR
    storm the old PR-per-mission flow created (each PR was unroutable through
    the Forge pipeline, so it sat open and paged heal-pipeline-stall until a
    reconciler closed it). The mission surfaces on the board on the next poll
    after the healer commits it — the same post-merge latency the PR flow had,
    minus the PR.
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

    # Default to the legacy 'drafting' phase; the only other accepted value is
    # 'proposed' (P3b retrospective author). Anything else is a 400 so a typo'd
    # phase can't smuggle an unknown lifecycle state onto the board.
    phase = body.phase or 'drafting'
    if phase not in ('drafting', 'proposed'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                'error': 'invalid phase',
                'detail': f"phase must be 'drafting' or 'proposed', got {phase!r}",
            },
        )

    now = now or datetime.now(timezone.utc)
    new_entry: dict[str, Any] = {
        'id': mission_id,
        'name': body.name,
        'phase': phase,
        'brief': body.brief,
        'spec_docs': list(body.spec_docs),
        'task_ids': [],
        'repo': body.repo,
        'created': now.date().isoformat(),
        'deferred_reason': None,
    }
    # Stamp the proposal provenance only for proposed cards so a drafting
    # mission's shape is byte-for-byte what it was before this field existed.
    if phase == 'proposed':
        # `proposed_by` and `predraft` land verbatim in the auto-committed
        # missions.json registry, so bound both: cap the provenance string and
        # reject an oversized pre-draft that would bloat every registry read
        # (review #749 finding 5). Stage B's own fields are ≤800 chars, so 8 KB
        # is generous headroom for a well-formed predraft.
        new_entry['proposed_by'] = (body.proposed_by or 'unknown')[:64]
        new_entry['proposed_at'] = now.isoformat()
        if body.predraft is not None:
            if len(json.dumps(body.predraft, default=str)) > 8192:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        'error': 'predraft too large',
                        'detail': 'predraft must serialize to <= 8192 bytes',
                    },
                )
            new_entry['predraft'] = body.predraft

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

        # Already queued (registered to the queue but not yet drained into
        # missions.json) → reject as an in-flight dup so a double-submit can't
        # enqueue the same mission twice. (Even without this the healer dedups
        # on append, so it's a UX nicety, not a correctness guard.)
        queue_path = queue_dir / f'{mission_id}.json'
        if queue_path.exists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'mission_id queued',
                    'id': mission_id,
                    'hint': (
                        'A mission with this name is already queued for '
                        'registration; it will appear on the board shortly.'
                    ),
                },
            )

        try:
            _atomic_write_json(queue_path, new_entry)
        except OSError as e:
            first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={'error': 'queue write failed', 'detail': first_line},
            )

    return {'mission_id': mission_id, 'status': 'queued'}


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
    'dag-preflight-',       # DAG-preflight session-start artifact (a validation
                            # run, never a buildable initiative)
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

# Whole-id noise SHAPES (anchored / delimited regexes): orphans that are never
# buildable initiatives — an approval-thread hash, a redispatch artifact (a re-kick
# of work already tracked elsewhere), an ops-cleanup ledger snapshot. Anchored so
# they can't sweep a genuine buildable (e.g. `rebase-pr252-…` and `pulse-check-iii-…`
# match NONE of these and stay proposable — they drain via the terminal gate).
_NON_PROPOSABLE_TASK_ID_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r'^larry-approval-[0-9a-f]{40}$'),   # approval-thread hash artifact
    re.compile(r'-redispatch-\d{8}T\d{6}Z$'),       # trailing redispatch timestamp
    re.compile(r'^ops-cleanup.*-ledger-\d{8}$'),    # ops-cleanup*-ledger-<YYYYMMDD>
)


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
    if any(rx.search(task_id) for rx in _NON_PROPOSABLE_TASK_ID_RES):
        return False
    return True


def _ts_key(ts: Optional[str]) -> datetime:
    """Sort key: parsed ts, with unparseable/missing sorting oldest."""
    return _ts_to_dt(ts) or datetime.min.replace(tzinfo=timezone.utc)


def detect_orphans(
    events: list[dict[str, Any]],
    registered_task_ids: set[str],
    collapsed_task_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Port of `detectOrphans`. task_ids in chain_events but not registered in
    any mission; infrastructure events filtered out. Output newest-first by
    last event ts. The caller supplies the time window (no filtering here).

    ``collapsed_task_ids`` (projects-v3 sequence-rollup-done-flip): task_ids
    that are STEPS of a build sequence LINKED to a pipeline phase
    (``_collapsed_step_task_ids``). They are excluded exactly like a registered
    task_id — a phase-linked step is attributed to its parent phase card, never
    surfaced as a standalone (loose) orphan/board card. None → no collapse, so
    existing callers (heal_orphan_autoregister) are unaffected."""
    collapsed = collapsed_task_ids or set()
    by_task: dict[str, dict[str, Any]] = {}
    for ev in events:
        tid = ev.get('task_id')
        if not tid or tid in registered_task_ids or tid in collapsed:
            continue
        # Phase 4 step 1b: card_message events are keyed by capture_id (a
        # capture, not a mission task) so a thread is one query. They'd
        # otherwise surface every captured card in the Orphans lane — they
        # are conversation rows on a capture, never standalone chain work.
        if (ev.get('event_type') or '') == 'card_message':
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


def _is_prompt_blob_title(title: str) -> bool:
    """True when a desktop_session payload.title looks like a truncated prompt
    fragment rather than a real conversation title. The desktop emitter
    populates payload.title with the first ~80 chars of the chat's prompt text,
    so ~20 desktop orphans all render the same blob ("You are characterizing a
    software component...") — less readable than repo/branch. A title is
    prompt-like when it is long (> 60 chars) OR ends with a truncation ellipsis
    (unicode horizontal ellipsis or a literal three-dot run)."""
    t = title.strip()
    return len(t) > 60 or t.endswith('…') or t.endswith('...')


def _orphan_label_and_location(
    events: list[dict[str, Any]],
    task_id: str,
) -> tuple[str, Optional[str], Optional[str]]:
    """Resolve an orphan's readable label + repo/branch (§ 3.4).

    Label resolution order: a MEANINGFUL desktop chat title (latest
    desktop_session_* event's payload.title) > repo/branch (from event payload)
    > a prompt-blob title (last resort, still better than the raw hash) >
    humanized task_id. Events are newest-first. A prompt-like title (see
    `_is_prompt_blob_title`) does not pre-empt repo/branch. Degrades gracefully
    when fields are absent (the desktop emitter may not yet populate
    payload.title)."""
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
    if title and not _is_prompt_blob_title(title):
        label = title
    elif repo and branch:
        label = f'{repo}/{branch}'
    elif repo:
        label = repo
    elif title:  # prompt-blob title — last resort, still beats the raw hash
        label = title
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


def _spawned_expected_cost_usd(raw: dict[str, Any]) -> Optional[float]:
    """Phase S (S6): the work's *estimated* cost captured on the spawned ref
    (`expected_cost_usd`, sourced from the build-sequence step's estimate), as a
    number — or None when absent/non-numeric. A bool is rejected (JSON booleans
    are ints in Python, but a cost is never a flag)."""
    v = raw.get('expected_cost_usd')
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _spawned_fields(
    cap: dict[str, Any],
    events_by_task_id: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Phase S (S1/S2/S6): echo the capture's `spawned` ref (the join key back to
    the work the card created) plus the linked work's derived phase + pr_url and
    the work's estimated cost (S6).

    The phase is computed through the EXISTING chain_events derive
    (`derive_phase_for_task`) — Phase S adds no new state machine. The `spawned`
    ref is normalized to known string keys so a garbage stamp degrades to the
    neutral None state (mirrors `_meaning_layer_fields`).

    `spawned_phase` stays None until the linked work emits its first chain_event:
    a freshly-delegated card whose work hasn't started surfaces None (neutral),
    never a misleading 'ready' from an empty event list.

    `spawned_expected_cost_usd` (S6) echoes the build-sequence step's estimated
    cost stamped on the ref, degrading to None — a card whose spawned ref carries
    no estimate surfaces no cost rather than a misleading 0."""
    raw = cap.get('spawned')
    if not isinstance(raw, dict):
        return {'spawned': None, 'spawned_phase': None, 'spawned_pr_url': None,
                'spawned_expected_cost_usd': None}
    ref: dict[str, Any] = {}
    for k in ('task_id', 'kind', 'mission_id', 'stamped_at'):
        v = raw.get(k)
        if isinstance(v, str) and v:
            ref[k] = v
    if not ref:
        return {'spawned': None, 'spawned_phase': None, 'spawned_pr_url': None,
                'spawned_expected_cost_usd': None}

    phase: Optional[str] = None
    pr_url: Optional[str] = None
    task_id = ref.get('task_id')
    if task_id:
        events = events_by_task_id.get(task_id) or []
        if events:
            phase = derive_phase_for_task(events, None)
            pr_url = summarize_task_events(events)['pr_url']
    return {'spawned': ref, 'spawned_phase': phase, 'spawned_pr_url': pr_url,
            'spawned_expected_cost_usd': _spawned_expected_cost_usd(raw)}


def _parked_spawned_task_ids(captures: list[dict[str, Any]]) -> list[str]:
    """The spawned task_ids linked to parked captures (Phase S S2). The derive
    fetches chain_events for these alongside the registered mission task_ids so
    the parked lane can surface the linked work's phase via the same derive.
    De-dupes; order-stable."""
    out: list[str] = []
    seen: set[str] = set()
    for cap in captures:
        if not isinstance(cap, dict) or cap.get('state') != 'parked':
            continue
        spawned = cap.get('spawned')
        if not isinstance(spawned, dict):
            continue
        tid = spawned.get('task_id')
        if isinstance(tid, str) and tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _parked_from_captures(
    captures: list[dict[str, Any]],
    now: datetime,
    events_by_task_id: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> list[dict[str, Any]]:
    """Build the `parked[]` array (§ 3.3) from captures.json. Only state=='parked'
    captures; `aging` is the GC healer's persisted flag (Phase 1 — never
    recomputed here). `area` is reserved (always None today — scene-graph T8).

    Phase 3 § 4.3: a capture snoozed past `now` (`snoozed_until` in the future)
    is suppressed from the Parked lane / resurfacing until the snooze elapses.

    Phase 4 § 4: the meaning-layer fields (briefing/risk/risk_note/
    recommended_action/briefing_provenance) ride along, validated so an
    un-briefed capture surfaces None (neutral state) rather than raw machine
    fields.

    Phase S (S1/S2): the spawned-ref fields (spawned/spawned_phase/
    spawned_pr_url) ride along too — the linked work's derived phase comes from
    the existing chain_events derive (`events_by_task_id`)."""
    events_by_task_id = events_by_task_id or {}
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
        entry.update(_spawned_fields(cap, events_by_task_id))
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
            # event_id is selected so _shape_thread_message can project it as the
            # per-message `id` (Phase 4b Contract C); additive for other callers.
            .select('event_id,event_type,task_id,agent,pr_url,ts,payload')
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


# ---------------------------------------------------------------------------
# Projects Tab v3 — P1 C4: the additive funnel grouping
# (spec: agents/beacon/specs/projects-v3-p1-funnel-retire-missions.md § 4 C4)
#
# A PURE re-view over the already-derived missions/orphans/parked sections — it
# adds NO new data source and removes/renames NO existing field. The live board
# keeps rendering off missions[]/orphans[]/parked[]; P2 consumes `funnel`. Intake
# is triaged into two lanes:
#   primary   — parked captures + genuinely team-suggested missions (front-and-
#               -centre: your stuff + the team's suggestions)
#   secondary — orphaned work, AUTO-FILTERED (verifiably-terminal items dropped —
#               the dead orphan clutter is cleared; live/uncertain ones kept)
# Suggested-source (Beacon / Medic / Pulse) is tagged where the provenance is
# known ("where available" per C4); Larry's own desktop captures and
# builder-emitted orphans carry suggested_source=None.
# ---------------------------------------------------------------------------

# Canonical suggesting sources. A provenance hint normalizes to one of these or
# to None — the funnel never invents a source it can't identify. `closeout` is a
# non-agent source (projects-v3 P4): the phase-closeout pass drops loose ends it
# finds into the suggested lane, tagged `proposed_by='closeout'`.
_SUGGESTED_AGENTS: tuple[str, ...] = ('beacon', 'medic', 'pulse', 'closeout')
# Provenance hint → canonical suggesting agent. 'pulse-check-i' is the recurring
# Pulse proposal parked in the Missions lane (see CAPTURE_ALLOWED_LABELS);
# 'retrospective-author' is the weekly Pulse retrospective (P3b). Both are
# Pulse-family suggestions, so they belong in the PRIMARY suggested lane — without
# this mapping a retrospective card's proposed_by normalizes to None and falls into
# the secondary orphan-clutter lane, defeating the feature (review #749 finding 1).
_CAPTURE_LABEL_SUGGESTED_SOURCE: dict[str, str] = {
    'pulse-check-i': 'pulse',
    'retrospective-author': 'pulse',
}


def _normalize_suggested_source(*candidates: Any) -> Optional[str]:
    """Map a raw provenance hint (capture label, mission proposed_by, orphan
    agent) to a canonical suggesting agent {beacon, medic, pulse}, or None when
    no team agent is identifiable. First identifiable candidate wins."""
    for c in candidates:
        if not isinstance(c, str):
            continue
        v = c.strip().lower()
        if not v:
            continue
        mapped = _CAPTURE_LABEL_SUGGESTED_SOURCE.get(v)
        if mapped:
            return mapped
        for agent in _SUGGESTED_AGENTS:
            if v == agent or v.startswith(agent + '-') or v.startswith(agent + '_'):
                return agent
    return None


def _proposed_mission_is_dead(mission: dict[str, Any]) -> bool:
    """A `proposed` orphan-mission is verifiably dead once the drain (C2/C3)
    acknowledges, retires, or archives it. The funnel's auto-filter drops these
    from the secondary lane — never on a clock, only on a real terminal flag."""
    if mission.get('acknowledged') is True:
        return True
    if mission.get('retired_at'):
        return True
    return (mission.get('phase') or '') in ('retired', 'archived', 'closed')


def _funnel_item(
    kind: str,
    ref: Any,
    label: Any,
    repo: Any,
    suggested_source: Optional[str],
) -> dict[str, Any]:
    """A lightweight funnel entry referencing a source intake item by id, so P2
    can render the lane and join back to the full missions/orphans/parked object."""
    return {
        'kind': kind,
        'ref': ref,
        'label': label,
        'repo': repo,
        'suggested_source': suggested_source,
    }


def _build_funnel(
    missions: list[dict[str, Any]],
    orphans: list[dict[str, Any]],
    parked: list[dict[str, Any]],
    now: Optional[datetime] = None,
    promoted_mission_ids: Optional[set[str]] = None,
    promoted_orphan_task_ids: Optional[set[str]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Group the existing intake (parked + proposed missions + orphans) into the
    primary/secondary funnel (C4). Pure over its three inputs — call it with the
    POST-filter arrays so the funnel tracks any ?repo=/?task_id= narrowing.

    P2 Contract B: a proposed mission snoozed past `now` (`snoozed_until` in the
    future) is suppressed from the funnel until the snooze elapses, mirroring the
    parked-capture lane (`_parked_from_captures`). Parked captures arrive already
    snooze-filtered by the derive; missions are filtered here.

    projects-v3 P3 (p3-promote-endpoint): a proposed mission whose id is in
    ``promoted_mission_ids`` (an ACTIVE project's `promoted_from.mission_id`) has
    been MOVED into the pipeline by Accept/Promote → it is suppressed from the
    funnel. The mission is never mutated; archiving the project drops it from this
    set, returning the mission to the funnel (reversible, no data loss).

    projects-v3 P3 follow-up (p3f-reversibility-and-orphan): a raw orphan whose
    task_id is in ``promoted_orphan_task_ids`` (an ACTIVE project's
    `promoted_from.task_id` where kind=='orphan') has likewise been MOVED into
    the pipeline → it is suppressed from the secondary lane. Archiving the project
    drops it from this set, returning the orphan to the funnel."""
    now = now or datetime.now(timezone.utc)
    promoted_mission_ids = promoted_mission_ids or set()
    promoted_orphan_task_ids = promoted_orphan_task_ids or set()
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []

    # Parked captures are always primary intake. A capture whose provenance maps
    # to a team agent is a 'suggested' item; otherwise it's Larry's own 'parked'
    # capture. (parked[] carries `label` as its provenance tag.)
    for p in parked:
        src = _normalize_suggested_source(p.get('label'))
        primary.append(_funnel_item(
            'suggested' if src else 'parked',
            p.get('capture_id'),
            p.get('label') or p.get('title'),
            p.get('repo'),
            src,
        ))

    # Only INTAKE missions belong in the funnel: a `proposed` mission is awaiting
    # accept/dismiss triage. Established work (drafting/ready/in_flight/shipped/
    # deferred) renders via the existing missions[] section / the P3 pipeline and
    # is intentionally excluded here. A proposed mission is either a genuine team
    # suggestion (→ primary) or orphan-derived auto-register clutter (→ secondary,
    # auto-filtered once dead).
    for m in missions:
        if (m.get('phase') or '') != 'proposed':
            continue
        # projects-v3 P3: a mission already MOVED into the pipeline (its id is an
        # active project's promoted_from.mission_id) leaves the funnel.
        if m.get('id') in promoted_mission_ids:
            continue
        # Contract B: a snoozed proposed mission hides until the snooze elapses
        # (mirrors the parked-capture lane). Applies to both suggested → primary
        # and orphan-derived → secondary threads.
        if _is_snoozed(m, now):
            continue
        proposed_by = m.get('proposed_by')
        src = _normalize_suggested_source(proposed_by)
        if src is not None:
            primary.append(_funnel_item(
                'suggested', m.get('id'), m.get('name'), m.get('repo'), src,
            ))
        elif not _proposed_mission_is_dead(m):
            # Orphan-derived (heal_orphan_autoregister) or unknown proposer:
            # keep visible in secondary until a real terminal signal retires it.
            secondary.append(_funnel_item(
                'orphan', m.get('id'), m.get('name'), m.get('repo'), None,
            ))

    # Orphans are secondary, AUTO-FILTERED: a verifiably-terminal orphan (merged
    # or explicitly-closed PR) is dropped (dead clutter cleared); every live /
    # stalled / uncertain orphan stays.
    for o in orphans:
        if o.get('terminal') is True:
            continue
        # p3f-reversibility-and-orphan: an orphan already MOVED into an active
        # project (its task_id is a project's promoted_from.task_id) leaves the
        # funnel; archiving the project returns it here.
        if o.get('task_id') in promoted_orphan_task_ids:
            continue
        secondary.append(_funnel_item(
            'orphan',
            o.get('task_id'),
            o.get('label') or o.get('task_id'),
            o.get('repo'),
            _normalize_suggested_source(o.get('agent')),
        ))

    return {'primary': primary, 'secondary': secondary}


def _promoted_mission_ids(projects: list[dict[str, Any]]) -> set[str]:
    """The mission ids that SUPPRESSING projects were promoted from (projects-v3
    P3). Read from each suppressing project's `promoted_from.mission_id`; the
    funnel derive uses this to suppress a proposed mission already MOVED into the
    pipeline. `active` AND `retired` projects suppress (a retired Done project's
    mission stays out of the funnel — it was completed, not dropped); only an
    `archived` project releases its mission back to the funnel (reversible)."""
    out: set[str] = set()
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        if not projects_store.suppresses_funnel_source(
                proj.get('state', projects_store.DEFAULT_PROJECT_STATE)):
            continue
        pf = proj.get('promoted_from')
        if isinstance(pf, dict) and pf.get('kind') == 'mission':
            mid = pf.get('mission_id')
            if isinstance(mid, str) and mid:
                out.add(mid)
    return out


def _promoted_orphan_task_ids(projects: list[dict[str, Any]]) -> set[str]:
    """The orphan task_ids that SUPPRESSING projects were promoted from (projects-
    v3 P3 follow-up, p3f-reversibility-and-orphan). Read from each suppressing
    project's `promoted_from.task_id` where kind=='orphan'; the funnel derive uses
    this to suppress a raw orphan already MOVED into the pipeline. `active` AND
    `retired` projects suppress (a retired Done orphan stays out of the funnel);
    only an `archived` project releases its orphan back to the funnel
    (reversible)."""
    out: set[str] = set()
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        if not projects_store.suppresses_funnel_source(
                proj.get('state', projects_store.DEFAULT_PROJECT_STATE)):
            continue
        pf = proj.get('promoted_from')
        if isinstance(pf, dict) and pf.get('kind') == 'orphan':
            tid = pf.get('task_id')
            if isinstance(tid, str) and tid:
                out.add(tid)
    return out


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
    promoted_mission_ids: Optional[set[str]] = None,
    promoted_orphan_task_ids: Optional[set[str]] = None,
    collapsed_task_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Pure derive: build the full /api/missions/derived response (pre-filter).

    `missions` + `orphans` (minus the additive orphan-readability keys) match
    the dashboard's MissionListResponse byte-for-byte — that's what § 4 pins.

    `pr_state_resolver` (optional) maps orphan pr_urls → live GitHub states for
    terminal detection (§ 3.4). None → no PR-state read (event-only derive); the
    route injects the real, fail-safe resolver, tests inject a stub. Mission-task
    pr_state always stays None regardless (parity-pinned).

    `collapsed_task_ids` (projects-v3 sequence-rollup-done-flip): step task_ids
    of phase-linked build sequences (`_collapsed_step_task_ids`) excluded from
    the orphan surface so a phase-linked step never floats as a standalone card.
    None → no collapse (unchanged behaviour).
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

    orphans = detect_orphans(
        recent_events, registered_task_ids, collapsed_task_ids)
    # § 4.8: the Orphaned secondary lane surfaces only buildable initiatives.
    # Narrow the orphans SURFACED to the dashboard (orphans[] + funnel.secondary,
    # built below) by the same is_proposable_initiative gate heal_orphan_autoregister
    # uses to pick what's a buildable mission — sweeping the non-buildable noise an
    # orphan can be (chain-incident/alert artifacts, desktop-capture hashes,
    # sequence-step proposals, dag-preflight runs, dated-digest / translation /
    # stale-fixture ids). detect_orphans' broader everything-in-flight semantics are
    # UNCHANGED: heal_orphan_autoregister calls it directly and applies this same
    # gate separately, so its decision queue is unaffected by this view-level filter.
    orphans = [
        o for o in orphans
        if is_proposable_initiative(o.get('task_id', ''), o.get('agent'))
    ]
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

    parked = _parked_from_captures(captures, now, events_by_task_id)
    return {
        'schema_version': 1,
        'missions': missions,
        'orphans': orphans,
        'parked': parked,
        # C4: additive funnel grouping. Built post-derive; re-derived against the
        # filtered arrays in _apply_derived_filters so it tracks ?repo=/?task_id=.
        'funnel': _build_funnel(
            missions, orphans, parked, now,
            promoted_mission_ids, promoted_orphan_task_ids,
        ),
        'last_synced_at': last_synced_at,
        'as_of': _now_utc_iso(now),
    }


def _apply_derived_filters(
    response: dict[str, Any],
    *,
    repo: Optional[str],
    task_id: Optional[str],
    now: Optional[datetime] = None,
    promoted_mission_ids: Optional[set[str]] = None,
    promoted_orphan_task_ids: Optional[set[str]] = None,
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
    # C4: re-derive the funnel against the filtered arrays so the grouping stays
    # consistent with the narrowed missions/orphans/parked sections.
    response['funnel'] = _build_funnel(
        missions, orphans, parked, now,
        promoted_mission_ids, promoted_orphan_task_ids,
    )
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
    projects_path: Optional[Path] = None,
    build_sequences_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Pure handler for GET /api/missions/derived. Reads the registry +
    captures, fetches chain_events, derives, applies filters.

    `pr_state_resolver` defaults to None (no PR-state read) so direct callers
    and unit tests are network-free; the route passes the real
    `_resolve_orphan_pr_states`. See `_build_derived_response`.

    `projects_path` (projects-v3 P3) is read for the additive "Actively working"
    pipeline; None → the pipeline section is an empty list, so existing callers
    and tests that don't pass it are unaffected.

    `build_sequences_root` (projects-v3 sequence-rollup-done-flip) is the
    blackboard build-sequences dir; None → the live `_sequence_blackboard_root()`
    (tests inject a tmp dir). It is read ONCE (reusing `_reader_build_sequences`)
    to drive BOTH the phase-card Done-flip rollup and the orphan step-collapse."""
    missions_data = _reader_missions(missions_path)
    captures_data = _reader_captures(captures_path)
    entries = missions_data.get('missions') or []
    captures = captures_data.get('captures') or []

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

    # Phase S (S2): also fetch chain_events for the work spawned by parked
    # captures so the parked lane can surface its derived phase via the same
    # derive. Union with the registered mission task_ids → one query.
    fetch_ids = list(registered)
    for tid in _parked_spawned_task_ids(captures):
        if tid not in seen:
            seen.add(tid)
            fetch_ids.append(tid)

    events_by_task_id = _fetch_events_for_task_ids(supabase_client, fetch_ids)
    recent_events = _fetch_recent_chain_events(
        supabase_client, _ORPHAN_WINDOW_DAYS, now,
    )

    # projects-v3 P3: read the pipeline store once. `promoted_mission_ids` feeds
    # the funnel derive (suppress a mission already MOVED into a project); the
    # project list also serves the additive "Actively working" pipeline below.
    projects = _reader_projects(projects_path) if projects_path else []
    promoted_mission_ids = _promoted_mission_ids(projects)
    promoted_orphan_task_ids = _promoted_orphan_task_ids(projects)

    # projects-v3 sequence-rollup-done-flip: read the build sequences ONCE and
    # derive both the {seq_id: status} rollup map (Done-flip) and the
    # phase-linked step task_ids to collapse out of the orphan surface. Fail-safe
    # — `_reader_build_sequences` degrades to empty buckets on a missing dir, so
    # both derived sets are empty and the board renders exactly as before.
    build_sequences = _reader_build_sequences(
        build_sequences_root or _sequence_blackboard_root(), now)
    sequence_status_by_id = _sequence_status_by_id(build_sequences)
    # Suppress sequence-owned ids from the loose-orphan surface: the UNION of the
    # phase-linked step ids (attributed to a parent phase card) AND every id OWNED
    # by ANY registered sequence (seq_id + step ids), so a bare/completed sequence
    # with no parent phase (e.g. projects-v3-p4) no longer leaks as loose orphans.
    collapsed_task_ids = (
        _collapsed_step_task_ids(projects, build_sequences)
        | _sequence_owned_task_ids(build_sequences)
    )

    response = _build_derived_response(
        entries=entries,
        last_synced_at=missions_data.get('last_synced_at'),
        captures=captures,
        events_by_task_id=events_by_task_id,
        recent_events=recent_events,
        now=now,
        pr_state_resolver=pr_state_resolver,
        promoted_mission_ids=promoted_mission_ids,
        promoted_orphan_task_ids=promoted_orphan_task_ids,
        collapsed_task_ids=collapsed_task_ids,
    )
    response = _apply_derived_filters(
        response, repo=repo, task_id=task_id, now=now,
        promoted_mission_ids=promoted_mission_ids,
        promoted_orphan_task_ids=promoted_orphan_task_ids,
    )

    # projects-v3 P3: additive "Actively working" pipeline. Built AFTER the
    # existing-board filters and injected as a NEW key, so it cannot perturb the
    # missions/orphans/parked/funnel sections. Repo filter applies (a project
    # carries an optional repo); the task_id filter narrows the active task-set
    # view of missions/orphans and does not apply to the project-level pipeline.
    pipeline = projects_store.build_pipeline(
        projects, now, sequence_status_by_id)
    if repo:
        pipeline = [p for p in pipeline if p.get('repo') == repo]
    response['pipeline'] = pipeline
    return response


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
# All three actions are now ONE-CLICK — none opens a PR for Larry to hand-merge.
# Each write routes through the single committer that owns the file it touches,
# honoring the machine-owned-file single-committer invariant:
#
#   * `drop` and `snooze` mutate ONLY captures.json. They write the LOCAL file
#     directly through the captures committer primitives (_CAPTURE_INGEST_LOCK +
#     _atomic_write_captures — the SAME writer the ingest path uses, NOT a second
#     one); heal_missions_card_gc (the SOLE captures.json git committer) version-
#     controls the delta on its next tick. drop returns {applied, state:dropped};
#     snooze returns {applied, snoozed_until} (§ 4.2 / § 4.3).
#
#   * `promote` touches TWO machine-owned files owned by TWO committers, so it
#     splits the write: it QUEUES the new mission entry for the missions writer
#     (heal_orphan_autoregister drains <queue_dir>/<mission_id>.json into
#     missions.json — the exact +New mission mechanism, see _handle_new_mission)
#     AND flips the capture (promoted_to + state:promoted) locally through the
#     captures committer (committed by heal_missions_card_gc). Returns
#     {mission_id, status:'queued', applied}. The board reflects both on the next
#     poll after the two healers commit — the same post-merge latency the old PR
#     flow had, minus the PR. (Cross-file atomicity is best-effort: the mission
#     is queued first so a mid-write crash never loses it; a failed capture flip
#     rolls the queue file back.)
#
# This ends the throwaway-PR-per-action friction the old _open_registry_pr flow
# created (each PR was unroutable through Forge, so it sat open until Larry went
# to GitHub to squash-merge + delete the branch by hand).

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
    registry-PR mechanism shared by promote/drop). `files` is a list of (repo-relative path,
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


# Phase S (S7): a pause(=snooze)/drop on a card whose linked work is in-flight
# must NOT interrupt the run (spec § 3 S7). The action is recorded as a
# `pending_action` and applied by heal_missions_card_gc only after the work
# reaches a safe stop. `promote` is excluded — it creates new work rather than
# pausing the running work, so it never defers.
_IN_FLIGHT_PHASES = frozenset({'in_flight', 'awaiting_merge'})


def _spawned_work_in_flight(cap: dict[str, Any], supabase_client: Any) -> bool:
    """True iff the capture's spawned work is currently in-flight (spec § 3 S7).

    Uses the SAME chain_events phase-derive the parked lane surfaces
    (`derive_phase_for_task`) so the in-flight gate matches what the board shows —
    no parallel state machine. Conservative: a card with no spawned task_id, or
    whose work has emitted no events yet, is NOT in-flight (the action applies
    immediately) — only KNOWN-in-flight work defers.

    A detected terminal FAILURE is a safe stop, not in-flight (S4<->S7): failed
    work keeps its `session_start` but never merges, so `derive_phase_for_task`
    would report in_flight/awaiting_merge forever — without this short-circuit a
    pause/drop on a failed card would defer indefinitely and never apply. Reuses
    the SAME recognizer the S4 ring uses
    (`build_sequence_advancer.chain_event_says_failed`) so both sides agree on
    what "failed" means."""
    spawned = cap.get('spawned')
    if not isinstance(spawned, dict):
        return False
    task_id = spawned.get('task_id')
    if not (isinstance(task_id, str) and task_id):
        return False
    import build_sequence_advancer as bsa  # noqa: PLC0415
    if bsa.chain_event_says_failed(supabase_client, task_id):
        return False
    events = _fetch_events_for_task_ids(supabase_client, [task_id]).get(task_id) or []
    if not events:
        return False
    return derive_phase_for_task(events, None) in _IN_FLIGHT_PHASES


def _make_pending_action(
    action: str, args: dict[str, Any], now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The `pending_action` record stamped on a card when a pause/drop is deferred
    behind in-flight work (spec § 3 S7). Carries the action + its non-null args so
    heal_missions_card_gc replays the exact intent after the safe stop."""
    return {
        'action': action,
        'args': {k: v for k, v in args.items() if v is not None},
        'requested_at': _now_utc_iso(now),
    }


def _defer_response(pending: dict[str, Any]) -> dict[str, Any]:
    """The response for an action deferred behind in-flight work (spec § 3 S7):
    recorded, not applied — the healer applies it after a safe stop."""
    return {'applied': False, 'deferred': True, 'pending_action': pending}


def _handle_capture_snooze(
    *,
    capture_id: str,
    snoozed_until: Any,
    captures_path: Path,
    now: Optional[datetime] = None,
    in_flight_resolver: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> dict[str, Any]:
    """`snooze` — set or clear a capture's `snoozed_until` (§ 4.3). DIRECT write
    via the single captures.json committer (NOT PR-backed): `snoozed_until=None`
    clears the snooze; an ISO-8601 datetime defers resurfacing until it passes.
    Returns {applied: True, snoozed_until}. 404 if no such capture; 409 if it
    isn't parked; 400 on a malformed or non-future date.

    Phase S (S7): if ``in_flight_resolver`` reports the card's linked work
    in-flight, the snooze is RECORDED as a pending_action (not applied) and
    {applied: False, deferred: True, pending_action} is returned — the healer
    applies it once the work reaches a safe stop, never interrupting the run."""
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
        if in_flight_resolver is not None and in_flight_resolver(cap):
            pending = _make_pending_action(
                'snooze',
                {'snoozed_until': parsed.isoformat() if parsed else None},
                now,
            )
            cap['pending_action'] = pending
            registry['schema_version'] = CAPTURES_SCHEMA_VERSION
            _atomic_write_captures(captures_path, registry)
            return _defer_response(pending)
        cap['snoozed_until'] = parsed.isoformat() if parsed else None
        registry['schema_version'] = CAPTURES_SCHEMA_VERSION
        _atomic_write_captures(captures_path, registry)
    return {'applied': True, 'snoozed_until': cap['snoozed_until']}


def _handle_capture_promote(
    *,
    capture_id: str,
    overrides: dict[str, Any],
    captures_path: Path,
    projects_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """`promote` — MOVE a parked capture into the pipeline (projects-v3 P3, spec
    § 0 / § 4 decision 2). One-click, no PR. Promote is a move, not a record: it
    creates a NEW single-phase project at Brainstorm AND removes the capture from
    the funnel (parked) lane. Two single-committers, no dual-write to one file:
      1. APPEND the new project to projects.json on disk (heal_projects_store is
         the SOLE committer of that file — the dashboard is a non-committer).
      2. Flip the capture (`promoted_to` = project_id, `state: promoted`,
         `spawned: {kind: 'project', ...}`) on the LOCAL captures.json; the
         captures committer (heal_missions_card_gc) commits the delta. The parked
         lane already excludes non-parked captures, so the flip removes it from
         the funnel.
    Optional overrides: name / brief (→ phase Desired End State) / repo /
    north_star_ref (defaults inferred from the capture). Returns
    {project_id, phase_id, status: 'promoted', applied: True}.

    Reversible with no data loss: archiving the project (PROJECT_STATES) takes it
    out of the pipeline; both the capture record and the archived project persist.
    Idempotent: the _require_parked guard rejects a double-click (409), and
    _create_project_from_funnel collapses any retry onto the existing project."""
    now = now or datetime.now(timezone.utc)

    # _CAPTURE_INGEST_LOCK guards the captures.json read-modify-write against the
    # ingest/snooze/drop writers AND serializes concurrent promotes of the same
    # capture (the _require_parked guard is then the idempotency gate against a
    # double-click). _create_project_from_funnel nests _PROJECTS_INGEST_LOCK
    # underneath (CAPTURE→PROJECTS); no path takes them reversed → no inversion.
    with _CAPTURE_INGEST_LOCK:
        cap_registry = _read_captures_registry(captures_path)
        cap = _find_capture(cap_registry['captures'], capture_id)
        _require_parked(cap, capture_id)

        cap_origin = cap.get('origin') if isinstance(cap.get('origin'), dict) else {}
        title = (overrides.get('name') or cap.get('title') or '').strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    'error': 'invalid project title',
                    'detail': 'name (override or capture title) is empty',
                },
            )
        desired_end_state = (
            overrides.get('brief')
            or cap.get('note')
            or cap.get('title')
            or ''
        )
        repo = overrides.get('repo') or cap_origin.get('repo') or None
        north_star_ref = overrides.get('north_star_ref')

        # 1) Create the project on disk FIRST. If the capture flip then fails, a
        #    retry is idempotent: _create_project_from_funnel re-finds this active
        #    project by `promoted_from` and the still-parked capture is flipped.
        result = _create_project_from_funnel(
            projects_path=projects_path,
            title=title,
            desired_end_state=desired_end_state,
            repo=repo,
            north_star_ref=north_star_ref,
            promoted_from={'kind': 'capture', 'capture_id': capture_id},
            now=now,
        )
        project_id = result['project_id']

        # 2) Flip the capture on the local captures.json (its committer commits
        #    the delta), removing it from the parked/funnel lane.
        cap['promoted_to'] = project_id
        cap['state'] = 'promoted'
        # Phase S (S1): stamp the spawned ref — the join key to the project this
        # card created. The project's lifecycle surfaces via the pipeline lane.
        cap['spawned'] = {
            'kind': 'project',
            'project_id': project_id,
            'stamped_at': _now_utc_iso(now),
        }
        cap_registry['schema_version'] = CAPTURES_SCHEMA_VERSION
        try:
            _atomic_write_captures(captures_path, cap_registry)
        except OSError as e:
            first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={'error': 'captures write failed', 'detail': first_line},
            )

    return {
        'project_id': project_id,
        'phase_id': result['phase_id'],
        'status': 'promoted',
        'applied': True,
    }


def _handle_capture_drop(
    *,
    capture_id: str,
    reason: Any,
    captures_path: Path,
    now: Optional[datetime] = None,
    in_flight_resolver: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> dict[str, Any]:
    """`drop` — retire a capture (§ 4.2). One-click DIRECT write through the
    single captures.json committer (NOT PR-backed — mirrors `snooze`): sets
    `state: dropped` (+ optional `drop_reason`) on the LOCAL captures.json;
    heal_missions_card_gc (the SOLE captures.json git committer) version-controls
    the delta on its next tick and collapses the card to the dropped lane. Never
    a silent delete — that GC commit IS the audit record. Returns
    {applied: True, state: 'dropped'}. 404 if no such capture; 409 if it isn't
    parked; 400 on a non-string reason.

    Phase S (S7): if ``in_flight_resolver`` reports the card's linked work
    in-flight, the drop is RECORDED as a pending_action (not applied) and
    {applied: False, deferred: True, pending_action} is returned — the healer
    drops the card once the work reaches a safe stop, never interrupting the run."""
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='reason must be a string',
        )

    with _CAPTURE_INGEST_LOCK:
        registry = _read_captures_registry(captures_path)
        cap = _find_capture(registry['captures'], capture_id)
        _require_parked(cap, capture_id)

        if in_flight_resolver is not None and in_flight_resolver(cap):
            pending = _make_pending_action('drop', {'reason': reason}, now)
            cap['pending_action'] = pending
            registry['schema_version'] = CAPTURES_SCHEMA_VERSION
            _atomic_write_captures(captures_path, registry)
            return _defer_response(pending)

        cap['state'] = 'dropped'
        if reason:
            cap['drop_reason'] = reason
        registry['schema_version'] = CAPTURES_SCHEMA_VERSION
        _atomic_write_captures(captures_path, registry)

    return {'applied': True, 'state': 'dropped'}


def _handle_capture_action(
    *,
    capture_id: str,
    action: str,
    args: dict[str, Any],
    captures_path: Path,
    projects_path: Path,
    now: Optional[datetime] = None,
    in_flight_resolver: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> dict[str, Any]:
    """Dispatch POST /api/missions/captures/{id}/action to the per-action
    handler. All three are one-click (no PR): `promote` MOVES the capture into a
    new single-phase project at Brainstorm + flips the capture →
    {project_id, phase_id, status:'promoted', applied} (projects-v3 P3); `drop`
    and `snooze` are direct captures.json committer writes → {applied, state} /
    {applied, snoozed_until}. Unknown action → 400.

    Phase S (S7): ``in_flight_resolver`` is threaded into the pausing actions
    (`drop`/`snooze`) so one whose linked work is in-flight is recorded as a
    pending_action instead of applied. `promote` creates new work rather than
    pausing the running work, so it never defers."""
    if action == 'promote':
        return _handle_capture_promote(
            capture_id=capture_id,
            overrides={
                k: args[k] for k in ('name', 'brief', 'repo', 'north_star_ref')
                if k in args and args[k] is not None
            },
            captures_path=captures_path,
            projects_path=projects_path,
            now=now,
        )
    if action == 'drop':
        return _handle_capture_drop(
            capture_id=capture_id,
            reason=args.get('reason'),
            captures_path=captures_path,
            now=now,
            in_flight_resolver=in_flight_resolver,
        )
    if action == 'snooze':
        return _handle_capture_snooze(
            capture_id=capture_id,
            snoozed_until=args.get('snoozed_until'),
            captures_path=captures_path,
            now=now,
            in_flight_resolver=in_flight_resolver,
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f'invalid action={action!r}; expected promote|drop|snooze',
    )


# Missions v2 Phase 4 step 1b — the capture-scoped conversation thread + doorbell
# (spec: agents/beacon/specs/missions-v2-phase4-meaning-layer.md § 8 + § 9)
#
# The thread reuses the chain_events store keyed by the capture_id (a `card_message`
# event per turn) rather than a bespoke thread table (§ 3 reuse map: "generalize
# the CLARIFY rails"). GET reads the rows back oldest-first; POST emits one row for
# Larry's message AND drops a resume/notify envelope into Beacon's inbox so she
# answers on her next cycle (§ 8: "writes a resume/notify envelope into Beacon's
# inbox; Beacon answers on its next cycle"). A Larry message also resolves any
# pending blocked-on-you doorbell (§ 9) — his reply silences the ping immediately.

# direction values on a card_message payload. larry_to_team is the operator
# asking/answering on the card; team_to_larry is Beacon's reply (push-emitted by
# Beacon's runtime, NOT this endpoint — listed here as the read-side contract).
_CARD_MSG_LARRY = 'larry_to_team'
_CARD_MSG_TEAM = 'team_to_larry'


def _shape_thread_message(ev: dict[str, Any]) -> dict[str, Any]:
    """Project a `card_message` chain_event into a thread entry (§ 8). Fields are
    read defensively — a malformed row degrades to None per field rather than
    500-ing the read."""
    payload = _ev_payload(ev)
    direction = payload.get('direction')
    if direction not in (_CARD_MSG_LARRY, _CARD_MSG_TEAM):
        direction = None
    text = payload.get('text')
    if not isinstance(text, str):
        text = None
    actor = payload.get('actor')
    if not isinstance(actor, str):
        actor = ev.get('agent') if isinstance(ev.get('agent'), str) else None
    needs_reply = payload.get('needs_reply')
    if not isinstance(needs_reply, bool):
        needs_reply = None
    return {
        'id': ev.get('event_id'),
        'ts': ev.get('ts'),
        'direction': direction,
        'text': text,
        'actor': actor,
        'needs_reply': needs_reply,
    }


def _card_thread_messages(
    item_id: str, supabase_client: Any,
) -> list[dict[str, Any]]:
    """Read a card's `card_message` chain_events (keyed by ``item_id``) back
    oldest-first (§ 8). Store-agnostic: ``item_id`` is the capture_id for a
    parked capture or the mission_id for a mission-backed funnel card — the
    conversation lives in the SAME chain_events store keyed by that id, so one
    reader serves both. Degrades to [] when Supabase is unavailable."""
    by_task = _fetch_events_for_task_ids(supabase_client, [item_id])
    events = by_task.get(item_id) or []  # newest-first from the fetch
    messages = [
        _shape_thread_message(ev)
        for ev in events
        if (ev.get('event_type') or '') == 'card_message'
    ]
    messages.reverse()  # oldest-first for natural thread render
    return messages


def _newest_team_doorbell(
    events: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """The newest `team_to_larry` card_message in ``events`` (newest-first from
    the fetch) projected as a compact doorbell, or None when the card has no
    team reply yet. ``blocked`` reflects that message's ``needs_reply`` (Phase 4
    § 9 loud-vs-quiet doorbell) — True only when it's explicitly set, so an FYI
    reply (needs_reply false/absent) reads as a quiet dot, not a louder badge."""
    for ev in events:
        if (ev.get('event_type') or '') != 'card_message':
            continue
        msg = _shape_thread_message(ev)
        if msg['direction'] != _CARD_MSG_TEAM:
            continue
        return {
            'latest_team_id': msg['id'],
            'latest_team_ts': msg['ts'],
            'blocked': msg['needs_reply'] is True,
        }
    return None


def _project_card_doorbells(
    captures: list[dict[str, Any]], supabase_client: Any,
) -> None:
    """Project a compact per-card `doorbell` signal onto the captures list
    in place (Missions v2 Phase 4b.2 Contract E, spec § 4). For each PARKED
    card the kanban already polls, summarize the newest `team_to_larry`
    card_message — its stable `event_id`, `ts`, and whether it's `blocked`
    (needs_reply) — so a closed card gets a server-driven unread signal the
    badge (Contract F) keys on. The open-thread poll (Contract A) is untouched.

    The signal is derived from ONE bounded chain_events read batched across
    every parked card id (`_fetch_events_for_task_ids`' single `.in_` query —
    no per-card round-trip), grouped by card id. Fail-safe: any read error
    degrades every card to `doorbell: null` rather than 500ing the kanban,
    mirroring how `_reader_captures` degrades a missing file to an empty list.
    A card with no team reply gets `doorbell: null`."""
    parked = [
        c for c in captures
        if isinstance(c, dict) and c.get('state') == 'parked'
        and isinstance(c.get('id'), str)
    ]
    if not parked:
        return
    try:
        by_task = _fetch_events_for_task_ids(
            supabase_client, [c['id'] for c in parked])
    except Exception:  # noqa: BLE001 — a broken doorbell never breaks the board
        by_task = {}
    for cap in parked:
        cap['doorbell'] = _newest_team_doorbell(by_task.get(cap['id']) or [])


def _handle_capture_thread(
    *,
    capture_id: str,
    captures_path: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """GET /api/missions/captures/{id}/thread (§ 8). Returns the card's
    conversation, oldest-first. 404 if the capture doesn't exist. Degrades to an
    empty thread when Supabase is unavailable (no creds / test env) — same
    read-resilience contract as the derive."""
    now = now or datetime.now(timezone.utc)
    registry = _read_captures_registry(captures_path)
    _find_capture(registry.get('captures') or [], capture_id)

    return {
        'capture_id': capture_id,
        'messages': _card_thread_messages(capture_id, supabase_client),
        'last_synced_at': now.isoformat(),
    }


# Card-message kinds → the noun + thread URL a Beacon resume prompt reads back.
# Store-agnostic: a parked capture and a mission-backed funnel card share the same
# conversation mechanism, differing only in the id key + where the thread lives.
_CARD_KIND_CAPTURE = 'capture'
_CARD_KIND_MISSION = 'mission'
_CARD_KIND_PHASE = 'phase'
_CARD_KIND_META: dict[str, dict[str, str]] = {
    _CARD_KIND_CAPTURE: {
        'id_key': 'capture_id',
        'noun': 'parked card',
        'thread_url': '/api/missions/captures/{id}/thread',
    },
    _CARD_KIND_MISSION: {
        'id_key': 'mission_id',
        'noun': 'mission card',
        'thread_url': '/api/system/missions/{id}/thread',
    },
    # projects-v3 P6.1 — a Brainstorm phase card gets the SAME team-chat thread as
    # a parked/mission card. The join key is a COMPOSITE `project_id::phase_id`
    # ref (phase ids are title slugs, NOT globally unique, so phase-id alone would
    # cross-thread two projects' conversations and mis-route resolution). Beacon
    # answers via the generic envelope, no phase-specific responder. The thread_url
    # carries the composite ref in the single-placeholder template.
    _CARD_KIND_PHASE: {
        'id_key': 'phase_ref',
        'noun': 'brainstorm phase',
        'thread_url': '/api/projects/phases/{id}/thread',
    },
}


def _post_card_message(
    *,
    item_id: str,
    item_title: Optional[str],
    item_kind: str,
    text: str,
    actor: str,
    agents_root: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Shared core for "Larry posts on a card" (§ 8) — store-agnostic over
    captures vs missions. ``item_kind`` selects the id key + thread URL + noun so
    the same three effects serve both a parked capture and a mission-backed
    funnel card:
      1. Emit one `card_message` chain_event (direction=larry_to_team) keyed by
         ``item_id`` (the conversation join key), so GET .../thread reads it back.
      2. Drop a resume/notify envelope into Beacon's inbox so she answers next
         cycle (the team is the single voice — § 2 decision #5).
      3. Resolve any pending blocked-on-you doorbell for this card (§ 9).

    The CALLER does the find/404 (the backing store differs); this core assumes
    the item exists. 400 on empty text; 503 if Supabase is unavailable (the
    message must be durable, so we refuse rather than silently drop it)."""
    text = (text or '').strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='message text must be non-empty',
        )
    if supabase_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='supabase unavailable',
        )
    meta = _CARD_KIND_META[item_kind]
    id_key = meta['id_key']
    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()

    compute_event_id, sanitize_payload = _import_chain_event_helpers()
    event_id = compute_event_id(item_id, 'card_message', ts_iso, extra=actor)
    payload = {
        id_key: item_id,
        'direction': _CARD_MSG_LARRY,
        'text': text,
        'actor': actor,
        # Larry asked/answered → the team owes a reply on its next cycle.
        'needs_reply': True,
    }
    row: dict[str, Any] = {
        'event_id': event_id,
        'ts': ts_iso,
        'agent': actor,
        'event_type': 'card_message',
        'task_id': item_id,
        'actor': actor,
        'payload': sanitize_payload(payload),
    }
    supabase_client.table('chain_events').upsert(
        [row], on_conflict='event_id', ignore_duplicates=True,
    ).execute()

    # Resume/notify envelope → Beacon's inbox. Beacon answers on her next cycle
    # (§ 8). Filename keyed on the event_id so concurrent messages never collide.
    inbox = (agents_root / 'inboxes' / 'beacon').resolve()
    filename = f'card-message-{event_id}.json'
    envelope_candidate = (inbox / filename).resolve()
    if envelope_candidate.parent != inbox:
        # Defense-in-depth: item_id flows into the event_id (a hex digest),
        # not the filename, so this should be unreachable — but never write
        # outside Beacon's inbox.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='invalid envelope filename',
        )
    thread_url = meta['thread_url'].replace('{id}', item_id)
    envelope = {
        'task_id': f'card-message-{item_id}',
        'source': 'dashboard',
        'actor': actor,
        id_key: item_id,
        'dedup_identity': f'card-message:{event_id}',
        'timeout': 600,
        'prompt': (
            f'Larry posted a message on {meta["noun"]} `{item_id}` '
            f'("{item_title or item_id}"). Read the card thread via '
            f'GET {thread_url}, answer in your '
            'single-voice as the team, and post your reply as a '
            f'team_to_larry card_message event for the same {id_key}. '
            f'Larry\'s message: {text}'
        ),
    }
    _atomic_write_envelope(envelope_candidate, envelope)
    envelope_written = str(envelope_candidate)

    # § 9: a Larry reply clears the blocked-on-you doorbell for this card.
    doorbell_resolved = False
    try:
        import missions_doorbell  # noqa: PLC0415 — lazy; sibling module
        result = missions_doorbell.resolve_doorbell(
            capture_id=item_id, now=now,
        )
        doorbell_resolved = bool(result.get('resolved'))
    except Exception:  # noqa: BLE001 — doorbell resolve is best-effort
        logger.exception(
            'doorbell resolve failed for card %s (message still posted)',
            item_id,
        )

    return {
        'posted': True,
        'event_id': event_id,
        'direction': _CARD_MSG_LARRY,
        'envelope_written': envelope_written,
        'doorbell_resolved': doorbell_resolved,
    }


def _handle_capture_message(
    *,
    capture_id: str,
    text: str,
    actor: str,
    captures_path: Path,
    agents_root: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """POST /api/missions/captures/{id}/message (§ 8) — Larry posts on a parked
    card. Finds the capture (404 if absent) then defers to the store-agnostic
    `_post_card_message` core. 503 if Supabase is unavailable (the message must
    be durable, so we refuse rather than silently drop it)."""
    registry = _read_captures_registry(captures_path)
    cap = _find_capture(registry.get('captures') or [], capture_id)
    return _post_card_message(
        item_id=capture_id,
        item_title=cap.get('title'),
        item_kind=_CARD_KIND_CAPTURE,
        text=text,
        actor=actor,
        agents_root=agents_root,
        supabase_client=supabase_client,
        now=now,
    )


# Missions v2 — droplet delegate endpoint (POST /api/missions/captures/{id}/delegate)
# (spec: agents/beacon/specs/missions-v2-delegate-fix.md § 2)
#
# "Delegate to team" — the primary Parked-card action. Mirrors the capture-action
# route's auth + guards, but instead of mutating the capture it emits a
# human-approval-gate APPROVAL_REQUEST proposal into Beacon's inbox (reusing the
# #502 message-handler envelope shape + safe_write_inbox). The capture stays
# `parked`; the delegation lives as a Beacon proposal, not a capture state — so
# NO captures.json mutation here. trust_policy (the existing gate) decides whether
# the resulting dispatch auto-fires or asks Larry again; this endpoint just
# creates the proposal. A re-POST for a capture that already has an open delegate
# proposal collapses onto it (deterministic filename + existence check) rather
# than double-proposing.

# The default action when neither the body nor the capture names one (§ 2).
_DELEGATE_DEFAULT_ACTION = 'delegate'


def _stamp_spawned_on_capture(
    captures_path: Path, capture_id: str, spawned: dict[str, Any],
) -> None:
    """Phase S (S1): stamp the `spawned` ref onto a capture under the shared
    capture-ingest lock — the SAME single committer / writer path (_read +
    _atomic_write_captures) the ingest, snooze, drop, and promote flows use, so
    captures.json keeps exactly one writer. The capture's `state` is untouched
    (a delegated card stays parked). Idempotent: a re-stamp with the same
    spawned identity (task_id + kind) is a no-op — no rewrite — so a deduped
    re-POST never thrashes the file or churns `stamped_at`."""
    with _CAPTURE_INGEST_LOCK:
        registry = _read_captures_registry(captures_path)
        cap = next(
            (c for c in registry.get('captures') or []
             if isinstance(c, dict) and c.get('id') == capture_id),
            None,
        )
        if cap is None:
            return
        existing = cap.get('spawned')
        if (isinstance(existing, dict)
                and existing.get('task_id') == spawned.get('task_id')
                and existing.get('kind') == spawned.get('kind')):
            return
        cap['spawned'] = spawned
        registry['schema_version'] = CAPTURES_SCHEMA_VERSION
        _atomic_write_captures(captures_path, registry)


def _handle_capture_delegate(
    *,
    capture_id: str,
    action: Optional[str],
    actor: str,
    captures_path: Path,
) -> dict[str, Any]:
    """POST /api/missions/captures/{id}/delegate (§ 2) — hand a parked card to
    the team. Emits a `human-approval-gate` APPROVAL_REQUEST proposal for Beacon
    via safe_write_inbox.

    Phase S (S1): also stamps a `spawned` ref (the join key back to the work this
    card created — `delegate-<capture_id>`) onto the capture. The capture's
    `state` stays parked — the spawned ref is additive — written through the
    shared single-committer path (`_stamp_spawned_on_capture`).

    404 if the capture doesn't exist; 409 if it isn't parked; 400 on an
    unrecognized action. Idempotent: a re-POST that finds an already-open
    proposal in Beacon's inbox collapses onto it (no second proposal), and the
    spawned-ref stamp is idempotent too (no rewrite on an unchanged identity)."""
    import safe_write_inbox  # noqa: PLC0415 — lazy; sibling module (jail-guarded)

    registry = _read_captures_registry(captures_path)
    cap = _find_capture(registry.get('captures') or [], capture_id)  # 404
    _require_parked(cap, capture_id)  # 409

    # Resolve the action: explicit body wins, else the capture's recommendation,
    # else the delegate default (§ 2). Validate against the shared enum so a
    # garbage body surfaces as 400 rather than a malformed proposal.
    resolved = action or cap.get('recommended_action') or _DELEGATE_DEFAULT_ACTION
    if resolved not in _VALID_RECOMMENDED_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f'invalid action={resolved!r}; expected one of '
                f'{list(_VALID_RECOMMENDED_ACTIONS)}'
            ),
        )

    # Deterministic identity: task_id + filename key on the capture_id so a
    # re-POST collapses onto the same proposal (the dedup the spec asks for —
    # mirrors the message handler's stable dedup_identity).
    task_id = f'delegate-{capture_id}'
    spawned_ref = {
        'kind': 'delegate',
        'task_id': task_id,
        'stamped_at': _now_utc_iso(),
    }
    filename = f'{task_id}.json'
    safe_name = safe_write_inbox.canonical_inbox_name(filename)
    proposal_path = (safe_write_inbox.INBOXES_ROOT / 'beacon' / safe_name)
    if proposal_path.exists():
        # An open delegate proposal already sits in Beacon's inbox — collapse
        # onto it rather than double-proposing. Re-assert the spawned ref
        # (idempotent: a no-op when already stamped) so a card whose proposal
        # predates Phase S still gets linked.
        _stamp_spawned_on_capture(captures_path, capture_id, spawned_ref)
        return {'dispatched': True, 'deduped': True}

    title = cap.get('title') or capture_id
    meaning = _meaning_layer_fields(cap)
    briefing = meaning['briefing'] or {}
    suggest = briefing.get('suggest')
    summary = (suggest or title or capture_id)

    # The proposal carries the APPROVAL_REQUEST required fields
    # (beacon_approval_handler.REQUIRED_FIELDS: task_id, summary, target_agent,
    # prompt) plus the capture identity, actor, a stable dedup identity, and a
    # timeout — the same envelope shape as the #502 message handler.
    prompt = (
        f'Larry clicked "Delegate to team" on parked card `{capture_id}` '
        f'("{title}"). His click IS the go — scope and propose/run this down '
        'as the team. '
        f'Recommended action: {resolved}. '
        f'Briefing — what: {briefing.get("what") or "(none)"}; '
        f'why: {briefing.get("why") or "(none)"}; '
        f'suggested next step: {suggest or "(none)"}. '
        'Treat this as a human-approval-gate proposal: whether the resulting '
        'dispatch auto-fires or asks again is governed by trust_policy. Do NOT '
        'mutate the capture — it stays parked; the delegation lives as this '
        'proposal.'
    )
    envelope = {
        'task_id': task_id,
        'target_agent': 'beacon',
        'summary': summary,
        'prompt': prompt,
        'source': 'dashboard',
        'actor': actor,
        'capture_id': capture_id,
        'action': resolved,
        'dedup_identity': f'delegate:{capture_id}',
        'timeout': 600,
    }
    safe_write_inbox.safe_write_inbox(
        target_agent='beacon',
        task_dict=envelope,
        source_agent='dashboard',
        filename=filename,
    )
    # Phase S (S1): stamp the spawned ref AFTER the proposal exists, so a crash
    # before the proposal write leaves no dangling link — and the capture stays
    # parked (the stamp is additive, not a state transition).
    _stamp_spawned_on_capture(captures_path, capture_id, spawned_ref)
    return {'dispatched': True}


# Missions v2 Phase 3 — mission write-back (POST /api/system/missions/{id}/action)
# (spec: agents/beacon/specs/missions-v2-phase3-writeback-autoregister.md § 5)
#
# defer / resume / reprioritize are ALL PR-backed (missions.json is the curated
# registry — every change auditable). Each is a single-field edit via the shared
# _open_registry_pr helper (the generalized registry-PR mechanism), so the
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
    projects_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """`accept` — MOVE a proposed funnel mission into the pipeline (projects-v3
    P3, spec § 4 decision 2: Proposed-lane Accept is UNIFIED onto Promote — the
    same gesture, one code path). It creates a NEW single-phase project at
    Brainstorm carrying `promoted_from: {kind: mission, mission_id}`; the mission
    is NOT mutated (no missions.json PR). The funnel derive (_build_funnel)
    suppresses a proposed mission whose id matches an ACTIVE project's
    `promoted_from`, so the card leaves the funnel lane. 404 if no such mission;
    409 if not proposed (only a funnel-card thread is acceptable). Returns
    {project_id, phase_id, status: 'promoted', applied}.

    Reversible with no data loss: archiving the project (PROJECT_STATES)
    un-suppresses the mission → it returns to the funnel; the mission record was
    never touched. Idempotent: a re-accept finds the existing active project via
    `promoted_from` and returns it (applied=False) instead of a duplicate — that
    is why the mission stays `proposed` (the cross-ref, not a phase flip, is the
    'accepted' signal)."""
    now = now or datetime.now(timezone.utc)

    # Read-only on missions.json (no mutation → no lock needed; the read is a
    # whole-file atomic read against the missions writer's atomic replace).
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

    title = (mission.get('name') or mission_id or '').strip() or mission_id
    desired_end_state = mission.get('brief') or mission.get('name') or ''
    repo = mission.get('repo') or None

    result = _create_project_from_funnel(
        projects_path=projects_path,
        title=title,
        desired_end_state=desired_end_state,
        repo=repo,
        north_star_ref=None,
        promoted_from={'kind': 'mission', 'mission_id': mission_id},
        now=now,
    )
    return {
        'project_id': result['project_id'],
        'phase_id': result['phase_id'],
        'status': 'promoted',
        'applied': result['applied'],
    }


def _handle_orphan_promote(
    *,
    task_id: str,
    overrides: dict[str, Any],
    projects_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """`orphan` — MOVE a raw orphan funnel card (a chain_events task_id with no
    registered mission) into the pipeline (projects-v3 P3 follow-up,
    p3f-reversibility-and-orphan, spec § 6 step 3). A raw orphan has no
    capture/mission registry row, so it is identified by its task_id alone: the
    new project records `promoted_from={'kind':'orphan','task_id':task_id}`, and
    the funnel derive (`_promoted_orphan_task_ids`) suppresses an orphan already
    MOVED into an ACTIVE project. Nothing outside projects.json is touched (no
    missions/captures PR — orphans have no source registry to flip).

    Reversible with no data loss: archiving the project un-suppresses the orphan
    → it returns to the funnel (the orphan is re-derived from chain_events, never
    mutated). Idempotent: a re-promote finds the existing active project via
    `promoted_from` and returns it (applied=False) instead of a duplicate.
    Returns {project_id, phase_id, status: 'promoted', applied}."""
    now = now or datetime.now(timezone.utc)

    task_id = (task_id or '').strip()
    if not task_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                'error': 'orphan task_id missing',
                'hint': "kind='orphan' requires a non-empty task_id ref",
            },
        )

    title = (overrides.get('name') or _humanize_task_id(task_id) or task_id).strip()
    desired_end_state = overrides.get('brief') or ''
    repo = overrides.get('repo') or None

    result = _create_project_from_funnel(
        projects_path=projects_path,
        title=title,
        desired_end_state=desired_end_state,
        repo=repo,
        north_star_ref=overrides.get('north_star_ref'),
        promoted_from={'kind': 'orphan', 'task_id': task_id},
        now=now,
    )
    return {
        'project_id': result['project_id'],
        'phase_id': result['phase_id'],
        'status': 'promoted',
        'applied': result['applied'],
    }


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


def _handle_mission_snooze(
    *,
    mission_id: str,
    snoozed_until: Any,
    missions_path: Path,
) -> dict[str, Any]:
    """`snooze` — defer a proposed mission-backed funnel card until a future
    instant (Contract B). Sets the additive `snoozed_until` (ISO-8601, or null to
    clear) on the registry entry; `phase` STAYS `proposed`. PR-backed (the SAME
    curated missions.json single-committer the other mission write-backs use — no
    local tree write, so the no-dirty-tree invariant holds). 404 if no such
    mission; 409 if not proposed (only a funnel-card thread is snoozable); 400 on
    a malformed or non-future date. Returns {pr_url, branch}.

    The funnel's snooze filter (`_build_funnel`) hides a mission whose
    `snoozed_until` is still in the future, mirroring the parked-capture lane —
    so the card resurfaces only once the snooze elapses."""
    parsed: Optional[datetime] = None
    if snoozed_until is not None:
        parsed = _parse_iso_utc(snoozed_until)
        if parsed is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='snoozed_until must be an ISO-8601 datetime or null',
            )
        if parsed <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='snoozed_until must be in the future',
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
        if mission.get('phase') != 'proposed':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'mission not proposed',
                    'mission_id': mission_id,
                    'phase': mission.get('phase'),
                    'hint': 'only a proposed mission can be snoozed',
                },
            )

        iso = parsed.isoformat() if parsed else None
        mission['snoozed_until'] = iso
        until_label = iso or 'cleared'

        branch = f'chore/snooze-mission-{mission_id}'
        title = f'chore(missions): snooze proposed mission {mission_id}'
        pr_body = '\n'.join([
            f'Snooze proposed mission `{mission_id}` '
            f'(`snoozed_until: {until_label}`).',
            '',
            'Single-field additive registry edit (`phase` stays `proposed`). The '
            'funnel hides a mission whose `snoozed_until` is still in the future, '
            'mirroring the parked-capture lane, so the card resurfaces only once '
            'the snooze elapses (Projects v3 P2 Contract B).',
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


def _handle_mission_delegate(
    *,
    mission_id: str,
    action: Optional[str],
    actor: str,
    missions_path: Path,
) -> dict[str, Any]:
    """POST /api/system/missions/{id}/delegate (Contract B) — hand a proposed
    mission-backed funnel card to the team, mirroring the parked-capture Delegate.
    Emits a `human-approval-gate` APPROVAL_REQUEST proposal for Beacon via
    safe_write_inbox; it does NOT mutate missions.json (the registry is PR-backed
    and the delegation lives as a Beacon proposal, not a mission-state edit — so
    the no-dirty-tree invariant holds, same as the capture Delegate leaves the
    capture parked).

    404 if no such mission; 409 if not proposed; 400 on an unrecognized action.
    Idempotent: a re-POST that finds an already-open proposal in Beacon's inbox
    collapses onto it (deterministic filename `delegate-{mission_id}.json`)."""
    import safe_write_inbox  # noqa: PLC0415 — lazy; sibling module (jail-guarded)

    registry = _read_missions_registry(missions_path)
    mission = _find_mission(registry['missions'], mission_id)  # 404
    if mission.get('phase') != 'proposed':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                'error': 'mission not proposed',
                'mission_id': mission_id,
                'phase': mission.get('phase'),
                'hint': 'only a proposed mission can be delegated',
            },
        )

    resolved = action or _DELEGATE_DEFAULT_ACTION
    if resolved not in _VALID_RECOMMENDED_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f'invalid action={resolved!r}; expected one of '
                f'{list(_VALID_RECOMMENDED_ACTIONS)}'
            ),
        )

    task_id = f'delegate-{mission_id}'
    filename = f'{task_id}.json'
    safe_name = safe_write_inbox.canonical_inbox_name(filename)
    proposal_path = (safe_write_inbox.INBOXES_ROOT / 'beacon' / safe_name)
    if proposal_path.exists():
        # An open delegate proposal already sits in Beacon's inbox — collapse
        # onto it rather than double-proposing.
        return {'dispatched': True, 'deduped': True}

    name = mission.get('name') or mission_id
    brief = mission.get('brief') or ''
    summary = brief or name

    prompt = (
        f'Larry clicked "Delegate to team" on proposed mission card '
        f'`{mission_id}` ("{name}"). His click IS the go — scope and propose/run '
        'this down as the team. '
        f'Recommended action: {resolved}. '
        f'Brief: {brief or "(none)"}. '
        'Treat this as a human-approval-gate proposal: whether the resulting '
        'dispatch auto-fires or asks again is governed by trust_policy. Do NOT '
        'mutate the mission registry — the proposed thread stays as-is; the '
        'delegation lives as this proposal.'
    )
    envelope = {
        'task_id': task_id,
        'target_agent': 'beacon',
        'summary': summary,
        'prompt': prompt,
        'source': 'dashboard',
        'actor': actor,
        'mission_id': mission_id,
        'action': resolved,
        'dedup_identity': f'delegate:{mission_id}',
        'timeout': 600,
    }
    safe_write_inbox.safe_write_inbox(
        target_agent='beacon',
        task_dict=envelope,
        source_agent='dashboard',
        filename=filename,
    )
    return {'dispatched': True}


def _handle_mission_thread(
    *,
    mission_id: str,
    missions_path: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """GET /api/system/missions/{id}/thread (Contract B) — the mission card's
    conversation, oldest-first. Mirrors the capture thread: 404 if the mission
    doesn't exist, degrades to an empty thread when Supabase is unavailable."""
    now = now or datetime.now(timezone.utc)
    registry = _read_missions_registry(missions_path)
    _find_mission(registry['missions'], mission_id)
    return {
        'mission_id': mission_id,
        'messages': _card_thread_messages(mission_id, supabase_client),
        'last_synced_at': now.isoformat(),
    }


def _handle_mission_message(
    *,
    mission_id: str,
    text: str,
    actor: str,
    missions_path: Path,
    agents_root: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """POST /api/system/missions/{id}/message (Contract B) — Larry posts on a
    mission card. Finds the mission (404 if absent) then defers to the
    store-agnostic `_post_card_message` core (same three effects as the capture
    message)."""
    registry = _read_missions_registry(missions_path)
    mission = _find_mission(registry['missions'], mission_id)
    return _post_card_message(
        item_id=mission_id,
        item_title=mission.get('name'),
        item_kind=_CARD_KIND_MISSION,
        text=text,
        actor=actor,
        agents_root=agents_root,
        supabase_client=supabase_client,
        now=now,
    )


def _handle_mission_action(
    *,
    mission_id: str,
    action: str,
    args: dict[str, Any],
    missions_path: Path,
    projects_path: Path,
    actor: str = '',
) -> dict[str, Any]:
    """Dispatch POST /api/system/missions/{id}/action to the per-action handler.
    defer / resume / reprioritize / dismiss / drop / snooze are PR-backed →
    {pr_url, branch}. `accept` is UNIFIED onto Promote (projects-v3 P3): it
    MOVES the proposed mission into a new project at Brainstorm → {project_id,
    phase_id, status, applied} (no missions.json PR). `drop` is the funnel-facing
    verb for the dismiss semantics (acknowledged=true, phase stays proposed —
    keeps the autoregister healer from re-proposing); `dismiss` is kept as its
    alias. `confirm_shipped` is the one-click confirm of a "looks shipped"
    off-board mission (on-disk delta, NOT PR-backed) → {applied, status}.
    Unknown action → 400."""
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
            projects_path=projects_path,
        )
    # `drop` is the funnel-facing verb (Contract B) for the dismiss semantics —
    # it supersedes bare `dismiss` while preserving them byte-for-byte
    # (acknowledged=true, phase stays proposed → stop re-proposing). `dismiss` is
    # kept as an alias so existing clients/tests keep working.
    if action in ('dismiss', 'drop'):
        return _handle_mission_dismiss(
            mission_id=mission_id,
            missions_path=missions_path,
        )
    if action == 'snooze':
        return _handle_mission_snooze(
            mission_id=mission_id,
            snoozed_until=args.get('snoozed_until'),
            missions_path=missions_path,
        )
    if action == 'confirm_shipped':
        return _handle_mission_confirm_shipped(
            mission_id=mission_id,
            missions_path=missions_path,
            actor=actor,
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f'invalid action={action!r}; expected '
            'defer|resume|reprioritize|accept|dismiss|drop|snooze|confirm_shipped'
        ),
    )


def _handle_mission_confirm_shipped(
    *,
    mission_id: str,
    missions_path: Path,
    actor: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """`confirm_shipped` — Larry's one-click confirm of a "looks shipped" off-board
    mission surfaced in the Where-are-we needs-you lane by
    heal_merged_pr_board_reconcile.

    One-click DIRECT write through the single missions.json committer (NOT
    PR-backed — mirrors capture `drop`): flips phase → 'shipped' with the
    auto-shipper's CORE audit stamp (shipped_at / shipped_by / prior_phase, the
    same three fields heal_missions_card_gc writes) plus a `shipped_note`
    recording that this was a human confirm, then resolves the for-Larry signal
    so the needs-you row clears immediately.
    heal_missions_card_gc (the SOLE missions.json git committer) version-controls
    the on-disk delta on its next tick — we never git-commit here.

    409 if already shipped (double-click guard); 404 if no such mission."""
    now = now or datetime.now(timezone.utc)
    with _NEW_MISSION_LOCK:
        registry = _read_missions_registry(missions_path)
        mission = _find_mission(registry['missions'], mission_id)
        prior = mission.get('phase')
        if prior == 'shipped':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={'error': 'mission already shipped',
                        'mission_id': mission_id},
            )
        mission['phase'] = 'shipped'
        mission['shipped_at'] = now.isoformat()
        mission['shipped_by'] = actor or 'dashboard:confirm_shipped'
        mission['prior_phase'] = prior
        mission['shipped_note'] = (
            'confirmed shipped from the Where-are-we needs-you lane '
            '(off-board merged-PR backstop)'
        )
        _atomic_write_json(missions_path, registry)

    # Clear the surfaced needs-you row immediately (idempotent with the healer's
    # own sync_prefix self-clear on its next tick). Fail-soft: a signal-resolve
    # hiccup must never fail the phase flip that already applied on disk.
    try:
        import for_larry_signal  # noqa: PLC0415 — sibling module, lazy import
        import heal_merged_pr_board_reconcile as _reconcile  # noqa: PLC0415
        for_larry_signal.resolve_record(_reconcile.SIGNAL_PREFIX + mission_id)
    except Exception as exc:  # noqa: BLE001 — never undo an applied flip
        logger.warning(
            'confirm_shipped: signal resolve failed for %s '
            '(phase flip applied): %s', mission_id, exc)

    return {'applied': True, 'status': 'shipped'}


def _handle_funnel_promote(
    *,
    ref: str,
    kind: Optional[str],
    overrides: dict[str, Any],
    captures_path: Path,
    missions_path: Path,
    projects_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The ONE unified Promote gesture (projects-v3 P3, spec § 0 / § 4 decision
    2). MOVES a funnel item — a parked capture OR a proposed mission — into a new
    single-phase project at Brainstorm, removing it from its funnel lane. Both
    lanes route through the SAME project-create core so there is no divergent
    path: a capture delegates to `_handle_capture_promote` (flip the capture), a
    mission to `_handle_mission_accept` (project `promoted_from` cross-ref
    suppresses it).

    `kind` is optional; when absent the item is auto-resolved — captures first,
    then proposed missions. A `raw orphan` card (a task_id in chain_events with
    no registered mission) carries no capture/mission registry row to auto-resolve
    against, so its promote MUST pass `kind='orphan'` explicitly; `ref` is then the
    orphan's task_id and the project records
    `promoted_from={'kind':'orphan','task_id':ref}`. An unresolvable `ref` → 404.
    Returns {project_id, phase_id, status, applied, source_kind}."""
    now = now or datetime.now(timezone.utc)

    resolved = kind if kind in ('capture', 'mission', 'orphan') else None
    if resolved is None and kind is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f'invalid kind={kind!r}; expected capture|mission|orphan or omit it'
            ),
        )
    if resolved is None:
        cap_registry = _read_captures_registry(captures_path)
        if any(isinstance(c, dict) and c.get('id') == ref
               for c in cap_registry['captures']):
            resolved = 'capture'
        else:
            missions_registry = _read_missions_registry(missions_path)
            if any(isinstance(m, dict) and m.get('id') == ref
                   for m in missions_registry['missions']):
                resolved = 'mission'

    if resolved == 'capture':
        result = _handle_capture_promote(
            capture_id=ref,
            overrides=overrides,
            captures_path=captures_path,
            projects_path=projects_path,
            now=now,
        )
        result['source_kind'] = 'capture'
        return result
    if resolved == 'mission':
        result = _handle_mission_accept(
            mission_id=ref,
            missions_path=missions_path,
            projects_path=projects_path,
            now=now,
        )
        result['source_kind'] = 'mission'
        return result
    if resolved == 'orphan':
        result = _handle_orphan_promote(
            task_id=ref,
            overrides=overrides,
            projects_path=projects_path,
            now=now,
        )
        result['source_kind'] = 'orphan'
        return result

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            'error': 'funnel item not found',
            'ref': ref,
            'hint': (
                'ref must be a parked capture id, a proposed mission id, or a '
                "raw orphan task_id with kind='orphan'"
            ),
        },
    )


def _find_active_phase(
    projects: list[Any], project_id: str, phase_id: str,
) -> Optional[dict[str, Any]]:
    """The phase dict `phase_id` inside ACTIVE project `project_id`, or None.
    Only active projects count — an archived (dropped-back) project is not
    launchable (spec § 5 reversibility: archiving leaves the pipeline)."""
    for proj in projects:
        if not isinstance(proj, dict) or proj.get('id') != project_id:
            continue
        if proj.get('state', projects_store.DEFAULT_PROJECT_STATE) != 'active':
            return None
        phases = proj.get('phases')
        if not isinstance(phases, list):
            return None
        for phase in phases:
            if isinstance(phase, dict) and phase.get('id') == phase_id:
                return phase
        return None
    return None


_PHASE_REF_SEP = '::'


def _split_phase_ref(phase_ref: str) -> tuple[Optional[str], str]:
    """Parse a P6.1 card-chat ref into ``(project_id, phase_id)``. The canonical
    form is the composite ``project_id::phase_id`` (phase ids are title slugs, not
    globally unique, so the project scopes resolution AND the conversation join
    key). A ref without the separator degrades to ``(None, phase_ref)`` — a
    best-effort scan fallback — so a hand-crafted phase-only ref still resolves."""
    if _PHASE_REF_SEP in phase_ref:
        project_id, phase_id = phase_ref.split(_PHASE_REF_SEP, 1)
        return (project_id or None, phase_id)
    return (None, phase_ref)


def _resolve_phase_for_ref(
    projects: list[Any], phase_ref: str,
) -> Optional[dict[str, Any]]:
    """Resolve the phase a P6.1 card-chat ``phase_ref`` names, or None. A
    composite ``project_id::phase_id`` resolves PRECISELY (`_find_active_phase`);
    a bare phase id falls back to the first active match by id. Only active
    projects count (an archived project has left the pipeline)."""
    project_id, phase_id = _split_phase_ref(phase_ref)
    if project_id is not None:
        return _find_active_phase(projects, project_id, phase_id)
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        if proj.get('state', projects_store.DEFAULT_PROJECT_STATE) != 'active':
            continue
        phases = proj.get('phases')
        if not isinstance(phases, list):
            continue
        for phase in phases:
            if isinstance(phase, dict) and phase.get('id') == phase_id:
                return phase
    return None


def _handle_launch_build(
    *,
    project_id: str,
    phase_id: str,
    actor: str,
    projects_path: Path,
    queue_dir: Path,
    models_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/projects/launch (projects-v3 P3,
    p3-launch-queue-drain). Queues a build-launch request for the Beacon-side
    drainer WITHOUT opening a PR, committing the repo, or touching the projects
    store. Steps:

      1. Read projects.json (read-only) and locate the phase. 404 if the active
         project / phase doesn't exist.
      2. 409 if the phase isn't spec-ready (no `spec_ref` — the drain authors
         the build sequence from the spec) or has already been launched
         (`sequence_ref` set, or lifecycle already building/done).
      3. Acquire the in-process lock, 409 if the same phase is already queued
         (a rapid double-click whose first request hasn't drained yet).
      4. Atomically drop `<queue_dir>/<phase_id>.json` (the launch request the
         drain authors from) and return {phase_id, project_id, status:'queued',
         seq_id}.

    The dashboard is NOT a committer — to the repo OR to projects.json. The
    request is keyed on the phase id; the drain's deterministic
    `launch-<phase_id>` sequence-file existence check is the durable
    idempotency backstop that makes a re-launch (after this queue file drained
    and was removed) a no-op rather than a second build dispatch. This honors
    the single-committer invariant (projects.json's sole committer is
    `heal_projects_store.py`) and the non-committer dispatch discipline (the
    `+New mission` precedent)."""
    now = now or datetime.now(timezone.utc)
    models_path = models_path or _agent_models_json_path()
    seq_id = f'launch-{phase_id}'

    projects = _reader_projects(projects_path)
    phase = _find_active_phase(projects, project_id, phase_id)
    if phase is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                'error': 'phase not found',
                'project_id': project_id,
                'phase_id': phase_id,
                'hint': (
                    'phase_id must name a phase inside an ACTIVE project_id in '
                    'the projects store'
                ),
            },
        )

    spec_ref = phase.get('spec_ref')
    if not isinstance(spec_ref, str) or not spec_ref.strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                'error': 'phase not spec-ready',
                'phase_id': phase_id,
                'hint': (
                    'Launch build requires a phase with a spec_ref; author + '
                    'attach the spec before launching.'
                ),
            },
        )

    lifecycle = phase.get('lifecycle_state', projects_store.DEFAULT_LIFECYCLE_STATE)
    if phase.get('sequence_ref') or lifecycle in ('building', 'done'):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                'error': 'phase already launched',
                'phase_id': phase_id,
                'lifecycle_state': lifecycle,
                'sequence_ref': phase.get('sequence_ref'),
            },
        )

    # Gate the build repo: the phase/project repo must be a buildable repo, else
    # reject LOUDLY. This is the gate that stops a bad repo (e.g. `ol-work`
    # inherited from a capture origin, dropped to None at promote) from riding to
    # an unbuildable dispatch that silently hangs for hours. We do NOT derive the
    # repo from the spec — every spec lives in agent-core regardless of the build
    # target, so spec location would mis-route every dashboard/graph build to
    # agent-core. The phase/project repo is the only reliable signal; when it's
    # missing/bogus the right answer is to ask, not guess.
    candidate_repo = phase.get('repo') or _project_repo(projects, project_id)
    valid_repos = _valid_repo_names(models_path)
    # Fail OPEN on an empty set (config unreadable) — never block a launch over a
    # transient config read miss.
    if valid_repos and (
        not isinstance(candidate_repo, str) or candidate_repo not in valid_repos
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                'error': 'unbuildable target repo',
                'phase_id': phase_id,
                'target_repo': candidate_repo,
                'valid_repos': sorted(valid_repos),
                'hint': (
                    "this phase has no buildable target repo (not in "
                    "config/agent-models.json repo_paths). Set the project's "
                    "repo to the repo the build should target, then re-launch."
                ),
            },
        )

    request_entry: dict[str, Any] = {
        'phase_id': phase_id,
        'project_id': project_id,
        'seq_id': seq_id,
        'spec_ref': spec_ref.strip(),
        'phase_title': phase.get('title') or phase_id,
        'desired_end_state': phase.get('desired_end_state', '') or '',
        'repo': candidate_repo,
        'requested_at': now.isoformat(),
        'requested_by': actor,
    }

    with _LAUNCH_QUEUE_LOCK:
        queue_path = queue_dir / f'{phase_id}.json'
        if queue_path.exists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'phase launch queued',
                    'phase_id': phase_id,
                    'hint': (
                        'A launch for this phase is already queued; the drain '
                        'will dispatch the build shortly.'
                    ),
                },
            )
        try:
            _atomic_write_json(queue_path, request_entry)
        except OSError as e:
            first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={'error': 'queue write failed', 'detail': first_line},
            )

    return {
        'phase_id': phase_id,
        'project_id': project_id,
        'status': 'queued',
        'seq_id': seq_id,
    }


def _project_repo(projects: list[Any], project_id: str) -> Optional[str]:
    """The `repo` of project `project_id` (a phase inherits its project's repo
    when it has none of its own), or None."""
    for proj in projects:
        if isinstance(proj, dict) and proj.get('id') == project_id:
            repo = proj.get('repo')
            return repo if isinstance(repo, str) and repo else None
    return None


# ---------------------------------------------------------------------------
# Phase transitions — the checkpoint-advance + spec-attach write side
# (projects-v3 P3 follow-up, step p3f-phase-transitions, spec § 0 / § 6 step 1).
#
# Both endpoints MUTATE projects.json on disk and stay NON-committers: they
# atomically rewrite the registry under `_PROJECTS_INGEST_LOCK` (so a concurrent
# reader / the healer never sees a partial file) and rely on `heal_projects_
# store.py` (the SOLE committer) to version-control the delta on its next tick —
# the same single-committer invariant Promote uses (spec § 5; #571). They read
# via `_read_projects_registry` (which 500s on a corrupt file) so a transition
# never appends onto malformed JSON, and locate the phase with `_find_active_
# phase` (archived projects are not transition-able — they've left the pipeline).
# ---------------------------------------------------------------------------
def _resolve_spec_ref(spec_ref: str) -> Optional[Path]:
    """Resolve a repo-relative `spec_ref` to an absolute path INSIDE the repo,
    or None if it escapes the repo root (path traversal / an absolute path).
    The attach-spec endpoint additionally requires the resolved path to be an
    existing file; this helper only does the safe-resolution half so the route
    can distinguish "unsafe ref" from "missing file" uniformly (both reject)."""
    root = _repo_root().resolve()
    candidate = (root / spec_ref).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _handle_edit_brainstorm(
    *,
    project_id: str,
    phase_id: str,
    draft: Optional[str],
    decisions: Optional[list[str]],
    projects_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/projects/brainstorm (projects-v3 P6.1). Larry
    edits the pre-filled Brainstorm card: persist the edited `draft` and/or
    `decisions` onto the phase, then return the flat card projection.

      1. 400 if neither `draft` nor `decisions` is provided.
      2. 404 if the active project / phase doesn't exist.
      3. 409 if the phase isn't at Brainstorm (the card — and so editing — only
         exists at the Brainstorm stage).
      4. `projects_store.edit_phase_brainstorm` applies the edit (stamps it
         Larry-authored so the Narrator never re-authors over it), atomic-write.

    Non-committer: the edit lands on disk under `_PROJECTS_INGEST_LOCK`;
    `heal_projects_store.py` commits the delta (the promote/advance precedent).
    Idempotent: a re-save of identical content writes nothing (`applied=False`)."""
    if draft is None and decisions is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='provide at least one of draft, decisions',
        )
    now = now or datetime.now(timezone.utc)
    with _PROJECTS_INGEST_LOCK:
        registry = _read_projects_registry(projects_path)
        phase = _find_active_phase(registry['projects'], project_id, phase_id)
        if phase is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    'error': 'phase not found',
                    'project_id': project_id,
                    'phase_id': phase_id,
                    'hint': (
                        'phase_id must name a phase inside an ACTIVE project_id '
                        'in the projects store'
                    ),
                },
            )
        state = phase.get('lifecycle_state', projects_store.DEFAULT_LIFECYCLE_STATE)
        if state != 'brainstorm':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'phase not at brainstorm',
                    'phase_id': phase_id,
                    'lifecycle_state': state,
                    'hint': 'the brainstorm draft is editable only at the Brainstorm stage',
                },
            )
        applied = projects_store.edit_phase_brainstorm(
            phase, draft=draft, decisions=decisions, now=now)
        if applied:
            try:
                _atomic_write_json(projects_path, registry)
            except OSError as e:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f'could not persist edit: {e}',
                )
        card = projects_store._phase_card(phase)
    return {
        'project_id': project_id,
        'phase_id': phase_id,
        'applied': applied,
        'status': 'edited',
        'draft': card.get('draft'),
        'decisions': card.get('decisions'),
        'spec_target_path': card.get('spec_target_path'),
    }


def _handle_phase_thread(
    *,
    phase_ref: str,
    projects_path: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """GET /api/projects/phases/{phase_ref}/thread (projects-v3 P6.1) — the
    Brainstorm phase card's conversation, oldest-first. ``phase_ref`` is the
    composite ``project_id::phase_id``. 404 if it names no active phase. Degrades
    to an empty thread when Supabase is unavailable (the derive read-resilience
    contract). Mirrors `_handle_capture_thread`. The conversation keys on the
    composite ref so two projects' threads never merge."""
    now = now or datetime.now(timezone.utc)
    registry = _read_projects_registry(projects_path)
    phase = _resolve_phase_for_ref(registry['projects'], phase_ref)
    if phase is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={'error': 'phase not found', 'phase_ref': phase_ref},
        )
    return {
        'phase_ref': phase_ref,
        'messages': _card_thread_messages(phase_ref, supabase_client),
        'last_synced_at': now.isoformat(),
    }


def _handle_phase_message(
    *,
    phase_ref: str,
    text: str,
    actor: str,
    projects_path: Path,
    agents_root: Path,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """POST /api/projects/phases/{phase_ref}/message (projects-v3 P6.1) — Larry
    posts on a Brainstorm phase card. ``phase_ref`` is the composite
    ``project_id::phase_id``. Finds the active phase (404 if absent) then defers
    to the store-agnostic `_post_card_message` core with the `phase` kind, so
    Beacon answers next cycle exactly as she does for a parked card. 503 if
    Supabase is unavailable (the message must be durable)."""
    registry = _read_projects_registry(projects_path)
    phase = _resolve_phase_for_ref(registry['projects'], phase_ref)
    if phase is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={'error': 'phase not found', 'phase_ref': phase_ref},
        )
    return _post_card_message(
        item_id=phase_ref,
        item_title=phase.get('title'),
        item_kind=_CARD_KIND_PHASE,
        text=text,
        actor=actor,
        agents_root=agents_root,
        supabase_client=supabase_client,
        now=now,
    )


def _handle_phase_advance(
    *,
    project_id: str,
    phase_id: str,
    projects_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/projects/advance (p3f-phase-transitions). The
    checkpoint "Ready to spec" gesture: advance an ACTIVE project's phase one
    forward lifecycle step, via `projects_store.next_lifecycle_state` +
    `can_transition`. This endpoint owns ONLY the Brainstorm→Spec checkpoint —
    Spec→Building is owned by Launch + status writeback, Building→Done by status
    writeback — so a phase NOT at Brainstorm is rejected (409). Steps:

      1. 404 if the active project / phase doesn't exist.
      2. 409 if the phase isn't at Brainstorm (already advanced, or a stage this
         endpoint doesn't own).
      3. Bump `lifecycle_state` to `next_lifecycle_state` (guarded by
         `can_transition`), stamp `updated_at`, atomic-write the registry.

    Non-committer: the bump lands on disk; `heal_projects_store.py` commits the
    delta. Returns {project_id, phase_id, from_state, to_state, status}."""
    now = now or datetime.now(timezone.utc)
    with _PROJECTS_INGEST_LOCK:
        registry = _read_projects_registry(projects_path)
        phase = _find_active_phase(registry['projects'], project_id, phase_id)
        if phase is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    'error': 'phase not found',
                    'project_id': project_id,
                    'phase_id': phase_id,
                    'hint': (
                        'phase_id must name a phase inside an ACTIVE project_id '
                        'in the projects store'
                    ),
                },
            )

        from_state = phase.get(
            'lifecycle_state', projects_store.DEFAULT_LIFECYCLE_STATE)
        if from_state != 'brainstorm':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'phase not at brainstorm checkpoint',
                    'phase_id': phase_id,
                    'lifecycle_state': from_state,
                    'hint': (
                        'The advance endpoint owns only the Brainstorm→Spec '
                        'checkpoint; Spec→Building is driven by Launch and '
                        'Building→Done by status writeback.'
                    ),
                },
            )

        to_state = projects_store.next_lifecycle_state(from_state)
        if to_state is None or not projects_store.can_transition(from_state, to_state):
            # Defensive: brainstorm→spec is always legal, so this only trips if
            # the lifecycle model changes underneath us — fail loudly, don't write.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'illegal transition',
                    'phase_id': phase_id,
                    'from_state': from_state,
                    'to_state': to_state,
                },
            )

        phase['lifecycle_state'] = to_state
        phase['updated_at'] = now.isoformat()
        try:
            _atomic_write_json(projects_path, registry)
        except OSError as e:
            first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={'error': 'projects write failed', 'detail': first_line},
            )

    return {
        'project_id': project_id,
        'phase_id': phase_id,
        'from_state': from_state,
        'to_state': to_state,
        'status': 'advanced',
    }


def _handle_spec_attach(
    *,
    project_id: str,
    phase_id: str,
    spec_ref: str,
    projects_path: Path,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/projects/attach-spec (p3f-phase-transitions).
    Points a Spec-stage phase at its already-authored spec doc, making it
    spec-ready (the Launch button keys off `spec_ref`). Steps:

      1. 400 if `spec_ref` doesn't resolve to an EXISTING file inside the repo
         (spec § 4 guardrail: a non-existent spec path fails loudly, never
         creates an un-launchable "spec-ready" phase). Validated BEFORE any
         write so a bad ref leaves projects.json byte-identical.
      2. 404 if the active project / phase doesn't exist.
      3. 409 if the phase isn't at the Spec stage (attach is the Spec affordance;
         advance Brainstorm→Spec first).
      4. Set `spec_ref`, stamp `updated_at`, atomic-write the registry.

    Non-committer: the `spec_ref` lands on disk; `heal_projects_store.py` commits
    the delta. Returns {project_id, phase_id, spec_ref, lifecycle_state, status}."""
    now = now or datetime.now(timezone.utc)
    spec_ref = spec_ref.strip()
    resolved = _resolve_spec_ref(spec_ref)
    if resolved is None or not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                'error': 'spec doc not found',
                'spec_ref': spec_ref,
                'hint': (
                    'spec_ref must be a repo-relative path to an existing spec '
                    'doc (e.g. agents/beacon/specs/<slug>.md); attach points the '
                    'phase at an authored spec, it does not create one.'
                ),
            },
        )

    with _PROJECTS_INGEST_LOCK:
        registry = _read_projects_registry(projects_path)
        phase = _find_active_phase(registry['projects'], project_id, phase_id)
        if phase is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    'error': 'phase not found',
                    'project_id': project_id,
                    'phase_id': phase_id,
                    'hint': (
                        'phase_id must name a phase inside an ACTIVE project_id '
                        'in the projects store'
                    ),
                },
            )

        lifecycle = phase.get(
            'lifecycle_state', projects_store.DEFAULT_LIFECYCLE_STATE)
        if lifecycle != 'spec':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'error': 'phase not at spec stage',
                    'phase_id': phase_id,
                    'lifecycle_state': lifecycle,
                    'hint': (
                        'Attach is the Spec-stage affordance; advance the phase '
                        'Brainstorm→Spec before attaching its spec doc.'
                    ),
                },
            )

        phase['spec_ref'] = spec_ref
        phase['updated_at'] = now.isoformat()
        try:
            _atomic_write_json(projects_path, registry)
        except OSError as e:
            first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={'error': 'projects write failed', 'detail': first_line},
            )

    return {
        'project_id': project_id,
        'phase_id': phase_id,
        'spec_ref': spec_ref,
        'lifecycle_state': lifecycle,
        'status': 'spec-attached',
    }


# A project's `promoted_from.kind` → the funnel lane archiving returns it to.
# Only these kinds carry a funnel source that un-suppresses on archive; a project
# with no (or unrecognized) provenance is archive-only and must NOT claim a return.
_FUNNEL_SOURCE_KINDS = frozenset({'capture', 'mission', 'orphan'})


def _archive_outcome(promoted_from: Any, *, retire: bool = False) -> dict[str, str]:
    """The honest Drop outcome for a project (p3f2/p3f3, spec § 6 — "Drop does
    what it says"). The `message` is the FULL display-ready toast text the UI
    renders verbatim — three mutually exclusive cases:

      • `retire` (a DONE project) → 'retired' / "Completed — cleared from the
        board." The work is finished; it leaves the board and its source is NOT
        returned to the funnel (the terminal "complete" gesture, not a reversal).
        Takes precedence over provenance — done is done regardless of source.
      • not done + funnel provenance (capture / mission / orphan) → 'returned-to-
        funnel' / "Dropped — returned to the funnel." A mis-promote is reversible:
        the capture is re-parked here; mission/orphan are un-suppressed
        structurally by the funnel derive's suppression sets.
      • not done + no funnel provenance → 'archived' / "Archived."

    Single source of truth for the toast across the flip and the idempotent no-op
    return, so the message can never claim a behavior that didn't happen."""
    if retire:
        return {'status': 'retired',
                'message': 'Completed — cleared from the board.'}
    kind = promoted_from.get('kind') if isinstance(promoted_from, dict) else None
    if kind in _FUNNEL_SOURCE_KINDS:
        return {'status': 'returned-to-funnel',
                'message': 'Dropped — returned to the funnel.'}
    return {'status': 'archived', 'message': 'Archived.'}


def _capture_is_parked(
    captures_path: Optional[Path], capture_id: Optional[str],
) -> bool:
    """Read-only: is the capture CURRENTLY in the parked/funnel lane? Used by the
    idempotent (already-archived) archive path so the toast reflects the capture's
    present state rather than its provenance kind — a capture re-parked by an
    earlier archive but since GC'd/dropped (or re-promoted) must NOT claim
    "returned to the funnel" on a re-archive (p3f2 honesty contract, spec §4). No
    mutation, no write. MUST be called holding `_CAPTURE_INGEST_LOCK`."""
    if captures_path is None or not capture_id:
        return False
    registry = _read_captures_registry(captures_path)
    for c in registry.get('captures') or []:
        if isinstance(c, dict) and c.get('id') == capture_id:
            return c.get('state') == 'parked'
    return False


def _return_capture_to_funnel(
    captures_path: Optional[Path], capture_id: Optional[str], now: datetime,
) -> bool:
    """Un-flip a capture promoted into a now-archived project so it RETURNS to the
    parked/funnel lane (p3f2 — capture→parked, the reverse of
    `_handle_capture_promote` step 2). Reverses exactly the three fields promote
    set: `state` 'promoted'→'parked', drop `promoted_to`, drop `spawned`. The
    parked lane filters `state=='parked'`, so this is what actually makes the
    capture re-appear (mission/orphan return structurally; a capture is mutated on
    its own store and so must be mutated back). Returns True iff a capture was
    actually re-parked — the caller uses this so the toast only claims a return
    that really happened.

    Non-committer, like promote: the flip lands on captures.json ON DISK; its sole
    committer `heal_missions_card_gc` version-controls the delta. Idempotent +
    fail-safe: a missing capture, or one no longer in `promoted` state, is left
    untouched (returns False — nothing to return). MUST be called holding
    `_CAPTURE_INGEST_LOCK` (CAPTURE→PROJECTS order) to serialize against
    ingest/promote."""
    if captures_path is None or not capture_id:
        return False
    registry = _read_captures_registry(captures_path)
    cap = None
    for c in registry.get('captures') or []:
        if isinstance(c, dict) and c.get('id') == capture_id:
            cap = c
            break
    if cap is None or cap.get('state') != 'promoted':
        return False
    cap['state'] = 'parked'
    cap.pop('promoted_to', None)
    cap.pop('spawned', None)
    cap['updated_at'] = _now_utc_iso(now)
    registry['schema_version'] = CAPTURES_SCHEMA_VERSION
    try:
        _atomic_write_captures(captures_path, registry)
    except OSError as e:
        first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={'error': 'captures write failed', 'detail': first_line},
        )
    return True


def _handle_project_archive(
    *,
    project_id: str,
    projects_path: Path,
    captures_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Handler for POST /api/projects/archive (p3f-reversibility-and-orphan +
    p3f2-archive-honest + p3f3-complete-and-retire, spec § 6 step 2/3). The Drop
    gesture is PHASE-AWARE — the terminal state and whether the source returns
    depend on whether the project is Done:

      • A DONE project (every phase done) is RETIRED: `state='retired'`, it leaves
        the board, and its funnel source is NOT returned — the work is finished,
        not mis-promoted. This is the "Complete & retire" terminal gesture.
      • A not-done project is DROPPED back: `state='archived'`, it leaves the
        board, and its funnel source RETURNS to the funnel (the reversible
        "Return to funnel" escape hatch — a mis-promote is never a dead end).

    Steps:
      1. 404 if no project carries `project_id`.
      2. Compute `retire` from the project's phases (rollup == 'done').
      3. Idempotent: a project already in a terminal state (`archived`/`retired`)
         is a no-op (applied=False) — no write, no spurious heal-commit delta —
         but still reports the honest outcome.
      4. Flip `state` to `retired` (done) or `archived` (not done), stamp
         `updated_at`, atomic-write the registry.
      5. Return the source ONLY when NOT retiring AND it came from the funnel: a
         capture is re-parked here (`_return_capture_to_funnel`); a mission/orphan
         is un-suppressed STRUCTURALLY — the funnel derive's suppression sets
         (`_promoted_mission_ids`, `_promoted_orphan_task_ids`) count `active` AND
         `retired` projects, so flipping to `archived` (and ONLY `archived`)
         returns it to its lane. A retired project keeps suppressing its source.

    Honesty contract (p3f2/p3f3): `status`/`message` is the full display-ready
    toast and matches behavior exactly — 'retired'/"Completed — cleared from the
    board." for a Done project; 'returned-to-funnel'/"Dropped — returned to the
    funnel." for a reversible drop of a funnel-sourced project; otherwise
    'archived'/"Archived." The toast never claims a return that didn't happen.

    Single-committer preserved: the project flip lands on projects.json ON DISK
    (`heal_projects_store.py` is the SOLE committer of THAT file); the capture
    un-flip lands on captures.json ON DISK (its own sole committer
    `heal_missions_card_gc`). The dashboard commits neither. Locks taken in the
    global CAPTURE→PROJECTS order (matching `_handle_capture_promote`) so an
    archive's capture un-flip can't deadlock a concurrent promote. Returns
    {project_id, state, status, message, applied}."""
    now = now or datetime.now(timezone.utc)
    # CAPTURE→PROJECTS lock order (same as _handle_capture_promote). The capture
    # lock is taken unconditionally — archive is a rare human gesture and we don't
    # know the provenance until the project is read under the projects lock, so
    # acquiring capture-first is the only inversion-free order. The capture WRITE
    # only happens for capture-provenance projects.
    with _CAPTURE_INGEST_LOCK:
        with _PROJECTS_INGEST_LOCK:
            registry = _read_projects_registry(projects_path)
            project = None
            for proj in registry['projects']:
                if isinstance(proj, dict) and proj.get('id') == project_id:
                    project = proj
                    break
            if project is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        'error': 'project not found',
                        'project_id': project_id,
                        'hint': 'project_id must name a project in the projects store',
                    },
                )

            promoted_from = project.get('promoted_from')
            current_state = project.get('state', projects_store.DEFAULT_PROJECT_STATE)

            if current_state in ('archived', 'retired'):
                # Idempotent no-op: the project already LEFT the board. Report the
                # outcome that matches its ALREADY-RECORDED terminal state, NOT a
                # fresh recompute from phases — a project dropped while not-done
                # that later completed offline is still `archived`, and the toast
                # must describe what happened, not relitigate it (so `state` and
                # `status` can never disagree). A retired project → "Completed…".
                # For an archived (dropped) one, a capture's return is only real if
                # its row is STILL parked — kind alone would re-claim a return for a
                # capture since GC'd/dropped (the leak p3f2 fixed).
                was_retired = current_state == 'retired'
                idempotent_outcome = _archive_outcome(promoted_from, retire=was_retired)
                if (not was_retired
                        and isinstance(promoted_from, dict)
                        and promoted_from.get('kind') == 'capture'
                        and not _capture_is_parked(
                            captures_path, promoted_from.get('capture_id'))):
                    idempotent_outcome = _archive_outcome(None)
                return {
                    'project_id': project_id,
                    'state': current_state,
                    'status': idempotent_outcome['status'],
                    'message': idempotent_outcome['message'],
                    'applied': False,
                }

            # Active project → apply the phase-aware terminal flip. A Done project
            # RETIRES (terminal, source not returned); a not-done one DROPS back to
            # the funnel. `retire` is computed from the stored phases — the SAME
            # rollup the pipeline card shows, so the button label and the outcome
            # can't disagree.
            retire = projects_store.project_is_done(project)
            terminal_state = 'retired' if retire else 'archived'
            outcome = _archive_outcome(promoted_from, retire=retire)

            project['state'] = terminal_state
            project['updated_at'] = now.isoformat()
            try:
                _atomic_write_json(projects_path, registry)
            except OSError as e:
                first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={'error': 'projects write failed', 'detail': first_line},
                )

        # Project flip is persisted. ONLY a reversible drop (not a retire) returns
        # the source. For a capture source, re-park it so the "returned to the
        # funnel" message is actually true (mission/orphan need no write — they
        # return structurally now that `archived` is excluded from the suppression
        # sets). Still under _CAPTURE_INGEST_LOCK. If the capture row is gone
        # (can't be re-parked), downgrade to an honest "Archived." — the toast must
        # not claim a return that didn't happen. A retire never re-parks: the work
        # is done and the source stays consumed.
        if (not retire
                and isinstance(promoted_from, dict)
                and promoted_from.get('kind') == 'capture'):
            if not _return_capture_to_funnel(
                    captures_path, promoted_from.get('capture_id'), now):
                outcome = _archive_outcome(None)

    return {
        'project_id': project_id,
        'state': terminal_state,
        'status': outcome['status'],
        'message': outcome['message'],
        'applied': True,
    }


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
    # Phase 2.1 FIX 1 + FIX 3: derive the canonical decision key ONCE, up front.
    # FIX 3 — stamp it into the larry_action audit row (below) so
    # heal_stale_approvals can reconcile a PR-coordinate pending entry when the
    # live fan-out is skipped/failed (the dashboard larry_action row otherwise
    # carries no pr_url/decision_key, so the backstop couldn't match it).
    # FIX 1 — set `fan_entry_id` ONLY for decision-type source rows, so a
    # mark_done on a bare escalation/alert never pops a pending approval that
    # merely shares a canonical key.
    _DECISION_EVENT_TYPES = {'approval_request', 'clarify_request'}
    _decision_resolve = None
    decision_key: Optional[str] = None
    try:
        import decision_resolve as _decision_resolve  # noqa: F811
    except Exception:  # noqa: BLE001 — no module → no fan-out; healer backstops
        _decision_resolve = None
    if _decision_resolve is not None:
        # Key derivation is isolated from the import: a derivation error (e.g. a
        # malformed source row with a non-string task_id) must NOT disable the
        # whole fan-out. With the module in hand, the P-leg can still pop the
        # acted-on entry by entry_id even when the cross-store key is underivable.
        try:
            _src_payload = source.get('payload') if isinstance(
                source.get('payload'), dict) else {}
            decision_key = _src_payload.get('decision_key') or \
                _decision_resolve.canonical_decision_key(
                    source_task_id, source.get('pr_url'))
        except Exception:  # noqa: BLE001 — key derivation is best-effort
            decision_key = None
    fan_entry_id = (
        source_task_id if source.get('event_type') in _DECISION_EVENT_TYPES
        else None
    )
    audit_persisted = True
    audit_error: Optional[str] = None
    action_event_id: Optional[str] = None
    try:
        compute_event_id, sanitize_payload = _import_chain_event_helpers()
        action_payload = {
            'source_event_id': source_event_id,
            'source_event_type': source.get('event_type'),
            'decision_key': decision_key,
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

    # Phase 2 Change B: fan the resolution out to the OTHER needs-Larry stores
    # (pending-approvals / escalations / alerts) so a dashboard approve/reject/
    # mark_done clears the same decision everywhere at once, instead of leaving
    # the Telegram queue + escalation feed + alert line reading "still waiting"
    # until heal_stale_approvals reconciles. The dashboard already cleared its
    # OWN chain_events row via the atomic claim above, so the C-leg is a harmless
    # idempotent no-op here. Best-effort: a fan-out failure NEVER fails the
    # action (which already committed) — the healer backstops it. `comment` does
    # NOT fan out (it is not a resolution).
    _LARRY_ACTION_TO_OUTCOME = {
        'approve': 'approved', 'reject': 'rejected', 'mark_done': 'approved',
    }
    fan_outcome = _LARRY_ACTION_TO_OUTCOME.get(action)
    if fan_outcome is not None and _decision_resolve is not None \
            and (decision_key or fan_entry_id):
        try:
            # Phase 2.1 FIX 1: pass entry_id so the P-leg pops ONLY the acted-on
            # pending approval (decision-type rows), never a key-colliding
            # sibling; the key clears the C/E/A stores. Key + entry_id were
            # derived up front.
            _decision_resolve.resolve_decision(
                decision_key, fan_outcome, entry_id=fan_entry_id, actor=actor,
                note=comment or '', chain_client=supabase_client,
            )
        except Exception:  # noqa: BLE001 — fan-out is best-effort; healer backstops
            logger.exception(
                'larry_action cross-store fan-out failed for source event %s '
                '(action=%s); heal_stale_approvals will reconcile',
                source_event_id, action,
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


def _handle_autonomy_posture_post(
    *,
    level: str,
    actor: str,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Apply an autonomy-dial preset: write the preset policy to the override
    file + write the larry_action audit row, then return the resulting posture.

    Raises HTTPException(400) on an unknown level BEFORE any write. The override
    file (trust_policy.OVERRIDE_POLICY_PATH) lives OUTSIDE the synced config/
    tree, so ourliberty-sync never clobbers it; it wins over the git policy until
    deleted. Atomic write (tmp + fsync + os.replace) so a concurrent evaluate()
    never reads a half-written policy. The dial can only pick a known preset —
    each keeps default_action='force_ask' and the standing gates — so it can
    never author an arbitrary or gate-weakening policy.
    """
    import trust_policy  # noqa: PLC0415 — scripts/ on sys.path at module load
    if level not in trust_policy.AUTONOMY_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid level={level!r}',
        )
    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()

    policy = trust_policy.policy_for_level(level)
    target = trust_policy.OVERRIDE_POLICY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    _import_atomic_io().atomic_write_text(
        target, json.dumps(policy, indent=2) + '\n',
    )

    # Audit row — same writer contract as _handle_rotation_mode_post (top-level
    # `actor` column, dedup on event_id). No source-event lookup: this control
    # has no originating chain-event.
    compute_event_id, sanitize_payload = _import_chain_event_helpers()
    action_payload = {
        'control': 'autonomy_posture',
        'level': level,
        'override_file': str(target),
    }
    action_event_id = compute_event_id('autonomy-posture', 'larry_action', ts_iso)
    row: dict[str, Any] = {
        'event_id': action_event_id,
        'ts': ts_iso,
        'agent': 'dashboard',
        'event_type': 'larry_action',
        'actor': actor,
        'task_id': 'autonomy-posture',
        'payload': sanitize_payload(action_payload),
    }
    supabase_client.table('chain_events').upsert(
        [row], on_conflict='event_id', ignore_duplicates=True,
    ).execute()

    # Re-read through the override → resolve → load → summarize round-trip so the
    # response reflects the truly-persisted state (and proves the write landed).
    return trust_policy.summarize_policy(trust_policy.load_policy())


def _handle_build_sequence_action(
    *,
    seq_id: str,
    action: str,
    step_id: Optional[str],
    reason: Optional[str],
    actor: str,
    supabase_client: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pure handler for POST /api/system/build-sequences/{seq_id}/action
    (operator-needs-you-feed spec § 5.5).

    Validates the steering verb against the allowlist (unknown → 400),
    requires step_id for the step-scoped verbs (skip / retry → 400 when
    missing), 404s when the sequence file is absent, then delegates to the
    matching sequence_shortcut_helpers.apply_* helper and writes a
    larry_action audit row keyed on (seq_id, action, ts). Raises
    HTTPException for 4xx; a helper hard error maps to 404 (not-found) /
    500. The audit write is best-effort — the helper mutation IS the action
    and is already on disk by the time we record it, so a failed audit write
    returns success with audit_persisted=False + audit_error rather than a
    500 (mirrors _handle_larry_action's audit-gap contract).
    """
    if action not in BUILD_SEQUENCE_ACTION_VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'invalid action={action!r}',
        )
    if action in BUILD_SEQUENCE_STEP_ACTIONS and not step_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'step_id required for action={action!r}',
        )

    import sequence_shortcut_helpers as ssh  # noqa: PLC0415 — scripts/ on sys.path

    # 404 pre-check via the helper's own path resolution so the existence
    # gate stays single-sourced with the apply_* helpers (and test-isolatable
    # by monkeypatching ssh.AGENTS_ROOT).
    seq_path = ssh.AGENTS_ROOT / 'blackboard' / 'build-sequences' / f'{seq_id}.json'
    if not seq_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'sequence {seq_id!r} not found',
        )

    now = now or datetime.now(timezone.utc)
    ts_iso = now.isoformat()

    if action == 'resume':
        result = ssh.apply_resume(seq_id, actor=actor)
    elif action == 'cancel':
        result = ssh.apply_cancel(seq_id, actor=actor, reason=reason)
    elif action == 'skip':
        result = ssh.apply_skip(seq_id, step_id, actor=actor, reason=reason)
    else:  # retry
        result = ssh.apply_retry(seq_id, step_id, actor=actor)

    # A hard helper error (read/parse/validate/write fail, or step not found)
    # surfaces as 404 when it's a not-found and 500 otherwise. Validation 400s
    # already fired above, before the mutation.
    if result.error:
        lowered = result.reason.lower()
        code = (
            status.HTTP_404_NOT_FOUND
            if 'not found' in lowered
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        raise HTTPException(status_code=code, detail=result.reason)

    # Audit row — same writer contract as _handle_rotation_mode_post (top-level
    # `actor` column, dedup on event_id). Keyed on (seq_id, action, ts) so two
    # distinct verbs on the same sequence in the same microsecond produce
    # distinct ids instead of one silently dropping via ignore_duplicates.
    # Best-effort: the apply_* mutation already landed on disk, so a failed
    # audit write must not 500 (which would invite a re-apply on retry).
    audit_persisted = True
    audit_error: Optional[str] = None
    action_event_id: Optional[str] = None
    try:
        compute_event_id, sanitize_payload = _import_chain_event_helpers()
        action_payload = {
            'control': 'build_sequence_action',
            'seq_id': seq_id,
            'action': action,
            'step_id': step_id,
            'reason': reason,
            'applied': result.applied,
            'detail': result.reason,
            'sequence_path': str(result.sequence_path),
        }
        action_event_id = compute_event_id(
            seq_id, 'larry_action', ts_iso, extra=action,
        )
        row: dict[str, Any] = {
            'event_id': action_event_id,
            'ts': ts_iso,
            'agent': 'dashboard',
            'event_type': 'larry_action',
            'actor': actor,
            'task_id': seq_id,
            'payload': sanitize_payload(action_payload),
        }
        supabase_client.table('chain_events').upsert(
            [row], on_conflict='event_id', ignore_duplicates=True,
        ).execute()
    except Exception as exc:  # noqa: BLE001 — best-effort audit; mutation landed
        audit_persisted = False
        audit_error = str(exc)
        action_event_id = None
        logger.exception(
            'build_sequence_action AUDIT GAP: %s on sequence %s (step=%s, '
            'actor=%s) applied on disk but audit-row write failed; this '
            'action has no audit row.',
            action, seq_id, step_id, actor,
        )

    return {
        'applied': result.applied,
        'action': action,
        'seq_id': seq_id,
        'step_id': step_id,
        'detail': result.reason,
        'action_event_id': action_event_id,
        'audit_persisted': audit_persisted,
        'audit_error': audit_error,
    }


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
    import active_tier  # noqa: PLC0415 — durable per-tier setup-token bridge
    refuse_under_test('claude-spawn')
    # Per-task tier dispatch (spec §10-W4): round-robin a tier; skip cleanly
    # when none is available (return {} = keep every uncertain row, the
    # fail-safe posture this helper already uses).
    env, tier = active_tier.select_durable_claude_env()
    if env is None:
        return {}
    try:
        proc = subprocess.run(
            ['claude', '--print', '--model', CLEANUP_REVIEW_VERIFY_MODEL,
             '--output-format', 'json', prompt],
            capture_output=True, text=True,
            env=env, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        return {}
    # §8/§10-W4: stamp an account-tagged cost row so this dispatch's burn is
    # visible per-tier (this path does not route through inbox_watcher).
    if isinstance(data, dict):
        try:
            active_tier.append_cost_row(
                tier, model=CLEANUP_REVIEW_VERIFY_MODEL,
                cost_usd=data.get('total_cost_usd'), usage=data.get('usage'),
                agent='dashboard-cleanup-verify',
                source='dashboard-cleanup-verify')
        except Exception:  # noqa: BLE001 — cost ledger must never break the API
            pass
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


# POST /api/system/agent-queue/{agent}/fast-track — the "Build next" gesture
# (forge-queue-fast-track). Floats a queued inbox task to the head of the
# dispatch order. Same two-layer auth as the other write routes:
# X-Dashboard-Token + an allowlisted X-Actor. Changes only WHEN a task builds,
# never what/whether — no preflight/review gate is bypassed.
@app.post(
    '/api/system/agent-queue/{agent}/fast-track',
    response_model=FastTrackResponse,
    dependencies=[Depends(_require_token)],
)
def post_agent_queue_fast_track(
    agent: str,
    body: FastTrackRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    if agent not in AGENT_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'unknown agent: {agent!r}',
        )
    return _handle_fast_track(_agents_root(), agent, body.task_id)


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


# GET /api/system/state-log — the work-in-flight State Log (system
# self-awareness Slice 1 § D3). Serves the narrator's doc verbatim plus
# present/stale freshness signals; same auth gate as the sibling /api/system
# reads. Fail-safe: a missing/malformed log returns present=False, never a 500.
@app.get(
    '/api/system/state-log',
    response_model=SystemStateLogResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_state_log() -> dict[str, Any]:
    return _reader_system_state_log(_state_log_json_path())


# GET /api/system/automated-work — the "Automated Work" feed (system
# self-awareness: the autonomy dial-in surface). Reads chain_events
# autonomy_decision rows; the State Log is untouched (it's local-files-only).
@app.get(
    '/api/system/automated-work',
    response_model=AutomatedWorkResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_automated_work(
    window_days: int = Query(_AUTOMATED_WORK_WINDOW_DAYS, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    client = _get_larry_action_supabase_client()
    return _reader_automated_work(
        client, window_days=window_days, limit=limit,
    )


# GET /api/system/autonomy-posture — the autonomy POSTURE (system
# self-awareness: the *prospective* dial-in surface, the companion to
# /api/system/automated-work). A plain-language read of the live trust policy's
# current stance — what auto-starts, what still asks Larry, the always-on gates.
# Pure read of config/trust-policy.json via trust_policy.summarize_policy();
# load_policy() and summarize_policy() both fail-closed and NEVER raise, so the
# lane never 500s (a bad policy reads as degraded="everything asks you").
@app.get(
    '/api/system/autonomy-posture',
    response_model=AutonomyPostureResponse,
    dependencies=[Depends(_require_token)],
)
def get_system_autonomy_posture() -> dict[str, Any]:
    import trust_policy  # noqa: E402 — scripts/ is on sys.path at module load
    return trust_policy.summarize_policy(trust_policy.load_policy())


# GET /api/missions/captures — the Parked-lane data source (Missions v2
# Phase 1 § 5). Serves captures.json verbatim + an mtime-derived
# last_synced_at; the dashboard filters to state=='parked'.
@app.get(
    '/api/missions/captures',
    response_model=CapturesResponse,
    dependencies=[Depends(_require_token)],
)
def get_missions_captures() -> dict[str, Any]:
    data = _reader_captures(_captures_json_path())
    # Phase 4b.2 Contract E: enrich each parked card with a doorbell signal
    # derived from one batched chain_events read. Additive + fail-safe — the
    # file-read contract above stands even if the doorbell read degrades.
    _project_card_doorbells(data['captures'], _get_larry_action_supabase_client())
    return data


class _TTLCache:
    """Tiny thread-safe in-process single-value-per-key TTL cache.

    Throttles the relatively expensive /api/missions/derived derive (file
    reads + chain_events fetch + a bounded GitHub PR-state read) to at most
    one recompute per `ttl_seconds` window per key. Bounded by design: a short
    TTL means nothing is served older than the window, and the keyspace is the
    small set of (repo, task_id) filter combinations the dashboard issues.

    `clock` defaults to `time.monotonic` (immune to wall-clock jumps) and is
    injectable so expiry is unit-testable without sleeping.
    """

    def __init__(
        self,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = __import__('threading').Lock()
        self._entries: dict[Any, tuple[float, Any]] = {}

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> Any:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (now - entry[0]) < self._ttl:
                return entry[1]
        # Compute outside the lock: holding it across the file/network read
        # would serialize every concurrent request (the endpoint is a sync def
        # run in uvicorn's threadpool). The cost is that two callers racing a
        # cold key both compute once — a bounded inefficiency, never staleness.
        value = compute()
        with self._lock:
            self._entries[key] = (self._clock(), value)
        return value


# ~10s window: bounds the derive cost while keeping the board near-live. The
# single uvicorn worker (systemd/ourliberty-dashboard-api.service) runs this
# sync endpoint in a threadpool, so the cache is shared across request threads
# and must be thread-safe — hence _TTLCache's internal lock.
_DERIVED_CACHE = _TTLCache(ttl_seconds=10.0)


# GET /api/missions/derived — the relocated mission-phase derive (Missions v2
# Phase 2 § 3). Source-of-truth phase/aggregate/orphan derivation, ported
# byte-faithfully from the dashboard's lib/mission-queries.ts (parity-gated,
# § 4), plus the additive parked[] array + orphan-readability fields. Optional
# ?repo= / ?task_id= filters (§ 3.2). Read-only; same X-Dashboard-Token gate as
# /api/system/missions. Orphan terminal detection does a bounded, fail-safe
# GitHub PR-state read (_resolve_orphan_pr_states, § 3.4) — reusing the existing
# GITHUB_TOKEN, degrading to the event-only derive on any error.
#
# Wrapped in a ~10s in-process TTL cache (_DERIVED_CACHE) keyed on the filter
# tuple so dashboard polling doesn't re-run the full derive on every request.
@app.get(
    '/api/missions/derived',
    response_model=MissionsDerivedResponse,
    dependencies=[Depends(_require_token)],
)
def get_missions_derived(
    repo: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
) -> dict[str, Any]:
    return _DERIVED_CACHE.get_or_compute(
        (repo, task_id),
        lambda: _handle_missions_derived(
            missions_path=_missions_json_path(),
            captures_path=_captures_json_path(),
            supabase_client=_get_larry_action_supabase_client(),
            repo=repo,
            task_id=task_id,
            pr_state_resolver=_resolve_orphan_pr_states,
            projects_path=_projects_json_path(),
        ),
    )


@app.post(
    '/api/system/missions/new',
    response_model=NewMissionResponse,
    dependencies=[Depends(_require_token)],
)
def post_system_missions_new(body: NewMissionRequest) -> dict[str, Any]:
    return _handle_new_mission(
        body=body,
        missions_path=_missions_json_path(),
        queue_dir=_new_mission_queue_dir(),
    )


# POST /api/missions/captures/{capture_id}/action — capture write-back (Missions
# v2 Phase 3 § 4). All one-click (no PR): promote queues the mission for the
# missions writer + flips the capture locally; drop/snooze are direct captures.json
# committer writes. Same auth as /api/larry/action: X-Dashboard-Token + an
# allowlisted X-Actor.
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
    # Phase S (S7): the in-flight gate uses the live chain_events client. When no
    # client is available (no creds) it stays None so the resolver is skipped —
    # the action applies immediately rather than being held indefinitely.
    client = _get_larry_action_supabase_client()
    in_flight_resolver = (
        (lambda cap: _spawned_work_in_flight(cap, client))
        if client is not None else None
    )
    return _handle_capture_action(
        capture_id=capture_id,
        action=body.action,
        args=body.model_dump(exclude={'action'}, exclude_none=True),
        captures_path=_captures_json_path(),
        projects_path=_projects_json_path(),
        in_flight_resolver=in_flight_resolver,
    )


# GET /api/missions/captures/{capture_id}/thread — the card's async conversation
# (Missions v2 Phase 4 § 8). Reads back the capture's card_message chain_events,
# oldest-first. Read-only → X-Dashboard-Token suffices (no actor mutation).
@app.get(
    '/api/missions/captures/{capture_id}/thread',
    response_model=CaptureThreadResponse,
    dependencies=[Depends(_require_token)],
)
def get_capture_thread(capture_id: str) -> dict[str, Any]:
    return _handle_capture_thread(
        capture_id=capture_id,
        captures_path=_captures_json_path(),
        supabase_client=_get_larry_action_supabase_client(),
    )


# POST /api/missions/captures/{capture_id}/message — Larry posts on a card
# (Missions v2 Phase 4 § 8). Emits a card_message event (direction larry_to_team),
# drops a resume envelope in Beacon's inbox, and clears any blocked-on-you
# doorbell. Same auth as the action route: X-Dashboard-Token + allowlisted X-Actor.
@app.post(
    '/api/missions/captures/{capture_id}/message',
    response_model=CaptureMessageResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_capture_message(
    capture_id: str,
    body: CaptureMessageRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_capture_message(
        capture_id=capture_id,
        text=body.text,
        actor=actor,
        captures_path=_captures_json_path(),
        agents_root=_agents_root(),
        supabase_client=_get_larry_action_supabase_client(),
    )


# POST /api/missions/captures/{capture_id}/delegate — "Delegate to team" (the
# primary Parked-card action; delegate-fix spec § 2). Emits a human-approval-gate
# APPROVAL_REQUEST proposal into Beacon's inbox; the capture stays parked (no
# captures.json mutation). Same auth as the action route: X-Dashboard-Token +
# an allowlisted X-Actor.
@app.post(
    '/api/missions/captures/{capture_id}/delegate',
    response_model=CaptureDelegateResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_capture_delegate(
    capture_id: str,
    actor: str = Depends(_require_actor),
    body: Optional[CaptureDelegateRequest] = None,
) -> dict[str, Any]:
    return _handle_capture_delegate(
        capture_id=capture_id,
        action=body.action if body else None,
        actor=actor,
        captures_path=_captures_json_path(),
    )


# POST /api/system/missions/{mission_id}/action — mission write-back (Missions
# v2 Phase 3 § 5 + § 6). defer/resume/reprioritize/dismiss/drop/snooze are
# PR-backed (missions.json is the curated registry — every change auditable) and
# open a PR via the shared _open_registry_pr helper. `accept` is UNIFIED onto
# Promote (projects-v3 P3): it MOVES the proposed mission into a new single-phase
# project at Brainstorm (no missions.json PR; the mission is suppressed from the
# funnel by the project's `promoted_from` cross-ref) → {project_id, phase_id,
# status, applied}. `dismiss`/`drop` set the additive acknowledged flag (phase
# stays proposed). Same auth as /api/larry/action: X-Dashboard-Token + an
# allowlisted X-Actor.
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
        projects_path=_projects_json_path(),
        actor=actor,
    )


# POST /api/funnel/promote — the unified Promote gesture (projects-v3 P3,
# p3-promote-endpoint). MOVES any funnel item (a parked capture OR a proposed
# mission, auto-resolved from `ref`) into a new single-phase project at
# Brainstorm and removes it from its funnel lane. The dashboard stays a
# non-committer: the project is written to projects.json on disk (heal_projects_
# store commits) and the capture flip rides the captures committer; no missions
# PR. Same auth as the action routes: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/funnel/promote',
    response_model=FunnelPromoteResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_funnel_promote(
    body: FunnelPromoteRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_funnel_promote(
        ref=body.ref,
        kind=body.kind,
        overrides=body.model_dump(
            include={'name', 'brief', 'repo', 'north_star_ref'},
            exclude_none=True,
        ),
        captures_path=_captures_json_path(),
        missions_path=_missions_json_path(),
        projects_path=_projects_json_path(),
    )


# POST /api/projects/launch — the dashboard "Launch build" gate (projects-v3 P3,
# p3-launch-queue-drain). Queues a build-launch request for a spec-ready phase;
# the Beacon-side drainer (`launch_queue_drain.py`) authors the build sequence
# from the phase's spec, runs Mirror DAG preflight, and kicks the build —
# Telegram is never in the loop. The dashboard stays a NON-committer: it only
# writes a queue file under the agents blackboard (the `+New mission`
# precedent); it never commits the repo or the projects store. Same auth as the
# other write routes: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/projects/launch',
    response_model=LaunchBuildResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_launch_build(
    body: LaunchBuildRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_launch_build(
        project_id=body.project_id,
        phase_id=body.phase_id,
        actor=actor,
        projects_path=_projects_json_path(),
        queue_dir=_build_launch_queue_dir(),
        models_path=_agent_models_json_path(),
    )


# POST /api/projects/advance — the checkpoint "Ready to spec" gesture (projects-v3
# P3 follow-up, p3f-phase-transitions). Advances an ACTIVE project's phase one
# forward lifecycle step (Brainstorm→Spec, the human checkpoint this endpoint
# owns). The dashboard stays a NON-committer: the lifecycle bump is written to
# projects.json on disk and `heal_projects_store.py` commits it. Same auth as the
# other write routes: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/projects/advance',
    response_model=PhaseAdvanceResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_phase_advance(
    body: PhaseAdvanceRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_phase_advance(
        project_id=body.project_id,
        phase_id=body.phase_id,
        projects_path=_projects_json_path(),
    )


# POST /api/projects/brainstorm — edit the pre-filled Brainstorm card (projects-v3
# P6.1). Persists the edited draft + decisions onto the phase. Non-committer: the
# edit is written to projects.json on disk and `heal_projects_store.py` commits.
# Same auth: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/projects/brainstorm',
    response_model=EditBrainstormResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_edit_brainstorm(
    body: EditBrainstormRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_edit_brainstorm(
        project_id=body.project_id,
        phase_id=body.phase_id,
        draft=body.draft,
        decisions=body.decisions,
        projects_path=_projects_json_path(),
    )


# GET /api/projects/phases/{phase_ref}/thread — the Brainstorm phase card's async
# conversation (projects-v3 P6.1). `phase_ref` is the composite project_id::phase_id.
# Read-only → X-Dashboard-Token suffices.
@app.get(
    '/api/projects/phases/{phase_ref}/thread',
    response_model=PhaseThreadResponse,
    dependencies=[Depends(_require_token)],
)
def get_phase_thread(phase_ref: str) -> dict[str, Any]:
    return _handle_phase_thread(
        phase_ref=phase_ref,
        projects_path=_projects_json_path(),
        supabase_client=_get_larry_action_supabase_client(),
    )


# POST /api/projects/phases/{phase_ref}/message — Larry posts on a Brainstorm phase
# card (projects-v3 P6.1). `phase_ref` is the composite project_id::phase_id. Emits
# a card_message event + drops a resume envelope in Beacon's inbox (she answers next
# cycle), reusing the kind-generic card-chat core. Same auth as the other write
# routes: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/projects/phases/{phase_ref}/message',
    response_model=CaptureMessageResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_phase_message(
    phase_ref: str,
    body: CaptureMessageRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_phase_message(
        phase_ref=phase_ref,
        text=body.text,
        actor=actor,
        projects_path=_projects_json_path(),
        agents_root=_agents_root(),
        supabase_client=_get_larry_action_supabase_client(),
    )


# POST /api/projects/attach-spec — point a Spec-stage phase at its authored spec
# doc, making it spec-ready so the Launch button appears (projects-v3 P3 follow-up,
# p3f-phase-transitions). Validates the spec doc EXISTS (a non-existent path is
# rejected loudly, never written). The dashboard stays a NON-committer: the
# `spec_ref` is written to projects.json on disk and `heal_projects_store.py`
# commits it. Same auth: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/projects/attach-spec',
    response_model=SpecAttachResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_spec_attach(
    body: SpecAttachRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_spec_attach(
        project_id=body.project_id,
        phase_id=body.phase_id,
        spec_ref=body.spec_ref,
        projects_path=_projects_json_path(),
    )


# POST /api/projects/archive — the Drop/Archive gesture (projects-v3 P3 follow-up,
# p3f-reversibility-and-orphan). Flips a project's state to `archived` so it leaves
# "Actively working" and its original funnel source item (capture / mission /
# orphan) returns to the funnel — a mis-promote is reversible, not a dead end. The
# dashboard stays a NON-committer: the state flip is written to projects.json on
# disk and `heal_projects_store.py` commits it. Same auth as the other write
# routes: X-Dashboard-Token + an allowlisted X-Actor.
@app.post(
    '/api/projects/archive',
    response_model=ProjectArchiveResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_project_archive(
    body: ProjectArchiveRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_project_archive(
        project_id=body.project_id,
        projects_path=_projects_json_path(),
        captures_path=_captures_json_path(),
    )


# POST /api/system/missions/{id}/delegate — "Delegate to team" for a proposed
# mission-backed funnel card (Projects v3 P2 Contract B). Mirrors the parked-card
# Delegate: emits a human-approval-gate APPROVAL_REQUEST proposal into Beacon's
# inbox without mutating missions.json. Same auth: X-Dashboard-Token + X-Actor.
@app.post(
    '/api/system/missions/{mission_id}/delegate',
    response_model=MissionDelegateResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_mission_delegate(
    mission_id: str,
    actor: str = Depends(_require_actor),
    body: Optional[MissionDelegateRequest] = None,
) -> dict[str, Any]:
    return _handle_mission_delegate(
        mission_id=mission_id,
        action=body.action if body else None,
        actor=actor,
        missions_path=_missions_json_path(),
    )


# GET /api/system/missions/{id}/thread — a mission card's async conversation
# (Contract B). Reads back the mission's card_message chain_events, oldest-first.
# Read-only → X-Dashboard-Token suffices.
@app.get(
    '/api/system/missions/{mission_id}/thread',
    response_model=MissionThreadResponse,
    dependencies=[Depends(_require_token)],
)
def get_mission_thread(mission_id: str) -> dict[str, Any]:
    return _handle_mission_thread(
        mission_id=mission_id,
        missions_path=_missions_json_path(),
        supabase_client=_get_larry_action_supabase_client(),
    )


# POST /api/system/missions/{id}/message — Larry posts on a mission card
# (Contract B). Emits a card_message event, drops a resume envelope in Beacon's
# inbox, clears any blocked-on-you doorbell. Same auth: X-Dashboard-Token + X-Actor.
@app.post(
    '/api/system/missions/{mission_id}/message',
    response_model=MissionMessageResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require_token)],
)
def post_mission_message(
    mission_id: str,
    body: MissionMessageRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    return _handle_mission_message(
        mission_id=mission_id,
        text=body.text,
        actor=actor,
        missions_path=_missions_json_path(),
        agents_root=_agents_root(),
        supabase_client=_get_larry_action_supabase_client(),
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


# POST /api/system/autonomy-posture — the autonomy DIAL write (#7), companion to
# the read-only GET above. Token + actor gated like /api/system/rotation; writes
# the chosen preset to the override policy file atomically + audited and echoes
# the resulting posture. The dial can only pick a known preset (each keeps the
# default-deny + standing gates), so it can never author a gate-weakening policy.
@app.post(
    '/api/system/autonomy-posture',
    response_model=AutonomyPostureResponse,
    dependencies=[Depends(_require_token)],
)
def post_system_autonomy_posture(
    body: AutonomyPostureRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    client = _get_larry_action_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='supabase unavailable',
        )
    return _handle_autonomy_posture_post(
        level=body.level,
        actor=actor,
        supabase_client=client,
    )


# POST /api/system/build-sequences/{seq_id}/action — the build-sequence STEERING
# write (operator-needs-you-feed § 5.5), companion to the read-only
# GET /api/system/build-sequences. Token + actor gated like /api/system/rotation;
# delegates the chosen verb (resume|skip|cancel|retry) to the matching
# sequence_shortcut_helpers.apply_* helper and writes an audited larry_action row.
@app.post(
    '/api/system/build-sequences/{seq_id}/action',
    response_model=BuildSequenceActionResponse,
    dependencies=[Depends(_require_token)],
)
def post_system_build_sequence_action(
    seq_id: str,
    body: BuildSequenceActionRequest,
    actor: str = Depends(_require_actor),
) -> dict[str, Any]:
    client = _get_larry_action_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='supabase unavailable',
        )
    return _handle_build_sequence_action(
        seq_id=seq_id,
        action=body.action,
        step_id=body.step_id,
        reason=body.reason,
        actor=actor,
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
