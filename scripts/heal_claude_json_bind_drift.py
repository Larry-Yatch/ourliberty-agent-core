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

WHAT THIS DOES, EACH TICK
-------------------------
1. Classify every ``ourliberty-*.service`` (see CLASSIFICATION below) and probe
   the ones that carry the carve-out and are active with a MainPID: enter the
   mount namespace (``sudo -n nsenter -m -t <MainPID>``) and try to open
   ``/home/larry/.claude.json`` ``O_RDWR``. A read-only mount fails the open
   with ``EROFS`` (errno 30) even for root, so this is the ground-truth signal —
   exactly the ``test -w`` probe used to diagnose the incident, but enforced at
   open() so a root probe can't falsely pass.
2. Repair (restart) the MONITORed units that probed EROFS: ``sudo -n systemctl
   restart --no-block <unit>``, settle, confirm via ``is-active`` AND re-probe
   the NEW MainPID's namespace to verify the file is writable again, then DM the
   outcome.

CLASSIFICATION: ``Restart=``, NOT ``TriggeredBy=``
--------------------------------------------------
A unit is EPHEMERAL (a run-to-completion job whose next activation gets a fresh
namespace, so a dangle heals itself and a restart only SIGTERMs the live run)
or a MONITORed daemon (long-running; a dangle persists until we rebind it).

The first cut is ``Type`` (oneshot is never persistent). The second cut used to
be ``TriggeredBy=*.timer`` — and that FAILED OPEN: ``TriggeredBy`` is a reverse
dependency materialised only while the triggering ``.timer`` is LOADED in the
manager, so ``systemctl disable --now``/``mask`` on the timer silently returned
its service to the repair path (verified on the droplet: a disabled, unloaded
timer reports ``TriggeredBy=`` empty while a loaded one populates it). And it
failed CLOSED from the other side: attaching any companion ``.timer`` to a real
daemon would have removed it from coverage permanently.

We therefore ask the question systemd answers from the UNIT FILE, independent of
manager link state: is this unit SUPERVISED?

  ``Restart=`` in {always, on-failure, on-abnormal, on-success, on-watchdog,
  on-abort}  → MONITOR (a daemon systemd keeps alive)
  ``Restart=`` in {no, ''}                     → EPHEMERAL_JOB

Measured across ``systemd/`` at the time of writing: the bots are
``Restart=always``; inbox-watcher / outbox-notifier / spec-review-runner are
``Restart=on-failure``; ``ourliberty-cycle`` — the unit that was killed mid-run
and then false-paged on 2026-07-30 — is the sole ``Restart=no`` persistent
unit. ``TriggeredBy`` is still read and logged as CORROBORATION, never as a
verdict.

The residual risk of this swap is the mirror image: a genuinely persistent
daemon that simply omits ``Restart=`` would classify EPHEMERAL and leave
coverage silently. Two detectors exist for exactly that, and they are not
decoration:
  - runtime: the per-tick ``coverage=`` line, which names the units currently in
    coverage AND carries a standing ``departed=`` list for every unit that has
    left it and not come back, plus a WARN on the transition (see
    ``coverage_delta``);
  - build time: a repo lint in the tests requiring every persistent carve-out
    unit to declare ``Restart=`` or be listed in a KNOWN_EPHEMERAL allowlist.
    KNOWN PROSPECTIVE GAP: that lint parses the unit FILE, so it skips a unit
    file that omits ``Type=`` entirely (systemd defaults such a unit to
    ``Type=simple``, which the runtime classifier does see). No unit in
    ``systemd/`` omits ``Type=`` today; a future one that omitted BOTH ``Type=``
    and ``Restart=`` would classify EPHEMERAL at runtime with the lint green.
    The runtime ``departed=`` detector is what covers it in the meantime.

Ephemeral units are still PROBED (detection and repair are separate concerns) —
an in-flight ``/cycle`` whose mount dangled is recorded as ``ephemeral-dangled``
in the journal and the tick line. It is deliberately NOT alerted and NOT
restarted: a restart ends that run exactly as the EROFS does, the next fire
rebinds in a fresh namespace, so a DM would be pure toil. The probe-only path
is a SEPARATE FUNCTION with no route to ``guarded_restart`` — structural, not an
``if`` one careless edit away from re-enabling the 2026-07-30 behaviour.

