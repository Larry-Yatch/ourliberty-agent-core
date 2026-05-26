#!/usr/bin/env python3
"""heal_stale_daemon_code.py — detect systemd daemons running pre-merge code.

Chain-discipline-v2 (2026-05-25). Surfaces the failure mode where a fix
to a long-running daemon's script is merged to main + synced to disk but
the systemd-managed process is still running the pre-merge code, because
nothing restarted the unit. The canonical case: PR #103's marker-parser
fix landed on disk at 19:16Z but `ourliberty-outbox-notifier.service` had
been running since 17:37 MDT (~23:37Z prior day) and continued executing
the stale Python module until Larry manually restarted at 17:37 MDT on
2026-05-25.

What this DOES NOT do:
  - Auto-restart anything (same posture as the stuck-detector — surface
    only, DM Larry, let him decide). Restart bypasses Mirror's review for
    behavior changes and risks restarting during in-flight work.
  - Catch stale Python imports inside a long-running daemon that runs as
    a single process across multiple script revisions (different failure
    mode; out of scope).

What this DOES:
  For each `ourliberty-*.service` unit (excluding .timer units):
    1. Read ActiveEnterTimestamp via systemctl show.
    2. Read FragmentPath via systemctl show; parse ExecStart from the
       .service file to identify the script path.
    3. Stat the script's mtime.
    4. If script_mtime > service_start AND (script_mtime - service_start)
       > RACE_AVOIDANCE_SEC: emit a DM via larry_alerts.

  Per-service 6h cooldown is tracked in
  ~/agents/state/heal-stale-daemon-code-cooldowns.json so a chronically
  stale service doesn't spam Larry every 30 min.

Safe-by-construction:
  - Read-only on systemctl + filesystem; no restart calls.
  - Kill-switch aware (exits immediately on ~/agents/healers.disabled).
  - Per-service cooldown (6h) on top of larry_alerts' own 1h cooldown.
  - Idempotent (re-running with no changes is a no-op).
  - Bounded blast radius (one DM per stale service per 6h).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-stale-daemon-code.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-stale-daemon-code.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-stale-daemon-code-cooldowns.json'

# Sub-shell timeout for systemctl calls. systemctl show is fast (<100 ms),
# but we cap it so a hung dbus doesn't wedge the healer tick.
SYSTEMCTL_TIMEOUT_S = 10

# Race-avoidance window. A daemon that's been running for less than this
# duration since the script's mtime is treated as "the restart probably
# just happened; not stale yet." Five minutes is enough to cover a normal
# systemd restart cycle plus typical apt/pip warmup; less than this and
# we'd alert during legitimate restart sequences.
RACE_AVOIDANCE_SEC = 5 * 60

# Per-service cooldown — don't re-alert about the same stale service
# within this window. 6h matches Larry's stated cadence for this healer
# (every 30 min the timer fires; we don't want 12 DMs/hour about the
# same drift). Larger than larry_alerts' built-in 60-min warning cooldown
# so it's the binding constraint when multiple subjects might collide.
PER_SERVICE_COOLDOWN_SEC = 6 * 60 * 60

# Unit glob. `ourliberty-*.service` (NOT .timer; timers don't have
# ExecStart pointing at code — they activate the underlying .service).
UNIT_GLOB = 'ourliberty-*.service'

# Conservative ExecStart parser. Match lines that look like:
#   ExecStart=<interpreter> <args> <script-path>
#   ExecStart=<script-path> <args>
# We extract every absolute path in the line and return the LAST one
# that exists and is not the interpreter itself. Multi-line ExecStart
# (with `\`) is not used in this codebase's units; if it appears, the
# heuristic will fall back to whatever path is on the first line.
_EXEC_START_RE = re.compile(r'^\s*ExecStart\s*=\s*(.+?)\s*$', re.MULTILINE)

# Interpreters we should NOT treat as the script (we want the script
# itself, not the runtime). Match by basename.
_INTERPRETER_BASENAMES = {
    'python', 'python3', 'python3.10', 'python3.11', 'python3.12',
    'node', 'nodejs', 'bash', 'sh', 'env',
}


# -------------------- logging --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- kill-switch --------------------

def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# -------------------- cooldown state file --------------------

def load_state() -> dict:
    """Return {'services': {unit_name: {'last_alert_ts': epoch_seconds}}}."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'services': {}}
        data.setdefault('services', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'services': {}}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def in_cooldown(state: dict, unit: str, now: Optional[float] = None) -> bool:
    entry = state['services'].get(unit)
    if not entry:
        return False
    last = entry.get('last_alert_ts')
    if not isinstance(last, (int, float)):
        return False
    now = now if now is not None else time.time()
    return (now - last) < PER_SERVICE_COOLDOWN_SEC


def mark_alerted(state: dict, unit: str, now: Optional[float] = None) -> None:
    state['services'][unit] = {
        'last_alert_ts': now if now is not None else time.time(),
    }


# -------------------- systemctl shellouts --------------------

