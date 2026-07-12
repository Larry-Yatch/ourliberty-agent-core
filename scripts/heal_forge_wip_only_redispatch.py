#!/usr/bin/env python3
"""heal_forge_wip_only_redispatch.py — auto-re-dispatch a Forge/Mirror build whose
session died leaving ONLY the ``[WIP][session-start]`` checkpoint commit.

THE FAILURE PATTERN (Pulse § G rule 'Forge-timeout-worktree-missing-retry-loop';
3 observed instances, latest 2026-06-24 task=reconcile-hardening-mission-shipped-001):
Forge is dispatched, ``worktree_manager.setup_branch_checkpoint`` pushes a
``[WIP][session-start] <stem>`` commit to ``forge/<task>``, then the session dies
mid-build (API overload / context limit / silent crash on a heavier task). The
branch tip stays the WIP-only commit: no build commit lands, no PR opens, and the
inbox file was already consumed (archived to
``~/agents/inboxes/forge/.archive/<task>.json``). Nothing auto-recovers this
signature today: ``heal_abandoned_inbox_tasks`` only recovers tasks whose inbox
file STILL exists; ``heal_phantom_dispatch_claim`` is detection-only;
``heal_pipeline_stall`` Check 1 (forge-built-no-PR) is alert-only by design AND
needs a terminal ``done success=True`` log line this failure never emits.

WHAT THIS HEALER DOES (poll-based, bounded, zero-LLM): every tick it enumerates
``forge/*`` and ``mirror/*`` dispatch branches across the canonical repos, finds
the ones that match the WIP-only / dead-session / no-PR / not-already-requeued
signature, and mechanically re-queues the SAME task ONCE by rebuilding a fresh
inbox envelope from the archived original. A durable ledger keyed by the original
task stem bounds auto-retries to ``MAX_AUTO_RETRIES`` (default 1); once exhausted
it fires exactly ONE loud Larry alert and never redispatches that family again.

MIRROR-REVIEW SUPPRESSION — a ``mirror/mirror-review-pr-<repo>-<N>`` branch is NOT
a build: the task REVIEWS PR #N and emits a verdict, never a PR/commit of its own,
so it ALWAYS trips the WIP-only / no-PR signature. When #N is already terminal
(merged or closed) the review is moot: the healer SUPPRESSES it (no redispatch, no
escalation) and records ``suppressed`` in the ledger so later ticks short-circuit
before re-probing gh. An OPEN or gh-UNKNOWN reviewed PR is left alone here — a
genuinely-dead open review is recovered by the orphaned-mirror-claims re-inject
path, not by this build-WIP healer (Mirror review #931 false 'exhausted', 2026-07-11).

PR-OPERATING SUPPRESSION (generalized) — mirror-review is one case of a broader
class: any task that operates on an EXISTING PR #N and opens none of its own also
always trips the WIP-only / no-PR signature. A ``rebase-pr-<N>`` / ``resolve-pr<N>``
task (Gate 0, ``_target_pr_coordinate``) rebases/resolves PR #N and emits no PR, so
after PR #N merges it too was WIP-retried + falsely escalated (``rebase-pr-860-001``
after #860 merged, 2026-07-11). Gate 0 now suppresses these the SAME way it
suppresses a mirror-review of a terminal PR — merged/closed target => suppress;
open/unknown => surface. The forge-no-PR stall path already guards rebase via
``heal_pipeline_stall._forge_rebase_target_shipped``; this closes the same gap in
the WIP-redispatch healer.

REJECT / NO-DELTA SUPPRESSION — a task Forge REJECTED at preflight ('no buildable
delta — already fully implemented') legitimately produces NO PR: its
``forge/<task>`` branch carries only the ``[WIP][session-start]`` checkpoint, so it
too trips the WIP-only / no-PR signature. Redispatching it is futile and never
terminates — every retry re-rejects (still no delta), leaving another WIP-only
branch (2026-07-11 ``auto-route-externally-authored-pr-reviews-001`` retry loop).
A rejected task is DONE, not a failed build: when the family's Forge dispatch
concluded with a durable ``preflight_reject`` chain_event (the notifier's per-task
terminal signal — ``outbox_notifier._emit_preflight_outcome_chain_event``; a
budget-exhausted clarify folds into the same type), the healer SUPPRESSES it (no
redispatch, no exhausted escalation) and records ``suppressed-task-rejected`` in
the ledger so later ticks short-circuit before re-querying chain_events. The
distinction from a genuine proceeded-then-died build is the terminal signal: a
build that PROCEEDED emits ``preflight_proceed`` (never ``preflight_reject``), so
its abandoned WIP-only branch is recovered/escalated exactly as before.

DETECTION SIGNATURE — a branch is a candidate only when ALL hold; any ambiguous
or indeterminate signal => SKIP, never redispatch:
  1. WIP-only: the branch tip adds NOTHING over its merge-base with origin/main
     (only the empty WIP commit). Classified by the SAME merge-base/changed-files
     classifier ``cleanup_dispatch_branches.compute_net_change`` ('empty-wip').
  2. Session ended: no live in-flight claim (``state/in-flight/<task>.json`` with
     a signalable pid).
  3. No open PR AND no merged PR whose head is this dispatch branch.
  4. No live (un-archived) inbox file already queued for this task family (covers
     the already-requeued case — e.g. a pending ``<task>-002`` manual retry).
  5. Branch tip older than ``GRACE_SECONDS`` (default 1800) so a live build is
     never raced.

MUST re-dispatch from the archived ORIGINAL envelope, never from reconstructed
chat prose. **Enforcement:** ``build_retry_envelope`` reads only
``inboxes/<agent>/.archive/<base>.json`` (``_find_archived_original``); if that
file is absent or ambiguous the candidate is SKIPPED (no synthesized envelope is
ever written) — exercised by ``test_skips_when_no_archived_original``.

MUST guarantee re-running NEVER double-dispatches the same family. **Enforcement:**
three independent idempotency gates — the durable ledger keyed by the retry-suffix-
stripped base stem (``_ledger_base``), the live-inbox family-root check
(``_live_inbox_family_roots``), and the retry-suffixed ``task_id`` itself — any one
of which turns the second tick into a no-op; covered by
``test_second_tick_is_noop_via_ledger`` and ``test_skips_when_retry_already_pending``.

NEVER fire a loud Larry alert unless the latest RETRY itself died WIP-only, and
never more than once per exhausted family. **Enforcement:** the escalate path in
``evaluate`` requires the candidate branch to BE the ledger's
``last_retry_task_id`` (the lingering original branch is skipped, so a healthy or
merged retry never triggers a false 'exhausted' DM), and the persisted
``escalated`` flag bounds it to one alert — covered by
``test_exhausted_escalates_exactly_once`` and
``test_no_false_escalation_on_lingering_original_after_redispatch``.

NEVER act when disabled or under test. **Enforcement:** ``main`` returns early on
``~/agents/healers.disabled``; the inbox write calls
``test_isolation_guard.refuse_under_test('inbox-write')`` first, so a test that
reaches the real write un-mocked fails loud rather than dispatching to production.

stdlib only (plus sibling repo modules).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Repo scripts dir on sys.path so sibling imports resolve when run by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cleanup_dispatch_branches as cdb  # noqa: E402  branch classifier + git/gh helpers
import task_terminal_state as tts  # noqa: E402  shared PR-coordinate parse + gh state
from test_isolation_guard import refuse_under_test  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
INBOXES_ROOT = AGENTS_ROOT / 'inboxes'
IN_FLIGHT_DIR = AGENTS_ROOT / 'state' / 'in-flight'
LEDGER_PATH = AGENTS_ROOT / 'state' / 'forge_wip_redispatch_ledger.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal_forge_wip_only_redispatch.log'

# Both agents that create a pushed WIP checkpoint via the inbox_watcher worktree
# path (worktree_enabled in agent-models.json). Mirrors cleanup_dispatch_branches.
DISPATCH_AGENTS = ('forge', 'mirror')

# Branch tip must be older than this before we touch it, so a live build that
# just pushed its WIP checkpoint (and is still grinding) is never raced. 30 min
# comfortably exceeds the checkpoint→first-build-commit gap of a healthy build.
GRACE_SECONDS = 1800

# Auto-retries per ORIGINAL task family. 1 means: one mechanical re-queue, then
# escalate to Larry. The ledger enforces this across ticks and across the whole
# retry chain (the base stem is retry-suffix-stripped).
MAX_AUTO_RETRIES = 1

ALERT_SOURCE = 'forge-wip-redispatch'

# A healer-minted retry suffix: `<base>-retry<N>`. Stripped to recover the base
# family stem so the ledger bounds the whole chain, not each retry separately.
_RETRY_SUFFIX_RE = re.compile(r'-retry\d+$')
# A trailing numeric counter (`-001`, `-002`) — the manual-requeue convention.
# Used ONLY for the "already requeued" dedup family-root, never for ledger keys.
_NUMERIC_SUFFIX_RE = re.compile(r'-\d+$')


# ---------- logging ----------


def _log_path() -> Path:
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    if override:
        return Path(override) / 'heal_forge_wip_only_redispatch.log'
    return LOG_FILE


def log(level: str, msg: str) -> None:
    line = f'[{datetime.now(timezone.utc).isoformat()}] [{level}] {msg}'
    print(line, flush=True)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        # A full/read-only log FS must never crash the healer.
        pass


# ---------- task-id / family helpers ----------


def _ledger_base(stem: str) -> str:
    """The ledger family key: the task stem with a healer retry suffix stripped.
    ``foo-001`` -> ``foo-001``; ``foo-001-retry1`` -> ``foo-001``. Keying on this
    makes the retry chain share ONE ledger entry, so MAX_AUTO_RETRIES bounds the
    whole family rather than resetting on each generated retry branch."""
    return _RETRY_SUFFIX_RE.sub('', stem)


def _family_root(stem: str) -> str:
    """The broad dedup root used to recognize an already-pending requeue of the
    SAME work: strip a healer retry suffix AND a trailing numeric counter.
    ``reconcile-...-001`` and ``reconcile-...-002`` and
    ``reconcile-...-001-retry1`` all collapse to ``reconcile-...``. Deliberately
    generous — a false match here only makes us SKIP (never redispatch), the safe
    direction per the 'any ambiguous signal => SKIP' doctrine."""
    return _NUMERIC_SUFFIX_RE.sub('', _RETRY_SUFFIX_RE.sub('', stem))


def _task_from_branch(branch: str) -> Optional[str]:
    """Sanitized task stem from a ``<agent>/<stem>`` dispatch branch, or None."""
    for agent in DISPATCH_AGENTS:
        prefix = f'{agent}/'
        if branch.startswith(prefix):
            return branch[len(prefix):]
    return None


def _reviewed_pr_coordinate(branch_stem: str) -> Optional[tuple[str, int]]:
    """(qualified_repo, pr_number) of the EXTERNAL PR a mirror-review task reviews,
    or None when the stem is not a mirror-review coordinate.

    A mirror-review dispatch branch is ``mirror/mirror-review-pr-<repo>-<N>`` (with
    an optional healer ``-retry<k>`` suffix). Such a task reviews PR #N and by
    nature produces NO PR/commit of its own, so it ALWAYS matches the WIP-only /
    no-PR build-failure signature — a false positive this healer must not treat as
    an abandoned build. We strip the retry suffix first (``_ledger_base``) because
    ``parse_pr_coordinate``'s numeric-tail anchor rejects a non-hex ``-retry<k>``
    tail; the retry-stripped ``mirror-review-pr-<repo>-<N>`` parses cleanly."""
    return tts.parse_pr_coordinate(_ledger_base(branch_stem))


# Action-verb prefixes a PR-OPERATING task id can carry: a ``rebase-``/``resolve-``
# task operates on an EXISTING PR and — exactly like a mirror-review — opens no PR
# of its own, so it always trips the WIP-only / no-PR signature and must be treated
# as terminal-by-design (not an abandoned build) when its target PR is merged/closed.
_PR_OP_PREFIXES = ('rebase-', 'resolve-')

# A ``pr<sep><digits>`` coordinate token: ``pr-860``, ``pr860``, ``pr-860-001``,
# ``pr252-digest-generator``. Anchored on a token boundary (start or ``-``) so a
# stray ``pr`` inside a word never yields a bogus number; the trailing boundary
# (``-`` or end) lets the target number precede a counter/descriptor tail.
_PR_NUM_TOKEN_RE = re.compile(r'(?:^|-)pr-?(\d+)(?:-|$)')


def _extract_target_pr_number(core: str) -> Optional[int]:
    """The target PR number from a repo-less rebase/resolve core (action verb and
    ``-retry<k>`` already stripped by the caller). A ``pr<sep><digits>`` token wins
    (``pr-860``, ``pr860``, ``pr252-digest-generator-001``); otherwise the trailing
    number after a manual ``-<counter>`` suffix is dropped
    (``...-mergeable-860-001`` -> 860). None when the core names no number.

    Mirrors the digit-extraction convention ``heal_pipeline_stall._forge_rebase_
    target_shipped`` uses (``re.findall(r'\\d+', task_id)``), but resolves the
    counter-vs-target ambiguity structurally from the id alone — the ``pr`` anchor,
    then a single trailing-counter strip — because this Gate does a direct
    single-PR probe rather than intersecting against the fetched PR set."""
    m = _PR_NUM_TOKEN_RE.search(core)
    if m:
        return int(m.group(1))
    trimmed = _NUMERIC_SUFFIX_RE.sub('', core)  # drop a trailing -<counter>
    m2 = re.search(r'(\d+)$', trimmed)
    return int(m2.group(1)) if m2 else None


def _target_pr_coordinate(branch_stem: str,
                          repo: Optional[Path] = None) -> Optional[tuple[str, int]]:
    """(qualified_repo, pr_number) of the EXISTING PR a PR-operating task targets,
    or None when the stem names no such PR (a genuine build).

    Generalizes the former mirror-review-only Gate 0 parser to every task shape
    that operates on an existing PR and opens none of its own, so all of them are
    recognized as terminal-by-design when that PR is already merged/closed:
      * ``mirror-review-pr-<repo>-<N>`` — unchanged; one case of the generalization
        (delegated verbatim to ``_reviewed_pr_coordinate``),
      * ``rebase-pr-<N>`` / ``rebase-pr-<repo>-<N>`` / ``rebase-pr-<N>-<counter>`` and
        rebase ids that embed the target number
        (``rebase-forge-post-open-mergeable-<N>-001``, ``rebase-pr252-...-001``),
      * ``resolve-pr<N>`` / ``resolve-pr-<repo>-<N>``,
    all tolerant of a trailing ``-retry<k>`` suffix (stripped via ``_ledger_base``).

    Repo resolution: the ``pr-<repo>-<N>`` shapes embed the repo (resolved by the
    shared ``parse_pr_coordinate``); the repo-less shapes fall back to ``repo`` — the
    canonical checkout the dispatch branch lives in, which IS the repo a
    rebase/resolve task targets. A wrong guess is safe: the downstream
    ``pr_coordinate_state`` probe then returns UNKNOWN (gh miss), which never
    suppresses — so at worst this preserves today's behavior, never a false
    suppress of a still-open target."""
    # Mirror-review keeps its exact prior parse.
    coord = _reviewed_pr_coordinate(branch_stem)
    if coord is not None:
        return coord

    stem = _ledger_base(branch_stem)
    prefix = next((p for p in _PR_OP_PREFIXES if stem.startswith(p)), None)
    if prefix is None:
        return None
    core = stem[len(prefix):]

    # ``rebase-pr-<repo>-<N>`` / ``resolve-pr-<repo>-<N>``: once the action verb is
    # stripped, the remainder is the shared repo-anchored coordinate shape.
    embedded = tts.parse_pr_coordinate(core)
    if embedded is not None:
        return embedded

    number = _extract_target_pr_number(core)
    if number is None or repo is None:
        return None
    qualified = tts._qualify_repo(repo.name)
    if not qualified:
        return None
    return qualified, number


# ---------- reject / no-delta terminal-signal lookup ----------


def _chain_events_has_preflight_reject(task_ids: set[str]) -> Optional[bool]:
    """True if chain_events records a Forge ``preflight_reject`` for ANY id in
    ``task_ids``; False if reachable and none found; None on infrastructure
    failure (missing Supabase env, import error, query exception).

    ``preflight_reject`` is the durable per-task terminal signal the outbox
    notifier emits for a Forge preflight REJECT / no-buildable-delta (a
    budget-exhausted clarify folds into the same event type) — keyed on
    ``task_id``. Reusing it satisfies the 'do not invent a parallel reject store'
    constraint. None is the FAILSAFE: the caller then behaves exactly as today
    (redispatch bounded by the ledger to MAX_AUTO_RETRIES + one escalation), so a
    transient Supabase outage costs at most the normal one retry, never an
    unbounded loop. Lazy import mirrors the heal_pipeline_stall canonical
    pattern — keeps tests mock-friendly and avoids a hard SDK dependency at
    module load."""
    ids = sorted(t for t in task_ids if t)
    if not ids:
        return False
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        return None
    try:
        from supabase_factory import get_supabase_client  # type: ignore
        client = get_supabase_client(url, key)
        res = (
            client.table('chain_events')
                  .select('event_type,task_id')
                  .in_('task_id', ids)
                  .eq('event_type', 'preflight_reject')
                  .limit(1)
                  .execute()
        )
        rows = getattr(res, 'data', None) or []
        return len(rows) > 0
    except Exception as exc:  # noqa: BLE001 — best-effort; None preserves behavior
        log('WARN', f'chain_events preflight_reject query failed for {ids}: '
                    f'{type(exc).__name__}: {exc}')
        return None


def _task_family_rejected(agent: str, branch_stem: str, base: str) -> bool:
    """True if this task family's Forge dispatch concluded with a preflight
    REJECT / no-buildable-delta (a ``preflight_reject`` chain_event).

    Probes the branch's sanitized stem, the retry-stripped ``base``, AND the
    archived original's REAL ``task_id`` — the dispatch branch carries the
    WORKTREE-sanitized stem (lowercased, non-alnum -> '-', truncated to 50),
    which can diverge from the true id the notifier keyed the event on, so we
    recover that id from the archived envelope. False on infrastructure failure
    (``_chain_events_has_preflight_reject`` returns None) so genuine build-failure
    recovery stays the failsafe."""
    candidates = {branch_stem, base}
    original_id = _resolve_original_task_id(agent, branch_stem)
    if original_id:
        candidates.add(original_id)
        candidates.add(_ledger_base(original_id))
    return bool(_chain_events_has_preflight_reject(candidates))


# ---------- archived-original lookup (source of truth for re-dispatch) ----------


def _resolve_original_task_id(agent: str, branch_stem: str) -> Optional[str]:
    """The real (un-sanitized) ``task_id`` of the archived original for a
    candidate branch, or None. Recovered from the archived envelope's ``task_id``
    field because branch sanitization can diverge from the true id the notifier
    keyed the ``preflight_reject`` chain_event on."""
    path = _find_archived_original(agent, branch_stem)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tid = data.get('task_id')
    return str(tid) if tid else None


def _find_archived_original(agent: str, branch_stem: str) -> Optional[Path]:
    """Locate the archived original envelope for a candidate branch.

    The dispatch branch name carries the WORKTREE-sanitized task stem (chars
    mapped to '-', truncated to 50), which may not equal the original task_id
    byte-for-byte. So we match by re-sanitizing each archived filename's stem the
    SAME way (cdb._sanitize_task_id) and comparing to the branch's stem. Returns
    the unique match, or None when there is no match OR more than one (ambiguous
    => caller SKIPs — we never guess which original to re-dispatch)."""
    archive = INBOXES_ROOT / agent / '.archive'
    if not archive.is_dir():
        return None
    matches = [
        f for f in archive.glob('*.json')
        if cdb._sanitize_task_id(f.stem) == branch_stem
    ]
    if len(matches) == 1:
        return matches[0]
    return None


# ---------- live-state gates ----------


def _has_live_in_flight(stems: list[str]) -> bool:
    """True if any of ``stems`` has a live in-flight registry entry (a signalable
    pid). agent_runner keys state/in-flight/<task_stem>.json on the raw task_id,
    so we check both the branch (sanitized) stem and the original stem."""
    if not IN_FLIGHT_DIR.is_dir():
        return False
    for stem in stems:
        if not stem:
            continue
        f = IN_FLIGHT_DIR / f'{stem}.json'
        try:
            entry = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict) and cdb._pid_alive(entry.get('pid')):
            return True
    return False


def _live_inbox_family_roots(agent: str) -> set[str]:
    """Family roots (_family_root) of every LIVE top-level inbox task for ``agent``
    — the set of work already queued. A candidate whose family root is in here is
    already being retried (by a human, the abandoned-inbox healer, or a prior tick
    of this one) and MUST NOT be piled on. Scans only ``*.json`` at the top level;
    .archive/ and .invalid/ (terminal) are correctly excluded."""
    roots: set[str] = set()
    inbox = INBOXES_ROOT / agent
    if not inbox.is_dir():
        return roots
    for f in inbox.glob('*.json'):
        try:
            body = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            # Unreadable -> use the filename stem so we still dedup conservatively.
            roots.add(_family_root(f.stem))
            continue
        task_id = body.get('task_id') if isinstance(body, dict) else None
        roots.add(_family_root(str(task_id) if task_id else f.stem))
    return roots


# ---------- ledger ----------


def load_ledger() -> dict:
    try:
        data = json.loads(LEDGER_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_ledger(ledger: dict) -> None:
    """Atomic tmp+os.replace write so a crash mid-write never corrupts the ledger
    (a corrupt ledger reads as empty and would reset every family's attempt
    count — the one way this healer could double-dispatch)."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LEDGER_PATH.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
        os.replace(tmp, LEDGER_PATH)
    except OSError as exc:
        log('ERROR', f'ledger save failed: {exc}')
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------- re-dispatch envelope ----------

