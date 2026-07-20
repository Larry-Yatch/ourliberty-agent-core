#!/usr/bin/env python3
"""task_resolution.py -- shared "was this task already resolved out-of-band?"
check for healers.

Several healers act on a task that LOOKS stuck -- re-dispatch it, recover it,
or alert on it. Before acting they should confirm the task was not ALREADY
resolved by a path the healer doesn't directly observe:

  * Larry approved/rejected it via the Approvals tab (a `larry_action`
    chain_event), or the producing agent already started a fresh attempt (a
    later `session_start` chain_event supersedes the stuck pointer); or
  * the work already shipped as a merged PR whose branch/title carries the
    task_id.

`resolved_out_of_band` returns (True, reason) ONLY on positive evidence; on
no evidence OR any infrastructure error it returns (False, None) so the
caller's existing behavior is preserved. This is the safe failsafe direction
for the current callers: they only SUPPRESS their action on a positive
signal, so an infra hiccup can never manufacture a false "resolved".

Mirrors heal_pipeline_stall._resolution_signal_present but is standalone and
dependency-light (stdlib + lazy optional supabase/gh) so any healer can
import it without pulling in that module's global state.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Repo scripts dir on sys.path so the sibling id_match import resolves cleanly
# when invoked by systemd / a healer.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from id_match import id_matches  # noqa: E402

GH_TIMEOUT_S = 15

# Repos a shipped build could land in. Mirrors heal_pipeline_stall.REPOS.
DEFAULT_REPOS = (
    'Larry-Yatch/ourliberty-agent-core',
    'Larry-Yatch/ourliberty-dashboard',
    'Larry-Yatch/RSDPM',
)

# Branch-truncation floor: Forge truncates long branch names, so a PR branch
# that is a strict prefix of the task_id and at least this long is treated as
# a (truncated) match. 30 avoids matching short common prefixes like `build-`.
_BRANCH_TRUNCATION_MIN_LEN = 30

# Forge re-attempts a task on a fresh branch by appending a numeric iteration
# suffix: `forge/<task_id>-002`, `-003`, … . The orphan's task_id is the bare
# `<task_id>` (no suffix), so the branch carries an EXTRA `-<NN>` that neither the
# exact nor the truncation rule strips — i.e. the canonical Forge re-attempt
# branch never matched its own task, and shipped proposals never retired. We strip
# ONE trailing `-<digits>` group and match on EXACT-core equality (not a
# substring); a length floor mirrors id_match's anti-short-id discipline so a tiny
# task id can't collide with an unrelated `<short>-NN` branch.
_BRANCH_ITER_MIN_LEN = 12


def _noop(*_a, **_k) -> None:
    pass


def _gh_env() -> dict:
    return {**os.environ,
            'PATH': '/usr/bin:/usr/local/bin:' + os.environ.get('PATH', '')}


def get_chain_events_for_task(
    task_id: str, since_ts: datetime,
    *, log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[list[dict]]:
    """Supabase chain_events for `task_id` with ts > since_ts. Returns the
    rows (possibly empty) on success, or None on infrastructure failure
    (missing env, import error, query exception). Callers treat None as 'no
    signal'. Lazy supabase import per the heal_chain_event_type_audit pattern."""
    log_fn = log_fn or _noop
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        return None
    try:
        from supabase_factory import get_supabase_client  # type: ignore
        client = get_supabase_client(url, key)
        since_iso = since_ts.astimezone(timezone.utc).isoformat()
        res = (
            client.table('chain_events')
                  .select('event_type,ts,actor,task_id')
                  .eq('task_id', task_id)
                  .gt('ts', since_iso)
                  .execute()
        )
        return getattr(res, 'data', None) or []
    except Exception as e:  # noqa: BLE001 -- failsafe, never raise
        log_fn(f'chain_events query failed for task={task_id}: '
               f'{type(e).__name__}: {e}')
        return None


def _branch_task(branch: str) -> Optional[str]:
    for prefix in ('forge/', 'larry/'):
        if branch.startswith(prefix):
            return branch[len(prefix):]
    return None


