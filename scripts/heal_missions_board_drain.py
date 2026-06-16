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

  C. DROP VERIFIABLY-TERMINAL ORPHAN PROPOSALS (spec § 4 C2). The Proposed lane
     fills with `proposed_by == heal_orphan_autoregister` cards — one per orphan
     task_id. Many name work that has SINCE shipped or been abandoned, yet the
     card lingers because nothing closed it. This pass drops ONLY the cards whose
     EVERY task_id is verifiably terminal: a merged PR (task_resolution.shipped_pr)
     OR a closed-unmerged PR (_closed_unmerged_pr). Both are positive-evidence,
     PR-state gates — NEVER clock/age-based. Anything open, unresolvable, or that
     errs on lookup is KEPT (fail-safe — the conservative posture the whole GC
     family shares). A dropped card is RELABELED in place (additive flags:
     `acknowledged`, `retired_by`/`retired_at`/`retired_reason`/`retired_signal`),
     never deleted, so the phase stays intact and the row is recoverable.
     Idempotent: an already-acknowledged card is skipped on every later run.

  D. ARCHIVE LEGACY DRAFTING MISSIONS (spec § 4 C3). The board carries hand-seeded
     `phase == 'drafting'` missions with `proposed_by is None` that predate
     orphan-autoregister and never advanced. This pass RELABELS each in place with
     an additive `archived` flag (+ `archived_by`/`archived_at`/`archived_reason`)
     so the dashboard can hide them — phase intact, NEVER deleted, fully
     recoverable. Idempotent: an already-archived mission is skipped.

  B. SURFACE UNATTENDED PROPOSED-LANE ITEMS (spec § 3 S8). Every mission with
     `phase == 'proposed'` that has not been acknowledged is written to a
     batch-review artifact under ~/agents/blackboard/missions-board-drain/ so
     Larry can batch accept (→ drafting) / dismiss (acknowledged) via the
     dashboard. This pass NEVER mutates missions.json — proposed items are
     SURFACED, not auto-dismissed (Mirror focus). It runs AFTER passes C/D so the
     artifact reflects the drained board. Regenerating the artifact from the same
     board state is deterministic, so re-running is idempotent.

SINGLE-COMMITTER INVARIANT. This drain WRITES the captures.json AND missions.json
deltas atomically but does NOT git-commit either — heal_missions_card_gc /
heal_orphan_autoregister are the SOLE committers of those files (the same contract
the ingest endpoint honors: the drain writes, the owning healer commits the delta
on its next tick, which picks up ANY working-tree change to the path). Both writes
use the same lost-update guard the GC healer uses: snapshot at read time, re-read
fresh, and apply only this run's per-id field deltas — so a concurrent dashboard /
healer write made during the (minutes-long) `gh pr view` verify window survives.

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

# The shared "is this task_id already resolved?" belt — reused here for the
# merged-PR terminal gate (shipped_pr) and its PR matcher (pr_matches_task).
import task_resolution as tr  # noqa: E402

DRAIN_BY = 'heal_missions_board_drain'

# Pass C/D selectors + audit stamps.
# Orphan proposals all carry this producer; it distinguishes the auto-registered
# Proposed cards (drainable when terminal) from any hand-seeded proposal.
ORPHAN_PROPOSED_BY = 'heal_orphan_autoregister'
# Mirrors heal_orphan_autoregister's own terminal-retire reason so a drain-dropped
# card is indistinguishable from one the orphan healer retired (one vocabulary).
DRAIN_ORPHAN_RETIRE_REASON = 'orphan-terminal'
# The C3 archive reason for the legacy proposed_by:None drafting missions.
ARCHIVE_REASON_LEGACY_DRAFT = 'legacy-drafting-never-advanced'

# The terminal gate is PR-STATE based, never clock-based, so it must consider a
# PR no matter how long ago it merged/closed. shipped_pr filters out PRs older
# than `since_ts`; pass this far-past floor so NO terminal PR is filtered on age.
_TERMINAL_SINCE_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)

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


# ---------- pass C: drop verifiably-terminal orphan proposals -----------------


@dataclass
class OrphanDrainResult:
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (id, signal)
    kept_open: list[str] = field(default_factory=list)            # not terminal
    kept_indeterminate: list[str] = field(default_factory=list)   # lookup errored

    @property
    def changed(self) -> bool:
        """True iff a mission was relabeled (so the caller writes the delta)."""
        return bool(self.dropped)


