#!/usr/bin/env python3
"""mirror_review_supersede.py — cancel a LIVE Mirror review before a Forge
revision is dispatched for the same PR, so the review cannot return a verdict
against a head that moved underneath it.

THE GAP THIS CLOSES
-------------------
`outbox_notifier._dispatch_mirror_review` already refuses to dispatch a review
while a Forge revision is in flight (`revision_in_flight_ledger.is_in_flight`).
The CONVERSE never existed: nothing on the revision-dispatch side asks whether a
Mirror review is executing RIGHT NOW. Two paths write a Forge task that pushes
to the PR branch:

  1. `outbox_notifier._dispatch_revision_to_forge` — Mirror's REVIEW_REVISION.
  2. `beacon_approval_handler.dispatch_approved` — Larry clicks **Approve** on a
     session-less `mirror-review-*` escalation card.

Either one, fired while a review is mid-flight, has Forge push its
`[WIP][session-start]` checkpoint and work commits under the reviewer. Mirror
then posts findings + a commit status against a head that no longer exists —
the stale-verdict harm of PR #865, now reachable on a HUMAN CLICK.

WHY SUPERSEDE AND NOT SUPPRESS (Larry's call, 2026-07-28)
---------------------------------------------------------
The obvious guard — "a review is live, so skip the revision dispatch" — is
actively harmful here, and this module exists specifically so nobody writes it.
The approve path is triggered by a human CLICK, not a retry loop, so there is
nothing to defer INTO: suppressing makes Approve silently do nothing. The card
resolves, the PR sits, and there is no "I fixed it myself" exit — the
approval-reject-strands-pr shape, which is the exact strand class agent-core
PR #1043 was written to remove. So the click always wins: cancel the review
first, then dispatch. Every failure mode below therefore degrades toward
DISPATCHING ANYWAY (today's behavior, a possible stale verdict) and never
toward withholding the dispatch.

WHAT "CANCEL" MEANS, PER RECORD STATE
-------------------------------------
A review is a record file that moves through Mirror's inbox:

  * **queued** — `inboxes/mirror/<review-stem>[...].json`, not yet claimed.
    Renamed to `.archive/<name>.superseded-<ts>.json`. Nothing has spawned, so
    this is complete and free: `inbox_watcher.scan_inbox` never sees it again.

  * **claimed** — `inboxes/mirror/.claimed/<slot>/<name>.json`; a slot claimed
    it (an `os.rename` performed BEFORE the claude spawn) and a paid review is
    executing. Two things must happen and the ORDER matters:
      1. archive the claim FIRST. The slot loop's `finally` re-queues a claimed
         file that still exists when `process_task` returns (its transient-defer
         contract), and `heal_orphaned_mirror_claims` re-INJECTS a
         not-concluded claim it finds stranded. Leave the record in place and
         the review we just cancelled comes straight back.
      2. then `task_cancel.request_cancel` so `agent_runner.run_claude`'s 5s
         poll SIGTERMs the session. We deliberately do NOT kill the process
         ourselves: agent_runner owns the terminate→grace→SIGKILL ladder, the
         in-flight registry cleanup and the lease release, and re-implementing
         that from a notifier hop is how you strand a slot.

THE CANCEL-MARKER CLOBBER TRAP (read before touching the ordering)
------------------------------------------------------------------
`task_cancel`'s marker is keyed on the TASK STEM, and a Mirror review reuses the
Forge build's `task_id` as its stem (`agent_work_in_flight`'s docstring records
the same collision biting the in-flight marker). The Forge revision we are about
to dispatch carries THAT SAME task_id — so a marker left on disk would cancel
the revision the moment it spawns, converting "supersede" into the strand this
module exists to prevent.

`agent_runner` clears the marker itself the instant it acts on it, so the
marker's ABSENCE is the acknowledgement. `await_cancel_ack` polls for that
absence over a bound comfortably wider than `CANCEL_POLL_INTERVAL` (5s) and, if
the marker is still there at the end, clears it anyway and reports
`ack=False`. That residual case — a review that exited between our write and its
next poll — lands back on exactly today's behavior (a possibly-stale verdict),
never on a cancelled revision.

CONTRACT
--------
  * `find_live_review_records` is a PURE READ (globs + `stat`), fail-open per
    directory: an unreadable dir is skipped, so an fs hiccup can only make us
    supersede LESS, never wedge a dispatch.
  * `supersede_live_review` NEVER raises. Every partial failure is reported on
    the returned `SupersedeOutcome` for the caller to log; the caller dispatches
    regardless.
  * Content-free logging: this module returns record NAMES and counts. It never
    reads or returns review bodies, findings or PR text.
"""

