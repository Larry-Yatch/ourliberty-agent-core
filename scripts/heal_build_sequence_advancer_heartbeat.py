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
import subprocess
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

# The advancer ships DEFAULT-OFF behind an activation gate: the service unit
# carries `OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=false` and the daemon
# exits at the top of each tick WITHOUT writing a heartbeat when the gate is
# not truthy (scripts/build_sequence_advancer.py). So in the default config a
# stale heartbeat is the EXPECTED state, not a crash -- alarming on it DMs
# Larry forever (the 2026-06-04 false-positive audit). Before alerting we
# probe systemd, read-only, to confirm the daemon is actually SUPPOSED to be
# running; if the gate is closed or the timer is disabled/masked we suppress.
ADVANCER_SERVICE = 'ourliberty-build-sequence-advancer.service'
ADVANCER_TIMER = 'ourliberty-build-sequence-advancer.timer'
ACTIVATION_ENV = 'OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED'

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
ALERT_SOURCE = 'heal-build-sequence-advancer-heartbeat'
ALERT_SUBJECT = 'build-sequence-advancer-stale'


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
                'build_sequence_advancer heartbeat is fresh again — the daemon '
                'has recovered. Standing down the earlier stale-heartbeat alert.'
            ),
        )
        if removed:
            log(f'retracted {removed} stale advancer-heartbeat alert line(s) '
                f'(heartbeat fresh again)')
        return removed
    except Exception as e:  # noqa: BLE001
        log(f'_retract_standdown failed: {type(e).__name__}: {e}', 'WARN')
        return 0


def intentionally_off() -> tuple[bool, str]:
    """True iff the advancer is DELIBERATELY not running -- the activation
    gate is closed, or the timer is disabled/masked. All probes are
    read-only. Any probe error or ambiguity returns (False, '') so an
    unexplained stale heartbeat still DMs Larry (fail loud)."""
    # 1. Activation gate -- the documented default-off mechanism. `systemctl
    #    show <svc> -p Environment` prints `Environment=VAR=val VAR2=val2`.
    rc, out = _systemctl(['show', ADVANCER_SERVICE, '-p', 'Environment'])
    if rc == 0 and out:
        env_blob = out.split('=', 1)[1] if out.startswith('Environment=') else out
        for tok in env_blob.split():
            if tok.startswith(ACTIVATION_ENV + '='):
                val = tok.split('=', 1)[1].strip().lower()
                if val not in ('true', '1', 'yes', 'on'):
                    return True, f'activation gate closed ({ACTIVATION_ENV}={val})'
                break  # gate explicitly open -> not this reason
    # 2. Timer deliberately disabled/masked.
    rc, out = _systemctl(['is-enabled', ADVANCER_TIMER])
    if out in ('disabled', 'masked'):
        return True, f'{ADVANCER_TIMER} is-enabled={out}'
    return False, ''


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
        # POSITIVE-CLEAR ONLY: retract our own stale red solely on a positively
        # observed fresh heartbeat (reason == 'fresh'), NEVER on a degraded /
        # unreadable probe. check_staleness returns is_stale=True on every error
        # path, so a degraded read never reaches this branch — but gate on the
        # positive sentinel explicitly so the invariant is local and testable.
        if reason == 'fresh':
            _retract_standdown()
        return 0

    # A stale heartbeat is only a fault if the daemon is supposed to be
    # running. Default-off via the activation gate (or a disabled timer) is an
    # EXPECTED, non-incident state -- suppress rather than DM Larry.
    off, off_reason = intentionally_off()
    if off:
        log(f'STALE advancer heartbeat but daemon is intentionally off '
            f'({off_reason}); suppressing DM')
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
