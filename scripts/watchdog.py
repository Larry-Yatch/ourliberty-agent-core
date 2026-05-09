#!/usr/bin/env python3
"""
ourliberty Agents Watchdog — 8-check system health monitor with auto-recovery.
Runs every 2 minutes via systemd timer. Supersedes the health-check cron.

Checks:
  1. Orchestrator process alive
  2. Telegram webhook process alive
  3. Disk usage < 90%
  4. Memory usage < 90%
  5. Inbox stale task detection (task older than 2h = stuck)
  6. Log file growth (orchestrator.log written to in last 5min)
  7. Token manager status (at least one account usable)
  8. Systemd service health (all ourliberty-* services active)

Auto-recovery:
  - Restart crashed ourliberty-orchestrator or ourliberty-telegram-webhook
  - Alert on disk/memory pressure
  - Flag stale tasks for investigation
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

AGENTS_ROOT = Path.home() / 'agents'
BLACKBOARD = AGENTS_ROOT / 'blackboard'
LOG_DIR = AGENTS_ROOT / 'logs'
HEALTH_FILE = BLACKBOARD / 'system-health.json'

# Services watchdog can restart
RESTARTABLE_SERVICES = ['ourliberty-orchestrator', 'ourliberty-telegram-webhook']

# All ourliberty services to monitor
ALL_SERVICES = [
    'ourliberty-orchestrator',
    'ourliberty-telegram-webhook',
    'ourliberty-github-webhook',
    'ourliberty-merge-watcher.timer',
    'ourliberty-worktree-cleanup.timer',
]

AGENT_IDS = ['beacon', 'forge', 'mirror', 'pulse']


def log(message, level='INFO'):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = f'[{ts}] [watchdog] [{level}] {message}'
    print(entry)
    log_file = LOG_DIR / 'watchdog.log'
    with open(log_file, 'a') as f:
        f.write(entry + '\n')


def check_service_active(service_name):
    """Check if a systemd service is alive (active or activating).

    Treats 'activating' as alive — the service is coming up via ExecStartPre
    or starting; calling restart while in this state would reset the timer
    and cause an infinite startup loop (B12 — observed 2026-04-30 with
    resurrect_orphan_workers ExecStartPre stuck in retry).
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True, text=True, timeout=10
        )
        state = result.stdout.strip()
        return state in ('active', 'activating', 'deactivating', 'reloading')
    except Exception:
        return False


def check_orchestrator():
    """Check 1: Orchestrator process alive.

    'start' is idempotent — a no-op if service is already running or
    activating. Using 'restart' would reset in-progress ExecStartPre,
    causing infinite retry loops (B12 — observed 2026-04-30).
    """
    alive = check_service_active('ourliberty-orchestrator')
    if not alive:
        log('Orchestrator is DOWN — attempting start', 'WARN')
        try:
            subprocess.run(['systemctl', 'start', 'ourliberty-orchestrator'],
                         capture_output=True, timeout=30)
            # Wait a bit longer — ExecStartPre can take 30-60s legitimately
            time.sleep(8)
            if check_service_active('ourliberty-orchestrator'):
                log('Orchestrator started successfully (or activating)')
                return {'status': 'recovered', 'action': 'started'}
            else:
                log('Orchestrator start FAILED', 'ERROR')
                return {'status': 'down', 'action': 'start_failed'}
        except Exception as e:
            log(f'Orchestrator start exception: {e}', 'ERROR')
            return {'status': 'down', 'action': str(e)}
    return {'status': 'ok'}


ORCH_PROCESS_RSS_THRESHOLD_BYTES = 1_073_741_824  # 1 GB

