"""_bootstrap.py — the SINGLE SOURCE OF TRUTH for the test-run sandbox.

WHY THIS FILE EXISTS
--------------------
scripts/tests/__init__.py used to hold the sandbox setup (the OURLIBERTY_*
env redirects + the #428 atexit live-write tripwire). That only engaged when
the ``scripts.tests`` PACKAGE was imported — i.e. under
``python3 -m unittest scripts.tests.<module>`` (dotted) or pytest. But the
regression gate and every Forge/Mirror dev loop run

    python3 -m unittest discover -s scripts/tests

which imports each ``test_*.py`` as a TOP-LEVEL module and NEVER executes the
package ``__init__``. So under discover the sandbox never armed, and any test
that transitively reached a production log()/alert/state write hit the REAL
~/agents tree. That leak class survived PR #412, #428 and #436 and spammed
Larry with false ``transcript-not-persisted`` CRITICAL DMs (mock /tmp paths,
fixture quota events, even ``../../../../etc/pwned`` path-traversal fixture
lines in the live inbox_watcher.log).

The fix: move the canonical setup HERE and have EVERY ``test_*.py`` import this
module as its first import (loader-agnostic form). A top-level import engages
the sandbox before any other import can freeze a path constant — regardless of
which loader brought the test module in. ``__init__.py`` now delegates to this
module too, so the dotted/pytest path and the discover path share ONE source
of truth and cannot drift.

IDEMPOTENCY
-----------
``engage()`` may be called more than once per process — and, worse, from TWO
distinct module identities: under ``discover`` the test files import this as
the top-level ``_bootstrap``; under the dotted/pytest path ``__init__`` imports
it as ``scripts.tests._bootstrap``. A module-level flag would not be shared
across those two identities, so the guard is a PROCESS-GLOBAL env var. The
first engage wins; every later call (any identity) is a harmless no-op. This is
what keeps the #428 tripwire from double-instrumenting the log helpers or
registering two atexit scans that would double-fire ``os._exit``.
"""
import atexit
import os
import sys
import tempfile
import time
import uuid

# Process guards that survive the dual module identity described above (a
# module-level bool would not, because top-level `_bootstrap` and
# `scripts.tests._bootstrap` are two different module objects in sys.modules).
# They are keyed by PID, NOT a bare '1': env vars are inherited by child
# subprocesses, so a bare flag would make a spawned child's engage() no-op and
# never arm ITS OWN tripwire (the UnittestGateExitSelfCheck child does exactly
# this — it inherits the parent gate's env and must still arm). Keying by PID
# means "already engaged" holds only WITHIN the process that set it; a value
# inherited from a different PID reads as not-engaged, so the child re-engages
# and arms freshly.
_ENGAGE_GUARD = 'OURLIBERTY_TEST_BOOTSTRAP_ENGAGED'
_ARMED_GUARD = 'OURLIBERTY_TEST_BOOTSTRAP_TRIPWIRE_ARMED'

# Module attributes exposed for debugging / for __init__'s re-export. The
# authoritative state lives in the PID-keyed guards above; these mirror it for
# the identity that actually ran engage().
_RUNTIME_TRIPWIRE_ARMED = False
_RUNTIME_TRIPWIRE_SENTINEL = None


def _is_this_process(var: str) -> bool:
    return os.environ.get(var) == str(os.getpid())


def is_armed() -> bool:
    """True if THIS PROCESS armed the runtime production-write tripwire. Reads
    the PID-keyed guard so the answer is correct from either module identity
    (top-level ``_bootstrap`` or ``scripts.tests._bootstrap``) and is NOT fooled
    by a guard value inherited from a parent process."""
    return _is_this_process(_ARMED_GUARD)


def run_sentinel():
    """The run sentinel stamped through the production log() helpers (or None
    under pytest before the conftest fixture mints its own)."""
    return os.environ.get('OURLIBERTY_TEST_RUN_SENTINEL')


