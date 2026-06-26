#!/usr/bin/env python3
"""heal_completed_sequence_mission_reconcile.py — reconcile a mission's board
phase to `shipped` when its build SEQUENCE reaches `status: complete`, and
SURFACE umbrella missions to needs-you instead of guessing.

WHY
---
When Beacon launches a mission as a build SEQUENCE, the mission's ``task_ids`` are
synthetic ``seq-<seq_id>-step-<name>`` ids (e.g.
``seq-operator-needs-you-feed-step-escalation-feed``) — NOT real Forge build
task_ids. That makes a sequence-built mission invisible to BOTH existing
reconcilers:

  * ``heal_missions_card_gc``'s terminal-state reconcile treats each task_id as
    PR-backed and probes it (``probeable_task_ids`` does NOT exclude ``seq-*``);
    the synthetic ids never resolve to a merged PR, so the all-terminal ship gate
    is never satisfied and the mission sits at ``in_flight`` forever.
  * ``heal_merged_pr_board_reconcile`` only considers OFF-BOARD missions (NO
    probeable task_id) — these missions carry probeable-LOOKING task_ids, so it
    skips them — and it deliberately never mutates the board anyway.

Net: the mission's rollup reads ``0 building / 0 in-review / 0 merged`` even when
the sequence is ``complete``, and the /where-we-are narrator mislabels finished
work as "still building / jammed". The gap bit twice and was each time hand-fixed
by a one-off ``missions.json`` PR (``operator-ux-catch-me-up-shortcut`` earlier;
``operator-needs-you-feed`` via PR #722 on 2026-06-26).

The build-sequence file IS the authoritative shipped signal: the advancer sets
``status: complete`` ONLY once every step is ``merged`` (i.e. every step PR
landed — see ``build_sequence_advancer`` § 3). So this healer can close the loop
purely LOCALLY — no ``gh``, no network — by reading the completed sequence files
under ``build_sequences_dir()`` and the board, and reconciling the OWNING
mission's phase.

WHAT
----
Each tick:
  1. Load every sequence file whose ``status == 'complete'`` (seq_id → sequence).
  2. For each reconcilable-phase, non-archived mission, derive which sequences it
     owns from the ``seq-<seq_id>-step-*`` task_id convention and decide:
       * SHIP — the completed sequence is unambiguously the mission's WHOLE scope
         (its probeable task_ids are ALL steps of exactly ONE sequence, and that
         sequence is complete) ⇒ flip to ``shipped``, mirroring the manual
         reconcile shape (``prior_phase`` + ``shipped_at`` / ``shipped_by`` /
         ``shipped_note``).
       * SURFACE — the mission spans MORE than that one sequence (≥2 sequences, or
         a non-sequence PR-backed task_id alongside) but at least one of its
         sequences is complete ⇒ an UMBRELLA: never auto-retire, only surface it
         to the for-Larry needs-you lane via ``for_larry_signal.sync_prefix``
         (self-clearing) so a human confirms the rest.
       * KEEP — otherwise (no sequence task_ids, or no referenced sequence is
         complete yet).
  3. Write the ``missions.json`` ship delta (lost-update-guarded re-read + merge),
     leaving the COMMIT to ``heal_missions_card_gc`` (single-committer invariant —
     this healer, like ``heal_missions_board_drain``, never commits). Surface
     records are synced under our own signal prefix.

CONSERVATIVE — UMBRELLA-SAFE (mirrors heal_merged_pr_board_reconcile)
--------------------------------------------------------------------
A single completed sub-sequence cannot prove a multi-part mission is fully done.
So we AUTO-FLIP only on the unambiguous single-sequence whole-scope case; every
multi-sequence / mixed-scope mission is SURFACED, never retired. ``proposed`` /
``deferred`` phases (deliberate holds, owned elsewhere) are never touched. A
missing/unreadable sequence file or a missing dir degrades to "do nothing this
tick" — never a false ship.

Dry-run by default; pass ``--apply`` to write the board delta + the signal.
stdlib + sibling modules only; no git, no gh, no network.

    cd ~/agent-core && python3 scripts/heal_completed_sequence_mission_reconcile.py          # dry-run
    cd ~/agent-core && python3 scripts/heal_completed_sequence_mission_reconcile.py --apply  # write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Repo scripts dir on sys.path so sibling imports resolve under systemd / discover.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Reuse the GC healer's path resolvers, fail-safe registry reader, the
# reconcilable-phase set + shipped constant, the PR-backed `probeable_task_ids`
# filter, the atomic JSON write, the lost-update field-merge, the PR-number
# parser, the shared kill switch, and its log() — ONE definition of "what the
# board considers active / shippable", and (critically) the GC healer remains the
# SOLE missions.json committer (we only WRITE the delta on disk).
import heal_missions_card_gc as gc  # noqa: E402
# The sanctioned needs-you producer. sync_prefix upserts the current snapshot and
# self-resolves any of OUR keys whose trigger left the snapshot.
import for_larry_signal  # noqa: E402
# The canonical build-sequences dir resolver (honors OURLIBERTY_BUILD_SEQUENCES_DIR).
from system_state_log import build_sequences_dir  # noqa: E402

log = gc.log

SHIPPED_BY = 'heal_completed_sequence_mission_reconcile'
# Namespace for every needs-you record this healer owns. sync_prefix only ever
# touches keys under this prefix, so other producers' rows are never disturbed.
SIGNAL_PREFIX = 'sequence-complete-umbrella:'

# A mission's `seq-<seq_id>-step-<step>` task_id → its owning sequence id. Mirrors
# projects_store._SEQ_STEP_RE (non-greedy: split at the FIRST `-step-`, since a
# step_id may itself contain `-step-`). Kept local to stay off projects_store's
# heavy import surface.
_SEQ_STEP_RE = re.compile(r'^seq-(?P<seq>.+?)-step-')


# ---------- completed-sequence loading (local file reads — fail-safe) ----------


def load_complete_sequences(sequences_dir: Path) -> dict[str, dict[str, Any]]:
    """Map seq_id → sequence dict for every ``*.json`` under ``sequences_dir``
    whose ``status == 'complete'``. Fail-safe: a missing dir → {}, and an
    unreadable / malformed / mis-shaped file is skipped (never fabricates a
    completed sequence). The seq_id is the file's ``seq_id`` field, falling back
    to the filename stem (matching load_active_sequences / the advancer)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        if not sequences_dir.is_dir():
            return out
        paths = sorted(sequences_dir.glob('*.json'))
    except OSError as e:
        log(f'sequence-reconcile: sequences dir unreadable ({sequences_dir}): {e}')
        return out
    for p in paths:
        try:
            seq = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f'sequence-reconcile: skip unreadable sequence {p.name}: {e}')
            continue
        if not isinstance(seq, dict) or seq.get('status') != 'complete':
            continue
        seq_id = seq.get('seq_id') or p.stem
        if isinstance(seq_id, str) and seq_id:
            out[seq_id] = seq
    return out