def check_orchestrator_memory():
    """F-MEM-RESTART (V2 — refined F-MEM-RESTART-V2 2026-05-01 18:48): restart on orchestrator
    PROCESS RSS bloat, not cgroup memory.

    V1 used cgroup memory.current which includes ALL workers + page cache
    in the orchestrator's cgroup. With 8+ active claude workers (each ~100-
    150 MB RSS) plus their disk cache, cgroup easily reports 10+ GB even
    when the orchestrator process itself is healthy at 26 MB. Restarting
    doesn't free worker memory (F43 keeps them alive) or cache, so the
    metric was triggering restart-thrash on pure overhead.

    V2 reads /proc/<MainPID>/status VmRSS — the orchestrator process's
    own resident memory. Hard threshold at 1 GB (healthy orchestrator with
    6 dispatch slots + tracking state is 50-200 MB; 1 GB indicates real
    bloat). Cgroup memory still logged as informational.

    Safety unchanged: F43 KillMode=process keeps workers alive across restart.
    Cooldown unchanged: 15 min between restarts to prevent thrash.

    Returns: {'status': ..., 'orch_rss_mb': ..., 'cgroup_gb': ...}
    """
    try:
        # 1. Get orchestrator MainPID + measure its RSS
        r = subprocess.run(
            ['systemctl', 'show', 'ourliberty-orchestrator.service',
             '-p', 'MainPID', '--value'],
            capture_output=True, text=True, timeout=5,
        )
        pid_raw = r.stdout.strip()
        if not pid_raw or not pid_raw.isdigit() or pid_raw == '0':
            return {'status': 'unavailable', 'reason': f'MainPID raw={pid_raw!r}'}
        main_pid = int(pid_raw)

        try:
            with open(f'/proc/{main_pid}/status') as f:
                status_lines = f.read().splitlines()
        except FileNotFoundError:
            return {'status': 'unavailable', 'reason': f'/proc/{main_pid}/status missing'}

        orch_rss_kb = None
        for line in status_lines:
            if line.startswith('VmRSS:'):
                orch_rss_kb = int(line.split()[1])
                break
        if orch_rss_kb is None:
            return {'status': 'unavailable', 'reason': 'VmRSS not found in status'}
        orch_rss = orch_rss_kb * 1024  # bytes

        # 2. Cgroup memory (informational — not used for restart decision)
        cgroup_bytes = None
        try:
            cgroup_path = '/sys/fs/cgroup/system.slice/ourliberty-orchestrator.service/memory.current'
            with open(cgroup_path) as f:
                cgroup_bytes = int(f.read().strip())
        except (FileNotFoundError, ValueError):
            pass

        # 3. Cooldown — 15 min between restarts to prevent thrash
        cooldown_marker = AGENTS_ROOT / 'state' / 'orch-mem-restart-cooldown'
        cooldown_marker.parent.mkdir(parents=True, exist_ok=True)
        if cooldown_marker.exists():
            age = time.time() - cooldown_marker.stat().st_mtime
            if age < 900:
                return {
                    'status': 'cooldown',
                    'orch_rss_mb': round(orch_rss / 1e6, 1),
                    'cgroup_gb': round(cgroup_bytes / 1e9, 2) if cgroup_bytes else None,
                    'reason': f'last restart {int(age)}s ago',
                }

        # 4. Restart decision — based on orchestrator process RSS, not cgroup
        if orch_rss > ORCH_PROCESS_RSS_THRESHOLD_BYTES:
            log(f'F-MEM-RESTART-V2: orch process RSS {orch_rss/1e9:.2f}G '
                f'> 1.0G threshold (cgroup={cgroup_bytes/1e9 if cgroup_bytes else "?":.1f}G) '
                f'— triggering graceful restart (F43 keeps workers alive)', 'WARN')
            try:
                subprocess.run(['systemctl', 'restart', 'ourliberty-orchestrator'],
                               capture_output=True, timeout=180)
                cooldown_marker.touch()
                time.sleep(5)
                return {
                    'status': 'restarted',
                    'orch_rss_mb': round(orch_rss / 1e6, 1),
                    'cgroup_gb': round(cgroup_bytes / 1e9, 2) if cgroup_bytes else None,
                    'threshold_mb': 1024,
                }
            except Exception as e:
                log(f'F-MEM-RESTART-V2 exception: {e}', 'ERROR')
                return {'status': 'restart_failed', 'error': str(e)}

        return {
            'status': 'ok',
            'orch_rss_mb': round(orch_rss / 1e6, 1),
            'cgroup_gb': round(cgroup_bytes / 1e9, 2) if cgroup_bytes else None,
        }
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def check_telegram():
    """Check 2: Telegram webhook process alive."""
    alive = check_service_active('ourliberty-telegram-webhook')
    if not alive:
        log('Telegram webhook is DOWN — attempting restart', 'WARN')
        try:
            subprocess.run(['systemctl', 'restart', 'ourliberty-telegram-webhook'],
                         capture_output=True, timeout=30)
            time.sleep(3)
            if check_service_active('ourliberty-telegram-webhook'):
                log('Telegram webhook restarted successfully')
                return {'status': 'recovered', 'action': 'restarted'}
            else:
                log('Telegram webhook restart FAILED', 'ERROR')
                return {'status': 'down', 'action': 'restart_failed'}
        except Exception as e:
            log(f'Telegram restart exception: {e}', 'ERROR')
            return {'status': 'down', 'action': str(e)}
    return {'status': 'ok'}


