#!/usr/bin/env python3
"""launch_queue_drain.py — the Beacon-side drain of the build-launch-queue.

projects-v3 P3, step p3-launch-queue-drain (spec
`agents/beacon/specs/projects-v3-p3-pipeline.md` § 7 step 3).

The dashboard "Launch build" click never touches Telegram and never commits.
It POSTs to `POST /api/projects/launch`, which writes a NON-committer queue
file under `~/agents/blackboard/build-launch-queue/<phase_id>.json` (the
`+New mission` precedent). This drain — run on a systemd timer — turns each
queued request into a real build:

  1. Author a one-step build SEQUENCE from the phase's spec, reusing the
     orchestrator machinery: the § 5.1 sequence schema + `build_sequence_
     validator.validate_dag` fail-fast gate. The sequence is written to
     `~/agents/blackboard/build-sequences/launch-<phase_id>.json` at
     `status: pending`.
  2. Run Mirror DAG preflight by dispatching the canonical
     `review-sequence-dag <seq-id>` routing-signal to Mirror's inbox via
     `safe_write_inbox(source='orchestrator')`. On Mirror PASS the outbox
     notifier's `_handle_mirror_dag_preflight_result` auto-transitions the
     sequence `pending → active`, and the advancer dispatches the first
     (only) step to Beacon → Forge. That is the build "kick" — no human, no
     Telegram.

**Idempotent on phase id.** The seq id is the deterministic `launch-<phase_id>`.
A re-launch of the same phase (a double-click, or a re-queue after this file
already drained and was removed) finds the sequence file already present:

  * status `pending`  → the sequence was authored but the Mirror dispatch may
    not have completed (e.g. a crash between the two writes); re-dispatch the
    Mirror preflight to the SAME deterministic inbox filename (an overwrite,
    not a duplicate) and remove the queue file. This re-dispatch is safe — the
    build only fires once, when the notifier flips the sequence to `active`.
  * any non-pending status (active/complete/failed/paused/archived) → the
    launch already proceeded; remove the queue file and do nothing else. NO
    second build is dispatched.

Crucially the drain does NOT mutate `projects.json` — that store has exactly
ONE committer (`heal_projects_store.py`); the drain (like the dashboard) is a
non-committer to it. Idempotency rides on the deterministic sequence-file
existence, NOT on a projects.json flag, so there is no second on-disk writer
racing the Promote endpoint (the #409→#413 dual-writer data-loss class).

Stdlib only beyond the two sibling reuse modules; runnable as a timer-driven
one-shot (`python3 scripts/launch_queue_drain.py`).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Allow imports of sibling modules in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_sequence_validator  # noqa: E402
import larry_alerts  # noqa: E402
import launch_dedup_guard  # noqa: E402 — pure-filesystem duplicate-work guard
import routing_validator  # noqa: E402
import safe_write_inbox  # noqa: E402

logger = logging.getLogger('launch_queue_drain')

# The DAG-preflight dispatch goes orchestrator → mirror (orchestrator is a
# routing_validator SYSTEM_SOURCE, so the topology check passes). The canonical
# `review-sequence-dag ` prompt prefix + phase `routing-signal` bypass the
# dispatch_validator MIN_PROMPT_LEN check for the short routing signal.
_MIRROR_AGENT = 'mirror'
_SOURCE_AGENT = 'orchestrator'
# Mirror reviews the sequence FILE, which lives in agent-core regardless of the
# build's own target repo — so the DAG-preflight task targets agent-core.
_MIRROR_REVIEW_REPO = 'ourliberty-agent-core'

# The build step's defaults.
_STEP_TASK_TYPE = 'feature-development'
_DEFAULT_BUILD_REPO = 'ourliberty-agent-core'

# The build step is dispatched Beacon → Forge, so the step's target_repo must
# fall within Forge's allowed_repos (config/agent-models.json, via
# routing_validator.allowed_repos_for). We resolve + validate the launch
# entry's `repo` against this set at AUTHOR time so a non-allowed value fails
# here (a dead-letter + a Larry-actionable alert) rather than at DISPATCH time
# as an opaque RoutingDenied that strands the build (the pipeline-empty-state-
# hint 'ol-work' incident, 2026-06-19).
_BUILD_AGENT = 'forge'
_ALERT_SOURCE = 'launch-queue-drain'

# Bound the work per tick so a flood of queue files can't monopolize a tick.
MAX_PER_TICK = 25


class UnknownRepoError(Exception):
    """The launch entry's resolved `repo` is not an allowed build repo and is
    not a short-form alias of one. Carries the offending value + the allowed
    set so the caller can dead-letter with an actionable message."""

    def __init__(self, repo: str, allowed: list[str]):
        self.repo = repo
        self.allowed = allowed
        super().__init__(
            f'target_repo {repo!r} is not an allowed build repo '
            f'(allowed: {allowed})'
        )


def _resolve_target_repo(raw_repo: Any) -> str:
    """Resolve a launch entry's `repo` to a canonical allowed build repo.

    Resolution order:

      1. Empty/None → the default build repo (`ourliberty-agent-core`).
      2. Already a canonical allowed repo → unchanged.
      3. A short-form alias (the bare name without the `ourliberty-` prefix,
         e.g. `dashboard` → `ourliberty-dashboard`) that resolves to an allowed
         repo → the canonical form. The alias is *derived* from the canonical
         naming, so it can only ever map onto a repo already in the allow-list —
         it cannot invent an out-of-list target, and a new `ourliberty-X` repo
         added to the allow-list gets its `X` short form for free.

    Anything else (e.g. `ol-work`, a bad value with no alias scheme) raises
    `UnknownRepoError`. Fails OPEN only when Forge has no `allowed_repos`
    configured — mirroring `routing_validator.check_target_repo`'s back-compat
    posture so a missing/unreadable config degrades to the pre-guard behavior
    rather than rejecting every launch."""
    repo = (str(raw_repo).strip() if raw_repo else '') or _DEFAULT_BUILD_REPO
    allowed = routing_validator.allowed_repos_for(_BUILD_AGENT)
    if not allowed:
        return repo
    if repo in allowed:
        return repo
    candidate = f'ourliberty-{repo}'
    if candidate in allowed:
        return candidate
    raise UnknownRepoError(repo, sorted(allowed))


def _alert_unknown_repo(phase_id: str, repo: str, allowed: list[str]) -> None:
    """Surface an author-time repo rejection to Larry (best-effort). Naming the
    phase + the bad repo so the fix (correct the phase/project `repo` field in
    projects.json to a canonical repo) is obvious. Never raises — an alert-queue
    failure (or the test jail) must not crash the drain."""
    try:
        larry_alerts.append_alert(
            source=_ALERT_SOURCE,
            severity='warning',
            message=(
                f'Launch of phase {phase_id!r} was rejected at author time: its '
                f'target_repo {repo!r} is not an allowed build repo '
                f'(allowed: {allowed}). No build was dispatched. Fix the phase/'
                f'project `repo` field in projects.json to a canonical repo and '
                f're-launch.'
            ),
            subject=phase_id,
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        logger.warning(
            'could not emit unknown-repo alert for phase %s: %s', phase_id, e)


def _agents_root() -> Path:
    """Env-overridable so tests redirect the whole blackboard to a tmpdir."""
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def _valid_repos() -> frozenset[str]:
    """Buildable repo names from ``config/agent-models.json`` ``repo_paths`` (the
    canonical repo block; config lives at ``<scripts>/../config/agent-models.json``).

    Best-effort: any read/parse error returns an EMPTY set, which the caller
    treats as "can't validate" and SKIPS the repo check (fail OPEN — never
    dead-letter otherwise-fine work over a transient config read miss). A
    populated set enables the belt-and-suspenders check in `_drain_one`."""
    cfg_path = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'
    try:
        data = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    block = data.get('repo_paths') if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return frozenset()
    return frozenset(k for k in block if isinstance(k, str) and k)


def _launch_queue_dir() -> Path:
    """Where `POST /api/projects/launch` drops queued launch requests."""
    return _agents_root() / 'blackboard' / 'build-launch-queue'


def _sequences_dir() -> Path:
    """The build-sequence blackboard — the SAME dir the outbox notifier reads
    on Mirror PASS (`AGENTS_ROOT/blackboard/build-sequences`). Keyed off
    `_agents_root()` so production and test agree on the path the notifier
    later resolves `launch-<phase_id>.json` against."""
    return _agents_root() / 'blackboard' / 'build-sequences'


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# sequence authoring (reuses the § 5.1 schema + validate_dag gate)
# --------------------------------------------------------------------------- #
def _build_dispatch_text(
    *, title: str, project_id: str, desired_end_state: str, spec_ref: str,
) -> str:
    """The step's ≤500-char dispatch_text (spec § 5.5 discipline 2): what to
    build + the spec pointer + a one-line Mirror-review-focus. The spec pointer
    and review-focus are kept intact; only the Desired End State is trimmed if
    the whole would exceed 500 chars."""
    head = (
        f'Build phase "{title}" (project `{project_id}`). '
        f'Spec: {spec_ref}. Implement per the spec and open one PR. '
        f'Mirror focus: the PR satisfies the phase Desired End State + spec.'
    )
    des = (desired_end_state or '').strip()
    if not des:
        return head[:500]
    prefix = f'Desired End State: {des} '
    budget = 500 - len(head)
    if budget <= 0:
        return head[:500]
    if len(prefix) > budget:
        prefix = prefix[: max(0, budget - 1)].rstrip() + ' '
    return (prefix + head)[:500]


def build_sequence(entry: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """Author the one-step build sequence for a launch request, conforming to
    the orchestrator § 5.1 schema (so `validate_dag` passes and the advancer
    can dispatch it). One phase = one step, no `depends_on` (P3 launches a
    single spec-ready phase; the closeout/DAG-detail phases are P4/P5)."""
    now_iso = _now_iso(now)
    phase_id = entry['phase_id']
    project_id = str(entry.get('project_id') or '')
    seq_id = entry.get('seq_id') or f'launch-{phase_id}'
    spec_ref = str(entry.get('spec_ref') or '')
    title = str(entry.get('phase_title') or phase_id)
    # Author-time guard: resolve + validate the target_repo against Forge's
    # allowed_repos. Raises UnknownRepoError for a non-allowed, non-alias value
    # (caught in _drain_one → dead-letter + alert) so we never author a sequence
    # that would RoutingDeny at dispatch.
    repo = _resolve_target_repo(entry.get('repo'))

    step = {
        'step_id': phase_id,
        'label': f'Build: {title}'[:200],
        'depends_on': [],
        'dispatch_text': _build_dispatch_text(
            title=title,
            project_id=project_id,
            desired_end_state=str(entry.get('desired_end_state') or ''),
            spec_ref=spec_ref,
        ),
        'target_repo': repo,
        'task_type': _STEP_TASK_TYPE,
        'status': 'pending',
        'dispatched_at': None,
        'merged_at': None,
        'pr_url': None,
        'current_actor': None,
        'failure_reason': None,
    }
    return {
        'seq_id': seq_id,
        'label': f'Launch build: {title}'[:200],
        'spec_doc': spec_ref,
        'created_at': now_iso,
        'created_by': 'launch_queue_drain',
        'status': 'pending',
        'current_steps': [],
        'steps': [step],
        'audit_log': [{
            'ts': now_iso,
            'event': 'authored-by-launch-drain',
            'actor': 'launch_queue_drain',
            'phase_id': phase_id,
            'project_id': project_id,
        }],
    }


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Delegates to the shared guarded atomic_io writer. Byte-identical to the
    prior inline writer: pretty JSON (indent=2) + trailing newline."""
    import atomic_io
    atomic_io.atomic_write_json(path, obj, trailing_newline=True)


