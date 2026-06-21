#!/usr/bin/env python3
"""launch_dedup_guard.py — a lightweight duplicate-work guard for the board
Launch path (``launch_queue_drain.py``).

WHY (the 2026-06-20 incident). The board Launch of project
``system-self-awareness-slice-1-state-log`` authored a build sequence for a
slice whose deliverables a SIBLING project's Forge build had *already* shipped
(PR #602, merged ~19s later). The redundant build found nothing to do
(byte-identical to ``main``) and stranded for 4h — a wasted ~$1.5 Forge run.
The tell of the underlying identity confusion: the sibling build emitted a
completion marker whose ``task_id`` was ``...slice-1-state-log`` while its
ENVELOPE ``task_id`` was ``...the-standing-brain`` — one build session shipped
work under a DIFFERENT task_id than its envelope, so the two projects'
deliverables overlapped without the system noticing.

WHAT this module provides — two cheap, fail-safe signals consulted at author
time, BEFORE ``launch_queue_drain`` authors a build sequence:

  (A) IN-FLIGHT SPEC OVERLAP. Another LIVE (pending/active) build sequence
      already targets the same ``spec_doc``. Advisory — building the same spec
      twice concurrently is *probably* redundant, but a phase legitimately
      sharing a spec doc must never be blocked, so this only surfaces an alert;
      the launch still proceeds.

  (B) DELIVERABLE CLAIM. A build session recently emitted a marker CLAIMING this
      phase's task_id while running under a *different* envelope task_id (the
      marker-task_id-mismatch class — the exact root of the incident). The
      ``outbox_notifier`` marker-error path records such a claim here via
      ``record_claim``; the launch drain reads it via ``find_matching_claims``.
      A claim whose ``claimed_task_id`` IS this phase's id is a high-confidence
      "the work is already being done elsewhere" signal, so the drain SKIPS
      authoring — but reversibly (it holds the queue file in ``.deduped/`` and
      alerts Larry with a one-line re-queue instruction), never a hard block.

DESIGN CONSTRAINTS (why this is a separate module, not inline in the drain):

  * PURE FILESYSTEM, stdlib only. NO ``subprocess``/``gh`` and NO writes to
    ``projects.json``. The launch drain is a contractually pure-filesystem
    non-committer (``test_launch_queue_drain``: ``test_module_imports_no_git`` +
    ``test_drain_never_writes_projects_json``); a guard it calls must keep that
    posture. gh-confirmed "already merged → reconcile phase to done" belongs in
    the advancer (which already shells to gh + writes the store) and is tracked
    as a follow-up, NOT done here.

  * FAIL-OPEN / FAIL-SAFE everywhere. Any read/parse error degrades to "no
    signal" (the launch proceeds). The guard can only ever ADD an alert or
    *reversibly* hold a launch; it can never crash the drain or permanently
    lose a launch request.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The blackboard ledger of cross-identity deliverable claims (one JSON object
# per line). Lives alongside the build-sequences under the same blackboard so a
# test that redirects the blackboard redirects this too.
CLAIMS_FILENAME = 'deliverable-claims.jsonl'

# Sequence statuses that count as "live" for the in-flight overlap check (a
# paused/complete/failed/archived sequence is NOT actively building anything).
# INTENTIONALLY duplicated from build_sequence_validator.LIVE_SEQUENCE_STATUSES
# rather than imported, to keep this guard dependency-free + pure (importing
# that module would pull `subprocess` in transitively). The sequence status
# enum is a spec-frozen contract; keep this in sync if it ever changes.
LIVE_SEQUENCE_STATUSES = frozenset({'pending', 'active'})

# A claim older than this is stale and ignored — the work it described has long
# since merged or failed, so it is no longer evidence about a fresh launch.
DEFAULT_CLAIM_WINDOW_SEC = 24 * 60 * 60

# Bound the ledger scan so an unrotated file can't make the guard read megabytes
# on every drain tick. The newest lines are what matter for a recency window.
MAX_CLAIM_LINES = 2000


def _agents_root() -> Path:
    """Env-overridable agents root — the SAME var the drain and the outbox
    notifier resolve, so all three agree on the blackboard path."""
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def claims_path(agents_root: Optional[Path] = None) -> Path:
    """The canonical claims ledger path (``<root>/blackboard/<file>``). Used by
    the outbox bridge (``record_claim``) and as the default for the drain."""
    root = agents_root or _agents_root()
    return root / 'blackboard' / CLAIMS_FILENAME


def claims_path_for_sequences_dir(sequences_dir: Path) -> Path:
    """The claims ledger that belongs to the blackboard holding
    ``sequences_dir`` (``<blackboard>/build-sequences`` → ``<blackboard>/<file>``).
    Lets the drain derive the ledger from the same dir it already scans, so a
    test pointing the drain at a tmp blackboard automatically gets a tmp ledger."""
    return sequences_dir.parent / CLAIMS_FILENAME


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _now_iso(now: Optional[datetime] = None) -> str:
    return _now(now).astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# claim recording (the outbox marker-error bridge calls this)
# --------------------------------------------------------------------------- #
def record_claim(
    *,
    claimed_task_id: Any,
    envelope_task_id: Any,
    agent: Optional[str] = None,
    target_repo: Optional[str] = None,
    source: Optional[str] = None,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Append one cross-identity deliverable claim to the ledger. Returns True
    iff a row was written.

    Called from the ``outbox_notifier`` marker-task_id-mismatch site: a build
    session emitted a marker whose ``task_id`` (``claimed_task_id``) differs from
    the envelope it was dispatched under (``envelope_task_id``). Recording the
    CLAIMED id is what lets a later launch of that same id recognise the work is
    already being done elsewhere.

    Best-effort and silent: a non-string ``claimed_task_id``, or any filesystem
    error, returns False without raising — recording a claim must NEVER perturb
    the notifier's marker-error handling."""
    if not isinstance(claimed_task_id, str) or not claimed_task_id.strip():
        return False
    ledger = path or claims_path()
    envelope = (
        envelope_task_id.strip()
        if isinstance(envelope_task_id, str) and envelope_task_id.strip()
        else None
    )
    record = {
        'ts': _now_iso(now),
        'claimed_task_id': claimed_task_id.strip(),
        'envelope_task_id': envelope,
        'agent': agent,
        'target_repo': target_repo,
        'source': source,
    }
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(record) + '\n')
        return True
    except OSError:
        return False


