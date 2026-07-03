#!/usr/bin/env python3
"""projects_status_writeback.py — the thin, NON-committer phase status writer
for the projects-v3 pipeline (P3 follow-up, step p3f-status-writeback).

End state: the board reflects reality. When a phase's build launch dispatches,
its lifecycle is stamped ``building`` (and its ``sequence_ref`` pinned to the
build sequence); when that sequence completes (SEQUENCE_COMPLETE), the phase is
stamped ``done`` — automatically, no manual status edit.

Wiring (event-driven, NEVER clock-based):
  * ``build_sequence_advancer`` calls ``stamp_building`` the moment it dispatches
    the first step of a ``launch-<phase_id>`` sequence.
  * ``outbox_notifier`` calls ``stamp_done`` inside the one-time
    SEQUENCE_COMPLETE signal, passing the sequence dict; resolution is the shared
    ``projects_store.resolve_phase_for_sequence`` (``sequence_ref`` then the
    ``authored-by-launch-drain`` audit ids) — so a done-stamp lands even when the
    building-stamp never persisted the ``sequence_ref``.

Single-committer invariant (spec § 4): this writer mutates
``agents/beacon/projects.json`` ON DISK only — atomic (tmp + ``os.replace``)
under an in-process lock — and does NOT git-commit. ``heal_projects_store.py``
remains the SOLE committer; it drains this writer's on-disk delta on its timer
(the same promote / advance / attach non-committer precedent). Decision logic
(what a stamp means, idempotency) lives in the PURE helpers in
``projects_store``; this module only adds the IO.

Idempotent + fail-safe: ``done``→``done`` and ``building``→``building`` (same
ref) are no-ops (no write, so no spurious git delta for the healer); a missing
phase, an unresolved path, or a malformed store is a logged no-op — a status
stamp must NEVER raise into, and wedge, the advancer or the notifier.

stdlib only.
"""
from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so the sibling imports resolve when run by the
# advancer / notifier under systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import projects_store  # noqa: E402 — shared pure schema + stamp helpers
import heal_projects_store as _committer  # noqa: E402 — reuse path resolution

# In-process serialization of the read-modify-write. Cross-process safety rests
# on the atomic ``os.replace`` (whole-file last-writer-wins) + the rarity of the
# events (one launch + one completion per phase), matching the dashboard
# endpoints' ``_PROJECTS_INGEST_LOCK`` posture.
_WRITE_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Best-effort log to the projects-store log (shared with the committer).
    A full / read-only log FS must never crash a status stamp."""
    line = f'[{datetime.now(timezone.utc).isoformat()}] [status-writeback] {msg}'
    print(line, flush=True)
    try:
        path = _committer._log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def _resolve_path() -> Optional[Path]:
    """The projects.json path, resolved the SAME way the committer resolves it
    so writer and committer always agree. Honors ``OURLIBERTY_PROJECTS_JSON``
    (test redirection) first; else the agent-core ``repo_paths`` entry."""
    override = os.environ.get('OURLIBERTY_PROJECTS_JSON')
    if override:
        return Path(override)
    return _committer.projects_path(_committer.load_repo_paths())


def _read(path: Path) -> Optional[dict[str, Any]]:
    """Load projects.json as a registry dict. Missing file → empty registry (so
    a stamp before the healer has ever seeded the file still works). Malformed
    JSON / non-dict → None: the caller no-ops rather than appending onto a
    corrupt file (mirrors heal_projects_store.read_registry)."""
    if not path.exists():
        return projects_store.empty_registry()
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else projects_store.empty_registry()
    except (OSError, json.JSONDecodeError) as e:
        _log(f'projects.json malformed/unreadable ({path}): {e} — no-op')
        return None
    if not isinstance(data, dict):
        _log(f'projects.json shape invalid ({path}) — no-op')
        return None
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Delegates to the shared guarded atomic_io writer. Byte-identical to the
    prior inline writer: pretty JSON (indent=2) + trailing newline."""
    import atomic_io
    atomic_io.atomic_write_json(path, data, trailing_newline=True)


def _apply(
    mutator: Callable[[dict[str, Any]], bool], *, now: Optional[datetime] = None,
) -> bool:
    """Read → mutate → atomic-write, under the lock, fail-safe. ``mutator``
    returns True iff it changed the registry; only then do we write. Returns
    True iff a write happened (so callers can log a real transition). NEVER
    raises — any error is logged and swallowed."""
    try:
        path = _resolve_path()
        if path is None:
            _log('projects.json path unresolved (agent-core not configured) — no-op')
            return False
        with _WRITE_LOCK:
            registry = _read(path)
            if registry is None:
                return False
            if not mutator(registry):
                return False
            _atomic_write(path, registry)
            return True
    except OSError as e:
        # A read-only / full FS is the failure that silently broke p3f live: the
        # advancer's systemd sandbox omitted agent-core from ReadWritePaths, so
        # this write raised EROFS (errno 30) and the phase never flipped to
        # `building`. Log it LOUDLY and distinctly (not a generic "raised") so a
        # recurrence is unmistakable in projects-store.log — but still swallow: a
        # stamp must never wedge the advancer / notifier. EROFS specifically
        # points at the caller unit's ReadWritePaths.
        hint = (" — likely the caller unit's ReadWritePaths is missing the "
                "projects.json dir") if e.errno == errno.EROFS else ''
        _log(f'WRITE FAILED ({type(e).__name__}: {e}) — phase status NOT '
             f'persisted{hint}; swallowing')
        return False
    except Exception as e:  # noqa: BLE001 — a stamp must never wedge its caller
        _log(f'status writeback raised {type(e).__name__}: {e}; swallowing')
        return False


