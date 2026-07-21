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
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Import the validator for read + schema-check.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import build_sequence_validator as bsv  # noqa: E402
import task_cancel  # noqa: E402  (shared cancel-marker contract w/ agent_runner)
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


# -------------------- post_merge executor (Contract B + C) --------------------

# projects-v3 P4 Contract B (p4-postmerge-exec): a finishing sequence may carry
# a declarative `post_merge` block of finish-steps. The executor runs them once
# the sequence completes, classifying each:
#   - `verify` (read-only go-live probes)              → ALWAYS auto-run.
#   - `run` marked `safe: true` (idempotent/fail-safe) → auto-run.
#   - `run` plain-string + every `restart`             → GATED (proposed via
#     human-approval-gate for a one-tap; NEVER auto-executed here).
# The executor NEVER raises and NEVER mutates the sequence record — a failing
# verify/cleanup reports loudly (Contract C) but can't block or corrupt the
# build. Stdlib only: the heavy propose→approval-gate wiring is injected by the
# caller as a `propose_gated` callback so this module stays import-light.
POST_MERGE_STEP_TIMEOUT_SEC = 120


@dataclass
class PostMergeStepResult:
    """Outcome of one post_merge finish-step.

    Attributes:
        kind: 'verify' | 'run' | 'restart'.
        command: the raw command (or service name, for restart).
        classification: 'auto' (executed here) or 'gated' (proposed for tap).
        executed: True iff the executor actually ran the command.
        ok: True/False for auto steps; None for gated (not run here).
        detail: stdout/stderr tail for auto steps; the proposal note for gated.
    """

    kind: str
    command: str
    classification: str
    executed: bool
    ok: Optional[bool]
    detail: str


@dataclass
class PostMergeReport:
    """Aggregate result of running a sequence's post_merge block."""

    seq_id: str
    results: list[PostMergeStepResult] = field(default_factory=list)

    @property
    def auto_results(self) -> list[PostMergeStepResult]:
        return [r for r in self.results if r.classification == 'auto']

    @property
    def gated_results(self) -> list[PostMergeStepResult]:
        return [r for r in self.results if r.classification == 'gated']

    @property
    def verify_failures(self) -> list[PostMergeStepResult]:
        """Auto verify probes that returned non-zero — Contract C reports these
        loudly (a failed go-live check is a blocked-on-you doorbell)."""
        return [
            r for r in self.results
            if r.kind == 'verify' and r.executed and r.ok is False
        ]

    @property
    def has_steps(self) -> bool:
        return bool(self.results)


def _iter_post_merge_steps(block: dict[str, Any]):
    """Yield (kind, command, is_safe) for each well-formed entry in a
    post_merge block, in report order: verify → run → restart (go-live checks
    first so they surface even when a later gated step is still pending).
    Malformed entries are silently skipped — the validator is the gate for
    shape; the executor is defensive against a hand-edited file."""
    if not isinstance(block, dict):
        return
    for cmd in block.get('verify') or []:
        if isinstance(cmd, str) and cmd.strip():
            yield 'verify', cmd, True  # read-only → always auto
    for entry in block.get('run') or []:
        if isinstance(entry, str) and entry.strip():
            yield 'run', entry, False  # plain string → gated by default
        elif isinstance(entry, dict):
            cmd = entry.get('cmd')
            if isinstance(cmd, str) and cmd.strip():
                yield 'run', cmd, bool(entry.get('safe'))
    for svc in block.get('restart') or []:
        if isinstance(svc, str) and svc.strip():
            yield 'restart', svc, False  # restart → always gated


def _classify_post_merge_step(kind: str, is_safe: bool) -> str:
    """'auto' for read-only verify + fail-safe (safe:true) run steps; 'gated'
    for everything else (plain run strings and every restart)."""
    if kind == 'verify':
        return 'auto'
    if kind == 'run' and is_safe:
        return 'auto'
    return 'gated'