def _read_recent_claims(
    ledger: Path, *, window_sec: int, now: datetime,
) -> list[dict[str, Any]]:
    """Parse the newest ``MAX_CLAIM_LINES`` of the ledger and return the claims
    whose ``ts`` is within ``window_sec`` of ``now``. Fail-safe: a missing or
    unreadable ledger returns ``[]``; an unparseable / undated line is skipped."""
    try:
        lines = ledger.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    cutoff = now.timestamp() - max(0, window_sec)
    out: list[dict[str, Any]] = []
    for line in lines[-MAX_CLAIM_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts_raw = rec.get('ts')
        if not isinstance(ts_raw, str):
            continue
        try:
            # Accept a trailing 'Z' (fromisoformat rejects it pre-3.11).
            ts = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.timestamp() < cutoff:
            continue
        out.append(rec)
    return out


def _candidate_ids(entry: dict[str, Any]) -> set[str]:
    """The id forms a deliverable claim might name for THIS launch: the phase id,
    the sequence id, and the sequence id with its ``launch-`` prefix stripped (a
    claim may carry either the phase or the seq form).

    Deliberately does NOT include ``project_id``: a build's marker names a phase/
    step id, never the launching project, and matching the project id would widen
    the surface so a claim could hold an UNRELATED project that happens to reuse
    the slug as a phase id. For a single-phase project ``phase_id == project_id``
    anyway (``new_single_phase_project`` defaults ``phid = pid``), so the
    2026-06-20 incident is still caught by the phase id."""
    ids: set[str] = set()
    phase_id = entry.get('phase_id')
    seq_id = entry.get('seq_id') or (
        f'launch-{phase_id}' if isinstance(phase_id, str) and phase_id else None
    )
    for value in (phase_id, seq_id):
        if isinstance(value, str) and value:
            ids.add(value)
    if isinstance(seq_id, str) and seq_id.startswith('launch-'):
        stripped = seq_id[len('launch-'):]
        if stripped:
            ids.add(stripped)
    return ids


def find_matching_claims(
    entry: dict[str, Any],
    *,
    path: Optional[Path] = None,
    sequences_dir: Optional[Path] = None,
    window_sec: int = DEFAULT_CLAIM_WINDOW_SEC,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Recent deliverable claims whose ``claimed_task_id`` matches one of THIS
    launch's id forms (phase / project / sequence). A non-empty result means a
    build session already (recently) claimed to deliver this phase's work under a
    different envelope — the duplicate-work signal.

    ``path`` wins; else the ledger is derived from ``sequences_dir`` (test path);
    else the production default. Fail-safe (returns ``[]`` on any error)."""
    if path is not None:
        ledger = path
    elif sequences_dir is not None:
        ledger = claims_path_for_sequences_dir(sequences_dir)
    else:
        ledger = claims_path()
    candidates = _candidate_ids(entry)
    if not candidates:
        return []
    matches: list[dict[str, Any]] = []
    for rec in _read_recent_claims(ledger, window_sec=window_sec, now=_now(now)):
        claimed = rec.get('claimed_task_id')
        if isinstance(claimed, str) and claimed in candidates:
            matches.append(rec)
    return matches


# --------------------------------------------------------------------------- #
# in-flight spec overlap (a cheap, pure-filesystem scan of the sequences dir)
# --------------------------------------------------------------------------- #
def find_inflight_spec_overlap(
    entry: dict[str, Any],
    *,
    sequences_dir: Path,
    exclude_seq_id: Optional[str] = None,
) -> list[str]:
    """``seq_id``s of OTHER live (pending/active) build sequences that target the
    same ``spec_doc`` as this launch's ``spec_ref``. Empty when the launch has no
    spec_ref, when the dir is absent, or when nothing overlaps.

    Pure filesystem + fail-safe: an unreadable dir or a malformed sequence file
    is skipped, never raised. Building the identical spec concurrently is *likely*
    redundant — but a legitimately-distinct phase may share a spec doc, so the
    caller treats this as ADVISORY, not a hard skip."""
    spec_ref = entry.get('spec_ref')
    if not isinstance(spec_ref, str) or not spec_ref.strip():
        return []
    spec_ref = spec_ref.strip()
    if not sequences_dir.is_dir():
        return []
    own = exclude_seq_id or entry.get('seq_id') or (
        f'launch-{entry.get("phase_id")}'
    )
    out: list[str] = []
    for p in sorted(sequences_dir.iterdir()):
        if not (p.is_file() and p.suffix == '.json' and not p.name.startswith('.')):
            continue
        try:
            seq = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(seq, dict):
            continue
        seq_id = seq.get('seq_id')
        if not isinstance(seq_id, str) or seq_id == own:
            continue
        if seq.get('status') not in LIVE_SEQUENCE_STATUSES:
            continue
        seq_spec = seq.get('spec_doc')
        if isinstance(seq_spec, str) and seq_spec.strip() == spec_ref:
            out.append(seq_id)
    return out


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #
@dataclass
class DedupVerdict:
    """The guard's per-launch decision.

    ``action`` is ``'skip_duplicate'`` only on the high-confidence deliverable-
    claim signal — the launch should NOT be authored (the drain holds it
    reversibly + alerts). ``'proceed'`` means author normally; an in-flight spec
    overlap surfaces here as an advisory (``overlapping_seqs`` populated) WITHOUT
    flipping the action.

    ``reason`` is the one-line human explanation for the log + alert. The caller
    alerts whenever ``has_signal`` is true, regardless of action."""
    action: str  # 'proceed' | 'skip_duplicate'
    reason: str = ''
    claims: list[dict[str, Any]] = field(default_factory=list)
    overlapping_seqs: list[str] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        return bool(self.claims or self.overlapping_seqs)


def evaluate(
    entry: dict[str, Any],
    *,
    sequences_dir: Path,
    claims_ledger: Optional[Path] = None,
    window_sec: int = DEFAULT_CLAIM_WINDOW_SEC,
    now: Optional[datetime] = None,
) -> DedupVerdict:
    """Evaluate the duplicate-work signals for one launch entry.

    Precedence: a matching deliverable CLAIM → ``skip_duplicate`` (the work is
    already being done elsewhere under a different identity). Otherwise an
    in-flight spec overlap is recorded as an advisory and the action stays
    ``proceed``. With no signal, ``proceed`` and an empty reason.

    Pure over the filesystem it is pointed at + fail-safe per check; the caller
    additionally wraps this so any unexpected error fails OPEN (authors)."""
    claims = find_matching_claims(
        entry, path=claims_ledger, sequences_dir=sequences_dir,
        window_sec=window_sec, now=now,
    )
    overlaps = find_inflight_spec_overlap(entry, sequences_dir=sequences_dir)

    if claims:
        envelopes = sorted({
            c.get('envelope_task_id') for c in claims
            if isinstance(c.get('envelope_task_id'), str)
        })
        env_hint = (
            f' under envelope task_id(s) {envelopes}' if envelopes else ''
        )
        reason = (
            f'a recent build marker already claimed this phase\'s deliverables'
            f'{env_hint} (cross-identity marker-task_id mismatch) — the work is '
            f'already in flight elsewhere, so no duplicate build was authored.'
        )
        return DedupVerdict(
            action='skip_duplicate', reason=reason,
            claims=claims, overlapping_seqs=overlaps,
        )

    if overlaps:
        reason = (
            f'another live build sequence already targets the same spec '
            f'({entry.get("spec_ref")!r}): {overlaps}. Proceeding, but this may '
            f'be duplicate work — cancel one if redundant.'
        )
        return DedupVerdict(
            action='proceed', reason=reason, overlapping_seqs=overlaps,
        )

    return DedupVerdict(action='proceed')
