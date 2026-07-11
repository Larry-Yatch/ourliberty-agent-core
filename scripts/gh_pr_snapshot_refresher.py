#!/usr/bin/env python3
"""gh_pr_snapshot_refresher.py — the SINGLE gh-list caller (gh-api-burn phase 2).

The other half of the durable phase-2 fix (reader: ``gh_pr_snapshot.py``). Phase
1 (PR #896) measured the burn: ~10 timer-driven daemons each shell ``gh pr
list`` against the same repos every few minutes, redundantly re-fetching the same
PR data and exhausting the shared ~5000 pts/hr GraphQL budget. This refresher is
the ONE process that fetches — once per repo per tick (~3 min timer) — writing a
shared snapshot the many consumers read for FREE via the reader. Consolidating N
redundant listings into one is the whole win.

WHY ``--state all`` (not open-only): the one cached fetch serves BOTH consumer
classes. The terminal-state kernel (``task_terminal_state`` /
``system_state_log._bulk_terminal_states``) needs MERGED/CLOSED PRs to retire
phantom bookkeeping records; the open-PR healers filter to ``state == OPEN``
locally. Every record carries its ``state`` — the discriminator both classes key
off. A single all-state listing costs the same one query whether or not a
consumer later filters it.

FAIL-OPEN (never make things worse than phase 1):
  * Honors the phase-1 ``gh_budget`` backoff up front — when the GraphQL budget
    is low the refresher SKIPS this tick entirely, leaving the prior snapshot
    intact (readers keep serving last-known data, flagged stale). It never
    deepens exhaustion.
  * A repo whose fetch fails does NOT blank that repo: its prior entry is carried
    forward UNCHANGED (with its old per-repo ``as_of``), so a reader correctly
    sees it as stale rather than as "zero PRs." A per-repo failure never retires
    a phantom by making its PR vanish.
  * A total failure (every repo failed, or the budget backoff fired) writes
    NOTHING — the existing file is left byte-for-byte intact.
  * Every write is atomic (``atomic_io.atomic_write_json``: unique tmp + fsync +
    ``os.replace``), so a reader never observes a torn snapshot.

Stdlib + ``gh`` + the shared helpers (``gh_budget``, ``task_terminal_state`` for
repo list / qualification, ``atomic_io``, ``gh_pr_snapshot`` for the path +
schema). Designed to run as a systemd oneshot on a timer.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import gh_budget  # noqa: E402
import gh_pr_snapshot as snap  # noqa: E402  path + schema (no cycle: reader lazy-imports tts)
import task_terminal_state as tts  # noqa: E402  default_repos + _qualify_repo
from atomic_io import atomic_write_json  # noqa: E402

# How many recent PRs to snapshot per repo. Deliberately >= the kernel's
# retirement lookback (``tts.DEFAULT_PR_LOOKBACK`` = 50): a reader slices the
# head of this number-descending list to reproduce a narrower window exactly, so
# a wider snapshot never SHRINKS any consumer's window — it only ever widens what
# is available. 200 covers a busy repo's recent churn well within one query.
SNAPSHOT_LIMIT = 200

# Generous per-``gh``-call timeout: this is the ONE fetcher on a 3-min timer, not
# a latency-sensitive request path, so a slow-but-successful listing beats a
# spurious carry-forward-stale.
GH_TIMEOUT_SEC = 60.0

# Rich field set — the superset every known consumer filters from, so no consumer
# needs its own gh listing. Terminal-state kernel: state/title/headRefName drive
# match+classify. Open-PR healers: isDraft/mergeStateStatus/reviewDecision/
# statusCheckRollup/labels (mergeability), headRefOid (dispatch dedup), createdAt.
# Timestamps (updatedAt/mergedAt/closedAt) are cheap and useful for observability.
# Deliberately NOT included: ``files`` (per-PR file list is the expensive field
# and no consumer needs it — including it would undercut the whole burn fix).
PRIMARY_FIELDS = (
    'number,state,title,url,headRefName,headRefOid,createdAt,isDraft,'
    'mergeStateStatus,reviewDecision,statusCheckRollup,labels,'
    'updatedAt,mergedAt,closedAt'
)

# Cheap fallback if the rich listing fails (e.g. a field is unsupported on this
# gh version, or the rich query times out): enough for the terminal-state kernel
# to keep working. A degraded-but-present snapshot beats none.
CORE_FIELDS = 'number,state,title,url,headRefName,updatedAt,mergedAt,closedAt'


def _log(msg: str) -> None:
    print(f'[gh_pr_snapshot_refresher] {msg}', file=sys.stderr, flush=True)


def _now_iso(now: Optional[datetime] = None) -> str:
    dt = now if now is not None else datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _gh_list(
    repo: str, fields: str, *, limit: int, timeout: float,
) -> Optional[list[dict]]:
    """One ``gh pr list --state all`` for a single (qualified) repo -> list of PR
    dicts, or None on ANY failure (gh missing, timeout, non-zero exit, unparseable
    or non-list JSON). None is the "fetch failed" signal the carry-forward keys
    off; it is never confused with an empty repo (an empty ``[]`` result)."""
    import json
    args = [
        'gh', 'pr', 'list', '--repo', tts._qualify_repo(repo),
        '--state', 'all', '--limit', str(limit), '--json', fields,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        _log(f'{repo}: gh pr list failed ({type(exc).__name__})')
        return None
    if proc.returncode != 0:
        _log(f'{repo}: gh pr list exit={proc.returncode}: '
             f'{(proc.stderr or "").strip()[:200]}')
        return None
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        _log(f'{repo}: gh pr list returned unparseable JSON')
        return None
    if not isinstance(data, list):
        return None
    return [p for p in data if isinstance(p, dict)]


def fetch_repo_prs(
    repo: str, *, limit: int = SNAPSHOT_LIMIT, timeout: float = GH_TIMEOUT_SEC,
) -> Optional[list[dict]]:
    """Fetch one repo's PRs: try the rich :data:`PRIMARY_FIELDS`, and on failure
    fall back ONCE to the cheap :data:`CORE_FIELDS` (which still satisfies the
    terminal-state kernel). Returns the PR list, or None only if BOTH attempts
    fail (the signal to carry the prior entry forward)."""
    prs = _gh_list(repo, PRIMARY_FIELDS, limit=limit, timeout=timeout)
    if prs is not None:
        return prs
    _log(f'{repo}: primary field fetch failed; trying core fields')
    return _gh_list(repo, CORE_FIELDS, limit=limit, timeout=timeout)


def _prior_repo_entry(prior: Optional[dict], key: str) -> Optional[dict]:
    if not isinstance(prior, dict):
        return None
    repos = prior.get('repos')
    if not isinstance(repos, dict):
        return None
    entry = repos.get(key)
    return entry if isinstance(entry, dict) else None


def build_snapshot(
    per_repo_prs: dict[str, Optional[list[dict]]], *,
    prior: Optional[dict], now_iso: str,
) -> dict:
    """Assemble the snapshot dict from this tick's per-repo fetch results.

    A successful fetch (``prs is not None``) writes a FRESH entry stamped
    ``now_iso``. A failed fetch (``None``) CARRIES the prior entry forward
    unchanged — preserving its old per-repo ``as_of`` so a reader sees it as
    stale, not as an empty repo. A repo that failed with no prior entry is
    omitted (a reader treats missing as stale -> last-known/[]). The top-level
    ``as_of`` is this refresh ATTEMPT's time regardless of per-repo outcomes."""
    repos_out: dict[str, dict] = {}
    for key, prs in per_repo_prs.items():
        if prs is not None:
            repos_out[key] = {'as_of': now_iso, 'prs': prs}
            continue
        carried = _prior_repo_entry(prior, key)
        if carried is not None:
            repos_out[key] = carried
    return {'schema': snap.SCHEMA, 'as_of': now_iso, 'repos': repos_out}


