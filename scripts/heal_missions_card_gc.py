#!/usr/bin/env python3
"""heal_missions_card_gc.py — the Missions v2 GC healer (spec § 6).

Phase 1 makes capture *broad* (keep everything); this timer healer keeps the
board clean so breadth doesn't drown it. Three reconciliations per tick, all
idempotent / atomic / fail-safe (a bad tick reports + skips that phase, never
corrupts):

  1. RETIRE STALE DESKTOP-SESSION CARDS (Phase 0 feed). A desktop_session_start
     with no later desktop_session_done is an "open" card. It is retired —
     by emitting a synthetic desktop_session_done via the canonical
     chain_event_emit.emit_event (the SAME write path the ingest endpoint uses)
     — when ANY of:
       * its branch is merged (head of a MERGED PR), or
       * its branch is deleted (no longer a local/remote ref), or
       * its repo dir is gone/archived (the repo_paths dir doesn't exist), or
       * it has been idle past STALE_SESSION_IDLE_SECONDS.
     Mirrors cleanup_dispatch_branches.py's philosophy (merged/abandoned →
     reclaim) applied to cards. Every indeterminate signal errs toward KEEP.
     Idempotent: once the synthetic done lands, the next tick sees the session
     closed and does nothing.

  2. AGE PARKED CAPTURES (never delete). A `state == "parked"` capture whose
     `last_touched` is more than AGING_BUSINESS_DAYS business days old is
     flagged `"aging": true` in captures.json so the dashboard nudges
     (contextual-resurfacing seed; full digest is Phase 2). Captures are NEVER
     deleted and no other field is mutated; setting the flag is idempotent.

  3. COMMIT + PUSH the captures.json delta to main (the batched-durability half
     of § 4). The ingest endpoint writes captures.json atomically on disk but
     opens no PR; this healer version-controls any delta on its timer, exactly
     like run_cycle.sh's self-commit path (push with a pull --rebase --autostash
     fallback on a non-fast-forward).

  4. RECONCILE SHIPPED MISSION PHASES (terminal-state spec § 3.3). A mission in
     a reconcilable phase (drafting/in_flight/ready) whose every task_id is
     terminal (MERGED/CLOSED per the shared task_terminal_state probe) is
     flipped to `shipped` (audit-preserved: prior_phase + shipped_at/by recorded
     in-file and logged), then the missions.json delta is committed like (3).
     `proposed` (owned by missions-proposed-lane-signal-hardening-001) and
     `deferred` are never touched; any OPEN/UNKNOWN task ⇒ KEEP (conservative).

  5. REPORT what it retired/aged/shipped on one audit line, never silently
     truncating (full id lists go to the log; the digest alert carries exact
     counts).

stdlib only (+ supabase-py via chain_event_emit, lazily and optionally).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports (chain_event_emit,
# larry_alerts) resolve when run by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import task_terminal_state as tts  # noqa: E402 — shared terminal-state probe

_MODELS_CONFIG_PATH = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'

# The agent that owns desktop-session cards. The ingest endpoint pins this; we
# mirror it so a synthetic done groups onto the same task_id as the start.
DESKTOP_AGENT = 'desktop-claude'
EVENT_START = 'desktop_session_start'
EVENT_ACTIVE = 'desktop_session_active'
EVENT_DONE = 'desktop_session_done'

# Idle floor: a desktop chat that has gone this long with no activity AND no
# done is over (the operator closed the laptop). 24h is well past any real
# interactive session, so the floor only ever protects a live card.
STALE_SESSION_IDLE_SECONDS = 24 * 3600

# Branches that always exist on a tracked repo — never "deleted"/"merged".
_TRUNK_BRANCHES = frozenset({'main', 'master'})

# Parked captures older than this many BUSINESS days get the aging nudge (§ 6.2).
AGING_BUSINESS_DAYS = 5

CAPTURES_REL = 'agents/beacon/captures.json'

# The mission registry (phase model). Honors OURLIBERTY_MISSIONS_JSON for test
# redirection, mirroring dashboard_api._missions_json_path.
MISSIONS_REL = 'agents/beacon/missions.json'

# Mission phases eligible for terminal-state reconciliation (spec § 3.3). A
# mission in one of these whose every task_id is terminal flips to `shipped`.
# Excluded on purpose: `proposed` (owned by the in-flight
# missions-proposed-lane-signal-hardening-001 PR — never touch), `deferred` (a
# deliberate human hold), and `shipped` (already terminal). Mirrors the three
# phases named in spec § 5 success criteria ("flips out of
# drafting/in_flight/ready").
RECONCILABLE_MISSION_PHASES = frozenset({'drafting', 'in_flight', 'ready'})
SHIPPED_PHASE = 'shipped'

GIT_TIMEOUT_SEC = 60
PUSH_TIMEOUT_SEC = 180
FETCH_TIMEOUT_SEC = 180
GH_TIMEOUT_SEC = 30


# ---------- env-resolved paths (read at call time so tests can override) ----------


def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def _kill_switch_path() -> Path:
    return _agents_root() / 'healers.disabled'


def _log_path() -> Path:
    """Honor the test/CI OURLIBERTY_LOG_DIR override so a test run never writes
    into the live ~/agents/logs/ tree (see scripts/tests/conftest.py)."""
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    base = Path(override) if override else (_agents_root() / 'logs')
    return base / 'missions-card-gc.log'


def log(msg: str) -> None:
    line = f'[{datetime.now(timezone.utc).isoformat()}] {msg}'
    print(line, flush=True)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        # Best-effort: a full/read-only log FS must never crash the healer.
        pass


# ---------- config ----------


def load_repo_paths() -> dict[str, Path]:
    """Repo name → Path from config/agent-models.json ``repo_paths`` (same block
    cleanup_dispatch_branches.py reads). Returns {} on a missing/unreadable
    block — the healer degrades (no merge/repo-gone signals) rather than
    crashing."""
    try:
        cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'could not read {_MODELS_CONFIG_PATH}: {e}')
        return {}
    block = cfg.get('repo_paths') if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return {}
    out: dict[str, Path] = {}
    for name, raw in block.items():
        if isinstance(raw, str) and raw:
            out[name] = Path(raw)
    return out


def captures_path(repo_paths: dict[str, Path]) -> Optional[Path]:
    """Path to agent-core's captures.json, or None if agent-core isn't
    configured."""
    core = repo_paths.get('ourliberty-agent-core')
    return (core / CAPTURES_REL) if core else None


def missions_path(repo_paths: dict[str, Path]) -> Optional[Path]:
    """Path to agent-core's missions.json, or None if agent-core isn't
    configured. Honors OURLIBERTY_MISSIONS_JSON (test redirection)."""
    override = os.environ.get('OURLIBERTY_MISSIONS_JSON')
    if override:
        return Path(override)
    core = repo_paths.get('ourliberty-agent-core')
    return (core / MISSIONS_REL) if core else None


# ---------- time helpers ----------


def parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string to an aware UTC datetime, or None. Mirrors
    dashboard_api._parse_iso_utc."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def business_days_between(start: datetime, end: datetime) -> int:
    """Count Mon–Fri days strictly after ``start``'s date up to and including
    ``end``'s date. Returns 0 if end <= start. Calendar-based (no holidays) —
    a conservative, deterministic aging clock."""
    if end <= start:
        return 0
    days = 0
    cur = start.date()
    end_date = end.date()
    # Bound the loop so a corrupt far-future `now` can't spin forever.
    for _ in range((end_date - cur).days):
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


# ---------- git / gh helpers (never raise) ----------


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """Run git in ``repo``; a timeout/OS error becomes a synthetic non-zero
    result so callers branch on returncode uniformly."""
    try:
        return subprocess.run(
            ['git', *args], cwd=str(repo),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'git {" ".join(args)} failed in {repo}: {type(e).__name__}: {e}')
        return subprocess.CompletedProcess(args, returncode=255, stdout='', stderr=str(e))


def _gh_env() -> dict:
    return {**os.environ,
            'PATH': '/usr/bin:/usr/local/bin:' + os.environ.get('PATH', '')}


def merged_pr_heads(repo: Path) -> Optional[set[str]]:
    """Head ref names of MERGED PRs for ``repo``. None on any gh failure so the
    caller treats "merged" as indeterminate (→ KEEP) rather than empty."""
    try:
        res = subprocess.run(
            ['gh', 'pr', 'list', '--state', 'merged', '--limit', '1000',
             '--json', 'headRefName'],
            cwd=str(repo), capture_output=True, text=True,
            timeout=GH_TIMEOUT_SEC, env=_gh_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr list --state merged in {repo} failed: {type(e).__name__}: {e}')
        return None
    if res.returncode != 0:
        log(f'gh pr list --state merged in {repo} rc={res.returncode}: {res.stderr[:200]}')
        return None
    try:
        rows = json.loads(res.stdout or '[]')
    except json.JSONDecodeError as e:
        log(f'gh pr list --state merged in {repo} bad json: {e}')
        return None
    heads: set[str] = set()
    for row in rows:
        ref = row.get('headRefName') if isinstance(row, dict) else None
        if isinstance(ref, str) and ref:
            heads.add(ref)
    return heads


def existing_branches(repo: Path) -> Optional[set[str]]:
    """Short branch names that exist locally (refs/heads) or on origin
    (refs/remotes/origin), origin/ prefix stripped. None on git failure so the
    caller treats "deleted" as indeterminate (→ KEEP). A best-effort
    ``fetch --prune`` first so a remotely-deleted branch disappears from view."""
    _git(repo, 'fetch', '--prune', 'origin', timeout=FETCH_TIMEOUT_SEC)
    r = _git(repo, 'for-each-ref', '--format=%(refname:short)',
             'refs/heads/', 'refs/remotes/origin/')
    if r.returncode != 0:
        return None
    names: set[str] = set()
    for line in r.stdout.splitlines():
        name = line.strip()
        if not name or name == 'origin':
            continue
        if name.startswith('origin/'):
            name = name[len('origin/'):]
        names.add(name)
    return names


# ---------- desktop-session open-card detection (pure) ----------


@dataclass(frozen=True)
class OpenSession:
    task_id: str
    repo: Optional[str]
    branch: Optional[str]
    last_activity_ts: Optional[datetime]
    idle_seconds: Optional[float]


def gather_open_sessions(rows: list[dict[str, Any]], now: datetime) -> list[OpenSession]:
    """From raw desktop-session chain_events, return the still-open cards: a
    task with a start whose latest start has no later (or equal-ts) done.

    Idempotency anchor: once a (synthetic or real) done lands at ts >= the
    latest start, the task drops out of this list. ``repo``/``branch`` come from
    the latest start's payload; ``last_activity_ts`` is the newest start/active
    ts (done excluded — a stale earlier done is not "activity")."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        tid = r.get('task_id')
        if not isinstance(tid, str) or not tid:
            continue
        if r.get('event_type') not in (EVENT_START, EVENT_ACTIVE, EVENT_DONE):
            continue
        by_task.setdefault(tid, []).append(r)

    out: list[OpenSession] = []
    for tid, evs in by_task.items():
        starts, dones, actives = [], [], []
        for e in evs:
            dt = parse_iso_utc(e.get('ts'))
            if dt is None:
                continue
            et = e.get('event_type')
            if et == EVENT_START:
                starts.append((dt, e))
            elif et == EVENT_DONE:
                dones.append((dt, e))
            elif et == EVENT_ACTIVE:
                actives.append((dt, e))
        if not starts:
            continue
        latest_start_dt, latest_start = max(starts, key=lambda x: x[0])
        latest_done_dt = max((d for d, _ in dones), default=None)
        # Closed iff a done lands at or after the latest start.
        if latest_done_dt is not None and latest_done_dt >= latest_start_dt:
            continue
        activity = [d for d, _ in starts] + [d for d, _ in actives]
        last_activity_dt = max(activity) if activity else latest_start_dt
        payload = latest_start.get('payload') if isinstance(latest_start.get('payload'), dict) else {}
        out.append(OpenSession(
            task_id=tid,
            repo=payload.get('repo'),
            branch=payload.get('branch'),
            last_activity_ts=last_activity_dt,
            idle_seconds=(now - last_activity_dt).total_seconds(),
        ))
    out.sort(key=lambda s: s.task_id)
    return out


