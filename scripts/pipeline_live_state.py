#!/usr/bin/env python3
"""pipeline_live_state.py — the canonical "is this pipeline work live RIGHT
NOW?" primitive.

WHY THIS MODULE EXISTS
----------------------
Multiple healers independently re-derive "is X live" from /proc + the
filesystem + dispatch leases, each hand-rolling the same boundary-matching and
fail-safe discipline. That duplication is exactly the drift class that bites us:
a boundary guard fixed in one probe but not its twin silently re-introduces the
false positive the fix was meant to kill. This module consolidates those
per-healer probes into ONE source of truth.

Provenance of the consolidated probes:
  - `pr_review_in_progress` is the EXACT port of #716's
    `heal_pipeline_stall._mirror_session_active_for_pr` (the per-PR "is a Mirror
    review live or freshly dispatched?" probe). heal_pipeline_stall now
    delegates to it; heal_undispatched_pr_review consumes it as a re-dispatch
    guard.
  - `agent_dispatch_live` is the per-agent liveness probe (a held dispatch
    lease whose holder is alive on this boot). #717's watchdog
    `_any_live_dispatch_lease` should migrate to this as a fast-follow once #717
    merges (NOT part of the PR that introduced this module).

CONTRACT (every public function obeys this)
-------------------------------------------
  - PURE READS ONLY: /proc scans, filesystem globs, and dispatch-lease reads.
    ZERO `claude`/LLM subprocess calls. `pgrep`/`os.readlink` are process
    listing, not LLM invocations.
  - FAIL-SAFE: any exception is swallowed and treated as "not live"
    (returns `(False, '')`). A probe that itself errors must NEVER cause a
    caller to suppress a legitimate alert or block a legitimate dispatch.
  - RETURN SHAPE is `tuple[bool, str]` = `(active, reason)`; `reason` is `''`
    whenever `active` is False. The reason string is a stable forensic
    identifier callers log (e.g. `MIRROR_ACTIVE_SKIP reason=live_pid`).
  - TEST-SAFE: this module only READS. The read helpers (`_claude_pids`,
    `_proc_cwd`, `dispatch_lease.is_held`) do NOT call
    `test_isolation_guard.refuse_under_test` — only lease WRITE/reclaim paths
    do — so importing and calling this module under a test process is safe.

The root constants (`WORKTREES_ROOT`, `AGENTS_ROOT`) are env-overridable
(`OURLIBERTY_WORKTREES_ROOT` / `OURLIBERTY_AGENTS_ROOT`) and resolved AT CALL
TIME, matching heal_pipeline_stall / heal_phantom_dispatch_claim conventions so
a test can redirect them without re-importing this module.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dispatch_lease  # noqa: E402  # per-agent liveness via held lease

HOME = Path.home()

# Production review worktrees live under ~/agent-worktrees (NOT under
# AGENTS_ROOT). Env-overridable for tests, matching heal_phantom_dispatch_claim
# / heal_pipeline_stall conventions. These module-level constants document the
# production defaults; the accessors below re-read the env at CALL time so a
# test that sets the env after import still redirects the probe.
WORKTREES_ROOT = Path(
    os.environ.get('OURLIBERTY_WORKTREES_ROOT', str(HOME / 'agent-worktrees'))
)
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))

# The kernel appends this to /proc/<pid>/cwd when a still-running process's
# working directory (here, a Mirror review worktree) was removed out from under
# it. A live mid-review session still reads cleanly; we strip the suffix so a
# torn-down cwd is name-matched the same way.
_DELETED_CWD_SUFFIX = ' (deleted)'

# Matches the remainder of a Mirror review worktree name AFTER the
# `wt-mirror-pr-<repo_short>-` prefix is stripped: a leading integer PR number,
# optionally followed by a single `-rev<N>` or `-replan<N>` revision suffix and
# nothing else. Anchored both ends so `713` never matches a `7130` PR (and vice
# versa) — the boundary discipline the FP report demands.
_MIRROR_WT_REMAINDER_RE = re.compile(r'^(\d+)(?:-(?:rev|replan)\d+)?$')


def _worktrees_root() -> Path:
    """WORKTREES_ROOT resolved at call time (env-overridable for tests)."""
    return Path(
        os.environ.get('OURLIBERTY_WORKTREES_ROOT', str(WORKTREES_ROOT))
    )


def _agents_root() -> Path:
    """AGENTS_ROOT resolved at call time (env-overridable for tests)."""
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(AGENTS_ROOT)))


def _claude_pids() -> list[int]:
    """All live `claude` PIDs (broad match; cwd filtering happens in the
    caller). `pgrep` is NOT a `claude`/LLM subprocess — pure process listing,
    same shape as heal_wedged_review_sessions.claude_pids."""
    try:
        out = subprocess.run(
            ['pgrep', '-f', 'claude'],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0:
        return []
    return [int(s) for s in out.stdout.split() if s.isdigit()]


def _proc_cwd(pid: int) -> Optional[str]:
    """The cwd of `pid` via /proc, or None if unreadable (race/permission)."""
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except OSError:
        return None


def pr_review_in_progress(repo_short: str, pr_number: int) -> tuple[bool, str]:
    """Is a Mirror review session live (or freshly dispatched) for this PR?

    EXACT port of #716's `heal_pipeline_stall._mirror_session_active_for_pr`.

    Two independent, cheap, claude-subprocess-free signals — either true =>
    active:

      live_pid: a `claude` process whose cwd sits inside a
        `wt-mirror-pr-<repo_short>-<pr_number>` worktree (tolerating a trailing
        `-rev<N>`/`-replan<N>` and the kernel `(deleted)` cwd suffix),
        boundary-matched on the PR-number segment so #713 never matches #7130.
      inbox_task_present: a dispatched review task file
        `review-pr-<repo_short>-<pr_number>[...]` sits in Mirror's inbox, with
        the same boundary guard (the char after the PR number must be the `.`
        extension dot or a `-` revision dash).

    Returns (active, reason); reason is '' when inactive. Fail-safe: any
    unexpected exception is swallowed and treated as "not live".
    """
    try:
        worktrees_root = _worktrees_root()
        wt_prefix = str(worktrees_root / 'wt-mirror-')
        name_prefix = f'wt-mirror-pr-{repo_short}-'
        for pid in _claude_pids():
            cwd = _proc_cwd(pid)
            if not cwd:
                continue
            if cwd.endswith(_DELETED_CWD_SUFFIX):
                cwd = cwd[: -len(_DELETED_CWD_SUFFIX)]
            if not cwd.startswith(wt_prefix):
                continue
            # The worktree name is the first path segment under WORKTREES_ROOT
            # (a review session's cwd is the worktree root, but slicing the
            # first segment also tolerates a nested cwd).
            rel = cwd[len(str(worktrees_root)):].lstrip('/')
            worktree_name = rel.split('/', 1)[0]
            if not worktree_name.startswith(name_prefix):
                continue
            remainder = worktree_name[len(name_prefix):]
            m = _MIRROR_WT_REMAINDER_RE.match(remainder)
            if m and int(m.group(1)) == pr_number:
                return True, 'live_pid'

        mirror_inbox = _agents_root() / 'inboxes' / 'mirror'
        file_prefix = f'review-pr-{repo_short}-{pr_number}'
        try:
            hits = mirror_inbox.glob(f'{file_prefix}*.json')
        except OSError:
            hits = iter(())
        for hit in hits:
            # Boundary-guard the glob's trailing `*` so a `7130` task never
            # suppresses a `713` PR: the char after the PR number must be the
            # extension dot or a revision-suffix dash.
            rest = hit.name[len(file_prefix):]
            if rest.startswith('.') or rest.startswith('-'):
                return True, 'inbox_task_present'

        return False, ''
    except Exception:  # noqa: BLE001 — a probe error must never be read as "live"
        return False, ''


def agent_dispatch_live(agent: str) -> tuple[bool, str]:
    """Is `agent` mid-dispatch right now (a held, alive dispatch lease)?

    Per-agent liveness via `dispatch_lease`: True iff a lease is held for the
    identity `inbox:<agent>` whose holder PID is alive on the same boot (exactly
    the "held" notion `dispatch_lease.is_held` enforces — within TTL +
    `_pid_alive_same_boot`). Reason 'lease_held'.

    Leases are written even in 'shadow' mode, so this is a reliable signal there
    too; in 'off' mode no lease files exist and `is_held` is always False, so
    this returns (False, '').

    This is the signal #717's watchdog `_any_live_dispatch_lease` should migrate
    to as a fast-follow. Fail-safe: any exception (or leases-off) → (False, '').
    """
    try:
        if dispatch_lease.is_held(f'inbox:{agent}'):
            return True, 'lease_held'
        return False, ''
    except Exception:  # noqa: BLE001 — a probe error must never be read as "live"
        return False, ''
