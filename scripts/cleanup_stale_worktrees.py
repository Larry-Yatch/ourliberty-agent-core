#!/usr/bin/env python3
"""cleanup_stale_worktrees.py — Remove git worktrees older than 4 hours.

Runs hourly via systemd timer (ourliberty-cleanup-stale-worktrees) as the GC
backstop for worktrees that never auto-merge (REJECT/ESCALATE/abandonment) —
merged-task worktrees are reaped immediately by outbox_notifier's event-driven
teardown. Cleans up ``~/agent-worktrees/wt-*`` directories left behind by
Forge dispatches. Worktrees are preserved for 4 hours after task completion so
Larry can inspect the working state if anything went wrong.

Adapted from GrowthMastery-ai/gm-agent-core ``scripts/cleanup_stale_worktrees.py``
for Larry-Yatch/ourliberty-agent-core (2026-05-12, Phase D3 commit 4b — Gap 10
in docs/upstream-audit.md). Adaptations:

  - paths joe→larry; gm-agents→agents; growth-mastery→agent-core
  - canonical repo list (not a single REPO_DIR) so multi-repo expansion
    is one config edit when ``allowed_repos`` in agent-models.json grows
    beyond ``ourliberty-agent-core``.
  - 4b followup #2: worktree base ``~/agent-worktrees/`` instead of upstream's
    ``/tmp/wt-`` — see worktree_manager.py for the PrivateTmp namespace
    rationale.
  - task-30: logical-name → filesystem-path mapping lives in the top-level
    ``repo_paths`` block of ``config/agent-models.json``; loaded lazily via
    ``_load_canonical_repos()`` (fail-loud on missing block or paths outside
    ``/home/larry/``).

stdlib only.
"""
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo scripts dir on sys.path so the sibling id_match import resolves cleanly
# when invoked by systemd. id_match is itself stdlib-only (re), so this preserves
# the module's stdlib-only contract.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from id_match import id_matches  # noqa: E402

_MODELS_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'
_REPO_PATHS_CACHE: list[Path] | None = None


def _load_canonical_repos() -> list[Path]:
    """Return the canonical repo Paths from ``config/agent-models.json``.

    Reads the top-level ``repo_paths`` block once and caches the result.
    Raises ``RuntimeError`` (fail-loud) when the block is missing or any
    value is non-absolute or escapes ``/home/larry/`` (the systemd sandbox
    ReadWritePaths constraint — paths outside this tree would fail later
    at ``git worktree`` invocation anyway).
    """
    global _REPO_PATHS_CACHE
    if _REPO_PATHS_CACHE is not None:
        return _REPO_PATHS_CACHE
    try:
        cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"could not read {_MODELS_CONFIG_PATH}: {e} — cannot resolve "
            "canonical repo paths"
        ) from e
    block = cfg.get('repo_paths') if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or not block:
        raise RuntimeError(
            "config/agent-models.json missing required 'repo_paths' block — "
            "cannot resolve canonical repo paths"
        )
    repos: list[Path] = []
    for name, raw in block.items():
        if not isinstance(raw, str) or not raw.startswith('/home/larry/'):
            raise RuntimeError(
                f"config/agent-models.json repo_paths[{name!r}]={raw!r} must "
                "be an absolute path under /home/larry/ (matches systemd "
                "ReadWritePaths)"
            )
        repos.append(Path(raw))
    _REPO_PATHS_CACHE = repos
    return repos

# Base dir holding all managed worktrees. Mirrors worktree_manager.WORKTREE_BASE
# (kept as a local literal to preserve this module's stdlib-only / import-free
# contract). MANAGED_WORKTREE_PREFIX is WORKTREE_BASE joined with the 'wt-' name
# prefix worktree_manager.WORKTREE_PREFIX uses.
WORKTREE_BASE = Path('/home/larry/agent-worktrees')
MANAGED_WORKTREE_PREFIX = '/home/larry/agent-worktrees/wt-'

LOG_FILE = Path('/home/larry/agents/logs/worktree-cleanup.log')
IN_FLIGHT_DIR = Path('/home/larry/agents/state/in-flight')
MAX_AGE_SECONDS = 14400  # 4 hours — tightened 2026-05-25 per Pulse off-cycle-inv finding; in-flight guard at load_active_task_stems() prevents collateral removal of active worktrees
# Orphan dirs (de-registered from git but physically present) get a longer grace
# window than registered worktrees: git losing track of a worktree is a
# lower-context signal than a normal completed build, and 24h is well past any
# legitimate in-flight build.
ORPHAN_MAX_AGE_SECONDS = 86400  # 24 hours
GIT_TIMEOUT_SEC = 60


