#!/usr/bin/env python3
"""decision_outcome_reconcile.py — join the team's build result onto the
decisions Larry made (Operator Feed Loop slice 2).

The decision-outcome ledger (decision_outcome_ledger.py) records every
approve/reject Larry makes, keyed by canonical decision key. For the govern loop
to learn where his approvals turn out WELL, each decision needs its downstream
build result attached: did the PR that decision authorized merge, merge-then-
regress, or get closed unmerged?

This reconciler is that join. It is deliberately a RECONCILING PASS, not a hook
on the merge/verdict hot path:

  * It reads the ledger for decisions whose canonical key is a PR COORDINATE
    (`pr-<repo>-<n>`) and that do not yet have a build_outcome row.
  * For each, it asks GitHub the ground truth (`gh pr view`) — the actual,
    authoritative merge state — rather than trusting an intermediate event bus.
  * Terminal states (merged / merged-then-regressed / closed-unmerged) are
    appended to the ledger as a build_outcome row keyed by the same decision
    key. A still-open PR is left `pending` — recorded nothing, re-checked next
    pass. This tolerates the gap between "Larry approved now" and "the PR merges
    hours later".

Design contract (mirrors the ledger's own discipline):
  * Stdlib + `gh` CLI only. Never raises — a reconcile pass that hits a bad row
    or a gh hiccup logs and moves on; the still-unrecorded decision is simply
    retried next pass. Idempotent via `has_build_outcome` (one terminal row per
    decision).
  * The GitHub call is injected as `runner` so tests never shell out.
  * Decisions with a NON-PR key (a bare task_id — the dispatch-approval class
    whose PR is not directly encoded in the key) are resolved via the shared
    `task_terminal_state` kernel (probes `gh pr list`, matches task_ids against
    PR titles/branches; PR-only by design). UNKNOWN => KEEP: an UNKNOWN or OPEN
    probe records nothing and the key stays pending — never a fabricated
    terminal. The terminal-state lookup is injected as `terminal_state_fn` so
    tests never shell out.

TIMER + LOCK: wired to a systemd timer (30-min cadence, `--once`). Because a
timer can fire a second pass while one is running, the pass takes a NON-BLOCKING
file lock (`fcntl.flock` under ~/agents/state) up front — a held lock means
another pass owns this tick, so this invocation logs and exits 0 WITHOUT writing
(it never queues; the cadence retries in 30 min). Run manually via
`python3 decision_outcome_reconcile.py --once`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import decision_outcome_ledger as dol  # noqa: E402
import task_terminal_state as tts  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
LOG_FILE = AGENTS_ROOT / 'logs' / 'decision-outcome-reconcile.log'
# Single-writer lock: a timer can fire a second pass mid-run; the lock makes the
# join concurrency-safe. Held-lock => log + exit 0 (never queue).
LOCK_FILE = AGENTS_ROOT / 'state' / 'decision-outcome-reconcile.lock'

# Repos with active dispatch chains — full `owner/repo` slugs. Mirrors
# heal_pr_auto_merge.REPOS / heal_pipeline_stall.REPOS (the codebase keeps this
# list per-module rather than importing, to avoid pulling a heavy healer into a
# lightweight tool). The canonical decision key drops the owner
# (`<owner>/<repo>` -> `pr-<repo>-<n>`), so we resolve the bare repo name back to
# its registered slug HERE rather than assuming an owner — an unknown repo is
# SKIPPED (never queried under a guessed owner, which could hit a same-named repo
# under the wrong account and record a wrong outcome).
REPOS = [
    'Larry-Yatch/ourliberty-agent-core',
    'Larry-Yatch/ourliberty-dashboard',
    'Larry-Yatch/RSDPM',
]


def resolve_repo_slug(bare_repo: str) -> Optional[str]:
    """`ourliberty-agent-core` -> `Larry-Yatch/ourliberty-agent-core` by matching
    the registered REPOS allowlist on the repo-name suffix (mirrors
    heal_pipeline_stall._forge_pr_task_id_resolved). Returns None for a repo not
    in the registry — the caller skips it rather than guessing an owner."""
    if not isinstance(bare_repo, str) or not bare_repo:
        return None
    return next((r for r in REPOS if r.split('/', 1)[-1] == bare_repo), None)
# A merged PR carrying this label failed post-merge production verification
# (post_merge_verifier.py re-opens the issue + adds it) — "shipped but bad".
_REGRESSION_LABEL = 'regression'
# `pr-<repo>-<n>`: repo may contain dashes; the PR number is the trailing digits.
_PR_COORD_RE = re.compile(r'^pr-(.+)-(\d+)$')

# Classifier verdicts that are NOT recorded — the pass records nothing and
# re-checks next cadence. 'pending' (an OPEN PR) is the sole member; named here
# so the call site tests membership instead of a bare 'pending' magic string.
NON_RECORDABLE_OUTCOMES = frozenset({'pending'})

# task_terminal_state kernel verdict -> build_outcome. Only MERGED / CLOSED are
# identity-grade terminal evidence. OPEN and UNKNOWN (and any unrecognized
# value) map to None => KEEP: record nothing, stay pending — the UNKNOWN=>KEEP
# invariant (specs/terminal-state-reconciliation.md). A bare-key merge cannot be
# labelled 'merged_regressed' (the kernel has no label signal), only 'merged'.
_TERMINAL_STATE_TO_OUTCOME = {'MERGED': 'merged', 'CLOSED': 'closed_unmerged'}

_LOGGER = logging.getLogger('decision_outcome_reconcile')


@contextmanager
def _reconcile_lock(lock_path: Optional[Path] = None) -> Iterator[bool]:
    """Non-blocking exclusive file lock guarding a single reconcile pass. Yields
    True if this pass acquired the lock, False if another pass holds it. Never
    blocks or queues — a False yield means "another pass owns this tick; exit
    cleanly". Never raises: a lock-file IO failure yields True (degrade to the
    pre-lock single-writer behavior rather than skip the pass)."""
    path = Path(lock_path) if lock_path else LOCK_FILE
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, 'w', encoding='utf-8')
    except OSError as e:
        _log('WARN', f'lock open failed ({e}); proceeding lock-less')
        yield True
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _log(level: str, msg: str) -> None:
    """Append a structured line to the reconciler log. Never raises."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] decision_outcome_reconcile: {msg}\n')
    except OSError:
        pass


