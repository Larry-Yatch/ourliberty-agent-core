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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Tuple

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
DEFAULT_BLACKBOARD_DIR = AGENTS_ROOT / 'blackboard' / 'build-sequences'

# The agent-core repo root, derived from this file's location
# (scripts/build_sequence_validator.py → parents[1]). Used as the default
# anchor for resolving a sequence's repo-relative `spec_doc` and for the
# `git -C <root>` calls in check_spec_doc_presence — deterministic on both
# the authoring Mac and the droplet, unlike cwd.
REPO_ROOT = Path(__file__).resolve().parents[1]

# Test seam for the `check-spec-doc` CLI's repo-root anchor. UNSET (the
# production default) preserves REPO_ROOT behavior exactly — Mirror's DAG
# preflight and ad-hoc validation never set it, so their `origin/main`
# resolution is unchanged. SpecDocCliTest sets it to a throwaway /tmp git
# fixture whose `origin/main` deterministically resolves, so the CLI's
# behind-origin-vs-not-authored classification no longer depends on the
# ambient checkout's fetch state (the recurring regression-gate flake).
SPEC_DOC_REPO_ROOT_ENV = 'OURLIBERTY_SPEC_DOC_REPO_ROOT'

# The canonical agent-core repo name in config/agent-models.json `repo_paths`.
# A sequence whose effective target_repo is agent-core resolves its spec_doc
# against REPO_ROOT (the ambient checkout / worktree), NOT the canonical
# repo_paths path — so behavior for the common agent-core case stays
# byte-for-byte identical to before the target_repo resolution was added.
AGENT_CORE_REPO_NAME = 'ourliberty-agent-core'

# config/agent-models.json lives at <scripts>/../config/agent-models.json —
# the same canonical block routing/dispatch and the advancer validate against.
AGENT_MODELS_CONFIG = REPO_ROOT / 'config' / 'agent-models.json'

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

# Per spec `agents/beacon/specs/projects-v3-p4-build-completion-autoverify.md`
# § 4 Contract B: an optional sequence-level `post_merge` block carrying
# finish-steps run by the executor when the sequence completes. Three optional
# list keys: `restart` (services, always human-gated), `run` (one-time
# cleanups — a plain string is gated; a `{"cmd": str, "safe": bool}` object may
# auto-run when `safe` is true), `verify` (read-only go-live probes, always
# auto). The validator type-checks shape only; it does NOT execute or judge
# command safety (the executor's classification owns that).
POST_MERGE_LIST_KEYS = ('restart', 'run', 'verify')


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


def _check_post_merge(seq: dict[str, Any], errors: list[str]) -> None:
    """Validate the optional `post_merge` block if present.

    Absent post_merge is fine (existing sequences validate unchanged). When
    present, must be a dict with optional `restart` / `run` / `verify` list
    keys. `restart` and `verify` entries are non-empty strings. `run` entries
    are EITHER a non-empty string (gated by default) OR an object
    `{"cmd": <non-empty str>, "safe": <bool>}` (auto-runs only when
    `safe: true`). Unknown keys inside the block are tolerated — the validator
    is permissive about extension, strict about the shapes it knows."""
    if 'post_merge' not in seq:
        return
    block = seq['post_merge']
    if not isinstance(block, dict):
        errors.append('post_merge must be a dict')
        return
    for key in POST_MERGE_LIST_KEYS:
        if key not in block:
            continue
        entries = block[key]
        if not isinstance(entries, list):
            errors.append(f'post_merge.{key} must be a list')
            continue
        for idx, entry in enumerate(entries):
            if key == 'run':
                _check_run_entry(entry, idx, errors)
            elif not isinstance(entry, str) or not entry.strip():
                errors.append(
                    f'post_merge.{key}[{idx}] must be a non-empty string'
                )