def check_disk():
    """Check 3: Disk usage < 90%."""
    try:
        stat = os.statvfs('/')
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used_pct = int((1 - free / total) * 100)
        if used_pct > 90:
            log(f'Disk usage CRITICAL: {used_pct}%', 'ERROR')
            return {'status': 'critical', 'percent': used_pct}
        elif used_pct > 80:
            log(f'Disk usage WARNING: {used_pct}%', 'WARN')
            return {'status': 'warning', 'percent': used_pct}
        return {'status': 'ok', 'percent': used_pct}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def check_memory():
    """Check 4: Memory usage < 90%."""
    try:
        with open('/proc/meminfo') as f:
            lines = f.readlines()
        mem = {}
        for line in lines:
            parts = line.split(':')
            if len(parts) == 2:
                key = parts[0].strip()
                val = int(parts[1].strip().split()[0])  # kB
                mem[key] = val
        total = mem.get('MemTotal', 1)
        available = mem.get('MemAvailable', total)
        used_pct = int((1 - available / total) * 100)
        if used_pct > 90:
            log(f'Memory usage CRITICAL: {used_pct}%', 'ERROR')
            return {'status': 'critical', 'percent': used_pct}
        elif used_pct > 80:
            log(f'Memory usage WARNING: {used_pct}%', 'WARN')
            return {'status': 'warning', 'percent': used_pct}
        return {'status': 'ok', 'percent': used_pct}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def check_stale_tasks():
    """Check 5: Inbox stale task detection (task older than 2h = stuck)."""
    stale = []
    cutoff = time.time() - 7200  # 2 hours ago
    for agent_id in AGENT_IDS:
        inbox = AGENTS_ROOT / 'inboxes' / agent_id
        if not inbox.exists():
            continue
        for task_file in inbox.glob('*.json'):
            try:
                mtime = task_file.stat().st_mtime
                # Check for not_before — task may be intentionally deferred
                with open(task_file) as f:
                    task = json.load(f)
                not_before = task.get('not_before')
                if not_before:
                    try:
                        nb_time = datetime.fromisoformat(not_before).timestamp()
                        if nb_time > time.time():
                            continue  # Task is intentionally deferred, not stale
                    except (ValueError, TypeError):
                        pass
                if mtime < cutoff:
                    age_min = int((time.time() - mtime) / 60)
                    stale.append({
                        'agent': agent_id,
                        'file': task_file.name,
                        'age_minutes': age_min
                    })
            except Exception:
                continue
    # Cycle Airtight Plan v1 iter 1 (2026-05-02): dedup WARN logging.
    # Without this, watchdog cron logs WARN every 2 minutes for the same
    # stuck tasks (~600 WARN events/day for 3 tasks). The escalation has
    # already happened — repeated WARN-level spam serves no purpose.
    # Track per-task last-warn time in a state file; only re-WARN after
    # 30 minutes per unique task.
    _stale_dedup_path = AGENTS_ROOT / 'state' / 'watchdog_stale_dedup.json'
    _now = time.time()
    _DEDUP_WINDOW_SEC = 1800  # 30 minutes
    _last_logged = {}
    try:
        if _stale_dedup_path.exists():
            with open(_stale_dedup_path) as _f:
                _last_logged = json.load(_f)
    except Exception:
        _last_logged = {}
    fresh_to_log = []
    for s in stale:
        key = f"{s['agent']}/{s['file']}"
        if _now - _last_logged.get(key, 0) >= _DEDUP_WINDOW_SEC:
            fresh_to_log.append(s)
            _last_logged[key] = _now
    # Garbage-collect entries older than 24h (file no longer in inbox)
    _last_logged = {k: v for k, v in _last_logged.items() if _now - v < 86400}
    try:
        _stale_dedup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_stale_dedup_path, 'w') as _f:
            json.dump(_last_logged, _f)
    except Exception:
        pass
    if fresh_to_log:
        log(f'Found {len(fresh_to_log)} stale tasks (deduped): {json.dumps(fresh_to_log[:5])}', 'WARN')
    elif stale:
        log(f'{len(stale)} known-stale tasks (within 30min dedup window)', 'INFO')
    return {'status': 'warning' if stale else 'ok', 'stale_tasks': stale}




