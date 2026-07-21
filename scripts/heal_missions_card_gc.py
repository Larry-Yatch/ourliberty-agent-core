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

  2b. AUTHOR THE MEANING LAYER (Phase 4.1 spec § 3). After aging and before the
     commit, run the Narrator's author sweep over the in-memory registry —
     missions_narrator.author_captures_in_registry briefs every capture whose
     needs_briefing() is true, bounded by NARRATOR_MAX_PER_TICK so an LLM-slow
     tick can't run unboundedly (the remainder briefs next tick; idempotent).
     The healer is the SOLE captures.json committer: the narrator only mutates
     the dict, and the single write+commit below batches its briefing delta.
     Per-capture author errors are skipped, never fatal.

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
import re
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
# completeness PR-3 R2: a mission all of whose PR-backed tasks are terminal but at
# least one is CLOSED-unmerged (abandoned, not merged) is RETIRED, not shipped —
# recording abandoned work as "shipped" corrupts the mission ledger. Retired
# missions also raise one needs-attention surface. all-MERGED still ships.
RETIRED_PHASE = 'retired'

# task_ids the orchestrator never opens a PR for — DAG-preflight routing signals,
# inter-agent notifies, and code-review handoffs. They can never resolve to a
# MERGED/CLOSED PR, so requiring them to be terminal makes a mission's
# shipped-detection permanently unsatisfiable (the clarify-round-visibility
# class: a `dag-preflight-*` id sat in task_ids and blocked the all-terminal gate
# forever). They are excluded from the ship gate — only PR-backed ids count.
REVIEW_SHAPED_TASK_ID_PREFIXES = ('dag-preflight-', 'notify-', 'review-')

# A reconcilable-phase mission with NO probeable (PR-backed) task_id can't be
# auto-shipped — there's nothing to verify terminal, so it stays manual reconcile
# (fundamentally unprobeable). Once it has lingered past this grace since
# `created`, it's flagged on the audit line so it surfaces for manual reconcile
# instead of sitting on the board silently forever.
EMPTY_PROBEABLE_MISSION_GRACE_DAYS = 14

# Phase S (S3) completion states. A parked card whose spawned work reaches
# verified-merged (belt-and-suspenders) is routed by its risk: a `safe` card
# auto-closes to `done` (with a shipped note), a `medium`/`careful` (or
# un-briefed) card moves to `review_close` carrying a Narrator closeout briefing
# and awaits Larry's ack. Both are terminal for the parked lane, so the
# completion reconcile's own `state == 'parked'` guard makes it idempotent.
COMPLETED_STATE_DONE = 'done'
COMPLETED_STATE_REVIEW = 'review_close'
COMPLETED_BY = 'heal_missions_card_gc'

