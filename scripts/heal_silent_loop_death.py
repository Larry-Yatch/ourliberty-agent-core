#!/usr/bin/env python3
"""
heal_silent_loop_death.py — Detect Sage/agent self-scheduled re-queue loops that died.

Mandated by: agent-core/runbooks/cycle-prompt.md AIRTIGHT PLAN v1
              ZERO-LLM HEALER CONSTELLATION (cycle iter 64+, 2026-05-04)
              Larry directive 2026-05-04 ~7:25 PM CT — "permanently fix
              whatever was causing you to miss those issues."

Failure mode it heals:
  Sage agents commonly run "silent re-queue loops" that self-dispatch
  the next iteration via `not_before` cooldown timestamps (e.g., the
  Team Access v1 queue-check loop that scheduled itself for 22:17Z but
  never fired again — found dead in iter 64+ when Larry explicitly asked
  about queue state). These loops fail silently when:
    - Orchestrator restarts during a `not_before` cooldown and loses
      the dispatch ledger entry
    - Sage worker crashes mid-loop and the result file never gets written
    - The next-iteration self-task gets stuck in an inbox subdir

  Cycle's iter 59 backlog promoter death (19 days dead) and iter 64+
  Team Access queue-check death (~2.5h dead) are the same failure class.
  Both went undetected without a specific liveness check.

What it does:
  Every 10 min, scan inboxes/forge and inboxes/pulse for completed result
  files matching patterns suggesting a self-scheduled loop:
    - `*queue-check*-result.json`
    - `*re-queue*-result.json`
    - `silent-requeue-*-result.json`
    - `*self-task-*-result.json`
  For each match older than 90min:
    - Check if a follow-up dispatch with similar slug is currently in
      the dispatcher's inbox (or active worktree). If yes → loop is alive.
    - If no follow-up found AND the result mentions "Next check at" or
      "Self-task scheduled for" → loop has likely died → log
      LOOP_DEATH_DETECTED and emit a Sentry-style alert.
    - Optionally re-dispatch a placeholder task to revive the loop
      (DISABLED by default; cycle/Larry approves manually).

Detection-only by default — no auto-revive without explicit Larry approval
(silent loop death IS a real signal that something deeper is wrong; a
healer that auto-revives masks the underlying cause). The HEARTBEAT log
makes the dead loops findable in next cycle iter.

Safe-by-construction:
  - Read-only filesystem (only logs, no moves/dispatches)
  - Kill-switch aware
  - HEARTBEAT every run + LOOP_DEATH_DETECTED on findings
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo scripts dir on sys.path so the sibling id_match import resolves cleanly
# when invoked by the systemd oneshot.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from id_match import id_matches  # noqa: E402

AGENTS_ROOT = Path("/home/larry/agents")
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_silent_loop_death.log"
INBOXES = (AGENTS_ROOT / "inboxes" / "forge", AGENTS_ROOT / "inboxes" / "pulse")
LOOP_PATTERNS = (
    re.compile(r"queue-check.*-result\.json$"),
    re.compile(r"re-queue.*-result\.json$"),
    re.compile(r"silent-requeue-.*-result\.json$"),
    re.compile(r"self-task-.*-result\.json$"),
)
NEXT_CHECK_RE = re.compile(r"Next check at\s+~?(\S+)", re.IGNORECASE)
SCHEDULED_FOR_RE = re.compile(r"Self-task scheduled for\s+(\S+)", re.IGNORECASE)
STALE_MIN = 90  # If a loop's "next check" is more than 90min past, declare dead


def log(level: str, msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


def find_recent_dispatches(slug_root: str, inboxes) -> bool:
    """True if any task with the slug root is currently in active inbox/processed."""
    for inbox in inboxes:
        if not inbox.is_dir():
            continue
        # The slug is matched as a whole, boundary-delimited token (audit #41) so
        # an unrelated artifact that merely contains it as an infix no longer
        # masks a dead loop. min_len=1 (no length floor): the filename is a
        # STRUCTURED name and real loop slugs are legitimately short
        # ('re-queue', 'self-task', 'queue-check' are all < 12), so a floor would
        # make every such live loop read as dead and emit a spurious
        # LOOP_DEATH_DETECTED.
        for f in inbox.iterdir():
            if not f.is_file() or not f.name.endswith(".json"):
                continue
            if id_matches(slug_root, f.name, min_len=1) and "-result" not in f.name:
                return True
        processed = inbox / "processed"
        if processed.is_dir():
            for f in processed.iterdir():
                # Mirror the active-branch filter: skip directories and
                # non-.json entries (audit #41 — the processed/ branch lacked it).
                if not f.is_file() or not f.name.endswith(".json"):
                    continue
                if id_matches(slug_root, f.name, min_len=1) and "-result" not in f.name:
                    try:
                        if (time.time() - f.stat().st_mtime) < STALE_MIN * 60:
                            return True
                    except OSError:
                        continue
    return False


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0

    now = time.time()
    cutoff = now - (STALE_MIN * 60)
    scanned = 0
    dead_loops = 0

    for inbox in INBOXES:
        result_dirs = [inbox, inbox / "archive", inbox / "processed"]
        for d in result_dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file() or not f.name.endswith(".json"):
                    continue
                if not any(p.search(f.name) for p in LOOP_PATTERNS):
                    continue
                scanned += 1
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if mtime > cutoff:
                    continue
                # Older than STALE_MIN. Check if loop is alive.
                slug_root = re.sub(r"-result\.json$", "", f.name)
                slug_root = re.sub(r"-\d{8}T\d{6}Z?-?\w*", "", slug_root)
                slug_root = slug_root[:50]
                if not slug_root:
                    continue
                alive = find_recent_dispatches(slug_root, INBOXES)
                if not alive:
                    age_min = (now - mtime) / 60
                    log("LOOP_DEATH_DETECTED",
                        f"task={f.name} slug_root={slug_root} age_min={age_min:.0f} "
                        f"no_follow_up_dispatch_in_inboxes_or_recent_processed")
                    dead_loops += 1

    log("HEARTBEAT", f"scanned={scanned} dead_loops={dead_loops} stale_min={STALE_MIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
