#!/usr/bin/env python3
"""heal_orphaned_mirror_claims.py — archive Mirror review-claim files that were
stranded in ``.claimed/<slot>/`` after the review CONCLUDED, so the slot
un-blocks and the next queued review gets a worker.

Incident (2026-07-10 — sentinel-in-flight-stall-translation-001 / PR #854)
--------------------------------------------------------------------------
A slot claims a task by ``os.rename``-ing it out of ``inboxes/mirror/`` into
``inboxes/mirror/.claimed/<slot>/`` BEFORE the claude review spawns
(inbox_watcher._claim_task). Every in-process terminal path in
``process_task`` then moves that claimed file to ``.archive`` (success) or
``.invalid`` (reject), and the loop's ``finally`` un-claims a transient defer.
So the file is archived on every path the watcher RETURNS through.

The gap is watcher DEATH: if ``inbox_watcher`` is SIGKILLed / restarted
(deploy, tier HOME-swap, crash) between the claim rename and the terminal
archive, the loop ``finally`` never runs, so the claimed file is stranded
under ``.claimed/<slot>/`` — where ``scan_inbox`` cannot see it, so it silently
head-of-line-blocks that slot. The only cleanup,
``inbox_watcher.sweep_claimed_orphans``, runs ONLY at watcher startup and only
for files older than a 4h ceiling; PR #854's re-dispatched review sat orphaned
for 7h+ because no restart happened and the file never crossed a startup sweep.

Worse, that startup sweep RE-QUEUES a not-in-flight orphan — which for a review
whose verdict was ALREADY delivered (a re-dispatch of a concluded review) would
trigger a paid Opus re-review of an already-reviewed PR.

What this healer does
---------------------
Every ~10 min (systemd timer), scan ``inboxes/mirror/.claimed/<slot>/
review-*.json``. Archive a claim as an orphan — moving it to
``inboxes/mirror/.archive/<stem>.orphan-cleared-<ts>.json`` — ONLY when it is
provably a concluded-and-stranded review, i.e. ALL of:

  1. age(mtime) >= ORPHAN_CLAIM_GRACE_SEC (default 45 min, comfortably above
     the 35-min agent_runner.REVIEW_SESSION_CEILING_SECONDS review wall) — so a
     just-claimed task whose in-flight entry / worktree process has not yet
     appeared is never yanked out from under a live dispatch;
  2. NO live claude process running in the task's worktree
     (``wt-mirror-<task_id>``) — the decisive "a review is executing right now"
     signal. This is LOAD-BEARING: a legitimate NEW review round can be running
     while a PRIOR round's verdict is already delivered, so "verdict delivered"
     alone must never win over a live process;
  3. NO live in-flight registry entry (``state/in-flight/<task_id>.json`` with a
     signalable pid) — the per-task "paid work in flight" gate;
  4. the review CONCLUDED — EITHER a Mirror verdict for the task_id was already
     delivered (durable: a consumed outbox archived under
     ``outboxes/mirror/.archive/<task_id>.json``; OR the same-named review
     request already sits in ``inboxes/mirror/.archive/`` — the re-dispatch-of-
     a-completed-review case; OR outbox-notifier.log recorded a
     ``marker-notified beacon <- mirror`` for it) OR the PR (``pr_url`` in the
     claim) is in a terminal state (MERGED / CLOSED) per ``gh pr view``.

Guards (1)–(3) prove the claim is NOT live; guard (4) proves it does not need a
(re-)review. A claim that is stranded but NOT concluded (no verdict, PR still
open, never ran) is deliberately LEFT for inbox_watcher.sweep_claimed_orphans to
re-queue — archiving it would silently drop a review that never happened.

Self-protection / discipline (matches the ZERO-LLM healer constellation)
-----------------------------------------------------------------------
Pure ``/proc`` reads, filesystem renames, one read-only ``gh`` call per
candidate, and a bounded log grep. No ``claude`` subprocess. Kill-switch aware
(``~/agents/healers.disabled``). Every gh / IO failure fails SAFE toward
SPARING the claim (never archives on uncertainty). Reversible: the archived
envelope is preserved verbatim (rename only) under a distinctive suffix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import mirror_review_conclusion  # noqa: E402  (shared conclusion predicate)
import worktree_manager  # noqa: E402  (lightweight: shutil/subprocess/pathlib only)

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT", str(HOME / "agents")))
KILL_SWITCH = AGENTS_ROOT / "healers.disabled"
LOG_FILE = AGENTS_ROOT / "logs" / "heal-orphaned-mirror-claims.log"
HEARTBEAT_FILE = AGENTS_ROOT / "blackboard" / "heal-orphaned-mirror-claims.heartbeat"
INBOXES_ROOT = AGENTS_ROOT / "inboxes"
OUTBOXES_ROOT = AGENTS_ROOT / "outboxes"
IN_FLIGHT_DIR = AGENTS_ROOT / "state" / "in-flight"
NOTIFIER_LOG = AGENTS_ROOT / "logs" / "outbox-notifier.log"

# Only Mirror uses the multi-consumer slot-claim primitive (spec §3.2 /
# review_slots). Kept as a tuple so a future slotted agent can be added without
# reshaping the scan.
TARGET_AGENTS = ("mirror",)
CLAIMED_SUBDIR = ".claimed"
ARCHIVE_SUBDIR = ".archive"

# Only review-request claims are in scope; the glob keeps any non-review claim
# out of this healer's reach.
CLAIM_GLOB = "review-*.json"

# Age floor before a claim is even a candidate. Above
# agent_runner.REVIEW_SESSION_CEILING_SECONDS (2100 = 35 min, the hard wall on a
# live review) so guards (2)/(3) are never the sole thing standing between a
# still-spawning dispatch and a wrongful archive. Env-overridable for incident
# response without a code change.
ORPHAN_CLAIM_GRACE_SEC = int(os.environ.get("OL_ORPHAN_CLAIM_GRACE_SEC", "2700"))

REPO = "Larry-Yatch/ourliberty-agent-core"
GH_TIMEOUT_SEC = 30


# ==================== logging / heartbeat ====================

def log(level: str, msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat() + "\n")
    except OSError:
        pass


# ==================== process / in-flight probes ====================

def _pid_alive(pid) -> bool:
    try:
        ipid = int(pid)
    except (TypeError, ValueError):
        return False
    if ipid <= 0:  # <=0 targets a process GROUP / all procs — never "alive"
        return False
    try:
        os.kill(ipid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another uid — alive; treating EPERM as dead would
        # let us archive a claim whose review is genuinely running.
        return True
    except OSError:
        return False
    return True


def get_active_claude_cwds() -> set[str]:
    """Full cwd paths of all live ``claude`` workers (the kernel's
    ``/proc/<pid>/cwd`` symlink target). A worktree torn down under a still-live
    process shows a trailing ``" (deleted)"`` which we normalize off so it still
    matches its worktree path."""
    cwds: set[str] = set()
    try:
        out = subprocess.run(
            ["pgrep", "-f", "claude"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return cwds
    if out.returncode != 0:
        return cwds
    for pid_str in out.stdout.split():
        if not pid_str.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{pid_str}/cwd")
        except OSError:
            continue
        if cwd.endswith(" (deleted)"):
            cwd = cwd[: -len(" (deleted)")]
        cwds.add(cwd)
    return cwds


def live_worktree_process(agent: str, task_id: str, active_cwds: set[str]) -> bool:
    """True iff a live claude worker is running in the task's Mirror worktree.

    The worktree path is DERIVED deterministically by
    ``worktree_manager.worktree_path_for`` (the exact namer the dispatch path
    uses), so we compare the resolved path rather than re-implementing the
    worktree-stem sanitizer (which would risk drift — see
    heal_abandoned_inbox_tasks._worktree_safe_stem's MUST-MATCH note)."""
    target = str(worktree_manager.worktree_path_for(agent, task_id))
    for cwd in active_cwds:
        if cwd == target or cwd.startswith(target + "/"):
            return True
    return False


def has_live_in_flight(task_id: str) -> bool:
    """True iff agent_runner's in-flight registry has a LIVE (signalable-pid)
    worker for this task. A stale entry whose pid is dead does NOT protect the
    claim (that IS the stranded case)."""
    try:
        entry = json.loads((IN_FLIGHT_DIR / f"{task_id}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(entry, dict) and _pid_alive(entry.get("pid"))


# ==================== dispatch-lease guard (claim→spawn window) ====================

def slot_dispatch_active(agent: str, slot: int) -> bool:
    """True iff the inbox_watcher slot that owns this ``.claimed/<slot>/`` dir is
    currently mid-dispatch — it holds its per-slot lease.

    This covers the claim→spawn window: a slot claims a task (``os.rename`` — which
    PRESERVES the envelope's mtime, so the age floor can't be trusted to detect a
    backlogged envelope claimed just now) and then runs minutes of worktree setup
    BEFORE the claude process + in-flight entry exist. During that window the
    live-process and in-flight guards both read false, so without this check a
    review that is actively spawning could be archived out from under a live
    dispatch. The per-agent version of this is
    heal_abandoned_inbox_tasks.has_live_dispatch_lease (audit H1); here we can be
    PRECISE — the slot is known from the ``.claimed/<slot>/`` dir, and the lease
    identity mirrors inbox_watcher._slot_identity (slot 0 keeps the legacy
    ``inbox:<agent>`` spelling; higher slots suffix ``:<n>``).

    READ-ONLY: dispatch_lease.is_held only inspects the lease file (never
    acquires/reclaims/kills). Lazy import + any error → False (fall back to the
    live-process / in-flight / age guards), so a missing dep never blocks
    cleanup — it only removes this extra cushion."""
    identity = f"inbox:{agent}" if slot == 0 else f"inbox:{agent}:{slot}"
    try:
        import dispatch_lease
        return dispatch_lease.is_held(identity)
    except Exception:  # noqa: BLE001 -- never block cleanup on a lease-probe error
        return False


# ==================== conclusion signals ====================

def verdict_delivered(agent: str, task_id: str, claim_name: str) -> bool:
    """True iff a Mirror verdict for this task was already delivered. Delegates to
    the shared mirror_review_conclusion predicate (single source of truth with
    inbox_watcher.sweep_claimed_orphans — see that module for the signals)."""
    return mirror_review_conclusion.verdict_delivered(
        outboxes_root=OUTBOXES_ROOT,
        inboxes_root=INBOXES_ROOT,
        notifier_log=NOTIFIER_LOG,
        agent=agent,
        task_id=task_id,
        claim_name=claim_name,
    )


def _claim_pr_url(claim: dict) -> Optional[str]:
    v = claim.get("pr_url")
    if isinstance(v, str) and v:
        return v
    ctx = claim.get("context")
    if isinstance(ctx, dict):
        v = ctx.get("pr_url")
        if isinstance(v, str) and v:
            return v
    return None


def pr_terminal(pr_url: str) -> bool:
    """True iff the PR at ``pr_url`` is MERGED or CLOSED per a read-only
    ``gh pr view``. Returns False on ANY gh failure or a still-OPEN PR — a
    transient gh error must never flip an open PR into a wrongful archive
    (fail-safe toward SPARING)."""
    if not pr_url:
        return False
    try:
        out = subprocess.run(
            ["gh", "pr", "view", pr_url, "--repo", REPO, "--json", "state"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log("WARN", f"gh pr view failed for {pr_url}: {type(e).__name__}: {e}; "
                    f"assuming not terminal")
        return False
    if out.returncode != 0:
        log("WARN", f"gh pr view {pr_url} returned {out.returncode}: "
                    f"{out.stderr.strip()[:200]}; assuming not terminal")
        return False
    try:
        state = str((json.loads(out.stdout or "{}") or {}).get("state", "")).upper()
    except (json.JSONDecodeError, TypeError):
        return False
    return state in ("MERGED", "CLOSED")


def claim_concluded(agent: str, claim_name: str, claim: dict) -> Optional[str]:
    """Return a short reason string iff the claim's review is provably concluded
    (verdict delivered OR PR terminal); else None. The reason is logged so a
    HEALED line is auditable."""
    task_id = claim.get("task_id") or Path(claim_name).stem
    if verdict_delivered(agent, task_id, claim_name):
        return "verdict-delivered"
    pr_url = _claim_pr_url(claim)
    if pr_url and pr_terminal(pr_url):
        return "pr-terminal"
    return None


# ==================== archive action ====================

def archive_orphan(agent: str, claim_file: Path, reason: str, age_min: int) -> bool:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = INBOXES_ROOT / agent / ARCHIVE_SUBDIR
    dest = dest_dir / f"{claim_file.stem}.orphan-cleared-{ts}.json"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.rename(claim_file, dest)
    except OSError as e:
        log("ERROR", f"{agent}/{claim_file.name} archive_failed: {e}")
        return False
    log("HEALED",
        f"{agent}/{claim_file.name} action=archive-orphan-claim reason={reason} "
        f"age_min={age_min} dest={dest.name} — slot unblocked")
    return True


# ==================== scan ====================

def scan_agent(agent: str, active_cwds: set[str], *, now: Optional[float] = None) -> tuple:
    """Return (scanned, cleared, spared)."""
    claimed_root = INBOXES_ROOT / agent / CLAIMED_SUBDIR
    if not claimed_root.is_dir():
        return 0, 0, 0
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    scanned = cleared = spared = 0
    for slot_dir in sorted(claimed_root.iterdir()):
        if not slot_dir.is_dir():
            continue
        try:
            slot = int(slot_dir.name)
        except ValueError:
            slot = None  # non-numeric slot dir: can't resolve a lease, guard off
        # Defer the WHOLE slot while its watcher thread is mid-dispatch (holds
        # the slot lease): the claim it is setting up has no live process /
        # in-flight entry yet, and its mtime may predate the claim. Cleanup
        # resumes next tick once the lease frees.
        if slot is not None and slot_dispatch_active(agent, slot):
            slot_files = list(slot_dir.glob(CLAIM_GLOB))
            scanned += len(slot_files)
            spared += len(slot_files)
            continue
        for claim_file in sorted(slot_dir.glob(CLAIM_GLOB)):
            scanned += 1
            try:
                age = now - claim_file.stat().st_mtime
            except OSError:
                continue
            if age < ORPHAN_CLAIM_GRACE_SEC:
                spared += 1
                continue  # still within the just-claimed / live-review window
            try:
                claim = json.loads(claim_file.read_text())
            except (OSError, json.JSONDecodeError):
                # A claim we cannot parse is NOT safe to reason about; leave it
                # for the parse-tolerant startup sweep. Never archive blind.
                spared += 1
                continue
            if not isinstance(claim, dict):
                spared += 1
                continue
            task_id = claim.get("task_id") or claim_file.stem
            # Liveness guards FIRST (cheapest + decisive) — a live review must
            # win over any "already concluded" signal from a prior round.
            if live_worktree_process(agent, task_id, active_cwds):
                spared += 1
                continue
            if has_live_in_flight(task_id):
                spared += 1
                continue
            reason = claim_concluded(agent, claim_file.name, claim)
            if not reason:
                # Stranded but NOT concluded (never ran, PR still open): leave it
                # for sweep_claimed_orphans to RE-QUEUE — archiving would drop a
                # review that never happened.
                spared += 1
                continue
            age_min = int(age / 60)
            if archive_orphan(agent, claim_file, reason, age_min):
                cleared += 1
            else:
                spared += 1
    return scanned, cleared, spared


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    if not INBOXES_ROOT.is_dir():
        heartbeat()
        log("HEARTBEAT", "inboxes root missing, nothing to do")
        return 0
    active_cwds = get_active_claude_cwds()
    total_scanned = total_cleared = total_spared = 0
    for agent in TARGET_AGENTS:
        s, c, sp = scan_agent(agent, active_cwds)
        total_scanned += s
        total_cleared += c
        total_spared += sp
    heartbeat()
    log("HEARTBEAT",
        f"scanned={total_scanned} cleared={total_cleared} spared={total_spared} "
        f"active_workers={len(active_cwds)} agents={','.join(TARGET_AGENTS)} "
        f"grace_sec={ORPHAN_CLAIM_GRACE_SEC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
