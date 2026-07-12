#!/usr/bin/env python3
"""heal_pr_terminal_fanout_heartbeat.py — DM Larry if the fan-out sentinel dies.

Spec: agents/beacon/specs/completeness-pr3-fanout-sentinel.md § 6.

The pr_terminal_fanout sentinel writes a heartbeat to
~/agents/blackboard/pr-terminal-fanout.heartbeat every run (OnCalendar 15 min).
This watcher runs on its own timer and DMs Larry if the heartbeat is older than
STALE_THRESHOLD_SEC (45 min == ~3 missed passes). The plain shape from
heal_chain_event_shipper_heartbeat — NOT the advancer watcher, whose default-OFF
activation-gate probe doesn't apply here (the sentinel has no default-off gate).

Death-alarm hardening (§ 6): the DM is emitted with route='escalate' AND the
never_silence entry in alert-translations.json under source 'pr-terminal-fanout'
guarantees it can't be triaged away — a dead self-watch must always reach Larry.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
EMERGENCY_HALT = AGENTS_ROOT / 'blackboard' / 'EMERGENCY_HALT'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'pr-terminal-fanout.heartbeat'
HEALER_HEARTBEAT = AGENTS_ROOT / 'blackboard' / 'heal-pr-terminal-fanout-heartbeat.heartbeat'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-pr-terminal-fanout-heartbeat.log'

# The emitting source string — MUST match the sentinel's ALERT_SOURCE and the
# never_silence entry in config/alert-translations.json exactly (§ 6).
ALERT_SOURCE = 'pr-terminal-fanout'
# The stale-alert subject (also the DM subject below) — the retraction keys on
# this SAME source:subject so it clears exactly its own stale red, nothing else.
ALERT_SUBJECT = 'pr-terminal-fanout-stale'

STALE_THRESHOLD_SEC = 45 * 60       # 45 min == ~3 missed 15-min passes (§ 6).


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEALER_HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEALER_HEARTBEAT.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists() or EMERGENCY_HALT.exists()


def _dm_larry(message: str, subject: str, suggested_action: str) -> bool:
    """Fire larry_alerts.append_alert with route='escalate' (the death alarm can
    never be triaged away, § 6). Falls back to the no-route signature for an
    older append_alert — the never_silence translation still escalates."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        try:
            return bool(la.append_alert(
                source=ALERT_SOURCE, severity='critical', subject=subject,
                message=message, suggested_action=suggested_action,
                route='escalate',
            ))
        except TypeError:
            return bool(la.append_alert(
                source=ALERT_SOURCE, severity='critical', subject=subject,
                message=message, suggested_action=suggested_action,
            ))
    except Exception as e:  # noqa: BLE001
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


def _retract_standdown() -> int:
    """Positive-clear retraction: the heartbeat was just observed FRESH, so the
    sentinel recovered. Retract any stale 🔴 this healer emitted for it and emit
    one closure stand-down (auditable, never silent). Keyed on this healer's own
    `source:subject`. Best-effort — swallow any error so it can never break the
    tick, mirroring the alert path's fire-and-forget posture."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        removed = la.retract_with_standdown(
            f'{ALERT_SOURCE}:{ALERT_SUBJECT}',
            standdown_message=(
                'pr_terminal_fanout sentinel heartbeat is fresh again — the '
                'sentinel has recovered. Standing down the earlier '
                'stale-heartbeat alert.'
            ),
        )
        if removed:
            log(f'retracted {removed} stale fanout-heartbeat alert line(s) '
                f'(heartbeat fresh again)')
        return removed
    except Exception as e:  # noqa: BLE001
        log(f'_retract_standdown failed: {type(e).__name__}: {e}', 'WARN')
        return 0


def check_staleness(now: float | None = None) -> tuple[bool, float, str]:
    """Return (is_stale, age_seconds, reason)."""
    if not HEARTBEAT_FILE.exists():
        return True, -1.0, 'heartbeat file does not exist'
    try:
        mtime = HEARTBEAT_FILE.stat().st_mtime
    except OSError as e:
        return True, -1.0, f'heartbeat stat failed: {e}'
    age = (now or time.time()) - mtime
    if age > STALE_THRESHOLD_SEC:
        return True, age, f'heartbeat mtime {int(age)}s old'
    return False, age, 'fresh'


def main() -> int:
    if kill_switch_active():
        log('KILL_SWITCH / EMERGENCY_HALT active; exiting')
        return 0
    heartbeat()

    stale, age, reason = check_staleness()
    if not stale:
        log(f'tick: sentinel heartbeat fresh ({int(age)}s)')
        # POSITIVE-CLEAR ONLY: retract our own stale red solely on a positively
        # observed fresh heartbeat (reason == 'fresh'), NEVER on a degraded /
        # unreadable probe. check_staleness returns is_stale=True on every error
        # path, so a degraded read never reaches this branch — but gate on the
        # positive sentinel explicitly so the invariant is local and testable.
        if reason == 'fresh':
            _retract_standdown()
        return 0

    log(f'STALE sentinel heartbeat — {reason}', 'WARN')
    delivered = _dm_larry(
        message=(
            'pr_terminal_fanout sentinel heartbeat is stale (>45 min). The '
            'terminal-event fan-out sentinel is most likely crashed or wedged.\n\n'
            f'Reason: {reason}\n'
            f'Heartbeat path: {HEARTBEAT_FILE}'
        ),
        subject=ALERT_SUBJECT,
        suggested_action=(
            'ssh larry@134.209.44.80 and run: '
            'systemctl status ourliberty-pr-terminal-fanout.service '
            '&& journalctl -u ourliberty-pr-terminal-fanout.service '
            '--since "1 hour ago" | tail -100. '
            'If wedged: sudo systemctl start ourliberty-pr-terminal-fanout.service'
        ),
    )
    log(f'DM dispatched={delivered}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
