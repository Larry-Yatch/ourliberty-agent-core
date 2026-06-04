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

The temp dir is left for the OS's normal /tmp cleanup; no atexit hook
because (a) test runs are short-lived, (b) the worst case is a few KB of
test log lines accumulating until /tmp is cleared, (c) atexit makes
debugging harder when a test crashes and we want to inspect the logs.
"""
import os
import tempfile

if not os.environ.get('OURLIBERTY_LOG_DIR'):
    os.environ['OURLIBERTY_LOG_DIR'] = tempfile.mkdtemp(
        prefix='ourliberty-test-logs-',
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
