#!/usr/bin/env python3
"""heal_build_sequence_advancer_heartbeat.py — DM Larry if the advancer is stale.

Phase E-orchestrator PR-S2. Spec: agents/beacon/specs/build-sequence-orchestrator.md
§ 5.4 failure mode 3 ("Advancer daemon failure. Detected via
heal_build_sequence_advancer_heartbeat.py").

The build_sequence_advancer daemon writes a heartbeat to
~/agents/blackboard/build-sequence-advancer.heartbeat every 5 min via its
systemd timer. This healer runs every 5 min via its own systemd timer and
DMs Larry (warning severity) when the heartbeat is older than
STALE_THRESHOLD_SEC (10 min per spec § 5.4 failure mode 3: "if heartbeat
timestamp is more than 10 min stale, DM Larry"). At the 5-min advancer
cadence that's 2 missed ticks; tight but spec-verbatim. The
chain-event-shipper analogue uses the same 10-min floor over a 30-second
heartbeat cadence.

Mirrors heal_chain_event_shipper_heartbeat.py exactly in shape — the
codebase has ten such healers and uniformity beats per-healer cleverness.

Kill switches:
  - ~/agents/healers.disabled — blanket switch shared with every other healer
  - OURLIBERTY_HEAL_BUILD_SEQUENCE_ADVANCER_DISABLE=true — per-healer
    override useful for muting this one healer while a known issue is in
    flight
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'build-sequence-advancer.heartbeat'
HEALER_HEARTBEAT = (
    AGENTS_ROOT / 'blackboard' / 'heal-build-sequence-advancer-heartbeat.heartbeat'
)
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-build-sequence-advancer-heartbeat.log'

# Spec § 5.4 failure mode 3: 10 min. At the 5-min advancer cadence that's
# 2 missed ticks — tight but spec-verbatim. The pre-revision value (15
# min == 3 missed ticks) was looser; Mirror's revision 1 caught the
# drift and we tightened to match the spec.
STALE_THRESHOLD_SEC = 10 * 60

PER_HEALER_KILL_ENV = 'OURLIBERTY_HEAL_BUILD_SEQUENCE_ADVANCER_DISABLE'


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
    if KILL_SWITCH.exists():
        return True
    return os.environ.get(PER_HEALER_KILL_ENV, '').strip().lower() in (
        'true', '1', 'yes', 'on',
    )


def _dm_larry(message: str, subject: str, suggested_action: str) -> bool:
    """Fire larry_alerts.append_alert. Cooldown enforced inside.

    Severity is `warning` — the advancer-stale condition is recoverable
    (restart the service) and does not represent data loss. `critical` is
    reserved for safety / data-integrity events per the codebase's
    existing two-level severity vocabulary."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-build-sequence-advancer-heartbeat',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


def check_staleness(now: float | None = None) -> tuple[bool, float, str]:
    """Return (is_stale, age_seconds, reason).

    Treats a missing heartbeat file as stale because (a) the daemon may
    have crashed before its first tick, and (b) it gives operators a
    clear signal during initial install ("the unit is enabled but the
    file never appeared")."""
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
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    stale, age, reason = check_staleness()
    if not stale:
        log(f'tick: advancer heartbeat fresh ({int(age)}s)')
        return 0

    log(f'STALE advancer heartbeat — {reason}', 'WARN')
    delivered = _dm_larry(
        message=(
            'build_sequence_advancer heartbeat is stale (>10 min). The '
            'daemon is most likely crashed, paused at its activation '
            'gate (OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED), or its '
            'systemd timer is disabled.\n\n'
            f'Reason: {reason}\n'
            f'Heartbeat path: {HEARTBEAT_FILE}'
        ),
        subject='build-sequence-advancer-stale',
        suggested_action=(
            'ssh larry@<droplet> and run: '
            'systemctl status ourliberty-build-sequence-advancer.timer && '
            'systemctl status ourliberty-build-sequence-advancer.service && '
            'journalctl -u ourliberty-build-sequence-advancer.service '
            '--since "30 min ago" | tail -100. '
            'If the unit is masked by the activation gate, this is expected '
            'until you set OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=true '
            'via `sudo systemctl edit '
            'ourliberty-build-sequence-advancer.service`.'
        ),
    )
    log(f'DM dispatched={delivered}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
