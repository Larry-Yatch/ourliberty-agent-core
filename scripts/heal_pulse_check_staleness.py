#!/usr/bin/env python3
"""heal_pulse_check_staleness.py — watch the watchers.

A Pulse check (I-X) is the one fleet component with no liveness signal of its
own: when the Pulse agent's /cycle stops invoking it, it goes dark silently.
That is what hid the IX/X stall for ~2 days (cost-signal audit, 2026-06-03).

Each check now emits a success heartbeat via scripts/pulse_check_heartbeat.py
(blackboard/pulse-check-<id>.heartbeat). This watcher runs on an OnCalendar
timer and, for each check in config/pulse-check-cadence.json, alerts when the
last success signal is older than cadence_hours + grace_hours. Freshness-of-
success catches all three failure modes — errored, can't-run/missing-env, and
fully-silent timer death — where a bare try/except would miss the silent stop.

Routing: pulse-check-stale:<id> goes through the shared larry_alerts queue
(severity warning, default 'escalate' route) -> Beacon triage -> Larry as an
outcome/escalation. A dark check usually cannot self-resolve, so it will
legitimately reach Larry — as an outcome, not a routine ping.

Fail-closed: a check the watcher knows about (the canonical set, plus any
pulse-check-<id>.heartbeat present on disk) that has no cadence entry, or a
malformed entry, gets a pulse-check-no-cadence:<id> alert rather than being
silently unmonitored.

DETECTION ONLY. This does not fix WHY a scheduler stopped invoking a check —
that root-cause is a separate follow-up. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Canonical check set. Independent of the config on purpose: a check present
# here but absent from the registry is a fail-closed alert, not a silent skip.
# (Check II was a deprecated draft that never shipped — the gap is by design.)
CANONICAL_CHECKS = ['i', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x']

# Bootstrap liveness fallback. Before the heartbeat-emitting check code has run
# even once (first deploy), use each check's existing dated proposal/audit
# artifact as the liveness signal. This is what lets the watcher flag the
# already-dark checks on first deploy (brief "Built-in validation") instead of
# false-alarming every check for lack of a brand-new heartbeat file. Once
# heartbeats exist they dominate (emitted every run, including clean skips).
ARTIFACT_GLOBS = {
    'i':    ['pulse-check-i/check-i-*.json'],
    'iii':  ['pulse-check-iii-proposals/check-iii-*.json'],
    'iv':   ['pulse-check-iv-proposals/check-iv-*.json'],
    'v':    ['pulse-check-v-proposals/check-v-*.json'],
    'vi':   ['pulse-check-vi-proposals/check-vi-*.json'],
    'vii':  ['pulse-check-vii-proposals/check-vii-*.json'],
    'viii': ['pulse-check-viii-proposals/check-viii-*.json'],
    'ix':   ['pulse-check-ix-proposals/check-ix-*.json'],
    'x':    ['pulse-check-x-proposals/check-x-*.json'],
}

CONFIG_FILE = (
    Path(__file__).resolve().parent.parent / 'config' / 'pulse-check-cadence.json'
)


def agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def blackboard() -> Path:
    return agents_root() / 'blackboard'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def healer_heartbeat() -> Path:
    return blackboard() / 'heal-pulse-check-staleness.heartbeat'


def log_file() -> Path:
    return agents_root() / 'logs' / 'heal-pulse-check-staleness.log'


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        hb = healer_heartbeat()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def load_cadence_config(path: Optional[Path] = None) -> dict:
    """Return {check_id: entry}. Raises on unreadable/invalid JSON so main()
    can fail loud rather than silently monitoring nothing."""
    cfg_path = path or CONFIG_FILE
    data = json.loads(cfg_path.read_text(encoding='utf-8'))
    checks = data.get('checks')
    if not isinstance(checks, dict):
        raise ValueError("cadence config missing a 'checks' object")
    return checks


def _read_heartbeat_ts(path: Path) -> Optional[float]:
    """Epoch seconds of the heartbeat's ts field; fall back to file mtime."""
    try:
        raw = path.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    try:
        obj = json.loads(raw)
        ts = obj.get('ts') if isinstance(obj, dict) else None
        if ts:
            return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def latest_signal_ts(check_id: str, bb: Path) -> tuple[Optional[float], str]:
    """Newest liveness signal for a check: max of the heartbeat ts and the
    newest matching artifact mtime. Returns (epoch_or_None, source)."""
    candidates: list[tuple[float, str]] = []

    hb = bb / f'pulse-check-{check_id}.heartbeat'
    if hb.exists():
        ts = _read_heartbeat_ts(hb)
        if ts is not None:
            candidates.append((ts, 'heartbeat'))

    for pattern in ARTIFACT_GLOBS.get(check_id, []):
        for artifact in bb.glob(pattern):
            try:
                candidates.append((artifact.stat().st_mtime, 'artifact'))
            except OSError:
                continue

    if not candidates:
        return None, 'none'
    ts, source = max(candidates, key=lambda c: c[0])
    return ts, source


def discover_heartbeat_ids(bb: Path) -> set[str]:
    """Check ids that have a heartbeat file on disk (catches a check added to
    the fleet but never registered in the cadence config)."""
    found: set[str] = set()
    try:
        for hb in bb.glob('pulse-check-*.heartbeat'):
            name = hb.name[len('pulse-check-'):-len('.heartbeat')]
            if name:
                found.add(name)
    except OSError:
        pass
    return found


