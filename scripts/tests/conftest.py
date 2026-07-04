"""Shared pytest fixtures for scripts/tests/*.

Autouse log-isolation fixture redirects production log writes — anything
that resolves its path via `resolve_log_dir()` in the production modules —
into a per-test tmp_path. Without this, tests that import beacon_telegram_bot
or agent_runner and trigger their log() helpers (often via mocked subprocess
flows that still reach the rate-limit / auth-401 detection branches) write
sentinel strings into the live ~/agents/logs/ files. The 2026-05-27 incident
left TIER_ONE_MARKER + '401 Unauthorized' tokens in
~/agents/logs/beacon_telegram_bot.log this way.

The fixture is autouse=True and function-scoped, which pytest applies
transparently to unittest.TestCase subclasses (the dominant shape in
scripts/tests/). Tests do NOT need to declare it as a parameter, and
existing setUp/tearDown logic is unaffected. Tests that want to verify
the production-default behavior must explicitly undo the env override
via `monkeypatch.delenv('OURLIBERTY_LOG_DIR', raising=False)` (see
scripts/tests/test_log_dir_resolution.py).

The env var name `OURLIBERTY_LOG_DIR` is the single coordination point
between production modules' `resolve_log_dir()` helpers and this fixture.
Keep them in sync.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

# --- Import-time sandbox for the OURLIBERTY_*_ROOT family (Gap A) ----------
# Many production modules bind their root AT IMPORT, e.g.
#   AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
# (chain_event_shipper, build_sequence_advancer, larry_alerts_retention,
# heal_* ...). The per-test fixtures below are function-scoped and so run
# AFTER collection imports those modules — too late to redirect a constant
# already frozen to the live ~/agents. A test that imports such a module at
# collection time and later triggers an inbox/blackboard/state write would
# hit the REAL tree (the filesystem analog of the 2026-06-02 chain_events
# leak). Establish the sandbox HERE, before any test module is collected.
#
# setdefault (not assignment): an outer harness that already pinned these
# wins, and a test that intentionally exercises production-default resolution
# can still delenv. The per-test OURLIBERTY_LOG_DIR override below still wins
# per test; this default only catches import-time path captures.
_SANDBOX_AGENTS_ROOT = tempfile.mkdtemp(prefix='ol-test-agents-root-')
os.makedirs(os.path.join(_SANDBOX_AGENTS_ROOT, 'logs'), exist_ok=True)
# FORCE-OVERRIDE (not setdefault) so an inherited LIVE root cannot defeat the
# sandbox — the 2026-07-02 tier-bench outage. The regression gate opts out via
# OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT (it pins its own coordinated tree).
if not os.environ.get('OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT'):
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _SANDBOX_AGENTS_ROOT
    os.environ['OURLIBERTY_WORKTREES_ROOT'] = os.path.join(
        _SANDBOX_AGENTS_ROOT, 'worktrees')
os.environ.setdefault(
    'OURLIBERTY_LOG_DIR', os.path.join(_SANDBOX_AGENTS_ROOT, 'logs'),
)

# The runtime tripwire's scan/stamp logic + self-checks live in the sibling
# test module; the session fixture below imports them. Ensure this directory
# is importable by plain module name regardless of pytest's import mode.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.fixture(autouse=True)
def _isolate_production_logs(tmp_path, monkeypatch):
    """Redirect any production log write to tmp_path for the duration of the
    test. Production default behavior is preserved when this fixture is not
    in scope (i.e., outside scripts/tests/)."""
    log_dir = tmp_path / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('OURLIBERTY_LOG_DIR', str(log_dir))
    yield log_dir


@pytest.fixture(autouse=True)
def _block_live_chain_event_emit(monkeypatch):
    """Make it impossible for a test to write the live chain_events table.

    The Forge/Mirror bot services inject live SUPABASE_* creds, so a pytest
    run inside a build worktree would otherwise let any test that transitively
    calls chain_event_emit.emit_event() upsert fixture rows into production
    (2026-06-02 leak: 200+ real-*/prod-* rows). Forcing _get_client() to None
    routes every unstubbed emit to the existing WARN+drop branch. Belt: the
    env var; suspenders: the direct attribute patch.
    """
    monkeypatch.setenv('OURLIBERTY_DISABLE_LIVE_EMIT', '1')
    try:
        import chain_event_emit as cee
        cee.reset_client_for_testing()
        monkeypatch.setattr(cee, '_get_client', lambda: None)
    except Exception:
        pass


@pytest.fixture(scope='session', autouse=True)
def _production_write_runtime_tripwire():
    """Runtime backstop (Gap B): assert the whole session wrote NOTHING to the
    real ~/agents tree.

    Every other isolation gate is static analysis and is blind to a leak via a
    novel mechanism (a frozen constant, a new daemon, a write bypassing
    resolve_log_dir()). This is the additive belt-and-suspenders runtime check.

    Content-sentinel attribution, not a size/mtime diff: this suite runs on the
    LIVE droplet where daemons mutate ~/agents/{logs,blackboard,state} every few
    seconds, so a size diff would false-positive on churn. We mint a unique run
    sentinel, stamp it through the production log() helpers so test-originated
    writes carry it, and at session end scan the REAL tree (resolved from the
    actual home, NOT through the sandbox env vars) for the token. Any hit = a
    write that escaped the redirect = a genuine leak; daemon churn never carries
    the token, so a clean run is green on the live host and in CI alike.

    The unittest mirror (scripts/tests/__init__.py) runs the SAME session-end
    scan via an atexit hook (both call runtime.run_session_end_tripwire); under
    the unittest gate the leak surfaces as os._exit(1), which the regression gate
    turns into a synthetic failure. See test_conftest_init_parity.py.
    """
    import test_no_production_writes_runtime as runtime

    sentinel = runtime.mint_sentinel()
    os.environ['OURLIBERTY_TEST_RUN_SENTINEL'] = sentinel
    session_start = time.time()
    undo = runtime.instrument_log_helpers(sentinel)
    try:
        yield
    finally:
        # Shared teardown (undo instrumentation + scan the real ~/agents tree),
        # identical to the unittest atexit path so the two cannot drift.
        _, message = runtime.run_session_end_tripwire(
            sentinel, session_start, undo, runner='pytest session',
        )
        os.environ.pop('OURLIBERTY_TEST_RUN_SENTINEL', None)
        if message:
            raise AssertionError(message)

