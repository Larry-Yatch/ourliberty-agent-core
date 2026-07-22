#!/usr/bin/env python3
"""sync_dispatch_repo_clones.py — keep the dispatch-repo working copies on the
droplet fast-forwarded to origin/main.

WHY
---
``config/agent-models.json`` → ``repo_paths`` registers a checkout per dispatch
repo. NOTHING kept those checkouts current except agent-core, which has its own
dedicated syncer (``scripts/sync_agent_core.sh``, an atomic-swap into ~/agents
that is deliberately agent-core-only). Every other registered clone drifted
until a human hand-pulled it.

That drift is invisible most of the time, because BUILDS never read the
checkout: ``worktree_manager.create_or_reuse_worktree_for_task`` fetches and
branches off ``origin/main``, so a stale clone still produces a fresh worktree.
What DOES read the checkout is the working-tree consumers — most visibly the
Mirror DAG-preflight, which reads a sequence's ``spec_doc`` and milestone specs
off disk.

RSDPM-2026-07-22 is the worked example: ``/home/larry/RSDPM`` sat 40 commits
behind, ``specs/`` did not exist in the working copy, and the DAG-preflight for
``rsdpm-v0-001`` parked overnight on files that were present on origin/main the
whole time. Measured at the same moment, ``ourliberty-dashboard`` was 30 commits
behind — so this was never RSDPM-specific, just RSDPM-visible.

CONSERVATIVE — fast-forward or skip, never resolve
--------------------------------------------------
Each repo is advanced ONLY when advancing is provably lossless:
- agent-core is SKIPPED — ``sync_agent_core.sh`` owns that tree, and two
  syncers on one checkout is the machine-owned-file trap.
- not a git repo / no origin → skip.
- not on ``main`` (a branch or a detached HEAD) → skip. Somebody is using it.
- local commits ahead of origin → skip. ff-only would refuse anyway; skipping
  says so in the log instead of surfacing a failed merge.
- TRACKED local modifications → skip. Somebody is mid-edit; moving the tree
  under them is rude even when it would be lossless. Untracked build litter
  (``__pycache__/``, ``.next/``) is NOT a skip reason — see ``sync_one``.
- otherwise: ``git fetch origin main`` then ``git merge --ff-only origin/main``.

Every skip is a LOG line, not an alert. A repo parked on local dirt is a normal
state (somebody is working in it), and paging on it would be exactly the alert
toil this system is supposed to absorb. A skipped repo simply retries next tick.

Safe against a concurrent build: worktrees live at their own paths off a
detached origin/main, so fast-forwarding the clone's ``main`` does not move any
worktree's HEAD. A racing ``git`` holding the index lock just fails this tick.

Content-free logging: repo names + commit counts only, never file contents or
paths inside the client repo (D33 — RSDPM is a client product).

Dry-run by default; pass --apply to actually fast-forward. stdlib only.

    cd ~/agent-core && python3 scripts/sync_dispatch_repo_clones.py          # dry-run
    cd ~/agent-core && python3 scripts/sync_dispatch_repo_clones.py --apply  # write
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

CONFIG_PATH = _REPO_ROOT / 'config' / 'agent-models.json'

# agent-core syncs itself via sync_agent_core.sh (atomic swap into ~/agents).
# A second syncer on the same checkout is the machine-owned-file single-committer
# trap — the two would race on the same index.
SELF_SYNCED_REPOS = frozenset({'ourliberty-agent-core'})

# git is fast here (a fetch of one branch); a hang means a network/lock problem
# and the next tick should get it instead.
GIT_TIMEOUT_SEC = 120


@dataclass
class RepoOutcome:
    """What happened to one registered checkout this tick."""
    repo: str
    path: str
    action: str          # advanced | current | skipped | error
    reason: str = ''
    commits: int = 0     # how many commits the fast-forward covered

    def line(self) -> str:
        bits = f'{self.repo}: {self.action}'
        if self.commits:
            bits += f' (+{self.commits})'
        if self.reason:
            bits += f' — {self.reason}'
        return bits


@dataclass
class SyncReport:
    outcomes: list[RepoOutcome] = field(default_factory=list)

    @property
    def advanced(self) -> list[RepoOutcome]:
        return [o for o in self.outcomes if o.action == 'advanced']

    @property
    def problems(self) -> list[RepoOutcome]:
        return [o for o in self.outcomes if o.action == 'error']


def _git(path: str, *args: str) -> tuple[int, str, str]:
    """Run git in `path`. Never raises — returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            ['git', '-C', path, *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC,
        )
        return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()
    except subprocess.TimeoutExpired:
        return 124, '', f'git {args[0]} timed out after {GIT_TIMEOUT_SEC}s'
    except Exception as exc:                             # pragma: no cover
        return 1, '', f'{type(exc).__name__}: {str(exc)[:200]}'


