#!/usr/bin/env python3
"""
heal_empty_inbox_files.py — Auto-archive empty/trivial *.json files in agent inboxes.

Mandated by: agent-core/runbooks/cycle-prompt.md AIRTIGHT PLAN v1
              ZERO-LLM HEALER CONSTELLATION (cycle iter 109-110, 2026-05-05)

Failure mode it heals:
  Operators discovered 0-byte signal-*.json files in agent inboxes (e.g. an
  observer's inbox) that had been sitting for many hours. Some upstream writer
  wrote the filename but never wrote content (probably a failed/aborted write
  or a cron that fired then exited before finishing the payload). An empty
  .json file in an inbox confuses agents that try to parse it (they hit
  JSONDecodeError) and pollutes Phase 2 stuck-scans because the file's mtime
  is fresh but the content is unparseable.

  This is a generalizable pattern: any inbox-writing process that fails between
  filename-creation and content-write leaves orphan empty files. This healer
  archives them deterministically so cycle's stuck-scan doesn't waste time on
  them and agents don't waste tokens trying to triage unparseable input.

What it does:
  Every 15 min, scan ALL inboxes/<agent>/*.json. For each file that is:
    - 0 bytes (empty), OR
    - <50 bytes AND not valid JSON, OR
    - Valid JSON but only contains an empty dict/list ({} or [])
  Move to inboxes/<agent>/blocked/empty-files/<YYYYMMDD>/<filename>
  Drop sidecar with archived_at, reason, agent, original_filename, file_size.
  Log HEALED: per archive + HEARTBEAT every run.

Safe-by-construction:
  - Idempotent (already-archived files have no effect)
  - Reversible (file moved + sidecar preserves origin path + size)
  - Conservative heuristic (3 conditions must match — false-positive risk
    minimized; legitimate small payloads with at least one substantive key
    pass through untouched)
  - Kill-switch aware (/home/larry/agents/healers.disabled)
  - Read-only on GitHub (no PR mutations)
  - Emits HEARTBEAT every run + HEALED on each archive

Verification (Empirical-Verification Gate per PRIME DIRECTIVE):
  - First HEALED: line within 24h of cron install proves the healer fires
    against the next empty-file write
  - Or: cycle stuck-scan no longer surfaces 0-byte files >24h after install
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path("/home/larry/agents")
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_empty_inbox_files.log"
INBOXES_ROOT = AGENTS_ROOT / "inboxes"

TRIVIAL_THRESHOLD_BYTES = 50
TRIVIAL_JSON_VALUES = ({}, [], "", None)

# Grace window before a file is eligible for empty/corrupt classification.
# A file written in the last MIN_FILE_AGE_SECONDS may still be mid-write by a
# NON-atomic producer (audit #55): inbox_watcher.bump_requeue rewrites a task
# in place with write_text (truncate-then-stream → a transient 0-byte/partial
# window), and the upstream/cron signal-*.json writers this healer was built
# for fail between filename-creation and content-write (see module docstring) —
# neither routes through safe_write_inbox's atomic os.replace. Judging such a
# file empty/corrupt would archive a real task out from under its writer, which
# the "reversible/idempotent" safety claims do not cover. Skipping it this tick
# costs nothing: a genuinely empty file is still <grace> seconds old at most and
# gets archived on the next 15-min run.
MIN_FILE_AGE_SECONDS = 10


def log(level: str, msg: str) -> None:
    # Best-effort: a full/read-only/permission-denied log filesystem must not
    # crash the healer (audit #42 / PR-F — main() calls log("HEARTBEAT", ...) as
    # its final statement with no surrounding try/except, so an OSError here
    # would exit the oneshot non-zero and record the tick as failed).
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] [{level}] {msg}\n")
    except OSError:
        pass


def classify(task_file: Path) -> tuple[bool, str, int]:
    """Return (is_empty_or_trivial, reason, file_size_bytes)."""
    try:
        stat = task_file.stat()
    except OSError:
        return False, "stat-failed", 0
    size = stat.st_size
    # Grace window (audit #55): never judge a just-touched file — a non-atomic
    # writer may still be mid-write, and a 0-byte/partial read here would archive
    # a real task. Gate BEFORE the zero-byte check (an in-progress write is
    # exactly the 0-byte case we must not race).
    age_seconds = time.time() - stat.st_mtime
    if age_seconds < MIN_FILE_AGE_SECONDS:
        return False, f"too-fresh ({age_seconds:.1f}s < {MIN_FILE_AGE_SECONDS}s grace)", size
    if size == 0:
        return True, "zero-byte-file", 0
    if size > TRIVIAL_THRESHOLD_BYTES:
        return False, f"size-above-threshold ({size}B)", size
    # Small file — check if it's trivial JSON
    try:
        content = task_file.read_text()
    except OSError:
        return False, "read-failed", size
    stripped = content.strip()
    if not stripped:
        return True, "whitespace-only-content", size
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        # Small file that isn't valid JSON — orphan/aborted write
        return True, "invalid-json-small-file", size
    if parsed in TRIVIAL_JSON_VALUES:
        return True, f"trivial-json ({type(parsed).__name__})", size
    if isinstance(parsed, dict) and not parsed:
        return True, "empty-dict", size
    return False, "valid-non-trivial-content", size


def archive_task(task_file: Path, agent: str, reason: str, size: int) -> bool:
    archive_root = (
        INBOXES_ROOT / agent / "blocked" / "empty-files"
    )
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d")
    archive_dir = archive_root / bucket
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / task_file.name
    try:
        shutil.move(str(task_file), str(target))
    except OSError as exc:
        log("ERROR", f"{agent}/{task_file.name} archive_failed: {exc}")
        return False
    sidecar = archive_dir / f"{task_file.stem}.archive-reason.txt"
    sidecar.write_text(
        f"archived_at_utc={datetime.now(timezone.utc).isoformat()}\n"
        f"reason={reason}\n"
        f"agent={agent}\n"
        f"original_path=inboxes/{agent}/{task_file.name}\n"
        f"file_size_bytes={size}\n"
        f"healer=heal_empty_inbox_files.py\n"
        f"note=empty/trivial inbox files cause JSONDecodeError when agents try to parse, "
        f"pollute Phase 2 stuck-scans, and indicate a writer process that failed between "
        f"filename-creation and content-write. Track upstream root cause separately.\n"
    )
    return True


def scan_agent_inbox(agent: str) -> tuple[int, int]:
    """Return (scanned, healed)."""
    inbox = INBOXES_ROOT / agent
    if not inbox.is_dir():
        return 0, 0
    scanned = 0
    healed = 0
    for task_file in inbox.glob("*.json"):
        scanned += 1
        is_empty, reason, size = classify(task_file)
        if not is_empty:
            continue
        if archive_task(task_file, agent, reason, size):
            log(
                "HEALED",
                f"{agent}/{task_file.name} action=archive reason={reason} size_b={size}",
            )
            healed += 1
    return scanned, healed


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    if not INBOXES_ROOT.is_dir():
        log("HEARTBEAT", "inboxes root missing, nothing to do")
        return 0
    total_scanned = 0
    total_healed = 0
    agents_scanned = []
    for agent_dir in INBOXES_ROOT.iterdir():
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        if agent.startswith("_") or agent.startswith("."):
            continue
        s, h = scan_agent_inbox(agent)
        total_scanned += s
        total_healed += h
        agents_scanned.append(agent)
    log(
        "HEARTBEAT",
        f"scanned={total_scanned} healed={total_healed} agents={','.join(agents_scanned)}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
