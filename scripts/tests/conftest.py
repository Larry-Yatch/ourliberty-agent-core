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

import pytest


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