WHAT THIS DOES NOT DO
---------------------
  - Touch ONESHOT (timer-driven) healer units: each tick spawns a fresh
    namespace that binds the current inode, so they cannot carry a stale mount.
  - Restart an EPHEMERAL unit (see CLASSIFICATION) — ever.
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
from typing import NamedTuple, Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import restart_guard  # noqa: E402  (shared cordon-and-drain, PR 3)
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

# `Restart=` values that mean "systemd supervises this as a daemon" → MONITOR.
SUPERVISED_RESTART_POLICIES = frozenset({
    'always', 'on-failure', 'on-abnormal', 'on-success', 'on-watchdog',
    'on-abort',
})
# `Restart=` values that mean "run to completion, then stay stopped" →
# EPHEMERAL_JOB. '' is included per the classification contract; a MISSING
# `Restart` property (systemctl could not be read) is SKIP_UNKNOWN instead, so
# an unreadable unit is never silently descoped OR silently restarted.
EPHEMERAL_RESTART_POLICIES = frozenset({'', 'no'})

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

# Cordon-and-drain ceiling for an agent-hosting unit (PR 3). Shorter than
# heal_stale_daemon_code's 3600s because this healer ticks every 2 minutes and a
# dangled mount is an ACTIVE outage — every session in that unit is failing with
# `[claude exit 1]` until the rebind. The cordon is what makes the drain
# terminate here: with the mount broken the watcher would otherwise keep
# dispatching fresh sessions that fail instantly, so in-flight would never reach
# zero on its own. Requires TimeoutStartSec >= this + restart overhead on
# ourliberty-heal-claude-json-bind-drift.service (raised 120 -> 1200 in this PR);
# undersizing it is the #1000 failure — SIGTERM mid-drain, restart silently
# skipped.
AGENT_DRAIN_CEILING_SEC = int(
    os.environ.get('OL_BIND_DRIFT_DRAIN_CEILING_SEC', '900')
)

# Per-subject DM cooldown for the escalation paths (still-dangled-after-restart,
# persistent probe-error). Larger than larry_alerts' built-in 60-min window so
# this is the binding constraint for a chronically broken unit/host.
ESCALATION_COOLDOWN_SEC = 6 * 60 * 60

# is-active states that mean the restart is succeeding or has succeeded.
_HEALTHY_ACTIVE_STATES = frozenset(
    {'active', 'activating', 'reloading', 'deactivating'}
)

# ---- unit classes ----
CLASS_MONITOR = 'monitor'
CLASS_EPHEMERAL = 'ephemeral-job'
CLASS_SKIP_ONESHOT = 'skip-oneshot'
CLASS_SKIP_UNKNOWN = 'skip-unknown'

# ---- per-unit outcomes (every one of these is a distinct tick-line bucket;
#      a unit leaving coverage must never be byte-identical to a unit that was
#      never in scope) ----
OUTCOME_SKIP_ONESHOT = 'skip-oneshot'
OUTCOME_SKIP_EPHEMERAL = 'skip-ephemeral'
OUTCOME_SKIP_INACTIVE = 'skip-inactive'
OUTCOME_SKIP_NOCARVE = 'skip-nocarve'
OUTCOME_SKIP_UNKNOWN = 'skip-unknown'
OUTCOME_SKIP_NSGONE = 'skip-nsgone'
OUTCOME_HEALTHY = 'healthy'
OUTCOME_EPHEMERAL_DANGLED = 'ephemeral-dangled'
OUTCOME_EPHEMERAL_PROBE_ERROR = 'ephemeral-probe-error'
OUTCOME_PROBE_ERROR = 'probe-error'
OUTCOME_REBOUND = 'rebound'
OUTCOME_REPAIR_FAILED = 'repair-failed'
OUTCOME_COOLDOWN_DANGLED = 'cooldown-dangled'
OUTCOME_REPAIR_SKIPPED_PEER = 'repair-skipped-peer-active'

OUTCOMES = (
    OUTCOME_SKIP_ONESHOT, OUTCOME_SKIP_EPHEMERAL, OUTCOME_SKIP_INACTIVE,
    OUTCOME_SKIP_NOCARVE, OUTCOME_SKIP_UNKNOWN, OUTCOME_SKIP_NSGONE,
    OUTCOME_HEALTHY, OUTCOME_EPHEMERAL_DANGLED, OUTCOME_EPHEMERAL_PROBE_ERROR,
    OUTCOME_PROBE_ERROR, OUTCOME_REBOUND, OUTCOME_REPAIR_FAILED,
    OUTCOME_COOLDOWN_DANGLED, OUTCOME_REPAIR_SKIPPED_PEER,
)