def parse_pr_coord(decision_key: str) -> Optional[tuple[str, int]]:
    """`pr-<repo>-<n>` -> (repo, n). Returns None for a non-PR key (a bare
    task_id) or a malformed one. The number is the trailing digit-run, so a
    dashed repo name round-trips."""
    if not isinstance(decision_key, str):
        return None
    m = _PR_COORD_RE.match(decision_key)
    if not m:
        return None
    try:
        return m.group(1), int(m.group(2))
    except (ValueError, IndexError):
        return None


def _gh_pr_view(slug: str, num: int) -> Optional[dict]:
    """Fetch a PR's state/labels from GitHub for a full `owner/repo` slug.
    Returns the parsed JSON dict, or None on any failure (gh missing, network,
    not-found, bad JSON). Never raises. This is the injectable `runner` seam —
    tests pass a stub keyed on (slug, num)."""
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(num),
             '--repo', slug,
             '--json', 'state,mergedAt,labels'],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _log('WARN', f'gh pr view {slug}#{num} failed: '
                     f'{type(e).__name__}: {e}')
        return None
    if proc.returncode != 0:
        _log('WARN', f'gh pr view {slug}#{num} rc={proc.returncode}: '
                     f'{proc.stderr.strip()[:200]}')
        return None
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        _log('WARN', f'gh pr view {slug}#{num} bad JSON: {e}')
        return None


def classify(pr_json: Optional[dict]) -> Optional[str]:
    """Map a `gh pr view` JSON blob to a build-outcome verdict.

      MERGED  + regression label -> 'merged_regressed'  (shipped but bad)
      MERGED                      -> 'merged'
      CLOSED  (not merged)        -> 'closed_unmerged'   (abandoned/rejected)
      OPEN                        -> 'pending'           (not terminal; recheck)

    Returns None when the blob is missing/unusable (treated as "unknown, retry
    next pass"), NOT a guessed terminal state — a wrong terminal verdict is
    worse than a deferred one."""
    if not isinstance(pr_json, dict):
        return None
    state = pr_json.get('state')
    if not isinstance(state, str):
        return None
    state = state.upper()
    if state == 'MERGED':
        labels = pr_json.get('labels')
        names = {
            lb.get('name') for lb in labels if isinstance(lb, dict)
        } if isinstance(labels, list) else set()
        return 'merged_regressed' if _REGRESSION_LABEL in names else 'merged'
    if state == 'CLOSED':
        return 'closed_unmerged'
    if state == 'OPEN':
        return 'pending'
    return None


def _terminal_state_verdict(state: Any) -> Optional[str]:
    """Map a `task_terminal_state` kernel result to a recordable build_outcome,
    or None to KEEP. MERGED -> 'merged', CLOSED -> 'closed_unmerged'; OPEN and
    UNKNOWN (and any unrecognized value) -> None (record nothing, stay pending)
    — the UNKNOWN=>KEEP invariant."""
    if not isinstance(state, str):
        return None
    return _TERMINAL_STATE_TO_OUTCOME.get(state.strip().upper())


