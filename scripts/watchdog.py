#!/usr/bin/env python3
"""ourliberty Agents Watchdog — system health monitor with auto-recovery.

Runs every 5 min via systemd timer. Adapted from gm-agent-core for our
4-bot topology (D3.5-prep, 2026-05-12). Previous incarnation referenced
GM-orchestrator and telegram-webhook services that don't exist in our fork.

Checks (9 total):

  1. inbox-watcher service active — auto-restart if down
  2. outbox-notifier service active — auto-restart if down
  3. inbox-watcher process RSS < 1.5 GB — auto-restart if exceeded
     (15 min cooldown; V2 RSS-via-MainPID pattern from upstream)
  4. inbox-watcher cgroup memory — warn at 80% of MemoryMax, critical at
     95%. Catches the spawned-claude-subprocess overrun shape that the
     V2 single-process check misses. No auto-restart (a child-driven
     cgroup spike that restarts the watcher orphans in-flight tasks)
  5. Disk usage — warn at 80%, critical at 90%
  6. System memory — warn at 80%, critical at 90%
  7. inbox_watcher.log freshness (idle-aware: stale log only matters when
     work is queued AND no active workers)
  8. Bot desired-state reconciliation — for each bot in
     `bot-liveness-policy.json`, restart `desired_state=up` bots that are
     down (subject to a per-bot M=3 attempts / 30 min flapping cap),
     suppress the down alert for `desired_state=down` bots, and emit an
     INFO divergence note for `desired_state=down` bots that are alive.
     Subsumes the prior beacon-bot carve-out — beacon is reconciled by
     exactly one mechanism so two paths cannot race to restart it.

Auto-recovery scope: inbox-watcher + outbox-notifier via the original
`_check_auto_restart` path; every policy-declared bot via the desired-
state reconciler (driven by `desired_state` in bot-liveness-policy.json).

Alerts route to Larry's phone via the shared `larry_alerts` queue read by
Beacon's Telegram bot. Local `system-health.json` blackboard summary
preserved for future Pulse digest consumption.

State on disk:
  ~/agents/blackboard/system-health.json    — overall health summary
  ~/agents/state/inbox-watcher-mem-restart-cooldown — V2 restart marker
  ~/agents/state/alert-cooldown/{critical,warning}/<key> — per-subject cooldown

History:
  2026-05-08  Initial adaptation from upstream (paths/services only).
  2026-05-12  Full rewrite for our topology (D3.5-prep).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow `import larry_alerts` from scripts/ directory.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bot_liveness_policy  # noqa: E402
import dispatch_lease  # noqa: E402  # TTL constant for the dispatch-lease suppressor
import larry_alerts  # noqa: E402
import marker_paths  # noqa: E402  # shared restart-coordination marker paths (Medic parity)
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path.home() / 'agents'
BLACKBOARD = AGENTS_ROOT / 'blackboard'
LOG_DIR = AGENTS_ROOT / 'logs'
HEALTH_FILE = BLACKBOARD / 'system-health.json'

# Services watchdog will auto-restart on detect-down. Bots are NOT in
# this list — they are reconciled per the desired-state reconciler below
# (subsumes the prior beacon-bot carve-out so one mechanism owns bot
# restart and the two paths cannot race).
AUTO_RESTART_SERVICES = (
    'ourliberty-inbox-watcher',
    'ourliberty-outbox-notifier',
)

# Desired-state reconciler safety cap: M attempts per WINDOW window.
# Mirrors systemd's StartLimitBurst philosophy at watchdog cadence. On
# exhaustion the reconciler stops actuating and emits a single
# `bot-reconcile-flapping:<bot>` critical alert per window so a true
# crash-loop pages once and stays paged (the regular per-tick down alert
# is suppressed once we've given up).
RECONCILE_MAX_ATTEMPTS = 3
RECONCILE_WINDOW_SEC = 30 * 60

# Service-active states treated as "alive enough — leave it alone".
# Includes 'auto-restart' SubState (M1 fix): systemd has scheduled a
# restart per Restart= policy; watchdog calling `systemctl start` during
# this window pointlessly depletes StartLimitBurst.
ALIVE_ACTIVE_STATES = ('active', 'activating', 'deactivating', 'reloading')

# Auto-restart flap detection. The M1 fix correctly stopped watchdog from
# actuating (`systemctl start`) while systemd is mid-auto-restart — but it did
# so by reporting the service ALIVE, which also suppressed the down-alert. A
# service WEDGED in auto-restart (crash-looping, never converging) therefore
# went completely invisible. We now count consecutive watchdog checks that find
# the service in auto-restart; once the streak hits this threshold, surface a
# critical alert (still WITHOUT actuating — systemd owns the restart). A normal
# restart completes in seconds, so catching the same service in auto-restart on
# >=2 consecutive checks (watchdog cadence is minutes apart) means it is not a
# transient restart.
AUTO_RESTART_FLAP_TICKS = 2
# Per-service consecutive-auto-restart counter dir. Path shape is owned by the
# shared marker_paths module (Medic reads these same markers -- parity).
_FLAP_STREAK_DIR = marker_paths.flap_streak_dir(AGENTS_ROOT)

# inbox-watcher RSS threshold for V2 process-memory check. Restart above
# this. Q2-Dial: 1.5 GB (was 1 GB on upstream's orchestrator; we have
# MemoryMax=2G with 500MB headroom for children).
WATCHER_PROCESS_RSS_THRESHOLD_BYTES = 1_500_000_000

# inbox-watcher cgroup-aggregate thresholds as ratios of MemoryMax read at
# runtime (M4 fix — don't hardcode 2G). Captures children + parent total.
CGROUP_WARN_RATIO = 0.80
CGROUP_CRITICAL_RATIO = 0.95

# Cooldown between V2 process-memory restarts.
PROC_MEM_RESTART_COOLDOWN_SEC = 15 * 60

# Disk + system-memory thresholds.
USAGE_WARN_PCT = 80
USAGE_CRITICAL_PCT = 90


def log(message: str, level: str = 'INFO') -> None:
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = f'[{ts}] [watchdog] [{level}] {message}'
    print(entry)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / 'watchdog.log', 'a') as f:
            f.write(entry + '\n')
    except OSError:
        pass


# ---------- systemd helpers ----------


def _systemctl_states(service_name: str) -> tuple[str | None, str | None]:
    """Return (ActiveState, SubState) for the unit, or (None, None) on error."""
    try:
        r = subprocess.run(
            ['systemctl', 'show', service_name,
             '-p', 'ActiveState', '-p', 'SubState'],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None, None
    active = None
    sub = None
    for line in r.stdout.splitlines():
        if line.startswith('ActiveState='):
            active = line.split('=', 1)[1].strip()
        elif line.startswith('SubState='):
            sub = line.split('=', 1)[1].strip()
    return active, sub


def is_service_alive(service_name: str) -> bool:
    """True if service is in any alive-or-coming-up state.

    Includes the SubState='auto-restart' case (M1 fix) — systemd already
    has a restart scheduled; watchdog intervening hurts more than helps.
    """
    active, sub = _systemctl_states(service_name)
    if active is None:
        return False
    if active in ALIVE_ACTIVE_STATES:
        return True
    if sub == 'auto-restart':
        return True
    return False


def _attempt_start(service_name: str, wait_sec: int = 8) -> bool:
    """Run `sudo -n systemctl start <name>`, wait briefly, return aliveness.

    Watchdog runs as User=larry per the service unit; starting system units
    requires root. larry has NOPASSWD sudo (see reference_droplet) so `-n`
    (non-interactive) succeeds without prompting.
    """
    try:
        subprocess.run(['sudo', '-n', 'systemctl', 'start', service_name],
                       capture_output=True, timeout=30)
    except Exception as e:
        log(f'{service_name} start exception: {e}', 'ERROR')
        return False
    time.sleep(wait_sec)
    return is_service_alive(service_name)


# ---------- checks ----------


def _flap_streak_path(service_name: str) -> Path:
    return _FLAP_STREAK_DIR / service_name


def _bump_flap_streak(service_name: str) -> int:
    """Increment and return the consecutive-auto-restart streak for a service."""
    path = _flap_streak_path(service_name)
    try:
        prior = int(path.read_text().strip())
    except (OSError, ValueError):
        prior = 0
    streak = prior + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(streak))
    except OSError:
        pass
    return streak


def _reset_flap_streak(service_name: str) -> None:
    try:
        _flap_streak_path(service_name).unlink()
    except (FileNotFoundError, OSError):
        pass


def _check_auto_restart(service_name: str, friendly: str) -> dict:
    """Alive? if not, start; if start fails, append critical alert. A service
    wedged in systemd auto-restart is handled separately: no actuation (systemd
    owns the restart), but a sustained streak escalates to a flapping alert so a
    crash-loop isn't silently treated as alive."""
    active, sub = _systemctl_states(service_name)

    # Genuinely up and not mid-restart → clear any streak and we're done.
    if active in ALIVE_ACTIVE_STATES and sub != 'auto-restart':
        _reset_flap_streak(service_name)
        return {'status': 'ok'}

    # systemd is between automatic restarts. Do NOT actuate (the M1 fix —
    # `systemctl start` here just burns StartLimitBurst). But track the streak:
    # a service that keeps reappearing in auto-restart is crash-looping.
    if sub == 'auto-restart':
        streak = _bump_flap_streak(service_name)
        if streak >= AUTO_RESTART_FLAP_TICKS:
            log(f'{friendly} ({service_name}) wedged in systemd auto-restart '
                f'across {streak} consecutive checks — crash-looping', 'ERROR')
            larry_alerts.append_alert(
                source='watchdog',
                severity='critical',
                subject=f'{service_name}-flapping',
                message=(f'{friendly} is crash-looping: systemd has held it in '
                         f'auto-restart across {streak} consecutive watchdog '
                         f'checks. Automatic restart is not converging.'),
                suggested_action=(
                    f'sudo systemctl status {service_name} && '
                    f'sudo journalctl -u {service_name} -n 80'
                ),
            )
            return {'status': 'flapping', 'streak': streak}
        log(f'{friendly} ({service_name}) in systemd auto-restart '
            f'(streak {streak}/{AUTO_RESTART_FLAP_TICKS}) — deferring to systemd',
            'WARN')
        return {'status': 'auto-restart-wait', 'streak': streak}

    # active is None (systemctl error) or a dead state (failed/inactive) →
    # genuinely down; watchdog actuates.
    if active is None:
        # Unknown state (transient systemctl error): do NOT reset the streak —
        # resetting on an uncertain reading would let a flapping service evade
        # detection by interleaving error ticks.
        log(f'{friendly} ({service_name}) state unknown (systemctl query failed) '
            f'— attempting start', 'WARN')
    else:
        # Confirmed not in auto-restart → this breaks the consecutive-auto-restart
        # streak (the flap counter only counts back-to-back auto-restart ticks).
        _reset_flap_streak(service_name)
        log(f'{friendly} ({service_name}) is DOWN (state={active}) — attempting '
            f'start', 'WARN')
    if _attempt_start(service_name):
        log(f'{friendly} recovered')
        _reset_flap_streak(service_name)
        # Surface the recovery event so Larry knows watchdog acted.
        larry_alerts.append_alert(
            source='watchdog',
            severity='info',
            subject=service_name,
            message=f'{friendly} was down; watchdog auto-restarted it.',
        )
        return {'status': 'recovered', 'action': 'started'}
    log(f'{friendly} start FAILED — appending critical alert', 'ERROR')
    larry_alerts.append_alert(
        source='watchdog',
        severity='critical',
        subject=service_name,
        message=f'{friendly} is DOWN and auto-restart failed.',
        suggested_action=(
            f'sudo systemctl status {service_name} && '
            f'sudo journalctl -u {service_name} -n 50'
        ),
    )
    return {'status': 'down', 'action': 'start_failed'}