def _check_run_entry(entry: Any, idx: int, errors: list[str]) -> None:
    """A post_merge.run entry: a non-empty string, or an object carrying a
    non-empty string `cmd` and a bool `safe`."""
    if isinstance(entry, str):
        if not entry.strip():
            errors.append(f'post_merge.run[{idx}] must be a non-empty string')
        return
    if not isinstance(entry, dict):
        errors.append(
            f'post_merge.run[{idx}] must be a string or '
            f'{{"cmd": str, "safe": bool}} object'
        )
        return
    cmd = entry.get('cmd')
    if not isinstance(cmd, str) or not cmd.strip():
        errors.append(f'post_merge.run[{idx}].cmd must be a non-empty string')
    if 'safe' in entry and not isinstance(entry['safe'], bool):
        errors.append(f'post_merge.run[{idx}].safe must be a bool')


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
    _check_post_merge(sequence_dict, errors)
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


# -------------------- spec_doc presence guard --------------------
#
# Incident 2026-06-10: a build-sequence kickoff failed DAG-preflight with
# the conclusion "the spec was never authored / referenced sections
# missing." That diagnosis was WRONG — the spec had been authored and
# merged to `origin/main` (PR #415, 9dbc2ae). The real cause: the droplet's
# `~/agent-core` working copy lagged `origin/main` by one commit, so the
# spec file was not yet in the checkout that Mirror's preflight and Forge
# read. `ourliberty-sync.timer` later fast-forwarded HEAD and the file
# appeared. A freshly-merged spec lives on `origin/main` but is invisible
# to Beacon/Mirror/Forge until sync advances HEAD — and the symptom
# masquerades as "spec never authored", sending people to re-author a spec
# that already exists (duplicate/conflict risk).
#
# This guard distinguishes the two cases deterministically: a spec_doc
# missing locally but present on origin/main is a SYNC-LAG problem, not a
# missing-spec problem.

# spec_doc presence statuses.
SPEC_DOC_PRESENT = 'present'            # in the working copy — all good
SPEC_DOC_BEHIND_ORIGIN = 'behind_origin'  # missing locally, present on origin/main → run sync
SPEC_DOC_NOT_AUTHORED = 'not_authored'    # absent both locally and on origin/main → author it
SPEC_DOC_INDETERMINATE = 'indeterminate'  # missing locally and origin/main doesn't resolve here


@dataclass
class SpecDocPresence:
    """Result of check_spec_doc_presence. Truthy iff the spec is present.

    `status` is one of the SPEC_DOC_* constants. `message` is the
    operator-facing, actionable string — emit it verbatim instead of the
    misleading "spec never authored". `behind_by` is the HEAD..origin/main
    commit count when known (status == behind_origin), else None."""
    status: str
    spec_doc: str
    message: str
    behind_by: Optional[int] = None

    def __bool__(self) -> bool:
        return self.status == SPEC_DOC_PRESENT


# A git runner takes argv (without the leading `git`) and returns
# (returncode, stdout-stripped). Injectable so tests stay hermetic.
GitRunner = Callable[[list[str]], Tuple[int, str]]


def _default_git_runner(repo_root: Path) -> GitRunner:
    """Build a `git -C <repo_root> ...` runner. Any OS/subprocess failure
    (git absent, not a repo, timeout) is folded into a non-zero rc so
    callers treat it as "could not determine" rather than crashing."""
    def run(argv: list[str]) -> Tuple[int, str]:
        try:
            proc = subprocess.run(
                ['git', '-C', str(repo_root), *argv],
                capture_output=True, text=True, timeout=15,
            )
            return proc.returncode, (proc.stdout or '').strip()
        except (OSError, subprocess.SubprocessError):
            return 1, ''
    return run


