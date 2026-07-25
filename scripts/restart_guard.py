#!/usr/bin/env python3
"""restart_guard.py — cordon-and-drain, factored out so EVERY restarter uses it.

WHY THIS EXISTS (PR 3 of the restart-inflight-guard arc)
--------------------------------------------------------
``heal_stale_daemon_code`` used to restart ``ourliberty-inbox-watcher.service``
mid-review, SIGTERMing paid work (children exit 143) and stranding the claim.
#994/#996/#999 fixed that healer with cordon-and-drain, and on 2026-07-22 it
proved itself live: ``RESTART_DRAINED unit=ourliberty-inbox-watcher.service
waited=135s`` — a busy watcher held for 135s, then restarted cleanly.

But the machinery lived INSIDE that one healer, while other restarters kept the
original blindness. This module is that machinery, moved out and shared. The
predicate ``agent_work_in_flight`` was factored out for exactly this reason
("a guard inlined into one healer would have fixed a fifth of the problem"); the
guard around it belongs at the same altitude.

THE FOUR PEERS, AS ACTUALLY MEASURED (not as assumed)
-----------------------------------------------------
The parked scope named four latent restarters. Re-reading the code first found
only THREE that restart anything:

  * ``medic_actions``               — ``restart-daemon`` / ``retrigger-inbox``,
                                      allowlisted to inbox-watcher + outbox-notifier.
  * ``heal_systemd_install_drift``  — re-installs a drifted unit, then restarts
                                      it (timers always; long-running services).
  * ``heal_claude_json_bind_drift`` — restarts a unit whose ~/.claude.json
                                      bind-mount dangled (globs ourliberty-*.service).
  * ``heal_pipeline_stall``         — NOT a restarter. Its only subprocesses are
                                      ``gh`` and ``journalctl``; it re-dispatches
                                      work, it never touches systemd. Nothing to
                                      guard. Left alone deliberately.

ONE RESTARTER AT A TIME (the race PR 3 introduces)
--------------------------------------------------
With a single producer, cordon-then-clear was safe. With four, it is not: A
cordons, B overwrites the cordon, A finishes and CLEARS it, and B goes on
draining against a cordon that no longer exists — the watcher resumes taking
work and B restarts into a live session. That is precisely the bug this arc
exists to kill, reintroduced by sharing the file.

Refcounting the holders would work but needs a file-format change on the
consumer (``inbox_watcher._restart_cordon_active``), which is the proven, live
side. Mutual exclusion is simpler and strictly better here: two concurrent
restarts of the SAME unit are redundant by definition — whoever loses the race
gets what it wanted from the winner's restart. So a restarter takes a
non-blocking per-unit lock and, if a peer holds it, SKIPS this tick and reports
``skipped-peer-active``. Callers must not treat that as a failure (no DM): every
caller here is timer-driven and will re-evaluate on its next tick.

CEILINGS ARE PER-CALLER, AND THAT IS LOAD-BEARING
--------------------------------------------------
A drain waits up to ``ceiling_sec`` for work to quiesce. #1000 taught the cost of
getting this wrong: the healer's ``TimeoutStartSec`` was 60s against a 3600s
drain, so systemd SIGTERMed it mid-drain and — because Python's ``finally`` does
not run on SIGTERM — the restart silently never happened. It failed safe and did
nothing, which is the worst kind of working.

Every caller's ceiling must therefore fit inside ITS OWN outer time budget:

  caller                        outer bound                       ceiling
  ----------------------------  --------------------------------  -------
  heal_stale_daemon_code        TimeoutStartSec=3900 (#1000)       3600 force
  heal_systemd_install_drift    TimeoutStartSec 60 -> 3900         3600 force
  heal_claude_json_bind_drift   TimeoutStartSec 120 -> 1200        900  force
  medic_actions                 CLAUDE_TIMEOUT=10m in run_medic.sh 120  refuse

Medic is the one that must NOT copy the 3600s ceiling. It runs inside a
``timeout 10m claude`` session (``run_medic.sh``), so a 3600s drain would be
killed by ``timeout(1)`` long before it finished — the #1000 failure mode
exactly. It gets a short ceiling and ``on_ceiling='refuse'`` instead of
``'force'``: Medic re-runs every 3 minutes and its alert persists, so declining
to act is free, whereas forcing a restart would kill the very review the guard
exists to protect. Medic also restarts units that are already down or flapping,
where in-flight work is normally zero and the drain returns immediately.

Medic is safe from self-deadlock only because its own Claude session is INVISIBLE
to the predicate: ``run_medic.sh`` execs ``claude`` directly rather than through
``agent_runner``, so it writes no ``state/in-flight`` marker and holds no inbox
dispatch lease. If Medic is ever rerouted through ``agent_runner``, its drain
would be waiting on itself. The failure would be bounded and visible rather than
destructive — ``refuse`` at a 120s ceiling means "Medic stops restarting things"
and says so in the log every time — but it would need this ceiling revisited.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
import sys  # noqa: E402
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import agent_work_in_flight  # noqa: E402  (shared predicate + canonical path)
import file_lock  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402

# Units that HOST Claude agent loops, i.e. where a restart SIGTERMs paid work
# mid-session. Only these get cordon-and-drain. The *-bot units (telegram
# front-ends), dashboard-api and every .timer host no sessions and must keep
# restarting immediately — a needless drain wait on them would just delay a fix.
# Must match inbox_watcher.CORDON_UNIT, which is what the watcher looks for.
AGENT_HOSTING_UNITS = frozenset({'ourliberty-inbox-watcher.service'})

# Short by design. A cordon is dangerous exactly in proportion to how long a
# DEAD holder's copy survives: while it stands, the watcher accepts no work, so
# an abandoned cordon is a silent full-queue stall. We keep the TTL short and
# RENEW while draining instead of buying headroom with a long TTL — the same
# trade dispatch_lease makes (180s TTL / 60s heartbeat).
CORDON_TTL_SEC = int(os.environ.get('OL_CORDON_TTL_SEC', '600'))
CORDON_RENEW_EVERY_SEC = max(1, CORDON_TTL_SEC // 3)
DRAIN_POLL_SEC = int(os.environ.get('OL_DRAIN_POLL_SEC', '15'))

# Default backstop for callers that do not pass one. Above
# REVIEW_SESSION_CEILING_SECONDS (2100s) so a single review can never exhaust
# it; only a continuous queue can.
RESTART_DEFER_CEILING_SEC = int(
    os.environ.get('OL_RESTART_DEFER_CEILING_SEC', '3600')
)

# How long to wait for the per-unit restart lock before concluding a peer owns
# it. Deliberately ~0: we want an immediate skip, not a queue of restarters.
LOCK_WAIT_SEC = float(os.environ.get('OL_RESTART_LOCK_WAIT_SEC', '0.5'))

# ---- outcomes -----------------------------------------------------------
OUTCOME_NOT_AGENT_HOSTING = 'not-agent-hosting'
OUTCOME_DRAINED = 'drained'
OUTCOME_FORCED_OVER_CEILING = 'forced-over-ceiling'
OUTCOME_REFUSED_WORK_IN_FLIGHT = 'refused-work-in-flight'
OUTCOME_SKIPPED_PEER_ACTIVE = 'skipped-peer-active'
OUTCOME_CORDON_UNAVAILABLE = 'cordon-unavailable'


class GuardedRestart(NamedTuple):
    """What the guard did.

    ``performed`` is the only field a caller must branch on: False means
    ``restart_fn`` was never called and the unit was NOT restarted. ``result``
    is whatever ``restart_fn`` returned (its own rc contract, untouched), or
    None when it never ran.
    """
    performed: bool
    outcome: str
    waited: float
    result: object = None


def _noop_log(msg: str, level: str = 'INFO') -> None:
    pass


def cordon_file(unit: str) -> Path:
    """Canonical helper — see agent_work_in_flight.cordon_file for why this must
    NOT be derived locally (producer and consumer disagreed under a HOME swap)."""
    return agent_work_in_flight.cordon_file(unit)


def _boot_id() -> str:
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except OSError:
        return ''


def write_cordon(unit: str, reason: str, log: Callable = _noop_log) -> bool:
    """Write/refresh the cordon for <unit>. True iff it landed.

    Called once to raise the cordon and again on every drain poll to push
    ``expires_at`` forward. Renewal is what makes a SHORT ttl safe: a review may
    run to REVIEW_SESSION_CEILING_SECONDS (2100s), 3.5x the 600s TTL, so a
    non-renewed cordon would EXPIRE MID-DRAIN — the watcher would resume taking
    work and we would restart into a live session, reintroducing the exact bug
    this exists to prevent."""
    path = cordon_file(unit)
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
    cordon we forget to clear stalls dispatch until its TTL.

    Safe under the per-unit lock: only the holder ever clears, so this can never
    delete a peer's live cordon out from under its drain."""
    try:
        cordon_file(unit).unlink()
    except OSError:
        pass