# Envelope fields preserved verbatim from the archived original. phase is forced
# to 'build' and task_id is replaced with the retry-suffixed id; everything else
# the build needs is carried through unchanged.
_PRESERVE_FIELDS = (
    'prompt', 'summary', 'target_agent', 'target_repo', 'task_type',
    'pr_title', 'changed_files', 'source', 'reply_chat_id',
)


def build_retry_envelope(original: dict, retry_task_id: str) -> dict:
    env: dict = {'task_id': retry_task_id}
    for key in _PRESERVE_FIELDS:
        if key in original:
            env[key] = original[key]
    env['phase'] = 'build'
    env['redispatch_of'] = original.get('task_id')
    return env


def _atomic_write_envelope(dest: Path, envelope: dict) -> None:
    """tmp+os.replace write into the agent inbox. Guarded: a test that reaches
    this un-mocked trips refuse_under_test rather than writing to the live tree."""
    refuse_under_test('inbox-write')
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(envelope, f, indent=2)
    os.replace(tmp, dest)


# ---------- alerting (actionable-only) ----------


def _emit_alert(route: str, severity: str, message: str, subject: str) -> None:
    """One best-effort larry_alerts line. route='digest' for routine auto-redispatch
    (daily CEO digest, never a DM); route='escalate' only for retries-exhausted."""
    try:
        import larry_alerts
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        log('WARN', f'larry_alerts unavailable: {exc}')
        return
    larry_alerts.append_alert(
        source=ALERT_SOURCE, severity=severity, message=message,
        subject=subject, route=route,
    )