# ---------- classification (pure; unit-tested directly) ----------


@dataclass(frozen=True)
class SessionDecision:
    action: str   # 'retire' | 'keep'
    reason: str


def classify_session(
    session: OpenSession,
    *,
    repo_present: Optional[bool],
    branch_merged: Optional[bool],
    branch_deleted: Optional[bool],
    idle_seconds: Optional[float],
    idle_floor: int = STALE_SESSION_IDLE_SECONDS,
) -> SessionDecision:
    """Apply the retire bar to one open card. Pure — the caller resolves all
    I/O (repo-dir presence, merged/deleted) into the keyword facts. Every
    indeterminate signal (None) is skipped; idle alone can still retire because
    it is purely time-based and the floor is conservative. When nothing fires,
    KEEP."""
    if repo_present is False:
        return SessionDecision('retire', 'repo-dir-gone')
    if branch_merged is True:
        return SessionDecision('retire', 'branch-merged')
    if branch_deleted is True:
        return SessionDecision('retire', 'branch-deleted')
    if idle_seconds is not None and idle_seconds > idle_floor:
        return SessionDecision('retire', f'idle>{idle_floor // 3600}h')
    return SessionDecision('keep', 'active/indeterminate')


# ---------- session retirement (effectful) ----------


@dataclass
class RepoSignals:
    """Per-repo git/gh facts, computed once per tick and cached."""
    present: bool
    merged_heads: Optional[set[str]]
    branches: Optional[set[str]]


