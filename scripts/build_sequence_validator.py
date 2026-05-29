#!/usr/bin/env python3
"""build_sequence_validator.py — DAG + schema validator for sequence files.

Phase E-orchestrator PR-S2. Spec: agents/beacon/specs/build-sequence-orchestrator.md
§ 5.1 (sequence file schema), § 5.2 (concurrency rule), § 5.5 discipline 2
(validator invoked at synthesis time).

Two entry points:

  - `validate_dag(sequence_dict) -> ValidationResult` — verifies a parsed
    sequence dict against the § 5.1 schema and runs DAG checks (no cycles,
    all `depends_on` references resolve to known step_ids, no step depends
    on itself).
  - `validate_no_concurrent_active(blackboard_dir=None) -> bool` — reads
    every `<seq-id>.json` in the blackboard build-sequences dir and returns
    False if any sequence is still live (status in {pending, active}). Per
    spec § 5.2 decision A: V1 allows exactly one live sequence at a time;
    Beacon calls this helper at sequence-creation time before writing a
    new file.

CLI:

    python3 scripts/build_sequence_validator.py <sequence_file.json>

Exits 0 if valid, 1 with errors written to stderr if invalid. Used by
Beacon's PLAN_SYNTHESIS_DISCIPLINE step 3 ("Run `python3
scripts/build_sequence_validator.py validate <seq-id>` to verify DAG
correctness before emitting the kickoff marker") and by Mirror's preflight
DAG review per spec § 5.5 discipline 3.

Stdlib only — keeps the helper importable from the advancer daemon without
a fresh dependency surface.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
DEFAULT_BLACKBOARD_DIR = AGENTS_ROOT / 'blackboard' / 'build-sequences'

# Per spec § 5.1: sequence-level status enum.
VALID_SEQUENCE_STATUS = frozenset({
    'pending', 'active', 'paused', 'complete', 'failed', 'archived',
})

# Per spec § 5.1: step-level status enum.
VALID_STEP_STATUS = frozenset({
    'pending', 'dispatchable', 'dispatched', 'building', 'reviewing',
    'merged', 'failed',
})

# Per spec § 5.1: step-level current_actor enum (None also allowed).
VALID_STEP_ACTOR = frozenset({
    'forge', 'mirror', 'auto_merge', 'larry',
})

# Statuses that count as "live" for the concurrency check. A sequence in
# any of these states blocks a new sequence from being created. Paused
# counts as NOT-live per the brief: Larry can resume a paused sequence,
# but until he does, a new sequence may run in parallel. Complete, failed,
# and archived are obviously not-live.
LIVE_SEQUENCE_STATUSES = frozenset({'pending', 'active'})

# Sequence-level required top-level fields. `audit_log` is required so the
# advancer never has to defensively `setdefault([])`.
REQUIRED_SEQ_FIELDS = (
    'seq_id', 'label', 'spec_doc', 'created_at', 'created_by',
    'status', 'current_steps', 'steps', 'audit_log',
)

# Step-level required fields. Schema mirrors the example in spec § 5.1
# verbatim — none of these are optional; absent fields surface as errors
# so the advancer never crashes mid-tick on a malformed file.
REQUIRED_STEP_FIELDS = (
    'step_id', 'label', 'depends_on', 'dispatch_text', 'target_repo',
    'task_type', 'status', 'dispatched_at', 'merged_at', 'pr_url',
    'current_actor', 'failure_reason',
)

# Per spec `agents/beacon/specs/operator-ux-gap-log-field.md`: optional
# sequence-level field for structured mid-run gap findings. Each entry
# requires these four string fields. Severity taxonomy is intentionally
# unenumerated (spec § 3 open question — NOW/SOON/FYI vs low/medium/high
# unresolved); the validator type-checks but doesn't constrain the value.
REQUIRED_GAP_LOG_ENTRY_FIELDS = ('ts', 'severity', 'finding', 'surfaced_by')


@dataclass
class ValidationResult:
    """Returned by validate_dag. Truthy iff `valid` is True.

    `errors` is a list of human-readable strings; `seq_id` echoes the
    input's seq_id when present (helps the CLI label failures even for
    malformed inputs)."""
    valid: bool
    errors: list[str] = field(default_factory=list)
    seq_id: Optional[str] = None

    def __bool__(self) -> bool:
        return self.valid

    def to_dict(self) -> dict[str, Any]:
        return {'valid': self.valid, 'errors': list(self.errors),
                'seq_id': self.seq_id}


# -------------------- schema checks --------------------


def _check_top_level_shape(seq: Any, errors: list[str]) -> bool:
    """Return True iff seq is a dict with all required top-level fields.

    On failure, appends one error per missing field and returns False so
    downstream checks can short-circuit safely (they assume the required
    fields exist)."""
    if not isinstance(seq, dict):
        errors.append(f'top-level value is {type(seq).__name__}, expected dict')
        return False
    missing = [f for f in REQUIRED_SEQ_FIELDS if f not in seq]
    if missing:
        errors.append(f'missing required top-level field(s): {sorted(missing)}')
        return False
    return True


def _check_top_level_types(seq: dict[str, Any], errors: list[str]) -> None:
    """Field-by-field type checks for the top-level shape.

    Each violation is its own error string. Does not short-circuit so the
    operator sees all type problems in one shot."""
    if not isinstance(seq.get('seq_id'), str) or not seq['seq_id'].strip():
        errors.append('seq_id must be a non-empty string')
    if not isinstance(seq.get('label'), str):
        errors.append('label must be a string')
    if not isinstance(seq.get('spec_doc'), str):
        errors.append('spec_doc must be a string')
    if not isinstance(seq.get('created_at'), str):
        errors.append('created_at must be a string (ISO-8601 timestamp)')
    if not isinstance(seq.get('created_by'), str):
        errors.append('created_by must be a string')
    status = seq.get('status')
    if status not in VALID_SEQUENCE_STATUS:
        errors.append(
            f'status={status!r} not in {sorted(VALID_SEQUENCE_STATUS)}'
        )
    if not isinstance(seq.get('current_steps'), list):
        errors.append('current_steps must be a list')
    if not isinstance(seq.get('steps'), list):
        errors.append('steps must be a list')
    elif not seq['steps']:
        errors.append('steps list is empty — a sequence with zero steps is meaningless')
    if not isinstance(seq.get('audit_log'), list):
        errors.append('audit_log must be a list')


def _check_gap_log(seq: dict[str, Any], errors: list[str]) -> None:
    """Validate the optional `gap_log` field if present.

    Absent gap_log is fine (existing sequences validate unchanged). When
    present, must be a list of dicts each carrying string `ts`, `severity`,
    `finding`, `surfaced_by`. Severity values are not enumerated — spec
    § 3 leaves the taxonomy open."""
    if 'gap_log' not in seq:
        return
    log = seq['gap_log']
    if not isinstance(log, list):
        errors.append('gap_log must be a list')
        return
    for idx, entry in enumerate(log):
        if not isinstance(entry, dict):
            errors.append(
                f'gap_log[{idx}] is {type(entry).__name__}, expected dict'
            )
            continue
        missing = [f for f in REQUIRED_GAP_LOG_ENTRY_FIELDS if f not in entry]
        if missing:
            errors.append(
                f'gap_log[{idx}] missing required field(s): {sorted(missing)}'
            )
            continue
        for field_name in REQUIRED_GAP_LOG_ENTRY_FIELDS:
            value = entry[field_name]
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f'gap_log[{idx}] {field_name} must be a non-empty string'
                )


def _check_step_shape(step: Any, idx: int, errors: list[str]) -> bool:
    """Return True iff step is a dict with all required step-level fields."""
    if not isinstance(step, dict):
        errors.append(f'steps[{idx}] is {type(step).__name__}, expected dict')
        return False
    missing = [f for f in REQUIRED_STEP_FIELDS if f not in step]
    if missing:
        sid = step.get('step_id', f'<idx-{idx}>')
        errors.append(
            f'steps[{idx}] step_id={sid!r} missing required field(s): {sorted(missing)}'
        )
        return False
    return True


def _check_step_types(step: dict[str, Any], idx: int, errors: list[str]) -> None:
    """Field-by-field type checks for one step entry."""
    sid = step.get('step_id', f'<idx-{idx}>')
    if not isinstance(step.get('step_id'), str) or not step['step_id'].strip():
        errors.append(f'steps[{idx}] step_id must be a non-empty string')
    if not isinstance(step.get('label'), str):
        errors.append(f'steps[{idx}] step_id={sid!r} label must be a string')
    if not isinstance(step.get('depends_on'), list):
        errors.append(
            f'steps[{idx}] step_id={sid!r} depends_on must be a list'
        )
    else:
        for j, dep in enumerate(step['depends_on']):
            if not isinstance(dep, str):
                errors.append(
                    f'steps[{idx}] step_id={sid!r} depends_on[{j}]={dep!r} '
                    f'must be a string'
                )
    dispatch = step.get('dispatch_text')
    if not isinstance(dispatch, str):
        errors.append(
            f'steps[{idx}] step_id={sid!r} dispatch_text must be a string'
        )
    elif len(dispatch) > 500:
        errors.append(
            f'steps[{idx}] step_id={sid!r} dispatch_text is {len(dispatch)} '
            f'chars; spec § 5.5 discipline 2 caps it at 500'
        )
    if not isinstance(step.get('target_repo'), str):
        errors.append(
            f'steps[{idx}] step_id={sid!r} target_repo must be a string'
        )
    if not isinstance(step.get('task_type'), str):
        errors.append(
            f'steps[{idx}] step_id={sid!r} task_type must be a string'
        )
    status = step.get('status')
    if status not in VALID_STEP_STATUS:
        errors.append(
            f'steps[{idx}] step_id={sid!r} status={status!r} not in '
            f'{sorted(VALID_STEP_STATUS)}'
        )
    actor = step.get('current_actor')
    if actor is not None and actor not in VALID_STEP_ACTOR:
        errors.append(
            f'steps[{idx}] step_id={sid!r} current_actor={actor!r} not in '
            f'{sorted(VALID_STEP_ACTOR)} (or null)'
        )


# -------------------- DAG checks --------------------


def _check_unique_step_ids(steps: list[dict[str, Any]], errors: list[str]) -> bool:
    """Return True iff every step_id appears exactly once."""
    seen: dict[str, int] = {}
    duplicate = False
    for idx, step in enumerate(steps):
        sid = step.get('step_id')
        if not isinstance(sid, str):
            continue  # type error already flagged
        if sid in seen:
            errors.append(
                f'duplicate step_id={sid!r} at steps[{idx}] (first at '
                f'steps[{seen[sid]}])'
            )
            duplicate = True
        else:
            seen[sid] = idx
    return not duplicate


def _check_depends_on_references(
    steps: list[dict[str, Any]], errors: list[str]
) -> None:
    """Verify each `depends_on` entry references an existing step_id and
    that no step depends on itself."""
    known_ids = {s.get('step_id') for s in steps if isinstance(s.get('step_id'), str)}
    for step in steps:
        sid = step.get('step_id')
        deps = step.get('depends_on')
        if not isinstance(sid, str) or not isinstance(deps, list):
            continue
        for dep in deps:
            if not isinstance(dep, str):
                continue
            if dep == sid:
                errors.append(
                    f'step {sid!r} depends on itself (self-loop)'
                )
                continue
            if dep not in known_ids:
                errors.append(
                    f'step {sid!r} depends_on references unknown step_id={dep!r}'
                )


def _check_no_cycles(
    steps: list[dict[str, Any]], errors: list[str]
) -> None:
    """Run Kahn's algorithm; if not all nodes resolve, the leftovers form
    one or more cycles. Reports the leftover ids so the operator can see
    where the cycle lives without re-running through a DFS trace."""
    in_degree: dict[str, int] = {}
    edges: dict[str, list[str]] = {}
    for step in steps:
        sid = step.get('step_id')
        deps = step.get('depends_on')
        if not isinstance(sid, str) or not isinstance(deps, list):
            return  # Type errors already flagged; skip cycle check.
        in_degree.setdefault(sid, 0)
        edges.setdefault(sid, [])
        for dep in deps:
            if not isinstance(dep, str):
                return
            # Edge dep → sid means "sid waits on dep".
            edges.setdefault(dep, []).append(sid)
            in_degree[sid] = in_degree.get(sid, 0) + 1
            in_degree.setdefault(dep, 0)
    # Drop edges to unknown dep ids — those surface as missing-ref errors
    # already; counting their in-degree contribution would double-report.
    known_ids = {s.get('step_id') for s in steps if isinstance(s.get('step_id'), str)}
    for sid in list(in_degree.keys()):
        if sid not in known_ids:
            in_degree.pop(sid, None)
            edges.pop(sid, None)
    for src, dsts in list(edges.items()):
        edges[src] = [d for d in dsts if d in known_ids]
    # Recompute in-degree against the trimmed graph so missing-ref errors
    # don't artificially inflate the cycle leftovers.
    in_degree = {sid: 0 for sid in known_ids}
    for src, dsts in edges.items():
        for dst in dsts:
            in_degree[dst] = in_degree.get(dst, 0) + 1

    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    resolved: set[str] = set()
    while queue:
        sid = queue.pop()
        resolved.add(sid)
        for dst in edges.get(sid, []):
            in_degree[dst] -= 1
            if in_degree[dst] == 0:
                queue.append(dst)
    leftovers = sorted(set(in_degree) - resolved)
    if leftovers:
        errors.append(
            f'depends_on contains a cycle (steps not reachable by topological '
            f'sort): {leftovers}'
        )


# -------------------- public API --------------------


def validate_dag(sequence_dict: Any) -> ValidationResult:
    """Run schema + DAG checks against a parsed sequence dict.

    Schema problems short-circuit DAG checks when they would cause spurious
    errors (e.g., steps without step_ids would each appear as a cycle).
    Otherwise all errors are accumulated so the operator sees the full
    picture in one pass."""
    errors: list[str] = []
    seq_id = sequence_dict.get('seq_id') if isinstance(sequence_dict, dict) else None
    if not _check_top_level_shape(sequence_dict, errors):
        return ValidationResult(valid=False, errors=errors, seq_id=seq_id)
    _check_top_level_types(sequence_dict, errors)
    _check_gap_log(sequence_dict, errors)
    steps = sequence_dict.get('steps')
    if not isinstance(steps, list) or not steps:
        # Already flagged in _check_top_level_types.
        return ValidationResult(valid=not errors, errors=errors, seq_id=seq_id)
    step_shapes_ok = True
    for idx, step in enumerate(steps):
        if not _check_step_shape(step, idx, errors):
            step_shapes_ok = False
    if not step_shapes_ok:
        return ValidationResult(valid=False, errors=errors, seq_id=seq_id)
    for idx, step in enumerate(steps):
        _check_step_types(step, idx, errors)
    if not _check_unique_step_ids(steps, errors):
        return ValidationResult(valid=False, errors=errors, seq_id=seq_id)
    _check_depends_on_references(steps, errors)
    _check_no_cycles(steps, errors)
    # current_steps entries must reference known step_ids.
    known_ids = {s['step_id'] for s in steps if isinstance(s.get('step_id'), str)}
    cur = sequence_dict.get('current_steps')
    if isinstance(cur, list):
        for entry in cur:
            if not isinstance(entry, str):
                errors.append(
                    f'current_steps entry {entry!r} must be a string'
                )
            elif entry not in known_ids:
                errors.append(
                    f'current_steps entry {entry!r} not in steps[].step_id'
                )
    return ValidationResult(valid=not errors, errors=errors, seq_id=seq_id)


def _iter_sequence_files(blackboard_dir: Path) -> Iterable[Path]:
    """Yield every *.json file directly under blackboard_dir.

    Hidden files (dotfiles) and subdirectories (.archive/) are skipped —
    .archive contains old completed sequences that should not block new
    creation per spec § 5.1 "Daily rotation of completed sequences"."""
    if not blackboard_dir.is_dir():
        return
    for entry in sorted(blackboard_dir.iterdir()):
        if entry.is_file() and entry.suffix == '.json' and not entry.name.startswith('.'):
            yield entry


def validate_no_concurrent_active(
    blackboard_dir: Optional[Path] = None,
) -> bool:
    """Per spec § 5.2 decision A: True iff no live sequence currently exists.

    Live = status in {pending, active}. Paused sequences do not block (Larry
    may resume them later but they are not actively dispatching steps);
    completed / failed / archived likewise do not block.

    Malformed sequence files are treated as live to fail closed — a parse
    error on a sequence file means the operator can't be sure what state
    that sequence is in, and the safe default is to block new creation
    until the malformed file is resolved.

    Defaults to ~/agents/blackboard/build-sequences/ if no dir override.
    Used by Beacon at sequence-creation time (PR-S3 wires the call site)
    and as a sanity check during advancer ticks.
    """
    bdir = blackboard_dir or DEFAULT_BLACKBOARD_DIR
    if not bdir.is_dir():
        return True
    for entry in _iter_sequence_files(bdir):
        try:
            seq = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            # Fail-closed: an unreadable file is treated as a live blocker.
            return False
        status = seq.get('status') if isinstance(seq, dict) else None
        if status in LIVE_SEQUENCE_STATUSES:
            return False
    return True


# -------------------- CLI --------------------


def _cli(argv: list[str]) -> int:
    # PR-S4 rectification (H5): support both forms documented in
    # `agents/beacon/CLAUDE.md` discipline 2 + spec § 5.5:
    #   python3 scripts/build_sequence_validator.py validate <seq-id>
    #   python3 scripts/build_sequence_validator.py <path-to-file>
    # The `validate <seq-id>` form expands to the canonical blackboard
    # path so Beacon doesn't have to remember the directory layout. The
    # positional path form is preserved for direct file usage (Mirror's
    # DAG preflight, ad-hoc validation, test fixtures).
    parser = argparse.ArgumentParser(
        description='Validate a build-sequence file against spec § 5.1 + DAG checks.',
    )
    parser.add_argument(
        'args',
        nargs='+',
        help=(
            'Either `validate <seq-id>` (expands to '
            '~/agents/blackboard/build-sequences/<seq-id>.json) OR a path to '
            'a sequence file (JSON). Exits 0 if valid, 1 otherwise.'
        ),
    )
    parsed = parser.parse_args(argv)
    raw_args = parsed.args

    if len(raw_args) == 2 and raw_args[0] == 'validate':
        seq_id = raw_args[1]
        path = DEFAULT_BLACKBOARD_DIR / f'{seq_id}.json'
    elif len(raw_args) == 1:
        path = Path(raw_args[0])
    else:
        sys.stderr.write(
            'ERROR: usage:\n'
            '  build_sequence_validator.py validate <seq-id>\n'
            '  build_sequence_validator.py <path-to-sequence-file.json>\n'
        )
        return 2

    if not path.is_file():
        sys.stderr.write(f'ERROR: not a file: {path}\n')
        return 1
    try:
        seq = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.stderr.write(f'ERROR: {path}: invalid JSON: {e}\n')
        return 1
    except OSError as e:
        sys.stderr.write(f'ERROR: {path}: read failed: {e}\n')
        return 1
    result = validate_dag(seq)
    if result.valid:
        label = result.seq_id or path.name
        sys.stdout.write(f'OK: {label} valid\n')
        return 0
    label = result.seq_id or path.name
    sys.stderr.write(f'INVALID: {label}\n')
    for err in result.errors:
        sys.stderr.write(f'  - {err}\n')
    return 1


if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))