class _CordonSigtermGuard:
    """Clear the cordon if we are SIGTERMed mid-drain.

    Belt-and-braces for the systemd kill path. A `finally` does NOT run when the
    default SIGTERM disposition terminates the process, so a restarter killed
    while draining leaves its cordon file behind. Holder-liveness already makes
    that cordon inert within one watcher poll (the pid is dead), so this is not
    what prevents a stall — it removes the corpse promptly instead of leaving a
    confusing file on disk for up to CORDON_TTL_SEC, and it re-raises via the
    previous handler so systemd still sees a normal termination.

    Restores the prior handler on exit; a non-main-thread install is a no-op
    (signal.signal raises there) rather than an error."""

    def __init__(self, unit: str, log: Callable = _noop_log):
        self.unit = unit
        self._log = log
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
        self._log(
            f'SIGTERM during drain of {self.unit} — cordon cleared; the restart '
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


def drain_agent_work(
    unit: str,
    reason: str,
    ceiling_sec: Optional[int] = None,
    log: Callable = _noop_log,
) -> tuple[bool, float]:
    """Hold the cordon until no agent session is live. Return (drained, waited).

    ``drained`` False means we hit ``ceiling_sec`` with work still running — the
    caller decides whether to restart anyway or stand down.

    The cordon is raised BEFORE the first liveness probe, which is what makes
    this terminate: with new dispatch blocked, in-flight can only fall to zero.
    Probing first and cordoning after would race the watcher's 5s poll forever
    on a busy queue."""
    ceiling = RESTART_DEFER_CEILING_SEC if ceiling_sec is None else ceiling_sec
    started = time.time()
    last_renew = started
    while True:
        if not agent_work_in_flight.agent_work_in_flight():
            return True, time.time() - started
        waited = time.time() - started
        if waited >= ceiling:
            return False, waited
        time.sleep(DRAIN_POLL_SEC)
        if time.time() - last_renew >= CORDON_RENEW_EVERY_SEC:
            write_cordon(unit, reason, log)  # push expires_at forward
            last_renew = time.time()


def guarded_restart(
    unit: str,
    reason: str,
    restart_fn: Callable[[], object],
    *,
    ceiling_sec: Optional[int] = None,
    on_ceiling: str = 'force',
    log: Callable = _noop_log,
) -> GuardedRestart:
    """Restart ``unit`` without SIGTERMing a live Claude session.

    For non-agent-hosting units (every ``.timer``, the ``*-bot`` front-ends,
    dashboard-api) this is a pass-through: ``restart_fn`` runs immediately,
    exactly as before. Only ``AGENT_HOSTING_UNITS`` pay the drain.

    ``on_ceiling`` decides the pathological case — work that never stops:
      * ``'force'``  restart anyway and log loudly. Staleness must eventually
                     win; this is the one path that can still kill a session.
      * ``'refuse'`` stand down without restarting. Correct for a caller that
                     runs again soon and whose alert persists (Medic), where
                     killing a review is strictly worse than waiting a tick.
    """
    if unit not in AGENT_HOSTING_UNITS:
        return GuardedRestart(True, OUTCOME_NOT_AGENT_HOSTING, 0.0, restart_fn())

    # One restarter per unit. A peer mid-drain owns the cordon; if we proceeded
    # we would clear ITS cordon on our way out and leave it draining unprotected.
    lock_path = file_lock.sidecar_lock_path(cordon_file(unit))
    try:
        with file_lock.exclusive_lock(lock_path, timeout=LOCK_WAIT_SEC):
            return _guarded_restart_locked(
                unit, reason, restart_fn, ceiling_sec, on_ceiling, log,
            )
    except file_lock.LockTimeout:
        log(f'RESTART_SKIPPED_PEER_ACTIVE unit={unit} reason={reason} — another '
            f'restarter holds the cordon lock (its restart serves this need too); '
            f'standing down this tick', 'INFO')
        return GuardedRestart(False, OUTCOME_SKIPPED_PEER_ACTIVE, 0.0, None)


def _guarded_restart_locked(
    unit: str,
    reason: str,
    restart_fn: Callable[[], object],
    ceiling_sec: Optional[int],
    on_ceiling: str,
    log: Callable,
) -> GuardedRestart:
    """Cordon → drain → restart → uncordon, with the per-unit lock held."""
    if not write_cordon(unit, reason, log):
        # Could not raise the cordon (disk full, perms). Restarting anyway is
        # exactly the pre-#994 behavior — no worse — and refusing would let a
        # stale daemon run indefinitely on an unrelated IO fault. Never silent.
        log(f'cordon unavailable for {unit}; restarting WITHOUT drain '
            f'(pre-#994 behavior — a live session may be killed)', 'WARN')
        return GuardedRestart(True, OUTCOME_CORDON_UNAVAILABLE, 0.0, restart_fn())

    try:
        with _CordonSigtermGuard(unit, log):
            drained, waited = drain_agent_work(unit, reason, ceiling_sec, log)
        if drained:
            log(f'RESTART_DRAINED unit={unit} waited={waited:.0f}s '
                f'reason={reason} — restarting into a quiet window', 'INFO')
            return GuardedRestart(True, OUTCOME_DRAINED, waited, restart_fn())

        ceiling = RESTART_DEFER_CEILING_SEC if ceiling_sec is None else ceiling_sec
        if on_ceiling == 'refuse':
            # Stand down. The caller re-runs soon and its trigger persists, so
            # the cost is a delay; forcing would cost a killed review.
            log(f'RESTART_REFUSED_WORK_IN_FLIGHT unit={unit} '
                f'drained_for={waited:.0f}s ceiling={ceiling}s reason={reason} — '
                f'agent work still live; NOT restarting, will retry next tick',
                'INFO')
            return GuardedRestart(False, OUTCOME_REFUSED_WORK_IN_FLIGHT, waited, None)

        # Backstop: work never stopped. Restart anyway (staleness must
        # eventually win) but make it loud — this is the one path that can
        # still kill a live session.
        log(f'RESTART_FORCED_OVER_CEILING unit={unit} '
            f'drained_for={waited:.0f}s ceiling={ceiling}s '
            f'reason={reason} — restarting with work STILL in flight', 'WARN')
        return GuardedRestart(True, OUTCOME_FORCED_OVER_CEILING, waited, restart_fn())
    finally:
        # Unconditional: an un-cleared cordon stalls dispatch until its TTL.
        clear_cordon(unit)