def _load_repo_paths(config_path: Path = AGENT_MODELS_CONFIG) -> dict[str, Path]:
    """Repo name → checkout Path from config/agent-models.json `repo_paths`.

    Same canonical block routing/dispatch and the advancer read. Best-effort:
    any read/parse/shape error returns an EMPTY map, which callers treat as
    "can't map target_repo this run" and fall back to the agent-core default
    (never a hard failure just because the config was briefly unreadable)."""
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    block = data.get('repo_paths') if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return {}
    out: dict[str, Path] = {}
    for name, raw in block.items():
        if isinstance(name, str) and name and isinstance(raw, str) and raw:
            out[name] = Path(raw)
    return out


def effective_target_repo(seq: Any) -> Optional[str]:
    """The repo a sequence's steps predominantly target, or None.

    Returns the majority `target_repo` across the sequence's steps (tie-break:
    first-seen). Steps with a non-string / empty target_repo are ignored. A
    sequence with no steps (or no usable target_repo) returns None so the
    caller falls back to the agent-core default. Cross-repo sequences are not
    expected in V1 (rsdpm-v0-001's 20 steps are uniformly RSDPM); majority is
    the defensive choice if steps ever disagree."""
    if not isinstance(seq, dict):
        return None
    steps = seq.get('steps')
    if not isinstance(steps, list):
        return None
    counts: dict[str, int] = {}
    order: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        repo = step.get('target_repo')
        if not isinstance(repo, str) or not repo.strip():
            continue
        repo = repo.strip()
        if repo not in counts:
            counts[repo] = 0
            order.append(repo)
        counts[repo] += 1
    if not order:
        return None
    # Majority, tie-broken by first-seen (order preserves insertion).
    return max(order, key=lambda r: (counts[r], -order.index(r)))


def resolve_spec_doc_repo_root(
    seq: Any,
    *,
    env: Optional[dict[str, str]] = None,
    repo_paths: Optional[dict[str, Path]] = None,
) -> Optional[Path]:
    """Which checkout root a sequence's `spec_doc` should resolve against.

    Resolution order:
      1. SPEC_DOC_REPO_ROOT_ENV set → that path. The test override wins so
         SpecDocCliTest's hermetic /tmp fixture keeps working regardless of
         the sequence's target_repo.
      2. Effective target_repo is agent-core (the common case), unset, or not
         in `repo_paths` → None. The caller then anchors at REPO_ROOT exactly
         as before — agent-core sequences are byte-for-byte unchanged, and an
         unmappable repo degrades to the old behavior rather than failing.
      3. Effective target_repo maps to a checkout in `repo_paths` → that path.
         A cross-repo sequence (e.g. rsdpm-v0-001, target_repo=RSDPM) resolves
         its spec_doc inside the RSDPM checkout where the file actually lives.

    `env` / `repo_paths` are injectable for hermetic tests; they default to the
    process environment and the on-disk config map."""
    environ = env if env is not None else os.environ
    override = environ.get(SPEC_DOC_REPO_ROOT_ENV)
    if override:
        return Path(override)
    target = effective_target_repo(seq)
    if not target or target == AGENT_CORE_REPO_NAME:
        return None
    paths = repo_paths if repo_paths is not None else _load_repo_paths()
    return paths.get(target)