from __future__ import annotations

import glob as _glob
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import task_cancel  # noqa: E402  # the graceful in-flight cancel marker

HOME = Path.home()

# Production default. The live value is resolved at CALL time by `agents_root()`
# below (not frozen at import) so a test that sets the env after import still
# redirects — the convention `pipeline_live_state` / the healers follow. The env
# read is inlined here too because `agent_runner` swaps HOME to the active
# tier's account home for the Claude CLI's OAuth, and a bare `HOME / 'agents'`
# under that swap points at the wrong tree entirely (the medic was blind to
# exactly this until #1045).
_DEFAULT_AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT') or HOME / 'agents'
)

# How long a `.claimed/<slot>/` record counts as a LIVE review. Sized off
# `agent_runner.REVIEW_SESSION_CEILING_SECONDS` (35 min) plus headroom: past
# that ceiling the session is gone and `heal_orphaned_mirror_claims` owns the
# record, so treating it as live would only make us cancel a ghost.
CLAIM_LIVE_WINDOW_MINUTES = 45

# Bound on waiting for `agent_runner` to acknowledge the cancel by deleting the
# marker. Must comfortably exceed `agent_runner.CANCEL_POLL_INTERVAL` (5s) —
# three polls of margin — because a premature give-up clears a marker the
# session has not read yet, and the review survives.
CANCEL_ACK_TIMEOUT_SECONDS = 20.0
CANCEL_ACK_POLL_SECONDS = 0.5


def agents_root(override: Optional[Path] = None) -> Path:
    """The agents root, honoring `OURLIBERTY_AGENTS_ROOT` (repo-wide convention
    — the medic was blind to a tier HOME swap for reading a var nothing set)."""
    if override is not None:
        return Path(override)
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or _DEFAULT_AGENTS_ROOT)


def mirror_inbox_dir(override: Optional[Path] = None) -> Path:
    return agents_root(override) / 'inboxes' / 'mirror'


@dataclass(frozen=True)
class LiveReviewRecord:
    """One on-disk review record that represents a review not yet concluded.

    `kind` is 'queued' (inbox root — nothing spawned) or 'claimed'
    (`.claimed/<slot>/` — a paid session is executing). `age_minutes` is None
    for a queued record: a queued task has no execution ceiling (the watcher
    claims it whenever a slot frees), so its age says nothing about whether it
    is still going to run.
    """

    path: Path
    kind: str
    age_minutes: Optional[float] = None

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class SupersedeOutcome:
    """What `supersede_live_review` actually managed to do.

    `attempted` is False when no live record was found — the overwhelmingly
    common case, and the one where the caller logs nothing.

    `cancel_acked` is tri-state: None when no claimed record needed cancelling,
    True when `agent_runner` consumed the marker, False when it did not and we
    cleared it defensively (see the clobber trap in the module docstring).

    `session_reason` is the `pipeline_live_state` forensic identifier for WHY
    the session read as executing ('live_worktree_proc' / 'in_flight_marker'),
    or '' when it did not.
    """

    attempted: bool = False
    archived: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    cancel_acked: Optional[bool] = None
    session_reason: str = ''
    failures: list[str] = field(default_factory=list)

    @property
    def superseded(self) -> bool:
        return bool(self.archived)

    def log_line(self, task_id: str) -> str:
        """A single content-free forensic line (standing rule 1: names and
        counts only — never a record body, findings, or PR text)."""
        ack = (
            'n/a' if self.cancel_acked is None
            else ('yes' if self.cancel_acked else 'NO')
        )
        return (
            f'MIRROR_REVIEW_SUPERSEDED task={task_id} '
            f'records={len(self.archived)} '
            f'names={",".join(self.archived) or "-"} '
            f'session={self.session_reason or "-"} '
            f'cancel_requested={str(self.cancel_requested).lower()} '
            f'cancel_acked={ack} '
            f'failures={len(self.failures)}'
        )