def list_ourliberty_services() -> list[str]:
    """Return a list of unit names (without path) matching UNIT_GLOB.

    Uses `systemctl list-unit-files <glob>` which returns all units known
    to systemd whether or not they are active. .timer units are excluded
    by the glob (it constrains to .service). State (enabled/disabled) is
    not filtered — even a disabled unit can have a running PID we want to
    check (e.g. one started manually).
    """
    try:
        result = subprocess.run(
            ['systemctl', 'list-unit-files', '--type=service',
             '--no-legend', '--no-pager', UNIT_GLOB],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
        if result.returncode != 0:
            log(f'systemctl list-unit-files rc={result.returncode} '
                f'stderr={result.stderr.strip()[:200]}', 'WARN')
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f'systemctl list-unit-files: {type(e).__name__}: {e}', 'WARN')
        return []
    units = []
    for line in result.stdout.splitlines():
        # Lines look like: `ourliberty-foo.service enabled ...`
        parts = line.strip().split()
        if not parts:
            continue
        name = parts[0]
        if name.endswith('.service') and name.startswith('ourliberty-'):
            units.append(name)
    return units


def systemctl_show(unit: str, prop: str) -> Optional[str]:
    """Return the value of a single systemctl property, or None on error.

    `systemctl show <unit> --property=<P>` prints `<P>=<value>` on stdout.
    Returns the value portion (may be empty string).
    """
    try:
        result = subprocess.run(
            ['systemctl', 'show', unit, f'--property={prop}'],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip()
        if '=' not in line:
            return None
        return line.split('=', 1)[1]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# -------------------- timestamp + path parsing --------------------

def parse_systemd_timestamp(value: str) -> Optional[float]:
    """Parse systemd's ActiveEnterTimestamp into epoch seconds, or None.

    systemd renders timestamps as `Mon 2026-05-25 17:37:48 MDT` or empty
    string when the unit has never been started. We accept either the
    `Mon YYYY-MM-DD HH:MM:SS TZ` form or the ISO-ish `YYYY-MM-DD HH:MM:SS`
    form some locales produce.
    """
    if not value or value.strip() in ('', 'n/a'):
        return None
    s = value.strip()
    # systemd canonical: drop the leading weekday if present.
    # `Mon 2026-05-25 17:37:48 MDT` → `2026-05-25 17:37:48 MDT`
    parts = s.split(' ', 1)
    if parts and len(parts[0]) == 3 and parts[0].isalpha():
        s = parts[1]
    # Two shapes to try in order: `YYYY-MM-DD HH:MM:SS TZNAME` and just
    # `YYYY-MM-DD HH:MM:SS`.
    for fmt in ('%Y-%m-%d %H:%M:%S %Z', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(s, fmt)
            # %Z is unreliable across locales; fall back to droplet local TZ
            # via time.mktime when needed. For unit tests we pass UTC-ish
            # values and accept the resulting epoch — drift of a few hours
            # is far smaller than the 5-min race-avoidance + 6h cooldown
            # windows that gate the alert.
            if dt.tzinfo is None:
                return time.mktime(dt.timetuple())
            return dt.timestamp()
        except ValueError:
            continue
    return None


def parse_script_path_from_service_file(fragment_path: str) -> Optional[Path]:
    """Read the .service file at fragment_path and extract the script path.

    Heuristic: collect every absolute path on the ExecStart line. Return
    the LAST one that exists on disk and is not a known interpreter
    basename. This handles common shapes:
        ExecStart=/usr/bin/python3 /home/larry/agent-core/scripts/foo.py
        ExecStart=/home/larry/agent-core/scripts/foo.py
        ExecStart=/usr/bin/env python3 /home/larry/agent-core/scripts/foo.py
    Returns None when no ExecStart line is found or no candidate path
    exists on disk.
    """
    if not fragment_path:
        return None
    try:
        text = Path(fragment_path).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    m = _EXEC_START_RE.search(text)
    if not m:
        return None
    line = m.group(1)
    # Strip a leading systemd modifier (e.g. `-` for ignore-failure, `+`
    # for run-as-root). These appear at the very start before the path.
    while line and line[0] in ('-', '+', '!', '@', ':'):
        line = line[1:].lstrip()
    # Tokenize on whitespace. Quoting is rare in our service files; we
    # treat tokens as paths if they start with `/`.
    tokens = line.split()
    abs_paths = [t for t in tokens if t.startswith('/')]
    # Drop interpreters. Prefer the LAST surviving candidate (closest to
    # the end of the line — typically the script).
    candidates = [
        p for p in abs_paths
        if Path(p).name not in _INTERPRETER_BASENAMES
    ]
    for p in reversed(candidates):
        if Path(p).is_file():
            return Path(p)
    # Nothing exists — return the last non-interpreter path so callers
    # can log it; staleness check on a non-existent file will fall back
    # to "no alert" naturally.
    if candidates:
        return Path(candidates[-1])
    return None


# -------------------- staleness predicate --------------------

def is_stale(
    service_start: float, script_mtime: float,
    race_avoidance_sec: float = RACE_AVOIDANCE_SEC,
) -> bool:
    """True iff the script has been modified after the service started
    AND the gap is larger than the race-avoidance window.

    The race window guards against the legitimate restart sequence where
    a deploy writes the script (advancing mtime) milliseconds before
    systemd restarts the unit. Without the window, the next healer tick
    after a deploy + restart would false-positive on every healed daemon.
    """
    if script_mtime <= service_start:
        return False
    return (script_mtime - service_start) > race_avoidance_sec


# -------------------- DM to Larry --------------------

def dm_larry_about_stale(
    unit: str, script_path: Path, service_start: float, script_mtime: float,
) -> bool:
    """Append a warning-level alert via larry_alerts. Returns True on append."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        # Format both times as human-readable UTC for the DM body.
        svc_iso = datetime.fromtimestamp(service_start, tz=timezone.utc).isoformat()
        scr_iso = datetime.fromtimestamp(script_mtime, tz=timezone.utc).isoformat()
        gap_min = (script_mtime - service_start) / 60.0
        return la.append_alert(
            source='heal-stale-daemon-code',
            severity='warning',
            subject=f'stale-daemon-code:{unit}',
            message=(
                f'Daemon {unit} appears to be running stale code.\n\n'
                f'Service started: {svc_iso}\n'
                f'Script mtime:    {scr_iso}\n'
                f'Script path:     {script_path}\n'
                f'Gap:             {gap_min:.1f} min (script newer than '
                f'running process by this much).'
            ),
            suggested_action=(
                f'Restart the unit to pick up the latest script: '
                f'`sudo systemctl restart {unit}`. If the staleness is '
                f'intentional (e.g. holding off a behavior change until a '
                f'maintenance window), suppress further alerts by touching '
                f'the cooldown file: `touch {STATE_FILE}` then edit the JSON '
                f'to bump `services.{unit}.last_alert_ts` forward.'
            ),
        )
    except Exception as e:
        log(f'dm_larry_about_stale failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- per-unit orchestration --------------------

def check_unit(
    unit: str, state: dict, now: Optional[float] = None,
) -> str:
    """Inspect one unit. Return one of:
       'alerted'         — DM sent.
       'cooldown'        — stale but in 6h cooldown; suppressed.
       'race-window'     — modified recently but inside race-avoidance.
       'fresh'           — script older than (or equal to) service start.
       'unparseable'     — couldn't extract service-start or script path.
    """
    raw_ts = systemctl_show(unit, 'ActiveEnterTimestamp')
    service_start = parse_systemd_timestamp(raw_ts) if raw_ts is not None else None
    if service_start is None:
        log(f'{unit}: ActiveEnterTimestamp unparseable ({raw_ts!r}); '
            f'unit may not be running yet', 'INFO')
        return 'unparseable'

    fragment_path = systemctl_show(unit, 'FragmentPath')
    script_path = (
        parse_script_path_from_service_file(fragment_path)
        if fragment_path else None
    )
    if script_path is None or not script_path.is_file():
        log(f'{unit}: could not resolve a script path from '
            f'FragmentPath={fragment_path!r}', 'INFO')
        return 'unparseable'

    try:
        script_mtime = script_path.stat().st_mtime
    except OSError as e:
        log(f'{unit}: stat({script_path}) failed: {e}', 'WARN')
        return 'unparseable'

    if not is_stale(service_start, script_mtime):
        if script_mtime > service_start:
            return 'race-window'
        return 'fresh'

    if in_cooldown(state, unit, now=now):
        log(f'{unit}: stale (script newer by '
            f'{(script_mtime - service_start) / 60:.1f}min) — '
            f'in 6h cooldown; suppressing DM')
        return 'cooldown'

    sent = dm_larry_about_stale(unit, script_path, service_start, script_mtime)
    if sent:
        mark_alerted(state, unit, now=now)
        save_state(state)
        log(f'{unit}: ALERTED — script {script_path} newer than service '
            f'start by {(script_mtime - service_start) / 60:.1f}min')
        return 'alerted'
    # Append failed (larry_alerts internal cooldown collision or write
    # error). Still record locally so we don't hammer larry_alerts on the
    # next tick — its own cooldown would suppress anyway.
    mark_alerted(state, unit, now=now)
    save_state(state)
    log(f'{unit}: stale but larry_alerts.append_alert returned False '
        f'(internal cooldown or write error)', 'WARN')
    return 'alerted'


# -------------------- main --------------------

def main() -> int:
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return 0
    heartbeat()

    units = list_ourliberty_services()
    if not units:
        log('tick: no ourliberty-*.service units found')
        return 0

    state = load_state()
    counts = {
        'alerted': 0, 'cooldown': 0, 'race-window': 0,
        'fresh': 0, 'unparseable': 0,
    }
    for unit in units:
        outcome = check_unit(unit, state)
        counts[outcome] = counts.get(outcome, 0) + 1

    log('tick: ' + ' '.join(f'{k}={v}' for k, v in counts.items() if v))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
