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

See memory note ``claude-json-erofs-readwrite`` and
``runbooks/claude-json-bind-drift.md``.

WHY A HEALER (not a unit-file change)
-------------------------------------
A *file* carve-out — whether ``ReadWritePaths=`` or ``BindPaths=`` — always
pins an inode and can therefore dangle on atomic-replace; you cannot make a
file bind-mount replacement-proof from the unit file alone. So the robust,
version-independent fix is to respond to the SYMPTOM (the mount is no longer
writable in the namespace) regardless of which process or claude version
replaced the file. This healer COVERS every current and future claude-invoking
persistent unit by discovering them dynamically from systemd.

WHAT THIS DOES, EACH TICK, IN THIS ORDER
----------------------------------------
1. ``reconcile_pending()`` — resolve every restart this healer issued on an
   EARLIER tick that had not yet demonstrably landed. Reconciliation OWNS a
   unit's narrative until its ledger entry closes; the ordinary path below
   skips such units (``awaiting-verify``) so one repair produces at most one DM.
2. Classify every ``ourliberty-*.service`` (see CLASSIFICATION below) and probe
   the ones that carry the carve-out and are active with a MainPID.
3. Repair (restart) the MONITORed units that probed EROFS — cheap peers first,
   the drain-paying agent-hosting unit last, and only while the tick budget
   still covers the repair in full.

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

VERDICTS: EVIDENCE, NOT A WALL-CLOCK GUESS
------------------------------------------
``systemctl restart --no-block`` only ENQUEUES a job. The old verify polled
``is-active`` and, when the window expired, turned whatever it last saw into a
verdict — which is unwinnable, because the repo's own measured start latency for
these units is ~90s (``After=ourliberty-inbox-watcher.service`` on both
outbox-notifier and spec-review-runner, whose predecessor takes ~90s to
SIGTERM-drain). Worse, ``is-active`` reads 'active' for the OLD, still-dangled
process too, so a restart that was never enqueued read as a success.

``restart_and_verify`` now returns one of four verdicts from evidence:

  LANDED       identity CHANGED (``InvocationID``, or ``MainPID`` when the
               invocation was unreadable pre-restart) AND a started state.
  IN_PROGRESS  a pending ``Job=``; or deactivating/activating/reloading; or
               identity unreadable. NO DM, NO outcome record — a pending-
               verification entry is written to the state file instead.
  NOT_ENQUEUED identity UNCHANGED, no pending job, and the restart shellout
               itself hung (wedged sudo/dbus). Names the real fault.
  REJECTED     the restart was refused up front (rc != 0, or sudo/systemctl
               missing). This — and ONLY this — emits ``repair-failed:``, whose
               translation wakes Larry NOW.

Expiry of the verify window is no longer a verdict, so the window exists only to
close a fast repair out inside the same tick. A restart that has not landed
becomes a DURABLE OBLIGATION in ``STATE_FILE`` (written BEFORE the restart is
issued, so a SIGTERM mid-restart still leaves something reconcilable), which the
next tick's ``reconcile_pending`` resolves:
  active + new invocation + new-namespace probe OK    → ``rebound`` (digest)
  active + new invocation + new-namespace probe EROFS → ``still-dangled`` (page)
  still not started RESTART_LANDING_GRACE_S later     → ``repair-did-not-land``
                                                        (page — the backstop the
                                                        old comment promised and
                                                        never implemented)
  systemctl UNREADABLE for that whole window          → ``verify-unreadable``
                                                        (page). Its own subject:
                                                        a failed READ is not
                                                        absence, and the
                                                        repair-did-not-land
                                                        translation asserts the
                                                        unit "is down" — which
                                                        was never observed here.
  unit no longer present                              → GC, INFO, no page.

Reconciliation reads the unit's systemd facts DIRECTLY. The old blindness was an
accessor that returned a pid only for ``ActiveState == 'active'``: a restart that
settled ``failed``/``inactive`` was then indistinguishable from a unit that was
never in scope, forever. There is no active-only filter on this path.

DELIBERATELY NOT COPIED TO THE SIBLING RESTARTERS
--------------------------------------------------
``heal_stale_daemon_code`` and ``heal_dashboard_api_sha_drift`` conclude from
is-active alone too, and the ``Job=`` discriminator IS ported to both (a
repo-wide lint in ``scripts/tests/test_restart_verify_invariants.py`` enforces
it). The pending ledger and the identity evidence are NOT ported: their
detectors (code mtime, deployed sha) still fire on a non-active unit, so they
cannot go blind the way this one could. Do not "harmonise" that away.

Also recorded so it is not re-litigated: Medic has no stop verb
(``medic_actions.py`` issues only ``systemctl restart``; ``config/medic-
reversible-targets.json`` exposes only restart_daemon_units /
retrigger_inbox_targets), so there is no Medic-displaced-job path to guard. The
real concurrency is peer RESTARTERS (systemd's own ``Restart=``, watchdog.py,
heal_stale_daemon_code, medic). ``restart_guard`` serialises only
ourliberty-inbox-watcher.service, so a peer restart of any other unit is normal.
That is handled with EVIDENCE, not a second lock: reconciliation accepts ANY new
invocation as a landing (the mount is rebound either way, and the namespace
re-probe is the real proof) and says so in the log rather than claiming
causation.