def evaluate_check(
    check_id: str,
    entry: Optional[dict],
    bb: Path,
    now: float,
) -> Optional[dict]:
    """Pure: return an alert dict ({key, subject, message, suggested_action})
    for this check, or None if it is fresh / event-driven."""
    if entry is None:
        return {
            'key': f'pulse-check-no-cadence:{check_id}',
            'subject': f'pulse-check-no-cadence:{check_id}',
            'message': (
                f'Pulse check {check_id} has no entry in '
                'config/pulse-check-cadence.json, so its liveness is not being '
                'monitored. Fail-closed: alerting rather than leaving it dark.'
            ),
            'suggested_action': (
                'Add a cadence_hours + grace_hours (or event_driven) entry for '
                f'"{check_id}" to config/pulse-check-cadence.json.'
            ),
        }

    if entry.get('event_driven'):
        return None  # external trigger; time-based staleness does not apply.

    cadence_h = entry.get('cadence_hours')
    if not isinstance(cadence_h, (int, float)) or cadence_h <= 0:
        return {
            'key': f'pulse-check-no-cadence:{check_id}',
            'subject': f'pulse-check-no-cadence:{check_id}',
            'message': (
                f'Pulse check {check_id} has a malformed cadence entry '
                f'(cadence_hours={cadence_h!r}); cannot evaluate staleness. '
                'Fail-closed: alerting rather than leaving it dark.'
            ),
            'suggested_action': (
                'Fix the cadence_hours value for '
                f'"{check_id}" in config/pulse-check-cadence.json.'
            ),
        }

    grace_h = entry.get('grace_hours', 0) or 0
    threshold = (cadence_h + grace_h) * 3600.0
    label = entry.get('label', check_id)

    ts, source = latest_signal_ts(check_id, bb)
    if ts is None:
        return {
            'key': f'pulse-check-stale:{check_id}',
            'subject': f'pulse-check-stale:{check_id}',
            'message': (
                f'Pulse check {check_id} ({label}) has never emitted a success '
                'heartbeat and has no recent artifact — it may have never run, '
                'or stopped before its first heartbeat under the new code.'
            ),
            'suggested_action': (
                f'Run python3 ~/agent-core/scripts/pulse_check_{check_id}.py '
                'and confirm it exits 0; check why the Pulse /cycle is not '
                'invoking it.'
            ),
        }

    age = now - ts
    if age <= threshold:
        return None

    age_h = age / 3600.0
    return {
        'key': f'pulse-check-stale:{check_id}',
        'subject': f'pulse-check-stale:{check_id}',
        'message': (
            f'Pulse check {check_id} ({label}) has gone stale: last success '
            f'signal ({source}) is {age_h:.1f}h old, past the '
            f'{cadence_h:g}h cadence + {grace_h:g}h grace. The check has '
            'silently stopped firing — its invoker is most likely not calling '
            'it.'
        ),
        'suggested_action': (
            f'Run python3 ~/agent-core/scripts/pulse_check_{check_id}.py to '
            'confirm the check itself is healthy (it usually is — the failure '
            'is in the scheduler), then check why the Pulse /cycle stopped '
            'invoking it. Logs: ~/agents/logs/pulse-check-*.log.'
        ),
    }


def evaluate_all(config: dict, bb: Path, now: Optional[float] = None) -> list[dict]:
    """Pure: alert dicts for every stale / unmonitored check."""
    now = now if now is not None else time.time()
    ids = set(CANONICAL_CHECKS) | discover_heartbeat_ids(bb) | set(config)
    alerts: list[dict] = []
    for check_id in sorted(ids):
        alert = evaluate_check(check_id, config.get(check_id), bb, now)
        if alert is not None:
            alerts.append(alert)
    return alerts


def main() -> int:
    if kill_switch().exists():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    try:
        config = load_cadence_config()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        # The registry drives everything; an unreadable one is itself the
        # silent-death risk. Alert loudly and stop.
        log(f'cadence config unreadable: {type(e).__name__}: {e}', 'ERROR')
        _emit(
            subject='pulse-check-config-unreadable',
            message=(
                'config/pulse-check-cadence.json could not be loaded '
                f'({type(e).__name__}: {e}); the pulse-check liveness watcher '
                'is monitoring nothing until it is fixed.'
            ),
            suggested_action=(
                'Validate config/pulse-check-cadence.json (python3 -c '
                '"import json,sys; json.load(open(sys.argv[1]))" '
                'config/pulse-check-cadence.json).'
            ),
        )
        return 1

    alerts = evaluate_all(config, blackboard())
    if not alerts:
        log('tick: all pulse checks fresh')
        return 0

    fired = 0
    for alert in alerts:
        delivered = _emit(
            subject=alert['subject'],
            message=alert['message'],
            suggested_action=alert['suggested_action'],
        )
        log(f'{"alerted" if delivered else "suppressed"}: {alert["key"]}',
            'WARN' if delivered else 'INFO')
        fired += int(bool(delivered))
    log(f'done: {fired} alert(s) fired, {len(alerts) - fired} suppressed')
    return 0


def _emit(subject: str, message: str, suggested_action: str) -> bool:
    """Append a larry-alert. Cooldown + routing are enforced inside
    larry_alerts. Never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402

        return la.append_alert(
            source='heal-pulse-check-staleness',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:  # noqa: BLE001
        log(f'emit failed for {subject}: {type(e).__name__}: {e}', 'WARN')
        return False


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