def load_repo_paths(config_path: Path = CONFIG_PATH) -> dict[str, str]:
    """Read repo_paths from agent-models.json. Missing/malformed → {}."""
    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return {}
    paths = data.get('repo_paths')
    if not isinstance(paths, dict):
        return {}
    return {k: v for k, v in paths.items() if isinstance(v, str) and v}


def sync_one(repo: str, path: str, apply: bool = False) -> RepoOutcome:
    """Fast-forward one checkout if — and only if — it is provably lossless."""
    out = lambda action, reason='', commits=0: RepoOutcome(  # noqa: E731
        repo=repo, path=path, action=action, reason=reason, commits=commits)

    if repo in SELF_SYNCED_REPOS:
        return out('skipped', 'self-synced by sync_agent_core.sh')

    p = Path(path)
    if not p.is_dir():
        return out('skipped', 'checkout path does not exist')

    rc, _, _ = _git(path, 'rev-parse', '--git-dir')
    if rc != 0:
        return out('skipped', 'not a git repository')

    rc, branch, _ = _git(path, 'rev-parse', '--abbrev-ref', 'HEAD')
    if rc != 0:
        return out('error', 'could not read HEAD')
    if branch != 'main':
        # Detached HEAD reports 'HEAD'. Either way somebody is using this tree.
        return out('skipped', f'on {branch!r}, not main')

    rc, _, err = _git(path, 'fetch', 'origin', 'main')
    if rc != 0:
        return out('error', f'fetch failed: {err[:160]}')

    # TRACKED modifications only (-uno). Untracked files are deliberately NOT a
    # skip reason: every long-lived checkout accumulates build litter
    # (__pycache__/, .next/, node_modules/) that will never be committed, and
    # blocking on it would park that repo forever — ourliberty-graph was sitting
    # on exactly that when this job was written. Untracked files also can't be
    # silently clobbered: if an incoming commit adds a file at the same path,
    # git REFUSES the merge ("untracked working tree files would be
    # overwritten"), which lands in the error branch below with git's own words.
    rc, dirty, _ = _git(path, 'status', '--porcelain', '--untracked-files=no')
    if rc != 0:
        return out('error', 'could not read working-tree status')
    if dirty:
        n = len(dirty.splitlines())
        return out('skipped', f'{n} uncommitted change(s) — not ours to touch')

    rc, counts, _ = _git(
        path, 'rev-list', '--left-right', '--count', 'origin/main...HEAD')
    if rc != 0 or not counts:
        return out('error', 'could not compare against origin/main')
    try:
        behind_s, ahead_s = counts.split()
        behind, ahead = int(behind_s), int(ahead_s)
    except ValueError:
        return out('error', f'unparseable rev-list output: {counts[:60]!r}')

    if ahead:
        return out('skipped', f'{ahead} local commit(s) ahead of origin')
    if not behind:
        return out('current')

    if not apply:
        return out('advanced', f'DRY-RUN would fast-forward', commits=behind)

    rc, _, err = _git(path, 'merge', '--ff-only', 'origin/main')
    if rc != 0:
        return out('error', f'ff-only merge failed: {err[:160]}')
    return out('advanced', commits=behind)


def run(apply: bool = False, config_path: Path = CONFIG_PATH) -> SyncReport:
    report = SyncReport()
    for repo, path in sorted(load_repo_paths(config_path).items()):
        report.outcomes.append(sync_one(repo, path, apply=apply))
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true',
                    help='actually fast-forward (default: dry-run)')
    ap.add_argument('--json', action='store_true',
                    help='emit the report as JSON')
    args = ap.parse_args(argv)

    report = run(apply=args.apply)

    if args.json:
        print(json.dumps(
            [o.__dict__ for o in report.outcomes], indent=2, sort_keys=True))
    else:
        mode = 'apply' if args.apply else 'dry-run'
        for o in report.outcomes:
            print(f'[{mode}] {o.line()}')
        print(f'[{mode}] {len(report.advanced)} advanced, '
              f'{len(report.problems)} error(s), '
              f'{len(report.outcomes)} registered')

    # Errors are reported but never fail the unit: a transient git/network
    # problem must not turn a best-effort freshness sweep into a failed-unit
    # page. The next tick retries.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