SAFE-BY-CONSTRUCTION
--------------------
  - The probe NEVER writes: it opens ``O_RDWR`` (no create, no truncate) and
    immediately closes — content and mtime are untouched.
  - ``sudo -n`` (non-interactive): a revoked sudoers contract fails fast and
    surfaces as a ``probe-error`` DM rather than wedging.
  - Kill-switch aware: ``~/agents/healers.disabled`` disables detection AND
    repair (``main()`` short-circuits before any per-unit work).
  - Per-unit restart cooldown RATE-LIMITS repair; it does not stop it. A mount
    that stays dangled is retried every RESTART_COOLDOWN_SEC indefinitely, which
    is correct — the mount is a live outage and a later restart may win. What is
    capped is the TELLING (``escalate_still_dangled``, one gate for all three
    arms that can prove a dangle). Read those two together: the DM body must
    never claim the healer "stopped restarting", because it does not.
  - Every restart is preceded by ``reconfirm_before_restart``: detection runs
    across the whole fleet before the ordered repair pass, so the evidence can
    be minutes old, and a peer restarter may already have rebound the mount.
  - NOT_ENQUEUED does not burn the cooldown (nothing was restarted, so the next
    tick must be free to try again).
  - The tick is bounded by construction (TICK_BUDGET_S, derived from this
    healer's own ``TimeoutStartSec``): a repair that does not fit is DEFERRED to
    the next tick, never shortened. Chronic deferral escalates.
  - Idempotent: with every mount healthy the tick is a read-only no-op.
"""
from __future__ import annotations

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

# After a --no-block restart, wait this long before the FIRST identity sample,
# and between subsequent samples.
RESTART_SETTLE_S = 3

# How long we keep sampling for a restart to become PROVABLY landed before
# handing the unit to the pending ledger. This is NOT a deadline whose expiry
# means anything: expiry defers, it never pages. It is an optimisation — "did
# this land fast enough to close out inside the same tick" — so it is kept short
# to buy back the multi-unit tick budget (see TICK_BUDGET_S). A unit whose start
# job is queued behind an After= dependency (outbox-notifier and
# spec-review-runner are both ordered After=ourliberty-inbox-watcher.service,
# whose claude loop takes ~90s to SIGTERM-drain) will NOT land inside this
# window and is not supposed to: it is IN_PROGRESS and gets reconciled later.
VERIFY_WINDOW_S = 10

# How long a restart we issued may take to reach a started state before we call
# it a failure to land and page. 300s ≈ 2.5 ticks: comfortably past the ~90s
# SIGTERM drain plus After= ordering, and short enough that a genuinely dead
# unit surfaces within ~5 minutes rather than never (the old code's behaviour).
RESTART_LANDING_GRACE_S = 300

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
# zero on its own.
AGENT_DRAIN_CEILING_SEC = int(
    os.environ.get('OL_BIND_DRIFT_DRAIN_CEILING_SEC', '900')
)

# ---- tick budget (multi-unit, because one atomic replace dangles them ALL) ----
# A single atomic replacement of CLAUDE_JSON_PATH dangles every persistent
# carve-out unit in the same tick, so the cost model must be per-FLEET, not
# per-unit. #1000 is the failure this bounds: systemd SIGTERMs the healer
# mid-loop, Python's `finally` does not run on SIGTERM, and the alphabetically
# later dangled units are silently never repaired with no alert.
#
# Mirrors TimeoutStartSec on ourliberty-heal-claude-json-bind-drift.service;
# scripts/tests/test_restart_guard.py pins the two together AND asserts the unit
# timeout covers the whole fleet, not one unit.
HEALER_TIMEOUT_START_SEC = 2100

# The pre-flight re-confirmation immediately before the restart
# (reconfirm_before_restart): one capped `systemctl show`, plus the nsenter
# re-probe on the branch where a peer restarted the unit since detection. The
# common fleet-wide case pays only the show — but a budget that assumes the
# common case is not a budget.
PREFLIGHT_BUDGET_S = SYSTEMCTL_TIMEOUT_S + NSENTER_TIMEOUT_S

# Worst-case cost of repairing one pass-through (non-agent-hosting) unit:
# the pre-flight, the restart shellout, the verify window, two capped
# `systemctl show`s per sample round, and the confirming namespace re-probe.
PEER_REPAIR_BUDGET_S = (
    PREFLIGHT_BUDGET_S
    + RESTART_TIMEOUT_S + VERIFY_WINDOW_S + 2 * SYSTEMCTL_TIMEOUT_S
    + NSENTER_TIMEOUT_S
)
# …plus the full cordon-and-drain ceiling for the one unit that pays it.
AGENT_REPAIR_BUDGET_S = AGENT_DRAIN_CEILING_SEC + PEER_REPAIR_BUDGET_S

# Stop starting repairs at 90% of the unit's start timeout: the budget must be
# enforced BEFORE entering guarded_restart, because enforcing it inside would be
# enforcing it in the process systemd is about to kill.
TICK_BUDGET_S = int(HEALER_TIMEOUT_START_SEC * 0.9)

# A dangled unit deferred this many consecutive ticks is being STARVED by its
# peers (deferral is order-dependent), which is invisible without saying so.
DEFER_ESCALATE_TICKS = 3

# Per-subject DM cooldown for the escalation paths. Larger than larry_alerts'
# built-in 60-min window so this is the binding constraint for a chronically
# broken unit/host.
ESCALATION_COOLDOWN_SEC = 6 * 60 * 60

# is-active states that mean the restart's START half has begun or finished.
_RESTART_STARTED_STATES = frozenset({'active', 'activating', 'reloading'})
# States that mean systemd is demonstrably still working on this unit. Never a
# verdict on their own — they defer.
_RESTART_IN_PROGRESS_STATES = frozenset({'deactivating', 'activating', 'reloading'})

# ---- restart verdicts ----
VERDICT_LANDED = 'landed'
VERDICT_IN_PROGRESS = 'in-progress'
VERDICT_NOT_ENQUEUED = 'not-enqueued'
VERDICT_REJECTED = 'rejected'

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
# Distinct from OUTCOME_HEALTHY on purpose: "a peer rebound this while we were
# repairing its neighbours" must not read in the tick line as "was never
# dangled". They are the same end state reached by different systems, and only
# one of them means the fleet had a simultaneous dangle.
OUTCOME_HEALED_BY_PEER = 'healed-by-peer'
OUTCOME_EPHEMERAL_DANGLED = 'ephemeral-dangled'
OUTCOME_EPHEMERAL_PROBE_ERROR = 'ephemeral-probe-error'
OUTCOME_PROBE_ERROR = 'probe-error'
OUTCOME_AWAITING_VERIFY = 'awaiting-verify'
OUTCOME_VERIFY_INCONCLUSIVE = 'verify-inconclusive'
OUTCOME_RESTART_IN_PROGRESS = 'restart-in-progress'
OUTCOME_REBOUND = 'rebound'
OUTCOME_STILL_DANGLED = 'still-dangled'
OUTCOME_REPAIR_FAILED = 'repair-failed'
OUTCOME_REPAIR_NOT_ENQUEUED = 'repair-not-enqueued'
OUTCOME_REPAIR_DID_NOT_LAND = 'repair-did-not-land'
OUTCOME_VERIFY_UNREADABLE = 'verify-unreadable'
OUTCOME_COOLDOWN_DANGLED = 'cooldown-dangled'
OUTCOME_REPAIR_SKIPPED_PEER = 'repair-skipped-peer-active'
OUTCOME_DEFERRED = 'deferred'
OUTCOME_PENDING_GC = 'pending-gc'

OUTCOMES = (
    OUTCOME_SKIP_ONESHOT, OUTCOME_SKIP_EPHEMERAL, OUTCOME_SKIP_INACTIVE,
    OUTCOME_SKIP_NOCARVE, OUTCOME_SKIP_UNKNOWN, OUTCOME_SKIP_NSGONE,
    OUTCOME_HEALTHY, OUTCOME_HEALED_BY_PEER, OUTCOME_EPHEMERAL_DANGLED,
    OUTCOME_EPHEMERAL_PROBE_ERROR,
    OUTCOME_PROBE_ERROR, OUTCOME_AWAITING_VERIFY, OUTCOME_VERIFY_INCONCLUSIVE,
    OUTCOME_RESTART_IN_PROGRESS, OUTCOME_REBOUND, OUTCOME_STILL_DANGLED,
    OUTCOME_REPAIR_FAILED, OUTCOME_REPAIR_NOT_ENQUEUED,
    OUTCOME_REPAIR_DID_NOT_LAND, OUTCOME_VERIFY_UNREADABLE,
    OUTCOME_COOLDOWN_DANGLED,
    OUTCOME_REPAIR_SKIPPED_PEER, OUTCOME_DEFERRED, OUTCOME_PENDING_GC,
)

# Sentinel returned by inspect_unit when a unit needs the (ordered, budgeted)
# repair pass. Never a tick-line bucket — main() turns it into one.
REPAIR_REQUESTED = '__repair-requested__'

# Probe exit-code contract (see _PROBE_SNIPPET below).
_PROBE_OK = 0       # opened O_RDWR cleanly → mount writable (healthy)
_PROBE_EROFS = 2    # OSError errno == EROFS → mount dangled (repair)
_PROBE_OTHER = 3    # any other OSError (e.g. ENOENT) → probe-error (no repair)
# Not an nsenter exit code — a caller-side classification of a non-OK/non-EROFS
# result where the target's mount namespace is already gone (the process has
# exited / is mid-reap between MainPID enumeration and the probe — a benign
# TOCTOU race on short-lived Type=simple units).
_PROBE_GONE = 4     # target namespace already gone mid-probe → benign skip

# The in-namespace probe: open the file O_RDWR (write-intent, but NO create and
# NO truncate) and close it immediately. On a read-only mount the open(2) itself
# returns EROFS even for root, so this faithfully reports "can a process in this
# namespace write the file?" without ever modifying it.
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


# -------------------- durable state --------------------
#
# {'services': {unit: {'last_restart_ts', 'last_alert_ts', 'deferred_ticks',
#                      'last_defer_alert_ts'}},
#  'pending':  {unit: {'restarted_ts', 'prior_invocation', 'prior_pid',
#                      'grace_deadline_ts'}},
#  'coverage': [unit, …]}
#
# 'pending' is the obligation ledger: a restart we issued that has not yet
# demonstrably landed. It is written BEFORE the restart shellout, so a SIGTERM
# mid-restart (Python's `finally` does not run on SIGTERM) still leaves a
# reconcilable entry rather than a silently forgotten repair.

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        data = {}
    if not isinstance(data.get('services'), dict):
        data['services'] = {}
    if not isinstance(data.get('pending'), dict):
        data['pending'] = {}
    # DO NOT default an ABSENT 'coverage' to []. coverage_delta reads a MISSING
    # key as "no baseline recorded yet" and [] as "the baseline is empty", and
    # those must stay distinguishable: defaulting absent -> [] makes a total
    # collapse to zero monitored units read as a first run, on every tick,
    # forever. A corrupt (non-list) value is DROPPED rather than emptied, so the
    # next tick re-baselines honestly instead of silently claiming every unit
    # just left — and so a hand-edited state file cannot make `set(...)` raise
    # and kill the tick.
    if 'coverage' in data and not isinstance(data['coverage'], list):
        del data['coverage']
    if not isinstance(data.get('coverage_departed'), dict):
        data['coverage_departed'] = {}
    return data


def save_state(state: dict) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _svc(state: dict, unit: str) -> dict:
    return state.setdefault('services', {}).setdefault(unit, {})


def in_restart_cooldown(state: dict, unit: str, now: Optional[float] = None) -> bool:
    entry = state.get('services', {}).get(unit)
    if not entry:
        return False
    last = entry.get('last_restart_ts')
    if not isinstance(last, (int, float)):
        return False
    now = now if now is not None else time.time()
    return (now - last) < RESTART_COOLDOWN_SEC


def mark_restarted(state: dict, unit: str, now: Optional[float] = None) -> None:
    _svc(state, unit)['last_restart_ts'] = now if now is not None else time.time()


def in_alert_cooldown(state: dict, unit: str, now: Optional[float] = None) -> bool:
    entry = state.get('services', {}).get(unit)
    if not entry:
        return False
    last = entry.get('last_alert_ts')
    if not isinstance(last, (int, float)):
        return False
    now = now if now is not None else time.time()
    return (now - last) < ESCALATION_COOLDOWN_SEC


def mark_alerted(state: dict, unit: str, now: Optional[float] = None) -> None:
    _svc(state, unit)['last_alert_ts'] = now if now is not None else time.time()


# ---- pending-verification ledger ----

def pending_entry(state: dict, unit: str) -> Optional[dict]:
    entry = state.get('pending', {}).get(unit)
    return entry if isinstance(entry, dict) else None


def open_pending(state: dict, unit: str, prior_invocation: str,
                 prior_pid: Optional[int], now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    state.setdefault('pending', {})[unit] = {
        'restarted_ts': now,
        'prior_invocation': prior_invocation or '',
        'prior_pid': int(prior_pid or 0),
        'grace_deadline_ts': now + RESTART_LANDING_GRACE_S,
    }


def close_pending(state: dict, unit: str) -> None:
    state.setdefault('pending', {}).pop(unit, None)


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


def unit_pending_job(unit: str) -> str:
    """Return the unit's enqueued systemd job id, or '' if none.

    `systemctl show <unit> --property=Job` prints `Job=` (empty) for a settled
    unit and `Job=<id> <...>` while a job is enqueued and waiting — typically
    held behind an After= ordering dependency that is still draining
    (outbox-notifier and spec-review-runner are both ordered After=
    ourliberty-inbox-watcher.service, whose claude loop takes ~90s to
    SIGTERM-drain). Returns '' on empty / timeout / missing binary, so only a
    CONFIRMED pending job counts as in-progress and an unreadable probe can
    never manufacture a false in-progress verdict that suppresses a real
    failure. Ported verbatim in semantics from
    heal_stale_daemon_code._unit_pending_job — one contract, one behaviour.
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


def unit_active_state(unit: str) -> str:
    """`systemctl is-active <unit>`'s state string, or 'unknown' on error.

    Kept for the manual/runbook shape and for logging. It is NEVER the basis of
    a verdict on its own: 'active' reads identically for the OLD, still-dangled
    process, which is exactly how a restart that never happened used to be
    reported as a successful repair.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
        return (result.stdout or '').strip() or 'unknown'
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 'unknown'


class UnitFacts(NamedTuple):
    """One direct read of a unit's systemd facts.

    ``present`` False means `systemctl show` could not be read at all — the
    caller must treat that as UNKNOWN, never as 'not in scope' and never as a
    landing. There is deliberately no ActiveState filter here: a unit that
    settled `failed` must stay READABLE, or a restart that never landed becomes
    invisible to every later tick (the old target_main_pid blindness)."""
    unit: str
    present: bool
    type_: str
    active_state: str
    main_pid: Optional[int]
    read_write_paths: str
    restart_policy: Optional[str]
    triggered_by: str
    invocation_id: Optional[str]


_FACT_PROPS = ['Type', 'ActiveState', 'MainPID', 'ReadWritePaths',
               'TriggeredBy', 'Restart', 'InvocationID']


def unit_facts(unit: str) -> UnitFacts:
    props = systemctl_show(unit, _FACT_PROPS)
    if not props:
        return UnitFacts(unit, False, '', 'unknown', None, '', None, '', None)
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
        invocation_id=props.get('InvocationID'),
    )


def carveout_present(read_write_paths: str) -> bool:
    """True iff CLAUDE_JSON_PATH appears as a whitespace-separated token in the
    unit's effective ReadWritePaths (so a substring of a longer path can't
    false-match)."""
    return CLAUDE_JSON_PATH in (read_write_paths or '').split()


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

    Keys on ns/mnt rather than the coarser /proc/<pid> dir because a just-exited
    / zombie process retains its /proc/<pid> dir during the kernel reaping window
    while its /proc/<pid>/ns/mnt has ALREADY been released."""
    return os.path.exists(f'/proc/{pid}/ns/mnt')


