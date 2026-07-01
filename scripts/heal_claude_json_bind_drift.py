#!/usr/bin/env python3
"""heal_claude_json_bind_drift.py — detect + auto-repair a dangled
``/home/larry/.claude.json`` bind-mount in long-running claude-invoking units.

THE FAILURE MODE (recurred 2026-06-26 on ourliberty-beacon-bot)
--------------------------------------------------------------
The claude-invoking sandboxed units run with ``ProtectHome=read-only`` and
carve ``/home/larry/.claude.json`` back to read-write via ``ReadWritePaths=``
(added in PR #470). ``ReadWritePaths=`` on a *file* is implemented as a
per-FILE bind-mount: it pins the file's INODE into the unit's mount namespace.

``claude`` itself writes ``~/.claude.json`` IN PLACE (open + truncate; same
inode) — so claude never breaks its own mount. But when something OUTSIDE the
namespace (an interactive ``claude`` session on the droplet, a self-update, or
claude's own occasional repair path — all run on the host where ``/home`` is
writable) ATOMICALLY REPLACES the file (write-temp + rename, new inode), the
running services' bind-mounts still point at the OLD inode. Path resolution to
``/home/larry/.claude.json`` inside those namespaces then falls through to the
read-only ``/home`` mount → the next open-for-write raises
``EROFS: read-only file system`` → ``claude`` exits 1 → the telegram bridges
relay a bare ``[claude exit 1]`` and look offline. The ONLY fix is to restart
the affected unit, which rebuilds its namespace and re-binds the CURRENT inode.

Beacon started 03:19Z, the file was replaced later, EROFS began ~13:26Z, and it
stayed broken until a manual ``sudo systemctl restart`` ~hours later. This
healer automates that manual fix: it detects the dangled mount early and
restarts only the affected units. See memory note ``claude-json-erofs-readwrite``
and ``runbooks/claude-json-bind-drift.md``.

WHY A HEALER (not a unit-file change)
-------------------------------------
A *file* carve-out — whether ``ReadWritePaths=`` or ``BindPaths=`` — always
pins an inode and can therefore dangle on atomic-replace; you cannot make a
file bind-mount replacement-proof from the unit file alone. Relocating the file
into the already-safe ``.claude/`` *directory* carve-out would require
``CLAUDE_CONFIG_DIR`` to move ``.claude.json`` too, which is
undocumented/version-dependent (a fragile bet on a specific claude build). So
the robust, version-independent fix is to respond to the SYMPTOM (the mount is
no longer writable in the namespace) regardless of which process or claude
version replaced the file. This healer COVERS every current and future
claude-invoking persistent unit by discovering them dynamically from systemd,
so the ``.claude.json`` carve-out on the units stays exactly as #470 left it.

WHAT THIS DOES
--------------
Each tick, for every ``ourliberty-*.service`` that is (a) a persistent unit
(``Type`` in simple/notify/exec/idle), (b) currently ``active`` with a
``MainPID``, and (c) carves ``/home/larry/.claude.json`` in its effective
``ReadWritePaths``:
  1. Enter its mount namespace (``sudo -n nsenter -m -t <MainPID>``) and probe
     whether ``/home/larry/.claude.json`` can be opened ``O_RDWR``. A
     read-only mount fails the open with ``EROFS`` (errno 30) even for root, so
     this is the ground-truth signal — exactly the ``test -w`` probe used to
     diagnose the incident, but enforced at open() so a root probe can't
     falsely pass.
  2. ``EROFS`` → the mount has dangled → ``sudo -n systemctl restart --no-block
     <unit>``, settle, confirm via ``is-active`` AND re-probe the NEW MainPID's
     namespace to verify the file is writable again, then DM the outcome.

WHAT THIS DOES NOT DO
---------------------
  - Touch ONESHOT (timer-driven) healer units: each tick spawns a fresh
    namespace that binds the current inode, so they cannot carry a stale mount.
  - Restart a unit on a non-EROFS probe error (sudo revoked, nsenter missing,
    file absent) — those are logged/escalated, never used to hammer a unit.
  - Retry within the per-unit restart cooldown — one restart attempt per unit
    per cooldown window; a still-dangled unit past cooldown escalates instead.

SAFE-BY-CONSTRUCTION
--------------------
  - The probe NEVER writes: it opens ``O_RDWR`` (no create, no truncate) and
    immediately closes — content and mtime are untouched.
  - ``sudo -n`` (non-interactive): a revoked sudoers contract fails fast and
    surfaces as a ``probe-error``/``repair-failed`` DM rather than wedging.
  - Kill-switch aware: ``~/agents/healers.disabled`` disables detection AND
    repair (``main()`` short-circuits before the per-unit loop).
  - Per-unit restart cooldown bounds blast radius to one restart per unit per
    window; the failure path is no-retry.
  - Idempotent: with every mount healthy the tick is a read-only no-op.
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-claude-json-bind-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-claude-json-bind-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-claude-json-bind-drift-cooldowns.json'

# The home-root config file whose per-FILE bind-mount dangles on atomic-replace.
# Env-overridable so tests can point the probe at a fixture path.
CLAUDE_JSON_PATH = os.environ.get(
    'OURLIBERTY_CLAUDE_JSON_PATH', '/home/larry/.claude.json',
)

# systemd Type values that denote a PERSISTENT (long-running) unit whose mount
# namespace survives across an external file replacement. oneshot units spawn a
# fresh namespace each activation and therefore cannot carry a stale mount.
PERSISTENT_TYPES = frozenset({'simple', 'notify', 'exec', 'idle', 'dbus'})

# Unit glob (NOT .timer — timers activate the underlying .service).
UNIT_GLOB = 'ourliberty-*.service'

# Subprocess timeouts. systemctl show is fast (<100ms); nsenter probe is a
# single open()/close() in a python subinterpreter; both capped so a hung dbus
# or stuck namespace can't wedge the healer tick.
SYSTEMCTL_TIMEOUT_S = 10
NSENTER_TIMEOUT_S = 15
RESTART_TIMEOUT_S = 30

# After a --no-block restart, wait this long before verifying is-active +
# re-probing the new MainPID. Units running claude loops take ~90s to drain on
# SIGTERM, but the NEW process (and its fresh, correctly-bound namespace) comes
# up promptly; 3s is enough to read a settled is-active and a new MainPID.
RESTART_SETTLE_S = 3

# Per-unit restart cooldown — at most one repair attempt per unit per window. A
# genuine dangle is fixed permanently by one restart (until the file is replaced
# again), so a recurrence inside this window means either a restart storm bug or
# a structural problem; either way we suppress and escalate rather than hammer.
RESTART_COOLDOWN_SEC = 15 * 60

# Per-subject DM cooldown for the escalation paths (still-dangled-after-restart,
# persistent probe-error). Larger than larry_alerts' built-in 60-min window so
# this is the binding constraint for a chronically broken unit/host.
ESCALATION_COOLDOWN_SEC = 6 * 60 * 60

# is-active states that mean the restart is succeeding or has succeeded.
_HEALTHY_ACTIVE_STATES = frozenset(
    {'active', 'activating', 'reloading', 'deactivating'}
)

# Probe exit-code contract (see _PROBE_SNIPPET below).
_PROBE_OK = 0       # opened O_RDWR cleanly → mount writable (healthy)
_PROBE_EROFS = 2    # OSError errno == EROFS → mount dangled (repair)
_PROBE_OTHER = 3    # any other OSError (e.g. ENOENT) → probe-error (no repair)
# Not an nsenter exit code — a caller-side classification of a non-OK/non-EROFS
# result where the target process has exited between MainPID enumeration and the
# probe (a benign TOCTOU race on short-lived Type=simple units). nsenter fails
# with ENOENT on /proc/<pid>/ns/mnt, indistinguishable at the exit-code level
# from a real probe-blind failure — process liveness is the discriminator.
_PROBE_GONE = 4     # target process exited mid-probe → benign skip (re-probe next tick)

# The in-namespace probe: open the file O_RDWR (write-intent, but NO create and
# NO truncate) and close it immediately. On a read-only mount the open(2) itself
# returns EROFS even for root, so this faithfully reports "can a process in this
# namespace write the file?" without ever modifying it. Exit code classifies the
# outcome so the caller never has to parse locale-dependent strerror text.
_PROBE_SNIPPET = (
    'import os,sys,errno\n'
    'try:\n'
    '    fd=os.open({path!r}, os.O_RDWR)\n'
    '    os.close(fd)\n'
    '    sys.exit(0)\n'
    'except OSError as e:\n'
    '    sys.stderr.write("{{}}:{{}}".format(e.errno, e.strerror))\n'
    '    sys.exit(2 if e.errno==errno.EROFS else 3)\n'
)


# -------------------- logging / heartbeat / kill-switch --------------------

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


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# -------------------- cooldown state --------------------

def load_state() -> dict:
    """Return {'services': {unit: {'last_restart_ts':…, 'last_alert_ts':…}}}."""
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


def in_restart_cooldown(state: dict, unit: str, now: Optional[float] = None) -> bool:
    entry = state['services'].get(unit)
    if not entry:
        return False
    last = entry.get('last_restart_ts')
    if not isinstance(last, (int, float)):
        return False
    now = now if now is not None else time.time()
    return (now - last) < RESTART_COOLDOWN_SEC


def mark_restarted(state: dict, unit: str, now: Optional[float] = None) -> None:
    entry = state['services'].setdefault(unit, {})
    entry['last_restart_ts'] = now if now is not None else time.time()


def in_alert_cooldown(state: dict, unit: str, now: Optional[float] = None) -> bool:
    entry = state['services'].get(unit)
    if not entry:
        return False
    last = entry.get('last_alert_ts')
    if not isinstance(last, (int, float)):
        return False
    now = now if now is not None else time.time()
    return (now - last) < ESCALATION_COOLDOWN_SEC


def mark_alerted(state: dict, unit: str, now: Optional[float] = None) -> None:
    entry = state['services'].setdefault(unit, {})
    entry['last_alert_ts'] = now if now is not None else time.time()


# -------------------- systemctl shellouts --------------------

def list_ourliberty_services() -> list[str]:
    """Return unit names matching UNIT_GLOB (active or not). Mirrors
    heal_stale_daemon_code's discovery so the source of truth is systemd, not a
    hand-maintained list — new claude-invoking units are covered automatically.
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
        parts = line.strip().split()
        if not parts:
            continue
        name = parts[0]
        if name.endswith('.service') and name.startswith('ourliberty-'):
            units.append(name)
    return units