def check_spec_doc_presence(
    spec_doc: Any,
    repo_root: Optional[Path] = None,
    *,
    git: Optional[GitRunner] = None,
    local_exists: Optional[Callable[[], bool]] = None,
) -> SpecDocPresence:
    """Classify whether a sequence's `spec_doc` is usable from this checkout.

    Resolution order:
      1. Present in the working copy → SPEC_DOC_PRESENT (the common,
         authoring-time-on-Mac case; no git needed).
      2. Missing locally, but `git cat-file -e origin/main:<spec_doc>`
         succeeds → SPEC_DOC_BEHIND_ORIGIN. The spec exists on main; this
         checkout just hasn't synced. Message tells the operator to run
         `ourliberty-sync.service`, NOT to re-author.
      3. Missing locally AND absent on origin/main → SPEC_DOC_NOT_AUTHORED
         (the genuine "author + merge it first" case).
      4. Missing locally but `origin/main` doesn't resolve (not a synced
         git checkout, e.g. a test fixture or a detached export) →
         SPEC_DOC_INDETERMINATE. We can't tell behind-origin from
         never-authored, so we don't claim either; callers should not hard-
         fail on this branch.

    `repo_root` defaults to REPO_ROOT (agent-core root). `git` and
    `local_exists` are injectable for hermetic tests."""
    if not isinstance(spec_doc, str) or not spec_doc.strip():
        return SpecDocPresence(
            status=SPEC_DOC_INDETERMINATE,
            spec_doc=str(spec_doc),
            message=(
                'spec_doc is empty or not a string; cannot check presence. '
                'Fix the sequence file\'s `spec_doc` field.'
            ),
        )
    spec_doc = spec_doc.strip()
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    if local_exists is None:
        def local_exists() -> bool:  # type: ignore[misc]
            return (root / spec_doc).is_file()

    if local_exists():
        return SpecDocPresence(
            status=SPEC_DOC_PRESENT,
            spec_doc=spec_doc,
            message=f'spec_doc `{spec_doc}` is present in the working copy.',
        )

    run = git if git is not None else _default_git_runner(root)

    # origin/main must resolve to distinguish behind-origin from
    # never-authored. If it doesn't, stay honest: indeterminate.
    rc, _ = run(['rev-parse', '--verify', '--quiet', 'origin/main'])
    if rc != 0:
        return SpecDocPresence(
            status=SPEC_DOC_INDETERMINATE,
            spec_doc=spec_doc,
            message=(
                f'spec_doc `{spec_doc}` is missing from the working copy and '
                f'`origin/main` does not resolve here (not a synced git '
                f'checkout). Cannot distinguish a sync-lag from a never-'
                f'authored spec — verify on `origin/main` manually before '
                f're-authoring.'
            ),
        )

    rc, _ = run(['cat-file', '-e', f'origin/main:{spec_doc}'])
    if rc == 0:
        # Present on origin/main, missing locally → this checkout is behind.
        behind: Optional[int] = None
        crc, cout = run(['rev-list', '--count', 'HEAD..origin/main'])
        if crc == 0 and cout.isdigit():
            behind = int(cout)
        # Truthy check, not `is not None`: a 0 count is anomalous here (the
        # file is on origin/main yet absent locally while HEAD is NOT behind
        # — e.g. an uncommitted local deletion), so don't print the self-
        # contradictory "behind by 0 commit(s)". Fall back to generic wording.
        by = f'{behind} commit(s)' if behind else 'one or more commits'
        return SpecDocPresence(
            status=SPEC_DOC_BEHIND_ORIGIN,
            spec_doc=spec_doc,
            behind_by=behind,
            message=(
                f'working copy is behind origin/main by {by}; spec_doc '
                f'`{spec_doc}` EXISTS on origin/main but is not yet in this '
                f'checkout. Run sync (`systemctl start '
                f'ourliberty-sync.service`) before kickoff, then re-dispatch. '
                f'This is a sync-lag, NOT a missing spec — do not re-author it.'
            ),
        )

    return SpecDocPresence(
        status=SPEC_DOC_NOT_AUTHORED,
        spec_doc=spec_doc,
        message=(
            f'spec_doc `{spec_doc}` not found in the working copy or on '
            f'origin/main — author + merge it first, then re-dispatch the '
            f'kickoff.'
        ),
    )


# -------------------- CLI --------------------


def _read_seq_json(path: Path) -> Tuple[Optional[Any], int]:
    """Read + JSON-parse a sequence file for the CLI, writing a diagnostic
    to stderr on failure. Returns `(parsed, 0)` on success or `(None, 1)`
    on a missing/unreadable/invalid file. Shared by the `validate`,
    `check-spec-doc`, and bare-path CLI forms so their error wording can't
    drift apart."""
    if not path.is_file():
        sys.stderr.write(f'ERROR: not a file: {path}\n')
        return None, 1
    try:
        return json.loads(path.read_text()), 0
    except json.JSONDecodeError as e:
        sys.stderr.write(f'ERROR: {path}: invalid JSON: {e}\n')
        return None, 1
    except OSError as e:
        sys.stderr.write(f'ERROR: {path}: read failed: {e}\n')
        return None, 1