def probe_namespace_writable(pid: int) -> int:
    """Probe whether CLAUDE_JSON_PATH is writable inside pid's mount namespace.

    Returns one of _PROBE_OK / _PROBE_EROFS / _PROBE_OTHER / _PROBE_GONE."""
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
    # 126/127 = exec failure). ns/mnt gone → benign mid-probe exit race (INFO);
    # ns/mnt present → genuine probe-blind (WARN).
    stderr = result.stderr.strip()
    if not _namespace_probeable(pid):
        log(f'probe pid={pid}: mount namespace gone mid-probe '
            f'(rc={result.returncode} stderr={stderr[:160]!r}); skipping, '
            f'will re-probe next tick', 'INFO')
        return _PROBE_GONE
    log(f'probe pid={pid}: non-EROFS rc={result.returncode} '
        f'stderr={stderr[:160]!r}', 'WARN')
    return _PROBE_OTHER


# -------------------- restart + evidence-based verify --------------------

class RestartOutcome(NamedTuple):
    verdict: str
    detail: str
    samples: int
    facts: Optional[UnitFacts] = None


def _identity_changed(prior_invocation: str, prior_pid: Optional[int],
                      facts: UnitFacts) -> Optional[bool]:
    """True / False / None(unknown) — did a NEW activation of this unit appear?

    InvocationID is systemd's per-activation identity (32 hex chars for a live
    activation, empty for an inactive unit) and changes on every activation, so
    it distinguishes "the same process we were about to restart" from "a new
    one" — which a bare is-active string can never do. MainPID is the fallback
    when the invocation was unreadable pre-restart. When NEITHER was captured we
    return None: unknown, and unknown may never conclude LANDED."""
    if not facts.present:
        return None
    if prior_invocation:
        if facts.invocation_id is None:
            return None  # property unreadable now — cannot conclude
        return bool(facts.invocation_id) and facts.invocation_id != prior_invocation
    if prior_pid:
        if facts.main_pid is None:
            return False  # no live main process → certainly not a new activation
        return facts.main_pid != prior_pid
    return None  # no identity captured before the restart