def _record_name_matcher(review_stem: str):
    """The shared review-record filename grammar, or None if unavailable.

    Borrowed from `mirror_review_conclusion`, NOT hand-rolled here: two scanners
    with two patterns is exactly the drift that made the `-rev<N>` rounds
    invisible to one of them in #865. That module is the LIGHT copy of the
    grammar (`outbox_notifier` owns the original; a cross-module sync test in
    `test_heal_orphaned_mirror_claims` pins the two identical), and borrowing
    the light one matters structurally: `daemon_restart_manifest`'s watch-path
    scan walks lazy imports too, so reaching for the notifier here would add its
    entire transitive graph to the restart triggers of every unit that touches
    this module — and each extra restart trigger is another chance to SIGTERM a
    live review (the #971 shape).

    Fail-open: if the grammar cannot be imported we return None and the caller
    finds no records — degrading to today's unguarded dispatch rather than
    guessing at a pattern that could match another task's files.
    """
    try:
        import mirror_review_conclusion
        return mirror_review_conclusion.review_record_name_re(review_stem)
    except Exception:  # noqa: BLE001 — never break a dispatch on an import
        return None


def find_live_review_records(
    review_stem: str,
    mirror_inbox: Path,
    now: Optional[datetime] = None,
    window_minutes: int = CLAIM_LIVE_WINDOW_MINUTES,
) -> list[LiveReviewRecord]:
    """Every review record for `review_stem` that represents an UNCONCLUDED
    review — queued in the inbox root, or claimed and still within the session
    ceiling window.

    `review_stem` is the CANONICAL on-disk stem the writer produced
    (`safe_write_inbox.canonical_inbox_name('review-<task>.json')` minus
    `.json`), so a task_id that `sanitize_component` rewrites is matched under
    its real name rather than its raw form.

    Deliberately does NOT scan `.archive/` or `.invalid/`: a record there is a
    review that already CONCLUDED. Its verdict is delivered (or dead-lettered)
    and there is nothing left to cancel — cancelling on that signal would fire
    on every task that has ever been reviewed.

    THE `.claimed/` AGE MUST COME FROM THE CLAIM, NOT THE MTIME. The claim is an
    `os.rename`, which PRESERVES mtime (still the DISPATCH time) and bumps
    ctime, so `max(st_mtime, st_ctime)` is the claim. Reading bare mtime here
    would make a review that queued longer than the window read as expired at
    exactly the moment a slot picked it up — unprotected while running, which is
    the state we most need to catch. (`max` rather than bare ctime so a platform
    that does not bump ctime on rename degrades to the old reading instead of
    looking brand-new forever.) This was a real review finding on PR #1043; see
    `heal_undispatched_pr_review.recent_review_record` for the same treatment.

    Pure read. Fail-open per directory — an unreadable dir or entry is skipped.
    """
    now = now or datetime.now(timezone.utc)
    name_re = _record_name_matcher(review_stem)
    if name_re is None:
        return []
    glob_pat = f'{_glob.escape(review_stem)}*.json'
    found: list[LiveReviewRecord] = []

    # Leg 1 — the LIVE inbox root. Age-independent: a queued task has no
    # execution ceiling, so it is going to run whenever a slot frees.
    try:
        entries = list(mirror_inbox.glob(glob_pat))
    except OSError:
        entries = []
    for p in entries:
        if name_re.fullmatch(p.name):
            found.append(LiveReviewRecord(path=p, kind='queued'))

    # Leg 2 — `.claimed/<slot>/`: a session is executing. Slot dirs are globbed
    # (not the record name) so a metacharacter-bearing task_id cannot widen the
    # scan beyond its own records.
    try:
        slots = [s for s in (mirror_inbox / '.claimed').glob('*') if s.is_dir()]
    except OSError:
        slots = []
    for slot_dir in slots:
        try:
            claimed = list(slot_dir.glob(glob_pat))
        except OSError:
            continue
        for p in claimed:
            if not name_re.fullmatch(p.name):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            ts = max(st.st_mtime, st.st_ctime)  # CLAIM time — see docstring
            age_min = (
                now - datetime.fromtimestamp(ts, tz=timezone.utc)
            ).total_seconds() / 60.0
            if age_min > window_minutes:
                continue  # past the ceiling; heal_orphaned_mirror_claims owns it
            found.append(
                LiveReviewRecord(path=p, kind='claimed', age_minutes=age_min)
            )
    return found


