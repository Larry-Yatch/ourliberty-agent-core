#!/usr/bin/env python3
"""system_state_log.py — System self-awareness, Slice 1: the work-in-flight State Log.

Spec: `agents/beacon/specs/system-awareness-slice-1-state-log.md`.

Stands up the first **State Log**: a continuously-updated, plain-English picture
of *all work currently in flight* across the system — active missions and the
build/PR status underneath them — built on a schedule and readable by Beacon so
Larry can ask "where are we on work in flight?" and get a current, trustworthy
answer without summoning Claude.

This is the whole-system sibling of the per-card missions Narrator. It REUSES,
does not reinvent:

  * `missions_narrator.generate_briefing_voice` — the claude CLI round-trip +
    tolerant JSON parse + deterministic-fallback contract — to author the prose,
    plus `NARRATOR_MODEL` for provenance.
  * `task_terminal_state` — the OPEN/MERGED/CLOSED/UNKNOWN gh probe — for
    per-task pipeline status (we do NOT re-derive phase). Its matching kernel
    (`expand_variants` / `_pr_matches` / `classify_state` / `_combine`) is reused
    to reduce a SINGLE per-repo PR listing across many task ids, so a tick's gh
    cost is bounded by repo count, not by task count.
  * `atomic_io.atomic_write_json` — for the atomic write.
  * `heal_missions_card_gc` path/registry readers — to locate + read
    missions.json / captures.json the same way the rest of the chain does.

INVARIANTS (spec § 3):

  * The output `~/agents/blackboard/system-state-log.json` is **droplet runtime
    state, NOT git-committed** (it lives under the agents blackboard, like the
    other blackboard files). This narrator is its SOLE writer.
  * It is **read-only** on missions.json / captures.json / beacon-pending-
    approvals.json / for-Larry escalations / the in-flight + build sequence state
    — it NEVER mutates them (preserves the single-committer rule).
  * **Bounded + fail-safe:** at most one LLM call per tick; the gh probe lists
    each repo once; any single read failure degrades that section to empty,
    never aborts the whole log; the prose always falls back to deterministic
    bullets (never empty, never crash).

stdlib + the reused helpers only. No HTTP; no Supabase dependency (chain_events
texture is deliberately out of this slice).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Sibling scripts/ on path so imports resolve under systemd, the GC tick, or tests.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import task_terminal_state as tts  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402
from heal_missions_card_gc import (  # noqa: E402
    captures_path,
    load_repo_paths,
    log,
    missions_path,
    read_captures_registry,
    read_missions_registry,
)
from missions_narrator import NARRATOR_MODEL, generate_briefing_voice  # noqa: E402

SCHEMA_VERSION = 1
NARRATOR_BY = 'system-state-narrator'

# A mission is "in flight" for the State Log when its phase is one of these AND
# it is not archived/retired (spec § 3: exclude terminal + proposed-funnel).
ACTIVE_MISSION_PHASES = frozenset({'drafting', 'in_flight', 'ready'})

# Upper bound on per-task gh-derived pipeline probes per tick. The probe lists
# each repo ONCE regardless, but we still cap the number of task ids we reduce so
# a pathological mission list can't blow the snapshot up. Tasks beyond the cap
# render as 'unknown' (KEEP-shaped — never a false terminal).
MAX_TASK_PROBES = 60

# Cap on the itemized waiting-on-Larry list (Slice 2a, spec § 2 D2). Items past
# the cap are dropped from `items` but still counted in the totals; `truncated`
# flags it so the surface can say "+N more".
WAITING_ITEMS_CAP = 25

# A build-sequence step that has been `dispatched` (handed to Forge) for longer
# than this with no PR opened is "stuck" enough to surface as a needs-you row
# (operator-needs-you-feed spec §5.4). Hardcoded v1 per the spec — a Pulse Check
# sibling can propose tuning later.
STUCK_DISPATCHED_THRESHOLD_SEC = 15 * 60

# Severity ordering for the waiting list — most urgent first. None sorts last.
_SEVERITY_RANK = {'critical': 0, 'warning': 1, 'info': 2, None: 3}

# Pipeline status labels (spec § 3 structured_snapshot).
ST_BUILDING = 'building'
ST_IN_REVIEW = 'in_review'
ST_MERGED = 'merged'
ST_STUCK = 'stuck'
ST_UNKNOWN = 'unknown'


# ---------------- path resolvers (env-overridable for tests) ----------------


def _agents_root() -> Path:
    """Droplet agents root (mirrors dashboard_api._agents_root / the bot)."""
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def state_log_path() -> Path:
    """Where the State Log is written. `OURLIBERTY_SYSTEM_STATE_LOG` overrides
    (test redirection); otherwise the agents blackboard — NOT inside the git
    checkout, so it is never committed (single-committer invariant)."""
    override = os.environ.get('OURLIBERTY_SYSTEM_STATE_LOG')
    if override:
        return Path(override)
    return _agents_root() / 'blackboard' / 'system-state-log.json'


def in_flight_dir() -> Path:
    """Dir of per-task in-flight sentinels. `OURLIBERTY_IN_FLIGHT_DIR` overrides."""
    override = os.environ.get('OURLIBERTY_IN_FLIGHT_DIR')
    if override:
        return Path(override)
    return _agents_root() / 'state' / 'in-flight'


def build_sequences_dir() -> Path:
    """Dir of multi-step build-sequence files. `OURLIBERTY_BUILD_SEQUENCES_DIR`
    overrides."""
    override = os.environ.get('OURLIBERTY_BUILD_SEQUENCES_DIR')
    if override:
        return Path(override)
    return _agents_root() / 'blackboard' / 'build-sequences'


def pending_approvals_path() -> Path:
    """Beacon's pending-approvals state file (read-only here). The SAME file
    `scripts/beacon_approval_handler.py` owns. `OURLIBERTY_PENDING_APPROVALS`
    overrides for tests; otherwise the agents state dir."""
    override = os.environ.get('OURLIBERTY_PENDING_APPROVALS')
    if override:
        return Path(override)
    return _agents_root() / 'state' / 'beacon-pending-approvals.json'


def escalations_path() -> Path:
    """For-Larry escalations source (read-only). `OURLIBERTY_ESCALATIONS_FILE`
    overrides for tests; otherwise the pulse-escalations blackboard file."""
    override = os.environ.get('OURLIBERTY_ESCALATIONS_FILE')
    if override:
        return Path(override)
    return _agents_root() / 'blackboard' / 'pulse-escalations.json'


# ---------------- read helpers (each fail-safe to empty) ----------------


def _normalize_severity(raw: Any) -> Optional[str]:
    """Map a source's free-form severity onto the waiting-item enum
    (critical / warning / info / None)."""
    s = str(raw).strip().lower() if raw is not None else ''
    if s in ('critical', 'crit', 'fatal'):
        return 'critical'
    if s in ('warning', 'warn', 'high', 'medium'):
        return 'warning'
    if s in ('info', 'low', 'notice'):
        return 'info'
    return None


def _age_seconds(ts: Any, now: datetime) -> int:
    """Whole seconds between an ISO-8601 timestamp and `now`, fail-safe to 0.
    Tolerates a trailing 'Z' and naive timestamps (assumed UTC)."""
    if not isinstance(ts, str) or not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (now - dt).total_seconds()
    return int(delta) if delta > 0 else 0


def _mission_is_active(mission: dict[str, Any]) -> bool:
    """True for a mission the State Log should narrate: an active build phase
    that is not archived/retired/acknowledged. Mirrors the funnel-vs-active split
    the dashboard + narrator already use, without importing either heavy module."""
    if not isinstance(mission, dict):
        return False
    if (mission.get('phase') or '') not in ACTIVE_MISSION_PHASES:
        return False
    if mission.get('archived') is True:
        return False
    if mission.get('acknowledged') is True:
        return False
    if mission.get('retired_at'):
        return False
    return True


def load_active_missions() -> list[dict[str, Any]]:
    """Active missions from missions.json (read-only), fail-safe to []. Reuses
    the chain's path resolver + registry reader so we read it exactly as the GC
    healer does."""
    try:
        path = missions_path(load_repo_paths())
        if path is None:
            return []
        registry = read_missions_registry(path)
    except Exception as e:  # noqa: BLE001 — a read failure degrades, never aborts
        log(f'state-log: missions read failed: {type(e).__name__}: {e}')
        return []
    if not isinstance(registry, dict):
        return []
    missions = registry.get('missions')
    if not isinstance(missions, list):
        return []
    return [m for m in missions if _mission_is_active(m)]


def load_in_flight() -> list[str]:
    """Identifiers of tasks actively dispatched right now (one sentinel file per
    task under the in-flight dir). Uses `task_stem` when present, else the file
    stem. Fail-safe to []."""
    d = in_flight_dir()
    out: list[str] = []
    try:
        if not d.is_dir():
            return []
        for p in sorted(d.glob('*.json')):
            stem = None
            try:
                import json  # local import keeps the module top stdlib-light
                data = json.loads(p.read_text())
                if isinstance(data, dict):
                    stem = data.get('task_stem') or data.get('task_id')
            except (OSError, ValueError):
                stem = None
            out.append(str(stem) if stem else p.stem)
    except OSError as e:
        log(f'state-log: in-flight read failed: {type(e).__name__}: {e}')
        return []
    return out


def load_active_sequences() -> list[dict[str, Any]]:
    """Active/paused multi-step sequences, summarized as {seq_id, step:"N/M",
    status}. Fail-safe to []. Mirrors catch_me_up.fetch_active_sequences's filter
    without importing its module-level SEQUENCES_DIR constant (we honor our own
    env-overridable resolver)."""
    import json  # local import; sequence read is a cold path
    d = build_sequences_dir()
    out: list[dict[str, Any]] = []
    try:
        if not d.is_dir():
            return []
        for p in sorted(d.glob('*.json')):
            try:
                seq = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(seq, dict):
                continue
            if seq.get('status') not in ('active', 'paused'):
                continue
            steps = seq.get('steps') if isinstance(seq.get('steps'), list) else []
            total = len(steps)
            done = sum(1 for s in steps
                       if isinstance(s, dict) and s.get('status') == 'merged')
            out.append({
                'seq_id': seq.get('seq_id') or p.stem,
                'step': f'{done}/{total}',
                'status': seq.get('status'),
            })
    except OSError as e:
        log(f'state-log: sequences read failed: {type(e).__name__}: {e}')
        return []
    return out


def _last_audit_ts(seq: dict[str, Any], event: Optional[str] = None) -> Any:
    """Timestamp of the last audit_log entry (optionally filtered to `event`).
    Returns None when none match — the age then degrades to 0 downstream."""
    last = None
    for entry in seq.get('audit_log') or []:
        if not isinstance(entry, dict):
            continue
        if event is not None and entry.get('event') != event:
            continue
        ts = entry.get('ts')
        if ts:
            last = ts
    return last


def load_waiting_sequences(now: datetime) -> list[dict[str, Any]]:
    """Build-sequence rows that need Larry, as waiting-items (operator-needs-you-
    feed spec §5.3 + §5.4). Two producers, both `source='sequence'`:

      1. Each `paused` sequence → one item with Resume / Cancel actions.
      2. Each `dispatched` step with no PR whose `dispatched_at` is older than
         STUCK_DISPATCHED_THRESHOLD_SEC → one item with Skip / Cancel actions.

    Read-only on the build-sequences dir; fail-safe to []. Each item carries the
    `seq_id` (and `step_id` for step rows) the steering endpoint needs, an
    `actions` allowlist the render uses for buttons, and a private `_ts` that
    build_snapshot turns into `age_seconds`. `now` is injected (not read inline)
    so the >15-min threshold and the displayed age share one clock."""
    import json  # local import; sequence read is a cold path
    d = build_sequences_dir()
    out: list[dict[str, Any]] = []
    try:
        if not d.is_dir():
            return []
        for p in sorted(d.glob('*.json')):
            try:
                seq = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(seq, dict):
                continue
            seq_id = seq.get('seq_id') or p.stem
            status = seq.get('status')

            if status == 'paused':
                out.append({
                    'source': 'sequence',
                    'id': seq_id,
                    'seq_id': seq_id,
                    'step_id': None,
                    'title': seq_id,
                    'why': 'Paused — waiting on you to resume or cancel',
                    'severity': 'warning',
                    'action_hint': 'resume/cancel in Build Sequences',
                    'actions': ['resume', 'cancel'],
                    '_ts': _last_audit_ts(seq, 'paused') or _last_audit_ts(seq),
                })

            # Stuck-dispatched steps surface regardless of sequence status: a
            # step left `dispatched` with no PR is the signal, whether the
            # sequence is active or (e.g.) auto-paused around it.
            steps = seq.get('steps') if isinstance(seq.get('steps'), list) else []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                if step.get('status') != 'dispatched' or step.get('pr_url'):
                    continue
                age = _age_seconds(step.get('dispatched_at'), now)
                if age <= STUCK_DISPATCHED_THRESHOLD_SEC:
                    continue
                step_id = step.get('step_id') or '?'
                mins = age // 60
                out.append({
                    'source': 'sequence',
                    'id': f'{seq_id}/{step_id}',
                    'seq_id': seq_id,
                    'step_id': step_id,
                    'title': f'{seq_id} / {step_id}',
                    'why': (f'Step dispatched {mins}m ago with no PR — '
                            f'may be stuck'),
                    'severity': 'warning',
                    'action_hint': 'skip/cancel in Build Sequences',
                    'actions': ['skip', 'cancel'],
                    '_ts': step.get('dispatched_at'),
                })
    except OSError as e:
        log(f'state-log: waiting-sequences read failed: {type(e).__name__}: {e}')
        return []
    return out


def load_parked_items() -> list[dict[str, Any]]:
    """Parked captures awaiting Larry, as waiting-items (the itemized form of
    load_parked_count — same read). Read-only on captures.json; fail-safe to [].
    Each item carries a private `_ts` (raw timestamp) that build_snapshot turns
    into `age_seconds` against the injected `now`."""
    try:
        path = captures_path(load_repo_paths())
        if path is None:
            return []
        registry = read_captures_registry(path)
    except Exception as e:  # noqa: BLE001 — degrade, never abort
        log(f'state-log: captures read failed: {type(e).__name__}: {e}')
        return []
    if not isinstance(registry, dict):
        return []
    captures = registry.get('captures')
    if not isinstance(captures, list):
        return []
    items: list[dict[str, Any]] = []
    for c in captures:
        if not isinstance(c, dict) or c.get('state') != 'parked':
            continue
        origin = c.get('origin') if isinstance(c.get('origin'), dict) else {}
        items.append({
            'source': 'parked',
            'id': str(c.get('id') or ''),
            'title': str(c.get('title') or c.get('id') or 'parked item'),
            'why': str(c.get('briefing') or c.get('note') or ''),
            'severity': None,
            'action_hint': 'promote/drop in Missions',
            '_ts': c.get('last_touched') or origin.get('captured_at'),
        })
    return items


def load_parked_count() -> int:
    """Count of parked captures awaiting Larry (the "what's waiting on you" line).
    Read-only on captures.json; fail-safe to 0. Itemized companion:
    load_parked_items()."""
    return len(load_parked_items())


def load_pending_approvals() -> list[dict[str, Any]]:
    """Pending approvals awaiting Larry, as waiting-items. Read-only on Beacon's
    beacon-pending-approvals.json `pending[]` (the file owned by
    scripts/beacon_approval_handler.py). Fail-open to []."""
    import json  # local import keeps the module top stdlib-light
    path = pending_approvals_path()
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log(f'state-log: pending-approvals read failed: {type(e).__name__}: {e}')
        return []
    if not isinstance(data, dict):
        return []
    pending = data.get('pending')
    if not isinstance(pending, list):
        return []
    items: list[dict[str, Any]] = []
    for e in pending:
        if not isinstance(e, dict):
            continue
        if e.get('status') not in (None, 'pending'):
            continue
        summary = (e.get('plan_summary') or '').strip()
        target = e.get('target_agent') or 'forge'
        items.append({
            'source': 'approval',
            'id': str(e.get('id') or ''),
            'title': summary or str(e.get('id') or 'pending approval'),
            'why': f'Dispatch to {target} awaiting your approval.',
            'severity': 'warning',
            'action_hint': 'approve/reject in Approvals',
            '_ts': e.get('created_at'),
        })
    return items


def _escalation_to_waiting_item(e: dict[str, Any]) -> dict[str, Any]:
    """Normalize one for-Larry escalation record into a waiting-item.

    A record produced by heal_merged_pr_board_reconcile (a "looks shipped"
    off-board mission) carries a `mission_id` + `source` tag; pass those through
    plus a `mission_actions: ['confirm_shipped']` affordance so the Where-are-we
    needs-you row can render a one-click confirm that flips the mission to
    shipped. (Kept distinct from the sequence-row `actions` allowlist so the two
    vocabularies never mix.) Other escalations carry neither (read-only links as
    before)."""
    item: dict[str, Any] = {
        'source': 'escalation',
        'id': str(e.get('id') or e.get('headline') or ''),
        'title': str(e.get('headline') or e.get('id') or 'escalation'),
        'why': str(e.get('context') or e.get('suggested_action') or ''),
        'severity': _normalize_severity(e.get('severity')),
        'action_hint': 'review escalation',
        '_ts': e.get('ts') or e.get('created_at'),
    }
    mission_id = e.get('mission_id')
    if mission_id and e.get('source') == 'heal_merged_pr_board_reconcile':
        item['mission_id'] = str(mission_id)
        item['mission_actions'] = ['confirm_shipped']
        item['action_hint'] = 'confirm shipped'
    return item


def load_for_larry_escalations() -> list[dict[str, Any]]:
    """For-Larry escalations awaiting Larry, as waiting-items — CONSERVATIVE:
    only entries EXPLICITLY flagged for Larry (`for_larry: true`) and not yet
    resolved. Read-only; fail-open to [].

    Two sources fold in here (operator-needs-you-feed spec §5.1, decision c):

      1. The canonical durable for-Larry signal file
         (`for_larry_signal.active_entries`) — the substrate the promote_alerts
         gate (critical-unhandled) and the outbox_notifier CLARIFY-exhausted
         handler write to. This is the live producer of "needs you" rows.
      2. The ops-internal pulse-escalations.json — carries no for-Larry flag in
         production today (so it contributes NONE), but kept so a future
         for-Larry-flagged entry there appears with no schema change.

    Both sources share `_escalation_to_waiting_item`; both apply the same
    conservative `for_larry: true` + not-`resolved` filter."""
    import json  # local import; escalations read is a cold path

    items: list[dict[str, Any]] = []

    # Source 1: the canonical durable for-Larry signal (already filtered to
    # unresolved, for_larry records). Fail-open — a read error degrades to none.
    try:
        import for_larry_signal  # local import keeps the cold path cheap
        for e in for_larry_signal.active_entries():
            if isinstance(e, dict):
                items.append(_escalation_to_waiting_item(e))
    except Exception as exc:  # noqa: BLE001 — never abort the State Log tick
        log(f'state-log: for-larry signal read failed: '
            f'{type(exc).__name__}: {exc}')

    # Source 2: the legacy pulse-escalations.json for-Larry path (back-compat).
    path = escalations_path()
    try:
        if not path.is_file():
            return items
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log(f'state-log: escalations read failed: {type(e).__name__}: {e}')
        return items
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get('escalations'), list):
        entries = data['escalations']
    else:
        return items
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get('for_larry') is not True:   # conservative: explicit flag only
            continue
        if e.get('resolved') is True:
            continue
        items.append(_escalation_to_waiting_item(e))
    return items


def _finalize_waiting_items(
    escalations: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    sequences: list[dict[str, Any]],
    parked: list[dict[str, Any]],
    now: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    """Turn each source's raw waiting-items into the bounded, ordered cross-source
    list (spec § 2 D2): compute `age_seconds` from the private `_ts`, order
    most-urgent first (escalations → pending approvals → sequences → aged
    parked), and cap at WAITING_ITEMS_CAP. Returns (items, truncated)."""
    def aged(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for it in raw:
            item = dict(it)
            item['age_seconds'] = _age_seconds(item.pop('_ts', None), now)
            out.append(item)
        return out

    esc = aged(escalations)
    pend = aged(pending)
    seqs = aged(sequences)
    park = aged(parked)
    # Within escalations: severity first, then oldest. Others: oldest first.
    esc.sort(key=lambda i: (_SEVERITY_RANK.get(i.get('severity'), 3),
                            -i.get('age_seconds', 0)))
    pend.sort(key=lambda i: -i.get('age_seconds', 0))
    seqs.sort(key=lambda i: -i.get('age_seconds', 0))
    park.sort(key=lambda i: -i.get('age_seconds', 0))
    ordered = esc + pend + seqs + park
    truncated = len(ordered) > WAITING_ITEMS_CAP
    return ordered[:WAITING_ITEMS_CAP], truncated


# ---------------- pipeline probe (bounded, reuses the tts kernel) ----------------


def _bulk_terminal_states(
    task_ids: list[str],
    repos: Optional[list[str]] = None,
    *,
    limit: int = tts.DEFAULT_PR_LOOKBACK,
    timeout: float = tts.DEFAULT_GH_TIMEOUT_SEC,
    min_len: int = tts.MATCH_MIN_LEN,
) -> dict[str, str]:
    """Probe the terminal state (MERGED/CLOSED/OPEN/UNKNOWN) of each task id,
    listing each repo's PRs ONCE and reducing every task id against that single
    snapshot. This is the bounded form of calling `task_terminal_state` per id
    (which would re-list PRs for every task). Returns {task_id -> state}; any gh
    failure leaves a task at UNKNOWN (KEEP — never a guessed terminal, spec § 2
    conservative posture preserved)."""
    states = {tid: tts.UNKNOWN for tid in task_ids}
    if not task_ids:
        return states
    repo_list = repos if repos is not None else tts.default_repos()
    # One PR listing per repo (bounded by repo count, not task count).
    prs: list[dict[str, Any]] = []
    for repo in repo_list:
        data = tts.gh_json(
            [
                'gh', 'pr', 'list', '--repo', tts._qualify_repo(repo),
                '--state', 'all', '--limit', str(limit),
                '--json', 'number,state,title,headRefName,url',
            ],
            timeout=timeout,
        )
        if isinstance(data, list):
            prs.extend(p for p in data if isinstance(p, dict))
    for tid in task_ids:
        candidates = tts.expand_variants(tid)
        matched = [
            tts.classify_state(pr.get('state'))
            for pr in prs
            if tts._pr_matches(pr, candidates, min_len)
        ]
        if matched:
            states[tid] = tts._combine(matched)
    return states


def _default_pipeline_probe(task_ids: list[str]) -> dict[str, str]:
    """Default probe used by the live tick: the bounded bulk gh reduce. A tick
    caps total probes at MAX_TASK_PROBES; ids past the cap stay UNKNOWN."""
    capped = task_ids[:MAX_TASK_PROBES]
    return _bulk_terminal_states(capped)


def _map_pipeline_status(state: str, in_flight: bool) -> str:
    """Map a terminal-state probe result to a pipeline status label (spec § 3).

      MERGED                 -> merged
      OPEN  (a PR is open)   -> in_review
      CLOSED (no merge)      -> stuck   (closed without shipping — needs a look)
      UNKNOWN + in-flight    -> building (dispatched, no PR yet)
      UNKNOWN + not in-flight-> unknown
    """
    if state == tts.MERGED:
        return ST_MERGED
    if state == tts.OPEN:
        return ST_IN_REVIEW
    if state == tts.CLOSED:
        return ST_STUCK
    return ST_BUILDING if in_flight else ST_UNKNOWN


# ---------------- snapshot builder (pure) ----------------


def build_snapshot(
    *,
    missions: list[dict[str, Any]],
    in_flight: list[str],
    sequences: list[dict[str, Any]],
    parked: int,
    now: datetime,
    pipeline_probe: Callable[[list[str]], dict[str, str]],
    parked_items: Optional[list[dict[str, Any]]] = None,
    pending_approvals: Optional[list[dict[str, Any]]] = None,
    escalations: Optional[list[dict[str, Any]]] = None,
    waiting_sequences: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build the structured_snapshot (spec § 3) from already-loaded inputs. Pure:
    no IO beyond the injected `pipeline_probe` (which the live path backs with the
    bounded gh reduce; tests inject a fake). This is the mechanical, ground-truth
    layer the prose rides on — it must be accurate, so it is fully unit-tested."""
    parked_items = parked_items or []
    pending_approvals = pending_approvals or []
    escalations = escalations or []
    waiting_sequences = waiting_sequences or []
    in_flight_set = set(in_flight)

    # Gather every task id across all active missions, probe once in bulk.
    all_task_ids: list[str] = []
    for m in missions:
        tids = m.get('task_ids')
        if isinstance(tids, list):
            all_task_ids.extend(t for t in tids if isinstance(t, str) and t)
    # Dedup, order-preserving, so the probe (and its cap) sees each id once.
    seen: set[str] = set()
    unique_ids = [t for t in all_task_ids if not (t in seen or seen.add(t))]
    terminal_states = pipeline_probe(unique_ids) if unique_ids else {}

    def status_for(tid: str) -> str:
        return _map_pipeline_status(
            terminal_states.get(tid, tts.UNKNOWN), tid in in_flight_set)

    missions_active: list[dict[str, Any]] = []
    agg_building = agg_in_review = agg_merged = 0
    stuck: list[dict[str, Any]] = []
    for m in missions:
        tids = [t for t in (m.get('task_ids') or []) if isinstance(t, str) and t]
        tasks = [{'task_id': t, 'status': status_for(t)} for t in tids]
        n_building = sum(1 for t in tasks if t['status'] == ST_BUILDING)
        n_review = sum(1 for t in tasks if t['status'] == ST_IN_REVIEW)
        n_merged = sum(1 for t in tasks if t['status'] == ST_MERGED)
        agg_building += n_building
        agg_in_review += n_review
        agg_merged += n_merged
        for t in tasks:
            if t['status'] == ST_STUCK:
                stuck.append({'task_id': t['task_id'],
                              'why': 'PR closed without merging'})
        missions_active.append({
            'id': m.get('id'),
            'name': m.get('name'),
            'phase': m.get('phase'),
            'repo': m.get('repo'),
            'tasks': tasks,
            'rollup': f'{n_building} building / {n_review} in-review / '
                      f'{n_merged} merged',
        })

    return {
        'as_of': now.astimezone(timezone.utc).isoformat(),
        'missions_active': missions_active,
        'pipeline': {
            'building': agg_building,
            'in_review': agg_in_review,
            'merged_recent': agg_merged,
            'stuck': stuck,
        },
        'in_flight_now': len(in_flight),
        'sequences_active': sequences,
        'waiting_on_larry': _build_waiting_on_larry(
            parked=parked,
            parked_items=parked_items,
            pending_approvals=pending_approvals,
            escalations=escalations,
            sequences=waiting_sequences,
            now=now,
        ),
        'health': None,  # reserved — NOT this slice (Slice C).
    }


