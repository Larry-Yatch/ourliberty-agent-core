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
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a normalized single-phase project at Brainstorm — the shape Promote
    lands a funnel item into (spec § 0 "Promote is a move, not a record", § 4
    decision 2). One model for everything: a one-off is a 1-phase project
    (``one_off=True``), state ``active`` so it shows in the "Actively working"
    pipeline immediately; a mis-promote is reversible by archiving it (it never
    duplicates the source — the caller removes the item from its funnel lane).

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
        'lifecycle_state': DEFAULT_LIFECYCLE_STATE,
        'order': 0,
        'spec_ref': None,
        'sequence_ref': None,
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


# --------------------------------------------------------------------------- #
# the "Actively working" derive (the read surface)
# --------------------------------------------------------------------------- #
def _phase_card(phase: dict[str, Any]) -> dict[str, Any]:
    """The lightweight phase card the pipeline UI renders: lifecycle state +
    the plain-language Desired End State + the optional spec/sequence refs, plus
    the authored ``closeout`` once the phase is done (the live surface the UI
    renders — spec § 0.24)."""
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