# --------------------------------------------------------------------------- #
# Mirror DAG-preflight dispatch (the build "kick")
# --------------------------------------------------------------------------- #
def dispatch_mirror_preflight(seq_id: str) -> Path:
    """Dispatch the canonical `review-sequence-dag <seq-id>` routing-signal to
    Mirror's inbox. The filename is DETERMINISTIC (`review-sequence-dag-<seq-
    id>.json`) so a re-dispatch overwrites the same inbox file rather than
    minting a duplicate review. On PASS the outbox notifier auto-activates the
    sequence and the advancer kicks the build."""
    task = {
        'task_id': f'review-sequence-dag-{seq_id}',
        'source': _SOURCE_AGENT,
        'prompt': f'review-sequence-dag {seq_id}',
        'task_type': 'code-review',
        'phase': 'routing-signal',
        'target_repo': _MIRROR_REVIEW_REPO,
        'reply_chat_id': None,
    }
    return safe_write_inbox.safe_write_inbox(
        target_agent=_MIRROR_AGENT,
        task_dict=task,
        source_agent=_SOURCE_AGENT,
        filename=f'review-sequence-dag-{seq_id}.json',
    )


# --------------------------------------------------------------------------- #
# the drain
# --------------------------------------------------------------------------- #
@dataclass
class DrainResult:
    """Per-tick outcome, for the audit summary + tests."""
    launched: list[str] = field(default_factory=list)        # newly authored + dispatched
    redispatched: list[str] = field(default_factory=list)    # pending seq re-kicked
    already_launched: list[str] = field(default_factory=list)  # non-pending seq, queue cleared
    dead_lettered: list[str] = field(default_factory=list)   # malformed / invalid
    dispatch_failed: list[str] = field(default_factory=list)  # left for retry next tick
    deduped: list[str] = field(default_factory=list)         # skipped — work already in flight

    def to_dict(self) -> dict[str, Any]:
        return {
            'launched': list(self.launched),
            'redispatched': list(self.redispatched),
            'already_launched': list(self.already_launched),
            'dead_lettered': list(self.dead_lettered),
            'dispatch_failed': list(self.dispatch_failed),
            'deduped': list(self.deduped),
        }