def _repo_signals(repo_name: Optional[str], repo_paths: dict[str, Path],
                  cache: dict[str, RepoSignals]) -> RepoSignals:
    """Resolve (and cache) merged-PR heads + existing branches for ``repo_name``.

    A repo not in repo_paths is "unknown presence" (present=True so repo-gone
    never fires on a repo we can't authoritatively check) with no git signals."""
    key = repo_name or ''
    if key in cache:
        return cache[key]
    path = repo_paths.get(repo_name) if repo_name else None
    if path is None:
        sig = RepoSignals(present=True, merged_heads=None, branches=None)
    elif not path.exists():
        # The one authoritative repo-gone signal: a configured repo dir absent.
        sig = RepoSignals(present=False, merged_heads=None, branches=None)
    else:
        sig = RepoSignals(present=True,
                          merged_heads=merged_pr_heads(path),
                          branches=existing_branches(path))
    cache[key] = sig
    return sig


def _branch_facts(session: OpenSession, sig: RepoSignals) -> tuple[Optional[bool], Optional[bool]]:
    """(branch_merged, branch_deleted) for a session, both Optional (None =
    indeterminate). Trunk branches are never merged/deleted."""
    branch = session.branch
    if not branch or branch in _TRUNK_BRANCHES:
        return (False, False)
    merged = (branch in sig.merged_heads) if sig.merged_heads is not None else None
    if sig.branches is None:
        deleted = None
    else:
        deleted = branch not in sig.branches
    return (merged, deleted)


