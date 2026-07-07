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
    whose PR is not yet known) are skipped and counted; slice-2 scope is the
    PR-coordinate class (the "approve/merge this PR" decisions). Extending the
    join to task-keyed decisions is later work.

NOT wired to a systemd timer in this PR — ship the join logic + CLI + tests;
scheduling the cadence is a small follow-up (or call `reconcile()` from an
existing pass). Run manually via `python3 decision_outcome_reconcile.py --once`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import decision_outcome_ledger as dol  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
LOG_FILE = AGENTS_ROOT / 'logs' / 'decision-outcome-reconcile.log'

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

_LOGGER = logging.getLogger('decision_outcome_reconcile')


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


def reconcile(
    *,
    limit: int = 500,
    runner: Optional[Callable[..., Optional[dict]]] = None,
    logger: Optional[logging.Logger] = None,
) -> dict[str, int]:
    """One reconcile pass. For each PR-coordinate decision key with no
    build_outcome yet, resolve its repo against the REPOS registry, look up
    GitHub truth, and record a terminal outcome. Returns a summary dict. Never
    raises.

    `runner(slug, num)` is the GitHub lookup seam (defaults to `_gh_pr_view`,
    which takes a resolved `owner/repo` slug); tests inject a stub so no real gh
    call happens.

    Idempotency is SINGLE-WRITER: the work-list snapshot + per-key
    `has_build_outcome` re-check guard SEQUENTIAL re-runs (the intended mode —
    this pass is not timer-wired in slice 2, run manually via --once). Two
    genuinely CONCURRENT passes could each append a build_outcome row for one
    key; both rows carry the same verdict (no contradiction), but if a timer is
    added later this should take a file lock."""
    log = logger or _LOGGER
    fetch = runner or _gh_pr_view
    summary = {'checked': 0, 'recorded': 0, 'pending': 0,
               'skipped_non_pr': 0, 'skipped_unknown_repo': 0, 'errors': 0}
    try:
        keys = dol.decision_keys_without_outcome(limit)
    except Exception as e:  # noqa: BLE001 — never let a bad read abort the pass
        _log('WARN', f'work-list read failed: {type(e).__name__}: {e}')
        return summary
    for key in keys:
        coord = parse_pr_coord(key)
        if coord is None:
            summary['skipped_non_pr'] += 1
            continue
        bare_repo, num = coord
        slug = resolve_repo_slug(bare_repo)
        if slug is None:
            # Repo not in the registry — skip rather than guess an owner.
            summary['skipped_unknown_repo'] += 1
            _log('WARN', f'{key}: repo {bare_repo!r} not in REPOS — skipping')
            continue
        # Guard against a race: another pass may have recorded it since the
        # work-list read (the list is a snapshot).
        if dol.has_build_outcome(key):
            continue
        summary['checked'] += 1
        try:
            pr_json = fetch(slug, num)
        except Exception as e:  # noqa: BLE001 — a stub/gh blow-up is not fatal
            _log('WARN', f'runner raised for {key}: {type(e).__name__}: {e}')
            summary['errors'] += 1
            continue
        verdict = classify(pr_json)
        if verdict is None:
            summary['errors'] += 1
            continue
        if verdict == 'pending':
            summary['pending'] += 1
            continue
        if dol.record_build_outcome(key, verdict, pr_number=num):
            summary['recorded'] += 1
            log.info('recorded build_outcome=%s for %s', verdict, key)
        else:
            summary['errors'] += 1
    _log('INFO', f'reconcile pass: {json.dumps(summary)}')
    return summary


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--once':
        print(json.dumps(reconcile(), ensure_ascii=False))
        sys.exit(0)
    print('usage: decision_outcome_reconcile.py --once', file=sys.stderr)
    sys.exit(2)
