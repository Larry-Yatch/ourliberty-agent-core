#!/usr/bin/env python3
"""
heal_blocked_inbox_age.py — Auto-archive stale tasks in inboxes/*/blocked/.

Mandated by: agent-core/runbooks/cycle-prompt.md AIRTIGHT PLAN v1
              ZERO-LLM HEALER CONSTELLATION (cycle iter 1, 2026-05-03 evening)

Failure mode it heals:
  Operators discovered <agent>/blocked/ directories holding stranded files
  from rapid retry storms — files that had been blocked for days with no
  drainage mechanism. The orchestrator moves rejected/blocked tasks to
  inboxes/<agent>/blocked/ but no script archives stale ones, so the dir
  grows unbounded as a permanent dead-letter.

  This is a systemic shape: defensive-archival of stranded files past a
  generous age threshold.

What it does:
  Every 15 min, scan inboxes/*/blocked/*.json. For each *.json file older than
  ARCHIVE_AGE_HOURS (default 48), move to inboxes/<agent>/blocked/.archive/
  with a sidecar .archive-reason.txt preserving the original payload reference.
  Uses 48h (vs human-inbox's 24h) because some blocked tasks are intentionally
  held briefly while a hotfix lands.

  Companion task: identify dispatchers that retry-storm into blocked/ and add
  upstream dedup. Iter 1+ open work.

Safe-by-construction:
  - Idempotent (already-archived files have no effect)
  - Reversible (file moved + sidecar preserves origin)
  - Kill-switch aware (/home/larry/agents/healers.disabled)
  - Read-only on GitHub (no PR mutations)
  - Emits HEARTBEAT every run + HEALED on each archive
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path("/home/larry/agents")
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_blocked_inbox_age.log"
INBOXES_ROOT = AGENTS_ROOT / "inboxes"
ARCHIVE_AGE_HOURS = 48


def log(level: str, msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    if not INBOXES_ROOT.is_dir():
        log("HEARTBEAT", "inboxes root missing, nothing to do")
        return 0
    healed_total = 0
    scanned_total = 0
    cutoff = datetime.now(timezone.utc).timestamp() - (ARCHIVE_AGE_HOURS * 3600)
    for blocked_dir in INBOXES_ROOT.glob("*/blocked"):
        if not blocked_dir.is_dir():
            continue
        agent = blocked_dir.parent.name
        archive_dir = blocked_dir / ".archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for task_file in blocked_dir.glob("*.json"):
            scanned_total += 1
            try:
                mtime = task_file.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            age_h = int((datetime.now(timezone.utc).timestamp() - mtime) / 3600)
            try:
                shutil.move(str(task_file), str(archive_dir / task_file.name))
                sidecar = archive_dir / f"{task_file.stem}.archive-reason.txt"
                sidecar.write_text(
                    f"archived_at_utc={datetime.now(timezone.utc).isoformat()}\n"
                    f"reason=blocked_inbox_age_exceeded_threshold\n"
                    f"agent={agent}\n"
                    f"age_hours_at_archive={age_h}\n"
                    f"healer=heal_blocked_inbox_age.py\n"
                    f"note=blocked tasks past threshold are unrecoverable; review companion dispatcher dedup\n"
                )
                log("HEALED", f"{agent}/blocked/{task_file.name} action=archive_stale age_h={age_h}")
                healed_total += 1
            except OSError as e:
                log("ERROR", f"{agent}/blocked/{task_file.name} archive_failed: {e}")
    log("HEARTBEAT", f"scanned={scanned_total} healed={healed_total} threshold_hours={ARCHIVE_AGE_HOURS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
