#!/usr/bin/env python3
"""cleanup_stale_worktrees.py — Remove git worktrees older than 24 hours.

Runs daily via systemd timer (ourliberty-cleanup-stale-worktrees). Cleans
up ``~/agent-worktrees/wt-*`` directories left behind by Forge dispatches.
Worktrees are preserved for 24 hours after task completion so Larry can
inspect the working state if anything went wrong.

Adapted from GrowthMastery-ai/gm-agent-core ``scripts/cleanup_stale_worktrees.py``
for Larry-Yatch/ourliberty-agent-core (2026-05-12, Phase D3 commit 4b — Gap 10
in docs/upstream-audit.md). Adaptations:

  - paths joe→larry; gm-agents→agents; growth-mastery→agent-core
  - ``CANONICAL_REPOS`` list (not a single REPO_DIR) so multi-repo expansion
    is one-line when ``allowed_repos`` in agent-models.json grows beyond
    ``ourliberty-agent-core``.
  - 4b followup #2: worktree base ``~/agent-worktrees/`` instead of upstream's
    ``/tmp/wt-`` — see worktree_manager.py for the PrivateTmp namespace
    rationale.

stdlib only.
"""
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Canonical repos. Each is the working tree from which Forge worktrees are
# spawned (``git worktree add --detach --from-here``). ``git worktree list``
# only reports worktrees of the specific canonical it's run against, so the
# cleanup loop must iterate every canonical.
#
# TODO(D5+): when ``allowed_repos`` in config/agent-models.json grows beyond
# the agent-core repo, populate this list from that config so logical-name →
# filesystem-path mapping lives in one place.
CANONICAL_REPOS = [
    Path('/home/larry/agent-core'),
]

# Path prefix that identifies a managed worktree. Must agree with
# worktree_manager.WORKTREE_BASE + WORKTREE_PREFIX.
MANAGED_WORKTREE_PREFIX = '/home/larry/agent-worktrees/wt-'

LOG_FILE = Path('/home/larry/agents/logs/worktree-cleanup.log')
IN_FLIGHT_DIR = Path('/home/larry/agents/state/in-flight')
MAX_AGE_SECONDS = 86400  # 24 hours
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


def sweep_canonical(canonical_repo: Path, active_stems: set[str]) -> tuple[int, int]:
    """Process one canonical repo. Returns (removed, kept)."""
    if not canonical_repo.exists():
        log(f'canonical_repo missing, skipping: {canonical_repo}')
        return 0, 0

    worktrees = list_worktrees(canonical_repo)
    log(f'{canonical_repo}: found {len(worktrees)} worktree(s)')

    removed = 0
    kept = 0
    now = time.time()

    for wt in worktrees:
        path = wt.get('path', '')
        # Skip the canonical itself and any worktree that isn't one of ours.
        if path == str(canonical_repo) or MANAGED_WORKTREE_PREFIX not in path:
            kept += 1
            continue

        # 4b review fix: skip worktrees referenced by the in-flight registry.
        # A long Read-heavy build doesn't touch the worktree's mtime, so the
        # 24h gate isn't enough on its own — without this check the cleanup
        # could remove an actively-in-use worktree.
        if any(stem and stem in Path(path).name for stem in active_stems):
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

    return removed, kept


def main() -> int:
    log('Starting worktree cleanup scan')
    active_stems = load_active_task_stems()
    if active_stems:
        log(f'In-flight task stems: {sorted(active_stems)}')
    total_removed = 0
    total_kept = 0
    for canonical in CANONICAL_REPOS:
        removed, kept = sweep_canonical(canonical, active_stems)
        total_removed += removed
        total_kept += kept
    log(f'Done. Removed: {total_removed}, Kept: {total_kept}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
