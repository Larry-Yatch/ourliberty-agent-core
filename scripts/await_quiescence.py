#!/usr/bin/env python3
"""
await_quiescence.py — Wait for all agents to be idle before sync.

Checks:
  1. No files in any agent's inbox (or all have future not_before timestamps)
  2. No running Claude processes (via concurrency_guard slot files)
  3. No active agent_runner processes

If not quiescent, sleeps 10s and rechecks up to --timeout seconds.
Exit 0 if quiescent, exit 1 if timeout reached.
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo scripts dir on sys.path so the sibling concurrency_guard import resolves.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

AGENTS_ROOT = "/home/larry/agents"
INBOXES_DIR = os.path.join(AGENTS_ROOT, "inboxes")


def get_inbox_files() -> list[str]:
    """Return list of JSON files across all agent inboxes."""
    inbox_files = []
    if not os.path.isdir(INBOXES_DIR):
        return inbox_files

    for agent_dir in os.listdir(INBOXES_DIR):
        agent_inbox = os.path.join(INBOXES_DIR, agent_dir)
        if not os.path.isdir(agent_inbox):
            continue
        for fname in os.listdir(agent_inbox):
            if fname.endswith(".json"):
                inbox_files.append(os.path.join(agent_inbox, fname))

    return inbox_files


def is_future_task(filepath: str) -> bool:
    """Check if a task has a not_before timestamp in the future."""
    try:
        with open(filepath) as f:
            data = json.load(f)
        not_before = data.get("not_before")
        if not not_before:
            return False
        # Parse ISO 8601 timestamp
        nb_dt = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
        return nb_dt > datetime.now(timezone.utc)
    except (json.JSONDecodeError, ValueError, OSError):
        return False


def has_pending_tasks() -> bool:
    """Check if any inbox has tasks ready to execute (not deferred to future)."""
    inbox_files = get_inbox_files()
    for filepath in inbox_files:
        if not is_future_task(filepath):
            return True
    return False


def has_running_agents() -> bool:
    """True if any concurrency-guard slot is occupied (a live claude worker
    holds capacity).

    Reads the guard's OWN accounting via concurrency_guard.active_count()
    instead of globbing `.concurrency-guard.*` — the old glob matched a
    top-level path the guard never writes (it stores config/.concurrency-guard.json),
    so this always returned False and quiescence could be declared mid-dispatch,
    letting a git sync/reset run while workers held slots (audit #5).

    Fail-safe: if the guard can't be read, assume agents MIGHT be running
    (return True / not-quiescent) rather than greenlight a sync."""
    try:
        import concurrency_guard
        return concurrency_guard.get_guard().active_count() > 0
    except Exception:
        return True


def has_claude_processes() -> bool:
    """Check if any claude/agent_runner processes are running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "agent_runner.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude.*--print"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return False


def is_quiescent() -> tuple[bool, str]:
    """Check all quiescence conditions. Returns (is_quiet, reason_if_not)."""
    if has_pending_tasks():
        return False, "Pending tasks in inbox(es)"
    if has_running_agents():
        return False, "Running agent(s) detected via concurrency guard"
    if has_claude_processes():
        return False, "Active claude/agent_runner process(es) detected"
    return True, "All quiet"


def main():
    parser = argparse.ArgumentParser(
        description="Wait for all agents to be idle before sync"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Maximum seconds to wait for quiescence (default: 300)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Seconds between checks (default: 10)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print status on each check",
    )
    args = parser.parse_args()

    start_time = time.time()
    deadline = start_time + args.timeout

    while True:
        quiet, reason = is_quiescent()

        if quiet:
            elapsed = time.time() - start_time
            print(
                f"[quiescence] All agents idle after {elapsed:.0f}s. Safe to sync.",
                file=sys.stderr,
            )
            sys.exit(0)

        remaining = deadline - time.time()
        if remaining <= 0:
            print(
                f"[quiescence] TIMEOUT after {args.timeout}s. Reason: {reason}",
                file=sys.stderr,
            )
            sys.exit(1)

        if args.verbose:
            print(
                f"[quiescence] Not quiet: {reason}. "
                f"Retrying in {args.interval}s ({remaining:.0f}s remaining)...",
                file=sys.stderr,
            )

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