def _session_live(task_id: str) -> tuple[bool, str]:
    """Is a Mirror review session executing for `task_id`? `(live, reason)`.

    Delegates to `pipeline_live_state.mirror_review_session_live` — the
    canonical liveness module — rather than re-deriving the worktree name and
    the in-flight-marker read here, which is exactly the duplication that lets
    one copy of a boundary guard get fixed and the other not.

    Fail-safe to (False, ''): an unavailable probe means we skip the ack WAIT,
    never the archive and never the dispatch.
    """
    try:
        import pipeline_live_state
        return pipeline_live_state.mirror_review_session_live(task_id)
    except Exception:  # noqa: BLE001
        return False, ''


def _archive_superseded(record: LiveReviewRecord, mirror_inbox: Path,
                        now: datetime) -> Optional[str]:
    """Rename `record` into `.archive/` under a distinctive superseded suffix.

    Returns the archived name, or None on failure. The suffix is deliberately
    NOT one of the pipeline's own (`.orphan-cleared-`, plain `.archive`) so a
    forensic grep can tell "we cancelled this" from "it concluded".

    Reversible by construction: a rename preserves the envelope verbatim.
    """
    dest_dir = mirror_inbox / '.archive'
    stamp = now.strftime('%Y%m%dT%H%M%SZ')
    dest = dest_dir / f'{record.path.stem}.superseded-{stamp}.json'
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        # A same-second second record would collide; disambiguate rather than
        # clobber (the archive is the only surviving copy of the envelope).
        n = 1
        while dest.exists():
            dest = dest_dir / (
                f'{record.path.stem}.superseded-{stamp}.{n}.json'
            )
            n += 1
        record.path.rename(dest)
        return dest.name
    except OSError:
        # Includes the benign race where the pipeline archived it first —
        # either way the record is no longer claimable, which is the goal.
        return None


def await_cancel_ack(
    root: Path,
    task_stem: str,
    timeout_seconds: float = CANCEL_ACK_TIMEOUT_SECONDS,
    poll_seconds: float = CANCEL_ACK_POLL_SECONDS,
    sleep=time.sleep,
    now=time.monotonic,
) -> bool:
    """Wait for `agent_runner` to consume the cancel marker; True if it did.

    `agent_runner.run_claude` deletes the marker (`_clear_cancel`) the moment it
    acts on it, so absence IS the acknowledgement — there is no other channel.

    On timeout the marker is cleared HERE and False is returned. That is not
    optional politeness: the marker is keyed on the task stem, the Forge
    revision about to be dispatched carries the same stem, and a stale marker
    would cancel it on spawn (see the clobber trap in the module docstring).
    Monotonic clock so a wall-clock jump cannot extend or truncate the bound.
    """
    marker = task_cancel.cancel_marker_path(root, task_stem)
    deadline = now() + max(0.0, timeout_seconds)
    while True:
        try:
            if not marker.exists():
                return True
        except OSError:
            return True  # unreadable ⇒ not something that can cancel anything
        if now() >= deadline:
            break
        sleep(poll_seconds)
    task_cancel.clear_cancel(root, task_stem)
    return False


