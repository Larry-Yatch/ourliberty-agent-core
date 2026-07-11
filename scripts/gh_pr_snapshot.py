#!/usr/bin/env python3
"""gh_pr_snapshot.py — shared cached PR-snapshot READER (gh-api-burn phase 2).

Phase 1 (PR #896) added the backoff/measurement that proved the burn: ~10
timer-driven daemons each shell ``gh pr list`` against the same repos every few
minutes, redundantly re-fetching the same PR data and exhausting the shared
~5000 pts/hr GraphQL budget. This module is half of the durable phase-2 fix: a
SINGLE refresher (``gh_pr_snapshot_refresher.py``) fetches every repo's PRs once
per ~3 min into a shared snapshot, and the many consumers read that snapshot
through THIS module instead of each shelling ``gh`` themselves.

The snapshot is ``--state all`` (not open-only): the ONE cached fetch serves BOTH
the terminal-state kernel (which needs MERGED/CLOSED PRs to retire phantom
records) AND the open-PR consumers (which filter to ``state == OPEN`` locally via
:func:`open_prs`). See the refresher for why all-state.

Snapshot schema (``~/agents/state/gh-open-pr-snapshot.json``)::

    {
      "schema": "gh-open-pr-snapshot-v1",
      "as_of": "<iso8601>",                 # last refresh ATTEMPT
      "repos": {
        "Larry-Yatch/ourliberty-agent-core": {
          "as_of": "<iso8601>",             # when THIS repo was last fetched
          "prs": [ {number, state, title, url, headRefName, ...}, ... ]
        },
        ...
      }
    }

FRESHNESS CONTRACT (the fail-safe the whole design rides on)
-----------------------------------------------------------
* Each repo entry carries its OWN ``as_of``; a reader keys staleness off that,
  not the top-level one. (The refresher advances the top-level ``as_of`` every
  tick but CARRIES FORWARD a repo's prior entry — with its old ``as_of`` — when
  that repo's fetch fails, so a fail-open carry reads correctly as stale.)
* A repo entry is FRESH when ``now - as_of <= max_age_s`` (default
  :data:`DEFAULT_MAX_AGE_S`). Fresh reads are FREE — no ``gh``, no GraphQL burn.
* On a STALE or MISSING entry a reader returns the LAST-KNOWN prs (possibly
  empty), flagged stale via a log line. This is the default and it NEVER shells
  ``gh`` — so a stale snapshot can never make the ~10 readers stampede the API.
* A BOUNDED single live ``gh`` fallback is available ONLY opt-in
  (``live_fallback=True``) for the few callers where correctness needs current
  data when the snapshot is down. Even then it is doubly bounded: (a) a
  cross-process cooldown (:data:`FALLBACK_COOLDOWN_S`) so at most ONE reader
  process fires a live fetch per repo per window — the rest get the stale data;
  (b) the phase-1 ``gh_budget`` backoff, so a live fallback never deepens
  exhaustion. Only the refresher ever WRITES the snapshot; a reader's fallback
  result is ephemeral (returned to the caller, never persisted).

stdlib + (lazy) ``gh_budget`` / ``atomic_io``. Never raises to a caller: every
public reader degrades to ``[]`` / ``None`` on any internal error.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
SNAPSHOT_PATH = AGENTS_ROOT / 'state' / 'gh-open-pr-snapshot.json'
SCHEMA = 'gh-open-pr-snapshot-v1'

OPEN = 'OPEN'

# A reader treats a per-repo entry as FRESH within this window. The refresher
# runs ~every 3 min, so 10 min tolerates ~3 missed refreshes before a read is
# flagged stale (and, if opted in, a bounded fallback becomes eligible).
DEFAULT_MAX_AGE_S = 600.0

# no-stampede guard: at most ONE live fallback per repo per this window across
# ALL reader processes, tracked in a shared cooldown sidecar. Matches the refresh
# cadence — by the time a second window opens the refresher has likely repaired
# the snapshot anyway.
FALLBACK_COOLDOWN_S = 180.0
# Bounded live-fallback fetch: kept cheap (a small field set, the kernel's
# retirement lookback) — the refresher owns the rich/wide fetch.
FALLBACK_LIMIT = 50
FALLBACK_TIMEOUT_S = 15.0
_FALLBACK_STATE_PATH = AGENTS_ROOT / 'state' / 'gh-open-pr-snapshot.fallback.json'
# Field set the bounded fallback fetches: enough for the terminal-state kernel
# (state/title/headRefName drive the match+classify) plus the cheap open-consumer
# basics. The refresher fetches the richer superset consumers filter from.
_FALLBACK_FIELDS = (
    'number,state,title,url,headRefName,isDraft,mergeStateStatus,'
    'labels,updatedAt,mergedAt,closedAt'
)


def _log(msg: str) -> None:
    print(f'[gh_pr_snapshot] {msg}', file=sys.stderr, flush=True)


def _gh_owner() -> str:
    return os.environ.get('OURLIBERTY_GH_OWNER', 'Larry-Yatch')


def _qualify_repo(repo: str) -> str:
    """Return ``repo`` in the ``OWNER/REPO`` form the snapshot is keyed by.

    Delegates to the single shared ``sequence_shortcut_helpers.qualify_repo`` so
    the owner default lives in one place; a lazy import + local fallback keeps
    this reader importable (and the cycle with ``task_terminal_state`` broken)
    even where that helper isn't available."""
    try:
        from sequence_shortcut_helpers import qualify_repo
        return qualify_repo(repo)
    except Exception:  # noqa: BLE001 — fall back to the identical local rule
        if not repo:
            return repo
        return repo if '/' in repo else f'{_gh_owner()}/{repo}'