def orphan_proposed_entries(missions_registry: dict[str, Any]) -> list[dict[str, Any]]:
    """The orphan-derived Proposed cards still on the board: `phase == 'proposed'`,
    `proposed_by == ORPHAN_PROPOSED_BY`, and NOT yet acknowledged. `acknowledged`
    is the off-board line (set by a dismiss / a prior retire), so an already-dropped
    card is skipped — that is what makes this pass idempotent. Read-only."""
    out: list[dict[str, Any]] = []
    for m in missions_registry.get('missions', []):
        if not isinstance(m, dict) or m.get('phase') != 'proposed':
            continue
        if m.get('proposed_by') != ORPHAN_PROPOSED_BY:
            continue
        if m.get('acknowledged') is True:
            continue
        out.append(m)
    return out


def _closed_unmerged_pr(
    task_id: str, *, repos: Optional[tuple] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """A CLOSED-unmerged PR whose branch/title matches `task_id`, else None.

    Mirrors task_resolution.shipped_pr but queries `--state closed` (gh's `closed`
    state EXCLUDES merged — those are `merged`), so this is the "abandoned /
    superseded" terminal signal that complements shipped_pr's "shipped". Reuses
    tr.pr_matches_task so the branch/title match rule is identical. Failsafe: any
    gh error skips that repo (so a lookup failure can never manufacture a drop)."""
    import json as _json
    import subprocess
    _log = log_fn or (lambda *_a, **_k: None)
    if not task_id or task_id == 'unknown':
        return None
    for repo in (repos or tr.DEFAULT_REPOS):
        try:
            res = subprocess.run(
                ['gh', 'pr', 'list', '--repo', repo, '--state', 'closed',
                 '--limit', '40', '--json', 'number,title,headRefName,state'],
                capture_output=True, text=True, timeout=tr.GH_TIMEOUT_S,
                env=tr._gh_env(),
            )
            if res.returncode != 0:
                _log(f'gh pr list (closed) {repo} rc={res.returncode}: '
                     f'{res.stderr[:160]}')
                continue
            prs = _json.loads(res.stdout or '[]')
        except (subprocess.TimeoutExpired, _json.JSONDecodeError,
                FileNotFoundError, OSError) as e:
            _log(f'gh pr list (closed) {repo} failed: {type(e).__name__}: {e}')
            continue
        for pr in prs:
            if tr.pr_matches_task(pr, task_id):
                pr['_repo'] = repo
                return pr
    return None


def _default_orphan_terminal_fn(
) -> Callable[[str, datetime], tuple[bool, Optional[str]]]:
    """Build the PR-state terminal gate: (True, signal) iff a `task_id`'s work is
    verifiably DONE — a merged PR (shipped_pr) OR a closed-unmerged PR
    (_closed_unmerged_pr). Positive evidence ONLY; no clock/age input. Any gh
    error inside the helpers degrades to no-match, so the gate returns
    (False, None) and the card is KEPT (fail-safe)."""
    def _terminal(task_id: str, since_ts: datetime) -> tuple[bool, Optional[str]]:
        pr = tr.shipped_pr(task_id, since_ts=since_ts, log_fn=gc.log)
        if pr is not None:
            return (True, f'pr_merged:#{pr.get("number")}')
        pr = _closed_unmerged_pr(task_id, log_fn=gc.log)
        if pr is not None:
            return (True, f'pr_closed:#{pr.get("number")}')
        return (False, None)
    return _terminal


def _verify_all_terminal(
    task_ids: list[str],
    terminal_fn: Callable[[str, datetime], tuple[bool, Optional[str]]],
) -> tuple[bool, Optional[str], bool]:
    """A card is droppable iff EVERY resolved task_id is verifiably terminal.
    Short-circuits on the first non-terminal (or erroring) task. Returns
    (terminal_all, first_signal, errored). An EMPTY task_ids list returns
    (False, None, False) — a card we cannot tie to verifiable work is never
    dropped."""
    signal: Optional[str] = None
    for tid in task_ids:
        try:
            terminal, sig = terminal_fn(tid, _TERMINAL_SINCE_FLOOR)
        except Exception as e:  # noqa: BLE001 — per-task fail-safe: keep the card
            gc.log(f'drain: terminal check failed for {tid}: '
                   f'{type(e).__name__}: {e} — keep')
            return (False, signal, True)
        if signal is None and isinstance(sig, str) and sig:
            signal = sig
        if not terminal:
            return (False, signal, False)
    return (bool(task_ids), signal, False)


def reconcile_dead_orphans(
    missions_registry: dict[str, Any],
    now: datetime,
    *,
    terminal_fn: Callable[[str, datetime], tuple[bool, Optional[str]]],
    dry_run: bool,
) -> OrphanDrainResult:
    """Drop (relabel, never delete) every orphan Proposed card whose every
    task_id is verifiably terminal (spec § 4 C2). Effectful ONLY on the in-memory
    registry — the caller owns the single atomic write (and never commits; the
    missions healer is the sole committer). Idempotent: a dropped card is
    `acknowledged`, so it is skipped on every later run. Fail-safe per card: an
    open/unresolvable/erroring lookup KEEPS the card."""
    res = OrphanDrainResult()
    for m in orphan_proposed_entries(missions_registry):
        mid = m.get('id') if isinstance(m.get('id'), str) else '<unknown>'
        task_ids = [t for t in (m.get('task_ids') or [])
                    if isinstance(t, str) and t]
        terminal_all, signal, errored = _verify_all_terminal(task_ids, terminal_fn)
        if not terminal_all:
            if errored:
                res.kept_indeterminate.append(mid)
            else:
                res.kept_open.append(mid)
            continue
        sig = signal or 'terminal'
        if dry_run:
            res.dropped.append((mid, sig + ' (dry-run)'))
            continue
        m['acknowledged'] = True
        m['retired_by'] = DRAIN_BY
        m['retired_at'] = now.isoformat()
        m['retired_reason'] = DRAIN_ORPHAN_RETIRE_REASON
        m['retired_signal'] = sig
        res.dropped.append((mid, sig))
        gc.log(f'drain: orphan proposal {mid} terminal [{sig}] -> dropped '
               '(acknowledged)')
    return res


# ---------- pass D: archive legacy drafting missions --------------------------


@dataclass
class ArchiveResult:
    archived: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.archived)