def supersede_live_review(
    task_id: str,
    review_stem: str,
    *,
    reason: str,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
    window_minutes: int = CLAIM_LIVE_WINDOW_MINUTES,
    cancel_timeout_seconds: float = CANCEL_ACK_TIMEOUT_SECONDS,
    sleep=time.sleep,
) -> SupersedeOutcome:
    """Cancel any live Mirror review of `task_id` so a Forge revision can be
    dispatched without a verdict landing against a head Forge is about to move.

    Returns a `SupersedeOutcome`. NEVER raises — the caller dispatches whatever
    this reports, because withholding the dispatch is the strand this module
    exists to avoid.
    """
    outcome = SupersedeOutcome()
    try:
        now = now or datetime.now(timezone.utc)
        resolved_root = agents_root(root)
        inbox = mirror_inbox_dir(root)
        records = find_live_review_records(
            review_stem, inbox, now=now, window_minutes=window_minutes,
        )
        if not records:
            return outcome
        outcome.attempted = True

        # Archive EVERY record before requesting the cancel. The claim must stop
        # being claimable first, or the slot loop's `finally` (transient-defer
        # contract) and `heal_orphaned_mirror_claims` (re-inject a
        # not-concluded claim) each put the cancelled review straight back.
        any_claimed = False
        for rec in records:
            if rec.kind == 'claimed':
                any_claimed = True
            archived = _archive_superseded(rec, inbox, now)
            if archived is None:
                outcome.failures.append(f'archive-failed:{rec.name}')
            else:
                outcome.archived.append(archived)

        # Only a CLAIMED record means a session was spawned for this task; a
        # queued record has spawned nothing, and a marker written for it would
        # sit on disk with no reader — then cancel the Forge task, which shares
        # the stem.
        if any_claimed and task_cancel.is_safe_task_stem(task_id):
            live, outcome.session_reason = _session_live(task_id)
            outcome.cancel_requested = task_cancel.request_cancel(
                resolved_root, task_id,
                reason=reason, actor='outbox-notifier',
            )
            if not outcome.cancel_requested:
                outcome.failures.append('cancel-marker-write-failed')
            elif live:
                outcome.cancel_acked = await_cancel_ack(
                    resolved_root, task_id,
                    timeout_seconds=cancel_timeout_seconds,
                    sleep=sleep,
                )
            else:
                # Nothing is running that could ever read the marker, so waiting
                # the full ack window would stall the caller for nothing. This
                # is the common benign shape: `inbox_watcher.process_task`
                # writes the verdict outbox BEFORE it archives the claim, so the
                # notifier can legitimately be processing a verdict while that
                # round's claim is still under `.claimed/` for another
                # microsecond. The session is already gone; the archive above is
                # the whole job.
                #
                # KNOWN RESIDUAL, same branch: the claim→spawn window (a slot
                # has claimed the task and is minutes into worktree setup) also
                # reads as not-live, and there is no snapshot that separates the
                # two directions of travel. That review will run to completion
                # against a head Forge moves — pre-fix behavior, and preferable
                # to leaving a live marker behind, which would cancel the Forge
                # revision instead and strand the PR.
                outcome.cancel_acked = False
                task_cancel.clear_cancel(resolved_root, task_id)
        elif any_claimed:
            outcome.failures.append('cancel-refused-unsafe-task-stem')
    except Exception as e:  # noqa: BLE001 — a supersede error must never block
        outcome.failures.append(f'{type(e).__name__}')
    return outcome
