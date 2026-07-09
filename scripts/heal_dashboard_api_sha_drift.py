#!/usr/bin/env python3
"""heal_dashboard_api_sha_drift.py — restart ourliberty-dashboard-api when it
is running stale code.

dashboard-api-deploy-race-001 (2026-07-08). The dashboard-api uvicorn
process runs directly from the git checkout (`WorkingDirectory=/home/larry/
agent-core`), so the code it serves is fixed at the SHA it imported at
startup. A deploy's restart trigger can race ahead of the merge landing on
disk: on 2026-07-08 the service restarted at 21:39:33, PR #885's commit
c9046686 landed at 21:40:21 (48s later), and the process served a build
without the new `/api/system/health` route — 404 for ~2h — until a manual
restart. The mtime-based `heal_stale_daemon_code` healer could not catch it:
its RACE_AVOIDANCE_SEC window (5 min) treats a small (48s) gap between the
script's mtime and the process start as a benign "the restart just picked
this up" case, so a restart that lands *just before* the code does is a
permanent blind spot for the mtime heuristic.

A SHA equality check has no such blind spot. This healer:
  1. Reads the live on-disk HEAD (`git -C <repo> rev-parse HEAD`).
  2. Asks the running process what SHA it loaded, via the token-gated
     `/api/system/health` endpoint (`git_sha` field, captured at import).
  3. If the two disagree — OR the running process predates the field/route
     entirely (missing `git_sha`, or a 404 on the route) — the process is
     stale: `sudo -n systemctl restart --no-block` it, verify it comes back
     `active`, and record a restart cooldown.

Safe-by-construction (mirrors heal_stale_daemon_code):
  - Kill-switch aware: ~/agents/healers.disabled disables the whole tick.
  - Restarts ONLY on a *confirmed* stale-code signal. An unreachable API
    (connection refused / timeout / 5xx) is NOT treated as drift — the
    process may be legitimately restarting, and `Restart=on-failure` plus
    heal_stale_daemon_code own the crashed-daemon case. 401/403 (token
    misconfig) is logged, never restarted.
  - Per-restart cooldown (RESTART_COOLDOWN_SEC) bounds this to one restart
    per window, so a deploy loop or a persistently-null git_sha can't turn
    into a restart storm; a still-drifted unit after the cooldown escalates
    to a manual-investigation DM instead of hammering the service.
  - `sudo -n` (non-interactive): fails fast + DMs if passwordless sudo is
    ever revoked, rather than wedging the systemd one-shot.
  - Successful self-heals route to the digest lane (no page — it's fully
    resolved); restart FAILURE and post-cooldown-still-stale escalate.

Stdlib only. Run:
    python3 scripts/heal_dashboard_api_sha_drift.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-dashboard-api-sha-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-dashboard-api-sha-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-dashboard-api-sha-drift.json'

# The repo the dashboard-api process runs from (its systemd WorkingDirectory).
# Env-overridable for tests.
REPO_DIR = Path(os.environ.get('OURLIBERTY_AGENT_CORE_DIR', '/home/larry/agent-core'))

UNIT = 'ourliberty-dashboard-api.service'

# Local, token-gated API base. The healer runs on the droplet, so it talks to
# the loopback listener directly rather than through the public edge.
DEFAULT_API_BASE = 'http://127.0.0.1:8000'
HEALTH_PATH = '/api/system/health'
HEALTH_HEADER = 'X-Dashboard-Token'
HTTP_TIMEOUT_S = 10.0

# One restart per this window. Matches the healer's own timer cadence closely
# so a genuine post-deploy drift is fixed on the first tick, while a deploy
# loop or an environment where git_sha is persistently null can't restart-storm
# the service. A still-drifted unit after the cooldown escalates to a DM.
RESTART_COOLDOWN_SEC = 15 * 60

# Don't escalate the same still-stale-after-restart incident more than once per
# this window (larger than larry_alerts' built-in 60-min cooldown so this is the
# binding constraint for the escalation subject).
ALERT_COOLDOWN_SEC = 6 * 60 * 60

RESTART_TIMEOUT_S = 30
RESTART_SETTLE_S = 3
_HEALTHY_ACTIVE_STATES = frozenset(
    {'active', 'activating', 'reloading', 'deactivating'}
)


# -------------------- logging + heartbeat --------------------

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
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# -------------------- state (cooldowns) --------------------

def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2, sort_keys=True)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _within_cooldown(state: dict[str, Any], key: str, window: float,
                     now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    last = state.get(key)
    if not isinstance(last, (int, float)):
        return False
    return (now - last) < window


def _stamp(state: dict[str, Any], key: str, now: Optional[float] = None) -> None:
    state[key] = now if now is not None else time.time()


# -------------------- git + API probes --------------------

def read_disk_head(repo_dir: Path = REPO_DIR) -> Optional[str]:
    """Full SHA of the repo's current on-disk HEAD, or None if unavailable."""
    try:
        proc = subprocess.run(
            ['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _api_base() -> str:
    return os.environ.get('DASHBOARD_API_URL', DEFAULT_API_BASE).rstrip('/')


def _api_token() -> Optional[str]:
    tok = (os.environ.get('DASHBOARD_API_TOKEN') or '').strip()
    return tok or None


# Sentinels for probe_running_sha's status so the caller can branch precisely.
PROBE_OK = 'ok'                  # got a 200 with a git_sha value
PROBE_FIELD_MISSING = 'field-missing'  # 200 but git_sha absent/null -> stale code
PROBE_ROUTE_MISSING = 'route-missing'  # 404 -> code predates the route -> stale
PROBE_UNREACHABLE = 'unreachable'      # conn refused / timeout / 5xx -> skip
PROBE_AUTH = 'auth'              # 401/403 -> token misconfig -> skip
PROBE_NO_TOKEN = 'no-token'      # token not configured -> skip


def probe_running_sha(
    api_base: Optional[str] = None,
    token: Optional[str] = None,
    opener=None,
) -> tuple[str, Optional[str]]:
    """Ask the running process what SHA it loaded.

    Returns (probe_status, running_sha). running_sha is set only when
    probe_status == PROBE_OK. `opener` is injectable for tests (a callable
    matching urllib.request.urlopen).
    """
    base = (api_base or _api_base()).rstrip('/')
    tok = token or _api_token()
    if not tok:
        return PROBE_NO_TOKEN, None
    url = f'{base}{HEALTH_PATH}'
    req = urllib.request.Request(url, headers={HEALTH_HEADER: tok}, method='GET')
    _open = opener or urllib.request.urlopen
    try:
        with _open(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            status = getattr(resp, 'status', 200)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return PROBE_ROUTE_MISSING, None
        if e.code in (401, 403):
            return PROBE_AUTH, None
        # 5xx and anything else transient -> treat as unreachable (don't restart).
        return PROBE_UNREACHABLE, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return PROBE_UNREACHABLE, None
    if status != 200:
        return PROBE_UNREACHABLE, None
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return PROBE_UNREACHABLE, None
    if not isinstance(payload, dict):
        return PROBE_UNREACHABLE, None
    sha = payload.get('git_sha')
    if isinstance(sha, str) and sha.strip():
        return PROBE_OK, sha.strip()
    # 200 but no git_sha: the running code predates this feature -> stale.
    return PROBE_FIELD_MISSING, None


# -------------------- restart action --------------------

def _unit_active_state(unit: str = UNIT) -> str:
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=10,
        )
        return (result.stdout or '').strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def restart_unit(unit: str = UNIT) -> tuple[int, str]:
    """`sudo -n systemctl restart --no-block <unit>`, verify via is-active.

    Contract (same as heal_stale_daemon_code.auto_restart_unit):
      - (0, '')      restart enqueued and the unit is active/activating.
      - (rc, stderr) the restart job was rejected up front (genuine failure).
      - (-1, detail) enqueued but not active/activating after the settle.

    A `sudo -n systemctl daemon-reload` is prepended (parity with
    heal_stale_daemon_code) so the restart picks up any unit-file edit that
    landed since systemd last parsed it; a failed reload is a WARN, never a
    blocker (a stale-but-running daemon beats a stopped one).
    """
    try:
        reload_result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'daemon-reload'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
        if reload_result.returncode != 0:
            log(f'daemon-reload before restart of {unit} failed '
                f'rc={reload_result.returncode}; proceeding with restart', 'WARN')
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f'daemon-reload before restart of {unit}: {type(e).__name__}; '
            f'proceeding with restart', 'WARN')

    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', '--no-block', unit],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        result = None  # even --no-block hung; fall through to verify
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'

    if result is not None and result.returncode != 0:
        return result.returncode, (result.stderr or '').strip()

    # Verify the unit comes back. A single is-active can catch a brief transient
    # (old process still draining, new one not yet up) as inactive/failed, so a
    # non-healthy first read gets a second look after another settle before we
    # declare failure — cheap insurance against a false 'restart-failed' page.
    time.sleep(RESTART_SETTLE_S)
    state = _unit_active_state(unit)
    if state in _HEALTHY_ACTIVE_STATES:
        return 0, ''
    time.sleep(RESTART_SETTLE_S)
    state = _unit_active_state(unit)
    if state in _HEALTHY_ACTIVE_STATES:
        return 0, ''
    return -1, (
        f'restart issued but unit is {state!r} after {2 * RESTART_SETTLE_S}s '
        f'(expected active/activating)'
    )


# -------------------- DM --------------------

def _dm(message: str, subject: str, suggested_action: str,
        severity: str, route: str) -> bool:
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-dashboard-api-sha-drift',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
            route=route,
        )
    except Exception as e:  # noqa: BLE001 — never wedge the healer on a DM
        log(f'dm failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- orchestration --------------------

def run_once(now: Optional[float] = None) -> str:
    """Single tick. Returns a short outcome string (also for tests)."""
    if kill_switch_active():
        log('KILL_SWITCH active; exiting')
        return 'kill-switch'
    heartbeat()
    now = now if now is not None else time.time()

    disk_head = read_disk_head()
    if not disk_head:
        log(f'cannot read on-disk HEAD at {REPO_DIR}; nothing to compare', 'WARN')
        return 'no-disk-head'

    probe_status, running_sha = probe_running_sha()

    if probe_status == PROBE_OK:
        if running_sha == disk_head:
            log(f'fresh: running {running_sha[:8]} == HEAD {disk_head[:8]}')
            return 'fresh'
        reason = (f'running git_sha {running_sha[:8]} != on-disk HEAD '
                  f'{disk_head[:8]}')
    elif probe_status == PROBE_FIELD_MISSING:
        reason = ('running process returned no git_sha (code predates the '
                  f'SHA-reporting field; on-disk HEAD is {disk_head[:8]})')
    elif probe_status == PROBE_ROUTE_MISSING:
        reason = (f'{HEALTH_PATH} returned 404 (running code predates the '
                  f'route; on-disk HEAD is {disk_head[:8]})')
    elif probe_status == PROBE_NO_TOKEN:
        log('DASHBOARD_API_TOKEN not configured; cannot probe running SHA', 'WARN')
        return 'no-token'
    elif probe_status == PROBE_AUTH:
        log(f'{HEALTH_PATH} rejected the token (401/403); not restarting on an '
            f'auth misconfig', 'WARN')
        return 'auth'
    else:  # PROBE_UNREACHABLE
        log('dashboard-api unreachable (down/restarting/5xx); not a code-drift '
            'signal — Restart=on-failure and heal_stale_daemon_code own that '
            'case. Skipping.')
        return 'unreachable'

    # --- confirmed stale code below ---
    log(f'STALE: {reason}', 'WARN')
    state = load_state()

    # The cooldown is keyed on the TARGET SHA, not time alone: "stuck" means we
    # already restarted FOR THIS on-disk HEAD within the window and it's STILL
    # drifted (a genuine loop / restart-not-taking). A DIFFERENT, newer on-disk
    # HEAD is a fresh back-to-back deploy — restart for it now rather than
    # mislabeling it a stuck loop.
    restarted_recently = _within_cooldown(
        state, 'last_restart_ts', RESTART_COOLDOWN_SEC, now=now)
    same_target = state.get('last_restart_target_sha') == disk_head
    if restarted_recently and same_target:
        # Escalate (not another restart) at most once per ALERT window.
        if not _within_cooldown(state, 'last_alert_ts', ALERT_COOLDOWN_SEC, now=now):
            _dm(
                message=(
                    f'{UNIT} is STILL running stale code after an auto-restart '
                    f'within the last {RESTART_COOLDOWN_SEC // 60} min.\n\n'
                    f'{reason}\n\n'
                    f'This is a loop or a stuck deploy — the restart is not '
                    f'making the process pick up on-disk HEAD.'
                ),
                subject='dashboard-api-sha-drift-stuck',
                suggested_action=(
                    'ssh larry@134.209.44.80 and run: '
                    'systemctl status ourliberty-dashboard-api.service && '
                    'git -C /home/larry/agent-core log --oneline -3 && '
                    'journalctl -u ourliberty-dashboard-api.service '
                    '--since "20 min ago" | tail -80. '
                    'Manual recovery: sudo systemctl restart '
                    'ourliberty-dashboard-api.service'
                ),
                severity='critical',
                route='escalate',
            )
            _stamp(state, 'last_alert_ts', now=now)
            save_state(state)
        else:
            log('still stale but within restart+alert cooldown; suppressing')
        return 'stuck-in-cooldown'

    rc, stderr = restart_unit()
    # Record what we restarted FOR on both paths so the next tick can tell a
    # genuine stuck loop (same target) from a fresh deploy (different target).
    _stamp(state, 'last_restart_ts', now=now)
    state['last_restart_target_sha'] = disk_head
    if rc == 0:
        save_state(state)
        log(f'auto-restarted {UNIT} (was stale: {reason})')
        _dm(
            message=(
                f'Auto-restarted {UNIT} — it was running stale code and is now '
                f'reloading on-disk HEAD {disk_head[:8]}.\n\n{reason}'
            ),
            subject='dashboard-api-sha-drift-healed',
            suggested_action='',
            severity='warning',
            route='digest',  # fully self-healed; log-only, no page
        )
        return 'restarted'

    # Restart itself failed — this needs hands. Stamp the alert cooldown too so
    # the next tick (same target, still in restart cooldown) suppresses the
    # 'stuck' escalation instead of double-paging for one incident.
    _stamp(state, 'last_alert_ts', now=now)
    save_state(state)
    log(f'auto-restart FAILED for {UNIT}: rc={rc} {stderr}', 'ERROR')
    _dm(
        message=(
            f'Tried to auto-restart {UNIT} (running stale code) but the '
            f'restart FAILED.\n\n{reason}\nRestart error: {stderr}'
        ),
        subject='dashboard-api-sha-drift-restart-failed',
        suggested_action=(
            'ssh larry@134.209.44.80 and run: '
            'sudo systemctl restart ourliberty-dashboard-api.service && '
            'systemctl status ourliberty-dashboard-api.service'
        ),
        severity='critical',
        route='escalate',
    )
    return 'restart-failed'


def main() -> int:
    try:
        outcome = run_once()
        log(f'tick outcome={outcome}')
        return 0
    except Exception as exc:  # noqa: BLE001 — daemon-never-wedge
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main())