def load_active_task_stems() -> set[str]:
    """Read the in-flight registry to know which task_stems are mid-dispatch.

    A worktree path whose name contains any of these stems is still in use
    by a live agent_runner.run_claude subprocess. The 24h mtime gate isn't
    enough on its own — a build that's mostly Read-heavy doesn't update
    mtime, so the cleanup script would otherwise remove an actively-in-use
    worktree.
    """
    stems: set[str] = set()
    if not IN_FLIGHT_DIR.exists():
        return stems
    for f in IN_FLIGHT_DIR.glob('*.json'):
        try:
            entry = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        stem = entry.get('task_stem') or f.stem
        if stem:
            stems.add(stem)
    return stems


def log(msg: str) -> None:
    line = f'[{datetime.now(timezone.utc).isoformat()}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def list_worktrees(canonical_repo: Path) -> list[dict]:
    """Parse ``git worktree list --porcelain`` for one canonical repo."""
    try:
        result = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'],
            cwd=str(canonical_repo),
            capture_output=True, text=True, check=True,
            timeout=GIT_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log(f'git worktree list failed for {canonical_repo}: {str(e)[:200]}')
        return []

    worktrees: list[dict] = []
    current: dict = {}
    for line in result.stdout.split('\n'):
        if line.startswith('worktree '):
            if current:
                worktrees.append(current)
            current = {'path': line[len('worktree '):].strip()}
        elif line.startswith('HEAD '):
            current['head'] = line[len('HEAD '):].strip()
        elif line.startswith('branch '):
            current['branch'] = line[len('branch '):].strip()
        elif line.strip() == 'detached':
            current['detached'] = True
    if current:
        worktrees.append(current)
    return worktrees


def remove_worktree(canonical_repo: Path, path: str) -> bool:
    """Remove a worktree (registry + filesystem). Returns True on success."""
    try:
        subprocess.run(
            ['git', 'worktree', 'remove', '--force', path],
            cwd=str(canonical_repo),
            capture_output=True, text=True, check=True,
            timeout=GIT_TIMEOUT_SEC,
        )
        log(f'Removed worktree: {path}')
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log(f'git worktree remove failed: {str(e)[:200]} — trying force cleanup')
        try:
            if Path(path).exists():
                shutil.rmtree(path, ignore_errors=True)
            subprocess.run(
                ['git', 'worktree', 'prune'],
                cwd=str(canonical_repo),
                capture_output=True, text=True,
                timeout=GIT_TIMEOUT_SEC,
            )
            log(f'Force-removed: {path}')
            return True
        except (OSError, subprocess.TimeoutExpired) as e2:
            log(f'Force cleanup also failed: {e2}')
            return False


def sweep_canonical(
    canonical_repo: Path, active_stems: set[str]
) -> tuple[int, int, set[str]]:
    """Process one canonical repo.

    Returns ``(removed, kept, registered_paths)`` where ``registered_paths`` is
    the set of resolved-path strings for every worktree git reported for this
    repo. ``main()`` aggregates that set across all canonical repos so the orphan
    sweep can skip any dir git still tracks (handled here by sweep_canonical).
    """
    if not canonical_repo.exists():
        log(f'canonical_repo missing, skipping: {canonical_repo}')
        return 0, 0, set()

    worktrees = list_worktrees(canonical_repo)
    log(f'{canonical_repo}: found {len(worktrees)} worktree(s)')

    removed = 0
    kept = 0
    registered_paths: set[str] = set()
    now = time.time()

    for wt in worktrees:
        path = wt.get('path', '')
        if path:
            registered_paths.add(str(Path(path).resolve()))
        # Skip the canonical itself and any worktree that isn't one of ours.
        if path == str(canonical_repo) or MANAGED_WORKTREE_PREFIX not in path:
            kept += 1
            continue

        # 4b review fix: skip worktrees referenced by the in-flight registry.
        # A long Read-heavy build doesn't touch the worktree's mtime, so the
        # 24h gate isn't enough on its own — without this check the cleanup
        # could remove an actively-in-use worktree.
        #
        # audit #12 (PR-B id-match discipline): match the stem against the
        # structured ``wt-<agent>-<stem>-<ts>`` basename on token boundaries,
        # not as a bare substring — a short stem 'b' must not match the infix of
        # 'wt-forge-rebuild-...'. min_len=1: the basename is structured (boundary
        # alone disambiguates), so no length floor. Fail-safe: a non-match only
        # happens when the stem genuinely isn't this worktree's, so the keep
        # guard never wrongly drops an active worktree.
        if any(id_matches(stem, Path(path).name, min_len=1) for stem in active_stems):
            log(f'Keeping {path} — in-flight task active')
            kept += 1
            continue

        if not Path(path).exists():
            # Stale registry entry — prune.
            try:
                subprocess.run(
                    ['git', 'worktree', 'prune'],
                    cwd=str(canonical_repo),
                    capture_output=True, text=True,
                    timeout=GIT_TIMEOUT_SEC,
                )
                log(f'Pruned orphan: {path}')
                removed += 1
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                log(f'Prune orphan failed for {path}: {str(e)[:200]}')
                kept += 1
            continue

        try:
            mtime = Path(path).stat().st_mtime
            age = now - mtime
            if age > MAX_AGE_SECONDS:
                if remove_worktree(canonical_repo, path):
                    removed += 1
                else:
                    kept += 1
            else:
                hours = age / 3600
                log(f'Keeping {path} (age: {hours:.1f}h)')
                kept += 1
        except OSError as e:
            log(f'Error checking {path}: {e}')
            kept += 1

    return removed, kept, registered_paths