def refresh(
    *,
    repos: Optional[list[str]] = None,
    path: Path = snap.SNAPSHOT_PATH,
    limit: int = SNAPSHOT_LIMIT,
    timeout: float = GH_TIMEOUT_SEC,
    now: Optional[datetime] = None,
    min_graphql: int = gh_budget.DEFAULT_MIN_GRAPHQL,
) -> int:
    """Refresh the shared snapshot. Returns the number of repos fetched FRESH this
    tick (0 when the budget backoff fired or every repo failed — in both cases the
    existing snapshot is left untouched).

    Fail-open at every layer (see the module docstring): budget backoff up front,
    per-repo carry-forward on partial failure, no-write on total failure, atomic
    write on success."""
    if gh_budget.should_skip('gh_pr_snapshot_refresher', min_graphql, log=_log):
        _log('GraphQL budget low; skipping refresh (prior snapshot left intact)')
        return 0

    repo_list = repos if repos is not None else tts.default_repos()
    prior = snap.load_snapshot(path)
    per_repo: dict[str, Optional[list[dict]]] = {}
    fresh = 0
    for repo in repo_list:
        key = tts._qualify_repo(repo)
        prs = fetch_repo_prs(repo, limit=limit, timeout=timeout)
        per_repo[key] = prs
        if prs is not None:
            fresh += 1

    if fresh == 0:
        _log('all repo fetches failed; leaving prior snapshot intact (no write)')
        return 0

    now_iso = _now_iso(now)
    snapshot = build_snapshot(per_repo, prior=prior, now_iso=now_iso)
    atomic_write_json(path, snapshot, trailing_newline=True)
    total = len(repo_list)
    _log(f'wrote snapshot: {fresh}/{total} repos fresh, '
         f'{total - fresh} carried stale -> {path}')
    return fresh


def main(argv: Optional[list[str]] = None) -> int:
    fresh = refresh()
    # Always exit 0: a budget skip or an all-fail carry is a NORMAL fail-open
    # outcome, not a service failure (systemd would else mark the unit failed and
    # noise the operator). The count is logged above for observability.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
