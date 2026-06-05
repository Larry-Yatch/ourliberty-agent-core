#!/usr/bin/env python3
"""dispatch_sentinel.py — stall detection for inbox + in-flight tasks + leases.

Phase D3, Gap 2. Runs every 10 minutes via systemd timer. Three scans per run:

  1. **Inbox stalls** — tasks sitting in `inboxes/<agent>/` past
     INBOX_STALL_SECONDS (3h) without being picked up. Respects `not_before`
     cooldowns on requeued tasks.
  2. **In-flight stalls** — tasks recorded in `state/in-flight/<stem>.json`
     past their per-model threshold without completion (i.e., picked up by
     the watcher but stuck mid-claude). This is the D3-specific addition;
     upstream did not have this scan.
  3. **Stale leases** — `state/dispatch-leases/*.lease` files whose
     `timestamp_renewed` is older than STALE_LEASE_SECONDS (15m), indicating
     the heartbeat stopped without reclaim.

Alerts:

  - First time a task crosses threshold: write a record to
    `~/agents/blackboard/sentinel-alerts.jsonl` AND log to the unit's stderr.
  - State persisted in `state/dispatch-sentinel.json` so we don't repeat the
    same alert every cycle.

This script does NOT kill stalled tasks. Cancel-marker
(`blackboard/cancel-task-<stem>.json`) is the explicit human-in-loop kill
switch (see `agent_runner.run_claude` lines 409–429). The sentinel surfaces
the stall; the operator decides whether to cancel.

A future commit (D3-approval) will wire a Telegram DM to Larry via Beacon's
bot when ALERTS_PAUSED is not set. For D3-prep, alerts land on disk only.

Adapted from GrowthMastery-ai/gm-agent-core (`scripts/dispatch_sentinel.py`)
for Larry-Yatch/ourliberty-agent-core (2026-05-11, Phase D3-prep — Gap 2).

Usage:
  python3 dispatch_sentinel.py
    Exits 0 always; alerts to disk + stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import larry_alerts  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
STATE_FILE = AGENTS_ROOT / 'state' / 'dispatch-sentinel.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'dispatch-sentinel.log'
ALERTS_LOG = AGENTS_ROOT / 'blackboard' / 'sentinel-alerts.jsonl'
LEASES_DIR = AGENTS_ROOT / 'state' / 'dispatch-leases'
IN_FLIGHT_DIR = AGENTS_ROOT / 'state' / 'in-flight'
# Source-of-truth git copy (~/agent-core/config), NOT AGENTS_ROOT/config
# (~/agents) which is a hand-synced runtime copy that drifts stale.
# Matches inbox_watcher._MODELS_CONFIG_PATH so all consumers read one file.
AGENT_MODELS_FILE = _SCRIPTS_DIR.parent / 'config' / 'agent-models.json'

MONITORED_AGENTS = ['beacon', 'forge', 'mirror', 'pulse']

INBOX_STALL_SECONDS = 3 * 60 * 60       # 3h — task sitting unpicked
STALE_LEASE_SECONDS = 15 * 60           # 15m — heartbeat stopped, no reclaim
DEFAULT_IN_FLIGHT_THRESHOLD = 30 * 60   # 30m default if model unknown


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [sentinel] [{level}] {msg}\n'
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line)
    # Also surface to stderr so systemd journal captures it.
    sys.stderr.write(line)


def load_state() -> tuple[dict[str, Any], bool]:
    """Return (state, cold_start).

    cold_start=True when STATE_FILE is missing OR was corrupted/unreadable.
    Callers suppress the larry-alerts Telegram append on cold starts so a
    corrupted state file doesn't fan out every pre-existing stall to the
    user as a fresh DM (design review C2).
    """
    if not STATE_FILE.exists():
        return {'alerted': {}}, True
    try:
        with open(STATE_FILE) as f:
            return json.load(f), False
    except (json.JSONDecodeError, OSError):
        return {'alerted': {}}, True


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_FILE)


def _per_model_threshold(model_name: str | None) -> int:
    """Return seconds. Larger thresholds for Opus, smaller for Sonnet/Haiku."""
    if not model_name:
        return DEFAULT_IN_FLIGHT_THRESHOLD
    n = model_name.lower()
    if 'opus' in n:
        return 60 * 60          # 60m
    if 'sonnet' in n:
        return 30 * 60          # 30m
    if 'haiku' in n:
        return 15 * 60          # 15m
    return DEFAULT_IN_FLIGHT_THRESHOLD


def _agent_inbox_model(agent_id: str) -> str | None:
    """Look up the agent's inbox model from agent-models.json. Best-effort."""
    if not AGENT_MODELS_FILE.exists():
        return None
    try:
        with open(AGENT_MODELS_FILE) as f:
            cfg = json.load(f)
        agents = cfg.get('agents', {})
        return agents.get(agent_id, {}).get('inbox_model')
    except (json.JSONDecodeError, OSError):
        return None


