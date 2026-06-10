"""Test package init — process-level log isolation for unittest invocation.

When tests are run via `python3 -m unittest scripts.tests.test_X` (the
repo's default, since pytest is not always installed), this __init__.py
runs ONCE at package import time and sets OURLIBERTY_LOG_DIR to a fresh
temp directory. Any production module imported afterwards
(beacon_telegram_bot, agent_runner, ...) writes its logs into that temp
dir instead of the live ~/agents/logs/.

This is the unittest-native counterpart to conftest.py's autouse pytest
fixture: both target the same env var. Whichever one runs first wins; the
other becomes a no-op (because both check for an existing value). pytest
flows get per-test isolation via the fixture; unittest flows get
process-level isolation via this __init__.

PARITY IS ENFORCED. scripts/tests/test_conftest_init_parity.py fails if a
conftest.py autouse fixture has no mirror here — so a new pytest-only
protection cannot silently skip the unittest regression gate. When you add a
protection to conftest.py, add its env-var mirror below and register it in
that test's MIRRORED_AUTOUSE_FIXTURES.

Background — 2026-05-27 11:56–11:57 MDT incident: tests calling production
functions (e.g., beacon_telegram_bot.call_beacon with a mocked subprocess)
reached the bot's internal log() helper, which wrote test sentinel strings
into the live beacon_telegram_bot.log. Closing both pytest + unittest
invocation paths prevents the regression.

The sandbox temp dirs are left for the OS's normal /tmp cleanup; we add no
atexit hook to REMOVE them (test runs are short-lived, the worst case is a few
KB of test logs until /tmp is cleared, and keeping them aids debugging a
crashed test). The Gap B runtime tripwire below DOES register an atexit hook —
but only to SCAN the real ~/agents tree at process end; it never deletes the
sandbox.
"""
import atexit
import os
import sys
import tempfile
import time
import uuid

if not os.environ.get('OURLIBERTY_LOG_DIR'):
    os.environ['OURLIBERTY_LOG_DIR'] = tempfile.mkdtemp(
        prefix='ourliberty-test-logs-',
    )

# Import-time sandbox for the OURLIBERTY_*_ROOT family (Gap A) — the unittest
# mirror of conftest.py's module-top setdefaults. Many production modules bind
#   AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
# AT IMPORT; without an import-time default a test that imports such a module
# and then triggers an inbox/blackboard/state write hits the REAL ~/agents
# tree. setdefault so an outer harness or an intentional delenv still wins.
if not os.environ.get('OURLIBERTY_AGENTS_ROOT'):
    _sandbox_agents_root = tempfile.mkdtemp(prefix='ourliberty-test-agents-root-')
    os.makedirs(os.path.join(_sandbox_agents_root, 'logs'), exist_ok=True)
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _sandbox_agents_root
    os.environ.setdefault(
        'OURLIBERTY_WORKTREES_ROOT',
        os.path.join(_sandbox_agents_root, 'worktrees'),
    )

# Test-isolation guard (2026-06-02 live-DB leak): block the chain-event
# emitter from ever building a live Supabase client during a test run.
# The Forge/Mirror bot services inject live SUPABASE_* creds, so without
# this any test that transitively calls chain_event_emit.emit_event()
# upserts fixture rows (real-001, ...) into the production chain_events
# table. Runner-agnostic counterpart to conftest.py's pytest fixture,
# exactly like OURLIBERTY_LOG_DIR above — set at unittest/pytest package
# import. chain_event_emit._get_client() returns None when this is set.
os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'

# Runtime production-write tripwire (Gap B) — the unittest mirror of
# conftest.py's _production_write_runtime_tripwire session fixture. The fixture
# mints a run sentinel, stamps it through the production log() helpers, and at
# session-end TEARDOWN scans the REAL ~/agents tree for the token; any hit is a
# write that escaped the Gap A sandbox. Under the regression gate
# (python3 -m unittest) that fixture never runs, so we replicate its session
# lifecycle here: arm at package import, scan at process exit via atexit.
#
# State is exposed as module attributes for test_conftest_init_parity.py to
# assert the unittest gate actually arms the scan (not just sets the env var).
_RUNTIME_TRIPWIRE_ARMED = False
_RUNTIME_TRIPWIRE_SENTINEL = None


def _arm_runtime_tripwire():
    """Mint the sentinel, instrument the production log() helpers, and register
    an atexit scan of the real ~/agents tree. Mirrors the conftest fixture's
    setup/teardown across the package-import / process-exit boundary.

    Fail-open by design: if the tripwire infrastructure can't be imported the
    gate still runs (degraded), it just loses this backstop — better than a
    bootstrap that hard-fails every test invocation."""
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


# pytest owns the scan via conftest's session-scoped fixture; arming here too
# would double-instrument the helpers and double-fire the exit. Detect the
# pytest path (pytest is imported well before this package is collected) and
# only arm the atexit scan under the bare unittest gate. Under pytest, still
# set the env var so the OURLIBERTY_TEST_RUN_SENTINEL mirror stays present for
# parity (the fixture overwrites it with its own minted value).
if 'pytest' in sys.modules:
    os.environ.setdefault(
        'OURLIBERTY_TEST_RUN_SENTINEL', 'OL-TEST-RUN-SENTINEL-' + uuid.uuid4().hex,
    )
else:
    _arm_runtime_tripwire()