# ---------- pure mission classification (unit-tested directly) ----------


def referenced_sequence_ids(task_ids: Any) -> set[str]:
    """Every build-sequence id referenced by a mission's ``seq-<id>-step-*``
    task_ids (a mission can, in principle, span more than one)."""
    out: set[str] = set()
    if not isinstance(task_ids, list):
        return out
    for tid in task_ids:
        if isinstance(tid, str):
            m = _SEQ_STEP_RE.match(tid)
            if m:
                out.add(m.group('seq'))
    return out


def is_active_mission(mission: Any) -> bool:
    """True for a mission this healer may act on: an identifiable, reconcilable-
    phase (drafting/in_flight/ready) mission that is not archived/acknowledged/
    retired. Mirrors heal_merged_pr_board_reconcile.is_offboard_active_mission's
    guards (minus the off-board predicate — this healer targets the opposite
    population: missions WITH sequence-step task_ids)."""
    if not isinstance(mission, dict):
        return False
    if not (isinstance(mission.get('id'), str) and mission.get('id')):
        return False
    if mission.get('phase') not in gc.RECONCILABLE_MISSION_PHASES:
        return False
    if mission.get('archived') is True:
        return False
    if mission.get('acknowledged') is True:
        return False
    if mission.get('retired_at'):
        return False
    return True


