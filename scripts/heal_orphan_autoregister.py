#!/usr/bin/env python3
"""heal_orphan_autoregister.py — the Missions v2 orphan auto-registration healer
(Phase 3 § 6).

Sibling to heal_missions_card_gc (the GC pattern): a timer healer that keeps the
Orphans lane self-draining. Each tick scans NON-TERMINAL, NON-INFRASTRUCTURE
orphans and appends a `phase: "proposed"` entry to missions.json so the board can
render an accept/dismiss affordance (the dashboard step). The parked
`cap-bidirectional-missions-board` idea is retired here: auto-registration IS the
concrete realization of "agents read the board to self-prioritize".

The proposed lane is a SHORT, high-signal decision queue that stays short on its
own. Two mechanisms keep it that way (both reusing the Phase-2 derive, no drift):

  * FILTER (proposal time) — only GENUINE BUILDABLE initiatives become proposed.
    is_proposable_initiative (shared with the dashboard derive) is a stricter gate
    than is_infrastructure_task: it additionally sweeps chain-incident/alert
    artifacts, desktop captures, sequence-step proposals, translation/rule/
    dated-digest artifacts, and stale test-fixture ids. The Orphans lane still
    surfaces those; only the curated proposed lane filters them.

  * RETIREMENT (retire-with-audit) — an existing proposal is retired when its
    orphan is TERMINAL (PR merged/closed) or it would no longer pass the FILTER.
    Retirement sets the additive `acknowledged: true` flag (the same one the
    dashboard dismiss uses) + provenance; `phase` stays `proposed` so the task_id
    stays registered (never re-proposed, never hard-deleted). The pass runs every
    tick, so the first post-merge run RECONCILES the existing backlog retroactively
    and the run log reports the surviving proposed ids.

Three properties the spec pins (§ 6), all enforced structurally:

  1. REUSE THE PHASE-2 DERIVE (no drift). Orphan classification — which task_ids
     are orphans, which are infrastructure, and whether one is terminal — is NOT
     reimplemented here. We import the SAME pure functions the dashboard
     `/api/missions/derived` route uses (dashboard_api.detect_orphans,
     is_infrastructure_task, _derive_orphan_readability, ...). One classification,
     one source of truth.

  2. IDEMPOTENT. A proposed entry carries the orphan's task_id in its `task_ids`.
     That registers the task_id, so the NEXT tick's detect_orphans (which excludes
     every registered task_id) no longer surfaces it — it cannot be re-proposed.
     Accept (phase proposed→drafting) and dismiss (mark acknowledged) both keep the
     task_id registered, so neither re-proposes either. There is no separate dedup
     store to drift; the registry IS the dedup key.

  3. FAIL-SAFE — every indeterminate signal errs toward NOT proposing (no noise):
       * chain_events unavailable / fetch error  → propose nothing this tick.
       * missions.json malformed                 → skip (never append onto a
                                                    corrupt registry).
       * an orphan carries a pr_url we could NOT resolve a live state for → skip it
         (it might actually be merged/terminal; we refuse to propose a dead thread).
       * a terminal orphan (merged or explicitly-closed PR)              → skip.
     A bad tick reports + skips; it never corrupts the registry or proposes noise.

  4. COMMIT + PUSH the missions.json delta to main, the durability half — exactly
     like the GC healer commits its captures.json delta. The healer ONLY APPENDS
     proposed entries (never edits an existing one), so a rebase conflict against a
     concurrent New-Mission PR merge is near-impossible.

stdlib only (+ dashboard_api for the derive and chain_event_emit / larry_alerts,
all imported lazily so a missing optional dep degrades the tick rather than
crashing it).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports (dashboard_api, chain_event_emit,
# larry_alerts) resolve when run by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_MODELS_CONFIG_PATH = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'

MISSIONS_REL = 'agents/beacon/missions.json'

# The phase enum value that marks an auto-proposed (pre-drafting) thread (§ 6).
PROPOSED_PHASE = 'proposed'
# Stable, deterministic entry id for a proposal so a second tick can detect the
# entry already exists even before detect_orphans' registered-exclusion kicks in.
PROPOSED_ID_PREFIX = 'proposed-'
PROPOSED_BY = 'heal_orphan_autoregister'

GIT_TIMEOUT_SEC = 60
PUSH_TIMEOUT_SEC = 180
GH_TIMEOUT_SEC = 30

# How many recent merged/closed PRs per repo the terminal gate scans to resolve a
# proposed orphan's terminal state. Fetched ONCE per tick per (repo, state) and
# matched in-memory, so gh cost is O(repos) — NOT O(candidates) — regardless of the
# backlog size. The window must reach back past the oldest un-drained proposal:
# `--limit 40` aged a week-old backlog out, then 200 still aged out a backlog whose
# shipped PRs were 500+ merges deep (a high-churn fortnight left proposals pointing
# at PRs far older than the 200 most-recent). 1000 spans the full live backlog with
# headroom; the stuck-proposal flag pass (flag_stuck_proposals) bounds growth past
# that so the window never has to chase an unbounded reach. PRs older than this
# window aren't seen — they degrade to the event-only path / KEEP (fail-safe), then
# get flagged for a manual keep/drop — never a false drop.
_TERMINAL_PR_FETCH_LIMIT = 1000

# A proposed card that has lingered this many days WITHOUT a terminal-PR match is
# not auto-retirable by the gate: its task_id never corresponds to a merged/closed
# PR (a closeout deferred-work note, or a synthesized-label orphan whose task_id
# diverges from any branch). Rather than let it rot silently on the lane, it is
# flagged `needs_decision` so Larry can keep-or-drop it. 14d mirrors
# heal_missions_card_gc.EMPTY_PROBEABLE_MISSION_GRACE_DAYS (one grace clock).
STUCK_PROPOSAL_TTL_DAYS = 14

# Dashboard "+New mission" registration is drained HERE, inside the missions
# writer, so the dashboard never has to be a git committer (it just drops a queue
# file) and no unroutable PR is ever created. Two sources, both append-only +
# bounded + fail-safe (a fault in either never blocks propose/retire):
#
#   * PRIMARY — the queue. The dashboard drops one `<id>.json` per mission under
#     `<agents_root>/blackboard/new-mission-queue/`; we append each into
#     missions.json via this healer's own (single-committer) commit and remove the
#     file once its mission is confirmed on origin/main. This is the canonical
#     path now (dashboard_api._handle_new_mission).
#   * BACKSTOP (transitional) — open `feat/new-mission-*` PRs. The OLD dashboard
#     flow opened one PR per mission via the GitHub API outside the Forge pipeline;
#     those never got a Mirror review, sat open, and paged heal-pipeline-stall. The
#     dashboard no longer opens them, but in-flight PRs at deploy time (and any old
#     dashboard-api process before its restart) still need closing — we ingest the
#     single named mission (append-only) and close the PR (no stale-branch merge →
#     no conflicts; no `feat(missions):` merge → no unreviewed-merge page). Remove
#     this path once `gh pr list` shows zero feat/new-mission-* PRs for a few days.
NEWMISSION_BRANCH_PREFIX = 'feat/new-mission-'
NEWMISSION_MAX_PER_TICK = 20
ENV_NEWMISSION_INGEST = 'OURLIBERTY_NEWMISSION_INGEST_ENABLED'
# Subdir under the agents blackboard the dashboard +New mission flow drops queued
# mission entries into (mirrors dashboard_api._new_mission_queue_dir). NOT inside
# the git checkout, so a pending file is never untracked-file drift.
NEWMISSION_QUEUE_SUBPATH = ('blackboard', 'new-mission-queue')


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
    return base / 'missions-autoregister.log'


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
    """Repo name → Path from config/agent-models.json ``repo_paths`` (the same
    block the GC healer reads). Returns {} on a missing/unreadable block — the
    healer degrades (skips the tick) rather than crashing."""
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


def missions_path(repo_paths: dict[str, Path]) -> Optional[Path]:
    """Path to agent-core's missions.json, or None if agent-core isn't
    configured."""
    core = repo_paths.get('ourliberty-agent-core')
    return (core / MISSIONS_REL) if core else None


# ---------- derive reuse (the SAME functions the dashboard route uses) ----------


def load_derive() -> Optional[Any]:
    """Import the dashboard_api module — the single source of the orphan derive.

    Lazy + fail-safe: if the import fails (e.g. fastapi absent under the runtime
    python), we log and the caller skips the tick rather than crashing. Reusing
    the module (instead of re-porting detect_orphans / is_infrastructure_task /
    _derive_orphan_readability) is what guarantees ZERO drift between what the
    board shows as an orphan and what this healer proposes."""
    try:
        import dashboard_api  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — fail-safe: a missing dep skips the tick
        log(f'dashboard_api (derive) import failed: {type(e).__name__}: {e}')
        return None
    return dashboard_api


def sequence_owned_task_ids(derive: Any, now: datetime) -> set[str]:
    """Every task_id OWNED by a registered build sequence (its ``seq_id`` + each
    step's task_id), so the catcher excludes them from the orphan derive and never
    auto-proposes sequence work as a standalone initiative — the SAME suppression
    the board applies (zero drift; reuses the derive's own
    ``_reader_build_sequences`` + ``_sequence_owned_task_ids``). Fail-safe: any
    read/parse error returns an empty set so a sequence-store fault never crashes
    the tick (the catcher then degrades to its prior, un-collapsed behavior)."""
    try:
        bs = derive._reader_build_sequences(derive._sequence_blackboard_root(), now)
        return derive._sequence_owned_task_ids(bs)
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the tick
        log(f'sequence-owned read failed: {type(e).__name__}: {e} — collapsing nothing')
        return set()


# ---------- missions.json (read / write) — fail-safe ----------


def read_missions_registry(path: Path) -> Optional[dict[str, Any]]:
    """Load missions.json as a registry dict. Missing file → fresh empty registry.
    Malformed / wrong-shape → None (caller skips this tick; we never append onto a
    corrupt registry). Mirrors heal_missions_card_gc.read_captures_registry: a bad
    tick reports rather than crashes (§ 6 fail-safe)."""
    if not path.exists():
        return {'schema_version': 1, 'missions': []}
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else {'schema_version': 1, 'missions': []}
    except (OSError, json.JSONDecodeError) as e:
        log(f'missions.json malformed/unreadable ({path}): {e} — skipping this tick')
        return None
    if not isinstance(data, dict) or not isinstance(data.get('missions'), list):
        log(f'missions.json shape invalid ({path}) — skipping this tick')
        return None
    data.setdefault('schema_version', 1)
    return data


def registered_task_ids(registry: dict[str, Any]) -> set[str]:
    """Every task_id already registered to a mission entry (proposed, accepted,
    dismissed, or any other phase). detect_orphans excludes these, so this set is
    the idempotency key: once an orphan's task_id lands here it is never
    re-proposed."""
    out: set[str] = set()
    for entry in registry.get('missions', []):
        if not isinstance(entry, dict):
            continue
        tids = entry.get('task_ids')
        if isinstance(tids, list):
            out.update(t for t in tids if isinstance(t, str) and t)
    return out


def existing_entry_ids(registry: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for entry in registry.get('missions', []):
        if isinstance(entry, dict):
            eid = entry.get('id')
            if isinstance(eid, str) and eid:
                out.add(eid)
    return out


def atomic_write_missions(path: Path, registry: dict[str, Any]) -> None:
    """tmp-in-same-dir + os.replace. Mirrors heal_missions_card_gc.atomic_write_captures
    so a reader never sees a partial file."""
    import tempfile  # noqa: PLC0415
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
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


def _snapshot_missions_by_id(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Shallow per-entry snapshot of the read-time registry, keyed by mission id —
    the baseline the pre-write lost-update merge diffs this tick's work against.
    Shallow is enough: every pass (retire, stuck-flag, propose, ingest, drain)
    replaces top-level keys or appends whole new entries; none mutates a nested
    object in place."""
    return {
        m['id']: dict(m)
        for m in registry.get('missions', [])
        if isinstance(m, dict) and m.get('id')
    }


def _merge_mission_deltas(fresh: dict[str, Any],
                          before_by_id: dict[str, dict[str, Any]],
                          worked: dict[str, Any]) -> tuple[int, int]:
    """Apply only the work THIS tick did (``worked`` vs the read-time snapshot
    ``before_by_id``) onto a freshly re-read ``fresh`` registry, matched by
    mission ``id``. Two shapes of work:

    - FIELD DELTAS on entries that existed at read time (retire-with-audit
      flags, stuck needs_decision flags) — the same field-level merge as
      heal_missions_card_gc._merge_registry_deltas: touch only the keys we
      changed, never an entry we didn't author onto, never an entry missing
      from the fresh file;
    - APPENDS for entries this tick authored (scan_and_propose / new-mission PR
      ingest / queue drain) — absent from ``before_by_id`` AND from ``fresh``.
      An id a concurrent writer already landed in ``fresh`` keeps the fresh
      copy untouched (dedup — we never overwrite another writer's entry).

    Why not whole-file replace: the passes above can hold the read-time
    snapshot across chain_events and gh round-trips (seconds to minutes),
    during which a separate process — the dashboard API stamping a delegate
    ``spawned``, the GC healer flipping phases / authoring briefings — may
    write missions.json. Replacing the file with the stale in-memory copy
    would silently lose those writes (the § 2 lost-update class).

    Returns (field deltas applied, new entries appended)."""
    missions = fresh.setdefault('missions', [])
    fresh_by_id = {
        m['id']: m for m in missions
        if isinstance(m, dict) and m.get('id')
    }
    applied = 0
    appended = 0
    for entry in worked.get('missions', []):
        if not isinstance(entry, dict):
            continue
        mid = entry.get('id')
        if not mid:
            continue
        before = before_by_id.get(mid)
        if before is None:
            # Not present at read time — an entry this tick authored. Append it
            # unless a concurrent writer already landed the same id.
            if mid not in fresh_by_id:
                missions.append(entry)
                fresh_by_id[mid] = entry
                appended += 1
            continue
        set_fields = {k: v for k, v in entry.items()
                      if k not in before or before[k] != v}
        removed = [k for k in before if k not in entry]
        if not set_fields and not removed:
            continue  # untouched this tick — leave the fresh copy alone
        target = fresh_by_id.get(mid)
        if target is None:
            continue  # gone from the fresh file — drop our stale delta, don't re-add
        for k, v in set_fields.items():
            target[k] = v
        for k in removed:
            target.pop(k, None)
        applied += 1
    return applied, appended


# ---------- proposal selection + construction (pure; unit-tested directly) ----------


def select_proposable_orphans(
    orphans: list[dict[str, Any]],
    pr_state_by_url: dict[str, str],
) -> list[dict[str, Any]]:
    """From derive-enriched orphans, keep only the ones safe to propose.

    Pure — the caller resolves all I/O (orphan derive + PR states) first. The two
    fail-safe guards (§ 6 'every indeterminate signal errs toward NOT proposing'):

      * TERMINAL → skip. A merged or explicitly-closed orphan is done; proposing it
        is noise. `terminal` is the derive's own field.
      * PR-STATE INDETERMINATE → skip. An orphan that carries a pr_url whose live
        state we could NOT resolve (no token, network error) is indeterminate — it
        might actually be merged. We refuse to propose it rather than risk a dead
        thread. (A PR-less orphan has no merge state to be unsure about, so it is
        proposable when non-terminal.)
    """
    out: list[dict[str, Any]] = []
    for o in orphans:
        if o.get('terminal'):
            continue
        pr_url = o.get('pr_url')
        if pr_url and pr_url not in pr_state_by_url:
            # Has a PR but its live state is indeterminate → err toward NOT proposing.
            continue
        out.append(o)
    return out


def build_proposed_entry(orphan: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Construct the `phase: "proposed"` missions.json entry for one orphan (§ 6).

    Carries the orphan's task_id (the idempotency anchor — registers it so the next
    tick's detect_orphans excludes it), its derived label, repo/branch, and last
    activity. Shaped like a normal registry entry (id/name/phase/brief/spec_docs/
    task_ids/repo/created/deferred_reason) plus additive provenance fields the
    Proposed affordance reads."""
    task_id = orphan['task_id']
    label = orphan.get('label') or task_id
    repo = orphan.get('repo')
    branch = orphan.get('branch')
    last_activity = orphan.get('last_event_ts')
    if repo and branch:
        loc = f' ({repo}/{branch})'
    elif repo:
        loc = f' ({repo})'
    else:
        loc = ''
    return {
        'id': f'{PROPOSED_ID_PREFIX}{task_id}',
        'name': label,
        'phase': PROPOSED_PHASE,
        'brief': (
            f'Auto-proposed from orphan task `{task_id}`{loc}. '
            f'Last activity {last_activity}. Accept to claim the task_id into a '
            f'drafting mission; dismiss to stop re-proposing.'
        ),
        'spec_docs': [],
        'task_ids': [task_id],
        'repo': repo,
        'created': now.date().isoformat(),
        'deferred_reason': None,
        # Additive provenance + orphan metadata (§ 6) — ignored by existing reads.
        'proposed_by': PROPOSED_BY,
        'proposed_at': now.isoformat(),
        'orphan_branch': branch,
        'orphan_last_activity_ts': last_activity,
    }


# ---------- the derive-backed scan (effectful edges seamed for tests) ----------


@dataclass
class ProposeResult:
    proposed: list[tuple[str, str]] = field(default_factory=list)  # (task_id, entry_id)
    retired: list[tuple[str, str]] = field(default_factory=list)  # (entry_id, reason)
    surviving_proposed: list[str] = field(default_factory=list)  # live proposed entry ids
    flagged_stuck: list[str] = field(default_factory=list)  # newly needs_decision-flagged ids
    ingested: list[tuple[int, str]] = field(default_factory=list)  # (pr_number, mission_id) — PR backstop
    closeable_prs: list[tuple[int, str]] = field(default_factory=list)  # new-mission PRs to close
    queue_drained: list[str] = field(default_factory=list)  # mission_ids drained from the queue this tick
    deletable_queue: list[tuple[Path, str]] = field(default_factory=list)  # (queue_file, mission_id) to remove
    scanned_orphans: int = 0
    skipped_terminal_or_indeterminate: int = 0
    skipped_non_proposable: int = 0
    events_unavailable: bool = False
    registry_unreadable: bool = False
    derive_unavailable: bool = False


def scan_and_propose(
    registry: dict[str, Any],
    rows: list[dict[str, Any]],
    derive: Any,
    now: datetime,
    *,
    pr_state_resolver: Optional[Callable[[list[str]], dict[str, str]]] = None,
    collapsed_task_ids: Optional[set[str]] = None,
) -> ProposeResult:
    """Reuse the derive to find orphans, then append a proposed entry for each
    proposable one. Mutates ``registry['missions']`` in place; returns what was
    proposed. Fail-safe: any unexpected error proposes nothing (the registry is
    left untouched for this orphan set).

    ``collapsed_task_ids`` (sequence-owned): ids OWNED by a registered build
    sequence (seq_id + step ids). Passed straight to ``detect_orphans`` so the
    catcher excludes them exactly like the board does — a bare/completed sequence's
    ids are never auto-proposed. None → no collapse (prior behavior)."""
    res = ProposeResult()
    registered = registered_task_ids(registry)
    existing_ids = existing_entry_ids(registry)

    detected = derive.detect_orphans(rows, registered, collapsed_task_ids)
    res.scanned_orphans = len(detected)
    # Same buildable-initiative gate the dashboard's Orphaned lane now applies
    # (§ 4.8): only genuine buildable initiatives become a `proposed` decision-queue
    # entry. detect_orphans already drops is_infrastructure_task;
    # is_proposable_initiative additionally sweeps
    # chain-incident/alert artifacts, desktop captures, sequence-step proposals,
    # translation/rule/dated-digest artifacts, and stale test-fixture ids.
    orphans = [
        o for o in detected
        if derive.is_proposable_initiative(o.get('task_id', ''), o.get('agent'))
    ]
    res.skipped_non_proposable = len(detected) - len(orphans)
    if not orphans:
        return res

    # Group events per orphan (newest-first preserved) for readability/terminal —
    # exactly how _build_derived_response feeds _derive_orphan_readability.
    events_by_orphan: dict[str, list[dict[str, Any]]] = {}
    for ev in rows:
        tid = ev.get('task_id')
        if tid:
            events_by_orphan.setdefault(tid, []).append(ev)

    # Resolve live PR states (fail-safe). A {} result (no token / error) means
    # EVERY pr_url-bearing orphan is indeterminate → none of them are proposed.
    if pr_state_resolver is None:
        pr_state_resolver = derive._resolve_orphan_pr_states
    pr_state_by_url: dict[str, str] = {}
    orphan_pr_urls = [o['pr_url'] for o in orphans if o.get('pr_url')]
    if orphan_pr_urls:
        try:
            pr_state_by_url = pr_state_resolver(orphan_pr_urls) or {}
        except Exception as e:  # noqa: BLE001 — fail-safe: indeterminate → propose none
            log(f'PR-state resolve raised: {type(e).__name__}: {e} — treating as indeterminate')
            pr_state_by_url = {}

    for o in orphans:
        o.update(derive._derive_orphan_readability(
            o, events_by_orphan.get(o['task_id'], []), now,
            pr_state=pr_state_by_url.get(o.get('pr_url')),
        ))

    proposable = select_proposable_orphans(orphans, pr_state_by_url)
    res.skipped_terminal_or_indeterminate = len(orphans) - len(proposable)

    for o in proposable:
        entry = build_proposed_entry(o, now)
        # Defensive second idempotency layer: never duplicate an entry id (detect_orphans
        # already excludes registered task_ids, so this only guards a malformed prior entry).
        if entry['id'] in existing_ids:
            continue
        registry['missions'].append(entry)
        existing_ids.add(entry['id'])
        res.proposed.append((o['task_id'], entry['id']))
    return res


# ---------- retirement: self-clean stale proposals (retire-with-audit) ----------


def _events_by_task(rows: list[dict[str, Any]], derive: Any) -> dict[str, list[dict[str, Any]]]:
    """Group chain_events by task_id, newest-first (the order the derive's
    readability/terminal helpers expect)."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for ev in rows:
        tid = ev.get('task_id')
        if tid:
            by_task.setdefault(tid, []).append(ev)
    for evs in by_task.values():
        evs.sort(key=lambda e: derive._ts_key(e.get('ts')), reverse=True)
    return by_task


def _retire_reason(
    entry: dict[str, Any],
    events_by_task: dict[str, list[dict[str, Any]]],
    derive: Any,
    now: datetime,
    pr_state_by_url: dict[str, str],
    sequence_owned: Optional[set[str]] = None,
) -> Optional[str]:
    """Why this proposed entry should retire, or None to KEEP it.

    Three triggers, all fail-safe (an indeterminate signal → KEEP a live buildable):
      * SEQUENCE-OWNED — every task_id the entry carries is OWNED by a registered
        build sequence (its seq_id or a step id): sequence work that should never
        have been a standalone proposal → retire 'sequence-owned'. (Checked first so
        the more specific reason wins over the generic filter trigger.)
      * FILTER — every task_id the entry carries is non-proposable noise (would not
        pass is_proposable_initiative today): a category that should never have been
        proposed → retire 'filter-non-buildable'.
      * TERMINAL — the orphan's PR is merged or explicitly closed → retire
        'orphan-terminal'. A task_id with no events in the window, or a PR whose
        live state we could not resolve, is indeterminate → not a retire signal.
    """
    tids = [t for t in (entry.get('task_ids') or []) if isinstance(t, str) and t]
    if not tids:
        return None
    owned = sequence_owned or set()
    if owned and all(t in owned for t in tids):
        return 'sequence-owned'
    if all(not derive.is_proposable_initiative(t) for t in tids):
        return 'filter-non-buildable'
    for tid in tids:
        evs = events_by_task.get(tid)
        if not evs:
            continue  # no signal in window → indeterminate → keep
        summ = derive.summarize_task_events(evs)
        pr_url = summ.get('pr_url')
        pr_state = pr_state_by_url.get(pr_url) if pr_url else None
        if pr_url and pr_state is None:
            continue  # has a PR but its live state is indeterminate → keep
        readability = derive._derive_orphan_readability(
            {'task_id': tid, 'last_event_ts': summ.get('last_event_ts'), 'pr_url': pr_url},
            evs, now, pr_state=pr_state,
        )
        if readability.get('terminal'):
            return 'orphan-terminal'
    return None


def _fetch_terminal_prs(repos: tuple[str, ...]) -> Optional[list[tuple[dict[str, Any], str]]]:
    """The recent merged + closed PRs across `repos`, fetched ONCE (reach-back-
    independent of any single task). Returns a flat [(pr_dict, state), ...] index,
    or None if EVERY (repo, state) fetch failed — the gate then degrades to the
    event-only path (fail-safe; a total gh outage never drops a card).

    gh's `--state merged` and `--state closed` are disjoint (closed EXCLUDES merged),
    so the union is exactly the verifiably-TERMINAL set — open/in-flight PRs are
    never fetched and so can never be mistaken for terminal. A single (repo, state)
    failure is skipped (a partial index can only MISS a terminal PR → KEEP, never a
    false drop)."""
    import json as _json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import task_resolution as tr  # noqa: PLC0415
    index: list[tuple[dict[str, Any], str]] = []
    any_ok = False
    for repo in repos:
        for state in ('merged', 'closed'):
            try:
                res = subprocess.run(
                    ['gh', 'pr', 'list', '--repo', repo, '--state', state,
                     '--limit', str(_TERMINAL_PR_FETCH_LIMIT), '--json',
                     'number,title,headRefName,state'],
                    capture_output=True, text=True, timeout=tr.GH_TIMEOUT_S,
                    env=tr._gh_env(),
                )
                if res.returncode != 0:
                    log(f'terminal-PR fetch {repo}/{state} rc={res.returncode}: '
                        f'{(res.stderr or "")[:160]}')
                    continue
                prs = _json.loads(res.stdout or '[]')
            except (subprocess.TimeoutExpired, _json.JSONDecodeError,
                    FileNotFoundError, OSError) as e:
                log(f'terminal-PR fetch {repo}/{state} failed: {type(e).__name__}: {e}')
                continue
            any_ok = True
            if isinstance(prs, list):
                for pr in prs:
                    if isinstance(pr, dict):
                        index.append((pr, state))
    return index if any_ok else None


def _terminal_gate_from_index(
    index: list[tuple[dict[str, Any], str]],
) -> Callable[[list[str]], tuple[bool, Optional[str]]]:
    """Pure (no I/O) gate over a pre-fetched terminal-PR index. Returns
    callable(task_ids) -> (terminal_all, signal): True iff EVERY task_id matches a
    merged/closed PR (positive evidence only, § 5). Reuses the SAME matcher the
    pipeline-stall + drain healers use (task_resolution.pr_matches_task) so the
    branch/title rule never drifts."""
    import task_resolution as tr  # noqa: PLC0415

    def gate(task_ids: list[str]) -> tuple[bool, Optional[str]]:
        signal: Optional[str] = None
        for tid in task_ids:
            hit: Optional[tuple[dict[str, Any], str]] = None
            for pr, state in index:
                if tr.pr_matches_task(pr, tid):
                    hit = (pr, state)
                    break
            if hit is None:
                return (False, signal)  # tid not verifiably terminal → KEEP
            if signal is None:
                pr, _state = hit
                kind = 'pr_merged' if (pr.get('state') or '').upper() == 'MERGED' else 'pr_closed'
                signal = f'{kind}:#{pr.get("number")}'
        return (bool(task_ids), signal)

    return gate


def _load_terminal_gate() -> Optional[Callable[[list[str]], tuple[bool, Optional[str]]]]:
    """Build the RELIABLE PR-state terminal gate, or None if unavailable.

    The per-tick event-only terminal signal (an `auto_merge` chain event or a
    resolvable pr_url) is BLIND to old shipped work whose events carry neither —
    those read `stalled` forever and never retire. This gate closes that gap with a
    POSITIVE-EVIDENCE PR-state check, never a clock (§ 5 guardrail): it scans a deep
    recent window of merged/closed PRs (fetched once per tick, not per candidate) and
    matches each proposed orphan's task_id by branch/title. None when gh is wholly
    unavailable (the tick then leans on the event-only path alone — fail-safe)."""
    import task_resolution as tr  # noqa: PLC0415
    try:
        index = _fetch_terminal_prs(tr.DEFAULT_REPOS)
    except Exception as e:  # noqa: BLE001 — any fetch fault degrades to event-only, never crashes
        log(f'terminal-gate fetch raised: {type(e).__name__}: {e} — event-only path this tick')
        return None
    if index is None:
        return None
    return _terminal_gate_from_index(index)


def retire_stale_proposals(
    registry: dict[str, Any],
    rows: list[dict[str, Any]],
    derive: Any,
    now: datetime,
    *,
    pr_state_resolver: Optional[Callable[[list[str]], dict[str, str]]] = None,
    terminal_gate: Optional[Callable[[list[str]], tuple[bool, Optional[str]]]] = None,
    collapsed_task_ids: Optional[set[str]] = None,
) -> list[tuple[str, str]]:
    """Retire-with-audit any auto-proposed entry that should no longer be live.

    Reuses the system's existing dismiss mechanism: set the ADDITIVE
    `acknowledged: true` flag (which the dashboard's Proposed affordance already
    reads to hide a thread) plus retirement provenance. `phase` STAYS 'proposed'
    so the task_id stays registered — the entry is never re-proposed, never
    hard-deleted, and a re-run is a no-op (already-acknowledged entries are
    skipped). Only touches entries this healer proposed (`proposed_by`).

    Mutates ``registry['missions']`` in place; returns [(entry_id, reason), ...]
    for the entries newly retired this tick. Fail-safe: an indeterminate signal
    never retires a still-live buildable proposal."""
    candidates = [
        e for e in registry.get('missions', [])
        if isinstance(e, dict)
        and e.get('phase') == PROPOSED_PHASE
        and e.get('proposed_by') == PROPOSED_BY
        and not e.get('acknowledged')
    ]
    if not candidates:
        return []

    events_by_task = _events_by_task(rows, derive)

    # Resolve live PR states for the candidates' PRs (fail-safe — {} → every
    # PR-bearing candidate is indeterminate, so only the FILTER trigger can retire).
    if pr_state_resolver is None:
        pr_state_resolver = derive._resolve_orphan_pr_states
    cand_pr_urls: list[str] = []
    for e in candidates:
        for tid in (e.get('task_ids') or []):
            if not isinstance(tid, str):
                continue
            pr_url = derive.summarize_task_events(events_by_task.get(tid, [])).get('pr_url')
            if pr_url:
                cand_pr_urls.append(pr_url)
    pr_state_by_url: dict[str, str] = {}
    if cand_pr_urls:
        try:
            pr_state_by_url = pr_state_resolver(cand_pr_urls) or {}
        except Exception as e:  # noqa: BLE001 — fail-safe: indeterminate → keep
            log(f'retire PR-state resolve raised: {type(e).__name__}: {e} — treating as indeterminate')
            pr_state_by_url = {}

    # The reliable gh-PR-state terminal gate (fail-safe: None when unavailable).
    if terminal_gate is None:
        terminal_gate = _load_terminal_gate()

    retired: list[tuple[str, str]] = []
    for entry in candidates:
        reason = _retire_reason(entry, events_by_task, derive, now, pr_state_by_url,
                                sequence_owned=collapsed_task_ids)
        signal: Optional[str] = None
        # Event-only path indeterminate (reason is None) → fall back to the RELIABLE
        # terminal gate (in-memory match over the once-fetched PR index — cheap, so
        # EVERY candidate is checked, no per-tick budget / starvation). A still-live
        # (open / unmatched) entry is correctly KEPT; only positive merged/closed
        # evidence retires it (§ 5).
        if reason is None and terminal_gate is not None:
            tids = [t for t in (entry.get('task_ids') or []) if isinstance(t, str) and t]
            if tids:
                try:
                    is_terminal, sig = terminal_gate(tids)
                except Exception as e:  # noqa: BLE001 — fail-safe: any gate error → keep
                    log(f'terminal gate raised for {entry.get("id")}: '
                        f'{type(e).__name__}: {e} — keeping')
                    is_terminal, sig = False, None
                if is_terminal:
                    reason, signal = 'orphan-terminal', sig
        if reason is None:
            continue
        entry['acknowledged'] = True
        entry['retired_by'] = PROPOSED_BY
        entry['retired_at'] = now.isoformat()
        entry['retired_reason'] = reason
        # Provenance parity with heal_missions_board_drain's drop: record the
        # positive terminal signal (pr_merged:#N / pr_closed:#N) when the gh gate
        # fired. Absent for the filter/event triggers (no PR# to cite).
        if signal:
            entry['retired_signal'] = signal
        retired.append((entry.get('id', ''), reason))
    return retired


def surviving_proposed_ids(registry: dict[str, Any]) -> list[str]:
    """Entry ids still LIVE on the proposed lane (phase=proposed, not
    acknowledged) — the short list Beacon relays to Larry after a tick."""
    out: list[str] = []
    for entry in registry.get('missions', []):
        if (isinstance(entry, dict) and entry.get('phase') == PROPOSED_PHASE
                and not entry.get('acknowledged')):
            eid = entry.get('id')
            if isinstance(eid, str) and eid:
                out.append(eid)
    return out


# ---------- surface the stuck residue for a manual keep/drop (backlog) -------


def _parse_date(value: Any) -> Optional[datetime]:
    """Parse an ISO date/datetime string to an aware UTC datetime, or None. A
    proposal's `created` is a date-only ISO string (YYYY-MM-DD)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def flag_stuck_proposals(
    registry: dict[str, Any],
    now: datetime,
    *,
    terminal_gate: Callable[[list[str]], tuple[bool, Optional[str]]],
    ttl_days: int = STUCK_PROPOSAL_TTL_DAYS,
) -> list[str]:
    """Surface (never retire) the residue: proposed cards stuck with no terminal-PR
    match. A proposed + not-acknowledged card older than ``ttl_days`` whose task_ids
    the reliable terminal gate does NOT match cannot be auto-retired — its work
    either never shipped (a closeout deferred note) or its task_id diverges from any
    branch. Stamp an additive `needs_decision` marker (+ reason/since) so the board
    can flag it for a human keep/drop, instead of leaving it to rot silently.

    Covers EVERY owner (no `proposed_by` guard, unlike retire): the closeout-authored
    notes need this surface as much as the orphan-derived ones. Mutates the registry
    in place; returns the ids newly flagged this tick. Idempotent: an already-flagged
    card, or one the gate matches (the retire pass sweeps it instead), is skipped.
    Fail-safe: the caller invokes this ONLY when the gate is live, so a gh outage
    (gate is None / a transient miss) never mass-flags a healthy lane."""
    newly: list[str] = []
    for entry in registry.get('missions', []):
        if not isinstance(entry, dict):
            continue
        if entry.get('phase') != PROPOSED_PHASE or entry.get('acknowledged'):
            continue
        if entry.get('proposed_by') == 'retrospective-author':
            # The weekly retrospective author owns its own re-proposal/dismiss
            # lifecycle (it re-evaluates each week and reconciles the dismissed
            # flag from the board). The orphan-healer's 14d "keep or drop" nudge
            # would double-surface its standing suggestions onto the very needs-you
            # lane this alert-pipeline-rework is meant to declutter (review #749
            # finding 2). These have empty task_ids by design and never had a PR.
            continue
        if entry.get('needs_decision'):
            continue  # already surfaced — idempotent
        created = _parse_date(entry.get('created'))
        if created is None or (now - created) < timedelta(days=ttl_days):
            continue  # too young (or undateable) — give the gate time to retire it
        tids = [t for t in (entry.get('task_ids') or []) if isinstance(t, str) and t]
        try:
            is_terminal, _sig = terminal_gate(tids) if tids else (False, None)
        except Exception as e:  # noqa: BLE001 — fail-safe: a gate error never flags
            log(f'stuck-flag gate raised for {entry.get("id")}: '
                f'{type(e).__name__}: {e} — skip')
            continue
        if is_terminal:
            continue  # the retire pass sweeps it this tick — not stuck
        age_days = (now - created).days
        entry['needs_decision'] = True
        entry['needs_decision_since'] = now.isoformat()
        entry['needs_decision_reason'] = (
            f'{age_days}d on the proposed lane with no terminal-PR match — keep or drop')
        eid = entry.get('id')
        if isinstance(eid, str) and eid:
            newly.append(eid)
    return newly


# ---------- commit + push the missions.json delta to main (§ 6) ----------


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """Run git in ``repo``; a timeout/OS error becomes a synthetic non-zero result
    so callers branch on returncode uniformly."""
    try:
        return subprocess.run(
            ['git', *args], cwd=str(repo),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'git {" ".join(args)} failed in {repo}: {type(e).__name__}: {e}')
        return subprocess.CompletedProcess(args, returncode=255, stdout='', stderr=str(e))


def commit_and_push_missions(repo: Path, audit_msg: str) -> str:
    """Commit + push any missions.json delta to origin/main. Returns a status token:
      'nothing'       — no delta to commit
      'wrong-branch'  — repo not on main; refuse to commit (would land on a feature
                        branch) — caller escalates
      'committed'     — committed and pushed
      'commit-failed' / 'push-failed' — git step failed; commit retained locally

    Mirrors heal_missions_card_gc.commit_and_push_captures: try push; on a non-FF
    refusal, pull --rebase --autostash and retry; abort the rebase on conflict.
    Never force-pushes. This healer only APPENDS proposed entries (never edits an
    existing one), so a rebase conflict against a concurrent New-Mission PR merge is
    near-impossible — the appended object can't collide with an unrelated edit."""
    head = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    branch = head.stdout.strip() if head.returncode == 0 else ''
    if branch != 'main':
        return 'wrong-branch'

    clean = _git(repo, 'diff', '--quiet', '--', MISSIONS_REL)
    clean_cached = _git(repo, 'diff', '--quiet', '--cached', '--', MISSIONS_REL)
    if clean.returncode == 0 and clean_cached.returncode == 0:
        return 'nothing'

    if _git(repo, 'add', MISSIONS_REL).returncode != 0:
        return 'commit-failed'
    commit = _git(repo, 'commit', '-m',
                  'chore(missions): autoregister healer — reconcile proposed lane',
                  '-m', audit_msg)
    if commit.returncode != 0:
        log(f'missions.json commit failed in {repo}: {(commit.stderr or commit.stdout).strip()[:200]}')
        return 'commit-failed'

    if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
        return 'committed'
    log('missions.json push refused (likely non-FF); attempting pull --rebase --autostash')
    rebase = _git(repo, 'pull', '--rebase', '--autostash', '-q', 'origin', 'main',
                  timeout=PUSH_TIMEOUT_SEC)
    if rebase.returncode == 0:
        if _git(repo, 'push', '-q', 'origin', 'main', timeout=PUSH_TIMEOUT_SEC).returncode == 0:
            return 'committed'
        return 'push-failed'
    log('missions.json rebase failed; aborting (commit retained locally)')
    _git(repo, 'rebase', '--abort')
    return 'push-failed'


# ---------- alerting ----------


# ---------- new-mission PR ingestion (clears the dashboard +New mission stall) ----------


def _missions_repo_full() -> str:
    """owner/repo the dashboard opens +New mission PRs against. Env-overridable to
    match dashboard_api._missions_repo_full."""
    return os.environ.get(
        'OURLIBERTY_MISSIONS_REPO', 'Larry-Yatch/ourliberty-agent-core')


def newmission_ingest_enabled() -> bool:
    """True ONLY when OURLIBERTY_NEWMISSION_INGEST_ENABLED == 'true'. Defaults OFF
    (observe/dry-run) so a mutating action — committing to main and closing PR
    branches — gets a soak before it goes live, matching heal_pr_auto_merge's
    default-off posture. When off, run_once logs what it WOULD reconcile and
    mutates nothing. The global healers.disabled kills the whole healer."""
    return os.environ.get(ENV_NEWMISSION_INGEST, '').strip().lower() == 'true'


def _gh(*args: str, timeout: int = GH_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """Tolerant `gh` wrapper (mirrors `_git`): never raises — returns a
    CompletedProcess with returncode 255 on timeout/OSError so callers degrade.
    Hardens PATH like the sibling gh wrappers (heal_pr_auto_merge.gh_json): the
    systemd unit runs with a minimal PATH and `gh` lives in /usr/local/bin or
    /snap/bin, so without this every call would 255 and silently no-op."""
    env = {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}
    try:
        return subprocess.run(['gh', *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(args, returncode=255, stdout='', stderr=str(e))


def list_open_new_mission_prs(repo_full: str) -> list[dict[str, Any]]:
    """Open PRs whose branch starts with NEWMISSION_BRANCH_PREFIX, as
    [{'number': int, 'branch': str}]. [] on any gh failure (logged upstream)."""
    proc = _gh('pr', 'list', '--repo', repo_full, '--state', 'open',
               '--limit', '100', '--json', 'number,headRefName')
    if proc.returncode != 0:
        log(f'list new-mission PRs failed: {(proc.stderr or "").strip()[:200]}')
        return []
    try:
        rows = json.loads(proc.stdout or '[]')
    except (json.JSONDecodeError, TypeError):
        return []
    out: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            branch, num = r.get('headRefName'), r.get('number')
            if (isinstance(branch, str) and branch.startswith(NEWMISSION_BRANCH_PREFIX)
                    and isinstance(num, int)):
                out.append({'number': num, 'branch': branch})
    return out


def fetch_branch_mission_entry(repo_full: str, branch: str,
                               mission_id: str) -> Optional[dict[str, Any]]:
    """Fetch the branch's missions.json (GitHub contents API) and return the single
    entry whose id == mission_id, or None (gh failure / not found / malformed).

    Only the ONE named entry is read — never the whole stale file — so the branch
    being behind main doesn't matter, and a PR can't smuggle edits to OTHER
    missions: we append exactly the mission its branch name claims."""
    proc = _gh('api', f'repos/{repo_full}/contents/{MISSIONS_REL}?ref={branch}',
               '--jq', '.content')
    if proc.returncode != 0:
        return None
    content = proc.stdout.replace('\n', '').strip()
    if not content:
        # The contents API returns empty `content` for a file over its inline-size
        # limit (~1MB). Log distinctly (not a transient retry) so an oversized
        # missions.json doesn't become a silent never-reconciled PR.
        log(f'new-mission branch {branch}: empty contents-API body for '
            f'{MISSIONS_REL} (file too large for the inline API?) — leaving open')
        return None
    try:
        import base64  # noqa: PLC0415
        data = json.loads(base64.b64decode(content))
    except Exception:  # noqa: BLE001 — any decode/parse failure => skip, retry next tick
        return None
    missions = data.get('missions') if isinstance(data, dict) else None
    if not isinstance(missions, list):
        return None
    for m in missions:
        if isinstance(m, dict) and m.get('id') == mission_id:
            return m
    return None


def _valid_mission_entry(entry: Any, mission_id: str) -> bool:
    """A minimally well-formed mission entry whose id matches the branch claim.
    task_ids (if present) must be a list — a non-list would corrupt the
    registered_task_ids idempotency set the propose path relies on."""
    return (isinstance(entry, dict)
            and entry.get('id') == mission_id
            and isinstance(entry.get('name'), str)
            and isinstance(entry.get('phase'), str)
            and isinstance(entry.get('task_ids', []), list))


def ingest_new_mission_prs(
    registry: dict[str, Any], repo_full: str,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Append the mission named by each open `feat/new-mission-*` PR into the
    in-memory registry (append-only, dedup by id, bounded), and report which PRs
    are now fully reconciled and safe to close.

    Returns (ingested, closeable): `ingested` = (pr, id) actually appended this
    tick; `closeable` = (pr, id) whose mission is present on main (just-appended OR
    already there) and so can be closed once the registry is committed. A PR whose
    branch content can't be fetched, or whose named entry is missing/malformed, is
    left open (not closeable) for a human."""
    ingested: list[tuple[int, str]] = []
    closeable: list[tuple[int, str]] = []
    prs = list_open_new_mission_prs(repo_full)
    if not prs:
        return ingested, closeable
    existing = existing_entry_ids(registry)
    missions = registry.setdefault('missions', [])
    for pr in prs[:NEWMISSION_MAX_PER_TICK]:
        num, branch = pr['number'], pr['branch']
        mission_id = branch[len(NEWMISSION_BRANCH_PREFIX):]
        if not mission_id:
            continue
        if mission_id in existing:
            closeable.append((num, mission_id))  # already on main → redundant PR
            continue
        entry = fetch_branch_mission_entry(repo_full, branch, mission_id)
        if entry is None:
            continue  # gh failure / not found → retry next tick
        if not _valid_mission_entry(entry, mission_id):
            log(f'new-mission PR #{num}: branch entry for `{mission_id}` '
                f'missing/malformed — leaving open for review')
            continue
        missions.append(entry)
        existing.add(mission_id)
        ingested.append((num, mission_id))
        closeable.append((num, mission_id))
    return ingested, closeable


def _missions_ids_on_main(repo: Path) -> set[str]:
    """Mission ids present in missions.json at origin/main — the PUSHED truth, not
    the local working tree. A successful push updates this local remote-tracking
    ref, so after this tick's commit it reflects just-ingested missions. Empty set
    on any git/parse failure => close nothing (fail toward leaving PRs open)."""
    proc = _git(repo, 'show', f'origin/main:{MISSIONS_REL}')
    if proc.returncode != 0:
        return set()
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return set()
    out: set[str] = set()
    if isinstance(data, dict) and isinstance(data.get('missions'), list):
        for m in data['missions']:
            if isinstance(m, dict) and isinstance(m.get('id'), str) and m['id']:
                out.add(m['id'])
    return out


def close_new_mission_pr(repo_full: str, number: int, mission_id: str) -> bool:
    """Close a reconciled +New mission PR (and delete its branch) with an audit
    comment. Best-effort: logs + returns False on failure (retry next tick)."""
    comment = (
        f'Reconciled mission `{mission_id}` into {MISSIONS_REL} on main via '
        f'heal_orphan_autoregister (the missions single-committer path), so this '
        f'+New mission PR is no longer needed — closing to clear the '
        f'pipeline-stall. The mission is registered on main.')
    proc = _gh('pr', 'close', str(number), '--repo', repo_full,
               '--delete-branch', '--comment', comment)
    if proc.returncode != 0:
        log(f'close new-mission PR #{number} failed: {(proc.stderr or "").strip()[:200]}')
        return False
    return True


# ---------- new-mission QUEUE drain (the PRIMARY +New mission source) ----------


def _new_mission_queue_dir() -> Path:
    """Dir the dashboard +New mission flow drops queued `<id>.json` entries into
    for this healer to drain. Keyed off `_agents_root()` (OURLIBERTY_AGENTS_ROOT)
    so it resolves to the SAME path dashboard_api._new_mission_queue_dir writes,
    and so tests redirect both via one env override."""
    return _agents_root().joinpath(*NEWMISSION_QUEUE_SUBPATH)


def list_queued_new_missions(queue_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every `*.json` file in the +New mission queue, as (path, parsed entry),
    name-sorted for deterministic order. Missing dir → []. A malformed / non-object
    / unreadable file is logged and SKIPPED (left in place for a human) — never
    crashes the tick. `*.tmp` files (an in-flight atomic write) are ignored."""
    out: list[tuple[Path, dict[str, Any]]] = []
    try:
        if not queue_dir.is_dir():
            return out
        paths = sorted(
            p for p in queue_dir.iterdir()
            if p.is_file() and p.suffix == '.json'
        )
    except OSError as e:
        log(f'new-mission queue list failed ({queue_dir}): {e}')
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f'new-mission queue file {path.name} unreadable/malformed: {e} '
                f'— leaving in place')
            continue
        if not isinstance(data, dict):
            log(f'new-mission queue file {path.name} is not a JSON object '
                f'— leaving in place')
            continue
        out.append((path, data))
    return out


def drain_new_mission_queue(
    registry: dict[str, Any], queue_dir: Path,
) -> tuple[list[str], list[tuple[Path, str]]]:
    """Append each queued +New mission entry into the in-memory registry
    (append-only, dedup by id, bounded), and report which queue files are now
    safe to remove.

    Returns (drained, deletable): `drained` = ids actually appended this tick;
    `deletable` = (path, id) whose mission is present on main (just-appended OR
    already there) and so can be removed once the registry is committed. A file
    with no string id, a malformed entry, is left in place for a human.

    The filename is advisory only — the AUTHORITATIVE id is `entry['id']`
    (validated well-formed by _valid_mission_entry), so a misnamed file can't
    smuggle a mismatched id, and we append exactly the entry the dashboard wrote
    (preserving brief/spec_docs/repo/…)."""
    drained: list[str] = []
    deletable: list[tuple[Path, str]] = []
    files = list_queued_new_missions(queue_dir)
    if not files:
        return drained, deletable
    existing = existing_entry_ids(registry)
    missions = registry.setdefault('missions', [])
    for path, entry in files[:NEWMISSION_MAX_PER_TICK]:
        mission_id = entry.get('id')
        if not isinstance(mission_id, str) or not mission_id:
            log(f'new-mission queue file {path.name}: entry has no string `id` '
                f'— leaving in place')
            continue
        if mission_id in existing:
            deletable.append((path, mission_id))  # already on main → redundant file
            continue
        if not _valid_mission_entry(entry, mission_id):
            log(f'new-mission queue file {path.name}: entry for `{mission_id}` '
                f'malformed — leaving in place for review')
            continue
        missions.append(entry)
        existing.add(mission_id)
        drained.append(mission_id)
        deletable.append((path, mission_id))
    return drained, deletable


def delete_queue_file(path: Path) -> bool:
    """Remove a drained +New mission queue file. Best-effort: an already-gone file
    is success; logs + returns False on any other error (retry next tick)."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log(f'delete new-mission queue file {path.name} failed: {e}')
        return False


def _emit_summary(res: ProposeResult, commit_status: str, dry_run: bool) -> None:
    """One audit line (log) + a low-noise digest alert when something was proposed;
    escalate on a hard failure. Exact counts + the full id list go to the log."""
    proposed_ids = [eid for _, eid in res.proposed]
    retired_ids = [eid for eid, _ in res.retired]
    ingested_ids = [mid for _, mid in res.ingested]
    verb = 'would propose' if dry_run else 'proposed'
    retire_verb = 'would retire' if dry_run else 'retired'
    summary = (
        f'missions-autoregister: {verb} {len(res.proposed)} orphan thread(s) '
        f'{proposed_ids}; {retire_verb} {len(res.retired)} stale proposal(s) '
        f'{retired_ids}; flagged {len(res.flagged_stuck)} stuck-for-decision '
        f'{res.flagged_stuck}; drained {len(res.queue_drained)} queued mission(s) '
        f'{res.queue_drained}; ingested {len(res.ingested)} new-mission PR(s) '
        f'{ingested_ids}; surviving proposed={len(res.surviving_proposed)} '
        f'{res.surviving_proposed}; scanned {res.scanned_orphans} orphan(s); '
        f'skipped {res.skipped_non_proposable} non-proposable, '
        f'{res.skipped_terminal_or_indeterminate} terminal/indeterminate; '
        f'commit={commit_status}'
    )
    if res.events_unavailable:
        summary += '; chain_events unavailable (skipped)'
    if res.registry_unreadable:
        summary += '; missions.json unreadable (skipped)'
    if res.derive_unavailable:
        summary += '; derive unavailable (skipped)'
    log(summary)

    if dry_run:
        return
    try:
        import larry_alerts  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        log(f'larry_alerts unavailable: {e}')
        return

    hard_failure = commit_status in ('wrong-branch', 'commit-failed', 'push-failed')
    if hard_failure:
        larry_alerts.append_alert(
            source='missions-autoregister', severity='warning',
            message=summary, subject=f'failure:{commit_status}', route='escalate')

    # Low-noise digest (never an escalation DM) when cards newly need a decision —
    # the residue that auto-retirement can't resolve, surfaced ONCE per newly-flagged
    # set (idempotent flagging means a quiet lane re-alerts nothing next tick).
    if res.flagged_stuck:
        larry_alerts.append_alert(
            source='missions-autoregister', severity='info',
            message=(f'{len(res.flagged_stuck)} proposed card(s) have sat past '
                     f'{STUCK_PROPOSAL_TTL_DAYS}d with no shipped-PR match and need a '
                     f'keep/drop decision: {res.flagged_stuck}'),
            subject='proposed:needs-decision', route='digest')


# ---------- main ----------


def _default_events_fetcher() -> Optional[list[dict[str, Any]]]:
    """Fetch all chain_events in the orphan window, newest-first — mirrors
    dashboard_api._fetch_recent_chain_events but via chain_event_emit's client (the
    same path the GC healer uses). None on an unavailable client or read error so
    the caller skips the tick (fail-safe)."""
    import chain_event_emit  # noqa: PLC0415
    from datetime import timedelta  # noqa: PLC0415
    cli = chain_event_emit._get_client()
    if cli is None:
        return None
    derive = load_derive()
    window_days = getattr(derive, '_ORPHAN_WINDOW_DAYS', 30) if derive else 30
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        resp = (
            cli.table('chain_events')
            .select('event_type,task_id,agent,pr_url,ts,payload')
            .gte('ts', since)
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — read must never crash the tick
        log(f'chain_events read failed: {type(e).__name__}: {e}')
        return None
    return list(getattr(resp, 'data', None) or [])


def run_once(*, dry_run: bool,
             events_fetcher: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None,
             derive: Optional[Any] = None,
             pr_state_resolver: Optional[Callable[[list[str]], dict[str, str]]] = None,
             now: Optional[datetime] = None) -> int:
    """One healer tick. The injectable seams (events_fetcher / derive /
    pr_state_resolver / now) keep the effectful edges test-controllable; production
    resolves them from chain_event_emit + dashboard_api's derive."""
    now = now or datetime.now(timezone.utc)
    repo_paths = load_repo_paths()

    mpath = missions_path(repo_paths)
    if mpath is None:
        log('missions.json path unresolved (agent-core not in repo_paths) — skipping')
        _emit_summary(ProposeResult(registry_unreadable=True), 'nothing', dry_run)
        return 0

    registry = read_missions_registry(mpath)
    if registry is None:
        _emit_summary(ProposeResult(registry_unreadable=True), 'nothing', dry_run)
        return 0
    # Read-time snapshot so the pre-write merge can apply ONLY this tick's work
    # onto a fresh re-read (§ 2 lost-update guard — same shape as
    # heal_missions_card_gc's captures/missions write paths).
    before_by_id = _snapshot_missions_by_id(registry)

    # Propose/retire both need the derive module AND chain_events. If either is
    # unavailable, SKIP those passes (recording why) but fall through to the
    # new-mission ingestion below — ingestion is independent of chain_events, so a
    # chain_events outage must not strand open +New mission PRs. (Each pass is
    # isolated so a fault in one never blocks the others or corrupts the registry.)
    res = ProposeResult()  # default; reassigned only on a successful propose pass
    if derive is None:
        derive = load_derive()
    if derive is None:
        res.derive_unavailable = True
        log('derive unavailable — proposing/retiring nothing this tick')
    else:
        if events_fetcher is None:
            events_fetcher = _default_events_fetcher
        try:
            rows = events_fetcher()
        except Exception as e:  # noqa: BLE001 — fail-safe
            log(f'event fetch raised: {type(e).__name__}: {e}')
            rows = None
        if rows is None:
            res.events_unavailable = True
            log('chain_events unavailable — proposing/retiring nothing this tick')
        else:
            # Read the build sequences ONCE per tick: ids OWNED by any registered
            # sequence (seq_id + step ids) are collapsed out of the orphan derive
            # (never auto-proposed) and trigger the 'sequence-owned' retire of any
            # already-live proposal — the SAME suppression the board applies.
            # Fail-safe: a sequence-store fault degrades to an empty set.
            sequence_owned = sequence_owned_task_ids(derive, now)
            try:
                res = scan_and_propose(registry, rows, derive, now,
                                       pr_state_resolver=pr_state_resolver,
                                       collapsed_task_ids=sequence_owned)
            except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                log(f'scan-and-propose raised: {type(e).__name__}: {e} — proposing nothing')
                # scan_and_propose appends to registry in place, so a mid-loop raise
                # can leave partial proposals in memory. Re-read the clean on-disk copy
                # (disk was never written) so a same-tick ingest doesn't commit
                # half-applied propose work — preserving the old early-return's "scan
                # failure persists nothing" guarantee.
                registry = read_missions_registry(mpath) or registry
                # Re-baseline the lost-update snapshot on the (re-)read registry:
                # on a successful re-read it matches the clean disk copy; on a
                # failed one it baselines the partial proposals as pre-existing,
                # so the pre-write merge neither deltas nor appends them.
                before_by_id = _snapshot_missions_by_id(registry)
            else:
                # Build the reliable terminal-PR gate ONCE per tick (a single deep
                # gh fetch) and share it across BOTH the retire pass and the stuck-
                # proposal flag pass below — never fetch the PR index twice. None
                # when gh is unavailable (both passes then degrade fail-safe).
                terminal_gate = _load_terminal_gate()
                # Retire only after a successful propose (as before); isolated so a
                # retirement fault never blocks a healthy propose pass.
                try:
                    res.retired = retire_stale_proposals(
                        registry, rows, derive, now,
                        pr_state_resolver=pr_state_resolver,
                        terminal_gate=terminal_gate,
                        collapsed_task_ids=sequence_owned)
                except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                    log(f'retire-stale-proposals raised: {type(e).__name__}: {e} — retiring nothing')
                # Surface the residue: proposed cards stuck past the TTL with no
                # terminal-PR match get a `needs_decision` flag (a human keep/drop)
                # so they leave the silent backlog. Only when the gate is live — a
                # gh outage must never mass-flag a transient miss (fail-safe).
                if terminal_gate is not None:
                    try:
                        res.flagged_stuck = flag_stuck_proposals(
                            registry, now, terminal_gate=terminal_gate)
                    except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                        log(f'flag-stuck-proposals raised: {type(e).__name__}: {e} — flagging nothing')
                res.surviving_proposed = surviving_proposed_ids(registry)

    # New-mission registration: drain the queue (PRIMARY) + reconcile any open
    # feat/new-mission-* PRs (transitional BACKSTOP) so each named mission lands
    # via this healer's single-committer commit (not a stale-branch merge / a
    # dashboard co-commit). Each source is isolated + fail-safe like the passes
    # above — a fault here proposes/retires normally and registers nothing.
    # Gated by the same soak flag (one kill switch); when disabled, observe + log
    # pending work in BOTH sources so a queue can't silently pile up while off.
    if not dry_run and newmission_ingest_enabled():
        try:
            res.queue_drained, res.deletable_queue = drain_new_mission_queue(
                registry, _new_mission_queue_dir())
        except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
            log(f'new-mission queue drain raised: {type(e).__name__}: {e} — draining nothing')
        try:
            res.ingested, res.closeable_prs = ingest_new_mission_prs(
                registry, _missions_repo_full())
        except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
            log(f'new-mission PR ingest raised: {type(e).__name__}: {e} — ingesting nothing')
    elif not dry_run:
        try:
            pending_q = list_queued_new_missions(_new_mission_queue_dir())
            if pending_q:
                log(f'new-mission queue drain DISABLED: {len(pending_q)} queued '
                    f'mission(s) {[p.name for p, _ in pending_q]} would be '
                    f'registered — set {ENV_NEWMISSION_INGEST}=true to act')
            pending = list_open_new_mission_prs(_missions_repo_full())
            if pending:
                log(f'new-mission PR ingest DISABLED: {len(pending)} open '
                    f'feat/new-mission-* PR(s) '
                    f'{[p["number"] for p in pending]} would be reconciled — set '
                    f'{ENV_NEWMISSION_INGEST}=true to act')
        except Exception as e:  # noqa: BLE001 — observe is best-effort
            log(f'new-mission observe raised: {type(e).__name__}: {e}')

    commit_status = 'nothing'
    if (res.proposed or res.retired or res.ingested or res.queue_drained
            or res.flagged_stuck) and not dry_run:
        # Lost-update guard (mirrors heal_missions_card_gc's missions write
        # path): the passes above can hold the read-time snapshot across
        # chain_events + gh round-trips, during which a separate process (the
        # dashboard's delegate `spawned` stamp, the GC healer's phase flips /
        # briefings) may write missions.json. Re-read FRESH and apply only this
        # tick's work — field deltas on pre-existing entries plus appends for
        # the entries this tick authored — so the read→write window is
        # sub-millisecond instead of the length of the round-trips. A malformed
        # re-read skips the write (never clobber the file with a stale copy).
        try:
            fresh = read_missions_registry(mpath)
            if fresh is None:
                log('missions.json re-read malformed before write — skipping '
                    'write this tick (avoids clobbering with stale)')
                _emit_summary(res, 'commit-failed', dry_run)
                return 0
            merged, appended = _merge_mission_deltas(fresh, before_by_id, registry)
            atomic_write_missions(mpath, fresh)
            log(f'missions.json write: merged {merged} delta(s) + appended '
                f'{appended} mission(s) onto a fresh re-read (lost-update guard)')
        except Exception as e:  # noqa: BLE001 — fail-safe
            log(f'missions.json write raised: {type(e).__name__}: {e}')
            _emit_summary(res, 'commit-failed', dry_run)
            return 0
        core = repo_paths.get('ourliberty-agent-core')
        if core:
            try:
                commit_status = commit_and_push_missions(core, _commit_audit(res))
            except Exception as e:  # noqa: BLE001 — fail-safe
                log(f'commit+push raised: {type(e).__name__}: {e}')
                commit_status = 'push-failed'

    # Clean up each reconciled +New mission source whose mission is CONFIRMED
    # present on origin/main (the pushed truth — read from the remote-tracking ref,
    # not the local working tree). Robust to every state: a just-registered mission
    # is on main only after this tick's push landed it; a redundant dup is already
    # there; a not-yet-pushed mission (push failed) is absent so we leave its
    # PR/queue-file for the next tick. We never delete/close a source whose mission
    # isn't durably on main.
    core = repo_paths.get('ourliberty-agent-core')
    if (res.closeable_prs or res.deletable_queue) and not dry_run and core:
        on_main = _missions_ids_on_main(core)
        repo_full = _missions_repo_full()
        for num, mid in res.closeable_prs:
            if mid not in on_main:
                continue  # not on main yet — retry next tick
            try:
                close_new_mission_pr(repo_full, num, mid)
            except Exception as e:  # noqa: BLE001 — fail-safe
                log(f'close new-mission PR #{num} raised: {type(e).__name__}: {e}')
        for path, mid in res.deletable_queue:
            if mid not in on_main:
                continue  # not on main yet — retry next tick
            try:
                delete_queue_file(path)
            except Exception as e:  # noqa: BLE001 — fail-safe
                log(f'delete new-mission queue file {path.name} raised: '
                    f'{type(e).__name__}: {e}')

    _emit_summary(res, commit_status, dry_run)
    return 0


def _commit_audit(res: ProposeResult) -> str:
    return (f'Auto-committed by {PROPOSED_BY}. '
            f'proposed={len(res.proposed)} retired={len(res.retired)} '
            f'flagged-stuck={len(res.flagged_stuck)} '
            f'drained-new-mission-queue={len(res.queue_drained)} '
            f'ingested-new-mission-prs={len(res.ingested)} '
            f'scanned={res.scanned_orphans} surviving={len(res.surviving_proposed)}.')


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='heal_orphan_autoregister.py',
        description='Missions v2 orphan auto-registration healer: scan non-terminal, '
                    'non-infrastructure orphans and propose phase=proposed missions.json '
                    'entries (idempotent + fail-safe).')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report what WOULD be proposed; append nothing, write nothing, commit '
             'nothing.')
    args = parser.parse_args(argv)

    if _kill_switch_path().exists():
        log('KILLED_BY_SWITCH: healers.disabled present, exiting')
        return 0

    mode = 'DRY-RUN' if args.dry_run else 'LIVE'
    log(f'Starting missions orphan-autoregister ({mode})')
    rc = run_once(dry_run=args.dry_run)
    log('Done.')
    return rc


if __name__ == '__main__':
    sys.exit(main())
