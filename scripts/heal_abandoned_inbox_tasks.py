#!/usr/bin/env python3
"""
heal_abandoned_inbox_tasks.py — Auto-recover tasks stuck in inboxes after worker
exited silently without completion handler running.

Mandated by: agent-core/runbooks/cycle-prompt.md AIRTIGHT PLAN v1
              ZERO-LLM HEALER CONSTELLATION (cycle iter 113, 2026-05-05)

Failure mode it heals:
  Cycle iter 113 (2026-05-05) discovered Surfaces B + D from Lindsay's
  bulk-email plan stuck in inboxes/main/ for 34+ minutes. The orchestrator
  had Submitted them at 22:35:33, workers created worktrees at 22:35:36,
  then claude exited silently (no Completed/Permanently failed log entry).
  Worktrees were cleaned by ourliberty-worktree-cleanup but the COMPLETION HANDLER
  never ran (because claude didn't exit cleanly), so the original task
  file was never moved out of inbox. Meanwhile orchestrator's in-memory
  submitted_tasks cache used Path(k).exists() to gate re-pickup — since
  the file STILL exists in inbox, the cache entry is preserved and the
  orchestrator skips it on every subsequent main-loop iteration.

  Net effect: customer-priority work permanently abandoned, orchestrator
  cycles past it indefinitely, no alert fires.

  Manual workaround used iter 113: rename file with new suffix → orchestrator
  sees it as new task → re-dispatches normally. This healer automates that.

What it does:
  Every 10 min, scan inboxes/main/*.json (and other agent inboxes that have
  the same pattern). For each task file at the top level:
    1. Check mtime — only consider files >ABANDON_AGE_MINUTES old (default 30)
    2. Cross-check: no lease file at state/dispatch-leases/<task_id>.lock
    3. Cross-check: no active claude worker with cwd containing the task name
    4. If all 3 conditions met → file is abandoned
  Rename file with `-recovery-<UTCtimestamp>.json` suffix to bypass
  orchestrator's submitted_tasks cache. This forces re-dispatch on next
  orchestrator main-loop iteration.

Safe-by-construction:
  - Conservative thresholds (30 min mtime, lease check, worker check) — false-
    positive risk minimized; legitimate slow builds with active workers pass through
  - Reversible (rename only — original task body preserved verbatim)
  - Lease-aware (won't touch tasks with active dispatch-leases/*.lock)
  - Worker-aware (won't touch tasks with claude procs running in their worktree)
  - Kill-switch aware (/home/larry/agents/healers.disabled)
  - Read-only on GitHub
  - Emits HEARTBEAT every run + HEALED on each rename

Verification (Empirical-Verification Gate per PRIME DIRECTIVE):
  - First HEALED: line within 24h proves healer detects the pattern
  - Iter 113 already empirically verified the rename trick (B+D picked up
    by orchestrator within 30s of rename); healer just automates it.
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path("/home/larry/agents")
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_abandoned_inbox_tasks.log"
INBOXES_ROOT = AGENTS_ROOT / "inboxes"
LEASE_DIR = AGENTS_ROOT / "state" / "dispatch-leases"

# Only scan these agents' top-level inboxes — they have the workflow pattern
# where workers create worktrees but the completion handler is supposed to move
# the file out. Other agents (e.g. observers like pulse) have different patterns
# and are out of scope for this healer.
TARGET_AGENTS = ("forge", "beacon")

# Iter 117 calibration: ship-plan tasks legitimately take 45-90 min for the
# full workflow (build → fix-lint → push → CI → PR open → walkthrough →
# merge). 30 min was too aggressive — caused false-positive recovery attempts
# on actively-self-completing workers (iter 115/116 lesson). 60 min preserves
# stuck-detection while letting workers finish their natural cycle.
ABANDON_AGE_MINUTES = 60


def log(level: str, msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


def get_active_claude_cwds() -> set[str]:
    """Return set of basenames of cwds for all active claude workers."""
    cwds: set[str] = set()
    try:
        out = subprocess.run(
            ["pgrep", "-f", "claude.*-p"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return cwds
    if out.returncode != 0:
        return cwds
    for pid_str in out.stdout.strip().split("\n"):
        if not pid_str:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid_str}/cwd")
            cwds.add(os.path.basename(cwd))
        except OSError:
            continue
    return cwds


def has_active_worker(task_id: str, active_cwds: set[str]) -> bool:
    """A worktree is named `wt-<agent>-<task_id_truncated>-<timestamp>`.
    Check if any active worker's cwd basename matches this specific task_id.

    Iter 117 fix: previous 30-char truncation was not unique enough for
    parallel surface dispatches with shared prefix. False-positive matches
    caused healer to skip recovery on genuinely-abandoned tasks because a
    sibling surface's worker was active.

    New approach: take a longer, more discriminating prefix that includes
    the full distinguishing slug. We use 50 chars which captures most
    realistic task slugs without cross-surface overlap. Filesystem worktree
    names are typically truncated around 60-70 chars so 50 is a safe match
    window."""
    truncated = task_id[:50]
    for cwd in active_cwds:
        if cwd.startswith("wt-") and truncated in cwd:
            return True
    return False


def has_lease(task_id: str) -> bool:
    return (LEASE_DIR / f"{task_id}.lock").exists()


def is_recovery_filename(filename: str) -> bool:
    """Skip files we've already renamed (avoid infinite recovery loop)."""
    return "-recovery-" in filename


def recover_task(task_file: Path, agent: str, age_min: int) -> bool:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    new_name = f"{task_file.stem}-recovery-{ts}.json"
    new_path = task_file.parent / new_name
    try:
        shutil.move(str(task_file), str(new_path))
    except OSError as exc:
        log("ERROR", f"{agent}/{task_file.name} rename_failed: {exc}")
        return False
    log(
        "HEALED",
        f"{agent}/{task_file.name} action=rename-for-redispatch new_name={new_name} age_min={age_min}",
    )
    return True


def scan_agent_inbox(agent: str, active_cwds: set[str]) -> tuple[int, int]:
    """Return (scanned, healed)."""
    inbox = INBOXES_ROOT / agent
    if not inbox.is_dir():
        return 0, 0
    scanned = 0
    healed = 0
    cutoff = datetime.now(timezone.utc).timestamp() - (ABANDON_AGE_MINUTES * 60)
    for task_file in inbox.glob("*.json"):
        scanned += 1
        if is_recovery_filename(task_file.name):
            continue  # already recovered, skip
        try:
            mtime = task_file.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue  # not old enough
        task_id = task_file.stem
        if has_lease(task_id):
            continue  # actively dispatched
        if has_active_worker(task_id, active_cwds):
            continue  # worker grinding
        age_min = int((datetime.now(timezone.utc).timestamp() - mtime) / 60)
        if recover_task(task_file, agent, age_min):
            healed += 1
    return scanned, healed


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    if not INBOXES_ROOT.is_dir():
        log("HEARTBEAT", "inboxes root missing, nothing to do")
        return 0
    active_cwds = get_active_claude_cwds()
    total_scanned = 0
    total_healed = 0
    for agent in TARGET_AGENTS:
        s, h = scan_agent_inbox(agent, active_cwds)
        total_scanned += s
        total_healed += h
    log(
        "HEARTBEAT",
        f"scanned={total_scanned} healed={total_healed} "
        f"active_workers={len(active_cwds)} agents={','.join(TARGET_AGENTS)} "
        f"threshold_min={ABANDON_AGE_MINUTES}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