@dataclass(frozen=True)
class MissionDecision:
    action: str          # 'ship' | 'surface' | 'keep'
    seq_id: Optional[str]  # the owning sequence (ship); a representative one (surface)
    reason: str


def classify_mission(
    mission: dict[str, Any], complete_seq_ids: set[str],
) -> MissionDecision:
    """Decide a mission's fate given the set of seq_ids that are ``complete``.

    SHIP only when the mission's whole scope is one completed sequence: its
    probeable (PR-backed, non-review-shaped) task_ids are ALL steps of exactly
    ONE sequence and that sequence is complete. A mission spanning a second
    sequence OR carrying a non-sequence PR-backed task_id is an UMBRELLA — we
    SURFACE it (never auto-retire) once at least one of its sequences is complete.
    Everything else (no sequence task_ids, or none of its sequences complete yet)
    is KEEP. Conservative: ambiguity always falls to surface/keep, never ship."""
    if not is_active_mission(mission):
        return MissionDecision('keep', None, 'not an active reconcilable mission')
    task_ids = mission.get('task_ids')
    seq_refs = referenced_sequence_ids(task_ids)
    if not seq_refs:
        return MissionDecision('keep', None, 'no sequence-step task_ids')
    complete_refs = seq_refs & complete_seq_ids
    if not complete_refs:
        return MissionDecision('keep', None, 'no referenced sequence is complete yet')
    probeable = gc.probeable_task_ids(task_ids)
    non_seq_probeable = [t for t in probeable if not _SEQ_STEP_RE.match(t)]
    # Whole-scope iff exactly one referenced sequence AND no extra PR-backed work.
    # complete_refs (guarded non-empty above) guarantees that sole sequence is the
    # completed one, so this is an unconditional ship.
    if len(seq_refs) == 1 and not non_seq_probeable:
        return MissionDecision('ship', next(iter(seq_refs)),
                               'single-owner sequence complete')
    # Umbrella: spans >1 sequence or has standalone non-sequence work alongside a
    # completed sequence. Never auto-retire — surface for a human confirm.
    rep = sorted(complete_refs)[0]
    if len(seq_refs) > 1:
        reason = (f'umbrella: spans {len(seq_refs)} sequences '
                  f'({len(complete_refs)} complete)')
    else:
        reason = (f'umbrella: sequence complete but {len(non_seq_probeable)} '
                  f'non-sequence task(s) remain in scope')
    return MissionDecision('surface', rep, reason)


# ---------- ship mutation + surface record builders (pure) ----------


def _merged_pr_refs(seq: dict[str, Any]) -> list[str]:
    """`#<n>` refs for the merged steps of a sequence that carry a parseable PR
    url — purely to make the shipped note operator-readable. Best-effort; a step
    with no/odd pr_url is simply omitted."""
    refs: list[str] = []
    for step in seq.get('steps') or []:
        if not isinstance(step, dict) or step.get('status') != 'merged':
            continue
        n = gc.pr_number_from_url(step.get('pr_url'))
        if n:
            refs.append(f'#{n}')
    return refs


def build_shipped_note(seq_id: str, seq: dict[str, Any]) -> str:
    """The audit note stamped on an auto-shipped mission (mirrors the manual
    reconcile's ``shipped_note`` shape, e.g. operator-ux-catch-me-up-shortcut)."""
    steps = seq.get('steps') if isinstance(seq.get('steps'), list) else []
    refs = _merged_pr_refs(seq)
    refs_phrase = f' ({", ".join(refs)})' if refs else ''
    return (
        f'Build sequence `{seq_id}` reached status:complete — all {len(steps)} '
        f'step(s) merged{refs_phrase}. Single-owner mission auto-reconciled by '
        f'{SHIPPED_BY}: its task_ids are synthetic seq-*-step-* ids, so the '
        f'terminal-state reconciler could not probe them to a merged PR.'
    )


def apply_ship(mission: dict[str, Any], seq_id: str, seq: dict[str, Any],
               now: datetime) -> str:
    """Flip ``mission`` to ``shipped`` in place, audit-preserved (records the
    prior phase + provenance). Returns the prior phase for the audit line. The
    dashboard reader tolerates the extra keys (same shape the GC healer writes)."""
    prior = str(mission.get('phase'))
    mission['phase'] = gc.SHIPPED_PHASE
    mission['prior_phase'] = prior
    mission['shipped_at'] = now.isoformat()
    mission['shipped_by'] = SHIPPED_BY
    mission['shipped_note'] = build_shipped_note(seq_id, seq)
    return prior


