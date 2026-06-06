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
    2. Cross-check: no LIVE in-flight registry entry (state/in-flight/<task_id>.json
       with a signalable pid) — the real per-task "worker running" signal
    3. Cross-check: no active claude worker whose worktree belongs to the task
    4. If all 3 conditions met → file is abandoned
  Rename file with `-recovery-<UTCtimestamp>.json` suffix to bypass
  orchestrator's submitted_tasks cache. This forces re-dispatch on next
  orchestrator main-loop iteration.

Safe-by-construction:
  - Conservative thresholds (60 min mtime, in-flight check, worker check) —
    false-positive risk minimized; legitimate slow builds with active workers pass through
  - Reversible (rename only — original task body preserved verbatim)
  - In-flight-aware (won't touch a task with a live state/in-flight/*.json worker)
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

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Repo scripts dir on sys.path so the sibling id_match import resolves cleanly
# when invoked by systemd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from id_match import id_matches  # noqa: E402

AGENTS_ROOT = Path("/home/larry/agents")
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal_abandoned_inbox_tasks.log"
INBOXES_ROOT = AGENTS_ROOT / "inboxes"
# The per-task "this task is being processed by a live worker" signal:
# agent_runner writes state/in-flight/<task_stem>.json (with the worker `pid`)
# for the WHOLE duration of the claude run, and removes it on completion. The
# previous code checked state/dispatch-leases/<task_id>.lock, which nothing
# writes; the in-flight registry is the correct PER-TASK gate that replaced it.
#
# But the in-flight file does NOT cover the watcher's whole dispatch window.
# inbox_watcher acquires the per-AGENT lease `inbox:<agent>` and holds it across
# the ENTIRE process_task (acquire→release in agent_loop's finally), which
# includes the minutes-wide ensure_worktree_for_task setup (git fetch/add/WIP
# commit) that runs BEFORE the in-flight file is written and BEFORE the claude
# process exists. During that window has_in_flight_worker AND has_active_worker
# both return False, all gates pass, and the healer renames a task the watcher
# is actively dispatching — the work then gets double-billed (audit H1). An
# earlier comment removed the lease check on the belief the lease is "held only
# briefly during a poll"; that is wrong. The per-agent lease is the one signal
# covering that pre-registration window, so has_live_dispatch_lease() re-adds it
# (read-only — see that function) as an agent-level gate.
IN_FLIGHT_DIR = AGENTS_ROOT / "state" / "in-flight"

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


def _worktree_safe_stem(task_stem: str) -> str:
    """Reproduce the WORKTREE-domain sanitization so we can match a task
    against its `wt-<agent>-<safe_stem>-<ts>` directory. Maps every
    non-[alnum-_] char to '-' and truncates to 50. Matching the RAW task_id
    (as the old code did) silently failed for any task_id containing a '.',
    ':', or space — those become '-' in the worktree name, so the raw
    substring was never present and a live worker went undetected.

    MUST-MATCH INVARIANT (PR-A follow-up, audit #53): this mirrors the TWO
    live worktree namers byte-for-byte (same char map, same 50-char cap):
    `worktree_manager._sanitize_task_id` (the inbox_watcher dispatch path) and
    `agent_runner._worktree_safe_stem` (the agent_runner 'main'-agent path).
    Both produce `wt-<agent>-<safe_stem>` dirs that a live worker runs in. If
    any of the three diverge, `has_active_worker` builds a stem the dispatcher
    never wrote, the substring check misses, and a live worker goes undetected
    (the abandoned-task healer then double-dispatches). The one intentional
    difference: this returns '' (not 'task') for an empty stem, because
    `has_active_worker` guards on `if not safe` to avoid a spurious match — see
    the consistency-contract test
    `test_path_traversal_sanitizer.WorktreeSanitizerConsistencyTest`.

    NB: this is the AGGRESSIVE worktree sanitizer, NOT the printable-
    preserving inbox one (`safe_write_inbox.sanitize_component`); the two
    domains diverge on purpose — see `_sanitize_task_id`'s docstring."""
    return ''.join(c if (c.isalnum() or c in '-_') else '-' for c in task_stem)[:50]


def has_active_worker(task_id: str, active_cwds: set[str]) -> bool:
    """True if a live claude worker's worktree belongs to this task. A worktree
    basename is `wt-<agent>-<safe_stem>[-<timestamp>]` where safe_stem is the
    SANITIZED task stem — so we must sanitize the task_id the same way before
    the substring check (see _worktree_safe_stem). 50 chars is the same match
    window the dispatcher truncates to."""
    safe = _worktree_safe_stem(task_id)
    if not safe:
        return False
    # Match the sanitized stem as a whole, boundary-delimited token (audit #26).
    # A bare `safe in cwd` let a short/common stem (e.g. 'b') match an unrelated
    # worktree like 'wt-forge-rebuild-...' (where 'b' is only an infix of
    # 'rebuild') and falsely report a live worker, so the abandoned task was
    # never recovered. The worktree basename is a STRUCTURED name
    # (`wt-<agent>-<stem>-<ts>`), so the stem only appears boundary-delimited
    # when it is genuinely this task's worktree — no length floor is needed, and
    # min_len=1 keeps a legitimately-short stem matching its OWN live worktree
    # (a floor would drop it and double-dispatch live work).
    for cwd in active_cwds:
        if cwd.startswith("wt-") and id_matches(safe, cwd, min_len=1):
            return True
    return False


def _pid_alive(pid) -> bool:
    try:
        ipid = int(pid)
    except (TypeError, ValueError):
        return False
    # pid<=0 would signal a process GROUP / every process, not a specific pid —
    # a corrupted registry entry must not read as "alive forever".
    if ipid <= 0:
        return False
    try:
        os.kill(ipid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process EXISTS but is owned by another uid — it's alive; we just
        # can't signal it. Treating EPERM as dead would re-dispatch a live
        # cross-user worker (double dispatch).
        return True
    except OSError:
        return False
    return True


def has_in_flight_worker(task_id: str) -> bool:
    """True if agent_runner's in-flight registry has a LIVE worker for this task
    (state/in-flight/<task_id>.json present AND its pid signalable). This is the
    real per-task 'being processed' gate. The prior `has_lease(task_id)` checked
    state/dispatch-leases/<task_id>.lock — a file nothing writes — so it always
    returned False (dead), leaving the worker check as the only guard. A stale
    entry whose pid is dead does NOT block recovery (that IS the abandoned
    case)."""
    try:
        entry = json.loads((IN_FLIGHT_DIR / f"{task_id}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(entry, dict) and _pid_alive(entry.get("pid"))


def has_live_dispatch_lease(agent: str) -> bool:
    """True if inbox_watcher currently holds the per-agent dispatch lease
    `inbox:<agent>` with a live holder PID on the current boot, within TTL.

    This is the ONLY signal that covers the watcher's full dispatch window —
    including the ensure_worktree_for_task setup that runs AFTER lease
    acquisition but BEFORE the in-flight registry file or the claude process
    exists (where has_in_flight_worker and has_active_worker both return False).
    Without this gate the healer renames a task the watcher is mid-dispatch on
    and the LLM work is double-billed (audit H1).

    Gate granularity is per-AGENT, not per-task: the lease keys on `inbox:<agent>`
    and does not name the specific task being dispatched, so we conservatively
    defer recovery for the whole agent this tick when the watcher is active.
    That is safe — a genuinely abandoned task is recovered on a later tick once
    the watcher releases the lease (recovery is not time-critical; the task is
    already >60 min old). A dead/stale watcher leaves the lease past TTL, which
    reads as not-held so recovery proceeds normally.

    READ-ONLY by construction: it inspects the lease file directly and NEVER
    calls try_acquire — acquiring would trip dispatch_lease's kill-before-reclaim
    and SIGKILL the live watcher. In dispatch_lease 'off' mode no lease file is
    written, so this gate is inert and the in-flight/worker gates still apply.

    Failsafe: any error returns False so a reconciliation hiccup never blocks
    recovery of a genuinely abandoned task."""
    try:
        import dispatch_lease  # local import: optional dependency, failsafe below
        lease = dispatch_lease._read_lease(
            dispatch_lease._lease_path(f"inbox:{agent}"))
        if not lease:
            return False
        age = time.time() - lease.get("timestamp_renewed", 0)
        if age >= dispatch_lease.TTL_SECONDS:
            return False  # stale -> watcher gone; this is the abandoned case
        return dispatch_lease._pid_alive_same_boot(
            lease.get("holder_pid"), lease.get("boot_id", "unknown"))
    except Exception as exc:  # noqa: BLE001 -- never block recovery on error
        log("WARN", f"dispatch-lease check failed for {agent}: {exc}")
        return False


def _canonical_task_id(task_file: Path) -> str:
    """The task_id the dispatcher keyed the worktree + in-flight registry on:
    the task body's `task_id` field, falling back to the filename stem — exactly
    inbox_watcher's `task.get('task_id') or task_file.stem`. Using the filename
    stem alone misses the worktree/in-flight entry whenever the two differ."""
    try:
        body = json.loads(task_file.read_text())
        if isinstance(body, dict):
            tid = body.get("task_id")
            if isinstance(tid, str) and tid:
                return tid
    except (OSError, json.JSONDecodeError):
        pass
    return task_file.stem


def task_already_resolved(task_id: str, since_ts: datetime) -> Optional[str]:
    """If the task already shipped or was resolved out-of-band since
    `since_ts`, return the reason; else None.

    Guards the documented failure mode in reverse: a forge task can COMPLETE
    and merge its PR, then have its completion handler crash before moving the
    file out of the inbox. The lease is released and the worker is gone, so
    all three structural gates pass and the healer would re-dispatch
    already-shipped work. A merged PR matching the task (or a `larry_action` /
    later `session_start` chain_event) means the work is done -- skip recovery.

    Failsafe: any infra error returns None so a genuinely-abandoned task is
    still recovered (never block recovery on a reconciliation hiccup)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import task_resolution as tr
        resolved, reason = tr.resolved_out_of_band(
            task_id, since_ts, check_pr=True,
            log_fn=lambda m: log("INFO", m),
        )
        return reason if resolved else None
    except Exception as exc:  # noqa: BLE001 -- never block recovery on error
        log("WARN", f"resolution check failed for {task_id}: {exc}")
        return None


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
    # Defer the whole agent while inbox_watcher holds the dispatch lease — it is
    # mid-process_task and the in-flight/worker gates do not yet see the work
    # being set up (audit H1). Recovery resumes next tick once the lease frees.
    if has_live_dispatch_lease(agent):
        log("SKIP",
            f"{agent} action=skip-agent reason=dispatch-lease-live — "
            f"inbox_watcher actively dispatching; deferring recovery to next tick")
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
        # The worktree, in-flight registry, AND the PR branch are all keyed on
        # the BODY task_id (which may differ from the filename stem), so derive
        # the canonical id once and use it for every cross-check.
        task_id = _canonical_task_id(task_file)
        if has_in_flight_worker(task_id):
            continue  # live worker registered in-flight for this task
        if has_active_worker(task_id, active_cwds):
            continue  # worker grinding in the task's worktree
        since_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
        resolved_reason = task_already_resolved(task_id, since_ts)
        if resolved_reason:
            log(
                "SKIP",
                f"{agent}/{task_file.name} action=skip-already-resolved "
                f"reason={resolved_reason} — work shipped/resolved "
                f"out-of-band; not re-dispatching",
            )
            continue
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
