#!/usr/bin/env python3
"""sequence_shortcut_helpers.py — executable enforcement for the 5
non-kickoff sequence shortcuts (PR-S4 rectification M1).

PR-S4 shipped `approve sequence <seq-id>` with a real Python handler in
the outbox-notifier (`_handle_build_sequence_advancer_kickoff`). The
other 5 shortcuts (`pause` / `resume` / `cancel` / `retry` / `skip`)
were CLAUDE.md prose only — Beacon-as-Claude had to parse the chat
verb, read the sequence file, apply the mutation, and atomic-write
back. No central library meant no idempotency gate, no schema check,
no audit_log shape enforcement. The "shortcut idempotency" Mirror
review focus was empirically satisfied only on kickoff.

This module fills that gap with pure-Python helpers that:

  1. Read `~/agents/blackboard/build-sequences/<seq-id>.json` via the
     shared `build_sequence_validator` read helper.
  2. Validate current state allows the requested mutation (idempotent
     no-op if the sequence/step is already in the target state).
  3. Mutate the in-memory dict — set new status, append the audit_log
     entry with actor + ts + (optional) reason.
  4. Atomic-write back (tmp + os.replace) so a crash mid-write cannot
     corrupt the on-disk file.
  5. Return `Result(applied: bool, reason: str, sequence_path: Path)`.
     `applied=False` means WARN no-op (idempotent re-apply); the caller
     surfaces that to Larry as "already <state>; no-op".

Beacon's CLAUDE.md is updated to invoke these helpers via
`python3 -c "from sequence_shortcut_helpers import apply_pause; ..."`
rather than hand-edit JSON. Same posture as the kickoff handler —
discipline becomes executable, idempotent, and testable.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Import the validator for read + schema-check.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import build_sequence_validator as bsv  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402


# AGENTS_ROOT is a module-level constant so tests can monkeypatch it the
# same way they monkeypatch outbox_notifier.AGENTS_ROOT. The default
# matches build_sequence_validator's resolution rule (env var override
# falling back to ~/agents).
AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)


@dataclass
class Result:
    """Outcome of a shortcut mutation.

    Attributes:
        applied: True if the on-disk file changed; False on WARN no-op
            (idempotent re-apply) and on hard failure (missing file,
            malformed JSON, validator failure, write failure). Hard
            failures surface in `reason` — caller distinguishes via
            `error` truthiness.
        reason: human-readable summary of the outcome. On `applied=True`,
            describes the transition. On `applied=False`, describes why
            (already in target state, sequence not found, etc.).
        sequence_path: the absolute path of the sequence file (whether
            written or not — caller surfaces it to Larry on error).
        error: True only on hard failure (read/parse/validate/write).
            False on success AND on idempotent no-op.
    """

    applied: bool
    reason: str
    sequence_path: Path
    error: bool = False


def _seq_path(seq_id: str) -> Path:
    return AGENTS_ROOT / 'blackboard' / 'build-sequences' / f'{seq_id}.json'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_sequence(seq_id: str) -> tuple[Optional[dict[str, Any]], Optional[Result]]:
    """Read and JSON-parse the sequence file. Returns (seq, err_result).

    On success: (seq_dict, None). On any read/parse failure: (None,
    Result(error=True, applied=False, ...)). Caller propagates the
    error Result.
    """
    path = _seq_path(seq_id)
    if not path.is_file():
        return None, Result(
            applied=False,
            reason=f'Sequence `{seq_id}` not found at {path}',
            sequence_path=path,
            error=True,
        )
    try:
        return json.loads(path.read_text()), None
    except json.JSONDecodeError as e:
        return None, Result(
            applied=False,
            reason=f'Sequence `{seq_id}` is not valid JSON: {e}',
            sequence_path=path,
            error=True,
        )
    except OSError as e:
        return None, Result(
            applied=False,
            reason=f'Sequence `{seq_id}` read failed: {e}',
            sequence_path=path,
            error=True,
        )


def _atomic_write(path: Path, seq: dict[str, Any]) -> Optional[Result]:
    """Atomic write of seq to path via a UNIQUE tmp + os.replace.

    On failure (OSError), returns an error Result; on success returns None.

    Audit #62: the previous implementation used a *fixed* ``<path>.tmp`` name,
    so two concurrent shortcut writers (or a shortcut racing another process
    using the same convention, e.g. outbox_notifier) could truncate each
    other's half-written tmp before ``os.replace`` — corrupting the sequence
    file or surfacing a spurious FileNotFoundError. The shared
    ``atomic_write_json`` helper writes to a unique mkstemp tmp + fsync, so
    concurrent writers never share a tmp. Trailing newline is preserved.
    """
    try:
        atomic_write_json(path, seq, indent=2, trailing_newline=True)
    except OSError as e:
        return Result(
            applied=False,
            reason=f'Sequence file write failed at {path}: {e}',
            sequence_path=path,
            error=True,
        )
    return None


def _append_audit(seq: dict[str, Any], entry: dict[str, Any]) -> None:
    """Defensive audit_log append — same shape as the kickoff handler."""
    if not isinstance(seq.get('audit_log'), list):
        seq['audit_log'] = []
    seq['audit_log'].append(entry)


def _find_step(seq: dict[str, Any], step_id: str) -> Optional[dict[str, Any]]:
    for step in seq.get('steps') or []:
        if step.get('step_id') == step_id:
            return step
    return None


def _check_sequence_completion(seq: dict[str, Any], actor: str) -> bool:
    """If every step in `seq` is `merged` AND the sequence is currently in
    a live state (`active` or `pending`), mutate `seq` in place to
    finalize: status → `complete`, current_steps → [], append a
    `sequence-complete` audit_log entry attributed to `actor`. Returns
    True iff a finalization mutation was applied.

    Why: the zombie-active gap surfaced on operator-ux-rollout
    (2026-05-29) — apply_skip mutated the final pending step to merged
    but never ran the rollup, leaving status=active and current_steps
    naming the just-skipped step. Centralizing the rollup here lets
    apply_step_merged AND apply_skip share one finalization path; the
    apply_step_merged idempotency branch can also call it so a
    skipped-then-real-merge ordering still finalizes.

    Idempotent: a sequence already in any non-live status (complete,
    failed, paused, archived) is a no-op — terminal statuses are
    operator-overriding (apply_cancel sets failed; auto-flip to complete
    would silently undo the cancel). The caller owns atomic write.
    """
    if seq.get('status') not in ('active', 'pending'):
        return False
    steps = seq.get('steps') or []
    if not steps:
        return False
    if not all(step.get('status') == 'merged' for step in steps):
        return False
    seq['status'] = 'complete'
    seq['current_steps'] = []
    _append_audit(seq, {
        'ts': _now_iso(),
        'event': 'sequence-complete',
        'actor': actor,
    })
    return True


# -------------------- completion signal (Contract A) --------------------

# projects-v3 P4 Contract A (p4-complete-signal): the audit_log event marking
# that the one-time `sequence_complete` chain event + Larry completion DM have
# been emitted for a finished sequence. This is the exactly-once guard: the DM
# fires only on the FIRST claim, so a re-tick or notifier crash-resume that
# re-detects the same merged sequence never double-DMs.
#
# Distinct from the `sequence-complete` audit event (written by
# _check_sequence_completion when status flips to `complete`). That marks the
# state transition; THIS marks that the outward-facing signal went out. The two
# are deliberately separate so the signal can be claimed idempotently even if
# the status flip happened on a prior tick.
SEQUENCE_COMPLETE_SIGNALED_EVENT = 'sequence-complete-signaled'


def is_completion_signaled(seq: dict[str, Any]) -> bool:
    """True iff `seq`'s audit_log already carries the completion-signaled
    marker — i.e. the one-time completion event + DM have already fired.

    Cheap in-memory check the notifier uses as a pre-filter before paying for
    a gh veto or a write. The authoritative claim is `claim_completion_signal`,
    which re-checks under the read-modify-write so two racing ticks can't both
    win.
    """
    for entry in seq.get('audit_log') or []:
        if (
            isinstance(entry, dict)
            and entry.get('event') == SEQUENCE_COMPLETE_SIGNALED_EVENT
        ):
            return True
    return False


def claim_completion_signal(seq_id: str, actor: str = 'notifier') -> Result:
    """Atomically claim the right to emit the one-time completion signal.

    Returns `Result(applied=True)` ONLY on the first successful claim for a
    sequence that is `complete` and has no prior signaled-marker; the marker is
    appended and the file atomically rewritten BEFORE returning, so the marker
    is durable the instant the caller learns it won. The caller then emits the
    `sequence_complete` chain event + Larry DM.

    Returns `Result(applied=False, error=False)` — a benign no-op — when the
    sequence is not yet `complete`, or when the marker is already present (a
    re-tick / crash-resume that re-detected the same merge). Returns
    `Result(error=True)` only on hard read/write failure.

    Ordering rationale (spec § 5 governing constraint — exactly-once DM): the
    marker is written before the DM. If the process crashes after the write but
    before the DM, the sequence is silently un-DM'd — a single missed DM is the
    accepted failure mode, strictly preferred over any double-DM.
    """
    seq, err = _read_sequence(seq_id)
    if err is not None:
        return err
    path = _seq_path(seq_id)
    if seq.get('status') != 'complete':
        return Result(
            applied=False,
            reason=(
                f'Sequence `{seq_id}` has status `{seq.get("status")}`; '
                f'completion signal is only claimable on `complete`'
            ),
            sequence_path=path,
        )
    if is_completion_signaled(seq):
        return Result(
            applied=False,
            reason=(
                f'Sequence `{seq_id}` completion signal already claimed; '
                f'no-op (idempotent re-detect is safe — no double-DM)'
            ),
            sequence_path=path,
        )
    _append_audit(seq, {
        'ts': _now_iso(),
        'event': SEQUENCE_COMPLETE_SIGNALED_EVENT,
        'actor': actor,
    })
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=f'Sequence `{seq_id}` completion signal claimed',
        sequence_path=path,
    )


# -------------------- pause / resume --------------------


def apply_pause(seq_id: str, actor: str = 'larry') -> Result:
    """Set sequence status to `paused`.

    Idempotent: if already paused, returns applied=False with reason
    naming the current state. Hard error if file missing or status not
    in a pausable state (terminal states cancel/complete/archived are
    not legitimate pause targets — pause is for live sequences).
    """
    seq, err = _read_sequence(seq_id)
    if err is not None:
        return err
    path = _seq_path(seq_id)
    current = seq.get('status')

    if current == 'paused':
        return Result(
            applied=False,
            reason=f'Sequence `{seq_id}` is already paused; no-op',
            sequence_path=path,
        )
    # Pause only makes sense on live (pending/active) sequences. Terminal
    # statuses (complete/failed/archived) silently no-op rather than raise.
    if current not in ('pending', 'active'):
        return Result(
            applied=False,
            reason=(
                f'Sequence `{seq_id}` has status `{current}`; pause is only '
                f'meaningful on `pending` or `active` sequences'
            ),
            sequence_path=path,
        )

    seq['status'] = 'paused'
    _append_audit(seq, {
        'ts': _now_iso(),
        'event': 'paused',
        'actor': actor,
    })
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=f'Sequence `{seq_id}` paused (was `{current}`)',
        sequence_path=path,
    )


def apply_resume(seq_id: str, actor: str = 'larry') -> Result:
    """Set sequence status to `active`.

    Idempotent: if already active (or pending), returns applied=False
    with reason naming the current state. Resume from terminal statuses
    is a hard error — re-running a complete/failed/archived sequence
    requires a fresh seq-id.
    """
    seq, err = _read_sequence(seq_id)
    if err is not None:
        return err
    path = _seq_path(seq_id)
    current = seq.get('status')

    if current == 'active':
        return Result(
            applied=False,
            reason=f'Sequence `{seq_id}` is already active; no-op',
            sequence_path=path,
        )
    if current == 'pending':
        return Result(
            applied=False,
            reason=(
                f'Sequence `{seq_id}` is `pending` (not yet kicked off); '
                f'resume is a no-op — use `approve sequence {seq_id}` to '
                f'kick off instead'
            ),
            sequence_path=path,
        )
    if current != 'paused':
        return Result(
            applied=False,
            reason=(
                f'Sequence `{seq_id}` has terminal status `{current}`; '
                f'resume only valid on `paused` sequences'
            ),
            sequence_path=path,
        )

    seq['status'] = 'active'
    _append_audit(seq, {
        'ts': _now_iso(),
        'event': 'resumed',
        'actor': actor,
    })
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=f'Sequence `{seq_id}` resumed (was `paused`)',
        sequence_path=path,
    )


# -------------------- cancel --------------------


def apply_cancel(
    seq_id: str, actor: str = 'larry', reason: Optional[str] = None,
) -> Result:
    """Set sequence status to `failed` with a `cancelled` audit event.

    Per spec § 5.4 + runbooks/build-sequence-shortcuts.md, cancel uses
    `status: failed` (no new enum value). Idempotent: if already failed
    or cancelled, returns applied=False. `reason` is optional — when
    omitted, the audit_log entry omits the `reason` key entirely (no
    empty string), per runbook discipline.
    """
    seq, err = _read_sequence(seq_id)
    if err is not None:
        return err
    path = _seq_path(seq_id)
    current = seq.get('status')

    if current == 'failed':
        return Result(
            applied=False,
            reason=f'Sequence `{seq_id}` is already failed; no-op',
            sequence_path=path,
        )
    if current in ('complete', 'archived'):
        return Result(
            applied=False,
            reason=(
                f'Sequence `{seq_id}` has terminal status `{current}`; '
                f'cancel is a no-op'
            ),
            sequence_path=path,
        )

    seq['status'] = 'failed'
    entry: dict[str, Any] = {
        'ts': _now_iso(),
        'event': 'cancelled',
        'actor': actor,
    }
    if reason:
        entry['reason'] = reason
    _append_audit(seq, entry)
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=f'Sequence `{seq_id}` cancelled (was `{current}`)',
        sequence_path=path,
    )


# -------------------- retry --------------------


def apply_retry(
    seq_id: str, step_id: str, actor: str = 'larry',
) -> Result:
    """Reset a failed step to `pending` and remove from current_steps.

    The step's `dispatched_at` / `merged_at` / `pr_url` /
    `current_actor` / `failure_reason` fields are cleared. The
    sequence's `current_steps` list is filtered to exclude `step_id`.
    The audit_log gets a `step-retried` entry.

    Idempotent: step already in `pending` → applied=False, no mutation.
    Step not found → hard error.
    """
    seq, err = _read_sequence(seq_id)
    if err is not None:
        return err
    path = _seq_path(seq_id)
    step = _find_step(seq, step_id)
    if step is None:
        return Result(
            applied=False,
            reason=f'Step `{step_id}` not found in sequence `{seq_id}`',
            sequence_path=path,
            error=True,
        )
    current = step.get('status')
    if current == 'pending':
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` in sequence `{seq_id}` is already '
                f'`pending`; no-op'
            ),
            sequence_path=path,
        )
    if current == 'merged':
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` is already `merged`; retry is a no-op '
                f'(merged steps are immutable per spec § 5.3)'
            ),
            sequence_path=path,
        )

    step['status'] = 'pending'
    step['dispatched_at'] = None
    step['pr_url'] = None
    step['current_actor'] = None
    step['failure_reason'] = None
    step['merged_at'] = None
    seq['current_steps'] = [
        s for s in (seq.get('current_steps') or []) if s != step_id
    ]
    _append_audit(seq, {
        'ts': _now_iso(),
        'event': 'step-retried',
        'step_id': step_id,
        'actor': actor,
    })
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=(
            f'Step `{step_id}` in sequence `{seq_id}` reset to `pending` '
            f'(was `{current}`)'
        ),
        sequence_path=path,
    )


# -------------------- skip --------------------


def apply_skip(
    seq_id: str, step_id: str, actor: str = 'larry',
    reason: Optional[str] = None,
) -> Result:
    """Mark a step as `merged` without an actual PR (Larry's out-of-band
    completion shortcut).

    Per spec § 5.4 + preflight Q4: skip uses status `merged`, NOT a new
    `skipped` value — the audit_log event (`step-skipped`) discriminates.
    The step's `merged_at` is set to now. Idempotent: step already
    `merged` → applied=False. `reason` is optional and omitted from the
    audit entry when not provided.
    """
    seq, err = _read_sequence(seq_id)
    if err is not None:
        return err
    path = _seq_path(seq_id)
    step = _find_step(seq, step_id)
    if step is None:
        return Result(
            applied=False,
            reason=f'Step `{step_id}` not found in sequence `{seq_id}`',
            sequence_path=path,
            error=True,
        )
    current = step.get('status')
    if current == 'merged':
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` in sequence `{seq_id}` is already '
                f'`merged`; skip is a no-op'
            ),
            sequence_path=path,
        )

    now = _now_iso()
    step['status'] = 'merged'
    step['merged_at'] = now
    seq['current_steps'] = [
        s for s in (seq.get('current_steps') or []) if s != step_id
    ]
    entry: dict[str, Any] = {
        'ts': now,
        'event': 'step-skipped',
        'step_id': step_id,
        'actor': actor,
    }
    if reason:
        entry['reason'] = reason
    _append_audit(seq, entry)
    _check_sequence_completion(seq, actor)
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=(
            f'Step `{step_id}` in sequence `{seq_id}` skipped to `merged` '
            f'(was `{current}`)'
        ),
        sequence_path=path,
    )


# -------------------- step-merged (V6, orchestrator-rectification-v2) --------------------


def apply_step_merged(
    seq_id: str,
    step_id: str,
    pr_url: str,
    merged_at: str,
    actor: str = 'notifier',
) -> Result:
    """Mark a step as `merged` from a real PR merge (NOT a skip).

    Hook for the outbox-notifier's AUTO_MERGE path. Whenever AUTO_MERGE
    successfully merges a PR for a task whose `task_id` matches a step
    in an active sequence, the notifier calls this helper to flip the
    step's status `dispatched → merged`, record `merged_at` + `pr_url`,
    and remove the step from `current_steps`. Without this signal, the
    build_sequence_advancer never observes the merge — bootstrap-002 V6
    surfaced exactly that failure (step-verify-write merged at 17:36 MDT;
    sequence file still showed status=dispatched, merged_at=null 9 hours
    later until manual cancel).

    Distinct from `apply_skip` (which uses `event: step-skipped` for
    Larry's out-of-band completion shortcut). `apply_step_merged` uses
    `event: step-merged` so audit-log readers can distinguish a real PR
    merge from a manual skip-to-merged.

    Idempotent: if `step.status` is already `merged`, returns
    `Result(applied=False, reason='already merged')` so a notifier crash
    between merge and archive (which re-processes the same outbox on
    restart) doesn't double-fire. Hard error if file missing, JSON
    invalid, or step_id not found in the sequence.

    Args:
        seq_id: target sequence id (file at
            `<AGENTS_ROOT>/blackboard/build-sequences/<seq_id>.json`).
        step_id: the step whose status flips to `merged`. Per § E (spec
            decision), task_id matches step_id one-to-one across the
            sequence's dispatch path.
        pr_url: GitHub PR URL the AUTO_MERGE just merged.
        merged_at: ISO-8601 timestamp the merge completed. Caller
            supplies (typically `datetime.now(timezone.utc).isoformat()`)
            so the helper stays free of clock dependencies for testing.
        actor: who recorded the event. Defaults to `notifier` since the
            AUTO_MERGE path is the canonical caller. The audit_log event
            is still `step-merged` regardless of actor.
    """
    seq, err = _read_sequence(seq_id)
    if err is not None:
        return err
    path = _seq_path(seq_id)
    step = _find_step(seq, step_id)
    if step is None:
        return Result(
            applied=False,
            reason=f'Step `{step_id}` not found in sequence `{seq_id}`',
            sequence_path=path,
            error=True,
        )

    current = step.get('status')
    if current == 'merged':
        # Step-level no-op, but a previously-skipped step that never
        # triggered the rollup (or any pre-existing zombie-active state)
        # gets one more chance to finalize the sequence here.
        finalized = _check_sequence_completion(seq, actor)
        if finalized:
            write_err = _atomic_write(path, seq)
            if write_err is not None:
                return write_err
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` in sequence `{seq_id}` is already '
                f'`merged`; no-op (idempotent re-fire from notifier '
                f'crash-resume is safe)'
            ),
            sequence_path=path,
        )

    step['status'] = 'merged'
    step['merged_at'] = merged_at
    step['pr_url'] = pr_url
    seq['current_steps'] = [
        s for s in (seq.get('current_steps') or []) if s != step_id
    ]
    _append_audit(seq, {
        'ts': merged_at,
        'event': 'step-merged',
        'step_id': step_id,
        'pr_url': pr_url,
        'actor': actor,
    })
    _check_sequence_completion(seq, actor)
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=(
            f'Step `{step_id}` in sequence `{seq_id}` transitioned '
            f'`{current}` → `merged` (pr={pr_url})'
        ),
        sequence_path=path,
    )


__all__ = [
    'Result',
    'apply_pause',
    'apply_resume',
    'apply_cancel',
    'apply_retry',
    'apply_skip',
    'apply_step_merged',
    'is_completion_signaled',
    'claim_completion_signal',
    'SEQUENCE_COMPLETE_SIGNALED_EVENT',
    'AGENTS_ROOT',
]
