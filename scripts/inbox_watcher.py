#!/usr/bin/env python3
"""inbox_watcher.py — shared multi-agent inbox watcher daemon (Phase D2).

Polls ~/agents/inboxes/{beacon,forge,mirror,pulse}/*.json on a 5s cadence.
For each task: validate -> acquire lease -> spawn `claude --print` -> write
outbox -> append cost record -> archive task -> release lease.

Concurrency model: one thread per agent. Each thread holds the lease
"inbox:<agent>" while running a task, so at most one task per agent is in
flight at any time, but all four agents run in parallel. Cross-process
safety + restart-safety come from dispatch_lease (flock + nonce + TTL +
boot-id PID-reuse guard).

stdlib only. Reuses dispatch_validator.validate_task and dispatch_lease.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch_lease  # noqa: E402
import dispatch_validator  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = HOME / "agents"
INBOXES_ROOT = AGENTS_ROOT / "inboxes"
OUTBOXES_ROOT = AGENTS_ROOT / "outboxes"
BLACKBOARD = AGENTS_ROOT / "blackboard"
LOG_FILE = AGENTS_ROOT / "logs" / "inbox_watcher.log"
COSTS_FILE = BLACKBOARD / "costs.jsonl"
AGENT_CORE = HOME / "agent-core"
MODELS_FILE = AGENT_CORE / "config" / "agent-models.json"
AGENTS_DIR = AGENT_CORE / "agents"

AGENTS = ["beacon", "forge", "mirror", "pulse"]
POLL_INTERVAL_SEC = 5
DEFAULT_TIMEOUT_SEC = 14400  # 4h; matches HANDSHAKE-SCHEMA default
FALLBACK_MODEL = "claude-sonnet-4-6"
REQUEUE_MAX = 3

CLAUDE_BIN = shutil.which("claude") or "/usr/bin/claude"

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


def run_claude(agent: str, task: dict, model: str) -> tuple[dict, bool]:
    cwd = AGENTS_DIR / agent
    prompt = task["prompt"]
    timeout = task.get("timeout") or DEFAULT_TIMEOUT_SEC

    cmd = [CLAUDE_BIN, "--print", "--output-format", "json", "--model", model, prompt]

    started = now_iso()
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return ({
            "started_at": started,
            "completed_at": now_iso(),
            "duration_sec": round(time.time() - t0, 2),
            "exit_code": -1,
            "error": f"timeout after {timeout}s",
            "model": model,
            "result": "",
        }, False)
    except FileNotFoundError:
        return ({
            "started_at": started,
            "completed_at": now_iso(),
            "duration_sec": round(time.time() - t0, 2),
            "exit_code": -2,
            "error": f"claude binary not found at {CLAUDE_BIN}",
            "model": model,
            "result": "",
        }, False)

    out = {
        "started_at": started,
        "completed_at": now_iso(),
        "duration_sec": round(time.time() - t0, 2),
        "exit_code": result.returncode,
        "model": model,
    }

    if result.returncode != 0:
        out["error"] = (result.stderr or "")[:2000]
        out["result"] = (result.stdout or "")[:2000]
        return (out, False)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        out["result"] = (result.stdout or "").strip()
        out["error"] = "claude stdout not JSON"
        return (out, False)

    out["result"] = (data.get("result") or data.get("text") or "").strip()
    out["claude_session_id"] = data.get("session_id")
    cost = data.get("total_cost_usd")
    if cost is None:
        cost = data.get("cost_usd")
    out["cost_usd"] = cost
    usage = data.get("usage") or {}
    out["usage"] = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read": usage.get("cache_read_input_tokens"),
        "cache_creation": usage.get("cache_creation_input_tokens"),
    }
    return (out, True)


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

    task_id = task.get("task_id") or task_file.stem
    model = resolve_model(agent, task, models_config)

    log(f"[{agent}] start task={task_id} model={model} file={task_file.name}")
    outbox, success = run_claude(agent, task, model)
    outbox.update({
        "task_id": task_id,
        "agent": agent,
        "source_task_file": str(task_file),
        "dedup_identity": task.get("dedup_identity"),
        "reply_chat_id": task.get("reply_chat_id"),
        "source": task.get("source"),
    })

    outbox_path = _unique_dest(OUTBOXES_ROOT / agent, f"{task_id}.json")
    try:
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        outbox_path.write_text(json.dumps(outbox, indent=2))
    except OSError as e:
        log(f"[{agent}] outbox write failed: {e}; leaving task for retry")
        bump_requeue(task_file, task)
        return

    if outbox.get("cost_usd") is not None:
        append_cost({
            "ts": outbox["completed_at"],
            "agent": agent,
            "task_id": task_id,
            "model": model,
            "cost_usd": outbox.get("cost_usd"),
            "input_tokens": outbox.get("usage", {}).get("input_tokens"),
            "output_tokens": outbox.get("usage", {}).get("output_tokens"),
            "cache_read": outbox.get("usage", {}).get("cache_read"),
            "cache_creation": outbox.get("usage", {}).get("cache_creation"),
            "duration_sec": outbox.get("duration_sec"),
            "source": "inbox-watcher",
        })

    try:
        move_to(task_file, INBOXES_ROOT / agent / ".archive")
    except OSError as e:
        log(f"[{agent}] archive failed for {task_file}: {e}")

    log(f"[{agent}] done task={task_id} success={success} "
        f"duration={outbox.get('duration_sec')}s cost=${outbox.get('cost_usd', '?')}")


def agent_loop(agent: str, models_config: dict) -> None:
    log(f"[{agent}] loop started")
    while not _shutdown.is_set():
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
            if _shutdown.is_set():
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
    swept = dispatch_lease.startup_sweep()
    if swept:
        log(f"startup_sweep cleared {swept} prev-boot leases")

    _install_signals()
    models_config = load_models()
    log(f"starting; agents={AGENTS} poll={POLL_INTERVAL_SEC}s claude={CLAUDE_BIN}")

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
