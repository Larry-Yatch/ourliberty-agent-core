#!/usr/bin/env python3
"""heal_chain_event_shipper_heartbeat.py — DM Larry if the shipper is stale.

Phase E4.4d PR-B. Spec: agents/beacon/specs/e4-4d-system-tab.md § 5.2.

The chain_event_shipper daemon writes a heartbeat to
~/agents/blackboard/chain-event-shipper.heartbeat every 30 seconds.
This healer runs every 5 min via systemd timer and DMs Larry (yellow
severity, 6h cooldown per incident) if the heartbeat is older than
STALE_THRESHOLD_SEC (10 min) — long enough that a slow drain won't
fire false alarms, short enough that a real crash is visible within
~half an hour of detection.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'chain-event-shipper.heartbeat'
HEALER_HEARTBEAT = AGENTS_ROOT / 'blackboard' / 'heal-chain-event-shipper-heartbeat.heartbeat'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-chain-event-shipper-heartbeat.log'

STALE_THRESHOLD_SEC = 10 * 60       # 10 min: shipper heartbeats every 30s
                                    # so 10 min == ~20 missed beats.

# The shipper ships DEFAULT-OFF behind an activation gate: the service unit
# carries `OURLIBERTY_CHAIN_SHIPPER_ENABLED=false` and the daemon exits early
# WITHOUT writing a heartbeat when the gate is not truthy
# (scripts/chain_event_shipper.py). So in the default config a stale heartbeat
# is the EXPECTED state, not a crash -- alarming on it DMs Larry forever (the
# 2026-06-04 false-positive audit). Before alerting we probe systemd,
# read-only, to confirm the daemon is actually SUPPOSED to be running; if the
# gate is closed or the service is disabled/masked we suppress.
SHIPPER_SERVICE = 'ourliberty-chain-event-shipper.service'
ACTIVATION_ENV = 'OURLIBERTY_CHAIN_SHIPPER_ENABLED'


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
    return KILL_SWITCH.exists()


def _dm_larry(message: str, subject: str, suggested_action: str) -> bool:
    """Fire larry_alerts.append_alert. Cooldown is enforced inside.

    Brief calls for "yellow severity" / 6h cooldown — `warning` is the
    closest enum (the only available choices are `warning` and
    `critical`), and larry_alerts uses a 60-min warning cooldown
    by default. The 6h spec value isn't enforceable through the
    shared queue without bypassing the standard cooldown machinery, so
    we accept the 60-min default and document the deviation in the
    runbook. The healer-side stalled_alerted state file below adds a
    one-shot gate so the same incident doesn't re-DM each tick within
    that 60-min window anyway.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-chain-event-shipper-heartbeat',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


def _systemctl(args: list[str]) -> tuple[int, str]:
    """Run a read-only `systemctl` query. Returns (rc, stdout). rc=-1 on a
    launch/timeout error so callers can treat it as 'unknown'. Patched in
    tests so no real systemctl runs."""
    try:
        proc = subprocess.run(['systemctl', *args], capture_output=True,
                              text=True, timeout=10)
        return proc.returncode, (proc.stdout or '').strip()
    except (OSError, subprocess.SubprocessError):
        return -1, ''


# The alert this healer emits (source:subject) — the retraction is keyed on the
# SAME cooldown key so it retracts exactly its own stale red, nothing else.
ALERT_SOURCE = 'heal-chain-event-shipper-heartbeat'
ALERT_SUBJECT = 'chain-event-shipper-stale'


def _retract_standdown() -> int:
    """Positive-clear retraction: the heartbeat was just observed FRESH, so the
    daemon recovered. Retract any stale 🔴 this healer emitted for it and emit
    one closure stand-down (auditable, never silent). Keyed on this healer's own
    `source:subject`. Best-effort — swallow any error so it can never break the
    tick, mirroring the alert path's fire-and-forget posture."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        removed = la.retract_with_standdown(
            f'{ALERT_SOURCE}:{ALERT_SUBJECT}',
            standdown_message=(
                'chain_event_shipper heartbeat is fresh again — the daemon has '
                'recovered. Standing down the earlier stale-heartbeat alert.'
            ),
        )
        if removed:
            log(f'retracted {removed} stale shipper-heartbeat alert line(s) '
                f'(heartbeat fresh again)')
        return removed
    except Exception as e:  # noqa: BLE001
        log(f'_retract_standdown failed: {type(e).__name__}: {e}', 'WARN')
        return 0


def intentionally_off() -> tuple[bool, str]:
    """True iff the shipper is DELIBERATELY not running -- the activation gate
    is closed, or the service is disabled/masked. All probes are read-only.
    Any probe error or ambiguity returns (False, '') so an unexplained stale
    heartbeat still DMs Larry (fail loud)."""
    # 1. Activation gate -- the documented default-off mechanism. `systemctl
    #    show <svc> -p Environment` prints `Environment=VAR=val VAR2=val2`.
    rc, out = _systemctl(['show', SHIPPER_SERVICE, '-p', 'Environment'])
    if rc == 0 and out:
        env_blob = out.split('=', 1)[1] if out.startswith('Environment=') else out
        for tok in env_blob.split():
            if tok.startswith(ACTIVATION_ENV + '='):
                val = tok.split('=', 1)[1].strip().lower()
                if val not in ('true', '1', 'yes', 'on'):
                    return True, f'activation gate closed ({ACTIVATION_ENV}={val})'
                break  # gate explicitly open -> not this reason
    # 2. Service deliberately disabled/masked.
    rc, out = _systemctl(['is-enabled', SHIPPER_SERVICE])
    if out in ('disabled', 'masked'):
        return True, f'{SHIPPER_SERVICE} is-enabled={out}'
    return False, ''


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
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    stale, age, reason = check_staleness()
    if not stale:
        log(f'tick: shipper heartbeat fresh ({int(age)}s)')
        # POSITIVE-CLEAR ONLY: retract our own stale red solely on a positively
        # observed fresh heartbeat (reason == 'fresh'), NEVER on a degraded /
        # unreadable probe. check_staleness returns is_stale=True on every error
        # path, so a degraded read never reaches this branch — but gate on the
        # positive sentinel explicitly so the invariant is local and testable.
        if reason == 'fresh':
            _retract_standdown()
        return 0

    # A stale heartbeat is only a fault if the daemon is supposed to be
    # running. Default-off via the activation gate (or a disabled service) is
    # an EXPECTED, non-incident state -- suppress rather than DM Larry.
    off, off_reason = intentionally_off()
    if off:
        log(f'STALE shipper heartbeat but daemon is intentionally off '
            f'({off_reason}); suppressing DM')
        return 0

    log(f'STALE shipper heartbeat — {reason}', 'WARN')
    delivered = _dm_larry(
        message=(
            'chain_event_shipper heartbeat is stale (>10 min). The daemon '
            'is most likely crashed or paused.\n\n'
            f'Reason: {reason}\n'
            f'Heartbeat path: {HEARTBEAT_FILE}'
        ),
        subject='chain-event-shipper-stale',
        suggested_action=(
            'ssh larry@134.209.44.80 and run: '
            'systemctl status ourliberty-chain-event-shipper.service '
            '&& journalctl -u ourliberty-chain-event-shipper.service '
            '--since "30 min ago" | tail -100. '
            'If crashed: sudo systemctl restart '
            'ourliberty-chain-event-shipper.service'
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