def _inbox_task_has_active_worker(task_filename):
    """Return True if any active claude worker has a /proc/*/cwd that
    references this task's inbox filename or its derived worktree.

    Mirrors pipeline_watcher._task_has_active_worker — used to suppress
    false positives where a queued task is actively being processed.

    Cycle Airtight Plan v1 iter 2 (2026-05-02): broadened from
    'claude.*--model claude-opus' (only matched claude CLIs) to also
    detect bash post-push waiters that are children of (now-dead)
    claude CLIs. Without this broadening, ~30 'Orchestrator log stale'
    false-positive WARNs/hour fired even though work was actively in
    flight via bash children. Now scans ANY process whose /proc/*/cwd
    contains the task signature — same matching logic, broader process
    set, no semantic change beyond fewer false positives.
    """
    import subprocess, os, glob
    # Strip .json + take a substring signature for fuzzy match
    stem = task_filename.replace('.json', '')
    # Use first 30 chars as the signature (worktrees often truncate names)
    sig = stem[:30] if len(stem) > 30 else stem
    try:
        # Walk all /proc/<pid>/cwd directly. Faster than pgrep + readlink loop
        # and catches every process regardless of comm/args (claude CLIs,
        # bash post-push waiters, eslint, tsc, playwright, etc).
        for cwd_link in glob.glob('/proc/[0-9]*/cwd'):
            try:
                cwd = os.readlink(cwd_link)
                # Strip the " (deleted)" suffix that Linux appends when the
                # directory has been unlinked — the path string itself still
                # contains the worktree name we want to match against.
                cwd_clean = cwd.replace(' (deleted)', '')
                if sig in cwd_clean or stem in cwd_clean:
                    return True
            except (OSError, FileNotFoundError):
                # Process exited between glob and readlink; skip
                pass
    except Exception:
        pass
    return False