def _run_post_merge_command(
    command: str, timeout: int = POST_MERGE_STEP_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """Run one auto-classified command with shell=False + a bounded timeout.

    Returns (ok, detail). NEVER raises — an unparseable command, a timeout, a
    non-zero exit, or an OS error all become (False, <detail>). shell=False +
    shlex.split avoids shell-injection: the command is argv, not a shell line.
    """
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return False, f'unparseable command: {e}'
    if not argv:
        return False, 'empty command'
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f'timed out after {timeout}s'
    except OSError as e:
        return False, f'{type(e).__name__}: {e}'
    out = ((proc.stdout or '') + (proc.stderr or '')).strip()
    tail = out[-400:]
    if proc.returncode == 0:
        return True, tail or 'ok (no output)'
    return False, f'exit {proc.returncode}: {tail}'


def execute_post_merge(
    seq: dict[str, Any],
    *,
    propose_gated: Optional[Callable[[str, str, str], Any]] = None,
    runner: Optional[Callable[[str], tuple[bool, str]]] = None,
) -> PostMergeReport:
    """Run a sequence's optional `post_merge` block (Contract B) and return a
    PostMergeReport for the completion DM (Contract C).

    Args:
        seq: the (already-complete) sequence dict.
        propose_gated: callback `(seq_id, kind, command) -> note` invoked once
            per gated step to register it on the human-approval-gate. Its return
            value (e.g. a task_id) is captured into the result detail. If None,
            gated steps are still listed in the report as awaiting a tap, just
            not registered (keeps this module stdlib-only + unit-testable).
        runner: injectable command runner for tests; defaults to
            `_run_post_merge_command`.

    NEVER raises and NEVER mutates `seq` — a failure becomes a result with
    ok=False so the build is never blocked or corrupted (spec § 5 guardrail).
    """
    seq_id = seq.get('seq_id') or '?'
    report = PostMergeReport(seq_id=seq_id, results=[])
    block = seq.get('post_merge')
    if not isinstance(block, dict):
        return report
    run = runner or _run_post_merge_command
    for kind, command, is_safe in _iter_post_merge_steps(block):
        if _classify_post_merge_step(kind, is_safe) == 'auto':
            try:
                ok, detail = run(command)
            except Exception as e:  # noqa: BLE001 — a runner must never wedge
                ok, detail = False, f'runner raised: {type(e).__name__}: {e}'
            report.results.append(PostMergeStepResult(
                kind=kind, command=command, classification='auto',
                executed=True, ok=ok, detail=detail,
            ))
            continue
        detail = 'proposed for one-tap approval'
        if propose_gated is not None:
            try:
                note = propose_gated(seq_id, kind, command)
                if note:
                    detail = str(note)
            except Exception as e:  # noqa: BLE001 — propose must never block
                detail = f'propose failed: {type(e).__name__}: {e}'
        report.results.append(PostMergeStepResult(
            kind=kind, command=command, classification='gated',
            executed=False, ok=None, detail=detail,
        ))
    return report


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

    # STOP THE WORK, not just the bookkeeping. Flipping the sequence to failed
    # blocks auto-merge of anything it produces (#606), but on its own it leaves
    # the dispatched Claude session running to its 4h ceiling — still burning
    # tokens and still free to push to the branch. `agent_runner` polls for a
    # cancel marker every 5s and SIGTERMs the worker; until now nothing in the
    # tree ever wrote one. Request it for every step currently in flight.
    #
    # AFTER the sequence write, deliberately: the status flip is the durable
    # guarantee, so it lands first and a marker failure can never cost us it.
    # Best-effort by design — `request_cancel` fails quiet, and a step that has
    # already finished simply leaves a marker nobody reads (agent_runner clears
    # it on the next dispatch of that stem).
    cancelled_steps = _request_step_cancels(seq, actor=actor, reason=reason)

    detail = ''
    if cancelled_steps:
        detail = f'; stop requested for {len(cancelled_steps)} in-flight step(s)'
    return Result(
        applied=True,
        reason=f'Sequence `{seq_id}` cancelled (was `{current}`){detail}',
        sequence_path=path,
    )


def _request_step_cancels(
    seq: dict[str, Any], *, actor: str, reason: Optional[str],
) -> list[str]:
    """Write a cancel marker for each of the sequence's in-flight steps.
    Returns the step ids a marker landed for.

    `current_steps` is the sequence's own record of what is running right now —
    the same list `apply_skip` / `apply_retry` / the merge signal filter on — so
    it is the authoritative in-flight set rather than something re-derived from
    step statuses. A step id that is unsafe as a filename (path separators) is
    refused by `task_cancel` and simply absent from the return.

    Never raises: cancel's contract is that the sequence status flip has already
    landed, and no marker problem may turn that into an error.
    """
    steps = seq.get('current_steps')
    if not isinstance(steps, list):
        return []
    out: list[str] = []
    for step_id in steps:
        try:
            if task_cancel.request_cancel(
                AGENTS_ROOT, step_id, reason=reason, actor=actor,
            ):
                out.append(step_id)
        except Exception:  # noqa: BLE001 — bookkeeping already durable
            continue
    return out


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


# -------------------- step-failed (push-signal non-merge terminals) --------------------


def apply_step_failed(
    seq_id: str,
    step_id: str,
    failure_reason: str,
    actor: str = 'notifier',
) -> Result:
    """Mark a step `failed` from a terminal NON-merge outcome and pause the
    sequence.

    Push-signal sibling to `apply_step_merged`. Where the merge path flips a
    step `dispatched → merged` and rolls the sequence up to `complete`, this
    flips a step `→ failed` and pauses the sequence so no dependent step is
    dispatched onto a broken predecessor. The notifier calls it the moment it
    classifies a terminal failure — a Forge preflight REJECT, a marker-error /
    dead-letter exhaustion, or a build crash — instead of leaving the step in
    `dispatched` for the advancer to notice on a later poll.

    Why push it from the notifier: a non-merge terminal otherwise leaves the
    step `dispatched`, where the 4h stall backstop
    (`_escalate_stranded_dispatched_steps`) eventually MISATTRIBUTES it as
    "Forge may never have picked it up" (the slice-2b incident). Flipping the
    step terminal here removes it from `dispatched` at once, so the stall
    backstop never misfires and Larry sees the real reason now.

    Mirrors the advancer's own failed-transition semantics
    (`build_sequence_advancer._process_active_sequence`): status `failed`,
    record `failure_reason`, clear `current_actor`, KEEP the step in
    `current_steps` for operator visibility, and flip a live sequence
    (`active`/`pending`) to `paused` with a `sequence-paused` audit entry.

    Idempotent:
      - step already `failed` → `applied=False` no-op.
      - step already `merged` → `applied=False` no-op. A real merge is
        terminal; a late or duplicate failure signal must NEVER clobber a
        success (the success/failure race resolves in favor of success).
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
    if current == 'failed':
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` in sequence `{seq_id}` is already '
                f'`failed`; no-op'
            ),
            sequence_path=path,
        )
    if current == 'merged':
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` in sequence `{seq_id}` is already '
                f'`merged`; failure signal is a no-op (a real merge is '
                f'terminal and is never clobbered)'
            ),
            sequence_path=path,
        )

    now = _now_iso()
    step['status'] = 'failed'
    step['failure_reason'] = failure_reason
    step['current_actor'] = None
    # Unlike apply_step_merged, the failed step STAYS in current_steps so the
    # paused sequence shows operator-visibly which step broke (mirrors the
    # advancer keeping it for visibility).
    _append_audit(seq, {
        'ts': now,
        'event': 'step-failed',
        'step_id': step_id,
        'reason': failure_reason,
        'actor': actor,
    })
    paused = seq.get('status') in ('active', 'pending')
    if paused:
        seq['status'] = 'paused'
        _append_audit(seq, {
            'ts': now,
            'event': 'sequence-paused',
            'reason': f'Step `{step_id}` failed: {failure_reason}',
            'actor': actor,
        })
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=(
            f'Step `{step_id}` in sequence `{seq_id}` transitioned '
            f'`{current}` → `failed` ({failure_reason})'
            + ('; sequence paused' if paused else '')
        ),
        sequence_path=path,
    )


# -------------------- step-pr-opened (pr_url + substatus at PR-open) --------------------


def apply_step_pr_opened(
    seq_id: str,
    step_id: str,
    pr_url: str,
    actor: str = 'notifier',
) -> Result:
    """Record a step's `pr_url` and flip its substatus to `reviewing` when its
    PR opens — not only at merge.

    Push-signal companion to `apply_step_merged`. Forge's build phase opens one
    PR and the notifier dispatches Mirror's review; this records that PR on the
    step at OPEN time, which restores two things the merge-only recording lost:

      1. The advancer's dual-gate `gh` leg (`gh_pr_says_merged(step.pr_url)`)
         is computed only `if pr_url` — with `pr_url` unset until merge it was
         dark during the whole review window. Recording it here lights that leg
         up so the belt-and-suspenders gate works during review.
      2. The 4h stall backstop distinguishes a step with an OPEN PR in review
         (`pr_url` set → "in review, don't strand") from one that never opened
         a PR (`pr_url` unset → genuine stall). Without an open-time `pr_url`
         both looked identical.

    Substatus `dispatched → reviewing` per orchestrator §5.1; `current_actor`
    becomes `mirror` (the review is now hers). The sequence status is
    untouched — an in-flight PR-open is not a sequence-level transition.

    Idempotent / monotonic — never walks a terminal step backward:
      - step already `merged` or `failed` → `applied=False` no-op (terminal;
        a re-processed build outbox must not reopen review on a done step).
      - step already `reviewing` with the SAME `pr_url` → `applied=False`
        no-op (clean re-fire from a build-outbox re-process).
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
    if current in ('merged', 'failed'):
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` in sequence `{seq_id}` is already terminal '
                f'`{current}`; pr-open is a no-op'
            ),
            sequence_path=path,
        )
    if current == 'reviewing' and step.get('pr_url') == pr_url:
        return Result(
            applied=False,
            reason=(
                f'Step `{step_id}` in sequence `{seq_id}` is already '
                f'`reviewing` with pr={pr_url}; no-op'
            ),
            sequence_path=path,
        )

    now = _now_iso()
    step['status'] = 'reviewing'
    step['pr_url'] = pr_url
    step['current_actor'] = 'mirror'
    _append_audit(seq, {
        'ts': now,
        'event': 'step-pr-opened',
        'step_id': step_id,
        'pr_url': pr_url,
        'actor': actor,
    })
    write_err = _atomic_write(path, seq)
    if write_err is not None:
        return write_err
    return Result(
        applied=True,
        reason=(
            f'Step `{step_id}` in sequence `{seq_id}` transitioned '
            f'`{current}` → `reviewing` (pr={pr_url})'
        ),
        sequence_path=path,
    )


# -------------------- no-delta / already-merged build outcome --------------------
#
# Forge's build phase opens ONE PR for the work it builds. When a build session
# RESUMES onto a slice whose work has ALREADY merged via another path — a
# concurrent Forge session, a manual desktop merge — `git diff main..HEAD` is
# empty: there is no delta to commit, so `gh pr create` has nothing to open.
# Forge correctly REFUSES to fabricate a `PR opened:` line and instead narrates
# the PR that already shipped. That outbox carries no PR URL and (by protocol)
# no marker, so the sequence step it belongs to would strand in `dispatched`
# until the 4h stall backstop fails + pauses it + pages Larry to reconcile by
# hand. The real incident: seq `launch-system-self-awareness-slice-1-state-log`,
# step `system-self-awareness-slice-1-state-log`, work already merged via
# https://github.com/Larry-Yatch/ourliberty-agent-core/pull/602 — whose branch
# (`forge/system-self-awareness-the-standing-brain`) and title carry none of the
# step_id token, so neither the notifier nor the advancer's branch/title match
# could bridge the step to the PR. The one DURABLE bridge is the build outbox's
# own result text, which names the already-merged PR.
#
# These two helpers let BOTH seams (the notifier live, the advancer as backstop)
# turn that honest no-delta outcome into a terminal, auto-reconcilable state.
# Detection is deliberately conservative — two text gates PLUS a gh-truth gate:
#   1. an already-merged / no-delta CUE must be present (Forge's vocabulary for
#      this outcome — she leads with it), AND
#   2. the result must name EXACTLY ONE distinct PR number (ambiguous multiples
#      refuse to guess — mirrors outbox_notifier._extract_pr_url_from_build_result),
#   3. the caller then gh-VERIFIES that PR is genuinely MERGED before flipping
#      the step. Prose alone never mutates sequence state.

# STRUCTURED-FIELD TIER — the canonical no-delta CONTRACT line. When a build
# RESUMES onto a slice whose work already merged via another path, Forge leads
# her result with this exact form (agents/forge/CLAUDE.md Build phase protocol),
# mirroring the `PR opened: <url>` contract for the normal case:
#
#     NO PR — already merged: #<N>
#
# This is the DURABLE entry signal the prose cue below can't be: a tight,
# line-anchored field that names THE already-merged PR explicitly, so detection
# does NOT silently degrade when Forge's surrounding narration is reworded. The
# regex matches the contract PHRASE + the rest of its line (em-dash / colon /
# hyphen as the lead separator are all tolerated, a minor transcription drift);
# the PR number is then pulled from that line by `_iter_pr_numbers`, so BOTH
# documented forms — `#<N>` and a full `https://.../pull/<N>` URL — are honored
# identically. MULTILINE `^` so the line is recognized wherever Forge places it
# (she typically follows it with a one-paragraph explanation). Only PR refs ON
# the canonical line are authoritative — a stray PR number in the explanation
# paragraph below can't override it.
_CANONICAL_ALREADY_MERGED_RE = re.compile(
    r'^\s*NO\s+PR\s*[—:-]\s*already\s+merged\b[^\n]*',
    re.IGNORECASE | re.MULTILINE,
)

_ALREADY_MERGED_CUE_RE = re.compile(
    r'already\s+(?:built\s+and\s+)?merged'
    r'|no\s+delta'
    r'|diff\s+[^\n]*\bis\s+empty'
    r'|byte-identical\s+to\s+`?main`?'
    # The canonical `NO PR — already merged: #N` lead line also trips the cue, so
    # a result carrying it still satisfies the prose tier even if the structured
    # parse above is ever tightened. Belt-and-suspenders, costs nothing.
    r'|\bNO\s+PR\s*[—:-]',
    re.IGNORECASE,
)

# A PR reference as `#<N>`, `PR #<N>`, or the trailing `/pull/<N>` of a full URL.
# Two alternates (not one `(?:/pull/|#)`) so a full PR URL's OWN trailing
# `#fragment` can never contribute a phantom SECOND number: the standalone `#<N>`
# form requires a boundary before the `#` that excludes word / `/` / `#` / `-`
# characters, so a numeric fragment hung off a URL (`/pull/602#603`,
# `/pull/602#issuecomment-123`) is read as part of that URL, not as an
# independent PR. Bare integers (line counts, deliverable ids like `D1`) carry no
# leading `#` or `/pull/`, so they don't match either.
_PR_NUMBER_RE = re.compile(r'/pull/(\d+)|(?<![\w/#-])#(\d+)')


def _iter_pr_numbers(text: str):
    """Yield each PR number `text` names via `/pull/<N>` or a standalone `#<N>`.

    Exactly one of the two capture groups matches per hit (the other is None);
    `\\d+` guarantees the matched group is a non-empty digit string."""
    for m in _PR_NUMBER_RE.finditer(text):
        yield int(m.group(1) or m.group(2))


def parse_already_merged_pr_ref(result_text: Optional[str]) -> Optional[int]:
    """Return the single PR number a no-delta/already-merged build result names.

    Two-tier, STRUCTURED-FIELD-FIRST:

      1. The canonical CONTRACT line `NO PR — already merged: #<N>`
         (`_CANONICAL_ALREADY_MERGED_RE`; the PR may be a `#<N>` or a full
         `/pull/<N>` URL) — authoritative. It names THE merged PR explicitly, so
         it is honored even when the surrounding prose mentions OTHER PR numbers
         (a superseded attempt, a sibling slice). This is the durable signal Forge
         is asked to emit; rewording the narration around it never degrades
         detection.

      2. Backward-compat PROSE fallback for results that pre-date the contract
         (or an un-updated session): an already-merged CUE
         (`_ALREADY_MERGED_CUE_RE`) PLUS exactly ONE distinct PR number. More than
         one distinct number is ambiguous — refuse to guess (the safe direction),
         since prose alone gives no way to tell which is the merge.

    Returns None on empty/non-str input or when neither tier resolves a number.
    The caller MUST gh-verify the returned PR is genuinely MERGED (via
    `gh_pr_merge_info`) before treating the step as terminal — this parser only
    proposes a candidate."""
    if not isinstance(result_text, str) or not result_text:
        return None
    # Tier 1 — structured canonical contract line (authoritative). Pull the PR
    # the line names (either `#<N>` or a full `/pull/<N>` URL) via the shared
    # extractor; the first ref ON the canonical line wins. If the phrase matched
    # but carries no PR ref on its line, fall through to the prose tier.
    canonical = _CANONICAL_ALREADY_MERGED_RE.search(result_text)
    if canonical is not None:
        for pr in _iter_pr_numbers(canonical.group(0)):
            return pr
    # Tier 2 — prose-cue fallback (single distinct PR only).
    if not _ALREADY_MERGED_CUE_RE.search(result_text):
        return None
    nums = set(_iter_pr_numbers(result_text))
    if len(nums) != 1:
        return None
    return next(iter(nums))


def qualify_repo(repo: str) -> str:
    """Return `repo` in the OWNER/REPO form `gh --repo` requires.

    A bare name (`ourliberty-agent-core`) is prefixed with `OURLIBERTY_GH_OWNER`
    (default `Larry-Yatch`); an already-qualified value (a '/' is present) or a
    falsy value passes through untouched. The empty passthrough mirrors
    task_terminal_state's guard so the older `_qualify_repo` copies can delegate
    here without a behavior change.

    This is the SINGLE shared resolver — outbox_notifier, build_sequence_advancer
    (`_qualify_repo`), and task_terminal_state (`_qualify_repo`) all route here so
    the owner default lives in exactly one place."""
    if not repo or '/' in repo:
        return repo
    owner = os.environ.get('OURLIBERTY_GH_OWNER', 'Larry-Yatch')
    return f'{owner}/{repo}'


def gh_pr_merge_info(
    repo_coords: str, pr_number: int, timeout: int = 10,
) -> Optional[tuple[str, str]]:
    """Return `(pr_url, merged_at_iso)` iff PR `pr_number` in `repo_coords` is
    MERGED, else None.

    `repo_coords` is the OWNER/REPO gh form (e.g. `Larry-Yatch/ourliberty-agent-
    core`). Shells `gh pr view <N> --repo <coords> --json state,url,mergedAt`.
    Returns None on any non-MERGED state, transport error, non-zero exit, or
    parse failure — the gh-truth gate for the already-merged reconcile, so a
    candidate PR from `parse_already_merged_pr_ref` only flips a step when GitHub
    itself confirms the merge. Never raises."""
    if not repo_coords or not isinstance(pr_number, int) or pr_number < 1:
        return None
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number), '--repo', repo_coords,
             '--json', 'state,url,mergedAt'],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get('state') != 'MERGED':
        return None
    url = data.get('url')
    merged_at = data.get('mergedAt')
    if not isinstance(url, str) or not url:
        return None
    if not isinstance(merged_at, str) or not merged_at:
        return None
    return url, merged_at


__all__ = [
    'Result',
    'apply_pause',
    'apply_resume',
    'apply_cancel',
    'apply_retry',
    'apply_skip',
    'apply_step_merged',
    'parse_already_merged_pr_ref',
    'qualify_repo',
    'gh_pr_merge_info',
    'is_completion_signaled',
    'claim_completion_signal',
    'SEQUENCE_COMPLETE_SIGNALED_EVENT',
    'execute_post_merge',
    'PostMergeReport',
    'PostMergeStepResult',
    'POST_MERGE_STEP_TIMEOUT_SEC',
    'AGENTS_ROOT',
]