def verify_verdict(prior_invocation: str, prior_pid: Optional[int],
                   facts: UnitFacts, pending_job: str,
                   shellout_hung: bool) -> tuple[str, str]:
    """The whole verdict rule table, in one ordered place.

    There is NO wall-clock term in any rule. Evaluated in this order:
      1. identity CHANGED and a started state          → LANDED
      2. a confirmed pending systemd Job               → IN_PROGRESS
      3. deactivating / activating / reloading         → IN_PROGRESS
      4. identity unreadable                           → IN_PROGRESS
      5. identity UNCHANGED, no job, shellout hung     → NOT_ENQUEUED
      6. anything else                                 → IN_PROGRESS
    Note rule 2 beats rule 5: a pending Job with unchanged identity is
    IN_PROGRESS, which is exactly what keeps the After=-queued peers off the
    pager. REJECTED is never produced here — it is an up-front rejection of the
    restart command itself, decided before any of this runs."""
    changed = _identity_changed(prior_invocation, prior_pid, facts)
    state = facts.active_state if facts.present else 'unknown'
    if changed and state in _RESTART_STARTED_STATES:
        return VERDICT_LANDED, (
            f'new systemd invocation observed; unit is {state!r} '
            f'(a peer restarter may have caused it — causation unproven, the '
            f'namespace re-probe is the evidence that matters)'
        )
    if pending_job:
        return VERDICT_IN_PROGRESS, (
            f'systemd still has job {pending_job!r} enqueued for this unit '
            f'(state {state!r}) — typically held behind an After= ordering '
            f'dependency that is still draining'
        )
    if state in _RESTART_IN_PROGRESS_STATES:
        return VERDICT_IN_PROGRESS, f'unit is {state!r}'
    if changed is None:
        return VERDICT_IN_PROGRESS, (
            f'unit identity unreadable (state {state!r}); cannot conclude'
        )
    if not changed and shellout_hung:
        return VERDICT_NOT_ENQUEUED, (
            'the restart command itself hung and no systemd job appeared; the '
            'unit still runs its ORIGINAL invocation'
        )
    return VERDICT_IN_PROGRESS, (
        f'unit is {state!r} with no new invocation and no pending job yet'
    )


def restart_and_verify(unit: str, prior_invocation: str,
                       prior_pid: Optional[int]) -> RestartOutcome:
    """`sudo -n systemctl restart --no-block <unit>`, then gather EVIDENCE.

    Returns a RestartOutcome whose verdict is one of VERDICT_*. The poll ends
    early the instant LANDED is provable (so the fast path — a pass-through peer
    that comes back in one sample — is not taxed by the window); otherwise it
    samples to VERIFY_WINDOW_S and reports whatever the rule table concluded.
    Window expiry is NOT a verdict: IN_PROGRESS defers to the pending ledger."""
    shellout_hung = False
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', '--no-block', unit],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Nothing is known to have been enqueued. This is NOT a failure verdict
        # and NOT a success: the rule table resolves it from evidence below.
        result = None
        shellout_hung = True
    except FileNotFoundError:
        return RestartOutcome(
            VERDICT_REJECTED, 'sudo or systemctl not found in PATH', 0, None)

    if result is not None and result.returncode != 0:
        return RestartOutcome(
            VERDICT_REJECTED,
            (result.stderr or '').strip() or f'rc={result.returncode}',
            0, None,
        )

    deadline = time.monotonic() + VERIFY_WINDOW_S
    samples = 0
    verdict, detail = VERDICT_IN_PROGRESS, 'no sample taken'
    facts: Optional[UnitFacts] = None
    while True:
        time.sleep(RESTART_SETTLE_S)
        samples += 1
        facts = unit_facts(unit)
        job = unit_pending_job(unit)
        verdict, detail = verify_verdict(
            prior_invocation, prior_pid, facts, job, shellout_hung)
        if verdict == VERDICT_LANDED:
            return RestartOutcome(verdict, detail, samples, facts)
        if time.monotonic() >= deadline:
            return RestartOutcome(verdict, detail, samples, facts)


# -------------------- DM to Larry --------------------

def _import_larry_alerts():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import larry_alerts as la  # noqa: E402
    return la