# Match the trailing PR number in a GitHub PR URL (…/pull/<N>).
_PR_NUM_RE = re.compile(r'/pull/(\d+)')

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
        # --limit 100 (was 1000): this merged-history query only needs recent
        # merged heads to decide retirement, and the full 1000-deep page is one
        # of the priciest GraphQL calls in the chain (gh-api-burn phase 1).
        res = subprocess.run(
            ['gh', 'pr', 'list', '--state', 'merged', '--limit', '100',
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


# NOTE: the `parked_capture` chain_events projection (approval-sync Phase 3a
# §3a.1) was RETIRED 2026-07-01. The dashboard dropped the Parked lane from the
# Needs-You surface (ourliberty-dashboard#101) — the parked backlog lives only on
# the Missions tab, which reads captures.json directly, not this projection — so
# nothing consumes `parked_capture` rows anymore. `project_parked_captures` and
# its per-tick `reconcile_open_events('parked_capture', ...)` call were removed
# so we stop writing/clearing rows for a lane no reader renders. Already-shipped
# open rows are cleared once by scripts/retire_parked_capture_rows.py. The
# `sequence_needs_you` projection (build_sequence_advancer) is unaffected.


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Delegates to the shared guarded atomic_io writer. Byte-identical to the
    prior inline writer: pretty JSON (indent=2) + trailing newline."""
    import atomic_io
    atomic_io.atomic_write_json(path, data, trailing_newline=True)


def atomic_write_captures(path: Path, registry: dict[str, Any]) -> None:
    """Atomic write for captures.json (thin wrapper over _atomic_write_json)."""
    _atomic_write_json(path, registry)


def _merge_registry_deltas(fresh: dict[str, Any],
                           before_by_id: dict[str, dict[str, Any]],
                           worked: dict[str, Any], *, list_key: str) -> int:
    """Apply only the per-item field changes THIS tick made (``worked`` vs the
    read-time snapshot ``before_by_id``) onto a freshly re-read ``fresh`` registry,
    matching items in ``fresh[list_key]`` by ``id``. Returns the count of items
    whose delta was applied.

    Why field-level, not whole-registry replace: the Narrator sweep can span
    minutes of LLM round-trips, during which a separate process (the dashboard
    API, the orphan-autoregister drain) may ingest new items or change an item's
    state. Replacing the whole file with the stale in-memory copy would silently
    lose those writes (§ 2 lost-update class). By touching ONLY the keys we
    changed — never an item we didn't author, and never an item missing from the
    fresh file — concurrent writes survive. An item another writer moved out from
    under us mid-briefing keeps its new state and merely carries a now-stale
    briefing that the next sweep regenerates."""
    fresh_by_id = {
        c['id']: c for c in fresh.get(list_key, [])
        if isinstance(c, dict) and c.get('id')
    }
    applied = 0
    for cap in worked.get(list_key, []):
        if not isinstance(cap, dict):
            continue
        cid = cap.get('id')
        if not cid:
            continue
        before = before_by_id.get(cid)
        if before is None:
            continue  # not present at read time — we never authored onto it
        set_fields = {k: v for k, v in cap.items()
                      if k not in before or before[k] != v}
        removed = [k for k in before if k not in cap]
        if not set_fields and not removed:
            continue  # untouched this tick — leave the fresh copy alone
        target = fresh_by_id.get(cid)
        if target is None:
            continue  # gone from the fresh file — drop our stale delta, don't re-add
        for k, v in set_fields.items():
            target[k] = v
        for k in removed:
            target.pop(k, None)
        applied += 1
    return applied


def _merge_capture_deltas(fresh: dict[str, Any],
                          before_by_id: dict[str, dict[str, Any]],
                          worked: dict[str, Any]) -> int:
    """Capture-registry view of `_merge_registry_deltas` (matches by capture id on
    the ``captures`` list). A capture the dashboard moved out of ``parked``
    mid-briefing keeps its new state and merely carries a now-stale briefing that
    ``needs_briefing`` regenerates next tick."""
    return _merge_registry_deltas(fresh, before_by_id, worked, list_key='captures')


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
    action: str   # 'ship' | 'retire' | 'keep'
    reason: str


def is_review_shaped_task_id(task_id: Any) -> bool:
    """True if `task_id` is a non-PR / review-shaped id the orchestrator never
    opens a PR for (DAG-preflight routing signals, inter-agent notifies, code
    reviews). Such ids never resolve to a merged PR, so they must NOT count
    toward the all-PR-backed-task-terminal ship gate — otherwise one review id
    would permanently block a mission's shipped-detection."""
    return (isinstance(task_id, str)
            and task_id.startswith(REVIEW_SHAPED_TASK_ID_PREFIXES))


def probeable_task_ids(task_ids: Any) -> list[str]:
    """The PR-backed task_ids worth probing for a terminal state: non-empty
    string ids that are NOT review-shaped. A mission ships when all of THESE are
    terminal; review-shaped ids are ignored entirely (they can never be
    terminal)."""
    if not isinstance(task_ids, list):
        return []
    return [t for t in task_ids
            if isinstance(t, str) and t and not is_review_shaped_task_id(t)]


def classify_mission(
    phase: Any, task_ids: Any, terminal_states: dict[str, str],
) -> MissionDecision:
    """Decide whether a mission flips to `shipped`. Pure.

    Terminal ONLY when ALL of: the phase is reconcilable (drafting/in_flight/ready
    — `proposed`/`deferred`/`shipped` are left alone), the mission has at least
    one PR-backed (non-review-shaped) task_id, and EVERY PR-backed task_id
    resolves to a terminal state (MERGED/CLOSED) in ``terminal_states``. A
    PR-backed task_id missing from the map counts as non-terminal. Review-shaped
    ids (`dag-preflight-*`/`notify-*`/`review-*`) are ignored — they never back a
    PR, so requiring them terminal would block shipping permanently. Any
    OPEN/UNKNOWN/missing PR-backed task, no probeable task at all (empty list or
    only review-shaped ids), or a non-reconcilable phase ⇒ KEEP — the
    conservative posture (spec § 1): never falsely retire live work.

    Once all-terminal, the terminal MIX decides the outcome (PR-3 R2): every task
    MERGED ⇒ SHIP; at least one CLOSED-unmerged (abandoned) ⇒ RETIRE — abandoned
    work must never be recorded as shipped."""
    if phase not in RECONCILABLE_MISSION_PHASES:
        return MissionDecision('keep', f'phase={phase} not reconcilable')
    probeable = probeable_task_ids(task_ids)
    if not probeable:
        # Nothing PR-backed to verify: "every entry is terminal" would be
        # vacuously true, so guard explicitly — a mission with no probeable task
        # (empty, or only review-shaped ids) can't be terminal-state shipped; it
        # surfaces via the reconcile flag for manual review instead.
        return MissionDecision('keep', 'no probeable task_ids')
    non_terminal = [
        t for t in probeable
        if terminal_states.get(t) not in tts.TERMINAL_STATES
    ]
    if non_terminal:
        return MissionDecision('keep', f'{len(non_terminal)} task(s) not terminal')
    closed_unmerged = [t for t in probeable if terminal_states.get(t) == tts.CLOSED]
    if closed_unmerged:
        return MissionDecision(
            'retire',
            f'{len(closed_unmerged)} task(s) CLOSED-unmerged (abandoned): '
            f'{closed_unmerged}')
    return MissionDecision('ship', 'all-pr-backed-tasks-merged')


@dataclass
class MissionReconcileResult:
    shipped: list[tuple[str, str]] = field(default_factory=list)  # (id, prior_phase)
    retired: list[tuple[str, str]] = field(default_factory=list)  # (id, reason) — R2
    flagged: list[tuple[str, str]] = field(default_factory=list)  # (id, reason)
    kept: int = 0
    probed: int = 0


def _maybe_flag_unprobeable_mission(
    res: MissionReconcileResult, mid: str, mission: dict[str, Any],
    task_ids: Any, now: datetime,
) -> None:
    """Flag a reconcilable mission that has no PR-backed task_id to probe once it
    has lingered past EMPTY_PROBEABLE_MISSION_GRACE_DAYS since `created`. Surfaces
    it on the audit line for manual reconcile; it never auto-ships (unprobeable).
    A missing/unparseable `created` stamp can't be aged, so it stays silent."""
    created = parse_iso_utc(mission.get('created'))
    if created is None:
        return
    if now - created < timedelta(days=EMPTY_PROBEABLE_MISSION_GRACE_DAYS):
        return
    n = len(task_ids) if isinstance(task_ids, list) else 0
    reason = (f'{(now - created).days}d in reconcilable phase with no probeable '
              f'task_id ({n} review-shaped/empty)')
    res.flagged.append((mid, reason))
    log(f'mission {mid}: no probeable task_id past '
        f'{EMPTY_PROBEABLE_MISSION_GRACE_DAYS}d grace — flag for manual reconcile '
        f'({reason})')


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
        mid = mission.get('id') if isinstance(mission.get('id'), str) else '<unknown>'
        probeable = probeable_task_ids(task_ids)
        if not probeable:
            # No PR-backed task to verify terminal (empty, or only review-shaped
            # ids) — can't auto-ship (stays manual reconcile). Flag it if it has
            # lingered past the grace so it surfaces instead of sitting silently.
            res.kept += 1
            _maybe_flag_unprobeable_mission(res, mid, mission, task_ids, now)
            continue
        states: dict[str, str] = {}
        for tid in probeable:
            states[tid] = probe_fn(tid)
            res.probed += 1
            if states[tid] not in tts.TERMINAL_STATES:
                break  # one live/indeterminate task ⇒ KEEP; stop probing
        decision = classify_mission(phase, task_ids, states)
        if decision.action == 'retire':
            # R2: all-terminal but at least one CLOSED-unmerged (abandoned work).
            # Flip to `retired`, NOT `shipped`, and surface for attention.
            if dry_run:
                res.retired.append((mid, f'{decision.reason} (dry-run)'))
                continue
            mission['phase'] = RETIRED_PHASE
            mission['retired_at'] = now.isoformat()
            mission['retired_by'] = 'heal_missions_card_gc'
            mission['retired_reason'] = decision.reason
            mission['prior_phase'] = phase
            res.retired.append((mid, decision.reason))
            log(f'mission {mid}: {phase} -> retired ({decision.reason}; '
                f'states={states})')
            continue
        if decision.action != 'ship':
            res.kept += 1
            continue
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
        log(f'mission {mid}: {phase} -> shipped (all {len(probeable)} PR-backed '
            f'task_id(s) merged: {states})')
    return res


# ---------- Phase S (S3): completion — auto-close safe / closeout risky -------


def pr_number_from_url(pr_url: Any) -> Optional[str]:
    """The trailing PR number from a GitHub PR URL (…/pull/541 → '541'), or None
    if the value isn't a URL carrying one."""
    if not isinstance(pr_url, str):
        return None
    m = _PR_NUM_RE.search(pr_url)
    return m.group(1) if m else None


def shipped_note(pr_url: Any) -> str:
    """The "shipped in PR #X" note stamped on an auto-closed card (spec § 3 S3).
    Degrades to a PR-less phrasing when the URL is missing/unparseable so the
    note is always operator-readable."""
    n = pr_number_from_url(pr_url)
    return f'shipped in PR #{n}' if n else 'shipped (linked work merged)'


@dataclass(frozen=True)
class CompletionDecision:
    action: str   # 'auto_close' | 'closeout' | 'keep'
    reason: str


def classify_completion(
    capture: dict[str, Any], *, verified_merged: bool,
) -> CompletionDecision:
    """Route a parked card on its spawned work's verified-merge (spec § 3 S3).
    Pure — the caller resolves the belt-and-suspenders gate into ``verified_merged``.

      not verified            ⇒ keep   (retry next tick; never close early)
      verified + risk == safe ⇒ auto_close (state → done + shipped note)
      verified + medium/      ⇒ closeout   (Narrator closeout + review_close)
        careful/un-briefed

    An un-briefed card (no risk yet) routes to closeout, not auto-close — fail
    toward Larry's review rather than silently closing a card we never classified
    (mirrors the Narrator's fail-toward-caution posture)."""
    if not verified_merged:
        return CompletionDecision('keep', 'not-verified-merged')
    if capture.get('risk') == 'safe':
        return CompletionDecision('auto_close', 'safe-verified-merged')
    return CompletionDecision('closeout', f'{capture.get("risk") or "unbriefed"}-verified-merged')


@dataclass
class CompletionResult:
    closed: list[tuple[str, str]] = field(default_factory=list)   # (id, note)
    closeouts: list[str] = field(default_factory=list)            # ids → review_close
    kept: int = 0

    @property
    def changed(self) -> bool:
        """True iff this tick mutated a capture (so the caller writes+commits)."""
        return bool(self.closed or self.closeouts)


def reconcile_completed_cards(
    registry: dict[str, Any],
    now: datetime,
    *,
    verify_fn: Callable[[str, Optional[str]], tuple[bool, Optional[str]]],
    closeout_fn: Callable[[dict[str, Any], Optional[str], datetime], dict[str, Any]],
    dry_run: bool,
) -> CompletionResult:
    """Phase S S3: close the loop on parked cards whose spawned work has merged.

    For each parked capture carrying a `spawned.task_id` (S-1's delegate join
    key), resolve the belt-and-suspenders verified-merge gate via ``verify_fn``
    (production: chain_events `auto_merge` AND `gh pr view MERGED`, both required)
    and route by risk:

      * safe        → state → `done`, stamped with a "shipped in PR #X" note.
      * medium/     → ``closeout_fn`` authors a Narrator closeout briefing onto
        careful/       the card and the state moves to `review_close`, awaiting
        un-briefed     Larry's ack.

    Effectful ONLY on the in-memory registry dict — the caller (run_once) owns
    the single atomic write + git commit, so this never becomes a second
    captures.json committer (single-committer invariant; the same reuse the
    Narrator sweep relies on). Fail-safe per capture: a verify/author error keeps
    the card (retried next tick). Idempotent: a closed/review_close card is no
    longer `parked`, so it is skipped on every subsequent tick.

    ``verify_fn(task_id, dispatched_at) -> (verified_merged, pr_url)``; ``pr_url``
    is threaded into the shipped note and the closeout so neither has to refetch."""
    res = CompletionResult()
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict) or cap.get('state') != 'parked':
            continue
        spawned = cap.get('spawned')
        if not isinstance(spawned, dict):
            continue
        task_id = spawned.get('task_id')
        if not (isinstance(task_id, str) and task_id):
            continue
        dispatched_at = (spawned.get('stamped_at')
                         if isinstance(spawned.get('stamped_at'), str) else None)
        try:
            verified, pr_url = verify_fn(task_id, dispatched_at)
        except Exception as e:  # noqa: BLE001 — per-capture fail-safe
            log(f'completion: verify failed for {task_id}: {type(e).__name__}: {e} — keep')
            res.kept += 1
            continue
        decision = classify_completion(cap, verified_merged=verified)
        if decision.action == 'keep':
            res.kept += 1
            continue
        cid = cap.get('id') if isinstance(cap.get('id'), str) else '<unknown>'
        if decision.action == 'auto_close':
            note = shipped_note(pr_url)
            if dry_run:
                res.closed.append((cid, note + ' (dry-run)'))
                continue
            cap['state'] = COMPLETED_STATE_DONE
            cap['closed_at'] = now.isoformat()
            cap['closed_by'] = COMPLETED_BY
            cap['shipped_note'] = note
            if pr_url:
                cap['shipped_pr_url'] = pr_url
            cap.pop('aging', None)  # a closed card is no longer an aging nudge
            res.closed.append((cid, note))
            log(f'completion: capture {cid} (safe) -> done [{note}]')
            continue
        # closeout (medium / careful / un-briefed)
        if dry_run:
            res.closeouts.append(cid)
            continue
        try:
            fields = closeout_fn(cap, pr_url, now)
        except Exception as e:  # noqa: BLE001 — per-capture fail-safe
            log(f'completion: closeout author failed for {cid}: '
                f'{type(e).__name__}: {e} — keep')
            res.kept += 1
            continue
        cap.update(fields)
        cap['state'] = COMPLETED_STATE_REVIEW
        cap['awaiting_ack'] = True
        if pr_url:
            cap['shipped_pr_url'] = pr_url
        cap.pop('aging', None)
        res.closeouts.append(cid)
        log(f'completion: capture {cid} ({cap.get("risk") or "unbriefed"}) '
            f'-> review_close (closeout authored)')
    return res


def _fetch_task_events(client: Any, task_id: str) -> list[dict[str, Any]]:
    """Best-effort fetch of a task's chain_events (newest first). Any failure
    yields [] so pr_url resolution degrades to "no PR" rather than crashing the
    tick. Mirrors missions_narrator.fetch_capture_events's fail-safe contract."""
    if client is None or not task_id:
        return []
    try:
        resp = (
            client.table('chain_events')
            .select('event_type,task_id,pr_url,ts,payload')
            .eq('task_id', task_id)
            .order('ts', desc=True)
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — context fetch is best-effort
        log(f'completion: chain_events fetch for {task_id} failed: '
            f'{type(e).__name__}: {e}')
        return []
    return list(getattr(resp, 'data', None) or [])


def _pr_url_from_events(events: list[dict[str, Any]]) -> Optional[str]:
    """First non-null top-level `pr_url` across ``events`` (the column
    dashboard_api.summarize_task_events reads), or None. Events are newest-first,
    so this returns the most recent PR association."""
    for e in events:
        if not isinstance(e, dict):
            continue
        pr_url = e.get('pr_url')
        if isinstance(pr_url, str) and pr_url:
            return pr_url
    return None


def _default_completion_verify_fn() -> Callable[[str, Optional[str]], tuple[bool, Optional[str]]]:
    """Build the production belt-and-suspenders verify_fn (spec § 3 S3): a card's
    spawned work is verified-merged ONLY when BOTH legs agree — chain_events has
    an `auto_merge` row with a merged outcome (since dispatch) AND `gh pr view`
    reports the PR MERGED. Either leg failing/indeterminate ⇒ NOT verified (keep,
    retry next tick). The pr_url is resolved from chain_events and threaded back
    so the shipped note / closeout don't refetch it.

    Lazy imports (build_sequence_advancer, chain_event_emit) keep stdlib-only
    import at module load — the advancer's module-level is constants-only, so this
    is side-effect-free to import."""
    import build_sequence_advancer as bsa  # noqa: PLC0415
    import chain_event_emit  # noqa: PLC0415

    def _verify(task_id: str, dispatched_at: Optional[str]) -> tuple[bool, Optional[str]]:
        client = chain_event_emit._get_client()
        events = _fetch_task_events(client, task_id)
        pr_url = _pr_url_from_events(events)
        # Leg 1: chain_events auto_merge says merged (since dispatch).
        if not bsa.chain_event_says_merged(client, task_id, dispatched_at):
            return (False, pr_url)
        # Leg 2: gh pr view says MERGED. None (gh failure/indeterminate) is a
        # soft fail — wait rather than close on a single leg.
        gh_merged = bsa.gh_pr_says_merged(pr_url) if pr_url else None
        return (gh_merged is True, pr_url)

    return _verify


def _default_completion_closeout_fn() -> Callable[[dict[str, Any], Optional[str], datetime], dict[str, Any]]:
    """Build the production closeout_fn (spec § 3 S3): the Narrator authors a
    plain-language closeout briefing (what we did · the outcome · anything to
    know) for a medium/careful card, returned as a field dict the reconcile merges
    onto the capture. Lazy import (no narrator import at module load; the
    narrator→healer import is the only static edge, so importing it here is
    cycle-free)."""
    from missions_narrator import author_closeout  # noqa: PLC0415
    return author_closeout


# ---------- Phase S (S4): failure/blocked rings back loud -------------------

# The dashboard frontend base — a doorbell ping deep-links back to the card (§ 9).
_DASHBOARD_BASE = 'https://dashboard.ourliberty.dev'

# Linked-work phases that count as in-flight (spec § 3 S7). Mirrors the
# dashboard's in-flight gate (dashboard_api._IN_FLIGHT_PHASES) so a deferred
# pause/drop applies on the exact safe-stop boundary the board shows.
_IN_FLIGHT_PHASES = ('in_flight', 'awaiting_merge')

# A deferred operator action the healer applies after a safe stop (spec § 3 S7).
_DEFERRED_ACTIONS = ('drop', 'snooze')


def card_deep_link(capture_id: str) -> str:
    """The dashboard deep-link a doorbell ping carries back to the card (§ 9)."""
    return f'{_DASHBOARD_BASE}/missions?card={capture_id}'


@dataclass
class FailureResult:
    rung: list[tuple[str, str]] = field(default_factory=list)   # (id, reason)
    kept: int = 0

    @property
    def changed(self) -> bool:
        """True iff this tick stamped a capture (so the caller writes+commits)."""
        return bool(self.rung)


def reconcile_failed_cards(
    registry: dict[str, Any],
    now: datetime,
    *,
    failure_fn: Callable[[str], Optional[str]],
    ring_fn: Callable[..., dict[str, Any]],
    dry_run: bool,
) -> FailureResult:
    """Phase S S4: surface failed/blocked linked work as a loud blocked-on-you
    doorbell on the parked card, carrying a plain-English reason.

    For each parked capture carrying a `spawned.task_id`, ``failure_fn`` resolves
    the chain_events terminal-failure signal (production:
    build_sequence_advancer.chain_event_says_failed — recognizes
    mirror_revision_exhausted / mirror_emergency_halt / forge_reject) into a
    reason string, or None when the work is healthy. On a reason, ``ring_fn``
    (production: missions_doorbell.ring_doorbell) rings blocked-on-you LOUD
    (route escalate; severity critical for a `careful` card) through the shared
    larry_alerts + alert_triage_state rails — no bespoke notifier (spec § 3 reuse
    map / § 9).

    Idempotent: the ring stamps `failure_signaled` on the capture, and a stamped
    card is skipped on every later tick (record_triage would otherwise rewrite
    the triage row each pass). Effectful ONLY on the in-memory registry — the
    caller (run_once) owns the single write+commit (single-committer invariant).
    Fail-safe per capture: a failure_fn / ring_fn error keeps the card (retried
    next tick). The card STAYS parked — failure surfaces for Larry, it does not
    close the card."""
    res = FailureResult()
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict) or cap.get('state') != 'parked':
            continue
        if cap.get('failure_signaled'):  # already rung — idempotent skip
            continue
        spawned = cap.get('spawned')
        if not isinstance(spawned, dict):
            continue
        task_id = spawned.get('task_id')
        if not (isinstance(task_id, str) and task_id):
            continue
        try:
            reason = failure_fn(task_id)
        except Exception as e:  # noqa: BLE001 — per-capture fail-safe
            log(f'failure: detect failed for {task_id}: {type(e).__name__}: {e} — keep')
            res.kept += 1
            continue
        if not reason:
            res.kept += 1
            continue
        cid = cap.get('id') if isinstance(cap.get('id'), str) else '<unknown>'
        if dry_run:
            res.rung.append((cid, reason + ' (dry-run)'))
            continue
        try:
            ring_fn(
                capture_id=cid,
                blocked=True,
                risk=cap.get('risk'),
                title=cap.get('title'),
                deep_link=card_deep_link(cid),
                detail=reason,
            )
        except Exception as e:  # noqa: BLE001 — per-capture fail-safe
            log(f'failure: ring failed for {cid}: {type(e).__name__}: {e} — keep')
            res.kept += 1
            continue
        cap['failure_signaled'] = {
            'reason': reason,
            'at': now.isoformat(),
            'by': COMPLETED_BY,
        }
        res.rung.append((cid, reason))
        log(f'failure: capture {cid} rang blocked-on-you [{reason}]')
    return res


def _default_failure_fn() -> Callable[[str], Optional[str]]:
    """Production failure detector (spec § 3 S4): the chain_events terminal-failure
    signal for a task. Lazy imports keep module-load stdlib-only (the advancer's
    module-level is constants-only, so this is side-effect-free to import)."""
    import build_sequence_advancer as bsa  # noqa: PLC0415
    import chain_event_emit  # noqa: PLC0415

    def _failed(task_id: str) -> Optional[str]:
        client = chain_event_emit._get_client()
        return bsa.chain_event_says_failed(client, task_id)

    return _failed


def _default_ring_fn() -> Callable[..., dict[str, Any]]:
    """Production doorbell ring (spec § 3 S4 / § 9): missions_doorbell.ring_doorbell,
    which sits on the shared larry_alerts + alert_triage_state rails."""
    import missions_doorbell  # noqa: PLC0415
    return missions_doorbell.ring_doorbell


# ---------- G3 (completeness-pr2): terminal-state backstop reconcile ----------
#
# S3 (reconcile_completed_cards) closes a parked card only on the belt-and-
# suspenders verified-merge signal — a chain_events `auto_merge` row AND `gh pr
# view MERGED`. S4 (reconcile_failed_cards) surfaces only a RECOGNIZED
# chain_events failure (mirror_revision_exhausted / mirror_emergency_halt /
# forge_reject). A spawned capture whose PR reached a terminal state NEITHER leg
# observes — a merge that never emitted an `auto_merge` event, or a PR
# closed-without-merging (abandoned/superseded) — is stamped by neither and sits
# `parked` forever, violating the terminal-state-reconciliation invariant (no
# in-flight record outlives its work's terminal state). This backstop probes the
# shared conservative gh-only `task_terminal_state` ground truth and stamps the
# card on a MERGED/CLOSED verdict. UNKNOWN/OPEN change NOTHING (KEEP): a failed
# delegate that never opened a PR probes UNKNOWN and MUST surface as still-open,
# never silently close (spec § 1(b) — the `delegate-cap-slice-…` shape).

TERMINAL_BACKSTOP_BY = 'heal_missions_card_gc:terminal-backstop'


@dataclass
class TerminalReconcileResult:
    completed: list[tuple[str, str]] = field(default_factory=list)      # (id, note) → done
    closeouts: list[str] = field(default_factory=list)                  # id → review_close
    closed_failed: list[tuple[str, str]] = field(default_factory=list)  # (id, reason) → rung
    kept: int = 0

    @property
    def changed(self) -> bool:
        """True iff this tick stamped a capture (so the caller writes+commits)."""
        return bool(self.completed or self.closeouts or self.closed_failed)


def reconcile_terminal_captures(
    registry: dict[str, Any],
    now: datetime,
    *,
    terminal_fn: Callable[[str], str],
    ring_fn: Callable[..., dict[str, Any]],
    dry_run: bool,
) -> TerminalReconcileResult:
    """Backstop reconcile (completeness-pr2 G3): stamp a parked capture whose
    spawned work reached an identity-grade terminal state that S3/S4 missed.

    For each parked capture carrying a `spawned.task_id`, probe
    ``terminal_fn(task_id)`` (production: tts.task_terminal_state — the shared,
    conservative gh-only probe: OPEN wins, any ambiguity → UNKNOWN) and route:

      * MERGED → stamp `spawned.outcome='merged'` and complete the card by risk
        (classify_completion: safe → `done`, else → `review_close`) — the S3
        completion the belt-and-suspenders gate never saw.
      * CLOSED → stamp `spawned.outcome='closed'` and ring the loud
        blocked-on-you doorbell (the work was abandoned/rejected); the card STAYS
        parked — failure surfaces for Larry, it never auto-closes (mirrors S4).
      * OPEN / UNKNOWN → KEEP, no mutation. UNKNOWN is the failed-delegate shape
        that must surface as still-open, never silently close (spec § 1(b)).

    Runs AFTER S3/S4 so this only sees cards those passes KEPT; guards on
    `state=='parked'`, an absent `spawned.outcome`, and no `failure_signaled`, so
    a stamped card is skipped on every later tick (idempotent). Stamps the
    outcome by REPLACING the top-level `spawned` key with a copy carrying
    `outcome` (never an in-place nested mutation) so the lost-update delta merge
    in run_once detects and persists it — the same top-level-keys-only discipline
    S3/S4 follow. Effectful ONLY on the in-memory registry — the caller (run_once)
    owns the single write+commit (single-committer invariant). Fail-safe per
    capture: a probe/ring error keeps the card (retried next tick)."""
    res = TerminalReconcileResult()
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict) or cap.get('state') != 'parked':
            continue
        if cap.get('failure_signaled'):  # S4 already surfaced it — leave alone
            continue
        spawned = cap.get('spawned')
        if not isinstance(spawned, dict):
            continue
        if spawned.get('outcome'):  # already reconciled by this backstop — idempotent
            continue
        task_id = spawned.get('task_id')
        if not (isinstance(task_id, str) and task_id):
            continue
        try:
            state = terminal_fn(task_id)
        except Exception as e:  # noqa: BLE001 — per-capture fail-safe
            log(f'terminal-backstop: probe failed for {task_id}: '
                f'{type(e).__name__}: {e} — keep')
            res.kept += 1
            continue
        if state not in tts.TERMINAL_STATES:  # OPEN / UNKNOWN → KEEP (spec § 1(b))
            res.kept += 1
            continue
        cid = cap.get('id') if isinstance(cap.get('id'), str) else '<unknown>'
        if state == tts.MERGED:
            decision = classify_completion(cap, verified_merged=True)
            note = shipped_note(None)  # tts gives no url; degrades to PR-less phrasing
            if dry_run:
                res.completed.append((cid, note + ' (dry-run)'))
                continue
            cap['spawned'] = {**spawned, 'outcome': 'merged'}
            cap.pop('aging', None)  # a completed card is no longer an aging nudge
            cap['closed_by'] = TERMINAL_BACKSTOP_BY
            cap['shipped_note'] = note
            if decision.action == 'auto_close':
                cap['state'] = COMPLETED_STATE_DONE
                cap['closed_at'] = now.isoformat()
                res.completed.append((cid, note))
                log(f'terminal-backstop: capture {cid} (safe) -> done '
                    f'[MERGED via task_terminal_state]')
            else:
                cap['state'] = COMPLETED_STATE_REVIEW
                cap['awaiting_ack'] = True
                res.closeouts.append(cid)
                log(f'terminal-backstop: capture {cid} '
                    f'({cap.get("risk") or "unbriefed"}) -> review_close '
                    f'[MERGED via task_terminal_state]')
            continue
        # state == CLOSED — abandoned/rejected work; surface loudly, keep parked.
        reason = 'linked PR was closed without merging (terminal-state backstop)'
        if dry_run:
            res.closed_failed.append((cid, reason + ' (dry-run)'))
            continue
        try:
            ring_fn(
                capture_id=cid,
                blocked=True,
                risk=cap.get('risk'),
                title=cap.get('title'),
                deep_link=card_deep_link(cid),
                detail=reason,
            )
        except Exception as e:  # noqa: BLE001 — per-capture fail-safe
            log(f'terminal-backstop: ring failed for {cid}: '
                f'{type(e).__name__}: {e} — keep')
            res.kept += 1
            continue
        cap['spawned'] = {**spawned, 'outcome': 'closed'}
        cap['failure_signaled'] = {
            'reason': reason,
            'at': now.isoformat(),
            'by': TERMINAL_BACKSTOP_BY,
        }
        res.closed_failed.append((cid, reason))
        log(f'terminal-backstop: capture {cid} rang blocked-on-you '
            f'[CLOSED via task_terminal_state]')
    return res