def _synthetic_done_payload(session: OpenSession, reason: str) -> dict[str, Any]:
    return {
        'repo': session.repo,
        'branch': session.branch,
        'host': socket.gethostname().lower().split('.')[0],
        'synthetic': True,
        'retired_by': 'heal_missions_card_gc',
        'retire_reason': reason,
    }


@dataclass
class RetireResult:
    retired: list[tuple[str, str]] = field(default_factory=list)   # (task_id, reason)
    kept: int = 0
    emit_failures: list[str] = field(default_factory=list)         # task_ids
    skipped_no_client: bool = False


def retire_stale_sessions(
    rows: list[dict[str, Any]],
    repo_paths: dict[str, Path],
    now: datetime,
    *,
    emit_fn: Callable[..., bool],
    dry_run: bool,
) -> RetireResult:
    """Find open desktop-session cards, classify each, and retire the stale ones
    by emitting a synthetic done. Effectful but fail-safe: an emit failure is
    recorded and the card is left open (it'll be retried next tick)."""
    res = RetireResult()
    sessions = gather_open_sessions(rows, now)
    cache: dict[str, RepoSignals] = {}
    for s in sessions:
        sig = _repo_signals(s.repo, repo_paths, cache)
        merged, deleted = _branch_facts(s, sig)
        decision = classify_session(
            s,
            repo_present=sig.present,
            branch_merged=merged,
            branch_deleted=deleted,
            idle_seconds=s.idle_seconds,
        )
        if decision.action != 'retire':
            res.kept += 1
            continue
        if dry_run:
            res.retired.append((s.task_id, decision.reason + ' (dry-run)'))
            continue
        ok = emit_fn(
            event_type=EVENT_DONE,
            agent=DESKTOP_AGENT,
            task_id=s.task_id,
            payload=_synthetic_done_payload(s, decision.reason),
            ts=now.isoformat(),
        )
        if ok:
            res.retired.append((s.task_id, decision.reason))
        else:
            res.emit_failures.append(s.task_id)
            log(f'session {s.task_id}: synthetic-done emit failed — left open for next tick')
    return res


# ---------- captures.json (read / age / atomic write) — fail-safe ----------