def scan_inbox(agent_id: str, now: float) -> list[dict[str, Any]]:
    """Stalls in `inboxes/<agent>/` — tasks not picked up after threshold."""
    inbox = AGENTS_ROOT / 'inboxes' / agent_id
    if not inbox.exists():
        return []
    stalls = []
    for task_file in inbox.glob('*.json'):
        try:
            stat = task_file.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        if age < INBOX_STALL_SECONDS:
            continue
        source = 'unknown'
        not_before = None
        reply_chat = None
        try:
            with open(task_file) as f:
                data = json.load(f)
            source = data.get('source', 'unknown')
            not_before = data.get('not_before')
            reply_chat = data.get('reply_chat_id')
        except (json.JSONDecodeError, OSError):
            pass
        if not_before:
            try:
                nb = datetime.fromisoformat(not_before.replace('Z', '+00:00'))
                if nb.tzinfo is not None:
                    nb = nb.astimezone(timezone.utc).replace(tzinfo=None)
                if nb > datetime.utcnow():
                    continue
            except (ValueError, AttributeError):
                pass
        stalls.append({
            'kind': 'inbox-stall',
            'agent': agent_id,
            'file': task_file.name,
            'path': str(task_file),
            'age_seconds': int(age),
            'age_hours': round(age / 3600, 2),
            'source': source,
            'reply_chat_id': reply_chat,
        })
    return stalls