def stamp_building(
    *, seq_id: str, project_id: str, phase_id: str,
    now: Optional[datetime] = None,
) -> bool:
    """Stamp phase ``project_id``/``phase_id`` to ``building`` and pin its
    ``sequence_ref`` to ``seq_id``, on disk (non-committer). Returns True iff a
    write happened. Idempotent (a re-dispatch of the same launch is a no-op) and
    fail-safe (a missing phase is a logged no-op). Event-driven: invoked on the
    launch dispatch event.

    The success log fires ONLY after the write actually persists (it is gated on
    ``_apply``'s return, NOT emitted inside the mutator) — the old code logged
    "stamped building" from inside the mutator, BEFORE the atomic write, so an
    EROFS write failure printed a false success (the 2026-06-19 dogfood trap)."""
    def _mut(registry: dict[str, Any]) -> bool:
        _, phase = projects_store.find_phase(registry, project_id, phase_id)
        if phase is None:
            _log(
                f'building stamp: phase {project_id!r}/{phase_id!r} not found '
                f'(seq={seq_id!r}) — no-op'
            )
            return False
        return projects_store.stamp_phase_building(phase, seq_id, now=now)

    wrote = _apply(_mut, now=now)
    if wrote:
        _log(f'stamped building: {project_id}/{phase_id} sequence_ref={seq_id}')
    return wrote


def attach_closeout(
    *, seq_id: str, closeout_fields: dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """Merge an authored ``closeout`` (the schema dict + ``closeout_provenance``,
    as returned by ``projects_closeout_author.author_phase_closeout``) onto the
    phase whose ``sequence_ref == seq_id``, on disk (NON-committer). Returns True
    iff a write happened. Idempotent (re-attaching the same closeout is a no-op,
    so a duplicate SEQUENCE_COMPLETE leaves no spurious delta) and fail-safe (a
    sequence with no matching phase — e.g. a non-launch build sequence — is a
    logged no-op). Event-driven: invoked on the SEQUENCE_COMPLETE event, right
    after the done-stamp. The decision logic (idempotency, shape) lives in the
    pure ``projects_store.attach_phase_closeout``; this only adds the IO."""
    attached_id: dict[str, Any] = {}

    def _mut(registry: dict[str, Any]) -> bool:
        _, phase = projects_store.find_phase_by_sequence_ref(registry, seq_id)
        if phase is None:
            _log(f'closeout attach: no phase with sequence_ref={seq_id!r} — no-op')
            return False
        attached_id['id'] = phase.get('id')
        return projects_store.attach_phase_closeout(phase, closeout_fields, now=now)

    wrote = _apply(_mut, now=now)
    if wrote:
        _log(f'attached closeout: sequence_ref={seq_id} phase={attached_id.get("id")}')
    return wrote


def stamp_done(*, seq: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Stamp the completing sequence's phase to ``done``, on disk (non-committer).
    Returns True iff a write happened. Takes the whole sequence dict so its phase
    resolution does NOT depend on the building-stamp having persisted
    ``sequence_ref`` (that write can fail — e.g. EROFS — and leave the phase at
    ``spec``/``sequence_ref=None``, the 2026-06-19 dogfood failure).

    Resolution is the SINGLE shared policy ``projects_store.resolve_phase_for_
    sequence`` (``sequence_ref`` first, then the launch-drain audit ids) — the
    SAME policy the P4 closeout uses, so the done-stamp and the closeout always
    pick the same phase. On a hit it also PINS ``sequence_ref`` when the phase is
    missing it (``pin_phase_sequence_ref``), so the closeout's ``sequence_ref``
    lookup and the card's ref field are correct.

    Idempotent (``done``→``done`` with the ref already pinned is a no-op — the
    double-SEQUENCE_COMPLETE guard) and fail-safe (a non-launch sequence with no
    matching phase is a logged no-op). Event-driven: invoked on SEQUENCE_COMPLETE."""
    seq_id = seq.get('seq_id') if isinstance(seq, dict) else None
    done_id: dict[str, Any] = {}

    def _mut(registry: dict[str, Any]) -> bool:
        _, phase = projects_store.resolve_phase_for_sequence(registry, seq)
        if phase is None:
            # Common + expected for ordinary (non-launch) build sequences.
            _log(f'done stamp: no phase for seq={seq_id!r} — no-op')
            return False
        done_id['id'] = phase.get('id')
        # Stamp done, then repair the closeout linkage by pinning the ref when
        # missing (a phase resolved by audit id may lack it). Two independent
        # mutations → OR their changed flags.
        changed = projects_store.stamp_phase_done(phase, now=now)
        if seq_id:
            changed = projects_store.pin_phase_sequence_ref(
                phase, seq_id, now=now) or changed
        return changed

    wrote = _apply(_mut, now=now)
    if wrote:
        _log(f'stamped done: seq={seq_id} phase={done_id.get("id")}')
    return wrote