# Sentinel returned by inspect_unit when a MONITORed unit needs the repair pass.
# Never a tick-line bucket — the caller turns it into one.
REPAIR_REQUESTED = '__repair-requested__'

# Probe exit-code contract (see _PROBE_SNIPPET below).
_PROBE_OK = 0       # opened O_RDWR cleanly → mount writable (healthy)
_PROBE_EROFS = 2    # OSError errno == EROFS → mount dangled (repair)
_PROBE_OTHER = 3    # any other OSError (e.g. ENOENT) → probe-error (no repair)
# Not an nsenter exit code — a caller-side classification of a non-OK/non-EROFS
# result where the target's mount namespace is already gone (the process has
# exited / is mid-reap between MainPID enumeration and the probe — a benign
# TOCTOU race on short-lived Type=simple units). nsenter fails with ENOENT on
# /proc/<pid>/ns/mnt, indistinguishable at the exit-code level from a real
# probe-blind failure — /proc/<pid>/ns/mnt existence is the discriminator.
_PROBE_GONE = 4     # target namespace already gone mid-probe → benign skip (re-probe next tick)

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


class UnitFacts(NamedTuple):
    """One direct read of a unit's systemd facts.

    ``present`` False means `systemctl show` could not be read at all — the
    caller must treat that as UNKNOWN, never as 'not in scope'. There is
    deliberately no ActiveState filter here: the old ``target_main_pid``
    accessor collapsed 'oneshot', 'inactive', 'no carve-out' and 'unreadable'
    into one ``None``, so a unit that left coverage was byte-identical to a unit
    that was never in scope. Every caller now sees the reason."""
    unit: str
    present: bool
    type_: str
    active_state: str
    main_pid: Optional[int]
    read_write_paths: str
    restart_policy: Optional[str]
    triggered_by: str


_FACT_PROPS = ['Type', 'ActiveState', 'MainPID', 'ReadWritePaths',
               'TriggeredBy', 'Restart']


def unit_facts(unit: str) -> UnitFacts:
    props = systemctl_show(unit, _FACT_PROPS)
    if not props:
        return UnitFacts(unit, False, '', 'unknown', None, '', None, '')
    try:
        pid: Optional[int] = int(props.get('MainPID', '0'))
    except ValueError:
        pid = None
    if pid is not None and pid <= 0:
        pid = None
    return UnitFacts(
        unit=unit,
        present=True,
        type_=props.get('Type', ''),
        active_state=props.get('ActiveState', '') or 'unknown',
        main_pid=pid,
        read_write_paths=props.get('ReadWritePaths', ''),
        restart_policy=props.get('Restart'),
        triggered_by=props.get('TriggeredBy', ''),
    )


def classify_unit(facts: UnitFacts) -> tuple[str, str]:
    """Return (class, human reason). See CLASSIFICATION in the module docstring.

    Reads `Restart=` — a property of the UNIT FILE — rather than `TriggeredBy`,
    a reverse dependency that only exists while the triggering .timer is loaded
    in the manager. `TriggeredBy` is carried in the reason as corroboration."""
    corroborate = f'; TriggeredBy={facts.triggered_by or "(none)"}'
    if not facts.present:
        return CLASS_SKIP_UNKNOWN, 'systemctl show unreadable (timeout/missing)'
    if facts.type_ not in PERSISTENT_TYPES:
        return CLASS_SKIP_ONESHOT, f'Type={facts.type_ or "(unset)"} is not persistent'
    policy = facts.restart_policy
    if policy is None:
        return CLASS_SKIP_UNKNOWN, 'Restart= property unreadable'
    policy = policy.strip()
    if policy in SUPERVISED_RESTART_POLICIES:
        return CLASS_MONITOR, f'Restart={policy}{corroborate}'
    if policy in EPHEMERAL_RESTART_POLICIES:
        return CLASS_EPHEMERAL, f'EPHEMERAL_JOB (Restart={policy or "(empty)"}){corroborate}'
    return CLASS_SKIP_UNKNOWN, f'unrecognised Restart={policy!r}'


# -------------------- in-namespace write probe --------------------