def _list_queue_files(queue_dir: Path) -> list[Path]:
    """Every `*.json` directly under the queue dir, oldest first (mtime), so a
    backlog drains in arrival order. Dotfiles + the `.failed/` dead-letter
    subdir are skipped."""
    if not queue_dir.is_dir():
        return []
    files = [
        p for p in queue_dir.iterdir()
        if p.is_file() and p.suffix == '.json' and not p.name.startswith('.')
    ]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return files


def _dead_letter(path: Path, reason: str) -> None:
    """Move a malformed / invalid queue file into `<queue_dir>/.failed/` so it
    stops re-logging every tick and a human can inspect it. Best-effort."""
    logger.warning('launch-queue %s dead-lettered: %s', path.name, reason)
    failed_dir = path.parent / '.failed'
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, failed_dir / path.name)
    except OSError as e:
        logger.warning('could not dead-letter %s: %s', path.name, e)


def _remove_queue_file(path: Path) -> None:
    """Remove a drained queue file. An already-gone file is success."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning('could not remove drained queue file %s: %s', path.name, e)


def _read_existing_status(seq_path: Path) -> Optional[str]:
    """The `status` of an already-present sequence file, or None if the file is
    unreadable/malformed (treated as 'present but unknown' → caller leaves it
    alone rather than re-authoring over it)."""
    try:
        data = json.loads(seq_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data.get('status') if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# duplicate-work guard (launch_dedup_guard — pure-filesystem, fail-open)
# --------------------------------------------------------------------------- #
# Where a launch held for duplicate-work review is parked. NOT `.failed/` — this
# is a reversible hold, not a malformed request: a wrong de-dup is recovered by
# moving the file back to the queue dir (the alert spells out the one command).
_DEDUPED_DIRNAME = '.deduped'


def _evaluate_dedup(
    entry: dict[str, Any], sequences_dir: Path, now: datetime,
) -> Optional[launch_dedup_guard.DedupVerdict]:
    """Run the duplicate-work guard, FAIL-OPEN. Returns the verdict, or None on
    any error — a guard failure must never block an otherwise-fine launch (the
    drain then authors normally)."""
    try:
        return launch_dedup_guard.evaluate(
            entry, sequences_dir=sequences_dir, now=now)
    except Exception as e:  # noqa: BLE001 — fail open; the guard is advisory
        logger.warning('dedup guard raised for %s (%s); proceeding with launch',
                       entry.get('phase_id'), e)
        return None


def _alert_duplicate_launch(
    phase_id: str, verdict: launch_dedup_guard.DedupVerdict, held_path: Optional[Path],
) -> None:
    """Surface a duplicate-work detection to Larry (best-effort). The message is
    keyed on the VERDICT (not on whether the hold happened), so a held launch
    names the one-command undo, a hold-that-could-not-be-parked says so honestly
    (rather than the advisory's misleading "proceeding"), and an advisory says
    the build is proceeding. Never raises — alerting is best-effort."""
    if verdict.action == 'skip_duplicate':
        if held_path is not None:
            action_line = (
                f'No build was authored; the launch is parked. If this is a '
                f'legitimately distinct build, re-queue it: `mv {held_path} '
                f'{held_path.parent.parent / held_path.name}`.'
            )
        else:
            action_line = (
                'No build was authored, but the launch request could NOT be '
                'parked (filesystem error) — it will be re-evaluated next tick.'
            )
    else:
        action_line = (
            'Proceeding with the build anyway (advisory only). Cancel the '
            'sequence if it turns out to be redundant.'
        )
    severity = 'warning'
    try:
        larry_alerts.append_alert(
            source=_ALERT_SOURCE,
            severity=severity,
            message=(
                f'Launch of phase {phase_id!r}: {verdict.reason} {action_line}'
            ),
            subject=phase_id,
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        logger.warning('could not emit duplicate-launch alert for %s: %s',
                       phase_id, e)


def _dedup_hold(path: Path, reason: str) -> Optional[Path]:
    """Park a de-duplicated launch request in `<queue_dir>/.deduped/` so it stops
    re-firing every tick but stays recoverable (vs `.failed/` which connotes a
    malformed request). Returns the new path, or None if the move failed (the
    caller then leaves the file in place — better a re-alert than a lost launch)."""
    logger.info('launch-queue %s held for duplicate work: %s', path.name, reason)
    held_dir = path.parent / _DEDUPED_DIRNAME
    try:
        held_dir.mkdir(parents=True, exist_ok=True)
        dest = held_dir / path.name
        os.replace(path, dest)
        return dest
    except OSError as e:
        logger.warning('could not hold de-duplicated launch %s: %s', path.name, e)
        return None


def _drain_one(path: Path, sequences_dir: Path, now: datetime, result: DrainResult) -> None:
    try:
        entry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _dead_letter(path, f'unreadable/invalid JSON: {e}')
        result.dead_lettered.append(path.name)
        return
    if not isinstance(entry, dict):
        _dead_letter(path, 'entry is not a JSON object')
        result.dead_lettered.append(path.name)
        return
    phase_id = entry.get('phase_id')
    if not isinstance(phase_id, str) or not phase_id:
        _dead_letter(path, 'entry has no string `phase_id`')
        result.dead_lettered.append(path.name)
        return

    seq_id = entry.get('seq_id') or f'launch-{phase_id}'
    seq_path = sequences_dir / f'{seq_id}.json'

    # --- idempotency: deterministic seq-file existence is the backstop ---
    if seq_path.exists():
        status = _read_existing_status(seq_path)
        if status == 'pending':
            # Authored, but the Mirror dispatch may not have completed (crash
            # between the two writes). Re-dispatch to the SAME inbox filename
            # (overwrite, not a duplicate); the build still fires only once.
            try:
                dispatch_mirror_preflight(seq_id)
            except Exception as e:  # noqa: BLE001 — leave for next-tick retry
                logger.warning(
                    'seq %s pending re-dispatch failed (%s); leaving queue file',
                    seq_id, e,
                )
                result.dispatch_failed.append(phase_id)
                return
            _remove_queue_file(path)
            result.redispatched.append(phase_id)
            return
        # Any non-pending status → already launched / in-flight. Do NOT
        # re-author and do NOT dispatch a second build; just clear the queue.
        _remove_queue_file(path)
        result.already_launched.append(phase_id)
        return

    # --- repo sanity: never author a sequence Forge can't build ---
    # The dashboard launch endpoint already validates/repairs target_repo, so
    # this is belt-and-suspenders for a legacy / hand-written queue file. An
    # invalid repo (e.g. a working-dir name like `ol-work` that rode in from a
    # capture origin) is dead-lettered rather than authored into an unbuildable
    # sequence that would sit `dispatched` forever (the 2026-06-19 wedge).
    # valid_repos empty (config unreadable) → skip the check (fail open).
    repo = str(entry.get('repo') or _DEFAULT_BUILD_REPO)
    valid_repos = _valid_repos()
    if valid_repos and repo not in valid_repos:
        _dead_letter(
            path,
            f'target_repo {repo!r} is not a buildable repo '
            f'(config/agent-models.json repo_paths: {sorted(valid_repos)})',
        )
        result.dead_lettered.append(path.name)
        return

    # --- duplicate-work guard: do not author a launch whose deliverables are
    # already in flight elsewhere (the 2026-06-20 cross-identity redundant-build
    # incident). A matching deliverable claim → reversibly hold + alert; an
    # in-flight spec overlap → advisory alert + proceed. Fail-open: a guard
    # error or a clean verdict authors normally.
    verdict = _evaluate_dedup(entry, sequences_dir, now)
    if verdict is not None and verdict.has_signal:
        if verdict.action == 'skip_duplicate':
            held = _dedup_hold(path, verdict.reason)
            _alert_duplicate_launch(phase_id, verdict, held)
            # Only count it deduped when the file was actually parked. If the
            # hold MOVE failed, the queue file is still in place: leave it for
            # next-tick re-evaluation (don't author a duplicate, don't over-
            # count) — the alert above said so honestly.
            if held is not None:
                result.deduped.append(phase_id)
            return
        # 'proceed' with a signal (in-flight spec overlap) → advisory only.
        _alert_duplicate_launch(phase_id, verdict, None)

    # --- new launch: author → validate → write → dispatch Mirror ---
    try:
        seq = build_sequence(entry, now=now)
    except UnknownRepoError as e:
        # Author-time repo rejection: the entry names a non-allowed build repo
        # (no alias resolves it). Dead-letter the request + alert Larry naming
        # the phase + bad repo, rather than authoring a doomed sequence that
        # RoutingDenies at dispatch and strands the build.
        _dead_letter(path, f'non-allowed target_repo {e.repo!r}: {e}')
        _alert_unknown_repo(phase_id, e.repo, e.allowed)
        result.dead_lettered.append(path.name)
        return
    validation = build_sequence_validator.validate_dag(seq)
    if not validation.valid:
        _dead_letter(path, f'authored sequence failed validate_dag: {validation.errors}')
        result.dead_lettered.append(path.name)
        return

    try:
        _atomic_write_json(seq_path, seq)
    except OSError as e:
        logger.warning(
            'could not write sequence file %s (%s); leaving queue file for retry',
            seq_path.name, e,
        )
        result.dispatch_failed.append(phase_id)
        return

    try:
        dispatch_mirror_preflight(seq_id)
    except Exception as e:  # noqa: BLE001
        # The sequence file is written (status pending); leave the queue file
        # so the next tick re-dispatches via the pending branch above.
        logger.warning(
            'Mirror preflight dispatch for %s failed (%s); sequence written '
            '(pending), leaving queue file for next-tick re-dispatch',
            seq_id, e,
        )
        result.dispatch_failed.append(phase_id)
        return

    _remove_queue_file(path)
    result.launched.append(phase_id)


def drain_once(
    *,
    queue_dir: Optional[Path] = None,
    sequences_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    max_per_tick: int = MAX_PER_TICK,
) -> DrainResult:
    """Drain up to `max_per_tick` queued launch requests in arrival order.
    Pure over the filesystem state it's pointed at; safe to run on every timer
    tick (idempotent per phase id)."""
    queue_dir = queue_dir or _launch_queue_dir()
    sequences_dir = sequences_dir or _sequences_dir()
    now = now or datetime.now(timezone.utc)
    result = DrainResult()
    for path in _list_queue_files(queue_dir)[:max_per_tick]:
        _drain_one(path, sequences_dir, now, result)
    return result


def _emit_summary(result: DrainResult) -> None:
    total = (len(result.launched) + len(result.redispatched)
             + len(result.already_launched) + len(result.dead_lettered)
             + len(result.dispatch_failed) + len(result.deduped))
    if total == 0:
        logger.info('launch-queue-drain: nothing queued')
        return
    logger.info(
        'launch-queue-drain: launched %s; re-dispatched %s; already-launched '
        '%s; dead-lettered %s; dispatch-failed %s; de-duped %s',
        result.launched, result.redispatched, result.already_launched,
        result.dead_lettered, result.dispatch_failed, result.deduped,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Drain the build-launch-queue into Mirror-gated build sequences.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='List what would be drained without authoring sequences or '
             'dispatching Mirror (reads the queue, makes no writes).',
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )

    if args.dry_run:
        files = _list_queue_files(_launch_queue_dir())
        logger.info('launch-queue-drain (dry-run): %d queued: %s',
                    len(files), [p.name for p in files])
        return 0

    result = drain_once()
    _emit_summary(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