def _default_terminal_fn() -> Callable[[str], str]:
    """Production terminal-state probe (completeness-pr2 G3): the shared
    conservative gh-only `task_terminal_state`. Stdlib + gh only (no Supabase),
    so this seam degrades independently of the chain_events-backed S3/S4 seams."""
    return lambda task_id: tts.task_terminal_state(task_id)  # noqa: E731


# ---------- Phase S (S7): apply a deferred pause/drop after a safe stop ------


@dataclass
class DeferredActionResult:
    applied: list[tuple[str, str]] = field(default_factory=list)  # (id, action)
    kept: int = 0

    @property
    def changed(self) -> bool:
        """True iff this tick applied a deferred action (so the caller writes)."""
        return bool(self.applied)


def apply_pending_action(cap: dict[str, Any], now: datetime) -> Optional[str]:
    """Apply a capture's recorded `pending_action` (drop|snooze), mirroring the
    dashboard handlers' own state writes, and clear the marker. Returns the
    applied action name, or None when there's nothing applicable."""
    pending = cap.get('pending_action')
    if not isinstance(pending, dict):
        return None
    action = pending.get('action')
    args = pending.get('args') if isinstance(pending.get('args'), dict) else {}
    if action == 'drop':
        cap['state'] = 'dropped'
        reason = args.get('reason')
        if isinstance(reason, str) and reason:
            cap['drop_reason'] = reason
    elif action == 'snooze':
        cap['snoozed_until'] = args.get('snoozed_until')
    else:
        return None
    cap.pop('pending_action', None)
    cap['pending_action_applied'] = {'action': action, 'at': now.isoformat()}
    return action