def load_snapshot(path: Path = SNAPSHOT_PATH) -> Optional[dict]:
    """Parse the snapshot file, or None on any read/parse error (missing file,
    bad JSON, wrong top-level shape). Never raises."""
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def _repo_entry(snapshot: Optional[dict], repo: str) -> Optional[dict]:
    """The per-repo entry for ``repo`` (qualified, with a bare-name fallback), or
    None if absent/malformed."""
    if not isinstance(snapshot, dict):
        return None
    repos = snapshot.get('repos')
    if not isinstance(repos, dict):
        return None
    key = _qualify_repo(repo)
    entry = repos.get(key)
    if entry is None and key != repo:
        entry = repos.get(repo)  # tolerate a snapshot keyed by bare name
    return entry if isinstance(entry, dict) else None


def _parse_ts(iso: Any) -> Optional[float]:
    """ISO-8601 string -> epoch seconds (naive stamps treated as UTC), else None."""
    if not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _entry_prs(entry: Optional[dict]) -> Optional[list[dict]]:
    if not isinstance(entry, dict):
        return None
    prs = entry.get('prs')
    if not isinstance(prs, list):
        return None
    return [p for p in prs if isinstance(p, dict)]


def _is_fresh(entry: Optional[dict], now: float, max_age_s: float) -> bool:
    ts = _parse_ts(entry.get('as_of')) if isinstance(entry, dict) else None
    if ts is None:
        return False
    return (now - ts) <= max_age_s


def freshness(
    repo: str, *, max_age_s: float = DEFAULT_MAX_AGE_S,
    snapshot: Optional[dict] = None, now: Optional[float] = None,
) -> dict:
    """Observability helper: {'present','stale','age_seconds','count'} for
    ``repo``'s snapshot entry. Pure (no gh); safe for logs/health checks."""
    now = time.time() if now is None else now
    snap = snapshot if snapshot is not None else load_snapshot()
    entry = _repo_entry(snap, repo)
    ts = _parse_ts(entry.get('as_of')) if isinstance(entry, dict) else None
    prs = _entry_prs(entry)
    return {
        'present': isinstance(entry, dict),
        'stale': not _is_fresh(entry, now, max_age_s),
        'age_seconds': (now - ts) if ts is not None else None,
        'count': len(prs) if prs is not None else 0,
    }


# -------------------- bounded, no-stampede live fallback --------------------

def _budget_ok_for_fallback() -> bool:
    """Respect the phase-1 backoff: a stale snapshot beats deepening exhaustion,
    so decline the live fallback when the GraphQL budget is low. Fail-open (an
    unknown/broken budget proceeds)."""
    try:
        import gh_budget
        return not gh_budget.should_skip('gh_pr_snapshot.live_fallback')
    except Exception:  # noqa: BLE001
        return True