def systemctl_show(unit: str, props: list[str]) -> dict[str, str]:
    """Return {prop: value} from `systemctl show <unit> -p P1 -p P2 …`.

    Missing properties simply don't appear in the dict. Returns {} on error.
    """
    args = ['systemctl', 'show', unit]
    for p in props:
        args.append(f'--property={p}')
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
        if result.returncode != 0:
            return {}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k] = v
    return out


def unit_active_state(unit: str) -> str:
    """`systemctl is-active <unit>`'s state string, or 'unknown' on error."""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
        return (result.stdout or '').strip() or 'unknown'
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 'unknown'


def carveout_present(read_write_paths: str) -> bool:
    """True iff CLAUDE_JSON_PATH appears as a whitespace-separated token in the
    unit's effective ReadWritePaths (so a substring of a longer path can't
    false-match)."""
    return CLAUDE_JSON_PATH in (read_write_paths or '').split()


def target_main_pid(unit: str) -> Optional[int]:
    """Return the MainPID of `unit` iff it is a PERSISTENT, active claude-
    carve-out unit we must monitor; otherwise None (skip).

    Skips: oneshot units (fresh namespace each run), inactive units (nothing to
    probe), and units that don't carve /home/larry/.claude.json (a replacement
    can't dangle a mount they never had).
    """
    props = systemctl_show(
        unit, ['Type', 'ActiveState', 'MainPID', 'ReadWritePaths'],
    )
    if not props:
        return None
    if props.get('Type', '') not in PERSISTENT_TYPES:
        return None
    if props.get('ActiveState', '') != 'active':
        return None
    if not carveout_present(props.get('ReadWritePaths', '')):
        return None
    raw_pid = props.get('MainPID', '0')
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    return pid if pid > 0 else None


