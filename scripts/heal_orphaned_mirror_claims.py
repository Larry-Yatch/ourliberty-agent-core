#!/usr/bin/env python3
"""heal_orphaned_mirror_claims.py — clear Mirror review-claim files stranded in
``.claimed/<slot>/`` when a slot died mid-flight, so the slot un-blocks and the
review is not silently dropped. A concluded (or PR-terminal) claim is ARCHIVED;
a genuinely-not-concluded claim on an OPEN PR is RE-INJECTED into the live inbox
so it re-runs automatically.

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
review-*.json``. A claim is a candidate only when it is provably NOT live:

  1. age(mtime) >= ORPHAN_CLAIM_GRACE_SEC (default 45 min, comfortably above
     the 35-min agent_runner.REVIEW_SESSION_CEILING_SECONDS review wall) — so a
     just-claimed task whose in-flight entry / worktree process has not yet
     appeared is never yanked out from under a live dispatch. SKIPPED when the
     claim is LEASE-PROVEN orphaned (PR #971): the slot lease is acquired before
     the claim rename and released only after process_task returns, so a claim
     under a lease-free numeric slot is owned by no dispatch and the floor is
     guarding an impossible state. Gated on dispatch_leases_enabled() — in
     mode()=='off' is_held is vacuously False and would fast-path everything;
  2. NO live claude process running in the task's worktree
     (``wt-mirror-<task_id>``) — the decisive "a review is executing right now"
     signal. This is LOAD-BEARING: a legitimate NEW review round can be running
     while a PRIOR round's verdict is already delivered, so "verdict delivered"
     alone must never win over a live process;
  3. NO live in-flight registry entry (``state/in-flight/<task_id>.json`` with a
     signalable pid) — the per-task "paid work in flight" gate;
  4. its owning slot is not mid-dispatch (``slot_dispatch_active``) — the
     claim→spawn window.

For a candidate, the action is chosen by whether THIS review round concluded and
by the PR's live state:

  * THIS round's verdict was delivered (ROUND-AWARE — see below), OR the PR
    (``pr_url`` in the claim) is TERMINAL (MERGED / CLOSED) → ARCHIVE the claim
    as ``inboxes/mirror/.archive/<stem>.orphan-cleared-<ts>.json``. Both are
    safe to drop: a re-review would be a duplicate / would review a dead PR.
  * NOT concluded AND the PR is OPEN → RE-INJECT the claim into the live inbox
    (``inboxes/mirror/<claim_name>``) so the running inbox_watcher re-claims and
    re-runs it. The restarted watcher never scans ``.claimed/``, so leaving it
    there strands the review forever and archiving would DROP it — the
    gg-s4-silent-failure-gauge stall (PR #923, 2026-07-11).
  * gh could not resolve the PR state (UNKNOWN), or the claim has no ``pr_url``
    → SPARE it this tick and retry next tick. Never archive-drop a maybe-open
    review, never re-review a maybe-terminal PR, on gh uncertainty.

ROUND-AWARE conclusion (the gg-s4 fix)
--------------------------------------
"THIS round's verdict was delivered" is judged by
``mirror_review_conclusion.round_verdict_delivered`` — the round's OWN archived
review-request, keyed on the round-suffixed claim filename + the claim's
``head_sha`` — NOT the task_id-keyed ``verdict_delivered`` the startup sweep
uses. The verdict OUTBOX never records ``head_sha``, so a task_id-keyed signal
cannot tell a rev-N round's verdict from round-0's; a prior round's delivered
verdict would otherwise mask a not-concluded current round and the review would
be wrongly archived (exactly the #923 drop). An ambiguous / missing ``head_sha``
on an OPEN PR fails safe toward RE-INJECT (a duplicate review is cheap; a dropped
review stalls the sequence forever).

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


def dispatch_leases_enabled() -> bool:
    """True iff dispatch leases are actually being WRITTEN, so a free lease is
    evidence of anything.

    Gate for the lease-proven fast path below. ``dispatch_lease.is_held``
    returns False UNCONDITIONALLY in ``mode() == 'off'`` (no lease files are
    written at all), so an ungated fast path would read every freshly-claimed
    task as provably-orphaned and re-inject it out from under a live dispatch —
    a paid duplicate Opus review on EVERY claim. Default mode is 'shadow'
    (leases written), but the gate is not optional.

    Fails CLOSED (any import / probe error → False → keep the age floor):
    losing the fast path costs latency, losing the gate costs money."""
    try:
        import dispatch_lease
        return dispatch_lease.mode() != "off"
    except Exception:  # noqa: BLE001 -- unavailable lease module → no fast path
        return False


# ==================== conclusion signals ====================

def _claim_head_sha(claim: dict) -> Optional[str]:
    """The head_sha this claim's review round is pinned to (top-level, then under
    ``context``), or None. Mirrors mirror_review_conclusion.recorded_head_sha /
    inbox_watcher._task_head_sha so the round identity is read the same way
    everywhere."""
    v = claim.get("head_sha")
    if isinstance(v, str) and v:
        return v
    ctx = claim.get("context")
    if isinstance(ctx, dict):
        v = ctx.get("head_sha")
        if isinstance(v, str) and v:
            return v
    return None


def round_verdict_delivered(agent: str, claim_name: str,
                            head_sha: Optional[str]) -> bool:
    """True iff a verdict for THIS review round was already delivered. Delegates
    to the shared ROUND-AWARE predicate (head-pinned + round-suffixed filename),
    NOT the round-blind task_id ``verdict_delivered`` used by the startup sweep —
    a prior round's verdict must never mask a not-concluded current round
    (gg-s4-silent-failure-gauge, PR #923, 2026-07-11)."""
    return mirror_review_conclusion.round_verdict_delivered(
        inboxes_root=INBOXES_ROOT,
        agent=agent,
        claim_name=claim_name,
        head_sha=head_sha,
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


def pr_state(pr_url: str) -> Optional[str]:
    """The PR's state — ``'OPEN'`` / ``'MERGED'`` / ``'CLOSED'`` — per a
    read-only ``gh pr view``, or None (UNKNOWN) on a missing url or ANY gh
    failure. Tri-state so the caller can tell a confirmed-OPEN PR (safe to
    RE-INJECT) apart from a gh error (act on NEITHER this tick): a transient gh
    error must never flip an open PR into a wrongful archive, NOR a terminal PR
    into a wrongful re-review — both are avoided by returning None and sparing."""
    if not pr_url:
        return None
    try:
        out = subprocess.run(
            ["gh", "pr", "view", pr_url, "--repo", REPO, "--json", "state"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log("WARN", f"gh pr view failed for {pr_url}: {type(e).__name__}: {e}; "
                    f"state UNKNOWN")
        return None
    if out.returncode != 0:
        log("WARN", f"gh pr view {pr_url} returned {out.returncode}: "
                    f"{out.stderr.strip()[:200]}; state UNKNOWN")
        return None
    try:
        state = str((json.loads(out.stdout or "{}") or {}).get("state", "")).upper()
    except (json.JSONDecodeError, TypeError):
        return None
    return state or None


# ==================== archive action ====================

def _via(lease_proven: bool) -> str:
    """Log suffix naming WHY this claim was eligible. A lease-proven action can
    fire minutes after the claim (by design), so an operator reading
    ``age_min=2`` needs to see that as a fast path and not a clock bug."""
    return " via=lease-proven" if lease_proven else ""


def archive_orphan(agent: str, claim_file: Path, reason: str, age_min: int,
                   *, lease_proven: bool = False) -> bool:
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
        f"age_min={age_min}{_via(lease_proven)} dest={dest.name} — slot unblocked")
    return True


def reinject_orphan(agent: str, claim_file: Path, age_min: int,
                    *, ambiguous: bool = False, lease_proven: bool = False) -> bool:
    """Re-inject a stranded-but-NOT-concluded review claim on an OPEN PR back into
    the LIVE inbox so the running inbox_watcher re-claims and re-runs it.

    ``os.rename`` the claim to ``inboxes/<agent>/<claim_name>`` — its ORIGINAL
    round-suffixed name. This is the missing action that the original healer
    lacked: the restarted inbox_watcher only scans the LIVE inbox, never
    ``.claimed/<slot>/``, so archiving would DROP the review and leaving it in
    place would strand the owning sequence step in 'reviewing' forever. The
    review's own round did not conclude (round-aware) and its PR is still open,
    so a fresh review is exactly what is owed.

    Idempotent: the healer scans only ``.claimed/``, so once re-injected the
    claim is invisible on the next tick; and inbox_watcher's claim rename dedups
    a double if one somehow races in."""
    dest = INBOXES_ROOT / agent / claim_file.name
    try:
        (INBOXES_ROOT / agent).mkdir(parents=True, exist_ok=True)
        os.rename(claim_file, dest)
    except OSError as e:
        log("ERROR", f"{agent}/{claim_file.name} reinject_failed: {e}")
        return False
    note = " head_sha=ambiguous(fail-safe)" if ambiguous else ""
    log("HEALED",
        f"{agent}/{claim_file.name} action=reinject-orphan-claim "
        f"reason=not-concluded-open-pr age_min={age_min}{_via(lease_proven)} "
        f"dest={dest.name}{note} — re-queued to live inbox")
    return True


# ==================== scan ====================

def scan_agent(agent: str, active_cwds: set[str], *, now: Optional[float] = None) -> tuple:
    """Return (scanned, cleared, reinjected, spared)."""
    claimed_root = INBOXES_ROOT / agent / CLAIMED_SUBDIR
    if not claimed_root.is_dir():
        return 0, 0, 0, 0
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    scanned = cleared = reinjected = spared = 0
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
        # LEASE-PROVEN ORPHAN (the #971 fast path). Reaching here means the slot
        # lease is NOT held — the branch above continues while it is. inbox_watcher
        # acquires that lease BEFORE _claim_task and releases it only after
        # process_task returns (inbox_watcher.py:1606/1622/1667), so a claim under
        # a lease-free slot is owned by NO dispatch: not spawning, not running.
        # The age floor exists solely to cover the claim→spawn window where the
        # live-process / in-flight guards read false; when the lease itself proves
        # there is no dispatch, that floor is guarding a state that cannot exist
        # and only delays recovery (PR #971 sat ~40 min this way).
        # Requires a resolvable slot (non-numeric dir → no lease identity → no
        # proof) AND leases actually written (see dispatch_leases_enabled).
        lease_proven_orphan = slot is not None and dispatch_leases_enabled()
        for claim_file in sorted(slot_dir.glob(CLAIM_GLOB)):
            scanned += 1
            try:
                age = now - claim_file.stat().st_mtime
            except OSError:
                continue
            # Is THIS claim relying on the fast path (i.e. under the age floor)?
            # Tracked separately from lease_proven_orphan so the re-probe and the
            # log label apply only where the floor was actually skipped.
            fast_pathed = age < ORPHAN_CLAIM_GRACE_SEC
            if fast_pathed and not lease_proven_orphan:
                spared += 1
                continue  # still within the just-claimed / live-review window
            if fast_pathed and slot_dispatch_active(agent, slot):
                # TOCTOU: the per-slot lease probe above ran BEFORE this glob, so
                # a watcher that acquired the lease and claimed a task in between
                # would look lease-free here. Previously the age floor absorbed
                # that window; the fast path removes it, so re-probe (read-only,
                # ~one stat) immediately before acting. Only for fast-pathed
                # claims — an aged claim never depended on this proof.
                spared += 1
                continue
            try:
                claim = json.loads(claim_file.read_text())
            except (OSError, json.JSONDecodeError):
                # A claim we cannot parse is NOT safe to reason about; leave it
                # for the parse-tolerant startup sweep. Never act blind.
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
            age_min = int(age / 60)
            head_sha = _claim_head_sha(claim)
            # THIS round's verdict already delivered (round-aware) → safe to drop.
            if round_verdict_delivered(agent, claim_file.name, head_sha):
                if archive_orphan(agent, claim_file, "verdict-delivered", age_min,
                                  lease_proven=fast_pathed):
                    cleared += 1
                else:
                    spared += 1
                continue
            # Not concluded: decide on the PR's live state (one gh call).
            state = pr_state(_claim_pr_url(claim))
            if state in ("MERGED", "CLOSED"):
                # PR terminal → the review is moot; archive-drop (no re-review).
                if archive_orphan(agent, claim_file, "pr-terminal", age_min,
                                  lease_proven=fast_pathed):
                    cleared += 1
                else:
                    spared += 1
                continue
            if state == "OPEN":
                # Stranded, NOT concluded, PR still open: RE-INJECT so the review
                # re-runs automatically. A missing/ambiguous head_sha on an open
                # PR still re-injects (fail-safe: a duplicate review is cheap; a
                # dropped review stalls the sequence forever) with a logged note.
                if reinject_orphan(agent, claim_file, age_min,
                                   ambiguous=head_sha is None,
                                   lease_proven=fast_pathed):
                    reinjected += 1
                else:
                    spared += 1
                continue
            # state is UNKNOWN (gh error / no pr_url): act on NEITHER path this
            # tick — never archive-drop a maybe-open review, never re-review a
            # maybe-terminal PR. Retry next tick once gh recovers.
            spared += 1
    return scanned, cleared, reinjected, spared


def main() -> int:
    if KILL_SWITCH.exists():
        log("KILLED_BY_SWITCH", "healers.disabled flag present, exiting")
        return 0
    if not INBOXES_ROOT.is_dir():
        heartbeat()
        log("HEARTBEAT", "inboxes root missing, nothing to do")
        return 0
    active_cwds = get_active_claude_cwds()
    total_scanned = total_cleared = total_reinjected = total_spared = 0
    for agent in TARGET_AGENTS:
        s, c, r, sp = scan_agent(agent, active_cwds)
        total_scanned += s
        total_cleared += c
        total_reinjected += r
        total_spared += sp
    heartbeat()
    log("HEARTBEAT",
        f"scanned={total_scanned} cleared={total_cleared} "
        f"reinjected={total_reinjected} spared={total_spared} "
        f"active_workers={len(active_cwds)} agents={','.join(TARGET_AGENTS)} "
        f"grace_sec={ORPHAN_CLAIM_GRACE_SEC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