def read_captures_registry(path: Path) -> Optional[dict[str, Any]]:
    """Load captures.json as a registry dict. Missing file → fresh empty
    registry. Malformed → None (caller skips aging + commit; the write path
    never appends onto a corrupt file). Mirrors dashboard_api._read_captures_registry
    but returns None instead of raising, so a bad tick reports rather than
    crashes (§ 6 fail-safe)."""
    if not path.exists():
        return {'schema_version': 1, 'captures': []}
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else {'schema_version': 1, 'captures': []}
    except (OSError, json.JSONDecodeError) as e:
        log(f'captures.json malformed/unreadable ({path}): {e} — skipping aging+commit this tick')
        return None
    if not isinstance(data, dict) or not isinstance(data.get('captures'), list):
        log(f'captures.json shape invalid ({path}) — skipping aging+commit this tick')
        return None
    data.setdefault('schema_version', 1)
    return data


def age_parked_captures(registry: dict[str, Any], now: datetime,
                        *, business_days: int = AGING_BUSINESS_DAYS) -> list[str]:
    """Flag `aging: true` on each parked capture whose last_touched is more than
    ``business_days`` business days old. NEVER deletes; mutates no field but the
    additive flag; idempotent (skips a capture already flagged). Returns the ids
    newly flagged this tick."""
    newly: list[str] = []
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict) or cap.get('state') != 'parked':
            continue
        if cap.get('aging') is True:
            continue
        touched = parse_iso_utc(cap.get('last_touched'))
        if touched is None:
            continue
        if business_days_between(touched, now) > business_days:
            cap['aging'] = True
            cid = cap.get('id')
            if isinstance(cid, str) and cid:
                newly.append(cid)
    return newly


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """tmp-in-same-dir + os.replace. Mirrors dashboard_api._atomic_write_captures
    so a reader never sees a partial file."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(data, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_captures(path: Path, registry: dict[str, Any]) -> None:
    """Atomic write for captures.json (thin wrapper over _atomic_write_json)."""
    _atomic_write_json(path, registry)


# ---------- missions phase reconciliation (terminal-state spec § 3.3) -------


def read_missions_registry(path: Path) -> Optional[dict[str, Any]]:
    """Load missions.json as a registry dict. Missing file → fresh empty
    registry. Malformed/wrong-shape → None (caller skips reconcile+commit; the
    write path never appends onto a corrupt file). Mirrors
    read_captures_registry's fail-safe contract."""
    if not path.exists():
        return {'schema_version': 1, 'missions': []}
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else {'schema_version': 1, 'missions': []}
    except (OSError, json.JSONDecodeError) as e:
        log(f'missions.json malformed/unreadable ({path}): {e} — skipping reconcile this tick')
        return None
    if not isinstance(data, dict) or not isinstance(data.get('missions'), list):
        log(f'missions.json shape invalid ({path}) — skipping reconcile this tick')
        return None
    data.setdefault('schema_version', 1)
    return data


@dataclass(frozen=True)
class MissionDecision:
    action: str   # 'ship' | 'keep'
    reason: str


def classify_mission(
    phase: Any, task_ids: Any, terminal_states: dict[str, str],
) -> MissionDecision:
    """Decide whether a mission flips to `shipped`. Pure.

    Ships ONLY when ALL of: the phase is reconcilable (drafting/in_flight/ready
    — `proposed`/`deferred`/`shipped` are left alone), `task_ids` is a non-empty
    list, and EVERY task_id resolves to a terminal state (MERGED/CLOSED) in
    ``terminal_states``. A task_id missing from the map counts as non-terminal.
    Any OPEN/UNKNOWN/missing task, an empty task list, or a non-reconcilable
    phase ⇒ KEEP — the conservative posture (spec § 1): never falsely retire
    live work."""
    if phase not in RECONCILABLE_MISSION_PHASES:
        return MissionDecision('keep', f'phase={phase} not reconcilable')
    if not isinstance(task_ids, list) or not task_ids:
        # Empty task list: "every entry is terminal" is vacuously true, so guard
        # explicitly — a mission with no tasks can't be terminal-state shipped.
        return MissionDecision('keep', 'no task_ids')
    non_terminal = [
        t for t in task_ids
        if terminal_states.get(t) not in tts.TERMINAL_STATES
    ]
    if non_terminal:
        return MissionDecision('keep', f'{len(non_terminal)} task(s) not terminal')
    return MissionDecision('ship', 'all-tasks-terminal')


@dataclass
class MissionReconcileResult:
    shipped: list[tuple[str, str]] = field(default_factory=list)  # (id, prior_phase)
    kept: int = 0
    probed: int = 0