def legacy_drafting_missions(missions_registry: dict[str, Any]) -> list[dict[str, Any]]:
    """The hand-seeded legacy drafts: `phase == 'drafting'`, `proposed_by is None`
    (they predate orphan-autoregister), and NOT yet archived. An already-archived
    mission is skipped, which makes the archive pass idempotent. Read-only."""
    out: list[dict[str, Any]] = []
    for m in missions_registry.get('missions', []):
        if not isinstance(m, dict) or m.get('phase') != 'drafting':
            continue
        if m.get('proposed_by') is not None:
            continue
        if m.get('archived') is True:
            continue
        out.append(m)
    return out


def archive_legacy_drafts(
    missions_registry: dict[str, Any],
    now: datetime,
    *,
    dry_run: bool,
) -> ArchiveResult:
    """Relabel each legacy draft with an additive `archived` flag (spec § 4 C3).
    Phase is left INTACT and the row is never deleted — the dashboard hides
    archived missions, and clearing the flag fully restores the card. Effectful
    ONLY on the in-memory registry (caller writes, healer commits). Idempotent."""
    res = ArchiveResult()
    for m in legacy_drafting_missions(missions_registry):
        mid = m.get('id') if isinstance(m.get('id'), str) else '<unknown>'
        if dry_run:
            res.archived.append(mid)
            continue
        m['archived'] = True
        m['archived_by'] = DRAIN_BY
        m['archived_at'] = now.isoformat()
        m['archived_reason'] = ARCHIVE_REASON_LEGACY_DRAFT
        res.archived.append(mid)
        gc.log(f'drain: legacy drafting mission {mid} -> archived (in place)')
    return res


