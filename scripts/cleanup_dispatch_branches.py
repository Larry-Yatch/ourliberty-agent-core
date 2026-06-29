#!/usr/bin/env python3
"""cleanup_dispatch_branches.py — prune stale Forge/Mirror dispatch branches.

The branch analogue of ``cleanup_stale_worktrees.py``. Every Forge and Mirror
dispatch creates a git branch (``forge/<task-id>``, ``mirror/<task-id>``,
``mirror/review-<task-id>``) with a pre-pushed empty ``[WIP][session-start]``
commit — see ``worktree_manager.setup_branch_checkpoint``. Worktrees self-clean
(``cleanup_stale_worktrees.py``), but NOTHING ever prunes these branches: not on
PR merge, not on dispatch abandonment, neither locally nor on ``origin``. They
accumulate indefinitely (~950 stale branches as of 2026-06-08: ~644 droplet-local
+ ~309 on the remote). This healer is the branch GC backstop.

WHAT IT DOES (per canonical repo in ``config/agent-models.json`` ``repo_paths``):
  1. ``git fetch --prune origin`` so origin/main and the remote-tracking refs are
     current and refs for already-deleted remote branches disappear.
  2. Gather the safety context (open/merged PR heads via ``gh``; the active-
     dispatch branch set from inboxes + the in-flight registry + checked-out
     worktrees).
  3. Enumerate candidate ``forge/*`` and ``mirror/*`` branches, both local
     (``refs/heads/``) and remote (``refs/remotes/origin/``).
  4. Classify each against the "safe to prune" bar (below) and delete only the
     safe ones — in bounded batches, oldest-first — locally with ``git branch -D``
     and on the remote with ``git push origin --delete``.
  5. Journal a one-line summary (log file + a low-noise ``larry_alerts`` digest
     entry; an ``escalate`` alert only when ``gh`` is unavailable / a batch errs).

"SAFE TO PRUNE" BAR — a dispatch branch is pruned only when ALL hold:
  * NOT active: its name is not the head of an OPEN PR, not derivable from any
    live inbox task or live in-flight registry entry, and not checked out by any
    registered worktree.
  * Old enough: its tip commit is older than ``MIN_AGE_SECONDS`` (48h) so a
    just-created dispatch branch can never be raced.
  * Work accounted for: it is the head of a MERGED PR, OR its tip adds nothing
    over its merge-base (only the empty WIP commit — terminally abandoned /
    preflight-REJECT / clarify-timeout), OR every file it changed since its
    merge-base is byte-identical to ``origin/main`` (its content was squash-
    merged under a re-keyed branch). SHA / patch-id equality is NOT used —
    everything is squash-merged, so the per-file content diff is the only
    reliable signal.
A branch with unique unmerged content that is not a terminal-abandoned WIP is
KEPT and logged. When any signal is indeterminate (no merge-base, a git error),
the branch is KEPT. Deleting a remote OPEN-PR head, or a branch a live dispatch
is using, is the failure mode this design avoids; every ambiguous case errs
toward KEEP.

Dry-run (``--dry-run``) lists what WOULD be pruned, with the reason, and deletes
nothing — so the safety bar can be audited against the live backlog first.

``--by-pr-state`` is an opt-in one-time mode for draining the accumulated
backlog: prune any branch whose PR is MERGED or CLOSED, bypassing ONLY the 48h
age floor. The active-dispatch, open-PR-head, and current-branch guards remain
intact (they are never bypassed), and the no-PR net_change paths are disabled —
a branch with no merged/closed PR (including terminally-abandoned WIP) is KEPT.
It does NOT change the scheduled healer's default behavior.

stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Repo scripts dir on sys.path so sibling imports (larry_alerts) resolve when
# run by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from test_isolation_guard import refuse_under_test  # noqa: E402

_MODELS_CONFIG_PATH = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'

AGENTS_ROOT = Path('/home/larry/agents')
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
INBOXES_ROOT = AGENTS_ROOT / 'inboxes'
IN_FLIGHT_DIR = AGENTS_ROOT / 'state' / 'in-flight'
LOG_FILE = AGENTS_ROOT / 'logs' / 'dispatch-branch-cleanup.log'

# Agents that create dispatch branches via inbox_watcher's worktree path
# (worktree_enabled in agent-models.json -> derive_branch_name -> a pushed WIP
# checkpoint). Both Forge and Mirror do; observers (pulse/scout/...) do not.
DISPATCH_AGENTS = ('forge', 'mirror')

# Age floor on the tip commit. 48h is well past any legitimate in-flight
# dispatch (the abandoned-task healer's own ceiling is 60min; a build that
# round-trips preflight->CLARIFY->build->review finishes inside a few hours),
# so the floor only ever protects, never strands real backlog.
MIN_AGE_SECONDS = 48 * 3600

# Bounded batches: drain the backlog over several runs and cap the blast radius
# of any single (buggy) invocation. Local deletes are instant; remote deletes
# are network round-trips, so the remote cap is lower. Oldest branches go first.
MAX_LOCAL_DELETES_PER_RUN = 300
MAX_REMOTE_DELETES_PER_RUN = 150
# Refs per git invocation when deleting (keeps a single command line bounded;
# a failed chunk is retried one ref at a time so one stale ref can't block the
# rest — preserving idempotency).
DELETE_CHUNK = 25

# Cap on the changed-file list we diff against origin/main. A dispatch that
# touched more files than this is not the abandoned-WIP backlog norm; treat it
# as unique content and KEEP (conservative — never risk an over-long command
# line or mis-pruning a large change).
MAX_CHANGED_FILES = 500

GIT_TIMEOUT_SEC = 60
FETCH_TIMEOUT_SEC = 180
PUSH_TIMEOUT_SEC = 180
GH_TIMEOUT_SEC = 30

# Worktree-domain sanitizer length cap. MUST mirror
# worktree_manager.MAX_TASK_ID_LEN (see _sanitize_task_id).
_MAX_TASK_ID_LEN = 50


# ---------- logging ----------


def _log_path() -> Path:
    """Resolve the log path, honoring the test/CI OURLIBERTY_LOG_DIR override so
    a test run never writes into the live ~/agents/logs/ tree (see
    scripts/tests/conftest.py)."""
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    if override:
        return Path(override) / 'dispatch-branch-cleanup.log'
    return LOG_FILE


def log(msg: str) -> None:
    line = f'[{datetime.now(timezone.utc).isoformat()}] {msg}'
    print(line, flush=True)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        # Best-effort: a full/read-only log FS must never crash the healer.
        pass


# ---------- worktree-domain branch-name reconstruction ----------
#
# To know which branch a live dispatch is using we rebuild its branch name the
# exact way inbox_watcher does: derive_branch_name(agent, task_id) ==
# f'{agent}/{_sanitize_task_id(task_id)}'. The sanitizer below MUST agree
# byte-for-byte with worktree_manager._sanitize_task_id (and the two siblings
# agent_runner / heal_abandoned_inbox_tasks._worktree_safe_stem); the
# four-way contract is locked by
# test_path_traversal_sanitizer.WorktreeSanitizerConsistencyTest. If the live
# namer ever diverges from this copy, reconstruct_branch_name() would build a
# name no live branch carries and the active-dispatch guard would miss it — so
# the contract test fails loud rather than letting a live branch be pruned.


def _sanitize_task_id(task_id: str) -> str:
    """Worktree-domain sanitizer: map every non-[A-Za-z0-9_-] char to '-', cap
    at 50. Mirrors worktree_manager._sanitize_task_id byte-for-byte."""
    safe = ''.join(
        c if (c.isalnum() or c in '-_') else '-'
        for c in (task_id or 'task')
    )
    return safe[:_MAX_TASK_ID_LEN] or 'task'


def reconstruct_branch_name(agent_id: str, task_id: str) -> str:
    """Rebuild the branch a dispatch of (agent_id, task_id) would carry —
    mirrors worktree_manager.derive_branch_name."""
    return f'{agent_id}/{_sanitize_task_id(task_id)}'


# ---------- git helpers ----------


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """Run a git command in ``repo``. Never raises — a timeout/OS error is
    returned as a synthetic non-zero CompletedProcess so callers branch on
    returncode uniformly."""
    try:
        return subprocess.run(
            ['git', *args], cwd=str(repo),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'git {" ".join(args)} failed in {repo}: {type(e).__name__}: {e}')
        return subprocess.CompletedProcess(args, returncode=255, stdout='', stderr=str(e))


def fetch_prune(repo: Path) -> None:
    """Best-effort ``git fetch --prune origin`` so origin/main and the remote-
    tracking refs are fresh and refs for already-deleted remote branches are
    pruned from our view. Non-fatal: a fetch failure only means we evaluate
    against a slightly staler base, which can only make us KEEP more."""
    r = _git(repo, 'fetch', '--prune', 'origin', timeout=FETCH_TIMEOUT_SEC)
    if r.returncode != 0:
        log(f'fetch --prune warning in {repo}: {r.stderr[:200]}')


def current_branch(repo: Path) -> Optional[str]:
    r = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    return r.stdout.strip() if r.returncode == 0 else None


def list_candidate_branches(repo: Path, scope: str) -> list[tuple[str, int]]:
    """Return [(branch_name, committer_unix_ts)] for forge/* and mirror/*
    branches. ``scope`` is 'local' (refs/heads) or 'remote'
    (refs/remotes/origin). ``branch_name`` is always the short form WITHOUT an
    ``origin/`` prefix (e.g. 'forge/x'), so it compares directly to PR head
    refs and reconstructed active names regardless of scope."""
    if scope == 'local':
        globs = [f'refs/heads/{a}/' for a in DISPATCH_AGENTS]
    else:
        globs = [f'refs/remotes/origin/{a}/' for a in DISPATCH_AGENTS]
    # %09 is a literal tab — a stable field separator a branch name can't contain
    # (git ref names forbid control chars), unlike a space.
    r = _git(repo, 'for-each-ref', '--format=%(refname:short)%09%(committerdate:unix)', *globs)
    if r.returncode != 0:
        log(f'for-each-ref ({scope}) failed in {repo}: {r.stderr[:200]}')
        return []
    out: list[tuple[str, int]] = []
    for line in r.stdout.splitlines():
        if '\t' not in line:
            continue
        name, ts_raw = line.rsplit('\t', 1)
        try:
            ts = int(ts_raw)
        except ValueError:
            continue
        if scope == 'remote':
            # refname:short of refs/remotes/origin/forge/x is 'origin/forge/x'.
            if not name.startswith('origin/'):
                continue
            name = name[len('origin/'):]
        out.append((name, ts))
    return out


def checked_out_branches(repo: Path) -> set[str]:
    """Branch names checked out by any registered worktree of ``repo`` — git
    refuses to delete these locally, and a checkout is a strong 'active' signal,
    so they are protected in both scopes."""
    r = _git(repo, 'worktree', 'list', '--porcelain')
    names: set[str] = set()
    if r.returncode != 0:
        return names
    for line in r.stdout.splitlines():
        if line.startswith('branch '):
            ref = line[len('branch '):].strip()
            if ref.startswith('refs/heads/'):
                names.add(ref[len('refs/heads/'):])
    return names


def _tip_ref(branch: str, scope: str) -> str:
    return branch if scope == 'local' else f'origin/{branch}'


def compute_net_change(repo: Path, branch: str, scope: str) -> str:
    """Classify a branch's content relative to origin/main. Returns one of:

      'empty-wip'      — adds no file changes over its merge-base (only the
                         empty [WIP][session-start] commit / nothing): abandoned.
      'content-on-main'— every file it changed since merge-base is byte-identical
                         to origin/main (squash-merged under a re-keyed branch).
      'unique'         — has file content not on origin/main (real unmerged work).
      'unknown'        — a git step failed / no merge-base: caller KEEPs.

    SHA/patch-id are deliberately NOT used (squash-merge rewrites them); the
    per-file content comparison against origin/main is the reliable signal.
    """
    tip = _tip_ref(branch, scope)
    mb = _git(repo, 'merge-base', 'origin/main', tip)
    if mb.returncode != 0:
        return 'unknown'
    merge_base = mb.stdout.strip()
    if not merge_base:
        return 'unknown'
    # --no-renames: rename detection collapses 'old -> new' to just 'new', which
    # would drop the deleted old-side path from `changed`; the per-file diff below
    # then never compares it and a rename/delete branch whose NEW side happens to
    # match origin/main would be mis-pruned as content-on-main, destroying its
    # unmerged delete. With --no-renames a rename surfaces BOTH paths, so old.py's
    # deletion keeps the per-file diff non-empty -> 'unique' -> KEEP.
    # --literal-pathspecs: a changed file whose path starts with ':' (pathspec
    # magic) must be treated literally when fed back as a pathspec below, else
    # git exits 128 and the branch becomes permanently unprunable.
    names = _git(repo, '--literal-pathspecs', 'diff', '--name-only',
                 '--no-renames', merge_base, tip)
    if names.returncode != 0:
        return 'unknown'
    changed = [p for p in names.stdout.splitlines() if p]
    if not changed:
        return 'empty-wip'
    if len(changed) > MAX_CHANGED_FILES:
        # Too large to diff cheaply and not the abandoned-WIP norm — keep.
        return 'unique'
    # Are all the branch's changed files identical to origin/main? --quiet implies
    # --exit-code: rc 0 == no diff (content already on main), rc 1 == differs.
    diff = _git(repo, '--literal-pathspecs', 'diff', '--quiet', 'origin/main',
                tip, '--', *changed)
    if diff.returncode == 0:
        return 'content-on-main'
    if diff.returncode == 1:
        return 'unique'
    return 'unknown'


# ---------- active-dispatch / PR context ----------


def _pid_alive(pid) -> bool:
    try:
        ipid = int(pid)
    except (TypeError, ValueError):
        return False
    if ipid <= 0:
        return False
    try:
        os.kill(ipid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def active_dispatch_branches() -> set[str]:
    """Branch names that belong to a live (not-yet-terminal) dispatch and must
    never be pruned, derived from three sources:

      * inbox tasks still queued/in-progress (top-level inboxes/{forge,mirror}/
        *.json) — the branch reconstruct_branch_name(agent, task_id) plus any
        explicit ``branch`` the envelope set;
      * live in-flight registry entries (a signalable pid) — the reconstructed
        derived name (inbox_watcher registers task_stem = the raw task_id, so
        reconstruct_branch_name(agent_id, task_stem) reproduces the DEFAULT
        derived branch exactly) plus the raw stem as a cheap belt-and-suspenders
        add.
      * (added per-repo by the caller, in sweep_repo) the branches checked out
        in a worktree. This is the NAME-AGNOSTIC backstop and the load-bearing
        guard for a live dispatch: setup_branch_checkpoint does
        `git checkout -B <branch>` in the worktree, so for the build's whole
        duration `git worktree list` reports that branch — covering branches a
        dispatch named via an explicit envelope `branch` field or a free-form
        `Branch hint:` that reconstruct_branch_name cannot reproduce. (The 48h
        age floor independently protects any freshly-created dispatch branch.)

    Read-only and failsafe: an unreadable file is skipped, never expanded into a
    wrongly-empty protected set.
    """
    names: set[str] = set()
    for agent in DISPATCH_AGENTS:
        inbox = INBOXES_ROOT / agent
        if not inbox.is_dir():
            continue
        # glob('*.json') is top-level only — .archive/ and .invalid/ (terminal)
        # are correctly excluded.
        for f in inbox.glob('*.json'):
            try:
                body = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                # Can't read it -> don't know its branch -> the age + work-
                # accounted gates still protect a real in-flight branch (it is
                # <48h old and has unique content), so skipping here is safe.
                continue
            if not isinstance(body, dict):
                continue
            task_id = body.get('task_id') or f.stem
            names.add(reconstruct_branch_name(agent, str(task_id)))
            explicit = body.get('branch')
            if isinstance(explicit, str) and explicit:
                names.add(explicit)

    if IN_FLIGHT_DIR.is_dir():
        for f in IN_FLIGHT_DIR.glob('*.json'):
            try:
                entry = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict) or not _pid_alive(entry.get('pid')):
                continue
            stem = entry.get('task_stem') or f.stem
            agent = entry.get('agent_id')
            if isinstance(stem, str) and stem:
                # task_stem is the raw task_id (inbox_watcher: task_stem=task_id),
                # so the real derived branch is reconstruct_branch_name(agent,
                # stem); the bare-stem add is a harmless extra. When agent_id is
                # absent we cannot reconstruct, but the worktree-checkout backstop
                # (see docstring) and the 48h age floor still protect the live
                # branch — this leg is not the sole guard.
                names.add(stem)
                if isinstance(agent, str) and agent:
                    names.add(reconstruct_branch_name(agent, stem))
    return names


def _gh_env() -> dict:
    return {**os.environ,
            'PATH': '/usr/bin:/usr/local/bin:' + os.environ.get('PATH', '')}


def _gh_pr_heads(repo: Path, state: str) -> Optional[set[str]]:
    """Head ref names of PRs in ``state`` for ``repo`` (gh infers the GitHub repo
    from the local checkout's origin). Returns None on any gh failure so the
    caller can distinguish 'no such PRs' (empty set) from 'gh unavailable'
    (None) — the open-PR query treats None as a hard stop."""
    try:
        res = subprocess.run(
            ['gh', 'pr', 'list', '--state', state, '--limit', '1000',
             '--json', 'headRefName'],
            cwd=str(repo), capture_output=True, text=True,
            timeout=GH_TIMEOUT_SEC, env=_gh_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr list --state {state} in {repo} failed: {type(e).__name__}: {e}')
        return None
    if res.returncode != 0:
        log(f'gh pr list --state {state} in {repo} rc={res.returncode}: {res.stderr[:200]}')
        return None
    try:
        rows = json.loads(res.stdout or '[]')
    except json.JSONDecodeError as e:
        log(f'gh pr list --state {state} in {repo} bad json: {e}')
        return None
    heads: set[str] = set()
    for row in rows:
        ref = row.get('headRefName') if isinstance(row, dict) else None
        if isinstance(ref, str) and ref:
            heads.add(ref)
    return heads


# ---------- classification (pure; unit-tested directly) ----------


@dataclass(frozen=True)
class BranchFacts:
    name: str               # short branch, no origin/ (e.g. 'forge/x')
    scope: str              # 'local' | 'remote'
    committer_ts: int       # tip commit committer date (unix)
    is_active: bool         # inbox / in-flight / checked-out worktree
    is_open_pr_head: bool
    is_merged_pr_head: bool
    net_change: str         # 'empty-wip'|'content-on-main'|'unique'|'unknown'
    is_closed_pr_head: bool = False  # head of a CLOSED-unmerged PR (by-pr-state)


@dataclass(frozen=True)
class Decision:
    action: str             # 'prune' | 'keep'
    reason: str


def classify(facts: BranchFacts, now: float, min_age_sec: int = MIN_AGE_SECONDS,
             by_pr_state: bool = False) -> Decision:
    """Apply the safe-to-prune bar to one branch. Pure — all I/O is resolved
    into ``facts`` by the caller. Every ambiguous/indeterminate case returns
    KEEP. See the module docstring for the full bar.

    ``by_pr_state`` is the opt-in one-time mode: prune any branch whose PR is
    MERGED or CLOSED, bypassing ONLY the age floor. The active-dispatch and
    open-PR guards still fire first (they are never bypassed), and the no-PR
    net_change paths (empty-WIP / content-on-main) are disabled — a branch with
    no merged/closed PR is KEPT (out of scope for this mode)."""
    # Active and open-PR guards come first and ignore age — a branch a live
    # dispatch is using or an OPEN PR head is never pruned, even if somehow old.
    if facts.is_active:
        return Decision('keep', 'active-dispatch (inbox/in-flight/worktree)')
    if facts.is_open_pr_head:
        return Decision('keep', 'open-PR head')
    if by_pr_state:
        # One-time PR-state sweep: age floor bypassed, net_change ignored. Only
        # a MERGED or CLOSED PR head is pruned; everything else is kept.
        if facts.is_merged_pr_head:
            return Decision('prune', 'merged-PR head (by-pr-state)')
        if facts.is_closed_pr_head:
            return Decision('prune', 'closed-PR head (by-pr-state)')
        return Decision('keep', 'no merged/closed PR (by-pr-state)')
    # Age guard: never race a just-created dispatch branch.
    if (now - facts.committer_ts) < min_age_sec:
        return Decision('keep', f'too-young (<{min_age_sec // 3600}h)')
    # Work accounted for?
    if facts.is_merged_pr_head:
        return Decision('prune', 'merged-PR head')
    if facts.net_change == 'empty-wip':
        return Decision('prune', 'empty-WIP-only (terminally abandoned)')
    if facts.net_change == 'content-on-main':
        return Decision('prune', 'content fully on origin/main (squash-merged)')
    if facts.net_change == 'unique':
        return Decision('keep', 'unique unmerged content')
    return Decision('keep', 'indeterminate (kept for safety)')


# ---------- deletion (bounded, chunked, idempotent) ----------


def delete_local(repo: Path, branches: list[str]) -> int:
    """``git branch -D`` in chunks; retry a failed chunk per-ref so one
    undeletable ref (e.g. checked out) never blocks the rest. Returns the count
    git reported as deleted."""
    deleted = 0
    for i in range(0, len(branches), DELETE_CHUNK):
        chunk = branches[i:i + DELETE_CHUNK]
        r = _git(repo, 'branch', '-D', *chunk)
        # git prints "Deleted branch <name> (was ...)." per success to stdout
        # EVEN when the chunk's rc is non-zero (one bad ref fails the command but
        # the good refs are still deleted). Count from stdout regardless of rc,
        # then retry ONLY the refs not reported deleted — so an already-gone ref
        # in the chunk never mis-logs a genuinely-deleted sibling as 'failed'.
        done = {
            parts[2] for parts in (ln.split() for ln in r.stdout.splitlines())
            if len(parts) >= 3 and parts[0] == 'Deleted' and parts[1] == 'branch'
        }
        deleted += len(done & set(chunk))
        for b in chunk:
            if b in done:
                continue
            r1 = _git(repo, 'branch', '-D', b)
            if r1.returncode == 0:
                deleted += 1
            else:
                log(f'keep local {b}: delete failed: {(r1.stderr or "").strip()[:160]}')
    return deleted


def delete_remote(repo: Path, branches: list[str]) -> int:
    """``git push origin --delete`` in chunks; retry a failed chunk per-ref so a
    single already-deleted ref doesn't fail the batch. Returns the count
    successfully deleted."""
    refuse_under_test('git-push-delete')
    deleted = 0
    for i in range(0, len(branches), DELETE_CHUNK):
        chunk = branches[i:i + DELETE_CHUNK]
        r = _git(repo, 'push', 'origin', '--delete', *chunk, timeout=PUSH_TIMEOUT_SEC)
        if r.returncode == 0:
            deleted += len(chunk)
            continue
        for b in chunk:
            r1 = _git(repo, 'push', 'origin', '--delete', b, timeout=PUSH_TIMEOUT_SEC)
            if r1.returncode == 0:
                deleted += 1
            else:
                stderr = (r1.stderr or '').strip().lower()
                if 'remote ref does not exist' in stderr:
                    # Already gone — idempotent success.
                    deleted += 1
                else:
                    log(f'keep remote {b}: push --delete failed: {(r1.stderr or "").strip()[:160]}')
    return deleted


# ---------- per-repo sweep ----------


@dataclass
class SweepResult:
    repo: str
    local_pruned: int = 0
    remote_pruned: int = 0
    local_kept: int = 0
    remote_kept: int = 0
    local_capped: int = 0
    remote_capped: int = 0
    gh_unavailable: bool = False


def sweep_repo(repo: Path, active_global: set[str], now: float, dry_run: bool,
               by_pr_state: bool = False) -> SweepResult:
    res = SweepResult(repo=str(repo))
    if not repo.exists():
        log(f'canonical repo missing, skipping: {repo}')
        return res

    fetch_prune(repo)

    open_heads = _gh_pr_heads(repo, 'open')
    if open_heads is None:
        # Without the open-PR list we cannot guarantee we are not deleting an
        # OPEN PR head — the one irreversible mistake. Refuse to prune this repo.
        log(f'SKIP repo {repo}: open-PR list unavailable (gh) — refusing to prune')
        res.gh_unavailable = True
        return res
    merged_heads = _gh_pr_heads(repo, 'merged') or set()
    # CLOSED-unmerged heads are only consulted in the opt-in by-pr-state mode. A
    # failed query (-> None -> set()) keeps more, never deletes more — safe.
    closed_heads = (_gh_pr_heads(repo, 'closed') or set()) if by_pr_state else set()

    active = set(active_global) | checked_out_branches(repo)
    cur = current_branch(repo)
    if cur:
        active.add(cur)

    for scope, cap in (('local', MAX_LOCAL_DELETES_PER_RUN),
                       ('remote', MAX_REMOTE_DELETES_PER_RUN)):
        candidates = list_candidate_branches(repo, scope)
        # Oldest-first so the long-stale backlog drains before fresh branches.
        candidates.sort(key=lambda c: c[1])
        to_prune: list[str] = []
        kept = 0
        for name, ts in candidates:
            facts = BranchFacts(
                name=name, scope=scope, committer_ts=ts,
                is_active=name in active,
                is_open_pr_head=name in open_heads,
                is_merged_pr_head=name in merged_heads,
                is_closed_pr_head=name in closed_heads,
                # Only compute the (cost) diff once cheaper guards haven't already
                # decided KEEP — active/open-PR/too-young short-circuit it. In
                # by-pr-state mode net_change is never consulted, so skip it.
                net_change=(
                    compute_net_change(repo, name, scope)
                    if not (by_pr_state or name in active or name in open_heads
                            or (now - ts) < MIN_AGE_SECONDS)
                    else 'unknown'
                ),
            )
            decision = classify(facts, now, by_pr_state=by_pr_state)
            if decision.action == 'prune':
                to_prune.append(name)
            else:
                kept += 1
                log(f'keep {scope} {name}: {decision.reason}')

        capped = 0
        if len(to_prune) > cap:
            capped = len(to_prune) - cap
            log(f'{scope}: {len(to_prune)} prunable, capping at {cap} this run '
                f'({capped} deferred to next run)')
            to_prune = to_prune[:cap]

        pruned = 0
        if to_prune:
            if dry_run:
                for b in to_prune:
                    log(f'DRY-RUN would prune {scope} {b}')
                pruned = 0
            elif scope == 'local':
                pruned = delete_local(repo, to_prune)
            else:
                pruned = delete_remote(repo, to_prune)
            if not dry_run:
                log(f'{scope}: pruned {pruned}/{len(to_prune)} branch(es)')

        if scope == 'local':
            res.local_pruned, res.local_kept, res.local_capped = (
                len(to_prune) if dry_run else pruned, kept, capped)
        else:
            res.remote_pruned, res.remote_kept, res.remote_capped = (
                len(to_prune) if dry_run else pruned, kept, capped)

    return res


# ---------- canonical repo config ----------


def load_canonical_repos() -> list[Path]:
    """Canonical repo Paths from config/agent-models.json ``repo_paths`` — the
    same block cleanup_stale_worktrees.py reads. Fail-loud (RuntimeError) on a
    missing block or a path outside /home/larry/ (the systemd ReadWritePaths
    constraint)."""
    try:
        cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f'could not read {_MODELS_CONFIG_PATH}: {e}') from e
    block = cfg.get('repo_paths') if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or not block:
        raise RuntimeError(
            "config/agent-models.json missing required 'repo_paths' block")
    repos: list[Path] = []
    for name, raw in block.items():
        if not isinstance(raw, str) or not raw.startswith('/home/larry/'):
            raise RuntimeError(
                f'repo_paths[{name!r}]={raw!r} must be an absolute path under '
                '/home/larry/')
        repos.append(Path(raw))
    return repos


# ---------- summary alert ----------


def _emit_summary(results: list[SweepResult], dry_run: bool) -> None:
    """One low-noise journal line. Routine pruning -> route='digest' (surfaced in
    the daily CEO digest, never DM'd). gh-unavailable -> route='escalate' so a
    persistent loss of the open-PR guard reaches Larry. Per-branch detail stays
    in the log file, never the DM."""
    local_pruned = sum(r.local_pruned for r in results)
    remote_pruned = sum(r.remote_pruned for r in results)
    capped = sum(r.local_capped + r.remote_capped for r in results)
    gh_down = [r.repo for r in results if r.gh_unavailable]

    verb = 'would prune' if dry_run else 'pruned'
    summary = (f'dispatch-branch cleanup: {verb} {local_pruned} local + '
               f'{remote_pruned} remote stale branch(es)')
    if capped:
        summary += f'; {capped} deferred (batch cap)'
    if gh_down:
        summary += f'; {len(gh_down)} repo(s) skipped — gh unavailable'
    log(summary)

    if dry_run:
        return
    try:
        import larry_alerts  # noqa: E402
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        log(f'larry_alerts unavailable: {e}')
        return
    if gh_down:
        larry_alerts.append_alert(
            source='dispatch-branch-cleanup', severity='warning',
            message=summary, subject='gh-unavailable', route='escalate')
    elif local_pruned or remote_pruned or capped:
        larry_alerts.append_alert(
            source='dispatch-branch-cleanup', severity='info',
            message=summary, subject='summary', route='digest')


# ---------- main ----------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='cleanup_dispatch_branches.py',
        description='Prune stale Forge/Mirror dispatch branches (local + origin).')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='List what WOULD be pruned, with the reason; delete nothing.')
    parser.add_argument(
        '--by-pr-state', action='store_true',
        help='One-time opt-in mode: prune any branch whose PR is MERGED or '
             'CLOSED, bypassing ONLY the 48h age floor. Active-dispatch, '
             'open-PR, and current-branch guards stay intact; branches with no '
             'merged/closed PR (including abandoned-WIP) are kept.')
    args = parser.parse_args(argv)

    if KILL_SWITCH.exists():
        log('KILLED_BY_SWITCH: healers.disabled present, exiting')
        return 0

    mode = 'DRY-RUN' if args.dry_run else 'LIVE'
    if args.by_pr_state:
        mode += ' by-pr-state'
    log(f'Starting dispatch-branch cleanup ({mode})')
    now = datetime.now(timezone.utc).timestamp()
    active_global = active_dispatch_branches()
    if active_global:
        log(f'active-dispatch branches protected: {len(active_global)}')

    results: list[SweepResult] = []
    for repo in load_canonical_repos():
        results.append(
            sweep_repo(repo, active_global, now, args.dry_run,
                       by_pr_state=args.by_pr_state))

    _emit_summary(results, args.dry_run)
    log('Done.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
