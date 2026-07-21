#!/usr/bin/env python3
"""heal_stale_daemon_code.py — detect + auto-restart stale daemons.

Chain-discipline-v2 (2026-05-25); auto-restart added 2026-05-26. Surfaces
and fixes the failure mode where a long-running daemon's script is merged
to main + synced to disk but the systemd-managed process is still running
pre-merge code. Canonical case: PR #103's marker-parser fix landed on disk
at 19:16Z but `ourliberty-outbox-notifier.service` had been running since
17:37 MDT prior day and continued executing the stale module until manual
restart at 17:37 MDT 2026-05-25. Today (2026-05-26) the same shape
appeared twice within hours after PR #114 and PR #118 merges, and Larry
had to manually restart both times — this revision absorbs that loop.

What this DOES NOT do:
  - Restart units it hasn't detected as stale via the mtime predicate.
  - Retry a `systemctl restart` that returned non-zero (broken units
    don't get hammered; one attempt per 30-min cycle, DM Larry instead).
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
       > RACE_AVOIDANCE_SEC: invoke `sudo -n systemctl restart <unit>`
       and DM Larry the outcome (success body includes unit, mtime,
       pre-restart timestamp, best-effort PR list inferred from git log;
       failure body includes stderr + manual recovery command — no retry).
    5. Per-unit 30-min restart cooldown prevents loops; post-cooldown
       still-stale state escalates to a manual-investigation DM (6h
       cooldown on that DM via larry_alerts subject discipline).

  Restart + DM cooldowns share `~/agents/state/heal-stale-daemon-code-cooldowns.json`
  (`last_restart_ts` and the existing `last_alert_ts` field).

Safe-by-construction:
  - `sudo -n` (non-interactive): errors fast if sudoers ever revokes
    passwordless access, surfaces as `auto-restart-failed:<unit>` DM
    rather than wedging the systemd one-shot.
  - Kill-switch aware: ~/agents/healers.disabled disables BOTH detection
    AND restart for free (`main()` short-circuits before the per-unit loop).
  - Per-unit 30-min restart cooldown bounds blast radius to one restart
    per unit per cycle. Failure path is no-retry.
  - Idempotent: re-running with no new staleness is a no-op.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_work_in_flight  # noqa: E402  (shared live-session predicate, #994)
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-stale-daemon-code.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-stale-daemon-code.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-stale-daemon-code-cooldowns.json'

# Optional per-service metadata overlay. Auto-discovery via
# `list_ourliberty_services()` is the SOLE source of truth for which units
# get scanned; this overlay only supplies per-service thresholds and the
# `restart_strategy` enum for entries that appear. Absent file or absent
# entry → defaults (= today's behavior identically). The path is
# overridable for tests; the env var also lets a future ad-hoc test
# substitute a fixture without monkey-patching the constant.
OVERRIDE_CONFIG_PATH = Path(os.environ.get(
    'OURLIBERTY_STALE_DAEMON_OVERRIDES',
    '/home/larry/agent-core/config/tracked-services.json',
))

# Valid values for the `restart_strategy` enum in the override config.
# Unrecognized values fall back to 'auto' (= today's behavior) so a typo
# in the config cannot silently disable the auto-restart path.
_VALID_RESTART_STRATEGIES = ('auto', 'dm-only', 'never')

# Sub-shell timeout for systemctl calls. systemctl show is fast (<100 ms),
# but we cap it so a hung dbus doesn't wedge the healer tick.
SYSTEMCTL_TIMEOUT_S = 10

# Race-avoidance window. A daemon that's been running for less than this
# duration since the script's mtime is treated as "the restart probably
# just happened; not stale yet." Five minutes is enough to cover a normal
# systemd restart cycle plus typical apt/pip warmup; less than this and
# we'd alert during legitimate restart sequences.
RACE_AVOIDANCE_SEC = 5 * 60

# Alias used by the per-service overrides mechanism so the config field
# name (`stale_threshold_seconds`) reads cleanly. Identical value; pointing
# at the same constant keeps the default behavior interpretable.
DEFAULT_STALE_THRESHOLD_SECONDS = RACE_AVOIDANCE_SEC

# Per-service DM cooldown — don't DM about a chronically still-stale unit
# (post-auto-restart escalation case) more than once per window. 6h
# matches the original DM-only behavior pre-2026-05-26 and is larger than
# larry_alerts' built-in 60-min cooldown so this is the binding constraint
# for the `still-stale-after-restart:<unit>` subject. Successful-restart
# and restart-failure DMs use larry_alerts' own subject-keyed 60-min
# cooldown (one DM per outcome shape per hour is the right rate when the
# event itself is rare and informational).
PER_SERVICE_COOLDOWN_SEC = 6 * 60 * 60

# Per-unit restart cooldown — don't auto-restart the same unit twice
# within this window. 30 min matches the healer timer cadence (one tick =
# one restart attempt, max). If a restart fails OR the unit is still
# stale at the next tick (script regressing, deploy looping, broken
# unit), we suppress the second attempt and escalate to a manual-
# investigation DM instead of hammering the unit.
RESTART_COOLDOWN_SEC = 30 * 60

# Grace window distinguishing a failed prior restart from a NEW deploy that
# landed after the last restart, while still inside the 30-min restart
# cooldown. If a script's mtime is newer than last_restart_ts by more than
# this many seconds, it's a fresh post-restart deploy the running process
# hasn't picked up yet → re-restart immediately instead of escalating to a
# false "still stale after restart" DM. The ~30s grace absorbs sync/restart
# timing races so the very deploy that triggered the restart (whose mtime
# can land a few seconds after last_restart_ts) is NOT misread as a new one.
FRESH_DEPLOY_GRACE_SEC = 30

# Subprocess timeout for `sudo -n systemctl daemon-reload`. daemon-reload
# is fast (re-parses unit files); 30s is comfortably above realistic
# duration and bounds the healer tick time.
RESTART_TIMEOUT_S = 30

# The restart itself is issued with `--no-block` (returns as soon as the
# job is enqueued, so it does NOT wait for a slow SIGTERM drain). After a
# brief settle we verify the unit's state via `systemctl is-active`: a
# unit that is `active`/`activating` is succeeding even if it's still
# draining the old process, so we treat that as success and never emit a
# false `auto-restart-failed:<unit>` DM. Only `failed`/`inactive` (or an
# is-active error) is a genuine failure.
RESTART_SETTLE_S = 3

# States that mean the restart is succeeding or has succeeded. systemd's
# `is-active` prints one of: active / activating / reloading / deactivating
# / inactive / failed. `reloading` and `deactivating` are transient states
# on the way to active, so they count as in-progress success too.
_HEALTHY_ACTIVE_STATES = frozenset(
    {'active', 'activating', 'reloading', 'deactivating'}
)

# Unit glob. `ourliberty-*.service` (NOT .timer; timers don't have
# ExecStart pointing at code — they activate the underlying .service).
UNIT_GLOB = 'ourliberty-*.service'

# orchestrator-rectification-v2 V4 (2026-05-28). Multi-daemon-restart
# shape: a shared library imported by N long-running daemons mutates on
# merge, but the daemons keep running pre-merge module bytes until each
# is individually restarted. Bootstrap-002 verified this empirically —
# `scripts/dispatch_validator.py` changes from PR #145 didn't take effect
# until beacon-bot, outbox-notifier, AND inbox-watcher all restarted.
#
# Each key is an absolute path to a script in this repo's `scripts/`.
# Each value is the set of systemd units that import that script (directly
# or transitively) and must be restarted when the script's mtime moves
# forward on disk. The runbook
# `runbooks/post-merge-daemon-restart-discipline.md` documents the
# mapping in prose; this dict is the executable enforcement.
#
# Resolution rule per (lib, unit) pair on each tick: when
# `script_mtime > service_start + RACE_AVOIDANCE_SEC`, treat the unit as
# stale and route it through the same auto-restart path as a
# direct-script staleness hit. The per-unit 30-min restart cooldown is
# shared with the direct-script path — a unit won't be restarted twice
# in the same window regardless of which path detected the staleness.
#
# Path resolution: `_SCRIPTS_DIR` is computed lazily so test fixtures can
# point the watchlist at a tmp directory by monkey-patching this constant
# before the first `check_shared_lib_watchlist` call.
_SCRIPTS_DIR = Path(__file__).resolve().parent

SHARED_LIB_WATCHLIST: dict[Path, set[str]] = {
    _SCRIPTS_DIR / 'dispatch_validator.py': {
        'ourliberty-beacon-bot.service',
        'ourliberty-outbox-notifier.service',
        'ourliberty-inbox-watcher.service',
    },
    _SCRIPTS_DIR / 'safe_write_inbox.py': {
        'ourliberty-beacon-bot.service',
        'ourliberty-outbox-notifier.service',
        'ourliberty-inbox-watcher.service',
    },
    _SCRIPTS_DIR / 'routing_validator.py': {
        'ourliberty-beacon-bot.service',
        'ourliberty-outbox-notifier.service',
        'ourliberty-inbox-watcher.service',
    },
    _SCRIPTS_DIR / 'marker.py': {
        'ourliberty-beacon-bot.service',
        'ourliberty-outbox-notifier.service',
        'ourliberty-inbox-watcher.service',
    },
}

# deploy-restart-gap-001 (2026-06-03). The committed restart manifest
# (config/daemon-restart-manifest.json) maps each persistent Type=simple
# daemon to the repo paths whose change must restart it — the transitive
# first-party import closure of its entrypoint. The healer folds that
# manifest into the watchlist so a daemon whose imported module changed
# (but whose entrypoint did not) is caught even if the deploy-side restart
# in sync_agent_core.sh somehow missed it. The hardcoded SHARED_LIB_WATCHLIST
# above is retained and UNIONED in: it covers subprocess-invoked deps (e.g.
# marker.py) that an import scan can't see. Manifest path is env-overridable
# for tests.
MANIFEST_PATH = Path(os.environ.get(
    'OURLIBERTY_DAEMON_RESTART_MANIFEST',
    str(_SCRIPTS_DIR.parent / 'config' / 'daemon-restart-manifest.json'),
))


def load_manifest_watchlist() -> dict[Path, set[str]]:
    """Invert the committed restart manifest into {abs_path: {units}}.

    Repo-relative `watch_paths` (e.g. `scripts/foo.py`) are resolved against
    the repo root (`_SCRIPTS_DIR.parent`). Returns {} on any read/parse
    failure (fail-open: the healer keeps using the hardcoded watchlist, i.e.
    behavior identical to pre-extension)."""
    repo_root = _SCRIPTS_DIR.parent
    try:
        data = json.loads(MANIFEST_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    units = data.get('units', {})
    if not isinstance(units, dict):
        return {}
    inverse: dict[Path, set[str]] = {}
    for unit, spec in units.items():
        if not isinstance(spec, dict):
            continue
        for rel in spec.get('watch_paths', []):
            if not isinstance(rel, str):
                continue
            inverse.setdefault((repo_root / rel).resolve(), set()).add(unit)
    return inverse


def build_effective_watchlist() -> dict[Path, set[str]]:
    """Union of the hardcoded SHARED_LIB_WATCHLIST and the manifest-derived
    watchlist. The manifest is the source of truth for import-closure
    staleness; the hardcoded entries add subprocess-dep coverage the import
    scan cannot derive. Union can only widen coverage, never narrow it."""
    effective: dict[Path, set[str]] = {
        path: set(units) for path, units in SHARED_LIB_WATCHLIST.items()
    }
    for path, units in load_manifest_watchlist().items():
        effective.setdefault(path, set()).update(units)
    return effective

# Conservative ExecStart parser. Match lines that look like:
#   ExecStart=<interpreter> <args> <script-path>
#   ExecStart=<script-path> <args>
# We extract every absolute path in the line and return the LAST one
# that exists and is not the interpreter itself. Multi-line ExecStart
# (with `\`) is not used in this codebase's units; if it appears, the
# heuristic will fall back to whatever path is on the first line.
_EXEC_START_RE = re.compile(r'^\s*ExecStart\s*=\s*(.+?)\s*$', re.MULTILINE)

# WorkingDirectory= line. Needed for the `-m uvicorn <dotted.module>:<attr>`
# ExecStart shape, where the script path is not on the ExecStart line — it
# has to be reconstructed from the dotted module + the unit's working dir.
_WORKING_DIR_RE = re.compile(
    r'^\s*WorkingDirectory\s*=\s*(.+?)\s*$', re.MULTILINE,
)

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
        atomic_write_json(STATE_FILE, state, indent=2)
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
    # Merge into existing entry so we don't clobber last_restart_ts.
    entry = state['services'].setdefault(unit, {})
    entry['last_alert_ts'] = now if now is not None else time.time()


def in_restart_cooldown(
    state: dict, unit: str, now: Optional[float] = None,
) -> bool:
    entry = state['services'].get(unit)
    if not entry:
        return False
    last = entry.get('last_restart_ts')
    if not isinstance(last, (int, float)):
        return False
    now = now if now is not None else time.time()
    return (now - last) < RESTART_COOLDOWN_SEC


def mark_restarted(
    state: dict, unit: str, now: Optional[float] = None,
) -> None:
    entry = state['services'].setdefault(unit, {})
    entry['last_restart_ts'] = now if now is not None else time.time()


# -------------------- per-service override loader --------------------

def load_service_overrides() -> dict:
    """Return the per-service overrides mapping, or {} on any failure.

    Schema (read from `config/tracked-services.json`):
        {
          "_meta": {...},
          "services": {
            "ourliberty-foo.service": {
              "stale_threshold_seconds": 600,
              "restart_strategy": "auto"  # or "dm-only" / "never"
            },
            ...
          }
        }

    Fail-open: FileNotFoundError, malformed JSON, wrong top-level shape, or
    any other read error returns `{}` — every service falls back to the
    DEFAULT_STALE_THRESHOLD_SECONDS + `auto` strategy (= behavior identical
    to pre-extension). Tests R1 + R6 lock this invariant.
    """
    try:
        data = json.loads(OVERRIDE_CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log(f'tracked-services.json unreadable ({type(e).__name__}: {e}); '
            f'falling back to defaults', 'WARN')
        return {}
    if not isinstance(data, dict):
        log(f'tracked-services.json top-level is not a dict; '
            f'falling back to defaults', 'WARN')
        return {}
    services = data.get('services', {})
    if not isinstance(services, dict):
        log(f'tracked-services.json `services` is not a dict; '
            f'falling back to defaults', 'WARN')
        return {}
    return services


def _resolve_override(
    overrides: dict, unit: str,
) -> tuple[float, str]:
    """Return (threshold_seconds, strategy) for `unit`.

    Looks up the exact unit name (including the `.service` suffix that
    list_ourliberty_services() returns). Unknown unit → defaults. Bad
    threshold type → default threshold (with a WARN log). Bad strategy
    value → 'auto' (preserves today's auto-restart behavior on typos).
    """
    entry = overrides.get(unit, {})
    if not isinstance(entry, dict):
        return DEFAULT_STALE_THRESHOLD_SECONDS, 'auto'
    raw_threshold = entry.get('stale_threshold_seconds',
                              DEFAULT_STALE_THRESHOLD_SECONDS)
    if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float)):
        log(f'{unit}: stale_threshold_seconds is not a number; '
            f'using default', 'WARN')
        threshold = float(DEFAULT_STALE_THRESHOLD_SECONDS)
    else:
        threshold = float(raw_threshold)
    strategy = entry.get('restart_strategy', 'auto')
    if strategy not in _VALID_RESTART_STRATEGIES:
        log(f'{unit}: restart_strategy={strategy!r} unrecognized; '
            f'using `auto`', 'WARN')
        strategy = 'auto'
    return threshold, strategy


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
    Also handles the uvicorn module-mode shape:
        ExecStart=/usr/bin/env python3 -m uvicorn scripts.dashboard_api:app ...
    by resolving <dotted.module> against the unit's WorkingDirectory.
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

    # Uvicorn module-mode: `python3 -m uvicorn <pkg>.<mod>:<attr> ...`. The
    # script path lives in WorkingDirectory + dotted module → file. Scope:
    # `-m uvicorn` only (other launchers like gunicorn/flask are deferred);
    # two-segment `<pkg>.<mod>:<attr>` only (three-segment leaf-attribute
    # forms are out of scope until a real daemon needs them).
    uvicorn_path = _resolve_uvicorn_script_path(line, text)
    if uvicorn_path is not None:
        return uvicorn_path

    # Nothing exists — return the last non-interpreter path so callers
    # can log it; staleness check on a non-existent file will fall back
    # to "no alert" naturally.
    if candidates:
        return Path(candidates[-1])
    return None


def _resolve_uvicorn_script_path(
    exec_start_line: str, fragment_text: str,
) -> Optional[Path]:
    """Resolve `python3 -m uvicorn <pkg>.<mod>:<attr>` to an on-disk path.

    Returns the resolved Path only if it exists on disk; otherwise None so
    the caller falls through to the existing `could not resolve` branch
    rather than returning a phantom path.
    """
    try:
        argv = shlex.split(exec_start_line)
    except ValueError:
        return None
    if '-m' not in argv:
        return None
    m_idx = argv.index('-m')
    if m_idx + 1 >= len(argv) or argv[m_idx + 1] != 'uvicorn':
        return None
    if m_idx + 2 >= len(argv):
        return None
    app_target = argv[m_idx + 2]
    module_part = app_target.split(':', 1)[0]
    if not module_part:
        return None
    rel_path = Path(*module_part.split('.')).with_suffix('.py')
    wd_match = _WORKING_DIR_RE.search(fragment_text)
    if not wd_match:
        return None
    working_dir = wd_match.group(1).strip()
    if not working_dir:
        return None
    resolved = Path(working_dir) / rel_path
    if resolved.is_file():
        return resolved
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


# -------------------- restart cordon + drain (spec: restart-inflight-guard) ---

# Units that HOST Claude agent loops, i.e. where a restart SIGTERMs paid work
# mid-session. Only these get cordon-and-drain. The *-bot units (telegram
# front-ends) and dashboard-api host no sessions and must keep restarting
# immediately — a needless drain wait on them would just delay a fix.
# Must match inbox_watcher.CORDON_UNIT, which is what the watcher looks for.
AGENT_HOSTING_UNITS = frozenset({'ourliberty-inbox-watcher.service'})

# Short by design. A cordon is dangerous exactly in proportion to how long a
# DEAD healer's copy survives: while it stands, the watcher accepts no work, so
# an abandoned cordon is a silent full-queue stall. We keep the TTL short and
# RENEW while draining instead of buying headroom with a long TTL — the same
# trade dispatch_lease makes (180s TTL / 60s heartbeat).
CORDON_TTL_SEC = int(os.environ.get('OL_CORDON_TTL_SEC', '600'))
CORDON_RENEW_EVERY_SEC = max(1, CORDON_TTL_SEC // 3)
DRAIN_POLL_SEC = int(os.environ.get('OL_DRAIN_POLL_SEC', '15'))

# Backstop only. Normally the drain ends when the running session ends. This
# bounds the pathological case — work that never stops — after which we restart
# anyway and say so loudly. Above REVIEW_SESSION_CEILING_SECONDS (2100s) so a
# single review can never exhaust it; only a continuous queue can.
RESTART_DEFER_CEILING_SEC = int(
    os.environ.get('OL_RESTART_DEFER_CEILING_SEC', '3600')
)


def _cordon_file(unit: str) -> Path:
    """Canonical helper — see agent_work_in_flight.cordon_file for why this must
    NOT be derived locally."""
    return agent_work_in_flight.cordon_file(unit)


def _boot_id() -> str:
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except OSError:
        return ''


def write_cordon(unit: str, reason: str) -> bool:
    """Write/refresh the cordon for <unit>. True iff it landed.

    Called once to raise the cordon and again on every drain poll to push
    ``expires_at`` forward. Renewal is what makes a SHORT ttl safe: a review may
    run to REVIEW_SESSION_CEILING_SECONDS (2100s), 3.5x the 600s TTL, so a
    non-renewed cordon would EXPIRE MID-DRAIN — the watcher would resume taking
    work and we would restart into a live session, reintroducing the exact bug
    this exists to prevent."""
    path = _cordon_file(unit)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {
            'unit': unit,
            'reason': reason,
            'created_at': time.time(),
            'expires_at': time.time() + CORDON_TTL_SEC,
            'pid': os.getpid(),
            'boot_id': _boot_id(),
        })
        return True
    except (OSError, ValueError) as e:
        log(f'cordon write failed for {unit}: {e}', 'WARN')
        return False


def clear_cordon(unit: str) -> None:
    """Remove the cordon. Called from a `finally` on every non-fatal path — a
    cordon we forget to clear stalls dispatch until its TTL."""
    try:
        _cordon_file(unit).unlink()
    except OSError:
        pass


class _CordonSigtermGuard:
    """Clear the cordon if we are SIGTERMed mid-drain.

    Belt-and-braces for the systemd kill path. A `finally` does NOT run when the
    default SIGTERM disposition terminates the process, so a healer killed while
    draining leaves its cordon file behind. Holder-liveness already makes that
    cordon inert within one watcher poll (the pid is dead), so this is not what
    prevents a stall — it removes the corpse promptly instead of leaving a
    confusing file on disk for up to CORDON_TTL_SEC, and it re-raises via the
    previous handler so systemd still sees a normal termination.

    Restores the prior handler on exit; a non-main-thread install is a no-op
    (signal.signal raises there) rather than an error."""

    def __init__(self, unit: str):
        self.unit = unit
        self._prev = None
        self._installed = False

    def __enter__(self):
        try:
            self._prev = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._on_term)
            self._installed = True
        except (ValueError, OSError):
            pass  # not the main thread — holder-liveness still covers us
        return self

    def _on_term(self, signum, frame):
        clear_cordon(self.unit)
        log(f'SIGTERM during drain of {self.unit} — cordon cleared; the restart '
            f'did NOT happen (raise TimeoutStartSec if this repeats)', 'WARN')
        if callable(self._prev):
            self._prev(signum, frame)
        else:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

    def __exit__(self, *exc):
        if self._installed:
            try:
                signal.signal(signal.SIGTERM, self._prev)
            except (ValueError, OSError):
                pass
        return False


def drain_agent_work(unit: str, reason: str) -> tuple[bool, float]:
    """Hold the cordon until no agent session is live. Return (drained, waited).

    ``drained`` False means we hit RESTART_DEFER_CEILING_SEC with work still
    running — the caller restarts anyway and logs it loudly.

    The cordon is raised BEFORE the first liveness probe, which is what makes
    this terminate: with new dispatch blocked, in-flight can only fall to zero.
    Probing first and cordoning after would race the watcher's 5s poll forever
    on a busy queue."""
    started = time.time()
    last_renew = started
    while True:
        if not agent_work_in_flight.agent_work_in_flight():
            return True, time.time() - started
        waited = time.time() - started
        if waited >= RESTART_DEFER_CEILING_SEC:
            return False, waited
        time.sleep(DRAIN_POLL_SEC)
        if time.time() - last_renew >= CORDON_RENEW_EVERY_SEC:
            write_cordon(unit, reason)  # push expires_at forward
            last_renew = time.time()


# -------------------- auto-restart + PR-inference helpers --------------------

def auto_restart_unit(unit: str, reason: str = 'stale-shared-lib') -> tuple[int, str]:
    """Restart <unit> and verify it came back. Return (returncode, stderr).

    For AGENT_HOSTING_UNITS this first cordons the unit and waits for its live
    Claude session to finish, so the restart lands in a quiet window instead of
    SIGTERMing paid work mid-review (#971). Every other unit restarts
    immediately, exactly as before.

    Prepended with `sudo -n systemctl daemon-reload` so a restart picks up
    unit-file edits that landed since systemd last parsed them. Without the
    daemon-reload, a `.timer` whose unit file changed mid-deploy comes back
    up with stale config and (for timers specifically) hits the infinity
    trap — `NextElapseUSecRealtime` empty, `NextElapseUSecMonotonic=infinity`.

    A failed daemon-reload is logged WARN but never blocks the restart: a
    stale-but-running daemon is better than a stopped daemon.

    The restart is issued with `--no-block` and confirmed via `is-active`
    after a short settle, NOT by waiting on the restart subprocess. Units
    running Claude agent loops take ~90s to drain on SIGTERM, far past any
    sane subprocess timeout; blocking on that drain produced false
    `auto-restart-failed:<unit>` DMs even though systemd completed the
    restart in the background. So the rc contract is now:
      - (0, '')         — restart enqueued AND the unit is active/activating
                          after the settle (success; no failure DM).
      - (rc, stderr)    — the restart job itself was rejected (rc != 0,
                          e.g. unit not found) — a genuine, drain-independent
                          failure.
      - (-1, descriptive) — restart enqueued but the unit is NOT
                          active/activating after the settle, or the state
                          couldn't be verified — genuine failure (DM fires).

    Sudoers contract on the droplet is `(ALL) NOPASSWD: ALL`; -n errors
    immediately if that ever changes rather than blocking on a prompt.
    """
    if unit not in AGENT_HOSTING_UNITS:
        return _restart_unit_now(unit)

    # Cordon → drain → restart → uncordon.
    if not write_cordon(unit, reason):
        # Could not raise the cordon (disk full, perms). Restarting anyway is
        # exactly today's behavior — no worse — and refusing would let a stale
        # daemon run indefinitely on an unrelated IO fault. Never silent.
        log(f'watchlist: cordon unavailable for {unit}; restarting WITHOUT '
            f'drain (pre-#994 behavior — a live session may be killed)', 'WARN')
        return _restart_unit_now(unit)
    try:
        with _CordonSigtermGuard(unit):
            drained, waited = drain_agent_work(unit, reason)
        if drained:
            log(f'RESTART_DRAINED unit={unit} waited={waited:.0f}s '
                f'reason={reason} — restarting into a quiet window', 'INFO')
        else:
            # Backstop: work never stopped. Restart anyway (staleness must
            # eventually win) but make it loud — this is the one path that can
            # still kill a live session.
            log(f'RESTART_FORCED_OVER_CEILING unit={unit} '
                f'drained_for={waited:.0f}s ceiling={RESTART_DEFER_CEILING_SEC}s '
                f'reason={reason} — restarting with work STILL in flight', 'WARN')
        return _restart_unit_now(unit)
    finally:
        # Unconditional: an un-cleared cordon stalls dispatch until its TTL.
        clear_cordon(unit)


def _restart_unit_now(unit: str) -> tuple[int, str]:
    """The original unconditional restart. Split out so cordon-and-drain wraps
    it without duplicating the daemon-reload / settle / verify contract."""
    try:
        reload_result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'daemon-reload'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
        if reload_result.returncode != 0:
            log(
                f'daemon-reload before restart of {unit} failed '
                f'rc={reload_result.returncode} '
                f'stderr={(reload_result.stderr or "").strip()!r}; '
                f'proceeding with restart anyway',
                'WARN',
            )
    except subprocess.TimeoutExpired:
        log(
            f'daemon-reload before restart of {unit} timed out after '
            f'{RESTART_TIMEOUT_S}s; proceeding with restart anyway',
            'WARN',
        )
    except FileNotFoundError:
        log(
            f'daemon-reload before restart of {unit}: sudo or systemctl '
            f'not found in PATH; proceeding with restart anyway',
            'WARN',
        )

    # Issue the restart with `--no-block` so the subprocess returns as soon
    # as systemd enqueues the job, rather than blocking on a slow SIGTERM
    # drain (units running Claude agent loops take ~90s to drain — well past
    # any sane subprocess timeout). With --no-block, the restart rc no longer
    # proves recovery, so the is-active verify below is the source of truth.
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', '--no-block', unit],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Even --no-block timed out (systemd itself unresponsive). Fall
        # through to the verify — the unit may still have come up.
        result = None
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'

    if result is not None and result.returncode != 0:
        # The restart job was rejected up front (e.g. unit not found). This
        # is a genuine failure independent of drain timing; report it.
        return result.returncode, (result.stderr or '').strip()

    # Brief settle, then verify the unit is (becoming) active. This is what
    # makes a slow-draining restart report success instead of a false
    # failure: systemd completes the restart in the background.
    time.sleep(RESTART_SETTLE_S)
    state = _unit_active_state(unit)
    if state in _HEALTHY_ACTIVE_STATES:
        log(
            f'RESTART_SLOW_DRAIN: {unit} restart issued with --no-block; '
            f'service is {state} after {RESTART_SETTLE_S}s settle — systemd '
            f'completing restart in background, treating as success',
            'INFO',
        )
        return 0, ''

    # Not in a healthy state after the settle — but before declaring failure,
    # check whether systemd still has a restart job enqueued for this unit. A
    # non-empty `Job=` means the restart hasn't run yet: it's typically held
    # behind an After= ordering dependency that is still draining (e.g.
    # outbox-notifier ordered After= inbox-watcher, whose Claude loop takes
    # ~90s to SIGTERM-drain). The restart is in-progress and WILL complete
    # once the predecessor is up, so this is a slow start, not a failure.
    job = _unit_pending_job(unit)
    if job:
        log(
            f'RESTART_QUEUED_PENDING_ORDERING: {unit} is {state!r} after '
            f'{RESTART_SETTLE_S}s settle but systemd still has restart job '
            f'{job!r} enqueued (held behind an After= ordering dependency '
            f'still draining) — treating as in-progress success',
            'INFO',
        )
        return 0, ''

    # Unhealthy state AND no pending job → genuine failure. Surface the
    # observed state so the failure DM is actionable.
    return -1, (
        f'restart issued but unit is {state!r} after {RESTART_SETTLE_S}s '
        f'(expected active/activating)'
    )


def _unit_active_state(unit: str) -> str:
    """Return `systemctl is-active <unit>`'s state string (stdout).

    `is-active` prints the unit's high-level state (active / activating /
    failed / inactive / ...) to stdout and uses the exit code only as a
    boolean for `active`. We parse stdout so transient states are visible.
    Returns 'unknown' on timeout / missing binary so the caller treats an
    unverifiable restart as a failure (DM fires) rather than a false success.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
        return (result.stdout or '').strip() or 'unknown'
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 'unknown'


def _unit_pending_job(unit: str) -> str:
    """Return the unit's enqueued systemd job id, or '' if none.

    `systemctl show <unit> --property=Job` prints `Job=` (empty) for a settled
    unit and `Job=<id> <...>` while a job (e.g. a restart) is enqueued and
    waiting — typically held behind an After= ordering dependency that is still
    draining. We parse the value after `Job=`. Returns '' on empty / timeout /
    missing binary so the caller only treats a CONFIRMED pending job as
    in-progress (and an unhealthy unit with no pending job as a real failure).
    """
    try:
        result = subprocess.run(
            ['systemctl', 'show', unit, '--property=Job'],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
        line = (result.stdout or '').strip()
        if line.startswith('Job='):
            return line[len('Job='):].strip()
        return ''
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ''


def infer_recent_prs(script_path: Path, since_iso: str) -> list[str]:
    """Best-effort: return PR-shaped lines from `git log --since=<iso>`.

    Returns [] on any error or no commits — caller treats empty as
    "omit PR list from the DM body cleanly." Format per line:
    `<short-sha> <subject>`. We pass the script path, not the repo, so the
    log is naturally scoped to commits that touched THIS script (which is
    what Larry wants to see when a stale-daemon DM fires).
    """
    try:
        script_path = Path(script_path)
        if not script_path.is_file():
            return []
        result = subprocess.run(
            ['git', 'log', f'--since={since_iso}',
             '--format=%h %s', '--', str(script_path)],
            capture_output=True, text=True, timeout=10,
            cwd=str(script_path.parent),
        )
        if result.returncode != 0:
            return []
        lines = [
            line.strip() for line in result.stdout.splitlines() if line.strip()
        ]
        return lines
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


# -------------------- DM to Larry --------------------

def _import_larry_alerts():
    """Import larry_alerts module lazily (the module is in scripts/ next to us)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import larry_alerts as la  # noqa: E402
    return la


def dm_larry_auto_restarted(
    unit: str, script_path: Path, service_start: float, script_mtime: float,
    pr_lines: list[str], changed_lib_entrypoint: Optional[Path] = None,
) -> bool:
    """Append a warning-level closure alert after a successful auto-restart.

    Two attribution shapes, selected by `changed_lib_entrypoint`:

    - `None` (direct-script path, `check_unit`): `script_path` IS the
      service's own entrypoint and `script_mtime` is its mtime. The body
      labels it `Script path:` — the plain "your own script changed" case.
    - set (shared-lib watchlist path, `_check_watchlist_pair`):
      `script_path`/`script_mtime` describe the CHANGED SHARED LIBRARY the
      service imports, and `changed_lib_entrypoint` is the service's OWN
      entrypoint. The body distinguishes the two on separate, clearly
      labeled lines so the alert never reads as "this service's own script
      is <a library it merely imports>."
    """
    try:
        la = _import_larry_alerts()
        svc_iso = datetime.fromtimestamp(service_start, tz=timezone.utc).isoformat()
        scr_iso = datetime.fromtimestamp(script_mtime, tz=timezone.utc).isoformat()
        gap_min = (script_mtime - service_start) / 60.0
        if changed_lib_entrypoint is not None:
            body = (
                f'Auto-restarted {unit} (imported shared library changed '
                f'{gap_min:.1f} min after this service last started, so it '
                f'was running stale module bytes; new code now live).\n\n'
                f'Service start (pre-restart): {svc_iso}\n'
                f'Library mtime:               {scr_iso}\n'
                f'Changed shared library:      {script_path}\n'
                f'Service entrypoint:          {changed_lib_entrypoint}'
            )
        else:
            body = (
                f'Auto-restarted {unit} (script mtime newer than active-since by '
                f'{gap_min:.1f} min; new code now live).\n\n'
                f'Service start (pre-restart): {svc_iso}\n'
                f'Script mtime:                {scr_iso}\n'
                f'Script path:                 {script_path}'
            )
        if pr_lines:
            body += '\n\nCommits since pre-restart active-start:\n' + '\n'.join(
                f'  {line}' for line in pr_lines
            )
        # Successful auto-restart → route on outcome (fix-first): a routine
        # recovery goes to the digest; a significant one would DM a closure.
        subject = f'auto-restarted:{unit}'
        route = la.classify_route('heal-stale-daemon-code', subject, healed=True)
        return la.append_alert(
            source='heal-stale-daemon-code',
            severity='info',
            subject=subject,
            message=body,
            route=route,
        )
    except Exception as e:
        log(f'dm_larry_auto_restarted failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_larry_auto_restart_failed(
    unit: str, rc: int, stderr: str,
) -> bool:
    """Append a warning-level alert when `sudo -n systemctl restart` fails."""
    try:
        la = _import_larry_alerts()
        body = (
            f'Auto-restart of {unit} FAILED (rc={rc}).\n\n'
            f'systemctl stderr:\n{stderr or "(empty)"}\n\n'
            f'No automatic retry. Manual recovery:\n'
            f'  sudo systemctl restart {unit}'
        )
        return la.append_alert(
            source='heal-stale-daemon-code',
            severity='warning',
            subject=f'auto-restart-failed:{unit}',
            message=body,
            suggested_action=f'sudo systemctl restart {unit}',
        )
    except Exception as e:
        log(f'dm_larry_auto_restart_failed failed: {type(e).__name__}: {e}',
            'WARN')
        return False


def dm_larry_dm_only_stale(
    unit: str, script_path: Path, service_start: float, script_mtime: float,
) -> bool:
    """Alert when a unit is stale AND its override strategy is `dm-only`.

    Unlike the `auto` path which alerts only after a successful restart,
    this path alerts INSTEAD of restarting. Subject is `stale-code:<unit>`
    (distinct namespace from `auto-restarted:<unit>` so the existing
    translation layer + dedup buckets don't collide). Body carries the
    manual recovery command.
    """
    try:
        la = _import_larry_alerts()
        svc_iso = datetime.fromtimestamp(service_start, tz=timezone.utc).isoformat()
        scr_iso = datetime.fromtimestamp(script_mtime, tz=timezone.utc).isoformat()
        gap_min = (script_mtime - service_start) / 60.0
        body = (
            f'Stale code detected for {unit} (script mtime newer than '
            f'active-since by {gap_min:.1f} min).\n\n'
            f'Auto-restart suppressed by config override '
            f'(restart_strategy=dm-only).\n\n'
            f'Service start: {svc_iso}\n'
            f'Script mtime:  {scr_iso}\n'
            f'Script path:   {script_path}\n\n'
            f'Manual recovery: sudo systemctl restart {unit}'
        )
        return la.append_alert(
            source='heal-stale-daemon-code',
            severity='warning',
            subject=f'stale-code:{unit}',
            message=body,
            suggested_action=f'sudo systemctl restart {unit}',
        )
    except Exception as e:
        log(f'dm_larry_dm_only_stale failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_larry_still_stale_after_restart(
    unit: str, last_restart_ts: float,
) -> bool:
    """Append a warning alert when a unit is still stale after the cooldown."""
    try:
        la = _import_larry_alerts()
        restart_iso = datetime.fromtimestamp(
            last_restart_ts, tz=timezone.utc,
        ).isoformat()
        body = (
            f'{unit} still stale after auto-restart at {restart_iso}; '
            f'manual investigation needed.\n\n'
            f'The healer attempted a restart at the timestamp above but the '
            f'script is still newer than the unit\'s ActiveEnterTimestamp. '
            f'No further auto-restart attempts will be made.'
        )
        return la.append_alert(
            source='heal-stale-daemon-code',
            severity='warning',
            subject=f'still-stale-after-restart:{unit}',
            message=body,
            suggested_action=(
                f'Investigate why {unit} did not pick up the new code: '
                f'`journalctl -u {unit} -n 200` and '
                f'`systemctl status {unit}`.'
            ),
        )
    except Exception as e:
        log(f'dm_larry_still_stale_after_restart failed: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- per-unit orchestration --------------------

def check_unit(
    unit: str, state: dict, now: Optional[float] = None,
    overrides: Optional[dict] = None,
) -> str:
    """Inspect one unit. Return one of:
       'auto-restarted'           — restart returned rc=0; closure DM sent.
       'auto-restart-failed'      — restart returned non-zero; failure DM sent.
       'restart-cooldown'         — stale but inside 30-min restart cooldown.
       'still-stale-after-restart' — stale, past cooldown, prior restart did
                                    not help; escalation DM sent (6h-gated).
       'race-window'              — modified recently but inside race-avoidance.
       'fresh'                    — script older than (or equal to) service start.
       'unparseable'              — couldn't extract service-start or script path.
       'strategy-never-skip'      — stale, but config override says `never`; no action.
       'strategy-dm-only'         — stale, override says `dm-only`; alert fired,
                                    no restart attempted.

    `overrides` is the optional per-service metadata mapping from
    `load_service_overrides()`. None / {} → every unit uses defaults
    (= behavior identical to pre-overlay).
    """
    if overrides is None:
        overrides = {}
    threshold, strategy = _resolve_override(overrides, unit)

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

    if not is_stale(service_start, script_mtime, race_avoidance_sec=threshold):
        if script_mtime > service_start:
            return 'race-window'
        return 'fresh'

    # Stale. Apply the per-service strategy gate BEFORE touching restart
    # machinery — `never` and `dm-only` short-circuit the auto-restart path
    # entirely while leaving the default `auto` path verbatim below.
    if strategy == 'never':
        log(f'{unit}: STALE_CODE_SKIP strategy=never reason=config-override',
            'INFO')
        return 'strategy-never-skip'
    if strategy == 'dm-only':
        dm_larry_dm_only_stale(unit, script_path, service_start, script_mtime)
        log(f'{unit}: STALE_CODE_DM_ONLY strategy=dm-only', 'INFO')
        return 'strategy-dm-only'

    # Stale. Decide between (a) inside restart cooldown → escalation-or-suppress
    # vs (b) outside cooldown → attempt restart.
    if in_restart_cooldown(state, unit, now=now):
        last_restart_ts = state['services'].get(unit, {}).get('last_restart_ts')
        # A NEW deploy landing AFTER the last restart is a distinct staleness
        # event, not a failed restart: the running process is on the old code
        # because it was restarted BEFORE this deploy, so re-restart instead
        # of escalating. A genuinely-broken unit had its code on disk before
        # the restart (script_mtime <= last_restart_ts) → fresh_deploy False →
        # escalation path below, no re-restart, so no restart loop is possible.
        fresh_deploy = (
            isinstance(last_restart_ts, (int, float))
            and script_mtime > last_restart_ts + FRESH_DEPLOY_GRACE_SEC
        )
        if not fresh_deploy:
            # Unit was restarted recently AND is still stale — escalate to a
            # manual-investigation DM (gated by the existing 6h subject cooldown
            # so a chronically broken unit doesn't DM every tick).
            if in_cooldown(state, unit, now=now):
                log(f'{unit}: still stale after restart at '
                    f'{last_restart_ts}; in 6h DM cooldown — suppressing '
                    f'escalation DM', 'INFO')
                return 'restart-cooldown'
            sent = dm_larry_still_stale_after_restart(unit, last_restart_ts or 0.0)
            # Mark the alert regardless of larry_alerts internal-cooldown outcome
            # so the healer's own 6h gate is the binding constraint.
            mark_alerted(state, unit, now=now)
            save_state(state)
            if sent:
                log(f'{unit}: STILL STALE AFTER RESTART — escalation DM sent',
                    'WARN')
            else:
                log(f'{unit}: still-stale escalation DM suppressed by '
                    f'larry_alerts internal cooldown or write error', 'WARN')
            return 'still-stale-after-restart'
        # fresh_deploy → fall through to the restart path below, which
        # re-restarts, re-marks last_restart_ts, and returns 'auto-restarted'.
        log(f'{unit}: FRESH_DEPLOY_AFTER_RESTART — script {script_path} '
            f'mtime is {(script_mtime - last_restart_ts):.0f}s newer than '
            f'last_restart_ts ({last_restart_ts}); re-restarting to pick '
            f'up the new deploy despite the restart cooldown', 'INFO')

    # Outside restart cooldown — attempt restart now.
    pre_restart_iso = datetime.fromtimestamp(
        service_start, tz=timezone.utc,
    ).isoformat()
    rc, stderr = auto_restart_unit(unit)
    # Record the attempt under the cooldown clock regardless of outcome.
    # A failed restart STILL gets the 30-min cooldown — we don't hammer
    # broken units between healer ticks.
    mark_restarted(state, unit, now=now)
    save_state(state)

    if rc == 0:
        pr_lines = infer_recent_prs(script_path, pre_restart_iso)
        dm_larry_auto_restarted(
            unit, script_path, service_start, script_mtime, pr_lines,
        )
        log(f'{unit}: AUTO-RESTARTED — script {script_path} newer than '
            f'service start by {(script_mtime - service_start) / 60:.1f}min; '
            f'PRs since pre-restart: {len(pr_lines)}')
        return 'auto-restarted'

    dm_larry_auto_restart_failed(unit, rc, stderr)
    log(f'{unit}: AUTO-RESTART FAILED rc={rc} stderr={stderr[:200]!r}', 'WARN')
    return 'auto-restart-failed'


# -------------------- shared-lib watchlist (V4) --------------------

def check_shared_lib_watchlist(
    state: dict, now: Optional[float] = None,
) -> dict[str, int]:
    """Scan the effective watchlist (hardcoded SHARED_LIB_WATCHLIST unioned
    with the committed restart manifest). For each (lib, dependent-unit) pair,
    if the lib's mtime is newer than the dependent unit's ActiveEnterTimestamp
    by more than RACE_AVOIDANCE_SEC, route the unit through the same
    auto-restart machinery as the direct-script path.

    Reuses the existing per-unit primitives (`in_restart_cooldown`,
    `auto_restart_unit`, `mark_restarted`, `dm_larry_auto_restarted`,
    `dm_larry_auto_restart_failed`). Outcome counts share names with
    `check_unit` so the main-loop summary log stays interpretable when
    both paths fire on the same tick.

    Returns a counts dict; the caller folds it into the main loop's
    aggregate.

    Important: when a dependent unit is restarted via this path, the
    cooldown clock is shared with the direct-script path
    (`mark_restarted`). The next tick's `check_unit` for that unit will
    therefore observe `in_restart_cooldown=True` and not double-restart.
    """
    counts: dict[str, int] = {}
    for lib_path, dependent_units in build_effective_watchlist().items():
        if not lib_path.is_file():
            # Defensive: a watchlisted file may not exist in a sparse test
            # fixture or during a reorganization. Skip silently — the
            # runbook is the spec; missing-on-disk is operator action.
            continue
        try:
            lib_mtime = lib_path.stat().st_mtime
        except OSError as e:
            log(f'watchlist: stat({lib_path}) failed: {e}', 'WARN')
            continue
        for unit in sorted(dependent_units):
            outcome = _check_watchlist_pair(
                lib_path, lib_mtime, unit, state, now=now,
            )
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _check_watchlist_pair(
    lib_path: Path, lib_mtime: float, unit: str,
    state: dict, now: Optional[float] = None,
) -> str:
    """Evaluate one (shared-lib, dependent-unit) pair. Outcomes mirror
    `check_unit`'s vocabulary so the main-loop summary stays uniform:
    'auto-restarted' / 'auto-restart-failed' / 'restart-cooldown' /
    'race-window' / 'fresh' / 'unparseable'.
    """
    raw_ts = systemctl_show(unit, 'ActiveEnterTimestamp')
    service_start = parse_systemd_timestamp(raw_ts) if raw_ts is not None else None
    if service_start is None:
        # Either the unit isn't installed on this host or systemd hasn't
        # finished bringing it up. Either way: nothing to compare against.
        return 'unparseable'

    if not is_stale(service_start, lib_mtime):
        if lib_mtime > service_start:
            return 'race-window'
        return 'fresh'

    if in_restart_cooldown(state, unit, now=now):
        last_restart_ts = state['services'].get(unit, {}).get('last_restart_ts')
        # Same fresh-deploy override as check_unit(), keyed on the shared lib's
        # mtime: a shared-lib deploy landing after the last restart is a new
        # staleness event → re-restart instead of silently suppressing. A unit
        # that can't pick up the lib had it on disk before the restart
        # (lib_mtime <= last_restart_ts) → fresh_deploy False → suppress, so no
        # restart loop. Shared last_restart_ts state, same grace window.
        fresh_deploy = (
            isinstance(last_restart_ts, (int, float))
            and lib_mtime > last_restart_ts + FRESH_DEPLOY_GRACE_SEC
        )
        if not fresh_deploy:
            log(
                f'watchlist: {unit} stale vs {lib_path.name} but in 30-min '
                f'restart cooldown — suppressing duplicate restart',
                'INFO',
            )
            return 'restart-cooldown'
        log(
            f'watchlist: FRESH_DEPLOY_AFTER_RESTART {unit} — shared lib '
            f'{lib_path.name} mtime is {(lib_mtime - last_restart_ts):.0f}s '
            f'newer than last_restart_ts ({last_restart_ts}); re-restarting '
            f'despite the restart cooldown',
            'INFO',
        )

    rc, stderr = auto_restart_unit(unit)
    mark_restarted(state, unit, now=now)
    save_state(state)

    if rc == 0:
        pre_restart_iso = datetime.fromtimestamp(
            service_start, tz=timezone.utc,
        ).isoformat()
        pr_lines = infer_recent_prs(lib_path, pre_restart_iso)
        # Resolve the unit's OWN entrypoint so the alert can distinguish the
        # changed shared library (lib_path) from the service's own script.
        # Falls back to the unit name when FragmentPath/ExecStart can't be
        # parsed — the alert still names the changed library unambiguously.
        fragment_path = systemctl_show(unit, 'FragmentPath')
        entrypoint = (
            parse_script_path_from_service_file(fragment_path)
            if fragment_path else None
        ) or Path(unit)
        dm_larry_auto_restarted(
            unit, lib_path, service_start, lib_mtime, pr_lines,
            changed_lib_entrypoint=entrypoint,
        )
        log(
            f'watchlist: AUTO-RESTARTED {unit} (shared lib '
            f'{lib_path.name} newer than active-since by '
            f'{(lib_mtime - service_start) / 60:.1f}min); PRs since: '
            f'{len(pr_lines)}'
        )
        return 'auto-restarted'

    dm_larry_auto_restart_failed(unit, rc, stderr)
    log(
        f'watchlist: AUTO-RESTART FAILED unit={unit} '
        f'lib={lib_path.name} rc={rc} stderr={stderr[:200]!r}',
        'WARN',
    )
    return 'auto-restart-failed'


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
    overrides = load_service_overrides()
    counts = {
        'auto-restarted': 0, 'auto-restart-failed': 0,
        'restart-cooldown': 0, 'still-stale-after-restart': 0,
        'race-window': 0, 'fresh': 0, 'unparseable': 0,
        'strategy-never-skip': 0, 'strategy-dm-only': 0,
    }
    for unit in units:
        outcome = check_unit(unit, state, overrides=overrides)
        counts[outcome] = counts.get(outcome, 0) + 1

    # V4: shared-lib watchlist scan after the direct-script pass. The
    # cooldown clock is shared via `state`, so a direct-script restart on
    # tick N suppresses a watchlist restart on the same tick for the same
    # unit (and vice versa).
    watchlist_counts = check_shared_lib_watchlist(state, now=None)
    for k, v in watchlist_counts.items():
        counts[k] = counts.get(k, 0) + v

    log('tick: ' + ' '.join(f'{k}={v}' for k, v in counts.items() if v))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
