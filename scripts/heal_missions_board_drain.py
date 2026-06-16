#!/usr/bin/env python3
"""heal_missions_board_drain.py — Missions v2 Phase S, step S-6 / contract S8.

A ONE-TIME, idempotent reconciliation that drains the stale board so it starts
clean — and then S-2's ongoing GC auto-close keeps it that way. Two passes, both
idempotent / atomic / fail-safe (a bad pass reports + skips, never corrupts):

  A. CLOSE ALREADY-MERGED PROMOTED DRAFTS (spec § 3 S8 + S3). The ongoing GC
     healer (heal_missions_card_gc) closes only `parked` captures (its
     reconcile_completed_cards has a `state == 'parked'` guard) and flips only
     `drafting`/`in_flight`/`ready` MISSIONS to `shipped`. A capture that was
     `promoted` to a mission is never closed by either path, so a promoted draft
     whose work already merged sits on the board forever. This pass closes those:
     for each `state == 'promoted'` capture it resolves the spawned work's
     task_id(s) (via `spawned.task_id`, else the promoted mission's `task_ids`)
     and runs the SAME belt-and-suspenders verified-merge gate S-2 uses
     (chain_events `auto_merge` AND `gh pr view MERGED`, BOTH required, for EVERY
     task_id). Only a fully verified-merged draft is closed; anything
     unresolved/indeterminate is KEPT (fail-safe — the conservative posture the
     whole GC family shares). Routing reuses S-2's `classify_completion`: a
     `safe` draft auto-closes to `done` with a "shipped in PR #X" note; a
     `medium`/`careful`/un-briefed draft moves to `review_close` (awaiting Larry's
     ack) so a risky card is never silently closed.

  B. SURFACE UNATTENDED PROPOSED-LANE ITEMS (spec § 3 S8). Every mission with
     `phase == 'proposed'` that has not been acknowledged is written to a
     batch-review artifact under ~/agents/blackboard/missions-board-drain/ so
     Larry can batch accept (→ drafting) / dismiss (acknowledged) via the
     dashboard. This pass NEVER mutates missions.json — proposed items are
     SURFACED, not auto-dismissed (Mirror focus). Regenerating the artifact from
     the same board state is deterministic, so re-running is idempotent.

SINGLE-COMMITTER INVARIANT. This drain WRITES the captures.json delta atomically
but does NOT git-commit it — heal_missions_card_gc is the SOLE captures.json
committer (the same contract the ingest endpoint honors: it writes, the healer
commits the delta on its next tick). missions.json is never written at all.

Dry-run by default; pass --apply to write. Idempotent: a closed draft is no
longer `promoted` (so pass A skips it next run), and pass B only ever rewrites
the surfacing artifact. stdlib only (+ supabase-py via heal_missions_card_gc's
lazy verify gate, optional — degrades to surface-only if unavailable).

    cd ~/agent-core && python3 scripts/heal_missions_board_drain.py           # dry-run
    cd ~/agent-core && python3 scripts/heal_missions_board_drain.py --apply   # write
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports resolve under systemd / discover.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Reuse the GC healer's pure helpers + fail-safe IO so the drain and the ongoing
# loop share ONE definition of "verified-merged", "shipped note", the risk
# routing, and the atomic-write path (no second source of truth to drift).
import heal_missions_card_gc as gc  # noqa: E402

DRAIN_BY = 'heal_missions_board_drain'

# A deterministic closeout stamped on a risky promoted draft drained to review.
# The drain is a one-shot board-clean, not the live loop, so it does NOT invoke
# the Narrator (no LLM dependency in a one-time script): it leaves a plain,
# operator-readable note and the card awaits Larry's ack like any review_close.
DRAIN_CLOSEOUT_NOTE = 'Linked work already merged (board drain) — review & close.'

# The proposed-backlog surfacing artifact lives in the blackboard (gitignored
# operational state, NOT the repo) so it never dirties the working tree.
_DRAIN_ARTIFACT_SUBDIR = 'missions-board-drain'


def _agents_root() -> Path:
    """Agents root, honoring the OURLIBERTY_AGENTS_ROOT test/CI override (mirrors
    heal_missions_card_gc._agents_root so the drain and the healer resolve the
    same tree)."""
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def drain_artifact_path(now: datetime) -> Path:
    """Path to the date-stamped proposed-backlog surfacing artifact."""
    return (_agents_root() / 'blackboard' / _DRAIN_ARTIFACT_SUBDIR
            / f'proposed-batch-{now:%Y-%m-%d}.json')


# ---------- pass A: close already-merged promoted drafts --------------------


@dataclass
class PromotedDrainResult:
    closed: list[tuple[str, str]] = field(default_factory=list)   # (id, note)
    closeouts: list[str] = field(default_factory=list)            # ids → review_close
    unresolved: list[str] = field(default_factory=list)           # no resolvable task_id
    kept: int = 0

    @property
    def changed(self) -> bool:
        """True iff a capture was mutated (so the caller writes the delta)."""
        return bool(self.closed or self.closeouts)


def _missions_by_id(missions_registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in missions_registry.get('missions', []):
        if isinstance(m, dict) and isinstance(m.get('id'), str) and m.get('id'):
            out[m['id']] = m
    return out


def resolve_draft_task_ids(
    capture: dict[str, Any], missions_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """The spawned-work task_id(s) that verify a promoted draft's merge.

    Join order (S-1's stamps, then the promote link):
      1. `spawned.task_id` (delegate kind) — a direct join key.
      2. else the promoted mission's `task_ids` — resolved via `spawned.mission_id`
         (mission kind) or, failing that, the `promoted_to` mission id (the
         promote path sets `promoted_to = mission_id`).
    Returns [] when nothing resolves — the caller then KEEPS the card (a draft we
    cannot tie to verifiable work is never closed)."""
    spawned = capture.get('spawned') if isinstance(capture.get('spawned'), dict) else {}
    tid = spawned.get('task_id')
    if isinstance(tid, str) and tid:
        return [tid]
    mid = spawned.get('mission_id')
    if not (isinstance(mid, str) and mid):
        pt = capture.get('promoted_to')
        mid = pt if (isinstance(pt, str) and pt) else None
    if mid and mid in missions_by_id:
        task_ids = missions_by_id[mid].get('task_ids')
        if isinstance(task_ids, list):
            return [t for t in task_ids if isinstance(t, str) and t]
    return []


def _verify_all_merged(
    task_ids: list[str], dispatched_at: Optional[str],
    verify_fn: Callable[[str, Optional[str]], tuple[bool, Optional[str]]],
) -> tuple[bool, Optional[str]]:
    """A draft is verified-merged iff EVERY resolved task_id passes the
    belt-and-suspenders gate. Short-circuits on the first non-verified (or
    erroring) task so an unmerged draft costs at most one gh round-trip past its
    first live task. Returns (verified_all, first_pr_url)."""
    pr_url: Optional[str] = None
    for tid in task_ids:
        try:
            verified, this_pr = verify_fn(tid, dispatched_at)
        except Exception as e:  # noqa: BLE001 — per-task fail-safe: keep the card
            gc.log(f'drain: verify failed for {tid}: {type(e).__name__}: {e} — keep')
            return (False, pr_url)
        if pr_url is None and isinstance(this_pr, str) and this_pr:
            pr_url = this_pr
        if not verified:
            return (False, pr_url)
    return (True, pr_url)


def reconcile_promoted_drafts(
    captures_registry: dict[str, Any],
    missions_by_id: dict[str, dict[str, Any]],
    now: datetime,
    *,
    verify_fn: Callable[[str, Optional[str]], tuple[bool, Optional[str]]],
    dry_run: bool,
) -> PromotedDrainResult:
    """Close every `state == 'promoted'` capture whose spawned work is
    verified-merged (spec § 3 S8). Effectful ONLY on the in-memory registry — the
    caller owns the single atomic write (and never commits; the GC healer is the
    sole committer). Idempotent: a closed/review_close card is no longer
    `promoted`, so it is skipped on every later run. Fail-safe per capture: an
    unresolved join or a verify error KEEPS the card."""
    res = PromotedDrainResult()
    for cap in captures_registry.get('captures', []):
        if not isinstance(cap, dict) or cap.get('state') != 'promoted':
            continue
        cid = cap.get('id') if isinstance(cap.get('id'), str) else '<unknown>'
        task_ids = resolve_draft_task_ids(cap, missions_by_id)
        if not task_ids:
            res.unresolved.append(cid)
            res.kept += 1
            gc.log(f'drain: promoted draft {cid} — no resolvable task_id, keep')
            continue
        spawned = cap.get('spawned') if isinstance(cap.get('spawned'), dict) else {}
        dispatched_at = (spawned.get('stamped_at')
                         if isinstance(spawned.get('stamped_at'), str) else None)
        verified, pr_url = _verify_all_merged(task_ids, dispatched_at, verify_fn)
        decision = gc.classify_completion(cap, verified_merged=verified)
        if decision.action == 'keep':
            res.kept += 1
            continue
        if decision.action == 'auto_close':
            note = gc.shipped_note(pr_url)
            if dry_run:
                res.closed.append((cid, note + ' (dry-run)'))
                continue
            cap['state'] = gc.COMPLETED_STATE_DONE
            cap['closed_at'] = now.isoformat()
            cap['closed_by'] = DRAIN_BY
            cap['shipped_note'] = note
            if pr_url:
                cap['shipped_pr_url'] = pr_url
            cap.pop('aging', None)
            res.closed.append((cid, note))
            gc.log(f'drain: promoted draft {cid} (safe) -> done [{note}]')
            continue
        # closeout: medium / careful / un-briefed → review_close (awaiting ack).
        if dry_run:
            res.closeouts.append(cid)
            continue
        cap['state'] = gc.COMPLETED_STATE_REVIEW
        cap['awaiting_ack'] = True
        cap['closed_by'] = DRAIN_BY
        cap['shipped_note'] = gc.shipped_note(pr_url)
        cap['drain_closeout'] = DRAIN_CLOSEOUT_NOTE
        if pr_url:
            cap['shipped_pr_url'] = pr_url
        cap.pop('aging', None)
        res.closeouts.append(cid)
        gc.log(f'drain: promoted draft {cid} ({cap.get("risk") or "unbriefed"}) '
               f'-> review_close')
    return res


# ---------- pass B: surface unattended proposed-lane items -------------------


@dataclass
class ProposedSurfaceResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    artifact_path: Optional[Path] = None
    written: bool = False

    @property
    def count(self) -> int:
        return len(self.items)


def gather_proposed_backlog(missions_registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every `phase == 'proposed'` mission that is NOT acknowledged — the
    unattended Proposed lane. `acknowledged: true` (set by the dashboard dismiss
    action) is the dividing line, so a dismissed thread is already off the board
    and is not re-surfaced. Read-only on the registry."""
    out: list[dict[str, Any]] = []
    for m in missions_registry.get('missions', []):
        if not isinstance(m, dict) or m.get('phase') != 'proposed':
            continue
        if m.get('acknowledged') is True:
            continue
        out.append({
            'id': m.get('id'),
            'name': m.get('name'),
            'task_ids': m.get('task_ids') if isinstance(m.get('task_ids'), list) else [],
            'brief': m.get('brief'),
            'created': m.get('created'),
        })
    return out


def build_surface_artifact(items: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """The batch-review artifact body Larry acts on. Self-describing so a stranger
    reading the file knows it is a surfacing list, not an applied change."""
    return {
        'schema_version': 1,
        'generated_at': now.isoformat(),
        'generated_by': DRAIN_BY,
        'note': ('Unattended Proposed-lane missions surfaced for batch '
                 'accept/dismiss. These are SURFACED, not auto-dismissed — accept '
                 '(-> drafting) or dismiss (acknowledged) each via the dashboard '
                 'POST /api/missions/{id}/action.'),
        'count': len(items),
        'items': items,
    }


def surface_proposed_backlog(
    missions_registry: dict[str, Any],
    now: datetime,
    *,
    dry_run: bool,
) -> ProposedSurfaceResult:
    """Write the unattended Proposed-lane backlog to the batch-review artifact
    (spec § 3 S8). NEVER mutates missions.json — surfacing only. Idempotent:
    regenerating from the same board state yields the same artifact."""
    items = gather_proposed_backlog(missions_registry)
    res = ProposedSurfaceResult(items=items, artifact_path=drain_artifact_path(now))
    if dry_run:
        return res
    gc._atomic_write_json(res.artifact_path, build_surface_artifact(items, now))
    res.written = True
    gc.log(f'drain: surfaced {res.count} unattended proposed item(s) -> {res.artifact_path}')
    return res


# ---------- orchestration ----------------------------------------------------


def run_drain(
    *,
    dry_run: bool,
    verify_fn: Optional[Callable[[str, Optional[str]], tuple[bool, Optional[str]]]] = None,
    captures_reg_path: Optional[Path] = None,
    missions_reg_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """One drain run. The injectable seams (verify_fn / paths / now) keep the
    effectful edges test-controllable; production resolves paths from
    config/agent-models.json and the verify gate from the GC healer's
    belt-and-suspenders factory. Returns a process exit code (0 = ok)."""
    now = now or datetime.now(timezone.utc)
    repo_paths = gc.load_repo_paths()
    cap_path = captures_reg_path or gc.captures_path(repo_paths)
    miss_path = missions_reg_path or gc.missions_path(repo_paths)

    missions_registry: Optional[dict[str, Any]] = None
    if miss_path is not None:
        missions_registry = gc.read_missions_registry(miss_path)
    if missions_registry is None:
        missions_registry = {'schema_version': 1, 'missions': []}

    # --- pass A: close already-merged promoted drafts ---
    promoted = PromotedDrainResult()
    if cap_path is None:
        gc.log('drain: captures.json path unresolved (agent-core not in repo_paths) '
               '— skipping promoted-draft pass')
    else:
        captures_registry = gc.read_captures_registry(cap_path)
        if captures_registry is None:
            gc.log('drain: captures.json unreadable/malformed — skipping promoted-draft pass')
        else:
            if verify_fn is None:
                try:
                    verify_fn = gc._default_completion_verify_fn()
                except Exception as e:  # noqa: BLE001 — degrade: surface-only this run
                    gc.log(f'drain: verify gate unavailable: {type(e).__name__}: {e} '
                           '— skipping promoted-draft pass')
            if verify_fn is not None:
                try:
                    promoted = reconcile_promoted_drafts(
                        captures_registry, _missions_by_id(missions_registry), now,
                        verify_fn=verify_fn, dry_run=dry_run)
                except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                    gc.log(f'drain: promoted-draft pass raised: {type(e).__name__}: {e}')
                    promoted = PromotedDrainResult()
                # Write the captures delta atomically; the GC healer commits it
                # (single-committer invariant — the drain never commits).
                if promoted.changed and not dry_run:
                    try:
                        gc.atomic_write_captures(cap_path, captures_registry)
                    except OSError as e:
                        gc.log(f'drain: captures.json write failed: {type(e).__name__}: {e}')

    # --- pass B: surface unattended proposed-lane items ---
    surfaced = surface_proposed_backlog(missions_registry, now, dry_run=dry_run)

    _emit_summary(promoted, surfaced, dry_run)
    return 0


def _emit_summary(promoted: PromotedDrainResult, surfaced: ProposedSurfaceResult,
                  dry_run: bool) -> None:
    close_verb = 'would close' if dry_run else 'closed'
    review_verb = 'would move' if dry_run else 'moved'
    surface_verb = 'would surface' if dry_run else 'surfaced'
    parts = [
        'DRY-RUN ' if dry_run else '',
        f'drain: {close_verb} {len(promoted.closed)} promoted draft(s) to done; ',
        f'{review_verb} {len(promoted.closeouts)} to review_close; ',
        f'kept {promoted.kept} ({len(promoted.unresolved)} unresolved); ',
        f'{surface_verb} {surfaced.count} unattended proposed item(s)',
    ]
    if not dry_run and surfaced.written:
        parts.append(f' -> {surfaced.artifact_path}')
    gc.log(''.join(parts))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='heal_missions_board_drain.py',
        description=(
            'One-time idempotent drain of the stale Missions board (Phase S, '
            'S-6 / S8): close already-merged promoted drafts + surface the '
            'unattended Proposed lane for batch accept/dismiss. Dry-run by '
            'default; pass --apply to write.'
        ),
    )
    parser.add_argument(
        '--apply', action='store_true',
        help='Actually write the captures delta + surfacing artifact. '
             'Default: dry-run (no writes).',
    )
    args = parser.parse_args(argv)
    mode = 'LIVE' if args.apply else 'DRY-RUN'
    gc.log(f'drain: starting ({mode})')
    return run_drain(dry_run=not args.apply)


if __name__ == '__main__':
    raise SystemExit(main())