# ---------- per-candidate decision ----------


@dataclass
class Candidate:
    repo: Path
    agent: str
    branch: str          # short, no origin/ (e.g. 'forge/foo-001')
    branch_stem: str     # sanitized task stem from the branch
    committer_ts: int


def _open_and_merged_heads(repo: Path) -> Optional[tuple[set[str], set[str]]]:
    """(open_heads, merged_heads) for ``repo`` via gh, or None if the OPEN-PR list
    is unavailable. None is a hard stop for the repo: without it we cannot prove a
    branch is not still an open PR head, so we refuse to redispatch anything there."""
    open_heads = cdb._gh_pr_heads(repo, 'open')
    if open_heads is None:
        return None
    merged_heads = cdb._gh_pr_heads(repo, 'merged') or set()
    return open_heads, merged_heads


def evaluate(cand: Candidate, now: float, open_heads: set[str],
             merged_heads: set[str], ledger: dict) -> tuple[str, str]:
    """Pure-ish decision for one candidate. Returns (action, reason) where action
    is 'redispatch' | 'escalate' | 'skip'. All git/gh/inbox I/O is resolved by the
    caller except the cheap inbox/in-flight/archive lookups (read-only)."""
    base = _ledger_base(cand.branch_stem)

    # Gate 5 (age): never race a live build that just pushed its checkpoint.
    if (now - cand.committer_ts) < GRACE_SECONDS:
        return 'skip', f'too-young (<{GRACE_SECONDS}s)'

    # Gate 0 (PR-operating task): a task that operates on an EXISTING PR #N and
    # emits no PR of its own — a `mirror-review-pr-<repo>-<N>` verdict, or a
    # `rebase-`/`resolve-` op on PR #N — always trips the WIP-only / no-PR signature
    # below. When that target PR is already terminal (merged or closed) the op is
    # moot: suppress it (never redispatch, never escalate) and record the
    # suppression in the ledger so later ticks short-circuit without re-probing gh.
    # An OPEN or UNKNOWN (gh error) target PR is left alone here — a genuinely-dead
    # open review is recovered by the orphaned-mirror-claims re-inject path, and a
    # genuine rebase/resolve failure on a still-open PR must still surface — so the
    # conservative posture (only MERGED/CLOSED suppresses) is unchanged.
    coord = _target_pr_coordinate(cand.branch_stem, cand.repo)
    if coord is not None:
        if (ledger.get(base) or {}).get('suppressed'):
            return 'skip', 'target-pr-terminal (already suppressed)'
        pr_repo, pr_number = coord
        state, _ = tts.pr_coordinate_state(pr_repo, pr_number)
        if state in (tts.MERGED, tts.CLOSED):
            return 'suppress', (f'target-pr-terminal ({pr_repo}#{pr_number} '
                                f'{state})')
        return 'skip', (f'PR-operating task on {pr_repo}#{pr_number} '
                        f'(state={state}) — not a build-WIP candidate')

    # Gate 0b (already-suppressed): a family already recorded as a terminal
    # reject/no-delta (or, defensively, any other suppressed reason) short-
    # circuits here so later ticks never re-query chain_events — idempotent
    # suppression. Mirror-review suppressions returned above (coord != None), so
    # only a reject-suppressed forge build reaches this line.
    if (ledger.get(base) or {}).get('suppressed'):
        reason = (ledger.get(base) or {}).get('suppress_reason', 'suppressed')
        return 'skip', f'already suppressed ({reason})'

    # Gate 3: an open or merged PR for this branch means the work is not abandoned.
    if cand.branch in open_heads:
        return 'skip', 'open-PR head'
    if cand.branch in merged_heads:
        return 'skip', 'merged-PR head'

    # Gate 2: a live session is still building this task.
    if _has_live_in_flight([cand.branch_stem, base]):
        return 'skip', 'live in-flight session'

    # Gate 4: already requeued (human, abandoned-inbox healer, or a prior tick).
    root = _family_root(cand.branch_stem)
    if root in _live_inbox_family_roots(cand.agent):
        return 'skip', 'already-requeued (live inbox family member)'

    # Gate 6 (reject / no-delta terminal): this branch passed every abandoned-
    # build gate (WIP-only, no PR, no live session, not requeued) — but if the
    # family's Forge dispatch concluded with a preflight REJECT, `no PR` is the
    # CORRECT terminal outcome, not a failed build. Retrying re-rejects forever
    # (2026-07-11 auto-route-... loop). Suppress permanently; a genuine
    # proceeded-then-died build emits `preflight_proceed` (never
    # `preflight_reject`) and falls through to redispatch unchanged.
    if _task_family_rejected(cand.agent, cand.branch_stem, base):
        return ('suppress-rejected',
                'family concluded with a Forge preflight REJECT (no buildable delta)')

    # Ledger: bound auto-retries per family.
    entry = ledger.get(base) or {}
    attempts = int(entry.get('attempts', 0) or 0)
    if attempts >= MAX_AUTO_RETRIES:
        if entry.get('escalated'):
            return 'skip', 'retries-exhausted (already escalated)'
        # Exhaustion keys on the latest RETRY's outcome, NOT the original
        # branch's persistence. After a successful redispatch the original
        # forge/<base> branch stays empty-wip until branch GC (48h), and its
        # PR/in-flight live under the retry id (forge/<base>-retryN), so it
        # passes gates 2-4 here every tick — escalating off it would fire a
        # FALSE 'exhausted' DM while the retry is healthily building or already
        # merged (Mirror review #693, rev 1). So only the retry's OWN abandoned
        # branch may escalate: gates 2-4 above already proved THIS candidate is
        # WIP-only / no-PR / not-in-flight / not-queued, so when the candidate
        # IS the retry branch the retry is genuinely abandoned. The lingering
        # original (or any earlier retry) is skipped; if the latest retry also
        # dies WIP-only its own branch surfaces and escalates exactly once.
        last_retry = entry.get('last_retry_task_id')
        if last_retry and cand.branch_stem != last_retry:
            return 'skip', 'superseded by active retry (original branch lingering)'
        return 'escalate', f'retries-exhausted (attempts={attempts})'

    return 'redispatch', 'wip-only abandoned dispatch'


