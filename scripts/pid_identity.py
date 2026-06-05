#!/usr/bin/env python3
"""pid_identity.py — defeat PID-reuse races before an irreversible kill.

A PID observed as a stale lease holder or an idle zombie can exit and be
recycled by the kernel to an *unrelated* process before we get around to
signalling it. Comparing the process **start time** (``/proc/<pid>/stat`` field
22 — monotonic ticks since boot, distinct for a recycled PID within the same
boot) — and optionally a cmdline substring — distinguishes the original process
from a same-PID impostor, so a reclaim/heal never SIGKILLs an innocent bystander.

Linux ``/proc`` only; callers are droplet daemons. Every read fails closed
(returns ``None``/``False``) so an unreadable ``/proc`` makes a kill *less*
likely, never more.
"""
from __future__ import annotations

from typing import Optional


def proc_starttime(pid) -> Optional[int]:
    """Process start time in clock ticks since boot (``/proc/<pid>/stat`` field
    22), or ``None`` if the PID is gone / unreadable. Stable for the life of a
    process and different for a recycled PID, so it is a reliable same-boot
    identity token."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    # field 2 (comm) is parenthesized and may itself contain spaces or ')';
    # split after the LAST ')' so the trailing fields are positional.
    rparen = data.rfind(b")")
    if rparen == -1:
        return None
    rest = data[rparen + 1:].split()
    # rest[0] is field 3 (state); starttime is field 22 -> rest[22 - 3] = rest[19].
    if len(rest) < 20:
        return None
    try:
        return int(rest[19])
    except ValueError:
        return None


def proc_cmdline(pid) -> Optional[str]:
    """The process command line (NUL-separated argv joined by spaces), or
    ``None`` if the PID is gone / unreadable."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return None


def still_same_process(pid, expected_starttime, *, require_cmdline_substr=None) -> bool:
    """True iff `pid` is alive and is (still) the process we meant to signal.

    Fails closed — returns False, i.e. "do NOT kill" — when the PID is gone or
    unreadable, when its start time differs from `expected_starttime` (a recycled
    PID), or when `require_cmdline_substr` is given and no longer appears in its
    cmdline. `expected_starttime` of ``None`` skips the start-time comparison
    (e.g. a legacy lease that predates start-time recording), so the check
    degrades to liveness + cmdline without ever becoming less safe.
    """
    st = proc_starttime(pid)
    if st is None:
        return False  # gone / unreadable -> unsafe to kill
    if expected_starttime is not None and st != expected_starttime:
        return False  # PID reused by a different process
    if require_cmdline_substr is not None:
        cmd = proc_cmdline(pid)
        if cmd is None or require_cmdline_substr not in cmd:
            return False  # no longer the command we targeted
    return True
