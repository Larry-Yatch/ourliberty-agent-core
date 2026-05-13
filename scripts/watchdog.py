#!/usr/bin/env python3
"""ourliberty Agents Watchdog — system health monitor with auto-recovery.

Runs every 5 min via systemd timer. Adapted from gm-agent-core for our
4-bot topology (D3.5-prep, 2026-05-12). Previous incarnation referenced
GM-orchestrator and telegram-webhook services that don't exist in our fork.

Checks (9 total):

  1. inbox-watcher service active — auto-restart if down
  2. outbox-notifier service active — auto-restart if down
  3. beacon-bot service active — auto-restart if down (carve-out from the
     bots-are-alert-only rule, because beacon-bot IS the alert delivery
     channel; alerts about beacon-bot being down can't reach Larry's phone
     if beacon-bot stays down. See design review C1)
  4. inbox-watcher process RSS < 1.5 GB — auto-restart if exceeded
     (15 min cooldown; V2 RSS-via-MainPID pattern from upstream)
  5. inbox-watcher cgroup memory — warn at 80% of MemoryMax, critical at
     95%. Catches the spawned-claude-subprocess overrun shape that the
     V2 single-process check misses. No auto-restart (a child-driven
     cgroup spike that restarts the watcher orphans in-flight tasks)
  6. Disk usage — warn at 80%, critical at 90%
  7. System memory — warn at 80%, critical at 90%
  8. inbox_watcher.log freshness (idle-aware: stale log only matters when
     work is queued AND no active workers)
  9. Bots health — forge/mirror/pulse alert-only (their systemd
     `Restart=always` handles crashes; watchdog surfaces sustained
     outages where StartLimitBurst has been exhausted)

Auto-recovery scope: per Q1-Dial-3 with carve-out for beacon-bot, only
inbox-watcher, outbox-notifier, and beacon-bot get auto-restart. The
other 3 bots are alert-only.

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
from datetime import datetime
from pathlib import Path

# Allow `import larry_alerts` from scripts/ directory.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import larry_alerts  # noqa: E402

AGENTS_ROOT = Path.home() / 'agents'
BLACKBOARD = AGENTS_ROOT / 'blackboard'
LOG_DIR = AGENTS_ROOT / 'logs'
HEALTH_FILE = BLACKBOARD / 'system-health.json'

# Services watchdog will auto-restart on detect-down. beacon-bot is here
# (despite being a bot) because it's the alert delivery channel — its
# down-alert can't reach the user if its consumer is down.
AUTO_RESTART_SERVICES = (
    'ourliberty-inbox-watcher',
    'ourliberty-outbox-notifier',
    'ourliberty-beacon-bot',
)

# Bots that watchdog ALERTS on but does not auto-restart. Their systemd
# units have Restart=always so crash recovery is already automatic;
# watchdog surfaces the case where StartLimitBurst (10 starts / 300s) is
# exhausted and systemd has given up.
ALERT_ONLY_BOTS = ('forge', 'mirror', 'pulse')

# Service-active states treated as "alive enough — leave it alone".
# Includes 'auto-restart' SubState (M1 fix): systemd has scheduled a
# restart per Restart= policy; watchdog calling `systemctl start` during
# this window pointlessly depletes StartLimitBurst.
ALIVE_ACTIVE_STATES = ('active', 'activating', 'deactivating', 'reloading')

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


def _check_auto_restart(service_name: str, friendly: str) -> dict:
    """Shared shape: alive? if not, start; if start fails, append critical alert."""
    if is_service_alive(service_name):
        return {'status': 'ok'}
    log(f'{friendly} ({service_name}) is DOWN — attempting start', 'WARN')
    if _attempt_start(service_name):
        log(f'{friendly} recovered')
        # Surface the recovery event so Larry knows watchdog acted.
        larry_alerts.append_alert(
            source='watchdog',
            severity='warning',
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


def check_beacon_bot() -> dict:
    """beacon-bot is the alert delivery channel — auto-restart on detect-down
    despite being a bot (design review C1 carve-out)."""
    return _check_auto_restart('ourliberty-beacon-bot', 'beacon-bot')


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


def check_inbox_watcher_memory() -> dict:
    """V2 process-RSS check. Adapted verbatim from upstream's V2 logic;
    swaps the service name + cooldown marker. Restart above threshold."""
    main_pid = _inbox_watcher_main_pid()
    if main_pid is None:
        return {'status': 'unavailable', 'reason': 'MainPID unresolvable'}
    rss_bytes = _read_process_rss(main_pid)
    if rss_bytes is None:
        return {'status': 'unavailable', 'reason': f'VmRSS unreadable for pid {main_pid}'}

    cooldown_marker = AGENTS_ROOT / 'state' / 'inbox-watcher-mem-restart-cooldown'
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

    # Are any inboxes non-empty?
    live_files = 0
    inboxes_root = AGENTS_ROOT / 'inboxes'
    try:
        for agent_dir in inboxes_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith(('.', '_')):
                continue
            try:
                if any(agent_dir.glob('*.json')):
                    live_files += 1
            except OSError:
                pass
    except OSError:
        live_files = -1

    if not watcher_alive:
        return {
            'status': 'critical',
            'seconds_since_write': int(age),
            'reason': 'inbox-watcher service not active',
        }
    if live_files == 0 and age <= 43200:
        return {
            'status': 'ok',
            'seconds_since_write': int(age),
            'reason': 'idle (empty inboxes, watcher healthy)',
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


def check_bots() -> dict:
    """Alert-only sweep over forge/mirror/pulse bots.

    M3 fix: subject-specific cooldown keys (`bots:mirror`, `bots:forge`,
    `bots:pulse`) so each bot has its own dedup bucket — if mirror-bot
    goes down at 11pm and forge-bot goes down at 11:30pm, the second
    alert isn't suppressed by the first's cooldown.
    """
    results: dict = {}
    down: list[str] = []
    for short in ALERT_ONLY_BOTS:
        service = f'ourliberty-{short}-bot'
        alive = is_service_alive(service)
        results[service] = 'active' if alive else 'inactive'
        if alive:
            continue
        down.append(service)
        log(
            f'{service} is DOWN — alert-only (systemd Restart=always handles '
            f'crash recovery; watchdog surfaces sustained outage)',
            'WARN',
        )
        larry_alerts.append_alert(
            source='watchdog',
            severity='critical',
            subject=f'bots:{short}',
            message=(
                f'{service} is DOWN and systemd has not recovered it. '
                'Likely StartLimitBurst exhausted.'
            ),
            suggested_action=(
                f'sudo systemctl status {service} && '
                f'sudo systemctl restart {service}'
            ),
        )
    return {
        'status': 'critical' if down else 'ok',
        'services': results,
        'down': down,
    }


# ---------- orchestration ----------


def run_all_checks() -> dict:
    """Run every check, persist summary to system-health.json."""
    results = {
        'timestamp': datetime.now().isoformat(),
        'checks': {
            'inbox_watcher': check_inbox_watcher(),
            'outbox_notifier': check_outbox_notifier(),
            'beacon_bot': check_beacon_bot(),
            'inbox_watcher_memory': check_inbox_watcher_memory(),
            'inbox_watcher_cgroup': check_inbox_watcher_cgroup(),
            'disk': check_disk(),
            'memory': check_memory(),
            'log_growth': check_log_growth(),
            'bots': check_bots(),
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
        BLACKBOARD.mkdir(parents=True, exist_ok=True)
        with open(HEALTH_FILE, 'w') as f:
            json.dump(results, f, indent=2)
    except OSError as e:
        log(f'health-file write failed: {e}', 'ERROR')

    log(f'Watchdog complete: overall={results["overall"]}')
    return results


if __name__ == '__main__':
    results = run_all_checks()
    print(json.dumps(results, indent=2))
    sys.exit(0 if results['overall'] != 'critical' else 1)