def reconcile_deferred_actions(
    registry: dict[str, Any],
    now: datetime,
    *,
    in_flight_fn: Callable[[str], bool],
    dry_run: bool,
) -> DeferredActionResult:
    """Phase S S7: apply a card's deferred pause/drop once its linked work stops.

    A `pending_action` was recorded by the dashboard because the action landed
    while the spawned work was in-flight (S7: never interrupt a run). Each tick
    we re-check ``in_flight_fn(task_id)`` (production: the SAME chain_events
    phase-derive the dashboard's in-flight gate uses); while it's still in-flight
    we keep waiting, and once it reaches a safe stop the recorded action is
    applied (drop → dropped; snooze → snoozed_until) and the marker cleared. A
    card with a pending action but no spawned task_id has nothing in-flight to
    wait on, so it applies immediately.

    Effectful ONLY on the in-memory registry — the caller owns the single
    write+commit. Idempotent: applying clears `pending_action`, so a card is
    processed once. Fail-safe per capture: an in_flight_fn error keeps the card
    (waits — never applies mid-flight on an indeterminate signal)."""
    res = DeferredActionResult()
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict):
            continue
        pending = cap.get('pending_action')
        if not isinstance(pending, dict) or pending.get('action') not in _DEFERRED_ACTIONS:
            continue
        cid = cap.get('id') if isinstance(cap.get('id'), str) else '<unknown>'
        spawned = cap.get('spawned')
        task_id = spawned.get('task_id') if isinstance(spawned, dict) else None
        if isinstance(task_id, str) and task_id:
            try:
                still_in_flight = in_flight_fn(task_id)
            except Exception as e:  # noqa: BLE001 — per-capture fail-safe
                log(f'deferred: in-flight probe failed for {task_id}: '
                    f'{type(e).__name__}: {e} — keep')
                res.kept += 1
                continue
            if still_in_flight:
                res.kept += 1
                continue
        if dry_run:
            res.applied.append((cid, f"{pending.get('action')} (dry-run)"))
            continue
        action = apply_pending_action(cap, now)
        if action is None:
            res.kept += 1
            continue
        res.applied.append((cid, action))
        log(f'deferred: capture {cid} applied {action} (linked work reached safe stop)')
    return res