def sweep_orphan_dirs(
    registered_paths: set[str], active_stems: set[str]
) -> tuple[int, int]:
    """Reap ``wt-*`` dirs under WORKTREE_BASE that git no longer tracks.

    ``sweep_canonical`` only sees paths git reports via ``git worktree list``;
    a dir de-registered by ``git worktree remove``/``prune`` whose filesystem
    directory persists is invisible to that sweep and accumulates forever. This
    closes that gap.

    Removal here is a filesystem ``rmtree``, NOT ``git worktree remove``, because
    the dir is already de-registered. Three independent guards protect a live
    worktree: the registered-set membership check, the in-flight-stem guard, and
    the 24h age floor. Returns ``(removed, kept)``.
    """
    if not WORKTREE_BASE.exists():
        return 0, 0

    removed = 0
    kept = 0
    now = time.time()

    for child in sorted(WORKTREE_BASE.iterdir()):
        if not child.is_dir() or not child.name.startswith('wt-'):
            continue

        resolved = str(child.resolve())
        # git already tracks this dir — sweep_canonical owns it.
        if resolved in registered_paths:
            continue

        # Same in-flight guard sweep_canonical uses: a live agent_runner
        # subprocess may hold this worktree even if mtime is stale. Boundary
        # match (audit #12) against the structured basename, min_len=1.
        if any(id_matches(stem, child.name, min_len=1) for stem in active_stems):
            log(f'Keeping orphan candidate {child} — in-flight task active')
            kept += 1
            continue

        try:
            age = now - child.stat().st_mtime
        except OSError as e:
            log(f'Error checking orphan candidate {child}: {e}')
            kept += 1
            continue

        hours = age / 3600
        if age > ORPHAN_MAX_AGE_SECONDS:
            shutil.rmtree(child, ignore_errors=True)
            log(
                f'Removed orphan dir (not git-registered): {child} '
                f'(age {hours:.1f}h)'
            )
            removed += 1
        else:
            log(f'Keeping orphan candidate {child} (age: {hours:.1f}h)')
            kept += 1

    return removed, kept