def pr_matches_task(pr: dict, task_id: str) -> Optional[str]:
    """Return a match reason ('branch'/'branch_iter'/'branch_truncated'/'title')
    if `pr` corresponds to `task_id`, else None. Mirrors
    heal_pipeline_stall._pr_matches_task."""
    branch_task = _branch_task(pr.get('headRefName') or '')
    if branch_task:
        if branch_task == task_id:
            return 'branch'
        # Forge iteration branch: `forge/<task_id>-<NN>` carries an extra numeric
        # suffix the bare task_id lacks. Strip one trailing `-<digits>` and match
        # on length-floored exact-core equality, so the task's own re-attempt
        # branch matches without ever matching an unrelated PR.
        iter_core = re.sub(r'-\d+$', '', branch_task)
        if (iter_core != branch_task
                and iter_core == task_id
                and len(iter_core) >= _BRANCH_ITER_MIN_LEN):
            return 'branch_iter'
        if (len(branch_task) >= _BRANCH_TRUNCATION_MIN_LEN
                and task_id.startswith(branch_task)):
            return 'branch_truncated'
    # Title fallback: match the task_id only as a whole, boundary-delimited
    # token with a length floor (id_matches). A bare `task_id.lower() in
    # title.lower()` let a short/common task_id match an unrelated merged PR
    # title and suppress legitimate recovery (audit #19); the branch path above
    # already carries its own `_BRANCH_TRUNCATION_MIN_LEN` floor.
    title = pr.get('title') or ''
    if id_matches(task_id, title):
        return 'title'
    return None


def shipped_pr(
    task_id: str,
    *, since_ts: Optional[datetime] = None,
    repos: Optional[tuple] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """Return a merged PR (augmented with `_repo`) whose branch/title matches
    `task_id`, else None. When `since_ts` is given, only PRs merged at/after
    it count. Failsafe: any gh error skips that repo (returns None overall if
    none match)."""
    log_fn = log_fn or _noop
    if not task_id or task_id == 'unknown':
        return None
    # _parse_iso always returns an AWARE datetime; comparing it to a naive
    # since_ts raises TypeError (not ValueError), which would escape this
    # module's documented never-raise/failsafe contract (audit #40). Normalize a
    # naive since_ts to UTC so every comparison is aware-vs-aware.
    if since_ts is not None and since_ts.tzinfo is None:
        since_ts = since_ts.replace(tzinfo=timezone.utc)
    for repo in (repos or DEFAULT_REPOS):
        try:
            res = subprocess.run(
                ['gh', 'pr', 'list', '--repo', repo, '--state', 'merged',
                 '--limit', '40', '--json',
                 'number,title,headRefName,mergedAt'],
                capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                env=_gh_env(),
            )
            if res.returncode != 0:
                log_fn(f'gh pr list {repo} rc={res.returncode}: '
                       f'{res.stderr[:160]}')
                continue
            prs = json.loads(res.stdout or '[]')
        except (subprocess.TimeoutExpired, json.JSONDecodeError,
                FileNotFoundError, OSError) as e:
            log_fn(f'gh pr list {repo} failed: {type(e).__name__}: {e}')
            continue
        for pr in prs:
            if since_ts is not None:
                merged = pr.get('mergedAt')
                if merged:
                    try:
                        if _parse_iso(merged) < since_ts:
                            continue
                    except (ValueError, TypeError):
                        # Unparseable mergedAt or any residual aware/naive
                        # mismatch — don't let it escape the failsafe contract.
                        pass
            if pr_matches_task(pr, task_id):
                pr['_repo'] = repo
                return pr
    return None


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolved_out_of_band(
    task_id: str, since_ts: datetime,
    *, check_pr: bool = True,
    repos: Optional[tuple] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> tuple[bool, Optional[str]]:
    """(True, reason) iff `task_id` was resolved out-of-band since `since_ts`:

      - 'larry_action'        -- Larry acted via the Approvals tab.
      - 'superseded_session'  -- a later session_start re-ran the task.
      - 'pr_merged:#<n>'      -- a merged PR matches the task (check_pr only).

    Returns (False, None) on no signal OR any infra error (failsafe: the
    caller proceeds with its normal action). `task_id` of '' / 'unknown'
    short-circuits to (False, None)."""
    log_fn = log_fn or _noop
    if not task_id or task_id == 'unknown':
        return (False, None)
    rows = get_chain_events_for_task(task_id, since_ts, log_fn=log_fn)
    if rows:
        if any(r.get('event_type') == 'larry_action' for r in rows):
            return (True, 'larry_action')
        if any(r.get('event_type') == 'session_start' for r in rows):
            return (True, 'superseded_session')
    if check_pr:
        pr = shipped_pr(task_id, since_ts=since_ts, repos=repos, log_fn=log_fn)
        if pr is not None:
            return (True, f'pr_merged:#{pr.get("number")}')
    return (False, None)