def reconcile_mission_phases(
    registry: dict[str, Any],
    now: datetime,
    *,
    probe_fn: Callable[[str], str],
    dry_run: bool,
) -> MissionReconcileResult:
    """Flip every eligible mission whose work has shipped to `shipped`
    (spec § 3.3). Effectful only on the in-memory registry dict (the caller
    atomic-writes); fail-safe per-mission. `probe_fn(task_id) -> state` is the
    terminal-state probe (production: tts.task_terminal_state).

    Probing short-circuits on the first non-terminal task so a still-open
    mission costs at most one gh round-trip past its first live task."""
    res = MissionReconcileResult()
    for mission in registry.get('missions', []):
        if not isinstance(mission, dict):
            continue
        phase = mission.get('phase')
        if phase not in RECONCILABLE_MISSION_PHASES:
            continue
        task_ids = mission.get('task_ids')
        if not isinstance(task_ids, list) or not task_ids:
            res.kept += 1
            continue
        states: dict[str, str] = {}
        for tid in task_ids:
            if not (isinstance(tid, str) and tid):
                continue
            states[tid] = probe_fn(tid)
            res.probed += 1
            if states[tid] not in tts.TERMINAL_STATES:
                break  # one live/indeterminate task ⇒ KEEP; stop probing
        decision = classify_mission(phase, task_ids, states)
        if decision.action != 'ship':
            res.kept += 1
            continue
        mid = mission.get('id') if isinstance(mission.get('id'), str) else '<unknown>'
        if dry_run:
            res.shipped.append((mid, f'{phase} (dry-run)'))
            continue
        # Audit-preserved flip: record the prior phase + provenance in-file
        # (the dashboard reader tolerates extra keys) and in the log line below.
        mission['phase'] = SHIPPED_PHASE
        mission['shipped_at'] = now.isoformat()
        mission['shipped_by'] = 'heal_missions_card_gc'
        mission['prior_phase'] = phase
        res.shipped.append((mid, str(phase)))
        log(f'mission {mid}: {phase} -> shipped (all {len(task_ids)} task_id(s) '
            f'terminal: {states})')
    return res


# ---------- commit + push the captures.json delta to main (§ 6.3) ----------


def commit_and_push_captures(repo: Path, audit_msg: str) -> str:
    """Commit + push any captures.json delta to origin/main (the batched-
    durability half of § 4). Thin wrapper over _commit_and_push_path."""
    return _commit_and_push_path(
        repo, CAPTURES_REL,
        'chore(missions): GC healer — commit captures.json delta', audit_msg)


def commit_and_push_missions(repo: Path, audit_msg: str) -> str:
    """Commit + push any missions.json delta to origin/main (the durability
    half of the § 3.3 phase reconcile). Thin wrapper over _commit_and_push_path."""
    return _commit_and_push_path(
        repo, MISSIONS_REL,
        'chore(missions): GC healer — reconcile terminal mission phases', audit_msg)


def _commit_and_push_path(repo: Path, rel_path: str, commit_subject: str,
                          audit_msg: str) -> str:
    """Commit + push any delta to ``rel_path`` to origin/main. Returns a status
    token:
      'nothing'       — no delta to commit
      'wrong-branch'  — repo not on main; refuse to commit (would land on a
                        feature branch) — caller escalates
      'committed'     — committed and pushed
      'commit-failed' / 'push-failed' — git step failed; commit retained locally

    Push uses run_cycle.sh's strategy: try push; on a non-FF refusal,
    pull --rebase --autostash and retry; abort the rebase on conflict. Never
    force-pushes."""
    head = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    branch = head.stdout.strip() if head.returncode == 0 else ''
    if branch != 'main':
        return 'wrong-branch'

    clean = _git(repo, 'diff', '--quiet', '--', rel_path)
    clean_cached = _git(repo, 'diff', '--quiet', '--cached', '--', rel_path)
    # rc 0 == no diff; rc 1 == differs. Both clean → nothing to do.
    if clean.returncode == 0 and clean_cached.returncode == 0:
        return 'nothing'

    if _git(repo, 'add', rel_path).returncode != 0:
        return 'commit-failed'
    commit = _git(repo, 'commit', '-m', commit_subject, '-m', audit_msg)
    if commit.returncode != 0:
        # "nothing to commit" shouldn't happen (we checked the delta), but treat
        # any non-zero as a failed commit so we don't claim success.
        log(f'{rel_path} commit failed in {repo}: {(commit.stderr or commit.stdout).strip()[:200]}')
        return 'commit-failed'

    if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
        return 'committed'
    log(f'{rel_path} push refused (likely non-FF); attempting pull --rebase --autostash')
    rebase = _git(repo, 'pull', '--rebase', '--autostash', '-q', 'origin', 'main',
                  timeout=PUSH_TIMEOUT_SEC)
    if rebase.returncode == 0:
        if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
            return 'committed'
        return 'push-failed'
    # Rebase failed (a conflict): abort and retain the local commit, leaving the
    # branch local-ahead of origin until the next successful push. This healer is
    # the sole captures.json committer (sync no longer commits it — #409
    # follow-up), so the orphan is benign in the normal case: it is part of THIS
    # working tree's HEAD, so the next tick that finds a fresh delta pushes it
    # along, and sync's `git merge --ff-only` is a no-op when origin is merely
    # behind local. The only non-benign outcome — origin diverging so sync's
    # ff-only fails and pages — requires a rebase CONFLICT, which for a
    # single-committer linear file is near-impossible (no second writer to
    # conflict with); even then the fail-safe is a page, not data loss. So we
    # accept the rare orphan over a retry loop: retrying would re-run `pull
    # --rebase --autostash`, briefly stashing a possibly-dirty live captures.json
    # off disk — a worse trade than the rare page.
    log(f'{rel_path} rebase failed; aborting (commit retained locally)')
    _git(repo, 'rebase', '--abort')
    return 'push-failed'


