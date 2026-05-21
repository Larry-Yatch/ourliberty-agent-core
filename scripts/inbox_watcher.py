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
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from task_type_inference import infer_task_type

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import agent_runner  # noqa: E402
import dispatch_lease  # noqa: E402
import dispatch_validator  # noqa: E402
import routing_validator  # noqa: E402
import worktree_manager  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = HOME / "agents"
INBOXES_ROOT = AGENTS_ROOT / "inboxes"
OUTBOXES_ROOT = AGENTS_ROOT / "outboxes"
BLACKBOARD = AGENTS_ROOT / "blackboard"
LOG_FILE = AGENTS_ROOT / "logs" / "inbox_watcher.log"
COSTS_FILE = BLACKBOARD / "costs.jsonl"
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
    inbox = INBOXES_ROOT / agent
    if not inbox.exists():
        return []
    entries = []
    for e in os.scandir(inbox):
        if not e.is_file() or e.name.startswith(".") or not e.name.endswith(".json"):
            continue
        entries.append((e.stat().st_mtime, Path(e.path)))
    entries.sort(key=lambda x: x[0])
    return [p for _, p in entries]


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


def write_invalid(task_file: Path, reason: str) -> None:
    agent = task_file.parent.name
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

        outbox_dir = OUTBOXES_ROOT / agent
        try:
            outbox_dir.mkdir(parents=True, exist_ok=True)
            forfeit = _unique_dest(outbox_dir, f"{task_stem}.forfeit.json")
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
        "attempts": meta.get("attempts"),
        "result": output_text or "",
        "claude_session_id": session_id,
        "cost_usd": meta.get("cost_usd"),
        "usage": meta.get("usage"),
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


def process_task(agent: str, task_file: Path, models_config: dict) -> None:
    try:
        task = json.loads(task_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f"[{agent}] malformed task {task_file.name}: {e}")
        write_invalid(task_file, f"json: {e}")
        return

    ok, reason = dispatch_validator.validate_task(task)
    if not ok:
        log(f"[{agent}] validator rejected {task_file.name}: {reason}")
        write_invalid(task_file, f"validator: {reason}")
        return

    # Phase D3 — defense in depth: re-check role-boundary topology even though
    # tasks written via safe_write_inbox already passed it. Catches tasks that
    # bypassed safe_write_inbox (manual drops, future buggy dispatchers).
    route_ok, route_reason = routing_validator.check_hard_topology(
        task.get("source", ""), agent,
    )
    if not route_ok:
        log(f"[{agent}] routing denied for {task_file.name}: {route_reason}")
        write_invalid(task_file, f"routing: {route_reason}")
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
    # worktree across all dispatches. The branch checkpoint is set up on
    # origin with an empty WIP commit so a session that times out mid-build
    # still has the branch reachable for resume.
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
        branch = task.get("branch") or worktree_manager.derive_branch_name(
            agent, task_id,
        )
        wt_path, wt_branch = worktree_manager.ensure_worktree_for_task(
            agent_id=agent,
            task_id=task_id,
            canonical_repo=canonical_path,
            branch=branch,
            log_fn=lambda m: log(f"[{agent}] worktree: {m}"),
        )
        if wt_path is None:
            log(
                f"[{agent}] WORKTREE FAILED for {task_file.name}; "
                f"bumping requeue, leaving task in inbox"
            )
            bump_requeue(task_file, task)
            return
        working_dir = str(wt_path)
        log(
            f"[{agent}] worktree ready: {wt_path} "
            f"(branch={wt_branch}, target_repo={target_repo})"
        )

    log(f"[{agent}] start task={task_id} model={model} timeout={timeout}s"
        + (f" resume={resume_session_id[:12]}..." if resume_session_id else ""))
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
        )
    except Exception as e:
        log(f"[{agent}] agent_runner.run_claude raised on {task_file.name}: {e!r}")
        outbox = _build_outbox(agent, task_id, task, task_file,
                               False, "", None, meta,
                               error=f"agent_runner exception: {e!r}")
        outbox_path = _unique_dest(OUTBOXES_ROOT / agent, f"{task_id}.json")
        try:
            outbox_path.parent.mkdir(parents=True, exist_ok=True)
            outbox_path.write_text(json.dumps(outbox, indent=2))
        except OSError as oe:
            log(f"[{agent}] outbox write also failed: {oe}")
        try:
            move_to(task_file, INBOXES_ROOT / agent / ".archive")
        except OSError:
            pass
        return

    outbox = _build_outbox(agent, task_id, task, task_file,
                           success, output_text, session_id, meta,
                           error=None if success else (output_text or "claude returned non-success"))

    outbox_path = _unique_dest(OUTBOXES_ROOT / agent, f"{task_id}.json")
    try:
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        outbox_path.write_text(json.dumps(outbox, indent=2))
    except OSError as e:
        log(f"[{agent}] outbox write failed: {e}; bumping requeue and leaving task in inbox")
        bump_requeue(task_file, task)
        return

    if outbox.get("cost_usd") is not None:
        usage = outbox.get("usage") or {}
        # task_type either rides through from the dispatch envelope (already
        # in outbox) or is inferred from task_id prefix so cost rows are not
        # uniformly "unknown". See task_type_inference for the discriminator.
        task_type = outbox.get("task_type") or infer_task_type(task_id)
        append_cost({
            "ts": outbox["completed_at"],
            "agent": agent,
            "task_id": task_id,
            "task_type": task_type,
            "model": outbox.get("model"),
            "cost_usd": outbox.get("cost_usd"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read": usage.get("cache_read"),
            "cache_creation": usage.get("cache_creation"),
            "duration_sec": outbox.get("duration_sec"),
            "attempts": outbox.get("attempts"),
            "source": "inbox-watcher",
        })

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


def agent_loop(agent: str, models_config: dict) -> None:
    log(f"[{agent}] loop started")
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
            _shutdown.wait(POLL_INTERVAL_SEC)
            continue

        identity = f"inbox:{agent}"
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
            try:
                process_task(agent, task_file, models_config)
            except Exception as e:
                log(f"[{agent}] unexpected error on {task_file.name}: {e!r}")
            finally:
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
        t = threading.Thread(target=agent_loop, args=(a, models_config), name=f"loop-{a}")
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    log("shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