def reconcile(
    *,
    limit: int = 500,
    runner: Optional[Callable[..., Optional[dict]]] = None,
    terminal_state_fn: Optional[Callable[[str], str]] = None,
    now: Optional[datetime] = None,
    logger: Optional[logging.Logger] = None,
) -> dict[str, int]:
    """One reconcile pass. For each decision key in the work-list (no
    build_outcome yet, OR a re-checkable `closed_unmerged` inside the settle
    window), resolve GitHub truth and record a terminal outcome — a PR-coordinate
    key via `gh pr view`, a bare task_id via the shared `task_terminal_state`
    kernel. Returns a summary dict. Never raises.

    `runner(slug, num)` is the PR-coordinate GitHub seam (defaults to
    `_gh_pr_view`). `terminal_state_fn(task_id)` is the bare-key seam (defaults
    to `task_terminal_state.task_terminal_state`). Tests inject both so no real
    gh call happens.

    Re-check + supersede (item b): the work-list already includes a
    closed_unmerged key still in its settle window. The pre-computed
    `latest` map (newest build_outcome per key, ONE whole-file scan — not a
    per-key `has_build_outcome` scan) lets the pass record only a CHANGED
    verdict: a later `merged` supersedes the closed_unmerged; an unchanged
    closed_unmerged re-check appends nothing (no churn).

    Concurrency: the caller (`run_once`) holds a file lock, so passes are
    serialized; within one pass each key is processed once."""
    log = logger or _LOGGER
    fetch = runner or _gh_pr_view
    tstate = terminal_state_fn or tts.task_terminal_state
    now = now or datetime.now(timezone.utc)
    summary = {'checked': 0, 'recorded': 0, 'superseded': 0, 'pending': 0,
               'rechecked_no_change': 0, 'skipped_unknown_repo': 0, 'errors': 0}
    try:
        keys = dol.decision_keys_without_outcome(limit, now=now)
    except Exception as e:  # noqa: BLE001 — never let a bad read abort the pass
        _log('WARN', f'work-list read failed: {type(e).__name__}: {e}')
        return summary
    try:
        latest = dol.latest_build_outcomes()  # one whole-file scan (cleanup 3)
    except Exception as e:  # noqa: BLE001
        _log('WARN', f'latest-outcome read failed: {type(e).__name__}: {e}')
        latest = {}
    for key in keys:
        coord = parse_pr_coord(key)
        if coord is not None:
            bare_repo, num = coord
            slug = resolve_repo_slug(bare_repo)
            if slug is None:
                # Repo not in the registry — skip rather than guess an owner.
                summary['skipped_unknown_repo'] += 1
                _log('WARN', f'{key}: repo {bare_repo!r} not in REPOS — skipping')
                continue
            summary['checked'] += 1
            try:
                pr_json = fetch(slug, num)
            except Exception as e:  # noqa: BLE001 — a stub/gh blow-up is not fatal
                _log('WARN', f'runner raised for {key}: {type(e).__name__}: {e}')
                summary['errors'] += 1
                continue
            verdict = classify(pr_json)
            pr_number: Optional[int] = num
            # A None from classify() is an unusable blob for a PR-coord key.
            unusable_is_error = True
        else:
            # Bare task_id (dispatch-approval class): the shared kernel probes gh
            # for a PR matching the task_id. UNKNOWN/OPEN => KEEP.
            summary['checked'] += 1
            try:
                state = tstate(key)
            except Exception as e:  # noqa: BLE001 — a kernel/gh blow-up is not fatal
                _log('WARN', f'terminal_state raised for {key}: '
                             f'{type(e).__name__}: {e}')
                summary['errors'] += 1
                continue
            verdict = _terminal_state_verdict(state)  # None for OPEN/UNKNOWN
            pr_number = None
            # A None here is KEEP (OPEN/UNKNOWN), not an error.
            unusable_is_error = False
        if verdict is None:
            if unusable_is_error:
                summary['errors'] += 1
            else:
                summary['pending'] += 1  # KEEP
            continue
        if verdict in NON_RECORDABLE_OUTCOMES:  # 'pending' (OPEN PR)
            summary['pending'] += 1
            continue
        prior = latest.get(key)
        if prior is not None and verdict == prior:
            # Re-check found no change (e.g. still closed_unmerged inside the
            # settle window) — record nothing, avoid churn.
            summary['rechecked_no_change'] += 1
            continue
        if dol.record_build_outcome(key, verdict, pr_number=pr_number):
            if prior is None:
                summary['recorded'] += 1
                log.info('recorded build_outcome=%s for %s', verdict, key)
            else:
                summary['superseded'] += 1
                log.info('superseded %s -> %s for %s', prior, verdict, key)
        else:
            summary['errors'] += 1
    _log('INFO', f'reconcile pass: {json.dumps(summary)}')
    return summary


def run_once(**kwargs) -> dict:
    """Timer entrypoint: take the single-writer lock, then run ONE reconcile
    pass. If another pass holds the lock, log and return without writing (the
    concurrency-safety guarantee behind the 30-min timer). Never raises."""
    with _reconcile_lock() as acquired:
        if not acquired:
            _log('INFO', 'reconcile lock held by another pass; exiting 0')
            return {'skipped_locked': True}
        return reconcile(**kwargs)


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--once':
        print(json.dumps(run_once(), ensure_ascii=False))
        sys.exit(0)
    print('usage: decision_outcome_reconcile.py --once', file=sys.stderr)
    sys.exit(2)