def _default_in_flight_fn() -> Callable[[str], bool]:
    """Production in-flight probe (spec § 3 S7): the linked work is in-flight iff
    the SAME chain_events phase-derive the dashboard uses reports in_flight /
    awaiting_merge. Lazy imports keep module-load stdlib-only and mirror the
    dashboard's gate exactly, so a deferred action applies on the same safe-stop
    boundary the board shows. (heal_orphan_autoregister reuses dashboard_api's
    derive the same way — zero drift over re-porting the state machine.)

    A detected terminal FAILURE counts as a safe stop, not in-flight (S4<->S7):
    failed work derives as in_flight/awaiting_merge forever, so without this a
    pending pause/drop recorded against work that later fails would be stranded
    (in_flight every tick → never applied). Same recognizer the S4 ring uses
    (`build_sequence_advancer.chain_event_says_failed`), so the apply side and the
    ring side agree on what "failed" means — the deferred action applies on the
    failure boundary instead of waiting forever."""
    import build_sequence_advancer as bsa  # noqa: PLC0415
    import chain_event_emit  # noqa: PLC0415
    import dashboard_api  # noqa: PLC0415

    def _in_flight(task_id: str) -> bool:
        client = chain_event_emit._get_client()
        if bsa.chain_event_says_failed(client, task_id):
            return False
        events = _fetch_task_events(client, task_id)
        if not events:
            return False
        return dashboard_api.derive_phase_for_task(events, None) in _IN_FLIGHT_PHASES

    return _in_flight


# ---------- commit + push the captures.json delta to main (§ 6.3) ----------


def commit_and_push_captures(repo: Path, audit_msg: str) -> str:
    """Commit + push any captures.json delta to origin/main (the batched-
    durability half of § 4). Thin wrapper over _commit_and_push_path."""
    return _commit_and_push_path(
        repo, CAPTURES_REL,
        'chore(missions): GC healer — commit captures.json delta', audit_msg)


def commit_and_push_missions(repo: Path, audit_msg: str) -> str:
    """Commit + push any missions.json delta to origin/main (the durability half
    of the § 3.3 phase reconcile AND any cleanup delta the GC healer absorbs as
    the single committer). Thin wrapper over _commit_and_push_path."""
    return _commit_and_push_path(
        repo, MISSIONS_REL,
        'chore(missions): GC healer — commit missions.json delta', audit_msg)


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
                  missions_commit_status: str = 'nothing',
                  briefed: int = 0, deferred: int = 0,
                  mission_briefed: int = 0, mission_deferred: int = 0,
                  completed: Optional[CompletionResult] = None,
                  failed: Optional[FailureResult] = None,
                  deferred_actions: Optional[DeferredActionResult] = None) -> None:
    """One audit line (log) + a low-noise digest alert when something happened;
    escalate on a hard failure. Exact counts only — never a silently truncated
    list (full ids live in the log line above)."""
    retired_ids = [tid for tid, _ in retire.retired]
    verb = 'would retire' if dry_run else 'retired'
    brief_verb = 'would brief' if dry_run else 'briefed'
    summary = (f'missions-card-gc: {verb} {len(retire.retired)} stale session card(s) '
               f'{retired_ids}; aged {len(aged)} parked capture(s) {aged}; '
               f'{brief_verb} {briefed} capture(s), deferred {deferred}; '
               f'kept {retire.kept} session(s); commit={commit_status}')
    if completed is not None and completed.changed:
        close_verb = 'would close' if dry_run else 'closed'
        closed_ids = [cid for cid, _ in completed.closed]
        summary += (f'; {close_verb} {len(completed.closed)} card(s) {closed_ids}; '
                    f'{len(completed.closeouts)} closeout(s) {completed.closeouts}')
    if failed is not None and failed.changed:
        ring_verb = 'would ring' if dry_run else 'rang'
        rung_ids = [cid for cid, _ in failed.rung]
        summary += f'; {ring_verb} {len(failed.rung)} failure doorbell(s) {rung_ids}'
    if deferred_actions is not None and deferred_actions.changed:
        apply_verb = 'would apply' if dry_run else 'applied'
        applied = [f'{cid}:{act}' for cid, act in deferred_actions.applied]
        summary += (f'; {apply_verb} {len(deferred_actions.applied)} '
                    f'deferred action(s) {applied}')
    if missions is not None:
        ship_verb = 'would ship' if dry_run else 'shipped'
        shipped_ids = [mid for mid, _ in missions.shipped]
        m_brief_verb = 'would brief' if dry_run else 'briefed'
        summary += (f'; {ship_verb} {len(missions.shipped)} mission(s) {shipped_ids}; '
                    f'{m_brief_verb} {mission_briefed} funnel mission(s), '
                    f'deferred {mission_deferred}; '
                    f'missions-commit={missions_commit_status}')
        if missions.retired:
            retire_verb = 'would retire' if dry_run else 'retired'
            retired_ids = [mid for mid, _ in missions.retired]
            summary += (f'; {retire_verb} {len(missions.retired)} abandoned '
                        f'mission(s) {retired_ids} (CLOSED-unmerged, not shipped)')
        if missions.flagged:
            flagged_ids = [mid for mid, _ in missions.flagged]
            summary += (f'; flagged {len(missions.flagged)} unprobeable mission(s) '
                        f'{flagged_ids} for manual reconcile')
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
    if hard_failure:
        fail_token = commit_status if commit_status in failure_statuses else missions_commit_status
        larry_alerts.append_alert(
            source='missions-card-gc', severity='warning',
            message=summary, subject=f'failure:{fail_token}', route='escalate')

    # R2: one needs-attention surface per tick that retired abandoned missions —
    # abandoned work (CLOSED-unmerged) shouldn't vanish silently into `retired`.
    if missions is not None and missions.retired:
        retired_detail = '; '.join(f'{mid}: {reason}' for mid, reason in missions.retired)
        larry_alerts.append_alert(
            source='missions-card-gc', severity='warning',
            subject='missions-retired-abandoned',
            message=(f'{len(missions.retired)} mission(s) retired (all tasks '
                     f'terminal but at least one CLOSED-unmerged — abandoned, not '
                     f'shipped): {retired_detail}. Review whether the work should '
                     f'be re-attempted or is correctly dropped.'),
            suggested_action='Review the retired mission(s); re-open if the work is still wanted.')


# ---------- delegate-thread narrator (delegate-thread-narrator-001) ----------
#
# Give every DELEGATED capture card a running narrative in its team thread: one
# `team_to_larry` card_message per phase transition, in the team's single voice,
# describing WHAT HAPPENED. Folded into this GC tick (no new timer) because it
# needs the SAME phase resolver the dashboard reads AND the same ~10-min cadence.
#
# Three load-bearing invariants:
#   - SINGLE WRITER: this sweep NEVER writes captures.json — it only READS the
#     in-memory registry + chain_events/approval signals and EMITS card_message
#     rows. The healer's captures.json write/commit is untouched.
#   - IDEMPOTENT: the event_id is a DETERMINISTIC hash of (capture_id + phase)
#     (phase passed as the `ts` seed, NOT wall-clock), so `ignore_duplicates` on
#     event_id makes a re-tick a no-op and a skipped phase simply gets no post.
#   - HONEST: a `merged` post fires ONLY on a real merge stamp (never inferred
#     from a bare review_passed trail); a no-PR delegation never gets a merged
#     post; posts are FYI (needs_reply False) so they never ring the doorbell.

_NARRATION_ACTOR = 'beacon'
_CARD_MSG_TEAM = 'team_to_larry'
_CARD_MESSAGE_EVENT_TYPE = 'card_message'
# The (capture_id, phase) event_id disambiguator — keeps these deterministic ids
# from colliding with any real card_message that happens to share (id, phase).
_NARRATION_ID_NAMESPACE = 'delegate-thread-narrator'
# Bound the emits per tick so a huge board can't run unboundedly; the remainder
# narrates next tick (idempotent — the same posts re-plan and re-emit).
_NARRATION_MAX_PER_TICK = 40


def _narration_text(phase: str, pr_url: Optional[str]) -> str:
    """One team-voice line per narrative phase, describing WHAT HAPPENED.
    `review_passed` / `merged` carry the PR link; `merged` degrades to a PR-less
    phrasing when the merge stamp carried no URL (a no-PR linked-work merge)."""
    if phase == 'handed_off':
        return "Picked this up — we're scoping the work now."
    if phase == 'waiting_approval':
        return ("We've drafted the plan; it's waiting on your approval before "
                "we start building.")
    if phase == 'building':
        return "We're building this now."
    if phase == 'in_review':
        base = 'The build is up as a PR and under review.'
        return f'{base} {pr_url}' if pr_url else base
    if phase == 'review_passed':
        base = 'Review passed — the change is queued to merge.'
        return f'{base} {pr_url}' if pr_url else base
    if phase == 'merged':
        return f'Merged and shipped. {pr_url}' if pr_url else (
            'Merged and shipped (linked work merged).')
    if phase == 'closed_failed':
        return 'The linked PR was closed without merging — needs your eyes.'
    return ''


