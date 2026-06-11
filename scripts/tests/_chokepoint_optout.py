"""_chokepoint_optout.py — module-scope opt-out from the Layer B
production-side choke guards (scripts/test_isolation_guard.py).

WHY
---
The Layer B guards key off the run sentinel (OURLIBERTY_TEST_RUN_SENTINEL,
armed for every test run by _bootstrap.py): any production chokepoint
(larry_alerts writes, inbox writes, gh-write, claude spawn, telegram send,
concurrency slots) called under that sentinel raises ``TestIsolationBreach``.
That is exactly the loud failure we want for an ACCIDENTAL chokepoint hit.

But a handful of pre-existing test modules legitimately drive a guarded
chokepoint against an ALREADY-ISOLATED target — they reroute
OURLIBERTY_AGENTS_ROOT into a tmpdir and/or mock the underlying subprocess, and
the #428 runtime tripwire still scans the REAL ~/agents tree for any genuine
leak. For those modules the guard is a false-positive on isolated state.

WHAT
----
``disengage_guards()`` pops the run sentinel for the module's duration so the
call-time guards become the same pure pass-through they are in production;
``reengage_guards(saved)`` restores it verbatim in tearDownModule.

This does NOT weaken leak detection: the #428 instrumentation stamps writes via
a captured closure (instrument_log_helpers binds ``_s=sentinel``), not a runtime
read of this env var, so its atexit scan of the real tree is unaffected. The
census gate (test_chokepoint_census.py) independently proves every PRODUCTION
sink still routes through a guard, and any NEW test that accidentally reaches a
chokepoint still fails loud unless it opts out here deliberately.
"""
import os

_SENTINEL = 'OURLIBERTY_TEST_RUN_SENTINEL'


def disengage_guards():
    """Pop the run sentinel; return its prior value (None if unset) to restore."""
    return os.environ.pop(_SENTINEL, None)


def reengage_guards(saved):
    """Restore the sentinel to its pre-opt-out value (None => leave unset)."""
    if saved is None:
        os.environ.pop(_SENTINEL, None)
    else:
        os.environ[_SENTINEL] = saved
