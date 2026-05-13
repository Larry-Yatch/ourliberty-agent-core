#!/usr/bin/env python3
"""worktree_manager.py — keyed-reuse worktree creation for agent dispatch.

Phase D3 commit 4b. Combines two upstream patterns:

  - ``agent_runner.create_worktree_for_task`` (mechanics: ``git worktree add
    --detach``, /tmp base, sanitize stem). Upstream timestamps every path so
    each dispatch gets a fresh worktree.
  - ``merge_gates._ensure_pr_worktree`` (reuse-if-exists keyed by a stable
    identifier; tear-down-and-recreate if stale).

For agent-dispatch worktrees we want stable keying so a multi-dispatch task
(preflight → CLARIFY round-trip → build) hits the SAME worktree across all
dispatches: the worktree path is keyed by ``task_id`` (no timestamp). The
``--resume`` session_id chain rests on the worktree state surviving across
dispatches.

Called from ``inbox_watcher.process_task`` when
``models_config[agent].worktree_enabled`` is true. Cleanup is performed
daily by ``cleanup_stale_worktrees.py`` (24h grace).

Public API:

  ``ensure_worktree_for_task(agent_id, task_id, canonical_repo,
                              branch=None, log_fn=None)``
      High-level entry. Returns ``(worktree_path, branch_set_or_None)``.
      Idempotent: safe to call on every dispatch of the same task_id.

  ``worktree_path_for(agent_id, task_id)``
      Deterministic path computation (no filesystem side effects).

  ``setup_branch_checkpoint(worktree_path, branch, task_id, log_fn=None)``
      Pre-create branch + empty WIP commit + push. Idempotent (handles
      "branch already exists" and "nothing to commit" silently).

stdlib only. No state on disk beyond git's own worktree registry.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional, Tuple

# Worktree base. NOT /tmp — the inbox-watcher and outbox-notifier services
# run with systemd `PrivateTmp=yes`, so /tmp is a service-private namespace
# (different services see different /tmps, and the namespace is destroyed
# on every service restart). Persistent home-directory location instead:
#
#   - Visible to every service running as `larry` (no PrivateTmp games).
#   - Survives watcher restarts → task_id-keyed reuse actually persists
#     across CLARIFY round-trips and crash-recovery scenarios.
#   - Cleanup service can reach the same dir.
#
# The directory is auto-created if missing. cleanup_stale_worktrees.py
# scans the same path with the same 24h MAX_AGE_SECONDS grace.
WORKTREE_BASE = Path.home() / 'agent-worktrees'
WORKTREE_BASE.mkdir(parents=True, exist_ok=True)
WORKTREE_PREFIX = 'wt-'

# Defensive cap on task_id length when building filesystem paths.
MAX_TASK_ID_LEN = 50

# Subprocess timeouts. `git fetch` can be slow on first run; worktree
# operations are local and fast.
FETCH_TIMEOUT_SEC = 180
WORKTREE_OP_TIMEOUT_SEC = 60
PUSH_TIMEOUT_SEC = 120

LogFn = Callable[[str], None]


def _sanitize_task_id(task_id: str) -> str:
    """Sanitize a task_id for safe filesystem path use."""
    safe = ''.join(
        c if (c.isalnum() or c in '-_') else '-'
        for c in (task_id or 'task')
    )
    return safe[:MAX_TASK_ID_LEN] or 'task'


def worktree_path_for(agent_id: str, task_id: str) -> Path:
    """Return the deterministic worktree path for an (agent_id, task_id) pair.

    No filesystem side effects. The path is ``/tmp/wt-<agent>-<task_id>/``.
    """
    safe_stem = _sanitize_task_id(task_id)
    return WORKTREE_BASE / f'{WORKTREE_PREFIX}{agent_id}-{safe_stem}'


def derive_branch_name(agent_id: str, task_id: str) -> str:
    """Default branch name when an envelope doesn't set ``branch``.

    Returns ``<agent>/<safe_task_id>``. Matches the in-flight-registry stem
    convention and Forge's expected branch naming per
    ``agents/forge/CLAUDE.md``.
    """
    return f'{agent_id}/{_sanitize_task_id(task_id)}'


def _list_worktrees(canonical_repo: Path) -> list[dict]:
    """Return parsed ``git worktree list --porcelain`` entries.

    Each entry is a dict with optional keys: path, head, branch, detached.
    Returns empty list on any git error — caller treats absence-of-info
    as not-registered.
    """
    try:
        result = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'],
            cwd=str(canonical_repo),
            capture_output=True, text=True, check=True,
            timeout=WORKTREE_OP_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    entries: list[dict] = []
    current: dict = {}
    for line in result.stdout.split('\n'):
        if line.startswith('worktree '):
            if current:
                entries.append(current)
            current = {'path': line[len('worktree '):].strip()}
        elif line.startswith('HEAD '):
            current['head'] = line[len('HEAD '):].strip()
        elif line.startswith('branch '):
            current['branch'] = line[len('branch '):].strip()
        elif line.strip() == 'detached':
            current['detached'] = True
    if current:
        entries.append(current)
    return entries


def _is_worktree_registered(canonical_repo: Path, wt_path: Path) -> bool:
    """True if ``git worktree list`` knows about ``wt_path`` on ``canonical_repo``."""
    target = str(wt_path)
    for entry in _list_worktrees(canonical_repo):
        if entry.get('path') == target:
            return True
    return False


def _remove_worktree(
    canonical_repo: Path,
    wt_path: Path,
    log_fn: Optional[LogFn] = None,
) -> None:
    """Best-effort worktree teardown. Falls back to filesystem rm + git prune.

    Never raises — failures are logged and the caller proceeds (the next
    ``git worktree add`` will surface any persistent problem).
    """
    try:
        subprocess.run(
            ['git', 'worktree', 'remove', '--force', str(wt_path)],
            cwd=str(canonical_repo),
            capture_output=True, text=True, check=True,
            timeout=WORKTREE_OP_TIMEOUT_SEC,
        )
        return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if log_fn:
            log_fn(
                f'git worktree remove failed for {wt_path}: '
                f'{str(e)[:200]}; falling back to filesystem rm + prune'
            )
    except OSError as e:
        if log_fn:
            log_fn(f'git worktree remove OSError for {wt_path}: {e}')

    if wt_path.exists():
        try:
            shutil.rmtree(wt_path, ignore_errors=True)
        except OSError as e:
            if log_fn:
                log_fn(f'fallback rmtree failed for {wt_path}: {e}')
    try:
        subprocess.run(
            ['git', 'worktree', 'prune'],
            cwd=str(canonical_repo),
            capture_output=True, text=True,
            timeout=WORKTREE_OP_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        if log_fn:
            log_fn(f'git worktree prune failed: {str(e)[:200]}')


def create_or_reuse_worktree_for_task(
    agent_id: str,
    task_id: str,
    canonical_repo: Path,
    log_fn: Optional[LogFn] = None,
) -> Optional[Path]:
    """Return the worktree path for (agent_id, task_id), or None on failure.

    Algorithm:

      1. Compute deterministic path = ``/tmp/wt-<agent>-<task_id>/``.
      2. If both (a) the path exists on disk AND (b) ``git worktree list``
         knows about it as a worktree of ``canonical_repo``, reuse it
         (return the path unchanged).
      3. If only (a) holds (stale dir; the registry forgot or the dir was
         created outside of git): remove the dir + prune, then create fresh.
      4. If only (b) holds (orphan registry entry from a vanished dir):
         prune, then create fresh.
      5. If neither holds: ``git fetch origin main`` (best-effort, non-fatal),
         then ``git worktree add --detach <path> origin/main``.

    Returns None on any non-recoverable failure (missing canonical_repo,
    ``git worktree add`` returning non-zero). The caller logs and skips
    dispatch.

    ``log_fn`` is called with informational messages. Tests pass a silent
    callback. Production passes ``inbox_watcher.log``.
    """
    if not canonical_repo.exists():
        if log_fn:
            log_fn(f'canonical_repo missing: {canonical_repo}')
        return None

    wt_path = worktree_path_for(agent_id, task_id)
    on_disk = wt_path.exists()
    registered = _is_worktree_registered(canonical_repo, wt_path)

    if on_disk and registered:
        if log_fn:
            log_fn(f'reusing worktree {wt_path} for {agent_id}/{task_id}')
        return wt_path

    if on_disk and not registered:
        if log_fn:
            log_fn(
                f'stale dir at {wt_path} not registered as worktree; '
                f'removing before recreate'
            )
        _remove_worktree(canonical_repo, wt_path, log_fn=log_fn)

    if registered and not on_disk:
        if log_fn:
            log_fn(
                f'orphan worktree registry entry for {wt_path}; pruning'
            )
        try:
            subprocess.run(
                ['git', 'worktree', 'prune'],
                cwd=str(canonical_repo),
                capture_output=True, text=True,
                timeout=WORKTREE_OP_TIMEOUT_SEC,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            if log_fn:
                log_fn(f'git worktree prune failed: {str(e)[:200]}')

    # Best-effort fetch so the new worktree starts on the freshest base.
    try:
        subprocess.run(
            ['git', 'fetch', 'origin', 'main'],
            cwd=str(canonical_repo),
            capture_output=True, text=True,
            timeout=FETCH_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        if log_fn:
            log_fn(f'git fetch warning before worktree create: {str(e)[:200]}')

    try:
        result = subprocess.run(
            ['git', 'worktree', 'add', '--detach', str(wt_path), 'origin/main'],
            cwd=str(canonical_repo),
            capture_output=True, text=True,
            timeout=FETCH_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        if log_fn:
            log_fn(f'git worktree add raised: {e}')
        return None

    if result.returncode != 0:
        if log_fn:
            log_fn(f'git worktree add failed: {result.stderr[:300]}')
        return None

    if log_fn:
        log_fn(f'created worktree {wt_path} for {agent_id}/{task_id}')
    return wt_path


def setup_branch_checkpoint(
    worktree_path: Path,
    branch: str,
    task_id: str,
    log_fn: Optional[LogFn] = None,
) -> Optional[str]:
    """Pre-create the branch on origin with an empty WIP commit.

    Mirrors ``agent_runner.setup_branch_checkpoint`` but reads ``branch``
    from the task envelope (caller's arg) rather than regex-extracting it
    from the prompt. Idempotent — safe to call on every dispatch of the
    same task_id:

      - ``git checkout -B`` succeeds whether the branch exists or not.
      - ``git commit --allow-empty`` may say "nothing to commit"; treated
        as success.
      - ``git push -u origin <branch>`` may say "everything up-to-date";
        treated as success. Falls back to ``--force-with-lease`` on
        divergence.

    Returns the branch name on success, None on bad input or push failure
    (logged but non-fatal — caller still uses the worktree, just without a
    remote checkpoint).
    """
    if not worktree_path or not worktree_path.exists():
        if log_fn:
            log_fn(f'setup_branch_checkpoint: worktree {worktree_path} missing')
        return None
    if not branch or not isinstance(branch, str) or len(branch) > 200:
        if log_fn:
            log_fn(f'setup_branch_checkpoint: bad branch name {branch!r}')
        return None

    safe_stem = (task_id or 'task')[:60]
    commit_msg = f'[WIP][session-start] {safe_stem}'

    try:
        r1 = subprocess.run(
            ['git', 'checkout', '-B', branch],
            cwd=str(worktree_path),
            capture_output=True, text=True,
            timeout=WORKTREE_OP_TIMEOUT_SEC,
        )
        if r1.returncode != 0:
            if log_fn:
                log_fn(
                    f'setup_branch_checkpoint: checkout -B failed: '
                    f'{r1.stderr[:200]}'
                )
            return None

        # 4b review fix: skip the empty WIP commit when HEAD is already a
        # WIP checkpoint for this task. Without this gate, each re-dispatch
        # of the same task (preflight → CLARIFY → build) stacks another
        # empty commit before any real work — clutters PR history.
        head_subject = subprocess.run(
            ['git', 'log', '-1', '--pretty=%s'],
            cwd=str(worktree_path),
            capture_output=True, text=True,
            timeout=WORKTREE_OP_TIMEOUT_SEC,
        )
        already_checkpointed = (
            head_subject.returncode == 0
            and head_subject.stdout.strip() == commit_msg
        )

        if not already_checkpointed:
            r2 = subprocess.run(
                ['git', 'commit', '--allow-empty', '-m', commit_msg],
                cwd=str(worktree_path),
                capture_output=True, text=True,
                timeout=WORKTREE_OP_TIMEOUT_SEC,
            )
            if (
                r2.returncode != 0
                and 'nothing to commit' not in (r2.stdout + r2.stderr).lower()
            ):
                if log_fn:
                    log_fn(
                        f'setup_branch_checkpoint: empty commit warn: '
                        f'{r2.stderr[:200]}'
                    )

        r3 = subprocess.run(
            ['git', 'push', '-u', 'origin', branch],
            cwd=str(worktree_path),
            capture_output=True, text=True,
            timeout=PUSH_TIMEOUT_SEC,
        )
        if r3.returncode != 0:
            r3b = subprocess.run(
                ['git', 'push', '-u', '--force-with-lease', 'origin', branch],
                cwd=str(worktree_path),
                capture_output=True, text=True,
                timeout=PUSH_TIMEOUT_SEC,
            )
            if r3b.returncode != 0:
                if log_fn:
                    log_fn(
                        f'setup_branch_checkpoint: push failed: '
                        f'{r3.stderr[:200]} | force-with-lease: '
                        f'{r3b.stderr[:200]}'
                    )
                return None

        if log_fn:
            log_fn(f'setup_branch_checkpoint: pushed {branch} with WIP checkpoint')
        return branch
    except (subprocess.TimeoutExpired, OSError) as e:
        if log_fn:
            log_fn(f'setup_branch_checkpoint: exception {e}')
        return None


def ensure_worktree_for_task(
    agent_id: str,
    task_id: str,
    canonical_repo: Path,
    branch: Optional[str] = None,
    log_fn: Optional[LogFn] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """High-level entry: ensure a worktree for (agent_id, task_id) exists,
    and if ``branch`` is set, ensure its checkpoint is on origin.

    Returns ``(worktree_path, branch_set_or_None)``:

      - ``worktree_path`` is None if creation failed; caller skips dispatch.
      - ``branch_set_or_None`` is None if ``branch`` was None or the push
        failed. The worktree path is still returned in the push-failure
        case so dispatch proceeds; Forge will retry push during her build
        phase.

    Idempotent: safe to call every dispatch. Reuses existing worktree for
    matching (agent_id, task_id) and treats branch checkpoint as a no-op
    when already in the right state.
    """
    path = create_or_reuse_worktree_for_task(
        agent_id, task_id, canonical_repo, log_fn=log_fn,
    )
    if path is None:
        return None, None
    if not branch:
        return path, None
    actual_branch = setup_branch_checkpoint(
        path, branch, task_id, log_fn=log_fn,
    )
    return path, actual_branch