def _merge_mission_deltas(
    fresh: dict[str, Any],
    before_by_id: dict[str, dict[str, Any]],
    worked: dict[str, Any],
) -> int:
    """Missions twin of gc._merge_capture_deltas, keyed by mission `id`. Applies
    ONLY the per-mission field changes THIS run made (`worked` vs the read-time
    snapshot `before_by_id`) onto a freshly re-read `fresh` registry. A mission
    gone from `fresh`, or absent at read time, is left untouched — so a concurrent
    dashboard / healer write made during the verify window survives instead of
    being clobbered by the stale in-memory copy (§ 2 lost-update class)."""
    fresh_by_id = {
        m['id']: m for m in fresh.get('missions', [])
        if isinstance(m, dict) and m.get('id')
    }
    applied = 0
    for m in worked.get('missions', []):
        if not isinstance(m, dict):
            continue
        mid = m.get('id')
        if not mid:
            continue
        before = before_by_id.get(mid)
        if before is None:
            continue  # not present at read time — we never authored onto it
        set_fields = {k: v for k, v in m.items()
                      if k not in before or before[k] != v}
        removed = [k for k in before if k not in m]
        if not set_fields and not removed:
            continue  # untouched this run — leave the fresh copy alone
        target = fresh_by_id.get(mid)
        if target is None:
            continue  # gone from the fresh file — drop our stale delta
        for k, v in set_fields.items():
            target[k] = v
        for k in removed:
            target.pop(k, None)
        applied += 1
    return applied


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
    orphan_terminal_fn: Optional[Callable[[str, datetime], tuple[bool, Optional[str]]]] = None,
    captures_reg_path: Optional[Path] = None,
    missions_reg_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """One drain run. The injectable seams (verify_fn / orphan_terminal_fn /
    paths / now) keep the effectful edges test-controllable; production resolves
    paths from config/agent-models.json, the verify gate from the GC healer's
    belt-and-suspenders factory, and the orphan terminal gate from
    _default_orphan_terminal_fn. Returns a process exit code (0 = ok)."""
    now = now or datetime.now(timezone.utc)
    repo_paths = gc.load_repo_paths()
    cap_path = captures_reg_path or gc.captures_path(repo_paths)
    miss_path = missions_reg_path or gc.missions_path(repo_paths)

    missions_registry: Optional[dict[str, Any]] = None
    if miss_path is not None:
        missions_registry = gc.read_missions_registry(miss_path)
    if missions_registry is None:
        missions_registry = {'schema_version': 1, 'missions': []}

    # Snapshot missions at read time so the pre-write merge applies ONLY this
    # run's per-mission field deltas onto a fresh re-read (lost-update guard,
    # mirroring the captures pass below). Passes C/D mutate missions_registry.
    missions_before_by_id = {
        m['id']: dict(m)
        for m in missions_registry.get('missions', [])
        if isinstance(m, dict) and m.get('id')
    }

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
            # Snapshot read-time state so the pre-write merge applies ONLY this
            # run's per-capture field deltas onto a fresh re-read (shallow per
            # capture is enough — reconcile_promoted_drafts replaces top-level
            # keys, never mutates nested objects in place).
            before_by_id = {
                c['id']: dict(c)
                for c in captures_registry.get('captures', [])
                if isinstance(c, dict) and c.get('id')
            }
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
                # Write the captures delta; the GC healer commits it (single-
                # committer invariant — the drain never commits). The verify loop
                # above can span minutes of `gh pr view` round-trips on a stale
                # board, during which the GC daemon or the dashboard ingest (both
                # SEPARATE processes) may add or state-change a capture. Writing
                # the stale in-memory snapshot back wholesale would clobber those
                # (#409→#413 lost-update class, spec § 2). So mirror the healer's
                # guard: re-read FRESH and apply only this run's per-capture field
                # deltas by id, shrinking the read→write window to sub-millisecond.
                if promoted.changed and not dry_run:
                    try:
                        fresh = gc.read_captures_registry(cap_path)
                        if fresh is None:
                            gc.log('drain: captures.json re-read malformed before '
                                   'write — skipping write (avoids clobbering with stale)')
                        else:
                            merged = gc._merge_capture_deltas(
                                fresh, before_by_id, captures_registry)
                            gc.atomic_write_captures(cap_path, fresh)
                            gc.log(f'drain: captures.json write: merged {merged} '
                                   'capture delta(s) onto a fresh re-read (lost-update guard)')
                    except Exception as e:  # noqa: BLE001 — fail-safe
                        gc.log(f'drain: captures.json write failed: {type(e).__name__}: {e}')

    # --- pass C: drop verifiably-terminal orphan proposals ---
    orphans = OrphanDrainResult()
    candidates = orphan_proposed_entries(missions_registry)
    if candidates:
        # Build the gh-backed terminal gate ONLY when there is work — keeps the
        # gh round-trips (and the import side effects) off the no-candidate path,
        # and lets tests run without creds by injecting orphan_terminal_fn.
        terminal_fn = orphan_terminal_fn
        if terminal_fn is None:
            try:
                terminal_fn = _default_orphan_terminal_fn()
            except Exception as e:  # noqa: BLE001 — degrade: skip the orphan pass
                gc.log(f'drain: orphan terminal gate unavailable: '
                       f'{type(e).__name__}: {e} — skipping orphan-drain pass')
                terminal_fn = None
        if terminal_fn is not None:
            try:
                orphans = reconcile_dead_orphans(
                    missions_registry, now, terminal_fn=terminal_fn, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
                gc.log(f'drain: orphan-drain pass raised: {type(e).__name__}: {e}')
                orphans = OrphanDrainResult()

    # --- pass D: archive legacy drafting missions ---
    archived = ArchiveResult()
    try:
        archived = archive_legacy_drafts(missions_registry, now, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — fail-safe: report, never corrupt
        gc.log(f'drain: legacy-archive pass raised: {type(e).__name__}: {e}')
        archived = ArchiveResult()

    # Write the missions.json delta; the missions healer commits it (single-
    # committer invariant — the drain never commits). Same lost-update guard as
    # the captures pass: re-read FRESH and merge only this run's per-mission
    # field deltas by id (the gh verify loop in pass C can span minutes, during
    # which the dashboard / a healer may write missions.json).
    if (orphans.changed or archived.changed) and not dry_run and miss_path is not None:
        try:
            fresh = gc.read_missions_registry(miss_path)
            if fresh is None:
                gc.log('drain: missions.json re-read malformed before write — '
                       'skipping write (avoids clobbering with stale)')
            else:
                merged = _merge_mission_deltas(
                    fresh, missions_before_by_id, missions_registry)
                gc._atomic_write_json(miss_path, fresh)
                gc.log(f'drain: missions.json write: merged {merged} mission '
                       'delta(s) onto a fresh re-read (lost-update guard); commit '
                       'deferred to the missions committer')
        except Exception as e:  # noqa: BLE001 — fail-safe
            gc.log(f'drain: missions.json write failed: {type(e).__name__}: {e}')

    # --- pass B: surface unattended proposed-lane items (post-drain board) ---
    surfaced = surface_proposed_backlog(missions_registry, now, dry_run=dry_run)

    _emit_summary(promoted, orphans, archived, surfaced, dry_run)
    return 0


def _emit_summary(promoted: PromotedDrainResult, orphans: OrphanDrainResult,
                  archived: ArchiveResult, surfaced: ProposedSurfaceResult,
                  dry_run: bool) -> None:
    close_verb = 'would close' if dry_run else 'closed'
    review_verb = 'would move' if dry_run else 'moved'
    drop_verb = 'would drop' if dry_run else 'dropped'
    archive_verb = 'would archive' if dry_run else 'archived'
    surface_verb = 'would surface' if dry_run else 'surfaced'
    orphan_kept = len(orphans.kept_open) + len(orphans.kept_indeterminate)
    parts = [
        'DRY-RUN ' if dry_run else '',
        f'drain: {close_verb} {len(promoted.closed)} promoted draft(s) to done; ',
        f'{review_verb} {len(promoted.closeouts)} to review_close; ',
        f'kept {promoted.kept} ({len(promoted.unresolved)} unresolved); ',
        f'{drop_verb} {len(orphans.dropped)} terminal orphan proposal(s) '
        f'(kept {orphan_kept}: {len(orphans.kept_open)} open, '
        f'{len(orphans.kept_indeterminate)} indeterminate); ',
        f'{archive_verb} {len(archived.archived)} legacy drafting mission(s); ',
        f'{surface_verb} {surfaced.count} unattended proposed item(s)',
    ]
    if not dry_run and surfaced.written:
        parts.append(f' -> {surfaced.artifact_path}')
    gc.log(''.join(parts))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='heal_missions_board_drain.py',
        description=(
            'One-time idempotent drain of the stale Missions board: close '
            'already-merged promoted drafts; drop verifiably-terminal orphan '
            'proposals (PR merged/closed, never clock-based); archive legacy '
            'drafting missions in place; surface the unattended Proposed lane '
            'for batch accept/dismiss. Dry-run by default; pass --apply to write.'
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
