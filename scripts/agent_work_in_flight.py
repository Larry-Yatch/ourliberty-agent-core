#!/usr/bin/env python3
"""agent_work_in_flight.py — shared "is a Claude agent session live right now?"
predicate.

WHY THIS EXISTS (PR #981 / #989 — the #971 incident)
----------------------------------------------------
``heal_stale_daemon_code`` restarted ``ourliberty-inbox-watcher.service`` five
minutes into a live Mirror review of PR #971. The review died on SIGTERM
(children exit 143) and its claim stranded under ``.claimed/1/``. The restarter
had no way to ask "is paid work running in there?" — so it swung blind.

Four other restarters share that blindness (``heal_pipeline_stall``,
``medic_actions``, ``heal_systemd_install_drift``,
``heal_claude_json_bind_drift``); they simply had not fired recently. A guard
inlined into one healer would have fixed a fifth of the problem, so the
predicate lives here instead.

TWO SIGNALS, BOTH REQUIRED
--------------------------
Lifted from ``watchdog.py``, which solved this for its own stall suppression and
whose reasoning is battle-tested:

  1. **live in-flight marker** — ``state/in-flight/<stem>.json`` with a live pid.
  2. **held+live inbox dispatch lease** — the watcher's per-slot lease, held for
     the whole duration of the Claude session it spawns.

Neither alone is sufficient:

  * The MARKER is clobberable. A Mirror review reuses the Forge build's
    ``task_id`` as its ``task_stem`` (``outbox_notifier._dispatch_mirror_review``),
    so both write the SAME ``state/in-flight/<stem>.json``, and the build's
    ``agent_runner._unregister_in_flight`` (keyed on stem only) can delete the
    review's marker while the review is still live
    (watchdog-mirror-active-stale-suppression-001).
  * The LEASE is absent in ``dispatch_lease`` 'off' mode (no lease files are
    written at all), where the marker is the only signal left.

SLOT-AWARE (fixes a latent watchdog gap — see MIGRATION below)
--------------------------------------------------------------
``inbox_watcher._slot_identity`` names slot 0 ``inbox:<agent>`` and higher slots
``inbox:<agent>:<n>``. watchdog's original loop opened only the slot-0 spelling,
so a review running in Mirror slot 1 was INVISIBLE to the lease signal — and
Mirror runs two slots. #971's killed review was in slot 1 precisely.
This module globs every slot.

Conservative in every direction: unreadable / half-written / unparseable state is
skipped, never counted as live. Pure ``/proc`` + filesystem reads; no subprocess,
no mutation, safe to call from any healer on any tick.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
import sys  # noqa: E402
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dispatch_lease  # noqa: E402  (TTL constant + mode)

# inbox_watcher runs one thread per agent, each holding its per-slot
# `inbox:<agent>[:<n>]` dispatch lease for the whole duration of the Claude
# session it spawns. Mirror is the case this exists for, but any agent's held
# lease is an equally valid "watcher mid-session" proxy.
INBOX_LEASE_AGENTS = ("beacon", "forge", "mirror", "pulse")


def _agents_root() -> Path:
    """Resolved at CALL time so the OURLIBERTY_AGENTS_ROOT test-sandbox redirect
    is honored (a module-level constant would freeze the real ~/agents at import
    and leak live state into tests)."""
    root = os.environ.get("OURLIBERTY_AGENTS_ROOT")
    return Path(root) if root else Path.home() / "agents"


def cordon_file(unit: str, agents_root: Path | None = None) -> Path:
    """CANONICAL path of the restart cordon for ``unit`` — the single definition
    both the producer (a restarter) and the consumer (inbox_watcher) must use.

    This lives here because the two sides previously derived it independently
    and DISAGREED: ``heal_stale_daemon_code`` defaults ``AGENTS_ROOT`` to the
    hardcoded ``/home/larry/agents`` while ``inbox_watcher`` derives it from
    ``Path.home()``. Identical on the droplet under the normal user — but this
    system SWAPS HOME for tier rotation, and under a swap the restarter would
    write a cordon the watcher never reads: the cordon silently does nothing and
    the restart kills live work again. It fails toward today's behavior rather
    than a stall, which is the safe direction, but it fails SILENTLY, and a
    safety mechanism that quietly stops working is worse than one that never
    shipped.

    One definition, one resolution order (explicit arg → env → ~/agents)."""
    base = agents_root or _agents_root()
    return base / "state" / "restart-cordon" / f"{unit}.json"


def _pid_alive(pid) -> bool:
    """True if a process with this pid currently exists."""
    if not isinstance(pid, int) or pid <= 0:
        # <=0 targets a process GROUP / every process — never a live holder.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another uid
    except OSError:
        return False
    return True


def any_live_in_flight_marker(agents_root: Path | None = None) -> bool:
    """True iff any ``state/in-flight/*.json`` marker has a live pid.

    ``agents_root`` lets a caller that keeps its OWN root constant (watchdog
    freezes ``AGENTS_ROOT`` at import, and its tests patch that attribute) stay
    authoritative; omitted, we resolve the env redirect at call time."""
    in_flight = (agents_root or _agents_root()) / "state" / "in-flight"
    try:
        markers = list(in_flight.glob("*.json"))
    except OSError:
        return False
    for marker in markers:
        try:
            with open(marker) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and _pid_alive(data.get("pid")):
            return True
    return False


def any_live_inbox_lease(agents_root: Path | None = None) -> bool:
    """True iff any inbox dispatch lease is held, within TTL, by a live holder.

    Globs EVERY slot (``inbox:<agent>.lease`` and ``inbox:<agent>:<n>.lease``),
    not just slot 0 — see the SLOT-AWARE note in the module docstring.

    Reads the lease files directly rather than via ``dispatch_lease.is_held``,
    whose LEASES_DIR is fixed at import: going through it would bypass the
    OURLIBERTY_AGENTS_ROOT redirect and read LIVE state during tests."""
    leases_dir = (agents_root or _agents_root()) / "state" / "dispatch-leases"
    for agent in INBOX_LEASE_AGENTS:
        try:
            candidates = list(leases_dir.glob(f"inbox:{agent}.lease")) + \
                list(leases_dir.glob(f"inbox:{agent}:*.lease"))
        except OSError:
            continue
        for lease_file in candidates:
            try:
                with open(lease_file) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            try:
                age = time.time() - float(data.get("timestamp_renewed", 0))
            except (TypeError, ValueError):
                continue
            if age >= dispatch_lease.TTL_SECONDS:
                continue  # stale — holder died without releasing
            if _pid_alive(data.get("holder_pid")):
                return True
    return False


def agent_work_in_flight(agents_root: Path | None = None) -> bool:
    """True iff any Claude agent session is live right now.

    The question a restarter must ask before it SIGTERMs a unit hosting agent
    loops. Marker OR lease — see the module docstring for why both.

    Errs toward True only on real evidence: every read failure is skipped, so an
    unreadable state tree reports "no work" and degrades to today's
    restart-immediately behavior rather than wedging restarts forever."""
    return (any_live_in_flight_marker(agents_root)
            or any_live_inbox_lease(agents_root))