def dm_rebound(unit: str, active_state: str, evidence: str) -> bool:
    """The ONLY healed=True record. Emitted exclusively when a probe of the
    CURRENT MainPID, in the namespace of a NEW invocation, returned _PROBE_OK.

    Positive namespace evidence is what makes this honest: the mount is writable
    regardless of who rebound it. (Identity governs a different question —
    whether WE restarted it, hence whether to burn the cooldown.)"""
    try:
        la = _import_larry_alerts()
        body = (
            f'Auto-repaired {unit}: its {CLAUDE_JSON_PATH} bind-mount had '
            f'dangled (EROFS — the file was atomically replaced on the host '
            f'after the unit started), so claude was failing with '
            f'`[claude exit 1]`. Restarted the unit to re-bind the current '
            f'inode. Verified: systemd reports ActiveState={active_state} under '
            f'a NEW invocation, and a fresh probe inside that new mount '
            f'namespace opened {CLAUDE_JSON_PATH} O_RDWR successfully. '
            f'({evidence})'
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
        log(f'dm_rebound failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_repair_failed(unit: str, detail: str) -> bool:
    """Escalation DM when the restart command was REJECTED up front.

    Reserved for that one cause. Its translation is URGENT/NOW and asserts there
    is no automatic retry, so it must never be reachable from "the unit has not
    come back YET" — that is what the pending ledger is for."""
    try:
        la = _import_larry_alerts()
        body = (
            f'Auto-repair of {unit} was REJECTED up front: '
            f'{detail or "(no detail)"}\n\n'
            f'Its {CLAUDE_JSON_PATH} bind-mount dangled (EROFS) and systemd '
            f'refused the restart command outright — no restart job was ever '
            f'created, so nothing will retry it.\n\n'
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


def dm_repair_not_enqueued(unit: str, detail: str) -> bool:
    """Escalation DM when the restart shellout HUNG and no job appeared.

    Deliberately NOT dm_still_dangled: under this fault the carve-out is fine
    and nothing is replacing the file — the healer's own privilege path is
    broken, and sending Larry to `grep ReadWritePaths` is the wrong
    investigation."""
    try:
        la = _import_larry_alerts()
        body = (
            f'Auto-repair of {unit} never started: the restart command itself '
            f'hung ({detail or "no detail"}). {unit} is still running its '
            f'ORIGINAL invocation with a dangled {CLAUDE_JSON_PATH} mount '
            f'(EROFS), and systemd shows no restart job enqueued for it.\n\n'
            f'This points at the healer\'s privilege/IPC path, not at the unit: '
            f'`sudo -n` or systemd/dbus is unresponsive. Check '
            f'`systemctl is-system-running`, the sudoers NOPASSWD contract, and '
            f'dbus.\n\nThe healer will retry on its next 2-minute tick (no '
            f'cooldown was burned, because nothing was restarted).'
        )
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=f'repair-not-enqueued:{unit}',
            message=body,
            suggested_action=(
                f'systemctl is-system-running ; sudo -n systemctl restart {unit}'
            ),
        )
    except Exception as e:
        log(f'dm_repair_not_enqueued failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_repair_did_not_land(unit: str, observed_state: str, waited_s: float,
                           identity_changed: bool) -> bool:
    """Escalation DM when a restart we issued never reached a started state
    within RESTART_LANDING_GRACE_S. This is the backstop for the case the old
    code reported as a successful 'rebound' and then went permanently blind to.

    ``identity_changed`` picks the narrative, because there are two ways to fail
    to land and they are not the same fault. The body used to hardcode the first
    one ("with no new invocation — the stop half ran but the start half has
    not") and send it for BOTH, including the exhausted-StartLimitBurst case the
    alert itself names — where a new invocation demonstrably DID appear and then
    died. Do not collapse these back into one string; a page that misdescribes
    the evidence sends the reader to the wrong half of the journal."""
    try:
        la = _import_larry_alerts()
        if identity_changed:
            what = (
                f'A NEW systemd invocation DID appear — so the start half ran — '
                f'but the unit is now {observed_state!r}. It came back and did '
                f'not stay up.'
            )
        else:
            what = (
                f'systemd still reports the unit as {observed_state!r} with no '
                f'new invocation — the stop half ran but the start half has not.'
            )
        body = (
            f'Auto-repair of {unit} DID NOT LAND. The healer restarted it '
            f'{int(waited_s)}s ago to re-bind a dangled {CLAUDE_JSON_PATH} mount '
            f'(EROFS). {what}\n\n'
            f'The unit is not running, so anything it hosts is failing. Check '
            f'why:\n'
            f'  journalctl -u {unit} -n 80\n'
            f'  systemctl status {unit}\n\n'
            f'Common causes: an ExecStart failure, a failed dependency, or an '
            f'exhausted StartLimitBurst.'
        )
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=f'repair-did-not-land:{unit}',
            message=body,
            suggested_action=(
                f'journalctl -u {unit} -n 80 ; sudo systemctl restart {unit}'
            ),
        )
    except Exception as e:
        log(f'dm_repair_did_not_land failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_verify_unreadable(unit: str, waited_s: float) -> bool:
    """Escalation DM when `systemctl show` was UNREADABLE for the whole landing
    grace, so the healer cannot say whether its restart landed.

    Its own subject, deliberately. This used to fall into
    ``repair-did-not-land``, whose translation tells Larry the unit "is sitting
    inactive/failed" and "is down" — neither of which was observed. A failed
    read is not absence: the unit may be perfectly healthy and the healer blind.
    The fault to investigate is systemd/dbus, not the unit's ExecStart."""
    try:
        la = _import_larry_alerts()
        body = (
            f'The healer cannot VERIFY its own repair of {unit}. It restarted '
            f'the unit {int(waited_s)}s ago to re-bind a dangled '
            f'{CLAUDE_JSON_PATH} mount (EROFS), and every `systemctl show` since '
            f'has been unreadable (timeout, or systemctl missing).\n\n'
            f'This says nothing about the unit — it may be running fine. What is '
            f'broken is the healer\'s read path, and while it stays broken this '
            f'healer cannot detect OR repair a dangle on any unit.\n\n'
            f'  systemctl is-system-running\n'
            f'  systemctl status {unit}'
        )
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=f'verify-unreadable:{unit}',
            message=body,
            suggested_action=(
                f'systemctl is-system-running ; systemctl status {unit}'
            ),
        )
    except Exception as e:
        log(f'dm_verify_unreadable failed: {type(e).__name__}: {e}', 'WARN')
        return False


def dm_still_dangled(unit: str, context: str) -> bool:
    """Escalation DM for a PROVEN failed repair: the namespace re-probe (or a
    later tick's probe inside the restart cooldown) concluded EROFS.

    The body says what the healer ACTUALLY does next, which is not what it used
    to claim. It said "Suppressing further restarts to avoid a loop"; the only
    brake in the code is RESTART_COOLDOWN_SEC, so the healer goes on restarting
    every 15 minutes for as long as the mount stays dangled — measured at 23
    restarts over 6 hours. What is suppressed is the TELLING, by
    ESCALATION_COOLDOWN_SEC. Retrying is the right behaviour (the mount is an
    active outage and a later restart may well win); describing it as "stopped"
    is what sent Larry looking for a healer that had given up."""
    try:
        la = _import_larry_alerts()
        body = (
            f'{unit} is STILL read-only ({CLAUDE_JSON_PATH}, EROFS) {context}.\n\n'
            f'The healer has NOT given up: it retries the restart every '
            f'{RESTART_COOLDOWN_SEC // 60} min for as long as the mount stays '
            f'dangled. What is rate-limited is this message — at most once every '
            f'{ESCALATION_COOLDOWN_SEC // 3600}h per unit, so the retries go on '
            f'quietly in between (`grep still-dangled` in the healer log to see '
            f'them).\n\n'
            f'Restarting is not fixing it, which points at something structural: '
            f'is the carve-out still in the unit file? is something replacing '
            f'{CLAUDE_JSON_PATH} every few minutes?'
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


def escalate_still_dangled(state: dict, unit: str, context: str,
                           now: Optional[float] = None) -> bool:
    """The ONE gate every still-dangled escalation goes through. True iff a DM
    was emitted.

    There are three arms that can prove a mount is still dangled — the
    cooldown-window probe in ``inspect_unit``, the post-restart re-probe in
    ``repair_unit``, and the deferred re-probe in ``_reconcile_one`` — and they
    used to disagree: only the first consulted ``in_alert_cooldown``, while the
    other two DM'd unconditionally AND stamped ``mark_alerted``. The stamp meant
    the gated arm was permanently suppressed by the ungated ones, so the
    binding constraint for a chronically broken unit was larry_alerts' 60-min
    warning window, not the ESCALATION_COOLDOWN_SEC this module documents.
    One function, three call sites, so a fourth arm inherits the gate."""
    if in_alert_cooldown(state, unit, now=now):
        log(f'{unit}: still dangled — DM suppressed, inside the '
            f'{ESCALATION_COOLDOWN_SEC // 3600}h escalation window ({context}). '
            f'Restart retries continue on the {RESTART_COOLDOWN_SEC // 60}min '
            f'cooldown.', 'WARN')
        return False
    dm_still_dangled(unit, context)
    mark_alerted(state, unit, now=now)
    save_state(state)
    return True


def dm_repair_deferred(unit: str, ticks: int) -> bool:
    """Escalation DM for STARVATION: a dangled unit whose repair has not fit in
    the tick budget for several consecutive ticks. Deferral is normal once;
    chronic deferral is an outage nobody is being told about."""
    try:
        la = _import_larry_alerts()
        body = (
            f'{unit} has a dangled {CLAUDE_JSON_PATH} mount (EROFS) and its '
            f'repair has been DEFERRED for {ticks} consecutive ticks: each tick '
            f'the healer ran out of its time budget ({TICK_BUDGET_S}s, bounded '
            f'by TimeoutStartSec) repairing other units first, so this one is '
            f'being starved.\n\n'
            f'It is still broken — claude in that unit is failing with '
            f'`[claude exit 1]`. Repair it by hand, then look at why so many '
            f'units are dangling at once:\n  sudo systemctl restart {unit}'
        )
        return la.append_alert(
            source='heal-claude-json-bind-drift',
            severity='warning',
            subject=f'repair-deferred:{unit}',
            message=body,
            suggested_action=f'sudo systemctl restart {unit}',
        )
    except Exception as e:
        log(f'dm_repair_deferred failed: {type(e).__name__}: {e}', 'WARN')
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


# -------------------- pending-verification reconciliation --------------------

def reconcile_pending(state: dict, known_units: set[str],
                      now: Optional[float] = None) -> dict[str, str]:
    """Resolve every open restart obligation. Runs FIRST, before any
    classification or probing, and OWNS the narrative of every unit it touches
    for this tick (main() skips those units in the ordinary path, so one repair
    can never produce two DMs).

    Reads each unit's systemd facts DIRECTLY — no active-only accessor — which
    is what makes a restart that settled `failed` visible at all."""
    now = now if now is not None else time.time()
    results: dict[str, str] = {}
    pending = dict(state.get('pending') or {})
    if not pending:
        return results
    for unit, entry in pending.items():
        if not isinstance(entry, dict):
            close_pending(state, unit)
            results[unit] = OUTCOME_PENDING_GC
            continue
        results[unit] = _reconcile_one(state, unit, entry, known_units, now)
    save_state(state)
    return results


def _reconcile_one(state: dict, unit: str, entry: dict,
                   known_units: set[str], now: float) -> str:
    if unit not in known_units:
        # GC: the unit was renamed/removed. Nothing to repair, nobody to page —
        # but the ledger must not grow without bound.
        close_pending(state, unit)
        log(f'{unit}: pending restart-verification dropped — the unit is no '
            f'longer present in systemd', 'INFO')
        return OUTCOME_PENDING_GC

    prior_invocation = str(entry.get('prior_invocation') or '')
    prior_pid = entry.get('prior_pid') or None
    restarted_ts = entry.get('restarted_ts') or now
    grace = entry.get('grace_deadline_ts')
    if not isinstance(grace, (int, float)):
        grace = restarted_ts + RESTART_LANDING_GRACE_S

    facts = unit_facts(unit)
    changed = _identity_changed(prior_invocation, prior_pid, facts)

    if changed and facts.active_state == 'active':
        # It came back. Whether WE caused it is unproven and does not matter —
        # the namespace re-probe is the evidence.
        probe = probe_namespace_writable(facts.main_pid) if facts.main_pid else None
        if probe == _PROBE_OK:
            close_pending(state, unit)
            dm_rebound(unit, facts.active_state,
                       'resolved by deferred verification on a later tick')
            log(f'{unit}: REBOUND (deferred verify) — new invocation is active '
                f'and its namespace probe is writable')
            return OUTCOME_REBOUND
        if probe == _PROBE_EROFS:
            close_pending(state, unit)
            escalate_still_dangled(
                state, unit,
                'after the auto-restart landed — the NEW namespace probed '
                'read-only, so the restart did not fix it', now=now)
            log(f'{unit}: STILL DANGLED after a landed restart — proven failed '
                f'repair', 'WARN')
            return OUTCOME_STILL_DANGLED
        # Probe could not conclude (namespace gone / probe-blind / no MainPID).
        # Say nothing rather than guess; hand the unit back to the ordinary
        # probe path once the grace window is spent.
        if now >= grace:
            close_pending(state, unit)
            log(f'{unit}: restart landed (new invocation, active) but the '
                f'namespace re-probe never concluded within '
                f'{RESTART_LANDING_GRACE_S}s — closing the obligation and '
                f'returning the unit to the ordinary probe path', 'WARN')
            return OUTCOME_VERIFY_INCONCLUSIVE
        return OUTCOME_AWAITING_VERIFY

    if now >= grace:
        close_pending(state, unit)
        waited = now - restarted_ts
        if not facts.present:
            # A failed READ is not absence. We know nothing about the unit —
            # only that our own read path is broken. Say that, under its own
            # subject, instead of telling Larry the unit is down.
            dm_verify_unreadable(unit, waited)
            log(f'{unit}: CANNOT VERIFY REPAIR — systemctl unreadable for the '
                f'whole {RESTART_LANDING_GRACE_S}s grace after the restart; the '
                f'unit\'s state is unknown, not known-bad', 'WARN')
            return OUTCOME_VERIFY_UNREADABLE
        dm_repair_did_not_land(unit, facts.active_state, waited,
                               identity_changed=bool(changed))
        log(f'{unit}: REPAIR DID NOT LAND — {facts.active_state!r} '
            f'{int(waited)}s after the restart '
            f'({"a new invocation appeared and did not stay up" if changed else "no new invocation"})',
            'WARN')
        return OUTCOME_REPAIR_DID_NOT_LAND

    log(f'{unit}: awaiting restart verification — '
        f'{facts.active_state!r}, no new invocation yet '
        f'({int(grace - now)}s of grace left)')
    return OUTCOME_AWAITING_VERIFY


# -------------------- detection --------------------

def probe_ephemeral(unit: str, pid: int) -> str:
    """Detection-only path for a run-to-completion unit.

    Separate function BY CONSTRUCTION: it has no reference to restart_guard or
    restart_and_verify, so the ephemeral class cannot reach the repair path even
    by a careless edit. That matters — an `if` guard is one line away from
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
    REPAIR_REQUESTED when a MONITORed unit needs the ordered, budgeted repair
    pass. Never restarts anything itself."""
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
        return OUTCOME_SKIP_NSGONE
    if probe == _PROBE_OTHER:
        if not in_alert_cooldown(state, unit, now=now):
            dm_probe_blind(unit)
            mark_alerted(state, unit, now=now)
            save_state(state)
        return OUTCOME_PROBE_ERROR

    # _PROBE_EROFS — the mount has dangled.
    if in_restart_cooldown(state, unit, now=now):
        escalate_still_dangled(
            state, unit,
            f'within {RESTART_COOLDOWN_SEC // 60} min of an auto-restart',
            now=now)
        log(f'{unit}: STILL DANGLED inside restart cooldown — deferring the '
            f'retry until the cooldown expires', 'WARN')
        return OUTCOME_COOLDOWN_DANGLED
    return REPAIR_REQUESTED


# -------------------- repair --------------------

def reconfirm_before_restart(unit: str, facts: UnitFacts):
    """Re-read the unit immediately before restarting it. Returns fresh
    ``UnitFacts`` to restart with, or a tick-line outcome STRING meaning DO NOT
    RESTART.

    WHY THE EVIDENCE IS STALE BY THE TIME WE GET HERE. ``main()`` probes the
    WHOLE fleet first and repairs in a second, ordered pass, and the
    agent-hosting unit is deliberately repaired LAST — measured at 408s of
    staleness in a full-fleet tick — after which ``guarded_restart`` may drain
    for a further AGENT_DRAIN_CEILING_SEC before the restart actually lands. The
    module docstring already records that PEER RESTARTERS (systemd's own
    ``Restart=``, watchdog, heal_stale_daemon_code, medic) rebind this same
    mount. So a decision taken on detection-time facts can be ~20 minutes old
    when it fires, and the unit it restarts may have been healthy for most of
    them — followed by a ``rebound`` DM claiming we re-bound a dangle that no
    longer existed.

    THE RE-PROBE IS CONDITIONAL, AND THAT IS WHAT MAKES IT CHEAP. A dangle is a
    property of the mount NAMESPACE, which is fixed for the life of an
    invocation: nothing re-binds inside a running namespace, and ProtectHome's
    read-only /home persists with it. So an UNCHANGED invocation is still
    dangled BY CONSTRUCTION and there is nothing to re-probe. Only a NEW
    invocation can have healed it, and only that branch pays the nsenter."""
    fresh = unit_facts(unit)
    if not fresh.present:
        log(f'{unit}: systemctl became unreadable between detection and repair; '
            f'NOT restarting on stale evidence', 'WARN')
        return OUTCOME_SKIP_UNKNOWN
    if fresh.active_state != 'active' or fresh.main_pid is None:
        log(f'{unit}: no longer active ({fresh.active_state!r}) between '
            f'detection and repair; NOT restarting — nothing to re-bind, and '
            f'the next tick re-evaluates', 'WARN')
        return OUTCOME_SKIP_INACTIVE
    if _identity_changed(facts.invocation_id or '', facts.main_pid, fresh) is not True:
        # Same (or unknowable) invocation → same namespace → still dangled.
        return fresh
    probe = probe_namespace_writable(fresh.main_pid)
    if probe == _PROBE_OK:
        log(f'{unit}: a peer restarter rebound the mount between detection and '
            f'this repair — the NEW invocation probes writable. NOT restarting; '
            f'their restart served this need.')
        return OUTCOME_HEALED_BY_PEER
    if probe == _PROBE_GONE:
        return OUTCOME_SKIP_NSGONE
    if probe == _PROBE_OTHER:
        log(f'{unit}: a peer restarted this unit since detection and the new '
            f'namespace could not be probed; NOT restarting on evidence that no '
            f'longer describes the running process', 'WARN')
        return OUTCOME_PROBE_ERROR
    return fresh  # _PROBE_EROFS — a peer restart did not fix it. Restart.


def repair_unit(unit: str, facts: UnitFacts, state: dict,
                now: Optional[float] = None) -> str:
    """Restart a dangled MONITORed unit and record what was actually observed.

    Operation order is load-bearing: the pending-verification entry is written
    (and persisted) BEFORE the restart is issued, so a SIGTERM landing anywhere
    after this point still leaves a reconcilable obligation. The old code
    stamped the cooldown only after the restart returned, so a SIGTERM mid-drain
    lost the stamp entirely."""
    now = now if now is not None else time.time()

    # Detection may be many minutes old by now (see reconfirm_before_restart).
    # Re-read BEFORE opening the obligation, so a unit we decline to restart
    # leaves no pending entry to reconcile.
    reconfirmed = reconfirm_before_restart(unit, facts)
    if isinstance(reconfirmed, str):
        return reconfirmed
    facts = reconfirmed

    prior_invocation = facts.invocation_id or ''
    prior_pid = facts.main_pid

    open_pending(state, unit, prior_invocation, prior_pid, now=now)
    save_state(state)

    guarded = restart_guard.guarded_restart(
        unit, 'claude-json-bind-drift',
        lambda: restart_and_verify(unit, prior_invocation, prior_pid),
        ceiling_sec=AGENT_DRAIN_CEILING_SEC,
        on_ceiling='force',  # a dangled mount is an outage; it must be repaired
        log=log,
    )
    if not guarded.performed:
        # Peer-active skip only (on_ceiling='force' always restarts otherwise).
        # Nothing was restarted: drop the obligation, burn no cooldown.
        close_pending(state, unit)
        save_state(state)
        log(f'{unit}: repair skipped — a peer restarter holds the cordon lock; '
            f'its restart rebinds the mount too. Re-checking next tick.')
        return OUTCOME_REPAIR_SKIPPED_PEER

    outcome: RestartOutcome = guarded.result

    if outcome.verdict == VERDICT_REJECTED:
        close_pending(state, unit)
        mark_restarted(state, unit, now=now)  # bound hammering on a refused verb
        save_state(state)
        dm_repair_failed(unit, outcome.detail)
        log(f'{unit}: RESTART REJECTED — {outcome.detail[:160]!r}', 'WARN')
        return OUTCOME_REPAIR_FAILED

    if outcome.verdict == VERDICT_NOT_ENQUEUED:
        # Nothing was restarted, so: no obligation to reconcile, no cooldown to
        # burn, and the next tick must be free to try again.
        close_pending(state, unit)
        save_state(state)
        dm_repair_not_enqueued(unit, outcome.detail)
        log(f'{unit}: RESTART NEVER ENQUEUED — {outcome.detail[:160]!r}', 'WARN')
        return OUTCOME_REPAIR_NOT_ENQUEUED

    # A restart WAS issued: burn the cooldown either way.
    mark_restarted(state, unit, now=now)

    if outcome.verdict == VERDICT_IN_PROGRESS:
        save_state(state)  # the pending entry stays OPEN
        log(f'{unit}: restart in progress after {outcome.samples} sample(s) — '
            f'{outcome.detail}. Deferred to the pending-verification ledger; '
            f'no DM until it resolves.')
        return OUTCOME_RESTART_IN_PROGRESS

    # LANDED — now the only question is whether the NEW namespace is writable.
    new_facts = outcome.facts or unit_facts(unit)
    new_pid = new_facts.main_pid
    probe = probe_namespace_writable(new_pid) if new_pid else None
    if probe == _PROBE_OK:
        close_pending(state, unit)
        save_state(state)
        dm_rebound(unit, new_facts.active_state, outcome.detail)
        log(f'{unit}: REBOUND — restarted (old pid {prior_pid} → new pid '
            f'{new_pid}); new-namespace probe is writable')
        return OUTCOME_REBOUND
    if probe == _PROBE_EROFS:
        close_pending(state, unit)
        save_state(state)
        escalate_still_dangled(
            state, unit,
            'immediately after the auto-restart landed — the NEW namespace '
            'probed read-only, so the restart did not fix it', now=now)
        log(f'{unit}: STILL DANGLED after restart — proven failed repair', 'WARN')
        return OUTCOME_STILL_DANGLED

    # Re-probe could not run or could not conclude. Say NOTHING and keep the
    # obligation: silence plus a durable obligation is strictly more honest than
    # a false success, and the next tick's reconciliation will resolve it.
    save_state(state)
    log(f'{unit}: restart landed but the new-namespace re-probe was '
        f'inconclusive (new pid {new_pid}); no DM — left in the '
        f'pending-verification ledger for the next tick', 'WARN')
    return OUTCOME_AWAITING_VERIFY


def defer_repair(state: dict, unit: str, remaining: float, cost: int,
                 now: Optional[float] = None) -> str:
    """Record a repair that did not fit in this tick's budget.

    Deferring costs 2 minutes; shortening a live drain costs a killed Claude
    session, so the drain ceiling is NEVER reduced to make something fit."""
    now = now if now is not None else time.time()
    entry = _svc(state, unit)
    ticks = int(entry.get('deferred_ticks') or 0) + 1
    entry['deferred_ticks'] = ticks
    log(f'{unit}: repair DEFERRED (tick budget exhausted: {remaining:.0f}s left, '
        f'{cost}s needed) — consecutive_deferrals={ticks}. The drain ceiling is '
        f'never shortened to make a repair fit.', 'WARN')
    if ticks >= DEFER_ESCALATE_TICKS:
        last = entry.get('last_defer_alert_ts')
        fresh = (not isinstance(last, (int, float))
                 or (now - last) >= ESCALATION_COOLDOWN_SEC)
        if fresh:
            dm_repair_deferred(unit, ticks)
            entry['last_defer_alert_ts'] = now
    return OUTCOME_DEFERRED


def clear_deferral(state: dict, unit: str) -> None:
    entry = state.get('services', {}).get(unit)
    if isinstance(entry, dict) and entry.get('deferred_ticks'):
        entry['deferred_ticks'] = 0


def repair_cost_s(unit: str) -> int:
    return (AGENT_REPAIR_BUDGET_S
            if unit in restart_guard.AGENT_HOSTING_UNITS
            else PEER_REPAIR_BUDGET_S)


# -------------------- single-unit convenience (tests / manual runs) --------------------

def check_unit(unit: str, state: dict, now: Optional[float] = None) -> str:
    """Inspect (and if needed repair) ONE unit, unbudgeted. main() does not use
    this — it runs detection for the whole fleet first and then repairs in a
    deliberate, budgeted order — but the per-unit contract is identical.

    Outcomes are the OUTCOMES tuple; every one of them is documented in
    runbooks/claude-json-bind-drift.md (pinned by a test)."""
    if pending_entry(state, unit) is not None:
        # Reconciliation owns this unit until its obligation closes.
        return OUTCOME_AWAITING_VERIFY
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
    tick_started = time.monotonic()
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

    # 1. Resolve outstanding restart obligations FIRST. These units are then
    #    skipped by the ordinary path — one repair, at most one DM.
    known = set(units)
    reconciled = reconcile_pending(state, known)
    for outcome in reconciled.values():
        bump(outcome)

    # 2. Classify + detect across the whole fleet (cheap and bounded).
    repairs: list[tuple[str, UnitFacts]] = []
    monitored: set[str] = set()
    reasons: dict[str, str] = {}
    for unit in units:
        facts = unit_facts(unit)
        klass, reason = classify_unit(facts)
        reasons[unit] = reason
        if klass == CLASS_MONITOR and carveout_present(facts.read_write_paths):
            monitored.add(unit)
        if unit in reconciled:
            continue  # reconciliation already spoke for this unit
        outcome = inspect_unit(unit, facts, klass, state)
        if outcome == REPAIR_REQUESTED:
            repairs.append((unit, facts))
            continue
        clear_deferral(state, unit)
        bump(outcome)

    coverage_delta(state, monitored, reasons)

    # 3. Repair, cheap pass-through peers FIRST and the drain-paying
    #    agent-hosting unit LAST, each only if the remaining budget covers it in
    #    full. Enforced here — before entering guarded_restart — because
    #    enforcing it inside would be enforcing it in the process systemd is
    #    about to SIGTERM.
    repairs.sort(key=lambda t: (t[0] in restart_guard.AGENT_HOSTING_UNITS, t[0]))
    for unit, facts in repairs:
        cost = repair_cost_s(unit)
        remaining = TICK_BUDGET_S - (time.monotonic() - tick_started)
        if remaining < cost:
            bump(defer_repair(state, unit, remaining, cost))
            continue
        bump(repair_unit(unit, facts, state))
        clear_deferral(state, unit)

    save_state(state)
    # `deferred=` is always printed, zero included: a tick that silently stopped
    # repairing must be visible in the same read that /cycle already does.
    line = ' '.join(f'{k}={v}' for k, v in counts.items()
                    if v and k != OUTCOME_DEFERRED)
    log(f'tick: {line} deferred={counts.get(OUTCOME_DEFERRED, 0)}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