def build_surface_record(mission: dict[str, Any], seq_id: str, reason: str,
                         now: datetime) -> dict[str, Any]:
    """A self-clearing for-Larry needs-you record for an umbrella mission whose
    sub-sequence shipped (shape mirrors heal_merged_pr_board_reconcile's)."""
    mid = mission.get('id')
    name = mission.get('name') or mid
    phase = mission.get('phase')
    return {
        'headline': f'Umbrella mission — a sub-sequence shipped: {name}',
        'context': (
            f"Build sequence `{seq_id}` is complete, but this mission spans more "
            f"than that one sequence ({reason}) and the card is still '{phase}'. "
            f"It is NOT auto-shipped (a single completed sub-sequence can't prove "
            f"the whole mission is done). Confirm the remaining scope shipped, or "
            f"advance it, in Missions."
        ),
        'suggested_action': 'Confirm shipped / advance remaining work in Missions',
        'severity': 'info',
        'mission_id': mid,
        'seq_id': seq_id,
        'source': SHIPPED_BY,
        'ts': now.isoformat(),
    }


# ---------- one reconcile tick ----------


@dataclass
class ReconcileResult:
    shipped: list[tuple[str, str, str]] = field(default_factory=list)  # (mid, prior, seq_id)
    surfaced: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> record
    written: list[str] = field(default_factory=list)                   # surface keys written
    resolved: list[str] = field(default_factory=list)                  # surface keys self-cleared
    kept: int = 0


