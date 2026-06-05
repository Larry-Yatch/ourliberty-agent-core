#!/usr/bin/env python3
"""
heal_restart_dedup_obsolete.py — Archive stale RESTART_DEDUP duplicates.

Mandated by: agent-core/runbooks/cycle-prompt.md AIRTIGHT PLAN v1
              ZERO-LLM HEALER CONSTELLATION (cycle iter 64, 2026-05-04)

Failure mode it heals:
  When the orchestrator restarts (e.g., to clear schema cache), it
  detects in-flight tasks whose ledger shows recent dispatch, defers
  them to prevent immediate re-dispatch, and renames the file with a
  timestamp prefix:
    `admin-backlog-1417-1412-1411-build-20260504T2135Z.json`
      → `20260504231311-admin-backlog-1417-1412-1411-build-20260504T2135Z.json`

  The original work usually completes (PR merges), but the timestamp-
  prefixed duplicate sits in main inbox indefinitely. After the dedup
  window expires (~30min), main worker may re-dispatch it — building a
  duplicate PR for already-closed work, wasting tokens, possibly
  reintroducing already-resolved bugs.

  Iter 64 found 2 such files in main inbox (admin-backlog-1417 +
  concierge-backlog-1644) for work that PR #2180 / #2178 had already
  merged. Yanked manually; this healer prevents future recurrence.

What it does:
  Every 5 min, scan inboxes/main for files matching `^\\d{14}-.+\\.json$`
  AND older than 2h. Move them to inboxes/main/archive/ with HEALED log.

Why 2h:
  - Orchestrator's RESTART_DEDUP window is 30min. After that, it could
    legitimately re-dispatch from the prefixed file. So we don't want
    to archive within that window.
  - A 2h grace gives the original work plenty of time to complete OR
    to be re-picked-up by a worker post-dedup-window. If still here
    after 2h, the file is stale.
  - Tunable via STALE_HOURS const if pattern needs adjustment.

Safe-by-construction:
  - Move (not delete) — preserves audit trail
  - Idempotent — already-archived files don't re-trigger
  - Kill-switch aware
  - Conservative threshold (2h) avoids false positives during heavy
    backpressure-V3 deferral cycles
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path("/home/larry/agents")
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_restart_dedup_obsolete.log"
MAIN_INBOX = AGENTS_ROOT / "inboxes" / "main"
ARCHIVE_DIR = MAIN_INBOX / "archive"
STALE_HOURS = 2.0
PREFIX_RE = re.compile(r"^\d{14}-.+\.json$")


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


def _atomic_archive_move(src: Path, target: Path) -> Path:
    """Move ``src`` into the archive at ``target`` (or the next free
    ``<name>-dupN``) — race-free — and return the final path.

    Audit #54 + PR-E2 residual: the previous ``if target.exists():`` guard then
    ``shutil.move`` had a TOCTOU window — a concurrent writer or a parallel healer
    run could create the destination between the existence check and the move, and
    ``shutil.move`` would silently overwrite it, destroying a prior archived task
    (defeating the module's "Move, not delete — preserves audit trail" guarantee).

    ``os.link`` is an ATOMIC no-clobber primitive: it raises ``FileExistsError`` if
    the destination already exists. We link-then-unlink, looping over ``-dupN``
    suffixes — whoever wins the link owns that name; everyone else falls through to
    the next free one, so no earlier archived copy is ever clobbered. Inbox and
    archive live under the same agents tree (one filesystem), so the hardlink
    always succeeds; a cross-device ``OSError`` propagates to the caller's archive-
    failed handler rather than risking a clobber. A crash between link and unlink
    leaves a harmless duplicate (src re-archived as ``-dup1`` next run), never loss.
    """
    candidate = target
    i = 0
    while True:
        try:
            os.link(src, candidate)
            break
        except FileExistsError:
            i += 1
            candidate = target.parent / f"{target.stem}-dup{i}{target.suffix}"
    os.unlink(src)
    return candidate


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    if not MAIN_INBOX.is_dir():
        log("HEARTBEAT", "main inbox dir missing, nothing to do")
        return 0
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    now = time.time()
    threshold = now - (STALE_HOURS * 3600)
    healed = 0
    scanned = 0

    for f in MAIN_INBOX.iterdir():
        if not f.is_file() or not f.name.endswith(".json"):
            continue
        if not PREFIX_RE.match(f.name):
            continue
        scanned += 1
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if mtime > threshold:
            continue
        target = ARCHIVE_DIR / f.name
        try:
            _atomic_archive_move(f, target)
            age_h = (now - mtime) / 3600
            log("HEALED", f"action=archive_restart_dedup_obsolete task={f.name} age_h={age_h:.1f}")
            healed += 1
        except OSError as e:
            log("ERROR", f"archive_failed task={f.name} err={e}")

    log("HEARTBEAT", f"scanned={scanned} healed={healed} threshold_hours={STALE_HOURS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
