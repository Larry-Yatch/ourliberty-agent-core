#!/usr/bin/env python3
"""projects_store.py — the Project + Phase data model for the Projects-tab-v3
pipeline (projects-v3 P3, step p3-project-store).

This is the canonical, single-source schema + normalization + derive helpers
for the "Actively working" pipeline. It is deliberately stdlib-only and pure
(no IO, no network) so BOTH writers and readers share one definition:

  * the SOLE committer — `heal_projects_store.py` — imports `normalize_registry`
    to keep `agents/beacon/projects.json` well-formed and commits its delta to
    main (single-committer invariant; see that healer's docstring).
  * the READ surface — `dashboard_api.py` — imports `build_pipeline` to expose
    the "Actively working" view on `GET /api/missions/derived` (additive: the
    existing missions/orphans/parked/funnel board is untouched).

The model (North Star §4, spec § 0 / § 7 step 1):

  Project = a North Star reference + an ordered list of Phases.
            A one-off is a single-phase project rendered collapsed.
  Phase   = its own plain-language Desired End State ("why this exists",
            distinct from the spec) + a lifecycle state
            (brainstorm → spec → building → done) + an optional spec-doc ref
            + an optional build-sequence ref.

Deliberately SEPARATE from the Supabase Programs `pm-data-model` — projects-v3
keeps the two un-unified per North Star §2. This store is the agent-OS-side,
file-backed, single-committer registry; it is NOT the relational PM backbone.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

# The endpoint/registry contract version. Bumped only on a breaking shape
# change; readers tolerate-and-ignore unknown additive keys.
SCHEMA_VERSION = 1

# The phase lifecycle, in canonical forward order (North Star §4.4, spec § 0):
# Brainstorm → Spec → Building → Done. Modeled explicitly so the model — not a
# scatter of string literals across callers — owns the legal states and the
# legal forward transitions.
LIFECYCLE_STATES: tuple[str, ...] = ('brainstorm', 'spec', 'building', 'done')
DEFAULT_LIFECYCLE_STATE = 'brainstorm'

# A project is either actively-worked or archived. Promote (P3 step 2) lands a
# new project at `active`; a mis-promote is reversible by archiving it (spec § 5
# "Promote irreversibility" guardrail) — it is never a dead end.
PROJECT_STATES: tuple[str, ...] = ('active', 'archived')
DEFAULT_PROJECT_STATE = 'active'


# --------------------------------------------------------------------------- #
# lifecycle model
# --------------------------------------------------------------------------- #
def is_valid_lifecycle_state(state: Any) -> bool:
    """True iff `state` is one of the four canonical phase lifecycle states."""
    return state in LIFECYCLE_STATES


def is_valid_project_state(state: Any) -> bool:
    """True iff `state` is one of the canonical project states."""
    return state in PROJECT_STATES


def next_lifecycle_state(state: str) -> Optional[str]:
    """The state immediately after `state` in the forward lifecycle, or None
    if `state` is terminal (`done`) or not a known state. Pure lookup — does
    NOT mutate anything; mutators (P3 step 2+) call this to advance a phase."""
    if state not in LIFECYCLE_STATES:
        return None
    idx = LIFECYCLE_STATES.index(state)
    if idx + 1 >= len(LIFECYCLE_STATES):
        return None
    return LIFECYCLE_STATES[idx + 1]


def can_transition(from_state: str, to_state: str) -> bool:
    """Whether a phase may move from `from_state` to `to_state`.

    Legal moves: one step FORWARD (brainstorm→spec→building→done), or any move
    BACKWARD (a checkpoint "refine" can send a phase back a stage; spec § 5
    keeps promotion reversible). A no-op (from == to) is allowed (idempotent).
    Skipping forward stages (e.g. brainstorm→building) is NOT allowed — the
    gates between stages are the point of the lifecycle.
    """
    if not (is_valid_lifecycle_state(from_state)
            and is_valid_lifecycle_state(to_state)):
        return False
    fi = LIFECYCLE_STATES.index(from_state)
    ti = LIFECYCLE_STATES.index(to_state)
    # forward by exactly one, any backward, or no-op
    return ti <= fi or ti == fi + 1


# --------------------------------------------------------------------------- #
# registry shape + normalization (the committer's contract)
# --------------------------------------------------------------------------- #
def empty_registry() -> dict[str, Any]:
    """A fresh, valid, empty projects registry."""
    return {'schema_version': SCHEMA_VERSION, 'projects': []}


def _iso_now(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _coerce_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _normalize_phase(raw: Any, *, default_order: int,
                     now_iso: str) -> Optional[dict[str, Any]]:
    """Normalize one phase dict, backfilling defaults. Returns the normalized
    phase, or None if it is too malformed to keep (not a dict, or no id). A
    dropped phase is logged by the caller, never silently corrupts the file."""
    if not isinstance(raw, dict):
        return None
    pid = _coerce_str(raw.get('id'))
    if pid is None:
        return None
    phase = dict(raw)
    phase['id'] = pid
    phase['title'] = _coerce_str(raw.get('title')) or pid
    phase['desired_end_state'] = (
        raw.get('desired_end_state')
        if isinstance(raw.get('desired_end_state'), str) else ''
    )
    state = raw.get('lifecycle_state')
    phase['lifecycle_state'] = (
        state if is_valid_lifecycle_state(state) else DEFAULT_LIFECYCLE_STATE
    )
    order = raw.get('order')
    phase['order'] = order if isinstance(order, int) else default_order
    # Optional refs — null when absent, never a bare missing key, so readers
    # can treat the field as always-present.
    phase['spec_ref'] = _coerce_str(raw.get('spec_ref'))
    phase['sequence_ref'] = _coerce_str(raw.get('sequence_ref'))
    phase.setdefault('created_at', now_iso)
    phase.setdefault('updated_at', phase['created_at'])
    return phase


def _normalize_project(raw: Any, *, now_iso: str) -> Optional[dict[str, Any]]:
    """Normalize one project dict, backfilling defaults and normalizing its
    ordered phases. Returns None if too malformed to keep (not a dict, or no
    id)."""
    if not isinstance(raw, dict):
        return None
    proj_id = _coerce_str(raw.get('id'))
    if proj_id is None:
        return None
    project = dict(raw)
    project['id'] = proj_id
    project['title'] = _coerce_str(raw.get('title')) or proj_id
    project['north_star_ref'] = _coerce_str(raw.get('north_star_ref'))
    project['repo'] = _coerce_str(raw.get('repo'))
    state = raw.get('state')
    project['state'] = state if is_valid_project_state(state) else DEFAULT_PROJECT_STATE

    raw_phases = raw.get('phases')
    raw_phases = raw_phases if isinstance(raw_phases, list) else []
    phases: list[dict[str, Any]] = []
    for i, rp in enumerate(raw_phases):
        np = _normalize_phase(rp, default_order=i, now_iso=now_iso)
        if np is not None:
            phases.append(np)
    # Stable sort by `order` so the phase cards always render in lifecycle
    # sequence regardless of insertion order in the file.
    phases.sort(key=lambda p: p['order'])
    project['phases'] = phases
    # A one-off is a single-phase project; expose the flag so the UI can
    # collapse it with no ceremony (North Star §4.7). Honor an explicit flag if
    # present, else derive it from the phase count.
    explicit = raw.get('one_off')
    project['one_off'] = explicit if isinstance(explicit, bool) else (len(phases) == 1)
    project.setdefault('created_at', now_iso)
    project.setdefault('updated_at', project['created_at'])
    return project


def normalize_registry(
    data: Any, *, now: Optional[datetime] = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(registry, dropped_ids)`` — a well-formed registry plus the ids
    (or ``'<unidentifiable>'``) of any project/phase that was too malformed to
    keep.

    Pure and idempotent: re-normalizing an already-normalized registry returns
    an equal registry and an empty ``dropped_ids`` list. The SOLE committer
    calls this every tick; a clean tick produces no delta (so no commit), which
    is how the single-committer healer stays quiet when there is nothing to do.

    Never raises on bad input — a non-dict top level, a missing ``projects``
    key, or junk entries all degrade to the empty/partial registry rather than
    blowing up the committer or the reader.
    """
    now_iso = _iso_now(now)
    if not isinstance(data, dict):
        return empty_registry(), ['<non-dict-registry>']

    raw_projects = data.get('projects')
    raw_projects = raw_projects if isinstance(raw_projects, list) else []
    projects: list[dict[str, Any]] = []
    dropped: list[str] = []
    for rp in raw_projects:
        np = _normalize_project(rp, now_iso=now_iso)
        if np is None:
            ident = rp.get('id') if isinstance(rp, dict) else None
            dropped.append(ident if isinstance(ident, str) and ident
                           else '<unidentifiable>')
            continue
        projects.append(np)

    registry = {'schema_version': SCHEMA_VERSION, 'projects': projects}
    return registry, dropped