def _narration_event_id(capture_id: str, phase: str) -> str:
    """Deterministic per-(card, phase) id: exactly one post per phase per card,
    forever. The phase is the hash seed (NOT wall-clock ts), so a re-tick recomputes
    the SAME id and `ignore_duplicates` no-ops it."""
    from chain_event_shipper import compute_event_id  # noqa: PLC0415
    return compute_event_id(
        capture_id, _CARD_MESSAGE_EVENT_TYPE, phase,
        extra=_NARRATION_ID_NAMESPACE)


def plan_delegate_narrations(
    captures: list[dict[str, Any]],
    *,
    build_events_by_origin: Optional[dict[str, list[dict[str, Any]]]],
    open_delegate_approvals: dict[str, dict[str, Any]],
    native_build_events: Optional[dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Pure planner: for each DELEGATED capture, resolve its current narrative
    phase (via the ONE shared `dashboard_api.resolve_delegation_narrative_phase`)
    and build the post that narrates it. Returns a list of post dicts
    ``{capture_id, phase, event_id, text}`` — one per card that has a narratable
    phase. A card with phase None (nothing has happened yet) yields no post.

    No side effects, no Supabase, no captures.json — the caller emits. Because the
    event_id is deterministic on (capture_id, phase), the emit is idempotent: the
    SAME card at the SAME phase re-plans the SAME event_id every tick, and only
    the FIRST emit lands (later ones ignore_duplicates as no-ops)."""
    import dashboard_api as dash  # noqa: PLC0415 — lazy: heavy module, read-only use
    posts: list[dict[str, Any]] = []
    for cap in captures:
        if not isinstance(cap, dict):
            continue
        spawned = cap.get('spawned')
        if not (isinstance(spawned, dict) and spawned.get('kind') == 'delegate'):
            continue
        cid = cap.get('id')
        if not (isinstance(cid, str) and cid):
            continue
        origin_tid = spawned.get('task_id')
        has_open_approval = bool(
            isinstance(origin_tid, str)
            and open_delegate_approvals.get(origin_tid))
        resolved = dash.resolve_delegation_narrative_phase(
            cap,
            build_events_by_origin,
            has_open_approval=has_open_approval,
            native_build_events=native_build_events,
        )
        phase = resolved.get('narrative_phase')
        if not phase:
            continue
        text = _narration_text(phase, resolved.get('narrative_pr_url'))
        if not text:
            continue
        posts.append({
            'capture_id': cid,
            'phase': phase,
            'event_id': _narration_event_id(cid, phase),
            'text': text,
        })
    return posts


def _emit_narration_post(
    supabase_client: Any, post: dict[str, Any], now: datetime,
) -> None:
    """Upsert one FYI `team_to_larry` card_message for a post (idempotent on
    event_id). The row's `ts` is the real wall-clock time (so the thread orders
    naturally oldest-first) while the event_id is the deterministic (id, phase)
    hash — the two are decoupled on purpose."""
    from chain_event_shipper import sanitize_payload  # noqa: PLC0415
    cid = post['capture_id']
    payload = {
        'capture_id': cid,
        'direction': _CARD_MSG_TEAM,
        'text': post['text'],
        'actor': _NARRATION_ACTOR,
        # FYI narration, never a question → never rings the blocked-on-you
        # doorbell (which keys on needs_reply True). It's a chip echo, not a ping.
        'needs_reply': False,
    }
    row = {
        'event_id': post['event_id'],
        'ts': now.isoformat(),
        'agent': _NARRATION_ACTOR,
        'event_type': _CARD_MESSAGE_EVENT_TYPE,
        'task_id': cid,
        'actor': _NARRATION_ACTOR,
        'payload': sanitize_payload(payload),
    }
    supabase_client.table('chain_events').upsert(
        [row], on_conflict='event_id', ignore_duplicates=True,
    ).execute()


def author_delegate_thread_narration(
    registry: dict[str, Any],
    *,
    now: datetime,
    dry_run: bool,
    supabase_client: Any = None,
    agents_root: Optional[Path] = None,
) -> int:
    """The folded sweep (delegate-thread-narrator-001). Reads the in-memory
    captures registry + the delegation signals the dashboard reads, plans one
    card_message per delegated card's current phase, and emits the FYI posts.
    Returns the number of posts emitted (or, in dry-run, would emit).

    NEVER writes captures.json — the healer's single write+commit is untouched.
    Fail-safe: assembling inputs or emitting a post is guarded so one bad card /
    one Supabase hiccup logs + skips, never aborts the tick."""
    captures = registry.get('captures', [])
    if not isinstance(captures, list):
        return 0
    delegated = [
        c for c in captures
        if isinstance(c, dict) and isinstance(c.get('spawned'), dict)
        and c['spawned'].get('kind') == 'delegate'
        and isinstance(c['spawned'].get('task_id'), str)
        and isinstance(c.get('id'), str) and c.get('id')
    ]
    if not delegated:
        return 0

    import dashboard_api as dash  # noqa: PLC0415 — lazy: read-only helper reuse
    if supabase_client is None:
        import chain_event_emit  # noqa: PLC0415
        supabase_client = chain_event_emit._get_client()
    if supabase_client is None:
        log('delegate-thread narration: supabase unavailable — skipping sweep')
        return 0
    if agents_root is None:
        agents_root = dash._agents_root()

    try:
        spawned_ids = [c['spawned']['task_id'] for c in delegated]
        open_delegate_approvals = dash._open_delegate_approvals(
            spawned_ids, agents_root)
        dispatched_by_origin = dash._delegate_dispatched_task_ids(
            spawned_ids, agents_root)
        # Origins for the review trail: the delegate ref (`delegate-<cid>`) AND
        # the discuss key (`card-message-<cid>`) — a delegated card may ALSO have
        # been discussed, and the resolver merges whichever join(s) hit.
        trail_origin_ids = list(spawned_ids)
        _seen = set(trail_origin_ids)
        for c in delegated:
            origin = f"{dash._CARD_MESSAGE_ORIGIN_PREFIX}{c['id']}"
            if origin not in _seen:
                _seen.add(origin)
                trail_origin_ids.append(origin)
        build_events_by_origin = dash._fetch_delegation_build_events(
            supabase_client, trail_origin_ids)
        # The ledger bridge → the build's OWN (never origin-tagged) session_start,
        # which is the `building` signal for a card whose PR hasn't opened yet.
        fetch_ids = list(dispatched_by_origin.values())
        events_by_task_id = (
            dash._fetch_events_for_task_ids(supabase_client, fetch_ids)
            if fetch_ids else {})
        native_build_events = {
            origin: (events_by_task_id.get(fid) or [])
            for origin, fid in dispatched_by_origin.items()
        }
    except Exception as e:  # noqa: BLE001 — fail-safe: skip the sweep, never abort
        log(f'delegate-thread narration: input assembly failed: '
            f'{type(e).__name__}: {e} — skipping sweep')
        return 0

    try:
        posts = plan_delegate_narrations(
            delegated,
            build_events_by_origin=build_events_by_origin,
            open_delegate_approvals=open_delegate_approvals,
            native_build_events=native_build_events,
        )
    except Exception as e:  # noqa: BLE001 — fail-safe
        log(f'delegate-thread narration: planning failed: '
            f'{type(e).__name__}: {e} — skipping sweep')
        return 0

    if not posts:
        return 0
    if len(posts) > _NARRATION_MAX_PER_TICK:
        log(f'delegate-thread narration: {len(posts)} posts exceed '
            f'{_NARRATION_MAX_PER_TICK}/tick — emitting first '
            f'{_NARRATION_MAX_PER_TICK}, rest next tick (idempotent)')
        posts = posts[:_NARRATION_MAX_PER_TICK]

    if dry_run:
        for p in posts:
            log(f"delegate-thread narration (dry-run): would post "
                f"'{p['phase']}' for capture {p['capture_id']}")
        return len(posts)

    emitted = 0
    for p in posts:
        try:
            _emit_narration_post(supabase_client, p, now)
            emitted += 1
        except Exception as e:  # noqa: BLE001 — per-post fail-safe
            log(f"delegate-thread narration: emit failed for "
                f"{p['capture_id']}/{p['phase']}: {type(e).__name__}: {e}")
    log(f'delegate-thread narration: emitted {emitted}/{len(posts)} '
        f'card_message post(s)')
    return emitted


# ---------- main ----------


def run_once(*, dry_run: bool,
             emit_fn: Optional[Callable[..., bool]] = None,
             events_fetcher: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None,
             mission_probe_fn: Optional[Callable[[str], str]] = None,
             author_fn: Optional[Callable[..., tuple[int, int]]] = None,
             narrate_fn: Optional[Callable[..., int]] = None,
             mission_author_fn: Optional[Callable[..., tuple[int, int]]] = None,
             completion_verify_fn: Optional[Callable[[str, Optional[str]], tuple[bool, Optional[str]]]] = None,
             completion_closeout_fn: Optional[Callable[[dict[str, Any], Optional[str], datetime], dict[str, Any]]] = None,
             failure_fn: Optional[Callable[[str], Optional[str]]] = None,
             ring_fn: Optional[Callable[..., dict[str, Any]]] = None,
             terminal_fn: Optional[Callable[[str], str]] = None,
             in_flight_fn: Optional[Callable[[str], bool]] = None,
             state_log_fn: Optional[Callable[..., Any]] = None,
             now: Optional[datetime] = None) -> int:
    """One healer tick. The injectable seams (emit_fn / events_fetcher /
    mission_probe_fn / author_fn / completion_verify_fn / completion_closeout_fn /
    failure_fn / ring_fn / in_flight_fn / state_log_fn / now) keep the effectful edges
    test-controllable; production resolves them from chain_event_emit + the live
    Supabase client + the shared terminal-state probe + the Narrator sweep + the
    belt-and-suspenders verified-merge gate + the chain_events failure signal +
    the doorbell rails + the shared phase-derive in-flight gate + the
    work-in-flight State Log writer."""
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

    # --- phase 2 + 3: age parked captures, author the meaning layer, then
    # commit the delta. The Narrator sweep (spec § 3) runs AFTER aging and
    # BEFORE the single write+commit so the GC healer stays the SOLE
    # captures.json committer (single-committer invariant): the narrator only
    # mutates the in-memory registry, and the one atomic write + git commit
    # below batches both the aging and the briefing deltas. ---
    aged: list[str] = []
    briefed = 0
    deferred = 0
    completed = CompletionResult()
    failed = FailureResult()
    terminal_backstop = TerminalReconcileResult()
    deferred_actions = DeferredActionResult()
    commit_status = 'nothing'
    cap_path = captures_path(repo_paths)
    if cap_path is None:
        log('captures.json path unresolved (agent-core not in repo_paths) — skipping aging+commit')
    else:
        registry = read_captures_registry(cap_path)
        if registry is not None:
            # Snapshot the read-time state (shallow per capture is enough — aging
            # and the sweep replace top-level keys, never mutate nested objects in
            # place) so the pre-write merge can apply ONLY the fields this tick
            # changed onto a fresh re-read (§ 2 lost-update guard).
            before_by_id = {
                c['id']: dict(c)
                for c in registry.get('captures', [])
                if isinstance(c, dict) and c.get('id')
            }
            # --- phase S (S7): apply a deferred pause/drop whose linked work has
            # reached a safe stop. Runs FIRST so an operator's late pause/drop
            # wins the moment the work finishes — before completion might close
            # the same card (a dropped card is no longer parked, so completion's
            # state==parked guard then skips it). Production resolves the in-flight
            # gate (chain_events phase-derive) lazily. ---
            if in_flight_fn is None:
                try:
                    in_flight_fn = _default_in_flight_fn()
                except Exception as e:  # noqa: BLE001 — degrade: skip deferred this tick
                    log(f'deferred: in-flight seam unavailable: {type(e).__name__}: {e}')
            if in_flight_fn is not None:
                try:
                    deferred_actions = reconcile_deferred_actions(
                        registry, now, in_flight_fn=in_flight_fn, dry_run=dry_run)
                except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                    log(f'deferred-reconcile raised: {type(e).__name__}: {e}')
                    deferred_actions = DeferredActionResult()
            # --- phase S (S3): complete linked work — auto-close safe / closeout
            # risky. Runs BEFORE aging + the Narrator sweep so terminal work is
            # resolved first (a card closing this tick is never also aged), and so
            # the sweep's needs_briefing skip-states guard sees the new
            # done/review_close state. Production resolves the belt-and-suspenders
            # verify gate + the Narrator closeout author lazily. ---
            if completion_verify_fn is None:
                try:
                    completion_verify_fn = _default_completion_verify_fn()
                except Exception as e:  # noqa: BLE001 — degrade: skip completion this tick
                    log(f'completion: verify seam unavailable: {type(e).__name__}: {e}')
            if completion_closeout_fn is None:
                try:
                    completion_closeout_fn = _default_completion_closeout_fn()
                except Exception as e:  # noqa: BLE001 — degrade: skip completion this tick
                    log(f'completion: closeout seam unavailable: {type(e).__name__}: {e}')
            if completion_verify_fn is not None and completion_closeout_fn is not None:
                try:
                    completed = reconcile_completed_cards(
                        registry, now,
                        verify_fn=completion_verify_fn,
                        closeout_fn=completion_closeout_fn,
                        dry_run=dry_run)
                except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                    log(f'completion-reconcile raised: {type(e).__name__}: {e}')
                    completed = CompletionResult()
            # --- phase S (S4): ring the loud blocked-on-you doorbell for parked
            # cards whose linked work failed/blocked. Runs after completion (a
            # failed card never verify-merges, so completion keeps it) and before
            # aging (a card that just rang isn't also aged this tick). Production
            # resolves the chain_events failure signal + the doorbell rails
            # lazily; both seams are independent so a missing one skips only S4. ---
            if failure_fn is None:
                try:
                    failure_fn = _default_failure_fn()
                except Exception as e:  # noqa: BLE001 — degrade: skip failure this tick
                    log(f'failure: detect seam unavailable: {type(e).__name__}: {e}')
            if ring_fn is None:
                try:
                    ring_fn = _default_ring_fn()
                except Exception as e:  # noqa: BLE001 — degrade: skip failure this tick
                    log(f'failure: ring seam unavailable: {type(e).__name__}: {e}')
            if failure_fn is not None and ring_fn is not None:
                try:
                    failed = reconcile_failed_cards(
                        registry, now,
                        failure_fn=failure_fn, ring_fn=ring_fn, dry_run=dry_run)
                except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                    log(f'failure-reconcile raised: {type(e).__name__}: {e}')
                    failed = FailureResult()
            # --- G3 (completeness-pr2): terminal-state backstop. Runs AFTER S3/S4
            # so it only sees cards those passes KEPT (their belt-and-suspenders /
            # recognized-failure gates didn't fire), and BEFORE aging (a card just
            # stamped terminal isn't also aged this tick). Probes the shared gh-only
            # task_terminal_state and stamps MERGED/CLOSED; UNKNOWN/OPEN keep. The
            # probe seam is stdlib+gh (no Supabase) so it degrades independently of
            # the S3/S4 chain_events seams; the ring seam is shared with S4. ---
            if terminal_fn is None:
                try:
                    terminal_fn = _default_terminal_fn()
                except Exception as e:  # noqa: BLE001 — degrade: skip backstop this tick
                    log(f'terminal-backstop: probe seam unavailable: {type(e).__name__}: {e}')
            if terminal_fn is not None and ring_fn is not None:
                try:
                    terminal_backstop = reconcile_terminal_captures(
                        registry, now,
                        terminal_fn=terminal_fn, ring_fn=ring_fn, dry_run=dry_run)
                except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                    log(f'terminal-backstop-reconcile raised: {type(e).__name__}: {e}')
                    terminal_backstop = TerminalReconcileResult()
            try:
                aged = age_parked_captures(registry, now)
            except Exception as e:  # noqa: BLE001 — fail-safe
                log(f'capture-aging raised: {type(e).__name__}: {e}')
                aged = []
            if author_fn is None:
                from missions_narrator import author_captures_in_registry  # noqa: PLC0415
                author_fn = author_captures_in_registry
            try:
                # use_llm only in LIVE mode; a dry-run uses the deterministic raw
                # briefing (no claude spawn, no cost) and writes nothing.
                briefed, deferred = author_fn(registry, now=now, use_llm=not dry_run)
            except Exception as e:  # noqa: BLE001 — fail-safe: never abort the tick
                log(f'narrator sweep raised: {type(e).__name__}: {e}')
                briefed, deferred = 0, 0
            # --- delegate-thread narrator (delegate-thread-narrator-001): narrate
            # each delegated card's current phase as a FYI team_to_larry post.
            # READS the registry + delegation signals and EMITS card_message rows;
            # it NEVER writes captures.json, so it sits outside the write-condition
            # below (its emits don't gate the healer's single captures.json write).
            # Fail-isolated so a narration error never aborts the tick. ---
            if narrate_fn is None:
                narrate_fn = author_delegate_thread_narration
            try:
                narrate_fn(registry, now=now, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001 — fail-safe: never abort the tick
                log(f'delegate-thread narration raised: {type(e).__name__}: {e}')
            if (completed.changed or failed.changed or terminal_backstop.changed
                    or deferred_actions.changed or aged or briefed) and not dry_run:
                # The Narrator sweep can hold the registry for minutes (one claude
                # spawn per pending capture, up to NARRATOR_MAX_PER_TICK ×
                # CLAUDE_TIMEOUT_SEC). Writing the stale in-memory copy would clobber
                # any capture the dashboard (a SEPARATE process) ingested or
                # state-changed during that window — the #409→#413 lost-update class
                # spec § 2 exists to prevent. Re-read the file FRESH and apply only
                # this tick's per-capture field deltas by id, so the read→write
                # window is sub-millisecond instead of the length of the LLM
                # round-trips. Single git-committer invariant is unchanged: this is
                # still the sole atomic write + the sole commit below.
                try:
                    fresh = read_captures_registry(cap_path)
                    if fresh is None:
                        log('captures.json re-read malformed before write — '
                            'skipping write this tick (avoids clobbering with stale)')
                    else:
                        merged = _merge_capture_deltas(fresh, before_by_id, registry)
                        atomic_write_captures(cap_path, fresh)
                        log(f'captures.json write: merged {merged} capture delta(s) '
                            'onto a fresh re-read (lost-update guard)')
                except Exception as e:  # noqa: BLE001 — fail-safe
                    log(f'captures.json write raised: {type(e).__name__}: {e}')
            # The parked_capture chain_events projection was retired 2026-07-01
            # (see the note by the removed project_parked_captures above): no
            # consumer reads it, so this healer no longer emits/clears those rows.
            if not dry_run:
                core = repo_paths.get('ourliberty-agent-core')
                if core:
                    try:
                        commit_status = commit_and_push_captures(
                            core, _commit_audit(retire, aged, briefed, completed,
                                                failed, deferred_actions,
                                                terminal_backstop))
                    except Exception as e:  # noqa: BLE001 — fail-safe
                        log(f'commit+push raised: {type(e).__name__}: {e}')
                        commit_status = 'push-failed'

    # --- phase 4: reconcile shipped mission phases (terminal-state § 3.3) +
    # author the funnel meaning layer (projects-v3 P2 Contract A). The Narrator
    # mission sweep briefs proposed orphan/suggested missions in-memory BEFORE the
    # single missions.json write+commit, so the GC healer stays the SOLE
    # missions.json committer (single-committer invariant): the narrator only
    # mutates the in-memory registry; the one atomic write + git commit below
    # batches the reconcile and the briefing deltas together. ---
    missions = MissionReconcileResult()
    mission_briefed = 0
    mission_deferred = 0
    missions_commit_status = 'nothing'
    miss_path = missions_path(repo_paths)
    if miss_path is None:
        log('missions.json path unresolved (agent-core not in repo_paths) — skipping reconcile')
    else:
        registry = read_missions_registry(miss_path)
        if registry is not None:
            # Read-time snapshot (shallow per mission) so the pre-write merge can
            # apply ONLY this tick's field deltas onto a fresh re-read (§ 2
            # lost-update guard — same shape as the captures path above).
            before_missions_by_id = {
                m['id']: dict(m)
                for m in registry.get('missions', [])
                if isinstance(m, dict) and m.get('id')
            }
            try:
                missions = reconcile_mission_phases(
                    registry, now, probe_fn=mission_probe_fn, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                log(f'mission-reconcile raised: {type(e).__name__}: {e}')
                missions = MissionReconcileResult()
            if mission_author_fn is None:
                from missions_narrator import author_missions_in_registry  # noqa: PLC0415
                mission_author_fn = author_missions_in_registry
            try:
                # use_llm only in LIVE mode; a dry-run uses the deterministic raw
                # briefing (no claude spawn, no cost) and writes nothing.
                mission_briefed, mission_deferred = mission_author_fn(
                    registry, now=now, use_llm=not dry_run)
            except Exception as e:  # noqa: BLE001 — fail-safe: never abort the tick
                log(f'narrator mission sweep raised: {type(e).__name__}: {e}')
                mission_briefed, mission_deferred = 0, 0
            if (missions.shipped or missions.retired or mission_briefed) and not dry_run:
                # Lost-update guard (mirrors the captures path): the mission sweep
                # can hold the registry for minutes (one claude spawn per pending
                # mission). Re-read fresh and apply only this tick's per-mission
                # field deltas by id so a concurrent dashboard accept/drop or the
                # orphan-autoregister drain landing mid-sweep isn't clobbered by
                # the stale in-memory copy. Single git-committer invariant is
                # unchanged: still the sole atomic write + the sole commit below.
                try:
                    fresh = read_missions_registry(miss_path)
                    if fresh is None:
                        log('missions.json re-read malformed before write — '
                            'skipping write this tick (avoids clobbering with stale)')
                    else:
                        merged = _merge_registry_deltas(
                            fresh, before_missions_by_id, registry,
                            list_key='missions')
                        _atomic_write_json(miss_path, fresh)
                        log(f'missions.json write: merged {merged} mission delta(s) '
                            'onto a fresh re-read (lost-update guard)')
                except Exception as e:  # noqa: BLE001 — fail-safe
                    log(f'missions.json write raised: {type(e).__name__}: {e}')
            # Single-committer durability (Contract D): commit ANY pending
            # missions.json delta on disk — our own reconcile write above OR a
            # delta a separate machine-owned cleanup left uncommitted (e.g. a
            # dashboard retire) — so it persists within ONE tick instead of
            # lingering as dirt that jams the next sync (the P1 incident). The GC
            # healer is the SOLE committer: it never writes a delta it didn't
            # author (no new writer), it only commits whatever is already on
            # disk. Mirrors the captures.json commit above; commit_and_push_
            # missions returns 'nothing' on a clean tree, so this is cheap when
            # there is nothing to persist.
            if not dry_run:
                core = repo_paths.get('ourliberty-agent-core')
                if core:
                    try:
                        missions_commit_status = commit_and_push_missions(
                            core, _missions_commit_audit(missions))
                    except Exception as e:  # noqa: BLE001 — fail-safe
                        log(f'missions commit+push raised: {type(e).__name__}: {e}')
                        missions_commit_status = 'push-failed'

    # --- phase 4b: author the Approvals-queue meaning layer (projects-v3 P7.2a).
    # Rides this tick so there is no new systemd unit; mirrors the State Log phase
    # below. Unlike captures/missions (JSON registries committed above), the
    # Approvals queue is read by the dashboard DIRECTLY from Supabase chain_events,
    # so the Narrator writes the briefing into each pending row's payload JSONB
    # itself — its OWN sole writer of those keys, never git-committed, outside the
    # single-committer machinery. Skipped under --dry-run (the sweep writes to
    # Supabase when it briefs; a dry tick must touch nothing). Fully fail-isolated:
    # an approval-sweep error must never break the GC tick. ---
    if not dry_run:
        try:
            from missions_narrator import author_pending_approvals  # noqa: PLC0415
            a_briefed, a_deferred = author_pending_approvals(now=now, use_llm=True)
            if a_briefed or a_deferred:
                log(f'approval meaning-layer: briefed {a_briefed}, '
                    f'deferred {a_deferred}')
        except Exception as e:  # noqa: BLE001 — fail-safe: never abort the tick
            log(f'narrator approval sweep raised: {type(e).__name__}: {e}')

    # --- phase 5: refresh the work-in-flight State Log (system self-awareness
    # Slice 1). Rides this tick so there is no new systemd unit; runs AFTER the
    # Narrator sweeps above so it sees fresh briefings. The State Log is droplet
    # runtime state under the agents blackboard — its OWN sole writer, never
    # git-committed and never a writer of missions/captures — so it is outside
    # the single-committer machinery above. Fully fail-isolated: a State-Log
    # error here must never break the GC tick (spec § 2 / D2). ---
    if state_log_fn is None:
        from system_state_log import write_state_log as state_log_fn  # noqa: PLC0415
    try:
        # use_llm + write only in LIVE mode; a dry-run authors the deterministic
        # narrative (no claude spawn, no cost) and writes nothing.
        state_log_fn(now=now, use_llm=not dry_run, write=not dry_run)
    except Exception as e:  # noqa: BLE001 — fail-safe: never abort the tick
        log(f'state-log refresh raised: {type(e).__name__}: {e}')

    _emit_summary(retire, aged, commit_status, dry_run,
                  missions=missions, missions_commit_status=missions_commit_status,
                  briefed=briefed, deferred=deferred,
                  mission_briefed=mission_briefed, mission_deferred=mission_deferred,
                  completed=completed,
                  failed=failed, deferred_actions=deferred_actions)
    return 0


def _commit_audit(retire: RetireResult, aged: list[str], briefed: int = 0,
                  completed: Optional['CompletionResult'] = None,
                  failed: Optional['FailureResult'] = None,
                  deferred_actions: Optional['DeferredActionResult'] = None,
                  terminal_backstop: Optional['TerminalReconcileResult'] = None) -> str:
    closed = len(completed.closed) if completed else 0
    closeouts = len(completed.closeouts) if completed else 0
    rang = len(failed.rung) if failed else 0
    applied = len(deferred_actions.applied) if deferred_actions else 0
    ts_done = len(terminal_backstop.completed) if terminal_backstop else 0
    ts_closeouts = len(terminal_backstop.closeouts) if terminal_backstop else 0
    ts_closed = len(terminal_backstop.closed_failed) if terminal_backstop else 0
    return (f'Auto-committed by heal_missions_card_gc. '
            f'retired={len(retire.retired)} aged={len(aged)} briefed={briefed} '
            f'closed={closed} closeouts={closeouts} failure-rings={rang} '
            f'deferred-applied={applied} '
            f'terminal-backstop-done={ts_done} '
            f'terminal-backstop-closeouts={ts_closeouts} '
            f'terminal-backstop-closed={ts_closed}.')


def _missions_commit_audit(missions: MissionReconcileResult) -> str:
    shipped = [f'{mid}({prior}->shipped)' for mid, prior in missions.shipped]
    retired = [f'{mid}(->retired)' for mid, _ in missions.retired]
    return (f'Auto-committed by heal_missions_card_gc. '
            f'terminal-state reconcile shipped={len(missions.shipped)} {shipped} '
            f'retired={len(missions.retired)} {retired}.')


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
