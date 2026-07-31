#!/usr/bin/env python3
"""heal_lost_marker.py — catch approval markers rendered but never emitted.

This is the THIRD net in the marker-vs-prose disease, keyed off the deterministic
RENDER event. On 2026-06-03/04 the lesson was 'the helper built to stop lost
markers is how one got lost': `scripts/marker.py render beacon approval_request`
produces a marker block but, until lost-marker-render-emission-net-001, wrote NO
persistent record of the render. If Beacon fails to PASTE the block into her
Telegram reply, the marker never dispatches, the approval gate never reaches
Larry, and NOTHING detects the loss — the analysis work is paid for and the
decision silently vanishes.

Two existing nets cover the CLAIM side:
  - scripts/heal_phantom_dispatch_claim.py (after-the-fact detector)
  - the authoritative-dispatch-confirmation prose guard (emission-time)
BOTH key off Beacon's outbound PROSE ('dispatched', 'goes to Forge now'). Neither
catches the case where Beacon renders a marker but neither pastes it NOR claims
anything — with no prose to detect, both nets are blind. This healer complements
them: it reconciles the RENDER ledger (written by marker.py cmd_render) against
EMISSION evidence.

DETECTION ONLY. Auto-re-emit is out of scope — a rendered marker may have been
intentionally abandoned (a superseded draft re-rendered under a new task_id), and
reconstructing intent is unsafe. Per tick, for each ledger entry older than the
GRACE window and not already reconciled, verify emission evidence. A render is
EMITTED (reconciled) if ANY of:
  - the `approval DMed for <task_id>` bot-log line
    (beacon_telegram_bot.py:1228 — the force_ask DM the notifier writes after a
    successful marker extraction + trust-policy gate);
  - a real Forge-dispatch artifact (REUSED from heal_phantom_dispatch_claim's
    dispatch_exists probes: inbox <task>.json/build-<task>.json live/.archive/
    .invalid, state/in-flight/<task>.json, worktree wt-forge-<task>,
    outbox-notifier.log line);
  - presence in beacon-pending-approvals.json (the approvals store).
If ANY -> reconciled, no alert. If NONE after the grace window -> one larry-alert
(route=escalate, severity=warning, subject lost-marker:<task_id>) in PLAIN
language, deduped on task_id+rendered_at.

KNOWN LIMITATION (accepted + documented): a rendered-then-superseded draft
(re-rendered under a NEW task_id) flags as lost. Accepted — the alert frames it
as possibly-superseded and dismissible; a dismissible flag beats a silently-lost
paid-for decision (Larry: 'flag any finished-but-never-delivered task').

Stdlib only. Env-overridable paths for hermetic tests (OURLIBERTY_AGENTS_ROOT,
OURLIBERTY_MARKER_RENDER_LEDGER, OURLIBERTY_LOG_DIR, OURLIBERTY_WORKTREES_ROOT),
mirroring heal_phantom_dispatch_claim.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# REUSE heal_phantom_dispatch_claim's artifact probes rather than reimplement the
# Forge-dispatch surface enumeration. Its dispatch_exists() + beacon_log_file() /
# log_dir() read the OURLIBERTY_* env on every call, so the hermetic test env
# flows through unchanged.
import heal_phantom_dispatch_claim as hpdc  # noqa: E402

CONFIG_FILE = _SCRIPT_DIR.parent / 'config' / 'lost-marker-patterns.json'

DEFAULT_GRACE_MINUTES = 15
DEFAULT_SCAN_WINDOW_MINUTES = 1440


# ---------- path resolution (all env-overridable for hermetic tests) ----------


def agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def blackboard() -> Path:
    return agents_root() / 'blackboard'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def healer_heartbeat() -> Path:
    return blackboard() / 'heal-lost-marker.heartbeat'


def alerted_state_file() -> Path:
    return blackboard() / 'lost-marker-alerted.json'


def render_ledger_path() -> Path:
    # Same resolution as marker.py.render_ledger_path so writer + reader agree.
    override = os.environ.get('OURLIBERTY_MARKER_RENDER_LEDGER')
    if override:
        return Path(override)
    return blackboard() / 'marker-render-ledger.jsonl'


def pending_approvals_path() -> Path:
    # Mirrors beacon_approval_handler.PENDING_APPROVALS_PATH.
    return agents_root() / 'state' / 'beacon-pending-approvals.json'


def healer_log_file() -> Path:
    return agents_root() / 'logs' / 'heal-lost-marker.log'


# ---------- logging / heartbeat ----------


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        lf = healer_log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        hb = healer_heartbeat()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(
            json.dumps({'ts': datetime.now(timezone.utc).isoformat()})
        )
    except OSError:
        pass


# ---------- config ----------


def load_config(path: Optional[Path] = None) -> dict:
    """Return the grace/scan-window config. Best-effort: a missing or invalid
    config falls back to defaults (and logs) rather than failing the net — unlike
    heal_phantom_dispatch_claim's load-bearing pattern list, this config carries
    only numeric tuning, so a bad file must not blind the reconciler."""
    cfg_path = path or CONFIG_FILE
    try:
        data = json.loads(cfg_path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('lost-marker config is not a JSON object')
        return data
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log(f'config unreadable ({type(e).__name__}: {e}); using defaults',
            'WARN')
        return {}


def _grace_sec(cfg: dict) -> float:
    val = cfg.get('grace_minutes', DEFAULT_GRACE_MINUTES)
    return (val if isinstance(val, (int, float)) and val > 0
            else DEFAULT_GRACE_MINUTES) * 60.0


def _scan_window_sec(cfg: dict) -> float:
    val = cfg.get('scan_window_minutes', DEFAULT_SCAN_WINDOW_MINUTES)
    return (val if isinstance(val, (int, float)) and val > 0
            else DEFAULT_SCAN_WINDOW_MINUTES) * 60.0


# ---------- ledger reading ----------


def _parse_iso_epoch(value: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp to epoch seconds; None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    v = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def read_ledger_entries(now: float, cfg: dict) -> list[dict]:
    """Parse render-ledger JSONL lines within the scan window into entry dicts.

    An entry is {task_id, rendered_at, rendered_ts (epoch), summary, phase}. Only
    records that carry a task_id + a parseable rendered_at inside the scan window
    are returned; malformed lines are skipped so one bad append never blinds the
    reconciler."""
    window_start = now - _scan_window_sec(cfg)
    entries: list[dict] = []
    try:
        text = render_ledger_path().read_text(encoding='utf-8', errors='replace')
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        task_id = rec.get('task_id')
        rendered_at = rec.get('rendered_at')
        if not isinstance(task_id, str) or not task_id:
            continue
        rendered_ts = _parse_iso_epoch(rendered_at)
        if rendered_ts is None:
            continue
        if rendered_ts < window_start or rendered_ts > now:
            continue
        entries.append({
            'task_id': task_id,
            'rendered_at': rendered_at,
            'rendered_ts': rendered_ts,
            'summary': rec.get('summary', ''),
            'phase': rec.get('phase'),
        })
    return entries


# ---------- emission-evidence verification ----------


def _bot_log_dmed(task_id: str) -> bool:
    """True if the `approval DMed for <task_id>` line exists in the Beacon bot
    log — the load-bearing proof-of-emission signal (beacon_telegram_bot.py:1228,
    written after the notifier extracts the marker + the trust-policy gate)."""
    needle = f'approval DMed for {task_id}'
    try:
        with open(hpdc.beacon_log_file(), encoding='utf-8',
                  errors='replace') as fh:
            for line in fh:
                if needle in line:
                    return True
    except OSError:
        return False
    return False


def _in_pending_approvals(task_id: str) -> bool:
    """True if task_id appears in beacon-pending-approvals.json (pending OR
    history) — a rendered marker that reached the approvals store was emitted."""
    try:
        data = json.loads(pending_approvals_path().read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    for bucket in ('pending', 'history'):
        items = data.get(bucket)
        if not isinstance(items, list):
            continue
        for entry in items:
            if isinstance(entry, dict) and entry.get('id') == task_id:
                return True
    return False


def emission_evidence(task_id: str) -> tuple[bool, str]:
    """True (+evidence label) if ANY emission surface shows this rendered marker
    was actually emitted. Checked at evaluation time, so a late-but-real emission
    correctly reads as 'not lost'."""
    if _bot_log_dmed(task_id):
        return True, 'bot-log:approval-DMed'
    exists, ev = hpdc.dispatch_exists(task_id)
    if exists:
        return True, ev
    if _in_pending_approvals(task_id):
        return True, 'pending-approvals'
    return False, ''


# ---------- evaluation ----------


def _dedup_key(entry: dict) -> str:
    return f'{entry["task_id"]}|{entry["rendered_at"]}'


def evaluate_entry(entry: dict, cfg: dict, now: float) -> Optional[dict]:
    """Return an alert dict for a lost marker, or None (inside grace / emission
    evidence found). Does the live evidence lookups."""
    grace = _grace_sec(cfg)
    age = now - entry['rendered_ts']
    if age < grace:
        return None  # inside grace — normal render->paste->gate lag.

    found, _ev = emission_evidence(entry['task_id'])
    if found:
        return None

    task_id = entry['task_id']
    mins = age / 60.0
    return {
        'key': _dedup_key(entry),
        'subject': f'lost-marker:{task_id}',
        'message': (
            f'A decision was prepared but never reached you: task `{task_id}` '
            f'had an approval marker RENDERED at {entry["rendered_at"]} '
            f'({mins:.0f} min ago) but it was never emitted — no approval DM, '
            'no Forge dispatch, and nothing in the approvals store. It may be a '
            'superseded draft (re-prepared under a different id) — if so, this '
            'is safely dismissible. If not, re-run it. Nothing was dispatched '
            'and no decision is currently waiting on you.'
        ),
        'suggested_action': (
            f'Check whether `{task_id}` still matters. If it does, re-run it so '
            'Beacon emits (and PASTES) a fresh approval marker; if it was '
            'superseded, dismiss this alert.'
        ),
    }


# ---------- dedup state ----------


def load_alerted_state() -> set[str]:
    try:
        data = json.loads(alerted_state_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    keys = data.get('alerted') if isinstance(data, dict) else None
    return set(k for k in keys if isinstance(k, str)) if isinstance(keys, list) \
        else set()


def save_alerted_state(keys: set[str]) -> None:
    """Atomic tmp + replace. Best-effort; never raises (a watcher must not die
    because it could not persist dedup state — worst case is a duplicate DM,
    which larry_alerts' per-subject cooldown still dampens)."""
    payload = {
        '_schema': {
            'version': 1,
            'purpose': (
                'Dedup keys (task_id|rendered_at) for lost-marker renders '
                'already alerted, so each lost marker DMs Larry exactly once '
                'across ticks. Idempotent.'
            ),
        },
        'alerted': sorted(keys),
    }
    try:
        path = alerted_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass


# ---------- orchestration ----------


def run_once(now: float, cfg: dict, alerted: set[str]) -> list[dict]:
    """Return new (not-yet-alerted) lost-marker alerts. Pure w.r.t. the dedup
    set: does not mutate `alerted` or persist anything — main() owns that."""
    out: list[dict] = []
    for entry in read_ledger_entries(now, cfg):
        key = _dedup_key(entry)
        if key in alerted:
            continue
        alert = evaluate_entry(entry, cfg, now)
        if alert is not None:
            out.append(alert)
    return out


def main() -> int:
    if kill_switch().exists():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    cfg = load_config()
    now = time.time()
    alerted = load_alerted_state()
    alerts = run_once(now, cfg, alerted)
    if not alerts:
        log('tick: no lost markers')
        return 0

    fired = 0
    for alert in alerts:
        delivered = _emit(
            subject=alert['subject'],
            message=alert['message'],
            suggested_action=alert['suggested_action'],
        )
        # Mark alerted once we have attempted the emit: a lost marker is a
        # one-time finding, and re-DMing it every tick would just be noise.
        alerted.add(alert['key'])
        log(f'{"alerted" if delivered else "suppressed"}: {alert["subject"]}',
            'WARN' if delivered else 'INFO')
        fired += int(bool(delivered))
    save_alerted_state(alerted)
    log(f'done: {fired} alert(s) fired, {len(alerts) - fired} suppressed')
    return 0


def _emit(subject: str, message: str, suggested_action: str) -> bool:
    """Append a larry-alert (route=escalate, severity warning). Cooldown +
    routing enforced inside larry_alerts. Never raises."""
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        import larry_alerts as la  # noqa: E402

        return la.append_alert(
            source='heal-lost-marker',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001
        log(f'emit failed for {subject}: {type(e).__name__}: {e}', 'WARN')
        return False


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        # Self-failure must reach Larry: an exception here means the lost-marker
        # net itself is down.
        _emit(
            subject='lost-marker-self-failure',
            message=(
                'heal_lost_marker.py crashed '
                f'({type(exc).__name__}: {exc}); rendered-but-never-emitted '
                'approval markers are not being detected until it is fixed.'
            ),
            suggested_action=(
                'Run python3 ~/agent-core/scripts/heal_lost_marker.py and read '
                'the traceback.'
            ),
        )
        sys.exit(1)