# --------------------------------------------------------------------------- #
# id helpers (used by the promote endpoint in P3 step 2; defined here so the
# id grammar lives with the schema)
# --------------------------------------------------------------------------- #
_SLUG_RE = re.compile(r'[^a-z0-9]+')


def slugify(text: str, *, max_len: int = 48) -> str:
    """Lowercase kebab slug for ids — stdlib-only, deterministic."""
    s = _SLUG_RE.sub('-', (text or '').lower()).strip('-')
    return s[:max_len].strip('-') or 'untitled'


# --------------------------------------------------------------------------- #
# project builder — Promote = relocate a funnel item into a new project
# (P3 step 2, p3-promote-endpoint). Pure: builds the dict; the on-disk write +
# the single-committer commit happen in dashboard_api / heal_projects_store.
# --------------------------------------------------------------------------- #
def new_single_phase_project(
    *,
    title: str,
    desired_end_state: str = '',
    north_star_ref: Optional[str] = None,
    repo: Optional[str] = None,
    promoted_from: Optional[dict[str, Any]] = None,
    project_id: Optional[str] = None,
    phase_id: Optional[str] = None,
    lifecycle_state: Optional[str] = None,
    spec_ref: Optional[str] = None,
    sequence_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a normalized single-phase project — the shape Promote lands a funnel
    item into (spec § 0 "Promote is a move, not a record", § 4 decision 2). One
    model for everything: a one-off is a 1-phase project (``one_off=True``), state
    ``active`` so it shows in the "Actively working" pipeline immediately; a
    mis-promote is reversible by archiving it (it never duplicates the source —
    the caller removes the item from its funnel lane).

    Defaults to **Brainstorm** with no refs (the Promote path). The kanban→pipeline
    migration (`mirror_missions_as_projects`) passes a mapped ``lifecycle_state``
    (an in-flight mission lands at ``building``) plus the mission's ``spec_ref`` /
    ``sequence_ref`` so the coarse status reflects reality on day one. An invalid
    ``lifecycle_state`` degrades to the default (never a corrupt phase).

    ``promoted_from`` is the provenance back-reference (e.g.
    ``{'kind': 'capture', 'capture_id': ...}`` or
    ``{'kind': 'mission', 'mission_id': ...}``) the funnel derive reads to
    suppress the now-promoted item from its lane and to make the move reversible
    without data loss.

    Returns an already-normalized dict: feeding it back through
    ``normalize_registry`` is a no-op (the single-committer healer therefore
    sees no spurious delta)."""
    now_iso = _iso_now(now)
    pid = _coerce_str(project_id) or slugify(title)
    phid = _coerce_str(phase_id) or pid
    title = _coerce_str(title) or pid
    phase = {
        'id': phid,
        'title': title,
        'desired_end_state': desired_end_state if isinstance(desired_end_state, str) else '',
        'lifecycle_state': (
            lifecycle_state if is_valid_lifecycle_state(lifecycle_state)
            else DEFAULT_LIFECYCLE_STATE
        ),
        'order': 0,
        'spec_ref': _coerce_str(spec_ref),
        'sequence_ref': _coerce_str(sequence_ref),
        'created_at': now_iso,
        'updated_at': now_iso,
    }
    project: dict[str, Any] = {
        'id': pid,
        'title': title,
        'north_star_ref': _coerce_str(north_star_ref),
        'repo': _coerce_str(repo),
        'state': DEFAULT_PROJECT_STATE,
        'phases': [phase],
        'one_off': True,
        'created_at': now_iso,
        'updated_at': now_iso,
    }
    if isinstance(promoted_from, dict):
        project['promoted_from'] = dict(promoted_from)
    return project


# --------------------------------------------------------------------------- #
# kanban → pipeline migration (projects-v3 retire-missions-kanban). Mirror each
# ACTIVE (non-shipped) mission as a single-phase project so the legacy Missions
# kanban can be retired and the work lives in "Actively working". Idempotent +
# reversible: a mission already represented (by project id OR a `mission`
# promoted_from back-ref, active OR archived) is skipped — so a re-run is a no-op
# and a dropped project never resurrects. missions.json is NEVER written here;
# the migration only mints projects, exactly like Promote.
# --------------------------------------------------------------------------- #

# Mission phases that belong on the active board → their pipeline lifecycle.
# `shipped`/`deferred`/`proposed` are intentionally absent: shipped is done work
# (retired, not mirrored — spec § 4.2); deferred/proposed are not active.
_MISSION_PHASE_TO_LIFECYCLE: dict[str, str] = {
    'drafting': 'brainstorm',
    'ready': 'spec',
    'in_flight': 'building',
}

_SEQ_STEP_RE = re.compile(r'^seq-(?P<seq>.+?)-step-')  # non-greedy: split at the FIRST -step-


def _sequence_ref_from_task_ids(task_ids: Any) -> Optional[str]:
    """The build-sequence id a mission's `seq-<id>-step-*` task_ids belong to, or
    None. Pins the migrated phase's `sequence_ref` so the coarse Building/Done
    status reflects the real sequence. Best-effort: first match wins; a mission
    with no sequence-step task gets a null ref (coarse `building`, no sequence —
    acceptable per spec § 5)."""
    if not isinstance(task_ids, list):
        return None
    for tid in task_ids:
        if isinstance(tid, str):
            m = _SEQ_STEP_RE.match(tid)
            if m:
                return m.group('seq')
    return None


def _mission_already_mirrored(projects: list[dict[str, Any]], mission_id: str) -> bool:
    """True iff some project (ACTIVE or ARCHIVED) already represents this mission —
    by sharing its id (the migration uses ``project_id = mission_id``) OR carrying
    a ``{'kind': 'mission', 'mission_id': <id>}`` promoted_from. Checking archived
    too is what makes a dropped project permanent (a re-run never resurrects it)
    and skips a mission already promoted into the pipeline by hand (e.g. a
    capture-promoted project that shares the slug)."""
    for p in projects:
        if not isinstance(p, dict):
            continue
        if p.get('id') == mission_id:
            return True
        pf = p.get('promoted_from')
        if (isinstance(pf, dict) and pf.get('kind') == 'mission'
                and pf.get('mission_id') == mission_id):
            return True
    return False


def mirror_missions_as_projects(
    registry: dict[str, Any],
    missions: Any,
    now: Optional[datetime] = None,
) -> list[str]:
    """Mint a single-phase project for each ACTIVE (non-shipped) mission not yet
    represented in the pipeline. Mutates ``registry['projects']`` in place; returns
    the ids minted (empty on a no-op tick). Pure over its inputs (no IO): the
    caller supplies the parsed missions list and commits the delta.

    Maps the mission phase to the pipeline lifecycle (drafting→brainstorm,
    ready→spec, in_flight→building), takes the mission's first spec doc as
    ``spec_ref``, and derives ``sequence_ref`` from its sequence-step task_ids so
    the coarse status reflects reality. Idempotent + reversible via
    ``_mission_already_mirrored``; ``missions.json`` is never written."""
    if not isinstance(registry, dict) or not isinstance(missions, list):
        return []
    projects = registry.setdefault('projects', [])
    if not isinstance(projects, list):
        return []
    minted: list[str] = []
    for m in missions:
        if not isinstance(m, dict):
            continue
        lifecycle = _MISSION_PHASE_TO_LIFECYCLE.get(m.get('phase'))
        if lifecycle is None:
            continue  # shipped/deferred/proposed/unknown — not an active board card
        if m.get('archived') is True or m.get('acknowledged') is True:
            continue
        mid = _coerce_str(m.get('id'))
        if mid is None or _mission_already_mirrored(projects, mid):
            continue
        spec_docs = m.get('spec_docs')
        spec_ref = (spec_docs[0] if isinstance(spec_docs, list) and spec_docs
                    and isinstance(spec_docs[0], str) else None)
        projects.append(new_single_phase_project(
            title=m.get('name') or mid,
            desired_end_state=m.get('brief') or '',
            repo=m.get('repo'),
            promoted_from={'kind': 'mission', 'mission_id': mid},
            project_id=mid,
            lifecycle_state=lifecycle,
            spec_ref=spec_ref,
            sequence_ref=_sequence_ref_from_task_ids(m.get('task_ids')),
            now=now,
        ))
        minted.append(mid)
    return minted


# --------------------------------------------------------------------------- #
# phase status writeback — pure lookups + idempotent stamps
# (projects-v3 P3 step p3f-status-writeback). These are PURE: they locate /
# mutate a phase dict in place and return whether anything changed. The on-disk
# read-modify-atomic-write (the NON-committer) lives in
# `projects_status_writeback.py`; the git commit stays with the SOLE committer
# `heal_projects_store.py`. Keeping the decision logic here (no IO) is what lets
# both writers and the committer share one definition of "what a stamp means".
# --------------------------------------------------------------------------- #
def find_phase(
    registry: dict[str, Any], project_id: str, phase_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return ``(project, phase)`` for the phase ``phase_id`` inside project
    ``project_id``, or ``(None, None)``. Pure lookup over a registry dict; safe
    on junk (non-dict entries are skipped)."""
    if not isinstance(registry, dict) or not project_id or not phase_id:
        return None, None
    for proj in registry.get('projects', []) or []:
        if not isinstance(proj, dict) or proj.get('id') != project_id:
            continue
        for phase in proj.get('phases', []) or []:
            if isinstance(phase, dict) and phase.get('id') == phase_id:
                return proj, phase
    return None, None


def launch_ids_from_sequence(
    seq: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(project_id, phase_id)`` from a launch sequence's
    ``authored-by-launch-drain`` audit entry — the robust, ``sequence_ref``-
    INDEPENDENT linkage the done-stamp + closeout use to find the phase a
    ``launch-<phase_id>`` sequence belongs to. The launch drain writes this entry
    with both ids when it authors the sequence (launch_queue_drain.py); the
    building-stamp reads the same source. ``(None, None)`` for an ordinary
    (non-launch) sequence, a missing entry, or junk — callers then fall back to a
    ``sequence_ref`` lookup or no-op. Pure; safe on any input."""
    if not isinstance(seq, dict):
        return None, None
    for entry in seq.get('audit_log') or []:
        if isinstance(entry, dict) and entry.get('event') == 'authored-by-launch-drain':
            pid = _coerce_str(entry.get('project_id'))
            phid = _coerce_str(entry.get('phase_id'))
            return pid, phid
    return None, None


def find_phase_by_sequence_ref(
    registry: dict[str, Any], sequence_ref: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return ``(project, phase)`` for the phase whose ``sequence_ref`` equals
    ``sequence_ref``, or ``(None, None)``. This is the linkage the done-stamp
    uses: SEQUENCE_COMPLETE carries a ``seq_id``; the building-stamp pinned that
    same ``seq_id`` onto the phase, so a completing sequence finds its phase by
    this back-reference. Pure; safe on junk."""
    if not isinstance(registry, dict) or not sequence_ref:
        return None, None
    for proj in registry.get('projects', []) or []:
        if not isinstance(proj, dict):
            continue
        for phase in proj.get('phases', []) or []:
            if isinstance(phase, dict) and phase.get('sequence_ref') == sequence_ref:
                return proj, phase
    return None, None


def resolve_phase_for_sequence(
    registry: dict[str, Any], seq: Any,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return ``(project, phase)`` for the phase a completed/launching sequence
    belongs to — THE single resolution policy shared by the done-stamp
    (``projects_status_writeback.stamp_done``) and the P4 closeout
    (``projects_closeout_author.narrator_find_phase``) so both always pick the
    SAME phase for a given sequence.

    Order: ``sequence_ref == seq['seq_id']`` first (the building-stamp's pin, also
    re-pinned by the done-stamp — reliable once either has run), then the
    ``(project_id, phase_id)`` in the sequence's ``authored-by-launch-drain``
    audit entry (the robust fallback when the building-stamp never persisted the
    ref — e.g. an EROFS write failure). ``(None, None)`` for an ordinary
    non-launch sequence. Pure; safe on junk."""
    if not isinstance(seq, dict):
        return None, None
    seq_id = _coerce_str(seq.get('seq_id'))
    if seq_id:
        proj, phase = find_phase_by_sequence_ref(registry, seq_id)
        if phase is not None:
            return proj, phase
    project_id, phase_id = launch_ids_from_sequence(seq)
    if project_id and phase_id:
        return find_phase(registry, project_id, phase_id)
    return None, None


def stamp_phase_building(
    phase: dict[str, Any], sequence_ref: str, *, now: Optional[datetime] = None,
) -> bool:
    """Idempotently move ``phase`` to ``building`` and pin its ``sequence_ref``.
    Returns True iff the phase dict was mutated.

    Idempotent / forward-only: a no-op (returns False, NO mutation) when the
    phase is already ``done`` (never regress a completed phase back to building —
    a late/duplicate launch dispatch must not undo completion) or already
    ``building`` with this same ``sequence_ref``. Otherwise stamps building +
    the ref + ``updated_at``. Event-driven: the caller invokes this on the launch
    dispatch event, never on a clock."""
    if not isinstance(phase, dict):
        return False
    state = phase.get('lifecycle_state')
    if state == 'done':
        return False
    if state == 'building' and phase.get('sequence_ref') == sequence_ref:
        return False
    phase['lifecycle_state'] = 'building'
    phase['sequence_ref'] = sequence_ref
    phase['updated_at'] = _iso_now(now)
    return True


def pin_phase_sequence_ref(
    phase: dict[str, Any], sequence_ref: str, *, now: Optional[datetime] = None,
) -> bool:
    """Pin ``phase['sequence_ref'] = sequence_ref`` when the phase is MISSING a
    ref, bumping ``updated_at``. Returns True iff the phase dict was mutated.

    The repair the done-stamp applies when it had to resolve the phase by its
    launch-drain audit ids because the building-stamp never persisted the ref —
    so the downstream closeout's ``sequence_ref`` lookup (and the card's ref
    field) become correct. Never OVERWRITES an existing (truthy) ref — a phase
    already carrying a different ref is left untouched (no silent relink). Pure;
    keeps all phase mutation + its updated_at bump in this layer (alongside
    ``stamp_phase_building`` / ``stamp_phase_done``)."""
    if not isinstance(phase, dict) or not sequence_ref:
        return False
    if phase.get('sequence_ref'):
        return False
    phase['sequence_ref'] = sequence_ref
    phase['updated_at'] = _iso_now(now)
    return True


def stamp_phase_done(
    phase: dict[str, Any], *, now: Optional[datetime] = None,
) -> bool:
    """Idempotently move ``phase`` to ``done``. Returns True iff mutated;
    ``done``→``done`` is a no-op (returns False) — the SEQUENCE_COMPLETE
    idempotency guard so a double completion signal never re-writes the store.
    Event-driven: the caller invokes this on the SEQUENCE_COMPLETE event."""
    if not isinstance(phase, dict):
        return False
    if phase.get('lifecycle_state') == 'done':
        return False
    phase['lifecycle_state'] = 'done'
    phase['updated_at'] = _iso_now(now)
    return True


def attach_phase_closeout(
    phase: dict[str, Any], closeout_fields: dict[str, Any],
    *, now: Optional[datetime] = None,
) -> bool:
    """Merge an authored closeout (the ``closeout`` schema dict + its
    ``closeout_provenance``) onto ``phase``. Returns True iff the phase dict was
    mutated. Pure — the decision (what an attach means, idempotency) lives here;
    the IO/commit is the writer's/healer's job (single-committer invariant).

    Idempotent: re-attaching an identical closeout is a no-op (returns False, NO
    mutation) so a duplicate SEQUENCE_COMPLETE never produces a spurious store
    delta for the healer to commit. Fail-safe on junk: a non-dict phase or a
    closeout payload without a ``closeout`` body is a no-op."""
    if not isinstance(phase, dict) or not isinstance(closeout_fields, dict):
        return False
    closeout = closeout_fields.get('closeout')
    if not isinstance(closeout, dict):
        return False
    provenance = closeout_fields.get('closeout_provenance')
    if phase.get('closeout') == closeout and (
        provenance is None or phase.get('closeout_provenance') == provenance
    ):
        return False
    phase['closeout'] = closeout
    if provenance is not None:
        phase['closeout_provenance'] = provenance
    phase['updated_at'] = _iso_now(now)
    return True


def attach_phase_brainstorm(
    phase: dict[str, Any], brainstorm_fields: dict[str, Any],
    *, now: Optional[datetime] = None,
) -> bool:
    """Merge an authored brainstorm (the ``brainstorm`` schema dict + its
    ``brainstorm_provenance``) onto ``phase``. Returns True iff the phase dict was
    mutated. Pure — the decision (what an attach means, idempotency) lives here;
    the IO/commit is the healer's job (single-committer invariant), exactly as
    ``attach_phase_closeout`` for the P4 closeout.

    Idempotent: re-attaching an identical brainstorm is a no-op (returns False, NO
    mutation) so the periodic author sweep never produces a spurious store delta
    for the healer to commit. Fail-safe on junk: a non-dict phase or a payload
    without a ``brainstorm`` body is a no-op."""
    if not isinstance(phase, dict) or not isinstance(brainstorm_fields, dict):
        return False
    brainstorm = brainstorm_fields.get('brainstorm')
    if not isinstance(brainstorm, dict):
        return False
    provenance = brainstorm_fields.get('brainstorm_provenance')
    if phase.get('brainstorm') == brainstorm and (
        provenance is None or phase.get('brainstorm_provenance') == provenance
    ):
        return False
    phase['brainstorm'] = brainstorm
    if provenance is not None:
        phase['brainstorm_provenance'] = provenance
    phase['updated_at'] = _iso_now(now)
    return True


# --------------------------------------------------------------------------- #
# the "Actively working" derive (the read surface)
# --------------------------------------------------------------------------- #
# The 8 brainstorm-draft sections (projects_brainstorm_author.BRAINSTORM_SECTION_
# KEYS) paired with the plain-English headers the card renders them under. The
# author STORES the draft as a section→prose dict; the Brainstorm card (dashboard
# p6-brainstorm-card-ui) renders a single pre-wrapped STRING, so the card view
# flattens the dict into one headed string here at the read boundary. Kept in
# this module (not imported from the author) so the read surface never depends on
# the author's optional `claude`/narrator import chain.
_BRAINSTORM_DRAFT_SECTIONS: tuple[tuple[str, str], ...] = (
    ('end_state', 'Desired end state'),
    ('why_now', 'Why now'),
    ('scope', 'Scope & non-goals'),
    ('constraints_reuse', 'Constraints & reuse'),
    ('options_decision', 'Options & the decision'),
    ('risks_guardrails', 'Risks & guardrails'),
    ('done_gate', 'Done-gate'),
    ('breakdown', 'Breakdown'),
)


def _flatten_brainstorm_draft(draft: Any) -> Optional[str]:
    """Flatten the stored 8-section draft dict into the single headed string the
    Brainstorm card renders (whitespace-pre-wrap). A draft that is already a
    plain string is returned trimmed; a dict is rendered section-by-section in
    canonical order, skipping empty sections; anything else (or an all-empty
    draft) yields None so the card omits the draft block (graceful no-prefill)."""
    if isinstance(draft, str):
        return draft.strip() or None
    if not isinstance(draft, dict):
        return None
    blocks: list[str] = []
    for key, header in _BRAINSTORM_DRAFT_SECTIONS:
        value = draft.get(key)
        if isinstance(value, str) and value.strip():
            blocks.append(f'{header}\n{value.strip()}')
    return '\n\n'.join(blocks) or None


def _brainstorm_decision_cards(brainstorm: dict[str, Any]) -> list[dict[str, Any]]:
    """Map the stored "Your decisions" forks (a capped list of plain-English
    strings — the genuine choices that are Larry's to call) into the decision
    objects the card renders (``{id, title, decision}``: the card shows the fork
    as ``title`` and an em-dash for the as-yet-unmade ``decision``). When the
    author flagged ``decisions_overflow`` (more genuine forks existed than the
    ≤5 cap — spec § 4 guardrail), a trailing note item says so, so the card and
    the handoff make clear more will surface in the Claude session."""
    raw = brainstorm.get('decisions')
    cards: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for i, fork in enumerate(raw):
            if isinstance(fork, str) and fork.strip():
                cards.append({'id': f'dec-{i}', 'title': fork.strip(), 'decision': ''})
    if cards and brainstorm.get('decisions_overflow'):
        cards.append({
            'id': 'dec-more',
            'title': 'More decisions will surface as you work this through with Claude.',
            'decision': '',
        })
    return cards


def _phase_card(phase: dict[str, Any]) -> dict[str, Any]:
    """The lightweight phase card the pipeline UI renders: lifecycle state +
    the plain-language Desired End State + the optional spec/sequence refs, plus
    the authored ``closeout`` once the phase is done (the live surface the UI
    renders — spec § 0.24).

    Brainstorm pre-fill (projects-v3 P6): the author STORES a nested
    ``phase['brainstorm']`` ({is_ai_draft, draft (8-section dict), decisions
    (forks), decisions_overflow, handoff}); the Brainstorm card consumes a FLAT
    shape (``draft`` string, ``decisions`` objects, ``spec_target_path``). This
    read boundary projects the stored fields into that flat card shape so the
    two steps (author #611 / card #72) meet — additive + graceful: a phase with
    no brainstorm carries none of these keys and the card no-prefills."""
    card = {
        'id': phase.get('id'),
        'title': phase.get('title'),
        'desired_end_state': phase.get('desired_end_state', ''),
        'lifecycle_state': phase.get('lifecycle_state', DEFAULT_LIFECYCLE_STATE),
        'order': phase.get('order', 0),
        'spec_ref': phase.get('spec_ref'),
        'sequence_ref': phase.get('sequence_ref'),
    }
    closeout = phase.get('closeout')
    if isinstance(closeout, dict):
        card['closeout'] = closeout
    brainstorm = phase.get('brainstorm')
    if isinstance(brainstorm, dict):
        draft = _flatten_brainstorm_draft(brainstorm.get('draft'))
        if draft:
            card['draft'] = draft
        decisions = _brainstorm_decision_cards(brainstorm)
        if decisions:
            card['decisions'] = decisions
        handoff = brainstorm.get('handoff')
        if isinstance(handoff, dict):
            spec_target = handoff.get('spec_target_path')
            if isinstance(spec_target, str) and spec_target.strip():
                card['spec_target_path'] = spec_target.strip()
    return card


def _project_status(phases: list[dict[str, Any]]) -> str:
    """A COARSE rollup of a project's phase states (P3 shows coarse status only;
    full DAG N-of-M detail is P5). 'done' iff every phase is done; 'building' if
    any phase is building/done but not all done; else 'brainstorm'/'spec' from
    the least-advanced active phase — kept intentionally cheap."""
    if not phases:
        return DEFAULT_LIFECYCLE_STATE
    states = [p.get('lifecycle_state', DEFAULT_LIFECYCLE_STATE) for p in phases]
    if all(s == 'done' for s in states):
        return 'done'
    if any(s == 'building' for s in states):
        return 'building'
    # no building, not all done → a brainstorm dominates the spec/done remainder
    if any(s == 'brainstorm' for s in states):
        return 'brainstorm'
    return 'spec'


def build_pipeline(
    projects: list[dict[str, Any]], now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """The "Actively working" pipeline view (spec § 0, § 7 step 1): the list of
    ACTIVE projects, each with its ordered phase cards, coarse rollup status,
    and the one-off collapse flag. Archived projects are excluded (a dropped /
    archived project leaves the pipeline). Pure over its input; safe on junk
    (a non-dict project is skipped). Additive — this is exposed under a NEW
    `pipeline` key; it never touches the existing board sections.
    """
    out: list[dict[str, Any]] = []
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        if proj.get('state', DEFAULT_PROJECT_STATE) != 'active':
            continue
        phases = proj.get('phases')
        phases = phases if isinstance(phases, list) else []
        cards = [_phase_card(p) for p in phases if isinstance(p, dict)]
        out.append({
            'id': proj.get('id'),
            'title': proj.get('title'),
            'north_star_ref': proj.get('north_star_ref'),
            'repo': proj.get('repo'),
            'one_off': bool(proj.get('one_off', len(cards) == 1)),
            'status': _project_status([p for p in phases if isinstance(p, dict)]),
            'phases': cards,
        })
    return out