def _namespace_probeable(pid: int) -> bool:
    """True iff /proc/<pid>/ns/mnt still exists — the mount-namespace handle
    nsenter needs is still present, so a non-OK/non-EROFS probe result reflects a
    genuine failure to enter/probe rather than a vanished target.

    This is the correct discriminator for the non-EROFS fallback: /proc/<pid>
    existence is too coarse because a just-exited / zombie process retains its
    /proc/<pid> dir during the kernel reaping window while its /proc/<pid>/ns/mnt
    has ALREADY been released. Such a process satisfies os.path.exists('/proc/N')
    yet has no namespace to enter — nsenter fails ENOENT on ns/mnt — which is a
    benign mid-probe exit race, NOT a broken sudo/nsenter contract. Keying on
    ns/mnt matches that failing signature directly and is locale-independent.
    """
    return os.path.exists(f'/proc/{pid}/ns/mnt')


def probe_namespace_writable(pid: int) -> int:
    """Probe whether CLAUDE_JSON_PATH is writable inside pid's mount namespace.

    Returns one of _PROBE_OK / _PROBE_EROFS / _PROBE_OTHER / _PROBE_GONE. nsenter
    into another process's mount namespace needs CAP_SYS_ADMIN, hence `sudo -n`.
    A non-OK/non-EROFS result is classified by namespace probeability: if the
    target's /proc/<pid>/ns/mnt is already gone (a benign mid-probe exit/reap
    race), _PROBE_GONE; otherwise _PROBE_OTHER — never a dangle, so a blind
    healer cannot trigger a restart.
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
    # 126/127 = exec failure). Before treating this as probe-blind, check whether
    # the mount namespace nsenter needs is even still there: a short-lived
    # Type=simple unit (e.g. ourliberty-cycle) can exit between MainPID
    # enumeration and the probe, and nsenter then fails with ENOENT on
    # /proc/<pid>/ns/mnt — a benign race, NOT a broken sudo/nsenter contract. A
    # just-exited / zombie process retains /proc/<pid> during the reaping window
    # while its /proc/<pid>/ns/mnt is already released, so we key on the ns/mnt
    # handle (which is exactly what nsenter opens) rather than the coarser
    # /proc/<pid> dir. ns/mnt gone → benign skip (re-probe next tick, INFO not
    # WARN); ns/mnt present → genuine probe-blind (_PROBE_OTHER, WARN).
    stderr = result.stderr.strip()
    if not _namespace_probeable(pid):
        log(f'probe pid={pid}: mount namespace gone mid-probe '
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


# -------------------- detection --------------------

def probe_ephemeral(unit: str, pid: int) -> str:
    """Detection-only path for a run-to-completion unit.

    Separate function BY CONSTRUCTION: it has no reference to restart_guard or
    restart_unit, so the ephemeral class cannot reach the repair path even by a
    careless edit. That matters — an `if` guard is one line away from
    re-enabling the 2026-07-30 behaviour (SIGTERM a live /cycle mid-run, then
    page because `inactive` is its correct resting state).

    A dangle here is recorded, never alerted: the run is already failing, a
    restart would end it exactly as the EROFS does, and the next fire rebinds in
    a fresh namespace."""
    probe = probe_namespace_writable(pid)
    if probe == _PROBE_OK:
        return OUTCOME_HEALTHY
    if probe == _PROBE_EROFS:
        log(f'{unit}: EPHEMERAL DANGLE — {CLAUDE_JSON_PATH} is read-only inside '
            f'the in-flight run (pid {pid}). NOT restarting: a restart would end '
            f'this run exactly as the EROFS does, and the next activation gets a '
            f'fresh namespace bound to the current inode. Recorded, not alerted.',
            'WARN')
        return OUTCOME_EPHEMERAL_DANGLED
    if probe == _PROBE_GONE:
        return OUTCOME_SKIP_NSGONE
    return OUTCOME_EPHEMERAL_PROBE_ERROR


def inspect_unit(unit: str, facts: UnitFacts, klass: str, state: dict,
                 now: Optional[float] = None) -> str:
    """Classify + probe one unit. Returns a tick-line outcome, or
    REPAIR_REQUESTED when a MONITORed unit needs the repair pass. Never restarts
    anything itself."""
    if klass == CLASS_SKIP_UNKNOWN:
        return OUTCOME_SKIP_UNKNOWN
    if klass == CLASS_SKIP_ONESHOT:
        return OUTCOME_SKIP_ONESHOT
    if not carveout_present(facts.read_write_paths):
        return OUTCOME_SKIP_NOCARVE
    if facts.active_state != 'active' or facts.main_pid is None:
        # Nothing to probe. For an ephemeral job this is its resting state.
        return (OUTCOME_SKIP_EPHEMERAL if klass == CLASS_EPHEMERAL
                else OUTCOME_SKIP_INACTIVE)
    if klass == CLASS_EPHEMERAL:
        return probe_ephemeral(unit, facts.main_pid)

    probe = probe_namespace_writable(facts.main_pid)
    if probe == _PROBE_OK:
        return OUTCOME_HEALTHY
    if probe == _PROBE_GONE:
        # Target's mount namespace vanished between MainPID enumeration and the
        # probe (benign exit/reap race). No probe-blind DM, no escalation-
        # cooldown state write — just re-probe next tick.
        return OUTCOME_SKIP_NSGONE
    if probe == _PROBE_OTHER:
        # Blind on this unit. Escalate at most once per ESCALATION window so a
        # persistent sudoers/nsenter breakage surfaces without spamming.
        if not in_alert_cooldown(state, unit, now=now):
            dm_probe_blind(unit)
            mark_alerted(state, unit, now=now)
            save_state(state)
        return OUTCOME_PROBE_ERROR

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
        return OUTCOME_COOLDOWN_DANGLED
    return REPAIR_REQUESTED


# -------------------- repair --------------------

def repair_unit(unit: str, facts: UnitFacts, state: dict,
                now: Optional[float] = None) -> str:
    """Restart a dangled MONITORed unit and verify the rebind.

    Reached only via REPAIR_REQUESTED, which ``inspect_unit`` returns only for
    CLASS_MONITOR. The ephemeral class has no path to this function at all."""
    prior_pid = facts.main_pid
    # Cordoned, so rebinding a unit that hosts Claude sessions doesn't SIGTERM
    # one mid-review (PR 3). Pass-through for any unit that hosts no sessions,
    # which is most of what UNIT_GLOB matches.
    guarded = restart_guard.guarded_restart(
        unit, 'claude-json-bind-drift', lambda: restart_unit(unit),
        ceiling_sec=AGENT_DRAIN_CEILING_SEC,
        on_ceiling='force',  # a dangled mount is an outage; it must be repaired
        log=log,
    )
    if not guarded.performed:
        # Peer-active skip only (on_ceiling='force' always restarts otherwise).
        # No cooldown is marked: we did not act, so the next 2-minute tick must
        # be free to repair this unit if the peer's restart didn't land.
        log(f'{unit}: repair skipped — a peer restarter holds the cordon lock; '
            f'its restart rebinds the mount too. Re-checking next tick.')
        return OUTCOME_REPAIR_SKIPPED_PEER

    rc, detail = guarded.result
    mark_restarted(state, unit, now=now)
    save_state(state)

    if rc != 0:
        dm_repair_failed(unit, rc, detail)
        log(f'{unit}: REPAIR FAILED rc={rc} detail={detail[:160]!r}', 'WARN')
        return OUTCOME_REPAIR_FAILED

    # Restart reported active. Re-probe the NEW MainPID's namespace to confirm
    # the file is actually writable again (the whole point of the repair).
    fresh = unit_facts(unit)
    new_pid = (fresh.main_pid
               if fresh.present and fresh.active_state == 'active' else None)
    reverified = False
    if new_pid is not None and new_pid != prior_pid:
        reverified = probe_namespace_writable(new_pid) == _PROBE_OK
    dm_repaired(unit, reverified)
    log(f'{unit}: REBOUND — restarted (old pid {prior_pid} → new pid {new_pid}); '
        f'reverified_writable={reverified}')
    return OUTCOME_REBOUND


# -------------------- single-unit convenience (tests / manual runs) --------------------

def check_unit(unit: str, state: dict, now: Optional[float] = None) -> str:
    """Inspect (and if needed repair) ONE unit. main() does not use this — it
    runs detection for the whole fleet first and then repairs — but the per-unit
    contract is identical.

    Outcomes are the OUTCOMES tuple; every one of them is documented in
    runbooks/claude-json-bind-drift.md (pinned by a test)."""
    facts = unit_facts(unit)
    klass, _reason = classify_unit(facts)
    outcome = inspect_unit(unit, facts, klass, state, now=now)
    if outcome == REPAIR_REQUESTED:
        return repair_unit(unit, facts, state, now=now)
    return outcome


# -------------------- coverage observability --------------------

def coverage_delta(state: dict, monitored: set[str],
                   reasons: dict[str, str]) -> None:
    """Journal (never DM) which units are in coverage and what changed.

    Without this, a daemon LEAVING coverage is byte-identical to a healer that
    was never in scope. Both directions are logged: an asymmetric detector would
    make a newly-covered unit look like it had always been covered, which is how
    a coverage gap gets blamed on the wrong deploy.

    TWO THINGS IT DELIBERATELY DOES NOT DO, because a coverage detector that
    does either of them is a blind spot wearing a detector's clothes:

    1. It does not announce a departure ONCE and then read normal forever. A
       transition WARN is an EVENT; a unit sitting OUTSIDE coverage is a STATE,
       and the state is what matters to whoever reads a tick line three weeks
       later. Departures are carried in ``state['coverage_departed']`` and
       reprinted on the ``coverage=`` line of EVERY later tick until the unit
       comes back or leaves systemd altogether.
    2. It does not conflate "no baseline recorded yet" with "the baseline is
       empty". Missing ``state['coverage']`` means the former, ``[]`` the
       latter. Reading both as falsey is what made a TOTAL collapse to zero
       monitored units — every unit unreadable at once, the loudest signal this
       healer can produce — log the reassuring "coverage baseline recorded (0
       monitored)" line instead, on every tick, forever."""
    prior_raw = state.get('coverage')
    departed: dict[str, str] = dict(state.get('coverage_departed') or {})
    carried = set(departed)      # departures from EARLIER ticks
    seen = set(reasons)          # every unit systemd listed this tick

    if prior_raw is None:
        log(f'coverage baseline recorded ({len(monitored)} monitored): '
            f'{", ".join(sorted(monitored)) or "(none)"}')
    else:
        prior = set(prior_raw)
        for unit in sorted(prior - monitored):
            reason = reasons.get(unit, 'unit no longer present in systemd')
            log(f'{unit} LEFT coverage: {reason}', 'WARN')
            departed[unit] = reason
        for unit in sorted(monitored - prior):
            log(f'{unit} ENTERED coverage: {reasons.get(unit, "newly monitored")}')

    # Came back — the ENTERED line above already said so, so drop it silently.
    for unit in carried & monitored:
        departed.pop(unit, None)
    # Gone from systemd entirely (decommissioned/renamed). Retiring it from the
    # standing list is a different event from it dropping out of coverage, so it
    # is still said out loud once rather than just disappearing.
    for unit in sorted(carried - seen):
        log(f'{unit} left coverage earlier and is no longer present in systemd '
            f'at all; retiring it from the departed list')
        departed.pop(unit, None)

    state['coverage'] = sorted(monitored)
    state['coverage_departed'] = {u: departed[u] for u in sorted(departed)}
    line = 'coverage=' + (','.join(sorted(monitored)) or '(none)')
    if departed:
        line += ' departed=' + ','.join(sorted(departed))
    log(line)


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
    counts: dict[str, int] = {k: 0 for k in OUTCOMES}

    def bump(outcome: str) -> None:
        counts[outcome] = counts.get(outcome, 0) + 1

    # 1. Classify + detect across the whole fleet (cheap and bounded). Detection
    #    is separated from repair so the coverage picture is taken BEFORE any
    #    restart perturbs it — a unit cannot appear to leave coverage because we
    #    happened to be restarting it while we counted.
    repairs: list[tuple[str, UnitFacts]] = []
    monitored: set[str] = set()
    reasons: dict[str, str] = {}
    for unit in units:
        facts = unit_facts(unit)
        klass, reason = classify_unit(facts)
        reasons[unit] = reason
        if klass == CLASS_MONITOR and carveout_present(facts.read_write_paths):
            monitored.add(unit)
        outcome = inspect_unit(unit, facts, klass, state)
        if outcome == REPAIR_REQUESTED:
            repairs.append((unit, facts))
            continue
        bump(outcome)

    coverage_delta(state, monitored, reasons)

    # 2. Repair the MONITORed units that probed EROFS.
    for unit, facts in repairs:
        bump(repair_unit(unit, facts, state))

    save_state(state)
    log('tick: ' + ' '.join(f'{k}={v}' for k, v in counts.items() if v))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