def check_inbox_watcher() -> dict:
    return _check_auto_restart('ourliberty-inbox-watcher', 'inbox-watcher')


def check_outbox_notifier() -> dict:
    return _check_auto_restart('ourliberty-outbox-notifier', 'outbox-notifier')


def _inbox_watcher_main_pid() -> int | None:
    try:
        r = subprocess.run(
            ['systemctl', 'show', 'ourliberty-inbox-watcher.service',
             '-p', 'MainPID', '--value'],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    pid_raw = r.stdout.strip()
    if not pid_raw or not pid_raw.isdigit() or pid_raw == '0':
        return None
    return int(pid_raw)


def _read_process_rss(pid: int) -> int | None:
    """RSS in bytes for a PID, or None if unavailable."""
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _task_in_flight(task_id: str) -> bool:
    """True if an in-flight marker exists for task_id AND its pid is alive.

    Correlates an inbox task to ~/agents/state/in-flight/<task_id>.json. A
    marker whose pid is dead does NOT count as in-flight, so a build whose
    process died still reads as queued work (the stale-log WARN can fire).
    """
    if not task_id:
        return False
    marker = AGENTS_ROOT / 'state' / 'in-flight' / f'{task_id}.json'
    try:
        with open(marker) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    pid = data.get('pid')
    if not isinstance(pid, int):
        return False
    return _pid_alive(pid)


def _any_live_in_flight_session() -> bool:
    """True if any in-flight marker has an int pid that is currently alive.

    A live in-flight pid is a direct proxy for the single inbox_watcher process
    being blocked mid-session (it polls a spawned Claude session and writes zero
    log lines for its whole duration), so a quiet watcher log is expected, not a
    stall. Globbing errors or unparseable markers are skipped conservatively.
    """
    in_flight = AGENTS_ROOT / 'state' / 'in-flight'
    try:
        markers = list(in_flight.glob('*.json'))
    except OSError:
        return False
    for marker in markers:
        try:
            with open(marker) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        pid = data.get('pid')
        if isinstance(pid, int) and _pid_alive(pid):
            return True
    return False


# inbox_watcher runs one thread per agent (scripts/inbox_watcher.py AGENTS),
# each holding a per-agent `inbox:<agent>` dispatch lease for the whole duration
# of the Claude session it spawns. Mirror is the case this list exists for, but
# any agent's held lease is an equally valid "watcher mid-session" proxy.
_INBOX_LEASE_AGENTS = ('beacon', 'forge', 'mirror', 'pulse')


def _any_live_dispatch_lease() -> bool:
    """True if any inbox dispatch lease is held by a live holder right now.

    A held ``inbox:<agent>`` lease is the authoritative proxy for the watcher's
    per-agent thread being blocked inside ``run_claude`` for the whole duration
    of a spawned Claude session (it polls the session and writes zero log lines).
    It is strictly more reliable than the in-flight marker for the Mirror-review
    case: a Mirror review reuses the Forge build's ``task_id`` as its
    ``task_stem`` (outbox_notifier._dispatch_mirror_review), so both write the
    SAME ``state/in-flight/<stem>.json`` and the build's
    ``agent_runner._unregister_in_flight`` (keyed on stem only) can delete the
    review's marker while the review is still live — leaving
    ``_any_live_in_flight_session`` blind to the active review
    (watchdog-mirror-active-stale-suppression-001). The per-agent lease cannot be
    clobbered that way: it is keyed by agent, renewed on a 60s heartbeat, and
    only counts when it is within TTL AND its holder pid is alive.

    Read directly from ``AGENTS_ROOT`` (not via ``dispatch_lease.is_held``, whose
    LEASES_DIR is fixed at import) so the suppressor stays scoped to the same
    state root the rest of these checks use — which also keeps it test-isolated.
    Conservative: an unreadable/half-written lease is skipped, never counted. In
    lease 'off' mode no lease files exist and this is always False, degrading
    cleanly to the in-flight-marker signal alone.
    """
    leases_dir = AGENTS_ROOT / 'state' / 'dispatch-leases'
    for agent in _INBOX_LEASE_AGENTS:
        try:
            with open(leases_dir / f'inbox:{agent}.lease') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        try:
            age = time.time() - float(data.get('timestamp_renewed', 0))
        except (TypeError, ValueError):
            continue
        if age >= dispatch_lease.TTL_SECONDS:
            continue  # stale lease — holder died without releasing
        pid = data.get('holder_pid')
        if isinstance(pid, int) and _pid_alive(pid):
            return True
    return False


def _inbox_has_queued_task(agent_dir: Path) -> bool:
    """True if the inbox holds at least one *.json task that is NOT in-flight.

    Reads each envelope's task_id field (not its filename) and treats a task
    as live queued work unless its task is in-flight with a live pid. A file
    we cannot parse is conservatively counted as queued.
    """
    for env in agent_dir.glob('*.json'):
        # Outbox-notifier retry/terminal dead-letters are not freshly-dispatched
        # work a healthy watcher should be picking up — skip them so a lingering
        # one doesn't read as a stalled queue.
        if env.name.startswith('marker-error-'):
            continue
        try:
            with open(env) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return True  # unparseable envelope counts as queued work
        task_id = data.get('task_id')
        if not _task_in_flight(task_id):
            return True
    return False


def check_inbox_watcher_memory() -> dict:
    """V2 process-RSS check. Adapted verbatim from upstream's V2 logic;
    swaps the service name + cooldown marker. Restart above threshold."""
    main_pid = _inbox_watcher_main_pid()
    if main_pid is None:
        return {'status': 'unavailable', 'reason': 'MainPID unresolvable'}
    rss_bytes = _read_process_rss(main_pid)
    if rss_bytes is None:
        return {'status': 'unavailable', 'reason': f'VmRSS unreadable for pid {main_pid}'}

    cooldown_marker = marker_paths.mem_restart_cooldown_path(AGENTS_ROOT, 'inbox-watcher')
    try:
        cooldown_marker.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if cooldown_marker.exists():
        age = time.time() - cooldown_marker.stat().st_mtime
        if age < PROC_MEM_RESTART_COOLDOWN_SEC:
            return {
                'status': 'cooldown',
                'rss_mb': round(rss_bytes / 1e6, 1),
                'reason': f'last restart {int(age)}s ago',
            }

    if rss_bytes > WATCHER_PROCESS_RSS_THRESHOLD_BYTES:
        log(
            f'inbox-watcher RSS {rss_bytes/1e9:.2f}G '
            f'> {WATCHER_PROCESS_RSS_THRESHOLD_BYTES/1e9:.2f}G threshold '
            f'— triggering graceful restart',
            'WARN',
        )
        try:
            # Requires root for write operation — see _attempt_start docstring.
            subprocess.run(
                ['sudo', '-n', 'systemctl', 'restart', 'ourliberty-inbox-watcher.service'],
                capture_output=True, timeout=180,
            )
        except Exception as e:
            log(f'inbox-watcher mem-restart exception: {e}', 'ERROR')
            return {'status': 'restart_failed', 'error': str(e), 'rss_mb': round(rss_bytes / 1e6, 1)}
        try:
            cooldown_marker.touch()
        except OSError:
            pass
        time.sleep(5)
        larry_alerts.append_alert(
            source='watchdog',
            severity='critical',
            subject='inbox-watcher-process-memory',
            message=(
                f'inbox-watcher process RSS {rss_bytes/1e9:.2f} GB exceeded '
                f'{WATCHER_PROCESS_RSS_THRESHOLD_BYTES/1e9:.2f} GB threshold — restarted.'
            ),
        )
        return {
            'status': 'restarted',
            'rss_mb': round(rss_bytes / 1e6, 1),
            'threshold_mb': WATCHER_PROCESS_RSS_THRESHOLD_BYTES // (1024 * 1024),
        }
    return {'status': 'ok', 'rss_mb': round(rss_bytes / 1e6, 1)}


def _read_memory_max() -> int | None:
    """Resolve MemoryMax from systemctl. None if unset/infinity (M4 fix)."""
    try:
        r = subprocess.run(
            ['systemctl', 'show', 'ourliberty-inbox-watcher.service',
             '-p', 'MemoryMax', '--value'],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    raw = r.stdout.strip()
    if not raw or raw.lower() in ('infinity', '[not set]'):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def check_inbox_watcher_cgroup() -> dict:
    """Cgroup-aggregate memory pressure (Q6 — catches subprocess overrun).

    Alert-only (no auto-restart): a child-driven cgroup spike that restarts
    the watcher would orphan in-flight tasks. The V2 check handles parent
    bloat; this check handles total pressure.
    """
    memory_max = _read_memory_max()
    if memory_max is None:
        return {'status': 'unavailable', 'reason': 'MemoryMax unset or infinity'}
    cgroup_path = '/sys/fs/cgroup/system.slice/ourliberty-inbox-watcher.service/memory.current'
    try:
        with open(cgroup_path) as f:
            current_bytes = int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError) as e:
        return {'status': 'unavailable', 'reason': f'memory.current read failed: {e}'}

    ratio = current_bytes / memory_max if memory_max else 0.0
    parent_pid = _inbox_watcher_main_pid()
    parent_rss = _read_process_rss(parent_pid) if parent_pid else None
    children_bytes = max(0, current_bytes - (parent_rss or 0))
    payload: dict = {
        'current_gb': round(current_bytes / 1e9, 2),
        'memory_max_gb': round(memory_max / 1e9, 2),
        'ratio': round(ratio, 3),
        'parent_rss_mb': round(parent_rss / 1e6, 1) if parent_rss else None,
        'children_mb': round(children_bytes / 1e6, 1),
    }

    if ratio >= CGROUP_CRITICAL_RATIO:
        log(
            f'inbox-watcher cgroup CRITICAL: {ratio*100:.0f}% of MemoryMax '
            f'({current_bytes/1e9:.2f}G / {memory_max/1e9:.2f}G)',
            'ERROR',
        )
        larry_alerts.append_alert(
            source='watchdog',
            severity='critical',
            subject='inbox-watcher-cgroup',
            message=(
                f'inbox-watcher cgroup memory CRITICAL: '
                f'{current_bytes/1e9:.2f} GB / {memory_max/1e9:.2f} GB '
                f'({ratio*100:.0f}%). Parent process RSS: '
                f'{payload["parent_rss_mb"] or "?"} MB, '
                f'children ~{payload["children_mb"]:.0f} MB.'
            ),
            suggested_action=(
                'ls ~/agents/state/in-flight/ && '
                'sudo journalctl -u ourliberty-inbox-watcher -n 100'
            ),
        )
        payload['status'] = 'critical'
    elif ratio >= CGROUP_WARN_RATIO:
        log(f'inbox-watcher cgroup at {ratio*100:.0f}% of MemoryMax', 'WARN')
        larry_alerts.append_alert(
            source='watchdog',
            severity='warning',
            subject='inbox-watcher-cgroup',
            message=(
                f'inbox-watcher cgroup memory warning: '
                f'{current_bytes/1e9:.2f} GB / {memory_max/1e9:.2f} GB '
                f'({ratio*100:.0f}%). Parent process RSS: '
                f'{payload["parent_rss_mb"] or "?"} MB, '
                f'children ~{payload["children_mb"]:.0f} MB.'
            ),
        )
        payload['status'] = 'warning'
    else:
        payload['status'] = 'ok'
    return payload


def check_disk() -> dict:
    try:
        stat = os.statvfs('/')
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used_pct = int((1 - free / total) * 100)
    except Exception as e:
        return {'status': 'error', 'error': str(e)}
    if used_pct > USAGE_CRITICAL_PCT:
        log(f'Disk usage CRITICAL: {used_pct}%', 'ERROR')
        larry_alerts.append_alert(
            source='watchdog', severity='critical', subject='disk',
            message=f'Disk usage CRITICAL at {used_pct}%.',
            suggested_action='df -h && du -sh ~/agents/logs ~/agent-worktrees',
        )
        return {'status': 'critical', 'percent': used_pct}
    if used_pct > USAGE_WARN_PCT:
        log(f'Disk usage warning: {used_pct}%', 'WARN')
        larry_alerts.append_alert(
            source='watchdog', severity='warning', subject='disk',
            message=f'Disk usage warning at {used_pct}%.',
        )
        return {'status': 'warning', 'percent': used_pct}
    return {'status': 'ok', 'percent': used_pct}


def check_memory() -> dict:
    try:
        mem: dict = {}
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = mem.get('MemTotal', 1)
        available = mem.get('MemAvailable', total)
        used_pct = int((1 - available / total) * 100)
    except Exception as e:
        return {'status': 'error', 'error': str(e)}
    if used_pct > USAGE_CRITICAL_PCT:
        log(f'System memory CRITICAL: {used_pct}%', 'ERROR')
        larry_alerts.append_alert(
            source='watchdog', severity='critical', subject='system-memory',
            message=f'System memory CRITICAL at {used_pct}%.',
        )
        return {'status': 'critical', 'percent': used_pct}
    if used_pct > USAGE_WARN_PCT:
        log(f'System memory warning: {used_pct}%', 'WARN')
        larry_alerts.append_alert(
            source='watchdog', severity='warning', subject='system-memory',
            message=f'System memory warning at {used_pct}%.',
        )
        return {'status': 'warning', 'percent': used_pct}
    return {'status': 'ok', 'percent': used_pct}


def check_log_growth() -> dict:
    """inbox_watcher.log freshness. Idle-aware: under 5 min always ok;
    over 5 min only flag if work is queued AND watcher service is down,
    or if log is stale > 12h regardless of inboxes.
    """
    log_file = LOG_DIR / 'inbox_watcher.log'
    if not log_file.exists():
        return {'status': 'warning', 'reason': 'inbox_watcher.log not found'}
    try:
        age = time.time() - log_file.stat().st_mtime
    except OSError as e:
        return {'status': 'error', 'error': str(e)}
    if age <= 300:
        return {'status': 'ok', 'seconds_since_write': int(age)}

    watcher_alive = is_service_alive('ourliberty-inbox-watcher.service')

    if not watcher_alive:
        return {
            'status': 'critical',
            'seconds_since_write': int(age),
            'reason': 'inbox-watcher service not active',
        }

    # The single inbox_watcher process blocks in its worker-poll loop for the
    # whole duration of every spawned Claude session, writing zero log lines, so
    # a quiet log during a live session is expected. A live in-flight pid is the
    # canonical proxy for that — suppress globally before scanning inboxes.
    if _any_live_in_flight_session():
        return {
            'status': 'ok',
            'seconds_since_write': int(age),
            'reason': 'active agent session (watcher blocked, quiet log expected)',
        }

    # The in-flight marker is unreliable for Mirror reviews: the review reuses
    # the Forge build's task_id as its task_stem, so the build's
    # _unregister_in_flight can clobber the review's marker mid-review (see
    # _any_live_dispatch_lease). The per-agent dispatch lease is the
    # clobber-immune fallback — a held+live lease means the watcher is blocked in
    # a session right now, so a quiet log is expected. Extends PR #694's
    # session-aware suppression to the Mirror-active case
    # (watchdog-mirror-active-stale-suppression-001).
    if _any_live_dispatch_lease():
        return {
            'status': 'ok',
            'seconds_since_write': int(age),
            'reason': 'active dispatch lease (watcher mid-session, quiet log expected)',
        }

    # Are any inboxes holding queued (not in-flight) work? An inbox task that
    # is actively being built keeps its envelope here for the whole build, so
    # an in-flight-with-live-pid task is excluded — otherwise a multi-minute
    # build (log naturally quiet > 5 min) mis-reads as a stalled queue.
    live_files = 0
    any_inflight = False
    inboxes_root = AGENTS_ROOT / 'inboxes'
    try:
        for agent_dir in inboxes_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith(('.', '_')):
                continue
            try:
                if _inbox_has_queued_task(agent_dir):
                    live_files += 1
                elif any(agent_dir.glob('*.json')):
                    any_inflight = True  # non-empty, but all tasks in-flight
            except OSError:
                pass
    except OSError:
        live_files = -1

    if live_files == 0 and age <= 43200:
        reason = (
            'work in progress (active build, watcher healthy)'
            if any_inflight
            else 'idle (empty inboxes, watcher healthy)'
        )
        return {
            'status': 'ok',
            'seconds_since_write': int(age),
            'reason': reason,
        }
    if age > 43200:
        return {
            'status': 'warning',
            'seconds_since_write': int(age),
            'reason': 'idle >12h',
        }
    log(
        f'Watcher log stale {int(age)}s with {live_files} non-empty inbox(es)',
        'WARN',
    )
    return {
        'status': 'warning',
        'seconds_since_write': int(age),
        'queued_inboxes': live_files,
    }


# ---------- bot desired-state reconciler ----------


def _reconcile_marker_path(short: str) -> Path:
    return marker_paths.reconcile_marker_path(AGENTS_ROOT, short)


def _read_reconcile_marker(short: str, now: float) -> dict:
    """Return rolling-window state for `short`: window_start, count, paged.

    Window auto-resets when current time is past `window_start + WINDOW`,
    so a bot that recovered and re-failed gets a fresh attempt budget.
    Malformed markers reset to a fresh window — better than tripping the
    cap from corrupt JSON.
    """
    path = _reconcile_marker_path(short)
    fresh = {'window_start': now, 'count': 0, 'paged': False}
    if not path.exists():
        return fresh
    try:
        data = json.loads(path.read_text())
        window_start = float(data['window_start'])
        count = int(data['count'])
        paged = bool(data.get('paged', False))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return fresh
    if now - window_start > RECONCILE_WINDOW_SEC:
        return fresh
    return {'window_start': window_start, 'count': count, 'paged': paged}


def _write_reconcile_marker(short: str, state: dict) -> None:
    path = _reconcile_marker_path(short)
    try:
        atomic_write_json(path, state)
    except OSError:
        pass


def _attempt_restart(unit: str, wait_sec: int = 5) -> bool:
    """Run `sudo -n systemctl restart <unit>`, wait briefly, return aliveness.

    Mirrors `_attempt_start` but uses `restart` because the reconciler is
    invoked for both freshly-down units (where start/restart are equivalent)
    and units that may have residual failed state needing reset.
    """
    try:
        subprocess.run(['sudo', '-n', 'systemctl', 'restart', unit],
                       capture_output=True, timeout=30)
    except Exception as e:
        log(f'{unit} restart exception: {e}', 'ERROR')
        return False
    time.sleep(wait_sec)
    return is_service_alive(unit)


def reconcile_bot_desired_state() -> dict:
    """Reconcile each policy-declared bot toward its `desired_state`.

    For each bot in `config/bot-liveness-policy.json`:
      - `up`+alive   -> no-op; reset the flapping window (clean recovery).
      - `up`+down    -> `sudo -n systemctl restart <unit>` (subject to
                        M=3 attempts / 30 min cap). Emits `bots:<bot>:down`
                        critical while still attempting; switches to
                        `bot-reconcile-flapping:<bot>` once the cap is
                        hit, and suppresses the per-tick down alert from
                        that point until the window resets.
      - `down`+down  -> no-op; alert suppressed (intended state must not page).
      - `down`+alive -> leave it (reconciler restores availability, it does
                        not enforce shutdown). Emits one INFO log line
                        about the intent/actual divergence — surfaced in
                        watchdog.log rather than as a Larry-paging alert
                        because nothing is broken.

    SubState='auto-restart' is treated as alive via `is_service_alive`
    so we never race systemd's own pending restart (M1 fix preserved).

    Subsumes the prior beacon-bot carve-out: beacon is restarted by
    exactly one mechanism (this reconciler), removing the double-restart
    race with the bespoke `check_beacon_bot` carve-out.
    """
    policy = bot_liveness_policy.load_policy()
    now = time.time()
    results: dict = {}
    for short in bot_liveness_policy.policy_agents(policy):
        intent = bot_liveness_policy.desired_state(short, policy=policy)
        unit = bot_liveness_policy.systemd_unit(short, policy=policy)
        alive = is_service_alive(unit)
        entry: dict = {'desired': intent, 'alive': alive, 'unit': unit}

        if intent == bot_liveness_policy.DESIRED_UP and alive:
            entry['action'] = 'noop'
            marker = _reconcile_marker_path(short)
            if marker.exists():
                try:
                    marker.unlink()
                except OSError:
                    pass
            results[short] = entry
            continue

        if intent == bot_liveness_policy.DESIRED_DOWN and not alive:
            log(f'{unit} is down per desired_state=down — alert suppressed', 'INFO')
            entry['action'] = 'suppressed'
            results[short] = entry
            continue

        if intent == bot_liveness_policy.DESIRED_DOWN and alive:
            log(
                f'{unit} is alive but desired_state=down — reconciler does not '
                f'enforce shutdown; leave running',
                'INFO',
            )
            entry['action'] = 'divergence'
            results[short] = entry
            continue

        # intent == up, alive == False: attempt recovery within window.
        state = _read_reconcile_marker(short, now)
        if state['count'] >= RECONCILE_MAX_ATTEMPTS:
            entry['action'] = 'flapping'
            entry['attempts'] = state['count']
            if not state['paged']:
                log(
                    f'{unit} reconcile flapping: {state["count"]} restart attempts '
                    f'in {int((now - state["window_start"])/60)}m — halting actuation, paging',
                    'ERROR',
                )
                larry_alerts.append_alert(
                    source='watchdog',
                    severity='critical',
                    subject=f'bot-reconcile-flapping:{short}',
                    message=(
                        f'{unit} crash-looping: reconciler attempted '
                        f'{state["count"]} restarts in '
                        f'{RECONCILE_WINDOW_SEC // 60} min and gave up. '
                        f'Further restarts halted until window resets.'
                    ),
                    suggested_action=(
                        f'sudo systemctl status {unit} && '
                        f'sudo journalctl -u {unit} -n 100'
                    ),
                )
                state['paged'] = True
                _write_reconcile_marker(short, state)
            results[short] = entry
            continue

        # Within budget: attempt restart, increment counter.
        state['count'] += 1
        _write_reconcile_marker(short, state)
        log(
            f'{unit} is DOWN with desired_state=up — restart attempt '
            f'{state["count"]}/{RECONCILE_MAX_ATTEMPTS}',
            'WARN',
        )
        recovered = _attempt_restart(unit)
        entry['action'] = 'restarted' if recovered else 'restart_failed'
        entry['attempts'] = state['count']
        if recovered:
            log(f'{unit} recovered via reconciler', 'INFO')
            larry_alerts.append_alert(
                source='watchdog',
                severity='warning',
                subject=f'bots:{short}:recovered',
                message=(
                    f'{unit} was down; reconciler restarted it '
                    f'(attempt {state["count"]}/{RECONCILE_MAX_ATTEMPTS}).'
                ),
            )
        else:
            larry_alerts.append_alert(
                source='watchdog',
                severity='critical',
                subject=f'bots:{short}:down',
                message=(
                    f'{unit} is DOWN; reconciler restart attempt '
                    f'{state["count"]}/{RECONCILE_MAX_ATTEMPTS} failed.'
                ),
                suggested_action=f'sudo systemctl restart {unit}',
            )
        results[short] = entry

    statuses = [e.get('action') for e in results.values()]
    if any(s in ('flapping', 'restart_failed') for s in statuses):
        overall = 'critical'
    elif any(s == 'restarted' for s in statuses):
        overall = 'recovered'
    else:
        overall = 'ok'
    return {'status': overall, 'bots': results}


# ---------- orchestration ----------


def run_all_checks() -> dict:
    """Run every check, persist summary to system-health.json."""
    results = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'checks': {
            'inbox_watcher': check_inbox_watcher(),
            'outbox_notifier': check_outbox_notifier(),
            'inbox_watcher_memory': check_inbox_watcher_memory(),
            'inbox_watcher_cgroup': check_inbox_watcher_cgroup(),
            'disk': check_disk(),
            'memory': check_memory(),
            'log_growth': check_log_growth(),
            'bots': reconcile_bot_desired_state(),
        },
    }
    statuses = [c.get('status', 'ok') for c in results['checks'].values()]
    if any(s in ('critical', 'down', 'restarted') for s in statuses):
        results['overall'] = 'critical'
    elif any(s in ('warning', 'error', 'recovered') for s in statuses):
        results['overall'] = 'warning'
    else:
        results['overall'] = 'healthy'

    try:
        atomic_write_json(HEALTH_FILE, results, indent=2)
    except OSError as e:
        log(f'health-file write failed: {e}', 'ERROR')

    log(f'Watchdog complete: overall={results["overall"]}')
    return results


if __name__ == '__main__':
    results = run_all_checks()
    print(json.dumps(results, indent=2))
    sys.exit(0 if results['overall'] != 'critical' else 1)
