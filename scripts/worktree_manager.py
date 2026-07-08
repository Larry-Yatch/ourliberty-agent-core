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
``models_config[agent].worktree_enabled`` is true. Merged-task worktrees are
reaped immediately by ``outbox_notifier._teardown_worktrees_for_task`` at
auto-merge; the hourly ``cleanup_stale_worktrees.py`` (4h grace) is the GC
backstop for tasks that never merge.

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
# scans the same path with the same 4h MAX_AGE_SECONDS grace.
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
    """Sanitize a task_id for safe filesystem path use.

    Sanitizer architecture (PR-A follow-up, audit #53)
    --------------------------------------------------
    This is the WORKTREE-domain sanitizer and it deliberately diverges from
    the INBOX-domain sanitizer in ``safe_write_inbox.sanitize_component``:

      - The inbox sanitizer neutralizes ONLY path-structural bytes
        (``/ \\`` NUL, control) and PRESERVES other printables (``: @ #``,
        space). It must, because the on-disk inbox name has to stay equal to
        ``f'{task_id}.json'`` — the idempotency readers in ``outbox_notifier``
        and ``heal_pipeline_stall`` rebuild that name from the RAW task_id, so
        a rewrite there would defeat dedup and re-dispatch.

      - This worktree sanitizer maps EVERY non-``[A-Za-z0-9_-]`` char to
        ``-`` and caps length. It can be aggressive because the worktree
        directory name is a DERIVED identifier never reconstructed from the
        task_id: nothing reads the task_id back out of the worktree path.
        Aggressiveness is also desirable — the stem feeds
        ``derive_branch_name`` (``<agent>/<safe_stem>``), and git ref names
        reject ``:``, ``..``, control bytes, etc., so the conservative
        allowlist keeps branch names valid.

    The two domains have different round-trip requirements, so they correctly
    use different rules. Do NOT route this through ``sanitize_component`` —
    that would leak ``:``/``@`` into worktree and branch names.

    INVARIANT: two siblings mirror this exact char mapping + 50-char cap so
    ``heal_abandoned_inbox_tasks.has_active_worker`` can match a task against
    its on-disk ``wt-<agent>-<safe_stem>`` dir: ``agent_runner.
    _worktree_safe_stem`` (the 'main'-agent dispatch path also names worktrees)
    and ``heal_abandoned_inbox_tasks._worktree_safe_stem`` (the matcher). If
    you change the mapping here, change it in BOTH — the three-way consistency
    contract is locked by
    ``test_path_traversal_sanitizer.WorktreeSanitizerConsistencyTest``.
    """
    safe = ''.join(
        c if (c.isalnum() or c in '-_') else '-'
        for c in (task_id or 'task')
    )
    return safe[:MAX_TASK_ID_LEN] or 'task'