def _worktrees_metadata_dir(canonical_repo: Path) -> Path | None:
    """Return the canonical repo's ``.git/worktrees`` dir, or None if unresolvable.

    Resolved via ``git rev-parse --git-common-dir`` so it is correct whether the
    repo's ``.git`` is a directory (a normal checkout) or a file (the repo is
    itself a worktree). ``--git-common-dir`` does NOT load refs, so it succeeds
    even when a leaked zero-HEAD entry has corrupted the ref machinery (that
    corruption is exactly what this reaper exists to remove).
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--git-common-dir'],
            cwd=str(canonical_repo),
            capture_output=True, text=True, check=True,
            timeout=GIT_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log(f'git rev-parse --git-common-dir failed for {canonical_repo}: {str(e)[:200]}')
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (canonical_repo / common).resolve()
    return common / 'worktrees'


def _orphan_working_dir(entry: Path) -> Path | None:
    """Return the entry's recorded working dir IFF it no longer exists on disk.

    Each ``.git/worktrees/<name>/gitdir`` file records the absolute path of the
    worktree's ``.git`` file (e.g. ``/tmp/test-regression-check-X/gate-wt-Y/.git``);
    the working dir is its parent. Returns that path only when it is absent (the
    orphan signal). Returns None when the working dir still exists (a LIVE run —
    never reap) or when the gitdir file can't be read (fail-safe: an entry we
    can't characterize is left alone, never reaped).
    """
    try:
        gitdir = (entry / 'gitdir').read_text().strip()
    except OSError:
        return None
    if not gitdir:
        return None
    working_dir = Path(gitdir).parent
    if working_dir.exists():
        return None
    return working_dir


def sweep_orphan_locked_worktrees(canonical_repo: Path) -> tuple[int, int]:
    """Reap leaked ``.git/worktrees/*`` entries whose working dir is gone.

    The regression gate / baseline warmer create detached worktrees under /tmp via
    ``git worktree add``, which writes a ``locked`` file (reason 'initializing')
    for the duration of the add and removes it only on success. If the process is
    SIGKILLed mid-init (OOM, per-SHA timeout, the zombie/wedged-session healer) the
    lock survives. ``git worktree prune`` SKIPS locked entries, and the in-process
    teardown never runs on a kill — so the metadata accumulates forever. An entry
    killed early enough to leave an all-zero HEAD corrupts ``git fetch``
    (``fatal: bad object worktrees/<name>/HEAD``), blocking every push to
    origin/main. This is the load-bearing reaper for that kill-mid-init case: no
    in-process ``finally`` can cover a SIGKILL.

    For each entry whose recorded working dir (from its ``gitdir`` file) no longer
    exists, clear any stale ``locked`` file then ``git worktree prune`` to drop the
    entry; if prune leaves the entry (e.g. a git version that loads the corrupt
    HEAD while pruning), rmtree the metadata dir directly as the backstop that
    guarantees the corrupting entry is gone.

    SAFETY INVARIANT (non-negotiable): an entry whose working dir STILL EXISTS is
    NEVER touched. A live gate run under /tmp/test-regression-check-* legitimately
    holds an 'initializing' lock during checkout; clearing that lock or pruning the
    entry would corrupt the in-flight run. Scope is strictly orphans (working dir
    absent) — the enforcement of that rule is _orphan_working_dir returning None
    for any live or unreadable entry, plus the live-dir test fixture that proves a
    present working dir is left intact. Returns ``(reaped, kept)``.
    """
    meta_dir = _worktrees_metadata_dir(canonical_repo)
    if meta_dir is None or not meta_dir.exists():
        return 0, 0

    reaped = 0
    kept = 0
    orphan_entries: list[Path] = []
    for entry in sorted(meta_dir.iterdir()):
        if not entry.is_dir():
            continue
        working_dir = _orphan_working_dir(entry)
        if working_dir is None:
            # Live run (working dir present) or unreadable metadata — leave it.
            kept += 1
            continue
        locked_file = entry / 'locked'
        if locked_file.exists():
            try:
                locked_file.unlink()
            except OSError as e:
                log(f'Could not clear stale lock for orphan worktree '
                    f'{entry.name}: {str(e)[:200]} — leaving entry in place')
                kept += 1
                continue
        log(f'Reaping orphaned worktree metadata {entry.name} '
            f'(working dir {working_dir} absent)')
        orphan_entries.append(entry)
        reaped += 1

    if orphan_entries:
        try:
            subprocess.run(
                ['git', 'worktree', 'prune'],
                cwd=str(canonical_repo),
                capture_output=True, text=True,
                timeout=GIT_TIMEOUT_SEC,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            log(f'git worktree prune (orphan reaper) failed for '
                f'{canonical_repo}: {str(e)[:200]}')
        # Backstop: ensure each reaped entry's metadata is actually gone. If
        # prune choked on a corrupt HEAD, rmtree removes the offending dir
        # directly — safe because we already confirmed the working dir is absent.
        for entry in orphan_entries:
            if entry.exists():
                shutil.rmtree(entry, ignore_errors=True)
                log(f'Force-removed leftover worktree metadata {entry.name} '
                    f'(prune did not clear it)')

    return reaped, kept


def main() -> int:
    log('Starting worktree cleanup scan')
    active_stems = load_active_task_stems()
    if active_stems:
        log(f'In-flight task stems: {sorted(active_stems)}')
    total_removed = 0
    total_kept = 0
    registered_paths: set[str] = set()
    for canonical in _load_canonical_repos():
        removed, kept, repo_registered = sweep_canonical(canonical, active_stems)
        total_removed += removed
        total_kept += kept
        registered_paths |= repo_registered

        # Reap leaked .git/worktrees/* entries (gate/warmer kill-mid-init orphans)
        # whose working dir is gone. Scoped strictly to orphans, so a live gate
        # run holding an 'initializing' lock is never disturbed.
        locked_reaped, locked_kept = sweep_orphan_locked_worktrees(canonical)
        total_removed += locked_reaped
        total_kept += locked_kept

    orphan_removed, orphan_kept = sweep_orphan_dirs(registered_paths, active_stems)
    total_removed += orphan_removed
    total_kept += orphan_kept

    log(f'Done. Removed: {total_removed}, Kept: {total_kept}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
