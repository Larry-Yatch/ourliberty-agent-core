#!/usr/bin/env python3
"""
heal_recovery_already_merged.py — Archive recovery/result tasks whose target PR is already merged.

Mandated by: agent-core/runbooks/cycle-prompt.md AIRTIGHT PLAN v1
              ZERO-LLM HEALER CONSTELLATION (cycle iter 3, 2026-05-02)

Failure mode it heals:
  ship_completion_watcher dispatches recovery tasks (`00-RECOVER-*.json`) when
  a sub-task PR appears stale. Sometimes the original work HAS already merged
  but the recovery task wasn't archived. The recovery task then sits in the
  inbox indefinitely, watchdog flags it as stale every 2 min, and a worker
  may even re-build the already-merged PR. Three such files were found on
  2026-05-02 (PR-1f → #2108, PR-2a → #2110, plus stale cohort verdict).

What it does:
  Every 5 min, scan inboxes/{beacon,forge,mirror,pulse}/*.json
  for files matching the recovery pattern. For each, extract sub_task_id and
  query GitHub for merged PRs with that tag in the title. If the PR is already
  merged, move the file to inboxes/<agent>/archive/ with a sidecar reason file.

Safe-by-construction:
  - Idempotent (already-archived files have no effect)
  - Reversible (file moved, not deleted; sidecar records why)
  - Kill-switch aware (`/home/larry/agents/healers.disabled` → exit immediately)
  - Read-only on GitHub (no PR mutations)
  - Emits HEARTBEAT every run + HEALED on each archive
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Path isolation: honor OURLIBERTY_AGENTS_ROOT (the repo-wide convention — see
# agent_runner / dashboard_api / the other healers) so tests can point this at a
# tmp tree and droplet overrides work. Was a hardcoded "/home/larry/agents",
# which silently scanned the real inbox during tests and ignored the override
# every sibling healer respects.
AGENTS_ROOT = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT", "/home/larry/agents"))
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_recovery_already_merged.log"
INBOX_AGENTS = ("beacon", "forge", "mirror", "pulse")
RECOVERY_PATTERNS = (re.compile(r"00-RECOVER-"), re.compile(r"recover-"), re.compile(r"forge-stage3-results-"))
PR_TAG_RE = re.compile(r"PR-[0-9]+[a-z]?")

_DEFAULT_VERIFY_REPO = "Larry-Yatch/ourliberty-agent-core"


def verify_repos() -> list[str]:
    """Repos to search for an already-merged target PR, in priority order.

    A recovery task's target may live in any repo the constellation ships to,
    so honor a comma-separated `OURLIBERTY_VERIFY_REPOS` (falling back to the
    single `OURLIBERTY_VERIFY_REPO`, then the default). Scanning only one
    hardcoded repo meant a merged PR in a sibling repo looked un-merged, so the
    recovery task sat forever and watchdog re-flagged it — the exact stuck-task
    failure mode this healer exists to clear."""
    multi = os.environ.get("OURLIBERTY_VERIFY_REPOS")
    if multi:
        repos = [r.strip() for r in multi.split(",") if r.strip()]
        if repos:
            return repos
    return [os.environ.get("OURLIBERTY_VERIFY_REPO", _DEFAULT_VERIFY_REPO)]


def log(level: str, msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = f"[{ts}] [{level}] {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)


def query_merged_pr(sub_task_tag: str) -> dict | None:
    """Return dict with PR number/title/mergedAt/repo if a merged PR in any
    configured repo has the tag in its title. Searches `verify_repos()` in
    order and returns the first hit; a query error on one repo doesn't abort
    the others."""
    for repo in verify_repos():
        try:
            r = subprocess.run(
                ["gh", "pr", "list",
                 "--repo", repo,
                 "--state", "merged",
                 "--search", f"[{sub_task_tag}] in:title",
                 "--json", "number,title,mergedAt",
                 "--limit", "5"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                continue
            prs = json.loads(r.stdout)
            for pr in prs:
                if f"[{sub_task_tag}]" in pr.get("title", ""):
                    pr["repo"] = repo
                    return pr
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            continue
    return None


def archive_file(task_file: Path, reason: str) -> None:
    archive_dir = task_file.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / task_file.name
    shutil.move(str(task_file), str(dest))
    sidecar = archive_dir / f"{task_file.stem}.archive-reason.txt"
    sidecar.write_text(f"archived_at_utc={datetime.now(timezone.utc).isoformat()}\nreason={reason}\nhealer=heal_recovery_already_merged.py\n")


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    healed = 0
    scanned = 0
    for agent in INBOX_AGENTS:
        inbox = AGENTS_ROOT / "inboxes" / agent
        if not inbox.is_dir():
            continue
        for task_file in inbox.glob("*.json"):
            scanned += 1
            if not any(p.search(task_file.name) for p in RECOVERY_PATTERNS):
                continue
            try:
                with open(task_file) as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            sub_task = payload.get("sub_task_id")
            if not sub_task:
                # Stale cohort-verdict pattern: archive if observation_end_utc passed
                obs_end = payload.get("observation_end_utc")
                if obs_end:
                    try:
                        end_dt = datetime.fromisoformat(obs_end.replace("Z", "+00:00"))
                        if end_dt < datetime.now(timezone.utc):
                            archive_file(task_file, f"cohort_verdict_observation_window_expired obs_end={obs_end}")
                            log("HEALED", f"{agent}/{task_file.name} action=archive_expired_cohort_verdict")
                            healed += 1
                    except (ValueError, TypeError):
                        pass
                continue
            pr = query_merged_pr(sub_task)
            if pr:
                repo = pr.get("repo", _DEFAULT_VERIFY_REPO)
                archive_file(task_file, f"target_pr_already_merged repo={repo} pr=#{pr['number']} mergedAt={pr['mergedAt']}")
                log("HEALED", f"{agent}/{task_file.name} action=archive_already_merged repo={repo} pr=#{pr['number']}")
                healed += 1
    log("HEARTBEAT", f"scanned={scanned} healed={healed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