def check_log_growth():
    """Check 6: Log file growth — context-aware.

    A stale orchestrator.log is only a problem when work is queued. If
    inboxes are empty AND no Larry-originated tasks pending AND orchestrator
    process is healthy-sleeping, this is normal idle. We threshold against
    12h of true idleness, not 5 min of writes.
    """
    log_file = LOG_DIR / 'orchestrator.log'
    if not log_file.exists():
        return {'status': 'warning', 'reason': 'orchestrator.log not found'}
    try:
        mtime = log_file.stat().st_mtime
        age = time.time() - mtime

        # Always ok under 5 min (active)
        if age <= 300:
            return {'status': 'ok', 'seconds_since_write': int(age)}

        # Above 5 min — check whether system is genuinely idle
        inboxes_root = Path.home() / 'agents' / 'inboxes'
        live_files = 0
        try:
            for agent_dir in inboxes_root.iterdir():
                if not agent_dir.is_dir():
                    continue
                if agent_dir.name in ('larry', 'deferred'):
                    continue
                if agent_dir.name.startswith('.') or agent_dir.name.startswith('_'):
                    continue
                for f in agent_dir.glob('*.json'):
                    if f.is_file():
                        live_files += 1
                        break
                if live_files:
                    break
        except Exception:
            live_files = -1  # unable to determine — be conservative

        # Check orchestrator process is alive + healthy
        orch_alive = False
        try:
            import subprocess
            r = subprocess.run(['systemctl', 'is-active', 'ourliberty-orchestrator.service'],
                              capture_output=True, text=True, timeout=5)
            orch_alive = (r.stdout.strip() == 'active')
        except Exception:
            pass

        # Genuine idle: empty inboxes + alive orchestrator + age within 12h
        if live_files == 0 and orch_alive and age <= 43200:
            return {'status': 'ok', 'seconds_since_write': int(age),
                    'reason': 'idle (empty inboxes, orchestrator healthy)'}

        # If queued work exists OR orchestrator dead OR genuinely too long
        if age > 600 and live_files > 0:
            # Check if all queued tasks are actually being processed by active
            # workers — if so, the log staleness is normal, not a hang signal.
            from pathlib import Path as _P
            inboxes = tuple(AGENTS_ROOT / 'inboxes' / a for a in AGENT_IDS)
            unattended = 0
            for inbox in inboxes:
                if not inbox.exists():
                    continue
                for task_file in inbox.glob('*.json'):
                    if not _inbox_task_has_active_worker(task_file.name):
                        unattended += 1
                        break
                if unattended:
                    break
            if unattended == 0:
                # All queued work has active workers — system is healthy, just busy
                return {'status': 'ok', 'seconds_since_write': int(age),
                        'reason': f'all {live_files} queued task(s) have active workers'}
            log(f'Orchestrator log stale {int(age)}s with {live_files} queued tasks ({unattended} unattended)', 'WARN')
            return {'status': 'warning', 'seconds_since_write': int(age),
                    'queued': live_files, 'unattended': unattended}
        if not orch_alive:
            return {'status': 'critical', 'seconds_since_write': int(age),
                    'reason': 'orchestrator service not active'}
        if age > 43200:
            return {'status': 'warning', 'seconds_since_write': int(age),
                    'reason': 'idle >12h'}
        return {'status': 'ok', 'seconds_since_write': int(age),
                'reason': 'idle'}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def check_token_manager():
    """Check 7: Token manager status (at least one account usable)."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from token_manager import get_manager
        tm = get_manager()
        active = tm.active_account
        # Check if the active account is rate limited
        accounts = getattr(tm, 'accounts', [])  # list of account dicts (token_manager.accounts is a property)
        if isinstance(accounts, dict):
            accounts = list(accounts.values())
        usable = []
        for acct in accounts:
            if not isinstance(acct, dict):
                continue
            acct_id = acct.get('id', '?')
            rate_limited_until = acct.get('rate_limited_until')
            if rate_limited_until:
                try:
                    rl_time = datetime.fromisoformat(rate_limited_until)
                    if rl_time > datetime.now():
                        continue  # Still rate limited
                except (ValueError, TypeError):
                    pass
            usable.append(acct_id)
        if not usable:
            log('ALL token accounts rate limited!', 'ERROR')
            return {'status': 'critical', 'active': active, 'usable_accounts': 0}
        return {'status': 'ok', 'active': active, 'usable_accounts': len(usable)}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def check_all_services():
    """Check 8: All gm-* systemd services health."""
    results = {}
    down = []
    for svc in ALL_SERVICES:
        active = check_service_active(svc)
        results[svc] = 'active' if active else 'inactive'
        if not active:
            down.append(svc)
    if down:
        log(f'Services down: {down}', 'WARN')
    return {
        'status': 'warning' if down else 'ok',
        'services': results,
        'down': down
    }


def run_all_checks():
    """Run all 8 checks and write results to blackboard."""
    results = {
        'timestamp': datetime.now().isoformat(),
        'checks': {
            'orchestrator': check_orchestrator(),
            'orchestrator_memory': check_orchestrator_memory(),  # F-MEM-RESTART 2026-05-01
            'telegram': check_telegram(),
            'disk': check_disk(),
            'memory': check_memory(),
            'stale_tasks': check_stale_tasks(),
            'log_growth': check_log_growth(),
            'token_manager': check_token_manager(),
            'services': check_all_services(),
        }
    }

    # Determine overall status
    statuses = [c.get('status', 'ok') for c in results['checks'].values()]
    if 'critical' in statuses or 'down' in statuses:
        results['overall'] = 'critical'
    elif 'warning' in statuses or 'error' in statuses:
        results['overall'] = 'warning'
    else:
        results['overall'] = 'healthy'

    # Write to blackboard
    BLACKBOARD.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_FILE, 'w') as f:
        json.dump(results, f, indent=2)

    log(f'Watchdog complete: overall={results["overall"]}')

    # If critical, write an alert task to Sage's inbox — but with cooldown
    # so we don't spam every 2min while a single root cause persists.
    ALERT_COOLDOWN_SECS = 600  # 10 min between alerts for same root cause
    if results['overall'] == 'critical':
        critical_checks = {k: v for k, v in results['checks'].items()
                          if v.get('status') in ('critical', 'down')}
        # Build a stable key from the set of failing checks so we dedupe
        # on the same underlying problem, not just any 'critical' state.
        cooldown_key = '+'.join(sorted(critical_checks.keys())) or 'unknown'
        cooldown_dir = AGENTS_ROOT / 'state' / 'watchdog-alert-cooldown'
        cooldown_dir.mkdir(parents=True, exist_ok=True)
        cooldown_file = cooldown_dir / cooldown_key
        now_ts = time.time()
        last_alert_ts = 0
        if cooldown_file.exists():
            try:
                last_alert_ts = float(cooldown_file.read_text().strip() or '0')
            except (ValueError, OSError):
                last_alert_ts = 0
        if now_ts - last_alert_ts < ALERT_COOLDOWN_SECS:
            log(f'Critical alert SUPPRESSED (cooldown {int(now_ts - last_alert_ts)}s < '
                f'{ALERT_COOLDOWN_SECS}s) for root cause: {cooldown_key}')
            sys.exit(1)
        try:
            cooldown_file.write_text(str(now_ts))
        except OSError:
            pass
        # TODO(Larry): wire up dispatcher equivalent in Phase D — for now route
        # critical alerts to pulse (the observer/healer) since compass isn't live yet.
        alert_inbox = AGENTS_ROOT / 'inboxes' / 'pulse'
        alert_inbox.mkdir(parents=True, exist_ok=True)
        alert_file = alert_inbox / f'watchdog-alert-{int(time.time())}.json'
        alert_task = {
            'prompt': (
                'SYSTEM ALERT from watchdog:\n\n'
                f'Overall status: {results["overall"]}\n'
                f'Critical checks: {json.dumps(critical_checks, indent=2)}\n\n'
                'Investigate immediately and report to Larry if action is needed.'
            ),
            'source': 'watchdog',
            # 'reply_chat_id' will be supplied by Larry's chat-id config when wired up.
            'timeout': 120,
        }
        with open(alert_file, 'w') as f:
            json.dump(alert_task, f, indent=2)
        log('Critical alert dispatched to Pulse')

    return results


if __name__ == '__main__':
    results = run_all_checks()
    print(json.dumps(results, indent=2))
    sys.exit(0 if results['overall'] != 'critical' else 1)
