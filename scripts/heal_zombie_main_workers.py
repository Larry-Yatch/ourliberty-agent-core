#!/usr/bin/env python3
"""
heal_zombie_main_workers.py — Auto-kill claude processes that are zombie workers.

Mandated by: agent-core/runbooks/cycle-prompt.md AIRTIGHT PLAN v1
              ZERO-LLM HEALER CONSTELLATION (cycle iter 105 → 130, 2026-05-05/06)
              Iter 201 enhancement (2026-05-06): detect completed-PR zombies too.

Failure modes it heals:

  PATTERN A — Deleted-worktree zombie (original, iter 105/130 lesson):
    Cycle iter 105 first observed: claude main-agent processes can outlive their
    worktrees. When ourliberty-worktree-cleanup deletes a worktree but the claude
    subprocess inside is still running (or hung), the process keeps running with
    cwd pointing to a (deleted) path. Orchestrator's MAX_CONCURRENT_TASKS slot
    counter sees them as live workers, so the slot stays occupied indefinitely.

  PATTERN B — Completed-PR zombie (iter 201 lesson, 2026-05-06):
    Worker successfully completes its work (PR opened, merged, deployed) but
    fails to terminate cleanly. Stays alive in MCP/sleep polling loops with
    intact worktree. Iter 201 found 9 such zombies all from completed PRs:
    - 7 from AI editor pivot ship (PRs #2216, #2214, #2217, #2215, #2230,
      #2225, #2223 — all MERGED), workers alive 16-17 hours post-merge
    - 2 from Lindsay bulk-email fixes (PRs #2234, #2235 — both MERGED), workers
      alive 1.4 hours post-merge
    Each zombie burns 3-6% CPU on sleep loops doing nothing useful. Detection
    depends on worktree HEAD being merged into origin/main (not deleted).

What it does:
  Every 5 min, scan all `claude` processes matching the main-agent sysprompt.
  For each PID, classify:
    1. PATTERN A — cwd is `(deleted)` AND etime > 10 min → SIGKILL
    2. PATTERN B — cwd is a real `/tmp/wt-main-*` worktree AND
       worktree HEAD is merged to origin/main AND
       etime > 4 hours AND
       no file mods in worktree within last 30 min → SIGKILL

Safe-by-construction:
  - Only kills main-agent claude processes (sysprompt-main filter)
  - PATTERN A: only kills if cwd genuinely deleted + >10 min etime
  - PATTERN B: only kills if worktree HEAD is on origin/main (work is shipped)
    AND >4 hour etime AND >30 min idle (no recent file activity)
  - NEVER touches non-pulse sysprompts (only acts on pulse-class workers)
  - Kill-switch aware (/home/larry/agents/healers.disabled)
  - Read-only on GitHub
  - Emits HEARTBEAT every run + HEALED on each kill (pattern noted)

Verification:
  - PATTERN A: iter 130 + iter 195 history shows HEALED events firing
  - PATTERN B: iter 201 will be first observation; 9 zombies should clear
    within 1 sweep window
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pid_identity  # noqa: E402  # PID-reuse guard before SIGKILL

AGENTS_ROOT = Path("/home/larry/agents")
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_zombie_main_workers.log"

MIN_DELETED_CWD_AGE_SECS = 600           # PATTERN A: 10 min
MIN_COMPLETED_PR_AGE_SECS = 4 * 3600     # PATTERN B: 4 hours
MAX_RECENT_FILE_ACTIVITY_SECS = 30 * 60  # PATTERN B: must be 30 min idle
# TODO(Larry): wire up sysprompt-filter / worktree-prefix conventions in Phase D.
# Defaults below match the upstream GM "main" worker naming; once Larry settles
# on his own pattern (e.g., sysprompt-forge / /tmp/wt-forge-*), update these.
SYSPROMPT_FILTER = "sysprompt-main"
WORKTREE_PREFIX = "/tmp/wt-main-"


def log(level: str, msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


def get_main_agent_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"claude.*{SYSPROMPT_FILTER}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0:
        return []
    pids = []
    for s in out.stdout.strip().split("\n"):
        if s and s.isdigit():
            pids.append(int(s))
    return pids


def proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def proc_etime_secs(pid: int) -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return int(out.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def worktree_branch_shipped(worktree: str) -> bool:
    """Return True if the worktree's branch has a MERGED PR on origin.

    Detection: get the worker's branch name from the worktree, then query
    GitHub via `gh pr list --head <branch> --state merged`. If a merged PR
    exists for the branch, the work has shipped to main. This is robust
    against squash-merge SHA rewriting AND admin-merge-without-delete-branch
    (both observed patterns on this repo).
    """
    try:
        # Get the local branch name from the worktree
        branch = subprocess.run(
            ["git", "-c", f"safe.directory={worktree}", "-C", worktree,
             "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
        if branch.returncode != 0:
            return False
        branch_name = branch.stdout.strip()
        if not branch_name or branch_name == "main":
            return False
        # Query GitHub for a merged PR on this branch
        gh = subprocess.run(
            ["gh", "pr", "list", "--head", branch_name, "--state", "merged",
             "--limit", "1", "--json", "number,state"],
            capture_output=True, text=True, timeout=20, cwd=worktree,
        )
        if gh.returncode != 0:
            return False
        # Non-empty array = merged PR exists for this branch
        return gh.stdout.strip() not in ("", "[]")
    except (subprocess.TimeoutExpired, OSError):
        return False


def worktree_recently_modified(worktree: str) -> bool:
    """Return True if any file under worktree was modified within MAX_RECENT_FILE_ACTIVITY_SECS."""
    cutoff = MAX_RECENT_FILE_ACTIVITY_SECS // 60  # find -mmin uses minutes
    try:
        # cd into worktree first to avoid 'Failed to restore working dir' from /root
        out = subprocess.run(
            ["find", ".", "-maxdepth", "5", "-type", "f",
             "-not", "-path", "./node_modules/*",
             "-not", "-path", "./.git/*",
             "-mmin", f"-{cutoff}", "-print", "-quit"],
            capture_output=True, text=True, timeout=15, cwd=worktree,
        )
        # If find produced any output, there's recent activity
        return bool(out.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        # On error, assume recently modified (safer — don't kill)
        return True


def is_zombie(pid: int) -> tuple[bool, str]:
    """Return (is_zombie, reason). Detects PATTERN A and PATTERN B."""
    cwd = proc_cwd(pid)
    if cwd is None:
        return False, "cwd-unreadable"
    etime = proc_etime_secs(pid)
    if etime is None:
        return False, "etime-unreadable"

    # PATTERN A — deleted-cwd zombie
    if "(deleted)" in cwd:
        if etime < MIN_DELETED_CWD_AGE_SECS:
            return False, f"pattern-A-too-fresh ({etime}s < {MIN_DELETED_CWD_AGE_SECS}s)"
        return True, f"pattern-A deleted-cwd etime={etime}s"

    # PATTERN B — completed-PR zombie
    if not cwd.startswith(WORKTREE_PREFIX):
        return False, f"cwd-not-worktree ({cwd[:60]})"
    if etime < MIN_COMPLETED_PR_AGE_SECS:
        return False, f"pattern-B-too-fresh ({etime}s < {MIN_COMPLETED_PR_AGE_SECS}s)"
    if not os.path.isdir(cwd):
        return False, "worktree-dir-missing"
    if not worktree_branch_shipped(cwd):
        return False, "branch-has-no-merged-PR (work not yet shipped)"
    if worktree_recently_modified(cwd):
        return False, f"recent-file-activity (<{MAX_RECENT_FILE_ACTIVITY_SECS//60}min)"
    return True, f"pattern-B completed-PR etime={etime}s, merged-PR-on-branch, idle"


def kill_zombie(pid: int, reason: str, expected_starttime: int | None = None) -> bool:
    # PID-reuse guard: is_zombie() makes slow blocking calls (gh pr list, find)
    # between the pgrep snapshot and here, so the PID could have exited and been
    # recycled to an unrelated process. Re-verify it is STILL a sysprompt-main
    # claude worker with the same start time before the irreversible SIGKILL.
    # A missing baseline start time (None) means we never had a stable identity
    # to compare against — on the droplet that only happens if the PID already
    # exited (nothing to kill), so fail closed rather than fall back to the
    # weaker cmdline-only check.
    if expected_starttime is None or not pid_identity.still_same_process(
        pid, expected_starttime, require_cmdline_substr=SYSPROMPT_FILTER
    ):
        log("SKIP", f"pid={pid} identity unverifiable/changed before kill (PID reuse?); "
                    f"not killing. reason={reason}")
        return False
    try:
        os.kill(pid, signal.SIGKILL)
        log("HEALED", f"pid={pid} action=sigkill reason={reason}")
        return True
    except OSError as exc:
        log("ERROR", f"pid={pid} kill_failed: {exc}")
        return False


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    pids = get_main_agent_pids()
    scanned = len(pids)
    healed = 0
    pattern_a = 0
    pattern_b = 0
    for pid in pids:
        # Capture identity up front so the kill can detect a PID recycled during
        # is_zombie()'s slow checks.
        expected_starttime = pid_identity.proc_starttime(pid)
        is_z, reason = is_zombie(pid)
        if is_z:
            if kill_zombie(pid, reason, expected_starttime):
                healed += 1
                if "pattern-A" in reason:
                    pattern_a += 1
                elif "pattern-B" in reason:
                    pattern_b += 1
    log(
        "HEARTBEAT",
        f"scanned={scanned} healed={healed} pattern_a={pattern_a} pattern_b={pattern_b} "
        f"threshold_a={MIN_DELETED_CWD_AGE_SECS}s threshold_b={MIN_COMPLETED_PR_AGE_SECS}s",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