# -------------------- in-namespace write probe --------------------

def _process_alive(pid: int) -> bool:
    """True iff /proc/<pid> still exists — the target has not exited.

    Used to discriminate a benign mid-probe exit (short-lived Type=simple unit
    whose process died between MainPID enumeration and the nsenter probe) from a
    genuine probe-blind failure (process alive, but sudo/nsenter rejected).
    """
    return os.path.exists(f'/proc/{pid}')


def probe_namespace_writable(pid: int) -> int:
    """Probe whether CLAUDE_JSON_PATH is writable inside pid's mount namespace.

    Returns one of _PROBE_OK / _PROBE_EROFS / _PROBE_OTHER / _PROBE_GONE. nsenter
    into another process's mount namespace needs CAP_SYS_ADMIN, hence `sudo -n`.
    A non-OK/non-EROFS result is classified by target liveness: if the process
    has exited (a benign TOCTOU race), _PROBE_GONE; otherwise _PROBE_OTHER — never
    a dangle, so a blind healer cannot trigger a restart.
    """
    snippet = _PROBE_SNIPPET.format(path=CLAUDE_JSON_PATH)
    try:
        result = subprocess.run(
            ['sudo', '-n', 'nsenter', '-m', '-t', str(pid), '--',
             '/usr/bin/python3', '-c', snippet],
            capture_output=True, text=True, timeout=NSENTER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        log(f'probe pid={pid}: nsenter timed out after {NSENTER_TIMEOUT_S}s', 'WARN')
        return _PROBE_OTHER
    except FileNotFoundError:
        log('probe: sudo or nsenter not found in PATH', 'WARN')
        return _PROBE_OTHER
    if result.returncode == _PROBE_OK:
        return _PROBE_OK
    if result.returncode == _PROBE_EROFS:
        log(f'probe pid={pid}: EROFS — {CLAUDE_JSON_PATH} mount dangled '
            f'({result.stderr.strip()[:120]})', 'WARN')
        return _PROBE_EROFS
    # Any other exit code (3 = non-EROFS OSError; 1 = sudo/nsenter rejection;
    # 126/127 = exec failure). Before treating this as probe-blind, re-check
    # target liveness: a short-lived Type=simple unit (e.g. ourliberty-cycle)
    # can exit between MainPID enumeration and the probe, and nsenter then fails
    # with ENOENT on /proc/<pid>/ns/mnt — a benign race, NOT a broken sudo/nsenter
    # contract. Process gone → benign skip (re-probe next tick, INFO not WARN);
    # process alive → genuine probe-blind (_PROBE_OTHER, WARN). Liveness is the
    # robust primary signal; the ns/mnt-ENOENT stderr is only corroborating
    # (locale/format-fragile), so we don't gate on it.
    stderr = result.stderr.strip()
    if not _process_alive(pid):
        log(f'probe pid={pid}: process exited mid-probe '
            f'(rc={result.returncode} stderr={stderr[:160]!r}); skipping, '
            f'will re-probe next tick', 'INFO')
        return _PROBE_GONE
    log(f'probe pid={pid}: non-EROFS rc={result.returncode} '
        f'stderr={stderr[:160]!r}', 'WARN')
    return _PROBE_OTHER


# -------------------- restart --------------------

def restart_unit(unit: str) -> tuple[int, str]:
    """`sudo -n systemctl restart --no-block <unit>`, settle, verify is-active.

    --no-block returns as soon as the job is enqueued (claude units take ~90s to
    drain on SIGTERM); the is-active verify after the settle is the source of
    truth for "did the new process come up". Contract:
      - (0, '')          restart enqueued AND unit active/activating after settle
      - (rc, stderr)     restart job rejected up front (rc != 0)
      - (-1, descriptive) enqueued but not active/activating after settle
    """
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', '--no-block', unit],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        result = None  # systemd unresponsive; fall through to verify
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'

    if result is not None and result.returncode != 0:
        return result.returncode, (result.stderr or '').strip()

    time.sleep(RESTART_SETTLE_S)
    state = unit_active_state(unit)
    if state in _HEALTHY_ACTIVE_STATES:
        return 0, ''
    return -1, (
        f'restart issued but unit is {state!r} after {RESTART_SETTLE_S}s '
        f'(expected active/activating)'
    )


# -------------------- DM to Larry --------------------

def _import_larry_alerts():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import larry_alerts as la  # noqa: E402
    return la


def dm_repaired(unit: str, reverified: bool) -> bool:
    """Outcome-routed DM after a successful repair (closure if significant,
    else digest — a routine self-heal Larry needn't act on)."""
    try:
        la = _import_larry_alerts()
        verify_line = (
            'Re-probe of the new namespace confirms the file is writable again.'
            if reverified else
            'Unit is active again (new-namespace re-probe was inconclusive).'
        )
        body = (
            f'Auto-repaired {unit}: its {CLAUDE_JSON_PATH} bind-mount had '
            f'dangled (EROFS — the file was atomically replaced on the host '
            f'after the unit started), so claude was failing with '
            f'`[claude exit 1]`. Restarted the unit to re-bind the current '
            f'inode. {verify_line}'
        )
        subject = f'rebound:{unit}'
        route = la.classify_route('heal-claude-json-bind-drift', subject, healed=True)
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=subject,
            message=body,
            route=route,
        )
    except Exception as e:
        log(f'dm_repaired failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_repair_failed(unit: str, rc: int, detail: str) -> bool:
    """Escalation DM when the restart itself failed."""
    try:
        la = _import_larry_alerts()
        body = (
            f'Auto-repair of {unit} FAILED (rc={rc}): {detail or "(no detail)"}\n\n'
            f'Its {CLAUDE_JSON_PATH} bind-mount dangled (EROFS) but the restart '
            f'did not bring the unit back. No automatic retry.\n\n'
            f'Manual recovery:\n  sudo systemctl restart {unit}'
        )
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=f'repair-failed:{unit}',
            message=body,
            suggested_action=f'sudo systemctl restart {unit}',
        )
    except Exception as e:
        log(f'dm_repair_failed failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_still_dangled(unit: str) -> bool:
    """Escalation DM when a unit is still dangled inside the restart cooldown —
    a restart already fired recently yet the mount is read-only again. Points at
    a structural problem (carve-out removed, file churning every few minutes)."""
    try:
        la = _import_larry_alerts()
        body = (
            f'{unit} still shows a dangled {CLAUDE_JSON_PATH} mount (EROFS) '
            f'within {RESTART_COOLDOWN_SEC // 60} min of an auto-restart. '
            f'Suppressing further restarts to avoid a loop — manual '
            f'investigation needed (is the carve-out still in the unit file? '
            f'is something replacing {CLAUDE_JSON_PATH} every few minutes?).'
        )
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=f'still-dangled:{unit}',
            message=body,
            suggested_action=(
                f'systemctl cat {unit} | grep ReadWritePaths ; '
                f'sudo systemctl restart {unit}'
            ),
        )
    except Exception as e:
        log(f'dm_still_dangled failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_probe_blind(unit: str) -> bool:
    """Escalation DM when the healer cannot even probe a unit (sudo/nsenter
    failure). A blind healer is itself an incident — surface it (6h-gated)."""
    try:
        la = _import_larry_alerts()
        body = (
            f'Cannot probe {unit}\'s mount namespace for a dangled '
            f'{CLAUDE_JSON_PATH} (sudo -n / nsenter failed). The bind-drift '
            f'healer is BLIND for this unit and cannot auto-repair an EROFS '
            f'dangle until this is fixed.'
        )
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=f'probe-blind:{unit}',
            message=body,
            suggested_action=(
                'Check the droplet sudoers NOPASSWD contract and that '
                'nsenter (util-linux) is installed.'
            ),
        )
    except Exception as e:
        log(f'dm_probe_blind failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- per-unit orchestration --------------------

def check_unit(unit: str, state: dict, now: Optional[float] = None) -> str:
    """Inspect one unit. Returns one of:
       'skip'            — not a persistent/active/carve-out unit, OR the target
                           process exited mid-probe (benign race, no action).
       'healthy'         — mount writable in namespace (no action).
       'probe-error'     — couldn't probe (sudo/nsenter); escalation (6h-gated).
       'rebound'         — dangle detected, restart succeeded + verified.
       'repair-failed'   — dangle detected, restart failed; escalation.
       'cooldown-dangled' — dangle but inside restart cooldown; escalation.
    """
    pid = target_main_pid(unit)
    if pid is None:
        return 'skip'

    outcome = probe_namespace_writable(pid)
    if outcome == _PROBE_OK:
        return 'healthy'
    if outcome == _PROBE_GONE:
        # Target exited between MainPID enumeration and the probe (benign TOCTOU
        # race on a short-lived unit). Treat exactly like skip: no probe-blind
        # DM, no escalation-cooldown state write — just re-probe next tick.
        return 'skip'
    if outcome == _PROBE_OTHER:
        # Blind on this unit. Escalate at most once per ESCALATION window so a
        # persistent sudoers/nsenter breakage surfaces without spamming.
        if not in_alert_cooldown(state, unit, now=now):
            dm_probe_blind(unit)
            mark_alerted(state, unit, now=now)
            save_state(state)
        return 'probe-error'

    # _PROBE_EROFS — the mount has dangled.
    if in_restart_cooldown(state, unit, now=now):
        # A restart fired recently yet it's dangled again → structural. Escalate
        # (6h-gated) instead of restarting in a loop.
        if not in_alert_cooldown(state, unit, now=now):
            dm_still_dangled(unit)
            mark_alerted(state, unit, now=now)
            save_state(state)
        log(f'{unit}: STILL DANGLED inside restart cooldown — suppressing '
            f'restart', 'WARN')
        return 'cooldown-dangled'

    # Attempt the repair.
    rc, detail = restart_unit(unit)
    mark_restarted(state, unit, now=now)
    save_state(state)

    if rc != 0:
        dm_repair_failed(unit, rc, detail)
        log(f'{unit}: REPAIR FAILED rc={rc} detail={detail[:160]!r}', 'WARN')
        return 'repair-failed'

    # Restart reported active. Re-probe the NEW MainPID's namespace to confirm
    # the file is actually writable again (the whole point of the repair).
    new_pid = target_main_pid(unit)
    reverified = False
    if new_pid is not None and new_pid != pid:
        reverified = probe_namespace_writable(new_pid) == _PROBE_OK
    dm_repaired(unit, reverified)
    log(f'{unit}: REBOUND — restarted (old pid {pid} → new pid {new_pid}); '
        f'reverified_writable={reverified}')
    return 'rebound'


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
        'skip': 0, 'healthy': 0, 'probe-error': 0,
        'rebound': 0, 'repair-failed': 0, 'cooldown-dangled': 0,
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