def _cli_check_spec_doc(seq_id_or_path: str) -> int:
    """CLI for `check-spec-doc <seq-id|path>`.

    Resolves the sequence file (a bare token expands to the canonical
    blackboard path; anything containing a path separator or `.json` is
    treated as a path), reads its `spec_doc`, and classifies presence.

    Exit codes (distinct so callers can branch without parsing stdout):
      0  present OR indeterminate (don't block — authoring-time-on-Mac and
         not-a-synced-checkout both land here and shouldn't fail kickoff)
      1  not_authored — genuinely missing; author + merge first
      3  behind_origin — spec exists on main; run sync, don't re-author

    Repo-root resolution (resolve_spec_doc_repo_root): SPEC_DOC_REPO_ROOT_ENV,
    when set, wins and anchors local-file resolution + the `git -C <root>`
    probes at that path (the SpecDocCliTest /tmp-fixture seam). Otherwise the
    sequence's effective target_repo picks the checkout: agent-core / unset /
    unmappable → None (falls through to REPO_ROOT, unchanged); a mapped
    cross-repo target (e.g. RSDPM) → that repo's local checkout, so a spec_doc
    living in the target repo (rsdpm-v0-001's BUILD_PLAN.md) resolves where it
    actually exists instead of false-failing NOT_AUTHORED against agent-core.
    """
    token = seq_id_or_path
    if token.endswith('.json') or '/' in token or os.sep in token:
        path = Path(token)
    else:
        path = DEFAULT_BLACKBOARD_DIR / f'{token}.json'
    seq, rc = _read_seq_json(path)
    if seq is None:
        return rc
    spec_doc = seq.get('spec_doc') if isinstance(seq, dict) else None
    repo_root = resolve_spec_doc_repo_root(seq)
    presence = check_spec_doc_presence(spec_doc, repo_root=repo_root)
    if presence.status == SPEC_DOC_PRESENT:
        sys.stdout.write(f'OK: {presence.message}\n')
        return 0
    if presence.status == SPEC_DOC_INDETERMINATE:
        sys.stdout.write(f'INDETERMINATE: {presence.message}\n')
        return 0
    if presence.status == SPEC_DOC_BEHIND_ORIGIN:
        sys.stderr.write(f'BEHIND_ORIGIN: {presence.message}\n')
        return 3
    sys.stderr.write(f'NOT_AUTHORED: {presence.message}\n')
    return 1


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
            '~/agents/blackboard/build-sequences/<seq-id>.json), '
            '`check-spec-doc <seq-id|path>` (classify the sequence\'s '
            'spec_doc as present / behind-origin / not-authored), OR a path '
            'to a sequence file (JSON). Exits 0 if valid, 1 otherwise.'
        ),
    )
    parsed = parser.parse_args(argv)
    raw_args = parsed.args

    if len(raw_args) == 2 and raw_args[0] == 'validate':
        seq_id = raw_args[1]
        path = DEFAULT_BLACKBOARD_DIR / f'{seq_id}.json'
    elif len(raw_args) == 2 and raw_args[0] == 'check-spec-doc':
        return _cli_check_spec_doc(raw_args[1])
    elif len(raw_args) == 1:
        path = Path(raw_args[0])
    else:
        sys.stderr.write(
            'ERROR: usage:\n'
            '  build_sequence_validator.py validate <seq-id>\n'
            '  build_sequence_validator.py check-spec-doc <seq-id|path>\n'
            '  build_sequence_validator.py <path-to-sequence-file.json>\n'
        )
        return 2

    seq, rc = _read_seq_json(path)
    if seq is None:
        return rc
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