def worktree_path_for(agent_id: str, task_id: str) -> Path:
    """Return the deterministic worktree path for an (agent_id, task_id) pair.

    No filesystem side effects. The path is
    ``<WORKTREE_BASE>/wt-<agent>-<safe_task_id>/``. ``_sanitize_task_id``
    strips ``/`` and ``..``, so a hostile task_id cannot traverse out of
    ``WORKTREE_BASE`` (verified by ``test_path_traversal_sanitizer.
    WorktreePathTraversalTest``). NB: ``agent_id`` is NOT sanitized here — it
    is an internal models_config key, never wire-supplied; the traversal
    surface (#53) is the task_id.
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
        # DOUBLE --force: a single --force refuses a worktree carrying git's
        # 'initializing' lock (written during `git worktree add`, cleared only on
        # success); --force --force overrides it. Without this a still-locked
        # worktree silently survives the remove and leaks its .git/worktrees
        # metadata. See test_regression_check.remove_worktree for the same fix.
        subprocess.run(
            ['git', 'worktree', 'remove', '--force', '--force', str(wt_path)],
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
    readonly: bool = False,
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

    ``readonly=True`` is for dispatches that must never mutate
    origin/<branch> — Mirror reviews, where ``branch`` is someone else's
    PR branch. Pushing the WIP commit there moves the PR head SHA, which
    retriggers review dispatch and defeats head-SHA review dedup
    (duplicate round-0 reviews, observed PR #841 2026-07-08), posts
    commit statuses on a throwaway sha, and merges [WIP] noise into
    main's history. Read-only mode does a forced DETACHED checkout of
    origin/<branch>: no WIP commit, no push, and no local branch ref
    either — a ref would collide across worktrees sharing this .git
    (git refuses ``checkout -B`` of a branch checked out elsewhere,
    e.g. Forge's revision worktree on the same PR branch) and invites
    accidental pushes. A branch absent on origin is a refusal (return
    None): for a reviewer that means "nothing to review", and silently
    proceeding would review whatever the worktree happens to contain.

    Returns the branch name on success, None on bad input or push/checkout
    failure (logged but non-fatal — caller decides; see
    ensure_worktree_for_task).
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
        # D3.5 5c-followup-3 (Miss #1): fetch origin/<branch> explicitly
        # BEFORE the local checkout. The worktree was created from
        # origin/main (per create_or_reuse_worktree_for_task), so its HEAD
        # has no knowledge of origin/<branch> if that branch exists with
        # commits we don't yet have locally. Without this fetch, the
        # `git checkout -B branch` below would create a NEW local branch
        # at origin/main's commit — and the subsequent force-with-lease
        # push would silently overwrite origin/<branch>'s real commits
        # (the empty-lease fallback succeeds because we never fetched it).
        # Surfaced on PR #7 2026-05-14: Mirror's worktree wiped Larry's
        # 1594-LOC commit on the branch.
        #
        # Defense-in-depth (PR #11 reviewer §2 + §6): the fetch MUST
        # succeed or we MUST have a fresh-local-only state. A silently-
        # failed fetch combined with a stale `refs/remotes/origin/<branch>`
        # ref from a prior dispatch could base our checkout on stale state,
        # WIP-commit on top, and the subsequent `--force-with-lease`
        # (whose lease references the stale local ref) could succeed if
        # the git version's lease semantics are looser than modern's.
        # Treat fetch failure as a hard refusal — the dispatch returns
        # None, caller logs + skips. Recovering from a transient fetch
        # failure on the NEXT dispatch attempt is preferable to risking
        # a wipe.
        r_fetch = subprocess.run(
            ['git', 'fetch', 'origin', branch],
            cwd=str(worktree_path),
            capture_output=True, text=True,
            timeout=FETCH_TIMEOUT_SEC,
        )
        # `git fetch origin <branch>` returns 128 when the branch doesn't
        # exist on origin (with stderr `couldn't find remote ref ...`).
        # That's NOT an error condition for us — we just want to know
        # whether origin has the branch. Distinguish:
        #   - returncode 0: branch exists on origin, fetched its tip.
        #   - returncode 128 + "couldn't find remote ref": branch doesn't
        #     exist on origin. Fresh-branch dispatch — fall through.
        #   - other non-zero: real fetch failure (network, auth). Refuse.
        fetch_stderr = r_fetch.stderr or ''
        branch_exists_on_origin = (r_fetch.returncode == 0)
        branch_absent_on_origin = (
            r_fetch.returncode != 0
            and "couldn't find remote ref" in fetch_stderr.lower()
        )
        fetch_truly_failed = (
            r_fetch.returncode != 0 and not branch_absent_on_origin
        )
        if fetch_truly_failed:
            if log_fn:
                log_fn(
                    f'setup_branch_checkpoint: fetch origin/{branch} '
                    f'failed (rc={r_fetch.returncode}): '
                    f'{fetch_stderr[:200]} — refusing to checkpoint '
                    f'without remote state knowledge (would risk wiping '
                    f'unseen commits via force-with-lease)'
                )
            return None

        if readonly:
            # See the docstring: detached forced checkout at the origin
            # tip — no local branch ref, no WIP commit, no push. --force
            # absorbs a dirty reused worktree (the reset --hard below
            # covers that for the checkpoint path). Absent branch = refuse.
            if not branch_exists_on_origin:
                if log_fn:
                    log_fn(
                        f'setup_branch_checkpoint: readonly checkout of '
                        f'{branch} refused — branch missing on origin '
                        f'(nothing to review)'
                    )
                return None
            r_ro = subprocess.run(
                ['git', 'checkout', '--force', '--detach',
                 f'origin/{branch}'],
                cwd=str(worktree_path),
                capture_output=True, text=True,
                timeout=WORKTREE_OP_TIMEOUT_SEC,
            )
            if r_ro.returncode != 0:
                if log_fn:
                    log_fn(
                        f'setup_branch_checkpoint: readonly checkout of '
                        f'{branch} failed: {r_ro.stderr[:200]}'
                    )
                return None
            if log_fn:
                log_fn(
                    f'setup_branch_checkpoint: read-only detached checkout '
                    f'of {branch} at origin tip'
                )
            return branch

        # If origin/<branch> exists, base the local branch on it so the
        # WIP commit lands ON TOP of any existing remote work. If it
        # doesn't exist (confirmed by fetch returncode 128), fall through
        # to the legacy "create from current HEAD" behavior — safe because
        # there's nothing on origin to overwrite.
        if branch_exists_on_origin:
            r1 = subprocess.run(
                ['git', 'checkout', '-B', branch, f'origin/{branch}'],
                cwd=str(worktree_path),
                capture_output=True, text=True,
                timeout=WORKTREE_OP_TIMEOUT_SEC,
            )
        else:
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

        # Guarantee the session-start checkpoint commits NO content. The
        # worktree may be REUSED across rounds (create_or_reuse_worktree_for_task
        # returns it unchanged, no reset) and `git checkout -B` PRESERVES
        # non-conflicting local modifications — so a stale working tree (e.g. an
        # old pre-fix copy of a file left by a prior session) survives into the
        # `git commit --allow-empty` below. `--allow-empty` commits a DIRTY index,
        # not just an empty tree, so that snapshot silently REVERTS the branch tip
        # and clobbers mid-review work — the #731 revision-loop class. Hard-reset
        # to the checkout target so index+worktree exactly match it; safe because
        # this runs at session START (before any work) and the round's prior work
        # is already committed on origin/<branch>.
        reset_target = f'origin/{branch}' if branch_exists_on_origin else 'HEAD'
        r_reset = subprocess.run(
            ['git', 'reset', '--hard', reset_target],
            cwd=str(worktree_path),
            capture_output=True, text=True,
            timeout=WORKTREE_OP_TIMEOUT_SEC,
        )
        if r_reset.returncode != 0:
            if log_fn:
                log_fn(
                    f'setup_branch_checkpoint: reset --hard {reset_target} '
                    f'failed: {r_reset.stderr[:200]}'
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
            elif r2.returncode == 0:
                # Defense in depth: a session-start checkpoint MUST be empty
                # (tree == parent). The reset above guarantees this; if it ever
                # didn't and `--allow-empty` captured a dirty index, pushing the
                # commit would REVERT the branch tip. Drop it locally and refuse
                # to push rather than clobber.
                r_verify = subprocess.run(
                    ['git', 'diff', '--quiet', 'HEAD~1', 'HEAD'],
                    cwd=str(worktree_path),
                    capture_output=True, text=True,
                    timeout=WORKTREE_OP_TIMEOUT_SEC,
                )
                if r_verify.returncode != 0:
                    if log_fn:
                        log_fn(
                            'setup_branch_checkpoint: REFUSING to push a '
                            'non-empty [WIP][session-start] checkpoint (would '
                            'revert branch tip); dropped it locally'
                        )
                    subprocess.run(
                        ['git', 'reset', '--hard', 'HEAD~1'],
                        cwd=str(worktree_path),
                        capture_output=True, text=True,
                        timeout=WORKTREE_OP_TIMEOUT_SEC,
                    )
                    return None

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
    readonly: bool = False,
) -> Tuple[Optional[Path], Optional[str]]:
    """High-level entry: ensure a worktree for (agent_id, task_id) exists,
    and if ``branch`` is set, ensure its checkpoint is on origin.

    Returns ``(worktree_path, branch_set_or_None)``:

      - ``worktree_path`` is None if creation failed; caller skips dispatch.
      - ``branch_set_or_None`` is None if ``branch`` was None or the
        checkpoint push / readonly checkout failed. The worktree path is
        still returned in that case; the CALLER decides whether a None
        branch is tolerable (Forge retries the push during her build
        phase) or a dispatch blocker (a Mirror review must not run
        against the wrong tree — see inbox_watcher).

    ``readonly=True`` selects the read-only detached checkout of
    origin/<branch> (no WIP commit, no push, no local branch ref) — for
    Mirror reviews, where ``branch`` is the PR branch under review; see
    setup_branch_checkpoint.

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
        path, branch, task_id, log_fn=log_fn, readonly=readonly,
    )
    return path, actual_branch