def run_cycle(
    *, apply: bool,
    now: Optional[datetime] = None,
    sequences_dir: Optional[Path] = None,
    missions_reg_path: Optional[Path] = None,
    signal_path: Optional[Path] = None,
) -> ReconcileResult:
    """One reconcile tick. Fail-safe: a missing/unreadable registry or sequences
    dir degrades to "do nothing" — never raises, never falsely ships.

    apply=False (default) logs the planned snapshot and writes nothing; apply=True
    writes the missions.json ship delta (lost-update-guarded; the GC healer
    commits it) and syncs the umbrella needs-you signal."""
    now = now or datetime.now(timezone.utc)
    sequences_dir = sequences_dir if sequences_dir is not None else build_sequences_dir()
    if missions_reg_path is None:
        missions_reg_path = gc.missions_path(gc.load_repo_paths())

    res = ReconcileResult()

    # Bail on an unresolved board path BEFORE any sequence-file IO (a misconfigured
    # host shouldn't glob+parse the blackboard only to discard it).
    if missions_reg_path is None:
        log('sequence-reconcile: missions.json path unresolved (agent-core not in '
            'repo_paths) — nothing to do')
        return res
    registry = gc.read_missions_registry(missions_reg_path)
    if not isinstance(registry, dict):
        log('sequence-reconcile: missions registry unavailable/malformed — nothing to do')
        return res

    complete = load_complete_sequences(sequences_dir)
    complete_ids = set(complete)

    # Read-time snapshot so the pre-write merge applies ONLY this tick's per-mission
    # field deltas onto a fresh re-read (lost-update guard — board_drain pattern).
    # Only a completed sequence can produce a ship (the only thing we write), so on
    # the common no-complete tick the snapshot is skipped — we still walk the board
    # below to self-clear stale umbrella rows, but nothing is ever written.
    before_by_id = {
        m['id']: dict(m)
        for m in registry.get('missions', [])
        if isinstance(m, dict) and m.get('id')
    } if complete_ids else {}

    # Even with NO completed sequence we still walk the board: a previously-
    # surfaced umbrella whose mission has since advanced/archived must self-clear,
    # which sync_prefix does when its key drops out of `active` below.
    for mission in registry.get('missions') or []:
        if not isinstance(mission, dict):
            continue
        decision = classify_mission(mission, complete_ids)
        if decision.action == 'ship':
            seq = complete[decision.seq_id]
            mid = mission.get('id')
            if apply:
                prior = apply_ship(mission, decision.seq_id, seq, now)
            else:
                prior = str(mission.get('phase'))
            res.shipped.append((mid, prior, decision.seq_id))
            log(f"sequence-reconcile: mission {mid}: {prior} -> shipped "
                f"(sequence `{decision.seq_id}` complete){'' if apply else ' (dry-run)'}")
        elif decision.action == 'surface':
            mid = mission.get('id')
            res.surfaced[SIGNAL_PREFIX + str(mid)] = build_surface_record(
                mission, decision.seq_id, decision.reason, now)
            log(f"sequence-reconcile: mission {mid}: surface umbrella "
                f"({decision.reason})")
        else:
            res.kept += 1

    if not apply:
        log(f'sequence-reconcile DRY-RUN: would ship {len(res.shipped)} mission(s), '
            f'surface {len(res.surfaced)} umbrella(s); kept {res.kept}')
        return res

    # --- write the ship delta (NO commit — the GC healer is the sole missions.json
    # committer; it commits any pending on-disk delta on its next tick, exactly as
    # for a board-drain write). Re-read fresh and merge only the fields we changed
    # so a concurrent dashboard/healer write is never clobbered (lost-update guard).
    ship_write_ok = True
    if res.shipped:
        try:
            fresh = gc.read_missions_registry(missions_reg_path)
            if fresh is None:
                ship_write_ok = False
                log('sequence-reconcile: missions.json re-read malformed before write '
                    '— skipping write (avoids clobbering with stale)')
            else:
                merged = gc._merge_registry_deltas(
                    fresh, before_by_id, registry, list_key='missions')
                gc._atomic_write_json(missions_reg_path, fresh)
                log(f'sequence-reconcile: missions.json write: merged {merged} '
                    'mission delta(s) onto a fresh re-read (lost-update guard); '
                    'commit deferred to the missions committer')
        except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
            ship_write_ok = False
            log(f'sequence-reconcile: missions.json write failed: '
                f'{type(e).__name__}: {e}')

    # --- sync the umbrella needs-you signal. Called even when empty so a stale row
    # (umbrella since advanced/shipped) self-resolves. BUT skip the sync entirely
    # when this tick's ship write FAILED: a mission we classified `ship` is absent
    # from res.surfaced, so syncing would self-clear any stale umbrella row it
    # carried from a prior tick — dropping a mission that did NOT actually flip on
    # disk from BOTH surfaces (the exact stuck class this healer kills). Leaving the
    # signal untouched lets the next tick retry the ship and re-decide cleanly.
    if ship_write_ok:
        try:
            sync = for_larry_signal.sync_prefix(
                res.surfaced, SIGNAL_PREFIX, path=signal_path)
            res.written = sync.get('written', [])
            res.resolved = sync.get('resolved', [])
        except Exception as e:  # noqa: BLE001 — surfacing is best-effort, never fatal
            log(f'sequence-reconcile: needs-you sync failed: {type(e).__name__}: {e}')
    else:
        log('sequence-reconcile: ship write failed — deferring needs-you sync to '
            'next tick (avoids self-clearing a stale row for an unshipped mission)')

    log(f'sequence-reconcile: shipped {len(res.shipped)} mission(s), surfaced '
        f'{len(res.written)} umbrella(s), self-cleared {len(res.resolved)}; '
        f'kept {res.kept}')
    return res


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='heal_completed_sequence_mission_reconcile.py',
        description='Flip a single-owner mission to shipped when its build '
                    'sequence completes; surface umbrella missions to needs-you.')
    parser.add_argument(
        '--apply', action='store_true',
        help='write the board delta + the needs-you signal (default: dry-run)')
    args = parser.parse_args(argv)
    if gc._kill_switch_path().exists():
        log('KILLED_BY_SWITCH: healers.disabled present, exiting')
        return 0
    try:
        run_cycle(apply=args.apply)
    except Exception as exc:  # noqa: BLE001 — a healer tick must never crash-loop
        # Degrade QUIETLY (exit 0): a hard exit would mark the oneshot unit failed
        # and page Larry for something the next tick retries. Logged for scanners.
        log(f'sequence-reconcile: tick failed (degraded, no board change): '
            f'{type(exc).__name__}: {exc}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
