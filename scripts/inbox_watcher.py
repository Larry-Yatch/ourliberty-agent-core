#!/usr/bin/env python3
"""inbox_watcher.py — shared multi-agent inbox watcher daemon.

Phase D2 introduced this watcher; Phase D2.5 migrated the actual claude
spawning to call agent_runner.run_claude (upstream's hardened path with
retry/backoff, identity-assertion preamble, /tmp landmine scrub, parent
CLAUDE.md poison quarantine, in-flight registry, cancel-marker polling,
start_new_session=True). The thread-per-agent structure (max one task
per agent in flight, agents parallel across themselves) is preserved
because it's a clean fit for our 4-agent topology — upstream uses a
ThreadPoolExecutor over a flat queue.

Polling cadence: 5s. One thread per agent in AGENTS. Per-agent lease
"inbox:<agent>" via dispatch_lease provides cross-process safety + TTL +
boot-id PID-reuse guard. EMERGENCY_HALT flag in blackboard/ stops all
agent loops on next poll. Orphan-adoption on startup: any in-flight
registry entries from a prior boot get a forfeit outbox written, no
re-dispatch (output is forfeit; re-dispatch would risk double-billing
if the original claude actually completed).

stdlib only. Reuses dispatch_lease, dispatch_validator, agent_runner.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_type_inference import infer_task_type

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import active_tier  # noqa: E402
import agent_runner  # noqa: E402
import dispatch_lease  # noqa: E402
import dispatch_validator  # noqa: E402
import fixture_patterns  # noqa: E402
from inbox_dispatch_order import order_pending, read_fast_tracked_at  # noqa: E402
import larry_alerts  # noqa: E402
import mirror_review_conclusion  # noqa: E402
import mirror_review_handler  # noqa: E402
import routing_validator  # noqa: E402
import safe_write_inbox  # noqa: E402
import task_terminal_state  # noqa: E402
import worktree_manager  # noqa: E402

HOME = Path.home()
# Honor the OURLIBERTY_AGENTS_ROOT sandbox redirect (set by the test bootstrap /
# conftest) like every sibling module. Without this, the import-time log/state
# paths resolve under the real ~/agents and a test that triggers a log() write
# escapes the sandbox into the live tree.
AGENTS_ROOT = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT", str(HOME / "agents")))
INBOXES_ROOT = AGENTS_ROOT / "inboxes"
OUTBOXES_ROOT = AGENTS_ROOT / "outboxes"
BLACKBOARD = AGENTS_ROOT / "blackboard"
LOG_FILE = AGENTS_ROOT / "logs" / "inbox_watcher.log"
COSTS_FILE = BLACKBOARD / "costs.jsonl"
# Local durable ledger of Mirror review queue-wait samples (spec §4 PR3). Each
# review-start appends {ts, task_id, pr_url, review_slot, queue_wait_sec}; the
# sibling gauge (mirror_queue_wait_gauge.py) reads THIS file (not Supabase) to
# decide whether burst queue-wait warrants a third slot. The chain_events row is
# the dashboard/analytics copy; this jsonl is the self-firing signal source.
MIRROR_QUEUE_WAIT_LEDGER = BLACKBOARD / "mirror-queue-wait.jsonl"
EMERGENCY_HALT_FILE = BLACKBOARD / "EMERGENCY_HALT"
IN_FLIGHT_DIR = AGENTS_ROOT / "state" / "in-flight"
AGENT_CORE = HOME / "agent-core"
MODELS_FILE = AGENT_CORE / "config" / "agent-models.json"
AGENTS_DIR = AGENT_CORE / "agents"

AGENTS = ["beacon", "forge", "mirror", "pulse"]
POLL_INTERVAL_SEC = 5
DEFAULT_TIMEOUT_SEC = 14400  # 4h; matches HANDSHAKE-SCHEMA default
FALLBACK_MODEL = "claude-sonnet-4-6"
REQUEUE_MAX = 3
# Subdir under each agent inbox where a review slot atomically parks the task
# it claimed (spec §3.2). Dotfile so scan_inbox skips it (claimed != queued).
CLAIMED_SUBDIR = ".claimed"
# A task still parked in .claimed/<slot>/ older than one full session ceiling
# belongs to a slot whose process died mid-flight; sweep_claimed_orphans
# re-queues it on the next boot (spec §3.2). A live slot completes within the
# ceiling (run_claude's own timeout), so this never races a task in flight.
CLAIM_ORPHAN_CEILING_SEC = DEFAULT_TIMEOUT_SEC

# Phase D3 commit 4b: logical target_repo name → canonical filesystem path.
# Worktrees are spawned via `git worktree add` from the canonical. The agent's
# `allowed_repos` (in agent-models.json) gates which logical names may be
# targeted; _load_repo_paths() resolves them to disk paths at dispatch time.
# Source of truth is the top-level "repo_paths" block in agent-models.json
# (task-30 — folded out of the prior hardcoded mapping). Script-relative
# config path (matches outbox_notifier.py) so worktree smoke tests pick up
# the worktree's own config, while production daemons (installed at
# /home/larry/agent-core/scripts/) still hit the canonical config file.
_MODELS_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "agent-models.json"
_REPO_PATHS_CACHE: dict[str, Path] | None = None


def _load_repo_paths() -> dict[str, Path]:
    """Return logical target_repo → canonical filesystem Path.

    Reads the top-level ``repo_paths`` block in ``config/agent-models.json``
    once and caches the result. Raises ``RuntimeError`` (fail-loud) when the
    block is missing or any value is non-absolute or escapes ``/home/larry/``
    (the systemd sandbox ReadWritePaths constraint — paths outside this tree
    would fail later at worktree creation anyway).
    """
    global _REPO_PATHS_CACHE
    if _REPO_PATHS_CACHE is not None:
        return _REPO_PATHS_CACHE
    try:
        cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"could not read {_MODELS_CONFIG_PATH}: {e} — cannot resolve "
            "target_repo → filesystem path"
        ) from e
    block = cfg.get("repo_paths") if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or not block:
        raise RuntimeError(
            "config/agent-models.json missing required 'repo_paths' block — "
            "cannot resolve target_repo → filesystem path"
        )
    resolved: dict[str, Path] = {}
    for name, raw in block.items():
        if not isinstance(raw, str) or not raw.startswith("/home/larry/"):
            raise RuntimeError(
                f"config/agent-models.json repo_paths[{name!r}]={raw!r} must "
                "be an absolute path under /home/larry/ (matches systemd "
                "ReadWritePaths)"
            )
        resolved[name] = Path(raw)
    _REPO_PATHS_CACHE = resolved
    return resolved

_shutdown = threading.Event()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    line = f"[{now_iso()}] inbox_watcher: {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_models() -> dict:
    try:
        return json.loads(MODELS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f"could not load {MODELS_FILE}: {e}; using fallback model {FALLBACK_MODEL}")
        return {}


def resolve_model(agent: str, task: dict, models_config: dict) -> str:
    """Pick the model for this task. Order: explicit task override → per-agent
    inbox_model in config → default inbox_model → FALLBACK_MODEL."""
    if task.get("model"):
        return task["model"]
    agents_block = models_config.get("agents", {})
    per_agent = agents_block.get(agent, {}).get("inbox_model")
    if per_agent:
        return per_agent
    default = models_config.get("default", {}).get("inbox_model")
    return default or FALLBACK_MODEL


def ensure_dirs() -> None:
    for a in AGENTS:
        (INBOXES_ROOT / a).mkdir(parents=True, exist_ok=True)
        (INBOXES_ROOT / a / ".archive").mkdir(parents=True, exist_ok=True)
        # Pre-create the lost-result subdir so archiving a run whose outbox
        # could not be persisted is a PURE RENAME into an existing directory
        # (see _archive_dir). Creating it lazily at failure time would require
        # a fresh inode/dir-block mkdir under the exact disk-full condition
        # that triggers the lost-result path — a mkdir that can itself fail
        # and leave the task in the live inbox for a paid re-run.
        (INBOXES_ROOT / a / ".archive" / safe_write_inbox.LOST_RESULT_SUBDIR).mkdir(
            parents=True, exist_ok=True
        )
        (INBOXES_ROOT / a / ".invalid").mkdir(parents=True, exist_ok=True)
        (OUTBOXES_ROOT / a).mkdir(parents=True, exist_ok=True)
    BLACKBOARD.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    IN_FLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    # concurrency_guard writes its state to ~/agents/config/.concurrency-guard.json
    # — pre-create the directory so the first run_claude call doesn't fail with
    # FileNotFoundError before reaching the scrub/spawn path.
    (AGENTS_ROOT / "config").mkdir(parents=True, exist_ok=True)


def scan_inbox(agent: str) -> list[Path]:
    # Dispatch order: fast-tracked tasks first (newest "Build next" click wins,
    # LIFO), then oldest-mtime-first FIFO. The dashboard's queued-lane reader
    # applies the SAME rule via inbox_dispatch_order so the Forge Queue panel
    # shows exactly what will build next. See inbox_dispatch_order for the
    # contract and the bad-file fallback.
    inbox = INBOXES_ROOT / agent
    if not inbox.exists():
        return []
    entries = []
    for e in os.scandir(inbox):
        if not e.is_file() or e.name.startswith(".") or not e.name.endswith(".json"):
            continue
        p = Path(e.path)
        entries.append((e.stat().st_mtime, read_fast_tracked_at(p), p))
    return [payload for _, _, payload in order_pending(entries)]


def _unique_dest(dest_dir: Path, name: str) -> Path:
    base = dest_dir / name
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    for i in range(1, 1000):
        alt = dest_dir / f"{stem}.{i}{suffix}"
        if not alt.exists():
            return alt
    return dest_dir / f"{stem}.{int(time.time())}{suffix}"


def move_to(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir, path.name)
    path.rename(dest)
    return dest


def _slot_identity(agent: str, slot: int) -> str:
    """Dispatch-lease identity for a review slot (spec §3.1).

    Slot 0 keeps the legacy spelling ``inbox:<agent>`` so every healer/tool that
    greps for it (e.g. ``inbox:mirror``) keeps working during rollout; higher
    slots get an ``:<n>`` suffix. dispatch_lease keys everything off this string,
    so one holder per slot with TTL/heartbeat/PID-guard semantics unchanged.
    """
    return f"inbox:{agent}" if slot == 0 else f"inbox:{agent}:{slot}"


def _claimed_dir(agent: str, slot: int) -> Path:
    return INBOXES_ROOT / agent / CLAIMED_SUBDIR / str(slot)


def _agent_for_task_file(task_file: Path) -> str:
    """Resolve the owning agent from a task-file path, tolerating the
    ``.claimed/<slot>/`` relocation (spec §3.2). An unclaimed task lives at
    ``inboxes/<agent>/<name>.json`` (agent = parent); a claimed task lives at
    ``inboxes/<agent>/.claimed/<slot>/<name>.json`` (agent is three levels up).
    Used by write_invalid so a claimed task still lands in the RIGHT agent's
    ``.invalid`` dir instead of one named after the slot number."""
    parent = task_file.parent
    if parent.parent.name == CLAIMED_SUBDIR:
        return parent.parent.parent.name
    return parent.name


def _claim_task(agent: str, task_file: Path, slot: int) -> Path | None:
    """Atomically claim ``task_file`` for this slot via ``os.rename`` into
    ``.claimed/<slot>/`` (spec §3.2, the new multi-consumer primitive).

    Same-filesystem rename is atomic, so exactly one of two slots scanning the
    same inbox wins; the loser's source path is already gone and ``os.rename``
    raises FileNotFoundError → return None so the caller skips to the next task.
    No lock needed. Returns the claimed path on success."""
    dest_dir = _claimed_dir(agent, slot)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / task_file.name
        os.rename(task_file, dest)
        return dest
    except OSError:
        # FileNotFoundError => another slot claimed it first (expected race);
        # any other OSError => this slot does not own the task either. Skip it.
        return None


def _unclaim_task(agent: str, claimed_file: Path) -> None:
    """Return a claimed task to the live inbox (spec §3.2 deferral safety).

    process_task's transient-defer paths — rotation gate, TIER_HOLD, and the
    worktree-setup bump_requeue rewrite — leave the task file in place on the
    contract that the next 5s poll re-sees it ("delay one poll, never drop").
    But a claimed file lives under ``.claimed/<slot>/`` where scan_inbox can't
    look, and the orphan sweep only runs at startup — so without un-claiming, a
    deferred claim is stranded until the watcher restarts. The caller detects a
    deferral as "the claimed file still exists after process_task returned"
    (every terminal path archives/invalidates it, so it is already gone)."""
    try:
        move_to(claimed_file, INBOXES_ROOT / agent)
    except OSError as e:
        log(f"[{agent}] un-claim of {claimed_file} failed: {e}")


def _task_head_sha(task_file: Path) -> str | None:
    """The PR head SHA a task targets, or None (spec §4 PR2 same-head guard).

    Mirrors outbox_notifier._recorded_review_head_sha: top-level ``head_sha``
    first, then nested under ``context`` (chain-envelope shape). Only Mirror
    review-requests carry it; Forge builds and pre-head-recording envelopes
    return None (→ no same-head guard applies, which is correct — the guard is
    only about two review slots landing on one diff). Unreadable/malformed
    fails safe to None (no guard) rather than raising in the claim path."""
    try:
        data = json.loads(task_file.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    v = data.get("head_sha")
    if isinstance(v, str) and v:
        return v
    ctx = data.get("context")
    if isinstance(ctx, dict):
        v = ctx.get("head_sha")
        if isinstance(v, str) and v:
            return v
    return None


def _head_lease_identity(agent: str, head_sha: str) -> str:
    """Dispatch-lease identity that serializes review of ONE PR head across all
    slots (spec §4 PR2). Distinct namespace from the per-slot inbox leases
    (``inbox:<agent>[:<n>]``) so it never collides with slot arbitration.

    The atomic file-claim already stops two slots grabbing the SAME task file;
    this closes the residual gap where two DISTINCT review-requests for one head
    (a re-dispatch racing the original, a reconcile sweep, a lost-result
    re-queue) get claimed by two slots and reviewed CONCURRENTLY — 2× Opus burn
    on an identical diff plus racing verdicts. dispatch_lease gives a race-free
    single holder with TTL + PID-guard, so exactly one slot reviews a given head
    at a time regardless of claim interleaving; the loser defers and runs the
    (benign, pre-existing) serial re-review after the winner frees the head —
    never a concurrent double-review."""
    return f"review-head:{agent}:{head_sha}"


def _archive_dir(agent: str, *, lost_result: bool = False) -> Path:
    """The archive destination for a processed task envelope.

    `lost_result=True` selects `.archive/.lost-result/` — the POSITIVE marker
    for a run whose outbox could not be persisted, so its result is LOST and
    no downstream consumer (verdict routing, marker-error cascade) ever saw
    it. Pure renames, so the marker lands even when data writes are failing
    (disk-full). Consumer: outbox_notifier's review-request dedup re-dispatches
    a Mirror review whose same-head envelope carries this marker (debounced +
    capped), while a plain `.archive/` envelope dedups forever."""
    d = INBOXES_ROOT / agent / ".archive"
    if lost_result:
        d = d / safe_write_inbox.LOST_RESULT_SUBDIR
    return d


def write_invalid(task_file: Path, reason: str) -> None:
    agent = _agent_for_task_file(task_file)
    dest = move_to(task_file, INBOXES_ROOT / agent / ".invalid")
    try:
        dest.with_suffix(dest.suffix + ".reason").write_text(f"{now_iso()}\n{reason}\n")
    except OSError as e:
        log(f"write_invalid sidecar failed for {dest}: {e}")


def append_cost(record: dict) -> None:
    try:
        BLACKBOARD.mkdir(parents=True, exist_ok=True)
        with open(COSTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"append_cost failed: {e}")


def bump_requeue(task_file: Path, task: dict) -> None:
    rc = (task.get("requeue_count") or 0) + 1
    if rc >= REQUEUE_MAX:
        log(f"requeue cap hit for {task_file.name}; moving to .invalid")
        write_invalid(task_file, f"requeue_count >= {REQUEUE_MAX}")
        return
    task["requeue_count"] = rc
    try:
        task_file.write_text(json.dumps(task, indent=2))
    except OSError as e:
        log(f"requeue write failed for {task_file}: {e}")


def reap_orphans_on_startup() -> int:
    """For every in-flight registry entry from a prior boot, write a forfeit
    outbox so downstream consumers know the task did not produce a result,
    then remove the registry entry.

    Adopt-if-alive, mark-failed-if-dead policy (per Phase D2.5 Call C):
    - If the PID is alive, the detached claude subprocess is still running
      but we've lost its stdout pipe. Output is forfeit either way.
    - If the PID is dead, we just clean up the registry.
    NEVER re-dispatch: paid work that may have completed should not be
    automatically duplicated. Operator re-dispatches manually if needed.
    """
    if not IN_FLIGHT_DIR.exists():
        return 0
    reaped = 0
    for f in IN_FLIGHT_DIR.glob("*.json"):
        try:
            entry = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f"reap_orphans: bad registry file {f.name}: {e}; deleting")
            try:
                f.unlink()
            except OSError:
                pass
            continue

        task_stem = entry.get("task_stem") or f.stem
        agent = entry.get("agent_id") or "unknown"
        pid = entry.get("pid")

        alive = False
        if pid:
            try:
                os.kill(int(pid), 0)
                alive = True
            except (OSError, ProcessLookupError, ValueError, TypeError):
                alive = False

        outbox_dir = OUTBOXES_ROOT / safe_write_inbox.sanitize_component(agent)
        try:
            outbox_dir.mkdir(parents=True, exist_ok=True)
            forfeit = _unique_dest(
                outbox_dir,
                safe_write_inbox.sanitize_component(f"{task_stem}.forfeit.json"),
            )
            forfeit.write_text(json.dumps({
                "task_id": task_stem,
                "agent": agent,
                "started_at": entry.get("started_at"),
                "completed_at": now_iso(),
                "exit_code": -3,
                "error": (
                    f"in-flight registry orphan; output forfeit during watcher "
                    f"restart (pid={pid}, alive_at_reap={alive}). "
                    f"Operator may manually re-dispatch with a fresh task_id."
                ),
                "result": "",
                "model": entry.get("model"),
            }, indent=2))
        except OSError as e:
            log(f"reap_orphans: forfeit write failed for {task_stem}: {e}")

        try:
            f.unlink()
        except OSError:
            pass
        reaped += 1
        log(f"reap_orphans: marked {agent}/{task_stem} as forfeit (alive_at_reap={alive})")

    if reaped:
        log(f"reap_orphans: total {reaped} orphan(s) marked as forfeit")
    return reaped


def _review_already_concluded(agent: str, task_id: str, claim_name: str) -> bool:
    """True iff a Mirror review for this claim already ran to a delivered verdict.

    Delegates to the shared mirror_review_conclusion predicate (single source of
    truth with scripts/heal_orphaned_mirror_claims.py, so the startup-sweep
    backstop and the timer healer can never disagree). Used by
    sweep_claimed_orphans to ARCHIVE such an orphan instead of RE-QUEUING it:
    re-queuing a concluded review sends it back to a slot for a PAID Opus
    re-review of an already-reviewed PR (the 2026-07-10 PR #854 class).

    Read-only; any error → False (fall through to the existing re-queue path, so
    a hiccup here can only ever RE-QUEUE — never wrongly archive). The
    notifier-log root is derived from INBOXES_ROOT.parent so a test that patches
    INBOXES_ROOT never reads the real production log."""
    try:
        return mirror_review_conclusion.verdict_delivered(
            outboxes_root=OUTBOXES_ROOT,
            inboxes_root=INBOXES_ROOT,
            notifier_log=INBOXES_ROOT.parent / "logs" / "outbox-notifier.log",
            agent=agent,
            task_id=task_id,
            claim_name=claim_name,
        )
    except Exception as e:  # noqa: BLE001 -- never block the sweep on a probe error
        log(f"[{agent}] concluded-check error for {task_id}: {e!r}")
        return False


def sweep_claimed_orphans(ceiling_sec: int = CLAIM_ORPHAN_CEILING_SEC) -> int:
    """Re-queue tasks stranded in ``.claimed/<slot>/`` by a slot that died
    mid-flight (spec §3.2). Run at startup, BEFORE reap_orphans_on_startup, so
    the in-flight registry is still intact for the paid-work check below.

    A task is claimed (renamed out of the live inbox) BEFORE run_claude spawns.
    Two death windows:
      * died AFTER the LLM spawned → an in-flight registry entry exists for the
        task_id. Re-queuing would re-dispatch PAID work and double-bill, which
        this module forbids (see reap_orphans_on_startup). Archive it under the
        lost-result marker instead; reap_orphans forfeits the registry entry.
      * died BEFORE the LLM spawned → no registry entry, no spend. Re-queue it
        to the live inbox so it dispatches exactly once more.

    The ceiling guard skips tasks younger than one session ceiling so a live
    slot's in-progress claim is never yanked out from under it.
    """
    if not INBOXES_ROOT.exists():
        return 0
    now = time.time()
    requeued = 0
    for agent in AGENTS:
        claimed_root = INBOXES_ROOT / agent / CLAIMED_SUBDIR
        if not claimed_root.exists():
            continue
        for slot_dir in sorted(claimed_root.iterdir()):
            if not slot_dir.is_dir():
                continue
            for f in sorted(slot_dir.glob("*.json")):
                try:
                    age = now - f.stat().st_mtime
                except OSError:
                    continue
                if age < ceiling_sec:
                    continue  # may still be in flight within the session ceiling
                task_id = None
                try:
                    task_id = (json.loads(f.read_text()) or {}).get("task_id")
                except (OSError, json.JSONDecodeError, AttributeError):
                    pass
                task_id = task_id or f.stem
                # Concluded-check FIRST (before the in-flight paid-orphan branch):
                # a review that was stranded by a mid-flight watcher death often
                # ALSO leaves a stale in-flight entry from the prior boot (this
                # sweep runs before reap_orphans_on_startup clears it). If the
                # in-flight check ran first it would mark a CONCLUDED review as
                # `lost-result`, whose marker triggers a re-dispatch — a PAID
                # re-review of an already-reviewed PR (#854). The >ceiling age gate
                # above already guarantees no LIVE review is here (the review
                # session ceiling is ~35 min « the 4h ceiling), so archiving a
                # concluded claim even with a lingering in-flight entry is safe.
                if _review_already_concluded(agent, task_id, f.name):
                    try:
                        move_to(f, _archive_dir(agent))
                        log(f"[{agent}] claimed-orphan {f.name} already concluded "
                            f"(verdict delivered/archived); archived, NOT re-queued")
                    except OSError as e:
                        log(f"[{agent}] claimed-orphan concluded-archive failed "
                            f"for {f}: {e}")
                    continue
                if (IN_FLIGHT_DIR / f"{task_id}.json").exists():
                    # Paid run in flight/completed (but no delivered verdict) —
                    # never re-dispatch; archive under the lost-result marker.
                    try:
                        move_to(f, _archive_dir(agent, lost_result=True))
                        log(f"[{agent}] claimed-orphan {f.name} has a live in-flight "
                            f"entry (paid); archived lost-result, NOT re-queued")
                    except OSError as e:
                        log(f"[{agent}] claimed-orphan archive failed for {f}: {e}")
                    continue
                try:
                    dest = move_to(f, INBOXES_ROOT / agent)
                    requeued += 1
                    log(f"[{agent}] re-queued orphaned claim {f.name} -> {dest.name} "
                        f"(slot={slot_dir.name}, age={int(age)}s >= "
                        f"ceiling {ceiling_sec}s)")
                except OSError as e:
                    log(f"[{agent}] re-queue of orphaned claim {f} failed: {e}")
    if requeued:
        log(f"sweep_claimed_orphans: re-queued {requeued} orphaned claim(s)")
    return requeued


def _review_slots_for(agent: str, models_config: dict) -> int:
    """Number of concurrent watcher threads for ``agent`` (spec §3.3). Read from
    the per-agent ``review_slots`` key in agent-models.json; absent or malformed
    => 1 (inert, behavior identical to today). Only mirror sets it; the key is
    honored generically for every agent."""
    block = models_config.get("agents", {}).get(agent, {})
    try:
        n = int(block.get("review_slots", 1))
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def _pr_created_at(pr_url: str) -> datetime | None:
    """PR-open timestamp via ``gh pr view <url> --json createdAt`` (spec §4 PR3
    queue-wait anchor). Best-effort: any failure (gh missing, auth, network,
    unparseable) returns None so the caller emits no sample rather than blocking
    the review. Short timeout — this runs on the review's hot path."""
    if not pr_url:
        return None
    import subprocess  # local: keep module import surface unchanged
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "createdAt",
             "-q", ".createdAt"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        # gh emits RFC-3339 with a trailing Z; normalize to +00:00 for fromisoformat.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def emit_review_queue_wait(task_id: str, pr_url: str | None, slot: int) -> None:
    """Record the PR-open → review-start queue-wait for one Mirror review (spec
    §4 PR3). Two sinks, both best-effort and non-raising:

      1. Local jsonl ledger (``MIRROR_QUEUE_WAIT_LEDGER``) — the self-firing
         source the sibling gauge reads.
      2. A ``review_queue_wait`` chain_event — the dashboard/analytics copy.

    A missing/unresolvable PR-open time (gh failed, no pr_url) means no sample:
    we never fabricate a queue-wait, and the review proceeds regardless."""
    created = _pr_created_at(pr_url) if pr_url else None
    if created is None:
        return
    now = datetime.now(timezone.utc)
    queue_wait_sec = max(0.0, (now - created).total_seconds())
    sample = {
        "ts": now.isoformat(),
        "task_id": task_id,
        "pr_url": pr_url,
        "review_slot": slot,
        "queue_wait_sec": round(queue_wait_sec, 1),
    }
    try:
        BLACKBOARD.mkdir(parents=True, exist_ok=True)
        with open(MIRROR_QUEUE_WAIT_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample) + "\n")
    except OSError as e:
        log(f"[mirror] queue-wait ledger append failed for {task_id}: {e}")
    try:
        import chain_event_emit  # local: avoids Supabase client init at import
        chain_event_emit.emit_event(
            event_type="review_queue_wait",
            agent="mirror",
            task_id=task_id,
            payload={
                "review_slot": slot,
                "queue_wait_sec": round(queue_wait_sec, 1),
            },
            pr_url=pr_url,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(f"[mirror] queue-wait chain_event emit raised for {task_id}: "
            f"{type(e).__name__}: {e}")


def _record_outbox_cost(agent: str, task_id: str, outbox: dict) -> None:
    """Append the paid run's cost row to costs.jsonl (best-effort via
    append_cost). Called on BOTH the success path and the outbox-write-failure
    path (#13) so a paid run is never invisible to budget/quota accounting just
    because its outbox couldn't be persisted. No-op when no cost was recorded."""
    if outbox.get("cost_usd") is None:
        return
    usage = outbox.get("usage") or {}
    # task_type either rides through from the dispatch envelope (already in
    # outbox) or is inferred from task_id prefix so cost rows are not uniformly
    # "unknown". See task_type_inference for the discriminator.
    task_type = outbox.get("task_type") or infer_task_type(task_id)
    append_cost({
        "ts": outbox.get("completed_at") or now_iso(),
        "agent": agent,
        "task_id": task_id,
        "task_type": task_type,
        "model": outbox.get("model"),
        # Step C: per-account field for rolling-5h scoping. Falls back to
        # 'tier1' when meta did not populate `account_tier` (older outbox rows
        # from before this PR), preserving the documented V1 default in
        # config/agent-models.json:tier1_quota._note. Readers MUST tolerate an
        # absent field too (= unknown/tier1).
        "account": outbox.get("account_tier") or "tier1",
        "cost_usd": outbox.get("cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read": usage.get("cache_read"),
        "cache_creation": usage.get("cache_creation"),
        "duration_sec": outbox.get("duration_sec"),
        "attempts": outbox.get("attempts"),
        "source": "inbox-watcher",
        # Per-repo cost attribution (approach A): stamp the build's target repo
        # at write-time so cost_by_repo can bucket LLM spend by repo. Null-safe:
        # outbox lacks target_repo for non-build/older rows -> None (unattributed).
        "target_repo": outbox.get("target_repo"),
    })


def _build_outbox(agent: str, task_id: str, task: dict, task_file: Path,
                  success: bool, output_text: str, session_id: str | None,
                  meta: dict, error: str | None = None) -> dict:
    outbox = {
        "task_id": task_id,
        "agent": agent,
        "source_task_file": str(task_file),
        "dedup_identity": task.get("dedup_identity"),
        "reply_chat_id": task.get("reply_chat_id"),
        "source": task.get("source"),
        "started_at": meta.get("started_at") or now_iso(),
        "completed_at": meta.get("completed_at") or now_iso(),
        "duration_sec": meta.get("duration_sec"),
        "exit_code": 0 if success else -1,
        "model": meta.get("model"),
        "account_id": meta.get("account_id"),
        # Step C: tier1/tier2 from blackboard/active-tier.json, surfaced so
        # the costs.jsonl row downstream can carry an `account` field.
        # Distinct from `account_id` (OAuth pool identity).
        "account_tier": meta.get("account_tier"),
        "attempts": meta.get("attempts"),
        "result": output_text or "",
        "claude_session_id": session_id,
        "cost_usd": meta.get("cost_usd"),
        "usage": meta.get("usage"),
        # Harness-enforced review-ceiling signal (run_claude sets meta['timed_out']
        # when it kills a session at the wall clock). Surfaced on the outbox so
        # outbox_notifier can synthesize a clean REVIEW_ESCALATE for a timed-out
        # phase=review session instead of raising a marker-error.
        "timed_out": bool(meta.get("timed_out")),
        "timeout_seconds": meta.get("timeout_seconds"),
    }
    # Phase D3 commit 4a: propagate preflight envelope fields so the notifier
    # can apply marker-driven routing decisions (clarification budget, intent
    # selection) without re-reading the (already-archived) inbox task file.
    # `original_source` + `marker_error_count` survive across the marker-error
    # cascade so a recovered marker still routes back to the right dispatcher.
    # Phase D3 commit 4b: also propagate the build-phase metadata fields
    # (branch, pr_title, pr_body) that Beacon sets on her APPROVAL_REQUEST
    # dispatch. Without these in the outbox, _dispatch_build_phase in the
    # notifier reads None for branch and falls back to derive_branch_name —
    # any explicit branch from Beacon's spec gets silently overwritten.
    # D3.5 5b: also propagate forge_build_session_id (Forge's build session,
    # threaded through Mirror's review-request so the revision dispatch back
    # to Forge can --resume the right session) and revision_count (the
    # round counter, incremented each Forge→Mirror→Forge cycle).
    # D3.5 5b second-pass M-8 fix: also propagate previous_findings so
    # Mirror's re-review prompt can include her round-N-1 findings (her
    # session is fresh per round; she has no other reliable source for
    # the prior findings).
    # D3.5 5c: also propagate replan_count, max_replans, and intent (as
    # `inbound_intent`). replan_count + max_replans flow forward through
    # the dispatch chain so the next REVIEW_ESCALATE notify carries the
    # incremented budget. `inbound_intent` is the inbound task's `intent`
    # field surfaced on the outbox so outbox_notifier can recognize
    # "Beacon is responding to a review-escalate notify" without re-reading
    # the (already-archived) inbox task file. Symmetric with how
    # `original_source` and `marker_error_count` ride through the cascade.
    for envelope_field in ('clarification_count', 'max_clarifications',
                           'phase', 'target_repo', 'task_type',
                           'original_source', 'marker_error_count',
                           'branch', 'pr_title', 'pr_body',
                           'forge_build_session_id', 'revision_count',
                           'max_revisions', 'pr_url',
                           'previous_findings',
                           'replan_count', 'max_replans',
                           'mirror_escalate_reason',
                           # Delegate-tracking Slice 2a — propagate the origin
                           # envelope task_id (`delegate-<cid>`) through the
                           # Forge outbox so outbox_notifier can stamp it onto
                           # the build-lifecycle chain_events, joining the build
                           # back to the delegated card. Absent on non-delegated
                           # work (only set when it differs from the marker id).
                           'origin_task_id',
                           # PR-S4 rectification (H1+M4): propagate the
                           # inbound task `prompt` so outbox_notifier can
                           # discriminate Mirror DAG-preflight sessions
                           # (prompt starts with `review-sequence-dag`)
                           # from regular PR reviews. Without this, the
                           # M4 short-circuit on `_classify_mirror_marker`
                           # and the H1 DAG-result handler have no signal
                           # to gate on.
                           'prompt',
                           # task-25 (2026-05-20) — Forge's preflight session
                           # ID, threaded through Beacon's clarification
                           # round-trip so the clarification-response leg
                           # can --resume Forge in her original session +
                           # worktree (closes chain-routing gap #5: the
                           # `notify-notify-{task}` doubled-prefix branch
                           # bug). Symmetric with forge_build_session_id.
                           'forge_session_id'):
        if task.get(envelope_field) is not None:
            outbox[envelope_field] = task[envelope_field]
    # `inbound_intent` is special — sourced from task['intent'] (not
    # task['inbound_intent']) and renamed on the outbox to make the
    # ownership unambiguous: this is "the intent that fired the inbox
    # dispatch I just ran," not "an intent I as the agent decided." Skip
    # propagation when task has no intent (e.g., chat-mode tasks have none).
    # M-6 review fix: NEVER add `inbound_intent` to the envelope_fields
    # propagation list above — that would carry a stale prior-dispatch
    # intent through to a chat-mode follow-up, and outbox_notifier's
    # _BEACON_REPLAN_INBOUND_INTENTS check would mis-fire on it. The
    # value must always derive from THIS dispatch's inbound task, never
    # from a propagated prior value.
    if task.get('intent') is not None:
        outbox['inbound_intent'] = task['intent']
    if error:
        outbox["error"] = error
    return outbox


_GH_PR_URL_RE = re.compile(r'^https?://github\.com/([^/]+/[^/]+)/pull/(\d+)')


def _mirror_review_pr_terminal_state(task: dict):
    """For a Mirror `phase=review` task, return the PR's terminal state
    (`'MERGED'`/`'CLOSED'`) when the PR under review has already left OPEN,
    else `None`.

    A non-None return means the review is pure waste: the PR is decided, so a
    review gates nothing. The caller skips the (expensive) Opus session.

    `None` means "let the review proceed" and covers EVERY uncertain path —
    no `pr_url`, an unparseable url, an OPEN/UNKNOWN state, or any `gh` hiccup.
    This is the execution-time half of the merged/closed guard: it runs
    immediately before the review session launches, catching the race where the
    merge lands AFTER the review was dispatched (the dispatch-time pre-check in
    outbox_notifier only sees state at dispatch — observed case: review
    dispatched ~6s before an auto-merge, then ran ~12 min post-merge for
    $0.918). Fail-OPEN by construction so a `gh` blip never silently drops a
    legitimate review."""
    pr_url = task.get('pr_url')
    if not isinstance(pr_url, str) or not pr_url:
        return None
    m = _GH_PR_URL_RE.search(pr_url)
    if not m:
        return None
    repo_coords, pr_number = m.group(1), m.group(2)
    raw = task_terminal_state.gh_json(
        ['gh', 'pr', 'view', pr_number,
         '--repo', repo_coords, '--json', 'state'],
    )
    if not isinstance(raw, dict):
        return None
    state = task_terminal_state.classify_state(raw.get('state'))
    return state if state in task_terminal_state.TERMINAL_STATES else None


# mirror-marker-self-validate-gate-001: default cap on the in-process verdict-
# marker self-validation re-prompt loop. Overridable via config/agent-models.json
# loop_bounds.mirror_marker_self_validate_retries.
DEFAULT_MIRROR_MARKER_SELF_VALIDATE_RETRIES = 2


def _load_marker_self_validate_retries(models_config: dict) -> int:
    """Cap on in-process mirror verdict-marker self-validation re-prompts.

    Read from config/agent-models.json `loop_bounds.mirror_marker_self_validate_retries`.
    Falls back to DEFAULT_MIRROR_MARKER_SELF_VALIDATE_RETRIES for a missing key,
    a non-int, a bool (json `true`/`false`), or a negative value — same defensive
    shape as outbox_notifier's loop_bounds loaders.
    """
    loop_bounds = models_config.get("loop_bounds") if isinstance(models_config, dict) else None
    if not isinstance(loop_bounds, dict):
        return DEFAULT_MIRROR_MARKER_SELF_VALIDATE_RETRIES
    raw = loop_bounds.get("mirror_marker_self_validate_retries")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return DEFAULT_MIRROR_MARKER_SELF_VALIDATE_RETRIES
    return raw


def _mirror_marker_is_clean(output_text: str) -> tuple[bool, str | None]:
    """True iff `output_text` carries exactly one valid REVIEW_* verdict marker.

    Detection is mirror_review_handler.parse_mirror_marker — no new validation
    logic. A missing marker (the parser returns marker_type None) OR any parse
    failure (MalformedMirrorMarker for delimiter-without-JSON / bare-keyword /
    loose-delimiter, MultipleMirrorMarkers for two-or-more blocks) is treated as
    "not clean" and carries the parser's own diagnostic as err_msg.
    """
    try:
        marker_type, _payload, _narrative = mirror_review_handler.parse_mirror_marker(
            output_text
        )
    except (
        mirror_review_handler.MalformedMirrorMarker,
        mirror_review_handler.MultipleMirrorMarkers,
    ) as e:
        return False, str(e)
    if marker_type is None:
        return False, (
            "No canonical verdict marker block found in the review output. A "
            "review verdict requires one `=== REVIEW_PASS ===` (or REVIEW_REVISION "
            "/ REVIEW_ESCALATE / REVIEW_EMERGENCY_HALT) block with a JSON body and "
            "matching `=== END_XXX ===` delimiter."
        )
    return True, None


def _mirror_marker_self_validate(
    *,
    agent: str,
    task: dict,
    output_text: str,
    session_id: str | None,
    working_dir: str | None,
    model: str | None,
    timeout: int,
    task_id: str,
    meta: dict,
    models_config: dict,
) -> tuple[str, str | None]:
    """Bounded SAME-PROCESS verdict-marker self-validation for Mirror reviews.

    mirror-marker-self-validate-gate-001. Mirror's first phase=review within
    ~10-25 min of a mirror-bot restart can end with a malformed verdict marker
    (prose-no-delimiter, or a `=== REVIEW_PASS ===` delimiter with no JSON body).
    The existing outbox_notifier marker-error path corrects this, but each round
    costs a cross-process notify cycle. This gate is a fast loop in FRONT of that
    slow one: when a malformed/missing marker is detected here, re-invoke
    run_claude under --resume with a terse correction prompt (capped, config-
    driven), substituting the corrected output_text/session_id BEFORE the single
    existing outbox write. On exhaust the best-effort output flows through
    unchanged and the outbox_notifier net stays as the outer backstop.

    Mirrors the bounded kickback in beacon_telegram_bot.py:750-798. Only fires
    for agent=="mirror" + phase=="review" (the caller additionally gates on
    run_claude success). Adds no new outbox; only substitutes output_text and
    session_id.
    """
    if not (agent == "mirror" and task.get("phase") == "review"):
        return output_text, session_id

    clean, err_msg = _mirror_marker_is_clean(output_text)
    if clean:
        return output_text, session_id

    max_retries = _load_marker_self_validate_retries(models_config)
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        correction = (
            f"Your previous review output on task `{task_id}` could not be parsed "
            f"as a valid verdict marker (in-process self-validation retry {attempt} "
            f"of {max_retries}). Error: {err_msg} Re-read your CLAUDE.md marker-"
            f"discipline section and re-emit EXACTLY one valid REVIEW_* marker "
            f"block: `=== REVIEW_PASS ===` (or REVIEW_REVISION / REVIEW_ESCALATE / "
            f"REVIEW_EMERGENCY_HALT) on its own line, a single JSON object with the "
            f"required fields, then the matching `=== END_XXX ===` delimiter. Put "
            f"your narrative ABOVE the block — JSON only INSIDE it. Prefer marker.py "
            f"to hand-typing the delimiters."
        )
        log(f"[{agent}] mirror-marker self-validate retry {attempt}/{max_retries} "
            f"task={task_id}: {err_msg}")
        try:
            ok, new_output, new_session = agent_runner.run_claude(
                agent_id=agent,
                prompt=correction,
                working_dir=working_dir,
                session_id=session_id,
                timeout=timeout,
                context="inbox",
                model_override=model,
                task_stem=task_id,
                out_meta=meta,
                expected_agent=agent,
                phase=task.get("phase"),
            )
        except Exception as e:
            log(f"[{agent}] mirror-marker self-validate run_claude raised "
                f"task={task_id} attempt={attempt}: {e!r}; keeping prior output")
            break
        # Carry the latest session id forward so the next --resume continues the
        # corrected conversation (run_claude may rotate the session id).
        if new_session:
            session_id = new_session
        if not ok:
            log(f"[{agent}] mirror-marker self-validate run_claude non-success "
                f"task={task_id} attempt={attempt}; keeping prior output, "
                f"falling through to notifier net")
            break
        output_text = new_output
        clean, err_msg = _mirror_marker_is_clean(output_text)
        if clean:
            log(f"[{agent}] mirror-marker self-validate RESOLVED in-process "
                f"task={task_id} after {attempt} re-prompt(s) — zero cross-process "
                f"marker-error round-trips")
            return output_text, session_id

    log(f"[{agent}] mirror-marker self-validate exhausted task={task_id} "
        f"({attempt}/{max_retries}); writing best-effort outbox, outbox_notifier "
        f"marker-error net is the outer backstop")
    return output_text, session_id


def process_task(agent: str, task_file: Path, models_config: dict,
                 slot: int = 0) -> None:
    try:
        task = json.loads(task_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f"[{agent}] malformed task {task_file.name}: {e}")
        write_invalid(task_file, f"json: {e}")
        return

    # Fixture-pattern dispatch gate (2026-05-28 cost-loop fix). Test/fixture
    # envelopes must NEVER reach an LLM dispatch. They leak into live inboxes
    # via the notify/dead-letter/marker-error cascade — whose doubled-prefix
    # routing artifacts self-replicate — and previously burned real Opus cost
    # in a loop. matched_fixture_envelope peels those wrappers so wrapped
    # fixtures still match; we also test the bare task_id field. Single source
    # of truth: fixture_patterns.py. See docs/inbox-watcher-fixture-gate-brief.md.
    fixture_hit = (
        fixture_patterns.matched_fixture_envelope(task_file.stem)
        or fixture_patterns.matched_fixture_pattern(task.get("task_id"))
    )
    if fixture_hit:
        dest = move_to(task_file, INBOXES_ROOT / agent / ".archive")
        log(f"[{agent}] fixture-suppressed task={task_file.stem} "
            f"pattern={fixture_hit!r} dest={dest.name} cost=$0 (not dispatched)")
        append_cost({
            "ts": now_iso(),
            "agent": agent,
            "task_id": task.get("task_id") or task_file.stem,
            "task_type": "fixture-suppressed",
            "model": None,
            # Step C: per-account field for rolling-5h scoping. Fixture-
            # suppressed rows never spawn a Claude subprocess so there is no
            # real tier to record; use 'fixture' as a sentinel so they can be
            # filtered out of account-scoped sums.
            "account": "fixture",
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_sec": 0,
            "attempts": 0,
            "source": "inbox-watcher",
        })
        return

    ok, reason = dispatch_validator.validate_task(task)
    if not ok:
        log(f"[{agent}] validator rejected {task_file.name}: {reason}")
        write_invalid(task_file, f"validator: {reason}")
        return

    # Rotation drain + cooldown gate (spec § 6.3). After fixture-suppression
    # + structural validation (so malformed tasks still write_invalid
    # promptly), skip dispatching NEW top-level tasks when the rotation
    # scheduler has opened the drain gate OR the active tier has a
    # per-account rate-limit cooldown. ALLOW --resume / phase=build|revision
    # continuations through so in-flight work finishes on its original
    # account. Mirrors the _emergency_halt_active() pattern: the task stays
    # in the inbox; the next poll re-evaluates. No archive, no write_invalid
    # — drain must NEVER drop work, only delay it.
    rotation_block = _rotation_gate_block_reason(task)
    if rotation_block:
        log(f"[{agent}] rotation-gate {rotation_block} blocking new "
            f"top-level task={task_file.stem} (continuations pass through)")
        return

    # Phase D3 — defense in depth: re-check role-boundary topology even though
    # tasks written via safe_write_inbox already passed it. Catches tasks that
    # bypassed safe_write_inbox (manual drops, future buggy dispatchers).
    route_ok, route_reason = routing_validator.check_hard_topology(
        task.get("source", ""), agent,
    )
    if not route_ok:
        src = task.get("source", "")
        envelope_id = task.get("task_id") or task_file.stem
        log(f"[{agent}] routing denied for {task_file.name}: {route_reason}")
        write_invalid(task_file, f"routing: {route_reason}")
        # A routing-denied drop means a (possibly user-facing) control surface
        # silently lost an action — never let that be silent again. The
        # 2026-05-28 dashboard gap dropped Larry's Approve/Reject envelopes to
        # .invalid while the API returned 200; a warning alert here is the
        # standing tripwire for any future layer-1/layer-2 mismatch.
        larry_alerts.append_alert(
            source="inbox-watcher",
            severity="warning",
            subject=f"routing-denied:{src}->{agent}",
            message=(
                f"Envelope {envelope_id} dropped to {agent}/.invalid — "
                f"routing denied: {route_reason}. The {src!r} control surface "
                f"lost this action silently (its API call may have returned "
                f"success). No auto-replay; re-issue manually if needed."
            ),
        )
        return

    # Phase D3 commit 4b — defense in depth: re-check target_repo scope.
    # safe_write_inbox enforces this at write time; this catches manual drops.
    repo_ok, repo_reason = routing_validator.check_target_repo(
        agent, task.get("target_repo"),
    )
    if not repo_ok:
        log(f"[{agent}] target_repo denied for {task_file.name}: {repo_reason}")
        write_invalid(task_file, f"target_repo: {repo_reason}")
        return

    task_id = task.get("task_id") or task_file.stem

    # Merged/closed-PR guard (execution-time half). A Mirror review of a PR
    # that has already merged or closed gates nothing — it is pure cost. The
    # dispatch-time pre-check in outbox_notifier skips most of these, but it
    # only sees state at dispatch; a PR can merge in the seconds-to-minutes
    # between dispatch and this launch, and the review session then runs almost
    # entirely AFTER the merge (observed: $0.918 reviewing an already-merged
    # PR). Re-check state here, before the worktree is built and the expensive
    # session starts, and no-op if the PR has left OPEN. Fail-open: every
    # uncertain path (no/unparseable pr_url, OPEN/UNKNOWN, any gh hiccup)
    # returns None and the review proceeds, so a legitimate review is never
    # silently dropped.
    if agent == "mirror" and task.get("phase") == "review":
        terminal_state = _mirror_review_pr_terminal_state(task)
        if terminal_state is not None:
            dest = move_to(task_file, INBOXES_ROOT / agent / ".archive")
            log(f"[{agent}] MIRROR_REVIEW_SKIPPED_TERMINAL task={task_id} "
                f"pr={task.get('pr_url')} state={terminal_state} "
                f"dest={dest.name} cost=$0 (not dispatched — review gates "
                f"nothing on a {terminal_state.lower()} PR)")
            append_cost({
                "ts": now_iso(),
                "agent": agent,
                "task_id": task_id,
                "task_type": "review-skipped-terminal",
                "model": None,
                "account": "skipped",
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "duration_sec": 0,
                "attempts": 0,
                "source": "inbox-watcher",
            })
            return

    model = resolve_model(agent, task, models_config)
    timeout = task.get("timeout") or DEFAULT_TIMEOUT_SEC
    # Only consume session_id when the dispatcher explicitly opted into a
    # --resume continuation. Phase D3 commit 4b wires preflight→build; D3.5
    # commit 5b extends the gate to `revision` (Forge's build session resumes
    # again when Mirror's REVIEW_REVISION dispatches a revision task back to
    # her with her findings as the next user turn). Other notify paths may
    # carry claude_session_id for telemetry, but those sessions belong to
    # the SENDER not the target, so consuming them blindly would resume the
    # wrong agent's conversation.
    #
    # task-25 (2026-05-20) — additional explicit opt-in via `resume_session_id`.
    # outbox_notifier's clarification-response handler writes a resume envelope
    # under phase='preflight' (Forge re-runs preflight with the answer), so the
    # phase-based gate above can't authorize it. Instead the dispatcher names
    # the field `resume_session_id` to make the intent unambiguous — this is
    # the TARGET agent's session to resume, distinct from any sender-side
    # `session_id` that may also ride the envelope for telemetry. Closes the
    # `notify-notify-{task}` doubled-prefix cascade (chain-routing gap #5).
    resume_session_id = task.get("resume_session_id") or (
        task.get("session_id")
        if task.get("phase") in ("build", "revision")
        else None
    )

    # Identity-assertion preamble (Phase D2.5 Call A: on by default;
    # E1.2: gating moved INTO agent_runner.run_claude). expected_agent is
    # implicit from the inbox path (Call B). run_claude handles the three
    # idempotency conditions itself: skips when session_id is set (resume),
    # when the marker is already present in the prompt, and (trivially)
    # when expected_agent is None.
    prompt = task["prompt"]

    # Phase D3 commit 4b: per-agent worktree creation. For agents with
    # worktree_enabled in agent-models.json, dispatch happens inside a
    # /tmp/wt-<agent>-<task_id>/ worktree keyed by task_id so multi-dispatch
    # tasks (preflight → CLARIFY → build under --resume) hit the same
    # worktree across all dispatches. For BUILDER dispatches the branch
    # checkpoint is set up on origin with an empty WIP commit so a session
    # that times out mid-build still has the branch reachable for resume;
    # mirror REVIEWS get a read-only detached checkout instead (see below).
    agents_block = models_config.get("agents", {})
    agent_block = agents_block.get(agent, {})
    working_dir = str(AGENTS_DIR / agent)
    if agent_block.get("worktree_enabled"):
        target_repo = task.get("target_repo")
        canonical_path = (
            _load_repo_paths().get(target_repo) if target_repo else None
        )
        if canonical_path is None:
            log(
                f"[{agent}] worktree_enabled but no canonical path for "
                f"target_repo={target_repo!r} on {task_file.name}; refusing"
            )
            write_invalid(
                task_file,
                f"worktree: no canonical path for target_repo={target_repo!r}",
            )
            return
        envelope_branch = task.get("branch")
        branch = envelope_branch or worktree_manager.derive_branch_name(
            agent, task_id,
        )
        # A mirror envelope that names someone ELSE's branch (review-shaped:
        # phase=review, or a pr_url riding the envelope) gets a READ-ONLY
        # detached checkout — the reviewer must never push to the PR branch
        # under review (WIP push moves the PR head SHA → duplicate round-0
        # reviews past the head-SHA dedup, statuses on a throwaway sha,
        # [WIP] noise merged to main; PR #841 2026-07-08 — see
        # worktree_manager.setup_branch_checkpoint). Keyed on phase OR
        # pr_url because healers may rewrite phase on re-dispatch
        # (heal_resume_paused_on_tier1 forces 'preflight') and ad-hoc
        # review envelopes may omit it; a derived mirror/<task_id> branch
        # (no envelope branch) stays writable — it's mirror-owned scratch
        # space that heal_forge_wip_only_redispatch's WIP signal relies on.
        is_mirror_review = (
            agent == "mirror"
            and bool(envelope_branch)
            and (task.get("phase") == "review" or bool(task.get("pr_url")))
        )
        wt_path, wt_branch = worktree_manager.ensure_worktree_for_task(
            agent_id=agent,
            task_id=task_id,
            canonical_repo=canonical_path,
            branch=branch,
            log_fn=lambda m: log(f"[{agent}] worktree: {m}"),
            readonly=is_mirror_review,
        )
        if wt_path is None:
            log(
                f"[{agent}] WORKTREE FAILED for {task_file.name}; "
                f"bumping requeue, leaving task in inbox"
            )
            bump_requeue(task_file, task)
            return
        if is_mirror_review and wt_branch is None:
            # For a review the checkout IS the load-bearing step: a None
            # branch means the worktree is NOT at the PR head (branch gone
            # from origin, or fetch/checkout failed) and reviewing whatever
            # tree it holds instead can pass unfixed code. Requeue (capped
            # by bump_requeue) rather than review the wrong tree.
            log(
                f"[{agent}] readonly checkout of {branch} failed for "
                f"{task_file.name}; bumping requeue (won't review the "
                f"wrong tree)"
            )
            bump_requeue(task_file, task)
            return
        # The worktree ROOT has no top-level CLAUDE.md (agent identities live
        # in agents/<agent>/ subdirs), so this cwd is NOT the worker's identity
        # source. Identity is pinned deterministically downstream via
        # expected_agent=<agent> below -> run_claude's identity_pin_args, which
        # is independent of cwd/CLAUDE.md discovery. Do not infer the worker's
        # identity from this working_dir.
        working_dir = str(wt_path)
        log(
            f"[{agent}] worktree ready: {wt_path} "
            f"(branch={wt_branch}, target_repo={target_repo})"
        )

    log(f"[{agent}] start task={task_id} model={model} timeout={timeout}s"
        + (f" resume={resume_session_id[:12]}..." if resume_session_id else ""))

    # Two-slot observability (spec §4 PR3). For a Mirror review, stamp the slot
    # onto run_claude's start-line (review_slot=<n>, alongside the existing
    # dispatch_tier=<t>) and emit the PR-open → review-start queue-wait so the
    # burst-latency success metric (§8) and the sibling gauge (§4 "or sibling
    # gauge") have real samples. Best-effort: never blocks or fails the review.
    is_review_dispatch = agent == "mirror" and task.get("phase") == "review"
    review_slot = slot if is_review_dispatch else None
    if is_review_dispatch:
        emit_review_queue_wait(task_id, task.get("pr_url"), slot)

    meta: dict = {}
    try:
        success, output_text, session_id = agent_runner.run_claude(
            agent_id=agent,
            prompt=prompt,
            working_dir=working_dir,
            session_id=resume_session_id,
            timeout=timeout,
            context="inbox",
            model_override=model,
            task_stem=task_id,
            out_meta=meta,
            expected_agent=agent,
            phase=task.get("phase"),
            review_slot=review_slot,
        )
    except Exception as e:
        log(f"[{agent}] agent_runner.run_claude raised on {task_file.name}: {e!r}")
        outbox = _build_outbox(agent, task_id, task, task_file,
                               False, "", None, meta,
                               error=f"agent_runner exception: {e!r}")
        outbox_path = _unique_dest(
            OUTBOXES_ROOT / safe_write_inbox.sanitize_component(agent),
            safe_write_inbox.sanitize_component(f"{task_id}.json"),
        )
        outbox_written = False
        try:
            outbox_path.parent.mkdir(parents=True, exist_ok=True)
            outbox_path.write_text(json.dumps(outbox, indent=2))
            outbox_written = True
        except OSError as oe:
            log(f"[{agent}] outbox write also failed: {oe}")
        try:
            # No outbox persisted → archive under the lost-result marker so
            # the review-dedup can tell "died verdict-less, re-dispatchable"
            # from a concluded run (see _archive_dir / LOST_RESULT_SUBDIR).
            move_to(task_file, _archive_dir(agent,
                                            lost_result=not outbox_written))
        except OSError:
            pass
        return

    # TIER_HOLD (spec §4 caller contract): run_claude could not pick a dispatch
    # tier — a TOCTOU after the gate (a tier benched between gate and spawn) or
    # a resume whose bound tier benched mid-flight. NO LLM spend happened
    # (run_claude returned before spawning), so HOLD like the rotation gate:
    # leave the task in the inbox, no outbox, no archive, no requeue-bump; the
    # next poll re-evaluates when a tier frees (the §9 all-held alert makes a
    # persistent hold visible). NEVER drop held work.
    if not success and isinstance(output_text, str) \
            and output_text.startswith('TIER_HOLD:'):
        log(f"[{agent}] TIER_HOLD task={task_id} held in inbox "
            f"({output_text}); next poll re-evaluates")
        return

    # mirror-marker-self-validate-gate-001: bounded SAME-PROCESS verdict-marker
    # self-validation, in FRONT of the outbox_notifier marker-error net. Only on
    # run_claude success (a non-success is a different class and gets no
    # re-prompt); the helper further gates on agent=="mirror" + phase=="review".
    # Substitutes the corrected output_text/session_id before the single outbox
    # write below; adds no new outbox.
    if success:
        output_text, session_id = _mirror_marker_self_validate(
            agent=agent,
            task=task,
            output_text=output_text,
            session_id=session_id,
            working_dir=working_dir,
            model=model,
            timeout=timeout,
            task_id=task_id,
            meta=meta,
            models_config=models_config,
        )

    outbox = _build_outbox(agent, task_id, task, task_file,
                           success, output_text, session_id, meta,
                           error=None if success else (output_text or "claude returned non-success"))

    outbox_path = _unique_dest(
        OUTBOXES_ROOT / safe_write_inbox.sanitize_component(agent),
        safe_write_inbox.sanitize_component(f"{task_id}.json"),
    )
    try:
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        outbox_path.write_text(json.dumps(outbox, indent=2))
    except OSError as e:
        # nervous-system-audit #13 (2026-06-05): the run_claude call above
        # already PAID for the LLM (and may have produced side effects — a PR,
        # a merge). bump_requeue leaves the task in the inbox, so the next poll
        # re-runs it and re-pays + duplicates those side effects. That violates
        # this module's own "NEVER re-dispatch paid work" policy (see
        # reap_orphans_on_startup). Archive the task instead so it cannot
        # re-run; the lost outbox is recovered by the reconcile/heal layer
        # (missing-outbox sweeps), which is strictly safer than a double-bill.
        log(f"[{agent}] outbox write failed for {task_file.name}: {e}; "
            f"archiving task under the lost-result marker WITHOUT requeue "
            f"to avoid a paid re-run (result lost — for a phase=review task "
            f"the review-dedup re-dispatches off the marker, bounded)")
        # The run already PAID — record the spend before archiving so the
        # budget/quota ledger stays accurate even though the outbox is lost.
        # (The old re-run path eventually costed it on a successful retry; this
        # path never retries, so record it here.)
        _record_outbox_cost(agent, task_id, outbox)
        try:
            # Lost-result marker (rename-only, see _archive_dir): the result
            # was never persisted, so downstream recovery that keys off the
            # outbox can never fire for this run.
            move_to(task_file, _archive_dir(agent, lost_result=True))
        except OSError as me:
            log(f"[{agent}] archive after outbox-write failure also failed "
                f"for {task_file}: {me}")
        return

    _record_outbox_cost(agent, task_id, outbox)

    try:
        move_to(task_file, INBOXES_ROOT / agent / ".archive")
    except OSError as e:
        log(f"[{agent}] archive failed for {task_file}: {e}")

    log(f"[{agent}] done task={task_id} success={success} "
        f"duration={outbox.get('duration_sec')}s "
        f"attempts={outbox.get('attempts')} "
        f"cost=${outbox.get('cost_usd') if outbox.get('cost_usd') is not None else '?'}")


def _emergency_halt_active() -> bool:
    """True if the EMERGENCY_HALT flag is present. Writers: scripts/kill_switch.py
    or manual `touch ~/agents/blackboard/EMERGENCY_HALT`. Clearing the file
    requires the operator (no auto-clear) — a tripped halt is sticky until
    investigated."""
    return EMERGENCY_HALT_FILE.exists()


def _is_continuation_task(task: dict) -> bool:
    """True if the envelope is a continuation / --resume dispatch that must
    NOT be blocked by the rotation gate. Mirrors the resume gate in
    process_task: a continuation either carries an explicit
    ``resume_session_id`` (clarification-response writes one under
    phase=preflight) OR has ``phase in (build, revision)`` (outbox notifier
    writes these with the target agent's session_id).

    Continuations finish on the original account because session IDs are
    not portable between OAuth tiers (see agent_runner.TIER2_HOME comment).
    Blocking them on rotation state would orphan in-flight work."""
    if not isinstance(task, dict):
        return False
    if task.get("resume_session_id"):
        return True
    if task.get("phase") in ("build", "revision"):
        return True
    return False


def _tier_pool_hold_file() -> Path:
    """State file recording when the tier pool first went all-unavailable, so
    the § 9 escalation only fires after a PERSISTENT hold (not a transient
    blip). Resolved at CALL time to honor the OURLIBERTY_AGENTS_ROOT sandbox
    redirect (AGENTS_ROOT is frozen at import)."""
    root = os.environ.get("OURLIBERTY_AGENTS_ROOT")
    base = Path(root) if root else AGENTS_ROOT
    return base / "state" / "tier-pool-hold.json"


def _clear_tier_pool_hold() -> None:
    """Reset the all-held clock — the pool can serve a dispatch again."""
    try:
        _tier_pool_hold_file().unlink()
    except OSError:
        pass


def _tier_pool_hold_reasons() -> str:
    """One-line per-tier reason string (usable / cooldown / auth / near-cap)
    from active_tier.tier_pool_status, for the § 9 alert body. Best-effort;
    never raises."""
    try:
        st = active_tier.tier_pool_status()
    except Exception:
        return "status unavailable"
    parts = []
    for tier, info in (st.get("tiers") or {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("usable"):
            why = "usable"
        elif info.get("cooldown_until"):
            why = f"cooldown until {info['cooldown_until']}"
        elif not info.get("auth_ok", True):
            why = "auth down"
        elif info.get("near_cap"):
            why = "near cap"
        else:
            why = "unavailable"
        parts.append(f"{tier}={why}")
    return "; ".join(parts) if parts else "no tiers"


def _escalate_tier_pool_held_if_persistent(now=None) -> None:
    """Spec § 9: when the pool has been unable to serve ANY new dispatch for
    longer than ``tier_pool.hold_alert_minutes``, fire ONE deduped
    ``larry_alert`` naming each tier's reason. ``append_alert`` supplies the
    dedup/cooldown (so it never spams and re-arms per window); the hold file
    only enforces the initial delay so a transient blip does not page. Never
    raises."""
    now = now or datetime.now(timezone.utc)
    path = _tier_pool_hold_file()
    held_since = None
    try:
        raw = json.loads(path.read_text()).get("held_since")
        held_since = datetime.fromisoformat(raw) if raw else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        held_since = None
    if held_since is None:
        # First blocked poll of this episode — start the clock, don't page yet.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"held_since": now.isoformat()}))
        except OSError:
            pass
        return
    if held_since.tzinfo is None:
        held_since = held_since.replace(tzinfo=timezone.utc)
    try:
        threshold_min = float(
            active_tier._tier_pool_config().get("hold_alert_minutes", 10))
    except Exception:
        threshold_min = 10.0
    if (now - held_since) < timedelta(minutes=threshold_min):
        return
    larry_alerts.append_alert(
        source="inbox-watcher",
        severity="warning",
        subject="tier-pool-all-unavailable",
        message=(
            "No dispatch tier is available — new work is holding in the inbox. "
            "Per-tier: " + _tier_pool_hold_reasons() + ". New dispatches resume "
            "automatically when a tier frees; no action needed unless this "
            "persists."
        ),
        suggested_action=(
            "cat ~/agents/blackboard/active-tier.json  # inspect cooldowns; "
            "`echo tier1 > ~/agents/rotation.disabled` force-pins a tier"
        ),
        route="escalate",
    )


def _rotation_gate_block_reason(task: dict) -> str | None:
    """Block a NEW top-level dispatch iff the tier pool has NOTHING to dispatch
    on (spec § 10-G): ``active_tier.select_dispatch_tier(None) is None`` — every
    primary benched/near-cap AND the fallback held under its reserve.
    Continuations (``--resume`` / ``phase=build|revision``) ALWAYS pass so
    in-flight work finishes on its bound account (session IDs are not portable
    across tiers). On a persistent all-held state this also escalates via § 9.

    Replaces the old drain/active-tier-cooldown gate: under per-task dispatch
    a cooldown on ONE tier no longer blocks new work — the selector routes to a
    healthy tier. (While an operator pin is set, the selector returns the pin
    UNCONDITIONALLY — the § 16 rollback contract requires it, so the gate is
    pass-through and § 9 pool-exhaustion escalation is bypassed until the pin is
    removed at cutover. That is by design: under a pin the operator has taken
    manual control, and a pinned tier that walls is still surfaced by the
    per-failure rate-limit ledger + DMs from run_claude.)

    Defense-in-depth: any error reading the pool is treated as open-gate
    (return None). The gate is an additive guard; run_claude re-selects and
    holds/pauses per-task if the pool truly cannot serve."""
    # Probe pool health with a SIDE-EFFECT-FREE check (no round-robin bump)
    # BEFORE the continuation early-return, so the all-held debounce clock is
    # cleared on EVERY healthy poll — incl. continuation-only or idle recovery.
    # Otherwise the clock, cleared only on a fresh-top-level open, goes stale
    # and the next unrelated all-held blip pages instantly (defeating § 9's
    # 10-min debounce).
    try:
        available = active_tier.has_usable_dispatch_tier()
    except Exception:
        return None
    if available:
        _clear_tier_pool_hold()
        return None
    # Pool has NOTHING to dispatch on. Continuations still pass so in-flight
    # work finishes on its bound account; only NEW top-level dispatches are held.
    if _is_continuation_task(task):
        return None
    try:
        _escalate_tier_pool_held_if_persistent()
    except Exception:
        pass
    return "tier-pool-unavailable"


def agent_loop(agent: str, models_config: dict, slot: int = 0,
               total_slots: int = 1) -> None:
    log(f"[{agent}] loop started (slot={slot} of {total_slots})")
    # With one slot the single lease serializes the whole loop, so scan ->
    # process needs no per-task claim (behavior identical to pre-slot). With
    # >1 slot the leases are per-slot (they no longer serialize each other), so
    # two threads can scan the same task file — the atomic claim (spec §3.2) is
    # what guarantees exactly one slot processes it.
    claim_enabled = total_slots > 1
    identity = _slot_identity(agent, slot)
    while not _shutdown.is_set():
        if _emergency_halt_active():
            log(f"[{agent}] EMERGENCY_HALT detected at {EMERGENCY_HALT_FILE}; shutting down")
            _shutdown.set()
            break

        try:
            tasks = scan_inbox(agent)
        except OSError as e:
            log(f"[{agent}] scan_inbox error: {e}")
            tasks = []

        if not tasks:
            # Idle poll: if the pool has recovered, clear any stale all-held
            # debounce clock even though no task is here to open the gate — the
            # held tasks may already have drained (§ 9; side-effect-free probe).
            try:
                if active_tier.has_usable_dispatch_tier():
                    _clear_tier_pool_hold()
            except Exception:
                pass
            _shutdown.wait(POLL_INTERVAL_SEC)
            continue

        for task_file in tasks:
            if _shutdown.is_set() or _emergency_halt_active():
                break
            acq = dispatch_lease.try_acquire(identity)
            if not acq.get("acquired"):
                # Held by another process (or stale lease still within TTL).
                # Wait one poll interval and re-scan.
                break
            nonce = acq.get("nonce")
            hb = dispatch_lease.Heartbeat(identity, nonce) if nonce else None
            if hb:
                hb.start()
            target = None
            head_identity = None
            head_nonce = None
            head_hb = None
            try:
                target = task_file
                if claim_enabled:
                    target = _claim_task(agent, task_file, slot)
                    if target is None:
                        # Another slot claimed this task first; try the next one
                        # (the finally block releases the lease before continue).
                        continue
                    # Same-head concurrent-review guard (spec §4 PR2): serialize
                    # review of one PR head across slots via a race-free
                    # head-lease. If another slot already holds this head, defer
                    # (un-claim → next poll) so the two never review one diff at
                    # once. Only Mirror review tasks carry head_sha; others skip.
                    head = _task_head_sha(target)
                    if head:
                        head_identity = _head_lease_identity(agent, head)
                        head_acq = dispatch_lease.try_acquire(head_identity)
                        if not head_acq.get("acquired"):
                            log(f"[{agent}] slot {slot}: head {head[:12]} already "
                                f"under review by another slot; deferring "
                                f"{target.name}")
                            _unclaim_task(agent, target)
                            target = None
                            head_identity = None
                            continue
                        head_nonce = head_acq.get("nonce")
                        head_hb = (dispatch_lease.Heartbeat(head_identity, head_nonce)
                                   if head_nonce else None)
                        if head_hb:
                            head_hb.start()
                process_task(agent, target, models_config, slot=slot)
            except Exception as e:
                log(f"[{agent}] unexpected error on {task_file.name}: {e!r}")
            finally:
                # A claimed task still present here means process_task took a
                # transient-defer path (rotation gate / TIER_HOLD / worktree
                # bump_requeue) and expects the next poll to re-see it — but it
                # is parked under .claimed/<slot>/ where scan_inbox can't. Move
                # it back to the live inbox so the defer contract holds instead
                # of stranding it until restart. Terminal paths already moved it.
                if claim_enabled and target is not None and target.exists():
                    _unclaim_task(agent, target)
                if head_hb:
                    head_hb.stop()
                if head_identity is not None:
                    dispatch_lease.release(head_identity, head_nonce)
                if hb:
                    hb.stop()
                dispatch_lease.release(identity, nonce)

        _shutdown.wait(POLL_INTERVAL_SEC)
    log(f"[{agent}] loop exiting")


def _install_signals() -> None:
    def handler(signum, _frame):
        log(f"received signal {signum}; shutting down")
        _shutdown.set()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main() -> int:
    ensure_dirs()

    # Re-queue tasks stranded in .claimed/<slot>/ by a slot that died mid-flight
    # (spec §3.2). MUST run before reap_orphans_on_startup: it consults the
    # in-flight registry to avoid re-dispatching paid work, and reap_orphans
    # deletes those registry entries.
    swept_claims = sweep_claimed_orphans()
    if swept_claims:
        log(f"startup: re-queued {swept_claims} orphaned claim(s)")

    # Reap any in-flight registry entries from a prior boot (Phase D2.5 Call C:
    # adopt-if-alive, mark-failed-if-dead, never re-dispatch).
    reap_orphans_on_startup()

    # Clear any leases left over from previous boots (PID re-use guard inside
    # dispatch_lease handles current-boot stale entries automatically).
    swept = dispatch_lease.startup_sweep()
    if swept:
        log(f"startup_sweep cleared {swept} prev-boot leases")

    _install_signals()
    models_config = load_models()
    log(f"starting; agents={AGENTS} poll={POLL_INTERVAL_SEC}s "
        f"runner=agent_runner.run_claude")

    if _emergency_halt_active():
        log(f"WARNING: EMERGENCY_HALT exists at startup ({EMERGENCY_HALT_FILE}); "
            f"agent loops will see it and exit immediately. Remove the file to resume.")

    threads = []
    for a in AGENTS:
        slots = _review_slots_for(a, models_config)
        if slots > 1:
            log(f"[{a}] review_slots={slots}; spawning {slots} concurrent loops")
        for slot in range(slots):
            t = threading.Thread(
                target=agent_loop,
                args=(a, models_config, slot, slots),
                name=f"loop-{a}-s{slot}",
            )
            t.start()
            threads.append(t)

    for t in threads:
        t.join()

    log("shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