# ---------- alerting ----------


def _emit_summary(retire: RetireResult, aged: list[str], commit_status: str,
                  dry_run: bool,
                  missions: Optional[MissionReconcileResult] = None,
                  missions_commit_status: str = 'nothing') -> None:
    """One audit line (log) + a low-noise digest alert when something happened;
    escalate on a hard failure. Exact counts only — never a silently truncated
    list (full ids live in the log line above)."""
    retired_ids = [tid for tid, _ in retire.retired]
    verb = 'would retire' if dry_run else 'retired'
    summary = (f'missions-card-gc: {verb} {len(retire.retired)} stale session card(s) '
               f'{retired_ids}; aged {len(aged)} parked capture(s) {aged}; '
               f'kept {retire.kept} session(s); commit={commit_status}')
    if missions is not None:
        ship_verb = 'would ship' if dry_run else 'shipped'
        shipped_ids = [mid for mid, _ in missions.shipped]
        summary += (f'; {ship_verb} {len(missions.shipped)} mission(s) {shipped_ids}; '
                    f'missions-commit={missions_commit_status}')
    if retire.emit_failures:
        summary += f'; {len(retire.emit_failures)} emit-failure(s) {retire.emit_failures}'
    if retire.skipped_no_client:
        summary += '; session-retirement skipped (supabase unavailable)'
    log(summary)

    if dry_run:
        return
    try:
        import larry_alerts  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        log(f'larry_alerts unavailable: {e}')
        return

    failure_statuses = ('wrong-branch', 'commit-failed', 'push-failed')
    hard_failure = (
        commit_status in failure_statuses
        or missions_commit_status in failure_statuses
        or bool(retire.emit_failures)
    )
    shipped_missions = bool(missions and missions.shipped)
    if hard_failure:
        fail_token = commit_status if commit_status in failure_statuses else missions_commit_status
        larry_alerts.append_alert(
            source='missions-card-gc', severity='warning',
            message=summary, subject=f'failure:{fail_token}', route='escalate')
    elif (retire.retired or aged or shipped_missions
          or commit_status == 'committed' or missions_commit_status == 'committed'):
        larry_alerts.append_alert(
            source='missions-card-gc', severity='warning',
            message=summary, subject='summary', route='digest')


# ---------- main ----------


