"""test_isolation_guard.py — call-time choke guards that fail LOUD when a
production side-effect is reached from a test process.

WHY THIS FILE EXISTS (test-jail Layer B)
----------------------------------------
PR-1's ``scripts/tests/_bootstrap.py`` arms a per-module test sandbox under
EVERY loader and mints the run sentinel ``OURLIBERTY_TEST_RUN_SENTINEL`` before
any production module loads. But that bootstrap is *env-redirection only* — it
pins OURLIBERTY_LOG_DIR / OURLIBERTY_AGENTS_ROOT / OURLIBERTY_WORKTREES_ROOT and
sets OURLIBERTY_DISABLE_LIVE_EMIT=1; it does NOT swap ``$HOME`` and does NOT
strip credentials. So any module that freezes a path from ``Path.home()`` at
IMPORT (notably ``larry_alerts`` — its AGENTS_ROOT ignores OURLIBERTY_AGENTS_ROOT
— plus ``kill_switch``, ``concurrency_guard``) STILL resolves to the REAL
``~/agents`` tree during tests. Env redirection structurally cannot reach that
class; a CALL-TIME guard keyed off the sentinel can.

THE GUARD
---------
``refuse_under_test(channel)`` is the FIRST statement of each guarded production
side-effect. It RAISES ``TestIsolationBreach`` iff the run sentinel is set — i.e.
only inside a test process. In production the sentinel is NEVER set (production
code never imports ``_bootstrap``/conftest), so the guard is a pure pass-through
and changes zero production behavior.

The breach IS the bug: a test that reaches a guarded sink un-mocked has escaped
its mocks and would have hit production (paged Larry, spent money, sent a DM,
killed a process, wrote the real repo). Failing loud is the entire point — we
RAISE, never divert / return-None / no-op.

THE ESCAPE HATCH
----------------
The FEW tests that must exercise a guarded function FOR REAL (e.g. the guard's
own meta-test, or ``concurrency_guard`` / ``larry_alerts`` own-behavior tests
that drive their sandboxed sink deliberately) wrap the real call in
``with test_isolation_guard.allow('<channel>'):`` — which temporarily clears the
sentinel so the guard passes through, then restores it. Default for every other
test stays DENY.
"""
import contextlib
import os
import subprocess
from pathlib import Path

_SENTINEL_VAR = 'OURLIBERTY_TEST_RUN_SENTINEL'


class TestIsolationBreach(RuntimeError):
    """Raised when a guarded production side-effect is reached from a test
    process (the run sentinel is set). Signals a test that escaped its mocks."""


def refuse_under_test(channel: str) -> None:
    """Raise if a guarded production side-effect is reached from a test process.

    Pure pass-through in production: ``OURLIBERTY_TEST_RUN_SENTINEL`` is set ONLY
    by the test bootstrap/conftest, never in prod."""
    if os.environ.get(_SENTINEL_VAR):
        raise TestIsolationBreach(
            f'{channel} reached from a test process ({_SENTINEL_VAR} set) '
            f'— a test escaped its mocks and would have hit production. Mock '
            f'this call in the test, or wrap a legitimate real call in '
            f'test_isolation_guard.allow().'
        )


def _real_agents_roots():
    """The REAL production agents tree(s) a test must never write: the
    HOME-derived default (``~/agents``) and the hardcoded ``/home/larry/agents``
    that several modules embed literally. Resolved so a symlinked/relative
    path can't slip a live target past the check."""
    roots = []
    try:
        roots.append((Path.home() / 'agents').resolve())
    except (OSError, RuntimeError):
        pass
    try:
        roots.append(Path('/home/larry/agents').resolve())
    except (OSError, RuntimeError):
        pass
    out = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def refuse_live_state_write(path, channel: str) -> None:
    """Destination-AWARE state-write guard (test-jail Layer B, state channel).

    Under a test process (run sentinel set), RAISE ``TestIsolationBreach`` iff
    ``path`` resolves UNDER the real agents tree — i.e. the sandbox root
    redirect failed OPEN and a test is about to write LIVE production state
    (the 2026-07-02 active-tier.json tier-bench outage class). Unlike
    ``refuse_under_test`` (destination-BLIND — for network/spawn/paging sinks
    that must be mocked), this ALLOWS a write to a correctly-redirected sandbox
    tmpdir, so the many tests that legitimately drive tier-state transitions
    against ``OURLIBERTY_AGENTS_ROOT=<tmpdir>`` keep passing with no ``allow()``
    wrapper. Pure pass-through in production: the sentinel is never set there.

    The guard runs BEFORE any mkdir/write in each caller, so a refused write
    never creates a directory or file in the live tree."""
    if not os.environ.get(_SENTINEL_VAR):
        return
    try:
        target = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return
    # Invariant: the test sandbox root (bootstrap/gate ``mkdtemp``) lives under
    # /tmp, NEVER beneath a real agents tree. If a future harness ever pins
    # TMPDIR under ~/agents, a legitimate sandbox write would be misclassified
    # as live and refused — treat that as a harness misconfiguration, not a
    # reason to relax this check.
    for root in _real_agents_roots():
        if target == root or root in target.parents:
            raise TestIsolationBreach(
                f'{channel}: a test process is about to write LIVE production '
                f'state at {target} (under real agents root {root}) — the '
                f'sandbox root redirect failed OPEN. This is the tier-bench '
                f'leak class. Ensure OURLIBERTY_AGENTS_ROOT points at a tmpdir '
                f'in this test, or wrap a deliberate real-tree write in '
                f'test_isolation_guard.allow({channel!r}).'
            )


@contextlib.contextmanager
def allow(channel: str):
    """Sanctioned escape hatch for the FEW tests that must exercise a guarded
    function FOR REAL (e.g. larry_alerts'/concurrency_guard's OWN unit tests).
    Temporarily clears the sentinel so ``refuse_under_test()`` is a pass-through,
    then restores it. Default for every other test stays deny."""
    saved = os.environ.pop(_SENTINEL_VAR, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[_SENTINEL_VAR] = saved


def gh_write(args, **kwargs):
    """Guarded ``gh``/``git`` destructive-write subprocess wrapper.

    Calls ``refuse_under_test('gh-write')`` then runs the subprocess. Route
    DESTRUCTIVE write verbs through this — ``gh pr merge``, remote branch
    deletion (``git push --delete`` / ``gh api -X DELETE .../git/refs/...``),
    label/status writes (``gh pr edit --add-label``, ``gh api -X POST|PATCH|PUT``).
    Read-only ``gh`` (``gh pr view`` / ``gh pr list`` / ``gh api`` GET) is EXEMPT
    and must NOT be routed here. ``args`` is the full argv list (including the
    leading ``gh`` or ``git``); ``**kwargs`` pass through to ``subprocess.run``."""
    refuse_under_test('gh-write')
    return subprocess.run(args, **kwargs)