def _fallback_allowed(
    repo: str, now: float, *,
    path: Path = _FALLBACK_STATE_PATH, cooldown: float = FALLBACK_COOLDOWN_S,
) -> bool:
    """Check-and-claim the cross-process cooldown so at most ONE reader fires a
    live fallback per repo per ``cooldown`` window. Returns True (claimed) or
    False (another reader claimed it recently — return stale instead).

    Fail-SAFE: any error claiming the slot returns False (DENY) — never stampede.
    A chronically unwritable cooldown file therefore degrades to "no live
    fallback ever," i.e. the conservative last-known-data path, which is exactly
    the pre-phase-2 behavior on a gh failure."""
    key = _qualify_repo(repo)
    try:
        from atomic_io import atomic_write_json, locked_update
        with locked_update(path):
            data: dict = {}
            try:
                loaded = json.loads(Path(path).read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError, TypeError):
                data = {}
            last = data.get(key)
            if isinstance(last, (int, float)) and (now - last) < cooldown:
                return False
            data[key] = now
            atomic_write_json(path, data)
            return True
    except Exception:  # noqa: BLE001 — never stampede on a cooldown-machinery error
        return False


def _live_fetch(
    repo: str, limit: int, *, timeout: float = FALLBACK_TIMEOUT_S,
) -> Optional[list[dict]]:
    """One bounded ``gh pr list --state all`` for a single repo. None on any gh
    failure. This is the ONLY place a reader shells gh, and only under the
    cooldown + budget guards above."""
    args = [
        'gh', 'pr', 'list', '--repo', _qualify_repo(repo),
        '--state', 'all', '--limit', str(limit), '--json', _FALLBACK_FIELDS,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else None


# -------------------- public readers --------------------

def all_prs(
    repo: str, *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    live_fallback: bool = False,
    limit: Optional[int] = None,
    fallback_timeout: float = FALLBACK_TIMEOUT_S,
    snapshot: Optional[dict] = None,
    now: Optional[float] = None,
) -> list[dict]:
    """All PRs (any state) for ``repo`` from the shared snapshot.

    Fresh snapshot -> its prs (free, no gh). Stale/missing -> last-known prs
    (flagged stale), OR — only when ``live_fallback`` is set AND the cooldown +
    budget guards allow — a single bounded live fetch. ``limit`` slices the head
    of the list (the snapshot is number-descending, as ``gh pr list`` returns),
    so a consumer can reproduce a narrower ``--limit`` window exactly.
    """
    now = time.time() if now is None else now
    snap = snapshot if snapshot is not None else load_snapshot()
    entry = _repo_entry(snap, repo)
    prs = _entry_prs(entry)

    if prs is not None and _is_fresh(entry, now, max_age_s):
        return prs[:limit] if limit else prs

    # Stale or missing from here down.
    if live_fallback and _budget_ok_for_fallback() and _fallback_allowed(repo, now):
        live = _live_fetch(repo, limit or FALLBACK_LIMIT, timeout=fallback_timeout)
        if live is not None:
            return live

    if prs is not None:
        _log(f'{_qualify_repo(repo)}: serving STALE snapshot '
             f'({len(prs)} prs, max_age_s={max_age_s})')
        return prs[:limit] if limit else prs
    _log(f'{_qualify_repo(repo)}: snapshot missing/unreadable; returning [] '
         f'(live_fallback={live_fallback})')
    return []


def open_prs(
    repo: str, *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    live_fallback: bool = False,
    limit: Optional[int] = None,
    fallback_timeout: float = FALLBACK_TIMEOUT_S,
    snapshot: Optional[dict] = None,
    now: Optional[float] = None,
) -> list[dict]:
    """The OPEN PRs for ``repo`` — the shared snapshot filtered to
    ``state == 'OPEN'``. ``limit`` bounds the OPEN subset (not the all-state
    list), matching a direct ``gh pr list --state open --limit N``."""
    prs = all_prs(
        repo, max_age_s=max_age_s, live_fallback=live_fallback, limit=None,
        fallback_timeout=fallback_timeout, snapshot=snapshot, now=now,
    )
    open_only = [p for p in prs if str(p.get('state', '')).upper() == OPEN]
    return open_only[:limit] if limit else open_only


def pr(
    repo: str, number: int, *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    live_fallback: bool = False,
    snapshot: Optional[dict] = None,
    now: Optional[float] = None,
) -> Optional[dict]:
    """The snapshot record for a single PR by (repo, number), or None if absent.

    NOTE: this reads the CACHED snapshot; it is NOT the targeted per-PR
    confirmation a consumer should do at the moment of a mutating action (that
    stays a live ``gh pr view <n>`` for point-of-decision freshness)."""
    for p in all_prs(
        repo, max_age_s=max_age_s, live_fallback=live_fallback,
        snapshot=snapshot, now=now,
    ):
        if p.get('number') == number:
            return p
    return None