def run_once(*, dry_run: bool,
             emit_fn: Optional[Callable[..., bool]] = None,
             events_fetcher: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None,
             mission_probe_fn: Optional[Callable[[str], str]] = None,
             now: Optional[datetime] = None) -> int:
    """One healer tick. The injectable seams (emit_fn / events_fetcher /
    mission_probe_fn / now) keep the effectful edges test-controllable;
    production resolves them from chain_event_emit + the live Supabase client +
    the shared terminal-state probe."""
    now = now or datetime.now(timezone.utc)
    repo_paths = load_repo_paths()
    if mission_probe_fn is None:
        mission_probe_fn = lambda tid: tts.task_terminal_state(tid)  # noqa: E731

    # --- phase 1: retire stale desktop-session cards ---
    if emit_fn is None or events_fetcher is None:
        import chain_event_emit  # noqa: PLC0415

        def _default_fetch() -> Optional[list[dict[str, Any]]]:
            cli = chain_event_emit._get_client()
            if cli is None:
                return None
            try:
                resp = (
                    cli.table('chain_events')
                    .select('task_id,event_type,ts,payload')
                    .eq('agent', DESKTOP_AGENT)
                    .execute()
                )
            except Exception as e:  # noqa: BLE001 — read must never crash the tick
                log(f'chain_events read failed: {type(e).__name__}: {e}')
                return None
            return list(getattr(resp, 'data', None) or [])

        emit_fn = emit_fn or chain_event_emit.emit_event
        events_fetcher = events_fetcher or _default_fetch

    try:
        rows = events_fetcher()
    except Exception as e:  # noqa: BLE001 — fail-safe
        log(f'event fetch raised: {type(e).__name__}: {e}')
        rows = None

    if rows is None:
        retire = RetireResult(skipped_no_client=True)
        log('session-retirement: chain_events unavailable — skipping phase')
    else:
        try:
            retire = retire_stale_sessions(
                rows, repo_paths, now, emit_fn=emit_fn, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
            log(f'session-retirement raised: {type(e).__name__}: {e}')
            retire = RetireResult(skipped_no_client=False)

    # --- phase 2 + 3: age parked captures, then commit the delta ---
    aged: list[str] = []
    commit_status = 'nothing'
    cap_path = captures_path(repo_paths)
    if cap_path is None:
        log('captures.json path unresolved (agent-core not in repo_paths) — skipping aging+commit')
    else:
        registry = read_captures_registry(cap_path)
        if registry is not None:
            try:
                aged = age_parked_captures(registry, now)
                if aged and not dry_run:
                    atomic_write_captures(cap_path, registry)
            except Exception as e:  # noqa: BLE001 — fail-safe
                log(f'capture-aging raised: {type(e).__name__}: {e}')
                aged = []
            if not dry_run:
                core = repo_paths.get('ourliberty-agent-core')
                if core:
                    try:
                        commit_status = commit_and_push_captures(core, _commit_audit(retire, aged))
                    except Exception as e:  # noqa: BLE001 — fail-safe
                        log(f'commit+push raised: {type(e).__name__}: {e}')
                        commit_status = 'push-failed'

    # --- phase 4: reconcile shipped mission phases (terminal-state § 3.3) ---
    missions = MissionReconcileResult()
    missions_commit_status = 'nothing'
    miss_path = missions_path(repo_paths)
    if miss_path is None:
        log('missions.json path unresolved (agent-core not in repo_paths) — skipping reconcile')
    else:
        registry = read_missions_registry(miss_path)
        if registry is not None:
            try:
                missions = reconcile_mission_phases(
                    registry, now, probe_fn=mission_probe_fn, dry_run=dry_run)
                if missions.shipped and not dry_run:
                    _atomic_write_json(miss_path, registry)
            except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                log(f'mission-reconcile raised: {type(e).__name__}: {e}')
                missions = MissionReconcileResult()
            if missions.shipped and not dry_run:
                core = repo_paths.get('ourliberty-agent-core')
                if core:
                    try:
                        missions_commit_status = commit_and_push_missions(
                            core, _missions_commit_audit(missions))
                    except Exception as e:  # noqa: BLE001 — fail-safe
                        log(f'missions commit+push raised: {type(e).__name__}: {e}')
                        missions_commit_status = 'push-failed'

    _emit_summary(retire, aged, commit_status, dry_run,
                  missions=missions, missions_commit_status=missions_commit_status)
    return 0


def _commit_audit(retire: RetireResult, aged: list[str]) -> str:
    return (f'Auto-committed by heal_missions_card_gc. '
            f'retired={len(retire.retired)} aged={len(aged)}.')


def _missions_commit_audit(missions: MissionReconcileResult) -> str:
    shipped = [f'{mid}({prior}->shipped)' for mid, prior in missions.shipped]
    return (f'Auto-committed by heal_missions_card_gc. '
            f'terminal-state reconcile shipped={len(missions.shipped)} {shipped}.')


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='heal_missions_card_gc.py',
        description='Missions v2 GC healer: retire stale desktop-session cards, '
                    'age parked captures, commit the captures.json delta.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report what WOULD be retired/aged; emit nothing, write nothing, '
             'commit nothing.')
    args = parser.parse_args(argv)

    if _kill_switch_path().exists():
        log('KILLED_BY_SWITCH: healers.disabled present, exiting')
        return 0

    mode = 'DRY-RUN' if args.dry_run else 'LIVE'
    log(f'Starting missions-card GC ({mode})')
    rc = run_once(dry_run=args.dry_run)
    log('Done.')
    return rc


if __name__ == '__main__':
    sys.exit(main())
