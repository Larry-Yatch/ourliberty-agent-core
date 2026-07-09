#!/usr/bin/env python3
"""gh_budget.py — shared GitHub rate-limit budget helper (gh-api-burn phase 1).

The chain shares ONE GitHub token across ~two dozen timer-driven daemons that each
call ``gh pr list`` / ``gh pr view`` (GraphQL) every few minutes against the same PR
repos. That exhausts the ~5000 pts/hr GraphQL budget hourly, and once it does the
safety-critical healers (branch cleanup, auto-merge, stall detection) fail with
'GraphQL: API rate limit already exceeded' and skip their run.

This module lets a heavy caller cheaply ask "is there budget left?" BEFORE firing
its data queries, so it can back off (skip its run, logged) instead of deepening the
exhaustion. The durable fix (a shared cached open-PR snapshot) is phase 2 and is NOT
here — this is the stop-the-bleeding backoff guard.

The budget read uses ``gh api rate_limit`` — the ONE gh call that does NOT consume
the GraphQL budget (it is free), so checking is cheap and the sampler can poll it
often. Results are cached (in-process first, then a best-effort ~60s on-disk cache)
so many callers firing in the same timer tick don't each re-spawn the query.

Fail-OPEN by contract: if ``gh api rate_limit`` errors (gh missing, auth expired,
network, unparseable output) ``remaining()`` returns ``{}`` and ``budget_ok()``
returns ``True``. A broken budget check must NEVER wedge a healer — unknown budget
is treated as OK. stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
CACHE_FILE = AGENTS_ROOT / 'blackboard' / 'gh-budget-cache.json'

# gh api rate_limit is free, but re-spawning it from every caller in a tick is
# wasteful; a short TTL dedups without letting the reading go stale enough to
# matter for a coarse budget gate.
CACHE_TTL_SEC = 60.0
DEFAULT_TIMEOUT_SEC = 20.0
# A GraphQL PR-state poll costs ~1 pt/PR; a healer scanning a few repos of open
# PRs wants comfortable headroom, not the last drops. 500 leaves room for the
# in-flight callers already mid-query when a low reading lands.
DEFAULT_MIN_GRAPHQL = 500

# In-process cache: {'ts': epoch_seconds, 'data': dict}. Reset via _reset_cache().
_CACHE: dict[str, Any] = {}


def _log(msg: str) -> None:
    print(f'[gh_budget] {msg}', file=sys.stderr, flush=True)


def _reset_cache() -> None:
    """Clear the in-process cache and remove the on-disk cache. Test helper —
    production never needs this (a fresh process starts with an empty cache)."""
    _CACHE.clear()
    try:
        CACHE_FILE.unlink()
    except OSError:
        pass


def _read_disk_cache(now: float) -> Optional[dict]:
    try:
        raw = json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return None
    ts = raw.get('ts') if isinstance(raw, dict) else None
    data = raw.get('data') if isinstance(raw, dict) else None
    if not isinstance(ts, (int, float)) or not isinstance(data, dict):
        return None
    if now - ts > CACHE_TTL_SEC:
        return None
    return data


def _write_disk_cache(now: float, data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({'ts': now, 'data': data}))
        tmp.replace(CACHE_FILE)
    except OSError:
        pass  # best-effort — a non-writable cache just means callers re-query


def _parse_rate_limit(payload: Any) -> dict:
    """Reduce the `gh api rate_limit` JSON to the fields callers care about.

    Shape: {'graphql': {'remaining','limit','reset'}, 'rest': {...}}. Returns {}
    if the payload is missing the resources we need (treated as unknown budget)."""
    if not isinstance(payload, dict):
        return {}
    resources = payload.get('resources')
    if not isinstance(resources, dict):
        return {}
    out: dict[str, dict] = {}
    for key in ('graphql', 'core'):
        block = resources.get(key)
        if not isinstance(block, dict):
            continue
        rem = block.get('remaining')
        lim = block.get('limit')
        reset = block.get('reset')
        if not isinstance(rem, int) or not isinstance(lim, int):
            continue
        # Expose 'core' under the friendlier 'rest' name the sampler/analyzer use.
        out['rest' if key == 'core' else key] = {
            'remaining': rem,
            'limit': lim,
            'reset': reset if isinstance(reset, int) else None,
        }
    return out


def remaining(*, timeout: float = DEFAULT_TIMEOUT_SEC,
              now: Optional[float] = None) -> dict:
    """Current GraphQL + REST rate-limit budget, via the FREE `gh api rate_limit`.

    Returns ``{'graphql': {'remaining','limit','reset'}, 'rest': {...}}`` on
    success (either or both keys may be absent if GitHub omits that resource), or
    ``{}`` when the budget cannot be read (fail-open signal: unknown == OK).

    Cached ~60s: an in-process hit returns immediately; otherwise a fresh on-disk
    cache is adopted; otherwise the query runs and both caches are refreshed. A
    failed query is NOT cached, so the next caller retries rather than inheriting
    a spurious empty reading."""
    now = time.time() if now is None else now

    cached_ts = _CACHE.get('ts')
    if isinstance(cached_ts, (int, float)) and now - cached_ts <= CACHE_TTL_SEC:
        return dict(_CACHE.get('data') or {})

    disk = _read_disk_cache(now)
    if disk is not None:
        _CACHE['ts'] = now
        _CACHE['data'] = disk
        return dict(disk)

    try:
        proc = subprocess.run(
            ['gh', 'api', 'rate_limit'],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        _log(f'rate_limit query failed ({type(e).__name__}); treating budget as '
             'unknown (fail-open)')
        return {}
    if proc.returncode != 0:
        _log(f'rate_limit query rc={proc.returncode}: {proc.stderr[:200]}; '
             'treating budget as unknown (fail-open)')
        return {}
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError):
        _log('rate_limit query returned unparseable JSON; treating budget as '
             'unknown (fail-open)')
        return {}

    data = _parse_rate_limit(payload)
    if not data:
        return {}
    _CACHE['ts'] = now
    _CACHE['data'] = data
    _write_disk_cache(now, data)
    return dict(data)


def budget_ok(min_graphql: int = DEFAULT_MIN_GRAPHQL, *,
              timeout: float = DEFAULT_TIMEOUT_SEC) -> bool:
    """True if the GraphQL budget has at least ``min_graphql`` points remaining.

    Fail-OPEN: if the budget can't be read, or the graphql resource is absent from
    the reading, returns True. A budget check must never be the reason a healer
    skips — only a confidently-low reading returns False."""
    data = remaining(timeout=timeout)
    graphql = data.get('graphql') if isinstance(data, dict) else None
    if not isinstance(graphql, dict):
        return True  # unknown budget -> proceed
    rem = graphql.get('remaining')
    if not isinstance(rem, int):
        return True
    return rem >= min_graphql


def describe(data: Optional[dict] = None) -> str:
    """A compact 'graphql <rem>/<lim>, resets <iso>' string for a skip-log line.

    Unknown budget renders as 'graphql budget unknown'."""
    data = remaining() if data is None else data
    graphql = data.get('graphql') if isinstance(data, dict) else None
    if not isinstance(graphql, dict):
        return 'graphql budget unknown'
    rem = graphql.get('remaining')
    lim = graphql.get('limit')
    reset = graphql.get('reset')
    reset_str = ''
    if isinstance(reset, int):
        try:
            from datetime import datetime, timezone
            reset_str = (', resets '
                         + datetime.fromtimestamp(reset, timezone.utc).isoformat())
        except (OverflowError, OSError, ValueError):
            reset_str = ''
    return f'graphql {rem}/{lim}{reset_str}'


def should_skip(caller: str, min_graphql: int = DEFAULT_MIN_GRAPHQL, *,
                log=None) -> bool:
    """Backoff-guard convenience for a heavy caller's entry point.

    Returns True and logs a clear one-line reason when the GraphQL budget is too
    low to safely fire ``caller``'s data queries; returns False (proceed) when the
    budget is healthy OR unknown (fail-open). ``log`` is an optional
    ``log(msg)``/``log(msg, level)`` callable (the healer's own logger); falls back
    to stderr. The check itself never raises — any internal error means proceed."""
    try:
        if budget_ok(min_graphql):
            return False
        reason = (f'skipping this run: GraphQL budget low '
                  f'({describe()}), min={min_graphql}')
    except Exception as e:  # noqa: BLE001 — a broken guard must never wedge a caller
        _log(f'{caller}: budget guard errored ({type(e).__name__}); proceeding '
             '(fail-open)')
        return False
    msg = f'{caller}: {reason}'
    if log is not None:
        try:
            log(msg)
        except TypeError:
            log(msg, 'INFO')
        except Exception:  # noqa: BLE001
            _log(msg)
    else:
        _log(msg)
    return True


if __name__ == '__main__':
    print(json.dumps(remaining(), indent=2))
    print(f'budget_ok(): {budget_ok()}', file=sys.stderr)