# ---------- per-repo sweep ----------


@dataclass
class SweepCounts:
    scanned: int = 0
    redispatched: int = 0
    escalated: int = 0
    suppressed: int = 0
    skipped: int = 0


def _collect_candidates(repo: Path) -> list[Candidate]:
    """WIP-only candidate branches in ``repo`` across both scopes. The checkpoint
    is pushed to origin, so origin/<branch> is authoritative; local is included
    so a droplet that still has the local ref is covered too. Dedup by branch
    name (a branch present in both scopes is one candidate)."""
    cdb.fetch_prune(repo)
    seen: dict[str, Candidate] = {}
    for scope in ('remote', 'local'):
        for name, ts in cdb.list_candidate_branches(repo, scope):
            stem = _task_from_branch(name)
            if not stem or name in seen:
                continue
            if cdb.compute_net_change(repo, name, scope) != 'empty-wip':
                continue
            agent = name.split('/', 1)[0]
            seen[name] = Candidate(repo=repo, agent=agent, branch=name,
                                   branch_stem=stem, committer_ts=ts)
    return list(seen.values())


def _do_redispatch(cand: Candidate, ledger: dict) -> bool:
    """Rebuild + write the retry envelope and record the attempt. Returns True on a
    successful write. Dedup-by-base ledger: the retry id is `<base>-retry<N>`."""
    base = _ledger_base(cand.branch_stem)
    original_path = _find_archived_original(cand.agent, cand.branch_stem)
    if original_path is None:
        log('SKIP', f'{cand.branch}: no unambiguous archived original — not '
                    f'synthesizing an envelope')
        return False
    try:
        original = json.loads(original_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log('SKIP', f'{cand.branch}: archived original unreadable: {exc}')
        return False
    if not isinstance(original, dict):
        log('SKIP', f'{cand.branch}: archived original is not an object')
        return False

    entry = ledger.get(base) or {}
    attempts = int(entry.get('attempts', 0) or 0)
    retry_task_id = f'{base}-retry{attempts + 1}'
    envelope = build_retry_envelope(original, retry_task_id)

    dest = INBOXES_ROOT / cand.agent / f'{retry_task_id}.json'
    if dest.exists():
        # Belt-and-suspenders: never overwrite an existing queued retry.
        log('SKIP', f'{cand.branch}: retry envelope {dest.name} already exists')
        return False
    try:
        _atomic_write_envelope(dest, envelope)
    except OSError as exc:
        log('ERROR', f'{cand.branch}: redispatch write failed: {exc}')
        return False

    ledger[base] = {
        'attempts': attempts + 1,
        'last_attempt_ts': datetime.now(timezone.utc).isoformat(),
        'last_retry_task_id': retry_task_id,
        'branch': cand.branch,
        'escalated': bool(entry.get('escalated', False)),
    }
    log('HEALED', f'{cand.branch}: re-dispatched as {cand.agent}/{retry_task_id} '
                  f'(attempt {attempts + 1}/{MAX_AUTO_RETRIES})')
    _emit_alert('digest', 'info',
                f'Auto-re-dispatched WIP-only abandoned {cand.agent} build '
                f'{cand.branch} as {retry_task_id} '
                f'(attempt {attempts + 1}/{MAX_AUTO_RETRIES}).',
                subject=base)
    return True


def _do_escalate(cand: Candidate, ledger: dict) -> None:
    """Fire the ONE loud retries-exhausted alert and mark the family escalated so
    no later tick repeats it."""
    base = _ledger_base(cand.branch_stem)
    entry = ledger.get(base) or {}
    entry['escalated'] = True
    entry.setdefault('attempts', MAX_AUTO_RETRIES)
    entry['last_escalation_ts'] = datetime.now(timezone.utc).isoformat()
    entry['branch'] = cand.branch
    ledger[base] = entry
    log('ESCALATE', f'{cand.branch}: auto-recovery exhausted '
                    f'(attempts={entry.get("attempts")}); escalating to Larry')
    _emit_alert('escalate', 'critical',
                f'Forge WIP-only auto-recovery EXHAUSTED for {base} '
                f'(branch {cand.branch}): {MAX_AUTO_RETRIES} auto-retry already '
                f'died WIP-only with no PR. Manual investigation needed — the '
                f'task keeps dying mid-build before any commit lands.',
                subject=base)


def _do_suppress(cand: Candidate, ledger: dict, reason: str,
                 suppress_reason: str) -> None:
    """Record a task whose no-PR outcome is TERMINAL-BY-DESIGN as suppressed, so
    it never redispatches, never escalates, and later ticks short-circuit before
    any re-probe. Two classes use this:
      * ``suppressed-reviewed-pr-terminal`` — a mirror-review whose reviewed PR is
        already merged/closed (the review is moot); and
      * ``suppressed-task-rejected`` — a Forge dispatch that concluded with a
        preflight REJECT / no-buildable-delta (a rejected task is DONE, not a
        failed build).
    Deliberately carries NO ``attempts`` count — the escalate path keys on
    attempts>=MAX, which a suppressed entry can never reach, so a suppressed task
    can never contribute to an 'exhausted' alert."""
    base = _ledger_base(cand.branch_stem)
    entry = ledger.get(base) or {}
    entry['suppressed'] = True
    entry['suppress_reason'] = suppress_reason
    entry['last_suppress_ts'] = datetime.now(timezone.utc).isoformat()
    entry['branch'] = cand.branch
    ledger[base] = entry
    log('SUPPRESSED', f'{cand.branch}: {reason} — no redispatch, no escalation')


def sweep_repo(repo: Path, ledger: dict, now: float) -> SweepCounts:
    counts = SweepCounts()
    if not repo.exists():
        log('WARN', f'canonical repo missing, skipping: {repo}')
        return counts

    heads = _open_and_merged_heads(repo)
    if heads is None:
        log('SKIP', f'repo {repo}: open-PR list unavailable (gh) — refusing to '
                    f'redispatch this repo this tick')
        return counts
    open_heads, merged_heads = heads

    # One base family acted on at most once per tick (dedup original + retry
    # branches of the same family that may both be present).
    acted_bases: set[str] = set()
    for cand in _collect_candidates(repo):
        counts.scanned += 1
        base = _ledger_base(cand.branch_stem)
        if base in acted_bases:
            continue
        action, reason = evaluate(cand, now, open_heads, merged_heads, ledger)
        if action == 'redispatch':
            if _do_redispatch(cand, ledger):
                counts.redispatched += 1
                acted_bases.add(base)
            else:
                counts.skipped += 1
        elif action == 'escalate':
            _do_escalate(cand, ledger)
            counts.escalated += 1
            acted_bases.add(base)
        elif action == 'suppress':
            _do_suppress(cand, ledger, reason, 'suppressed-reviewed-pr-terminal')
            counts.suppressed += 1
            acted_bases.add(base)
        elif action == 'suppress-rejected':
            _do_suppress(cand, ledger, reason, 'suppressed-task-rejected')
            counts.suppressed += 1
            acted_bases.add(base)
        else:
            counts.skipped += 1
            log('SKIP', f'{cand.branch}: {reason}')
    return counts


# ---------- main ----------


def main(argv: Optional[list[str]] = None) -> int:
    if KILL_SWITCH.exists():
        log('KILLED_BY_SWITCH', 'healers.disabled present, exiting')
        return 0

    now = datetime.now(timezone.utc).timestamp()
    try:
        repos = cdb.load_canonical_repos()
    except RuntimeError as exc:
        log('ERROR', f'cannot load canonical repos: {exc}')
        return 0

    ledger = load_ledger()
    total = SweepCounts()
    for repo in repos:
        c = sweep_repo(repo, ledger, now)
        total.scanned += c.scanned
        total.redispatched += c.redispatched
        total.escalated += c.escalated
        total.suppressed += c.suppressed
        total.skipped += c.skipped

    if total.redispatched or total.escalated or total.suppressed:
        save_ledger(ledger)

    log('HEARTBEAT',
        f'scanned={total.scanned} redispatched={total.redispatched} '
        f'escalated={total.escalated} suppressed={total.suppressed} '
        f'skipped={total.skipped} '
        f'grace_s={GRACE_SECONDS} max_retries={MAX_AUTO_RETRIES}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