def _started_epoch(data: dict[str, Any]) -> float:
    """Epoch seconds for when the in-flight worker started, 0.0 if unknown.

    agent_runner._register_in_flight writes `started_at` as an ISO-8601 STRING
    (datetime.now(timezone.utc).isoformat()). The prior code did float(started)
    on that string → ValueError → started_f=0.0 → `if started_f <= 0: continue`
    skipped EVERY in-flight entry, so the stall scan was dead code: a worker
    wedged mid-dispatch (exactly what this scan exists to catch) was never
    surfaced. Parse the ISO form first, then fall back to a raw epoch float for
    the legacy `timestamp_started` field."""
    iso = data.get('started_at')
    if isinstance(iso, str) and iso:
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass  # not ISO — maybe a numeric string; fall through to float()
    for raw in (data.get('started_at'), data.get('timestamp_started')):
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def scan_in_flight(now: float) -> list[dict[str, Any]]:
    """Stalls in `state/in-flight/` — tasks picked up but stuck past threshold."""
    if not IN_FLIGHT_DIR.exists():
        return []
    stalls = []
    for reg_file in IN_FLIGHT_DIR.glob('*.json'):
        try:
            with open(reg_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        started_f = _started_epoch(data)
        if started_f <= 0:
            continue
        age = now - started_f
        agent_id = data.get('agent_id') or data.get('agent', 'unknown')
        model = data.get('model') or _agent_inbox_model(agent_id)
        threshold = _per_model_threshold(model)
        if age < threshold:
            continue
        stalls.append({
            'kind': 'in-flight-stall',
            'agent': agent_id,
            'file': reg_file.name,
            'path': str(reg_file),
            'age_seconds': int(age),
            'age_hours': round(age / 3600, 2),
            'threshold_seconds': threshold,
            'model': model,
            'task_stem': data.get('task_stem'),
            'pid': data.get('pid'),
        })
    return stalls


def scan_stale_leases(now: float) -> list[dict[str, Any]]:
    """Leases whose heartbeat is dead but nothing reclaimed them."""
    if not LEASES_DIR.exists():
        return []
    stalls = []
    for lease_file in LEASES_DIR.glob('*.lease'):
        try:
            with open(lease_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        last_renewed = data.get('timestamp_renewed', 0)
        try:
            last_renewed_f = float(last_renewed)
        except (TypeError, ValueError):
            continue
        age = now - last_renewed_f
        if age < STALE_LEASE_SECONDS:
            continue
        stalls.append({
            'kind': 'stale-lease',
            'agent': data.get('identity', '').split(':')[0] or 'unknown',
            'file': lease_file.name,
            'path': str(lease_file),
            'age_seconds': int(age),
            'age_hours': round(age / 3600, 2),
            'identity': data.get('identity', lease_file.stem),
        })
    return stalls


def alert_key(stall: dict[str, Any]) -> str:
    """Stable key for dedup — kind + path is enough."""
    return f"{stall['kind']}:{stall['path']}"


def record_alert(stall: dict[str, Any]) -> None:
    """Append a JSON line to sentinel-alerts.jsonl."""
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = dict(stall)
    record['alerted_at'] = datetime.now(timezone.utc).isoformat()
    with open(ALERTS_LOG, 'a') as f:
        f.write(json.dumps(record) + '\n')


def _stall_dm_message(stall: dict[str, Any]) -> str:
    """Plain-English summary for the Telegram DM."""
    kind = stall.get('kind', '?')
    agent = stall.get('agent', '?')
    file = stall.get('file', '?')
    age_h = stall.get('age_hours', '?')
    if kind == 'inbox-stall':
        return (
            f'Inbox task on {agent} unpicked for {age_h}h: {file}'
        )
    if kind == 'in-flight-stall':
        threshold = stall.get('threshold_seconds', 0)
        return (
            f'In-flight task on {agent} stuck for {age_h}h '
            f'(threshold {int(threshold/60)}m): {file}'
        )
    if kind == 'stale-lease':
        return (
            f'Stale lease for {agent} ({stall.get("identity", file)}) — '
            f'no renew for {age_h}h.'
        )
    return f'{kind} on {agent}: {file} ({age_h}h)'


def main() -> int:
    now = time.time()
    state, cold_start = load_state()
    alerted: dict[str, float] = state.get('alerted', {})
    if cold_start:
        log('cold start — state file missing or corrupted; this sweep will '
            'record alerts to disk but will NOT DM Larry (re-arming dedup)',
            'INFO')

    all_stalls: list[dict[str, Any]] = []
    for agent_id in MONITORED_AGENTS:
        all_stalls.extend(scan_inbox(agent_id, now))
    all_stalls.extend(scan_in_flight(now))
    all_stalls.extend(scan_stale_leases(now))

    new_alerts = 0
    for stall in all_stalls:
        key = alert_key(stall)
        if key in alerted:
            continue
        alerted[key] = now
        record_alert(stall)
        new_alerts += 1
        log(
            f"{stall['kind']} on {stall.get('agent', '?')}: "
            f"{stall.get('file', '?')} age={stall.get('age_hours', '?')}h",
            'WARN',
        )
        # D3.5-prep: surface new stalls to Larry via the shared alert queue.
        # Suppressed on cold start to avoid fanning out pre-existing stalls
        # if the state file was corrupted (design review C2).
        if not cold_start:
            larry_alerts.append_alert(
                source='sentinel',
                severity='warning',
                subject=key,
                message=_stall_dm_message(stall),
                suggested_action=(
                    f'ls -la {stall.get("path", "?")}'
                    if stall.get('path') else None
                ),
            )

    # Garbage-collect alerted entries whose underlying file no longer exists.
    # Keeps the state file from growing unboundedly.
    to_drop = []
    for key in alerted:
        try:
            _kind, path = key.split(':', 1)
        except ValueError:
            to_drop.append(key)
            continue
        if not Path(path).exists():
            to_drop.append(key)
    for k in to_drop:
        alerted.pop(k, None)

    state['alerted'] = alerted
    state['last_run'] = datetime.now(timezone.utc).isoformat()
    state['last_new_alerts'] = new_alerts
    save_state(state)

    if new_alerts == 0:
        log(f'sweep complete — {len(all_stalls)} known stalls, 0 new')
    else:
        log(f'sweep complete — {len(all_stalls)} known stalls, {new_alerts} new')
    return 0


if __name__ == '__main__':
    sys.exit(main())