def _build_waiting_on_larry(
    *,
    parked: int,
    parked_items: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
    sequences: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Assemble the itemized cross-source `waiting_on_larry` block (Slice 2a, spec
    § 2 D2): per-source counts + a grand total + a bounded, ordered `items` list.
    `parked` is KEPT as a top-level count for back-compat with the current
    /where-we-are panel (which reads `waiting_on_larry.parked`)."""
    items, truncated = _finalize_waiting_items(
        escalations, pending_approvals, sequences, parked_items, now)
    return {
        'parked': parked,                          # KEEP — /where-we-are reads this
        'pending_approvals': len(pending_approvals),
        'escalations': len(escalations),
        'sequences': len(sequences),
        'total': (parked + len(pending_approvals) + len(escalations)
                  + len(sequences)),
        'items': items,
        'truncated': truncated,
    }


# ---------------- narrative (LLM voice + deterministic fallback) ----------------


def render_fallback_narrative(snapshot: dict[str, Any]) -> str:
    """Deterministic plain-English render of the snapshot as terse bullets — the
    fallback when the LLM voice is unavailable (and what the value tests assert
    against). Never empty: even a fully-idle system yields a one-line summary."""
    lines: list[str] = []
    missions = snapshot.get('missions_active') or []
    pipeline = snapshot.get('pipeline') or {}
    in_flight = snapshot.get('in_flight_now') or 0
    sequences = snapshot.get('sequences_active') or []
    waiting = snapshot.get('waiting_on_larry') or {}
    parked = waiting.get('parked') or 0
    pending_approvals = waiting.get('pending_approvals') or 0
    escalations = waiting.get('escalations') or 0
    waiting_sequences = waiting.get('sequences') or 0
    waiting_total = waiting.get('total') or 0

    if missions:
        lines.append(f'Active missions ({len(missions)}):')
        for m in missions:
            name = m.get('name') or m.get('id') or 'unnamed mission'
            repo = m.get('repo')
            where = f' [{repo}]' if repo else ''
            lines.append(f'  - {name}{where}: {m.get("rollup")}')
    else:
        lines.append('No active missions in flight.')

    lines.append(
        f'Pipeline: {pipeline.get("building", 0)} building, '
        f'{pipeline.get("in_review", 0)} in review, '
        f'{pipeline.get("merged_recent", 0)} merged.')

    stuck = pipeline.get('stuck') or []
    if stuck:
        lines.append(f'Stuck ({len(stuck)}):')
        for s in stuck:
            lines.append(f'  - {s.get("task_id")}: {s.get("why")}')

    lines.append(f'Tasks dispatched right now: {in_flight}.')

    if sequences:
        lines.append(f'Build sequences ({len(sequences)}):')
        for s in sequences:
            lines.append(
                f'  - {s.get("seq_id")} [{s.get("status")}] step {s.get("step")}')

    if waiting_total:
        lines.append(
            f'Waiting on you: {waiting_total} item(s) — '
            f'{pending_approvals} approval(s), {escalations} escalation(s), '
            f'{waiting_sequences} sequence(s), {parked} parked.')
    else:
        lines.append('Waiting on you: nothing parked.')

    return '\n'.join(lines)


def build_narrative_prompt(snapshot: dict[str, Any]) -> str:
    """Author-the-prose prompt over the structured snapshot. Larry's voice, plain
    English, blast-radius framing (spec § 3). JSON-out (one `narrative` key) so it
    rides the same tolerant-parse path as the missions narrator."""
    import json
    return (
        "You are the operator's chief of staff. Below is a structured snapshot of "
        "ALL work currently in flight across the system. Write a short, plain-"
        "English status the operator can read in seconds to answer "
        "\"where are we on work in flight?\" — what missions are active, what's "
        "building, what's in review, what's stuck, and what's waiting on him.\n\n"
        "Rules:\n"
        "  - Plain business English, never engineering jargon. Do NOT name task "
        "ids, branches, commits, or agents.\n"
        "  - Be accurate to the snapshot. If something is stuck or waiting on "
        "him, say so plainly. If the system is mostly idle, say that — never "
        "invent activity.\n"
        "  - A few sentences or short bullets. No preamble, no sign-off.\n\n"
        "Return ONLY a JSON object with exactly one key:\n"
        '  "narrative": the status text (plain English; \\n between bullets is fine).\n\n'
        "Here is the snapshot (JSON):\n"
        f"{json.dumps(snapshot, indent=2)}\n\n"
        "Write the status now. Output ONLY the JSON object."
    )


def author_narrative(
    snapshot: dict[str, Any],
    *,
    use_llm: bool = True,
    voice_fn: Optional[Callable[..., Optional[dict[str, str]]]] = None,
) -> tuple[str, bool]:
    """Author the narrative prose over the snapshot. Returns (prose, fallback):
    the LLM voice when available, else the deterministic bullet render. `fallback`
    is True when the deterministic path produced the prose. Never raises, never
    returns empty (spec § 3). `voice_fn` defaults to the reused
    missions_narrator.generate_briefing_voice; tests inject a fake."""
    if use_llm:
        fn = voice_fn or generate_briefing_voice
        try:
            voiced = fn(build_narrative_prompt(snapshot), keys=('narrative',))
        except Exception as e:  # noqa: BLE001 — any author error falls back
            log(f'state-log: narrative voice raised: {type(e).__name__}: {e}')
            voiced = None
        if isinstance(voiced, dict):
            prose = (voiced.get('narrative') or '').strip()
            if prose:
                return prose, False
    return render_fallback_narrative(snapshot), True


# ---------------- the write entry point (called by the GC tick) ----------------


def load_inputs(now: Optional[datetime] = None) -> dict[str, Any]:
    """Load every snapshot input from the droplet filesystem, each section
    fail-safe to empty. Read-only on every source. `now` is threaded into the
    waiting-sequences reader so its >15-min stuck-step threshold shares the
    snapshot's clock; it defaults to the current UTC time for ad-hoc callers."""
    now = now or datetime.now(timezone.utc)
    parked_items = load_parked_items()
    return {
        'missions': load_active_missions(),
        'in_flight': load_in_flight(),
        'sequences': load_active_sequences(),
        'parked': len(parked_items),
        'parked_items': parked_items,
        'pending_approvals': load_pending_approvals(),
        'escalations': load_for_larry_escalations(),
        'waiting_sequences': load_waiting_sequences(now),
    }


def write_state_log(
    *,
    now: Optional[datetime] = None,
    use_llm: bool = True,
    write: bool = True,
    pipeline_probe: Optional[Callable[[list[str]], dict[str, str]]] = None,
    voice_fn: Optional[Callable[..., Optional[dict[str, str]]]] = None,
) -> dict[str, Any]:
    """Build the snapshot, author the narrative, and (when `write`) atomically
    write `~/agents/blackboard/system-state-log.json`. Returns the assembled log
    document. Never raises: any failure degrades a section, and the prose always
    has a deterministic fallback. This is the SOLE writer of that file and writes
    NOTHING else (single-committer invariant).

    The GC tick calls this once per cycle, AFTER the Narrator sweep, fail-isolated
    so a State-Log error cannot break the tick."""
    now = now or datetime.now(timezone.utc)
    probe = pipeline_probe or _default_pipeline_probe

    inputs = load_inputs(now)
    snapshot = build_snapshot(
        missions=inputs['missions'],
        in_flight=inputs['in_flight'],
        sequences=inputs['sequences'],
        parked=inputs['parked'],
        now=now,
        pipeline_probe=probe,
        parked_items=inputs.get('parked_items'),
        pending_approvals=inputs.get('pending_approvals'),
        escalations=inputs.get('escalations'),
        waiting_sequences=inputs.get('waiting_sequences'),
    )
    prose, fallback = author_narrative(snapshot, use_llm=use_llm, voice_fn=voice_fn)

    doc = {
        'schema_version': SCHEMA_VERSION,
        'as_of': snapshot['as_of'],
        'narrative_prose': prose,
        'structured_snapshot': snapshot,
        'provenance': {
            'by': NARRATOR_BY,
            'model': NARRATOR_MODEL if (use_llm and not fallback) else 'raw',
            'at': now.astimezone(timezone.utc).isoformat(),
            'fallback': fallback,
        },
    }

    if write:
        path = state_log_path()
        atomic_write_json(path, doc)
        log(f'state-log: wrote {path} '
            f'(missions={len(snapshot["missions_active"])} '
            f'in_flight={snapshot["in_flight_now"]} fallback={fallback})')
    return doc


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Build + write the work-in-flight State Log.')
    parser.add_argument('--no-llm', action='store_true',
                        help='use the deterministic narrative (no claude CLI)')
    parser.add_argument('--dry-run', action='store_true',
                        help='build the log but do not write it to disk')
    args = parser.parse_args(argv)
    doc = write_state_log(use_llm=not args.no_llm, write=not args.dry_run)
    log(f'state-log: done (fallback={doc["provenance"]["fallback"]}, '
        f'written={not args.dry_run})')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'state-log FATAL: {type(exc).__name__}: {exc}')
        sys.exit(1)