def _arm_runtime_tripwire():
    """Mint the sentinel, instrument the production log() helpers, and register
    an atexit scan of the real ~/agents tree. Mirrors conftest's session
    fixture setup/teardown across the package-import / process-exit boundary.

    Fail-open by design: if the tripwire infrastructure can't be imported the
    gate still runs (degraded) — better than a bootstrap that hard-fails every
    test invocation."""
    global _RUNTIME_TRIPWIRE_ARMED, _RUNTIME_TRIPWIRE_SENTINEL

    here = os.path.dirname(os.path.abspath(__file__))
    scripts_dir = os.path.dirname(here)
    # The tripwire module is imported by bare name (as conftest does); its
    # instrument_log_helpers in turn imports agent_runner by bare name. Put BOTH
    # the tests dir and scripts/ on the path or instrumentation silently no-ops
    # (import fails -> nothing stamped -> the scan can never see a leak).
    for p in (here, scripts_dir):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        import test_no_production_writes_runtime as runtime
    except Exception:
        return

    sentinel = runtime.mint_sentinel()
    os.environ['OURLIBERTY_TEST_RUN_SENTINEL'] = sentinel
    session_start = time.time()
    undo = runtime.instrument_log_helpers(sentinel)
    _RUNTIME_TRIPWIRE_SENTINEL = sentinel
    _RUNTIME_TRIPWIRE_ARMED = True
    os.environ[_ARMED_GUARD] = str(os.getpid())

    def _scan_at_exit(runtime=runtime, sentinel=sentinel,
                      session_start=session_start, undo=undo):
        try:
            _, message = runtime.run_session_end_tripwire(
                sentinel, session_start, undo, runner='unittest gate',
            )
        except Exception as exc:
            # A scan bug must not red-flag a clean gate (the GateSelfCheck tests
            # cover scan correctness); warn and let the real exit code stand.
            print(f'[production-write-tripwire] scan skipped ({exc!r})',
                  file=sys.stderr)
            return
        if not message:
            return
        print(message, file=sys.stderr)
        # Force a non-zero gate exit. os._exit only fires on a CONFIRMED leak
        # (rare alarm), so the green path is untouched; flush first so a piped
        # capture (Mirror's gate) keeps the runner's already-printed results.
        # NB: the regression gate (scripts/test_regression_check.py) keys off
        # this non-zero exit and surfaces it as a synthetic failure — without
        # that, the exit code alone is swallowed by its FAIL/ERROR-line parser.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    atexit.register(_scan_at_exit)


def engage() -> None:
    """Establish the test sandbox AT IMPORT. Idempotent across calls and across
    the dual module identity (guarded by a process-global env flag)."""
    if _is_this_process(_ENGAGE_GUARD):
        return
    os.environ[_ENGAGE_GUARD] = str(os.getpid())

    # Production log redirection (Gap: 2026-05-27 live-log leak). Many daemons
    # resolve OURLIBERTY_LOG_DIR at write time; pin it to a tmp dir so an
    # un-mocked log() call lands in /tmp, not ~/agents/logs/. setdefault-style
    # (only when unset) so an outer harness / the conftest fixture still wins.
    if not os.environ.get('OURLIBERTY_LOG_DIR'):
        os.environ['OURLIBERTY_LOG_DIR'] = tempfile.mkdtemp(
            prefix='ourliberty-test-logs-',
        )

    # Import-time sandbox for the OURLIBERTY_*_ROOT family (Gap A). Production
    # modules bind
    #   AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
    # AT IMPORT; without an import-time default a test that imports such a
    # module and then triggers an inbox/blackboard/state write hits the REAL
    # ~/agents tree. setdefault so an outer harness or an intentional delenv
    # still wins.
    if not os.environ.get('OURLIBERTY_AGENTS_ROOT'):
        _sandbox_agents_root = tempfile.mkdtemp(
            prefix='ourliberty-test-agents-root-')
        os.makedirs(os.path.join(_sandbox_agents_root, 'logs'), exist_ok=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = _sandbox_agents_root
        os.environ.setdefault(
            'OURLIBERTY_WORKTREES_ROOT',
            os.path.join(_sandbox_agents_root, 'worktrees'),
        )

    # Test-isolation guard (2026-06-02 live-DB leak): block the chain-event
    # emitter from ever building a live Supabase client during a test run.
    # chain_event_emit._get_client() returns None when this is set.
    os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'

    # Runtime production-write tripwire (#428). pytest owns the scan via
    # conftest's session-scoped fixture; arming here too would double-instrument
    # the helpers and double-fire the exit. Under pytest, still set the sentinel
    # env var so the OURLIBERTY_TEST_RUN_SENTINEL mirror stays present for parity
    # (the fixture overwrites it with its own minted value).
    if 'pytest' in sys.modules:
        os.environ.setdefault(
            'OURLIBERTY_TEST_RUN_SENTINEL',
            'OL-TEST-RUN-SENTINEL-' + uuid.uuid4().hex,
        )
    else:
        _arm_runtime_tripwire()


# Engage AT IMPORT — this is the whole point: importing this module (top-level
# under discover, or as a package member via __init__) arms the sandbox before
# any subsequent import in the test file can freeze a path constant.
engage()
